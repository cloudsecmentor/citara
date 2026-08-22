from __future__ import annotations

import pytest


def _fillers(session, count: int = 12) -> None:
    """Enough documents for IDF to have something to discriminate against.

    On a two-document corpus every term is either in all documents or one, so
    IDF collapses to zero and BM25 cannot be told apart from term counting.
    """
    from citara.core.ingestion.text import add_text_source

    for index in range(count):
        add_text_source(session, title=f"Filler {index}", text="the " * 40 + "assorted filler prose about the day")


def test_stopword_frequency_does_not_outrank_content_terms(db_session):
    """Regression for the defect BM25 was introduced to fix.

    Under the previous raw term-count scorer, a chunk containing 'the' 40
    times and no content word scored 40, while the chunk that actually
    discussed the topic scored 1 -- so the corpus's most common stopword
    decided the ranking.
    """
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.keyword import search_knowledge

    _fillers(db_session)
    target = add_text_source(db_session, title="Covenant Note", text="a short note on the covenant at Sinai")

    results = search_knowledge(db_session, query="what does the covenant mean", limit=5)

    assert results, "expected at least one match"
    assert results[0].source_id == target.id


def test_query_terms_that_are_fts5_operators_do_not_break_the_query(db_session):
    """'and', 'or', 'not', 'near' are FTS5 operators, not just words.

    Unquoted, an ordinary English query hands FTS5 its own boolean syntax and
    either errors or silently changes meaning. Every token is quoted, so
    these are searched as literals.
    """
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.keyword import search_knowledge

    _fillers(db_session)
    target = add_text_source(db_session, title="Operators", text="rest and peace are near the wilderness")

    for query in ("rest and peace", "near wilderness", "NOT peace", "peace OR rest", "a NEAR b"):
        results = search_knowledge(db_session, query=query, limit=5)
        assert isinstance(results, list)

    assert any(r.source_id == target.id for r in search_knowledge(db_session, query="rest and peace", limit=5))


def test_ingestion_populates_and_deletion_clears_the_fts_index(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval import fts
    from citara.core.sources import delete_source

    assert fts.fts_available(db_session), "test fixture should provide FTS5"
    assert fts.index_row_count(db_session) == 0

    source = add_text_source(db_session, title="Indexed", text="first paragraph here\n\nsecond paragraph here")
    indexed = fts.index_row_count(db_session)
    assert indexed > 0

    delete_source(db_session, source.id)
    assert fts.index_row_count(db_session) == 0, "deleting a source must not leave orphaned index rows"


def test_retrieval_weight_still_orders_results(db_session):
    """Weighting is applied inside the ranking SQL, so it stays exact."""
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.keyword import search_knowledge
    from citara.core.sources import set_source_preference

    _fillers(db_session)
    first = add_text_source(db_session, title="Alpha Covenant", text="covenant discussion alpha")
    second = add_text_source(db_session, title="Beta Covenant", text="covenant discussion beta")

    set_source_preference(db_session, second.id, retrieval_weight=5.0, preference_label="current")
    ranked = [r.source_id for r in search_knowledge(db_session, query="covenant discussion", limit=5)]

    assert ranked.index(second.id) < ranked.index(first.id)


def test_scan_fallback_agrees_with_the_indexed_path(db_session, monkeypatch):
    """The portable BM25 scan and FTS5's native bm25() should agree on the winner."""
    from citara.core.chunking.simple import tokenize
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.keyword import scan_search, search_knowledge

    _fillers(db_session)
    target = add_text_source(db_session, title="Covenant Note", text="a short note on the covenant at Sinai")

    indexed = search_knowledge(db_session, query="what does the covenant mean", limit=5)
    scanned = scan_search(
        db_session,
        query_tokens=tokenize("what does the covenant mean"),
        limit=5,
        tenant_id="local",
    )

    assert indexed[0].source_id == target.id
    assert scanned[0].source_id == target.id


def test_empty_query_returns_nothing(db_session):
    from citara.core.retrieval.keyword import search_knowledge

    _fillers(db_session)
    assert search_knowledge(db_session, query="   ...  ", limit=5) == []


@pytest.mark.parametrize(
    ("total", "df", "expectation"),
    [
        (100, 1, "high"),
        (100, 50, "low"),
        (100, 100, "negligible"),
    ],
)
def test_idf_is_never_negative(total, df, expectation):
    """A term in every document must never contribute a negative weight.

    Without this, a document could be *penalized* for containing a query
    term -- the pathological case Lucene's floor exists to prevent. The
    `log(1 + x)` form already guarantees non-negativity, so a term appearing
    in every document decays to negligible rather than flipping sign.
    """
    from citara.core.retrieval.bm25 import idf

    value = idf(total, df)
    assert value >= 0.0
    if expectation == "high":
        assert value > 3.0
    elif expectation == "negligible":
        assert value < 0.01
