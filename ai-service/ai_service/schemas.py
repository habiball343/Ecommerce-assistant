from typing import Any, Literal

from pydantic import BaseModel, Field


class CurrentUser(BaseModel):
    user_id: str
    role: Literal["user", "staff", "admin"] = "user"
    access_token: str


class AgentRunRequest(BaseModel):
    """客服服务发来的一轮咨询：本轮用户消息 + 之前的对话历史。"""

    conversation_id: str
    messages: list[dict[str, Any]] = Field(min_length=1)
    history: list[dict[str, Any]] = Field(default_factory=list)
