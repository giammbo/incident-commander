from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import get_db
from app.models import Role, User
from app.services import automation
from app.services import automation_editor as editor
from app.templating import templates

router = APIRouter()


@router.get("/automations/builder", response_class=HTMLResponse)
def builder(
    request: Request,
    trigger: str = "",
    user: User = Depends(require_role(Role.admin)),
):
    """Condition rows + action picker allowed for the chosen trigger (HTMX swap)."""
    return templates.TemplateResponse(
        request, "partials/automation_builder.html", editor.editor_context(trigger)
    )


@router.get("/automations/condition-value", response_class=HTMLResponse)
def condition_value(
    request: Request,
    i: int = 0,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    """Operator + value widget for condition row ``i`` once its field/op is picked."""
    q = request.query_params
    field = q.get(f"cond-{i}-field", "")
    op = q.get(f"cond-{i}-op", "equals")
    return templates.TemplateResponse(
        request,
        "partials/automation_condition_value.html",
        {
            "i": i,
            "field": field,
            "op": op if op in editor.OP_LABELS else "equals",
            "op_labels": editor.OP_LABELS,
            "options": editor.field_options(db, field) if field else None,
            "selected": [v for v in q.getlist(f"cond-{i}-value") if v],
        },
    )


@router.get("/automations/action-params", response_class=HTMLResponse)
def action_params(
    request: Request,
    type: str = "",
    action_type: str = "",  # what the editor's <select name="action_type"> actually sends
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.models import IncidentRoleType, SeverityLevel, StatusLevel
    from app.models import User as UserModel
    from app.services.incident_types import list_incident_types

    ctx = {
        "request": request,
        "action_type": type or action_type,
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
    rules = automation.list_rules(db)
    lookups = editor.load_lookups(db)
    return templates.TemplateResponse(
        request,
        "automations.html",
        {
            "current_user": user,
            "rules": rules,
            "summaries": {r.id: editor.summarize_rule(r, lookups) for r in rules},
            "trigger_labels": editor.TRIGGER_LABELS,
            **editor.editor_context(""),
        },
    )


@router.post("/automations")
async def create(
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
    # The editor posts indexed rows (cond-<i>-field|op|value); the older parallel
    # cond_field/cond_op/cond_value lists are still accepted for API callers.
    conditions = editor.conditions_from_form(await request.form())
    for f, op, v in zip(cond_field, cond_op, cond_value):
        if not (v or "").strip():
            continue  # skip blank rows — also guards int("") on a blank _INT_FIELDS value
        cond = editor.build_condition(f, op, v.split(",") if op == "in" else [v])
        if cond is not None:
            conditions.append(cond)
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
