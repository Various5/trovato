"""Regression test for the DetachedInstanceError users saw on v0.1.3.

The pattern that hit production:

    with session_scope() as s:
        return s.get(User, uid)

…then the caller reads ``user.username`` outside the ``with`` block.

With ``expire_on_commit=True`` (SQLAlchemy default) this raises
``DetachedInstanceError: Instance is not bound to a Session``. The session
factory now sets ``expire_on_commit=False`` to avoid that — this test
locks the behaviour in.
"""

from __future__ import annotations

from app.auth.security import hash_password
from app.database import init_db, session_scope
from app.models import User, UserRole


def test_user_attributes_survive_session_close() -> None:
    init_db()
    with session_scope() as session:
        u = User(
            username="detach_probe",
            password_hash=hash_password("x"),
            role=UserRole.user,
        )
        session.add(u)
        session.flush()
        uid = u.id

    # Re-fetch in a fresh session and let it close — this is the exact pattern
    # used by ``_current_user()``.
    with session_scope() as session:
        fetched = session.get(User, uid)
    assert fetched is not None
    # All of these used to raise DetachedInstanceError
    assert fetched.id == uid
    assert fetched.username == "detach_probe"
    assert fetched.role == UserRole.user
    assert fetched.is_active is True
