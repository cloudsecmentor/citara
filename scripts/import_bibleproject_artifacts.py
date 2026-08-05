#!/usr/bin/env python3
"""Import published and generated BibleProject transcript artifacts into Citara."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from artifact_paths import data_root

DEFAULT_CITARA_ROOT = data_root()

from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from citara.connectors.podcasts.bibleproject import clean_transcript_text, extract_pdf_text, make_segments
from citara.core.ingestion.transcript import add_transcript_source
from citara.core.models import Base, Chunk, Embedding, IngestionJob, Source, SourceEntity, TranscriptSegment

BIBLEPROJECT_SOURCE_TREE_SLUG = "bibleproject"
BIBLEPROJECT_ENTITIES = [
    {
        "type": "organization",
        "slug": "bibleproject",
        "label": "BibleProject",
        "role": "publisher",
        "provenance": "source_config",
        "aliases": ["Bible Project", "The Bible Project"],
    }
]

_engine: Engine | None = None
SessionLocal = sessionmaker(autoflush=False, autocommit=False, future=True)


def configure_citara_root(root: Path) -> None:
    """Bind importer sessions and storage settings to the requested Citara root."""
    global _engine

    root = root.expanduser().resolve()
    db_path = root / "citara.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["SOURCE_ARTIFACT_ROOT"] = str(root / "source-artifacts")
    os.environ["SOURCE_STATE_ROOT"] = str(root / "import-state")
    os.environ["OBJECT_STORE_PATH"] = str(root / "object-store")
    _engine = create_engine(f"sqlite:///{db_path}", future=True)
    SessionLocal.configure(bind=_engine)


def init_db() -> None:
    if _engine is None:
        raise RuntimeError("Citara root has not been configured")
    Base.metadata.create_all(bind=_engine)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def state_by_number(state: dict[str, Any]) -> dict[int, tuple[str, dict[str, Any]]]:
    by_number: dict[int, tuple[str, dict[str, Any]]] = {}
    for guid, episode in state.get("episodes", {}).items():
        number = episode.get("episode")
        if isinstance(number, int):
            by_number[number] = (guid, dict(episode))
    return by_number


def raw_episode_number(path: Path) -> int:
    match = re.match(r"e(\d+)-oai-raw(?:-chunked)?\.json$", path.name)
    if not match:
        raise ValueError(path.name)
    return int(match.group(1))


def _start_ms(value: Any, fallback_index: int) -> int:
    raw = str(value or "").strip()
    if raw.isdigit() and len(raw) >= 3:
        return (int(raw[:-2] or "0") * 60 + int(raw[-2:])) * 1000
    return fallback_index * 120_000


def segments_from_chunked(path: Path) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for index, item in enumerate(load_json(path)):
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue
        source_metadata = dict(item.get("metadata") or {})
        timestamp_url = str(source_metadata.get("url") or "").strip()
        if timestamp_url:
            source_metadata["url"] = timestamp_url
        metadata: dict[str, Any] = {
            "source_metadata": source_metadata,
            "source_tree_slug": BIBLEPROJECT_SOURCE_TREE_SLUG,
            "source_tree_type": "podcast",
        }
        if timestamp_url:
            metadata["timestamp_url"] = timestamp_url
        if "overlap_chars" in source_metadata:
            metadata["overlap_chars"] = source_metadata["overlap_chars"]
        segments.append(
            {
                "start_ms": _start_ms(source_metadata.get("start"), index),
                "end_ms": None,
                "speaker": None,
                "text": text,
                "metadata_json": metadata,
            }
        )
    for index in range(len(segments) - 1):
        segments[index]["end_ms"] = segments[index + 1]["start_ms"]
    return segments


def generated_metadata(*, number: int, guid: str, episode: dict[str, Any], item_dir: Path, chunked_path: Path) -> dict[str, Any]:
    return {
        "source_tree_slug": BIBLEPROJECT_SOURCE_TREE_SLUG,
        "source_tree_type": "podcast",
        "source_item_id": f"bibleproject-{number:03d}-generated-faster-whisper",
        "episode_number": number,
        "episode_guid": guid,
        "episode_duration_seconds": episode.get("duration_seconds"),
        "audio_url": episode.get("audio_url"),
        "transcript_provenance": "generated_faster_whisper",
        "transcription_model": episode.get("model") or "medium",
        "version_label": "generated",
        "preference_label": "generated",
        "retrieval_weight": 0.9,
        "language": "en",
        "artifact_uri": str(item_dir),
        "raw_transcript_path": str(chunked_path.with_name(chunked_path.name.replace("-chunked", ""))),
        "chunked_transcript_path": str(chunked_path),
    }


def published_metadata(*, number: int, guid: str, episode: dict[str, Any], item_dir: Path) -> dict[str, Any]:
    return {
        "source_tree_slug": BIBLEPROJECT_SOURCE_TREE_SLUG,
        "source_tree_type": "podcast",
        "source_item_id": f"bibleproject-{number:03d}-published-transcript",
        "episode_number": number,
        "episode_guid": guid,
        "episode_duration_seconds": episode.get("duration_seconds"),
        "transcript_url": episode.get("transcript_url"),
        "transcript_provenance": "published_transcript_pdf",
        "version_label": "published",
        "preference_label": "published",
        "retrieval_weight": 1.0,
        "language": "en",
        "artifact_uri": str(item_dir),
    }


def merge_source_metadata(existing: dict[str, Any] | None, imported: dict[str, Any]) -> dict[str, Any]:
    return {**(existing or {}), **imported}


def source_exists(title: str) -> str | None:
    with SessionLocal() as session:
        return session.execute(select(Source.id).where(Source.title == title)).scalar_one_or_none()


def patch_existing_source(source_id: str, metadata: dict[str, Any], canonical_url: str) -> None:
    with SessionLocal() as session:
        source = session.get(Source, source_id)
        if source is None:
            return
        source.language = "en"
        if canonical_url:
            source.canonical_url = canonical_url
        source.metadata_json = merge_source_metadata(source.metadata_json, metadata)
        for chunk in session.query(Chunk).filter(Chunk.source_id == source_id).all():
            chunk.metadata_json = merge_source_metadata(chunk.metadata_json, metadata)
        for segment in session.query(TranscriptSegment).filter(TranscriptSegment.source_id == source_id).all():
            segment.metadata_json = merge_source_metadata(segment.metadata_json, metadata)
        session.commit()


def _delete_source_rows(session: Any, source_id: str) -> None:
    session.execute(delete(Embedding).where(Embedding.source_id == source_id))
    session.execute(delete(SourceEntity).where(SourceEntity.source_id == source_id))
    session.execute(delete(Chunk).where(Chunk.source_id == source_id))
    session.execute(delete(TranscriptSegment).where(TranscriptSegment.source_id == source_id))
    session.execute(delete(IngestionJob).where(IngestionJob.source_id == source_id))
    session.execute(delete(Source).where(Source.id == source_id))


def delete_source(source_id: str) -> None:
    with SessionLocal() as session:
        _delete_source_rows(session, source_id)
        session.commit()


def swap_replacement_source(existing_id: str, replacement_id: str, title: str) -> None:
    """Atomically remove the old source and give its prepared replacement the final title."""
    with SessionLocal() as session:
        replacement = session.get(Source, replacement_id)
        if replacement is None:
            raise RuntimeError(f"prepared replacement source disappeared: {replacement_id}")
        _delete_source_rows(session, existing_id)
        replacement.title = title
        session.commit()


def import_payload(title: str, episode_url: str, segments: list[dict[str, Any]], metadata: dict[str, Any]) -> str:
    existing = source_exists(title)
    if existing:
        return existing
    with SessionLocal() as session:
        source = add_transcript_source(
            session,
            payload={
                "show_title": "BibleProject",
                "episode_title": title,
                "episode_url": episode_url,
                "language": "en",
                "segments": segments,
                "entities": BIBLEPROJECT_ENTITIES,
                "metadata_json": metadata,
            },
        )
        source.metadata_json = {**(source.metadata_json or {}), **metadata}
        for chunk in session.query(Chunk).filter(Chunk.source_id == source.id).all():
            chunk.metadata_json = {**(chunk.metadata_json or {}), **metadata}
        for segment in session.query(TranscriptSegment).filter(TranscriptSegment.source_id == source.id).all():
            segment.metadata_json = {**(segment.metadata_json or {}), **metadata}
        session.commit()
        return source.id


def resolve_uncertain_swap(existing_id: str, replacement_id: str, title: str, preparation_title: str) -> str:
    """Resolve uncertain swap state while holding SQLite's write lock.

    Revalidation and any cleanup happen in the same transaction, preventing a
    concurrent recovery from promoting the replacement between check and delete.
    """
    with SessionLocal() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        existing = session.get(Source, existing_id)
        replacement = session.get(Source, replacement_id)
        if existing is None and replacement is not None and replacement.title == title:
            session.commit()
            return "succeeded"
        if existing is not None and replacement is not None and replacement.title == preparation_title:
            _delete_source_rows(session, replacement_id)
            session.commit()
            return "cleaned_prepared"
        session.rollback()
        return "unknown"


def replace_payload(
    existing_id: str,
    title: str,
    episode_url: str,
    segments: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> str:
    """Fully ingest a replacement before atomically swapping it for the old source."""
    preparation_title = f"{title} [prepared replacement {uuid4().hex}]"
    replacement_id = import_payload(preparation_title, episode_url, segments, metadata)
    try:
        swap_replacement_source(existing_id, replacement_id, title)
    except Exception:
        state = resolve_uncertain_swap(existing_id, replacement_id, title, preparation_title)
        if state == "succeeded":
            return replacement_id
        raise
    return replacement_id


def write_item_artifacts(
    item_dir: Path,
    *,
    title: str,
    canonical_url: str,
    provenance: str,
    metadata: dict[str, Any],
    segments: list[dict[str, Any]],
) -> None:
    item_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        item_dir / "source.json",
        {
            "schema": "citara.source_item.v1",
            "source_tree_slug": BIBLEPROJECT_SOURCE_TREE_SLUG,
            "source_tree_type": "podcast",
            "item_id": metadata["source_item_id"],
            "item_type": "podcast_episode",
            "title": title,
            "canonical_url": canonical_url,
            "language": "en",
            "transcript_provenance": provenance,
            "artifact_version": 1,
            **{k: metadata.get(k) for k in ("episode_number", "episode_guid", "preference_label", "retrieval_weight")},
            "entities": BIBLEPROJECT_ENTITIES,
        },
    )
    write_json(
        item_dir / "transcript.normalized.json",
        {
            "schema": "citara.transcript.normalized.v1",
            "language": "en",
            "segments": [dict(segment_index=i, **s) for i, s in enumerate(segments)],
        },
    )
    (item_dir / "transcript.txt").write_text("\n".join(segment["text"] for segment in segments) + "\n")
    write_json(
        item_dir / "import-payload.json",
        {
            "show_title": "BibleProject",
            "episode_title": title,
            "episode_url": canonical_url,
            "language": "en",
            "segments": segments,
            "entities": BIBLEPROJECT_ENTITIES,
            "metadata_json": metadata,
        },
    )


def apply_import_results_to_state(state: dict[str, Any], *, published: list[dict[str, Any]], generated: list[dict[str, Any]]) -> None:
    entries = state.get("episodes", {})
    by_number = {episode.get("episode"): episode for episode in entries.values()}
    for result in published:
        episode = by_number.get(result.get("episode"))
        if episode is None or result.get("status") == "error":
            continue
        episode["published_status"] = result.get("status")
        episode["source_id"] = result.get("source_id")
    for result in generated:
        episode = by_number.get(result.get("episode"))
        if episode is None or result.get("status") == "error":
            continue
        episode["generated_import_status"] = result.get("status")
        episode["generated_source_id"] = result.get("source_id")


def update_status(path: Path, **updates: Any) -> None:
    current = load_json(path) if path.exists() else {}
    current.update(updates)
    current["updated_at"] = datetime.now(UTC).isoformat()
    write_json(path, current)


def download_pdf(url: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return
    request = urllib.request.Request(url, headers={"User-Agent": "citara/0.1 (+BibleProject importer)"})
    with urllib.request.urlopen(request, timeout=180) as response:
        data = response.read()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def import_published(
    *, state: dict[str, Any], artifact_root: Path, status_path: Path, replace_existing: bool, limit: int | None
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for number, (guid, episode) in sorted(state_by_number(state).items()):
        if not episode.get("has_published_transcript"):
            continue
        title = f"BibleProject: {episode.get('title') or number} (Published Transcript)"
        existing = source_exists(title)
        if existing and not replace_existing:
            item_dir = artifact_root / "items" / f"bibleproject-{number:03d}-published-transcript"
            metadata = published_metadata(number=number, guid=guid, episode=episode, item_dir=item_dir)
            patch_existing_source(existing, metadata, str(episode.get("episode_url") or "").strip())
            results.append({"episode": number, "source_id": existing, "status": "skipped_existing"})
            continue
        try:
            item_id = f"bibleproject-{number:03d}-published-transcript"
            item_dir = artifact_root / "items" / item_id
            pdf_path = item_dir / "transcript.source.pdf"
            download_pdf(str(episode["transcript_url"]), pdf_path)
            text = clean_transcript_text(extract_pdf_text(pdf_path))
            if len(text) < 500:
                raise RuntimeError(f"published transcript too short: {len(text)} characters")
            segments = make_segments(text, int(episode.get("duration_seconds") or 0))
            metadata = published_metadata(number=number, guid=guid, episode=episode, item_dir=item_dir)
            canonical_url = str(episode.get("episode_url") or "").strip()
            write_item_artifacts(
                item_dir,
                title=title,
                canonical_url=canonical_url,
                provenance="published_transcript_pdf",
                metadata=metadata,
                segments=segments,
            )
            source_id = (
                replace_payload(existing, title, canonical_url, segments, metadata)
                if existing
                else import_payload(title, canonical_url, segments, metadata)
            )
            results.append({"episode": number, "source_id": source_id, "status": "imported", "segments": len(segments)})
        except Exception as exc:
            results.append({"episode": number, "status": "error", "error": str(exc)})
        update_status(status_path, phase="published", last_episode=number, published_processed=len(results))
        if limit is not None and sum(row["status"] == "imported" for row in results) >= limit:
            break
    return results


def import_generated(
    *,
    state: dict[str, Any],
    artifact_root: Path,
    raw_root: Path,
    status_path: Path,
    replace_existing: bool,
    start: int | None,
    end: int | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    by_number = state_by_number(state)
    paths = sorted(raw_root.glob("e*-oai-raw-chunked.json"), key=raw_episode_number)
    for chunked_path in paths:
        number = raw_episode_number(chunked_path)
        if start is not None and number < start:
            continue
        if end is not None and number > end:
            continue
        guid, episode = by_number.get(number, (None, {}))
        if not episode or episode.get("has_published_transcript"):
            continue
        title = f"BibleProject: {episode.get('title') or number} (Generated Transcript)"
        existing = source_exists(title)
        if existing and not replace_existing:
            item_dir = artifact_root / "items" / f"bibleproject-{number:03d}-generated-faster-whisper"
            metadata = generated_metadata(number=number, guid=str(guid), episode=episode, item_dir=item_dir, chunked_path=chunked_path)
            patch_existing_source(existing, metadata, str(episode.get("episode_url") or "").strip())
            results.append({"episode": number, "source_id": existing, "status": "skipped_existing"})
            continue
        try:
            item_id = f"bibleproject-{number:03d}-generated-faster-whisper"
            item_dir = artifact_root / "items" / item_id
            segments = segments_from_chunked(chunked_path)
            if not segments:
                raise RuntimeError("no generated transcript segments")
            metadata = generated_metadata(number=number, guid=str(guid), episode=episode, item_dir=item_dir, chunked_path=chunked_path)
            canonical_url = str(episode.get("episode_url") or "").strip()
            write_item_artifacts(
                item_dir,
                title=title,
                canonical_url=canonical_url,
                provenance="generated_faster_whisper",
                metadata=metadata,
                segments=segments,
            )
            source_id = (
                replace_payload(existing, title, canonical_url, segments, metadata)
                if existing
                else import_payload(title, canonical_url, segments, metadata)
            )
            results.append({"episode": number, "source_id": source_id, "status": "imported", "segments": len(segments)})
        except Exception as exc:
            results.append({"episode": number, "status": "error", "error": str(exc)})
        update_status(
            status_path,
            phase="generated",
            last_episode=number,
            generated_processed=len(results),
            generated_total=len(paths),
            errors=sum(row["status"] == "error" for row in results),
        )
        if limit is not None and sum(row["status"] == "imported" for row in results) >= limit:
            break
    return results


def db_stats() -> dict[str, Any]:
    with SessionLocal() as session:
        sources = (
            session.execute(select(Source).where(Source.metadata_json["source_tree_slug"].as_string() == BIBLEPROJECT_SOURCE_TREE_SLUG))
            .scalars()
            .all()
        )
        source_ids = [source.id for source in sources]
        chunks = session.execute(select(Chunk.id).where(Chunk.source_id.in_(source_ids))).all() if source_ids else []
    provenance: dict[str, int] = {}
    for source in sources:
        key = str((source.metadata_json or {}).get("transcript_provenance"))
        provenance[key] = provenance.get(key, 0) + 1
    return {"sources": len(sources), "chunks": len(chunks), "by_provenance": provenance}


def main() -> None:
    parser = argparse.ArgumentParser(description="Import BibleProject transcript artifacts into Citara")
    parser.add_argument("--citara-root", type=Path, default=DEFAULT_CITARA_ROOT)
    parser.add_argument("--phase", choices=["all", "published", "generated"], default="all")
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args()

    root = args.citara_root.expanduser().resolve()
    configure_citara_root(root)
    state_path = root / "import-state" / "bibleproject_pipeline_state.json"
    state = load_json(state_path)
    artifact_root = root / "source-artifacts" / "bibleproject"
    raw_root = artifact_root / "remote-openai"
    status_path = root / "import-state" / "bibleproject_import_status.json"
    db_path = root / "citara.db"
    if db_path.exists():
        backup = db_path.with_suffix(f".backup-before-bibleproject-import-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.db")
        shutil.copy2(db_path, backup)
        print(f"backup={backup}", flush=True)
    init_db()
    update_status(
        status_path,
        status="running",
        phase=args.phase,
        expected_episodes=state.get("episode_count", 0),
        generated_total=sum(not e.get("has_published_transcript") for e in state.get("episodes", {}).values()),
        published_total=sum(bool(e.get("has_published_transcript")) for e in state.get("episodes", {}).values()),
    )

    published: list[dict[str, Any]] = []
    generated: list[dict[str, Any]] = []
    try:
        if args.phase in {"all", "published"}:
            published = import_published(
                state=state, artifact_root=artifact_root, status_path=status_path, replace_existing=args.replace_existing, limit=args.limit
            )
            print(
                f"published_imported={sum(row['status'] == 'imported' for row in published)} existing={sum(row['status'] == 'skipped_existing' for row in published)} errors={sum(row['status'] == 'error' for row in published)}",
                flush=True,
            )
        if args.phase in {"all", "generated"}:
            generated = import_generated(
                state=state,
                artifact_root=artifact_root,
                raw_root=raw_root,
                status_path=status_path,
                replace_existing=args.replace_existing,
                start=args.start,
                end=args.end,
                limit=args.limit,
            )
            print(
                f"generated_imported={sum(row['status'] == 'imported' for row in generated)} existing={sum(row['status'] == 'skipped_existing' for row in generated)} errors={sum(row['status'] == 'error' for row in generated)}",
                flush=True,
            )
        apply_import_results_to_state(state, published=published, generated=generated)
        write_json(state_path, state)
        stats = db_stats()
        errors = [row for row in published + generated if row.get("status") == "error"]
        update_status(
            status_path, status="complete" if not errors else "complete_with_errors", phase=args.phase, stats=stats, errors=errors
        )
        print(json.dumps({"stats": stats, "errors": errors[:10]}, indent=2, ensure_ascii=False), flush=True)
    except BaseException as exc:
        update_status(status_path, status="interrupted", error=f"{type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    main()
