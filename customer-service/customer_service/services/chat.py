"""对话主流程：接收用户消息、合并连续提问、交给 AI、保存并推送回复。"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from customer_service.enums import ConversationMode, MessageRole, ReplyType, TurnStatus
from customer_service.models import Conversation, ConversationTurn, Message
from customer_service.schemas import ChatMessageRequest, normalize_content
from customer_service.services.handoff import HandoffService
from customer_service.services.realtime import RealtimeService, message_payload
from customer_service.settings import get_settings
from customer_service.signals import notify_realtime
from customer_service.utils import new_id, utcnow

logger = logging.getLogger(__name__)

# AI Service 不可用时，用这些关键词识别“用户明确要求人工”，直接创建工单兜底
HANDOFF_KEYWORDS = ("转人工", "人工客服", "人工服务", "找人工", "真人客服", "转接人工")

AI_UNAVAILABLE_TEXT = "AI 客服暂时不可用，请稍后再试；如需人工协助，可以直接发送“转人工”。"
AI_UNAVAILABLE_HANDOFF_TEXT = "AI 客服暂时不可用，已为你转接人工客服，请稍候。"


@dataclass(slots=True, frozen=True)
class ClaimedTurn:
    """已被 Worker 领取、准备交给 AI Service 的轮次。"""

    turn_id: str
    conversation_id: str
    user_id: str
    payload: dict[str, Any]


def sanitize_reply(result: Any) -> dict[str, Any]:
    """校验 AI Service 的返回结构，只保留前端约定的字段。结构不合法时抛出 ValueError。"""
    if not isinstance(result, dict):
        raise ValueError("AI Service 返回的不是 JSON 对象")
    reply_type = result.get("reply_type")
    if reply_type not in set(ReplyType):
        raise ValueError(f"未知的 reply_type：{reply_type!r}")
    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("AI Service 返回的回复文本为空")

    content: dict[str, Any] = {"reply_type": str(reply_type), "text": text.strip()}

    page_action = result.get("page_action")
    if isinstance(page_action, dict):
        href = page_action.get("href")
        # 只允许站内路径，防止把用户带到外部链接
        if isinstance(href, str) and href.startswith("/") and not href.startswith("//"):
            content["page_action"] = {
                "label": str(page_action.get("label") or "前往办理"),
                "description": str(page_action.get("description") or ""),
                "href": href,
            }

    choices = []
    for choice in result.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        if choice.get("object_type") not in ("order", "product"):
            continue
        if not isinstance(choice.get("object_id"), str) or not isinstance(choice.get("label"), str):
            continue
        item = {
            "object_type": choice["object_type"],
            "object_id": choice["object_id"],
            "label": choice["label"],
            "description": str(choice.get("description") or ""),
        }
        if isinstance(choice.get("query_text"), str) and choice["query_text"].strip():
            item["query_text"] = choice["query_text"].strip()[:500]
        choices.append(item)
    if choices:
        content["choices"] = choices[:6]

    if reply_type == ReplyType.REQUEST_HANDOFF:
        content["handoff_summary"] = str(result.get("handoff_summary") or "").strip()[:2000]
    return content


class ChatService:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._settings = get_settings()
        self._realtime = RealtimeService(session)
        self._handoffs = HandoffService(session)

    # ------------------------------------------------------------------ 用户端接口

    async def get_history(self, user_id: str) -> dict[str, Any]:
        """返回当前会话信息，以及该用户全部会话的历史消息（前端按会话分组展示）。"""
        conversation = await self._get_or_create_conversation(user_id)
        stmt = (
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.user_id == user_id)
            .order_by(Message.id.desc())
            .limit(1000)
        )
        messages = list(reversed((await self._session.scalars(stmt)).all()))
        result = {
            "conversation_id": conversation.id,
            "conversation_mode": conversation.mode,
            "messages": [message_payload(item) for item in messages],
        }
        await self._session.commit()
        return result

    async def accept_message(self, payload: ChatMessageRequest, user_id: str) -> dict[str, Any]:
        """保存用户消息，并按会话当前的接待方分流：AI 模式进入轮次，人工模式通知管理端。"""
        content = normalize_content(payload.type, payload.content)
        for attempt in range(2):
            try:
                return await self._accept_message_once(payload, content, user_id)
            except IntegrityError:
                # 并发提交（同一 message_id 重复发送 / 同一用户同时新建会话）时重试一次
                await self._session.rollback()
                if attempt == 1:
                    raise
        raise RuntimeError("unreachable")

    async def _accept_message_once(
        self, payload: ChatMessageRequest, content: dict[str, Any], user_id: str
    ) -> dict[str, Any]:
        duplicate = await self._session.scalar(select(Message).where(Message.message_id == payload.message_id))
        if duplicate is not None:
            conversation = await self._session.get(Conversation, duplicate.conversation_id)
            await self._session.commit()
            return {
                "status": "duplicate",
                "conversation_id": duplicate.conversation_id,
                "conversation_mode": conversation.mode if conversation else None,
            }

        conversation = await self._get_or_create_conversation(user_id)
        now = utcnow()
        message = Message(
            message_id=payload.message_id,
            conversation_id=conversation.id,
            role=MessageRole.USER,
            message_type=payload.type,
            content=content,
            created_at=now,
        )
        self._session.add(message)
        conversation.last_active_at = now

        if conversation.mode == ConversationMode.AI:
            await self._touch_collecting_turn(conversation)
        else:
            # 排队或人工服务中：消息不再交给 AI，实时送到管理端
            self._realtime.add_message_event(message, conversation, notify_staff=True)

        await self._session.commit()
        notify_realtime()
        return {
            "status": "accepted",
            "conversation_id": conversation.id,
            "conversation_mode": conversation.mode,
        }

    async def _get_or_create_conversation(self, user_id: str) -> Conversation:
        """取得用户当前未关闭的会话；AI 模式下空闲超时的会话在这里关闭并新建。"""
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id, Conversation.mode != ConversationMode.CLOSED)
            .with_for_update()
        )
        conversation = await self._session.scalar(stmt)
        if conversation is not None:
            idle_limit = timedelta(minutes=self._settings.conversation_idle_timeout_minutes)
            expired = (
                conversation.mode == ConversationMode.AI and utcnow() - conversation.last_active_at >= idle_limit
            )
            if not expired:
                return conversation
            conversation.mode = ConversationMode.CLOSED
            await self._session.flush()

        conversation = Conversation(id=new_id("conv"), user_id=user_id, mode=ConversationMode.AI)
        self._session.add(conversation)
        await self._session.flush()
        return conversation

    async def _touch_collecting_turn(self, conversation: Conversation) -> ConversationTurn:
        """连续提问合并：每来一条消息就把收集窗口顺延一点，但累计等待不超过上限。"""
        now = utcnow()
        delay = timedelta(milliseconds=self._settings.message_merge_delay_ms)
        max_wait = timedelta(milliseconds=self._settings.message_merge_max_wait_ms)
        turn = await self._session.scalar(
            select(ConversationTurn).where(
                ConversationTurn.conversation_id == conversation.id,
                ConversationTurn.status == TurnStatus.COLLECTING,
            )
        )
        if turn is None:
            turn = ConversationTurn(
                conversation_id=conversation.id,
                status=TurnStatus.COLLECTING,
                collect_until=now + delay,
                created_at=now,
            )
            self._session.add(turn)
        else:
            turn.collect_until = max(turn.collect_until, min(now + delay, turn.created_at + max_wait))
        return turn

    # ------------------------------------------------------------------ AI Worker 使用

    async def recover_stale_turns(self) -> int:
        """服务重启后，把上次没处理完的轮次放回队列。"""
        stale_turns = (
            await self._session.scalars(
                select(ConversationTurn).where(ConversationTurn.status == TurnStatus.RUNNING)
            )
        ).all()
        for turn in stale_turns:
            has_newer = await self._session.scalar(
                select(ConversationTurn.id).where(
                    ConversationTurn.conversation_id == turn.conversation_id,
                    ConversationTurn.status == TurnStatus.COLLECTING,
                )
            )
            if has_newer:
                turn.status = TurnStatus.SUPERSEDED
            else:
                turn.status = TurnStatus.COLLECTING
                turn.collect_until = utcnow()
        await self._session.commit()
        return len(stale_turns)

    async def claim_next_turn(self) -> ClaimedTurn | None:
        """领取一个收集窗口已结束的轮次，并构建交给 AI Service 的请求。"""
        while True:
            running = aliased(ConversationTurn)
            stmt = (
                select(ConversationTurn)
                .where(
                    ConversationTurn.status == TurnStatus.COLLECTING,
                    ConversationTurn.collect_until <= utcnow(),
                    # 同一会话上一轮还没处理完时先不领取，等它结束后合并处理
                    ~exists().where(
                        running.conversation_id == ConversationTurn.conversation_id,
                        running.status == TurnStatus.RUNNING,
                    ),
                )
                .order_by(ConversationTurn.collect_until)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            turn = await self._session.scalar(stmt)
            if turn is None:
                await self._session.commit()
                return None

            conversation = await self._session.scalar(
                select(Conversation).where(Conversation.id == turn.conversation_id).with_for_update()
            )
            if conversation is None or conversation.mode != ConversationMode.AI:
                # 会话已经转给人工，这一轮不再需要 AI 回答
                turn.status = TurnStatus.SUPERSEDED
                await self._session.commit()
                continue

            snapshot_id = await self._latest_user_message_id(conversation.id)
            payload = await self._build_ai_payload(conversation, snapshot_id)
            if payload is None:
                turn.status = TurnStatus.COMPLETED
                await self._session.commit()
                continue

            turn.status = TurnStatus.RUNNING
            turn.snapshot_message_id = snapshot_id
            turn.attempts += 1
            await self._session.commit()
            return ClaimedTurn(
                turn_id=turn.id,
                conversation_id=conversation.id,
                user_id=conversation.user_id,
                payload=payload,
            )

    async def finish_turn(self, turn_id: str, result: Any, error: str | None) -> None:
        """结算轮次：保存 AI 回复 / 创建人工工单 / 失败重试或降级。"""
        turn = await self._session.scalar(
            select(ConversationTurn).where(ConversationTurn.id == turn_id).with_for_update()
        )
        if turn is None or turn.status != TurnStatus.RUNNING:
            await self._session.commit()
            return
        conversation = await self._session.scalar(
            select(Conversation).where(Conversation.id == turn.conversation_id).with_for_update()
        )

        # 处理期间用户又发了新消息：本次结果作废，新消息所在的轮次会把内容合并后重新提问
        latest_id = await self._latest_user_message_id(conversation.id)
        if latest_id != turn.snapshot_message_id or conversation.mode != ConversationMode.AI:
            turn.status = TurnStatus.SUPERSEDED
            await self._session.commit()
            return

        reply: dict[str, Any] | None = None
        if error is None:
            try:
                reply = sanitize_reply(result)
            except ValueError as exc:
                error = str(exc)
                logger.warning("会话 %s 的 AI 返回结构不合法：%s", conversation.id, exc)

        if reply is None:
            if turn.attempts < self._settings.ai_worker_max_attempts:
                turn.status = TurnStatus.COLLECTING
                turn.collect_until = utcnow() + timedelta(seconds=self._settings.ai_worker_retry_delay_seconds)
            else:
                turn.status = TurnStatus.FAILED
                await self._degrade_when_ai_unavailable(conversation, turn)
            await self._session.commit()
            notify_realtime()
            return

        turn.status = TurnStatus.COMPLETED
        if reply["reply_type"] == ReplyType.REQUEST_HANDOFF:
            summary = reply.get("handoff_summary") or "用户需要人工客服协助，请查看会话记录。"
            self._handoffs.create_handoff(conversation, summary)
            conversation.mode = ConversationMode.QUEUED
        self._save_ai_message(conversation, reply)
        await self._session.commit()
        notify_realtime()

    async def _degrade_when_ai_unavailable(self, conversation: Conversation, turn: ConversationTurn) -> None:
        """AI Service 多次调用失败后的降级：用户明确要人工就直接建工单，否则提示稍后重试。"""
        texts = await self._current_user_texts(conversation.id, turn.snapshot_message_id)
        wants_human = any(keyword in text for text in texts for keyword in HANDOFF_KEYWORDS)
        if wants_human:
            self._handoffs.create_handoff(
                conversation, "AI 服务不可用期间用户要求人工客服，请查看会话记录后接入处理。"
            )
            conversation.mode = ConversationMode.QUEUED
            self._save_ai_message(
                conversation,
                {"reply_type": str(ReplyType.REQUEST_HANDOFF), "text": AI_UNAVAILABLE_HANDOFF_TEXT},
            )
        else:
            self._save_ai_message(conversation, {"kind": "error", "text": AI_UNAVAILABLE_TEXT})

    def _save_ai_message(self, conversation: Conversation, content: dict[str, Any]) -> Message:
        now = utcnow()
        message = Message(
            message_id=new_id("msg"),
            conversation_id=conversation.id,
            role=MessageRole.AI,
            message_type="text",
            content=content,
            created_at=now,
        )
        conversation.last_active_at = now
        self._session.add(message)
        self._realtime.add_message_event(
            message,
            conversation,
            notify_user=True,
            # 转人工时同时通知管理端：有新的待处理工单
            notify_staff=conversation.mode == ConversationMode.QUEUED,
        )
        return message

    async def _latest_user_message_id(self, conversation_id: str) -> int | None:
        return await self._session.scalar(
            select(func.max(Message.id)).where(
                Message.conversation_id == conversation_id, Message.role == MessageRole.USER
            )
        )

    async def _latest_reply_id(self, conversation_id: str) -> int:
        return (
            await self._session.scalar(
                select(func.max(Message.id)).where(
                    Message.conversation_id == conversation_id, Message.role != MessageRole.USER
                )
            )
            or 0
        )

    async def _current_user_texts(self, conversation_id: str, snapshot_id: int | None) -> list[str]:
        stmt = select(Message).where(
            Message.conversation_id == conversation_id,
            Message.role == MessageRole.USER,
            Message.id > await self._latest_reply_id(conversation_id),
            Message.id <= (snapshot_id or 0),
        )
        return [str(item.content.get("text") or "") for item in (await self._session.scalars(stmt)).all()]

    async def _build_ai_payload(self, conversation: Conversation, snapshot_id: int | None) -> dict[str, Any] | None:
        """本轮消息 = 最近一次回复之后的全部用户消息；历史 = 在此之前最近的若干条消息。"""
        if snapshot_id is None:
            return None
        latest_reply_id = await self._latest_reply_id(conversation.id)
        current = (
            await self._session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.role == MessageRole.USER,
                    Message.id > latest_reply_id,
                    Message.id <= snapshot_id,
                )
                .order_by(Message.id)
            )
        ).all()
        if not current:
            return None
        history = (
            await self._session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id, Message.id < current[0].id)
                .order_by(Message.id.desc())
                .limit(self._settings.history_message_limit)
            )
        ).all()

        def to_item(message: Message) -> dict[str, Any]:
            return {"role": message.role, "type": message.message_type, "content": message.content}

        return {
            "conversation_id": conversation.id,
            "messages": [to_item(item) for item in current],
            "history": [to_item(item) for item in reversed(history)],
        }
