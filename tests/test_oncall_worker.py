import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import get_db
from app.main import create_app
from app.models import EscalationPath, Role
from app.services import oncall_worker as worker
from app.services.users import bootstrap_admin, create_user
from app.settings_store import oncall_settings

BANNER = "on-call worker is not running"


@pytest.fixture
def client(db_session, get_db_override, monkeypatch):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    monkeypatch.setattr(worker, "_session_factory", lambda: _factory(db_session))
    return TestClient(app)


def _factory(db_session):
    return sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)


def test_run_once_updates_last_tick_and_executes_due_jobs(db_session, monkeypatch):
    seen = {}

    def fake_tick(db, *, now, base_url, limit=50):
        seen["now"] = now
        return 3

    monkeypatch.setattr(worker.oncall_engine, "tick", fake_tick)
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    assert worker.run_once(_factory(db_session), now=now) == 3
    assert seen["now"] == now
    db_session.expire_all()
    assert oncall_settings(db_session).worker_last_tick_at == now


def test_run_once_with_real_engine_stamps_heartbeat(db_session):
    assert worker.run_once(_factory(db_session)) == 0
    db_session.expire_all()
    assert oncall_settings(db_session).worker_last_tick_at is not None


def test_worker_status_thresholds(db_session):
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    assert worker.worker_status(db_session, now=now) == "never"
    s = oncall_settings(db_session)
    s.worker_last_tick_at = now - timedelta(seconds=119)
    assert worker.worker_status(db_session, now=now) == "ok"
    s.worker_last_tick_at = now - timedelta(seconds=121)
    assert worker.worker_status(db_session, now=now) == "stale"


def test_healthz_reports_stale_but_returns_200(client, db_session):
    oncall_settings(db_session).worker_last_tick_at = datetime.now(UTC) - timedelta(minutes=10)
    db_session.commit()
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "oncall_worker": "stale"}


def test_healthz_returns_200_when_never_ticked(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "oncall_worker": "never"}


def _login_ic(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(
        db_session, email="ic@x.io", name="IC", role=Role.incident_commander, password="pw-123456"
    )
    db_session.commit()
    client.post("/login", data={"email": "ic@x.io", "password": "pw-123456"})


def test_oncall_page_shows_banner_when_worker_stale(client, db_session):
    db_session.add(EscalationPath(name="Payments"))
    oncall_settings(db_session).worker_last_tick_at = datetime.now(UTC) - timedelta(minutes=10)
    db_session.commit()
    _login_ic(client, db_session)
    assert BANNER in client.get("/oncall").text
    assert BANNER in client.get("/oncall/escalation-paths").text
    oncall_settings(db_session).worker_last_tick_at = datetime.now(UTC)
    db_session.commit()
    assert BANNER not in client.get("/oncall").text


def test_banner_hidden_without_escalation_paths(client, db_session):
    _login_ic(client, db_session)
    assert BANNER not in client.get("/oncall").text


def test_run_forever_survives_tick_exception(monkeypatch):
    stop = threading.Event()
    calls = []

    def fake_run_once(factory, *, now=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        stop.set()
        return 0

    monkeypatch.setattr(worker, "run_once", fake_run_once)
    monkeypatch.setattr("app.db.init_engine", lambda: None)
    worker.run_forever(stop, interval=0.01)
    assert len(calls) == 2


@pytest.fixture
def lifespan_db(db_session, monkeypatch):
    """Point the lifespan's own session at the test database."""

    def _get_db():
        yield db_session

    monkeypatch.setattr("app.db.get_db", _get_db)


def test_lifespan_starts_thread_when_enabled(monkeypatch, lifespan_db):
    from app.config import get_settings

    monkeypatch.setenv("ONCALL_WORKER_IN_PROCESS", "true")
    get_settings.cache_clear()
    started = []

    def fake_start():
        stop = threading.Event()
        t = threading.Thread(target=stop.wait, name="oncall-worker", daemon=True)
        t.start()
        started.append((t, stop))
        return t, stop

    monkeypatch.setattr(worker, "start_in_process", fake_start)
    try:
        with TestClient(create_app()):
            assert len(started) == 1
            assert started[0][0].is_alive()
        assert started[0][1].is_set()
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_lifespan_skips_thread_when_disabled(monkeypatch, lifespan_db):
    monkeypatch.setattr(worker, "start_in_process", lambda: pytest.fail("must not start"))
    with TestClient(create_app()):
        pass


def test_healthz_unknown_when_lookup_raises(client, monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(worker, "_lookup_last_tick", boom)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "oncall_worker": "unknown"}


def test_healthz_unknown_when_lookup_times_out(client, monkeypatch):
    import threading

    release = threading.Event()
    monkeypatch.setattr(worker, "HEALTH_TIMEOUT", 0.05)
    monkeypatch.setattr(worker.healthz_worker_status, "__kwdefaults__", {"timeout": 0.05})
    monkeypatch.setattr(worker, "_lookup_last_tick", lambda: release.wait(10))
    try:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["oncall_worker"] == "unknown"
    finally:
        release.set()
        worker._HEALTH_POOL.submit(lambda: None).result(timeout=5)  # drain the 1-worker pool


def test_healthz_creates_no_settings_row(client, db_session):
    from sqlalchemy import func, select

    from app.models import OnCallSettings

    assert client.get("/healthz").json()["oncall_worker"] == "never"
    db_session.rollback()
    assert db_session.scalar(select(func.count()).select_from(OnCallSettings)) == 0


def test_run_once_works_with_encrypted_columns_after_worker_crypto_setup(db_session, monkeypatch):
    import app.crypto as crypto

    prev = crypto._fernet
    oncall_settings(db_session).twilio_auth_token = "secret-token"
    db_session.commit()
    try:
        crypto._fernet = None  # a fresh worker process has no Fernet yet
        with pytest.raises(RuntimeError):
            worker.run_once(_factory(db_session))
        worker.configure_crypto()
        monkeypatch.setattr(worker.oncall_engine, "tick", lambda db, **kw: 0)
        assert worker.run_once(_factory(db_session)) == 0
    finally:
        crypto._fernet = prev


def test_run_forever_retries_when_init_engine_fails(monkeypatch):
    stop = threading.Event()
    calls = []

    def flaky_init():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("cannot build engine")

    monkeypatch.setattr("app.db.init_engine", flaky_init)
    monkeypatch.setattr(worker, "run_once", lambda f, now=None: stop.set() or 0)
    worker.run_forever(stop, interval=0.01)
    assert len(calls) == 2
