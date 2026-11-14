from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from citara.core.entities import list_source_entities
from citara.core.models import Chunk, Source

SCRIPT = Path(__file__).parents[1] / "scripts" / "import_youtube_captions.py"
SPEC = importlib.util.spec_from_file_location("import_youtube_captions", SCRIPT)
assert SPEC and SPEC.loader
caption_importer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(caption_importer)


def caption_payload() -> dict:
    return {
        "events": [
            {"tStartMs": 0, "dDurationMs": 50_000},
            {
                "tStartMs": 1_000,
                "dDurationMs": 2_000,
                "segs": [
                    {"utf8": "First", "tOffsetMs": 0},
                    {"utf8": " caption", "tOffsetMs": 400},
                ],
            },
            {"tStartMs": 3_000, "dDurationMs": 500, "aAppend": 1, "segs": [{"utf8": "\n"}]},
            {
                "tStartMs": 3_100,
                "dDurationMs": 2_000,
                "segs": [{"utf8": "Second"}, {"utf8": " thought", "tOffsetMs": 300}],
            },
        ]
    }


def test_parse_json3_uses_real_caption_events_and_word_timestamps() -> None:
    cues = caption_importer.parse_json3_caption(caption_payload())

    assert [cue["text"] for cue in cues] == ["First caption", "Second thought"]
    assert cues[0]["start_ms"] == 1_000
    assert cues[0]["end_ms"] == 3_000
    assert cues[0]["words"][1] == {"start_ms": 1_400, "word": "caption"}


def test_parse_json3_clips_display_window_duration_at_next_substantive_cue() -> None:
    payload = {
        "events": [
            {"tStartMs": 1_000, "dDurationMs": 10_000, "segs": [{"utf8": "First"}]},
            {"tStartMs": 2_500, "dDurationMs": 2_000, "segs": [{"utf8": "Second"}]},
        ]
    }

    cues = caption_importer.parse_json3_caption(payload)

    assert cues[0]["end_ms"] == 2_500
    assert cues[1]["end_ms"] == 4_500


def test_chunk_caption_cues_preserves_first_word_anchor_and_timestamp_url() -> None:
    cues = caption_importer.parse_json3_caption(caption_payload())

    chunks = caption_importer.chunk_caption_cues(cues, "https://www.youtube.com/watch?v=abcdefghijk", max_chars=1_000)

    assert len(chunks) == 1
    assert chunks[0]["text"] == "First caption Second thought"
    assert chunks[0]["start_ms"] == 1_000
    assert chunks[0]["end_ms"] == 5_100
    assert chunks[0]["metadata_json"]["timestamp_url"] == "https://www.youtube.com/watch?v=abcdefghijk&t=1s"
    assert chunks[0]["metadata_json"]["chunk_anchor"] == "first_word_timestamp"


def test_timestamp_url_replaces_existing_anchor_and_preserves_fragment() -> None:
    assert (
        caption_importer.timestamp_url("https://www.youtube.com/watch?v=abcdefghijk&t=9#chapter", 3_000)
        == "https://www.youtube.com/watch?v=abcdefghijk&t=3s#chapter"
    )


def test_chunk_caption_cues_splits_a_single_oversized_cue() -> None:
    cue = {"start_ms": 0, "end_ms": 200_000, "text": "x" * 2_500, "words": []}
    chunks = caption_importer.chunk_caption_cues([cue], "https://www.youtube.com/watch?v=abcdefghijk")
    assert all(len(chunk["text"]) <= 1_200 for chunk in chunks)
    assert all(chunk["end_ms"] - chunk["start_ms"] <= 90_000 for chunk in chunks)


def test_item_and_caption_paths_are_fail_closed(tmp_path: Path) -> None:
    item = {
        "source_tree_slug": "../../escape",
        "video_id": "abcdefghijk",
        "guid": "youtube-abcdefghijk",
        "canonical_url": "https://www.youtube.com/watch?v=abcdefghijk",
    }
    with pytest.raises(ValueError, match="source_tree_slug"):
        caption_importer.validate_item(item)
    with pytest.raises(ValueError, match="escapes"):
        caption_importer.resolve_caption_path(
            str(tmp_path / "secret.json"),
            manifest_path=tmp_path / "manifest.json",
            caption_root=tmp_path / "captions",
        )


