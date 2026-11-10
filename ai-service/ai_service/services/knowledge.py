"""知识库：文档解析、切片、入库与检索。

检索默认使用关键词相关度（BM25），不依赖任何外部服务；
如果在 .env 中配置了 OpenAI 兼容的 Embedding 接口，则在入库时写入向量，检索时做“关键词 + 向量”的混合排序。
"""

import asyncio
import logging
import math
import re
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import delete, func, select

from ai_service.db import session_factory
from ai_service.models import KnowledgeChunk
from ai_service.settings import get_settings
from ai_service.utils import utcnow

logger = logging.getLogger(__name__)

BUILTIN_FILE = Path(__file__).resolve().parents[1] / "knowledge" / "platform_rules.md"
BUILTIN_FILE_NAME = "平台服务规则（内置）.md"


class KnowledgeError(ValueError):
    """上传的文档无法处理（格式不对、内容为空等），错误信息可以直接展示给管理员。"""


# --------------------------------------------------------------------------- 文本解析与切片

_HEADING_PATTERNS = (
    re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*#*$"),
    re.compile(r"^(?P<title>第[一二三四五六七八九十百零〇\d]+[章节条款部分].{0,30})$"),
    re.compile(r"^(?P<title>[一二三四五六七八九十]+[、.．]\s*\S.{0,30})$"),
)
_SENTENCE_END = "。！？；!?;：:"
_BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\d{1,2}[.、)）]|[（(]\d{1,2}[)）])\s*")


def _heading_title(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 40:
        return None
    for pattern in _HEADING_PATTERNS:
        match = pattern.match(stripped)
        if match:
            return match.group("title").strip()
    return None


def extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 - pypdf 对损坏文件会抛出多种异常
        raise KnowledgeError("无法解析该 PDF 文件，请确认文件没有损坏或加密") from exc
    return "\n".join(pages)


def _merge_broken_lines(text: str) -> list[str]:
    """PDF 抽取的文本常在句子中间换行，这里把属于同一段的行重新接起来。"""
    merged: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            merged.append("")
            continue
        previous = merged[-1] if merged else ""
        continues_previous = (
            previous
            and previous[-1] not in _SENTENCE_END
            and _heading_title(previous) is None
            and _heading_title(line) is None
            and not _BULLET_RE.match(line)
            and not line.startswith(">")
        )
        if continues_previous:
            joiner = " " if previous[-1].isascii() and line[0].isascii() else ""
            merged[-1] = previous + joiner + line
        else:
            merged.append(line)
    return merged


def _split_long_paragraph(paragraph: str, limit: int) -> list[str]:
    if len(paragraph) <= limit:
        return [paragraph]
    pieces: list[str] = []
    current = ""
    for sentence in re.findall(r"[^。！？!?；;]+[。！？!?；;]?", paragraph):
        if current and len(current) + len(sentence) > limit:
            pieces.append(current)
            current = ""
        while len(sentence) > limit:
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        current += sentence
    if current:
        pieces.append(current)
    return pieces


def split_into_chunks(text: str, chunk_size: int) -> list[tuple[str, str]]:
    """按“标题 → 段落”切分文档，返回 [(章节标题, 切片内容)]。同一章节的段落尽量放在一个切片里。"""
    sections: list[tuple[str, list[str]]] = []
    document_title = ""
    current_title = ""
    current_lines: list[str] = []

    def flush() -> None:
        paragraphs = [line for line in current_lines if line and not line.startswith(">")]
        if paragraphs:
            sections.append((current_title, paragraphs))

    for line in _merge_broken_lines(text):
        title = _heading_title(line)
        if title is None:
            current_lines.append(line)
            continue
        flush()
        current_lines = []
        if line.strip().startswith("# ") and not document_title:
            # 一级标题视为文档标题，不单独成段
            document_title = title
            current_title = ""
        else:
            current_title = title
    flush()

    # 第一个标题之前只有一两行（通常是文档名、副标题）时，不单独成为切片
    if len(sections) > 1 and not sections[0][0] and sum(len(line) for line in sections[0][1]) < 60:
        sections = sections[1:]

    chunks: list[tuple[str, str]] = []
    for title, paragraphs in sections:
        buffer: list[str] = []
        size = 0
        for paragraph in paragraphs:
            for piece in _split_long_paragraph(paragraph, chunk_size):
                if buffer and size + len(piece) > chunk_size:
                    chunks.append((title, "\n".join(buffer)))
                    buffer, size = [], 0
                buffer.append(piece)
                size += len(piece)
        if buffer:
            chunks.append((title, "\n".join(buffer)))
    return chunks


# --------------------------------------------------------------------------- 关键词检索（BM25）

_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]+")
# 提问里常见、但对区分规则没有帮助的二字组合
_STOP_TOKENS = frozenset(
    "我想 想了 了解 一下 请问 什么 怎么 怎样 如何 可以 是否 能不 不能 有没 没有 这个 那个 你们 我们 平台 的话 "
    "一个 时候 知道 告诉 帮我 想要 需要 咨询 问一 下平 想问 哪些 多少 是不 不是 还是 或者 以及 关于 相关".split()
)


