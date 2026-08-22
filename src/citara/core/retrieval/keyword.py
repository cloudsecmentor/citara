from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from citara.core.chunking.simple import tokenize
from citara.core.config import settings
from citara.core.models import Chunk, Source
from citara.core.retrieval import bm25, fts
from citara.core.retrieval.base import (
    TIMESTAMPED_SOURCE_TYPES,
    SearchResult,
    _citation_label,
    _format_timestamp,
    _source_weight,
    _timestamp_url,
    apply_source_filters,
    result_from,
)

# Re-exported for backward compatibility: `core/summary.py` and several tests
# import these from this module, which was their home before the retrieval
# backends were split apart.
__all__ = [
    "TIMESTAMPED_SOURCE_TYPES",
    "SearchResult",
    "_citation_label",
    "_format_timestamp",
    "_source_weight",
    "_timestamp_url",
    "scan_search",
    "search_knowledge",
]


def scan_search(
    session: Session,
    *,
    query_tokens: list[str],
    limit: int,
    tenant_id: str,
    entity_slugs: list[str] | None = None,
    source_tree_slug: str | None = None,
    source_language: str | None = None,
    include_und: bool = False,
) -> list[SearchResult]:
    """BM25 over a full scan. The portable fallback when FTS5 is unavailable.

    Used on Postgres and on any SQLite build without FTS5. It reads every
    matching chunk and tokenizes it per query, so it is O(corpus) -- the
    indexed path in `fts.py` is strongly preferred and is what SQLite uses.
    Ranking is BM25 either way, so the two paths stay comparable.
    """

    if not query_tokens:
        return []

    statement = select(Chunk, Source).join(Source, Chunk.source_id == Source.id)
    statement, matchable = apply_source_filters(
        statement,
        session=session,
        tenant_id=tenant_id,
        entity_slugs=entity_slugs,
        source_tree_slug=source_tree_slug,
        source_language=source_language,
        include_und=include_und,
    )
    if not matchable:
        return []

    rows = session.execute(statement).all()
    tokenized = [tokenize(chunk.text) for chunk, _ in rows]
    doc_freqs, total_docs, avg_len = bm25.corpus_stats(tokenized, query_tokens)

    scored: list[tuple[float, Source, Chunk]] = []
    for (chunk, source), doc_tokens in zip(rows, tokenized, strict=True):
        score = bm25.score_document(
            query_tokens,
            doc_tokens,
            doc_freqs=doc_freqs,
            total_docs=total_docs,
            avg_doc_len=avg_len,
        )
        if score > 0:
            scored.append((score * _source_weight(source), source, chunk))

    scored.sort(key=lambda item: (-item[0], item[1].title, item[2].chunk_index))
    return [result_from(source, chunk, score) for score, source, chunk in scored[:limit]]


def search_knowledge(
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
    """Rank chunks by BM25, using the FTS5 index when one is available."""

    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    backend = fts.fts_search if fts.fts_available(session) else scan_search
    return backend(
        session,
        query_tokens=query_tokens,
        limit=limit,
        tenant_id=tenant_id,
        entity_slugs=entity_slugs,
        source_tree_slug=source_tree_slug,
        source_language=source_language,
        include_und=include_und,
    )
