from __future__ import annotations

import re
from urllib.parse import urlencode

from slack_sdk import WebClient

_SANITIZE_RE = re.compile(r"[^a-z0-9_-]+")


def sanitize_channel_name(raw: str, max_len: int = 80) -> str:
    s = raw.strip().lower().replace(" ", "-")
    s = _SANITIZE_RE.sub("", s)
    s = re.sub(r"[-_]{2,}", "-", s).strip("-_")
    return s[:max_len]


def build_channel_name(template: str, *, title: str, date_str: str) -> str:
    slug = sanitize_channel_name(title)
    name = template.replace("{date}", date_str).replace("{slug}", slug)
    return sanitize_channel_name(name)


def authorize_url(*, client_id: str, redirect_uri: str, state: str, scopes: list[str]) -> str:
    q = urlencode(
        {
            "client_id": client_id,
            "scope": ",".join(scopes),
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"https://slack.com/oauth/v2/authorize?{q}"


def exchange_code(*, client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    resp = WebClient().oauth_v2_access(
        client_id=client_id, client_secret=client_secret, code=code, redirect_uri=redirect_uri
    )
    return dict(resp.data)


def channel_url(team_id: str, channel_id: str) -> str:
    return f"https://slack.com/app_redirect?channel={channel_id}&team={team_id}"


def create_channel(token: str, *, name: str, is_private: bool) -> dict:
    resp = WebClient(token=token).conversations_create(name=name, is_private=is_private)
    ch = resp.data["channel"]
    return {"id": ch["id"], "name": ch["name"]}


def set_topic_purpose(token: str, *, channel_id: str, topic: str, purpose: str) -> None:
    client = WebClient(token=token)
    client.conversations_setTopic(channel=channel_id, topic=topic[:250])
    client.conversations_setPurpose(channel=channel_id, purpose=purpose[:250])


def post_message(token: str, *, channel_id: str, text: str) -> None:
    WebClient(token=token).chat_postMessage(channel=channel_id, text=text)