def tokenize(text: str) -> list[str]:
    """中文按相邻两个字切分，英文数字按单词切分。不依赖分词库，对规则类短文足够好用。"""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        segment = match.group()
        if segment[0].isascii():
            tokens.append(segment)
        elif len(segment) == 1:
            tokens.append(segment)
        else:
            tokens.extend(segment[index : index + 2] for index in range(len(segment) - 1))
    return [token for token in tokens if token not in _STOP_TOKENS]


def bm25_scores(query: str, documents: list[list[str]], k1: float = 1.4, b: float = 0.75) -> list[float]:
    query_tokens = set(tokenize(query))
    if not query_tokens or not documents:
        return [0.0] * len(documents)
    total = len(documents)
    average_length = sum(len(tokens) for tokens in documents) / total or 1.0
    document_frequency: Counter[str] = Counter()
    for tokens in documents:
        document_frequency.update(set(tokens) & query_tokens)

    scores: list[float] = []
    for tokens in documents:
        frequencies = Counter(token for token in tokens if token in query_tokens)
        score = 0.0
        for token, frequency in frequencies.items():
            idf = math.log(1 + (total - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5))
            score += idf * frequency * (k1 + 1) / (frequency + k1 * (1 - b + b * len(tokens) / average_length))
        scores.append(score)
    return scores


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


# --------------------------------------------------------------------------- 知识库服务


