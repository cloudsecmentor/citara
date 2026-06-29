from __future__ import annotations

from pathlib import Path


def test_fixture_transcription_provider_returns_normalized_transcript(fixtures_dir):
    from hermes_knowledge.core.transcription.providers import FixtureTranscriptionProvider

    provider = FixtureTranscriptionProvider(fixtures_dir / "sources" / "transcripts" / "sample_podcast_transcript.json")
    transcript = provider.transcribe(Path("/tmp/nonexistent-audio.mp3"))

    assert transcript["show_title"] == "Test Podcast"
    assert transcript["episode_title"] == "Ambiguity and Action"
    assert transcript["segments"][0]["start_ms"] == 1000
    assert transcript["segments"][1]["speaker"] == "Guest"
