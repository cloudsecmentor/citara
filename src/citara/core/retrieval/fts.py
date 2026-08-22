from __future__ import annotations

from sqlalchemy import column, literal_column, select, table, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from citara.core.chunking.simple import tokenize
from citara.core.models import CHUNK_FTS_TABLE, Chunk, Source
from citara.core.retrieval.base import SearchResult, apply_source_filters, result_from

_fts = table(CHUNK_FTS_TABLE, column("chunk_id"), column("tokens"))

# `retrieval_weight` applied in SQL rather than after the fact. FTS5's bm25()
# is negative (more relevant = more negative), so multiplying by a weight > 1
# pushes a row further down the ascending sort -- i.e. ranks it higher. That
# means weighting stays EXACT under an indexed top-N query; there is no need
# to over-fetch and re-sort, and no window outside which a heavily weighted
# source becomes unreachable.
_WEIGHT_SQL = (
    "CASE WHEN typeof(json_extract(sources.metadata_json, '$.retrieval_weight')) IN ('integer', 'real') "
    "AND json_extract(sources.metadata_json, '$.retrieval_weight') > 0 "
    "THEN json_extract(sources.metadata_json, '$.retrieval_weight') ELSE 1.0 END"
)


def fts_available(session: Session) -> bool:
    """True when this session can serve keyword search from the FTS index."""
    if session.bind is None or session.bind.dialect.name != "sqlite":
        return False
    row = session.execute(
        text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": CHUNK_FTS_TABLE},
    ).first()
    return row is not None


def build_match(tokens: list[str]) -> str:
    """Build an FTS5 MATCH expression from already-tokenized query terms.

    Every token is quoted as an FTS5 string literal. This is not optional:
    unquoted, a perfectly ordinary query like "black and white" or
    "near death" hands FTS5 its own boolean operators (AND, OR, NOT, NEAR)
    and either errors or silently changes the query's meaning.
    """
    return " OR ".join('"{}"'.format(token.replace('"', '""')) for token in tokens)


def index_text(text_value: str) -> str:
    """The string stored in the index -- tokenized, so index and query agree."""
    return " ".join(tokenize(text_value))


def index_chunks(session: Session, chunks: list[Chunk]) -> None:
    """Add chunks to the FTS index. No-op when the index is unavailable."""
    if not chunks or not fts_available(session):
        return
    session.execute(
        text(f"INSERT INTO {CHUNK_FTS_TABLE} (chunk_id, tokens) VALUES (:chunk_id, :tokens)"),
        [{"chunk_id": chunk.id, "tokens": index_text(chunk.text)} for chunk in chunks],
    )


def delete_source_chunks(session: Session, source_id: str) -> None:
    """Drop every indexed row belonging to a source."""
    if not fts_available(session):
        return
    session.execute(
        text(f"DELETE FROM {CHUNK_FTS_TABLE} WHERE chunk_id IN (SELECT id FROM chunks WHERE source_id = :source_id)"),
        {"source_id": source_id},
    )


def index_row_count(session: Session) -> int:
    if not fts_available(session):
        return 0
    return int(session.execute(text(f"SELECT count(*) FROM {CHUNK_FTS_TABLE}")).scalar_one())


def fts_search(
    session: Session,
    *,
    query_tokens: list[str],
    limit: int,
    tenant_id: str,
    entity_slugs: list[str] | None = None,
    source_tree_slug: str | None = None,
    source_language: str | None = None,
    include_und: bool = False,
) -> list[SearchResult]:
    """Rank chunks by FTS5's native BM25, filtered in the same statement.

    Filters are applied inside the query rather than to an already-truncated
    result set -- otherwise a selective filter (a rare entity, a minority
    language) would silently lose recall by discarding most of a top-N that
    was chosen before the filter ran.
    """

    if not query_tokens:
        return []

    weighted: ColumnElement[float] = literal_column(f"bm25({CHUNK_FTS_TABLE}) * ({_WEIGHT_SQL})")

    statement = (
        select(Chunk, Source, weighted.label("weighted_score"))
        .select_from(_fts)
        .join(Chunk, Chunk.id == literal_column(f"{CHUNK_FTS_TABLE}.chunk_id"))
        .join(Source, Source.id == Chunk.source_id)
        .where(text(f"{CHUNK_FTS_TABLE} MATCH :fts_match").bindparams(fts_match=build_match(query_tokens)))
    )

    statement, matchable = apply_source_filters(
        statement,
        session=session,
        tenant_id=tenant_id,
        entity_slugs=entity_slugs,
        source_tree_slug=source_tree_slug,
        source_language=source_language,
        include_und=include_und,
    )
    if not matchable:
        return []

    statement = statement.order_by(literal_column("weighted_score")).limit(limit)

    # bm25() is negative, so negate to keep the public convention that a
    # higher score means a better match.
    return [result_from(source, chunk, -float(score)) for chunk, source, score in session.execute(statement).all()]
