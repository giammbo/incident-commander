from app.security.passwords import generate_password, hash_password, verify_password


def test_hash_and_verify():
    h = hash_password("s3cret!")
    assert h != "s3cret!"
    assert verify_password("s3cret!", h) is True
    assert verify_password("wrong", h) is False


def test_generate_password_length():
    pw = generate_password()
    assert len(pw) >= 24
