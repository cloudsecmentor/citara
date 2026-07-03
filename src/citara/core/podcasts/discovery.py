from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse
from xml.etree import ElementTree as ET


@dataclass(frozen=True)
class PodcastEpisode:
    episode_title: str
    episode_url: str | None
    audio_url: str | None = None
    transcript_url: str | None = None
    publish_date: str | None = None
    duration: str | None = None
    guid: str | None = None


@dataclass(frozen=True)
class PodcastFeed:
    show_title: str
    show_description: str | None
    rss_url: str | None
    website_url: str | None
    episodes: list[PodcastEpisode] = field(default_factory=list)


def classify_podcast_input(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.lower()
    if path.endswith((".xml", ".rss", "/feed", "/rss")) or "rss" in path:
        return "rss_feed"
    return "episode_or_show_page"


def parse_podcast_rss(xml_text: str, *, rss_url: str | None = None) -> PodcastFeed:
    root = ET.fromstring(xml_text)
    channel = root.find("channel") if root.tag.lower() == "rss" else root
    if channel is None:
        raise ValueError("RSS feed is missing channel")

    episodes: list[PodcastEpisode] = []
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        episodes.append(
            PodcastEpisode(
                episode_title=_text(item, "title") or "Untitled episode",
                episode_url=_text(item, "link"),
                audio_url=enclosure.attrib.get("url") if enclosure is not None else None,
                publish_date=_text(item, "pubDate"),
                guid=_text(item, "guid"),
            )
        )

    return PodcastFeed(
        show_title=_text(channel, "title") or "Untitled podcast",
        show_description=_text(channel, "description"),
        rss_url=rss_url,
        website_url=_text(channel, "link"),
        episodes=episodes,
    )


def _text(parent: ET.Element, tag: str) -> str | None:
    element = parent.find(tag)
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None
