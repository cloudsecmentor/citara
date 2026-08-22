#!/usr/bin/env python3
"""Repoint Anchor-hosted podcast sources at open.spotify.com episode URLs.

Citation deep links are built by appending `?t=<seconds>` to
`sources.canonical_url` (see `core/retrieval/base._timestamp_url`). Episodes
ingested from an Anchor RSS feed carry a `podcasters.spotify.com` /
`creators.spotify.com` link, and that page ignores `t=` -- so every timestamp
link for those sources is well-formed but does not seek. `open.spotify.com`
episode URLs do honor it.

The two URL forms use unrelated id spaces (Anchor's trailing `-e1du4b3` is
base36; Spotify episode ids are 22-character base62), so there is no
conversion -- the mapping has to be looked up. This joins:

    sources.metadata_json.episode_guid
      -> RSS <guid>  -> RSS <title>
      -> Spotify episode title -> Spotify episode id

Titles are matched after normalization, and every unmatched row is reported
rather than guessed at.

Requires a Spotify app's client credentials (free:
https://developer.spotify.com/dashboard -> Create App). Both values are
needed -- the client-credentials grant sends them as an HTTP Basic pair:

    SPOTIFY_CLIENT_ID=...  SPOTIFY_CLIENT_SECRET=...

Dry run first (the default -- writing requires --yes):

    uv run python scripts/fix_spotify_episode_urls.py
    uv run python scripts/fix_spotify_episode_urls.py --yes
"""

from __future__ import annotations

import argparse
import base64
import html
import os
import re
import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# "Text in Us" on Spotify. Recovered from the server-rendered embed endpoint
# (https://open.spotify.com/embed/episode/<id>), which exposes the show uri
# even though the main episode page is a JavaScript shell.
DEFAULT_SHOW_ID = "7MUydNdhmGQmGOsbwrr5tb"
DEFAULT_FEED_URL = "https://anchor.fm/s/7cd8d890/podcast/rss"

# Hosts whose pages ignore the `t=` query parameter.
STALE_URL_PATTERNS = ("podcasters.spotify.com", "creators.spotify.com", "anchor.fm")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="DATABASE_URL override.")
    parser.add_argument("--show-id", default=DEFAULT_SHOW_ID)
    parser.add_argument("--feed-url", default=DEFAULT_FEED_URL)
    parser.add_argument("--market", default="US", help="Spotify market; episode listings are market-scoped.")
    parser.add_argument("--yes", action="store_true", help="Apply the updates. Without it, this is a dry run.")
    parser.add_argument("--limit", type=int, help="Only process this many sources (for a cautious first pass).")
    return parser.parse_args()


args = _parse_args()

if args.db:
    os.environ["DATABASE_URL"] = args.db
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from citara.core.db import SessionLocal, init_db  # noqa: E402
from citara.core.models import Source  # noqa: E402


def normalize_title(title: str) -> str:
    """Collapse a title to a comparable key.

    Feed and Spotify titles agree in substance but drift in punctuation --
    curly vs straight quotes, en/em dashes, doubled spaces, ampersands. NFKD
    folding plus stripping non-alphanumerics removes every difference seen
    across this show's 170 episodes without loosening the match enough to
    collide (all 170 titles stay distinct under it).
    """
    folded = unicodedata.normalize("NFKD", html.unescape(title))
    folded = folded.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def fetch_feed_titles(feed_url: str) -> dict[str, str]:
    """Map RSS guid -> episode title."""
    text = httpx.get(feed_url, timeout=120, follow_redirects=True).text
    titles: dict[str, str] = {}
    for item in re.findall(r"<item>(.*?)</item>", text, re.S):
        guid = re.search(r"<guid[^>]*>(.*?)</guid>", item, re.S)
        title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", item, re.S) or re.search(r"<title>(.*?)</title>", item, re.S)
        if guid and title:
            titles[guid.group(1).strip()] = html.unescape(title.group(1).strip())
    return titles


