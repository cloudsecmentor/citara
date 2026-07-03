from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol


class TranscriptionProvider(Protocol):
    def transcribe(self, audio_path: Path) -> dict:
        """Return a normalized transcript payload for an audio file."""


class FixtureTranscriptionProvider:
    """Deterministic provider used by tests before real transcription is wired in."""

    def __init__(self, transcript_path: Path) -> None:
        self.transcript_path = transcript_path

    def transcribe(self, audio_path: Path) -> dict:
        # The audio path is intentionally unused for the fixture provider.
        return json.loads(self.transcript_path.read_text())
