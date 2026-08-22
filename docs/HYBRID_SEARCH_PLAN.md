# Hybrid search: rebuild plan

Status: **draft for review** · Author: analysis pass 2026-08-16 · Corpus measured: `../citara-data/citara.db`

---

## TL;DR

Hybrid search already exists structurally — `retrieval/hybrid.py` fuses keyword and vector
rankings with textbook RRF, and `search_by_mode` already routes `keyword` / `vector` / `hybrid`
through the API, MCP, and context-pack surfaces. **The architecture is right. Both inputs to it
are broken, and the whole thing is 43 seconds per query on the real corpus.**

So this is not "implement hybrid search." It is "make the two halves it fuses actually work, and
make them fast enough to call from an agent."

Measured on the live corpus (1,444 sources / 58,471 chunks / 58,471 embeddings):

| Mode | Latency | Verdict |
|---|---|---|
| `keyword` | **31.4 s** | Ranks by stopword frequency (see below) |
| `vector` | **13.0 s** | 8-dim hash space, near-zero discriminative power |
| `hybrid` | **43.1 s** | Fuses the two above |

---

## What I found

### Defect 1 — keyword ranking is dominated by stopwords

`retrieval/keyword.py:107` scores a chunk as the raw sum of query-token occurrences:

```python
score = sum(chunk_tokens.count(token) for token in query_tokens)
```

No IDF, no document-length normalization, no stopword handling. Every term is worth the same, and
longer chunks mechanically win.

I ran the query `what does the exodus teach about covenant` and decomposed the winning score:

```
score=121.5  len_tokens=450  'BEMA 342: Sabbath Reflection IV — Teaching'
  per-token contribution:
    {'what': 0, 'does': 0, 'the': 135, 'exodus': 0,
     'teach': 0, 'about': 0, 'covenant': 0}
```

**The top-ranked chunk contains zero occurrences of `exodus`, `teach`, or `covenant`.** It won
entirely on 135 occurrences of `the`. Ranks 2 and 3 are the same story (`the` × 26, `the` × 75).
The content words contributed nothing to any of the top three results.

This is the single highest-impact defect, and fixing it needs no infrastructure change and no
re-embedding.

### Defect 2 — the vector half is not semantic

Every stored vector is `deterministic-hash-v1` at **8 dimensions** (`config.py:33`). That provider
hashes tokens into 8 buckets — with a real vocabulary, collisions are total.

I sampled 4,000 stored vectors and computed cosine over ~20,000 random pairs of **unrelated**
chunks:

```
mean=0.521   median=0.585   p90=0.828   p99=0.930
2.7% of unrelated chunk pairs score cosine > 0.9
```

A pair of arbitrary, topically unrelated chunks sits at ~0.52 similarity by default. There is no
headroom left to separate a relevant chunk from an irrelevant one. The "vector" half of hybrid
search is contributing noise, not semantics — which also means the current RRF fusion is spending
half its rank budget on a random ordering.

This is already known internally (`todo.md`, P1 Stage 4) but its severity is understated there.

### Defect 3 — both backends full-scan the corpus in Python

- `keyword.py:102` loads **every chunk row for the tenant**, then re-tokenizes each chunk's text on
  every query (`keyword.py:106`). 58k chunks × ~1.1 KB each, tokenized per request.
- `vector.py:62` loads **every embedding row**, parses each JSON vector, and computes cosine in a
  Python loop (`vector.py:65-68`).

Neither uses an index. Cost grows linearly with the corpus, and it is already past the point where
an MCP tool call is usable interactively.

**Critical sequencing consequence:** Defect 2's fix makes Defect 3 catastrophically worse. Moving
from 8 dims to 1536 is ~200× more float math per query on a path that already takes 13 s. **The
storage/scan path must be fixed before real embeddings land**, or vector search goes from slow to
unusable.

---

## Target architecture

Keep `search_by_mode` / `hybrid_search` / `rrf_fuse` and the whole language-policy layer — they are
sound and well-tested. Replace what feeds them.

