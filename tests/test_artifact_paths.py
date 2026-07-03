from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "artifact_paths.py"
spec = importlib.util.spec_from_file_location("artifact_paths", SCRIPT_PATH)
artifact_paths = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(artifact_paths)


def test_source_roots_default_to_sibling_hkb(monkeypatch):
    monkeypatch.delenv("SOURCE_ARTIFACT_ROOT", raising=False)
    monkeypatch.delenv("SOURCE_STATE_ROOT", raising=False)

    repo_root = Path(__file__).resolve().parents[1]

    assert artifact_paths.source_artifact_root() == repo_root.parent / "hkb" / "source-artifacts"
    assert artifact_paths.source_state_root() == repo_root.parent / "hkb" / "import-state"


def test_source_roots_can_be_overridden_with_env(monkeypatch, tmp_path):
    artifact_root = tmp_path / "artifacts"
    state_root = tmp_path / "state"
    monkeypatch.setenv("SOURCE_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("SOURCE_STATE_ROOT", str(state_root))

    assert artifact_paths.source_artifact_root() == artifact_root
    assert artifact_paths.source_state_root() == state_root
