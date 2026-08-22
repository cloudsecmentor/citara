from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from citara.core.chunking.simple import chunk_text
from citara.core.config import settings
from citara.core.embeddings.service import embed_chunks
from citara.core.jobs import record_succeeded_ingestion_job
from citara.core.language.detect import detect_language_code
from citara.core.models import Chunk, Source
from citara.core.retrieval.fts import index_chunks
from citara.core.tenants import ensure_local_identity


def add_text_source(
    session: Session,
    *,
    title: str,
    text: str,
    collection_id: str | None = None,
    tenant_id: str = settings.default_tenant_id,
    user_id: str = settings.default_user_id,
) -> Source:
    ensure_local_identity(session, tenant_id=tenant_id, user_id=user_id)

    detected_language, language_confidence = detect_language_code(text)

    # Store language on the Source so retrieval can filter by it.
    # If detection is uncertain, keep NULL.
    source_language = detected_language if (detected_language and language_confidence >= 0.4) else None

    source = Source(
        id=f"src_{uuid4().hex}",
        tenant_id=tenant_id,
        user_id=user_id,
        collection_id=collection_id,
        source_type="text_note",
        title=title,
        status="succeeded",
        language=source_language,
        metadata_json={"input_type": "text"},
    )
    session.add(source)
    session.flush()

    chunks = []
    for index, chunk in enumerate(chunk_text(text), start=1):
        db_chunk = Chunk(
            id=f"chk_{uuid4().hex}",
            tenant_id=tenant_id,
            source_id=source.id,
            chunk_index=index,
            text=chunk.text,
            start_char=chunk.start_char,
            end_char=chunk.end_char,
            metadata_json={},
        )
        session.add(db_chunk)
        chunks.append(db_chunk)

    session.flush()
    embed_chunks(session, chunks, tenant_id=tenant_id)
    index_chunks(session, chunks)
    record_succeeded_ingestion_job(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        source_id=source.id,
        job_type="text_ingestion",
        input_json={"title": title, "collection_id": collection_id},
        result_json={"source_id": source.id, "chunk_count": len(chunks)},
    )

    session.commit()
    session.refresh(source)
    return source
