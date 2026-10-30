"""Fixed Settings catalog allowlists, preserving identities and references."""

from __future__ import annotations

import re

from sqlalchemy import func, select

from app.models import (
    Component,
    CustomFieldDef,
    Incident,
    IncidentRoleAssignment,
    IncidentRoleType,
    IncidentType,
    SeverityLevel,
    StatusCategory,
    StatusLevel,
    System,
    Team,
    WorkflowRule,
)
from app.services import custom_fields

MODELS = {
    "severity": SeverityLevel,
    "statuses": StatusLevel,
    "incident-types": IncidentType,
    "roles": IncidentRoleType,
    "teams": Team,
    "custom-fields": CustomFieldDef,
}
LIMITS = {
    "severity": 40,
    "statuses": 40,
    "incident-types": 60,
    "roles": 60,
    "teams": 80,
    "custom-fields": 80,
}


def text(value, label, limit, *, required=True):
    value = str(value or "").strip()
    if required and not value:
        raise ValueError(f"{label} is required.")
    if len(value) > limit:
        raise ValueError(f"{label} must be {limit} characters or fewer.")
    return value


def integer(value, label="Rank", *, optional=False):
    if optional and (value is None or value == ""):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number.") from None
    if not 0 <= n <= 2147483647:
        raise ValueError(f"{label} must be between 0 and 2147483647.")
    return n


def checked(value):
    return str(value).lower() in ("true", "on", "1", "yes")


def save(db, kind, data, *, record_id=None):
    model = MODELS[kind]  # fixed map, never a client-provided model/attribute
    row = db.get(model, record_id) if record_id is not None else None
    if record_id is not None and row is None:
        raise ValueError("Record not found.")
    field = "name" if kind == "teams" else "label"
    label = text(data.get(field), "Name" if field == "name" else "Label", LIMITS[kind])
    clash = select(model).where(func.lower(getattr(model, field)) == label.lower())
    if row is not None:
        clash = clash.where(model.id != row.id)
    if db.scalar(clash) is not None:
        raise ValueError("A record with this name already exists.")
    values = {field: label}
    if kind != "teams":
        values["rank"] = integer(data.get("rank", "100"))
    if kind == "severity":
        color = str(data.get("color", "")).strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", color):
            raise ValueError("Colour must be a hexadecimal value such as #FF5D5D.")
        values["color"] = color
    if kind == "statuses":
        try:
            values["category"] = StatusCategory(data.get("category"))
        except ValueError:
            raise ValueError("Choose a valid status category.") from None
        if (
            row is not None
            and row.category != values["category"]
            and db.scalar(select(Incident.id).where(Incident.status_id == row.id).limit(1))
        ):
            raise ValueError("This status is in use; its lifecycle category cannot change.")
    if kind in ("teams", "incident-types"):
        values["description"] = (
            text(data.get("description"), "Description", 10000, required=False) or None
        )
    if kind == "incident-types":
        sid = integer(data.get("default_severity_level_id"), "Default severity", optional=True)
        if sid is not None and db.get(SeverityLevel, sid) is None:
            raise ValueError("Choose an existing default severity.")
        values["default_severity_level_id"] = sid
    if kind == "custom-fields":
        args = {
            "label": label,
            "field_type": data.get("field_type", ""),
            "options": data.get("options", ""),
            "rank": values["rank"],
            "required": checked(data.get("required", "")),
            "incident_type_ids": [
                integer(v, "Incident type") for v in data.getlist("incident_type_ids")
            ],
        }
        if row is None:
            return custom_fields.create_field_def(db, **args)
        custom_fields.update_field_def(db, row, **args)
        return row
    if row is None:
        row = model(**values)
        db.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    if hasattr(model, "is_default") and checked(data.get("is_default", "")):
        for previous in db.scalars(select(model).where(model.is_default.is_(True))):
            previous.is_default = False
        row.is_default = True
    db.flush()
    return row


def make_default(db, kind, record_id):
    model = MODELS[kind]
    target = db.get(model, record_id)
    if target is None:
        raise ValueError("Record not found.")
    for row in db.scalars(select(model).where(model.is_default.is_(True))):
        row.is_default = False
    target.is_default = True
    db.flush()


def _rule_uses(rule, kind, record_id):
    def same(value):
        return str(value) == str(record_id)

    fields = {"severity": "severity", "incident-types": "type"}
    for c in rule.conditions or []:
        value = c.get("value")
        if (
            kind in fields
            and c.get("field") == fields[kind]
            and (same(value) or isinstance(value, list) and any(same(v) for v in value))
        ):
            return True
    params = {
        "severity": "severity_level_id",
        "statuses": "status_id",
        "incident-types": "incident_type_id",
        "roles": "role_type_id",
    }
    return kind in params and any(
        same((a.get("params") or {}).get(params[kind])) for a in rule.actions or []
    )


def delete(db, kind, record_id):
    model = MODELS[kind]
    row = db.get(model, record_id)
    if row is None:
        return
    if getattr(row, "is_default", False):
        raise ValueError("Choose another default before deleting this record.")
    refs = {
        "severity": [
            (Incident, Incident.severity_level_id),
            (IncidentType, IncidentType.default_severity_level_id),
        ],
        "statuses": [(Incident, Incident.status_id)],
        "incident-types": [(Incident, Incident.incident_type_id)],
        "roles": [(IncidentRoleAssignment, IncidentRoleAssignment.role_type_id)],
        "teams": [(System, System.owner_team_id), (Component, Component.owner_team_id)],
    }
    for other, column in refs.get(kind, []):
        if db.scalar(select(other.id).where(column == record_id).limit(1)):
            raise ValueError("This record is in use. Reassign its references before deleting it.")
    if any(_rule_uses(r, kind, record_id) for r in db.scalars(select(WorkflowRule))):
        raise ValueError("This record is in use by an automation. Edit or delete that rule first.")
    db.delete(row)
    db.flush()
