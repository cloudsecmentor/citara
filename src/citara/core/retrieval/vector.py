from __future__ import annotations

import math

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from citara.core.config import settings
from citara.core.embeddings.providers import get_embedding_provider
from citara.core.embeddings.service import embed_query
from citara.core.models import Chunk, Source
from citara.core.retrieval import vector_cache
from citara.core.retrieval.base import SearchResult, apply_source_filters, result_from


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    Raises on a length mismatch rather than comparing a prefix. The previous
    `zip(..., strict=False)` silently truncated to the shorter vector while
    still normalizing by the longer one's magnitude -- so an 8-dimensional
    stored vector scored against a 512-dimensional query returned a
    plausible-looking number that was not a cosine of anything. During a
    re-embed, when both vector spaces are present at once, that produced
    silently wrong rankings with no error.
    """
    if len(left) != len(right):
        raise ValueError(f"Cannot compare vectors of different dimensions: {len(left)} vs {len(right)}")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _allowed_rows(
    session: Session,
    index: vector_cache.VectorIndex,
    *,
    tenant_id: str,
    entity_slugs: list[str] | None,
    source_tree_slug: str | None,
    source_language: str | None,
    include_und: bool,
) -> np.ndarray | None:
    """Row indices permitted by the filters, or None when unfiltered.

    Resolving filters to a row subset *before* scoring keeps results exact.
    Scoring everything and filtering the top-N afterwards would quietly lose
    recall whenever a filter is selective -- a rare entity or a minority
    language could have every one of its chunks fall outside the window.
    """
    if not (entity_slugs or source_tree_slug or source_language):
        return None

    statement = select(Chunk.id).join(Source, Source.id == Chunk.source_id)
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
        return np.zeros(0, dtype=np.int64)

    row_of = index.row_of
    rows = [row_of[chunk_id] for chunk_id in session.execute(statement).scalars() if chunk_id in row_of]
    return np.asarray(rows, dtype=np.int64)


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
    model = get_embedding_provider().model
    index = vector_cache.get_index(session, tenant_id=tenant_id, model=model)
    if len(index) == 0:
        return []

    query_vector = np.asarray(embed_query(query), dtype=np.float32)
    if query_vector.shape[0] != index.dimensions:
        # The stored vectors were produced by a different model than the one
        # now answering queries. Better to return nothing than to rank against
        # an incompatible space.
        return []
    norm = float(np.linalg.norm(query_vector))
    if norm == 0:
        return []
    query_vector /= norm

    candidates = _allowed_rows(
        session,
        index,
        tenant_id=tenant_id,
        entity_slugs=entity_slugs,
        source_tree_slug=source_tree_slug,
        source_language=source_language,
        include_und=include_und,
    )

    if candidates is None:
        scores = index.matrix @ query_vector
        rows = np.arange(index.matrix.shape[0])
    else:
        if candidates.size == 0:
            return []
        scores = index.matrix[candidates] @ query_vector
        rows = candidates

    scores = scores * index.weights[rows]

    positive = scores > 0
    if not positive.any():
        return []
    scores, rows = scores[positive], rows[positive]

    take = min(limit, scores.shape[0])
    top = np.argpartition(-scores, take - 1)[:take] if scores.shape[0] > take else np.arange(scores.shape[0])
    top = top[np.argsort(-scores[top])]

    chunk_ids = [index.chunk_ids[rows[position]] for position in top]
    ranked_scores = {index.chunk_ids[rows[position]]: float(scores[position]) for position in top}

    pairs = session.execute(select(Chunk, Source).join(Source, Source.id == Chunk.source_id).where(Chunk.id.in_(chunk_ids))).all()
    by_id = {chunk.id: (chunk, source) for chunk, source in pairs}

    results = []
    for chunk_id in chunk_ids:
        if chunk_id not in by_id:
            continue
        chunk, source = by_id[chunk_id]
        results.append(result_from(source, chunk, ranked_scores[chunk_id]))
    return results
