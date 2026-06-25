from unittest.mock import MagicMock, patch

from app.services import google


def test_fetch_notes_via_conference_records():
    # Build mocks for the Meet API chain
    meet = MagicMock()
    meet.conferenceRecords().list(filter='space.name="spaces/ABC"').execute.return_value = {
        "conferenceRecords": [{"name": "conferenceRecords/R1"}]
    }
    meet.conferenceRecords().transcripts().list(
        parent="conferenceRecords/R1"
    ).execute.return_value = {
        "transcripts": [
            {"state": "STARTED", "docsDestination": {}},
            {"state": "FILE_GENERATED", "docsDestination": {"document": "DOC1"}},
        ]
    }

    # Build mocks for the Drive API chain
    drive = MagicMock()
    drive.files().export(
        fileId="DOC1", mimeType="text/plain"
    ).execute.return_value = b"  the notes  "

    with (
        patch.object(google, "_sa_credentials", return_value="CREDS"),
        patch.object(google, "build", side_effect=[meet, drive]),
    ):
        result = google.fetch_gemini_notes_text(
            service_account_json='{"type":"service_account"}',
            impersonate_email="bot@example.com",
            space_name="spaces/ABC",
        )

    assert result == "the notes"
    # Verify the filter kwarg contains the space name
    list_call_kwargs = meet.conferenceRecords().list.call_args
    assert "spaces/ABC" in str(list_call_kwargs)
    # Verify export was called with the correct doc id (FILE_GENERATED, not STARTED)
    drive.files().export.assert_called_with(fileId="DOC1", mimeType="text/plain")


def test_fetch_notes_returns_none_when_no_record():
    meet = MagicMock()
    meet.conferenceRecords().list(filter='space.name="spaces/EMPTY"').execute.return_value = {
        "conferenceRecords": []
    }

    with (
        patch.object(google, "_sa_credentials", return_value="CREDS"),
        patch.object(google, "build", return_value=meet),
    ):
        result = google.fetch_gemini_notes_text(
            service_account_json='{"type":"service_account"}',
            impersonate_email="bot@example.com",
            space_name="spaces/EMPTY",
        )

    assert result is None


def test_fetch_notes_returns_none_when_no_file_generated_transcript():
    # Records exist, but the transcript is still being produced (notes not ready yet) —
    # the common "poll again later" case. Drive must never be built.
    meet = MagicMock()
    meet.conferenceRecords().list(filter='space.name="spaces/WIP"').execute.return_value = {
        "conferenceRecords": [{"name": "conferenceRecords/R1"}]
    }
    meet.conferenceRecords().transcripts().list(
        parent="conferenceRecords/R1"
    ).execute.return_value = {
        "transcripts": [
            {"state": "STARTED", "docsDestination": {}},
            {"state": "ENDED", "docsDestination": {}},
        ]
    }

    build_mock = MagicMock(return_value=meet)
    with (
        patch.object(google, "_sa_credentials", return_value="CREDS"),
        patch.object(google, "build", build_mock),
    ):
        result = google.fetch_gemini_notes_text(
            service_account_json='{"type":"service_account"}',
            impersonate_email="bot@example.com",
            space_name="spaces/WIP",
        )

    assert result is None
    # Drive is never built when no notes doc is ready (only the "meet" build happened).
    assert all(call.args[0] != "drive" for call in build_mock.call_args_list)
