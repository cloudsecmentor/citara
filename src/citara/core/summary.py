from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from citara.core.config import settings
from citara.core.models import Chunk, Source
from citara.core.retrieval.keyword import _source_weight, _timestamp_url

TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(value: str) -> set[str]:
    return set(TOKEN_RE.findall(value.lower()))


def _episode_number(value: str) -> str | None:
    match = re.search(r"\b(?:bema\s*)?(-?\d+[a-z]?)\b", value.lower())
    return match.group(1) if match else None


def _preference_rank(source: Source, preference: str = "current") -> int:
    label = (source.metadata_json or {}).get("preference_label")
    if label == preference:
        return 0
    if label == "current":
        return 1
    if label == "legacy":
        return 3
    return 2


def _match_score(source: Source, query: str) -> float:
    title = source.title or ""
    query_tokens = _tokens(query)
    title_tokens = _tokens(title)
    score = float(len(query_tokens & title_tokens))
    episode = _episode_number(query)
    if episode:
        # Match BEMA episode numbers in titles such as "BEMA 10: ...".
        title_episode = _episode_number(title)
        if title_episode == episode:
            score += 10.0
    if query.lower() in title.lower():
        score += 5.0
    return score


def resolve_source_for_summary(
    session: Session,
    *,
    query: str,
    preference: str = "current",
    tenant_id: str = settings.default_tenant_id,
) -> Source | None:
    sources = list(session.execute(select(Source).where(Source.tenant_id == tenant_id).order_by(Source.created_at.desc())).scalars())
    candidates = [(source, _match_score(source, query)) for source in sources]
    candidates = [(source, score) for source, score in candidates if score > 0]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            -item[1],
            _preference_rank(item[0], preference),
            -_source_weight(item[0]),
            item[0].title,
        )
    )
    return candidates[0][0]


def _serialize_source(source: Source) -> dict:
    metadata = source.metadata_json or {}
    return {
        "source_id": source.id,
        "title": source.title,
        "source_type": source.source_type,
        "canonical_url": source.canonical_url,
        "preference_label": metadata.get("preference_label"),
        "retrieval_weight": metadata.get("retrieval_weight", 1.0),
        "metadata": metadata,
    }


def get_source_summary_context(
    session: Session,
    source_id: str,
    *,
    tenant_id: str = settings.default_tenant_id,
    max_chunks: int | None = None,
) -> dict | None:
    source = session.get(Source, source_id)
    if source is None or source.tenant_id != tenant_id:
        return None
    query = select(Chunk).where(Chunk.tenant_id == tenant_id, Chunk.source_id == source_id).order_by(Chunk.chunk_index.asc())
    if max_chunks is not None:
        query = query.limit(max_chunks)
    chunks = list(session.execute(query).scalars())
    return {
        **_serialize_source(source),
        "summary_instructions": {
            "strategy": "ordered_source_transcript",
            "guidance": "Summarize from chunks in chunk_index order; use timestamp_url citations for key claims; prefer this source over search-only snippets for whole-episode summaries.",
        },
        "chunks": [
            {
                "chunk_id": chunk.id,
                "chunk_index": chunk.chunk_index,
                "transcript_segment_id": chunk.transcript_segment_id,
                "text": chunk.text,
                "start_ms": chunk.start_ms,
                "end_ms": chunk.end_ms,
                "timestamp_url": _timestamp_url(source, chunk),
            }
            for chunk in chunks
        ],
    }


def resolve_summary_context(
    session: Session,
    *,
    query: str,
    preference: str = "current",
    tenant_id: str = settings.default_tenant_id,
    max_chunks: int | None = None,
) -> dict | None:
    source = resolve_source_for_summary(session, query=query, preference=preference, tenant_id=tenant_id)
    if source is None:
        return None
    return get_source_summary_context(session, source.id, tenant_id=tenant_id, max_chunks=max_chunks)
