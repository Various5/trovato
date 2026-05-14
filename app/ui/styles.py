"""Global stylesheet for the modern, restrained UI.

A single source of truth for typography, surfaces, motion, gradient
backgrounds and a small motion-design system. The look stays deliberately
neutral — soft greys with a single accent — but animations now feel
polished and intentional.

Motion principles:
    * Cubic-bezier(0.16, 1, 0.3, 1)  → 'expo-out' easing for entrances
    * Cubic-bezier(0.4, 0, 0.2, 1)   → material curve for state changes
    * Durations: 180ms (state), 220-320ms (entrance), 1.4-2.4s (looped)
    * Honour ``prefers-reduced-motion: reduce``
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
            "radial-gradient(at 0% 0%, rgba(255,255,255,0.05) 0px, transparent 55%),"
            "radial-gradient(at 100% 100%, rgba(255,255,255,0.025) 0px, transparent 60%),"
            f"linear-gradient(180deg, {theme.bg} 0%, {theme.surface} 100%)"
        )
    return (
        "radial-gradient(at 0% 0%, rgba(15,40,90,0.045) 0px, transparent 55%),"
        "radial-gradient(at 100% 100%, rgba(15,40,90,0.025) 0px, transparent 60%),"
        f"linear-gradient(180deg, {theme.bg} 0%, #ffffff 100%)"
    )


def build_global_css(theme_name: str) -> str:
    """Return the full <style> body for the active theme."""
    t = THEMES.get(theme_name) or THEMES["slate"]
    bg = _background_layers(t)
    if t.is_dark:
        glass_bg = "rgba(255,255,255,0.025)"
        glass_bg_strong = "rgba(255,255,255,0.05)"
        glass_border = "rgba(255,255,255,0.08)"
        glass_shadow = "0 2px 12px rgba(0,0,0,0.35), 0 0 0 1px rgba(255,255,255,0.03)"
        hover_glow = "0 0 0 1px rgba(255,255,255,0.14), 0 14px 32px -8px rgba(0,0,0,0.5)"
        bubble_assistant_bg = "rgba(255,255,255,0.04)"
        code_bg = "rgba(255,255,255,0.05)"
        pre_bg = "rgba(0,0,0,0.30)"
        focus_glow = "rgba(125, 164, 255, 0.20)"
    else:
        glass_bg = "rgba(255,255,255,0.85)"
        glass_bg_strong = "rgba(255,255,255,0.95)"
        glass_border = "rgba(15,30,55,0.08)"
        glass_shadow = "0 2px 10px rgba(15,30,55,0.06), 0 0 0 1px rgba(15,30,55,0.03)"
        hover_glow = "0 0 0 1px rgba(15,30,55,0.12), 0 14px 32px -8px rgba(15,30,55,0.15)"
        bubble_assistant_bg = "rgba(255,255,255,0.92)"
        code_bg = "rgba(15,30,55,0.05)"
        pre_bg = "rgba(15,30,55,0.04)"
        focus_glow = "rgba(58, 94, 224, 0.18)"

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
  --ldi-glass-bg-strong: {glass_bg_strong};
  --ldi-glass-border: {glass_border};
  --ldi-glass-shadow: {glass_shadow};
  --ldi-hover-glow: {hover_glow};
  --ldi-focus-glow: {focus_glow};
  --ldi-radius: 14px;
  --ldi-radius-sm: 9px;
  --ldi-radius-lg: 20px;
  --ldi-blur: 18px;
  --ldi-trans: 180ms cubic-bezier(0.4, 0, 0.2, 1);
  --ldi-trans-expo: 280ms cubic-bezier(0.16, 1, 0.3, 1);
}}

/* ------------------------------------------------------------------ */
/*  Motion primitives                                                  */
/* ------------------------------------------------------------------ */
@keyframes ldi-fade-in        {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
@keyframes ldi-fade-in-up     {{ from {{ opacity: 0; transform: translateY(8px); }}
                                  to   {{ opacity: 1; transform: translateY(0); }} }}
@keyframes ldi-scale-in       {{ from {{ opacity: 0; transform: scale(0.985); }}
                                  to   {{ opacity: 1; transform: scale(1); }} }}
@keyframes ldi-shimmer        {{ from {{ background-position: -200% 0; }}
                                  to   {{ background-position:  200% 0; }} }}
@keyframes ldi-pulse-dot      {{ 0%, 100% {{ opacity: 1; transform: scale(1); }}
                                  50%      {{ opacity: 0.55; transform: scale(0.9); }} }}
@keyframes ldi-pulse-ring     {{ 0%   {{ box-shadow: 0 0 0 0 var(--ldi-primary); opacity: 0.55; }}
                                  100% {{ box-shadow: 0 0 0 12px transparent; opacity: 0; }} }}
@keyframes ldi-spin-soft      {{ to {{ transform: rotate(360deg); }} }}
@keyframes ldi-brand-gleam    {{ 0%,100% {{ background-position: 0% 50%; }}
                                  50%     {{ background-position: 100% 50%; }} }}
@keyframes ldi-blink {{
  0%, 50%   {{ opacity: 1; }}
  51%, 100% {{ opacity: 0.15; }}
}}
@keyframes ldi-indet {{
  0%   {{ left: -35%; }}
  100% {{ left: 100%; }}
}}

/* ------------------------------------------------------------------ */
/*  Base                                                                */
/* ------------------------------------------------------------------ */
html {{ scroll-behavior: smooth; }}
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

/* Page entrance — the wrapper fades+slides once per navigation */
.q-page-container > .q-page {{
  animation: ldi-fade-in-up 320ms cubic-bezier(0.16, 1, 0.3, 1) both;
}}

/* ------------------------------------------------------------------ */
/*  Surfaces                                                            */
/* ------------------------------------------------------------------ */
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
  transition: transform 220ms cubic-bezier(0.16, 1, 0.3, 1),
              box-shadow 220ms ease,
              border-color 220ms ease;
}}
.q-card:hover {{
  transform: translateY(-2px);
  box-shadow: var(--ldi-hover-glow) !important;
  border-color: rgba(125, 164, 255, 0.18) !important;
}}

.q-drawer {{
  border-right: 1px solid var(--ldi-glass-border) !important;
  box-shadow: none !important;
}}

.q-header, .q-toolbar {{
  border-bottom: 1px solid var(--ldi-glass-border) !important;
  box-shadow: 0 1px 0 var(--ldi-glass-border) !important;
}}

/* Menus & dialogs scale+fade in */
.q-menu,
.q-dialog .q-dialog__inner > div {{
  animation: ldi-scale-in 220ms cubic-bezier(0.16, 1, 0.3, 1) both;
  border-radius: var(--ldi-radius) !important;
}}

/* ------------------------------------------------------------------ */
/*  Fields                                                              */
/* ------------------------------------------------------------------ */
.q-field__control, .q-field__native, .q-field__label, .q-field__marginal {{
  color: var(--ldi-text) !important;
}}
.q-field--outlined .q-field__control:before {{
  border: 1px solid var(--ldi-glass-border) !important;
  border-radius: var(--ldi-radius-sm) !important;
  transition: border-color var(--ldi-trans);
}}
.q-field--outlined .q-field__control:hover:before,
.q-field--outlined .q-field__control:focus-within:before {{
  border-color: var(--ldi-primary) !important;
}}
.q-field--outlined .q-field__control:focus-within:before {{
  box-shadow: 0 0 0 4px var(--ldi-focus-glow);
}}
.q-field input, .q-field textarea {{
  font-family: 'Inter', sans-serif !important;
}}

/* ------------------------------------------------------------------ */
/*  Buttons                                                             */
/* ------------------------------------------------------------------ */
.q-btn {{
  border-radius: var(--ldi-radius-sm) !important;
  font-weight: 500 !important;
  letter-spacing: 0 !important;
  text-transform: none !important;
  transition: transform 120ms ease,
              background var(--ldi-trans),
              color var(--ldi-trans),
              box-shadow var(--ldi-trans),
              filter var(--ldi-trans);
}}
.q-btn:active {{ transform: scale(0.97); }}
.q-btn--standard.bg-primary,
.q-btn[color=primary] {{
  background: var(--ldi-primary) !important;
  color: white !important;
  box-shadow: 0 1px 0 rgba(0,0,0,0.12), 0 8px 22px -10px var(--ldi-primary) !important;
}}
.q-btn--standard.bg-primary:hover,
.q-btn[color=primary]:hover {{
  filter: brightness(1.08);
  box-shadow: 0 1px 0 rgba(0,0,0,0.12), 0 12px 28px -10px var(--ldi-primary) !important;
}}

/* Round flat icon buttons get a soft glass hover */
.q-btn--round.q-btn--flat:hover,
.q-btn--flat.q-btn--dense:hover {{
  background: var(--ldi-glass-bg) !important;
}}

/* ------------------------------------------------------------------ */
/*  Typography                                                          */
/* ------------------------------------------------------------------ */
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

.ldi-hero-text {{
  background: linear-gradient(90deg, var(--ldi-text) 10%, var(--ldi-primary) 90%);
  -webkit-background-clip: text;
          background-clip: text;
  color: transparent;
  font-weight: 700;
  letter-spacing: -0.02em;
}}

/* ------------------------------------------------------------------ */
/*  Scrollbar & selection                                               */
/* ------------------------------------------------------------------ */
::-webkit-scrollbar           {{ width: 10px; height: 10px; }}
::-webkit-scrollbar-track     {{ background: transparent; }}
::-webkit-scrollbar-thumb     {{
  background: var(--ldi-glass-border);
  border-radius: 6px;
  border: 2px solid transparent;
  background-clip: padding-box;
  transition: background var(--ldi-trans);
}}
::-webkit-scrollbar-thumb:hover {{
  background: var(--ldi-primary);
  background-clip: padding-box;
}}

::selection {{ background: var(--ldi-primary); color: white; }}

/* ------------------------------------------------------------------ */
/*  Utility classes                                                     */
/* ------------------------------------------------------------------ */
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
  transition: background var(--ldi-trans), border-color var(--ldi-trans);
}}
.ldi-pill:hover {{ border-color: var(--ldi-primary); }}
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

/* Live status dot — breathing pulse */
.ldi-status-dot {{
  display: inline-block;
  width: 9px; height: 9px;
  border-radius: 50%;
  background: var(--ldi-accent);
  margin-right: 6px;
  vertical-align: middle;
  animation: ldi-pulse-dot 1.6s ease-in-out infinite;
  position: relative;
}}
.ldi-status-dot.success {{ background: #6ce5b0; }}
.ldi-status-dot.warning {{ background: #ffb86c; }}
.ldi-status-dot.error   {{ background: #ff8b8b; animation: none; }}
.ldi-status-dot.idle    {{ background: var(--ldi-glass-border); animation: none; }}
.ldi-status-dot.live::after {{
  content: '';
  position: absolute;
  inset: -4px;
  border-radius: 50%;
  animation: ldi-pulse-ring 1.8s ease-out infinite;
  pointer-events: none;
}}

/* Skeleton shimmer for loading states */
.ldi-skeleton {{
  background: linear-gradient(
    90deg,
    var(--ldi-glass-bg) 0%,
    var(--ldi-glass-border) 50%,
    var(--ldi-glass-bg) 100%
  );
  background-size: 200% 100%;
  animation: ldi-shimmer 1.4s ease infinite;
  border-radius: var(--ldi-radius-sm);
  height: 18px;
  border: none !important;
}}

/* ------------------------------------------------------------------ */
/*  Progress bar (with shimmer)                                         */
/* ------------------------------------------------------------------ */
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
  background: linear-gradient(
    90deg,
    var(--ldi-primary) 0%,
    var(--ldi-accent) 50%,
    var(--ldi-primary) 100%
  );
  background-size: 200% 100%;
  animation: ldi-shimmer 2.6s linear infinite;
  border-radius: 999px;
  transition: width 320ms cubic-bezier(0.16, 1, 0.3, 1);
}}
.ldi-progress-fill.indeterminate {{
  width: 35% !important;
  position: absolute;
  background: linear-gradient(
    90deg,
    transparent,
    var(--ldi-primary),
    var(--ldi-accent),
    transparent
  );
  animation: ldi-indet 1.4s infinite ease-in-out;
}}

/* ------------------------------------------------------------------ */
/*  Chat                                                                */
/* ------------------------------------------------------------------ */
.ldi-chat-bubble-user,
.ldi-chat-bubble-assistant {{
  animation: ldi-fade-in-up 260ms cubic-bezier(0.16, 1, 0.3, 1) both;
}}
.ldi-chat-bubble-user {{
  background: var(--ldi-primary) !important;
  color: white !important;
  border-radius: 14px 14px 4px 14px !important;
  padding: 11px 14px;
  max-width: 78%;
  margin-left: auto;
  box-shadow: 0 4px 14px -6px var(--ldi-primary);
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

.ldi-source-card {{
  display: block;
  padding: 9px 12px;
  border-radius: var(--ldi-radius-sm);
  background: var(--ldi-glass-bg);
  border: 1px solid var(--ldi-glass-border);
  margin-top: 6px;
  cursor: pointer;
  transition: transform 200ms cubic-bezier(0.16, 1, 0.3, 1),
              box-shadow 200ms ease,
              border-color 200ms ease;
}}
.ldi-source-card:hover {{
  transform: translateY(-2px);
  box-shadow: var(--ldi-hover-glow);
  border-color: rgba(125, 164, 255, 0.22);
}}

/* ------------------------------------------------------------------ */
/*  Navigation                                                          */
/* ------------------------------------------------------------------ */
.ldi-nav-item {{
  position: relative;
  width: 100%;
  justify-content: flex-start !important;
  border-radius: var(--ldi-radius-sm) !important;
  padding: 8px 12px !important;
  margin-bottom: 2px;
  transition: background var(--ldi-trans),
              color var(--ldi-trans),
              padding var(--ldi-trans);
  overflow: hidden;
}}
.ldi-nav-item:hover {{
  background: var(--ldi-glass-bg) !important;
  padding-left: 14px !important;
}}
.ldi-nav-item.active {{
  background: var(--ldi-glass-bg-strong) !important;
  color: var(--ldi-primary) !important;
  border: 1px solid var(--ldi-glass-border) !important;
}}
.ldi-nav-item.active::before {{
  content: '';
  position: absolute;
  left: 0;
  top: 22%;
  bottom: 22%;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: linear-gradient(180deg, var(--ldi-primary), var(--ldi-accent));
  box-shadow: 0 0 12px var(--ldi-primary);
}}

/* ------------------------------------------------------------------ */
/*  Brand                                                               */
/* ------------------------------------------------------------------ */
.ldi-brand {{
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-weight: 600;
  font-size: 1.05rem;
  color: var(--ldi-text);
}}
.ldi-brand-mark {{
  width: 32px; height: 32px;
  border-radius: 9px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, var(--ldi-primary) 0%, var(--ldi-accent) 50%, var(--ldi-primary) 100%);
  background-size: 200% 200%;
  animation: ldi-brand-gleam 6s ease-in-out infinite;
  color: white;
  font-weight: 700;
  font-size: 15px;
  box-shadow: 0 6px 14px -4px var(--ldi-primary),
              inset 0 1px 0 rgba(255,255,255,0.18);
  transition: transform var(--ldi-trans);
}}
.ldi-brand:hover .ldi-brand-mark {{ transform: rotate(-6deg) scale(1.05); }}

/* ------------------------------------------------------------------ */
/*  Stats / dashboard                                                   */
/* ------------------------------------------------------------------ */
.ldi-stat {{
  position: relative;
  padding: 16px 16px 14px 16px;
}}
.ldi-stat::before {{
  content: '';
  position: absolute;
  inset: 0;
  border-radius: var(--ldi-radius);
  background: linear-gradient(135deg,
              rgba(125, 164, 255, 0.06) 0%,
              transparent 60%);
  pointer-events: none;
  opacity: 0;
  transition: opacity var(--ldi-trans);
}}
.q-card:hover .ldi-stat::before {{ opacity: 1; }}

/* ------------------------------------------------------------------ */
/*  Focus visibility                                                    */
/* ------------------------------------------------------------------ */
button:focus-visible, a:focus-visible, .q-btn:focus-visible {{
  outline: 2px solid var(--ldi-primary);
  outline-offset: 2px;
  box-shadow: 0 0 0 4px var(--ldi-focus-glow);
}}

.q-page-container > .q-page {{ padding-top: 12px; }}

/* ------------------------------------------------------------------ */
/*  Search highlight                                                    */
/* ------------------------------------------------------------------ */
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

/* ------------------------------------------------------------------ */
/*  Citation pill in chat                                               */
/* ------------------------------------------------------------------ */
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
  transition: background var(--ldi-trans),
              color var(--ldi-trans),
              transform var(--ldi-trans);
  vertical-align: baseline;
}}
.ldi-citation-ref:hover {{
  background: var(--ldi-primary);
  color: white;
  transform: translateY(-1px);
}}

/* ------------------------------------------------------------------ */
/*  Dashboard mini-chart bars                                           */
/* ------------------------------------------------------------------ */
.ldi-chart-bar {{
  flex: 1;
  min-width: 6px;
  background: linear-gradient(180deg, var(--ldi-primary), var(--ldi-accent));
  border-radius: 3px 3px 0 0;
  opacity: 0.82;
  transition: opacity var(--ldi-trans), transform var(--ldi-trans);
  transform-origin: bottom;
}}
.ldi-chart-bar:hover {{
  opacity: 1;
  transform: scaleY(1.04);
}}

/* ------------------------------------------------------------------ */
/*  Tooltip                                                             */
/* ------------------------------------------------------------------ */
.q-tooltip {{
  background: rgba(15, 20, 28, 0.92) !important;
  color: #e3e7ee !important;
  border-radius: 6px !important;
  font-size: 12px !important;
  letter-spacing: 0 !important;
  padding: 5px 8px !important;
  backdrop-filter: blur(6px);
  border: 1px solid var(--ldi-glass-border);
  animation: ldi-fade-in 140ms ease both;
}}

/* ------------------------------------------------------------------ */
/*  Spinner                                                             */
/* ------------------------------------------------------------------ */
.q-spinner {{ animation-duration: 0.9s !important; }}

/* ------------------------------------------------------------------ */
/*  Reduced motion                                                      */
/* ------------------------------------------------------------------ */
@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{
    animation-duration: 0.001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.001ms !important;
    scroll-behavior: auto !important;
  }}
}}
"""
