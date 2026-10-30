import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.models import (
    Alert,
    Escalation,
    EscalationEvent,
    EscalationLevel,
    EscalationPath,
    EscalationStatus,
    Incident,
    IncidentEvent,
    NotificationJob,
    NotificationRule,
    Schedule,
    ScheduleRotation,
    ScheduleRotationVersion,
    Urgency,
    User,
)
from app.services import oncall_engine as engine
from app.services import oncall_notify

T0 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
BASE = "http://testserver"


def _m(minutes):
    return T0 + timedelta(minutes=minutes)


# ---- fakes & helpers --------------------------------------------------------


class FakeChannel:
    def __init__(self, key, rec):
        self.key = key
        self.label = key.capitalize()
        self.rec = rec
        self.is_available = True
        self.unreachable: set[int] = set()
        self.error: Exception | None = None
        self.delay = 0.0

    def available(self, db):
        return self.is_available

    def reachable(self, db, user):
        return user.id not in self.unreachable

    def send(self, db, *, user, escalation, ack_url):
        if self.delay:
            time.sleep(self.delay)
        self.rec.attempts.append((self.key, user.id, escalation.id))
        assert ack_url.startswith(BASE + "/oncall/ack/")
        if self.error is not None:
            raise self.error
        self.rec.sent.append((self.key, user.id))
        return f"{self.key}-ref-{user.id}"


@pytest.fixture
def ch(monkeypatch):
    rec = SimpleNamespace(sent=[], attempts=[])
    rec.ch = {k: FakeChannel(k, rec) for k in ("email", "slack", "push", "sms", "voice")}
    monkeypatch.setattr(oncall_notify, "CHANNELS", rec.ch)
    return rec


def _user(db, name, *, active=True):
    u = User(email=f"{name.lower()}@x.io", name=name, is_active=active)
    db.add(u)
    db.flush()
    return u


def _rules(db, user, rules, urgency=Urgency.high):
    for channel, delay in rules:
        db.add(
            NotificationRule(user_id=user.id, urgency=urgency, channel=channel, delay_minutes=delay)
        )
    db.flush()


def _path(db, levels, repeat_times=0, name="Payments"):
    p = EscalationPath(name=name, repeat_times=repeat_times)
    p.levels = [
        EscalationLevel(
            rank=i + 1,
            ack_timeout_minutes=lv.get("timeout", 5),
            schedule_ids=lv.get("schedules", []),
            user_ids=lv.get("users", []),
        )
        for i, lv in enumerate(levels)
    ]
    db.add(p)
    db.flush()
    return p


def _schedule(db, user_ids, name="Payments primary", effective=None):
    s = Schedule(name=name, timezone="UTC")
    r = ScheduleRotation(name="r1", rank=1)
    start = effective or (T0 - timedelta(days=1))
    r.versions = [
        ScheduleRotationVersion(
            effective_from=start, anchor=start, shift_hours=24, user_ids=user_ids, windows=[]
        )
    ]
    s.rotations = [r]
    db.add(s)
    db.flush()
    return s


def _start(db, **kw):
    kw.setdefault("urgency", Urgency.high)
    kw.setdefault("title", "Payments 5xx above 5%")
    kw.setdefault("source", "alert_route")
    kw.setdefault("now", T0)
    esc = engine.start_escalation(db, **kw)
    db.commit()
    return esc


def run(db, now, max_rounds=20):
    total = 0
    for _ in range(max_rounds):
        done = engine.tick(db, now=now, base_url=BASE)
        if not done:
            return total
        total += done
    raise AssertionError("tick did not settle")


def _kinds(db, esc):
    return list(
        db.scalars(
            select(EscalationEvent.kind)
            .where(EscalationEvent.escalation_id == esc.id)
            .order_by(EscalationEvent.id)
        )
    )


def _jobs(db, esc, **filters):
    q = select(NotificationJob).where(NotificationJob.escalation_id == esc.id)
    for k, v in filters.items():
        q = q.where(getattr(NotificationJob, k) == v)
    return list(db.scalars(q.order_by(NotificationJob.id)))


# ---- ladder -----------------------------------------------------------------


