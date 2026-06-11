"""The chat system prompt must carry TRUE library counts.

RAG only shows the model the few excerpts retrieved per question, so meta
questions ("wieviele Dokumente hast du?") used to be answered with the
SOURCES count (e.g. 10) even for a 126-document library. _library_overview
injects the real, ACL-filtered numbers.
"""

from __future__ import annotations

from app.auth.security import create_user
from app.chat.rag import SYSTEM_PROMPT, _library_overview
from app.database import init_db, session_scope
from app.models import (
    Document,
    DocumentSource,
    DocumentStatus,
    SourceType,
    UserRole,
    Visibility,
)


def _seed(prefix: str) -> tuple[int, int]:
    """(owner_id, other_id): owner has 3 indexed private docs (10 pages) +
    1 processing doc; other user has none of their own."""
    init_db()
    with session_scope() as session:
        owner = create_user(session, username=f"{prefix}-owner", password="pw-123456", role=UserRole.user)
        other = create_user(session, username=f"{prefix}-other", password="pw-123456", role=UserRole.user)
        session.flush()
        src = DocumentSource(
            name=f"{prefix}-src",
            type=SourceType.local,
            path=f"/tmp/{prefix}",
            owner_id=owner.id,
            visibility=Visibility.private,
        )
        session.add(src)
        session.flush()
        for i, (status, pages) in enumerate(
            [
                (DocumentStatus.indexed, 2),
                (DocumentStatus.indexed, 3),
                (DocumentStatus.indexed, 5),
                (DocumentStatus.processing, 99),  # must NOT count
            ]
        ):
            session.add(
                Document(
                    source_id=src.id,
                    path=f"/tmp/{prefix}/d{i}.pdf",
                    filename=f"d{i}.pdf",
                    content_hash=f"{prefix}-h{i}",
                    status=status,
                    page_count=pages,
                    owner_id=owner.id,
                    visibility=Visibility.private,
                )
            )
        session.flush()
        return owner.id, other.id


def test_overview_counts_indexed_docs_and_pages() -> None:
    owner_id, _ = _seed("ov1")
    with session_scope() as session:
        from app.models import User

        owner = session.get(User, owner_id)
        text = _library_overview(owner, {}, session)
    assert "3 indexed documents" in text
    assert "(10 pages)" in text
    assert "filters" not in text.lower() or "restricted" not in text


def test_overview_respects_acl() -> None:
    _, other_id = _seed("ov2")
    with session_scope() as session:
        from app.models import User

        other = session.get(User, other_id)
        text = _library_overview(other, {}, session)
    # The other user can't see the owner's private docs from THIS seed; any
    # count shown must come only from docs visible to them (shared/own).
    assert "ov2" not in text  # sanity: no leakage of names
    # Build expected count: shared docs from other tests may exist, but the
    # 3 private ov2 docs must not be included. Verify by comparing against a
    # direct ACL-filtered count.
    from sqlalchemy import func
    from sqlmodel import select

    from app.auth.acl import filter_documents
    from app.models import Document as _D
    from app.models import DocumentStatus as _DS

    with session_scope() as session:
        from app.models import User

        other = session.get(User, other_id)
        expected = session.exec(
            filter_documents(
                select(func.count(_D.id)).where(_D.status == _DS.indexed),
                other,
            )
        ).one()
        expected_count = int(expected[0] if isinstance(expected, tuple) else expected)
    assert f"{expected_count} indexed documents" in text


def test_overview_mentions_context_filter_restriction() -> None:
    owner_id, _ = _seed("ov3")
    with session_scope() as session:
        from app.models import User

        owner = session.get(User, owner_id)
        text = _library_overview(owner, {"tags": ["foo"]}, session)
    assert "restricted by context filters" in text


def test_system_prompt_has_library_overview_rule() -> None:
    assert "LIBRARY OVERVIEW" in SYSTEM_PROMPT
