from __future__ import annotations

import uvicorn

from citara.adapters.api.main import create_app
from citara.adapters.mcp.server import create_mcp_server


app = create_app()
mcp_server = create_mcp_server()


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
