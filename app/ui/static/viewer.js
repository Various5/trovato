/* Trovato continuous PDF viewer — client side.
 *
 * The server injects window.__ldiViewerInit BEFORE this script runs:
 *   { doc, total, page, q, dims: [[w,h], ...], rects: {"<page>": [[x,y,w,h]...]},
 *     urlTpl: "/api/documents/<doc>/page/{n}/image?t=...",
 *     matchesTpl: "/api/documents/<doc>/matches?t=...",
 *     buckets: [768, ...], defaultW: 1024 }
 *
 * All page flips, jumps, zooms and the fullscreen read mode run entirely in
 * the browser — no server round-trip, no page reload. Only the sidebar sync
 * emits a debounced custom event the NiceGUI page listens to ('ldi_page').
 */
(function () {
  'use strict';
  const init = window.__ldiViewerInit;
  if (!init) return;

  const clampPage = (n) => Math.min(Math.max(n, 1), init.total);

  const state = {
    cur: clampPage(init.page || 1),
    anchor: clampPage(init.page || 1),
    zoom: 1.0,
    rects: init.rects || {},
    matchPages: new Set(init.matchPages || []),
    rectsFetched: new Set(),
    q: init.q || '',
    emitTimer: null,
    resizeTimer: null,
    urlTimer: null,
    scrollPending: false,
    lastEmitted: null,
  };

  // Memoize the stable host/pages/input lookups so the per-frame scroll hot
  // path doesn't re-run getElementById. Lazy `_x || (_x = …)` only caches a
  // truthy result, so a not-yet-mounted element is retried next call (keeps the
  // startWhenReady boot polling intact).
  let _host = null;
  let _wrap = null;
  let _input = null;
  const $ = (id) => document.getElementById(id);
  const scrollEl = () => _host || (_host = $('ldi-pdfscroll'));
  const pagesEl = () => _wrap || (_wrap = $('ldi-pdfpages'));
  const inputEl = () => _input || (_input = $('ldi-pginput'));

  function pageUrl(n, w) {
    let u = init.urlTpl.replace('{n}', String(n));
    if (w) u += (u.includes('?') ? '&' : '?') + 'w=' + w;
    return u;
  }

  function buildDom() {
    const host = scrollEl();
    if (!host) return;
    const wrap = document.createElement('div');
    wrap.id = 'ldi-pdfpages';
    wrap.className = 'ldi-pdfpages';
    const srcset = (n) => init.buckets.map((b) => pageUrl(n, b) + ' ' + b + 'w').join(', ');
    for (let n = 1; n <= init.total; n++) {
      const dims = init.dims[n - 1] || [595, 842];
      const pw = dims[0] > 0 ? dims[0] : 595;
      const ph = dims[1] > 0 ? dims[1] : 842;
      const div = document.createElement('div');
      div.className = 'ldi-pgwrap';
      div.id = 'ldi-pg-' + n;
      div.dataset.n = String(n);
      div.style.aspectRatio = pw + ' / ' + ph;
      // Placeholder size for content-visibility:auto (see styles.py) so the
      // scrollbar doesn't jump before an off-screen page first renders; `auto`
      // lets the browser remember each page's real size afterwards. Geometry is
      // derived from the same dims as the aspect-ratio, so there's no drift.
      const estW = 960; // ~column max-width (980px minus padding)
      div.style.containIntrinsicSize =
        'auto ' + estW + 'px auto ' + Math.round((estW * ph) / pw) + 'px';
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.decoding = 'async';
      img.alt = 'page ' + n;
      img.src = pageUrl(n, init.defaultW);
      img.srcset = srcset(n);
      img.sizes = '60vw';
      const ov = document.createElement('div');
      ov.className = 'ldi-pgov';
      const badge = document.createElement('div');
      badge.className = 'ldi-pgbadge';
      badge.textContent = n + ' / ' + init.total;
      div.appendChild(img);
      div.appendChild(ov);
      div.appendChild(badge);
      wrap.appendChild(div);
    }
    host.appendChild(wrap);
    host.tabIndex = 0; // PageUp/PageDown/Space/arrows scroll natively once focused
  }

  function renderPageHighlights(page, rects) {
    const ov = document.querySelector('#ldi-pg-' + page + ' .ldi-pgov');
    if (!ov) return;
    ov.innerHTML = '';
    for (const r of rects) {
      const d = document.createElement('div');
      d.className = 'ldi-pghl';
      d.style.left = r[0] * 100 + '%';
      d.style.top = r[1] * 100 + '%';
      d.style.width = r[2] * 100 + '%';
      d.style.height = r[3] * 100 + '%';
      ov.appendChild(d);
    }
  }

  function applyHighlights() {
    document.querySelectorAll('.ldi-pgov').forEach((ov) => (ov.innerHTML = ''));
    for (const [page, rects] of Object.entries(state.rects)) {
      renderPageHighlights(page, rects);
    }
  }

  // The initial server payload only carries rects for the first ~80 matching
  // pages. When the user scrolls to a KNOWN matching page beyond that window,
  // fetch its rects on demand (once per page per term).
  function fetchRectsFor(n) {
    if (!state.q || !init.matchesTpl) return;
    if (!state.matchPages.has(n)) return;
    if (state.rects[String(n)] || state.rectsFetched.has(n)) return;
    state.rectsFetched.add(n);
    const sep = init.matchesTpl.includes('?') ? '&' : '?';
    fetch(init.matchesTpl + sep + 'q=' + encodeURIComponent(state.q) + '&pages=' + n)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data || !data.rects) return;
        // Only the lazy path runs here and it only ADDS pages, so render just
        // the new overlays instead of clearing + rebuilding the whole layer
        // (which flashed/stuttered while scrolling through results).
        const addedPages = [];
        for (const [page, rects] of Object.entries(data.rects)) {
          if (!state.rects[page]) {
            state.rects[page] = rects;
            addedPages.push(page);
          }
        }
        for (const page of addedPages) renderPageHighlights(page, state.rects[page]);
      })
      .catch(() => {});
  }

  function syncSizes() {
    // Tell the browser the CSS pixel width each page actually renders at, so
    // srcset picks the bucket matching layout width × devicePixelRatio.
    const wrap = pagesEl();
    if (!wrap) return;
    const sizes = Math.max(200, Math.round(wrap.getBoundingClientRect().width)) + 'px';
    wrap.querySelectorAll('img').forEach((img) => (img.sizes = sizes));
  }

  // Scroll ONLY the viewer pane. scrollIntoView would also scroll every
  // outer ancestor and yank the toolbar/find rows out of the window.
  function scrollToPage(n) {
    const host = scrollEl();
    const el = $('ldi-pg-' + n);
    if (!host || !el) return;
    host.scrollTop += el.getBoundingClientRect().top - host.getBoundingClientRect().top - 6;
  }

  function syncUrl() {
    const url =
      '/viewer?doc=' + init.doc + '&page=' + state.cur + (state.q ? '&q=' + encodeURIComponent(state.q) : '');
    try {
      history.replaceState(null, '', url);
    } catch (e) {}
  }

  function setCurrent(n, fromScroll) {
    n = clampPage(n);
    if (n === state.cur && fromScroll) return;
    state.cur = n;
    const input = inputEl();
    if (input && document.activeElement !== input) input.value = String(n);
    // Coalesce a scroll fling into one URL write (WebKit throttles >100
    // replaceState/30s); explicit jumps and ?q= refreshes write immediately.
    clearTimeout(state.urlTimer);
    if (fromScroll) {
      state.urlTimer = setTimeout(syncUrl, 400);
    } else {
      syncUrl();
    }
    fetchRectsFor(n);
    clearTimeout(state.emitTimer);
    state.emitTimer = setTimeout(() => {
      if (state.lastEmitted !== state.cur && window.emitEvent) {
        state.lastEmitted = state.cur;
        emitEvent('ldi_page', { n: state.cur });
      }
    }, 450);
  }

  function jump(n) {
    const v = Math.round(Number(n));
    const input = inputEl();
    if (!Number.isFinite(v) || String(n).trim() === '') {
      if (input) input.value = String(state.cur); // restore on garbage input
      return;
    }
    const p = clampPage(v);
    state.anchor = p;
    scrollToPage(p);
    setCurrent(p, false);
    if (input && document.activeElement !== input) input.value = String(p);
  }

  // The initial jump can land before the (cached, but still async) theme CSS
  // applies — the layout then grows and the scroll position ends up mid-page.
  // Re-anchor on layout growth for a short settling window, unless the user
  // already moved (incl. keyboard scrolling — the host is keyboard-focused).
  function keepAnchored() {
    const host = scrollEl();
    const wrap = pagesEl();
    if (!host || !wrap || !('ResizeObserver' in window)) return;
    let userMoved = false;
    const markMoved = () => (userMoved = true);
    for (const ev of ['wheel', 'touchstart', 'pointerdown', 'keydown']) {
      host.addEventListener(ev, markMoved, { passive: true, once: true });
    }
    const ro = new ResizeObserver(() => {
      if (!userMoved && state.anchor > 1) scrollToPage(state.anchor);
    });
    ro.observe(wrap);
    setTimeout(() => ro.disconnect(), 2500);
  }

  // Current page = last page whose top edge sits above a reference line ~35%
  // down the pane. Computed from scroll geometry (binary search over the
  // monotonically ordered wrappers) — IntersectionObserver's sparse threshold
  // crossings go stale between events and lag by up to a full viewport.
  function computeCurrent() {
    const host = scrollEl();
    const wrap = pagesEl();
    if (!host || !wrap || !wrap.children.length) return;
    const refY = host.getBoundingClientRect().top + Math.min(host.clientHeight * 0.35, 400);
    const kids = wrap.children;
    let lo = 0;
    let hi = kids.length - 1;
    let best = 0;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (kids[mid].getBoundingClientRect().top <= refY) {
        best = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    setCurrent(best + 1, true);
  }

  function bindScroll() {
    const host = scrollEl();
    if (!host) return;
    host.addEventListener(
      'scroll',
      () => {
        if (state.scrollPending) return;
        state.scrollPending = true;
        requestAnimationFrame(() => {
          state.scrollPending = false;
          computeCurrent();
        });
      },
      { passive: true }
    );
  }

  function zoomTo(z) {
    const host = scrollEl();
    const ratio = host && host.scrollHeight > 0 ? host.scrollTop / host.scrollHeight : 0;
    state.zoom = Math.min(3, Math.max(0.5, Math.round(z * 100) / 100));
    if (pagesEl()) pagesEl().style.setProperty('--ldi-pgzoom', String(state.zoom));
    if (host) host.scrollTop = ratio * host.scrollHeight;
    syncSizes();
    computeCurrent();
  }

  function toggleFullscreen() {
    const root = $('ldi-viewer-root');
    if (!root) return;
    if (document.fullscreenElement || root.classList.contains('ldi-fakefull')) {
      root.classList.remove('ldi-fakefull');
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    } else if (root.requestFullscreen) {
      root.requestFullscreen().catch(() => root.classList.add('ldi-fakefull'));
    } else {
      root.classList.add('ldi-fakefull');
    }
    setTimeout(syncSizes, 120);
  }

  function setHighlights(rectsByPage, q, matchPages) {
    state.rects = rectsByPage || {};
    state.q = q || '';
    state.matchPages = new Set(matchPages || []);
    state.rectsFetched = new Set();
    applyHighlights();
    setCurrent(state.cur, false); // refresh the URL so ?q= reflects the new term
  }

  function bindKeys() {
    window.addEventListener('keydown', (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable))
        return;
      if (e.key === '+' || e.key === '=') {
        zoomTo(state.zoom + 0.25);
        e.preventDefault();
      } else if (e.key === '-') {
        zoomTo(state.zoom - 0.25);
        e.preventDefault();
      } else if (e.key === '0') {
        zoomTo(1);
        e.preventDefault();
      } else if (e.key === 'f' || e.key === 'F') {
        toggleFullscreen();
        e.preventDefault();
      }
    });
    document.addEventListener('fullscreenchange', () => setTimeout(syncSizes, 120));
    window.addEventListener('resize', () => {
      clearTimeout(state.resizeTimer);
      state.resizeTimer = setTimeout(() => {
        syncSizes();
        computeCurrent();
      }, 200);
    });
  }

  function bindInput() {
    const input = inputEl();
    if (!input) return;
    input.value = String(state.cur);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        input.blur(); // blur first so setCurrent may correct the value
        jump(input.value);
      }
    });
    input.addEventListener('blur', () => {
      const v = Math.round(Number(input.value));
      if (input.value.trim() === '' || !Number.isFinite(v)) {
        input.value = String(state.cur); // restore, don't jump to page 1
        return;
      }
      if (v !== state.cur) jump(input.value);
    });
  }

  let started = false;
  function start() {
    if (started || !scrollEl()) return;
    started = true;
    buildDom();
    applyHighlights();
    bindInput();
    bindKeys();
    bindScroll();
    syncSizes();
    if (state.cur > 1) jump(state.cur);
    keepAnchored();
    const host = scrollEl();
    if (host) host.focus({ preventScroll: true });
  }

  // NiceGUI mounts the page DOM through Vue AFTER DOMContentLoaded, so the
  // scroll host usually doesn't exist yet when this (deferred) script runs —
  // watch the document until it appears, then boot exactly once.
  function startWhenReady() {
    if (scrollEl()) {
      start();
      return;
    }
    const mo = new MutationObserver(() => {
      if (scrollEl()) {
        mo.disconnect();
        start();
      }
    });
    mo.observe(document.documentElement, { childList: true, subtree: true });
  }

  window.ldiViewer = {
    jump,
    next: () => jump(state.cur + 1),
    prev: () => jump(state.cur - 1),
    zoomIn: () => zoomTo(state.zoom + 0.25),
    zoomOut: () => zoomTo(state.zoom - 0.25),
    fit: () => zoomTo(1),
    fullscreen: toggleFullscreen,
    setHighlights,
    current: () => state.cur,
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', startWhenReady);
  else startWhenReady();
})();
