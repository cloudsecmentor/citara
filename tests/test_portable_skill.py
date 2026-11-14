"""Contract checks for the public, portable agent entry point."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_portable_skill_has_self_contained_setup_and_usage():
    skill_path = ROOT / "skills/citara/SKILL.md"
    assert skill_path.exists(), "Public installable Citara skill is missing"
    skill = skill_path.read_text()
    assert skill.startswith("---\nname: citara\n")
    for section in ("## When to use", "## Installation", "## Use Citara", "## Pitfalls", "## Verification"):
        assert section in skill
    setup = ROOT / "skills/citara/references/setup.md"
    assert setup.exists()
    for term in ("CITARA_SKIP_DOTENV", "--directory", "--env-file", "citara_bundle.py", 'mode="keyword"', "HERMES_HOME"):
        assert term in skill + setup.read_text()
    assert "/Users/seb" not in skill + setup.read_text()


def test_readme_leads_with_agent_install_and_explains_zip():
    readme = (ROOT / "README.md").read_text()
    assert "## Install with your AI agent" in readme
    assert readme.index("## Install with your AI agent") < readme.index("## Quick start")
    for term in ("skills/citara", "citara_bundle.py export", "citara_bundle.py import", "--include-artifacts", "not encrypted"):
        assert term in readme
