#!/usr/bin/env python3
"""Discover a YouTube playlist and build a transcription queue manifest.

The pipeline is deliberately split in two so only the first step touches the network:

    discover     yt-dlp --flat-playlist -> discovery.json (raw, replayable)
    build-queue  discovery.json         -> approved-queue.json (validated manifest)

The resulting manifest is consumed unchanged by ``transcribe_podcast_remote_batch.py``,
which recognises ``audio_fetch: "yt-dlp"`` items and re-resolves their audio at fetch
time. YouTube's own captions are never used: they are unpunctuated ASR output with
rolling-window duplication, which defeats sentence-aware chunking.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from artifact_paths import source_artifact_root

SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
# faster-whisper feeds initial_prompt in as prior tokens and truncates at roughly half the
# context window, so an over-long glossary silently loses its tail.
INITIAL_PROMPT_WORD_BUDGET = 160


def slugify(value: str, *, max_len: int = 60) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug[:max_len].strip("-")


def run_yt_dlp(argv: list[str], *, timeout: int = 900) -> str:
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not on PATH; install it with `uv sync --extra dev` and re-run via `uv run`.")
    process = subprocess.run(
        ["yt-dlp", *argv],
        check=True,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return process.stdout


def run_yt_dlp_tolerant(argv: list[str], *, timeout: int = 1800) -> str:
    """Run yt-dlp where per-item failures are expected and reported via --ignore-errors.

    yt-dlp exits non-zero when any item fails, so the caller must inspect the emitted
    records rather than the exit status.
    """
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not on PATH; install it with `uv sync --extra dev` and re-run via `uv run`.")
    process = subprocess.run(
        ["yt-dlp", *argv],
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return process.stdout


def verify_entries(playlist_url: str) -> dict[str, dict[str, Any]]:
    """Resolve every video page to learn which entries are actually playable.

    The flat listing reports a duration even for deleted or private videos and leaves
    ``availability`` null, so it cannot be trusted to build a queue on its own.
    """
    raw = run_yt_dlp_tolerant(["--skip-download", "--dump-json", "--ignore-errors", playlist_url])
    resolved: dict[str, dict[str, Any]] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        video_id = record.get("id")
        if not video_id:
            continue
        resolved[video_id] = {
            "duration_seconds": record.get("duration"),
            "upload_date": record.get("upload_date"),
            "channel": record.get("channel"),
            "has_manual_subtitles": bool({k: v for k, v in (record.get("subtitles") or {}).items() if k != "live_chat"}),
            "has_auto_captions": bool(record.get("automatic_captions")),
        }
    return resolved


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def discover(playlist_url: str, out_dir: Path, *, verify: bool = True) -> dict[str, Any]:
    """Dump playlist entries, then resolve each video to confirm it is playable."""
    raw = run_yt_dlp(["--flat-playlist", "--dump-single-json", playlist_url])
    payload = json.loads(raw)
    entries = [entry for entry in (payload.get("entries") or []) if isinstance(entry, dict)]
    resolved = verify_entries(playlist_url) if verify else {}
    discovery = {
        "playlist_url": playlist_url,
        "playlist_id": payload.get("id"),
        "playlist_title": payload.get("title"),
        "channel": payload.get("channel") or payload.get("uploader"),
        "channel_url": payload.get("channel_url"),
        "entry_count": len(entries),
        "verified": verify,
        "entries": [
            {
                "position": index,
                "video_id": entry.get("id"),
                "title": entry.get("title"),
                # Prefer the resolved duration: the flat listing's value survives deletion.
                "duration_seconds": (resolved.get(entry.get("id"), {}).get("duration_seconds") or entry.get("duration")),
                "resolvable": (entry.get("id") in resolved) if verify else None,
                **{k: v for k, v in resolved.get(entry.get("id"), {}).items() if k != "duration_seconds"},
            }
            for index, entry in enumerate(entries, start=1)
        ],
    }
    write_json_atomic(out_dir / "discovery.json", discovery)
    return discovery


def derive_glossary(titles: list[str], extra_terms: list[str]) -> str:
    """Build a vocabulary hint from terms that actually occur in the playlist.

    Whisper biases decoding toward the prompt's vocabulary, so feeding it the real
    parsha names is what keeps transliterations out of the transcript as guesswork.
    """
    parsha_names: list[str] = []
    for title in titles:
        match = re.match(r"\s*Parshat\s+([A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-]*)?)\s*[:\-]", title)
        if match:
            name = match.group(1).strip()
            if name not in parsha_names:
                parsha_names.append(name)

    terms = [*extra_terms, *(f"Parshat {name}" for name in parsha_names)]
    prompt = ". ".join(terms) + "."
    words = prompt.split()
    if len(words) > INITIAL_PROMPT_WORD_BUDGET:
        prompt = " ".join(words[:INITIAL_PROMPT_WORD_BUDGET])
        print(
            f"Warning: glossary truncated to {INITIAL_PROMPT_WORD_BUDGET} words to stay inside faster-whisper's initial_prompt budget.",
            file=sys.stderr,
        )
    return prompt


def build_queue(
    discovery: dict[str, Any],
    *,
    corpus_slug: str,
    remote_namespace: str,
    show_title: str,
    publisher: str | None,
    collection: str | None,
    tags: list[str],
    entities: list[dict[str, Any]],
    glossary_terms: list[str],
    language: str = "en",
) -> dict[str, Any]:
    for name, value in (("corpus_slug", corpus_slug), ("remote_namespace", remote_namespace)):
        if not SAFE_NAME.fullmatch(value):
            raise ValueError(f"{name} must be a safe lowercase name, got {value!r}")

    episodes: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen_video_ids: set[str] = set()

    for entry in discovery["entries"]:
        video_id = entry.get("video_id")
        title = (entry.get("title") or "").strip()
        duration = entry.get("duration_seconds")
        canonical_url = WATCH_URL.format(video_id=video_id)
        base = {
            "video_id": video_id,
            "title": title,
            "canonical_url": canonical_url,
            "playlist_position": entry.get("position"),
        }

        if not video_id or not title:
            excluded.append({**base, "exclusion_reason": "missing_video_id_or_title"})
            continue
        # Deleted and private videos keep their flat-listing duration, so only the
        # verification pass can rule them out.
        if entry.get("resolvable") is False:
            excluded.append({**base, "exclusion_reason": "video_unavailable"})
            continue
        if not isinstance(duration, (int, float)) or duration <= 0:
            excluded.append({**base, "exclusion_reason": "missing_duration"})
            continue
        # A playlist may legitimately list the same video twice. Keeping both would emit
        # duplicate guids, which the orchestrator rejects with a far less obvious message.
        if video_id in seen_video_ids:
            excluded.append({**base, "exclusion_reason": "duplicate_in_playlist"})
            continue
        seen_video_ids.add(video_id)

        number = len(episodes) + 1
        # artifact_stem becomes a filename and must satisfy the orchestrator's lowercase
        # SAFE_NAME rule, but YouTube IDs are case-sensitive, so fold for the filename only.
        stem = f"q{number:03d}-yt-{video_id.lower()}"
        if not SAFE_NAME.fullmatch(stem):
            raise ValueError(f"video {video_id} yields an unsafe artifact_stem {stem!r}")

        episodes.append(
            {
                "queue_number": number,
                "episode_label": f"Video{entry['position']:02d}",
                "guid": f"youtube-{video_id}",
                "title": title,
                "audio_url": canonical_url,
                "audio_fetch": "yt-dlp",
                "canonical_url": canonical_url,
                "duration_seconds": int(duration),
                "artifact_stem": stem,
                "playlist_position": entry["position"],
                "external_ids": {"youtube_video_id": video_id},
            }
        )

    if not episodes:
        raise ValueError("no usable videos found in the discovery payload")

    return {
        "schema_version": 1,
        "corpus_slug": corpus_slug,
        "remote_namespace": remote_namespace,
        "approval_status": "approved",
        "approved_episode_count": len(episodes),
        "source_tree_type": "video_channel",
        "item_type": "youtube_video",
        "provider": "youtube",
        "language": language,
        "show_title": show_title,
        "publisher": publisher,
        "collection": collection,
        "playlist_url": discovery["playlist_url"],
        "playlist_id": discovery.get("playlist_id"),
        "channel": discovery.get("channel"),
        "channel_url": discovery.get("channel_url"),
        "caption_policy": "ignore_youtube_captions",
        "caption_policy_reason": (
            "Playlist has no uploader-provided captions; auto-captions are unpunctuated and duplicated across rolling cue windows."
        ),
        "initial_prompt": derive_glossary([item["title"] for item in episodes], glossary_terms),
        "tags": tags,
        "entities": entities,
        "episodes": episodes,
        "excluded_episodes": excluded,
        "total_duration_seconds": sum(item["duration_seconds"] for item in episodes),
    }


def load_source_config(name: str, config_path: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for entry in config.get("sources", []):
        if entry.get("name") == name:
            return entry
    raise SystemExit(f"source {name!r} not found in {config_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["discover", "build-queue", "all"])
    parser.add_argument("--source", required=True, help="Source name in citara.sources.json")
    parser.add_argument("--config", type=Path, default=SCRIPT_DIR.parent / "citara.sources.json")
    parser.add_argument("--artifact-root", type=Path, default=None)
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip the per-video resolve pass. Faster, but deleted videos will enter the queue.",
    )
    args = parser.parse_args()

    source = load_source_config(args.source, args.config)
    slug = source["slug"]
    artifact_root = args.artifact_root or source_artifact_root()
    out_dir = artifact_root / slug / "remote-openai"

    if args.command in {"discover", "all"}:
        discovery = discover(source["playlist_url"], out_dir, verify=not args.skip_verify)
        unresolvable = sum(1 for entry in discovery["entries"] if entry.get("resolvable") is False)
        print(f"Discovered {discovery['entry_count']} entries from {discovery['playlist_title']!r} ({unresolvable} unresolvable)")
    else:
        discovery = json.loads((out_dir / "discovery.json").read_text(encoding="utf-8"))

    if args.command in {"build-queue", "all"}:
        manifest = build_queue(
            discovery,
            corpus_slug=slug,
            remote_namespace=f"{slug}-approved",
            show_title=source.get("show_title") or discovery.get("playlist_title") or slug,
            publisher=source.get("publisher"),
            collection=source.get("collection"),
            tags=source.get("tags", []),
            entities=source.get("entities", []),
            glossary_terms=source.get("glossary_terms", []),
        )
        write_json_atomic(out_dir / "approved-queue.json", manifest)
        hours = manifest["total_duration_seconds"] / 3600
        print(
            f"Queued {manifest['approved_episode_count']} videos "
            f"({hours:.1f}h, {len(manifest['excluded_episodes'])} excluded) -> {out_dir / 'approved-queue.json'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
