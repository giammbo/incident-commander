from __future__ import annotations

from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

_fernet: MultiFernet | None = None


def build_fernet(keys: list[str]) -> MultiFernet:
    if not keys:
        raise ValueError("At least one Fernet key is required")
    return MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in keys])


def set_fernet(mf: MultiFernet) -> None:
    global _fernet
    _fernet = mf


def get_fernet() -> MultiFernet:
    if _fernet is None:
        raise RuntimeError("Fernet not configured; call set_fernet() at startup")
    return _fernet


class EncryptedString(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return get_fernet().encrypt(value.encode()).decode()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return get_fernet().decrypt(value.encode()).decode()