class KnowledgeService:
    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self._settings = get_settings()
        self._http = http_client

    async def count(self) -> int:
        async with session_factory() as session:
            return await session.scalar(select(func.count(KnowledgeChunk.id))) or 0

    async def seed_builtin(self) -> int:
        """知识库为空时写入内置的平台规则，保证开箱即可回答规则类问题。"""
        if await self.count() > 0:
            return 0
        text = BUILTIN_FILE.read_text(encoding="utf-8")
        return await self.ingest_text(BUILTIN_FILE_NAME, text)

    async def ingest_upload(self, file_name: str, data: bytes) -> int:
        limit = self._settings.knowledge_max_upload_mb * 1024 * 1024
        if not data:
            raise KnowledgeError("上传的文件是空的")
        if len(data) > limit:
            raise KnowledgeError(f"文件不能超过 {self._settings.knowledge_max_upload_mb} MB")

        if data[:5] == b"%PDF-":
            text = await asyncio.to_thread(extract_pdf_text, data)
            if len(re.sub(r"\s", "", text)) < 20:
                raise KnowledgeError("没有从 PDF 中提取到文字，可能是扫描件或图片 PDF，请上传可复制文字的 PDF")
        elif file_name.lower().endswith((".md", ".txt")):
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise KnowledgeError("文本文件请使用 UTF-8 编码") from exc
        else:
            raise KnowledgeError("仅支持 PDF 文档（也可以上传 UTF-8 编码的 .md / .txt 文本）")
        return await self.ingest_text(file_name, text)

    async def ingest_text(self, file_name: str, text: str) -> int:
        """切片并入库。同名文件重新上传时先删除旧切片，相当于更新。"""
        file_name = Path(file_name or "未命名文档").name[:200]
        chunks = split_into_chunks(text, self._settings.knowledge_chunk_size)
        if not chunks:
            raise KnowledgeError("文档中没有可用的文字内容")
        embeddings = await self._embed([f"{title}\n{content}" for title, content in chunks])

        created_at = utcnow()  # 同一份文档的切片使用同一个入库时间，列表按“文档 → 切片顺序”展示
        async with session_factory() as session:
            await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.file_name == file_name))
            for index, (title, content) in enumerate(chunks):
                session.add(
                    KnowledgeChunk(
                        file_name=file_name,
                        chunk_index=index,
                        title=title[:200],
                        content=content,
                        embedding=embeddings[index] if embeddings else None,
                        created_at=created_at,
                    )
                )
            await session.commit()
        logger.info("知识文档 %s 已入库，共 %s 个切片", file_name, len(chunks))
        return len(chunks)

    async def delete_file(self, file_name: str) -> int:
        async with session_factory() as session:
            result = await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.file_name == file_name))
            await session.commit()
            return result.rowcount or 0

    async def list_chunks(self) -> list[dict[str, Any]]:
        async with session_factory() as session:
            records = (
                await session.scalars(
                    select(KnowledgeChunk).order_by(
                        KnowledgeChunk.created_at.desc(), KnowledgeChunk.file_name, KnowledgeChunk.chunk_index
                    )
                )
            ).all()
        return [
            {
                "id": item.id,
                "file_name": item.file_name,
                "title": item.title,
                "content": f"【{item.title}】{item.content}" if item.title else item.content,
                "vectorized": item.embedding is not None,
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in records
        ]

    async def search(self, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
        """返回与问题最相关的切片；没有任何相关内容时返回空列表（由上层如实告知用户）。"""
        top_k = top_k or self._settings.knowledge_top_k
        async with session_factory() as session:
            records = (await session.scalars(select(KnowledgeChunk))).all()
        if not records or not query.strip():
            return []

        # 标题命中比正文命中更说明相关，这里让标题参与两次计分
        documents = [tokenize(f"{item.title} {item.title} {item.content}") for item in records]
        keyword_scores = bm25_scores(query, documents)
        best_keyword = max(keyword_scores) or 0.0
        combined = [score / best_keyword if best_keyword else 0.0 for score in keyword_scores]

        query_vectors = await self._embed([query]) if any(item.embedding for item in records) else None
        if query_vectors:
            for index, item in enumerate(records):
                similarity = max(_cosine(query_vectors[0], item.embedding or []), 0.0)
                combined[index] = 0.5 * combined[index] + 0.5 * similarity
        elif best_keyword <= 0:
            return []

        ranked = sorted(zip(records, combined, keyword_scores, strict=True), key=lambda row: row[1], reverse=True)
        results = []
        for item, score, keyword_score in ranked[:top_k]:
            # 关键词完全不沾边、向量相似度也很低的切片不返回，避免答非所问
            if score < 0.25 or (not query_vectors and keyword_score <= 0):
                continue
            results.append(
                {
                    "id": item.id,
                    "file_name": item.file_name,
                    "title": item.title,
                    "content": item.content,
                    "score": round(score, 3),
                }
            )
        return results

    async def _embed(self, texts: list[str]) -> list[list[float]] | None:
        """调用 OpenAI 兼容的 /embeddings 接口；未配置或调用失败时返回 None，退回关键词检索。"""
        if not self._settings.embedding_configured or not texts:
            return None
        url = self._settings.embedding_base_url.rstrip("/") + "/embeddings"
        headers = {"Authorization": f"Bearer {self._settings.embedding_api_key}"}
        vectors: list[list[float]] = []
        try:
            client = self._http or httpx.AsyncClient(timeout=30)
            try:
                for start in range(0, len(texts), 16):
                    response = await client.post(
                        url,
                        headers=headers,
                        json={"model": self._settings.embedding_model, "input": texts[start : start + 16]},
                        timeout=30,
                    )
                    response.raise_for_status()
                    data = sorted(response.json()["data"], key=lambda row: row.get("index", 0))
                    vectors.extend(row["embedding"] for row in data)
            finally:
                if self._http is None:
                    await client.aclose()
        except Exception:  # noqa: BLE001
            logger.warning("调用 Embedding 接口失败，本次退回关键词检索", exc_info=True)
            return None
        return vectors if len(vectors) == len(texts) else None
