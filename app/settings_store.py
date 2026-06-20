from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AppSettings, GoogleSettings, SlackSettings, SmtpSettings, SsoSettings


def get_or_create(db: Session, model):
    obj = db.get(model, 1)
    if obj is None:
        obj = model(id=1)
        db.add(obj)
        db.flush()
    return obj


def app_settings(db: Session) -> AppSettings:
    return get_or_create(db, AppSettings)


def slack_settings(db: Session) -> SlackSettings:
    return get_or_create(db, SlackSettings)


def google_settings(db: Session) -> GoogleSettings:
    return get_or_create(db, GoogleSettings)


def sso_settings(db: Session) -> SsoSettings:
    return get_or_create(db, SsoSettings)


def smtp_settings(db: Session) -> SmtpSettings:
    return get_or_create(db, SmtpSettings)
