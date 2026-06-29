from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

TOKEN_RE = re.compile(r"[A-Za-z0-9']+")


def tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_RE.finditer(text)]


@dataclass(frozen=True)
class TextChunk:
    text: str
    start_char: int
    end_char: int


def chunk_text(text: str, *, max_chars: int = 700) -> list[TextChunk]:
    """Deterministically split text on paragraphs, combining small paragraphs."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        stripped = text.strip()
        return [TextChunk(stripped, 0, len(stripped))] if stripped else []

    chunks: list[TextChunk] = []
    current: list[str] = []
    current_start: int | None = None
    cursor = 0

    for paragraph in paragraphs:
        start = text.find(paragraph, cursor)
        end = start + len(paragraph)
        cursor = end
        candidate = "\n\n".join([*current, paragraph]) if current else paragraph
        if current and len(candidate) > max_chars:
            chunk_body = "\n\n".join(current)
            chunks.append(TextChunk(chunk_body, current_start or 0, (current_start or 0) + len(chunk_body)))
            current = [paragraph]
            current_start = start
        else:
            if not current:
                current_start = start
            current.append(paragraph)

    if current:
        chunk_body = "\n\n".join(current)
        chunks.append(TextChunk(chunk_body, current_start or 0, (current_start or 0) + len(chunk_body)))
    return chunks
