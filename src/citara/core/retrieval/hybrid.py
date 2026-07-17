from __future__ import annotations

from sqlalchemy.orm import Session

from citara.core.config import settings
from citara.core.retrieval.keyword import SearchResult, search_knowledge
from citara.core.retrieval.vector import vector_search


def hybrid_search(
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
    merged: dict[str, SearchResult] = {}
    scores: dict[str, float] = {}

    for result in search_knowledge(
        session,
        query=query,
        limit=limit * 2,
        tenant_id=tenant_id,
        entity_slugs=entity_slugs,
        source_tree_slug=source_tree_slug,
        source_language=source_language,
        include_und=include_und,
    ):
        merged[result.chunk_id] = result
        scores[result.chunk_id] = scores.get(result.chunk_id, 0.0) + result.score

    for result in vector_search(
        session,
        query=query,
        limit=limit * 2,
        tenant_id=tenant_id,
        entity_slugs=entity_slugs,
        source_tree_slug=source_tree_slug,
        source_language=source_language,
        include_und=include_und,
    ):
        merged.setdefault(result.chunk_id, result)
        scores[result.chunk_id] = scores.get(result.chunk_id, 0.0) + result.score

    ranked = sorted(merged.values(), key=lambda result: (-scores[result.chunk_id], result.source_title, result.chunk_id))
    return [
        SearchResult(
            chunk_id=result.chunk_id,
            source_id=result.source_id,
            source_title=result.source_title,
            source_type=result.source_type,
            text=result.text,
            score=float(scores[result.chunk_id]),
            citation_label=result.citation_label,
            canonical_url=result.canonical_url,
            timestamp_url=result.timestamp_url,
            page_number=result.page_number,
            start_ms=result.start_ms,
            end_ms=result.end_ms,
        )
        for result in ranked[:limit]
    ]
