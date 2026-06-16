"""NiceGUI frontend — registers all UI pages onto the FastAPI app.

The UI talks to the same process via direct Python service calls (no
HTTP roundtrips). Session-cookie auth is shared with the API.
"""

from __future__ import annotations

import time
from datetime import UTC
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI
from nicegui import app as nicegui_app
from nicegui import ui
from sqlalchemy import func
from sqlmodel import select

from app import __app_name__, __contact__, __version__
from app.auth.acl import can_see_document, filter_documents
from app.auth.security import (
    SESSION_USER_KEY,
    create_user,
    has_users,
    hash_password,
    make_recovery_key,
    session_fingerprint,
    verify_password,
)
from app.config import get_settings, save_user_settings
from app.database import session_scope, write_session
from app.llm import LMStudioClient, reset_client_cache
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
from app.services import licensing
from app.services.hardware import detect_hardware
from app.services.indexer import abort_scan_job, start_scan_in_background
from app.ui.components import (
    breadcrumbs,
    confirm_dialog,
    empty_state,
    error_state,
    help_callout,
    page_header,
    section_card,
    skeleton_list,
    status_pill,
)
from app.ui.styles import build_global_css
from app.ui.themes import DEFAULT_THEME, THEME_ORDER, THEMES
from app.utils.i18n import SUPPORTED_LANGUAGES, t
from app.utils.logging import logger
from app.utils.secret_store import delete_secret, get_secret, put_secret

# Shared with the page-match API (app/api/routes/documents.py) — the on-image
# highlight rects, the snippet highlighter and find-in-document must agree on
# which query tokens count. Re-exported here for the existing UI call sites.
from app.utils.terms import HL_STOPWORDS as _HL_STOPWORDS  # noqa: F401
from app.utils.terms import meaningful_terms  # noqa: F401

REMEMBER_SECRET_NAME = "ui_login_remember"  # legacy encrypted creds — scrubbed on login
# Per-browser remembered USERNAME (app.storage.user is keyed to a per-browser
# cookie). We deliberately never store the password and never auto-login: the
# long-lived session cookie already keeps you signed in between visits, and a
# stored/auto-filled password is a credential-leak + privilege-escalation risk on
# a LAN/WAN-exposed instance.
REMEMBER_USERNAME_KEY = "remember_username"


def _classify_model(mid: str) -> str:
    """Heuristically bucket an LM Studio model id into chat / embedding / vision."""
    lid = mid.lower()
    if any(k in lid for k in ("embed", "bge", "nomic", "e5-", "gte-", "snowflake-arctic")):
        return "embedding"
    if any(k in lid for k in ("-vl", "vision", "llava", "moondream", "internvl")):
        return "vision"
    return "chat"


def _current_user() -> User | None:
    try:
        uid = nicegui_app.storage.user.get(SESSION_USER_KEY)
        if uid is None:
            return None
        with session_scope() as session:
            user = session.get(User, uid)
        if not user or not user.is_active:
            return None
        # Session invalidation: if the password changed since this session was
        # stamped (its hash fingerprint differs), treat the session as expired.
        if nicegui_app.storage.user.get("pwv") != session_fingerprint(user):
            return None
        return user
    except Exception:
        return None