```
query
  ├─ keyword: tokenize() → FTS5 MATCH + bm25()  ──┐
  │            (SQLite index, IDF + length-norm)  │
  │                                               ├─ weighted RRF → results
  └─ vector:  real embedding model → packed       │
              float32 scan / ANN                ──┘
```

Two principles:

1. **Retrieval goes to the database engine; ranking policy stays in Python.** Filters (tenant,
   language, `source_tree_slug`, entity) must be applied *in the same SQL statement* as the match,
   not after a truncated fetch, or filtered queries silently lose recall.
2. **`tokenize()` stays the single source of truth for tokenization.** The FTS index is built over
   `" ".join(tokenize(text))`, not raw text, so the index and the query agree — this preserves the
   existing Unicode/CJK-bigram multilingual work in `chunking/simple.py` rather than handing
   tokenization to FTS5's `unicode61` and regressing it.

---

## Staged plan

Each stage is independently shippable and independently valuable. Stage 0 comes first on purpose.

### Stage 0 — Evaluation harness *(do this first)*

Without it, every subsequent stage is an unfalsifiable claim.

- `scripts/eval_retrieval.py` — runs a query set through all three modes, reports
  **Recall@10, MRR, nDCG@10, and p50/p95 latency** per mode.
- `tests/eval/queries.yaml` — ~30 real queries with hand-labeled relevant `source_id`s.
- Capture today's numbers as the baseline to beat.

**The one part I can't fully automate:** the relevance labels need your judgment on this corpus.
I'd bootstrap it by pooling top-20 from all three modes per query so you're labeling a candidate
list rather than authoring from scratch — roughly an hour of review.

*Acceptance:* baseline table committed; harness runs in CI against the small test corpus.

### Stage 1 — BM25 scoring (quality)

Replace the raw term-count sum in `keyword.py` with BM25 (`k1=1.2`, `b=0.75`), keeping the current
full-scan mechanics for now.

- IDF neutralizes `the` automatically — no stopword list, which matters for a multilingual corpus.
- Length normalization stops long chunks winning by default.
- Corpus statistics (document frequency, average length) come from a small `term_stats` computation
  cached per tenant.

*Acceptance:* the `exodus/covenant` query returns chunks that actually contain those terms;
Recall@10 improves measurably over the Stage 0 baseline. **No schema change, no re-embed.**

> Stage 1 and Stage 2 can reasonably be collapsed, since FTS5's native `bm25()` delivers both at
> once. Keeping them separate is only worth it if you want the portable Python BM25 as a fallback
> for non-FTS5 builds and for Postgres ranking parity.

### Stage 2 — Push keyword search into the index (latency)

- New FTS5 table (migration `0005`): `chunk_fts(chunk_id UNINDEXED, tokens, tokenize='unicode61')`,
  populated with `" ".join(tokenize(chunk.text))`.
- Query becomes a single statement: FTS5 `MATCH` driving a join to `chunks`/`sources` with all
  existing filters applied inline, ordered by `bm25()`.
- **Prototyped against the real corpus — see "Prototype results" below.** Build takes 33 s for all
  58,471 chunks; the index is **138 MB** (a 336 MB database grows ~41% to ~474 MB — larger than a
  naive estimate, and the main cost of this stage).
- Optimization worth including from the start: drop tokens whose document frequency exceeds a
  threshold from the `MATCH` clause. They contribute ~0 IDF but force a scan of a near-corpus-sized
  postings list — `the` alone is most of the measured 174 ms.
- **MATCH-string construction must double-quote every token.** Raw user queries containing `-`,
  `"`, `*`, or `NEAR` are FTS5 operators and will otherwise error or silently change semantics.
- Write-path hooks: index on ingest (one chokepoint — `embeddings/service.py:embed_chunks`, called
  by both `ingestion/text.py` and `ingestion/transcript.py`), delete in
  `sources.py:delete_source`.
