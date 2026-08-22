#!/usr/bin/env python3
"""Plan a whole-channel YouTube import, split into per-show source trees.

`youtube_pipeline.py` imports one playlist into one tree. Importing an entire
channel that way means either one undifferentiated bucket, or a run per
playlist -- and a run per playlist double-imports the videos that sit in more
than one. This does a single channel discovery, assigns each video exactly one
tree, and emits one queue manifest per tree, ready for
`transcribe_podcast_remote_batch.py`.

Two things it will not do:

* Re-import anything already in the corpus. Existing YouTube videos are matched
  by id (recoverable from `canonical_url` and `metadata_json.guid`). Episodes
  imported from a podcast RSS feed carry no video id, so those are matched by
  normalized title instead -- otherwise the video version of an episode you
  already have as audio would be ingested a second time.
* Guess when a video's show is genuinely ambiguous. Those land in a single
  fallback tree rather than being assigned by coin flip.

Dry run first (the default -- writing manifests requires --write):

    uv run python scripts/youtube_channel_plan.py
    uv run python scripts/youtube_channel_plan.py --write
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

CHANNEL_URL = "https://www.youtube.com/@AlephBeta/videos"
SHORTS_URL = "https://www.youtube.com/@AlephBeta/shorts"
PLAYLISTS_URL = "https://www.youtube.com/@AlephBeta/playlists"
FALLBACK_SLUG = "alephbeta-misc"
SHORTS_SLUG = "alephbeta-shorts"
CHANNEL_BATCH_DIR = "remote-youtube-channel"
CHANNEL_REMOTE_PREFIX = "alephbeta-channel"

# Playlist title -> (source_tree_slug, priority). Lower priority wins when a
# video belongs to several playlists.
#
# The channel's "Holiday Videos" and "Course Trailers" are umbrella playlists
# that overlap the specific ones -- nearly every multi-playlist video is
# `<specific> + umbrella`. Ranking the umbrellas last resolves 29 of the 30
# ambiguous videos to the show a reader would expect. A video whose best
# priority is shared by two *different* trees stays ambiguous and goes to the
# fallback rather than being assigned arbitrarily.
#
# Slugs that already exist in the corpus are reused deliberately, so new videos
# join their show instead of starting a parallel tree.
TREE_RULES: dict[str, tuple[str, int]] = {
    # Named shows (existing trees).
    "Podcast: A Book Like No Other": ("a-book-like-no-other", 10),
    "Podcast: Into the Verse - Parsha Podcast": ("into-the-verse", 10),
    "Podcast: Meaningful Judaism": ("meaningful-judaism", 10),
    "Weekly Parsha Experiment": ("weekly-parsha-experiment", 10),
    # Named show (new tree).
    "Aleph Beta Quarantined: Podcast": ("aleph-beta-quarantined", 10),
    # Specific holidays.
    "High Holidays": ("high-holidays", 20),
    "Hanukkah": ("hanukkah", 20),
    "Passover": ("passover", 20),
    "Shavuot": ("shavuot", 20),
    "Purim": ("purim", 20),
    "Tisha B'Av": ("tisha-bav", 20),
    # Format series.
    "Parsha: 50 Second Recaps": ("parsha-50-second-recaps", 30),
    # Umbrellas -- only used when nothing more specific applies.
    "Holiday Videos": ("holiday-videos", 90),
    "Course Trailers": ("course-trailers", 90),
}

# Existing source whose publisher/entities/glossary the new trees inherit, so
# every Aleph Beta tree keeps consistent entity links and vocabulary hints.
TEMPLATE_SOURCE = "weekly-parsha-experiment"


@dataclass(frozen=True)
class ChannelBatchLocation:
    local_dir: Path
    manifest_path: Path
    remote_namespace: str


def channel_batch_location(artifact_root: Path, source_tree_slug: str) -> ChannelBatchLocation:
    """Return a channel-only namespace that cannot collide with podcast batches."""
    local_dir = artifact_root / source_tree_slug / CHANNEL_BATCH_DIR
    return ChannelBatchLocation(
        local_dir=local_dir,
        manifest_path=local_dir / "approved-queue.json",
        remote_namespace=f"{CHANNEL_REMOTE_PREFIX}-{source_tree_slug}",
    )


def merge_channel_tabs(tabs: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Merge channel-tab discoveries by video ID while retaining provenance."""
    merged_entries: list[dict[str, Any]] = []
    by_video_id: dict[str, dict[str, Any]] = {}
    for tab_name, discovery in tabs:
        for entry in discovery.get("entries", []):
            video_id = entry.get("video_id")
            if video_id and video_id in by_video_id:
                existing = by_video_id[video_id]
                if tab_name not in existing["channel_tabs"]:
                    existing["channel_tabs"].append(tab_name)
                continue
            merged = {**entry, "channel_tabs": [tab_name]}
            merged_entries.append(merged)
            if video_id:
                by_video_id[video_id] = merged
    for position, entry in enumerate(merged_entries, start=1):
        entry["position"] = position
    first = tabs[0][1] if tabs else {}
    return {
        **{key: value for key, value in first.items() if key != "entries"},
        "playlist_url": first.get("channel_url") or first.get("playlist_url"),
        "playlist_title": first.get("channel") or first.get("playlist_title"),
        "included_tabs": [name for name, _ in tabs],
        "entry_count": len(merged_entries),
        "entries": merged_entries,
    }


