"""Bound browser validation: drafts live only in this response, never in cookies."""

import json
from datetime import date
from math import isfinite

from app.templating import templates


def validation_response(request, response, error, draft=None, action=None):
    context = dict(response.context)
    allowed = {
        "title",
        "incident_id",
        "description",
        "severity_level_id",
        "incident_type_id",
        "system_id",
        "component_ids",
        "is_private",
        "status_id",
        "slack_connection_id",
        "video",
        "assignee_id",
        "due_on",
        "body",
        "message",
        "return_tab",
        "return_filter",
    }
    bound = {
        key: value
        for key, value in (draft or {}).items()
        if key in allowed or (key.startswith("cf_") and key[3:].isdigit())
    }
    context.update(core_error=error, core_draft=bound, core_action=action or request.url.path)
    headers = {}
    if request.headers.get("HX-Request"):
        headers = {"HX-Retarget": "main", "HX-Reswap": "innerHTML", "HX-Reselect": "main > *"}
    return templates.TemplateResponse(
        request, response.template.name, context, status_code=422, headers=headers
    )


def custom_values(db, type_id, raw):
    from app.services.custom_fields import fields_for_type

    values = {}
    for field in fields_for_type(db, type_id):
        items = [str(v).strip() for v in raw.get(field.id, []) if str(v).strip()]
        if field.required and not items:
            raise ValueError(f"{field.label} is required")
        if not items:
            continue
        if field.field_type in ("select", "multi_select") and any(
            v not in field.options_list for v in items
        ):
            raise ValueError(f"Choose a valid option for {field.label}")
        if field.field_type == "number":
            try:
                if not isfinite(float(items[0])):
                    raise ValueError
            except ValueError:
                raise ValueError(f"{field.label} must be a number") from None
        if field.field_type == "date":
            try:
                date.fromisoformat(items[0])
            except ValueError:
                raise ValueError(f"{field.label} must be a valid date") from None
        values[field.id] = json.dumps(items) if field.field_type == "multi_select" else items[0]
    return values
