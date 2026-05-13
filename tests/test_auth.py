from app.auth.security import hash_password, verify_password


def test_password_round_trip() -> None:
    h = hash_password("CorrectHorseBatteryStaple")
    assert verify_password("CorrectHorseBatteryStaple", h)
    assert not verify_password("wrong", h)


def test_verify_handles_garbage() -> None:
    assert verify_password("anything", "not-a-valid-argon2-hash") is False
