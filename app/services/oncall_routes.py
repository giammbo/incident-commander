"""Alert routes: match an incoming alert to an escalation path and page.

Conditions are stored as ``{"field", "op", "value"}`` dicts (the automation editor's
format) and are ANDed; an empty list means "any alert".
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Alert,
    AlertRoute,
    Escalation,
    EscalationStatus,
    InboundIntegration,
    Urgency,
)
from app.services import oncall_engine

log = logging.getLogger(__name__)

ROUTE_FIELDS = {
    "integration": "Integration",
    "source": "Alert source",
    "severity_raw": "Alert severity (as sent)",
    "title": "Alert title",
}
OP_LABELS = {"equals": "is", "in": "is one of"}
TITLE_OP_LABELS = {**OP_LABELS, "contains": "contains"}
MAX_CONDITIONS = 3
MAX_HIGH_IF = 2
_OPEN = (EscalationStatus.triggered, EscalationStatus.acknowledged, EscalationStatus.exhausted)


def op_labels(field: str) -> dict[str, str]:
    return TITLE_OP_LABELS if field == "title" else OP_LABELS


# --- matching --------------------------------------------------------------


def _alert_value(alert, field: str):
    if field == "integration":
        return alert.integration_id
    return getattr(alert, field, None)


def _cond_matches(c: dict, alert) -> bool:
    field, op, want = c.get("field"), c.get("op") or "equals", c.get("value")
    have = _alert_value(alert, field)
    if have is None:
        return False
    if op == "contains":
        return str(want or "").lower() in str(have).lower()
    wants = want if op == "in" and isinstance(want, list) else [want]
    if field == "integration":
        return have in [_to_int(w) for w in wants]
    return str(have) in [str(w) for w in wants]


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def route_matches(conditions: list[dict], alert) -> bool:
    return all(_cond_matches(c, alert) for c in conditions or [])


def match_route(db: Session, alert) -> AlertRoute | None:
    rows = db.scalars(
        select(AlertRoute)
        .where(AlertRoute.enabled.is_(True))
        .order_by(AlertRoute.rank, AlertRoute.id)
    )
    return next((r for r in rows if route_matches(r.conditions, alert)), None)


def route_urgency(route: AlertRoute, alert) -> Urgency:
    if not route.high_if:
        return route.urgency
    return Urgency.high if route_matches(route.high_if, alert) else Urgency.low


def page_for_alert(db: Session, alert: Alert, *, now: datetime) -> Escalation | None:
    """Start the escalation of the first matching route, unless one is already open."""
    already = db.scalar(
        select(Escalation.id).where(Escalation.alert_id == alert.id, Escalation.status.in_(_OPEN))
    )
    if already is not None:
        return None
    route = match_route(db, alert)
    if route is None:
        return None
    return oncall_engine.start_escalation(
        db,
        urgency=route_urgency(route, alert),
        title=alert.title,
        path=route.path,
        alert=alert,
        route=route,
        source="alert_route",
        now=now,
    )


def safe_page_for_alert(db: Session, alert: Alert, *, now: datetime) -> Escalation | None:
    """Ingest hook: paging problems never break ingestion, but are never silent either.

    On failure the savepoint is rolled back and ``alert.paging_error`` records a short
    reason (exception type only, no message) that the inbox shows; a later successful
    page clears it.
    """
    try:
        with db.begin_nested():
            esc = page_for_alert(db, alert, now=now)
    except Exception as exc:
        log.exception("paging for alert %s failed", alert.id)
        alert.paging_error = f"Paging failed: {type(exc).__name__}"
        return None
    if esc is not None:
        alert.paging_error = None
    return esc


def safe_resolve_for_alert(db: Session, alert: Alert, *, now: datetime) -> None:
    """Close the alert's open escalations without ever blocking the alert-state change."""
    try:
        with db.begin_nested():
            oncall_engine.resolve_for_alert(db, alert, now=now)
    except Exception as exc:
        log.exception("closing escalation for alert %s failed", alert.id)
        alert.paging_error = f"Could not close the paging escalation: {type(exc).__name__}"


# --- editor ----------------------------------------------------------------


def build_route_condition(field: str, op: str, raw_values: list[str]) -> dict | None:
    """One stored condition from submitted widget values; None for a blank/invalid row."""
    field = (field or "").strip()
    if field not in ROUTE_FIELDS:
        return None
    op = op if op in op_labels(field) else "equals"
    raws = [(v or "").strip() for v in raw_values]
    if op == "contains":
        return {"field": field, "op": op, "value": raws[0]} if raws and raws[0] else None
    if field == "integration":
        ids = [int(v) for v in raws if v.lstrip("-").isdigit()]
        if not ids:
            return None
        return {"field": field, "op": op, "value": ids if op == "in" else ids[0]}
    if op == "in":
        values = [p.strip() for v in raws for p in v.split(",") if p.strip()]
        return {"field": field, "op": op, "value": values} if values else None
    return {"field": field, "op": op, "value": raws[0]} if raws and raws[0] else None


