import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_db
from app.main import create_app
from app.models import InboundIntegration, Role
from app.services.users import bootstrap_admin, create_user


@pytest.fixture
def client(db_session, get_db_override):
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
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


def _generic(db_session, token="tok-gen", settings=None):
    integ = InboundIntegration(name="gen", kind="generic", token=token, settings=settings or {})
    db_session.add(integ)
    db_session.commit()
    return integ


def test_admin_saves_generic_mapping_only_non_empty_keys(client, db_session):
    _login(client, db_session, Role.admin)
    integ = _generic(db_session)
    r = client.post(
        f"/settings/integrations/{integ.id}/settings",
        data={
            "dedup_key": " alert.id ",
            "title": "alert.name",
            "status": "",
            "severity": "alert.level",
            "resolved_value": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.refresh(integ)
    assert integ.settings == {
        "dedup_key": "alert.id",
        "title": "alert.name",
        "severity": "alert.level",
    }


def test_saving_mapping_clears_previous_paths(client, db_session):
    _login(client, db_session, Role.admin)
    integ = _generic(db_session, settings={"dedup_key": "a", "title": "old.title", "status": "s"})
    client.post(
        f"/settings/integrations/{integ.id}/settings",
        data={"dedup_key": "b", "title": "", "status": "st", "resolved_value": "ok"},
        follow_redirects=False,
    )
    db_session.refresh(integ)
    assert integ.settings == {"dedup_key": "b", "status": "st", "resolved_value": "ok"}


def test_blank_dedup_key_is_rejected(client, db_session):
    _login(client, db_session, Role.admin)
    integ = _generic(db_session, settings={"dedup_key": "keep.me"})
    r = client.post(
        f"/settings/integrations/{integ.id}/settings",
        data={"dedup_key": "   ", "title": "t"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.refresh(integ)
    assert integ.settings == {"dedup_key": "keep.me"}


def test_mapping_rejected_for_non_generic_kind(client, db_session):
    _login(client, db_session, Role.admin)
    integ = InboundIntegration(name="am", kind="alertmanager", token="tok-am", settings={})
    db_session.add(integ)
    db_session.commit()
    client.post(
        f"/settings/integrations/{integ.id}/settings",
        data={"dedup_key": "x"},
        follow_redirects=False,
    )
    db_session.refresh(integ)
    assert integ.settings == {}


def test_mapping_unknown_integration_404(client, db_session):
    _login(client, db_session, Role.admin)
    r = client.post(
        "/settings/integrations/9999/settings", data={"dedup_key": "x"}, follow_redirects=False
    )
    assert r.status_code == 404


def test_non_admin_cannot_save_mapping(client, db_session):
    integ = _generic(db_session)
    _login(client, db_session, Role.incident_commander)
    r = client.post(
        f"/settings/integrations/{integ.id}/settings",
        data={"dedup_key": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    db_session.refresh(integ)
    assert integ.settings == {}


def test_settings_page_shows_generic_mapping_form_with_current_values(client, db_session):
    _login(client, db_session, Role.admin)
    integ = _generic(db_session, settings={"dedup_key": "alert.id", "title": "alert.name"})
    other = InboundIntegration(name="am", kind="alertmanager", token="tok-am2", settings={})
    db_session.add(other)
    db_session.commit()
    html = client.get("/settings").text
    assert f'action="/settings/integrations/{integ.id}/settings"' in html
    assert f'action="/settings/integrations/{other.id}/settings"' not in html
    assert 'name="dedup_key" value="alert.id" required' in html
    assert 'name="title" value="alert.name"' in html
    # resolved_value falls back to the parser default when unset
    assert 'name="resolved_value" value="resolved"' in html


def test_settings_page_flags_unconfigured_generic(client, db_session):
    _login(client, db_session, Role.admin)
    _generic(db_session)
    html = client.get("/settings").text
    assert "Not configured" in html
