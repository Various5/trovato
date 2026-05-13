"""Audit-log service — wraps writes to ``audit_events`` and offers a query API.

The service never raises so that audit-write failures cannot block the action
itself. The payload should be JSON-serialisable; sensitive fields (passwords,
recovery keys, full document content) must be redacted by callers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlmodel import select

from app.database import session_scope
from app.models import AuditEvent
from app.utils.logging import logger

_SENSITIVE_KEYS = {"password", "old_password", "new_password", "recovery_key", "secret_key"}


def _redact(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    cleaned: dict[str, Any] = {}
    for k, v in payload.items():
        if k.lower() in _SENSITIVE_KEYS:
            cleaned[k] = "[redacted]"
        elif isinstance(v, dict):
            cleaned[k] = _redact(v)
        else:
            cleaned[k] = v
    return cleaned


def log(event: str, *, user_id: int | None = None, payload: dict[str, Any] | None = None) -> None:
    """Best-effort write of an audit event."""
    try:
        with session_scope() as session:
            session.add(AuditEvent(user_id=user_id, event=event, payload=_redact(payload or {})))
    except Exception as e:
        logger.debug("audit write failed for {}: {}", event, e)


def list_events(
    *,
    limit: int = 200,
    user_id: int | None = None,
    event_prefix: str | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    with session_scope() as session:
        stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)  # type: ignore[arg-type]
        if user_id is not None:
            stmt = stmt.where(AuditEvent.user_id == user_id)
        if event_prefix:
            stmt = stmt.where(AuditEvent.event.like(f"{event_prefix}%"))  # type: ignore[attr-defined]
        if since is not None:
            stmt = stmt.where(AuditEvent.created_at >= since)
        rows = session.exec(stmt).all()
        return [r.model_dump(mode="json") for r in rows]
