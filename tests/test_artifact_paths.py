from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "artifact_paths.py"
spec = importlib.util.spec_from_file_location("artifact_paths", SCRIPT_PATH)
artifact_paths = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(artifact_paths)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_source_roots_default_to_sibling_data_dir(monkeypatch):
    monkeypatch.delenv("CITARA_DATA_ROOT", raising=False)
    monkeypatch.delenv("SOURCE_ARTIFACT_ROOT", raising=False)
    monkeypatch.delenv("SOURCE_STATE_ROOT", raising=False)
    monkeypatch.delenv("OBJECT_STORE_PATH", raising=False)

    assert artifact_paths.source_artifact_root() == REPO_ROOT.parent / "citara-data" / "source-artifacts"
    assert artifact_paths.source_state_root() == REPO_ROOT.parent / "citara-data" / "import-state"
    assert artifact_paths.object_store_path() == REPO_ROOT.parent / "citara-data" / "object-store"


def test_default_data_root_is_outside_the_repo(monkeypatch):
    """Corpus data must never default to a path inside the checkout, or it gets committed."""
    monkeypatch.delenv("CITARA_DATA_ROOT", raising=False)

    root = artifact_paths.data_root()

    assert root != REPO_ROOT
    assert REPO_ROOT not in root.parents


def test_data_root_can_be_overridden_with_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CITARA_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("SOURCE_ARTIFACT_ROOT", raising=False)
    monkeypatch.delenv("SOURCE_STATE_ROOT", raising=False)

    assert artifact_paths.data_root() == tmp_path
    assert artifact_paths.source_artifact_root() == tmp_path / "source-artifacts"
    assert artifact_paths.source_state_root() == tmp_path / "import-state"


def test_source_roots_can_be_overridden_with_env(monkeypatch, tmp_path):
    artifact_root = tmp_path / "artifacts"
    state_root = tmp_path / "state"
    monkeypatch.setenv("SOURCE_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("SOURCE_STATE_ROOT", str(state_root))

    assert artifact_paths.source_artifact_root() == artifact_root
    assert artifact_paths.source_state_root() == state_root


def test_specific_roots_win_over_data_root(monkeypatch, tmp_path):
    monkeypatch.setenv("CITARA_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("SOURCE_ARTIFACT_ROOT", str(tmp_path / "explicit"))

    assert artifact_paths.source_artifact_root() == tmp_path / "explicit"
    assert artifact_paths.source_state_root() == tmp_path / "data" / "import-state"


def test_apply_default_env_does_not_override_existing(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///already-set.db")
    monkeypatch.delenv("SOURCE_ARTIFACT_ROOT", raising=False)

    artifact_paths.apply_default_env(tmp_path)

    import os

    assert os.environ["DATABASE_URL"] == "sqlite:///already-set.db"
    assert os.environ["SOURCE_ARTIFACT_ROOT"] == str(tmp_path / "source-artifacts")
