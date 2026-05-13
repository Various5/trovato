"""Runtime metrics — CPU / RAM / disk / queue depth.

``psutil`` is the canonical source for CPU + memory data. If it's missing we
return ``{"available": False}`` and the UI degrades gracefully.
"""

from __future__ import annotations

import os
from typing import Any

from sqlmodel import select

from app.database import session_scope
from app.models import ScanJob, ScanJobStatus
from app.services.indexer import JOB_CONTROLLER
from app.services.watcher import watching_ids


def system_metrics() -> dict[str, Any]:
    out: dict[str, Any] = {"available": False, "pid": os.getpid()}
    try:
        import psutil  # type: ignore
    except ImportError:
        return out
    proc = psutil.Process(os.getpid())
    with proc.oneshot():
        mem = proc.memory_info()
        out.update(
            {
                "available": True,
                "process_cpu_percent": proc.cpu_percent(interval=0.05),
                "process_rss_bytes": mem.rss,
                "process_threads": proc.num_threads(),
                "system_cpu_percent": psutil.cpu_percent(interval=0.05),
                "system_cpu_count": psutil.cpu_count(logical=True),
                "system_memory_total": psutil.virtual_memory().total,
                "system_memory_used": psutil.virtual_memory().used,
                "system_memory_percent": psutil.virtual_memory().percent,
            }
        )
    return out


def queue_metrics() -> dict[str, Any]:
    with session_scope() as session:
        running = session.exec(
            select(ScanJob).where(ScanJob.status == ScanJobStatus.running)
        ).all()
        paused = session.exec(
            select(ScanJob).where(ScanJob.status == ScanJobStatus.paused)
        ).all()
        queued = session.exec(
            select(ScanJob).where(ScanJob.status == ScanJobStatus.queued)
        ).all()
    return {
        "in_memory_jobs": list(JOB_CONTROLLER.keys()),
        "watchers_active": watching_ids(),
        "running": len(running),
        "paused": len(paused),
        "queued": len(queued),
        "running_details": [
            {
                "id": j.id,
                "source_id": j.source_id,
                "processed": j.processed_files,
                "total": j.total_files,
                "current": j.current_file,
            }
            for j in running
        ],
    }
