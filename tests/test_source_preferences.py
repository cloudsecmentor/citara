from __future__ import annotations


def test_source_retrieval_weight_prefers_current_when_text_matches(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.retrieval.keyword import search_knowledge
    from citara.core.sources import set_source_preference

    legacy = add_text_source(db_session, title="BEMA Legacy", text="shared crossroads idea")
    current = add_text_source(db_session, title="BEMA Current", text="shared crossroads idea")
    set_source_preference(db_session, legacy.id, retrieval_weight=0.5, preference_label="legacy")
    set_source_preference(db_session, current.id, retrieval_weight=2.0, preference_label="current")

    results = search_knowledge(db_session, query="shared crossroads", limit=2)

    assert [result.source_id for result in results] == [current.id, legacy.id]
    assert results[0].score > results[1].score


def test_api_can_set_source_preference_weight():
    from fastapi.testclient import TestClient

    from citara.adapters.api.main import create_app

    with TestClient(create_app()) as client:
        response = client.post("/sources/text", json={"title": "Preference Note", "text": "weighted preference text"})
        source_id = response.json()["source_id"]

        preference = client.patch(
            f"/sources/{source_id}/preference",
            json={"retrieval_weight": 1.75, "preference_label": "preferred"},
        )

        assert preference.status_code == 200
        body = preference.json()
        assert body["source_id"] == source_id
        assert body["metadata"]["retrieval_weight"] == 1.75
        assert body["metadata"]["preference_label"] == "preferred"
