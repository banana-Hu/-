"""
Bridge 模块单元测试
"""
import pytest
import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

from wechat_helper.modules.bridge import Bridge
from wechat_helper.modules.event import WeChatEvent, MessageType


@pytest.fixture
def tmp_config(tmp_path):
    """临时配置"""
    return {
        'host': '127.0.0.1',
        'port': 15031,  # 用不同端口避免冲突
        'auth_token': 'test_token',
        'events_db': str(tmp_path / 'events.db'),
    }


@pytest.mark.asyncio
async def test_bridge_start_stop(tmp_config):
    """测试启动和停止"""
    bridge = Bridge(tmp_config)
    await bridge.start()
    assert bridge._ready.is_set()
    await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_push_event(tmp_config):
    """测试事件推送"""
    bridge = Bridge(tmp_config)
    await bridge.start()

    event = WeChatEvent(
        event_id='evt_test_1',
        timestamp=1787760000,
        session_id='wxid_001',
        session_name='Test User',
        sender='wxid_002',
        sender_name='Sender',
        message_type=MessageType.TEXT,
        content='hello world',
    )

    bridge.push_event(event)

    # 验证数据库
    conn = sqlite3.connect(tmp_config['events_db'])
    cursor = conn.execute('SELECT COUNT(*) FROM events WHERE event_id = ?', ('evt_test_1',))
    assert cursor.fetchone()[0] == 1
    conn.close()

    await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_http_health(tmp_config):
    """测试 /health 端点"""
    from aiohttp import ClientSession

    bridge = Bridge(tmp_config)
    await bridge.start()

    try:
        async with ClientSession() as session:
            async with session.get(f"http://{tmp_config['host']}:{tmp_config['port']}/health") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data['status'] == 'ok'
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_inject_replies(tmp_config):
    """测试注入回复方案"""
    from aiohttp import ClientSession

    bridge = Bridge(tmp_config)
    await bridge.start()

    # 先创建一个事件
    event = WeChatEvent(
        event_id='evt_inject_test',
        timestamp=1787760000,
        session_id='wxid_001',
        session_name='Test',
        content='test',
        message_type=MessageType.TEXT,
    )
    bridge.push_event(event)

    try:
        async with ClientSession() as session:
            payload = {
                'event_id': 'evt_inject_test',
                'suggested_replies': ['reply 1', 'reply 2', 'reply 3']
            }
            headers = {'Authorization': f'Bearer {tmp_config["auth_token"]}'}

            async with session.post(
                f"http://{tmp_config['host']}:{tmp_config['port']}/api/bridge/inject",
                json=payload,
                headers=headers
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data['success'] is True
                assert data['reply_count'] == 3
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_unauthorized(tmp_config):
    """测试未授权访问"""
    from aiohttp import ClientSession

    bridge = Bridge(tmp_config)
    await bridge.start()

    try:
        async with ClientSession() as session:
            async with session.get(
                f"http://{tmp_config['host']}:{tmp_config['port']}/api/bridge/events"
            ) as resp:
                assert resp.status == 401
    finally:
        await bridge.stop()