def manifest_totals(manifests: list[dict[str, Any]]) -> tuple[int, int]:
    """Return only validated/approved item totals, excluding unusable candidates."""
    return (
        sum(int(manifest["approved_episode_count"]) for manifest in manifests),
        sum(int(manifest["total_duration_seconds"]) for manifest in manifests),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="DATABASE_URL override.")
    parser.add_argument("--channel-url", default=CHANNEL_URL)
    parser.add_argument("--shorts-url", default=SHORTS_URL)
    parser.add_argument("--playlists-url", default=PLAYLISTS_URL)
    parser.add_argument("--config", type=Path, default=REPO / "citara.sources.json")
    parser.add_argument("--artifact-root", type=Path, default=None)
    parser.add_argument("--cache", type=Path, default=None, help="Directory for discovery/playlist caches.")
    parser.add_argument("--refresh", action="store_true", help="Re-query YouTube instead of using the cache.")
    parser.add_argument("--skip-verify", action="store_true", help="Skip the per-video resolve pass (faster, less safe).")
    parser.add_argument(
        "--include-title-duplicates",
        action="store_true",
        help="Queue videos whose title already exists in the corpus from another source (e.g. the podcast audio).",
    )
    parser.add_argument("--write", action="store_true", help="Write the per-tree manifests. Without it, this is a dry run.")
    return parser.parse_args()


args = _parse_args()

if args.db:
    os.environ["DATABASE_URL"] = args.db
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from sqlalchemy import select  # noqa: E402

from artifact_paths import source_artifact_root  # noqa: E402
from citara.core.db import SessionLocal, init_db  # noqa: E402
from citara.core.models import Source  # noqa: E402
from youtube_pipeline import build_queue, discover, run_yt_dlp, write_json_atomic  # noqa: E402

VIDEO_ID_RE = re.compile(r"(?:watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})")


