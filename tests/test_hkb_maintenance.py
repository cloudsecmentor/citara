from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "hkb_maintenance.py"
spec = importlib.util.spec_from_file_location("hkb_maintenance", SCRIPT_PATH)
hkb_maintenance = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(hkb_maintenance)


def test_reset_dry_run_reports_actions_without_deleting(tmp_path, capsys):
    repo = tmp_path / "repo"
    artifact_root = tmp_path / "hkb" / "source-artifacts"
    state_root = tmp_path / "hkb" / "import-state"
    python_tree = artifact_root / "python-bytes"
    python_tree.mkdir(parents=True)
    (python_tree / "source-tree.json").write_text("{}")
    sqlite = repo.parent / "hkb" / "hermes_knowledge_vault.db"
    repo.mkdir()
    sqlite.parent.mkdir(parents=True, exist_ok=True)
    sqlite.write_text("db")

    code = hkb_maintenance.main(
        [
            "reset",
            "--dry-run",
            "--remove-tree",
            "python-bytes",
            "--reset-sqlite",
            "--reset-docker-db",
        ],
        repo=repo,
        artifact_root=artifact_root,
        state_root=state_root,
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN" in output
    assert str(python_tree) in output
    assert "docker compose down -v" in output
    assert python_tree.exists()
    assert sqlite.exists()


def test_reset_refuses_destructive_actions_without_yes(tmp_path, capsys):
    repo = tmp_path / "repo"
    artifact_root = tmp_path / "hkb" / "source-artifacts"
    state_root = tmp_path / "hkb" / "import-state"
    (artifact_root / "python-bytes").mkdir(parents=True)

    code = hkb_maintenance.main(
        ["reset", "--remove-tree", "python-bytes"],
        repo=repo,
        artifact_root=artifact_root,
        state_root=state_root,
    )

    output = capsys.readouterr().out
    assert code == 2
    assert "Refusing destructive reset without --yes" in output
    assert (artifact_root / "python-bytes").exists()


def test_reset_yes_removes_requested_tree_and_sqlite(tmp_path):
    repo = tmp_path / "repo"
    artifact_root = tmp_path / "hkb" / "source-artifacts"
    state_root = tmp_path / "hkb" / "import-state"
    tree = artifact_root / "python-bytes"
    tree.mkdir(parents=True)
    repo.mkdir()
    sqlite = repo.parent / "hkb" / "hermes_knowledge_vault.db"
    sqlite.parent.mkdir(parents=True, exist_ok=True)
    sqlite.write_text("db")

    code = hkb_maintenance.main(
        ["reset", "--yes", "--remove-tree", "python-bytes", "--reset-sqlite"],
        repo=repo,
        artifact_root=artifact_root,
        state_root=state_root,
    )

    assert code == 0
    assert not tree.exists()
    assert not sqlite.exists()
