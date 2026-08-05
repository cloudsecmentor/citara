# Changelog

All notable changes to Citara will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses source-available noncommercial licensing under the PolyForm Noncommercial License 1.0.0.

## [Unreleased]

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
