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


def test_context_pack_exposes_timestamp_provenance(db_session, fixtures_dir):
    import json

    from citara.core.ingestion.transcript import add_transcript_source
    from citara.core.retrieval.context_pack import retrieve_context_pack

    payload = json.loads((fixtures_dir / "sources" / "transcripts" / "sample_podcast_transcript.json").read_text())
    payload["metadata"] = {
        "transcript_provenance": "generated_openai_whisper",
        "timestamp_provenance": "asr_segment",
        "timestamp_precision": "segment",
        "citation_anchor": "chunk_start",
    }
    add_transcript_source(db_session, payload=payload)

    pack = retrieve_context_pack(db_session, query="ambiguous action", mode="keyword")
    chunk = next(item for item in pack["chunks"] if "Procrastination" in item["text"])

    assert chunk["transcript_provenance"] == "generated_openai_whisper"
    assert chunk["citation"]["timestamp_provenance"] == "asr_segment"
    assert chunk["citation"]["timestamp_precision"] == "segment"
    assert chunk["citation"]["citation_anchor"] == "chunk_start"


def test_youtube_video_gets_deep_link_with_ampersand_separator(db_session, fixtures_dir):
    import json

    from citara.core.ingestion.transcript import add_transcript_source
    from citara.core.retrieval.context_pack import retrieve_context_pack

    payload = json.loads((fixtures_dir / "sources" / "transcripts" / "sample_podcast_transcript.json").read_text())
    payload["source_type"] = "youtube_video"
    payload["provider"] = "youtube"
    payload["show_title"] = "Test Channel"
    payload["episode_title"] = "Ambiguity on Video"
    payload["episode_url"] = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    source = add_transcript_source(db_session, payload=payload)

    assert source.source_type == "youtube_video"
    assert source.provider == "youtube"

    pack = retrieve_context_pack(db_session, query="ambiguous action", mode="keyword")
    chunk = next(item for item in pack["chunks"] if "Procrastination" in item["text"])

    # The canonical URL already carries ?v=, so the offset must join with & to stay valid.
    assert chunk["citation"]["timestamp_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1"
    assert chunk["citation"]["label"].startswith("Test Channel, Ambiguity on Video, ")


def test_non_timestamped_source_type_gets_no_deep_link(db_session):
    from citara.core.models import Chunk, Source
    from citara.core.retrieval.keyword import _timestamp_url

    source = Source(source_type="note", canonical_url="https://example.com/note", metadata_json={})
    chunk = Chunk(chunk_index=1, start_ms=5000)

    assert _timestamp_url(source, chunk) is None
