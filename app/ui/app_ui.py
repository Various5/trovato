"""NiceGUI frontend — registers all UI pages onto the FastAPI app.

The UI talks to the same process via direct Python service calls (no
HTTP roundtrips). Session-cookie auth is shared with the API.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import FastAPI
from nicegui import app as nicegui_app
from nicegui import ui
from sqlalchemy import func
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
from app.ui.styles import build_global_css
from app.ui.themes import THEMES
from app.utils.i18n import SUPPORTED_LANGUAGES, t
from app.utils.logging import logger
from app.utils.secret_store import delete_secret, get_secret, put_secret

REMEMBER_SECRET_NAME = "ui_login_remember"


def _current_user() -> User | None:
    try:
        uid = nicegui_app.storage.user.get(SESSION_USER_KEY)
        if uid is None:
            return None
        with session_scope() as session:
            return session.get(User, uid)
    except Exception:
        return None


def _bridge_session_cookie(uid: int | None) -> None:
    """Mirror the UI login into the Starlette session cookie.

    The UI authenticates via ``app.storage.user`` (server-side), but the REST
    API — page-image previews (``/api/documents/{id}/page/{n}/image``) and PDF
    open/download (``/api/documents/{id}/file``) — authenticates via
    ``request.session`` (``login_required``). Browser-issued requests for those
    URLs can't reach ``app.storage.user`` (it's unavailable outside NiceGUI's
    request context), so without this they look unauthenticated → 401 → broken
    thumbnails and "login required" on open.

    ``app.storage.browser`` *is* ``request.session``; writing the uid here
    during a page build sets the session cookie, so the browser then sends it
    on those sub-requests. Best-effort: browser storage is only writable while
    a page is being built, so guard against the read-only (event-handler) case.
    """
    if uid is None:
        return
    try:
        if nicegui_app.storage.browser.get(SESSION_USER_KEY) != uid:
            nicegui_app.storage.browser[SESSION_USER_KEY] = uid
    except Exception:
        # Read-only outside an initial page request — nothing to do.
        pass


def _require_login() -> User | None:
    u = _current_user()
    if not u:
        ui.navigate.to("/login")
        return None
    _bridge_session_cookie(u.id)
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


def _now_utc():
    from datetime import datetime

    return datetime.now(UTC)


def _maybe_send_on_enter(event, send_fn) -> None:
    """Send on plain Enter; allow Shift+Enter for newline.

    NiceGUI's `keydown.enter` fires before the textarea inserts the newline,
    so we only need to prevent / not-prevent based on the modifier flag.
    """
    args = getattr(event, "args", {}) or {}
    if args.get("shiftKey"):
        return  # let the textarea grow
    ui.run_javascript("event && event.preventDefault && event.preventDefault();")
    import asyncio

    asyncio.create_task(send_fn())


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


def _apply_theme(theme_name: str = "slate") -> None:
    theme = THEMES.get(theme_name) or THEMES["slate"]
    ui.dark_mode().set_value(theme.is_dark)
    ui.add_head_html(f"<style>{build_global_css(theme_name)}</style>")


def _user_theme(user: User) -> str:
    from app.ui.themes import DEFAULT_THEME

    with session_scope() as session:
        s = session.exec(select(UserSetting).where(UserSetting.user_id == user.id)).first()
        return s.theme if s and s.theme in THEMES else DEFAULT_THEME


def _user_lang(user: User) -> str:
    with session_scope() as session:
        s = session.exec(select(UserSetting).where(UserSetting.user_id == user.id)).first()
        lang = (s.language if s else "en") or "en"
        return lang if lang in SUPPORTED_LANGUAGES else "en"


NAV_ITEMS: list[tuple[str, str, str]] = [
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


def _layout(user: User, current: str) -> None:
    """Build the modern app shell: header + collapsible drawer + page area.

    The header sticks to the top and carries the brand mark, a hamburger to
    collapse the drawer, and a quick-actions area on the right (current page
    label, theme/lang shortcuts, user menu). The drawer is a glass panel that
    can slide off-screen via the hamburger toggle.
    """
    import asyncio

    _apply_theme(_user_theme(user))
    lang = _user_lang(user)
    try:
        asyncio.create_task(_refresh_update_state())
    except RuntimeError:
        pass

    # --- Header ----------------------------------------------------------
    page_title = next((t(k, lang) for k, p, _ in NAV_ITEMS if p == current), __app_name__)

    with (
        ui.header(elevated=False)
        .classes("items-center gap-3 q-px-md")
        .style("height: 60px; padding-left: 12px; padding-right: 12px;")
    ):
        # Drawer toggle (left)
        hamburger = ui.button(icon="menu").props("flat round dense").classes("ldi-accent")

        # Brand
        with ui.row().classes("items-center gap-2 ldi-brand"):
            with ui.element("div").classes("ldi-brand-mark"):
                ui.label("L")
            with ui.column().classes("gap-0"):
                ui.label(__app_name__).classes("text-body1 leading-tight")
                ui.label(f"v{__version__}").classes("text-caption opacity-60 leading-tight")

        ui.space()

        # Current page tag
        ui.label(page_title).classes("text-body2 opacity-80")

        # Live scan indicator — visible on every page while jobs are running.
        scan_indicator = ui.row().classes("items-center gap-2 q-ml-md")

        def _refresh_scan_indicator() -> None:
            from app.models import ScanJob, ScanJobStatus

            scan_indicator.clear()
            with session_scope() as session:
                running = session.exec(select(ScanJob).where(ScanJob.status == ScanJobStatus.running)).all()
            if not running:
                return
            j = running[0]
            total = max(j.total_files or 0, j.processed_files)
            pct = (100.0 * j.processed_files / total) if total else 0.0
            with scan_indicator:
                ui.html('<span class="ldi-status-dot live"></span>')
                with ui.column().classes("gap-0").style("min-width: 170px;"):
                    label_text = (
                        f"Scanning · {j.processed_files}/{total}"
                        if total
                        else f"Scanning · {j.processed_files} files"
                    )
                    if len(running) > 1:
                        label_text += f" (+{len(running) - 1} more)"
                    ui.label(label_text).classes("text-caption")
                    with ui.element("div").classes("ldi-progress").style("height: 4px;"):
                        if total:
                            ui.element("div").classes("ldi-progress-fill").style(f"width: {pct:.1f}%;")
                        else:
                            ui.element("div").classes("ldi-progress-fill indeterminate")
                ui.button(icon="folder_open", on_click=lambda: ui.navigate.to("/sources")).props(
                    "flat dense round"
                ).tooltip("Open Sources")

        _refresh_scan_indicator()
        ui.timer(3.0, _refresh_scan_indicator)

        # Theme cycle button (light/dark quick swap)
        def _cycle_theme() -> None:
            # Cycle through professional themes first, then legacy.
            order = [
                "slate",
                "pearl",
                "obsidian",
                "graphite",
                "light",
                "dark",
                "nord",
                "solarized",
                "dracula",
                "highcontrast",
            ]
            cur = _user_theme(user)
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "slate"
            with session_scope() as session:
                us = session.exec(select(UserSetting).where(UserSetting.user_id == user.id)).first()
                if not us:
                    us = UserSetting(user_id=user.id)
                us.theme = nxt
                session.add(us)
            ui.navigate.reload()

        ui.button(icon="palette", on_click=_cycle_theme).props("flat round dense").tooltip(
            t("common.theme", lang)
        )

        # User menu
        with ui.button(icon="person").props("flat round dense") as user_btn:
            with ui.menu().props("anchor='bottom right' self='top right'"):
                ui.label(f"@{user.username}").classes("text-body2 ldi-primary q-px-md q-pt-sm")
                ui.label(user.role.value).classes("text-caption opacity-70 q-px-md q-pb-sm")
                ui.separator()
                ui.menu_item(t("nav.settings", lang), on_click=lambda: ui.navigate.to("/settings")).props(
                    "icon=settings"
                )
                ui.menu_item(t("btn.logout", lang), on_click=_do_logout).props("icon=logout")
        _ = user_btn  # silence linter

    # --- Drawer ----------------------------------------------------------
    with ui.left_drawer(value=True, bordered=False).props("width=260").classes("q-pa-md gap-1") as drawer:
        with ui.column().classes("gap-0 q-mb-md"):
            ui.label(t("nav.dashboard", lang).upper() if False else "").classes(
                "text-caption opacity-50 q-px-sm"
            )
            # Section header
            ui.label("MENU").classes("text-caption opacity-50 q-px-sm").style("letter-spacing: 0.12em;")

        for key, path, icon in NAV_ITEMS:
            classes = "ldi-nav-item"
            if current == path:
                classes += " active"
            with (
                ui.button(on_click=lambda p=path: ui.navigate.to(p))
                .props("flat align=left no-caps")
                .classes(classes)
            ):
                with ui.row().classes("items-center gap-3 w-full no-wrap"):
                    ui.icon(icon).classes("text-xl")
                    ui.label(t(key, lang)).classes("text-body2")

        ui.space()
        ui.separator().classes("q-my-md")
        with (ui.row().classes("items-center gap-2 q-px-sm"),):
            ui.icon("verified_user").classes("ldi-accent")
            ui.label(user.username).classes("text-body2 flex-1")
            ui.label(user.role.value).classes("text-caption opacity-60")

    # Wire up the hamburger now that the drawer exists
    hamburger.on("click", lambda: drawer.toggle())

    # --- Update banner sits at the top of the page area ------------------
    _render_update_banner(lang)


def _do_logout() -> None:
    # Clear only the auth/session key — NOT storage.user.clear(), which also
    # wiped UI prefs like dismissed update banners (they'd reappear on re-login).
    try:
        nicegui_app.storage.user.pop(SESSION_USER_KEY, None)
    except Exception as e:
        logger.warning("logout: clearing session failed: {}", e)
    # Don't wipe the saved credentials on logout — but disable auto-login so
    # the user explicitly clicks "Sign in" once before being auto-signed back.
    try:
        stored = get_secret(REMEMBER_SECRET_NAME)
        if stored and stored.get("auto"):
            stored["auto"] = False
            put_secret(REMEMBER_SECRET_NAME, stored)
    except Exception as e:
        logger.warning("logout: disabling auto-login failed: {}", e)
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
        # Pre-fill from the encrypted credential store. This survives WebView2
        # cookie resets and a moved/uninstalled+reinstalled app.
        stored = get_secret(REMEMBER_SECRET_NAME) or {}
        remembered_user = stored.get("username", "")
        remembered_pw = stored.get("password", "")
        auto_login = bool(stored.get("auto", False))

        # If credentials are stored and "auto" is set, try to log in silently
        # before painting the form so the user doesn't see it at all.
        if auto_login and remembered_user and remembered_pw:
            with session_scope() as session:
                u = session.exec(select(User).where(User.username == remembered_user)).first()
                if u and verify_password(remembered_pw, u.password_hash):
                    nicegui_app.storage.user[SESSION_USER_KEY] = u.id
                    ui.navigate.to("/")
                    return

        with ui.card().classes("absolute-center ldi-static w-96 p-6"):
            ui.label(t("login.title", ph_lang)).classes("text-h5 q-mb-md ldi-primary")
            username = ui.input(t("common.username", ph_lang), value=remembered_user).classes("w-full")
            password = ui.input(
                t("common.password", ph_lang),
                value=remembered_pw,
                password=True,
                password_toggle_button=True,
            ).classes("w-full")
            remember = ui.checkbox(
                "Remember username & password on this device",
                value=bool(remembered_user),
            ).classes("q-mt-sm")
            auto = ui.checkbox(
                "Sign in automatically next time",
                value=auto_login,
            ).classes("q-mt-xs")
            err = ui.label("").classes("text-negative q-mt-sm")

            def _login() -> None:
                with session_scope() as session:
                    u = session.exec(select(User).where(User.username == username.value)).first()
                    if not u or not verify_password(password.value, u.password_hash):
                        err.text = t("login.invalid", ph_lang)
                        return
                    nicegui_app.storage.user[SESSION_USER_KEY] = u.id
                # Encrypted credential persistence — only if the user opted in.
                try:
                    if remember.value:
                        put_secret(
                            REMEMBER_SECRET_NAME,
                            {
                                "username": username.value,
                                "password": password.value,
                                "auto": bool(auto.value),
                            },
                        )
                    else:
                        delete_secret(REMEMBER_SECRET_NAME)
                except Exception as e:
                    logger.warning("login: persisting remember-me failed: {}", e)
                ui.navigate.to("/")

            ui.button(t("btn.login", ph_lang), on_click=_login).props("color=primary").classes(
                "w-full q-mt-md"
            )
            password.on("keydown.enter", lambda _: _login())
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
        lang = _user_lang(user)
        with session_scope() as session:
            doc_count = session.exec(select(func.count()).select_from(Document)).one()
            source_count = session.exec(select(func.count()).select_from(DocumentSource)).one()
            chat_count = session.exec(
                select(func.count()).select_from(Chat).where(Chat.user_id == user.id)
            ).one()
        from app.vectorstore import collection_size

        chunks = collection_size()

        lang = _user_lang(user)
        ui.label(t("dash.title", lang)).classes("text-h4 q-mb-md ldi-hero-text")
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
        # ----- Active scans (live) -----
        from app.models import ScanJob, ScanJobStatus

        active_card = ui.card().classes("w-full p-3 q-mt-md")

        def _refresh_active() -> None:
            active_card.clear()
            with session_scope() as session:
                running = session.exec(select(ScanJob).where(ScanJob.status == ScanJobStatus.running)).all()
                paused = session.exec(select(ScanJob).where(ScanJob.status == ScanJobStatus.paused)).all()
            with active_card:
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.icon("radio_button_checked").classes("ldi-accent")
                    ui.label(t("dash.active_scans", lang)).classes("text-h6 flex-1")
                    ui.button(
                        icon="open_in_new",
                        on_click=lambda: ui.navigate.to("/sources"),
                    ).props(
                        "flat dense"
                    ).tooltip("Open Sources")
                if not running and not paused:
                    ui.label(t("dash.no_scans", lang)).classes("text-caption opacity-70")
                    return
                for j in running + paused:
                    total = max(j.total_files or 0, j.processed_files)
                    pct = (100.0 * j.processed_files / total) if total else 0.0
                    with ui.column().classes("w-full gap-1 q-mb-sm"):
                        with ui.row().classes("items-center gap-2 w-full"):
                            badge = "● running" if j.status == ScanJobStatus.running else "❚❚ paused"
                            badge_cls = (
                                "ldi-pill ldi-pill-success"
                                if j.status == ScanJobStatus.running
                                else "ldi-pill ldi-pill-warning"
                            )
                            ui.label(badge).classes(badge_cls)
                            ui.label(
                                f"job #{j.id} · source {j.source_id} · "
                                f"{j.processed_files}/{total if total else '?'} files"
                                + (f" · {j.error_count} errors" if j.error_count else "")
                            ).classes("text-caption flex-1")
                        with ui.element("div").classes("ldi-progress"):
                            if total:
                                ui.element("div").classes("ldi-progress-fill").style(f"width: {pct:.1f}%;")
                            else:
                                ui.element("div").classes("ldi-progress-fill indeterminate")
                        if j.current_file:
                            ui.label(j.current_file).classes("text-caption opacity-70 ellipsis").style(
                                "max-width: 100%;"
                            )

        _refresh_active()
        ui.timer(3.0, _refresh_active)

        # ----- Mini analytics -----
        from app.services.dashboard import overview as _dash_overview

        agg = _dash_overview()

        # ----- Week-over-week trend tile -----
        trend = agg.get("trend", {})
        with ui.row().classes("w-full gap-3 q-mt-md flex-wrap"):
            with ui.card().classes("p-3").style("flex: 1; min-width: 220px;"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("trending_up").classes("ldi-accent")
                    ui.label(t("dash.this_week", lang)).classes("text-h6 flex-1")
                ui.label(str(trend.get("this_week", 0))).classes("text-h3").style("line-height: 1.05;")
                pct = trend.get("pct_change")
                if pct is None:
                    arrow_txt = "—"
                    arrow_cls = "ldi-pill"
                elif pct > 5:
                    arrow_txt = f"↑ {pct:+.0f}%"
                    arrow_cls = "ldi-pill ldi-pill-success"
                elif pct < -5:
                    arrow_txt = f"↓ {pct:+.0f}%"
                    arrow_cls = "ldi-pill ldi-pill-error"
                else:
                    arrow_txt = f"≈ {pct:+.0f}%"
                    arrow_cls = "ldi-pill"
                with ui.row().classes("items-center gap-2 q-mt-xs"):
                    ui.label(arrow_txt).classes(arrow_cls)
                    ui.label(f"vs last week ({trend.get('prev_week', 0)})").classes("text-caption opacity-70")
                if trend.get("most_active_day"):
                    ui.label(f"Peak: {trend['most_active_day']} ({trend['most_active_count']} docs)").classes(
                        "text-caption opacity-60 q-mt-sm"
                    )

        with ui.row().classes("w-full gap-3 q-mt-md flex-wrap"):
            # Per-day column chart (last 14 days)
            with ui.card().classes("p-3").style("flex: 2; min-width: 360px;"):
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.icon("show_chart").classes("ldi-accent")
                    ui.label(t("dash.indexed_14d", lang)).classes("text-h6 flex-1")
                per_day = agg["per_day"]
                if not per_day or sum(c for _, c in per_day) == 0:
                    ui.label(t("dash.no_docs_indexed", lang)).classes("text-caption opacity-60")
                else:
                    max_v = max(c for _, c in per_day) or 1
                    with ui.row().classes("items-end gap-1 w-full q-mt-sm").style("height: 90px;"):
                        for date_str, count in per_day:
                            height = (count / max_v) * 100 if max_v else 0
                            with (
                                ui.element("div")
                                .classes("ldi-chart-bar")
                                .style(f"height: {max(height, 2)}%;")
                            ):
                                pass
                    with ui.row().classes("text-caption opacity-60 q-mt-xs"):
                        ui.label(per_day[0][0])
                        ui.space()
                        ui.label(f"max: {max_v}/day · total: {sum(c for _, c in per_day)}")
                        ui.space()
                        ui.label(per_day[-1][0])

            # Per-source horizontal bars
            with ui.card().classes("p-3").style("flex: 1; min-width: 280px;"):
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.icon("folder").classes("ldi-accent")
                    ui.label(t("dash.docs_per_source", lang)).classes("text-h6 flex-1")
                per_src = agg["per_source"]
                if not per_src:
                    ui.label(t("dash.no_sources", lang)).classes("text-caption opacity-60")
                else:
                    max_v = max(c for _, c in per_src) or 1
                    for name, count in per_src:
                        with ui.column().classes("w-full gap-0 q-mb-xs"):
                            with ui.row().classes("items-center w-full"):
                                ui.label(name[:24]).classes("text-caption flex-1 ellipsis")
                                ui.label(str(count)).classes("text-caption opacity-80")
                            with ui.element("div").classes("ldi-progress").style("height: 4px;"):
                                ui.element("div").classes("ldi-progress-fill").style(
                                    f"width: {(count / max_v) * 100:.1f}%;"
                                )

            # Doc-type breakdown
            with ui.card().classes("p-3").style("flex: 1; min-width: 260px;"):
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.icon("category").classes("ldi-accent")
                    ui.label(t("dash.doc_types", lang)).classes("text-h6 flex-1")
                types = agg["doc_types"]
                if not types:
                    ui.label(t("dash.auto_class", lang)).classes("text-caption opacity-60")
                else:
                    max_v = max(c for _, c in types) or 1
                    for tname, count in types:
                        with ui.column().classes("w-full gap-0 q-mb-xs"):
                            with ui.row().classes("items-center w-full"):
                                ui.label(tname).classes("text-caption flex-1 ellipsis")
                                ui.label(str(count)).classes("text-caption opacity-80")
                            with ui.element("div").classes("ldi-progress").style("height: 4px;"):
                                ui.element("div").classes("ldi-progress-fill").style(
                                    f"width: {(count / max_v) * 100:.1f}%;"
                                )

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
        ui.label(t("sources.title", lang)).classes("text-h4 q-mb-md ldi-hero-text")

        from app.models import ScanJob, ScanJobStatus

        def _latest_job_for(session, source_id: int) -> ScanJob | None:
            return session.exec(
                select(ScanJob)
                .where(ScanJob.source_id == source_id)
                .order_by(ScanJob.id.desc())  # type: ignore
                .limit(1)
            ).first()

        def _status_pill(job: ScanJob | None):
            if job is None:
                return None
            status = job.status
            if status == ScanJobStatus.running:
                cls, label = "ldi-pill ldi-pill-success", "● running"
            elif status == ScanJobStatus.paused:
                cls, label = "ldi-pill ldi-pill-warning", "❚❚ paused"
            elif status == ScanJobStatus.queued:
                cls, label = "ldi-pill ldi-pill-warning", "queued"
            elif status == ScanJobStatus.completed:
                cls, label = "ldi-pill", "✓ completed"
            elif status == ScanJobStatus.error:
                cls, label = "ldi-pill ldi-pill-error", "✗ error"
            elif status == ScanJobStatus.aborted:
                cls, label = "ldi-pill", "⨯ aborted"
            else:
                cls, label = "ldi-pill", str(status)
            ui.label(label).classes(cls)

        def _progress_block(job: ScanJob) -> None:
            total = max(job.total_files or 0, job.processed_files)
            if total <= 0:
                pct = 0.0
            else:
                pct = min(100.0, 100.0 * (job.processed_files / total))
            with ui.column().classes("w-full gap-1 q-mt-sm"):
                with ui.element("div").classes("ldi-progress"):
                    if job.status == ScanJobStatus.running and total <= 0:
                        ui.element("div").classes("ldi-progress-fill indeterminate")
                    else:
                        ui.element("div").classes("ldi-progress-fill").style(f"width: {pct:.1f}%;")
                with ui.row().classes("items-center gap-3 text-caption opacity-80"):
                    if total > 0:
                        ui.label(f"{job.processed_files} / {total}")
                    else:
                        ui.label(f"{job.processed_files} files")
                    if job.error_count:
                        ui.label(f"· errors: {job.error_count}").classes("ldi-pill-error")
                    if job.current_file:
                        ui.label(f"· {job.current_file}").classes("ellipsis").style("max-width: 380px;")

        def _show_credentials_dialog(source_id: int, source_type: str, default_path: str) -> None:
            from app.utils.secret_store import get_secret, put_secret

            existing = get_secret(f"source-{source_id}") or {}
            with ui.dialog() as dialog, ui.card().classes("w-[460px] p-4"):
                ui.label(t("sources.creds_dialog_title", lang).format(type=source_type.upper())).classes(
                    "text-h6 ldi-primary"
                )
                ui.label(t("sources.creds_stored_help", lang)).classes("text-caption opacity-70 q-mb-md")
                fields: dict[str, Any] = {}
                if source_type == "webdav":
                    fields["base_url"] = ui.input(
                        t("sources.creds_base_url", lang), value=existing.get("base_url", "")
                    ).classes("w-full")
                elif source_type == "sftp":
                    fields["host"] = ui.input(
                        t("sources.creds_host", lang), value=existing.get("host", "")
                    ).classes("w-full")
                    fields["port"] = ui.number(
                        t("sources.creds_port", lang), value=existing.get("port", 22), min=1, max=65535
                    ).classes("w-full")
                    fields["private_key_path"] = ui.input(
                        t("sources.creds_private_key", lang),
                        value=existing.get("private_key_path", ""),
                    ).classes("w-full")
                elif source_type == "smb":
                    # Derive a default server from \\server\share if present
                    default_server = ""
                    if default_path.startswith("\\\\") or default_path.startswith("//"):
                        parts = default_path.lstrip("\\/").split("/")[0].split("\\")
                        default_server = parts[0] if parts else ""
                    fields["server"] = ui.input(
                        t("sources.creds_smb_server", lang),
                        value=existing.get("server", default_server),
                    ).classes("w-full")
                    fields["domain"] = ui.input(
                        t("sources.creds_smb_domain", lang), value=existing.get("domain", "")
                    ).classes("w-full")
                fields["username"] = ui.input(
                    t("common.username", lang), value=existing.get("username", "")
                ).classes("w-full")
                fields["password"] = ui.input(
                    t("common.password", lang),
                    value=existing.get("password", ""),
                    password=True,
                    password_toggle_button=True,
                ).classes("w-full")

                with ui.row().classes("justify-end gap-2 q-mt-md w-full"):
                    ui.button(t("common.cancel", lang), on_click=dialog.close).props("flat")

                    def _save() -> None:
                        payload = {
                            k: (v.value if hasattr(v, "value") else v)
                            for k, v in fields.items()
                            if (v.value if hasattr(v, "value") else v) not in ("", None)
                        }
                        ref = f"source-{source_id}"
                        put_secret(ref, payload)
                        with session_scope() as session:
                            src = session.get(DocumentSource, source_id)
                            if src:
                                src.credentials_ref = ref
                                session.add(src)
                        ui.notify(t("sources.creds_saved", lang), color="positive")
                        dialog.close()

                    ui.button(t("common.save", lang), on_click=_save).props("color=primary")
                dialog.open()

        # ----- Add-new card ---------------------------------------------
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
                        src = DocumentSource(
                            name=name.value,
                            type=SourceType(stype.value),
                            path=path.value,
                            owner_id=user.id,
                        )
                        session.add(src)
                        session.flush()
                        new_id = src.id
                        new_type = src.type.value
                        new_path = src.path
                    name.value = ""
                    path.value = ""
                    _refresh()
                    ui.notify(t("sources.added", lang))
                    # Prompt for credentials if remote
                    if new_type in ("smb", "webdav", "sftp") and new_id is not None:
                        _show_credentials_dialog(new_id, new_type, new_path)

                ui.button(t("common.add", lang), icon="add", on_click=_add).props("color=primary")

        # ----- Source list (live updating) ------------------------------
        table_container = ui.column().classes("w-full gap-2")

        def _refresh() -> None:
            from app.services.watcher import (
                is_watching as _is_watching,
            )
            from app.services.watcher import (
                start_watcher as _start_watch,
            )
            from app.services.watcher import (
                stop_watcher as _stop_watch,
            )

            table_container.clear()
            with table_container, session_scope() as session:
                rows = session.exec(select(DocumentSource).order_by(DocumentSource.id)).all()
                for s in rows:
                    job = _latest_job_for(session, s.id)
                    is_active = job is not None and job.status in (
                        ScanJobStatus.running,
                        ScanJobStatus.queued,
                        ScanJobStatus.paused,
                    )
                    with ui.card().classes("w-full p-3"):
                        # Top row: name / path / status
                        with ui.row().classes("justify-between items-start w-full gap-3 no-wrap"):
                            with ui.column().classes("gap-0 flex-1 min-w-0"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.label(s.name).classes("text-h6")
                                    ui.label(s.type.value.upper()).classes("ldi-pill")
                                    _status_pill(job)
                                    if s.credentials_ref:
                                        ui.label("🔒 creds").classes("ldi-pill")
                                ui.label(s.path).classes("ldi-muted text-caption ellipsis")
                                if s.last_scan_at:
                                    ui.label(
                                        f"{t('sources.last_scan', lang)} {s.last_scan_at:%Y-%m-%d %H:%M}"
                                    ).classes("text-caption opacity-60")
                            with ui.row().classes("gap-1 flex-shrink-0"):

                                def _start(
                                    sid: int,
                                    phase: str,
                                    note_key: str,
                                ) -> None:
                                    start_scan_in_background(sid, phase=phase)
                                    ui.notify(t(note_key, lang))
                                    _refresh()

                                with (
                                    ui.button(icon="play_arrow")
                                    .props("color=primary dense round")
                                    .tooltip(t("sources.scan", lang))
                                ):
                                    with ui.menu():
                                        ui.menu_item(
                                            t("sources.phase_quick", lang),
                                            on_click=lambda sid=s.id: _start(
                                                sid, "quick", "sources.scan_started"
                                            ),
                                        )
                                        ui.menu_item(
                                            t("sources.phase_text", lang),
                                            on_click=lambda sid=s.id: _start(
                                                sid, "text", "sources.scan_started"
                                            ),
                                        )
                                        ui.menu_item(
                                            t("sources.phase_ocr", lang),
                                            on_click=lambda sid=s.id: _start(
                                                sid, "ocr", "sources.ocr_started"
                                            ),
                                        )
                                        ui.menu_item(
                                            t("sources.phase_vision", lang),
                                            on_click=lambda sid=s.id: _start(
                                                sid, "vision", "sources.vision_started"
                                            ),
                                        )
                                        ui.separator()
                                        ui.menu_item(
                                            t("sources.phase_full", lang),
                                            on_click=lambda sid=s.id: _start(
                                                sid, "full", "sources.scan_started"
                                            ),
                                        )
                                ui.button(
                                    icon="bolt",
                                    on_click=lambda sid=s.id: _start(sid, "quick", "sources.scan_started"),
                                ).props("flat dense round").tooltip(t("sources.phase_quick", lang))
                                ui.button(
                                    icon="text_fields",
                                    on_click=lambda sid=s.id: _start(sid, "ocr", "sources.ocr_started"),
                                ).props("flat dense round").tooltip(t("sources.force_ocr", lang))
                                ui.button(
                                    icon="image",
                                    on_click=lambda sid=s.id: _start(sid, "vision", "sources.vision_started"),
                                ).props("flat dense round").tooltip(t("sources.vision", lang))
                                ui.button(
                                    icon="science",
                                    on_click=lambda sid=s.id: (
                                        start_scan_in_background(sid, dry_run=True),
                                        ui.notify(t("sources.dryrun_started", lang)),
                                        _refresh(),
                                    ),
                                ).props("flat dense round").tooltip(t("sources.dry_run", lang))
                                if s.type.value in ("smb", "webdav", "sftp"):
                                    ui.button(
                                        icon="vpn_key",
                                        on_click=lambda sid=s.id, sty=s.type.value, sp=s.path: _show_credentials_dialog(
                                            sid, sty, sp
                                        ),
                                    ).props("flat dense round").tooltip("Credentials")
                                watching = _is_watching(s.id)
                                ui.button(
                                    icon="visibility_off" if watching else "visibility",
                                    on_click=lambda sid=s.id: (
                                        (_stop_watch(sid) if _is_watching(sid) else _start_watch(sid)),
                                        _refresh(),
                                    ),
                                ).props("flat dense round").tooltip(
                                    t("sources.unwatch" if watching else "sources.watch", lang)
                                )
                                ui.button(
                                    icon="delete",
                                    on_click=lambda sid=s.id: _delete_source(sid),
                                ).props("flat dense round color=negative").tooltip(t("sources.delete", lang))

                        if is_active and job is not None:
                            _progress_block(job)
                        elif job is not None and job.message and job.status == ScanJobStatus.error:
                            with ui.row().classes("items-center gap-2 q-mt-sm w-full"):
                                ui.label(job.message).classes("ldi-pill ldi-pill-error flex-1").style(
                                    "white-space: normal; word-break: break-word;"
                                )
                                if (
                                    "embedding" in (job.message or "").lower()
                                    or "lm studio" in (job.message or "").lower()
                                    or "preflight" in (job.message or "").lower()
                                ):
                                    ui.button(
                                        "Open Settings",
                                        icon="settings",
                                        on_click=lambda: ui.navigate.to("/settings"),
                                    ).props("dense flat")

        def _delete_source(sid: int) -> None:
            with ui.dialog() as d, ui.card().classes("w-[420px] p-4"):
                ui.label(t("sources.delete_confirm", lang)).classes("text-h6 ldi-primary")
                ui.label(t("sources.delete_help", lang)).classes("text-caption opacity-70")

                def _do() -> None:
                    with session_scope() as session:
                        src = session.get(DocumentSource, sid)
                        if src:
                            session.delete(src)
                    d.close()
                    _refresh()

                with ui.row().classes("justify-end gap-2 w-full q-mt-md"):
                    ui.button(t("common.cancel", lang), on_click=d.close).props("flat")
                    ui.button(t("common.delete", lang), on_click=_do).props("color=negative")
            d.open()

        with ui.row().classes("items-center gap-2 q-mb-md"):
            ui.button(t("common.refresh", lang), icon="refresh", on_click=_refresh).props("flat dense")
            ui.label("Auto-refresh: 3 s").classes("text-caption opacity-60")

        _refresh()
        # Live progress poll — only refreshes the table area, doesn't reload the page.
        ui.timer(3.0, _refresh)

    @ui.page("/documents")
    def page_documents() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/documents")
        lang = _user_lang(user)
        ui.label(t("docs.title", lang)).classes("text-h4 q-mb-md ldi-hero-text")

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

        # --- Bulk selection state ---
        selection: set[int] = set()
        bulk_bar = (
            ui.row()
            .classes("ldi-glass items-center gap-2 q-pa-sm w-full no-wrap q-mb-md")
            .style("display: none;")
        )

        def _update_bulk_bar() -> None:
            bulk_bar.clear()
            bulk_bar.style("display: none;" if not selection else "display: flex;")
            if not selection:
                return
            with bulk_bar:
                ui.label(t("docs.bulk_selected", lang).format(n=len(selection))).classes(
                    "text-body2 ldi-primary"
                )
                ui.space()

                async def _bulk_tag() -> None:
                    from app.models import DocumentTagLink, Tag

                    with ui.dialog() as d, ui.card().classes("w-[420px] p-4"):
                        ui.label(t("docs.bulk_add_tag_title", lang).format(n=len(selection))).classes(
                            "text-h6 ldi-primary"
                        )
                        name_in = ui.input(t("docs.bulk_tag_name_input", lang)).classes("w-full")

                        def _apply() -> None:
                            tname = (name_in.value or "").strip()
                            if not tname:
                                ui.notify(t("docs.bulk_tag_name_required", lang), color="warning")
                                return
                            with session_scope() as sess:
                                tag = sess.exec(select(Tag).where(Tag.name == tname)).first()
                                if not tag:
                                    tag = Tag(name=tname, auto=False)
                                    sess.add(tag)
                                    sess.flush()
                                for did in selection:
                                    exists = sess.exec(
                                        select(DocumentTagLink).where(
                                            DocumentTagLink.document_id == did,
                                            DocumentTagLink.tag_id == tag.id,
                                        )
                                    ).first()
                                    if not exists:
                                        sess.add(
                                            DocumentTagLink(
                                                document_id=did,
                                                tag_id=tag.id,
                                                auto=False,
                                            )
                                        )
                            ui.notify(
                                t("docs.bulk_tag_added", lang).format(name=tname, n=len(selection)),
                                color="positive",
                            )
                            d.close()
                            selection.clear()
                            _refresh()

                        with ui.row().classes("justify-end gap-2 w-full q-mt-md"):
                            ui.button(t("common.cancel", lang), on_click=d.close).props("flat")
                            ui.button(t("common.apply", lang), on_click=_apply).props("color=primary")
                    d.open()

                async def _bulk_reindex() -> None:
                    from app.services.indexer import index_document

                    with session_scope() as sess:
                        docs = sess.exec(
                            select(Document).where(Document.id.in_(list(selection)))  # type: ignore[attr-defined]
                        ).all()
                        source_ids = {d.source_id for d in docs}
                        sources_by_id = {
                            sr.id: sr
                            for sr in sess.exec(
                                select(DocumentSource).where(
                                    DocumentSource.id.in_(list(source_ids))  # type: ignore[attr-defined]
                                )
                            ).all()
                        }
                        doc_data = [(d.id, d.path, sources_by_id.get(d.source_id)) for d in docs]
                    ui.notify(t("docs.bulk_reindexing", lang).format(n=len(doc_data)), color="positive")

                    import asyncio as _aio

                    async def _runner():
                        from pathlib import Path as _P

                        for did, dpath, src in doc_data:
                            if not src:
                                continue
                            snap = DocumentSource(**src.model_dump())
                            try:
                                await index_document(snap, _P(dpath), force_ocr=False, force_embed=True)
                            except Exception as e:
                                logger.warning("bulk re-index failed for {}: {}", dpath, e)

                    _aio.create_task(_runner())
                    selection.clear()
                    _refresh()

                async def _bulk_delete() -> None:
                    from app.models import DocumentChunk, DocumentImage, DocumentPage, DocumentTagLink
                    from app.vectorstore import delete_for_document

                    with ui.dialog() as d, ui.card().classes("w-[420px] p-4"):
                        ui.label(t("docs.bulk_delete_confirm", lang).format(n=len(selection))).classes(
                            "text-h6 ldi-primary"
                        )
                        ui.label(t("docs.bulk_delete_help", lang)).classes("text-caption opacity-70")

                        def _do_delete() -> None:
                            with session_scope() as sess:
                                for did in list(selection):
                                    for table in (
                                        DocumentChunk,
                                        DocumentPage,
                                        DocumentImage,
                                        DocumentTagLink,
                                    ):
                                        for row in sess.exec(
                                            select(table).where(table.document_id == did)  # type: ignore[arg-type]
                                        ).all():
                                            sess.delete(row)
                                    try:
                                        delete_for_document(did)
                                    except Exception:
                                        pass
                                    doc_obj = sess.get(Document, did)
                                    if doc_obj:
                                        sess.delete(doc_obj)
                            ui.notify(
                                t("docs.bulk_deleted", lang).format(n=len(selection)),
                                color="positive",
                            )
                            d.close()
                            selection.clear()
                            _refresh()

                        with ui.row().classes("justify-end gap-2 w-full q-mt-md"):
                            ui.button(t("common.cancel", lang), on_click=d.close).props("flat")
                            ui.button(t("common.delete", lang), on_click=_do_delete).props("color=negative")
                    d.open()

                ui.button(t("docs.bulk_add_tag_button", lang), icon="label", on_click=_bulk_tag).props(
                    "dense"
                )
                ui.button(t("docs.bulk_reindex_button", lang), icon="refresh", on_click=_bulk_reindex).props(
                    "dense"
                )
                ui.button(t("common.delete", lang), icon="delete", on_click=_bulk_delete).props(
                    "dense color=negative"
                )
                ui.button(
                    t("common.clear", lang),
                    icon="close",
                    on_click=lambda: (selection.clear(), _refresh()),
                ).props("flat dense")

        def _refresh() -> None:
            _update_bulk_bar()
            results.clear()
            with results, session_scope() as session:
                stmt = select(Document)
                if q_input.value:
                    like = f"%{q_input.value}%"
                    stmt = stmt.where(Document.filename.like(like))  # type: ignore
                stmt = stmt.order_by(Document.id.desc()).limit(200)  # type: ignore
                for d in session.exec(stmt).all():
                    with ui.card().classes("w-full p-3"):
                        with ui.row().classes("items-start gap-3 w-full no-wrap"):
                            cb = ui.checkbox(value=d.id in selection)

                            def _toggle(e, _did=d.id) -> None:
                                if e.value:
                                    selection.add(_did)
                                else:
                                    selection.discard(_did)
                                _update_bulk_bar()

                            cb.on("update:model-value", _toggle)
                            # Page-1 thumbnail — browser-lazy so we don't
                            # blast the server with N parallel renders.
                            thumb_url = f"/api/documents/{d.id}/page/1/image"
                            ui.html(f"""
