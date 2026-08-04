# Development TODO

Prioritized next steps toward a clean public (source-available) release and beyond.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done

Completed items are pruned from this file once done; see `CHANGELOG.md` and git history for what shipped.

---

## P0 — Before the first public push (blockers)

- [ ] **Flip the repo to public** once the remaining P0 item is closed: `gh repo edit cloudsecmentor/citara --visibility public`. (Verified 2026-08-04: still `PRIVATE`.)
- [ ] **Run the manifest rebuild against the real tree.** `--rebuild-from-artifacts` shipped in 0.3.0 but has not been run on `citara-data/`, whose `organization-manifest.json` is still the empty one from 2026-07-30. Run `--rebuild-from-artifacts --no-hash` first to check the shape, then without `--no-hash` for the real index (~1.3 GB, a few minutes).

## P1 — Multilingual query & response support

**Status (2026-08-04): Stages 1–3 shipped.** `tokenize()` and `DeterministicEmbeddingProvider` are
now Unicode-aware (with character-bigram fallback for Han/Kana/Hangul); `detect_language_code` no
longer mislabels every Latin-containing query `en`; `search_knowledge`/`retrieve_context_pack`
return a `notice` when a query's language has no matching corpus sources instead of failing
silently, and `_resolve_source_language`'s filter trap is guarded; `query_translated`/
`query_language` params let a client (or a configured server-side `TranslationProvider` fallback)
bridge cross-language retrieval via dual-query RRF fusion; and `retrieve_context_pack` now reports
`response_language`/`response_language_directive` plus optional, citation-preserving
`translate_quotes`. See `CHANGELOG.md` `[Unreleased]` and `tests/test_language_policy.py`,
`tests/test_tokenize.py`, `tests/test_language_detect.py`, `tests/test_translation_providers.py`
for what shipped and how it's verified. Stage 4 below remains open.

### Stage 4 — Real cross-lingual retrieval

- [ ] **Multilingual embeddings.** Query translation is a bridge; true cross-lingual recall wants a
  shared vector space (`multilingual-e5`, `bge-m3`, or OpenAI `text-embedding-3-*`). The re-index
  half is no longer a blocker — `scripts/reembed_corpus.py` (0.2.0) rewrites stored vectors in
  place for whatever provider is configured. What remains is raising `EMBEDDING_DIMENSIONS` from
  its default of 8 and confirming the pgvector column width, since vectors are stored per model.
- [ ] **Domain lexicon for proper nouns.** This corpus is biblical podcasts, where the
  highest-value query terms are exactly the ones generic MT transliterates inconsistently
  (Исход → Exodus, Второзаконие → Deuteronomy). A small curated scripture/name glossary applied
  before retrieval will beat generic MT on the queries users actually ask.
- [ ] **Corpus-language discovery.** Extend `list_sources`, or add a `corpus_languages` tool, so a
  client can learn the corpus language up front and translate proactively instead of reactively.

## P2 — Product / functional gaps

- [ ] **Promote real audio transcription into `core/transcription/` and collapse the per-podcast pipelines.** Today each show has its own near-parallel scripts (`remote_bema_transcribe.py`, `transcribe_bema_remote_batch.py`, `transcribe_bibleproject_remote_batch.py`, plus per-show import scripts), and real Whisper lives inside connectors while `core/transcription/providers.py` only has a fixture provider. Plan:
  - [ ] Move a real transcription provider (faster-whisper local and/or remote batch) behind the existing `core/transcription/` protocol.
  - [ ] Build one generic batch driver (discover → download → transcribe → publish with taxonomy metadata) that connectors feed with show-specific config only.
  - [ ] Reduce `scripts/transcribe_*_remote_batch.py` and per-show import scripts to thin wrappers or delete them.
  - [ ] Removes the README's first listed limitation ("Raw audio transcription is not implemented yet").
- [ ] **`tree_meta()` creates phantom trees when `data/` is empty.** `organize_bema_transcripts()` calls `tree_meta("bema", ...)` unconditionally before checking whether any source data exists, so running the organize path against an empty staging dir writes a stray `bema/source-tree.json` into an otherwise-empty artifact tree. Harmless on the real corpus (the file already exists, so `tree_meta` returns early), but it is the same "organize assumes `data/` is populated" assumption that emptied the manifest. Move the `tree_meta()` calls behind the same existence checks the record-building code already does.
- [ ] **Reranking** for retrieval (cross-encoder or LLM reranker layered on top of RRF-fused hybrid results).
- [ ] **Implement or remove Docker stubs.** `worker` and `frontend` services in `docker-compose.yml` are print-stubs — either implement a minimal version or clearly mark/remove them so first-run `docker compose up` isn't confusing.
- [ ] **Deferred ingestion types** from `docs/IDEA.md`: PDF (dep `pymupdf` already present), OCR/screenshots, web article ingestion. Scope one at a time.
- [ ] **A minimal debug web UI** (the IDEA doc calls for one for uploads/inspecting jobs/testing retrieval).

## P3 — Docs & polish

- [ ] **Quickstart that works in <5 min** at the top of README (clone → `uv sync` → `docker compose up -d postgres api` → curl example).
- [ ] **Architecture diagram** kept in sync (there's `docs/current-architecture.html`; consider a Mermaid diagram in README).
- [ ] **Document the connector authoring flow** (how to add a new podcast/source connector under `src/.../connectors/`).
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
