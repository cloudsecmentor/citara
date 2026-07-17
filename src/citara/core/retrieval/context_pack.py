from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from citara.core.config import settings
from citara.core.language.detect import detect_language_code
from citara.core.models import Source
from citara.core.retrieval.hybrid import hybrid_search
from citara.core.retrieval.keyword import SearchResult, search_knowledge
from citara.core.retrieval.vector import vector_search


def _dominant_source_language(session: Session, *, tenant_id: str) -> str | None:
    # Pick the most common non-NULL Source.language.
    stmt = (
        select(Source.language)
        .where(Source.tenant_id == tenant_id, Source.language.is_not(None))
        .group_by(Source.language)
        .order_by(func.count().desc())
        .limit(1)
    )
    row = session.execute(stmt).first()
    if not row:
        return None
    return row[0]


def _resolve_source_language(
    *,
    session: Session,
    tenant_id: str,
    query: str,
    language_policy: str,
    language: str | None,
) -> tuple[str | None, bool]:
    """Return (source_language_filter, include_und).

    - source_language_filter is applied as `Source.language == X`.
    - include_und means include NULL-labeled sources alongside X.

    """

    policy = (language_policy or "auto").lower()

    if policy == "any":
        return None, False

    if policy not in {"auto", "strict"}:
        raise ValueError("Unsupported language_policy. Use 'auto', 'strict', or 'any'.")

    if policy == "strict":
        include_und = False
    else:
        include_und = True

    detected, confidence = detect_language_code(query)

    # Candidate: explicit param wins.
    candidate = language or (detected if confidence >= 0.4 else None)
    dominant = _dominant_source_language(session, tenant_id=tenant_id)

    # If nothing was detected and we have no dominant language, don't filter.
    if candidate is None:
        return dominant, include_und

    # If detected language doesn't exist in the corpus, fall back to dominant.
    exists_stmt = (
        select(func.count())
        .select_from(Source)
        .where(
            Source.tenant_id == tenant_id,
            Source.language == candidate,
        )
    )
    count = session.execute(exists_stmt).scalar_one()
    if count == 0:
        return dominant, include_und

    return candidate, include_und


def search_by_mode(
    session: Session,
    *,
    query: str,
    limit: int = 10,
    mode: str = "hybrid",
    entity_slugs: list[str] | None = None,
    source_tree_slug: str | None = None,
    language_policy: str = "auto",
    language: str | None = None,
) -> list[SearchResult]:
    tenant_id = settings.default_tenant_id
    source_language, include_und = _resolve_source_language(
        session=session,
        tenant_id=tenant_id,
        query=query,
        language_policy=language_policy,
        language=language,
    )

    if mode == "keyword":
        return search_knowledge(
            session,
            query=query,
            limit=limit,
            tenant_id=tenant_id,
            entity_slugs=entity_slugs,
            source_tree_slug=source_tree_slug,
            source_language=source_language,
            include_und=include_und,
        )
    if mode == "vector":
        return vector_search(
            session,
            query=query,
            limit=limit,
            tenant_id=tenant_id,
            entity_slugs=entity_slugs,
            source_tree_slug=source_tree_slug,
            source_language=source_language,
            include_und=include_und,
        )
    if mode == "hybrid":
        return hybrid_search(
            session,
            query=query,
            limit=limit,
            tenant_id=tenant_id,
            entity_slugs=entity_slugs,
            source_tree_slug=source_tree_slug,
            source_language=source_language,
            include_und=include_und,
        )

    raise ValueError(f"Unsupported search mode: {mode}")


def retrieve_context_pack(
    session: Session,
    *,
    query: str,
    limit: int = 8,
    mode: str = "hybrid",
    entity_slugs: list[str] | None = None,
    source_tree_slug: str | None = None,
    language_policy: str = "auto",
    language: str | None = None,
) -> dict:
    results = search_by_mode(
        session,
        query=query,
        limit=limit,
        mode=mode,
        entity_slugs=entity_slugs,
        source_tree_slug=source_tree_slug,
        language_policy=language_policy,
        language=language,
    )
    return {
        "query": query,
        "mode": mode,
        "chunks": [
            {
                "chunk_id": result.chunk_id,
                "text": result.text,
                "score": result.score,
                "citation": {
                    "label": result.citation_label,
                    "source_url": result.canonical_url,
                    "timestamp_url": result.timestamp_url,
                    "page_number": result.page_number,
                    "start_ms": result.start_ms,
                    "end_ms": result.end_ms,
                },
            }
            for result in results
        ],
    }
