"""Human-facing side of the automation editor: labels, value pickers, rule summaries.

The stored rule format (``WorkflowRule.conditions`` / ``.actions`` JSONB) is unchanged:
conditions are ``{"field", "op": "equals"|"in", "value"}`` with catalog IDs as ints.
This module only translates between that format and what an admin sees and submits.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IncidentRoleType, IncidentType, SeverityLevel, StatusLevel, System, User
from app.services.automation import _INT_FIELDS, TRIGGER_ACTIONS, TRIGGER_FIELDS

TRIGGER_LABELS = {
    "incident.opened": "When an incident is opened",
    "incident.status_changed": "When an incident's status changes",
    "alert.received": "When an alert is received",
}
# Editor label, and the lowercase noun used in the one-line rule summary.
FIELD_LABELS = {
    "severity": ("Severity", "severity"),
    "type": ("Incident type", "type"),
    "is_private": ("Private", "private"),
    "system": ("System", "system"),
    "source": ("Alert source", "source"),
    "severity_raw": ("Alert severity (as sent)", "severity"),
    "status": ("Alert status", "status"),
}
ACTION_LABELS = {
    "open_slack_channel": "Open a Slack channel",
    "assign_role": "Assign a role",
    "set_status": "Set the status",
    "post_update": "Post an update",
    "create_followup": "Create a follow-up",
    "create_incident": "Create an incident",
}
OP_LABELS = {"equals": "is", "in": "is one of"}
ALERT_STATUSES = [("firing", "Firing"), ("resolved", "Resolved")]
TEXT_FIELDS = {"source", "severity_raw"}
MAX_CONDITIONS = 3


def trigger_label(trigger: str) -> str:
    return TRIGGER_LABELS.get(trigger, trigger)


def field_options(db: Session, field: str) -> list[tuple[str, str]] | None:
    """(value, label) choices for a select-backed field; None for text/checkbox fields."""
    if field == "severity":
        rows = db.scalars(select(SeverityLevel).order_by(SeverityLevel.rank))
        return [(str(s.id), s.label) for s in rows]
    if field == "type":
        rows = db.scalars(select(IncidentType).order_by(IncidentType.rank, IncidentType.label))
        return [(str(t.id), t.label) for t in rows]
    if field == "system":
        return [(str(s.id), s.name) for s in db.scalars(select(System).order_by(System.name))]
    if field == "status":
        return list(ALERT_STATUSES)
    return None


def load_lookups(db: Session) -> dict[str, dict]:
    """id → label maps for every catalog a rule can reference (one query per table)."""
    return {
        "severity": {s.id: s.label for s in db.scalars(select(SeverityLevel))},
        "type": {t.id: t.label for t in db.scalars(select(IncidentType))},
        "system": {s.id: s.name for s in db.scalars(select(System))},
        "status": dict(ALERT_STATUSES),
        "status_level": {s.id: s.label for s in db.scalars(select(StatusLevel))},
        "role_type": {r.id: r.label for r in db.scalars(select(IncidentRoleType))},
        "user": {u.id: u.name for u in db.scalars(select(User))},
    }


def _name(lookup: dict, value) -> str:
    """Resolve an ID to its label, tolerating string IDs and deleted rows in old rules."""
    key = value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        key = int(value)
    if key in lookup:
        return lookup[key]
    return f"#{value} (deleted)" if isinstance(key, int) else str(value)


def _describe_condition(c: dict, lookups: dict) -> str:
    field, op, value = c.get("field"), c.get("op") or "equals", c.get("value")
    if field == "is_private":
        return "the incident is private" if value else "the incident is not private"
    noun = FIELD_LABELS.get(field, (field, field))[1]
    lookup = lookups.get(field) if field in (*_INT_FIELDS, "status") else None
    values = value if isinstance(value, list) else [value]
    shown = [_name(lookup, v) if lookup is not None else str(v) for v in values]
    if op == "in":
        return f"{noun} is one of {', '.join(shown) or '(nothing)'}"
    return f"{noun} is {shown[0]}"


def _describe_action(a: dict, lookups: dict) -> str:
    t, p = a.get("type"), a.get("params") or {}
    if t == "open_slack_channel":
        return "open a Slack channel"
    if t == "assign_role":
        role = _name(lookups["role_type"], p.get("role_type_id"))
        users = [_name(lookups["user"], u) for u in p.get("user_ids") or []]
        return f"assign {role} to {', '.join(users)}" if users else f"assign {role}"
    if t == "set_status":
        return f"set status to {_name(lookups['status_level'], p.get('status_id'))}"
    if t == "post_update":
        msg = (p.get("message") or "").strip()
        return f'post update "{msg[:60]}{"…" if len(msg) > 60 else ""}"' if msg else "post update"
    if t == "create_followup":
        text = f'create follow-up "{p.get("title") or ""}"'
        if p.get("assignee_id"):
            text += f" for {_name(lookups['user'], p['assignee_id'])}"
        return text
    if t == "create_incident":
        bits = [
            _name(lookups["severity"], p["severity_level_id"])
            if p.get("severity_level_id")
            else "default severity"
        ]
        if p.get("incident_type_id"):
            bits.append(_name(lookups["type"], p["incident_type_id"]))
        if p.get("is_private"):
            bits.append("private")
        return f"create incident ({', '.join(bits)})"
    return str(t)


def summarize_rule(rule, lookups: dict) -> str:
    """One readable line, e.g. "When an alert is received and severity is critical →
    create incident (SEV1)"."""
    text = trigger_label(rule.trigger)
    conds = [_describe_condition(c, lookups) for c in rule.conditions or []]
    if conds:
        text += " and " + " and ".join(conds)
    actions = [_describe_action(a, lookups) for a in rule.actions or []]
    return f"{text} → {'; '.join(actions) if actions else 'do nothing'}"


