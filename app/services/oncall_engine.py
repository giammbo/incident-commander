"""Escalation engine: durable job ladder for on-call pages.

All scheduled work lives in ``notification_jobs`` ('level' | 'notify' | 'exhaust');
there are no in-memory timers. ``tick`` runs the due jobs, one transaction per job.

Locking protocol (why it cannot double-send, deadlock, or page after an ack):

* The escalation row lock is the serialization point. Every change to an
  escalation *and to its jobs* happens while holding it: ``tick`` takes it per
  job (after claiming the job), and ``acknowledge`` / ``resolve`` /
  ``escalate_now`` take it before doing anything.
* A worker claims a job with ``FOR UPDATE SKIP LOCKED`` (batch select, then a
  per-job re-claim that also re-checks ``status = 'pending'``, since the
  per-job commit releases the rest of the batch). Two workers can never run the
  same job.
* Whoever holds an escalation lock never *waits* on a job lock: bulk skips use
  ``SKIP LOCKED``. Waiting would deadlock against a worker that holds a job and
  is waiting for the escalation. The price is that a bulk skip can miss a job
  another worker has claimed; that worker re-validates after it gets the
  escalation lock: status must still be ``triggered`` and the job must not be
  superseded (see ``_superseded``). So an ack that lands while a job is in
  flight still stops it.

Delivery is at-least-once: a crash between a provider ``send`` and the job's
commit leaves the job pending, so it is sent again on the next tick.

A job that fails unexpectedly never stalls the ladder: ``_mark_failed`` queues the
next step (or marks the escalation exhausted) in a fresh transaction.

Statuses: ``exhausted`` means paging stopped but the page is still open (it can be
acked and resolved, and keeps its short code); only ``resolved`` is closed.

``acknowledge`` / ``resolve`` / ``escalate_now`` / ``start_escalation`` only flush:
the caller commits (promptly — the escalation lock is held until then).
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import (
    Escalation,
    EscalationEvent,
    EscalationPath,
    EscalationStatus,
    Incident,
    NotificationJob,
    Schedule,
    Urgency,
    User,
)
from app.services import oncall_notify
from app.services.oncall_schedule import oncall_user_ids
from app.services.timeline import log_event

log = logging.getLogger(__name__)

MANUAL_TIMEOUT_MINUTES = 30

_OPEN = (EscalationStatus.triggered, EscalationStatus.acknowledged, EscalationStatus.exhausted)
_ACKABLE = (EscalationStatus.triggered, EscalationStatus.exhausted)
_STEP_KINDS = ("level", "exhaust")
_REPEAT = "repeat"  # detail marker on a level job that restarts the path
_SHORT_CODE_LOCK = 0x0C0DE5  # pg advisory lock key serializing short-code allocation
_DETAIL_MAX = 500


def _clip(text: str, n: int = _DETAIL_MAX) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
_INTL_PHONE_RE = re.compile(r"\+(\d{2})[\d ().-]{5,18}\d")
_BARE_PHONE_RE = re.compile(r"(?<![\w.])\d{8,15}(?![\w.])")


def _redact(text: str) -> str:
    """Mask contact details (emails, phone numbers) a provider error may echo."""
    text = _EMAIL_RE.sub(lambda m: "•••@" + m.group(1), text)
    text = _INTL_PHONE_RE.sub(lambda m: f"+{m.group(1)}•••", text)
    return _BARE_PHONE_RE.sub("•••", text)


def _random_code() -> int:
    return secrets.randbelow(10000)


def _event(
    db: Session, esc: Escalation, kind: str, body: str, *, now: datetime, user_id=None
) -> None:
    db.add(
        EscalationEvent(
            escalation_id=esc.id, kind=kind, body=_clip(body), user_id=user_id, created_at=now
        )
    )


def _name(db: Session, user_id: int | None) -> str:
    if user_id is None:
        return "the system"
    u = db.get(User, user_id)
    return u.name if u is not None else f"user {user_id}"


def _timeline(db: Session, esc: Escalation, entry_type: str, body: str, user_id) -> None:
    if esc.incident_id is None:
        return
    inc = db.get(Incident, esc.incident_id)
    if inc is not None:
        log_event(db, inc, entry_type=entry_type, body=body, created_by=user_id)


def _lock(db: Session, esc: Escalation) -> Escalation | None:
    return db.scalar(
        select(Escalation)
        .where(Escalation.id == esc.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _skip_pending(
    db: Session,
    esc_id: int,
    detail: str,
    now: datetime,
    *,
    kinds: tuple[str, ...] | None = None,
    exclude: int | None = None,
) -> None:
    """Mark pending jobs skipped without ever waiting on a job lock (see module doc)."""
    q = select(NotificationJob.id).where(
        NotificationJob.escalation_id == esc_id, NotificationJob.status == "pending"
    )
    if kinds:
        q = q.where(NotificationJob.kind.in_(kinds))
    if exclude is not None:
        q = q.where(NotificationJob.id != exclude)
    ids = list(db.scalars(q.with_for_update(skip_locked=True)))
    if ids:
        db.execute(
            update(NotificationJob)
            .where(NotificationJob.id.in_(ids))
            .values(status="skipped", detail=_clip(detail), done_at=now)
        )


def _path_of(db: Session, esc: Escalation) -> EscalationPath | None:
    return db.get(EscalationPath, esc.path_id) if esc.path_id is not None else None


def _next_step(esc: Escalation, level: int, path: EscalationPath | None):
    """(kind, level, is_repeat) of the step after ``level``."""
    if path is None:  # direct (manual) page: no ladder
        return "exhaust", None, False
    n = len(path.levels)
    if level + 1 < n:
        return "level", level + 1, False
    if n and esc.repeat_count < (path.repeat_times or 0):
        return "level", 0, True
    return "exhaust", None, False


def _enqueue_next(
    db: Session, esc: Escalation, level: int, path: EscalationPath | None, run_at: datetime
) -> tuple[str, int | None]:
    kind, nxt, repeat = _next_step(esc, level, path)
    db.add(
        NotificationJob(
            escalation_id=esc.id,
            kind=kind,
            level=nxt,
            run_at=run_at,
            detail=_REPEAT if repeat else None,
        )
    )
    return kind, nxt


def _short_code(db: Session) -> str:
    db.execute(select(func.pg_advisory_xact_lock(_SHORT_CODE_LOCK)))
    taken = set(db.scalars(select(Escalation.short_code).where(Escalation.status.in_(_OPEN))))
    for _ in range(200):
        code = f"{_random_code():04d}"
        if code not in taken:
            return code
    for i in range(10000):
        code = f"{i:04d}"
        if code not in taken:
            return code
    raise RuntimeError("no free escalation short code")


# ---- public API -------------------------------------------------------------


def start_escalation(
    db: Session,
    *,
    urgency: Urgency,
    title: str,
    path: EscalationPath | None = None,
    user_ids: list[int] | None = None,
    alert=None,
    incident=None,
    route=None,
    message: str | None = None,
    initiated_by: int | None = None,
    source: str,
    now: datetime,
) -> Escalation:
    if (path is None) == (not user_ids):
        raise ValueError("Page either an escalation path or one or more people")
    direct = list(dict.fromkeys(int(u) for u in user_ids)) if user_ids else []
    esc = Escalation(
        source=source,
        status=EscalationStatus.triggered,
        urgency=urgency,
        title=_clip(title, 500),
        message=message,
        short_code=_short_code(db),
        alert_id=alert.id if alert is not None else None,
        incident_id=incident.id if incident is not None else None,
        route_id=route.id if route is not None else None,
        path_id=path.id if path is not None else None,
        direct_user_ids=direct,
        current_level=0,
        repeat_count=0,
        initiated_by=initiated_by,
        created_at=now,
    )
    db.add(esc)
    db.flush()
    target = f"path {path.name}" if path is not None else f"{len(direct)} people"
    _event(
        db,
        esc,
        "triggered",
        f"Triggered ({urgency.value} urgency), paging {target}",
        now=now,
        user_id=initiated_by,
    )
    db.add(NotificationJob(escalation_id=esc.id, kind="level", level=0, run_at=now))
    db.flush()
    return esc


def acknowledge(db: Session, esc: Escalation, *, user_id: int | None, via: str, now) -> bool:
    esc = _lock(db, esc)
    if esc is None or esc.status not in _ACKABLE:
        return False
    esc.status = EscalationStatus.acknowledged
    esc.acked_at, esc.acked_by = now, user_id
    _skip_pending(db, esc.id, "escalation acknowledged", now)
    who = _name(db, user_id)
    _event(db, esc, "acknowledged", f"Acknowledged by {who} via {via}", now=now, user_id=user_id)
    _timeline(db, esc, "page_acked", f"Page acknowledged by {who} via {via}: {esc.title}", user_id)
    db.flush()
    return True


def resolve(db: Session, esc: Escalation, *, user_id: int | None, via: str, now) -> bool:
    esc = _lock(db, esc)
    if esc is None or esc.status not in _OPEN:
        return False
    esc.status = EscalationStatus.resolved
    esc.resolved_at, esc.resolved_by = now, user_id
    _skip_pending(db, esc.id, "escalation resolved", now)
    if via == "alert" and user_id is None:
        body = "Resolved automatically: the alert was resolved"
    else:
        body = f"Resolved by {_name(db, user_id)} via {via}"
    _event(db, esc, "resolved", body, now=now, user_id=user_id)
    _timeline(db, esc, "page_resolved", f"Page resolved ({body[0].lower() + body[1:]})", user_id)
    db.flush()
    return True


def resolve_for_alert(db: Session, alert, *, now) -> None:
    open_escs = db.scalars(
        select(Escalation)
        .where(Escalation.alert_id == alert.id, Escalation.status.in_(_OPEN))
        .order_by(Escalation.id)
    ).all()
    for esc in open_escs:
        resolve(db, esc, user_id=None, via="alert", now=now)


def escalate_now(db: Session, esc: Escalation, *, user_id, now) -> bool:
    esc = _lock(db, esc)
    if esc is None or esc.status != EscalationStatus.triggered:
        return False
    _skip_pending(db, esc.id, "skipped by escalate now", now)
    kind, nxt = _enqueue_next(db, esc, esc.current_level, _path_of(db, esc), now)
    where = f"level {nxt + 1}" if kind == "level" else "the end of the path"
    _event(
        db,
        esc,
        "escalated",
        f"Escalated now by {_name(db, user_id)} to {where}",
        now=now,
        user_id=user_id,
    )
    db.flush()
    return True


# ---- worker -----------------------------------------------------------------


def tick(db: Session, *, now: datetime, base_url: str, limit: int = 50) -> int:
    """Run due jobs; commits after each one. Returns the number of jobs executed."""
    batch = db.scalars(
        select(NotificationJob)
        .where(NotificationJob.status == "pending", NotificationJob.run_at <= now)
        .order_by(NotificationJob.run_at, NotificationJob.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    ids = [j.id for j in batch]
    done = 0
    for job_id in ids:
        try:
            ran = _run_one(db, job_id, now=now, base_url=base_url)
            db.commit()
        except Exception as exc:
            db.rollback()
            log.exception("on-call job %s failed unexpectedly", job_id)
            ran = _mark_failed(db, job_id, exc, now)
        done += int(ran)
    db.commit()  # ends the batch transaction when nothing was claimed
    return done


def _mark_failed(db: Session, job_id: int, exc: Exception, now: datetime) -> bool:
    try:
        # the repeat marker is set at insert and never changes: a plain read is safe
        was_repeat = db.scalar(select(NotificationJob.detail).where(NotificationJob.id == job_id))
        row = db.execute(
            update(NotificationJob)
            .where(NotificationJob.id == job_id, NotificationJob.status == "pending")
            .values(status="failed", detail=f"internal error: {type(exc).__name__}", done_at=now)
            .returning(NotificationJob.escalation_id, NotificationJob.kind)
        ).first()
        if row is not None:
            db.add(
                EscalationEvent(
                    escalation_id=row.escalation_id,
                    kind="error",
                    body=f"Internal error running a {row.kind} job ({type(exc).__name__})",
                    created_at=now,
                )
            )
            if row.kind in _STEP_KINDS:
                _recover_step(db, job_id, row.escalation_id, now, was_repeat == _REPEAT)
        db.commit()
        return row is not None
    except Exception:
        db.rollback()
        log.exception("could not mark on-call job %s as failed", job_id)
        return False


def _recover_step(db: Session, job_id: int, esc_id: int, now: datetime, repeat: bool) -> None:
    """Keep the ladder moving after a level/exhaust job failed (its work was rolled back).

    Lock order stays job -> escalation: the caller already updated the job row.
    """
    esc = db.scalar(
        select(Escalation)
        .where(Escalation.id == esc_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if esc is None or esc.status != EscalationStatus.triggered:
        return
    job = db.get(NotificationJob, job_id)
    if job.kind == "exhaust":
        esc.status = EscalationStatus.exhausted
        _skip_pending(db, esc.id, "escalation exhausted", now, exclude=job.id)
        _event(db, esc, "exhausted", "Nobody acknowledged: escalation exhausted", now=now)
        return
    try:
        with db.begin_nested():
            if _superseded(db, job):  # a newer step already exists: nothing to queue
                return
            idx = job.level or 0
            if repeat:  # the in-job increment was rolled back
                esc.repeat_count += 1
            esc.current_level = idx
            kind, nxt, wraps = _next_step(esc, idx, _path_of(db, esc))
            db.add(
                NotificationJob(
                    escalation_id=esc.id,
                    kind=kind,
                    level=nxt,
                    run_at=now,
                    detail=_REPEAT if wraps else None,
                )
            )
            db.flush()
    except Exception:
        log.exception("could not compute the next step for escalation %s", esc_id)
        db.add(NotificationJob(escalation_id=esc.id, kind="exhaust", run_at=now))
        kind, nxt = "exhaust", None
    where = f"level {nxt + 1}" if kind == "level" else "exhaustion"
    _event(
        db,
        esc,
        "level_failed",
        f"Level {(job.level or 0) + 1} failed: moving on to {where}",
        now=now,
    )


def _finish(job: NotificationJob, status: str, now: datetime, detail: str | None = None) -> None:
    job.status = status
    job.done_at = now
    if detail is not None:
        job.detail = _clip(detail)


def _run_one(db: Session, job_id: int, *, now: datetime, base_url: str) -> bool:
    db.expire_all()  # paths, levels, schedules, users: read them fresh for each job
    job = db.scalar(
        select(NotificationJob)
        .where(NotificationJob.id == job_id, NotificationJob.status == "pending")
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if job is None:  # done or claimed by another worker since the batch select
        return False
    esc = db.scalar(
        select(Escalation)
        .where(Escalation.id == job.escalation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if esc is None:  # pragma: no cover - jobs cascade with their escalation
        return False
    if esc.status != EscalationStatus.triggered:
        _finish(job, "skipped", now, f"escalation {esc.status.value}")
        return True
    stale = _superseded(db, job)
    if stale:
        _finish(job, "skipped", now, stale)
        return True
    if job.kind == "level":
        _run_level(db, job, esc, now)
    elif job.kind == "notify":
        _run_notify(db, job, esc, now, base_url)
    elif job.kind == "exhaust":
        _run_exhaust(db, job, esc, now)
    else:
        _finish(job, "failed", now, f"unknown job kind {job.kind!r}")
    return True


def _superseded(db: Session, job: NotificationJob) -> str | None:
    """Backstop for jobs a SKIP LOCKED bulk skip could not reach.

    Each run of a level job inserts its notify jobs and *then* its next step, and
    every new step (level/exhaust) supersedes older pending ones. So a step job is
    current only if no newer step exists, and a notify job only while the single
    step created right after it has not run yet.
    """
    later = db.execute(
        select(NotificationJob.id, NotificationJob.status)
        .where(
            NotificationJob.escalation_id == job.escalation_id,
            NotificationJob.kind.in_(_STEP_KINDS),
            NotificationJob.id > job.id,
        )
        .order_by(NotificationJob.id)
    ).all()
    if job.kind in _STEP_KINDS:
        return "superseded by a newer escalation step" if later else None
    if job.kind == "notify" and (len(later) != 1 or later[0].status == "done"):
        return f"superseded: the escalation moved past level {(job.level or 0) + 1}"
    return None


def _active_users(db: Session, ids: list) -> list[User]:
    order: list[int] = []
    for raw in ids:
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            continue
        if uid not in order:
            order.append(uid)
    if not order:
        return []
    found = {
        u.id: u
        for u in db.scalars(select(User).where(User.id.in_(order), User.is_active.is_(True)))
    }
    return [found[i] for i in order if i in found]


def _recipient_ids(db: Session, esc: Escalation, path, level_idx: int, now) -> tuple[list, int]:
    """(candidate user ids, ack timeout minutes) for a level of this escalation."""
    if path is None:
        return list(esc.direct_user_ids or []), MANUAL_TIMEOUT_MINUTES
    levels = list(path.levels)
    if level_idx >= len(levels):  # path shortened mid-escalation
        return [], 0
    level = levels[level_idx]
    ids: list = []
    for sid in level.schedule_ids or []:
        try:
            sched = db.get(Schedule, int(sid))
        except (TypeError, ValueError):
            sched = None
        if sched is not None:  # a deleted schedule contributes nobody
            ids.extend(oncall_user_ids(sched, now))
    ids.extend(level.user_ids or [])
    return ids, level.ack_timeout_minutes


def preview_level_users(db: Session, path, level_idx: int, now) -> list[User]:
    """Active users the given path level would page right now (for the paging preview)."""
    ids, _timeout = _recipient_ids(db, None, path, level_idx, now)
    return _active_users(db, ids)


def _can_reach(db: Session, user: User, rules: list[tuple[str, int]]) -> bool:
    # Local channels first: a reachable email/push/sms/voice must never trigger a
    # Slack API lookup while the escalation row lock is held.
    ordered = sorted(rules, key=lambda r: r[0] == "slack")
    for channel_key, _delay in ordered:
        channel = oncall_notify.CHANNELS.get(channel_key)
        if channel is None:
            continue
        try:
            if channel.available(db) and channel.reachable(db, user):
                return True
        except Exception:  # unknown: don't skip a level on a probe error
            log.warning("reachability probe for %s failed", channel_key, exc_info=True)
            return True
    return False


def _channel_unavailable(db: Session, channel_key: str) -> bool:
    channel = oncall_notify.CHANNELS.get(channel_key)
    if channel is None:
        return False  # unknown channel: keep the job, _run_notify records the skip
    try:
        return not channel.available(db)
    except Exception:  # unknown: keep today's behavior (the job decides at send time)
        log.warning("availability probe for %s failed", channel_key, exc_info=True)
        return False


def _run_level(db: Session, job: NotificationJob, esc: Escalation, now: datetime) -> None:
    idx = job.level or 0
    if job.detail == _REPEAT:
        esc.repeat_count += 1
    esc.current_level = idx
    _skip_pending(db, esc.id, f"superseded by level {idx + 1}", now, kinds=("notify",))
    path = _path_of(db, esc)
    label = f"Level {idx + 1}" if path is not None else "Direct page"
    if job.detail == _REPEAT:
        label += f" (repeat {esc.repeat_count})"
    ids, timeout = _recipient_ids(db, esc, path, idx, now)
    users = [u for u in _active_users(db, ids) if oncall_notify.can_receive_escalation(db, u, esc)]
    raw_plans = [(u, oncall_notify.rules_for(db, u, esc.urgency)) for u in users]
    # Channels nobody can use (not configured by an admin) get no jobs at all: one
    # summary event per level start instead of a skipped row per person.
    unavailable: list[str] = []
    plans = []
    for u, rules in raw_plans:
        kept = []
        for channel_key, delay in rules:
            if _channel_unavailable(db, channel_key):
                ch_label = getattr(oncall_notify.CHANNELS.get(channel_key), "label", channel_key)
                if ch_label not in unavailable:
                    unavailable.append(ch_label)
                continue
            kept.append((channel_key, delay))
        plans.append((u, kept))
    if unavailable and users:
        _event(
            db,
            esc,
            "channels_unavailable",
            f"Not configured, skipped: {', '.join(unavailable)}",
            now=now,
        )
    reachable = [u for u, rules in plans if _can_reach(db, u, rules)]

    if not reachable:
        if not users:
            reason = "nobody on call or no active people"
        else:
            names = ", ".join(u.name for u in users)
            reason = f"nobody reachable ({names}: no configured channel)"
        _event(db, esc, "level_empty", f"{label} skipped: {reason}", now=now)
        _enqueue_next(db, esc, idx, path, now)
    else:
        for user, rules in plans:
            for channel_key, delay in rules:
                db.add(
                    NotificationJob(
                        escalation_id=esc.id,
                        kind="notify",
                        level=idx,
                        user_id=user.id,
                        channel=channel_key,
                        run_at=now + timedelta(minutes=max(0, delay)),
                    )
                )
        names = ", ".join(u.name for u in users)
        _event(db, esc, "level_started", f"{label} started: paging {names}", now=now)
        db.flush()  # notify ids must precede the next step's id (see _superseded)
        _enqueue_next(db, esc, idx, path, now + timedelta(minutes=timeout))
    _finish(job, "done", now)


def _run_notify(
    db: Session, job: NotificationJob, esc: Escalation, now: datetime, base_url: str
) -> None:
    user = db.get(User, job.user_id) if job.user_id is not None else None
    channel = oncall_notify.CHANNELS.get(job.channel or "")
    label = getattr(channel, "label", None) or str(job.channel)
    name = user.name if user is not None else f"user {job.user_id}"

    def skip(reason: str) -> None:
        _finish(job, "skipped", now, reason)
        _event(
            db,
            esc,
            "notify_skipped",
            f"{label} to {name} skipped: {reason}",
            now=now,
            user_id=job.user_id if user is not None else None,
        )

    if user is None or not user.is_active:
        return skip(f"{name} is not an active user")
    if not oncall_notify.can_receive_escalation(db, user, esc):
        return skip("recipient no longer has access to the linked incident")
    if channel is None:
        return skip(f"unknown channel {job.channel!r}")
    try:
        if not channel.available(db):
            return skip(f"{label} is not configured")
        if not channel.reachable(db, user):
            return skip(f"{name} is not reachable on {label}")
        ref = channel.send(
            db, user=user, escalation=esc, ack_url=oncall_notify.ack_url(base_url, esc, user)
        )
    except PermissionError:
        return skip("recipient no longer has access to the linked incident")
    except Exception as exc:
        reason = _clip(_redact(f"{type(exc).__name__}: {exc}"), 300)
        _finish(job, "failed", now, reason)
        _event(
            db,
            esc,
            "notify_failed",
            f"{label} to {name} failed: {reason}",
            now=now,
            user_id=user.id,
        )
        log.warning("on-call %s notification to user %s failed: %s", label, user.id, reason)
        return None
    _finish(job, "sent", now, str(ref or "sent"))
    _event(db, esc, "notified", f"Notified {name} via {label}", now=now, user_id=user.id)
    return None


def _run_exhaust(db: Session, job: NotificationJob, esc: Escalation, now: datetime) -> None:
    esc.status = EscalationStatus.exhausted
    _skip_pending(db, esc.id, "escalation exhausted", now, exclude=job.id)
    _event(db, esc, "exhausted", "Nobody acknowledged: escalation exhausted", now=now)
    _finish(job, "done", now)