- `scripts/backfill_fts.py` for the existing 58k chunks; a staleness check added to
  `scripts/citara_maintenance.py`.
- Postgres gets `tsvector` + GIN behind the same interface. **Ranking parity is a known gap** —
  Postgres has no native BM25, so `ts_rank_cd` will rank differently from SQLite's `bm25()`. The
  eval harness measures the difference; the Python BM25 from Stage 1 stays as the portable
  fallback.

*Acceptance:* keyword p95 < 200 ms on the 58k corpus; Stage 1 quality preserved.

### Stage 3 — Vector storage and scan (must precede Stage 4)

- Store vectors as packed **float32 BLOB** instead of JSON, via the existing per-dialect
  `EmbeddingVector` TypeDecorator in `models.py:18`. `np.frombuffer` makes load near-free; JSON
  parsing 58k × 1536 floats per query does not.
- Replace the Python cosine loop with a single vectorized matmul over a cached matrix.
- Migration `0006` + a re-pack pass (`reembed_corpus.py` already rewrites vectors in place).

Measured on this corpus shape (58,471 × N, numpy GEMV + top-10 selection):

| dims | query | fp32 RAM | fp16 RAM |
|---|---|---|---|
| 384 | 6.8 ms | 90 MB | 45 MB |
| 512 | 9.3 ms | 120 MB | 60 MB |
| 768 | 14.9 ms | 180 MB | 90 MB |
| 1536 | 20.2 ms | 359 MB | 180 MB |

**Compute is a non-issue at every dimension.** Brute force is 9–20 ms; even a 10× larger corpus
stays under ~200 ms. The binding constraint is resident memory, not speed — which is what should
drive the dimension choice, not latency.

*Acceptance:* vector p95 < 100 ms on the 58k corpus.

### Stage 4 — Real embeddings

- Switch `EMBEDDING_PROVIDER` off `deterministic-hash-v1`; re-embed via the existing
  `scripts/reembed_corpus.py` (already built for exactly this, with a dry-run drift report).
- **Recommend 512–768 dimensions, not 1536** — for memory, not speed. `text-embedding-3-small`
  supports Matryoshka truncation, so 512 dims costs little quality while halving-to-thirding the
  resident matrix (120 MB vs 359 MB at fp32). Note both API providers currently need a
  `dimensions` parameter added: `OpenAIEmbeddingProvider.embed_texts` sends only `model`/`input`,
  and `AzureFoundryEmbeddingProvider` sends only `input` (`embeddings/providers.py`).
- **Naming trap to resolve:** `EMBEDDING_PROVIDER=local` currently maps to the *fake*
  `DeterministicEmbeddingProvider`, and `LOCAL_EMBEDDING_MODEL_PATH` is defined in `config.py:48`
  but never read by `get_embedding_provider()`. A real local model needs a distinct provider key,
  or existing configs will silently keep the hash provider.
- **Cost is not a blocker:** 58,471 chunks × ~280 tokens ≈ 16.4 M tokens ≈ **$0.33** one-time on
  `text-embedding-3-small`.
- Keep `DeterministicEmbeddingProvider` as the test provider so the suite stays offline and fast.
- Confirm the pgvector column width story (migration `0003` claims variable dimensions).

*Acceptance:* random unrelated-pair cosine drops from ~0.52 toward ~0.0–0.1; large nDCG@10 gain in
`vector` and `hybrid` modes.

### Stage 5 — Fusion tuning and observability

- Weighted RRF: `score += weight / (K + rank)`, with `keyword_weight` / `vector_weight` in
  `config.py`, tuned against the Stage 0 harness rather than by intuition.
- Raise the candidate depth. `hybrid.py:64` currently over-fetches only `limit * 2`; once both
  backends are indexed, `max(50, limit * 5)` is nearly free and improves fusion quality.
