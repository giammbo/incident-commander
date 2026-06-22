from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Incident, IncidentType

_DEFAULTS = ["Outage", "Degraded performance", "Maintenance", "Security"]


def seed_incident_types(db: Session) -> None:
    if db.scalar(select(IncidentType).limit(1)) is not None:
        return
    for i, label in enumerate(_DEFAULTS, start=1):
        db.add(IncidentType(label=label, rank=i, is_default=(i == 1)))
    db.flush()


def list_incident_types(db: Session) -> list[IncidentType]:
    return list(db.scalars(select(IncidentType).order_by(IncidentType.rank, IncidentType.id)))


def default_incident_type_id(db: Session) -> int | None:
    t = db.scalar(select(IncidentType).where(IncidentType.is_default.is_(True)))
    if t is None:
        t = db.scalar(select(IncidentType).order_by(IncidentType.rank))
    return t.id if t else None


def create_incident_type(
    db, *, label, description=None, rank=100, is_default=False, default_severity_level_id=None
) -> IncidentType:
    label = label.strip()
    if not label:
        raise ValueError("Type label is required")
    if db.scalar(select(IncidentType).where(IncidentType.label == label)):
        raise ValueError(f"A type '{label}' already exists")
    if is_default:
        for t in db.scalars(select(IncidentType).where(IncidentType.is_default.is_(True))):
            t.is_default = False
    t = IncidentType(
        label=label,
        description=description,
        rank=rank,
        is_default=is_default,
        default_severity_level_id=default_severity_level_id,
    )
    db.add(t)
    db.flush()
    return t


def update_incident_type(db, t, *, label, description, rank, default_severity_level_id) -> None:
    label = label.strip()
    if not label:
        raise ValueError("Type label is required")
    clash = db.scalar(
        select(IncidentType).where(IncidentType.label == label, IncidentType.id != t.id)
    )
    if clash:
        raise ValueError(f"A type '{label}' already exists")
    t.label = label
    t.description = description
    t.rank = rank
    t.default_severity_level_id = default_severity_level_id
    db.flush()


def set_default_incident_type(db, type_id: int) -> None:
    target = db.get(IncidentType, type_id)
    if target is None:
        raise ValueError("Type not found")
    for t in db.scalars(select(IncidentType).where(IncidentType.is_default.is_(True))):
        t.is_default = False
    target.is_default = True
    db.flush()


def delete_incident_type(db, type_id: int) -> None:
    if db.scalar(select(Incident).where(Incident.incident_type_id == type_id)):
        raise ValueError("This type is in use by an incident")
    t = db.get(IncidentType, type_id)
    if t:
        db.delete(t)
        db.flush()
