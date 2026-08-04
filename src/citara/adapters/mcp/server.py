from __future__ import annotations

from collections.abc import Callable

from citara import __version__
from citara.core.db import SessionLocal, init_db
from citara.core.entities import list_entities as core_list_entities
from citara.core.entities import list_source_entities as core_list_source_entities
from citara.core.ingestion.text import add_text_source as core_add_text_source
from citara.core.ingestion.transcript import add_transcript_source as core_add_transcript_source
from citara.core.jobs import get_ingestion_job as core_get_ingestion_job
from citara.core.jobs import list_ingestion_jobs as core_list_ingestion_jobs
from citara.core.jobs import list_ingestion_jobs_for_source as core_list_ingestion_jobs_for_source
from citara.core.jobs import serialize_ingestion_job
from citara.core.retrieval.context_pack import query_language_notice as core_query_language_notice
from citara.core.retrieval.context_pack import retrieve_context_pack as core_retrieve_context_pack
from citara.core.retrieval.context_pack import search_by_mode as core_search_by_mode
from citara.core.sources import delete_source as core_delete_source
from citara.core.sources import list_sources as core_list_sources
from citara.core.sources import set_source_preference as core_set_source_preference
from citara.core.summary import get_source_summary_context as core_get_source_summary_context
from citara.core.summary import resolve_source_for_summary as core_resolve_source_for_summary
from citara.core.summary import resolve_summary_context as core_resolve_summary_context

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
    server = FastMCP("citara")

    @server.tool()
    def ping() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @server.tool()
    def add_text_source(title: str, text: str, collection_id: str | None = None) -> dict[str, str | None]:
        init_db()
        with SessionLocal() as session:
            source = core_add_text_source(session, title=title, text=text, collection_id=collection_id)
            jobs = core_list_ingestion_jobs_for_source(session, source.id, limit=1)
            return {"source_id": source.id, "status": source.status, "job_id": jobs[0].id if jobs else None}

    @server.tool()
    def add_transcript_source(payload: dict, collection_id: str | None = None) -> dict[str, str | None]:
        init_db()
        with SessionLocal() as session:
            source = core_add_transcript_source(session, payload=payload, collection_id=collection_id)
            jobs = core_list_ingestion_jobs_for_source(session, source.id, limit=1)
            return {"source_id": source.id, "status": source.status, "job_id": jobs[0].id if jobs else None}

    @server.tool()
    def search_knowledge(
        query: str,
        limit: int = 10,
        mode: str = "hybrid",
        entity_slugs: list[str] | None = None,
        source_tree_slug: str | None = None,
        language_policy: str = "auto",
        language: str | None = None,
        query_translated: str | None = None,
        query_language: str | None = None,
    ) -> dict:
        """Search the knowledge base and return citation-backed chunks.

        If `query` is not in the corpus's language (the corpus is English by
        default), pass `query_translated` with an English translation of the
        query -- you are already a multilingual model holding the user's
        turn, so this costs nothing and is the preferred path. Citara then
        searches both the original and translated query and fuses the
        results, so proper nouns that only survive in the original are not
        lost. `query_language` optionally states the language of `query`
        (e.g. "ru") when you already know it; otherwise Citara detects it.
        A `notice` is included in the response when the query couldn't be
        tokenized or its detected language has no matching sources, so an
        empty `results` list is explainable rather than silent.
        """
        init_db()
        with SessionLocal() as session:
            results = core_search_by_mode(
                session,
                query=query,
                limit=limit,
                mode=mode,
                entity_slugs=entity_slugs,
                source_tree_slug=source_tree_slug,
                language_policy=language_policy,
                language=language,
                query_translated=query_translated,
                query_language=query_language,
            )
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
                    for result in results
                ],
                "notice": core_query_language_notice(session, query=query),
            }

    @server.tool()
    def retrieve_context_pack(
        query: str,
        limit: int = 8,
        mode: str = "hybrid",
        entity_slugs: list[str] | None = None,
        source_tree_slug: str | None = None,
        language_policy: str = "auto",
        language: str | None = None,
        query_translated: str | None = None,
        query_language: str | None = None,
        translate_quotes: bool = False,
    ) -> dict:
        """Retrieve a compact, citation-backed context pack for a query.

        If `query` is not in the corpus's language, pass `query_translated`
        with an English translation alongside the original -- see
        `search_knowledge` for why this is the preferred path. The response
        includes `response_language` (the language to answer the user in)
        and a `response_language_directive`; retrieved `chunks[].text` is
        always the verbatim, untranslated source string. Set
        `translate_quotes=True` to additionally receive
        `chunks[].text_translated` and `translation_provenance` -- these are
        added alongside `text`, never in place of it. A `notice` explains an
        empty/near-empty `chunks` list caused by a query/corpus language
        mismatch.
        """
        init_db()
        with SessionLocal() as session:
            return core_retrieve_context_pack(
                session,
                query=query,
                limit=limit,
                mode=mode,
                entity_slugs=entity_slugs,
                source_tree_slug=source_tree_slug,
                language_policy=language_policy,
                language=language,
                query_translated=query_translated,
                query_language=query_language,
                translate_quotes=translate_quotes,
            )

    @server.tool()
    def list_entities() -> dict:
        init_db()
        with SessionLocal() as session:
            return {"entities": core_list_entities(session)}

    @server.tool()
    def get_source_entities(source_id: str) -> dict:
        init_db()
        with SessionLocal() as session:
            return {"source_id": source_id, "entities": core_list_source_entities(session, source_id)}

    @server.tool()
    def resolve_source(query: str, preference: str = "current") -> dict:
        init_db()
        with SessionLocal() as session:
            source = core_resolve_source_for_summary(session, query=query, preference=preference)
            if source is None:
                return {"found": False, "query": query}
            metadata = source.metadata_json or {}
            return {
                "found": True,
                "source_id": source.id,
                "title": source.title,
                "source_type": source.source_type,
                "canonical_url": source.canonical_url,
                "preference_label": metadata.get("preference_label"),
                "retrieval_weight": metadata.get("retrieval_weight", 1.0),
            }

    @server.tool()
    def get_source_summary_context(source_id: str, max_chunks: int | None = None) -> dict:
        init_db()
        with SessionLocal() as session:
            context = core_get_source_summary_context(session, source_id, max_chunks=max_chunks)
            return {"found": False, "source_id": source_id} if context is None else {"found": True, **context}

    @server.tool()
    def resolve_summary_context(query: str, preference: str = "current", max_chunks: int | None = None) -> dict:
        init_db()
        with SessionLocal() as session:
            context = core_resolve_summary_context(session, query=query, preference=preference, max_chunks=max_chunks)
            return {"found": False, "query": query} if context is None else {"found": True, **context}

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
    def set_source_preference(source_id: str, retrieval_weight: float, preference_label: str | None = None) -> dict:
        init_db()
        with SessionLocal() as session:
            source = core_set_source_preference(
                session,
                source_id,
                retrieval_weight=retrieval_weight,
                preference_label=preference_label,
            )
            return (
                {"found": False, "source_id": source_id}
                if source is None
                else {"found": True, "source_id": source.id, "metadata": source.metadata_json}
            )

    @server.tool()
    def delete_source(source_id: str) -> dict:
        init_db()
        with SessionLocal() as session:
            return {"source_id": source_id, "deleted": core_delete_source(session, source_id)}

    return server


server = create_mcp_server()
