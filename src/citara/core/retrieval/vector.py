from __future__ import annotations

import math

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from citara.core.config import settings
from citara.core.embeddings.service import embed_query
from citara.core.entities import resolve_entity_ids
from citara.core.models import Chunk, Embedding, Source, SourceEntity
from citara.core.retrieval.keyword import SearchResult, _citation_label, _source_weight, _timestamp_url


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
    entity_slugs: list[str] | None = None,
    source_tree_slug: str | None = None,
    source_language: str | None = None,
    include_und: bool = False,
) -> list[SearchResult]:
    query_vector = embed_query(query)
    statement = (
        select(Embedding, Chunk, Source)
        .join(Chunk, Embedding.chunk_id == Chunk.id)
        .join(Source, Chunk.source_id == Source.id)
        .where(
            Embedding.tenant_id == tenant_id,
            Chunk.tenant_id == tenant_id,
            Source.tenant_id == tenant_id,
        )
    )
    if source_language:
        if include_und:
            statement = statement.where(
                or_(Source.language == source_language, Source.language.is_(None))
            )
        else:
            statement = statement.where(Source.language == source_language)
    if source_tree_slug:
        statement = statement.where(Source.metadata_json["source_tree_slug"].as_string() == source_tree_slug)
    if entity_slugs:
        entity_ids = resolve_entity_ids(session, entity_slugs=entity_slugs, tenant_id=tenant_id)
        if not entity_ids:
            return []
        source_ids = select(SourceEntity.source_id).where(
            SourceEntity.tenant_id == tenant_id,
            SourceEntity.entity_id.in_(entity_ids),
        )
        statement = statement.where(Chunk.source_id.in_(source_ids))
    rows = session.execute(statement).all()

    scored: list[tuple[float, Source, Chunk]] = []
    for embedding, chunk, source in rows:
        score = cosine_similarity(query_vector, list(embedding.vector)) * _source_weight(source)
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
            timestamp_url=_timestamp_url(source, chunk),
            start_ms=chunk.start_ms,
            end_ms=chunk.end_ms,
        )
        for score, source, chunk in scored[:limit]
    ]
