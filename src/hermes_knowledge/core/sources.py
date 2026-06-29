from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes_knowledge.core.config import settings
from hermes_knowledge.core.models import Source


def list_sources(session: Session, *, tenant_id: str = settings.default_tenant_id, limit: int = 50) -> list[Source]:
    return list(
        session.execute(
            select(Source)
            .where(Source.tenant_id == tenant_id)
            .order_by(Source.created_at.desc())
            .limit(limit)
        ).scalars()
    )
