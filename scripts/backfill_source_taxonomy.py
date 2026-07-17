#!/usr/bin/env python3
"""Backfill source-tree metadata and person/org links for known podcast corpora."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from sqlalchemy import select

DEFAULT_CITARA_ROOT = Path("../citara")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{DEFAULT_CITARA_ROOT / 'citara.db'}")
os.environ.setdefault("SOURCE_ARTIFACT_ROOT", str(DEFAULT_CITARA_ROOT / "source-artifacts"))
os.environ.setdefault("SOURCE_STATE_ROOT", str(DEFAULT_CITARA_ROOT / "import-state"))
os.environ.setdefault("OBJECT_STORE_PATH", str(DEFAULT_CITARA_ROOT / "object-store"))

from citara.core.db import SessionLocal, init_db
from citara.core.entities import attach_source_entities
from citara.core.models import Chunk, Source, TranscriptSegment
from citara.core.source_taxonomy import BEMA_ENTITIES, BEMA_SOURCE_TREE_SLUG, TEXTINUS_ENTITIES, TEXTINUS_SOURCE_TREE_SLUG


def infer_tree(source: Source) -> str | None:
    metadata = source.metadata_json or {}
    if metadata.get("source_tree_slug") in {BEMA_SOURCE_TREE_SLUG, TEXTINUS_SOURCE_TREE_SLUG}:
        return str(metadata["source_tree_slug"])
    title = source.title or ""
    show_title = str(metadata.get("show_title") or "")
    if title.startswith("BEMA ") or show_title in {"The BEMA Podcast", "BEMA Podcast"}:
        return BEMA_SOURCE_TREE_SLUG
    if title.startswith("Text in Us:") or show_title == "Text in Us":
        return TEXTINUS_SOURCE_TREE_SLUG
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Citara source_tree_slug and source_entities for BEMA/Text in Us")
    parser.add_argument("--citara-root", type=Path, default=DEFAULT_CITARA_ROOT)
    args = parser.parse_args()
    os.environ["DATABASE_URL"] = f"sqlite:///{args.citara_root / 'citara.db'}"
    os.environ["SOURCE_ARTIFACT_ROOT"] = str(args.citara_root / "source-artifacts")
    os.environ["SOURCE_STATE_ROOT"] = str(args.citara_root / "import-state")
    os.environ["OBJECT_STORE_PATH"] = str(args.citara_root / "object-store")

    init_db()
    counts = {"bema": 0, "textinus": 0, "unchanged": 0}
    with SessionLocal() as session:
        sources = session.execute(select(Source)).scalars().all()
        for source in sources:
            tree = infer_tree(source)
            if tree is None:
                counts["unchanged"] += 1
                continue
            metadata = dict(source.metadata_json or {})
            metadata.setdefault("source_tree_slug", tree)
            metadata.setdefault("source_tree_type", "podcast")
            source.metadata_json = metadata
            entities = BEMA_ENTITIES if tree == BEMA_SOURCE_TREE_SLUG else TEXTINUS_ENTITIES
            attach_source_entities(session, source_id=source.id, entities=entities, tenant_id=source.tenant_id)
            # Keep chunk/segment metadata filterable/debuggable for existing rows too.
            for chunk in session.query(Chunk).filter(Chunk.source_id == source.id).all():
                chunk.metadata_json = {**(chunk.metadata_json or {}), "source_tree_slug": tree, "source_tree_type": "podcast"}
            for segment in session.query(TranscriptSegment).filter(TranscriptSegment.source_id == source.id).all():
                segment.metadata_json = {**(segment.metadata_json or {}), "source_tree_slug": tree, "source_tree_type": "podcast"}
            counts[tree] += 1
        session.commit()
    print(counts)


if __name__ == "__main__":
    main()