def conditions_from_form(form, prefix: str, count: int) -> list[dict]:
    out = []
    for i in range(count):
        c = build_route_condition(
            form.get(f"{prefix}-{i}-field", ""),
            form.get(f"{prefix}-{i}-op", ""),
            form.getlist(f"{prefix}-{i}-value"),
        )
        if c is not None:
            out.append(c)
        elif form.get(f"{prefix}-{i}-field", ""):
            raise ValueError(f"Complete condition {i + 1} or choose no condition")
    return out


def field_options(db: Session, field: str) -> list[tuple[str, str]] | None:
    if field == "integration":
        rows = db.scalars(select(InboundIntegration).order_by(InboundIntegration.name))
        return [(str(i.id), i.name) for i in rows]
    return None


def condition_rows(db: Session, conditions: list[dict], count: int) -> list[dict]:
    """Template rows for the editor: stored conditions first, padded with blank rows."""
    rows = []
    for i in range(count):
        c = conditions[i] if i < len(conditions) else {}
        field = c.get("field", "")
        value = c.get("value")
        selected = (
            [str(v) for v in value]
            if isinstance(value, list)
            else ([str(value)] if value is not None else [])
        )
        rows.append(
            {
                "i": i,
                "field": field,
                "op": c.get("op", "equals"),
                "op_labels": op_labels(field),
                "options": field_options(db, field) if field else None,
                "selected": selected,
            }
        )
    return rows


def _describe(c: dict, integrations: dict[int, str]) -> tuple[str, str]:
    """(kind, phrase): kind 'from' for origin phrases, 'where' for attribute phrases."""
    field, op, value = c.get("field"), c.get("op") or "equals", c.get("value")
    values = value if isinstance(value, list) else [value]
    if field == "integration":
        names = [integrations.get(_to_int(v), f"#{v} (deleted)") for v in values]
        return "from", " or ".join(names)
    noun = {"source": "source", "severity_raw": "severity", "title": "title"}.get(field, field)
    if op == "contains":
        return "where", f"title contains “{value}”"
    shown = ", ".join(str(v) for v in values)
    if op == "in":
        return "where", f"{noun} is one of {shown}"
    return "where", f"{noun} is {shown}"


def describe_conditions(conditions: list[dict], integrations: dict[int, str]) -> str:
    if not conditions:
        return "All alerts"
    parts = [_describe(c, integrations) for c in conditions]
    froms = [p for k, p in parts if k == "from"]
    wheres = [p for k, p in parts if k == "where"]
    text = "Alerts"
    if froms:
        text += " from " + " and ".join(froms)
    if wheres:
        text += " where " + " and ".join(wheres)
    return text


def describe_route(route: AlertRoute, integrations: dict[int, str]) -> str:
    text = describe_conditions(route.conditions, integrations)
    path = route.path.name if route.path is not None else "(no path)"
    if route.high_if:
        _, hi = zip(*[_describe(c, integrations) for c in route.high_if], strict=True)
        urg = f"high if {' and '.join(hi)}, otherwise low"
    else:
        urg = route.urgency.value
    return f"{text} → {path} ({urg})"


def integration_names(db: Session) -> dict[int, str]:
    return {i.id: i.name for i in db.scalars(select(InboundIntegration))}


def renumber(db: Session) -> list[AlertRoute]:
    """Give every route a distinct, dense rank (1..n) in current order."""
    rows = list(db.scalars(select(AlertRoute).order_by(AlertRoute.rank, AlertRoute.id)))
    for n, r in enumerate(rows, start=1):
        r.rank = n
    db.flush()
    return rows


def move(db: Session, route: AlertRoute, direction: str) -> None:
    rows = renumber(db)
    idx = rows.index(route)
    j = idx - 1 if direction == "up" else idx + 1
    if 0 <= j < len(rows):
        rows[idx].rank, rows[j].rank = rows[j].rank, rows[idx].rank
    db.flush()


def condition_rows_from_form(db: Session, form, prefix: str, count: int) -> list[dict]:
    """Keep even incomplete condition rows in a rejected editor submission."""
    rows = condition_rows(db, [], count)
    for row in rows:
        i = row["i"]
        field = form.get(f"{prefix}-{i}-field", "")
        row.update(
            field=field,
            op=form.get(f"{prefix}-{i}-op", "equals"),
            op_labels=op_labels(field),
            options=field_options(db, field) if field else None,
            selected=form.getlist(f"{prefix}-{i}-value"),
        )
    return rows
