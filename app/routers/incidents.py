from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role, require_user
from app.config import get_settings
from app.db import get_db
from app.models import (
    Component,
    GoogleConnection,
    Incident,
    IncidentType,
    Role,
    SeverityLevel,
    SlackConnection,
    StatusLevel,
    System,
    User,
)
from app.services import automation, webhooks
from app.services import postmortems as pm_svc
from app.services.catalog import default_severity_level_id
from app.services.incident_types import default_incident_type_id, list_incident_types
from app.services.incidents import (
    close_incident,
    create_incident,
    list_incidents,
    set_incident_status,
    update_incident,
)
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
    status_levels = list(db.scalars(select(StatusLevel).order_by(StatusLevel.rank)))
    systems = list(db.scalars(select(System).order_by(System.name)))
    incident_types = list_incident_types(db)
    _default_type_id = default_incident_type_id(db)
    _default_type = db.get(IncidentType, _default_type_id) if _default_type_id else None
    # Pre-select the severity the default type implies, so the form's initial render
    # matches what the Type-select HTMX would produce on change.
    _initial_severity_id = (
        _default_type.default_severity_level_id if _default_type else None
    ) or default_severity_level_id(db)
    from app.services.custom_fields import fields_for_type

    fields = fields_for_type(db, _default_type_id)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "current_user": user,
            "incidents": list_incidents(db),
            "severity_levels": severity_levels,
            "default_severity_level_id": _initial_severity_id,
            "default_incident_type_id": _default_type_id,
            "status_levels": status_levels,
            "systems": systems,
            "slack_connections": slack_connections,
            "google_connections": google_connections,
            "incident_types": incident_types,
            "fields": fields,
            "values": {},
        },
    )


@router.post("/incidents")
async def create(
    request: Request,
    title: str = Form(...),
    severity_level_id: int | None = Form(None),
    is_private: bool = Form(False),
    description: str | None = Form(None),
    system_id: int | None = Form(None),
    component_ids: list[int] = Form(default=[]),
    slack_connection_id: int | None = Form(None),
    video: str = Form(""),
    status_id: int | None = Form(None),
    incident_type_id: int | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    try:
        inc = create_incident(
            db,
            title=title,
            severity_level_id=severity_level_id,
            is_private=is_private,
            created_by=user.id,
            description=description,
            system_id=system_id,
            component_ids=component_ids,
            slack_connection_id=slack_connection_id,
            status_id=status_id,
            incident_type_id=incident_type_id,
        )
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
        return RedirectResponse("/", status_code=303)
    from app.services import providers
    from app.services.custom_fields import set_incident_values

    form = await request.form()
    raw = {int(k[3:]): form.getlist(k) for k in form if k.startswith("cf_") and k[3:].isdigit()}
    missing = set_incident_values(db, inc, raw)
    if missing:
        request.session["flash"] = "Missing required: " + ", ".join(missing)

    vkey, vconn_id = providers.parse_video_choice(video)
    if vkey:
        vp = providers.VIDEO_PROVIDERS[vkey]
        gconn = None
        if vp.needs_connection and vconn_id is not None:
            gconn = db.scalar(select(GoogleConnection).where(GoogleConnection.id == vconn_id))
        if not vp.needs_connection or gconn is not None:
            vp.create(db, inc, connection=gconn)
    if slack_connection_id:
        conn = db.scalar(select(SlackConnection).where(SlackConnection.id == slack_connection_id))
        if conn is not None:
            providers.CHAT_PROVIDERS["slack"].open_room(db, inc, connection=conn)
    automation.run_rules(db, trigger="incident.opened", incident=inc, by_user=user.id)
    db.commit()
    webhooks.notify(db, inc, "opened", base_url=get_settings().base_url)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request, "partials/incident_row.html", {"current_user": user, "i": inc}
        )
    return RedirectResponse("/", status_code=303)


