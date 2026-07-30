#!/usr/bin/env python3
"""Import generated Text in Us faster-whisper artifacts into Citara."""

from __future__ import annotations

import argparse
import email.utils
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

DEFAULT_CITARA_ROOT = Path("../citara-data")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{DEFAULT_CITARA_ROOT / 'citara.db'}")
os.environ.setdefault("SOURCE_ARTIFACT_ROOT", str(DEFAULT_CITARA_ROOT / "source-artifacts"))
os.environ.setdefault("SOURCE_STATE_ROOT", str(DEFAULT_CITARA_ROOT / "import-state"))
os.environ.setdefault("OBJECT_STORE_PATH", str(DEFAULT_CITARA_ROOT / "object-store"))

from citara.core.db import SessionLocal, init_db
from citara.core.ingestion.transcript import add_transcript_source
from citara.core.models import Chunk, Embedding, IngestionJob, Source, SourceEntity, TranscriptSegment
from citara.core.source_taxonomy import TEXTINUS_ENTITIES, TEXTINUS_SOURCE_TREE_SLUG


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def raw_episode_number(path: Path) -> int:
    match = re.match(r"e(\d+)-oai-raw(?:-chunked)?\.json$", path.name)
    if not match:
        raise ValueError(path.name)
    return int(match.group(1))


def state_by_number(state: dict[str, Any]) -> dict[int, tuple[str, dict[str, Any]]]:
    out: dict[int, tuple[str, dict[str, Any]]] = {}
    rows: list[tuple[str, dict[str, Any]]] = [(guid, dict(ep)) for guid, ep in state.get("episodes", {}).items()]

    def sort_key(row: tuple[str, dict[str, Any]]) -> tuple[str, str]:
        guid, episode = row
        try:
            parsed = email.utils.parsedate_to_datetime(str(episode.get("published") or ""))
            return (parsed.isoformat(), guid)
        except Exception:
            return ("9999-12-31T23:59:59+00:00", guid)

    for idx, (guid, episode) in enumerate(sorted(rows, key=sort_key), start=1):
        number = int(episode.get("episode_number") or idx)
        out[number] = (guid, episode)
    return out


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
        else:
            start_ms = index * 120_000
        metadata = {"source_metadata": meta, "source_tree_slug": TEXTINUS_SOURCE_TREE_SLUG, "source_tree_type": "podcast"}
        if "overlap_chars" in meta:
            metadata["overlap_chars"] = meta["overlap_chars"]
        segments.append({"start_ms": start_ms, "end_ms": None, "speaker": None, "text": text, "metadata_json": metadata})
    for i in range(len(segments) - 1):
        segments[i]["end_ms"] = segments[i + 1]["start_ms"]
    return segments


def source_exists(session, title: str) -> str | None:  # type: ignore[no-untyped-def]
    return session.execute(select(Source.id).where(Source.title == title)).scalar_one_or_none()


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


def import_payload(title: str, episode_url: str, segments: list[dict[str, Any]], metadata: dict[str, Any]) -> str:
    with SessionLocal() as session:
        existing = source_exists(session, title)
        if existing:
            return existing
        source = add_transcript_source(
            session,
            payload={
                "show_title": "Text in Us",
                "episode_title": title,
                "episode_url": episode_url,
                "language": "en",
                "segments": segments,
                "entities": TEXTINUS_ENTITIES,
                "metadata_json": metadata,
            },
        )
        # add_transcript_source commits; patch chunks/segments with final metadata.
        source.metadata_json = {**(source.metadata_json or {}), **metadata}
        session.add(source)
        for chunk in session.query(Chunk).filter(Chunk.source_id == source.id).all():
            chunk.metadata_json = {**(chunk.metadata_json or {}), **metadata}
        for segment in session.query(TranscriptSegment).filter(TranscriptSegment.source_id == source.id).all():
            segment.metadata_json = {**(segment.metadata_json or {}), **metadata}
        session.commit()
        return source.id


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Text in Us generated transcripts")
    parser.add_argument("--citara-root", type=Path, default=DEFAULT_CITARA_ROOT)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args()
    raw_root = args.raw_root or args.citara_root / "source-artifacts" / "textinus" / "remote-openai"

    os.environ["DATABASE_URL"] = f"sqlite:///{args.citara_root / 'citara.db'}"
    os.environ["SOURCE_ARTIFACT_ROOT"] = str(args.citara_root / "source-artifacts")
    os.environ["SOURCE_STATE_ROOT"] = str(args.citara_root / "import-state")
    os.environ["OBJECT_STORE_PATH"] = str(args.citara_root / "object-store")

    db_path = args.citara_root / "citara.db"
    if db_path.exists():
        backup = db_path.with_suffix(f".backup-before-textinus-import-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.db")
        shutil.copy2(db_path, backup)
        print(f"backup={backup}")
    init_db()

    state = load_json(args.citara_root / "import-state" / "textinus_pipeline_state.json")
    by_number = state_by_number(state)
    imported: list[dict[str, Any]] = []
    for chunked_path in sorted(raw_root.glob("e*-oai-raw-chunked.json"), key=raw_episode_number):
        number = raw_episode_number(chunked_path)
        if args.start is not None and number < args.start:
            continue
        if args.end is not None and number > args.end:
            continue
        guid, episode = by_number.get(number, (None, {}))
        if not episode:
            continue
        title = f"Text in Us: {episode.get('title') or number} (Generated Transcript)"
        with SessionLocal() as session:
            existing_id = source_exists(session, title)
        if existing_id and args.replace_existing:
            delete_source_ids([existing_id])
        elif existing_id:
            imported.append({"episode": number, "title": title, "source_id": existing_id, "status": "skipped_existing"})
            continue
        segments = segments_from_chunked(chunked_path)
        if not segments:
            continue
        item_id = f"textinus-{number:03d}-generated-faster-whisper"
        metadata = {
            "source_tree_slug": TEXTINUS_SOURCE_TREE_SLUG,
            "source_tree_type": "podcast",
            "source_item_id": item_id,
            "episode_number": number,
            "episode_guid": guid,
            "transcript_provenance": "generated_faster_whisper",
            "version_label": "generated",
            "preference_label": "generated",
            "retrieval_weight": 0.9,
            "raw_transcript_path": str(chunked_path),
        }
        source_id = import_payload(title, episode.get("episode_url") or "", segments, metadata)
        imported.append({"episode": number, "title": title, "source_id": source_id, "status": "imported", "segments": len(segments)})
        if args.limit is not None and len([row for row in imported if row.get("status") == "imported"]) >= args.limit:
            break
    print(json.dumps({"imported_or_present": len(imported), "sample": imported[:5]}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
