from __future__ import annotations


def test_language_policy_auto_filters_by_source_language(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.context_pack import search_by_mode

    # Both sources contain the same English text so keyword matching would match
    # both without language filtering.
    s_en = add_text_source(db_session, title="EN Source", text="cats chase mice")
    s_other = add_text_source(db_session, title="Other Source", text="cats chase mice")

    s_en.language = "en"
    s_other.language = "es"
    db_session.commit()

    results = search_by_mode(
        db_session,
        query="cats chase",
        mode="keyword",
        limit=5,
        language_policy="auto",
    )

    assert results, "Expected at least one result"
    assert all(result.source_id == s_en.id for result in results)


def test_language_policy_auto_guards_against_filter_trap_on_thin_minority_language(db_session):
    """A detected-but-thin minority query language must not hide a large corpus.

    Without the guard in `_resolve_source_language`, detecting the query as
    "ru" (because exactly one mislabeled/foreign source exists) would filter
    the corpus down to `language == 'ru' OR language IS NULL`, hiding all 12
    English sources -- even though the query also carries the literal
    keyword terms ("cats chase") that only the English majority contains.
    """

    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.context_pack import search_by_mode

    for i in range(12):
        source = add_text_source(db_session, title=f"English Source {i}", text=f"cats chase mice across yard number {i}")
        source.language = "en"

    minority = add_text_source(db_session, title="Stray Minority Source", text="a completely unrelated snippet about turnips")
    minority.language = "ru"
    db_session.commit()

    # Cyrillic-dominant (so detect_language_code picks "ru"), but also
    # carries the English keyword terms the majority corpus shares -- the
    # lone "ru"-labeled source shares none of them.
    query = "Что Что Что Что Что Что Что Что Что Что cats chase"

    results = search_by_mode(db_session, query=query, mode="keyword", limit=20, language_policy="auto")

    assert results, "the filter trap must not hide the English majority behind one mislabeled source"
    assert any(result.source_title.startswith("English Source") for result in results)


def test_cross_language_query_with_translation_returns_results_and_notice(db_session):
    """Stage 2's main fix: a client-supplied translation bridges retrieval.

    Stage 1 alone does not make a Russian query match English text (Cyrillic
    tokens never equal English ones), so this specifically exercises the
    dual-query RRF fusion added in Stage 2 via `query_translated`.
    """

    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.context_pack import retrieve_context_pack

    source = add_text_source(
        db_session,
        title="Exodus Notes",
        text="The Exodus tells the story of Israel leaving slavery in Egypt and crossing the sea.",
    )
    source.language = "en"
    db_session.commit()

    query_ru = "Что говорит об Исходе"
    query_translated = "What does it say about the Exodus and Egypt"

    pack = retrieve_context_pack(
        db_session,
        query=query_ru,
        query_translated=query_translated,
        mode="hybrid",
        language_policy="auto",
    )

    assert pack["chunks"], "expected non-empty results via translated-query fusion"
    assert any("Exodus Notes" in chunk["citation"]["label"] for chunk in pack["chunks"])
    # `text` must always stay the verbatim, untranslated source string.
    assert all("Exodus" in chunk["text"] for chunk in pack["chunks"])

    # The notice is computed from the ORIGINAL query/corpus language
    # relationship and is still useful metadata even though retrieval
    # succeeded -- it tells the caller *why* a translation was needed.
    assert pack["notice"] == {
        "code": "cross_language_query",
        "query_language": "ru",
        "corpus_languages": ["en"],
    }
    assert pack["response_language"] == "ru"
    assert pack["response_language_directive"] is not None
    assert "ru" in pack["response_language_directive"]


def test_cross_language_query_without_translation_is_explainable_not_silent(db_session):
    """Stage 1's fix: an empty result set must become explainable, not silent."""

    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.context_pack import retrieve_context_pack

    source = add_text_source(db_session, title="Exodus Notes", text="The Exodus tells the story of Israel leaving Egypt.")
    source.language = "en"
    db_session.commit()

    # No query_translated, and the default TRANSLATION_PROVIDER is a no-op,
    # so this is expected to come back empty -- but explainably so.
    pack = retrieve_context_pack(db_session, query="Что говорит об Исходе", mode="hybrid", language_policy="auto")

    assert pack["chunks"] == []
    assert pack["notice"] == {
        "code": "cross_language_query",
        "query_language": "ru",
        "corpus_languages": ["en"],
    }


def test_query_language_notice_is_none_when_query_matches_corpus_language(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.context_pack import retrieve_context_pack

    source = add_text_source(db_session, title="EN Source", text="cats chase mice")
    source.language = "en"
    db_session.commit()

    pack = retrieve_context_pack(db_session, query="cats chase", mode="keyword")

    assert pack["chunks"]
    assert pack["notice"] is None


def test_query_language_notice_fires_when_query_tokenizes_to_nothing(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.context_pack import retrieve_context_pack

    source = add_text_source(db_session, title="EN Source", text="cats chase mice")
    source.language = "en"
    db_session.commit()

    pack = retrieve_context_pack(db_session, query="???!!!", mode="keyword")

    assert pack["chunks"] == []
    assert pack["notice"] is not None
    assert pack["notice"]["code"] == "cross_language_query"
    assert pack["notice"]["corpus_languages"] == ["en"]


def test_retrieve_context_pack_response_language_honors_explicit_override(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.context_pack import retrieve_context_pack

    source = add_text_source(db_session, title="EN Source", text="cats chase mice")
    source.language = "en"
    db_session.commit()

    pack = retrieve_context_pack(db_session, query="cats chase", mode="keyword", query_language="ru")

    assert pack["response_language"] == "ru"


def test_retrieve_context_pack_translate_quotes_adds_fields_without_altering_text(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.context_pack import retrieve_context_pack

    source = add_text_source(db_session, title="EN Source", text="cats chase mice")
    source.language = "en"
    db_session.commit()

    pack = retrieve_context_pack(db_session, query="cats chase", mode="keyword", translate_quotes=True)

    assert pack["chunks"]
    for chunk in pack["chunks"]:
        assert "text" in chunk and chunk["text"]
        # The default TranslationProvider is a no-op, so the translated
        # field is a pass-through here -- but it must be present and must
        # never overwrite `text`.
        assert chunk["text_translated"] == chunk["text"]
        assert chunk["translation_provenance"]["model"] == "noop"
        assert chunk["translation_provenance"]["target_language"] == "en"


def test_retrieve_context_pack_without_translate_quotes_omits_translation_fields(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.context_pack import retrieve_context_pack

    source = add_text_source(db_session, title="EN Source", text="cats chase mice")
    source.language = "en"
    db_session.commit()

    pack = retrieve_context_pack(db_session, query="cats chase", mode="keyword")

    assert pack["chunks"]
    for chunk in pack["chunks"]:
        assert "text_translated" not in chunk
        assert "translation_provenance" not in chunk
