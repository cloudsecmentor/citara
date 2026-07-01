from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def load_script(name: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


BEMA_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>The BEMA Podcast</title>
    <item>
      <title><![CDATA[1: Trust the Story]]></title>
      <guid>bema-1</guid>
      <link>https://www.bemadiscipleship.com/1</link>
      <itunes:season>1</itunes:season>
      <itunes:episode>1</itunes:episode>
      <itunes:duration>00:53:08</itunes:duration>
      <description><![CDATA[The Creation Story of Genesis 1]]></description>
      <enclosure url="https://audio.example.com/bema-1.mp3" />
    </item>
    <item>
      <title><![CDATA[21b: “Jesus Shema”]]></title>
      <guid>bema-21b</guid>
      <link>https://www.bemadiscipleship.com/21b</link>
      <itunes:season>1</itunes:season>
      <itunes:episode>21b</itunes:episode>
      <itunes:duration>00:01:18</itunes:duration>
      <description><![CDATA[A Daily Prayer]]></description>
      <enclosure url="https://audio.example.com/bema-21b.mp3" />
    </item>
  </channel>
</rss>
"""

BEMA_PAGE = """
<html><body>
<section><h2>Study Tools</h2>
  <a href="https://docs.google.com/document/d/e/current/pub">Transcript for BEMA 1</a>
</section>
<section><h2>Legacy Episode Content</h2>
  <a href="https://docs.google.com/document/d/e/legacy/pub">Transcript for BEMA 1 of 8 September 2016</a>
</section>
</body></html>
"""

TEXT_IN_US_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Text in Us</title>
    <item>
      <title><![CDATA[Narrative Numbers 16 Part 2: Cracking Rage]]></title>
      <guid>textinus-1</guid>
      <link>https://podcasters.spotify.com/pod/show/episode</link>
      <itunes:duration>00:37:54</itunes:duration>
      <description><![CDATA[Join George and Elle as they discuss the text.]]></description>
      <enclosure url="https://audio.example.com/textinus.m4a" type="audio/x-m4a" />
    </item>
  </channel>
</rss>
"""


def test_bema_pipeline_parses_rss_and_transcript_versions():
    bema = load_script("bema_pipeline")

    show_title, episodes = bema.parse_rss_items(BEMA_RSS)
    versions = bema.extract_transcript_links(BEMA_PAGE)

    assert show_title == "The BEMA Podcast"
    assert episodes[0]["episode"] == "1"
    assert episodes[0]["season"] == "1"
    assert episodes[1]["episode"] == "21b"
    assert versions == [
        {"version": "current", "url": "https://docs.google.com/document/d/e/current/pub"},
        {"version": "legacy", "url": "https://docs.google.com/document/d/e/legacy/pub"},
    ]


def test_textinus_pipeline_parses_anchor_feed_as_audio_only():
    textinus = load_script("textinus_pipeline")

    show_title, episodes = textinus.parse_rss_items(TEXT_IN_US_RSS)

    assert show_title == "Text in Us"
    assert episodes == [
        {
            "show_title": "Text in Us",
            "title": "Narrative Numbers 16 Part 2: Cracking Rage",
            "guid": "textinus-1",
            "episode_url": "https://podcasters.spotify.com/pod/show/episode",
            "description": "Join George and Elle as they discuss the text.",
            "duration": "00:37:54",
            "duration_seconds": 2274,
            "audio_url": "https://audio.example.com/textinus.m4a",
            "transcript_url": None,
            "transcript_urls": [],
        }
    ]


def test_bema_and_textinus_state_helpers_choose_unfinished_episode(tmp_path: Path):
    bema = load_script("bema_pipeline")
    textinus = load_script("textinus_pipeline")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"episodes": {"done": {"transcription_status": "transcribed"}}}))
    episodes = [{"guid": "done"}, {"guid": "next"}]

    assert bema.planned_next_episode(state_path, episodes, "transcription_status") == {"guid": "next"}
    assert textinus.planned_next_episode(state_path, episodes, "transcription_status") == {"guid": "next"}
