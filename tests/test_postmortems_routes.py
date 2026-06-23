import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import Postmortem, Role, SeverityLevel
from app.services import roles, statuses
from app.services.incidents import create_incident
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _login(client, db_session, email, role):
    bootstrap_admin(db_session, "admin@localhost")
    create_user(db_session, email=email, name="U", role=role, password="pw-123456")
    db_session.flush()
    client.post("/login", data={"email": email, "password": "pw-123456"})


def _incident(db_session):
    statuses.seed_status_levels(db_session)
    roles.seed_incident_role_types(db_session)
    db_session.flush()
    lvl = SeverityLevel(label="SEV1", color="#FF5D5D", rank=1, is_default=True)
    db_session.add(lvl)
    db_session.flush()
    inc = create_incident(
        db_session, title="X", severity_level_id=lvl.id, is_private=False, created_by=1
    )
    db_session.flush()
    return inc


def test_readonly_cannot_generate(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)
    assert (
        client.post("/incidents/1/postmortem/generate", follow_redirects=False).status_code == 403
    )


def test_generate_view_download(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    db_session.commit()
    r = client.post(f"/incidents/{inc.id}/postmortem/generate", follow_redirects=False)
    assert r.status_code == 303
    assert db_session.scalar(select(Postmortem).where(Postmortem.incident_id == inc.id)) is not None
    v = client.get(f"/incidents/{inc.id}/postmortem")
    assert v.status_code == 200 and "Postmortem" in v.text
    d = client.get(f"/incidents/{inc.id}/postmortem.md")
    assert d.status_code == 200
    assert d.headers["content-type"].startswith("text/markdown")
    assert "attachment" in d.headers.get("content-disposition", "")
    assert "# Postmortem: X" in d.text


def test_view_is_require_user(client, db_session):
    _login(client, db_session, "ro@x.io", Role.read_only)
    inc = _incident(db_session)
    db_session.commit()
    assert client.get(f"/incidents/{inc.id}/postmortem").status_code == 200  # read-only can view


def test_edit_saves(client, db_session):
    _login(client, db_session, "ic@x.io", Role.incident_commander)
    inc = _incident(db_session)
    db_session.commit()
    client.post(f"/incidents/{inc.id}/postmortem/generate", follow_redirects=False)
    client.post(
        f"/incidents/{inc.id}/postmortem", data={"body": "Edited body"}, follow_redirects=False
    )
    pm = db_session.scalar(select(Postmortem).where(Postmortem.incident_id == inc.id))
    db_session.refresh(pm)
    assert pm.body == "Edited body"
