#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from hermes_knowledge.core.config import settings
from hermes_knowledge.core.paths import source_artifact_root, source_state_root

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_CONFIG_PATH = Path("hkb.sources.json")
USER_AGENT = "hermes-knowledge-vault/0.1 (+generic podcast import pipeline)"
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
PODCAST_NS = "https://podcastindex.org/namespace/1.0"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
TERMINAL_STATUSES = {"imported", "skipped_existing", "missing_transcript", "error"}


def fetch_bytes(url: str, *, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_text(url: str, timeout: int = 60) -> str:
    return fetch_bytes(url, timeout=timeout).decode("utf-8", "ignore")


def clean_html_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", value, flags=re.S)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</p\s*>", "\n\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    lines = [" ".join(html.unescape(line).split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def seconds_from_duration(value: str | None) -> int:
    value = (value or "").strip()
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    parts = value.split(":")
    if not all(part.isdigit() for part in parts):
        return 0
    nums = [int(part) for part in parts]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    return 0


def slugify(value: str, *, max_len: int = 80) -> str:
    value = html.unescape(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:max_len].strip("-") or "podcast"


def paths_for_slug(slug_or_title: str, *, base_dir: Path = Path(".")) -> dict[str, Path]:
    slug = slugify(slug_or_title)
    if base_dir == Path("."):
        artifact_dir = source_artifact_root() / slug
        state = source_state_root() / f"{slug}_pipeline_state.json"
    else:
        hkb_root = base_dir / "hkb"
        artifact_dir = hkb_root / "source-artifacts" / slug
        state = hkb_root / "import-state" / f"{slug}_pipeline_state.json"
    return {
        "state": state,
        "artifact_dir": artifact_dir,
        "metadata_dir": artifact_dir / "metadata",
        "transcript_dir": artifact_dir / "transcripts",
        "payload_dir": artifact_dir / "payloads",
        "audio_dir": artifact_dir / "audio",
    }


def _item_text(item: ET.Element, tag: str) -> str:
    value = item.findtext(tag)
    if value is None and tag.startswith("itunes:"):
        value = item.findtext(f"{{{ITUNES_NS}}}{tag.split(':', 1)[1]}")
    if value is None and tag.startswith("content:"):
        value = item.findtext(f"{{{CONTENT_NS}}}{tag.split(':', 1)[1]}")
    return clean_html_text(value)


def _item_raw(item: ET.Element, tag: str) -> str:
    child = item.find(tag)
    if child is None and tag.startswith("itunes:"):
        child = item.find(f"{{{ITUNES_NS}}}{tag.split(':', 1)[1]}")
    if child is None and tag.startswith("content:"):
        child = item.find(f"{{{CONTENT_NS}}}{tag.split(':', 1)[1]}")
    return child.text or "" if child is not None else ""


def _enclosure_url(item: ET.Element) -> str:
    enclosure = item.find("enclosure")
    return html.unescape(enclosure.attrib.get("url", "")) if enclosure is not None else ""


def _transcripts(item: ET.Element) -> list[dict[str, str | None]]:
    transcripts = []
    for element in item.findall(f"{{{PODCAST_NS}}}transcript") + item.findall("podcast:transcript"):
        url = element.attrib.get("url")
        if url:
            transcripts.append(
                {
                    "url": html.unescape(url),
                    "type": element.attrib.get("type"),
                    "language": element.attrib.get("language") or element.attrib.get("lang"),
                }
            )
    priority = {"text/vtt": 0, "application/srt": 1, "text/srt": 1, "application/json": 2, "text/html": 3, "text/plain": 4}
    return sorted(transcripts, key=lambda entry: priority.get((entry.get("type") or "").lower(), 9))


def _episode_url_fallback(link: str, transcript_entries: list[dict[str, str | None]], audio_url: str) -> str:
    if link:
        return link
    for entry in transcript_entries:
        transcript_url = entry.get("url") or ""
        match = re.search(r"(https://www\.buzzsprout\.com/\d+/\d+)", transcript_url)
        if match:
            return match.group(1)
    match = re.search(r"(https://www\.buzzsprout\.com/\d+)/episodes/(\d+)", audio_url)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return ""


def parse_rss_items(rss_text: str, *, feed_url: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    root = ET.fromstring(rss_text)
    channel = root.find("channel") if root.tag.lower() == "rss" else root
    if channel is None:
        return "", []
    show_title = clean_html_text(channel.findtext("title")) or "Untitled Podcast"
    episodes: list[dict[str, Any]] = []
    for item in channel.findall("item"):
        duration = _item_text(item, "itunes:duration")
        link = _item_text(item, "link")
        title = _item_text(item, "title") or "Untitled episode"
        guid = _item_text(item, "guid") or link or title
        transcript_entries = _transcripts(item)
        first_transcript = transcript_entries[0] if transcript_entries else {}
        audio_url = _enclosure_url(item)
        episode_url = _episode_url_fallback(link, transcript_entries, audio_url)
        episodes.append(
            {
                "show_title": show_title,
                "title": title,
                "guid": guid,
                "episode_url": episode_url,
                "publish_date": _item_text(item, "pubDate") or None,
                "duration": duration,
                "duration_seconds": seconds_from_duration(duration),
                "audio_url": audio_url,
                "transcript_url": first_transcript.get("url"),
                "transcript_type": first_transcript.get("type"),
                "transcript_language": first_transcript.get("language"),
                "transcript_urls": [entry["url"] for entry in transcript_entries if entry.get("url")],
                "feed_url": feed_url,
            }
        )
    return show_title, episodes


def load_connector_module(connector: str) -> Any:
    module_name = f"hermes_knowledge.connectors.podcasts.{connector}"
    try:
        return __import__(module_name, fromlist=[connector])
    except ImportError as exc:
        raise RuntimeError(f"Unknown podcast source connector {connector!r}") from exc


def parse_source_rss_items(config: dict[str, Any], rss_text: str, *, feed_url: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    connector = (config.get("connector") or "generic").strip().lower()
    if connector in {"", "generic", "generic_rss", "podcast"}:
        return parse_rss_items(rss_text, feed_url=feed_url)
    module = load_connector_module(connector)
    parser = getattr(module, "parse_rss_items", None)
    if parser is None:
        raise RuntimeError(f"Podcast source connector {connector!r} does not expose parse_rss_items")
    return parser(rss_text)


def resolve_input_url(url: str, *, fetch_text_fn: Callable[[str, int], str] = fetch_text) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path
    query = urllib.parse.parse_qs(parsed.query)
    apple_match = re.search(r"/id(\d+)", path)
    if "podcasts.apple.com" in host and apple_match:
        podcast_id = apple_match.group(1)
        episode_adam_id = (query.get("i") or [None])[0]
        lookup_url = f"https://itunes.apple.com/lookup?id={podcast_id}&entity=podcast"
        data = json.loads(fetch_text_fn(lookup_url, 60))
        if not data.get("results"):
            raise RuntimeError(f"Apple podcast lookup returned no results for {podcast_id}")
        result = data["results"][0]
        feed_url = result.get("feedUrl")
        if not feed_url:
            raise RuntimeError(f"Apple podcast lookup did not expose feedUrl for {podcast_id}")
        return {
            "input_url": url,
            "input_kind": "apple_episode" if episode_adam_id else "apple_podcast",
            "feed_url": feed_url,
            "show_title": result.get("collectionName") or result.get("trackName"),
            "artist_name": result.get("artistName"),
            "collection_id": podcast_id,
            "episode_adam_id": episode_adam_id,
        }
    if path.lower().endswith((".xml", ".rss")) or path.lower().endswith(("/feed", "/rss")) or "rss" in path.lower():
        return {"input_url": url, "input_kind": "rss_feed", "feed_url": url}
    return {"input_url": url, "input_kind": "unknown_url", "feed_url": url}


def fetch_apple_episode_metadata(collection_id: str, episode_adam_id: str) -> dict[str, Any] | None:
    """Return public iTunes episode metadata for an Apple Podcasts episode URL.

    Apple app/web autogenerated transcript bodies are not exposed through RSS or the
    iTunes lookup response. This metadata still lets the pipeline reliably map an
    Apple episode URL (`?i=...`) back to the RSS GUID/audio enclosure so missing
    transcripts are queued for generated import instead of being silently counted as
    undiscoverable.
    """
    lookup_url = f"https://itunes.apple.com/lookup?id={collection_id}&entity=podcastEpisode&limit=200"
    try:
        data = json.loads(fetch_text(lookup_url, timeout=60))
    except Exception:
        return None
    for result in data.get("results", []):
        if str(result.get("trackId")) == str(episode_adam_id):
            return result
    return None


def load_source_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"sources": []}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise RuntimeError(f"Source config {path} must be an object with a sources array")
    return data


def source_config_by_name(name: str, *, config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any] | None:
    for source in load_source_config(config_path).get("sources", []):
        if not isinstance(source, dict):
            continue
        if source.get("name") == name or source.get("slug") == name:
            return dict(source)
    return None


def load_config_or_url(value: str, *, config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    path = Path(value)
    if path.exists():
        data = json.loads(path.read_text())
        if "feed_url" not in data and "url" not in data:
            raise RuntimeError(f"Config {path} must contain feed_url or url")
        return data
    if source := source_config_by_name(value, config_path=config_path):
        if "feed_url" not in source and "url" not in source:
            raise RuntimeError(f"Source {value!r} in {config_path} must contain feed_url or url")
        return source
    return {"url": value}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"inputs": [], "episodes": {}}
    return json.loads(path.read_text())


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def make_segments_from_text(text: str, duration_seconds: int) -> list[dict[str, Any]]:
    duration_ms = max(int(duration_seconds or 0) * 1000, 1)
    paragraphs = [" ".join(part.split()) for part in re.split(r"\n\s*\n+", text) if part.strip()]
    chunks: list[str] = []
    for paragraph in paragraphs or [text]:
        units = re.split(r"(?<=[.!?])\s+", paragraph) if len(paragraph) > 1800 else [paragraph]
        current = ""
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
    chunks = [chunk for chunk in chunks if len(chunk) >= 10]
    if not chunks:
        return []
    total_chars = max(sum(len(chunk) for chunk in chunks), 1)
    elapsed = 0
    segments: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        start_ms = segments[-1]["end_ms"] if segments else 0
        if index == len(chunks) - 1:
            end_ms = duration_ms
        else:
            elapsed += len(chunk)
            end_ms = max(start_ms + 1000, int(duration_ms * elapsed / total_chars))
        segments.append({"start_ms": start_ms, "end_ms": end_ms, "speaker": None, "text": chunk})
    return segments


def _timestamp_ms(value: str) -> int:
    hours = 0
    if value.count(":") == 2:
        h, m, rest = value.split(":")
        hours = int(h)
    else:
        m, rest = value.split(":")
    s, ms = re.split(r"[,.]", rest)
    return ((hours * 3600 + int(m) * 60 + int(s)) * 1000) + int(ms.ljust(3, "0")[:3])


def parse_vtt(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or lines[0].upper().startswith("WEBVTT"):
            continue
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        start, end = [part.strip().split()[0] for part in lines[timing_index].split("-->", 1)]
        cue_text = clean_html_text("\n".join(lines[timing_index + 1 :]))
        if cue_text:
            segments.append({"start_ms": _timestamp_ms(start), "end_ms": _timestamp_ms(end), "speaker": None, "text": cue_text})
    return segments


def fetch_transcript_segments(episode: dict[str, Any], artifact_dir: Path) -> tuple[list[dict[str, Any]], Path]:
    transcript_url = episode.get("transcript_url")
    if not transcript_url:
        raise RuntimeError("episode has no published transcript URL")
    transcript_dir = artifact_dir / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(urllib.parse.urlparse(transcript_url).path).suffix or ".txt"
    path = transcript_dir / f"{slugify(episode['title'])}{suffix}"
    if not path.exists():
        path.write_bytes(fetch_bytes(transcript_url, timeout=180))
    raw = path.read_text(errors="ignore")
    transcript_type = (episode.get("transcript_type") or "").lower()
    if suffix.lower() in {".vtt"} or "vtt" in transcript_type:
        segments = parse_vtt(raw)
    else:
        segments = make_segments_from_text(clean_html_text(raw), episode.get("duration_seconds") or 0)
    return segments, path


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode())


def patch_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}, method="PATCH")
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode())


