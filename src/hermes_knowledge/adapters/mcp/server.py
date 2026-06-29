from __future__ import annotations

from collections.abc import Callable

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover - fallback for minimal local installs
    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, name: str | None = None, **_: object) -> None:
            self.name = name
            self._tools: dict[str, Callable[..., object]] = {}

        def tool(self, name: str | None = None) -> Callable[[Callable[..., object]], Callable[..., object]]:
            def decorator(func: Callable[..., object]) -> Callable[..., object]:
                self._tools[name or func.__name__] = func
                return func

            return decorator



def create_mcp_server() -> FastMCP:
    server = FastMCP("hermes-knowledge-vault")

    @server.tool()
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    return server


server = create_mcp_server()
