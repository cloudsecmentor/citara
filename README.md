# Hermes Knowledge Vault

Hermes Knowledge Vault is a local-first personal knowledge backend for ingesting notes and podcast transcripts, preserving source metadata, generating embeddings, and retrieving citation-backed context through FastAPI and Hermes MCP tools.

It is designed for personal research workflows where you want to ask questions over your own material while keeping sources, transcript timestamps, and retrieval context inspectable.

## License

This project is **source-available**, not OSI open source.

- Free for personal and noncommercial use under the [PolyForm Noncommercial License 1.0.0](LICENSE).
- Commercial use requires explicit written permission from the copyright holder.
- See [COMMERCIAL_USE.md](COMMERCIAL_USE.md) for commercial-use terms.

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
- Keyword, vector, and hybrid retrieval.
- Citation/context-pack output.
- FastAPI HTTP API.
- Hermes MCP stdio tools.
- Alembic migrations.
- Docker Compose local runtime.

## Limitations

- Raw audio transcription is not implemented yet.
- PDF, OCR, screenshots, and general web article ingestion are intentionally deferred.
- Retrieval ranking is basic hybrid search; reranking is not implemented.
- Multi-user and hosted/SaaS deployment are not production-ready.
- Podcast timestamps for untimed transcripts are approximate and proportional to transcript character offsets.
- This repository does not include third-party podcast transcripts or audio.

## Data and content responsibility

Users are responsible for ensuring they have the right to ingest, store, process, and use any external content. Do not redistribute third-party transcripts, audio, PDFs, or other copyrighted content unless you have permission from the content owner.

## Local development

Install dependencies and run tests:

```bash
uv sync
uv run pytest -q
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
hermes-knowledge-vault_postgres_data
```

To start with a completely fresh database, delete the volume and restart the services:

```bash
docker compose down -v
docker compose up -d postgres api
```

This deletes all ingested sources, transcript segments, chunks, embeddings, and ingestion jobs. Alembic recreates an empty schema when the API starts.

Verify the reset:

```bash
docker compose exec -T postgres psql -U hermes -d hermes_kv -tAc \
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
docker compose exec -T postgres pg_dump -U hermes hermes_kv > backup.sql
```

Restore a backup:

```bash
docker compose exec -T postgres psql -U hermes -d hermes_kv < backup.sql
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

### BibleProject import and transcription pipeline

BibleProject uses a Simplecast RSS feed with official transcript PDFs for some episodes and audio-only entries for the rest:

```bash
uv run python scripts/bibleproject_pipeline.py discover
uv run python scripts/bibleproject_pipeline.py status
uv run python scripts/bibleproject_pipeline.py import-published
```

The pipeline is resumable. It stores state under `data/import-state/bibleproject_pipeline_state.json` and artifacts under `data/import-artifacts/bibleproject/`. Re-running a command checks state and skips episodes already imported.

To sequentially transcribe missing audio locally with `faster-whisper`:

```bash
uv pip install faster-whisper
uv run python scripts/bibleproject_pipeline.py transcribe-missing \
  --model small \
  --device cpu \
  --compute-type int8
```

Stop with `Ctrl+C`; the current episode is marked interrupted/error and the next run resumes from the first unfinished episode. On a CUDA-capable VM, use options such as `--device cuda --compute-type float16`.

For deployments where the API container is used but source provenance should be written directly to Postgres, provide `DATABASE_URL` to the script. It will best-effort annotate imported sources with `transcript_url`, episode GUID, duration, and transcript provenance metadata.

### BEMA and Text in Us transcription queues

BEMA published transcripts were already imported separately. The BEMA pipeline is therefore focused on the remaining episodes that need generated transcription. It reads the BEMA RSS feed, checks the vault for any existing `BEMA <episode>:` source, and queues only episodes not already represented in the DB:

```bash
uv run python scripts/bema_pipeline.py discover
uv run python scripts/bema_pipeline.py status
uv run python scripts/bema_pipeline.py transcribe-missing --model small --device cpu --compute-type int8
```

Text in Us currently exposes no published transcript links in its Anchor/Spotify RSS feed, so its pipeline is transcription-first:

```bash
uv run python scripts/textinus_pipeline.py discover
uv run python scripts/textinus_pipeline.py status
uv run python scripts/textinus_pipeline.py transcribe-missing --model small --device cpu --compute-type int8
```

Both scripts are sequential and resumable. They store state under `data/import-state/` and artifacts under `data/import-artifacts/`. If stopped, rerun the same command and it continues from the first unfinished episode.

Podcast citations include clickable timestamp links when a chunk has `start_ms` and the source has `canonical_url`:

```text
https://www.bemadiscipleship.com/35?t=360
```

For transcripts without native timing, approximate timestamps can be generated proportionally to character position in the transcript.

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

## MCP stdio server

Hermes can launch the MCP server over stdio with:

```bash
uv run hermes-kv-mcp
```

Equivalent module form:

```bash
uv run python -m hermes_knowledge.adapters.mcp.stdio
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
