# Architecture

## Layers

```
┌─────────────────────────────────────────────────────────────────────┐
│                          NiceGUI Pages                              │
│  Login · Wizard · Dashboard · Documents · Search · Chat · Sources   │
│  Tags · Backup · Settings · Logs · About                            │
└──────────────────────────────▲──────────────────────────────────────┘
                               │ (in-process)
┌──────────────────────────────┴──────────────────────────────────────┐
│                       FastAPI REST API                              │
│  /api/auth · /api/sources · /api/scan · /api/documents · /api/...   │
└──────────────────────────────▲──────────────────────────────────────┘
                               │
            ┌──────────────────┴─────────────────────┐
            │                                        │
   ┌────────▼────────┐                     ┌─────────▼────────┐
   │   Services      │                     │   Background      │
   │  · indexer      │  ◀── async tasks ──▶│   job runner       │
   │  · search       │                     │  (asyncio)         │
   │  · tagging      │                     └────────────────────┘
   │  · rag/chat     │
   │  · backup       │
   └──┬───────┬──┬───┘
      │       │  │
      │       │  ▼
      │       │  ┌──────────────────────────────┐
      │       │  │  Ingestion / OCR / Vision    │
      │       │  │  · PyMuPDF + OpenCV + tess.  │
      │       │  │  · LM Studio vision call     │
      │       │  └──────────────────────────────┘
      │       │
      │       ▼
      │  ┌─────────────────┐
      │  │  Vector store   │  ChromaDB (persistent)
      │  └─────────────────┘
      ▼
┌──────────────────────────────────────────────────────────┐
│              SQLite + FTS5 (SQLModel/Alembic)            │
└──────────────────────────────────────────────────────────┘
```

## Data flow (ingestion)

1. **Scanner** walks the source directory (glob + size + hidden filters).
2. For each file, **SHA256** is computed and compared against `documents.content_hash`.
3. **PyMuPDF** extracts native text per page, embedded images, and renders pages
   to PNG when OCR is needed.
4. **Tesseract** runs on rendered pages and on embedded images.
5. **Vision** (optional, configurable) describes images via LM Studio.
6. **Chunker** produces token-aware sliding-window chunks that respect page
   boundaries.
7. **Embeddings** are produced in batches via LM Studio.
8. Chunks land in SQLite (`document_chunks`) **and** ChromaDB (cosine index),
   plus the FTS5 mirror (`chunks_fts`).
9. **Auto-tagger** runs heuristic classification.

## Data flow (RAG)

1. Hybrid retrieval: vector similarity + FTS5 BM25 + metadata filters from
   `chat_context_items`.
2. Top-K chunks are concatenated into a numbered SOURCES block.
3. System prompt forces citation by [#] and disallows hallucinations.
4. LM Studio generates the answer; the assistant message is persisted with
   structured `sources` JSON.

## Storage

- `%APPDATA%/Trovato/`
  - `trovato.db` — main SQLite DB (WAL mode, foreign keys on)
  - `chroma/` — persistent vector store
  - `cache/pages/` — rendered page PNGs (lazy)
  - `cache/images/` — embedded image extracts
  - `backups/` — archives
  - `logs/` — rotating loguru output
  - `settings.json` — user-editable runtime settings

## Security

- Argon2id password hashing; one recovery key per user, also Argon2-hashed.
- `itsdangerous`-signed session cookies; secret key auto-generated and stored
  in `settings.json` (chmod 600 on POSIX; Windows ACL inherits from `%APPDATA%`).
- Backups optionally encrypted (Fernet + PBKDF2-HMAC-SHA256, 200k iterations).
- CORS limited to localhost by default; LAN exposure opt-in.
- Sensitive memory entries require explicit `confirmed=True` before use.
- Logs never contain document content unless `LDI_DEBUG=1`.

## Pluggable bits

- **Database**: switch via `LDI_DB_URL` (e.g. `postgresql+psycopg://...`).
- **Vector store**: replace ChromaDB by writing another adapter behind the
  `app/vectorstore` interface.
- **LLM**: swap LM Studio for any OpenAI-compatible endpoint — change the URL.
- **OCR**: PaddleOCR can drop in by replacing `app/ocr/tesseract.py`.
