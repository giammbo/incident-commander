from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import Schedule, ScheduleOverride, ScheduleRotation, ScheduleRotationVersion
from app.services.oncall_schedule import next_handoff, oncall_user_ids, shifts

ROME = ZoneInfo("Europe/Rome")


def _utc(*a):
    return datetime(*a, tzinfo=UTC)


def _rome(*a):
    return datetime(*a, tzinfo=ROME).astimezone(UTC)


def _version(users, anchor, hours=24, effective=None, windows=None):
    return ScheduleRotationVersion(
        effective_from=effective or _utc(2020, 1, 1),
        anchor=anchor,
        shift_hours=hours,
        user_ids=users,
        windows=windows or [],
    )


def _sched(*rotations, overrides=()):
    s = Schedule(id=1, name="S", timezone="Europe/Rome")
    s.rotations = list(rotations)
    s.overrides = list(overrides)
    return s


def _rot(rid, rank, *versions):
    r = ScheduleRotation(id=rid, name=f"r{rid}", rank=rank)
    r.versions = list(versions)
    return r


def _daily(users=(1, 2, 3)):
    return _sched(_rot(1, 1, _version(list(users), _rome(2026, 11, 2, 9))))


def test_daily_rotation_cycles_users_at_handoff():
    s = _daily()
    assert oncall_user_ids(s, _rome(2026, 11, 3, 8, 59)) == [1]
    assert oncall_user_ids(s, _rome(2026, 11, 3, 9, 0)) == [2]


def test_daily_handoff_stays_at_local_9am_across_dst_spring():
    s = _sched(_rot(1, 1, _version([1, 2], _rome(2026, 3, 27, 9))))
    # 28 Mar 09:00 CET = 08:00 UTC -> user 2; 29 Mar 09:00 CEST = 07:00 UTC -> user 1
    assert oncall_user_ids(s, _utc(2026, 3, 29, 6, 59)) == [2]
    assert oncall_user_ids(s, _utc(2026, 3, 29, 7, 0)) == [1]
    assert next_handoff(s, _utc(2026, 3, 29, 6, 0)) == (_utc(2026, 3, 29, 7, 0), 1)


def test_daily_handoff_stays_at_local_9am_across_dst_autumn():
    s = _sched(_rot(1, 1, _version([1, 2], _rome(2026, 10, 23, 9))))
    # 25 Oct 09:00 CET = 08:00 UTC
    assert oncall_user_ids(s, _utc(2026, 10, 25, 7, 59)) == [2]
    assert oncall_user_ids(s, _utc(2026, 10, 25, 8, 0)) == [1]
    assert next_handoff(s, _utc(2026, 10, 25, 7, 0)) == (_utc(2026, 10, 25, 8, 0), 1)


def test_weekly_rotation():
    s = _sched(_rot(1, 1, _version([1, 2], _rome(2026, 11, 2, 9), hours=168)))
    assert oncall_user_ids(s, _rome(2026, 11, 8, 12)) == [1]
    assert oncall_user_ids(s, _rome(2026, 11, 9, 9)) == [2]
    assert oncall_user_ids(s, _rome(2026, 10, 30, 9)) == [2]  # before anchor


def test_twelve_hour_rotation_uses_absolute_hours():
    s = _sched(_rot(1, 1, _version([1, 2], _utc(2026, 3, 28, 0), hours=12)))
    assert oncall_user_ids(s, _utc(2026, 3, 29, 11, 59)) == [1]
    assert oncall_user_ids(s, _utc(2026, 3, 29, 12, 0)) == [2]
    assert oncall_user_ids(s, _utc(2026, 3, 30, 0, 0)) == [1]


