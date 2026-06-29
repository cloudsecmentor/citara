from __future__ import annotations

from collections.abc import Callable

from hermes_knowledge.core.db import SessionLocal, init_db
from hermes_knowledge.core.ingestion.text import add_text_source as core_add_text_source
from hermes_knowledge.core.jobs import get_ingestion_job as core_get_ingestion_job
from hermes_knowledge.core.jobs import list_ingestion_jobs as core_list_ingestion_jobs
from hermes_knowledge.core.jobs import list_ingestion_jobs_for_source as core_list_ingestion_jobs_for_source
from hermes_knowledge.core.jobs import serialize_ingestion_job
from hermes_knowledge.core.retrieval.context_pack import retrieve_context_pack as core_retrieve_context_pack
from hermes_knowledge.core.retrieval.context_pack import search_by_mode as core_search_by_mode
from hermes_knowledge.core.sources import delete_source as core_delete_source
from hermes_knowledge.core.sources import list_sources as core_list_sources

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover - fallback for minimal local installs
    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, name: str | None = None, **_: object) -> None:
            self.name = name
            self._tools: dict[str, Callable[..., object]] = {}

        def tool(self, name: str | None = None) -> Callable[[Callable[..., object]], Callable[..., object]]:
            def decorator(func: Callable[..., object]) -> Callable[..., object]:
                self._tools[name or func.__name__] = func
                return func

            return decorator


def create_mcp_server() -> FastMCP:
    server = FastMCP("hermes-knowledge-vault")

    @server.tool()
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    @server.tool()
    def add_text_source(title: str, text: str, collection_id: str | None = None) -> dict[str, str | None]:
        init_db()
        with SessionLocal() as session:
            source = core_add_text_source(session, title=title, text=text, collection_id=collection_id)
            jobs = core_list_ingestion_jobs_for_source(session, source.id, limit=1)
            return {"source_id": source.id, "status": source.status, "job_id": jobs[0].id if jobs else None}

    @server.tool()
    def search_knowledge(query: str, limit: int = 10, mode: str = "hybrid") -> dict:
        init_db()
        with SessionLocal() as session:
            return {
                "results": [
                    {
                        "chunk_id": result.chunk_id,
                        "source_id": result.source_id,
                        "source_title": result.source_title,
                        "text": result.text,
                        "citation_label": result.citation_label,
                        "source_url": result.canonical_url,
                        "timestamp_url": result.timestamp_url,
                        "score": result.score,
                    }
                    for result in core_search_by_mode(session, query=query, limit=limit, mode=mode)
                ]
            }

    @server.tool()
    def retrieve_context_pack(query: str, limit: int = 8, mode: str = "hybrid") -> dict:
        init_db()
        with SessionLocal() as session:
            return core_retrieve_context_pack(session, query=query, limit=limit, mode=mode)

    @server.tool()
    def list_sources(limit: int = 50) -> dict:
        init_db()
        with SessionLocal() as session:
            return {
                "sources": [
                    {
                        "source_id": source.id,
                        "title": source.title,
                        "source_type": source.source_type,
                        "status": source.status,
                        "canonical_url": source.canonical_url,
                    }
                    for source in core_list_sources(session, limit=limit)
                ]
            }

    @server.tool()
    def list_ingestion_jobs(limit: int = 50) -> dict:
        init_db()
        with SessionLocal() as session:
            return {"jobs": [serialize_ingestion_job(job) for job in core_list_ingestion_jobs(session, limit=limit)]}

    @server.tool()
    def get_ingestion_job_status(job_id: str) -> dict:
        init_db()
        with SessionLocal() as session:
            job = core_get_ingestion_job(session, job_id)
            return {"found": False, "job_id": job_id} if job is None else {"found": True, **serialize_ingestion_job(job)}

    @server.tool()
    def delete_source(source_id: str) -> dict:
        init_db()
        with SessionLocal() as session:
            return {"source_id": source_id, "deleted": core_delete_source(session, source_id)}

    return server


server = create_mcp_server()
