from unittest.mock import MagicMock, patch

from app.services import google


def test_create_meet_space_sets_smart_notes_on():
    fake_space = {"meetingUri": "https://meet.google.com/abc-defg-hij", "name": "spaces/XYZ"}
    spaces = MagicMock()
    spaces.create.return_value.execute.return_value = fake_space
    svc = MagicMock()
    svc.spaces.return_value = spaces

    with (
        patch.object(google, "_sa_credentials", return_value="CREDS"),
        patch.object(google, "build", return_value=svc),
    ):
        uri, name = google.create_meet_space(
            service_account_json='{"type":"service_account"}',
            impersonate_email="bot@example.com",
        )

    assert uri == "https://meet.google.com/abc-defg-hij"
    assert name == "spaces/XYZ"
    body = spaces.create.call_args.kwargs["body"]
    art = body["config"]["artifactConfig"]
    assert art["smartNotesConfig"]["autoSmartNotesGeneration"] == "ON"
    assert art["transcriptionConfig"]["autoTranscriptionGeneration"] == "ON"
