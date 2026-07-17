#!/usr/bin/env python3
from __future__ import annotations

import argparse
import email.utils
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from citara.core.config import settings
from citara.core.paths import source_artifact_root, source_state_root
from citara.core.source_taxonomy import TEXTINUS_ENTITIES, TEXTINUS_SOURCE_TREE_SLUG

DEFAULT_FEED_URL = "https://anchor.fm/s/7cd8d890/podcast/rss"
DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_STATE = source_state_root() / "textinus_pipeline_state.json"
DEFAULT_ARTIFACT_DIR = source_artifact_root() / "textinus"
USER_AGENT = "citara/0.1 (+Text in Us import pipeline)"
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"


def fetch_bytes(url: str, *, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_text(url: str, *, timeout: int = 60) -> str:
    return fetch_bytes(url, timeout=timeout).decode("utf-8", "ignore")


def clean_html_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", value, flags=re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(value).split())


def seconds_from_duration(value: str) -> int:
    parts = (value or "").strip().split(":")
    if not parts or not all(part.isdigit() for part in parts):
        return 0
    nums = [int(part) for part in parts]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    return 0


def _item_text(item: ET.Element, tag: str) -> str:
    value = item.findtext(tag)
    if value is None and tag.startswith("itunes:"):
        value = item.findtext(f"{{{ITUNES_NS}}}{tag.split(':', 1)[1]}")
    return clean_html_text(value)


def _item_raw(item: ET.Element, tag: str) -> str:
    child = item.find(tag)
    if child is None and tag.startswith("itunes:"):
        child = item.find(f"{{{ITUNES_NS}}}{tag.split(':', 1)[1]}")
    return child.text or "" if child is not None else ""


def _enclosure_url(item: ET.Element) -> str:
    enclosure = item.find("enclosure")
    return html.unescape(enclosure.attrib.get("url", "")) if enclosure is not None else ""


def parse_rss_items(rss_text: str) -> tuple[str, list[dict[str, Any]]]:
    root = ET.fromstring(rss_text)
    channel = root.find("channel")
    if channel is None:
        return "", []
    show_title = clean_html_text(channel.findtext("title")) or "Text in Us"
    items: list[dict[str, Any]] = []
    for item in channel.findall("item"):
        description = clean_html_text(_item_raw(item, "description"))
        duration = _item_text(item, "itunes:duration")
        link = _item_text(item, "link")
        title = _item_text(item, "title") or "Untitled episode"
        items.append(
            {
                "show_title": show_title,
                "title": title,
                "guid": _item_text(item, "guid") or link or title,
                "episode_url": link,
                "description": description,
                "duration": duration,
                "duration_seconds": seconds_from_duration(duration),
                "published": _item_text(item, "pubDate"),
                "audio_url": _enclosure_url(item),
                "transcript_url": None,
                "transcript_urls": [],
            }
        )
    return show_title, items


def episode_sort_key(episode: dict[str, Any]) -> tuple[str, str]:
    try:
        parsed = email.utils.parsedate_to_datetime(str(episode.get("published") or ""))
        return (parsed.isoformat(), str(episode.get("guid") or episode.get("title") or ""))
    except Exception:
        return ("9999-12-31T23:59:59+00:00", str(episode.get("guid") or episode.get("title") or ""))


def chronological_episodes(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(episode, episode_number=index) for index, episode in enumerate(sorted(episodes, key=episode_sort_key), start=1)]


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
    return load_state(state_path).get("episodes", {}).get(guid, {}).get(field)


def planned_next_episode(state_path: Path, episodes: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    state = load_state(state_path)
    for episode in episodes:
        if state.get("episodes", {}).get(episode["guid"], {}).get(field) not in {"imported", "transcribed", "skipped_existing", "missing_transcript"}:
            return episode
    return None


def update_episode_state(state_path: Path, episode: dict[str, Any], **updates: Any) -> None:
    state = load_state(state_path)
    state.setdefault("episodes", {}).setdefault(episode["guid"], {}).update({**episode, **updates})
    save_state(state_path, state)


def discover(feed_url: str, state_path: Path) -> tuple[str, list[dict[str, Any]]]:
    show_title, episodes = parse_rss_items(fetch_text(feed_url, timeout=120))
    state = load_state(state_path)
    state.update({"feed_url": feed_url, "show_title": show_title, "episode_count": len(episodes), "published_transcript_count": 0})
    state.setdefault("episodes", {})
    for episode in chronological_episodes(episodes):
        state["episodes"].setdefault(episode["guid"], {}).update({**episode, "has_published_transcript": False})
    save_state(state_path, state)
    return show_title, episodes


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode())


def patch_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}, method="PATCH")
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode())


def existing_source_id(title: str) -> str | None:
    database_url = os.getenv("DATABASE_URL", settings.database_url)
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import sessionmaker
        from citara.core.models import Source
    except Exception:
        return None
    try:
        engine = create_engine(database_url, future=True)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            source = session.execute(select(Source).where(Source.title == title)).scalar_one_or_none()
            return source.id if source else None
    except Exception:
        return None


