from __future__ import annotations

import pytest


def test_remote_model_replaces_the_local_placeholder(monkeypatch):
    """`EMBEDDING_PROVIDER=openai` alone must not request the fake model name.

    `EMBEDDING_MODEL` defaults to "deterministic-hash-v1" for local runs.
    Passing that straight through to the API would fail with a confusing
    model-not-found at the moment someone first enables real embeddings.
    """
    from citara.core.embeddings.providers import DEFAULT_REMOTE_EMBEDDING_MODEL, _remote_model

    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    assert _remote_model() == DEFAULT_REMOTE_EMBEDDING_MODEL

    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-large")
    assert _remote_model() == "text-embedding-3-large"


def test_remote_dimensions_never_inherits_the_local_default(monkeypatch):
    """The local default is 8 dimensions -- catastrophic for a real model."""
    from citara.core.embeddings.providers import DEFAULT_REMOTE_EMBEDDING_DIMENSIONS, _remote_dimensions

    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    assert _remote_dimensions() == DEFAULT_REMOTE_EMBEDDING_DIMENSIONS

    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "8")
    assert _remote_dimensions() == DEFAULT_REMOTE_EMBEDDING_DIMENSIONS

    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1536")
    assert _remote_dimensions() == 1536


def test_openai_provider_requests_matryoshka_dimensions(monkeypatch):
    import httpx

    from citara.core.embeddings.providers import OpenAIEmbeddingProvider

    captured: dict = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"embedding": [0.1] * 512}]}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None, timeout=None):
            captured.update(json)
            return _Response()

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _Client())

    provider = OpenAIEmbeddingProvider(api_key="k", model="text-embedding-3-small", dimensions=512)
    vectors = provider.embed_texts(["hello"])

    assert captured["dimensions"] == 512
    assert captured["model"] == "text-embedding-3-small"
    assert len(vectors[0]) == 512


def test_openai_provider_omits_dimensions_when_unset(monkeypatch):
    """An unset dimension must not send `dimensions: null` or 0."""
    import httpx

    from citara.core.embeddings.providers import OpenAIEmbeddingProvider

    captured: dict = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"embedding": [0.1] * 1536}]}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None, timeout=None):
            captured.update(json)
            return _Response()

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _Client())

    OpenAIEmbeddingProvider(api_key="k", model="m", dimensions=None).embed_texts(["hello"])
    assert "dimensions" not in captured


def test_dotenv_fills_gaps_without_overriding_real_env(monkeypatch, tmp_path):
    """A real environment variable must always beat the file."""
    import os

    from citara.core.config import _load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment line\n"
        "\n"
        "CITARA_TEST_FROM_FILE=from_file\n"
        'CITARA_TEST_QUOTED="quoted value"\n'
        "export CITARA_TEST_EXPORTED=exported\n"
        "CITARA_TEST_ALREADY_SET=from_file\n"
    )

    monkeypatch.delenv("CITARA_SKIP_DOTENV", raising=False)
    monkeypatch.setenv("CITARA_ENV_FILE", str(env_file))
    monkeypatch.setenv("CITARA_TEST_ALREADY_SET", "from_real_env")
    for name in ("CITARA_TEST_FROM_FILE", "CITARA_TEST_QUOTED", "CITARA_TEST_EXPORTED"):
        monkeypatch.delenv(name, raising=False)

    _load_dotenv()

    assert os.environ["CITARA_TEST_FROM_FILE"] == "from_file"
    assert os.environ["CITARA_TEST_QUOTED"] == "quoted value"
    assert os.environ["CITARA_TEST_EXPORTED"] == "exported"
    assert os.environ["CITARA_TEST_ALREADY_SET"] == "from_real_env"


def test_dotenv_can_be_skipped(monkeypatch, tmp_path):
    import os

    from citara.core.config import _load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("CITARA_TEST_SKIPPED=should_not_load\n")

    monkeypatch.setenv("CITARA_SKIP_DOTENV", "1")
    monkeypatch.setenv("CITARA_ENV_FILE", str(env_file))
    monkeypatch.delenv("CITARA_TEST_SKIPPED", raising=False)

    _load_dotenv()

    assert "CITARA_TEST_SKIPPED" not in os.environ


@pytest.mark.parametrize("provider_name", ["local", "deterministic", "test"])
def test_local_provider_names_all_select_the_deterministic_stub(monkeypatch, provider_name):
    """`local` means the hash stub, not a locally hosted model."""
    from citara.core.embeddings.providers import DeterministicEmbeddingProvider, get_embedding_provider

    monkeypatch.setenv("EMBEDDING_PROVIDER", provider_name)
    assert isinstance(get_embedding_provider(), DeterministicEmbeddingProvider)
