from __future__ import annotations


def test_add_text_source_chunks_and_searches_note(db_session, fixtures_dir):
    from hermes_knowledge.core.ingestion.text import add_text_source
    from hermes_knowledge.core.retrieval.keyword import search_knowledge

    text = (fixtures_dir / "sources" / "notes" / "procrastination.md").read_text()

    source = add_text_source(db_session, title="Procrastination Note", text=text)
    results = search_knowledge(db_session, query="next physical action")

    assert source.source_type == "text_note"
    assert source.title == "Procrastination Note"
    assert len(results) >= 1
    top = results[0]
    assert top.source_id == source.id
    assert top.source_title == "Procrastination Note"
    assert top.source_type == "text_note"
    assert "next physical action" in top.text.lower()
    assert top.citation_label == "Procrastination Note, chunk 1"
    assert top.score > 0


def test_retrieve_context_pack_returns_compact_cited_chunks(db_session, fixtures_dir):
    from hermes_knowledge.core.ingestion.text import add_text_source
    from hermes_knowledge.core.retrieval.context_pack import retrieve_context_pack

    text = (fixtures_dir / "sources" / "notes" / "procrastination.md").read_text()
    add_text_source(db_session, title="Procrastination Note", text=text)

    pack = retrieve_context_pack(db_session, query="ambiguous task", limit=2)

    assert pack["query"] == "ambiguous task"
    assert len(pack["chunks"]) >= 1
    citation = pack["chunks"][0]["citation"]
    assert citation["label"] == "Procrastination Note, chunk 1"
    assert citation["source_url"] is None
    assert citation["timestamp_url"] is None
    assert citation["page_number"] is None
