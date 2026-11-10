"""大模型引擎：用 LangChain 1.x 的 Agent 组织“理解问题 → 调用只读工具 → 结构化输出”。

模型只负责理解问题和组织语言；业务事实一律通过工具获取，最终回复还要经过校验器检查。
支持 DeepSeek、通义千问、Ollama 以及任意 OpenAI 兼容接口，在 .env 中切换。
"""

import json
import logging
from dataclasses import dataclass
from typing import Annotated, Any

from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from ai_service.agent.analysis import message_text
from ai_service.agent.catalog import ActionCode, render_action_index
from ai_service.agent.toolbox import ToolBox
from ai_service.agent.validator import Draft, ValidationError, validate
from ai_service.enums import ReplyType
from ai_service.schemas import AgentRunRequest
from ai_service.settings import Settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- 系统提示词

SYSTEM_PROMPT = f"""你是「小二商城」的智能客服“小二”，通过只读工具为用户查询信息并给出可靠的回复。

【职责与边界】
1. 只处理电商相关咨询（商品、订单、物流、售后、平台规则）和日常寒暄；与电商无关的问题礼貌拒绝（DECLINE）。
2. 你只有只读权限：不能取消订单、修改地址、申请售后或退款，也不能承诺“稍后为您处理”。用户要办理这些业务时，在 submit_reply 中给出 action_code，引导用户去对应页面自己确认。
3. 你只能看到当前登录用户自己的订单和售后记录；工具返回“不存在”或“无权限”时如实告知，不要猜测。

【获取事实】
1. 订单、商品、价格、库存、物流、售后进度等业务事实必须来自工具返回结果；平台规则（退换货、运费、发票、时效等）必须来自 search_knowledge。严禁凭记忆或常识编造。
2. 用户没有给出订单编号时，先调用 list_orders。如果能根据问题唯一确定订单（例如只有一笔待发货的订单），直接回答并说明是哪一笔；如果有多笔可能的订单，用 CLARIFY 请用户选择，系统会自动附上订单卡片，文字里不需要逐条罗列订单。
3. 回答某一笔订单的详情，或给出订单相关的 action_code 之前，必须先调用 get_order 确认该订单。
4. 物流用 get_logistics；售后申请记录与进度用 list_after_sales；商品用 search_products（关键词要短，例如“耳机”；留空返回全部在售商品）或 get_product。
5. 工具返回失败或查不到时，如实说明“暂时无法确认”，不要用相同参数重复调用。

【页面动作 action_code（白名单）】
{render_action_index()}
只有用户想办理业务或查看页面时才提供；单纯查询信息时不要提供。订单状态不允许的操作不要提供对应动作，而是说明原因并给出可行的替代方案。

【回复类型 reply_type】
- ANSWER：已有可靠依据，可以直接回答；或日常寒暄。
- CLARIFY：缺少必要信息（例如不确定是哪一笔订单），需要用户补充或选择。
- DECLINE：与电商无关、超出职责，或工具无法提供可靠依据。
- REQUEST_HANDOFF：用户明确要求人工，或涉及投诉、交易争议等需要人工判断的问题。此时必须填写 handoff_summary，向人工客服概括用户诉求和已经查到的信息。不要仅仅因为查不到答案就转人工。

【表达要求】
1. 使用简体中文，语气友好、简洁，先给结论；可以使用 Markdown 列表，不要使用表格。
2. 引用订单编号、金额、数量、状态时，必须与工具结果完全一致；金额写成 ¥899.00 的形式。
3. 状态使用中文：paid=已支付，pending_shipment=待发货，shipped=已发货，completed=已完成，cancelled=已取消，refunded=已退款；物流 in_transit=运输中，delivered=已签收。
4. 完成所有必要的查询后，必须调用 submit_reply 工具提交最终回复，不要直接输出文本；submit_reply 不要和查询工具在同一步里调用。"""


# --------------------------------------------------------------------------- 工具定义


@dataclass(slots=True)
class RunContext:
    """一次运行的上下文：工具通过它拿到带用户身份的工具箱，并在这里交回最终回复。"""

    toolbox: ToolBox
    submitted: Draft | None = None


def _dump(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)


@tool
async def search_products(
    runtime: ToolRuntime[RunContext],
    query: Annotated[str, "商品名称或类别中的关键词，例如“耳机”；留空返回全部在售商品"] = "",
) -> str:
    """搜索在售商品，返回名称、类别、价格、库存、描述和规格。用户询问商品但没有给出商品编号时调用。"""
    return _dump(await runtime.context.toolbox.search_products(query, tool_call_id=runtime.tool_call_id))


