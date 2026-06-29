from __future__ import annotations


def test_parse_podcast_rss_discovers_show_and_episode(fixtures_dir):
    from hermes_knowledge.core.podcasts.discovery import parse_podcast_rss

    xml = (fixtures_dir / "sources" / "podcasts" / "sample_feed.xml").read_text()
    feed = parse_podcast_rss(xml, rss_url="https://example.com/feed.xml")

    assert feed.show_title == "Test Podcast"
    assert feed.rss_url == "https://example.com/feed.xml"
    assert len(feed.episodes) == 1
    episode = feed.episodes[0]
    assert episode.episode_title == "Ambiguity and Action"
    assert episode.episode_url == "https://example.com/podcast/ambiguity-action"
    assert episode.audio_url == "https://example.com/audio/ambiguity-action.mp3"
    assert episode.guid == "ambiguity-action"


def test_classify_podcast_input_detects_rss_url():
    from hermes_knowledge.core.podcasts.discovery import classify_podcast_input

    assert classify_podcast_input("https://example.com/feed.xml") == "rss_feed"
    assert classify_podcast_input("https://example.com/podcast/episode") == "episode_or_show_page"
