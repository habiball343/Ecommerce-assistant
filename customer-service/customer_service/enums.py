from enum import StrEnum


class ConversationMode(StrEnum):
    """会话当前由谁接待。"""

    AI = "AI"  # AI 客服接待
    QUEUED = "QUEUED"  # 已转人工，等待客服接入
    HUMAN = "HUMAN"  # 人工客服服务中
    CLOSED = "CLOSED"  # 会话已结束


class MessageRole(StrEnum):
    USER = "USER"
    AI = "AI"
    HUMAN = "HUMAN"


class TurnStatus(StrEnum):
    """一次 AI 处理轮次的状态。"""

    COLLECTING = "COLLECTING"  # 正在收集（合并）用户连续发来的消息
    RUNNING = "RUNNING"  # 已交给 AI Service 处理
    COMPLETED = "COMPLETED"  # 处理完成，回复已保存
    FAILED = "FAILED"  # 多次调用失败
    SUPERSEDED = "SUPERSEDED"  # 处理期间用户又发了新消息，本次结果作废


class HandoffStatus(StrEnum):
    WAITING = "WAITING"  # 等待客服接入
    ACTIVE = "ACTIVE"  # 客服处理中
    RESOLVED = "RESOLVED"  # 已结束


class ReplyType(StrEnum):
    """AI Service 返回的处理结果类型。"""

    ANSWER = "ANSWER"
    CLARIFY = "CLARIFY"
    DECLINE = "DECLINE"
    REQUEST_HANDOFF = "REQUEST_HANDOFF"
