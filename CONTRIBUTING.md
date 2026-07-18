# Contributing

Thank you for your interest in Citara.

This project is source-available under the PolyForm Noncommercial License 1.0.0. By contributing, you agree that your contribution may be distributed under the project license and any future commercial licenses granted by the maintainer.

## Development setup

```bash
uv sync --extra dev
uv run pytest -q
```

Install optional local hooks:

```bash
uv run pre-commit install
uv run pre-commit install --hook-type pre-push
```

Run the Docker stack:

```bash
docker compose up -d postgres api
curl http://127.0.0.1:8000/health
```

## Contribution guidelines

- Keep changes small and focused.
- Add or update tests for behavior changes.
- Do not commit credentials, local DB files, or ingested third-party content.
- Prefer deterministic fixtures over live network calls in tests.
- Update README/docs when user-facing behavior changes.

## Licensing of contributions

Commercial use of this project requires explicit written permission. Do not submit code that you cannot license to this project under those terms.
