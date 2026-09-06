"""
Watcher 模块 - SSE 订阅与事件分发

订阅 WeFlow 的 /api/v1/push/messages SSE 流，
将新消息转换为标准 WeChatEvent 推送到 Bridge
"""
import asyncio
import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, List, Optional

from .event import WeChatEvent, MessageType
from .weflow_client import WeFlowClient


class Watcher:
    """WeFlow SSE 事件监听器"""

    def __init__(self, watcher_config: dict, weflow_config: dict, bridge):
        self.config = watcher_config
        self.bridge = bridge
        self.logger = logging.getLogger(self.__class__.__name__)

        # WeFlow 客户端配置
        self.weflow = WeFlowClient(
            base_url=weflow_config['base_url'],
            token=self._get_weflow_token(),
            timeout=30.0,
        )

        # 监控的会话白名单
        self.watch_sessions: List[str] = self.config.get('watch_sessions', [])

        # 会话名称缓存（避免每次请求 API）
        self._session_name_cache: Dict[str, str] = {}

        self._running = False
        self._task: Optional[asyncio.Task] = None

    def _get_weflow_token(self) -> str:
        """从 WeFlow 配置文件读取 token"""
        import json
        from pathlib import Path
        cfg_path = Path(r"C:\Users\hu\AppData\Roaming\weflow\WeFlow-config.json")
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
            return cfg.get('httpApiToken', '')
        return ''

    async def start(self):
        """启动 watcher"""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        self.logger.info("🛰  Watcher started")

    async def stop(self):
        """停止 watcher"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.weflow.close()
        self.logger.info("Watcher stopped")

    async def _run_loop(self):
        """主循环"""
        # 启动时先刷新会话名称缓存
        await self._refresh_session_names()

        while self._running:
            try:
                async for event_data in self.weflow.subscribe_push_events():
                    if not self._running:
                        break
                    await self._handle_event(event_data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Watcher loop error: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _refresh_session_names(self):
        """刷新会话名称缓存"""
        sessions = await self.weflow.get_sessions(limit=500)
        for s in sessions:
            self._session_name_cache[s.get('username', '')] = s.get('displayName', '')

        self.logger.info(f"📋 Cached {len(self._session_name_cache)} session names")

    async def _handle_event(self, event_data: dict):
        """处理一个 SSE 事件"""
        event_type = event_data.get('event', '')
        data = event_data.get('data', {})

        # WeFlow push 事件格式
        if event_type == 'ready':
            self.logger.info("SSE ready event received")
            return

        if event_type == 'message' or 'message' in data:
            await self._handle_message_event(data)

    async def _handle_message_event(self, data: dict):
        """处理消息事件"""
        try:
            # WeFlow 推送的数据结构
            # 可能是 {'message': {...}} 或直接 {...}
            msg = data.get('message', data)

            session_id = msg.get('sessionId', msg.get('talker', ''))
            local_type = str(msg.get('localType', '1'))

            # 白名单过滤
            if self.watch_sessions and session_id not in self.watch_sessions:
                return

            # 内容提取
            content_raw = msg.get('content', '')
            content_text = self._extract_text(content_raw)

            # 构造 event
            event = WeChatEvent(
                event_id=self._make_event_id(msg),
                timestamp=msg.get('createTime', int(datetime.now().timestamp())),
                session_id=session_id,
                session_name=self._session_name_cache.get(session_id, session_id),
                sender=msg.get('senderUsername', ''),
                sender_name=msg.get('senderDisplayName', ''),
                message_type=self._parse_type(local_type),
                content=content_text,
                raw_content=content_raw[:2000],
                attach_id=self._extract_xml_attr(content_raw, 'attachid'),
                attach_url=self._extract_xml_attr(content_raw, 'cdnattachurl'),
                source='weflow_sse',
            )

            # 推送到 bridge
            self.bridge.push_event(event)

        except Exception as e:
            self.logger.error(f"Failed to handle message event: {e}", exc_info=True)

    def _make_event_id(self, msg: dict) -> str:
        """生成事件 ID（用 serverId 或 fallback）"""
        sid = msg.get('serverId', '')
        if not sid:
            ts = msg.get('createTime', '')
            sess = msg.get('sessionId', msg.get('talker', ''))
            sid = f"{ts}-{sess}-{msg.get('localId', '')}"
        return f"evt_{sid}"

    def _parse_type(self, local_type: str) -> MessageType:
        """localType 字符串 → MessageType 枚举"""
        type_map = {
            '1': MessageType.TEXT,
            '3': MessageType.IMAGE,
            '34': MessageType.VOICE,
            '43': MessageType.VIDEO,
            '48': MessageType.LOCATION,
            '42': MessageType.CONTACT,
            '10000': MessageType.SYSTEM,
            '81604378673': MessageType.FORWARD,
            '25769803825': MessageType.FILE,
            '21474836529': MessageType.APPMSG,
        }
        return type_map.get(local_type, MessageType.UNKNOWN)

    def _extract_text(self, content: str) -> str:
        """从 XML 格式的内容中提取可读文本"""
        if not content:
            return ""

        # 尝试 XML 解析
        try:
            # 去除 CDATA 包装
            cleaned = re.sub(r'<!\[CDATA\[|\]\]>', '', content)

            # 提取 title
            title_match = re.search(r'<title>(?:<!\[CDATA\[)?([^<]*?)(?:\]\]>)?</title>', cleaned)
            if title_match:
                return title_match.group(1).strip()

            # 提取 recorditem（合并转发）
            items = re.findall(r'<recorditem[^>]*>([^<]*(?:<(?!recorditem)[^>]*>[^<]*)*)</recorditem>', cleaned)
            if items:
                return ' | '.join(re.sub(r'<[^>]+>', '', item).strip() for item in items[:10])

            # 纯文本：去 XML 标签
            text = re.sub(r'<[^>]+>', '', cleaned).strip()
            if text:
                return text[:500]
        except Exception:
            pass

        # 占位符
        placeholders = {'[图片]', '[视频]', '[表情]', '[语音]', '[位置]', '[名片]', '[文件]'}
        if content.strip() in placeholders:
            return content.strip()

        # 兜底：返回原文前 500 字
        return content[:500].strip()

    def _extract_xml_attr(self, content: str, attr: str) -> Optional[str]:
        """从 XML 中提取标签值"""
        m = re.search(rf'<{attr}>([^<]+)</{attr}>', content)
        return m.group(1) if m else None