<div class="ldi-glass-sm" style="width:120px; min-width:120px; height:150px;
   overflow:hidden; display:flex; align-items:center; justify-content:center;
   cursor:pointer;" onclick="window.location.href='/viewer?doc={d.id}&amp;page=1'">
  <img src="{thumb_url}" loading="lazy" referrerpolicy="no-referrer"
       style="max-width:100%; max-height:100%; object-fit:contain;"
       onerror="this.style.display='none';this.parentElement.innerHTML=
       '<div style=&quot;opacity:0.4;font-size:11px;text-align:center&quot;>no preview</div>'"/>
</div>
""")
                            with ui.column().classes("flex-1 gap-0 min-w-0"):
                                ui.label(d.filename).classes("text-h6")
                                ui.label(d.path).classes("text-caption opacity-70 break-all")
                                ui.label(
                                    f"{t('docs.pages', lang)}: {d.page_count} · "
                                    f"{t('docs.status', lang)}: {d.status.value} · "
                                    f"{t('docs.type', lang)}: {d.doc_type or '—'} · "
                                    f"{t('docs.lang', lang)}: {d.language or '—'}"
                                ).classes("text-caption")
                                with ui.row().classes("gap-1 q-mt-xs flex-wrap"):
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

                                    async def _reocr(did=d.id, dpath=d.path, src_id=d.source_id) -> None:
                                        from pathlib import Path as _P

                                        from app.services.indexer import index_document

                                        with session_scope() as sess:
                                            src = sess.get(DocumentSource, src_id)
                                            if src is None:
                                                ui.notify("Source missing", color="negative")
                                                return
                                            snap = DocumentSource(**src.model_dump())
                                        ui.notify(
                                            f"Re-OCR queued for {_P(dpath).name}",
                                            color="positive",
                                        )
                                        import asyncio as _aio

                                        _aio.create_task(index_document(snap, _P(dpath), force_ocr=True))

                                    ui.button(
                                        t("docs.bulk_reocr_button", lang),
                                        icon="text_fields",
                                        on_click=_reocr,
                                    ).props("dense flat")

        with ui.row().classes("items-center gap-2"):
            q_input.on("change", lambda _: _refresh())
            ui.button(t("common.refresh", lang), icon="refresh", on_click=_refresh).props("dense")

            def _select_all() -> None:
                with session_scope() as sess:
                    docs = sess.exec(select(Document).limit(200)).all()
                    for d in docs:
                        if d.id is not None:
                            selection.add(d.id)
                _refresh()

            ui.button(
                t("docs.select_all_button", lang).format(n=200),
                icon="select_all",
                on_click=_select_all,
            ).props("dense flat")

        _refresh()

    @ui.page("/search")
    def page_search(tag: str = "", q: str = "") -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/search")
        lang = _user_lang(user)
        ui.label(t("search.title", lang)).classes("text-h4 q-mb-md ldi-hero-text")

        # Deep-link params: ?tag=X (browse a tag, e.g. from the Tags page) and
        # ?q=… (pre-filled query). Captured before the input element shadows `q`.
        initial_tag = (tag or "").strip()
        initial_query = (q or "").strip()

        # Search bar with embedded icon-button
        with ui.row().classes("w-full gap-2 items-center no-wrap"):
            q = ui.input(t("search.placeholder", lang), value=initial_query).classes("flex-1")
            q.props("autofocus dense outlined")
            rerank_toggle = ui.checkbox(t("search.rerank", lang), value=False)

        # Filter state — read by _go() before each search
        filter_state: dict[str, list] = {"source_ids": [], "tags": [], "doc_types": []}
        if initial_tag:
            filter_state["tags"].append(initial_tag)

        # ----- Saved searches row -----
        from app.models import SavedSearch

        saved_row = ui.row().classes("items-center gap-2 q-mt-xs flex-wrap")

        def _refresh_saved() -> None:
            saved_row.clear()
            with saved_row, session_scope() as session:
                rows = session.exec(
                    select(SavedSearch)
                    .where(SavedSearch.user_id == user.id)
                    .order_by(SavedSearch.last_used_at.desc().nullslast(), SavedSearch.id.desc())  # type: ignore[attr-defined]
                    .limit(12)
                ).all()
                if not rows:
                    ui.label(t("search.no_saved", lang)).classes("text-caption opacity-60")
                    return
                ui.label(t("search.saved_label", lang)).classes("text-caption opacity-60")
                for s in rows:

                    def _load(
                        sid=s.id,
                        name=s.name,
                        query=s.query,
                        src=tuple(s.source_ids),
                        tg=tuple(s.tags),
                        dt=tuple(s.doc_types),
                        rr=s.rerank,
                    ) -> None:
                        q.value = query
                        rerank_toggle.value = rr
                        filter_state["source_ids"] = list(src)
                        filter_state["tags"] = list(tg)
                        filter_state["doc_types"] = list(dt)
                        # Mark as used
                        with session_scope() as sess:
                            row = sess.get(SavedSearch, sid)
                            if row:
                                from datetime import datetime as _dt

                                row.last_used_at = _dt.now(UTC)
                                row.use_count += 1
                                sess.add(row)
                        import asyncio as _aio

                        _aio.create_task(_go())

                    with ui.row().classes("items-center gap-0"):
                        ui.button(s.name, on_click=_load).props("flat dense no-caps").classes("ldi-pill")

                        def _delete_saved(sid=s.id) -> None:
                            with ui.dialog() as d, ui.card().classes("w-[420px] p-4"):
                                ui.label(t("search.delete_saved_confirm", lang)).classes(
                                    "text-h6 ldi-primary"
                                )

                                def _do() -> None:
                                    with session_scope() as sess:
                                        row = sess.get(SavedSearch, sid)
                                        if row and row.user_id == user.id:
                                            sess.delete(row)
                                    d.close()
                                    _refresh_saved()
                                    ui.notify(t("search.saved_removed", lang), color="positive")

                                with ui.row().classes("justify-end gap-2 w-full q-mt-md"):
                                    ui.button(t("common.cancel", lang), on_click=d.close).props("flat")
                                    ui.button(t("common.delete", lang), on_click=_do).props("color=negative")
                            d.open()

                        ui.button(icon="close", on_click=_delete_saved).props(
                            "flat dense round size=xs"
                        ).style("min-height: 0;")

        def _save_current() -> None:
            query = (q.value or "").strip()
            if not query:
                ui.notify(t("search.enter_query_first", lang), color="warning")
                return
            with ui.dialog() as d, ui.card().classes("w-[400px] p-4"):
                ui.label(t("search.save_dialog_title", lang)).classes("text-h6 ldi-primary")
                ui.label(f"Query: {query!r}").classes("text-caption opacity-70")
                if any(filter_state.values()):
                    parts = []
                    if filter_state["source_ids"]:
                        parts.append(f"{len(filter_state['source_ids'])} source(s)")
                    if filter_state["tags"]:
                        parts.append(f"{len(filter_state['tags'])} tag(s)")
                    if filter_state["doc_types"]:
                        parts.append(f"{len(filter_state['doc_types'])} type(s)")
                    ui.label("Filters: " + ", ".join(parts)).classes("text-caption opacity-70")
                name_in = ui.input("Name", value=query[:50]).classes("w-full q-mt-md")

                def _do_save() -> None:
                    nm = (name_in.value or "").strip() or query[:50]
                    with session_scope() as sess:
                        sess.add(
                            SavedSearch(
                                user_id=user.id,  # type: ignore[arg-type]
                                name=nm,
                                query=query,
                                source_ids=list(filter_state["source_ids"]),
                                tags=list(filter_state["tags"]),
                                doc_types=list(filter_state["doc_types"]),
                                rerank=bool(rerank_toggle.value),
                            )
                        )
                    ui.notify(t("search.saved_as", lang).format(name=nm), color="positive")
                    d.close()
                    _refresh_saved()

                with ui.row().classes("justify-end gap-2 w-full q-mt-md"):
                    ui.button("Cancel", on_click=d.close).props("flat")
                    ui.button("Save", on_click=_do_save).props("color=primary")
            d.open()

        from app.models import Tag

        with session_scope() as session:
            sources_for_filter = [
                (sr.id, sr.name)
                for sr in session.exec(select(DocumentSource).order_by(DocumentSource.name)).all()
            ]
            tags_for_filter = [t_.name for t_ in session.exec(select(Tag).order_by(Tag.name)).all()]
            doc_types_for_filter = sorted(
                {d.doc_type for d in session.exec(select(Document)).all() if d.doc_type}
            )

        result_summary = ui.label("").classes("text-caption opacity-70 q-mt-sm")

        # Layout: filter sidebar (left) + results (right)
        with ui.row().classes("w-full gap-3 no-wrap q-mt-sm items-start"):
            with ui.column().classes("ldi-glass gap-3 q-pa-md").style("min-width: 240px; max-width: 260px;"):
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.icon("filter_list").classes("ldi-accent")
                    ui.label(t("search.filters", lang)).classes("text-h6 flex-1")
                if sources_for_filter:
                    ui.label(t("search.sources", lang)).classes("text-caption opacity-60 q-mt-xs").style(
                        "letter-spacing: 0.12em;"
                    )
                    for sid, sname in sources_for_filter:

                        def _toggle_src(e, _sid=sid) -> None:
                            if e.value and _sid not in filter_state["source_ids"]:
                                filter_state["source_ids"].append(_sid)
                            elif not e.value and _sid in filter_state["source_ids"]:
                                filter_state["source_ids"].remove(_sid)

                        ui.checkbox(sname).classes("text-body2").on("update:model-value", _toggle_src)
                if doc_types_for_filter:
                    ui.label(t("search.doc_type", lang)).classes("text-caption opacity-60 q-mt-md").style(
                        "letter-spacing: 0.12em;"
                    )
                    for dt in doc_types_for_filter:

                        def _toggle_dt(e, _dt=dt) -> None:
                            if e.value and _dt not in filter_state["doc_types"]:
                                filter_state["doc_types"].append(_dt)
                            elif not e.value and _dt in filter_state["doc_types"]:
                                filter_state["doc_types"].remove(_dt)

                        ui.checkbox(dt).classes("text-body2").on("update:model-value", _toggle_dt)
                if tags_for_filter:
                    ui.label(t("search.tags", lang)).classes("text-caption opacity-60 q-mt-md").style(
                        "letter-spacing: 0.12em;"
                    )
                    tag_chip_row = ui.row().classes("gap-1 flex-wrap")
                    with tag_chip_row:
                        for tname in tags_for_filter[:30]:
                            active = {"v": tname in filter_state["tags"]}
                            chip = ui.button(tname).props("flat dense no-caps").classes("ldi-pill")

                            def _toggle_tag(_e=None, _tname=tname, _state=active, _chip=chip) -> None:
                                _state["v"] = not _state["v"]
                                if _state["v"]:
                                    if _tname not in filter_state["tags"]:
                                        filter_state["tags"].append(_tname)
                                    _chip.props("color=primary")
                                else:
                                    if _tname in filter_state["tags"]:
                                        filter_state["tags"].remove(_tname)
                                    _chip.props(remove="color")

                            chip.on("click", _toggle_tag)
                            # Reflect a pre-seeded (deep-linked ?tag=) selection.
                            if active["v"]:
                                chip.props("color=primary")
                ui.separator().classes("q-my-sm")
                ui.button(
                    t("search.clear_filters", lang),
                    icon="restart_alt",
                    on_click=lambda: ui.navigate.to("/search"),
                ).props("flat dense")

            out = ui.column().classes("flex-1 gap-2 min-w-0")

        import html as _html
        import re as _re

        def _highlight(snippet: str, query: str) -> str:
            """Wrap each query word in <mark> tags. Returns HTML-safe string."""
            safe = _html.escape(snippet or "")
            if not query.strip():
                return safe
            words = [w for w in _re.split(r"\s+", query.strip()) if len(w) >= 2]
            if not words:
                return safe
            try:
                pattern = _re.compile(
                    "(" + "|".join(_re.escape(w) for w in words) + ")",
                    flags=_re.IGNORECASE,
                )
                return pattern.sub(lambda m: f"<mark class='ldi-mark'>{m.group(0)}</mark>", safe)
            except _re.error:
                return safe

        def _source_pill(source: str) -> str:
            colour = {
                "native_text": "ldi-pill",
                "ocr_text": "ldi-pill ldi-pill-warning",
                "image_description": "ldi-pill ldi-pill-success",
                "table": "ldi-pill ldi-pill-success",
            }.get(source, "ldi-pill")
            return f'<span class="{colour}">{source}</span>'

        async def _compute_hits(top_k: int = 15) -> tuple[str, list, bool]:
            """Resolve hits honouring the live filters. Returns (query, hits, browse).

            With a query → hybrid search (+ client-side doc-type filter). With no
            query but active filters → metadata browse (tag/source/type). Shared by
            _go() and _export() so exports match what's on screen.
            """
            import asyncio as _aio

            from app.services.search_service import browse_documents, hybrid_search

            query = (q.value or "").strip()
            src = list(filter_state["source_ids"]) or None
            tg = list(filter_state["tags"]) or None
            dts = list(filter_state["doc_types"]) or None
            if query:
                hits = await hybrid_search(
                    query,
                    top_k=top_k,
                    rerank=rerank_toggle.value,
                    user=user,
                    source_ids=src,
                    tags=tg,
                )
                # Apply doc-type filter client-side (hybrid_search has no native one)
                if dts:
                    wanted = set(dts)
                    with session_scope() as session:
                        docs = session.exec(
                            select(Document).where(
                                Document.id.in_(list({h.document_id for h in hits}))  # type: ignore[attr-defined]
                            )
                        ).all()
                        by_id = {d.id: d for d in docs}
                    hits = [
                        h
                        for h in hits
                        if (by_id.get(h.document_id) and by_id[h.document_id].doc_type in wanted)
                    ]
                return query, hits, False
            if src or tg or dts:
                hits = await _aio.to_thread(
                    browse_documents,
                    user=user,
                    source_ids=src,
                    tags=tg,
                    doc_types=dts,
                    top_k=max(top_k, 50),
                )
                return "", hits, True
            return "", [], False

        async def _go() -> None:
            out.clear()
            result_summary.text = ""
            import time as _time

            t0 = _time.perf_counter()
            query, hits, browse = await _compute_hits()
            elapsed = _time.perf_counter() - t0
            if not query and not browse:
                return

            if browse:
                parts = []
                if filter_state["tags"]:
                    parts.append("tags: " + ", ".join(filter_state["tags"]))
                if filter_state["source_ids"]:
                    parts.append(f"{len(filter_state['source_ids'])} source(s)")
                if filter_state["doc_types"]:
                    parts.append(", ".join(filter_state["doc_types"]))
                result_summary.text = t("search.browse_count", lang).format(n=len(hits)) + (
                    " — " + " · ".join(parts) if parts else ""
                )
            else:
                result_summary.text = (
                    t("search.result_count", lang).format(n=len(hits), q=repr(query))
                    + (" " + t("search.reranked", lang) if rerank_toggle.value else "")
                    + f" · {elapsed * 1000:.0f} ms"
                )
            with out:
                if not hits:
                    with ui.card().classes("w-full p-4"):
                        ui.icon("search_off").classes("text-4xl opacity-30")
                        ui.label(t("search.no_results", lang)).classes("opacity-70")
                        ui.label(t("search.no_results_hint", lang)).classes("text-caption opacity-60")
                    return
                for rank, h in enumerate(hits, start=1):
                    score_pct = max(0.0, min(1.0, float(h.score))) * 100
                    with ui.card().classes("w-full p-3"):
                        # Header row
                        with ui.row().classes("items-center gap-2 w-full no-wrap"):
                            ui.label(f"#{rank}").classes("ldi-pill").style(
                                "min-width: 38px; justify-content: center;"
                            )
                            with ui.column().classes("flex-1 gap-0 min-w-0"):
                                ui.label(h.filename).classes("text-body1").style("font-weight: 600;")
                                ui.label(h.path).classes("text-caption opacity-60 ellipsis")
                            ui.label(f"p.{h.page_from}").classes("ldi-pill")
                            ui.html(_source_pill(h.source)).style("flex-shrink: 0;")
                        # Snippet with highlighted matches
                        ui.html(f"<div class='ldi-snippet'>{_highlight(h.snippet, query)}</div>").classes(
                            "q-mt-sm"
                        )
                        # Footer: score bar + actions
                        with ui.row().classes("items-center gap-3 q-mt-sm w-full no-wrap"):
                            with ui.column().classes("gap-0").style("min-width: 130px;"):
                                ui.label(f"{t('search.score', lang)} {h.score:.3f}").classes(
                                    "text-caption opacity-70"
                                )
                                with ui.element("div").classes("ldi-progress").style("height: 4px;"):
                                    ui.element("div").classes("ldi-progress-fill").style(
                                        f"width: {score_pct:.1f}%;"
                                    )
                            ui.space()
                            ui.button(
                                t("search.btn_view", lang),
                                icon="visibility",
                                on_click=lambda did=h.document_id, pg=h.page_from, qv=query: ui.navigate.to(
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
            ui.button(t("search.save", lang), icon="bookmark_add", on_click=_save_current).props("dense")

            async def _export(fmt: str) -> None:
                from app.services.exports import search_hits_to_csv, search_hits_to_json

                # Honour the same query + filters + rerank as the on-screen results.
                _query, hits, _browse = await _compute_hits(top_k=100)
                if not hits:
                    ui.notify(t("search.nothing_to_export", lang), color="warning")
                    return
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
        _refresh_saved()
        # Auto-run when deep-linked with a tag or query (e.g. a Tags-page chip).
        if initial_tag or initial_query:
            ui.timer(0.1, _go, once=True)

    @ui.page("/chat")
    def page_chat() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/chat")
        lang = _user_lang(user)

        chat_state: dict = {"chat_id": None}
        user_initials = (user.username[:1] + (user.username[1:2] if len(user.username) > 1 else "")).upper()

        # --- Helpers ----------------------------------------------------
        def _fmt_when(ts) -> str:
            if not ts:
                return ""
            try:
                from datetime import datetime

                import humanize

                now = datetime.now(UTC)
                delta = now - (ts if ts.tzinfo else ts.replace(tzinfo=UTC))
                return humanize.naturaltime(delta)
            except Exception:
                return ts.strftime("%H:%M") if hasattr(ts, "strftime") else ""

        def _scroll_msgs_to_bottom() -> None:
            # NiceGUI doesn't expose a direct scroll API on a column, so we
            # use a small JS shim that targets our msg-container.
            ui.run_javascript(
                "(()=>{const el=document.getElementById('ldi-msgs');"
                "if(el){el.scrollTop=el.scrollHeight;}})()"
            )

        def _render_user_message(content: str, when: str | None = None) -> None:
            import html

            safe = html.escape(content).replace("\n", "<br>")
            with ui.row().classes("w-full no-wrap q-mb-md justify-end items-end gap-2"):
                with ui.column().classes("gap-0 items-end").style("max-width: 78%"):
                    ui.html(safe).classes("ldi-chat-bubble-user")
                    if when:
                        ui.label(when).classes("text-caption opacity-60 q-mt-xs")
                with ui.element("div").classes("ldi-avatar ldi-avatar-user").style("margin-bottom: 18px;"):
                    ui.label(user_initials)

        def _render_assistant_message_open() -> tuple:
            """Render the assistant bubble shell and return (md_el, bubble_card,
            footer_row). Used by both replay and streaming."""
            with ui.row().classes("w-full no-wrap q-mb-md items-end gap-2"):
                with ui.element("div").classes("ldi-avatar ldi-avatar-bot").style("margin-bottom: 18px;"):
                    ui.icon("auto_awesome")
                bubble = ui.column().classes("gap-1").style("max-width: 92%; min-width: 0;")
                with bubble:
                    md_card = ui.element("div").classes("ldi-chat-bubble-assistant")
                    with md_card:
                        md_el = ui.markdown("")
                    footer = ui.row().classes("items-center gap-2 q-mt-xs")
            return md_el, md_card, footer

        def _link_citations(text: str, sources: list[dict]) -> str:
            """Replace bracketed citation tokens like ``[1]`` with markdown
            links to the viewer page for the cited document."""
            if not sources or not text:
                return text
            result = text
            for s in sources:
                n = s.get("n")
                did = s.get("document_id")
                pg = s.get("page_from") or 1
                if n is None or did is None:
                    continue
                token = f"[{n}]"
                # Wrap the brackets in a styled markdown link
                replacement = f"[**\\[{n}\\]**](/viewer?doc={did}&page={pg})"
                # Replace, but only when not already linked (avoid double-wrap
                # if the same N appears twice in the answer).
                result = result.replace(token, replacement)
            return result

        def _render_sources_footer(footer_row, sources: list[dict]) -> None:
            """Horizontal scroller of source cards beneath an assistant bubble.
            Each card shows index, filename, page and a short snippet, with
            View/PDF actions on hover."""
            with footer_row:
                with (
                    ui.expansion(
                        f"{t('chat.sources', lang)} · {len(sources)}",
                        icon="link",
                    )
                    .props("dense")
                    .classes("ldi-glass-sm")
                    .style("padding: 4px 10px;")
                ):
                    # Horizontal carousel — wraps to multiline only on small
                    # screens; otherwise pure side-scroll keeps the answer
                    # area uncluttered when there are many sources.
                    with (
                        ui.row().classes("gap-2 no-wrap").style("overflow-x: auto; padding: 4px 2px 6px 2px;")
                    ):
                        for s in sources:
                            with (
                                ui.card()
                                .classes("ldi-source-card q-pa-sm")
                                .style("min-width: 240px; max-width: 280px; " "flex: 0 0 auto;")
                            ):
                                with ui.row().classes("items-center gap-2 w-full"):
                                    ui.label(f"[{s.get('n')}]").classes("ldi-pill text-caption")
                                    ui.label(f"p.{s.get('page_from')}").classes("ldi-pill text-caption")
                                    ui.space()
                                    ui.button(
                                        icon="visibility",
                                        on_click=lambda d=s.get("document_id"), p=s.get(
                                            "page_from"
                                        ): ui.navigate.to(f"/viewer?doc={d}&page={p}"),
                                    ).props("flat dense round size=sm").tooltip("View")
                                    ui.button(
                                        icon="picture_as_pdf",
                                        on_click=lambda d=s.get("document_id"), p=s.get(
                                            "page_from"
                                        ): open_pdf(d, p),
                                    ).props("flat dense round size=sm").tooltip("PDF")
                                ui.label(s.get("filename") or "").classes("text-body2 ellipsis").style(
                                    "font-weight: 500;"
                                )
                                ui.label(s.get("snippet") or "").classes("text-caption opacity-70").style(
                                    "display: -webkit-box; "
                                    "-webkit-line-clamp: 3; "
                                    "-webkit-box-orient: vertical; "
                                    "overflow: hidden;"
                                )

        # --- Layout: two-column grid -----------------------------------
        with (
            ui.row()
            .classes("w-full gap-4 no-wrap")
            .style("height: calc(100vh - 120px); align-items: stretch;")
        ):
            # ============ Left: chat sidebar ============
            with ui.column().classes("ldi-glass gap-2 q-pa-md").style("min-width: 280px; max-width: 300px;"):
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label(t("chat.list_title", lang)).classes("text-h6")
                    ui.button(icon="add", on_click=lambda: _new_chat()).props(
                        "color=primary dense round"
                    ).tooltip(t("chat.new", lang))

                # Export-Buttons
                with ui.row().classes("gap-1 w-full"):

                    def _export_chat_md() -> None:
                        cid = chat_state.get("chat_id")
                        if not cid:
                            ui.notify(t("chat.open_first", lang), color="warning")
                            return
                        from app.services.exports import chat_to_markdown

                        md = chat_to_markdown(cid)
                        ui.download(
                            md.encode("utf-8"),
                            filename=f"chat-{cid}.md",
                            media_type="text/markdown",
                        )

                    def _export_chat_pdf() -> None:
                        cid = chat_state.get("chat_id")
                        if not cid:
                            ui.notify(t("chat.open_first", lang), color="warning")
                            return
                        from app.services.exports import chat_to_pdf

                        ui.download(
                            chat_to_pdf(cid),
                            filename=f"chat-{cid}.pdf",
                            media_type="application/pdf",
                        )

                    ui.button(t("chat.btn_md", lang), icon="download", on_click=_export_chat_md).props(
                        "flat dense"
                    ).classes("flex-1")
                    ui.button(
                        t("chat.btn_pdf", lang),
                        icon="picture_as_pdf",
                        on_click=_export_chat_pdf,
                    ).props("flat dense").classes("flex-1")

                ui.separator()

                chat_list = ui.column().classes("gap-1 w-full overflow-auto").style("flex: 1; min-height: 0;")

                def _refresh_chats() -> None:
                    chat_list.clear()
                    with chat_list, session_scope() as session:
                        chats = session.exec(
                            select(Chat)
                            .where(Chat.user_id == user.id)
                            .order_by(Chat.updated_at.desc())  # type: ignore
                        ).all()
                        if not chats:
                            ui.label(t("chat.start_or_pick", lang)).classes("text-caption opacity-60 q-px-sm")
                            return
                        active_id = chat_state.get("chat_id")
                        for c in chats:
                            cls = "ldi-nav-item"
                            if c.id == active_id:
                                cls += " active"
                            with ui.row().classes("items-center gap-1 w-full no-wrap"):
                                with (
                                    ui.button(on_click=lambda cid=c.id: _open_chat(cid))
                                    .props("flat align=left no-caps")
                                    .classes(cls + " flex-1")
                                ):
                                    with ui.row().classes("items-center gap-2 w-full no-wrap"):
                                        ui.icon("chat_bubble_outline").classes("text-base opacity-70")
                                        with ui.column().classes("gap-0 flex-1 items-start"):
                                            ui.label((c.title or "Untitled")[:30]).classes(
                                                "text-body2 text-left"
                                            )
                                            ui.label(_fmt_when(c.updated_at)).classes(
                                                "text-caption opacity-50"
                                            )
                                ui.button(
                                    icon="delete_outline",
                                    on_click=lambda cid=c.id: _delete_chat(cid),
                                ).props("flat dense round").classes("opacity-70")

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
                    _refresh_chats()
                    _refresh_msgs()

                def _delete_chat(cid: int) -> None:
                    with ui.dialog() as d, ui.card().classes("w-[420px] p-4"):
                        ui.label(t("chat.delete_confirm", lang)).classes("text-h6 ldi-primary")

                        def _do() -> None:
                            with session_scope() as session:
                                for m in session.exec(
                                    select(ChatMessage).where(ChatMessage.chat_id == cid)
                                ).all():
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
                            d.close()
                            _refresh_chats()
                            _refresh_msgs()

                        with ui.row().classes("justify-end gap-2 w-full q-mt-md"):
                            ui.button(t("common.cancel", lang), on_click=d.close).props("flat")
                            ui.button(t("common.delete", lang), on_click=_do).props("color=negative")
                    d.open()

            # ============ Right: chat conversation ============
            with ui.column().classes("flex-1 gap-2").style("min-width: 0;"):
                # Chat header
                chat_header = (
                    ui.row()
                    .classes("ldi-glass items-center gap-3 q-px-md q-py-sm w-full no-wrap")
                    .style("min-height: 52px;")
                )
                with chat_header:
                    chat_header_title = ui.label(t("chat.title", lang)).classes(
                        "text-h6 ldi-primary flex-1 ellipsis"
                    )

                    def _show_context_dialog() -> None:
                        """Pick sources / tags to constrain RAG retrieval for
                        the active chat."""
                        cid = chat_state.get("chat_id")
                        if not cid:
                            ui.notify(t("chat.open_first", lang), color="warning")
                            return
                        from app.models import Tag

                        with session_scope() as session:
                            all_sources = session.exec(
                                select(DocumentSource).order_by(DocumentSource.name)
                            ).all()
                            all_tags = session.exec(select(Tag).order_by(Tag.name)).all()
                            existing = session.exec(
                                select(ChatContextItem).where(ChatContextItem.chat_id == cid)
                            ).all()
                            existing_src = {it.ref_id for it in existing if it.kind == "source"}
                            existing_tag = {it.value for it in existing if it.kind == "tag" and it.value}

                        with ui.dialog() as dialog, ui.card().classes("w-[520px] p-4"):
                            ui.label(t("chat.restrict_to", lang)).classes("text-h6 ldi-primary")
                            ui.label(t("chat.restrict_help", lang)).classes("text-caption opacity-70 q-mb-md")
                            ui.label(t("chat.flt_sources", lang)).classes("text-caption opacity-60").style(
                                "letter-spacing: 0.12em;"
                            )
                            src_checks: dict[int, Any] = {}
                            for sr in all_sources:
                                src_checks[sr.id] = ui.checkbox(f"{sr.name}", value=sr.id in existing_src)
                            ui.label(t("chat.flt_tags", lang)).classes(
                                "text-caption opacity-60 q-mt-md"
                            ).style("letter-spacing: 0.12em;")
                            tag_checks: dict[str, Any] = {}
                            with ui.row().classes("flex-wrap gap-1"):
                                for tag in all_tags[:50]:
                                    tag_checks[tag.name] = ui.checkbox(
                                        tag.name, value=tag.name in existing_tag
                                    ).classes("text-caption")

                            def _apply() -> None:
                                with session_scope() as session:
                                    for old in session.exec(
                                        select(ChatContextItem).where(ChatContextItem.chat_id == cid)
                                    ).all():
                                        session.delete(old)
                                    for sid, cb in src_checks.items():
                                        if cb.value:
                                            session.add(
                                                ChatContextItem(
                                                    chat_id=cid,
                                                    kind="source",
                                                    ref_id=sid,
                                                )
                                            )
                                    for tname, cb in tag_checks.items():
                                        if cb.value:
                                            session.add(
                                                ChatContextItem(
                                                    chat_id=cid,
                                                    kind="tag",
                                                    value=tname,
                                                )
                                            )
                                ui.notify(t("chat.context_updated", lang), color="positive")
                                dialog.close()

                            with ui.row().classes("justify-end w-full gap-2 q-mt-md"):
                                ui.button(t("common.cancel", lang), on_click=dialog.close).props("flat")
                                ui.button(t("common.apply", lang), on_click=_apply).props("color=primary")
                        dialog.open()

                    ui.button(icon="filter_alt", on_click=_show_context_dialog).props(
                        "flat round dense"
                    ).tooltip(t("chat.restrict_context", lang))
                    ui.icon("auto_awesome").classes("ldi-accent text-xl")

                # Message scroll area
                msg_area = (
                    ui.column().classes("w-full q-pa-md overflow-auto").style("flex: 1; min-height: 0;")
                )
                msg_area.props('id="ldi-msgs"')

                # Input dock
                with (
                    ui.row()
                    .classes("ldi-glass items-end gap-2 q-pa-sm w-full no-wrap")
                    .style("border-radius: 18px;")
                ):
                    inp = (
                        ui.textarea(placeholder=t("chat.ph_ask", lang))
                        .props("borderless dense autogrow rows=1 max-rows=6")
                        .classes("flex-1")
                        .style("font-size: 15px;")
                    )

                    sending_state = {"busy": False}

                    async def _send() -> None:
                        if sending_state["busy"]:
                            return
                        question = (inp.value or "").strip()
                        if not question:
                            return
                        sending_state["busy"] = True
                        try:
                            if chat_state.get("chat_id") is None:
                                _new_chat()
                            inp.value = ""
                            with msg_area:
                                _render_user_message(question, when=_fmt_when(_now_utc()))
                                md_el, md_card, footer = _render_assistant_message_open()
                                md_card.classes("ldi-stream-cursor")
                            _scroll_msgs_to_bottom()

                            from app.chat.rag import stream_answer

                            cites_state: list[dict] = []
                            buffer: list[str] = []
                            async for ev in stream_answer(
                                chat_id=chat_state["chat_id"],
                                user=user,
                                question=question,
                            ):
                                ev_t = ev.get("type")
                                if ev_t == "sources":
                                    cites_state = ev.get("citations", []) or []
                                elif ev_t == "token":
                                    buffer.append(ev.get("text", ""))
                                    md_el.content = "".join(buffer)
                                    _scroll_msgs_to_bottom()
                                elif ev_t == "done":
                                    # remove streaming cursor & wire up sources
                                    md_card.classes(remove="ldi-stream-cursor")
                                    if cites_state:
                                        # Make [1], [2], … in the answer clickable links
                                        md_el.content = _link_citations("".join(buffer), cites_state)
                                        _render_sources_footer(footer, cites_state)
                                    _refresh_chats()  # update timestamp on sidebar
                                    _scroll_msgs_to_bottom()
                        finally:
                            sending_state["busy"] = False

                    ui.button(icon="send", on_click=_send).props("color=primary round dense").style(
                        "align-self: flex-end; margin-bottom: 4px;"
                    )
                    inp.on("keydown.enter", lambda e: _maybe_send_on_enter(e, _send))

                def _refresh_msgs() -> None:
                    msg_area.clear()
                    cid = chat_state.get("chat_id")
                    if not cid:
                        from app.services.suggestions import suggested_starters

                        starters = suggested_starters(limit=6, lang=lang)
                        with (
                            msg_area,
                            ui.column().classes("items-center w-full").style("padding-top: 32px; gap: 18px;"),
                        ):
                            ui.icon("forum").classes("text-6xl opacity-30")
                            ui.label(t("chat.start_or_pick", lang)).classes("opacity-70 text-body1")
                            ui.label(f"💡 {t('chat.ph_ask', lang)}").classes("text-caption opacity-50")
                            if starters:
                                ui.label(t("chat.try_one", lang)).classes(
                                    "text-caption opacity-70 q-mt-md"
                                ).style("letter-spacing: 0.06em;")
                                with (
                                    ui.row()
                                    .classes("gap-2 flex-wrap justify-center")
                                    .style("max-width: 720px;")
                                ):
                                    for st in starters:

                                        def _use_starter(_q=st["question"]) -> None:
                                            _new_chat()
                                            inp.value = _q
                                            import asyncio as _aio

                                            _aio.create_task(_send())

                                        with (
                                            ui.card()
                                            .classes("ldi-glass-sm q-pa-md cursor-pointer")
                                            .style("max-width: 320px; min-width: 220px;") as starter_card
                                        ):
                                            ui.label(st["question"]).classes("text-body2").style(
                                                "font-weight: 500;"
                                            )
                                            ui.label(st["hint"]).classes("text-caption opacity-60 q-mt-xs")
                                        starter_card.on("click", lambda _e, q=st["question"]: _use_starter(q))
                        chat_header_title.text = t("chat.title", lang)
                        return
                    with session_scope() as session:
                        chat_obj = session.get(Chat, cid)
                        msgs = session.exec(
                            select(ChatMessage).where(ChatMessage.chat_id == cid).order_by(ChatMessage.id)
                        ).all()
                    chat_header_title.text = (chat_obj.title if chat_obj else None) or t("chat.title", lang)
                    with msg_area:
                        for m in msgs:
                            when_str = _fmt_when(m.created_at)
                            if m.role == "user":
                                _render_user_message(m.content, when=when_str)
                            elif m.role == "assistant":
                                md_el, _md_card, footer = _render_assistant_message_open()
                                md_el.content = _link_citations(m.content, m.sources or [])
                                if m.sources:
                                    _render_sources_footer(footer, m.sources)
                                if when_str:
                                    with footer:
                                        ui.label(when_str).classes("text-caption opacity-50")
                            else:
                                with ui.row().classes("opacity-60 q-mb-sm"):
                                    ui.label(f"[{m.role}] {m.content}").classes("text-caption")
                    _scroll_msgs_to_bottom()

                _refresh_chats()
                _refresh_msgs()

    @ui.page("/tags")
    def page_tags() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/tags")
        lang = _user_lang(user)
        ui.label(t("tags.title", lang)).classes("text-h4 q-mb-md ldi-hero-text")
        from app.models import DocumentTagLink, Tag

        cloud_card = ui.card().classes("w-full p-4 q-mb-md")
        list_card = ui.column().classes("w-full gap-1")

        def _refresh() -> None:
            cloud_card.clear()
            list_card.clear()
            with session_scope() as session:
                tags = session.exec(select(Tag).order_by(Tag.name)).all()
                # Count documents per tag for the cloud sizing
                counts: dict[int, int] = {}
                for tag in tags:
                    cnt = len(
                        session.exec(select(DocumentTagLink).where(DocumentTagLink.tag_id == tag.id)).all()
                    )
                    counts[tag.id] = cnt

            # ------ Tag cloud ------
            with cloud_card:
                with ui.row().classes("items-center gap-2 w-full q-mb-sm"):
                    ui.icon("local_offer").classes("ldi-accent")
                    ui.label(f"{len(tags)} tags · cloud").classes("text-h6 flex-1")
                if not tags:
                    ui.label(t("tags.auto_generated_help", lang)).classes("text-caption opacity-70")
                else:
                    from urllib.parse import quote as _quote

                    max_count = max(counts.values()) if counts else 1
                    with ui.row().classes("flex-wrap gap-2 q-py-sm").style("line-height: 2.2;"):
                        for tag in tags:
                            count = counts.get(tag.id, 0)
                            ratio = count / max(max_count, 1)
                            size_em = 0.85 + 1.2 * ratio
                            opacity = 0.6 + 0.4 * ratio
                            chip = (
                                ui.button(
                                    f"{tag.name}  · {count}",
                                    on_click=lambda tn=tag.name: ui.navigate.to(f"/search?tag={_quote(tn)}"),
                                )
                                .props("flat no-caps")
                                .classes("ldi-pill")
                            )
                            chip.style(f"font-size: {size_em:.2f}em; opacity: {opacity:.2f};")
                            if "lang:" in tag.name or tag.name.startswith("has:"):
                                chip.classes(add="ldi-pill-warning")

            # ------ Plain list (sortable, deletable) ------
            with list_card:
                ui.label(t("tags.all_tags_heading", lang)).classes("text-h6 q-mb-sm")
                if not tags:
                    return
                with session_scope() as session:
                    refreshed = session.exec(select(Tag).order_by(Tag.name)).all()
                for tag in refreshed:
                    cnt = counts.get(tag.id, 0)
                    with ui.row().classes("items-center gap-2 w-full"):
                        ui.label(tag.name).classes("flex-1")
                        ui.label(t("tags.doc_count", lang).format(n=cnt)).classes(
                            "text-caption opacity-70"
                        ).style("min-width: 70px;")
                        if tag.auto:
                            ui.label(t("tags.auto", lang)).classes("ldi-pill text-caption")
                        ui.button(icon="delete", on_click=lambda tid=tag.id: _delete(tid)).props(
                            "flat dense color=negative"
                        )

        def _delete(tid: int) -> None:
            with ui.dialog() as d, ui.card().classes("w-[420px] p-4"):
                ui.label(t("tags.delete_confirm", lang)).classes("text-h6 ldi-primary")
                ui.label(t("tags.delete_help", lang)).classes("text-caption opacity-70")

                def _do() -> None:
                    with session_scope() as session:
                        for link in session.exec(
                            select(DocumentTagLink).where(DocumentTagLink.tag_id == tid)
                        ).all():
                            session.delete(link)
                        tag_obj = session.get(Tag, tid)  # was `t` — shadowed the i18n t()
                        if tag_obj:
                            session.delete(tag_obj)
                    d.close()
                    _refresh()

                with ui.row().classes("justify-end gap-2 w-full q-mt-md"):
                    ui.button(t("common.cancel", lang), on_click=d.close).props("flat")
                    ui.button(t("common.delete", lang), on_click=_do).props("color=negative")
            d.open()

        _refresh()

    @ui.page("/backup")
    def page_backup() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/backup")
        lang = _user_lang(user)
        ui.label(t("backup.title", lang)).classes("text-h4 q-mb-md ldi-hero-text")

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

            async def _do_backup() -> None:
                comps = [k for k, c in checks.items() if c.value]
                if not comps:
                    ui.notify(t("backup.select_at_least_one", lang), color="negative")
                    return
                import asyncio as _aio

                # Off the event loop so the UI stays responsive while zipping.
                try:
                    res = await _aio.to_thread(create_backup, comps, encrypt_password=pw.value or None)
                except Exception as e:
                    logger.warning("backup failed: {}", e)
                    ui.notify(f"{t('common.error', lang)}: {e}", color="negative")
                    return
                ui.notify(f"{t('backup.written_to', lang)} {res.path}", color="positive")
                _refresh()

            ui.button(t("backup.create_btn", lang), icon="save", on_click=_do_backup).props(
                "color=primary"
            ).classes("q-mt-md")

        # One-click preset for transferring the index to another machine
        # (scan once on a fast box, search on the others — no re-scan).
        with ui.card().classes("w-full p-3 q-mt-md"):
            ui.label(t("backup.portable_title", lang)).classes("text-h6")
            ui.label(t("backup.portable_hint", lang)).classes("text-caption opacity-70")

            async def _do_portable_backup() -> None:
                import asyncio as _aio

                try:
                    res = await _aio.to_thread(create_backup, ["db", "vector", "settings"])
                except Exception as e:
                    logger.warning("portable backup failed: {}", e)
                    ui.notify(f"{t('common.error', lang)}: {e}", color="negative")
                    return
                ui.notify(f"{t('backup.written_to', lang)} {res.path}", color="positive")
                _refresh()

            ui.button(
                t("backup.portable_btn", lang), icon="drive_file_move", on_click=_do_portable_backup
            ).props("color=primary").classes("q-mt-md")

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

                        async def _restore(b=b) -> None:
                            # Open a dialog to choose components, supply a
                            # decryption password, and optionally remap file
                            # paths for this machine before restoring.
                            with ui.dialog() as dialog, ui.card().classes("w-96 p-4 gap-2"):
                                ui.label(t("backup.restore_options", lang)).classes("text-h6")
                                ui.label(b["filename"]).classes("text-caption opacity-70 break-words")

                                ui.label(t("backup.components_label", lang)).classes("text-body2 q-mt-sm")
                                rchecks: dict[str, ui.checkbox] = {}
                                with ui.row().classes("gap-3 flex-wrap"):
                                    for comp in b["components"]:
                                        rchecks[comp] = ui.checkbox(comp, value=True)

                                rpw = (
                                    ui.input(
                                        t("backup.password_dec", lang),
                                        password=True,
                                        password_toggle_button=True,
                                    ).classes("w-full")
                                    if b.get("encrypted")
                                    else None
                                )

                                ui.label(t("backup.remap_hint", lang)).classes(
                                    "text-caption opacity-70 q-mt-sm"
                                )
                                remap_old = ui.input(t("backup.remap_old", lang)).classes("w-full")
                                remap_new = ui.input(t("backup.remap_new", lang)).classes("w-full")

                                async def _confirm() -> None:
                                    import asyncio as _aio

                                    comps = [k for k, c in rchecks.items() if c.value] or None
                                    remap = (
                                        (remap_old.value, remap_new.value)
                                        if remap_old.value and remap_new.value
                                        else None
                                    )
                                    try:
                                        res = await _aio.to_thread(
                                            restore_backup,
                                            b["path"],
                                            components=comps,
                                            password=(rpw.value or None) if rpw else None,
                                            path_remap=remap,
                                        )
                                    except Exception as e:
                                        logger.warning("restore failed for {}: {}", b["path"], e)
                                        ui.notify(f"{t('common.error', lang)}: {e}", color="negative")
                                        return
                                    dialog.close()
                                    ui.notify(
                                        f"{t('backup.restored', lang)}: {res['restored']}; "
                                        f"{t('backup.errors', lang)}: {res['errors']}",
                                        color="warning" if res.get("errors") else "positive",
                                    )

                                with ui.row().classes("justify-end w-full q-mt-md"):
                                    ui.button(t("common.cancel", lang), on_click=dialog.close).props("flat")
                                    ui.button(t("backup.restore", lang), on_click=_confirm).props(
                                        "color=primary"
                                    )
                            dialog.open()

                        ui.button(t("backup.restore", lang), on_click=_restore).props("dense")

        _refresh()

    @ui.page("/settings")
    def page_settings() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/settings")
        lang = _user_lang(user)
        ui.label(t("settings.title", lang)).classes("text-h4 q-mb-md ldi-hero-text")
        s = get_settings()

        with ui.card().classes("w-full p-3"):
            ui.label(t("settings.lmstudio", lang)).classes("text-h6")
            url = ui.input(t("settings.base_url", lang), value=s.lmstudio_base_url).classes("w-full")

            # Free-text inputs so any model id can be typed in regardless of
            # how LM Studio's /v1/models responds. Buttons below let you pick
            # from a popup listing the actually-loaded models.
            chat_model = ui.input(
                t("settings.chat_model", lang),
                value=s.chat_model,
                placeholder="e.g. qwen2.5-7b-instruct",
            ).classes("w-full")
            vision_model = ui.input(
                t("settings.vision_model", lang),
                value=s.vision_model,
                placeholder="e.g. llava-llama-3-8b-v1_1 (optional)",
            ).classes("w-full")
            emb_model = ui.input(
                t("settings.emb_model", lang),
                value=s.embedding_model,
                placeholder="e.g. text-embedding-bge-m3",
            ).classes("w-full")

            connection_status = ui.label("").classes("text-caption q-mt-sm opacity-80")

            # ----- Model browser dialog -----------------------------------
            async def _fetch_models() -> tuple[list[dict], str | None]:
                from app.llm import LMStudioClient

                c = LMStudioClient(base_url=url.value or s.lmstudio_base_url)
                try:
                    return await c.list_models(), None
                except Exception as e:
                    return [], str(e)

            def _classify(mid: str) -> str:
                lid = mid.lower()
                if any(k in lid for k in ("embed", "bge", "nomic", "e5-", "gte-", "snowflake-arctic")):
                    return "embedding"
                if any(k in lid for k in ("-vl", "vision", "llava", "moondream", "internvl")):
                    return "vision"
                return "chat"

            async def _browse_models() -> None:
                raw, err = await _fetch_models()
                with ui.dialog() as dialog, ui.card().classes("w-[640px] p-4"):
                    ui.label("Loaded models in LM Studio").classes("text-h6 ldi-primary")
                    if err:
                        ui.label(f"✗ Cannot reach LM Studio: {err}").classes(
                            "ldi-pill ldi-pill-error q-mb-md"
                        ).style("white-space: normal;")
                        ui.label(
                            "Open LM Studio → Developer tab → Start Server. Make sure "
                            "at least one chat *and* one embedding model are loaded."
                        ).classes("text-caption opacity-80")
                    elif not raw:
                        ui.label(
                            "LM Studio answered but the model list is empty. "
                            "Load a model in LM Studio's Developer / Local Server tab."
                        ).classes("ldi-pill ldi-pill-warning q-mb-md").style("white-space: normal;")
                    else:
                        ui.label(f"{len(raw)} model(s) currently loaded:").classes(
                            "text-caption q-mb-sm opacity-80"
                        )
                        for m in raw:
                            if not isinstance(m, dict):
                                continue
                            mid = m.get("id") or m.get("model")
                            if not mid:
                                continue
                            role = _classify(mid)
                            with ui.row().classes("items-center gap-2 w-full no-wrap q-mb-xs"):
                                ui.label(role.upper()).classes(
                                    "ldi-pill " + ("ldi-pill-success" if role == "embedding" else "")
                                ).style("min-width: 80px;")
                                ui.label(mid).classes("flex-1 text-body2 ellipsis")

                                def _use(model_id=mid, target_role=role) -> None:
                                    # Persist to settings.json immediately so the
                                    # Save button isn't required (and so a later
                                    # browser sync can't blank the field).
                                    key = {
                                        "embedding": "embedding_model",
                                        "vision": "vision_model",
                                        "chat": "chat_model",
                                    }[target_role]
                                    save_user_settings({key: model_id})
                                    get_settings.cache_clear()
                                    if target_role == "embedding":
                                        emb_model.set_value(model_id)
                                    elif target_role == "vision":
                                        vision_model.set_value(model_id)
                                    else:
                                        chat_model.set_value(model_id)
                                    ui.notify(f"Saved as {target_role}: {model_id}", color="positive")

                                ui.button("Use", on_click=_use).props("dense flat color=primary")

                        ui.separator().classes("q-my-md")
                        # Optional: show raw JSON for diagnosis
                        with ui.expansion("Raw response (for debugging)", icon="code"):
                            import json as _json

                            ui.code(_json.dumps(raw, indent=2), language="json").classes("w-full").style(
                                "max-height: 240px; overflow: auto;"
                            )

                    with ui.row().classes("justify-end w-full q-mt-md"):
                        ui.button("Close", on_click=dialog.close).props("flat")
                dialog.open()

            async def _auto_pick() -> None:
                raw, err = await _fetch_models()
                if err:
                    connection_status.text = f"✗ {err}"
                    ui.notify(err, color="negative")
                    return
                chat = ""
                emb = ""
                vis = ""
                for m in raw:
                    if not isinstance(m, dict):
                        continue
                    mid = m.get("id") or m.get("model")
                    if not mid:
                        continue
                    role = _classify(mid)
                    if role == "embedding" and not emb:
                        emb = mid
                    elif role == "vision" and not vis:
                        vis = mid
                    elif role == "chat" and not chat:
                        chat = mid
                updates: dict[str, Any] = {}
                if chat:
                    chat_model.set_value(chat)
                    updates["chat_model"] = chat
                if emb:
                    emb_model.set_value(emb)
                    updates["embedding_model"] = emb
                if vis:
                    vision_model.set_value(vis)
                    updates["vision_model"] = vis
                if updates:
                    save_user_settings(updates)
                    get_settings.cache_clear()
                applied = [k for k, v in (("chat", chat), ("embedding", emb), ("vision", vis)) if v]
                if applied:
                    ui.notify(f"Auto-picked & saved: {', '.join(applied)}", color="positive")
                else:
                    ui.notify("No models found. Load at least one in LM Studio.", color="warning")
                connection_status.text = (
                    f"✓ {len(raw)} model(s) loaded · chat={chat or '—'} · "
                    f"embedding={emb or '—'} · vision={vis or '—'}"
                )

            async def _test() -> None:
                from app.llm import LMStudioClient

                c = LMStudioClient(base_url=url.value)
                ok = await c.ping()
                connection_status.text = "✓ Reachable" if ok else f"✗ Cannot reach {url.value}"
                ui.notify(
                    f"{t('settings.lm_reachable', lang)} {ok}",
                    color=("positive" if ok else "negative"),
                )
                if ok and emb_model.value:
                    emb_ok, msg = await c.preflight_embed(model=emb_model.value)
                    connection_status.text += " · embed: " + ("✓" if emb_ok else f"✗ {msg}")

            help_card = ui.card().classes("w-full q-mt-sm").style("display: none;")

            async def _test_embed() -> None:
                from app.llm import LMStudioClient

                mid = (emb_model.value or "").strip()
                if not mid:
                    ui.notify("Enter an embedding model id first", color="warning")
                    return
                c = LMStudioClient(base_url=url.value or s.lmstudio_base_url)
                ok, message = await c.preflight_embed(model=mid)
                help_card.clear()
                if ok:
                    connection_status.text = f"✓ Embedding works · {message}"
                    ui.notify(f"Embedding OK: {message}", color="positive")
                    help_card.style("display: none;")
                else:
                    connection_status.text = f"✗ {message}"
                    ui.notify("Embedding failed — see hint below", color="negative")
                    help_card.style("display: block;")
                    with help_card:
                        ui.label("How to fix this in LM Studio").classes("text-h6 ldi-primary")
                        ui.markdown(
                            "**LM Studio needs the embedding model loaded as an "
                            "*Embedding* model, not a Chat model.**\n\n"
                            "1. Open **LM Studio Desktop** → left sidebar **Developer**.\n"
                            "2. In *Models loaded for inference*, click the ⋯ menu / "
                            "wrench next to your embedding model.\n"
                            "3. Set its **type / role** to **Embedding** (some versions "
                            "show a dropdown directly under the model name; older ones "
                            "need you to remove and re-load it with *'Use as embedding "
                            "model'*).\n"
                            "4. Toggle the server off and on at the top.\n"
                            "5. Verify in your browser: `http://localhost:1234/v1/models` "
                            "should list this model. Come back and click **Test embedding** "
                            "again."
                        ).classes("text-body2 q-mb-sm")
                        ui.label("Diagnostic detail").classes("text-caption opacity-70 q-mt-sm")
                        ui.code(message, language="text").classes("w-full").style(
                            "max-height: 200px; overflow: auto;"
                        )

            async def _debug_embed() -> None:
                """Probe every plausible embeddings endpoint and show the raw
                response from each. Diagnose without DevTools."""
                import httpx as _httpx

                mid = (emb_model.value or "").strip()
                base = (url.value or s.lmstudio_base_url).rstrip("/")
                if not mid:
                    ui.notify("Enter an embedding model id first", color="warning")
                    return
                native = base[: -len("/v1")] if base.endswith("/v1") else base
                candidates = [
                    f"{base}/embeddings",
                    f"{native}/api/v0/embeddings",
                    f"{base}/embedding",
                ]
                payload = {"model": mid, "input": ["debug ping"]}
                rows: list[tuple[str, str, str]] = []
                async with _httpx.AsyncClient(
                    timeout=12.0,
                    headers={
                        "Authorization": "Bearer lm-studio",
                        "Content-Type": "application/json",
                    },
                ) as ac:
                    for ep in candidates:
                        try:
                            r = await ac.post(ep, json=payload)
                            rows.append((ep, str(r.status_code), r.text[:800]))
                        except Exception as e:
                            rows.append((ep, "ERROR", f"{type(e).__name__}: {e}"))

                with ui.dialog() as dialog, ui.card().classes("w-[720px] p-4"):
                    ui.label("Embedding endpoint diagnostic").classes("text-h6 ldi-primary")
                    ui.label(f"Model: {mid}").classes("text-caption opacity-80 q-mb-sm")
                    for ep, status, body in rows:
                        is_ok = (
                            status.isdigit()
                            and 200 <= int(status) < 300
                            and ('"embedding"' in body or '"data"' in body)
                        )
                        with ui.expansion(
                            f"{ep}  →  {status}",
                            icon="check_circle" if is_ok else "report",
                        ).classes("w-full q-mb-xs"):
                            ui.code(body, language="json").classes("w-full").style(
                                "max-height: 260px; overflow: auto;"
                            )
                    ui.markdown(
                        "**How to read this:**\n"
                        '- `200` + body with `"embedding"`: endpoint works ✓\n'
                        '- `200` + `{"error":"Unexpected endpoint…"}`: '
                        "llama.cpp's wrong-path quirk — try a different URL\n"
                        "- `404`: path doesn't exist on this server\n"
                        '- `400` + `"Model not found"`: model id wrong / not loaded'
                    ).classes("text-caption q-mt-md")
                    with ui.row().classes("justify-end w-full q-mt-md"):
                        ui.button("Close", on_click=dialog.close).props("flat")
                dialog.open()

            with ui.row().classes("gap-2 q-mt-sm flex-wrap"):
                ui.button(t("settings.test_connection", lang), icon="cable", on_click=_test).props("dense")
                ui.button("Test embedding", icon="psychology", on_click=_test_embed).props("dense")
                ui.button("Debug embedding", icon="bug_report", on_click=_debug_embed).props("dense")
                ui.button("Browse models", icon="list", on_click=_browse_models).props("dense")
                ui.button("Auto-pick", icon="auto_fix_high", on_click=_auto_pick).props("dense color=primary")

        with ui.card().classes("w-full p-3 q-mt-md"):
            ui.label(t("settings.ocr", lang)).classes("text-h6")
            tcmd = ui.input(t("settings.tesseract", lang), value=s.tesseract_cmd).classes("w-full")
            tlang = ui.input(t("settings.ocr_langs", lang), value=s.ocr_lang).classes("w-full")
            tess_status = ui.label("").classes("text-caption opacity-80 q-mt-xs")

            def _detect_tesseract() -> None:
                """Probe common install locations and PATH for tesseract.exe."""
                import os
                import shutil
                from pathlib import Path as _P

                candidates = [
                    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
                    os.path.expandvars(r"%USERPROFILE%\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
                    shutil.which("tesseract") or "",
                ]
                for c in candidates:
                    if c and _P(c).is_file():
                        tcmd.set_value(c)
                        save_user_settings({"tesseract_cmd": c})
                        get_settings.cache_clear()
                        tess_status.text = f"✓ Found and saved: {c}"
                        ui.notify(f"Tesseract found: {c}", color="positive")
                        return
                tess_status.text = (
                    "✗ Tesseract not found. Install from " "github.com/UB-Mannheim/tesseract/wiki"
                )
                ui.notify("Tesseract not found — see the OCR link", color="warning")

            with ui.row().classes("gap-2 q-mt-sm"):
                ui.button("Auto-detect Tesseract", icon="search", on_click=_detect_tesseract).props("dense")
                ui.link(
                    "Download Tesseract",
                    "https://github.com/UB-Mannheim/tesseract/wiki",
                    new_tab=True,
                ).classes("text-caption q-pa-sm")

        with ui.card().classes("w-full p-3 q-mt-md"):
            ui.label(t("settings.indexing", lang)).classes("text-h6")
            csize = ui.number(t("settings.chunk_size", lang), value=s.chunk_size).classes("w-32")
            coverlap = ui.number(t("settings.chunk_overlap", lang), value=s.chunk_overlap).classes("w-32")

        with ui.card().classes("w-full p-3 q-mt-md"):
            ui.label(t("settings.performance", lang)).classes("text-h6")
            from app.services.hardware import detect_hardware, resolve_tuning

            _hw = detect_hardware()
            _ram = f"{_hw.total_ram_gb:.0f} GB" if _hw.total_ram_gb else "?"

            def _perf_text(profile: str) -> str:
                tn = resolve_tuning(profile or "auto", _hw, worker_override=s.parallel_workers or 0)
                gpu = f" · {_hw.gpu}" if _hw.has_gpu else ""
                return (
                    f"{_hw.physical_cores} cores · {_ram} RAM{gpu}  →  "
                    f"{tn.tier} · {tn.workers} workers · DPI {tn.page_dpi} · batch {tn.embed_batch}"
                )

            perf = ui.select(
                {
                    "auto": t("settings.perf_auto", lang),
                    "low": t("settings.perf_low", lang),
                    "balanced": t("settings.perf_balanced", lang),
                    "high": t("settings.perf_high", lang),
                },
                value=s.performance_profile,
                label=t("settings.performance_profile", lang),
                on_change=lambda e: perf_summary.set_text(_perf_text(e.value)),
            ).classes("w-72")
            ui.label(t("settings.perf_hint", lang)).classes("text-caption opacity-70")
            perf_summary = ui.label(_perf_text(s.performance_profile)).classes(
                "text-caption ldi-primary q-mt-xs"
            )

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
            # Stringify and strip — NiceGUI inputs return None when never
            # touched, and trailing whitespace breaks model id lookups.
            def _str(x):
                return (x or "").strip() if isinstance(x, str) else x

            save_user_settings(
                {
                    "lmstudio_base_url": _str(url.value) or s.lmstudio_base_url,
                    "chat_model": _str(chat_model.value),
                    "vision_model": _str(vision_model.value),
                    "embedding_model": _str(emb_model.value),
                    "tesseract_cmd": _str(tcmd.value),
                    "ocr_lang": _str(tlang.value) or s.ocr_lang,
                    "chunk_size": int(csize.value or s.chunk_size),
                    "chunk_overlap": int(coverlap.value or s.chunk_overlap),
                    "performance_profile": perf.value or s.performance_profile,
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
        ui.label(t("logs.title", lang)).classes("text-h4 q-mb-md ldi-hero-text")
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
    def page_compare(a: int = 0, b: int = 0) -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/compare")
        lang = _user_lang(user)
        ui.label(t("compare.title", lang)).classes("text-h4 q-mb-md ldi-hero-text")

        with session_scope() as session:
            docs = session.exec(select(Document).order_by(Document.id.desc()).limit(2000)).all()
            options = {d.id: f"#{d.id} — {d.filename}" for d in docs if d.id is not None}

        if not options:
            ui.label(t("compare.index_first", lang)).classes("opacity-70")
            return

        # Pre-select when arriving from a deep link (e.g. Diagnostics → Compare,
        # which links to /compare?a=<id>&b=<id>).
        pre_a = a if a in options else None
        pre_b = b if b in options else None
        with ui.row().classes("w-full gap-2"):
            sel_a = ui.select(options, value=pre_a, label=t("compare.doc_a", lang), with_input=True).classes(
                "flex-1"
            )
            sel_b = ui.select(options, value=pre_b, label=t("compare.doc_b", lang), with_input=True).classes(
                "flex-1"
            )

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

        # Auto-run when both documents arrived via the deep link.
        if pre_a and pre_b and pre_a != pre_b:
            ui.timer(0.1, _run, once=True)

    @ui.page("/diagnostics")
    def page_diagnostics() -> None:
        user = _require_login()
        if not user:
            return
        _layout(user, "/diagnostics")
        lang = _user_lang(user)
        ui.label(t("diag.title", lang)).classes("text-h4 q-mb-md ldi-hero-text")

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
            hw_card.clear()
            metrics_card.clear()
            dup_card.clear()
            near_card.clear()
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

                # Tesseract OCR availability
                ui.separator().classes("q-my-sm")
                ui.label("OCR (Tesseract)").classes("text-body2 ldi-primary")
                try:
                    from app.ocr.tesseract import tesseract_available

                    tess_ok = tesseract_available()
                except Exception:
                    tess_ok = False
                if tess_ok:
                    ui.label("✓ Tesseract found — OCR for scanned PDFs is available").classes(
                        "text-caption text-positive"
                    )
                else:
                    ui.label(
                        "✗ Tesseract not found — OCR for image-only PDFs is disabled. "
                        "Native PDF text is still indexed."
                    ).classes("text-caption text-negative")
                    ui.link(
                        "Download Tesseract for Windows",
                        "https://github.com/UB-Mannheim/tesseract/wiki",
                        new_tab=True,
                    ).classes("text-caption")

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

            with hw_card:
                from app.services.hardware import tuning_summary

                ui.label(t("diag.hardware", lang)).classes("text-h6")
                summ = tuning_summary()
                hw = summ["hardware"]
                tn = summ["tuning"]
                ram = f"{hw['total_ram_gb']:.0f} GB" if hw["total_ram_gb"] else "?"
                gpu = hw["gpu"] or "—"
                with ui.row().classes("gap-4 flex-wrap"):
                    ui.label(
                        f"{t('diag.hw_cpu', lang)}: {hw['physical_cores']} cores "
                        f"({hw['logical_cores']} threads)"
                    ).classes("text-caption")
                    ui.label(f"{t('diag.hw_ram', lang)}: {ram}").classes("text-caption")
                    ui.label(f"{t('diag.hw_gpu', lang)}: {gpu}").classes("text-caption")
                profile_label = tn["profile"]
                if tn["auto_resolved"]:
                    profile_label = f"auto → {tn['tier']}"
                with ui.row().classes("gap-4 flex-wrap q-mt-xs"):
                    ui.label(f"{t('diag.hw_profile', lang)}: {profile_label}").classes(
                        "text-caption ldi-primary"
                    )
                    ui.label(
                        f"{t('diag.hw_workers', lang)}: {tn['workers']} "
                        f"(quick {tn['quick_workers']})" + ("  ⚙" if tn["worker_override"] else "")
                    ).classes("text-caption")
                    ui.label(f"{t('diag.hw_embed_batch', lang)}: {tn['embed_batch']}").classes("text-caption")
                    ui.label(f"{t('diag.hw_page_dpi', lang)}: {tn['page_dpi']}").classes("text-caption")
                if not hw["psutil_available"]:
                    ui.label(t("diag.metrics_disabled", lang)).classes("text-caption opacity-70")

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
        hw_card = ui.card().classes("w-full p-3 q-mt-md")
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
        ui.label(t("about.title", lang)).classes("text-h4 q-mb-md ldi-hero-text")
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
                t("about.github", lang), "https://github.com/Various5/localdoc-intelligence", new_tab=True
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

    # Pin NiceGUI's per-user storage to our writable data dir — otherwise it
    # defaults to ``<cwd>/.nicegui`` which is read-only when the frozen exe
    # runs from ``C:\\Program Files\\...``, silently dropping login state.
    try:
        from pathlib import Path as _P

        s = get_settings()
        storage_path = _P(s.data_path) / "nicegui_storage"
        storage_path.mkdir(parents=True, exist_ok=True)
        nicegui_app.storage.path = storage_path
    except Exception:
        pass

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
        # Give the server a generous grace period before a slow background
        # job is treated as a "client gone". Heavy CPU work runs in threads
        # but a single huge PDF can still spike total latency.
        reconnect_timeout=90.0,
    )
