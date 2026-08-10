from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from transcribe_podcast_remote_batch import fetch_audio, load_manifest  # noqa: E402
from youtube_pipeline import build_queue, derive_glossary  # noqa: E402


def make_discovery(entries: list[dict]) -> dict:
    return {
        "playlist_url": "https://youtube.com/playlist?list=PLtest",
        "playlist_id": "PLtest",
        "playlist_title": "Test Playlist",
        "channel": "Test Channel",
        "channel_url": "https://www.youtube.com/channel/UCtest",
        "entry_count": len(entries),
        "verified": True,
        "entries": entries,
    }


def entry(position: int, video_id: str, title: str, *, duration: int = 600, resolvable: bool = True) -> dict:
    return {
        "position": position,
        "video_id": video_id,
        "title": title,
        "duration_seconds": duration,
        "resolvable": resolvable,
    }


def build(entries: list[dict], **overrides):
    kwargs = {
        "corpus_slug": "test-playlist",
        "remote_namespace": "test-playlist-approved",
        "show_title": "Test Playlist",
        "publisher": "Test Publisher",
        "collection": "Test Collection",
        "tags": ["test"],
        "entities": [],
        "glossary_terms": ["Rabbi David Fohrman of Aleph Beta"],
    }
    kwargs.update(overrides)
    return build_queue(make_discovery(entries), **kwargs)


def test_unresolvable_videos_are_excluded_despite_reported_duration():
    # A deleted video keeps its flat-listing duration, so duration alone cannot filter it.
    manifest = build(
        [
            entry(1, "aaaaaaaaaaa", "Parshat Bereishit: One Story?"),
            entry(2, "bbbbbbbbbbb", "Parshat Noach: Another Flood?", duration=717, resolvable=False),
            entry(3, "ccccccccccc", "Parshat Lech Lecha: Wandering?"),
        ]
    )

    assert manifest["approved_episode_count"] == 2
    assert [item["queue_number"] for item in manifest["episodes"]] == [1, 2]
    assert manifest["excluded_episodes"] == [
        {
            "video_id": "bbbbbbbbbbb",
            "title": "Parshat Noach: Another Flood?",
            "canonical_url": "https://www.youtube.com/watch?v=bbbbbbbbbbb",
            "playlist_position": 2,
            "exclusion_reason": "video_unavailable",
        }
    ]


def test_queue_numbers_stay_contiguous_after_exclusion():
    """The orchestrator rejects manifests whose queue numbers have gaps."""
    manifest = build(
        [
            entry(1, "aaaaaaaaaaa", "First", resolvable=False),
            entry(2, "bbbbbbbbbbb", "Second"),
            entry(3, "ccccccccccc", "Third"),
        ]
    )

    assert [item["queue_number"] for item in manifest["episodes"]] == [1, 2]
    assert [item["artifact_stem"] for item in manifest["episodes"]] == ["q001-yt-bbbbbbbbbbb", "q002-yt-ccccccccccc"]
    # Playlist position is preserved separately so the original ordering stays recoverable.
    assert [item["playlist_position"] for item in manifest["episodes"]] == [2, 3]


def test_artifact_stem_is_lowercased_but_the_video_id_keeps_its_case():
    manifest = build([entry(1, "VIIkUeXFFnY", "Mixed Case ID")])
    item = manifest["episodes"][0]

    assert item["artifact_stem"] == "q001-yt-viikuexffny"
    # The case-sensitive ID survives untouched for round-tripping back to YouTube.
    assert item["external_ids"]["youtube_video_id"] == "VIIkUeXFFnY"
    assert item["canonical_url"] == "https://www.youtube.com/watch?v=VIIkUeXFFnY"


def test_video_repeated_in_playlist_is_excluded_once():
    manifest = build(
        [
            entry(1, "aaaaaaaaaaa", "First airing"),
            entry(2, "bbbbbbbbbbb", "Different video"),
            entry(3, "aaaaaaaaaaa", "Rebroadcast of the first"),
        ]
    )

    assert manifest["approved_episode_count"] == 2
    assert [item["guid"] for item in manifest["episodes"]] == ["youtube-aaaaaaaaaaa", "youtube-bbbbbbbbbbb"]
    assert manifest["excluded_episodes"][0]["exclusion_reason"] == "duplicate_in_playlist"


def test_items_are_marked_for_yt_dlp_fetch_with_watch_urls():
    manifest = build([entry(1, "aaaaaaaaaaa", "Only")])
    item = manifest["episodes"][0]

    assert item["audio_fetch"] == "yt-dlp"
    assert item["audio_url"] == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert item["canonical_url"] == item["audio_url"]
    assert item["guid"] == "youtube-aaaaaaaaaaa"


def test_glossary_derives_parsha_names_from_titles():
    prompt = derive_glossary(
        [
            "Parshat Bereishit: Is The Torah One Big Story?",
            "Parshat Lech Lecha: Was Abraham The First Wandering Jew?",
            "Parshat Ha'azinu: Moses' Farewell To Israel",
            "A Title With No Parsha Prefix",
        ],
        ["Rabbi David Fohrman of Aleph Beta"],
    )

    assert "Rabbi David Fohrman of Aleph Beta" in prompt
    assert "Parshat Bereishit" in prompt
    assert "Parshat Lech Lecha" in prompt
    assert "Parshat Ha'azinu" in prompt
    assert "No Parsha Prefix" not in prompt


def test_glossary_is_truncated_to_the_initial_prompt_budget():
    titles = [f"Parshat Name{index}: A Question?" for index in range(200)]
    prompt = derive_glossary(titles, [])
    assert len(prompt.split()) <= 160


def test_generated_manifest_passes_the_orchestrator_validator(tmp_path):
    names = ["Bereishit", "Noach", "Lech Lecha"]
    manifest = build([entry(index, f"video{index:07d}", f"Parshat {name}: A Question?") for index, name in enumerate(names, start=1)])
    path = tmp_path / "approved-queue.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    loaded = load_manifest(path)
    assert loaded["approved_episode_count"] == 3
    # Configured terms lead, then the names derived from the playlist's own titles.
    assert loaded["initial_prompt"].startswith("Rabbi David Fohrman of Aleph Beta.")
    assert "Parshat Lech Lecha" in loaded["initial_prompt"]


def test_unknown_audio_fetch_strategy_is_rejected(tmp_path):
    """A typo must fail validation rather than silently GET a watch page as audio."""
    manifest = build([entry(1, "aaaaaaaaaaa", "Only")])
    manifest["episodes"][0]["audio_fetch"] = "ytdlp"
    path = tmp_path / "approved-queue.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown audio_fetch"):
        load_manifest(path)


def test_fetch_audio_dispatches_on_the_declared_strategy(monkeypatch, tmp_path):
    calls: list[str] = []

    def fake_yt_dlp(url, destination, *, attempts=3):
        calls.append(f"yt-dlp:{url}")
        return 1.0

    def fake_http(url, destination, *, attempts=3):
        calls.append(f"http:{url}")
        return 1.0

    import transcribe_podcast_remote_batch as module

    monkeypatch.setattr(module, "download_with_yt_dlp", fake_yt_dlp)
    monkeypatch.setattr(module, "download_with_retries", fake_http)

    fetch_audio({"audio_url": "https://youtube.com/watch?v=x", "audio_fetch": "yt-dlp"}, tmp_path / "a.mp3")
    fetch_audio({"audio_url": "https://example.com/ep.mp3"}, tmp_path / "b.mp3")

    assert calls == ["yt-dlp:https://youtube.com/watch?v=x", "http:https://example.com/ep.mp3"]
