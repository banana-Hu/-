"""
Watcher 模块单元测试
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from wechat_helper.modules.watcher import Watcher
from wechat_helper.modules.event import MessageType


@pytest.fixture
def watcher():
    """创建 Watcher 实例"""
    watcher_config = {
        'watch_sessions': [],
        'max_queue_size': 1000,
        'heartbeat_timeout': 30,
        'history_fallback_count': 20,
    }
    weflow_config = {
        'base_url': 'http://127.0.0.1:5030',
        'token': 'test_token',
    }
    bridge = MagicMock()
    return Watcher(watcher_config, weflow_config, bridge)


def test_extract_text_text_message(watcher):
    """测试纯文本提取"""
    content = "hello world"
    assert watcher._extract_text(content) == "hello world"


def test_extract_text_with_title(watcher):
    """测试含 title 的 XML"""
    content = "<msg><appmsg><title>test title</title></appmsg></msg>"
    assert watcher._extract_text(content) == "test title"


def test_extract_text_placeholder(watcher):
    """测试占位符"""
    assert watcher._extract_text("[图片]") == "[图片]"


def test_extract_xml_attr(watcher):
    """测试 XML 属性提取"""
    content = "<msg><attachid>abc123</attachid></msg>"
    assert watcher._extract_xml_attr(content, 'attachid') == 'abc123'


def test_make_event_id_with_server_id(watcher):
    """测试带 serverId 的事件ID"""
    msg = {'serverId': '123456', 'createTime': 1787760000}
    eid = watcher._make_event_id(msg)
    assert eid == 'evt_123456'


def test_make_event_id_fallback(watcher):
    """测试 fallback 事件ID"""
    msg = {'createTime': 1787760000, 'sessionId': 'wxid_001', 'localId': 99}
    eid = watcher._make_event_id(msg)
    assert 'wxid_001' in eid


def test_parse_type_text(watcher):
    """测试文本类型解析"""
    assert watcher._parse_type('1') == MessageType.TEXT


def test_parse_type_image(watcher):
    """测试图片类型解析"""
    assert watcher._parse_type('3') == MessageType.IMAGE


def test_parse_type_unknown(watcher):
    """测试未知类型"""
    assert watcher._parse_type('999') == MessageType.UNKNOWN


def test_is_chinese(watcher):
    """测试中文检测"""
    assert watcher._is_chinese("你好") is True
    assert watcher._is_chinese("hello") is False
    assert watcher._is_chinese("hello 你好") is True