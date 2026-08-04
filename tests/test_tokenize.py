from __future__ import annotations

from citara.core.chunking.simple import tokenize


def test_tokenize_latin_matches_previous_ascii_behavior():
    assert tokenize("Cats chase mice.") == ["cats", "chase", "mice"]
    assert tokenize("cat123 and DOG456") == ["cat123", "and", "dog456"]


def test_tokenize_empty_and_symbol_only_input_returns_empty_list():
    assert tokenize("") == []
    assert tokenize("...???!!!") == []


def test_tokenize_accented_latin_keeps_words_intact():
    # Regression test: the old `[A-Za-z0-9']+` regex split "éxodo" into
    # "xodo" because it didn't recognize "é" as a word character at all.
    tokens = tokenize("¿Qué dice sobre el éxodo?")

    assert tokens == ["qué", "dice", "sobre", "el", "éxodo"]
    assert "xodo" not in tokens


def test_tokenize_cyrillic_produces_whole_words_not_empty_list():
    tokens = tokenize("Что говорит об Исходе")

    assert tokens == ["что", "говорит", "об", "исходе"]


def test_tokenize_mixed_cyrillic_and_latin_keeps_both():
    tokens = tokenize("Что говорит BEMA об Исходе?")

    assert tokens == ["что", "говорит", "bema", "об", "исходе"]


def test_tokenize_hebrew_produces_whole_words_not_empty_list():
    tokens = tokenize("מה אומר על יציאת מצרים")

    assert tokens == ["מה", "אומר", "על", "יציאת", "מצרים"]


def test_tokenize_cjk_falls_back_to_character_bigrams():
    # Han script has no inter-word whitespace, so a single word-character
    # run would otherwise become one giant "token" for an entire sentence.
    tokens = tokenize("什么是耶稣")

    assert tokens == ["什么", "么是", "是耶", "耶稣"]


def test_tokenize_single_cjk_character_is_its_own_token():
    assert tokenize("字") == ["字"]


def test_tokenize_is_case_insensitive_for_latin():
    assert tokenize("HELLO World") == tokenize("hello world")
