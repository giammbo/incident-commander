from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Postmortem


def _humanize_duration(delta) -> str:
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


def render_template(incident, *, events, follow_ups, roles_by_type, role_types) -> str:
    sev = incident.severity_level.label if incident.severity_level else "—"
    typ = incident.incident_type.label if incident.incident_type else "—"
    status = incident.status.label if incident.status else "—"
    opened = incident.created_at.strftime("%Y-%m-%d %H:%M UTC") if incident.created_at else "—"
    if incident.closed_at and incident.created_at:
        closed = incident.closed_at.strftime("%Y-%m-%d %H:%M UTC")
        duration = _humanize_duration(incident.closed_at - incident.created_at)
    else:
        closed = duration = "—"
    system = "—"
    if incident.system:
        system = incident.system.name
        if incident.system.owner_team:
            system += f" (owned by {incident.system.owner_team.name})"

    lines = [f"# Postmortem: {incident.title}", ""]
    lines += [
        f"- **Severity:** {sev}",
        f"- **Type:** {typ}",
        f"- **Status:** {status}",
        f"- **Opened:** {opened}",
        f"- **Closed:** {closed}",
        f"- **Duration:** {duration}",
        f"- **System:** {system}",
    ]
    for rt in role_types:
        names = [u.name for u in roles_by_type.get(rt.id, [])]
        lines.append(f"- **{rt.label}:** {', '.join(names) or 'unassigned'}")

    lines += ["", "## Summary", "_What happened, in a sentence or two._", ""]
    lines += ["## Impact", "_Who/what was affected, and how badly?_", ""]
    lines += ["## Timeline", ""]
    ordered = sorted(events, key=lambda e: (e.created_at, e.id))
    for e in ordered:
        ts = e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else ""
        lines.append(f"- {ts} — {e.body}")
    if not ordered:
        lines.append("_No timeline events recorded._")
    lines += ["", "## Root cause", "_Why did it happen?_", ""]
    lines += ["## Resolution", "_How was it fixed or mitigated?_", ""]
    lines += ["## Follow-ups", ""]
    for f in follow_ups:
        box = "x" if f.status == "completed" else " "  # cancelled is not "done"
        assignee = f.assignee.name if f.assignee else "unassigned"
        lines.append(f"- [{box}] {f.title} — {assignee} ({f.status})")
    if not follow_ups:
        lines.append("_No follow-ups._")
    lines += ["", "## Lessons learned", "_What will we change so this doesn't recur?_", ""]
    return "\n".join(lines)


def _build(db: Session, incident) -> str:
    from app.services import followups, roles

    return render_template(
        incident,
        events=list(incident.events),
        follow_ups=followups.list_followups(incident),
        roles_by_type=roles.assignments_by_role(incident),
        role_types=roles.list_incident_role_types(db),
    )


def get_postmortem(db: Session, incident) -> Postmortem | None:
    return db.scalar(select(Postmortem).where(Postmortem.incident_id == incident.id))


def generate(db: Session, incident, *, by_user) -> Postmortem:
    existing = get_postmortem(db, incident)
    if existing is not None:
        return existing
    pm = Postmortem(incident_id=incident.id, body=_build(db, incident), updated_by=by_user)
    db.add(pm)
    db.flush()
    return pm


def regenerate(db: Session, postmortem, incident, *, by_user) -> None:
    postmortem.body = _build(db, incident)
    postmortem.updated_at = datetime.now(UTC)
    postmortem.updated_by = by_user
    db.flush()


def update_body(db: Session, postmortem, *, body, by_user) -> None:
    body = (body or "").strip()
    if not body:
        raise ValueError("Postmortem cannot be empty")
    postmortem.body = body
    postmortem.updated_at = datetime.now(UTC)
    postmortem.updated_by = by_user
    db.flush()
