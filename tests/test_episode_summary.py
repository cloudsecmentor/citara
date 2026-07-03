from __future__ import annotations


def _add_weighted_text_source(session, title: str, text: str, weight: float, label: str):
    from citara.core.ingestion.text import add_text_source
    from citara.core.sources import set_source_preference

    source = add_text_source(session, title=title, text=text)
    set_source_preference(session, source.id, retrieval_weight=weight, preference_label=label)
    return source


def test_resolve_source_prefers_current_weighted_episode(db_session):
    from citara.core.summary import resolve_source_for_summary

    legacy = _add_weighted_text_source(
        db_session,
        "BEMA 10: Walking the Blood Path (Legacy, 1 December 2016)",
        "legacy blood path text",
        0.7,
        "legacy",
    )
    current = _add_weighted_text_source(
        db_session,
        "BEMA 10: Walking the Blood Path (Current)",
        "current blood path text",
        2.0,
        "current",
    )

    resolved = resolve_source_for_summary(db_session, query="BEMA 10")

    assert resolved is not None
    assert resolved.id == current.id
    assert resolved.id != legacy.id


def test_summary_context_returns_ordered_chunks_with_timestamp_urls(db_session, fixtures_dir):
    import json

    from citara.core.ingestion.transcript import add_transcript_source
    from citara.core.summary import get_source_summary_context

    payload = json.loads((fixtures_dir / "sources" / "transcripts" / "sample_podcast_transcript.json").read_text())
    source = add_transcript_source(db_session, payload=payload)

    context = get_source_summary_context(db_session, source.id)

    assert context["source_id"] == source.id
    assert context["title"] == source.title
    assert [chunk["chunk_index"] for chunk in context["chunks"]] == [1, 2]
    assert context["chunks"][0]["timestamp_url"] == "https://example.com/podcast/ambiguity-action?t=1"
    assert context["summary_instructions"]["strategy"] == "ordered_source_transcript"


def test_api_resolves_source_and_returns_summary_context(fixtures_dir):
    import json

    from fastapi.testclient import TestClient

    from citara.adapters.api.main import create_app

    payload = json.loads((fixtures_dir / "sources" / "transcripts" / "sample_podcast_transcript.json").read_text())

    with TestClient(create_app()) as client:
        response = client.post("/sources/transcript", json=payload)
        source_id = response.json()["source_id"]

        resolved = client.get("/sources/resolve", params={"q": "Ambiguity and Action"})
        assert resolved.status_code == 200
        assert resolved.json()["source_id"] == source_id

        context = client.get(f"/sources/{source_id}/summary-context")
        assert context.status_code == 200
        body = context.json()
        assert body["source_id"] == source_id
        assert body["chunks"][0]["timestamp_url"].endswith("?t=1")
