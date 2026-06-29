from __future__ import annotations

from sqlalchemy.orm import Session

from hermes_knowledge.core.retrieval.hybrid import hybrid_search
from hermes_knowledge.core.retrieval.keyword import SearchResult, search_knowledge
from hermes_knowledge.core.retrieval.vector import vector_search


def search_by_mode(session: Session, *, query: str, limit: int = 10, mode: str = "hybrid") -> list[SearchResult]:
    if mode == "keyword":
        return search_knowledge(session, query=query, limit=limit)
    if mode == "vector":
        return vector_search(session, query=query, limit=limit)
    if mode == "hybrid":
        return hybrid_search(session, query=query, limit=limit)
    raise ValueError(f"Unsupported search mode: {mode}")


def retrieve_context_pack(session: Session, *, query: str, limit: int = 8, mode: str = "hybrid") -> dict:
    results = search_by_mode(session, query=query, limit=limit, mode=mode)
    return {
        "query": query,
        "mode": mode,
        "chunks": [
            {
                "chunk_id": result.chunk_id,
                "text": result.text,
                "score": result.score,
                "citation": {
                    "label": result.citation_label,
                    "source_url": result.canonical_url,
                    "timestamp_url": result.timestamp_url,
                    "page_number": result.page_number,
                    "start_ms": result.start_ms,
                    "end_ms": result.end_ms,
                },
            }
            for result in results
        ],
    }
