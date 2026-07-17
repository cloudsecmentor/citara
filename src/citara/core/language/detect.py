from __future__ import annotations

import re

# Very lightweight, dependency-free language hinting based on Unicode ranges.
# This is not meant to be perfect language ID; it's only used to select a
# retrieval language filter.

_HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
_HAN_RE = re.compile(r"[\u4E00-\u9FFF]")
_HANGUL_RE = re.compile(r"[\uAC00-\uD7AF]")
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def detect_language_code(text: str) -> tuple[str | None, float]:
    """Return (language_code, confidence).

    Possible language codes: 'en', 'he', 'ru', 'ar', 'zh', 'ko', 'hi', or None.
    """

    if not text:
        return None, 0.0

    sample = text.strip()
    if not sample:
        return None, 0.0

    # Count script-specific characters.
    latin_count = len(_LATIN_RE.findall(sample))

    candidates: list[tuple[str, int]] = [
        ("he", len(_HEBREW_RE.findall(sample))),
        ("ru", len(_CYRILLIC_RE.findall(sample))),
        ("ar", len(_ARABIC_RE.findall(sample))),
        ("zh", len(_HAN_RE.findall(sample))),
        ("ko", len(_HANGUL_RE.findall(sample))),
        ("hi", len(_DEVANAGARI_RE.findall(sample))),
    ]

    # If Latin letters are present, assume English (good enough for your current
    # tokenization pipeline which is Latin-focused).
    if latin_count > 0:
        # Confidence based on density of Latin letters.
        confidence = min(1.0, latin_count / max(20, len(sample)))
        return "en", confidence

    # Otherwise pick the strongest script signal.
    best_code, best_count = max(candidates, key=lambda item: item[1])
    if best_count == 0:
        return None, 0.0

    confidence = min(1.0, best_count / max(20, len(sample)))
    return best_code, confidence