def test_path_escalation_notifies_level1_then_level2_after_timeout(db_session, ch):
    a, b = _user(db_session, "Ann"), _user(db_session, "Bob")
    _rules(db_session, a, [("email", 0)])
    _rules(db_session, b, [("email", 0)])
    sched = _schedule(db_session, [a.id])
    path = _path(
        db_session,
        [{"schedules": [sched.id], "timeout": 5}, {"users": [b.id], "timeout": 5}],
    )
    esc = _start(db_session, path=path)
    assert esc.status == EscalationStatus.triggered
    assert _kinds(db_session, esc) == ["triggered"]
    assert [(j.kind, j.level, j.run_at) for j in _jobs(db_session, esc)] == [("level", 0, T0)]

    run(db_session, T0)
    assert ch.sent == [("email", a.id)]
    assert esc.current_level == 0
    assert run(db_session, _m(4)) == 0
    assert ch.sent == [("email", a.id)]

    run(db_session, _m(5))
    assert ch.sent == [("email", a.id), ("email", b.id)]
    assert esc.current_level == 1
    kinds = _kinds(db_session, esc)
    assert kinds.count("level_started") == 2 and kinds.count("notified") == 2
    sent = _jobs(db_session, esc, kind="notify", status="sent")
    assert [j.detail for j in sent] == [f"email-ref-{a.id}", f"email-ref-{b.id}"]
    assert all(j.done_at is not None for j in sent)


def test_ack_stops_all_pending_notifications_and_further_levels(db_session, ch):
    a, b = _user(db_session, "Ann"), _user(db_session, "Bob")
    _rules(db_session, a, [("email", 0), ("slack", 0), ("sms", 2), ("voice", 5)])
    path = _path(db_session, [{"users": [a.id], "timeout": 10}, {"users": [b.id]}])
    inc = Incident(title="Checkout down")
    db_session.add(inc)
    db_session.flush()
    esc = _start(db_session, path=path, incident=inc)

    run(db_session, T0)
    assert sorted(ch.sent) == [("email", a.id), ("slack", a.id)]
    assert engine.acknowledge(db_session, esc, user_id=a.id, via="web", now=_m(1)) is True
    db_session.commit()
    assert esc.status == EscalationStatus.acknowledged
    assert esc.acked_by == a.id and esc.acked_at == _m(1)
    assert _jobs(db_session, esc, status="pending") == []
    assert run(db_session, _m(60)) == 0
    assert sorted(ch.sent) == [("email", a.id), ("slack", a.id)]
    assert "acknowledged" in _kinds(db_session, esc)
    ev = db_session.scalar(
        select(EscalationEvent).where(
            EscalationEvent.escalation_id == esc.id, EscalationEvent.kind == "acknowledged"
        )
    )
    assert "web" in ev.body and ev.user_id == a.id
    tl = db_session.scalars(select(IncidentEvent).where(IncidentEvent.incident_id == inc.id)).all()
    assert [t.entry_type for t in tl] == ["page_acked"]
    # no longer triggered: every state change is refused without raising
    assert engine.acknowledge(db_session, esc, user_id=b.id, via="web", now=_m(2)) is False
    assert engine.escalate_now(db_session, esc, user_id=b.id, now=_m(2)) is False


def _engine_sessions(pg_engine):
    return sessionmaker(bind=pg_engine, expire_on_commit=False)


def _one_due_notify(db, ch):
    """A manual page to Ann whose email notify job is pending and due at T0."""
    a = _user(db, "Ann")
    _rules(db, a, [("email", 0)])
    esc = _start(db, user_ids=[a.id], source="manual")
    assert engine.tick(db, now=T0, base_url=BASE) == 1  # the level job only
    db.commit()
    (job,) = _jobs(db, esc, kind="notify", status="pending")
    assert ch.sent == []
    return a, esc, job


