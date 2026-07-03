from __future__ import annotations

from pathlib import Path

from citara.core.config import settings

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CITARA_ROOT = REPO_ROOT.parent / "citara"


def _resolve_repo_relative(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (REPO_ROOT / path).resolve()


def source_artifact_root() -> Path:
    return _resolve_repo_relative(settings.source_artifact_root)


def source_state_root() -> Path:
    return _resolve_repo_relative(settings.source_state_root)


def local_database_path() -> Path:
    database_url = settings.database_url
    prefix = "sqlite:///"
    if database_url.startswith(prefix):
        return _resolve_repo_relative(database_url[len(prefix) :])
    return DEFAULT_CITARA_ROOT / "citara.db"
