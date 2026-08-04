from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select

from citara.core.entities import list_source_entities
from citara.core.models import Chunk, Entity, Source, SourceEntity, TranscriptSegment

SCRIPT = Path(__file__).parents[1] / "scripts" / "import_podcast_remote_artifacts.py"
SPEC = importlib.util.spec_from_file_location("import_podcast_remote_artifacts", SCRIPT)
assert SPEC and SPEC.loader
importer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(importer)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def episode(*, number: int = 1, guid: str = "Buzzsprout-123") -> dict:
    return {
        "queue_number": number,
        "episode_label": "S4E1",
        "guid": guid,
        "title": "The Episode",
        "audio_url": "https://audio.example/episode.mp3",
        "canonical_url": "https://example.test/episode",
        "duration_seconds": 42,
        "artifact_stem": f"q{number:03d}-buzzsprout-123-s4e1",
    }


def make_triplet(remote_root: Path, item: dict, *, complete: bool = True) -> None:
    stem = item["artifact_stem"]
    write_json(
        remote_root / f"{stem}-oai-raw.json",
        {
            "text": "First sentence. Second sentence.",
            "segments": [
                {
                    "start": 1.25,
                    "end": 3.0,
                    "text": "First sentence.",
                    "words": [{"start": 1.25, "end": 1.5, "word": " First"}],
                }
            ],
        },
    )
    write_json(
        remote_root / f"{stem}-oai-raw-chunked.json",
        [
            {
                "text": " First   sentence. ",
                "metadata": {
                    "start": "0001",
                    "episode": item["queue_number"],
                    "url": f" {item['canonical_url']}?t=1 ",
                    "overlap_chars": 0,
                },
            },
            {
                "text": "Second sentence.",
                "metadata": {
                    "start": "0003",
                    "episode": item["queue_number"],
                    "url": f"{item['canonical_url']}?t=3",
                    "overlap_chars": 0,
                },
            },
        ],
    )
    write_json(
        remote_root / f"{stem}-transcribe-stats.json",
        {
            "complete": complete,
            "artifact_stem": stem,
            "queue_number": item["queue_number"],
            "episode_label": item["episode_label"],
            "guid": item["guid"],
            "canonical_url": item["canonical_url"],
            "model": "medium",
            "device": "cpu",
            "compute_type": "int8",
        },
    )


def setup_roots(tmp_path: Path, *, include_triplet: bool = True) -> tuple[Path, Path, Path, dict]:
    citara_root = tmp_path / "citara-data"
    remote_root = citara_root / "source-artifacts" / "a-book-like-no-other" / "remote-openai"
    item = episode()
    manifest = remote_root / "approved-queue.json"
    write_json(
        manifest,
        {
            "schema_version": 1,
            "corpus_slug": "a-book-like-no-other",
            "remote_namespace": "a-book-like-no-other-approved",
            "episodes": [item],
        },
    )
    state = {
        "episode_count": 99,
        "unrelated_top_level": {"keep": True},
        "feed_url": importer.FEED_URL,
        "apple_podcast_id": importer.APPLE_PODCAST_ID,
        "episodes": {item["guid"]: {"published_status": "missing_transcript", "unrelated": "keep"}},
    }
    state_path = citara_root / "import-state" / "a-book-like-no-other_pipeline_state.json"
    write_json(state_path, state)
    if include_triplet:
        make_triplet(remote_root, item)
    return citara_root, manifest, state_path, item


def test_manifest_mapping_ignores_unlisted_artifacts_and_incomplete_triplets(tmp_path: Path) -> None:
    root, manifest_path, _state_path, item = setup_roots(tmp_path)
    extra = episode(number=2, guid="Buzzsprout-unlisted")
    make_triplet(manifest_path.parent, extra)

    manifest = importer.load_manifest(manifest_path)
    candidates = importer.load_candidates(manifest, manifest_path.parent)

    assert [candidate.item["guid"] for candidate in candidates] == [item["guid"]]
    assert candidates[0].complete is True

    (manifest_path.parent / f"{item['artifact_stem']}-oai-raw.json").unlink()
    incomplete = importer.load_candidates(manifest, manifest_path.parent)
    assert incomplete[0].complete is False
    assert "missing" in incomplete[0].reason
    assert root.exists()


def test_segments_use_chunked_sentence_aware_artifact_and_first_word_anchor(tmp_path: Path) -> None:
    _root, manifest_path, _state_path, item = setup_roots(tmp_path)
    candidate = importer.load_candidates(importer.load_manifest(manifest_path), manifest_path.parent)[0]

    segments = importer.segments_from_candidate(candidate)

    assert [segment["text"] for segment in segments] == ["First sentence.", "Second sentence."]
    assert segments[0]["start_ms"] == 1_250
    assert segments[0]["end_ms"] == 3_000
    assert segments[0]["metadata_json"]["timestamp_url"] == f"{item['canonical_url']}?t=1"


