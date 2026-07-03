#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from hermes_knowledge.core.config import settings
from hermes_knowledge.core.paths import source_artifact_root, source_state_root

DEFAULT_FEED_URL = "https://feeds.simplecast.com/3NVmUWZO"
DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_STATE = source_state_root() / "bibleproject_pipeline_state.json"
DEFAULT_ARTIFACT_DIR = source_artifact_root() / "bibleproject"
USER_AGENT = "hermes-knowledge-vault/0.1 (+BibleProject import pipeline)"

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"


def fetch_bytes(url: str, *, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_text(url: str, *, timeout: int = 60) -> str:
    return fetch_bytes(url, timeout=timeout).decode("utf-8", "ignore")


def _strip_cdata(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", value, flags=re.S)
    return html.unescape(value)


def clean_html_text(value: str | None) -> str:
    value = _strip_cdata(value)
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(value).split())


def seconds_from_duration(value: str) -> int:
    value = (value or "").strip()
    if not value:
        return 0
    parts = value.split(":")
    if not all(part.isdigit() for part in parts):
        return 0
    numbers = [int(part) for part in parts]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    if len(numbers) == 1:
        return numbers[0]
    return 0


def extract_transcript_links(description_html: str) -> list[str]:
    links: list[str] = []
    description_html = _strip_cdata(description_html)
    for match in re.finditer(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', description_html, re.S | re.I):
        href = html.unescape(match.group(1))
        label = clean_html_text(match.group(2))
        blob = f"{href} {label}".lower()
        href_lower = href.lower()
        label_lower = label.lower()
        if "transcript" not in label_lower and not ("transcript" in href_lower and ".pdf" in href_lower):
            continue
        if not (href_lower.endswith(".pdf") or ".pdf" in href_lower or "transcript" in href_lower):
            continue
        if href not in links:
            links.append(href)
    return links


def _item_text(item: ET.Element, tag: str) -> str:
    value = item.findtext(tag)
    if value is None and tag.startswith("itunes:"):
        value = item.findtext(f"{{{ITUNES_NS}}}{tag.split(':', 1)[1]}")
    return clean_html_text(value)


def _item_raw_text(item: ET.Element, tag: str) -> str:
    child = item.find(tag)
    if child is None and tag.startswith("itunes:"):
        child = item.find(f"{{{ITUNES_NS}}}{tag.split(':', 1)[1]}")
    return child.text or "" if child is not None else ""


def parse_rss_items(rss_text: str) -> tuple[str, list[dict[str, Any]]]:
    root = ET.fromstring(rss_text)
    channel = root.find("channel")
    if channel is None:
        return "", []
    show_title = clean_html_text(channel.findtext("title")) or "BibleProject"
    items: list[dict[str, Any]] = []
    for item in channel.findall("item"):
        description_raw = _item_raw_text(item, "description") or _item_raw_text(item, "{http://purl.org/rss/1.0/modules/content/}encoded")
        transcript_links = extract_transcript_links(description_raw)
        enclosure = item.find("enclosure")
        audio_url = html.unescape(enclosure.attrib.get("url", "")) if enclosure is not None else ""
        duration = _item_text(item, "itunes:duration")
        link = _item_text(item, "link")
        show_notes = _first_show_notes_url(description_raw) or link
        guid = _item_text(item, "guid") or show_notes or _item_text(item, "title")
        items.append(
            {
                "show_title": show_title,
                "title": _item_text(item, "title") or "Untitled episode",
                "guid": guid,
                "episode_url": show_notes,
                "rss_link": link,
                "description": clean_html_text(description_raw),
                "duration": duration,
                "duration_seconds": seconds_from_duration(duration),
                "audio_url": audio_url,
                "transcript_url": transcript_links[0] if transcript_links else None,
                "transcript_urls": transcript_links,
            }
        )
    return show_title, items


def _first_show_notes_url(description_html: str) -> str | None:
    description_html = _strip_cdata(description_html)
    for match in re.finditer(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>', description_html, re.S | re.I):
        href = html.unescape(match.group(1))
        if "bibleproject.com/podcast" in href or "bibleproject.com/podcasts" in href:
            return href
    return None


def safe_filename(value: str, *, max_len: int = 120) -> str:
    value = html.unescape(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:max_len].strip("-") or "episode"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"episodes": {}}
    return json.loads(path.read_text())


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def episode_status(state_path: Path, guid: str, field: str) -> str | None:
    state = load_state(state_path)
    return state.get("episodes", {}).get(guid, {}).get(field)


def planned_next_episode(state_path: Path, episodes: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    state = load_state(state_path)
    for episode in episodes:
        status = state.get("episodes", {}).get(episode["guid"], {}).get(field)
        if status not in {"imported", "transcribed", "skipped_existing", "missing_transcript"}:
            return episode
    return None


def update_episode_state(state_path: Path, episode: dict[str, Any], **updates: Any) -> None:
    state = load_state(state_path)
    episodes = state.setdefault("episodes", {})
    entry = episodes.setdefault(episode["guid"], {})
    entry.update(
        {
            "title": episode.get("title"),
            "episode_url": episode.get("episode_url"),
            "audio_url": episode.get("audio_url"),
            "transcript_url": episode.get("transcript_url"),
            "duration_seconds": episode.get("duration_seconds"),
        }
    )
    entry.update(updates)
    save_state(state_path, state)


def discover(feed_url: str, state_path: Path) -> tuple[str, list[dict[str, Any]]]:
    rss_text = fetch_text(feed_url, timeout=120)
    show_title, episodes = parse_rss_items(rss_text)
    state = load_state(state_path)
    state["feed_url"] = feed_url
    state["show_title"] = show_title
    state["episode_count"] = len(episodes)
    state["published_transcript_count"] = sum(1 for episode in episodes if episode.get("transcript_url"))
    state.setdefault("episodes", {})
    for episode in episodes:
        entry = state["episodes"].setdefault(episode["guid"], {})
        entry.update(
            {
                "title": episode["title"],
                "episode_url": episode["episode_url"],
                "audio_url": episode["audio_url"],
                "transcript_url": episode["transcript_url"],
                "duration_seconds": episode["duration_seconds"],
                "has_published_transcript": bool(episode.get("transcript_url")),
            }
        )
    save_state(state_path, state)
    return show_title, episodes


def extract_pdf_text(pdf_path: Path) -> str:
    try:
        import fitz  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for PDF extraction. Install with: uv add pymupdf") from exc
    doc = fitz.open(pdf_path)
    parts = []
    for page in doc:
        text = page.get_text("text")
        if text.strip():
            parts.append(text)
    doc.close()
    return clean_transcript_text("\n".join(parts))


def clean_transcript_text(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    skip_patterns = [
        r"^BibleProject Podcast$",
        r"^Official Transcript$",
        r"^Transcript$",
        r"^Show Notes$",
        r"^Page \d+ of \d+$",
    ]
    cleaned = []
    for line in lines:
        if any(re.search(pattern, line, re.I) for pattern in skip_patterns):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def make_segments(text: str, duration_seconds: int) -> list[dict[str, Any]]:
    duration_ms = max(duration_seconds * 1000, 1)
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        units = re.split(r"(?<=[.!?])\s+", paragraph) if len(paragraph) > 1800 else [paragraph]
        for unit in units:
            unit = unit.strip()
            if not unit:
                continue
            if current and len(current) + len(unit) + 1 > 1200:
                chunks.append(current)
                current = unit
            else:
                current = f"{current} {unit}".strip() if current else unit
    if current:
        chunks.append(current)
    chunks = [chunk for chunk in chunks if len(chunk) >= 20]
    total_chars = max(sum(len(chunk) for chunk in chunks), 1)
    elapsed = 0
    segments = []
    for index, chunk in enumerate(chunks):
        start_ms = segments[-1]["end_ms"] if segments else 0
        if index == len(chunks) - 1:
            end_ms = duration_ms
        else:
            elapsed += len(chunk)
            end_ms = max(start_ms + 1000, int(duration_ms * elapsed / total_chars))
        segments.append({"start_ms": start_ms, "end_ms": end_ms, "speaker": None, "text": chunk})
    return segments


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode("utf-8"))


def patch_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode("utf-8"))


def list_api_sources(api_url: str) -> list[dict[str, Any]]:
    with urllib.request.urlopen(f"{api_url.rstrip('/')}/sources", timeout=60) as response:
        return json.loads(response.read().decode("utf-8")).get("sources", [])


def source_exists(api_url: str, title: str) -> bool:
    return any(source.get("title") == title for source in list_api_sources(api_url))


def annotate_source_metadata(source_id: str, metadata: dict[str, Any]) -> None:
    """Best-effort source metadata patch for local DB-backed runs.

    The public API currently exposes source weighting but not arbitrary metadata.
    When DATABASE_URL is available, this preserves transcript provenance for
    import artifacts without blocking API-only use.
    """
    database_url = os.getenv("DATABASE_URL", settings.database_url)
    if not database_url:
        return
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from hermes_knowledge.core.models import Source
    except Exception:
        return
    engine = create_engine(database_url, future=True)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        source = session.get(Source, source_id)
        if source is None:
            return
        merged = dict(source.metadata_json or {})
        merged.update(metadata)
        source.metadata_json = merged
        session.commit()


def import_published(episodes: list[dict[str, Any]], state_path: Path, artifact_dir: Path, api_url: str, limit: int | None = None) -> list[dict[str, Any]]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    imported: list[dict[str, Any]] = []
    processed = 0
    for episode in episodes:
        if not episode.get("transcript_url"):
            update_episode_state(state_path, episode, published_status="missing_transcript")
            continue
        if episode_status(state_path, episode["guid"], "published_status") in {"imported", "skipped_existing"}:
            continue
        title = f"BibleProject: {episode['title']} (Published Transcript)"
        if source_exists(api_url, title):
            update_episode_state(state_path, episode, published_status="skipped_existing", source_title=title)
            continue
        try:
            slug = safe_filename(episode["title"])
            pdf_path = artifact_dir / "pdf" / f"{slug}.pdf"
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            if not pdf_path.exists():
                pdf_path.write_bytes(fetch_bytes(episode["transcript_url"], timeout=180))
            text = extract_pdf_text(pdf_path)
            if len(text) < 500:
                raise RuntimeError(f"extracted transcript too short: {len(text)} chars")
            text_path = artifact_dir / "text" / f"{slug}.txt"
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(text)
            segments = make_segments(text, episode.get("duration_seconds") or 0)
            payload = {
                "show_title": episode.get("show_title") or "BibleProject",
                "episode_title": title,
                "episode_url": episode.get("episode_url"),
                "segments": segments,
            }
            payload_path = artifact_dir / "payloads" / f"{slug}.json"
            payload_path.parent.mkdir(parents=True, exist_ok=True)
            payload_path.write_text(json.dumps(payload, indent=2))
            response = post_json(f"{api_url.rstrip('/')}/sources/transcript", payload)
            annotate_source_metadata(
                response["source_id"],
                {
                    "show_title": episode.get("show_title") or "BibleProject",
                    "episode_guid": episode.get("guid"),
                    "episode_duration_seconds": episode.get("duration_seconds"),
                    "rss_feed_url": DEFAULT_FEED_URL,
                    "rss_link": episode.get("rss_link"),
                    "transcript_url": episode.get("transcript_url"),
                    "transcript_provenance": "published_pdf",
                    "input_type": "transcript_pdf",
                },
            )
            try:
                patch_json(
                    f"{api_url.rstrip('/')}/sources/{response['source_id']}/preference",
                    {"retrieval_weight": 1.0, "preference_label": "published"},
                )
            except Exception:
                pass
            result = {"source_title": title, "source_id": response.get("source_id"), "segments": len(segments), "pdf_path": str(pdf_path)}
            update_episode_state(state_path, episode, published_status="imported", **result)
            imported.append(result)
            processed += 1
            print(f"IMPORTED published transcript: {title} segments={len(segments)}", flush=True)
            if limit is not None and processed >= limit:
                break
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            update_episode_state(state_path, episode, published_status="error", error=str(exc))
            print(f"ERROR published import: {episode.get('title')}: {exc}", file=sys.stderr, flush=True)
    return imported


def download_audio(episode: dict[str, Any], artifact_dir: Path) -> Path:
    if not episode.get("audio_url"):
        raise RuntimeError("episode has no audio enclosure URL")
    suffix = Path(urllib.parse.urlparse(episode["audio_url"]).path).suffix or ".mp3"
    audio_path = artifact_dir / "audio" / f"{safe_filename(episode['title'])}{suffix}"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    if not audio_path.exists():
        audio_path.write_bytes(fetch_bytes(episode["audio_url"], timeout=600))
    return audio_path


def transcribe_with_faster_whisper(audio_path: Path, model: str, device: str, compute_type: str) -> tuple[str, list[dict[str, Any]]]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Install faster-whisper first: uv pip install faster-whisper") from exc
    whisper = WhisperModel(model, device=device, compute_type=compute_type)
    segments_iter, _info = whisper.transcribe(str(audio_path), vad_filter=True)
    segments: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for segment in segments_iter:
        text = segment.text.strip()
        if not text:
            continue
        text_parts.append(text)
        segments.append({"start_ms": int(segment.start * 1000), "end_ms": int(segment.end * 1000), "speaker": None, "text": text})
    return "\n".join(text_parts), segments


def transcribe_missing(
    episodes: list[dict[str, Any]],
    state_path: Path,
    artifact_dir: Path,
    api_url: str,
    model: str,
    device: str,
    compute_type: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    processed = 0
    for episode in episodes:
        if episode.get("transcript_url"):
            continue
        if episode_status(state_path, episode["guid"], "transcription_status") in {"transcribed", "skipped_existing"}:
            continue
        title = f"BibleProject: {episode['title']} (Generated Transcript)"
        if source_exists(api_url, title):
            update_episode_state(state_path, episode, transcription_status="skipped_existing", source_title=title)
            continue
        try:
            update_episode_state(state_path, episode, transcription_status="running")
            audio_path = download_audio(episode, artifact_dir)
            transcript_text, segments = transcribe_with_faster_whisper(audio_path, model=model, device=device, compute_type=compute_type)
            if len(transcript_text) < 500 or not segments:
                raise RuntimeError("generated transcript was unexpectedly short")
            slug = safe_filename(episode["title"])
            text_path = artifact_dir / "generated-text" / f"{slug}.txt"
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(transcript_text)
            payload = {
                "show_title": episode.get("show_title") or "BibleProject",
                "episode_title": title,
                "episode_url": episode.get("episode_url"),
                "segments": segments,
            }
            response = post_json(f"{api_url.rstrip('/')}/sources/transcript", payload)
            annotate_source_metadata(
                response["source_id"],
                {
                    "show_title": episode.get("show_title") or "BibleProject",
                    "episode_guid": episode.get("guid"),
                    "episode_duration_seconds": episode.get("duration_seconds"),
                    "rss_feed_url": DEFAULT_FEED_URL,
                    "rss_link": episode.get("rss_link"),
                    "audio_url": episode.get("audio_url"),
                    "transcript_provenance": "generated_faster_whisper",
                    "transcription_model": model,
                    "input_type": "generated_transcript",
                },
            )
            try:
                patch_json(
                    f"{api_url.rstrip('/')}/sources/{response['source_id']}/preference",
                    {"retrieval_weight": 0.9, "preference_label": "generated"},
                )
            except Exception:
                pass
            result = {"source_title": title, "source_id": response.get("source_id"), "segments": len(segments), "audio_path": str(audio_path)}
            update_episode_state(state_path, episode, transcription_status="transcribed", **result)
            results.append(result)
            processed += 1
            print(f"TRANSCRIBED {title} segments={len(segments)}", flush=True)
            if limit is not None and processed >= limit:
                break
        except KeyboardInterrupt:
            update_episode_state(state_path, episode, transcription_status="interrupted")
            raise
        except Exception as exc:
            update_episode_state(state_path, episode, transcription_status="error", error=str(exc))
            print(f"ERROR transcription: {episode.get('title')}: {exc}", file=sys.stderr, flush=True)
    return results


def print_status(state_path: Path, episodes: list[dict[str, Any]]) -> None:
    state = load_state(state_path)
    entries = state.get("episodes", {})
    published_total = sum(1 for episode in episodes if episode.get("transcript_url"))
    missing_total = len(episodes) - published_total
    published_imported = sum(1 for episode in episodes if entries.get(episode["guid"], {}).get("published_status") in {"imported", "skipped_existing"})
    generated_done = sum(1 for episode in episodes if entries.get(episode["guid"], {}).get("transcription_status") in {"transcribed", "skipped_existing"})
    next_published = planned_next_episode(state_path, [episode for episode in episodes if episode.get("transcript_url")], "published_status")
    next_generated = planned_next_episode(state_path, [episode for episode in episodes if not episode.get("transcript_url")], "transcription_status")
    print(json.dumps({
        "episodes": len(episodes),
        "published_transcript_episodes": published_total,
        "missing_transcript_episodes": missing_total,
        "published_imported_or_existing": published_imported,
        "generated_transcribed_or_existing": generated_done,
        "next_published": next_published["title"] if next_published else None,
        "next_missing_to_transcribe": next_generated["title"] if next_generated else None,
    }, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resumable BibleProject podcast transcript import/transcription pipeline.")
    parser.add_argument("command", choices=["discover", "status", "import-published", "transcribe-missing"])
    parser.add_argument("--feed-url", default=DEFAULT_FEED_URL)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--api", default=DEFAULT_API_URL)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--model", default="small", help="faster-whisper model for transcribe-missing")
    parser.add_argument("--device", default="cpu", help="faster-whisper device: cpu, cuda, auto")
    parser.add_argument("--compute-type", default="int8", help="faster-whisper compute type, e.g. int8, float16")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    show_title, episodes = discover(args.feed_url, args.state)
    if args.command == "discover":
        print(json.dumps({"show_title": show_title, "episodes": len(episodes), "with_published_transcripts": sum(1 for episode in episodes if episode.get("transcript_url"))}, indent=2))
    elif args.command == "status":
        print_status(args.state, episodes)
    elif args.command == "import-published":
        results = import_published(episodes, args.state, args.artifact_dir, args.api, limit=args.limit)
        print(json.dumps({"imported_this_run": len(results), "results": results[:5]}, indent=2))
    elif args.command == "transcribe-missing":
        results = transcribe_missing(
            episodes,
            args.state,
            args.artifact_dir,
            args.api,
            model=args.model,
            device=args.device,
            compute_type=args.compute_type,
            limit=args.limit,
        )
        print(json.dumps({"transcribed_this_run": len(results), "results": results[:5]}, indent=2))


if __name__ == "__main__":
    main()
