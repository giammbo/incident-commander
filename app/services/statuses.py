from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Incident, StatusCategory, StatusLevel

_DEFAULTS = [
    {"label": "Triage", "category": StatusCategory.triage, "rank": 1, "is_default": True},
    {"label": "Investigating", "category": StatusCategory.active, "rank": 2, "is_default": False},
    {"label": "Identified", "category": StatusCategory.active, "rank": 3, "is_default": False},
    {"label": "Monitoring", "category": StatusCategory.active, "rank": 4, "is_default": False},
    {"label": "Closed", "category": StatusCategory.closed, "rank": 5, "is_default": False},
]


def seed_status_levels(db: Session) -> None:
    if db.scalar(select(StatusLevel).limit(1)) is not None:
        return
    for s in _DEFAULTS:
        db.add(StatusLevel(**s))
    db.flush()


def default_status_level_id(db: Session) -> int | None:
    lvl = db.scalar(select(StatusLevel).where(StatusLevel.is_default.is_(True)))
    if lvl is None:
        lvl = db.scalar(select(StatusLevel).order_by(StatusLevel.rank))
    return lvl.id if lvl else None


def create_status_level(db, *, label, category, rank=100, is_default=False) -> StatusLevel:
    label = label.strip()
    if not label:
        raise ValueError("Status label is required")
    if db.scalar(select(StatusLevel).where(StatusLevel.label == label)):
        raise ValueError(f"A status '{label}' already exists")
    cat = StatusCategory(category)
    if is_default:
        for lvl in db.scalars(select(StatusLevel).where(StatusLevel.is_default.is_(True))):
            lvl.is_default = False
    s = StatusLevel(label=label, category=cat, rank=rank, is_default=is_default)
    db.add(s)
    db.flush()
    return s


def update_status_level(db, status, *, label, category, rank) -> None:
    label = label.strip()
    if not label:
        raise ValueError("Status label is required")
    clash = db.scalar(
        select(StatusLevel).where(StatusLevel.label == label, StatusLevel.id != status.id)
    )
    if clash:
        raise ValueError(f"A status '{label}' already exists")
    status.label = label
    status.category = StatusCategory(category)
    status.rank = rank
    db.flush()


def set_default_status_level(db, level_id: int) -> None:
    target = db.get(StatusLevel, level_id)
    if target is None:
        raise ValueError("Status not found")
    for lvl in db.scalars(select(StatusLevel).where(StatusLevel.is_default.is_(True))):
        lvl.is_default = False
    target.is_default = True
    db.flush()


def delete_status_level(db, level_id: int) -> None:
    if db.scalar(select(Incident).where(Incident.status_id == level_id)):
        raise ValueError("This status is in use by an incident")
    s = db.get(StatusLevel, level_id)
    if s:
        db.delete(s)
        db.flush()
