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
