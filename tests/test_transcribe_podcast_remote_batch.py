from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "transcribe_podcast_remote_batch.py"
SPEC = importlib.util.spec_from_file_location("transcribe_podcast_remote_batch", SCRIPT)
assert SPEC and SPEC.loader
batch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(batch)


def episode(number: int = 1) -> dict[str, object]:
    return {
        "queue_number": number,
        "episode_label": "S4E1",
        "guid": f"Buzzsprout-{number}",
        "title": "Episode title",
        "audio_url": "https://example.test/audio.mp3",
        "canonical_url": "https://example.test/episode",
        "duration_seconds": 1200,
        "artifact_stem": f"q{number:03d}-buzzsprout-{number}-s4e1",
    }


def write_manifest(path: Path, episodes: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "corpus_slug": "test-podcast",
                "remote_namespace": "test-podcast-approved",
                "episodes": episodes,
            }
        )
    )


def write_triplet(out: Path, item: dict[str, object], *, complete: bool = True) -> None:
    out.mkdir(parents=True, exist_ok=True)
    stem = str(item["artifact_stem"])
    (out / f"{stem}-oai-raw.json").write_text('{"segments": [{"text": "ok"}]}')
    (out / f"{stem}-oai-raw-chunked.json").write_text('[{"text": "ok"}]')
    (out / f"{stem}-transcribe-stats.json").write_text(json.dumps({"complete": complete, "segments": 1}))


