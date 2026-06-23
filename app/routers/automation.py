from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import get_db
from app.models import Role, User
from app.services import automation
from app.templating import templates

router = APIRouter()


@router.get("/automations/action-params", response_class=HTMLResponse)
def action_params(
    request: Request,
    type: str = "",
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.models import IncidentRoleType, SeverityLevel, StatusLevel
    from app.models import User as UserModel
    from app.services.incident_types import list_incident_types

    ctx = {
        "request": request,
        "action_type": type,
        "statuses": list(db.scalars(select(StatusLevel).order_by(StatusLevel.rank))),
        "severities": list(db.scalars(select(SeverityLevel).order_by(SeverityLevel.rank))),
        "incident_types": list_incident_types(db),
        "role_types": list(db.scalars(select(IncidentRoleType).order_by(IncidentRoleType.rank))),
        "users": list(
            db.scalars(
                select(UserModel).where(UserModel.is_active.is_(True)).order_by(UserModel.name)
            )
        ),
    }
    return templates.TemplateResponse(request, "partials/automation_action_params.html", ctx)


@router.get("/automations", response_class=HTMLResponse)
def index(
    request: Request, user: User = Depends(require_role(Role.admin)), db: Session = Depends(get_db)
):
    return templates.TemplateResponse(
        request,
        "automations.html",
        {
            "current_user": user,
            "rules": automation.list_rules(db),
            "trigger_fields": automation.TRIGGER_FIELDS,
            "trigger_actions": automation.TRIGGER_ACTIONS,
        },
    )


@router.post("/automations")
def create(
    request: Request,
    name: str = Form(...),
    trigger: str = Form(...),
    cond_field: list[str] = Form(default=[]),
    cond_op: list[str] = Form(default=[]),
    cond_value: list[str] = Form(default=[]),
    action_type: str = Form(""),
    ap_status: int | None = Form(None),
    ap_role: int | None = Form(None),
    ap_users: list[int] = Form(default=[]),
    ap_message: str = Form(""),
    ap_title: str = Form(""),
    ap_assignee: int | None = Form(None),
    ap_severity: int | None = Form(None),
    ap_type: int | None = Form(None),
    ap_private: bool = Form(False),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    conditions = []
    for f, op, v in zip(cond_field, cond_op, cond_value):
        f = (f or "").strip()
        if not f or not (v or "").strip():
            continue  # skip blank rows — also guards int("") on a blank _INT_FIELDS value
        op = op or "equals"
        if op == "in":
            parts = [p.strip() for p in (v or "").split(",") if p.strip()]
            value: object = (
                [int(p) for p in parts if p.lstrip("-").isdigit()]
                if f in automation._INT_FIELDS
                else parts
            )
        elif f in automation._INT_FIELDS:
            value = int(v) if v.strip().lstrip("-").isdigit() else None
        elif f == "is_private":
            value = (v or "").lower() in ("true", "1", "on", "yes")
        else:
            value = v
        conditions.append({"field": f, "op": op, "value": value})
    # v1: one action per rule (the no-JS editor has a single action block)
    params: dict = {}
    if action_type == "set_status":
        params = {"status_id": ap_status}
    elif action_type == "assign_role":
        params = {"role_type_id": ap_role, "user_ids": ap_users}
    elif action_type == "post_update":
        params = {"message": ap_message}
    elif action_type == "create_followup":
        params = {"title": ap_title, "assignee_id": ap_assignee}
    elif action_type == "create_incident":
        params = {
            "severity_level_id": ap_severity,
            "incident_type_id": ap_type,
            "is_private": ap_private,
        }
    actions = [{"type": action_type, "params": params}] if action_type else []
    try:
        automation.create_rule(
            db, name=name, trigger=trigger, conditions=conditions, actions=actions
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/automations", status_code=303)


@router.post("/automations/{rule_id}/delete")
def delete(
    rule_id: int, user: User = Depends(require_role(Role.admin)), db: Session = Depends(get_db)
):
    automation.delete_rule(db, rule_id)
    db.commit()
    return RedirectResponse("/automations", status_code=303)


@router.post("/automations/{rule_id}/toggle")
def toggle(
    rule_id: int, user: User = Depends(require_role(Role.admin)), db: Session = Depends(get_db)
):
    automation.toggle_rule(db, rule_id)
    db.commit()
    return RedirectResponse("/automations", status_code=303)
