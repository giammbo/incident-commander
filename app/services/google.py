from __future__ import annotations

import time
import uuid
from urllib.parse import urlencode

import httpx
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SSO_SCOPES = ["openid", "email", "profile"]
MEET_SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly",
]
_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def authorize_url(
    *, client_id: str, redirect_uri: str, state: str, scopes: list[str], offline: bool = False
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    if offline:
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    return f"{_AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code(*, client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    resp = httpx.post(
        _TOKEN_ENDPOINT,
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


def verify_id_token(id_token_str: str, *, client_id: str) -> dict:
    return google_id_token.verify_oauth2_token(id_token_str, google_requests.Request(), client_id)


_NOTES_MIME = "application/vnd.google-apps.document"


def _google_services(*, client_id: str, client_secret: str, refresh_token: str):
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=_TOKEN_ENDPOINT,
        client_id=client_id,
        client_secret=client_secret,
        scopes=[
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )
    creds.refresh(google_requests.Request())
    calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return calendar, drive


def fetch_gemini_notes_text(
    *, client_id: str, client_secret: str, refresh_token: str, calendar_id: str, event_id: str
) -> str | None:
    calendar, drive = _google_services(
        client_id=client_id, client_secret=client_secret, refresh_token=refresh_token
    )
    event = calendar.events().get(calendarId=calendar_id, eventId=event_id).execute()
    attachments = event.get("attachments") or []
    docs = [a for a in attachments if a.get("mimeType") == _NOTES_MIME]
    if not docs:
        return None
    pick = next(
        (a for a in docs if any(k in (a.get("title") or "").lower() for k in ("gemini", "notes"))),
        docs[0],
    )
    file_id = pick.get("fileId")
    if not file_id:
        return None
    data = drive.files().export(fileId=file_id, mimeType="text/plain").execute()
    text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
    text = text.strip()
    return text or None


def _calendar_service(*, client_id: str, client_secret: str, refresh_token: str):
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=_TOKEN_ENDPOINT,
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/calendar.events"],
    )
    creds.refresh(google_requests.Request())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def create_meet(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    calendar_id: str,
    summary: str,
    now_iso: str,
    end_iso: str,
    attempts: int = 5,
    sleep=time.sleep,
) -> tuple[str | None, str | None]:
    service = _calendar_service(
        client_id=client_id, client_secret=client_secret, refresh_token=refresh_token
    )
    body = {
        "summary": summary,
        "start": {"dateTime": now_iso},
        "end": {"dateTime": end_iso},
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    event = (
        service.events()
        .insert(calendarId=calendar_id, conferenceDataVersion=1, body=body)
        .execute()
    )
    event_id = event.get("id")
    link = event.get("hangoutLink")
    for _ in range(attempts):
        if link:
            return link, event_id
        sleep(1)
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        link = event.get("hangoutLink")
    return link, event_id