@router.get("/incidents/component-options", response_class=HTMLResponse)
def component_options(
    request: Request,
    system_id: int | None = None,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    components = (
        list(
            db.scalars(
                select(Component).where(Component.system_id == system_id).order_by(Component.name)
            )
        )
        if system_id
        else []
    )
    return templates.TemplateResponse(
        request, "partials/component_options.html", {"components": components, "selected_ids": []}
    )


@router.get("/incidents/type-options", response_class=HTMLResponse)
def type_options(
    request: Request,
    incident_type_id: int | None = None,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    from app.services.custom_fields import fields_for_type

    severity_levels = list(db.scalars(select(SeverityLevel).order_by(SeverityLevel.rank)))
    t = db.get(IncidentType, incident_type_id) if incident_type_id else None
    selected_id = (t.default_severity_level_id if t else None) or default_severity_level_id(db)
    fields = fields_for_type(db, incident_type_id)
    return templates.TemplateResponse(
        request,
        "partials/type_options.html",
        {
            "severity_levels": severity_levels,
            "selected_id": selected_id,
            "fields": fields,
            "values": {},
        },
    )


@router.post("/incidents/{incident_id}/custom-fields")
async def edit_custom_fields(
    request: Request,
    incident_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    from app.services.custom_fields import set_incident_values

    inc = db.scalar(select(Incident).where(Incident.id == incident_id))
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    form = await request.form()
    raw = {int(k[3:]): form.getlist(k) for k in form if k.startswith("cf_") and k[3:].isdigit()}
    missing = set_incident_values(db, inc, raw)
    if missing:
        request.session["flash"] = "Missing required: " + ", ".join(missing)
    db.commit()
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


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
    systems = list(db.scalars(select(System).order_by(System.name)))
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
    status_levels = list(db.scalars(select(StatusLevel).order_by(StatusLevel.rank)))
    from app.services.followups import list_followups, open_count
    from app.services.roles import assignments_by_role, list_incident_role_types
    from app.services.timeline import list_events

    role_types = list_incident_role_types(db)
    role_assignees = assignments_by_role(inc)
    assignable_users = list(
        db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.name))
    )
    events = list_events(inc)
    follow_ups = list_followups(inc)
    followups_open = open_count(inc)
    incident_types = list_incident_types(db)
    from app.services.custom_fields import fields_for_type, values_for_incident

    custom_fields = fields_for_type(db, inc.incident_type_id)
    custom_values = values_for_incident(inc)
    return templates.TemplateResponse(
        request,
        "incident_detail.html",
        {
            "current_user": user,
            "i": inc,
            "severity_levels": severity_levels,
            "status_levels": status_levels,
            "systems": systems,
            "slack_connections": slack_connections,
            "google_connections": google_connections,
            "role_types": role_types,
            "role_assignees": role_assignees,
            "assignable_users": assignable_users,
            "follow_ups": follow_ups,
            "followups_open": followups_open,
            "events": events,
            "incident_types": incident_types,
            "custom_fields": custom_fields,
            "custom_values": custom_values,
        },
    )


@router.post("/incidents/{incident_id}/edit")
def edit(
    request: Request,
    incident_id: int,
    title: str = Form(...),
    description: str | None = Form(None),
    severity_level_id: int = Form(...),
    system_id: int | None = Form(None),
    component_ids: list[int] = Form(default=[]),
    incident_type_id: int | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = db.scalar(select(Incident).where(Incident.id == incident_id))
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    try:
        update_incident(
            db,
            inc,
            title=title,
            description=description,
            severity_level_id=severity_level_id,
            system_id=system_id,
            component_ids=component_ids,
            incident_type_id=incident_type_id,
        )
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)
    if inc.slack_connection_id and inc.slack_channel_id:
        from app.services import providers

        conn = db.scalar(
            select(SlackConnection).where(SlackConnection.id == inc.slack_connection_id)
        )
        if conn is not None:
            providers.CHAT_PROVIDERS["slack"].post_update(db, inc, connection=conn)
    db.commit()
    webhooks.notify(db, inc, "updated", base_url=get_settings().base_url)
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@router.post("/incidents/{incident_id}/add-meet")
def add_meet(
    request: Request,
    incident_id: int,
    video: str = Form(...),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    from app.services import providers

    inc = db.scalar(select(Incident).where(Incident.id == incident_id))
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    if inc.is_closed:
        request.session["flash"] = "Cannot add a video bridge to a closed incident."
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)
    if inc.meet_url:
        request.session["flash"] = "This incident already has a video bridge."
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)
    vkey, vconn_id = providers.parse_video_choice(video)
    vp = providers.VIDEO_PROVIDERS.get(vkey) if vkey else None
    if vp is None:
        request.session["flash"] = "Unknown video provider."
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)
    gconn = None
    if vp.needs_connection:
        gconn = (
            db.scalar(select(GoogleConnection).where(GoogleConnection.id == vconn_id))
            if vconn_id
            else None
        )
        if gconn is None:
            request.session["flash"] = "Pick a Google account for Meet."
            return RedirectResponse(f"/incidents/{incident_id}", status_code=303)
    vp.create(db, inc, connection=gconn)
    if inc.meet_url and inc.slack_channel_id:
        sconn = db.scalar(
            select(SlackConnection).where(SlackConnection.id == inc.slack_connection_id)
        )
        if sconn is not None:
            providers.CHAT_PROVIDERS["slack"].announce_video(db, inc, connection=sconn)
    if not inc.meet_url:
        request.session["flash"] = (
            "Video bridge creation failed — check the connection and try again."
        )
    if inc.meet_url:
        from app.services.timeline import log_event

        log_event(db, inc, entry_type="video_added", body="Video bridge added.", created_by=user.id)
    db.commit()
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@router.post("/incidents/{incident_id}/open-slack")
def open_slack(
    request: Request,
    incident_id: int,
    slack_connection_id: int = Form(...),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = db.scalar(select(Incident).where(Incident.id == incident_id))
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    if inc.is_closed:
        request.session["flash"] = "Cannot open a channel for a closed incident."
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)
    if inc.slack_channel_id:
        request.session["flash"] = "This incident already has a Slack channel."
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)
    sconn = db.scalar(select(SlackConnection).where(SlackConnection.id == slack_connection_id))
    if sconn is None:
        request.session["flash"] = "Unknown Slack connection."
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)
    from app.services import providers

    inc.slack_connection_id = sconn.id
    providers.CHAT_PROVIDERS["slack"].open_room(db, inc, connection=sconn)
    if not inc.slack_channel_id:
        request.session["flash"] = (
            "Slack channel creation failed — check the workspace and try again."
        )
    if inc.slack_channel_id:
        from app.services.timeline import log_event

        log_event(
            db,
            inc,
            entry_type="slack_opened",
            body=f"Slack channel opened: {inc.slack_channel_name or inc.slack_channel_id}.",
            created_by=user.id,
        )
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
    if not inc.is_closed:
        close_incident(db, inc, closed_by=user.id)
        if inc.slack_connection_id:
            from app.services import providers

            conn = db.scalar(
                select(SlackConnection).where(SlackConnection.id == inc.slack_connection_id)
            )
            if conn is not None:
                providers.CHAT_PROVIDERS["slack"].post_closed(db, inc, connection=conn)
        webhooks.notify(db, inc, "closed", base_url=get_settings().base_url)
        try:
            pm_svc.maybe_pull_gemini_notes(db, inc)
        except Exception:  # noqa: BLE001 — closing must not fail on notes
            pass
        db.commit()
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request, "partials/incident_row.html", {"current_user": user, "i": inc}
        )
    return RedirectResponse("/", status_code=303)


