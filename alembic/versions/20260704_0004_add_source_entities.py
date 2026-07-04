"""add person/org source entities

Revision ID: 20260704_0004
Revises: 20260629_0003
Create Date: 2026-07-04 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260704_0004"
down_revision: str | None = "20260629_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_entities_tenant_slug"),
    )
    op.create_index(op.f("ix_entities_entity_type"), "entities", ["entity_type"], unique=False)
    op.create_index(op.f("ix_entities_slug"), "entities", ["slug"], unique=False)
    op.create_index(op.f("ix_entities_tenant_id"), "entities", ["tenant_id"], unique=False)

    op.create_table(
        "entity_aliases",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "alias", name="uq_entity_aliases_tenant_alias"),
    )
    op.create_index(op.f("ix_entity_aliases_alias"), "entity_aliases", ["alias"], unique=False)
    op.create_index(op.f("ix_entity_aliases_entity_id"), "entity_aliases", ["entity_id"], unique=False)
    op.create_index(op.f("ix_entity_aliases_tenant_id"), "entity_aliases", ["tenant_id"], unique=False)

    op.create_table(
        "source_entities",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=100), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("provenance", sa.String(length=100), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "source_id", "entity_id", "role", name="uq_source_entities_source_entity_role"),
    )
    op.create_index(op.f("ix_source_entities_entity_id"), "source_entities", ["entity_id"], unique=False)
    op.create_index(op.f("ix_source_entities_source_id"), "source_entities", ["source_id"], unique=False)
    op.create_index(op.f("ix_source_entities_tenant_id"), "source_entities", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_source_entities_tenant_id"), table_name="source_entities")
    op.drop_index(op.f("ix_source_entities_source_id"), table_name="source_entities")
    op.drop_index(op.f("ix_source_entities_entity_id"), table_name="source_entities")
    op.drop_table("source_entities")
    op.drop_index(op.f("ix_entity_aliases_tenant_id"), table_name="entity_aliases")
    op.drop_index(op.f("ix_entity_aliases_entity_id"), table_name="entity_aliases")
    op.drop_index(op.f("ix_entity_aliases_alias"), table_name="entity_aliases")
    op.drop_table("entity_aliases")
    op.drop_index(op.f("ix_entities_tenant_id"), table_name="entities")
    op.drop_index(op.f("ix_entities_slug"), table_name="entities")
    op.drop_index(op.f("ix_entities_entity_type"), table_name="entities")
    op.drop_table("entities")
