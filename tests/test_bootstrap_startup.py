import logging

from sqlalchemy import select

from app.config import get_settings
from app.main import run_startup_bootstrap
from app.models import User


def test_bootstrap_logs_password_once(db_session, caplog):
    with caplog.at_level(logging.WARNING, logger="app.bootstrap"):
        run_startup_bootstrap(db_session, get_settings())
    assert db_session.scalar(select(User).where(User.is_protected_admin.is_(True))) is not None
    assert "Generated password" in caplog.text
