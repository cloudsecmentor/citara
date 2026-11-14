from __future__ import annotations

import multiprocessing
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from citara.core import backups


def make_database(path: Path, value: str = "live") -> Path:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES (?)", (value,))
        connection.commit()
    return path


def test_backup_keeps_only_the_newest_generation(tmp_path: Path) -> None:
    database = make_database(tmp_path / "citara.db")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    old_db = backup_dir / "citara-before-import-20260101T010101Z.db"
    old_tar = backup_dir / "bema-source-json-before-metadata-20260101T010101Z.tar.gz"
    old_fractional = backup_dir / "citara-before-youtube-caption-import-20260202T020202.123456Z.db"
    legacy_root = tmp_path / "citara.backup-before-bema-rebuild-20260303030303.db"
    unrelated = backup_dir / "README.txt"
    for path in (old_db, old_tar, old_fractional, legacy_root, unrelated):
        path.write_text("old", encoding="utf-8")

    created = backups.backup_database(database, "test", generation="20261025T000000Z")

    assert created == backup_dir / "citara-before-test-20261025T000000Z.db"
    assert sorted(path.name for path in backup_dir.iterdir()) == sorted([created.name, "README.txt"])
    assert not legacy_root.exists()
    assert database.exists()
    with closing(sqlite3.connect(created)) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone() == ("live",)


def test_companion_files_of_the_kept_generation_survive(tmp_path: Path) -> None:
    database = make_database(tmp_path / "citara.db")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    companion = backup_dir / "artifacts-20261025T000000Z.tar.gz"
    companion.write_text("tar", encoding="utf-8")

    with closing(sqlite3.connect(database)) as connection:
        backups.backup_database(database, "test", source=connection, generation="20261025T000000Z")

    assert companion.exists()


def test_failed_backup_leaves_previous_backup_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = make_database(tmp_path / "citara.db")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    previous = backup_dir / "citara-before-import-20260101T010101Z.db"
    previous.write_text("old", encoding="utf-8")

    class BrokenConnection:
        def backup(self, target: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("disk full")

    with pytest.raises(sqlite3.OperationalError):
        backups.backup_database(database, "test", source=BrokenConnection())  # type: ignore[arg-type]

    assert [path.name for path in backup_dir.iterdir()] == [previous.name]


def test_missing_database_creates_nothing(tmp_path: Path) -> None:
    assert backups.backup_database(tmp_path / "citara.db", "test") is None
    assert not (tmp_path / "backups").exists()


def _blocked_writer(database: Path, entered: Any, release: Any, phase: str) -> None:
    prune = backups._prune_backups

    def paused_prune(database: Path, *, keep_generation: str) -> list[Path]:
        entered.set()
        assert release.wait(10)
        return prune(database, keep_generation=keep_generation)

    if phase == "prune":
        backups._prune_backups = paused_prune

    class PausedSource:
        def backup(self, target: sqlite3.Connection) -> None:
            if phase == "snapshot":
                entered.set()
                assert release.wait(10)
            with closing(sqlite3.connect(database)) as live:
                live.backup(target)

    backups.backup_database(database, "writer", source=PausedSource(), generation="20260101T010101Z")  # type: ignore[arg-type]


def _following_writer(database: Path, started: Any, generated: Any) -> None:
    def generation() -> str:
        generated.set()
        return "20260202T020202Z"

    backups.new_generation = generation
    started.set()
    backups.backup_database(database, "writer")


@pytest.mark.parametrize("phase", ["snapshot", "prune"])
@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="POSIX process regression")
def test_concurrent_writer_waits_before_timestamp_and_retention(tmp_path: Path, phase: str) -> None:
    database = make_database(tmp_path / "citara.db")
    context = multiprocessing.get_context("fork")
    entered, release, started, generated = [context.Event() for _ in range(4)]
    first = context.Process(target=_blocked_writer, args=(database, entered, release, phase))
    second = context.Process(target=_following_writer, args=(database, started, generated))
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        assert started.wait(5)
        assert not generated.wait(0.3), "timestamp generated before acquiring publication lock"
    finally:
        release.set()
        first.join(10)
        if second.pid is not None:
            second.join(10)
        for process in (first, second):
            if process.is_alive():
                process.kill()
                process.join()
    assert first.exitcode == second.exitcode == 0
    paths = list((tmp_path / "backups").glob("*.db"))
    assert [path.name for path in paths] == ["citara-before-writer-20260202T020202Z.db"]
    with closing(sqlite3.connect(paths[0])) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.parametrize("failure", ["integrity", "publication"])
def test_failed_replacement_preserves_previous_good_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    database = make_database(tmp_path / "citara.db")
    previous = backups.backup_database(database, "test", generation="20260101T010101Z")
    assert previous is not None
    original = previous.read_bytes()
    abandoned = previous.with_name(previous.name + ".partial")
    abandoned.write_text("another writer's temporary file")
    connect = sqlite3.connect

    class InvalidSnapshot(sqlite3.Connection):
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            if sql == "PRAGMA integrity_check":
                return super().execute("SELECT 'corrupt'")
            return super().execute(sql, *args, **kwargs)

    def target_connect(path: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if str(path).endswith(".partial"):
            assert Path(path) != abandoned
            kwargs["factory"] = InvalidSnapshot
        return connect(path, *args, **kwargs)

    def fail_replace(self: Path, target: Path) -> Path:
        assert self != abandoned
        raise OSError("publication failed")

    if failure == "integrity":
        monkeypatch.setattr(sqlite3, "connect", target_connect)
    else:
        monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises((RuntimeError, OSError)):
        backups.backup_database(database, "test", generation="20260101T010101Z")
    assert previous.read_bytes() == original
    assert abandoned.read_text() == "another writer's temporary file"
    assert set(previous.parent.iterdir()) == {previous, abandoned}


def test_stale_pruner_cannot_remove_the_only_remaining_generation(tmp_path: Path) -> None:
    database = make_database(tmp_path / "citara.db")
    current = backups.backup_database(database, "test", generation="20260202T020202Z")
    assert backups.prune_backups(database, keep_generation="20260101T010101Z") == []
    assert current is not None and current.exists()
