# Changelog

All notable changes to Citara will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses source-available noncommercial licensing under the PolyForm Noncommercial License 1.0.0.

## [Unreleased]

Rebuilds both halves of hybrid retrieval. The RRF fusion layer is unchanged — the inputs it was
fusing were the problem. Measured on the 1,444-source / 58,471-chunk corpus.

### Fixed

- **Keyword ranking was decided by stopwords.** `search_knowledge` scored a chunk as the raw sum of
  query-token occurrences, with no IDF and no length normalization. For
  `what does the exodus teach about covenant`, the top three results contained *zero* occurrences
  of `exodus`, `teach`, or `covenant`; the winner scored 121.5 on 135 occurrences of `the`.
  Scoring is now BM25, so a term appearing everywhere carries almost no weight.
- **`cosine_similarity` silently truncated mismatched vectors.** It used
  `zip(left, right, strict=False)`, summing only the shorter vector's products while dividing by
  the longer vector's magnitude — returning a plausible number that was not a cosine of anything.
  An 8-dimension stored vector scored against a 512-dimension query produced silently wrong
  rankings with no error, which is exactly the state a corpus is in partway through a re-embed.
  It now raises on a length mismatch.
- **`vector_search` did not filter by embedding model.** It scored a query against every stored
  vector regardless of which model produced it. Retrieval now loads only vectors matching the
  active provider's model, so a half-migrated corpus cannot be scored against a foreign space.

### Added

- `scripts/eval_retrieval.py` and `tests/eval/queries.json`: a 32-query evaluation harness
  reporting Recall@k, MRR, nDCG@k, and latency per mode. Ground truth comes from title-anchored
  known-item labels (free) and pooled per-chunk judgments (`--judge`). Retrieval changes are now
  measurable rather than asserted.
- `chunk_fts`, a SQLite FTS5 index over chunk text, with native `bm25()` ranking
  (migration `20260816_0005`). Indexed content is `" ".join(tokenize(text))` rather than raw text,
  so the index and the query agree on tokenization and the existing Unicode/CJK-bigram handling
  stays authoritative instead of deferring to FTS5's `unicode61`.
- `scripts/backfill_fts.py` to populate the index on an existing corpus, with `--check` for
  index/corpus drift.
- `core/retrieval/base.py`, `bm25.py`, `fts.py`, and `vector_cache.py`. Filter construction, which
  had been duplicated across three backends, now lives in one place.
- `dimensions` support on the OpenAI and Azure embedding providers (Matryoshka truncation).

### Changed

- **`.env` is now loaded automatically**, reversing the `0.4.1` position that it is consumed only
  by `docker compose`. Real environment variables still win — the file only fills in what is
  unset — and `CITARA_SKIP_DOTENV=1` disables it (the test suite sets this, so a developer's local
  credentials can never change what the suite exercises). The previous behavior meant a file that
  looked like configuration silently did nothing.
- Keyword search runs against the FTS index on SQLite; Postgres and any SQLite build without FTS5
  fall back to the BM25 scan in `keyword.scan_search`. Both rank by BM25, so they stay comparable.
- Vector search scores the corpus as one matrix-vector product over a cached, pre-normalized
  float32 matrix instead of a per-row Python cosine loop.
- Embeddings are stored as a packed float32 buffer rather than a JSON array on non-Postgres
  backends. Reads still accept legacy JSON rows, so no migration is required, and the format
  converts as vectors are rewritten. Round trips are now float32-accurate (~1e-7) rather than
  bit-exact.
- `EMBEDDING_PROVIDER=openai` no longer inherits the local placeholders. `EMBEDDING_MODEL`
  defaulting to `deterministic-hash-v1` would have been sent to the API as a model name, and
  `EMBEDDING_DIMENSIONS` defaulting to `8` would have silently requested 8-dimension vectors from a
  real model. Both now fall back to `text-embedding-3-small` at 512 dimensions.
- `numpy` is now a declared dependency; it was previously only transitive via `pgvector`.

### Performance

Measured by `scripts/eval_retrieval.py` over 32 queries at k=10 against the 58,471-chunk corpus.

