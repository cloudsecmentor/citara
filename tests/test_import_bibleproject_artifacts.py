from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "import_bibleproject_artifacts.py"
SPEC = importlib.util.spec_from_file_location("import_bibleproject_artifacts", SCRIPT)
assert SPEC and SPEC.loader
importer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(importer)


def test_segments_from_chunked_normalizes_timestamp_url(tmp_path: Path) -> None:
    path = tmp_path / "e001-oai-raw-chunked.json"
    path.write_text(
        json.dumps(
            [
                {
                    "text": "  The   opening words.  ",
                    "metadata": {
                        "start": "0208",
                        "episode": 1,
                        "url": " https://example.test/episode?t=128 ",
                        "overlap_chars": 17,
                    },
                },
                {
                    "text": "The next words.",
                    "metadata": {
                        "start": "0411",
                        "episode": 1,
                        "url": "https://example.test/episode?t=251",
                    },
                },
            ]
        )
    )

    segments = importer.segments_from_chunked(path)

    assert segments[0]["text"] == "The opening words."
    assert segments[0]["start_ms"] == 128_000
    assert segments[0]["end_ms"] == 251_000
    assert segments[0]["metadata_json"]["timestamp_url"] == "https://example.test/episode?t=128"
    assert segments[0]["metadata_json"]["source_metadata"]["url"] == "https://example.test/episode?t=128"
    assert segments[0]["metadata_json"]["overlap_chars"] == 17


def test_state_by_number_uses_stable_episode_numbers() -> None:
    state = {
        "episodes": {
            "guid-two": {"episode": 2, "title": "Second"},
            "guid-one": {"episode": 1, "title": "First"},
        }
    }

    by_number = importer.state_by_number(state)

    assert by_number[1][0] == "guid-one"
    assert by_number[2][1]["title"] == "Second"


def test_generated_metadata_preserves_provenance_and_episode_identity(tmp_path: Path) -> None:
    metadata = importer.generated_metadata(
        number=7,
        guid="episode-guid",
        episode={"duration_seconds": 3600, "audio_url": "https://audio.test/e7.mp3"},
        item_dir=tmp_path / "item",
        chunked_path=tmp_path / "e007-oai-raw-chunked.json",
    )

    assert metadata["source_tree_slug"] == "bibleproject"
    assert metadata["episode_number"] == 7
    assert metadata["episode_guid"] == "episode-guid"
    assert metadata["transcript_provenance"] == "generated_faster_whisper"
    assert metadata["language"] == "en"
    assert metadata["retrieval_weight"] == 0.9


def test_merge_source_metadata_preserves_existing_fields_and_applies_import_identity() -> None:
    merged = importer.merge_source_metadata(
        {"show_title": "BibleProject", "legacy_note": "keep"},
        {"episode_number": 12, "language": "en", "source_tree_slug": "bibleproject"},
    )

    assert merged["legacy_note"] == "keep"
    assert merged["episode_number"] == 12
    assert merged["language"] == "en"


def test_apply_import_results_updates_pipeline_state_without_losing_transcription_status() -> None:
    state = {
        "episodes": {
            "guid-1": {"episode": 1, "transcription_status": "transcribed"},
            "guid-2": {"episode": 2, "published_status": None},
        }
    }

    importer.apply_import_results_to_state(
        state,
        published=[{"episode": 2, "source_id": "src-pub", "status": "skipped_existing"}],
        generated=[{"episode": 1, "source_id": "src-gen", "status": "imported"}],
    )

    assert state["episodes"]["guid-2"]["published_status"] == "skipped_existing"
    assert state["episodes"]["guid-2"]["source_id"] == "src-pub"
    assert state["episodes"]["guid-1"]["transcription_status"] == "transcribed"
    assert state["episodes"]["guid-1"]["generated_import_status"] == "imported"


def test_citara_root_overrides_ambient_database_url(tmp_path: Path) -> None:
    ambient_db = tmp_path / "ambient.db"
    with sqlite3.connect(ambient_db) as connection:
        connection.execute("CREATE TABLE ambient_sentinel (value TEXT)")
        connection.execute("INSERT INTO ambient_sentinel VALUES ('untouched')")

    citara_root = tmp_path / "custom-citara"
    (citara_root / "import-state").mkdir(parents=True)
    (citara_root / "import-state" / "bibleproject_pipeline_state.json").write_text(json.dumps({"episode_count": 0, "episodes": {}}))
    custom_db = citara_root / "citara.db"
    with sqlite3.connect(custom_db) as connection:
        connection.execute("CREATE TABLE custom_sentinel (value TEXT)")

    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{ambient_db}"
    env["PYTHONPATH"] = str(SCRIPT.parents[1] / "src")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--citara-root", str(citara_root), "--phase", "generated"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    with sqlite3.connect(custom_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone() == (0,)
    with sqlite3.connect(ambient_db) as connection:
        assert connection.execute("SELECT value FROM ambient_sentinel").fetchone() == ("untouched",)
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'sources'").fetchone() == (0,)
    assert list(citara_root.glob("citara.backup-before-bibleproject-import-*.db"))
    assert not list(tmp_path.glob("ambient.backup-before-bibleproject-import-*.db"))


