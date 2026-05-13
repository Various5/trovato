"""Global stylesheet for the modern glassmorphism UI.

A single source of truth for typography, surfaces, motion, and gradient
backgrounds. Per-theme accent colours are still defined in
:mod:`app.ui.themes`; this module just builds the surrounding chrome.
"""

from __future__ import annotations

from app.ui.themes import THEMES, Theme

_FONT_IMPORT = (
    "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;"
    "600;700&family=JetBrains+Mono:wght@400;500&display=swap');"
)


def _background_gradient(theme: Theme) -> str:
    """Pick a soft, slowly-shifting backdrop suited to the theme."""
    if theme.is_dark:
        # Layered radial gradients for depth
        return (
            "radial-gradient(at 20% 0%, hsla(231, 65%, 35%, 0.55) 0px, transparent 55%),"
            "radial-gradient(at 100% 30%, hsla(280, 65%, 35%, 0.40) 0px, transparent 55%),"
            "radial-gradient(at 50% 100%, hsla(190, 70%, 28%, 0.45) 0px, transparent 60%),"
            f"linear-gradient(160deg, {theme.bg} 0%, {theme.surface} 100%)"
        )
    return (
        "radial-gradient(at 10% 10%, hsla(220, 90%, 80%, 0.45) 0px, transparent 55%),"
        "radial-gradient(at 95% 20%, hsla(295, 75%, 80%, 0.30) 0px, transparent 55%),"
        "radial-gradient(at 50% 100%, hsla(200, 80%, 85%, 0.40) 0px, transparent 60%),"
        f"linear-gradient(180deg, {theme.bg} 0%, #ffffff 100%)"
    )


