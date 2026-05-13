from app.database import init_db
from app.services.audit import _redact, list_events, log


def test_redact_sensitive_keys() -> None:
    cleaned = _redact({"password": "secret", "username": "alice", "nested": {"new_password": "x"}})
    assert cleaned["password"] == "[redacted]"
    assert cleaned["username"] == "alice"
    assert cleaned["nested"]["new_password"] == "[redacted]"


def test_log_and_query() -> None:
    init_db()
    log("test.event.audit", payload={"foo": 1, "password": "shh"})
    rows = list_events(event_prefix="test.event", limit=5)
    assert any(r["event"] == "test.event.audit" for r in rows)
    for r in rows:
        if r["event"] == "test.event.audit":
            assert r["payload"]["password"] == "[redacted]"
            return
    raise AssertionError("event not found")
