from __future__ import annotations

import json
from pathlib import Path

import pytest

from citara.connectors.podcasts import bibleproject as bibleproject_pipeline

episode_status = bibleproject_pipeline.episode_status
extract_transcript_links = bibleproject_pipeline.extract_transcript_links
parse_rss_items = bibleproject_pipeline.parse_rss_items
planned_next_episode = bibleproject_pipeline.planned_next_episode
safe_filename = bibleproject_pipeline.safe_filename
seconds_from_duration = bibleproject_pipeline.seconds_from_duration


RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>BibleProject</title>
    <item>
      <title><![CDATA[Episode With Transcript]]></title>
      <guid>guid-1</guid>
      <link>https://bibleproject.com/podcasts/episode-with-transcript/</link>
      <itunes:duration xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">01:02:03</itunes:duration>
      <description><![CDATA[
        <p>FULL SHOW NOTES <a href="https://bibleproject.com/podcasts/episode-with-transcript/">show notes</a></p>
        <p><a href="https://cdn.example.com/final-transcript.pdf">View this episode’s official transcript.</a></p>
      ]]></description>
      <enclosure url="https://audio.example.com/e1.mp3" length="100" type="audio/mpeg" />
    </item>
    <item>
      <title><![CDATA[Episode Without Transcript]]></title>
      <guid>guid-2</guid>
      <link>https://bibleproject.com/podcasts/episode-without-transcript/</link>
      <itunes:duration xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">45:10</itunes:duration>
      <description><![CDATA[No transcript here.]]></description>
      <enclosure url="https://audio.example.com/e2.mp3" length="100" type="audio/mpeg" />
    </item>
  </channel>
</rss>
"""


def test_parse_rss_items_finds_official_transcript_and_audio_url():
    show_title, items = parse_rss_items(RSS_FIXTURE)

    assert show_title == "BibleProject"
    assert len(items) == 2
    assert items[0]["title"] == "Episode With Transcript"
    assert items[0]["guid"] == "guid-1"
    assert items[0]["duration_seconds"] == 3723
    assert items[0]["audio_url"] == "https://audio.example.com/e1.mp3"
    assert items[0]["transcript_url"] == "https://cdn.example.com/final-transcript.pdf"
    assert items[1]["transcript_url"] is None


def test_extract_transcript_links_requires_transcript_context():
    html = """
    <a href="https://example.com/general.pdf">general PDF</a>
    <a href="https://cdn.example.com/transcript.pdf">View this episode’s official transcript.</a>
    """

    assert extract_transcript_links(html) == ["https://cdn.example.com/transcript.pdf"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("01:02:03", 3723), ("45:10", 2710), ("59", 59), ("", 0)],
)
def test_seconds_from_duration(value: str, expected: int):
    assert seconds_from_duration(value) == expected


def test_state_helpers_skip_done_and_choose_next_episode(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "episodes": {
                    "guid-1": {"published_status": "imported"},
                    "guid-2": {"published_status": "pending"},
                    "guid-3": {"published_status": "error"},
                }
            }
        )
    )
    episodes = [{"guid": "guid-1"}, {"guid": "guid-2"}, {"guid": "guid-3"}]

    assert episode_status(state_path, "guid-1", "published_status") == "imported"
    assert planned_next_episode(state_path, episodes, "published_status") == {"guid": "guid-2"}


def test_safe_filename_is_stable_and_short():
    assert safe_filename("10th Commandment: Do Not Desire Your Neighbor’s Possessions") == "10th-commandment-do-not-desire-your-neighbor-s-possessions"
    assert len(safe_filename("x" * 300)) <= 120