def build_global_css(theme_name: str) -> str:
    """Return the full <style> body to inject for the active theme."""
    t = THEMES.get(theme_name) or THEMES["dark"]
    bg = _background_gradient(t)
    # Glass surface alpha differs slightly between light/dark to keep contrast
    glass_bg = "rgba(255,255,255,0.06)" if t.is_dark else "rgba(255,255,255,0.55)"
    glass_border = "rgba(255,255,255,0.14)" if t.is_dark else "rgba(255,255,255,0.6)"
    glass_shadow = "0 8px 32px 0 rgba(0,0,0,0.4)" if t.is_dark else "0 8px 32px 0 rgba(0,40,90,0.10)"
    hover_glow = (
        "0 0 0 1px rgba(255,255,255,0.18), 0 14px 40px rgba(0,0,0,0.45)"
        if t.is_dark
        else "0 0 0 1px rgba(0,40,90,0.10), 0 14px 40px rgba(0,40,90,0.12)"
    )
    return f"""
{_FONT_IMPORT}

:root {{
  --ldi-bg: {t.bg};
  --ldi-surface: {t.surface};
  --ldi-text: {t.text};
  --ldi-primary: {t.primary};
  --ldi-accent: {t.accent};
  --ldi-border: {t.border};
  --ldi-glass-bg: {glass_bg};
  --ldi-glass-border: {glass_border};
  --ldi-glass-shadow: {glass_shadow};
  --ldi-hover-glow: {hover_glow};
  --ldi-radius: 16px;
  --ldi-radius-sm: 10px;
  --ldi-blur: 22px;
  --ldi-trans: 200ms cubic-bezier(0.4, 0, 0.2, 1);
}}

html, body {{
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
  letter-spacing: -0.005em;
}}

body, .q-layout, .q-page-container, .q-page {{
  background: {bg} !important;
  background-attachment: fixed !important;
  color: var(--ldi-text) !important;
  min-height: 100vh;
}}

/* Default Quasar surfaces become glass cards */
.q-card,
.q-drawer,
.q-header,
.q-footer,
.q-menu,
.q-dialog .q-dialog__inner > div,
.q-table {{
  background: var(--ldi-glass-bg) !important;
  backdrop-filter: blur(var(--ldi-blur)) saturate(180%);
  -webkit-backdrop-filter: blur(var(--ldi-blur)) saturate(180%);
  border: 1px solid var(--ldi-glass-border) !important;
  color: var(--ldi-text) !important;
}}

.q-card {{
  border-radius: var(--ldi-radius) !important;
  box-shadow: var(--ldi-glass-shadow) !important;
  transition: transform var(--ldi-trans), box-shadow var(--ldi-trans),
              border-color var(--ldi-trans);
}}

.q-card:hover {{
  box-shadow: var(--ldi-hover-glow) !important;
}}

/* Drawer */
.q-drawer {{
  border-right: 1px solid var(--ldi-glass-border) !important;
  box-shadow: var(--ldi-glass-shadow) !important;
}}

/* Header / toolbar */
.q-header, .q-toolbar {{
  border-bottom: 1px solid var(--ldi-glass-border) !important;
  box-shadow: 0 1px 18px rgba(0,0,0,0.18) !important;
}}

/* Form fields */
.q-field__control, .q-field__native, .q-field__label, .q-field__marginal {{
  color: var(--ldi-text) !important;
}}
.q-field--outlined .q-field__control:before {{
  border: 1px solid var(--ldi-glass-border) !important;
  border-radius: var(--ldi-radius-sm) !important;
}}
.q-field--outlined .q-field__control:hover:before {{
  border-color: var(--ldi-primary) !important;
}}
.q-field input, .q-field textarea {{
  font-family: 'Inter', sans-serif !important;
}}

/* Buttons */
.q-btn {{
  border-radius: var(--ldi-radius-sm) !important;
  font-weight: 500 !important;
  letter-spacing: 0 !important;
  text-transform: none !important;
  transition: transform var(--ldi-trans), box-shadow var(--ldi-trans),
              background var(--ldi-trans);
}}
.q-btn:hover:not(:disabled) {{
  transform: translateY(-1px);
}}
.q-btn--standard.bg-primary,
.q-btn[color=primary] {{
  background: linear-gradient(135deg, var(--ldi-primary), var(--ldi-accent)) !important;
  color: white !important;
  box-shadow: 0 6px 22px -6px var(--ldi-primary) !important;
}}

/* Markdown / typography in chat answers */
.q-card .markdown-body,
.q-card p {{
  line-height: 1.7;
  color: var(--ldi-text);
}}
code, pre, .q-card code {{
  font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace !important;
  font-size: 0.92em;
  border-radius: 6px;
  padding: 0.12em 0.4em;
  background: rgba(255,255,255,0.07);
}}
pre {{
  padding: 1rem !important;
  border-radius: var(--ldi-radius-sm) !important;
  overflow-x: auto;
  background: rgba(0,0,0,0.30) !important;
  border: 1px solid var(--ldi-glass-border) !important;
}}

/* Scrollbar — slimmer + theme-aware */
::-webkit-scrollbar           {{ width: 8px; height: 8px; }}
::-webkit-scrollbar-track     {{ background: transparent; }}
::-webkit-scrollbar-thumb     {{
  background: var(--ldi-glass-border);
  border-radius: 4px;
}}
::-webkit-scrollbar-thumb:hover {{ background: var(--ldi-primary); }}

/* Selection */
::selection {{
  background: var(--ldi-primary);
  color: white;
}}

/* App-specific helpers */
.ldi-primary {{ color: var(--ldi-primary); }}
.ldi-accent  {{ color: var(--ldi-accent); }}
.ldi-border  {{ border-color: var(--ldi-glass-border) !important; }}
.ldi-glass {{
  background: var(--ldi-glass-bg) !important;
  backdrop-filter: blur(var(--ldi-blur)) saturate(180%);
  -webkit-backdrop-filter: blur(var(--ldi-blur)) saturate(180%);
  border: 1px solid var(--ldi-glass-border);
  border-radius: var(--ldi-radius);
  box-shadow: var(--ldi-glass-shadow);
}}
.ldi-glass-sm {{
  background: var(--ldi-glass-bg);
  backdrop-filter: blur(14px) saturate(160%);
  -webkit-backdrop-filter: blur(14px) saturate(160%);
  border: 1px solid var(--ldi-glass-border);
  border-radius: var(--ldi-radius-sm);
}}
.ldi-pill {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 500;
  background: var(--ldi-glass-bg);
  border: 1px solid var(--ldi-glass-border);
  color: var(--ldi-text);
}}

/* Chat-specific */
.ldi-chat-bubble-user {{
  background: linear-gradient(135deg, var(--ldi-primary), var(--ldi-accent)) !important;
  color: white !important;
  border-radius: 20px 20px 4px 20px !important;
  padding: 14px 18px;
  max-width: 78%;
  margin-left: auto;
  box-shadow: 0 8px 22px -8px var(--ldi-primary);
}}
.ldi-chat-bubble-assistant {{
  background: var(--ldi-glass-bg) !important;
  backdrop-filter: blur(var(--ldi-blur)) saturate(160%);
  border: 1px solid var(--ldi-glass-border) !important;
  border-radius: 20px 20px 20px 4px !important;
  padding: 14px 18px;
  max-width: 92%;
  color: var(--ldi-text) !important;
}}
.ldi-avatar {{
  width: 34px; height: 34px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0;
  flex-shrink: 0;
}}
.ldi-avatar-user {{
  background: linear-gradient(135deg, var(--ldi-primary), var(--ldi-accent));
  color: white;
}}
.ldi-avatar-bot {{
  background: var(--ldi-glass-bg);
  border: 1px solid var(--ldi-glass-border);
  color: var(--ldi-accent);
}}

/* Streaming cursor */
.ldi-stream-cursor::after {{
  content: '▍';
  margin-left: 2px;
  color: var(--ldi-accent);
  animation: ldi-blink 1.1s infinite;
}}
@keyframes ldi-blink {{
  0%, 50% {{ opacity: 1; }}
  51%, 100% {{ opacity: 0.15; }}
}}

/* Source pill row */
.ldi-source-card {{
  display: block;
  padding: 10px 12px;
  border-radius: var(--ldi-radius-sm);
  background: var(--ldi-glass-bg);
  border: 1px solid var(--ldi-glass-border);
  margin-top: 6px;
  cursor: pointer;
  transition: transform var(--ldi-trans), box-shadow var(--ldi-trans);
}}
.ldi-source-card:hover {{
  transform: translateY(-1px);
  box-shadow: var(--ldi-hover-glow);
}}

/* Sidebar nav item */
.ldi-nav-item {{
  width: 100%;
  justify-content: flex-start !important;
  border-radius: var(--ldi-radius-sm) !important;
  padding: 8px 12px !important;
  margin-bottom: 2px;
  transition: background var(--ldi-trans), color var(--ldi-trans);
}}
.ldi-nav-item:hover {{
  background: var(--ldi-glass-bg) !important;
}}
.ldi-nav-item.active {{
  background: linear-gradient(135deg,
    rgba(255,255,255,0.10),
    rgba(255,255,255,0.04)) !important;
  color: var(--ldi-primary) !important;
  border: 1px solid var(--ldi-glass-border) !important;
}}

/* Topbar logo */
.ldi-brand {{
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-weight: 600;
  font-size: 1.05rem;
  color: var(--ldi-text);
  letter-spacing: -0.01em;
}}
.ldi-brand-mark {{
  width: 32px; height: 32px;
  border-radius: 9px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, var(--ldi-primary), var(--ldi-accent));
  color: white;
  font-weight: 700;
  box-shadow: 0 6px 20px -8px var(--ldi-primary);
}}

/* Stat tile (dashboard) */
.ldi-stat {{
  position: relative;
  overflow: hidden;
  padding: 18px 18px 16px 18px;
}}
.ldi-stat::after {{
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  border-radius: var(--ldi-radius);
  background: radial-gradient(circle at 100% 0%,
    rgba(255,255,255,0.10), transparent 60%);
}}

/* Better focus rings (accessibility + polish) */
button:focus-visible, a:focus-visible, .q-btn:focus-visible {{
  outline: 2px solid var(--ldi-primary);
  outline-offset: 2px;
}}

/* Hide the NiceGUI default 'q-py-md' container padding that leaves a strip */
.q-page-container > .q-page {{ padding-top: 12px; }}
"""
