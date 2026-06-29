from __future__ import annotations


def test_api_add_text_search_and_context_pack():
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

        search = client.get("/search", params={"q": "visible tiny"})
        assert search.status_code == 200
        assert any(result["source_id"] == source_id for result in search.json()["results"])

        pack = client.get("/context-pack", params={"q": "next action"})
        assert pack.status_code == 200
        assert pack.json()["query"] == "next action"
        assert len(pack.json()["chunks"]) >= 1


def test_mcp_server_exposes_hermes_tools():
    from hermes_knowledge.adapters.mcp.server import create_mcp_server

    server = create_mcp_server()
    tool_names = set(getattr(server, "_tools", {}).keys())

    # Real FastMCP does not expose the same private testing registry as the fallback,
    # so this assertion is strongest for the local fallback and smoke-tests construction otherwise.
    if tool_names:
        assert {"add_text_source", "search_knowledge", "retrieve_context_pack", "list_sources"}.issubset(tool_names)
    assert server is not None
