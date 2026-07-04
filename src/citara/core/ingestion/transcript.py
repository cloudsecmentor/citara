from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from citara.core.config import settings
from citara.core.embeddings.service import embed_chunks
from citara.core.entities import attach_source_entities
from citara.core.jobs import record_succeeded_ingestion_job
from citara.core.models import Chunk, Source, TranscriptSegment
from citara.core.tenants import ensure_local_identity


def add_transcript_source(
    session: Session,
    *,
    payload: dict,
    collection_id: str | None = None,
    tenant_id: str = settings.default_tenant_id,
    user_id: str = settings.default_user_id,
) -> Source:
    ensure_local_identity(session, tenant_id=tenant_id, user_id=user_id)
    show_title = payload["show_title"]
    episode_title = payload["episode_title"]
    episode_url = payload.get("episode_url")
    source = Source(
        id=f"src_{uuid4().hex}",
        tenant_id=tenant_id,
        user_id=user_id,
        collection_id=collection_id,
        source_type="podcast_episode",
        title=episode_title,
        canonical_url=episode_url,
        provider="podcast",
        status="succeeded",
        metadata_json={"show_title": show_title, "input_type": "transcript_fixture"},
    )
    session.add(source)
    session.flush()
    attach_source_entities(session, source_id=source.id, entities=payload.get("entities"), tenant_id=tenant_id)

    chunks = []
    for index, segment_payload in enumerate(payload.get("segments", []), start=1):
        segment = TranscriptSegment(
            id=f"seg_{uuid4().hex}",
            tenant_id=tenant_id,
            source_id=source.id,
            start_ms=segment_payload["start_ms"],
            end_ms=segment_payload.get("end_ms"),
            speaker=segment_payload.get("speaker"),
            text=segment_payload["text"],
            metadata_json=segment_payload.get("metadata_json", {}),
        )
        session.add(segment)
        session.flush()
        chunk = Chunk(
            id=f"chk_{uuid4().hex}",
            tenant_id=tenant_id,
            source_id=source.id,
            transcript_segment_id=segment.id,
            chunk_index=index,
            text=segment.text,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            metadata_json=segment_payload.get("metadata_json", {}),
        )
        session.add(chunk)
        chunks.append(chunk)

    session.flush()
    embed_chunks(session, chunks, tenant_id=tenant_id)
    record_succeeded_ingestion_job(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        source_id=source.id,
        job_type="transcript_ingestion",
        input_json={"show_title": show_title, "episode_title": episode_title, "collection_id": collection_id},
        result_json={"source_id": source.id, "segment_count": len(chunks), "chunk_count": len(chunks)},
    )

    session.commit()
    session.refresh(source)
    return source
