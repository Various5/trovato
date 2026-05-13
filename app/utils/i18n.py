"""Minimal i18n layer.

Translation tables live inline as Python dicts (small, fast, no extra deps).
``t(key, lang)`` returns the localised string or the key itself as a sensible
fallback. The UI looks up the active user's preferred language from
``UserSetting`` and threads it through.
"""

from __future__ import annotations


_DICT: dict[str, dict[str, str]] = {
    "en": {
        # generic
        "common.welcome": "Welcome to LocalDoc Intelligence",
        "common.username": "Username",
        "common.password": "Password",
        "common.confirm_password": "Confirm password",
        "common.current_password": "Current password",
        "common.new_password": "New password",
        "common.theme": "Theme",
        "common.language": "Language",
        "common.add": "Add",
        "common.edit": "Edit",
        "common.cancel": "Cancel",
        "common.close": "Close",
        "common.error": "Error",
        "common.success": "Success",
        "common.no_results": "No results.",
        "common.loading": "Loading…",
        "common.actions": "Actions",
        "common.name": "Name",
        "common.path": "Path",
        "common.type": "Type",
        "common.optional": "optional",
        "common.refresh": "Refresh",
        "common.delete": "Delete",
        "common.save": "Save",
        "common.update": "Update",
        "common.test": "Test",

        # login / wizard
        "login.title": "Sign in",
        "login.invalid": "Invalid credentials",
        "login.forgot": "Forgot password?",
        "wizard.intro": "Initial setup — create your admin account and point to LM Studio.",
        "wizard.admin_user": "Admin username",
        "wizard.lm_url": "LM Studio base URL",
        "wizard.initial_folder": "Initial document folder (optional)",
        "wizard.create_admin": "Create admin account",
        "wizard.continue": "Continue to dashboard",
        "wizard.recovery_note": "Save your recovery key now (shown only once):",
        "wizard.pw_mismatch": "Passwords do not match.",
        "wizard.user_pw_required": "Username and password are required.",
        "recover.title": "Recover password",
        "recover.recovery_key": "Recovery key",
        "recover.invalid": "Invalid username or recovery key.",
        "recover.success": "Password reset. Please log in.",
        "recover.btn": "Reset password",

        # dashboard
        "dash.title": "Dashboard",
        "dash.documents": "Documents",
        "dash.sources": "Sources",
        "dash.chunks": "Chunks",
        "dash.chats": "Chats",
        "dash.quick_actions": "Quick actions",
        "dash.add_source": "Add a source",
        "dash.start_scan": "Start a scan",
        "dash.new_chat": "New chat",

        # documents
        "docs.title": "Documents",
        "docs.filter": "Filter by name/path",
        "docs.btn_view": "View",
        "docs.btn_pdf": "PDF",
        "docs.btn_similar": "Similar",
        "docs.btn_summarize": "Summarize",
        "docs.similar_to": "Similar to:",
        "docs.no_similar": "No similar documents found.",
        "docs.summary_of": "Summary:",
        "docs.pages": "Pages",
        "docs.status": "Status",
        "docs.type": "Type",
        "docs.lang": "Lang",
        "docs.viewer.not_found": "Document not found.",
        "docs.viewer.page_text": "Page text",
        "docs.viewer.no_text": "(no extracted text on this page)",
        "docs.viewer.open_pdf": "Open PDF",
        "docs.viewer.prev": "Prev",
        "docs.viewer.next": "Next",
        "docs.viewer.page_of": "page",

        # search
        "search.title": "Search",
        "search.placeholder": "Search across content, OCR, image descriptions, tags",
        "search.rerank": "Rerank with LLM",
        "search.go": "Search",
        "search.no_results": "No results.",
        "search.btn_view": "View",
        "search.btn_pdf": "PDF",
        "search.btn_csv": "Export CSV",
        "search.btn_json": "Export JSON",
        "search.score": "score",
        "search.source": "source",

        # chat
        "chat.title": "Chat with your documents",
        "chat.list_title": "Chats",
        "chat.new": "New chat",
        "chat.ph_ask": "Ask a question…",
        "chat.you": "You",
        "chat.assistant": "Assistant",
        "chat.sources": "Sources",
        "chat.start_or_pick": "Start a new chat or pick one on the left.",
        "chat.btn_md": "MD",
        "chat.btn_pdf": "PDF",
        "chat.open_first": "Open a chat first",

        # sources
        "sources.title": "Sources",
        "sources.add_new": "Add new source",
        "sources.last_scan": "Last scan:",
        "sources.scan": "Scan",
        "sources.force_ocr": "Force OCR",
        "sources.vision": "Vision",
        "sources.dry_run": "Dry run",
        "sources.watch": "Watch",
        "sources.unwatch": "Unwatch",
        "sources.delete": "Delete",
        "sources.added": "Source added",
        "sources.need_name_path": "Name and path required",
        "sources.scan_started": "Scan started",
        "sources.ocr_started": "OCR scan started",
        "sources.vision_started": "Vision scan started",
        "sources.dryrun_started": "Dry run started",

        # tags
        "tags.title": "Tags",
        "tags.auto": "(auto)",

        # backup
        "backup.title": "Backup & Restore",
        "backup.create": "Create backup",
        "backup.create_btn": "Create backup",
        "backup.password_enc": "Encryption password (optional)",
        "backup.select_at_least_one": "Select at least one component",
        "backup.written_to": "Backup written to",
        "backup.existing": "Existing backups",
        "backup.restore": "Restore",
        "backup.restored": "Restored",
        "backup.errors": "errors",

        # settings
        "settings.title": "Settings",
        "settings.lmstudio": "LM Studio",
        "settings.base_url": "Base URL",
        "settings.chat_model": "Chat model id",
        "settings.vision_model": "Vision model id",
        "settings.emb_model": "Embedding model id",
        "settings.test_connection": "Test connection",
        "settings.lm_reachable": "LM Studio reachable:",
        "settings.ocr": "OCR",
        "settings.tesseract": "Tesseract path (optional)",
        "settings.ocr_langs": "OCR languages (e.g. eng+deu)",
        "settings.indexing": "Indexing",
        "settings.chunk_size": "Chunk size (tokens)",
        "settings.chunk_overlap": "Overlap (tokens)",
        "settings.appearance": "Appearance",
        "settings.save": "Save settings",
        "settings.saved_reload": "Settings saved. Reloading…",
        "settings.change_pw": "Change password",
        "settings.update_pw": "Update password",
        "settings.pw_wrong": "Current password incorrect",
        "settings.pw_updated": "Password updated",

        # logs
        "logs.title": "Logs",
        "logs.empty": "(no log file yet)",

        # diagnostics
        "diag.title": "Diagnostics",
        "diag.storage": "Storage",
        "diag.index": "Index",
        "diag.lmstudio": "LM Studio",
        "diag.metrics": "System & queue metrics",
        "diag.metrics_disabled": "psutil not installed — system metrics disabled.",
        "diag.duplicates": "Duplicates (by content hash)",
        "diag.dup_none": "None found.",
        "diag.near_dup": "Near-duplicates (shingles)",
        "diag.near_dup_none": "None found ≥70 % similarity.",
        "diag.orphans": "Orphan caches",
        "diag.orphans_found": "Found {n} orphan cache folders.",
        "diag.clean_orphans": "Clean orphan caches",
        "diag.clean_done": "Cleaned",
        "diag.models": "Models:",
        "diag.compare": "Compare",
        "diag.refresh": "Refresh",

        # compare
        "compare.title": "Compare two documents",
        "compare.index_first": "Index some PDFs first.",
        "compare.pick_two_different": "Pick two different documents",
        "compare.narrative": "Narrative comparison",
        "compare.shared_ratio": "Line-level shared ratio:",
        "compare.only_in": "Only in",
        "compare.btn": "Compare",
        "compare.doc_a": "Document A",
        "compare.doc_b": "Document B",

        # about
        "about.title": "About",
        "about.version": "Version",
        "about.author": "Author",
        "about.contact": "Contact",
        "about.handle": "Handle",
        "about.github": "GitHub (placeholder)",
        "about.license": "License: MIT",
        "about.privacy_note": (
            "Privacy: all processing is local. No cloud requests are made unless you explicitly "
            "configure a source pointing at a cloud-sync folder."
        ),
        "about.built_with": (
            "Built with FastAPI, NiceGUI, SQLModel, ChromaDB, PyMuPDF, tesseract, "
            "argon2-cffi, httpx, loguru — and your local LM Studio."
        ),
        "about.description": (
            "Local PDF intelligence: indexing, OCR, image analysis, semantic search and chat "
            "— all running on your machine."
        ),

        # update banner
        "update.available": "Update available: v{latest}",
        "update.download": "Download",
        "update.dismiss": "Dismiss",
        "update.up_to_date": "You're on the latest version.",
        "update.check_now": "Check for updates",

        # navigation
        "nav.dashboard": "Dashboard",
        "nav.documents": "Documents",
        "nav.search": "Search",
        "nav.chat": "Chat",
        "nav.sources": "Sources",
        "nav.compare": "Compare",
        "nav.tags": "Tags",
        "nav.backup": "Backup",
        "nav.settings": "Settings",
        "nav.diagnostics": "Diagnostics",
        "nav.logs": "Logs",
        "nav.about": "About",

        # buttons
        "btn.login": "Sign in",
        "btn.logout": "Logout",
        "btn.new_chat": "New chat",
        "btn.search": "Search",
        "btn.export_csv": "Export CSV",
        "btn.export_json": "Export JSON",
        "btn.export_md": "Export Markdown",
        "btn.export_pdf": "Export PDF",
        "btn.refresh": "Refresh",
        "btn.delete": "Delete",
        "btn.save": "Save",

        # placeholders
        "ph.search": "Search across content, OCR, image descriptions, tags",
        "ph.ask": "Ask a question…",
    },
    "de": {
        # generic
        "common.welcome": "Willkommen bei LocalDoc Intelligence",
        "common.username": "Benutzername",
        "common.password": "Passwort",
        "common.confirm_password": "Passwort bestätigen",
        "common.current_password": "Aktuelles Passwort",
        "common.new_password": "Neues Passwort",
        "common.theme": "Erscheinungsbild",
        "common.language": "Sprache",
        "common.add": "Hinzufügen",
        "common.edit": "Bearbeiten",
        "common.cancel": "Abbrechen",
        "common.close": "Schließen",
        "common.error": "Fehler",
        "common.success": "Erfolg",
        "common.no_results": "Keine Treffer.",
        "common.loading": "Lade…",
        "common.actions": "Aktionen",
        "common.name": "Name",
        "common.path": "Pfad",
        "common.type": "Typ",
        "common.optional": "optional",
        "common.refresh": "Aktualisieren",
        "common.delete": "Löschen",
        "common.save": "Speichern",
        "common.update": "Aktualisieren",
        "common.test": "Testen",

        # login / wizard
        "login.title": "Anmelden",
        "login.invalid": "Ungültige Zugangsdaten",
        "login.forgot": "Passwort vergessen?",
        "wizard.intro": "Erste Einrichtung — lege einen Admin-Account an und gib LM Studio an.",
        "wizard.admin_user": "Admin-Benutzername",
        "wizard.lm_url": "LM-Studio-Basis-URL",
        "wizard.initial_folder": "Erster Dokumentordner (optional)",
        "wizard.create_admin": "Admin-Account erstellen",
        "wizard.continue": "Weiter zur Übersicht",
        "wizard.recovery_note": "Speichere deinen Wiederherstellungs-Key jetzt (wird nur einmal gezeigt):",
        "wizard.pw_mismatch": "Passwörter stimmen nicht überein.",
        "wizard.user_pw_required": "Benutzername und Passwort sind erforderlich.",
        "recover.title": "Passwort wiederherstellen",
        "recover.recovery_key": "Wiederherstellungs-Key",
        "recover.invalid": "Ungültiger Benutzername oder Wiederherstellungs-Key.",
        "recover.success": "Passwort zurückgesetzt. Bitte anmelden.",
        "recover.btn": "Passwort zurücksetzen",

        # dashboard
        "dash.title": "Übersicht",
        "dash.documents": "Dokumente",
        "dash.sources": "Quellen",
        "dash.chunks": "Chunks",
        "dash.chats": "Chats",
        "dash.quick_actions": "Schnellaktionen",
        "dash.add_source": "Quelle hinzufügen",
        "dash.start_scan": "Scan starten",
        "dash.new_chat": "Neuer Chat",

        # documents
        "docs.title": "Dokumente",
        "docs.filter": "Nach Name/Pfad filtern",
        "docs.btn_view": "Ansehen",
        "docs.btn_pdf": "PDF",
        "docs.btn_similar": "Ähnliche",
        "docs.btn_summarize": "Zusammenfassen",
        "docs.similar_to": "Ähnlich zu:",
        "docs.no_similar": "Keine ähnlichen Dokumente gefunden.",
        "docs.summary_of": "Zusammenfassung:",
        "docs.pages": "Seiten",
        "docs.status": "Status",
        "docs.type": "Typ",
        "docs.lang": "Sprache",
        "docs.viewer.not_found": "Dokument nicht gefunden.",
        "docs.viewer.page_text": "Seitentext",
        "docs.viewer.no_text": "(kein extrahierter Text auf dieser Seite)",
        "docs.viewer.open_pdf": "PDF öffnen",
        "docs.viewer.prev": "Zurück",
        "docs.viewer.next": "Weiter",
        "docs.viewer.page_of": "Seite",

        # search
        "search.title": "Suche",
        "search.placeholder": "In Inhalt, OCR, Bildbeschreibungen und Tags suchen",
        "search.rerank": "Mit LLM neu sortieren",
        "search.go": "Suchen",
        "search.no_results": "Keine Treffer.",
        "search.btn_view": "Ansehen",
        "search.btn_pdf": "PDF",
        "search.btn_csv": "CSV-Export",
        "search.btn_json": "JSON-Export",
        "search.score": "Score",
        "search.source": "Quelle",

        # chat
        "chat.title": "Mit deinen Dokumenten chatten",
        "chat.list_title": "Chats",
        "chat.new": "Neuer Chat",
        "chat.ph_ask": "Stelle eine Frage…",
        "chat.you": "Du",
        "chat.assistant": "Assistent",
        "chat.sources": "Quellen",
        "chat.start_or_pick": "Starte einen neuen Chat oder wähle links einen aus.",
        "chat.btn_md": "MD",
        "chat.btn_pdf": "PDF",
        "chat.open_first": "Öffne zuerst einen Chat",

        # sources
        "sources.title": "Quellen",
        "sources.add_new": "Neue Quelle hinzufügen",
        "sources.last_scan": "Letzter Scan:",
        "sources.scan": "Scannen",
        "sources.force_ocr": "OCR erzwingen",
        "sources.vision": "Vision",
        "sources.dry_run": "Probelauf",
        "sources.watch": "Überwachen",
        "sources.unwatch": "Überwachung beenden",
        "sources.delete": "Löschen",
        "sources.added": "Quelle hinzugefügt",
        "sources.need_name_path": "Name und Pfad erforderlich",
        "sources.scan_started": "Scan gestartet",
        "sources.ocr_started": "OCR-Scan gestartet",
        "sources.vision_started": "Vision-Scan gestartet",
        "sources.dryrun_started": "Probelauf gestartet",

        # tags
        "tags.title": "Tags",
        "tags.auto": "(auto)",

        # backup
        "backup.title": "Sicherung & Wiederherstellung",
        "backup.create": "Sicherung erstellen",
        "backup.create_btn": "Sicherung erstellen",
        "backup.password_enc": "Verschlüsselungs-Passwort (optional)",
        "backup.select_at_least_one": "Wähle mindestens eine Komponente",
        "backup.written_to": "Sicherung geschrieben nach",
        "backup.existing": "Vorhandene Sicherungen",
        "backup.restore": "Wiederherstellen",
        "backup.restored": "Wiederhergestellt",
        "backup.errors": "Fehler",

        # settings
        "settings.title": "Einstellungen",
        "settings.lmstudio": "LM Studio",
        "settings.base_url": "Basis-URL",
        "settings.chat_model": "Chat-Modell-ID",
        "settings.vision_model": "Vision-Modell-ID",
        "settings.emb_model": "Embedding-Modell-ID",
        "settings.test_connection": "Verbindung testen",
        "settings.lm_reachable": "LM Studio erreichbar:",
        "settings.ocr": "OCR",
        "settings.tesseract": "Tesseract-Pfad (optional)",
        "settings.ocr_langs": "OCR-Sprachen (z.B. eng+deu)",
        "settings.indexing": "Indexierung",
        "settings.chunk_size": "Chunk-Größe (Tokens)",
        "settings.chunk_overlap": "Überlappung (Tokens)",
        "settings.appearance": "Erscheinungsbild",
        "settings.save": "Einstellungen speichern",
        "settings.saved_reload": "Einstellungen gespeichert. Lade neu…",
        "settings.change_pw": "Passwort ändern",
        "settings.update_pw": "Passwort aktualisieren",
        "settings.pw_wrong": "Aktuelles Passwort falsch",
        "settings.pw_updated": "Passwort aktualisiert",

        # logs
        "logs.title": "Protokoll",
        "logs.empty": "(noch keine Logdatei)",

        # diagnostics
        "diag.title": "Diagnose",
        "diag.storage": "Speicher",
        "diag.index": "Index",
        "diag.lmstudio": "LM Studio",
        "diag.metrics": "System- & Queue-Metriken",
        "diag.metrics_disabled": "psutil nicht installiert — Systemmetriken deaktiviert.",
        "diag.duplicates": "Duplikate (nach Content-Hash)",
        "diag.dup_none": "Keine gefunden.",
        "diag.near_dup": "Fast-Duplikate (Shingles)",
        "diag.near_dup_none": "Keine ≥70 % Ähnlichkeit gefunden.",
        "diag.orphans": "Verwaiste Caches",
        "diag.orphans_found": "{n} verwaiste Cache-Ordner gefunden.",
        "diag.clean_orphans": "Verwaiste Caches bereinigen",
        "diag.clean_done": "Bereinigt",
        "diag.models": "Modelle:",
        "diag.compare": "Vergleichen",
        "diag.refresh": "Aktualisieren",

        # compare
        "compare.title": "Zwei Dokumente vergleichen",
        "compare.index_first": "Indexiere zuerst einige PDFs.",
        "compare.pick_two_different": "Wähle zwei verschiedene Dokumente",
        "compare.narrative": "Strukturierter Vergleich",
        "compare.shared_ratio": "Zeilenanteil gemeinsam:",
        "compare.only_in": "Nur in",
        "compare.btn": "Vergleichen",
        "compare.doc_a": "Dokument A",
        "compare.doc_b": "Dokument B",

        # about
        "about.title": "Über",
        "about.version": "Version",
        "about.author": "Autor",
        "about.contact": "Kontakt",
        "about.handle": "Handle",
        "about.github": "GitHub (Platzhalter)",
        "about.license": "Lizenz: MIT",
        "about.privacy_note": (
            "Datenschutz: Alle Verarbeitung läuft lokal. Es werden keine Cloud-Anfragen "
            "gestellt, außer du konfigurierst eine Quelle, die auf einen Cloud-Sync-Ordner zeigt."
        ),
        "about.built_with": (
            "Gebaut mit FastAPI, NiceGUI, SQLModel, ChromaDB, PyMuPDF, tesseract, "
            "argon2-cffi, httpx, loguru — und deinem lokalen LM Studio."
        ),
        "about.description": (
            "Lokale PDF-Intelligenz: Indexierung, OCR, Bildanalyse, semantische Suche und Chat "
            "— alles auf deinem Rechner."
        ),

        # update banner
        "update.available": "Update verfügbar: v{latest}",
        "update.download": "Herunterladen",
        "update.dismiss": "Ausblenden",
        "update.up_to_date": "Du nutzt die aktuelle Version.",
        "update.check_now": "Nach Updates suchen",

        # navigation
        "nav.dashboard": "Übersicht",
        "nav.documents": "Dokumente",
        "nav.search": "Suche",
        "nav.chat": "Chat",
        "nav.sources": "Quellen",
        "nav.compare": "Vergleichen",
        "nav.tags": "Tags",
        "nav.backup": "Sicherung",
        "nav.settings": "Einstellungen",
        "nav.diagnostics": "Diagnose",
        "nav.logs": "Protokoll",
        "nav.about": "Über",

        # buttons
        "btn.login": "Anmelden",
        "btn.logout": "Abmelden",
        "btn.new_chat": "Neuer Chat",
        "btn.search": "Suchen",
        "btn.export_csv": "CSV-Export",
        "btn.export_json": "JSON-Export",
        "btn.export_md": "Markdown-Export",
        "btn.export_pdf": "PDF-Export",
        "btn.refresh": "Aktualisieren",
        "btn.delete": "Löschen",
        "btn.save": "Speichern",

        # placeholders
        "ph.search": "In Inhalt, OCR, Bildbeschreibungen und Tags suchen",
        "ph.ask": "Stelle eine Frage…",
    },
}


SUPPORTED_LANGUAGES = ["en", "de"]


def t(key: str, lang: str | None = "en") -> str:
    lang = (lang or "en").lower()
    if lang not in _DICT:
        lang = "en"
    return _DICT[lang].get(key) or _DICT["en"].get(key) or key
