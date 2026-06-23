from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Incident


def window_since(days: int) -> datetime | None:
    if days <= 0:
        return None
    return datetime.now(UTC) - timedelta(days=days)


def _humanize_duration(delta: timedelta) -> str:
    total = max(int(delta.total_seconds()), 0)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins or not parts:
        parts.append(f"{mins}m")
    return " ".join(parts)


# Status category → bar color (mirrors the CSS tokens in app.css).
_STATUS_COLOR = {"active": "#F25533", "triage": "#F4B740", "closed": "#6B7B8C"}


def _breakdown(incidents, keyfn, colorfn=None) -> list[dict]:
    counts: Counter = Counter()
    colors: dict[str, str | None] = {}
    for i in incidents:
        label = keyfn(i)
        counts[label] += 1
        if colorfn is not None and label not in colors:
            colors[label] = colorfn(i)
    return [
        {"label": label, "count": n, "color": colors.get(label)}
        for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def compute_insights(db: Session, *, since: datetime | None) -> dict:
    stmt = select(Incident)
    if since is not None:
        stmt = stmt.where(Incident.created_at >= since)
    incidents = list(db.scalars(stmt))

    total = len(incidents)
    closed = [i for i in incidents if i.is_closed]
    durs = [i.closed_at - i.created_at for i in incidents if i.closed_at and i.created_at]
    mttr = _humanize_duration(sum(durs, timedelta()) / len(durs)) if durs else None
    open_followups = sum(1 for i in incidents for f in i.follow_ups if f.status == "open")

    return {
        "total": total,
        "open": total - len(closed),
        "closed": len(closed),
        "mttr": mttr,
        "open_followups": open_followups,
        "by_severity": _breakdown(
            incidents,
            lambda i: i.severity_level.label if i.severity_level else "—",
            colorfn=lambda i: i.severity_level.color if i.severity_level else None,
        ),
        "by_type": _breakdown(
            incidents, lambda i: i.incident_type.label if i.incident_type else "—"
        ),
        "by_status": _breakdown(
            incidents,
            lambda i: i.status.label if i.status else "—",
            colorfn=lambda i: _STATUS_COLOR.get(i.status.category.value) if i.status else None,
        ),
        "by_system": _breakdown(incidents, lambda i: i.system.name if i.system else "—"),
        "by_team": _breakdown(
            incidents,
            lambda i: i.system.owner_team.name if (i.system and i.system.owner_team) else "—",
        ),
    }
