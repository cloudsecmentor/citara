from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

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


def test_organize_all_sets_mode_organized(tmp_path):
    repo = tmp_path / "repo"
    payload = repo / "data" / "import-artifacts" / "podcasts" / "fixture-show" / "payloads" / "fixture-episode.json"
    write_payload(payload)

    summary = organizer.organize_all(
        repo=repo,
        artifact_root=tmp_path / "citara" / "source-artifacts",
        state_root=tmp_path / "citara" / "import-state",
    )

    assert summary["mode"] == "organized"
    assert summary["schema"] == "citara.organization_manifest.v1"


def test_organize_all_guard_blocks_zero_record_overwrite_and_force_overrides(tmp_path):
    repo = tmp_path / "repo"
    payload = repo / "data" / "import-artifacts" / "podcasts" / "fixture-show" / "payloads" / "fixture-episode.json"
    write_payload(payload)
    artifact_root = tmp_path / "citara" / "source-artifacts"
    state_root = tmp_path / "citara" / "import-state"

    organizer.organize_all(repo=repo, artifact_root=artifact_root, state_root=state_root)
    manifest_path = artifact_root.parent / "organization-manifest.json"
    before = manifest_path.read_text()
    assert json.loads(before)["artifact_count"] > 0

    # Simulate the bug scenario: the data/ staging dir got emptied out.
    shutil.rmtree(repo / "data")

    with pytest.raises(organizer.ManifestWriteRefused):
        organizer.organize_all(repo=repo, artifact_root=artifact_root, state_root=state_root)

    # The refused write must not have touched the manifest on disk.
    assert manifest_path.read_text() == before

    summary = organizer.organize_all(repo=repo, artifact_root=artifact_root, state_root=state_root, force=True)
    assert summary["artifact_count"] == 0
    assert json.loads(manifest_path.read_text())["artifact_count"] == 0


def _build_rebuild_fixture_tree(artifact_root: Path) -> None:
    """A small tree shaped like the real corpus: two trees, one with source-tree.json
    and a fully-provenanced item, one without either."""
    bema_dir = artifact_root / "bema"
    bema_dir.mkdir(parents=True, exist_ok=True)
    (bema_dir / "source-tree.json").write_text(json.dumps({"schema": "citara.source_tree.v1", "source_tree_slug": "bema"}))

    item_dir = bema_dir / "items" / "bema-032"
    item_dir.mkdir(parents=True, exist_ok=True)
    (item_dir / "source.json").write_text(
        json.dumps(
            {
                "schema": "citara.source_item.v1",
                "source_tree_slug": "bema",
                "item_id": "bema-032",
                "original_path": "data/bema-session-1/BEMA_32_Current.json",
            }
        )
    )
    (item_dir / "transcript.txt").write_text("Hello.\n")
    (item_dir / "transcript.normalized.json").write_text(json.dumps({"schema": "citara.transcript.normalized.v1"}))
    (item_dir / "import-payload.json").write_text(json.dumps({"episode_title": "BEMA 32"}))
    (item_dir / "transcript.raw.json").write_text(json.dumps({"episode_title": "BEMA 32"}))
    (item_dir / "source-page.html").write_text("<html></html>")

    remote_dir = bema_dir / "remote-openai"
    remote_dir.mkdir(parents=True, exist_ok=True)
    (remote_dir / "e032-oai-raw.json").write_text("{}")
    (remote_dir / "e032-oai-raw-chunked.json").write_text("[]")
    (remote_dir / "e032-transcribe-stats.json").write_text("{}")

    # textinus: no source-tree.json, and its item's source.json has no original_path.
    item2_dir = artifact_root / "textinus" / "items" / "item-two"
    item2_dir.mkdir(parents=True, exist_ok=True)
    (item2_dir / "source.json").write_text(
        json.dumps({"schema": "citara.source_item.v1", "source_tree_slug": "textinus", "item_id": "item-two"})
    )
    (item2_dir / "transcript.source.pdf").write_bytes(b"%PDF-1.4 fake")
    (item2_dir / "transcript.source.txt").write_text("raw text")


