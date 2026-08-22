from __future__ import annotations

import importlib
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_module(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["youtube_channel_plan.py"])
    sys.modules.pop("youtube_channel_plan", None)
    return importlib.import_module("youtube_channel_plan")


def test_channel_batch_uses_dedicated_local_and_remote_namespaces(monkeypatch, tmp_path):
    module = load_module(monkeypatch)

    location = module.channel_batch_location(tmp_path, "into-the-verse")

    assert location.local_dir == tmp_path / "into-the-verse" / "remote-youtube-channel"
    assert location.manifest_path == location.local_dir / "approved-queue.json"
    assert location.remote_namespace == "alephbeta-channel-into-the-verse"
    assert location.local_dir != tmp_path / "into-the-verse" / "remote-openai"


def test_merge_channel_tabs_deduplicates_ids_and_preserves_origins(monkeypatch):
    module = load_module(monkeypatch)
    videos = {
        "playlist_url": "https://www.youtube.com/@AlephBeta/videos",
        "entries": [
            {"position": 1, "video_id": "aaaaaaaaaaa", "title": "Regular", "duration_seconds": 600},
            {"position": 2, "video_id": "bbbbbbbbbbb", "title": "Also shown as Short", "duration_seconds": 55},
        ],
    }
    shorts = {
        "playlist_url": "https://www.youtube.com/@AlephBeta/shorts",
        "entries": [
            {"position": 1, "video_id": "bbbbbbbbbbb", "title": "Also shown as Short", "duration_seconds": 55},
            {"position": 2, "video_id": "ccccccccccc", "title": "Short only", "duration_seconds": 45},
        ],
    }

    merged = module.merge_channel_tabs([("videos", videos), ("shorts", shorts)])

    assert merged["entry_count"] == 3
    assert [entry["video_id"] for entry in merged["entries"]] == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    assert merged["entries"][1]["channel_tabs"] == ["videos", "shorts"]
    assert merged["entries"][2]["channel_tabs"] == ["shorts"]


def test_manifest_totals_exclude_unusable_candidates(monkeypatch):
    module = load_module(monkeypatch)

    count, duration = module.manifest_totals(
        [
            {"approved_episode_count": 2, "total_duration_seconds": 100},
            {"approved_episode_count": 3, "total_duration_seconds": 200},
        ]
    )

    assert count == 5
    assert duration == 300


def test_parser_includes_shorts_by_default(monkeypatch):
    module = load_module(monkeypatch)

    args = module._parse_args()

    assert args.shorts_url == "https://www.youtube.com/@AlephBeta/shorts"


def test_short_only_video_without_playlist_uses_shorts_tree(monkeypatch):
    module = load_module(monkeypatch)

    slug, reason = module.assign_entry_tree([], ["shorts"])

    assert slug == "alephbeta-shorts"
    assert reason == "shorts_tab"


def test_short_in_named_playlist_keeps_named_tree(monkeypatch):
    module = load_module(monkeypatch)

    slug, reason = module.assign_entry_tree(["Hanukkah"], ["shorts"])

    assert slug == "hanukkah"
    assert reason == "Hanukkah"
