import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import InboundIntegration, Role
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _login(client, db_session, role):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email="u@x.io", name="U", role=role, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": "u@x.io", "password": "pw-123456"})


def test_admin_creates_integration_with_token(client, db_session):
    _login(client, db_session, Role.admin)
    r = client.post(
        "/settings/integrations", data={"name": "prod", "kind": "sns"}, follow_redirects=False
    )
    assert r.status_code == 303
    integ = db_session.scalar(select(InboundIntegration).where(InboundIntegration.name == "prod"))
    assert integ is not None and integ.kind == "sns" and len(integ.token) >= 20


def test_non_admin_cannot_create(client, db_session):
    _login(client, db_session, Role.incident_commander)
    assert (
        client.post(
            "/settings/integrations", data={"name": "x", "kind": "sns"}, follow_redirects=False
        ).status_code
        == 403
    )


def test_delete_integration(client, db_session):
    _login(client, db_session, Role.admin)
    integ = InboundIntegration(name="d", kind="generic", token="tok-del")
    db_session.add(integ)
    db_session.commit()
    r = client.post(f"/settings/integrations/{integ.id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert (
        db_session.scalar(select(InboundIntegration).where(InboundIntegration.id == integ.id))
        is None
    )


def test_non_admin_cannot_delete(client, db_session):
    integ = InboundIntegration(name="d2", kind="generic", token="tok-del-2")
    db_session.add(integ)
    db_session.commit()
    _login(client, db_session, Role.incident_commander)
    r = client.post(f"/settings/integrations/{integ.id}/delete", follow_redirects=False)
    assert r.status_code == 403
    # still present — the delete was rejected
    assert (
        db_session.scalar(select(InboundIntegration).where(InboundIntegration.id == integ.id))
        is not None
    )