def test_rebuild_from_artifacts_emits_one_record_per_file_with_tree_and_kind(tmp_path):
    repo = tmp_path / "repo"
    artifact_root = tmp_path / "citara" / "source-artifacts"
    state_root = tmp_path / "citara" / "import-state"
    _build_rebuild_fixture_tree(artifact_root)
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "bema_pipeline_state.json").write_text(json.dumps({"episodes": {}}))

    summary = organizer.rebuild_from_artifacts(repo=repo, artifact_root=artifact_root, state_root=state_root)

    assert summary["mode"] == "rebuilt"
    assert summary["schema"] == "citara.organization_manifest.v1"

    total_files = sum(1 for p in artifact_root.rglob("*") if p.is_file())
    assert summary["artifact_count"] == total_files

    item_records = {Path(r["target"]).name: r for r in summary["records"] if r.get("item") == "bema-032"}
    assert item_records["source.json"]["kind"] == "source_metadata_json"
    assert item_records["transcript.txt"]["kind"] == "transcript_text"
    assert item_records["transcript.normalized.json"]["kind"] == "transcript_normalized_json"
    assert item_records["import-payload.json"]["kind"] == "payload_json"
    assert item_records["transcript.raw.json"]["kind"] == "payload_json"
    assert item_records["source-page.html"]["kind"] == "source_page_html"
    for rec in item_records.values():
        assert rec["tree"] == "bema"
        assert rec["source"] == "data/bema-session-1/BEMA_32_Current.json"

    remote_records = {Path(r["target"]).name: r for r in summary["records"] if "remote-openai" in r["target"]}
    assert remote_records["e032-oai-raw.json"]["kind"] == "oai_raw_json"
    assert remote_records["e032-oai-raw-chunked.json"]["kind"] == "oai_raw_chunked_json"
    assert remote_records["e032-transcribe-stats.json"]["kind"] == "transcribe_stats_json"
    for rec in remote_records.values():
        assert rec["tree"] == "bema"
        assert "item" not in rec

    tree_json_records = [r for r in summary["records"] if r["kind"] == "source_tree_json"]
    assert len(tree_json_records) == 1
    assert tree_json_records[0]["tree"] == "bema"

    state_record = summary["state_records"][0]
    assert state_record["kind"] == "state_json"
    assert state_record["source"] is None


def test_rebuild_source_from_sibling_source_json_or_null_when_absent(tmp_path):
    repo = tmp_path / "repo"
    artifact_root = tmp_path / "citara" / "source-artifacts"
    state_root = tmp_path / "citara" / "import-state"
    _build_rebuild_fixture_tree(artifact_root)

    summary = organizer.rebuild_from_artifacts(repo=repo, artifact_root=artifact_root, state_root=state_root)

    textinus_records = [r for r in summary["records"] if r["tree"] == "textinus"]
    assert textinus_records
    for rec in textinus_records:
        assert rec["source"] is None  # sibling source.json exists but has no original_path


def test_rebuild_reports_trees_missing_source_tree_json_without_synthesizing_one(tmp_path):
    repo = tmp_path / "repo"
    artifact_root = tmp_path / "citara" / "source-artifacts"
    state_root = tmp_path / "citara" / "import-state"
    _build_rebuild_fixture_tree(artifact_root)

    summary = organizer.rebuild_from_artifacts(repo=repo, artifact_root=artifact_root, state_root=state_root)

    assert summary["trees_missing_source_tree_json"] == ["textinus"]
    # Rebuild is read-only against the artifact tree: no file should have been created.
    assert not (artifact_root / "textinus" / "source-tree.json").exists()


def test_rebuild_no_hash_skips_hashing(tmp_path):
    repo = tmp_path / "repo"
    artifact_root = tmp_path / "citara" / "source-artifacts"
    state_root = tmp_path / "citara" / "import-state"
    _build_rebuild_fixture_tree(artifact_root)

    hashed = organizer.rebuild_from_artifacts(repo=repo, artifact_root=artifact_root, state_root=state_root)
    hashed_record = next(r for r in hashed["records"] if Path(r["target"]).name == "transcript.txt")
    expected = organizer.sha256_file(Path(hashed_record["target"]))
    assert hashed_record["sha256"] == expected

    unhashed = organizer.rebuild_from_artifacts(repo=repo, artifact_root=artifact_root, state_root=state_root, hash_files=False)
    unhashed_record = next(r for r in unhashed["records"] if Path(r["target"]).name == "transcript.txt")
    assert unhashed_record["sha256"] is None
    unhashed_state = next(iter(unhashed["state_records"]), None)
    if unhashed_state is not None:
        assert unhashed_state["sha256"] is None


