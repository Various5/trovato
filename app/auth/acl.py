"""ACL helpers — visibility rules for documents and sources.

Rules:
    - admin role sees everything
    - owner sees their own rows (any visibility)
    - everyone sees ``shared`` rows

Apply via the ``filter_documents`` / ``filter_sources`` helpers to any SELECT
returning :class:`Document` / :class:`DocumentSource`.
"""

from __future__ import annotations

from sqlalchemy import or_
from sqlmodel.sql.expression import SelectOfScalar

from app.models import Document, DocumentSource, User, UserRole, Visibility


def can_see_document(user: User, doc: Document) -> bool:
    if user.role == UserRole.admin:
        return True
    if doc.owner_id == user.id:
        return True
    return doc.visibility == Visibility.shared


def can_see_source(user: User, src: DocumentSource) -> bool:
    if user.role == UserRole.admin:
        return True
    if src.owner_id == user.id:
        return True
    return src.visibility == Visibility.shared


def filter_documents(stmt: SelectOfScalar, user: User) -> SelectOfScalar:
    if user.role == UserRole.admin:
        return stmt
    return stmt.where(
        or_(
            Document.owner_id == user.id,
            Document.visibility == Visibility.shared,
        )
    )


def filter_sources(stmt: SelectOfScalar, user: User) -> SelectOfScalar:
    if user.role == UserRole.admin:
        return stmt
    return stmt.where(
        or_(
            DocumentSource.owner_id == user.id,
            DocumentSource.visibility == Visibility.shared,
        )
    )


def allowed_document_ids(user: User, session) -> set[int]:
    """Return the set of document IDs this user can read (admin → None means 'all').

    For non-admins we materialise a set so it can be passed to vector-store and
    full-text filters.
    """
    from sqlmodel import select

    if user.role == UserRole.admin:
        rows = session.exec(select(Document.id)).all()
        return {r for r in rows if r is not None}
    rows = session.exec(filter_documents(select(Document.id), user)).all()
    return {r for r in rows if r is not None}