def test_import_manifest_is_idempotent_and_sets_author_entities_and_provenance(tmp_path: Path) -> None:
    citara_root = tmp_path / "citara-data"
    caption = tmp_path / "captions" / "video.en.json3"
    caption.parent.mkdir()
    caption.write_text(json.dumps(caption_payload()), encoding="utf-8")
    manifest = tmp_path / "approved-channel-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "channel": "Tim Mackie Archives",
                "provider": "youtube",
                "publisher": "Tim Mackie Archives",
                "publisher_slug": "tim-mackie-archives",
                "author": "Tim Mackie",
                "entities": [
                    {"type": "person", "slug": "tim-mackie", "label": "Tim Mackie", "role": "teacher"},
                    {
                        "type": "organization",
                        "slug": "tim-mackie-archives",
                        "label": "Tim Mackie Archives",
                        "role": "uploader",
                    },
                ],
                "episodes": [
                    {
                        "queue_number": 1,
                        "guid": "youtube-abcdefghijk",
                        "video_id": "abcdefghijk",
                        "title": "A Teaching",
                        "canonical_url": "https://www.youtube.com/watch?v=abcdefghijk",
                        "duration_seconds": 50,
                        "source_tree_slug": "tim-mackie-test-series",
                        "series_title": "Test Series",
                        "caption_path": str(caption),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    first = caption_importer.run_import(citara_root=citara_root, manifest_path=manifest)
    second = caption_importer.run_import(citara_root=citara_root, manifest_path=manifest)

    assert first == [{"guid": "youtube-abcdefghijk", "status": "imported", "source_id": first[0]["source_id"]}]
    assert second == [{"guid": "youtube-abcdefghijk", "status": "skipped_existing", "source_id": first[0]["source_id"]}]
    with caption_importer.SessionLocal() as session:
        source = session.execute(select(Source)).scalar_one()
        assert source.author == "Tim Mackie"
        assert source.provider == "youtube"
        assert source.source_type == "youtube_video"
        assert source.external_id == "youtube-abcdefghijk"
        assert source.metadata_json["source_tree_slug"] == "tim-mackie-test-series"
        assert source.metadata_json["transcript_provenance"] == "youtube_auto_caption"
        assert source.metadata_json["caption_kind"] == "asr"
        assert session.query(Chunk).filter_by(source_id=source.id).count() == 1
        item = json.loads(manifest.read_text())["episodes"][0]
        item_dir = citara_root / "source-artifacts" / "tim-mackie-test-series" / "items" / caption_importer.stable_item_id(item)
        assert json.loads((item_dir / "transcript.raw.json").read_text()) == caption_payload()
        entities = list_source_entities(session, source.id)
        assert sorted((entity["slug"], entity["role"]) for entity in entities) == [
            ("tim-mackie", "teacher"),
            ("tim-mackie-archives", "uploader"),
        ]


def test_foreign_source_item_identity_is_never_reconciled_or_deleted(tmp_path: Path) -> None:
    citara_root = tmp_path / "citara-data"
    caption = tmp_path / "captions" / "video.en.json3"
    caption.parent.mkdir()
    caption.write_text(json.dumps(caption_payload()), encoding="utf-8")
    manifest_path = tmp_path / "approved-channel-manifest.json"
    manifest_data = {
        "schema_version": 1,
        "channel": "Tim Mackie Archives",
        "author": "Tim Mackie",
        "publisher": "Tim Mackie Archives",
        "publisher_slug": "tim-mackie-archives",
        "provider": "youtube",
        "entities": [],
        "episodes": [
            {
                "video_id": "abcdefghijk",
                "guid": "youtube-abcdefghijk",
                "title": "Example",
                "series_title": "Test Series",
                "source_tree_slug": "tim-mackie-test-series",
                "canonical_url": "https://www.youtube.com/watch?v=abcdefghijk",
                "duration_seconds": 10,
                "caption_path": str(caption),
            }
        ],
    }
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
    first = caption_importer.run_import(citara_root=citara_root, manifest_path=manifest_path)
    foreign_id = first[0]["source_id"]
    item = manifest_data["episodes"][0]
    with caption_importer.SessionLocal() as session:
        foreign = session.get(Source, foreign_id)
        assert foreign is not None
        foreign.author = "Foreign Author"
        foreign.metadata_json = {
            **foreign.metadata_json,
            "source_item_id": "FOREIGN-IDENTITY",
            "caption_normalization_version": 1,
        }
        session.commit()

    assert caption_importer.find_existing_source(item) is None
    with pytest.raises(RuntimeError, match="non-importer-owned"):
        caption_importer.reconcile_existing(foreign_id, manifest_data, item, {}, [])
    with pytest.raises(RuntimeError, match="non-importer-owned"):
        caption_importer.replace_stale_caption_source(foreign_id, item)

    second = caption_importer.run_import(citara_root=citara_root, manifest_path=manifest_path)
    assert second[0]["status"] == "imported"
    with caption_importer.SessionLocal() as session:
        sources = session.execute(select(Source)).scalars().all()
        assert len(sources) == 2
        foreign = session.get(Source, foreign_id)
        assert foreign is not None
        assert foreign.author == "Foreign Author"
        assert foreign.metadata_json["source_item_id"] == "FOREIGN-IDENTITY"


@pytest.fixture()
def stale_caption(tmp_path):
    root = tmp_path / "data"
    caption = tmp_path / "captions" / "video.json3"
    caption.parent.mkdir()
    caption.write_text(json.dumps(caption_payload()))
    item = {
        "video_id": "abcdefghijk",
        "guid": "youtube-abcdefghijk",
        "title": "Example",
        "series_title": "Test",
        "source_tree_slug": "test-series",
        "canonical_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "duration_seconds": 10,
        "caption_path": str(caption),
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "channel": "Test",
                "author": "Teacher",
                "publisher": "Test",
                "publisher_slug": "test",
                "provider": "youtube",
                "episodes": [item],
            }
        )
    )
    old_id = caption_importer.run_import(citara_root=root, manifest_path=manifest)[0]["source_id"]
    with caption_importer.SessionLocal() as session:
        old = session.get(Source, old_id)
        old.metadata_json = {**old.metadata_json, "caption_normalization_version": 1}
        old_artifact = Path(old.metadata_json["artifact_uri"]) / "transcript.txt"
        old_artifact.write_text("original artifact")
        old_chunk_ids = [chunk.id for chunk in session.query(Chunk).filter_by(source_id=old_id)]
        session.commit()
    return root, manifest, item, old_id, old_chunk_ids, old_artifact


