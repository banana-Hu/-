"""
WeFlowClient 单元测试
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from wechat_helper.modules.weflow_client import WeFlowClient
from wechat_helper.modules.event import WeChatEvent, MessageType


@pytest.mark.asyncio
async def test_get_sessions():
    """测试获取会话列表"""
    client = WeFlowClient("http://127.0.0.1:5030", "test_token")

    mock_response = MagicMock()
    mock_response.json.return_value = {
        'sessions': [
            {'username': 'wxid_001', 'displayName': 'Test User', 'sessionType': 'private'},
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch.object(client, '_ensure_client'):
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)

        sessions = await client.get_sessions()
        assert len(sessions) == 1
        assert sessions[0]['displayName'] == 'Test User'

    await client.close()


@pytest.mark.asyncio
async def test_get_messages():
    """测试获取消息"""
    client = WeFlowClient("http://127.0.0.1:5030", "test_token")

    mock_response = MagicMock()
    mock_response.json.return_value = {
        'messages': [
            {
                'serverId': '123',
                'createTime': 1787760000,
                'sessionId': 'wxid_001',
                'senderUsername': 'wxid_002',
                'content': 'hello',
                'localType': '1'
            }
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch.object(client, '_ensure_client'):
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)

        msgs = await client.get_messages('wxid_001')
        assert len(msgs) == 1
        assert msgs[0]['content'] == 'hello'

    await client.close()


def test_event_to_dict():
    """测试事件序列化"""
    event = WeChatEvent(
        event_id='evt_123',
        timestamp=1787760000,
        session_id='wxid_001',
        session_name='Test',
        sender='wxid_002',
        content='hello',
        message_type=MessageType.TEXT,
    )

    d = event.to_dict()
    assert d['event_id'] == 'evt_123'
    assert d['message_type'] == '1'  # enum value


def test_event_is_text():
    """测试消息类型判断"""
    event = WeChatEvent(
        event_id='evt_1',
        timestamp=0,
        message_type=MessageType.TEXT,
    )
    assert event.is_text() is True

    event2 = WeChatEvent(
        event_id='evt_2',
        timestamp=0,
        message_type=MessageType.IMAGE,
    )
    assert event2.is_text() is False