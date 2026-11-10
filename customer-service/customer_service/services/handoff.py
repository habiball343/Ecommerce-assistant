"""人工工单：创建、接入、回复、结束，以及管理员指标。"""

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.enums import ConversationMode, HandoffStatus, MessageRole
from customer_service.models import Conversation, Handoff, Message
from customer_service.services.realtime import RealtimeService, message_payload
from customer_service.utils import new_id, utcnow


def handoff_payload(handoff: Handoff) -> dict[str, Any]:
    return {
        "id": handoff.id,
        "conversation_id": handoff.conversation_id,
        "user_id": handoff.user_id,
        "summary": handoff.summary,
        "status": handoff.status,
        "staff_id": handoff.staff_id,
        "created_at": handoff.created_at.isoformat() if handoff.created_at else None,
    }


class HandoffService:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._realtime = RealtimeService(session)

    def create_handoff(self, conversation: Conversation, summary: str) -> Handoff:
        """AI 请求转人工时创建待处理工单（不提交事务）。"""
        handoff = Handoff(
            conversation_id=conversation.id,
            user_id=conversation.user_id,
            summary=summary.strip()[:2000] or "用户需要人工客服协助",
            status=HandoffStatus.WAITING,
        )
        self._session.add(handoff)
        return handoff

    async def list_open_handoffs(self) -> list[dict[str, Any]]:
        stmt = (
            select(Handoff)
            .where(Handoff.status.in_([HandoffStatus.WAITING, HandoffStatus.ACTIVE]))
            .order_by(Handoff.created_at)
        )
        return [handoff_payload(item) for item in (await self._session.scalars(stmt)).all()]

    async def get_messages(self, handoff_id: str) -> dict[str, Any]:
        """客服接入的是原会话：返回该会话的完整消息，用户不必重新描述问题。"""
        handoff = await self._session.get(Handoff, handoff_id)
        if handoff is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")
        stmt = select(Message).where(Message.conversation_id == handoff.conversation_id).order_by(Message.id)
        messages = (await self._session.scalars(stmt)).all()
        return {"handoff": handoff_payload(handoff), "messages": [message_payload(item) for item in messages]}

    async def accept(self, handoff_id: str, staff_id: str) -> dict[str, Any]:
        handoff, conversation = await self._load_for_update(handoff_id)
        if handoff.status == HandoffStatus.ACTIVE:
            # 多个客服同时抢单时，只有第一个成功
            raise HTTPException(status.HTTP_409_CONFLICT, f"该工单已被客服 {handoff.staff_id} 接入")
        if handoff.status != HandoffStatus.WAITING:
            raise HTTPException(status.HTTP_409_CONFLICT, "该工单已结束")

        handoff.status = HandoffStatus.ACTIVE
        handoff.staff_id = staff_id
        handoff.accepted_at = utcnow()
        conversation.mode = ConversationMode.HUMAN
        self._save_human_message(conversation, f"人工客服 {staff_id} 已接入，请问有什么可以帮你？")
        await self._session.commit()
        return handoff_payload(handoff)

    async def reply(self, handoff_id: str, staff_id: str, text: str) -> dict[str, Any]:
        handoff, conversation = await self._load_for_update(handoff_id)
        self._ensure_owner(handoff, staff_id)
        message = self._save_human_message(conversation, text.strip())
        await self._session.commit()
        return message_payload(message)

    async def resolve(self, handoff_id: str, staff_id: str) -> dict[str, Any]:
        handoff, conversation = await self._load_for_update(handoff_id)
        self._ensure_owner(handoff, staff_id)
        handoff.status = HandoffStatus.RESOLVED
        handoff.resolved_at = utcnow()
        # 人工服务结束后，原会话交还给 AI 继续接待
        conversation.mode = ConversationMode.AI
        self._save_human_message(conversation, "人工客服已结束本次服务，后续由 AI 客服继续为你服务。")
        await self._session.commit()
        return handoff_payload(handoff)

    async def metrics(self) -> dict[str, Any]:
        return {
            "conversations": await self._count(Conversation.id),
            "messages": await self._count(Message.id),
            "handoffs": await self._count(Handoff.id),
            "queued": await self._count(Conversation.id, Conversation.mode == ConversationMode.QUEUED),
            "human": await self._count(Conversation.id, Conversation.mode == ConversationMode.HUMAN),
            "resolved": await self._count(Handoff.id, Handoff.status == HandoffStatus.RESOLVED),
            "generated_at": utcnow().isoformat(),
        }

    async def _count(self, column, *conditions) -> int:
        return await self._session.scalar(select(func.count(column)).where(*conditions)) or 0

    async def _load_for_update(self, handoff_id: str) -> tuple[Handoff, Conversation]:
        handoff = await self._session.scalar(select(Handoff).where(Handoff.id == handoff_id).with_for_update())
        if handoff is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")
        conversation = await self._session.scalar(
            select(Conversation).where(Conversation.id == handoff.conversation_id).with_for_update()
        )
        return handoff, conversation

    @staticmethod
    def _ensure_owner(handoff: Handoff, staff_id: str) -> None:
        if handoff.status != HandoffStatus.ACTIVE:
            raise HTTPException(status.HTTP_409_CONFLICT, "请先接入会话再进行操作")
        if handoff.staff_id != staff_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"该工单由客服 {handoff.staff_id} 处理中")

    def _save_human_message(self, conversation: Conversation, text: str) -> Message:
        message = Message(
            message_id=new_id("msg"),
            conversation_id=conversation.id,
            role=MessageRole.HUMAN,
            message_type="text",
            content={"text": text},
            created_at=utcnow(),
        )
        conversation.last_active_at = utcnow()
        self._session.add(message)
        # 推给用户；同时通知管理端刷新（多个客服窗口保持同步）
        self._realtime.add_message_event(message, conversation, notify_user=True, notify_staff=True)
        return message
