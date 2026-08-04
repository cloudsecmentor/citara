from __future__ import annotations

import pytest


def test_noop_translation_provider_returns_text_unchanged():
    from citara.core.language.translate import NoopTranslationProvider

    provider = NoopTranslationProvider()

    assert provider.translate("Что говорит об Исходе") == "Что говорит об Исходе"
    assert provider.model == "noop"


def test_get_translation_provider_defaults_to_noop(monkeypatch):
    from citara.core.language.translate import NoopTranslationProvider, get_translation_provider

    monkeypatch.delenv("TRANSLATION_PROVIDER", raising=False)

    provider = get_translation_provider()

    assert isinstance(provider, NoopTranslationProvider)


def test_get_translation_provider_openai_requires_api_key(monkeypatch):
    from citara.core.language.translate import get_translation_provider

    monkeypatch.setenv("TRANSLATION_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        get_translation_provider()


def test_get_translation_provider_rejects_unsupported_provider(monkeypatch):
    from citara.core.language.translate import get_translation_provider

    monkeypatch.setenv("TRANSLATION_PROVIDER", "not-a-real-provider")

    with pytest.raises(ValueError, match="Unsupported translation provider"):
        get_translation_provider()


def test_openai_translation_provider_posts_expected_request(monkeypatch):
    from citara.core.language.translate import OpenAITranslationProvider

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": " Что говорит об Исходе "}}]}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, *, headers, json, timeout):
            captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return FakeResponse()

    monkeypatch.setattr("citara.core.language.translate.httpx.Client", FakeClient)

    provider = OpenAITranslationProvider(api_key="test-key", model="gpt-4o-mini")
    translated = provider.translate("What does it say about the Exodus?", target_language="ru")

    # Whitespace from the LLM response is stripped; the translation itself
    # (a stand-in here, not a real translation) is returned verbatim.
    assert translated == "Что говорит об Исходе"
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["model"] == "gpt-4o-mini"
    assert captured["json"]["messages"][-1] == {"role": "user", "content": "What does it say about the Exodus?"}
    assert "ru" in captured["json"]["messages"][0]["content"]


def test_azure_foundry_translation_provider_posts_expected_request(monkeypatch):
    from citara.core.language.translate import AzureFoundryTranslationProvider

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "Bonjour"}}]}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, *, headers, json, timeout):
            captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return FakeResponse()

    monkeypatch.setattr("citara.core.language.translate.httpx.Client", FakeClient)

    provider = AzureFoundryTranslationProvider(
        endpoint="https://example.cognitiveservices.azure.com/",
        api_key="azure-key",
        deployment="chat-deploy",
        api_version="2024-02-01",
    )
    translated = provider.translate("Hello", target_language="fr")

    assert translated == "Bonjour"
    assert captured["url"] == (
        "https://example.cognitiveservices.azure.com/openai/deployments/chat-deploy/chat/completions?api-version=2024-02-01"
    )
    assert captured["headers"]["api-key"] == "azure-key"
    assert provider.model == "chat-deploy"


def test_translate_text_caches_on_provider_model_text_and_target_language():
    from citara.core.language.translate import translate_text

    calls: list[tuple[str, str]] = []

    class CountingProvider:
        model = "counting-test-provider"

        def translate(self, text: str, *, target_language: str = "en") -> str:
            calls.append((text, target_language))
            return f"{text}::{target_language}"

    provider = CountingProvider()

    first = translate_text("hello", target_language="ru", provider=provider)
    second = translate_text("hello", target_language="ru", provider=provider)
    third = translate_text("hello", target_language="es", provider=provider)

    assert first == second == "hello::ru"
    assert third == "hello::es"
    # Same (model, text, target_language) key is served from cache; a
    # different target_language is a cache miss and calls through again.
    assert calls == [("hello", "ru"), ("hello", "es")]
