#!/usr/bin/env python3
"""Safely import manifest-approved remote podcast transcript artifacts.

This importer is intentionally corpus-specific at the metadata boundary and generic at
its artifact boundary. It only considers files explicitly mapped by the supplied queue
manifest, never replaces sources, and binds SQLite directly to ``--citara-root/citara.db``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from citara.core.entities import attach_source_entities
from citara.core.ingestion.transcript import add_transcript_source
from citara.core.models import Base, Chunk, Source, TranscriptSegment

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from artifact_paths import data_root

DEFAULT_CITARA_ROOT = data_root()
CORPUS_SLUG = "a-book-like-no-other"
SHOW_TITLE = "A Book Like No Other"
SOURCE_TREE_TYPE = "podcast"
ITEM_TYPE = "podcast_episode"
FEED_URL = "https://rss.buzzsprout.com/2113502.rss"
APPLE_PODCAST_ID = "1667348746"
PUBLISHER = "Aleph Beta"
PROVIDER = "buzzsprout"
ACRONYM: str | None = "BLNO"
ALIASES = ["BLNO"]
TAGS = [
    "aleph-beta",
    "a-book-like-no-other",
    "blno",
    "podcast",
    "judaism",
    "religion-and-spirituality",
    "rabbi-fohrman",
    "torah",
]
ALEPH_BETA_ENTITIES = [
    {
        "type": "organization",
        "slug": "aleph-beta",
        "label": "Aleph Beta",
        "role": "publisher",
        "provenance": "source_config",
        "aliases": ["Aleph Beta Academy"],
    },
    {
        "type": "person",
        "slug": "david-fohrman",
        "label": "Rabbi David Fohrman",
        "role": "teacher",
        "provenance": "user_requested_taxonomy",
        "aliases": ["Rabbi Fohrman", "David Fohrman", "Rabbi Foreman"],
    },
]
DEFAULT_TAGS = tuple(TAGS)
DEFAULT_ALEPH_BETA_ENTITIES = tuple(ALEPH_BETA_ENTITIES)
REQUIRED_FIELDS = {
    "queue_number",
    "episode_label",
    "guid",
    "title",
    "audio_url",
    "canonical_url",
    "duration_seconds",
    "artifact_stem",
}
SAFE_STEM = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

_engine: Engine | None = None
SessionLocal = sessionmaker(autoflush=False, autocommit=False, future=True)


class Candidate(NamedTuple):
    item: dict[str, Any]
    raw_path: Path
    chunked_path: Path
    stats_path: Path
    raw: dict[str, Any] | None
    chunked: list[dict[str, Any]] | None
    stats: dict[str, Any] | None
    complete: bool
    reason: str


def configure_citara_root(root: Path) -> None:
    """Bind directly to root/citara.db, regardless of ambient configuration."""
    global _engine

    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if _engine is not None:
        _engine.dispose()
    database_url = f"sqlite:///{root / 'citara.db'}"
    # Keep settings imported later by ingestion helpers consistent, but never use
    # the ambient value to choose this importer's engine.
    os.environ["DATABASE_URL"] = database_url
    os.environ["SOURCE_ARTIFACT_ROOT"] = str(root / "source-artifacts")
    os.environ["SOURCE_STATE_ROOT"] = str(root / "import-state")
    os.environ["OBJECT_STORE_PATH"] = str(root / "object-store")
    _engine = create_engine(database_url, future=True)
    SessionLocal.configure(bind=_engine)


def init_db() -> None:
    if _engine is None:
        raise RuntimeError("Citara root has not been configured")
    Base.metadata.create_all(bind=_engine)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load queue manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("manifest schema_version must be 1")
    corpus_slug = manifest.get("corpus_slug")
    if not isinstance(corpus_slug, str) or not SAFE_STEM.fullmatch(corpus_slug):
        raise ValueError("manifest corpus_slug must be a safe lowercase name")
    episodes = manifest.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        raise ValueError("manifest episodes must be a non-empty list")
    seen_guids: set[str] = set()
    seen_stems: set[str] = set()
    for index, item in enumerate(episodes, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"manifest episode {index} must be an object")
        missing = REQUIRED_FIELDS - item.keys()
        if missing:
            raise ValueError(f"manifest episode {index} missing fields: {sorted(missing)}")
        if item["queue_number"] != index:
            raise ValueError(f"queue_number must be stable and contiguous (expected {index})")
        stem = item["artifact_stem"]
        if not isinstance(stem, str) or not SAFE_STEM.fullmatch(stem) or not stem.startswith(f"q{index:03d}-"):
            raise ValueError(f"manifest episode {index} has invalid artifact_stem")
        guid = item["guid"]
        if not isinstance(guid, str) or not guid.strip() or guid in seen_guids or stem in seen_stems:
            raise ValueError(f"manifest episode {index} has duplicate or invalid identity")
        for field in ("episode_label", "title", "audio_url", "canonical_url"):
            if not isinstance(item[field], str) or not item[field].strip():
                raise ValueError(f"manifest episode {index} has invalid {field}")
        seen_guids.add(guid)
        seen_stems.add(stem)
    configure_corpus(manifest)
    return manifest


def configure_corpus(manifest: dict[str, Any]) -> None:
    """Apply optional corpus metadata while retaining BLNO-compatible defaults."""
    global CORPUS_SLUG, SHOW_TITLE, SOURCE_TREE_TYPE, ITEM_TYPE, FEED_URL, APPLE_PODCAST_ID
    global PUBLISHER, PROVIDER, ACRONYM, ALIASES, TAGS, ALEPH_BETA_ENTITIES

    CORPUS_SLUG = str(manifest["corpus_slug"])
    legacy_blno = CORPUS_SLUG == "a-book-like-no-other"
    SHOW_TITLE = str(manifest.get("show_title") or ("A Book Like No Other" if legacy_blno else CORPUS_SLUG))
    SOURCE_TREE_TYPE = str(manifest.get("source_tree_type") or "podcast")
    ITEM_TYPE = str(manifest.get("item_type") or ("podcast_episode" if SOURCE_TREE_TYPE == "podcast" else "source_item"))
    FEED_URL = str(manifest.get("feed_url") or ("https://rss.buzzsprout.com/2113502.rss" if legacy_blno else ""))
    APPLE_PODCAST_ID = str(manifest.get("apple_podcast_id") or ("1667348746" if legacy_blno else ""))
    PUBLISHER = str(manifest.get("publisher") or ("Aleph Beta" if legacy_blno else ""))
    PROVIDER = str(manifest.get("provider") or ("buzzsprout" if legacy_blno else ""))
    acronym = manifest.get("acronym", "BLNO" if legacy_blno else None)
    ACRONYM = str(acronym) if acronym else None
    ALIASES = [str(value) for value in manifest.get("aliases", ["BLNO"] if legacy_blno else [])]
    TAGS = [str(value) for value in manifest.get("tags", DEFAULT_TAGS if legacy_blno else [])]
    entities = manifest.get("entities", list(DEFAULT_ALEPH_BETA_ENTITIES) if legacy_blno else [])
    if not isinstance(entities, list) or not all(isinstance(value, dict) for value in entities):
        raise ValueError("manifest entities must be a list of objects")
    ALEPH_BETA_ENTITIES = entities


def _try_json(path: Path) -> Any | None:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def _validate_triplet(item: dict[str, Any], raw: Any, chunked: Any, stats: Any) -> tuple[bool, str]:
    if raw is None or chunked is None or stats is None:
        return False, "missing or invalid JSON artifact"
    if not isinstance(raw, dict) or not isinstance(raw.get("segments"), list) or not raw["segments"]:
        return False, "raw artifact has no segments"
    if not isinstance(chunked, list) or not chunked:
        return False, "chunked artifact has no sentence-aware chunks"
    if not isinstance(stats, dict) or stats.get("complete") is not True:
        return False, "transcription stats are not complete"
    expected = {
        "artifact_stem": item["artifact_stem"],
        "queue_number": item["queue_number"],
        "episode_label": item["episode_label"],
        "guid": item["guid"],
        "canonical_url": item["canonical_url"],
        "model": "medium",
        "device": "cpu",
        "compute_type": "int8",
    }
    mismatches = [key for key, value in expected.items() if stats.get(key) != value]
    if mismatches:
        return False, f"stats identity/config mismatch: {', '.join(mismatches)}"
    first_segment = raw["segments"][0]
    words = first_segment.get("words") if isinstance(first_segment, dict) else None
    if not isinstance(words, list) or not words or not isinstance(words[0].get("start"), (int, float)):
        return False, "raw artifact lacks word timestamps"
    for chunk in chunked:
        if not isinstance(chunk, dict) or not str(chunk.get("text") or "").strip():
            return False, "chunked artifact contains an empty chunk"
        metadata = chunk.get("metadata")
        if not isinstance(metadata, dict) or "start" not in metadata:
            return False, "chunked artifact lacks chunk anchors"
    return True, "complete"


def load_candidates(manifest: dict[str, Any], remote_root: Path) -> list[Candidate]:
    """Resolve only the exact artifact triplets named by the manifest."""
    candidates: list[Candidate] = []
    for item in manifest["episodes"]:
        stem = item["artifact_stem"]
        raw_path = remote_root / f"{stem}-oai-raw.json"
        chunked_path = remote_root / f"{stem}-oai-raw-chunked.json"
        stats_path = remote_root / f"{stem}-transcribe-stats.json"
        raw = _try_json(raw_path)
        chunked = _try_json(chunked_path)
        stats = _try_json(stats_path)
        complete, reason = _validate_triplet(item, raw, chunked, stats)
        candidates.append(
            Candidate(
                item=item,
                raw_path=raw_path,
                chunked_path=chunked_path,
                stats_path=stats_path,
                raw=raw if isinstance(raw, dict) else None,
                chunked=chunked if isinstance(chunked, list) else None,
                stats=stats if isinstance(stats, dict) else None,
                complete=complete,
                reason=reason,
            )
        )
    return candidates


def _start_seconds(value: Any, fallback: int) -> float:
    raw = str(value or "").strip()
    if raw.isdigit() and len(raw) >= 3:
        return float(int(raw[:-2] or "0") * 60 + int(raw[-2:]))
    return float(fallback * 120)


def _word_starts(raw: dict[str, Any]) -> list[float]:
    starts: list[float] = []
    for segment in raw.get("segments", []):
        if not isinstance(segment, dict):
            continue
        for word in segment.get("words") or []:
            start = word.get("start") if isinstance(word, dict) else None
            if isinstance(start, (int, float)):
                starts.append(float(start))
    return starts


def segments_from_candidate(candidate: Candidate) -> list[dict[str, Any]]:
    if not candidate.complete or candidate.raw is None or candidate.chunked is None:
        raise ValueError(f"cannot segment incomplete triplet: {candidate.reason}")
    word_starts = _word_starts(candidate.raw)
    segments: list[dict[str, Any]] = []
    for index, chunk in enumerate(candidate.chunked):
        source_metadata = dict(chunk.get("metadata") or {})
        timestamp_url = str(source_metadata.get("url") or "").strip()
        if timestamp_url:
            source_metadata["url"] = timestamp_url
        approximate = _start_seconds(source_metadata.get("start"), index)
        nearest = min(word_starts, key=lambda value: abs(value - approximate)) if word_starts else approximate
        # The chunker emits MMSS anchors at whole-second precision. Preserve its
        # value when no first-word timestamp plausibly corresponds to the anchor.
        start_seconds = nearest if abs(nearest - approximate) <= 1.0 else approximate
        metadata_json: dict[str, Any] = {
            "source_metadata": source_metadata,
            "source_tree_slug": CORPUS_SLUG,
            "source_tree_type": SOURCE_TREE_TYPE,
            "chunk_anchor": "first_word_timestamp",
        }
        if timestamp_url:
            metadata_json["timestamp_url"] = timestamp_url
        if "overlap_chars" in source_metadata:
            metadata_json["overlap_chars"] = source_metadata["overlap_chars"]
        segments.append(
            {
                "start_ms": round(start_seconds * 1000),
                "end_ms": None,
                "speaker": None,
                "text": " ".join(str(chunk["text"]).split()),
                "metadata_json": metadata_json,
            }
        )
    for index in range(len(segments) - 1):
        segments[index]["end_ms"] = segments[index + 1]["start_ms"]
    return segments


def stable_item_id(item: dict[str, Any]) -> str:
    safe_guid = re.sub(r"[^a-z0-9]+", "-", str(item["guid"]).lower()).strip("-")
    return f"{CORPUS_SLUG}-{safe_guid}-generated-faster-whisper"


def generated_metadata(item: dict[str, Any], stats: dict[str, Any], item_dir: Path, candidate: Candidate) -> dict[str, Any]:
    metadata = {
        "source_tree_slug": CORPUS_SLUG,
        "source_tree_type": SOURCE_TREE_TYPE,
        "show_title": SHOW_TITLE,
        "series_title": SHOW_TITLE,
        "aliases": list(ALIASES),
        "publisher": PUBLISHER,
        "publisher_slug": "aleph-beta",
        "source_item_id": stable_item_id(item),
        "queue_number": item["queue_number"],
        "episode_label": item["episode_label"],
        "guid": item["guid"],
        "episode_guid": item["guid"],
        "episode_duration_seconds": item["duration_seconds"],
        "audio_url": item["audio_url"],
        "model": stats["model"],
        "transcription_model": stats["model"],
        "transcription_device": stats["device"],
        "transcription_compute_type": stats["compute_type"],
        "transcript_provenance": "generated_faster_whisper_remote",
        "provenance": "generated_faster_whisper_remote",
        "version_label": "generated",
        "preference_label": "generated",
        "retrieval_weight": 0.9,
        "language": "en",
        "tags": list(TAGS),
        "artifact_uri": str(item_dir),
        "raw_transcript_path": str(candidate.raw_path),
        "chunked_transcript_path": str(candidate.chunked_path),
        "transcription_stats_path": str(candidate.stats_path),
    }
    if ACRONYM:
        metadata["acronym"] = ACRONYM
    if FEED_URL:
        metadata["feed_url"] = FEED_URL
    if APPLE_PODCAST_ID:
        metadata["apple_podcast_id"] = APPLE_PODCAST_ID
        metadata["apple_id"] = APPLE_PODCAST_ID
    return metadata


def source_exists(item: dict[str, Any], title: str) -> str | None:
    with SessionLocal() as session:
        sources = session.execute(select(Source)).scalars().all()
        for source in sources:
            metadata = source.metadata_json or {}
            if (
                metadata.get("source_tree_slug") == CORPUS_SLUG
                and metadata.get("episode_guid") == item["guid"]
                and metadata.get("preference_label") == "generated"
            ):
                return source.id
        for source in session.execute(select(Source).where(Source.title == title)).scalars():
            metadata = source.metadata_json or {}
            known_guid = metadata.get("episode_guid") or metadata.get("guid") or source.external_id
            if known_guid and str(known_guid) != item["guid"]:
                continue
            if source.canonical_url and source.canonical_url != item["canonical_url"]:
                continue
            return source.id
        return None


def import_payload(*, title: str, canonical_url: str, segments: list[dict[str, Any]], metadata: dict[str, Any]) -> str:
    propagated = [{**segment, "metadata_json": {**segment.get("metadata_json", {}), **metadata}} for segment in segments]
    with SessionLocal() as session:
        source = add_transcript_source(
            session,
            payload={
                "show_title": SHOW_TITLE,
                "episode_title": title,
                "episode_url": canonical_url,
                "language": "en",
                "segments": propagated,
                "entities": ALEPH_BETA_ENTITIES,
                "metadata_json": metadata,
            },
        )
        source.author = PUBLISHER
        source.provider = PROVIDER
        if episode_guid := metadata.get("episode_guid"):
            source.external_id = str(episode_guid)
        session.add(source)
        session.commit()
        return source.id


def reconcile_existing_source(source_id: str, metadata: dict[str, Any], canonical_url: str) -> None:
    """Refresh importer-managed identity/metadata without replacing existing content."""
    with SessionLocal() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise RuntimeError(f"existing source disappeared: {source_id}")
        source.author = PUBLISHER
        source.provider = PROVIDER
        source.canonical_url = canonical_url
        source.external_id = str(metadata["episode_guid"])
        source.language = "en"
        source.metadata_json = {**(source.metadata_json or {}), **metadata}
        for chunk in session.query(Chunk).filter(Chunk.source_id == source_id).all():
            chunk.metadata_json = {**(chunk.metadata_json or {}), **metadata}
        for segment in session.query(TranscriptSegment).filter(TranscriptSegment.source_id == source_id).all():
            segment.metadata_json = {**(segment.metadata_json or {}), **metadata}
        attach_source_entities(session, source_id=source_id, entities=ALEPH_BETA_ENTITIES)
        session.commit()


def write_item_artifacts(
    item_dir: Path,
    *,
    item: dict[str, Any],
    title: str,
    metadata: dict[str, Any],
    segments: list[dict[str, Any]],
) -> None:
    payload_segments = [{**segment, "metadata_json": {**segment.get("metadata_json", {}), **metadata}} for segment in segments]
    write_json_atomic(
        item_dir / "source.json",
        {
            "schema": "citara.source_item.v1",
            "source_tree_slug": CORPUS_SLUG,
            "source_tree_type": SOURCE_TREE_TYPE,
            "item_id": metadata["source_item_id"],
            "item_type": ITEM_TYPE,
            "title": title,
            "canonical_url": item["canonical_url"],
            "language": "en",
            "transcript_provenance": metadata["transcript_provenance"],
            "artifact_version": 1,
            "metadata": metadata,
            "entities": ALEPH_BETA_ENTITIES,
        },
    )
    write_json_atomic(
        item_dir / "transcript.normalized.json",
        {
            "schema": "citara.transcript.normalized.v1",
            "language": "en",
            "segments": [{"segment_index": index, **segment} for index, segment in enumerate(payload_segments)],
        },
    )
    write_text_atomic(item_dir / "transcript.txt", "\n".join(segment["text"] for segment in segments) + "\n")
    write_json_atomic(
        item_dir / "import-payload.json",
        {
            "show_title": SHOW_TITLE,
            "episode_title": title,
            "episode_url": item["canonical_url"],
            "language": "en",
            "segments": payload_segments,
            "entities": ALEPH_BETA_ENTITIES,
            "metadata_json": metadata,
        },
    )


def update_state(path: Path, item: dict[str, Any], result: dict[str, Any]) -> None:
    if path.exists():
        try:
            state = load_json(path)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"refusing to overwrite invalid state file {path}") from exc
        if not isinstance(state, dict):
            raise RuntimeError(f"refusing to overwrite non-object state file {path}")
    else:
        state = {"episodes": {}}
    episodes = state.setdefault("episodes", {})
    entry = episodes.setdefault(item["guid"], {})
    entry.update(
        {
            "queue_number": item["queue_number"],
            "episode_label": item["episode_label"],
            "artifact_stem": item["artifact_stem"],
            "generated_import_status": result["status"],
        }
    )
    if result.get("source_id"):
        entry["generated_source_id"] = result["source_id"]
    if result.get("reason"):
        entry["generated_import_reason"] = result["reason"]
    write_json_atomic(path, state)


def run_import(*, citara_root: Path, manifest_path: Path) -> list[dict[str, Any]]:
    root = citara_root.expanduser().resolve()
    configure_citara_root(root)
    init_db()
    manifest_path = manifest_path.expanduser().resolve()
    manifest = load_manifest(manifest_path)
    candidates = load_candidates(manifest, manifest_path.parent)
    artifact_root = root / "source-artifacts" / CORPUS_SLUG
    state_path = root / "import-state" / f"{CORPUS_SLUG}_pipeline_state.json"
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        item = candidate.item
        if not candidate.complete:
            result = {
                "queue_number": item["queue_number"],
                "guid": item["guid"],
                "status": "skipped_incomplete",
                "reason": candidate.reason,
            }
            results.append(result)
            update_state(state_path, item, result)
            continue
        title = f"{SHOW_TITLE}: {item['title']} (Generated Transcript)"
        existing = source_exists(item, title)
        item_dir = artifact_root / "items" / stable_item_id(item)
        assert candidate.stats is not None
        metadata = generated_metadata(item, candidate.stats, item_dir, candidate)
        segments = segments_from_candidate(candidate)
        write_item_artifacts(item_dir, item=item, title=title, metadata=metadata, segments=segments)
        if existing:
            reconcile_existing_source(existing, metadata, item["canonical_url"])
            result = {
                "queue_number": item["queue_number"],
                "guid": item["guid"],
                "source_id": existing,
                "status": "skipped_existing",
            }
        else:
            try:
                source_id = import_payload(
                    title=title,
                    canonical_url=item["canonical_url"],
                    segments=segments,
                    metadata=metadata,
                )
                result = {
                    "queue_number": item["queue_number"],
                    "guid": item["guid"],
                    "source_id": source_id,
                    "status": "imported",
                    "segments": len(segments),
                }
            except Exception as exc:
                result = {
                    "queue_number": item["queue_number"],
                    "guid": item["guid"],
                    "status": "error",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
        results.append(result)
        update_state(state_path, item, result)
    return results


def backup_database(root: Path) -> Path | None:
    database = root / "citara.db"
    if not database.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    backup = root / f"citara.backup-before-podcast-artifact-import-{stamp}.db"
    # SQLite's online backup API includes committed WAL pages and produces a
    # transactionally consistent snapshot even while another reader is open.
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description="Import manifest-approved remote podcast transcript artifacts")
    parser.add_argument("--citara-root", type=Path, default=DEFAULT_CITARA_ROOT)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()

    root = args.citara_root.expanduser().resolve()
    manifest = args.manifest or root / "source-artifacts" / CORPUS_SLUG / "remote-openai" / "approved-queue.json"
    backup = backup_database(root)
    if backup:
        print(f"backup={backup}", flush=True)
    results = run_import(citara_root=root, manifest_path=manifest)
    counts = {
        status: sum(result["status"] == status for result in results)
        for status in ("imported", "skipped_existing", "skipped_incomplete", "error")
    }
    print(json.dumps({**counts, "results": results}, indent=2, ensure_ascii=False), flush=True)
    if counts["error"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