def test_job_picked_before_ack_does_not_send_after_ack(db_session, pg_engine, ch):
    a, esc, job = _one_due_notify(db_session, ch)
    make = _engine_sessions(pg_engine)
    acker, worker = make(), make()
    result = {}

    def work():
        try:
            result["n"] = engine.tick(worker, now=T0, base_url=BASE)
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            result["error"] = exc

    try:
        # The acker holds the escalation row: the worker will pick (lock) the job,
        # then block on the escalation lock.
        esc_b = acker.scalar(select(Escalation).where(Escalation.id == esc.id).with_for_update())
        t = threading.Thread(target=work)
        t.start()
        deadline = time.monotonic() + 10
        with pg_engine.connect() as probe:
            while True:
                waiting = probe.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                    )
                ).scalar()
                if waiting:
                    break
                assert time.monotonic() < deadline, "worker never blocked on the escalation"
                time.sleep(0.02)
            free = probe.execute(
                text("SELECT id FROM notification_jobs WHERE id = :id FOR UPDATE SKIP LOCKED"),
                {"id": job.id},
            ).first()
            probe.rollback()
        assert free is None, "the worker should already hold the job row"
        assert engine.acknowledge(acker, esc_b, user_id=a.id, via="slack", now=T0) is True
        acker.commit()
        t.join(timeout=10)
        assert not t.is_alive()
    finally:
        acker.close()
        worker.close()

    assert "error" not in result
    assert result["n"] == 1
    assert ch.attempts == []
    db_session.expire_all()
    assert db_session.get(NotificationJob, job.id).status == "skipped"
    assert "acknowledged" in db_session.get(NotificationJob, job.id).detail
    assert "notified" not in _kinds(db_session, esc)


def test_two_concurrent_ticks_send_each_notification_once(db_session, pg_engine, ch):
    a, esc, job = _one_due_notify(db_session, ch)
    ch.ch["email"].delay = 0.3
    make = _engine_sessions(pg_engine)
    sessions = [make(), make()]
    barrier = threading.Barrier(2)
    results, errors = [], []

    def work(s):
        try:
            barrier.wait()
            results.append(engine.tick(s, now=T0, base_url=BASE))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(s,)) for s in sessions]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
    finally:
        for s in sessions:
            s.close()

    assert errors == []
    assert sorted(results) == [0, 1]
    assert ch.attempts == [("email", a.id, esc.id)]
    db_session.expire_all()
    assert db_session.get(NotificationJob, job.id).status == "sent"
    assert _kinds(db_session, esc).count("notified") == 1


def test_empty_level_moves_on_and_logs_level_empty(db_session, ch):
    a, b = _user(db_session, "Ann", active=False), _user(db_session, "Bob")
    _rules(db_session, b, [("email", 0)])
    nobody = _schedule(db_session, [], name="Empty")
    future = _schedule(db_session, [b.id], name="Later", effective=_m(60 * 24))
    path = _path(
        db_session,
        [
            {"schedules": [nobody.id, future.id, 9999], "users": [a.id], "timeout": 15},
            {"users": [b.id]},
        ],
    )
    esc = _start(db_session, path=path)

    run(db_session, T0)  # no waiting for the 15-minute timeout
    assert ch.sent == [("email", b.id)]
    assert esc.current_level == 1
    kinds = _kinds(db_session, esc)
    assert kinds.index("level_empty") < kinds.index("notified")
    assert _jobs(db_session, esc, kind="notify", level=0) == []


def test_unreachable_channel_skipped_with_reason_others_sent(db_session, ch):
    a, b = _user(db_session, "Ann"), _user(db_session, "Bob")
    _rules(db_session, a, [("email", 0), ("slack", 0), ("sms", 0)])
    _rules(db_session, b, [("email", 0)])
    ch.ch["slack"].unreachable = {a.id}
    ch.ch["sms"].is_available = False
    path = _path(db_session, [{"users": [a.id], "timeout": 5}, {"users": [b.id]}])
    esc = _start(db_session, path=path)

    run(db_session, T0)
    assert ch.sent == [("email", a.id)]
    slack = _jobs(db_session, esc, channel="slack")[0]
    assert slack.status == "skipped" and "Ann" in slack.detail
    assert _jobs(db_session, esc, channel="sms") == []  # unavailable: no job at all
    assert _kinds(db_session, esc).count("notify_skipped") == 1

    run(db_session, _m(5))  # the ladder carries on
    assert ch.sent == [("email", a.id), ("email", b.id)]


