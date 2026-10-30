"""Pure "who is on call" calculation over loaded Schedule objects (no DB access)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.models import Schedule, ScheduleOverride, ScheduleRotationVersion

_HANDOFF_HORIZON = timedelta(days=30)


@dataclass(frozen=True)
class Shift:
    user_id: int
    start: datetime
    end: datetime
    source: str  # "rotation" | "override"
    rotation_id: int | None


def _override_key(o: ScheduleOverride):
    return (o.created_at or datetime.min.replace(tzinfo=UTC), o.id or 0)


def _active_override(schedule: Schedule, at: datetime) -> ScheduleOverride | None:
    active = [o for o in schedule.overrides if o.starts_at <= at < o.ends_at]
    return max(active, key=_override_key) if active else None


def _version_at(rotation, at: datetime) -> ScheduleRotationVersion | None:
    eligible = [v for v in rotation.versions if v.effective_from <= at]
    return max(eligible, key=lambda v: v.effective_from) if eligible else None


def _parse_hm(value: str) -> timedelta:
    h, m = value.split(":")
    return timedelta(hours=int(h), minutes=int(m))


def _in_windows(windows: list, local: datetime) -> bool:
    if not windows:
        return True
    offset = timedelta(hours=local.hour, minutes=local.minute, seconds=local.second)
    for w in windows:
        if local.weekday() in w.get("days", []) and (
            _parse_hm(w["start"]) <= offset < _parse_hm(w["end"])
        ):
            return True
    return False


def _shift_index(v: ScheduleRotationVersion, tz: ZoneInfo, at: datetime) -> int:
    if v.shift_hours % 24 == 0:
        # wall-clock: handoffs stay at the local hour across DST changes
        a = v.anchor.astimezone(tz).replace(tzinfo=None)
        t = at.astimezone(tz).replace(tzinfo=None)
        return math.floor((t - a).total_seconds() / (v.shift_hours * 3600))
    return math.floor((at - v.anchor).total_seconds() / (v.shift_hours * 3600))


def _rotation_user(schedule: Schedule, at: datetime) -> tuple[int, int] | None:
    """(user_id, rotation_id) of the first applicable rotation, or None."""
    tz = ZoneInfo(schedule.timezone)
    local = at.astimezone(tz)
    for rot in sorted(schedule.rotations, key=lambda r: r.rank):
        v = _version_at(rot, at)
        if v is None or not v.user_ids or not _in_windows(v.windows or [], local):
            continue
        idx = _shift_index(v, tz, at)
        return v.user_ids[idx % len(v.user_ids)], rot.id
    return None


def _resolve(schedule: Schedule, at: datetime) -> tuple[int, str, int | None] | None:
    o = _active_override(schedule, at)
    if o is not None:
        return o.user_id, "override", None
    r = _rotation_user(schedule, at)
    if r is not None:
        return r[0], "rotation", r[1]
    return None


def oncall_user_ids(schedule: Schedule, at: datetime) -> list[int]:
    r = _resolve(schedule, at)
    return [r[0]] if r else []


def _version_boundaries(
    v: ScheduleRotationVersion, tz: ZoneInfo, start: datetime, end: datetime
) -> set[datetime]:
    out: set[datetime] = set()
    step = timedelta(hours=v.shift_hours)
    k0 = _shift_index(v, tz, start) - 1
    k1 = _shift_index(v, tz, end) + 1
    wall = v.shift_hours % 24 == 0
    a_local = v.anchor.astimezone(tz).replace(tzinfo=None)
    for k in range(k0, k1 + 1):
        if wall:
            b = (a_local + k * step).replace(tzinfo=tz).astimezone(UTC)
        else:
            b = v.anchor + k * step
        if start < b < end:
            out.add(b)
    return out


def _window_boundaries(
    v: ScheduleRotationVersion, tz: ZoneInfo, start: datetime, end: datetime
) -> set[datetime]:
    out: set[datetime] = set()
    if not v.windows:
        return out
    day = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date() + timedelta(days=1)
    while day <= last:
        midnight = datetime.combine(day, time())
        for w in v.windows:
            for edge in (w["start"], w["end"]):
                b = (midnight + _parse_hm(edge)).replace(tzinfo=tz).astimezone(UTC)
                if start < b < end:
                    out.add(b)
        day += timedelta(days=1)
    return out


def _timeline(schedule: Schedule, start: datetime, end: datetime):
    """Elementary intervals [(s, e, resolved|None)] covering [start, end)."""
    tz = ZoneInfo(schedule.timezone)
    points: set[datetime] = {start, end}
    for o in schedule.overrides:
        points.update(p for p in (o.starts_at, o.ends_at) if start < p < end)
    for rot in schedule.rotations:
        for v in rot.versions:
            if start < v.effective_from < end:
                points.add(v.effective_from)
            if v.user_ids:
                points |= _version_boundaries(v, tz, start, end)
                points |= _window_boundaries(v, tz, start, end)
    ordered = sorted(points)
    return [(s, e, _resolve(schedule, s)) for s, e in zip(ordered, ordered[1:], strict=False)]


def shifts(schedule: Schedule, start: datetime, end: datetime) -> list[Shift]:
    out: list[Shift] = []
    for s, e, res in _timeline(schedule, start, end):
        if res is None:
            continue
        user_id, source, rotation_id = res
        last = out[-1] if out else None
        if (
            last is not None
            and last.end == s
            and (last.user_id, last.source, last.rotation_id) == (user_id, source, rotation_id)
        ):
            out[-1] = Shift(user_id, last.start, e, source, rotation_id)
        else:
            out.append(Shift(user_id, s, e, source, rotation_id))
    return out


def next_handoff(schedule: Schedule, at: datetime) -> tuple[datetime, int | None] | None:
    """Next instant the on-call user changes within 30 days, and who comes in."""
    timeline = _timeline(schedule, at, at + _HANDOFF_HORIZON)
    if not timeline:
        return None
    current = timeline[0][2][0] if timeline[0][2] else None
    for s, _e, res in timeline[1:]:
        user = res[0] if res else None
        if user != current:
            return s, user
    return None


def rotation_coverage(schedule: Schedule, start: datetime, end: datetime) -> dict[int, timedelta]:
    """Time each rotation is the one resolved over [start, end), ignoring overrides.

    Every rotation id is present; 0 means "never the one on call" (a higher rotation
    always wins, or its windows/users never apply in the range).
    """
    out = {rot.id: timedelta() for rot in schedule.rotations}
    for s, e, _res in _timeline(schedule, start, end):
        r = _rotation_user(schedule, s)
        if r is not None:
            out[r[1]] += e - s
    return out
