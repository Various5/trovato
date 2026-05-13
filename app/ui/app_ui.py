"""NiceGUI frontend — registers all UI pages onto the FastAPI app.

The UI talks to the same process via direct Python service calls (no
HTTP roundtrips). Session-cookie auth is shared with the API.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from nicegui import app as nicegui_app
from nicegui import ui
from sqlmodel import select

from app import __app_name__, __version__
from app.auth.security import (
    SESSION_USER_KEY,
    create_user,
    has_users,
    hash_password,
    make_recovery_key,
    verify_password,
)
from app.config import get_settings, save_user_settings
from app.database import session_scope
from app.models import (
    Chat,
    ChatContextItem,
    ChatMessage,
    Document,
    DocumentSource,
    SourceType,
    User,
    UserSetting,
)
from app.services.indexer import start_scan_in_background
from app.ui.themes import THEMES, theme_css
from app.utils.i18n import SUPPORTED_LANGUAGES, t


def _current_user() -> User | None:
    try:
        uid = nicegui_app.storage.user.get(SESSION_USER_KEY)
        if uid is None:
            return None
        with session_scope() as session:
            return session.get(User, uid)
    except Exception:
        return None


def _require_login() -> User | None:
    u = _current_user()
    if not u:
        ui.navigate.to("/login")
        return None
    return u


def pdf_url(document_id: int, page: int | None = None) -> str:
    """Build a URL to the original PDF that jumps to ``page`` when the browser
    PDF viewer opens it."""
    base = f"/api/documents/{document_id}/file"
    if page and page > 0:
        # `#page=N` is the standard fragment understood by Chrome/Edge/Firefox
        # PDF viewers (and Adobe Reader). Some viewers also accept additional
        # parameters like `zoom=` and `nameddest=`.
        return f"{base}#page={page}"
    return base


def open_pdf(document_id: int, page: int | None = None) -> None:
    """Open the PDF in a new browser tab on the specified page."""
    ui.run_javascript(f"window.open({pdf_url(document_id, page)!r}, '_blank')")


# ---------------------------------------------------------------------------
# Update-banner state — cached for 1 hour to avoid hammering the endpoint
# ---------------------------------------------------------------------------

_UPDATE_STATE: dict[str, Any] = {"checked_at": 0.0, "info": None}
_UPDATE_TTL = 3600.0  # seconds


async def _refresh_update_state() -> None:
    import time

    from app.services.updates import check_for_update

    now = time.time()
    if now - _UPDATE_STATE["checked_at"] < _UPDATE_TTL:
        return
    try:
        info = await check_for_update()
        _UPDATE_STATE["info"] = info
        _UPDATE_STATE["checked_at"] = now
    except Exception:
        # leave previous state; try again next time
        _UPDATE_STATE["checked_at"] = now


def _render_update_banner(lang: str) -> None:
    info = _UPDATE_STATE.get("info")
    if not info or info.up_to_date or not info.latest:
        return
    # per-user dismiss for this version
    dismiss_key = f"dismiss_update_{info.latest}"
    try:
        dismissed = bool(nicegui_app.storage.user.get(dismiss_key))
    except Exception:
        dismissed = False
    if dismissed:
        return

    with (
        ui.card()
        .classes("w-full p-3 q-mb-md ldi-border")
        .style("border: 1px solid; background: rgba(255,193,7,0.08)"),
        ui.row().classes("items-center w-full no-wrap gap-3"),
    ):
        ui.icon("system_update").classes("text-2xl ldi-accent")
        with ui.column().classes("flex-1 gap-0"):
            ui.label(t("update.available", lang).format(latest=info.latest)).classes("text-body1")
            if info.notes:
                ui.label(info.notes[:300] + ("…" if len(info.notes) > 300 else "")).classes(
                    "text-caption opacity-70"
                )
        with ui.row().classes("gap-1"):
            if info.url:
                ui.button(
                    t("update.download", lang),
                    icon="download",
                    on_click=lambda u=info.url: ui.run_javascript(f"window.open({u!r}, '_blank')"),
                ).props("color=primary dense")

            def _dismiss(k=dismiss_key) -> None:
                nicegui_app.storage.user[k] = True
                ui.navigate.reload()

            ui.button(t("update.dismiss", lang), on_click=_dismiss).props("flat dense")


def _render_citation(c: dict) -> None:
    """Render one chat citation as a row with View + PDF buttons."""
    n = c.get("n")
    did = c.get("document_id")
    pg = c.get("page_from") or 1
    fname = c.get("filename")
    snippet = c.get("snippet") or ""
    with ui.row().classes("items-start gap-2 w-full no-wrap q-mb-xs"):
        with ui.column().classes("flex-1 gap-0"):
            ui.label(f"[{n}] {fname} (p.{pg})").classes("text-caption ldi-accent")
            ui.label(snippet).classes("text-caption opacity-70")
        with ui.row().classes("gap-1"):
            ui.button(
                "View",
                icon="visibility",
                on_click=lambda d=did, p=pg: ui.navigate.to(f"/viewer?doc={d}&page={p}"),
            ).props("dense flat")
            ui.button(
                "PDF",
                icon="picture_as_pdf",
                on_click=lambda d=did, p=pg: open_pdf(d, p),
            ).props("dense flat")


def _apply_theme(theme_name: str = "dark") -> None:
    t = THEMES.get(theme_name) or THEMES["dark"]
    ui.dark_mode().set_value(t.is_dark)
    ui.add_head_html(f"<style>{theme_css(theme_name)}</style>")


def _user_theme(user: User) -> str:
    with session_scope() as session:
        s = session.exec(select(UserSetting).where(UserSetting.user_id == user.id)).first()
        return s.theme if s and s.theme in THEMES else "dark"


def _user_lang(user: User) -> str:
    with session_scope() as session:
        s = session.exec(select(UserSetting).where(UserSetting.user_id == user.id)).first()
        lang = (s.language if s else "en") or "en"
        return lang if lang in SUPPORTED_LANGUAGES else "en"


def _layout(user: User, current: str) -> None:
    import asyncio

    _apply_theme(_user_theme(user))
    lang = _user_lang(user)
    # Kick off (or refresh) the update check in the background; the banner
    # renders on the *next* page navigation after the check completes.
    try:
        asyncio.create_task(_refresh_update_state())
    except RuntimeError:
        pass
    _render_update_banner(lang)
    with ui.left_drawer().classes("p-3 gap-2").style("min-width: 220px"):
        ui.label(__app_name__).classes("text-h6 ldi-primary")
        ui.label(f"v{__version__}").classes("text-caption opacity-70")
        ui.separator()
        nav = [
            ("nav.dashboard", "/", "dashboard"),
            ("nav.documents", "/documents", "description"),
            ("nav.search", "/search", "search"),
            ("nav.chat", "/chat", "forum"),
            ("nav.sources", "/sources", "folder"),
            ("nav.compare", "/compare", "compare_arrows"),
            ("nav.tags", "/tags", "label"),
            ("nav.backup", "/backup", "save"),
            ("nav.settings", "/settings", "settings"),
            ("nav.diagnostics", "/diagnostics", "monitor_heart"),
            ("nav.logs", "/logs", "article"),
            ("nav.about", "/about", "info"),
        ]
        for key, path, icon in nav:
            cls = "w-full justify-start"
            if current == path:
                cls += " ldi-primary"
            ui.button(t(key, lang), on_click=lambda p=path: ui.navigate.to(p), icon=icon).props(
                "flat"
            ).classes(cls)
        ui.separator()
        ui.button(
            f"{t('btn.logout', lang)} ({user.username})",
            icon="logout",
            on_click=_do_logout,
        ).props("flat outline")


def _do_logout() -> None:
    try:
        nicegui_app.storage.user.pop(SESSION_USER_KEY, None)
        # also clear any cached user data
        nicegui_app.storage.user.clear()
    except Exception:
        pass
    ui.navigate.to("/login")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def register_ui(fastapi_app: FastAPI) -> None:
    @ui.page("/login")
    def page_login() -> None:
        _apply_theme("dark")
        if _current_user():
            ui.navigate.to("/")
            return
        with session_scope() as session:
            first = not has_users(session)
        if first:
            ui.navigate.to("/first-run")
            return
        # Use browser-language as a hint until the user logs in.
        ph_lang = "en"
        with ui.card().classes("absolute-center w-96 p-6"):
            ui.label(t("login.title", ph_lang)).classes("text-h5 q-mb-md ldi-primary")
            username = ui.input(t("common.username", ph_lang)).classes("w-full")
            password = ui.input(
                t("common.password", ph_lang), password=True, password_toggle_button=True
            ).classes("w-full")
            err = ui.label("").classes("text-negative q-mt-sm")

            def _login() -> None:
                with session_scope() as session:
                    u = session.exec(select(User).where(User.username == username.value)).first()
                    if not u or not verify_password(password.value, u.password_hash):
                        err.text = t("login.invalid", ph_lang)
                        return
                    nicegui_app.storage.user[SESSION_USER_KEY] = u.id
                ui.navigate.to("/")

            ui.button(t("btn.login", ph_lang), on_click=_login).props("color=primary").classes(
                "w-full q-mt-md"
            )
            ui.link(t("login.forgot", ph_lang), "/recover").classes("text-caption q-mt-sm")

    @ui.page("/first-run")
    def page_first_run() -> None:
        _apply_theme("dark")
        with session_scope() as session:
            if has_users(session):
                ui.navigate.to("/login")
                return
        wl = "en"
        with ui.card().classes("absolute-center w-[480px] p-6"):
            ui.label(t("common.welcome", wl)).classes("text-h5 ldi-primary")
            ui.label(t("wizard.intro", wl)).classes("q-mb-md opacity-80")
            username = ui.input(t("wizard.admin_user", wl), value="admin").classes("w-full")
            password = ui.input(t("common.password", wl), password=True, password_toggle_button=True).classes(
                "w-full"
            )
            confirm = ui.input(
                t("common.confirm_password", wl), password=True, password_toggle_button=True
            ).classes("w-full")
            lm_url = ui.input(t("wizard.lm_url", wl), value=get_settings().lmstudio_base_url).classes(
                "w-full"
            )
            source_path = ui.input(t("wizard.initial_folder", wl)).classes("w-full")
            err = ui.label("").classes("text-negative")
            recovery_box = ui.label("").classes("text-positive break-words")

            def _create() -> None:
                if not username.value or not password.value:
                    err.text = t("wizard.user_pw_required", wl)
                    return
                if password.value != confirm.value:
                    err.text = t("wizard.pw_mismatch", wl)
                    return
                save_user_settings({"lmstudio_base_url": lm_url.value or "http://localhost:1234/v1"})
                get_settings.cache_clear()
                with session_scope() as session:
                    user = create_user(session, username=username.value, password=password.value)
                    rk = make_recovery_key()
                    user.recovery_key_hash = hash_password(rk)
                    session.add(user)
                    session.flush()
                    if source_path.value:
                        session.add(
                            DocumentSource(
                                name="Initial folder",
                                type=SourceType.local,
                                path=source_path.value,
                            )
                        )
                    nicegui_app.storage.user[SESSION_USER_KEY] = user.id
                recovery_box.text = f"{t('wizard.recovery_note', wl)} {rk}"
                ui.button(t("wizard.continue", wl), on_click=lambda: ui.navigate.to("/")).props(
                    "color=primary"
                )

            ui.button(t("wizard.create_admin", wl), on_click=_create).props("color=primary").classes(
                "w-full q-mt-md"
            )

    @ui.page("/recover")
    def page_recover() -> None:
        _apply_theme("dark")
        rl = "en"
        with ui.card().classes("absolute-center w-96 p-6"):
            ui.label(t("recover.title", rl)).classes("text-h5 ldi-primary")
            uname = ui.input(t("common.username", rl)).classes("w-full")
            rkey = ui.input(t("recover.recovery_key", rl)).classes("w-full")
            new_pw = ui.input(
                t("common.new_password", rl), password=True, password_toggle_button=True
            ).classes("w-full")
            err = ui.label("").classes("text-negative")

            def _go() -> None:
                from app.auth.security import reset_password_with_recovery

                with session_scope() as session:
                    ok = reset_password_with_recovery(
                        session,
                        username=uname.value,
                        recovery_key=rkey.value,
                        new_password=new_pw.value,
                    )
                if not ok:
                    err.text = t("recover.invalid", rl)
                else:
                    ui.notify(t("recover.success", rl))
                    ui.navigate.to("/login")

            ui.button(t("recover.btn", rl), on_click=_go).props("color=primary").classes("w-full")

    # ---- protected pages -------------------------------------------------

    @ui.page("/")
    def page_dashboard() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/")
        with session_scope() as session:
            doc_count = len(session.exec(select(Document)).all())
            source_count = len(session.exec(select(DocumentSource)).all())
            chat_count = len(session.exec(select(Chat).where(Chat.user_id == user.id)).all())
        from app.vectorstore import collection_size

        chunks = collection_size()

        lang = _user_lang(user)
        ui.label(t("dash.title", lang)).classes("text-h4 q-mb-md ldi-primary")
        with ui.row().classes("gap-4 q-mb-md"):
            for label_key, value, icon in [
                ("dash.documents", doc_count, "description"),
                ("dash.sources", source_count, "folder"),
                ("dash.chunks", chunks, "data_object"),
                ("dash.chats", chat_count, "forum"),
            ]:
                with ui.card().classes("p-4 w-44"):
                    ui.icon(icon).classes("text-3xl ldi-accent")
                    ui.label(str(value)).classes("text-h4")
                    ui.label(t(label_key, lang)).classes("opacity-80")
        ui.label(t("dash.quick_actions", lang)).classes("text-h6 q-mt-md")
        with ui.row().classes("gap-2"):
            ui.button(t("dash.add_source", lang), icon="add", on_click=lambda: ui.navigate.to("/sources"))
            ui.button(
                t("dash.start_scan", lang), icon="play_arrow", on_click=lambda: ui.navigate.to("/sources")
            )
            ui.button(t("dash.new_chat", lang), icon="forum", on_click=lambda: ui.navigate.to("/chat"))

    @ui.page("/sources")
    def page_sources() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/sources")
        lang = _user_lang(user)
        ui.label(t("sources.title", lang)).classes("text-h4 q-mb-md ldi-primary")

        table_container = ui.column().classes("w-full gap-2")

        def _refresh() -> None:
            table_container.clear()
            with table_container, session_scope() as session:
                rows = session.exec(select(DocumentSource).order_by(DocumentSource.id)).all()
                for s in rows:
                    with ui.card().classes("w-full p-3"):
                        with ui.row().classes("justify-between items-center w-full"):
                            with ui.column().classes("gap-0"):
                                ui.label(f"{s.name}  ").classes("text-h6")
                                ui.label(f"{s.type.value} — {s.path}").classes("opacity-70 text-caption")
                                if s.last_scan_at:
                                    ui.label(f"{t('sources.last_scan', lang)} {s.last_scan_at}").classes(
                                        "text-caption opacity-60"
                                    )
                            with ui.row().classes("gap-1"):
                                ui.button(
                                    t("sources.scan", lang),
                                    icon="play_arrow",
                                    on_click=lambda sid=s.id: (
                                        start_scan_in_background(sid),
                                        ui.notify(t("sources.scan_started", lang)),
                                    ),
                                ).props("color=primary dense")
                                ui.button(
                                    t("sources.force_ocr", lang),
                                    icon="text_fields",
                                    on_click=lambda sid=s.id: (
                                        start_scan_in_background(sid, force_ocr=True),
                                        ui.notify(t("sources.ocr_started", lang)),
                                    ),
                                ).props("dense")
                                ui.button(
                                    t("sources.vision", lang),
                                    icon="image",
                                    on_click=lambda sid=s.id: (
                                        start_scan_in_background(sid, force_vision=True),
                                        ui.notify(t("sources.vision_started", lang)),
                                    ),
                                ).props("dense")
                                ui.button(
                                    t("sources.dry_run", lang),
                                    icon="science",
                                    on_click=lambda sid=s.id: (
                                        start_scan_in_background(sid, dry_run=True),
                                        ui.notify(t("sources.dryrun_started", lang)),
                                    ),
                                ).props("dense")

                                from app.services.watcher import (
                                    is_watching as _is_watching,
                                )
                                from app.services.watcher import (
                                    start_watcher as _start_watch,
                                )
                                from app.services.watcher import (
                                    stop_watcher as _stop_watch,
                                )

                                watching = _is_watching(s.id)
                                ui.button(
                                    t("sources.unwatch" if watching else "sources.watch", lang),
                                    icon="visibility_off" if watching else "visibility",
                                    on_click=lambda sid=s.id: (
                                        (_stop_watch(sid) if _is_watching(sid) else _start_watch(sid)),
                                        _refresh(),
                                    ),
                                ).props("dense")
                                ui.button(
                                    t("sources.delete", lang),
                                    icon="delete",
                                    on_click=lambda sid=s.id: _delete_source(sid),
                                ).props("dense color=negative")

        def _delete_source(sid: int) -> None:
            with session_scope() as session:
                src = session.get(DocumentSource, sid)
                if src:
                    session.delete(src)
            _refresh()

        with ui.card().classes("w-full p-3 q-mb-md"):
            ui.label(t("sources.add_new", lang)).classes("text-h6")
            with ui.row().classes("w-full items-end gap-2"):
                name = ui.input(t("common.name", lang)).classes("flex-1")
                stype = ui.select(
                    [st.value for st in SourceType], value="local", label=t("common.type", lang)
                ).classes("w-32")
                path = ui.input(t("common.path", lang)).classes("flex-2")

                def _add() -> None:
                    if not (name.value and path.value):
                        ui.notify(t("sources.need_name_path", lang), color="negative")
                        return
                    with session_scope() as session:
                        session.add(
                            DocumentSource(
                                name=name.value,
                                type=SourceType(stype.value),
                                path=path.value,
                                owner_id=user.id,
                            )
                        )
                    name.value = ""
                    path.value = ""
                    _refresh()
                    ui.notify(t("sources.added", lang))

                ui.button(t("common.add", lang), icon="add", on_click=_add).props("color=primary")

        _refresh()

    @ui.page("/documents")
    def page_documents() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/documents")
        lang = _user_lang(user)
        ui.label(t("docs.title", lang)).classes("text-h4 q-mb-md ldi-primary")

        async def _show_similar(did: int, fname: str) -> None:
            from app.services.similar import find_similar

            with ui.dialog() as dialog, ui.card().classes("w-[640px] p-4"):
                ui.label(f"{t('docs.similar_to', lang)} {fname}").classes("text-h6 ldi-primary")
                spinner = ui.spinner(size="lg")
                content = ui.column().classes("w-full gap-2")
                dialog.open()
                hits = await find_similar(did, top_k=15)
                spinner.delete()
                if not hits:
                    with content:
                        ui.label(t("docs.no_similar", lang)).classes("opacity-70")
                    return
                with content:
                    for h in hits:
                        with ui.card().classes("w-full p-2"):
                            ui.label(h.filename).classes("text-body1")
                            ui.label(
                                f"{t('search.score', lang)}: {h.score:.3f} · " f"matches: {h.matched_chunks}"
                            ).classes("text-caption opacity-70")
                            ui.label(h.path).classes("text-caption opacity-60 break-all")
                            with ui.row().classes("gap-1"):
                                ui.button(
                                    t("docs.btn_view", lang),
                                    icon="visibility",
                                    on_click=lambda d=h.document_id: (
                                        dialog.close(),
                                        ui.navigate.to(f"/viewer?doc={d}&page=1"),
                                    ),
                                ).props("dense flat")
                                ui.button(
                                    t("docs.btn_pdf", lang),
                                    icon="picture_as_pdf",
                                    on_click=lambda d=h.document_id: open_pdf(d, 1),
                                ).props("dense flat")

        async def _show_summary(did: int, fname: str) -> None:
            from app.chat.rag import summarize_document

            with ui.dialog() as dialog, ui.card().classes("w-[640px] p-4"):
                ui.label(f"{t('docs.summary_of', lang)} {fname}").classes("text-h6 ldi-primary")
                spinner = ui.spinner(size="lg")
                md = ui.markdown("")
                dialog.open()
                text = await summarize_document(did)
                spinner.delete()
                md.content = text

        q_input = ui.input(t("docs.filter", lang)).classes("w-full")
        results = ui.column().classes("w-full gap-2")

        def _refresh() -> None:
            results.clear()
            with results, session_scope() as session:
                stmt = select(Document)
                if q_input.value:
                    like = f"%{q_input.value}%"
                    stmt = stmt.where(Document.filename.like(like))  # type: ignore
                stmt = stmt.order_by(Document.id.desc()).limit(200)  # type: ignore
                for d in session.exec(stmt).all():
                    with ui.card().classes("w-full p-3"):
                        ui.label(d.filename).classes("text-h6")
                        ui.label(d.path).classes("text-caption opacity-70 break-all")
                        ui.label(
                            f"{t('docs.pages', lang)}: {d.page_count} · "
                            f"{t('docs.status', lang)}: {d.status.value} · "
                            f"{t('docs.type', lang)}: {d.doc_type or '—'} · "
                            f"{t('docs.lang', lang)}: {d.language or '—'}"
                        ).classes("text-caption")
                        with ui.row().classes("gap-1 q-mt-xs"):
                            ui.button(
                                t("docs.btn_view", lang),
                                icon="visibility",
                                on_click=lambda did=d.id: ui.navigate.to(f"/viewer?doc={did}&page=1"),
                            ).props("dense flat")
                            ui.button(
                                t("docs.btn_pdf", lang),
                                icon="picture_as_pdf",
                                on_click=lambda did=d.id: open_pdf(did, 1),
                            ).props("dense flat")
                            ui.button(
                                t("docs.btn_similar", lang),
                                icon="auto_awesome",
                                on_click=lambda did=d.id, fname=d.filename: _show_similar(did, fname),
                            ).props("dense flat")
                            ui.button(
                                t("docs.btn_summarize", lang),
                                icon="summarize",
                                on_click=lambda did=d.id, fname=d.filename: _show_summary(did, fname),
                            ).props("dense flat")

        q_input.on("change", lambda _: _refresh())
        ui.button(t("common.refresh", lang), icon="refresh", on_click=_refresh).props("dense")
        _refresh()

    @ui.page("/search")
    def page_search() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/search")
        lang = _user_lang(user)
        ui.label(t("search.title", lang)).classes("text-h4 q-mb-md ldi-primary")
        q = ui.input(t("search.placeholder", lang)).classes("w-full")
        rerank_toggle = ui.checkbox(t("search.rerank", lang), value=False)
        out = ui.column().classes("w-full gap-2")

        async def _go() -> None:
            out.clear()
            if not q.value.strip():
                return
            from app.services.search_service import hybrid_search

            hits = await hybrid_search(q.value, rerank=rerank_toggle.value, user=user)
            with out:
                if not hits:
                    ui.label(t("search.no_results", lang)).classes("opacity-70")
                    return
                for h in hits:
                    with ui.card().classes("w-full p-3"):
                        ui.label(f"{h.filename} (p.{h.page_from})").classes("text-h6")
                        ui.label(h.snippet).classes("text-body2")
                        with ui.row().classes("gap-2 q-mt-xs"):
                            ui.label(f"{t('search.score', lang)}: {h.score:.3f}").classes(
                                "text-caption opacity-70"
                            )
                            ui.label(f"{t('search.source', lang)}: {h.source}").classes(
                                "text-caption opacity-70"
                            )
                            ui.button(
                                t("search.btn_view", lang),
                                icon="visibility",
                                on_click=lambda did=h.document_id, pg=h.page_from, qv=q.value: ui.navigate.to(
                                    f"/viewer?doc={did}&page={pg}&q={qv}"
                                ),
                            ).props("dense flat")
                            ui.button(
                                t("search.btn_pdf", lang),
                                icon="picture_as_pdf",
                                on_click=lambda did=h.document_id, pg=h.page_from: open_pdf(did, pg),
                            ).props("dense flat")

        with ui.row().classes("gap-2"):
            ui.button(t("search.go", lang), icon="search", on_click=_go).props("color=primary")

            async def _export(fmt: str) -> None:
                from app.services.exports import search_hits_to_csv, search_hits_to_json
                from app.services.search_service import hybrid_search

                hits = await hybrid_search(q.value, top_k=100, user=user)
                payload = search_hits_to_csv(hits) if fmt == "csv" else search_hits_to_json(hits)
                ui.download(
                    payload.encode("utf-8"),
                    filename=f"search.{fmt}",
                    media_type=f"text/{fmt}",
                )

            ui.button(t("search.btn_csv", lang), icon="download", on_click=lambda: _export("csv")).props(
                "dense"
            )
            ui.button(t("search.btn_json", lang), icon="download", on_click=lambda: _export("json")).props(
                "dense"
            )
        q.on("keydown.enter", lambda _: _go())

    @ui.page("/chat")
    def page_chat() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/chat")
        lang = _user_lang(user)
        ui.label(t("chat.title", lang)).classes("text-h4 q-mb-md ldi-primary")

        chat_state: dict = {"chat_id": None}

        with ui.row().classes("w-full gap-4 no-wrap").style("height: calc(100vh - 180px)"):
            # Left: chat list
            with ui.column().classes("gap-2").style("min-width: 260px; max-width: 280px"):
                ui.label(t("chat.list_title", lang)).classes("text-h6")
                with ui.row().classes("gap-1"):
                    ui.button(t("chat.new", lang), icon="add", on_click=lambda: _new_chat()).props(
                        "color=primary dense"
                    )

                    def _export_chat_md() -> None:
                        cid = chat_state.get("chat_id")
                        if not cid:
                            ui.notify(t("chat.open_first", lang), color="warning")
                            return
                        from app.services.exports import chat_to_markdown

                        md = chat_to_markdown(cid)
                        ui.download(md.encode("utf-8"), filename=f"chat-{cid}.md", media_type="text/markdown")

                    def _export_chat_pdf() -> None:
                        cid = chat_state.get("chat_id")
                        if not cid:
                            ui.notify(t("chat.open_first", lang), color="warning")
                            return
                        from app.services.exports import chat_to_pdf

                        ui.download(
                            chat_to_pdf(cid), filename=f"chat-{cid}.pdf", media_type="application/pdf"
                        )

                    ui.button(t("chat.btn_md", lang), icon="download", on_click=_export_chat_md).props(
                        "dense"
                    )
                    ui.button(
                        t("chat.btn_pdf", lang), icon="picture_as_pdf", on_click=_export_chat_pdf
                    ).props("dense")
                chat_list = ui.column().classes("gap-1")

                def _refresh_chats() -> None:
                    chat_list.clear()
                    with chat_list, session_scope() as session:
                        chats = session.exec(
                            select(Chat)
                            .where(Chat.user_id == user.id)
                            .order_by(Chat.updated_at.desc())  # type: ignore
                        ).all()
                        for c in chats:
                            with ui.row().classes("items-center w-full"):
                                ui.button(
                                    c.title[:30],
                                    on_click=lambda cid=c.id: _open_chat(cid),
                                ).props(
                                    "flat dense"
                                ).classes("flex-1 justify-start")
                                ui.button(
                                    icon="delete",
                                    on_click=lambda cid=c.id: _delete_chat(cid),
                                ).props("flat dense color=negative")

                def _new_chat() -> None:
                    with session_scope() as session:
                        c = Chat(user_id=user.id, title="New chat")
                        session.add(c)
                        session.flush()
                        cid = c.id
                    chat_state["chat_id"] = cid
                    _refresh_chats()
                    _refresh_msgs()

                def _open_chat(cid: int) -> None:
                    chat_state["chat_id"] = cid
                    _refresh_msgs()

                def _delete_chat(cid: int) -> None:
                    with session_scope() as session:
                        for m in session.exec(select(ChatMessage).where(ChatMessage.chat_id == cid)).all():
                            session.delete(m)
                        for c in session.exec(
                            select(ChatContextItem).where(ChatContextItem.chat_id == cid)
                        ).all():
                            session.delete(c)
                        ch = session.get(Chat, cid)
                        if ch:
                            session.delete(ch)
                    if chat_state.get("chat_id") == cid:
                        chat_state["chat_id"] = None
                    _refresh_chats()
                    _refresh_msgs()

            # Right: messages
            with ui.column().classes("flex-1 gap-2").style("min-width: 0"):
                msg_area = (
                    ui.column().classes("w-full gap-2 q-pa-sm overflow-auto").style("flex: 1; min-height: 0")
                )
                input_row = ui.row().classes("w-full no-wrap")
                with input_row:
                    inp = ui.input(placeholder=t("chat.ph_ask", lang)).classes("flex-1")

                    async def _send() -> None:
                        if chat_state.get("chat_id") is None:
                            _new_chat()
                        question = (inp.value or "").strip()
                        if not question:
                            return
                        inp.value = ""
                        with msg_area, ui.card().classes("w-full p-3 ldi-border"):
                            ui.label(t("chat.you", lang)).classes("text-caption opacity-70")
                            ui.label(question)

                        # Streaming answer
                        from app.chat.rag import stream_answer

                        answer_card = None
                        answer_md = None
                        cites_state: list[dict] = []
                        buffer = []
                        async for ev in stream_answer(
                            chat_id=chat_state["chat_id"],
                            user=user,
                            question=question,
                        ):
                            ev_t = ev.get("type")
                            if ev_t == "sources":
                                cites_state = ev.get("citations", [])
                                with msg_area:
                                    answer_card = ui.card().classes("w-full p-3")
                                    with answer_card:
                                        ui.label(t("chat.assistant", lang)).classes("text-caption ldi-accent")
                                        answer_md = ui.markdown("")
                            elif ev_t == "token":
                                buffer.append(ev.get("text", ""))
                                if answer_md is not None:
                                    answer_md.content = "".join(buffer)
                            elif ev_t == "done":
                                if cites_state and answer_card is not None:
                                    with answer_card:
                                        with ui.expansion(t("chat.sources", lang), icon="link"):
                                            for c in cites_state:
                                                _render_citation(c)

                    ui.button(icon="send", on_click=_send).props("color=primary")
                    inp.on("keydown.enter", lambda _: _send())

                def _refresh_msgs() -> None:
                    msg_area.clear()
                    cid = chat_state.get("chat_id")
                    if not cid:
                        with msg_area:
                            ui.label(t("chat.start_or_pick", lang)).classes("opacity-70")
                        return
                    with msg_area, session_scope() as session:
                        msgs = session.exec(
                            select(ChatMessage).where(ChatMessage.chat_id == cid).order_by(ChatMessage.id)
                        ).all()
                        for m in msgs:
                            role_label = (
                                t("chat.you", lang)
                                if m.role == "user"
                                else (
                                    t("chat.assistant", lang)
                                    if m.role == "assistant"
                                    else m.role.capitalize()
                                )
                            )
                            with ui.card().classes("w-full p-3"):
                                ui.label(role_label).classes(
                                    "text-caption "
                                    + ("ldi-accent" if m.role == "assistant" else "opacity-70")
                                )
                                ui.markdown(m.content)
                                if m.sources:
                                    with ui.expansion(t("chat.sources", lang)).classes("q-mt-sm"):
                                        for s in m.sources:
                                            _render_citation(s)

                _refresh_chats()
                _refresh_msgs()

    @ui.page("/tags")
    def page_tags() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/tags")
        lang = _user_lang(user)
        ui.label(t("tags.title", lang)).classes("text-h4 q-mb-md ldi-primary")
        from app.models import Tag

        results = ui.column().classes("w-full gap-2")

        def _refresh() -> None:
            results.clear()
            with results, session_scope() as session:
                tags = session.exec(select(Tag).order_by(Tag.name)).all()
                for tag in tags:
                    with ui.row().classes("items-center gap-2 w-full"):
                        ui.label(tag.name).classes("flex-1")
                        ui.label(t("tags.auto", lang) if tag.auto else "").classes("opacity-50 text-caption")
                        ui.button(
                            icon="delete",
                            on_click=lambda tid=tag.id: _delete(tid),
                        ).props("flat dense color=negative")

        def _delete(tid: int) -> None:
            from app.models import DocumentTagLink

            with session_scope() as session:
                for link in session.exec(select(DocumentTagLink).where(DocumentTagLink.tag_id == tid)).all():
                    session.delete(link)
                t = session.get(Tag, tid)
                if t:
                    session.delete(t)
            _refresh()

        _refresh()

    @ui.page("/backup")
    def page_backup() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/backup")
        lang = _user_lang(user)
        ui.label(t("backup.title", lang)).classes("text-h4 q-mb-md ldi-primary")

        from app.backup import BACKUP_COMPONENTS, create_backup, list_backups, restore_backup

        with ui.card().classes("w-full p-3"):
            ui.label(t("backup.create", lang)).classes("text-h6")
            checks: dict[str, ui.checkbox] = {}
            with ui.row().classes("gap-3 flex-wrap"):
                for comp in BACKUP_COMPONENTS:
                    checks[comp] = ui.checkbox(
                        comp, value=comp in ("db", "vector", "chats", "memory", "settings")
                    )
            pw = ui.input(t("backup.password_enc", lang), password=True, password_toggle_button=True).classes(
                "w-full"
            )

            def _do_backup() -> None:
                comps = [k for k, c in checks.items() if c.value]
                if not comps:
                    ui.notify(t("backup.select_at_least_one", lang), color="negative")
                    return
                res = create_backup(comps, encrypt_password=pw.value or None)
                ui.notify(f"{t('backup.written_to', lang)} {res.path}", color="positive")
                _refresh()

            ui.button(t("backup.create_btn", lang), icon="save", on_click=_do_backup).props(
                "color=primary"
            ).classes("q-mt-md")

        listing = ui.column().classes("w-full gap-2 q-mt-md")

        def _refresh() -> None:
            listing.clear()
            with listing:
                ui.label(t("backup.existing", lang)).classes("text-h6")
                for b in list_backups():
                    with ui.card().classes("w-full p-2"):
                        ui.label(b["filename"]).classes("text-body1")
                        ui.label(
                            f"{b['size_bytes']} bytes · components: "
                            f"{', '.join(b['components'])} · encrypted: {b['encrypted']}"
                        ).classes("text-caption opacity-70")

                        async def _restore(p=b["path"]) -> None:
                            res = restore_backup(p)
                            ui.notify(
                                f"{t('backup.restored', lang)}: {res['restored']}; "
                                f"{t('backup.errors', lang)}: {res['errors']}",
                                color="positive",
                            )

                        ui.button(t("backup.restore", lang), on_click=_restore).props("dense")

        _refresh()

    @ui.page("/settings")
    def page_settings() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/settings")
        lang = _user_lang(user)
        ui.label(t("settings.title", lang)).classes("text-h4 q-mb-md ldi-primary")
        s = get_settings()

        with ui.card().classes("w-full p-3"):
            ui.label(t("settings.lmstudio", lang)).classes("text-h6")
            url = ui.input(t("settings.base_url", lang), value=s.lmstudio_base_url).classes("w-full")
            chat_model = ui.input(t("settings.chat_model", lang), value=s.chat_model).classes("w-full")
            vision_model = ui.input(t("settings.vision_model", lang), value=s.vision_model).classes("w-full")
            emb_model = ui.input(t("settings.emb_model", lang), value=s.embedding_model).classes("w-full")

            async def _test() -> None:
                from app.llm import LMStudioClient

                c = LMStudioClient(base_url=url.value)
                ok = await c.ping()
                ui.notify(
                    f"{t('settings.lm_reachable', lang)} {ok}",
                    color=("positive" if ok else "negative"),
                )

            ui.button(t("settings.test_connection", lang), icon="cable", on_click=_test).props("dense")

        with ui.card().classes("w-full p-3 q-mt-md"):
            ui.label(t("settings.ocr", lang)).classes("text-h6")
            tcmd = ui.input(t("settings.tesseract", lang), value=s.tesseract_cmd).classes("w-full")
            tlang = ui.input(t("settings.ocr_langs", lang), value=s.ocr_lang).classes("w-full")

        with ui.card().classes("w-full p-3 q-mt-md"):
            ui.label(t("settings.indexing", lang)).classes("text-h6")
            csize = ui.number(t("settings.chunk_size", lang), value=s.chunk_size).classes("w-32")
            coverlap = ui.number(t("settings.chunk_overlap", lang), value=s.chunk_overlap).classes("w-32")

        with ui.card().classes("w-full p-3 q-mt-md"):
            ui.label(t("settings.appearance", lang)).classes("text-h6")
            theme = ui.select(
                {k: v.label for k, v in THEMES.items()},
                value=_user_theme(user),
                label=t("common.theme", lang),
            ).classes("w-64")
            language = ui.select(
                {"en": "English", "de": "Deutsch"},
                value=_user_lang(user),
                label=t("common.language", lang),
            ).classes("w-64")

        def _save() -> None:
            save_user_settings(
                {
                    "lmstudio_base_url": url.value,
                    "chat_model": chat_model.value,
                    "vision_model": vision_model.value,
                    "embedding_model": emb_model.value,
                    "tesseract_cmd": tcmd.value,
                    "ocr_lang": tlang.value,
                    "chunk_size": int(csize.value or s.chunk_size),
                    "chunk_overlap": int(coverlap.value or s.chunk_overlap),
                }
            )
            get_settings.cache_clear()
            with session_scope() as session:
                us = session.exec(select(UserSetting).where(UserSetting.user_id == user.id)).first()
                if not us:
                    us = UserSetting(user_id=user.id)
                us.theme = theme.value
                us.language = language.value
                session.add(us)
            ui.notify(t("settings.saved_reload", lang), color="positive")
            ui.navigate.reload()

        ui.button(t("settings.save", lang), icon="save", on_click=_save).props("color=primary").classes(
            "q-mt-md"
        )

        with ui.card().classes("w-full p-3 q-mt-md"):
            ui.label(t("settings.change_pw", lang)).classes("text-h6")
            old = ui.input(
                t("common.current_password", lang), password=True, password_toggle_button=True
            ).classes("w-full")
            new = ui.input(
                t("common.new_password", lang), password=True, password_toggle_button=True
            ).classes("w-full")

            def _change() -> None:
                with session_scope() as session:
                    u = session.get(User, user.id)
                    if not u or not verify_password(old.value, u.password_hash):
                        ui.notify(t("settings.pw_wrong", lang), color="negative")
                        return
                    u.password_hash = hash_password(new.value)
                    session.add(u)
                ui.notify(t("settings.pw_updated", lang), color="positive")

            ui.button(t("settings.update_pw", lang), on_click=_change).props("color=primary")

    @ui.page("/logs")
    def page_logs() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/logs")
        lang = _user_lang(user)
        ui.label(t("logs.title", lang)).classes("text-h4 q-mb-md ldi-primary")
        s = get_settings()
        log_file = s.logs_path / "localdoc.log"
        if log_file.exists():
            try:
                text_blob = log_file.read_text(encoding="utf-8", errors="replace")[-20_000:]
            except Exception as e:
                text_blob = f"(cannot read log: {e})"
        else:
            text_blob = t("logs.empty", lang)
        ui.code(text_blob, language="text").classes("w-full")

    @ui.page("/compare")
    def page_compare() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/compare")
        lang = _user_lang(user)
        ui.label(t("compare.title", lang)).classes("text-h4 q-mb-md ldi-primary")

        with session_scope() as session:
            docs = session.exec(select(Document).order_by(Document.id.desc()).limit(2000)).all()
            options = {d.id: f"#{d.id} — {d.filename}" for d in docs if d.id is not None}

        if not options:
            ui.label(t("compare.index_first", lang)).classes("opacity-70")
            return

        with ui.row().classes("w-full gap-2"):
            sel_a = ui.select(options, label=t("compare.doc_a", lang), with_input=True).classes("flex-1")
            sel_b = ui.select(options, label=t("compare.doc_b", lang), with_input=True).classes("flex-1")

        output = ui.column().classes("w-full gap-2 q-mt-md")

        async def _run() -> None:
            if not sel_a.value or not sel_b.value or sel_a.value == sel_b.value:
                ui.notify(t("compare.pick_two_different", lang), color="warning")
                return
            output.clear()
            from app.services.compare import compare_documents

            with output:
                spinner = ui.spinner(size="lg")
            result = await compare_documents(int(sel_a.value), int(sel_b.value))
            spinner.delete()
            with output:
                with ui.card().classes("w-full p-3"):
                    ui.label(t("compare.narrative", lang)).classes("text-h6")
                    ui.markdown(result.narrative)
                with ui.card().classes("w-full p-3"):
                    ui.label(f"{t('compare.shared_ratio', lang)} {result.shared_ratio:.1%}").classes(
                        "text-body1"
                    )
                with ui.row().classes("w-full gap-2"):
                    with ui.card().classes("flex-1 p-3"):
                        ui.label(f"{t('compare.only_in', lang)} {result.doc_a.get('filename')}").classes(
                            "text-h6"
                        )
                        for ln in result.only_in_a_sample:
                            ui.label(ln).classes("text-caption opacity-80")
                    with ui.card().classes("flex-1 p-3"):
                        ui.label(f"{t('compare.only_in', lang)} {result.doc_b.get('filename')}").classes(
                            "text-h6"
                        )
                        for ln in result.only_in_b_sample:
                            ui.label(ln).classes("text-caption opacity-80")

        ui.button(t("compare.btn", lang), icon="compare_arrows", on_click=_run).props("color=primary")

    @ui.page("/diagnostics")
    def page_diagnostics() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/diagnostics")
        lang = _user_lang(user)
        ui.label(t("diag.title", lang)).classes("text-h4 q-mb-md ldi-primary")

        import humanize

        from app.services.diagnostics import (
            cleanup_orphan_caches,
            find_duplicates,
            index_overview,
            lmstudio_status,
            orphan_caches,
            storage_overview,
        )

        async def _refresh() -> None:
            storage_card.clear()
            idx_card.clear()
            lm_card.clear()
            dup_card.clear()
            orph_card.clear()

            with storage_card:
                ui.label(t("diag.storage", lang)).classes("text-h6")
                for name, info in storage_overview().items():
                    ui.label(f"{name}: {humanize.naturalsize(info['size_bytes'])} — {info['path']}").classes(
                        "text-caption"
                    )

            with idx_card:
                ui.label(t("diag.index", lang)).classes("text-h6")
                for k, v in index_overview().items():
                    ui.label(f"{k}: {v}").classes("text-caption")

            with lm_card:
                ui.label(t("diag.lmstudio", lang)).classes("text-h6")
                status = await lmstudio_status()
                colour = "text-positive" if status["reachable"] else "text-negative"
                ui.label(
                    f"{t('settings.lm_reachable', lang)} {status['reachable']} ({status['base_url']})"
                ).classes(f"text-caption {colour}")
                if status.get("models"):
                    ui.label(
                        f"{t('diag.models', lang)} "
                        + ", ".join(status["models"][:10])
                        + (" …" if len(status["models"]) > 10 else "")
                    ).classes("text-caption opacity-70")

            with dup_card:
                ui.label(t("diag.duplicates", lang)).classes("text-h6")
                dups = find_duplicates()
                if not dups:
                    ui.label(t("diag.dup_none", lang)).classes("text-caption opacity-70")
                for grp in dups[:50]:
                    with ui.expansion(f"{len(grp['documents'])} docs share hash {grp['content_hash'][:12]}…"):
                        for d in grp["documents"]:
                            ui.label(f"#{d['id']}  {d['filename']}").classes("text-caption")
                            ui.label(d["path"]).classes("text-caption opacity-60 break-all")

            with metrics_card:
                from app.services.metrics import queue_metrics, system_metrics

                ui.label(t("diag.metrics", lang)).classes("text-h6")
                sysm = system_metrics()
                queue = queue_metrics()
                if sysm.get("available"):
                    with ui.row().classes("gap-3 flex-wrap"):
                        ui.label(
                            f"CPU (proc/sys): {sysm['process_cpu_percent']:.0f}% / "
                            f"{sysm['system_cpu_percent']:.0f}% on {sysm['system_cpu_count']} cores"
                        ).classes("text-caption")
                        ui.label(
                            f"RSS: {humanize.naturalsize(sysm['process_rss_bytes'])} · "
                            f"RAM used: {sysm['system_memory_percent']:.0f}%"
                        ).classes("text-caption")
                        ui.label(f"Threads: {sysm['process_threads']}").classes("text-caption")
                else:
                    ui.label(t("diag.metrics_disabled", lang)).classes("text-caption opacity-70")
                ui.label(
                    f"Jobs running={queue['running']} · paused={queue['paused']} · "
                    f"queued={queue['queued']} · in-mem={len(queue['in_memory_jobs'])} · "
                    f"watchers={len(queue['watchers_active'])}"
                ).classes("text-caption")
                for det in queue["running_details"]:
                    ui.label(
                        f"  ↪ job #{det['id']} on source {det['source_id']}: "
                        f"{det['processed']}/{det['total']} · {det.get('current') or '—'}"
                    ).classes("text-caption opacity-70")

            with near_card:
                ui.label(t("diag.near_dup", lang)).classes("text-h6")
                from app.services.near_dup import find_near_duplicates

                pairs = find_near_duplicates(threshold=0.7)
                if not pairs:
                    ui.label(t("diag.near_dup_none", lang)).classes("text-caption opacity-70")
                for p in pairs[:30]:
                    with ui.row().classes("items-center w-full"):
                        ui.label(f"{p.a_filename}  ⇔  {p.b_filename}  ({p.similarity:.0%})").classes(
                            "text-caption flex-1"
                        )
                        ui.button(
                            t("diag.compare", lang),
                            icon="compare_arrows",
                            on_click=lambda aid=p.a_id, bid=p.b_id: ui.navigate.to(
                                f"/compare?a={aid}&b={bid}"
                            ),
                        ).props("dense flat")

            with orph_card:
                ui.label(t("diag.orphans", lang)).classes("text-h6")
                orph = orphan_caches()
                total = len(orph["pages"]) + len(orph["images"])
                ui.label(t("diag.orphans_found", lang).format(n=total)).classes("text-caption")
                if total:

                    def _cleanup() -> None:
                        counts = cleanup_orphan_caches()
                        ui.notify(
                            f"{t('diag.clean_done', lang)}: {counts['pages']} pages, {counts['images']} images",
                            color="positive",
                        )

                    ui.button(
                        t("diag.clean_orphans", lang),
                        icon="cleaning_services",
                        on_click=lambda: (_cleanup(), ui.timer(0.1, _refresh, once=True)),
                    ).props("dense")

        with ui.row().classes("w-full gap-3 flex-wrap"):
            storage_card = ui.card().classes("p-3").style("min-width: 320px; flex: 1")
            idx_card = ui.card().classes("p-3").style("min-width: 220px")
            lm_card = ui.card().classes("p-3").style("min-width: 280px; flex: 1")
        metrics_card = ui.card().classes("w-full p-3 q-mt-md")
        dup_card = ui.card().classes("w-full p-3 q-mt-md")
        near_card = ui.card().classes("w-full p-3 q-mt-md")
        orph_card = ui.card().classes("w-full p-3 q-mt-md")

        ui.timer(0.05, lambda: _refresh(), once=True)
        ui.button(t("diag.refresh", lang), icon="refresh", on_click=lambda: _refresh()).props("dense")

    @ui.page("/viewer")
    def page_viewer(doc: int = 0, page: int = 1, q: str = "") -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/documents")
        lang = _user_lang(user)
        with session_scope() as session:
            d = session.get(Document, doc)
            if not d:
                ui.label(t("docs.viewer.not_found", lang)).classes("text-h6 text-negative")
                return
            from app.models import DocumentPage as _DP

            total_pages = d.page_count or 1
            page = max(1, min(page or 1, total_pages))
            page_row = session.exec(
                select(_DP).where(_DP.document_id == doc, _DP.page_number == page)
            ).first()
            page_text = (page_row.native_text or page_row.ocr_text or "") if page_row else ""

        ui.label(f"{d.filename} — {t('docs.viewer.page_of', lang)} {page}/{total_pages}").classes(
            "text-h5 ldi-primary"
        )
        with ui.row().classes("gap-2 q-mb-md"):
            ui.button(
                t("docs.viewer.prev", lang),
                icon="navigate_before",
                on_click=lambda: ui.navigate.to(f"/viewer?doc={doc}&page={max(1, page - 1)}&q={q}"),
            ).props("dense")
            ui.button(
                t("docs.viewer.next", lang),
                icon="navigate_next",
                on_click=lambda: ui.navigate.to(f"/viewer?doc={doc}&page={min(total_pages, page + 1)}&q={q}"),
            ).props("dense")
            ui.button(
                t("docs.viewer.open_pdf", lang),
                icon="picture_as_pdf",
                on_click=lambda d=doc, p=page: open_pdf(d, p),
            ).props("dense color=primary")

        with ui.row().classes("w-full gap-4 no-wrap"):
            with ui.column().classes("gap-2").style("min-width: 0; flex: 2"):
                ui.image(f"/api/documents/{doc}/page/{page}/image").classes("w-full ldi-border").style(
                    "border: 1px solid; max-height: 80vh; object-fit: contain"
                )
            with ui.column().classes("gap-2").style("min-width: 280px; flex: 1"):
                ui.label(t("docs.viewer.page_text", lang)).classes("text-h6")
                snippet = page_text
                if q and q.lower() in snippet.lower():
                    idx = snippet.lower().find(q.lower())
                    snippet = (
                        snippet[max(0, idx - 200) : idx]
                        + "**"
                        + snippet[idx : idx + len(q)]
                        + "**"
                        + snippet[idx + len(q) : idx + len(q) + 200]
                    )
                ui.markdown(snippet or f"_{t('docs.viewer.no_text', lang)}_").classes("text-body2")

    @ui.page("/about")
    def page_about() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/about")
        lang = _user_lang(user)
        ui.label(t("about.title", lang)).classes("text-h4 q-mb-md ldi-primary")
        from app import __app_name__ as N
        from app import __author__, __contact__, __handle__
        from app import __version__ as V

        with ui.card().classes("w-full p-4"):
            ui.label(N).classes("text-h5")
            ui.label(f"{t('about.version', lang)} {V}").classes("opacity-70")
            ui.label(t("about.description", lang)).classes("q-mt-sm")
            ui.separator().classes("q-my-md")
            ui.label(f"{t('about.author', lang)}: {__author__}")
            ui.label(f"{t('about.contact', lang)}: {__contact__}")
            ui.label(f"{t('about.handle', lang)}: {__handle__}")
            ui.link(
                t("about.github", lang), "https://github.com/varous555/localdoc-intelligence", new_tab=True
            )
            ui.separator().classes("q-my-md")
            ui.label(t("about.license", lang)).classes("text-caption")
            ui.label(t("about.privacy_note", lang)).classes("text-caption opacity-70")
            ui.label(t("about.built_with", lang)).classes("text-caption opacity-70")

            ui.separator().classes("q-my-md")

            async def _check_now() -> None:
                _UPDATE_STATE["checked_at"] = 0.0
                await _refresh_update_state()
                info = _UPDATE_STATE.get("info")
                if info and not info.up_to_date and info.latest:
                    ui.notify(
                        t("update.available", lang).format(latest=info.latest),
                        color="warning",
                    )
                    ui.navigate.reload()
                else:
                    ui.notify(t("update.up_to_date", lang), color="positive")

            ui.button(t("update.check_now", lang), icon="system_update", on_click=_check_now).props("outline")

    # Finally bind NiceGUI to the FastAPI app.
    # Keep the favicon ASCII — an emoji favicon traverses Windows console
    # codepage on PyInstaller builds and can blow up early init.
    ui.run_with(
        fastapi_app,
        storage_secret=get_settings().secret_key,
        mount_path="/",
        title=__app_name__,
        favicon=None,
        dark=True,
    )
