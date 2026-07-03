from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from citara.core.config import settings
from citara.core.models import IngestionJob


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def record_succeeded_ingestion_job(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    source_id: str,
    job_type: str,
    input_json: dict,
    result_json: dict,
) -> IngestionJob:
    now = utcnow()
    job = IngestionJob(
        id=f"job_{uuid4().hex}",
        tenant_id=tenant_id,
        user_id=user_id,
        source_id=source_id,
        job_type=job_type,
        status="succeeded",
        input_json=input_json,
        result_json=result_json,
        created_at=now,
        started_at=now,
        finished_at=now,
    )
    session.add(job)
    session.flush()
    return job


def list_ingestion_jobs(
    session: Session,
    *,
    tenant_id: str = settings.default_tenant_id,
    limit: int = 50,
) -> list[IngestionJob]:
    return list(
        session.execute(
            select(IngestionJob)
            .where(IngestionJob.tenant_id == tenant_id)
            .order_by(IngestionJob.created_at.desc())
            .limit(limit)
        ).scalars()
    )


def list_ingestion_jobs_for_source(
    session: Session,
    source_id: str,
    *,
    tenant_id: str = settings.default_tenant_id,
    limit: int = 10,
) -> list[IngestionJob]:
    return list(
        session.execute(
            select(IngestionJob)
            .where(IngestionJob.tenant_id == tenant_id, IngestionJob.source_id == source_id)
            .order_by(IngestionJob.created_at.desc())
            .limit(limit)
        ).scalars()
    )


def get_ingestion_job(
    session: Session,
    job_id: str,
    *,
    tenant_id: str = settings.default_tenant_id,
) -> IngestionJob | None:
    job = session.get(IngestionJob, job_id)
    if job is None or job.tenant_id != tenant_id:
        return None
    return job


def serialize_ingestion_job(job: IngestionJob) -> dict:
    return {
        "job_id": job.id,
        "tenant_id": job.tenant_id,
        "user_id": job.user_id,
        "source_id": job.source_id,
        "job_type": job.job_type,
        "status": job.status,
        "input": job.input_json,
        "result": job.result_json,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
