from __future__ import annotations


def test_podcast_context_pack_includes_clickable_timestamp_url(db_session, fixtures_dir):
    import json

    from citara.core.ingestion.transcript import add_transcript_source
    from citara.core.retrieval.context_pack import retrieve_context_pack

    payload = json.loads((fixtures_dir / "sources" / "transcripts" / "sample_podcast_transcript.json").read_text())
    source = add_transcript_source(db_session, payload=payload)

    pack = retrieve_context_pack(db_session, query="ambiguous action", mode="keyword")
    chunk = next(item for item in pack["chunks"] if "Procrastination" in item["text"])

    assert chunk["citation"]["source_url"] == source.canonical_url
    assert chunk["citation"]["timestamp_url"] == "https://example.com/podcast/ambiguity-action?t=1"
