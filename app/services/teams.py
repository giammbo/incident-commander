from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Component, System, Team


def list_teams(db: Session) -> list[Team]:
    return list(db.scalars(select(Team).order_by(Team.name)))


def create_team(db: Session, *, name: str, description: str | None = None) -> Team:
    name = name.strip()
    if not name:
        raise ValueError("Team name is required")
    if db.scalar(select(Team).where(Team.name == name)):
        raise ValueError(f"A team '{name}' already exists")
    t = Team(name=name, description=description)
    db.add(t)
    db.flush()
    return t


def update_team(db: Session, team: Team, *, name: str, description: str | None) -> None:
    name = name.strip()
    if not name:
        raise ValueError("Team name is required")
    clash = db.scalar(select(Team).where(Team.name == name, Team.id != team.id))
    if clash:
        raise ValueError(f"A team '{name}' already exists")
    team.name = name
    team.description = description
    db.flush()


def delete_team(db: Session, team_id: int) -> None:
    if db.scalar(select(System).where(System.owner_team_id == team_id)) or db.scalar(
        select(Component).where(Component.owner_team_id == team_id)
    ):
        raise ValueError("This team owns a system or component; reassign it first")
    t = db.get(Team, team_id)
    if t:
        db.delete(t)
        db.flush()
