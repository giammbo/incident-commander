from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role, require_user
from app.db import get_db
from app.models import Alert, Incident, Role, User
from app.services import alerts as alerts_service
from app.services.catalog import default_severity_level_id
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
    alerts = alerts_service.list_alerts(db, status=status)
    firing = len(alerts_service.list_alerts(db, status="firing"))
    return templates.TemplateResponse(
        request,
        "alerts.html",
        {
            "current_user": user,
            "alerts": alerts,
            "filter_status": status,
            "firing_count": firing,
            "open_incidents": list(
                db.scalars(select(Incident).where(Incident.closed_at.is_(None)))
            ),
        },
    )


def _get_alert(db: Session, alert_id: int) -> Alert | None:
    return db.scalar(select(Alert).where(Alert.id == alert_id))


@router.post("/alerts/{alert_id}/declare")
def declare(
    alert_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    alert = _get_alert(db, alert_id)
    if alert is None:
        return HTMLResponse("Not found", status_code=404)
    links = "\n".join(
        f"- [{lk.get('label', 'link')}]({lk.get('url')})" for lk in (alert.links or [])
    )
    desc = (alert.description or "") + (f"\n\n{links}" if links else "")
    inc = create_incident(
        db,
        title=alert.title,
        severity_level_id=default_severity_level_id(db),
        is_private=False,
        description=desc or None,
        created_by=user.id,
    )
    db.flush()
    alerts_service.attach_alert_to_incident(db, alert, inc, by_user=user.id)
    db.commit()
    return RedirectResponse(f"/incidents/{inc.id}", status_code=303)


@router.post("/alerts/{alert_id}/attach")
def attach(
    alert_id: int,
    request: Request,
    incident_id: int = Form(...),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    alert = _get_alert(db, alert_id)
    inc = db.scalar(select(Incident).where(Incident.id == incident_id))
    if alert is None or inc is None:
        return HTMLResponse("Not found", status_code=404)
    alerts_service.attach_alert_to_incident(db, alert, inc, by_user=user.id)
    db.commit()
    return RedirectResponse(f"/incidents/{inc.id}", status_code=303)


@router.post("/alerts/{alert_id}/resolve")
def resolve(
    alert_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    alert = _get_alert(db, alert_id)
    if alert is None:
        return HTMLResponse("Not found", status_code=404)
    alerts_service.resolve_alert(db, alert)
    db.commit()
    return RedirectResponse("/alerts", status_code=303)
