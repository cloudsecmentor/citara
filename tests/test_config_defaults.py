"""Guards on where Citara puts corpus data when nothing is configured.

The bug these cover: a default of `../citara` resolves back into a checkout named
`citara`, so a bare-metal run wrote the database and artifacts into the repository
itself. That is how corpus state came to be committed.
"""

from __future__ import annotations

import re
from pathlib import Path

from citara.core.config import Settings, _data_root

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"


def _resolve_from_repo(value: str) -> Path:
    """Resolve a default the way a process launched from the repo root would."""
    return (REPO_ROOT / value.removeprefix("sqlite:///")).resolve()


def test_data_root_defaults_to_sibling_outside_the_repo(monkeypatch):
    monkeypatch.delenv("CITARA_DATA_ROOT", raising=False)

    assert _data_root() == "../citara-data"

    resolved = _resolve_from_repo(_data_root())
    assert resolved != REPO_ROOT
    assert REPO_ROOT not in resolved.parents


def test_data_root_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CITARA_DATA_ROOT", str(tmp_path))

    assert _data_root() == str(tmp_path)


def test_data_root_strips_trailing_slash(monkeypatch):
    """Defaults interpolate this value, so a trailing slash would yield `root//db`."""
    monkeypatch.setenv("CITARA_DATA_ROOT", "../elsewhere/")

    assert _data_root() == "../elsewhere"


def test_settings_path_defaults_never_resolve_inside_the_repo():
    settings = Settings()
    paths = [
        settings.database_url,
        settings.object_store_path,
        settings.source_artifact_root,
        settings.source_state_root,
    ]

    for value in paths:
        resolved = _resolve_from_repo(value)
        assert resolved != REPO_ROOT, f"{value} resolves to the repo itself"
        assert REPO_ROOT not in resolved.parents, f"{value} resolves inside the repo"


def test_no_stale_sibling_citara_default_in_source():
    """`../citara/` is always wrong as a default -- it collides with the checkout name."""
    offenders = []
    for path in SRC_ROOT.rglob("*.py"):
        if re.search(r"\.\./citara/", path.read_text()):
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert not offenders, f"stale ../citara/ default in: {offenders}"