def spotify_token(client_id: str, client_secret: str) -> str:
    pair = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = httpx.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {pair}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials"},
        timeout=60,
    )
    if response.status_code != 200:
        raise SystemExit(f"Spotify token request failed ({response.status_code}): {response.text[:300]}")
    return response.json()["access_token"]


def fetch_show_episodes(token: str, show_id: str, market: str) -> dict[str, str]:
    """Map normalized episode title -> Spotify episode id."""
    episodes: dict[str, str] = {}
    url = f"https://api.spotify.com/v1/shows/{show_id}/episodes?limit=50&market={market}"
    while url:
        response = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        if response.status_code != 200:
            raise SystemExit(f"Spotify episode listing failed ({response.status_code}): {response.text[:300]}")
        payload = response.json()
        for item in payload.get("items") or []:
            if item and item.get("id") and item.get("name"):
                episodes[normalize_title(item["name"])] = item["id"]
        url = payload.get("next")
    return episodes


def main() -> None:
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    missing = [n for n, v in {"SPOTIFY_CLIENT_ID": client_id, "SPOTIFY_CLIENT_SECRET": client_secret}.items() if not v]
    if missing:
        raise SystemExit(
            f"Missing {', '.join(missing)}.\n"
            "Both are required: the client-credentials grant sends them as an HTTP Basic pair, "
            "so a client id on its own cannot obtain a token.\n"
            "Create a free app at https://developer.spotify.com/dashboard and put both in .env."
        )
    assert client_id and client_secret  # narrowed above; keeps mypy happy

    print(f"Feed:  {args.feed_url}")
    print(f"Show:  {args.show_id}")
    print(f"Mode:  {'APPLY (writing)' if args.yes else 'dry run (no writes)'}\n")

    feed_titles = fetch_feed_titles(args.feed_url)
    print(f"RSS episodes:      {len(feed_titles)}")

    episodes = fetch_show_episodes(spotify_token(client_id, client_secret), args.show_id, args.market)
    print(f"Spotify episodes:  {len(episodes)}")

    init_db()
    with SessionLocal() as session:
        sources = [
            source
            for source in session.execute(select(Source)).scalars()
            if source.canonical_url and any(pattern in source.canonical_url for pattern in STALE_URL_PATTERNS)
        ]
        if args.limit:
            sources = sources[: args.limit]
        print(f"Sources to fix:    {len(sources)}\n")

        updated = 0
        no_guid: list[str] = []
        no_feed_match: list[str] = []
        no_spotify_match: list[str] = []

        for source in sources:
            metadata = dict(source.metadata_json or {})
            guid = metadata.get("episode_guid")
            if not guid:
                no_guid.append(source.title)
                continue
            feed_title = feed_titles.get(guid)
            if not feed_title:
                no_feed_match.append(source.title)
                continue
            episode_id = episodes.get(normalize_title(feed_title))
            if not episode_id:
                no_spotify_match.append(feed_title)
                continue

            new_url = f"https://open.spotify.com/episode/{episode_id}"
            if source.canonical_url == new_url:
                continue

            if args.yes:
                # Keep the original for provenance; this is a lossy rewrite of
                # a field used in citations.
                metadata.setdefault("anchor_url", source.canonical_url)
                metadata["spotify_episode_id"] = episode_id
                source.metadata_json = metadata
                source.canonical_url = new_url
            updated += 1
            if updated <= 3:
                print(f"  {feed_title[:52]:54s} -> {new_url}")

        if args.yes:
            session.commit()

        print(f"\nWould update: {updated}" if not args.yes else f"\nUpdated: {updated}")
        for label, rows in (
            ("no episode_guid in metadata", no_guid),
            ("guid not present in RSS feed", no_feed_match),
            ("no Spotify episode matched title", no_spotify_match),
        ):
            if rows:
                print(f"  SKIPPED - {label}: {len(rows)}")
                for row in rows[:5]:
                    print(f"      {row[:70]}")

        if not args.yes:
            print("\nNothing was written. Re-run with --yes to apply.")


if __name__ == "__main__":
    main()
