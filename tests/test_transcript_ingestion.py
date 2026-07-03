from __future__ import annotations

import json


def test_add_transcript_source_preserves_segments_and_timestamp_citations(db_session, fixtures_dir):
    from sqlalchemy import select

    from citara.core.ingestion.transcript import add_transcript_source
    from citara.core.models import TranscriptSegment
    from citara.core.retrieval.keyword import search_knowledge

    payload = json.loads((fixtures_dir / "sources" / "transcripts" / "sample_podcast_transcript.json").read_text())

    source = add_transcript_source(db_session, payload=payload)
    segments = db_session.execute(select(TranscriptSegment).order_by(TranscriptSegment.start_ms)).scalars().all()
    results = search_knowledge(db_session, query="two minute start")

    assert source.source_type == "podcast_episode"
    assert source.title == "Ambiguity and Action"
    assert source.canonical_url == "https://example.com/podcast/ambiguity-action"
    assert len(segments) == 2
    assert segments[0].speaker == "Host"
    assert segments[0].start_ms == 1000
    assert segments[1].end_ms == 9000
    assert results[0].citation_label == "Test Podcast, Ambiguity and Action, 00:05"
    assert results[0].start_ms == 5000
    assert results[0].end_ms == 9000
