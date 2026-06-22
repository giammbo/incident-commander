from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role, require_user
from app.db import get_db
from app.models import FollowUp, Incident, Role, User
from app.services import followups as svc
from app.templating import templates

router = APIRouter()


@router.get("/follow-ups", response_class=HTMLResponse)
def open_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    items = svc.list_open_followups(db)
    return templates.TemplateResponse(
        request, "follow_ups.html", {"current_user": user, "items": items}
    )


def _parse_due(value: str | None):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@router.post("/incidents/{incident_id}/followups")
def create(
    request: Request,
    incident_id: int,
    title: str = Form(...),
    description: str | None = Form(None),
    assignee_id: int | None = Form(None),
    due_on: str | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = db.scalar(select(Incident).where(Incident.id == incident_id))
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    try:
        svc.create_followup(
            db,
            inc,
            title=title,
            description=description,
            assignee_id=assignee_id,
            due_on=_parse_due(due_on),
            created_by=user.id,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@router.post("/followups/{followup_id}/status")
def set_status(
    request: Request,
    followup_id: int,
    status: str = Form(...),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    fu = db.scalar(select(FollowUp).where(FollowUp.id == followup_id))
    if fu is None:
        return HTMLResponse("Not found", status_code=404)
    iid = fu.incident_id
    try:
        svc.set_followup_status(db, fu, status=status, by_user=user.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse(f"/incidents/{iid}", status_code=303)


@router.post("/followups/{followup_id}/edit")
def edit(
    request: Request,
    followup_id: int,
    title: str = Form(...),
    description: str | None = Form(None),
    assignee_id: int | None = Form(None),
    due_on: str | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    fu = db.scalar(select(FollowUp).where(FollowUp.id == followup_id))
    if fu is None:
        return HTMLResponse("Not found", status_code=404)
    iid = fu.incident_id
    try:
        svc.update_followup(
            db,
            fu,
            title=title,
            description=description,
            assignee_id=assignee_id,
            due_on=_parse_due(due_on),
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse(f"/incidents/{iid}", status_code=303)


@router.post("/followups/{followup_id}/delete")
def delete(
    request: Request,
    followup_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    fu = db.scalar(select(FollowUp).where(FollowUp.id == followup_id))
    if fu is None:
        return HTMLResponse("Not found", status_code=404)
    iid = fu.incident_id
    db.delete(
        fu
    )  # fu is already loaded; delete directly (svc.delete_followup re-fetch could raise)
    db.commit()
    return RedirectResponse(f"/incidents/{iid}", status_code=303)
