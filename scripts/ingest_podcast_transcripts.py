#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

NS = {
    "podcast": "https://podcastindex.org/namespace/1.0",
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
}
TIMESTAMP_RE = re.compile(r"(?P<h>\d{2}:)?(?P<m>\d{2}):(?P<s>\d{2})[.,](?P<ms>\d{3})")


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return " ".join(self.parts)


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "citara/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "ignore")


def timestamp_to_ms(value: str) -> int:
    match = TIMESTAMP_RE.search(value)
    if not match:
        return 0
    hours = int((match.group("h") or "00:").rstrip(":"))
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    millis = int(match.group("ms"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def parse_vtt(text: str) -> list[dict]:
    segments: list[dict] = []
    current_start: int | None = None
    current_end: int | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_start, current_end, current_lines
        cleaned = " ".join(
            re.sub(r"<[^>]+>", "", line).strip()
            for line in current_lines
            if line.strip() and not line.strip().isdigit()
        ).strip()
        if cleaned and current_start is not None:
            segments.append({"start_ms": current_start, "end_ms": current_end, "text": html.unescape(cleaned)})
        current_start = current_end = None
        current_lines = []

    for raw_line in text.splitlines():
        line = raw_line.strip("\ufeff ")
        if not line or line == "WEBVTT" or line.startswith(("NOTE", "STYLE", "REGION")):
            if current_lines:
                flush()
            continue
        if "-->" in line:
            if current_lines:
                flush()
            start, end = [part.strip().split()[0] for part in line.split("-->", 1)]
            current_start = timestamp_to_ms(start)
            current_end = timestamp_to_ms(end)
            current_lines = []
        elif current_start is not None:
            current_lines.append(line)
    if current_lines:
        flush()
    return segments


def parse_html_transcript(text: str) -> list[dict]:
    parser = TextExtractor()
    parser.feed(text)
    normalized = re.sub(r"\s+", " ", parser.text()).strip()
    # Keep HTML transcript fallback coarse but usable. Split roughly every 1,000 chars.
    segments = []
    for index, start in enumerate(range(0, len(normalized), 1000)):
        chunk = normalized[start : start + 1000].strip()
        if chunk:
            segments.append({"start_ms": index * 60_000, "end_ms": (index + 1) * 60_000, "text": chunk})
    return segments


def transcript_segments(url: str, transcript_type: str | None) -> list[dict]:
    text = fetch_text(url)
    if (transcript_type or "").lower().endswith("vtt") or url.lower().endswith(".vtt"):
        return parse_vtt(text)
    return parse_html_transcript(text)


def feed_items(feed_url: str) -> tuple[str, list[dict]]:
    root = ET.fromstring(fetch_text(feed_url))
    channel = root.find("channel")
    show_title = channel.findtext("title") if channel is not None else feed_url
    items = []
    for item in root.findall("./channel/item"):
        transcript = item.find("podcast:transcript", NS)
        if transcript is None:
            continue
        items.append(
            {
                "show_title": show_title,
                "episode_title": item.findtext("title") or "Untitled episode",
                "episode_url": item.findtext("link"),
                "transcript_url": transcript.attrib["url"],
                "transcript_type": transcript.attrib.get("type"),
            }
        )
    return show_title, items


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest podcast RSS items with podcast:transcript metadata.")
    parser.add_argument("feed_url")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--save-dir", type=Path)
    args = parser.parse_args()

    _, items = feed_items(args.feed_url)
    selected = items[args.offset : args.offset + args.count]
    if not selected:
        raise SystemExit("No podcast:transcript items found")

    results = []
    for item in selected:
        segments = transcript_segments(item["transcript_url"], item.get("transcript_type"))
        payload = {
            "show_title": item["show_title"],
            "episode_title": item["episode_title"],
            "episode_url": item["episode_url"],
            "segments": segments,
        }
        if args.save_dir:
            args.save_dir.mkdir(parents=True, exist_ok=True)
            safe_title = re.sub(r"[^A-Za-z0-9_.-]+", "-", item["episode_title"]).strip("-")[:80]
            (args.save_dir / f"{safe_title}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        response = post_json(f"{args.api.rstrip('/')}/sources/transcript", payload)
        results.append({**item, "segment_count": len(segments), **response})

    print(json.dumps({"ingested": results}, indent=2))


if __name__ == "__main__":
    main()