@tool
async def get_product(
    product_id: Annotated[str, "商品编号，例如 PRODUCT_001"],
    runtime: ToolRuntime[RunContext],
) -> str:
    """查询一个商品的名称、价格、实时库存、描述和规格。已知商品编号时调用。"""
    return _dump(await runtime.context.toolbox.get_product(product_id, tool_call_id=runtime.tool_call_id))


@tool
async def list_orders(runtime: ToolRuntime[RunContext]) -> str:
    """查询当前用户的全部订单（编号、状态、金额、下单时间、商品）。用户没有给出订单编号时调用。"""
    return _dump(await runtime.context.toolbox.list_orders(tool_call_id=runtime.tool_call_id))


@tool
async def get_order(
    order_id: Annotated[str, "订单编号，例如 ORDER_001"],
    runtime: ToolRuntime[RunContext],
) -> str:
    """查询当前用户的一笔订单详情（状态、金额、收货地址、商品）。生成订单相关的页面动作之前必须先调用。"""
    return _dump(await runtime.context.toolbox.get_order(order_id, tool_call_id=runtime.tool_call_id))


@tool
async def get_logistics(
    order_id: Annotated[str, "订单编号，例如 ORDER_001"],
    runtime: ToolRuntime[RunContext],
) -> str:
    """查询一笔订单的物流公司、运单号、物流状态和轨迹。未发货的订单没有物流信息。"""
    return _dump(await runtime.context.toolbox.get_logistics(order_id, tool_call_id=runtime.tool_call_id))


@tool
async def list_after_sales(
    runtime: ToolRuntime[RunContext],
    order_id: Annotated[str | None, "可选的订单编号；提供后只查询该订单的售后记录"] = None,
) -> str:
    """查询当前用户的售后申请记录与处理进度（仅退款 / 退货退款 / 换货）。"""
    return _dump(await runtime.context.toolbox.list_after_sales(order_id, tool_call_id=runtime.tool_call_id))


@tool
async def search_knowledge(
    query: Annotated[str, "要查询的平台规则，例如“七天无理由退货的条件”"],
    runtime: ToolRuntime[RunContext],
) -> str:
    """查询平台规则知识库（退换货、退款时效、运费、发票、发货时效等）。不用于查询订单、库存等实时数据。"""
    return _dump(await runtime.context.toolbox.search_knowledge(query, tool_call_id=runtime.tool_call_id))


@tool(return_direct=True)
async def submit_reply(
    reply_type: Annotated[ReplyType, "本轮回复的类型：ANSWER / CLARIFY / DECLINE / REQUEST_HANDOFF"],
    text: Annotated[str, "直接展示给用户的完整回复，使用简体中文"],
    runtime: ToolRuntime[RunContext],
    handoff_summary: Annotated[
        str | None, "仅当 reply_type 为 REQUEST_HANDOFF 时填写：给人工客服看的问题摘要"
    ] = None,
    action_code: Annotated[
        ActionCode | None, "仅当用户要办理业务或查看页面时填写：白名单中的页面动作编码"
    ] = None,
    resource_id: Annotated[
        str | None, "action_code 对应的订单编号，必须是已经用 get_order 确认过的订单；browse_orders 不需要"
    ] = None,
) -> str:
    """提交给用户的最终回复。完成所有必要的查询之后调用，调用后本轮对话结束。"""
    runtime.context.submitted = Draft(
        reply_type=reply_type,
        text=text,
        handoff_summary=handoff_summary,
        action_code=action_code,
        resource_id=resource_id,
    )
    return "回复已提交"


# 结构化输出没有使用 LangChain 的 response_format：它会强制 tool_choice=required，
# 而部分 OpenAI 兼容接口（如 Ollama、部分通义模型）不支持该参数。
# 把“提交最终回复”做成一个普通工具，任何支持工具调用的模型都能用。
AGENT_TOOLS = [
    search_products,
    get_product,
    list_orders,
    get_order,
    get_logistics,
    list_after_sales,
    search_knowledge,
    submit_reply,
]


# --------------------------------------------------------------------------- 模型与消息


