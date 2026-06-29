"""allow variable embedding dimensions

Revision ID: 20260629_0003
Revises: 20260629_0002
Create Date: 2026-06-29 00:00:02.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "20260629_0003"
down_revision: str | None = "20260629_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column("embeddings", "vector", type_=Vector())


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column("embeddings", "vector", type_=Vector(8))
