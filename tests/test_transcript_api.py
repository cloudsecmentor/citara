from __future__ import annotations

import json


def test_api_ingests_transcript_fixture_and_searches_timestamp_citation(fixtures_dir):
    from fastapi.testclient import TestClient

    from hermes_knowledge.adapters.api.main import create_app

    payload = json.loads((fixtures_dir / "sources" / "transcripts" / "sample_podcast_transcript.json").read_text())

    with TestClient(create_app()) as client:
        response = client.post("/sources/transcript", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["source_id"].startswith("src_")
        assert body["job_id"].startswith("job_")

        search = client.get("/search", params={"q": "ambiguous action", "mode": "hybrid"})
        assert search.status_code == 200
        results = search.json()["results"]
        result = next(item for item in results if item["source_id"] == body["source_id"])
        assert result["source_type"] == "podcast_episode"
        assert result["citation_label"].startswith("Test Podcast, Ambiguity and Action")
        assert result["start_ms"] is not None
