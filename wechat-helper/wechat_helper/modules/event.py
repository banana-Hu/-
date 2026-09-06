"""
事件类型定义
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional


class MessageType(str, Enum):
    """消息类型枚举（与 WeFlow localType 对应）"""
    TEXT = "1"
    IMAGE = "3"
    VOICE = "34"
    VIDEO = "43"
    FILE = "25769803825"
    LOCATION = "48"
    CONTACT = "42"
    SYSTEM = "10000"
    FORWARD = "81604378673"
    APPMSG = "21474836529"  # 公众号/链接
    POKE = "266287972401"
    UNKNOWN = "0"


@dataclass
class WeChatEvent:
    """统一的微信消息事件结构"""

    # 基础信息
    event_id: str
    timestamp: int  # Unix timestamp (seconds)
    received_at: float = field(default_factory=lambda: datetime.now().timestamp())

    # 消息信息
    session_id: str = ""  # 会话 ID（wxid 或 群 ID）
    session_name: str = ""
    sender: str = ""  # 发送者 wxid
    sender_name: str = ""

    # 消息内容
    message_type: MessageType = MessageType.TEXT
    content: str = ""
    raw_content: str = ""

    # 引用/回复
    quote_content: Optional[str] = None

    # 附件信息
    attach_id: Optional[str] = None
    attach_url: Optional[str] = None

    # WeFlow 推送来源
    source: str = "weflow_sse"

    # ── Decision Agent 填写的字段 ──
    suggested_replies: list = field(default_factory=list)  # AI 生成的回复方案
    chosen_reply: Optional[str] = None  # 用户选择的方案
    sent_at: Optional[float] = None  # 实际发送时间
    send_status: str = "pending"  # pending / sent / failed / cancelled

    def to_dict(self) -> dict:
        """转字典（用于 JSON 持久化）"""
        d = asdict(self)
        d['message_type'] = self.message_type.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "WeChatEvent":
        """从字典恢复"""
        if isinstance(data.get('message_type'), str):
            data['message_type'] = MessageType(data['message_type'])
        return cls(**data)

    def is_text(self) -> bool:
        return self.message_type == MessageType.TEXT

    def is_incoming(self) -> bool:
        """是否收到的消息（非自己发出）"""
        # WeFlow 的 isSend 字段未在 SSE 事件中，需要从其他字段判断
        return True  # 默认假设 SSE 推送的都是收到的
