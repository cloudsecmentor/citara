# Citara

**A local-first, citation-backed knowledge backend that any AI agent can query over MCP.**

[![CI](https://github.com/cloudsecmentor/citara/actions/workflows/ci.yml/badge.svg)](https://github.com/cloudsecmentor/citara/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: PolyForm Noncommercial](https://img.shields.io/badge/license-PolyForm%20Noncommercial-lightgrey.svg)](LICENSE)

Citara ingests your notes and podcast transcripts, preserves their source metadata, generates
embeddings, and serves **citation-backed context** to AI agents through an
[MCP](https://modelcontextprotocol.io/) server and a FastAPI HTTP API. Every passage it returns
carries its source, its transcript timestamp, and a clickable link back to the original — so you can
check the answer instead of trusting it.

It runs entirely on your machine. Your corpus stays in your own SQLite or Postgres database, and the
default embedding provider makes no network calls at all.

```
   your notes + podcasts  ──▶  ingest & chunk  ──▶  embed  ──▶  hybrid retrieval
                                                                      │
                          Claude / Cursor / your agent  ◀── MCP ──────┘
                                                     (text + source + timestamp URL)
```

## Table of contents

- [Why Citara](#why-citara)
- [Quick start](#quick-start)
- [Connect an AI agent (MCP)](#connect-an-ai-agent-mcp)
- [HTTP API](#http-api)
- [Ingesting content](#ingesting-content)
- [Retrieval features](#retrieval-features)
- [Configuration](#configuration)
- [Operations](#operations)
- [Limitations](#limitations)
- [Project information](#project-information)

## Why Citara

Most "chat with your documents" tools give you an answer and leave you to trust it. Citara is built
around the opposite assumption: **the citation is the product.**

- **Citation-backed by design.** Retrieval returns the verbatim source text, its source record, and
  `start_ms`/`end_ms` timestamps — never a paraphrase. For podcasts it builds clickable deep links
  such as `https://www.bemadiscipleship.com/35?t=360`.
- **Local-first and private.** SQLite or Postgres on your own machine. The default embedding
  provider is deterministic and offline, so tests and local development never touch the network.
- **Agent-agnostic.** MCP stdio tools work with Claude, Cursor, or anything you write yourself.
  Citara returns evidence; your agent composes the prose.
- **Source fidelity over convenience.** Transcript timing, episode GUIDs, publishers, and
  provenance survive ingestion instead of being flattened into an undifferentiated vector blob.
- **Your corpus is yours.** Data lives outside the code repo in a directory you control, and the
  schema is plain SQL you can query directly.

### What it does today

| Area | Capability |
| --- | --- |
| Ingestion | Text/markdown, podcast RSS (`podcast:transcript`), manual transcripts via API/MCP |
| Transcripts | VTT + HTML normalization, approximate timestamps for untimed transcripts, clickable timestamp links |
| Storage | SQLite or Postgres + pgvector, Alembic migrations |
| Embeddings | Deterministic offline provider, OpenAI, Azure AI Foundry / Azure OpenAI |
| Retrieval | BM25 keyword (SQLite FTS5), vector, and hybrid (reciprocal rank fusion) with per-source weights |
| Language | Unicode-aware tokenization, dependency-free language detection, cross-language query translation |
| Interfaces | MCP stdio server, FastAPI HTTP API, Docker Compose runtime |

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/cloudsecmentor/citara.git
cd citara
uv sync --extra dev
uv run pytest -q
```

Create the database. The built-in defaults already write to `../citara-data`, outside the repo, so
this works with no configuration at all:

```bash
uv run alembic upgrade head
```

To change any of it, copy `.env.example` to `.env` — it is loaded automatically. Real environment
variables take precedence, so an explicit export still overrides the file, and
`CITARA_SKIP_DOTENV=1` ignores it entirely:

```bash
cp .env.example .env
# Optional: exporting still works and wins over .env
set -a; source .env; set +a
```

Start the API and add your first note:

```bash
uv run uvicorn citara.adapters.api.main:app --port 8000
```

```bash
curl -X POST http://127.0.0.1:8000/sources/text \
  -H 'content-type: application/json' \
  -d '{"title":"Example note","text":"Cats are excellent local-first test subjects."}'

curl "http://127.0.0.1:8000/search?q=feline&mode=hybrid"
```

Prefer containers? `docker compose up -d postgres api` runs migrations and starts the API on the
same port.

> **Where your data lives.** Citara keeps your corpus *outside* the checkout so transcripts, audio,
> and database files can never be committed by accident. The default is a sibling `../citara-data/`
> directory; set `CITARA_DATA_ROOT` to put it anywhere else.

## Connect an AI agent (MCP)

Any MCP client — Claude, Cursor, or your own agent — can launch the server over stdio:

```bash
uv run citara-mcp
```

Equivalent module form:

```bash
uv run python -m citara.adapters.mcp.stdio
```

### MCP tools

| Tool | Purpose |
| --- | --- |
| `search_knowledge` | Search the corpus; `mode="keyword"`, `"vector"`, or `"hybrid"` |
| `retrieve_context_pack` | Citation-ready passages with timestamps and provenance |
| `resolve_source` | Find a source by fuzzy name, preferring current over legacy versions |
| `get_source_summary_context` | All chunks for one source, in transcript order |
| `resolve_summary_context` | Resolve and fetch summary context in one call |
| `add_text_source` | Ingest a note or document |
| `add_transcript_source` | Ingest a timestamped transcript |
| `list_sources` / `delete_source` | Inspect and remove sources |
| `set_source_preference` | Set retrieval weight and preference label |
| `list_entities` / `get_source_entities` | Discover people/organizations and source links |
| `list_ingestion_jobs` / `get_ingestion_job_status` | Ingestion job status |
| `ping` | Health check |

## HTTP API

```bash
curl http://127.0.0.1:8000/health
```

Add a text source:

```bash
curl -X POST http://127.0.0.1:8000/sources/text \
  -H 'content-type: application/json' \
  -d '{"title":"Example note","text":"Cats are excellent local-first test subjects."}'
```

Search in any of the three retrieval modes:

```bash
curl "http://127.0.0.1:8000/search?q=feline&mode=keyword"
curl "http://127.0.0.1:8000/search?q=feline&mode=vector"
curl "http://127.0.0.1:8000/search?q=feline&mode=hybrid"
```

Retrieve a context pack:

```bash
curl "http://127.0.0.1:8000/context-pack?q=feline&mode=hybrid&limit=5"
```

Ingestion endpoints record inline job status rows:

```bash
curl http://127.0.0.1:8000/jobs
curl http://127.0.0.1:8000/jobs/<job_id>
```

### Whole-episode summaries

Context packs are best for targeted questions. Summarizing a whole episode uses a different path:
resolve the source first, then fetch all of its chunks in transcript order.

```bash
# Resolve, preferring the current version when current and legacy both match
curl "http://127.0.0.1:8000/sources/resolve?q=BEMA%2010&preference=current"

# Fetch ordered summary context by source ID
curl "http://127.0.0.1:8000/sources/<source_id>/summary-context"

# Or do both in one call
curl "http://127.0.0.1:8000/sources/summary-context?q=BEMA%2010&preference=current"
```

The response contains source metadata, ordered chunks, `start_ms`/`end_ms`, and clickable
`timestamp_url` citations. Chat clients should summarize from chunks in `chunk_index` order and cite
key claims with the returned timestamp URLs.

## Ingesting content

### Podcast transcripts from RSS

Ingest episodes from any feed that exposes `podcast:transcript` metadata:

```bash
scripts/ingest_podcast_transcripts.py "https://pythonbytes.fm/episodes/rss" --count 2
```

This fetches transcript files, normalizes them into timestamped segments, and posts them to
`POST /sources/transcript`.

### Configured podcast connectors

Source-specific behavior lives in connector modules under `citara.connectors.podcasts`, while
`scripts/podcast_pipeline.py` is the config-driven entrypoint. Copy `citara.sources.example.json` to
an untracked `citara.sources.json` and edit your source list there.

```bash
uv run python scripts/podcast_pipeline.py --config citara.sources.json discover bibleproject
uv run python scripts/podcast_pipeline.py --config citara.sources.json status bibleproject
uv run python scripts/podcast_pipeline.py --config citara.sources.json import-published bibleproject
```

Transcribe missing audio locally with `faster-whisper`:

```bash
uv pip install faster-whisper
uv run python scripts/podcast_pipeline.py --config citara.sources.json transcribe-missing bibleproject \
  --model small \
  --device cpu \
  --compute-type int8
```

The same entrypoint drives every configured connector:

```bash
uv run python scripts/podcast_pipeline.py --config citara.sources.json status bema
uv run python scripts/podcast_pipeline.py --config citara.sources.json transcribe-missing bema --model small --device cpu --compute-type int8

uv run python scripts/podcast_pipeline.py --config citara.sources.json status textinus
uv run python scripts/podcast_pipeline.py --config citara.sources.json transcribe-missing textinus --model small --device cpu --compute-type int8
```

Connectors are sequential and resumable. They store state under `SOURCE_STATE_ROOT` and artifacts
under `SOURCE_ARTIFACT_ROOT`, which default to `../citara-data/import-state` and
`../citara-data/source-artifacts` outside the code repo. If stopped, rerun the same command and it
continues from the first unfinished episode.

<details>
<summary><strong>Remote transcription workers</strong> — offload CPU-heavy transcription over SSH</summary>

Download the episode audio locally first, stage the MP3 on the worker, transcribe there, copy the
JSON artifacts back, and delete the worker's audio. This avoids worker-side media download stalls
while keeping CPU-heavy transcription off your laptop.

The worker is deployment-specific, so there is no default. Configure it with `CITARA_WORKER_SSH` or
pass `--worker`:

```bash
export CITARA_WORKER_SSH=user@your-worker-host
export CITARA_WORKER_SSH_KEY=~/.ssh/id_ed25519
```

Durable generated-transcript artifacts use this layout:

```text
e365-oai-raw.json          # provider-like raw Whisper output with fine segments
e365-oai-raw-chunked.json  # sentence-aware ~1.8k-char import chunks with overlap + metadata.start/episode/url
e365-transcribe-stats.json # timing/throughput stats from the worker
```

Do **not** make `*-oai-raw-chunked.json` one entry per Whisper segment. Keep raw Whisper segments in
`*-oai-raw.json`; use the chunked file for database import so chunks are larger semantic passages
with timestamp starts. Chunked artifacts should prefer sentence/segment boundaries and include a
small overlap from the previous chunk; `metadata.start` should still point to the first non-overlap
segment for citation accuracy.

```bash
uv run python scripts/transcribe_bema_remote_batch.py --start 365 --end 365
uv run python scripts/import_bema_artifacts.py \
  --skip-published-pages \
  --rewrite-openai-chunked \
  --rewrite-start 365 \
  --rewrite-end 365 \
  --replace-generated-openai \
  --openai-raw ../citara-data/source-artifacts/bema/remote-openai
```

For deployments where the API container is used but source provenance should be written directly to
Postgres, provide `DATABASE_URL`. It will best-effort annotate imported sources with transcript URL,
episode GUID, duration, and transcript provenance metadata.

</details>

Podcast citations include clickable timestamp links when a chunk has `start_ms` and the source has a
`canonical_url`:

```text
https://www.bemadiscipleship.com/35?t=360
```

For transcripts without native timing, approximate timestamps are generated proportionally to
character position in the transcript.

## Retrieval features

### Source preferences and retrieval weights

Sources can carry a retrieval preference in `sources.metadata_json`:

```json
{
  "retrieval_weight": 2.0,
  "preference_label": "current"
}
```

Retrieval multiplies keyword and vector scores by `retrieval_weight`, letting you prefer newer or
authoritative sources while keeping legacy versions searchable. For example, prefer a current BEMA
episode over its legacy version when both mention the same idea:

```bash
curl -X PATCH http://127.0.0.1:8000/sources/<current_source_id>/preference \
  -H 'content-type: application/json' \
  -d '{"retrieval_weight":2.0,"preference_label":"current"}'

curl -X PATCH http://127.0.0.1:8000/sources/<legacy_source_id>/preference \
  -H 'content-type: application/json' \
  -d '{"retrieval_weight":0.7,"preference_label":"legacy"}'
```

Weights must be greater than zero. The default is `1.0`.

### Source entities: people and organizations only

Citara has a deliberately small explicit relationship layer:

```text
entities          # canonical person/organization rows
entity_aliases    # spelling/name aliases such as "Tim Mackey" -> tim-mackie
source_entities   # source-level links: source -> person/org with a role
```

The boundary is intentional: **only people and organizations are modeled as entities.** Topics,
themes, scripture references, theology concepts, and series-level ideas stay in transcript text and
are handled by keyword/vector/hybrid retrieval rather than graph tables.

Import payloads may include source-level entities:

```json
{
  "entities": [
    {"type": "organization", "slug": "bema-discipleship", "label": "BEMA Discipleship", "role": "publisher"},
    {"type": "person", "slug": "marty-solomon", "label": "Marty Solomon", "role": "host"}
  ]
}
```

Search and context-pack calls can filter by entity while the query text stays thematic:

```bash
curl 'http://127.0.0.1:8000/search?q=Sabbath%20rest&entity=person:marty-solomon'
curl 'http://127.0.0.1:8000/context-pack?q=exile&entity=organization:bema-discipleship'
```

MCP tools accept the same list as `entity_slugs`, for example
`entity_slugs=["person:marty-solomon"]`, and expose `list_entities` plus `get_source_entities` for
discovery and provenance.

### Multilingual queries

`search_knowledge` and `retrieve_context_pack` accept a query in any language. If the corpus is
English (the common case) and the query is not, the **preferred** path costs nothing: pass
`query_translated` with an English translation alongside the original `query` — the calling agent is
already a multilingual LLM holding the user's turn. Citara searches both the original and translated
query and fuses the results by rank, so proper nouns and terms that only survive untranslated (show
names, transliterations) aren't lost.

If the client doesn't supply a translation, Citara falls back to a server-side `TranslationProvider`
— a no-op by default, so local, offline, and test runs never touch the network, and optionally
backed by the same OpenAI/Azure credentials as the embedding provider.

`retrieve_context_pack` also returns `response_language` (the language to answer the user in) and a
short `response_language_directive`, since Citara returns evidence, not prose — the calling agent
composes the reply. Retrieved `chunks[].text` is always the verbatim, untranslated source string;
pass `translate_quotes=true` to additionally receive `chunks[].text_translated` and
`translation_provenance`. Both tools include a `notice` field
(`{"code": "cross_language_query", ...}`) when a query's language has no matching corpus sources, so
an empty result set is explainable rather than silent.

## Configuration

Citara reads configuration from the process environment, and loads a `.env` file into it at import
time to fill any gaps. Precedence is: real environment variable → `.env` → built-in default. Point
`CITARA_ENV_FILE` at a different file, or set `CITARA_SKIP_DOTENV=1` to disable the file entirely.

Settings bind when `citara.core.config` is first imported, so a variable set programmatically must
be set before importing `citara`.

`.env.example` is the full reference; the essentials:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CITARA_DATA_ROOT` | `../citara-data` | Sets database, artifact, state, and object-store paths at once |
| `DATABASE_URL` | SQLite in the data root | SQLite or `postgresql+psycopg://…` |
| `SOURCE_ARTIFACT_ROOT` | `<data root>/source-artifacts` | Durable transcript/import artifacts |
| `SOURCE_STATE_ROOT` | `<data root>/import-state` | Resumable connector state |
| `EMBEDDING_PROVIDER` | `local` | `local`, `openai`, or `azure_foundry` |
| `TRANSLATION_PROVIDER` | `noop` | `noop`, `openai`, or `azure_foundry` |

### Embedding providers

Verify whichever provider is configured:

```bash
scripts/verify_embeddings.py "embedding smoke test"
```

```bash
# Offline deterministic provider — used by tests and default local dev
export EMBEDDING_PROVIDER=local

# OpenAI
export EMBEDDING_PROVIDER=openai
export EMBEDDING_MODEL=text-embedding-3-small
export OPENAI_API_KEY=...

# Azure AI Foundry / Azure OpenAI-compatible
export EMBEDDING_PROVIDER=azure_foundry
export AZURE_OPENAI_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
export AZURE_OPENAI_DEPLOYMENT=text-embedding-3-small
export AZURE_OPENAI_API_VERSION=2024-02-01
export AZURE_OPENAI_API_KEY=...
```

### Translation providers

Used only when a client calls `search_knowledge`/`retrieve_context_pack` without its own
`query_translated`:

```bash
# Default: no-op, no network calls
export TRANSLATION_PROVIDER=noop

# OpenAI-backed fallback translation
export TRANSLATION_PROVIDER=openai
export TRANSLATION_MODEL=gpt-4o-mini
export OPENAI_API_KEY=...

# Azure AI Foundry / Azure OpenAI-compatible
export TRANSLATION_PROVIDER=azure_foundry
export AZURE_OPENAI_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
export AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
export AZURE_OPENAI_API_VERSION=2024-02-01
export AZURE_OPENAI_API_KEY=...
```

Never commit provider credentials, `.env` files, `.azure/`, database dumps, or ingested third-party
content.

## Operations

### Local development

```bash
uv sync --extra dev
uv run pytest -q
```

Optional local hooks:

```bash
uv run pre-commit install
uv run pre-commit install --hook-type pre-push
```

### Database migrations

Alembic is the schema source of truth for persistent databases.

```bash
# Create or update the local development database
uv run alembic upgrade head

# Create a new migration after changing SQLAlchemy models
uv run alembic revision --autogenerate -m "describe change"

# Docker Compose runs migrations before starting the API
docker compose up -d postgres api
```

### Reset the local Docker database

The Docker Postgres database lives in the named volume `citara_postgres_data`. To start completely
fresh:

```bash
docker compose down -v
docker compose up -d postgres api
```

This deletes all ingested sources, transcript segments, chunks, embeddings, and ingestion jobs.
Alembic recreates an empty schema when the API starts.

Back up before resetting, and restore when needed:

```bash
docker compose exec -T postgres pg_dump -U citara citara > backup.sql
docker compose exec -T postgres psql -U citara -d citara < backup.sql
```

Verify a reset:

```bash
docker compose exec -T postgres psql -U citara -d citara -tAc \
"select version_num from alembic_version; select count(*) from sources; select count(*) from transcript_segments; select count(*) from chunks; select count(*) from embeddings;"
```

Expected result after reset:

```text
20260629_0003
0
0
0
0
```

### Corpus maintenance

Always preview destructive work first:

```bash
uv run python scripts/citara_maintenance.py reset --dry-run \
  --remove-tree python-bytes \
  --reset-sqlite \
  --reset-docker-db
```

Then execute only after reviewing the printed actions:

```bash
uv run python scripts/citara_maintenance.py reset --yes \
  --remove-tree python-bytes \
  --reset-sqlite \
  --reset-docker-db
```

Rebuild the external source-artifact tree and manifest from available local artifacts:

```bash
uv run python scripts/organize_source_artifacts.py
```

The organizer reads `SOURCE_ARTIFACT_ROOT` and `SOURCE_STATE_ROOT`, defaulting to the sibling
`../citara-data` tree.

## Limitations

Citara is `0.x` software. Known gaps, stated plainly:

- Raw audio transcription is not built into the service; transcription runs through the
  `faster-whisper` pipeline scripts.
- PDF, OCR, screenshots, and general web-article ingestion are intentionally deferred.
- Hybrid retrieval fuses keyword and vector rankings with reciprocal rank fusion; cross-encoder
  reranking is not implemented, and the fusion is unweighted (keyword and vector contribute
  equally).
- BM25 keyword ranking is served by SQLite's FTS5 index. Postgres has no native BM25, so it falls
  back to a full-scan BM25 implementation — correct, and comparably ranked, but O(corpus) per
  query. SQLite is the better-supported backend today.
- Embeddings are not multilingual. The default deterministic provider and `EMBEDDING_DIMENSIONS` are
  English-tuned; cross-language retrieval works via query translation and fusion, not a shared
  multilingual vector space. There is no curated proper-noun glossary.
- Multi-user and hosted/SaaS deployment are not production-ready.
- Podcast timestamps for untimed transcripts are approximate, proportional to transcript character
  offsets.
- This repository contains no third-party podcast transcripts or audio.

### Data and content responsibility

You are responsible for ensuring you have the right to ingest, store, process, and use any external
content. Do not redistribute third-party transcripts, audio, PDFs, or other copyrighted material
without permission from the content owner.

## Project information

### Design documents

- [Ingestion contract](docs/INGESTION_CONTRACT.md)
- [Source artifact storage design](docs/SOURCE_ARTIFACT_STORAGE.md)
- [Original product/architecture idea](docs/IDEA.md)
- [Current architecture diagram](docs/current-architecture.html)
- [Competitive landscape](docs/comparison.html)

### Versioning

Citara follows [Semantic Versioning](https://semver.org/), with the `0.x` caveat SemVer itself
defines: **while the version is `0.y.z`, minor bumps (`0.y.0`) may contain breaking changes, and
patch bumps (`0.y.z`) never do.** `src/citara/__init__.py`'s `__version__` is the single source of
truth; `pyproject.toml` reads it dynamically (`dynamic = ["version"]`).

The public API this policy covers: MCP tool names, parameters, and response shapes; the HTTP API
(routes, parameters, response shapes); environment-variable configuration names; the database schema
(see `alembic/`); and the CLI (`citara-mcp`). Refactors inside `src/citara/core/` that change none of
the above are not breaking, even between patch releases. Citara reaches `1.0.0` once the MCP tool
surface — the primary integration point for agents — is considered stable.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the changelog convention and release checklist, and
[CHANGELOG.md](CHANGELOG.md) for release history.

### License

This project is **source-available**, not OSI open source.

- Free for personal and noncommercial use under the
  [PolyForm Noncommercial License 1.0.0](LICENSE).
- Commercial use requires explicit written permission from the copyright holder.
- See [COMMERCIAL_USE.md](COMMERCIAL_USE.md) for commercial-use terms.

### Security

Report security issues privately as described in [SECURITY.md](SECURITY.md) rather than opening a
public issue.

### Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Before publishing a fork or release, review `.gitignore`
and `git status --ignored`, and confirm no credentials, private data, local database files, or
third-party ingested content are committed.
