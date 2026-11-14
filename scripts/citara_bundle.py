#!/usr/bin/env python3
"""Export, fully inspect, or import portable corpus ZIPs without runtime config."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Works from a source checkout without importing environment-bound runtime settings.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from citara.core.bundles import BundleError, BundleLimits, export_bundle, import_bundle, inspect_bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    exporting = commands.add_parser("export", help="Export a read-only SQLite online backup")
    exporting.add_argument("--data-root", type=Path, required=True)
    exporting.add_argument("--output", type=Path, required=True)
    exporting.add_argument("--embedding-provider", required=True)
    exporting.add_argument("--embedding-model", required=True)
    exporting.add_argument("--embedding-dimensions", type=int, required=True)
    exporting.add_argument("--tenant-id", default="local")
    exporting.add_argument("--user-id", default="owner")
    exporting.add_argument("--include-artifacts", action="store_true")
    inspecting = commands.add_parser("inspect", help="Fully verify ZIP payloads, hashes, and SQLite")
    importing = commands.add_parser("import", help="Verify and atomically publish a fresh data root")
    importing.add_argument("--data-root", type=Path, required=True)
    defaults = BundleLimits()
    for command in (inspecting, importing):
        command.add_argument("archive", type=Path)
        for name in ("max_file_bytes", "max_total_bytes", "max_members", "max_manifest_bytes"):
            command.add_argument("--" + name.replace("_", "-"), type=int, default=getattr(defaults, name))
        command.add_argument("--max-compression-ratio", type=float, default=defaults.max_compression_ratio)
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    try:
        if command == "export":
            result = export_bundle(**args)
        else:
            limits = BundleLimits(**{key: args.pop(key) for key in vars(defaults)})
            archive = args.pop("archive")
            result = inspect_bundle(archive, limits=limits) if command == "inspect" else import_bundle(archive, limits=limits, **args)
    except (BundleError, OSError) as exc:
        parser.exit(2, f"citara_bundle: error: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
