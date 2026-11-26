"""Shared prerequisites for tests that create their own disposable databases."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from culinary_copilot.config import Settings


@pytest.fixture(scope="module")
def postgres_available() -> None:
    """Skip on connection failure before a test attempts disposable DB setup.

    Probe only the maintenance database. Errors in the actual migrations,
    disposable database creation, or test assertions must still fail the test.
    """
    url = make_url(Settings().database_url.get_secret_value()).set(database="postgres")
    engine = create_engine(url, connect_args={"connect_timeout": 2})
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("PostgreSQL unavailable for disposable-database tests")
    finally:
        engine.dispose()
