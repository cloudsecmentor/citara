from __future__ import annotations

from citara.core.language.detect import detect_language_code


def test_detect_language_code_handles_empty_input():
    assert detect_language_code("") == (None, 0.0)
    assert detect_language_code("   ") == (None, 0.0)


def test_detect_language_code_pure_cyrillic_is_russian():
    code, confidence = detect_language_code("Что говорит об Исходе")

    assert code == "ru"
    assert confidence >= 0.4


def test_detect_language_code_pure_hebrew_is_hebrew():
    code, confidence = detect_language_code("מה אומר על יציאת מצרים")

    assert code == "he"
    assert confidence >= 0.4


def test_detect_language_code_mixed_cyrillic_and_latin_is_not_english():
    # Regression test: previously *any* Latin character (even a single
    # embedded proper noun like "BEMA") forced an "en" verdict, even though
    # this query is overwhelmingly Cyrillic.
    code, confidence = detect_language_code("Что говорит BEMA об Исходе?")

    assert code == "ru"
    assert confidence >= 0.4


def test_detect_language_code_plain_english_is_english():
    code, confidence = detect_language_code("The cats chase the mice in the house")

    assert code == "en"
    assert confidence >= 0.4


def test_detect_language_code_spanish_is_not_mislabeled_english():
    # Regression test: previously any Latin letters meant "en" unconditionally.
    code, confidence = detect_language_code("¿Qué dice sobre el éxodo?")

    assert code == "es"
    assert confidence >= 0.4


def test_detect_language_code_french_is_detected():
    code, confidence = detect_language_code("Que dit le texte sur l'Exode et pourquoi c'est important")

    assert code == "fr"
    assert confidence > 0


def test_detect_language_code_german_is_detected():
    code, confidence = detect_language_code("Was sagt der Text über den Auszug und warum ist das wichtig")

    assert code == "de"
    assert confidence > 0


def test_detect_language_code_accented_latin_is_not_mangled_into_none():
    # A single accented word alone has no stopword signal, so it falls back
    # to the English default -- but it must not crash or return None just
    # because the only letter present is accented.
    code, confidence = detect_language_code("Éxodo")

    assert code == "en"
    assert isinstance(confidence, float)


def test_detect_language_code_bare_proper_noun_falls_back_to_english():
    code, confidence = detect_language_code("BEMA")

    assert code == "en"
    assert 0.0 <= confidence <= 1.0
