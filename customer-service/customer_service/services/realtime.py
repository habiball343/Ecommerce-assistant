"""实时事件：与业务数据在同一事务内写入发件箱表，再由 Worker 发布到事件总线。"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from customer_service.bus import STAFF_CHANNEL, user_channel
from customer_service.models import Conversation, Message, RealtimeEvent
from customer_service.utils import utcnow


def message_payload(message: Message) -> dict[str, Any]:
    """消息对外的统一结构（历史接口与实时事件共用）。"""
    return {
        "message_id": message.message_id,
        "conversation_id": message.conversation_id,
        "role": message.role,
        "type": message.message_type,
        "content": message.content,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


class RealtimeService:
    def __init__(self, session: AsyncSession):
        self._session = session

    def add_message_event(
        self,
        message: Message,
        conversation: Conversation,
        *,
        notify_user: bool = False,
        notify_staff: bool = False,
    ) -> None:
        """登记一条待推送的消息事件；不提交事务，由调用方统一提交。"""
        targets: list[str] = []
        if notify_user:
            targets.append(user_channel(conversation.user_id))
        if notify_staff:
            targets.append(STAFF_CHANNEL)
        if message.created_at is None:
            message.created_at = utcnow()
        data = {
            "event_type": "message",
            "conversation_mode": conversation.mode,
            "user_id": conversation.user_id,
            **message_payload(message),
        }
        for target in targets:
            self._session.add(RealtimeEvent(target=target, data=data))

    async def list_unpublished(self, limit: int = 50) -> list[RealtimeEvent]:
        stmt = (
            select(RealtimeEvent)
            .where(RealtimeEvent.published_at.is_(None))
            .order_by(RealtimeEvent.id)
            .limit(limit)
        )
        return list((await self._session.scalars(stmt)).all())

    async def purge_published(self, before) -> None:
        await self._session.execute(delete(RealtimeEvent).where(RealtimeEvent.published_at < before))
