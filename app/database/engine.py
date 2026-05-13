"""SQLAlchemy engine + session helpers, FTS5 setup."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
from app.utils.logging import logger

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine
    s = get_settings()
    url = s.effective_db_url
    connect_args: dict = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 30}
    _engine = create_engine(url, echo=False, future=True, connect_args=connect_args)

    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _enable_sqlite_pragmas(dbapi_connection, _):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA temp_store=MEMORY")
            cur.close()

    return _engine


def init_db() -> None:
    """Create all tables and the FTS5 mirror for full-text search."""
    from app import models  # noqa: F401 — register tables

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _ensure_fts5(engine)
    logger.info("Database initialised at {}", get_settings().db_path)


def _ensure_fts5(engine: Engine) -> None:
    if not engine.url.drivername.startswith("sqlite"):
        return
    with engine.begin() as conn:
        try:
            conn.exec_driver_sql("""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    document_id UNINDEXED,
                    text,
                    tags,
                    tokenize='porter unicode61'
                )
                """)
        except Exception as e:  # FTS5 missing → search degrades to vector only
            logger.warning("FTS5 not available: {} — full-text search disabled", e)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-managed session with commit/rollback."""
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s


def fts_insert(chunk_id: int, document_id: int, content: str, tags: list[str]) -> None:
    """Insert into the FTS5 mirror (no-op if FTS5 missing)."""
    engine = get_engine()
    if not engine.url.drivername.startswith("sqlite"):
        return
    with engine.begin() as conn:
        try:
            conn.execute(
                text(
                    "INSERT INTO chunks_fts(chunk_id, document_id, text, tags) "
                    "VALUES (:cid, :did, :t, :tg)"
                ),
                {"cid": chunk_id, "did": document_id, "t": content, "tg": " ".join(tags)},
            )
        except Exception as e:
            logger.debug("FTS insert skipped: {}", e)


def fts_delete_for_document(document_id: int) -> None:
    engine = get_engine()
    if not engine.url.drivername.startswith("sqlite"):
        return
    with engine.begin() as conn:
        try:
            conn.execute(text("DELETE FROM chunks_fts WHERE document_id = :d"), {"d": document_id})
        except Exception as e:
            logger.debug("FTS delete skipped: {}", e)


def fts_search(query: str, limit: int = 25) -> list[tuple[int, int, float]]:
    """Return ``[(chunk_id, document_id, bm25_score), ...]`` (lower score = better)."""
    engine = get_engine()
    if not engine.url.drivername.startswith("sqlite"):
        return []
    with engine.connect() as conn:
        try:
            rows = conn.execute(
                text(
                    "SELECT chunk_id, document_id, bm25(chunks_fts) AS score "
                    "FROM chunks_fts WHERE chunks_fts MATCH :q "
                    "ORDER BY score LIMIT :l"
                ),
                {"q": query, "l": limit},
            ).all()
            return [(int(r[0]), int(r[1]), float(r[2])) for r in rows]
        except Exception as e:
            logger.debug("FTS search failed ({}): falling back to vector only", e)
            return []