def _bridge_session_cookie(user: User | None) -> None:
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
    Also stamps the password fingerprint so the API's get_current_user can revoke
    the session on a password change (matching the UI check).
    """
    if user is None:
        return
    try:
        if nicegui_app.storage.browser.get(SESSION_USER_KEY) != user.id:
            nicegui_app.storage.browser[SESSION_USER_KEY] = user.id
        pwv = session_fingerprint(user)
        if nicegui_app.storage.browser.get("pwv") != pwv:
            nicegui_app.storage.browser["pwv"] = pwv
    except Exception:
        # Read-only outside an initial page request — nothing to do.
        pass


def _require_login() -> User | None:
    u = _current_user()
    if not u:
        ui.navigate.to("/login")
        return None
    _bridge_session_cookie(u)
    return u


def _require_license() -> bool:
    """Whole-app license gate: True if activated, else redirect to ``/activate``.

    The primary gate lives in ``_layout`` (covers every chrome page); this
    helper is for pages that need to bail out before doing work.
    """
    if licensing.is_activated():
        return True
    ui.navigate.to("/activate")
    return False


def _render_license_summary(status: licensing.LicenseStatus, lang: str) -> None:
    """Status pill + licensee/plan/expiry lines for a LicenseStatus (shared by
    the /activate page and the Settings → License tab)."""
    info = status.info
    if status.active and info:
        with ui.row().classes("items-center gap-2"):
            ui.label("● " + t("license.status_active", lang)).classes("ldi-pill ldi-pill-success")
            if info.plan:
                ui.label(info.plan).classes("ldi-pill")
        if info.licensee:
            ui.label(f"{t('license.licensee', lang)}: {info.licensee}").classes("text-body2 q-mt-xs")
        if info.is_perpetual:
            ui.label(t("license.perpetual", lang)).classes("text-caption opacity-70")
        else:
            days = licensing.days_remaining(info)
            line = f"{t('license.expires', lang)}: {info.expires}"
            if days is not None:
                line += "  ·  " + t("license.days_left", lang).format(n=days)
            ui.label(line).classes("text-caption opacity-70")
        return
    if status.reason == "expired":
        ui.label(t("license.status_expired", lang)).classes("ldi-pill ldi-pill-error")
        if info and info.expires:
            ui.label(t("license.expired_on", lang).format(d=info.expires)).classes("text-caption opacity-70")
    else:
        ui.label(t("license.status_inactive", lang)).classes("ldi-pill")


def _media_token() -> str:
    """Signed token authorizing media URLs (PDF/page-image) for new tabs / <img>.

    Reads the logged-in user from NiceGUI session storage (available in any page
    or handler context). Without it, browser-issued requests to
    /api/documents/.../file (a new tab) and /page/{n}/image (an <img> src) can
    401 because the session isn't carried — see media_user() in
    app/api/routes/documents.py.
    """
    try:
        uid = nicegui_app.storage.user.get(SESSION_USER_KEY)
        # The pwv fingerprint is already in session storage (stamped at login),
        # so the media token binds to it without a per-image DB lookup.
        fp = nicegui_app.storage.user.get("pwv")
    except Exception:
        uid = None
        fp = None
    if uid is None:
        return ""
    from app.auth.security import make_media_token

    return make_media_token(int(uid), fp)


def pdf_url(document_id: int, page: int | None = None, token: str | None = None) -> str:
    """Build a URL to the original PDF that jumps to ``page`` when the browser
    PDF viewer opens it. ``token`` (a signed media token) authorizes the new tab."""
    base = f"/api/documents/{document_id}/file"
    if token:
        base += f"?t={token}"
    if page and page > 0:
        # `#page=N` is the standard fragment understood by Chrome/Edge/Firefox
        # PDF viewers (and Adobe Reader).
        base += f"#page={page}"
    return base


def media_image_url(document_id: int, page: int = 1) -> str:
    """Token-authorized page-image URL for <img>/ui.image sources."""
    tok = _media_token()
    base = f"/api/documents/{document_id}/page/{page}/image"
    return f"{base}?t={tok}" if tok else base


def doc_image_url(document_id: int, image_id: int) -> str:
    """Token-authorized URL for an embedded image extracted during a vision scan."""
    tok = _media_token()
    base = f"/api/documents/{document_id}/img/{image_id}"
    return f"{base}?t={tok}" if tok else base


def _images_by_page(doc_ids: set[int]) -> dict[tuple[int, int], list[int]]:
    """Map (document_id, page_number) -> [DocumentImage id, …] for the given docs,
    so result cards can show the embedded pictures on a matched page. Empty until
    a vision scan has extracted images."""
    from collections import defaultdict

    from app.models import DocumentImage

    out: dict[tuple[int, int], list[int]] = defaultdict(list)
    ids = [d for d in doc_ids if d is not None]
    if not ids:
        return out
    with session_scope() as session:
        rows = session.exec(
            select(DocumentImage).where(DocumentImage.document_id.in_(ids))  # type: ignore[attr-defined]
        ).all()
    for im in rows:
        out[(im.document_id, im.page_number)].append(im.id)
    return out


def open_pdf(document_id: int, page: int | None = None) -> None:
    """Open the PDF in a new browser tab (token-authorized) on the given page."""
    ui.run_javascript(f"window.open({pdf_url(document_id, page, _media_token())!r}, '_blank')")


def download_pdf_url(document_id: int) -> str:
    """Token-authorized URL that serves the original PDF as an attachment."""
    tok = _media_token()
    base = f"/api/documents/{document_id}/file?download=1"
    return f"{base}&t={tok}" if tok else base


def download_pdf(document_id: int) -> None:
    """Save the original PDF to disk (Content-Disposition: attachment) without
    navigating away — clicks a transient hidden <a download> anchor."""
    url = download_pdf_url(document_id)
    ui.run_javascript(
        "const a=document.createElement('a');"
        f"a.href={url!r};a.download='';"
        "document.body.appendChild(a);a.click();a.remove();"
    )


def highlight_terms(text: str, query: str) -> str:
    """HTML-escape ``text`` and wrap meaningful query terms in ``<mark>``.

    Stopwords and 1-char tokens are dropped so a full-sentence chat question
    only highlights the words that actually matter. Shared by search results,
    chat source cards and the in-app viewer. Returns an HTML-safe string —
    render with ``ui.html``.
    """
    import html as _html
    import re as _re

    safe = _html.escape(text or "")
    terms = meaningful_terms(query)
    if not terms:
        return safe
    try:
        pattern = _re.compile("(" + "|".join(_re.escape(w) for w in terms) + ")", flags=_re.IGNORECASE)
        return pattern.sub(lambda m: f"<mark class='ldi-mark'>{m.group(0)}</mark>", safe)
    except _re.error:
        return safe


# Friendly leading icon per system-tag namespace. The raw "lang:de" / "has:org"
# keys look like debug output, so chips show an icon + the suffix instead.
_TAG_NS_ICON = {"lang": "language", "has": "check_circle", "sensitive": "shield", "type": "description"}


def _tag_display(tg: str) -> tuple[str, str | None]:
    """``(label, icon)`` for a chip: namespaced system tags show their suffix +
    an icon; free topics show their name and no icon."""
    if ":" in tg:
        prefix, _, suffix = tg.partition(":")
        return (suffix or tg), _TAG_NS_ICON.get(prefix.lower())
    return tg, None


def render_tag_chips(
    tags: list[str], *, limit: int = 6, clickable: bool = True, show_overflow: bool = True
) -> None:
    """Compact, screen-fitting tag chips with a ``+N`` overflow popover.

    Free topic tags come first (solid pills), then namespaced system tags
    (``lang:`` / ``has:`` / ``sensitive:`` …) rendered muted with an icon + the
    suffix only — never the raw ``prefix:`` key. Clickable chips deep-link to
    ``/search?tag=`` using the FULL tag value. Shared by document cards, search
    results and the viewer so tags look and behave the same everywhere.
    """
    seen: list[str] = []
    for tg in tags:
        if tg and tg not in seen:
            seen.append(tg)
    if not seen:
        return
    topics = [tg for tg in seen if ":" not in tg]
    system = [tg for tg in seen if ":" in tg]
    ordered = topics + system
    shown = ordered[:limit]
    rest = ordered[limit:]
    with ui.row().classes("items-center gap-1 flex-wrap"):
        for tg in shown:
            label, icon = _tag_display(tg)
            cls = "ldi-pill" if ":" not in tg else "ldi-pill ldi-muted"
            if clickable:
                ui.button(
                    label, icon=icon, on_click=lambda n=tg: ui.navigate.to(f"/search?tag={quote(n)}")
                ).props("flat dense no-caps size=sm").classes(cls)
            else:
                with ui.row().classes(cls + " items-center gap-1 no-wrap").style("padding: 2px 8px;"):
                    if icon:
                        ui.icon(icon).style("font-size: 13px;")
                    ui.label(label)
        if rest and show_overflow:
            if clickable:
                with (
                    ui.button(f"+{len(rest)}")
                    .props("flat dense no-caps size=sm")
                    .classes("ldi-pill ldi-muted")
                ):
                    with ui.menu():
                        for tg in rest:
                            label, _ic = _tag_display(tg)
                            ui.menu_item(
                                label, on_click=lambda n=tg: ui.navigate.to(f"/search?tag={quote(n)}")
                            )
            else:
                ui.label(f"+{len(rest)}").classes("ldi-pill ldi-muted").style("font-size: 12px;")


def tags_for_documents(doc_ids) -> dict[int, list[str]]:
    """Batch-fetch tag names per document id (two queries), ordered by global
    document-frequency so the most-used tags rank first — that way, when chips
    are truncated to a limit, the meaningful tags survive instead of an
    arbitrary id-order slice. ``render_tag_chips`` then floats topics ahead of
    namespaced system tags. Avoids N+1 on search results."""
    ids = [int(i) for i in doc_ids if i is not None]
    if not ids:
        return {}
    from sqlalchemy import func as _func

    from app.models import DocumentTagLink as _DTL
    from app.models import Tag as _Tag

    with session_scope() as session:
        counts = dict(
            session.exec(select(_DTL.tag_id, _func.count(_DTL.document_id)).group_by(_DTL.tag_id)).all()
        )
        rows = session.exec(
            select(_DTL.document_id, _DTL.tag_id, _Tag.name)
            .join(_Tag, _Tag.id == _DTL.tag_id)
            .where(_DTL.document_id.in_(ids))
        ).all()
    by_doc: dict[int, list[tuple[int, str]]] = {}
    for did, tid, name in rows:
        by_doc.setdefault(did, []).append((tid, name))
    out: dict[int, list[str]] = {}
    for did, items in by_doc.items():
        items.sort(key=lambda it: (-counts.get(it[0], 0), it[1].lower()))
        out[did] = [name for _tid, name in items]
    return out


def _now_utc():
    from datetime import datetime

    return datetime.now(UTC)


async def _maybe_send_on_enter(event, send_fn) -> None:
    """Send on plain Enter; allow Shift+Enter for a newline.

    ``send_fn`` is awaited here so it runs inside the event's NiceGUI client
    context. A bare ``asyncio.create_task(send_fn())`` would run it detached in
    a fresh task whose slot stack is empty, raising "The current slot cannot be
    determined…" the instant it touches the UI.
    """
    args = getattr(event, "args", {}) or {}
    if args.get("shiftKey"):
        return  # let the textarea grow
    await send_fn()


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


def _apply_theme(theme_name: str = DEFAULT_THEME) -> None:
    if theme_name == "system":
        # Follow the OS: None = auto. Quasar toggles body--dark from the OS
        # preference, and styles.py emits both variable sets via @media.
        ui.dark_mode().set_value(None)
    else:
        theme = THEMES.get(theme_name) or THEMES[DEFAULT_THEME]
        ui.dark_mode().set_value(theme.is_dark)
    # A cacheable <link> instead of ~24 KB inline <style>: the browser fetches
    # the stylesheet once per app version (?v= busts on update) instead of
    # re-parsing it on every navigation. Served by /ldi/theme/{name}.css below.
    if theme_name not in THEMES and theme_name != "system":
        theme_name = DEFAULT_THEME
    # Preload the primary body font (Inter latin) so the scanner fetches it
    # early instead of waiting for layout to need a glyph — removes the FOUT
    # text-reflow flash on navigation. crossorigin is mandatory for fonts even
    # same-origin; the href is unversioned so it shares the @font-face cache
    # entry (no double fetch).
    ui.add_head_html(
        '<link rel="preload" as="font" type="font/woff2" crossorigin '
        'href="/ldi-static/fonts/inter-latin-wght-normal.woff2">'
    )
    ui.add_head_html(f'<link rel="stylesheet" href="/ldi/theme/{theme_name}.css?v={quote(__version__)}">')


def _user_prefs(user: User, *, fresh: bool = False) -> tuple[str, str]:
    """``(theme, lang)`` from the user's UserSetting row — ONE query per page build.

    ``_layout`` needs the theme, then the lang, and most page bodies ask for the
    lang again; without caching that's three identical SELECTs per navigation.
    The result is pinned to the current NiceGUI client (= this page build), so
    a fresh navigation re-reads the DB and theme/language changes (which always
    reload) are picked up immediately. Handlers that MUTATE prefs based on the
    current value (theme cycle) must pass ``fresh=True`` — another tab may have
    changed the DB since this client was built.
    """
    client = None
    try:
        client = ui.context.client
        cached = getattr(client, "_ldi_prefs", None)
        if not fresh and cached is not None and cached[0] == user.id:
            return cached[1], cached[2]
    except Exception:
        pass
    with session_scope() as session:
        s = session.exec(select(UserSetting).where(UserSetting.user_id == user.id)).first()
        theme = s.theme if s and s.theme in THEMES else DEFAULT_THEME
        lang = (s.language if s else "en") or "en"
        lang = lang if lang in SUPPORTED_LANGUAGES else "en"
    if client is not None:
        client._ldi_prefs = (user.id, theme, lang)
    return theme, lang


def _user_theme(user: User) -> str:
    return _user_prefs(user)[0]


def _user_lang(user: User) -> str:
    return _user_prefs(user)[1]


# Each entry: (i18n key, path, icon, expert_only).
# ``expert_only`` items are hidden in Basic mode and only appear once the user
# flips the Basic⟷Expert switch in the drawer — they're advanced/occasional
# tools (compare, backup, diagnostics, logs, about) rather than day-to-day work.
NAV_ITEMS: list[tuple[str, str, str, bool]] = [
    ("nav.dashboard", "/", "dashboard", False),
    ("nav.documents", "/documents", "description", False),
    ("nav.search", "/search", "search", False),
    ("nav.chat", "/chat", "forum", False),
    ("nav.sources", "/sources", "folder", False),
    ("nav.compare", "/compare", "compare_arrows", True),
    ("nav.tags", "/tags", "label", False),
    ("nav.backup", "/backup", "save", True),
    ("nav.settings", "/settings", "settings", False),
    ("nav.help", "/help", "help_outline", False),
    ("nav.diagnostics", "/diagnostics", "monitor_heart", True),
    ("nav.logs", "/logs", "article", True),
    ("nav.about", "/about", "info", True),
]

EXPERT_MODE_KEY = "expert_mode"


def _expert_mode() -> bool:
    """Whether the drawer shows advanced/occasional menu items (Expert mode).

    Stored per-browser in ``app.storage.user`` (like the dismissed-update and
    remember-me prefs) rather than the DB ``UserSetting`` row — this keeps the
    feature migration-free for existing installs, since startup only runs
    ``create_all`` and would not add a new column to an existing table.
    Defaults to Basic (False) so the menu stays uncluttered out of the box.
    """
    try:
        return bool(nicegui_app.storage.user.get(EXPERT_MODE_KEY, False))
    except Exception:
        return False


def _layout(user: User, current: str) -> bool:
    """Build the modern app shell: header + collapsible drawer + page area.

    Returns ``True`` once the shell is built, or ``False`` if the page was gated
    and redirected — callers MUST do ``if not _layout(...): return`` so no page
    body (DB queries, document text) is built/serialized for an unlicensed user.

    The header sticks to the top and carries the brand mark, a hamburger to
    collapse the drawer, and a quick-actions area on the right (current page
    label, theme/lang shortcuts, user menu). The drawer is a glass panel that
    can slide off-screen via the hamburger toggle.
    """
    # Whole-app license gate (blocking). The /activate page builds its own
    # minimal shell and never calls _layout, so there is no redirect loop;
    # everything else is locked until a valid key is entered. Licensed users
    # pass instantly (one local verify).
    if not licensing.is_activated():
        ui.navigate.to("/activate")
        return False

    import asyncio

    _apply_theme(_user_theme(user))
    lang = _user_lang(user)
    try:
        asyncio.create_task(_refresh_update_state())
    except RuntimeError:
        pass

    # --- Header ----------------------------------------------------------
    page_title = next((t(k, lang) for k, p, _, _ in NAV_ITEMS if p == current), __app_name__)

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

        # Command palette trigger (Ctrl/⌘+K) — late-bound to _open_palette,
        # which is defined after the drawer (so its closures already exist).
        with ui.element("div").classes("ldi-cmdk-trigger q-ml-md gt-xs").on("click", lambda: _open_palette()):
            ui.icon("search").style("font-size: 16px;")
            ui.label(t("common.search", lang))
            ui.html('<span class="ldi-kbd">Ctrl</span><span class="ldi-kbd">K</span>')

        ui.space()

        # Current page tag
        ui.label(page_title).classes("text-body2 opacity-80")

        # Live scan indicator — visible on every page while jobs are running.
        scan_indicator = ui.row().classes("items-center gap-2 q-ml-md")

        _scan_sig: dict[str, object] = {"v": None}

        def _refresh_scan_indicator() -> None:
            from app.models import ScanJob, ScanJobStatus

            with session_scope() as session:
                running = session.exec(select(ScanJob).where(ScanJob.status == ScanJobStatus.running)).all()
            # Tiny fingerprint of exactly what this indicator renders. The header
            # lives on every page, so without this guard every connected tab
            # tears down + rebuilds the row (and its websocket diff) every 3s
            # forever, even while idle. Skip when nothing visible changed.
            sig = tuple(
                (j.id, j.processed_files, max(j.total_files or 0, j.processed_files)) for j in running
            )
            if sig == _scan_sig["v"]:
                return
            _scan_sig["v"] = sig

            scan_indicator.clear()
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
            # Cycle through System + restrained themes first, then bold/legacy.
            # fresh=True: the client-pinned prefs cache holds the page-build
            # theme; another tab (or a double-click racing the reload) may have
            # advanced it since — cycling from a stale value would skip/undo.
            order = THEME_ORDER
            cur = _user_prefs(user, fresh=True)[0]
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else DEFAULT_THEME
            with session_scope() as session:
                us = session.exec(select(UserSetting).where(UserSetting.user_id == user.id)).first()
                if not us:
                    us = UserSetting(user_id=user.id)
                us.theme = nxt
                session.add(us)
            try:
                ui.context.client._ldi_prefs = None
            except Exception:
                pass
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
            ui.label("MENU").classes("text-caption opacity-50 q-px-sm").style("letter-spacing: 0.12em;")

        expert_on = _expert_mode()
        for key, path, icon, expert_only in NAV_ITEMS:
            if expert_only and not expert_on:
                continue
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

        # --- Basic / Expert mode toggle ---------------------------------
        # Flips the per-browser preference and reloads so the nav re-renders
        # with the advanced items shown/hidden. Kept in the drawer so it's
        # always reachable regardless of which items are currently visible.
        def _set_expert(value: bool) -> None:
            try:
                nicegui_app.storage.user[EXPERT_MODE_KEY] = bool(value)
            except Exception as e:
                logger.warning("expert-mode toggle failed: {}", e)
            ui.navigate.reload()

        with ui.row().classes("items-center gap-2 q-px-sm no-wrap"):
            ui.icon("tune").classes("ldi-accent")
            ui.switch(
                t("nav.expert_mode", lang),
                value=expert_on,
                on_change=lambda e: _set_expert(e.value),
            ).props("dense").tooltip(t("nav.expert_hint", lang))

        ui.separator().classes("q-my-md")
        with (ui.row().classes("items-center gap-2 q-px-sm"),):
            ui.icon("verified_user").classes("ldi-accent")
            ui.label(user.username).classes("text-body2 flex-1")
            ui.label(user.role.value).classes("text-caption opacity-60")

    # Wire up the hamburger now that the drawer exists
    hamburger.on("click", lambda: drawer.toggle())

    # --- Command palette (Ctrl/⌘+K) -------------------------------------
    # A keyboard-first launcher over every route + a few global actions.
    # Built once per page; both the header trigger and the global key handler
    # call _open_palette(). Open state is read straight off _palette.value so
    # backdrop-clicks stay in sync (no separate flag to drift).
    _cmd_state: dict[str, Any] = {"idx": 0, "filtered": []}

    _commands: list[dict[str, Any]] = []
    for _k, _p, _ic, _exp in NAV_ITEMS:
        _commands.append(
            {
                "label": t(_k, lang),
                "icon": _ic,
                "hint": _p,
                "group": "nav",
                "run": (lambda p=_p: ui.navigate.to(p)),
            }
        )
    for _akey, _aic, _afn in (
        ("cmd.act_new_chat", "forum", lambda: ui.navigate.to("/chat")),
        ("cmd.act_search", "search", lambda: ui.navigate.to("/search")),
        ("cmd.act_theme", "palette", _cycle_theme),
        ("cmd.act_expert", "tune", lambda: _set_expert(not _expert_mode())),
        ("cmd.act_reload", "refresh", lambda: ui.navigate.reload()),
        ("cmd.act_logout", "logout", _do_logout),
    ):
        _commands.append({"label": t(_akey, lang), "icon": _aic, "hint": "", "group": "actions", "run": _afn})

    _cmd_group_labels = {"nav": t("cmd.group_nav", lang), "actions": t("cmd.group_actions", lang)}

    with ui.dialog().props("position=top") as _palette, ui.card().classes("ldi-cmdk ldi-static"):
        with ui.element("div").classes("ldi-cmdk-search"):
            ui.icon("search").classes("ldi-cmdk-icon")
            _cmd_search = (
                ui.input(placeholder=t("cmd.placeholder", lang))
                .props("borderless autofocus")
                .classes("w-full")
            )
        _cmd_list = ui.element("div").classes("ldi-cmdk-list")
        with ui.element("div").classes("ldi-cmdk-footer"):
            ui.html('<span class="ldi-kbd">↑</span><span class="ldi-kbd">↓</span>')
            ui.label(t("cmd.foot_nav", lang))
            ui.html('<span class="ldi-kbd">↵</span>')
            ui.label(t("cmd.foot_select", lang))
            ui.html('<span class="ldi-kbd">esc</span>')
            ui.label(t("cmd.foot_close", lang))

    def _cmd_run(idx: int) -> None:
        items = _cmd_state["filtered"]
        if 0 <= idx < len(items):
            cmd = items[idx]
            _palette.close()
            cmd["run"]()

    def _cmd_render() -> None:
        _cmd_list.clear()
        with _cmd_list:
            items = _cmd_state["filtered"]
            if not items:
                ui.label(t("cmd.no_match", lang)).classes("ldi-cmdk-empty")
                return
            last_group = None
            for i, cmd in enumerate(items):
                if cmd["group"] != last_group:
                    ui.label(_cmd_group_labels.get(cmd["group"], "")).classes("ldi-cmdk-group")
                    last_group = cmd["group"]
                row = ui.element("div").classes(
                    "ldi-cmdk-item" + (" active" if i == _cmd_state["idx"] else "")
                )
                with row:
                    ui.icon(cmd["icon"]).classes("ldi-cmdk-icon")
                    ui.label(cmd["label"]).classes("ldi-cmdk-label")
                    if cmd["hint"]:
                        ui.label(cmd["hint"]).classes("ldi-cmdk-hint")
                row.on("click", lambda _e, idx=i: _cmd_run(idx))

    def _cmd_filter() -> None:
        qv = (_cmd_search.value or "").strip().lower()
        if not qv:
            _cmd_state["filtered"] = list(_commands)
        else:
            _cmd_state["filtered"] = [
                c for c in _commands if qv in c["label"].lower() or qv in str(c["hint"]).lower()
            ]
        _cmd_state["idx"] = 0
        _cmd_render()

    def _cmd_move(delta: int) -> None:
        items = _cmd_state["filtered"]
        if items:
            _cmd_state["idx"] = (_cmd_state["idx"] + delta) % len(items)
            _cmd_render()

    def _open_palette() -> None:
        _cmd_search.value = ""
        _cmd_state["filtered"] = list(_commands)
        _cmd_state["idx"] = 0
        _cmd_render()
        _palette.open()
        ui.timer(0.05, lambda: _cmd_search.run_method("focus"), once=True)

    _cmd_search.on_value_change(lambda _e: _cmd_filter())

    # Ctrl/⌘+K is captured in the BROWSER and forwarded as one custom event.
    # The previous ui.keyboard(ignore=[]) handler shipped every keydown AND
    # keyup from every input on every page to the server — two websocket
    # round-trips per keystroke app-wide, i.e. visible typing lag. Navigation
    # keys are bound to the palette's search input instead (it autofocuses, so
    # all typing lands there while the dialog is open).
    ui.add_head_html(
        "<script>"
        "window.addEventListener('keydown', function(e) {"
        "  if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'k' || e.key === 'K')) {"
        "    e.preventDefault();"
        "    if (window.emitEvent) emitEvent('ldi_cmdk');"
        "  }"
        "});"
        "</script>"
    )
    ui.on("ldi_cmdk", lambda: _open_palette())
    _cmd_search.on("keydown.down.prevent", lambda: _cmd_move(1))
    _cmd_search.on("keydown.up.prevent", lambda: _cmd_move(-1))
    _cmd_search.on("keydown.enter", lambda: _cmd_run(_cmd_state["idx"]))
    _cmd_search.on("keydown.escape", lambda: _palette.close())
    # Clicking dialog chrome (group labels, footer, padding) blurs the input
    # and would leave arrow/Enter navigation dead — refocus on any click.
    _palette.on("click", lambda: _cmd_search.run_method("focus"))

    # --- Update banner sits at the top of the page area ------------------
    _render_update_banner(lang)
    return True


def _do_logout() -> None:
    # Route through the /logout PAGE so the teardown runs during a page build,
    # where app.storage.browser (the bridged API session cookie) is writable —
    # clearing it in this click handler would silently no-op and leave the REST
    # API authenticated after "logout".
    ui.navigate.to("/logout")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def register_ui(fastapi_app: FastAPI) -> None:
    # Bundled fonts (Inter / JetBrains Mono woff2) — replaces the Google Fonts
    # @import so first paint never waits on an external fetch (offline-safe).
    from pathlib import Path as _Path

    _static_dir = _Path(__file__).parent / "static"
    if _static_dir.is_dir():
        nicegui_app.add_static_files("/ldi-static", str(_static_dir))

    @fastapi_app.get("/ldi/theme/{name}.css", include_in_schema=False)
    def theme_css(name: str):  # type: ignore[no-untyped-def]
        from fastapi import Response

        if name != "system" and name not in THEMES:
            from fastapi import HTTPException

            raise HTTPException(status_code=404)
        return Response(
            build_global_css(name),
            media_type="text/css",
            # Immutable per app version — the <link> URL carries ?v=__version__,
            # so updates bust the cache by changing the URL, never by expiry.
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @ui.page("/login")
    def page_login() -> None:
        _apply_theme(DEFAULT_THEME)
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
        # SECURITY: we never store/auto-fill the password and never auto-login.
        # The session cookie already keeps you signed in between visits; "remember
        # me" only pre-fills the USERNAME on THIS browser (app.storage.user is
        # per-browser). A fresh/WAN client inherits nothing. Scrub any legacy
        # encrypted credential blob (older builds stored the cleartext password).
        try:
            delete_secret(REMEMBER_SECRET_NAME)
        except Exception:
            pass
        try:
            remembered_user = str(nicegui_app.storage.user.get(REMEMBER_USERNAME_KEY, "") or "")
        except Exception:
            remembered_user = ""

        with (
            ui.column().classes("w-full items-center justify-center p-4").style("min-height: 100vh"),
            ui.card().classes("ldi-static w-full max-w-sm p-6"),
        ):
            ui.label(t("login.title", ph_lang)).classes("text-h5 q-mb-md ldi-primary")
            username = ui.input(t("common.username", ph_lang), value=remembered_user).classes("w-full")
            # The password is NEVER pre-filled — it would sit in the DOM for anyone
            # who opens /login on a shared/exposed instance.
            password = ui.input(
                t("common.password", ph_lang),
                password=True,
                password_toggle_button=True,
            ).classes("w-full")
            remember = ui.checkbox(
                "Remember my username on this device",
                value=bool(remembered_user),
            ).classes("q-mt-sm")
            err = ui.label("").classes("text-negative q-mt-sm")

            def _login() -> None:
                # Brute-force protection: the UI login runs in-process (it never
                # hits /api/auth/login), so it must apply the SAME rate limiter —
                # otherwise a WAN-exposed instance has an unthrottled guess path.
                from nicegui import context

                from app.auth.rate_limit import is_locked, record_failure, record_success

                ip = getattr(getattr(context, "client", None), "ip", None) or "ui"
                uname = username.value or ""
                locked, retry = is_locked(ip, uname)
                if locked:
                    err.text = t("login.locked", ph_lang).format(s=int(retry))
                    return
                with session_scope() as session:
                    u = session.exec(select(User).where(User.username == uname)).first()
                    if not u or not u.is_active or not verify_password(password.value, u.password_hash):
                        was_locked, lockout = record_failure(ip, uname)
                        err.text = (
                            t("login.locked", ph_lang).format(s=int(lockout))
                            if was_locked
                            else t("login.invalid", ph_lang)
                        )
                        return
                    record_success(ip, uname)
                    nicegui_app.storage.user[SESSION_USER_KEY] = u.id
                    nicegui_app.storage.user["pwv"] = session_fingerprint(u)
                # Remember ONLY the username on this browser — never the password.
                try:
                    if remember.value:
                        nicegui_app.storage.user[REMEMBER_USERNAME_KEY] = uname
                    else:
                        nicegui_app.storage.user.pop(REMEMBER_USERNAME_KEY, None)
                except Exception as e:
                    logger.warning("login: remember-username failed: {}", e)
                ui.navigate.to("/")

            ui.button(t("btn.login", ph_lang), on_click=_login).props("color=primary").classes(
                "w-full q-mt-md"
            )
            password.on("keydown.enter", lambda _: _login())
            ui.link(t("login.forgot", ph_lang), "/recover").classes("text-caption q-mt-sm")

    @ui.page("/logout")
    def page_logout() -> None:
        # Full sign-out — runs during a page build so app.storage.browser (the
        # REST API's session cookie) is writable. The old UI logout cleared only
        # app.storage.user, leaving the API authenticated.
        try:
            nicegui_app.storage.user.pop(SESSION_USER_KEY, None)
            nicegui_app.storage.user.pop(REMEMBER_USERNAME_KEY, None)
        except Exception as e:
            logger.warning("logout: clearing ui session failed: {}", e)
        try:
            nicegui_app.storage.browser.pop(SESSION_USER_KEY, None)
        except Exception as e:
            logger.warning("logout: clearing api session failed: {}", e)
        ui.navigate.to("/login")

    @ui.page("/activate")
    def page_activate() -> None:
        user = _require_login()
        if not user:
            return
        _apply_theme(_user_theme(user))
        lang = _user_lang(user)
        status = licensing.current_status()

        with (
            ui.column().classes("w-full items-center justify-center p-4").style("min-height: 100vh"),
            ui.card().classes("ldi-static w-full max-w-md p-6"),
        ):
            with ui.row().classes("items-center gap-2"):
                ui.icon("workspace_premium").classes("ldi-accent text-3xl")
                ui.label(t("license.activate_title", lang)).classes("text-h5 ldi-primary")
            ui.label(t("license.activate_intro", lang)).classes("text-caption opacity-80")

            _render_license_summary(status, lang)

            if status.active:
                ui.button(
                    t("license.go_dashboard", lang),
                    icon="arrow_forward",
                    on_click=lambda: ui.navigate.to("/"),
                ).props("color=primary").classes("w-full q-mt-md")

            key_in = (
                ui.textarea(t("license.key_label", lang))
                .props("outlined autogrow")
                .classes("w-full q-mt-sm")
                .style("font-family: 'JetBrains Mono', monospace;")
            )
            msg = ui.label("").classes("text-caption q-mt-xs")

            def _do_activate() -> None:
                st = licensing.activate(key_in.value or "")
                if st.active:
                    ui.notify(t("license.activated", lang), color="positive")
                    ui.navigate.to("/")
                    return
                err_key = (
                    "license.err_empty"
                    if st.reason == "none"
                    else "license.err_expired" if st.reason == "expired" else "license.err_invalid"
                )
                msg.text = t(err_key, lang)
                msg.classes(replace="text-caption q-mt-xs text-negative")

            ui.button(t("license.activate_btn", lang), icon="lock_open", on_click=_do_activate).props(
                "color=primary"
            ).classes("w-full q-mt-md")

            ui.separator().classes("q-my-md")
            ui.label(t("license.how_to_buy", lang)).classes("text-caption opacity-70")
            ui.link(t("license.contact", lang), f"mailto:{__contact__}").classes("text-caption")
            ui.button(t("btn.logout", lang), icon="logout", on_click=_do_logout).props("flat").classes(
                "q-mt-sm"
            )

    @ui.page("/first-run")
    def page_first_run() -> None:
        _apply_theme(DEFAULT_THEME)
        with session_scope() as session:
            if has_users(session):
                ui.navigate.to("/login")
                return
        wl = "en"
        s0 = get_settings()
        state: dict[str, Any] = {
            "user_id": None,
            "ping_ok": False,
            "models": [],
            "embed_ok": False,
            "embed_skipped": False,
            "source_id": None,
            "scan_started": False,
        }

        def _client() -> LMStudioClient:
            return LMStudioClient(base_url=(lm_url.value or s0.lmstudio_base_url))

        with (
            ui.column().classes("w-full items-center justify-center p-4").style("min-height: 100vh"),
            ui.card().classes("ldi-static w-full max-w-[640px] p-6"),
        ):
            ui.label(t("setup.welcome", wl)).classes("text-h5 ldi-primary")
            ui.label(t("setup.intro", wl)).classes("q-mb-md opacity-80")

            with ui.stepper().props("vertical").classes("w-full") as stepper:
                # 1) Prerequisites --------------------------------------------
                with ui.step(t("setup.prereq_title", wl)):
                    ui.markdown(t("setup.prereq_body", wl)).classes("text-body2")
                    ui.link(t("setup.open_help", wl), "/help", new_tab=True).classes("text-caption")
                    with ui.stepper_navigation():
                        ui.button(t("setup.get_started", wl), on_click=stepper.next).props("color=primary")

                # 2) Account --------------------------------------------------
                with ui.step(t("setup.step_account", wl)):
                    ui.label(t("setup.account_intro", wl)).classes("text-caption opacity-80")
                    username = ui.input(t("wizard.admin_user", wl), value="admin").classes("w-full")
                    password = ui.input(
                        t("common.password", wl), password=True, password_toggle_button=True
                    ).classes("w-full")
                    confirm = ui.input(
                        t("common.confirm_password", wl), password=True, password_toggle_button=True
                    ).classes("w-full")
                    acct_err = ui.label("").classes("text-negative text-caption")
                    recovery_box = ui.label("").classes("text-positive break-words q-mt-sm")
                    ack = ui.checkbox(t("setup.recovery_ack", wl))
                    ack.set_visibility(False)
                    with ui.stepper_navigation():
                        create_btn = ui.button(t("setup.create_account", wl)).props("color=primary")
                        acct_next = ui.button(t("setup.next", wl), on_click=stepper.next).props(
                            "color=primary"
                        )
                    acct_next.set_visibility(False)

                    def _make_account() -> None:
                        if not username.value or not password.value:
                            acct_err.text = t("wizard.user_pw_required", wl)
                            return
                        if password.value != confirm.value:
                            acct_err.text = t("wizard.pw_mismatch", wl)
                            return
                        if state["user_id"]:
                            return
                        acct_err.text = ""
                        with session_scope() as session:
                            u = create_user(session, username=username.value, password=password.value)
                            rk = make_recovery_key()
                            u.recovery_key_hash = hash_password(rk)
                            session.add(u)
                            session.flush()
                            state["user_id"] = u.id
                            nicegui_app.storage.user[SESSION_USER_KEY] = u.id
                        recovery_box.text = f"{t('wizard.recovery_note', wl)} {rk}"
                        username.disable()
                        password.disable()
                        confirm.disable()
                        create_btn.set_visibility(False)
                        ack.set_visibility(True)

                    create_btn.on("click", _make_account)
                    ack.on_value_change(lambda: acct_next.set_visibility(bool(ack.value)))

                # 3) Connect --------------------------------------------------
                with ui.step(t("setup.step_connect", wl)):
                    ui.label(t("setup.connect_intro", wl)).classes("text-caption opacity-80")
                    lm_url = ui.input(t("wizard.lm_url", wl), value=s0.lmstudio_base_url).classes("w-full")
                    conn_status = ui.label("").classes("text-caption q-mt-sm")
                    with ui.stepper_navigation():
                        ui.button(t("setup.back", wl), on_click=stepper.previous).props("flat")
                        test_btn = ui.button(t("setup.test_connection", wl), icon="cable").props(
                            "color=primary"
                        )
                        conn_next = ui.button(t("setup.next", wl), on_click=stepper.next).props(
                            "color=primary"
                        )
                        ui.button(t("setup.skip_for_now", wl), on_click=stepper.next).props("flat")
                    conn_next.set_visibility(False)

                    async def _test_conn() -> None:
                        save_user_settings({"lmstudio_base_url": lm_url.value or "http://localhost:1234/v1"})
                        get_settings.cache_clear()
                        reset_client_cache()
                        conn_status.text = t("setup.testing", wl)
                        conn_status.classes(replace="text-caption q-mt-sm opacity-80")
                        test_btn.disable()
                        try:
                            c = _client()
                            ok = await c.ping()
                            state["ping_ok"] = ok
                            if ok:
                                state["models"] = await c.list_models()
                                conn_status.text = t("setup.connect_ok", wl).format(n=len(state["models"]))
                                conn_status.classes(replace="text-caption q-mt-sm text-positive")
                                conn_next.set_visibility(True)
                            else:
                                conn_status.text = t("setup.connect_fail", wl)
                                conn_status.classes(replace="text-caption q-mt-sm text-negative")
                                conn_next.set_visibility(False)
                                state["models"] = []
                        finally:
                            test_btn.enable()

                    test_btn.on("click", _test_conn)

                # 4) Models ---------------------------------------------------
                with ui.step(t("setup.step_models", wl)):
                    ui.label(t("setup.models_intro", wl)).classes("text-caption opacity-80")
                    # new_value_mode lets users type a model id even when the
                    # loaded-model list is empty (e.g. they skipped the connect step).
                    chat_sel = ui.select(
                        [], label=t("setup.chat_model", wl), with_input=True, new_value_mode="add-unique"
                    ).classes("w-full")
                    emb_sel = ui.select(
                        [], label=t("setup.embedding_model", wl), with_input=True, new_value_mode="add-unique"
                    ).classes("w-full")
                    vis_sel = ui.select(
                        [], label=t("setup.vision_model", wl), with_input=True, new_value_mode="add-unique"
                    ).classes("w-full")
                    model_status = ui.label("").classes("text-caption q-mt-sm")
                    fix_box = ui.column().classes("w-full q-mt-xs")
                    fix_box.set_visibility(False)
                    with ui.stepper_navigation():
                        ui.button(t("setup.back", wl), on_click=stepper.previous).props("flat")
                        auto_btn = ui.button(t("setup.auto_pick", wl), icon="auto_fix_high").props(
                            "color=primary"
                        )
                        validate_btn = ui.button(t("setup.validate_embedding", wl), icon="psychology").props(
                            "flat"
                        )
                        models_next = ui.button(t("setup.next", wl), on_click=stepper.next).props(
                            "color=primary"
                        )
                        skip_btn = ui.button(t("setup.embedding_skip", wl)).props("flat")
                    models_next.set_visibility(False)
                    # The "Skip anyway" escape is always available on this step so
                    # the user can never get stuck (e.g. no embedding model loaded).
                    skip_btn.set_visibility(True)

                    def _option_ids() -> list[str]:
                        out: list[str] = []
                        for m in state["models"]:
                            if isinstance(m, dict):
                                mid = m.get("id") or m.get("model")
                                if mid:
                                    out.append(mid)
                        return out

                    async def _validate_emb() -> None:
                        updates: dict[str, Any] = {}
                        if chat_sel.value:
                            updates["chat_model"] = chat_sel.value
                        if vis_sel.value:
                            updates["vision_model"] = vis_sel.value
                        emb = (emb_sel.value or "").strip()
                        if emb:
                            updates["embedding_model"] = emb
                        if updates:
                            save_user_settings(updates)
                            get_settings.cache_clear()
                            reset_client_cache()
                        fix_box.clear()
                        # Reset each attempt: keep the skip escape, hide Next until valid.
                        models_next.set_visibility(False)
                        skip_btn.set_visibility(True)
                        if not emb:
                            model_status.text = t("setup.embedding_fail", wl)
                            model_status.classes(replace="text-caption q-mt-sm text-negative")
                            return
                        ok, msg = await _client().preflight_embed(model=emb)
                        state["embed_ok"] = ok
                        if ok:
                            model_status.text = "✓ " + t("setup.embedding_ok", wl)
                            model_status.classes(replace="text-caption q-mt-sm text-positive")
                            fix_box.set_visibility(False)
                            models_next.set_visibility(True)
                            skip_btn.set_visibility(False)
                        else:
                            model_status.text = "✗ " + t("setup.embedding_fail", wl)
                            model_status.classes(replace="text-caption q-mt-sm text-negative")
                            fix_box.set_visibility(True)
                            with fix_box:
                                ui.label(t("setup.embedding_fix", wl)).classes("text-caption opacity-80")
                                ui.code(msg, language="text").classes("w-full")
                            skip_btn.set_visibility(True)

                    async def _auto_pick() -> None:
                        if not state["models"]:
                            try:
                                state["models"] = await _client().list_models()
                            except Exception:
                                state["models"] = []
                        ids = _option_ids()
                        for sel in (chat_sel, emb_sel, vis_sel):
                            sel.set_options(ids)
                        chat = emb = vis = ""
                        for mid in ids:
                            role = _classify_model(mid)
                            if role == "embedding" and not emb:
                                emb = mid
                            elif role == "vision" and not vis:
                                vis = mid
                            elif role == "chat" and not chat:
                                chat = mid
                        if chat:
                            chat_sel.set_value(chat)
                        if emb:
                            emb_sel.set_value(emb)
                        if vis:
                            vis_sel.set_value(vis)
                        await _validate_emb()

                    def _skip_emb() -> None:
                        state["embed_skipped"] = True
                        ui.notify(t("setup.embedding_skip_warn", wl), color="warning")
                        stepper.next()

                    auto_btn.on("click", _auto_pick)
                    validate_btn.on("click", _validate_emb)
                    skip_btn.on("click", _skip_emb)

                # 5) Performance ----------------------------------------------
                with ui.step(t("setup.step_hardware", wl)):
                    ui.label(t("setup.hardware_intro", wl)).classes("text-caption opacity-80")
                    hw = detect_hardware()
                    ui.label(
                        f"{t('setup.detected', wl)}: {hw.physical_cores} cores · "
                        f"{hw.total_ram_gb:.0f} GB RAM · GPU: {hw.gpu or '—'}"
                    ).classes("text-caption q-mt-xs opacity-80")
                    prof = ui.select(
                        {"auto": "Auto", "low": "Low", "balanced": "Balanced", "high": "High"},
                        value=(s0.performance_profile or "auto"),
                        label=t("setup.perf_profile", wl),
                    ).classes("w-full")

                    def _save_profile() -> None:
                        save_user_settings({"performance_profile": prof.value or "auto"})
                        get_settings.cache_clear()
                        stepper.next()

                    with ui.stepper_navigation():
                        ui.button(t("setup.back", wl), on_click=stepper.previous).props("flat")
                        ui.button(t("setup.next", wl), on_click=_save_profile).props("color=primary")

                # 6) Folder + first action ------------------------------------
                with ui.step(t("setup.step_folder", wl)):
                    ui.label(t("setup.folder_intro", wl)).classes("text-caption opacity-80")
                    folder = ui.input(t("wizard.initial_folder", wl)).classes("w-full")
                    action = ui.radio(
                        {
                            "quick": t("setup.action_quick", wl),
                            "full": t("setup.action_full", wl),
                            "skip": t("setup.action_skip", wl),
                        },
                        value="quick",
                    ).classes("q-mt-sm")

                    def _finish_folder() -> None:
                        if folder.value:
                            with session_scope() as session:
                                src = DocumentSource(
                                    name="Initial folder",
                                    type=SourceType.local,
                                    path=folder.value,
                                    owner_id=state["user_id"],
                                )
                                session.add(src)
                                session.flush()
                                state["source_id"] = src.id
                        act = action.value
                        if state["embed_skipped"] and act == "full":
                            act = "quick"
                        if state["source_id"] and act != "skip":
                            start_scan_in_background(state["source_id"], phase=act)
                            state["scan_started"] = True
                        done_msg.text = (
                            t("setup.done_scan_started", wl)
                            if state["scan_started"]
                            else t("setup.done_no_scan", wl)
                        )
                        stepper.next()

                    with ui.stepper_navigation():
                        ui.button(t("setup.back", wl), on_click=stepper.previous).props("flat")
                        ui.button(t("setup.next", wl), on_click=_finish_folder).props("color=primary")

                # 7) Done -----------------------------------------------------
                with ui.step(t("setup.step_done", wl)):
                    ui.label(t("setup.done_title", wl)).classes("text-h6 ldi-primary")
                    done_msg = ui.label(t("setup.done_no_scan", wl)).classes(
                        "text-caption opacity-80 q-mt-xs"
                    )
                    with ui.stepper_navigation():
                        ui.button(
                            t("setup.go_dashboard", wl),
                            icon="dashboard",
                            on_click=lambda: ui.navigate.to("/"),
                        ).props("color=primary")

    @ui.page("/help")
    def page_help() -> None:
        user = _require_login()
        if not user:
            return
        if not _layout(user, "/help"):
            return
        lang = _user_lang(user)
        page_header("help.title", lang)
        sections = [
            ("help.prereq_title", "setup.prereq_body", "rocket_launch"),
            ("help.models_title", "help.models_body", "memory"),
            ("help.scanning_title", "help.scanning_body", "document_scanner"),
            ("help.search_title", "help.search_body", "search"),
            ("help.backup_title", "help.backup_body", "save"),
            ("help.trouble_title", "help.trouble_body", "healing"),
        ]
        for title_key, body_key, icon in sections:
            with section_card(lang, title_key=title_key, icon=icon, extra="q-mb-md"):
                ui.markdown(t(body_key, lang)).classes("text-body2")

    @ui.page("/recover")
    def page_recover() -> None:
        _apply_theme(DEFAULT_THEME)
        rl = "en"
        with (
            ui.column().classes("w-full items-center justify-center p-4").style("min-height: 100vh"),
            ui.card().classes("ldi-static w-full max-w-sm p-6"),
        ):
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
        if not _layout(user, "/"):
            return
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
        page_header("dash.title", lang)
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
        # ----- Getting started (only while the library is empty) -----
        if doc_count == 0:
            with section_card(lang, title_key="dash.gs_title", icon="rocket_launch", extra="q-mt-md"):
                ui.label(t("dash.gs_intro", lang)).classes("text-caption opacity-80 q-mb-sm")
                gs_steps = [
                    ("folder_open", "dash.gs_add_source", "/sources", source_count > 0),
                    ("document_scanner", "dash.gs_run_scan", "/sources", False),
                    ("search", "dash.gs_ask", "/search", False),
                ]
                for s_icon, s_key, s_route, s_done in gs_steps:
                    with ui.row().classes("items-center gap-2 w-full q-py-xs"):
                        ui.icon("check_circle" if s_done else "radio_button_unchecked").classes(
                            "ldi-accent" if s_done else "opacity-40"
                        )
                        ui.label(t(s_key, lang)).classes("flex-1")
                        ui.button(icon=s_icon, on_click=lambda r=s_route: ui.navigate.to(r)).props(
                            "flat dense round"
                        )
                ui.button(
                    t("dash.gs_help", lang), icon="help_outline", on_click=lambda: ui.navigate.to("/help")
                ).props("flat").classes("q-mt-sm")
        # ----- Active scans (live) -----
        from app.models import ScanJob, ScanJobStatus

        active_card = ui.card().classes("w-full p-3 q-mt-md")
        _active_sig: dict[str, object] = {"v": None}

        def _refresh_active() -> None:
            with session_scope() as session:
                running = session.exec(select(ScanJob).where(ScanJob.status == ScanJobStatus.running)).all()
                paused = session.exec(select(ScanJob).where(ScanJob.status == ScanJobStatus.paused)).all()
            # Skip the teardown/rebuild (and its 3s flicker + relayout) when
            # nothing the card shows has changed.
            sig = tuple(
                (j.id, j.status, j.processed_files, j.total_files, j.current_file, j.error_count)
                for j in running + paused
            )
            if sig == _active_sig["v"]:
                return
            _active_sig["v"] = sig

            active_card.clear()
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
                            status_pill(j.status, lang)
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
        if not _layout(user, "/sources"):
            return
        lang = _user_lang(user)
        page_header("sources.title", lang)

        from app.models import ScanJob, ScanJobStatus

        def _latest_job_for(session, source_id: int) -> ScanJob | None:
            # Prefer the live, controllable job (running/paused) over a job queued
            # behind it or finished history, so the row's progress + Stop button
            # target the scan that's actually executing.
            live = session.exec(
                select(ScanJob)
                .where(
                    ScanJob.source_id == source_id,
                    ScanJob.status.in_([ScanJobStatus.running, ScanJobStatus.paused]),  # type: ignore[attr-defined]
                )
                .order_by(ScanJob.id.desc())  # type: ignore
                .limit(1)
            ).first()
            if live is not None:
                return live
            return session.exec(
                select(ScanJob)
                .where(ScanJob.source_id == source_id)
                .order_by(ScanJob.id.desc())  # type: ignore
                .limit(1)
            ).first()

        def _status_pill(job: ScanJob | None):
            if job is None:
                return None
            status_pill(job.status, lang)

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

        def _sources_sig(session) -> tuple:
            """A lightweight fingerprint of everything the rows display, so the
            auto-refresh only rebuilds (tearing down any open scan dropdown) when
            something actually changed."""
            out = []
            for s in session.exec(select(DocumentSource).order_by(DocumentSource.id)).all():
                j = _latest_job_for(session, s.id)
                out.append(
                    (
                        s.id,
                        s.name,
                        s.path,
                        str(s.last_scan_at),
                        str(getattr(j, "status", None)),
                        getattr(j, "processed_files", 0) or 0,
                        getattr(j, "total_files", 0) or 0,
                        # current_file is None when idle (keeps the dropdown
                        # stable) but ticks during a scan — including it makes
                        # per-file/per-image progress refresh live.
                        getattr(j, "current_file", None),
                    )
                )
            return tuple(out)

        _last_sig: dict[str, object] = {"v": None}

        def _refresh(force: bool = True) -> None:
            from app.services.watcher import (
                is_watching as _is_watching,
            )
            from app.services.watcher import (
                start_watcher as _start_watch,
            )
            from app.services.watcher import (
                stop_watcher as _stop_watch,
            )

            with session_scope() as _sess:
                sig = _sources_sig(_sess)
            if not force and sig == _last_sig["v"]:
                return  # nothing changed — keep any open scan dropdown alive
            _last_sig["v"] = sig

            table_container.clear()
            with table_container, session_scope() as session:
                rows = session.exec(select(DocumentSource).order_by(DocumentSource.id)).all()
                if not rows:
                    empty_state("create_new_folder", "sources.empty_title", "sources.empty_hint", lang)
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
                                    if phase == "vision" and not (get_settings().vision_model or "").strip():
                                        # Vision phase only makes images searchable when a vision model
                                        # describes them — warn instead of silently indexing nothing.
                                        ui.notify(
                                            t("sources.vision_no_model", lang),
                                            color="warning",
                                            timeout=7000,
                                        )
                                    start_scan_in_background(sid, phase=phase)
                                    ui.notify(t(note_key, lang))
                                    _refresh()

                                def _force_rescan(sid: int, phase: str) -> None:
                                    if phase == "vision" and not (get_settings().vision_model or "").strip():
                                        ui.notify(
                                            t("sources.vision_no_model", lang), color="warning", timeout=7000
                                        )
                                    with ui.dialog() as fd, ui.card().classes("w-[440px] p-4"):
                                        ui.label(t("sources.force_rescan_title", lang)).classes(
                                            "text-h6 ldi-primary"
                                        )
                                        ui.label(t("sources.force_rescan_help", lang)).classes(
                                            "text-caption opacity-70"
                                        )

                                        def _go(sid=sid, phase=phase) -> None:
                                            kwargs = {"phase": phase, "force_ocr": True, "force_embed": True}
                                            if phase == "vision":
                                                kwargs["force_vision"] = True
                                            start_scan_in_background(sid, **kwargs)
                                            fd.close()
                                            ui.notify(t("sources.force_rescan_started", lang))
                                            _refresh()

                                        with ui.row().classes("justify-end gap-2 w-full q-mt-md"):
                                            ui.button(t("common.cancel", lang), on_click=fd.close).props(
                                                "flat"
                                            )
                                            ui.button(
                                                t("sources.force_rescan_btn", lang), on_click=_go
                                            ).props("color=warning")
                                    fd.open()

                                def _stop_scan(jid: int) -> None:
                                    abort_scan_job(jid)
                                    ui.notify(t("sources.scan_stopping", lang))
                                    _refresh()

                                if is_active and job is not None and job.id is not None:
                                    _jid = job.id
                                    ui.button(
                                        icon="stop",
                                        on_click=lambda jid=_jid: _stop_scan(jid),
                                    ).props(
                                        "flat dense round color=negative"
                                    ).tooltip(t("sources.stop", lang))

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
                                        ui.separator()
                                        ui.menu_item(
                                            t("sources.force_rescan_ocr", lang),
                                            on_click=lambda sid=s.id: _force_rescan(sid, "ocr"),
                                        )
                                        ui.menu_item(
                                            t("sources.force_rescan_vision", lang),
                                            on_click=lambda sid=s.id: _force_rescan(sid, "vision"),
                                        )
                                ui.button(
                                    icon="bolt",
                                    on_click=lambda sid=s.id: _start(sid, "quick", "sources.scan_started"),
                                ).props("flat dense round").tooltip(t("sources.phase_quick", lang))
                                ui.button(
                                    icon="text_fields",
                                    on_click=lambda sid=s.id: _start(sid, "ocr", "sources.ocr_started"),
                                ).props("flat dense round").tooltip(t("sources.phase_ocr", lang))
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

                async def _do() -> None:
                    import asyncio

                    from app.services.sources import delete_source_cascade

                    d.close()
                    # Cascade delete touches docs/chunks/vectors/jobs and uses the
                    # write lock — run it off the event loop so the UI never freezes.
                    try:
                        ok = await asyncio.to_thread(delete_source_cascade, sid)
                    except Exception as e:  # surface the failure instead of silently no-op'ing
                        ui.notify(f"{t('sources.delete_failed', lang)}: {e}", color="negative")
                        _refresh()
                        return
                    if ok:
                        ui.notify(t("sources.deleted", lang), color="positive")
                    else:
                        ui.notify(t("sources.delete_failed", lang), color="warning")
                    _refresh()

                with ui.row().classes("justify-end gap-2 w-full q-mt-md"):
                    ui.button(t("common.cancel", lang), on_click=d.close).props("flat")
                    ui.button(t("common.delete", lang), on_click=_do).props("color=negative")
            d.open()

        with ui.row().classes("items-center gap-2 q-mb-md"):
            ui.button(t("common.refresh", lang), icon="refresh", on_click=_refresh).props("flat dense")
            ui.label("Auto-refresh: 3 s").classes("text-caption opacity-60")

        _refresh()
        # Live progress poll — rebuilds only when source/scan state changed, so an
        # open scan dropdown isn't torn down every 3 s while you're using it.
        ui.timer(3.0, lambda: _refresh(force=False))

    @ui.page("/documents")
    def page_documents() -> None:
        user = _require_login()
        if not user:
            return
        if not _layout(user, "/documents"):
            return
        lang = _user_lang(user)
        page_header("docs.title", lang)

        async def _show_similar(did: int, fname: str) -> None:
            from app.services.similar import find_similar

            with ui.dialog() as dialog, ui.card().classes("w-[640px] p-4"):
                ui.label(f"{t('docs.similar_to', lang)} {fname}").classes("text-h6 ldi-primary")
                loading = ui.column().classes("w-full gap-2")
                with loading:
                    skeleton_list(4, lines=1)
                content = ui.column().classes("w-full gap-2")
                dialog.open()
                hits = await find_similar(did, top_k=15)
                loading.delete()
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

            with ui.dialog() as dialog, ui.card().classes("w-[680px] max-w-[92vw] p-4"):
                ui.label(f"{t('docs.summary_of', lang)} {fname}").classes("text-h6 ldi-primary")
                loading = ui.column().classes("w-full gap-2")
                with loading:
                    skeleton_list(1, lines=6)
                with ui.column().classes("ldi-prose w-full"):
                    md = ui.markdown("")
                dialog.open()
                text = await summarize_document(did)
                loading.delete()
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

        sort_state = {"by": "newest"}
        view_state = {"limit": 60}  # windowed render; grows via "Load more"
        _SOReq = {
            "newest": Document.id.desc(),
            "oldest": Document.id.asc(),
            "name_az": Document.filename.asc(),
            "name_za": Document.filename.desc(),
            "largest": Document.size_bytes.desc(),
            "smallest": Document.size_bytes.asc(),
        }

        def _refresh() -> None:
            _update_bulk_bar()
            results.clear()
            with results, session_scope() as session:
                stmt = select(Document)
                if q_input.value:
                    like = f"%{q_input.value}%"
                    stmt = stmt.where(Document.filename.like(like))  # type: ignore
                order = _SOReq.get(sort_state["by"], Document.id.desc())
                # Windowed render with "Load more": rebuilding hundreds of heavy
                # cards (thumbnail + tags + 6 buttons) on every sort/refresh
                # hitches a large library. Fetch one extra row to tell whether
                # more exist without a separate COUNT query.
                _limit = int(view_state["limit"])
                fetched = session.exec(stmt.order_by(order).limit(_limit + 1)).all()  # type: ignore
                has_more = len(fetched) > _limit
                docs_list = list(fetched[:_limit])
                if not docs_list and q_input.value:
                    # Filter matched nothing — not an empty library.
                    empty_state("search_off", "search.no_results", "search.no_results_hint", lang)
                elif not docs_list:
                    empty_state(
                        "folder_off",
                        "docs.empty_title",
                        "docs.empty_hint",
                        lang,
                        action_label_key="dash.gs_add_source",
                        on_action=lambda: ui.navigate.to("/sources"),
                    )
                # Batch-fetch tags for all shown docs in one query → chips per card.
                from app.models import DocumentTagLink as _DTL
                from app.models import Tag as _Tag

                _doc_ids = [d.id for d in docs_list if d.id is not None]
                tags_by_doc: dict[int, list[str]] = {}
                if _doc_ids:
                    for _did, _name in session.exec(
                        select(_DTL.document_id, _Tag.name)
                        .join(_Tag, _Tag.id == _DTL.tag_id)
                        .where(_DTL.document_id.in_(_doc_ids))
                    ).all():
                        tags_by_doc.setdefault(_did, []).append(_name)

                for d in docs_list:
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
                            thumb_url = media_image_url(d.id, 1)
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
                                # The caption already shows type + language, so
                                # drop lang:* from the chips to avoid duplication.
                                _dtags = [
                                    tg for tg in tags_by_doc.get(d.id, []) if not tg.startswith("lang:")
                                ]
                                if _dtags:
                                    with ui.element("div").classes("q-mt-xs"):
                                        render_tag_chips(_dtags, limit=6)
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
                                        t("common.download", lang),
                                        icon="download",
                                        on_click=lambda did=d.id: download_pdf(did),
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

                if has_more:

                    def _load_more() -> None:
                        view_state["limit"] = int(view_state["limit"]) + 60
                        _refresh()

                    with ui.row().classes("w-full justify-center q-mt-sm"):
                        ui.button(
                            t("docs.load_more", lang),
                            icon="expand_more",
                            on_click=_load_more,
                        ).props("flat dense")

        def _reset_and_refresh() -> None:
            view_state["limit"] = 60  # new filter/sort starts back at the top
            _refresh()

        with ui.row().classes("items-center gap-2"):
            q_input.on("change", lambda _: _reset_and_refresh())
            sort_sel = (
                ui.select(
                    {
                        "newest": t("docs.sort_newest", lang),
                        "oldest": t("docs.sort_oldest", lang),
                        "name_az": t("docs.sort_name_az", lang),
                        "name_za": t("docs.sort_name_za", lang),
                        "largest": t("docs.sort_largest", lang),
                        "smallest": t("docs.sort_smallest", lang),
                    },
                    value="newest",
                    label=t("docs.sort_by", lang),
                )
                .props("dense outlined")
                .classes("min-w-[170px]")
            )

            def _on_sort() -> None:
                sort_state["by"] = sort_sel.value or "newest"
                _reset_and_refresh()

            sort_sel.on("update:model-value", lambda _e: _on_sort())
            ui.button(t("common.refresh", lang), icon="refresh", on_click=_refresh).props("dense")

            def _select_all() -> None:
                with session_scope() as sess:
                    stmt = select(Document)
                    if q_input.value:
                        stmt = stmt.where(Document.filename.like(f"%{q_input.value}%"))  # type: ignore
                    for d in sess.exec(stmt.limit(200)).all():
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
        if not _layout(user, "/search"):
            return
        lang = _user_lang(user)

        # Deep-link params: ?tag=X (browse a tag, e.g. from the Tags page) and
        # ?q=… (pre-filled query). Captured before the input element shadows `q`.
        initial_tag = (tag or "").strip()
        initial_query = (q or "").strip()

        # When browsing a specific tag, show where we came from.
        if initial_tag:
            breadcrumbs([(t("nav.tags", lang), "/tags"), (f"#{initial_tag}", None)])
        page_header("search.title", lang)

        # Search bar — an inline Search button plus an overflow (⋯) menu for the
        # occasional actions (rerank, save, export), so the primary row stays
        # uncluttered. _go / _save_current / _export are bound lazily (defined
        # below), and the rerank checkbox lives in the menu (it doesn't dismiss
        # the menu, so it can be toggled in place).
        with ui.row().classes("w-full gap-2 items-center no-wrap"):
            q = ui.input(t("search.placeholder", lang), value=initial_query).classes("flex-1")
            q.props("autofocus dense outlined")
            ui.button(icon="search", on_click=lambda: _go()).props("unelevated color=primary").tooltip(
                t("search.go", lang)
            )
            with ui.button(icon="more_vert").props("flat dense round").tooltip(t("common.actions", lang)):
                with ui.menu():
                    with ui.element("div").classes("q-px-md q-py-sm"):
                        rerank_toggle = ui.checkbox(t("search.rerank", lang), value=False)
                    ui.separator()
                    ui.menu_item(t("search.save", lang), on_click=lambda: _save_current())
                    ui.menu_item(t("search.btn_csv", lang), on_click=lambda: _export("csv"))
                    ui.menu_item(t("search.btn_json", lang), on_click=lambda: _export("json"))

        async def _export(fmt: str) -> None:
            from app.services.exports import search_hits_to_csv, search_hits_to_json

            # Honour the same query + filters + rerank as the on-screen results.
            await _ensure_universe()
            hits = _filtered(100)
            if not hits:
                ui.notify(t("search.nothing_to_export", lang), color="warning")
                return
            payload = search_hits_to_csv(hits) if fmt == "csv" else search_hits_to_json(hits)
            ui.download(payload.encode("utf-8"), filename=f"search.{fmt}", media_type=f"text/{fmt}")

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

        result_summary = ui.label("").classes("text-caption opacity-70 q-mt-sm")

        # Active-filter chips (removable) shown above the results.
        chips_box = ui.row().classes("items-center gap-1 flex-wrap w-full q-mt-xs")

        # Cache the (unfiltered) result universe for the current query so toggling
        # a facet re-filters instantly without re-embedding the query.
        search_cache: dict[str, Any] = {"key": None, "universe": [], "facets": None, "browse": False}

        # Layout: result-driven facet sidebar (left) + results (right).
        with ui.row().classes("w-full gap-3 no-wrap q-mt-sm items-start"):
            with ui.column().classes("ldi-glass gap-2 q-pa-md").style("min-width: 250px; max-width: 280px;"):
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.icon("filter_list").classes("ldi-accent")
                    ui.label(t("search.filters", lang)).classes("text-h6 flex-1")
                    ui.button(icon="restart_alt", on_click=lambda: _clear_filters()).props(
                        "flat dense round size=sm"
                    ).tooltip(t("search.clear_filters", lang))
                ui.label(t("search.facets_hint", lang)).classes("text-caption opacity-50")
                facet_box = ui.column().classes("w-full gap-2 q-mt-xs")
            out = ui.column().classes("flex-1 gap-2 min-w-0")

        def _highlight(snippet: str, query: str) -> str:
            """Wrap each query word in <mark> tags. Returns HTML-safe string."""
            return highlight_terms(snippet, query)

        def _source_pill(source: str) -> str:
            colour = {
                "native_text": "ldi-pill",
                "ocr_text": "ldi-pill ldi-pill-warning",
                "image_description": "ldi-pill ldi-pill-success",
                "table": "ldi-pill ldi-pill-success",
            }.get(source, "ldi-pill")
            return f'<span class="{colour}">{source}</span>'

        DISPLAY_K = 15

        def _match_filters(h, meta, src, tg, dts) -> bool:
            m = meta.get(h.document_id)
            if m is None:
                return not (src or dts or tg)
            return (
                (not src or m["source_id"] in src)
                and (not dts or m["doc_type"] in dts)
                and (not tg or bool(tg & m["tags"]))  # any selected tag present
            )

        async def _ensure_universe() -> None:
            """Fetch the unfiltered result set for the current query (or a library
            browse when there's no query) and compute its facets, caching by
            (query, rerank) so facet toggles don't re-embed."""
            import asyncio as _aio

            from app.services.search_service import (
                browse_documents,
                document_facets,
                hybrid_search,
            )

            query = (q.value or "").strip()
            key = (query, bool(rerank_toggle.value))
            if search_cache["key"] == key and search_cache["facets"] is not None:
                return
            if query:
                universe = await hybrid_search(
                    query, top_k=150, rerank=rerank_toggle.value, collapse_per_doc=True, user=user
                )
                browse = False
            else:
                universe = await _aio.to_thread(browse_documents, user=user, top_k=400)
                browse = True
            # document_facets runs several blocking ORM queries — offload it so
            # it doesn't stall the event loop (and every other client) right
            # after each search the way the inline call did.
            facets = await _aio.to_thread(document_facets, universe)
            search_cache.update(key=key, universe=universe, facets=facets, browse=browse)

        def _filtered(limit: int) -> list:
            facets = search_cache["facets"]
            if not facets:
                return []
            meta = facets["meta"]
            src = set(filter_state["source_ids"])
            tg = set(filter_state["tags"])
            dts = set(filter_state["doc_types"])
            res = [h for h in search_cache["universe"] if _match_filters(h, meta, src, tg, dts)]
            return res[:limit]

        async def _facet_click(kind, value) -> None:
            lst = filter_state[kind]
            if value in lst:
                lst.remove(value)
            else:
                lst.append(value)
            await _go()

        async def _chip_remove(kind, value) -> None:
            if value in filter_state[kind]:
                filter_state[kind].remove(value)
            await _go()

        async def _clear_filters() -> None:
            filter_state["source_ids"] = []
            filter_state["tags"] = []
            filter_state["doc_types"] = []
            await _go()

        def _render_chips() -> None:
            chips_box.clear()
            facets = search_cache["facets"] or {}
            src_name = {sid: nm for sid, nm, _ in facets.get("sources", [])}
            active = (
                [("source_ids", sid, src_name.get(sid, str(sid))) for sid in filter_state["source_ids"]]
                + [("doc_types", dt, dt) for dt in filter_state["doc_types"]]
                + [("tags", tn, tn) for tn in filter_state["tags"]]
            )
            if not active:
                return
            with chips_box:
                ui.label(t("search.active_filters", lang)).classes("text-caption opacity-60")
                for kind, value, label in active:
                    with (
                        ui.row()
                        .classes("ldi-pill ldi-pill-success items-center gap-1 no-wrap")
                        .style("padding: 2px 8px;")
                    ):
                        ui.label(str(label)).classes("text-caption")
                        ui.icon("close").classes("cursor-pointer").style("font-size: 14px;").on(
                            "click", lambda _e, k=kind, v=value: _chip_remove(k, v)
                        )

        def _render_facets() -> None:
            facet_box.clear()
            facets = search_cache["facets"]
            with facet_box:
                if not facets or not (facets["sources"] or facets["doc_types"] or facets["tags"]):
                    ui.label(t("search.no_facets", lang)).classes("text-caption opacity-60")
                    return
                groups = [
                    (
                        "search.sources",
                        "source_ids",
                        [(sid, nm, c) for sid, nm, c in facets["sources"]],
                        True,
                    ),
                    ("search.doc_type", "doc_types", [(dt, dt, c) for dt, c in facets["doc_types"]], True),
                    ("search.tags", "tags", [(tn, tn, c) for tn, c in facets["tags"]], True),
                ]
                for title_key, kind, items, searchable in groups:
                    if not items:
                        continue
                    n_active = sum(1 for it in items if it[0] in filter_state[kind])
                    title = t(title_key, lang) + (f"  ·  {n_active}" if n_active else "")
                    # Collapsed by default — only auto-open a group that has an
                    # active filter, so the sidebar isn't a wall of checkboxes.
                    with ui.expansion(title, value=bool(n_active)).classes("w-full").props("dense"):
                        refs: list = []
                        if searchable and len(items) > 8:

                            def _filter_rows(e, _refs=refs) -> None:
                                term = (e.value or "").lower()
                                shown = 0
                                for row, lbl in _refs:
                                    if term:
                                        row.set_visibility(term in lbl)
                                    else:
                                        row.set_visibility(shown < 40)
                                        shown += 1

                            ui.input(placeholder=t("search.filter_within", lang)).props(
                                "dense borderless clearable"
                            ).classes("w-full").on("update:model-value", _filter_rows)
                        for i, (key, label, count) in enumerate(items):
                            active = key in filter_state[kind]
                            row = (
                                ui.row()
                                .classes("items-center gap-2 w-full no-wrap cursor-pointer")
                                .style("padding: 1px 2px; border-radius: 6px;")
                            )
                            with row:
                                ui.icon("check_box" if active else "check_box_outline_blank").classes(
                                    "text-sm " + ("ldi-accent" if active else "opacity-40")
                                )
                                ui.label(str(label)).classes("text-body2 flex-1 ellipsis").style(
                                    "" if active else "opacity: 0.85;"
                                )
                                ui.label(str(count)).classes("ldi-pill text-caption")
                            row.on("click", lambda _e, k=kind, v=key: _facet_click(k, v))
                            refs.append((row, str(label).lower()))
                            if searchable and len(items) > 40 and i >= 40:
                                row.set_visibility(False)

        async def _go() -> None:
            # Show skeletons while the (possibly slow) universe loads — embedding
            # + hybrid search can take a moment and the column would go blank.
            out.clear()
            with out:
                skeleton_list(4, lines=2, thumb=True)
            try:
                await _ensure_universe()
            except Exception as e:
                logger.warning("search universe failed: {}", e)
                out.clear()
                with out:
                    error_state(
                        "cloud_off",
                        "search.error_title",
                        "search.error_hint",
                        lang,
                        detail=str(e),
                        on_retry=_go,
                    )
                return
            query = (q.value or "").strip()
            _render_facets()
            _render_chips()
            out.clear()
            result_summary.text = ""
            has_filter = bool(filter_state["source_ids"] or filter_state["tags"] or filter_state["doc_types"])
            if not query and not has_filter:
                with out:
                    empty_state("manage_search", "search.start_title", "search.start_hint", lang)
                return
            hits = _filtered(DISPLAY_K)
            if search_cache["browse"]:
                result_summary.text = t("search.browse_count", lang).format(n=len(hits))
            else:
                # Plain query, no developer-facing millisecond timing.
                result_summary.text = t("search.result_count", lang).format(n=len(hits), q=query) + (
                    " " + t("search.reranked", lang) if rerank_toggle.value else ""
                )
            with out:
                if not hits:
                    empty_state("search_off", "search.no_results", "search.no_results_hint", lang)
                    return
                _img_map = _images_by_page({h.document_id for h in hits})
                _tag_map = tags_for_documents({h.document_id for h in hits})
                for rank, h in enumerate(hits, start=1):
                    score_pct = max(0.0, min(1.0, float(h.score))) * 100
                    with ui.card().classes("w-full p-3"):
                        with ui.row().classes("w-full gap-3 no-wrap items-start"):
                            # Matched-page thumbnail (lazy) — shows the page as it
                            # looks, logos/figures included, without a vision scan.
                            ui.html(
                                f"<img src='{media_image_url(h.document_id, h.page_from)}' "
                                f"loading='lazy' referrerpolicy='no-referrer' "
                                f"style='width:82px;height:106px;object-fit:cover;object-position:top;"
                                f"border-radius:8px;border:1px solid var(--ldi-glass-border);"
                                f"flex-shrink:0;background:rgba(255,255,255,0.03);'/>"
                            )
                            with ui.column().classes("flex-1 gap-2 min-w-0"):
                                # Header row — rank is conveyed by order; the full
                                # path moves to a tooltip on the filename.
                                with ui.row().classes("items-center gap-2 w-full no-wrap"):
                                    ui.label(h.filename).classes("text-body1 flex-1 ellipsis").style(
                                        "font-weight: 600;"
                                    ).tooltip(h.path)
                                    ui.label(f"p.{h.page_from}").classes("ldi-pill")
                                    if h.source and h.source != "native_text":
                                        ui.html(_source_pill(h.source)).style("flex-shrink: 0;")
                                # Snippet with highlighted matches
                                ui.html(f"<div class='ldi-snippet'>{_highlight(h.snippet, query)}</div>")
                                _dtags = _tag_map.get(h.document_id, [])
                                if _dtags:
                                    render_tag_chips(_dtags, limit=6)
                                # Embedded images on the matched page (after a vision scan)
                                _imgs = _img_map.get((h.document_id, h.page_from), [])
                                if _imgs:
                                    with ui.row().classes("gap-1 flex-wrap q-mt-xs"):
                                        for _iid in _imgs[:3]:
                                            ui.html(
                                                f"<img src='{doc_image_url(h.document_id, _iid)}' "
                                                f"loading='lazy' referrerpolicy='no-referrer' "
                                                f"style='height:60px;width:auto;border-radius:6px;"
                                                f"border:1px solid var(--ldi-glass-border);'/>"
                                            )
                                # Footer: a single relevance bar (% on hover) + actions
                                with ui.row().classes("items-center gap-3 w-full no-wrap"):
                                    with (
                                        ui.element("div")
                                        .classes("ldi-progress")
                                        .style("height: 4px; max-width: 120px;")
                                        .tooltip(f"{score_pct:.0f}%")
                                    ):
                                        ui.element("div").classes("ldi-progress-fill").style(
                                            f"width: {score_pct:.1f}%;"
                                        )
                                    ui.space()
                                    ui.button(
                                        t("search.btn_view", lang),
                                        icon="visibility",
                                        on_click=lambda did=h.document_id, pg=h.page_from, qv=query: ui.navigate.to(
                                            f"/viewer?doc={did}&page={pg}&q={quote(qv)}"
                                        ),
                                    ).props("dense flat")
                                    ui.button(
                                        t("search.btn_pdf", lang),
                                        icon="picture_as_pdf",
                                        on_click=lambda did=h.document_id, pg=h.page_from: open_pdf(did, pg),
                                    ).props("dense flat")
                                    ui.button(
                                        icon="download",
                                        on_click=lambda did=h.document_id: download_pdf(did),
                                    ).props("dense flat round").tooltip(t("common.download_pdf", lang))

        q.on("keydown.enter", lambda _: _go())
        _refresh_saved()
        # Run once on load: populates the result-driven facet sidebar (library-wide
        # when there's no query) and auto-runs any deep-linked ?tag= / ?q= search.
        ui.timer(0.1, _go, once=True)

    @ui.page("/chat")
    def page_chat() -> None:
        user = _require_login()
        if not user:
            return
        if not _layout(user, "/chat"):
            return
        lang = _user_lang(user)

        # Reopen the last conversation on load, so clicking a citation (→ /viewer)
        # and pressing Back returns to the chat you were in instead of the empty
        # starter screen. Validate ownership before trusting the stored id.
        _last_id = nicegui_app.storage.user.get("chat.last_id")
        if _last_id is not None:
            with session_scope() as _s:
                _c = _s.get(Chat, _last_id)
                if not _c or _c.user_id != user.id:
                    _last_id = None
        chat_state: dict = {"chat_id": _last_id}
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

        def _link_citations(text: str, sources: list[dict], query: str = "") -> str:
            """Replace bracketed citation tokens like ``[1]`` with markdown
            links to the viewer page for the cited document. ``query`` is carried
            into the viewer (``&q=``) so the searched terms get highlighted —
            without it, clicking a citation opened the page with nothing marked."""
            if not sources or not text:
                return text
            qs = f"&q={quote(query)}" if query else ""
            result = text
            for s in sources:
                n = s.get("n")
                did = s.get("document_id")
                pg = s.get("page_from") or 1
                if n is None or did is None:
                    continue
                token = f"[{n}]"
                # Wrap the brackets in a styled markdown link
                replacement = f"[**\\[{n}\\]**](/viewer?doc={did}&page={pg}{qs})"
                # Replace, but only when not already linked (avoid double-wrap
                # if the same N appears twice in the answer).
                result = result.replace(token, replacement)
            return result

        def _render_sources_footer(footer_row, sources: list[dict], query: str = "") -> None:
            """Horizontal scroller of source cards beneath an assistant bubble.
            Each card shows index, filename, page and a short snippet, with
            View/PDF actions on hover. ``query`` (the user's question) highlights
            the matched terms in each snippet and is carried into the viewer."""
            _img_map = _images_by_page({s.get("document_id") for s in sources})
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
                                        ): ui.navigate.to(f"/viewer?doc={d}&page={p}&q={quote(query)}"),
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
                                ui.html(
                                    f"<div class='text-caption opacity-70' style='display:-webkit-box;"
                                    f"-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;'>"
                                    f"{highlight_terms(s.get('snippet') or '', query)}</div>"
                                )
                                _imgs = _img_map.get((s.get("document_id"), s.get("page_from")), [])
                                if _imgs:
                                    with ui.row().classes("gap-1 flex-wrap q-mt-xs"):
                                        for _iid in _imgs[:4]:
                                            ui.html(
                                                f"<img src='{doc_image_url(s.get('document_id'), _iid)}' "
                                                f"loading='lazy' referrerpolicy='no-referrer' "
                                                f"style='height:52px;width:auto;border-radius:6px;"
                                                f"border:1px solid var(--ldi-glass-border);'/>"
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
                    nicegui_app.storage.user["chat.last_id"] = cid
                    _refresh_chats()
                    _refresh_msgs()

                def _open_chat(cid: int) -> None:
                    chat_state["chat_id"] = cid
                    nicegui_app.storage.user["chat.last_id"] = cid
                    _refresh_chats()
                    _refresh_msgs()

                def _delete_chat(cid: int) -> None:
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
                            nicegui_app.storage.user["chat.last_id"] = None
                        _refresh_chats()
                        _refresh_msgs()

                    confirm_dialog("chat.delete_confirm", "chat.delete_hint", _do, lang, danger=True)

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
                        md_el = md_card = footer = None
                        buffer: list[str] = []
                        # Throttle the live markdown re-render: reassigning
                        # md_el.content re-serialises the whole growing answer
                        # over the websocket and re-parses it client-side, so
                        # doing it per-token is O(n²) and stutters on long
                        # answers. Flush ~10 fps; the 0.0 seed flushes the first
                        # token immediately so the bubble fills right away.
                        last_flush = 0.0
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
                                    now = time.monotonic()
                                    if now - last_flush >= 0.1:  # ~10 fps
                                        md_el.content = "".join(buffer)
                                        _scroll_msgs_to_bottom()
                                        last_flush = now
                                elif ev_t == "error":
                                    # stream_answer surfaces unrecoverable errors here
                                    # (e.g. chat not found); show them instead of
                                    # leaving an empty bubble spinning forever.
                                    buffer.append(f"\n\n_⚠️ {ev.get('error', 'error')}_")
                                    md_el.content = "".join(buffer)
                                    _scroll_msgs_to_bottom()
                                elif ev_t == "done":
                                    # remove streaming cursor & wire up sources.
                                    # Always set the final content here — token
                                    # flushes are throttled, so the last partial
                                    # tail would otherwise be missing.
                                    md_card.classes(remove="ldi-stream-cursor")
                                    if cites_state:
                                        # Make [1], [2], … in the answer clickable links
                                        md_el.content = _link_citations(
                                            "".join(buffer), cites_state, query=question
                                        )
                                        _render_sources_footer(footer, cites_state, query=question)
                                    else:
                                        md_el.content = "".join(buffer)
                                    _refresh_chats()  # update timestamp on sidebar
                                    _scroll_msgs_to_bottom()
                        except Exception as e:
                            logger.exception("chat send failed: {}", e)
                            if md_el is not None:
                                buffer.append(f"\n\n_⚠️ {e}_")
                                md_el.content = "".join(buffer)
                        finally:
                            sending_state["busy"] = False
                            # Always clear the streaming cursor, and never leave a
                            # blank bubble if the model returned nothing.
                            if md_card is not None:
                                md_card.classes(remove="ldi-stream-cursor")
                            if md_el is not None and not "".join(buffer).strip():
                                md_el.content = t("chat.no_answer", lang)

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

                                        async def _use_starter(_q=st["question"]) -> None:
                                            _new_chat()
                                            inp.value = _q
                                            await _send()

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
                        last_question = ""
                        for m in msgs:
                            when_str = _fmt_when(m.created_at)
                            if m.role == "user":
                                last_question = m.content or ""
                                _render_user_message(m.content, when=when_str)
                            elif m.role == "assistant":
                                md_el, _md_card, footer = _render_assistant_message_open()
                                md_el.content = _link_citations(
                                    m.content, m.sources or [], query=last_question
                                )
                                if m.sources:
                                    _render_sources_footer(footer, m.sources, query=last_question)
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
        if not _layout(user, "/tags"):
            return
        lang = _user_lang(user)
        page_header("tags.title", lang)
        from app.models import DocumentTagLink, Tag
        from app.services.tag_insights import merge_tags, tag_overview

        # Real subject topics come from the chat model — the heuristic tagger only
        # makes system flags. This runs the LLM over documents missing a topic.
        with ui.row().classes("items-center gap-3 q-mb-sm no-wrap"):
            gen_btn = ui.button(
                t("tags.gen_btn", lang), icon="auto_awesome", on_click=lambda: _generate_topics()
            ).props("color=primary")
            gen_status = ui.label("").classes("text-caption ldi-primary")
            ui.label(t("tags.gen_hint", lang)).classes("text-caption opacity-60")

        async def _generate_topics() -> None:
            if not (get_settings().chat_model or "").strip():
                ui.notify(t("tags.gen_no_model", lang), color="warning")
                return
            from app.services.indexer import backfill_llm_topics

            gen_btn.props("loading")

            def _p(done: int, total: int) -> None:
                gen_status.text = t("tags.gen_running", lang).format(d=done, n=total)

            try:
                n = await backfill_llm_topics(progress=_p)
                gen_status.text = ""
                ui.notify(t("tags.gen_done", lang).format(n=n), color="positive")
            except Exception as e:
                logger.warning("generate topics failed: {}", e)
                gen_status.text = ""
                ui.notify(f"{t('common.error', lang)}: {e}", color="negative")
            finally:
                gen_btn.props(remove="loading")
                _refresh()

        container = ui.column().classes("w-full gap-3")

        def _render_tag_row(s) -> None:
            with ui.row().classes("items-center gap-2 w-full no-wrap q-py-xs"):
                ui.button(s.name, on_click=lambda n=s.name: ui.navigate.to(f"/search?tag={quote(n)}")).props(
                    "flat dense no-caps"
                ).classes("ldi-pill")
                ui.label(t("tags.doc_count", lang).format(n=s.count)).classes(
                    "text-caption opacity-60"
                ).style("min-width: 64px;")
                if s.auto:
                    ui.label(t("tags.auto", lang)).classes("ldi-pill ldi-pill-warning text-caption")
                if s.related:
                    rel = "↔  " + ", ".join(f"{n}·{c}" for n, c in s.related[:4])
                    ui.label(rel).classes("text-caption opacity-50 ellipsis flex-1").tooltip(
                        t("tags.related_tip", lang)
                    )
                else:
                    ui.space()
                ui.button(icon="delete", on_click=lambda tid=s.id: _delete(tid)).props(
                    "flat dense round color=negative"
                )

        def _render_dups(dups) -> None:
            # Body only — rendered inside a "Manage tags" expander by _refresh.
            ui.label(t("tags.dups_hint", lang)).classes("text-caption opacity-70")
            for grp in dups[:12]:
                canonical = grp[0]
                others = [s for s in grp if s.id != canonical.id]
                with ui.row().classes("items-center gap-2 w-full no-wrap"):
                    ui.label(" / ".join(s.name for s in grp)).classes("text-body2 flex-1 ellipsis")

                    def _merge(_c=canonical, _o=others) -> None:
                        def _do() -> None:
                            n = merge_tags(_c.id, [s.id for s in _o])
                            ui.notify(t("tags.merged", lang).format(n=n, name=_c.name), color="positive")
                            _refresh()

                        confirm_dialog(
                            "tags.merge_confirm", "tags.merge_hint", _do, lang, confirm_key="tags.merge"
                        )

                    ui.button(
                        t("tags.merge_into", lang).format(name=canonical.name),
                        icon="merge_type",
                        on_click=_merge,
                    ).props("flat dense")

        def _topic_cloud(items) -> None:
            # Compact + screen-fitting: uniform chips (no sprawling font-scaling),
            # a search-within box, and a top-N window with show-all / show-less so
            # a 100-topic library doesn't render one giant unfit row.
            INITIAL = 18
            state = {"q": "", "expanded": False}
            with section_card(lang, title_key="tags.group_topics", icon="sell"):
                with ui.row().classes("items-center justify-between w-full no-wrap q-mb-xs"):
                    ui.label(t("tags.cloud_hint", lang)).classes("text-caption opacity-60")
                    ui.label(f"{len(items)}").classes("ldi-pill text-caption opacity-70")
                search = (
                    ui.input(placeholder=t("tags.search_ph", lang))
                    # debounce so a large topic library doesn't clear+rebuild the
                    # whole chip grid on every keystroke
                    .props("dense outlined clearable debounce=250").classes("w-full max-w-xs q-mb-sm")
                )
                grid = ui.row().classes("items-center gap-2 flex-wrap")

                def _draw() -> None:
                    grid.clear()
                    qv = (state["q"] or "").strip().lower()
                    filt = [s for s in items if qv in s.name.lower()] if qv else items
                    shown = filt if state["expanded"] else filt[:INITIAL]
                    with grid:
                        for s in shown:
                            chip = ui.row().classes("ldi-pill items-center gap-1 no-wrap cursor-pointer")
                            chip.style("padding: 3px 10px;")
                            chip.on("click", lambda n=s.name: ui.navigate.to(f"/search?tag={quote(n)}"))
                            with chip:
                                ui.label(s.name)
                                ui.label(str(s.count)).classes("opacity-60").style("font-size: 0.72em;")
                                ui.icon("close").classes("opacity-50 cursor-pointer").style(
                                    "font-size: 0.85em;"
                                ).on("click.stop", lambda _e, tid=s.id: _delete(tid))
                        if not shown:
                            ui.label(t("tags.none_match", lang)).classes("text-caption opacity-60")
                        elif not state["expanded"] and len(filt) > INITIAL:
                            ui.button(t("tags.show_all", lang).format(n=len(filt)), icon="expand_more").props(
                                "flat dense no-caps"
                            ).classes("ldi-pill").on("click", lambda: (state.update(expanded=True), _draw()))
                        elif state["expanded"] and len(filt) > INITIAL:
                            ui.button(t("tags.show_less", lang), icon="expand_less").props(
                                "flat dense no-caps"
                            ).classes("ldi-pill").on("click", lambda: (state.update(expanded=False), _draw()))

                def _on_search() -> None:
                    state["q"] = search.value or ""
                    state["expanded"] = bool(state["q"].strip())
                    _draw()

                search.on("update:model-value", lambda _e: _on_search())
                _draw()

        def _pairs_section(pairs) -> None:
            # Body only — rendered inside a "Manage tags" expander by _refresh.
            ui.label(t("tags.together_hint", lang)).classes("text-caption opacity-70 q-mb-xs")
            with ui.row().classes("gap-2 flex-wrap"):
                for (a, b), c in pairs:
                    chip = (
                        ui.row()
                        .classes("ldi-pill ldi-pill-success items-center gap-1 no-wrap cursor-pointer")
                        .style("padding: 3px 10px;")
                    )
                    chip.on("click", lambda _a=a: ui.navigate.to(f"/search?tag={quote(_a)}"))
                    with chip:
                        ui.label(f"{a}  +  {b}").classes("text-caption")
                        ui.label(str(c)).classes("opacity-70").style("font-size: 0.75em;")

        def _refresh() -> None:
            container.clear()
            ov = tag_overview()
            with container:
                if not ov["total"]:
                    empty_state("sell", "tags.empty_title", "tags.empty_hint", lang)
                    return
                help_callout("tags.auto_generated_help", lang)
                topics = ov["groups"].get("topic", [])
                if topics:
                    _topic_cloud(topics)
                else:
                    ui.label(t("tags.no_topics", lang)).classes("text-caption opacity-60 q-pa-sm")
                # Tag *management* (merge duplicates, co-occurrence, system tags) is
                # secondary — tuck it behind collapsed expanders so the page is a
                # quick browse-by-topic by default rather than a wall of sections.
                sys_kinds = sorted(k for k in ov["groups"] if k != "topic")
                if ov["dups"] or ov.get("pairs") or sys_kinds:
                    with section_card(lang, title_key="tags.manage_title", icon="tune"):
                        if ov["dups"]:
                            with (
                                ui.expansion(t("tags.dups_title", lang), icon="merge_type")
                                .classes("w-full")
                                .props("dense")
                            ):
                                _render_dups(ov["dups"])
                        if ov.get("pairs"):
                            with (
                                ui.expansion(t("tags.together_title", lang), icon="hub")
                                .classes("w-full")
                                .props("dense")
                            ):
                                _pairs_section(ov["pairs"])
                        if sys_kinds:
                            with (
                                ui.expansion(t("tags.system_title", lang), icon="label")
                                .classes("w-full")
                                .props("dense")
                            ):
                                for kind in sys_kinds:
                                    items = ov["groups"][kind]
                                    with (
                                        ui.expansion(f"{kind}  ·  {len(items)}")
                                        .classes("w-full")
                                        .props("dense")
                                    ):
                                        for s in items:
                                            _render_tag_row(s)

        def _delete(tid: int) -> None:
            def _do() -> None:
                with session_scope() as session:
                    for link in session.exec(
                        select(DocumentTagLink).where(DocumentTagLink.tag_id == tid)
                    ).all():
                        session.delete(link)
                    tag_obj = session.get(Tag, tid)
                    if tag_obj:
                        session.delete(tag_obj)
                _refresh()

            confirm_dialog("tags.delete_confirm", "tags.delete_help", _do, lang, danger=True)

        _refresh()

    @ui.page("/backup")
    def page_backup() -> None:
        user = _require_login()
        if not user:
            return
        if not _layout(user, "/backup"):
            return
        lang = _user_lang(user)
        page_header("backup.title", lang)

        from app.backup import BACKUP_COMPONENTS, create_backup, list_backups, restore_backup

        with section_card(lang, title_key="backup.create", icon="save"):
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
        with section_card(lang, title_key="backup.portable_title", icon="drive_file_move", extra="q-mt-md"):
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
                _existing = list_backups()
                if not _existing:
                    empty_state("inventory_2", "backup.empty_title", "backup.empty_hint", lang)
                for b in _existing:
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
        if not _layout(user, "/settings"):
            return
        lang = _user_lang(user)
        page_header("settings.title", lang)
        s = get_settings()

        # Tabbed settings: the section cards below are built flat (so the shared
        # _save() closure still sees every widget), then relocated into these
        # panels with .move() — no re-indentation of the long section bodies.
        with (
            ui.tabs()
            .props("align=left active-color=primary inline-label")
            .classes("w-full") as _settings_tabs
        ):
            ui.tab("models", label=t("settings.tab_models", lang), icon="memory")
            ui.tab("indexing", label=t("settings.tab_indexing", lang), icon="tune")
            ui.tab("appearance", label=t("settings.tab_appearance", lang), icon="palette")
            ui.tab("network", label=t("settings.tab_network", lang), icon="lan")
            ui.tab("account", label=t("settings.tab_account", lang), icon="person")
            ui.tab("license", label=t("settings.tab_license", lang), icon="workspace_premium")
        with ui.tab_panels(_settings_tabs, value="models").classes("w-full"):
            _p_models = ui.tab_panel("models").classes("q-px-none")
            _p_indexing = ui.tab_panel("indexing").classes("q-px-none")
            _p_appearance = ui.tab_panel("appearance").classes("q-px-none")
            _p_network = ui.tab_panel("network").classes("q-px-none")
            _p_account = ui.tab_panel("account").classes("q-px-none")
            _p_license = ui.tab_panel("license").classes("q-px-none")

        with section_card(lang, title_key="settings.lmstudio", icon="memory") as _card_lm:
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

            with ui.row().classes("items-center gap-4 w-full q-mt-sm"):
                quality_sel = (
                    ui.select(
                        {
                            "fastest": t("settings.q_fastest", lang),
                            "balanced": t("settings.q_balanced", lang),
                            "max": t("settings.q_max", lang),
                        },
                        value=getattr(s, "model_quality", "balanced"),
                        label=t("settings.model_quality", lang),
                    )
                    .props("dense outlined")
                    .classes("min-w-[220px]")
                )

                def _save_quality() -> None:
                    save_user_settings({"model_quality": quality_sel.value})
                    get_settings.cache_clear()

                quality_sel.on("update:model-value", lambda _e: _save_quality())

                vis_lang_sel = (
                    ui.select(
                        {
                            "auto": t("settings.vlang_auto", lang),
                            "de": t("settings.vlang_de", lang),
                            "en": t("settings.vlang_en", lang),
                        },
                        value=getattr(s, "vision_language", "auto"),
                        label=t("settings.vision_language", lang),
                    )
                    .props("dense outlined")
                    .classes("min-w-[220px]")
                )

                def _save_vis_lang() -> None:
                    save_user_settings({"vision_language": vis_lang_sel.value})
                    get_settings.cache_clear()

                vis_lang_sel.on("update:model-value", lambda _e: _save_vis_lang())

                preload_sw = ui.switch(
                    t("settings.preload_models", lang),
                    value=bool(getattr(s, "preload_models", True)),
                )

                def _save_preload() -> None:
                    save_user_settings({"preload_models": bool(preload_sw.value)})
                    get_settings.cache_clear()

                preload_sw.on("update:model-value", lambda _e: _save_preload())

                unload_sw = ui.switch(
                    t("settings.unload_on_exit", lang),
                    value=bool(getattr(s, "unload_on_exit", True)),
                )

                def _save_unload() -> None:
                    save_user_settings({"unload_on_exit": bool(unload_sw.value)})
                    get_settings.cache_clear()

                unload_sw.on("update:model-value", lambda _e: _save_unload())

                topics_sw = ui.switch(
                    t("settings.llm_topics", lang),
                    value=bool(getattr(s, "llm_topics_enabled", False)),
                )

                def _save_topics() -> None:
                    save_user_settings({"llm_topics_enabled": bool(topics_sw.value)})
                    get_settings.cache_clear()

                topics_sw.on("update:model-value", lambda _e: _save_topics())

            ui.label(t("settings.llm_topics_hint", lang)).classes("text-caption opacity-60")
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

            async def _offer_downloads(missing: list) -> None:
                """Ask-first download prompt for roles with no suitable local model."""
                from app.services import lms_cli

                have_lms = lms_cli.is_available()
                with ui.dialog() as dlg, ui.card().classes("w-[540px] p-4"):
                    ui.label(t("settings.download_models_title", lang)).classes("text-h6 ldi-primary")
                    if not have_lms:
                        ui.label(t("settings.lms_missing", lang)).classes(
                            "ldi-pill ldi-pill-warning q-mb-sm"
                        ).style("white-space: normal;")
                    for ch in missing:
                        with ui.row().classes("items-center gap-2 w-full no-wrap q-mb-xs"):
                            ui.label(f"{ch.role}: {ch.suggestion}  (~{ch.size_gb:g} GB)").classes(
                                "flex-1 text-body2"
                            )

                            async def _dl(target=ch.suggestion) -> None:
                                ui.notify(t("settings.download_started", lang).format(m=target), color="info")
                                ok, out = await lms_cli.download(target)
                                ui.notify(
                                    (
                                        t("settings.download_ok", lang)
                                        if ok
                                        else t("settings.download_fail", lang)
                                    ).format(m=target),
                                    color=("positive" if ok else "negative"),
                                )

                            b = ui.button(t("settings.download", lang), icon="download", on_click=_dl).props(
                                "dense color=primary"
                            )
                            if not have_lms:
                                b.props("disable")
                    with ui.row().classes("justify-end w-full q-mt-md"):
                        ui.button(t("common.close", lang), on_click=dlg.close).props("flat")
                dlg.open()

            async def _auto_pick() -> None:
                # Hardware-aware: pick the best *downloaded* model per role for
                # this machine's tier + the saved quality preference, then warm
                # them in LM Studio. Missing roles get an ask-first download.
                from app.llm import LMStudioClient, warm_up_configured
                from app.services.hardware import active_tuning
                from app.services.model_advisor import recommend

                c = LMStudioClient(base_url=url.value or s.lmstudio_base_url)
                try:
                    available = await c.list_downloaded()
                except Exception as e:
                    connection_status.text = f"✗ {e}"
                    ui.notify(str(e), color="negative")
                    return
                pref = getattr(get_settings(), "model_quality", "balanced")
                tier = active_tuning().tier
                plan = recommend(available, tier=tier, chat_preference=pref)

                updates: dict[str, Any] = {}
                if plan.embedding.model:
                    emb_model.set_value(plan.embedding.model)
                    updates["embedding_model"] = plan.embedding.model
                if plan.chat.model:
                    chat_model.set_value(plan.chat.model)
                    updates["chat_model"] = plan.chat.model
                if plan.vision.model:
                    vision_model.set_value(plan.vision.model)
                    updates["vision_model"] = plan.vision.model
                if updates:
                    save_user_settings(updates)
                    get_settings.cache_clear()

                picked = " · ".join(f"{k.replace('_model', '')}={v}" for k, v in updates.items()) or "—"
                connection_status.text = f"✓ {tier} tier / {pref}: {picked}"
                if updates:
                    ui.notify(f"Auto-picked & saved: {picked}", color="positive")
                    warm = await warm_up_configured()
                    failed = [k for k, (ok, _m) in warm.items() if not ok]
                    if failed:
                        ui.notify("Loaded, but couldn't warm: " + ", ".join(failed), color="warning")
                else:
                    ui.notify("No suitable models downloaded yet.", color="warning")
                if plan.missing():
                    await _offer_downloads(plan.missing())

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

        with section_card(
            lang, title_key="settings.ocr", icon="document_scanner", extra="q-mt-md"
        ) as _card_ocr:
            tcmd = ui.input(t("settings.tesseract", lang), value=s.tesseract_cmd).classes("w-full")
            tlang = ui.input(t("settings.ocr_langs", lang), value=s.ocr_lang).classes("w-full")
            tess_status = ui.label("").classes("text-caption opacity-80 q-mt-xs")

            def _detect_tesseract() -> None:
                """Probe common install locations and PATH for tesseract, save the
                hit, then verify it actually runs (so this agrees with Diagnostics)."""
                from app.ocr.tesseract import find_tesseract_binary, tesseract_available

                found = find_tesseract_binary()
                if found:
                    tcmd.set_value(found)
                    save_user_settings({"tesseract_cmd": found})
                    get_settings.cache_clear()
                    if tesseract_available(refresh=True):
                        tess_status.text = f"✓ Found and saved: {found}"
                        ui.notify(f"Tesseract found: {found}", color="positive")
                    else:
                        tess_status.text = f"⚠ Found {found}, but it failed to run — check the install."
                        ui.notify("Tesseract found but did not run", color="warning")
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

        with section_card(lang, title_key="settings.indexing", icon="tune", extra="q-mt-md") as _card_idx:
            csize = ui.number(t("settings.chunk_size", lang), value=s.chunk_size).classes("w-32")
            coverlap = ui.number(t("settings.chunk_overlap", lang), value=s.chunk_overlap).classes("w-32")

        with section_card(
            lang, title_key="settings.performance", icon="speed", extra="q-mt-md"
        ) as _card_perf:
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

        with section_card(
            lang, title_key="settings.appearance", icon="palette", extra="q-mt-md"
        ) as _card_appear:
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

        with section_card(lang, title_key="settings.network", icon="lan", extra="q-mt-md") as _card_net:
            port_in = ui.number(
                t("settings.app_port", lang), value=s.port, min=1, max=65535, format="%d"
            ).classes("w-40")
            lan_sw = ui.switch(t("settings.allow_lan", lang), value=s.allow_lan)
            secure_sw = ui.switch(
                t("settings.secure_cookies", lang), value=bool(getattr(s, "secure_cookies", False))
            )
            ui.label(t("settings.secure_cookies_hint", lang)).classes("text-caption opacity-60")
            help_callout("settings.network_hint", lang, icon="warning")
            help_callout("settings.wan_security_hint", lang, icon="security")
            ui.label(t("settings.network_restart", lang)).classes("text-caption opacity-60 q-mt-xs")

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
                    "vision_language": vis_lang_sel.value or "auto",
                    "tesseract_cmd": _str(tcmd.value),
                    "ocr_lang": _str(tlang.value) or s.ocr_lang,
                    "chunk_size": int(csize.value or s.chunk_size),
                    "chunk_overlap": int(coverlap.value or s.chunk_overlap),
                    "performance_profile": perf.value or s.performance_profile,
                    "port": int(port_in.value or s.port),
                    "allow_lan": bool(lan_sw.value),
                    "secure_cookies": bool(secure_sw.value),
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

        with section_card(
            lang, title_key="settings.tab_account", icon="manage_accounts", extra="q-mt-md"
        ) as _card_pw:
            ui.label(t("settings.account_hint", lang)).classes("text-caption opacity-70")

            # --- Admin username -------------------------------------------
            uname_in = ui.input(t("settings.admin_username", lang), value=user.username).classes("w-full")

            def _change_username() -> None:
                new_un = (uname_in.value or "").strip()
                if not new_un:
                    ui.notify(t("settings.username_required", lang), color="negative")
                    return
                if new_un == user.username:
                    return
                with write_session() as session:
                    clash = session.exec(select(User).where(User.username == new_un)).first()
                    if clash and clash.id != user.id:
                        ui.notify(t("settings.username_taken", lang), color="negative")
                        return
                    u = session.get(User, user.id)
                    if u:
                        u.username = new_un
                        session.add(u)
                # The saved remember-me credential still holds the OLD username.
                try:
                    delete_secret(REMEMBER_SECRET_NAME)
                except Exception as e:
                    logger.warning("username change: clearing remember-me failed: {}", e)
                ui.notify(t("settings.username_updated", lang), color="positive")
                ui.navigate.reload()

            ui.button(t("settings.update_username", lang), icon="badge", on_click=_change_username).props(
                "color=primary"
            )

            ui.separator().classes("q-my-md")

            # --- Password (requires the current one) ----------------------
            old = ui.input(
                t("common.current_password", lang), password=True, password_toggle_button=True
            ).classes("w-full")
            new = ui.input(
                t("common.new_password", lang), password=True, password_toggle_button=True
            ).classes("w-full")

            def _change() -> None:
                with write_session() as session:
                    u = session.get(User, user.id)
                    if not u or not verify_password(old.value, u.password_hash):
                        ui.notify(t("settings.pw_wrong", lang), color="negative")
                        return
                    u.password_hash = hash_password(new.value)
                    session.add(u)
                    new_pwv = session_fingerprint(u)
                # The password change revokes every OTHER session (their stamped
                # fingerprint is now stale); keep THIS browser signed in.
                nicegui_app.storage.user["pwv"] = new_pwv
                # Stored remember-me holds the old password — drop it so the next
                # sign-in uses the new one.
                try:
                    delete_secret(REMEMBER_SECRET_NAME)
                except Exception as e:
                    logger.warning("password change: clearing remember-me failed: {}", e)
                ui.notify(t("settings.pw_updated", lang), color="positive")

            ui.button(t("settings.update_pw", lang), icon="lock", on_click=_change).props("color=primary")

        with section_card(
            lang, title_key="settings.tab_license", icon="workspace_premium", extra="q-mt-md"
        ) as _card_lic:
            _lic_status = licensing.current_status()
            _render_license_summary(_lic_status, lang)
            ui.separator().classes("q-my-sm")
            ui.label(t("license.replace", lang)).classes("text-caption opacity-70")
            lic_key_in = (
                ui.textarea(t("license.key_label", lang))
                .props("outlined autogrow")
                .classes("w-full")
                .style("font-family: 'JetBrains Mono', monospace;")
            )
            lic_msg = ui.label("").classes("text-caption")

            def _lic_apply() -> None:
                st = licensing.activate(lic_key_in.value or "")
                if st.active:
                    ui.notify(t("license.activated", lang), color="positive")
                    ui.navigate.reload()
                    return
                err_key = (
                    "license.err_empty"
                    if st.reason == "none"
                    else "license.err_expired" if st.reason == "expired" else "license.err_invalid"
                )
                lic_msg.text = t(err_key, lang)
                lic_msg.classes(replace="text-caption text-negative")

            with ui.row().classes("gap-2 q-mt-sm"):
                ui.button(t("license.activate_btn", lang), icon="lock_open", on_click=_lic_apply).props(
                    "color=primary"
                )

                if _lic_status.active:

                    def _lic_remove() -> None:
                        def _do() -> None:
                            licensing.deactivate()
                            ui.notify(t("license.removed", lang), color="warning")
                            ui.navigate.to("/activate")

                        confirm_dialog(
                            "license.remove",
                            "license.remove_confirm",
                            _do,
                            lang,
                            danger=True,
                            confirm_key="license.remove",
                        )

                    ui.button(t("license.remove", lang), icon="lock", on_click=_lic_remove).props(
                        "flat color=negative"
                    )

        # Relocate each flat-built section card into its tab panel.
        _card_lm.move(_p_models)
        _card_ocr.move(_p_models)
        _card_idx.move(_p_indexing)
        _card_perf.move(_p_indexing)
        _card_appear.move(_p_appearance)
        _card_net.move(_p_network)
        _card_pw.move(_p_account)
        _card_lic.move(_p_license)

        # Persistent save bar below the tabs (applies to every tab except
        # Account, which has its own update button).
        with ui.row().classes("w-full q-mt-md justify-end"):
            ui.button(t("settings.save", lang), icon="save", on_click=_save).props("color=primary")

    @ui.page("/logs")
    def page_logs() -> None:
        user = _require_login()
        if not user:
            return
        if not _layout(user, "/logs"):
            return
        lang = _user_lang(user)
        page_header("logs.title", lang)
        help_callout("logs.tail_hint", lang)
        s = get_settings()
        log_file = s.logs_path / "trovato.log"
        if log_file.exists():
            try:
                text_blob = log_file.read_text(encoding="utf-8", errors="replace")[-20_000:]
            except Exception as e:
                text_blob = f"(cannot read log: {e})"
            ui.code(text_blob, language="text").classes("w-full")
        else:
            empty_state("article", "logs.empty_title", "logs.empty_hint", lang)

    @ui.page("/compare")
    def page_compare(a: int = 0, b: int = 0) -> None:
        user = _require_login()
        if not user:
            return
        if not _layout(user, "/compare"):
            return
        lang = _user_lang(user)
        breadcrumbs([(t("nav.documents", lang), "/documents"), (t("compare.title", lang), None)])
        page_header("compare.title", lang)

        with session_scope() as session:
            # ACL: only offer documents this user can see in the compare picker.
            docs = session.exec(
                filter_documents(select(Document), user).order_by(Document.id.desc()).limit(2000)
            ).all()
            options = {d.id: f"#{d.id} — {d.filename}" for d in docs if d.id is not None}

        if not options:
            empty_state("library_books", "compare.empty_title", "compare.index_first", lang)
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
                loading = ui.column().classes("w-full gap-2")
                with loading:
                    skeleton_list(2, lines=3)
            result = await compare_documents(int(sel_a.value), int(sel_b.value))
            loading.delete()
            with output:
                with section_card(lang, title_key="compare.narrative", icon="compare_arrows"):
                    ui.markdown(result.narrative)
                with section_card(lang):
                    ui.label(f"{t('compare.shared_ratio', lang)} {result.shared_ratio:.1%}").classes(
                        "text-body1"
                    )
                with ui.row().classes("w-full gap-2"):
                    with section_card(lang, extra="flex-1"):
                        ui.label(f"{t('compare.only_in', lang)} {result.doc_a.get('filename')}").classes(
                            "text-h6"
                        )
                        for ln in result.only_in_a_sample:
                            ui.label(ln).classes("text-caption opacity-80")
                    with section_card(lang, extra="flex-1"):
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
        if not _layout(user, "/diagnostics"):
            return
        lang = _user_lang(user)
        page_header("diag.title", lang)

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

                    # refresh=True: re-probe on every visit so the page never
                    # shows a stale "not found" cached from an earlier run.
                    tess_ok = tesseract_available(refresh=True)
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
                    empty_state("content_copy", "diag.dup_none_title", "diag.dup_none", lang)
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
                    empty_state("join_full", "diag.near_dup_none_title", "diag.near_dup_none", lang)
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
    async def page_viewer(doc: int = 0, page: int = 1, q: str = "") -> None:
        """Continuous-scroll PDF viewer.

        All pages render as one lazy-loading scroll (like a real PDF reader):
        page flips, the page-number jump, zoom and the fullscreen read mode run
        entirely client-side (app/ui/static/viewer.js) — no full page reload
        per flip. Page images come from the width-bucketed render endpoint via
        ``srcset``, so the browser picks a resolution matching the actual
        display. Search terms are highlighted ON the page images (normalized
        rects from app/services/page_matches.py) and in the sidebar text.
        """
        user = _require_login()
        if not user:
            return
        if not _layout(user, "/documents"):
            return
        lang = _user_lang(user)
        with session_scope() as session:
            d = session.get(Document, doc)
            # ACL: a non-admin must not open a document they can't see (direct
            # /viewer?doc=N access). Treated as not-found to avoid id probing.
            if not d or not can_see_document(user, d):
                empty_state("find_in_page", "docs.viewer.not_found", "docs.viewer.not_found_hint", lang)
                return
            from app.models import DocumentImage as _DI
            from app.models import DocumentPage as _DP
            from app.models import DocumentTagLink as _DTL
            from app.models import Tag as _Tag

            total_pages = d.page_count or 1
            page = max(1, min(page or 1, total_pages))
            # Per-page dimensions (PDF points) → aspect-ratio placeholders, so
            # the lazy-loading scroll keeps its geometry before images arrive.
            # Rows from older scans may carry 0/0; viewer.js falls back to A4.
            dims_by_no: dict[int, tuple[int, int]] = {}
            for pn, w, h in session.exec(
                select(_DP.page_number, _DP.width, _DP.height).where(_DP.document_id == doc)
            ).all():
                dims_by_no[pn] = (w or 0, h or 0)
            dims = [list(dims_by_no.get(n, (0, 0))) for n in range(1, total_pages + 1)]
            # The document's tags (topic + system), for the viewer sidebar.
            doc_tags = list(
                session.exec(
                    select(_Tag.name).join(_DTL, _DTL.tag_id == _Tag.id).where(_DTL.document_id == doc)
                ).all()
            )
            doc_path = d.path
            doc_filename = d.filename

        state: dict[str, Any] = {"q": q, "page": page}

        # ---- find-in-document -----------------------------------------------
        def _find_matches(term: str) -> tuple[list[int], dict[int, list[tuple]]]:
            """(matching pages, on-image highlight rects) for the term's
            meaningful tokens. Pages match on native/OCR text OR an image
            description (chat often cites an IMAGE match whose text lives in
            DocumentImage.vision_description, not in the page text)."""
            terms = meaningful_terms(term)
            if not terms:
                return [], {}
            from sqlalchemy import or_ as _or

            with session_scope() as s2:
                conds = []
                for tm in terms:
                    like = f"%{tm}%"
                    conds.append(_DP.native_text.ilike(like))
                    conds.append(_DP.ocr_text.ilike(like))
                text_pages = set(
                    s2.exec(select(_DP.page_number).where(_DP.document_id == doc, _or(*conds))).all()
                )
                img_conds = [_DI.vision_description.ilike(f"%{tm}%") for tm in terms]
                img_pages = set(
                    s2.exec(select(_DI.page_number).where(_DI.document_id == doc, _or(*img_conds))).all()
                )
            pages = sorted(text_pages | img_pages)
            from app.services.page_matches import match_rects_for_pages

            rects = match_rects_for_pages(doc_path, pages, terms) if pages else {}
            return pages, rects

        # io_bound: _find_matches runs ILIKE scans + PyMuPDF text search over up
        # to 80 pages — seconds on big scanned PDFs. Sync page builders execute
        # on the asyncio event loop, which would freeze EVERY connected client.
        from nicegui import run as _run

        match_pages, match_rects = (await _run.io_bound(_find_matches, q)) if q else ([], {})

        # ---- header row -----------------------------------------------------
        breadcrumbs([(t("nav.documents", lang), "/documents"), (doc_filename, None)])
        pdf_base = pdf_url(doc, None, _media_token())
        with ui.row().classes("items-center w-full gap-2 no-wrap q-mb-sm"):
            ui.label(doc_filename).classes("text-h5 ldi-primary").style(
                "min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
            )
            ui.space()
            ui.button(
                t("docs.viewer.open_pdf", lang),
                icon="picture_as_pdf",
                # Opens the original at the page currently in view (client-side).
                on_click=lambda: ui.run_javascript(
                    f"window.open({pdf_base!r} + '#page=' + ldiViewer.current(), '_blank')"
                ),
            ).props("dense color=primary")
            ui.button(
                t("common.download_pdf", lang),
                icon="download",
                on_click=lambda: download_pdf(doc),
            ).props("dense outline")
            if q:
                ui.button(
                    t("docs.viewer.back_to_search", lang),
                    icon="arrow_back",
                    on_click=lambda qq=q: ui.navigate.to(f"/search?q={quote(qq)}"),
                ).props("dense flat")

        # ---- find row ---------------------------------------------------------
        with ui.row().classes("items-center gap-2 q-mb-sm w-full no-wrap"):
            find_in = (
                ui.input(placeholder=t("docs.viewer.find_placeholder", lang), value=q)
                .props("dense outlined clearable")
                .classes("min-w-[260px]")
            )

            async def _on_find_click() -> None:
                await _apply_find(find_in.value)

            ui.button(icon="search", on_click=_on_find_click).props("dense")
            chips_row = (
                ui.row().classes("items-center gap-2 no-wrap").style("overflow-x: auto; min-width: 0;")
            )

        def _render_chips(pages: list[int], term: str) -> None:
            chips_row.clear()
            if not term:
                return
            with chips_row:
                if pages:
                    ui.label(t("docs.viewer.find_count", lang).format(n=len(pages))).classes(
                        "text-caption opacity-70 no-wrap"
                    )
                    for _pn in pages[:40]:
                        ui.button(
                            str(_pn),
                            on_click=lambda pn=_pn: ui.run_javascript(f"ldiViewer.jump({pn})"),
                        ).props("dense flat size=sm")
                    ui.label(t("docs.viewer.hl_hint", lang)).classes("text-caption opacity-50 no-wrap")
                else:
                    ui.label(t("docs.viewer.find_none", lang).format(q=term)).classes(
                        "text-caption opacity-70"
                    )

        # ---- viewer + sidebar -------------------------------------------------
        with ui.row().classes("w-full gap-4 no-wrap items-start"):
            with (
                ui.element("div")
                .classes("column gap-2")
                .style("min-width: 0; flex: 2;")
                .props("id=ldi-viewer-root")
            ):
                with ui.row().classes("items-center gap-1 no-wrap w-full"):
                    ui.button(
                        icon="navigate_before",
                        on_click=lambda: ui.run_javascript("ldiViewer.prev()"),
                    ).props("dense flat").tooltip(t("docs.viewer.prev", lang))
                    # Plain HTML input: viewer.js keeps it in sync while
                    # scrolling and jumps on Enter — zero server round-trips.
                    ui.html(
                        f"""<input id="ldi-pginput" class="ldi-pginput" type="number" min="1" """
                        f"""max="{total_pages}" value="{page}" """
                        f"""aria-label="{t("docs.viewer.jump_to", lang)}">"""
                    )
                    ui.label(t("docs.viewer.of_pages", lang).format(n=total_pages)).classes(
                        "text-caption opacity-70 no-wrap"
                    )
                    ui.button(
                        icon="navigate_next",
                        on_click=lambda: ui.run_javascript("ldiViewer.next()"),
                    ).props("dense flat").tooltip(t("docs.viewer.next", lang))
                    ui.space()
                    ui.button(
                        icon="zoom_out", on_click=lambda: ui.run_javascript("ldiViewer.zoomOut()")
                    ).props("dense flat").tooltip(t("docs.viewer.zoom_out", lang))
                    ui.button(icon="fit_screen", on_click=lambda: ui.run_javascript("ldiViewer.fit()")).props(
                        "dense flat"
                    ).tooltip(t("docs.viewer.fit_width", lang))
                    ui.button(icon="zoom_in", on_click=lambda: ui.run_javascript("ldiViewer.zoomIn()")).props(
                        "dense flat"
                    ).tooltip(t("docs.viewer.zoom_in", lang))
                    ui.space()
                    ui.button(
                        icon="fullscreen",
                        on_click=lambda: ui.run_javascript("ldiViewer.fullscreen()"),
                    ).props("dense flat").tooltip(
                        t("docs.viewer.read_mode", lang) + " — " + t("docs.viewer.read_mode_exit", lang)
                    )
                # viewer.js builds all page wrappers + lazy images in here.
                ui.element("div").classes("ldi-pdfscroll w-full").props("id=ldi-pdfscroll")
            with ui.column().classes("gap-2").style("min-width: 280px; flex: 1"):
                if doc_tags:
                    with section_card(lang, title_key="tags.section", icon="label"):
                        render_tag_chips(doc_tags, limit=20)
                with section_card(lang, title_key="docs.viewer.page_text", icon="article"):
                    side_caption = ui.label("").classes("text-caption opacity-60")
                    side_text = ui.element("div").classes("w-full")
                imgs_box = ui.element("div").classes("w-full")

        def _update_side(n: int) -> None:
            """Refresh the sidebar (page text + image descriptions) for page n.

            Called at build time and from the debounced 'ldi_page' scroll event
            — the only viewer interaction that talks to the server at all.
            """
            state["page"] = n
            with session_scope() as s3:
                row = s3.exec(select(_DP).where(_DP.document_id == doc, _DP.page_number == n)).first()
                text = (row.native_text or row.ocr_text or "") if row else ""
                descs = [
                    (img.vision_description or "").strip()
                    for img in s3.exec(
                        select(_DI)
                        .where(_DI.document_id == doc, _DI.page_number == n)
                        .order_by(_DI.image_index)
                    ).all()
                    if (img.vision_description or "").strip()
                ]
            side_caption.set_text(t("docs.viewer.text_of_page", lang).format(n=n))
            side_text.clear()
            with side_text:
                if text:
                    ui.html(
                        f"<div class='text-body2' style='white-space:pre-wrap;"
                        f"max-height:56vh;overflow:auto;'>{highlight_terms(text, state['q'])}</div>"
                    )
                elif not descs:
                    ui.markdown(f"_{t('docs.viewer.no_text', lang)}_").classes("text-body2")
            imgs_box.clear()
            if descs:
                with imgs_box, section_card(lang, title_key="docs.viewer.images", icon="image"):
                    for _desc in descs:
                        ui.html(
                            f"<div class='text-body2 q-mb-sm' style='white-space:pre-wrap;'>"
                            f"{highlight_terms(_desc, state['q'])}</div>"
                        )

        async def _apply_find(term: str) -> None:
            import json as _json

            term = (term or "").strip()
            state["q"] = term
            # Worker thread — same event-loop-freeze reasoning as the build.
            pages, rects = (await _run.io_bound(_find_matches, term)) if term else ([], {})
            payload = _json.dumps({str(k): [list(r) for r in v] for k, v in rects.items()}).replace(
                "<", "\\u003c"
            )
            js = f"ldiViewer.setHighlights({payload}, {_json.dumps(term)}, {_json.dumps(pages)});"
            if pages:
                js += f"ldiViewer.jump({pages[0]});"
            ui.run_javascript(js)
            _render_chips(pages, term)
            _update_side(state["page"])
            if term and not pages:
                ui.notify(t("docs.viewer.find_none", lang).format(q=term), color="warning")

        async def _on_find_submit() -> None:
            await _apply_find(find_in.value)

        async def _on_find_clear() -> None:
            await _apply_find("")

        find_in.on("keydown.enter", _on_find_submit)
        find_in.on("clear", _on_find_clear)

        def _on_page_evt(e: Any) -> None:
            try:
                n = int((e.args or {}).get("n", 0))
            except Exception:
                return
            if 1 <= n <= total_pages and n != state.get("page"):
                _update_side(n)

        ui.on("ldi_page", _on_page_evt)

        _render_chips(match_pages, q)
        _update_side(page)

        # ---- client bootstrap --------------------------------------------------
        import json as _json

        from app.api.routes.documents import PAGE_WIDTH_BUCKETS

        tok = _media_token()
        url_tpl = f"/api/documents/{doc}/page/{{n}}/image" + (f"?t={tok}" if tok else "")
        # matchesTpl lets viewer.js lazily fetch highlight rects for matching
        # pages beyond the initial server-computed window (MAX_PAGES).
        matches_tpl = f"/api/documents/{doc}/matches" + (f"?t={tok}" if tok else "")
        init_payload = _json.dumps(
            {
                "doc": doc,
                "total": total_pages,
                "page": page,
                "q": q,
                "dims": dims,
                "rects": {str(k): [list(r) for r in v] for k, v in match_rects.items()},
                "matchPages": match_pages,
                "urlTpl": url_tpl,
                "matchesTpl": matches_tpl,
                "buckets": list(PAGE_WIDTH_BUCKETS),
                "defaultW": 1024,
                # Shown by viewer.js when a page image fails to load — almost
                # always because the original file isn't reachable from THIS
                # machine (index copied/synced without the source PDFs, source
                # on an unplugged drive, or the file was moved). Translated here
                # since viewer.js has no i18n.
                "labels": {
                    "pageUnavailable": t("viewer.page_unavailable", lang),
                    "originalMissing": t("viewer.original_missing", lang).format(path=doc_path),
                },
            }
        ).replace("<", "\\u003c")
        ui.add_body_html(f"<script>window.__ldiViewerInit = {init_payload};</script>")
        ui.add_body_html(f'<script src="/ldi-static/viewer.js?v={quote(__version__)}" defer></script>')

    @ui.page("/about")
    def page_about() -> None:
        user = _require_login()
        if not user:
            return
        if not _layout(user, "/about"):
            return
        lang = _user_lang(user)
        page_header("about.title", lang)
        from app import __app_name__ as N
        from app import __author__, __contact__, __handle__, __lead_programmer__
        from app import __version__ as V

        with ui.card().classes("w-full p-4"):
            ui.label(N).classes("text-h5")
            ui.label(f"{t('about.version', lang)} {V}").classes("opacity-70")
            ui.label(t("about.description", lang)).classes("q-mt-sm")
            ui.separator().classes("q-my-md")
            ui.label(f"{t('about.author', lang)}: {__author__}")
            ui.label(f"{t('about.lead_programmer', lang)}: {__lead_programmer__}")
            ui.label(f"{t('about.contact', lang)}: {__contact__}")
            ui.label(f"{t('about.handle', lang)}: {__handle__}")
            ui.link(t("about.github", lang), "https://github.com/Various5/trovato", new_tab=True)
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