def test_override_beats_rotation_and_latest_override_wins():
    t0 = _rome(2026, 11, 3, 10)
    o1 = ScheduleOverride(
        id=1, user_id=7, starts_at=t0, ends_at=t0 + timedelta(hours=4), created_at=_utc(2026, 11, 1)
    )
    o2 = ScheduleOverride(
        id=2, user_id=8, starts_at=t0, ends_at=t0 + timedelta(hours=2), created_at=_utc(2026, 11, 2)
    )
    s = _sched(_rot(1, 1, _version([1, 2, 3], _rome(2026, 11, 2, 9))), overrides=[o1, o2])
    assert oncall_user_ids(s, t0 - timedelta(minutes=1)) == [2]
    assert oncall_user_ids(s, t0 + timedelta(hours=1)) == [8]
    assert oncall_user_ids(s, t0 + timedelta(hours=3)) == [7]
    assert oncall_user_ids(s, t0 + timedelta(hours=4)) == [2]


def test_higher_rank_rotation_on_top_only_inside_its_window():
    office = _version(
        [10],
        _rome(2026, 1, 1, 9),
        windows=[{"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00"}],
    )
    always = _version([20], _rome(2026, 1, 1, 9))
    s = _sched(_rot(1, 1, office), _rot(2, 2, always))
    assert oncall_user_ids(s, _rome(2026, 11, 3, 10)) == [10]  # Tuesday
    assert oncall_user_ids(s, _rome(2026, 11, 3, 20)) == [20]
    assert oncall_user_ids(s, _rome(2026, 11, 7, 10)) == [20]  # Saturday
    assert oncall_user_ids(s, _rome(2026, 11, 3, 18)) == [20]  # end exclusive


def test_new_version_applies_only_from_effective_from():
    v1 = _version([1, 2], _rome(2026, 11, 2, 9))
    v2 = _version([9], _rome(2026, 11, 2, 9), effective=_rome(2026, 11, 10, 0))
    s = _sched(_rot(1, 1, v1, v2))
    assert oncall_user_ids(s, _rome(2026, 11, 9, 12)) == [2]  # old list: idx 7 -> user 2
    assert oncall_user_ids(s, _rome(2026, 11, 10, 12)) == [9]


def test_empty_schedule_returns_empty_list():
    assert oncall_user_ids(_sched(), _utc(2026, 11, 3)) == []
    assert (
        oncall_user_ids(_sched(_rot(1, 1, _version([], _utc(2026, 1, 1)))), _utc(2026, 11, 3)) == []
    )
    assert shifts(_sched(), _utc(2026, 11, 3), _utc(2026, 11, 4)) == []
    assert next_handoff(_sched(), _utc(2026, 11, 3)) is None


def test_shifts_are_contiguous_and_include_override_segments():
    start = _rome(2026, 11, 3, 0)
    end = start + timedelta(days=3)
    ov = ScheduleOverride(
        id=1,
        user_id=7,
        starts_at=_rome(2026, 11, 4, 12),
        ends_at=_rome(2026, 11, 4, 16),
        created_at=_utc(2026, 11, 1),
    )
    s = _sched(_rot(1, 1, _version([1, 2, 3], _rome(2026, 11, 2, 0))), overrides=[ov])
    out = shifts(s, start, end)
    assert out[0].start == start and out[-1].end == end
    assert all(a.end == b.start for a, b in zip(out, out[1:], strict=False))
    assert [(x.user_id, x.source) for x in out] == [
        (2, "rotation"),  # 3 Nov (idx 1)
        (3, "rotation"),  # 4 Nov before override
        (7, "override"),
        (3, "rotation"),  # 4 Nov after override
        (1, "rotation"),  # 5 Nov
    ]
    assert out[2].rotation_id is None and out[0].rotation_id == 1


def test_next_handoff_none_when_nothing_changes_and_gap_handling():
    one = _sched(_rot(1, 1, _version([5], _rome(2026, 1, 1, 9))))
    assert next_handoff(one, _utc(2026, 11, 3)) is None
    win = _version(
        [5],
        _rome(2026, 1, 1, 9),
        windows=[{"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00"}],
    )
    s = _sched(_rot(1, 1, win))
    assert next_handoff(s, _rome(2026, 11, 3, 10)) == (_rome(2026, 11, 3, 18), None)
    assert next_handoff(s, _rome(2026, 11, 3, 20)) == (_rome(2026, 11, 4, 9), 5)
