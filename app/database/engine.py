"""SQLAlchemy engine + session helpers, FTS5 setup."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event, text
from sqlalchemy.engine import Connection, Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
from app.utils.logging import logger

_engine: Engine | None = None

# SQLite permits a single writer at a time, but the indexer fans documents out
# across a thread pool — so multiple worker threads open their own connections
# and try to write concurrently, tripping "database is locked". (pysqlite's
# busy-timeout doesn't save them: a deferred BEGIN does a SELECT first, then the
# SHARED→RESERVED upgrade hits a write-write deadlock and SQLite returns
# SQLITE_BUSY *immediately*, bypassing the busy handler.) This process-global
# lock funnels every *write* transaction through one at a time. Readers never
# take it — WAL lets them run concurrently with the single writer, so search and
# the UI stay responsive mid-scan. Reentrant so a write path that nests another
# write doesn't self-deadlock.
_write_lock = threading.RLock()

# Flipped off if the FTS5 virtual table can't be created (FTS5 not compiled in).
# Lets the write path skip FTS statements entirely instead of issuing a doomed
# INSERT against a missing table inside an open transaction.
_fts_available: bool = True


@contextmanager
def db_write_lock() -> Iterator[None]:
    """Serialize a SQLite write across threads (reentrant).

    Acquire this around any write transaction that can run concurrently with
    the indexer's worker threads. Do **not** ``await`` while holding it — it is
    a thread lock, not an async lock; keep the critical section synchronous and
    SQLite-only (no LM/Chroma calls).
    """
    with _write_lock:
        yield


def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine
    s = get_settings()
    url = s.effective_db_url
    busy_ms = max(0, int(getattr(s, "sqlite_busy_timeout_ms", 10_000)))
    connect_args: dict = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": busy_ms / 1000.0}
    _engine = create_engine(url, echo=False, future=True, connect_args=connect_args)

    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _enable_sqlite_pragmas(dbapi_connection, _):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA temp_store=MEMORY")
            # Wait (rather than fail instantly) when another connection holds the
            # write lock — covers cross-process access and WAL checkpoints that
            # the in-process write lock can't guard.
            cur.execute(f"PRAGMA busy_timeout={busy_ms}")
            cur.close()

    return _engine


def reset_engine() -> None:
    """Dispose the cached engine so the next ``get_engine()`` rebuilds it.

    Used when the database file is replaced underneath the app (restore from
    backup): pooled connections still point at the old file — and on Windows
    keep it open, blocking the overwrite — so the file must be closed first.
    After this, the next ``get_engine()`` reconnects to whatever is now on disk.
    """
    global _engine
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception as e:
            logger.debug("engine dispose failed: {}", e)
    _engine = None


def init_db() -> None:
    """Create all tables and the FTS5 mirror for full-text search."""
    from app import models  # noqa: F401 — register tables

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _ensure_fts5(engine)
    logger.info("Database initialised at {}", get_settings().db_path)


def _ensure_fts5(engine: Engine) -> None:
    global _fts_available
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
            _fts_available = True
        except Exception as e:  # FTS5 missing → search degrades to vector only
            _fts_available = False
            logger.warning("FTS5 not available: {} — full-text search disabled", e)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-managed session with commit/rollback.

    ``expire_on_commit=False`` is intentional: this app routinely returns ORM
    objects from a ``with session_scope():`` block and keeps using them
    afterwards (UI handlers, audit logging, etc.). With the SQLAlchemy default
    those reads would all raise ``DetachedInstanceError`` because every commit
    expires every attribute. We're a local single-user app — no concurrent
    writers — so the freshness penalty is irrelevant.
    """
    session = Session(get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def write_session() -> Iterator[Session]:
    """``session_scope()`` for write paths, serialized via :data:`_write_lock`.

    Use this anywhere a write can run concurrently with the indexer's worker
    threads (i.e. the whole scan path). Readers should keep using
    ``session_scope()`` so they stay lock-free. Keep the block synchronous and
    SQLite-only — never ``await`` or call out to LM Studio/Chroma while it's
    open, or the lock will be held across slow work and serialize the pipeline.
    """
    with _write_lock:
        with session_scope() as s:
            yield s


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s


def fts_insert(
    chunk_id: int,
    document_id: int,
    content: str,
    tags: list[str],
    *,
    conn: Connection | None = None,
) -> None:
    """Insert into the FTS5 mirror (best-effort; no-op if FTS5 missing).

    Pass ``conn`` (e.g. ``session.connection()``) to run inside an existing
    transaction. This is required when called from within an open
    ``session_scope()``/``write_session()`` block: opening a *second*
    connection there would have it fight the session's own write lock on the
    same file and deadlock. With ``conn`` the FTS row commits atomically with
    the chunk. Without it we open our own serialized write transaction.
    """
    engine = get_engine()
    if not _fts_available or not engine.url.drivername.startswith("sqlite"):
        return
    stmt = text("INSERT INTO chunks_fts(chunk_id, document_id, text, tags) VALUES (:cid, :did, :t, :tg)")
    params = {"cid": chunk_id, "did": document_id, "t": content, "tg": " ".join(tags)}
    try:
        if conn is not None:
            conn.execute(stmt, params)
        else:
            with db_write_lock(), engine.begin() as own:
                own.execute(stmt, params)
    except Exception as e:
        # FTS is best-effort. When ``conn`` is the active session's connection
        # an ordinary statement error doesn't poison the transaction; warn so a
        # genuinely failing mirror is observable rather than silently empty.
        logger.warning("FTS insert skipped: {}", e)


def fts_delete_for_document(document_id: int, *, conn: Connection | None = None) -> None:
    engine = get_engine()
    if not _fts_available or not engine.url.drivername.startswith("sqlite"):
        return
    stmt = text("DELETE FROM chunks_fts WHERE document_id = :d")
    try:
        if conn is not None:
            conn.execute(stmt, {"d": document_id})
        else:
            with db_write_lock(), engine.begin() as own:
                own.execute(stmt, {"d": document_id})
    except Exception as e:
        logger.warning("FTS delete skipped: {}", e)


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
