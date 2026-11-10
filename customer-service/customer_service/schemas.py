from typing import Any, Literal

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from customer_service.settings import get_settings


class CurrentUser(BaseModel):
    user_id: str
    role: Literal["user", "staff", "admin"] = "user"


class ChatMessageRequest(BaseModel):
    """用户端发送的消息。message_id 由前端生成，用于重复提交时去重。"""

    message_id: str = Field(min_length=1, max_length=80)
    type: Literal["text", "object"]
    content: dict[str, Any]


class ReplyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def normalize_content(message_type: str, content: dict[str, Any]) -> dict[str, Any]:
    """只保留约定的字段，避免把任意结构存进消息表再交给 AI。"""
    limit = get_settings().max_message_chars
    if message_type == "text":
        text = content.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "消息内容不能为空")
        if len(text) > limit:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"消息内容不能超过 {limit} 个字符")
        return {"text": text.strip()}

    object_type = content.get("object_type")
    object_id = content.get("object_id")
    if object_type not in ("order", "product") or not isinstance(object_id, str) or not object_id.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "对象消息必须包含 object_type 与 object_id")
    cleaned: dict[str, Any] = {"object_type": object_type, "object_id": object_id.strip()[:64]}
    query_text = content.get("query_text")
    if isinstance(query_text, str) and query_text.strip():
        cleaned["query_text"] = query_text.strip()[:500]
    return cleaned
