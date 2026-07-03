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

    assert paths["state"] == tmp_path / "hkb" / "import-state" / "a-book-like-no-other_pipeline_state.json"
    assert paths["artifact_dir"] == tmp_path / "hkb" / "source-artifacts" / "a-book-like-no-other"


def test_parser_supports_generic_transcription_subcommand():
    args = podcast_pipeline.build_parser().parse_args([
        "transcribe-missing",
        "https://example.com/feed.rss",
        "--limit",
        "2",
        "--model",
        "small",
    ])

    assert args.command == "transcribe-missing"
    assert args.limit == 2
    assert args.model == "small"