@router.post("/incidents/{incident_id}/roles")
def set_roles(
    request: Request,
    incident_id: int,
    role_type_id: int = Form(...),
    user_ids: list[int] = Form(default=[]),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = db.scalar(select(Incident).where(Incident.id == incident_id))
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    from app.services.incidents import set_incident_role_assignees

    try:
        set_incident_role_assignees(
            db, inc, role_type_id=role_type_id, user_ids=user_ids, by_user=user.id
        )
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)
    if inc.slack_connection_id and inc.slack_channel_id:
        from app.services import providers

        conn = db.scalar(
            select(SlackConnection).where(SlackConnection.id == inc.slack_connection_id)
        )
        if conn is not None:
            providers.CHAT_PROVIDERS["slack"].post_update(db, inc, connection=conn)
    db.commit()
    webhooks.notify(db, inc, "updated", base_url=get_settings().base_url)
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@router.post("/incidents/{incident_id}/notes")
def add_note_route(
    request: Request,
    incident_id: int,
    body: str = Form(...),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = db.scalar(select(Incident).where(Incident.id == incident_id))
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    from app.services.timeline import add_note

    try:
        add_note(db, inc, body=body, created_by=user.id)
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)
    db.commit()
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@router.post("/incidents/{incident_id}/events/{event_id}/pin")
def pin_event_route(
    request: Request,
    incident_id: int,
    event_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    from app.models import IncidentEvent
    from app.services.timeline import toggle_pin

    ev = db.scalar(select(IncidentEvent).where(IncidentEvent.id == event_id))
    if ev is None or ev.incident_id != incident_id:
        return HTMLResponse("Not found", status_code=404)
    toggle_pin(db, event_id)
    db.commit()
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@router.post("/incidents/{incident_id}/events/{event_id}/delete")
def delete_event_route(
    request: Request,
    incident_id: int,
    event_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    from app.models import IncidentEvent
    from app.services.timeline import delete_note

    ev = db.scalar(select(IncidentEvent).where(IncidentEvent.id == event_id))
    if ev is None or ev.incident_id != incident_id:
        return HTMLResponse("Not found", status_code=404)
    try:
        delete_note(db, event_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@router.post("/incidents/{incident_id}/updates")
def post_update_route(
    request: Request,
    incident_id: int,
    message: str = Form(...),
    status_id: int | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = db.scalar(select(Incident).where(Incident.id == incident_id))
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    from app.services.updates import post_update

    try:
        kind = post_update(db, inc, message=message, status_id=status_id, by_user=user.id)
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)
    if inc.slack_connection_id and inc.slack_channel_id:
        from app.services import providers

        conn = db.scalar(
            select(SlackConnection).where(SlackConnection.id == inc.slack_connection_id)
        )
        if conn is not None:
            providers.CHAT_PROVIDERS["slack"].post_announcement(
                db, inc, connection=conn, text=message
            )
            # If the update also moved the status, announce the lifecycle change too
            # (mirrors the /status route), so a close/reopen via update is still visible.
            if kind == "closed":
                providers.CHAT_PROVIDERS["slack"].post_closed(db, inc, connection=conn)
            elif kind is not None:
                providers.CHAT_PROVIDERS["slack"].post_update(db, inc, connection=conn)
    db.commit()
    webhooks.notify(db, inc, "update", base_url=get_settings().base_url, message=message)
    if kind is not None:
        webhooks.notify(
            db, inc, "closed" if kind == "closed" else "updated", base_url=get_settings().base_url
        )
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@router.post("/incidents/{incident_id}/status")
def change_status(
    request: Request,
    incident_id: int,
    status_id: int = Form(...),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = db.scalar(select(Incident).where(Incident.id == incident_id))
    if inc is None:
        return HTMLResponse("Not found", status_code=404)
    try:
        kind = set_incident_status(db, inc, status_id=status_id, by_user=user.id)
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)
    from app.services import providers

    if inc.slack_connection_id and inc.slack_channel_id:
        conn = db.scalar(
            select(SlackConnection).where(SlackConnection.id == inc.slack_connection_id)
        )
        if conn is not None:
            if kind == "closed":
                providers.CHAT_PROVIDERS["slack"].post_closed(db, inc, connection=conn)
            else:
                providers.CHAT_PROVIDERS["slack"].post_update(db, inc, connection=conn)
    # Run automation BEFORE the lifecycle webhook (mirrors the declare route), so the
    # webhook payload reflects any automation side-effects on the incident's state.
    automation.run_rules(db, trigger="incident.status_changed", incident=inc, by_user=user.id)
    webhooks.notify(
        db, inc, "closed" if kind == "closed" else "updated", base_url=get_settings().base_url
    )
    db.commit()
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)
