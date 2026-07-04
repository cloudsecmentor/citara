from __future__ import annotations

import re
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from citara.core.config import settings
from citara.core.models import Entity, EntityAlias, SourceEntity

ALLOWED_ENTITY_TYPES = {"person", "organization"}
_ENTITY_TYPE_ALIASES = {"org": "organization", "organization": "organization", "person": "person"}


def normalize_entity_type(value: str | None) -> str | None:
    if value is None:
        return None
    return _ENTITY_TYPE_ALIASES.get(value.strip().lower())


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def parse_entity_slug(value: str) -> tuple[str | None, str]:
    if ":" not in value:
        return None, value
    prefix, slug = value.split(":", 1)
    return normalize_entity_type(prefix), slug


def normalize_entity_payload(payload: dict) -> dict | None:
    entity_type = normalize_entity_type(payload.get("entity_type") or payload.get("type"))
    if entity_type not in ALLOWED_ENTITY_TYPES:
        return None
    label = payload.get("entity_label") or payload.get("label") or payload.get("name")
    slug = payload.get("entity_slug") or payload.get("slug") or (slugify(label) if label else None)
    if not slug:
        return None
    label = label or slug.replace("-", " ").title()
    return {
        "entity_type": entity_type,
        "slug": slugify(slug),
        "label": label,
        "role": payload.get("role") or "related",
        "confidence": payload.get("confidence"),
        "provenance": payload.get("provenance") or "source_metadata",
        "metadata_json": payload.get("metadata_json") or payload.get("metadata") or {},
        "aliases": payload.get("aliases") or [],
    }


def resolve_or_create_entity(
    session: Session,
    *,
    entity_type: str,
    slug: str,
    label: str,
    aliases: list[str] | None = None,
    tenant_id: str = settings.default_tenant_id,
    metadata_json: dict | None = None,
) -> Entity:
    slug = slugify(slug)
    entity = session.execute(select(Entity).where(Entity.tenant_id == tenant_id, Entity.slug == slug)).scalar_one_or_none()
    if entity is None:
        entity = Entity(
            id=f"ent_{uuid4().hex}",
            tenant_id=tenant_id,
            entity_type=entity_type,
            name=label,
            slug=slug,
            metadata_json=metadata_json or {},
        )
        session.add(entity)
        session.flush()
    elif entity.entity_type != entity_type:
        raise ValueError(f"Entity slug {slug!r} already exists as {entity.entity_type}, not {entity_type}")

    for alias in [label, *(aliases or [])]:
        alias = str(alias).strip()
        if not alias:
            continue
        existing_alias = session.execute(
            select(EntityAlias).where(EntityAlias.tenant_id == tenant_id, EntityAlias.alias == alias)
        ).scalar_one_or_none()
        if existing_alias is None:
            session.add(EntityAlias(id=f"eal_{uuid4().hex}", tenant_id=tenant_id, entity_id=entity.id, alias=alias))
    session.flush()
    return entity


def attach_source_entities(
    session: Session,
    *,
    source_id: str,
    entities: list[dict] | None,
    tenant_id: str = settings.default_tenant_id,
) -> list[SourceEntity]:
    attached: list[SourceEntity] = []
    for raw in entities or []:
        normalized = normalize_entity_payload(raw)
        if normalized is None:
            continue
        entity = resolve_or_create_entity(
            session,
            entity_type=normalized["entity_type"],
            slug=normalized["slug"],
            label=normalized["label"],
            aliases=normalized["aliases"],
            tenant_id=tenant_id,
            metadata_json=normalized["metadata_json"],
        )
        existing = session.execute(
            select(SourceEntity).where(
                SourceEntity.tenant_id == tenant_id,
                SourceEntity.source_id == source_id,
                SourceEntity.entity_id == entity.id,
                SourceEntity.role == normalized["role"],
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = SourceEntity(
                id=f"sen_{uuid4().hex}",
                tenant_id=tenant_id,
                source_id=source_id,
                entity_id=entity.id,
                role=normalized["role"],
                confidence=normalized["confidence"],
                provenance=normalized["provenance"],
                metadata_json=normalized["metadata_json"],
            )
            session.add(existing)
            session.flush()
        attached.append(existing)
    return attached


def resolve_entity_ids(
    session: Session,
    *,
    entity_slugs: list[str] | None = None,
    tenant_id: str = settings.default_tenant_id,
) -> list[str]:
    ids: list[str] = []
    for raw in entity_slugs or []:
        entity_type, slug = parse_entity_slug(raw)
        conditions = [Entity.tenant_id == tenant_id, Entity.slug == slugify(slug)]
        if entity_type:
            conditions.append(Entity.entity_type == entity_type)
        entity = session.execute(select(Entity).where(*conditions)).scalar_one_or_none()
        if entity is not None:
            ids.append(entity.id)
    return ids


def list_source_entities(session: Session, source_id: str, *, tenant_id: str = settings.default_tenant_id) -> list[dict]:
    rows = session.execute(
        select(SourceEntity, Entity)
        .join(Entity, SourceEntity.entity_id == Entity.id)
        .where(SourceEntity.tenant_id == tenant_id, SourceEntity.source_id == source_id)
        .order_by(Entity.entity_type, Entity.slug, SourceEntity.role)
    ).all()
    return [
        {
            "id": entity.id,
            "type": entity.entity_type,
            "slug": entity.slug,
            "label": entity.name,
            "role": source_entity.role,
            "confidence": source_entity.confidence,
            "provenance": source_entity.provenance,
            "metadata": source_entity.metadata_json,
        }
        for source_entity, entity in rows
    ]


def list_entities(session: Session, *, tenant_id: str = settings.default_tenant_id) -> list[dict]:
    rows = session.execute(select(Entity).where(Entity.tenant_id == tenant_id).order_by(Entity.entity_type, Entity.slug)).scalars().all()
    return [{"id": row.id, "type": row.entity_type, "slug": row.slug, "label": row.name, "metadata": row.metadata_json} for row in rows]