def test_unavailable_channels_get_one_summary_event_per_level_and_no_jobs(db_session, ch):
    a, b = _user(db_session, "Ann"), _user(db_session, "Bob")
    for u in (a, b):
        _rules(db_session, u, [("email", 0), ("sms", 0), ("voice", 0), ("slack", 0)])
    for k in ("sms", "voice", "slack"):
        ch.ch[k].is_available = False
    path = _path(db_session, [{"users": [a.id, b.id], "timeout": 5}])
    esc = _start(db_session, path=path)

    run(db_session, T0)
    assert ch.sent == [("email", a.id), ("email", b.id)]
    for k in ("sms", "voice", "slack"):
        assert _jobs(db_session, esc, channel=k) == []
    assert "notify_skipped" not in _kinds(db_session, esc)
    evs = [
        e
        for e in db_session.scalars(
            select(EscalationEvent).where(EscalationEvent.escalation_id == esc.id)
        )
        if e.kind == "channels_unavailable"
    ]
    assert len(evs) == 1
    assert evs[0].body == "Not configured, skipped: Sms, Voice, Slack"
    started = [
        e
        for e in db_session.scalars(
            select(EscalationEvent).where(EscalationEvent.escalation_id == esc.id)
        )
        if e.kind == "level_started"
    ]
    assert len(started) == 1 and started[0].body.startswith("Level 1 started")


def test_level_with_nobody_reachable_moves_on_immediately(db_session, ch):
    a, b = _user(db_session, "Ann"), _user(db_session, "Bob")
    _rules(db_session, a, [("sms", 0), ("voice", 1)])
    _rules(db_session, b, [("email", 0)])
    ch.ch["sms"].is_available = False
    ch.ch["voice"].is_available = False
    path = _path(db_session, [{"users": [a.id], "timeout": 30}, {"users": [b.id]}])
    esc = _start(db_session, path=path)

    run(db_session, T0)
    assert ch.sent == [("email", b.id)]
    assert "level_empty" in _kinds(db_session, esc)


def test_failed_send_recorded_and_ladder_continues(db_session, ch):
    a, b = _user(db_session, "Ann"), _user(db_session, "Bob")
    _rules(db_session, a, [("email", 0), ("slack", 0)])
    _rules(db_session, b, [("email", 0)])
    path = _path(db_session, [{"users": [a.id], "timeout": 5}, {"users": [b.id]}])
    esc = _start(db_session, path=path)
    ch.ch["email"].error = RuntimeError("SMTP down")

    run(db_session, T0)
    email = _jobs(db_session, esc, channel="email")[0]
    assert email.status == "failed" and "SMTP down" in email.detail
    assert ch.sent == [("slack", a.id)]
    assert "notify_failed" in _kinds(db_session, esc)

    ch.ch["email"].error = None
    run(db_session, _m(5))
    assert ("email", b.id) in ch.sent
    assert esc.current_level == 1


def test_repeat_then_exhausted(db_session, ch):
    a = _user(db_session, "Ann")
    _rules(db_session, a, [("email", 0)])
    path = _path(db_session, [{"users": [a.id], "timeout": 5}], repeat_times=1)
    esc = _start(db_session, path=path)

    run(db_session, T0)
    assert ch.sent == [("email", a.id)] and esc.repeat_count == 0
    run(db_session, _m(5))
    assert ch.sent == [("email", a.id)] * 2 and esc.repeat_count == 1
    assert esc.status == EscalationStatus.triggered
    run(db_session, _m(10))
    assert esc.status == EscalationStatus.exhausted
    assert _kinds(db_session, esc)[-1] == "exhausted"
    assert run(db_session, _m(120)) == 0
    assert len(ch.sent) == 2
    assert _jobs(db_session, esc, status="pending") == []


def test_level1_people_not_chased_after_escalation_to_level2(db_session, ch):
    a, b = _user(db_session, "Ann"), _user(db_session, "Bob")
    _rules(db_session, a, [("email", 0), ("slack", 0), ("sms", 2), ("voice", 5)])
    _rules(db_session, b, [("email", 0)])
    path = _path(db_session, [{"users": [a.id], "timeout": 1}, {"users": [b.id]}])
    esc = _start(db_session, path=path)

    run(db_session, T0)
    run(db_session, _m(1))
    run(db_session, _m(30))
    assert sorted(ch.sent) == sorted([("email", a.id), ("slack", a.id), ("email", b.id)])
    for channel in ("sms", "voice"):
        (job,) = _jobs(db_session, esc, channel=channel)
        assert job.status == "skipped" and "level 2" in job.detail


