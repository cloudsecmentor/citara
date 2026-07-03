#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Allow running from repo root without installing the package first.
repo_src = Path(__file__).resolve().parents[1] / "src"
if str(repo_src) not in sys.path:
    sys.path.insert(0, str(repo_src))

from citara.core.embeddings.providers import get_embedding_provider  # noqa: E402


def main() -> None:
    text = " ".join(sys.argv[1:]) or "Citara embedding smoke test"
    provider = get_embedding_provider()
    vector = provider.embed_texts([text])[0]
    print(
        json.dumps(
            {
                "provider": os.getenv("EMBEDDING_PROVIDER", "local"),
                "model": provider.model,
                "dimensions": len(vector),
                "preview": vector[:5],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