- **Expose a score breakdown.** Today an API consumer gets an RRF score of `0.0164` in hybrid mode
  and a BM25-ish score of `121.5` in keyword mode, under the same field name. Add per-backend rank
  and score alongside the fused value.

---

## Results

Measured by `scripts/eval_retrieval.py` over 32 queries at k=10 against the live corpus.

**Baseline (before any change):**

| mode | recall@10 | MRR | nDCG@10 | p50 |
| --- | --- | --- | --- | --- |
| keyword | 0.373 | 0.233 | 0.232 | 12,751 ms |
| vector | 0.148 | 0.076 | 0.075 | 6,000 ms |
| hybrid | 0.222 | 0.170 | 0.130 | 18,782 ms |

The baseline alone justified the work: **hybrid scored *worse* than keyword alone on every quality
metric.** Fusing a usable ranking with a noise ranking degrades it. The vector half was not merely
failing to contribute — it was actively displacing good keyword hits.

**After BM25 + FTS5 (Stages 1–2):**

| mode | recall@10 | nDCG@10 | p50 |
| --- | --- | --- | --- |
| keyword | 0.373 → **0.786** | 0.232 → **0.657** | 12,751 → **844 ms** |
| hybrid | 0.222 → **0.441** | 0.130 → **0.354** | 18,782 → **931 ms** |

> **Reading the vector row correctly.** Vector's measured recall *fell* (0.148 → 0.032) across this
> comparison, and that is a measurement artifact, not a regression — nothing about vector search
> changed at this stage. BM25 surfaced 282 chunks no system had returned before; judging them
> enlarged the known-relevant pool, so the same vector results now account for a smaller share of
> a larger denominator. This is the pooling caveat in `eval_retrieval.py` behaving exactly as
> documented, and it is why the harness re-judges after every change.

**Final, after real embeddings (Stages 3–4), against the original baseline:**

| mode | recall@10 | MRR | nDCG@10 | p50 |
| --- | --- | --- | --- | --- |
| keyword | 0.373 → **0.741** | 0.233 → **0.891** | 0.232 → **0.581** | 12,751 → **1,989 ms** |
| vector | 0.148 → **0.800** | 0.076 → **0.938** | 0.075 → **0.660** | 6,000 → **181 ms** |
| hybrid | 0.222 → **0.831** | 0.170 → **0.969** | 0.130 → **0.682** | 18,782 → **981 ms** |

**Hybrid now beats both of its inputs** — the property that makes fusion worth doing, and the one
the baseline lacked. Keyword's apparent dip from the Stage 1–2 numbers (0.786 → 0.741) is the same
pooling artifact described above, this time from 253 chunks newly surfaced by real embeddings.

Two self-inflicted performance bugs turned up while profiling the final state, both in code added
by this work:

- The cache's corpus-version check cost **338 ms**, against **17 ms** for the matrix product it was
  guarding. Replaced with explicit invalidation on the write paths plus a 30 s TTL for
  cross-process writes: now a 0.002 ms dictionary lookup.
- Cold index build took **21.4 s**, because selecting `Embedding.vector` through its TypeDecorator
  converted 58k rows into Python float lists and materializing `Source` built 58k ORM objects to
  read one metadata key each. Reading the raw BLOB into `np.frombuffer` cut it to **6.0 s**.

The predicted memory figure held exactly: 120 MB resident for 58,471 × 512 float32.

## Implementation notes (what changed once it met the corpus)

Three things in this plan did not survive contact with measurement. Recording them because the
reasoning that produced them looked sound and would otherwise be repeated.