def test_load_manifest_requires_stable_unique_queue_and_safe_mapping(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    items = [episode(1), episode(2)]
    write_manifest(path, items)

    manifest = batch.load_manifest(path)

    assert [item["queue_number"] for item in manifest["episodes"]] == [1, 2]
    assert manifest["remote_namespace"] == "test-podcast-approved"

    items[1]["queue_number"] = 1
    write_manifest(path, items)
    with pytest.raises(ValueError, match="queue_number"):
        batch.load_manifest(path)

    items = [episode(1)]
    items[0]["artifact_stem"] = "../escape"
    write_manifest(path, items)
    with pytest.raises(ValueError, match="artifact_stem"):
        batch.load_manifest(path)


def test_complete_requires_valid_triplet_and_explicit_complete_marker(tmp_path: Path) -> None:
    item = episode()
    write_triplet(tmp_path, item)
    assert batch.artifact_triplet_complete(tmp_path, item)

    stats = tmp_path / f"{item['artifact_stem']}-transcribe-stats.json"
    stats.write_text('{"segments": 1}')
    assert not batch.artifact_triplet_complete(tmp_path, item)

    write_triplet(tmp_path, item)
    (tmp_path / f"{item['artifact_stem']}-oai-raw-chunked.json").write_text("not json")
    assert not batch.artifact_triplet_complete(tmp_path, item)


def test_scp_json_uses_part_validates_before_atomic_promotion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    final = tmp_path / "artifact.json"
    final.write_text('{"old": true}')

    def invalid_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        Path(argv[-1]).write_text("invalid")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(batch, "run", invalid_run)
    with pytest.raises(RuntimeError, match="JSON validation"):
        batch.atomic_scp_json(Path("key"), "worker", "/remote/file.json", final)
    assert json.loads(final.read_text()) == {"old": True}
    assert not final.with_suffix(".json.part").exists()

    def valid_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        Path(argv[-1]).write_text('{"new": true}')
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(batch, "run", valid_run)
    assert batch.atomic_scp_json(Path("key"), "worker", "/remote/file.json", final) == {"new": True}
    assert json.loads(final.read_text()) == {"new": True}


def test_download_retries_and_removes_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"audio"

    def urlopen(*_args: object, **_kwargs: object) -> Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OSError("temporary")
        return Response()

    monkeypatch.setattr(batch.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(batch.time, "sleep", lambda _seconds: None)
    dest = tmp_path / "audio.mp3"

    assert batch.download_with_retries("https://example.test/a.mp3", dest, attempts=3) >= 0
    assert calls == 3
    assert dest.read_bytes() == b"audio"
    assert not dest.with_suffix(".mp3.part").exists()


def test_state_updates_are_atomic_and_preserve_other_entries(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    batch.update_item_state(state_path, episode(1), {"status": "running"})
    batch.update_item_state(state_path, episode(2), {"status": "complete"})

    state = json.loads(state_path.read_text())
    assert state["episodes"]["Buzzsprout-1"]["status"] == "running"
    assert state["episodes"]["Buzzsprout-2"]["status"] == "complete"
    assert not state_path.with_suffix(".json.tmp").exists()


def test_dry_run_and_limit_select_without_network_or_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = tmp_path / "queue.json"
    out = tmp_path / "out"
    state = tmp_path / "state.json"
    items = [episode(1), episode(2), episode(3)]
    write_manifest(manifest_path, items)
    write_triplet(out, items[0])

    monkeypatch.setattr(batch, "run", lambda *_args, **_kwargs: pytest.fail("dry-run used ssh/scp"))
    result = batch.orchestrate(
        batch.Config(
            manifest=manifest_path,
            local_out=out,
            state_path=state,
            repo=tmp_path,
            worker="worker",
            ssh_key=tmp_path / "key",
            remote_root=Path("/remote"),
            model="medium",
            device="cpu",
            compute_type="int8",
            beam_size=5,
            best_of=1,
            timeout_seconds=10,
            download_attempts=3,
            inter_episode_delay_fraction=0.10,
            limit=1,
            dry_run=True,
        )
    )

    assert [row["status"] for row in result["episodes"]] == ["skipped_existing_artifact", "dry_run"]
    assert result["episodes"][1]["queue_number"] == 2
    assert not state.exists()


def test_orchestrate_success_uses_namespace_maps_artifacts_and_cleans_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = tmp_path / "queue.json"
    out = tmp_path / "out"
    state_path = tmp_path / "state.json"
    item = episode(1)
    item["guid"] = "tag:soundcloud,2010:tracks/123"
    write_manifest(manifest_path, [item])
    commands: list[list[str]] = []

    def fake_download(_url: str, destination: Path, *, attempts: int) -> float:
        assert attempts == 3
        destination.write_bytes(b"audio")
        return 1.25

    def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(argv)
        remote_source = argv[-2]
        if argv[0] == "scp" and remote_source.startswith("worker:"):
            destination = Path(argv[-1])
            if remote_source.endswith("-oai-raw.json"):
                destination.write_text('{"segments": [{"text": "raw"}]}')
            elif remote_source.endswith("-oai-raw-chunked.json"):
                destination.write_text('[{"text": "chunk"}]')
            elif remote_source.endswith("-transcribe-stats.json"):
                destination.write_text('{"complete": true, "segments": 1}')
        return SimpleNamespace(stdout="worker ok")

    monkeypatch.setattr(batch, "download_with_retries", fake_download)
    monkeypatch.setattr(batch, "run", fake_run)
    config = batch.Config(
        manifest=manifest_path,
        local_out=out,
        state_path=state_path,
        repo=tmp_path,
        worker="worker",
        ssh_key=tmp_path / "key",
        remote_root=Path("/remote"),
        model="medium",
        device="cpu",
        compute_type="int8",
        beam_size=5,
        best_of=1,
        timeout_seconds=10,
        download_attempts=3,
        inter_episode_delay_fraction=0,
        limit=None,
        dry_run=False,
    )

    result = batch.orchestrate(config)

    assert result["successful_queue_numbers"] == [1]
    assert batch.artifact_triplet_complete(out, item)
    stats = json.loads((out / f"{item['artifact_stem']}-transcribe-stats.json").read_text())
    assert stats["guid"] == item["guid"]
    assert stats["local_download_seconds"] == 1.25
    assert json.loads(state_path.read_text())["episodes"][item["guid"]]["status"] == "complete"
    rendered = [" ".join(command) for command in commands]
    assert any("/remote/test-podcast-approved/out" in command and "--model medium" in command for command in rendered)
    uploads = [command for command in rendered if command.startswith("scp ") and ".mp3 worker:" in command]
    assert uploads and "q001-buzzsprout-1-s4e1.mp3" in uploads[0]
    assert "tag:soundcloud" not in uploads[0]
    assert any("rm -f" in command and "q001-buzzsprout-1-s4e1.mp3" in command for command in rendered)
