#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from artifact_paths import source_artifact_root, source_state_root

DEFAULT_FEED_URL = "https://www.bemadiscipleship.com/rss"
DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_STATE = source_state_root() / "bema_pipeline_state.json"
DEFAULT_ARTIFACT_DIR = source_artifact_root() / "bema"
USER_AGENT = "hermes-knowledge-vault/0.1 (+BEMA import pipeline)"
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CURRENT_WEIGHT = 2.0
LEGACY_WEIGHT = 0.7


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


def _enclosure_url(item: ET.Element) -> str:
    enclosure = item.find("enclosure")
    return html.unescape(enclosure.attrib.get("url", "")) if enclosure is not None else ""


def parse_rss_items(rss_text: str) -> tuple[str, list[dict[str, Any]]]:
    root = ET.fromstring(rss_text)
    channel = root.find("channel")
    if channel is None:
        return "", []
    show_title = clean_html_text(channel.findtext("title")) or "The BEMA Podcast"
    episodes: list[dict[str, Any]] = []
    for item in channel.findall("item"):
        title = _item_text(item, "title") or "Untitled episode"
        link = _item_text(item, "link")
        episode = _item_text(item, "itunes:episode") or _episode_from_link(link) or title.split(":", 1)[0]
        season = _item_text(item, "itunes:season")
        duration = _item_text(item, "itunes:duration")
        guid = _item_text(item, "guid") or link or title
        episodes.append(
            {
                "show_title": show_title,
                "title": title,
                "episode_title": re.sub(rf"^{re.escape(str(episode))}\s*:\s*", "", title).strip() or title,
                "guid": guid,
                "episode": str(episode),
                "season": str(season) if season else None,
                "episode_url": link,
                "description": _item_text(item, "description"),
                "duration": duration,
                "duration_seconds": seconds_from_duration(duration),
                "audio_url": _enclosure_url(item),
            }
        )
    return show_title, episodes


def _episode_from_link(link: str) -> str | None:
    match = re.search(r"bemadiscipleship\.com/([^/?#]+)", link or "")
    return match.group(1) if match else None


