"""一次 AI 处理（Run）的编排：

    前置分析 ──► 需要人工？──是──► 请求转人工
        │否
        ▼
    大模型引擎（已配置时）──失败 / 超时 / 校验不过──► 规则引擎（降级）
        │                                              │
        └──────────────► 校验器 ◄──────────────────────┘
                            │
                            ▼
                  保存运行记录，返回客服服务
"""

import asyncio
import logging
import time
from typing import Any

import httpx
from fastapi import HTTPException, status
from langchain_core.callbacks import UsageMetadataCallbackHandler

from ai_service.agent.analysis import TurnAnalysis, analyze_turn
from ai_service.agent.llm_engine import LLMEngine, token_usage
from ai_service.agent.rule_engine import ENGINE_NAME as RULE_ENGINE_NAME
from ai_service.agent.rule_engine import RuleEngine
from ai_service.agent.toolbox import ToolBox
from ai_service.agent.validator import (
    DEFAULT_DECLINE_MESSAGE,
    HANDOFF_TEXT,
    ValidationError,
    build_output,
    validate,
)
from ai_service.db import session_factory
from ai_service.enums import ReplyType, TraceStatus
from ai_service.models import AgentTrace
from ai_service.schemas import AgentRunRequest, CurrentUser
from ai_service.services.knowledge import KnowledgeService
from ai_service.settings import get_settings

logger = logging.getLogger(__name__)


class AgentExecutor:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        knowledge: KnowledgeService,
        llm_engine: LLMEngine | None,
    ):
        self._settings = get_settings()
        self._http = http_client
        self._knowledge = knowledge
        self._llm = llm_engine
        self._rules = RuleEngine()

    @property
    def engine_label(self) -> str:
        return self._llm.name if self._llm else RULE_ENGINE_NAME

    async def start_run(self, payload: AgentRunRequest, user: CurrentUser) -> dict[str, Any]:
        trace_id = await self._create_trace(payload, user)
        toolbox = ToolBox(
            trace_id=trace_id, access_token=user.access_token, http_client=self._http, knowledge=self._knowledge
        )
        usage = UsageMetadataCallbackHandler()
        started = time.perf_counter()
        try:
            output, engine = await self._respond(payload, toolbox, usage)
        except Exception as exc:  # noqa: BLE001
            logger.exception("会话 %s 的 AI 处理失败", payload.conversation_id)
            await self._finish_trace(
                trace_id, TraceStatus.FAILED, "error", {"error": repr(exc)}, usage, started
            )
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "AI 服务处理失败") from exc

        await self._finish_trace(trace_id, TraceStatus.COMPLETED, engine, output, usage, started)
        return output

    async def _respond(
        self, payload: AgentRunRequest, toolbox: ToolBox, usage: UsageMetadataCallbackHandler
    ) -> tuple[dict[str, Any], str]:
        analysis = analyze_turn(payload.messages, payload.history)

        # 1. 明确要求人工、投诉争议、情绪激动：不再让 AI 尝试回答，直接请求人工接管
        if analysis.handoff_reason:
            return self._handoff_output(analysis), "router"

        # 2. 大模型引擎
        fell_back = False
        if self._llm is not None:
            try:
                output = await asyncio.wait_for(
                    self._llm.run(payload, toolbox, analysis.text, usage),
                    timeout=self._settings.agent_timeout_seconds,
                )
                return output, self._llm.name
            except Exception as exc:  # noqa: BLE001 - 模型不可用、超时、回复多次校验不过，都走降级
                fell_back = True
                logger.warning("大模型引擎没有给出可靠回复（%r），降级到规则引擎", exc)

        # 3. 规则引擎（未配置大模型时的主引擎，或大模型失败后的降级方案）
        draft = await self._rules.respond(analysis, toolbox)
        try:
            output = validate(draft, toolbox.calls, analysis.text)
        except ValidationError:
            logger.exception("规则引擎的回复没有通过校验，返回兜底话术")
            output = build_output(ReplyType.DECLINE, DEFAULT_DECLINE_MESSAGE)
        engine = f"{self._llm.name} → {RULE_ENGINE_NAME}" if fell_back and self._llm else RULE_ENGINE_NAME
        return output, engine

    @staticmethod
    def _handoff_output(analysis: TurnAnalysis) -> dict[str, Any]:
        """给人工客服的摘要：为什么转、用户说了什么、涉及哪笔订单 / 哪个商品。"""
        parts = [analysis.handoff_reason or "用户需要人工协助"]
        if analysis.text:
            parts.append(f"最新消息：{analysis.text[:200]}")
        order_id = analysis.order_id or analysis.context_order_id
        if order_id:
            parts.append(f"相关订单：{order_id}")
        product_id = analysis.product_id or analysis.context_product_id
        if product_id:
            parts.append(f"相关商品：{product_id}")
        return build_output(ReplyType.REQUEST_HANDOFF, HANDOFF_TEXT, handoff_summary="｜".join(parts))

    async def _create_trace(self, payload: AgentRunRequest, user: CurrentUser) -> str:
        async with session_factory() as session:
            trace = AgentTrace(
                conversation_id=payload.conversation_id,
                user_id=user.user_id,
                status=TraceStatus.RUNNING,
                engine="",
                model_name=self.engine_label,
                prompt_version=self._settings.prompt_version,
            )
            session.add(trace)
            await session.commit()
            return trace.id

    async def _finish_trace(
        self,
        trace_id: str,
        trace_status: TraceStatus,
        engine: str,
        result: dict[str, Any],
        usage: UsageMetadataCallbackHandler,
        started: float,
    ) -> None:
        input_tokens, output_tokens = token_usage(usage)
        try:
            async with session_factory() as session:
                trace = await session.get(AgentTrace, trace_id)
                if trace is None:
                    return
                trace.status = trace_status
                trace.engine = engine[:40]
                if engine and engine not in ("router", "error"):
                    trace.model_name = engine[:120]
                trace.result = result
                trace.input_tokens = input_tokens
                trace.output_tokens = output_tokens
                trace.latency_ms = int((time.perf_counter() - started) * 1000)
                await session.commit()
        except Exception:  # noqa: BLE001 - 审计失败不影响回复
            logger.exception("保存运行记录 %s 失败", trace_id)
