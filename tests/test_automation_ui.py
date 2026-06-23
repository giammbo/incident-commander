import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Role, WorkflowRule
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _login_admin(client, db_session):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email="a@x.io", name="A", role=Role.admin, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": "a@x.io", "password": "pw-123456"})


def test_automations_page_lists_rules(client, db_session):
    _login_admin(client, db_session)
    db_session.add(
        WorkflowRule(name="My Rule", trigger="incident.opened", conditions=[], actions=[])
    )
    db_session.commit()
    body = client.get("/automations").text
    assert "My Rule" in body and "incident.opened" in body


def test_action_params_partial(client, db_session):
    _login_admin(client, db_session)
    # the HTMX partial returns the param inputs for a chosen action type
    body = client.get("/automations/action-params?type=create_followup").text
    assert "title" in body  # create_followup exposes a title input


def test_action_params_partial_requires_admin(client, db_session):
    _login_admin(client, db_session)  # admin ok
    assert client.get("/automations/action-params?type=set_status").status_code == 200
