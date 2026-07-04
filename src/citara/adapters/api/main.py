from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from citara.core.db import get_session, init_db
from citara.core.entities import list_entities, list_source_entities
from citara.core.ingestion.text import add_text_source
from citara.core.ingestion.transcript import add_transcript_source
from citara.core.jobs import get_ingestion_job, list_ingestion_jobs, list_ingestion_jobs_for_source, serialize_ingestion_job
from citara.core.retrieval.context_pack import retrieve_context_pack, search_by_mode
from citara.core.sources import delete_source, list_sources, set_source_preference
from citara.core.summary import get_source_summary_context, resolve_source_for_summary, resolve_summary_context


class TextSourceRequest(BaseModel):
    title: str
    text: str
    collection_id: str | None = None


class TranscriptSegmentRequest(BaseModel):
    start_ms: int
    end_ms: int | None = None
    speaker: str | None = None
    text: str


class TranscriptSourceRequest(BaseModel):
    show_title: str
    episode_title: str
    episode_url: str | None = None
    collection_id: str | None = None
    segments: list[TranscriptSegmentRequest]


class SourcePreferenceRequest(BaseModel):
    retrieval_weight: float
    preference_label: str | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Citara", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/sources/text")
    def add_text(payload: TextSourceRequest, session: Session = Depends(get_session)) -> dict[str, str]:
        source = add_text_source(
            session,
            title=payload.title,
            text=payload.text,
            collection_id=payload.collection_id,
        )
        jobs = list_ingestion_jobs_for_source(session, source.id, limit=1)
        return {"source_id": source.id, "status": source.status, "job_id": jobs[0].id if jobs else None}

    @app.post("/sources/transcript")
    def add_transcript(payload: TranscriptSourceRequest, session: Session = Depends(get_session)) -> dict[str, str | None]:
        source = add_transcript_source(
            session,
            payload=payload.model_dump(exclude={"collection_id"}),
            collection_id=payload.collection_id,
        )
        jobs = list_ingestion_jobs_for_source(session, source.id, limit=1)
        return {"source_id": source.id, "status": source.status, "job_id": jobs[0].id if jobs else None}

    @app.get("/jobs")
    def jobs(limit: int = 50, session: Session = Depends(get_session)) -> dict:
        return {"jobs": [serialize_ingestion_job(job) for job in list_ingestion_jobs(session, limit=limit)]}

    @app.get("/jobs/{job_id}")
    def job_status(job_id: str, session: Session = Depends(get_session)) -> dict:
        job = get_ingestion_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return serialize_ingestion_job(job)

    @app.get("/sources")
    def sources(session: Session = Depends(get_session)) -> dict:
        return {
            "sources": [
                {
                    "source_id": source.id,
                    "title": source.title,
                    "source_type": source.source_type,
                    "status": source.status,
                    "canonical_url": source.canonical_url,
                }
                for source in list_sources(session)
            ]
        }

    @app.get("/sources/resolve")
    def resolve_source(q: str, preference: str = "current", session: Session = Depends(get_session)) -> dict:
        source = resolve_source_for_summary(session, query=q, preference=preference)
        if source is None:
            raise HTTPException(status_code=404, detail="Source not found")
        metadata = source.metadata_json or {}
        return {
            "source_id": source.id,
            "title": source.title,
            "source_type": source.source_type,
            "canonical_url": source.canonical_url,
            "preference_label": metadata.get("preference_label"),
            "retrieval_weight": metadata.get("retrieval_weight", 1.0),
        }

    @app.get("/sources/summary-context")
    def resolved_summary_context(q: str, preference: str = "current", max_chunks: int | None = None, session: Session = Depends(get_session)) -> dict:
        context = resolve_summary_context(session, query=q, preference=preference, max_chunks=max_chunks)
        if context is None:
            raise HTTPException(status_code=404, detail="Source not found")
        return context

    @app.get("/sources/{source_id}/summary-context")
    def source_summary_context(source_id: str, max_chunks: int | None = None, session: Session = Depends(get_session)) -> dict:
        context = get_source_summary_context(session, source_id, max_chunks=max_chunks)
        if context is None:
            raise HTTPException(status_code=404, detail="Source not found")
        return context

    @app.delete("/sources/{source_id}")
    def delete_source_route(source_id: str, session: Session = Depends(get_session)) -> dict:
        deleted = delete_source(session, source_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Source not found")
        return {"source_id": source_id, "deleted": True}

    @app.patch("/sources/{source_id}/preference")
    def source_preference(source_id: str, payload: SourcePreferenceRequest, session: Session = Depends(get_session)) -> dict:
        try:
            source = set_source_preference(
                session,
                source_id,
                retrieval_weight=payload.retrieval_weight,
                preference_label=payload.preference_label,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if source is None:
            raise HTTPException(status_code=404, detail="Source not found")
        return {"source_id": source.id, "metadata": source.metadata_json}

    @app.get("/search")
    def search(q: str, limit: int = 10, mode: str = "hybrid", entity: str | None = None, session: Session = Depends(get_session)) -> dict:
        entity_slugs = [part.strip() for part in entity.split(",") if part.strip()] if entity else None
        try:
            results = search_by_mode(session, query=q, limit=limit, mode=mode, entity_slugs=entity_slugs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "results": [
                {
                    "chunk_id": result.chunk_id,
                    "source_id": result.source_id,
                    "source_title": result.source_title,
                    "source_type": result.source_type,
                    "text": result.text,
                    "score": result.score,
                    "citation_label": result.citation_label,
                    "source_url": result.canonical_url,
                    "timestamp_url": result.timestamp_url,
                    "start_ms": result.start_ms,
                    "end_ms": result.end_ms,
                }
                for result in results
            ]
        }

    @app.get("/context-pack")
    def context_pack(q: str, limit: int = 8, mode: str = "hybrid", entity: str | None = None, session: Session = Depends(get_session)) -> dict:
        entity_slugs = [part.strip() for part in entity.split(",") if part.strip()] if entity else None
        try:
            return retrieve_context_pack(session, query=q, limit=limit, mode=mode, entity_slugs=entity_slugs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/entities")
    def entities(session: Session = Depends(get_session)) -> dict:
        return {"entities": list_entities(session)}

    @app.get("/sources/{source_id}/entities")
    def source_entities(source_id: str, session: Session = Depends(get_session)) -> dict:
        return {"source_id": source_id, "entities": list_source_entities(session, source_id)}

    return app


app = create_app()
