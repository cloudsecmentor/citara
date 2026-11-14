from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("bema_backfill", Path(__file__).parents[1] / "scripts/backfill_bema_episode_metadata.py")
assert SPEC is not None and SPEC.loader is not None
backfill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backfill)


def seed(root: Path, metadata: dict | None = None, artifact: dict | None = None) -> Path:
    root.mkdir()
    db = root / "citara.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE sources (id TEXT PRIMARY KEY, title TEXT, metadata_json TEXT, updated_at TEXT)")
        connection.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?)",
            ("one", "BEMA 35: Test", json.dumps(metadata or {"source_tree_slug": "bema", "source_item_id": "one"}), "old"),
        )
    if artifact is not None:
        path = root / "source-artifacts/bema/items/one/source.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(artifact), encoding="utf-8")
    return db


def invoke(monkeypatch, root: Path, *, apply: bool = False) -> None:
    monkeypatch.setattr(sys, "argv", ["backfill", "--citara-root", str(root), *(["--apply"] if apply else [])])
    backfill.main()


def read_metadata(db: Path) -> dict:
    with sqlite3.connect(db) as connection:
        return json.loads(connection.execute("SELECT metadata_json FROM sources").fetchone()[0])


def test_dry_run_targets_explicit_root_read_only(tmp_path, monkeypatch, capsys):
    root = tmp_path / "selected"
    db = seed(root)
    before = db.read_bytes()
    real_connect = sqlite3.connect
    calls = []

    def connect(database, *args, **kwargs):
        calls.append((str(database), kwargs))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(backfill.sqlite3, "connect", connect)
    monkeypatch.setenv("CITARA_DATA_ROOT", str(tmp_path / "ignored"))
    invoke(monkeypatch, root)
    assert str(db.resolve()) in capsys.readouterr().out
    assert all("mode=ro" in target and options.get("uri") for target, options in calls)
    assert db.read_bytes() == before
    assert not (root / "backups").exists()


@pytest.mark.parametrize("fields", [{}, {"episode_number": None, "season": None}, {"episode_number": 35, "season": 2}])
def test_apply_repairs_missing_null_numeric_metadata(tmp_path, monkeypatch, fields):
    root = tmp_path / "selected"
    metadata = {"source_tree_slug": "bema", "source_item_id": "one", "unrelated": "keep", **fields}
    db = seed(root, metadata, {"episode_number": None, "season": None, "other": [1]})
    invoke(monkeypatch, root, apply=True)
    assert read_metadata(db) == {**metadata, "episode_number": 35, "season": "2"}
    assert json.loads((root / "source-artifacts/bema/items/one/source.json").read_text()) == {
        "episode_number": 35,
        "season": "2",
        "other": [1],
    }


def test_rereads_concurrent_metadata_under_write_lock(tmp_path, monkeypatch):
    root = tmp_path / "selected"
    db = seed(root)
    original_backup = backfill.backup_database

    def backup(*args, **kwargs):
        result = original_backup(*args, **kwargs)
        metadata = read_metadata(db)
        metadata["concurrent"] = {"keep": True}
        metadata["season"] = 3
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE sources SET metadata_json = ?", (json.dumps(metadata),))
        return result

    monkeypatch.setattr(backfill, "backup_database", backup)
    invoke(monkeypatch, root, apply=True)
    assert read_metadata(db)["concurrent"] == {"keep": True}
    assert read_metadata(db)["season"] == "3"


def test_artifact_write_failure_is_repaired_on_rerun(tmp_path, monkeypatch):
    root = tmp_path / "selected"
    db = seed(root, artifact={"episode_number": None, "season": None})
    path = root / "source-artifacts/bema/items/one/source.json"
    raw = path.read_bytes()
    original_write = backfill.atomic_write_json

    def fail(*args):
        raise OSError("injected disk failure")

    monkeypatch.setattr(backfill, "atomic_write_json", fail)
    with pytest.raises(OSError, match="injected"):
        invoke(monkeypatch, root, apply=True)
    assert read_metadata(db)["season"] == "2"
    assert path.read_bytes() == raw
    monkeypatch.setattr(backfill, "atomic_write_json", original_write)
    invoke(monkeypatch, root, apply=True)
    assert json.loads(path.read_text()) == {"episode_number": 35, "season": "2"}
    after = path.stat().st_mtime_ns
    invoke(monkeypatch, root, apply=True)
    assert path.stat().st_mtime_ns == after


def test_malformed_artifact_aborts_before_database_commit(tmp_path, monkeypatch):
    root = tmp_path / "selected"
    db = seed(root, artifact={})
    path = root / "source-artifacts/bema/items/one/source.json"
    path.write_text("{bad json")
    before = read_metadata(db)
    with pytest.raises(ValueError):
        invoke(monkeypatch, root, apply=True)
    assert read_metadata(db) == before


@pytest.mark.parametrize(
    "fields",
    [
        {"episode_number": -2},
        {"episode_number": 508},
        {"episode_number": True},
        {"episode_number": "35"},
        {"episode_number": 1.5},
        {"season": "None"},
        {"season": 0},
        {"season": 11},
        {"season": False},
        {"season": 2.5},
    ],
)
def test_invalid_metadata_aborts_without_writes(tmp_path, monkeypatch, fields):
    root = tmp_path / "selected"
    db = seed(root, {"source_tree_slug": "bema", **fields})
    before = db.read_bytes()
    with pytest.raises(ValueError):
        invoke(monkeypatch, root, apply=True)
    assert db.read_bytes() == before
    assert not (root / "backups").exists()


