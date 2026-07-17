from __future__ import annotations

from sqlalchemy.orm import Session

from citara.core.config import settings
from citara.core.retrieval.keyword import SearchResult, search_knowledge
from citara.core.retrieval.vector import vector_search

# Standard reciprocal-rank-fusion damping constant (Cormack et al.); keeps
# a single top rank from dominating the fused score.
RRF_K = 60


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
    keyword_results = search_knowledge(
        session,
        query=query,
        limit=limit * 2,
        tenant_id=tenant_id,
        entity_slugs=entity_slugs,
        source_tree_slug=source_tree_slug,
        source_language=source_language,
        include_und=include_und,
    )
    vector_results = vector_search(
        session,
        query=query,
        limit=limit * 2,
        tenant_id=tenant_id,
        entity_slugs=entity_slugs,
        source_tree_slug=source_tree_slug,
        source_language=source_language,
        include_und=include_und,
    )

    # Keyword and vector scores live on incomparable scales (term counts vs
    # cosine similarity), so fuse by rank instead of adding raw scores.
    # Source retrieval weights already shaped each backend's ranking.
    merged: dict[str, SearchResult] = {}
    scores: dict[str, float] = {}
    for results in (keyword_results, vector_results):
        for rank, result in enumerate(results, start=1):
            merged.setdefault(result.chunk_id, result)
            scores[result.chunk_id] = scores.get(result.chunk_id, 0.0) + 1.0 / (RRF_K + rank)

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