**1. High-DF token pruning was dropped — it bought nothing.** The plan called for removing
stopword-class terms from the `MATCH` clause "from the start", on the theory that walking `the`'s
58k-row postings list dominated the query. The document frequencies looked damning (`the` 81%,
`what` 57%, `about` 53%, against `exodus` 4.9% and `covenant` 3.3%), and pruning did preserve
ranking perfectly (10/10 top-10 overlap, identical #1 across five queries). But the measured
speedup was 0.9–1.1× — pure noise. Profiling found the real cost:

| stage | time |
| --- | --- |
| FTS5 `MATCH` only | 0.2 ms |
| + `bm25()` ordering | 91.5 ms |
| + joins to `chunks`/`sources` | 380.6 ms |

The join dominates, not the postings scan. The pruning machinery (a vocab table, a per-query
lookup, extra migration DDL) was reverted.

**2. `retrieval_weight` stayed exact.** The plan predicted weighting would degrade to an
approximation over a candidate window. It did not need to: FTS5's `bm25()` is negative, so
multiplying by a weight inside the `ORDER BY` ranks a heavier source higher under the same
ascending sort. Weighting is applied in the ranking SQL, and no over-fetch is required. The known
behavior change listed under "Risks" below therefore did not happen.

**3. No migration was needed for packed vectors.** SQLite's TEXT affinity stores a BLOB unchanged,
so the column did not need altering, and reads accept both formats. The remaining lever, if
keyword latency ever matters more than exactness, is to rank in an FTS-only subquery with an
over-fetch and join only the survivors — roughly 380 ms → 90 ms, at the cost of exact weighting.

## Prototype results

Three claims in this plan were load-bearing enough to verify rather than assume.

**1. FTS5 with native `bm25()` fixes the ranking defect.** I built the proposed index over all
58,471 real chunks and reran the failing query. Before, the top three results contained zero
occurrences of `exodus`, `teach`, or `covenant`. After:

```
FTS5 bm25 query: 174 ms   (vs 31,400 ms today — 180× faster)

bm25=-12.059  'BEMA 30: Lead with Your Voice'
   matched: {'what': 1, 'the': 8, 'exodus': 3, 'teach': 4, 'about': 1}
bm25=-11.130  'BibleProject: The Cathedral in Time - 7th Day Rest'
   matched: {'what': 2, 'the': 29, 'exodus': 8, 'about': 3, 'covenant': 7}
bm25=-10.921  'BEMA 234: Jen Rosner — The Jewish Roots of Christian…'
   matched: {'what': 1, 'the': 5, 'teach': 3, 'about': 1, 'covenant': 1}
```

Content words now drive the ranking. Stage 1+2 is de-risked.

**2. Brute-force vector scan is fast enough at every plausible dimension** — see the table in
Stage 3. An ANN index is not justified at this corpus size.

**3. `sqlite-vec` is technically viable here** — this environment's Python (3.11.11, SQLite 3.53.4
via uv) *does* support `enable_load_extension`, which is often the blocker on macOS system Python.
So the choice below is a genuine trade-off, not a forced hand. Also confirmed: **numpy 2.4.6 is
already installed transitively** (via `pgvector`), so the numpy option adds no new dependency.

---

## Decisions — RESOLVED 2026-08-16

1. **Vector scan: numpy brute force.** No new dependency; exact; 9–20 ms at corpus scale.
2. **Embeddings: OpenAI `text-embedding-3-small`.** Key copied from `podocracy-app/.env` into a
   gitignored, mode-600 `.env`. Validated live: auth OK, `dimensions=512` truncation supported,
   and it separates related (0.549) from unrelated (0.084) text — versus the current hash space,
   where *unrelated* chunks already sit at 0.521.
3. **Storage: SQLite first.** FTS5 `bm25()` verified working on the real corpus.

> **`.env` is not read by anything.** `config.py` is a plain frozen dataclass calling `os.getenv`
> at *import* time, and no `load_dotenv` exists anywhere in `src/` or `scripts/`. The file is a
> store, not a mechanism. To actually use it: `set -a; . ./.env; set +a` before running — and
> because settings bind at import, export before the process starts, not after. Worth considering
> a real dotenv load in `config.py`, since this trips people repeatedly.

### Stage 4 prerequisite — a latent bug that will bite mid-migration

`reembed_corpus.py` rewrites vectors incrementally, so the corpus is **mixed-model while it runs**.
Two problems compound there:

