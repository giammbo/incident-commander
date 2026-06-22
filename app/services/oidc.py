from __future__ import annotations

from urllib.parse import urlencode

import httpx
import jwt

DEFAULT_SCOPES = ("openid", "email", "profile")
_DISCOVERY_CACHE: dict[str, dict] = {}


def discover(issuer: str) -> dict:
    issuer = issuer.rstrip("/")
    cached = _DISCOVERY_CACHE.get(issuer)
    if cached is not None:
        return cached
    resp = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=10)
    resp.raise_for_status()
    doc = resp.json()
    if doc.get("issuer") != issuer:
        raise ValueError("OIDC discovery issuer mismatch")
    _DISCOVERY_CACHE[issuer] = doc
    return doc


def authorize_url(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    scopes=DEFAULT_SCOPES,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "nonce": nonce,
    }
    return f"{authorization_endpoint}?{urlencode(params)}"


def exchange_code(
    *, token_endpoint: str, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict:
    resp = httpx.post(
        token_endpoint,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def verify_id_token(
    *, id_token: str, jwks_uri: str, issuer: str, client_id: str, nonce: str
) -> dict:
    signing_key = jwt.PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256", "ES256"],
        audience=client_id,
        issuer=issuer,
        options={"require": ["exp", "iat", "sub"]},
    )
    if claims.get("nonce") != nonce:
        raise ValueError("OIDC nonce mismatch")
    return claims
