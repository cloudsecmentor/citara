from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from hermes_knowledge.core.config import settings

TOKEN_RE = re.compile(r"[A-Za-z0-9']+")
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
    dimensions: int

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        ...


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


def _normalized_tokens(text: str) -> list[str]:
    tokens = [match.group(0).lower() for match in TOKEN_RE.finditer(text)]
    return [SYNONYMS.get(token, token.removesuffix("s")) for token in tokens]


def get_embedding_provider() -> EmbeddingProvider:
    if settings.embedding_provider in {"local", "deterministic", "test"}:
        return DeterministicEmbeddingProvider(settings.embedding_dimensions)
    raise ValueError(f"Unsupported embedding provider: {settings.embedding_provider}")