@pytest.mark.parametrize("failure", ["artifact", "embedding", "ingestion"])
def test_stale_replacement_failure_preserves_old_source(stale_caption, monkeypatch, failure):
    from citara.core.ingestion import transcript

    root, manifest, item, old_id, old_chunk_ids, artifact = stale_caption

    def fail(*args, **kwargs):
        raise RuntimeError("injected failure")

    target, name = {
        "artifact": (caption_importer, "write_item_artifacts"),
        "embedding": (transcript, "embed_chunks"),
        "ingestion": (caption_importer, "add_transcript_source"),
    }[failure]
    with monkeypatch.context() as patch:
        patch.setattr(target, name, fail)
        with pytest.raises(RuntimeError, match="injected failure"):
            caption_importer.run_import(citara_root=root, manifest_path=manifest)
    with caption_importer.SessionLocal() as session:
        assert session.get(Source, old_id) is not None
        assert [chunk.id for chunk in session.query(Chunk).filter_by(source_id=old_id)] == old_chunk_ids
    assert artifact.read_text() == "original artifact"
    result = caption_importer.run_import(citara_root=root, manifest_path=manifest)[0]
    assert result["status"] == "reimported"
    with caption_importer.SessionLocal() as session:
        from citara.core.retrieval.fts import index_row_count

        assert index_row_count(session) == 1
        source = session.execute(select(Source)).scalar_one()
        assert source.id == result["source_id"] != old_id
        assert source.external_id == item["guid"]
        assert source.metadata_json["caption_normalization_version"] == 2


