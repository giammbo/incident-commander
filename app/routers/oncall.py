from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.auth import require_role, require_user
from app.db import get_db
from app.models import (
    ROLE_RANK,
    Alert,
    AlertRoute,
    Escalation,
    EscalationLevel,
    EscalationPath,
    EscalationStatus,
    Incident,
    NotificationJob,
    Role,
    Schedule,
    ScheduleOverride,
    ScheduleRotation,
    SeverityLevel,
    Urgency,
    User,
    effective_role,
)
from app.services import oncall_catalog as cat
from app.services import oncall_engine as engine
from app.services import oncall_notify
from app.services import oncall_routes as routes_svc
from app.services.core_forms import custom_values, validation_response
from app.services.custom_fields import fields_for_type
from app.services.incident_access import (
    can_view_incident,
    get_visible_incident,
    visible_incident_condition,
)
from app.services.incident_types import default_incident_type_id
from app.services.oncall_schedule import (
    next_handoff,
    oncall_user_ids,
    rotation_coverage,
    shifts,
)
from app.services.oncall_worker import banner_state
from app.templating import templates

router = APIRouter()

TIMELINE_DAYS = 14


def _active_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.name)))


def _parse_dt(value: str, tzname: str) -> datetime:
    """ISO string -> aware UTC datetime. Naive values (datetime-local) are schedule-local."""
    try:
        d = datetime.fromisoformat(value.strip())
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid date/time: {value!r}") from exc
    if d.tzinfo is None:
        d = d.replace(tzinfo=ZoneInfo(tzname))
    return d.astimezone(UTC)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid date: {value!r}") from exc


def _ids(values: list[str]) -> list[int]:
    try:
        return [int(v) for v in values if str(v).strip()]
    except ValueError as exc:
        raise ValueError("Invalid person selection") from exc


def _windows(days: list[int], start: str, end: str) -> list[dict]:
    if not days and not start and not end:
        return []
    return [{"days": days, "start": start, "end": end}]


