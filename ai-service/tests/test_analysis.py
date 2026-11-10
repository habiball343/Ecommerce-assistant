"""前置分析（意图识别、订单指代、转人工识别）的单元测试。"""

import pytest

from ai_service.agent.analysis import Intent, analyze_turn, detect_handoff, find_order_ids


def text_message(text: str) -> dict:
    return {"role": "USER", "type": "text", "content": {"text": text}}


def card(object_type: str, object_id: str, query_text: str | None = None) -> dict:
    content = {"object_type": object_type, "object_id": object_id}
    if query_text:
        content["query_text"] = query_text
    return {"role": "USER", "type": "object", "content": content}


def ai_message(text: str, reply_type: str = "ANSWER") -> dict:
    return {"role": "AI", "type": "text", "content": {"reply_type": reply_type, "text": text}}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("你好", Intent.GREETING),
        ("在吗？", Intent.GREETING),
        ("谢谢啦", Intent.THANKS),
        ("我的订单什么时候发货？", Intent.LOGISTICS),
        ("ORDER_002 到哪了", Intent.LOGISTICS),
        ("快递怎么还没到", Intent.LOGISTICS),
        ("我想取消订单", Intent.CANCEL_ORDER),
        ("不想要了", Intent.CANCEL_ORDER),
        ("怎么取消订单", Intent.POLICY),
        ("订单发货后还能取消吗", Intent.POLICY),
        ("我想了解平台售后规则", Intent.POLICY),
        ("七天无理由退货运费谁出", Intent.POLICY),
        ("发票怎么开", Intent.POLICY),
        ("退款多久到账", Intent.POLICY),
        ("耳机坏了保修吗", Intent.POLICY),
        ("我的退款到哪了", Intent.AFTER_SALE_PROGRESS),
        ("售后进度怎么样了", Intent.AFTER_SALE_PROGRESS),
        ("我要退货", Intent.AFTER_SALE_APPLY),
        ("东西坏了怎么办", Intent.AFTER_SALE_APPLY),
        ("地址写错了", Intent.EDIT_ADDRESS),
        ("能帮我改一下收货地址吗", Intent.EDIT_ADDRESS),
        ("查一下我的订单", Intent.ORDER_LIST),
        ("ORDER_001 现在什么状态", Intent.ORDER_QUERY),
        ("那实付多少", Intent.ORDER_QUERY),
        ("耳机有货吗", Intent.PRODUCT),
        ("这款多少钱", Intent.PRODUCT),
        ("耳机什么时候到货", Intent.PRODUCT),
        ("你们卖什么", Intent.PRODUCT),
        ("你能做什么", Intent.CAPABILITY),
        ("今天天气怎么样", Intent.UNKNOWN),
    ],
)
def test_intent(text: str, expected: Intent) -> None:
    assert analyze_turn([text_message(text)], []).intent == expected


@pytest.mark.parametrize(
    ("text", "ids"),
    [
        ("ORDER_002 到哪了", ["ORDER_002"]),
        ("order-2 呢", ["ORDER_002"]),
        ("订单号 3 帮我看看", ["ORDER_003"]),
        ("订单 2 天了还没发货", []),
        ("我有 2 个订单", []),
    ],
)
def test_find_order_ids(text: str, ids: list[str]) -> None:
    assert find_order_ids(text) == ids


@pytest.mark.parametrize(
    ("text", "should_handoff"),
    [
        ("转人工", True),
        ("我要找人工客服", True),
        ("人工", True),
        ("你们这是欺诈，我要投诉", True),
        ("什么破平台，垃圾", True),
        ("不用转人工，我再看看", False),
        ("人工客服几点上班", False),
        ("我想取消订单", False),
        ("这个耳机的人工智能降噪怎么样", False),
    ],
)
def test_detect_handoff(text: str, should_handoff: bool) -> None:
    assert (detect_handoff(text) is not None) == should_handoff


def test_consecutive_messages_are_understood_together() -> None:
    analysis = analyze_turn([text_message("我的订单"), text_message("什么时候发货")], [])
    assert analysis.intent == Intent.LOGISTICS
    assert analysis.logistics_focus == "shipping"


def test_order_card_with_question() -> None:
    analysis = analyze_turn([card("order", "ORDER_001", "我想取消订单")], [])
    assert analysis.order_id == "ORDER_001"
    assert analysis.intent == Intent.CANCEL_ORDER


def test_order_card_alone_shows_order() -> None:
    analysis = analyze_turn([card("order", "order_003")], [])
    assert analysis.order_id == "ORDER_003"
    assert analysis.intent == Intent.ORDER_QUERY


def test_product_card_alone_is_product_consultation() -> None:
    analysis = analyze_turn([card("product", "PRODUCT_002")], [])
    assert analysis.product_id == "PRODUCT_002"
    assert analysis.intent == Intent.PRODUCT


def test_answer_to_clarify_recovers_original_question() -> None:
    history = [text_message("帮我看看物流到哪了"), ai_message("你想查询哪一笔订单？", "CLARIFY")]
    analysis = analyze_turn([text_message("ORDER_002")], history)
    assert analysis.order_id == "ORDER_002"
    assert analysis.intent == Intent.LOGISTICS


def test_follow_up_uses_order_from_context() -> None:
    history = [text_message("ORDER_002 到哪了"), ai_message("订单 **ORDER_002** 的物流信息：……")]
    analysis = analyze_turn([text_message("帮我取消这个订单")], history)
    assert analysis.order_id is None
    assert analysis.context_order_id == "ORDER_002"
    assert analysis.refers_to_context is True
    assert analysis.intent == Intent.CANCEL_ORDER


def test_listing_several_orders_is_not_a_context() -> None:
    history = [text_message("查一下我的订单"), ai_message("这是你的订单：ORDER_001、ORDER_002、ORDER_003")]
    assert analyze_turn([text_message("取消")], history).context_order_id is None


def test_order_that_was_not_found_is_not_a_context() -> None:
    history = [
        text_message("ORDER_001 现在什么状态"),
        ai_message("没有在你的账号下查到订单 ORDER_001，请核对订单编号是否正确。"),
    ]
    assert analyze_turn([text_message("转人工")], history).context_order_id is None
