#!/usr/bin/env python3
"""Transcribe one BEMA episode on a remote/worker host with faster-whisper.

This script is copied to the worker by the operator or by an orchestration script.
It writes OpenAI-like raw JSON, Citara's legacy chunked JSON, and timing stats,
then removes the temporary audio file from the worker.
"""
from __future__ import annotations

import argparse
import json
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
    return text.rstrip().endswith((".", "?", "!", '.”', '?"', '!"', ".'", "?'", "!'"))


def _choose_chunk_end(units: list[dict[str, object]], start: int, *, target_chars: int, max_chars: int) -> int:
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
    segments: list[dict[str, float | int | str]],
    *,
    episode: int,
    episode_url: str,
    target_chars: int = 1800,
    max_chars: int = 2400,
    overlap_chars: int = 250,
) -> list[dict[str, object]]:
    """Group Whisper segments into BEMA_az-style chunked artifacts.

    Raw fine-grained Whisper segments stay in `*-oai-raw.json`. The chunked
    artifact is the import/retrieval shape: sentence/segment-boundary chunks near
    target size, with small overlap from the previous chunk and metadata.start
    pointing at the first non-overlap segment.
    """
    units = [
        {"text": text, "start": float(segment.get("start") or 0.0)}
        for segment in segments
        if (text := _clean_text(segment.get("text")))
    ]
    chunks: list[dict[str, object]] = []
    primary_start = 0
    while primary_start < len(units):
        primary_end = _choose_chunk_end(units, primary_start, target_chars=target_chars, max_chars=max_chars)
        overlap_start = _overlap_start(units, primary_start, overlap_chars=overlap_chars)
        chunk_units = units[overlap_start:primary_end]
        primary_start_seconds = int(float(units[primary_start]["start"]))
        chunks.append(
            {
                "text": " ".join(str(unit["text"]) for unit in chunk_units),
                "metadata": {
                    "start": mmss(float(units[primary_start]["start"])),
                    "episode": episode,
                    "url": f" {episode_url}?t={primary_start_seconds} ",
                    "overlap_chars": 0 if overlap_start == primary_start else sum(len(str(unit["text"])) + 1 for unit in units[overlap_start:primary_start]),
                },
            }
        )
        primary_start = primary_end
    return chunks


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
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    audio_path = args.out_dir / f"e{int(args.episode):03d}.mp3"
    raw_path = args.out_dir / f"e{int(args.episode):03d}-oai-raw.json"
    chunked_path = args.out_dir / f"e{int(args.episode):03d}-oai-raw-chunked.json"
    stats_path = args.out_dir / f"e{int(args.episode):03d}-transcribe-stats.json"

    t0 = time.time()
    if args.audio_path:
        shutil.copy2(args.audio_path, audio_path)
        download_seconds = 0.0
        downloaded_on_worker = False
    else:
        request = urllib.request.Request(args.audio_url, headers={"User-Agent": "citara-worker/0.1"})
        with urllib.request.urlopen(request, timeout=900) as response:
            audio_path.write_bytes(response.read())
        download_seconds = time.time() - t0
        downloaded_on_worker = True
    audio_bytes = audio_path.stat().st_size

    model_t0 = time.time()
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    model_load_seconds = time.time() - model_t0

    tx_t0 = time.time()
    segments_iter, info = model.transcribe(str(audio_path), vad_filter=True)
    segments = []
    text_parts = []
    for index, segment in enumerate(segments_iter):
        text = segment.text.strip()
        if not text:
            continue
        segments.append({"id": index, "start": float(segment.start), "end": float(segment.end), "text": text})
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
        },
    }
    raw_path.write_text(json.dumps(raw_doc, indent=2, ensure_ascii=False) + "\n")

    chunked = build_legacy_chunked(segments, episode=int(args.episode), episode_url=args.url)
    chunked_path.write_text(json.dumps(chunked, indent=2, ensure_ascii=False) + "\n")

    if audio_path.exists():
        audio_path.unlink()

    wall_seconds = time.time() - t0
    audio_duration_seconds = float(getattr(info, "duration", None) or args.duration_seconds or 0)
    stats = {
        "episode": int(args.episode),
        "title": args.title,
        "model": args.model,
        "device": args.device,
        "compute_type": args.compute_type,
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
        "audio_removed": not audio_path.exists(),
    }
    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
