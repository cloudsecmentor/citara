from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HKB_ROOT = REPO_ROOT.parent / "hkb"


def source_artifact_root() -> Path:
    return Path(os.getenv("SOURCE_ARTIFACT_ROOT", str(DEFAULT_HKB_ROOT / "source-artifacts"))).expanduser()


def source_state_root() -> Path:
    return Path(os.getenv("SOURCE_STATE_ROOT", str(DEFAULT_HKB_ROOT / "import-state"))).expanduser()


def object_store_path() -> Path:
    return Path(os.getenv("OBJECT_STORE_PATH", str(DEFAULT_HKB_ROOT / "object-store"))).expanduser()
