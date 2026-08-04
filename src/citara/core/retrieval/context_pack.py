from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from citara.core.chunking.simple import tokenize
from citara.core.config import settings
from citara.core.language.detect import detect_language_code
from citara.core.language.translate import get_translation_provider, translate_text
from citara.core.models import Source
from citara.core.retrieval.hybrid import hybrid_search, rrf_fuse
from citara.core.retrieval.keyword import SearchResult, search_knowledge
from citara.core.retrieval.vector import vector_search

# Confidence threshold above which a detected query language is trusted
# enough to act on (as a source-language filter candidate, or to report as
# `query_language`/`response_language`).
_LANGUAGE_CONFIDENCE_THRESHOLD = 0.4

# Guards against the "filter trap": under `auto`, a *detected* (not
# explicitly requested) query language must never narrow the corpus to a
# near-empty slice while a much larger corpus in another language goes
# unsearched. Below this corpus size the guard doesn't trigger at all (a
# handful of sources is too small to call anything a "trap"); above it, the
# candidate language must cover at least this share of the corpus or
# resolution falls back to the corpus's dominant language instead.
_MIN_CORPUS_FOR_LANGUAGE_GUARD = 10
_MIN_LANGUAGE_SHARE = 0.1

_MODE_FUNCTIONS = {
    "keyword": search_knowledge,
    "vector": vector_search,
    "hybrid": hybrid_search,
}


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


def _corpus_languages(session: Session, *, tenant_id: str) -> list[str]:
    """Distinct source languages present in the corpus, most common first."""

    stmt = (
        select(Source.language)
        .where(Source.tenant_id == tenant_id, Source.language.is_not(None))
        .group_by(Source.language)
        .order_by(func.count().desc())
    )
    return [row[0] for row in session.execute(stmt).all()]


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
    candidate = language or (detected if confidence >= _LANGUAGE_CONFIDENCE_THRESHOLD else None)
    dominant = _dominant_source_language(session, tenant_id=tenant_id)

    # If nothing was detected and we have no dominant language, don't filter.
    if candidate is None:
        return dominant, include_und

    # If detected language doesn't exist in the corpus, fall back to dominant.
    total = session.execute(select(func.count()).select_from(Source).where(Source.tenant_id == tenant_id)).scalar_one()
    matching = session.execute(
        select(func.count())
        .select_from(Source)
        .where(
            Source.tenant_id == tenant_id,
            Source.language == candidate,
        )
    ).scalar_one()
    if matching == 0:
        return dominant, include_und

    # Filter-trap guard: only second-guess an *auto-detected* candidate (an
    # explicit `language=` param is a deliberate request and is never
    # overridden). If the candidate language is a thin sliver of a
    # meaningfully sized corpus, treat the detection as too weak to trust as
    # a hard filter and fall back to the corpus's dominant language instead
    # of silently hiding almost everything.
    if (
        language is None
        and policy == "auto"
        and total >= _MIN_CORPUS_FOR_LANGUAGE_GUARD
        and matching < max(1.0, total * _MIN_LANGUAGE_SHARE)
    ):
        return dominant, include_und

    return candidate, include_und


def query_language_notice(
    session: Session,
    *,
    query: str,
    tenant_id: str = settings.default_tenant_id,
) -> dict | None:
    """Diagnostic explaining a likely-empty-for-language-reasons result set.

    Returns a `{"code": "cross_language_query", "query_language": ..., "corpus_languages": [...]}`
    payload when either:
      - the query tokenizes to nothing (e.g. only punctuation/symbols), or
      - the query's detected language isn't present anywhere in the corpus.

    Returns None when nothing looks amiss, so callers can drop the field or
    leave it null rather than reporting an unexplained empty result set.
    """

    corpus_languages = _corpus_languages(session, tenant_id=tenant_id)
    detected, confidence = detect_language_code(query)

    if not tokenize(query):
        return {
            "code": "cross_language_query",
            "query_language": detected,
            "corpus_languages": corpus_languages,
        }

    if not corpus_languages:
        return None
    if detected is None or confidence < _LANGUAGE_CONFIDENCE_THRESHOLD:
        return None
    if detected in corpus_languages:
        return None

    return {
        "code": "cross_language_query",
        "query_language": detected,
        "corpus_languages": corpus_languages,
    }