def _coerce(field: str, raw: str):
    raw = (raw or "").strip()
    if field in _INT_FIELDS:
        return int(raw) if raw.lstrip("-").isdigit() else None
    return raw or None


def build_condition(field: str, op: str, raw_values: list[str]) -> dict | None:
    """One stored condition from the submitted widget values; None for a blank row.

    ``raw_values`` is the multi-select selection for "is one of", a single-element
    list otherwise; a text field in "is one of" mode takes a comma-separated list.
    """
    field = (field or "").strip()
    if not field:
        return None
    if field == "is_private":
        # a checkbox: absent means "not private" — the row itself was chosen explicitly
        on = any((v or "").lower() in ("true", "1", "on", "yes") for v in raw_values)
        return {"field": field, "op": "equals", "value": on}
    op = op if op in OP_LABELS else "equals"
    if op == "in":
        if field in TEXT_FIELDS:
            raw_values = [p for v in raw_values for p in (v or "").split(",")]
        values = [x for x in (_coerce(field, v) for v in raw_values) if x is not None]
        return {"field": field, "op": op, "value": values} if values else None
    value = _coerce(field, raw_values[0] if raw_values else "")
    return {"field": field, "op": op, "value": value} if value is not None else None


def conditions_from_form(form) -> list[dict]:
    """Parse the editor's indexed rows (``cond-<i>-field|op|value``)."""
    out = []
    for i in range(MAX_CONDITIONS):
        cond = build_condition(
            form.get(f"cond-{i}-field", ""),
            form.get(f"cond-{i}-op", ""),
            form.getlist(f"cond-{i}-value"),
        )
        if cond is not None:
            out.append(cond)
    return out


def editor_context(trigger: str) -> dict:
    """What the builder partial needs for a trigger: allowed fields and actions."""
    trigger = trigger if trigger in TRIGGER_FIELDS else next(iter(TRIGGER_FIELDS))
    return {
        "trigger": trigger,
        "fields": [(f, FIELD_LABELS[f][0]) for f in TRIGGER_FIELDS[trigger]],
        "actions": [(a, ACTION_LABELS[a]) for a in TRIGGER_ACTIONS[trigger]],
        "cond_indexes": range(MAX_CONDITIONS),
    }
