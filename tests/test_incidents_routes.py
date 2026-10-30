import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Role, SeverityLevel
from app.services import statuses
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    return TestClient(app)


def _login(client, db_session, email, role):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email=email, name="U", role=role, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": email, "password": "pw-123456"})


def _sev(db_session):
    statuses.seed_status_levels(db_session)
    db_session.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=False)
    db_session.add(lvl)
    db_session.flush()
    return lvl.id


def test_readonly_cannot_create(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)
    sev_id = _sev(db_session)
    r = client.post(
        "/incidents",
        data={"title": "X", "severity_level_id": str(sev_id)},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_ic_can_create_and_close(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    sev_id = _sev(db_session)
    r = client.post(
        "/incidents",
        data={"title": "Checkout down", "severity_level_id": str(sev_id)},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert r.headers["HX-Redirect"].startswith("/incidents/")
    # list shows it
    assert "Checkout down" in client.get("/").text
    # close it (find id via detail listing)
    from app.services.incidents import list_incidents

    inc = list_incidents(db_session)[0]
    r2 = client.post(f"/incidents/{inc.id}/close", headers={"HX-Request": "true"})
    assert r2.status_code == 200
    assert "closed" in r2.text.lower()


def test_create_incident_with_system_and_component(client, db_session):
    # reuse this file's real helper: _login(client, db_session, email, role)
    from sqlalchemy import select

    from app.models import Incident, Role, SeverityLevel
    from app.services.catalog import create_component, create_system

    _login(client, db_session, "ic@x.io", Role.incident_commander)
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    s = create_system(db_session, name="Billing")
    db_session.flush()
    c = create_component(db_session, name="Invoicer", system_id=s.id)
    db_session.flush()
    client.post(
        "/incidents",
        data={
            "title": "Down",
            "severity_level_id": str(lvl.id),
            "system_id": str(s.id),
            "component_ids": [str(c.id)],
        },
        headers={"HX-Request": "true"},
    )
    inc = db_session.scalar(select(Incident).order_by(Incident.id.desc()))
    assert inc.system_id == s.id and [x.name for x in inc.components] == ["Invoicer"]


def _stats_span(html: str) -> str:
    import re

    m = re.search(r'<span class="stats" id="inc-stats"[^>]*>.*?total</span>', html, re.S)
    assert m, "stats span with id inc-stats not found"
    return m.group(0)


def test_index_stats_span_has_id(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    _sev(db_session)
    db_session.commit()
    span = _stats_span(client.get("/").text)
    assert "hx-swap-oob" not in span
    assert "<b>0</b> open" in span and "0 total" in span


def test_htmx_create_redirects_to_each_new_incident(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    sev_id = _sev(db_session)
    db_session.commit()
    destinations = []
    for title in ("First", "Second"):
        response = client.post(
            "/incidents",
            data={"title": title, "severity_level_id": str(sev_id)},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        destinations.append(response.headers["HX-Redirect"])
        assert title in client.get(destinations[-1]).text
    assert destinations[0] != destinations[1]
    span = _stats_span(client.get("/").text)
    assert "<b>2</b> open" in span and "2 total" in span


def test_htmx_close_returns_oob_stats_with_new_counts(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    sev_id = _sev(db_session)
    db_session.commit()
    client.post(
        "/incidents",
        data={"title": "Only", "severity_level_id": str(sev_id)},
        headers={"HX-Request": "true"},
    )
    from app.services.incidents import list_incidents

    inc = list_incidents(db_session)[0]
    r = client.post(f"/incidents/{inc.id}/close", headers={"HX-Request": "true"})
    span = _stats_span(r.text)
    assert 'hx-swap-oob="true"' in span
    assert "<b>0</b> open" in span and "1 total" in span


def test_declare_form_retains_bound_errors_and_opens_detail_on_success(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    severity = _sev(db_session)
    response = client.post(
        "/incidents",
        data={"title": "   ", "description": "Draft", "severity_level_id": severity},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422 and "Draft" in response.text
    assert response.headers["HX-Retarget"] == "main"
    assert "this.reset()" not in response.text
