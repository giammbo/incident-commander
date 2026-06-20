import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.services.users import bootstrap_admin
from app.settings_store import slack_settings


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _admin(client, db_session):
    _, pw = bootstrap_admin(db_session, "admin@localhost")
    db_session.flush()
    client.post("/login", data={"email": "admin@localhost", "password": pw})
    client.post("/account/password", data={"new_password": "Admin-123", "confirm": "Admin-123"})


def test_save_slack_and_keep_secret(client, db_session):
    _admin(client, db_session)
    client.post(
        "/settings/slack",
        data={
            "client_id": "cid",
            "client_secret": "csec",
            "signing_secret": "sig",
            "enabled": "true",
        },
        follow_redirects=False,
    )
    s = slack_settings(db_session)
    assert s.client_id == "cid" and s.client_secret == "csec" and s.enabled is True
    client.post(
        "/settings/slack",
        data={"client_id": "cid2", "client_secret": "", "signing_secret": "", "enabled": "true"},
        follow_redirects=False,
    )
    s2 = slack_settings(db_session)
    assert s2.client_id == "cid2" and s2.client_secret == "csec" and s2.signing_secret == "sig"
