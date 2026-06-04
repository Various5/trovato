"""Built-in UI themes.

The default palette favours muted, professional tones — a single accent colour
per theme, neutral surfaces, restrained gradients. The original colourful
themes (Nord, Dracula, etc.) are still selectable but no longer the default.
"""

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
    # ----- Professional defaults -------------------------------------
    "slate": Theme(
        "slate",
        "Slate (recommended)",
        True,
        bg="#0f1419",
        surface="#171d25",
        text="#d9dce1",
        primary="#5b8def",
        accent="#7da4ff",
        border="#262d38",
    ),
    "pearl": Theme(
        "pearl",
        "Pearl",
        False,
        bg="#f6f7f9",
        surface="#ffffff",
        text="#1c1f24",
        primary="#3a5ee0",
        accent="#5b8def",
        border="#e3e6ec",
    ),
    "obsidian": Theme(
        "obsidian",
        "Obsidian",
        True,
        bg="#08090c",
        surface="#101218",
        text="#cdd0d6",
        primary="#7d9bff",
        accent="#9bb2ff",
        border="#1c1f27",
    ),
    "graphite": Theme(
        "graphite",
        "Graphite",
        True,
        bg="#15171b",
        surface="#1d2026",
        text="#d0d3d9",
        primary="#6f7a8a",
        accent="#a1abbb",
        border="#262a31",
    ),
    # ----- Bold identities (vivid accent, deep surfaces) -----
    "indigo": Theme(
        "indigo",
        "Indigo (bold)",
        True,
        bg="#0e0f1a",
        surface="#16182a",
        text="#e3e4f2",
        primary="#6366f1",
        accent="#a78bfa",
        border="#272b48",
    ),
    "emerald": Theme(
        "emerald",
        "Emerald (bold)",
        True,
        bg="#0a0f0d",
        surface="#111816",
        text="#d7e0db",
        primary="#10b981",
        accent="#34d399",
        border="#1d2a26",
    ),
    "royal": Theme(
        "royal",
        "Royal (bold)",
        True,
        bg="#0b1020",
        surface="#131a2e",
        text="#dde3ee",
        primary="#3b82f6",
        accent="#22d3ee",
        border="#21283f",
    ),
    # ----- Legacy / alternative themes (kept for users who prefer them) -----
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


DEFAULT_THEME = "emerald"


def theme_css(theme_name: str) -> str:
    t = THEMES.get(theme_name) or THEMES[DEFAULT_THEME]
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
