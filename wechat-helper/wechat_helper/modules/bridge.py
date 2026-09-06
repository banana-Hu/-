"""
Bridge 模块 - Hermes Agent 通信桥

本地 HTTP 服务，让 Decision Agent（Hermes）能够：
- GET /api/bridge/events  拉取新消息事件
- POST /api/bridge/inject 推送 AI 生成的回复方案
- GET /api/bridge/status  查询 Operator 状态
- POST /api/bridge/stop   紧急停止所有操作
"""
import asyncio
import json
import logging
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

from aiohttp import web

from .event import WeChatEvent


class Bridge:
    """Hermes ↔ 本地进程通信桥"""

    def __init__(self, config: dict):
        self.host = config['host']
        self.port = config['port']
        self.auth_token = config['auth_token']
        self.db_path = Path(config['events_db'])

        self.logger = logging.getLogger(self.__class__.__name__)
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._ready = asyncio.Event()

        # 事件存储（SQLite 持久化 + 内存缓存）
        self._db_lock = threading.Lock()
        self._pending_events: List[WeChatEvent] = []  # 内存中的待处理事件

        # Operator 状态
        self._operator_status = {
            'running': False,
            'last_action': None,
            'last_action_time': None,
            'pending_reply': None,
        }

    async def start(self):
        """启动 HTTP 服务"""
        # 初始化数据库
        self._init_db()

        # 启动 aiohttp 服务
        self._app = web.Application()
        self._register_routes()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        self._ready.set()
        self.logger.info(f"✅ Bridge listening on http://{self.host}:{self.port}")

    async def stop(self):
        """停止 HTTP 服务"""
        self._ready.clear()
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        self.logger.info("Bridge stopped")

    def _register_routes(self):
        """注册 HTTP 路由"""
        self._app.router.add_get('/health', self.handle_health)
        self._app.router.add_get('/api/bridge/events', self.handle_get_events)
        self._app.router.add_get('/api/bridge/event/{event_id}', self.handle_get_event)
        self._app.router.add_post('/api/bridge/inject', self.handle_inject_replies)
        self._app.router.add_get('/api/bridge/status', self.handle_status)
        self._app.router.add_post('/api/bridge/stop', self.handle_emergency_stop)
        self._app.router.add_post('/api/bridge/operator/event', self.handle_operator_event)

    def _check_auth(self, request: web.Request) -> bool:
        """简单的 token 鉴权"""
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        return token == self.auth_token

    # ────────────── 事件推送 API（Watcher → Bridge）──────────────

    def push_event(self, event: WeChatEvent):
        """由 Watcher 调用，推送一个新事件"""
        with self._db_lock:
            # 持久化
            self._save_event(event)
            # 加入内存队列
            self._pending_events.append(event)

        self.logger.info(f"📨 New event: {event.session_name} | {event.content[:50]}")

    def _save_event(self, event: WeChatEvent):
        """保存事件到 SQLite"""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path.absolute()))
            try:
                conn.execute('''
                    INSERT OR REPLACE INTO events (
                        event_id, timestamp, received_at,
                        session_id, session_name, sender, sender_name,
                        message_type, content, raw_content,
                        quote_content, attach_id, attach_url,
                        source, suggested_replies, chosen_reply,
                        sent_at, send_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    event.event_id,
                    event.timestamp,
                    event.received_at,
                    event.session_id,
                    event.session_name,
                    event.sender,
                    event.sender_name,
                    event.message_type.value,
                    event.content,
                    event.raw_content,
                    event.quote_content,
                    event.attach_id,
                    event.attach_url,
                    event.source,
                    json.dumps(event.suggested_replies, ensure_ascii=False),
                    event.chosen_reply,
                    event.sent_at,
                    event.send_status,
                ))
                conn.commit()
            finally:
                conn.close()

    def _load_event(self, event_id: str) -> Optional[WeChatEvent]:
        """从 SQLite 加载事件"""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path.absolute()))
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    'SELECT * FROM events WHERE event_id = ?',
                    (event_id,)
                ).fetchone()
                if not row:
                    return None
                return WeChatEvent(
                    event_id=row['event_id'],
                    timestamp=row['timestamp'],
                    received_at=row['received_at'],
                    session_id=row['session_id'],
                    session_name=row['session_name'],
                    sender=row['sender'],
                    sender_name=row['sender_name'],
                    message_type=row['message_type'],
                    content=row['content'],
                    raw_content=row['raw_content'],
                    quote_content=row['quote_content'],
                    attach_id=row['attach_id'],
                    attach_url=row['attach_url'],
                    source=row['source'],
                    suggested_replies=json.loads(row['suggested_replies'] or '[]'),
                    chosen_reply=row['chosen_reply'],
                    sent_at=row['sent_at'],
                    send_status=row['send_status'],
                )
            finally:
                conn.close()

    # ────────────── HTTP Handlers（Hermes Agent 调用）──────────────

    async def handle_health(self, request: web.Request):
        """健康检查"""
        return web.json_response({'status': 'ok', 'time': time.time()})

    async def handle_get_events(self, request: web.Request):
        """
        GET /api/bridge/events?limit=20&status=pending

        Hermes Agent 拉取待处理事件
        """
        if not self._check_auth(request):
            return web.json_response({'error': 'unauthorized'}, status=401)

        limit = int(request.query.get('limit', 20))
        status = request.query.get('status', 'pending')

        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path.absolute()))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    'SELECT * FROM events WHERE send_status = ? ORDER BY received_at DESC LIMIT ?',
                    (status, limit)
                ).fetchall()
                events = [dict(row) for row in rows]
            finally:
                conn.close()

        return web.json_response({
            'success': True,
            'count': len(events),
            'events': events
        })

    async def handle_get_event(self, request: web.Request):
        """获取单个事件的详情"""
        if not self._check_auth(request):
            return web.json_response({'error': 'unauthorized'}, status=401)

        event_id = request.match_info['event_id']
        event = self._load_event(event_id)
        if not event:
            return web.json_response({'error': 'not_found'}, status=404)
        return web.json_response(event.to_dict())

    async def handle_inject_replies(self, request: web.Request):
        """
        POST /api/bridge/inject
        Body: {
            "event_id": "xxx",
            "suggested_replies": ["方案1", "方案2", "方案3"]
        }

        Hermes Agent 推送 AI 生成的回复方案
        """
        if not self._check_auth(request):
            return web.json_response({'error': 'unauthorized'}, status=401)

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({'error': 'invalid_json'}, status=400)

        event_id = data.get('event_id')
        replies = data.get('suggested_replies', [])

        if not event_id or not isinstance(replies, list):
            return web.json_response({
                'error': 'missing_fields',
                'required': ['event_id', 'suggested_replies']
            }, status=400)

        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path.absolute()))
            try:
                conn.execute(
                    'UPDATE events SET suggested_replies = ? WHERE event_id = ?',
                    (json.dumps(replies, ensure_ascii=False), event_id)
                )
                conn.commit()
            finally:
                conn.close()

        self.logger.info(f"💡 Replies injected for event {event_id[:8]}: {len(replies)} options")

        # 通知 Operator
        self._operator_status['pending_reply'] = {
            'event_id': event_id,
            'replies': replies,
            'created_at': time.time()
        }

        return web.json_response({
            'success': True,
            'event_id': event_id,
            'reply_count': len(replies)
        })

    async def handle_status(self, request: web.Request):
        """获取系统状态"""
        if not self._check_auth(request):
            return web.json_response({'error': 'unauthorized'}, status=401)

        return web.json_response({
            'success': True,
            'operator': self._operator_status,
            'bridge': {
                'host': self.host,
                'port': self.port,
                'uptime': time.time() - self._start_time if hasattr(self, '_start_time') else 0
            }
        })

    async def handle_emergency_stop(self, request: web.Request):
        """紧急停止所有操作"""
        if not self._check_auth(request):
            return web.json_response({'error': 'unauthorized'}, status=401)

        self.logger.warning("🛑 EMERGENCY STOP triggered")
        self._operator_status['emergency_stop'] = True
        return web.json_response({'success': True, 'action': 'stop'})

    async def handle_operator_event(self, request: web.Request):
        """Operator 状态上报"""
        if not self._check_auth(request):
            return web.json_response({'error': 'unauthorized'}, status=401)

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({'error': 'invalid_json'}, status=400)

        # 更新状态
        if 'action' in data:
            self._operator_status['last_action'] = data['action']
            self._operator_status['last_action_time'] = time.time()
        if 'event_id' in data:
            self._operator_status['current_event'] = data['event_id']

        self.logger.debug(f"Operator event: {data}")
        return web.json_response({'success': True})

    # ────────────── 初始化 ──────────────

    def _init_db(self):
        """初始化 SQLite 数据库"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path.absolute()))
            try:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS events (
                        event_id TEXT PRIMARY KEY,
                        timestamp INTEGER NOT NULL,
                        received_at REAL NOT NULL,
                        session_id TEXT,
                        session_name TEXT,
                        sender TEXT,
                        sender_name TEXT,
                        message_type TEXT,
                        content TEXT,
                        raw_content TEXT,
                        quote_content TEXT,
                        attach_id TEXT,
                        attach_url TEXT,
                        source TEXT,
                        suggested_replies TEXT,
                        chosen_reply TEXT,
                        sent_at REAL,
                        send_status TEXT DEFAULT 'pending'
                    )
                ''')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_session ON events(session_id)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_status ON events(send_status)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON events(timestamp)')
                conn.commit()
                self.logger.info(f"📁 Events DB ready: {self.db_path}")
            finally:
                conn.close()

        self._start_time = time.time()
