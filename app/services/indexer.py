"""End-to-end indexing service.

Walks a source, ingests each PDF, runs OCR/vision/embeddings, persists
chunks to SQLite and Chroma, and updates a ScanJob with progress.

Runs as an asyncio task; cooperative pause/abort via the in-process
``JOB_CONTROLLER`` registry.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import select

from app.config import get_settings
from app.database import session_scope, write_session
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
from app.services.hardware import active_tuning
from app.services.tagging import auto_tags, detect_doc_type, detect_language
from app.utils.hashing import sha256_file
from app.utils.logging import logger
from app.vectorstore import add_chunks, delete_for_document, ensure_ready


def utcnow() -> datetime:
    return datetime.now(UTC)


def backfill_image_chunk_sources() -> int:
    """Relabel pre-existing image-description chunks as ``image_description``.

    Before this fix every chunk — including the ones built from a vision/OCR
    image description — was stored as ``ChunkSource.native_text``, so the RAG
    layer couldn't tell the model that a source describes an image actually
    embedded in the document. This walks the stored ``DocumentImage`` rows and
    relabels the matching chunks in place, so libraries indexed before the fix
    work without an expensive vision rescan. Idempotent and cheap — it only
    touches chunks that still match an image description and aren't labelled yet.
    """
    relabelled = 0
    try:
        with write_session() as session:
            imgs = session.exec(select(DocumentImage).where(DocumentImage.vision_description != "")).all()
            if not imgs:
                return 0
            want: dict[int, set[str]] = {}
            for im in imgs:
                txt = (im.vision_description + "\n" + im.ocr_text).strip()
                if len(txt) > 40:
                    want.setdefault(im.document_id, set()).add(txt)
            if not want:
                return 0
            chunks = session.exec(
                select(DocumentChunk).where(
                    DocumentChunk.document_id.in_(list(want.keys()))  # type: ignore[attr-defined]
                )
            ).all()
            for c in chunks:
                if c.source == ChunkSource.image_description:
                    continue
                if c.text.strip() in want.get(c.document_id, ()):
                    c.source = ChunkSource.image_description
                    if "has:image" not in (c.tags or []):
                        c.tags = [*(c.tags or []), "has:image"]
                    session.add(c)
                    relabelled += 1
    except Exception as e:
        logger.debug("image-chunk backfill skipped: {}", e)
        return 0
    if relabelled:
        logger.info("backfill: relabelled {} image-description chunks", relabelled)
    return relabelled


# Guards a vector-store rebuild so a startup heal and a manual rebuild can't run
# at the same time (each would re-embed the whole library).
_REEMBED_LOCK = asyncio.Lock()


async def reembed_all_documents(controller: JobController | None = None) -> int:
    """Re-generate embeddings for every indexed document and re-upsert them into
    the vector store, reusing the chunk text already stored in SQLite.

    Used to heal the index after the embedding model — and therefore the vector
    dimension — changed under it, which leaves Chroma rejecting every upsert and
    query ("Collection expecting embedding with dimension of X, got Y").
    """
    from app.vectorstore import ensure_ready

    ensure_ready()
    # Snapshot the work as plain values so we don't touch detached ORM objects
    # after the read session closes; rebuild a lightweight DocumentSource per
    # call (index_document only reads id/owner_id/visibility off it).
    with session_scope() as session:
        targets = [
            (d.path, d.source_id)
            for d in session.exec(select(Document)).all()
            if (d.page_count or 0) > 0 and d.source_id is not None
        ]
        srcs = {s.id: (s.owner_id, s.visibility) for s in session.exec(select(DocumentSource)).all()}

    count = 0
    for path_str, source_id in targets:
        if controller and not await controller.gate():
            break
        meta = srcs.get(source_id)
        if not meta:
            continue
        p = Path(path_str)
        if not p.exists():
            logger.debug("re-embed skip (missing file): {}", path_str)
            continue
        owner_id, visibility = meta
        src = DocumentSource(id=source_id, owner_id=owner_id, visibility=visibility)
        try:
            await index_document(src, p, force_embed=True, phase="text", controller=controller)
            count += 1
        except Exception as e:
            logger.warning("re-embed failed for {}: {}", path_str, e)
    logger.info("re-embed complete: {} document(s)", count)
    return count


async def heal_vector_store_if_model_changed() -> int:
    """If the configured embedding model produces a different vector dimension
    than the vectors already stored, rebuild the collection and re-embed every
    document. Best-effort and idempotent: once healed, the stored marker matches
    and subsequent calls are a cheap no-op. Returns the number of docs re-embedded
    (0 when nothing needed healing or LM Studio wasn't reachable to probe).
    """
    from app.vectorstore import (
        collection_dim,
        collection_size,
        query_dim_ok,
        read_embed_meta,
        reset_collection,
        write_embed_meta,
    )

    s = get_settings()
    model = s.embedding_model or ""
    if not model:
        return 0
    if _REEMBED_LOCK.locked():
        return 0

    client = get_client()
    try:
        probe = await client.embed(["dimension probe"])
    except Exception as e:
        # LM Studio not reachable yet — try again on the next startup.
        logger.debug("vector heal: embed probe skipped ({})", e)
        return 0
    if not probe or not probe[0]:
        return 0
    probe_dim = len(probe[0])

    size = collection_size()
    logger.info(
        "vector heal: model={!r} probe_dim={} stored_vectors={} stored_dim={}",
        model,
        probe_dim,
        size,
        collection_dim(),
    )

    # Authoritative check: run the *exact* query path production uses — NOT the
    # record count. A collection whose vectors were all deleted still keeps its
    # dimension pinned (Chroma fixes it at first write), so an "empty" collection
    # can keep rejecting every query with "expecting 1024, got 768". query_dim_ok
    # is True on a truly-fresh collection (no pinned dim → accepts any query) and
    # False only on a real dimension rejection. None ⇒ unrelated error: never wipe.
    ok = query_dim_ok(probe[0])
    if ok is None:
        logger.info("vector heal: dimension check inconclusive — leaving index as-is")
        return 0
    if ok:
        write_embed_meta(model, probe_dim)
        logger.info("vector heal: vector store matches the model (dim={}) — no action", probe_dim)
        return 0

    async with _REEMBED_LOCK:
        meta = read_embed_meta()
        logger.warning(
            "embedding model changed (stored model={!r}/dim={}, now {!r}/dim={}); "
            "rebuilding vector index and re-embedding {} document chunk-set(s)",
            meta.get("model"),
            collection_dim(),
            model,
            probe_dim,
            size,
        )
        reset_collection()
        write_embed_meta(model, probe_dim)
        return await reembed_all_documents()


# ---------------------------------------------------------------------------
# Vision-call concurrency cap
# ---------------------------------------------------------------------------
# Documents are indexed by several workers at once, and each describes its
# embedded images. Without a cap that means up to `workers` (≈8 on a fast box)
# simultaneous requests to the SAME vision model — which overloads LM Studio and
# surfaces as a "channel error" from the engine. The `vision_concurrency` tuning
# knob existed but was never enforced; this global semaphore enforces it across
# all concurrent index_document() calls. Keyed by event loop so each test (and
# the one real app loop) gets its own correctly-bound semaphore.
_VISION_SEM: asyncio.Semaphore | None = None
_VISION_SEM_LOOP: Any = None


def _vision_semaphore() -> asyncio.Semaphore:
    global _VISION_SEM, _VISION_SEM_LOOP
    loop = asyncio.get_running_loop()
    if _VISION_SEM is None or _VISION_SEM_LOOP is not loop:
        _VISION_SEM = asyncio.Semaphore(max(1, active_tuning().vision_concurrency))
        _VISION_SEM_LOOP = loop
    return _VISION_SEM


async def _describe_image_limited(client: Any, cache_path: str) -> str:
    """Describe one image, capped to `vision_concurrency` in-flight calls, with a
    hard per-image timeout and one retry on a transient engine hiccup (the
    intermittent "channel error"). Returns "" if it ultimately fails — the scan
    keeps going rather than hanging."""
    timeout = active_tuning().http_timeout + 30.0
    async with _vision_semaphore():
        for attempt in (1, 2):
            try:
                return await asyncio.wait_for(client.describe_image(cache_path), timeout=timeout)
            except Exception as e:
                logger.debug("vision describe failed (attempt {}/2): {}", attempt, e)
                if attempt == 1:
                    await asyncio.sleep(1.5)
    return ""


def _set_job_current_file(job_id: int, text: str) -> None:
    """Update a running scan job's ``current_file`` (the label the progress bar
    shows). Used to surface per-image vision progress so a document with hundreds
    of images doesn't make the bar look frozen. Best-effort, sync (call via
    ``to_thread``)."""
    with write_session() as session:
        j = session.get(ScanJob, job_id)
        if j and j.status == ScanJobStatus.running:
            j.current_file = text
            session.add(j)


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

# Live asyncio task per job, so the UI Stop button can force-cancel a wedged
# scan (e.g. one hung on a network read) that won't honor a cooperative abort.
JOB_TASKS: dict[int, asyncio.Task] = {}

# One scan runs at a time per source; extra requests queue behind this lock
# (and coalesce to at most one waiting job — see ``run_scan_job``).
_SOURCE_LOCKS: dict[int, asyncio.Lock] = {}


def _source_lock(source_id: int) -> asyncio.Lock:
    lock = _SOURCE_LOCKS.get(source_id)
    if lock is None:
        lock = asyncio.Lock()
        _SOURCE_LOCKS[source_id] = lock
    return lock


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
    phase: str = "full",
    controller: JobController | None = None,
) -> int | None:
    """Index a single file. Returns ``document_id`` or ``None`` on failure/skip.

    ``phase`` controls what work is actually done:
        * ``quick``  — only hash + Document row (fastest, hundreds/min).
        * ``text``   — native PDF text + embeddings, *no* OCR, *no* vision.
        * ``ocr``    — same as text but with OCR fallback on low-text pages.
        * ``vision`` — same as OCR plus vision descriptions of images.
        * ``full``   — backward-compatible: behaves like ocr + force_vision.

    ``force_ocr`` / ``force_vision`` still work (per-file overrides).
    """
    s = get_settings()
    client = get_client()

    # Phase resolution — what each phase enables/disables:
    do_ocr = force_ocr or phase == "ocr" or phase == "vision" or (phase == "full" and force_ocr)
    do_vision = force_vision or phase == "vision" or (phase == "full" and force_vision)
    catalog_only = phase == "quick"

    def _hash_and_upsert_sync() -> tuple[str, int | None, bool]:
        """Returns ``(content_hash, doc_id, unchanged)`` — all SQLite + sha256
        work runs here so the asyncio event loop isn't blocked."""
        try:
            ch = sha256_file(path)
        except OSError as e:
            logger.warning("hash failed for {}: {}", path, e)
            return "", None, False
        with write_session() as session:
            doc_row = session.exec(select(Document).where(Document.path == str(path))).first()
            has_real_index = doc_row is not None and (doc_row.page_count or 0) > 0
            is_unchanged = (
                doc_row is not None
                and doc_row.content_hash == ch
                and not (force_ocr or force_vision or force_embed)
                and (catalog_only or has_real_index)
            )
            # A Vision pass must NOT be skipped just because the file is
            # byte-identical: the doc was very likely indexed by an earlier
            # text/ocr phase and still has zero image descriptions. Only skip a
            # vision scan once the doc actually has vision data — otherwise the
            # scan silently no-ops in seconds and never describes any image.
            if is_unchanged and phase == "vision":
                has_vision = session.exec(
                    select(DocumentImage.id).where(
                        DocumentImage.document_id == doc_row.id,
                        DocumentImage.vision_description != "",
                    )
                ).first()
                if not has_vision:
                    is_unchanged = False
            if is_unchanged:
                return ch, doc_row.id, True
            try:
                stat = path.stat()
            except OSError as e:
                logger.warning("stat failed for {}: {}", path, e)
                return ch, None, False
            if doc_row is None:
                doc_row = Document(
                    source_id=source.id,  # type: ignore[arg-type]
                    owner_id=source.owner_id,
                    visibility=source.visibility,
                    path=str(path),
                    filename=path.name,
                    extension=path.suffix.lower().lstrip("."),
                    size_bytes=stat.st_size,
                    content_hash=ch,
                    created_at_fs=datetime.fromtimestamp(stat.st_ctime, tz=UTC),
                    modified_at_fs=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    status=DocumentStatus.processing,
                )
            else:
                doc_row.size_bytes = stat.st_size
                doc_row.modified_at_fs = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
                doc_row.status = DocumentStatus.processing
            session.add(doc_row)
            session.flush()
            return ch, doc_row.id, False

    content_hash, doc_id, unchanged = await asyncio.to_thread(_hash_and_upsert_sync)
    if unchanged:
        return doc_id
    if not content_hash or doc_id is None:
        return None

    # Quick / catalog-only phase: just record the file in the index and stop.
    # This lets the user see all PDFs as a browsable list within seconds —
    # text extraction + embeddings can run as a later phase.
    if catalog_only:

        def _mark_indexed_sync() -> None:
            with write_session() as session:
                d = session.get(Document, doc_id)
                if d:
                    d.status = DocumentStatus.indexed
                    d.content_hash = content_hash
                    d.error = None
                    session.add(d)

        await asyncio.to_thread(_mark_indexed_sync)
        logger.info("cataloged {} (quick phase)", path.name)
        return doc_id

    # If we got here with the same content hash but force_* is set, we still
    # rebuild. If embeddings already exist and only force_embed is set, we can
    # short-circuit text extraction by re-using stored chunk text.
    reuse_text_only = not force_ocr and not force_vision and force_embed and doc_id is not None

    # Extract pages + images
    all_pages_text: list[tuple[int, str]] = []
    page_rows: list[DocumentPage] = []
    image_rows: list[DocumentImage] = []

    page_count = 0

    if reuse_text_only:
        # Re-embed only: pull existing pages/images from DB; skip extraction.
        def _load_existing_sync() -> tuple[list, list]:
            with session_scope() as session:  # read-only — no write lock needed
                ep = session.exec(
                    select(DocumentPage)
                    .where(DocumentPage.document_id == doc_id)
                    .order_by(DocumentPage.page_number)
                ).all()
                ei = session.exec(select(DocumentImage).where(DocumentImage.document_id == doc_id)).all()
                return list(ep), list(ei)

        existing_pages, existing_images = await asyncio.to_thread(_load_existing_sync)
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
                        force_ocr=do_ocr,
                        extract_images=do_vision,
                    )
                )

            extracted_pages = await asyncio.to_thread(_extract_all)

        _vis_done = 0  # images described in this doc (for live progress)
        _vis_last_note = 0.0
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
                if do_vision and s.vision_model:
                    # Concurrency-capped (+1 retry) so parallel document workers
                    # don't flood the vision model and trigger engine errors.
                    vision_desc = await _describe_image_limited(client, img.cache_path)
                    # Surface per-image progress: a doc with hundreds of images
                    # otherwise leaves the bar's label frozen for many minutes.
                    _vis_done += 1
                    if controller is not None and (time.monotonic() - _vis_last_note) > 1.5:
                        _vis_last_note = time.monotonic()
                        try:
                            await asyncio.to_thread(
                                _set_job_current_file,
                                controller.job_id,
                                f"{path.name} · image {_vis_done}",
                            )
                        except Exception as e:
                            logger.debug("progress note failed: {}", e)
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
        err = str(e)[:500]

        def _mark_extract_error_sync() -> None:
            with write_session() as session:
                d = session.get(Document, doc_id)
                if d:
                    d.status = DocumentStatus.error
                    d.error = err
                    session.add(d)

        # Off the event loop: the write lock may be held by a sibling worker,
        # and blocking the loop on it would stall the UI/websocket.
        await asyncio.to_thread(_mark_extract_error_sync)
        return None

    # Chunk + embed (pages/images are *not yet* persisted — we do that
    # atomically with the chunks at the end so the document is never left in
    # a half-rebuilt state). Chunking is CPU-bound; off-thread it.
    chunks = await asyncio.to_thread(
        lambda: list(chunk_text(all_pages_text, chunk_tokens=s.chunk_size, overlap=s.chunk_overlap))
    )

    from app.ingestion.chunker import Chunk as _C

    # Image-description chunks are kept separate from prose so they can carry
    # ChunkSource.image_description. The RAG layer relies on that marker to tell
    # the model "this text describes an image embedded in the document" — which
    # is what makes "which documents have a photo of X?" answerable instead of
    # the model insisting it sees no images (they were stored as native_text
    # before, so the image signal was lost on the way into the prompt).
    image_chunks: list[_C] = []
    for img in image_rows:
        text = (img.vision_description + "\n" + img.ocr_text).strip()
        if len(text) > 40:
            image_chunks.append(
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
    # Image-description chunks — tagged so the RAG layer can flag them as images
    for ch in image_chunks:
        rec = DocumentChunk(
            document_id=doc_id,  # type: ignore[arg-type]
            page_from=ch.page_from,
            page_to=ch.page_to,
            text=ch.text,
            source=ChunkSource.image_description,
            token_count=ch.token_count,
            tags=["has:image"],
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
            batch = active_tuning().embed_batch
            for i in range(0, len(embedding_inputs), batch):
                if controller and not await controller.gate():
                    return None
                vecs = await client.embed(embedding_inputs[i : i + batch])
                embeddings.extend(vecs)
                # Let the event loop breathe between embedding batches
                await asyncio.sleep(0)
        except Exception as e:
            logger.warning("embedding failed for {}: {}", path, e)
            embeddings = []

    # If embeddings failed completely we abort — keep the old index intact.
    if chunk_records and not embeddings:
        logger.error("no embeddings produced for {} — aborting; old index preserved", path.name)

        def _mark_embed_error_sync() -> None:
            with write_session() as session:
                d = session.get(Document, doc_id)
                if d:
                    d.status = DocumentStatus.error
                    d.error = "embedding generation failed; previous index kept"
                    session.add(d)

        await asyncio.to_thread(_mark_embed_error_sync)
        return None

    # Atomic swap: delete old chunks/pages/images, then insert new ones.
    # All synchronous; runs off the event loop so the UI stays responsive
    # for big documents (1000+ chunks = lots of SQLite writes + Chroma upserts).
    def _persist_index_sync() -> None:
        # Heavy/slow work is deliberately kept OUT of the write transaction so
        # the global write lock (held by write_session below) isn't pinned
        # across it: tag detection is pure CPU on the aggregated text, and the
        # Chroma vector store is a *separate* database that needs no SQLite
        # lock. The lock therefore wraps only the SQLite writes — concurrent
        # indexer workers serialize for milliseconds, not across the Chroma
        # upsert, which is what previously tripped "database is locked".
        agg_text = "\n".join(t for _, t in all_pages_text)[:50_000]
        vision_text = "\n".join((img.vision_description or "") for img in image_rows)[:20_000]
        tags = auto_tags(agg_text, vision_text=vision_text)
        language = detect_language(agg_text)
        doc_type = detect_doc_type(agg_text)

        if doc_id is not None:
            delete_for_document(doc_id)  # Chroma — outside the SQLite write lock

        # Collected inside the transaction (needs the flushed chunk ids) but
        # upserted to Chroma *after* the SQLite commit, so the lock isn't held
        # across the vector write.
        chroma_ids: list[str] = []
        chroma_docs: list[str] = []
        chroma_embeddings: list[list[float]] = []
        chroma_metas: list[dict[str, Any]] = []

        with write_session() as session:
            conn = session.connection()
            if doc_id is not None:
                # Same transaction as the chunk rows → atomic, and (crucially)
                # no second connection fighting the session for the write lock.
                fts_delete_for_document(doc_id, conn=conn)
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
                for rec, vec in zip(chunk_records, embeddings, strict=False):
                    rec.embedding_id = str(rec.id)
                    session.add(rec)
                    chroma_ids.append(str(rec.id))
                    chroma_docs.append(rec.text)
                    chroma_embeddings.append(vec)
                    chroma_metas.append(
                        {
                            "document_id": int(doc_id) if doc_id else 0,
                            "source_id": int(source.id) if source.id else 0,
                            "page_from": rec.page_from,
                            "page_to": rec.page_to,
                            "filename": path.name,
                        }
                    )
                    if rec.id is not None and doc_id is not None:
                        fts_insert(rec.id, doc_id, rec.text, rec.tags or [], conn=conn)

            # Auto-tagging on aggregated text + vision descriptions
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
                d.language = language
                d.doc_type = doc_type
                d.content_hash = content_hash  # commit the new hash only on success
                session.add(d)

        # Vector upsert after the SQLite commit: the lock is released, and a
        # Chroma failure can no longer roll back the (now durable) chunk rows —
        # it just means those chunks aren't vector-searchable until a reindex.
        if chroma_ids:
            try:
                add_chunks(
                    ids=chroma_ids,
                    embeddings=chroma_embeddings,
                    documents=chroma_docs,
                    metadatas=chroma_metas,
                )
            except Exception as e:
                logger.warning("chroma upsert failed: {}", e)

    await asyncio.to_thread(_persist_index_sync)

    logger.info("indexed {} ({} pages, {} chunks)", path.name, page_count, len(chunk_records))
    return doc_id


# ---------------------------------------------------------------------------
# Scan job runner
# ---------------------------------------------------------------------------


async def resume_scan_job(job_id: int) -> int:
    """Continue an existing job: process every ScanJobItem still in ``pending``
    or ``processing`` status. Used at startup to recover from crashes."""
    with write_session() as session:
        job = session.get(ScanJob, job_id)
        if not job:
            raise ValueError(f"job {job_id} not found")
        if job.status in (ScanJobStatus.completed, ScanJobStatus.aborted):
            return job_id
        source_id = job.source_id
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

    phase = options.get("phase", "full")
    controller = JobController(job_id=job_id)
    JOB_CONTROLLER[job_id] = controller
    JOB_TASKS[job_id] = asyncio.current_task()  # type: ignore[assignment]
    src_lock = _source_lock(source_id) if source_id is not None else asyncio.Lock()
    acquired = False

    tuning = active_tuning()
    concurrency = tuning.quick_workers if phase == "quick" else tuning.workers
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()

    # Warm the vector store once so the resumed workers don't race its
    # first-time client/tenant initialization (quick phase never embeds).
    if phase != "quick" and not options.get("dry_run"):
        await asyncio.to_thread(ensure_ready)

    def _set_current_sync(name: str) -> None:
        with write_session() as session:
            jj = session.get(ScanJob, job_id)
            if jj:
                jj.current_file = name
                session.add(jj)

    def _finalize_item_sync(
        item_id_v: int | None,
        doc_id_v: int | None,
    ) -> None:
        with write_session() as session:
            item = session.get(ScanJobItem, item_id_v)
            if item:
                item.document_id = doc_id_v
                item.status = DocumentStatus.indexed if doc_id_v else DocumentStatus.error
                item.ended_at = utcnow()
                session.add(item)

    def _bump_progress_sync(failed: bool) -> None:
        with write_session() as session:
            jj = session.get(ScanJob, job_id)
            if jj:
                jj.processed_files = min(
                    jj.processed_files + 1,
                    jj.total_files or jj.processed_files + 1,
                )
                if failed:
                    jj.error_count += 1
                session.add(jj)

    async def _process_one(item_id: int | None, item_path: str) -> None:
        async with sem:
            if not await controller.gate():
                return

            await asyncio.to_thread(_set_current_sync, Path(item_path).name)

            if source_snapshot is None or options.get("dry_run"):
                doc_id: int | None = None
            else:
                try:
                    doc_id = await index_document(
                        source_snapshot,
                        Path(item_path),
                        force_ocr=options.get("force_ocr", False),
                        force_vision=options.get("force_vision", False),
                        force_embed=options.get("force_embed", False),
                        phase=phase,
                        controller=controller,
                    )
                except Exception as e:
                    logger.exception("index_document crashed for {}: {}", item_path, e)
                    doc_id = None

            await asyncio.to_thread(_finalize_item_sync, item_id, doc_id)
            async with lock:
                await asyncio.to_thread(_bump_progress_sync, not doc_id)
            await asyncio.sleep(0)

    try:
        # Serialize on the source lock, same as run_scan_job, so a resumed job
        # and a fresh scan never index the same source concurrently.
        await src_lock.acquire()
        acquired = True
        with write_session() as session:
            jj = session.get(ScanJob, job_id)
            if jj and jj.status not in (ScanJobStatus.completed, ScanJobStatus.aborted):
                jj.status = ScanJobStatus.running
                jj.message = (jj.message or "") + " | resumed"
                session.add(jj)

        tasks = [asyncio.create_task(_process_one(it_id, it_path)) for it_id, it_path in item_snapshots]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise

        if controller.abort_event.is_set():
            with write_session() as session:
                source = session.get(DocumentSource, source_id) if source_id is not None else None
                if source:
                    source.last_scan_at = utcnow()
                    session.add(source)
                j = session.get(ScanJob, job_id)
                if j:
                    j.status = ScanJobStatus.aborted
                    j.ended_at = utcnow()
                    j.current_file = None
                    session.add(j)
            return job_id

        with write_session() as session:
            source = session.get(DocumentSource, source_id) if source_id is not None else None
            if source:
                source.last_scan_at = utcnow()
                session.add(source)
            j = session.get(ScanJob, job_id)
            if j:
                j.status = ScanJobStatus.completed
                j.ended_at = utcnow()
                j.current_file = None
                session.add(j)
    except asyncio.CancelledError:
        with write_session() as session:
            j = session.get(ScanJob, job_id)
            if j and j.status not in (ScanJobStatus.completed, ScanJobStatus.error):
                j.status = ScanJobStatus.aborted
                j.ended_at = utcnow()
                j.current_file = None
                session.add(j)
        raise
    except Exception as e:
        logger.exception("resume scan job failed: {}", e)
        with write_session() as session:
            j = session.get(ScanJob, job_id)
            if j:
                j.status = ScanJobStatus.error
                j.ended_at = utcnow()
                j.message = str(e)[:500]
                session.add(j)
    finally:
        if acquired:
            src_lock.release()
        JOB_CONTROLLER.pop(job_id, None)
        JOB_TASKS.pop(job_id, None)
    return job_id


async def recover_unfinished_jobs() -> int:
    """Mark ``running`` jobs as ``paused`` on startup and queue resume tasks."""
    with write_session() as session:
        # Both running and paused jobs were in-flight at exit — resume both. (A
        # paused job that gets no resume would render a Resume button wired to a
        # controller that no longer exists: a zombie clearable only by Stop.)
        unfinished = session.exec(
            select(ScanJob).where(
                ScanJob.status.in_([ScanJobStatus.running, ScanJobStatus.paused])  # type: ignore[attr-defined]
            )
        ).all()
        ids = [j.id for j in unfinished if j.id is not None]
        for j in unfinished:
            j.status = ScanJobStatus.paused
            j.message = (j.message or "") + " | recovered after restart"
            session.add(j)
        # Queued jobs never started running — their coroutines died with the old
        # process, so nothing will ever pick them up. Abort them.
        for j in session.exec(select(ScanJob).where(ScanJob.status == ScanJobStatus.queued)).all():
            j.status = ScanJobStatus.aborted
            j.ended_at = utcnow()
            j.message = (j.message or "") + " | abandoned (restart)"
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
    phase: str = "full",
) -> int:
    """Create a ScanJob and process all candidate files.

    ``phase`` is passed straight through to ``index_document`` and changes
    how much work is done per file. The default ``full`` keeps backward
    compatibility with all the existing UI buttons; the new fast variants
    are ``quick``, ``text``, ``ocr`` and ``vision``.
    """

    new_options = {
        "force_ocr": force_ocr,
        "force_vision": force_vision,
        "force_embed": force_embed,
        "dry_run": dry_run,
        "phase": phase,
    }
    with write_session() as session:
        source = session.get(DocumentSource, source_id)
        if not source:
            raise ValueError(f"source {source_id} not found")
        # Coalesce ONLY when an identical request is already queued: a watcher
        # flood (same options) collapses to a single job, but a heavier request
        # (e.g. a Force re-scan with force_* flags or a different phase) is never
        # swallowed by a lighter queued scan — it gets its own queued job.
        pending = session.exec(
            select(ScanJob)
            .where(ScanJob.source_id == source_id, ScanJob.status == ScanJobStatus.queued)
            .order_by(ScanJob.id.desc())  # type: ignore[attr-defined]
        ).first()
        if pending is not None and (pending.options or {}) == new_options:
            return pending.id or 0
        job = ScanJob(
            source_id=source_id,
            status=ScanJobStatus.queued,
            options=new_options,
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
    JOB_TASKS[job_id] = asyncio.current_task()  # type: ignore[assignment]
    src_lock = _source_lock(source_id)
    acquired = False

    try:
        # Serialize per source: one scan at a time. Extra requests wait here as
        # 'queued' (the event loop stays free) until the running scan releases
        # the lock — then we promote this job to 'running'. NOTE: this is the
        # per-source lock; ``lock`` further down is the (separate) per-item
        # progress lock — keep the names distinct.
        await src_lock.acquire()
        acquired = True
        if controller.abort_event.is_set():
            # Stopped while still queued — finalize without doing any work.
            with write_session() as session:
                j = session.get(ScanJob, job_id)
                if j and j.status != ScanJobStatus.aborted:
                    j.status = ScanJobStatus.aborted
                    j.ended_at = utcnow()
                    session.add(j)
            return job_id
        with write_session() as session:
            j = session.get(ScanJob, job_id)
            if j:
                j.status = ScanJobStatus.running
                j.started_at = utcnow()
                session.add(j)

        # ----- LM Studio preflight ----------------------------------------
        # If embeddings aren't going to work, fail loudly *once* instead of
        # retrying for every PDF in a 5000-file source.  Quick phase only
        # catalogs filenames so embeddings aren't required.
        if not dry_run and phase != "quick":
            client = get_client()
            # Bound the preflight: a hung LM Studio must not stall the scan start
            # indefinitely (the job is already promoted to 'running' here).
            try:
                ok, message = await asyncio.wait_for(
                    client.preflight_embed(),
                    timeout=active_tuning().http_timeout + 10,
                )
            except TimeoutError:  # asyncio.TimeoutError is an alias of this on 3.11+
                ok, message = False, "embedding preflight timed out"
            if not ok:
                logger.error("scan {} aborted on preflight: {}", job_id, message)
                with write_session() as session:
                    j = session.get(ScanJob, job_id)
                    if j:
                        j.status = ScanJobStatus.error
                        j.message = f"Embedding preflight failed: {message}"
                        j.ended_at = utcnow()
                        session.add(j)
                JOB_CONTROLLER.pop(job_id, None)
                return job_id

        provider = get_provider(source_snapshot)
        # Enumerate off the event loop — a big local walk or a slow SMB/WebDAV/
        # SFTP listing must not block the shared NiceGUI loop (it freezes every
        # open tab, and the progress pollers can't even fire).
        files = await asyncio.to_thread(lambda: list(provider.iter_files(source_snapshot)))
        with write_session() as session:
            j = session.get(ScanJob, job_id)
            if j:
                j.total_files = len(files)
                session.add(j)

        # Warm the vector store once so concurrent workers don't race its
        # first-time client/tenant initialization (quick phase never embeds).
        if not dry_run and phase != "quick":
            await asyncio.to_thread(ensure_ready)

        # Parallel file processing: the quick phase scales widely (just hashing
        # + SQLite writes), the heavier phases stay bounded so OCR/embedding/
        # Chroma don't thrash. Both counts come from the hardware profile.
        tuning = active_tuning()
        concurrency = tuning.quick_workers if phase == "quick" else tuning.workers
        sem = asyncio.Semaphore(concurrency)
        processed = 0
        errors = 0
        lock = asyncio.Lock()

        def _open_item_sync(remote_path: str, local_name: str) -> int | None:
            with write_session() as session:
                item = ScanJobItem(
                    job_id=job_id,
                    path=remote_path,
                    status=DocumentStatus.processing,
                    started_at=utcnow(),
                )
                session.add(item)
                session.flush()
                jj = session.get(ScanJob, job_id)
                if jj:
                    jj.current_file = local_name
                    session.add(jj)
                return item.id

        def _close_item_sync(
            item_id_v: int | None,
            doc_id_v: int | None,
            snap_processed: int,
            snap_errors: int,
        ) -> None:
            with write_session() as session:
                item = session.get(ScanJobItem, item_id_v)
                if item:
                    item.document_id = doc_id_v
                    item.status = DocumentStatus.indexed if doc_id_v else DocumentStatus.error
                    item.ended_at = utcnow()
                    session.add(item)
                jj = session.get(ScanJob, job_id)
                if jj:
                    jj.processed_files = snap_processed
                    jj.error_count = snap_errors
                    session.add(jj)

        async def _process_one(idx: int, df: Any) -> None:
            nonlocal processed, errors
            async with sem:
                if not await controller.gate():
                    return

                # SQLite writes go through a thread pool so the asyncio loop
                # stays responsive for the UI websocket and other tasks.
                item_id = await asyncio.to_thread(_open_item_sync, df.remote_path, df.local_path.name)

                if dry_run:
                    doc_id: int | None = None
                else:
                    try:
                        doc_id = await index_document(
                            source_snapshot,
                            df.local_path,
                            force_ocr=force_ocr,
                            force_vision=force_vision,
                            force_embed=force_embed,
                            phase=phase,
                            controller=controller,
                        )
                    except Exception as e:
                        logger.exception("index_document crashed for {}: {}", df.local_path, e)
                        doc_id = None

                async with lock:
                    processed += 1
                    if not doc_id:
                        errors += 1
                    snap_processed = processed
                    snap_errors = errors

                await asyncio.to_thread(_close_item_sync, item_id, doc_id, snap_processed, snap_errors)
                await asyncio.sleep(0)

        tasks = [asyncio.create_task(_process_one(idx, df)) for idx, df in enumerate(files, start=1)]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise

        if controller.abort_event.is_set():
            with write_session() as session:
                source = session.get(DocumentSource, source_id)
                if source:
                    source.last_scan_at = utcnow()
                    session.add(source)
                j = session.get(ScanJob, job_id)
                if j:
                    j.status = ScanJobStatus.aborted
                    j.ended_at = utcnow()
                    j.current_file = None
                    session.add(j)
            return job_id

        with write_session() as session:
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
    except asyncio.CancelledError:
        # Force-cancelled (the Stop watchdog killed a wedged scan). Mark the job
        # aborted so it doesn't linger as 'running', then re-raise.
        with write_session() as session:
            source = session.get(DocumentSource, source_id)
            if source:
                source.last_scan_at = utcnow()
                session.add(source)
            j = session.get(ScanJob, job_id)
            if j and j.status not in (ScanJobStatus.completed, ScanJobStatus.error):
                j.status = ScanJobStatus.aborted
                j.ended_at = utcnow()
                j.current_file = None
                session.add(j)
        raise
    except Exception as e:
        logger.exception("scan job failed: {}", e)
        with write_session() as session:
            source = session.get(DocumentSource, source_id)
            if source:
                source.last_scan_at = utcnow()
                session.add(source)
            j = session.get(ScanJob, job_id)
            if j:
                j.status = ScanJobStatus.error
                j.ended_at = utcnow()
                j.message = str(e)[:500]
                session.add(j)
    finally:
        if acquired:
            src_lock.release()
        JOB_CONTROLLER.pop(job_id, None)
        JOB_TASKS.pop(job_id, None)

    return job_id


def start_scan_in_background(source_id: int, **kwargs: Any) -> asyncio.Task:
    return asyncio.create_task(run_scan_job(source_id, **kwargs))


# ---------------------------------------------------------------------------
# UI-facing controls (Stop / Pause / Resume)
# ---------------------------------------------------------------------------


async def _cancel_if_alive(job_id: int, grace: float) -> None:
    """After a graceful Stop, force-cancel a scan that hasn't stopped within
    ``grace`` seconds (e.g. one wedged on a hung network read). Safe: each
    document is persisted atomically, so an abandoned in-flight file is simply
    re-tried on the next scan."""
    try:
        await asyncio.sleep(grace)
    except asyncio.CancelledError:
        return
    task = JOB_TASKS.get(job_id)
    if task is not None and not task.done():
        logger.warning("scan job {} ignored stop for {}s — force-cancelling", job_id, grace)
        task.cancel()


def abort_scan_job(job_id: int, *, grace: float = 45.0) -> None:
    """Stop a scan from the UI. Cooperative first (the in-flight file is allowed
    to finish — file-boundary), then a watchdog force-cancels if it doesn't stop
    within ``grace`` seconds (so a wedged scan can't block the queue forever)."""
    ctrl = JOB_CONTROLLER.get(job_id)
    if ctrl is not None:
        ctrl.abort()
    with write_session() as session:
        j = session.get(ScanJob, job_id)
        if j is None:
            return
        # A queued job never started; a job whose controller is gone is a zombie
        # (lost across a restart). Finalize either immediately. A live running/
        # paused job is left for its worker to flip to aborted at the next file
        # boundary, so its counters/current_file settle correctly.
        if j.status == ScanJobStatus.queued or (
            ctrl is None and j.status in (ScanJobStatus.running, ScanJobStatus.paused)
        ):
            j.status = ScanJobStatus.aborted
            j.ended_at = utcnow()
            j.current_file = None
            session.add(j)
            return
    if ctrl is not None and JOB_TASKS.get(job_id) is not None:
        try:
            asyncio.get_running_loop()
            asyncio.create_task(_cancel_if_alive(job_id, grace), name=f"abort-watchdog-{job_id}")
        except RuntimeError:
            pass  # no running loop (called off the event loop) — skip the watchdog
