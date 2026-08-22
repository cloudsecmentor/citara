from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Isolate the test suite onto a throwaway SQLite database. This must run before any
# `citara` import because the engine is created at import time from settings.database_url.
# Without this, the API tests would connect to the user's real corpus DB (../citara-data).
_TEST_DB = Path(tempfile.gettempdir()) / "citara_test.db"
if _TEST_DB.exists():
    _TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

# Keep the suite hermetic. `config.py` loads a `.env` at import time, and a
# developer machine with real credentials on disk would otherwise change what
# the tests exercise -- e.g. a live OPENAI_API_KEY silently satisfying a test
# that asserts the missing-key error path.
os.environ["CITARA_SKIP_DOTENV"] = "1"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from citara.core.models import Base


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with Session() as session:
        yield session


@pytest.fixture()
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
