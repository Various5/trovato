# LocalDoc Intelligence

> Local, private PDF intelligence: index thousands of PDFs, run OCR on scans,
> describe embedded images with a local vision model, search semantically, and
> chat over your documents — **100 % offline** via [LM Studio](https://lmstudio.ai/).

PaperVault is the working repository name; the product ships as **LocalDoc
Intelligence**.

**Status: Beta (v0.4.0b3).** Feature-complete and stabilising toward 1.0. The
pipeline now **auto-tunes to your hardware** — see *Performance profiles* below.

---

## Highlights

- **Sources**: local folders, USB drives, SMB shares, cloud-sync folders
  (OneDrive / Dropbox / Google Drive sync) — WebDAV/SFTP prepared
- **Ingestion**: PyMuPDF text extraction, page rendering, embedded-image
  extraction, table extraction, automatic OCR fallback (pytesseract)
- **Image understanding**: optional vision-model description via LM Studio
  (Qwen2.5-VL recommended)
- **Embeddings & vector store**: LM Studio embeddings → ChromaDB
- **Hybrid search**: SQLite FTS5 + vector search + metadata filters
- **RAG chat**: persistent, citable answers with page numbers and snippets,
  per-chat context filters, user-memory
- **Auth**: local user(s), Argon2 password hashing, first-run wizard, recovery
- **Backup / Restore**: granular ZIP backups, optional encryption
- **UI**: NiceGUI desktop-style app, 6 themes, PDF viewer with page jump
- **Windows installer**: PyInstaller + Inno Setup recipe
- **Hardware auto-tuning**: scan concurrency, OCR render DPI and embedding
  batch size scale to the machine — from a 2-core laptop to a 16-core box

## Performance profiles

The same build runs on a low-end laptop and a workstation. Settings → **Performance**
exposes a profile:

| Profile    | When                          | Effect                                              |
|------------|-------------------------------|-----------------------------------------------------|
| **Auto**   | default                       | detects CPU cores + RAM (+ GPU) and picks a tier    |
| **Low**    | ≤2 cores or <6 GB RAM         | 1 worker, 150 DPI, small embedding batches          |
| **Balanced** | mid-range (the old defaults)| ~2–4 workers, 220 DPI, 128-text batches             |
| **High**   | 8+ cores, 16 GB+              | up to 8 workers, 300 DPI, 256-text batches          |

`Auto` is recommended. The resolved knobs (workers, DPI, batch size) and the
detected hardware are shown live in Settings and on the **Diagnostics** page
(`GET /api/diagnostics/hardware`). A manual `parallel_workers` override (0 = auto)
still wins for the worker count if you set it.

## Quick start (development)

```bash
# 1) Create venv (Python 3.11+)
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

# 2) Install
pip install -e ".[dev]"

# 3) Install tesseract (Windows): https://github.com/UB-Mannheim/tesseract/wiki
#    Point LDI_TESSERACT_CMD in .env to tesseract.exe

# 4) Start LM Studio, load Qwen2.5 chat + bge-m3 embedding (+ optional Qwen2.5-VL)
#    Enable the OpenAI-compatible server on port 1234

# 5) Run the app
python -m app.main
# Open http://127.0.0.1:8765  → First-run wizard
```

## Recommended LM Studio models

| Role        | Model                                    | Notes                            |
|-------------|------------------------------------------|----------------------------------|
| Chat        | Qwen2.5-7B / Qwen3-8B (or larger)        | reasoning + general chat         |
| Vision      | Qwen2.5-VL-7B (32B/72B for strong GPUs)  | image descriptions, OCR-on-image |
| Embeddings  | bge-m3 or nomic-embed-text-v1.5          | multilingual                     |

Models are **configurable** in the Settings page — nothing is hard-coded.

## Building the Windows installer

See [`installer/README.md`](installer/README.md). Short version:

```bash
pip install -e ".[build]"
python installer/build.py            # produces dist/LocalDocIntelligence/
# Then run Inno Setup on installer/localdoc.iss
```

## Backup & Restore

Use the Backup page in the UI or:

```bash
python -m scripts.backup --output backup.zip --include db,vector,chats,memory,settings
python -m scripts.restore --input backup.zip
```

## Project layout

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full picture.

## License

MIT — see [LICENSE](LICENSE).

## About

- Author: **Varous 555** · `varous555@gmail.com` · `@varous555`
- GitHub: <https://github.com/Various5/localdoc-intelligence>
- Privacy: all processing is local. The app never contacts the internet
  unless you point a source at a cloud-sync folder you own.
