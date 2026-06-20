import pytest
from cryptography.fernet import Fernet, InvalidToken

import app.crypto as crypto
from app.crypto import EncryptedString, build_fernet, get_fernet, set_fernet


def test_multifernet_roundtrip_and_rotation():
    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    mf_new = build_fernet([k2, k1])  # k2 is the new primary
    token = mf_new.encrypt(b"secret")
    # An old fernet that only knows k1 cannot decrypt a k2-encrypted token...
    with pytest.raises(InvalidToken):
        build_fernet([k1]).decrypt(token)
    # ...but a MultiFernet that still lists k1 can read it (rotation window).
    assert build_fernet([k2, k1]).decrypt(token) == b"secret"


def test_get_fernet_raises_when_unset():
    saved = crypto._fernet
    crypto._fernet = None
    try:
        with pytest.raises(RuntimeError):
            get_fernet()
    finally:
        crypto._fernet = saved


def test_encrypted_string_processes():
    set_fernet(build_fernet([Fernet.generate_key().decode()]))
    col = EncryptedString()
    bound = col.process_bind_param("hello", dialect=None)
    assert bound != "hello"  # ciphertext
    assert col.process_result_value(bound, dialect=None) == "hello"
    assert col.process_bind_param(None, dialect=None) is None
    assert col.process_result_value(None, dialect=None) is None