def build_chat_model(settings: Settings) -> BaseChatModel:
    """按提供商创建聊天模型。各家关闭“深度思考”的私有参数不同，在这里适配。"""
    provider = settings.llm_provider.lower()
    options: dict[str, Any] = {
        "model": settings.llm_model,
        "api_key": settings.llm_api_key or "not-needed",
        "base_url": settings.llm_base_url,
        "timeout": settings.llm_timeout_seconds,
        "temperature": settings.llm_temperature,
        "max_retries": 1,
    }
    if provider == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        thinking = "enabled" if settings.llm_enable_thinking else "disabled"
        return ChatDeepSeek(**options, extra_body={"thinking": {"type": thinking}})

    from langchain_openai import ChatOpenAI

    # use_responses_api=False：统一走 /chat/completions，兼容各家的 OpenAI 兼容接口
    if provider == "qwen":
        return ChatOpenAI(
            **options, use_responses_api=False, extra_body={"enable_thinking": settings.llm_enable_thinking}
        )
    return ChatOpenAI(**options, use_responses_api=False)


def build_messages(payload: AgentRunRequest, settings: Settings) -> list[BaseMessage]:
    """历史消息（按条数与字符预算裁剪）+ 本轮用户消息。"""
    kept: list[dict[str, Any]] = []
    used = 0
    for item in reversed(payload.history[-settings.history_message_limit :]):
        text = message_text(item)
        if not text:
            continue
        if used + len(text) > settings.history_character_budget:
            break
        kept.append(item)
        used += len(text)
    kept.reverse()
    # 对话必须从用户消息开始
    first_user = next((index for index, item in enumerate(kept) if item.get("role") == "USER"), len(kept))
    kept = kept[first_user:]

    messages: list[BaseMessage] = [
        HumanMessage(message_text(item)) if item.get("role") == "USER" else AIMessage(message_text(item))
        for item in kept
    ]
    current = [text for text in (message_text(item) for item in payload.messages) if text]
    if len(current) <= 1:
        messages.append(HumanMessage(current[0] if current else ""))
    else:
        numbered = "\n".join(f"{index}. {text}" for index, text in enumerate(current, start=1))
        messages.append(HumanMessage(f"用户连续发送了多条消息，请作为一个整体来理解：\n{numbered}"))
    return messages


# --------------------------------------------------------------------------- 引擎


class LLMEngine:
    def __init__(self, settings: Settings):
        self._settings = settings
        self.name = settings.llm_model
        self._agent = create_agent(
            model=build_chat_model(settings),
            tools=AGENT_TOOLS,
            system_prompt=SYSTEM_PROMPT,
            context_schema=RunContext,
            name="xiaoer-support-agent",
        )

    async def run(
        self,
        payload: AgentRunRequest,
        toolbox: ToolBox,
        question: str,
        usage: UsageMetadataCallbackHandler,
    ) -> dict[str, Any]:
        """运行 Agent 并校验结果；校验不通过时把原因反馈给模型重答。Token 用量累计到 usage 中。"""
        messages = build_messages(payload, self._settings)
        last_error: Exception | None = None

        for attempt in range(1, self._settings.agent_max_attempts + 1):
            context = RunContext(toolbox=toolbox)
            state = await self._agent.ainvoke(
                {"messages": messages},
                context=context,
                config={"callbacks": [usage], "recursion_limit": 24},
            )
            try:
                return validate(self._to_draft(context, state), toolbox.calls, question)
            except ValidationError as exc:
                last_error = exc
                logger.info("第 %s 次回复未通过校验：%s", attempt, exc)
                messages = [
                    *state["messages"],
                    HumanMessage(f"【系统校验】上一次回复没有通过校验：{exc}\n请修正后重新调用 submit_reply 提交最终回复。"),
                ]
        raise last_error or RuntimeError("Agent 没有产生结果")

    @staticmethod
    def _to_draft(context: RunContext, state: dict[str, Any]) -> Draft:
        if context.submitted is not None:
            return context.submitted
        # 个别模型不调用 submit_reply 而是直接回复文本：按普通回答处理，照样要过校验
        last = state["messages"][-1]
        text = last.content if isinstance(last.content, str) else str(last.content)
        return Draft(ReplyType.ANSWER, text)


def token_usage(usage: UsageMetadataCallbackHandler) -> tuple[int, int]:
    """汇总一次运行中所有模型调用的输入 / 输出 Token。"""
    totals = list(usage.usage_metadata.values())
    return (
        sum(int(item.get("input_tokens", 0)) for item in totals),
        sum(int(item.get("output_tokens", 0)) for item in totals),
    )
