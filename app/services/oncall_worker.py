"""On-call worker: drives ``oncall_engine.tick`` on a fixed cadence.

Runs in-process (daemon thread started by the app lifespan, the default) or as
a separate process (``python -m app.services.oncall_worker``, entrypoint
``worker``). Each loop iteration stamps ``OnCallSettings.worker_last_tick_at``
so the UI and ``/healthz`` can tell when nobody is driving the ladder.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import EscalationPath, OnCallSettings
from app.services import oncall_engine
from app.settings_store import oncall_settings

log = logging.getLogger(__name__)

STALE_AFTER_SECONDS = 120
DEFAULT_INTERVAL = 5.0


def run_once(session_factory, *, now: datetime | None = None) -> int:
    """One tick: run due jobs, stamp ``worker_last_tick_at``, commit."""
    now = now or datetime.now(UTC)
    db = session_factory()
    try:
        done = oncall_engine.tick(db, now=now, base_url=get_settings().base_url)
        oncall_settings(db).worker_last_tick_at = now
        db.commit()
        return done
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def run_forever(stop: threading.Event, *, interval: float = DEFAULT_INTERVAL) -> None:
    """Loop until ``stop`` is set. An error in one iteration is logged, never fatal."""
    from app import db as dbmod

    while not stop.is_set():
        try:
            dbmod.init_engine()  # inside the try: a failure is logged and retried
            run_once(dbmod.SessionLocal)
        except Exception:
            log.exception("on-call worker iteration failed")
        stop.wait(interval)


def start_in_process() -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(target=run_forever, args=(stop,), name="oncall-worker", daemon=True)
    thread.start()
    return thread, stop


def _status_from(last: datetime | None, now: datetime) -> str:
    if last is None:
        return "never"
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return "stale" if (now - last).total_seconds() > STALE_AFTER_SECONDS else "ok"


def worker_status(db: Session, *, now: datetime) -> str:
    """``"never"`` (no tick yet), ``"stale"`` (> 120 s ago) or ``"ok"``.

    ``/healthz`` can additionally report ``"unknown"`` (lookup failed or timed out),
    see ``healthz_worker_status``.
    """
    return _status_from(oncall_settings(db).worker_last_tick_at, now)


_HEALTH_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="healthz")
HEALTH_TIMEOUT = 0.5


def _session_factory():
    from app import db as dbmod

    dbmod.init_engine()
    return dbmod.SessionLocal


def _lookup_last_tick() -> datetime | None:
    """Read-only: never creates the settings row, never commits."""
    db = _session_factory()()
    try:
        return db.scalar(select(OnCallSettings.worker_last_tick_at).where(OnCallSettings.id == 1))
    finally:
        db.rollback()
        db.close()


def healthz_worker_status(*, timeout: float = HEALTH_TIMEOUT) -> str:
    """Worker status for ``/healthz``: ``never`` / ``stale`` / ``ok``, or ``unknown``.

    ``unknown`` means the lookup raised or took longer than ``timeout`` seconds; the
    probe must never hang on the database, so this never blocks past the timeout.
    """
    try:
        last = _HEALTH_POOL.submit(_lookup_last_tick).result(timeout=timeout)
        return _status_from(last, datetime.now(UTC))
    except Exception:  # includes TimeoutError
        return "unknown"


def banner_state(db: Session) -> str | None:
    """Worker status to warn about, or None: only when paths exist and the worker is down."""
    status = worker_status(db, now=datetime.now(UTC))
    if status == "ok":
        return None
    if db.scalar(select(EscalationPath.id).limit(1)) is None:
        return None
    return status


def configure_crypto() -> None:
    """Same Fernet setup as ``create_app`` (EncryptedString columns need it)."""
    from app.crypto import build_fernet, set_fernet

    set_fernet(build_fernet(get_settings().fernet_keys))


def main() -> None:  # pragma: no cover - process entrypoint
    import signal

    logging.basicConfig(level=logging.INFO)
    configure_crypto()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("on-call worker started")
    run_forever(stop)


if __name__ == "__main__":  # pragma: no cover
    main()
