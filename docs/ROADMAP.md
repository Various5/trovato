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
