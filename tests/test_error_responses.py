"""Browser recovery must not change machine clients or expose submitted secrets."""

import pytest
from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.auth import require_user
from app.main import create_app
from app.models import User


@pytest.fixture
def client():
    app = create_app()

    @app.get("/_errors/status/{code}")
    def status_error(code: int):
        raise HTTPException(code, "private record Secret incident", headers={"X-Error": "kept"})

    @app.post("/_errors/form")
    def invalid_form(number: int = Form(...), password: str = Form(...)):
        return {"ok": True}

    @app.get("/api/error")
    def machine_error():
        raise HTTPException(404, "machine detail")

    @app.get("/_errors/redirect")
    def auth_redirect():
        raise HTTPException(401, headers={"HX-Redirect": "/login"})

    @app.post("/_errors/bound")
    def bound_form(request: Request):
        return HTMLResponse(
            '<form><input name="title" value="My preserved draft"></form>',
            status_code=422,
            headers={"HX-Retarget": "main", "HX-Reselect": "main > *"},
        )

    return TestClient(app)


@pytest.mark.parametrize("status", [403, 404, 422])
def test_browser_errors_are_safe_recoverable_html(client, status):
    response = client.get(f"/_errors/status/{status}", headers={"Accept": "text/html"})
    assert response.status_code == status
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["x-error"] == "kept"
    assert '<a href="/">' in response.text
    assert '<a href="/login">' in response.text
    assert "Secret incident" not in response.text
    assert "private record" not in response.text


