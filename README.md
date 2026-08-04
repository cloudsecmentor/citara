# Citara

Citara is a local-first personal knowledge backend for ingesting notes and podcast transcripts, preserving source metadata, generating embeddings, and retrieving citation-backed context through FastAPI and MCP tools that any AI agent can call.

It is designed for personal research workflows where you want to ask questions over your own material while keeping sources, transcript timestamps, and retrieval context inspectable.

## License

This project is **source-available**, not OSI open source.

- Free for personal and noncommercial use under the [PolyForm Noncommercial License 1.0.0](LICENSE).
- Commercial use requires explicit written permission from the copyright holder.
- See [COMMERCIAL_USE.md](COMMERCIAL_USE.md) for commercial-use terms.

## Versioning

Citara follows [Semantic Versioning](https://semver.org/), with the `0.x` caveat SemVer itself
defines: **while the version is `0.y.z`, minor bumps (`0.y.0`) may contain breaking changes, and
patch bumps (`0.y.z`) never do.** `src/citara/__init__.py`'s `__version__` is the single source of
truth; `pyproject.toml` reads it dynamically (`dynamic = ["version"]`).

The public API this policy covers:

- MCP tool names, parameters, and response shapes.
- The HTTP API (routes, parameters, response shapes).
- Environment-variable configuration names.
- The database schema (see `alembic/`).
- The CLI (`citara-mcp`).

Refactors inside `src/citara/core/` that don't change any of the above are not breaking, even
between patch releases. Citara reaches `1.0.0` once the MCP tool surface -- the primary
integration point for agents -- is considered stable.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the changelog convention and release checklist.

## Current capabilities

- Text/markdown source ingestion.
- Podcast transcript ingestion from RSS feeds that expose `podcast:transcript` metadata.
- VTT and HTML transcript normalization.
- Manual transcript ingestion through API/MCP.
- Approximate timestamp generation for untimed transcripts.
- Clickable podcast timestamp links such as `https://example.com/episode?t=360`.
- Source preference/retrieval weights, e.g. prefer a current episode over a legacy version.
- Postgres + pgvector storage.
- Deterministic local embeddings for offline testing.
- OpenAI embedding provider.
- Azure AI Foundry / Azure OpenAI-compatible embedding provider.
- Keyword, vector, and hybrid retrieval (reciprocal rank fusion).
- Unicode-aware tokenization and dependency-free language detection (Latin, Cyrillic, Hebrew,
  Arabic, CJK, Hangul, Devanagari scripts), with a `notice` diagnostic when a query's language has
  no matching corpus sources instead of a silent empty result.
- Query translation for cross-language retrieval: pass `query_translated` (preferred) or let a
  configured `TranslationProvider` fall back server-side; both the original and translated query
  are searched and fused by rank. `retrieve_context_pack` reports `response_language` and, on
  request, verbatim-preserving `text_translated` quotes.
- Citation/context-pack output.
- FastAPI HTTP API.
- MCP stdio tools (agent-agnostic).
- Alembic migrations.
- Docker Compose local runtime.

## Limitations

- Raw audio transcription is not implemented yet.
- PDF, OCR, screenshots, and general web article ingestion are intentionally deferred.
- Hybrid retrieval fuses keyword and vector rankings with reciprocal rank fusion; cross-encoder reranking is not implemented.
- Embeddings are not multilingual (the default deterministic provider and `EMBEDDING_DIMENSIONS`
  are English-tuned); cross-language retrieval works today via query translation and fusion, not
  a shared multilingual vector space. A curated proper-noun glossary is not implemented either.
- Multi-user and hosted/SaaS deployment are not production-ready.
- Podcast timestamps for untimed transcripts are approximate and proportional to transcript character offsets.
- This repository does not include third-party podcast transcripts or audio.

## Data and content responsibility

Users are responsible for ensuring they have the right to ingest, store, process, and use any external content. Do not redistribute third-party transcripts, audio, PDFs, or other copyrighted content unless you have permission from the content owner.

## Design documents

- [Ingestion contract](docs/INGESTION_CONTRACT.md)
- [Source artifact storage design](docs/SOURCE_ARTIFACT_STORAGE.md)
- [Original product/architecture idea](docs/IDEA.md)
- [Current architecture diagram](docs/current-architecture.html)
- [Competitive landscape](docs/comparison.html)

## Local development

Install dependencies and run tests:

```bash
uv sync --extra dev
uv run pytest -q
```

Optional local hooks:

```bash
uv run pre-commit install
uv run pre-commit install --hook-type pre-push
```

## Database migrations

Alembic is the schema source of truth for persistent databases.

Create or update the local SQLite development database:

```bash
uv run alembic upgrade head
```

Create a new migration after changing SQLAlchemy models:

```bash
uv run alembic revision --autogenerate -m "describe change"
```

Docker Compose runs migrations before starting the API:

```bash
docker compose up -d postgres api
```

### Reset the local Docker database

The Docker Postgres database is stored in the named volume:

```text
citara_postgres_data
```

To start with a completely fresh database, delete the volume and restart the services:

```bash
docker compose down -v
docker compose up -d postgres api
```

This deletes all ingested sources, transcript segments, chunks, embeddings, and ingestion jobs. Alembic recreates an empty schema when the API starts.

For safer source-artifact and DB maintenance, preview destructive work first:

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

The organizer reads `SOURCE_ARTIFACT_ROOT` and `SOURCE_STATE_ROOT`, defaulting to the sibling `../citara` tree.

Verify the reset:

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

Back up before resetting:

```bash
docker compose exec -T postgres pg_dump -U citara citara > backup.sql
```

Restore a backup:

```bash
docker compose exec -T postgres psql -U citara -d citara < backup.sql
```

## Docker smoke check

```bash
curl http://127.0.0.1:8000/health
```

Add a text source:

```bash
curl -X POST http://127.0.0.1:8000/sources/text \
  -H 'content-type: application/json' \
  -d '{"title":"Example note","text":"Cats are excellent local-first test subjects."}'
```

Search supports three retrieval modes:

```bash
curl "http://127.0.0.1:8000/search?q=feline&mode=keyword"
curl "http://127.0.0.1:8000/search?q=feline&mode=vector"
curl "http://127.0.0.1:8000/search?q=feline&mode=hybrid"
```

Retrieve a context pack:

```bash
curl "http://127.0.0.1:8000/context-pack?q=feline&mode=hybrid&limit=5"
```

## Episode/source summaries

Search-oriented context packs are best for targeted questions. Whole-episode
summaries use a different path: first resolve the episode/source, then fetch all
chunks for that source in transcript order.

Resolve a source, preferring the current version when current and legacy both
match:

```bash
curl "http://127.0.0.1:8000/sources/resolve?q=BEMA%2010&preference=current"
```

Fetch ordered summary context by source ID:

```bash
curl "http://127.0.0.1:8000/sources/<source_id>/summary-context"
```

Or resolve and fetch summary context in one call:

```bash
curl "http://127.0.0.1:8000/sources/summary-context?q=BEMA%2010&preference=current"
```

The response contains source metadata, ordered chunks, `start_ms`/`end_ms`, and
clickable `timestamp_url` citations. Chat clients should summarize from chunks in
`chunk_index` order and cite key claims with the returned timestamp URLs.

## Ingestion jobs

Ingestion endpoints record inline job status rows:

```bash
curl http://127.0.0.1:8000/jobs
curl http://127.0.0.1:8000/jobs/<job_id>
```

## Podcast transcript ingestion

Ingest real podcast RSS episodes that expose `podcast:transcript` metadata:

```bash
scripts/ingest_podcast_transcripts.py "https://pythonbytes.fm/episodes/rss" --count 2
```

This fetches transcript files, normalizes them into timestamped segments, and posts them to `POST /sources/transcript`.

### Configured podcast connectors

Podcast source-specific behavior lives in connector modules under `citara.connectors.podcasts`, while `scripts/podcast_pipeline.py` is the config-driven entrypoint. Copy `citara.sources.example.json` to an untracked `citara.sources.json` and edit local source choices there.

BibleProject uses a Simplecast RSS feed with official transcript PDFs for some episodes and audio-only entries for the rest:

```bash
uv run python scripts/podcast_pipeline.py --config citara.sources.json discover bibleproject
uv run python scripts/podcast_pipeline.py --config citara.sources.json status bibleproject
uv run python scripts/podcast_pipeline.py --config citara.sources.json import-published bibleproject
```

To sequentially transcribe missing audio locally with `faster-whisper`:

```bash
uv pip install faster-whisper
uv run python scripts/podcast_pipeline.py --config citara.sources.json transcribe-missing bibleproject \
  --model small \
  --device cpu \
  --compute-type int8
```

BEMA and Text in Us use the same entrypoint with different configured connectors:

```bash
uv run python scripts/podcast_pipeline.py --config citara.sources.json status bema
uv run python scripts/podcast_pipeline.py --config citara.sources.json transcribe-missing bema --model small --device cpu --compute-type int8

uv run python scripts/podcast_pipeline.py --config citara.sources.json status textinus
uv run python scripts/podcast_pipeline.py --config citara.sources.json transcribe-missing textinus --model small --device cpu --compute-type int8
```

Connectors are sequential and resumable. They store state under `SOURCE_STATE_ROOT` and artifacts under `SOURCE_ARTIFACT_ROOT`, which default to sibling `../citara/import-state` and `../citara/source-artifacts` outside the automation repo. If stopped, rerun the same command and it continues from the first unfinished episode.

### BEMA remote transcription artifacts

For remote-worker BEMA transcription, download the episode audio locally first, upload/stage the MP3 to the worker, transcribe on the worker, copy JSON artifacts back, and delete worker audio. This avoids worker-side media download stalls while keeping CPU-heavy transcription off the Mac.

The durable generated-transcript artifacts intentionally match the existing BEMA OpenAI layout under `../citara-data/source-artifacts/bema/remote-openai/`:

```text
e365-oai-raw.json          # provider-like raw Whisper output with fine segments
e365-oai-raw-chunked.json  # sentence-aware ~1.8k-char import chunks with overlap + metadata.start/episode/url
e365-transcribe-stats.json # timing/throughput stats from the worker
```

Do **not** make `*-oai-raw-chunked.json` one entry per Whisper segment. Keep raw Whisper segments in `*-oai-raw.json`; use the chunked file for Citara DB import/retrieval chunks so DB chunks are larger semantic passages with timestamp starts. Chunked artifacts should prefer sentence/segment boundaries and include a small overlap from the previous chunk; `metadata.start` should still point to the first non-overlap segment for citation accuracy.

```bash
uv run python scripts/transcribe_bema_remote_batch.py --start 365 --end 365
uv run python scripts/import_bema_artifacts.py \
  --skip-published-pages \
  --rewrite-openai-chunked \
  --rewrite-start 365 \
  --rewrite-end 365 \
  --replace-generated-openai \
  --openai-raw ../citara/source-artifacts/bema/remote-openai
```

For deployments where the API container is used but source provenance should be written directly to Postgres, provide `DATABASE_URL`. It will best-effort annotate imported sources with transcript URL, episode GUID, duration, and transcript provenance metadata.

Podcast citations include clickable timestamp links when a chunk has `start_ms` and the source has `canonical_url`:

```text
https://www.bemadiscipleship.com/35?t=360
```

For transcripts without native timing, approximate timestamps can be generated proportionally to character position in the transcript.

## Source entities: people and organizations only

Citara has a deliberately small explicit relationship layer:

```text
entities          # canonical person/organization rows
entity_aliases    # spelling/name aliases such as "Tim Mackey" -> tim-mackie
source_entities   # source-level links: source -> person/org with a role
```

The structured boundary is intentional: **only people and organizations are modeled as entities**. Topics, themes, scripture references, theology concepts, and series-level ideas remain in transcript text and are handled by keyword/vector/hybrid retrieval rather than graph tables.

Import payloads may include source-level entities:

```json
{
  "entities": [
    {"type": "organization", "slug": "bema-discipleship", "label": "BEMA Discipleship", "role": "publisher"},
    {"type": "person", "slug": "marty-solomon", "label": "Marty Solomon", "role": "host"}
  ]
}
```

Search and context-pack calls can use entity filters while the query text remains thematic:

```bash
curl 'http://127.0.0.1:8000/search?q=Sabbath%20rest&entity=person:marty-solomon'
curl 'http://127.0.0.1:8000/context-pack?q=exile&entity=organization:bema-discipleship'
```

MCP tools accept the same `entity_slugs` list, for example `entity_slugs=["person:marty-solomon"]`, and expose `list_entities` plus `get_source_entities` for discovery/provenance.

## Source preferences and retrieval weights

Sources can carry a retrieval preference in `sources.metadata_json`:

```json
{
  "retrieval_weight": 2.0,
  "preference_label": "current"
}
```

Retrieval multiplies keyword/vector scores by `retrieval_weight`. This lets you prefer newer or authoritative sources while keeping legacy sources searchable.

Example: prefer a current BEMA episode over its legacy version when both mention the same idea:

```bash
curl -X PATCH http://127.0.0.1:8000/sources/<current_source_id>/preference \
  -H 'content-type: application/json' \
  -d '{"retrieval_weight":2.0,"preference_label":"current"}'

curl -X PATCH http://127.0.0.1:8000/sources/<legacy_source_id>/preference \
  -H 'content-type: application/json' \
  -d '{"retrieval_weight":0.7,"preference_label":"legacy"}'
```

Weights must be greater than zero. The default weight is `1.0`.

## Embedding providers

Verify whichever embedding provider is configured:

```bash
scripts/verify_embeddings.py "embedding smoke test"
```

Provider configuration examples:

```bash
# Offline deterministic provider, used by tests/default local dev
export EMBEDDING_PROVIDER=local

# OpenAI API provider
export EMBEDDING_PROVIDER=openai
export EMBEDDING_MODEL=text-embedding-3-small
export OPENAI_API_KEY=...

# Azure AI Foundry / Azure OpenAI-compatible provider
export EMBEDDING_PROVIDER=azure_foundry
export AZURE_OPENAI_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
export AZURE_OPENAI_DEPLOYMENT=text-embedding-3-small
export AZURE_OPENAI_API_VERSION=2024-02-01
export AZURE_OPENAI_API_KEY=...
```

Do not commit provider credentials, `.env` files, `.azure/`, database dumps, or ingested third-party content.

## Multilingual query support

`search_knowledge` and `retrieve_context_pack` accept a query in any language. If the corpus is
English (the common case) and the query is not, the **preferred** path costs nothing: pass
`query_translated` with an English translation alongside the original `query` -- the calling agent
is already a multilingual LLM holding the user's turn. Citara then searches both the original and
translated query and fuses the results by rank, so proper nouns and terms that only survive
untranslated (e.g. show names, transliterations) aren't lost.

If the client doesn't supply a translation, Citara falls back to a server-side
`TranslationProvider` -- a no-op by default (so local/offline/test runs never touch the network),
optionally backed by the same OpenAI/Azure credentials as the embedding provider:

```bash
# Default: no-op, no network calls.
export TRANSLATION_PROVIDER=noop

# OpenAI-backed fallback translation
export TRANSLATION_PROVIDER=openai
export TRANSLATION_MODEL=gpt-4o-mini
export OPENAI_API_KEY=...

# Azure AI Foundry / Azure OpenAI-compatible fallback translation
export TRANSLATION_PROVIDER=azure_foundry
export AZURE_OPENAI_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
export AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
export AZURE_OPENAI_API_VERSION=2024-02-01
export AZURE_OPENAI_API_KEY=...
```

`retrieve_context_pack` also returns `response_language` (the language to answer the user in) and
a short `response_language_directive`, since Citara returns evidence, not prose -- the calling
agent composes the reply. Retrieved `chunks[].text` is always the verbatim, untranslated source
string; pass `translate_quotes=true` to additionally receive `chunks[].text_translated` and
`translation_provenance` alongside it. Both tools include a `notice` field
(`{"code": "cross_language_query", ...}`) when a query's language has no matching corpus sources,
so an empty result set is explainable rather than silent.

## MCP stdio server

Any MCP client (Claude, Cursor, or your own agent) can launch the MCP server over stdio with:

```bash
uv run citara-mcp
```

Equivalent module form:

```bash
uv run python -m citara.adapters.mcp.stdio
```

Current MCP tools:

```text
ping
add_text_source
add_transcript_source
search_knowledge
retrieve_context_pack
resolve_source
get_source_summary_context
resolve_summary_context
list_sources
delete_source
set_source_preference
list_ingestion_jobs
get_ingestion_job_status
```

`search_knowledge` and `retrieve_context_pack` accept `mode="keyword"`, `mode="vector"`, or `mode="hybrid"`.

## Repository hygiene before publishing

Before making a fork or release public, review:

- [SECURITY.md](SECURITY.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [COMMERCIAL_USE.md](COMMERCIAL_USE.md)
- `.gitignore`
- `git status --ignored`

Ensure no credentials, private data, local database files, or third-party ingested content are committed.
