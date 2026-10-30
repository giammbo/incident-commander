"""CRUD + validation for on-call schedules, versioned rotations and overrides.

The calculation module (``oncall_schedule``) trusts validated input; every rule
that keeps it safe is enforced here and reported as ``ValueError``.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AlertRoute,
    EscalationLevel,
    EscalationPath,
    Schedule,
    ScheduleOverride,
    ScheduleRotation,
    ScheduleRotationVersion,
    User,
)

MAX_SHIFT_HOURS = 24 * 28
_HM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError, TypeError) as exc:
        raise ValueError(f"Invalid timezone: {name!r}") from exc


def _parse_hm(value: str, label: str) -> time:
    if not isinstance(value, str) or not _HM.match(value):
        raise ValueError(f"{label} must be HH:MM")
    h, m = value.split(":")
    return time(int(h), int(m))


def _validate_windows(windows) -> list[dict]:
    out: list[dict] = []
    for w in windows or []:
        if not isinstance(w, dict):
            raise ValueError("Invalid window")
        days = w.get("days")
        if (
            not isinstance(days, list)
            or not days
            or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days)
        ):
            raise ValueError("Window days must be a non-empty list of weekdays 0-6")
        start = _parse_hm(w.get("start"), "Window start")
        end = _parse_hm(w.get("end"), "Window end")
        if start >= end:
            raise ValueError("Window start must be before end")
        out.append({"days": sorted(set(days)), "start": w["start"], "end": w["end"]})
    return out


def _validate_users(db: Session, user_ids) -> list[int]:
    ids = [int(u) for u in (user_ids or [])]
    if not ids:
        raise ValueError("A rotation needs at least one person")
    for uid in dict.fromkeys(ids):
        u = db.get(User, uid)
        if u is None or not u.is_active:
            raise ValueError(f"Unknown or inactive user: {uid}")
    return ids


def _validate_hours(shift_hours: int) -> int:
    try:
        h = int(shift_hours)
    except (TypeError, ValueError) as exc:
        raise ValueError("Shift length must be a number of hours") from exc
    if h < 1 or h > MAX_SHIFT_HOURS:
        raise ValueError(f"Shift length must be between 1 and {MAX_SHIFT_HOURS} hours")
    return h


def _local_to_utc(tz: ZoneInfo, day: date, hm: time) -> datetime:
    return datetime.combine(day, hm).replace(tzinfo=tz).astimezone(UTC)


def create_schedule(db: Session, *, name: str, timezone: str, team_id: int | None = None):
    name = (name or "").strip()
    if not name:
        raise ValueError("Name is required")
    if len(name) > 120:
        raise ValueError("Schedule name must be 120 characters or fewer")
    _zone(timezone)
    if db.scalar(select(Schedule).where(Schedule.name == name)):
        raise ValueError(f"A schedule named {name!r} already exists")
    s = Schedule(name=name, timezone=timezone, team_id=team_id)
    db.add(s)
    db.flush()
    return s


def delete_schedule(db: Session, schedule: Schedule) -> None:
    for lv in db.scalars(select(EscalationLevel)):
        if schedule.id in (lv.schedule_ids or []):
            raise ValueError(
                f"Schedule is used by escalation path '{lv.path.name}'; "
                "remove it from that path first"
            )
    db.delete(schedule)
    db.flush()


def add_rotation(
    db: Session,
    schedule: Schedule,
    *,
    name: str,
    user_ids: list[int],
    shift_hours: int,
    handoff_local: str,
    start_date: date,
    windows: list[dict] | None = None,
) -> ScheduleRotation:
    name = (name or "").strip()
    if not name:
        raise ValueError("Name is required")
    if len(name) > 120:
        raise ValueError("Rotation name must be 120 characters or fewer")
    users = _validate_users(db, user_ids)
    hours = _validate_hours(shift_hours)
    wins = _validate_windows(windows)
    hm = _parse_hm(handoff_local, "Handoff time")
    anchor = _local_to_utc(_zone(schedule.timezone), start_date, hm)
    rot = ScheduleRotation(
        schedule_id=schedule.id,
        name=name,
        rank=max((r.rank for r in schedule.rotations), default=0) + 1,
    )
    rot.versions.append(
        ScheduleRotationVersion(
            effective_from=anchor, anchor=anchor, shift_hours=hours, user_ids=users, windows=wins
        )
    )
    db.add(rot)
    db.flush()
    db.refresh(schedule)
    return rot


def update_rotation(
    db: Session,
    rotation: ScheduleRotation,
    *,
    user_ids: list[int],
    shift_hours: int,
    handoff_local: str,
    windows: list[dict] | None,
    effective_from: datetime,
    now: datetime | None = None,
    version_id: int | None = None,
) -> ScheduleRotationVersion:
    """Append a new version; earlier versions stay untouched.

    If shift length and handoff time-of-day are unchanged from the version in force at
    ``effective_from``, that version's anchor is reused (cadence phase preserved).
    Otherwise the anchor is ``handoff_local`` on the local date of ``effective_from``.
    """
    users = _validate_users(db, user_ids)
    hours = _validate_hours(shift_hours)
    wins = _validate_windows(windows)
    hm = _parse_hm(handoff_local, "Handoff time")
    now = now or datetime.now(UTC)
    if effective_from.tzinfo is None:
        effective_from = effective_from.replace(tzinfo=UTC)
    if effective_from < now:
        effective_from = now
    planned = None
    if version_id is not None:
        planned = next((v for v in rotation.versions if v.id == version_id), None)
        if planned is None or planned.effective_from <= now:
            raise ValueError("Only a planned version can be edited; create a new version instead")
    tz = _zone(rotation.schedule.timezone)
    in_force = max(
        (x for x in rotation.versions if x.effective_from <= effective_from),
        key=lambda x: x.effective_from,
        default=None,
    )
    if (
        in_force is not None
        and in_force.shift_hours == hours
        and in_force.anchor.astimezone(tz).time().replace(second=0, microsecond=0) == hm
    ):
        anchor = in_force.anchor  # same cadence: keep the phase (e.g. weekly handoff weekday)
    else:
        anchor = _local_to_utc(tz, effective_from.astimezone(tz).date(), hm)
    # Planned changes can be corrected without duplicate effective dates. Historical
    # versions stay immutable; ordinary edits still append a new version.
    if planned is not None:
        planned.effective_from = effective_from
        planned.anchor = anchor
        planned.shift_hours = hours
        planned.user_ids = users
        planned.windows = wins
        db.flush()
        return planned
    v = ScheduleRotationVersion(
        rotation_id=rotation.id,
        effective_from=effective_from,
        anchor=anchor,
        shift_hours=hours,
        user_ids=users,
        windows=wins,
    )
    rotation.versions.append(v)
    db.flush()
    return v


def delete_rotation(db: Session, rotation: ScheduleRotation) -> None:
    schedule = rotation.schedule
    db.delete(rotation)
    db.flush()
    db.refresh(schedule)
    for i, r in enumerate(sorted(schedule.rotations, key=lambda r: r.rank), start=1):
        r.rank = i
    db.flush()


def move_rotation(db: Session, rotation: ScheduleRotation, direction: str) -> None:
    if direction not in ("up", "down"):
        raise ValueError("Direction must be up or down")
    ordered = sorted(rotation.schedule.rotations, key=lambda r: (r.rank, r.id))
    i = ordered.index(rotation)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(ordered):
        ordered[i], ordered[j] = ordered[j], ordered[i]
    for n, r in enumerate(ordered, start=1):
        r.rank = n
    db.flush()


def add_override(
    db: Session,
    schedule: Schedule,
    *,
    user_id: int,
    starts_at: datetime,
    ends_at: datetime,
    created_by: int | None,
) -> ScheduleOverride:
    if ends_at <= starts_at:
        raise ValueError("Override must end after it starts")
    _validate_users(db, [user_id])
    o = ScheduleOverride(
        schedule_id=schedule.id,
        user_id=user_id,
        starts_at=starts_at,
        ends_at=ends_at,
        created_by=created_by,
    )
    db.add(o)
    db.flush()
    db.refresh(schedule)
    return o


def delete_override(db: Session, override: ScheduleOverride) -> None:
    schedule = override.schedule
    db.delete(override)
    db.flush()
    db.refresh(schedule)


# --- Escalation paths ---------------------------------------------------------


def _validate_repeat(repeat_times) -> int:
    try:
        n = int(repeat_times)
    except (TypeError, ValueError) as exc:
        raise ValueError("Repeat must be a number") from exc
    if not 0 <= n <= 10:
        raise ValueError("Repeat must be between 0 and 10")
    return n


def _validate_path_name(db: Session, name: str, *, exclude_id: int | None = None) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Path name is required")
    if len(name) > 120:
        raise ValueError("Path name is too long")
    other = db.scalar(select(EscalationPath).where(EscalationPath.name == name))
    if other is not None and other.id != exclude_id:
        raise ValueError(f"An escalation path named {name!r} already exists")
    return name


def _validate_targets(db: Session, schedule_ids, user_ids) -> tuple[list[int], list[int]]:
    try:
        sids = list(dict.fromkeys(int(i) for i in (schedule_ids or [])))
        uids = list(dict.fromkeys(int(i) for i in (user_ids or [])))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid recipient selection") from exc
    if not sids and not uids:
        raise ValueError("A level needs at least one schedule or person to notify")
    for sid in sids:
        if db.get(Schedule, sid) is None:
            raise ValueError(f"Unknown schedule: {sid}")
    for uid in uids:
        u = db.get(User, uid)
        if u is None or not u.is_active:
            raise ValueError(f"Unknown or inactive user: {uid}")
    return sids, uids


def _validate_ack(minutes) -> int:
    try:
        m = int(minutes)
    except (TypeError, ValueError) as exc:
        raise ValueError("Ack timeout must be a number of minutes") from exc
    if not 1 <= m <= 1440:
        raise ValueError("Ack timeout must be between 1 and 1440 minutes")
    return m


def create_path(
    db: Session, *, name: str, team_id: int | None = None, repeat_times: int = 0
) -> EscalationPath:
    path = EscalationPath(
        name=_validate_path_name(db, name),
        team_id=team_id or None,
        repeat_times=_validate_repeat(repeat_times),
    )
    db.add(path)
    db.flush()
    return path


def update_path(
    db: Session, path: EscalationPath, *, name: str, team_id: int | None, repeat_times: int
) -> EscalationPath:
    path.name = _validate_path_name(db, name, exclude_id=path.id)
    path.team_id = team_id or None
    path.repeat_times = _validate_repeat(repeat_times)
    db.flush()
    return path


def delete_path(db: Session, path: EscalationPath) -> None:
    used = db.scalar(select(AlertRoute.id).where(AlertRoute.escalation_path_id == path.id).limit(1))
    if used is not None:
        raise ValueError("This escalation path is used by an alert route; remove the route first")
    db.delete(path)
    db.flush()


def add_level(
    db: Session, path: EscalationPath, *, schedule_ids, user_ids, ack_timeout_minutes
) -> EscalationLevel:
    sids, uids = _validate_targets(db, schedule_ids, user_ids)
    ack = _validate_ack(ack_timeout_minutes)
    level = EscalationLevel(
        path_id=path.id,
        rank=len(path.levels) + 1,
        ack_timeout_minutes=ack,
        schedule_ids=sids,
        user_ids=uids,
    )
    db.add(level)
    db.flush()
    db.refresh(path)
    return level


def update_level(
    db: Session, level: EscalationLevel, *, schedule_ids, user_ids, ack_timeout_minutes
) -> EscalationLevel:
    sids, uids = _validate_targets(db, schedule_ids, user_ids)
    level.ack_timeout_minutes = _validate_ack(ack_timeout_minutes)
    level.schedule_ids = sids
    level.user_ids = uids
    db.flush()
    return level


def move_level(db: Session, level: EscalationLevel, direction: str) -> None:
    if direction not in ("up", "down"):
        raise ValueError("Direction must be up or down")
    ordered = sorted(level.path.levels, key=lambda lv: (lv.rank, lv.id))
    i = ordered.index(level)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(ordered):
        ordered[i], ordered[j] = ordered[j], ordered[i]
    for n, lv in enumerate(ordered, start=1):
        lv.rank = n
    db.flush()
    db.refresh(level.path)


def delete_level(db: Session, level: EscalationLevel) -> None:
    path = level.path
    if len(path.levels) <= 1:
        routed = db.scalar(
            select(AlertRoute.id).where(AlertRoute.escalation_path_id == path.id).limit(1)
        )
        if routed is not None:
            raise ValueError("This is the only level of a path used by an alert route")
    db.delete(level)
    db.flush()
    db.refresh(path)
    for i, lv in enumerate(sorted(path.levels, key=lambda lv: lv.rank), start=1):
        lv.rank = i
    db.flush()
    db.refresh(path)