def test_import_is_idempotent_and_propagates_metadata_entities_artifacts_and_state(tmp_path: Path) -> None:
    root, manifest_path, state_path, item = setup_roots(tmp_path)
    importer.configure_citara_root(root)
    importer.init_db()

    first = importer.run_import(citara_root=root, manifest_path=manifest_path)
    with importer.SessionLocal() as session:
        source = session.execute(select(Source)).scalar_one()
        source.author = None
        source.provider = None
        source.external_id = None
        source.metadata_json = {key: value for key, value in source.metadata_json.items() if key != "publisher"}
        for chunk in session.query(Chunk).filter_by(source_id=source.id).all():
            chunk.metadata_json = {key: value for key, value in chunk.metadata_json.items() if key != "publisher"}
        for segment in session.query(TranscriptSegment).filter_by(source_id=source.id).all():
            segment.metadata_json = {key: value for key, value in segment.metadata_json.items() if key != "publisher"}
        fohrman = session.execute(select(Entity).where(Entity.slug == "david-fohrman")).scalar_one()
        session.query(SourceEntity).filter_by(source_id=source.id, entity_id=fohrman.id).delete()
        session.add(source)
        session.commit()
    second = importer.run_import(citara_root=root, manifest_path=manifest_path)

    assert [row["status"] for row in first] == ["imported"]
    assert [row["status"] for row in second] == ["skipped_existing"]
    with importer.SessionLocal() as session:
        sources = session.execute(select(Source)).scalars().all()
        assert len(sources) == 1
        source = sources[0]
        expected_title = "A Book Like No Other: The Episode (Generated Transcript)"
        assert source.title == expected_title
        assert source.canonical_url == item["canonical_url"]
        assert source.language == "en"
        assert source.author == "Aleph Beta"
        assert source.provider == "buzzsprout"
        assert source.external_id == item["guid"]
        metadata = source.metadata_json
        assert metadata["show_title"] == "A Book Like No Other"
        assert metadata["acronym"] == "BLNO"
        assert metadata["aliases"] == ["BLNO"]
        assert metadata["publisher"] == "Aleph Beta"
        assert metadata["publisher_slug"] == "aleph-beta"
        assert "blno" in metadata["tags"]
        assert "rabbi-fohrman" in metadata["tags"]
        assert metadata["tags"] == importer.TAGS
        assert metadata["queue_number"] == 1
        assert metadata["episode_label"] == "S4E1"
        assert metadata["guid"] == item["guid"]
        assert metadata["episode_guid"] == item["guid"]
        assert metadata["audio_url"] == item["audio_url"]
        assert metadata["feed_url"] == importer.FEED_URL
        assert metadata["apple_podcast_id"] == importer.APPLE_PODCAST_ID
        assert metadata["apple_id"] == importer.APPLE_PODCAST_ID
        assert metadata["model"] == "medium"
        assert metadata["transcription_model"] == "medium"
        assert metadata["transcription_device"] == "cpu"
        assert metadata["transcription_compute_type"] == "int8"
        assert metadata["transcript_provenance"] == "generated_faster_whisper_remote"
        assert metadata["provenance"] == "generated_faster_whisper_remote"
        assert metadata["retrieval_weight"] == 0.9
        chunks = session.query(Chunk).filter_by(source_id=source.id).all()
        segments = session.query(TranscriptSegment).filter_by(source_id=source.id).all()
        assert chunks and segments
        assert all(chunk.metadata_json["episode_guid"] == item["guid"] for chunk in chunks)
        assert all(chunk.metadata_json["publisher"] == "Aleph Beta" for chunk in chunks)
        assert all(segment.metadata_json["tags"] == importer.TAGS for segment in segments)
        assert all(segment.metadata_json["publisher"] == "Aleph Beta" for segment in segments)
        entities = list_source_entities(session, source.id)
        assert sorted((entity["slug"], entity["role"]) for entity in entities) == [
            ("aleph-beta", "publisher"),
            ("david-fohrman", "teacher"),
        ]

    item_dir = root / "source-artifacts" / "a-book-like-no-other" / "items" / importer.stable_item_id(item)
    assert sorted(path.name for path in item_dir.iterdir()) == [
        "import-payload.json",
        "source.json",
        "transcript.normalized.json",
        "transcript.txt",
    ]
    payload = json.loads((item_dir / "import-payload.json").read_text())
    assert payload["episode_url"] == item["canonical_url"]
    assert payload["metadata_json"]["episode_guid"] == item["guid"]
    assert (item_dir / "transcript.txt").read_text() == "First sentence.\nSecond sentence.\n"

    state = json.loads(state_path.read_text())
    assert state["episode_count"] == 99
    assert state["unrelated_top_level"] == {"keep": True}
    assert state["episodes"][item["guid"]]["unrelated"] == "keep"
    assert state["episodes"][item["guid"]]["generated_import_status"] == "skipped_existing"
    assert state["episodes"][item["guid"]]["generated_source_id"] == source.id