@pytest.mark.parametrize("boundary", ["after_ingestion_commit", "deletion", "promotion_commit"])
def test_replacement_boundary_failure_is_recoverable(stale_caption, monkeypatch, boundary):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    root, manifest, item, old_id, old_chunk_ids, artifact = stale_caption
    real_ingest = caption_importer.add_transcript_source

    def interrupted_ingest(*args, **kwargs):
        real_ingest(*args, **kwargs)
        raise RuntimeError("interrupted after ingestion commit")

    def fail_commit(session):
        if any(isinstance(row, Source) and row.id == old_id for row in session.deleted):
            raise RuntimeError("interrupted promotion commit")

    def fail_delete(*args, **kwargs):
        raise RuntimeError("interrupted deletion")

    with monkeypatch.context() as patch:
        if boundary == "after_ingestion_commit":
            patch.setattr(caption_importer, "add_transcript_source", interrupted_ingest)
        elif boundary == "deletion":
            patch.setattr(Session, "delete", fail_delete)
        else:
            event.listen(Session, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError, match="interrupted"):
                caption_importer.run_import(citara_root=root, manifest_path=manifest)
        finally:
            if boundary == "promotion_commit":
                event.remove(Session, "before_commit", fail_commit)
    with caption_importer.SessionLocal() as session:
        assert session.get(Source, old_id) is not None
        assert [chunk.id for chunk in session.query(Chunk).filter_by(source_id=old_id)] == old_chunk_ids
        candidates = session.query(Source).filter(Source.id != old_id).all()
        assert len(candidates) == 1
        replacement_id = candidates[0].id
        assert candidates[0].metadata_json["caption_replacement_of"] == old_id
    assert artifact.read_text() == "original artifact"

    def no_reingestion(*args, **kwargs):
        pytest.fail("complete replacement must be recovered, not ingested again")

    monkeypatch.setattr(caption_importer, "add_transcript_source", no_reingestion)
    result = caption_importer.run_import(citara_root=root, manifest_path=manifest)[0]
    assert result["source_id"] == replacement_id
    with caption_importer.SessionLocal() as session:
        source = session.execute(select(Source)).scalar_one()
        assert source.id == replacement_id
        assert source.external_id == item["guid"]
        assert source.status == "succeeded"
        assert "caption_replacement_of" not in source.metadata_json
        assert session.query(Chunk).filter_by(source_id=source.id).count() == 1


@pytest.mark.parametrize("corruption", ["failed_source", "missing_chunks", "missing_embeddings", "failed_job"])
def test_persisted_incomplete_replacement_is_not_promoted_and_retry_rebuilds(stale_caption, monkeypatch, corruption):
    from citara.core.models import Embedding, IngestionJob

    root, manifest, item, old_id, old_chunk_ids, artifact = stale_caption
    real_ingest = caption_importer.add_transcript_source
    bad_ids = []

    def persist_incomplete(session, **kwargs):
        source = real_ingest(session, **kwargs)
        bad_ids.append(source.id)
        if corruption == "failed_source":
            source.status = "failed"
        elif corruption == "missing_chunks":
            session.query(Chunk).filter_by(source_id=source.id).delete()
        elif corruption == "missing_embeddings":
            session.query(Embedding).filter_by(source_id=source.id).delete()
        else:
            session.query(IngestionJob).filter_by(source_id=source.id).update({"status": "failed"})
        session.commit()
        return source

    with monkeypatch.context() as patch:
        patch.setattr(caption_importer, "add_transcript_source", persist_incomplete)
        with pytest.raises(RuntimeError, match="incomplete caption replacement"):
            caption_importer.run_import(citara_root=root, manifest_path=manifest)
    with caption_importer.SessionLocal() as session:
        assert session.get(Source, old_id) is not None
        assert [chunk.id for chunk in session.query(Chunk).filter_by(source_id=old_id)] == old_chunk_ids
    assert artifact.read_text() == "original artifact"
    result = caption_importer.run_import(citara_root=root, manifest_path=manifest)[0]
    with caption_importer.SessionLocal() as session:
        source = session.execute(select(Source)).scalar_one()
        assert source.id == result["source_id"]
        assert source.id not in [old_id, *bad_ids]
        assert source.status == "succeeded"
        assert session.query(Chunk).filter_by(source_id=source.id).count() == 1