def normalize_title(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def corpus_state() -> tuple[set[str], dict[str, str]]:
    """Existing YouTube video ids, and normalized title -> owning tree."""
    init_db()
    video_ids: set[str] = set()
    titles: dict[str, str] = {}
    with SessionLocal() as session:
        for source in session.execute(select(Source)).scalars():
            metadata = source.metadata_json or {}
            if source.canonical_url:
                found = VIDEO_ID_RE.search(source.canonical_url)
                if found:
                    video_ids.add(found.group(1))
            guid = str(metadata.get("guid") or "")
            if guid.startswith("youtube-"):
                video_ids.add(guid[len("youtube-") :])

            # Strip the "Show: " prefix and the "(Generated Transcript)" style
            # suffix that ingestion adds, so titles compare against YouTube's.
            bare = re.sub(r"^[^:]+:\s*", "", source.title)
            bare = re.sub(r"\s*\((Generated|Published|Current|Legacy)[^)]*\)\s*$", "", bare)
            titles.setdefault(normalize_title(bare), metadata.get("source_tree_slug") or "(no tree)")
    return video_ids, titles


def fetch_playlist_members(playlists_url: str, cache_dir: Path, *, refresh: bool) -> dict[str, list[str]]:
    """Map playlist title -> video ids."""
    cache = cache_dir / "playlist-members.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))

    listing = run_yt_dlp(["--flat-playlist", "--print", "%(id)s|%(title)s", playlists_url])
    members: dict[str, list[str]] = {}
    for line in listing.splitlines():
        if "|" not in line:
            continue
        playlist_id, title = line.split("|", 1)
        ids = run_yt_dlp(["--flat-playlist", "--print", "%(id)s", f"https://www.youtube.com/playlist?list={playlist_id}"])
        members[title.strip()] = [v.strip() for v in ids.splitlines() if v.strip()]
    write_json_atomic(cache, members)
    return members


def assign_tree(playlists: list[str]) -> tuple[str, str]:
    """Choose a tree for a video. Returns (slug, reason)."""
    known = [(TREE_RULES[p][1], TREE_RULES[p][0], p) for p in playlists if p in TREE_RULES]
    if not known:
        return FALLBACK_SLUG, "no_playlist" if not playlists else "unmapped_playlist"
    best = min(priority for priority, _, _ in known)
    winners = {slug for priority, slug, _ in known if priority == best}
    if len(winners) > 1:
        return FALLBACK_SLUG, "ambiguous:" + "+".join(sorted(winners))
    slug = winners.pop()
    return slug, next(p for priority, s, p in known if s == slug and priority == best)


def assign_entry_tree(playlists: list[str], channel_tabs: list[str]) -> tuple[str, str]:
    """Prefer playlist semantics, then keep otherwise-unclassified Shorts separate."""
    slug, reason = assign_tree(playlists)
    if slug == FALLBACK_SLUG and reason == "no_playlist" and "shorts" in channel_tabs:
        return SHORTS_SLUG, "shorts_tab"
    return slug, reason


def load_template(config_path: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for entry in config.get("sources", []):
        if entry.get("name") == TEMPLATE_SOURCE:
            return entry
    raise SystemExit(f"template source {TEMPLATE_SOURCE!r} not found in {config_path}")


def main() -> int:
    artifact_root = args.artifact_root or source_artifact_root()
    cache_dir = args.cache or (artifact_root / "alephbeta-channel" / "planning")
    cache_dir.mkdir(parents=True, exist_ok=True)

    template = load_template(args.config)
    existing_ids, existing_titles = corpus_state()
    print(f"Corpus: {len(existing_ids)} YouTube video ids, {len(existing_titles)} titles\n")

    discovery_cache = cache_dir / "discovery.json"
    included_tabs = [("videos", args.channel_url), ("shorts", args.shorts_url)]
    cached_discovery = json.loads(discovery_cache.read_text(encoding="utf-8")) if discovery_cache.exists() else None
    if cached_discovery and cached_discovery.get("included_tabs") == [name for name, _ in included_tabs] and not args.refresh:
        discovery = cached_discovery
        print(f"Channel: {discovery['entry_count']} unique videos across videos + shorts (cached)")
    else:
        tab_discoveries: list[tuple[str, dict[str, Any]]] = []
        for tab_name, tab_url in included_tabs:
            tab_cache_dir = cache_dir / tab_name
            tab_cache = tab_cache_dir / "discovery.json"
            if tab_cache.exists() and not args.refresh:
                tab_discovery = json.loads(tab_cache.read_text(encoding="utf-8"))
            elif (
                tab_name == "videos"
                and cached_discovery
                and not cached_discovery.get("included_tabs")
                and cached_discovery.get("playlist_url") == tab_url
                and not args.refresh
            ):
                # Reuse the verified pre-Shorts cache while migrating to per-tab caches.
                tab_discovery = cached_discovery
                write_json_atomic(tab_cache, tab_discovery)
            else:
                tab_discovery = discover(tab_url, tab_cache_dir, verify=not args.skip_verify)
            tab_discoveries.append((tab_name, tab_discovery))
            print(f"Channel {tab_name}: {tab_discovery['entry_count']} videos")
        discovery = merge_channel_tabs(tab_discoveries)
        write_json_atomic(discovery_cache, discovery)
        print(f"Channel total: {discovery['entry_count']} unique videos across videos + shorts")

    members = fetch_playlist_members(args.playlists_url, cache_dir, refresh=args.refresh)
    playlists_of: dict[str, list[str]] = defaultdict(list)
    for playlist_title, ids in members.items():
        for video_id in ids:
            playlists_of[video_id].append(playlist_title)
    print(f"Playlists: {len(members)}\n")

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped_known: list[str] = []
    skipped_title: list[tuple[str, str]] = []
    fallback_reasons: dict[str, str] = {}

    for entry in discovery["entries"]:
        video_id = entry.get("video_id")
        title = (entry.get("title") or "").strip()
        if not video_id:
            continue
        if video_id in existing_ids:
            skipped_known.append(title)
            continue
        owner = existing_titles.get(normalize_title(title))
        if owner and not args.include_title_duplicates:
            skipped_title.append((title, owner))
            continue

        slug, reason = assign_entry_tree(playlists_of.get(video_id, []), entry.get("channel_tabs", []))
        if slug == FALLBACK_SLUG:
            fallback_reasons[title] = reason
        buckets[slug].append(entry)

    print(f"Already in corpus by video id, skipped: {len(skipped_known)}")
    print(f"Already in corpus by title, skipped:    {len(skipped_title)}")
    for title, owner in skipped_title[:6]:
        print(f"    [{owner}] {title[:60]}")
    if len(skipped_title) > 6:
        print(f"    ... and {len(skipped_title) - 6} more")
    queued = sum(len(v) for v in buckets.values())
    print(f"\nTo import: {queued} videos across {len(buckets)} trees\n")

    manifests: list[dict[str, Any]] = []
    for slug in sorted(buckets, key=lambda s: (-len(buckets[s]), s)):
        entries = buckets[slug]
        location = channel_batch_location(artifact_root, slug)
        subset = {**discovery, "entries": entries, "entry_count": len(entries)}
        show_title = next((t for t, (s, _) in TREE_RULES.items() if s == slug), "Aleph Beta")
        try:
            manifest = build_queue(
                subset,
                corpus_slug=slug,
                remote_namespace=location.remote_namespace,
                show_title=show_title.replace("Podcast: ", ""),
                publisher=template.get("publisher"),
                collection=template.get("collection"),
                tags=sorted({*template.get("tags", []), slug} - {TEMPLATE_SOURCE}),
                entities=template.get("entities", []),
                glossary_terms=template.get("glossary_terms", []),
            )
        except ValueError as exc:
            print(f"  {slug:26s} SKIPPED ({exc})")
            continue
        hours = manifest["total_duration_seconds"] / 3600
        manifests.append(manifest)
        note = "  <- fallback" if slug == FALLBACK_SLUG else ""
        print(f"  {slug:26s} {manifest['approved_episode_count']:3d} videos  {hours:5.1f}h{note}")
        if args.write:
            write_json_atomic(location.manifest_path, manifest)

    approved_count, approved_duration = manifest_totals(manifests)
    excluded_count = queued - approved_count
    print(f"\n  {'TOTAL':26s} {approved_count:3d} videos  {approved_duration / 3600:5.1f}h of audio to transcribe")
    if excluded_count:
        print(f"  {'UNUSABLE/UNAVAILABLE':26s} {excluded_count:3d} videos excluded during manifest validation")

    if fallback_reasons:
        print(f"\n{FALLBACK_SLUG} contents ({len(fallback_reasons)}):")
        for title, reason in list(fallback_reasons.items())[:12]:
            print(f"    {reason:34s} {title[:52]}")

    if not args.write:
        print("\nDry run. Nothing written. Re-run with --write to emit the manifests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
