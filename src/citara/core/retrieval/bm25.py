from __future__ import annotations

import math
from collections import Counter

# Okapi BM25 defaults. k1 controls term-frequency saturation (how quickly
# repeated occurrences stop adding value); b controls how strongly document
# length is normalized away. These are the standard values and are what
# SQLite's FTS5 `bm25()` uses too, so the pure-Python fallback and the
# indexed path rank comparably.
K1 = 1.2
B = 0.75


def idf(total_docs: int, doc_freq: int) -> float:
    """Robertson/Sparck-Jones IDF with the +0.5 smoothing, floored at zero.

    The floor matters: without it, a term appearing in more than half the
    corpus gets a *negative* weight, so a document could be penalized for
    containing a query term. Lucene floors it the same way.

    This is the piece the previous raw term-count scorer was missing
    entirely, and why "the" (135 occurrences) outranked chunks that actually
    contained "exodus" and "covenant".
    """
    if total_docs <= 0 or doc_freq <= 0:
        return 0.0
    return max(0.0, math.log(1.0 + (total_docs - doc_freq + 0.5) / (doc_freq + 0.5)))


def score_document(
    query_tokens: list[str],
    doc_tokens: list[str],
    *,
    doc_freqs: dict[str, int],
    total_docs: int,
    avg_doc_len: float,
    k1: float = K1,
    b: float = B,
) -> float:
    if not doc_tokens or avg_doc_len <= 0:
        return 0.0

    counts = Counter(doc_tokens)
    doc_len = len(doc_tokens)
    norm = k1 * (1.0 - b + b * (doc_len / avg_doc_len))

    total = 0.0
    for token in set(query_tokens):
        freq = counts.get(token, 0)
        if not freq:
            continue
        total += idf(total_docs, doc_freqs.get(token, 0)) * (freq * (k1 + 1.0)) / (freq + norm)
    return total


def corpus_stats(documents: list[list[str]], query_tokens: list[str]) -> tuple[dict[str, int], int, float]:
    """Document frequencies (for query terms only), doc count, and mean length.

    Only query terms need document frequencies, so this stays O(corpus) in
    time but O(query) in memory rather than building a full vocabulary.
    """
    wanted = set(query_tokens)
    doc_freqs: dict[str, int] = dict.fromkeys(wanted, 0)
    total_len = 0
    for tokens in documents:
        total_len += len(tokens)
        for token in wanted.intersection(tokens):
            doc_freqs[token] += 1
    count = len(documents)
    return doc_freqs, count, (total_len / count if count else 0.0)
