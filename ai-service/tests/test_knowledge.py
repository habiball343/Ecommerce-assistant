"""知识库切片与检索的单元测试（不访问数据库）。"""

from ai_service.services.knowledge import BUILTIN_FILE, bm25_scores, split_into_chunks, tokenize

RULES = BUILTIN_FILE.read_text(encoding="utf-8")


def search(query: str) -> str:
    chunks = split_into_chunks(RULES, 420)
    documents = [tokenize(f"{title} {title} {content}") for title, content in chunks]
    scores = bm25_scores(query, documents)
    return chunks[max(range(len(chunks)), key=scores.__getitem__)][0]


def test_builtin_rules_are_split_by_section() -> None:
    chunks = split_into_chunks(RULES, 420)
    titles = [title for title, _ in chunks]
    assert "七天无理由退货" in titles
    assert "取消订单规则" in titles
    assert all(len(content) <= 420 for _, content in chunks)
    assert not any("演示规则" in content for _, content in chunks)  # 引用块里的说明文字不入库


def test_broken_pdf_lines_are_merged() -> None:
    text = "一、退货规则\n自签收之日起 7 天内，商品完好的，可以申请\n七天无理由退货。\n\n二、运费规则\n满 99 元包邮。"
    chunks = split_into_chunks(text, 420)
    assert chunks[0] == ("一、退货规则", "自签收之日起 7 天内，商品完好的，可以申请七天无理由退货。")
    assert chunks[1][0] == "二、运费规则"


def test_retrieval_finds_the_relevant_section() -> None:
    assert search("我想了解平台售后规则") == "售后规则概览"
    assert search("退款多久到账") == "退款时效与到账方式"
    assert search("发票怎么开") == "发票规则"
    assert search("发货后还能改地址吗") == "修改收货地址规则"
    assert search("付款后多久发货") == "发货与配送时效"
    assert search("人工客服几点上班") == "人工客服服务"
