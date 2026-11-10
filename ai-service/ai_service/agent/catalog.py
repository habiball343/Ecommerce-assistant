"""业务目录：页面动作白名单、订单状态规则与展示文案。

AI 只能“引导用户去页面办理”，不能自己修改数据。能引导去哪些页面、在什么订单状态下可以引导，
全部由这里的服务端规则决定，而不是由模型自由发挥。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ActionCode(StrEnum):
    BROWSE_ORDERS = "browse_orders"
    OPEN_ORDER_DETAIL = "open_order_detail"
    CANCEL_ORDER = "cancel_order"
    EDIT_ADDRESS = "edit_address"
    APPLY_AFTER_SALE = "apply_after_sale"
    VIEW_AFTER_SALES = "view_after_sales"


# 与电商服务的业务规则保持一致
CANCELLABLE_STATUSES = frozenset({"paid", "pending_shipment"})
ADDRESS_EDITABLE_STATUSES = frozenset({"paid", "pending_shipment"})
AFTER_SALE_BLOCKED_STATUSES = frozenset({"cancelled", "refunded"})
UNSHIPPED_STATUSES = frozenset({"paid", "pending_shipment"})
SHIPPED_STATUSES = frozenset({"shipped", "completed"})
OPEN_AFTER_SALE_STATUSES = frozenset({"pending", "approved", "processing"})


@dataclass(slots=True, frozen=True)
class ActionDefinition:
    code: ActionCode
    label: str
    description: str
    path_template: str
    needs_order: bool = True
    # 允许生成该动作的订单状态；None 表示不限制
    allowed_statuses: frozenset[str] | None = None
    blocked_statuses: frozenset[str] = frozenset()

    def allows(self, order_status: str) -> bool:
        if order_status in self.blocked_statuses:
            return False
        return self.allowed_statuses is None or order_status in self.allowed_statuses

    def build(self, order_id: str | None = None) -> dict[str, Any]:
        return {
            "label": self.label,
            "description": self.description,
            "href": self.path_template.format(order_id=order_id or ""),
        }


ACTIONS: dict[ActionCode, ActionDefinition] = {
    definition.code: definition
    for definition in (
        ActionDefinition(
            ActionCode.BROWSE_ORDERS, "查看我的订单", "进入我的订单列表", "/me", needs_order=False
        ),
        ActionDefinition(
            ActionCode.OPEN_ORDER_DETAIL, "查看订单详情", "查看订单状态、物流与可用操作", "/me/orders/{order_id}"
        ),
        ActionDefinition(
            ActionCode.CANCEL_ORDER,
            "前往取消订单",
            "在订单页面核对信息并确认取消",
            "/me/orders/{order_id}/cancel",
            allowed_statuses=CANCELLABLE_STATUSES,
        ),
        ActionDefinition(
            ActionCode.EDIT_ADDRESS,
            "前往修改地址",
            "在订单页面填写新的收货地址",
            "/me/orders/{order_id}/address",
            allowed_statuses=ADDRESS_EDITABLE_STATUSES,
        ),
        ActionDefinition(
            ActionCode.APPLY_AFTER_SALE,
            "前往申请售后",
            "在订单页面选择售后类型并填写原因",
            "/me/orders/{order_id}/after-sale",
            blocked_statuses=AFTER_SALE_BLOCKED_STATUSES,
        ),
        ActionDefinition(
            ActionCode.VIEW_AFTER_SALES,
            "查看售后进度",
            "在订单页面查看售后记录",
            "/me/orders/{order_id}?section=after-sales",
        ),
    )
}


def render_action_index() -> str:
    """提供给大模型的页面动作说明。"""
    notes = {
        ActionCode.BROWSE_ORDERS: "不需要订单编号",
        ActionCode.OPEN_ORDER_DETAIL: "任何状态的订单都可以",
        ActionCode.CANCEL_ORDER: "仅订单状态为 paid / pending_shipment 时可用",
        ActionCode.EDIT_ADDRESS: "仅订单状态为 paid / pending_shipment 时可用",
        ActionCode.APPLY_AFTER_SALE: "订单状态为 cancelled / refunded 时不可用",
        ActionCode.VIEW_AFTER_SALES: "任何状态的订单都可以",
    }
    return "\n".join(
        f"- {definition.code}：{definition.description}（{notes[definition.code]}）"
        for definition in ACTIONS.values()
    )


ORDER_STATUS_LABELS = {
    "paid": "已支付",
    "pending_shipment": "待发货",
    "shipped": "已发货",
    "completed": "已完成",
    "cancelled": "已取消",
    "refunded": "已退款",
}
LOGISTICS_STATUS_LABELS = {
    "pending": "待揽收",
    "picked_up": "已揽收",
    "in_transit": "运输中",
    "delivered": "已签收",
    "exception": "物流异常",
}
AFTER_SALE_KIND_LABELS = {"refund": "仅退款", "return": "退货退款", "exchange": "换货"}
AFTER_SALE_STATUS_LABELS = {
    "pending": "待处理",
    "approved": "已通过",
    "processing": "处理中",
    "completed": "已完成",
    "rejected": "未通过",
}


def order_status_label(order: dict[str, Any]) -> str:
    return order.get("status_label") or ORDER_STATUS_LABELS.get(order.get("status", ""), order.get("status", ""))
