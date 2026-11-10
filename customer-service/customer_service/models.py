from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from customer_service.db import JSONType, UTCDateTime
from customer_service.enums import ConversationMode, HandoffStatus, TurnStatus
from customer_service.utils import new_id, utcnow


class Base(DeclarativeBase):
    pass


def _partial_unique(name: str, column: str, condition: str) -> Index:
    """带条件的唯一索引（PostgreSQL 与 SQLite 都支持），用数据库兜底保证业务不变量。"""
    return Index(name, column, unique=True, postgresql_where=text(condition), sqlite_where=text(condition))


class Conversation(Base):
    """一次客服会话。同一用户同一时间只能有一个未关闭的会话。"""

    __tablename__ = "conversations"
    __table_args__ = (
        _partial_unique("uq_open_conversation_per_user", "user_id", "mode IN ('AI', 'QUEUED', 'HUMAN')"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: new_id("conv"))
    user_id: Mapped[str] = mapped_column(String(40), index=True)
    mode: Mapped[str] = mapped_column(String(20), default=ConversationMode.AI, index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_active_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Message(Base):
    """会话中的一条消息（用户 / AI / 人工客服）。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(80), unique=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    message_type: Mapped[str] = mapped_column(String(20), default="text")
    content: Mapped[dict[str, Any]] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ConversationTurn(Base):
    """一次 AI 处理轮次：把用户连续发来的几条消息合并后交给 AI Service。"""

    __tablename__ = "conversation_turns"
    __table_args__ = (
        _partial_unique("uq_collecting_turn_per_conversation", "conversation_id", "status = 'COLLECTING'"),
        _partial_unique("uq_running_turn_per_conversation", "conversation_id", "status = 'RUNNING'"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: new_id("turn"))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default=TurnStatus.COLLECTING, index=True)
    # 交给 AI 时“最新一条用户消息”的主键；处理完成时若已变化，说明用户又补充了内容
    snapshot_message_id: Mapped[int | None] = mapped_column(Integer)
    collect_until: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RealtimeEvent(Base):
    """待发布的实时事件（发件箱）。与业务数据同事务写入，由 Worker 发布到事件总线。"""

    __tablename__ = "realtime_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    target: Mapped[str] = mapped_column(String(80), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)


class Handoff(Base):
    """人工工单。同一会话同一时间只能有一个未结束的工单。"""

    __tablename__ = "handoffs"
    __table_args__ = (
        _partial_unique("uq_open_handoff_per_conversation", "conversation_id", "status IN ('WAITING', 'ACTIVE')"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: new_id("handoff"))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(40), index=True)
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=HandoffStatus.WAITING, index=True)
    staff_id: Mapped[str | None] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
