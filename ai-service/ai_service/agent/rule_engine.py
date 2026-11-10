"""规则引擎：不依赖大模型的确定性回答引擎。

两种场景会用到它：
1. 没有配置大模型 API Key —— 产品开箱即可完整演示；
2. 大模型调用失败、超时，或回复多次没有通过校验 —— 作为降级方案，保证用户总能得到有依据的回复。

它和大模型走的是同一条链路：理解问题 → 调用只读工具获取事实 / 规则 → 组织回复 → 交给校验器。

约定：「待发货」「已发货」这类状态词只用于描述查到的真实数据，固定话术里不出现，
否则校验器会认为回复包含了没有依据的业务状态。
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ai_service.agent.analysis import ORDER_SCOPED_INTENTS, Intent, TurnAnalysis
from ai_service.agent.catalog import (
    ADDRESS_EDITABLE_STATUSES,
    AFTER_SALE_BLOCKED_STATUSES,
    AFTER_SALE_KIND_LABELS,
    AFTER_SALE_STATUS_LABELS,
    CANCELLABLE_STATUSES,
    LOGISTICS_STATUS_LABELS,
    OPEN_AFTER_SALE_STATUSES,
    UNSHIPPED_STATUSES,
    ActionCode,
    order_status_label,
)
from ai_service.agent.toolbox import ToolBox
from ai_service.agent.validator import BUSINESS_FAILURE_MESSAGE, Draft, order_choices, product_choices
from ai_service.enums import ReplyType, ToolCode

ENGINE_NAME = "rules-engine"
BEIJING = timezone(timedelta(hours=8))

GREETING_TEXT = (
    "你好，我是小二，你的智能客服。我可以帮你：\n\n"
    "- 查询订单状态、发货与物流进度\n"
    "- 介绍商品的价格、库存和规格\n"
    "- 引导你取消订单、修改地址或申请售后\n"
    "- 解答退换货、运费、发票等平台规则\n\n"
    "直接告诉我你的问题就可以；需要人工时，发送“转人工”。"
)
THANKS_TEXT = "不客气，有其他问题随时找我。"
UNKNOWN_TEXT = (
    "抱歉，我没有完全理解你的问题。我可以帮你查询订单与物流、了解商品信息、办理取消 / 改地址 / 售后的入口，"
    "或者解答平台规则。可以换个说法再问我一次；如需人工协助，请发送“转人工”。"
)
NO_ORDER_TEXT = "当前账号下还没有订单记录，暂时没有可以查询或办理的订单。"

_GENERIC_PRODUCT_RE = re.compile(r"推荐|有什么|有哪些|哪些|卖什么|卖哪些|都有|所有|全部|商品列表|看看商品")
_ORDER_NOUN_RE = re.compile(r"订单|下单")
_ACTION_TOPIC_RE = re.compile(r"取消|地址|售后|退货|退款|换货|发票")


@dataclass(slots=True)
class ResolvedOrder:
    order: dict[str, Any]
    note: str = ""  # 自动选中订单时，向用户说明是怎么选出来的


# --------------------------------------------------------------------------- 展示用的小工具


def _money(value: Any) -> str:
    return f"¥{float(value):.2f}"


def _local_time(iso_text: str | None) -> str:
    if not iso_text:
        return ""
    try:
        moment = datetime.fromisoformat(iso_text)
    except ValueError:
        return iso_text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")


def _items_text(order: dict[str, Any]) -> str:
    return "、".join(f"{item['product_name']} ×{item['quantity']}" for item in order.get("items", []))


def _order_title(order: dict[str, Any]) -> str:
    names = "、".join(item["product_name"] for item in order.get("items", []))
    return f"订单 **{order['id']}**（{names}）" if names else f"订单 **{order['id']}**"


def _name_keys(name: str) -> tuple[set[str], set[str]]:
    """把商品名拆成用于匹配的关键词：英文单词 + 中文相邻两字。"""
    lowered = name.lower()
    words = {word for word in re.findall(r"[a-z0-9]+", lowered) if len(word) >= 3}
    bigrams: set[str] = set()
    for segment in re.findall(r"[一-鿿]+", lowered):
        bigrams.update(segment[index : index + 2] for index in range(len(segment) - 1))
    return words, bigrams


def _match_score(text: str, *names: str) -> int:
    lowered = text.lower()
    score = 0
    for name in names:
        words, bigrams = _name_keys(name)
        score += sum(1 for word in words if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", lowered))
        score += sum(1 for bigram in bigrams if bigram in lowered)
    return score


def _best_matches(scored: list[tuple[int, dict[str, Any]]], minimum: int) -> list[dict[str, Any]]:
    """保留得分明显领先的候选：既能分辨“降噪耳机”，也能在只说“耳机”时把两款都留下。"""
    top = max((score for score, _ in scored), default=0)
    if top < minimum:
        return []
    return [item for score, item in scored if score >= max(minimum, top * 0.6)]


# --------------------------------------------------------------------------- 规则引擎


class RuleEngine:
    name = ENGINE_NAME

    async def respond(self, analysis: TurnAnalysis, toolbox: ToolBox) -> Draft:
        intent = analysis.intent
        if intent == Intent.GREETING:
            return Draft(ReplyType.ANSWER, GREETING_TEXT)
        if intent == Intent.THANKS:
            return Draft(ReplyType.ANSWER, THANKS_TEXT)
        if intent == Intent.CAPABILITY:
            return Draft(ReplyType.ANSWER, GREETING_TEXT)
        if intent == Intent.POLICY:
            return await self._policy(analysis, toolbox)
        if intent == Intent.ORDER_LIST:
            return await self._order_list(toolbox)
        if intent == Intent.AFTER_SALE_PROGRESS:
            return await self._after_sale_progress(analysis, toolbox)
        if intent == Intent.PRODUCT:
            return await self._product(analysis, toolbox, minimum_score=1)
        if intent in ORDER_SCOPED_INTENTS:
            return await self._order_scoped(analysis, toolbox)
        return await self._unknown(analysis, toolbox)

    # ------------------------------------------------------------------ 平台规则

    async def _policy(self, analysis: TurnAnalysis, toolbox: ToolBox) -> Draft:
        result = await toolbox.search_knowledge(analysis.text)
        if result["code"] == ToolCode.TOOL_CALL_FAILED:
            return Draft(ReplyType.DECLINE, "知识库暂时无法访问，请稍后再试，或发送“转人工”由人工客服为你解答。")
        chunks = result["data"] or []
        if not chunks:
            return Draft(
                ReplyType.DECLINE,
                "知识库里暂时没有找到和这个问题相关的平台规则。你可以换个说法再问一次，或发送“转人工”由人工客服为你解答。",
            )
        # 第一条最相关；第二条只有在相关度接近时才一并给出，避免答非所问
        selected = [chunks[0]] + [chunk for chunk in chunks[1:2] if chunk["score"] >= 0.7 * chunks[0]["score"]]
        sections = []
        for chunk in selected:
            heading = f"**{chunk['title']}**\n\n" if chunk.get("title") else ""
            sections.append(f"{heading}{chunk['content']}")
        text = "根据平台规则：\n\n" + "\n\n".join(sections)
        if _ACTION_TOPIC_RE.search(analysis.text):
            text += "\n\n需要办理时，可以在「我的订单」里找到对应订单操作，也可以把订单编号发给我。"
            return Draft(ReplyType.ANSWER, text, action_code=ActionCode.BROWSE_ORDERS)
        return Draft(ReplyType.ANSWER, text)

    # ------------------------------------------------------------------ 订单

    async def _order_list(self, toolbox: ToolBox) -> Draft:
        listing = await toolbox.list_orders()
        if not listing["success"]:
            return Draft(ReplyType.DECLINE, BUSINESS_FAILURE_MESSAGE)
        orders = listing["data"] or []
        if not orders:
            return Draft(ReplyType.ANSWER, NO_ORDER_TEXT)
        lines = [
            f"- **{order['id']}** · {_items_text(order)} · {_money(order['total_amount'])} · {order_status_label(order)}"
            for order in orders[:10]
        ]
        text = "这是你的订单：\n\n" + "\n".join(lines) + "\n\n点击下面的订单可以继续咨询，也可以进入「我的订单」查看详情。"
        return Draft(
            ReplyType.ANSWER, text, action_code=ActionCode.BROWSE_ORDERS, choices=order_choices(orders, "")
        )

    async def _order_scoped(self, analysis: TurnAnalysis, toolbox: ToolBox) -> Draft:
        resolved = await self._resolve_order(analysis, toolbox)
        if isinstance(resolved, Draft):
            return resolved
        handler = {
            Intent.ORDER_QUERY: self._order_detail,
            Intent.LOGISTICS: self._logistics,
            Intent.CANCEL_ORDER: self._cancel,
            Intent.EDIT_ADDRESS: self._edit_address,
            Intent.AFTER_SALE_APPLY: self._after_sale_apply,
        }[analysis.intent]
        draft = await handler(resolved.order, toolbox)
        if resolved.note:
            draft.text = f"{resolved.note}\n\n{draft.text}"
        return draft

    async def _resolve_order(self, analysis: TurnAnalysis, toolbox: ToolBox) -> ResolvedOrder | Draft:
        """确定用户说的是哪一笔订单：明确编号 → 上文指代 → 按商品名 / 订单状态筛选 → 请用户选择。"""
        order_id = analysis.order_id
        note = ""
        preferred_id: str | None = None
        if order_id is None and analysis.context_order_id:
            if analysis.refers_to_context:
                # “帮我取消这个订单”：明确指代上文刚讨论的订单
                order_id = analysis.context_order_id
            elif not _ORDER_NOUN_RE.search(analysis.text):
                context_note = (
                    f"接着刚才咨询的订单 {analysis.context_order_id} 来看（如果你指的是其他订单，把订单编号发给我就行）："
                )
                if analysis.intent in (Intent.ORDER_QUERY, Intent.LOGISTICS):
                    # “那什么时候到？”这类追问：按上下文推断，并说明依据，方便用户纠正
                    order_id, note = analysis.context_order_id, context_note
                else:
                    # 办理类诉求（取消 / 改地址 / 售后）要更谨慎
                    discussed = await toolbox.get_order(analysis.context_order_id)
                    if discussed["success"] and not self._eligible_orders(analysis, [discussed["data"]])[0]:
                        # 刚讨论的订单办不了这件事：就这笔订单说明原因，而不是悄悄换成另一笔
                        return ResolvedOrder(discussed["data"], context_note)
                    # 办得了：只有当它是唯一符合条件的订单时才直接选中，否则请用户确认
                    preferred_id = analysis.context_order_id

        if order_id:
            result = await toolbox.get_order(order_id)
            if result["success"]:
                return ResolvedOrder(result["data"], note)
            if result["code"] == ToolCode.TOOL_CALL_FAILED:
                return Draft(ReplyType.DECLINE, BUSINESS_FAILURE_MESSAGE)
            # 订单不存在，或不属于当前用户：两种情况对用户的说法一致，不泄露他人订单是否存在
            return Draft(
                ReplyType.ANSWER,
                f"没有在你的账号下查到订单 {order_id}，请核对订单编号是否正确。你也可以在「我的订单」里直接选择订单来咨询。",
                action_code=ActionCode.BROWSE_ORDERS,
            )

        listing = await toolbox.list_orders()
        if not listing["success"]:
            return Draft(ReplyType.DECLINE, BUSINESS_FAILURE_MESSAGE)
        orders = listing["data"] or []
        if not orders:
            return Draft(ReplyType.ANSWER, NO_ORDER_TEXT)

        candidates = _best_matches(
            [
                (_match_score(analysis.text, *(item["product_name"] for item in order.get("items", []))), order)
                for order in orders
            ],
            minimum=1,
        )
        if candidates:
            if len(candidates) == 1 and len(orders) > 1:
                note = "根据你提到的商品，找到了这笔订单："
        else:
            eligible, description = self._eligible_orders(analysis, orders)
            if not eligible:
                return self._no_eligible_order(analysis, orders)
            candidates = eligible
            if len(candidates) == 1 and len(orders) > 1 and description:
                note = f"你目前只有一笔{description}的订单："

        if len(candidates) == 1:
            result = await toolbox.get_order(candidates[0]["id"])
            if not result["success"]:
                return Draft(ReplyType.DECLINE, BUSINESS_FAILURE_MESSAGE)
            return ResolvedOrder(result["data"], note)

        prompts = {
            Intent.ORDER_QUERY: "你想查询哪一笔订单？",
            Intent.LOGISTICS: "你想查询哪一笔订单的发货或物流进度？",
            Intent.CANCEL_ORDER: "下面这些订单目前可以取消，请选择要取消的订单：",
            Intent.EDIT_ADDRESS: "下面这些订单目前可以修改收货地址，请选择订单：",
            Intent.AFTER_SALE_APPLY: "请选择要申请售后的订单：",
        }
        # 上文刚讨论过的订单排在最前面
        candidates = sorted(candidates, key=lambda order: order["id"] != preferred_id)
        return Draft(
            ReplyType.CLARIFY, prompts[analysis.intent], choices=order_choices(candidates, analysis.text)
        )

    @staticmethod
    def _eligible_orders(analysis: TurnAnalysis, orders: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        """按诉求筛出“可能是用户所指”的订单，并给出筛选条件的描述。"""
        intent = analysis.intent
        if intent == Intent.CANCEL_ORDER:
            return [order for order in orders if order["status"] in CANCELLABLE_STATUSES], "可以取消"
        if intent == Intent.EDIT_ADDRESS:
            return [order for order in orders if order["status"] in ADDRESS_EDITABLE_STATUSES], "可以修改地址"
        if intent == Intent.AFTER_SALE_APPLY:
            return [order for order in orders if order["status"] not in AFTER_SALE_BLOCKED_STATUSES], "可以申请售后"
        if intent == Intent.LOGISTICS:
            if analysis.logistics_focus == "shipping":
                waiting = [order for order in orders if order["status"] in UNSHIPPED_STATUSES]
                if waiting:
                    return waiting, "待发货"
            if analysis.logistics_focus == "delivery":
                in_transit = [order for order in orders if order["status"] == "shipped"]
                if in_transit:
                    return in_transit, "运输途中"
            active = [order for order in orders if order["status"] in UNSHIPPED_STATUSES | {"shipped"}]
            return (active or orders), ""
        return orders, ""

    @staticmethod
    def _no_eligible_order(analysis: TurnAnalysis, orders: list[dict[str, Any]]) -> Draft:
        lines = "\n".join(f"- **{order['id']}** · {_items_text(order)} · {order_status_label(order)}" for order in orders[:10])
        reasons = {
            Intent.CANCEL_ORDER: "你目前没有可以直接取消的订单。订单只有在发货前可以取消；发货后可以在签收后申请退货。",
            Intent.EDIT_ADDRESS: "你目前没有可以修改收货地址的订单。订单只有在发货前可以修改地址，发货后无法修改。",
            Intent.AFTER_SALE_APPLY: "你目前没有可以申请售后的订单。订单取消或退款以后不能再发起售后。",
        }
        text = f"{reasons.get(analysis.intent, '没有找到符合条件的订单。')}\n\n你的订单：\n\n{lines}"
        return Draft(ReplyType.ANSWER, text, action_code=ActionCode.BROWSE_ORDERS)

    async def _order_detail(self, order: dict[str, Any], toolbox: ToolBox) -> Draft:
        lines = [
            f"**订单 {order['id']}** · {order_status_label(order)}",
            "",
            f"- 商品：{_items_text(order)}",
            f"- 实付：{_money(order['total_amount'])}",
            f"- 收货地址：{order['address']}",
            f"- 下单时间：{_local_time(order.get('created_at'))}",
        ]
        if order["status"] in ("shipped", "completed"):
            logistics = await toolbox.get_logistics(order["id"])
            if logistics["success"]:
                data = logistics["data"]
                status = LOGISTICS_STATUS_LABELS.get(data["status"], data["status"])
                lines.append(f"- 物流：{data['company']} {data['tracking_number']}，{status}，{data['latest_event']}")
        lines += ["", "你可以继续问我这笔订单的物流、取消、修改地址或售后，也可以进入订单详情查看。"]
        return Draft(
            ReplyType.ANSWER, "\n".join(lines), action_code=ActionCode.OPEN_ORDER_DETAIL, resource_id=order["id"]
        )

    async def _logistics(self, order: dict[str, Any], toolbox: ToolBox) -> Draft:
        title = _order_title(order)
        status = order["status"]
        detail_action = {"action_code": ActionCode.OPEN_ORDER_DETAIL, "resource_id": order["id"]}

        if status in UNSHIPPED_STATUSES:
            text = f"{title}目前是「{order_status_label(order)}」状态，还没有发货，所以暂时没有物流信息。"
            rule = await self._rule_excerpt(toolbox, "发货与配送时效 付款后多久发货")
            if rule:
                text += f"\n\n> 平台规则：{rule}"
            return Draft(ReplyType.ANSWER, text, **detail_action)
        if status in ("cancelled", "refunded"):
            return Draft(
                ReplyType.ANSWER, f"{title}的状态是「{order_status_label(order)}」，不会再发货。", **detail_action
            )

        logistics = await toolbox.get_logistics(order["id"])
        if logistics["code"] == ToolCode.TOOL_CALL_FAILED:
            return Draft(ReplyType.DECLINE, BUSINESS_FAILURE_MESSAGE)
        if not logistics["success"]:
            return Draft(
                ReplyType.ANSWER,
                f"{title}的状态是「{order_status_label(order)}」，但暂时还没有查到物流信息，可以稍后再来问我。",
                **detail_action,
            )
        data = logistics["data"]
        lines = [
            f"{title}的物流信息：",
            "",
            f"- 承运公司：{data['company']}",
            f"- 运单号：{data['tracking_number']}",
            f"- 物流状态：{LOGISTICS_STATUS_LABELS.get(data['status'], data['status'])}",
            f"- 最新进展：{data['latest_event']}",
        ]
        events = data.get("events") or []
        if events:
            lines += ["", "物流轨迹："]
            lines += [f"- {event.get('time', '')} {event.get('description', '')}".rstrip() for event in events]
        if data["status"] != "delivered":
            # 具体哪天送到只能以物流公司为准，这里补充平台的时效规则作为参考
            rule = await self._rule_excerpt(toolbox, "发货与配送时效 发货后多久送达", keyword="送达")
            if rule:
                lines += ["", f"> 平台规则：{rule}"]
        return Draft(ReplyType.ANSWER, "\n".join(lines), **detail_action)

    async def _cancel(self, order: dict[str, Any], toolbox: ToolBox) -> Draft:
        title = _order_title(order)
        status = order["status"]
        if status in CANCELLABLE_STATUSES:
            text = (
                f"{title}目前是「{order_status_label(order)}」状态，可以取消，实付 {_money(order['total_amount'])}。\n\n"
                "取消订单需要你本人确认，我没有权限代为操作。请点击下方按钮进入订单页面，核对信息后确认取消；取消后立即生效且无法恢复。"
            )
            return Draft(ReplyType.ANSWER, text, action_code=ActionCode.CANCEL_ORDER, resource_id=order["id"])
        if status == "shipped":
            text = f"{title}已发货，不能直接取消。你可以在签收后申请退货退款，申请入口在订单详情页。"
            return Draft(ReplyType.ANSWER, text, action_code=ActionCode.OPEN_ORDER_DETAIL, resource_id=order["id"])
        if status == "completed":
            text = f"{title}已完成，不能取消。如果商品有问题，可以申请售后。"
            return Draft(ReplyType.ANSWER, text, action_code=ActionCode.APPLY_AFTER_SALE, resource_id=order["id"])
        text = f"{title}的状态是「{order_status_label(order)}」，不需要也无法再次取消。"
        return Draft(ReplyType.ANSWER, text, action_code=ActionCode.OPEN_ORDER_DETAIL, resource_id=order["id"])

    async def _edit_address(self, order: dict[str, Any], toolbox: ToolBox) -> Draft:
        title = _order_title(order)
        if order["status"] in ADDRESS_EDITABLE_STATUSES:
            text = (
                f"{title}目前是「{order_status_label(order)}」状态，可以修改收货地址。\n\n"
                f"- 当前收货地址：{order['address']}\n\n"
                "修改地址需要你本人提交，我没有权限代为修改。请点击下方按钮进入订单页面填写新的地址。"
            )
            return Draft(ReplyType.ANSWER, text, action_code=ActionCode.EDIT_ADDRESS, resource_id=order["id"])
        text = (
            f"{title}的状态是「{order_status_label(order)}」，订单只有在发货前可以修改收货地址，"
            "所以这笔订单不能修改了。如果包裹还在途中需要协调，可以发送“转人工”联系人工客服。"
        )
        return Draft(ReplyType.ANSWER, text, action_code=ActionCode.OPEN_ORDER_DETAIL, resource_id=order["id"])

    async def _after_sale_apply(self, order: dict[str, Any], toolbox: ToolBox) -> Draft:
        title = _order_title(order)
        if order["status"] in AFTER_SALE_BLOCKED_STATUSES:
            text = f"{title}的状态是「{order_status_label(order)}」，这类订单不能再发起售后申请。"
            return Draft(ReplyType.ANSWER, text, action_code=ActionCode.OPEN_ORDER_DETAIL, resource_id=order["id"])

        records = await toolbox.list_after_sales(order["id"])
        if records["code"] == ToolCode.TOOL_CALL_FAILED:
            return Draft(ReplyType.DECLINE, BUSINESS_FAILURE_MESSAGE)
        open_record = next(
            (item for item in records["data"] or [] if item.get("status") in OPEN_AFTER_SALE_STATUSES), None
        )
        if open_record:
            text = (
                f"{title}已经有一个进行中的售后申请：\n\n{self._after_sale_line(open_record)}\n\n"
                "同一笔订单同一时间只能有一个进行中的售后申请，你可以在订单页面查看处理进度。"
            )
            return Draft(ReplyType.ANSWER, text, action_code=ActionCode.VIEW_AFTER_SALES, resource_id=order["id"])

        text = (
            f"{title}目前是「{order_status_label(order)}」状态，可以申请售后。\n\n"
            "售后类型有：仅退款、退货退款、换货。售后申请需要你本人提交，请点击下方按钮进入订单页面，选择售后类型并填写原因。"
        )
        return Draft(ReplyType.ANSWER, text, action_code=ActionCode.APPLY_AFTER_SALE, resource_id=order["id"])

    async def _after_sale_progress(self, analysis: TurnAnalysis, toolbox: ToolBox) -> Draft:
        order_id = analysis.order_id
        if order_id is None and analysis.context_order_id and analysis.refers_to_context:
            order_id = analysis.context_order_id

        records = await toolbox.list_after_sales(order_id)
        if not records["success"]:
            return Draft(ReplyType.DECLINE, BUSINESS_FAILURE_MESSAGE)
        items = records["data"] or []
        if not items:
            scope = f"订单 {order_id} " if order_id else "你的账号下"
            text = f"{scope}目前没有售后申请记录。如果需要售后，可以在对应订单的详情页提交申请。"
            rule = await self._rule_excerpt(toolbox, "退款时效与到账方式") if "到账" in analysis.text else None
            if rule:
                text += f"\n\n> 平台规则：{rule}"
            return Draft(ReplyType.ANSWER, text, action_code=ActionCode.BROWSE_ORDERS)

        text = "查到以下售后申请：\n\n" + "\n".join(self._after_sale_line(item) for item in items[:10])
        related_orders = {item["order_id"] for item in items}
        if len(related_orders) == 1:
            # 页面动作引用的订单必须先经 get_order 确认
            confirmed = await toolbox.get_order(next(iter(related_orders)))
            if confirmed["success"]:
                return Draft(
                    ReplyType.ANSWER, text, action_code=ActionCode.VIEW_AFTER_SALES, resource_id=confirmed["data"]["id"]
                )
        return Draft(ReplyType.ANSWER, text, action_code=ActionCode.BROWSE_ORDERS)

    @staticmethod
    def _after_sale_line(record: dict[str, Any]) -> str:
        kind = AFTER_SALE_KIND_LABELS.get(record.get("kind", ""), record.get("kind", ""))
        status = AFTER_SALE_STATUS_LABELS.get(record.get("status", ""), record.get("status", ""))
        reason = str(record.get("reason", "")).rstrip("。；;.，, ")
        return (
            f"- **{record['id']}**（订单 {record['order_id']}）：{kind}，当前{status}；"
            f"原因：{reason}；提交时间：{_local_time(record.get('created_at'))}"
        )

    # ------------------------------------------------------------------ 商品

    async def _product(self, analysis: TurnAnalysis, toolbox: ToolBox, *, minimum_score: int) -> Draft:
        if analysis.product_id:
            result = await toolbox.get_product(analysis.product_id)
            if result["success"]:
                return Draft(ReplyType.ANSWER, self._product_detail(result["data"]))
            if result["code"] == ToolCode.TOOL_CALL_FAILED:
                return Draft(ReplyType.DECLINE, BUSINESS_FAILURE_MESSAGE)

        listing = await toolbox.search_products("")
        if not listing["success"]:
            return Draft(ReplyType.DECLINE, BUSINESS_FAILURE_MESSAGE)
        products = listing["data"] or []
        if not products:
            return Draft(ReplyType.ANSWER, "目前没有在售的商品。")

        matched = _best_matches(
            [(_match_score(analysis.text, product["name"], product.get("category", "")), product) for product in products],
            minimum=minimum_score,
        )
        is_generic = bool(_GENERIC_PRODUCT_RE.search(analysis.text))
        if not matched and analysis.context_product_id and not analysis.product_id and not is_generic:
            # “它有货吗”“多少钱”这类追问，指的是上文刚看过的商品
            result = await toolbox.get_product(analysis.context_product_id)
            if result["success"]:
                return Draft(ReplyType.ANSWER, self._product_detail(result["data"]))
        if len(matched) == 1:
            return Draft(ReplyType.ANSWER, self._product_detail(matched[0]))

        if matched:
            intro = "找到以下相关商品："
            shown = matched
        elif analysis.product_id:
            intro = f"没有查到商品 {analysis.product_id}，下面是目前在售的商品："
            shown = products
        elif is_generic:
            intro = "目前在售的商品有："
            shown = products
        else:
            intro = "没有找到和你描述直接匹配的商品，下面是目前在售的商品："
            shown = products
        lines = [
            f"- **{product['name']}**（{product['id']}）· {product.get('category', '')} · "
            f"{_money(product['price'])} · {self._stock_text(product)}"
            for product in shown[:8]
        ]
        text = f"{intro}\n\n" + "\n".join(lines) + "\n\n点击下面的商品可以查看详细介绍。"
        return Draft(ReplyType.ANSWER, text, choices=product_choices(shown))

    @staticmethod
    def _stock_text(product: dict[str, Any]) -> str:
        stock = int(product.get("stock") or 0)
        return f"有货（库存 {stock} 件）" if stock > 0 else "暂时缺货"

    def _product_detail(self, product: dict[str, Any]) -> str:
        lines = [
            f"**{product['name']}**（{product['id']}）",
            "",
            f"- 分类：{product.get('category', '')}",
            f"- 价格：{_money(product['price'])}",
            f"- 库存：{self._stock_text(product)}",
            f"- 简介：{product.get('description', '')}",
        ]
        specs = product.get("specs") or {}
        if specs:
            lines.append("- 规格：" + "；".join(f"{key} {value}" for key, value in specs.items()))
        if int(product.get("stock") or 0) <= 0:
            lines += ["", "这款商品目前缺货，暂时无法确认到货时间。"]
        return "\n".join(lines)

    # ------------------------------------------------------------------ 其他

    async def _unknown(self, analysis: TurnAnalysis, toolbox: ToolBox) -> Draft:
        # 没有识别出明确诉求时，看看是不是在说某个商品（例如“机械键盘是什么轴”）
        if analysis.text:
            listing = await toolbox.search_products("")
            products = listing["data"] or [] if listing["success"] else []
            matched = _best_matches(
                [(_match_score(analysis.text, product["name"]), product) for product in products], minimum=2
            )
            if matched:
                return await self._product(analysis, toolbox, minimum_score=2)
        return Draft(ReplyType.DECLINE, UNKNOWN_TEXT)

    @staticmethod
    async def _rule_excerpt(toolbox: ToolBox, query: str, keyword: str = "") -> str | None:
        """取一条最相关的平台规则，作为业务回答的补充说明。keyword 用于在规则里挑出最贴切的那一句。"""
        result = await toolbox.search_knowledge(query)
        chunks = result["data"] or []
        if not result["success"] or not chunks:
            return None
        lines = [line for line in chunks[0]["content"].splitlines() if line.strip()]
        chosen = next((line for line in lines if keyword and keyword in line), lines[0] if lines else "")
        return chosen.lstrip("-•· ").strip() or None
