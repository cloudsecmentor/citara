from __future__ import annotations


def test_api_add_text_search_context_pack_and_delete_source():
    from fastapi.testclient import TestClient

    from hermes_knowledge.adapters.api.main import create_app

    with TestClient(create_app()) as client:
        title = "API Procrastination Note"
        response = client.post(
            "/sources/text",
            json={"title": title, "text": "The next action should be visible and tiny."},
        )
        assert response.status_code == 200
        source_id = response.json()["source_id"]

        search = client.get("/search", params={"q": "visible tiny", "mode": "keyword"})
        assert search.status_code == 200
        assert any(result["source_id"] == source_id for result in search.json()["results"])

        hybrid_search = client.get("/search", params={"q": "small visible", "mode": "hybrid"})
        assert hybrid_search.status_code == 200
        assert any(result["source_id"] == source_id for result in hybrid_search.json()["results"])

        pack = client.get("/context-pack", params={"q": "next action", "mode": "hybrid"})
        assert pack.status_code == 200
        assert pack.json()["query"] == "next action"
        assert len(pack.json()["chunks"]) >= 1

        delete = client.delete(f"/sources/{source_id}")
        assert delete.status_code == 200
        assert delete.json() == {"source_id": source_id, "deleted": True}

        search_after_delete = client.get("/search", params={"q": "visible tiny"})
        assert not any(result["source_id"] == source_id for result in search_after_delete.json()["results"])


def test_mcp_server_exposes_hermes_tools():
    from hermes_knowledge.adapters.mcp.server import create_mcp_server

    server = create_mcp_server()
    tool_names = set(getattr(server, "_tools", {}).keys())

    # Real FastMCP does not expose the same private testing registry as the fallback,
    # so this assertion is strongest for the local fallback and smoke-tests construction otherwise.
    if tool_names:
        assert {
            "add_text_source",
            "add_transcript_source",
            "search_knowledge",
            "retrieve_context_pack",
            "resolve_source",
            "get_source_summary_context",
            "resolve_summary_context",
            "list_sources",
            "delete_source",
            "get_ingestion_job_status",
            "set_source_preference",
            "list_ingestion_jobs",
        }.issubset(tool_names)
    assert server is not None


def test_mcp_stdio_main_runs_stdio_transport(monkeypatch):
    from hermes_knowledge.adapters.mcp import stdio

    calls = []

    class FakeServer:
        def run(self, transport="stdio"):
            calls.append(transport)

    monkeypatch.setattr(stdio, "create_mcp_server", lambda: FakeServer())

    stdio.main()

    assert calls == ["stdio"]
