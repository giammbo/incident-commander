from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role, require_user
from app.db import get_db
from app.models import FollowUp, Role, User
from app.routers.incidents import detail, incident_redirect
from app.services import followups as svc
from app.services.core_forms import validation_response
from app.services.incident_access import get_visible_incident
from app.templating import templates

router = APIRouter()


def _global_redirect(status):
    return RedirectResponse(
        "/follow-ups?status="
        + (status if status in ("open", "completed", "cancelled", "all") else "open"),
        status_code=303,
    )


@router.get("/follow-ups", response_class=HTMLResponse)
def open_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    items = svc.list_open_followups(
        db, user=user, status=request.query_params.get("status", "open")
    )
    return templates.TemplateResponse(
        request,
        "follow_ups.html",
        {
            "current_user": user,
            "today": date.today(),
            "items": items,
            "filter_status": request.query_params.get("status", "open"),
            "users": list(
                db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.name))
            ),
        },
    )


def _parse_due(value: str | None):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("Enter a valid due date") from None


@router.post("/incidents/{incident_id}/followups")
def create(
    request: Request,
    incident_id: int,
    title: str = Form(""),
    description: str | None = Form(None),
    assignee_id: int | None = Form(None),
    due_on: str | None = Form(None),
    return_tab: str | None = Form(None),
    return_filter: str = Form("open"),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = get_visible_incident(db, incident_id, user)
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
        draft = {
            "title": [title],
            "description": [description or ""],
            "assignee_id": [str(assignee_id or "")],
            "due_on": [due_on or ""],
        }
        return validation_response(
            request, detail(request, incident_id, "followups", user, db), str(exc), draft
        )
    return incident_redirect(incident_id, return_tab)


@router.post("/followups/{followup_id}/status")
def set_status(
    request: Request,
    followup_id: int,
    status: str = Form(...),
    return_tab: str | None = Form(None),
    return_filter: str = Form("open"),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    fu = db.scalar(select(FollowUp).where(FollowUp.id == followup_id))
    if fu is None:
        return HTMLResponse("Not found", status_code=404)
    get_visible_incident(db, fu.incident_id, user)
    iid = fu.incident_id
    try:
        svc.set_followup_status(db, fu, status=status, by_user=user.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return (
        _global_redirect(return_filter)
        if return_tab == "global"
        else incident_redirect(iid, return_tab)
    )


@router.post("/followups/{followup_id}/edit")
def edit(
    request: Request,
    followup_id: int,
    title: str = Form(""),
    description: str | None = Form(None),
    assignee_id: int | None = Form(None),
    due_on: str | None = Form(None),
    return_tab: str | None = Form(None),
    return_filter: str = Form("open"),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    fu = db.scalar(select(FollowUp).where(FollowUp.id == followup_id))
    if fu is None:
        return HTMLResponse("Not found", status_code=404)
    get_visible_incident(db, fu.incident_id, user)
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
        draft = {
            "title": [title],
            "description": [description or ""],
            "assignee_id": [str(assignee_id or "")],
            "due_on": [due_on or ""],
        }
        response = (
            open_page(request, user, db)
            if return_tab == "global"
            else detail(request, iid, "followups", user, db)
        )
        if return_tab == "global":
            response.context["items"] = svc.list_open_followups(db, user=user, status=return_filter)
            response.context["filter_status"] = return_filter
        return validation_response(request, response, str(exc), draft)
    return (
        _global_redirect(return_filter)
        if return_tab == "global"
        else incident_redirect(iid, return_tab)
    )


@router.post("/followups/{followup_id}/delete")
def delete(
    request: Request,
    followup_id: int,
    return_tab: str | None = Form(None),
    return_filter: str = Form("open"),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    fu = db.scalar(select(FollowUp).where(FollowUp.id == followup_id))
    if fu is None:
        return HTMLResponse("Not found", status_code=404)
    get_visible_incident(db, fu.incident_id, user)
    iid = fu.incident_id
    db.delete(
        fu
    )  # fu is already loaded; delete directly (svc.delete_followup re-fetch could raise)
    db.commit()
    return (
        _global_redirect(return_filter)
        if return_tab == "global"
        else incident_redirect(iid, return_tab)
    )