def download_audio(episode: dict[str, Any], artifact_dir: Path) -> Path:
    if not episode.get("audio_url"):
        raise RuntimeError("episode has no audio enclosure URL")
    suffix = Path(urllib.parse.urlparse(episode["audio_url"]).path).suffix or ".m4a"
    path = artifact_dir / "audio" / f"{safe_filename(episode['title'])}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(fetch_bytes(episode["audio_url"], timeout=600))
    return path


def transcribe_with_faster_whisper(audio_path: Path, model: str, device: str, compute_type: str) -> tuple[str, list[dict[str, Any]]]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Install faster-whisper first: uv pip install faster-whisper") from exc
    whisper = WhisperModel(model, device=device, compute_type=compute_type)
    segments_iter, _ = whisper.transcribe(str(audio_path), vad_filter=True)
    parts, segments = [], []
    for segment in segments_iter:
        text = segment.text.strip()
        if text:
            parts.append(text)
            segments.append({"start_ms": int(segment.start * 1000), "end_ms": int(segment.end * 1000), "speaker": None, "text": text})
    return "\n".join(parts), segments


def transcribe_missing(episodes: list[dict[str, Any]], state_path: Path, artifact_dir: Path, api_url: str, model: str, device: str, compute_type: str, limit: int | None = None) -> list[dict[str, Any]]:
    results = []
    processed = 0
    for episode in episodes:
        if episode_status(state_path, episode["guid"], "transcription_status") in {"transcribed", "skipped_existing"}:
            continue
        try:
            title = f"Text in Us: {episode['title']} (Generated Transcript)"
            if source_id := existing_source_id(title):
                result = {"source_title": title, "source_id": source_id, "segments": None}
                update_episode_state(state_path, episode, transcription_status="skipped_existing", **result)
                results.append(result)
                processed += 1
                print(f"SKIPPED existing {title}", flush=True)
                if limit is not None and processed >= limit:
                    break
                continue
            update_episode_state(state_path, episode, transcription_status="running")
            audio = download_audio(episode, artifact_dir)
            text, segments = transcribe_with_faster_whisper(audio, model, device, compute_type)
            if not segments:
                raise RuntimeError("no transcript segments generated")
            text_path = artifact_dir / "generated-text" / f"{safe_filename(episode['title'])}.txt"
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(text)
            title = f"Text in Us: {episode['title']} (Generated Transcript)"
            payload = {
                "show_title": "Text in Us",
                "episode_title": title,
                "episode_url": episode["episode_url"],
                "segments": segments,
                "language": "en",
                "entities": TEXTINUS_ENTITIES,
                "metadata_json": {
                    "source_tree_slug": TEXTINUS_SOURCE_TREE_SLUG,
                    "source_tree_type": "podcast",
                    "source_item_id": episode.get("guid"),
                    "episode_guid": episode.get("guid"),
                    "transcript_provenance": "generated_faster_whisper",
                    "preference_label": "generated",
                    "retrieval_weight": 0.9,
                },
            }
            response = post_json(f"{api_url.rstrip('/')}/sources/transcript", payload)
            patch_json(f"{api_url.rstrip('/')}/sources/{response['source_id']}/preference", {"retrieval_weight": 0.9, "preference_label": "generated"})
            result = {"source_title": title, "source_id": response["source_id"], "segments": len(segments), "audio_path": str(audio)}
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
            print(f"ERROR transcription {episode.get('title')}: {exc}", file=sys.stderr, flush=True)
    return results


def print_status(state_path: Path, episodes: list[dict[str, Any]]) -> None:
    state = load_state(state_path)
    entries = state.get("episodes", {})
    print(json.dumps({
        "episodes": len(episodes),
        "published_transcript_episodes": 0,
        "missing_transcript_episodes": len(episodes),
        "generated_transcribed_or_existing": sum(1 for ep in entries.values() if ep.get("transcription_status") in {"transcribed", "skipped_existing"}),
        "next_missing_to_transcribe": (planned_next_episode(state_path, episodes, "transcription_status") or {}).get("title"),
    }, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resumable Text in Us podcast transcription pipeline.")
    parser.add_argument("command", choices=["discover", "status", "transcribe-missing"])
    parser.add_argument("--feed-url", default=DEFAULT_FEED_URL)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--api", default=DEFAULT_API_URL)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    show_title, episodes = discover(args.feed_url, args.state)
    if args.command == "discover":
        print(json.dumps({"show_title": show_title, "episodes": len(episodes), "published_transcript_episodes": 0, "missing_transcript_episodes": len(episodes)}, indent=2))
    elif args.command == "status":
        print_status(args.state, episodes)
    elif args.command == "transcribe-missing":
        results = transcribe_missing(episodes, args.state, args.artifact_dir, args.api, args.model, args.device, args.compute_type, args.limit)
        print(json.dumps({"transcribed_this_run": len(results), "results": results[:5]}, indent=2))


if __name__ == "__main__":
    main()
