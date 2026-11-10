"""管理端的运行观测：指标汇总、最近运行、单次运行的结果与工具调用。"""

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_service.models import AgentToolCall, AgentTrace, KnowledgeChunk


def _state(trace: AgentTrace) -> str:
    """展示用状态：完成的运行显示结果类型（ANSWER / CLARIFY / …），否则显示运行状态。"""
    return (trace.result or {}).get("reply_type") or trace.status


class AdminService:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def metrics(self) -> dict[str, Any]:
        async def scalar(expression) -> Any:
            return await self._session.scalar(select(expression))

        average_latency = await scalar(func.avg(AgentTrace.latency_ms))
        return {
            "agent_runs": await scalar(func.count(AgentTrace.id)) or 0,
            "average_latency_ms": round(float(average_latency)) if average_latency is not None else 0,
            "input_tokens": int(await scalar(func.sum(AgentTrace.input_tokens)) or 0),
            "output_tokens": int(await scalar(func.sum(AgentTrace.output_tokens)) or 0),
            "tool_calls": await scalar(func.count(AgentToolCall.id)) or 0,
            "knowledge_chunks": await scalar(func.count(KnowledgeChunk.id)) or 0,
        }

    async def list_runs(self, limit: int) -> list[dict[str, Any]]:
        traces = (
            await self._session.scalars(
                select(AgentTrace).order_by(AgentTrace.created_at.desc()).limit(max(1, min(limit, 200)))
            )
        ).all()
        return [
            {
                "id": trace.id,
                "conversation_id": trace.conversation_id,
                "user_id": trace.user_id,
                "state": _state(trace),
                "status": trace.status,
                "model_name": trace.model_name,
                "latency_ms": trace.latency_ms,
                "input_tokens": trace.input_tokens,
                "output_tokens": trace.output_tokens,
                "created_at": trace.created_at.isoformat() if trace.created_at else None,
            }
            for trace in traces
        ]

    async def get_run(self, run_id: str) -> dict[str, Any]:
        trace = await self._session.get(AgentTrace, run_id)
        if trace is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "运行记录不存在")
        tool_calls = (
            await self._session.scalars(
                select(AgentToolCall).where(AgentToolCall.trace_id == run_id).order_by(AgentToolCall.created_at)
            )
        ).all()
        return {
            "run": {
                "id": trace.id,
                "conversation_id": trace.conversation_id,
                "user_id": trace.user_id,
                "state": _state(trace),
                "status": trace.status,
                "engine": trace.engine,
                "model_name": trace.model_name,
                "prompt_version": trace.prompt_version,
                "outcome": trace.result,
                "input_tokens": trace.input_tokens,
                "output_tokens": trace.output_tokens,
                "latency_ms": trace.latency_ms,
            },
            "tool_calls": [
                {
                    "tool_call_id": call.tool_call_id,
                    "tool_name": call.tool_name,
                    "arguments": call.arguments,
                    "success": call.success,
                    "result": call.result,
                    "latency_ms": call.latency_ms,
                }
                for call in tool_calls
            ],
        }
