"""Tests for the per-source scan queue, coalescing, and Stop behaviour.

The scan overhaul serializes scans per source on an ``asyncio.Lock`` and creates
each ScanJob as ``queued`` before promoting it to ``running`` once the lock is
held. Extra requests coalesce *only* when their options are identical (so a
watcher flood collapses to one job but a Force re-scan is never swallowed), and
``abort_scan_job`` finalizes a still-queued job immediately.

These tests hold the source lock by hand so a ``run_scan_job`` coroutine parks
in the ``queued`` state deterministically, then assert the queue semantics.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

from app.database import init_db, session_scope
from app.models import DocumentSource, ScanJob, ScanJobStatus, SourceType, Visibility
from app.services import indexer


def _make_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DocumentSource:
    init_db()
    # Keep the heavy/networked deps out of the way — these tests only exercise
    # the queue state machine, never real extraction/embedding/vector writes.
    class _Client:
        async def preflight_embed(self) -> tuple[bool, str]:
            return True, "ok"

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 8 for _ in texts]

    monkeypatch.setattr(indexer, "get_client", lambda: _Client())
    monkeypatch.setattr(indexer, "ensure_ready", lambda: None)
    monkeypatch.setattr(indexer, "add_chunks", lambda **kw: None)
    monkeypatch.setattr(indexer, "delete_for_document", lambda *a, **kw: None)

    with session_scope() as session:
        s = DocumentSource(
            name="queue-test",
            type=SourceType.local,
            path=str(tmp_path),
            owner_id=None,
            visibility=Visibility.private,
        )
        session.add(s)
        session.flush()
        return DocumentSource(**s.model_dump())


async def _wait_for_queued_count(source_id: int, n: int, timeout: float = 2.0) -> None:
    elapsed = 0.0
    while elapsed < timeout:
        with session_scope() as s:
            cnt = len(
                s.exec(
                    select(ScanJob).where(
                        ScanJob.source_id == source_id,
                        ScanJob.status == ScanJobStatus.queued,
                    )
                ).all()
            )
        if cnt >= n:
            return
        await asyncio.sleep(0.02)
        elapsed += 0.02
    raise AssertionError(f"timed out waiting for {n} queued job(s) for source {source_id}")


def _queued(source_id: int) -> list[ScanJob]:
    with session_scope() as s:
        return list(
            s.exec(
                select(ScanJob).where(
                    ScanJob.source_id == source_id,
                    ScanJob.status == ScanJobStatus.queued,
                )
            ).all()
        )


@pytest.mark.asyncio
async def test_queue_coalesces_identical_options_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _make_source(tmp_path, monkeypatch)
    assert src.id is not None
    lock = indexer._source_lock(src.id)
    await lock.acquire()  # force every run_scan_job to park in 'queued'
    tasks: list[asyncio.Task[Any]] = []
    try:
        # First scan parks as the single queued job.
        tasks.append(asyncio.create_task(indexer.run_scan_job(src.id, phase="quick")))
        await _wait_for_queued_count(src.id, 1)
        q1_id = _queued(src.id)[0].id

        # Identical options coalesce: returns the existing job id, no new row.
        jid_same = await indexer.run_scan_job(src.id, phase="quick")
        assert jid_same == q1_id
        assert len(_queued(src.id)) == 1

        # Different options (a Force re-scan) must NOT be swallowed — it gets its
        # own queued job (and then parks on the lock).
        tasks.append(
            asyncio.create_task(indexer.run_scan_job(src.id, phase="ocr", force_ocr=True))
        )
        await _wait_for_queued_count(src.id, 2)
        assert len(_queued(src.id)) == 2
    finally:
        for t in tasks:
            t.cancel()
        lock.release()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_finalizes_queued_job_immediately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _make_source(tmp_path, monkeypatch)
    assert src.id is not None
    lock = indexer._source_lock(src.id)
    await lock.acquire()
    tasks: list[asyncio.Task[Any]] = []
    try:
        tasks.append(asyncio.create_task(indexer.run_scan_job(src.id, phase="quick")))
        await _wait_for_queued_count(src.id, 1)
        jid = _queued(src.id)[0].id
        assert jid is not None

        # Stop a job that hasn't started running yet — it finalizes at once,
        # without waiting for the (still-held) source lock.
        indexer.abort_scan_job(jid)
        with session_scope() as s:
            j = s.get(ScanJob, jid)
        assert j is not None
        assert j.status == ScanJobStatus.aborted
        assert j.ended_at is not None
    finally:
        for t in tasks:
            t.cancel()
        lock.release()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_scan_job_empty_source_completes_and_stamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _make_source(tmp_path, monkeypatch)
    assert src.id is not None

    job_id = await indexer.run_scan_job(src.id, phase="quick")

    with session_scope() as s:
        job = s.get(ScanJob, job_id)
        src_row = s.get(DocumentSource, src.id)
    assert job is not None
    assert job.status == ScanJobStatus.completed
    # last_scan_at is stamped on completion (and now also on abort/error).
    assert src_row is not None
    assert src_row.last_scan_at is not None
