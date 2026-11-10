"""理解用户本轮在问什么（前置分析）。

这里用确定性的规则完成三件事：
1. 把本轮的几条消息（文字、订单 / 商品卡片）整理成一段可理解的文字；
2. 识别是否需要直接转人工（明确要求人工、投诉争议、情绪激动）；
3. 给出意图与指向的订单 / 商品，供规则引擎使用，也作为大模型的参考。
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ai_service.utils import normalize_resource_id


class Intent(StrEnum):
    GREETING = "greeting"
    THANKS = "thanks"
    CAPABILITY = "capability"
    POLICY = "policy"  # 平台规则咨询 → 知识库
    ORDER_LIST = "order_list"  # 查看自己的订单
    ORDER_QUERY = "order_query"  # 某一笔订单的状态 / 金额 / 明细
    LOGISTICS = "logistics"  # 发货与物流进度
    CANCEL_ORDER = "cancel_order"
    EDIT_ADDRESS = "edit_address"
    AFTER_SALE_APPLY = "after_sale_apply"
    AFTER_SALE_PROGRESS = "after_sale_progress"
    PRODUCT = "product"  # 商品信息、价格、库存
    UNKNOWN = "unknown"


# 需要先确定“是哪一笔订单”才能回答的意图
ORDER_SCOPED_INTENTS = frozenset(
    {
        Intent.ORDER_QUERY,
        Intent.LOGISTICS,
        Intent.CANCEL_ORDER,
        Intent.EDIT_ADDRESS,
        Intent.AFTER_SALE_APPLY,
    }
)


@dataclass(slots=True)
class TurnAnalysis:
    text: str  # 本轮用户表达的完整文字（多条消息已合并）
    intent: Intent
    order_id: str | None = None  # 本轮明确指向的订单（卡片或文字中的订单编号）
    product_id: str | None = None
    context_order_id: str | None = None  # 上文刚讨论过的订单
    context_product_id: str | None = None
    refers_to_context: bool = False  # 是否在用“这个订单 / 它”指代上文
    logistics_focus: str | None = None  # shipping：关心何时发货；delivery：关心运输与送达
    handoff_reason: str | None = None  # 不为空表示应直接转人工


# --------------------------------------------------------------------------- 编号识别

_ORDER_ID_RE = re.compile(r"(?<![A-Za-z0-9_])ORDER[\s_\-]*(\d{1,8})(?![A-Za-z0-9])", re.IGNORECASE)
_ORDER_CN_RE = re.compile(r"订单(?:编号|号)?\s*[:：#]?\s*(\d{1,8})(?!\s*[\d个笔元天件%小分周月年次])")
_PRODUCT_ID_RE = re.compile(r"(?<![A-Za-z0-9_])PRODUCT[\s_\-]*(\d{1,8})(?![A-Za-z0-9])", re.IGNORECASE)


def find_order_ids(text: str) -> list[str]:
    ids = [f"ORDER_{digits.zfill(3)}" for digits in _ORDER_ID_RE.findall(text)]
    ids += [f"ORDER_{digits.zfill(3)}" for digits in _ORDER_CN_RE.findall(text)]
    return list(dict.fromkeys(ids))


def find_product_ids(text: str) -> list[str]:
    return list(dict.fromkeys(f"PRODUCT_{digits.zfill(3)}" for digits in _PRODUCT_ID_RE.findall(text)))


# --------------------------------------------------------------------------- 消息整理


def message_text(message: dict[str, Any]) -> str:
    """把一条消息转成给人 / 给模型看的文字。"""
    content = message.get("content") or {}
    if content.get("object_type"):
        kind = "订单" if content["object_type"] == "order" else "商品"
        text = f"【用户发送了{kind}卡片：{normalize_resource_id(str(content.get('object_id', '')))}】"
        if content.get("query_text"):
            text += f" {content['query_text']}"
        return text
    return str(content.get("text") or "").strip()


def _turn_parts(messages: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
    """返回 (文字列表, 订单卡片编号, 商品卡片编号)。"""
    texts: list[str] = []
    order_ids: list[str] = []
    product_ids: list[str] = []
    for message in messages:
        content = message.get("content") or {}
        if content.get("object_type"):
            object_id = normalize_resource_id(str(content.get("object_id", "")))
            (order_ids if content["object_type"] == "order" else product_ids).append(object_id)
            if content.get("query_text"):
                texts.append(str(content["query_text"]).strip())
        elif content.get("text"):
            texts.append(str(content["text"]).strip())
    return [text for text in texts if text], order_ids, product_ids


_NOT_FOUND_RE = re.compile(r"没有.{0,12}查到")


def _find_context(history: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """从最近的对话里找“正在讨论的订单 / 商品”。最近一条提到多笔订单时视为没有明确上下文。"""
    for item in reversed(history[-8:]):
        content = item.get("content") or {}
        if content.get("object_type") == "order":
            return normalize_resource_id(str(content.get("object_id", ""))), None
        if content.get("object_type") == "product":
            return None, normalize_resource_id(str(content.get("object_id", "")))
        searchable = f"{content.get('text') or ''} {(content.get('page_action') or {}).get('href') or ''}"
        order_ids = find_order_ids(searchable)
        product_ids = find_product_ids(searchable)
        if (order_ids or product_ids) and item.get("role") != "USER" and _NOT_FOUND_RE.search(searchable):
            # 最近一次提到的编号并没有查到（不存在或不属于该用户），不能当作上下文
            return None, None
        if order_ids or product_ids:
            return (
                order_ids[0] if len(order_ids) == 1 else None,
                product_ids[0] if len(product_ids) == 1 and not order_ids else None,
            )
    return None, None


def _pending_question(history: list[dict[str, Any]]) -> str | None:
    """上一条 AI 消息是在请用户补充信息（CLARIFY）时，找回用户当初的问题。"""
    if not history:
        return None
    last = history[-1]
    if last.get("role") == "USER" or (last.get("content") or {}).get("reply_type") != "CLARIFY":
        return None
    for item in reversed(history[:-1][-6:]):
        if item.get("role") != "USER":
            continue
        content = item.get("content") or {}
        question = content.get("text") or content.get("query_text")
        if question:
            return str(question).strip()
    return None


# --------------------------------------------------------------------------- 转人工识别

_HUMAN_RE = re.compile(
    r"转人工|转接人工|转客服|人工客服|人工服务|人工坐席|人工处理|真人|找人工|找客服|接人工|要人工|叫人工|换人工|活人|^人工[!！。.\s]*$"
)
_HUMAN_NEGATION_RE = re.compile(r"(不用|不需要|无需|不要|别|先不|暂时不).{0,3}(转|找|叫|接)?.{0,2}(人工|客服)")
_HUMAN_INFO_RE = re.compile(r"(人工|客服).{0,8}(时间|几点|上班|下班|电话|热线|在线吗|在哪)")
_COMPLAINT_RE = re.compile(r"投诉|举报|曝光|维权|12315|消协|工商局|起诉|律师|报警|赔偿|欺诈|诈骗|骗子|假货|虚假宣传")
_ANGER_RE = re.compile(r"垃圾|太差劲|气死|什么破|坑人|坑爹|恶心|废物|有病吧|忍不了|受不了了|太过分|什么玩意|去死|妈的|他妈|tmd|sb")


def detect_handoff(text: str) -> str | None:
    if _HUMAN_RE.search(text) and not _HUMAN_NEGATION_RE.search(text) and not _HUMAN_INFO_RE.search(text):
        return "用户明确要求人工客服"
    if _COMPLAINT_RE.search(text):
        return "用户提出投诉或交易争议"
    if _ANGER_RE.search(text.lower()):
        return "用户情绪激动，需要人工安抚并处理"
    return None


# --------------------------------------------------------------------------- 意图识别

_PUNCT = r"[\s!！。.~～,，、?？呀啊哦嘛吗呢吧啦了的]*"
_GREETING_RE = re.compile(
    rf"^(你好|您好|嗨|哈喽|hello|hi|hey|在吗|在不在|有人吗|早上好|上午好|中午好|下午好|晚上好|喂){_PUNCT}$",
    re.IGNORECASE,
)
_THANKS_RE = re.compile(
    rf"^(谢谢|感谢|多谢|辛苦|好的|好哒|好滴|ok|okay|嗯+|明白|知道了|没事了|没有了|不用了|再见|拜拜|bye)"
    rf"(你|您|啦|了)?{_PUNCT}$",
    re.IGNORECASE,
)
_CAPABILITY_RE = re.compile(
    r"你是谁|你是什么|你叫什么|你能(做|干|帮)|可以(帮我)?(做|干)(什么|啥)|有什么功能|能问什么|你会什么|机器人|怎么用你"
)
_STOCK_RE = re.compile(r"有货|到货|补货|库存|缺货|现货|没货|断货")
_POLICY_RE = re.compile(
    r"规则|政策|条款|规定|须知|无理由|运费|包邮|邮费|发票|质保|保修|保价|价保|价格保护|降价|会员权益|积分"
    r"|服务时间|工作时间|营业时间|几点(上班|下班)"
)
_HOWTO_RE = re.compile(
    r"怎么(?!样)|如何|怎样才|咋|流程|步骤|条件|能不能|可不可以|可以.{0,8}吗|能.{0,8}吗|支持.{0,6}吗|是否|多久|几天|多长时间|一般|通常|需要什么"
)
_OWN_RE = re.compile(
    r"我的|我要|我想|帮我|给我|替我|我买|我下|我那|我这|我刚|我昨|我前|我上|麻烦|查一下|查下|查询|帮忙"
    r"|还没|没收到|没到|没发|催"  # “快递怎么还没到”是在说自己的订单，不是问规则
)
_DEFECT_RE = re.compile(r"坏了|破损|质量问题|瑕疵|少发|发错|漏发|不好用|用不了|有问题|不满意|不合适")
_ANAPHORA_RE = re.compile(r"这个|这笔|这单|这件|该订单|此订单|那个|那笔|那单|刚才|刚刚|上面|上述|它")
_ORDER_NOUN_RE = re.compile(r"订单|下单|购买记录|买的东西")
_ORDER_LIST_RE = re.compile(
    r"(我的|有哪些|所有|全部|查看|看看|看一下|查一下|查下|查询|列出|多少).{0,4}订单|订单(列表|记录|有哪些)|买了(什么|哪些|啥)"
)
_ORDER_ATTRIBUTE_RE = re.compile(r"实付|订单金额|付了多少|花了多少|订单状态|什么状态|收货地址是")
_PRODUCT_RE = re.compile(
    r"商品|产品|价格|多少钱|价位|售价|规格|参数|配置|颜色|续航|防水|推荐|介绍|对比|区别|哪个好|哪款|哪种"
    r"|卖(什么|哪些|啥)|卖不卖|有卖|有(什么|哪些)(东西|货)|都有什么"
)

_ACTION_PATTERNS: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    (
        Intent.AFTER_SALE_PROGRESS,
        re.compile(
            r"(售后|退款|退货|换货|退钱).{0,8}(进度|到哪|哪一步|处理得|处理了|审核|结果|记录|状态|怎么样了|到账|好了吗|了吗|了没)"
            r"|售后单|钱.{0,4}(退|到账)"
        ),
    ),
    (Intent.CANCEL_ORDER, re.compile(r"取消|不想要了|不要了|撤销|退订|撤单")),
    (Intent.EDIT_ADDRESS, re.compile(r"地址|收货人|收件人|寄到|填错|改.{0,4}(电话|手机号)")),
    (
        Intent.LOGISTICS,
        re.compile(
            r"物流|快递|运单|单号|到哪|到了吗|到了没|什么时候(能)?(到|发|送)|几时(到|发)|多久(能)?(到|发)|何时(到|发)"
            r"|发货|配送|派送|签收|还没(收到|到|发)|没收到|催.{0,3}(发|单)|揽收|在路上|送达"
        ),
    ),
    (
        Intent.AFTER_SALE_APPLY,
        re.compile(r"退货|退款|换货|售后|退钱|想退|要退|想换|要换|维修|" + _DEFECT_RE.pattern),
    ),
)

_SHIPPING_RE = re.compile(r"发货|发出|没发|催发|(什么时候|几时|多久|何时)(能)?发")
_DELIVERY_RE = re.compile(
    r"到哪|到了|(什么时候|几时|多久|何时)(能)?(到|送)|签收|派送|配送|快递|物流|运单|单号|没收到|还没到|送达|在路上"
)


def detect_intent(text: str, *, has_explicit_order: bool = False, has_context_order: bool = False) -> Intent:
    stripped = text.strip()
    if not stripped:
        return Intent.ORDER_QUERY if has_explicit_order else Intent.UNKNOWN
    if _GREETING_RE.match(stripped):
        return Intent.GREETING
    if _THANKS_RE.match(stripped):
        return Intent.THANKS

    asks_stock = bool(_STOCK_RE.search(stripped))
    asks_howto = bool(_HOWTO_RE.search(stripped))
    about_own = bool(_OWN_RE.search(stripped) or _DEFECT_RE.search(stripped)) or has_explicit_order
    refers_back = has_context_order and bool(_ANAPHORA_RE.search(stripped))

    action: Intent | None = None
    if not asks_stock:  # “什么时候到货”问的是商品库存，不是订单物流
        action = next((intent for intent, pattern in _ACTION_PATTERNS if pattern.search(stripped)), None)

    # 明确在问“规则是什么”，且没有指向具体订单 → 知识库
    if _POLICY_RE.search(stripped) and not has_explicit_order and not refers_back:
        return Intent.POLICY
    if action is not None:
        # “我想取消订单”是办理诉求；“怎么取消订单”是在问规则
        if about_own or refers_back or not asks_howto:
            return action
        return Intent.POLICY
    if has_explicit_order:
        return Intent.ORDER_QUERY
    if _ORDER_LIST_RE.search(stripped):
        return Intent.ORDER_LIST
    if _ORDER_ATTRIBUTE_RE.search(stripped):
        return Intent.ORDER_QUERY
    if asks_stock or _PRODUCT_RE.search(stripped):
        return Intent.PRODUCT
    if _ORDER_NOUN_RE.search(stripped):
        return Intent.POLICY if asks_howto and not about_own else Intent.ORDER_QUERY
    if _CAPABILITY_RE.search(stripped):
        return Intent.CAPABILITY
    return Intent.UNKNOWN


def _logistics_focus(text: str) -> str | None:
    shipping, delivery = bool(_SHIPPING_RE.search(text)), bool(_DELIVERY_RE.search(text))
    if shipping and not delivery:
        return "shipping"
    if delivery and not shipping:
        return "delivery"
    return None


def analyze_turn(messages: list[dict[str, Any]], history: list[dict[str, Any]]) -> TurnAnalysis:
    texts, card_order_ids, card_product_ids = _turn_parts(messages)
    text = "；".join(texts)

    order_ids = card_order_ids + [item for item in find_order_ids(text) if item not in card_order_ids]
    product_ids = card_product_ids + [item for item in find_product_ids(text) if item not in card_product_ids]
    order_id = order_ids[-1] if order_ids else None
    product_id = product_ids[-1] if product_ids else None

    # 用户是在回答上一轮的追问（只给了订单 / 商品，没有再重复问题）：把当初的问题接回来
    remainder = _PRODUCT_ID_RE.sub("", _ORDER_CN_RE.sub("", _ORDER_ID_RE.sub("", text)))
    if (order_id or product_id) and len(re.sub(r"[\W_]+", "", remainder)) <= 3:
        pending = _pending_question(history)
        if pending:
            text = pending

    context_order_id, context_product_id = _find_context(history)
    handoff_reason = detect_handoff(text)
    intent = detect_intent(
        text,
        has_explicit_order=order_id is not None,
        has_context_order=context_order_id is not None,
    )
    # 发来商品卡片且没有订单类诉求时，按商品咨询处理
    if product_id and order_id is None and intent in (Intent.UNKNOWN, Intent.ORDER_QUERY, Intent.POLICY):
        if intent != Intent.POLICY or not _POLICY_RE.search(text):
            intent = Intent.PRODUCT

    return TurnAnalysis(
        text=text,
        intent=intent,
        order_id=order_id,
        product_id=product_id,
        context_order_id=context_order_id,
        context_product_id=context_product_id,
        refers_to_context=bool(_ANAPHORA_RE.search(text)),
        logistics_focus=_logistics_focus(text) if intent == Intent.LOGISTICS else None,
        handoff_reason=handoff_reason,
    )
