from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Corpus data lives outside the code repo so real transcripts, audio, and database
# files are never accidentally committed. The default is a sibling directory next to
# the checkout; override it with CITARA_DATA_ROOT (or the finer-grained
# SOURCE_ARTIFACT_ROOT / SOURCE_STATE_ROOT / OBJECT_STORE_PATH below).
DEFAULT_DATA_ROOT = REPO_ROOT.parent / "citara-data"


def data_root() -> Path:
    return Path(os.getenv("CITARA_DATA_ROOT", str(DEFAULT_DATA_ROOT))).expanduser()


def repo_root() -> Path:
    return Path(os.getenv("CITARA_REPO_ROOT", str(REPO_ROOT))).expanduser()


def source_artifact_root() -> Path:
    return Path(os.getenv("SOURCE_ARTIFACT_ROOT", str(data_root() / "source-artifacts"))).expanduser()


def source_state_root() -> Path:
    return Path(os.getenv("SOURCE_STATE_ROOT", str(data_root() / "import-state"))).expanduser()


def object_store_path() -> Path:
    return Path(os.getenv("OBJECT_STORE_PATH", str(data_root() / "object-store"))).expanduser()


def database_url() -> str:
    return os.getenv("DATABASE_URL", f"sqlite:///{data_root() / 'citara.db'}")


def apply_default_env(root: Path | None = None) -> Path:
    """Seed the data-location env vars scripts rely on, without overriding a real .env.

    Returns the resolved data root so callers can use it for paths of their own.
    """
    resolved = (root or data_root()).expanduser()
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{resolved / 'citara.db'}")
    os.environ.setdefault("SOURCE_ARTIFACT_ROOT", str(resolved / "source-artifacts"))
    os.environ.setdefault("SOURCE_STATE_ROOT", str(resolved / "import-state"))
    os.environ.setdefault("OBJECT_STORE_PATH", str(resolved / "object-store"))
    return resolved
