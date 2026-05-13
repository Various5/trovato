"""Scan job endpoints — start, status, pause/resume/abort."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth.security import login_required
from app.database import get_session
from app.models import ScanJob, User
from app.services.indexer import JOB_CONTROLLER, resume_scan_job, start_scan_in_background


router = APIRouter()


class ScanStartBody(BaseModel):
    source_id: int
    force_ocr: bool = False
    force_vision: bool = False
    force_embed: bool = False
    dry_run: bool = False


@router.post("/start")
def start(body: ScanStartBody, _user: User = Depends(login_required)) -> dict[str, Any]:
    task = start_scan_in_background(
        body.source_id,
        force_ocr=body.force_ocr,
        force_vision=body.force_vision,
        force_embed=body.force_embed,
        dry_run=body.dry_run,
    )
    return {"scheduled": True, "task_name": task.get_name()}


@router.get("/jobs")
def list_jobs(
    _user: User = Depends(login_required),
    session: Session = Depends(get_session),
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = session.exec(select(ScanJob).order_by(ScanJob.id.desc()).limit(limit)).all()
    return [r.model_dump(mode="json") for r in rows]


@router.get("/jobs/{job_id}")
def get_job(
    job_id: int,
    _user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    j = session.get(ScanJob, job_id)
    if not j:
        raise HTTPException(status_code=404, detail="not found")
    return j.model_dump(mode="json")


@router.post("/jobs/{job_id}/pause")
def pause_job(job_id: int, _user: User = Depends(login_required)) -> dict[str, str]:
    ctrl = JOB_CONTROLLER.get(job_id)
    if not ctrl:
        raise HTTPException(status_code=404, detail="job not running")
    ctrl.pause()
    return {"status": "paused"}


@router.post("/jobs/{job_id}/resume")
def resume_job(job_id: int, _user: User = Depends(login_required)) -> dict[str, str]:
    ctrl = JOB_CONTROLLER.get(job_id)
    if not ctrl:
        raise HTTPException(status_code=404, detail="job not running")
    ctrl.resume()
    return {"status": "running"}


@router.post("/jobs/{job_id}/resume_job")
def resume_job_endpoint(job_id: int, _user: User = Depends(login_required)) -> dict[str, Any]:
    import asyncio

    asyncio.create_task(resume_scan_job(job_id), name=f"resume-{job_id}")
    return {"resuming": True}


@router.post("/jobs/{job_id}/abort")
def abort_job(job_id: int, _user: User = Depends(login_required)) -> dict[str, str]:
    ctrl = JOB_CONTROLLER.get(job_id)
    if not ctrl:
        raise HTTPException(status_code=404, detail="job not running")
    ctrl.abort()
    return {"status": "aborting"}
