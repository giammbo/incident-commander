from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role, require_user
from app.db import get_db
from app.models import Incident, Role, User
from app.services import postmortems as pm_svc
from app.templating import templates

router = APIRouter()


def _incident(db, incident_id):
    return db.scalar(select(Incident).where(Incident.id == incident_id))


@router.post("/incidents/{incident_id}/postmortem/generate")
def generate(
    request: Request,
    incident_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = _incident(db, incident_id)
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    pm_svc.generate(db, inc, by_user=user.id)
    db.commit()
    return RedirectResponse(f"/incidents/{incident_id}/postmortem", status_code=303)


@router.get("/incidents/{incident_id}/postmortem", response_class=HTMLResponse)
def view(
    request: Request,
    incident_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    inc = _incident(db, incident_id)
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "postmortem.html",
        {"current_user": user, "i": inc, "postmortem": pm_svc.get_postmortem(db, inc)},
    )


@router.post("/incidents/{incident_id}/postmortem")
def save(
    request: Request,
    incident_id: int,
    body: str = Form(...),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = _incident(db, incident_id)
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    pm = pm_svc.get_postmortem(db, inc)
    if pm is None:
        request.session["flash"] = "Generate the postmortem first."
        return RedirectResponse(f"/incidents/{incident_id}/postmortem", status_code=303)
    try:
        pm_svc.update_body(db, pm, body=body, by_user=user.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse(f"/incidents/{incident_id}/postmortem", status_code=303)


@router.post("/incidents/{incident_id}/postmortem/regenerate")
def regenerate(
    request: Request,
    incident_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = _incident(db, incident_id)
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    pm = pm_svc.get_postmortem(db, inc)
    if pm is not None:
        pm_svc.regenerate(db, pm, inc, by_user=user.id)
        db.commit()
    return RedirectResponse(f"/incidents/{incident_id}/postmortem", status_code=303)


@router.get("/incidents/{incident_id}/postmortem.md", response_class=PlainTextResponse)
def download(
    request: Request,
    incident_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    inc = _incident(db, incident_id)
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    pm = pm_svc.get_postmortem(db, inc)
    if pm is None:
        return HTMLResponse("Not found", status_code=404)
    return PlainTextResponse(
        pm.body,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="incident-{incident_id}-postmortem.md"'
        },
    )
