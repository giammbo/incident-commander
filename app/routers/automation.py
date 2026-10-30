from __future__ import annotations

from fastapi import APIRouter, Depends, Request
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
    return templates.TemplateResponse(
        request, "partials/automation_action_params.html", action_context(db, type or action_type)
    )


def action_context(db, action_type, params=None):
    from app.models import IncidentRoleType, SeverityLevel, StatusLevel
    from app.services.incident_types import list_incident_types

    return {
        "action_type": action_type,
        "params": params or {},
        "statuses": list(db.scalars(select(StatusLevel).order_by(StatusLevel.rank))),
        "severities": list(db.scalars(select(SeverityLevel).order_by(SeverityLevel.rank))),
        "incident_types": list_incident_types(db),
        "role_types": list(db.scalars(select(IncidentRoleType).order_by(IncidentRoleType.rank))),
        "users": list(db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.name))),
    }


def form_context(db, trigger, conditions=None, actions=None, *, action_index=0):
    conditions, actions = conditions or [], actions or []
    ctx = editor.editor_context(trigger)
    ctx["cond_indexes"] = range(max(editor.MAX_CONDITIONS, len(conditions)))
    ctx["condition_rows"] = conditions
    ctx["field_options"] = lambda field: editor.field_options(db, field)
    ctx["op_labels"] = editor.OP_LABELS
    action = actions[action_index] if actions else {}
    ctx.update(action_context(db, action.get("type", ""), action.get("params")))
    return ctx


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
            "sample_systems": list(lookups["system"].items()),
            **form_context(db, ""),
        },
    )


def parse_rule_form(form, *, strict=True):
    from app.services.settings_catalog import checked, integer

    conditions = editor.conditions_from_form(form, strict=strict)
    for f, op, v in zip(
        form.getlist("cond_field"), form.getlist("cond_op"), form.getlist("cond_value")
    ):
        if not v.strip():
            continue
        cond = editor.parse_condition(f, op, v.split(",") if op == "in" else [v], strict=strict)
        if cond is not None:
            conditions.append(cond)
    action_type = str(form.get("action_type", ""))
    params = {}

    def reference(key):
        try:
            return integer(form.get(key), "Action reference", optional=True)
        except ValueError:
            if strict:
                raise
            return form.get(key)

    if action_type == "set_status":
        params = {"status_id": reference("ap_status")}
    elif action_type == "assign_role":
        params = {
            "role_type_id": reference("ap_role"),
            "user_ids": [
                integer(v, "Person") if v.isdigit() else v for v in form.getlist("ap_users")
            ],
        }
    elif action_type == "post_update":
        params = {"message": str(form.get("ap_message", "")).strip()}
    elif action_type == "create_followup":
        params = {
            "title": str(form.get("ap_title", "")).strip(),
            "assignee_id": reference("ap_assignee"),
        }
    elif action_type == "create_incident":
        params = {
            "severity_level_id": reference("ap_severity"),
            "incident_type_id": reference("ap_type"),
            "is_private": checked(form.get("ap_private", "")),
        }
    return {
        "name": str(form.get("name", "")),
        "trigger": str(form.get("trigger", "")),
        "conditions": conditions,
        "actions": [{"type": action_type, "params": params}] if action_type else [],
    }