def test_escalate_now_skips_pending_and_starts_next_level(db_session, ch):
    a, b = _user(db_session, "Ann"), _user(db_session, "Bob")
    _rules(db_session, a, [("email", 0), ("sms", 2)])
    _rules(db_session, b, [("email", 0)])
    path = _path(db_session, [{"users": [a.id], "timeout": 30}, {"users": [b.id]}])
    esc = _start(db_session, path=path)
    run(db_session, T0)

    assert engine.escalate_now(db_session, esc, user_id=a.id, now=_m(1)) is True
    db_session.commit()
    run(db_session, _m(1))
    assert ch.sent == [("email", a.id), ("email", b.id)]
    assert esc.current_level == 1
    assert _jobs(db_session, esc, channel="sms")[0].status == "skipped"
    assert "escalated" in _kinds(db_session, esc)
    run(db_session, _m(120))  # old level-2 timer was skipped: exhausted once, no double step
    assert esc.status == EscalationStatus.exhausted
    assert _kinds(db_session, esc).count("level_started") == 2


def test_superseded_jobs_missed_by_bulk_skip_never_run(db_session, ch):
    """Bulk skips use SKIP LOCKED, so a job held by another worker can stay
    pending; when that worker gets to it, it must recognise it as superseded."""
    a, b = _user(db_session, "Ann"), _user(db_session, "Bob")
    _rules(db_session, a, [("email", 0), ("sms", 2)])
    _rules(db_session, b, [("email", 0)])
    path = _path(db_session, [{"users": [a.id], "timeout": 30}, {"users": [b.id]}])
    esc = _start(db_session, path=path)
    run(db_session, T0)
    (old_step,) = _jobs(db_session, esc, kind="level", level=1)
    (sms,) = _jobs(db_session, esc, channel="sms")

    # escalate at +3 so the stale sms (+2) is due before the new level job
    assert engine.escalate_now(db_session, esc, user_id=a.id, now=_m(3)) is True
    db_session.flush()
    # simulate the bulk skip missing both rows (they were locked elsewhere)
    old_step.status, old_step.detail = "pending", None
    sms.status, sms.detail = "pending", None
    db_session.commit()

    run(db_session, _m(31))
    assert ch.sent == [("email", a.id), ("email", b.id)]
    assert _kinds(db_session, esc).count("level_started") == 2
    db_session.refresh(old_step)
    db_session.refresh(sms)
    assert old_step.status == "skipped" and "superseded" in old_step.detail
    assert sms.status == "skipped" and "superseded" in sms.detail


def test_urgency_low_uses_low_rules(db_session, ch):
    a = _user(db_session, "Ann")
    _rules(db_session, a, [("voice", 0)], urgency=Urgency.high)
    _rules(db_session, a, [("email", 0)], urgency=Urgency.low)
    path = _path(db_session, [{"users": [a.id]}])
    esc = _start(db_session, path=path, urgency=Urgency.low)

    run(db_session, T0)
    assert ch.sent == [("email", a.id)]
    assert esc.urgency == Urgency.low


def test_manual_direct_users_exhaust_after_30_minutes(db_session, ch):
    a, b = _user(db_session, "Ann"), _user(db_session, "Bob")
    _rules(db_session, a, [("email", 0)])
    _rules(db_session, b, [("email", 0)])
    me = _user(db_session, "Ivy")
    esc = _start(db_session, user_ids=[a.id, b.id, a.id], source="manual", initiated_by=me.id)
    assert esc.direct_user_ids == [a.id, b.id] and esc.path_id is None
    assert esc.initiated_by == me.id and esc.source == "manual"

    run(db_session, T0)
    assert sorted(ch.sent) == sorted([("email", a.id), ("email", b.id)])
    run(db_session, _m(29))
    assert esc.status == EscalationStatus.triggered
    run(db_session, _m(30))
    assert esc.status == EscalationStatus.exhausted
    assert len(ch.sent) == 2
    assert engine.MANUAL_TIMEOUT_MINUTES == 30