def test_unknown_browser_path_needs_no_database_or_current_user(client):
    response = client.get("/missing/token-secret", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert "Page unavailable" in response.text
    assert "token-secret" not in response.text


@pytest.mark.parametrize("headers", [{}, {"Accept": "application/json"}])
def test_json_client_retains_fastapi_error_contract(client, headers):
    response = client.get("/_errors/status/404", headers=headers)
    assert response.status_code == 404
    assert response.json() == {"detail": "private record Secret incident"}
    assert response.headers["x-error"] == "kept"


@pytest.mark.parametrize(
    "accept", ["text/html;q=0, application/json", "application/json, text/html;q=0.5"]
)
def test_explicit_json_preference_is_respected(client, accept):
    response = client.get("/_errors/status/404", headers={"Accept": accept})
    assert response.json() == {"detail": "private record Secret incident"}


def test_api_route_stays_json_even_for_browser_accept(client):
    response = client.get("/api/error", headers={"Accept": "text/html", "HX-Request": "true"})
    assert response.json() == {"detail": "machine detail"}
    assert "HX-Retarget" not in response.headers


def test_htmx_error_targets_global_feedback_without_whole_page(client):
    response = client.get("/_errors/status/404", headers={"HX-Request": "true"})
    assert response.status_code == 404
    assert response.headers["HX-Retarget"] == "#global-error"
    assert response.headers["HX-Reswap"] == "innerHTML"
    assert "Page unavailable" in response.text
    assert "<html" not in response.text
    assert 'role="alert"' in response.text


def test_validation_html_does_not_echo_inputs_password_or_referer(client):
    response = client.post(
        "/_errors/form",
        data={"number": "submitted-token-secret", "password": "password-secret"},
        headers={"Accept": "text/html", "Referer": "http://testserver/password-secret"},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert "Check your request" in response.text
    assert "submitted-token-secret" not in response.text
    assert "password-secret" not in response.text
    assert "int_parsing" not in response.text


def test_validation_json_retains_fastapi_contract(client):
    response = client.post("/_errors/form", data={"number": "bad", "password": "pw"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "number"]
    assert response.json()["detail"][0]["type"] == "int_parsing"


def test_real_invalid_incident_id_recovers_without_echoing_path(client):
    def signed_in(request: Request):
        user = User(id=1, email="reader@example.test", name="Reader", is_active=True)
        request.state.current_user = user
        return user

    client.app.dependency_overrides[require_user] = signed_in
    response = client.get("/incidents/not-an-id-secret", headers={"Accept": "text/html"})
    assert response.status_code == 422
    assert "Check your request" in response.text
    assert "not-an-id-secret" not in response.text
    assert "reader@example.test" in response.text


def test_auth_htmx_redirect_is_preserved(client):
    response = client.get("/_errors/redirect", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"
    assert "HX-Retarget" not in response.headers
    assert response.json() == {"detail": "Unauthorized"}


def test_native_bound_validation_response_is_not_intercepted(client):
    response = client.post("/_errors/bound", headers={"HX-Request": "true"})
    assert response.status_code == 422
    assert response.headers["HX-Retarget"] == "main"
    assert response.headers["HX-Reselect"] == "main > *"
    assert "My preserved draft" in response.text


@pytest.mark.parametrize("missing", ["issuer", "client_id", "client_secret"])
def test_legacy_incomplete_sso_is_not_offered(db_session, get_db_override, missing):
    from app.db import get_db
    from app.settings_store import sso_settings

    sso = sso_settings(db_session)
    sso.sso_enabled = True
    sso.issuer = "https://idp.example.test"
    sso.client_id = "client"
    sso.client_secret = "secret"
    setattr(sso, missing, None)
    db_session.flush()
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    response = TestClient(app).get("/login")
    assert response.status_code == 200
    assert 'href="/login/oidc"' not in response.text


@pytest.mark.parametrize(
    "accept, content_type", [("text/html", "text/html"), ("application/json", "application/json")]
)
def test_csrf_origin_error_preserves_browser_and_json_contracts(client, accept, content_type):
    response = client.post(
        "/_errors/form",
        data={"number": 1, "password": "never-echo"},
        headers={"Accept": accept, "Origin": "https://outside.example"},
    )
    assert response.status_code == 403
    assert response.headers["content-type"].startswith(content_type)
    if content_type == "application/json":
        assert response.json() == {"error": "CSRF origin check failed"}
    else:
        assert "Access unavailable" in response.text
        assert "never-echo" not in response.text


def test_json_payload_is_not_html_wrapped(client):
    response = client.post(
        "/_errors/form", json={"password": "secret"}, headers={"Accept": "text/html"}
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")


def test_machine_callback_validation_stays_json_for_browser_accept(client):
    response = client.post(
        "/oncall/twilio/voice/invalid/1", headers={"Accept": "text/html", "HX-Request": "true"}
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["path", "escalation_id"]
    assert "HX-Retarget" not in response.headers


def test_regular_login_redirect_keeps_location(client):
    response = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "HX-Retarget" not in response.headers


def test_unexpected_server_bug_is_not_swallowed(client):
    @client.app.get("/_errors/bug")
    def broken():
        raise RuntimeError("diagnostic preserved")

    with pytest.raises(RuntimeError, match="diagnostic preserved"):
        client.get("/_errors/bug", headers={"Accept": "text/html"})


@pytest.mark.parametrize("missing", ["issuer", "client_id", "client_secret", None])
def test_sso_readiness_after_valid_restricted_local_login(db_session, get_db_override, missing):
    from app.db import get_db
    from app.models import Role
    from app.services.users import bootstrap_admin, create_user
    from app.settings_store import sso_settings

    bootstrap_admin(db_session, "admin@localhost")
    user = create_user(
        db_session,
        email="reader@example.test",
        name="Reader",
        role=Role.read_only,
        password="correct-local-password",
    )
    assert not user.is_protected_admin
    sso = sso_settings(db_session)
    sso.sso_enabled = True
    sso.allow_local_login = False
    sso.issuer = "https://idp.example.test"
    sso.client_id = "client"
    sso.client_secret = "secret"
    if missing:
        setattr(sso, missing, None)
    db_session.commit()
    app = create_app()
    app.dependency_overrides[get_db] = get_db_override
    browser = TestClient(app)
    response = browser.post(
        "/login",
        data={"email": user.email, "password": "correct-local-password", "next": "/alerts"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert "Local login is disabled" in response.text
    assert ('href="/login/oidc"' in response.text) is (missing is None)
    assert 'name="next" value="/alerts"' in response.text
    # Valid credentials must still not create a session when local login is disabled.
    denied = browser.get("/alerts", follow_redirects=False)
    assert denied.status_code == 303
    assert denied.headers["location"].startswith("/login")
