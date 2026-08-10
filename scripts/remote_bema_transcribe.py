#!/usr/bin/env python3
"""Transcribe one BEMA episode on a remote/worker host with faster-whisper.

Writes (for episode N):
- eNNN-oai-raw.json
- eNNN-oai-raw-chunked.json
- eNNN-transcribe-stats.json

Robustness improvements:
- per-episode remote lock file to prevent duplicate concurrent runs
- atomic JSON writes (write *.tmp then os.replace)
- stats includes `complete: true` written as part of the final atomic commit
- prints stats JSON to stdout (useful for orchestrator logs)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import urllib.request
from pathlib import Path

from faster_whisper import WhisperModel


def mmss(seconds: float) -> str:
    seconds_i = max(int(seconds), 0)
    return f"{seconds_i // 60:02d}{seconds_i % 60:02d}"


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith((".", "?", "!", ".”", '?"', '!"', ".'", "?'", "!'"))


def _choose_chunk_end(
    units: list[dict[str, object]],
    start: int,
    *,
    target_chars: int,
    max_chars: int,
) -> int:
    """Return exclusive end index, preferring sentence-like segment boundaries."""

    total = 0
    best_sentence_after_min: int | None = None
    best_sentence_before_target: int | None = None
    fallback_after_target: int | None = None
    min_chars = max(400, int(target_chars * 0.65))

    for index in range(start, len(units)):
        text = str(units[index]["text"])
        next_total = total + len(text) + (1 if total else 0)
        if next_total > max_chars and index > start:
            return best_sentence_after_min or best_sentence_before_target or fallback_after_target or index

        total = next_total
        end = index + 1

        if total >= target_chars and fallback_after_target is None:
            fallback_after_target = end

        if _ends_sentence(text):
            if total <= target_chars:
                best_sentence_before_target = end
            if total >= min_chars:
                best_sentence_after_min = end
                if total >= target_chars:
                    return end

    return len(units)


def _overlap_start(units: list[dict[str, object]], primary_start: int, *, overlap_chars: int) -> int:
    if primary_start <= 0 or overlap_chars <= 0:
        return primary_start

    total = 0
    index = primary_start
    while index > 0:
        candidate = str(units[index - 1]["text"])
        if total and total + len(candidate) + 1 > overlap_chars:
            break
        total += len(candidate) + (1 if total else 0)
        index -= 1

    return index


def build_legacy_chunked(
    segments: list[dict[str, object]],
    *,
    episode: int,
    episode_url: str,
    target_chars: int = 1800,
    max_chars: int = 2400,
    overlap_chars: int = 250,
) -> list[dict[str, object]]:
    """Group Whisper segments into legacy-style chunked artifacts.

    Anchor logic: chunk metadata.start uses the **first word onset** when available,
    else falls back to the segment start.
    """

    units = [
        {
            "text": text,
            "start": float(segment.get("start") or 0.0),
            "word_start": (float((segment.get("words") or [])[0].get("start") or 0.0) if (segment.get("words") or []) else None),
        }
        for segment in segments
        if (text := _clean_text(segment.get("text")))
    ]

    chunks: list[dict[str, object]] = []
    primary_start = 0
    while primary_start < len(units):
        primary_end = _choose_chunk_end(
            units,
            primary_start,
            target_chars=target_chars,
            max_chars=max_chars,
        )
        overlap_start = _overlap_start(units, primary_start, overlap_chars=overlap_chars)
        chunk_units = units[overlap_start:primary_end]

        word_start = units[primary_start].get("word_start")
        anchor_seconds = word_start if word_start is not None else units[primary_start]["start"]
        anchor_seconds_i = int(float(anchor_seconds))

        chunks.append(
            {
                "text": " ".join(str(unit["text"]) for unit in chunk_units),
                "metadata": {
                    "start": mmss(float(anchor_seconds)),
                    "episode": episode,
                    "url": f" {episode_url}?t={anchor_seconds_i} ",
                    "overlap_chars": 0
                    if overlap_start == primary_start
                    else sum(len(str(unit["text"])) + 1 for unit in units[overlap_start:primary_start]),
                },
            }
        )

        primary_start = primary_end

    return chunks


def write_json_atomic(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    # write via temp then os.replace
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp, path)


def acquire_lock(out_dir: Path, episode: int, *, ttl_seconds: int = 24 * 3600) -> tuple[Path, int] | None:
    lock_path = out_dir / f"e{int(episode):03d}.lock"

    # stale lock cleanup
    if lock_path.exists():
        age = time.time() - lock_path.stat().st_mtime
        if age > ttl_seconds:
            try:
                lock_path.unlink()
            except Exception:
                pass

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return lock_path, os.getpid()
    except FileExistsError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe one BEMA episode with faster-whisper")
    parser.add_argument("--episode", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--audio-url", required=True)
    parser.add_argument("--audio-path", type=Path, help="Use an already-staged audio file instead of downloading on the worker")
    parser.add_argument("--duration-seconds", type=float, default=0)
    parser.add_argument("--out-dir", type=Path, default=Path("/opt/citara-worker/bema/out"))
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--best-of", type=int, default=1, help="Reduce CPU memory/time (default faster-whisper is 5).")
    parser.add_argument(
        "--initial-prompt",
        default=None,
        help="Vocabulary hint biasing decoding toward domain terms (names, transliterations).",
    )
    args = parser.parse_args()

    episode_i = int(args.episode)
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    lock = acquire_lock(out_dir, episode_i)
    if lock is None:
        # Another process is already handling this episode.
        # Exit non-zero so orchestrator can record the episode as error.
        raise SystemExit(2)

    t0 = time.time()
    audio_path = out_dir / f"e{episode_i:03d}.mp3"
    raw_path = out_dir / f"e{episode_i:03d}-oai-raw.json"
    chunked_path = out_dir / f"e{episode_i:03d}-oai-raw-chunked.json"
    stats_path = out_dir / f"e{episode_i:03d}-transcribe-stats.json"

    download_seconds: float = 0.0
    downloaded_on_worker: bool = True
    audio_bytes: int = 0

    try:
        if args.audio_path:
            shutil.copy2(args.audio_path, audio_path)
            download_seconds = 0.0
            downloaded_on_worker = False
        else:
            request = urllib.request.Request(args.audio_url, headers={"User-Agent": "citara-worker/0.1"})
            with urllib.request.urlopen(request, timeout=900) as response:
                audio_path.write_bytes(response.read())
            download_seconds = time.time() - t0

        audio_bytes = audio_path.stat().st_size

        model_t0 = time.time()
        model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
        model_load_seconds = time.time() - model_t0

        tx_t0 = time.time()
        segments_iter, info = model.transcribe(
            str(audio_path),
            vad_filter=True,
            word_timestamps=True,
            beam_size=args.beam_size,
            best_of=args.best_of,
            initial_prompt=args.initial_prompt,
        )

        segments: list[dict[str, object]] = []
        text_parts: list[str] = []

        for index, segment in enumerate(segments_iter):
            text = (segment.text or "").strip()
            if not text:
                continue

            words: list[dict[str, float | str]] = []
            for w in getattr(segment, "words", None) or []:
                words.append(
                    {
                        "start": float(getattr(w, "start", 0.0) or 0.0),
                        "end": float(getattr(w, "end", 0.0) or 0.0),
                        "word": str(getattr(w, "word", "") or ""),
                    }
                )

            segments.append(
                {
                    "id": index,
                    "start": float(segment.start),
                    "end": float(segment.end),
                    "text": text,
                    "words": words,
                }
            )
            text_parts.append(text)

        transcribe_seconds = time.time() - tx_t0
        full_text = "\n".join(text_parts)

        raw_doc = {
            "text": full_text,
            "segments": segments,
            "language": getattr(info, "language", None),
            "language_probability": getattr(info, "language_probability", None),
            "duration": getattr(info, "duration", None),
            "metadata": {
                "episode": int(args.episode),
                "title": args.title,
                "url": args.url,
                "audio_url": args.audio_url,
                "model": args.model,
                "device": args.device,
                "compute_type": args.compute_type,
                "initial_prompt": args.initial_prompt,
            },
        }

        # build chunked with first word onset anchor
        chunked = build_legacy_chunked(segments, episode=episode_i, episode_url=args.url)

        # Atomic writes: raw + chunked then stats (complete marker)
        write_json_atomic(raw_path, raw_doc)
        write_json_atomic(chunked_path, chunked)

        wall_seconds = time.time() - t0
        audio_duration_seconds = float(getattr(info, "duration", None) or args.duration_seconds or 0)

        stats = {
            "episode": episode_i,
            "title": args.title,
            "model": args.model,
            "device": args.device,
            "compute_type": args.compute_type,
            "initial_prompt": args.initial_prompt,
            "downloaded_on_worker": downloaded_on_worker,
            "download_seconds": download_seconds,
            "model_load_seconds": model_load_seconds,
            "transcribe_seconds": transcribe_seconds,
            "wall_seconds": wall_seconds,
            "audio_duration_seconds": audio_duration_seconds,
            "audio_hours": audio_duration_seconds / 3600 if audio_duration_seconds else None,
            "transcribe_seconds_per_audio_hour": transcribe_seconds / (audio_duration_seconds / 3600) if audio_duration_seconds else None,
            "wall_seconds_per_audio_hour": wall_seconds / (audio_duration_seconds / 3600) if audio_duration_seconds else None,
            "audio_bytes": audio_bytes,
            "segments": len(segments),
            "raw_path": str(raw_path),
            "chunked_path": str(chunked_path),
            "audio_removed": False,  # filled in below
            "complete": True,
        }

        # delete audio before final stats commit is fine, but reflect it correctly
        if audio_path.exists():
            audio_path.unlink()
            stats["audio_removed"] = True

        write_json_atomic(stats_path, stats)

        print(json.dumps(stats, indent=2, ensure_ascii=False))

    finally:
        # Remove lock if we created it.
        if lock is not None:
            try:
                lock_path, _pid = lock
                if lock_path.exists():
                    lock_path.unlink()
            except Exception:
                pass


if __name__ == "__main__":
    main()
