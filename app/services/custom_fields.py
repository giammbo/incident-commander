from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CustomFieldDef, IncidentCustomFieldValue, IncidentType

FIELD_TYPES = ("text", "textarea", "select", "multi_select", "number", "checkbox", "date")


def list_field_defs(db: Session) -> list[CustomFieldDef]:
    return list(db.scalars(select(CustomFieldDef).order_by(CustomFieldDef.rank, CustomFieldDef.id)))


def _validate_definition(db, *, label, field_type, options, rank, incident_type_ids, fd=None):
    label = (label or "").strip()
    if not label or len(label) > 80:
        raise ValueError("Field label is required and must be 80 characters or fewer")
    if field_type not in FIELD_TYPES:
        raise ValueError("Invalid field type")
    if not isinstance(rank, int) or not 0 <= rank <= 2147483647:
        raise ValueError("Rank must be a positive whole number")
    clash = select(CustomFieldDef).where(func.lower(CustomFieldDef.label) == label.lower())
    if fd is not None:
        clash = clash.where(CustomFieldDef.id != fd.id)
    if db.scalar(clash):
        raise ValueError("A field with this label already exists")
    choices = [o.strip() for o in (options or "").splitlines() if o.strip()]
    if field_type in ("select", "multi_select"):
        if not choices or len(choices) != len(set(choices)):
            raise ValueError("Select fields need at least one option, with no duplicates")
    ids = set(incident_type_ids or [])
    if ids and set(db.scalars(select(IncidentType.id).where(IncidentType.id.in_(ids)))) != ids:
        raise ValueError("Choose existing incident types")
    if fd is not None:
        used = list(
            db.scalars(
                select(IncidentCustomFieldValue).where(IncidentCustomFieldValue.field_id == fd.id)
            )
        )
        if used and field_type != fd.field_type:
            raise ValueError(
                "This field is in use; its type cannot change. Existing values are preserved."
            )
        if used and field_type in ("select", "multi_select"):
            for value in used:
                try:
                    selected = (
                        json.loads(value.value) if field_type == "multi_select" else [value.value]
                    )
                except ValueError:
                    selected = [value.value]
                if not isinstance(selected, list) or any(v not in choices for v in selected):
                    raise ValueError("This option is in use; keep it to preserve existing values.")
    return label, "\n".join(choices) if field_type in ("select", "multi_select") else None


def _apply_types(db, fd, incident_type_ids):
    ids = list(incident_type_ids or [])
    fd.incident_types = (
        list(db.scalars(select(IncidentType).where(IncidentType.id.in_(ids)))) if ids else []
    )


def create_field_def(
    db, *, label, field_type, options=None, required=False, rank=100, incident_type_ids=None
) -> CustomFieldDef:
    label, options = _validate_definition(
        db,
        label=label,
        field_type=field_type,
        options=options,
        rank=rank,
        incident_type_ids=incident_type_ids,
    )
    fd = CustomFieldDef(
        label=label, field_type=field_type, options=options, required=required, rank=rank
    )
    db.add(fd)
    db.flush()
    _apply_types(db, fd, incident_type_ids)
    db.flush()
    return fd


def update_field_def(db, fd, *, label, field_type, options, required, rank, incident_type_ids):
    label, options = _validate_definition(
        db,
        label=label,
        field_type=field_type,
        options=options,
        rank=rank,
        incident_type_ids=incident_type_ids,
        fd=fd,
    )
    fd.label, fd.field_type, fd.options, fd.required, fd.rank = (
        label,
        field_type,
        options,
        required,
        rank,
    )
    _apply_types(db, fd, incident_type_ids)
    db.flush()


def set_field_def_types(db, fd, incident_type_ids):
    _apply_types(db, fd, incident_type_ids)
    db.flush()


def delete_field_def(db, field_id):
    fd = db.get(CustomFieldDef, field_id)
    if fd:
        db.delete(fd)
        db.flush()


def fields_for_type(db, incident_type_id) -> list[CustomFieldDef]:
    out = []
    for fd in list_field_defs(db):
        if not fd.incident_types or (
            incident_type_id and any(t.id == incident_type_id for t in fd.incident_types)
        ):
            out.append(fd)
    return out


def values_for_incident(incident) -> dict[int, str]:
    return {v.field_id: v.value for v in incident.custom_values}


def display_value(field_def, raw) -> str:
    if raw is None or raw == "":
        return ""
    if field_def.field_type == "multi_select":
        try:
            return ", ".join(json.loads(raw))
        except (ValueError, TypeError):
            return raw
    if field_def.field_type == "checkbox":
        return "Yes" if raw == "true" else ""
    return raw


def _coerce(field_def, raw) -> str | None:
    """Return the stored string, or None to remove the value. Accepts a str OR a
    list (the route always passes form.getlist), so scalar types take the first item."""
    ft = field_def.field_type
    if ft == "multi_select":
        items = raw if isinstance(raw, list) else ([raw] if raw else [])
        items = [s for s in items if s]
        return json.dumps(items) if items else None
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    if ft == "checkbox":
        return "true" if raw else None
    s = (raw or "").strip() if isinstance(raw, str) else ""
    if not s:
        return None
    if ft == "number":
        try:
            float(s)
        except ValueError:
            return None
    return s


def set_incident_values(db, incident, raw: dict) -> list[str]:
    existing = {v.field_id: v for v in incident.custom_values}
    missing: list[str] = []
    for fd in fields_for_type(db, incident.incident_type_id):
        coerced = _coerce(fd, raw.get(fd.id))
        if coerced is None:
            if fd.id in existing:
                db.delete(existing[fd.id])
            if fd.required:
                missing.append(fd.label)
        elif fd.id in existing:
            existing[fd.id].value = coerced
        else:
            db.add(IncidentCustomFieldValue(incident_id=incident.id, field_id=fd.id, value=coerced))
    db.flush()
    db.expire(incident, ["custom_values"])
    return missing
