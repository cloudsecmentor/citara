from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol

import httpx

from citara.core.chunking.simple import tokenize
from citara.core.config import settings

# Scripts where English-style plural "s" stemming would corrupt the token
# rather than normalize it -- it's an English-specific heuristic. Mirrors the
# non-Latin ranges used for language detection elsewhere in `core/language`.
_NON_LATIN_RE = re.compile("[\u0400-\u04ff\u0590-\u05ff\u0600-\u06ff\u0900-\u097f\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

SYNONYMS = {
    "feline": "cat",
    "felines": "cat",
    "kitty": "cat",
    "kitten": "cat",
    "canine": "dog",
    "canines": "dog",
    "puppy": "dog",
}


class EmbeddingProvider(Protocol):
    model: str

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class DeterministicEmbeddingProvider:
    model = "deterministic-hash-v1"

    def __init__(self, dimensions: int = settings.embedding_dimensions) -> None:
        self.dimensions = dimensions

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_text(text) for text in texts]

    def _embed_text(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in _normalized_tokens(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [value / norm for value in vector]


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        dimensions: int | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.dimensions = dimensions

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        payload: dict = {"model": self.model, "input": texts}
        if self.dimensions:
            # Matryoshka truncation. text-embedding-3-* are trained so a
            # prefix of the vector remains useful, so asking for 512 instead
            # of the native 1536 costs little quality and thirds the resident
            # matrix that vector search keeps in memory.
            payload["dimensions"] = self.dimensions
        with httpx.Client() as client:
            response = client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=60,
            )
        response.raise_for_status()
        return [[float(value) for value in item["embedding"]] for item in response.json()["data"]]


class AzureFoundryEmbeddingProvider:
    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        deployment: str,
        api_version: str = "2024-02-01",
        dimensions: int | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.deployment = deployment
        self.api_version = api_version
        self.model = deployment
        self.dimensions = dimensions

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        url = f"{self.endpoint}/openai/deployments/{self.deployment}/embeddings?api-version={self.api_version}"
        payload: dict = {"input": texts}
        if self.dimensions:
            # Support depends on the deployed model and api-version; an older
            # deployment simply returns its native width.
            payload["dimensions"] = self.dimensions
        with httpx.Client() as client:
            response = client.post(
                url,
                headers={"api-key": self.api_key},
                json=payload,
                timeout=60,
            )
        response.raise_for_status()
        return [[float(value) for value in item["embedding"]] for item in response.json()["data"]]


def _normalized_tokens(text: str) -> list[str]:
    tokens = tokenize(text)
    normalized: list[str] = []
    for token in tokens:
        if _NON_LATIN_RE.search(token):
            # Non-Latin scripts: keep as-is (aside from synonym mapping,
            # which is itself English-specific and simply won't match).
            normalized.append(SYNONYMS.get(token, token))
        else:
            # Only stem multi-character tokens. `removesuffix("s")` reduces the
            # bare token "s" -- which Unicode tokenization now yields from split
            # contractions like "god's" -> ["god", "s"] -- to an empty string,
            # and every empty token hashes to the same fixed dimension, piling
            # unrelated contractions onto one axis of an 8-dimensional vector.
            stemmed = token.removesuffix("s") if len(token) > 1 else token
            normalized.append(SYNONYMS.get(token, stemmed))
    return normalized


# Defaults used when a real embedding backend is selected but the model /
# dimension settings still hold their local-development placeholders. Without
# this, `EMBEDDING_PROVIDER=openai` alone would request a model literally named
# "deterministic-hash-v1" from the API, or silently ask for 8-dimensional
# vectors -- both of which look like configuration bugs at the worst moment.
DEFAULT_REMOTE_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_REMOTE_EMBEDDING_DIMENSIONS = 512


def _remote_model() -> str:
    model = os.getenv("EMBEDDING_MODEL", settings.embedding_model)
    return DEFAULT_REMOTE_EMBEDDING_MODEL if model == DeterministicEmbeddingProvider.model else model


def _remote_dimensions() -> int | None:
    """Explicit EMBEDDING_DIMENSIONS wins; otherwise fall back to the default.

    `settings.embedding_dimensions` cannot be used directly here: it defaults
    to 8 for the deterministic provider, and 8-dimensional OpenAI vectors
    would be a silent quality catastrophe rather than an error.
    """
    raw = os.getenv("EMBEDDING_DIMENSIONS")
    if not raw:
        return DEFAULT_REMOTE_EMBEDDING_DIMENSIONS
    value = int(raw)
    return DEFAULT_REMOTE_EMBEDDING_DIMENSIONS if value <= 8 else value


def get_embedding_provider() -> EmbeddingProvider:
    provider = os.getenv("EMBEDDING_PROVIDER", settings.embedding_provider)
    # NOTE: "local" selects the DETERMINISTIC HASH provider -- a fast,
    # offline, non-semantic stand-in for tests and local development, not a
    # locally hosted model. `deterministic` is the clearer spelling; `local`
    # is kept because existing configs and docker-compose use it.
    if provider in {"local", "deterministic", "test"}:
        return DeterministicEmbeddingProvider(settings.embedding_dimensions)
    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY") or settings.openai_api_key
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required when EMBEDDING_PROVIDER=openai")
        return OpenAIEmbeddingProvider(
            api_key=api_key,
            model=_remote_model(),
            dimensions=_remote_dimensions(),
        )
    if provider in {"azure_foundry", "foundry", "azure_openai"}:
        api_key = os.getenv("AZURE_OPENAI_API_KEY") or settings.azure_openai_api_key
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT") or settings.azure_openai_endpoint
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT") or settings.azure_openai_deployment
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
            raise ValueError(f"{', '.join(missing)} required when EMBEDDING_PROVIDER={provider}")
        return AzureFoundryEmbeddingProvider(
            endpoint=endpoint,
            api_key=api_key,
            deployment=deployment,
            api_version=api_version,
            dimensions=_remote_dimensions(),
        )
    raise ValueError(f"Unsupported embedding provider: {provider}")
