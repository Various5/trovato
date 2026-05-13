"""Six built-in UI themes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    name: str
    label: str
    is_dark: bool
    bg: str
    surface: str
    text: str
    primary: str
    accent: str
    border: str


THEMES: dict[str, Theme] = {
    "light": Theme(
        "light",
        "Light Standard",
        False,
        bg="#f6f7fb",
        surface="#ffffff",
        text="#1a1a1a",
        primary="#2563eb",
        accent="#0ea5e9",
        border="#e5e7eb",
    ),
    "dark": Theme(
        "dark",
        "Dark Standard",
        True,
        bg="#0f172a",
        surface="#1e293b",
        text="#e2e8f0",
        primary="#3b82f6",
        accent="#06b6d4",
        border="#334155",
    ),
    "nord": Theme(
        "nord",
        "Nord Dark",
        True,
        bg="#2e3440",
        surface="#3b4252",
        text="#eceff4",
        primary="#88c0d0",
        accent="#81a1c1",
        border="#4c566a",
    ),
    "solarized": Theme(
        "solarized",
        "Solarized Light",
        False,
        bg="#fdf6e3",
        surface="#eee8d5",
        text="#073642",
        primary="#268bd2",
        accent="#b58900",
        border="#93a1a1",
    ),
    "dracula": Theme(
        "dracula",
        "Dracula",
        True,
        bg="#282a36",
        surface="#44475a",
        text="#f8f8f2",
        primary="#bd93f9",
        accent="#ff79c6",
        border="#6272a4",
    ),
    "highcontrast": Theme(
        "highcontrast",
        "High Contrast",
        True,
        bg="#000000",
        surface="#1a1a1a",
        text="#ffffff",
        primary="#ffff00",
        accent="#00ffff",
        border="#ffffff",
    ),
}


def theme_css(theme_name: str) -> str:
    t = THEMES.get(theme_name) or THEMES["dark"]
    return f"""
    :root {{
        --ldi-bg: {t.bg};
        --ldi-surface: {t.surface};
        --ldi-text: {t.text};
        --ldi-primary: {t.primary};
        --ldi-accent: {t.accent};
        --ldi-border: {t.border};
    }}
    body, .q-page, .q-layout {{
        background: var(--ldi-bg) !important;
        color: var(--ldi-text) !important;
    }}
    .q-card, .q-drawer, .q-header, .q-footer {{
        background: var(--ldi-surface) !important;
        color: var(--ldi-text) !important;
    }}
    .q-field__control, .q-field__native, .q-input input {{
        color: var(--ldi-text) !important;
    }}
    .ldi-primary {{ color: var(--ldi-primary); }}
    .ldi-accent  {{ color: var(--ldi-accent); }}
    .ldi-border  {{ border-color: var(--ldi-border) !important; }}
    """
