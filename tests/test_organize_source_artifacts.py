from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "organize_source_artifacts.py"
spec = importlib.util.spec_from_file_location("organize_source_artifacts", SCRIPT_PATH)
organizer = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(organizer)


def write_payload(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "show_title": "Fixture Show",
                "episode_title": "Fixture Episode",
                "episode_url": "https://example.com/fixture",
                "segments": [{"start_ms": 0, "end_ms": 1000, "speaker": "Host", "text": "Hello."}],
            }
        )
    )


def test_organize_all_writes_generic_payload_artifacts(tmp_path):
    repo = tmp_path / "repo"
    payload = repo / "data" / "import-artifacts" / "podcasts" / "fixture-show" / "payloads" / "fixture-episode.json"
    write_payload(payload)

    artifact_root = tmp_path / "citara" / "source-artifacts"
    state_root = tmp_path / "citara" / "import-state"

    summary = organizer.organize_all(repo=repo, artifact_root=artifact_root, state_root=state_root)

    item_dir = artifact_root / "fixture-show" / "items" / "fixture-episode"
    assert summary["artifact_counts_by_tree"] == {"fixture-show": 1}
    assert (item_dir / "source.json").exists()
    assert (item_dir / "import-payload.json").exists()
    assert (item_dir / "transcript.raw.json").exists()
    assert (item_dir / "transcript.normalized.json").exists()
    assert (item_dir / "transcript.txt").read_text() == "Hello.\n"

    source = json.loads((item_dir / "source.json").read_text())
    assert source["source_tree_slug"] == "fixture-show"
    assert source["canonical_url"] == "https://example.com/fixture"


def test_organize_all_rewrites_legacy_state_artifact_paths(tmp_path):
    repo = tmp_path / "repo"
    state = repo / "data" / "import-state" / "bibleproject_pipeline_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "episodes": {
                    "abc": {
                        "title": "Example Episode",
                        "pdf_path": "data/import-artifacts/bibleproject/pdf/example-episode.pdf",
                    }
                }
            }
        )
    )

    artifact_root = tmp_path / "citara" / "source-artifacts"
    state_root = tmp_path / "citara" / "import-state"
    organizer.organize_all(repo=repo, artifact_root=artifact_root, state_root=state_root)

    copied = json.loads((state_root / "bibleproject_pipeline_state.json").read_text())
    assert copied["episodes"]["abc"]["pdf_path"] == ("source-artifacts://bibleproject/items/example-episode/transcript.source.pdf")
    assert "data/import-artifacts" not in json.dumps(copied)


def test_bema_transcript_source_json_links_episode_page_and_version(tmp_path):
    repo = tmp_path / "repo"
    payload = repo / "data" / "bema-session-1" / "BEMA_32_Current.json"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text(
        json.dumps(
            {
                "show_title": "The BEMA Podcast",
                "episode_title": "BEMA 32: Session 1 Capstone (Current)",
                "episode_url": "https://www.bemadiscipleship.com/32",
                "segments": [{"start_ms": 0, "end_ms": 1000, "text": "Hello."}],
            }
        )
    )

    artifact_root = tmp_path / "citara" / "source-artifacts"
    state_root = tmp_path / "citara" / "import-state"
    organizer.organize_all(repo=repo, artifact_root=artifact_root, state_root=state_root)

    source_json = artifact_root / "bema" / "items" / "bema-32-session-1-capstone-current" / "source.json"
    source = json.loads(source_json.read_text())
    assert source["episode_number"] == 32
    assert source["source_page_item_id"] == "bema-032"
    assert source["version_label"] == "current"
