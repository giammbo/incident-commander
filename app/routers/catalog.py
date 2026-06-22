from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role, require_user
from app.db import get_db
from app.models import Component, Role, System, User
from app.services.catalog import (
    create_component,
    create_system,
    delete_component,
    delete_system,
    set_component_dependencies,
    set_system_dependencies,
    update_component,
    update_system,
)
from app.templating import templates

router = APIRouter()


@router.get("/systems", response_class=HTMLResponse)
def systems_page(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    from app.services.teams import list_teams

    systems = list(db.scalars(select(System).order_by(System.name)))
    teams = list_teams(db)
    return templates.TemplateResponse(
        request,
        "systems.html",
        {
            "current_user": user,
            "systems": systems,
            "teams": teams,
        },
    )


@router.get("/systems/{system_id}", response_class=HTMLResponse)
def system_detail(
    request: Request,
    system_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from app.services.teams import list_teams

    sysm = db.get(System, system_id)
    if sysm is None:
        return HTMLResponse("Not found", status_code=404)
    other_systems = list(
        db.scalars(select(System).where(System.id != system_id).order_by(System.name))
    )
    teams = list_teams(db)
    return templates.TemplateResponse(
        request,
        "system_detail.html",
        {"current_user": user, "s": sysm, "other_systems": other_systems, "teams": teams},
    )


@router.post("/systems")
def system_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    owner_team_id: int | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    try:
        create_system(
            db,
            name=name,
            description=description or None,
            created_by=user.id,
            owner_team_id=owner_team_id,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/systems", status_code=303)


@router.post("/systems/{system_id}/edit")
def system_edit(
    request: Request,
    system_id: int,
    name: str = Form(...),
    description: str = Form(""),
    owner_team_id: int | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    sysm = db.get(System, system_id)
    if sysm:
        try:
            update_system(
                db, sysm, name=name, description=description or None, owner_team_id=owner_team_id
            )
            db.commit()
        except ValueError as exc:
            db.rollback()
            request.session["flash"] = str(exc)
    return RedirectResponse(f"/systems/{system_id}", status_code=303)


@router.post("/systems/{system_id}/deps")
def system_deps(
    request: Request,
    system_id: int,
    depends_on_ids: list[int] = Form(default=[]),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    sysm = db.get(System, system_id)
    if sysm:
        try:
            set_system_dependencies(db, sysm, depends_on_ids)
            db.commit()
        except ValueError as exc:
            db.rollback()
            request.session["flash"] = str(exc)
    return RedirectResponse(f"/systems/{system_id}", status_code=303)


@router.post("/systems/{system_id}/delete")
def system_delete(
    request: Request,
    system_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    try:
        delete_system(db, system_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/systems", status_code=303)


@router.get("/components", response_class=HTMLResponse)
def components_page(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    from app.services.teams import list_teams

    components = list(db.scalars(select(Component).order_by(Component.name)))
    systems = list(db.scalars(select(System).order_by(System.name)))
    teams = list_teams(db)
    return templates.TemplateResponse(
        request,
        "components.html",
        {
            "current_user": user,
            "components": components,
            "systems": systems,
            "teams": teams,
        },
    )


@router.post("/components")
def component_create(
    request: Request,
    name: str = Form(...),
    system_id: int = Form(...),
    description: str = Form(""),
    owner_team_id: int | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    try:
        create_component(
            db,
            name=name,
            system_id=system_id,
            description=description or None,
            created_by=user.id,
            owner_team_id=owner_team_id,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/components", status_code=303)


@router.post("/components/{component_id}/edit")
def component_edit(
    request: Request,
    component_id: int,
    name: str = Form(...),
    system_id: int = Form(...),
    description: str = Form(""),
    owner_team_id: int | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    comp = db.get(Component, component_id)
    if comp:
        try:
            update_component(
                db,
                comp,
                name=name,
                description=description or None,
                system_id=system_id,
                owner_team_id=owner_team_id,
            )
            db.commit()
        except ValueError as exc:
            db.rollback()
            request.session["flash"] = str(exc)
    return RedirectResponse("/components", status_code=303)


@router.post("/components/{component_id}/deps")
def component_deps(
    request: Request,
    component_id: int,
    depends_on_ids: list[int] = Form(default=[]),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    comp = db.get(Component, component_id)
    if comp:
        try:
            set_component_dependencies(db, comp, depends_on_ids)
            db.commit()
        except ValueError as exc:
            db.rollback()
            request.session["flash"] = str(exc)
    return RedirectResponse("/components", status_code=303)


@router.post("/components/{component_id}/delete")
def component_delete(
    request: Request,
    component_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    try:
        delete_component(db, component_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse("/components", status_code=303)
