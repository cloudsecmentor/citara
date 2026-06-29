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
```

`search_knowledge` and `retrieve_context_pack` accept `mode="keyword"`,
`mode="vector"`, or `mode="hybrid"`.
