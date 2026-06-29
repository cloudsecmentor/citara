from __future__ import annotations

from sqlalchemy.orm import Session

from hermes_knowledge.core.config import settings
from hermes_knowledge.core.models import Tenant, User


def ensure_local_identity(
    session: Session,
    *,
    tenant_id: str = settings.default_tenant_id,
    user_id: str = settings.default_user_id,
) -> tuple[Tenant, User]:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        tenant = Tenant(id=tenant_id, name=tenant_id)
        session.add(tenant)

    user = session.get(User, user_id)
    if user is None:
        user = User(id=user_id, tenant_id=tenant_id, display_name=user_id, role="owner")
        session.add(user)

    session.flush()
    return tenant, user
