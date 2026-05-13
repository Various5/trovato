from app.auth.rate_limit import (
    MAX_ATTEMPTS,
    is_locked,
    record_failure,
    record_success,
)


def test_lockout_after_max_attempts() -> None:
    ip = "10.0.0.1"
    user = "alice_test"
    for _ in range(MAX_ATTEMPTS - 1):
        locked, _ = record_failure(ip, user)
        assert locked is False
    locked, retry = record_failure(ip, user)
    assert locked is True
    assert retry > 0
    locked_now, _ = is_locked(ip, user)
    assert locked_now is True


def test_record_success_clears_state() -> None:
    ip = "10.0.0.2"
    user = "bob_test"
    record_failure(ip, user)
    record_failure(ip, user)
    record_success(ip, user)
    locked, _ = is_locked(ip, user)
    assert locked is False
