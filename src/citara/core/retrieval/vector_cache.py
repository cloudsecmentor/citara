from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass

import numpy as np
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from citara.core.models import Embedding, Source

# How long a loaded index is trusted without re-checking the corpus version.
# Bounds staleness against writes from another process; in-process writes
# invalidate immediately via `invalidate()`, so this never delays them.
VERSION_TTL_SECONDS = 30.0

# Loaded matrices, keyed by (tenant_id, embedding_model). A corpus of 58k
# chunks at 512 dimensions is ~120 MB as float32, so this is deliberately
# process-local and rebuilt rather than duplicated per session.
_cache: dict[tuple[str, str], VectorIndex] = {}
_checked_at: dict[tuple[str, str], float] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class VectorIndex:
    """An L2-normalized matrix of every embedding for one model.

    Rows are pre-normalized so a query reduces to one matrix-vector product:
    with unit-length rows and a unit-length query, the dot product *is* the
    cosine similarity. That turns the former per-row Python cosine loop into
    a single BLAS call.
    """

    model: str
    dimensions: int
    chunk_ids: list[str]
    row_of: dict[str, int]
    matrix: np.ndarray
    weights: np.ndarray
    version: tuple

    def __len__(self) -> int:
        return len(self.chunk_ids)


def corpus_version(session: Session, *, tenant_id: str, model: str) -> tuple:
    """Cheap token that changes whenever the cache would go stale.

    Covers embeddings being added, removed, or rewritten, and also source
    `retrieval_weight` edits -- those change ranking without touching a
    single embedding row, so keying on embeddings alone would serve stale
    weights after `set_source_preference`.
    """
    embeddings = session.execute(
        select(func.count(), func.max(Embedding.created_at)).where(
            Embedding.tenant_id == tenant_id,
            Embedding.embedding_model == model,
        )
    ).one()
    sources = session.execute(select(func.max(Source.updated_at)).where(Source.tenant_id == tenant_id)).scalar()
    return (embeddings[0], str(embeddings[1]), str(sources))


def _weight_from_metadata(metadata: object) -> float:
    """Mirror of `base._source_weight`, over a raw metadata_json value."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return 1.0
    if not isinstance(metadata, dict):
        return 1.0
    try:
        weight = float(metadata.get("retrieval_weight", 1.0))
    except (TypeError, ValueError):
        return 1.0
    return weight if weight > 0 else 1.0


def _decode(raw: object) -> np.ndarray | None:
    """Decode one stored vector, packed or legacy JSON, into float32."""
    if isinstance(raw, memoryview | bytearray | bytes):
        return np.frombuffer(bytes(raw), dtype=np.float32)
    if isinstance(raw, str):
        return np.asarray(json.loads(raw), dtype=np.float32)
    if isinstance(raw, list):
        return np.asarray(raw, dtype=np.float32)
    return None


def build_index(session: Session, *, tenant_id: str, model: str, version: tuple) -> VectorIndex:
    # Deliberately raw SQL rather than the ORM. Selecting `Embedding.vector`
    # through its TypeDecorator converts every row into a Python list of
    # floats, and materializing `Source` builds 58k ORM objects to read one
    # metadata key from each -- together roughly 20 seconds of cold start on
    # this corpus. Reading the raw BLOB and handing it to `np.frombuffer`
    # skips both.
    #
    # The `embedding_model` predicate is the mixed-model guard: only vectors
    # from the model that is answering queries are loaded, so a corpus
    # mid-re-embed -- with two vector spaces present at once -- can never be
    # scored against the wrong one.
    rows = session.execute(
        text(
            "SELECT e.chunk_id, e.vector, s.metadata_json "
            "FROM embeddings e "
            "JOIN chunks c ON c.id = e.chunk_id "
            "JOIN sources s ON s.id = c.source_id "
            "WHERE e.tenant_id = :tenant_id AND e.embedding_model = :model"
        ),
        {"tenant_id": tenant_id, "model": model},
    ).all()

    if not rows:
        empty = np.zeros((0, 0), dtype=np.float32)
        return VectorIndex(model, 0, [], {}, empty, np.zeros(0, dtype=np.float32), version)

    decoded = [(chunk_id, _decode(raw), metadata) for chunk_id, raw, metadata in rows]
    dimensions = next((vector.shape[0] for _, vector, _ in decoded if vector is not None), 0)

    chunk_ids: list[str] = []
    vectors: list[np.ndarray] = []
    weights: list[float] = []
    for chunk_id, vector, metadata in decoded:
        # A differing width means something wrote a foreign model's vector
        # under this model's name. Skip it rather than silently truncate.
        if vector is None or vector.shape[0] != dimensions:
            continue
        chunk_ids.append(chunk_id)
        vectors.append(vector)
        weights.append(_weight_from_metadata(metadata))

    matrix = np.vstack(vectors).astype(np.float32, copy=False)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix = matrix / norms

    return VectorIndex(
        model=model,
        dimensions=dimensions,
        chunk_ids=chunk_ids,
        row_of={chunk_id: row for row, chunk_id in enumerate(chunk_ids)},
        matrix=matrix,
        weights=np.asarray(weights, dtype=np.float32),
        version=version,
    )


def get_index(session: Session, *, tenant_id: str, model: str) -> VectorIndex:
    """Return a current index, rebuilding only when the corpus changed.

    Freshness comes from two mechanisms rather than one:

    - Writes in this process call `invalidate()`, so an ingest or a
      preference change is reflected on the very next search.
    - `corpus_version` is re-checked at most once per `VERSION_TTL_SECONDS`
      to catch writes from *another* process (an ingestion script running
      alongside the MCP server).

    Checking the version on every search instead would cost more than the
    search: the count/max aggregate over the embeddings table measured
    ~338 ms on a 58k-chunk corpus, against ~17 ms for the matrix product it
    was guarding.
    """
    key = (tenant_id, model)
    now = time.monotonic()

    with _lock:
        cached = _cache.get(key)
        checked = _checked_at.get(key, 0.0)
        if cached is not None and (now - checked) < VERSION_TTL_SECONDS:
            return cached

    if cached is not None:
        version = corpus_version(session, tenant_id=tenant_id, model=model)
        if cached.version == version:
            with _lock:
                _checked_at[key] = now
            return cached
    else:
        version = corpus_version(session, tenant_id=tenant_id, model=model)

    index = build_index(session, tenant_id=tenant_id, model=model, version=version)
    with _lock:
        _cache[key] = index
        _checked_at[key] = now
    return index


def invalidate(tenant_id: str | None = None) -> None:
    """Force a rebuild on the next search. Called from the write paths."""
    with _lock:
        for key in [k for k in _cache if tenant_id is None or k[0] == tenant_id]:
            _cache.pop(key, None)
            _checked_at.pop(key, None)


def clear_cache() -> None:
    """Drop every cached matrix. Used by tests and after bulk re-embeds."""
    with _lock:
        _cache.clear()
        _checked_at.clear()
