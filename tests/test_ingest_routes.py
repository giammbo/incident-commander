import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import Alert, InboundIntegration


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _integ(db, kind="alertmanager", token="tk-1", settings=None):
    integ = InboundIntegration(name="i", kind=kind, token=token, settings=settings or {})
    db.add(integ)
    db.commit()
    return integ


def test_unknown_token_401(client, db_session):
    assert client.post("/ingest/nope", json={}).status_code == 401


def test_oversized_body_rejected_413(client, db_session):
    _integ(db_session, kind="generic", token="big-1")
    huge = {"id": "x", "blob": "A" * 300_000}  # > 256 KB cap
    assert client.post("/ingest/big-1", json=huge).status_code == 413


def test_alertmanager_ingest_no_session(client, db_session):
    _integ(db_session, kind="alertmanager", token="am-1")
    body = {
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "fp",
                "labels": {"alertname": "X"},
                "annotations": {},
            }
        ]
    }
    r = client.post("/ingest/am-1", json=body)
    assert r.status_code == 200
    assert db_session.scalar(select(Alert).where(Alert.dedup_key == "fp")).title == "X"


def test_sns_subscription_confirmation_confirms(client, db_session, monkeypatch):
    _integ(db_session, kind="sns", token="sns-1")
    visited = {}
    monkeypatch.setattr(
        "app.routers.ingest._confirm_subscription", lambda url: visited.setdefault("url", url)
    )
    env = {"Type": "SubscriptionConfirmation", "SubscribeURL": "https://sns.eu.amazonaws.com/c?x=1"}
    r = client.post("/ingest/sns-1", json=env)
    assert r.status_code == 200 and visited["url"].startswith("https://sns")


def test_disabled_integration_401(client, db_session):
    integ = _integ(db_session, token="off-1")
    integ.enabled = False
    db_session.commit()
    assert client.post("/ingest/off-1", json={}).status_code == 401


def test_sns_non_aws_subscribe_url_skips_get(client, db_session, monkeypatch):
    """SSRF mitigation: SubscriptionConfirmation with a non-AWS SubscribeURL must NOT trigger a GET."""
    _integ(db_session, kind="sns", token="sns-2")
    get_calls = []

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "get", lambda *args, **kwargs: get_calls.append(args[0]))

    env = {"Type": "SubscriptionConfirmation", "SubscribeURL": "https://attacker.example.com/pwn"}
    r = client.post("/ingest/sns-2", json=env)
    assert r.status_code == 200
    assert get_calls == [], "httpx.get must NOT be called for non-AWS SubscribeURL"
