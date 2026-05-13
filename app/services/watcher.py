"""Watched-folder service.

Per active source, an asyncio task watches the filesystem and triggers
incremental scans with a debounce window so bursts of edits don't kick off
a scan per file. Only PDFs (or whatever the source's include patterns
match) are considered.
"""

from __future__ import annotations

import asyncio
import fnmatch
from pathlib import Path
from typing import Any, Optional

from sqlmodel import select
from watchfiles import Change, awatch

from app.database import session_scope
from app.models import DocumentSource
from app.services.indexer import start_scan_in_background
from app.utils.logging import logger


_TASKS: dict[int, asyncio.Task[Any]] = {}
_DEBOUNCE_SECONDS = 4.0


async def _watch_source(source_id: int) -> None:
    while True:
        with session_scope() as session:
            src = session.get(DocumentSource, source_id)
            if not src or not src.active:
                logger.info("watcher: source {} gone/inactive", source_id)
                return
            path = src.path
            include = list(src.include_patterns or ["*.pdf"])
            exclude = list(src.exclude_patterns or [])

        root = Path(path)
        if not root.exists():
            logger.warning("watcher: source path missing: {}", path)
            await asyncio.sleep(30)
            continue

        logger.info("watcher: watching {}", root)
        pending = False
        try:
            async for changes in awatch(str(root), recursive=True, debounce=int(_DEBOUNCE_SECONDS * 1000)):
                relevant = False
                for change, p in changes:
                    if change == Change.deleted:
                        continue
                    name = Path(p).name
                    if any(fnmatch.fnmatch(name, pat) for pat in include) and not any(
                        fnmatch.fnmatch(name, pat) for pat in exclude
                    ):
                        relevant = True
                        break
                if not relevant:
                    continue
                if pending:
                    continue
                pending = True
                logger.info("watcher: triggering rescan of source {}", source_id)
                start_scan_in_background(source_id)
                # short cool-down before the next trigger window
                await asyncio.sleep(_DEBOUNCE_SECONDS)
                pending = False
        except asyncio.CancelledError:
            logger.info("watcher: stopping for source {}", source_id)
            raise
        except Exception as e:
            logger.exception("watcher error for source {}: {}", source_id, e)
            await asyncio.sleep(15)


def start_watcher(source_id: int) -> None:
    if source_id in _TASKS and not _TASKS[source_id].done():
        return
    task = asyncio.create_task(_watch_source(source_id), name=f"watch-source-{source_id}")
    _TASKS[source_id] = task


def stop_watcher(source_id: int) -> bool:
    task = _TASKS.pop(source_id, None)
    if not task:
        return False
    task.cancel()
    return True


def is_watching(source_id: int) -> bool:
    task = _TASKS.get(source_id)
    return bool(task and not task.done())


def watching_ids() -> list[int]:
    return [sid for sid, t in _TASKS.items() if not t.done()]


async def start_all_active() -> None:
    """Called once at startup — start watchers for every active source."""
    with session_scope() as session:
        active = session.exec(select(DocumentSource).where(DocumentSource.active == True)).all()  # noqa: E712
        ids = [s.id for s in active if s.id is not None]
    for sid in ids:
        start_watcher(sid)
    logger.info("watchers started for {} source(s)", len(ids))


_OPTIONAL = Optional  # silence unused-import warning
