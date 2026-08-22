#!/usr/bin/env python3
"""Populate the chunk_fts full-text index from existing chunks.

Run once after `alembic upgrade head` on a corpus that predates the index.
Ingestion keeps the index current from then on (see `core/ingestion/`), and
`--check` reports drift if anything ever bypasses those hooks.

    uv run python scripts/backfill_fts.py --check
    uv run python scripts/backfill_fts.py --yes

Rebuilding is idempotent: `--yes` clears the index and repopulates it, so it
is always safe to re-run.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="DATABASE_URL override.")
    parser.add_argument("--check", action="store_true", help="Report index/corpus drift and exit.")
    parser.add_argument("--yes", action="store_true", help="Actually rebuild. Without it, this is a dry run.")
    parser.add_argument("--batch", type=int, default=2000)
    return parser.parse_args()


args = _parse_args()

if args.db:
    os.environ["DATABASE_URL"] = args.db
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from sqlalchemy import func, select, text  # noqa: E402

from citara.core.db import SessionLocal, init_db  # noqa: E402
from citara.core.models import CHUNK_FTS_TABLE, Chunk  # noqa: E402
from citara.core.retrieval import fts  # noqa: E402


def main() -> None:
    init_db()
    with SessionLocal() as session:
        if not fts.fts_available(session):
            raise SystemExit("No FTS5 index available on this database (Postgres uses the BM25 scan fallback).")

        chunks = session.execute(select(func.count()).select_from(Chunk)).scalar_one()
        indexed = fts.index_row_count(session)
        print(f"chunks={chunks:,}  indexed={indexed:,}  drift={chunks - indexed:+,}")

        if args.check:
            raise SystemExit(0 if chunks == indexed else 1)

        if not args.yes:
            print("\nDry run. Re-run with --yes to rebuild.")
            return

        print(f"\nRebuilding {CHUNK_FTS_TABLE}...")
        start = time.perf_counter()
        session.execute(text(f"DELETE FROM {CHUNK_FTS_TABLE}"))

        done = 0
        while True:
            batch = session.execute(select(Chunk).order_by(Chunk.id).offset(done).limit(args.batch)).scalars().all()
            if not batch:
                break
            fts.index_chunks(session, list(batch))
            done += len(batch)
            print(f"  {done:,}/{chunks:,}", end="\r", flush=True)
        session.commit()

        elapsed = time.perf_counter() - start
        print(f"\nIndexed {done:,} chunks in {elapsed:.1f}s. Index now holds {fts.index_row_count(session):,} rows.")


if __name__ == "__main__":
    main()
