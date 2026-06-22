from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IncidentRoleAssignment, IncidentRoleType, User

_DEFAULTS = [("Incident Lead", 1), ("Communications", 2), ("Scribe", 3)]


def seed_incident_role_types(db: Session) -> None:
    if db.scalar(select(IncidentRoleType).limit(1)) is not None:
        return
    for label, rank in _DEFAULTS:
        db.add(IncidentRoleType(label=label, rank=rank))
    db.flush()


def list_incident_role_types(db: Session) -> list[IncidentRoleType]:
    return list(
        db.scalars(select(IncidentRoleType).order_by(IncidentRoleType.rank, IncidentRoleType.id))
    )


def create_incident_role_type(db: Session, *, label: str, rank: int = 100) -> IncidentRoleType:
    label = label.strip()
    if not label:
        raise ValueError("Role label is required")
    if db.scalar(select(IncidentRoleType).where(IncidentRoleType.label == label)):
        raise ValueError(f"A role '{label}' already exists")
    rt = IncidentRoleType(label=label, rank=rank)
    db.add(rt)
    db.flush()
    return rt


def delete_incident_role_type(db: Session, role_type_id: int) -> None:
    in_use = db.scalar(
        select(IncidentRoleAssignment).where(IncidentRoleAssignment.role_type_id == role_type_id)
    )
    if in_use is not None:
        raise ValueError("This role is assigned on an incident")
    rt = db.get(IncidentRoleType, role_type_id)
    if rt:
        db.delete(rt)
        db.flush()


def assignments_by_role(incident) -> dict[int, list[User]]:
    out: dict[int, list[User]] = {}
    for a in incident.role_assignments:
        out.setdefault(a.role_type_id, []).append(a.user)
    return out
