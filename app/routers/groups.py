from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import get_db
from app.models import Group, Role, User
from app.templating import templates

router = APIRouter()


@router.get("/groups", response_class=HTMLResponse)
def groups_page(
    request: Request, user: User = Depends(require_role(Role.admin)), db: Session = Depends(get_db)
):
    groups = list(db.scalars(select(Group).order_by(Group.id)))
    return templates.TemplateResponse(
        request, "groups.html", {"current_user": user, "groups": groups, "roles": list(Role)}
    )


@router.post("/groups")
def groups_create(
    name: str = Form(...),
    role: Role = Form(...),
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    if not db.scalar(select(Group).where(Group.name == name)):
        db.add(Group(name=name, role=role))
        db.commit()
    return RedirectResponse("/groups", status_code=303)
