"""结果校验：回复交还客服服务之前的最后一道检查。

无论回复来自大模型还是规则引擎，都要通过同一套校验：
1. 关键业务事实（订单 / 商品 / 售后编号、金额与数量、订单与物流状态）必须能在工具返回的数据里找到依据；
2. 不得声称“已经替用户取消 / 修改 / 退款”——AI 只有只读权限；
3. 页面动作必须在白名单内，引用的订单必须经 get_order 确认，且订单状态允许该操作。
"""

import re
from dataclasses import dataclass
from typing import Any

from ai_service.agent.catalog import ACTIONS, ORDER_STATUS_LABELS, ActionCode, order_status_label
from ai_service.agent.toolbox import BUSINESS_TOOLS, ToolCall
from ai_service.enums import ReplyType
from ai_service.utils import normalize_resource_id

BUSINESS_FAILURE_MESSAGE = (
    "暂时无法查询到可靠的订单、商品、物流或售后信息，请稍后重试，或直接发送“转人工”由人工客服为你处理。"
)
DEFAULT_DECLINE_MESSAGE = (
    "抱歉，这个问题我暂时无法确认。我可以帮你查询商品、订单、物流和售后进度，或者解答平台规则；"
    "如需人工协助，请发送“转人工”。"
)
HANDOFF_TEXT = "已为你转接人工客服，请稍候。客服接入后会在当前会话中继续处理，不需要重新描述问题。"


class ValidationError(ValueError):
    """回复没有通过校验。错误信息会反馈给大模型用于修正。"""


@dataclass(slots=True)
class Draft:
    """待校验的回复草稿（大模型的结构化输出，或规则引擎的结果）。"""

    reply_type: ReplyType
    text: str
    handoff_summary: str | None = None
    action_code: ActionCode | None = None
    resource_id: str | None = None
    choices: list[dict[str, Any]] | None = None


def build_output(
    reply_type: ReplyType,
    text: str,
    *,
    handoff_summary: str | None = None,
    page_action: dict[str, Any] | None = None,
    choices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """AI Service 返回给客服服务的统一结构。"""
    return {
        "reply_type": str(reply_type),
        "text": text.strip(),
        "handoff_summary": handoff_summary,
        "page_action": page_action,
        "choices": choices or None,
    }


# --------------------------------------------------------------------------- 证据收集


@dataclass(slots=True)
class Evidence:
    texts: set[str]
    numbers: set[float]
    joined: str

    def has_text(self, value: str) -> bool:
        return value in self.texts or value in self.joined

    def has_number(self, literal: str) -> bool:
        if float(literal) in self.numbers:
            return True
        # 规则原文里的数字（例如“满 99 元包邮”）是以文本形式出现的
        digits = literal.rstrip("0").rstrip(".") if "." in literal else literal
        return re.search(rf"(?<![\d.]){re.escape(digits)}(?!\d)", self.joined) is not None


def collect_evidence(calls: list[ToolCall]) -> Evidence:
    texts: set[str] = set()
    numbers: set[float] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                texts.add(str(key))
                walk(item)
        elif isinstance(value, list):
            numbers.add(float(len(value)))
            for item in value:
                walk(item)
        elif isinstance(value, bool):
            return
        elif isinstance(value, int | float):
            numbers.add(float(value))
        elif isinstance(value, str):
            texts.add(value)

    for call in calls:
        walk(call.arguments)
        walk(call.data)
    return Evidence(texts=texts, numbers=numbers, joined="\n".join(sorted(texts)))


# --------------------------------------------------------------------------- 事实校验

_RESOURCE_ID_RE = re.compile(r"(?<![A-Za-z0-9_])(?:ORDER|PRODUCT|AS)[_\-][A-Za-z0-9_\-]*[A-Za-z0-9]", re.IGNORECASE)
_PRICE_RE = re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)")
_QUANTITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块钱|件|台|套|笔|单(?![号据位独]))")

# 回复中出现这些状态词时，必须有对应的业务数据支持
_STATUS_CODES = {
    **{label: code for code, label in ORDER_STATUS_LABELS.items()},
    "运输中": "in_transit",
    "已签收": "delivered",
    "已揽收": "picked_up",
    "物流异常": "exception",
}

_WRITE_CLAIM_RE = re.compile(
    r"(已经?|刚刚?)(为|帮|替)(您|你)(成功)?(把|将)?.{0,8}(取消|修改|更改|变更|退款|退货|换货|申请|提交|办理)"
    r"|我(已经?|刚刚?)(成功)?(取消|修改|更改|提交|申请|办理)"
    r"|(稍后|马上|立即|立刻|这就|现在就)(会|将)?(为|帮|替)(您|你).{0,8}(取消|修改|更改|退款|退货|换货|申请|提交|办理)"
    r"|(订单|地址|售后|退款).{0,8}(已|已经)(成功)?(取消|修改|更新|提交|受理)(成功|完成|好了)"
)


def find_unsupported_facts(text: str, evidence: Evidence) -> list[str]:
    unsupported: list[str] = []
    for raw_id in dict.fromkeys(_RESOURCE_ID_RE.findall(text)):
        resource_id = normalize_resource_id(raw_id)
        if not evidence.has_text(resource_id) and not evidence.has_text(raw_id):
            unsupported.append(resource_id)
    for literal in dict.fromkeys(_PRICE_RE.findall(text) + _QUANTITY_RE.findall(text)):
        if not evidence.has_number(literal):
            unsupported.append(literal)
    for label, code in _STATUS_CODES.items():
        if label in text and not evidence.has_text(code) and not evidence.has_text(label):
            unsupported.append(label)
    return unsupported


