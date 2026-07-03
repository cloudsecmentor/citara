from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from citara.core.config import settings
from citara.core.embeddings.providers import EmbeddingProvider, get_embedding_provider
from citara.core.models import Chunk, Embedding


def embed_chunks(
    session: Session,
    chunks: list[Chunk],
    *,
    provider: EmbeddingProvider | None = None,
    tenant_id: str = settings.default_tenant_id,
) -> list[Embedding]:
    if not chunks:
        return []
    provider = provider or get_embedding_provider()
    vectors = provider.embed_texts([chunk.text for chunk in chunks])
    embeddings = [
        Embedding(
            id=f"emb_{uuid4().hex}",
            tenant_id=tenant_id,
            source_id=chunk.source_id,
            chunk_id=chunk.id,
            embedding_model=provider.model,
            dimensions=len(vector),
            vector=[float(value) for value in vector],
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    session.add_all(embeddings)
    session.flush()
    return embeddings


def embed_query(query: str, *, provider: EmbeddingProvider | None = None) -> list[float]:
    provider = provider or get_embedding_provider()
    return provider.embed_texts([query])[0]
