import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings, get_settings
from app.crypto import build_fernet, set_fernet
from app.routers import account as account_router
from app.routers import auth as auth_router
from app.routers import catalog as catalog_router
from app.routers import connections as connections_router
from app.routers import followups as followups_router
from app.routers import groups as groups_router
from app.routers import incidents as incidents_router
from app.routers import invites as invites_router
from app.routers import maps as maps_router
from app.routers import oidc_auth as oidc_auth_router
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
        yield

    app = FastAPI(title="Incident Commander", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=settings.session_https_only,
        same_site="lax",
    )
    app.mount(
        "/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static"
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

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
    return app


app = create_app()
