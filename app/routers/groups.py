from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import get_db
from app.models import Group, Role, User
from app.services.settings_catalog import text
from app.services.users import ADMINS_GROUP
from app.templating import templates

router = APIRouter()


def protected_group(group):
    return (
        (group.is_system and group.role == Role.admin)
        or group.name == ADMINS_GROUP
        or any(u.is_protected_admin for u in group.members)
    )


@router.get("/groups", response_class=HTMLResponse)
def groups_page(
    request: Request, user: User = Depends(require_role(Role.admin)), db: Session = Depends(get_db)
):
    return render_groups(request, user, db)


def render_groups(request, user, db, *, draft=None, error=None, status_code=200):
    groups = list(db.scalars(select(Group).order_by(Group.id)))
    return templates.TemplateResponse(
        request,
        "groups.html",
        {
            "current_user": user,
            "groups": groups,
            "roles": list(Role),
            "protected_group": protected_group,
            "draft": draft or {},
            "error": error,
        },
        status_code=status_code,
    )


@router.post("/groups")
@router.post("/groups/{group_id}/edit")
async def groups_save(
    request: Request,
    group_id: int | None = None,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    group = db.get(Group, group_id) if group_id is not None else None
    if group_id is not None and group is None:
        return HTMLResponse("Group not found", status_code=404)
    try:
        name = text(form.get("name"), "Group name", 120)
        try:
            role = Role(form.get("role"))
        except ValueError:
            raise ValueError("Choose a valid role.") from None
        if (
            group is not None
            and protected_group(group)
            and (name != group.name or role != Role.admin)
        ):
            raise ValueError("This protected admin group must keep its name and Admin authority.")
        clash = select(Group).where(func.lower(Group.name) == name.lower())
        if group is not None:
            clash = clash.where(Group.id != group.id)
        if db.scalar(clash):
            raise ValueError("A group with this name already exists.")
        if group is None:
            db.add(Group(name=name, role=role))
        else:
            group.name, group.role = name, role
        db.commit()
        request.session["flash"] = "Group saved. Member access now uses this role."
    except ValueError as exc:
        db.rollback()
        return render_groups(
            request,
            user,
            db,
            draft={
                "id": group_id,
                "name": str(form.get("name", "")),
                "role": str(form.get("role", "")),
            },
            error=str(exc),
            status_code=422,
        )
    return RedirectResponse("/groups", status_code=303)


@router.post("/groups/{group_id}/delete")
def groups_delete(
    group_id: int,
    request: Request,
    user: User = Depends(require_role(Role.admin)),
    db: Session = Depends(get_db),
):
    group = db.get(Group, group_id)
    if group is None:
        return HTMLResponse("Group not found", status_code=404)
    if protected_group(group) or group.is_system:
        request.session["flash"] = "Protected and system groups cannot be deleted."
    elif group.members:
        request.session["flash"] = "This group is in use. Reassign its members before deleting it."
    else:
        db.delete(group)
        db.commit()
        request.session["flash"] = "Group deleted."
    return RedirectResponse("/groups", status_code=303)