def _back(request: Request, url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _draft(request: Request) -> dict:
    return getattr(request.state, "oncall_draft", {})


def _invalid(request: Request, error: ValueError, kind: str, values, render):
    # Keep submitted operational text in this response only, never in the session cookie.
    request.state.oncall_draft = dict(values) | {"kind": kind}
    request.state.oncall_form = values
    request.state.oncall_error = str(error)
    response = render()
    response.status_code = 422
    return response


def _visible_escalation_condition(user: User):
    allowed = select(Incident.id).where(visible_incident_condition(user))
    hidden_alerts = select(Alert.id).where(
        Alert.incident_id.is_not(None), Alert.incident_id.not_in(allowed)
    )
    return and_(
        or_(Escalation.incident_id.is_(None), Escalation.incident_id.in_(allowed)),
        or_(Escalation.alert_id.is_(None), Escalation.alert_id.not_in(hidden_alerts)),
    )


def _get_visible_escalation(db: Session, esc_id: int, user: User):
    esc = db.scalar(
        select(Escalation).where(Escalation.id == esc_id, _visible_escalation_condition(user))
    )
    if esc is None:
        raise HTTPException(status_code=404, detail="Not found")
    return esc


OPEN_STATES = (
    EscalationStatus.triggered,
    EscalationStatus.acknowledged,
    EscalationStatus.exhausted,
)
_ACKABLE = (EscalationStatus.triggered, EscalationStatus.exhausted)


def _open_escalations(db: Session, user: User) -> list[Escalation]:
    return list(
        db.scalars(
            select(Escalation)
            .where(Escalation.status.in_(OPEN_STATES), _visible_escalation_condition(user))
            .order_by(Escalation.created_at.desc())
        )
    )


def _paged_info(
    db: Session, user: User, escs: list[Escalation], names: dict[int, str]
) -> dict[int, dict]:
    """Per escalation: who is being paged now, and whether this user may ack it inline."""
    if not escs:
        return {}
    ids = [e.id for e in escs]
    jobs = db.execute(
        select(NotificationJob.escalation_id, NotificationJob.level, NotificationJob.user_id)
        .where(NotificationJob.escalation_id.in_(ids), NotificationJob.kind == "notify")
        .order_by(NotificationJob.id)
    ).all()
    by_esc: dict[int, list[tuple[int | None, int | None]]] = {}
    for eid, level, uid in jobs:
        by_esc.setdefault(eid, []).append((level, uid))
    is_ic = _is_ic(user)
    out: dict[int, dict] = {}
    for e in escs:
        rows = by_esc.get(e.id, [])
        current = [uid for lvl, uid in rows if (lvl or 0) == e.current_level and uid is not None]
        if not current and e.source == "manual":
            current = list(e.direct_user_ids or [])
        uids = list(dict.fromkeys(current))
        out[e.id] = {
            "paged": [names.get(u, f"#{u}") for u in uids],
            "can_ack": e.status in _ACKABLE
            and (is_ic or any(uid == user.id for _lvl, uid in rows)),
        }
    return out


@router.get("/oncall", response_class=HTMLResponse)
def oncall_now(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    now = datetime.now(UTC)
    names = {u.id: u.name for u in db.scalars(select(User))}
    cards = []
    for s in db.scalars(select(Schedule).order_by(Schedule.name)):
        ids = oncall_user_ids(s, now)
        current = next(
            (x for x in shifts(s, now, now + timedelta(days=30)) if x.start <= now < x.end), None
        )
        nh = next_handoff(s, now)
        cards.append(
            {
                "schedule": s,
                "current": [names.get(i, f"#{i}") for i in ids],
                "until": current.end if current else None,
                "source": current.source if current else None,
                "next_at": nh[0] if nh else None,
                "next_user": names.get(nh[1]) if nh and nh[1] else None,
            }
        )
    return templates.TemplateResponse(
        request,
        "oncall/now.html",
        {
            "current_user": user,
            "draft": _draft(request),
            "worker_banner": banner_state(db),
            "cards": cards,
            "escalations": (open_escs := _open_escalations(db, user)),
            "paged": _paged_info(db, user, open_escs, names),
        },
    )


@router.get("/oncall/schedules", response_class=HTMLResponse)
def schedules_page(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    from app.services.teams import list_teams

    return templates.TemplateResponse(
        request,
        "oncall/schedules.html",
        {
            "current_user": user,
            "draft": _draft(request),
            "worker_banner": banner_state(db),
            "schedules": list(db.scalars(select(Schedule).order_by(Schedule.name))),
            "teams": list_teams(db),
        },
    )


@router.post("/oncall/schedules")
def schedule_create(
    request: Request,
    name: str = Form(...),
    timezone: str = Form(...),
    team_id: int | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    try:
        s = cat.create_schedule(db, name=name, timezone=timezone.strip(), team_id=team_id)
        db.commit()
        return _back(request, f"/oncall/schedules/{s.id}")
    except ValueError as exc:
        db.rollback()
        return _invalid(
            request,
            exc,
            "schedule",
            {"name": name, "timezone": timezone, "team_id": team_id},
            lambda: schedules_page(request, user=user, db=db),
        )
    return _back(request, "/oncall/schedules")


@router.get("/oncall/schedules/{schedule_id}", response_class=HTMLResponse)
def schedule_detail(
    request: Request,
    schedule_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    s = db.get(Schedule, schedule_id)
    if s is None:
        return HTMLResponse("Not found", status_code=404)
    tz = ZoneInfo(s.timezone)
    now = datetime.now(UTC)
    raw = request.query_params.get("from")
    try:
        first_day = _parse_date(raw) if raw else now.astimezone(tz).date()
    except ValueError:
        first_day = now.astimezone(tz).date()
    start = datetime.combine(first_day, datetime.min.time()).replace(tzinfo=tz)
    end = datetime.combine(first_day + timedelta(days=TIMELINE_DAYS), datetime.min.time()).replace(
        tzinfo=tz
    )
    span = (end - start).total_seconds()
    users = _active_users(db)
    names = {u.id: u.name for u in db.scalars(select(User))}
    palette = {u.id: i % 6 for i, u in enumerate(users)}
    bars = []
    for sh in shifts(s, start, end):
        a, b = max(sh.start, start), min(sh.end, end)
        bars.append(
            {
                "shift": sh,
                "name": names.get(sh.user_id, f"#{sh.user_id}"),
                "left": (a - start).total_seconds() / span * 100,
                "width": (b - a).total_seconds() / span * 100,
                "color": palette.get(sh.user_id, 0),
                "start_iso": sh.start.isoformat(),
                "end_iso": sh.end.isoformat(),
            }
        )
    days = [
        {
            "label": (first_day + timedelta(days=i)).strftime("%a %d"),
            "left": i / TIMELINE_DAYS * 100,
        }
        for i in range(TIMELINE_DAYS)
    ]
    now_pct = (now - start).total_seconds() / span * 100
    coverage = rotation_coverage(s, now, now + timedelta(days=TIMELINE_DAYS))
    ranked = sorted(s.rotations, key=lambda r: r.rank)
    rot_warnings: dict[int, str] = {}
    for idx, rot in enumerate(ranked):
        if coverage.get(rot.id):
            continue
        if not any(v.user_ids for v in rot.versions):
            rot_warnings[rot.id] = "No people in this rotation."
            continue
        above = [r for r in ranked[:idx] if coverage.get(r.id)]
        if above:
            winner = max(above, key=lambda r: coverage[r.id])
            rot_warnings[rot.id] = (
                f"Never on call in the next {TIMELINE_DAYS} days: {winner.name} always covers. "
                "Move it up or add a time window to the rotation above."
            )
    versions = {}
    for rot in s.rotations:
        current = max(
            (v for v in rot.versions if v.effective_from <= now),
            key=lambda v: v.effective_from,
            default=None,
        )
        planned = sorted(
            (v for v in rot.versions if v.effective_from > now), key=lambda v: v.effective_from
        )
        versions[rot.id] = {"current": current, "planned": planned}
    return templates.TemplateResponse(
        request,
        "oncall/schedule_detail.html",
        {
            "current_user": user,
            "draft": _draft(request),
            "worker_banner": banner_state(db),
            "s": s,
            "users": users,
            "names": names,
            "bars": bars,
            "legend": list(
                {b["name"]: {"name": b["name"], "color": b["color"]} for b in bars}.values()
            ),
            "days": days,
            "now_pct": now_pct if 0 <= now_pct <= 100 else None,
            "prev_from": (first_day - timedelta(days=TIMELINE_DAYS)).isoformat(),
            "next_from": (first_day + timedelta(days=TIMELINE_DAYS)).isoformat(),
            "today": now.astimezone(tz).date().isoformat(),
            "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "rot_warnings": rot_warnings,
            "versions": versions,
            "overrides": [o for o in s.overrides if o.ends_at > now],
            "now_local": now.astimezone(tz).strftime("%Y-%m-%dT%H:%M"),
        },
    )


@router.post("/oncall/schedules/{schedule_id}/delete")
def schedule_delete(
    request: Request,
    schedule_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    s = db.get(Schedule, schedule_id)
    if s:
        try:
            cat.delete_schedule(db, s)
            db.commit()
        except ValueError as exc:
            db.rollback()
            request.session["flash"] = str(exc)
    return RedirectResponse("/oncall/schedules", status_code=303)


@router.post("/oncall/schedules/{schedule_id}/rotations")
def rotation_create(
    request: Request,
    schedule_id: int,
    name: str = Form(...),
    user_ids: list[str] = Form([]),
    shift_hours: str = Form(""),
    handoff_local: str = Form(...),
    start_date: str = Form(...),
    win_days: list[int] = Form([]),
    win_start: str = Form(""),
    win_end: str = Form(""),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    s = db.get(Schedule, schedule_id)
    if s is None:
        return RedirectResponse("/oncall/schedules", status_code=303)
    try:
        cat.add_rotation(
            db,
            s,
            name=name,
            user_ids=_ids(user_ids),
            shift_hours=shift_hours,
            handoff_local=handoff_local,
            start_date=_parse_date(start_date),
            windows=_windows(win_days, win_start, win_end),
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _invalid(
            request,
            exc,
            "rotation-add",
            {
                "name": name,
                "user_ids": user_ids,
                "shift_hours": shift_hours,
                "handoff_local": handoff_local,
                "start_date": start_date,
                "win_days": win_days,
                "win_start": win_start,
                "win_end": win_end,
            },
            lambda: schedule_detail(request, schedule_id, user=user, db=db),
        )
    return RedirectResponse(f"/oncall/schedules/{schedule_id}", status_code=303)


@router.post("/oncall/rotations/{rotation_id}/edit")
def rotation_edit(
    request: Request,
    rotation_id: int,
    user_ids: list[str] = Form([]),
    shift_hours: str = Form(""),
    handoff_local: str = Form(...),
    effective_from: str = Form(""),
    version_id: int | None = Form(None),
    win_days: list[int] = Form([]),
    win_start: str = Form(""),
    win_end: str = Form(""),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    rot = db.get(ScheduleRotation, rotation_id)
    if rot is None:
        return RedirectResponse("/oncall/schedules", status_code=303)
    sid = rot.schedule_id
    try:
        eff = (
            _parse_dt(effective_from, rot.schedule.timezone)
            if effective_from.strip()
            else datetime.now(UTC)
        )
        cat.update_rotation(
            db,
            rot,
            user_ids=_ids(user_ids),
            shift_hours=shift_hours,
            handoff_local=handoff_local,
            windows=_windows(win_days, win_start, win_end),
            effective_from=eff,
            version_id=version_id,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _invalid(
            request,
            exc,
            "rotation-edit",
            {
                "rotation_id": rotation_id,
                "version_id": version_id,
                "user_ids": user_ids,
                "shift_hours": shift_hours,
                "handoff_local": handoff_local,
                "effective_from": effective_from,
                "win_days": win_days,
                "win_start": win_start,
                "win_end": win_end,
            },
            lambda: schedule_detail(request, sid, user=user, db=db),
        )
    return RedirectResponse(f"/oncall/schedules/{sid}", status_code=303)


@router.post("/oncall/rotations/{rotation_id}/move")
def rotation_move(
    request: Request,
    rotation_id: int,
    direction: str = Form(...),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    rot = db.get(ScheduleRotation, rotation_id)
    if rot is None:
        return RedirectResponse("/oncall/schedules", status_code=303)
    sid = rot.schedule_id
    try:
        cat.move_rotation(db, rot, direction)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse(f"/oncall/schedules/{sid}", status_code=303)


@router.post("/oncall/rotations/{rotation_id}/delete")
def rotation_delete(
    rotation_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    rot = db.get(ScheduleRotation, rotation_id)
    if rot is None:
        return RedirectResponse("/oncall/schedules", status_code=303)
    sid = rot.schedule_id
    cat.delete_rotation(db, rot)
    db.commit()
    return RedirectResponse(f"/oncall/schedules/{sid}", status_code=303)


@router.post("/oncall/schedules/{schedule_id}/overrides")
def override_create(
    request: Request,
    schedule_id: int,
    user_id: int = Form(...),
    starts_at: str = Form(...),
    ends_at: str = Form(...),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    s = db.get(Schedule, schedule_id)
    if s is None:
        return RedirectResponse("/oncall/schedules", status_code=303)
    try:
        cat.add_override(
            db,
            s,
            user_id=user_id,
            starts_at=_parse_dt(starts_at, s.timezone),
            ends_at=_parse_dt(ends_at, s.timezone),
            created_by=user.id,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _invalid(
            request,
            exc,
            "override",
            {"user_id": user_id, "starts_at": starts_at, "ends_at": ends_at},
            lambda: schedule_detail(request, schedule_id, user=user, db=db),
        )
    return RedirectResponse(f"/oncall/schedules/{schedule_id}", status_code=303)


@router.post("/oncall/overrides/{override_id}/delete")
def override_delete(
    override_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    o = db.get(ScheduleOverride, override_id)
    if o is None:
        return RedirectResponse("/oncall/schedules", status_code=303)
    sid = o.schedule_id
    cat.delete_override(db, o)
    db.commit()
    return RedirectResponse(f"/oncall/schedules/{sid}", status_code=303)


@router.post("/oncall/schedules/{schedule_id}/take")
def shift_take(
    request: Request,
    schedule_id: int,
    starts_at: str = Form(...),
    ends_at: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Any signed-in user may take a shift for themselves (never for someone else)."""
    s = db.get(Schedule, schedule_id)
    if s is None:
        return RedirectResponse("/oncall/schedules", status_code=303)
    if effective_role(user) is None:
        return HTMLResponse("Forbidden", status_code=403)
    try:
        cat.add_override(
            db,
            s,
            user_id=user.id,
            starts_at=_parse_dt(starts_at, s.timezone),
            ends_at=_parse_dt(ends_at, s.timezone),
            created_by=user.id,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return RedirectResponse(f"/oncall/schedules/{schedule_id}", status_code=303)


# --- Escalation paths ---------------------------------------------------------

_PATHS = "/oncall/escalation-paths"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _repeat_text(n: int) -> str:
    return {0: "", 1: "repeat once", 2: "repeat twice"}.get(n, f"repeat {n} times")


def _describe_level(db: Session, level: EscalationLevel, names: dict, now: datetime) -> dict:
    parts: list[str] = []
    now_lines: list[str] = []
    for sid in level.schedule_ids or []:
        s = db.get(Schedule, sid)
        if s is None:
            parts.append("a deleted schedule")
            continue
        parts.append(f"whoever is on call on {s.name}")
        who = [names.get(i, f"#{i}") for i in oncall_user_ids(s, now)]
        now_lines.append(f"{s.name}: {', '.join(who) if who else 'nobody on call'}")
    for uid in level.user_ids or []:
        parts.append(names.get(uid, f"#{uid}"))
    text = f"Notify {' and '.join(parts)} \u00b7 wait {level.ack_timeout_minutes} min"
    return {"level": level, "text": text, "now": now_lines}


@router.get(_PATHS, response_class=HTMLResponse)
def paths_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from app.services.teams import list_teams

    return templates.TemplateResponse(
        request,
        "oncall/paths.html",
        {
            "current_user": user,
            "draft": _draft(request),
            "worker_banner": banner_state(db),
            "paths": list(db.scalars(select(EscalationPath).order_by(EscalationPath.name))),
            "teams": list_teams(db),
        },
    )


@router.post(_PATHS)
def path_create(
    request: Request,
    name: str = Form(...),
    team_id: int | None = Form(None),
    repeat_times: str = Form("0"),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    try:
        p = cat.create_path(db, name=name, team_id=team_id, repeat_times=repeat_times)
        db.commit()
        return _back(request, f"{_PATHS}/{p.id}")
    except ValueError as exc:
        db.rollback()
        return _invalid(
            request,
            exc,
            "path",
            {"name": name, "team_id": team_id, "repeat_times": repeat_times},
            lambda: paths_page(request, user=user, db=db),
        )
    return _back(request, _PATHS)


@router.get(_PATHS + "/{path_id}", response_class=HTMLResponse)
def path_detail(
    request: Request,
    path_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from app.services.teams import list_teams

    p = db.get(EscalationPath, path_id)
    if p is None:
        return HTMLResponse("Not found", status_code=404)
    now = datetime.now(UTC)
    names = {u.id: u.name for u in db.scalars(select(User))}
    rows = [_describe_level(db, lv, names, now) for lv in p.levels]
    ladder = " \u2192 ".join(f"{i}. {r['text']}" for i, r in enumerate(rows, start=1))
    rep = _repeat_text(p.repeat_times)
    if rep:
        ladder = f"{ladder} \u2192 {rep}" if ladder else rep
    return templates.TemplateResponse(
        request,
        "oncall/path_detail.html",
        {
            "current_user": user,
            "draft": _draft(request),
            "worker_banner": banner_state(db),
            "p": p,
            "rows": rows,
            "ladder": ladder,
            "users": _active_users(db),
            "schedules": list(db.scalars(select(Schedule).order_by(Schedule.name))),
            "teams": list_teams(db),
        },
    )


@router.post(_PATHS + "/{path_id}/edit")
def path_edit(
    request: Request,
    path_id: int,
    name: str = Form(...),
    team_id: int | None = Form(None),
    repeat_times: str = Form("0"),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    p = db.get(EscalationPath, path_id)
    if p is None:
        return _back(request, _PATHS)
    try:
        cat.update_path(db, p, name=name, team_id=team_id, repeat_times=repeat_times)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _invalid(
            request,
            exc,
            "path-edit",
            {"name": name, "team_id": team_id, "repeat_times": repeat_times},
            lambda: path_detail(request, path_id, user=user, db=db),
        )
    return _back(request, f"{_PATHS}/{path_id}")


@router.post(_PATHS + "/{path_id}/delete")
def path_delete(
    request: Request,
    path_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    p = db.get(EscalationPath, path_id)
    if p is None:
        return _back(request, _PATHS)
    try:
        cat.delete_path(db, p)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
        return _back(request, f"{_PATHS}/{path_id}")
    return _back(request, _PATHS)


@router.post(_PATHS + "/{path_id}/levels")
def level_create(
    request: Request,
    path_id: int,
    schedule_ids: list[str] = Form([]),
    user_ids: list[str] = Form([]),
    ack_timeout_minutes: str = Form("5"),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    p = db.get(EscalationPath, path_id)
    if p is None:
        return _back(request, _PATHS)
    try:
        cat.add_level(
            db,
            p,
            schedule_ids=_ids(schedule_ids),
            user_ids=_ids(user_ids),
            ack_timeout_minutes=ack_timeout_minutes,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _invalid(
            request,
            exc,
            "level-add",
            {
                "schedule_ids": schedule_ids,
                "user_ids": user_ids,
                "ack_timeout_minutes": ack_timeout_minutes,
            },
            lambda: path_detail(request, path_id, user=user, db=db),
        )
    return _back(request, f"{_PATHS}/{path_id}")


@router.post("/oncall/levels/{level_id}/edit")
def level_edit(
    request: Request,
    level_id: int,
    schedule_ids: list[str] = Form([]),
    user_ids: list[str] = Form([]),
    ack_timeout_minutes: str = Form("5"),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    lv = db.get(EscalationLevel, level_id)
    if lv is None:
        return _back(request, _PATHS)
    pid = lv.path_id
    try:
        cat.update_level(
            db,
            lv,
            schedule_ids=_ids(schedule_ids),
            user_ids=_ids(user_ids),
            ack_timeout_minutes=ack_timeout_minutes,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _invalid(
            request,
            exc,
            "level-edit",
            {
                "level_id": level_id,
                "schedule_ids": schedule_ids,
                "user_ids": user_ids,
                "ack_timeout_minutes": ack_timeout_minutes,
            },
            lambda: path_detail(request, pid, user=user, db=db),
        )
    return _back(request, f"{_PATHS}/{pid}")


@router.post("/oncall/levels/{level_id}/move")
def level_move(
    request: Request,
    level_id: int,
    direction: str = Form(...),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    lv = db.get(EscalationLevel, level_id)
    if lv is None:
        return _back(request, _PATHS)
    pid = lv.path_id
    try:
        cat.move_level(db, lv, direction)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return _back(request, f"{_PATHS}/{pid}")


@router.post("/oncall/levels/{level_id}/delete")
def level_delete(
    request: Request,
    level_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    lv = db.get(EscalationLevel, level_id)
    if lv is None:
        return _back(request, _PATHS)
    pid = lv.path_id
    try:
        cat.delete_level(db, lv)
        db.commit()
    except ValueError as exc:
        db.rollback()
        request.session["flash"] = str(exc)
    return _back(request, f"{_PATHS}/{pid}")


# --- Alert routes ----------------------------------------------------------


def _urgency_mode(route: AlertRoute | None) -> str:
    if route is None:
        return "always_high"
    if route.high_if:
        return "high_when"
    return "always_low" if route.urgency == Urgency.low else "always_high"


def _route_ctx(db: Session, user: User, route: AlertRoute | None, request: Request) -> dict:
    form = getattr(request.state, "oncall_form", None)
    draft = _draft(request)
    return {
        "current_user": user,
        "draft": draft,
        "worker_banner": banner_state(db),
        "route": route,
        "paths": list(db.scalars(select(EscalationPath).order_by(EscalationPath.name))),
        "urgency_mode": draft.get("urgency_mode", _urgency_mode(route)),
        "route_fields": routes_svc.ROUTE_FIELDS,
        "cond_rows": routes_svc.condition_rows_from_form(
            db, form, "cond", routes_svc.MAX_CONDITIONS
        )
        if form is not None
        else routes_svc.condition_rows(
            db, route.conditions if route else [], routes_svc.MAX_CONDITIONS
        ),
        "high_rows": routes_svc.condition_rows_from_form(db, form, "hi", routes_svc.MAX_HIGH_IF)
        if form is not None
        else routes_svc.condition_rows(db, route.high_if if route else [], routes_svc.MAX_HIGH_IF),
    }


@router.get("/oncall/routes", response_class=HTMLResponse)
def routes_page(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    names = routes_svc.integration_names(db)
    rows = list(db.scalars(select(AlertRoute).order_by(AlertRoute.rank, AlertRoute.id)))
    return templates.TemplateResponse(
        request,
        "oncall/routes.html",
        {
            **_route_ctx(db, user, None, request),
            "routes": rows,
            "integrations": routes_svc.integration_names(db),
            "route_preview": getattr(request.state, "route_preview", None),
            "route_sample": getattr(request.state, "route_sample", {}),
            "sentences": {r.id: routes_svc.describe_route(r, names) for r in rows},
        },
    )


@router.get("/oncall/routes/condition-value", response_class=HTMLResponse)
def route_condition_value(
    request: Request,
    i: int = 0,
    prefix: str = "cond",
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    """Operator + value widget for one condition row once its field/op is picked."""
    if prefix not in ("cond", "hi"):
        prefix = "cond"
    q = request.query_params
    field = q.get(f"{prefix}-{i}-field", "")
    field = field if field in routes_svc.ROUTE_FIELDS else ""
    ops = routes_svc.op_labels(field)
    op = q.get(f"{prefix}-{i}-op", "equals")
    return templates.TemplateResponse(
        request,
        "oncall/_route_cond_value.html",
        {
            "prefix": prefix,
            "row": {
                "i": i,
                "field": field,
                "op": op if op in ops else "equals",
                "op_labels": ops,
                "options": routes_svc.field_options(db, field) if field else None,
                "selected": [v for v in q.getlist(f"{prefix}-{i}-value") if v],
            },
        },
    )


def _route_from_form(db: Session, form, route: AlertRoute) -> None:
    name = (form.get("name") or "").strip()
    if not name:
        raise ValueError("Route name is required")
    try:
        path = db.get(EscalationPath, int(form.get("escalation_path_id") or 0))
    except ValueError:
        path = None
    if path is None:
        raise ValueError("Pick an escalation path")
    if not path.levels:
        raise ValueError("That escalation path has no levels yet.")
    mode = form.get("urgency_mode") or "always_high"
    if mode not in ("always_high", "always_low", "high_when"):
        raise ValueError("Invalid urgency")
    high_if = (
        routes_svc.conditions_from_form(form, "hi", routes_svc.MAX_HIGH_IF)
        if mode == "high_when"
        else []
    )
    if mode == "high_when" and not high_if:
        raise ValueError('Add at least one condition for "High only when\u2026"')
    if len(name) > 200:
        raise ValueError("Route name must be 200 characters or fewer")
    route.name = name
    route.escalation_path_id = path.id
    route.urgency = Urgency.low if mode == "always_low" else Urgency.high
    route.conditions = routes_svc.conditions_from_form(form, "cond", routes_svc.MAX_CONDITIONS)
    route.high_if = high_if


@router.post("/oncall/routes")
async def route_create(
    request: Request,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    route = AlertRoute()
    try:
        _route_from_form(db, form, route)
        route.rank = len(routes_svc.renumber(db)) + 1
        route.enabled = True
        db.add(route)
        db.commit()
        return RedirectResponse(f"/oncall/routes/{route.id}", status_code=303)
    except ValueError as exc:
        db.rollback()
        return _invalid(request, exc, "route", form, lambda: routes_page(request, user=user, db=db))


@router.post("/oncall/routes/preview", response_class=HTMLResponse)
def route_preview(
    request: Request,
    title: str = Form(""),
    source: str = Form(""),
    severity_raw: str = Form(""),
    integration_id: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Run the real matcher against an in-memory sample; never ingest or enqueue."""
    sample = SimpleNamespace(
        title=title[:500],
        source=source[:200],
        severity_raw=severity_raw[:200],
        integration_id=int(integration_id)
        if integration_id.isdigit() and len(integration_id) < 10
        else None,
    )
    matched = routes_svc.match_route(db, sample)
    result = {
        "route": matched,
        "urgency": routes_svc.route_urgency(matched, sample) if matched else None,
    }
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request, "oncall/_route_preview.html", {"route_preview": result}
        )
    request.state.route_preview = result
    request.state.route_sample = vars(sample)
    return routes_page(request, user=user, db=db)


@router.get("/oncall/routes/{route_id}", response_class=HTMLResponse)
def route_detail(
    request: Request,
    route_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    route = db.get(AlertRoute, route_id)
    if route is None:
        return RedirectResponse("/oncall/routes", status_code=303)
    names = routes_svc.integration_names(db)
    return templates.TemplateResponse(
        request,
        "oncall/route_detail.html",
        {
            **_route_ctx(db, user, route, request),
            "sentence": routes_svc.describe_route(route, names),
        },
    )


@router.post("/oncall/routes/{route_id}/edit")
async def route_edit(
    request: Request,
    route_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    route = db.get(AlertRoute, route_id)
    if route is None:
        return RedirectResponse("/oncall/routes", status_code=303)
    form = await request.form()
    try:
        _route_from_form(db, form, route)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _invalid(
            request, exc, "route", form, lambda: route_detail(request, route_id, user=user, db=db)
        )
    return RedirectResponse(f"/oncall/routes/{route_id}", status_code=303)


@router.post("/oncall/routes/{route_id}/move")
def route_move(
    route_id: int,
    direction: str = Form(...),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    route = db.get(AlertRoute, route_id)
    if route is not None and direction in ("up", "down"):
        routes_svc.move(db, route, direction)
        db.commit()
    return RedirectResponse("/oncall/routes", status_code=303)


@router.post("/oncall/routes/{route_id}/toggle")
def route_toggle(
    route_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    route = db.get(AlertRoute, route_id)
    if route is not None:
        route.enabled = not route.enabled
        db.commit()
    return RedirectResponse("/oncall/routes", status_code=303)


@router.post("/oncall/routes/{route_id}/delete")
def route_delete(
    route_id: int,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    route = db.get(AlertRoute, route_id)
    if route is not None:
        db.delete(route)
        db.commit()
    return RedirectResponse("/oncall/routes", status_code=303)


# ---- escalations -------------------------------------------------------------


def _is_ic(user: User) -> bool:
    role = effective_role(user)
    return role is not None and ROLE_RANK[role] >= ROLE_RANK[Role.incident_commander]


def _can_act(db: Session, user: User, esc: Escalation) -> bool:
    """IC+ or anyone who was notified (has a notify job) for this escalation."""
    if not oncall_notify.can_receive_escalation(db, user, esc):
        return False
    if _is_ic(user):
        return True
    return (
        db.scalar(
            select(NotificationJob.id)
            .where(
                NotificationJob.escalation_id == esc.id,
                NotificationJob.kind == "notify",
                NotificationJob.user_id == user.id,
            )
            .limit(1)
        )
        is not None
    )


@router.get("/oncall/escalations", response_class=HTMLResponse)
def escalations_page(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    since = datetime.now(UTC) - timedelta(days=7)
    closed = list(
        db.scalars(
            select(Escalation)
            .where(
                Escalation.status == EscalationStatus.resolved,
                Escalation.resolved_at >= since,
                _visible_escalation_condition(user),
            )
            .order_by(Escalation.resolved_at.desc())
        )
    )
    return templates.TemplateResponse(
        request,
        "oncall/escalations.html",
        {
            "current_user": user,
            "draft": _draft(request),
            "worker_banner": banner_state(db),
            "open": (open_escs := _open_escalations(db, user)),
            "closed": closed,
            "paged": _paged_info(
                db, user, open_escs, {u.id: u.name for u in db.scalars(select(User))}
            ),
        },
    )


@router.get("/oncall/escalations/{esc_id}", response_class=HTMLResponse)
def escalation_detail(
    request: Request,
    esc_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    esc = _get_visible_escalation(db, esc_id, user)
    names = {u.id: u.name for u in db.scalars(select(User))}
    jobs = list(
        db.scalars(
            select(NotificationJob)
            .where(NotificationJob.escalation_id == esc.id)
            .order_by(NotificationJob.run_at, NotificationJob.id)
        )
    )
    levels: dict[int, list[NotificationJob]] = {}
    for j in jobs:
        if j.kind == "notify":
            levels.setdefault(j.level or 0, []).append(j)
    return templates.TemplateResponse(
        request,
        "oncall/escalation_detail.html",
        {
            "current_user": user,
            "draft": _draft(request),
            "worker_banner": banner_state(db),
            "esc": esc,
            "fields": fields_for_type(db, default_incident_type_id(db)),
            "values": {},
            "severity_levels": list(db.scalars(select(SeverityLevel).order_by(SeverityLevel.rank))),
            "names": names,
            "levels": sorted(levels.items()),
            "pending": [j for j in jobs if j.status == "pending"],
            "channel_labels": {
                k: getattr(c, "label", k) for k, c in oncall_notify.CHANNELS.items()
            },
            "alert": db.get(Alert, esc.alert_id) if esc.alert_id else None,
            "incident": db.get(Incident, esc.incident_id) if esc.incident_id else None,
            "path": db.get(EscalationPath, esc.path_id) if esc.path_id else None,
            "can_act": _can_act(db, user, esc),
            "paged": _paged_info(db, user, [esc], names).get(esc.id, {}).get("paged", []),
            "is_open": esc.status in OPEN_STATES,
            "is_ic": _is_ic(user),
        },
    )


_RETURN_TARGETS = ("/oncall", "/oncall/escalations")


def _return_target(return_to: str | None, esc_id: int) -> str:
    """Only fixed on-call list pages are valid return targets (no open redirect)."""
    if return_to in _RETURN_TARGETS:
        return return_to
    return f"/oncall/escalations/{esc_id}"


def _act(
    request: Request,
    esc_id: int,
    user: User,
    db: Session,
    action: str,
    return_to: str | None = None,
):
    esc = _get_visible_escalation(db, esc_id, user)
    if not _can_act(db, user, esc):
        raise HTTPException(status_code=403, detail="Forbidden")
    now = datetime.now(UTC)
    if action == "ack":
        ok = engine.acknowledge(db, esc, user_id=user.id, via="web", now=now)
    elif action == "resolve":
        ok = engine.resolve(db, esc, user_id=user.id, via="web", now=now)
    else:
        ok = engine.escalate_now(db, esc, user_id=user.id, now=now)
    db.commit()
    if not ok:
        request.session["flash"] = "That action does not apply to the escalation's current state."
    return RedirectResponse(_return_target(return_to, esc_id), status_code=303)


@router.post("/oncall/escalations/{esc_id}/ack")
def escalation_ack(
    request: Request,
    esc_id: int,
    return_to: str | None = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return _act(request, esc_id, user, db, "ack", return_to)


@router.post("/oncall/escalations/{esc_id}/resolve")
def escalation_resolve(
    request: Request,
    esc_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return _act(request, esc_id, user, db, "resolve")


@router.post("/oncall/escalations/{esc_id}/escalate")
def escalation_escalate(
    request: Request,
    esc_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return _act(request, esc_id, user, db, "escalate")


@router.post("/oncall/escalations/{esc_id}/declare")
async def escalation_declare(
    request: Request,
    esc_id: int,
    severity_level_id: int | None = Form(None),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    from app.routers.alerts import declare_from_alert

    esc = _get_visible_escalation(db, esc_id, user)
    alert = db.get(Alert, esc.alert_id) if esc.alert_id else None
    if alert is None or esc.incident_id is not None or alert.incident_id is not None:
        request.session["flash"] = "Declare needs an alert without an incident."
        return RedirectResponse(f"/oncall/escalations/{esc_id}", status_code=303)
    form = await request.form()
    response = escalation_detail(request, esc_id, user, db)
    draft = {key: form.getlist(key) for key in form}
    if severity_level_id is None or db.get(SeverityLevel, severity_level_id) is None:
        return validation_response(
            request, response, "Choose an incident severity before declaring.", draft
        )
    try:
        raw = {
            int(key[3:]): form.getlist(key)
            for key in form
            if key.startswith("cf_") and key[3:].isdigit()
        }
        custom_values(db, default_incident_type_id(db), raw)
        inc = declare_from_alert(
            db,
            alert,
            user=user,
            escalation=esc,
            severity_level_id=severity_level_id,
            field_values=raw,
        )
    except ValueError as exc:
        db.rollback()
        return validation_response(request, response, str(exc), draft)
    return RedirectResponse(f"/incidents/{inc.id}", status_code=303)


# ---- manual paging -----------------------------------------------------------

MESSAGE_MAX = 1000


def _page_form_url(incident_id: int | None) -> str:
    return f"/oncall/page?incident_id={incident_id}" if incident_id else "/oncall/page"


@router.get("/oncall/page", response_class=HTMLResponse)
def page_form(
    request: Request,
    incident_id: int | None = None,
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    incident = get_visible_incident(db, incident_id, user) if incident_id else None
    return templates.TemplateResponse(
        request,
        "oncall/page_form.html",
        {
            "current_user": user,
            "draft": _draft(request),
            "worker_banner": banner_state(db),
            "incident": incident,
            "paths": list(db.scalars(select(EscalationPath).order_by(EscalationPath.name))),
            "users": [
                u for u in _active_users(db) if incident is None or can_view_incident(u, incident)
            ],
        },
    )


@router.get("/oncall/page/preview", response_class=HTMLResponse)
def page_preview(
    request: Request,
    target_kind: str = "path",
    path_id: str = "",
    incident_id: int | None = None,
    user_ids: list[str] = Query(default=[]),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    """Small fragment: who the Page form would notify right now."""
    incident = get_visible_incident(db, incident_id, user) if incident_id else None
    candidates: list[User] = []
    names: list[str] = []
    note = ""
    if target_kind == "users":
        try:
            ids = _ids(user_ids)
        except ValueError:
            ids = []
        ids = [i for i in ids if 0 < i < 2**31]
        found = {
            u.id: u
            for u in db.scalars(select(User).where(User.id.in_(ids), User.is_active.is_(True)))
        }
        candidates = [found[i] for i in ids if i in found]
        names = [u.name for u in candidates]
        if not names:
            note = "Choose at least one person."
    else:
        try:
            pid = int(path_id)
            path = db.get(EscalationPath, pid) if 0 < pid < 2**31 else None
        except ValueError:
            path = None
        if path is None:
            note = "Choose an escalation path."
        elif not path.levels:
            note = "That escalation path has no levels."
        else:
            candidates = engine.preview_level_users(db, path, 0, datetime.now(UTC))
            names = [u.name for u in candidates]
            if not names:
                note = "Level 1 has nobody on call right now."
    if incident is not None and incident.is_private:
        allowed = [u for u in candidates if can_view_incident(u, incident)]
        if len(allowed) != len(candidates):
            note = "Assign an incident role before paging someone who cannot access this private incident. Excluded recipients will not be notified."
        names = [u.name for u in allowed]
    return templates.TemplateResponse(
        request, "oncall/_page_preview.html", {"names": names, "note": note}
    )


@router.post("/oncall/page")
def page_submit(
    request: Request,
    target_kind: str = Form(...),
    path_id: str = Form(""),
    user_ids: list[str] = Form(default=[]),
    urgency: str = Form("high"),
    message: str = Form(""),
    incident_id: str = Form(""),
    user: User = Depends(require_role(Role.incident_commander)),
    db: Session = Depends(get_db),
):
    from app.services.timeline import log_event

    incident = None
    if incident_id.strip():
        try:
            incident = get_visible_incident(db, int(incident_id), user)
        except ValueError:
            incident = None
        if incident is None:
            return HTMLResponse("Not found", status_code=404)
    try:
        try:
            urg = Urgency(urgency)
        except ValueError as exc:
            raise ValueError("Urgency must be high or low") from exc
        path = None
        users: list[int] = []
        if target_kind == "path":
            try:
                path = db.get(EscalationPath, int(path_id))
            except ValueError:
                path = None
            if path is None:
                raise ValueError("Choose an escalation path")
            if not path.levels:
                raise ValueError("That escalation path has no levels")
            if incident is not None and incident.is_private:
                eligible = [
                    u
                    for idx in range(len(path.levels))
                    for u in engine.preview_level_users(db, path, idx, datetime.now(UTC))
                    if can_view_incident(u, incident)
                ]
                if not eligible:
                    raise ValueError(
                        "Assign an incident role before paging this path: nobody on call can access this private incident"
                    )
            target = f"{path.name} path"
        elif target_kind == "users":
            users = _ids(user_ids)
            if not users:
                raise ValueError("Choose at least one person")
            found = set(
                db.scalars(select(User.id).where(User.id.in_(users), User.is_active.is_(True)))
            )
            if found != set(users):
                raise ValueError("Unknown or inactive person selected")
            if incident is not None and any(
                not can_view_incident(db.get(User, uid), incident) for uid in users
            ):
                raise ValueError(
                    "Assign an incident role before paging someone who cannot access this private incident"
                )
            names = [
                n
                for (n,) in db.execute(
                    select(User.name).where(User.id.in_(users)).order_by(User.name)
                )
            ]
            target = ", ".join(names)
        else:
            raise ValueError("Choose an escalation path or people to page")
        if len(message or "") > MESSAGE_MAX:
            raise ValueError("Message must be 1000 characters or fewer")
        text = (message or "").strip() or None
        if incident is not None:
            title = incident.title
        else:
            title = f"Page from {user.name}: {text}" if text else f"Page from {user.name}"
        esc = engine.start_escalation(
            db,
            urgency=urg,
            title=title,
            path=path,
            user_ids=users or None,
            incident=incident,
            message=text,
            initiated_by=user.id,
            source="manual",
            now=datetime.now(UTC),
        )
        if incident is not None:
            log_event(
                db,
                incident,
                entry_type="page",
                body=f"Paged {target} ({urg.value})" + (f": {text}" if text else ""),
                created_by=user.id,
            )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _invalid(
            request,
            exc,
            "page",
            {
                "target_kind": target_kind,
                "path_id": path_id,
                "user_ids": user_ids,
                "urgency": urgency,
                "message": message,
            },
            lambda: page_form(
                request, incident_id=incident.id if incident else None, user=user, db=db
            ),
        )
    if incident is not None:
        return RedirectResponse(f"/incidents/{incident.id}?tab=timeline", status_code=303)
    return RedirectResponse(f"/oncall/escalations/{esc.id}", status_code=303)
