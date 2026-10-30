from datetime import date
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role, require_user
from app.config import get_settings
from app.db import get_db
from app.models import (
    Component,
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
from app.services.core_forms import custom_values, validation_response
from app.services.incident_access import get_visible_incident
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

# Tabs on the incident detail page (CSS radio tabs, no JS). POST routes redirect
# back to the tab the form lives in via `?tab=`; anything off this list is ignored.
INCIDENT_TABS = ("overview", "roles", "timeline", "followups", "alerts")


def _board_row_response(request: Request, user: User, db: Session, inc: Incident):
    """Refresh filtered membership through a same-origin board destination."""
    current = urlsplit(request.headers.get("HX-Current-URL", ""))
    if current.path == "/" and current.query:
        query = parse_qs(current.query)
        retained = {
            key: query[key][0] for key in ("q", "status", "severity", "system") if key in query
        }
        if retained:
            return HTMLResponse("", headers={"HX-Redirect": "/?" + urlencode(retained)})
    incidents = list_incidents(db, user=user)
    return templates.TemplateResponse(
        request,
        "partials/incident_row_oob.html",
        {
            "current_user": user,
            "today": date.today(),
            "i": inc,
            "open_count": sum(1 for item in incidents if not item.is_closed),
            "total_count": len(incidents),
        },
    )


def incident_redirect(incident_id: int, return_tab: str | None = None) -> RedirectResponse:
    url = f"/incidents/{incident_id}"
    if return_tab in INCIDENT_TABS:
        url += f"?tab={return_tab}"
    return RedirectResponse(url, status_code=303)


@router.get("/", response_class=HTMLResponse)
def history(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    filters = {
        key: request.query_params.get(key, "") for key in ("q", "status", "severity", "system")
    }
    incidents = list_incidents(db, user=user)
    if filters["q"]:
        incidents = [i for i in incidents if filters["q"].casefold() in i.title.casefold()]
    for key, attr in (
        ("status", "status_id"),
        ("severity", "severity_level_id"),
        ("system", "system_id"),
    ):
        if filters[key]:
            incidents = [i for i in incidents if str(getattr(i, attr)) == filters[key]]
    slack = slack_settings(db)
    slack_connections = (
        list(db.scalars(select(SlackConnection).order_by(SlackConnection.id)))
        if (slack.enabled and slack.client_id and slack.client_secret)
        else []
    )
    goog = google_settings(db)
    google_configured = bool(goog.enabled and goog.service_account_json and goog.impersonate_email)
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
            "incidents": incidents,
            "filters": filters,
            "severity_levels": severity_levels,
            "default_severity_level_id": _initial_severity_id,
            "default_incident_type_id": _default_type_id,
            "status_levels": status_levels,
            "systems": systems,
            "slack_connections": slack_connections,
            "google_configured": google_configured,
            "incident_types": incident_types,
            "fields": fields,
            "values": {},
        },
    )


