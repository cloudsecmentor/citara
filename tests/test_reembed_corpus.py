from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import select

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from reembed_corpus import reembed  # noqa: E402


def _provider():
    from citara.core.embeddings.providers import get_embedding_provider

    return get_embedding_provider()


def test_dry_run_reports_drift_without_writing(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.models import Embedding

    source = add_text_source(db_session, title="Stale", text="you're going to see that god's people don't understand")
    embedding = db_session.execute(select(Embedding).where(Embedding.source_id == source.id)).scalars().first()
    assert embedding is not None

    # Simulate a vector stored under the pre-Unicode tokenizer.
    stale = [0.0] * len(embedding.vector)
    stale[0] = 1.0
    embedding.vector = stale
    db_session.flush()

    stats = reembed(db_session, provider=_provider(), tenant_id="local", batch_size=8, apply=False)

    assert stats.updated == 1
    assert stats.drifted == 1
    assert min(stats.similarities) < 0.999
    # Nothing written: the stale vector survives a dry run.
    db_session.refresh(embedding)
    assert embedding.vector == stale


def test_apply_rewrites_stale_vectors_in_place(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.models import Chunk, Embedding

    source = add_text_source(db_session, title="Repair", text="you're going to see that god's people don't understand")
    embedding = db_session.execute(select(Embedding).where(Embedding.source_id == source.id)).scalars().first()
    assert embedding is not None
    embedding_id = embedding.id
    chunk_id = embedding.chunk_id

    embedding.vector = [0.0] * len(embedding.vector)
    db_session.flush()

    provider = _provider()
    stats = reembed(db_session, provider=provider, tenant_id="local", batch_size=8, apply=True)

    assert stats.updated == 1
    db_session.refresh(embedding)

    chunk = db_session.get(Chunk, chunk_id)
    assert chunk is not None
    expected = provider.embed_texts([chunk.text])[0]
    assert embedding.vector == [float(value) for value in expected]

    # In place: same row, same chunk, no re-ingestion.
    assert embedding.id == embedding_id
    assert embedding.chunk_id == chunk_id
    assert db_session.execute(select(Embedding).where(Embedding.source_id == source.id)).scalars().all() == [embedding]


def test_reembed_is_idempotent(db_session):
    from citara.core.ingestion.text import add_text_source

    add_text_source(db_session, title="Fresh", text="the exodus story of liberation")

    first = reembed(db_session, provider=_provider(), tenant_id="local", batch_size=8, apply=True)
    second = reembed(db_session, provider=_provider(), tenant_id="local", batch_size=8, apply=True)

    assert first.updated == 0
    assert second.updated == 0
    assert second.unchanged == second.chunks_seen


def test_missing_embedding_is_created(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.models import Embedding

    source = add_text_source(db_session, title="Orphan", text="a chunk with no vector")
    for row in db_session.execute(select(Embedding).where(Embedding.source_id == source.id)).scalars().all():
        db_session.delete(row)
    db_session.flush()

    stats = reembed(db_session, provider=_provider(), tenant_id="local", batch_size=8, apply=True)

    assert stats.created == 1
    assert db_session.execute(select(Embedding).where(Embedding.source_id == source.id)).scalars().all()


def test_other_model_rows_are_reported_and_optionally_pruned(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.models import Embedding

    source = add_text_source(db_session, title="Mixed", text="mixed model corpus")
    original = db_session.execute(select(Embedding).where(Embedding.source_id == source.id)).scalars().first()
    assert original is not None

    db_session.add(
        Embedding(
            id="emb_other_model",
            tenant_id="local",
            source_id=source.id,
            chunk_id=original.chunk_id,
            embedding_model="some-other-model",
            dimensions=len(original.vector),
            vector=list(original.vector),
        )
    )
    db_session.flush()

    reported = reembed(db_session, provider=_provider(), tenant_id="local", batch_size=8, apply=False)
    assert reported.other_model_rows == 1
    assert reported.pruned == 0

    pruned = reembed(db_session, provider=_provider(), tenant_id="local", batch_size=8, apply=True, prune_other_models=True)
    assert pruned.pruned == 1
    remaining = db_session.execute(select(Embedding).where(Embedding.source_id == source.id)).scalars().all()
    assert [row.embedding_model for row in remaining] == ["deterministic-hash-v1"]
