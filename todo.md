# Development TODO

Prioritized next steps toward a clean public (source-available) release and beyond.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done

Completed items are pruned from this file once done; see `CHANGELOG.md` and git history for what shipped.

---

## P0 — Before the first public push (blockers)

- [ ] **Flip the repo to public** once the remaining P0 item is closed: `gh repo edit cloudsecmentor/citara --visibility public`. (Verified 2026-08-04: still `PRIVATE`.)
- [ ] **`organization-manifest.json` rebuild mode.** The manifest was reset to empty because `scripts/organize_source_artifacts.py` rebuilds it from the repo `data/` staging dir (now empty). It is an audit-only index (not used by the app or tests) and the underlying artifacts are intact, but the script still has no way to reconstruct it. Add a "rebuild manifest from the existing `source-artifacts/` tree" mode.

## P1 — Multilingual query & response support

**Problem (verified 2026-08-04).** The corpus is English; users query the MCP in other
languages. Those queries currently fail *silently* — an empty result set is indistinguishable
from "nothing relevant in the corpus."

Measured against the current code:

| Query | `tokenize()` | `detect_language_code` | deterministic embedding |
|---|---|---|---|
| `Что говорит об Исходе` | `[]` | `('ru', 0.86)` | all-zero vector |
| `מה אומר על יציאת מצרים` | `[]` | `('he', 0.82)` | all-zero vector |
| `Что говорит BEMA об Исходе?` | `['bema']` | `('en', 0.15)` | 1 of 8 dims set |
| `¿Qué dice sobre el éxodo?` | `['qu','dice','sobre','el','xodo']` | `('en', 0.68)` | — |

Root causes:

1. **`tokenize()` is Latin-only.** `TOKEN_RE = [A-Za-z0-9']+` (`core/chunking/simple.py:6`).
   Any non-Latin script tokenizes to `[]`, and `search_knowledge` bails at
   `core/retrieval/keyword.py:76` before touching the DB.
2. **The default embedder is Latin-only too.** `DeterministicEmbeddingProvider` uses the same
   regex (`core/embeddings/providers.py:13`), so a Cyrillic/Hebrew/Arabic/CJK query embeds to the
   zero vector, `cosine_similarity` short-circuits to `0.0` (`core/retrieval/vector.py:19`), and
   the `score > 0` filter drops everything. **On the default `EMBEDDING_PROVIDER=local`, hybrid
   mode returns nothing at all** — both backends are dead, not just keyword. (With
   `EMBEDDING_PROVIDER=openai` the vector half partially survives, since those models are
   multilingual — so severity is provider-dependent and invisible in local testing.)
3. **Latin-script non-English is silently mislabeled `en`.** `detect_language_code` returns `en`
   whenever *any* Latin letter appears (`core/language/detect.py:45`). Spanish/German/French/
   Portuguese queries are confidently "English", and accented words split mid-token
   (`éxodo` → `xodo`), so they degrade quietly instead of failing loudly.
4. **No output-language contract.** The MCP returns English chunks with no signal about what
   language the user asked in. Whether the reply comes back in the user's language depends
   entirely on the calling agent's own instincts.
5. **A latent filter trap.** `_resolve_source_language` falls back to the corpus's dominant
   language, which masks the problem while the corpus is monolingual. Once a single Russian
   source is ingested, a Russian query would filter *to that one source* and hide the entire
   English corpus.

**Design principle.** Citara is an MCP server — the user-facing reply is composed by the calling
agent, not by Citara. So the fix is not for Citara to generate prose in the user's language; it is
to (a) make retrieval work cross-lingually and (b) return explicit language metadata the client
can honor. And because the project's value prop is source fidelity, a translated quote must never
be substituted for the original: return verbatim source text always, with any translation as a
clearly marked *additional* field.

### Stage 1 — Stop failing silently (no new dependencies)

- [ ] Make `tokenize()` Unicode-aware: `re.compile(r"[^\W_]+", re.UNICODE)` keeps Latin behavior
  identical while admitting Cyrillic/Hebrew/Arabic/Greek and fixing `éxodo`. CJK has no
  whitespace and needs separate handling — at minimum, character bigrams for Han/Hangul/Kana.