def extract_transcript_links(page_html: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page_html, re.S | re.I):
        href = html.unescape(match.group(1))
        label = clean_html_text(match.group(2))
        if "docs.google.com" not in href or "transcript" not in f"{href} {label}".lower():
            continue
        context = clean_html_text(page_html[max(0, match.start() - 1200) : match.start() + 800]).lower()
        version = "legacy" if "legacy episode content" in context or "legacy" in context else "current"
        item = {"version": version, "url": href}
        if item not in links:
            links.append(item)
    if len(links) >= 2 and links[0]["version"] == links[1]["version"]:
        links[0]["version"] = "current"
        links[1]["version"] = "legacy"
    return links


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.skip += 1
        if not self.skip and tag in {"p", "br", "div", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1
        if not self.skip and tag in {"p", "div", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip and data.strip():
            self.parts.append(html.unescape(data))

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def extract_google_doc_text(url: str) -> str:
    parser = TextExtractor()
    parser.feed(fetch_text(url, timeout=90))
    lines = parser.text().splitlines()
    # Remove Google Docs chrome/status before the final repeated title or first speaker line.
    speaker_index = next((i for i, line in enumerate(lines) if re.search(r"^[A-Z][A-Za-z .’'\-]+:\s+", line)), None)
    if speaker_index is not None:
        lines = lines[speaker_index:]
    text = "\n".join(lines)
    text = re.sub(r"The BEMA Podcast is ©.*$", "", text, flags=re.I | re.S)
    text = re.sub(r"(?im)^Support The BEMA Podcast\s*$", "", text)
    text = re.sub(r"(?im)^← Previous episode.*$", "", text)
    text = text.strip()
    if len(text) < 300:
        raise RuntimeError(f"transcript too short from {url}: {len(text)} chars")
    return text


def safe_filename(value: str, *, max_len: int = 120) -> str:
    value = html.unescape(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:max_len].strip("-") or "episode"


def make_segments(text: str, duration_seconds: int) -> list[dict[str, Any]]:
    duration_ms = max(duration_seconds * 1000, 1)
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        units = re.split(r"(?<=[.!?])\s+", paragraph) if len(paragraph) > 1800 else [paragraph]
        for unit in units:
            if current and len(current) + len(unit) + 1 > 1000:
                chunks.append(current)
                current = unit
            else:
                current = f"{current} {unit}".strip() if current else unit
    if current:
        chunks.append(current)
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


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"episodes": {}, "versions": {}}
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


def version_status(state_path: Path, version_key: str) -> str | None:
    return load_state(state_path).get("versions", {}).get(version_key, {}).get("published_status")


def update_episode_state(state_path: Path, episode: dict[str, Any], **updates: Any) -> None:
    state = load_state(state_path)
    state.setdefault("episodes", {}).setdefault(episode["guid"], {}).update({**episode, **updates})
    save_state(state_path, state)


def update_version_state(state_path: Path, key: str, episode: dict[str, Any], transcript: dict[str, str], **updates: Any) -> None:
    state = load_state(state_path)
    state.setdefault("versions", {}).setdefault(key, {}).update({"episode": episode["episode"], "title": episode["title"], **transcript, **updates})
    save_state(state_path, state)


def discover(feed_url: str, state_path: Path, artifact_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    """Discover BEMA RSS episodes and mark which still need transcription.

    This intentionally does *not* crawl every episode page for Google Doc
    transcripts on normal runs. Existing published-transcript imports are
    detected from the vault DB by `BEMA <episode>:` title prefix, so the
    transcription queue only contains episodes not already represented in the
    vault.
    """
    show_title, episodes = parse_rss_items(fetch_text(feed_url, timeout=120))
    state = load_state(state_path)
    state.update({"feed_url": feed_url, "show_title": show_title, "episode_count": len(episodes)})
    state.setdefault("episodes", {})
    state.setdefault("versions", {})
    existing_sources = existing_bema_sources_by_episode()
    for episode in episodes:
        source_id = existing_sources.get(str(episode["episode"]))
        episode["transcripts"] = []
        entry = {**episode, "has_existing_source": bool(source_id), "existing_source_id": source_id, "has_published_transcript": bool(source_id)}
        if source_id:
            entry["transcription_status"] = "skipped_existing"
            entry["published_status"] = "skipped_existing"
        else:
            entry.setdefault("published_status", "missing_transcript")
        state["episodes"][episode["guid"]] = entry
    state["published_version_count"] = sum(1 for ep in state["episodes"].values() if ep.get("has_existing_source"))
    state["missing_transcript_count"] = sum(1 for ep in state["episodes"].values() if not ep.get("has_existing_source"))
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
    database_url = os.getenv("DATABASE_URL", "postgresql+psycopg://hermes:hermes@127.0.0.1:5432/hermes_kv")
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import sessionmaker
        from hermes_knowledge.core.models import Source
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


def existing_bema_source_id_for_episode(episode: dict[str, Any]) -> str | None:
    """Return any existing vault source for a BEMA episode."""
    return existing_bema_sources_by_episode().get(str(episode["episode"]))


def existing_bema_sources_by_episode() -> dict[str, str]:
    """Map BEMA episode numbers/slugs to one existing vault source ID.

    Opens a single DB connection for bulk discovery; doing one connection per
    episode can exhaust local Postgres during 500+ episode scans.
    """
    database_url = os.getenv("DATABASE_URL", "postgresql+psycopg://hermes:hermes@127.0.0.1:5432/hermes_kv")
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import sessionmaker
        from hermes_knowledge.core.models import Source
    except Exception:
        return {}
    try:
        engine = create_engine(database_url, future=True)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            rows = session.execute(select(Source.id, Source.title).where(Source.title.startswith("BEMA "))).all()
    except Exception:
        return {}
    mapping: dict[str, str] = {}
    for source_id, title in rows:
        match = re.match(r"BEMA\s+([^:]+):", title or "")
        if match:
            mapping.setdefault(match.group(1), source_id)
    return mapping


def import_published(episodes: list[dict[str, Any]], state_path: Path, artifact_dir: Path, api_url: str, limit: int | None = None) -> list[dict[str, Any]]:
    results = []
    processed = 0
    for episode in episodes:
        for transcript in episode.get("transcripts", []):
            key = f"{episode['guid']}:{transcript['version']}"
            if version_status(state_path, key) in {"imported", "skipped_existing"}:
                continue
            try:
                label = "Current" if transcript["version"] == "current" else "Legacy"
                title = f"BEMA {episode['episode']}: {episode['episode_title']} ({label})"
                if source_id := existing_source_id(title):
                    result = {"source_title": title, "source_id": source_id, "segments": None}
                    update_version_state(state_path, key, episode, transcript, published_status="skipped_existing", **result)
                    results.append(result)
                    processed += 1
                    print(f"SKIPPED existing {title}", flush=True)
                    if limit is not None and processed >= limit:
                        return results
                    continue
                text = extract_google_doc_text(transcript["url"])
                slug = safe_filename(f"{episode['episode']}-{transcript['version']}-{episode['episode_title']}")
                text_path = artifact_dir / "text" / f"{slug}.txt"
                text_path.parent.mkdir(parents=True, exist_ok=True)
                text_path.write_text(text)
                segments = make_segments(text, episode.get("duration_seconds") or 0)
                payload = {"show_title": episode.get("show_title") or "The BEMA Podcast", "episode_title": title, "episode_url": episode.get("episode_url"), "segments": segments}
                response = post_json(f"{api_url.rstrip('/')}/sources/transcript", payload)
                patch_json(
                    f"{api_url.rstrip('/')}/sources/{response['source_id']}/preference",
                    {"retrieval_weight": CURRENT_WEIGHT if transcript["version"] == "current" else LEGACY_WEIGHT, "preference_label": transcript["version"]},
                )
                result = {"source_title": title, "source_id": response["source_id"], "segments": len(segments)}
                update_version_state(state_path, key, episode, transcript, published_status="imported", **result)
                results.append(result)
                processed += 1
                print(f"IMPORTED {title} segments={len(segments)}", flush=True)
                if limit is not None and processed >= limit:
                    return results
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                update_version_state(state_path, key, episode, transcript, published_status="error", error=str(exc))
                print(f"ERROR {episode.get('title')} {transcript.get('version')}: {exc}", file=sys.stderr, flush=True)
    return results


def download_audio(episode: dict[str, Any], artifact_dir: Path) -> Path:
    if not episode.get("audio_url"):
        raise RuntimeError("episode has no audio enclosure URL")
    suffix = Path(urllib.parse.urlparse(episode["audio_url"]).path).suffix or ".mp3"
    path = artifact_dir / "audio" / f"{safe_filename(episode['episode'] + '-' + episode['episode_title'])}{suffix}"
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
        if episode.get("transcripts"):
            continue
        if episode_status(state_path, episode["guid"], "transcription_status") in {"transcribed", "skipped_existing"}:
            continue
        try:
            update_episode_state(state_path, episode, transcription_status="running")
            audio = download_audio(episode, artifact_dir)
            text, segments = transcribe_with_faster_whisper(audio, model, device, compute_type)
            if not segments:
                raise RuntimeError("no transcript segments generated")
            title = f"BEMA {episode['episode']}: {episode['episode_title']} (Generated Transcript)"
            payload = {"show_title": episode.get("show_title") or "The BEMA Podcast", "episode_title": title, "episode_url": episode.get("episode_url"), "segments": segments}
            response = post_json(f"{api_url.rstrip('/')}/sources/transcript", payload)
            patch_json(f"{api_url.rstrip('/')}/sources/{response['source_id']}/preference", {"retrieval_weight": 0.9, "preference_label": "generated"})
            result = {"source_title": title, "source_id": response["source_id"], "segments": len(segments)}
            update_episode_state(state_path, episode, transcription_status="transcribed", **result)
            results.append(result)
            processed += 1
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
    eps = state.get("episodes", {})
    versions = state.get("versions", {})
    print(json.dumps({
        "episodes": len(episodes),
        "episodes_already_in_vault": sum(1 for ep in eps.values() if ep.get("has_existing_source")),
        "episodes_requiring_transcription": sum(1 for ep in eps.values() if not ep.get("has_existing_source")),
        "published_imported_or_existing": sum(1 for v in versions.values() if v.get("published_status") in {"imported", "skipped_existing"}),
        "generated_transcribed_or_existing": sum(1 for ep in eps.values() if ep.get("transcription_status") in {"transcribed", "skipped_existing"}),
        "next_missing_to_transcribe": (planned_next_episode(state_path, [ep for ep in episodes if not ep.get("transcripts")], "transcription_status") or {}).get("title"),
    }, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resumable BEMA podcast transcript import/transcription pipeline.")
    parser.add_argument("command", choices=["discover", "status", "import-published", "transcribe-missing"])
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
    show_title, episodes = discover(args.feed_url, args.state, args.artifact_dir)
    if args.command == "discover":
        print(json.dumps({"show_title": show_title, "episodes": len(episodes), "episodes_already_in_vault": sum(1 for ep in load_state(args.state).get("episodes", {}).values() if ep.get("has_existing_source")), "episodes_requiring_transcription": sum(1 for ep in load_state(args.state).get("episodes", {}).values() if not ep.get("has_existing_source"))}, indent=2))
    elif args.command == "status":
        print_status(args.state, episodes)
    elif args.command == "import-published":
        results = import_published(episodes, args.state, args.artifact_dir, args.api, args.limit)
        print(json.dumps({"imported_this_run": len(results), "results": results[:5]}, indent=2))
    elif args.command == "transcribe-missing":
        results = transcribe_missing(episodes, args.state, args.artifact_dir, args.api, args.model, args.device, args.compute_type, args.limit)
        print(json.dumps({"transcribed_this_run": len(results), "results": results[:5]}, indent=2))


if __name__ == "__main__":
    main()