| mode | recall@10 | nDCG@10 | p50 latency |
| --- | --- | --- | --- |
| keyword | 0.373 → **0.741** | 0.232 → **0.581** | 12,751 ms → **1,989 ms** |
| vector | 0.148 → **0.800** | 0.075 → **0.660** | 6,000 ms → **181 ms** |
| hybrid | 0.222 → **0.831** | 0.130 → **0.682** | 18,782 ms → **981 ms** |

Hybrid gains 3.7× recall and 5.2× nDCG at 19× lower latency. More importantly it now behaves like
hybrid search is supposed to: at baseline it scored *below* keyword alone on every quality metric,
because RRF was fusing a usable keyword ranking with a vector ranking that carried no signal. It
now beats both of its inputs.

Vector-index cold start is 6.0 s for 58,471 × 512 (120 MB resident); the warm path is a dictionary
lookup. Re-embedding the full corpus took ~6 minutes and cost roughly $0.35.

### Fixed (citations)

- **Text in Us timestamp links did not seek.** All 168 sources for that show carried a
  `podcasters.spotify.com` canonical URL (now redirecting to `creators.spotify.com`), and that page
  ignores the `?t=` parameter that `_timestamp_url()` appends. Every citation deep link for the
  show was well-formed but landed at the start of the episode. They now point at
  `open.spotify.com/episode/{id}`, which honors `t=`.

  The two URL forms use unrelated id spaces (Anchor's trailing `-e1du4b3` is base36; Spotify
  episode ids are 22-character base62) and the RSS feed carries no Spotify id, so the mapping had
  to be looked up rather than derived. `scripts/fix_spotify_episode_urls.py` joins
  `episode_guid → RSS title → Spotify episode title → episode id` and rewrites `canonical_url`,
  preserving the original as `metadata_json.anchor_url` alongside `spotify_episode_id`. All 168
  matched with no unresolved rows; three mappings were spot-checked against Spotify's public
  oEmbed endpoint.

  The connector still stores the feed's `<link>`, since resolving ids needs the Spotify Web API and
  ingestion should not require credentials to produce correct links. Re-run the script after each
  import — it is idempotent and only touches stale URLs.

### Data & migrations

- Create the index, then populate it once with `uv run python scripts/backfill_fts.py --yes`.
  Ingestion maintains it from then on, and `--check` reports index/corpus drift.
- Creating the index: `alembic upgrade head` on an Alembic-tracked database. A database built by
  `init_db()` has an empty `alembic_version` table and is *not* Alembic-tracked — `upgrade head`
  would try to recreate existing tables and fail. `backfill_fts.py` calls `init_db()` itself, so on
  those databases it both creates and populates the index in one step, and no Alembic run is
  needed.
- No embedding migration is required to adopt the packed vector format.

## [0.4.1] - 2026-08-07

Completes the corpus-location fix that `0.4.0` only half-shipped.

### Fixed

- `src/citara/core/config.py` still defaulted to `../citara` for `DATABASE_URL`,
  `OBJECT_STORE_PATH`, `SOURCE_ARTIFACT_ROOT`, and `SOURCE_STATE_ROOT`. `0.4.0` corrected
  `scripts/artifact_paths.py`, `.env.example`, and the docs but missed the library itself, so the
  bug it claimed to fix — `../citara` resolving back into a checkout named `citara` — was still
  live for anything importing `citara.core`. That included every podcast connector, whose
  `DEFAULT_ARTIFACT_DIR` and `DEFAULT_STATE` derive from these settings. Defaults are now
  `../citara-data`, honoring `CITARA_DATA_ROOT`.
- `tests/test_bema_textinus_pipelines.py::test_specific_pipeline_defaults_use_external_citara_roots`
  asserted the connector defaults pointed at `repo_root.parent / "citara"` — the repository itself.
  The test encoded the bug as expected behavior, which is why the suite stayed green through
  `0.4.0`. It now asserts the corrected location, alongside a new test that resolves each default
  and fails if it lands inside the repo.

### Added

- `tests/test_config_defaults.py`: guards that `CITARA_DATA_ROOT` is honored, that a trailing slash
  is stripped before interpolation, that no `Settings` path default resolves inside the repository,
  and that no `../citara/` literal reappears anywhere under `src/`.

### Changed

