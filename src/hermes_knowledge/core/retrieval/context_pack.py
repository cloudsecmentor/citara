from __future__ import annotations

from sqlalchemy.orm import Session

from hermes_knowledge.core.retrieval.keyword import search_knowledge


def retrieve_context_pack(session: Session, *, query: str, limit: int = 8) -> dict:
    results = search_knowledge(session, query=query, limit=limit)
    return {
        "query": query,
        "chunks": [
            {
                "chunk_id": result.chunk_id,
                "text": result.text,
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
