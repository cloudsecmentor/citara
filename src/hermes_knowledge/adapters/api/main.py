from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from hermes_knowledge.core.db import get_session, init_db
from hermes_knowledge.core.ingestion.text import add_text_source
from hermes_knowledge.core.retrieval.context_pack import retrieve_context_pack, search_by_mode
from hermes_knowledge.core.sources import delete_source, list_sources


class TextSourceRequest(BaseModel):
    title: str
    text: str
    collection_id: str | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Hermes Knowledge Vault", version="0.1.0", lifespan=lifespan)

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
        return {"source_id": source.id, "status": source.status}

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

    @app.delete("/sources/{source_id}")
    def delete_source_route(source_id: str, session: Session = Depends(get_session)) -> dict:
        deleted = delete_source(session, source_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Source not found")
        return {"source_id": source_id, "deleted": True}

    @app.get("/search")
    def search(q: str, limit: int = 10, mode: str = "hybrid", session: Session = Depends(get_session)) -> dict:
        try:
            results = search_by_mode(session, query=q, limit=limit, mode=mode)
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
    def context_pack(q: str, limit: int = 8, mode: str = "hybrid", session: Session = Depends(get_session)) -> dict:
        try:
            return retrieve_context_pack(session, query=q, limit=limit, mode=mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


app = create_app()
