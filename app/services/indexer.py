"""End-to-end indexing service.

Walks a source, ingests each PDF, runs OCR/vision/embeddings, persists
chunks to SQLite and Chroma, and updates a ScanJob with progress.

Runs as an asyncio task; cooperative pause/abort via the in-process
``JOB_CONTROLLER`` registry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import select

from app.config import get_settings
from app.database import session_scope
from app.database.engine import fts_delete_for_document, fts_insert
from app.ingestion.chunker import chunk_text, count_tokens
from app.ingestion.pdf_extractor import extract_pdf
from app.ingestion.providers import get_provider
from app.llm import get_client
from app.models import (
    ChunkSource,
    Document,
    DocumentChunk,
    DocumentImage,
    DocumentPage,
    DocumentSource,
    DocumentStatus,
    DocumentTagLink,
    ScanJob,
    ScanJobItem,
    ScanJobStatus,
    Tag,
)
from app.services.tagging import auto_tags, detect_doc_type, detect_language
from app.utils.hashing import sha256_file
from app.utils.logging import logger
from app.vectorstore import add_chunks, delete_for_document


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# In-memory controller for pause/abort/progress
# ---------------------------------------------------------------------------


@dataclass
class JobController:
    job_id: int
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    progress: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.pause_event.set()  # set = running, cleared = paused

    def pause(self) -> None:
        self.pause_event.clear()

    def resume(self) -> None:
        self.pause_event.set()

    def abort(self) -> None:
        self.abort_event.set()
        self.pause_event.set()

    async def gate(self) -> bool:
        """Returns False if aborted, True after passing the pause gate."""
        if self.abort_event.is_set():
            return False
        if not self.pause_event.is_set():
            await self.pause_event.wait()
        return not self.abort_event.is_set()


JOB_CONTROLLER: dict[int, JobController] = {}


# ---------------------------------------------------------------------------
# Index a single file
# ---------------------------------------------------------------------------


async def index_document(
    source: DocumentSource,
    path: Path,
    *,
    force_ocr: bool = False,
    force_vision: bool = False,
    force_embed: bool = False,
    controller: JobController | None = None,
) -> int | None:
    """Index a single file. Returns document_id or None on failure/skip."""
    s = get_settings()
    client = get_client()

    try:
        content_hash = sha256_file(path)
    except OSError as e:
        logger.warning("hash failed for {}: {}", path, e)
        return None

    # Upsert document row (just metadata, status=processing — old chunks stay
    # intact until the new ones are ready, so a crash mid-process leaves the
    # previous index usable).
    with session_scope() as session:
        doc = session.exec(select(Document).where(Document.path == str(path))).first()
        unchanged = (
            doc is not None
            and doc.content_hash == content_hash
            and not (force_ocr or force_vision or force_embed)
        )
        if unchanged:
            return doc.id  # type: ignore[return-value]

        try:
            stat = path.stat()
        except OSError as e:
            logger.warning("stat failed for {}: {}", path, e)
            return None

        if doc is None:
            doc = Document(
                source_id=source.id,  # type: ignore[arg-type]
                owner_id=source.owner_id,
                visibility=source.visibility,
                path=str(path),
                filename=path.name,
                extension=path.suffix.lower().lstrip("."),
                size_bytes=stat.st_size,
                content_hash=content_hash,
                created_at_fs=datetime.fromtimestamp(stat.st_ctime, tz=UTC),
                modified_at_fs=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                status=DocumentStatus.processing,
            )
        else:
            # Don't yet overwrite content_hash — only after the new index is in.
            doc.size_bytes = stat.st_size
            doc.modified_at_fs = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            doc.status = DocumentStatus.processing
        session.add(doc)
        session.flush()
        doc_id = doc.id

    # If we got here with the same content hash but force_* is set, we still
    # rebuild. If embeddings already exist and only force_embed is set, we can
    # short-circuit text extraction by re-using stored chunk text.
    reuse_text_only = (
        not force_ocr and not force_vision and force_embed and doc is not None and doc_id is not None
    )

    # Extract pages + images
    all_pages_text: list[tuple[int, str]] = []
    page_rows: list[DocumentPage] = []
    image_rows: list[DocumentImage] = []

    page_count = 0

    if reuse_text_only:
        # Re-embed only: pull existing pages/images from DB; skip extraction.
        with session_scope() as session:
            existing_pages = session.exec(
                select(DocumentPage)
                .where(DocumentPage.document_id == doc_id)
                .order_by(DocumentPage.page_number)
            ).all()
            existing_images = session.exec(
                select(DocumentImage).where(DocumentImage.document_id == doc_id)
            ).all()
        for p in existing_pages:
            page_count = max(page_count, p.page_number)
            combined = (p.native_text + "\n" + p.ocr_text).strip()
            all_pages_text.append((p.page_number, combined))
            page_rows.append(
                DocumentPage(
                    document_id=doc_id,  # type: ignore[arg-type]
                    page_number=p.page_number,
                    native_text=p.native_text,
                    ocr_text=p.ocr_text,
                    has_images=p.has_images,
                    has_tables=p.has_tables,
                    rendered_image_path=p.rendered_image_path,
                    width=p.width,
                    height=p.height,
                )
            )
        for i in existing_images:
            image_rows.append(
                DocumentImage(
                    document_id=doc_id,  # type: ignore[arg-type]
                    page_number=i.page_number,
                    image_index=i.image_index,
                    image_hash=i.image_hash,
                    width=i.width,
                    height=i.height,
                    cache_path=i.cache_path,
                    ocr_text=i.ocr_text,
                    vision_description=i.vision_description,
                    tags=list(i.tags or []),
                )
            )
        logger.info("re-embed only for {} (existing pages: {})", path.name, len(existing_pages))
        # Fall through to chunking + embedding below.
        skip_extract = True
    else:
        skip_extract = False

    # Run the heavy synchronous extraction (PyMuPDF + tesseract) on a worker
    # thread so the asyncio event loop stays free — otherwise the NiceGUI
    # websocket times out and the UI shows "connection lost".
    try:
        if skip_extract:
            extracted_pages: list = []
        else:

            def _extract_all() -> list:
                return list(
                    extract_pdf(
                        path,
                        doc_id_for_cache=doc_id or content_hash[:10],
                        force_ocr=force_ocr,
                    )
                )

            extracted_pages = await asyncio.to_thread(_extract_all)

        for pc in extracted_pages:
            page_count = pc.page_number
            if controller and not await controller.gate():
                logger.info("aborted during extract of {}", path)
                return None
            combined_text = (pc.native_text + "\n" + pc.ocr_text).strip()
            all_pages_text.append((pc.page_number, combined_text))
            page_rows.append(
                DocumentPage(
                    document_id=doc_id,  # type: ignore[arg-type]
                    page_number=pc.page_number,
                    native_text=pc.native_text[:200_000],
                    ocr_text=pc.ocr_text[:200_000],
                    has_images=pc.has_images,
                    has_tables=pc.has_tables,
                    rendered_image_path=pc.rendered_image_path or None,
                    width=pc.width,
                    height=pc.height,
                )
            )
            for img in pc.images:
                vision_desc = ""
                if force_vision and s.vision_model:
                    try:
                        vision_desc = await client.describe_image(img.cache_path)
                    except Exception as e:
                        logger.debug("vision failed: {}", e)
                image_rows.append(
                    DocumentImage(
                        document_id=doc_id,  # type: ignore[arg-type]
                        page_number=img.page_number,
                        image_index=img.image_index,
                        image_hash=img.image_hash,
                        width=img.width,
                        height=img.height,
                        cache_path=img.cache_path,
                        ocr_text=img.ocr_text[:50_000],
                        vision_description=vision_desc[:20_000],
                    )
                )
            # Yield to the event loop after each page so the UI's
            # websocket heartbeat and the per-job progress bar update.
            await asyncio.sleep(0)
    except Exception as e:
        logger.exception("extract failed for {}: {}", path, e)
        with session_scope() as session:
            d = session.get(Document, doc_id)
            if d:
                d.status = DocumentStatus.error
                d.error = str(e)[:500]
                session.add(d)
        return None

    # Chunk + embed (pages/images are *not yet* persisted — we do that
    # atomically with the chunks at the end so the document is never left in
    # a half-rebuilt state). Chunking is CPU-bound; off-thread it.
    chunks = await asyncio.to_thread(
        lambda: list(chunk_text(all_pages_text, chunk_tokens=s.chunk_size, overlap=s.chunk_overlap))
    )

    # Add image-description chunks too
    from app.ingestion.chunker import Chunk as _C

    for img in image_rows:
        text = (img.vision_description + "\n" + img.ocr_text).strip()
        if len(text) > 40:
            chunks.append(
                _C(
                    text=text,
                    page_from=img.page_number,
                    page_to=img.page_number,
                    token_count=count_tokens(text),
                )
            )

    # Tables via pdfplumber → markdown chunks (skip in re-embed-only mode);
    # pdfplumber is also synchronous, run in worker thread.
    table_chunks: list[tuple[_C, int]] = []  # (chunk, page_no)
    if not skip_extract:
        try:
            from app.ingestion.tables import extract_tables_markdown

            def _extract_tables() -> list[tuple[int, str]]:
                return list(extract_tables_markdown(path))

            for page_no, md in await asyncio.to_thread(_extract_tables):
                if len(md) < 30:
                    continue
                table_chunks.append(
                    (
                        _C(
                            text=md,
                            page_from=page_no,
                            page_to=page_no,
                            token_count=count_tokens(md),
                        ),
                        page_no,
                    )
                )
        except Exception as e:
            logger.debug("table extraction skipped: {}", e)

    # Generate embeddings in batches
    chunk_records: list[DocumentChunk] = []
    embedding_inputs: list[str] = []
    for ch in chunks:
        rec = DocumentChunk(
            document_id=doc_id,  # type: ignore[arg-type]
            page_from=ch.page_from,
            page_to=ch.page_to,
            text=ch.text,
            source=ChunkSource.native_text,
            token_count=ch.token_count,
        )
        chunk_records.append(rec)
        embedding_inputs.append(ch.text)
    # Append table chunks (so the chunker step doesn't mix them with prose)
    for tbl_chunk, _page in table_chunks:
        rec = DocumentChunk(
            document_id=doc_id,  # type: ignore[arg-type]
            page_from=tbl_chunk.page_from,
            page_to=tbl_chunk.page_to,
            text=tbl_chunk.text,
            source=ChunkSource.table,
            token_count=tbl_chunk.token_count,
            tags=["has:table"],
        )
        chunk_records.append(rec)
        embedding_inputs.append(tbl_chunk.text)

    embeddings: list[list[float]] = []
    if embedding_inputs:
        try:
            batch = 32
            for i in range(0, len(embedding_inputs), batch):
                if controller and not await controller.gate():
                    return None
                vecs = await client.embed(embedding_inputs[i : i + batch])
                embeddings.extend(vecs)
        except Exception as e:
            logger.warning("embedding failed for {}: {}", path, e)
            embeddings = []

    # If embeddings failed completely we abort — keep the old index intact.
    if chunk_records and not embeddings:
        logger.error("no embeddings produced for {} — aborting; old index preserved", path.name)
        with session_scope() as session:
            d = session.get(Document, doc_id)
            if d:
                d.status = DocumentStatus.error
                d.error = "embedding generation failed; previous index kept"
                session.add(d)
        return None

    # Atomic swap: delete old chunks/pages/images for this doc, then insert new
    # ones in a single session. Vector store + FTS mirror are cleared in the
    # same step.
    if doc_id is not None:
        delete_for_document(doc_id)
        fts_delete_for_document(doc_id)
    with session_scope() as session:
        for table in (DocumentChunk, DocumentPage, DocumentImage, DocumentTagLink):
            rows = session.exec(
                select(table).where(table.document_id == doc_id)  # type: ignore[arg-type]
            ).all()
            for r in rows:
                session.delete(r)
        session.flush()

        for r in page_rows:
            session.add(r)
        for r in image_rows:
            session.add(r)
        for rec in chunk_records:
            session.add(rec)
        session.flush()

        if embeddings and len(embeddings) == len(chunk_records):
            ids: list[str] = []
            docs_for_chroma: list[str] = []
            metas: list[dict[str, Any]] = []
            for rec, vec in zip(chunk_records, embeddings, strict=False):
                rec.embedding_id = str(rec.id)
                ids.append(str(rec.id))
                docs_for_chroma.append(rec.text)
                metas.append(
                    {
                        "document_id": int(doc_id) if doc_id else 0,
                        "source_id": int(source.id) if source.id else 0,
                        "page_from": rec.page_from,
                        "page_to": rec.page_to,
                        "filename": path.name,
                    }
                )
                session.add(rec)
            try:
                add_chunks(
                    ids=ids,
                    embeddings=embeddings,
                    documents=docs_for_chroma,
                    metadatas=metas,
                )
            except Exception as e:
                logger.warning("chroma upsert failed: {}", e)

            # FTS mirror
            for rec in chunk_records:
                if rec.id is not None and doc_id is not None:
                    fts_insert(rec.id, doc_id, rec.text, rec.tags or [])

        # Auto-tagging on aggregated text + vision descriptions
        agg_text = "\n".join(t for _, t in all_pages_text)[:50_000]
        vision_text = "\n".join((img.vision_description or "") for img in image_rows)[:20_000]
        tags = auto_tags(agg_text, vision_text=vision_text)
        for tag_name in tags:
            tag_obj = session.exec(select(Tag).where(Tag.name == tag_name)).first()
            if not tag_obj:
                tag_obj = Tag(name=tag_name, auto=True)
                session.add(tag_obj)
                session.flush()
            session.add(
                DocumentTagLink(document_id=doc_id, tag_id=tag_obj.id, auto=True)  # type: ignore[arg-type]
            )

        d = session.get(Document, doc_id)
        if d:
            d.page_count = page_count
            d.indexed_at = utcnow()
            d.status = DocumentStatus.indexed
            d.error = None
            d.language = detect_language(agg_text)
            d.doc_type = detect_doc_type(agg_text)
            d.content_hash = content_hash  # commit the new hash only on success
            session.add(d)

    logger.info("indexed {} ({} pages, {} chunks)", path.name, page_count, len(chunk_records))
    return doc_id


# ---------------------------------------------------------------------------
# Scan job runner
# ---------------------------------------------------------------------------


async def resume_scan_job(job_id: int) -> int:
    """Continue an existing job: process every ScanJobItem still in ``pending``
    or ``processing`` status. Used at startup to recover from crashes."""
    with session_scope() as session:
        job = session.get(ScanJob, job_id)
        if not job:
            raise ValueError(f"job {job_id} not found")
        if job.status in (ScanJobStatus.completed, ScanJobStatus.aborted):
            return job_id
        job.status = ScanJobStatus.running
        job.message = (job.message or "") + " | resumed"
        session.add(job)
        source = session.get(DocumentSource, job.source_id) if job.source_id else None
        source_snapshot = DocumentSource(**source.model_dump()) if source else None
        items = session.exec(
            select(ScanJobItem).where(
                ScanJobItem.job_id == job_id,
                ScanJobItem.status.in_(  # type: ignore[attr-defined]
                    [DocumentStatus.pending, DocumentStatus.processing]
                ),
            )
        ).all()
        item_snapshots = [(it.id, it.path) for it in items]
        options = dict(job.options or {})

    controller = JobController(job_id=job_id)
    JOB_CONTROLLER[job_id] = controller

    try:
        for item_id, item_path in item_snapshots:
            if not await controller.gate():
                with session_scope() as session:
                    j = session.get(ScanJob, job_id)
                    if j:
                        j.status = ScanJobStatus.aborted
                        j.ended_at = utcnow()
                        session.add(j)
                return job_id

            with session_scope() as session:
                j = session.get(ScanJob, job_id)
                if j:
                    j.current_file = Path(item_path).name
                    session.add(j)

            if source_snapshot is None or options.get("dry_run"):
                doc_id = None
            else:
                doc_id = await index_document(
                    source_snapshot,
                    Path(item_path),
                    force_ocr=options.get("force_ocr", False),
                    force_vision=options.get("force_vision", False),
                    force_embed=options.get("force_embed", False),
                    controller=controller,
                )

            with session_scope() as session:
                item = session.get(ScanJobItem, item_id)
                if item:
                    item.document_id = doc_id
                    item.status = DocumentStatus.indexed if doc_id else DocumentStatus.error
                    item.ended_at = utcnow()
                    session.add(item)
                j = session.get(ScanJob, job_id)
                if j:
                    j.processed_files = min(j.processed_files + 1, j.total_files or j.processed_files + 1)
                    if not doc_id:
                        j.error_count += 1
                    session.add(j)

        with session_scope() as session:
            j = session.get(ScanJob, job_id)
            if j:
                j.status = ScanJobStatus.completed
                j.ended_at = utcnow()
                j.current_file = None
                session.add(j)
    finally:
        JOB_CONTROLLER.pop(job_id, None)
    return job_id


async def recover_unfinished_jobs() -> int:
    """Mark ``running`` jobs as ``paused`` on startup and queue resume tasks."""
    with session_scope() as session:
        unfinished = session.exec(select(ScanJob).where(ScanJob.status == ScanJobStatus.running)).all()
        ids = [j.id for j in unfinished if j.id is not None]
        for j in unfinished:
            j.status = ScanJobStatus.paused
            j.message = (j.message or "") + " | recovered after restart"
            session.add(j)
    for jid in ids:
        asyncio.create_task(resume_scan_job(jid), name=f"resume-job-{jid}")
    if ids:
        logger.info("recovered {} unfinished scan job(s)", len(ids))
    return len(ids)


async def run_scan_job(
    source_id: int,
    *,
    force_ocr: bool = False,
    force_vision: bool = False,
    force_embed: bool = False,
    dry_run: bool = False,
) -> int:
    """Create a ScanJob and process all candidate files. Returns job_id."""

    with session_scope() as session:
        source = session.get(DocumentSource, source_id)
        if not source:
            raise ValueError(f"source {source_id} not found")
        job = ScanJob(
            source_id=source_id,
            status=ScanJobStatus.running,
            started_at=utcnow(),
            options={
                "force_ocr": force_ocr,
                "force_vision": force_vision,
                "force_embed": force_embed,
                "dry_run": dry_run,
            },
        )
        session.add(job)
        session.flush()
        job_id = job.id
        # detach for use outside the session
        source_snapshot = DocumentSource(**source.model_dump())

    if job_id is None:
        return 0
    controller = JobController(job_id=job_id)
    JOB_CONTROLLER[job_id] = controller

    try:
        # ----- LM Studio preflight ----------------------------------------
        # If embeddings aren't going to work, fail loudly *once* instead of
        # retrying for every PDF in a 5000-file source.
        if not dry_run:
            client = get_client()
            ok, message = await client.preflight_embed()
            if not ok:
                logger.error("scan {} aborted on preflight: {}", job_id, message)
                with session_scope() as session:
                    j = session.get(ScanJob, job_id)
                    if j:
                        j.status = ScanJobStatus.error
                        j.message = f"Embedding preflight failed: {message}"
                        j.ended_at = utcnow()
                        session.add(j)
                JOB_CONTROLLER.pop(job_id, None)
                return job_id

        provider = get_provider(source_snapshot)
        files = list(provider.iter_files(source_snapshot))
        with session_scope() as session:
            j = session.get(ScanJob, job_id)
            if j:
                j.total_files = len(files)
                session.add(j)

        for idx, df in enumerate(files, start=1):
            if not await controller.gate():
                with session_scope() as session:
                    j = session.get(ScanJob, job_id)
                    if j:
                        j.status = ScanJobStatus.aborted
                        j.ended_at = utcnow()
                        session.add(j)
                return job_id

            with session_scope() as session:
                item = ScanJobItem(
                    job_id=job_id,
                    path=df.remote_path,
                    status=DocumentStatus.processing,
                    started_at=utcnow(),
                )
                session.add(item)
                session.flush()
                item_id = item.id
                j = session.get(ScanJob, job_id)
                if j:
                    j.current_file = df.local_path.name
                    j.processed_files = idx - 1
                    session.add(j)

            if dry_run:
                doc_id = None
            else:
                doc_id = await index_document(
                    source_snapshot,
                    df.local_path,
                    force_ocr=force_ocr,
                    force_vision=force_vision,
                    force_embed=force_embed,
                    controller=controller,
                )

            with session_scope() as session:
                item = session.get(ScanJobItem, item_id)
                if item:
                    item.document_id = doc_id
                    item.status = DocumentStatus.indexed if doc_id else DocumentStatus.error
                    item.ended_at = utcnow()
                    session.add(item)
                j = session.get(ScanJob, job_id)
                if j:
                    j.processed_files = idx
                    if not doc_id:
                        j.error_count += 1
                    session.add(j)

        with session_scope() as session:
            source = session.get(DocumentSource, source_id)
            if source:
                source.last_scan_at = utcnow()
                session.add(source)
            j = session.get(ScanJob, job_id)
            if j:
                j.status = ScanJobStatus.completed
                j.ended_at = utcnow()
                j.current_file = None
                session.add(j)
    except Exception as e:
        logger.exception("scan job failed: {}", e)
        with session_scope() as session:
            j = session.get(ScanJob, job_id)
            if j:
                j.status = ScanJobStatus.error
                j.ended_at = utcnow()
                j.message = str(e)[:500]
                session.add(j)
    finally:
        JOB_CONTROLLER.pop(job_id, None)

    return job_id


def start_scan_in_background(source_id: int, **kwargs: Any) -> asyncio.Task:
    return asyncio.create_task(run_scan_job(source_id, **kwargs))
