#!/usr/bin/env python3
"""Prune old timestamped Citara backup generations."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from pathlib import Path

TIMESTAMP_RE = re.compile(r"(?<!\d)(\d{8}T\d{6}Z)(?!\d)")
RECOGNIZED_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".tar.gz", ".tgz", ".zip")


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

    newest_first = sorted(generations, reverse=True)
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
    parser.add_argument("--keep", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        plan = plan_cleanup(args.backup_dir.expanduser().resolve(), args.keep)
    except (ValueError, FileNotFoundError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, indent=2))
        return 2

    deleted_files: list[str] = []
    if not args.dry_run:
        for path in plan["delete_paths"]:
            path.unlink()
            deleted_files.append(path.name)

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
