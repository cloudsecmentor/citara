#!/usr/bin/env python3
"""Extract a single release's section from CHANGELOG.md (Keep a Changelog format).

Used by `.github/workflows/release.yml` to populate GitHub release notes
without duplicating the changelog by hand. Prints the section body
(everything between the "## [X.Y.Z] - DATE" heading and the next "## ["
heading, or end of file) for the requested version to stdout.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_HEADING_RE = re.compile(r"^## \[(?P<version>[^\]]+)\]")


def extract_section(changelog_text: str, version: str) -> str | None:
    lines = changelog_text.splitlines()
    start: int | None = None
    end = len(lines)
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        if start is not None:
            end = index
            break
        if match.group("version") == version:
            start = index + 1
    if start is None:
        return None
    return "\n".join(lines[start:end]).strip("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract a version section from CHANGELOG.md")
    parser.add_argument("version", help="Version to extract, e.g. 0.2.0 (without the leading 'v')")
    parser.add_argument("--changelog", type=Path, default=REPO_ROOT / "CHANGELOG.md")
    args = parser.parse_args(argv)

    text = args.changelog.read_text(encoding="utf-8")
    section = extract_section(text, args.version)
    if section is None or not section.strip():
        print(
            f"No non-empty CHANGELOG.md section found for version {args.version!r}. "
            "Did you forget to rename [Unreleased] to this version before tagging?",
            file=sys.stderr,
        )
        return 1
    print(section)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
