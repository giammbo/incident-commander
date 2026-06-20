from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Service, SeverityLevel

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")

_DEFAULT_LEVELS = [
    {"label": "SEV1", "color": "#FF5D5D", "rank": 1, "is_default": False},
    {"label": "SEV2", "color": "#F4B740", "rank": 2, "is_default": True},
    {"label": "SEV3", "color": "#56B6E6", "rank": 3, "is_default": False},
]


def seed_severity_levels(db: Session) -> None:
    if db.scalar(select(SeverityLevel).limit(1)) is not None:
        return
    for lvl in _DEFAULT_LEVELS:
        db.add(SeverityLevel(**lvl))
    db.flush()


def default_severity_level_id(db: Session) -> int | None:
    lvl = db.scalar(select(SeverityLevel).where(SeverityLevel.is_default.is_(True)))
    if lvl is None:
        lvl = db.scalar(select(SeverityLevel).order_by(SeverityLevel.rank))
    return lvl.id if lvl else None


def create_severity_level(
    db: Session, *, label: str, color: str, rank: int, is_default: bool = False
) -> SeverityLevel:
    label = label.strip()
    if not label:
        raise ValueError("Severity label is required")
    if not _HEX_COLOR.match(color or ""):
        raise ValueError("Colour must be a hex value like #FF5D5D")
    if db.scalar(select(SeverityLevel).where(SeverityLevel.label == label)):
        raise ValueError(f"A severity level '{label}' already exists")
    if is_default:
        for lvl in db.scalars(select(SeverityLevel).where(SeverityLevel.is_default.is_(True))):
            lvl.is_default = False
    level = SeverityLevel(label=label, color=color, rank=rank, is_default=is_default)
    db.add(level)
    db.flush()
    return level


def set_default_severity_level(db: Session, level_id: int) -> None:
    target = db.get(SeverityLevel, level_id)
    if target is None:
        raise ValueError("Severity level not found")
    for lvl in db.scalars(select(SeverityLevel).where(SeverityLevel.is_default.is_(True))):
        lvl.is_default = False
    target.is_default = True
    db.flush()


def delete_severity_level(db: Session, level_id: int) -> None:
    from app.models import Incident

    if db.scalar(select(Incident).where(Incident.severity_level_id == level_id)):
        raise ValueError("This severity level is in use by an incident")
    level = db.get(SeverityLevel, level_id)
    if level:
        db.delete(level)
        db.flush()


def create_service(
    db: Session, *, name: str, description: str | None = None, created_by: int | None = None
) -> Service:
    svc = Service(name=name.strip(), description=description, created_by=created_by)
    db.add(svc)
    db.flush()
    return svc


def set_service_dependencies(db: Session, service: Service, depends_on_ids: list[int]) -> None:
    ids = [i for i in depends_on_ids if i != service.id]
    service.depends_on = list(db.scalars(select(Service).where(Service.id.in_(ids)))) if ids else []
    db.flush()


def delete_service(db: Session, service_id: int) -> None:
    svc = db.get(Service, service_id)
    if svc:
        db.delete(svc)
        db.flush()
