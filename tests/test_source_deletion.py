from __future__ import annotations

import json

from sqlalchemy import select


def test_delete_source_removes_text_source_chunks(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.models import Chunk, Source
    from citara.core.sources import delete_source

    source = add_text_source(db_session, title="Delete Me", text="delete cleanup target")

    deleted = delete_source(db_session, source.id)

    assert deleted is True
    assert db_session.get(Source, source.id) is None
    assert db_session.execute(select(Chunk).where(Chunk.source_id == source.id)).scalars().all() == []


def test_delete_source_removes_transcript_segments_and_chunks(db_session, fixtures_dir):
    from citara.core.ingestion.transcript import add_transcript_source
    from citara.core.models import Chunk, Source, TranscriptSegment
    from citara.core.sources import delete_source

    payload = json.loads((fixtures_dir / "sources" / "transcripts" / "sample_podcast_transcript.json").read_text())
    source = add_transcript_source(db_session, payload=payload)

    deleted = delete_source(db_session, source.id)

    assert deleted is True
    assert db_session.get(Source, source.id) is None
    assert db_session.execute(select(Chunk).where(Chunk.source_id == source.id)).scalars().all() == []
    assert db_session.execute(select(TranscriptSegment).where(TranscriptSegment.source_id == source.id)).scalars().all() == []


def test_delete_missing_source_returns_false(db_session):
    from citara.core.sources import delete_source

    assert delete_source(db_session, "src_missing") is False
