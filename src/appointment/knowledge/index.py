"""切块与关键词检索。

设计稿 §10.1 的建议起点（可调，不是已验证最优值）：

- 每块 300–600 token，按完整政策或段落边界切分；
- 每路召回约 20 条，融合后重排，最终 3–5 条；
- 价格表不得切成失去表头或适用条件的碎片。

P0 只做关键词路召回（应用层中文分词 + BM25 近似评分）；向量路留待启用 pgvector
后接入，接口已按"多路召回 + 融合"设计。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from .tokenizer import tokenize

#: 单块字数上限（中文按字近似 token）。
CHUNK_CHAR_LIMIT = 420
CHUNK_MIN_CHARS = 20


def build_chunks(text: str, *, limit: int = CHUNK_CHAR_LIMIT) -> list[str]:
    """按段落边界切块，尽量保留完整语义单元。

    段落过长时按句号/分号二次切分；仍过长才硬切，并对硬切结果保留重叠，
    避免表头与适用条件被拆散。
    """

    normalized = "\n".join(line.strip() for line in (text or "").splitlines())
    paragraphs = [p.strip() for p in normalized.split("\n") if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        if len(paragraph) > limit:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.extend(_split_long_paragraph(paragraph, limit=limit))
            continue
        if not buffer:
            buffer = paragraph
        elif len(buffer) + 1 + len(paragraph) <= limit:
            buffer = f"{buffer}\n{paragraph}"
        else:
            chunks.append(buffer)
            buffer = paragraph
    if buffer:
        chunks.append(buffer)
    return [c for c in chunks if len(c) >= CHUNK_MIN_CHARS] or ([text] if text else [])


def _split_long_paragraph(paragraph: str, *, limit: int) -> list[str]:
    sentences = [s for s in _split_sentences(paragraph) if s]
    chunks: list[str] = []
    buffer = ""
    for sentence in sentences:
        if len(sentence) > limit:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            step = max(limit - 40, CHUNK_MIN_CHARS)
            start = 0
            while start < len(sentence):
                chunks.append(sentence[start : start + limit])
                start += step
            continue
        if not buffer:
            buffer = sentence
        elif len(buffer) + len(sentence) <= limit:
            buffer = f"{buffer}{sentence}"
        else:
            chunks.append(buffer)
            buffer = sentence
    if buffer:
        chunks.append(buffer)
    return chunks


def _split_sentences(text: str) -> list[str]:
    parts: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in "。！？；":
            parts.append(buffer)
            buffer = ""
    if buffer:
        parts.append(buffer)
    return parts


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    chunk_id: str
    document_id: str
    document_version: int
    excerpt: str
    score: float
    matched_tokens: tuple[str, ...]
    document_frequency: int


def bm25_scores(
    *,
    query: str,
    documents: Sequence[tuple[str, str, int, Iterable[str]]],
    k1: float = 1.4,
    b: float = 0.72,
) -> list[ScoredChunk]:
    """对候选片段做 BM25 近似打分。

    ``documents`` 为 (chunk_id, document_id, document_version, tokens) 序列。
    只对授权范围内的片段调用，因此这里不做权限判断。
    """

    query_tokens = tokenize(query)
    if not query_tokens or not documents:
        return []

    doc_tokens: list[list[str]] = [list(tokens) for _, _, _, tokens in documents]
    lengths = [len(tokens) for tokens in doc_tokens]
    avg_length = sum(lengths) / len(lengths) if lengths else 1.0

    # 文档频率：只统计候选集合，语料规模变化不影响相对排序。
    df: dict[str, int] = {}
    for tokens in doc_tokens:
        for token in set(tokens):
            df[token] = df.get(token, 0) + 1
    total_docs = len(doc_tokens)

    results: list[ScoredChunk] = []
    for (chunk_id, document_id, document_version, _), tokens, length in zip(
        documents, doc_tokens, lengths
    ):
        token_counts: dict[str, int] = {}
        for token in tokens:
            token_counts[token] = token_counts.get(token, 0) + 1

        score = 0.0
        matched: list[str] = []
        for token in set(query_tokens):
            frequency = token_counts.get(token, 0)
            if not frequency:
                continue
            matched.append(token)
            idf = math.log(1 + (total_docs - df.get(token, 0) + 0.5) / (df.get(token, 0) + 0.5))
            denominator = frequency + k1 * (1 - b + b * (length / avg_length if avg_length else 1))
            score += idf * (frequency * (k1 + 1)) / denominator
        if score <= 0:
            continue
        results.append(
            ScoredChunk(
                chunk_id=chunk_id,
                document_id=document_id,
                document_version=document_version,
                excerpt="",
                score=score,
                matched_tokens=tuple(sorted(matched)),
                document_frequency=len(matched),
            )
        )

    results.sort(key=lambda item: item.score, reverse=True)
    return results
