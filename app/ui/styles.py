"""Global stylesheet — a restrained, professional design system.

One calm teal accent over warm-neutral greys. Flat surfaces, 1px borders, soft
shadows, tight radii. No glassmorphism blur, no rainbow gradients, no glows —
colour is reserved for the accent, state, and the primary action.

Every visual value is a CSS custom property resolved per theme, so the
``system`` theme can ship BOTH a light (Paper) and dark (Graphite) variable set
gated on ``prefers-color-scheme`` and switch with the OS — no reload needed.

Motion stays subtle and honours ``prefers-reduced-motion: reduce``.
"""

from __future__ import annotations

from app.ui.themes import SYSTEM_DARK, SYSTEM_LIGHT, THEMES, Theme

_FONT_IMPORT = (
    "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;"
    "600;700&family=JetBrains+Mono:wght@400;500&display=swap');"
)


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return 20, 184, 166  # teal fallback


def _tokens(t: Theme) -> dict[str, str]:
    """Resolve a theme into the full set of design-system CSS variables."""
    ar, ag, ab = _hex_to_rgb(t.accent)
    tr, tg, tb = _hex_to_rgb(t.text)
    muted = t.muted or f"rgba({tr},{tg},{tb},0.62)"
    surface2 = t.surface2 or t.surface

    if t.is_dark:
        hover_bg = "rgba(255,255,255,0.045)"
        subtle_bg = "rgba(255,255,255,0.022)"
        strong_bg = "rgba(255,255,255,0.065)"
        border_strong = "rgba(255,255,255,0.18)"
        shadow = "0 1px 2px rgba(0,0,0,0.40), 0 1px 3px rgba(0,0,0,0.28)"
        shadow_lg = "0 10px 30px -14px rgba(0,0,0,0.65)"
        code_bg = "rgba(255,255,255,0.05)"
        pre_bg = "rgba(0,0,0,0.28)"
    else:
        hover_bg = "rgba(17,20,24,0.035)"
        subtle_bg = "rgba(17,20,24,0.018)"
        strong_bg = "rgba(17,20,24,0.055)"
        border_strong = "rgba(17,20,24,0.16)"
        shadow = "0 1px 2px rgba(16,24,40,0.06), 0 1px 3px rgba(16,24,40,0.05)"
        shadow_lg = "0 10px 30px -16px rgba(16,24,40,0.20)"
        code_bg = "rgba(17,20,24,0.05)"
        pre_bg = "rgba(17,20,24,0.035)"

    focus = f"rgba({ar},{ag},{ab},{0.32 if t.is_dark else 0.20})"
    accent_soft = f"rgba({ar},{ag},{ab},{0.16 if t.is_dark else 0.12})"

    return {
        "--ldi-bg": t.bg,
        "--ldi-surface": t.surface,
        "--ldi-surface-2": surface2,
        "--ldi-text": t.text,
        "--ldi-muted": muted,
        "--ldi-primary": t.primary,
        "--ldi-accent": t.accent,
        "--ldi-accent-soft": accent_soft,
        "--ldi-border": t.border,
        "--ldi-border-strong": border_strong,
        "--ldi-hover-bg": hover_bg,
        "--ldi-subtle-bg": subtle_bg,
        "--ldi-strong-bg": strong_bg,
        "--ldi-shadow": shadow,
        "--ldi-shadow-lg": shadow_lg,
        "--ldi-focus": focus,
        "--ldi-code-bg": code_bg,
        "--ldi-pre-bg": pre_bg,
        "--ldi-success": t.success,
        "--ldi-warn": t.warn,
        "--ldi-error": t.error,
        # ---- Back-compat aliases (older inline styles still reference these) --
        "--ldi-glass-bg": subtle_bg,
        "--ldi-glass-bg-strong": strong_bg,
        "--ldi-glass-border": t.border,
        "--ldi-glass-shadow": shadow,
        "--ldi-hover-glow": shadow_lg,
        "--ldi-focus-glow": focus,
    }


