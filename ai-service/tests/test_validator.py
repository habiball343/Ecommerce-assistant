"""结果校验的单元测试：事实依据、只读约束、页面动作白名单。"""

import pytest

from ai_service.agent.catalog import ActionCode
from ai_service.agent.toolbox import ToolCall
from ai_service.agent.validator import Draft, ValidationError, validate
from ai_service.enums import ReplyType, ToolCode


def order(order_id: str, status: str, label: str, amount: float = 899.0) -> dict:
    return {
        "id": order_id,
        "user_id": "user_001",
        "status": status,
        "status_label": label,
        "total_amount": amount,
        "address": "上海市浦东新区世纪大道 100 号",
        "created_at": "2026-11-10T00:47:09+00:00",
        "items": [{"product_id": "PRODUCT_001", "product_name": "Aurora Pro 降噪耳机", "quantity": 1, "unit_price": amount}],
    }


def get_order_call(data: dict) -> ToolCall:
    return ToolCall(
        tool_call_id="call_1",
        tool_name="get_order",
        arguments={"order_id": data["id"]},
        result={"success": True, "code": "OK", "message": "订单查询成功", "data": data},
        latency_ms=3,
    )


def failed_call(tool_name: str) -> ToolCall:
    return ToolCall(
        tool_call_id="call_x",
        tool_name=tool_name,
        arguments={},
        result={"success": False, "code": str(ToolCode.TOOL_CALL_FAILED), "message": "暂时无法获取到可靠的数据", "data": None},
        latency_ms=8000,
    )


PENDING = order("ORDER_001", "pending_shipment", "待发货")
SHIPPED = order("ORDER_002", "shipped", "已发货", 399.0)


def test_grounded_answer_passes_and_builds_page_action() -> None:
    draft = Draft(
        ReplyType.ANSWER,
        "订单 ORDER_001 目前待发货，实付 ¥899.00，可以取消。",
        action_code=ActionCode.CANCEL_ORDER,
        resource_id="order_001",
    )
    output = validate(draft, [get_order_call(PENDING)])
    assert output["reply_type"] == "ANSWER"
    assert output["page_action"]["href"] == "/me/orders/ORDER_001/cancel"


@pytest.mark.parametrize(
    "text",
    [
        "订单 ORDER_009 已经发出了。",  # 没查过的订单编号
        "订单 ORDER_001 实付 ¥799.00。",  # 金额与数据不符
        "订单 ORDER_001 已发货，正在运输中。",  # 状态与数据不符
    ],
)
def test_unsupported_facts_are_rejected(text: str) -> None:
    with pytest.raises(ValidationError):
        validate(Draft(ReplyType.ANSWER, text), [get_order_call(PENDING)])


@pytest.mark.parametrize(
    "text",
    [
        "已为您取消订单 ORDER_001。",
        "我已经帮你修改了收货地址。",
        "稍后会为您办理退款。",
    ],
)
def test_claiming_write_operations_is_rejected(text: str) -> None:
    with pytest.raises(ValidationError):
        validate(Draft(ReplyType.ANSWER, text), [get_order_call(PENDING)])


def test_page_action_requires_confirmed_order() -> None:
    draft = Draft(ReplyType.ANSWER, "请前往订单页面办理。", action_code=ActionCode.CANCEL_ORDER, resource_id="ORDER_001")
    with pytest.raises(ValidationError, match="get_order"):
        validate(draft, [])


def test_page_action_respects_order_status() -> None:
    draft = Draft(
        ReplyType.ANSWER, "请前往订单页面取消。", action_code=ActionCode.CANCEL_ORDER, resource_id="ORDER_002"
    )
    with pytest.raises(ValidationError, match="已发货"):
        validate(draft, [get_order_call(SHIPPED)])


def test_all_business_tools_failed_degrades_to_decline() -> None:
    draft = Draft(ReplyType.ANSWER, "你的订单一切正常。")
    output = validate(draft, [failed_call("list_orders")])
    assert output["reply_type"] == "DECLINE"
    assert "转人工" in output["text"]


def test_clarify_gets_order_cards_with_original_question() -> None:
    listing = ToolCall(
        tool_call_id="call_2",
        tool_name="list_orders",
        arguments={},
        result={"success": True, "code": "OK", "message": "找到 2 个订单", "data": [PENDING, SHIPPED]},
        latency_ms=4,
    )
    output = validate(Draft(ReplyType.CLARIFY, "请选择要咨询的订单："), [listing], "我要申请售后")
    assert [choice["object_id"] for choice in output["choices"]] == ["ORDER_001", "ORDER_002"]
    assert all(choice["query_text"] == "我要申请售后" for choice in output["choices"])


def test_handoff_always_has_summary() -> None:
    output = validate(Draft(ReplyType.REQUEST_HANDOFF, "正在为你转接人工客服。"), [], "我要投诉")
    assert output["reply_type"] == "REQUEST_HANDOFF"
    assert "我要投诉" in output["handoff_summary"]


def test_rule_text_from_knowledge_counts_as_evidence() -> None:
    knowledge = ToolCall(
        tool_call_id="call_3",
        tool_name="search_knowledge",
        arguments={"query": "运费"},
        result={
            "success": True,
            "code": str(ToolCode.KNOWLEDGE_FOUND),
            "message": "找到 1 条相关规则",
            "data": [{"title": "运费规则", "content": "单笔订单实付满 99 元包邮，不满 99 元收取 8 元运费。订单已发货后不能修改地址。"}],
        },
        latency_ms=2,
    )
    output = validate(Draft(ReplyType.ANSWER, "满 99 元包邮，否则收取 8 元运费；已发货后不能改地址。"), [knowledge])
    assert output["reply_type"] == "ANSWER"
