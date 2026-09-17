"""知识检索领域服务（设计稿 §10.1）。

流程：先做身份与适用范围过滤，再进行关键词召回，去重融合，选择少量证据返回。
检索相似度**不是**事实置信度，LLM 自报置信度也不能成为单一拒答阈值；阈值必须
基于标注集、证据覆盖和冲突类型校准（本模块的 ``MIN_SCORE`` 只是可调起点）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import validation_error
from ..db import models as m
from ..knowledge.index import ScoredChunk, bm25_scores
from .context import TrustedContext

#: 最终返回的证据条数上限（设计稿建议 3–5 条）。
DEFAULT_TOP_K = 4
MAX_TOP_K = 8

#: 可调起点，不是校准后的阈值。低于它的结果被丢弃并明确报告"证据不足"。
MIN_SCORE = 0.6


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    evidence_id: str
    document_id: str
    chunk_id: str
    excerpt: str
    title: str
    source: str
    document_version: int
    scope: dict[str, Any]
    valid_from: str
    valid_to: str | None
    score: float
    matched_tokens: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "excerpt": self.excerpt,
            "title": self.title,
            "source": self.source,
            "document_version": self.document_version,
            "scope": self.scope,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "score": round(self.score, 4),
            "matched_tokens": list(self.matched_tokens),
        }


@dataclass(frozen=True, slots=True)
class KnowledgeResult:
    hits: list[KnowledgeHit]
    searched_chunks: int
    note: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "hits": [hit.to_dict() for hit in self.hits],
            "searched_chunks": self.searched_chunks,
            "note": self.note,
        }


async def search_knowledge(
    session: AsyncSession,
    ctx: TrustedContext,
    *,
    query: str,
    store_id: UUID | None,
    as_of: datetime,
    top_k: int = DEFAULT_TOP_K,
) -> KnowledgeResult:
    """在授权范围内检索知识。

    过滤条件全部在 SQL 侧完成：租户、门店适用范围、文档状态、生效区间。
    检索排除不适用或过期记录。
    """

    ctx.require("knowledge:read")
    text = (query or "").strip()
    if not text:
        raise validation_error("检索问题不能为空")
    if top_k <= 0:
        raise validation_error("top_k 必须为正")
    top_k = min(top_k, MAX_TOP_K)
    if store_id is not None:
        ctx.require_store_scope(store_id)

    statement = (
        select(m.KnowledgeChunk, m.KnowledgeDocument)
        .join(
            m.KnowledgeDocument,
            (m.KnowledgeChunk.tenant_id == m.KnowledgeDocument.tenant_id)
            & (m.KnowledgeChunk.document_id == m.KnowledgeDocument.id),
        )
        .where(
            m.KnowledgeChunk.tenant_id == ctx.tenant_id,
            m.KnowledgeDocument.status == "PUBLISHED",
            m.KnowledgeDocument.valid_from <= as_of,
        )
    )
    if store_id is not None:
        # 门店级文档 + 租户级通用文档（store_id 为空）。
        statement = statement.where(
            (m.KnowledgeDocument.store_id == store_id)
            | (m.KnowledgeDocument.store_id.is_(None))
        )
    else:
        statement = statement.where(m.KnowledgeDocument.store_id.is_(None))

    rows = (await session.execute(statement)).all()
    usable: list[tuple[m.KnowledgeChunk, m.KnowledgeDocument]] = [
        (chunk, document)
        for chunk, document in rows
        if document.valid_to is None or document.valid_to > as_of
    ]
    if not usable:
        return KnowledgeResult(
            hits=[], searched_chunks=0, note="授权范围内没有已发布且生效的知识文档"
        )

    documents = [
        (str(chunk.id), str(chunk.document_id), chunk.document_version, chunk.keyword_tokens or [])
        for chunk, _ in usable
    ]
    scored: list[ScoredChunk] = bm25_scores(query=text, documents=documents)
    if not scored:
        return KnowledgeResult(
            hits=[], searched_chunks=len(usable), note="未检索到与该问题相关的政策片段"
        )

    by_chunk = {str(chunk.id): (chunk, document) for chunk, document in usable}
    hits: list[KnowledgeHit] = []
    for item in scored:
        if item.score < MIN_SCORE:
            break
        chunk, document = by_chunk[item.chunk_id]
        hits.append(
            KnowledgeHit(
                evidence_id=f"{document.doc_key}#{chunk.ordinal}@v{document.version}",
                document_id=str(document.id),
                chunk_id=str(chunk.id),
                excerpt=chunk.content,
                title=document.title,
                source=document.source,
                document_version=document.version,
                scope={
                    "tenant_id": str(document.tenant_id),
                    "store_id": None if document.store_id is None else str(document.store_id),
                    "service_id": None if document.service_id is None else str(document.service_id),
                },
                valid_from=document.valid_from.isoformat(),
                valid_to=None if document.valid_to is None else document.valid_to.isoformat(),
                score=item.score,
                matched_tokens=item.matched_tokens,
            )
        )
        if len(hits) >= top_k:
            break

    if not hits:
        return KnowledgeResult(
            hits=[],
            searched_chunks=len(usable),
            note="检索到了候选片段，但相关度低于阈值，证据不足以给出确定答案",
        )
    return KnowledgeResult(hits=hits, searched_chunks=len(usable))
