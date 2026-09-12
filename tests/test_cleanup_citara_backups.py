from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "maintenance"
    / "citara-backup-cleanup"
    / "scripts"
    / "cleanup_backups.py"
)
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


def test_keep_must_be_positive(tmp_path: Path) -> None:
    try:
        cleanup_backups.plan_cleanup(tmp_path, keep=0)
    except ValueError as error:
        assert str(error) == "--keep must be at least 1"
    else:
        raise AssertionError("Expected ValueError")