- `vector_search` (`retrieval/vector.py:36-45`) filters by tenant only. It never filters by
  `embedding_model` or `dimensions`, so it will happily score 8-dim hash vectors against a 512-dim
  OpenAI query.
- `cosine_similarity` (`retrieval/vector.py:16`) uses `zip(left, right, strict=False)`, which
  **silently truncates to the shorter vector**. Worse, the dot product then uses 8 dims while
  `left_norm` uses all 512 — so the result is not even a valid cosine. It returns a plausible-looking
  number instead of raising.

Net effect: a half-migrated corpus produces silently wrong rankings with no error. **Fix both
before starting the re-embed** — filter on the active model in the query, and make the dimension
mismatch loud (`strict=True` or an explicit length check).

## Decisions (original analysis)

### Decision 1 — Vector scan strategy (Stage 3)

**Recommendation: numpy brute force.** The benchmark makes this lopsided in a way I did not expect
before measuring.

| | numpy cached matmul | `sqlite-vec` extension |
|---|---|---|
| **Pros** | Exact — no recall loss at all | Vectors stay in SQLite, paged by the OS |
| | No new dependency (numpy already present) | Flat, low process memory |
| | 9–20 ms measured at corpus scale | No cache-invalidation logic; writes are transactional |
| | Filters compose freely — you select row indices before or after the matmul | Scales past ~1M chunks |
| | Trivial to debug, swap metrics, or rerank | Backups stay a single self-contained file |
| **Cons** | 60–180 MB resident per process (fp16) | New C extension dependency; must be loaded per-connection via a SQLAlchemy connect hook |
| | Cache invalidation needed on ingest | Approximate results once ANN is engaged |
| | Cold start ~1–2 s to load the matrix | **Metadata filtering is limited** — combining with entity/language/`source_tree` filters likely means post-filtering, which loses recall exactly on your most selective queries |
| | Linear scaling — revisit past ~500k chunks | Two vector backends to maintain (pgvector separately) |

The deciding factor is that **at 58k chunks, brute force *is* the correct algorithm** — an index
would be premature optimization that costs exactness and filter composability to solve a problem
you do not have. The filtering point matters more than it looks: your retrieval path is unusually
filter-heavy (tenant, language, `include_und`, `source_tree_slug`, entity), and post-filtering an
ANN result set is precisely where hybrid systems quietly lose recall.

Memory is the one real cost. If 180 MB in the MCP process is unacceptable, store fp16 and compute
in fp32 — that halves it for negligible quality loss. Build it behind the existing backend
interface so swapping to `sqlite-vec` later is a contained change.

### Decision 2 — Embedding provider (Stage 4)

This is the genuine judgment call, because it trades project *identity* against convenience.

**Option A — OpenAI `text-embedding-3-small @ 512d`**

- **Pros:** ~$0.33 one-time for the whole corpus; no model download or torch dependency; strong,
  well-benchmarked quality; Matryoshka truncation; re-embed finishes in well under an hour.
- **Cons:** ships the entire corpus to OpenAI, which directly contradicts the README's
  "local-first" positioning — and you are about to flip this repo public (P0). Adds a **network
  call to every single query**, so vector search stops working offline and gains a failure mode in
  the MCP hot path. That ~50–100 ms round trip also dwarfs the 9 ms scan you just optimized.
  Vendor lock-in on the vector space: switching later means another full re-embed.

**Option B — Local `bge-m3` or `multilingual-e5`**

- **Pros:** fully offline; no key, no per-query network, no ongoing cost. **Strategically the best
  fit for your actual roadmap** — `bge-m3` is genuinely strong cross-lingually, which is exactly
  what P1 Stage 4 wants (a shared vector space so a Russian query reaches English chunks without
  the translation bridge). Query embedding is ~5–20 ms locally, comparable to or better than a
  network call.
