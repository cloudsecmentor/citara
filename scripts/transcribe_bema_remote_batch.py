#!/usr/bin/env python3
"""Orchestrate BEMA audio transcription on a remote worker.

High-level flow (per episode):
1) download MP3 locally (so we don't rely on worker media downloads)
2) stage MP3 on worker
3) run `remote_bema_transcribe.py` for that episode via SSH
4) copy back raw/chunked/stats JSON artifacts atomically (via .part)
5) validate JSON locally and only then delete the remote MP3
6) after the loop, import/rewrite artifacts into the local Citara DB for each successful episode.

Robustness improvements over the original version:
- per-episode try/except (one failure does not abort the whole batch)
- stronger skip logic (requires all three artifacts + valid JSON; prefer stats.complete)
- atomic artifact transfer (scp to *.part then rename)
- ssh/scp keepalive + connect timeout
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

DEFAULT_CITARA_ROOT = Path("../citara-data")
DEFAULT_REPO = Path("../citara")
DEFAULT_WORKER = "user@worker.example.invalid"
DEFAULT_SSH_KEY = Path("~/.ssh/id_ed25519").expanduser()
DEFAULT_REMOTE_ROOT = Path("/opt/citara-worker")

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
    # scp also supports -o ssh options
    return ["scp", "-i", str(key), *SSH_OPTS, *args]


def load_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def episode_map(state: dict[str, Any]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for guid, episode in state.get("episodes", {}).items():
        try:
            number = int(episode.get("episode"))
        except (TypeError, ValueError):
            continue
        episode = dict(episode)
        episode.setdefault("guid", guid)
        out[number] = episode
    return out


def download(url: str, dest: Path) -> float:
    t0 = time.time()
    request = urllib.request.Request(url, headers={"User-Agent": "citara-local-orchestrator/0.1"})
    # timeout=900 is the urllib socket read timeout
    with urllib.request.urlopen(request, timeout=900) as response:
        dest.write_bytes(response.read())
    return time.time() - t0


def try_load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def episode_complete(local_out: Path, number: int) -> bool:
    raw = local_out / f"e{number:03d}-oai-raw.json"
    chunked = local_out / f"e{number:03d}-oai-raw-chunked.json"
    stats = local_out / f"e{number:03d}-transcribe-stats.json"
    if not (raw.exists() and chunked.exists() and stats.exists()):
        return False

    # basic validation + prefer explicit completion marker
    if try_load_json(raw) is None:
        return False
    if try_load_json(chunked) is None:
        return False

    stats_data = try_load_json(stats)
    if not isinstance(stats_data, dict):
        return False

    if stats_data.get("complete") is True:
        return True

    # backward compatibility: older stats didn't have complete=true
    # treat as complete if we have non-empty segments and transcribe_seconds
    if isinstance(stats_data.get("segments"), int) and stats_data.get("segments", 0) > 0:
        return True

    return False


def atomic_scp_to_part(key: Path, worker: str, remote_path: str, local_final: Path) -> None:
    """scp remote_path -> local_final.part then rename to local_final"""
    local_part = local_final.with_suffix(local_final.suffix + ".part")

    # scp directly to part then validate later
    run(scp_args(key, f"{worker}:{remote_path}", str(local_part)), timeout=300)

    # rename
    if local_final.exists():
        local_final.unlink()
    os.replace(local_part, local_final)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe BEMA episodes on remote worker")
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--citara-root", type=Path, default=DEFAULT_CITARA_ROOT)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--worker", default=DEFAULT_WORKER)
    parser.add_argument("--ssh-key", type=Path, default=DEFAULT_SSH_KEY)
    parser.add_argument("--remote-root", type=Path, default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument(
        "--transcribe-timeout-seconds",
        type=int,
        default=21600,
        help="SSH timeout for each episode transcription on the worker (seconds)",
    )
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--best-of", type=int, default=1)
    parser.add_argument("--skip-existing", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--import-after", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    state_path = args.citara_root / "import-state" / "bema_pipeline_state.json"
    remote_out = args.remote_root / "bema" / "out"
    local_out = args.citara_root / "source-artifacts" / "bema" / "remote-openai"
    local_out.mkdir(parents=True, exist_ok=True)

    # Upload worker script + ensure remote output dir
    run(
        scp_args(
            args.ssh_key,
            str(args.repo / "scripts" / "remote_bema_transcribe.py"),
            f"{args.worker}:{args.remote_root}/remote_bema_transcribe.py",
        ),
        timeout=120,
    )
    run(ssh_args(args.ssh_key, args.worker, f"mkdir -p {shlex.quote(str(remote_out))}"), timeout=60)

    episodes = episode_map(load_state(state_path))

    summary: list[dict[str, Any]] = []
    successful_eps: list[int] = []
    summary_path = local_out / f"batch-{args.start}-{args.end}-summary.json"

    def write_summary() -> None:
        tmp = summary_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"episodes": summary}, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, summary_path)

    for number in range(args.start, args.end + 1):
        episode = episodes.get(number)
        if not episode:
            summary.append({"episode": number, "status": "missing_state"})
            write_summary()
            continue

        if args.skip_existing and episode_complete(local_out, number):
            summary.append({"episode": number, "status": "skipped_existing_artifact"})
            write_summary()
            continue

        audio_url = episode.get("audio_url")
        if not audio_url:
            summary.append({"episode": number, "status": "missing_audio_url"})
            write_summary()
            continue

        title = episode.get("episode_title") or episode.get("title") or f"BEMA {number}"
        page_url = episode.get("episode_url") or f"https://www.bemadiscipleship.com/{number}"

        try:
            with tempfile.TemporaryDirectory(prefix="citara-bema-audio-") as tmpdir:
                local_audio = Path(tmpdir) / f"e{number:03d}.mp3"
                dl_seconds = download(audio_url, local_audio)

                remote_audio = remote_out / f"bema{number:03d}.mp3"
                run(
                    scp_args(
                        args.ssh_key,
                        str(local_audio),
                        f"{args.worker}:{remote_audio}",
                    ),
                    timeout=300,
                )

                remote_cmd = " ".join(
                    [
                        shlex.quote(str(args.remote_root / "venv" / "bin" / "python")),
                        shlex.quote(str(args.remote_root / "remote_bema_transcribe.py")),
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

                proc = run(
                    ssh_args(args.ssh_key, args.worker, remote_cmd),
                    timeout=args.transcribe_timeout_seconds,
                )
                print(proc.stdout, flush=True)

            # Copy artifacts back (scp -> .part -> rename), then validate JSON
            artifact_map = [
                (
                    f"{args.worker}:{remote_out}/e{number:03d}-oai-raw.json",
                    local_out / f"e{number:03d}-oai-raw.json",
                ),
                (
                    f"{args.worker}:{remote_out}/e{number:03d}-oai-raw-chunked.json",
                    local_out / f"e{number:03d}-oai-raw-chunked.json",
                ),
                (
                    f"{args.worker}:{remote_out}/e{number:03d}-transcribe-stats.json",
                    local_out / f"e{number:03d}-transcribe-stats.json",
                ),
            ]

            for remote_spec, local_final in artifact_map:
                # remote_spec is "worker:path"; split it for atomic_scp
                worker, remote_path = remote_spec.split(":", 1)
                atomic_scp_to_part(args.ssh_key, worker, remote_path, local_final)

            if try_load_json(local_out / f"e{number:03d}-oai-raw.json") is None:
                raise RuntimeError("raw JSON validation failed")
            if try_load_json(local_out / f"e{number:03d}-oai-raw-chunked.json") is None:
                raise RuntimeError("chunked JSON validation failed")
            stats_data = try_load_json(local_out / f"e{number:03d}-transcribe-stats.json")
            if not isinstance(stats_data, dict):
                raise RuntimeError("stats JSON validation failed")

            stats_data["local_download_seconds"] = dl_seconds
            (local_out / f"e{number:03d}-transcribe-stats.json").write_text(json.dumps(stats_data, indent=2, ensure_ascii=False) + "\n")

            run(
                ssh_args(
                    args.ssh_key,
                    args.worker,
                    f"rm -f {shlex.quote(str(remote_audio))} {shlex.quote(str(remote_out / f'e{number:03d}.mp3'))}",
                ),
                timeout=60,
            )

            summary.append({"episode": number, "status": "transcribed", "stats": stats_data})
            successful_eps.append(number)
            write_summary()

        except subprocess.CalledProcessError as e:
            summary.append({"episode": number, "status": "error", "error": f"ssh/scp failed: {e.returncode}"})
            write_summary()
            continue
        except Exception as e:
            summary.append({"episode": number, "status": "error", "error": str(e)})
            write_summary()
            continue

    if args.import_after and successful_eps:
        # Import per-episode to avoid one missing episode aborting the whole range.
        env = os.environ.copy()
        env.update(
            {
                "DATABASE_URL": f"sqlite:///{args.citara_root / 'citara.db'}",
                "SOURCE_ARTIFACT_ROOT": str(args.citara_root / "source-artifacts"),
                "SOURCE_STATE_ROOT": str(args.citara_root / "import-state"),
                "OBJECT_STORE_PATH": str(args.citara_root / "object-store"),
            }
        )
        for n in successful_eps:
            cmd = [
                "uv",
                "run",
                "python",
                "scripts/import_bema_artifacts.py",
                "--skip-published-pages",
                "--rewrite-openai-chunked",
                "--rewrite-start",
                str(n),
                "--rewrite-end",
                str(n),
                "--replace-generated-openai",
                "--openai-raw",
                str(local_out),
            ]
            try:
                subprocess.run(cmd, check=True, cwd=args.repo, env=env, timeout=3600)
            except subprocess.CalledProcessError as e:
                print(f"Import failed for episode {n}: {e}", file=sys.stderr, flush=True)
            except subprocess.TimeoutExpired:
                print(f"Import timed out for episode {n}", file=sys.stderr, flush=True)

    # Final print (useful for cron/logging)
    print(
        json.dumps(
            {"summary_path": str(summary_path), "episodes": summary, "successful_episodes": successful_eps},
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise
