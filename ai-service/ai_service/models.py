from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ai_service.db import JSONType, UTCDateTime
from ai_service.enums import TraceStatus
from ai_service.utils import new_id, utcnow


class Base(DeclarativeBase):
    pass


class AgentTrace(Base):
    """一次 AI 处理（Run）的审计记录：用了哪个引擎、最终结果、耗时与 Token。"""

    __tablename__ = "agent_traces"

    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: new_id("run"))
    conversation_id: Mapped[str] = mapped_column(String(80), index=True)
    user_id: Mapped[str] = mapped_column(String(40), index=True, default="")
    status: Mapped[str] = mapped_column(String(30), default=TraceStatus.RUNNING, index=True)
    engine: Mapped[str] = mapped_column(String(40), default="")
    model_name: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(80))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class AgentToolCall(Base):
    """一次只读工具调用及其结果，是“回答有依据”的证据来源。"""

    __tablename__ = "agent_tool_calls"

    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: new_id("call"))
    trace_id: Mapped[str] = mapped_column(ForeignKey("agent_traces.id"), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(120))
    tool_name: Mapped[str] = mapped_column(String(80), index=True)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONType)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class KnowledgeChunk(Base):
    """知识库切片：平台规则文档按段落切分后的一段内容。"""

    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: new_id("kb"))
    file_name: Mapped[str] = mapped_column(String(200), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text)
    # 可选的向量表示（配置了 Embedding 接口时写入）
    embedding: Mapped[list[float] | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
