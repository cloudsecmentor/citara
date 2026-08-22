"""add chunk_fts full-text index (SQLite FTS5)

Creates the virtual table only. Populating it for an existing corpus is a
separate, resumable step -- run `scripts/backfill_fts.py` after upgrading.
Keeping the backfill out of the migration means a large corpus does not turn
`alembic upgrade head` into a long, non-restartable transaction.

Postgres is a no-op here: it has no FTS5, and keyword search falls back to the
portable BM25 scan in `core/retrieval/keyword.py`.

Revision ID: 20260816_0005
Revises: 20260704_0004
Create Date: 2026-08-16 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260816_0005"
down_revision: str | None = "20260704_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "chunk_fts"


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5(chunk_id UNINDEXED, tokens, tokenize='unicode61')")


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute(f"DROP TABLE IF EXISTS {TABLE}")
