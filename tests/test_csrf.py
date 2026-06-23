import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.security.csrf import CSRFMiddleware, _host


def _app():
    async def ok(request):
        return PlainTextResponse("ok")

    return Starlette(
        routes=[
            Route("/x", ok, methods=["GET", "POST"]),
            Route("/ingest/tok", ok, methods=["POST"]),
        ],
        middleware=[Middleware(CSRFMiddleware)],
    )


@pytest.fixture
def client():
    return TestClient(_app())  # default Host header is "testserver"


def test_host_extraction():
    assert _host("https://example.com:8000/path") == "example.com:8000"
    assert _host("example.com:8000") == "example.com:8000"
    assert _host(None) == "" and _host("") == ""


def test_safe_method_passes_with_foreign_origin(client):
    assert client.get("/x", headers={"origin": "https://evil.example"}).status_code == 200


def test_post_foreign_origin_blocked(client):
    r = client.post("/x", headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and "CSRF" in r.json()["error"]


def test_post_same_origin_allowed(client):
    # Origin host == request Host header ("testserver")
    assert client.post("/x", headers={"origin": "http://testserver"}).status_code == 200


def test_post_no_origin_no_referer_allowed(client):
    assert client.post("/x").status_code == 200


def test_referer_used_as_fallback(client):
    assert client.post("/x", headers={"referer": "https://evil.example/p"}).status_code == 403
    assert client.post("/x", headers={"referer": "http://testserver/p"}).status_code == 200


def test_ingest_exempt_even_with_foreign_origin(client):
    assert client.post("/ingest/tok", headers={"origin": "https://evil.example"}).status_code == 200
