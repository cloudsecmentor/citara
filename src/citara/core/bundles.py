"""Dependency-free, explicit-root portable corpus bundles. No runtime config imports."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePath

from citara import __version__

FORMAT = "citara-corpus"
FORMAT_VERSION = 1
SCHEMA_REVISION = "20260816_0005"

# Runtime column contract for SCHEMA_REVISION. Keep this stdlib-only: importing
# runtime database/config modules can bind to the caller's production settings.
# The tests compare every ORM table/column against this revision-bound map.
REQUIRED_SCHEMA = {
    "alembic_version": {"version_num"},
    "tenants": set("id name created_at updated_at".split()),
    "users": set("id tenant_id email display_name role created_at updated_at".split()),
    "collections": set("id tenant_id user_id name description created_at updated_at".split()),
    "sources": set(
        "id tenant_id user_id collection_id source_type title author canonical_url provider external_id "
        "original_asset_id status language metadata_json created_at updated_at".split()
    ),
    "entities": set("id tenant_id entity_type name slug metadata_json created_at updated_at".split()),
    "entity_aliases": set("id tenant_id entity_id alias created_at".split()),
    "source_entities": set("id tenant_id source_id entity_id role confidence provenance metadata_json created_at updated_at".split()),
    "transcript_segments": set("id tenant_id source_id start_ms end_ms speaker text confidence metadata_json created_at".split()),
    "chunks": set(
        "id tenant_id source_id transcript_segment_id chunk_index text heading start_char end_char start_ms end_ms metadata_json created_at".split()
    ),
    "embeddings": set("id tenant_id source_id chunk_id embedding_model dimensions vector created_at".split()),
    "ingestion_jobs": set(
        "id tenant_id user_id source_id job_type status input_json result_json error_message created_at started_at finished_at".split()
    ),
    "chunk_fts": {"chunk_id", "tokens"},
}


class BundleError(ValueError):
    """The bundle or requested filesystem operation is unsafe or incompatible."""


def _database_info(path: Path) -> dict:
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
            for table, columns in REQUIRED_SCHEMA.items():
                kind = db.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchone()
                actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if kind != ("table",) or not columns <= actual:
                    raise BundleError(f"Invalid SQLite schema table/columns: {table}")
            # Exercise the read-only FTS5 MATCH/BM25 interface, not just matching
            # column names on an ordinary table. Never rebuild or repair an index.
            db.execute(
                "SELECT bm25(chunk_fts) FROM chunk_fts WHERE chunk_fts MATCH ? LIMIT 1", ('"citara_bundle_schema_probe"',)
            ).fetchall()
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BundleError("Foreign key integrity check failed")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BundleError("SQLite integrity check failed")
            revisions = db.execute("SELECT version_num FROM alembic_version").fetchall()
            if revisions != [(SCHEMA_REVISION,)]:
                raise BundleError(f"Incompatible SQLite schema revision; expected {SCHEMA_REVISION}")
            counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("sources", "chunks")}
            return {"alembic_revision": SCHEMA_REVISION, "counts": counts}
    except sqlite3.Error as exc:
        raise BundleError(f"Invalid SQLite database/schema: {exc}") from exc


def _record(path: Path, name: str) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return {"path": name, "size": size, "sha256": digest.hexdigest()}


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True, timeout=5, check=True
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _safe_path(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise BundleError("Filesystem paths must be absolute without traversal")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise BundleError(f"Symlink path refused: {part}")
    return path


def _fresh_path(value: Path) -> Path:
    path = _safe_path(value)
    if path.exists():
        raise BundleError(f"Target already exists: {path}")
    if not path.parent.is_dir():
        raise BundleError("Target parent directory must exist")
    return path


def _artifact_files(root: Path) -> list[tuple[Path, str]]:
    result = []
    for folder in ("source-artifacts", "object-store"):
        base = root / folder
        if base.is_symlink():
            raise BundleError(f"Symlink path refused: {base}")
        if not base.exists():
            continue
        if not base.is_dir():
            raise BundleError(f"Artifact root must be a directory: {base}")

        def fail_walk(error):
            raise error

        for current, directories, files in os.walk(base, followlinks=False, onerror=fail_walk):
            for name in directories + files:
                path = Path(current) / name
                if path.is_symlink():
                    raise BundleError(f"Symlink path refused: {path}")
            for name in files:
                path = Path(current) / name
                if not stat.S_ISREG(path.stat().st_mode):
                    raise BundleError(f"Non-regular artifact refused: {path}")
                result.append((path, _payload_name(path.relative_to(root).as_posix())))
    return sorted(result, key=lambda item: item[1])


def export_bundle(
    *,
    data_root: Path,
    output: Path,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimensions: int,
    tenant_id: str = "local",
    user_id: str = "owner",
    include_artifacts: bool = False,
) -> dict:
    """Export a consistent SQLite online backup and an explicit manifest."""
    data_root, output = _safe_path(data_root), _fresh_path(Path(output).absolute())
    if output.is_relative_to(data_root):
        raise BundleError("Output must not be inside data root")
    for name in ("citara.db", "citara.db-wal", "citara.db-shm"):
        _safe_path(data_root / name)
    if not (data_root / "citara.db").is_file():
        raise BundleError("Data root must contain a regular citara.db file")
    embedding = _embedding(embedding_provider, embedding_model, embedding_dimensions)
    _token(tenant_id, "tenant_id")
    _token(user_id, "user_id")
    artifacts = _artifact_files(data_root) if include_artifacts else []
    with tempfile.TemporaryDirectory(prefix="citara-export-") as temporary:
        snapshot = Path(temporary) / "citara.db"
        try:
            with closing(sqlite3.connect((data_root / "citara.db").as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)
        except sqlite3.Error as exc:
            raise BundleError(f"SQLite backup failed: {exc}") from exc
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "citara_version": __version__,
            "git_commit": _git_commit(),
            **_database_info(snapshot),
            "embedding": embedding,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "payloads": [_record(snapshot, "citara.db"), *[_record(path, name) for path, name in artifacts]],
        }
        with tempfile.TemporaryDirectory(prefix=".citara-export-", dir=output.parent) as prepared:
            staged_zip = Path(prepared) / "corpus.zip"
            with zipfile.ZipFile(staged_zip, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot, "citara.db")
                for path, name in artifacts:
                    _safe_path(path)
                    archive.write(path, name)
                archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
            staged_zip.chmod(0o600)
            # Verify artifact hashes again: mutable artifacts must not yield a broken export.
            inspect_bundle(staged_zip)
            _fresh_path(output)
            try:
                os.link(staged_zip, output)  # Atomic create-if-absent; never replaces a racer.
            except FileExistsError as exc:
                raise BundleError(f"Target already exists: {output}") from exc
        return manifest


@dataclass(frozen=True)
class BundleLimits:
    """Uncompressed resource ceilings; callers may explicitly raise these."""

    max_file_bytes: int = 16 * 1024**3
    max_total_bytes: int = 64 * 1024**3
    max_members: int = 100_000
    max_compression_ratio: float = 1000
    max_manifest_bytes: int = 8 * 1024**2

    def __post_init__(self):
        for key, value in vars(self).items():
            numeric = type(value) in (int, float) if key == "max_compression_ratio" else type(value) is int
            if not numeric or not math.isfinite(value) or value <= 0:
                raise BundleError("Bundle limits must be finite, positive, and counts/bytes must be integers")


def _member_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise BundleError(f"Unsafe ZIP path: {name!r}")
    if any(part in ("", ".", "..") for part in name.split("/")) or any(ord(c) < 32 for c in name):
        raise BundleError(f"Unsafe ZIP path: {name!r}")
    for part in name.split("/"):
        # Windows interprets these as device aliases even with extensions.
        # Superscript 1/2/3 are also recognized COM/LPT device digits.
        stem = part.split(".", 1)[0].rstrip(" ").upper()
        if part.endswith((".", " ")) or stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem):
            raise BundleError(f"Unsafe Windows ZIP path segment: {part!r}")
    return name


def _payload_name(name: str) -> str:
    _member_name(name)
    if name != "citara.db" and not name.startswith(("source-artifacts/", "object-store/")):
        raise BundleError(f"Payload outside allowlist: {name}")
    forbidden = {"import-state", "backups", "logs", "cache", "caches", "__pycache__", ".git", ".ssh"}
    for part in name.lower().split("/"):
        if part in forbidden or part.startswith((".env", "credentials", "secrets")) or part.endswith((".pem", ".key", ".pyc", ".log")):
            raise BundleError(f"Secret/transient artifact path refused: {name}")
    return name


def _archive_members(archive: zipfile.ZipFile, limits: BundleLimits) -> dict:
    infos = archive.infolist()
    if len(infos) > limits.max_members:
        raise BundleError("ZIP member count limit exceeded")
    result: dict[str, zipfile.ZipInfo] = {}
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = _member_name(info.filename)
        if name.casefold() in seen:
            raise BundleError(f"Duplicate ZIP path: {name}")
        mode = (info.external_attr >> 16) & 0xFFFF
        if info.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise BundleError(f"Symlink or special ZIP member refused: {name}")
        if info.flag_bits & 1:
            raise BundleError("Encrypted ZIP members are not supported")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise BundleError("Unsupported ZIP compression")
        if info.file_size > limits.max_file_bytes:
            raise BundleError("ZIP file size limit exceeded")
        if info.file_size / max(1, info.compress_size) > limits.max_compression_ratio:
            raise BundleError("ZIP compression ratio limit exceeded")
        total += info.file_size
        if total > limits.max_total_bytes:
            raise BundleError("ZIP total size limit exceeded")
        seen.add(name.casefold())
        result[name] = info
    if "manifest.json" not in result or result["manifest.json"].file_size > limits.max_manifest_bytes:
        raise BundleError("Missing or oversize manifest.json")
    return result


def _token(value, field: str, *, model: bool = False) -> str:
    pattern = r"[A-Za-z0-9][A-Za-z0-9_./-]{0,255}" if model else r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise BundleError(f"Invalid manifest setting: {field}")
    return value


def _embedding(provider, model, dimensions) -> dict:
    provider = _token(provider, "embedding.provider")
    if provider not in {"local", "openai", "azure_foundry"}:
        raise BundleError("Unsupported embedding provider; expected local, openai, or azure_foundry")
    if type(dimensions) is not int or not 1 <= dimensions <= 65536:
        raise BundleError("Invalid embedding dimensions; expected integer 1..65536")
    return {
        "provider": provider,
        "model": _token(model, "embedding.model", model=True),
        "dimensions": dimensions,
    }


def _manifest(raw) -> dict:
    """Validate required fields and project onto a non-secret, known-field allowlist."""
    if not isinstance(raw, dict):
        raise BundleError("Manifest must be a JSON object")
    required = {
        "format",
        "format_version",
        "citara_version",
        "git_commit",
        "alembic_revision",
        "counts",
        "embedding",
        "tenant_id",
        "user_id",
        "payloads",
    }
    if not required <= raw.keys():
        raise BundleError("Manifest missing required fields: " + ", ".join(sorted(required - raw.keys())))
    if raw["format"] != FORMAT or type(raw["format_version"]) is not int or raw["format_version"] != FORMAT_VERSION:
        raise BundleError("Incompatible bundle format/version; expected citara-corpus v1")
    if raw["citara_version"] != __version__:
        raise BundleError(f"Incompatible Citara version; expected {__version__} (no automatic migrations)")
    if raw["alembic_revision"] != SCHEMA_REVISION:
        raise BundleError(f"Incompatible schema revision; expected {SCHEMA_REVISION} (no automatic migrations)")
    commit = raw["git_commit"]
    if commit is not None and (not isinstance(commit, str) or re.fullmatch(r"[a-f0-9]{40,64}", commit) is None):
        raise BundleError("Invalid manifest git_commit")
    counts = raw["counts"]
    if not isinstance(counts, dict) or set(counts) != {"sources", "chunks"} or any(type(v) is not int or v < 0 for v in counts.values()):
        raise BundleError("Invalid manifest counts")
    embedding = raw["embedding"]
    if not isinstance(embedding, dict) or not {"provider", "model", "dimensions"} <= embedding.keys():
        raise BundleError("Missing embedding provider/model/dimensions")
    embedding = _embedding(embedding["provider"], embedding["model"], embedding["dimensions"])
    payloads = raw["payloads"]
    if not isinstance(payloads, list) or not payloads:
        raise BundleError("Manifest payloads must be a nonempty list")
    sanitized = []
    for record in payloads:
        if not isinstance(record, dict) or not {"path", "size", "sha256"} <= record.keys():
            raise BundleError("Invalid manifest payload record")
        name = _payload_name(record["path"])
        size, digest = record["size"], record["sha256"]
        if type(size) is not int or size < 0 or not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise BundleError("Invalid payload size/SHA-256")
        sanitized.append({"path": name, "size": size, "sha256": digest})
    if "citara.db" not in {r["path"] for r in sanitized}:
        raise BundleError("Manifest must declare citara.db")
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "citara_version": __version__,
        "git_commit": commit,
        "alembic_revision": SCHEMA_REVISION,
        "counts": counts,
        "embedding": embedding,
        "tenant_id": _token(raw["tenant_id"], "tenant_id"),
        "user_id": _token(raw["user_id"], "user_id"),
        "payloads": sanitized,
    }


def _restore(archive_path: Path, staging: Path, limits: BundleLimits) -> dict:
    try:
        return _restore_checked(archive_path, staging, limits)
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError, EOFError, RuntimeError, NotImplementedError) as exc:
        raise BundleError(f"Invalid corpus ZIP: {exc}") from exc


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BundleError(f"Duplicate JSON manifest key: {key}")
        result[key] = value
    return result


def _restore_checked(archive_path: Path, staging: Path, limits: BundleLimits) -> dict:
    with zipfile.ZipFile(_safe_path(archive_path.absolute())) as archive:
        members = _archive_members(archive, limits)
        manifest = _manifest(json.loads(archive.read("manifest.json"), object_pairs_hook=_unique_json_object))
        names = [_payload_name(record["path"]) for record in manifest["payloads"]]
        if len(set(names)) != len(names) or set(members) != {"manifest.json", *names}:
            raise BundleError("Duplicate, missing, or undeclared ZIP payload members")
        name_set = set(names)
        for name in names:
            for parent in Path(name).parents:
                if parent.as_posix() in name_set:
                    raise BundleError("ZIP file/directory path conflict")
        for record in manifest["payloads"]:
            if record["size"] != members[record["path"]].file_size:
                raise BundleError(f"Payload size mismatch: {record['path']}")
            path = staging / record["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(record["path"]) as source, path.open("xb") as target:
                while block := source.read(1024 * 1024):
                    target.write(block)
            if _record(path, record["path"]) != record:
                raise BundleError(f"Payload hash/size mismatch: {record['path']}")
        info = _database_info(staging / "citara.db")
        if any(info[key] != manifest[key] for key in info):
            raise BundleError("SQLite counts/schema do not match manifest")
        return manifest


def inspect_bundle(archive: Path, *, limits: BundleLimits | None = None) -> dict:
    """Fully verify a bundle without publishing any corpus."""
    with tempfile.TemporaryDirectory(prefix="citara-inspect-") as temporary:
        return _restore(Path(archive), Path(temporary), limits or BundleLimits())


def _validate_data_root(root: PurePath) -> None:
    # Match the standalone launcher: raw SQLite URLs cannot contain literal '?'.
    if "?" in str(root) or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in str(root)):
        raise BundleError("Selected data root must not contain '?' or control characters")


def _environment(root: PurePath, manifest: dict) -> str:
    _validate_data_root(root)
    settings = {
        "CITARA_SKIP_DOTENV": "1",
        "CITARA_DATA_ROOT": str(root),
        "DATABASE_URL": f"sqlite:///{root.as_posix()}/citara.db",
        "SOURCE_ARTIFACT_ROOT": str(root / "source-artifacts"),
        "SOURCE_STATE_ROOT": str(root / "import-state"),
        "OBJECT_STORE_PATH": str(root / "object-store"),
        "EMBEDDING_PROVIDER": manifest["embedding"]["provider"],
        "EMBEDDING_MODEL": manifest["embedding"]["model"],
        "EMBEDDING_DIMENSIONS": str(manifest["embedding"]["dimensions"]),
        "DEFAULT_TENANT_ID": manifest["tenant_id"],
        "DEFAULT_USER_ID": manifest["user_id"],
        "TRANSLATION_PROVIDER": "noop",
    }
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in settings.items())


def _publish_directory(staging: Path, target: Path) -> None:
    """Atomic no-replace directory rename, including a target created after validation."""
    if sys.platform == "win32":
        try:
            os.rename(staging, target)  # Windows rename never replaces an existing target.
            return
        except FileExistsError as exc:
            raise BundleError(f"Target already exists: {target}") from exc
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(staging), os.fsencode(target), 0x4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, os.fsencode(staging), -100, os.fsencode(target), 1)  # RENAME_NOREPLACE
    else:
        raise BundleError("Platform lacks safe atomic no-replace directory publication")
    if result != 0:
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise BundleError(f"Target already exists: {target}")
        raise OSError(error, os.strerror(error), str(target))


def import_bundle(archive: Path, *, data_root: Path, limits: BundleLimits | None = None) -> dict:
    """Validate in a private sibling staging directory, then publish a fresh root."""
    _validate_data_root(data_root)
    root = _fresh_path(data_root)
    with tempfile.TemporaryDirectory(prefix=".citara-import-", dir=root.parent) as temporary:
        staging = Path(temporary) / "corpus"
        staging.mkdir(mode=0o700)
        manifest = _restore(Path(archive), staging, limits or BundleLimits())
        (staging / "corpus.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (staging / "citara.env").write_text(_environment(root, manifest))
        _fresh_path(root)
        _publish_directory(staging, root)
        return manifest
