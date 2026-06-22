import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Component, Role, System
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _login(client, db_session, *, role=Role.read_only, email="ro@x.io"):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email=email, name="U", role=role, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": email, "password": "pw-123456"})


def test_maps_page_renders_for_readonly(client, db_session):
    _login(client, db_session)
    r = client.get("/maps")
    assert r.status_code == 200
    assert "/static/3d-force-graph.min.js" in r.text and "/maps/graph.json" in r.text


def test_graph_json_shape(client, db_session):
    _login(client, db_session)
    s = System(name="Billing")
    db_session.add(s)
    db_session.flush()
    db_session.add(Component(name="A", system_id=s.id))
    db_session.flush()
    data = client.get("/maps/graph.json").json()
    assert "nodes" in data and "links" in data
    assert any(n["type"] == "system" for n in data["nodes"])
    assert any(n["type"] == "component" for n in data["nodes"])
    assert any(lnk["kind"] == "contains" for lnk in data["links"])  # System→Component edge


def test_maps_requires_login(client):
    assert client.get("/maps", follow_redirects=False).status_code in (303, 307)
    # the data endpoint is also gated
    assert client.get("/maps/graph.json", follow_redirects=False).status_code in (303, 307)


def test_nav_has_maps_link(client, db_session):
    _login(client, db_session)
    assert 'href="/maps"' in client.get("/").text