def check_facts(text: str, evidence: Evidence) -> None:
    unsupported = find_unsupported_facts(text, evidence)
    if unsupported:
        raise ValidationError(
            f"回复中的业务事实缺少工具结果支持：{'、'.join(unsupported)}。请只根据工具返回的数据回答，查不到就如实说明。"
        )


def check_read_only(text: str) -> None:
    if _WRITE_CLAIM_RE.search(text):
        raise ValidationError(
            "你只有只读查询权限，不能声称已经或即将替用户取消订单、修改地址、申请售后或退款；请改为引导用户到页面自行办理。"
        )


# --------------------------------------------------------------------------- 页面动作与候选卡片


def confirmed_order(order_id: str, calls: list[ToolCall]) -> dict[str, Any] | None:
    """返回经 get_order 成功确认过的订单数据。"""
    for call in reversed(calls):
        if call.tool_name == "get_order" and call.success and call.arguments.get("order_id") == order_id:
            return call.data
    return None


def validate_page_action(
    action_code: ActionCode | str | None, resource_id: str | None, calls: list[ToolCall]
) -> dict[str, Any] | None:
    if action_code is None:
        return None
    try:
        definition = ACTIONS[ActionCode(action_code)]
    except (ValueError, KeyError) as exc:
        raise ValidationError(f"页面动作 {action_code} 不在白名单内") from exc
    if not definition.needs_order:
        return definition.build()
    if not resource_id:
        raise ValidationError(f"页面动作 {definition.code} 必须提供订单编号")

    order_id = normalize_resource_id(resource_id)
    order = confirmed_order(order_id, calls)
    if order is None:
        raise ValidationError(f"页面动作引用的订单 {order_id} 还没有经过 get_order 工具确认")
    if not definition.allows(order.get("status", "")):
        raise ValidationError(
            f"订单 {order_id} 当前状态为「{order_status_label(order)}」，不能引导用户执行 {definition.code}；"
            "请如实说明原因，不要提供该页面动作。"
        )
    return definition.build(order_id)


def order_choices(orders: list[dict[str, Any]], question: str = "") -> list[dict[str, Any]]:
    """把订单列表转成前端可点击的候选卡片。question 会随用户的选择一起发回，保证上下文不丢。"""
    choices = []
    for order in orders[:5]:
        items = "、".join(item.get("product_name", "") for item in order.get("items", []))
        choice = {
            "object_type": "order",
            "object_id": order["id"],
            "label": f"{order['id']} · {items}" if items else order["id"],
            "description": f"¥{order.get('total_amount', 0):.2f} · {order_status_label(order)}",
        }
        if question:
            choice["query_text"] = question[:200]
        choices.append(choice)
    return choices


def product_choices(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "object_type": "product",
            "object_id": product["id"],
            "label": product.get("name", product["id"]),
            "description": f"¥{product.get('price', 0):.2f} · {'有货' if product.get('stock', 0) > 0 else '暂时缺货'}",
        }
        for product in products[:5]
    ]


def _default_clarify_choices(calls: list[ToolCall], question: str) -> list[dict[str, Any]] | None:
    for call in reversed(calls):
        if call.tool_name == "list_orders" and call.success and call.data:
            return order_choices(call.data, question)
    return None


def _supported_choices(choices: list[dict[str, Any]] | None, evidence: Evidence) -> list[dict[str, Any]] | None:
    if not choices:
        return None
    return [choice for choice in choices if evidence.has_text(str(choice.get("object_id", "")))] or None


# --------------------------------------------------------------------------- 入口


def validate(draft: Draft, calls: list[ToolCall], question: str = "") -> dict[str, Any]:
    """校验草稿并生成最终结果；不通过时抛出 ValidationError。"""
    text = (draft.text or "").strip()
    if not text:
        raise ValidationError("回复内容不能为空")
    evidence = collect_evidence(calls)

    if draft.reply_type == ReplyType.REQUEST_HANDOFF:
        summary = (draft.handoff_summary or "").strip() or f"用户需要人工客服协助。用户最新消息：{question[:300]}"
        return build_output(ReplyType.REQUEST_HANDOFF, text, handoff_summary=summary)

    check_read_only(text)

    if draft.reply_type == ReplyType.DECLINE:
        # 拒答时也不允许夹带没有依据的业务事实
        if find_unsupported_facts(text, evidence):
            text = DEFAULT_DECLINE_MESSAGE
        return build_output(ReplyType.DECLINE, text)

    business_calls = [call for call in calls if call.tool_name in BUSINESS_TOOLS]
    if business_calls and all(call.failed_to_reach for call in business_calls):
        # 业务数据全部查询失败：主动降级，不让回复建立在失败的查询之上
        return build_output(ReplyType.DECLINE, BUSINESS_FAILURE_MESSAGE)

    check_facts(text, evidence)

    if draft.reply_type == ReplyType.CLARIFY:
        choices = _supported_choices(draft.choices, evidence) or _default_clarify_choices(calls, question)
        return build_output(ReplyType.CLARIFY, text, choices=choices)

    page_action = validate_page_action(draft.action_code, draft.resource_id, calls)
    return build_output(
        ReplyType.ANSWER,
        text,
        page_action=page_action,
        choices=_supported_choices(draft.choices, evidence),
    )
