from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import require_role, require_user
from app.db import get_db
from app.models import Component, Incident, Role, System, User
from app.services.catalog import (
    create_component,
    create_system,
    delete_component,
    delete_system,
    dependency_view,
    set_component_dependencies,
    set_system_dependencies,
    update_component,
    update_system,
)
from app.templating import templates

router = APIRouter()


def _optional_id(value: str, label: str) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        raise ValueError(f"Choose an existing {label}") from None
    if parsed < 1:
        raise ValueError(f"Choose an existing {label}")
    return parsed


def _page(db, stmt, page):
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    pages = max(1, (total + 19) // 20)
    page = min(max(page, 1), pages)
    items = list(db.scalars(stmt.offset((page - 1) * 20).limit(20)))
    return items, {"page": page, "pages": pages, "total": total}


def _listing(
    request, user, db, *, kind, q="", team_id=None, system_id=None, page=1, draft=None, error=None
):
    from app.services.teams import list_teams

    model = System if kind == "systems" else Component
    stmt = select(model)
    q = q.strip()
    if q:
        stmt = stmt.where(
            or_(
                model.name.icontains(q, autoescape=True),
                model.description.icontains(q, autoescape=True),
            )
        )
    if team_id is not None:
        stmt = stmt.where(model.owner_team_id == team_id)
    if kind == "components" and system_id is not None:
        stmt = stmt.where(Component.system_id == system_id)
    items, pagination = _page(db, stmt.order_by(func.lower(model.name), model.id), page)
    filters = {"q": q, "team_id": team_id, "system_id": system_id}
    params = {key: val for key, val in filters.items() if val is not None and val != ""}
    pagination["previous"] = "?" + urlencode({**params, "page": pagination["page"] - 1})
    pagination["next"] = "?" + urlencode({**params, "page": pagination["page"] + 1})
    all_components = (
        list(db.scalars(select(Component).order_by(Component.name))) if kind == "components" else []
    )
    return templates.TemplateResponse(
        request,
        f"{kind}.html",
        {
            "current_user": user,
            kind: items,
            "teams": list_teams(db),
            "all_components": all_components,
            "deps": dependency_view(all_components),
            "all_systems": list(db.scalars(select(System).order_by(System.name))),
            "filters": filters,
            "pagination": pagination,
            "draft": draft or {},
            "error": error if draft is not None else None,
            "filter_error": error if draft is None else None,
        },
        status_code=422 if error else 200,
    )


def _detail(request, user, db, *, kind, item_id, draft=None, error=None):
    from app.services.incident_access import visible_incident_condition
    from app.services.teams import list_teams

    model = System if kind == "system" else Component
    item = db.get(model, item_id)
    if item is None:
        raise HTTPException(404, "Not found")
    peers = list(db.scalars(select(model).order_by(model.name)))
    if kind == "component":
        peers = [c for c in peers if c.system_id == item.system_id]
    view = dependency_view(peers)
    condition = (
        Incident.system_id == item.id
        if kind == "system"
        else Incident.components.any(Component.id == item.id)
    )
    incidents = list(
        db.scalars(
            select(Incident)
            .where(condition, visible_incident_condition(user))
            .order_by(Incident.created_at.desc())
            .limit(20)
        )
    )
    return templates.TemplateResponse(
        request,
        f"{kind}_detail.html",
        {
            "current_user": user,
            "s" if kind == "system" else "c": item,
            "other_systems": [x for x in peers if x.id != item_id],
            "peers": [x for x in peers if x.id != item_id],
            "systems": list(db.scalars(select(System).order_by(System.name))),
            "teams": list_teams(db),
            "used_by": view["used_by"].get(item.id, []),
            "blocked": view["blocked"].get(item.id, set()),
            "incidents": incidents,
            "draft": draft,
            "error": error,
        },
        status_code=422 if error else 200,
    )


@router.get("/systems", response_class=HTMLResponse)
def systems_page(
    request: Request,
    q: str = "",
    team_id: str = "",
    page: int = 1,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        owner = _optional_id(team_id, "owner team")
    except ValueError as exc:
        return _listing(request, user, db, kind="systems", q=q, error=str(exc))
    return _listing(request, user, db, kind="systems", q=q, team_id=owner, page=page)


@router.get("/systems/{system_id}", response_class=HTMLResponse)
def system_detail(
    request: Request,
    system_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return _detail(request, user, db, kind="system", item_id=system_id)


@router.post("/systems")
def system_create(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    owner_team_id: str = Form(""),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    draft = {"name": name, "description": description, "owner_team_id": owner_team_id}
    try:
        create_system(
            db,
            name=name,
            description=description or None,
            created_by=user.id,
            owner_team_id=_optional_id(owner_team_id, "owner team"),
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _listing(request, user, db, kind="systems", draft=draft, error=str(exc))
    return RedirectResponse("/systems", status_code=303)


@router.post("/systems/{system_id}/edit")
def system_edit(
    request: Request,
    system_id: int,
    name: str = Form(""),
    description: str = Form(""),
    owner_team_id: str = Form(""),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    sysm = db.get(System, system_id)
    if sysm is None:
        raise HTTPException(404, "Not found")
    draft = {"name": name, "description": description, "owner_team_id": owner_team_id}
    try:
        update_system(
            db,
            sysm,
            name=name,
            description=description or None,
            owner_team_id=_optional_id(owner_team_id, "owner team"),
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _detail(
            request, user, db, kind="system", item_id=system_id, draft=draft, error=str(exc)
        )
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
    request: Request,
    q: str = "",
    team_id: str = "",
    system_id: str = "",
    page: int = 1,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        owner = _optional_id(team_id, "owner team")
        parent = _optional_id(system_id, "system")
    except ValueError as exc:
        return _listing(request, user, db, kind="components", q=q, error=str(exc))
    return _listing(
        request, user, db, kind="components", q=q, team_id=owner, system_id=parent, page=page
    )


@router.get("/components/{component_id}", response_class=HTMLResponse)
def component_detail(
    request: Request,
    component_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return _detail(request, user, db, kind="component", item_id=component_id)


@router.post("/components")
def component_create(
    request: Request,
    name: str = Form(""),
    system_id: str = Form(""),
    description: str = Form(""),
    owner_team_id: str = Form(""),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    draft = {
        "name": name,
        "description": description,
        "owner_team_id": owner_team_id,
        "system_id": system_id,
    }
    try:
        create_component(
            db,
            name=name,
            system_id=_optional_id(system_id, "system"),
            description=description or None,
            created_by=user.id,
            owner_team_id=_optional_id(owner_team_id, "owner team"),
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _listing(request, user, db, kind="components", draft=draft, error=str(exc))
    return RedirectResponse("/components", status_code=303)


@router.post("/components/{component_id}/edit")
def component_edit(
    request: Request,
    component_id: int,
    name: str = Form(""),
    system_id: str = Form(""),
    description: str = Form(""),
    owner_team_id: str = Form(""),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    comp = db.get(Component, component_id)
    if comp is None:
        raise HTTPException(404, "Not found")
    draft = {
        "name": name,
        "description": description,
        "owner_team_id": owner_team_id,
        "system_id": system_id,
    }
    try:
        update_component(
            db,
            comp,
            name=name,
            description=description or None,
            system_id=_optional_id(system_id, "system"),
            owner_team_id=_optional_id(owner_team_id, "owner team"),
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _detail(
            request, user, db, kind="component", item_id=component_id, draft=draft, error=str(exc)
        )
    return RedirectResponse(f"/components/{component_id}", status_code=303)


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
