from __future__ import annotations

import os
from typing import Protocol

import httpx

from citara.core.config import settings


class TranslationProvider(Protocol):
    model: str

    def translate(self, text: str, *, target_language: str = "en") -> str:
        """Return `text` translated into `target_language`."""
        ...


class NoopTranslationProvider:
    """Default provider: returns the input unchanged.

    Used whenever no real translation backend is configured. Retrieval then
    simply runs against the original query only -- this keeps tests and
    offline/local deployments network-free by default, matching the local
    `DeterministicEmbeddingProvider`.
    """

    model = "noop"

    def translate(self, text: str, *, target_language: str = "en") -> str:
        return text


class OpenAITranslationProvider:
    """LLM-backed translation using an OpenAI-compatible chat endpoint.

    This is only used as a server-side fallback when the client supplies no
    `query_translated` (see `core/retrieval/context_pack.py`). The calling
    agent providing its own translation is the preferred, zero-cost path.
    """

    def __init__(self, *, api_key: str, model: str = "gpt-4o-mini", base_url: str = "https://api.openai.com/v1") -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    def translate(self, text: str, *, target_language: str = "en") -> str:
        with httpx.Client() as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "temperature": 0,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                f"Translate the user's message into {target_language}. "
                                "Reply with only the translation -- no commentary, no quotes."
                            ),
                        },
                        {"role": "user", "content": text},
                    ],
                },
                timeout=30,
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return str(content).strip()


class AzureFoundryTranslationProvider:
    """LLM-backed translation using an Azure AI Foundry / Azure OpenAI chat deployment."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        deployment: str,
        api_version: str = "2024-02-01",
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.deployment = deployment
        self.api_version = api_version
        self.model = deployment

    def translate(self, text: str, *, target_language: str = "en") -> str:
        url = f"{self.endpoint}/openai/deployments/{self.deployment}/chat/completions?api-version={self.api_version}"
        with httpx.Client() as client:
            response = client.post(
                url,
                headers={"api-key": self.api_key},
                json={
                    "temperature": 0,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                f"Translate the user's message into {target_language}. "
                                "Reply with only the translation -- no commentary, no quotes."
                            ),
                        },
                        {"role": "user", "content": text},
                    ],
                },
                timeout=30,
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return str(content).strip()


def get_translation_provider() -> TranslationProvider:
    provider = os.getenv("TRANSLATION_PROVIDER", settings.translation_provider)
    if provider in {"noop", "none", "off", "local", "test"}:
        return NoopTranslationProvider()
    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY") or settings.openai_api_key
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required when TRANSLATION_PROVIDER=openai")
        return OpenAITranslationProvider(
            api_key=api_key,
            model=os.getenv("TRANSLATION_MODEL", settings.translation_model),
        )
    if provider in {"azure_foundry", "foundry", "azure_openai"}:
        api_key = os.getenv("AZURE_OPENAI_API_KEY") or settings.azure_openai_api_key
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT") or settings.azure_openai_endpoint
        deployment = (
            os.getenv("AZURE_OPENAI_TRANSLATION_DEPLOYMENT") or os.getenv("AZURE_OPENAI_DEPLOYMENT") or settings.azure_openai_deployment
        )
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", settings.azure_openai_api_version)
        missing = [
            name
            for name, value in {
                "AZURE_OPENAI_API_KEY": api_key,
                "AZURE_OPENAI_ENDPOINT": endpoint,
                "AZURE_OPENAI_DEPLOYMENT": deployment,
            }.items()
            if not value
        ]
        if missing or not (endpoint and api_key and deployment):
            raise ValueError(f"{', '.join(missing)} required when TRANSLATION_PROVIDER={provider}")
        return AzureFoundryTranslationProvider(
            endpoint=endpoint,
            api_key=api_key,
            deployment=deployment,
            api_version=api_version,
        )
    raise ValueError(f"Unsupported translation provider: {provider}")


# Cache on (provider model, text, target_language) -- queries repeat, and
# translation is the most latency/cost-sensitive step in the fallback path.
# A simple process-lifetime dict is enough here: translation is only ever
# invoked for the server-side fallback (client-supplied translations skip
# this entirely), so volume is low and a persistent external cache would be
# over-engineering for a local-first, single-process server.
_CACHE: dict[tuple[str, str, str], str] = {}


def translate_text(text: str, *, target_language: str = "en", provider: TranslationProvider | None = None) -> str:
    provider = provider or get_translation_provider()
    cache_key = (provider.model, text, target_language)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached
    translated = provider.translate(text, target_language=target_language)
    _CACHE[cache_key] = translated
    return translated