def test_main_rebuild_guard_blocks_overwrite_then_force_overrides(tmp_path, capsys):
    repo = tmp_path / "repo"
    payload = repo / "data" / "import-artifacts" / "podcasts" / "fixture-show" / "payloads" / "fixture-episode.json"
    write_payload(payload)
    artifact_root = tmp_path / "citara" / "source-artifacts"
    state_root = tmp_path / "citara" / "import-state"

    organizer.organize_all(repo=repo, artifact_root=artifact_root, state_root=state_root)
    manifest_path = artifact_root.parent / "organization-manifest.json"
    before = manifest_path.read_text()
    assert json.loads(before)["artifact_count"] > 0

    # Simulate the artifact tree itself getting emptied out.
    shutil.rmtree(artifact_root)
    artifact_root.mkdir(parents=True)

    code = organizer.main(["--rebuild-from-artifacts"], repo=repo, artifact_root=artifact_root, state_root=state_root)
    output = capsys.readouterr().out
    assert code == 2
    assert "Refusing to write" in output
    assert "--force" in output
    assert manifest_path.read_text() == before

    code = organizer.main(
        ["--rebuild-from-artifacts", "--force", "--no-hash"], repo=repo, artifact_root=artifact_root, state_root=state_root
    )
    assert code == 0
    after = json.loads(manifest_path.read_text())
    assert after["artifact_count"] == 0
    assert after["mode"] == "rebuilt"


def test_classify_artifact_kind_matches_any_item_prefix_not_just_bema():
    # bema names these "e<NNN>-", other shows use forms like
    # "q001-buzzsprout-17003095-s4e1-". Anchoring on "e\d+-" dropped 240 real
    # artifacts into the "other" bucket on the live corpus.
    for prefix in ("e032", "q001-buzzsprout-17003095-s4e1", "ep-7b"):
        assert organizer.classify_artifact_kind(f"{prefix}-oai-raw.json") == "oai_raw_json"
        assert organizer.classify_artifact_kind(f"{prefix}-oai-raw-chunked.json") == "oai_raw_chunked_json"
        assert organizer.classify_artifact_kind(f"{prefix}-transcribe-stats.json") == "transcribe_stats_json"


def test_classify_artifact_kind_still_falls_back_to_other():
    assert organizer.classify_artifact_kind("batch-1-532-summary.json") == "other"
    assert organizer.classify_artifact_kind("approved-queue.json") == "other"
    # A bare suffix with no prefix is not a remote-transcription artifact.
    assert organizer.classify_artifact_kind("oai-raw.json") == "other"


def test_rebuild_indexes_pipeline_state_dotfiles_but_skips_os_cruft(tmp_path):
    repo = tmp_path / "repo"
    artifact_root = tmp_path / "citara" / "source-artifacts"
    state_root = tmp_path / "citara" / "import-state"
    _build_rebuild_fixture_tree(artifact_root)
    state_root.mkdir(parents=True, exist_ok=True)

    remote_dir = artifact_root / "bema" / "remote-openai"
    # Live pipeline state -- an audit index must not silently omit these.
    (remote_dir / ".transcription-watchdog.lock").write_text("")
    (remote_dir / ".hourly-completion-reported").write_text("")
    # OS cruft -- excluded.
    (remote_dir / ".DS_Store").write_bytes(b"\x00")
    (remote_dir / "._transcript.txt").write_bytes(b"\x00")

    summary = organizer.rebuild_from_artifacts(repo=repo, artifact_root=artifact_root, state_root=state_root, hash_files=False)
    names = {Path(r["target"]).name for r in summary["records"]}

    assert ".transcription-watchdog.lock" in names
    assert ".hourly-completion-reported" in names
    assert ".DS_Store" not in names
    assert "._transcript.txt" not in names


def test_organize_all_does_not_create_phantom_bema_tree_when_data_is_empty(tmp_path):
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    artifact_root = tmp_path / "citara" / "source-artifacts"
    state_root = tmp_path / "citara" / "import-state"

    organizer.organize_all(repo=repo, artifact_root=artifact_root, state_root=state_root)

    # organize_bema_transcripts() used to call tree_meta() before checking whether
    # any source data existed, littering a bema/ tree with no content behind it.
    assert not (artifact_root / "bema").exists()
    assert list(artifact_root.iterdir()) == []
