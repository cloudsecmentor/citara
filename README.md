# Hermes Knowledge Vault

Initial scaffold for the Hermes personal knowledge backend.

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

## Docker smoke check

```bash
curl http://127.0.0.1:8000/health
```

Search supports three retrieval modes:

```bash
curl "http://127.0.0.1:8000/search?q=feline&mode=keyword"
curl "http://127.0.0.1:8000/search?q=feline&mode=vector"
curl "http://127.0.0.1:8000/search?q=feline&mode=hybrid"
```

The current vector-search implementation uses deterministic local test embeddings
by default. Docker uses `pgvector/pgvector:pg16`, and Alembic enables the
`vector` extension before creating the `embeddings` table.

Ingestion endpoints record inline job status rows:

```bash
curl http://127.0.0.1:8000/jobs
curl http://127.0.0.1:8000/jobs/<job_id>
```

Ingest real podcast RSS episodes that expose `podcast:transcript` metadata:

```bash
scripts/ingest_podcast_transcripts.py "https://pythonbytes.fm/episodes/rss" --count 2
```

This fetches transcript files, normalizes them into timestamped segments, and
posts them to `POST /sources/transcript`.

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

Current DEV Azure resource created for Foundry-compatible embeddings:

```text
Subscription: DEV subscription
Resource group: hkv-dev-rg
Location: eastus
Resource: hkv-dev-ai-0e5f1e5a
Endpoint: https://hkv-dev-ai-0e5f1e5a.cognitiveservices.azure.com/
Deployment: text-embedding-3-small
```

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
search_knowledge
retrieve_context_pack
list_sources
delete_source
list_ingestion_jobs
get_ingestion_job_status
```

`search_knowledge` and `retrieve_context_pack` accept `mode="keyword"`,
`mode="vector"`, or `mode="hybrid"`.
