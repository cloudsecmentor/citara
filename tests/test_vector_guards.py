from __future__ import annotations

import pytest


def test_cosine_similarity_rejects_mismatched_dimensions():
    """A dimension mismatch must be loud, not silently truncated.

    The previous implementation used `zip(..., strict=False)`, so an 8-dim
    stored vector against a 512-dim query summed only 8 products while
    dividing by the 512-dim magnitude -- returning a plausible number that
    was not a cosine. This is exactly the state a corpus is in midway
    through a re-embed.
    """
    from citara.core.retrieval.vector import cosine_similarity

    with pytest.raises(ValueError, match="different dimensions"):
        cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0])


def test_vector_search_ignores_embeddings_from_another_model(db_session):
    """A half-migrated corpus must not be scored against a foreign space."""
    from uuid import uuid4

    from sqlalchemy import select

    from citara.core.ingestion.text import add_text_source
    from citara.core.models import Chunk, Embedding
    from citara.core.retrieval import vector_cache
    from citara.core.retrieval.vector import vector_search

    vector_cache.clear_cache()
    source = add_text_source(db_session, title="Cat Note", text="cats chase mice and sleep in sunbeams")

    # Simulate a re-embed in flight: same chunks, a second model's vectors
    # at a different width sitting alongside the active model's.
    chunk = db_session.execute(select(Chunk).where(Chunk.source_id == source.id)).scalars().first()
    db_session.add(
        Embedding(
            id=f"emb_{uuid4().hex}",
            tenant_id="local",
            source_id=source.id,
            chunk_id=chunk.id,
            embedding_model="some-other-model-v2",
            dimensions=512,
            vector=[0.1] * 512,
        )
    )
    db_session.flush()
    vector_cache.clear_cache()

    results = vector_search(db_session, query="feline animal", limit=5)

    assert results, "the active model's vectors should still be searchable"
    assert all(r.source_id == source.id for r in results)


def test_entity_filter_does_not_lose_recall(db_session):
    """Filters resolve to a row subset before scoring, not after.

    With enough unrelated-but-higher-scoring chunks in the corpus, a
    filter-after-top-N implementation returns nothing here.
    """
    from citara.core.ingestion.text import add_text_source
    from citara.core.ingestion.transcript import add_transcript_source
    from citara.core.retrieval import vector_cache
    from citara.core.retrieval.vector import vector_search

    vector_cache.clear_cache()
    for index in range(30):
        add_text_source(db_session, title=f"Feline Filler {index}", text="cats felines kittens chase mice in sunbeams")

    add_transcript_source(
        db_session,
        payload={
            "show_title": "Show",
            "episode_title": "Tagged Episode",
            "segments": [{"start_ms": 0, "end_ms": 1000, "text": "cats and felines discussed at length here"}],
            "entities": [{"type": "person", "slug": "marty-solomon", "label": "Marty Solomon", "role": "host"}],
        },
    )
    vector_cache.clear_cache()

    filtered = vector_search(db_session, query="feline animal", limit=5, entity_slugs=["person:marty-solomon"])

    assert filtered, "the tagged source must still be reachable behind a selective filter"
    assert all(r.source_type == "podcast_episode" for r in filtered)


def test_unknown_entity_slug_returns_nothing(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval import vector_cache
    from citara.core.retrieval.vector import vector_search

    vector_cache.clear_cache()
    add_text_source(db_session, title="Cat Note", text="cats chase mice")
    vector_cache.clear_cache()

    assert vector_search(db_session, query="feline", limit=5, entity_slugs=["person:nobody"]) == []


def test_cache_refreshes_when_new_sources_are_ingested(db_session):
    """A stale matrix would make freshly ingested chunks invisible."""
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval import vector_cache
    from citara.core.retrieval.vector import vector_search

    vector_cache.clear_cache()
    add_text_source(db_session, title="First", text="cats chase mice")
    assert vector_search(db_session, query="feline", limit=5)

    added = add_text_source(db_session, title="Second", text="felines kittens sunbeams warmth")
    results = vector_search(db_session, query="kitten sunbeam", limit=5)

    assert any(r.source_id == added.id for r in results), "cache did not pick up the new source"


def test_cache_refreshes_when_retrieval_weight_changes(db_session):
    """Preference edits change ranking without touching an embedding row."""
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval import vector_cache
    from citara.core.retrieval.vector import vector_search
    from citara.core.sources import set_source_preference

    vector_cache.clear_cache()
    first = add_text_source(db_session, title="Alpha", text="cats chase mice in sunbeams")
    second = add_text_source(db_session, title="Beta", text="cats chase mice in sunbeams")

    before = [r.source_id for r in vector_search(db_session, query="feline mice", limit=5)]
    assert set(before) >= {first.id, second.id}

    set_source_preference(db_session, second.id, retrieval_weight=9.0, preference_label="current")
    after = [r.source_id for r in vector_search(db_session, query="feline mice", limit=5)]

    assert after[0] == second.id, "weight change should be reflected without an embedding write"
