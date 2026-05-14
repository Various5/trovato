"""Global stylesheet for the modern, restrained UI.

A single source of truth for typography, surfaces, motion, and gradient
backgrounds. The look is deliberately neutral — soft greys with a single
accent — not "vibe-coded" rainbow.
"""

from __future__ import annotations

from app.ui.themes import THEMES, Theme

_FONT_IMPORT = (
    "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;"
    "600;700&family=JetBrains+Mono:wght@400;500&display=swap');"
)


def _background_layers(theme: Theme) -> str:
    """A very subtle 'depth' backdrop — one faint accent halo on dark themes,
    flat near-white on light. No rainbow gradients."""
    if theme.is_dark:
        return (
            "radial-gradient(at 0% 0%, rgba(255,255,255,0.04) 0px, transparent 55%),"
            "radial-gradient(at 100% 100%, rgba(255,255,255,0.025) 0px, transparent 60%),"
            f"linear-gradient(180deg, {theme.bg} 0%, {theme.surface} 100%)"
        )
    return (
        "radial-gradient(at 0% 0%, rgba(15,40,90,0.04) 0px, transparent 55%),"
        "radial-gradient(at 100% 100%, rgba(15,40,90,0.025) 0px, transparent 60%),"
        f"linear-gradient(180deg, {theme.bg} 0%, #ffffff 100%)"
    )


def build_global_css(theme_name: str) -> str:
    """Return the full <style> body for the active theme."""
    t = THEMES.get(theme_name) or THEMES["slate"]
    bg = _background_layers(t)
    if t.is_dark:
        glass_bg = "rgba(255,255,255,0.025)"
        glass_border = "rgba(255,255,255,0.08)"
        glass_shadow = "0 2px 12px rgba(0,0,0,0.35), 0 0 0 1px rgba(255,255,255,0.03)"
        hover_glow = "0 0 0 1px rgba(255,255,255,0.12), 0 8px 24px rgba(0,0,0,0.45)"
        bubble_assistant_bg = "rgba(255,255,255,0.04)"
        code_bg = "rgba(255,255,255,0.05)"
        pre_bg = "rgba(0,0,0,0.30)"
    else:
        glass_bg = "rgba(255,255,255,0.85)"
        glass_border = "rgba(15,30,55,0.08)"
        glass_shadow = "0 2px 10px rgba(15,30,55,0.06), 0 0 0 1px rgba(15,30,55,0.03)"
        hover_glow = "0 0 0 1px rgba(15,30,55,0.10), 0 8px 24px rgba(15,30,55,0.10)"
        bubble_assistant_bg = "rgba(255,255,255,0.90)"
        code_bg = "rgba(15,30,55,0.05)"
        pre_bg = "rgba(15,30,55,0.04)"

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
  --ldi-radius: 12px;
  --ldi-radius-sm: 8px;
  --ldi-blur: 18px;
  --ldi-trans: 180ms cubic-bezier(0.4, 0, 0.2, 1);
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

