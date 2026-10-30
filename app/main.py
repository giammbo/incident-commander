import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings, get_settings
from app.crypto import build_fernet, set_fernet
from app.errors import browser_http_exception_handler, browser_validation_exception_handler
from app.routers import account as account_router
from app.routers import alerts as alerts_router
from app.routers import auth as auth_router
from app.routers import automation as automation_router
from app.routers import catalog as catalog_router
from app.routers import connections as connections_router
from app.routers import followups as followups_router
from app.routers import groups as groups_router
from app.routers import incidents as incidents_router
from app.routers import ingest as ingest_router
from app.routers import insights as insights_router
from app.routers import invites as invites_router
from app.routers import maps as maps_router
from app.routers import oidc_auth as oidc_auth_router
from app.routers import oncall as oncall_router
from app.routers import oncall_inbound as oncall_inbound_router
from app.routers import postmortems as postmortems_router
from app.routers import settings as settings_router
from app.routers import users as users_router
from app.services.users import bootstrap_admin


def run_startup_bootstrap(db: Session, settings: Settings) -> None:
    """Create the protected admin on first boot and log the generated password once."""
    log = logging.getLogger("app.bootstrap")
    _, pw = bootstrap_admin(db, settings.ic_admin_email)
    db.commit()
    if pw:
        log.warning("=" * 60)
        log.warning("Bootstrap admin created: %s", settings.ic_admin_email)
        log.warning("Generated password (shown once): %s", pw)
        log.warning("Log in and change it immediately.")
        log.warning("=" * 60)


def create_app() -> FastAPI:
    settings = get_settings()
    set_fernet(build_fernet(settings.fernet_keys))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        from app.db import get_db

        db_gen = get_db()
        db = next(db_gen)
        try:
            run_startup_bootstrap(db, settings)
            from app.services.catalog import seed_severity_levels
            from app.services.incident_types import seed_incident_types
            from app.services.roles import seed_incident_role_types
            from app.services.statuses import seed_status_levels

            seed_severity_levels(db)
            seed_status_levels(db)
            seed_incident_role_types(db)
            seed_incident_types(db)
            db.commit()
        finally:
            db_gen.close()
        worker = None
        if settings.oncall_worker_in_process:
            from app.services.oncall_worker import start_in_process

            worker = start_in_process()
        try:
            yield
        finally:
            if worker is not None:
                thread, stop = worker
                stop.set()
                thread.join(timeout=5)

    app = FastAPI(title="Incident Commander", lifespan=lifespan)
    app.add_exception_handler(HTTPException, browser_http_exception_handler)
    app.add_exception_handler(RequestValidationError, browser_validation_exception_handler)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=settings.session_https_only,
        same_site="lax",
    )
    from app.security.csrf import CSRFMiddleware

    app.add_middleware(CSRFMiddleware)
    app.mount(
        "/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static"
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        # Always 200 (probes): a stale/unknown worker is reported, never an error.
        from app.services.oncall_worker import healthz_worker_status

        return {"status": "ok", "oncall_worker": healthz_worker_status()}

    app.include_router(auth_router.router)
    app.include_router(oidc_auth_router.router)
    app.include_router(account_router.router)
    app.include_router(connections_router.router)
    app.include_router(catalog_router.router)
    app.include_router(incidents_router.router)
    app.include_router(users_router.router)
    app.include_router(groups_router.router)
    app.include_router(settings_router.router)
    app.include_router(invites_router.router)
    app.include_router(maps_router.router)
    app.include_router(followups_router.router)
    app.include_router(postmortems_router.router)
    app.include_router(insights_router.router)
    app.include_router(ingest_router.router)
    app.include_router(alerts_router.router)
    app.include_router(automation_router.router)
    app.include_router(oncall_router.router)
    app.include_router(oncall_inbound_router.router)
    return app


app = create_app()
