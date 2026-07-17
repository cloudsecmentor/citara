from __future__ import annotations

from sqlalchemy import select


def test_text_ingestion_creates_deterministic_embeddings(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.models import Chunk, Embedding

    source = add_text_source(db_session, title="Vector Note", text="cats chase mice. dogs guard houses.")

    chunks = db_session.execute(select(Chunk).where(Chunk.source_id == source.id)).scalars().all()
    embeddings = db_session.execute(select(Embedding).where(Embedding.source_id == source.id)).scalars().all()

    assert len(embeddings) == len(chunks)
    assert embeddings[0].embedding_model == "deterministic-hash-v1"
    assert embeddings[0].dimensions == 8
    assert isinstance(embeddings[0].vector, list)
    assert len(embeddings[0].vector) == 8


def test_source_deletion_removes_embeddings(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.models import Embedding
    from citara.core.sources import delete_source

    source = add_text_source(db_session, title="Delete Vector", text="delete vector cleanup")

    assert db_session.execute(select(Embedding).where(Embedding.source_id == source.id)).scalars().all()
    assert delete_source(db_session, source.id) is True
    assert db_session.execute(select(Embedding).where(Embedding.source_id == source.id)).scalars().all() == []


def test_vector_search_finds_semantic_match_without_keyword_overlap(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.vector import vector_search

    cat_source = add_text_source(db_session, title="Cat Note", text="cats chase mice and sleep in sunbeams")
    add_text_source(db_session, title="Budget Note", text="quarterly invoices require careful bookkeeping")

    results = vector_search(db_session, query="feline animal", limit=3)

    assert results
    assert results[0].source_id == cat_source.id
    assert results[0].source_title == "Cat Note"
    assert results[0].score > 0


def test_hybrid_search_combines_keyword_and_vector_results(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.hybrid import hybrid_search

    cat_source = add_text_source(db_session, title="Cat Note", text="cats chase mice and sleep in sunbeams")
    dog_source = add_text_source(db_session, title="Dog Note", text="dogs enjoy parks and fetch tennis balls")

    results = hybrid_search(db_session, query="feline parks", limit=5)
    source_ids = [result.source_id for result in results]

    assert cat_source.id in source_ids
    assert dog_source.id in source_ids


def test_hybrid_search_rank_fusion_resists_keyword_score_dominance(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.hybrid import hybrid_search

    exact = add_text_source(db_session, title="A Sunbeam Note", text="sunbeam naps")
    add_text_source(db_session, title="Filler Note", text="sunbeam naps warmth")
    spam = add_text_source(db_session, title="Spam Note", text=" ".join(["sunbeam"] * 30))

    results = hybrid_search(db_session, query="sunbeam naps", limit=3)
    source_ids = [result.source_id for result in results]

    # Under raw score addition, the keyword term count (30) would drown out
    # cosine similarity (<= 1.0) and put the spam note first.
    assert source_ids[0] == exact.id
    assert spam.id in source_ids
