#!/usr/bin/env python3
"""Orchestrate BibleProject audio transcription on a remote worker.

Flow per episode:
1) discover/refresh BibleProject RSS state locally;
2) process episodes in chronological order by default;
3) skip episodes with official PDF transcripts;
4) download MP3 locally, upload/stage to worker;
5) run the worker faster-whisper script;
6) copy raw/chunked/stats JSON artifacts back atomically;
7) validate JSON, mark local state, and remove temporary worker audio.

This intentionally creates durable artifacts only. Import into the Citara DB can be done
later from the returned raw/chunked JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from artifact_paths import data_root, repo_root

DEFAULT_CITARA_ROOT = data_root()
DEFAULT_REPO = repo_root()
# The transcription worker is deployment-specific: supply it with --worker or
# CITARA_WORKER_SSH (e.g. "user@host"). There is deliberately no built-in default.
DEFAULT_WORKER = os.getenv("CITARA_WORKER_SSH", "")
DEFAULT_SSH_KEY = Path(os.getenv("CITARA_WORKER_SSH_KEY", "~/.ssh/id_ed25519")).expanduser()
DEFAULT_REMOTE_ROOT = Path(os.getenv("CITARA_WORKER_ROOT", "/opt/citara-worker"))
DEFAULT_FEED_URL = "https://feeds.simplecast.com/3NVmUWZO"

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


def run(argv: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(shlex.quote(a) for a in argv), flush=True)
    return subprocess.run(
        argv,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def ssh_args(key: Path, worker: str, remote_cmd: str) -> list[str]:
    return ["ssh", "-i", str(key), *SSH_OPTS, worker, remote_cmd]


def scp_args(key: Path, *args: str) -> list[str]:
    return ["scp", "-i", str(key), *SSH_OPTS, *args]


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"episodes": {}}
    return json.loads(path.read_text())


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def update_episode_state(state_path: Path, guid: str, updates: dict[str, Any]) -> None:
    state = load_state(state_path)
    entry = state.setdefault("episodes", {}).setdefault(guid, {})
    entry.update(updates)
    save_state(state_path, state)


def discover_episodes(repo: Path, citara_root: Path, feed_url: str) -> list[dict[str, Any]]:
    sys.path.insert(0, str(repo / "src"))
    from citara.connectors.podcasts import bibleproject  # type: ignore

    state_path = citara_root / "import-state" / "bibleproject_pipeline_state.json"
    show_title, episodes = bibleproject.discover(feed_url, state_path)
    # RSS is newest-first; process oldest-first for stable e001.. numbering.
    chronological = list(reversed(episodes))
    state = load_state(state_path)
    state["feed_url"] = feed_url
    state["show_title"] = show_title
    state["episode_count"] = len(chronological)
    state["published_transcript_count"] = sum(1 for ep in chronological if ep.get("transcript_url"))
    for index, episode in enumerate(chronological, start=1):
        guid = episode["guid"]
        entry = state.setdefault("episodes", {}).setdefault(guid, {})
        entry["episode"] = index
        entry["episode_title"] = episode.get("title")
        entry["has_published_transcript"] = bool(episode.get("transcript_url"))
    save_state(state_path, state)
    return chronological


def try_load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def artifact_complete(local_out: Path, number: int) -> bool:
    raw = local_out / f"e{number:03d}-oai-raw.json"
    chunked = local_out / f"e{number:03d}-oai-raw-chunked.json"
    stats = local_out / f"e{number:03d}-transcribe-stats.json"
    if not (raw.exists() and chunked.exists() and stats.exists()):
        return False
    if try_load_json(raw) is None or try_load_json(chunked) is None:
        return False
    stats_data = try_load_json(stats)
    if not isinstance(stats_data, dict):
        return False
    return stats_data.get("complete") is True or int(stats_data.get("segments") or 0) > 0


def download(url: str, dest: Path, *, attempts: int = 3) -> float:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        t0 = time.time()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "citara-bibleproject-local-orchestrator/0.1"})
            with urllib.request.urlopen(request, timeout=900) as response:
                dest.write_bytes(response.read())
            return time.time() - t0
        except Exception as exc:  # transient 5xx/socket failures are common enough to retry
            last_error = exc
            if attempt < attempts:
                time.sleep(2**attempt)
    raise RuntimeError(f"audio download failed after {attempts} attempts: {last_error}")


def atomic_scp_to_part(key: Path, worker: str, remote_path: str, local_final: Path) -> None:
    local_part = local_final.with_suffix(local_final.suffix + ".part")
    run(scp_args(key, f"{worker}:{remote_path}", str(local_part)), timeout=300)
    if local_final.exists():
        local_final.unlink()
    os.replace(local_part, local_final)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe BibleProject missing-transcript episodes on remote worker")
    parser.add_argument("--start", type=int, default=1, help="Chronological episode number to start at")
    parser.add_argument("--end", type=int, help="Chronological episode number to stop at")
    parser.add_argument("--limit", type=int, help="Maximum number of missing episodes to transcribe this run")
    parser.add_argument("--citara-root", type=Path, default=DEFAULT_CITARA_ROOT)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument(
        "--worker",
        default=DEFAULT_WORKER,
        required=not DEFAULT_WORKER,
        help="Transcription worker SSH target, e.g. user@host. Defaults to $CITARA_WORKER_SSH.",
    )
    parser.add_argument("--ssh-key", type=Path, default=DEFAULT_SSH_KEY)
    parser.add_argument("--remote-root", type=Path, default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--feed-url", default=DEFAULT_FEED_URL)
    parser.add_argument("--model", default="medium")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--best-of", type=int, default=1)
    parser.add_argument("--transcribe-timeout-seconds", type=int, default=28800)
    parser.add_argument(
        "--inter-episode-delay-fraction",
        type=float,
        default=0.10,
        help="Sleep this fraction of the just-finished episode processing wall time before starting the next transcription episode.",
    )
    parser.add_argument("--skip-existing", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    state_path = args.citara_root / "import-state" / "bibleproject_pipeline_state.json"
    local_out = args.citara_root / "source-artifacts" / "bibleproject" / "remote-openai"
    remote_out = args.remote_root / "bibleproject" / "out"
    local_out.mkdir(parents=True, exist_ok=True)

    episodes = discover_episodes(args.repo, args.citara_root, args.feed_url)
    end = args.end or len(episodes)

    run(
        scp_args(
            args.ssh_key,
            str(args.repo / "scripts" / "remote_bema_transcribe.py"),
            f"{args.worker}:{args.remote_root}/remote_bibleproject_transcribe.py",
        ),
        timeout=120,
    )
    run(ssh_args(args.ssh_key, args.worker, f"mkdir -p {shlex.quote(str(remote_out))}"), timeout=60)

    summary: list[dict[str, Any]] = []
    successful: list[int] = []
    summary_path = local_out / f"batch-{args.start}-{end}-summary.json"

    def write_summary() -> None:
        tmp = summary_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"episodes": summary, "successful_episodes": successful}, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, summary_path)

    if args.inter_episode_delay_fraction < 0:
        raise ValueError("--inter-episode-delay-fraction must be non-negative")

    def sleep_after_episode(episode_start: float, number: int, status: str) -> None:
        if args.inter_episode_delay_fraction <= 0:
            return
        elapsed = time.time() - episode_start
        delay = elapsed * args.inter_episode_delay_fraction
        if delay <= 0:
            return
        print(
            f"Episode {number:03d} {status}; processing wall time {elapsed:.1f}s. "
            f"Sleeping {delay:.1f}s ({args.inter_episode_delay_fraction:.0%}) before next episode.",
            flush=True,
        )
        time.sleep(delay)

    processed_missing = 0
    for number, episode in enumerate(episodes, start=1):
        if number < args.start or number > end:
            continue
        guid = episode["guid"]
        title = episode.get("title") or f"BibleProject {number}"

        update_episode_state(
            state_path,
            guid,
            {
                "episode": number,
                "episode_title": title,
                "has_published_transcript": bool(episode.get("transcript_url")),
            },
        )

        if episode.get("transcript_url"):
            summary.append({"episode": number, "title": title, "status": "skipped_published_pdf"})
            write_summary()
            continue
        if args.skip_existing and artifact_complete(local_out, number):
            update_episode_state(
                state_path,
                guid,
                {"transcription_status": "skipped_existing", "local_raw_path": str(local_out / f"e{number:03d}-oai-raw.json")},
            )
            summary.append({"episode": number, "title": title, "status": "skipped_existing_artifact"})
            write_summary()
            continue
        if args.limit is not None and processed_missing >= args.limit:
            break
        if not episode.get("audio_url"):
            update_episode_state(state_path, guid, {"transcription_status": "error", "error": "missing_audio_url"})
            summary.append({"episode": number, "title": title, "status": "missing_audio_url"})
            write_summary()
            continue

        page_url = episode.get("episode_url") or episode.get("rss_link") or "https://bibleproject.com/podcast/"
        audio_url = episode["audio_url"]
        remote_audio = remote_out / f"bibleproject{number:03d}.mp3"
        episode_start = time.time()

        try:
            update_episode_state(
                state_path,
                guid,
                {
                    "transcription_status": "running",
                    "model": args.model,
                    "device": args.device,
                    "compute_type": args.compute_type,
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
            with tempfile.TemporaryDirectory(prefix="citara-bibleproject-audio-") as tmpdir:
                local_audio = Path(tmpdir) / f"e{number:03d}.mp3"
                dl_seconds = download(audio_url, local_audio)
                run(scp_args(args.ssh_key, str(local_audio), f"{args.worker}:{remote_audio}"), timeout=300)

                remote_cmd = " ".join(
                    [
                        shlex.quote(str(args.remote_root / "venv" / "bin" / "python")),
                        shlex.quote(str(args.remote_root / "remote_bibleproject_transcribe.py")),
                        "--episode",
                        shlex.quote(str(number)),
                        "--title",
                        shlex.quote(str(title)),
                        "--url",
                        shlex.quote(str(page_url)),
                        "--audio-url",
                        shlex.quote(str(audio_url)),
                        "--audio-path",
                        shlex.quote(str(remote_audio)),
                        "--duration-seconds",
                        shlex.quote(str(episode.get("duration_seconds") or 0)),
                        "--out-dir",
                        shlex.quote(str(remote_out)),
                        "--model",
                        shlex.quote(args.model),
                        "--device",
                        shlex.quote(args.device),
                        "--compute-type",
                        shlex.quote(args.compute_type),
                        "--beam-size",
                        shlex.quote(str(args.beam_size)),
                        "--best-of",
                        shlex.quote(str(args.best_of)),
                    ]
                )
                proc = run(ssh_args(args.ssh_key, args.worker, remote_cmd), timeout=args.transcribe_timeout_seconds)
                print(proc.stdout, flush=True)

            for suffix in ["oai-raw.json", "oai-raw-chunked.json", "transcribe-stats.json"]:
                atomic_scp_to_part(
                    args.ssh_key,
                    args.worker,
                    str(remote_out / f"e{number:03d}-{suffix}"),
                    local_out / f"e{number:03d}-{suffix}",
                )

            raw = local_out / f"e{number:03d}-oai-raw.json"
            chunked = local_out / f"e{number:03d}-oai-raw-chunked.json"
            stats_path = local_out / f"e{number:03d}-transcribe-stats.json"
            if try_load_json(raw) is None:
                raise RuntimeError("raw JSON validation failed")
            if try_load_json(chunked) is None:
                raise RuntimeError("chunked JSON validation failed")
            stats = try_load_json(stats_path)
            if not isinstance(stats, dict):
                raise RuntimeError("stats JSON validation failed")
            stats["local_download_seconds"] = dl_seconds
            stats["guid"] = guid
            stats["has_published_transcript"] = False
            stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n")

            run(
                ssh_args(
                    args.ssh_key,
                    args.worker,
                    f"rm -f {shlex.quote(str(remote_audio))} {shlex.quote(str(remote_out / f'e{number:03d}.mp3'))}",
                ),
                timeout=60,
            )

            update_episode_state(
                state_path,
                guid,
                {
                    "transcription_status": "transcribed",
                    "local_raw_path": str(raw),
                    "local_chunked_path": str(chunked),
                    "local_stats_path": str(stats_path),
                    "segments": stats.get("segments"),
                    "audio_hours": stats.get("audio_hours"),
                    "transcribe_seconds_per_audio_hour": stats.get("transcribe_seconds_per_audio_hour"),
                    "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
            summary.append({"episode": number, "title": title, "status": "transcribed", "stats": stats})
            successful.append(number)
            processed_missing += 1
            write_summary()
            sleep_after_episode(episode_start, number, "transcribed")
        except subprocess.CalledProcessError as exc:
            error = f"ssh/scp failed: {exc.returncode}: {exc.stdout[-2000:] if exc.stdout else ''}"
            update_episode_state(state_path, guid, {"transcription_status": "error", "error": error})
            summary.append({"episode": number, "title": title, "status": "error", "error": error})
            write_summary()
            sleep_after_episode(episode_start, number, "errored")
            continue
        except Exception as exc:
            update_episode_state(state_path, guid, {"transcription_status": "error", "error": str(exc)})
            summary.append({"episode": number, "title": title, "status": "error", "error": str(exc)})
            write_summary()
            sleep_after_episode(episode_start, number, "errored")
            continue

    print(
        json.dumps(
            {"summary_path": str(summary_path), "episodes": summary, "successful_episodes": successful}, indent=2, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
