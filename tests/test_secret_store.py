from app.database import init_db
from app.utils.secret_store import delete_secret, get_secret, put_secret


def test_put_get_delete_round_trip() -> None:
    init_db()
    payload = {"host": "example.com", "username": "alice", "password": "s3cret"}
    put_secret("test-source-1", payload)
    back = get_secret("test-source-1")
    assert back == payload
    assert delete_secret("test-source-1")
    assert get_secret("test-source-1") is None
