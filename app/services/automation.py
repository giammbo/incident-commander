from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SlackConnection, WorkflowRule

INCIDENT_TRIGGERS = ("incident.opened", "incident.status_changed")
ALERT_TRIGGER = "alert.received"
TRIGGERS = (*INCIDENT_TRIGGERS, ALERT_TRIGGER)

# Editor metadata: which condition-fields and action-types each trigger allows.
TRIGGER_FIELDS = {
    "incident.opened": ["severity", "type", "is_private", "system"],
    "incident.status_changed": ["severity", "type", "is_private", "system"],
    "alert.received": ["source", "severity_raw", "status"],
}
TRIGGER_ACTIONS = {
    "incident.opened": [
        "open_slack_channel",
        "assign_role",
        "set_status",
        "post_update",
        "create_followup",
    ],
    "incident.status_changed": [
        "open_slack_channel",
        "assign_role",
        "set_status",
        "post_update",
        "create_followup",
    ],
    "alert.received": ["create_incident"],
}
_INT_FIELDS = {"severity", "type", "system"}  # condition values coerced to int


def create_rule(db: Session, *, name, trigger, conditions, actions, rank=100) -> WorkflowRule:
    if trigger not in TRIGGERS:
        raise ValueError("Unknown trigger")
    # Reject action/condition fields that don't belong to this trigger, so the
    # rule can't be silently dead (e.g. create_incident on an incident trigger,
    # or set_status on an alert trigger).
    allowed_actions = TRIGGER_ACTIONS.get(trigger, [])
    for a in actions or []:
        if a.get("type") not in allowed_actions:
            raise ValueError(f"Action '{a.get('type')}' is not valid for trigger '{trigger}'")
    allowed_fields = TRIGGER_FIELDS.get(trigger, [])
    for c in conditions or []:
        if c.get("field") not in allowed_fields:
            raise ValueError(
                f"Condition field '{c.get('field')}' is not valid for trigger '{trigger}'"
            )
    rule = WorkflowRule(
        name=(name or "").strip() or "Untitled rule",
        trigger=trigger,
        conditions=conditions,
        actions=actions,
        rank=rank,
    )
    db.add(rule)
    db.flush()
    return rule


def list_rules(db: Session):
    return list(
        db.scalars(
            select(WorkflowRule).order_by(WorkflowRule.trigger, WorkflowRule.rank, WorkflowRule.id)
        )
    )


def delete_rule(db: Session, rule_id: int) -> None:
    rule = db.get(WorkflowRule, rule_id)
    if rule is not None:
        db.delete(rule)


def toggle_rule(db: Session, rule_id: int) -> None:
    rule = db.get(WorkflowRule, rule_id)
    if rule is not None:
        rule.enabled = not rule.enabled


_MISSING = object()

_FIELD_ATTR = {
    "severity": "severity_level_id",
    "type": "incident_type_id",
    "is_private": "is_private",
    "system": "system_id",
    "source": "source",
    "severity_raw": "severity_raw",
    "status": "status",
}


def _subject_value(subject, field):
    attr = _FIELD_ATTR.get(field)
    if attr is None:
        return _MISSING
    return getattr(subject, attr, _MISSING)


def _matches(conditions, subject) -> bool:
    for c in conditions or []:
        actual = _subject_value(subject, c.get("field"))
        if actual is _MISSING:
            return False
        expected = c.get("value")
        if expected is None:
            # a None/missing comparison value is a misconfigured condition, not a
            # "match anything whose field is unset" wildcard — never match on it.
            return False
        if c.get("op") == "in":
            if actual not in (expected or []):
                return False
        elif actual != expected:
            return False
    return True


def run_rules(db: Session, *, trigger, incident=None, alert=None, by_user=None) -> None:
    subject = incident if incident is not None else alert
    rules = list(
        db.scalars(
            select(WorkflowRule)
            .where(WorkflowRule.trigger == trigger, WorkflowRule.enabled.is_(True))
            .order_by(WorkflowRule.rank, WorkflowRule.id)
        )
    )
    for rule in rules:
        if subject is None or not _matches(rule.conditions, subject):
            continue
        for action in rule.actions or []:
            try:
                _execute_action(db, action, incident=incident, alert=alert, by_user=by_user)
            except Exception:  # noqa: BLE001 — partial-failure-safe; one bad action must not 500
                _log(
                    db,
                    incident,
                    f"Automation '{rule.name}': action '{action.get('type')}' failed",
                    by_user,
                )
        _log(db, incident, f"Automation '{rule.name}' ran", by_user)
    db.flush()


def _log(db, incident, body, by_user):
    if incident is None:
        return
    from app.services.timeline import log_event

    log_event(db, incident, entry_type="automation", body=body, created_by=by_user)


def _execute_action(db, action, *, incident, alert, by_user) -> None:
    t = action.get("type")
    p = action.get("params") or {}
    if t == "open_slack_channel":
        if incident is not None and not incident.slack_channel_id:
            conn = db.scalar(select(SlackConnection).order_by(SlackConnection.id))
            if conn is not None:
                from app.services import providers

                incident.slack_connection_id = conn.id
                providers.CHAT_PROVIDERS["slack"].open_room(db, incident, connection=conn)
    elif t == "assign_role":
        from app.services.incidents import set_incident_role_assignees

        set_incident_role_assignees(
            db,
            incident,
            role_type_id=int(p["role_type_id"]),
            user_ids=[int(u) for u in p.get("user_ids", [])],
            by_user=by_user,
        )
    elif t == "set_status":
        from app.services.incidents import set_incident_status

        set_incident_status(db, incident, status_id=int(p["status_id"]), by_user=by_user)
    elif t == "post_update":
        from app.services.updates import post_update

        post_update(db, incident, message=p.get("message", ""), by_user=by_user)
    elif t == "create_followup":
        from app.services.followups import create_followup

        create_followup(
            db,
            incident,
            title=p.get("title", ""),
            assignee_id=(int(p["assignee_id"]) if p.get("assignee_id") else None),
            created_by=by_user,
        )
    elif t == "create_incident":
        _action_create_incident(db, alert, p, by_user)


def _action_create_incident(db, alert, params, by_user) -> None:
    from app.services.alerts import attach_alert_to_incident
    from app.services.catalog import default_severity_level_id
    from app.services.incidents import create_incident

    if alert is None or alert.incident_id is not None:
        return
    links = "\n".join(
        f"- [{lk.get('label', 'link')}]({lk.get('url')})" for lk in (alert.links or [])
    )
    desc = (alert.description or "") + (f"\n\n{links}" if links else "")
    sev_id = params.get("severity_level_id") or default_severity_level_id(db)
    inc = create_incident(
        db,
        title=alert.title,
        severity_level_id=sev_id,
        is_private=bool(params.get("is_private", False)),
        created_by=None,
        description=desc or None,
        incident_type_id=params.get("incident_type_id"),
    )
    db.flush()
    attach_alert_to_incident(db, alert, inc, by_user=None)
    # one bounded chaining edge: run incident.opened rules on the new incident
    run_rules(db, trigger="incident.opened", incident=inc, by_user=None)
