from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.models import effective_role
from app.services.markdown import render_markdown


def _auth_context(request) -> dict:
    # get_current_user stashes the resolved user on request.state; routes without
    # an auth dependency (login, invite, ...) never set it, so default to None.
    return {"current_user": getattr(request.state, "current_user", None)}


templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates"),
    context_processors=[_auth_context],
)

# Available in every template (e.g. base.html nav gating).
templates.env.globals["effective_role"] = effective_role

# Jinja2 filter: {{ text | markdown }} → sanitized HTML string
templates.env.filters["markdown"] = render_markdown


def _from_json(s):
    import json

    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return []


templates.env.filters["from_json"] = _from_json

from app.services.custom_fields import display_value  # noqa: E402

templates.env.globals["display_value"] = display_value


def _timeago(dt) -> str:
    if not dt:
        return "—"
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    d = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    secs = (now - d).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


templates.env.filters["timeago"] = _timeago


def _localdt(dt, tzname: str | None, fmt: str = "%a %d %b %H:%M") -> str:
    """Render an instant in the given IANA zone, e.g. 'Tue 28 Oct 09:00 Europe/Rome'."""
    if not dt:
        return "—"
    from datetime import UTC
    from zoneinfo import ZoneInfo

    d = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    if not tzname:
        return d.astimezone(UTC).strftime(fmt) + " UTC"
    return d.astimezone(ZoneInfo(tzname)).strftime(fmt) + f" {tzname}"


templates.env.filters["localdt"] = _localdt


def _openfor(dt) -> str:
    """Age of an open item: 'open for 5m' (the browser refreshes it, see base.html)."""
    if not dt:
        return "—"
    from datetime import UTC, datetime

    d = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    secs = max(0, (datetime.now(UTC) - d).total_seconds())
    if secs < 60:
        return "open for <1m"
    if secs < 3600:
        return f"open for {int(secs // 60)}m"
    if secs < 86400:
        h, m = int(secs // 3600), int(secs % 3600 // 60)
        return f"open for {h}h {m}m" if m else f"open for {h}h"
    return f"open for {int(secs // 86400)}d"


templates.env.filters["openfor"] = _openfor


def _time_tag(dt, fmt: str = "%a %d %b %H:%M", seconds: bool = False):
    """<time datetime=ISO>UTC fallback</time>; base.html rewrites it to browser-local time."""
    from datetime import UTC

    from markupsafe import Markup, escape

    if not dt:
        return "—"
    d = (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)
    iso = d.strftime("%Y-%m-%dT%H:%M:%SZ")
    attr = " data-seconds" if seconds else ""
    return Markup(f'<time datetime="{iso}"{attr}>{escape(d.strftime(fmt))} UTC</time>')


templates.env.filters["time_tag"] = _time_tag
