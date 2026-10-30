from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_role, require_user
from app.db import get_db
from app.models import Role, User
from app.services import postmortems as pm_svc
from app.services.core_forms import validation_response
from app.services.incident_access import get_visible_incident
from app.templating import templates

router = APIRouter()


@router.get("/postmortems", response_class=HTMLResponse)
def index(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "postmortems_index.html",
        {
            "current_user": user,
            "incidents": pm_svc.list_incidents_with_postmortem(db, user=user),
            "to_write": pm_svc.list_closed_incidents_without_postmortem(db, user=user),
        },
    )


@router.post("/incidents/{incident_id}/postmortem/generate")
def generate(
    request: Request,
    incident_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = get_visible_incident(db, incident_id, user)
    pm_svc.generate(db, inc, by_user=user.id)
    db.commit()
    return RedirectResponse(f"/incidents/{incident_id}/postmortem", status_code=303)


@router.get("/incidents/{incident_id}/postmortem", response_class=HTMLResponse)
def view(
    request: Request,
    incident_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    inc = get_visible_incident(db, incident_id, user)
    try:
        # Commit unconditionally: maybe_pull writes a throttle timestamp even when the
        # fetch fails/returns nothing, and it MUST persist (get_db has no commit-on-teardown,
        # so without this every page view would re-hit Google until the notes are ready —
        # or forever for a connection that hasn't re-consented to drive.readonly).
        pm_svc.maybe_pull_gemini_notes(db, inc)
        db.commit()
    except Exception:  # noqa: BLE001 — the postmortem page must render regardless
        db.rollback()
    return templates.TemplateResponse(
        request,
        "postmortem.html",
        {"current_user": user, "i": inc, "postmortem": pm_svc.get_postmortem(db, inc)},
    )


@router.post("/incidents/{incident_id}/postmortem")
def save(
    request: Request,
    incident_id: int,
    body: str = Form(""),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = get_visible_incident(db, incident_id, user)
    pm = pm_svc.get_postmortem(db, inc)
    if pm is None:
        request.session["flash"] = "Generate the postmortem first."
        return RedirectResponse(f"/incidents/{incident_id}/postmortem", status_code=303)
    try:
        pm_svc.update_body(db, pm, body=body, by_user=user.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        response = templates.TemplateResponse(
            request, "postmortem.html", {"current_user": user, "i": inc, "postmortem": pm}
        )
        return validation_response(request, response, str(exc), {"body": [body]})
    return RedirectResponse(f"/incidents/{incident_id}/postmortem", status_code=303)


@router.post("/incidents/{incident_id}/postmortem/regenerate")
def regenerate(
    request: Request,
    incident_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = get_visible_incident(db, incident_id, user)
    pm = pm_svc.get_postmortem(db, inc)
    if pm is not None:
        pm_svc.regenerate(db, pm, inc, by_user=user.id)
        db.commit()
    return RedirectResponse(f"/incidents/{incident_id}/postmortem", status_code=303)


@router.get("/incidents/{incident_id}/postmortem.md", response_class=PlainTextResponse)
def download(
    request: Request,
    incident_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    inc = get_visible_incident(db, incident_id, user)
    pm = pm_svc.get_postmortem(db, inc)
    if pm is None:
        return HTMLResponse("Not found", status_code=404)
    return PlainTextResponse(
        pm.body,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="incident-{incident_id}-postmortem.md"'
        },
    )
