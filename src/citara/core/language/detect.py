from __future__ import annotations

import re

from citara.core.chunking.simple import tokenize

# Very lightweight, dependency-free language hinting based on Unicode ranges
# and short stopword lists. This is not meant to be perfect language ID; it's
# only used to select a retrieval language filter and to report a query
# language.

_HEBREW_RE = re.compile("[\u0590-\u05ff]")
_CYRILLIC_RE = re.compile("[\u0400-\u04ff]")
_ARABIC_RE = re.compile("[\u0600-\u06ff]")
_HAN_RE = re.compile("[\u4e00-\u9fff]")
_HANGUL_RE = re.compile("[\uac00-\ud7af]")
_DEVANAGARI_RE = re.compile("[\u0900-\u097f]")
# Latin, including accented Latin (Latin-1 Supplement + Latin Extended-A/B),
# so accented queries (e.g. "éxodo") aren't undercounted as non-Latin and
# don't get treated as weaker evidence than they are.
_LATIN_RE = re.compile("[A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f]")

# Short, dependency-free stopword lists used only to tell Latin-script
# languages apart from English (and from each other). Not exhaustive -- this
# only needs to be "good enough to pick a filter and report a language", per
# the design brief; it is not meant to be a real language identifier.
_LATIN_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        "the a an and or of to in on for is are what does do say says said "
        "about that this with from by as it its was were be been has have "
        "had not but if so we you they he she who which when where why how".split()
    ),
    "es": frozenset(
        "que de la el los las un una unos unas y o en a es son del al por "
        "para con no su sus sobre dice dicen qué cómo cuándo dónde este "
        "esta estos estas pero como más muy también".split()
    ),
    "fr": frozenset(
        "le la les de des du un une et ou est sont que qui pour dans sur "
        "avec ne pas vous nous il elle ils elles ce cette ces mais comme "
        "plus très dit dis quoi où quand".split()
    ),
    "de": frozenset(
        "der die das und oder ist sind nicht mit für auf von zu ein eine "
        "einen was wer wie wo wann warum sagt sagen über aber auch sehr "
        "mehr".split()
    ),
    "pt": frozenset(
        "o a os as de do da dos das que e ou um uma uns umas é são não "
        "para com sobre diz dizem qual quando onde como mas muito também".split()
    ),
    "it": frozenset("il lo la i gli le di che e o un una non per con su dice dicono cosa quando dove come ma anche molto più".split()),
}


def detect_language_code(text: str) -> tuple[str | None, float]:
    """Return (language_code, confidence).

    Possible language codes: 'en', 'es', 'fr', 'de', 'pt', 'it', 'he', 'ru',
    'ar', 'zh', 'ko', 'hi', or None.
    """

    if not text:
        return None, 0.0

    sample = text.strip()
    if not sample:
        return None, 0.0

    # Count script-specific characters, including Latin, and let the
    # dominant script win -- rather than letting any Latin character (even a
    # single embedded proper noun like "BEMA" in an otherwise Cyrillic
    # query) automatically force an "en" verdict.
    candidates: list[tuple[str, int]] = [
        ("he", len(_HEBREW_RE.findall(sample))),
        ("ru", len(_CYRILLIC_RE.findall(sample))),
        ("ar", len(_ARABIC_RE.findall(sample))),
        ("zh", len(_HAN_RE.findall(sample))),
        ("ko", len(_HANGUL_RE.findall(sample))),
        ("hi", len(_DEVANAGARI_RE.findall(sample))),
        ("latin", len(_LATIN_RE.findall(sample))),
    ]

    best_code, best_count = max(candidates, key=lambda item: item[1])
    if best_count == 0:
        return None, 0.0

    if best_code != "latin":
        confidence = min(1.0, best_count / max(20, len(sample)))
        return best_code, confidence

    return _detect_latin_language(sample, latin_count=best_count)


def _detect_latin_language(sample: str, *, latin_count: int) -> tuple[str, float]:
    """Distinguish English from other Latin-script languages via stopwords."""

    words = tokenize(sample)
    if not words:
        return "en", 0.0

    scores = {lang: sum(1 for word in words if word in stopwords) for lang, stopwords in _LATIN_STOPWORDS.items()}
    best_lang = max(scores, key=lambda lang: scores[lang])
    best_score = scores[best_lang]

    char_density_confidence = min(1.0, latin_count / max(20, len(sample)))

    if best_score == 0:
        # No recognizable stopword from any known Latin-script language --
        # e.g. a bare proper noun. Fall back to the previous default of
        # assuming English, at reduced confidence; this is a fallback, not a
        # real detection.
        return "en", char_density_confidence

    # Stopwords pick *which* language; confidence takes the stronger of two
    # signals rather than the stopword fraction alone. A well-formed sentence
    # is usually mostly content words (not stopwords), so stopword-fraction
    # alone regularly undershoots the confidence a plain, unambiguous
    # sentence deserves -- and callers elsewhere (e.g. ingestion's
    # source-language auto-tagging) compare this confidence against the same
    # 0.4 threshold this module used pre-Stage-1 for any Latin text.
    confidence = max(best_score / len(words), char_density_confidence)
    return best_lang, min(1.0, confidence)