def test_resolve_for_alert_closes_open_escalation(db_session, ch):
    a = _user(db_session, "Ann")
    _rules(db_session, a, [("email", 0), ("sms", 5)])
    alert = Alert(source="alertmanager", dedup_key="k1", title="5xx")
    inc = Incident(title="Checkout down")
    db_session.add_all([alert, inc])
    db_session.flush()
    path = _path(db_session, [{"users": [a.id]}])
    esc = _start(db_session, path=path, alert=alert, incident=inc)
    old = _start(db_session, path=path, alert=alert)
    stopped = _start(db_session, path=path, alert=alert)
    stopped.status = EscalationStatus.exhausted
    assert engine.resolve(db_session, old, user_id=a.id, via="web", now=T0) is True
    db_session.commit()
    run(db_session, T0)

    engine.resolve_for_alert(db_session, alert, now=_m(2))
    db_session.commit()
    assert esc.status == EscalationStatus.resolved
    assert esc.resolved_at == _m(2) and esc.resolved_by is None
    assert old.resolved_at == T0  # already closed: untouched
    assert stopped.status == EscalationStatus.resolved  # exhausted is still open
    assert stopped.resolved_at == _m(2)
    assert _jobs(db_session, esc, status="pending") == []
    assert run(db_session, _m(60)) == 0
    assert ch.sent == [("email", a.id)]
    assert _kinds(db_session, esc)[-1] == "resolved"
    tl = db_session.scalars(
        select(IncidentEvent.entry_type).where(IncidentEvent.incident_id == inc.id)
    ).all()
    assert tl == ["page_resolved"]
    assert engine.resolve(db_session, esc, user_id=a.id, via="web", now=_m(3)) is False


def test_short_code_unique_among_open(db_session, ch, monkeypatch):
    codes = iter([1234, 1234, 1234, 42, 1234])
    monkeypatch.setattr(engine, "_random_code", lambda: next(codes))
    a = _user(db_session, "Ann")
    first = _start(db_session, user_ids=[a.id], source="manual")
    first.status = EscalationStatus.exhausted  # stopped paging, still open
    db_session.commit()
    second = _start(db_session, user_ids=[a.id], source="manual")
    assert first.short_code == "1234" and second.short_code == "0042"
    assert engine.resolve(db_session, first, user_id=None, via="web", now=T0) is True
    db_session.commit()
    third = _start(db_session, user_ids=[a.id], source="manual")
    assert third.short_code == "1234"  # free again once the first is closed


def test_start_requires_exactly_one_target(db_session, ch):
    a = _user(db_session, "Ann")
    path = _path(db_session, [{"users": [a.id]}])
    with pytest.raises(ValueError):
        _start(db_session, source="manual")
    with pytest.raises(ValueError):
        _start(db_session, path=path, user_ids=[a.id], source="manual")


def test_unexpected_error_marks_job_failed_and_other_jobs_still_run(db_session, ch, monkeypatch):
    a = _user(db_session, "Ann")
    _rules(db_session, a, [("email", 0)])
    broken = _start(db_session, user_ids=[a.id], source="manual", title="broken")
    fine = _start(db_session, user_ids=[a.id], source="manual", title="fine")
    real = oncall_notify.rules_for
    calls = []

    def flaky(db, user, urgency):
        calls.append(user.id)
        if len(calls) == 1:  # the first level job (broken's) blows up
            raise KeyError("boom")
        return real(db, user, urgency)

    monkeypatch.setattr(oncall_notify, "rules_for", flaky)
    run(db_session, T0)
    (lvl,) = _jobs(db_session, broken, kind="level")
    assert lvl.status == "failed" and "KeyError" in lvl.detail
    assert ch.sent == [("email", a.id)]
    assert _jobs(db_session, fine, kind="notify")[0].status == "sent"
    # the ladder did not stall: the follow-up step (exhaust, for a direct page) ran
    (ex,) = _jobs(db_session, broken, kind="exhaust")
    assert ex.status == "done"
    assert broken.status == EscalationStatus.exhausted
    kinds = _kinds(db_session, broken)
    assert kinds.index("error") < kinds.index("level_failed") < kinds.index("exhausted")


