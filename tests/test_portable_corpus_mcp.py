"""Exercise documented ZIP commands and the real stdio MCP transport."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/citara_bundle.py"


def run_cli(*args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout


def clean_env(data: Path) -> dict[str, str]:
    # Never inherit the developer's real database, .env, credentials or providers.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    env.update(
        {
            "CITARA_SKIP_DOTENV": "1",
            "CITARA_DATA_ROOT": str(data),
            "DATABASE_URL": f"sqlite:///{data / 'citara.db'}",
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
    )
    return env


def decoded(result):
    assert not result.isError, result
    return json.loads(next(item.text for item in result.content if item.type == "text"))


async def exercise_mcp(data: Path, *, seed: bool, use_env_file: bool = False) -> str:
    if use_env_file:
        minimal = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT") if key in os.environ}
        params = StdioServerParameters(
            command="uv",
            args=["--directory", str(ROOT), "run", "python", "scripts/run_citara.py", "--env-file", str(data / "citara.env"), "mcp"],
            env=minimal,
            cwd=str(data.parent),
        )
    else:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "citara.adapters.mcp.stdio"],
            env=clean_env(data),
            cwd=str(ROOT),
        )
    async with stdio_client(params) as streams, ClientSession(*streams) as session:
        await session.initialize()
        assert decoded(await session.call_tool("ping", {}))["status"] == "ok"
        if seed:
            payload = json.loads((ROOT / "tests/fixtures/sources/transcripts/sample_podcast_transcript.json").read_text())
            added = decoded(await session.call_tool("add_transcript_source", {"payload": payload}))
            source_id = added["source_id"]
        else:
            source_id = None
        sources = decoded(await session.call_tool("list_sources", {}))
        assert "Ambiguity and Action" in json.dumps(sources)
        pack = decoded(
            await session.call_tool(
                "retrieve_context_pack",
                {
                    "query": "Procrastination",
                    "mode": "keyword",
                    "limit": 5,
                },
            )
        )
        chunk = pack["chunks"][0]
        assert "next action is unclear" in chunk["text"]
        citation = chunk["citation"]
        assert citation["timestamp_url"].startswith("https://example.com/podcast/ambiguity-action?t=")
        assert citation["label"]
        search = decoded(
            await session.call_tool(
                "search_knowledge",
                {
                    "query": "Procrastination",
                    "mode": "keyword",
                    "limit": 5,
                },
            )
        )
        actual_id = search["results"][0]["source_id"]
        if source_id is not None:
            assert actual_id == source_id
        return actual_id


def test_export_import_preserves_real_mcp_citations(tmp_path):
    assert HELPER.exists(), "Corpus ZIP helper is missing"
    original = tmp_path / "sender"
    original.mkdir()
    env = clean_env(original)
    import shlex

    setting_names = set(clean_env(original)) - {"PATH", "HOME", "TMPDIR", "SYSTEMROOT"}
    settings = original / "citara.env"
    settings.write_text("".join(f"{key}={shlex.quote(env[key])}\n" for key in sorted(setting_names)))
    run_cli(str(ROOT / "scripts/run_citara.py"), "--env-file", str(settings), "migrate", env=env)
    # Existing corpora must never be migrated as a setup side effect.
    refused = subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_citara.py"), "--env-file", str(settings), "migrate"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert refused.returncode != 0
    assert "existing database" in refused.stderr
    source_id = asyncio.run(exercise_mcp(original, seed=True))
    bundle = tmp_path / "shared.zip"
    run_cli(
        str(HELPER),
        "export",
        "--data-root",
        str(original),
        "--output",
        str(bundle),
        "--embedding-provider",
        "local",
        "--embedding-model",
        "deterministic-hash-v1",
        "--embedding-dimensions",
        "8",
        env=env,
    )
    inspection = run_cli(str(HELPER), "inspect", str(bundle), env=env)
    assert "deterministic-hash-v1" in inspection
    restored = tmp_path / "recipient with spaces"
    run_cli(str(HELPER), "import", str(bundle), "--data-root", str(restored), env=env)
    assert (restored / "citara.env").exists()
    assert (restored / "corpus.json").exists()
    assert asyncio.run(exercise_mcp(restored, seed=False, use_env_file=True)) == source_id
    # Exercise the exact launch style documented for MCP clients, using the
    # generated env file rather than injected test settings or a developer .env.
    minimal_env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    result = subprocess.run(
        [
            "uv",
            "--directory",
            str(ROOT),
            "run",
            "python",
            "scripts/run_citara.py",
            "--env-file",
            str(restored / "citara.env"),
            "check",
        ],
        env=minimal_env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert str(restored / "citara.db") in result.stdout