- `.env.example` and `README.md` no longer imply that creating a `.env` configures a bare-metal run.
  Nothing in `src/` or `scripts/` calls `load_dotenv()`; configuration is read from the process
  environment, and `.env` is consumed only by `docker compose`. Both documents now say so and show
  `set -a; source .env; set +a`. Quick start no longer opens with `cp .env.example .env`, since the
  built-in defaults are correct with no configuration at all.

### Data & migrations

- No Alembic migration required; the database schema is unchanged.
- No corpus re-index or re-embed needed. Stored data remains backward compatible with `0.4.0`.
- **Check where your data actually landed.** If you ran `0.4.0` or earlier on bare metal without
  exporting `DATABASE_URL`/`SOURCE_ARTIFACT_ROOT`/`SOURCE_STATE_ROOT`, the library defaults wrote to
  `../citara` — your checkout — so look for `citara.db`, `source-artifacts/`, `import-state/`, and
  `object-store/` at the repository root and move them to `../citara-data`. `.gitignore` has
  covered those paths since `0.4.0`, so they are ignored rather than committed, but they are still
  in the wrong place. A `.env` alone will not have protected you unless you exported it.

## [0.4.0] - 2026-08-05

Public-release hygiene pass. No runtime behavior changes, but the default corpus location moved, so
read the migration note below before upgrading.

### Security

- Removed a hardcoded transcription-worker SSH target (`root@<ip>`) and private-key filename from
  `scripts/transcribe_bema_remote_batch.py`, `scripts/transcribe_bibleproject_remote_batch.py`, and
  `scripts/transcribe_podcast_remote_batch.py`. The worker is deployment-specific and now comes from
  `CITARA_WORKER_SSH` / `CITARA_WORKER_SSH_KEY` / `CITARA_WORKER_ROOT` or the corresponding CLI
  flags. `--worker` is a required argument when no environment default is set, so the scripts fail
  loudly rather than attempting to reach someone else's host.
- Removed hardcoded absolute developer paths (`/Users/<name>/...`) from eight scripts. All data
  locations now resolve through `scripts/artifact_paths.py`, which reads `CITARA_DATA_ROOT` and
  falls back to a sibling `../citara-data` directory.
- Stopped tracking two podcast pipeline-state files that had been committed into `import-state/`,
  and added `/source-artifacts/`, `/import-state/`, and `/object-store/` to `.gitignore`. The files
  remain on disk; only Git tracking changed.

### Fixed

- The documented default corpus root resolved back into the repository itself. `.env.example`,
  `docs/SOURCE_ARTIFACT_STORAGE.md`, and `scripts/artifact_paths.py` all pointed at `../citara`,
  which from a checkout named `citara` is the checkout — defeating the "keep corpus data outside the
  code repo" design and explaining how corpus state came to be committed. The default is now
  `../citara-data`, and a regression test asserts the resolved data root is never inside the repo.

### Added

- `CITARA_DATA_ROOT` sets the database, artifact, state, and object-store paths in one variable.
  Finer-grained `SOURCE_ARTIFACT_ROOT` / `SOURCE_STATE_ROOT` / `OBJECT_STORE_PATH` still win when
  set.
- `artifact_paths.apply_default_env()` seeds the data-location environment variables that scripts
  rely on without overriding values from a real `.env`.

### Changed

- Rewrote `README.md` around a reader's path through the project: value proposition, quick start,
  and MCP integration first; API, ingestion, and retrieval reference next; operations, limitations,
  and project metadata last. Added badges, a table of contents, capability and MCP-tool tables, and
  collapsed the remote-transcription operator detail into a `<details>` block. Content is
  preserved — license and versioning policy moved from the top to `Project information`.

### Data & migrations

- No Alembic migration required; the database schema is unchanged.
- No corpus re-index or re-embed needed. Embeddings, chunks, and stored data are untouched and
  remain backward compatible with `0.3.1`.
- **Action may be required if you relied on the built-in defaults rather than a `.env`.** The
  default data root changed from `../citara` to `../citara-data`. If you have an existing `.env`
  with explicit `DATABASE_URL` / `SOURCE_ARTIFACT_ROOT` / `SOURCE_STATE_ROOT` values, nothing
  changes. Otherwise, either move your corpus directory to `../citara-data` or set
  `CITARA_DATA_ROOT` to its current location.
