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

## Changelog

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and is maintained
**by hand**, not generated from commit history (this repo's commits are mostly not in Conventional
Commits form, so automated generation would produce a low-signal log). Add your change under the
`## [Unreleased]` section in the same commit/PR that makes the change, in the category that fits
(`Added`, `Changed`, `Fixed`, `Deprecated`, `Removed`, `Security`).

### Data & migrations convention

Every changelog release entry (including `[Unreleased]` while it accumulates changes) must include
a `### Data & migrations` subsection stating, explicitly, even when the answer is "no":

- Whether `alembic upgrade head` is required for this release.
- Whether the database schema/stored data remains backward compatible with the previous release.
- Whether a corpus re-index or re-embed is needed (e.g. because the embedding model or
  `EMBEDDING_DIMENSIONS` changed).

Citara is local-first and users own their corpus database directly, so this is the one thing a
release note must never leave implicit. If none of the three apply, say so
(e.g. "No migration required; DB stays backward compatible; no re-embed needed.") rather than
omitting the subsection.

## Release checklist

1. Confirm `main` is green: `uv run ruff check src tests scripts`, `uv run ruff format --check src
   tests scripts`, `uv run mypy`, `uv run pytest`.
2. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` and open a fresh, empty
   `## [Unreleased]` above it. Confirm the new `[X.Y.Z]` entry has a `### Data & migrations`
   subsection (see above).
3. Bump `__version__` in `src/citara/__init__.py` to `X.Y.Z`. This is the single source of truth --
   `pyproject.toml` reads it dynamically (`[tool.hatch.version] path = "src/citara/__init__.py"`),
   and `importlib.metadata.version("citara")` is asserted to match it in `tests/test_version.py`.
4. Commit, e.g. `chore(release): vX.Y.Z`.
5. Tag and push: `git tag vX.Y.Z && git push origin main --tags`.
6. The tag push triggers `.github/workflows/release.yml`, which re-runs the full CI check suite,
   fails loudly if the tag doesn't match `citara.__version__`, builds the sdist/wheel with
   `uv build`, extracts the matching `CHANGELOG.md` section, and publishes a GitHub Release with
   the built artifacts attached.
7. Verify the resulting GitHub Release: correct notes, `dist/*` attached.

PyPI publishing and GHCR image publishing are intentionally out of scope for `release.yml` -- both
are deferred until the repository is public / post-`1.0` (see `todo.md`).

## Licensing of contributions

Commercial use of this project requires explicit written permission. Do not submit code that you cannot license to this project under those terms.
