#!/usr/bin/env python3
"""Recompute stored chunk embeddings in place.

This does NOT re-ingest anything. Sources, chunks, chunk IDs, entity links,
source preferences (`retrieval_weight`), taxonomy metadata, and ingestion job
history are all left untouched -- only rows in the `embeddings` table are
rewritten, using the currently configured `EMBEDDING_PROVIDER`.

Use it when stored vectors were produced by a different tokenizer or model
than the one now answering queries. Two cases:

1. `EMBEDDING_PROVIDER=local` corpora embedded before the Unicode tokenizer
   fix (v0.1.0). `tokenize()` no longer treats the apostrophe as a word
   character, so "god's" became ["god", "s"] and stored vectors drifted from
   what a query now computes -- badly on contraction-heavy transcripts.
2. Switching embedding provider or model, which also changes `dimensions`.

Dry run first (the default -- writing requires `--yes`):

    uv run python scripts/reembed_corpus.py --dry-run

The dry run reports cosine drift between each stored vector and a freshly
computed one, so you can see whether a re-embed is warranted before paying
for it.
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from citara.core.config import settings
from citara.core.db import SessionLocal, init_db
from citara.core.embeddings.providers import EmbeddingProvider, get_embedding_provider
from citara.core.models import Chunk, Embedding
from citara.core.retrieval.vector import cosine_similarity

# Below this, a stored vector and a freshly computed one disagree enough to
# matter for ranking. Exact float reproduction is not expected.
DRIFT_THRESHOLD = 0.999


@dataclass
class ReembedStats:
    chunks_seen: int = 0
    updated: int = 0
    created: int = 0
    unchanged: int = 0
    other_model_rows: int = 0
    pruned: int = 0
    similarities: list[float] = field(default_factory=list)

    @property
    def drifted(self) -> int:
        return sum(1 for value in self.similarities if value < DRIFT_THRESHOLD)

    def render(self, *, applied: bool) -> str:
        width = 28
        rewrote = "Rewrote embeddings:" if applied else "Would rewrite embeddings:"
        created = "Missing, created:" if applied else "Missing, would create:"
        lines = [
            f"{'Chunks examined:':<{width}}{self.chunks_seen}",
            f"{rewrote:<{width}}{self.updated}",
            f"{created:<{width}}{self.created}",
            f"{'Already identical:':<{width}}{self.unchanged}",
        ]
        if self.similarities:
            mean = sum(self.similarities) / len(self.similarities)
            lines.extend(
                [
                    "",
                    f"Cosine(stored, fresh) mean: {mean:.4f}  min: {min(self.similarities):.4f}",
                    f"Vectors drifted (< {DRIFT_THRESHOLD}):   {self.drifted} of {len(self.similarities)}",
                ]
            )
        if self.other_model_rows:
            lines.extend(
                [
                    "",
                    f"NOTE: {self.other_model_rows} embedding row(s) belong to a different model.",
                    "      Retrieval ignores them -- `vector_search` loads only the active model's",
                    "      vectors -- so they are safe to leave in place, and keeping them lets you",
                    "      switch back without re-embedding. They do occupy space:",
                    "      re-run with --prune-other-models to delete them.",
                ]
            )
        if self.pruned:
            lines.append(f"Pruned other-model rows: {self.pruned}")
        return "\n".join(lines)


def _chunk_batches(session: Session, *, tenant_id: str, batch_size: int, source_id: str | None) -> Iterator[list[Chunk]]:
    """Yield chunks in keyset-paginated batches.

    Keyset pagination on the primary key rather than one big SELECT (a real
    corpus is tens of thousands of chunks, which should not all be resident)
    and rather than a streaming cursor, which would be held open across the
    flushes that `reembed` performs between batches.
    """

    last_id: str | None = None
    while True:
        statement = select(Chunk).where(Chunk.tenant_id == tenant_id).order_by(Chunk.id).limit(batch_size)
        if source_id:
            statement = statement.where(Chunk.source_id == source_id)
        if last_id is not None:
            statement = statement.where(Chunk.id > last_id)

        batch = list(session.execute(statement).scalars())
        if not batch:
            return
        yield batch
        last_id = batch[-1].id


def _existing_by_chunk(session: Session, *, tenant_id: str, chunk_ids: list[str]) -> dict[str, list[Embedding]]:
    statement = select(Embedding).where(Embedding.tenant_id == tenant_id, Embedding.chunk_id.in_(chunk_ids))
    grouped: dict[str, list[Embedding]] = defaultdict(list)
    for embedding in session.execute(statement).scalars():
        grouped[embedding.chunk_id].append(embedding)
    return grouped


def reembed(
    session: Session,
    *,
    provider: EmbeddingProvider,
    tenant_id: str,
    batch_size: int,
    apply: bool,
    source_id: str | None = None,
    prune_other_models: bool = False,
    progress: bool = False,
) -> ReembedStats:
    stats = ReembedStats()

    for batch in _chunk_batches(session, tenant_id=tenant_id, batch_size=batch_size, source_id=source_id):
        vectors = provider.embed_texts([chunk.text for chunk in batch])
        existing = _existing_by_chunk(session, tenant_id=tenant_id, chunk_ids=[chunk.id for chunk in batch])

        for chunk, raw_vector in zip(batch, vectors, strict=True):
            stats.chunks_seen += 1
            vector = [float(value) for value in raw_vector]
            rows = existing.get(chunk.id, [])
            same_model = [row for row in rows if row.embedding_model == provider.model]
            other_model = [row for row in rows if row.embedding_model != provider.model]
            stats.other_model_rows += len(other_model)

            if prune_other_models and apply:
                for row in other_model:
                    session.delete(row)
                    stats.pruned += 1

            if not same_model:
                stats.created += 1
                if apply:
                    session.add(
                        Embedding(
                            id=f"emb_{uuid4().hex}",
                            tenant_id=tenant_id,
                            source_id=chunk.source_id,
                            chunk_id=chunk.id,
                            embedding_model=provider.model,
                            dimensions=len(vector),
                            vector=vector,
                        )
                    )
                continue

            # Keep one row per (chunk, model); duplicates would double-count.
            primary, *duplicates = same_model
            stored = [float(value) for value in primary.vector]
            similarity = cosine_similarity(stored, vector) if len(stored) == len(vector) else 0.0
            stats.similarities.append(similarity)

            if similarity >= DRIFT_THRESHOLD and len(stored) == len(vector) and not duplicates:
                stats.unchanged += 1
                continue

            stats.updated += 1
            if apply:
                primary.vector = vector
                primary.dimensions = len(vector)
                primary.embedding_model = provider.model
                for row in duplicates:
                    session.delete(row)
                    stats.pruned += 1

        if apply:
            # Commit per batch, not once at the end. A full-corpus re-embed is
            # hundreds of network calls over many minutes; a single trailing
            # commit means one transient failure discards all of it.
            #
            # This is only safe because `vector_search` filters by the active
            # embedding model. A partially committed run leaves a corpus where
            # some chunks carry the new model and some the old, and retrieval
            # searches only the new ones -- a smaller index, never a wrong one.
            # Before that filter existed, this would have produced silently
            # corrupt rankings.
            session.commit()

        if progress and stats.chunks_seen % (batch_size * 10) < batch_size:
            print(f"  {stats.chunks_seen:,} chunks processed", flush=True)

    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recompute stored chunk embeddings in place (does not re-ingest)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change and exit (default unless --yes)")
    parser.add_argument("--yes", action="store_true", help="Required to actually write embeddings")
    parser.add_argument("--tenant-id", default=settings.default_tenant_id, help="Tenant to re-embed")
    parser.add_argument("--source-id", default=None, help="Limit to a single source")
    parser.add_argument("--batch-size", type=int, default=128, help="Chunks per provider call")
    parser.add_argument(
        "--prune-other-models",
        action="store_true",
        help="Delete embedding rows belonging to a different model. Retrieval already ignores them; this reclaims the space.",
    )
    parser.add_argument("--progress", action="store_true", help="Print progress while running")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    apply = args.yes and not args.dry_run

    provider = get_embedding_provider()
    print(f"Database:  {os.getenv('DATABASE_URL', settings.database_url)}")
    print(f"Provider:  {os.getenv('EMBEDDING_PROVIDER', settings.embedding_provider)} (model {provider.model})")
    print(f"Mode:      {'APPLY (writing)' if apply else 'dry run (no writes)'}\n")

    init_db()
    with SessionLocal() as session:
        stats = reembed(
            session,
            provider=provider,
            tenant_id=args.tenant_id,
            batch_size=args.batch_size,
            apply=apply,
            source_id=args.source_id,
            prune_other_models=args.prune_other_models,
            progress=args.progress,
        )
        if apply:
            session.commit()

    print(stats.render(applied=apply))
    if not apply:
        print("\nNothing was written. Re-run with --yes to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
