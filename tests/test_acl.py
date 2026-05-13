from app.auth.acl import can_see_document, can_see_source
from app.models import Document, DocumentSource, SourceType, User, UserRole, Visibility


def _u(uid: int, role: UserRole = UserRole.user) -> User:
    return User(id=uid, username=f"u{uid}", password_hash="x", role=role)


def test_admin_sees_everything() -> None:
    admin = _u(1, UserRole.admin)
    doc = Document(
        id=10, source_id=1, path="/x", filename="x.pdf", content_hash="h",
        owner_id=99, visibility=Visibility.private,
    )
    assert can_see_document(admin, doc)


def test_owner_sees_private() -> None:
    alice = _u(5)
    doc = Document(
        id=11, source_id=1, path="/x", filename="x.pdf", content_hash="h",
        owner_id=5, visibility=Visibility.private,
    )
    assert can_see_document(alice, doc)


def test_others_cannot_see_private() -> None:
    bob = _u(6)
    doc = Document(
        id=12, source_id=1, path="/x", filename="x.pdf", content_hash="h",
        owner_id=5, visibility=Visibility.private,
    )
    assert not can_see_document(bob, doc)


def test_shared_visible_to_all() -> None:
    bob = _u(6)
    doc = Document(
        id=13, source_id=1, path="/x", filename="x.pdf", content_hash="h",
        owner_id=5, visibility=Visibility.shared,
    )
    assert can_see_document(bob, doc)


def test_source_acl() -> None:
    bob = _u(6)
    s_private = DocumentSource(
        id=1, name="s", type=SourceType.local, path="/p",
        owner_id=5, visibility=Visibility.private,
    )
    s_shared = DocumentSource(
        id=2, name="s", type=SourceType.local, path="/p",
        owner_id=5, visibility=Visibility.shared,
    )
    assert not can_see_source(bob, s_private)
    assert can_see_source(bob, s_shared)