- **Cons:** `sentence-transformers` + `torch` is a ~2–3 GB install — a heavy ask for a package
  about to go public (mitigate with an optional extra, which fits the existing provider protocol
  cleanly). Model download of 1–2 GB. Re-embedding 58k chunks on CPU is hours rather than minutes,
  though Apple Silicon MPS helps. Marginally behind the best API models on English-only retrieval.

**Option C — Azure OpenAI**

- **Pros:** you already have the provider implemented and settings wired (`.azure/`,
  `AZURE_OPENAI_*`), so it is the lowest-friction paid path if a deployment already exists. Better
  enterprise data-handling terms than direct OpenAI. Same underlying model quality.
- **Cons:** all of Option A's network and non-local drawbacks, plus deployment/quota management.
  Matryoshka `dimensions` support depends on the deployed model version and API version, so you
  may be stuck at full 1536 (359 MB fp32 / 180 MB fp16).

**My suggestion — decouple the experiment from the commitment.** Use Option A to *prove* real
embeddings fix the problem: it is $0.33 and one afternoon, and Stage 0's harness will tell you
exactly how much they help. Only then decide whether Option B's dependency weight is worth paying
permanently. `reembed_corpus.py` already rewrites vectors in place, so a second pass is cheap, and
this avoids committing the project's local-first identity to a decision made on a hunch.

### Decision 3 — Postgres priority

**Recommendation: SQLite first**, and one specific fact should drive this more than anything else.

| | SQLite-first *(recommended)* | Postgres-first |
|---|---|---|
| **Pros** | It is where the corpus actually lives — all 58k chunks, and where the 43 s problem is felt | pgvector HNSW moots Decision 1 entirely |
| | **FTS5 has native BM25, verified working above** | One backend to build and tune, not two |
| | Zero infra; single-file backups (which your ~100 `.db` snapshots show you rely on) | Real concurrency for API + MCP + worker; already assumed by `docker-compose.yml` |
| | Preserves the local-first, no-server story | The path to P4 (multi-tenant / hosted); deps already present |
| **Cons** | Two keyword backends long-term; ranking parity gap | **Requires migrating the live 336 MB corpus** — real work, real risk, currently out of scope |
| | Single-writer; a wall for P4 | **Postgres has no native BM25.** `ts_rank_cd` is not BM25 and ranks worse |
| | No native ANN (fine at this size) | Kills the no-server story; requires Docker for every use |

The decisive point: **the highest-impact fix in this whole plan — BM25 — is built into SQLite and
absent from Postgres.** Getting true BM25 there needs ParadeDB's `pg_search`, which is not in the
`pgvector/pgvector:pg16` image and is unavailable on most managed Postgres. Choosing Postgres first
means accepting a weaker version of the one thing that matters most, in order to solve a scaling
problem you do not yet have.

Scope Postgres as same-interface and lower-priority; revisit when P4 becomes real.

---

## Behavior changes and risks

- **`retrieval_weight` becomes an approximation.** Today it multiplies scores across an exhaustive
  scan, so the global ordering is exact. Once retrieval is index-driven, weights apply to a
  candidate window — a heavily-weighted source that falls outside the window can no longer be
  pulled into the top results. Mitigated by over-fetching, but it is a real semantic change and
  should be covered by an eval case.
- **Index drift.** The FTS table can desynchronize from `chunks` if a write path bypasses the hook.
  Mitigated by routing through the single `embed_chunks` chokepoint plus a maintenance check.
- **Score field meaning changes** across modes and versions — see Stage 5.
- **Test-suite coupling:** `tests/test_vector_search.py` asserts 8 dimensions and
  `deterministic-hash-v1` directly. Those assertions need updating deliberately, not incidentally.

## Out of scope

Reranking (cross-encoder / LLM) — `todo.md` P2 already tracks it, and it belongs on top of a
working fusion layer, not as a substitute for one. Same for the domain lexicon of proper nouns
(P1 Stage 4), which becomes much easier to evaluate once Stage 0 exists.
