"""add embeddings table

Revision ID: 20260629_0002
Revises: 20260629_0001
Create Date: 2026-06-29 00:00:01.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "20260629_0002"
down_revision: str | None = "20260629_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _vector_column_type() -> sa.types.TypeEngine:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return Vector(8)
    return sa.JSON()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "embeddings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("chunk_id", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=255), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("vector", _vector_column_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_embeddings_chunk_id"), "embeddings", ["chunk_id"], unique=False)
    op.create_index(op.f("ix_embeddings_embedding_model"), "embeddings", ["embedding_model"], unique=False)
    op.create_index(op.f("ix_embeddings_source_id"), "embeddings", ["source_id"], unique=False)
    op.create_index(op.f("ix_embeddings_tenant_id"), "embeddings", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_embeddings_tenant_id"), table_name="embeddings")
    op.drop_index(op.f("ix_embeddings_source_id"), table_name="embeddings")
    op.drop_index(op.f("ix_embeddings_embedding_model"), table_name="embeddings")
    op.drop_index(op.f("ix_embeddings_chunk_id"), table_name="embeddings")
    op.drop_table("embeddings")
