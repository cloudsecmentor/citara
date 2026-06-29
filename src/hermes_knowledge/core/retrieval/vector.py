from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes_knowledge.core.config import settings
from hermes_knowledge.core.embeddings.service import embed_query
from hermes_knowledge.core.models import Chunk, Embedding, Source
from hermes_knowledge.core.retrieval.keyword import SearchResult, _citation_label


def cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def vector_search(
    session: Session,
    *,
    query: str,
    limit: int = 10,
    tenant_id: str = settings.default_tenant_id,
) -> list[SearchResult]:
    query_vector = embed_query(query)
    rows = session.execute(
        select(Embedding, Chunk, Source)
        .join(Chunk, Embedding.chunk_id == Chunk.id)
        .join(Source, Chunk.source_id == Source.id)
        .where(Embedding.tenant_id == tenant_id, Chunk.tenant_id == tenant_id, Source.tenant_id == tenant_id)
    ).all()

    scored: list[tuple[float, Source, Chunk]] = []
    for embedding, chunk, source in rows:
        score = cosine_similarity(query_vector, list(embedding.vector))
        if score > 0:
            scored.append((score, source, chunk))

    scored.sort(key=lambda item: (-item[0], item[1].title, item[2].chunk_index))
    return [
        SearchResult(
            chunk_id=chunk.id,
            source_id=source.id,
            source_title=source.title,
            source_type=source.source_type,
            text=chunk.text,
            score=float(score),
            citation_label=_citation_label(source, chunk),
            canonical_url=source.canonical_url,
            start_ms=chunk.start_ms,
            end_ms=chunk.end_ms,
        )
        for score, source, chunk in scored[:limit]
    ]
