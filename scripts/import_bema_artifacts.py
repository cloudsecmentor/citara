#!/usr/bin/env python3
"""Import BEMA published and generated transcript artifacts into the local Citara DB.

This script is intentionally DB-direct and resumable-ish for local maintenance:
- published Google Doc transcript links are discovered from saved BEMA source-page.html files
- already normalized published artifacts are imported first
- OpenAI/Whisper raw chunked transcripts are imported only for episodes without a current published transcript
- BEMA DB rows can be rebuilt without disturbing other sources
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

DEFAULT_CITARA_ROOT = Path("../citara")
DEFAULT_OPENAI_RAW = Path("../citara-data/source-artifacts/bema/remote-openai")

# Ensure direct script runs use the renamed sibling data folder even when no .env is loaded.
os.environ.setdefault("DATABASE_URL", f"sqlite:///{DEFAULT_CITARA_ROOT / 'citara.db'}")
os.environ.setdefault("SOURCE_ARTIFACT_ROOT", str(DEFAULT_CITARA_ROOT / "source-artifacts"))
os.environ.setdefault("SOURCE_STATE_ROOT", str(DEFAULT_CITARA_ROOT / "import-state"))
os.environ.setdefault("OBJECT_STORE_PATH", str(DEFAULT_CITARA_ROOT / "object-store"))

from citara.connectors.podcasts.bema import (
    CURRENT_WEIGHT,
    LEGACY_WEIGHT,
    extract_google_doc_text,
    extract_transcript_links,
    make_segments,
)
from citara.core.db import SessionLocal, init_db
from citara.core.ingestion.transcript import add_transcript_source
from citara.core.models import Chunk, Embedding, IngestionJob, Source, SourceEntity, TranscriptSegment
from citara.core.source_taxonomy import BEMA_ENTITIES


def slugify(value: str, *, max_len: int = 120) -> str:
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:max_len].strip("-") or "item"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def load_state(state_path: Path) -> dict[str, Any]:
    return load_json(state_path) if state_path.exists() else {"episodes": {}, "versions": {}}


def state_by_episode(state: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for guid, episode in state.get("episodes", {}).items():
        out[str(episode.get("episode"))] = (guid, episode)
    return out


def episode_from_item_dir(item_dir: Path) -> str | None:
    match = re.fullmatch(r"bema-(\d{3})", item_dir.name)
    if match:
        return str(int(match.group(1)))
    match = re.fullmatch(r"(\d+|\d+[a-z])", item_dir.name)
    if match:
        return match.group(1)
    return None


def raw_episode_number(path: Path) -> int:
    match = re.match(r"e(\d+)-oai-raw(?:-chunked)?\.json$", path.name)
    if not match:
        raise ValueError(path.name)
    number = int(match.group(1))
    return -1 if number == 999 else number


def segments_from_chunked(path: Path) -> list[dict[str, Any]]:
    data = load_json(path)
    segments: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        text = " ".join((item.get("text") or "").split())
        if not text:
            continue
        meta = item.get("metadata") or {}
        start_raw = str(meta.get("start") or "").strip()
        if start_raw.isdigit() and len(start_raw) >= 3:
            minutes = int(start_raw[:-2] or "0")
            seconds = int(start_raw[-2:])
            start_ms = (minutes * 60 + seconds) * 1000
        elif isinstance(meta.get("start_ms"), int):
            start_ms = int(meta["start_ms"])
        else:
            start_ms = index * 120_000
        metadata = {"source_metadata": meta}
        if "overlap_chars" in meta:
            metadata["overlap_chars"] = meta["overlap_chars"]
        segments.append({"start_ms": start_ms, "end_ms": None, "speaker": None, "text": text, "metadata_json": metadata})
    for i in range(len(segments) - 1):
        segments[i]["end_ms"] = segments[i + 1]["start_ms"]
    return segments


def raw_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = load_json(path)
    return data if isinstance(data, dict) else None


def format_mmss(seconds: float) -> str:
    seconds_i = max(int(seconds), 0)
    return f"{seconds_i // 60:02d}{seconds_i % 60:02d}"


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith((".", "?", "!", ".”", '?"', '!"', ".'", "?'", "!'"))


def _choose_chunk_end(units: list[dict[str, Any]], start: int, *, target_chars: int, max_chars: int) -> int:
    """Return exclusive end index, preferring sentence-like segment boundaries."""
    total = 0
    best_sentence_after_min: int | None = None
    best_sentence_before_target: int | None = None
    fallback_after_target: int | None = None
    min_chars = max(400, int(target_chars * 0.65))
    for index in range(start, len(units)):
        text = str(units[index]["text"])
        next_total = total + len(text) + (1 if total else 0)
        if next_total > max_chars and index > start:
            return best_sentence_after_min or best_sentence_before_target or fallback_after_target or index
        total = next_total
        end = index + 1
        if total >= target_chars and fallback_after_target is None:
            fallback_after_target = end
        if _ends_sentence(text):
            if total <= target_chars:
                best_sentence_before_target = end
            if total >= min_chars:
                best_sentence_after_min = end
                if total >= target_chars:
                    return end
    return len(units)


def _overlap_start(units: list[dict[str, Any]], primary_start: int, *, overlap_chars: int) -> int:
    if primary_start <= 0 or overlap_chars <= 0:
        return primary_start
    total = 0
    index = primary_start
    while index > 0:
        candidate = str(units[index - 1]["text"])
        if total and total + len(candidate) + 1 > overlap_chars:
            break
        total += len(candidate) + (1 if total else 0)
        index -= 1
    return index


def build_legacy_chunked_from_raw(
    raw_doc: dict[str, Any],
    *,
    episode: int,
    episode_url: str,
    target_chars: int = 1800,
    max_chars: int = 2400,
    overlap_chars: int = 250,
) -> list[dict[str, Any]]:
    """Build sentence-aware BEMA_az-style chunked JSON from raw Whisper segments."""
    units = [
        {
            "text": text,
            "start": float(segment.get("start") or 0.0),
            "word_start": (float((segment.get("words") or [])[0].get("start") or 0.0) if (segment.get("words") or []) else None),
        }
        for segment in (raw_doc.get("segments") or [])
        if (text := _clean_text(segment.get("text")))
    ]
    chunks: list[dict[str, Any]] = []
    primary_start = 0
    while primary_start < len(units):
        primary_end = _choose_chunk_end(units, primary_start, target_chars=target_chars, max_chars=max_chars)
        overlap_start = _overlap_start(units, primary_start, overlap_chars=overlap_chars)
        chunk_units = units[overlap_start:primary_end]
        word_start = units[primary_start].get("word_start")
        anchor_seconds = word_start if word_start is not None else units[primary_start]["start"]
        primary_start_seconds = int(float(anchor_seconds))
        chunks.append(
            {
                "text": " ".join(str(unit["text"]) for unit in chunk_units),
                "metadata": {
                    "start": format_mmss(float(anchor_seconds)),
                    "episode": episode,
                    "url": f" {episode_url}?t={primary_start_seconds} ",
                    "overlap_chars": 0
                    if overlap_start == primary_start
                    else sum(len(str(unit["text"])) + 1 for unit in units[overlap_start:primary_start]),
                },
            }
        )
        primary_start = primary_end
    return chunks


def rewrite_chunked_from_raw(
    raw_root: Path,
    state: dict[str, Any],
    *,
    target_chars: int = 1800,
    max_chars: int = 2400,
    overlap_chars: int = 250,
    start: int | None = None,
    end: int | None = None,
) -> int:
    by_episode = state_by_episode(state)
    rewritten = 0
    for raw_path in sorted(raw_root.glob("e*-oai-raw.json"), key=raw_episode_number):
        number = raw_episode_number(raw_path)
        if start is not None and number < start:
            continue
        if end is not None and number > end:
            continue
        episode = by_episode.get(str(number), (None, {}))[1]
        episode_url = episode.get("episode_url") or f"https://www.bemadiscipleship.com/{number}"
        raw_doc = raw_payload(raw_path)
        if not raw_doc:
            continue
        chunked = build_legacy_chunked_from_raw(
            raw_doc,
            episode=number,
            episode_url=episode_url,
            target_chars=target_chars,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
        )
        chunked_path = raw_path.with_name(raw_path.name.replace("-oai-raw.json", "-oai-raw-chunked.json"))
        write_json(chunked_path, chunked)
        rewritten += 1
    return rewritten


def source_exists(session, title: str) -> str | None:  # type: ignore[no-untyped-def]
    return session.execute(select(Source.id).where(Source.title == title)).scalar_one_or_none()


def bema_source_ids(session) -> list[str]:  # type: ignore[no-untyped-def]
    return [
        row[0] for row in session.execute(select(Source.id).where(Source.metadata_json["source_tree_slug"].as_string() == "bema")).all()
    ]


def delete_source_ids(source_ids: list[str]) -> int:
    if not source_ids:
        return 0
    with SessionLocal() as session:
        session.execute(delete(Embedding).where(Embedding.source_id.in_(source_ids)))
        session.execute(delete(SourceEntity).where(SourceEntity.source_id.in_(source_ids)))
        session.execute(delete(Chunk).where(Chunk.source_id.in_(source_ids)))
        session.execute(delete(TranscriptSegment).where(TranscriptSegment.source_id.in_(source_ids)))
        session.execute(delete(IngestionJob).where(IngestionJob.source_id.in_(source_ids)))
        session.execute(delete(Source).where(Source.id.in_(source_ids)))
        session.commit()
        return len(source_ids)


def delete_bema_sources() -> int:
    with SessionLocal() as session:
        source_ids = bema_source_ids(session)
    return delete_source_ids(source_ids)


def import_payload(
    title: str, episode_url: str, segments: list[dict[str, Any]], metadata: dict[str, Any], entities: list[dict[str, Any]] | None = None
) -> str:
    with SessionLocal() as session:
        existing = source_exists(session, title)
        if existing:
            return existing
        source = add_transcript_source(
            session,
            payload={
                "show_title": "The BEMA Podcast",
                "episode_title": title,
                "episode_url": episode_url,
                "segments": segments,
                "entities": entities or [],
            },
        )
        source.metadata_json = {**(source.metadata_json or {}), **metadata}
        session.add(source)
        for chunk in session.query(Chunk).filter(Chunk.source_id == source.id).all():
            chunk.metadata_json = {**(chunk.metadata_json or {}), **metadata}
        for segment in session.query(TranscriptSegment).filter(TranscriptSegment.source_id == source.id).all():
            segment.metadata_json = {**(segment.metadata_json or {}), **metadata}
        session.commit()
        return source.id


def import_existing_normalized(artifact_items: Path) -> list[dict[str, Any]]:
    imported: list[dict[str, Any]] = []
    for source_json in sorted(artifact_items.glob("*/source.json")):
        item_dir = source_json.parent
        source_doc = load_json(source_json)
        if source_doc.get("source_tree_slug") != "bema" or source_doc.get("transcript_provenance") != "published_transcript":
            continue
        normalized_path = item_dir / "transcript.normalized.json"
        if not normalized_path.exists():
            continue
        normalized = load_json(normalized_path)
        segments = normalized.get("segments", []) if isinstance(normalized, dict) else []
        if not segments:
            continue
        metadata = {
            "source_tree_slug": "bema",
            "source_item_id": source_doc.get("item_id") or item_dir.name,
            "transcript_provenance": "published_transcript",
            "version_label": source_doc.get("version_label"),
            "preference_label": source_doc.get("preference_label"),
            "retrieval_weight": source_doc.get("retrieval_weight"),
            "artifact_uri": str(item_dir),
        }
        source_id = import_payload(
            source_doc["title"], source_doc.get("canonical_url") or "", segments, metadata, source_doc.get("entities") or BEMA_ENTITIES
        )
        imported.append({"title": source_doc["title"], "source_id": source_id, "kind": "existing_published"})
    return imported


def import_published_from_pages(artifact_root: Path, state: dict[str, Any], *, limit: int | None = None) -> list[dict[str, Any]]:
    by_episode = state_by_episode(state)
    item_root = artifact_root / "items"
    imported: list[dict[str, Any]] = []
    for page in sorted(item_root.glob("*/source-page.html")):
        episode_key = episode_from_item_dir(page.parent)
        if not episode_key:
            continue
        episode = by_episode.get(episode_key, (None, {}))[1]
        if not episode:
            # Keep a conservative title if RSS state is missing.
            episode = {
                "episode": episode_key,
                "episode_title": f"BEMA {episode_key}",
                "episode_url": f"https://www.bemadiscipleship.com/{episode_key}",
                "duration_seconds": 0,
            }
        links = extract_transcript_links(page.read_text(errors="ignore"))
        for transcript in links:
            version = transcript["version"]
            label = "Current" if version == "current" else "Legacy"
            episode_title = episode.get("episode_title") or f"BEMA {episode_key}"
            title = f"BEMA {episode_key}: {episode_title} ({label})"
            with SessionLocal() as session:
                if source_exists(session, title):
                    continue
            text = extract_google_doc_text(transcript["url"])
            segments = make_segments(text, int(episode.get("duration_seconds") or 0))
            item_id = f"bema-{episode_key}-{version}-{slugify(str(episode_title), max_len=70)}"
            item_dir = item_root / item_id
            source_doc = {
                "schema": "citara.source_item.v1",
                "source_tree_slug": "bema",
                "source_tree_type": "podcast",
                "item_id": item_id,
                "item_type": "podcast_episode",
                "title": title,
                "canonical_url": episode.get("episode_url") or f"https://www.bemadiscipleship.com/{episode_key}",
                "language": "en",
                "transcript_provenance": "published_transcript",
                "transcript_url": transcript["url"],
                "artifact_version": 1,
                "episode_number": episode_key,
                "episode_guid": episode.get("guid"),
                "version_label": version,
                "preference_label": version,
                "retrieval_weight": CURRENT_WEIGHT if version == "current" else LEGACY_WEIGHT,
                "duration_seconds": episode.get("duration_seconds"),
                "season": episode.get("season"),
                "entities": BEMA_ENTITIES,
            }
            write_json(item_dir / "source.json", source_doc)
            (item_dir / "transcript.source.txt").write_text(text + "\n")
            (item_dir / "transcript.txt").write_text(text + "\n")
            write_json(
                item_dir / "transcript.normalized.json",
                {
                    "schema": "citara.transcript.normalized.v1",
                    "language": "en",
                    "segments": [dict(segment_index=i, **s) for i, s in enumerate(segments)],
                },
            )
            write_json(item_dir / "transcript.raw.json", {"source": "google_doc", "url": transcript["url"], "text": text})
            write_json(
                item_dir / "import-payload.json",
                {
                    "show_title": "The BEMA Podcast",
                    "episode_title": title,
                    "episode_url": source_doc["canonical_url"],
                    "segments": segments,
                    "entities": BEMA_ENTITIES,
                },
            )
            metadata = {
                "source_tree_slug": "bema",
                "source_item_id": item_id,
                "episode_number": episode_key,
                "episode_guid": episode.get("guid"),
                "season": episode.get("season"),
                "transcript_provenance": "published_transcript",
                "version_label": version,
                "preference_label": version,
                "retrieval_weight": source_doc["retrieval_weight"],
                "transcript_url": transcript["url"],
                "artifact_uri": str(item_dir),
            }
            source_id = import_payload(title, source_doc["canonical_url"], segments, metadata, BEMA_ENTITIES)
            imported.append({"title": title, "source_id": source_id, "kind": "published", "segments": len(segments)})
            if limit is not None and len(imported) >= limit:
                return imported
    return imported


def current_published_episode_numbers() -> set[int]:
    current: set[int] = set()
    with SessionLocal() as session:
        rows = session.execute(
            select(Source.title, Source.metadata_json).where(Source.metadata_json["source_tree_slug"].as_string() == "bema")
        ).all()
        for title, metadata in rows:
            metadata = metadata or {}
            if metadata.get("transcript_provenance") != "published_transcript" or metadata.get("preference_label") != "current":
                continue
            match = re.match(r"BEMA\s+(-?\d+):", title or "")
            if match:
                current.add(int(match.group(1)))
    return current


def import_generated_openai(
    artifact_root: Path,
    state: dict[str, Any],
    raw_root: Path,
    *,
    limit: int | None = None,
    replace_existing: bool = False,
    start: int | None = None,
    end: int | None = None,
) -> list[dict[str, Any]]:
    by_episode = state_by_episode(state)
    current_published = current_published_episode_numbers()
    imported: list[dict[str, Any]] = []
    for chunked_path in sorted(raw_root.glob("e*-oai-raw-chunked.json"), key=raw_episode_number):
        number = raw_episode_number(chunked_path)
        if start is not None and number < start:
            continue
        if end is not None and number > end:
            continue
        if number in current_published:
            continue
        episode_key = str(number)
        episode = by_episode.get(episode_key, (None, {}))[1]
        if not episode:
            continue
        title = f"BEMA {episode_key}: {episode.get('episode_title') or episode.get('title') or episode_key} (Generated Transcript)"
        existing_id = None
        with SessionLocal() as session:
            existing_id = source_exists(session, title)
            if existing_id and not replace_existing:
                continue
        if existing_id and replace_existing:
            delete_source_ids([existing_id])
        segments = segments_from_chunked(chunked_path)
        if not segments:
            continue
        item_id = f"bema-{number:03d}-generated-openai" if number >= 0 else "bema--1-generated-openai"
        item_dir = artifact_root / "items" / item_id
        raw_doc = raw_payload(raw_root / f"e{999 if number == -1 else number:03d}-oai-raw.json")
        source_doc = {
            "schema": "citara.source_item.v1",
            "source_tree_slug": "bema",
            "source_tree_type": "podcast",
            "item_id": item_id,
            "item_type": "podcast_episode",
            "title": title,
            "canonical_url": episode.get("episode_url") or f"https://www.bemadiscipleship.com/{episode_key}",
            "language": "en",
            "transcript_provenance": "generated_openai_whisper",
            "original_path": str(chunked_path),
            "artifact_version": 1,
            "episode_number": number,
            "episode_guid": episode.get("guid"),
            "version_label": "generated",
            "preference_label": "generated",
            "retrieval_weight": 0.9,
            "duration_seconds": episode.get("duration_seconds"),
            "season": episode.get("season"),
            "entities": BEMA_ENTITIES,
        }
        write_json(item_dir / "source.json", source_doc)
        if raw_doc is not None:
            write_json(item_dir / "transcript.raw.json", raw_doc)
        write_json(
            item_dir / "transcript.normalized.json",
            {
                "schema": "citara.transcript.normalized.v1",
                "language": "en",
                "segments": [dict(segment_index=i, **s) for i, s in enumerate(segments)],
            },
        )
        (item_dir / "transcript.txt").write_text("\n".join(s["text"] for s in segments) + "\n")
        write_json(
            item_dir / "import-payload.json",
            {
                "show_title": "The BEMA Podcast",
                "episode_title": title,
                "episode_url": source_doc["canonical_url"],
                "segments": segments,
                "entities": BEMA_ENTITIES,
            },
        )
        metadata = {
            "source_tree_slug": "bema",
            "source_item_id": item_id,
            "episode_number": number,
            "episode_guid": episode.get("guid"),
            "season": episode.get("season"),
            "transcript_provenance": "generated_openai_whisper",
            "version_label": "generated",
            "preference_label": "generated",
            "retrieval_weight": 0.9,
            "artifact_uri": str(item_dir),
            "raw_transcript_path": str(chunked_path),
        }
        source_id = import_payload(title, source_doc["canonical_url"], segments, metadata, BEMA_ENTITIES)
        imported.append({"title": title, "source_id": source_id, "kind": "generated_openai", "segments": len(segments)})
        if limit is not None and len(imported) >= limit:
            return imported
    return imported


def stats() -> dict[str, Any]:
    with SessionLocal() as session:
        rows = session.execute(select(Source.metadata_json).where(Source.metadata_json["source_tree_slug"].as_string() == "bema")).all()
        by_prov: dict[str, int] = {}
        by_pref: dict[str, int] = {}
        for (metadata,) in rows:
            metadata = metadata or {}
            by_prov[str(metadata.get("transcript_provenance"))] = by_prov.get(str(metadata.get("transcript_provenance")), 0) + 1
            by_pref[str(metadata.get("preference_label"))] = by_pref.get(str(metadata.get("preference_label")), 0) + 1
        source_count = sum(by_prov.values())
        chunk_count = session.execute(
            select(Chunk.id)
            .join(Source, Chunk.source_id == Source.id)
            .where(Source.metadata_json["source_tree_slug"].as_string() == "bema")
        ).all()
    return {"bema_sources": source_count, "bema_chunks": len(chunk_count), "by_provenance": by_prov, "by_preference": by_pref}


def main() -> None:
    parser = argparse.ArgumentParser(description="Import/rebuild BEMA transcript artifacts into Citara DB")
    parser.add_argument("--citara-root", type=Path, default=DEFAULT_CITARA_ROOT)
    parser.add_argument("--openai-raw", type=Path, default=DEFAULT_OPENAI_RAW)
    parser.add_argument("--rebuild-bema", action="store_true", help="Delete existing BEMA rows from DB before import")
    parser.add_argument("--skip-published-pages", action="store_true")
    parser.add_argument("--skip-generated-openai", action="store_true")
    parser.add_argument("--limit-published", type=int)
    parser.add_argument("--limit-generated", type=int)
    parser.add_argument(
        "--replace-generated-openai", action="store_true", help="Delete and re-import generated OpenAI/Whisper rows that already exist"
    )
    parser.add_argument(
        "--rewrite-openai-chunked",
        action="store_true",
        help="Regenerate BEMA_az-style *-oai-raw-chunked.json files from *-oai-raw.json before import",
    )
    parser.add_argument("--rewrite-start", type=int, help="First episode to rewrite when --rewrite-openai-chunked is set")
    parser.add_argument("--rewrite-end", type=int, help="Last episode to rewrite when --rewrite-openai-chunked is set")
    parser.add_argument("--chunk-target-chars", type=int, default=1800, help="Target characters per generated chunked artifact entry")
    parser.add_argument(
        "--chunk-max-chars", type=int, default=2400, help="Hard-ish maximum characters per generated chunked artifact entry"
    )
    parser.add_argument(
        "--chunk-overlap-chars", type=int, default=250, help="Approximate overlap characters from the previous generated chunk"
    )
    args = parser.parse_args()

    os.environ.setdefault("DATABASE_URL", f"sqlite:///{args.citara_root / 'citara.db'}")
    os.environ.setdefault("SOURCE_ARTIFACT_ROOT", str(args.citara_root / "source-artifacts"))
    os.environ.setdefault("SOURCE_STATE_ROOT", str(args.citara_root / "import-state"))
    os.environ.setdefault("OBJECT_STORE_PATH", str(args.citara_root / "object-store"))

    db_path = args.citara_root / "citara.db"
    if db_path.exists():
        backup = db_path.with_suffix(f".backup-before-bema-rebuild-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.db")
        shutil.copy2(db_path, backup)
        print(f"backup={backup}")
    init_db()
    deleted = delete_bema_sources() if args.rebuild_bema else 0
    if deleted:
        print(f"deleted_bema_sources={deleted}")

    artifact_root = args.citara_root / "source-artifacts" / "bema"
    state = load_state(args.citara_root / "import-state" / "bema_pipeline_state.json")
    if args.rewrite_openai_chunked:
        rewritten = rewrite_chunked_from_raw(
            args.openai_raw,
            state,
            target_chars=args.chunk_target_chars,
            max_chars=args.chunk_max_chars,
            overlap_chars=args.chunk_overlap_chars,
            start=args.rewrite_start,
            end=args.rewrite_end,
        )
        print(f"openai_chunked_rewritten={rewritten}")
    existing = import_existing_normalized(artifact_root / "items")
    print(f"existing_published_imported_or_present={len(existing)}")
    published: list[dict[str, Any]] = []
    if not args.skip_published_pages:
        published = import_published_from_pages(artifact_root, state, limit=args.limit_published)
        print(f"published_from_pages_imported={len(published)}")
    generated: list[dict[str, Any]] = []
    if not args.skip_generated_openai:
        generated = import_generated_openai(
            artifact_root,
            state,
            args.openai_raw,
            limit=args.limit_generated,
            replace_existing=args.replace_generated_openai,
            start=args.rewrite_start if args.rewrite_openai_chunked else None,
            end=args.rewrite_end if args.rewrite_openai_chunked else None,
        )
        print(f"generated_openai_imported={len(generated)}")
    print(
        json.dumps({"stats": stats(), "sample_published": published[:3], "sample_generated": generated[:3]}, indent=2, ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
