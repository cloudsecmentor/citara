from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from hermes_knowledge.core.config import settings
from hermes_knowledge.core.models import Chunk, Embedding, IngestionJob, Source, TranscriptSegment


def list_sources(session: Session, *, tenant_id: str = settings.default_tenant_id, limit: int = 50) -> list[Source]:
    return list(
        session.execute(
            select(Source)
            .where(Source.tenant_id == tenant_id)
            .order_by(Source.created_at.desc())
            .limit(limit)
        ).scalars()
    )


def delete_source(session: Session, source_id: str, *, tenant_id: str = settings.default_tenant_id) -> bool:
    source = session.get(Source, source_id)
    if source is None or source.tenant_id != tenant_id:
        return False

    session.execute(delete(Embedding).where(Embedding.tenant_id == tenant_id, Embedding.source_id == source_id))
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
    return True
