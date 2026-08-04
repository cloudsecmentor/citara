from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "extract_changelog.py"
spec = importlib.util.spec_from_file_location("extract_changelog", SCRIPT_PATH)
extract_changelog = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(extract_changelog)

SAMPLE_CHANGELOG = """\
# Changelog

All notable changes to Citara will be documented in this file.

## [Unreleased]

### Added

- Something not yet released.

## [0.2.0] - 2026-08-04

### Added

- Query translation support.

### Data & migrations

- No migration required.

## [0.1.0] - 2026-07-01

### Added

- Initial release.
"""


def test_extract_section_returns_body_for_requested_version():
    section = extract_changelog.extract_section(SAMPLE_CHANGELOG, "0.2.0")

    assert section is not None
    assert "Query translation support." in section
    assert "### Data & migrations" in section
    # Must not bleed into the neighboring sections.
    assert "Initial release." not in section
    assert "Something not yet released." not in section


def test_extract_section_returns_none_for_missing_version():
    assert extract_changelog.extract_section(SAMPLE_CHANGELOG, "9.9.9") is None


def test_extract_section_handles_last_section_in_file():
    section = extract_changelog.extract_section(SAMPLE_CHANGELOG, "0.1.0")

    assert section is not None
    assert "Initial release." in section


def test_main_writes_error_for_missing_version(tmp_path, capsys):
    changelog_path = tmp_path / "CHANGELOG.md"
    changelog_path.write_text(SAMPLE_CHANGELOG, encoding="utf-8")

    exit_code = extract_changelog.main(["9.9.9", "--changelog", str(changelog_path)])

    assert exit_code == 1
    assert "No non-empty CHANGELOG.md section found" in capsys.readouterr().err


def test_main_prints_section_for_existing_version(tmp_path, capsys):
    changelog_path = tmp_path / "CHANGELOG.md"
    changelog_path.write_text(SAMPLE_CHANGELOG, encoding="utf-8")

    exit_code = extract_changelog.main(["0.2.0", "--changelog", str(changelog_path)])

    assert exit_code == 0
    assert "Query translation support." in capsys.readouterr().out
