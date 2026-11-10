"""只读工具箱：AI 获取业务事实与平台规则的唯一途径。

- 业务事实（商品、订单、物流、售后）来自电商服务，并且始终携带“当前用户”的令牌，
  所以 AI 能看到的数据范围与用户本人在页面上能看到的完全一致；
- 平台规则来自知识库；
- 每一次调用都会写入审计表，并作为“回答是否有依据”的证据供校验器使用。
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ai_service.db import session_factory
from ai_service.enums import ToolCode
from ai_service.models import AgentToolCall
from ai_service.services.knowledge import KnowledgeService
from ai_service.settings import get_settings
from ai_service.utils import new_id, normalize_resource_id

logger = logging.getLogger(__name__)

BUSINESS_TOOLS = frozenset(
    {"search_products", "get_product", "list_orders", "get_order", "get_logistics", "list_after_sales"}
)
KNOWLEDGE_TOOLS = frozenset({"search_knowledge"})


@dataclass(slots=True)
class ToolCall:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    latency_ms: int

    @property
    def success(self) -> bool:
        return bool(self.result.get("success"))

    @property
    def failed_to_reach(self) -> bool:
        """工具本身调用失败（网络错误、服务不可用），区别于“查到了但业务上不存在”。"""
        return self.result.get("code") == ToolCode.TOOL_CALL_FAILED

    @property
    def data(self) -> Any:
        return self.result.get("data")


@dataclass(slots=True)
class ToolBox:
    trace_id: str
    access_token: str
    http_client: httpx.AsyncClient
    knowledge: KnowledgeService
    calls: list[ToolCall] = field(default_factory=list)
    _cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ------------------------------------------------------------------ 业务工具（电商服务）

    async def search_products(self, query: str = "", *, tool_call_id: str | None = None) -> dict[str, Any]:
        query = (query or "").strip()[:100]
        return await self._ecommerce("search_products", {"query": query}, "/products", {"query": query}, tool_call_id)

    async def get_product(self, product_id: str, *, tool_call_id: str | None = None) -> dict[str, Any]:
        product_id = normalize_resource_id(product_id)
        return await self._ecommerce(
            "get_product", {"product_id": product_id}, f"/products/{product_id}", None, tool_call_id
        )

    async def list_orders(self, *, tool_call_id: str | None = None) -> dict[str, Any]:
        return await self._ecommerce("list_orders", {}, "/orders", None, tool_call_id)

    async def get_order(self, order_id: str, *, tool_call_id: str | None = None) -> dict[str, Any]:
        order_id = normalize_resource_id(order_id)
        return await self._ecommerce("get_order", {"order_id": order_id}, f"/orders/{order_id}", None, tool_call_id)

    async def get_logistics(self, order_id: str, *, tool_call_id: str | None = None) -> dict[str, Any]:
        order_id = normalize_resource_id(order_id)
        return await self._ecommerce(
            "get_logistics", {"order_id": order_id}, f"/orders/{order_id}/logistics", None, tool_call_id
        )

    async def list_after_sales(
        self, order_id: str | None = None, *, tool_call_id: str | None = None
    ) -> dict[str, Any]:
        arguments = {"order_id": normalize_resource_id(order_id)} if order_id else {}
        return await self._ecommerce("list_after_sales", arguments, "/after-sales", arguments or None, tool_call_id)

    # ------------------------------------------------------------------ 知识工具（平台规则）

    async def search_knowledge(self, query: str, *, tool_call_id: str | None = None) -> dict[str, Any]:
        arguments = {"query": (query or "").strip()[:200]}

        async def operation() -> dict[str, Any]:
            chunks = await self.knowledge.search(arguments["query"])
            if not chunks:
                return _result(False, ToolCode.KNOWLEDGE_NOT_FOUND, "知识库中没有找到相关的平台规则", [])
            return _result(True, ToolCode.KNOWLEDGE_FOUND, f"找到 {len(chunks)} 条相关规则", chunks)

        return await self._execute("search_knowledge", arguments, operation, tool_call_id)

    # ------------------------------------------------------------------ 内部实现

    async def _ecommerce(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        path: str,
        params: dict[str, Any] | None,
        tool_call_id: str | None,
    ) -> dict[str, Any]:
        settings = get_settings()

        async def operation() -> dict[str, Any]:
            response = await self.http_client.get(
                f"{settings.ecommerce_base_url}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=settings.ecommerce_timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
            return _result(bool(body["success"]), str(body["code"]), str(body.get("message", "")), body.get("data"))

        return await self._execute(tool_name, arguments, operation, tool_call_id)

    async def _execute(self, tool_name: str, arguments: dict[str, Any], operation, tool_call_id: str | None):
        # 同一次运行内，相同工具 + 相同参数只查一次
        cache_key = f"{tool_name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        started = time.perf_counter()
        try:
            result = await operation()
        except Exception:  # noqa: BLE001 - 任何失败都转成统一的“查询失败”结果，由上层降级处理
            logger.warning("工具 %s 调用失败", tool_name, exc_info=True)
            result = _result(False, ToolCode.TOOL_CALL_FAILED, "暂时无法获取到可靠的数据")
        latency_ms = int((time.perf_counter() - started) * 1000)

        call = ToolCall(
            tool_call_id=tool_call_id or new_id("toolcall"),
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            latency_ms=latency_ms,
        )
        self.calls.append(call)
        if not call.failed_to_reach:
            self._cache[cache_key] = result
        await self._save(call)
        return result

    async def _save(self, call: ToolCall) -> None:
        try:
            async with session_factory() as session:
                session.add(
                    AgentToolCall(
                        trace_id=self.trace_id,
                        tool_call_id=call.tool_call_id,
                        tool_name=call.tool_name,
                        arguments=call.arguments,
                        result=call.result,
                        success=call.success,
                        latency_ms=call.latency_ms,
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - 审计失败不应影响回答
            logger.exception("保存工具调用记录失败")


def _result(success: bool, code: str, message: str, data: Any = None) -> dict[str, Any]:
    return {"success": success, "code": str(code), "message": message, "data": data}
