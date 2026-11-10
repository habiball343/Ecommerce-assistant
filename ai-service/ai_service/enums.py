from enum import StrEnum


class TraceStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReplyType(StrEnum):
    """一次处理的结果类型，客服服务据此决定直接回复还是创建人工工单。"""

    ANSWER = "ANSWER"  # 有可靠依据，直接回答
    CLARIFY = "CLARIFY"  # 信息不足，请用户补充或选择
    DECLINE = "DECLINE"  # 超出职责或暂时无法确认
    REQUEST_HANDOFF = "REQUEST_HANDOFF"  # 请求人工客服接管


class ToolCode(StrEnum):
    TOOL_CALL_FAILED = "TOOL_CALL_FAILED"
    KNOWLEDGE_FOUND = "KNOWLEDGE_FOUND"
    KNOWLEDGE_NOT_FOUND = "KNOWLEDGE_NOT_FOUND"