def test_incomplete_triplet_is_the_only_kind_skipped_without_import(tmp_path: Path) -> None:
    root, manifest_path, state_path, item = setup_roots(tmp_path)
    make_triplet(manifest_path.parent, item, complete=False)
    importer.configure_citara_root(root)
    importer.init_db()

    results = importer.run_import(citara_root=root, manifest_path=manifest_path)

    assert results[0]["status"] == "skipped_incomplete"
    with importer.SessionLocal() as session:
        assert session.query(Source).count() == 0
    state = json.loads(state_path.read_text())
    assert state["episodes"][item["guid"]]["unrelated"] == "keep"
    assert state["episodes"][item["guid"]]["generated_import_status"] == "skipped_incomplete"


def test_existing_published_source_is_never_deleted_or_replaced(tmp_path: Path) -> None:
    root, manifest_path, _state_path, _item = setup_roots(tmp_path)
    importer.configure_citara_root(root)
    importer.init_db()
    published = importer.import_payload(
        title="A Book Like No Other: S6 Ep. 1: Existing (Published Transcript)",
        canonical_url="https://example.test/s6e1",
        segments=[{"start_ms": 0, "end_ms": None, "speaker": None, "text": "published", "metadata_json": {}}],
        metadata={"episode_label": "S6E1", "transcript_provenance": "published_transcript"},
    )

    importer.run_import(citara_root=root, manifest_path=manifest_path)

    with importer.SessionLocal() as session:
        preserved = session.get(Source, published)
        assert preserved is not None
        assert preserved.title.endswith("(Published Transcript)")
        assert preserved.metadata_json["transcript_provenance"] == "published_transcript"
        assert session.query(Source).count() == 2


def test_cli_binds_to_citara_root_backs_up_db_and_ignores_ambient_database_url(tmp_path: Path) -> None:
    root, manifest_path, _state_path, _item = setup_roots(tmp_path)
    ambient_db = tmp_path / "ambient.db"
    with sqlite3.connect(ambient_db) as connection:
        connection.execute("CREATE TABLE ambient_sentinel (value TEXT)")
        connection.execute("INSERT INTO ambient_sentinel VALUES ('untouched')")
    root.mkdir(exist_ok=True)
    custom_db = root / "citara.db"
    with sqlite3.connect(custom_db) as connection:
        connection.execute("CREATE TABLE custom_sentinel (value TEXT)")

    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{ambient_db}"
    env["PYTHONPATH"] = str(SCRIPT.parents[1] / "src")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--citara-root", str(root), "--manifest", str(manifest_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert '"imported": 1' in completed.stdout
    with sqlite3.connect(custom_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone() == (1,)
    with sqlite3.connect(ambient_db) as connection:
        assert connection.execute("SELECT value FROM ambient_sentinel").fetchone() == ("untouched",)
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'sources'").fetchone() == (0,)
    backups = list(root.glob("citara.backup-before-podcast-artifact-import-*.db"))
    assert len(backups) == 1


def test_manifest_can_configure_another_podcast_corpus(tmp_path: Path) -> None:
    manifest_path = tmp_path / "approved-queue.json"
    item = episode(guid="tag:soundcloud,2010:tracks/123")
    write_json(
        manifest_path,
        {
            "schema_version": 1,
            "corpus_slug": "parsha-lab",
            "remote_namespace": "parsha-lab-approved",
            "show_title": "Parsha Lab from Aleph Beta",
            "feed_url": "https://feeds.soundcloud.example/rss",
            "apple_podcast_id": "1331532911",
            "publisher": "Aleph Beta",
            "provider": "soundcloud",
            "acronym": "PL",
            "aliases": ["Parsha Lab"],
            "tags": ["aleph-beta", "parsha-lab"],
            "entities": [
                {"type": "organization", "slug": "aleph-beta", "label": "Aleph Beta", "role": "publisher"}
            ],
            "episodes": [item],
        },
    )

    manifest = importer.load_manifest(manifest_path)

    assert manifest["corpus_slug"] == "parsha-lab"
    assert importer.CORPUS_SLUG == "parsha-lab"
    assert importer.SHOW_TITLE == "Parsha Lab from Aleph Beta"
    assert importer.PROVIDER == "soundcloud"
    assert importer.TAGS == ["aleph-beta", "parsha-lab"]