@router.get("/automations/{rule_id}/edit", response_class=HTMLResponse)
def edit_page(
    rule_id: int,
    request: Request,
    action_index: int = 0,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.models import WorkflowRule

    rule = db.get(WorkflowRule, rule_id)
    if rule is None:
        return HTMLResponse("Rule not found", status_code=404)
    if action_index < 0 or action_index >= max(1, len(rule.actions or [])):
        return HTMLResponse("Action not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "automation_edit.html",
        {
            "current_user": user,
            "rule": rule,
            "trigger_labels": editor.TRIGGER_LABELS,
            "action_index": action_index,
            **form_context(
                db, rule.trigger, rule.conditions, rule.actions, action_index=action_index
            ),
        },
    )


@router.post("/automations")
@router.post("/automations/{rule_id}/edit")
async def save(
    request: Request,
    rule_id: int | None = None,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from app.models import WorkflowRule
    from app.services.settings_catalog import integer

    form = await request.form()
    rule = db.get(WorkflowRule, rule_id) if rule_id is not None else None
    if rule_id is not None and rule is None:
        return HTMLResponse("Rule not found", status_code=404)
    try:
        values = parse_rule_form(form)
        if rule is None:
            automation.create_rule(db, **values)
        else:
            action_index = integer(form.get("action_index", "0"), "Action")
            if action_index >= max(1, len(rule.actions or [])):
                raise ValueError("Action not found.")
            # Legacy multi-action rules keep every other action when one is edited.
            if rule.actions and len(rule.actions) > 1 and values["actions"]:
                actions = list(rule.actions)
                actions[action_index] = values["actions"][0]
                values["actions"] = actions
            values["name"] = automation.validate_rule(db, **values)
            for key, value in values.items():
                setattr(rule, key, value)
        db.commit()
        request.session["flash"] = "Automation saved. No actions were run."
        return RedirectResponse("/automations", status_code=303)
    except ValueError as exc:
        db.rollback()
        # Automation fields contain no credentials; action bodies are kept in-page,
        # not in cookies or browser storage. HTML failures preserve the full editor.
        try:
            values = parse_rule_form(form, strict=False)
        except ValueError:
            values = {"trigger": form.get("trigger", ""), "conditions": [], "actions": []}
        ctx = form_context(db, values["trigger"], values["conditions"], values["actions"])
        ctx["condition_rows"] = editor.draft_condition_rows(form)
        ctx["cond_indexes"] = sorted(
            set(range(editor.MAX_CONDITIONS)) | ctx["condition_rows"].keys()
        )
        return templates.TemplateResponse(
            request,
            "automation_edit.html",
            {
                "current_user": user,
                "rule": rule,
                "name": form.get("name", ""),
                "trigger_labels": editor.TRIGGER_LABELS,
                "action_index": form.get("action_index", "0"),
                "error": str(exc),
                "raw_draft": {k: form.getlist(k) for k in form if not k.startswith("cond-")},
                **ctx,
            },
            status_code=422,
        )


@router.post("/automations/{rule_id}/preview")
async def preview(
    rule_id: int,
    request: Request,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    from types import SimpleNamespace

    from app.models import WorkflowRule
    from app.services.settings_catalog import checked, integer

    rule = db.get(WorkflowRule, rule_id)
    if rule is None:
        return HTMLResponse("Rule not found", status_code=404)
    form = await request.form()
    try:
        sample = SimpleNamespace(
            source=str(form.get("sample_source", "")),
            severity_raw=str(form.get("sample_severity_raw", "")),
            status=str(form.get("sample_status", "firing")),
            severity_level_id=integer(form.get("sample_severity"), "Severity", optional=True),
            incident_type_id=integer(form.get("sample_type"), "Type", optional=True),
            system_id=integer(form.get("sample_system"), "System", optional=True),
            is_private=checked(form.get("sample_private", "")),
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "automation_preview.html",
            {"current_user": user, "rule": rule, "error": str(exc), "checks": [], "matched": False},
        )
    lookups = editor.load_lookups(db)
    checks = [
        {
            "description": editor._describe_condition(c, lookups),
            "matched": automation._matches([c], sample),
        }
        for c in rule.conditions or []
    ]
    return templates.TemplateResponse(
        request,
        "automation_preview.html",
        {
            "current_user": user,
            "rule": rule,
            "matched": automation._matches(rule.conditions, sample),
            "checks": checks,
            "summary": editor.summarize_rule(rule, lookups),
        },
    )


@router.post("/automations/{rule_id}/delete")
def delete(
    rule_id: int,
    request: Request,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    automation.delete_rule(db, rule_id)
    db.commit()
    request.session["flash"] = "Automation deleted. It will no longer run on future events."
    return RedirectResponse("/automations", status_code=303)


@router.post("/automations/{rule_id}/toggle")
def toggle(
    rule_id: int,
    request: Request,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    try:
        automation.toggle_rule(db, rule_id)
        db.commit()
        request.session["flash"] = "Automation activation updated."
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/automations", status_code=303)