- [ ] Apply the same regex in `DeterministicEmbeddingProvider` so the local/test provider stops
  emitting zero vectors. Keep `_normalized_tokens`' `removesuffix("s")` stemming Latin-only —
  it is English-specific and corrupts other scripts.
- [ ] Return a diagnostic instead of an empty list. When the query tokenizes to nothing, or the
  detected query language has no matching sources, include a `notice` in the `search_knowledge` /
  `retrieve_context_pack` payload (e.g. `{"code": "cross_language_query", "query_language": "ru",
  "corpus_languages": ["en"]}`) so the agent can react rather than report "nothing found."
- [ ] Guard the filter trap: under `auto`, never let a detected query language narrow the corpus
  to a near-empty slice. Require a minimum source count, or treat query language and source
  language as independent axes.

Stage 1 alone does **not** make a Russian query match English text — Cyrillic tokens still never
equal English ones. What it buys is a loud, correct failure plus a working substrate for stages
2–3, and it fixes Latin-script languages (Spanish/German/French) outright, where proper nouns
and shared terms like `Exodus`/`BEMA` already overlap.

### Stage 2 — Translate the query before retrieval (the main fix)

- [ ] **Client-supplied translation (preferred, zero-dependency).** The calling agent is already a
  multilingual LLM holding the user's turn. Add optional `query_translated: str | None` and
  `query_language: str | None` params to `search_knowledge` / `retrieve_context_pack`, and state
  in the tool descriptions that a client querying in a non-corpus language should pass an English
  translation alongside the original. No API keys, no added latency, no cost.
- [ ] **Server-side fallback.** A `TranslationProvider` protocol in `core/language/translate.py`,
  mirroring the existing `EmbeddingProvider` / transcription-provider pattern: a no-op default
  plus an LLM-backed implementation reusing the configured OpenAI/Azure credentials. Used only
  when the client supplied nothing. Cache on `(text, target_lang)` — queries repeat.
- [ ] **Search both, fuse by rank.** Retrieve on the original *and* the translated query, then
  merge through the existing RRF machinery in `retrieval/hybrid.py` — it is already rank-based,
  so it fuses heterogeneous runs correctly. Keeps proper nouns and code-switched terms that only
  survive in the original.
- [ ] Resolve `language_policy` from the **original** query, never the translation — otherwise the
  translated English query re-detects as `en` and defeats `strict`.

### Stage 3 — Answer in the user's language

- [ ] Add `response_language` to the context pack (defaulting to the detected query language) plus
  a short rendering directive, so the calling agent knows to answer in Russian while the evidence
  it received is English.
- [ ] Keep citations verbatim: `text` always stays the untouched source string. Add
  `text_translated` and `translation_provenance` (`model`, `target_language`) only on request —
  never overwrite the original.
- [ ] Add `translate_quotes: bool = False` to the retrieval tools so the client opts in. Leave
  citation labels and URLs untranslated; they are identifiers, not prose.

### Stage 4 — Real cross-lingual retrieval

- [ ] **Multilingual embeddings.** Query translation is a bridge; true cross-lingual recall wants a
  shared vector space (`multilingual-e5`, `bge-m3`, or OpenAI `text-embedding-3-*`). Blocked on a
  re-index: `EMBEDDING_DIMENSIONS` defaults to 8 and vectors are stored per model, so switching
  needs a dimension migration plus re-embedding the corpus.
- [ ] **Domain lexicon for proper nouns.** This corpus is biblical podcasts, where the
  highest-value query terms are exactly the ones generic MT transliterates inconsistently
  (Исход → Exodus, Второзаконие → Deuteronomy). A small curated scripture/name glossary applied
  before retrieval will beat generic MT on the queries users actually ask.
- [ ] **Corpus-language discovery.** Extend `list_sources`, or add a `corpus_languages` tool, so a
  client can learn the corpus language up front and translate proactively instead of reactively.