/* Surfaces are subtly glassy, not rainbow */
.q-card,
.q-drawer,
.q-header,
.q-footer,
.q-menu,
.q-dialog .q-dialog__inner > div,
.q-table {{
  background: var(--ldi-glass-bg) !important;
  backdrop-filter: blur(var(--ldi-blur)) saturate(140%);
  -webkit-backdrop-filter: blur(var(--ldi-blur)) saturate(140%);
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

.q-drawer {{
  border-right: 1px solid var(--ldi-glass-border) !important;
  box-shadow: none !important;
}}

.q-header, .q-toolbar {{
  border-bottom: 1px solid var(--ldi-glass-border) !important;
  box-shadow: 0 1px 0 var(--ldi-glass-border) !important;
}}

.q-field__control, .q-field__native, .q-field__label, .q-field__marginal {{
  color: var(--ldi-text) !important;
}}
.q-field--outlined .q-field__control:before {{
  border: 1px solid var(--ldi-glass-border) !important;
  border-radius: var(--ldi-radius-sm) !important;
}}
.q-field--outlined .q-field__control:hover:before,
.q-field--outlined .q-field__control:focus-within:before {{
  border-color: var(--ldi-primary) !important;
}}
.q-field input, .q-field textarea {{
  font-family: 'Inter', sans-serif !important;
}}

/* Buttons — restrained */
.q-btn {{
  border-radius: var(--ldi-radius-sm) !important;
  font-weight: 500 !important;
  letter-spacing: 0 !important;
  text-transform: none !important;
  transition: background var(--ldi-trans), color var(--ldi-trans),
              box-shadow var(--ldi-trans);
}}
.q-btn--standard.bg-primary,
.q-btn[color=primary] {{
  background: var(--ldi-primary) !important;
  color: white !important;
  box-shadow: 0 1px 0 rgba(0,0,0,0.12), 0 6px 20px -8px var(--ldi-primary) !important;
}}
.q-btn--standard.bg-primary:hover,
.q-btn[color=primary]:hover {{
  filter: brightness(1.08);
}}

/* Markdown / typography */
.q-card .markdown-body, .q-card p {{ line-height: 1.7; color: var(--ldi-text); }}
code, pre, .q-card code {{
  font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace !important;
  font-size: 0.92em;
  border-radius: 6px;
  padding: 0.12em 0.4em;
  background: {code_bg};
}}
pre {{
  padding: 1rem !important;
  border-radius: var(--ldi-radius-sm) !important;
  overflow-x: auto;
  background: {pre_bg} !important;
  border: 1px solid var(--ldi-glass-border) !important;
}}

/* Slim scrollbar */
::-webkit-scrollbar           {{ width: 8px; height: 8px; }}
::-webkit-scrollbar-track     {{ background: transparent; }}
::-webkit-scrollbar-thumb     {{ background: var(--ldi-glass-border); border-radius: 4px; }}
::-webkit-scrollbar-thumb:hover {{ background: var(--ldi-primary); }}

::selection {{ background: var(--ldi-primary); color: white; }}

/* App-specific helpers */
.ldi-primary {{ color: var(--ldi-primary); }}
.ldi-accent  {{ color: var(--ldi-accent); }}
.ldi-border  {{ border-color: var(--ldi-glass-border) !important; }}
.ldi-muted   {{ color: var(--ldi-text); opacity: 0.65; }}
.ldi-glass {{
  background: var(--ldi-glass-bg) !important;
  backdrop-filter: blur(var(--ldi-blur)) saturate(140%);
  -webkit-backdrop-filter: blur(var(--ldi-blur)) saturate(140%);
  border: 1px solid var(--ldi-glass-border);
  border-radius: var(--ldi-radius);
  box-shadow: var(--ldi-glass-shadow);
}}
.ldi-glass-sm {{
  background: var(--ldi-glass-bg);
  backdrop-filter: blur(12px) saturate(140%);
  -webkit-backdrop-filter: blur(12px) saturate(140%);
  border: 1px solid var(--ldi-glass-border);
  border-radius: var(--ldi-radius-sm);
}}
.ldi-pill {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 9px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 500;
  background: var(--ldi-glass-bg);
  border: 1px solid var(--ldi-glass-border);
  color: var(--ldi-text);
}}
.ldi-pill-warning {{
  background: rgba(220, 130, 30, 0.14);
  border-color: rgba(220, 130, 30, 0.35);
  color: #ffb86c;
}}
.ldi-pill-success {{
  background: rgba(50, 180, 130, 0.14);
  border-color: rgba(50, 180, 130, 0.35);
  color: #6ce5b0;
}}
.ldi-pill-error {{
  background: rgba(230, 70, 70, 0.14);
  border-color: rgba(230, 70, 70, 0.35);
  color: #ff8b8b;
}}

/* Progress bar */
.ldi-progress {{
  width: 100%;
  height: 6px;
  background: var(--ldi-glass-border);
  border-radius: 999px;
  overflow: hidden;
  position: relative;
}}
.ldi-progress-fill {{
  height: 100%;
  background: linear-gradient(90deg, var(--ldi-primary), var(--ldi-accent));
  border-radius: 999px;
  transition: width 300ms ease;
}}
.ldi-progress-fill.indeterminate {{
  width: 35% !important;
  position: absolute;
  animation: ldi-indet 1.4s infinite ease-in-out;
}}
@keyframes ldi-indet {{
  0%   {{ left: -35%; }}
  100% {{ left: 100%; }}
}}

/* Chat */
.ldi-chat-bubble-user {{
  background: var(--ldi-primary) !important;
  color: white !important;
  border-radius: 14px 14px 4px 14px !important;
  padding: 11px 14px;
  max-width: 78%;
  margin-left: auto;
  box-shadow: 0 1px 0 rgba(0,0,0,0.12);
  line-height: 1.55;
}}
.ldi-chat-bubble-assistant {{
  background: {bubble_assistant_bg} !important;
  backdrop-filter: blur(var(--ldi-blur)) saturate(140%);
  border: 1px solid var(--ldi-glass-border) !important;
  border-radius: 14px 14px 14px 4px !important;
  padding: 11px 14px;
  max-width: 92%;
  color: var(--ldi-text) !important;
  line-height: 1.65;
}}
.ldi-avatar {{
  width: 30px; height: 30px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  flex-shrink: 0;
}}
.ldi-avatar-user {{ background: var(--ldi-primary); color: white; }}
.ldi-avatar-bot  {{
  background: var(--ldi-glass-bg);
  border: 1px solid var(--ldi-glass-border);
  color: var(--ldi-accent);
}}

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

.ldi-source-card {{
  display: block;
  padding: 9px 12px;
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

/* Nav item */
.ldi-nav-item {{
  width: 100%;
  justify-content: flex-start !important;
  border-radius: var(--ldi-radius-sm) !important;
  padding: 7px 11px !important;
  margin-bottom: 2px;
  transition: background var(--ldi-trans), color var(--ldi-trans);
}}
.ldi-nav-item:hover {{ background: var(--ldi-glass-bg) !important; }}
.ldi-nav-item.active {{
  background: var(--ldi-glass-bg) !important;
  color: var(--ldi-primary) !important;
  border: 1px solid var(--ldi-glass-border) !important;
}}

/* Brand */
.ldi-brand {{
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-weight: 600;
  font-size: 1.05rem;
  color: var(--ldi-text);
}}
.ldi-brand-mark {{
  width: 30px; height: 30px;
  border-radius: 7px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--ldi-primary);
  color: white;
  font-weight: 700;
  font-size: 14px;
}}

.ldi-stat {{ position: relative; padding: 16px 16px 14px 16px; }}

button:focus-visible, a:focus-visible, .q-btn:focus-visible {{
  outline: 2px solid var(--ldi-primary);
  outline-offset: 2px;
}}

.q-page-container > .q-page {{ padding-top: 12px; }}

/* Search highlight */
.ldi-mark {{
  background: var(--ldi-accent);
  color: var(--ldi-bg);
  padding: 1px 4px;
  border-radius: 4px;
  font-weight: 500;
}}
.ldi-snippet {{
  font-size: 14px;
  line-height: 1.55;
  color: var(--ldi-text);
  opacity: 0.92;
}}

/* Footnote / citation pill in chat */
.ldi-citation-ref {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 22px;
  height: 22px;
  padding: 0 6px;
  margin: 0 2px;
  border-radius: 11px;
  background: var(--ldi-glass-bg);
  border: 1px solid var(--ldi-glass-border);
  color: var(--ldi-accent);
  font-size: 11px;
  font-weight: 600;
  text-decoration: none;
  cursor: pointer;
  transition: background var(--ldi-trans);
  vertical-align: baseline;
}}
.ldi-citation-ref:hover {{
  background: var(--ldi-primary);
  color: white;
}}

/* Dashboard mini-chart bars */
.ldi-chart-bar {{
  flex: 1;
  min-width: 6px;
  background: linear-gradient(180deg, var(--ldi-primary), var(--ldi-accent));
  border-radius: 3px 3px 0 0;
  opacity: 0.85;
  transition: opacity var(--ldi-trans);
}}
.ldi-chart-bar:hover {{ opacity: 1; }}
"""
