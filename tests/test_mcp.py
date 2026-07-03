def test_create_mcp_server_registers_ping_tool():
    from citara.adapters.mcp.server import create_mcp_server

    server = create_mcp_server()

    assert server is not None
    assert server.name == "citara"