# Theme-independent scale tokens (radii, spacing, motion) — declared once.
_SCALE_TOKENS = """
  --ldi-radius: 12px;
  --ldi-radius-sm: 8px;
  --ldi-radius-lg: 16px;
  --ldi-trans: 150ms cubic-bezier(0.4, 0, 0.2, 1);
  --ldi-trans-expo: 260ms cubic-bezier(0.16, 1, 0.3, 1);
  --ldi-space-1: 4px;
  --ldi-space-2: 8px;
  --ldi-space-3: 12px;
  --ldi-space-4: 16px;
  --ldi-space-5: 24px;
  --ldi-card-pad: 16px;
"""


def _vars_css(t: Theme) -> str:
    return "\n".join(f"  {k}: {v};" for k, v in _tokens(t).items())


def _root_block(theme_name: str) -> str:
    """The ``:root`` variable declarations for a theme. For ``system`` it emits
    the light set plus a ``prefers-color-scheme: dark`` override."""
    if theme_name == "system":
        light = THEMES[SYSTEM_LIGHT]
        dark = THEMES[SYSTEM_DARK]
        return (
            f":root {{\n{_vars_css(light)}\n{_SCALE_TOKENS}}}\n"
            f"@media (prefers-color-scheme: dark) {{\n  :root {{\n{_vars_css(dark)}\n  }}\n}}"
        )
    t = THEMES.get(theme_name) or THEMES[SYSTEM_DARK]
    return f":root {{\n{_vars_css(t)}\n{_SCALE_TOKENS}}}"


def build_global_css(theme_name: str) -> str:
    """Return the full <style> body for the active theme."""
    return _FONT_IMPORT + "\n" + _root_block(theme_name) + "\n" + _STATIC_CSS


