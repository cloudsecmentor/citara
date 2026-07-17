# Development TODO

Prioritized next steps toward a clean public (source-available) release and beyond.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## P0 — Before the first public push (blockers)

- [x] **Finish the in-flight refactor.** Connector migration committed (`4fdbf17`); working tree clean.
- [x] **Rebrand to `Citara`** (agent-agnostic). Package, imports, console script, docker/compose/env/alembic, and docs renamed; `uv.lock` regenerated. `uv run pytest -q` (61 passing) and `uv run alembic upgrade head` (fresh DB) both green.
- [x] **Migrated the local corpus** to `../citara/` (`citara.db`, `source-artifacts/`, `import-state/`); `../hkb` removed. No `.env`, so defaults resolve correctly.
- [x] **Renamed** local `hkb.sources.json` → `citara.sources.json` (still gitignored).
- [x] **Published to GitHub**: pushed to `cloudsecmentor/citara` (currently **private**). Local folder stays `hermes-knowledge-vault` because `../citara` is the data dir (can't be both).
  - [ ] Flip to public when P0 is done: `gh repo edit cloudsecmentor/citara --visibility public`.
- [ ] **Note:** `organization-manifest.json` was reset to empty because `scripts/organize_source_artifacts.py` rebuilds it from the repo `data/` staging dir (now empty). The manifest is an audit-only index (not used by the app/tests), and the underlying artifacts are intact. Consider adding a "rebuild manifest from the existing `source-artifacts/` tree" mode.
- [x] **Verified no ignored artifacts published** (`.db`, `.azure/`, `.hermes/`, `citara.sources.json`, `data/` all gitignored).
- [x] **Added security contact** to `SECURITY.md`: `cloudsecmentor+citara@gmail.com`.
- [x] **Updated `pyproject.toml` package metadata** with Citara maintainer contact and `[project.urls]` homepage / repository / issues / security link.

## P1 — Repo hygiene & contributor experience

- [x] **Isolate tests from the real DB.** `tests/conftest.py` now forces `DATABASE_URL` onto a throwaway temp SQLite DB before importing `citara`, so the suite no longer reads/writes the user's real corpus.
- [x] **Add CI** (`.github/workflows/ci.yml`): ruff lint/format check, mypy, pytest, and an Alembic upgrade check on a fresh SQLite DB, on push/PR across Python 3.11/3.12/3.14.
- [x] **Add lint/format/type config** to `pyproject.toml`:
  - [x] `ruff` (lint + format, line-length 140; `E501` off, `B008` allowed for FastAPI `Depends`, `E402` allowed in scripts/conftest). Whole tree linted and formatted; the lint pass surfaced real `NameError` bugs (missing `sys`/`os` imports in connector error paths).
  - [x] `mypy` on `src/` (lenient baseline: `ignore_missing_imports`, `check_untyped_defs`; zero errors).
  - [x] Wire them into CI.
- [ ] **Add `.pre-commit-config.yaml`** (ruff, ruff-format, end-of-file-fixer, trailing-whitespace, mypy optional).
- [ ] **Add `CODE_OF_CONDUCT.md`** (Contributor Covenant) — standard for public repos.
- [ ] **Add issue/PR templates** under `.github/` (bug report, feature request, PR checklist).
- [ ] **Reconcile "open source" wording.** PolyForm Noncommercial is source-available, not OSI. Make sure README, `pyproject` classifiers, and `docs/IDEA.md` (which still says "open-source") are consistent with the actual license.

## P2 — Product / functional gaps

- [ ] **Promote real audio transcription into `core/transcription/` and collapse the per-podcast pipelines.** Today each show has its own near-parallel scripts (`remote_bema_transcribe.py`, `transcribe_bema_remote_batch.py`, `transcribe_bibleproject_remote_batch.py`, plus per-show import scripts), and real Whisper lives inside connectors while `core/transcription/providers.py` only has a fixture provider. Plan:
  - [ ] Move a real transcription provider (faster-whisper local and/or remote batch) behind the existing `core/transcription/` protocol.
  - [ ] Build one generic batch driver (discover → download → transcribe → publish with taxonomy metadata) that connectors feed with show-specific config only.
  - [ ] Reduce `scripts/transcribe_*_remote_batch.py` and per-show import scripts to thin wrappers or delete them.
  - [ ] Removes the README's first listed limitation ("Raw audio transcription is not implemented yet").
- [x] **Hybrid retrieval score fusion fixed**: keyword/vector rankings now merge via reciprocal rank fusion (k=60) instead of adding raw scores on incomparable scales.
- [ ] **Reranking** for retrieval (cross-encoder or LLM reranker layered on top of RRF-fused hybrid results).
- [ ] **Implement or remove Docker stubs.** `worker` and `frontend` services in `docker-compose.yml` are print-stubs — either implement a minimal version or clearly mark/remove them so first-run `docker compose up` isn't confusing.
- [ ] **Deferred ingestion types** from `docs/IDEA.md`: PDF (dep `pymupdf` already present), OCR/screenshots, web article ingestion. Scope one at a time.
- [ ] **A minimal debug web UI** (the IDEA doc calls for one for uploads/inspecting jobs/testing retrieval).

## P3 — Docs & polish

- [ ] **Quickstart that works in <5 min** at the top of README (clone → `uv sync` → `docker compose up -d postgres api` → curl example).
- [ ] **Architecture diagram** kept in sync (there's `docs/current-architecture.html`; consider a Mermaid diagram in README).
- [ ] **Document the connector authoring flow** (how to add a new podcast/source connector under `src/.../connectors/`).
- [ ] **CHANGELOG.md** + adopt tags/releases (a git version-tag workflow already exists in your tooling).
- [ ] **Screenshots / example citation output** in README to show the value prop.

## P4 — Toward multi-tenant / hosted (longer term, from IDEA.md)

- [ ] Real auth + per-user isolation (models already carry tenant/user; wiring is single-user local).
- [ ] Object store abstraction beyond local (S3/Azure Blob) — `OBJECT_STORE_TYPE` hook exists.
- [ ] Background job queue for the `worker` service.
- [ ] Rate limiting, quotas, upload-size enforcement end-to-end.

---

## Naming (decision + rename checklist)

**Chosen name: `Citara`** (coined from "cite") — agent-agnostic and points at the real
differentiator: *citation-backed retrieval with source fidelity*, not generic "agent memory."

Availability (checked): PyPI `citara` free, npm `citara` free, no GitHub software project by this
name in the knowledge/memory/RAG space. (Two unrelated IT-consulting firms use `citara.io`/`.us`,
so prefer `citara.dev`/`.app` for any website.)

Rejected `Mneme`/`Mnemo`: both are heavily taken by near-identical tools (Joshwani/mneme,
MnemeHQ/mneme, Mnemo-mcp/Mnemo, MnemoAI/mnemo), with the PyPI names already published.

Positioning: *Citara is a local-first, citation-backed personal knowledge vault that any AI agent
can query over MCP — a source-faithful context backend for agents.*

Proposed concrete scheme when renaming:
- Python package: `citara` (from `hermes_knowledge`)
- Console script: `citara-mcp` (from `hermes-kv-mcp`)
- Default DB file: `citara.db`; data dir: `citara/` (from `hkb/`)

Rename status (applied):
- [x] `pyproject.toml`: `name` → `citara`, console script `citara-mcp`, wheel package `src/citara`.
- [x] Python package dir `src/hermes_knowledge/` → `src/citara/` (all imports updated).
- [x] MCP server name (`FastMCP("citara")`) and stdio entrypoint.
- [x] `README.md`, `docs/*`, `NOTICE`, `COMMERCIAL_USE.md`, `SECURITY.md`, `CONTRIBUTING.md`.
- [x] `docker-compose.yml` (Postgres db/user/pass → `citara`), `Dockerfile`.
- [x] `.env.example`, `alembic.ini`, data dir `hkb` → `citara`, DB `hermes_knowledge_vault.db` → `citara.db`.
- [x] Renamed `scripts/hkb_maintenance.py` → `scripts/citara_maintenance.py` (+ test) and `hkb.sources.example.json` → `citara.sources.example.json`.
- [x] `uv.lock` regenerated (`citara v0.1.0`).
- [x] README carries a "formerly Hermes Knowledge Vault" note; `docs/IDEA.md` has a historical-rename disclaimer.
- [ ] Rename the GitHub repo itself (see P0).
- [x] `pyproject.toml`: maintainer/security contact + `[project.urls]` added.
