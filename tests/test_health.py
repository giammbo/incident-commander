from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz_ok(db_session, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from app.services import oncall_worker as worker

    monkeypatch.setattr(
        worker,
        "_session_factory",
        lambda: sessionmaker(bind=db_session.get_bind(), expire_on_commit=False),
    )
    resp = TestClient(create_app()).get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "oncall_worker": "never"}
