from __future__ import annotations


def test_openai_embedding_provider_posts_expected_request(monkeypatch):
    from citara.core.embeddings.providers import OpenAIEmbeddingProvider

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, *, headers, json, timeout):
            captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return FakeResponse()

    monkeypatch.setattr("citara.core.embeddings.providers.httpx.Client", FakeClient)

    provider = OpenAIEmbeddingProvider(api_key="test-key", model="text-embedding-3-small")
    vectors = provider.embed_texts(["one", "two"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert captured["url"] == "https://api.openai.com/v1/embeddings"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"] == {"model": "text-embedding-3-small", "input": ["one", "two"]}


def test_azure_foundry_embedding_provider_posts_expected_request(monkeypatch):
    from citara.core.embeddings.providers import AzureFoundryEmbeddingProvider

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"embedding": [0.5, 0.6]}]}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, *, headers, json, timeout):
            captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return FakeResponse()

    monkeypatch.setattr("citara.core.embeddings.providers.httpx.Client", FakeClient)

    provider = AzureFoundryEmbeddingProvider(
        endpoint="https://example.cognitiveservices.azure.com/",
        api_key="azure-key",
        deployment="embed-deploy",
        api_version="2024-02-01",
    )
    vectors = provider.embed_texts(["hello"])

    assert vectors == [[0.5, 0.6]]
    assert captured["url"] == "https://example.cognitiveservices.azure.com/openai/deployments/embed-deploy/embeddings?api-version=2024-02-01"
    assert captured["headers"]["api-key"] == "azure-key"
    assert captured["json"] == {"input": ["hello"]}
