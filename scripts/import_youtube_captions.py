#!/usr/bin/env python3
"""Import manifest-approved YouTube JSON3 auto-captions into Citara."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import sessionmaker

from citara.core.backups import backup_database
from citara.core.entities import attach_source_entities
from citara.core.ingestion.transcript import add_transcript_source
from citara.core.models import Base, Chunk, Embedding, IngestionJob, Source, SourceEntity, TranscriptSegment
from citara.core.retrieval.fts import delete_source_chunks

_engine: Engine | None = None
SessionLocal = sessionmaker(autoflush=False, autocommit=False, future=True)


def configure_citara_root(root: Path) -> None:
    global _engine
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if _engine is not None:
        _engine.dispose()
    database = root / "citara.db"
    database_url = URL.create("sqlite", database=str(database))
    os.environ["DATABASE_URL"] = database_url.render_as_string(hide_password=False)
    os.environ["SOURCE_ARTIFACT_ROOT"] = str(root / "source-artifacts")
    _engine = create_engine(database_url, future=True)
    SessionLocal.configure(bind=_engine)
    Base.metadata.create_all(bind=_engine)


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


SAFE_TREE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
SAFE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def validate_item(item: dict[str, Any]) -> None:
    tree = str(item.get("source_tree_slug") or "")
    video_id = str(item.get("video_id") or "")
    if not SAFE_TREE_SLUG.fullmatch(tree):
        raise ValueError(f"unsafe source_tree_slug: {tree!r}")
    if not SAFE_VIDEO_ID.fullmatch(video_id):
        raise ValueError(f"invalid YouTube video_id: {video_id!r}")
    if item.get("guid") != f"youtube-{video_id}":
        raise ValueError(f"guid does not match video_id: {item.get('guid')!r}")
    parsed = urlsplit(str(item.get("canonical_url") or ""))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if parsed.scheme != "https" or parsed.hostname not in {"youtube.com", "www.youtube.com"} or query.get("v") != video_id:
        raise ValueError(f"canonical_url does not match video_id: {item.get('canonical_url')!r}")


def resolve_caption_path(value: str, *, manifest_path: Path, caption_root: Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = manifest_path.parent / candidate
    resolved = candidate.resolve()
    approved_root = caption_root.expanduser().resolve()
    if not resolved.is_relative_to(approved_root):
        raise ValueError(f"caption_path escapes approved caption root: {resolved}")
    return resolved


@contextmanager
def import_lock(citara_root: Path):
    lock_path = citara_root / ".youtube-caption-import.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def parse_json3_caption(payload: dict[str, Any]) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict) or not isinstance(event.get("segs"), list):
            continue
        if event.get("aAppend"):
            continue
        start_ms = event.get("tStartMs")
        duration_ms = event.get("dDurationMs")
        if not isinstance(start_ms, (int, float)) or not isinstance(duration_ms, (int, float)):
            continue
        pieces: list[str] = []
        words: list[dict[str, Any]] = []
        for segment in event["segs"]:
            if not isinstance(segment, dict):
                continue
            raw = str(segment.get("utf8") or "")
            if raw.strip() in {"", "[Music]"}:
                continue
            pieces.append(raw)
            clean = raw.strip()
            if clean:
                offset = segment.get("tOffsetMs")
                word_start = int(start_ms + (offset if isinstance(offset, (int, float)) else 0))
                words.append({"start_ms": word_start, "word": clean})
        text = " ".join("".join(pieces).split())
        if not text:
            continue
        cues.append(
            {
                "start_ms": int(start_ms),
                "end_ms": int(start_ms + duration_ms),
                "text": text,
                "words": words,
            }
        )
    for index in range(len(cues) - 1):
        cues[index]["end_ms"] = min(cues[index]["end_ms"], cues[index + 1]["start_ms"])
    return cues


def timestamp_url(canonical_url: str, start_ms: int) -> str:
    parsed = urlsplit(canonical_url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "t"]
    query.append(("t", f"{start_ms // 1000}s"))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def split_oversized_cue(cue: dict[str, Any], *, max_chars: int, max_duration_ms: int) -> list[dict[str, Any]]:
    text = str(cue["text"])
    duration = max(0, int(cue["end_ms"]) - int(cue["start_ms"]))
    parts = max(1, math.ceil(len(text) / max_chars), math.ceil(duration / max_duration_ms))
    if parts == 1:
        return [cue]
    width = math.ceil(len(text) / parts)
    output: list[dict[str, Any]] = []
    for index in range(parts):
        piece = text[index * width : (index + 1) * width].strip()
        if not piece:
            continue
        start_ms = int(cue["start_ms"] + duration * index / parts)
        end_ms = int(cue["start_ms"] + duration * (index + 1) / parts)
        output.append({"start_ms": start_ms, "end_ms": end_ms, "text": piece, "words": []})
    return output


def chunk_caption_cues(
    cues: list[dict[str, Any]],
    canonical_url: str,
    *,
    max_chars: int = 1_200,
    max_duration_ms: int = 90_000,
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        start_ms = int(current[0]["words"][0]["start_ms"] if current[0].get("words") else current[0]["start_ms"])
        chunks.append(
            {
                "start_ms": start_ms,
                "end_ms": int(current[-1]["end_ms"]),
                "speaker": None,
                "text": " ".join(cue["text"] for cue in current),
                "metadata_json": {
                    "timestamp_url": timestamp_url(canonical_url, start_ms),
                    "chunk_anchor": "first_word_timestamp",
                    "caption_provider": "youtube_auto_caption_json3",
                },
            }
        )
        current = []
        current_chars = 0

    expanded_cues = [piece for cue in cues for piece in split_oversized_cue(cue, max_chars=max_chars, max_duration_ms=max_duration_ms)]
    for cue in expanded_cues:
        projected_chars = current_chars + (1 if current else 0) + len(cue["text"])
        projected_duration = int(cue["end_ms"]) - int(current[0]["start_ms"]) if current else 0
        if current and (projected_chars > max_chars or projected_duration > max_duration_ms):
            flush()
        current.append(cue)
        current_chars += (1 if len(current) > 1 else 0) + len(cue["text"])
    flush()
    return chunks


def stable_item_id(item: dict[str, Any]) -> str:
    return f"{item['source_tree_slug']}-youtube-{item['video_id']}-youtube-auto-caption"


def source_title(item: dict[str, Any]) -> str:
    return f"Tim Mackie Archives: {item['title']} (YouTube Auto-Captions)"


def find_existing_source(item: dict[str, Any]) -> Source | None:
    guid = item["guid"]
    identity = stable_item_id(item)
    with SessionLocal() as session:
        candidates = (
            session.execute(select(Source).where((Source.external_id == guid) | (Source.canonical_url == item["canonical_url"])))
            .scalars()
            .all()
        )
        matches = []
        for candidate in candidates:
            metadata = candidate.metadata_json or {}
            if (
                metadata.get("transcript_provenance") == "youtube_auto_caption"
                and metadata.get("preference_label") == "youtube_auto_caption"
                and metadata.get("source_tree_slug") == item["source_tree_slug"]
                and metadata.get("source_item_id") == identity
                and metadata.get("guid") == guid
                and metadata.get("episode_guid") == guid
                and candidate.external_id == guid
                and candidate.canonical_url == item["canonical_url"]
            ):
                if not metadata.get("caption_replacement_of"):
                    matches.append(candidate)
        if len(matches) > 1:
            raise RuntimeError(f"multiple auto-caption sources found for {guid}: {[row.id for row in matches]}")
        if matches:
            session.expunge(matches[0])
            return matches[0]
    return None


def item_metadata(manifest: dict[str, Any], item: dict[str, Any], item_dir: Path, caption_path: Path) -> dict[str, Any]:
    return {
        "source_tree_slug": item["source_tree_slug"],
        "source_tree_type": "video_channel",
        "source_item_id": stable_item_id(item),
        "item_type": "youtube_video",
        "show_title": item["series_title"],
        "series_title": item["series_title"],
        "publisher": manifest["publisher"],
        "publisher_slug": manifest["publisher_slug"],
        "author": manifest["author"],
        "guid": item["guid"],
        "episode_guid": item["guid"],
        "video_id": item["video_id"],
        "external_ids": item.get("external_ids") or {"youtube_video_id": item["video_id"]},
        "episode_duration_seconds": item["duration_seconds"],
        "transcript_provenance": "youtube_auto_caption",
        "provenance": "youtube_auto_caption",
        "caption_kind": "asr",
        "caption_provider": "youtube_auto_caption_json3",
        "caption_normalization_version": 2,
        "version_label": "youtube_auto_caption",
        "preference_label": "youtube_auto_caption",
        "retrieval_weight": 0.8,
        "language": "en",
        "artifact_uri": str(item_dir),
        "raw_caption_path": str(caption_path),
        "channel": manifest["channel"],
        "channel_id": manifest.get("channel_id"),
        "channel_url": manifest.get("channel_url"),
        "tags": ["tim-mackie", "youtube", "bible", "auto-caption"],
    }


def write_item_artifacts(
    item_dir: Path,
    *,
    manifest: dict[str, Any],
    item: dict[str, Any],
    metadata: dict[str, Any],
    chunks: list[dict[str, Any]],
    raw_caption: dict[str, Any],
) -> None:
    item_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        item_dir / "source.json",
        {
            "schema": "citara.source_item.v1",
            "source_tree_slug": item["source_tree_slug"],
            "source_tree_type": "video_channel",
            "item_id": stable_item_id(item),
            "item_type": "youtube_video",
            "title": source_title(item),
            "canonical_url": item["canonical_url"],
            "language": "en",
            "transcript_provenance": "youtube_auto_caption",
            "artifact_version": 1,
            "metadata": metadata,
            "entities": manifest.get("entities") or [],
        },
    )
    write_json_atomic(item_dir / "transcript.raw.json", raw_caption)
    write_json_atomic(
        item_dir / "transcript.normalized.json",
        {
            "schema": "citara.transcript.normalized.v1",
            "language": "en",
            "segments": [{"segment_index": index, **chunk} for index, chunk in enumerate(chunks)],
        },
    )
    write_text_atomic(item_dir / "transcript.txt", "\n".join(chunk["text"] for chunk in chunks) + "\n")


def assert_owned_caption_source(source: Source, item: dict[str, Any], *, stale: bool | None = None, pending: bool = False) -> None:
    metadata = source.metadata_json or {}
    expected = stable_item_id(item)
    owned = (
        metadata.get("transcript_provenance") == "youtube_auto_caption"
        and metadata.get("preference_label") == "youtube_auto_caption"
        and metadata.get("source_tree_slug") == item["source_tree_slug"]
        and metadata.get("source_item_id") == expected
        and metadata.get("guid") == item["guid"]
        and metadata.get("episode_guid") == item["guid"]
        and (source.external_id == item["guid"] or (pending and source.external_id is None))
        and source.canonical_url == item["canonical_url"]
        and source.source_type == "youtube_video"
        and source.provider == "youtube"
    )
    if not owned:
        raise RuntimeError(f"refusing to mutate non-importer-owned caption source {source.id}")
    is_stale = metadata.get("caption_normalization_version") != 2
    if stale is not None and is_stale != stale:
        raise RuntimeError(f"caption source {source.id} has unexpected normalization version")


def validate_replacement(session, source: Source, chunks: list[dict[str, Any]]) -> None:
    """Do not trust the normalization label or an ingestion return alone."""
    stored = session.query(Chunk).filter_by(source_id=source.id).order_by(Chunk.chunk_index).all()
    jobs = session.query(IngestionJob).filter_by(source_id=source.id, status="succeeded").all()
    if source.status != "succeeded" or not jobs or len(stored) != len(chunks):
        raise RuntimeError(f"incomplete caption replacement {source.id}; old source retained")
    for row, expected in zip(stored, chunks, strict=True):
        segment = session.get(TranscriptSegment, row.transcript_segment_id)
        embeddings = session.query(Embedding).filter_by(source_id=source.id, chunk_id=row.id).all()
        if (
            row.text != expected["text"]
            or row.start_ms != expected["start_ms"]
            or row.end_ms != expected["end_ms"]
            or segment is None
            or segment.text != row.text
            or segment.start_ms != row.start_ms
            or segment.end_ms != row.end_ms
            or not embeddings
            or any(not embedding.vector or len(embedding.vector) != embedding.dimensions for embedding in embeddings)
        ):
            raise RuntimeError(f"incomplete caption replacement {source.id}; old source retained")


def replace_stale_caption_source(
    source_id: str,
    item: dict[str, Any],
    *,
    replacement_id: str | None = None,
    chunks: list[dict[str, Any]] | None = None,
    author: str | None = None,
) -> None:
    """Promote a validated replacement and delete the old source in ONE transaction.

    Ingestion commits internally, so it must finish before this transaction starts.
    Its durable caption_replacement_of marker lets a retry reuse a committed source
    even if ingestion raised after committing but before returning its ID.
    """
    with SessionLocal() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise RuntimeError(f"stale caption source disappeared: {source_id}")
        assert_owned_caption_source(source, item, stale=True)
        replacement = session.get(Source, replacement_id) if replacement_id else None
        if replacement is None or chunks is None:
            raise RuntimeError("validated replacement required; old source retained")
        assert_owned_caption_source(replacement, item, stale=False, pending=True)
        if replacement.metadata_json.get("caption_replacement_of") != source_id:
            raise RuntimeError("replacement does not identify this stale source")
        validate_replacement(session, replacement, chunks)
        replacement.external_id = item["guid"]
        replacement.author = author
        replacement.metadata_json = {key: value for key, value in replacement.metadata_json.items() if key != "caption_replacement_of"}
        delete_source_chunks(session, source_id)
        session.query(Embedding).filter_by(source_id=source_id).delete(synchronize_session=False)
        session.query(Chunk).filter_by(source_id=source_id).delete(synchronize_session=False)
        session.query(TranscriptSegment).filter_by(source_id=source_id).delete(synchronize_session=False)
        session.query(SourceEntity).filter_by(source_id=source_id).delete(synchronize_session=False)
        session.query(IngestionJob).filter_by(source_id=source_id).delete(synchronize_session=False)
        session.delete(source)
        session.commit()


def reconcile_existing(
    source_id: str,
    manifest: dict[str, Any],
    item: dict[str, Any],
    metadata: dict[str, Any],
    normalized_chunks: list[dict[str, Any]],
) -> None:
    with SessionLocal() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise RuntimeError(f"source disappeared: {source_id}")
        assert_owned_caption_source(source, item, stale=False)
        source.author = manifest["author"]
        source.provider = manifest["provider"]
        source.source_type = "youtube_video"
        source.external_id = item["guid"]
        source.canonical_url = item["canonical_url"]
        source.language = "en"
        source.metadata_json = {**(source.metadata_json or {}), **metadata}
        stored_chunks = session.query(Chunk).filter_by(source_id=source_id).order_by(Chunk.chunk_index).all()
        if len(stored_chunks) != len(normalized_chunks):
            raise RuntimeError(
                f"caption normalization changed chunk count for {item['guid']}: "
                f"stored={len(stored_chunks)} normalized={len(normalized_chunks)}"
            )
        for stored, normalized in zip(stored_chunks, normalized_chunks, strict=True):
            if " ".join(stored.text.split()) != " ".join(normalized["text"].split()):
                raise RuntimeError(f"caption normalization changed chunk text for {item['guid']}")
            propagated = {**normalized["metadata_json"], **metadata}
            stored.start_ms = normalized["start_ms"]
            stored.end_ms = normalized["end_ms"]
            stored.metadata_json = propagated
            if stored.transcript_segment_id:
                segment = session.get(TranscriptSegment, stored.transcript_segment_id)
                if segment is not None:
                    segment.start_ms = normalized["start_ms"]
                    segment.end_ms = normalized["end_ms"]
                    segment.metadata_json = propagated
        attach_source_entities(session, source_id=source_id, entities=manifest.get("entities"))
        session.commit()


def _run_import_unlocked(*, citara_root: Path, manifest_path: Path, caption_root: Path) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    for item in manifest.get("episodes") or []:
        validate_item(item)
        caption_value = item.get("caption_path")
        if not caption_value:
            results.append({"guid": item["guid"], "status": "no_caption"})
            continue
        caption_path = resolve_caption_path(str(caption_value), manifest_path=manifest_path, caption_root=caption_root)
        if not caption_path.exists():
            results.append({"guid": item["guid"], "status": "missing_caption_file"})
            continue
        raw_caption = json.loads(caption_path.read_text(encoding="utf-8"))
        cues = parse_json3_caption(raw_caption)
        chunks = chunk_caption_cues(cues, item["canonical_url"])
        if not chunks:
            results.append({"guid": item["guid"], "status": "empty_caption"})
            continue
        item_dir = citara_root / "source-artifacts" / item["source_tree_slug"] / "items" / stable_item_id(item)
        metadata = item_metadata(manifest, item, item_dir, caption_path)
        existing = find_existing_source(item)
        stale_source = None
        if existing is not None and (existing.metadata_json or {}).get("caption_normalization_version") != 2:
            assert_owned_caption_source(existing, item, stale=True)
            stale_source = existing
            # Never overwrite artifacts still referenced by the usable old source.
            item_dir = item_dir / "replacements" / existing.id
            metadata = {**item_metadata(manifest, item, item_dir, caption_path), "caption_replacement_of": existing.id}
            existing = None
        if existing is not None:
            reconcile_existing(existing.id, manifest, item, metadata, chunks)
            write_item_artifacts(
                item_dir,
                manifest=manifest,
                item=item,
                metadata=metadata,
                chunks=chunks,
                raw_caption=raw_caption,
            )
            results.append({"guid": item["guid"], "status": "skipped_existing", "source_id": existing.id})
            continue
        write_item_artifacts(
            item_dir,
            manifest=manifest,
            item=item,
            metadata=metadata,
            chunks=chunks,
            raw_caption=raw_caption,
        )
        propagated = [{**chunk, "metadata_json": {**chunk["metadata_json"], **metadata}} for chunk in chunks]
        if stale_source is not None:
            with SessionLocal() as session:
                pending = [
                    row
                    for row in session.query(Source).filter_by(canonical_url=item["canonical_url"]).all()
                    if (row.metadata_json or {}).get("caption_replacement_of") == stale_source.id
                ]
                if len(pending) > 1:
                    raise RuntimeError("multiple pending caption replacements; old source retained")
                if pending:
                    candidate = pending[0]
                    assert_owned_caption_source(candidate, item, stale=False, pending=True)
                    try:
                        validate_replacement(session, candidate, chunks)
                    except RuntimeError:
                        # Only this exactly owned, journaled candidate is disposable.
                        # The stale source remains usable throughout cleanup/rebuild.
                        delete_source_chunks(session, candidate.id)
                        for model in (Embedding, Chunk, TranscriptSegment, SourceEntity, IngestionJob):
                            session.query(model).filter_by(source_id=candidate.id).delete(synchronize_session=False)
                        session.delete(candidate)
                        session.commit()
                        source_id = None
                    else:
                        source_id = candidate.id
                else:
                    source_id = None
            if source_id is not None:
                replace_stale_caption_source(stale_source.id, item, replacement_id=source_id, chunks=chunks, author=manifest["author"])
                results.append({"guid": item["guid"], "status": "reimported", "source_id": source_id})
                continue
        with SessionLocal() as session:
            source = add_transcript_source(
                session,
                payload={
                    "show_title": item["series_title"],
                    "episode_title": source_title(item),
                    "episode_url": item["canonical_url"],
                    "source_type": "youtube_video",
                    "provider": manifest["provider"],
                    "language": "en",
                    "segments": propagated,
                    "entities": manifest.get("entities") or [],
                    "metadata_json": metadata,
                },
            )
            source.author = manifest["author"]
            source.provider = manifest["provider"]
            source.external_id = item["guid"]
            session.add(source)
            session.commit()
            source_id = source.id
        if stale_source is not None:
            replace_stale_caption_source(stale_source.id, item, replacement_id=source_id, chunks=chunks, author=manifest["author"])
        results.append({"guid": item["guid"], "status": "reimported" if stale_source else "imported", "source_id": source_id})
    return results


def run_import(*, citara_root: Path, manifest_path: Path, caption_root: Path | None = None) -> list[dict[str, Any]]:
    root = citara_root.expanduser().resolve()
    manifest = manifest_path.expanduser().resolve()
    approved_captions = (caption_root or (manifest.parent / "captions")).expanduser().resolve()
    configure_citara_root(root)
    with import_lock(root):
        return _run_import_unlocked(
            citara_root=root,
            manifest_path=manifest,
            caption_root=approved_captions,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--citara-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    backup = backup_database(args.citara_root / "citara.db", "youtube-caption-import")
    results = run_import(citara_root=args.citara_root, manifest_path=args.manifest)
    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    print(json.dumps({"counts": counts, "backup": str(backup) if backup else None, "results": results}, indent=2))
    return 1 if any(status in counts for status in ("missing_caption_file", "empty_caption")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
