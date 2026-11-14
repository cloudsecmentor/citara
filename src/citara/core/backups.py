"""Single-generation backups of the local Citara SQLite database.

Every backup is written to ``<database dir>/backups``. Once the new copy has passed an
integrity check, every older backup is deleted, so at most one restore point (the most
recent one) is kept on disk. A failed backup never removes the previous one.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

GENERATION_FORMAT = "%Y%m%dT%H%M%SZ"
# Matches GENERATION_FORMAT plus the fractional-second variant older scripts wrote.
GENERATION_RE = re.compile(r"(?<!\d)(\d{8}T\d{6}(?:\.\d+)?Z)(?!\d)")
BACKUP_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".tar.gz", ".tgz", ".zip")
# Older import scripts wrote backups next to the live database instead of backups/.
LEGACY_ROOT_PATTERN = "citara.backup-*.db"


def backup_dir(database: Path) -> Path:
    return database.parent / "backups"


def new_generation() -> str:
    return datetime.now(UTC).strftime(GENERATION_FORMAT)


def backup_generation(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file() or not path.name.endswith(BACKUP_SUFFIXES):
        return None
    match = GENERATION_RE.search(path.name)
    return match.group(1) if match else None


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


def backup_database(
    database: Path,
    label: str,
    *,
    source: sqlite3.Connection | None = None,
    generation: str | None = None,
) -> Path | None:
    """Snapshot ``database`` and then prune every older backup.

    Pass ``source`` to back up through an already-open connection, and ``generation``
    when companion files (e.g. an artifact tarball) must share the backup's timestamp so
    they survive the prune together.
    """
    database = database.expanduser().resolve()
    if source is None and not database.exists():
        return None
    with backup_lock(database):
        generation = generation or new_generation()
        directory = backup_dir(database)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"citara-before-{label}-{generation}.db"
        descriptor, name = tempfile.mkstemp(prefix=f"{destination.name}.", suffix=".partial", dir=directory)
        os.close(descriptor)
        partial = Path(name)
        try:
            with closing(sqlite3.connect(partial)) as target:
                # The online backup API includes committed WAL pages.
                if source is None:
                    with closing(sqlite3.connect(database)) as live:
                        live.backup(target)
                else:
                    source.backup(target)
                integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Backup integrity check failed for {destination}: {integrity}")
            partial.replace(destination)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        _prune_backups(database, keep_generation=generation)
        return destination


def prune_backups(database: Path, *, keep_generation: str) -> list[Path]:
    """Delete every recognized backup except those from ``keep_generation``."""
    database = database.expanduser().resolve()
    with backup_lock(database):
        return _prune_backups(database, keep_generation=keep_generation)


def _prune_backups(database: Path, *, keep_generation: str) -> list[Path]:
    stale: list[Path] = []
    directory = backup_dir(database)
    # A queued caller may request a generation already replaced by another writer.
    # Never remove the remaining restore point on behalf of that stale request.
    if not directory.is_dir() or not any(backup_generation(path) == keep_generation for path in directory.iterdir()):
        return []
    if directory.is_dir():
        for path in directory.iterdir():
            generation = backup_generation(path)
            if generation is not None and generation != keep_generation:
                stale.append(path)
    for path in database.parent.glob(LEGACY_ROOT_PATTERN):
        if path.is_file() and not path.is_symlink():
            stale.append(path)
    for path in sorted(stale):
        path.unlink()
    return sorted(stale)
