from __future__ import annotations

import json

from google.oauth2 import service_account
from googleapiclient.discovery import build

MEET_SCOPES = [
    "https://www.googleapis.com/auth/meetings.space.created",
    "https://www.googleapis.com/auth/drive.readonly",
]


def fetch_gemini_notes_text(
    *, service_account_json: str, impersonate_email: str, space_name: str
) -> str | None:
    """Fetch the Gemini smart-notes Doc text for a Meet space via conferenceRecords (exact match)."""
    creds = _sa_credentials(
        service_account_json=service_account_json,
        impersonate_email=impersonate_email,
        scopes=MEET_SCOPES,
    )
    meet = build("meet", "v2", credentials=creds, cache_discovery=False)
    records = meet.conferenceRecords().list(filter=f'space.name="{space_name}"').execute()
    doc_id = None
    for rec in records.get("conferenceRecords") or []:
        transcripts = meet.conferenceRecords().transcripts().list(parent=rec["name"]).execute()
        for t in transcripts.get("transcripts") or []:
            dest = t.get("docsDestination") or {}
            if t.get("state") == "FILE_GENERATED" and dest.get("document"):
                doc_id = dest["document"]
                break
        if doc_id:
            break
    if not doc_id:
        return None
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    data = drive.files().export(fileId=doc_id, mimeType="text/plain").execute()
    text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
    return text.strip() or None


def _sa_credentials(*, service_account_json: str, impersonate_email: str, scopes: list[str]):
    info = json.loads(service_account_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    return creds.with_subject(impersonate_email)


def create_meet_space(
    *, service_account_json: str, impersonate_email: str
) -> tuple[str | None, str | None]:
    creds = _sa_credentials(
        service_account_json=service_account_json,
        impersonate_email=impersonate_email,
        scopes=MEET_SCOPES,
    )
    service = build("meet", "v2", credentials=creds, cache_discovery=False)
    body = {
        "config": {
            "artifactConfig": {
                "smartNotesConfig": {"autoSmartNotesGeneration": "ON"},
                "transcriptionConfig": {"autoTranscriptionGeneration": "ON"},
            }
        }
    }
    space = service.spaces().create(body=body).execute()
    return space.get("meetingUri"), space.get("name")
