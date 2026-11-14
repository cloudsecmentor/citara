from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from citara.core import backups

SCRIPT = Path(__file__).parents[1] / "skills" / "maintenance" / "citara-backup-cleanup" / "scripts" / "cleanup_backups.py"
spec = importlib.util.spec_from_file_location("cleanup_backups", SCRIPT)
assert spec and spec.loader
cleanup_backups = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup_backups)


def touch_backup(root: Path, name: str) -> Path:
    path = root / name
    path.write_text(name, encoding="utf-8")
    return path


def test_cleanup_keeps_three_complete_generations(tmp_path: Path) -> None:
    generations = [
        "20260101T010101Z",
        "20260202T020202Z",
        "20260303T030303Z",
        "20260404T040404Z",
    ]
    for generation in generations:
        touch_backup(tmp_path, f"citara-{generation}.db")
        touch_backup(tmp_path, f"artifacts-{generation}.tar.gz")
    touch_backup(tmp_path, "README.txt")

    plan = cleanup_backups.plan_cleanup(tmp_path, keep=3)

    assert plan["kept_generations"] == list(reversed(generations[1:]))
    assert plan["delete_generations"] == [generations[0]]
    assert {path.name for path in plan["delete_paths"]} == {
        f"citara-{generations[0]}.db",
        f"artifacts-{generations[0]}.tar.gz",
    }
    assert plan["ignored_files"] == ["README.txt"]


def test_unknown_files_and_symlinks_are_never_eligible(tmp_path: Path) -> None:
    target = touch_backup(tmp_path, "citara-20260101T010101Z.db")
    touch_backup(tmp_path, "notes-20250101T010101Z.txt")
    symlink = tmp_path / "citara-20240101T010101Z.sqlite"
    symlink.symlink_to(target)

    plan = cleanup_backups.plan_cleanup(tmp_path, keep=3)

    assert plan["recognized_generations"] == ["20260101T010101Z"]
    assert set(plan["ignored_files"]) == {
        "citara-20240101T010101Z.sqlite",
        "notes-20250101T010101Z.txt",
    }


def test_fractional_second_backups_are_recognized(tmp_path: Path) -> None:
    touch_backup(tmp_path, "citara-before-youtube-caption-import-20260101T010101.123456Z.db")

    plan = cleanup_backups.plan_cleanup(tmp_path, keep=1)

    assert plan["recognized_generations"] == ["20260101T010101.123456Z"]


def test_keep_must_be_positive(tmp_path: Path) -> None:
    try:
        cleanup_backups.plan_cleanup(tmp_path, keep=0)
    except ValueError as error:
        assert str(error) == "--keep must be at least 1"
    else:
        raise AssertionError("Expected ValueError")


def test_mixed_precision_generations_sort_chronologically(tmp_path: Path) -> None:
    stamps = ["20260101T010101Z", "20260101T010101.9Z", "20260101T010101.123456Z"]
    for stamp in stamps:
        touch_backup(tmp_path, f"citara-{stamp}.db")
    plan = cleanup_backups.plan_cleanup(tmp_path, keep=1)
    assert plan["recognized_generations"] == [stamps[1], stamps[2], stamps[0]]
    assert plan["kept_generations"] == [stamps[1]]


@pytest.mark.parametrize("database_name", ["citara.db", "custom.sqlite"])
def test_cleanup_waits_for_writer_and_replans_under_same_lock(tmp_path: Path, database_name: str) -> None:
    directory = tmp_path / "backups"
    directory.mkdir()
    touch_backup(directory, "citara-20260101T010101Z.db")
    touch_backup(directory, "citara-20260202T020202Z.db")
    database = tmp_path / database_name
    command = [sys.executable, str(SCRIPT), "--backup-dir", str(directory)]
    if database_name != "citara.db":
        command.extend(["--database", str(database)])
    process = None
    try:
        with backups.backup_lock(database):
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            with pytest.raises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.3)
            touch_backup(directory, "citara-20260303T030303Z.db")
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
    assert process is not None
    assert process.returncode == 0, stderr
    assert json.loads(stdout)["kept_generations"] == ["20260303T030303Z"]
    assert [path.name for path in directory.glob("*.db")] == ["citara-20260303T030303Z.db"]
