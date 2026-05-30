# Roadmap

## v0.2 — Robustness ✅

- [x] Resumable scan jobs (recovered at startup, manual `/api/scan/jobs/{id}/resume_job`)
- [x] Cross-document duplicate detection (content-hash) — Diagnostics page
- [x] Near-dup via shingles + Jaccard
- [x] Reranker hook (LLM-as-reranker — toggle on the Search page)
- [x] PDF page viewer with snippet highlight (`/viewer`)
- [x] PDF jump-to-page when opening from search/chat/viewer (`#page=N`)
- [x] Export search results (CSV, JSON) & chats (Markdown, PDF)

## v0.3 — Power features ✅

- [x] Multi-user with per-user document ACLs (private/shared visibility, owner_id)
- [x] User CRUD (admin endpoints `/api/users`)
- [x] WebDAV + SFTP sources (lazy-imported providers, encrypted credentials)
- [x] Watched-folder mode with debounced re-index (`watchfiles`)
- [x] Streaming chat (SSE + NiceGUI live updates)
- [x] Inline document comparison (`/compare`)
- [x] "Open similar documents" (`/api/documents/{id}/similar` + UI dialog)
- [x] pdfplumber-based table extraction → `ChunkSource.table` chunks
- [x] PaddleOCR adapter (lazy import, optional)
- [x] Vision-augmented auto-tagging (`has:images`, `has:dates`, `has:amounts`, `has:org`)

## v0.4 — Quality ✅

- [x] Alembic migration baseline + 0002 owner_id/visibility migration
- [x] Audit-event logging (login, logout, password change, lockouts, user-admin)
- [x] Login rate limiting + lockout
- [x] Transactional re-index (old chunks preserved on failure)
- [x] CI workflow (GitHub Actions) — SQLite + Postgres matrix, ruff, black, pytest
- [x] Property-based tests for chunker (hypothesis)
- [x] ACL, audit, rate-limit, near-dup, secret-store, i18n, exports tests
- [x] Playwright smoke-test scaffolding (`tests/e2e`)

## v0.4 — Beta ✅

- [x] **Hardware-aware performance auto-tuning** (`app/services/hardware.py`):
      detects CPU cores / RAM / GPU, classifies a tier, and resolves every
      pipeline knob (scan workers, quick-phase concurrency, embedding batch,
      OCR render DPI, per-image OCR pixel cap, vision concurrency, LLM timeout)
- [x] `performance_profile` setting (auto/low/balanced/high) — config → API
      (validated server-side) → Settings UI (live preview) → Diagnostics
- [x] `GET /api/diagnostics/hardware` — detected hardware + resolved tuning
- [x] Beta version bump (0.4.0b1) across package, pyproject and installer
- [x] Pre-release-aware update version comparison (`0.4.0b1 < 0.4.0`)
- [x] Fixed update-check URL (was pinging the wrong GitHub owner, silently)
- [x] Diagnostics fixes: no more duplicated cards on Refresh; dead no-op loop
      removed; `cpu_count()` None-guard; deep-linked `/compare?a=&b=` honoured

### Deferred follow-ups (tracked, not beta blockers)

- [ ] `/search?tag=` tag-only browse (needs tag-only retrieval in `_go()`)
- [ ] Search export should honour active filters + rerank (factor a shared helper)
- [ ] Finish the i18n sweep — remaining hardcoded English in dashboard/search/chat
- [ ] `near_dup` batch-fetch (avoid one DB session per document on Diagnostics)
- [ ] Replace `len(session.exec(select(...)).all())` counts with `func.count()`

## v1.0 — Polish

- [x] In-app metrics dashboard (CPU/RAM, queue depth, watcher status)
- [x] DE/EN translations (i18n layer + per-user language picker)
- [x] Update-check endpoint (`/api/about/check-update`)
- [x] Code-signing recipe + Inno Setup auto-sign hook (DEPLOYMENT.md)
- [x] Security threat model (SECURITY.md)
- [x] Issue + PR templates
- [x] Full DE/EN coverage of every label (all pages translated)
- [x] In-app auto-update banner (background check, per-user dismiss, manual recheck on About)
- [ ] Signed Windows installer (needs a real code-signing cert — pipeline documented)
