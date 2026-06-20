from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    session_secret: str
    fernet_keys: Annotated[list[str], NoDecode]
    base_url: str
    ic_admin_email: str = "admin@localhost"
    # Mark the session cookie Secure (HTTPS-only). Defaults True for the
    # production HTTPS posture; set SESSION_HTTPS_ONLY=false for plain-HTTP local dev.
    session_https_only: bool = True

    @field_validator("fernet_keys", mode="before")
    @classmethod
    def _split_keys(cls, v: object) -> object:
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    import os

    # Accept either FERNET_KEYS (csv) or a single FERNET_KEY.
    if "FERNET_KEYS" not in os.environ and "FERNET_KEY" in os.environ:
        os.environ["FERNET_KEYS"] = os.environ["FERNET_KEY"]
    return Settings()  # type: ignore[call-arg]
