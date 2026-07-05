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
