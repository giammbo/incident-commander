from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth import require_role, require_user
from app.config import get_settings
from app.db import get_db
from app.models import Alert, Escalation, Incident, Role, SeverityLevel, User
from app.services import alerts as alerts_service
from app.services import automation, webhooks
from app.services.catalog import default_severity_level_id
from app.services.core_forms import custom_values, validation_response
from app.services.custom_fields import fields_for_type
from app.services.incident_access import get_visible_incident, visible_incident_condition
from app.services.incident_types import default_incident_type_id
from app.services.incidents import create_incident
from app.templating import templates

router = APIRouter()


@router.get("/alerts", response_class=HTMLResponse)
def inbox(
    request: Request,
    status: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    status = status if status in ("firing", "resolved") else None
    alerts = alerts_service.list_alerts(
        db, status=status if status in ("firing", "resolved") else "firing", user=user
    )
    if status is None:
        alerts = [a for a in alerts if a.incident is None or not a.incident.is_closed]
    paging = {}
    ids = [a.id for a in alerts]
    if ids:
        for e in db.scalars(
            select(Escalation)
            .where(
                Escalation.alert_id.in_(ids),
                or_(
                    Escalation.incident_id.is_(None),
                    Escalation.incident_id.in_(
                        select(Incident.id).where(visible_incident_condition(user))
                    ),
                ),
            )
            .order_by(Escalation.id)
        ):
            paging[e.alert_id] = e  # latest wins
    firing = len(alerts_service.list_alerts(db, status="firing", user=user))
    return templates.TemplateResponse(
        request,
        "alerts.html",
        {
            "alerts": alerts,
            "current_user": user,
            "fields": fields_for_type(db, default_incident_type_id(db)),
            "values": {},
            "severity_levels": list(db.scalars(select(SeverityLevel).order_by(SeverityLevel.rank))),
            "paging": paging,
            "filter_status": status,
            "firing_count": firing,
            "open_incidents": list(
                db.scalars(
                    select(Incident).where(
                        Incident.closed_at.is_(None), visible_incident_condition(user)
                    )
                )
            ),
        },
    )


def _get_alert(db: Session, alert_id: int, user: User) -> Alert | None:
    return db.scalar(
        select(Alert).where(Alert.id == alert_id, alerts_service.visible_alert_condition(user))
    )


def declare_from_alert(
    db: Session,
    alert: Alert,
    *,
    user: User,
    escalation: Escalation | None = None,
    severity_level_id: int | None = None,
    field_values: dict | None = None,
) -> Incident:
    """Open an incident from an alert with the same side-effects as POST /incidents
    (automation before commit, webhook after). One commit; the alert row is locked so
    concurrent declares cannot create two incidents. Raises ValueError if already declared."""
    alert = db.scalar(
        select(Alert)
        .where(Alert.id == alert.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if alert is None or alert.incident_id is not None:
        db.rollback()
        raise ValueError("This alert already has an incident.")
    links = "\n".join(
        f"- [{lk.get('label', 'link')}]({lk.get('url')})"
        for lk in (alert.links or [])
        if str(lk.get("url", "")).startswith(("https://", "http://"))
    )
    desc = (alert.description or "") + (f"\n\n{links}" if links else "")
    inc = create_incident(
        db,
        title=alert.title[:300],
        severity_level_id=severity_level_id or default_severity_level_id(db),
        is_private=False,
        description=desc or None,
        created_by=user.id,
    )
    db.flush()
    if field_values is not None:
        from app.services.custom_fields import set_incident_values

        set_incident_values(db, inc, field_values)
    alerts_service.attach_alert_to_incident(db, alert, inc, by_user=user.id)
    if escalation is not None:
        escalation.incident_id = inc.id
    automation.run_rules(db, trigger="incident.opened", incident=inc, by_user=user.id)
    db.commit()
    webhooks.notify(db, inc, "opened", base_url=get_settings().base_url)
    return inc


@router.post("/alerts/{alert_id}/declare")
async def declare(
    request: Request,
    alert_id: int,
    severity_level_id: int | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    alert = _get_alert(db, alert_id, user)
    if alert is None:
        return HTMLResponse("Not found", status_code=404)
    form = await request.form()
    response = inbox(request, form.get("return_filter") or None, user, db)
    if alert not in response.context["alerts"]:
        response.context["alerts"].insert(0, alert)
    draft = {key: form.getlist(key) for key in form}
    if severity_level_id is None or db.get(SeverityLevel, severity_level_id) is None:
        return validation_response(
            request, response, "Choose an incident severity before declaring.", draft
        )
    try:
        from app.services.incident_types import default_incident_type_id

        raw = {
            int(key[3:]): form.getlist(key)
            for key in form
            if key.startswith("cf_") and key[3:].isdigit()
        }
        custom_values(db, default_incident_type_id(db), raw)
        inc = declare_from_alert(
            db, alert, user=user, severity_level_id=severity_level_id, field_values=raw
        )
    except ValueError as exc:
        db.rollback()
        return validation_response(request, response, str(exc), draft)
    return RedirectResponse(f"/incidents/{inc.id}", status_code=303)


@router.post("/alerts/{alert_id}/attach")
async def attach(
    alert_id: int,
    request: Request,
    incident_id: int | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    alert = _get_alert(db, alert_id, user)
    if alert is None:
        return HTMLResponse("Not found", status_code=404)
    form = await request.form()
    response = inbox(request, form.get("return_filter") or None, user, db)
    if alert not in response.context["alerts"]:
        response.context["alerts"].insert(0, alert)
    draft = {key: form.getlist(key) for key in form}
    if incident_id is None:
        return validation_response(
            request, response, "Choose an open incident to attach this alert.", draft
        )
    inc = get_visible_incident(db, incident_id, user)
    if inc.is_closed:
        return validation_response(
            request, response, "Choose an open incident to attach this alert.", draft
        )
    alerts_service.attach_alert_to_incident(db, alert, inc, by_user=user.id)
    db.commit()
    return RedirectResponse(f"/incidents/{inc.id}", status_code=303)


@router.post("/alerts/{alert_id}/resolve")
def resolve(
    alert_id: int,
    return_filter: str = Form(""),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    alert = _get_alert(db, alert_id, user)
    if alert is None:
        return HTMLResponse("Not found", status_code=404)
    alerts_service.resolve_alert(db, alert)
    db.commit()
    return RedirectResponse(
        "/alerts" + ("?status=" + return_filter if return_filter in ("firing", "resolved") else ""),
        status_code=303,
    )