@pytest.mark.parametrize("item_id", ["../escape", "/absolute", "one/two", "..", "", 42])
def test_unsafe_artifact_ids_are_rejected(tmp_path, monkeypatch, item_id):
    root = tmp_path / "selected"
    db = seed(root, {"source_tree_slug": "bema", "source_item_id": item_id})
    before = db.read_bytes()
    with pytest.raises(ValueError):
        invoke(monkeypatch, root, apply=True)
    assert db.read_bytes() == before
    assert not (root / "backups").exists()


@pytest.mark.parametrize("escape", ["items", "item", "manifest", "database", "backups"])
def test_symlink_escape_is_rejected(tmp_path, monkeypatch, escape):
    root = tmp_path / "selected"
    db = seed(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "source.json"
    outside_file.write_text("{}")
    items = root / "source-artifacts/bema/items"
    if escape == "database":
        target = outside / "citara.db"
        db.replace(target)
        db.symlink_to(target)
    elif escape == "backups":
        (root / "backups").symlink_to(outside, target_is_directory=True)
    elif escape == "items":
        items.parent.mkdir(parents=True)
        items.symlink_to(outside, target_is_directory=True)
    else:
        items.mkdir(parents=True)
        if escape == "item":
            (items / "one").symlink_to(outside, target_is_directory=True)
        else:
            (items / "one").mkdir()
            (items / "one/source.json").symlink_to(outside_file)
    with pytest.raises(ValueError):
        invoke(monkeypatch, root, apply=True)
    assert outside_file.read_text() == "{}"


def test_atomic_json_does_not_follow_predictable_temp_symlink(tmp_path):
    path = tmp_path / "source.json"
    path.write_text("{}")
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    path.with_suffix(".json.tmp").symlink_to(outside)
    backfill.atomic_write_json(path, {"season": "2"})
    assert outside.read_text() == "preserve"
    assert json.loads(path.read_text()) == {"season": "2"}


def test_archive_failure_never_publishes_partial_backup(tmp_path, monkeypatch):
    root = tmp_path / "selected"
    db = seed(root, artifact={"other": "raw"})
    before = read_metadata(db)

    def fail(*args, **kwargs):
        raise OSError("archive failure")

    monkeypatch.setattr(backfill.tarfile.TarFile, "addfile", fail)
    with pytest.raises(OSError, match="archive failure"):
        invoke(monkeypatch, root, apply=True)
    assert read_metadata(db) == before
    assert not list((root / "backups").glob("*.tar.gz"))


def test_raw_artifacts_and_database_are_backed_up(tmp_path, monkeypatch):
    root = tmp_path / "selected"
    db = seed(root, artifact={})
    path = root / "source-artifacts/bema/items/one/source.json"
    raw = b'{ "other": [1], "season": null }\n'
    path.write_bytes(raw)
    before = read_metadata(db)
    invoke(monkeypatch, root, apply=True)
    backups = root / "backups"
    with backfill.tarfile.open(next(backups.glob("*.tar.gz"))) as archive:
        stream = archive.extractfile("items/one/source.json")
        assert stream is not None and stream.read() == raw
    assert read_metadata(next(backups.glob("*.db"))) == before


def test_pruned_backup_aborts_before_mutation(tmp_path, monkeypatch):
    root = tmp_path / "selected"
    db = seed(root, artifact={})
    before = read_metadata(db)
    original = backfill.backup_database

    def backup(*args, **kwargs):
        result = original(*args, **kwargs)
        result.unlink()  # A newer generation won before we acquired the lock.
        return result

    monkeypatch.setattr(backfill, "backup_database", backup)
    with pytest.raises(RuntimeError, match="backup"):
        invoke(monkeypatch, root, apply=True)
    assert read_metadata(db) == before


def test_explicit_root_is_required_before_opening_database(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["backfill", "--apply"])
    monkeypatch.setenv("CITARA_DATA_ROOT", str(tmp_path))
    with pytest.raises(SystemExit) as error:
        backfill.main()
    assert error.value.code == 2
    assert not list(tmp_path.iterdir())


def test_apply_never_mutates_ambient_root(tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    other = tmp_path / "other"
    seed(selected, artifact={})
    other_db = seed(other, artifact={})
    other_manifest = other / "source-artifacts/bema/items/one/source.json"
    before = (other_db.read_bytes(), other_manifest.read_bytes())
    monkeypatch.setenv("CITARA_DATA_ROOT", str(other))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(other_db))
    invoke(monkeypatch, selected, apply=True)
    assert (other_db.read_bytes(), other_manifest.read_bytes()) == before
    assert not (other / "backups").exists()


@pytest.mark.parametrize("episode, season", [(-1, "1"), (32, "1"), (33, "2"), (72, "2"), (73, "3"), (507, "10")])
def test_existing_season_bounds(episode, season):
    assert backfill.season_for_episode(episode) == season


@pytest.mark.parametrize("apply", [False, True])
def test_missing_database_is_never_created(tmp_path, monkeypatch, apply):
    root = tmp_path / "missing"
    with pytest.raises((FileNotFoundError, sqlite3.OperationalError)):
        invoke(monkeypatch, root, apply=apply)
    assert not root.exists()
