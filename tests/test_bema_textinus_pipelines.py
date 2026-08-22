from __future__ import annotations

import json
from pathlib import Path


def load_connector(name: str):
    module_name = f"citara.connectors.podcasts.{name}"
    module = __import__(module_name, fromlist=[name])
    return module


def test_specific_pipeline_defaults_use_external_citara_roots():
    bema = load_connector("bema")
    textinus = load_connector("textinus")
    bibleproject = load_connector("bibleproject")
    repo_root = Path(__file__).resolve().parents[1]
    citara_root = repo_root.parent / "citara-data"

    assert bema.DEFAULT_ARTIFACT_DIR == citara_root / "source-artifacts" / "bema"
    assert bema.DEFAULT_STATE == citara_root / "import-state" / "bema_pipeline_state.json"
    assert textinus.DEFAULT_ARTIFACT_DIR == citara_root / "source-artifacts" / "textinus"
    assert textinus.DEFAULT_STATE == citara_root / "import-state" / "textinus_pipeline_state.json"
    assert bibleproject.DEFAULT_ARTIFACT_DIR == citara_root / "source-artifacts" / "bibleproject"
    assert bibleproject.DEFAULT_STATE == citara_root / "import-state" / "bibleproject_pipeline_state.json"


def test_connector_defaults_are_actually_external_to_the_repo():
    """The name of the test above was aspirational: `../citara` was the repo itself."""
    repo_root = Path(__file__).resolve().parents[1]

    for name in ("bema", "textinus", "bibleproject"):
        connector = load_connector(name)
        for default in (connector.DEFAULT_ARTIFACT_DIR, connector.DEFAULT_STATE):
            resolved = Path(default).resolve()
            assert resolved != repo_root, f"{name}: {default} is the repo itself"
            assert repo_root not in resolved.parents, f"{name}: {default} is inside the repo"


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
    bema = load_connector("bema")

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


def test_bema_timestamp_metadata_distinguishes_estimated_from_audio_derived():
    bema = load_connector("bema")

    assert bema.timestamp_metadata("published_transcript") == {
        "timestamp_provenance": "proportional_estimate",
        "timestamp_precision": "approximate",
        "citation_anchor": "chunk_start",
    }
    assert bema.timestamp_metadata("generated_openai_whisper") == {
        "timestamp_provenance": "asr_segment",
        "timestamp_precision": "segment",
        "citation_anchor": "chunk_start",
    }


def test_textinus_pipeline_parses_anchor_feed_as_audio_only():
    textinus = load_connector("textinus")

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
            "published": "",
            "audio_url": "https://audio.example.com/textinus.m4a",
            "transcript_url": None,
            "transcript_urls": [],
        }
    ]


def test_bema_and_textinus_state_helpers_choose_unfinished_episode(tmp_path: Path):
    bema = load_connector("bema")
    textinus = load_connector("textinus")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"episodes": {"done": {"transcription_status": "transcribed"}}}))
    episodes = [{"guid": "done"}, {"guid": "next"}]

    assert bema.planned_next_episode(state_path, episodes, "transcription_status") == {"guid": "next"}
    assert textinus.planned_next_episode(state_path, episodes, "transcription_status") == {"guid": "next"}
