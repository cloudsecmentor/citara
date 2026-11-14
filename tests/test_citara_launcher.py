"""Safety checks for the standalone setup/MCP launcher."""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts/run_citara.py"


@pytest.fixture()
def local_settings(tmp_path, request):
    data = tmp_path / getattr(request, "param", "local corpus")
    data.mkdir()
    values = {
        "CITARA_SKIP_DOTENV": "1",
        "CITARA_DATA_ROOT": str(data),
        "DATABASE_URL": f"sqlite:///{data.as_posix()}/citara.db",
        "SOURCE_ARTIFACT_ROOT": str(data / "source-artifacts"),
        "SOURCE_STATE_ROOT": str(data / "import-state"),
        "OBJECT_STORE_PATH": str(data / "object-store"),
        "EMBEDDING_PROVIDER": "local",
        "EMBEDDING_MODEL": "deterministic-hash-v1",
        "EMBEDDING_DIMENSIONS": "8",
        "TRANSLATION_PROVIDER": "noop",
        "DEFAULT_TENANT_ID": "local",
        "DEFAULT_USER_ID": "owner",
    }
    path = data / "citara.env"
    path.write_text("".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()))
    return path, values


def launch(path, action="check", env=None):
    return subprocess.run(
        [sys.executable, str(LAUNCHER), "--env-file", str(path), action],
        cwd=path.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_settings_override_ambient_database_without_opening_it(local_settings, tmp_path):
    path, values = local_settings
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp_path / 'wrong.db'}", EMBEDDING_PROVIDER="openai")
    result = launch(path, env=env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == values
    assert not (tmp_path / "wrong.db").exists()


@pytest.mark.parametrize("extra", ["OPENAI_API_KEY=secret\n", "CITARA_SKIP_DOTENV=0\n", "BAD_LINE\n"])
def test_unknown_duplicate_or_malformed_settings_are_refused(local_settings, extra):
    path, _ = local_settings
    with path.open("a") as stream:
        stream.write(extra)
    result = launch(path)
    assert result.returncode != 0
    assert "setting" in result.stderr


def test_shell_expansions_are_never_executed(local_settings, tmp_path):
    path, _ = local_settings
    marker = tmp_path / "must-not-exist"
    text = path.read_text().replace("DEFAULT_USER_ID=owner", f"DEFAULT_USER_ID='$(touch {marker})'")
    path.write_text(text)
    result = launch(path)
    assert result.returncode == 0
    assert json.loads(result.stdout)["DEFAULT_USER_ID"].startswith("$(touch")
    assert not marker.exists()


def test_mcp_refuses_missing_database_without_creating_one(local_settings):
    path, _ = local_settings
    result = launch(path, "mcp")
    assert result.returncode != 0
    assert "Database missing" in result.stderr
    assert not (path.parent / "citara.db").exists()


@pytest.mark.parametrize(
    "local_settings", ["corpus\x01control", "corpus\tcontrol", "corpus\ncontrol", "corpus\x7fcontrol", "corpus\x9fcontrol"], indirect=True
)
@pytest.mark.parametrize("action", ["check", "migrate", "mcp"])
def test_control_character_roots_are_refused(local_settings, action):
    path, _ = local_settings
    result = launch(path, action)
    assert result.returncode == 2
    assert "Invalid value" in result.stderr or "setting" in result.stderr
    assert not (path.parent / "citara.db").exists()


@pytest.mark.parametrize("local_settings", ["corpus å日本 'quoted'"], indirect=True)
def test_regular_unicode_space_quote_roots_are_preserved(local_settings):
    path, values = local_settings
    result = launch(path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == values


@pytest.mark.parametrize("action", ["check", "migrate", "mcp"])
@pytest.mark.parametrize("existing_sibling", [False, True])
def test_question_mark_root_cannot_create_or_migrate_sibling(local_settings, tmp_path, existing_sibling, action):
    path, values = local_settings
    data = tmp_path / "corpus?query"
    data.mkdir()
    values = {key: value.replace(str(path.parent), str(data)) for key, value in values.items()}
    path = data / "citara.env"
    path.write_text("".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()))
    sibling = tmp_path / "corpus"
    before = None
    if existing_sibling:
        with sqlite3.connect(sibling) as db:
            db.execute("CREATE TABLE sentinel(value TEXT)")
            db.execute("INSERT INTO sentinel VALUES ('must remain byte-identical')")
        before = sibling.read_bytes()
    selected = data / "citara.db"
    if action == "mcp":
        selected.write_bytes(b"selected database sentinel")
    result = launch(path, action)
    if existing_sibling:
        assert sibling.read_bytes() == before
    else:
        assert not sibling.exists()
    assert not (tmp_path / "corpus-wal").exists()
    assert not (tmp_path / "corpus-shm").exists()
    if action == "mcp":
        assert selected.read_bytes() == b"selected database sentinel"
    else:
        assert not selected.exists()
    assert result.returncode == 2
    assert "data root" in result.stderr and "?" in result.stderr


def test_settings_cannot_redirect_database_outside_data_root(local_settings, tmp_path):
    path, _ = local_settings
    text = path.read_text()
    path.write_text(
        "\n".join(
            f"DATABASE_URL=sqlite:///{tmp_path / 'other.db'}" if line.startswith("DATABASE_URL=") else line for line in text.splitlines()
        )
        + "\n"
    )
    result = launch(path)
    assert result.returncode != 0
    assert "selected local data root" in result.stderr
    assert not (tmp_path / "other.db").exists()