def _server_side_translation(query: str) -> str | None:
    """Best-effort translation fallback, used only when a client supplies none.

    Defensive by design: translation is a convenience, not a hard
    dependency, so any provider failure (network, auth, bad config) falls
    back to searching only the original query rather than raising.
    """

    detected, confidence = detect_language_code(query)
    if not detected or detected == "en" or confidence < _LANGUAGE_CONFIDENCE_THRESHOLD:
        return None
    try:
        return translate_text(query, target_language="en", provider=get_translation_provider())
    except Exception:
        return None


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
    query_translated: str | None = None,
    query_language: str | None = None,
) -> list[SearchResult]:
    tenant_id = settings.default_tenant_id

    # Resolve the retrieval language filter from the ORIGINAL query only.
    # Resolving it from a translated query would defeat `strict`: a Russian
    # query translated to English re-detects as "en" and would silently pass
    # a strict English-only filter regardless of what the user actually
    # asked in.
    source_language, include_und = _resolve_source_language(
        session=session,
        tenant_id=tenant_id,
        query=query,
        language_policy=language_policy,
        language=language,
    )

    mode_fn = _MODE_FUNCTIONS.get(mode)
    if mode_fn is None:
        raise ValueError(f"Unsupported search mode: {mode}")

    # Prefer a client-supplied translation (the calling agent is already a
    # multilingual LLM holding the user's turn -- this is free, has no added
    # latency, and needs no API keys). Only fall back to server-side
    # translation when the client supplied none.
    translated = query_translated.strip() if query_translated and query_translated.strip() else None
    if translated is None:
        translated = _server_side_translation(query)

    queries = [query]
    if translated and translated.strip().casefold() != query.strip().casefold():
        queries.append(translated.strip())

    # Retrieve on both the original and translated query when we have both,
    # then fuse by rank through the same RRF machinery hybrid_search uses
    # for keyword+vector fusion -- it's rank-based, so it fuses heterogeneous
    # runs correctly. This keeps proper nouns and code-switched terms that
    # only survive in the original query, while still benefiting from
    # whatever the translation newly makes matchable.
    per_query_limit = limit * 2 if len(queries) > 1 else limit
    runs = [
        mode_fn(
            session,
            query=q,
            limit=per_query_limit,
            tenant_id=tenant_id,
            entity_slugs=entity_slugs,
            source_tree_slug=source_tree_slug,
            source_language=source_language,
            include_und=include_und,
        )
        for q in queries
    ]

    if len(runs) == 1:
        return runs[0]
    return rrf_fuse(*runs, limit=limit)


def _resolve_response_language(query: str, *, query_language: str | None) -> str | None:
    if query_language:
        return query_language
    detected, confidence = detect_language_code(query)
    if detected and confidence >= _LANGUAGE_CONFIDENCE_THRESHOLD:
        return detected
    return None


def _translate_chunk_text(text: str, *, target_language: str) -> tuple[str, dict]:
    provider = get_translation_provider()
    translated = translate_text(text, target_language=target_language, provider=provider)
    return translated, {"model": provider.model, "target_language": target_language}


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
    query_translated: str | None = None,
    query_language: str | None = None,
    translate_quotes: bool = False,
) -> dict:
    tenant_id = settings.default_tenant_id
    results = search_by_mode(
        session,
        query=query,
        limit=limit,
        mode=mode,
        entity_slugs=entity_slugs,
        source_tree_slug=source_tree_slug,
        language_policy=language_policy,
        language=language,
        query_translated=query_translated,
        query_language=query_language,
    )

    response_language = _resolve_response_language(query, query_language=query_language)

    chunks = []
    for result in results:
        chunk: dict = {
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
        if translate_quotes:
            # `text` above always stays the verbatim, untranslated source
            # string -- citation fidelity is non-negotiable. A translation
            # is only ever added alongside it, in its own clearly-named
            # field, never substituted in. Citation labels/URLs are
            # identifiers, not prose, and are left untranslated too.
            text_translated, provenance = _translate_chunk_text(result.text, target_language=response_language or "en")
            chunk["text_translated"] = text_translated
            chunk["translation_provenance"] = provenance
        chunks.append(chunk)

    return {
        "query": query,
        "mode": mode,
        "chunks": chunks,
        "response_language": response_language,
        "response_language_directive": (
            f"The retrieved evidence text is in the corpus's original language. "
            f"Answer the user in '{response_language}' regardless of the evidence language."
            if response_language
            else None
        ),
        "notice": query_language_notice(session, query=query, tenant_id=tenant_id),
    }
