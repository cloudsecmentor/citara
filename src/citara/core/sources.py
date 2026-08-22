from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from citara.core.config import settings
from citara.core.models import Chunk, Embedding, IngestionJob, Source, SourceEntity, TranscriptSegment
from citara.core.retrieval import vector_cache
from citara.core.retrieval.fts import delete_source_chunks


def list_sources(session: Session, *, tenant_id: str = settings.default_tenant_id, limit: int = 50) -> list[Source]:
    return list(
        session.execute(select(Source).where(Source.tenant_id == tenant_id).order_by(Source.created_at.desc()).limit(limit)).scalars()
    )


def get_source(session: Session, source_id: str, *, tenant_id: str = settings.default_tenant_id) -> Source | None:
    source = session.get(Source, source_id)
    if source is None or source.tenant_id != tenant_id:
        return None
    return source


def set_source_preference(
    session: Session,
    source_id: str,
    *,
    retrieval_weight: float,
    preference_label: str | None = None,
    tenant_id: str = settings.default_tenant_id,
) -> Source | None:
    if retrieval_weight <= 0:
        raise ValueError("retrieval_weight must be greater than 0")
    source = get_source(session, source_id, tenant_id=tenant_id)
    if source is None:
        return None
    metadata = dict(source.metadata_json or {})
    metadata["retrieval_weight"] = retrieval_weight
    if preference_label is not None:
        metadata["preference_label"] = preference_label
    source.metadata_json = metadata
    session.commit()
    session.refresh(source)
    # Weights are baked into the cached matrix, so a preference change has to
    # drop it even though no embedding row moved.
    vector_cache.invalidate(tenant_id)
    return source


def delete_source(session: Session, source_id: str, *, tenant_id: str = settings.default_tenant_id) -> bool:
    source = session.get(Source, source_id)
    if source is None or source.tenant_id != tenant_id:
        return False

    session.execute(delete(Embedding).where(Embedding.tenant_id == tenant_id, Embedding.source_id == source_id))
    session.execute(delete(SourceEntity).where(SourceEntity.tenant_id == tenant_id, SourceEntity.source_id == source_id))
    # Must precede the chunk delete -- it resolves the rows to drop by
    # subquerying `chunks`, which would be empty afterwards and leave the
    # full-text index holding orphaned entries.
    delete_source_chunks(session, source_id)
    session.execute(delete(Chunk).where(Chunk.tenant_id == tenant_id, Chunk.source_id == source_id))
    session.execute(
        delete(TranscriptSegment).where(
            TranscriptSegment.tenant_id == tenant_id,
            TranscriptSegment.source_id == source_id,
        )
    )
    session.execute(
        delete(IngestionJob).where(
            IngestionJob.tenant_id == tenant_id,
            IngestionJob.source_id == source_id,
        )
    )
    session.delete(source)
    session.commit()
    vector_cache.invalidate(tenant_id)
    return True