def existing_source_id(title: str, canonical_url: str | None = None) -> str | None:
    database_url = os.getenv("DATABASE_URL", settings.database_url)
    try:
        from sqlalchemy import create_engine, or_, select
        from sqlalchemy.orm import sessionmaker

        from hermes_knowledge.core.models import Source
    except Exception:
        return None
    try:
        engine = create_engine(database_url, future=True)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            clauses = [Source.title == title]
            if canonical_url:
                clauses.append(Source.canonical_url == canonical_url)
            source = session.execute(select(Source).where(or_(*clauses))).scalar_one_or_none()
            return source.id if source else None
    except Exception:
        return None


def annotate_source_metadata(source_id: str, metadata: dict[str, Any]) -> None:
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


def discover(input_value: str, *, base_dir: Path = Path("."), config_path: Path = DEFAULT_CONFIG_PATH) -> tuple[dict[str, Any], list[dict[str, Any]], Path, Path]:
    config = load_config_or_url(input_value, config_path=config_path)
    resolved = resolve_input_url(config.get("feed_url") or config.get("url")) if config.get("url") else {"feed_url": config["feed_url"], "input_kind": "rss_feed"}
    feed_url = resolved["feed_url"]
    rss_text = fetch_text(feed_url, timeout=120)
    show_title, episodes = parse_source_rss_items(config, rss_text, feed_url=feed_url)
    apple_episode_meta = None
    if resolved.get("collection_id") and resolved.get("episode_adam_id"):
        apple_episode_meta = fetch_apple_episode_metadata(str(resolved["collection_id"]), str(resolved["episode_adam_id"]))
    show_title = config.get("show_title") or resolved.get("show_title") or show_title
    slug = config.get("slug") or show_title
    paths = paths_for_slug(slug, base_dir=base_dir)
    for directory in [paths["metadata_dir"], paths["transcript_dir"], paths["payload_dir"], paths["audio_dir"]]:
        directory.mkdir(parents=True, exist_ok=True)
    state_path = paths["state"]
    state = load_state(state_path)
    state.update(
        {
            "feed_url": feed_url,
            "show_title": show_title,
            "slug": slugify(slug),
            "episode_count": len(episodes),
            "published_transcript_count": sum(1 for episode in episodes if episode.get("transcript_url")),
            "target_apple_episode_adam_id": resolved.get("episode_adam_id"),
            "last_discovered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    state.setdefault("inputs", []).append({"url": config.get("url") or config.get("feed_url"), **resolved})
    entries = state.setdefault("episodes", {})
    for episode in episodes:
        key = episode["guid"]
        if apple_episode_meta:
            apple_guid = apple_episode_meta.get("episodeGuid")
            apple_audio = apple_episode_meta.get("episodeUrl") or apple_episode_meta.get("previewUrl")
            is_target = bool(
                (apple_guid and apple_guid == episode.get("guid"))
                or (apple_audio and apple_audio == episode.get("audio_url"))
                or (apple_episode_meta.get("trackName") and apple_episode_meta.get("trackName") == episode.get("title"))
            )
            if is_target:
                episode.update(
                    {
                        "apple_episode_adam_id": str(apple_episode_meta.get("trackId")),
                        "apple_episode_url": apple_episode_meta.get("trackViewUrl"),
                        "apple_closed_captioning": apple_episode_meta.get("closedCaptioning"),
                        "apple_generated_transcript_candidate": not bool(episode.get("transcript_url")),
                        "generated_transcript_reason": "Apple Podcasts episode URL resolved; RSS has no podcast:transcript, so queue generated transcript import",
                    }
                )
        previous = entries.get(key, {})
        status_fields = {k: v for k, v in previous.items() if k.endswith("_status") or k in {"source_id", "source_title", "error"}}
        entries[key] = {**episode, **status_fields, "has_published_transcript": bool(episode.get("transcript_url"))}
    state["generated_transcript_candidate_count"] = sum(
        1 for episode in entries.values() if episode.get("apple_generated_transcript_candidate") or not episode.get("has_published_transcript")
    )
    save_state(state_path, state)
    return state, episodes, state_path, paths["artifact_dir"]


def download_audio(episode: dict[str, Any], artifact_dir: Path) -> Path:
    if not episode.get("audio_url"):
        raise RuntimeError("episode has no audio enclosure URL")
    suffix = Path(urllib.parse.urlparse(episode["audio_url"]).path).suffix or ".m4a"
    path = artifact_dir / "audio" / f"{slugify(episode['title'])}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(fetch_bytes(episode["audio_url"], timeout=900))
    return path


def transcribe_with_faster_whisper(audio_path: Path, model: str, device: str, compute_type: str) -> tuple[str, list[dict[str, Any]]]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Install faster-whisper first: uv pip install faster-whisper") from exc
    whisper = WhisperModel(model, device=device, compute_type=compute_type)
    segments_iter, _ = whisper.transcribe(str(audio_path), vad_filter=True)
    parts: list[str] = []
    segments: list[dict[str, Any]] = []
    for segment in segments_iter:
        text = segment.text.strip()
        if text:
            parts.append(text)
            segments.append({"start_ms": int(segment.start * 1000), "end_ms": int(segment.end * 1000), "speaker": None, "text": text})
    return "\n".join(parts), segments


def import_published(
    input_value: str,
    *,
    api_url: str = DEFAULT_API_URL,
    limit: int | None = None,
    base_dir: Path = Path("."),
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> list[dict[str, Any]]:
    state, episodes, state_path, artifact_dir = discover(input_value, base_dir=base_dir, config_path=config_path)
    results: list[dict[str, Any]] = []
    processed = 0
    for episode in episodes:
        entry = load_state(state_path)["episodes"].get(episode["guid"], {})
        if entry.get("published_status") in {"imported", "skipped_existing", "missing_transcript"}:
            continue
        title = f"{state['show_title']}: {episode['title']} (Published Transcript)"
        if not episode.get("transcript_url"):
            update_episode_state(state_path, episode, published_status="missing_transcript")
            continue
        if source_id := existing_source_id(title, episode.get("episode_url")):
            result = {"source_title": title, "source_id": source_id}
            update_episode_state(state_path, episode, published_status="skipped_existing", **result)
            results.append(result)
            processed += 1
            if limit is not None and processed >= limit:
                break
            continue
        try:
            segments, transcript_path = fetch_transcript_segments(episode, artifact_dir)
            if not segments:
                raise RuntimeError("published transcript produced no segments")
            payload = {"show_title": state["show_title"], "episode_title": title, "episode_url": episode.get("episode_url"), "segments": segments}
            payload_path = artifact_dir / "payloads" / f"{slugify(episode['title'])}.json"
            payload_path.parent.mkdir(parents=True, exist_ok=True)
            payload_path.write_text(json.dumps(payload, indent=2))
            response = post_json(f"{api_url.rstrip('/')}/sources/transcript", payload)
            source_id = response["source_id"]
            annotate_source_metadata(
                source_id,
                {
                    "show_title": state["show_title"],
                    "episode_guid": episode.get("guid"),
                    "episode_duration_seconds": episode.get("duration_seconds"),
                    "rss_feed_url": state.get("feed_url"),
                    "transcript_url": episode.get("transcript_url"),
                    "transcript_type": episode.get("transcript_type"),
                    "transcript_language": episode.get("transcript_language"),
                    "transcript_provenance": "published_rss_transcript",
                    "input_type": "podcast_transcript",
                },
            )
            try:
                patch_json(f"{api_url.rstrip('/')}/sources/{source_id}/preference", {"retrieval_weight": 1.0, "preference_label": "published"})
            except Exception:
                pass
            result = {"source_title": title, "source_id": source_id, "segments": len(segments), "transcript_path": str(transcript_path)}
            update_episode_state(state_path, episode, published_status="imported", **result)
            results.append(result)
            processed += 1
            print(f"IMPORTED {title} segments={len(segments)}", flush=True)
            if limit is not None and processed >= limit:
                break
        except KeyboardInterrupt:
            update_episode_state(state_path, episode, published_status="interrupted")
            raise
        except Exception as exc:
            update_episode_state(state_path, episode, published_status="error", error=str(exc))
            print(f"ERROR {title}: {exc}", file=sys.stderr, flush=True)
    return results


def transcribe_missing(
    input_value: str,
    *,
    api_url: str = DEFAULT_API_URL,
    model: str = "large-v3-turbo",
    device: str = "auto",
    compute_type: str = "auto",
    limit: int | None = None,
    base_dir: Path = Path("."),
    keep_audio: bool = False,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> list[dict[str, Any]]:
    state, episodes, state_path, artifact_dir = discover(input_value, base_dir=base_dir, config_path=config_path)
    results: list[dict[str, Any]] = []
    processed = 0
    for episode in episodes:
        entry = load_state(state_path)["episodes"].get(episode["guid"], {})
        if entry.get("published_status") in {"imported", "skipped_existing"}:
            continue
        if entry.get("transcription_status") in {"transcribed", "skipped_existing"}:
            continue
        title = f"{state['show_title']}: {episode['title']} (Generated Transcript)"
        if source_id := existing_source_id(title, episode.get("episode_url")):
            result = {"source_title": title, "source_id": source_id}
            update_episode_state(state_path, episode, transcription_status="skipped_existing", **result)
            results.append(result)
            processed += 1
            if limit is not None and processed >= limit:
                break
            continue
        try:
            update_episode_state(state_path, episode, transcription_status="running")
            audio = download_audio(episode, artifact_dir)
            text, segments = transcribe_with_faster_whisper(audio, model, device, compute_type)
            if not segments:
                raise RuntimeError("no transcript segments generated")
            text_path = artifact_dir / "transcripts" / f"{slugify(episode['title'])}.generated.txt"
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(text)
            payload = {"show_title": state["show_title"], "episode_title": title, "episode_url": episode.get("episode_url"), "segments": segments}
            payload_path = artifact_dir / "payloads" / f"{slugify(episode['title'])}.generated.json"
            payload_path.parent.mkdir(parents=True, exist_ok=True)
            payload_path.write_text(json.dumps(payload, indent=2))
            response = post_json(f"{api_url.rstrip('/')}/sources/transcript", payload)
            source_id = response["source_id"]
            annotate_source_metadata(
                source_id,
                {
                    "show_title": state["show_title"],
                    "episode_guid": episode.get("guid"),
                    "episode_duration_seconds": episode.get("duration_seconds"),
                    "rss_feed_url": state.get("feed_url"),
                    "transcript_provenance": "generated_whisper",
                    "transcript_provider": "faster-whisper",
                    "transcription_model": model,
                    "input_type": "podcast_generated_transcript",
                },
            )
            try:
                patch_json(f"{api_url.rstrip('/')}/sources/{source_id}/preference", {"retrieval_weight": 0.9, "preference_label": "generated"})
            except Exception:
                pass
            if not keep_audio:
                try:
                    audio.unlink()
                except OSError:
                    pass
            result = {"source_title": title, "source_id": source_id, "segments": len(segments), "transcript_path": str(text_path)}
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
            print(f"ERROR transcription {title}: {exc}", file=sys.stderr, flush=True)
    return results


def update_episode_state(state_path: Path, episode: dict[str, Any], **updates: Any) -> None:
    state = load_state(state_path)
    state.setdefault("episodes", {}).setdefault(episode["guid"], {}).update({**episode, **updates})
    save_state(state_path, state)


def print_status(input_value: str, *, base_dir: Path = Path("."), refresh: bool = False, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    if refresh:
        state, _, state_path, _ = discover(input_value, base_dir=base_dir, config_path=config_path)
    else:
        config = load_config_or_url(input_value, config_path=config_path)
        if config.get("slug"):
            state_path = paths_for_slug(config["slug"], base_dir=base_dir)["state"]
        else:
            resolved = resolve_input_url(config.get("feed_url") or config.get("url")) if config.get("url") else {"feed_url": config["feed_url"]}
            show_title = config.get("show_title") or resolved.get("show_title") or Path(resolved["feed_url"]).stem
            state_path = paths_for_slug(config.get("slug") or show_title, base_dir=base_dir)["state"]
            if not state_path.exists():
                state, _, state_path, _ = discover(input_value, base_dir=base_dir, config_path=config_path)
        state = load_state(state_path)
    entries = state.get("episodes", {})
    summary = {
        "show_title": state.get("show_title"),
        "feed_url": state.get("feed_url"),
        "state_path": str(state_path),
        "episodes": len(entries),
        "published_transcript_episodes": sum(1 for ep in entries.values() if ep.get("has_published_transcript")),
        "published_imported_or_existing": sum(1 for ep in entries.values() if ep.get("published_status") in {"imported", "skipped_existing"}),
        "generated_transcript_candidates": sum(1 for ep in entries.values() if ep.get("apple_generated_transcript_candidate") or not ep.get("has_published_transcript")),
        "target_apple_episode_adam_id": state.get("target_apple_episode_adam_id"),
        "target_apple_episode_found": any(ep.get("apple_episode_adam_id") == state.get("target_apple_episode_adam_id") for ep in entries.values()),
        "missing_transcript": sum(1 for ep in entries.values() if ep.get("published_status") == "missing_transcript" or not ep.get("has_published_transcript")),
        "errors": sum(1 for ep in entries.values() if ep.get("published_status") == "error"),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


def connector_paths(config: dict[str, Any], *, base_dir: Path = Path(".")) -> tuple[Path, Path]:
    paths = paths_for_slug(config.get("slug") or config.get("name") or config.get("show_title") or "podcast", base_dir=base_dir)
    return paths["state"], paths["artifact_dir"]


def run_configured_connector_command(args: argparse.Namespace, config: dict[str, Any], *, base_dir: Path) -> bool:
    connector = (config.get("connector") or "generic").strip().lower()
    if connector in {"", "generic", "generic_rss", "podcast"}:
        return False

    module = load_connector_module(connector)
    feed_url = config.get("feed_url") or config.get("url")
    if not feed_url:
        raise RuntimeError(f"Configured source {config.get('name') or args.input!r} must contain feed_url or url")
    state_path, artifact_dir = connector_paths(config, base_dir=base_dir)

    if connector == "bema":
        show_title, episodes = module.discover(feed_url, state_path, artifact_dir)
    else:
        show_title, episodes = module.discover(feed_url, state_path)

    if args.command == "discover":
        print(json.dumps({"show_title": show_title, "episodes": len(episodes), "state_path": str(state_path), "artifact_dir": str(artifact_dir), "connector": connector}, indent=2))
        return True
    if args.command == "status":
        module.print_status(state_path, episodes)
        return True
    if args.command in {"import-published", "run"}:
        if not hasattr(module, "import_published"):
            raise RuntimeError(f"Connector {connector!r} does not support import-published")
        results = module.import_published(episodes, state_path, artifact_dir, args.api_url, args.limit)
        print(json.dumps({"imported_this_run": len(results), "results": results[:5]}, indent=2))
        return True
    if args.command == "transcribe-missing":
        if not hasattr(module, "transcribe_missing"):
            raise RuntimeError(f"Connector {connector!r} does not support transcribe-missing")
        results = module.transcribe_missing(episodes, state_path, artifact_dir, args.api_url, args.model, args.device, args.compute_type, args.limit)
        print(json.dumps({"transcribed_this_run": len(results), "results": results[:5]}, indent=2))
        return True
    return False


def run_command(args: argparse.Namespace) -> int:
    base_dir = Path(args.base_dir)
    config_path = Path(args.config)
    config = load_config_or_url(args.input, config_path=config_path)
    if run_configured_connector_command(args, config, base_dir=base_dir):
        return 0
    if args.command == "discover":
        state, _, state_path, artifact_dir = discover(args.input, base_dir=base_dir, config_path=config_path)
        print(json.dumps({"show_title": state.get("show_title"), "episodes": state.get("episode_count"), "published_transcript_count": state.get("published_transcript_count"), "state_path": str(state_path), "artifact_dir": str(artifact_dir)}, indent=2))
        return 0
    if args.command == "status":
        print_status(args.input, base_dir=base_dir, refresh=args.refresh, config_path=config_path)
        return 0
    if args.command in {"import-published", "run"}:
        results = import_published(args.input, api_url=args.api_url, limit=args.limit, base_dir=base_dir, config_path=config_path)
        print(json.dumps({"imported_or_skipped": len(results), "results": results}, indent=2))
        return 0
    if args.command == "transcribe-missing":
        results = transcribe_missing(
            args.input,
            api_url=args.api_url,
            model=args.model,
            device=args.device,
            compute_type=args.compute_type,
            limit=args.limit,
            base_dir=base_dir,
            keep_audio=args.keep_audio,
            config_path=config_path,
        )
        print(json.dumps({"transcribed_or_skipped": len(results), "results": results}, indent=2))
        return 0
    raise RuntimeError(f"unknown command {args.command}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generic resumable podcast transcript pipeline for HKB")
    parser.add_argument("--base-dir", default=".", help="Project/base directory for data paths")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Source config file, usually hkb.sources.json")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["discover", "status", "import-published", "run", "transcribe-missing"]:
        p = sub.add_parser(name)
        p.add_argument("input", help="Source name from config, RSS/Apple podcast URL, or JSON source config path")
        if name in {"import-published", "run", "transcribe-missing"}:
            p.add_argument("--api-url", default=DEFAULT_API_URL)
            p.add_argument("--limit", type=int)
        if name == "transcribe-missing":
            p.add_argument("--model", default="large-v3-turbo")
            p.add_argument("--device", default="auto")
            p.add_argument("--compute-type", default="auto")
            p.add_argument("--keep-audio", action="store_true")
        if name == "status":
            p.add_argument("--refresh", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run_command(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
