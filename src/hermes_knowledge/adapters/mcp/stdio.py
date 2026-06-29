from __future__ import annotations

from hermes_knowledge.adapters.mcp.server import create_mcp_server


def main() -> None:
    create_mcp_server().run("stdio")


if __name__ == "__main__":
    main()
