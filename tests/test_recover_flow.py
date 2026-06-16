"""AUTH-2 regression: password recovery returns a NEW, usable recovery key.

Before the fix, recovery silently rotated the key without revealing it, so a
user who recovered once could never recover again (the only copy — its hash —
is one-way). The endpoint now returns the fresh key, and it must actually work
for a subsequent recovery.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    # Use the shared (conftest) data dir + engine; we don't rely on first-run
    # state, so we don't need an isolated DB.
    from app.main import create_app

    return TestClient(create_app())


def test_recover_returns_new_usable_key(client):
    from app.auth.security import create_user, hash_password, make_recovery_key
    from app.database import session_scope
    from app.models import UserRole

    # Seed a uniquely-named user with a known recovery key (robust to whatever
    # other tests already created in the shared DB).
    uname = "recover_tester_auth2"
    rk0 = make_recovery_key()
    with session_scope() as s:
        u = create_user(s, username=uname, password="originalPass123", role=UserRole.admin)
        u.recovery_key_hash = hash_password(rk0)
        s.add(u)

    # Recover with the original key -> the response carries a NEW recovery key.
    r = client.post(
        "/api/auth/recover",
        json={"username": uname, "recovery_key": rk0, "new_password": "newPassword1234"},
    )
    assert r.status_code == 200, r.text
    rk1 = r.json().get("recovery_key")
    assert rk1 and rk1 != rk0, "recover must return a fresh recovery key"

    # The consumed original key must no longer work.
    r = client.post(
        "/api/auth/recover",
        json={"username": uname, "recovery_key": rk0, "new_password": "shouldFail12345"},
    )
    assert r.status_code == 400

    # The newly issued key DOES work (no second-recovery lockout) and rotates again.
    r = client.post(
        "/api/auth/recover",
        json={"username": uname, "recovery_key": rk1, "new_password": "thirdPassword12"},
    )
    assert r.status_code == 200, r.text
    rk2 = r.json().get("recovery_key")
    assert rk2 and rk2 != rk1