# ---------------------------------------------------------------------------
# Static component CSS — references variables only, identical for every theme.
# ---------------------------------------------------------------------------
_STATIC_CSS = """
/* ------------------------------------------------------------------ */
/*  Motion primitives                                                  */
/* ------------------------------------------------------------------ */
@keyframes ldi-fade-in     { from { opacity: 0; } to { opacity: 1; } }
@keyframes ldi-fade-in-up  { from { opacity: 0; transform: translateY(6px); }
                             to   { opacity: 1; transform: translateY(0); } }
@keyframes ldi-scale-in    { from { opacity: 0; transform: scale(0.99); }
                             to   { opacity: 1; transform: scale(1); } }
@keyframes ldi-shimmer     { from { background-position: -200% 0; }
                             to   { background-position:  200% 0; } }
@keyframes ldi-pulse-dot   { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
@keyframes ldi-blink       { 0%, 50% { opacity: 1; } 51%, 100% { opacity: 0.15; } }
@keyframes ldi-indet       { 0% { left: -35%; } 100% { left: 100%; } }

/* ------------------------------------------------------------------ */
/*  Base                                                                */
/* ------------------------------------------------------------------ */
html { scroll-behavior: smooth; }
html, body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
  letter-spacing: -0.004em;
}
body, .q-layout, .q-page-container, .q-page {
  background: var(--ldi-bg) !important;
  color: var(--ldi-text) !important;
  min-height: 100vh;
}
.q-page-container > .q-page {
  animation: ldi-fade-in-up 240ms cubic-bezier(0.16, 1, 0.3, 1) both;
  padding-top: 12px;
}

/* ------------------------------------------------------------------ */
/*  Surfaces — flat, bordered, softly shadowed (no blur)               */
/* ------------------------------------------------------------------ */
.q-card,
.q-drawer,
.q-header,
.q-footer,
.q-menu,
.q-dialog .q-dialog__inner > div,
.q-table {
  background: var(--ldi-surface) !important;
  border: 1px solid var(--ldi-border) !important;
  color: var(--ldi-text) !important;
}
.q-card {
  border-radius: var(--ldi-radius) !important;
  box-shadow: var(--ldi-shadow) !important;
  transition: border-color var(--ldi-trans), box-shadow var(--ldi-trans);
}
.q-card:hover { border-color: var(--ldi-border-strong) !important; }
/* Centered / dialog cards must not react to hover */
.q-card.absolute-center,
.q-card.ldi-static,
.q-dialog .q-card { box-shadow: var(--ldi-shadow) !important; }
.q-card.absolute-center:hover,
.q-card.ldi-static:hover,
.q-dialog .q-card:hover { border-color: var(--ldi-border) !important; }

.q-drawer { border-right: 1px solid var(--ldi-border) !important; box-shadow: none !important;
            background: var(--ldi-bg) !important; }
.q-header, .q-toolbar {
  background: var(--ldi-bg) !important;
  border-bottom: 1px solid var(--ldi-border) !important;
  box-shadow: none !important;
}
.q-menu, .q-dialog .q-dialog__inner > div {
  animation: ldi-scale-in 160ms cubic-bezier(0.16, 1, 0.3, 1) both;
  border-radius: var(--ldi-radius) !important;
  box-shadow: var(--ldi-shadow-lg) !important;
}

/* ------------------------------------------------------------------ */
/*  Fields                                                              */
/* ------------------------------------------------------------------ */
.q-field__control, .q-field__native, .q-field__label, .q-field__marginal {
  color: var(--ldi-text) !important;
}
.q-field--outlined .q-field__control:before {
  border: 1px solid var(--ldi-border) !important;
  border-radius: var(--ldi-radius-sm) !important;
  transition: border-color var(--ldi-trans);
}
.q-field--outlined .q-field__control { background: var(--ldi-surface-2) !important;
  border-radius: var(--ldi-radius-sm) !important; }
.q-field--outlined .q-field__control:hover:before { border-color: var(--ldi-border-strong) !important; }
.q-field--outlined .q-field__control:focus-within:before {
  border-color: var(--ldi-primary) !important;
  box-shadow: 0 0 0 3px var(--ldi-focus);
}
.q-field input, .q-field textarea { font-family: 'Inter', sans-serif !important; }

/* ------------------------------------------------------------------ */
/*  Buttons                                                             */
/* ------------------------------------------------------------------ */
.q-btn {
  border-radius: var(--ldi-radius-sm) !important;
  font-weight: 500 !important;
  letter-spacing: 0 !important;
  text-transform: none !important;
  transition: background var(--ldi-trans), color var(--ldi-trans),
              border-color var(--ldi-trans), filter var(--ldi-trans);
}
.q-btn--standard.bg-primary,
.q-btn[color=primary] {
  background: var(--ldi-primary) !important;
  color: #ffffff !important;
  box-shadow: none !important;
}
.q-btn--standard.bg-primary:hover,
.q-btn[color=primary]:hover { filter: brightness(1.06); }
.q-btn--outline { border: 1px solid var(--ldi-border) !important; }
.q-btn--round.q-btn--flat:hover,
.q-btn--flat.q-btn--dense:hover { background: var(--ldi-hover-bg) !important; }

/* ------------------------------------------------------------------ */
/*  Typography                                                          */
/* ------------------------------------------------------------------ */
.q-card .markdown-body, .q-card p { line-height: 1.65; color: var(--ldi-text); }
code, pre, .q-card code {
  font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace !important;
  font-size: 0.9em;
  border-radius: 6px;
  padding: 0.12em 0.4em;
  background: var(--ldi-code-bg);
}
pre {
  padding: 1rem !important;
  border-radius: var(--ldi-radius-sm) !important;
  overflow-x: auto;
  background: var(--ldi-pre-bg) !important;
  border: 1px solid var(--ldi-border) !important;
}
.ldi-hero-text { color: var(--ldi-text); font-weight: 700; letter-spacing: -0.02em; }

/* ------------------------------------------------------------------ */
/*  Scrollbar & selection                                               */
/* ------------------------------------------------------------------ */
::-webkit-scrollbar       { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
  background: var(--ldi-border-strong);
  border-radius: 6px;
  border: 2px solid transparent;
  background-clip: padding-box;
  transition: background var(--ldi-trans);
}
::-webkit-scrollbar-thumb:hover { background: var(--ldi-muted); background-clip: padding-box; }
::selection { background: var(--ldi-accent-soft); color: var(--ldi-text); }

/* ------------------------------------------------------------------ */
/*  Utility classes                                                     */
/* ------------------------------------------------------------------ */
.ldi-primary { color: var(--ldi-primary); }
.ldi-accent  { color: var(--ldi-accent); }
.ldi-border  { border-color: var(--ldi-border) !important; }
.ldi-muted   { color: var(--ldi-muted); }

.ldi-glass {
  background: var(--ldi-surface) !important;
  border: 1px solid var(--ldi-border);
  border-radius: var(--ldi-radius);
  box-shadow: var(--ldi-shadow);
}
.ldi-glass-sm {
  background: var(--ldi-surface);
  border: 1px solid var(--ldi-border);
  border-radius: var(--ldi-radius-sm);
}
.ldi-section-card { padding: var(--ldi-card-pad); }

.ldi-callout {
  display: flex;
  gap: 8px;
  padding: 10px 12px;
  border-radius: var(--ldi-radius-sm);
  background: var(--ldi-subtle-bg);
  border: 1px solid var(--ldi-border);
  border-left: 3px solid var(--ldi-accent);
}

.ldi-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 9px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 500;
  background: var(--ldi-subtle-bg);
  border: 1px solid var(--ldi-border);
  color: var(--ldi-text);
  transition: background var(--ldi-trans), border-color var(--ldi-trans);
}
.ldi-pill:hover { border-color: var(--ldi-border-strong); }
.ldi-pill-accent { background: var(--ldi-accent-soft); border-color: transparent; color: var(--ldi-primary); }
.ldi-pill-warning { background: rgba(245,158,11,0.13); border-color: rgba(245,158,11,0.32); color: var(--ldi-warn); }
.ldi-pill-success { background: rgba(34,197,94,0.13);  border-color: rgba(34,197,94,0.32);  color: var(--ldi-success); }
.ldi-pill-error   { background: rgba(239,68,68,0.13);  border-color: rgba(239,68,68,0.32);  color: var(--ldi-error); }

/* Live status dot */
.ldi-status-dot {
  display: inline-block;
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--ldi-muted);
  margin-right: 6px;
  vertical-align: middle;
}
.ldi-status-dot.live, .ldi-status-dot.success { background: var(--ldi-success);
  animation: ldi-pulse-dot 1.8s ease-in-out infinite; }
.ldi-status-dot.warning { background: var(--ldi-warn); }
.ldi-status-dot.error   { background: var(--ldi-error); }
.ldi-status-dot.idle    { background: var(--ldi-border-strong); }

/* Skeleton shimmer for loading states */
.ldi-skeleton {
  background: linear-gradient(90deg,
    var(--ldi-subtle-bg) 0%, var(--ldi-hover-bg) 50%, var(--ldi-subtle-bg) 100%);
  background-size: 200% 100%;
  animation: ldi-shimmer 1.5s ease infinite;
  border-radius: var(--ldi-radius-sm);
  height: 16px;
  border: none !important;
}
/* Skeleton shape modifiers — compose with .ldi-skeleton */
.ldi-skeleton-title  { height: 20px; }
.ldi-skeleton-text   { height: 12px; }
.ldi-skeleton-circle { border-radius: 50% !important; }
.ldi-skeleton-thumb  { border-radius: var(--ldi-radius-sm); }

/* ------------------------------------------------------------------ */
/*  Progress bar (solid, subtle)                                        */
/* ------------------------------------------------------------------ */
.ldi-progress {
  width: 100%; height: 6px;
  background: var(--ldi-subtle-bg);
  border: 1px solid var(--ldi-border);
  border-radius: 999px;
  overflow: hidden;
  position: relative;
}
.ldi-progress-fill {
  height: 100%;
  background: var(--ldi-primary);
  border-radius: 999px;
  transition: width 300ms cubic-bezier(0.16, 1, 0.3, 1);
}
.ldi-progress-fill.indeterminate {
  width: 35% !important;
  position: absolute;
  background: var(--ldi-primary);
  opacity: 0.8;
  animation: ldi-indet 1.4s infinite ease-in-out;
}

/* ------------------------------------------------------------------ */
/*  Chat                                                                */
/* ------------------------------------------------------------------ */
.ldi-chat-bubble-user,
.ldi-chat-bubble-assistant { animation: ldi-fade-in-up 200ms cubic-bezier(0.16, 1, 0.3, 1) both; }
.ldi-chat-bubble-user {
  background: var(--ldi-primary) !important;
  color: #ffffff !important;
  border-radius: 14px 14px 4px 14px !important;
  padding: 11px 14px;
  max-width: 78%;
  margin-left: auto;
  line-height: 1.55;
}
.ldi-chat-bubble-assistant {
  background: var(--ldi-surface) !important;
  border: 1px solid var(--ldi-border) !important;
  border-radius: 14px 14px 14px 4px !important;
  padding: 11px 14px;
  max-width: 92%;
  color: var(--ldi-text) !important;
  line-height: 1.65;
}
.ldi-avatar {
  width: 30px; height: 30px;
  border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 13px; font-weight: 600; flex-shrink: 0;
}
.ldi-avatar-user { background: var(--ldi-primary); color: #ffffff; }
.ldi-avatar-bot  { background: var(--ldi-accent-soft); border: 1px solid var(--ldi-border); color: var(--ldi-primary); }
.ldi-stream-cursor::after {
  content: '▍'; margin-left: 2px; color: var(--ldi-accent);
  animation: ldi-blink 1.1s infinite;
}

.ldi-source-card {
  display: block;
  padding: 9px 12px;
  border-radius: var(--ldi-radius-sm);
  background: var(--ldi-surface);
  border: 1px solid var(--ldi-border);
  margin-top: 6px;
  cursor: pointer;
  transition: border-color 150ms ease, background 150ms ease;
}
.ldi-source-card:hover { border-color: var(--ldi-border-strong); background: var(--ldi-hover-bg); }

/* ------------------------------------------------------------------ */
/*  Navigation                                                          */
/* ------------------------------------------------------------------ */
.ldi-nav-item {
  position: relative;
  width: 100%;
  justify-content: flex-start !important;
  border-radius: var(--ldi-radius-sm) !important;
  padding: 7px 12px !important;
  margin-bottom: 1px;
  color: var(--ldi-muted) !important;
  transition: background var(--ldi-trans), color var(--ldi-trans);
}
.ldi-nav-item:hover { background: var(--ldi-hover-bg) !important; color: var(--ldi-text) !important; }
.ldi-nav-item.active {
  background: var(--ldi-accent-soft) !important;
  color: var(--ldi-primary) !important;
}
.ldi-nav-item.active::before {
  content: '';
  position: absolute;
  left: 0; top: 20%; bottom: 20%;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--ldi-primary);
}

/* ------------------------------------------------------------------ */
/*  Command palette (Ctrl/⌘+K)                                          */
/* ------------------------------------------------------------------ */
.ldi-cmdk { width: 560px; max-width: 92vw; padding: 0 !important; overflow: hidden; }
.ldi-cmdk-search {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 16px; border-bottom: 1px solid var(--ldi-border);
}
.ldi-cmdk-search .q-field { flex: 1; }
.ldi-cmdk-list { max-height: 56vh; overflow-y: auto; padding: 6px; }
.ldi-cmdk-group {
  font-size: 11px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--ldi-muted); padding: 10px 12px 4px;
}
.ldi-cmdk-item {
  display: flex; align-items: center; gap: 12px;
  padding: 9px 12px; border-radius: var(--ldi-radius-sm);
  color: var(--ldi-text); cursor: pointer;
  transition: background var(--ldi-trans), color var(--ldi-trans);
}
.ldi-cmdk-item:hover { background: var(--ldi-hover-bg); }
.ldi-cmdk-item.active { background: var(--ldi-accent-soft); color: var(--ldi-primary); }
.ldi-cmdk-item.active .ldi-cmdk-icon { color: var(--ldi-primary); }
.ldi-cmdk-icon  { color: var(--ldi-muted); font-size: 20px; flex-shrink: 0; }
.ldi-cmdk-label { flex: 1; font-size: 14px; }
.ldi-cmdk-hint  { font-size: 11px; color: var(--ldi-muted); }
.ldi-cmdk-empty { padding: 28px; text-align: center; color: var(--ldi-muted); }
.ldi-cmdk-footer {
  display: flex; align-items: center; gap: 16px;
  padding: 8px 14px; border-top: 1px solid var(--ldi-border);
  font-size: 11px; color: var(--ldi-muted);
}
.ldi-kbd {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 18px; height: 18px; padding: 0 5px;
  border-radius: 5px; border: 1px solid var(--ldi-border);
  background: var(--ldi-code-bg); color: var(--ldi-muted);
  font-family: 'JetBrains Mono', 'Cascadia Code', monospace; font-size: 10.5px; font-weight: 500;
}
.ldi-cmdk-trigger {
  display: inline-flex; align-items: center; gap: 8px;
  height: 34px; padding: 0 10px;
  border-radius: var(--ldi-radius-sm);
  border: 1px solid var(--ldi-border); background: var(--ldi-surface-2);
  color: var(--ldi-muted); cursor: pointer; font-size: 13px;
  transition: border-color var(--ldi-trans), color var(--ldi-trans);
}
.ldi-cmdk-trigger:hover { border-color: var(--ldi-border-strong); color: var(--ldi-text); }

/* ------------------------------------------------------------------ */
/*  Breadcrumbs                                                         */
/* ------------------------------------------------------------------ */
.ldi-breadcrumbs {
  display: flex; align-items: center; flex-wrap: wrap; gap: 6px;
  font-size: 13px; margin-bottom: 10px; color: var(--ldi-muted);
}
.ldi-breadcrumb {
  color: var(--ldi-muted); text-decoration: none; cursor: pointer;
  display: inline-flex; align-items: center; gap: 5px;
  transition: color var(--ldi-trans);
}
.ldi-breadcrumb:hover { color: var(--ldi-text); }
.ldi-breadcrumb.current { color: var(--ldi-text); font-weight: 500; cursor: default; }
.ldi-breadcrumb-sep { color: var(--ldi-border-strong); user-select: none; }

/* ------------------------------------------------------------------ */
/*  Tabs (settings, etc.)                                               */
/* ------------------------------------------------------------------ */
.q-tabs { color: var(--ldi-muted); border-bottom: 1px solid var(--ldi-border); }
.q-tab { text-transform: none !important; letter-spacing: 0 !important; }
.q-tab--active { color: var(--ldi-primary) !important; }
.q-tab .q-tab__indicator { background: var(--ldi-primary) !important; }
.q-tab-panels, .q-tab-panel { background: transparent !important; }

/* ------------------------------------------------------------------ */
/*  Brand                                                               */
/* ------------------------------------------------------------------ */
.ldi-brand {
  display: inline-flex; align-items: center; gap: 10px;
  font-weight: 600; font-size: 1.05rem; color: var(--ldi-text);
}
.ldi-brand-mark {
  width: 30px; height: 30px;
  border-radius: 8px;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--ldi-primary);
  color: #ffffff; font-weight: 700; font-size: 15px;
}

/* ------------------------------------------------------------------ */
/*  Stats / dashboard                                                   */
/* ------------------------------------------------------------------ */
.ldi-stat { position: relative; padding: 16px; }

/* ------------------------------------------------------------------ */
/*  Focus visibility                                                    */
/* ------------------------------------------------------------------ */
button:focus-visible, a:focus-visible, .q-btn:focus-visible {
  outline: 2px solid var(--ldi-primary);
  outline-offset: 2px;
}

/* ------------------------------------------------------------------ */
/*  Search highlight                                                    */
/* ------------------------------------------------------------------ */
.ldi-mark {
  background: var(--ldi-accent-soft);
  color: var(--ldi-primary);
  padding: 0.5px 3px;
  border-radius: 3px;
  font-weight: 600;
}
.ldi-snippet { font-size: 14px; line-height: 1.55; color: var(--ldi-text); opacity: 0.92; }

/* ------------------------------------------------------------------ */
/*  Rendered markdown (summaries, chat answers)                         */
/* ------------------------------------------------------------------ */
.nicegui-markdown h1 { font-size: 1.4rem;  line-height: 1.3; margin: 0.6em 0 0.3em; }
.nicegui-markdown h2 { font-size: 1.18rem; line-height: 1.3; margin: 0.6em 0 0.3em; }
.nicegui-markdown h3 { font-size: 1.04rem; line-height: 1.3; margin: 0.5em 0 0.25em; }
.nicegui-markdown h4, .nicegui-markdown h5, .nicegui-markdown h6 { font-size: 1rem; margin: 0.4em 0 0.2em; }
.nicegui-markdown p { margin: 0.35em 0; line-height: 1.55; }
.nicegui-markdown ul, .nicegui-markdown ol { margin: 0.3em 0; padding-left: 1.3em; }
.nicegui-markdown li { margin: 0.15em 0; }
.nicegui-markdown a { color: var(--ldi-primary); }
.nicegui-markdown pre, .nicegui-markdown code { white-space: pre-wrap; word-break: break-word; }
.nicegui-markdown table { display: block; overflow-x: auto; max-width: 100%; }
.ldi-prose { max-height: 62vh; overflow: auto; }

/* ------------------------------------------------------------------ */
/*  Citation pill in chat                                               */
/* ------------------------------------------------------------------ */
.ldi-citation-ref {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 20px; height: 20px;
  padding: 0 6px; margin: 0 2px;
  border-radius: 6px;
  background: var(--ldi-accent-soft);
  color: var(--ldi-primary);
  font-size: 11px; font-weight: 600;
  text-decoration: none; cursor: pointer;
  transition: background var(--ldi-trans), color var(--ldi-trans);
  vertical-align: baseline;
}
.ldi-citation-ref:hover { background: var(--ldi-primary); color: #ffffff; }

/* ------------------------------------------------------------------ */
/*  Dashboard mini-chart bars                                           */
/* ------------------------------------------------------------------ */
.ldi-chart-bar {
  flex: 1; min-width: 6px;
  background: var(--ldi-primary);
  border-radius: 3px 3px 0 0;
  opacity: 0.85;
  transition: opacity var(--ldi-trans);
  transform-origin: bottom;
}
.ldi-chart-bar:hover { opacity: 1; }

/* ------------------------------------------------------------------ */
/*  Tooltip                                                             */
/* ------------------------------------------------------------------ */
.q-tooltip {
  background: var(--ldi-surface) !important;
  color: var(--ldi-text) !important;
  border-radius: 6px !important;
  font-size: 12px !important;
  letter-spacing: 0 !important;
  padding: 5px 8px !important;
  border: 1px solid var(--ldi-border);
  box-shadow: var(--ldi-shadow);
}

.q-spinner { animation-duration: 0.9s !important; color: var(--ldi-primary); }

/* ------------------------------------------------------------------ */
/*  Reduced motion                                                      */
/* ------------------------------------------------------------------ */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.001ms !important;
    scroll-behavior: auto !important;
  }
}
"""
