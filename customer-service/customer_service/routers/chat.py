from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.auth import UserDep
from customer_service.db import get_session
from customer_service.schemas import ChatMessageRequest
from customer_service.services.chat import ChatService

router = APIRouter(prefix="/api/v1/chat", tags=["用户对话"])


def get_chat_service(session: Annotated[AsyncSession, Depends(get_session)]) -> ChatService:
    return ChatService(session)


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]


@router.post("/messages", status_code=status.HTTP_202_ACCEPTED)
async def send_message(payload: ChatMessageRequest, service: ChatServiceDep, user: UserDep) -> dict[str, Any]:
    """接收用户消息。接口只负责保存并排队，AI 或人工的回复通过 WebSocket 实时推送。"""
    return await service.accept_message(payload, user.user_id)


@router.get("/history")
async def get_history(service: ChatServiceDep, user: UserDep) -> dict[str, Any]:
    """当前会话信息与该用户的全部历史消息。"""
    return await service.get_history(user.user_id)