- Users of the remote transcription scripts must now set `CITARA_WORKER_SSH` (or pass `--worker`);
  those scripts previously had a built-in host and will now exit with a missing-argument error
  instead.

## [0.3.1] - 2026-08-05

### Fixed

- Artifact `kind` classification no longer assumes the `bema` filename convention. The regexes anchored on `e\d+-`, so remote-transcription artifacts from shows using prefixes like `q001-buzzsprout-17003095-s4e1-` fell through to the `other` bucket — 240 of them on the real corpus. Matching is now on the kind suffix with any non-empty prefix.
- `iter_tree_files()` no longer skips every dotfile. The blanket skip also dropped live pipeline state (`.transcription-watchdog.lock`, `.hourly-completion-reported`, `.hourly-status-complete-sent`) from what is meant to be a complete audit index. Only genuine OS cruft (`.DS_Store`, `._*`, `Thumbs.db`, `.localized`) is excluded now.
- `organize_bema_transcripts()` no longer creates a phantom `bema/source-tree.json` when `data/` is empty. It called `tree_meta()` before checking whether any source data existed; unlike the other `organize_*()` functions it aggregates several directories, so the check now runs after source collection.

### Data & migrations

- No Alembic migration required; the database schema is unchanged.
- No corpus re-index or re-embed needed. These fixes affect only the audit manifest, which no application or test code reads.
- Corpora whose manifest was generated with `0.3.0` should re-run `--rebuild-from-artifacts` to pick up the corrected classification and the previously omitted pipeline-state files. On the reference corpus this moves 240 records out of `other` and raises the artifact count from 10,240 to 10,244.

## [0.3.0] - 2026-08-05

### Added

- Added `--rebuild-from-artifacts` to `scripts/organize_source_artifacts.py`, which reconstructs `organization-manifest.json` by walking the existing `source-artifacts/` and `import-state/` trees instead of the `data/` staging directory. Records carry `tree`, `kind`, `bytes`, and `sha256`; `--no-hash` skips hashing, which matters on a real tree (~1.3 GB / ~10k files). The mode is strictly read-only against the artifact trees.
- Added a `mode` field (`"organized"` or `"rebuilt"`) to the manifest. A rebuilt manifest cannot recover each file's original `data/` path, so `source` is salvaged from the sibling `source.json`'s `original_path` where present and left `null` otherwise, never invented. The marker exists so a consumer can tell the two provenance levels apart.
- Added `trees_missing_source_tree_json` to the manifest summary. Rebuild reports trees lacking `source-tree.json` rather than synthesizing one, keeping the mode read-only.

### Fixed

- `organize_source_artifacts.py` no longer silently replaces a populated manifest with an empty one. A write of 0 records over an existing manifest with records is now refused with an explanatory message and exit code 2, on both the organize and rebuild paths; `--force` is the explicit opt-out. This is the bug that emptied the manifest when the `data/` staging directory was cleared.

### Data & migrations

- No Alembic migration required; the database schema is unchanged.
- No corpus re-index or re-embed needed. This release touches only the audit manifest, which no application or test code reads.
- `organization-manifest.json` gains `mode` and `trees_missing_source_tree_json`; the `schema` value stays `citara.organization_manifest.v1` since existing fields are unchanged and both additions are additive.

## [0.2.0] - 2026-08-04

### Added

- Added `scripts/reembed_corpus.py`, which recomputes stored chunk embeddings **in place** using the configured provider. It re-embeds only; it does not re-ingest, so sources, chunks, chunk IDs, entity links, source preferences, and ingestion history are untouched. Dry run by default (writing requires `--yes`), reporting cosine drift between each stored vector and a freshly computed one. It also flags embedding rows belonging to a different model, since `vector_search` does not filter by `embedding_model` and such rows let a single chunk match more than once; `--prune-other-models` removes them. Keyset-paginated so a corpus of tens of thousands of chunks is not held in memory at once.

### Data & migrations

