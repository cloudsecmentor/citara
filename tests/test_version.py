from __future__ import annotations

import importlib.metadata


def test_package_version_matches_module_version():
    import citara

    # pyproject.toml declares `dynamic = ["version"]` with
    # `[tool.hatch.version] path = "src/citara/__init__.py"`, so the
    # installed package metadata and the importable module must always
    # agree -- there is exactly one place a release bumps the version.
    assert importlib.metadata.version("citara") == citara.__version__