@router.post("/incidents")
async def create(
    request: Request,
    title: str = Form(""),
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
    form = await request.form()
    draft = {k: form.getlist(k) for k in form}
    raw = {int(k[3:]): form.getlist(k) for k in form if k.startswith("cf_") and k[3:].isdigit()}
    try:
        custom_values(db, incident_type_id or default_incident_type_id(db), raw)
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
        response = history(request, user, db)
        from app.services.custom_fields import fields_for_type

        response.context["fields"] = fields_for_type(
            db, incident_type_id or default_incident_type_id(db)
        )
        response.context["draft_components"] = (
            list(db.scalars(select(Component).where(Component.system_id == system_id)))
            if system_id
            else []
        )
        return validation_response(request, response, str(exc), draft)
    from app.services import providers
    from app.services.custom_fields import set_incident_values

    set_incident_values(db, inc, raw)

    vkey = providers.parse_video_choice(video)
    if vkey:
        vp = providers.VIDEO_PROVIDERS[vkey]
        vp.create(db, inc, connection=None)
    if slack_connection_id:
        conn = db.scalar(select(SlackConnection).where(SlackConnection.id == slack_connection_id))
        if conn is not None:
            providers.CHAT_PROVIDERS["slack"].open_room(db, inc, connection=conn)
    automation.run_rules(db, trigger="incident.opened", incident=inc, by_user=user.id)
    db.commit()
    webhooks.notify(db, inc, "opened", base_url=get_settings().base_url)
    if request.headers.get("HX-Request"):
        return HTMLResponse("", headers={"HX-Redirect": f"/incidents/{inc.id}"})
    return incident_redirect(inc.id)


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
    return_tab: str | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    from app.services.custom_fields import set_incident_values

    inc = get_visible_incident(db, incident_id, user)
    form = await request.form()
    raw = {int(k[3:]): form.getlist(k) for k in form if k.startswith("cf_") and k[3:].isdigit()}
    try:
        custom_values(db, inc.incident_type_id, raw)
    except ValueError as exc:
        return validation_response(
            request,
            detail(request, incident_id, "overview", user, db),
            str(exc),
            {k: form.getlist(k) for k in form},
        )
    set_incident_values(db, inc, raw)
    db.commit()
    return incident_redirect(incident_id, return_tab)


@router.get("/incidents/{incident_id}", response_class=HTMLResponse)
def detail(
    request: Request,
    incident_id: int,
    tab: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    inc = get_visible_incident(db, incident_id, user)
    severity_levels = list(db.scalars(select(SeverityLevel).order_by(SeverityLevel.rank)))
    systems = list(db.scalars(select(System).order_by(System.name)))
    slack = slack_settings(db)
    slack_connections = (
        list(db.scalars(select(SlackConnection).order_by(SlackConnection.id)))
        if (slack.enabled and slack.client_id and slack.client_secret)
        else []
    )
    goog = google_settings(db)
    google_configured = bool(goog.enabled and goog.service_account_json and goog.impersonate_email)
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
    active_tab = tab if tab in INCIDENT_TABS else "overview"
    if active_tab == "alerts" and not inc.alerts:
        active_tab = "overview"
    from app.models import Escalation, EscalationStatus

    active_pages = list(
        db.scalars(
            select(Escalation)
            .where(
                Escalation.incident_id == inc.id,
                Escalation.source == "manual",
                Escalation.status.in_(
                    (
                        EscalationStatus.triggered,
                        EscalationStatus.acknowledged,
                        EscalationStatus.exhausted,
                    )
                ),
            )
            .order_by(Escalation.created_at.desc())
        )
    )
    from app.models import User as _User
    from app.routers.oncall import _paged_info

    paged_info = _paged_info(
        db, user, active_pages, {u.id: u.name for u in db.scalars(select(_User))}
    )
    return templates.TemplateResponse(
        request,
        "incident_detail.html",
        {
            "current_user": user,
            "today": date.today(),
            "i": inc,
            "severity_levels": severity_levels,
            "status_levels": status_levels,
            "systems": systems,
            "slack_connections": slack_connections,
            "google_configured": google_configured,
            "role_types": role_types,
            "role_assignees": role_assignees,
            "assignable_users": assignable_users,
            "follow_ups": follow_ups,
            "followups_open": followups_open,
            "events": events,
            "incident_types": incident_types,
            "custom_fields": custom_fields,
            "custom_values": custom_values,
            "active_tab": active_tab,
            "active_pages": active_pages,
            "paged_names": {k: v["paged"] for k, v in paged_info.items()},
        },
    )


@router.post("/incidents/{incident_id}/edit")
async def edit(
    request: Request,
    incident_id: int,
    title: str = Form(""),
    description: str | None = Form(None),
    severity_level_id: int = Form(...),
    system_id: int | None = Form(None),
    component_ids: list[int] = Form(default=[]),
    incident_type_id: int | None = Form(None),
    return_tab: str | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = get_visible_incident(db, incident_id, user)
    form = await request.form()
    raw = {int(k[3:]): form.getlist(k) for k in form if k.startswith("cf_") and k[3:].isdigit()}
    try:
        custom_values(db, incident_type_id, raw)
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
        from app.services.custom_fields import fields_for_type

        response = detail(request, incident_id, "overview", user, db)
        response.context["edit_fields"] = fields_for_type(db, incident_type_id)
        response.context["edit_components"] = (
            list(db.scalars(select(Component).where(Component.system_id == system_id)))
            if system_id
            else []
        )
        return validation_response(
            request, response, str(exc), {key: form.getlist(key) for key in form}
        )
    from app.services.custom_fields import set_incident_values

    set_incident_values(db, inc, raw)
    if inc.slack_connection_id and inc.slack_channel_id:
        from app.services import providers

        conn = db.scalar(
            select(SlackConnection).where(SlackConnection.id == inc.slack_connection_id)
        )
        if conn is not None:
            providers.CHAT_PROVIDERS["slack"].post_update(db, inc, connection=conn)
    db.commit()
    webhooks.notify(db, inc, "updated", base_url=get_settings().base_url)
    return incident_redirect(incident_id, return_tab)


@router.post("/incidents/{incident_id}/add-meet")
def add_meet(
    request: Request,
    incident_id: int,
    video: str = Form(...),
    return_tab: str | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    from app.services import providers

    inc = get_visible_incident(db, incident_id, user)
    if inc.is_closed:
        request.session["flash"] = "Cannot add a video bridge to a closed incident."
        return incident_redirect(incident_id, return_tab)
    if inc.meet_url:
        request.session["flash"] = "This incident already has a video bridge."
        return incident_redirect(incident_id, return_tab)
    vkey = providers.parse_video_choice(video)
    vp = providers.VIDEO_PROVIDERS.get(vkey) if vkey else None
    if vp is None:
        request.session["flash"] = "Unknown video provider."
        return incident_redirect(incident_id, return_tab)
    vp.create(db, inc, connection=None)
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
    return incident_redirect(incident_id, return_tab)


@router.post("/incidents/{incident_id}/open-slack")
def open_slack(
    request: Request,
    incident_id: int,
    slack_connection_id: int = Form(...),
    return_tab: str | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = get_visible_incident(db, incident_id, user)
    if inc.is_closed:
        request.session["flash"] = "Cannot open a channel for a closed incident."
        return incident_redirect(incident_id, return_tab)
    if inc.slack_channel_id:
        request.session["flash"] = "This incident already has a Slack channel."
        return incident_redirect(incident_id, return_tab)
    sconn = db.scalar(select(SlackConnection).where(SlackConnection.id == slack_connection_id))
    if sconn is None:
        request.session["flash"] = "Unknown Slack connection."
        return incident_redirect(incident_id, return_tab)
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
    return incident_redirect(incident_id, return_tab)


@router.post("/incidents/{incident_id}/close")
def close(
    request: Request,
    incident_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = get_visible_incident(db, incident_id, user)
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
        return _board_row_response(request, user, db, inc)
    return RedirectResponse("/", status_code=303)


@router.post("/incidents/{incident_id}/roles")
def set_roles(
    request: Request,
    incident_id: int,
    role_type_id: int = Form(...),
    user_ids: list[int] = Form(default=[]),
    return_tab: str | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = get_visible_incident(db, incident_id, user)
    from app.services.incidents import set_incident_role_assignees

    try:
        set_incident_role_assignees(
            db, inc, role_type_id=role_type_id, user_ids=user_ids, by_user=user.id
        )
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
        return incident_redirect(incident_id, return_tab)
    if inc.slack_connection_id and inc.slack_channel_id:
        from app.services import providers

        conn = db.scalar(
            select(SlackConnection).where(SlackConnection.id == inc.slack_connection_id)
        )
        if conn is not None:
            providers.CHAT_PROVIDERS["slack"].post_update(db, inc, connection=conn)
    db.commit()
    webhooks.notify(db, inc, "updated", base_url=get_settings().base_url)
    return incident_redirect(incident_id, return_tab)


@router.post("/incidents/{incident_id}/notes")
def add_note_route(
    request: Request,
    incident_id: int,
    body: str = Form(...),
    return_tab: str | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = get_visible_incident(db, incident_id, user)
    from app.services.timeline import add_note

    try:
        add_note(db, inc, body=body, created_by=user.id)
    except ValueError as exc:
        db.rollback()
        return validation_response(
            request,
            detail(request, incident_id, "timeline", user, db),
            str(exc),
            {"body": [body], "return_tab": [return_tab or "timeline"]},
        )
    db.commit()
    return incident_redirect(incident_id, return_tab)


@router.post("/incidents/{incident_id}/events/{event_id}/pin")
def pin_event_route(
    request: Request,
    incident_id: int,
    event_id: int,
    return_tab: str | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    from app.models import IncidentEvent
    from app.services.timeline import toggle_pin

    get_visible_incident(db, incident_id, user)
    ev = db.scalar(select(IncidentEvent).where(IncidentEvent.id == event_id))
    if ev is None or ev.incident_id != incident_id:
        return HTMLResponse("Not found", status_code=404)
    toggle_pin(db, event_id)
    db.commit()
    return incident_redirect(incident_id, return_tab)


@router.post("/incidents/{incident_id}/events/{event_id}/delete")
def delete_event_route(
    request: Request,
    incident_id: int,
    event_id: int,
    return_tab: str | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    from app.models import IncidentEvent
    from app.services.timeline import delete_note

    get_visible_incident(db, incident_id, user)
    ev = db.scalar(select(IncidentEvent).where(IncidentEvent.id == event_id))
    if ev is None or ev.incident_id != incident_id:
        return HTMLResponse("Not found", status_code=404)
    try:
        delete_note(db, event_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return incident_redirect(incident_id, return_tab)


@router.post("/incidents/{incident_id}/updates")
def post_update_route(
    request: Request,
    incident_id: int,
    message: str = Form(...),
    status_id: int | None = Form(None),
    return_tab: str | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = get_visible_incident(db, incident_id, user)
    from app.services.updates import post_update

    try:
        kind = post_update(db, inc, message=message, status_id=status_id, by_user=user.id)
    except ValueError as exc:
        db.rollback()
        return validation_response(
            request,
            detail(request, incident_id, return_tab, user, db),
            str(exc),
            {
                "message": [message],
                "status_id": [str(status_id or "")],
                "return_tab": [return_tab or "overview"],
            },
        )
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
    return incident_redirect(incident_id, return_tab)


@router.post("/incidents/{incident_id}/status")
def change_status(
    request: Request,
    incident_id: int,
    status_id: int = Form(...),
    return_tab: str | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    inc = get_visible_incident(db, incident_id, user)
    try:
        kind = set_incident_status(db, inc, status_id=status_id, by_user=user.id)
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
        return incident_redirect(incident_id, return_tab)
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
    return incident_redirect(incident_id, return_tab)
