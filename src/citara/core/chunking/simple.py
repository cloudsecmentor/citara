from __future__ import annotations

import re
from dataclasses import dataclass

# Unicode-aware: matches any run of "word" characters (letters/digits in any
# script), not just ASCII Latin. `\W` is negated rather than matching `\w`
# directly so tokens stay letters/digits only (no underscore), matching the
# intent of the previous Latin-only regex.
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Han, Hiragana/Katakana, and Hangul text has no inter-word whitespace, so a
# TOKEN_RE match against a run of CJK text is often a whole sentence rather
# than a "word". Fall back to character bigrams for those scripts so keyword
# matching still has something granular to compare against.
_CJK_RE = re.compile(r"[\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]")


def _bigrams(run: str) -> list[str]:
    if len(run) <= 1:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(text):
        run = match.group(0).lower()
        if _CJK_RE.search(run):
            tokens.extend(_bigrams(run))
        else:
            tokens.append(run)
    return tokens


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
