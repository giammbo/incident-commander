from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role, require_user
from app.db import get_db
from app.models import (
    GoogleConnection,
    Incident,
    IncidentStatus,
    Role,
    Service,
    SeverityLevel,
    SlackConnection,
    User,
)
from app.services.catalog import default_severity_level_id
from app.services.incidents import close_incident, create_incident, list_incidents, update_incident
from app.settings_store import google_settings, slack_settings
from app.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def history(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    slack = slack_settings(db)
    slack_connections = (
        list(db.scalars(select(SlackConnection).order_by(SlackConnection.id)))
        if (slack.enabled and slack.client_id and slack.client_secret)
        else []
    )
    goog = google_settings(db)
    google_connections = (
        list(db.scalars(select(GoogleConnection).order_by(GoogleConnection.id)))
        if (goog.enabled and goog.client_id and goog.client_secret)
        else []
    )
    severity_levels = list(db.scalars(select(SeverityLevel).order_by(SeverityLevel.rank)))
    services = list(db.scalars(select(Service).order_by(Service.name)))
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "current_user": user,
            "incidents": list_incidents(db),
            "severity_levels": severity_levels,
            "default_severity_level_id": default_severity_level_id(db),
            "services": services,
            "slack_connections": slack_connections,
            "google_connections": google_connections,
        },
    )


@router.post("/incidents")
def create(
    request: Request,
    title: str = Form(...),
    severity_level_id: int = Form(...),
    is_private: bool = Form(False),
    description: str | None = Form(None),
    service_ids: list[int] = Form(default=[]),
    slack_connection_id: int | None = Form(None),
    google_connection_id: int | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = create_incident(
        db,
        title=title,
        severity_level_id=severity_level_id,
        is_private=is_private,
        created_by=user.id,
        description=description,
        service_ids=service_ids,
        slack_connection_id=slack_connection_id,
        google_connection_id=google_connection_id,
    )
    if google_connection_id:
        from app.services.incident_actions import open_incident_google

        gconn = db.scalar(
            select(GoogleConnection).where(GoogleConnection.id == google_connection_id)
        )
        if gconn is not None:
            open_incident_google(db, inc, gconn)
    if slack_connection_id:
        from app.services.incident_actions import open_incident_slack

        conn = db.scalar(select(SlackConnection).where(SlackConnection.id == slack_connection_id))
        if conn is not None:
            open_incident_slack(db, inc, conn)
    db.commit()
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request, "partials/incident_row.html", {"current_user": user, "i": inc}
        )
    return RedirectResponse("/", status_code=303)


@router.get("/incidents/{incident_id}", response_class=HTMLResponse)
def detail(
    request: Request,
    incident_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    inc = db.scalar(select(Incident).where(Incident.id == incident_id))
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    severity_levels = list(db.scalars(select(SeverityLevel).order_by(SeverityLevel.rank)))
    services = list(db.scalars(select(Service).order_by(Service.name)))
    return templates.TemplateResponse(
        request,
        "incident_detail.html",
        {"current_user": user, "i": inc, "severity_levels": severity_levels, "services": services},
    )


@router.post("/incidents/{incident_id}/edit")
def edit(
    request: Request,
    incident_id: int,
    title: str = Form(...),
    description: str | None = Form(None),
    severity_level_id: int = Form(...),
    service_ids: list[int] = Form(default=[]),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = db.scalar(select(Incident).where(Incident.id == incident_id))
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    update_incident(
        db,
        inc,
        title=title,
        description=description,
        severity_level_id=severity_level_id,
        service_ids=service_ids,
    )
    if inc.slack_connection_id and inc.slack_channel_id:
        from app.services.incident_actions import update_incident_slack

        conn = db.scalar(
            select(SlackConnection).where(SlackConnection.id == inc.slack_connection_id)
        )
        if conn is not None:
            update_incident_slack(db, inc, conn)
    db.commit()
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@router.post("/incidents/{incident_id}/close")
def close(
    request: Request,
    incident_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = db.scalar(select(Incident).where(Incident.id == incident_id))
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    if inc.status == IncidentStatus.open:
        close_incident(db, inc, closed_by=user.id)
        if inc.slack_connection_id:
            from app.services.incident_actions import close_incident_slack

            conn = db.scalar(
                select(SlackConnection).where(SlackConnection.id == inc.slack_connection_id)
            )
            if conn is not None:
                close_incident_slack(db, inc, conn)
        db.commit()
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request, "partials/incident_row.html", {"current_user": user, "i": inc}
        )
    return RedirectResponse("/", status_code=303)
