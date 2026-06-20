import os

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

_KEY = Fernet.generate_key().decode()
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://ic:ic@localhost:5432/ic")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-0123456789-abcdef")
os.environ.setdefault("FERNET_KEYS", _KEY)
os.environ.setdefault("BASE_URL", "http://testserver")
# TestClient talks plain HTTP, so a Secure session cookie would never be sent
# back. Match the documented local-dev posture for the test suite.
os.environ.setdefault("SESSION_HTTPS_ONLY", "false")

from testcontainers.postgres import PostgresContainer  # noqa: E402

from app.crypto import build_fernet, set_fernet  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _fernet():
    set_fernet(build_fernet([_KEY]))


@pytest.fixture(scope="session")
def pg_engine():
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        engine = create_engine(pg.get_connection_url(), future=True)
        from app import models  # noqa: F401 ensures models are imported
        from app.db import Base

        Base.metadata.create_all(engine)
        yield engine
        engine.dispose()


def _truncate_all(engine) -> None:
    from app.db import Base

    table_names = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db_session(pg_engine) -> Session:
    # Routes under test call commit()/rollback() on this same session (via the
    # get_db override), so true SAVEPOINT-rollback isolation is impractical.
    # Instead each test gets a real session and the schema is truncated
    # afterwards, keeping tests isolated with zero teardown warnings.
    _truncate_all(pg_engine)
    session = sessionmaker(bind=pg_engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        _truncate_all(pg_engine)
