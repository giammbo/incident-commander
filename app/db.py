from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    settings = get_settings()
    return create_engine(settings.database_url, pool_pre_ping=True, future=True)


engine = None
SessionLocal: sessionmaker | None = None


def init_engine() -> None:
    global engine, SessionLocal
    if engine is None:
        engine = _make_engine()
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def get_db() -> Iterator[Session]:
    init_engine()
    if SessionLocal is None:  # pragma: no cover - init_engine always sets it
        raise RuntimeError("SessionLocal is not initialized")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
