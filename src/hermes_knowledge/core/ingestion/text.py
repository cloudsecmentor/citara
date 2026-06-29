from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from hermes_knowledge.core.chunking.simple import chunk_text
from hermes_knowledge.core.config import settings
from hermes_knowledge.core.embeddings.service import embed_chunks
from hermes_knowledge.core.models import Chunk, Source
from hermes_knowledge.core.tenants import ensure_local_identity


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
    source = Source(
        id=f"src_{uuid4().hex}",
        tenant_id=tenant_id,
        user_id=user_id,
        collection_id=collection_id,
        source_type="text_note",
        title=title,
        status="succeeded",
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

    session.commit()
    session.refresh(source)
    return source
