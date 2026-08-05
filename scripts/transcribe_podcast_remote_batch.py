#!/usr/bin/env python3
"""Run an explicit podcast transcription queue on a remote faster-whisper worker.

The orchestrator is deliberately artifact-only: it never opens or imports into a Citara
DB. Audio is downloaded locally, staged on the worker, and removed from the worker after
each attempt. The generic worker machinery is reused in a manifest-specific remote
namespace. Local raw/chunked/stats JSON is validated before atomic promotion.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from artifact_paths import data_root, repo_root

DEFAULT_REPO = repo_root()
DEFAULT_DATA_ROOT = data_root()
DEFAULT_MANIFEST = DEFAULT_DATA_ROOT / "source-artifacts" / "a-book-like-no-other" / "remote-openai" / "approved-queue.json"
# The transcription worker is deployment-specific: supply it with --worker or
# CITARA_WORKER_SSH (e.g. "user@host"). There is deliberately no built-in default.
DEFAULT_WORKER = os.getenv("CITARA_WORKER_SSH", "")
DEFAULT_SSH_KEY = Path(os.getenv("CITARA_WORKER_SSH_KEY", "~/.ssh/id_ed25519")).expanduser()
DEFAULT_REMOTE_ROOT = Path(os.getenv("CITARA_WORKER_ROOT", "/opt/citara-worker"))
ARTIFACT_SUFFIXES = ("oai-raw.json", "oai-raw-chunked.json", "transcribe-stats.json")
REQUIRED_ITEM_FIELDS = {
    "queue_number",
    "episode_label",
    "guid",
    "title",
    "audio_url",
    "canonical_url",
    "duration_seconds",
    "artifact_stem",
}
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SSH_OPTS = [
    "-o",
    "ConnectTimeout=30",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=4",
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
]


class Config(NamedTuple):
    manifest: Path
    local_out: Path
    state_path: Path
    repo: Path
    worker: str
    ssh_key: Path
    remote_root: Path
    model: str
    device: str
    compute_type: str
    beam_size: int
    best_of: int
    timeout_seconds: int
    download_attempts: int
    inter_episode_delay_fraction: float
    limit: int | None
    dry_run: bool


def run(argv: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(shlex.quote(part) for part in argv), flush=True)
    return subprocess.run(
        argv,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def ssh_args(key: Path, worker: str, remote_command: str) -> list[str]:
    return ["ssh", "-i", str(key), *SSH_OPTS, worker, remote_command]


def scp_args(key: Path, *args: str) -> list[str]:
    return ["scp", "-i", str(key), *SSH_OPTS, *args]


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load queue manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("manifest schema_version must be 1")
    for field in ("corpus_slug", "remote_namespace"):
        value = manifest.get(field)
        if not isinstance(value, str) or not SAFE_NAME.fullmatch(value):
            raise ValueError(f"manifest {field} must be a safe lowercase name")
    items = manifest.get("episodes")
    if not isinstance(items, list) or not items:
        raise ValueError("manifest episodes must be a non-empty list")

    seen_numbers: set[int] = set()
    seen_guids: set[str] = set()
    seen_stems: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"manifest episode {index} must be an object")
        missing = REQUIRED_ITEM_FIELDS - item.keys()
        if missing:
            raise ValueError(f"manifest episode {index} missing fields: {sorted(missing)}")
        number = item["queue_number"]
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0 or number in seen_numbers:
            raise ValueError(f"episode {index} has invalid or duplicate queue_number")
        # Stable queue numbers must be ordered and gap-free; this prevents accidental remapping.
        if number != index:
            raise ValueError(f"queue_number must be stable, ordered, and contiguous (expected {index})")
        stem = item["artifact_stem"]
        if not isinstance(stem, str) or not SAFE_NAME.fullmatch(stem) or stem in seen_stems:
            raise ValueError(f"episode {index} has invalid or duplicate artifact_stem")
        if not stem.startswith(f"q{number:03d}-"):
            raise ValueError(f"episode {index} artifact_stem must start with q{number:03d}-")
        guid = item["guid"]
        if not isinstance(guid, str) or not guid.strip() or guid in seen_guids:
            raise ValueError(f"episode {index} has invalid or duplicate guid")
        for field in ("episode_label", "title", "audio_url", "canonical_url"):
            if not isinstance(item[field], str) or not item[field].strip():
                raise ValueError(f"episode {index} has invalid {field}")
        duration = item["duration_seconds"]
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
            raise ValueError(f"episode {index} has invalid duration_seconds")
        seen_numbers.add(number)
        seen_guids.add(guid)
        seen_stems.add(stem)
    return manifest


def try_load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def artifact_paths(local_out: Path, item: dict[str, Any]) -> dict[str, Path]:
    stem = item["artifact_stem"]
    return {suffix: local_out / f"{stem}-{suffix}" for suffix in ARTIFACT_SUFFIXES}


def artifact_triplet_complete(local_out: Path, item: dict[str, Any]) -> bool:
    paths = artifact_paths(local_out, item)
    raw = try_load_json(paths["oai-raw.json"])
    chunked = try_load_json(paths["oai-raw-chunked.json"])
    stats = try_load_json(paths["transcribe-stats.json"])
    return raw is not None and chunked is not None and isinstance(stats, dict) and stats.get("complete") is True


def download_with_retries(url: str, destination: Path, *, attempts: int = 3) -> float:
    if attempts < 1:
        raise ValueError("download attempts must be at least 1")
    part = destination.with_suffix(destination.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        part.unlink(missing_ok=True)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "citara-podcast-orchestrator/1.0"})
            with urllib.request.urlopen(request, timeout=900) as response:
                part.write_bytes(response.read())
            if part.stat().st_size == 0:
                raise RuntimeError("download returned an empty file")
            os.replace(part, destination)
            return time.monotonic() - started
        except Exception as exc:
            last_error = exc
            part.unlink(missing_ok=True)
            if attempt < attempts:
                time.sleep(2**attempt)
    raise RuntimeError(f"audio download failed after {attempts} attempts: {last_error}") from last_error


def atomic_scp_json(key: Path, worker: str, remote_path: str, local_final: Path) -> Any:
    """Copy to .part, validate JSON, and only then atomically replace local_final."""
    local_final.parent.mkdir(parents=True, exist_ok=True)
    part = local_final.with_suffix(local_final.suffix + ".part")
    part.unlink(missing_ok=True)
    try:
        run(scp_args(key, f"{worker}:{remote_path}", str(part)), timeout=300)
        value = try_load_json(part)
        if value is None:
            raise RuntimeError(f"JSON validation failed for copied artifact {remote_path}")
        os.replace(part, local_final)
        return value
    finally:
        part.unlink(missing_ok=True)


def update_item_state(state_path: Path, item: dict[str, Any], updates: dict[str, Any]) -> None:
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"refusing to overwrite invalid state file {state_path}") from exc
    else:
        state = {"schema_version": 1, "episodes": {}}
    episodes = state.setdefault("episodes", {})
    entry = episodes.setdefault(
        item["guid"],
        {
            "queue_number": item["queue_number"],
            "episode_label": item["episode_label"],
            "title": item["title"],
            "artifact_stem": item["artifact_stem"],
        },
    )
    entry.update(updates)
    write_json_atomic(state_path, state)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _remote_command(config: Config, item: dict[str, Any], remote_out: Path, remote_audio: Path) -> str:
    number = int(item["queue_number"])
    parts = [
        config.remote_root / "venv" / "bin" / "python",
        config.remote_root / "remote_podcast_transcribe.py",
        "--episode",
        str(number),
        "--title",
        item["title"],
        "--url",
        item["canonical_url"],
        "--audio-url",
        item["audio_url"],
        "--audio-path",
        remote_audio,
        "--duration-seconds",
        str(item["duration_seconds"]),
        "--out-dir",
        remote_out,
        "--model",
        config.model,
        "--device",
        config.device,
        "--compute-type",
        config.compute_type,
        "--beam-size",
        str(config.beam_size),
        "--best-of",
        str(config.best_of),
    ]
    return " ".join(shlex.quote(str(part)) for part in parts)


def orchestrate(config: Config) -> dict[str, Any]:
    manifest = load_manifest(config.manifest)
    if config.limit is not None and config.limit < 0:
        raise ValueError("--limit must be non-negative")
    if config.inter_episode_delay_fraction < 0:
        raise ValueError("--inter-episode-delay-fraction must be non-negative")

    summary: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for item in manifest["episodes"]:
        if artifact_triplet_complete(config.local_out, item):
            row = {
                "queue_number": item["queue_number"],
                "guid": item["guid"],
                "title": item["title"],
                "status": "skipped_existing_artifact",
            }
            summary.append(row)
            if not config.dry_run:
                update_item_state(config.state_path, item, {"status": "complete", "verified_at": utc_now()})
            continue
        if config.limit is not None and len(pending) >= config.limit:
            break
        pending.append(item)
        if config.dry_run:
            summary.append(
                {
                    "queue_number": item["queue_number"],
                    "guid": item["guid"],
                    "title": item["title"],
                    "artifact_stem": item["artifact_stem"],
                    "status": "dry_run",
                }
            )

    if config.dry_run or not pending:
        return {"manifest": str(config.manifest), "episodes": summary, "successful_queue_numbers": []}

    config.local_out.mkdir(parents=True, exist_ok=True)
    remote_out = config.remote_root / manifest["remote_namespace"] / "out"
    run(
        scp_args(
            config.ssh_key,
            str(config.repo / "scripts" / "remote_bema_transcribe.py"),
            f"{config.worker}:{config.remote_root}/remote_podcast_transcribe.py",
        ),
        timeout=120,
    )
    run(ssh_args(config.ssh_key, config.worker, f"mkdir -p {shlex.quote(str(remote_out))}"), timeout=60)

    successful: list[int] = []
    for pending_index, item in enumerate(pending):
        number = int(item["queue_number"])
        started = time.monotonic()
        # Never embed a raw feed GUID in an SCP destination. SoundCloud GUIDs contain
        # colons (for example ``tag:soundcloud,2010:tracks/...``), which SCP parses as
        # host/path syntax and may report a misleading missing-directory error.
        remote_audio = remote_out / f"{item['artifact_stem']}.mp3"
        worker_audio = remote_out / f"e{number:03d}.mp3"
        staged = False
        try:
            update_item_state(
                config.state_path,
                item,
                {
                    "status": "running",
                    "started_at": utc_now(),
                    "model": config.model,
                    "device": config.device,
                    "compute_type": config.compute_type,
                },
            )
            with tempfile.TemporaryDirectory(prefix=f"citara-podcast-q{number:03d}-") as temporary_dir:
                local_audio = Path(temporary_dir) / f"{item['artifact_stem']}.mp3"
                download_seconds = download_with_retries(item["audio_url"], local_audio, attempts=config.download_attempts)
                # Cleanup is required even when SCP leaves a partial remote file and exits non-zero.
                staged = True
                run(scp_args(config.ssh_key, str(local_audio), f"{config.worker}:{remote_audio}"), timeout=900)
                process = run(
                    ssh_args(config.ssh_key, config.worker, _remote_command(config, item, remote_out, remote_audio)),
                    timeout=config.timeout_seconds,
                )
                print(process.stdout, flush=True)

            paths = artifact_paths(config.local_out, item)
            copied: dict[str, Any] = {}
            for suffix in ARTIFACT_SUFFIXES:
                copied[suffix] = atomic_scp_json(
                    config.ssh_key,
                    config.worker,
                    str(remote_out / f"e{number:03d}-{suffix}"),
                    paths[suffix],
                )
            stats = copied["transcribe-stats.json"]
            if not isinstance(stats, dict) or stats.get("complete") is not True:
                raise RuntimeError("stats JSON lacks complete=true")
            stats.update(
                {
                    "queue_number": number,
                    "episode_label": item["episode_label"],
                    "guid": item["guid"],
                    "artifact_stem": item["artifact_stem"],
                    "canonical_url": item["canonical_url"],
                    "local_download_seconds": download_seconds,
                }
            )
            write_json_atomic(paths["transcribe-stats.json"], stats)
            if not artifact_triplet_complete(config.local_out, item):
                raise RuntimeError("local artifact triplet failed final validation")
            update_item_state(
                config.state_path,
                item,
                {
                    "status": "complete",
                    "completed_at": utc_now(),
                    "raw_path": str(paths["oai-raw.json"]),
                    "chunked_path": str(paths["oai-raw-chunked.json"]),
                    "stats_path": str(paths["transcribe-stats.json"]),
                },
            )
            successful.append(number)
            summary.append(
                {
                    "queue_number": number,
                    "guid": item["guid"],
                    "title": item["title"],
                    "status": "transcribed",
                }
            )
        except Exception as exc:
            error = str(exc)
            if isinstance(exc, subprocess.CalledProcessError):
                output = exc.stdout[-2000:] if exc.stdout else ""
                error = f"ssh/scp failed: {exc.returncode}: {output}"
            update_item_state(config.state_path, item, {"status": "error", "error": error, "failed_at": utc_now()})
            summary.append(
                {
                    "queue_number": number,
                    "guid": item["guid"],
                    "title": item["title"],
                    "status": "error",
                    "error": error,
                }
            )
        finally:
            if staged:
                cleanup = f"rm -f {shlex.quote(str(remote_audio))} {shlex.quote(str(worker_audio))}"
                try:
                    run(ssh_args(config.ssh_key, config.worker, cleanup), timeout=60)
                except Exception as cleanup_error:
                    print(f"Warning: remote audio cleanup failed for queue {number}: {cleanup_error}", flush=True)

        if pending_index + 1 < len(pending) and config.inter_episode_delay_fraction:
            elapsed = time.monotonic() - started
            delay = elapsed * config.inter_episode_delay_fraction
            print(f"Queue {number:03d} finished in {elapsed:.1f}s; sleeping {delay:.1f}s", flush=True)
            time.sleep(delay)

    result = {"manifest": str(config.manifest), "episodes": summary, "successful_queue_numbers": successful}
    write_json_atomic(config.local_out / "latest-batch-summary.json", result)
    return result


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description="Transcribe an explicit podcast queue on a remote worker (artifacts only)")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--local-out", type=Path)
    parser.add_argument("--state-path", type=Path)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument(
        "--worker",
        default=DEFAULT_WORKER,
        required=not DEFAULT_WORKER,
        help="Transcription worker SSH target, e.g. user@host. Defaults to $CITARA_WORKER_SSH.",
    )
    parser.add_argument("--ssh-key", type=Path, default=DEFAULT_SSH_KEY)
    parser.add_argument("--remote-root", type=Path, default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--model", default="medium")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--best-of", type=int, default=1)
    parser.add_argument("--transcribe-timeout-seconds", type=int, default=28800)
    parser.add_argument("--download-attempts", type=int, default=3)
    parser.add_argument("--inter-episode-delay-fraction", type=float, default=0.10)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    local_out = args.local_out or args.manifest.parent
    state_path = args.state_path or local_out / "transcription-state.json"
    return Config(
        manifest=args.manifest,
        local_out=local_out,
        state_path=state_path,
        repo=args.repo,
        worker=args.worker,
        ssh_key=args.ssh_key,
        remote_root=args.remote_root,
        model=args.model,
        device=args.device,
        compute_type=args.compute_type,
        beam_size=args.beam_size,
        best_of=args.best_of,
        timeout_seconds=args.transcribe_timeout_seconds,
        download_attempts=args.download_attempts,
        inter_episode_delay_fraction=args.inter_episode_delay_fraction,
        limit=args.limit,
        dry_run=args.dry_run,
    )


def main() -> None:
    result = orchestrate(parse_args())
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
