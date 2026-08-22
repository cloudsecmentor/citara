#!/usr/bin/env python3
"""Measure retrieval quality across search modes.

Without this, every retrieval change is an unfalsifiable claim. Run it before
and after a change and compare.

    uv run python scripts/eval_retrieval.py --db sqlite:///../citara-data/citara.db
    uv run python scripts/eval_retrieval.py --out baseline.json
    uv run python scripts/eval_retrieval.py --compare baseline.json

Ground truth comes from two places, and a query may use either or both:

1. `relevant_source_titles` in the query file -- substrings matched against
   source titles. Any chunk of a matching source counts as relevant. This is
   free, needs no judging pass, and suits "known item" queries where the
   answer is a specific known episode.
2. `qrels.json` -- per-(query, chunk) graded judgments (0 irrelevant,
   1 related, 2 directly relevant). Populate it with `--judge`, then edit by
   hand; hand edits are never overwritten, only unjudged pairs are added.

POOLING CAVEAT: chunks that no evaluated system ever returns are never judged
and count as irrelevant. Recall is therefore recall *over the judged pool*,
not over the corpus. Re-run `--judge` after a retrieval change so newly
surfaced chunks get judged rather than silently scoring zero.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_QUERIES = REPO / "tests" / "eval" / "queries.json"
DEFAULT_QRELS = REPO / "tests" / "eval" / "qrels.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="DATABASE_URL override. Must be applied before citara is imported.")
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--qrels", type=Path, default=DEFAULT_QRELS)
    parser.add_argument("--modes", default="keyword,vector,hybrid")
    parser.add_argument("-k", "--limit", type=int, default=10, help="Cutoff for recall/nDCG/MRR (default 10).")
    parser.add_argument("--only", help="Run a single query id.")
    parser.add_argument("--out", type=Path, help="Write results JSON here.")
    parser.add_argument("--compare", type=Path, help="Compare against a previously written results JSON.")
    parser.add_argument("--judge", action="store_true", help="Grade unjudged pooled pairs with an LLM and update qrels.")
    parser.add_argument("--judge-model", default="gpt-4o-mini")
    parser.add_argument("--judge-limit", type=int, default=400, help="Max pairs to judge in one run (cost guard).")
    parser.add_argument("--verbose", action="store_true", help="Print per-query results.")
    return parser.parse_args()


args = _parse_args()

# Settings bind at import time (config.py is a frozen dataclass reading
# os.getenv), so DATABASE_URL has to be set before citara is imported.
if args.db:
    os.environ["DATABASE_URL"] = args.db

if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from sqlalchemy import select  # noqa: E402

from citara.core.db import SessionLocal  # noqa: E402
from citara.core.models import Chunk, Source  # noqa: E402
from citara.core.retrieval.context_pack import search_by_mode  # noqa: E402

# Grade >= this counts as relevant for recall and MRR.
RELEVANT_GRADE = 1


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------
def _gain(grade: int) -> float:
    return float(2**grade - 1)


def ndcg_at_k(ranked_grades: list[int], all_grades: list[int], k: int) -> float:
    dcg = sum(_gain(g) / math.log2(i + 2) for i, g in enumerate(ranked_grades[:k]))
    ideal = sorted(all_grades, reverse=True)[:k]
    idcg = sum(_gain(g) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def recall_at_k(ranked_grades: list[int], total_relevant: int, k: int) -> float:
    if not total_relevant:
        return 0.0
    hits = sum(1 for g in ranked_grades[:k] if g >= RELEVANT_GRADE)
    return hits / min(total_relevant, k)


def mrr_at_k(ranked_grades: list[int], k: int) -> float:
    for i, g in enumerate(ranked_grades[:k]):
        if g >= RELEVANT_GRADE:
            return 1.0 / (i + 1)
    return 0.0


# --------------------------------------------------------------------------
# ground truth
# --------------------------------------------------------------------------
def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def resolve_title_sources(session, titles: list[str]) -> set[str]:
    """Map title substrings to source ids. Substrings keep the query file readable."""
    found: set[str] = set()
    for fragment in titles:
        rows = session.execute(select(Source.id).where(Source.title.contains(fragment))).scalars().all()
        if not rows:
            print(f"  ! no source matches title fragment {fragment!r}", file=sys.stderr)
        found.update(rows)
    return found


def grade_for(chunk_id: str, source_id: str, *, qrels_q: dict, known_sources: set[str]) -> int:
    if chunk_id in qrels_q:
        return int(qrels_q[chunk_id])
    if source_id in known_sources:
        return 2
    return 0


# --------------------------------------------------------------------------
# LLM judging
# --------------------------------------------------------------------------
JUDGE_SYSTEM = (
    "You grade search results for a corpus of biblical-studies podcast transcripts. "
    "Given a QUERY and a PASSAGE, reply with exactly one digit:\n"
    "2 = the passage directly addresses the query and would be worth citing\n"
    "1 = the passage is related and gives useful context, but does not directly address it\n"
    "0 = the passage is not relevant\n"
    "Reply with the digit only."
)


def judge_pairs(pairs: list[tuple[str, str, str, str]], *, model: str) -> dict[tuple[str, str], int]:
    """Grade (query_id, query, chunk_id, text) pairs. Returns {(qid, chunk_id): grade}."""
    import httpx

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("--judge needs OPENAI_API_KEY. Try: set -a; . ./.env; set +a")

    out: dict[tuple[str, str], int] = {}
    with httpx.Client(timeout=60) as client:
        for n, (qid, query, chunk_id, text) in enumerate(pairs, start=1):
            response = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 2,
                    "messages": [
                        {"role": "system", "content": JUDGE_SYSTEM},
                        {"role": "user", "content": f"QUERY: {query}\n\nPASSAGE:\n{text[:4000]}"},
                    ],
                },
            )
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"].strip()
            grade = int(raw[0]) if raw[:1].isdigit() and int(raw[0]) in (0, 1, 2) else 0
            out[(qid, chunk_id)] = grade
            if n % 25 == 0:
                print(f"  judged {n}/{len(pairs)}", file=sys.stderr)
    return out


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------
def run_mode(session, queries: list[dict], mode: str, k: int) -> tuple[list[dict], list[float]]:
    runs, latencies = [], []
    for q in queries:
        start = time.perf_counter()
        results = search_by_mode(
            session,
            query=q["query"],
            limit=k,
            mode=mode,
            # 'any' keeps the language filter from confounding the comparison.
            language_policy="any",
        )
        latencies.append(time.perf_counter() - start)
        runs.append({"id": q["id"], "results": results})
    return runs, latencies


def main() -> None:
    k = args.limit
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    queries = load_json(args.queries, None)
    if queries is None:
        raise SystemExit(f"No query file at {args.queries}")
    if args.only:
        queries = [q for q in queries if q["id"] == args.only]
        if not queries:
            raise SystemExit(f"No query with id {args.only!r}")
    qrels: dict[str, dict[str, int]] = load_json(args.qrels, {})

    with SessionLocal() as session:
        known: dict[str, set[str]] = {q["id"]: resolve_title_sources(session, q.get("relevant_source_titles", [])) for q in queries}

        all_runs = {mode: run_mode(session, queries, mode, k) for mode in modes}

        if args.judge:
            pool: dict[tuple[str, str], tuple[str, str, str, str]] = {}
            for runs, _ in all_runs.values():
                for run in runs:
                    qtext = next(q["query"] for q in queries if q["id"] == run["id"])
                    for r in run["results"]:
                        key = (run["id"], r.chunk_id)
                        if r.chunk_id not in qrels.get(run["id"], {}):
                            pool[key] = (run["id"], qtext, r.chunk_id, r.text)
            pending = list(pool.values())[: args.judge_limit]
            if not pending:
                print("Nothing new to judge.")
            else:
                approx_tokens = sum(len(p[3][:4000]) // 4 + 120 for p in pending)
                print(f"Judging {len(pending)} pairs with {args.judge_model} (~{approx_tokens:,} input tokens).")
                for (qid, chunk_id), grade in judge_pairs(pending, model=args.judge_model).items():
                    qrels.setdefault(qid, {})[chunk_id] = grade
                args.qrels.parent.mkdir(parents=True, exist_ok=True)
                args.qrels.write_text(json.dumps(qrels, indent=2, sort_keys=True) + "\n")
                print(f"Wrote {args.qrels}")

        # Total relevant per query, over everything judged plus known-item sources.
        totals: dict[str, int] = {}
        for q in queries:
            qid = q["id"]
            judged = sum(1 for g in qrels.get(qid, {}).values() if g >= RELEVANT_GRADE)
            unjudged_known = 0
            if known[qid]:
                # Chunks of a known-item source that no judgment covers yet.
                # Counting them here keeps a hand-graded 0 authoritative over
                # the blanket "same source => relevant" assumption.
                chunk_ids = session.execute(select(Chunk.id).where(Chunk.source_id.in_(known[qid]))).scalars().all()
                unjudged_known = sum(1 for c in chunk_ids if c not in qrels.get(qid, {}))
            totals[qid] = judged + unjudged_known

        report: dict[str, dict] = {}
        for mode in modes:
            runs, latencies = all_runs[mode]
            per_query = {}
            for run in runs:
                qid = run["id"]
                grades = [grade_for(r.chunk_id, r.source_id, qrels_q=qrels.get(qid, {}), known_sources=known[qid]) for r in run["results"]]
                all_grades = list(qrels.get(qid, {}).values()) + [2] * (
                    totals[qid] - sum(1 for g in qrels.get(qid, {}).values() if g >= RELEVANT_GRADE)
                )
                per_query[qid] = {
                    "recall": recall_at_k(grades, totals[qid], k),
                    "mrr": mrr_at_k(grades, k),
                    "ndcg": ndcg_at_k(grades, all_grades, k),
                    "n": len(grades),
                }
            report[mode] = {
                "recall": statistics.mean(v["recall"] for v in per_query.values()) if per_query else 0.0,
                "mrr": statistics.mean(v["mrr"] for v in per_query.values()) if per_query else 0.0,
                "ndcg": statistics.mean(v["ndcg"] for v in per_query.values()) if per_query else 0.0,
                "p50_ms": statistics.median(latencies) * 1000,
                "p95_ms": (sorted(latencies)[int(len(latencies) * 0.95) - 1] if len(latencies) > 1 else latencies[0]) * 1000,
                "per_query": per_query,
            }

    _print_report(report, modes, k, queries)

    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nWrote {args.out}")
    if args.compare:
        _print_comparison(load_json(args.compare, {}), report, modes)


def _print_report(report: dict, modes: list[str], k: int, queries: list[dict]) -> None:
    print(f"\n{len(queries)} queries · k={k}\n")
    print(f"{'mode':10s} {'recall@k':>9s} {'MRR':>7s} {'nDCG@k':>8s} {'p50':>9s} {'p95':>9s}")
    print("-" * 58)
    for mode in modes:
        r = report[mode]
        print(f"{mode:10s} {r['recall']:9.3f} {r['mrr']:7.3f} {r['ndcg']:8.3f} {r['p50_ms']:8.0f}ms {r['p95_ms']:8.0f}ms")

    if args.verbose:
        print("\nper query (nDCG@k):")
        header = f"{'query':44s}" + "".join(f"{m:>10s}" for m in modes)
        print(header)
        print("-" * len(header))
        for q in queries:
            row = f"{q['query'][:42]:44s}"
            for mode in modes:
                row += f"{report[mode]['per_query'][q['id']]['ndcg']:10.3f}"
            print(row)


def _print_comparison(before: dict, after: dict, modes: list[str]) -> None:
    print(f"\nvs baseline:\n{'mode':10s} {'recall@k':>18s} {'nDCG@k':>18s}")
    print("-" * 48)
    for mode in modes:
        if mode not in before:
            continue
        rb, ra = before[mode]["recall"], after[mode]["recall"]
        nb, na = before[mode]["ndcg"], after[mode]["ndcg"]
        print(f"{mode:10s} {rb:7.3f} -> {ra:7.3f} {nb:7.3f} -> {na:7.3f}")


if __name__ == "__main__":
    main()
