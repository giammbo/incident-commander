import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import app.services.oidc as oidc


def _rsa_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return priv, key.public_key()


def test_authorize_url_has_state_and_nonce():
    url = oidc.authorize_url(
        authorization_endpoint="https://idp/authorize",
        client_id="cid",
        redirect_uri="https://app/cb",
        state="st",
        nonce="no",
        scopes=("openid", "email"),
    )
    assert url.startswith("https://idp/authorize?")
    assert "state=st" in url and "nonce=no" in url and "response_type=code" in url
    assert "client_id=cid" in url and "scope=openid+email" in url


def test_discover_parses_and_caches(monkeypatch):
    oidc._DISCOVERY_CACHE.clear()
    calls = []

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "issuer": "https://idp",
                "authorization_endpoint": "https://idp/a",
                "token_endpoint": "https://idp/t",
                "jwks_uri": "https://idp/jwks",
            }

    monkeypatch.setattr(oidc.httpx, "get", lambda url, timeout=10: calls.append(url) or R())
    a = oidc.discover("https://idp/")
    b = oidc.discover("https://idp/")  # cached → no 2nd HTTP call
    assert a["token_endpoint"] == "https://idp/t" and b == a
    assert calls == ["https://idp/.well-known/openid-configuration"]


def test_verify_id_token_ok_and_rejects(monkeypatch):
    priv, pub = _rsa_pem()
    token = jwt.encode(
        {
            "iss": "https://idp",
            "aud": "cid",
            "sub": "u1",
            "nonce": "no",
            "exp": 9999999999,
            "iat": 1,
        },
        priv,
        algorithm="RS256",
    )

    class FakeKey:
        key = pub

    monkeypatch.setattr(
        oidc.jwt,
        "PyJWKClient",
        lambda uri: type("C", (), {"get_signing_key_from_jwt": lambda self, t: FakeKey()})(),
    )
    claims = oidc.verify_id_token(
        id_token=token,
        jwks_uri="https://idp/jwks",
        issuer="https://idp",
        client_id="cid",
        nonce="no",
    )
    assert claims["sub"] == "u1"
    with pytest.raises(Exception):  # nonce mismatch
        oidc.verify_id_token(
            id_token=token,
            jwks_uri="https://idp/jwks",
            issuer="https://idp",
            client_id="cid",
            nonce="WRONG",
        )
    with pytest.raises(Exception):  # audience mismatch
        oidc.verify_id_token(
            id_token=token,
            jwks_uri="https://idp/jwks",
            issuer="https://idp",
            client_id="other",
            nonce="no",
        )
