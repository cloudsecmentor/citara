#!/usr/bin/env python3
"""Launch Citara with explicit non-secret settings, independent of shell cwd/.env."""

from __future__ import annotations

import argparse
import json
import os
import shlex
from pathlib import Path

KEYS = {
    "CITARA_SKIP_DOTENV",
    "CITARA_DATA_ROOT",
    "DATABASE_URL",
    "SOURCE_ARTIFACT_ROOT",
    "SOURCE_STATE_ROOT",
    "OBJECT_STORE_PATH",
    "EMBEDDING_PROVIDER",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS",
    "TRANSLATION_PROVIDER",
    "DEFAULT_TENANT_ID",
    "DEFAULT_USER_ID",
}


def read_settings(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in KEYS or key in values:
            raise ValueError(f"Unknown, duplicate or malformed setting: {key}")
        try:
            tokens = shlex.split(value, comments=True, posix=True)
        except ValueError as error:
            raise ValueError(f"Invalid value for {key}; quote paths containing spaces") from error
        if not tokens or len(tokens) != 1 or not tokens[0] or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in tokens[0]):
            raise ValueError(f"Invalid value for {key}; quote paths containing spaces")
        values[key] = tokens[0]
    if values.keys() != KEYS:
        raise ValueError("Missing settings: " + ", ".join(sorted(KEYS - values.keys())))
    root = Path(values["CITARA_DATA_ROOT"])
    # SQLAlchemy treats '?' as a query delimiter, not a literal SQLite filename.
    if "?" in str(root):
        raise ValueError("Selected data root must not contain '?'")
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("CITARA_DATA_ROOT must be an existing absolute local directory")
    expected = {
        "DATABASE_URL": f"sqlite:///{root.as_posix()}/citara.db",
        "SOURCE_ARTIFACT_ROOT": str(root / "source-artifacts"),
        "SOURCE_STATE_ROOT": str(root / "import-state"),
        "OBJECT_STORE_PATH": str(root / "object-store"),
        "CITARA_SKIP_DOTENV": "1",
        "TRANSLATION_PROVIDER": "noop",
    }
    if any(values[key] != value for key, value in expected.items()):
        raise ValueError("Paths must point to the selected local data root; dotenv must be disabled and translation noop")
    if values["EMBEDDING_PROVIDER"] not in {"local", "openai", "azure_foundry"} or int(values["EMBEDDING_DIMENSIONS"]) < 1:
        raise ValueError("Invalid embedding provider/dimensions")
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("action", choices=("mcp", "migrate", "check"))
    args = parser.parse_args()
    try:
        values = read_settings(args.env_file)
        database = Path(values["CITARA_DATA_ROOT"]) / "citara.db"
        if args.action == "migrate" and database.exists():
            raise ValueError("Refusing migrations on an existing database; migrate explicitly with a verified backup")
        if args.action == "mcp" and not database.is_file():
            raise ValueError("Database missing; initialize fresh data with migrate or import a corpus bundle first")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    # Override ambient settings BEFORE any Citara import. Never source shell code.
    os.environ.update(values)
    if args.action == "check":
        print(json.dumps(values, indent=2))
    elif args.action == "migrate":
        from alembic import command
        from alembic.config import Config

        repo = Path(__file__).resolve().parents[1]
        config = Config(str(repo / "alembic.ini"))
        config.set_main_option("script_location", str(repo / "alembic"))
        command.upgrade(config, "head")
    else:
        from citara.adapters.mcp.stdio import main as serve

        serve()


if __name__ == "__main__":
    main()
