from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from citara.core.chunking.simple import tokenize
from citara.core.config import settings
from citara.core.entities import resolve_entity_ids
from citara.core.models import Chunk, Source, SourceEntity


@dataclass(frozen=True)
class SearchResult:
    chunk_id: str
    source_id: str
    source_title: str
    source_type: str
    text: str
    score: float
    citation_label: str
    canonical_url: str | None = None
    timestamp_url: str | None = None
    page_number: int | None = None
    start_ms: int | None = None
    end_ms: int | None = None


def _citation_label(source: Source, chunk: Chunk) -> str:
    if source.source_type == "podcast_episode":
        show_title = source.metadata_json.get("show_title") or source.title
        timestamp = _format_timestamp(chunk.start_ms or 0)
        return f"{show_title}, {source.title}, {timestamp}"
    return f"{source.title}, chunk {chunk.chunk_index}"


def _timestamp_url(source: Source, chunk: Chunk) -> str | None:
    if source.source_type != "podcast_episode" or not source.canonical_url or chunk.start_ms is None:
        return None
    separator = "&" if "?" in source.canonical_url else "?"
    return f"{source.canonical_url}{separator}t={chunk.start_ms // 1000}"


def _source_weight(source: Source) -> float:
    value = (source.metadata_json or {}).get("retrieval_weight", 1.0)
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return 1.0
    return weight if weight > 0 else 1.0


def _format_timestamp(ms: int) -> str:
    total = ms // 1000
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def search_knowledge(
    session: Session,
    *,
    query: str,
    limit: int = 10,
    tenant_id: str = settings.default_tenant_id,
    entity_slugs: list[str] | None = None,
) -> list[SearchResult]:
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    statement = (
        select(Chunk, Source)
        .join(Source, Chunk.source_id == Source.id)
        .where(Chunk.tenant_id == tenant_id, Source.tenant_id == tenant_id)
    )
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
    for chunk, source in rows:
        chunk_tokens = tokenize(chunk.text)
        score = sum(chunk_tokens.count(token) for token in query_tokens)
        if score:
            scored.append((score * _source_weight(source), source, chunk))

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
