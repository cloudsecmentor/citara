#!/usr/bin/env python3
"""Prune old timestamped Citara backup generations."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

TIMESTAMP_RE = re.compile(r"(?<!\d)(\d{8}T\d{6}(?:\.\d+)?Z)(?!\d)")
RECOGNIZED_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".tar.gz", ".tgz", ".zip")


@contextmanager
def backup_lock(database: Path) -> Iterator[None]:
    """Serialize snapshot publication and retention for a resolved database path.

    Keep this persistent lock file: unlinking it could create two independent locks.
    The standalone cleanup utility implements the same on-disk protocol.
    """
    database = database.expanduser().resolve()
    with (database.parent / f".{database.name}.backup.lock").open("a+b") as lock:
        if sys.platform == "win32":
            import msvcrt

            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def default_backup_dir() -> Path:
    data_root = Path(os.environ.get("CITARA_DATA_ROOT", Path.home() / "code" / "citara-data"))
    return data_root / "backups"


def backup_generation(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    if not path.name.endswith(RECOGNIZED_SUFFIXES):
        return None
    match = TIMESTAMP_RE.search(path.name)
    return match.group(1) if match else None


def plan_cleanup(backup_dir: Path, keep: int) -> dict:
    if keep < 1:
        raise ValueError("--keep must be at least 1")
    if not backup_dir.is_dir():
        raise FileNotFoundError(f"Backup directory does not exist: {backup_dir}")

    generations: dict[str, list[Path]] = defaultdict(list)
    ignored: list[str] = []
    for path in sorted(backup_dir.iterdir(), key=lambda item: item.name):
        generation = backup_generation(path)
        if generation is None:
            ignored.append(path.name)
        else:
            generations[generation].append(path)

    # An integral-second Z sorts above '.', despite being earlier in time. Compare
    # normalized fractional digits separately (without losing sub-microsecond precision).
    def chronological_key(generation: str) -> tuple[str, str, str]:
        whole, _, fraction = generation.removesuffix("Z").partition(".")
        return whole, fraction.rstrip("0"), generation

    newest_first = sorted(generations, key=chronological_key, reverse=True)
    kept = newest_first[:keep]
    deleted = newest_first[keep:]
    delete_paths = [path for generation in deleted for path in generations[generation]]
    return {
        "backup_dir": str(backup_dir),
        "keep": keep,
        "recognized_generations": newest_first,
        "kept_generations": kept,
        "delete_generations": deleted,
        "delete_paths": delete_paths,
        "ignored_files": ignored,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, default=default_backup_dir())
    parser.add_argument("--database", type=Path, help="Database whose backup lock to acquire (default: <backup-dir>/../citara.db)")
    parser.add_argument("--keep", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    deleted_files: list[str] = []
    try:
        directory = args.backup_dir.expanduser().resolve()
        database = args.database or directory.parent / "citara.db"
        # Plan again after acquiring the writer's lock; a pre-lock plan can be stale.
        with backup_lock(database):
            plan = plan_cleanup(directory, args.keep)
            if not args.dry_run:
                for path in plan["delete_paths"]:
                    path.unlink()
                    deleted_files.append(path.name)
    except (ValueError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, indent=2))
        return 2

    result = {
        "ok": True,
        "dry_run": args.dry_run,
        "backup_dir": plan["backup_dir"],
        "keep": plan["keep"],
        "recognized_generations": plan["recognized_generations"],
        "kept_generations": plan["kept_generations"],
        "delete_generations": plan["delete_generations"],
        "delete_files": [path.name for path in plan["delete_paths"]],
        "deleted_files": deleted_files,
        "ignored_files": plan["ignored_files"],
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
