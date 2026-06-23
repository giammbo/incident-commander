from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Component, SeverityLevel, System

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")

_UNSET = object()

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


def create_system(
    db: Session,
    *,
    name: str,
    description: str | None = None,
    created_by: int | None = None,
    owner_team_id: int | None = None,
) -> System:
    name = name.strip()
    if not name:
        raise ValueError("System name is required")
    if db.scalar(select(System).where(System.name == name)):
        raise ValueError(f"A system '{name}' already exists")
    sysm = System(
        name=name, description=description, created_by=created_by, owner_team_id=owner_team_id
    )
    db.add(sysm)
    db.flush()
    return sysm


def update_system(
    db: Session,
    system: System,
    *,
    name=_UNSET,
    description=_UNSET,
    owner_team_id=_UNSET,
) -> None:
    if name is not _UNSET:
        name = name.strip()
        if not name:
            raise ValueError("System name is required")
        clash = db.scalar(select(System).where(System.name == name, System.id != system.id))
        if clash:
            raise ValueError(f"A system '{name}' already exists")
        system.name = name
    if description is not _UNSET:
        system.description = description
    if owner_team_id is not _UNSET:
        system.owner_team_id = owner_team_id
    db.flush()


def set_system_dependencies(db: Session, system: System, depends_on_ids: list[int]) -> None:
    ids = [i for i in depends_on_ids if i != system.id]
    system.depends_on = list(db.scalars(select(System).where(System.id.in_(ids)))) if ids else []
    db.flush()


def delete_system(db: Session, system_id: int) -> None:
    from app.models import Incident

    if db.scalar(select(Incident).where(Incident.system_id == system_id)):
        raise ValueError("This system is referenced by an incident; reassign it first")
    if db.scalar(select(Component).where(Component.system_id == system_id)):
        raise ValueError("This system still has components; reassign or delete them first")
    sysm = db.get(System, system_id)
    if sysm:
        db.delete(sysm)
        db.flush()


def create_component(
    db: Session,
    *,
    name: str,
    system_id: int,
    description: str | None = None,
    created_by: int | None = None,
    owner_team_id: int | None = None,
) -> Component:
    name = name.strip()
    if not name:
        raise ValueError("Component name is required")
    if db.get(System, system_id) is None:
        raise ValueError("Unknown system")
    if db.scalar(select(Component).where(Component.name == name)):
        raise ValueError(f"A component '{name}' already exists")
    comp = Component(
        name=name,
        description=description,
        system_id=system_id,
        created_by=created_by,
        owner_team_id=owner_team_id,
    )
    db.add(comp)
    db.flush()
    return comp


def update_component(
    db: Session,
    component: Component,
    *,
    name: str,
    description: str | None,
    system_id: int,
    owner_team_id: int | None = None,
) -> None:
    name = name.strip()
    if not name:
        raise ValueError("Component name is required")
    if db.get(System, system_id) is None:
        raise ValueError("Unknown system")
    clash = db.scalar(select(Component).where(Component.name == name, Component.id != component.id))
    if clash:
        raise ValueError(f"A component '{name}' already exists")
    moving = system_id != component.system_id
    component.name = name
    component.description = description
    component.system_id = system_id
    component.owner_team_id = owner_team_id
    if moving:
        # Drop dependency edges (both directions) that would now be cross-system.
        component.depends_on = [d for d in component.depends_on if d.system_id == system_id]
        for other in db.scalars(select(Component).where(Component.system_id != system_id)):
            if component in other.depends_on:
                other.depends_on = [d for d in other.depends_on if d.id != component.id]
    db.flush()


def set_component_dependencies(
    db: Session, component: Component, depends_on_ids: list[int]
) -> None:
    ids = [i for i in depends_on_ids if i != component.id]
    targets = list(db.scalars(select(Component).where(Component.id.in_(ids)))) if ids else []
    for t in targets:
        if t.system_id != component.system_id:
            raise ValueError("Components can only depend on components in the same system")
    component.depends_on = targets
    db.flush()


def delete_component(db: Session, component_id: int) -> None:
    comp = db.get(Component, component_id)
    if comp:
        db.delete(comp)
        db.flush()
