"""Built-in UI themes.

The design system is deliberately restrained and professional: warm-neutral
greys with a single calm teal accent, flat surfaces, 1px borders, soft shadows
— no rainbow gradients or glows. The default is ``system``, which follows the
operating system's light/dark preference (Graphite dark / Paper light). The
older vivid themes (Emerald, Indigo, Nord, Dracula, …) stay selectable.
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
    # Extended design-system tokens (optional; sensible fallbacks computed in
    # styles.py when left blank so legacy themes keep working unchanged).
    muted: str = ""  # secondary/label text
    surface2: str = ""  # elevated surface (inputs, hovered rows)
    success: str = "#22c55e"
    warn: str = "#f59e0b"
    error: str = "#ef4444"


# The light/dark pair the ``system`` theme switches between via
# ``prefers-color-scheme``. Keep both using the same teal accent so the brand
# identity is stable across modes.
SYSTEM_LIGHT = "paper"
SYSTEM_DARK = "graphite"

THEMES: dict[str, Theme] = {
    # ----- Graphite + Teal (the professional default pair) ---------------
    "graphite": Theme(
        "graphite",
        "Graphite (dark)",
        True,
        bg="#111418",
        surface="#1a1f24",
        text="#e7e5e4",
        primary="#14b8a6",
        accent="#2dd4bf",
        border="#2a2f36",
        muted="#9ca3af",
        surface2="#20262e",
    ),
    "paper": Theme(
        "paper",
        "Paper (light)",
        False,
        bg="#fafaf9",
        surface="#ffffff",
        text="#18181b",
        primary="#0d9488",
        accent="#14b8a6",
        border="#e7e5e4",
        muted="#6b7280",
        surface2="#f4f4f3",
    ),
    # ----- Alternate restrained palettes ---------------------------------
    "slate": Theme(
        "slate",
        "Slate (dark)",
        True,
        bg="#0f1419",
        surface="#171d25",
        text="#d9dce1",
        primary="#5b8def",
        accent="#7da4ff",
        border="#262d38",
        muted="#8b94a3",
        surface2="#1d2530",
    ),
    "pearl": Theme(
        "pearl",
        "Pearl (light)",
        False,
        bg="#f6f7f9",
        surface="#ffffff",
        text="#1c1f24",
        primary="#3a5ee0",
        accent="#5b8def",
        border="#e3e6ec",
        muted="#6a7280",
        surface2="#f0f2f5",
    ),
    "obsidian": Theme(
        "obsidian",
        "Obsidian (dark)",
        True,
        bg="#08090c",
        surface="#101218",
        text="#cdd0d6",
        primary="#7d9bff",
        accent="#9bb2ff",
        border="#1c1f27",
        muted="#838995",
        surface2="#161922",
    ),
    # ----- Bold identities (vivid accent — opt-in) -----------------------
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
        muted="#9095b8",
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
        muted="#8aa39a",
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
        muted="#8b94ad",
    ),
    # ----- Legacy / alternative themes -----------------------------------
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
        muted="#667085",
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
        muted="#94a3b8",
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
        muted="#a9b3c4",
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
        muted="#586e75",
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
        muted="#b9c0e0",
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
        muted="#d0d0d0",
    ),
}


# ``system`` is a meta-theme: it has no colours of its own — styles.py emits both
# the Paper (light) and Graphite (dark) variable sets gated on the OS preference.
# A placeholder entry keeps it selectable in the theme list and cycle.
THEMES["system"] = Theme(
    "system",
    "System (auto light/dark)",
    THEMES[SYSTEM_DARK].is_dark,
    bg=THEMES[SYSTEM_DARK].bg,
    surface=THEMES[SYSTEM_DARK].surface,
    text=THEMES[SYSTEM_DARK].text,
    primary=THEMES[SYSTEM_DARK].primary,
    accent=THEMES[SYSTEM_DARK].accent,
    border=THEMES[SYSTEM_DARK].border,
    muted=THEMES[SYSTEM_DARK].muted,
    surface2=THEMES[SYSTEM_DARK].surface2,
)


DEFAULT_THEME = "system"

# Order the theme picker / cycle button walk through: the recommended pair
# first, then restrained alternates, then the bold identities and legacy ones.
THEME_ORDER = [
    "system",
    "graphite",
    "paper",
    "slate",
    "pearl",
    "obsidian",
    "indigo",
    "emerald",
    "royal",
    "light",
    "dark",
    "nord",
    "solarized",
    "dracula",
    "highcontrast",
]
