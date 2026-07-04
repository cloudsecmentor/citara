#!/usr/bin/env python3
"""Orchestrate BEMA audio transcription on a remote worker.

The worker runs `scripts/remote_bema_transcribe.py`; this local script resolves episode
metadata from Citara's BEMA state file, downloads audio locally when worker media downloads
are unreliable, stages one MP3 at a time to the worker, copies transcript JSON back, and
removes audio from both machines.
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

DEFAULT_CITARA_ROOT = Path("../citara")
DEFAULT_REPO = Path("../hermes-knowledge-vault")
DEFAULT_WORKER = "user@worker.example.invalid"
DEFAULT_SSH_KEY = Path("~/.ssh/id_ed25519").expanduser()
DEFAULT_REMOTE_ROOT = Path("/opt/citara-worker")


def run(argv: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(shlex.quote(a) for a in argv), flush=True)
    return subprocess.run(argv, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)


def ssh_args(key: Path, worker: str, remote_cmd: str) -> list[str]:
    return ["ssh", "-i", str(key), worker, remote_cmd]


def scp_args(key: Path, *args: str) -> list[str]:
    return ["scp", "-i", str(key), *args]


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
    with urllib.request.urlopen(request, timeout=900) as response:
        dest.write_bytes(response.read())
    return time.time() - t0


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
    parser.add_argument("--skip-existing", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--import-after", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    state_path = args.citara_root / "import-state" / "bema_pipeline_state.json"
    remote_out = args.remote_root / "bema" / "out"
    local_out = args.citara_root / "source-artifacts" / "bema" / "remote-openai"
    local_out.mkdir(parents=True, exist_ok=True)

    run(scp_args(args.ssh_key, str(args.repo / "scripts" / "remote_bema_transcribe.py"), f"{args.worker}:{args.remote_root}/remote_bema_transcribe.py"), timeout=120)
    run(ssh_args(args.ssh_key, args.worker, f"mkdir -p {shlex.quote(str(remote_out))}"), timeout=60)

    episodes = episode_map(load_state(state_path))
    summary: list[dict[str, Any]] = []
    for number in range(args.start, args.end + 1):
        episode = episodes.get(number)
        if not episode:
            summary.append({"episode": number, "status": "missing_state"})
            continue
        if args.skip_existing and (local_out / f"e{number:03d}-transcribe-stats.json").exists():
            summary.append({"episode": number, "status": "skipped_existing_artifact"})
            continue
        audio_url = episode.get("audio_url")
        if not audio_url:
            summary.append({"episode": number, "status": "missing_audio_url"})
            continue
        title = episode.get("episode_title") or episode.get("title") or f"BEMA {number}"
        page_url = episode.get("episode_url") or f"https://www.bemadiscipleship.com/{number}"
        with tempfile.TemporaryDirectory(prefix="citara-bema-audio-") as tmpdir:
            local_audio = Path(tmpdir) / f"e{number:03d}.mp3"
            dl_seconds = download(audio_url, local_audio)
            remote_audio = remote_out / f"bema{number:03d}.mp3"
            run(scp_args(args.ssh_key, str(local_audio), f"{args.worker}:{remote_audio}"), timeout=300)
        remote_cmd = " ".join(
            [
                shlex.quote(str(args.remote_root / "venv" / "bin" / "python")),
                shlex.quote(str(args.remote_root / "remote_bema_transcribe.py")),
                "--episode", shlex.quote(str(number)),
                "--title", shlex.quote(str(title)),
                "--url", shlex.quote(str(page_url)),
                "--audio-url", shlex.quote(str(audio_url)),
                "--audio-path", shlex.quote(str(remote_audio)),
                "--duration-seconds", shlex.quote(str(episode.get("duration_seconds") or 0)),
                "--model", shlex.quote(args.model),
                "--device", shlex.quote(args.device),
                "--compute-type", shlex.quote(args.compute_type),
            ]
        )
        proc = run(ssh_args(args.ssh_key, args.worker, remote_cmd), timeout=7200)
        print(proc.stdout, flush=True)
        remote_files = [
            f"{args.worker}:{remote_out}/e{number:03d}-oai-raw.json",
            f"{args.worker}:{remote_out}/e{number:03d}-oai-raw-chunked.json",
            f"{args.worker}:{remote_out}/e{number:03d}-transcribe-stats.json",
        ]
        run(scp_args(args.ssh_key, *remote_files, str(local_out) + "/"), timeout=300)
        run(ssh_args(args.ssh_key, args.worker, f"rm -f {shlex.quote(str(remote_audio))} {shlex.quote(str(remote_out / f'e{number:03d}.mp3'))}"), timeout=60)
        stats = json.loads((local_out / f"e{number:03d}-transcribe-stats.json").read_text())
        stats["local_download_seconds"] = dl_seconds
        (local_out / f"e{number:03d}-transcribe-stats.json").write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n")
        summary.append({"episode": number, "status": "transcribed", "stats": stats})
        (local_out / f"batch-{args.start}-{args.end}-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")

    if args.import_after:
        env = os.environ.copy()
        env.update(
            {
                "DATABASE_URL": f"sqlite:///{args.citara_root / 'citara.db'}",
                "SOURCE_ARTIFACT_ROOT": str(args.citara_root / "source-artifacts"),
                "SOURCE_STATE_ROOT": str(args.citara_root / "import-state"),
                "OBJECT_STORE_PATH": str(args.citara_root / "object-store"),
            }
        )
        cmd = [
            "uv", "run", "python", "scripts/import_bema_artifacts.py",
            "--skip-published-pages",
            "--rewrite-openai-chunked",
            "--rewrite-start", str(args.start),
            "--rewrite-end", str(args.end),
            "--replace-generated-openai",
            "--openai-raw", str(local_out),
        ]
        print("$", " ".join(shlex.quote(a) for a in cmd), flush=True)
        subprocess.run(cmd, check=True, cwd=args.repo, env=env)

    print(json.dumps({"summary_path": str(local_out / f"batch-{args.start}-{args.end}-summary.json"), "episodes": summary}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise
