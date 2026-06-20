from cryptography.fernet import Fernet
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings, get_settings


def test_settings_parse_fernet_keys_csv():
    s = Settings(
        database_url="postgresql+psycopg://u:p@db:5432/ic",
        session_secret="x" * 32,
        fernet_keys="key1,key2",
        base_url="http://localhost:8000",
    )
    assert s.fernet_keys == ["key1", "key2"]
    assert s.ic_admin_email == "admin@localhost"  # default


def test_session_https_only_defaults_true():
    # The field default is secure (True) regardless of any HTTPS-only env in
    # the test session; assert it from the field definition itself.
    assert Settings.model_fields["session_https_only"].default is True


def test_settings_single_fernet_key():
    s = Settings(
        database_url="postgresql+psycopg://u:p@db:5432/ic",
        session_secret="x" * 32,
        fernet_keys="only-one",
        base_url="http://localhost:8000",
    )
    assert s.fernet_keys == ["only-one"]


def test_session_middleware_uses_https_only_setting(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/ic")
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("FERNET_KEYS", Fernet.generate_key().decode())
    monkeypatch.setenv("SESSION_HTTPS_ONLY", "true")
    get_settings.cache_clear()
    try:
        from app.main import create_app

        app = create_app()
        session_mw = next(m for m in app.user_middleware if m.cls is SessionMiddleware)
        assert session_mw.kwargs["https_only"] is True
    finally:
        get_settings.cache_clear()


def test_get_settings_shims_single_fernet_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/ic")
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("FERNET_KEY", "plain-single-key")
    monkeypatch.delenv("FERNET_KEYS", raising=False)

    get_settings.cache_clear()
    try:
        assert get_settings().fernet_keys == ["plain-single-key"]
    finally:
        get_settings.cache_clear()


def test_fernet_keys_plain_csv_from_env(monkeypatch):
    """Regression: FERNET_KEYS set as a plain CSV env var (not JSON) must load correctly.

    This is the test that would have caught the original bug where pydantic-settings
    JSON-decoded list[str] fields before field validators ran, causing a SettingsError
    for any non-JSON value such as a real Fernet key or comma-separated plain strings.
    With NoDecode applied, the raw string reaches _split_keys and is split on commas.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/ic")
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("FERNET_KEYS", "key1,key2")
    monkeypatch.delenv("FERNET_KEY", raising=False)

    get_settings.cache_clear()
    try:
        assert get_settings().fernet_keys == ["key1", "key2"]
    finally:
        get_settings.cache_clear()