def test_uncertain_swap_cleanup_revalidates_before_deleting_promoted_replacement(tmp_path: Path) -> None:
    citara_root = tmp_path / "citara"
    citara_root.mkdir()
    importer.configure_citara_root(citara_root)
    importer.init_db()
    segments = [{"start_ms": 0, "end_ms": None, "speaker": None, "text": "original", "metadata_json": {}}]
    old_id = importer.import_payload("Episode", "https://example.test/old", segments, {})
    preparation_title = "Episode [prepared replacement race]"
    replacement_id = importer.import_payload(preparation_title, "https://example.test/new", segments, {})

    # Concurrent recovery wins the race and promotes the replacement before
    # uncertain-error cleanup obtains its write lock.
    importer.swap_replacement_source(old_id, replacement_id, "Episode")
    state = importer.resolve_uncertain_swap(old_id, replacement_id, "Episode", preparation_title)

    assert state == "succeeded"
    with importer.SessionLocal() as session:
        assert session.get(importer.Source, old_id) is None
        replacement = session.get(importer.Source, replacement_id)
        assert replacement is not None
        assert replacement.title == "Episode"


def test_post_commit_swap_exception_keeps_promoted_replacement(monkeypatch, tmp_path: Path) -> None:
    citara_root = tmp_path / "citara"
    citara_root.mkdir()
    importer.configure_citara_root(citara_root)
    importer.init_db()
    segments = [{"start_ms": 0, "end_ms": None, "speaker": None, "text": "original", "metadata_json": {}}]
    old_id = importer.import_payload("Episode", "https://example.test/old", segments, {})
    original_swap = importer.swap_replacement_source

    def swap_then_lose_ack(existing_id: str, replacement_id: str, title: str) -> None:
        original_swap(existing_id, replacement_id, title)
        raise RuntimeError("commit acknowledgement lost")

    monkeypatch.setattr(importer, "swap_replacement_source", swap_then_lose_ack)

    replacement_id = importer.replace_payload(
        old_id,
        "Episode",
        "https://example.test/new",
        [{**segments[0], "text": "replacement"}],
        {},
    )

    with importer.SessionLocal() as session:
        assert session.get(importer.Source, old_id) is None
        replacement = session.get(importer.Source, replacement_id)
        assert replacement is not None
        assert replacement.title == "Episode"


def test_pre_commit_swap_exception_keeps_old_source_and_removes_prepared_replacement(monkeypatch, tmp_path: Path) -> None:
    citara_root = tmp_path / "citara"
    citara_root.mkdir()
    importer.configure_citara_root(citara_root)
    importer.init_db()
    segments = [{"start_ms": 0, "end_ms": None, "speaker": None, "text": "original", "metadata_json": {}}]
    old_id = importer.import_payload("Episode", "https://example.test/old", segments, {})

    def fail_before_swap(*_args) -> None:
        raise RuntimeError("swap failed before commit")

    monkeypatch.setattr(importer, "swap_replacement_source", fail_before_swap)

    with pytest.raises(RuntimeError, match="swap failed before commit"):
        importer.replace_payload(
            old_id,
            "Episode",
            "https://example.test/new",
            [{**segments[0], "text": "replacement"}],
            {},
        )

    with importer.SessionLocal() as session:
        old_source = session.get(importer.Source, old_id)
        assert old_source is not None
        assert old_source.title == "Episode"
        assert session.query(importer.Source).count() == 1


def test_generated_replacement_failure_preserves_existing_source(monkeypatch, tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    (raw_root / "e001-oai-raw-chunked.json").write_text(json.dumps([{"text": "A valid prepared segment", "metadata": {"start": "0000"}}]))
    state = {
        "episodes": {
            "guid-1": {
                "episode": 1,
                "title": "Existing episode",
                "episode_url": "https://example.test/1",
                "has_published_transcript": False,
            }
        }
    }
    deleted: list[str] = []
    monkeypatch.setattr(importer, "source_exists", lambda _title: "source-old")
    monkeypatch.setattr(importer, "delete_source", deleted.append)
    monkeypatch.setattr(
        importer,
        "import_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("import failed")),
    )

    results = importer.import_generated(
        state=state,
        artifact_root=tmp_path / "artifacts",
        raw_root=raw_root,
        status_path=tmp_path / "status.json",
        replace_existing=True,
        start=None,
        end=None,
        limit=None,
    )

    assert results[0]["status"] == "error"
    assert results[0]["error"] == "import failed"
    assert deleted == []
