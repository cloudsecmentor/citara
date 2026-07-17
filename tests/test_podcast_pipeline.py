from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "podcast_pipeline.py"
spec = importlib.util.spec_from_file_location("podcast_pipeline", SCRIPT_PATH)
podcast_pipeline = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(podcast_pipeline)


RSS_WITH_TRANSCRIPT = """<?xml version="1.0"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>Configurable Podcast</title>
    <link>https://example.com/show</link>
    <item>
      <title>Episode One</title>
      <guid>episode-one-guid</guid>
      <link>https://example.com/episodes/one</link>
      <pubDate>Tue, 01 Jan 2026 00:00:00 GMT</pubDate>
      <itunes:duration>01:02:03</itunes:duration>
      <enclosure url="https://cdn.example.com/one.mp3" type="audio/mpeg" />
      <podcast:transcript url="https://cdn.example.com/one.html" type="text/html" language="en" />
      <podcast:transcript url="https://cdn.example.com/one.vtt" type="text/vtt" language="en" />
    </item>
  </channel>
</rss>
"""


RSS_BIBLEPROJECT_WITH_TRANSCRIPT = """<?xml version="1.0" encoding="UTF-8"?>
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
  </channel>
</rss>
"""

RSS_BEMA = """<?xml version="1.0" encoding="UTF-8"?>
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
  </channel>
</rss>
"""


def test_resolve_apple_podcast_url_uses_itunes_lookup_feed_url():
    payload = {
        "resultCount": 1,
        "results": [
            {
                "collectionName": "A Book Like No Other",
                "artistName": "Aleph Beta",
                "feedUrl": "https://rss.buzzsprout.com/2113502.rss",
            }
        ],
    }

    def fake_fetch_text(url: str, timeout: int = 60) -> str:
        assert url == "https://itunes.apple.com/lookup?id=1667348746&entity=podcast"
        return json.dumps(payload)

    resolved = podcast_pipeline.resolve_input_url(
        "https://podcasts.apple.com/se/podcast/a-book-like-no-other/id1667348746?l=en-GB",
        fetch_text_fn=fake_fetch_text,
    )

    assert resolved["feed_url"] == "https://rss.buzzsprout.com/2113502.rss"
    assert resolved["show_title"] == "A Book Like No Other"
    assert resolved["input_kind"] == "apple_podcast"


def test_resolve_apple_episode_url_captures_episode_adam_id():
    payload = {
        "resultCount": 1,
        "results": [
            {
                "collectionName": "A Book Like No Other",
                "artistName": "Aleph Beta",
                "feedUrl": "https://rss.buzzsprout.com/2113502.rss",
            }
        ],
    }

    def fake_fetch_text(url: str, timeout: int = 60) -> str:
        assert url == "https://itunes.apple.com/lookup?id=1667348746&entity=podcast"
        return json.dumps(payload)

    resolved = podcast_pipeline.resolve_input_url(
        "https://podcasts.apple.com/se/podcast/a-book-like-no-other/id1667348746?l=en-GB&i=1000724527136",
        fetch_text_fn=fake_fetch_text,
    )

    assert resolved["input_kind"] == "apple_episode"
    assert resolved["episode_adam_id"] == "1000724527136"
    assert resolved["feed_url"] == "https://rss.buzzsprout.com/2113502.rss"


def test_parse_rss_items_reads_podcast_transcript_and_duration():
    show_title, episodes = podcast_pipeline.parse_rss_items(RSS_WITH_TRANSCRIPT, feed_url="https://example.com/feed.rss")

    assert show_title == "Configurable Podcast"
    assert episodes == [
        {
            "show_title": "Configurable Podcast",
            "title": "Episode One",
            "guid": "episode-one-guid",
            "episode_url": "https://example.com/episodes/one",
            "publish_date": "Tue, 01 Jan 2026 00:00:00 GMT",
            "duration": "01:02:03",
            "duration_seconds": 3723,
            "audio_url": "https://cdn.example.com/one.mp3",
            "transcript_url": "https://cdn.example.com/one.vtt",
            "transcript_type": "text/vtt",
            "transcript_language": "en",
            "transcript_urls": ["https://cdn.example.com/one.vtt", "https://cdn.example.com/one.html"],
            "feed_url": "https://example.com/feed.rss",
        }
    ]


