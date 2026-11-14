#!/usr/bin/env python3
"""Backfill BEMA metadata in an explicit data root; dry-run unless --apply.

Artifact reconciliation scans all BEMA rows, so rerunning --apply repairs a
post-commit artifact-write failure even when database metadata is already complete.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
import tarfile
import tempfile
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from citara.core.backups import backup_database, backup_dir, backup_lock

EPISODE_RE = re.compile(r"^BEMA\s+(-?\d+)(?:[A-Za-z])?:")
SEASON_BOUNDS = ((32, 1), (72, 2), (133, 3), (190, 4), (204, 5), (333, 6), (360, 7), (410, 8), (495, 9), (507, 10))


def season_for_episode(episode: int) -> str:
    if type(episode) is not int or episode < -1:
        raise ValueError(f"Invalid BEMA episode: {episode!r}")
    # BEMA -1 is introductory material grouped with Session/Season 1.
    for upper, season in SEASON_BOUNDS:
        if episode <= upper:
            return str(season)
    raise ValueError(f"No known BEMA season for episode {episode}")


def atomic_write_json(path: Path, payload: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def checked_path(path: Path, root: Path) -> Path:
    """Reject symlink aliases as well as paths outside the explicit target."""
    if not path.is_relative_to(root) or path.resolve() != path:
        raise ValueError(f"Unsafe target path: {path}")
    return path


def plan(connection: sqlite3.Connection, items: Path) -> tuple[list[tuple[str, dict]], dict[Path, tuple[bytes, dict]]]:
    """Read current rows and parse every manifest before allowing a DB commit."""
    changes: list[tuple[str, dict]] = []
    artifacts: dict[Path, tuple[bytes, dict]] = {}
    rows = connection.execute(
        "SELECT id, title, metadata_json FROM sources WHERE json_extract(metadata_json, '$.source_tree_slug') = 'bema' ORDER BY id"
    ).fetchall()
    for row in rows:
        metadata: dict[str, Any] = json.loads(row["metadata_json"])
        match = EPISODE_RE.match(row["title"])
        if not match:
            raise ValueError(f"Cannot parse BEMA episode from {row['id']}: {row['title']}")
        episode = int(match.group(1)) if metadata.get("episode_number") is None else metadata["episode_number"]
        derived_season = season_for_episode(episode)  # Also validates existing episode values.
        value = metadata.get("season")
        if value is None:
            season = derived_season
        elif type(value) in (int, float) and value in range(1, 11):
            season = str(int(value))
        elif isinstance(value, str) and value in {str(n) for n in range(1, 11)}:
            season = value
        else:
            raise ValueError(f"Invalid BEMA season: {value!r}")
        updated = {**metadata, "episode_number": episode, "season": season}
        if updated != metadata:
            changes.append((row["id"], updated))
        item_id = metadata.get("source_item_id")
        if item_id is None:
            continue
        if not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", item_id):
            raise ValueError(f"Unsafe BEMA source_item_id: {item_id!r}")
        path = checked_path(items / item_id / "source.json", items)
        if not path.exists():
            continue
        raw = path.read_bytes()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"Artifact must be a JSON object: {path}")
        updated_payload = {**payload, "episode_number": episode, "season": season}
        if updated_payload != payload:
            artifacts[path] = (raw, updated_payload)
    return changes, artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--citara-root", type=Path, required=True, help="Existing Citara data root (contains citara.db)")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.citara_root.expanduser().resolve()
    database = checked_path(root / "citara.db", root)
    items = checked_path(root / "source-artifacts/bema/items", root)
    checked_path(backup_dir(database), root)
    if not database.is_file():
        raise FileNotFoundError(database)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "citara_root": str(root), "database": str(database)}))
    uri = database.as_uri() + ("?mode=rw" if args.apply else "?mode=ro")
    with closing(sqlite3.connect(uri, uri=True)) as connection, ExitStack() as stack:
        connection.row_factory = sqlite3.Row
        changes, artifacts = plan(connection, items)
        print(json.dumps({"eligible": len(changes), "artifact_manifests_to_reconcile": len(artifacts)}))
        if not args.apply or not (changes or artifacts):
            return
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        # Online backup must precede BEGIN IMMEDIATE: backing up our own write
        # transaction would hang. Re-read everything after acquiring the lock.
        db_backup = backup_database(database, "bema-metadata", source=connection, generation=timestamp)
        # Do not nest backup_database inside this non-reentrant lock. Once it
        # returns, hold retention off until the companion archive is published
        # and mutations finish. A competing generation may already have won.
        stack.enter_context(backup_lock(database))
        if db_backup is None or not db_backup.is_file():
            raise RuntimeError("Database backup was pruned concurrently; rerun")
        checked_path(backup_dir(database), root)
        artifact_backup = backup_dir(database) / f"bema-source-json-before-metadata-{timestamp}.tar.gz"
        try:
            connection.execute("BEGIN IMMEDIATE")
            changes, artifacts = plan(connection, items)
            descriptor, name = tempfile.mkstemp(prefix=f".{artifact_backup.name}.", suffix=".partial", dir=artifact_backup.parent)
            os.close(descriptor)
            partial = Path(name)
            try:
                with tarfile.open(partial, "w:gz") as archive:
                    for path, (raw, _) in artifacts.items():
                        info = tarfile.TarInfo(str(path.relative_to(items.parent)))
                        info.size = len(raw)
                        archive.addfile(info, io.BytesIO(raw))
                partial.replace(artifact_backup)
            finally:
                partial.unlink(missing_ok=True)
            now = datetime.now(UTC).isoformat()
            for source_id, metadata in changes:
                connection.execute(
                    "UPDATE sources SET metadata_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False, sort_keys=True), now, source_id),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        for path, (raw, payload) in artifacts.items():
            checked_path(path, items)
            if path.read_bytes() != raw:
                raise RuntimeError(f"Artifact changed concurrently; rerun to reconcile: {path}")
            atomic_write_json(path, payload)
        remaining, remaining_artifacts = plan(connection, items)
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if remaining or remaining_artifacts or quick_check != "ok":
            raise RuntimeError("BEMA metadata reconciliation verification failed")
        print(
            json.dumps(
                {
                    "updated_sources": len(changes),
                    "updated_artifact_manifests": len(artifacts),
                    "remaining_missing": len(remaining),
                    "database_quick_check": quick_check,
                    "database_backup": str(db_backup),
                    "artifact_backup": str(artifact_backup),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