def test_every_level_failing_still_ends_exhausted(db_session, ch, monkeypatch):
    a, b = _user(db_session, "Ann"), _user(db_session, "Bob")
    path = _path(db_session, [{"users": [a.id]}, {"users": [b.id]}], repeat_times=1)
    esc = _start(db_session, path=path)

    def boom(*args, **kw):
        raise RuntimeError("schedule backend down")

    monkeypatch.setattr(engine, "_recipient_ids", boom)
    run(db_session, T0)
    assert esc.status == EscalationStatus.exhausted
    assert esc.repeat_count == 1  # the wrap was counted despite the rollback
    levels = _jobs(db_session, esc, kind="level")
    assert [(j.level, j.status) for j in levels] == [
        (0, "failed"),
        (1, "failed"),
        (0, "failed"),
        (1, "failed"),
    ]
    assert ch.attempts == []


def test_failed_next_step_and_failed_exhaust_still_end_exhausted(db_session, ch, monkeypatch):
    a = _user(db_session, "Ann")
    esc = _start(db_session, user_ids=[a.id], source="manual")

    def boom(*args, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "_next_step", boom)
    monkeypatch.setattr(engine, "_run_exhaust", boom)
    run(db_session, T0)
    assert esc.status == EscalationStatus.exhausted
    assert [(j.kind, j.status) for j in _jobs(db_session, esc)] == [
        ("level", "failed"),
        ("exhaust", "failed"),
    ]
    assert _kinds(db_session, esc)[-1] == "exhausted"


def test_exhausted_page_can_still_be_acked_and_resolved(db_session, ch):
    a = _user(db_session, "Ann")
    _rules(db_session, a, [("email", 0)])
    esc = _start(db_session, user_ids=[a.id], source="manual")
    run(db_session, T0)
    run(db_session, _m(30))
    assert esc.status == EscalationStatus.exhausted

    assert engine.escalate_now(db_session, esc, user_id=a.id, now=_m(31)) is False
    assert engine.acknowledge(db_session, esc, user_id=a.id, via="web", now=_m(31)) is True
    db_session.commit()
    assert esc.status == EscalationStatus.acknowledged and esc.acked_by == a.id
    assert engine.resolve(db_session, esc, user_id=a.id, via="web", now=_m(32)) is True
    db_session.commit()
    assert esc.status == EscalationStatus.resolved
    assert engine.acknowledge(db_session, esc, user_id=a.id, via="web", now=_m(33)) is False


def test_failed_send_reason_redacts_contact_details(db_session, ch, caplog):
    a = _user(db_session, "Ann")
    _rules(db_session, a, [("sms", 0)])
    ch.ch["sms"].error = RuntimeError(
        "Twilio error 21211: The 'To' number +393331234567 is not valid for alice@example.com"
        " (ref 4155550100)"
    )
    esc = _start(db_session, user_ids=[a.id], source="manual")
    with caplog.at_level("WARNING"):
        run(db_session, T0)
    (job,) = _jobs(db_session, esc, kind="notify")
    ev = db_session.scalar(
        select(EscalationEvent).where(
            EscalationEvent.escalation_id == esc.id, EscalationEvent.kind == "notify_failed"
        )
    )
    for text_ in (ev.body, job.detail, caplog.text):
        for secret in ("3331234567", "alice@", "4155550100"):
            assert secret not in text_
    assert "+39•••" in ev.body and "•••@example.com" in ev.body
    assert "21211" in ev.body  # provider error codes survive


def test_reachable_email_never_probes_slack_when_level_starts(db_session, ch):
    a = _user(db_session, "Ann")
    _rules(db_session, a, [("slack", 3), ("email", 0)])
    probes = []
    slack = ch.ch["slack"]
    orig = slack.reachable
    slack.reachable = lambda db, user: (probes.append(user.id), orig(db, user))[1]
    path = _path(db_session, [{"users": [a.id], "timeout": 5}])
    _start(db_session, path=path)
    run(db_session, T0)
    assert probes == []
    assert ("email", a.id) in ch.sent
