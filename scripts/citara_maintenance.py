#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from artifact_paths import source_artifact_root, source_state_root

REPO = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Citara maintenance helpers")
    sub = parser.add_subparsers(dest="command", required=True)
    reset = sub.add_parser("reset", help="Safely remove selected Citara derived/artifact state")
    reset.add_argument("--dry-run", action="store_true", help="Print planned actions without executing them")
    reset.add_argument("--yes", action="store_true", help="Required for destructive non-dry-run actions")
    reset.add_argument("--remove-tree", action="append", default=[], help="Remove one source tree under SOURCE_ARTIFACT_ROOT")
    reset.add_argument("--rebuild-artifacts", action="store_true", help="Remove the whole source artifact root")
    reset.add_argument("--reset-sqlite", action="store_true", help="Remove sibling ../citara/citara.db")
    reset.add_argument("--reset-docker-db", action="store_true", help="Run docker compose down -v to drop Docker Postgres volume")
    return parser


def planned_actions(args: argparse.Namespace, *, repo: Path, artifact_root: Path) -> list[tuple[str, Path | str]]:
    actions: list[tuple[str, Path | str]] = []
    for tree in args.remove_tree:
        actions.append(("remove_tree", artifact_root / tree))
    if args.rebuild_artifacts:
        actions.append(("remove_artifact_root", artifact_root))
    if args.reset_sqlite:
        actions.append(("remove_sqlite", repo.parent / "citara" / "citara.db"))
    if args.reset_docker_db:
        actions.append(("docker", "docker compose down -v"))
    return actions


def print_actions(actions: list[tuple[str, Path | str]], *, dry_run: bool) -> None:
    print("DRY RUN: planned Citara reset actions" if dry_run else "Executing Citara reset actions")
    for kind, target in actions:
        print(f"- {kind}: {target}")


def execute_actions(actions: list[tuple[str, Path | str]], *, repo: Path) -> None:
    for kind, target in actions:
        if kind in {"remove_tree", "remove_artifact_root"}:
            shutil.rmtree(Path(target), ignore_errors=True)
        elif kind == "remove_sqlite":
            Path(target).unlink(missing_ok=True)
        elif kind == "docker":
            subprocess.run(str(target).split(), cwd=repo, check=True)


def main(
    argv: list[str] | None = None,
    *,
    repo: Path = REPO,
    artifact_root: Path | None = None,
    state_root: Path | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    artifact_root = (artifact_root or source_artifact_root()).expanduser()
    _state_root = (state_root or source_state_root()).expanduser()

    if args.command == "reset":
        actions = planned_actions(args, repo=repo, artifact_root=artifact_root)
        if not actions:
            print("No reset actions requested.")
            return 0
        if args.dry_run:
            print_actions(actions, dry_run=True)
            return 0
        if not args.yes:
            print("Refusing destructive reset without --yes. Re-run with --dry-run to inspect actions first.")
            print_actions(actions, dry_run=True)
            return 2
        print_actions(actions, dry_run=False)
        execute_actions(actions, repo=repo)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