- No Alembic migration required; the database schema is unchanged.
- This release adds the remedy for the re-embed flagged in `0.1.0`. Corpora embedded under `EMBEDDING_PROVIDER=local` before `0.1.0` should run `uv run python scripts/reembed_corpus.py --dry-run` and then `--yes`. Measured on a 43,146-chunk podcast corpus, 40,106 chunks (93%) had drifted, mean cosine 0.9611 and minimum 0.2245 against freshly computed vectors.
- Running the script rewrites the `embeddings` table for the selected tenant. Back up the database first; it is not reversible.

## [0.1.0] - 2026-08-04

First tagged release. Everything below accumulated before Citara had a release process; it is
recorded here as the `0.1.0` baseline.

### Added

- Added repository hygiene tooling with pre-commit hooks for Ruff, formatting, file hygiene, large-file checks, and secret-key detection.
- Added a Contributor Covenant Code of Conduct.
- Added GitHub issue templates for bug reports and feature requests.
- Added a GitHub pull request template with test, data-safety, and documentation checklists.
- Added multilingual query support (P1 Stages 1-3): Unicode-aware tokenization (with character-bigram fallback for Han/Kana/Hangul), corrected dependency-free language detection for Latin-script non-English languages, and a `notice` diagnostic on `search_knowledge`/`retrieve_context_pack` when a query's language has no matching corpus sources.
- Added optional `query_translated`/`query_language` parameters to `search_knowledge` and `retrieve_context_pack` (MCP, HTTP API, and core), so a client-supplied translation is searched alongside the original query and fused by rank via the existing RRF machinery.
- Added a `TranslationProvider` protocol (`core/language/translate.py`) with a no-op default and an OpenAI/Azure-backed server-side fallback, used only when the client supplies no translation, cached on `(model, text, target_language)`. Configured via new `TRANSLATION_PROVIDER` (default `noop`, no network calls) and `TRANSLATION_MODEL` env vars, reusing the existing `OPENAI_API_KEY`/`AZURE_OPENAI_*` credentials.
- Added `response_language` and `response_language_directive` to `retrieve_context_pack`, and an optional `translate_quotes` parameter that adds `text_translated`/`translation_provenance` alongside (never in place of) the verbatim `text`.
- Added `version` to the `/health` endpoint and the MCP `ping` tool (additive, alongside the existing `status` key), sourced from `citara.__version__`.
- Added `.github/workflows/release.yml`, triggered on `v*` tags: re-runs the CI check suite, fails loudly if the tag doesn't match `citara.__version__`, builds sdist/wheel with `uv build`, extracts the matching `CHANGELOG.md` section, and publishes a GitHub Release with the artifacts attached.
- Added a SemVer `0.x` policy to the README defining Citara's public API (MCP tool surface, HTTP API, env-var config names, DB schema, CLI) and a release checklist plus a `### Data & migrations` changelog convention to `CONTRIBUTING.md`.

### Changed

- Standardized public-facing language around Citara as source-available/noncommercial rather than OSI open source.
- `pyproject.toml` now declares `dynamic = ["version"]`, reading the version from `src/citara/__init__.py` (`[tool.hatch.version]`) instead of duplicating it -- that file is now the single source of truth.

### Data & migrations

- No Alembic migration required; the database schema is unchanged.
- Fully backward compatible: all new fields (`notice`, `query_translated`, `query_language`, `response_language`, `response_language_directive`, `translate_quotes`, `text_translated`, `translation_provenance`, `version`) are additive and optional. No existing MCP/HTTP parameter, response field, or env var was renamed, removed, or retyped.
- **A re-embed is recommended for `EMBEDDING_PROVIDER=local` (the default).** `EMBEDDING_MODEL` and `EMBEDDING_DIMENSIONS` are unchanged, and the cross-language support itself works at query time via translation and rank fusion. However, `DeterministicEmbeddingProvider` derives its vectors from `tokenize()`, whose Unicode fix no longer treats the apostrophe as a word character: `"god's"` now tokenizes to `["god", "s"]` rather than `["god's"]`. Vectors stored before this release were computed under the old tokenizer, so on contraction-heavy text (podcast transcripts especially) a stored vector and a freshly computed one measure roughly 0.62 cosine instead of 1.0, degrading vector recall until the corpus is re-embedded. Keyword search is unaffected, because query and chunk are tokenized together at query time.
- No re-embed needed for `EMBEDDING_PROVIDER=openai` / `azure_foundry`: those providers tokenize server-side and never call `tokenize()`.
