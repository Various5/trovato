"""Shared, theme- and i18n-aware UI building blocks.

These are pure presentation helpers: they take an already-resolved ``lang`` and
do no DB access. Page logic stays in the page closures — handlers are passed in
as callbacks (``on_action`` / ``on_confirm`` / ``actions``) so they attach to the
correct NiceGUI parent and keep working unchanged. Every helper reuses the
existing ``ldi-*`` classes (see ``app/ui/styles.py``), so it inherits the active
theme automatically.

This module imports only ``nicegui.ui`` + ``i18n.t`` (never ``app_ui``), so it
can't create an import cycle.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from nicegui import ui

from app.utils.i18n import t


def page_header(
    title_key: str,
    lang: str,
    *,
    subtitle_key: str | None = None,
    actions: Callable[[], Any] | None = None,
) -> None:
    """Standard page title (gradient ``ldi-hero-text``), optional subtitle + right-aligned actions.

    With no subtitle/actions this renders the exact same single label the pages
    used before, so adoption is a no-op visually.
    """
    if subtitle_key is None and actions is None:
        ui.label(t(title_key, lang)).classes("text-h4 q-mb-md ldi-hero-text")
        return
    with ui.row().classes("items-center w-full q-mb-md gap-3 no-wrap"):
        with ui.column().classes("gap-0 flex-1 min-w-0"):
            ui.label(t(title_key, lang)).classes("text-h4 ldi-hero-text")
            if subtitle_key:
                ui.label(t(subtitle_key, lang)).classes("text-caption opacity-70")
        if actions is not None:
            with ui.row().classes("items-center gap-2"):
                actions()


@contextmanager
def section_card(
    lang: str = "en",
    *,
    title_key: str | None = None,
    icon: str | None = None,
    extra: str = "",
) -> Iterator[Any]:
    """A consistent section card (replaces ad-hoc ``ui.card().classes("w-full p-3")``).

    Yields the card element. If ``title_key`` is given, renders an icon + title
    header row at the top.
    """
    with ui.card().classes(f"w-full ldi-section-card {extra}".strip()) as card:
        if title_key:
            with ui.row().classes("items-center gap-2 q-mb-sm no-wrap"):
                if icon:
                    ui.icon(icon).classes("ldi-accent")
                ui.label(t(title_key, lang)).classes("text-h6 flex-1")
        yield card


def empty_state(
    icon: str,
    title_key: str,
    hint_key: str,
    lang: str,
    *,
    action_label_key: str | None = None,
    on_action: Callable[[], Any] | None = None,
) -> None:
    """A friendly empty/zero-results panel with an optional call-to-action button."""
    with ui.card().classes("w-full ldi-section-card column items-center text-center q-py-lg"):
        ui.icon(icon).classes("text-5xl opacity-30")
        ui.label(t(title_key, lang)).classes("text-h6 opacity-80 q-mt-sm")
        ui.label(t(hint_key, lang)).classes("text-caption opacity-60")
        if action_label_key and on_action is not None:
            ui.button(t(action_label_key, lang), on_click=on_action).props("color=primary").classes("q-mt-md")


def error_state(
    icon: str,
    title_key: str,
    hint_key: str,
    lang: str,
    *,
    detail: str | None = None,
    on_retry: Callable[[], Any] | None = None,
    retry_key: str = "common.retry",
) -> None:
    """A failure panel mirroring :func:`empty_state`, with an optional retry button.

    ``detail`` shows the raw error text muted under the hint (handy for diagnosing
    LM Studio / network problems without opening the logs).
    """
    with ui.card().classes("w-full ldi-section-card column items-center text-center q-py-lg"):
        ui.icon(icon).classes("text-5xl").style("color: var(--ldi-error); opacity: 0.75;")
        ui.label(t(title_key, lang)).classes("text-h6 q-mt-sm")
        ui.label(t(hint_key, lang)).classes("text-caption opacity-70")
        if detail:
            ui.label(str(detail)).classes("text-caption opacity-50 break-all q-mt-xs").style(
                "max-width: 540px;"
            )
        if on_retry is not None:
            ui.button(t(retry_key, lang), icon="refresh", on_click=on_retry).props("color=primary").classes(
                "q-mt-md"
            )


def skeleton_line(width: str = "100%", *, height: int = 12, mt: int = 0) -> None:
    """A single shimmering placeholder bar (reuses the ``.ldi-skeleton`` animation)."""
    style = f"width: {width}; height: {height}px;"
    if mt:
        style += f" margin-top: {mt}px;"
    ui.element("div").classes("ldi-skeleton ldi-skeleton-text").style(style)


def skeleton_card(*, lines: int = 2, thumb: bool = False) -> None:
    """One card-shaped skeleton: optional thumbnail + a title bar + N text lines."""
    with ui.card().classes("w-full ldi-section-card ldi-static"):
        with ui.row().classes("w-full gap-3 no-wrap items-start"):
            if thumb:
                ui.element("div").classes("ldi-skeleton ldi-skeleton-thumb").style(
                    "width: 82px; height: 106px; flex-shrink: 0;"
                )
            with ui.column().classes("flex-1 gap-2 min-w-0"):
                ui.element("div").classes("ldi-skeleton ldi-skeleton-title").style("width: 45%;")
                for i in range(max(1, lines)):
                    skeleton_line("70%" if i == lines - 1 else "100%")


def skeleton_list(count: int = 3, *, lines: int = 2, thumb: bool = False) -> None:
    """A column of ``count`` card skeletons — drop into a results area while it loads."""
    with ui.column().classes("w-full gap-2"):
        for _ in range(max(1, count)):
            skeleton_card(lines=lines, thumb=thumb)


def breadcrumbs(items: list[tuple[str, str | None]]) -> None:
    """A breadcrumb trail. ``items`` = ``[(label, path_or_None)]``.

    Crumbs with a path render as links; the last crumb (or any with ``path=None``)
    renders as the current, non-clickable page. Labels are passed already-resolved
    (filenames aren't translatable), so this helper takes no ``lang``.
    """
    if not items:
        return
    last = len(items) - 1
    with ui.row().classes("ldi-breadcrumbs"):
        for i, (label, path) in enumerate(items):
            if i:
                ui.label("/").classes("ldi-breadcrumb-sep")
            if path and i != last:
                ui.link(label, path).classes("ldi-breadcrumb")
            else:
                ui.label(label).classes("ldi-breadcrumb current")


def confirm_dialog(
    title_key: str,
    message_key: str | None,
    on_confirm: Callable[[], Any],
    lang: str,
    *,
    danger: bool = False,
    confirm_key: str | None = None,
) -> Any:
    """Open a modal confirm dialog. ``on_confirm`` (sync or async) runs after the
    dialog closes — keep any ``_refresh()`` call inside it, exactly as before."""
    with ui.dialog() as dialog, ui.card().classes("w-[420px] p-4"):
        ui.label(t(title_key, lang)).classes("text-h6 ldi-primary")
        if message_key:
            ui.label(t(message_key, lang)).classes("text-caption opacity-70 q-mt-xs")
        with ui.row().classes("justify-end gap-2 w-full q-mt-md"):
            ui.button(t("common.cancel", lang), on_click=dialog.close).props("flat")

            async def _run() -> None:
                dialog.close()
                result = on_confirm()
                if inspect.isawaitable(result):
                    await result

            label = confirm_key or ("common.delete" if danger else "common.confirm")
            ui.button(t(label, lang), on_click=_run).props(f"color={'negative' if danger else 'primary'}")
    dialog.open()
    return dialog


# Job-status -> (pill class, glyph, i18n label key). Single source of truth for
# the running/paused/queued/completed/aborted/error pills that were duplicated
# across the header, dashboard and sources page.
_STATUS_PILL: dict[str, tuple[str, str, str]] = {
    "running": ("ldi-pill-success", "● ", "status.running"),
    "paused": ("ldi-pill-warning", "⏸ ", "status.paused"),
    "queued": ("ldi-pill", "… ", "status.queued"),
    "pending": ("ldi-pill", "… ", "status.queued"),
    "completed": ("ldi-pill-success", "✓ ", "status.completed"),
    "aborted": ("ldi-pill-warning", "⊘ ", "status.aborted"),
    "error": ("ldi-pill-error", "✕ ", "status.error"),
}


def status_pill(status: Any, lang: str) -> Any:
    """Render a colored status pill for a ``ScanJobStatus`` (or its string value)."""
    key = str(getattr(status, "value", status)).lower()
    cls, glyph, label_key = _STATUS_PILL.get(key, ("ldi-pill", "", ""))
    text = (glyph + t(label_key, lang)) if label_key else key
    return ui.label(text).classes(f"ldi-pill {cls}".strip())


def help_callout(text_key: str, lang: str, *, icon: str = "info") -> None:
    """A subtle contextual hint band (accent left border)."""
    with ui.row().classes("ldi-callout items-start gap-2 w-full"):
        ui.icon(icon).classes("ldi-accent").style("margin-top: 2px;")
        ui.label(t(text_key, lang)).classes("text-caption opacity-80 flex-1")


def stat_card(icon: str, value: Any, label_key: str, lang: str) -> None:
    """A dashboard stat tile (icon + big value + label)."""
    with ui.card().classes("p-4 w-44"), ui.column().classes("ldi-stat gap-0 w-full"):
        ui.icon(icon).classes("text-3xl ldi-accent")
        ui.label(str(value)).classes("text-h4")
        ui.label(t(label_key, lang)).classes("opacity-80")


def kv_meta(pairs: list[tuple[str, Any]]) -> Any:
    """A single muted caption line of ``key: value`` pairs separated by · ."""
    text = "  ·  ".join(f"{k}: {v}" for k, v in pairs if v not in (None, ""))
    return ui.label(text).classes("text-caption opacity-70 break-all")
