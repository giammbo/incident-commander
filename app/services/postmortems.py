from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Incident, Postmortem, StatusCategory, StatusLevel

_NOTES_START = "<!-- gemini-notes:start -->"
_NOTES_END = "<!-- gemini-notes:end -->"


def _notes_section(notes: str) -> str:
    # Strip our delimiter markers out of the note text so a note that happens to contain
    # them can't corrupt the section or break the idempotent re-upsert.
    clean = (notes or "").replace(_NOTES_START, "").replace(_NOTES_END, "").strip()
    return f"{_NOTES_START}\n## Meeting notes (Gemini)\n\n{clean}\n{_NOTES_END}"


def _upsert_notes_section(body: str, notes: str) -> str:
    section = _notes_section(notes)
    body = body or ""
    if _NOTES_START in body and _NOTES_END in body:
        pattern = re.escape(_NOTES_START) + r".*?" + re.escape(_NOTES_END)
        return re.sub(pattern, lambda _m: section, body, flags=re.DOTALL)
    return (body + ("\n\n" if body.strip() else "")) + section


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
    if getattr(incident, "gemini_notes", None):
        lines += ["", _notes_section(incident.gemini_notes)]
    return "\n".join(lines)


def list_incidents_with_postmortem(db: Session):
    """Incidents that have a postmortem, newest incident first (archive view)."""
    return list(
        db.scalars(
            select(Incident)
            .join(Postmortem, Postmortem.incident_id == Incident.id)
            .order_by(Incident.created_at.desc())
        )
    )


def list_closed_incidents_without_postmortem(db: Session):
    """Closed incidents that don't have a postmortem yet — the 'to write' list.
    'Closed' mirrors Incident.is_closed (status category == closed)."""
    no_pm = ~select(Postmortem.id).where(Postmortem.incident_id == Incident.id).exists()
    return list(
        db.scalars(
            select(Incident)
            .join(StatusLevel, Incident.status_id == StatusLevel.id)
            .where(StatusLevel.category == StatusCategory.closed, no_pm)
            .order_by(Incident.created_at.desc())
        )
    )


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


def maybe_pull_gemini_notes(db: Session, incident) -> bool:
    """Best-effort, throttled lazy fetch of the Gemini notes doc into the postmortem.
    Returns True only if notes were just fetched. Never raises."""
    if incident.gemini_notes:
        return False
    if not incident.meet_space_name:
        return False
    state = dict(incident.creation_state or {})
    now = datetime.now(UTC)
    last = state.get("gemini_notes_try")
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < 120:
                return False
        except ValueError:
            pass
    state["gemini_notes_try"] = now.isoformat()
    incident.creation_state = state
    db.flush()

    from app.services import google
    from app.settings_store import google_settings

    g = google_settings(db)
    if not (g.service_account_json and g.impersonate_email):
        return False
    try:
        text = google.fetch_gemini_notes_text(
            service_account_json=g.service_account_json,
            impersonate_email=g.impersonate_email,
            space_name=incident.meet_space_name,
        )
    except Exception:  # noqa: BLE001 — best-effort; never break close/view
        return False
    if not text:
        return False
    incident.gemini_notes = text
    postmortem = get_postmortem(db, incident)
    if postmortem is not None:
        postmortem.body = _upsert_notes_section(postmortem.body, text)
    db.flush()
    return True