- [ ] **Tests.** Extend `tests/test_language_policy.py` with a cross-language case (Russian query
  against an English fixture corpus, asserting non-empty results), plus per-script tokenizer unit
  tests.

Ordering: Stage 1 is a small self-contained correctness fix; Stage 2 delivers most of the
user-visible win; Stage 3 is cheap once 2 lands; Stage 4 is the real engineering.

## P2 — Product / functional gaps

- [ ] **Promote real audio transcription into `core/transcription/` and collapse the per-podcast pipelines.** Today each show has its own near-parallel scripts (`remote_bema_transcribe.py`, `transcribe_bema_remote_batch.py`, `transcribe_bibleproject_remote_batch.py`, plus per-show import scripts), and real Whisper lives inside connectors while `core/transcription/providers.py` only has a fixture provider. Plan:
  - [ ] Move a real transcription provider (faster-whisper local and/or remote batch) behind the existing `core/transcription/` protocol.
  - [ ] Build one generic batch driver (discover → download → transcribe → publish with taxonomy metadata) that connectors feed with show-specific config only.
  - [ ] Reduce `scripts/transcribe_*_remote_batch.py` and per-show import scripts to thin wrappers or delete them.
  - [ ] Removes the README's first listed limitation ("Raw audio transcription is not implemented yet").
- [ ] **Reranking** for retrieval (cross-encoder or LLM reranker layered on top of RRF-fused hybrid results).
- [ ] **Implement or remove Docker stubs.** `worker` and `frontend` services in `docker-compose.yml` are print-stubs — either implement a minimal version or clearly mark/remove them so first-run `docker compose up` isn't confusing.
- [ ] **Deferred ingestion types** from `docs/IDEA.md`: PDF (dep `pymupdf` already present), OCR/screenshots, web article ingestion. Scope one at a time.
- [ ] **A minimal debug web UI** (the IDEA doc calls for one for uploads/inspecting jobs/testing retrieval).

## P3 — Docs & polish

- [ ] **Quickstart that works in <5 min** at the top of README (clone → `uv sync` → `docker compose up -d postgres api` → curl example).
- [ ] **Architecture diagram** kept in sync (there's `docs/current-architecture.html`; consider a Mermaid diagram in README).
- [ ] **Document the connector authoring flow** (how to add a new podcast/source connector under `src/.../connectors/`).
- [ ] **Adopt tags/releases.** `CHANGELOG.md` exists and is populated, but the repo has no git tags and no GitHub releases yet — wire up the version-tag workflow.
- [ ] **Screenshots / example citation output** in README to show the value prop.

## P4 — Toward multi-tenant / hosted (longer term, from IDEA.md)

- [ ] Real auth + per-user isolation (models already carry tenant/user; wiring is single-user local).
- [ ] Object store abstraction beyond local (S3/Azure Blob) — `OBJECT_STORE_TYPE` hook exists.
- [ ] Background job queue for the `worker` service.
- [ ] Rate limiting, quotas, upload-size enforcement end-to-end.

---

## Naming (decision — reference)

**Chosen name: `Citara`** (coined from "cite") — agent-agnostic and points at the real
differentiator: *citation-backed retrieval with source fidelity*, not generic "agent memory."

Availability (checked): PyPI `citara` free, npm `citara` free, no GitHub software project by this
name in the knowledge/memory/RAG space. (Two unrelated IT-consulting firms use `citara.io`/`.us`,
so prefer `citara.dev`/`.app` for any website.)

Rejected `Mneme`/`Mnemo`: both are heavily taken by near-identical tools (Joshwani/mneme,
MnemeHQ/mneme, Mnemo-mcp/Mnemo, MnemoAI/mnemo), with the PyPI names already published.

Positioning: *Citara is a local-first, citation-backed personal knowledge vault that any AI agent
can query over MCP — a source-faithful context backend for agents.*

The code/docs/packaging rename is complete. The one outstanding piece is renaming the GitHub repo
itself, tracked under P0.
