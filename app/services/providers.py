from __future__ import annotations

from typing import Protocol


class VideoProvider(Protocol):
    key: str
    label: str
    needs_connection: bool

    def create(self, db, incident, *, connection=None) -> None: ...


class ChatProvider(Protocol):
    key: str
    label: str

    def open_room(self, db, incident, *, connection) -> None: ...
    def post_update(self, db, incident, *, connection) -> None: ...
    def post_closed(self, db, incident, *, connection) -> None: ...
    def announce_video(self, db, incident, *, connection) -> None: ...
    def post_announcement(self, db, incident, *, connection, text) -> None: ...


class MeetVideoProvider:
    key = "meet"
    label = "Google Meet"
    needs_connection = True

    def create(self, db, incident, *, connection=None) -> None:
        from app.services import incident_actions

        incident_actions.open_incident_google(db, incident, connection)


class SlackChatProvider:
    key = "slack"
    label = "Slack"

    def open_room(self, db, incident, *, connection) -> None:
        from app.services import incident_actions

        incident_actions.open_incident_slack(db, incident, connection)

    def post_update(self, db, incident, *, connection) -> None:
        from app.services import incident_actions

        incident_actions.update_incident_slack(db, incident, connection)

    def post_closed(self, db, incident, *, connection) -> None:
        from app.services import incident_actions

        incident_actions.close_incident_slack(db, incident, connection)

    def announce_video(self, db, incident, *, connection) -> None:
        from app.services import incident_actions

        incident_actions.announce_meet_in_slack(db, incident, connection)

    def post_announcement(self, db, incident, *, connection, text) -> None:
        from app.services import incident_actions

        incident_actions.post_announcement(db, incident, connection, text)


VIDEO_PROVIDERS: dict[str, VideoProvider] = {
    "meet": MeetVideoProvider(),
}
CHAT_PROVIDERS: dict[str, ChatProvider] = {"slack": SlackChatProvider()}


def parse_video_choice(value: str) -> tuple[str | None, int | None]:
    """'' -> (None, None); 'meet:5' -> ('meet', 5); unknown/invalid -> (None, None)."""
    if not value:
        return (None, None)
    if value.startswith("meet:"):
        rest = value.split(":", 1)[1]
        return ("meet", int(rest)) if rest.isdigit() else (None, None)
    if value in VIDEO_PROVIDERS:
        return (value, None)
    return (None, None)