def test_make_segments_from_plain_text_approximates_timestamps():
    text = "First paragraph has useful words.\n\nSecond paragraph has more useful words."

    segments = podcast_pipeline.make_segments_from_text(text, duration_seconds=100)

    assert [segment["text"] for segment in segments] == [
        "First paragraph has useful words.",
        "Second paragraph has more useful words.",
    ]
    assert segments[0]["start_ms"] == 0
    assert 0 < segments[0]["end_ms"] < 100000
    assert segments[1]["start_ms"] == segments[0]["end_ms"]
    assert segments[1]["end_ms"] == 100000


def test_state_paths_are_namespaced_by_slug(tmp_path):
    paths = podcast_pipeline.paths_for_slug("A Book Like No Other", base_dir=tmp_path)

    assert paths["state"] == tmp_path / "citara" / "import-state" / "a-book-like-no-other_pipeline_state.json"
    assert paths["artifact_dir"] == tmp_path / "citara" / "source-artifacts" / "a-book-like-no-other"


def test_configured_bibleproject_connector_extracts_transcript_links():
    config = {"connector": "bibleproject", "feed_url": "https://example.com/bibleproject.rss"}

    show_title, episodes = podcast_pipeline.parse_source_rss_items(config, RSS_BIBLEPROJECT_WITH_TRANSCRIPT, feed_url=config["feed_url"])

    assert show_title == "BibleProject"
    assert episodes[0]["transcript_url"] == "https://cdn.example.com/final-transcript.pdf"
    assert episodes[0]["rss_link"] == "https://bibleproject.com/podcasts/episode-with-transcript/"


def test_configured_bema_connector_preserves_episode_metadata():
    config = {"connector": "bema", "feed_url": "https://www.bemadiscipleship.com/rss"}

    show_title, episodes = podcast_pipeline.parse_source_rss_items(config, RSS_BEMA, feed_url=config["feed_url"])

    assert show_title == "The BEMA Podcast"
    assert episodes[0]["episode"] == "1"
    assert episodes[0]["season"] == "1"
    assert episodes[0]["episode_title"] == "Trust the Story"


def test_load_config_or_url_resolves_named_source_from_config_file(tmp_path):
    config_path = tmp_path / "citara.sources.json"
    config_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "name": "bema",
                        "kind": "podcast",
                        "connector": "bema",
                        "feed_url": "https://www.bemadiscipleship.com/rss",
                        "slug": "bema",
                        "show_title": "The BEMA Podcast",
                    }
                ]
            }
        )
    )

    config = podcast_pipeline.load_config_or_url("bema", config_path=config_path)

    assert config["name"] == "bema"
    assert config["connector"] == "bema"
    assert config["feed_url"] == "https://www.bemadiscipleship.com/rss"


def test_load_config_or_url_treats_unknown_value_as_direct_url(tmp_path):
    config_path = tmp_path / "citara.sources.json"
    config_path.write_text(json.dumps({"sources": []}))

    config = podcast_pipeline.load_config_or_url("https://example.com/feed.rss", config_path=config_path)

    assert config == {"url": "https://example.com/feed.rss"}


def test_parser_supports_config_driven_source_names_and_config_path():
    args = podcast_pipeline.build_parser().parse_args(
        [
            "--config",
            "citara.sources.json",
            "transcribe-missing",
            "bema",
            "--limit",
            "2",
            "--model",
            "small",
        ]
    )

    assert args.config == "citara.sources.json"
    assert args.command == "transcribe-missing"
    assert args.input == "bema"
    assert args.limit == 2
    assert args.model == "small"
