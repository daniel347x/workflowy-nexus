/*
 * glimpse-ui.js — "The Rail": the presentation layer of the GLIMPSE extension.
 *
 * Everything the user sees is built here (the rail, the deck, the Reader
 * chrome, popovers, the command palette, toasts, the cursor-menu renderer) and
 * in glimpse-ui-drawers.js (the Anchors / Slices / Rod / Tabs / Jobs /
 * Settings / Help drawers). The plumbing — transport, request helpers, render
 * state, provenance, the six daemon requests — stays in glimpse-cache-client.js,
 * which MOUNTS this module with an `api` object (its request helpers and state
 * accessors) and binds its dock element variables to the Reader's elements, so
 * displaySliceInDock / adoptRenderScope / renderDockedList / the warning and
 * sync painters keep working unchanged. Design note: [ap:VX7YBY].
 *
 * Load order (manifest, MAIN world): hotkeys-shared → marked → katex →
 * marked-katex-init → glimpse-md-render → glimpse-ui → glimpse-ui-help (the
 * words: bylines, one-liners, the Help sections, the first-run tips) →
 * glimpse-ui-drawers → glimpse-cache-client. This file publishes globalThis.GlimpseUI.
 */
(function installGlimpseUi(root) {
  'use strict';

  // ───────────────────────── icons (16px line icons) ─────────────────────────
  const ICON_PATHS = {
    reader: 'M2 4h6a3 3 0 0 1 3 3v13a2 2 0 0 0-2-2H2z M22 4h-6a3 3 0 0 0-3 3v13a2 2 0 0 1 2-2h7z',
    anchor: 'M12 22V8 M5 12H2a10 10 0 0 0 20 0h-3 M12 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6',
    layers: 'M12 2 2 7l10 5 10-5-10-5z M2 12l10 5 10-5 M2 17l10 5 10-5',
    zap: 'M13 2 3 14h9l-1 8 10-12h-9l1-8z',
    tabs: 'M3 5h18v14H3z M3 9h18 M6.5 7h.01 M9.5 7h.01',
    activity: 'M22 12h-4l-3 9L9 3l-3 9H2',
    settings: 'M4 21v-7 M4 10V3 M12 21v-9 M12 8V3 M20 21v-5 M20 12V3 M1 14h6 M9 8h6 M17 16h6',
    help: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3 M12 17h.01',
    chevron: 'm9 18 6-6-6-6',
    collapse: 'm13 17 5-5-5-5 M6 17l5-5-5-5',
    expand: 'm11 17-5-5 5-5 M18 17l-5-5 5-5',
    lock: 'M5 11h14v10H5z M8 11V7a4 4 0 0 1 8 0v4',
    unlock: 'M5 11h14v10H5z M8 11V7a4 4 0 0 1 7.6-1.7',
    refresh: 'M21 12a9 9 0 1 1-2.6-6.4 M21 3v6h-6',
    sync: 'm17 2 4 4-4 4 M3 11v-1a4 4 0 0 1 4-4h14 M7 22l-4-4 4-4 M21 13v1a4 4 0 0 1-4 4H3',
    eye: 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6',
    copy: 'M8 8h12v12H8z M16 8V4H4v12h4',
    tag: 'M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L2 12V2h10l8.6 8.6a2 2 0 0 1 0 2.8z M7 7h.01',
    x: 'M18 6 6 18 M6 6l12 12',
    more: 'M12 12h.01 M19 12h.01 M5 12h.01',
    search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z M21 21l-4.3-4.3',
    plus: 'M12 5v14 M5 12h14',
    star: 'm12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z',
    play: 'm6 4 14 8-14 8z',
    pencil: 'M12 20h9 M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4z',
    trash: 'M3 6h18 M8 6V4h8v2 M19 6l-1 14H6L5 6 M10 11v6 M14 11v6',
    upload: 'M12 3v12 M8 7l4-4 4 4 M4 21h16',
    download: 'M12 21V9 M8 17l4 4 4-4 M4 3h16',
    command: 'm4 17 6-6-6-6 M12 19h8',
    columns: 'M3 3h18v18H3z M12 3v18',
    rows: 'M3 3h18v18H3z M3 12h18',
    left: 'M19 12H5 M12 19l-7-7 7-7',
    right: 'M5 12h14 M12 5l7 7-7 7',
    up: 'M12 19V5 M5 12l7-7 7 7',
    down: 'M12 5v14 M19 12l-7 7-7-7',
    check: 'm20 6-11 11-5-5',
    hash: 'M4 9h16 M4 15h16 M10 3 8 21 M16 3l-2 18',
    external: 'M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6 M15 3h6v6 M10 14 21 3',
    text: 'M4 7V4h16v3 M9 20h6 M12 4v16',
    filter: 'M22 3H2l8 9.5V19l4 2v-8.5z',
    clock: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z M12 6v6l4 2',
    pin: 'M12 17v5 M5 17h14l-2-7H7z M9 3h6l-1 7h-4z',
  };
  function icon(name, size) {
    const d = ICON_PATHS[name] || ICON_PATHS.more;
    const sw = (name === 'more') ? 3 : 1.7;
    const s = size || 16;
    return '<svg xmlns="http://www.w3.org/2000/svg" width="' + s + '" height="' + s + '" viewBox="0 0 24 24" fill="none" '
      + 'stroke="currentColor" stroke-width="' + sw + '" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="' + d + '"/></svg>';
  }

  // ───────────────────────── tiny DOM helpers ─────────────────────────
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function iconBtn(name, title, onClick, cls) {
    const b = el('button', 'nx-iconbtn' + (cls ? ' ' + cls : ''));
    b.type = 'button';
    b.innerHTML = icon(name);
    if (title) b.title = title;
    if (onClick) b.addEventListener('click', (ev) => { ev.stopPropagation(); onClick(ev); });
    return b;
  }
  function btn(label, title, onClick, cls, iconName) {
    const b = el('button', 'nx-btn' + (cls ? ' ' + cls : ''));
    b.type = 'button';
    if (iconName) b.innerHTML = icon(iconName, 14) + '<span></span>';
    const span = iconName ? b.querySelector('span') : b;
    span.textContent = label;
    if (title) b.title = title;
    if (onClick) b.addEventListener('click', (ev) => { ev.stopPropagation(); onClick(ev); });
    return b;
  }
  function seg(options, value, onPick) {
    const wrap = el('span', 'nx-seg');
    options.forEach((o) => {
      const b = el('button', (o.value === value) ? 'is-on' : '', o.label);
      b.type = 'button';
      if (o.title) b.title = o.title;
      if (o.disabled) b.disabled = true;
      b.addEventListener('click', (ev) => { ev.stopPropagation(); onPick(o.value); });
      wrap.appendChild(b);
    });
    return wrap;
  }
  function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }
  function copyText(text, done) {
    const cb = (ok) => { try { if (done) done(!!ok); } catch (e) { /* ignore */ } };
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => cb(true), () => S.api.fallbackCopy(text, cb));
      } else {
        S.api.fallbackCopy(text, cb);
      }
    } catch (e) { S.api.fallbackCopy(text, cb); }
  }

  // ───────────────────────── module state ─────────────────────────
  const S = {
    api: null,
    els: {},
    mounted: false,
    drawerId: null,
    drawers: new Map(),       // id -> { def, container, built }
    theme: 'light',
    connected: false,
    jobs: 0,
    pop: null,                // the one open popover { el, close }
    lastState: null,
    viewPopRefresh: null,
    activity: [],
    // "The Rail, side by side" (ui-next/rail-side-by-side; R1 [ap:FQ2UWA], S [ap:HFOLEJ]).
    readerHidden: false,      // 'a panel alone': a drawer is open and the Reader pane is hidden
    pinned: new Set(readStoredList('glimpse_ui_pinned')),   // picker drawers kept open beside the Reader
    widened: false,           // a pinned picker temporarily widened into the full sheet
    pushFallback: false,      // the push self-check failed in this tab: the deck overlays instead
    pushActive: false,        // Workflowy is pushed aside right now
    pushPx: 0, pushSide: null,
    fitNeed: 0, fitNeedH: 0, sheetW: 0,
    tipsShown: false,         // the first-run tips were offered once this session (showTips)
  };
  function readStoredList(key) {
    try { const v = JSON.parse(localStorage.getItem(key) || '[]'); return Array.isArray(v) ? v : []; } catch (e) { return []; }
  }
  const RAIL_PX = 36, DIVIDER_PX = 6;
  // Below this window width a pinned panel overlays Workflowy instead of pushing it (S's sizing
  // rule: at 1280 px a pinned summary beside a 640 px Reader would leave Workflowy about 155 px).
  const PIN_PUSH_MIN_VW = 1500;
  // Drawer roles (the brief's contract). A PICKER (Anchor Docks, Slice groups, the Lightning Rod)
  // picks something to render INTO the Reader: it opens with the Reader beside it, as a TRANSIENT
  // sheet over Workflowy (never over the Reader) that closes after a pick, unless pinned. A drawer
  // that OPENS ALONE (Jobs, Tabs, Settings, Help) opens without the Reader when the deck is closed.
  function drawerDef(id) { const d = id ? S.drawers.get(id) : null; return d ? d.def : null; }
  function isPicker(id) { const def = drawerDef(id); return !!(def && def.picker); }
  function opensAlone(id) { const def = drawerDef(id); return !!(def && def.opensAlone); }
  function isTransient(id) { return !!id && isPicker(id) && (!S.pinned.has(id) || S.widened); }
  function drawerWidthFor(id) {
    let w = null;
    try { w = Number(localStorage.getItem('glimpse_ui_drawer_w_' + id)); } catch (e) { w = null; }
    if (Number.isFinite(w) && w >= 260) return w;
    return isPicker(id) ? 440 : 340;   // 440 px: the head of every one of Dan's 82 real Dock names fits (R1 measured 430 px max)
  }
  function pushEnabled() { try { return S.api.settings().glimpse_ui_push !== false; } catch (e) { return true; } }
  function flipEnabled() { try { return !!S.api.settings().glimpse_ui_flip; } catch (e) { return false; } }
  // The words module (glimpse-ui-help.js) is read at paint time, each string with its own fallback.
  function wordOf(key, fallback) {
    const h = root.GlimpseUiHelp;
    const v = (h && h.strings) ? h.strings[key] : null;
    return (typeof v === 'string') ? v : fallback;
  }
  function dotTitle(on) {
    return on ? wordOf('dotConnected', 'Connected to the NEXUS broker') : wordOf('dotDisconnected', 'Not connected (the service worker may be waking)');
  }

  // ───────────────────────── theme ─────────────────────────
  function luminanceOf(color) {
    const m = String(color || '').match(/rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s/]+([\d.]+))?/i);
    if (!m) return null;
    if (m[4] !== undefined && Number(m[4]) === 0) return null;
    const [r, g, b] = [Number(m[1]), Number(m[2]), Number(m[3])];
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
  }
  function detectTheme() {
    let pref = 'auto';
    try { pref = String((S.api && S.api.settings().glimpse_ui_theme) || 'auto'); } catch (e) { /* ignore */ }
    if (pref === 'light' || pref === 'dark') return pref;
    let lum = null;
    try {
      lum = luminanceOf(getComputedStyle(document.body).backgroundColor);
      if (lum == null) lum = luminanceOf(getComputedStyle(document.documentElement).backgroundColor);
    } catch (e) { lum = null; }
    if (lum != null) return lum < 0.5 ? 'dark' : 'light';
    try { if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) return 'dark'; } catch (e) { /* ignore */ }
    return 'light';
  }
  function applyTheme() {
    S.theme = detectTheme();
    if (S.els.deck) S.els.deck.setAttribute('data-theme', S.theme);
    document.querySelectorAll('.nx-float, .nx-pop').forEach((n) => n.setAttribute('data-theme', S.theme));
  }
  function watchTheme() {
    try {
      const mo = new MutationObserver(() => applyTheme());
      mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style', 'data-theme'] });
      if (document.body) mo.observe(document.body, { attributes: true, attributeFilter: ['class', 'style', 'data-theme'] });
    } catch (e) { /* ignore */ }
    try {
      const mq = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');
      if (mq && mq.addEventListener) mq.addEventListener('change', () => applyTheme());
    } catch (e) { /* ignore */ }
    setTimeout(applyTheme, 1500);  // Workflowy paints its theme a beat after load
  }

  // ───────────────────────── popovers ─────────────────────────
  function closePop() {
    if (!S.pop) return;
    const p = S.pop; S.pop = null;
    try { document.removeEventListener('mousedown', p.onDown, true); } catch (e) { /* ignore */ }
    try { document.removeEventListener('keydown', p.onKey, true); } catch (e) { /* ignore */ }
    try { p.el.remove(); } catch (e) { /* ignore */ }
    S.viewPopRefresh = null;
    try { if (typeof p.onClose === 'function') p.onClose(); } catch (e) { /* ignore */ }
  }
  function openPop(anchorEl, build, opts) {
    const o = opts || {};
    if (S.pop && S.pop.anchor === anchorEl) { closePop(); return null; }
    closePop();
    const pop = el('div', 'nx-pop');
    pop.setAttribute('data-theme', S.theme);
    if (o.width) pop.style.width = o.width + 'px';
    pop.addEventListener('mousedown', (ev) => ev.stopPropagation());
    pop.addEventListener('click', (ev) => ev.stopPropagation());
    build(pop);
    (S.els.deck || document.body).appendChild(pop);
    placePop(pop, anchorEl, o);
    const onDown = (ev) => {
      if (pop.contains(ev.target) || (anchorEl && anchorEl.contains(ev.target))) return;
      closePop();
    };
    const onKey = (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); closePop(); } };
    document.addEventListener('mousedown', onDown, true);
    document.addEventListener('keydown', onKey, true);
    S.pop = { el: pop, anchor: anchorEl, onDown, onKey, close: closePop, onClose: o.onClose };
    if (o.focus) { try { const f = pop.querySelector(o.focus); if (f) f.focus(); } catch (e) { /* ignore */ } }
    return pop;
  }
  function placePop(pop, anchorEl, o) {
    const vw = window.innerWidth || 1200, vh = window.innerHeight || 800;
    const w = pop.offsetWidth, h = pop.offsetHeight;
    let left = 100, top = 100;
    if (anchorEl && anchorEl.getBoundingClientRect) {
      const r = anchorEl.getBoundingClientRect();
      if (o && o.align === 'left') left = r.left; else left = r.right - w;
      top = r.bottom + 6;
      if (top + h > vh - 8) top = Math.max(8, r.top - h - 6);
    }
    pop.style.left = clamp(left, 8, Math.max(8, vw - w - 8)) + 'px';
    pop.style.top = clamp(top, 8, Math.max(8, vh - h - 8)) + 'px';
  }
  // A menu popover: items = [{label, detail, icon, kbd, on, disabled, sep}]
  function openMenu(anchorEl, items, opts) {
    return openPop(anchorEl, (pop) => {
      if (opts && opts.title) pop.appendChild(el('div', 'nx-pop-title', opts.title));
      items.forEach((it) => {
        if (it.sep) { pop.appendChild(el('div', 'nx-menu-sep')); return; }
        const b = el('button', 'nx-menu-item' + (it.on ? ' is-on' : ''));
        b.type = 'button';
        if (it.icon) b.innerHTML = icon(it.icon, 15);
        const lab = el('span', '', it.label);
        if (it.detail) lab.appendChild(el('span', 'nx-menu-detail', it.detail));
        b.appendChild(lab);
        if (it.kbd) b.appendChild(el('kbd', 'nx-kbd', it.kbd));
        if (it.checked != null) { const c = el('span', 'nx-mono', it.checked ? '✓' : ''); c.style.marginLeft = 'auto'; b.appendChild(c); }
        if (it.disabled) b.disabled = true;
        b.addEventListener('click', (ev) => { ev.stopPropagation(); closePop(); try { it.run(ev); } catch (e) { console.warn('[GlimpseUI] menu item failed:', e); } });
        pop.appendChild(b);
      });
    }, opts);
  }

  // ───────────────────────── toasts ─────────────────────────
  function toast(message, ms, kind) {
    const host = S.els.toasts;
    if (!host) return;
    const t = el('div', 'nx-toast' + (kind === 'warn' || /^[⚠⛔]/.test(String(message || '')) ? ' is-warn' : ''), String(message || ''));
    const kill = () => { try { t.remove(); } catch (e) { /* ignore */ } };
    t.addEventListener('click', kill);
    host.appendChild(t);
    while (host.children.length > 3) host.removeChild(host.firstChild);
    setTimeout(kill, (Number.isFinite(ms) && ms > 0) ? ms : 3000);
  }

  // ───────────────────────── activity log ─────────────────────────
  function logActivity(text) {
    const t = String(text || '').trim();
    if (!t) return;
    const last = S.activity[0];
    if (last && last.text === t) return;
    S.activity.unshift({ text: t, at: new Date() });
    if (S.activity.length > 200) S.activity.length = 200;
    renderActivity();
  }
  function renderActivity() {
    const host = S.els.activity;
    if (!host) return;
    host.innerHTML = '';
    if (!S.activity.length) { host.appendChild(el('div', 'nx-empty', 'Nothing yet. Path lookups, cache refreshes and job acknowledgements land here.')); return; }
    S.activity.forEach((a) => {
      const row = el('div', 'nx-activity-item');
      const time = el('span', 'nx-activity-time', a.at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
      row.appendChild(time);
      row.appendChild(document.createTextNode(a.text));
      row.title = 'Click to copy';
      row.addEventListener('click', () => copyText(a.text, (ok) => toast(ok ? 'Copied.' : 'Copy failed.', 1500)));
      host.appendChild(row);
    });
  }

  // ───────────────────────── layout ─────────────────────────
  function sidePref() {
    let s = 'right';
    try { s = String(S.api.settings().md_preview_dock_position || 'right').toLowerCase(); } catch (e) { /* ignore */ }
    return ['right', 'left', 'top', 'bottom', 'auto'].includes(s) ? s : 'right';
  }
  // 'auto' is orientation-aware: a window taller than wide docks the Reader at the bottom, so a
  // picker sheet gets the whole width (77 of Dan's 82 Dock names on one line at 1080 px); any
  // other window docks it at the right edge. The four fixed sides are taken as given.
  function sideNow() {
    const s = sidePref();
    if (s !== 'auto') return s;
    const vw = window.innerWidth || 1200, vh = window.innerHeight || 800;
    return (vh > vw) ? 'bottom' : 'right';
  }
  // The layout model ("The Rail, side by side"; R1's prototype [ap:FQ2UWA] made production).
  // PERSISTENT panes take real space: the Reader, and a pinned slide-out beside it with a divider;
  // on a left or right dock their width pushes Workflowy aside (applyPush) instead of covering it.
  // A TRANSIENT picker sheet floats beside the Reader, over Workflowy and never over the Reader, as
  // wide as its names need. The rail gives three display states: a panel alone (the Reader pane
  // hidden), the Reader alone, or both with the divider. Direction: a panel sits on the Reader's
  // inner side (left of a right dock, right of a left dock; glimpse_ui_flip swaps it); on a top or
  // bottom dock a sheet rises above or drops below the band.
  function applyLayout() {
    const d = S.els.deck, body = S.els.body;
    if (!d || !body) return;
    const side = sideNow();
    d.setAttribute('data-side', side);
    d.setAttribute('data-flip', flipEnabled() ? '1' : '0');
    const vw = window.innerWidth || 1200, vh = window.innerHeight || 800;
    const open = isOpen();
    const id = S.drawerId;
    const drawerOn = open && !!id;
    const transient = drawerOn && isTransient(id) && !S.readerHidden;
    const persistentDrawer = drawerOn && !transient;
    const readerOn = open && !(drawerOn && S.readerHidden);
    const vertical = (side === 'right' || side === 'left');
    const R = vertical ? Math.round(S.api.dockWidth()) : 0;
    const Sw = drawerOn ? drawerWidthFor(id) : 0;
    // A pushed deck starts at the top: Workflowy's header sits beside it, not under it. An
    // overlay keeps the configured top offset (so Workflowy's search box stays visible).
    S.pushActive = pushEnabled() && !S.pushFallback && vertical;
    const top = S.pushActive ? 0 : S.api.dockTopOffset();
    d.style.top = ''; d.style.bottom = ''; d.style.height = ''; body.style.width = ''; body.style.height = '';
    S.els.reader.style.display = readerOn ? '' : 'none';
    S.els.divider.style.display = (readerOn && persistentDrawer) ? '' : 'none';
    const dr = S.els.drawer;
    dr.setAttribute('data-transient', transient ? '1' : '0');
    dr.setAttribute('data-picker', (drawerOn && isPicker(id)) ? '1' : '0');
    dr.setAttribute('data-mode', !drawerOn ? '' : (transient ? 'full' : (isPicker(id) ? 'summary' : 'normal')));
    let bodyW = 0;
    if (vertical) {
      d.style.top = Math.round(top) + 'px'; d.style.bottom = '0';
      bodyW = (readerOn ? R : 0) + (persistentDrawer ? Sw + DIVIDER_PX : 0);
      body.style.width = bodyW + 'px';
    } else if (side === 'top') {
      d.style.top = Math.round(top) + 'px'; d.style.height = Math.round(S.api.dockHeight()) + 'px';
    } else {
      d.style.bottom = '0'; d.style.height = Math.round(S.api.dockHeight()) + 'px';
    }
    // Drawer geometry. Persistent: a flex column of Sw px beside the Reader. Transient: a
    // content-fitted sheet outside the body box (its width comes from measureDrawer).
    dr.style.width = ''; dr.style.height = ''; dr.style.flex = '';
    if (persistentDrawer) { dr.style.width = Sw + 'px'; dr.style.flex = '0 0 ' + Sw + 'px'; }
    if (transient) {
      const avail = Math.max(320, vw - RAIL_PX - (vertical ? bodyW : 0) - 16);
      const need = Math.max(360, S.fitNeed || 0);
      const w = Math.min(need, avail);
      dr.style.width = w + 'px'; dr.style.flex = '0 0 auto';
      if (!vertical) {
        const band = Math.round(S.api.dockHeight());
        const room = (side === 'bottom') ? (vh - band - 8) : (vh - top - band - 8);
        dr.style.height = Math.max(200, Math.min(room, (S.fitNeedH || room))) + 'px';
      }
      S.sheetW = w;
    }
    // Outline orientation: a wide Reader puts the outline beside the document.
    let pref = 'auto';
    try { pref = String(S.api.settings().glimpse_ui_outline || 'auto'); } catch (e) { /* ignore */ }
    const readerW = vertical ? R : (vw - (persistentDrawer ? Sw + DIVIDER_PX : 0) - RAIL_PX);
    const orient = (pref === 'side' || pref === 'top') ? pref : (readerW >= 720 ? 'side' : 'top');
    S.els.split.setAttribute('data-outline', orient);
    try { S.api.applyListFraction(); } catch (e) { /* ignore */ }
    // Push by the persistent width: the rail, the Reader, and a pinned panel on a wide enough
    // window (below PIN_PUSH_MIN_VW the pinned panel overlays Workflowy instead).
    const pushW = RAIL_PX + (open ? ((readerOn ? R : 0) + ((persistentDrawer && vw >= PIN_PUSH_MIN_VW) ? Sw + DIVIDER_PX : 0)) : 0);
    applyPush(side, pushW);
    if (transient) applyRowSplit();
    syncReaderBtn();
  }
  // @beacon[
  //   id=glimpse-ui@applyPush,
  //   slice_labels=nexus--md-preview,
  //   kind=ast,
  //   comment=Pushes Workflowy aside by px on a left or right dock: one screen-only <style> on <html> narrows <body> (border-box, so Workflowy's own sidebar padding comes out of it; a left dock adds margin-left), so the outline lays out in the free area, and gives Workflowy's fixed chrome a margin on the deck's edge (the header, the dialog backdrop, and any .fixed element whose Tailwind classes anchor it to that edge or inset it), so the header, sidebars, banners and dialogs sit beside the deck. It never touches <html>'s overflow or gives <body> a transform: the document stays Workflowy's scroller (wheel, Page Up/Down, Home/End) and fixed overlays (the delete confirmation, the loading logo) stay anchored to the viewport. The deck and the float layer live under <html>. Off for top/bottom docks, when glimpse_ui_push is false, or after the self-check (pushSelfCheck) fell back to the overlay.,
  // ]
  function applyPush(side, px) {
    let st = document.getElementById('glimpse-nx-push');
    const want = pushEnabled() && !S.pushFallback && (side === 'right' || side === 'left') && px > 0;
    if (!want) { if (st) st.textContent = ''; S.pushPx = 0; S.pushSide = null; return; }
    if (!st) { st = el('style'); st.id = 'glimpse-nx-push'; document.documentElement.appendChild(st); }
    // Workflowy scrolls the document itself (window.scrollY, document.scrollingElement), so
    // nothing here may change the scroller or make <body> a containing block: html{overflow:hidden}
    // killed the wheel and Page keys, and a transform on <body> anchored Workflowy's fixed dialogs
    // and loading logo to the top of the document. Fixed chrome is moved off the deck's edge by
    // a margin instead (it composes with Workflowy's own left/right offsets).
    const edge = (side === 'left') ? 'left' : 'right';
    const chrome = 'body :is(.wf-header,.dialog-backdrop,.fixed[class*="' + edge + '-"],.fixed[class*="inset-"])';
    const css = '@media screen{'
      + 'body{box-sizing:border-box!important;width:calc(100% - ' + px + 'px)!important;'
      + (side === 'left' ? 'margin-left:' + px + 'px!important;' : '') + '}'
      + chrome + '{margin-' + edge + ':' + px + 'px!important}'
      + '}';
    if (st.textContent !== css) st.textContent = css;
    S.pushPx = px; S.pushSide = side;
    schedulePushCheck();
  }
  let pushCheckTimer = null;
  function schedulePushCheck() {
    if (!S.pushPx) return;
    if (pushCheckTimer) clearTimeout(pushCheckTimer);
    pushCheckTimer = setTimeout(() => { pushCheckTimer = null; pushSelfCheck(); }, 450);
  }
  // The built-in self-check: after a push, Workflowy's header, its outline's page container and
  // any open menu must lie inside the free area, and no fixed element of Workflowy's may reach
  // under the deck's inner edge at the header line (a hit-test, so a renamed header class is
  // caught too).
  // If one does not (a Workflowy build that does not reflow under a narrowed <body>, or whose
  // fixed chrome no longer takes the edge margin), the push is dropped for this tab, the deck
  // overlays the page as before, and one Activity line says so. Runs after every push, on
  // resize, and whenever Workflowy writes <body>'s inline style (its own sidebar push).
  function pushSelfCheck() {
    if (!S.pushPx || !S.pushSide || S.pushFallback) return;
    const vw = window.innerWidth || 1200, vh = window.innerHeight || 800;
    const free = (S.pushSide === 'right') ? { l: 0, r: vw - S.pushPx } : { l: S.pushPx, r: vw };
    const probes = [];
    const header = document.querySelector('.wf-header');
    if (header) probes.push(['header', header]);
    const root = document.querySelector('div.project.root[projectid]') || document.querySelector('div[projectid]');
    let sc = root ? root.parentElement : null;
    while (sc && sc !== document.body) {
      let o = ''; try { o = getComputedStyle(sc).overflowY; } catch (e) { o = ''; }
      if (o === 'auto' || o === 'scroll') break;
      sc = sc.parentElement;
    }
    if (sc && sc !== document.body) probes.push(['outline', sc]);
    document.querySelectorAll('.wf-menu').forEach((m) => probes.push(['menu', m]));
    const offenders = [];
    probes.forEach(([what, node]) => {
      let r = null; try { r = node.getBoundingClientRect(); } catch (e) { r = null; }
      if (!r) return;
      const onScreen = r.width > 20 && r.height > 20 && r.right > 0 && r.left < vw && r.bottom > 0 && r.top < vh;
      if (!onScreen) return;
      if (r.right > free.r + 2 || r.left < free.l - 2) offenders.push(what);
    });
    try {
      // The fixed element's own box counts, not a child overflowing it (at 1280 px Workflowy's
      // header icons overflow a header that did take the margin, as they always have).
      const lay = document.getElementById('glimpse-nx-layer');
      const x = (S.pushSide === 'right') ? free.r + 4 : free.l - 4;
      document.elementsFromPoint(x, 24).some((n) => {
        if (n === document.documentElement || n === document.body || (S.els.deck && S.els.deck.contains(n)) || (lay && lay.contains(n))) return false;
        let f = n;
        while (f && f !== document.body && getComputedStyle(f).position !== 'fixed') f = f.parentElement;
        if (!f || f === document.body) return false;
        const r = f.getBoundingClientRect();
        if (r.right <= free.r + 2 && r.left >= free.l - 2) return false;
        if (!offenders.includes('header')) offenders.push('header');
        return true;
      });
    } catch (e) { /* ignore */ }
    if (!offenders.length) return;
    S.pushFallback = true;
    logActivity('Push disabled in this tab: Workflowy’s ' + offenders.join(' and ') + ' did not fit beside the panel, so the deck covers the page instead (Settings → Push Workflowy aside).');
    try { toast('Push disabled in this tab: the deck covers the page instead (see Activity).', 5000, 'warn'); } catch (e) { /* ignore */ }
    applyLayout();
  }
  // Fit-or-split (R1): a picker row shows its whole name on one line when it fits the sheet;
  // otherwise the name splits at its first separator (' — ', ': ', ' (', ' - ') into a head line
  // and a smaller grey second line. There is never an ellipsis in the full sheet.
  let measureCtx = null;
  function oneLineWidth(row) {
    const nm = row.querySelector('.nx-name');
    if (!nm) return 0;
    if (!measureCtx) measureCtx = document.createElement('canvas').getContext('2d');
    const cs = getComputedStyle(nm);
    measureCtx.font = cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
    const txt = nm.getAttribute('data-full') || nm.textContent;
    const pad = parseFloat(getComputedStyle(row).paddingLeft) || 0;
    // A slice-group row's secondary line (tags · sort · root) also asks for room, up to 620 px.
    let metaW = 0;
    const meta = row.querySelector('.nx-slice-meta');
    if (meta) { meta.classList.add('is-measure'); metaW = Math.min(620, meta.scrollWidth); meta.classList.remove('is-measure'); }
    return Math.max(Math.ceil(measureCtx.measureText(txt).width), metaW) + pad + 18 + 4 + 14;
  }
  function measureDrawer() {
    const rows = Array.from(S.els.drawerBody.querySelectorAll('.nx-tree-row'));
    let need = 0;
    rows.forEach((r) => { r._oneLine = oneLineWidth(r); if (r._oneLine > need) need = r._oneLine; });
    S.fitNeed = need + 8;
    S.fitNeedH = Math.min(2000, rows.length * 27 + 140);
  }
  function applyRowSplit() {
    const w = (S.sheetW || 0) || (S.els.drawer.clientWidth || 440);
    S.els.drawerBody.querySelectorAll('.nx-tree-row').forEach((r) => {
      const ol = r._oneLine || oneLineWidth(r);
      r.classList.toggle('is-split', ol > w - 10 && !!r.querySelector('.nx-name-tail'));
    });
  }
  // Called by a picker drawer after it paints its rows: size the sheet to the names, then split
  // the rows that still do not fit. Pinned (summary) mode shows every head whole on one line, with
  // ' …' where a second line is hidden, and the second line only where two heads collide.
  function fitDrawer() {
    if (!S.drawerId || !isPicker(S.drawerId)) return;
    measureDrawer();
    applyLayout();
    if (!isTransient(S.drawerId)) {
      const w = S.els.drawer.clientWidth || drawerWidthFor(S.drawerId);
      const rows = Array.from(S.els.drawerBody.querySelectorAll('.nx-tree-row'));
      const heads = new Map();
      rows.forEach((r) => { const h = r.querySelector('.nx-name-head'); const k = h ? h.textContent.trim() : ''; heads.set(k, (heads.get(k) || 0) + 1); });
      rows.forEach((r) => {
        const h = r.querySelector('.nx-name-head'); const k = h ? h.textContent.trim() : '';
        const split = (r._oneLine || 0) > w - 10 && !!r.querySelector('.nx-name-tail');
        r.classList.toggle('is-split', split);
        r.classList.toggle('is-dup', split && heads.get(k) > 1);
      });
    }
  }
  function splitName(name) {
    const s = String(name || '');
    const seps = [' — ', ' – ', ': ', ' (', ' - '];
    let best = -1, sep = null;
    seps.forEach((x) => { const i = s.indexOf(x); if (i >= 8 && (best < 0 || i < best)) { best = i; sep = x; } });
    if (best < 0) return { head: s, sep: '', tail: '' };
    return { head: s.slice(0, best), sep: (sep === ' (' ? ' ' : sep), tail: s.slice(best + (sep === ' (' ? 1 : sep.length)) };
  }
  function nameEl(name) {
    const p = splitName(name);
    const n = el('span', 'nx-name');
    n.setAttribute('data-full', String(name || ''));
    n.appendChild(el('span', 'nx-name-head', p.head));
    if (p.tail) { n.appendChild(el('span', 'nx-name-sep', p.sep)); n.appendChild(el('span', 'nx-name-tail', p.tail)); }
    return n;
  }
  // The divider between a persistent slide-out and the Reader trades width between them: the
  // slide-out's width is remembered per drawer (localStorage), the Reader's in the dock setting.
  function initDivider() {
    const dv = S.els.divider;
    let dragging = false, startX = 0, startS = 0, startR = 0;
    const onMove = (e) => {
      if (!dragging || !S.drawerId) return;
      const side = sideNow();
      const dx = e.clientX - startX;
      const drawerLeftOfReader = (side === 'right') !== flipEnabled();   // moving the grip towards the Reader widens the drawer
      const sign = drawerLeftOfReader ? 1 : -1;
      const ns = clamp(Math.round(startS + sign * dx), 260, 900);
      const delta = ns - startS;
      try { localStorage.setItem('glimpse_ui_drawer_w_' + S.drawerId, String(ns)); } catch (err) { /* ignore */ }
      if (side === 'right' || side === 'left') S.api.persistSetting('md_preview_dock_width_px', clamp(startR - delta, 360, 2400));
      applyLayout();
    };
    const onUp = () => {
      if (!dragging) return;
      dragging = false; dv.classList.remove('is-dragging');
      document.removeEventListener('mousemove', onMove, true);
      document.removeEventListener('mouseup', onUp, true);
      document.body.style.userSelect = '';
    };
    dv.addEventListener('mousedown', (e) => {
      e.preventDefault(); e.stopPropagation();
      dragging = true; dv.classList.add('is-dragging');
      startX = e.clientX; startS = drawerWidthFor(S.drawerId); startR = S.api.dockWidth();
      document.body.style.userSelect = 'none';
      document.addEventListener('mousemove', onMove, true);
      document.addEventListener('mouseup', onUp, true);
    });
  }
  function initResize() {
    const h = S.els.resize;
    let dragging = false, startX = 0, startY = 0, startW = 0, startH = 0;
    const onMove = (e) => {
      if (!dragging) return;
      const side = sideNow();
      const vw = window.innerWidth || 1200, vh = window.innerHeight || 800;
      if (side === 'right' || side === 'left') {
        const dx = (side === 'right') ? (startX - e.clientX) : (e.clientX - startX);
        S.api.persistSetting('md_preview_dock_width_px', clamp(Math.round(startW + dx), 320, vw - 60));
      } else {
        const dy = (side === 'top') ? (e.clientY - startY) : (startY - e.clientY);
        S.api.persistSetting('md_preview_dock_height_px', clamp(Math.round(startH + dy), 200, vh - 60));
      }
      applyLayout();
    };
    const onUp = () => {
      if (!dragging) return;
      dragging = false; h.classList.remove('is-dragging');
      document.removeEventListener('mousemove', onMove, true);
      document.removeEventListener('mouseup', onUp, true);
      document.body.style.userSelect = '';
    };
    h.addEventListener('mousedown', (e) => {
      e.preventDefault(); e.stopPropagation();
      dragging = true; h.classList.add('is-dragging');
      startX = e.clientX; startY = e.clientY;
      startW = S.api.dockWidth(); startH = S.api.dockHeight();
      document.body.style.userSelect = 'none';
      document.addEventListener('mousemove', onMove, true);
      document.addEventListener('mouseup', onUp, true);
    });
  }
  function initSplitter() {
    const sp = S.els.splitter;
    let dragging = false;
    const onMove = (e) => {
      if (!dragging) return;
      const split = S.els.split, outline = S.els.outline;
      const horizontal = split.getAttribute('data-outline') === 'side';
      const r = outline.getBoundingClientRect();
      const total = horizontal ? split.clientWidth : split.clientHeight;
      if (!total) return;
      const pos = horizontal ? (e.clientX - r.left) : (e.clientY - r.top);
      const minFrac = Math.min(0.88, (horizontal ? 120 : 28) / total);
      const frac = clamp(pos / total, minFrac, 0.88);
      S.api.persistSetting('md_preview_dock_list_fraction', frac);
      try { S.api.applyListFraction(); } catch (err) { /* ignore */ }
    };
    const onUp = () => {
      if (!dragging) return;
      dragging = false; sp.classList.remove('is-dragging');
      document.removeEventListener('mousemove', onMove, true);
      document.removeEventListener('mouseup', onUp, true);
      document.body.style.userSelect = '';
    };
    sp.addEventListener('mousedown', (e) => {
      e.preventDefault(); e.stopPropagation();
      dragging = true; sp.classList.add('is-dragging');
      document.body.style.userSelect = 'none';
      document.addEventListener('mousemove', onMove, true);
      document.addEventListener('mouseup', onUp, true);
    });
  }

  // ───────────────────────── deck open/close + drawers ─────────────────────────
  function isOpen() { return !!(S.els.body && S.els.body.style.display !== 'none'); }
  let lastOpenState = null;
  function syncOpenState() {
    const open = isOpen();
    // The body's style attribute also changes when applyLayout sizes it; only a
    // real open/close transition may re-run layout (otherwise the observer loops).
    if (open === lastOpenState) return;
    lastOpenState = open;
    if (S.els.deck) S.els.deck.setAttribute('data-open', open ? '1' : '0');
    if (!open) { closePop(); }
    applyLayout();
    // The first deck open of a session offers the first-run tips, once (showTips decides whether
    // a card is still undismissed).
    if (open && !S.tipsShown) { S.tipsShown = true; setTimeout(() => { try { showTips(false); } catch (e) { /* ignore */ } }, 350); }
  }
  function syncReaderBtn() {
    if (!S.els.railReader) return;
    const showing = isOpen() && !(S.drawerId && S.readerHidden);
    S.els.railReader.classList.toggle('is-active', showing);
    S.els.railReader.title = (isOpen() && S.drawerId)
      ? (showing ? 'Reader — hide the Reader pane (the panel stays)' : 'Reader — show the Reader pane beside the panel')
      : wordOf('readerRailTooltip', 'Reader — what your agent sees, rendered (click a Workflowy node to show it here)');
  }
  function registerDrawer(id, def) {
    S.drawers.set(id, { id, def, container: null, built: false });
    if (S.mounted) buildRail();
  }
  function openDrawer(id) {
    const d = S.drawers.get(id);
    if (!d) return;
    // The three states: a drawer that opens alone hides the Reader pane when the deck was closed;
    // a picker always brings the Reader (it picks something to render there).
    if (!isOpen()) { S.readerHidden = opensAlone(id); try { S.api.openReader(); } catch (e) { /* ignore */ } }
    else if (isPicker(id)) { S.readerHidden = false; }
    closePop();
    S.widened = false;
    S.drawerId = id;
    const dr = S.els.drawer;
    dr.setAttribute('data-open', '1');
    S.els.drawerTitle.textContent = d.def.title;
    if (d.def.hint) S.els.drawerTitle.appendChild(el('div', 'nx-drawer-byline', d.def.hint));   // the byline: the title's second half
    S.els.drawerActions.innerHTML = '';
    S.els.drawerBody.innerHTML = '';
    if (!d.built) {
      d.container = el('div');
      try { d.def.build(d.container, publicApi); } catch (e) { console.warn('[GlimpseUI] drawer build failed:', id, e); }
      d.built = true;
    }
    S.els.drawerBody.appendChild(d.container);
    try { if (d.def.headActions) d.def.headActions(S.els.drawerActions, publicApi); } catch (e) { /* ignore */ }
    if (isPicker(id)) {
      // Pin keeps a picker open beside the Reader as a narrow summary; widen opens the full sheet again.
      const pinned = S.pinned.has(id);
      if (pinned) {
        S.els.drawerActions.appendChild(iconBtn('columns', 'Widen: the full names, over Workflowy (narrows back after a pick)', (ev) => {
          S.widened = !S.widened;
          try { ev.currentTarget.classList.toggle('is-on', S.widened); } catch (e) { /* ignore */ }
          fitDrawer();
        }));
      }
      S.els.drawerActions.appendChild(iconBtn('pin', pinned ? 'Unpin: open as a wide sheet that closes after a pick' : 'Pin beside the Reader: a narrow summary that stays open', () => {
        if (S.pinned.has(id)) S.pinned.delete(id); else S.pinned.add(id);
        try { localStorage.setItem('glimpse_ui_pinned', JSON.stringify(Array.from(S.pinned))); } catch (e) { /* ignore */ }
        openDrawer(id);
      }, pinned ? 'is-on' : ''));
    }
    try { if (d.def.onOpen) d.def.onOpen(publicApi); } catch (e) { console.warn('[GlimpseUI] drawer open failed:', id, e); }
    S.els.drawerBody.scrollTop = 0;
    S.drawers.forEach((x) => { if (x.railBtn) x.railBtn.classList.toggle('is-active', x.id === id); });
    applyLayout();
    setTimeout(fitDrawer, 0);
  }
  function closeDrawer() {
    const prev = S.drawerId ? S.drawers.get(S.drawerId) : null;
    S.drawerId = null;
    S.widened = false;
    // Closing the last pane closes the deck: a panel that was open alone takes the deck with it.
    if (S.readerHidden) { S.readerHidden = false; try { S.api.closeReader(); } catch (e) { /* ignore */ } }
    if (S.els.drawer) S.els.drawer.setAttribute('data-open', '0');
    S.drawers.forEach((x) => { if (x.railBtn) x.railBtn.classList.remove('is-active'); });
    try { if (prev && prev.def.onClose) prev.def.onClose(publicApi); } catch (e) { /* ignore */ }
    applyLayout();
  }
  function toggleDrawer(id) { if (S.drawerId === id && isOpen()) closeDrawer(); else openDrawer(id); }
  function refreshDrawer(id) {
    const d = S.drawers.get(id);
    if (!d || !d.built) return;
    try { if (d.def.refresh) d.def.refresh(publicApi); } catch (e) { /* ignore */ }
  }
  // After a pick (a Dock opened, a slice group applied, a search fired): a transient picker
  // closes; a pinned one stays, and a widened one narrows back to its summary.
  function afterPick() {
    const id = S.drawerId;
    if (!id || !isPicker(id)) return;
    if (!S.pinned.has(id)) { closeDrawer(); return; }
    if (S.widened) { S.widened = false; fitDrawer(); }
  }
  // An agent's push lands in the Reader: re-show a hidden Reader pane (the 'panel alone' state)
  // and close a transient picker, so what the agent sent is on screen.
  function showReaderPane() {
    S.readerHidden = false;
    if (S.drawerId && isTransient(S.drawerId)) closeDrawer();
    if (!isOpen()) { try { S.api.openReader(); } catch (e) { /* ignore */ } }
    applyLayout();
  }
  // One mount root for every floating element the extension draws outside the deck (cursor menus,
  // dialogs, tooltips): a child of <html>, not <body>, so a push (which narrows <body> and, on a
  // left dock, shifts it) never moves them. Absolute children keep document coordinates; fixed
  // children keep the viewport.
  function layer() {
    let l = document.getElementById('glimpse-nx-layer');
    if (!l) { l = el('div', 'nx-layer'); l.id = 'glimpse-nx-layer'; document.documentElement.appendChild(l); }
    return l;
  }

  // ───────────────────────── first-run tips ("1 of 3") ─────────────────────────
  // Three dismissible cards (R2's words and pictures, glimpse-ui-help.js) at the deck body's
  // bottom-right, above the toasts, offered once per session when the deck first opens. A card's
  // id lands in the synced setting glimpse_help_dismissed when it is passed (Next) or when the set
  // is dismissed (Don't show these again, ×, Done, Open Help), so the cards never return unless the
  // Settings drawer or the Options page clears the list. force=true (those reset buttons) shows
  // card 1 again at once.
  const TIP_IDS = ['tip-rail', 'tip-docks', 'tip-slices'];
  function tipsDismissed() {
    let arr = [];
    try { arr = S.api.settings().glimpse_help_dismissed; } catch (e) { arr = []; }
    return Array.isArray(arr) ? arr.slice() : [];
  }
  function dismissTips(ids) {
    const next = tipsDismissed();
    ids.forEach((id) => { if (TIP_IDS.includes(id) && !next.includes(id)) next.push(id); });
    try { S.api.persistSetting('glimpse_help_dismissed', next); } catch (e) { /* ignore */ }
  }
  function closeTip() {
    if (S.els.tip) { try { S.els.tip.remove(); } catch (e) { /* ignore */ } S.els.tip = null; }
  }
  function showTips(force) {
    const help = root.GlimpseUiHelp;
    const tips = (help && Array.isArray(help.tips)) ? help.tips : null;
    if (!tips || !tips.length || !S.els.body || !isOpen()) return false;
    const seen = force ? [] : tipsDismissed();
    const idx = tips.findIndex((t) => t && !seen.includes(t.id));
    if (idx < 0) return false;
    paintTip(tips, idx);
    return true;
  }
  function paintTip(tips, idx) {
    closeTip();
    const t = tips[idx];
    const last = idx >= tips.length - 1;
    const card = el('div', 'nx-tip');
    card.setAttribute('data-tip', t.id);
    card.addEventListener('mousedown', (e) => e.stopPropagation());
    const head = el('div', 'nx-tip-head');
    head.appendChild(el('span', 'nx-tip-title', t.title));
    head.appendChild(iconBtn('x', 'Close these tips (they stay away; Settings → First-run tips brings them back)', () => { dismissTips(TIP_IDS); closeTip(); }));
    card.appendChild(head);
    if (t.image) { const img = el('div', 'nx-tip-img'); img.innerHTML = t.image; card.appendChild(img); }
    card.appendChild(el('div', 'nx-tip-body', t.body));
    if (t.closing) card.appendChild(el('div', 'nx-tip-closing', t.closing));
    const acts = el('div', 'nx-tip-actions');
    if (!last) {
      acts.appendChild(btn('Don’t show these again', 'Dismiss all ' + tips.length + ' cards', () => { dismissTips(TIP_IDS); closeTip(); }, 'is-ghost'));
      acts.appendChild(btn('Next', 'Tip ' + (idx + 2) + ' of ' + tips.length, () => { dismissTips([t.id]); paintTip(tips, idx + 1); }, 'is-primary'));
    } else {
      acts.appendChild(btn('Open Help', 'The Help drawer explains each part', () => { dismissTips(TIP_IDS); closeTip(); openDrawer('help'); }, 'is-ghost', 'help'));
      acts.appendChild(btn('Done', '', () => { dismissTips(TIP_IDS); closeTip(); }, 'is-primary', 'check'));
    }
    card.appendChild(acts);
    S.els.body.appendChild(card);
    S.els.tip = card;
  }

  // ───────────────────────── rail ─────────────────────────
  function buildRail() {
    const rail = S.els.rail;
    if (!rail) return;
    rail.innerHTML = '';
    const top = el('div', 'nx-rail-top');
    const dot = el('span', 'nx-dot' + (S.connected ? ' is-on' : ''));
    dot.title = dotTitle(S.connected);
    S.els.dot = dot;
    top.appendChild(dot);
    const readerBtn = el('button', 'nx-rail-btn');
    readerBtn.type = 'button'; readerBtn.innerHTML = icon('reader');
    readerBtn.title = wordOf('readerRailTooltip', 'Reader — what your agent sees, rendered (click a Workflowy node to show it here)');
    // With a panel open the Reader icon hides or shows the Reader pane (the panel stays);
    // otherwise it opens or closes the deck.
    readerBtn.addEventListener('click', () => {
      if (isOpen() && S.drawerId) { S.readerHidden = !S.readerHidden; applyLayout(); }
      else if (isOpen()) S.api.closeReader();
      else S.api.openReader();
    });
    S.els.railReader = readerBtn;
    top.appendChild(readerBtn);
    rail.appendChild(top);

    const mid = el('div', 'nx-rail-mid');
    const ordered = Array.from(S.drawers.values()).sort((a, b) => (a.def.order || 0) - (b.def.order || 0));
    ordered.filter((d) => !d.def.bottom).forEach((d) => mid.appendChild(railButtonFor(d)));
    rail.appendChild(mid);

    const bottom = el('div', 'nx-rail-bottom');
    ordered.filter((d) => d.def.bottom).forEach((d) => bottom.appendChild(railButtonFor(d)));
    const pal = el('button', 'nx-rail-btn'); pal.type = 'button'; pal.innerHTML = icon('command');
    pal.title = wordOf('paletteRailTooltip', 'Command palette — every command, by name ({hotkey})').replace('{hotkey}', hotkeyLabel('command_palette') || 'Ctrl+Shift+K');
    pal.addEventListener('click', () => openPalette());
    bottom.appendChild(pal);
    const col = el('button', 'nx-rail-btn'); col.type = 'button'; col.innerHTML = icon(sideNow() === 'left' ? 'expand' : 'collapse');
    col.title = isOpen() ? 'Close the deck (' + hotkeyLabel('toggle_deck') + ')' : 'Open the deck (' + hotkeyLabel('toggle_deck') + ')';
    col.addEventListener('click', () => toggleDeck());
    bottom.appendChild(col);
    rail.appendChild(bottom);
    S.drawers.forEach((d) => { if (d.railBtn) d.railBtn.classList.toggle('is-active', d.id === S.drawerId); });
    syncOpenState();
  }
  function railButtonFor(d) {
    const b = el('button', 'nx-rail-btn');
    b.type = 'button';
    b.innerHTML = icon(d.def.icon || 'more');
    b.title = d.def.title + (d.def.hint ? ' — ' + d.def.hint : '');
    b.addEventListener('click', () => toggleDrawer(d.id));
    if (d.id === 'jobs') {
      const badge = el('span', 'nx-rail-badge' + (S.jobs ? ' is-on' : ''), S.jobs ? String(S.jobs) : '');
      S.els.jobsBadge = badge;
      b.appendChild(badge);
    }
    d.railBtn = b;
    return b;
  }
  function toggleDeck() { if (isOpen()) S.api.closeReader(); else S.api.openReader(); }
  function hotkeyLabel(action) {
    try { const h = S.api.hotkeys(); return (h && h[action]) || ''; } catch (e) { return ''; }
  }

  // ───────────────────────── Reader ─────────────────────────
  function buildReader() {
    const reader = el('div', 'nx-reader');
    const head = el('div', 'nx-reader-head');
    const crumb = el('div', 'nx-crumb');
    const title = el('div', 'nx-crumb-title', 'Reader');
    const meta = el('div', 'nx-crumb-meta');
    crumb.appendChild(title); crumb.appendChild(meta);
    head.appendChild(crumb);
    const actions = el('div', 'nx-actions');
    const freezeBtn = iconBtn('unlock', 'Freeze', () => S.api.toggleFreeze());
    const refreshBtn = iconBtn('refresh', 'Refresh', () => S.api.refresh());
    const syncBtn = iconBtn('sync', 'Sync the live Workflowy pane to the applied slice', () => S.api.syncView());
    syncBtn.style.display = 'none';
    const viewBtn = iconBtn('eye', 'View: sort, headings, since, layout, theme', () => openViewPop(viewBtn));
    const copyBtn = iconBtn('copy', 'Copy…', () => openCopyMenu(copyBtn));
    const tagsBtn = iconBtn('tag', 'Batch-edit tags on the selected rows', () => S.api.openTags());
    const closeBtn = iconBtn('x', 'Close the deck (the document is kept; ' + hotkeyLabel('markdown_preview_close') + ' clears it)', () => S.api.closeReader());
    [freezeBtn, refreshBtn, syncBtn, viewBtn, copyBtn, tagsBtn, closeBtn].forEach((b) => actions.appendChild(b));
    head.appendChild(actions);
    reader.appendChild(head);

    const warn = el('div', 'nx-notice nx-notice-warn'); warn.style.display = 'none';
    const sync = el('div', 'nx-notice nx-notice-sync'); sync.style.display = 'none';
    const freezeNote = el('div', 'nx-notice nx-notice-freeze'); freezeNote.style.display = 'none';
    reader.appendChild(warn); reader.appendChild(sync); reader.appendChild(freezeNote);

    const split = el('div', 'nx-split'); split.setAttribute('data-outline', 'top');
    const outline = el('div', 'nx-outline glimpse-md-row-list'); outline.tabIndex = 0;
    outline.addEventListener('keydown', (evt) => {
      const st = S.lastState || {};
      if (evt.key === 'ArrowDown') { evt.preventDefault(); S.api.selectRow((st.selectedIndex || 0) + 1); }
      else if (evt.key === 'ArrowUp') { evt.preventDefault(); S.api.selectRow((st.selectedIndex || 0) - 1); }
    });
    const splitter = el('div', 'nx-splitter');
    const doc = el('div', 'nx-doc');
    const tools = el('div', 'nx-doc-tools');
    const viewSeg = el('span', 'nx-seg');
    const segRendered = el('button', 'is-on', 'Rendered'); segRendered.type = 'button';
    const segSource = el('button', '', 'Source'); segSource.type = 'button';
    segRendered.title = 'Rendered Markdown (marked + KaTeX)'; segSource.title = 'The identical composed text as plain source (find works here)';
    segRendered.addEventListener('click', () => S.api.setSourceView('rendered'));
    segSource.addEventListener('click', () => S.api.setSourceView('source'));
    viewSeg.appendChild(segRendered); viewSeg.appendChild(segSource);
    tools.appendChild(viewSeg);
    const find = el('input', 'nx-input nx-find'); find.type = 'text';
    find.placeholder = 'find in text — terms, "phrases", * globs (Ctrl+F)';
    find.title = 'Highlights every match in place in Source view and raw-text renders. Enter = next, Shift+Enter = previous, Esc = clear.';
    let findTimer = null;
    find.addEventListener('input', () => { if (findTimer) clearTimeout(findTimer); findTimer = setTimeout(() => S.api.applyFind(find.value), 150); });
    find.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); if (findTimer) { clearTimeout(findTimer); findTimer = null; } S.api.applyFind(find.value, { nav: e.shiftKey ? -1 : 1 }); }
      else if (e.key === 'Escape') { find.value = ''; S.api.applyFind(''); }
    });
    const prev = iconBtn('up', 'Previous match (Shift+Enter)', () => S.api.navigateFind(-1));
    const next = iconBtn('down', 'Next match (Enter)', () => S.api.navigateFind(1));
    const count = el('span', 'nx-find-count', '');
    tools.appendChild(find); tools.appendChild(prev); tools.appendChild(next); tools.appendChild(count);
    const render = el('div', 'nx-render glimpse-md-render');
    render.id = 'glimpse-md-dock-render';
    doc.appendChild(tools); doc.appendChild(render);
    split.appendChild(outline); split.appendChild(splitter); split.appendChild(doc);
    reader.appendChild(split);
    const foot = el('div', 'nx-reader-foot', '');
    reader.appendChild(foot);
    reader.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && String(e.key).toLowerCase() === 'f') {
        e.preventDefault(); e.stopPropagation();
        if (S.lastState && S.lastState.sourceViewMode !== 'source' && S.lastState.contentFormat !== 'raw_text') S.api.setSourceView('source');
        find.focus(); find.select();
      }
    }, true);
    Object.assign(S.els, {
      reader, crumbTitle: title, crumbMeta: meta, freezeBtn, refreshBtn, syncBtn, viewBtn, copyBtn, tagsBtn, closeBtn,
      warn, sync, freezeNote, split, outline, splitter, doc, render, foot, find, findPrev: prev, findNext: next, findCount: count,
      segRendered, segSource,
    });
    // The sync banner is painted by the client; mirror its visibility onto the header button.
    try {
      new MutationObserver(() => { syncBtn.style.display = (sync.style.display !== 'none') ? '' : 'none'; })
        .observe(sync, { attributes: true, attributeFilter: ['style'] });
    } catch (e) { /* ignore */ }
    return reader;
  }

  function composeStatus(st) {
    const fmt = S.api.fmt;
    const rs = (st.source === 'slice' && st.renderScope && typeof st.renderScope === 'object') ? st.renderScope : null;
    let title = 'Reader';
    let kindChip = { cls: 'is-live', text: 'LIVE' };
    const bits = [];
    if (st.source === 'slice') {
      const k = rs ? rs.kind : null;
      const gname = (rs && rs.group_name) ? fmt.sliceGroupDisplayName(rs.group_name) : null;
      const gid = rs ? rs.group_id : null;
      const rootU = rs ? rs.root_uuid : null;
      const rt = rootU ? String(rootU).slice(0, 8) : '?';
      let src;
      if (k === 'lightning_rod') {
        const sel = rs ? String(rs.selection_kind || '') : '';
        const selO = (rs && rs.selection && typeof rs.selection === 'object') ? rs.selection : null;
        if (sel === 'gather') { title = '⚡ Gathered tags under ' + rt; src = '⚡ gathered tags under ' + rt; }
        else if (sel === 'tags') { const tg = (selO && Array.isArray(selO.tags)) ? selO.tags.join(', ') : ''; title = '⚡ Tags [' + tg + '] under ' + rt; src = '⚡ tag filter [' + tg + '] under ' + rt; }
        else if (sel === 'symbols') {
          const n = (selO && Array.isArray(selO.symbols)) ? selO.symbols.length : 0;
          const roots = (selO && Array.isArray(selO.root_uuids)) ? selO.root_uuids : (rootU ? [rootU] : []);
          title = '⚡ ' + n + ' symbol' + (n === 1 ? '' : 's') + ' under ' + roots.length + ' root' + (roots.length === 1 ? '' : 's'); src = title.replace('⚡ ', '⚡ ');
        } else { const q = (selO && typeof selO.query === 'string') ? selO.query : ''; title = '⚡ Search “' + q + '” under ' + rt; src = '⚡ search “' + q + '” under ' + rt; }
      } else if (k === 'raw_root' || (!k && !gid && rootU)) {
        title = (st.rootName ? st.rootName : ('Dock ' + rt));
        src = 'raw dump of ' + rt;
      } else {
        const nm = gname ? String(gname) : (gid ? ('id ' + String(gid).slice(0, 8)) : '(group)');
        title = 'Slice · ' + nm + ((k === 'slice_override' && rootU) ? (' @ ' + rt) : '');
        src = 'applied slice “' + nm + '”' + ((k === 'slice_override' && rootU) ? (' @ ' + rt) : '');
      }
      bits.push('source: ' + src);
      if (!(rs && rs.kind === 'lightning_rod')) bits.push('render may differ slightly from the live Workflowy GUI (by design)');
      kindChip = { cls: 'is-slice', text: 'SLICE' };
    } else {
      title = st.rootName ? st.rootName : (st.rows && st.rows.length ? 'Live view' : 'Reader');
      bits.push('source: live DOM');
      kindChip = st.frozen ? { cls: 'is-frozen', text: 'FROZEN' } : { cls: 'is-live', text: 'LIVE' };
    }
    const sb = rs ? (rs.sort_by || 'none') : st.sortBy;
    const sdir = rs ? (rs.sort_direction || 'asc') : st.sortDir;
    bits.push('sort: ' + (sb === 'none' || !sb ? 'none' : (sb + ' ' + sdir)));
    const headingsOn = rs ? !!rs.headings_only : !!st.headingsOnly;
    if (headingsOn) bits.push('headings-only');
    const sinceRaw = rs ? (rs.since || null) : st.since;
    const tok = fmt.sinceToDisplayToken(sinceRaw);
    if (tok) {
      const isObj = sinceRaw && typeof sinceRaw === 'object';
      const shape = isObj ? (sinceRaw.form === 'window' ? 'ok-window' : (sinceRaw.form === 'curated' ? 'ok-curated' : 'ok-count')) : fmt.sinceShapeState(tok);
      let m;
      if (shape === 'ok-count') m = 'item count'; else if (shape === 'bad') m = 'bad token';
      else if (shape === 'ok-curated') m = (st.source === 'slice') ? 'curation cursor' : 'needs a slice'; else m = 'time window';
      bits.push('since: ' + tok + ' (' + m + ')');
    }
    if (st.frozen && st.source !== 'slice') bits.push('frozen');
    return { title, kindChip, footer: bits.join('  ·  '), sortLabel: (sb && sb !== 'none') ? (sb + ' ' + (sdir === 'desc' ? '▼' : '▲')) : '', headingsOn, sinceTok: tok };
  }

  function updateReader(st) {
    if (!S.els.reader || !st) return;
    S.lastState = st;
    const c = composeStatus(st);
    S.els.crumbTitle.textContent = c.title;
    S.els.crumbTitle.title = c.title;
    const meta = S.els.crumbMeta;
    meta.innerHTML = '';
    const kind = el('span', 'nx-chip ' + c.kindChip.cls, c.kindChip.text);
    kind.title = (c.kindChip.text === 'SLICE') ? 'A complete, cache-sourced document. Workflowy clicks do not disturb it; Unfreeze exits to the live view.'
      : (c.kindChip.text === 'FROZEN' ? 'Browsing a captured snapshot; the live tree may change underneath.' : 'Mirrors the live Workflowy view; click a node to re-root.');
    meta.appendChild(kind);
    if (st.sizeLabel) meta.appendChild(el('span', 'nx-chip is-quiet', st.sizeLabel));
    if (typeof st.leafCount === 'number' && st.leafCount >= 0) {
      const lc = el('span', 'nx-chip is-quiet', 'bottom ' + st.leafCount);
      lc.title = 'Leaves from the selected row to the end of this view: the value to pass as since="bottom N".';
      meta.appendChild(lc);
    }
    if (c.sortLabel) meta.appendChild(el('span', 'nx-chip is-quiet', c.sortLabel));
    if (c.headingsOn) meta.appendChild(el('span', 'nx-chip is-quiet', 'headings'));
    if (c.sinceTok) meta.appendChild(el('span', 'nx-chip is-quiet', 'since ' + c.sinceTok));
    if (st.copyIdMode) meta.appendChild(el('span', 'nx-chip is-quiet', 'copy id'));
    // actions
    const fb = S.els.freezeBtn;
    fb.innerHTML = icon(st.frozen ? 'lock' : 'unlock');
    fb.classList.toggle('is-on', !!st.frozen);
    fb.title = (st.source === 'slice')
      ? 'Frozen (slice). Click to unfreeze: exits slice mode back to the live view; the document stays on screen until your next click or Refresh.'
      : (st.frozen ? 'Frozen: browsing a snapshot. Click to unfreeze and resume live tracking.' : 'Freeze: stop tracking Workflowy and browse a captured snapshot (enables sort and since).');
    S.els.refreshBtn.disabled = !(st.rootUuid || st.source === 'slice');
    S.els.refreshBtn.title = (st.source === 'slice') ? 'Re-apply this slice from the current nodes cache' : 'Re-walk the visible Workflowy tree';
    S.els.tagsBtn.disabled = !st.canMutateTags;
    S.els.tagsBtn.title = st.canMutateTags ? 'Batch-edit tags on the selected rows' : 'Select rows (Ctrl-click, Shift-click) in a slice or a frozen view to batch-edit tags';
    // doc tools
    const isSrc = (st.sourceViewMode === 'source');
    S.els.segRendered.classList.toggle('is-on', !isSrc);
    S.els.segSource.classList.toggle('is-on', isSrc);
    const fs = st.find || {};
    S.els.find.disabled = !fs.active;
    S.els.findPrev.disabled = !fs.navOk; S.els.findNext.disabled = !fs.navOk;
    const fc = S.els.findCount;
    fc.className = 'nx-find-count';
    if (!fs.active) fc.textContent = '';
    else if (fs.error) { fc.textContent = '⚠ ' + fs.error; fc.classList.add('is-err'); }
    else if (!fs.hasRegex) fc.textContent = '';
    else if (fs.count === 0) fc.textContent = '0 matches';
    else { fc.textContent = (fs.current + 1) + '/' + fs.count; fc.classList.add('is-hit'); }
    S.els.foot.textContent = c.footer;
    if (S.viewPopRefresh) { try { S.viewPopRefresh(); } catch (e) { /* ignore */ } }
  }

  // ───────────────────────── View popover ─────────────────────────
  function openViewPop(anchor) {
    openPop(anchor, (pop) => {
      const paint = () => {
        pop.innerHTML = '';
        const st = S.lastState || S.api.state();
        pop.appendChild(el('div', 'nx-pop-title', 'View'));
        const allowed = !!st.sortSinceAllowed;
        const row1 = el('div', 'nx-pop-row');
        row1.appendChild(el('label', '', 'Sort'));
        row1.appendChild(seg([
          { value: 'none', label: 'Document' }, { value: 'created', label: 'Created' }, { value: 'modified', label: 'Modified' },
        ].map((o) => Object.assign(o, { disabled: !allowed })), st.sortBy || 'none', (v) => { S.api.setSort(v); paint(); }));
        const dir = iconBtn(st.sortDir === 'desc' ? 'down' : 'up', 'Toggle ascending (oldest first) / descending (newest first)', () => { S.api.toggleSortDir(); paint(); });
        dir.disabled = !allowed || (st.sortBy || 'none') === 'none';
        row1.appendChild(dir);
        pop.appendChild(row1);
        if (!allowed) pop.appendChild(el('div', 'nx-pop-hint', 'Sort and since need a frozen view or an applied slice.'));
        const row2 = el('div', 'nx-pop-row');
        row2.appendChild(el('label', '', 'Headings'));
        row2.appendChild(seg([{ value: false, label: 'With notes' }, { value: true, label: 'Headings only' }], !!st.headingsOnly, (v) => { if (v !== !!st.headingsOnly) S.api.toggleHeadings(); paint(); }));
        pop.appendChild(row2);
        const row3 = el('div', 'nx-pop-row');
        row3.appendChild(el('label', '', 'since'));
        const since = el('input', 'nx-input is-grow'); since.type = 'text';
        since.placeholder = '1w / last 10 / curated';
        since.value = st.sinceToken || '';
        since.disabled = !allowed;
        since.title = 'Recent-tail filter. Window: 1w · 3d · 12h · 90m (bare number = days). Count: last 10 / newest 10 / bottom 10. curated: everything since the last curation pass (applied slices only). Suffix: 1w@modified desc.';
        const hint = el('div', 'nx-pop-hint', '');
        const paintHint = () => {
          const shape = S.api.fmt.sinceShapeState(since.value);
          hint.className = 'nx-pop-hint';
          if (!allowed) { hint.textContent = 'freeze to use'; return; }
          if (shape === 'empty') hint.textContent = 'off';
          else if (shape === 'ok-count') { hint.textContent = '→ item count (last / newest / bottom N)'; hint.classList.add('is-ok'); }
          else if (shape === 'bad') { hint.textContent = '⚠ bad token'; hint.classList.add('is-warn'); }
          else if (shape === 'ok-curated') { if (st.source === 'slice') { hint.textContent = '→ curation cursor'; hint.classList.add('is-ok'); } else { hint.textContent = '⚠ curated needs a slice'; hint.classList.add('is-warn'); } }
          else { hint.textContent = '→ time window'; hint.classList.add('is-ok'); }
        };
        since.addEventListener('input', paintHint);
        const commit = () => {
          if (!allowed) return;
          const res = S.api.commitSince(since.value.trim());
          if (res && res.error) { hint.textContent = '⚠ ' + res.error; hint.className = 'nx-pop-hint is-warn'; }
        };
        since.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); commit(); since.blur(); } });
        since.addEventListener('blur', commit);
        row3.appendChild(since);
        pop.appendChild(row3); pop.appendChild(hint); paintHint();
        pop.appendChild(el('div', 'nx-menu-sep'));
        let outlinePref = 'auto'; try { outlinePref = String(S.api.settings().glimpse_ui_outline || 'auto'); } catch (e) { /* ignore */ }
        const row4 = el('div', 'nx-pop-row');
        row4.appendChild(el('label', '', 'Outline'));
        row4.appendChild(seg([{ value: 'auto', label: 'Auto' }, { value: 'side', label: 'Beside' }, { value: 'top', label: 'Above' }], outlinePref, (v) => { S.api.persistSetting('glimpse_ui_outline', v); applyLayout(); paint(); }));
        pop.appendChild(row4);
        const row5 = el('div', 'nx-pop-row');
        row5.appendChild(el('label', '', 'Dock side'));
        const sideWrap = el('span', 'nx-seg');
        const sideIcons = { left: 'left', top: 'up', bottom: 'down', right: 'right' };
        ['left', 'top', 'bottom', 'right', 'auto'].forEach((s) => {
          const b = el('button', (sidePref() === s) ? 'is-on' : ''); b.type = 'button';
          if (s === 'auto') { b.textContent = 'Auto'; b.title = 'Auto: bottom on a window taller than wide (pickers get the width), right otherwise'; }
          else { b.innerHTML = icon(sideIcons[s], 13); b.title = 'Dock ' + s; }
          b.addEventListener('click', (ev) => { ev.stopPropagation(); S.api.persistSetting('md_preview_dock_position', s); applyLayout(); buildRail(); paint(); });
          sideWrap.appendChild(b);
        });
        row5.appendChild(sideWrap);
        pop.appendChild(row5);
        let themePref = 'auto'; try { themePref = String(S.api.settings().glimpse_ui_theme || 'auto'); } catch (e) { /* ignore */ }
        const row6 = el('div', 'nx-pop-row');
        row6.appendChild(el('label', '', 'Theme'));
        row6.appendChild(seg([{ value: 'auto', label: 'Follow Workflowy' }, { value: 'light', label: 'Light' }, { value: 'dark', label: 'Dark' }], themePref, (v) => { S.api.persistSetting('glimpse_ui_theme', v); applyTheme(); paint(); }));
        pop.appendChild(row6);
      };
      paint();
      S.viewPopRefresh = paint;
    }, { width: 340 });
  }

  function openCopyMenu(anchor) {
    const st = S.lastState || S.api.state();
    openMenu(anchor, [
      { label: 'Copy as Markdown', detail: 'The source of what is shown', icon: 'copy', run: () => S.api.copyMarkdown((ok) => toast(ok ? 'Copied Markdown.' : 'Copy failed.', 2000)) },
      { label: 'Select all and copy', icon: 'text', run: () => S.api.selectAllCopy() },
      { label: 'Copy focus spec for an agent', detail: 'render_spec JSON: scope + selected row', icon: 'command', disabled: st.source !== 'slice', run: () => S.api.copyFocus() },
      { label: 'Copy id: search string of the selected row', icon: 'hash', disabled: !(st.selectedUuid), run: () => S.api.copySelectedId() },
      { sep: true },
      { label: 'Copy id on every selection', detail: 'Toggle: selecting a row also copies id:<uuid>', icon: 'pin', checked: !!st.copyIdMode, run: () => S.api.toggleCopyId() },
    ], { title: 'Copy' });
  }

  // ───────────────────────── cursor menu renderer (Ctrl+F12 and pickers) ─────────────────────────
  function renderMenu(host, spec) {
    host.innerHTML = '';
    host.classList.add('nx-float');
    host.setAttribute('data-theme', S.theme);
    if (spec.title) host.appendChild(el('div', 'nx-menu-title', spec.title));
    if (spec.subtitle) { const s = el('div', 'nx-menu-sub', spec.subtitle); s.title = spec.subtitle; host.appendChild(s); }
    if (spec.pending) host.appendChild(el('div', 'nx-menu-pending', spec.pending));
    (spec.rows || []).forEach((r) => {
      const b = el('button', 'nx-menu-item' + (r.armed ? ' is-armed' : ''));
      b.type = 'button';
      if (r.disabled) b.disabled = true;
      const k = el('kbd', 'nx-kbd', r.key); k.style.marginLeft = '0';
      b.appendChild(k);
      const lab = el('span', '', r.label);
      if (r.detail) lab.appendChild(el('span', 'nx-menu-detail', r.detail));
      b.appendChild(lab);
      b.addEventListener('click', (ev) => { ev.stopPropagation(); try { r.onClick(ev); } catch (e) { /* ignore */ } });
      host.appendChild(b);
    });
    if (spec.extra) host.appendChild(spec.extra);
    if (spec.footer) host.appendChild(el('div', 'nx-menu-foot', spec.footer));
  }

  // ───────────────────────── command palette ─────────────────────────
  function allCommands() {
    const cmds = [];
    const hk = (a) => hotkeyLabel(a);
    // Type-to-go: the fast path when the name is known (browsing stays in the sheets).
    cmds.push({ group: 'Go to', label: 'Go to Dock…', detail: 'type a Dock name; Enter opens it in the Reader', run: () => openPalette('docks') });
    cmds.push({ group: 'Go to', label: 'Apply slice group…', detail: 'type a slice group name; Enter applies it', run: () => openPalette('slices') });
    cmds.push({ group: 'Deck', label: isOpen() ? 'Close the deck' : 'Open the deck', kbd: hk('toggle_deck'), run: () => toggleDeck() });
    S.drawers.forEach((d) => cmds.push({ group: 'Deck', label: 'Open ' + d.def.title, run: () => openDrawer(d.id) }));
    const st = S.lastState || (S.api ? S.api.state() : {});
    cmds.push({ group: 'Reader', label: st.frozen ? 'Unfreeze' : 'Freeze', run: () => S.api.toggleFreeze() });
    cmds.push({ group: 'Reader', label: 'Refresh', run: () => S.api.refresh() });
    cmds.push({ group: 'Reader', label: 'Sync the live Workflowy pane to the slice', run: () => S.api.syncView() });
    cmds.push({ group: 'Reader', label: 'Toggle Rendered / Source', run: () => S.api.setSourceView(st.sourceViewMode === 'source' ? 'rendered' : 'source') });
    cmds.push({ group: 'Reader', label: 'Toggle headings-only', run: () => S.api.toggleHeadings() });
    cmds.push({ group: 'Reader', label: 'Copy as Markdown', run: () => S.api.copyMarkdown((ok) => toast(ok ? 'Copied Markdown.' : 'Copy failed.', 2000)) });
    cmds.push({ group: 'Reader', label: 'Copy focus spec for an agent', run: () => S.api.copyFocus() });
    cmds.push({ group: 'Reader', label: 'Batch-edit tags on the selected rows', run: () => S.api.openTags() });
    cmds.push({ group: 'Reader', label: 'Clear the Reader and close', kbd: hk('markdown_preview_close'), run: () => S.api.clearReader() });
    cmds.push({ group: 'Appearance', label: 'Theme: follow Workflowy', run: () => { S.api.persistSetting('glimpse_ui_theme', 'auto'); applyTheme(); } });
    cmds.push({ group: 'Appearance', label: 'Theme: light', run: () => { S.api.persistSetting('glimpse_ui_theme', 'light'); applyTheme(); } });
    cmds.push({ group: 'Appearance', label: 'Theme: dark', run: () => { S.api.persistSetting('glimpse_ui_theme', 'dark'); applyTheme(); } });
    ['right', 'left', 'top', 'bottom', 'auto'].forEach((s) => cmds.push({ group: 'Appearance', label: 'Dock ' + s, run: () => { S.api.persistSetting('md_preview_dock_position', s); applyLayout(); buildRail(); } }));
    cmds.push({ group: 'Tools', label: 'Resolve a UUID path…', run: () => openUuidPathPrompt() });
    S.drawers.forEach((d) => { try { if (d.def.commands) d.def.commands(publicApi).forEach((c) => cmds.push(Object.assign({ group: d.def.title }, c))); } catch (e) { /* ignore */ } });
    return cmds;
  }

  // ───────────────────────── UUID path tool ─────────────────────────
  // The old widget's paste-to-path box as a palette command: paste a UUID (or
  // any text carrying one: an F2/F3 copy, a Workflowy link), the daemon
  // resolves the node's ancestor path (resolve_uuid_path) and the result lands
  // in the Activity log (Jobs drawer), where one click copies it.
  let uuidPromptEl = null;
  function closeUuidPathPrompt() { if (uuidPromptEl) { try { uuidPromptEl.remove(); } catch (e) { /* ignore */ } uuidPromptEl = null; } }
  function openUuidPathPrompt() {
    closeUuidPathPrompt(); closePalette();
    const wrap = el('div', 'nx-palette-wrap');
    const box = el('div', 'nx-dialog nx-float');
    box.setAttribute('data-theme', S.theme);
    box.style.position = 'static'; box.style.transform = 'none'; box.style.width = '520px';
    const head = el('div', 'nx-dialog-head');
    head.appendChild(el('div', 'nx-dialog-title', 'Resolve a UUID path'));
    head.appendChild(iconBtn('x', 'Close (Esc)', () => closeUuidPathPrompt(), 'nx-dialog-close'));
    box.appendChild(head);
    box.appendChild(el('div', 'nx-dialog-hint', 'Paste a Workflowy node UUID, or any text that carries one (an F2/F3 copy, a Workflowy link). The daemon resolves the node’s path from the nodes cache; the result lands in the Activity log (Jobs drawer), where a click copies it.'));
    const input = el('input', 'nx-input'); input.type = 'text'; input.placeholder = 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx';
    input.style.width = '100%';
    const err = el('div', 'nx-dialog-err'); err.style.display = 'none';
    const go = () => {
      let uuid = null;
      try { uuid = S.api.extractUuid(input.value); } catch (e) { uuid = null; }
      if (!uuid) { err.textContent = 'No UUID found in that text.'; err.style.display = 'block'; return; }
      closeUuidPathPrompt();
      try { S.api.resolveUuidPath(uuid); } catch (e) { toast('⚠ Could not request the path: ' + (e && e.message ? e.message : String(e)), 5000, 'warn'); return; }
      openDrawer('jobs');
      toast('Resolving the path of ' + uuid.slice(0, 8) + '… (Activity log)', 2500);
    };
    const foot = el('div', 'nx-dialog-foot');
    foot.appendChild(el('span', 'nx-spacer'));
    foot.appendChild(btn('Cancel', '', () => closeUuidPathPrompt(), 'is-ghost'));
    foot.appendChild(btn('Resolve', 'Ask the daemon for this node’s path (Enter)', () => go(), 'is-primary', 'search'));
    box.appendChild(input); box.appendChild(err); box.appendChild(foot);
    wrap.appendChild(box);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); go(); } else if (e.key === 'Escape') { e.preventDefault(); closeUuidPathPrompt(); } e.stopPropagation(); });
    input.addEventListener('keyup', (e) => e.stopPropagation());
    wrap.addEventListener('mousedown', (e) => { if (e.target === wrap) closeUuidPathPrompt(); });
    box.addEventListener('mousedown', (e) => e.stopPropagation());
    (S.els.deck || document.body).appendChild(wrap);
    uuidPromptEl = wrap;
    input.focus();
  }
  let paletteEl = null;
  function closePalette() { if (paletteEl) { try { paletteEl.remove(); } catch (e) { /* ignore */ } paletteEl = null; } }
  // Type-to-go (R1's option 1, kept as a complement to the sheets): openPalette('docks') lists every
  // Dock by its full name and Enter opens it in the Reader; openPalette('slices') lists every slice
  // group by its full path and Enter applies it. The plain palette lists every command.
  function paletteItems(mode) {
    if (mode === 'docks') {
      const docks = (publicApi.anchors && publicApi.anchors.flat) ? publicApi.anchors.flat() : null;
      if (!docks) return { items: [], pending: true };
      return { items: docks.map((dk) => ({ group: 'Anchor Docks', label: dk.name, detail: dk.path || '', run: () => publicApi.anchors.open(dk.id, dk.name) })) };
    }
    if (mode === 'slices') {
      let groups = []; try { groups = S.api.groups(); } catch (e) { groups = []; }
      return { items: groups.map((g) => {
        const segs = String(g.name || '').split('|');
        const cat = segs.slice(0, -1).join(' › ');
        const tg = (g.prefixes || []).length ? '#' + (g.prefixes || []).join(' #') : '';
        return { group: 'Slice groups', label: S.api.fmt.sliceGroupDisplayName(g.name), detail: [cat, tg].filter(Boolean).join('  ·  '), run: () => { if (publicApi.slices && publicApi.slices.applyDefault) publicApi.slices.applyDefault(g.id); } };
      }) };
    }
    return { items: allCommands() };
  }
  function openPalette(mode) {
    const m = (mode === 'docks' || mode === 'slices') ? mode : null;
    if (paletteEl && !m) { closePalette(); return; }
    closePalette();
    const wrap = el('div', 'nx-palette-wrap');
    const box = el('div', 'nx-palette');
    box.setAttribute('data-theme', S.theme);
    box.className = 'nx-palette nx-float';
    const input = el('input'); input.type = 'text';
    input.placeholder = (m === 'docks') ? 'Type a Dock name… (Enter opens it in the Reader)'
      : (m === 'slices') ? 'Type a slice group name… (Enter applies it)'
        : wordOf('palettePlaceholder', 'Type a command… (↑ ↓ choose, Enter runs, Esc closes)');
    const list = el('div', 'nx-palette-list');
    box.appendChild(input); box.appendChild(list); wrap.appendChild(box);
    wrap.addEventListener('mousedown', (e) => { if (e.target === wrap) closePalette(); });
    box.addEventListener('mousedown', (e) => e.stopPropagation());
    let focus = 0, shown = [];
    const paint = () => {
      const q = input.value.trim().toLowerCase();
      const src = paletteItems(m);
      shown = src.items.filter((c) => !q || (c.label + ' ' + (c.detail || '') + ' ' + (c.group || '')).toLowerCase().includes(q));
      list.innerHTML = '';
      let lastGroup = null;
      shown.forEach((c, i) => {
        if (c.group !== lastGroup) { list.appendChild(el('div', 'nx-palette-group', c.group || '')); lastGroup = c.group; }
        const b = el('button', 'nx-menu-item' + (i === focus ? ' is-focus' : ''));
        b.type = 'button';
        const lab = el('span', '', c.label);
        if (c.detail) lab.appendChild(el('span', 'nx-menu-detail', c.detail));
        b.appendChild(lab);
        if (c.kbd) b.appendChild(el('kbd', 'nx-kbd', c.kbd));
        b.addEventListener('click', () => { closePalette(); try { c.run(); } catch (e) { console.warn('[GlimpseUI] command failed:', e); } });
        list.appendChild(b);
      });
      if (!shown.length) list.appendChild(el('div', 'nx-empty', src.pending ? 'Loading the Dock list…' : (m ? 'No match.' : 'No matching command.')));
    };
    if (m === 'docks' && publicApi.anchors && publicApi.anchors.ensure) publicApi.anchors.ensure(() => { if (paletteEl === wrap) paint(); });
    input.addEventListener('input', () => { focus = 0; paint(); });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); focus = Math.min(shown.length - 1, focus + 1); paint(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); focus = Math.max(0, focus - 1); paint(); }
      else if (e.key === 'Enter') { e.preventDefault(); const c = shown[focus]; if (c) { closePalette(); try { c.run(); } catch (err) { /* ignore */ } } }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closePalette(); }
      e.stopPropagation();
    });
    input.addEventListener('keyup', (e) => e.stopPropagation());
    (S.els.deck || document.body).appendChild(wrap);
    paletteEl = wrap;
    paint();
    input.focus();
  }

  // ───────────────────────── mount ─────────────────────────
  function mount(api) {
    if (S.mounted) return S.els;
    S.api = api;
    const deck = el('div', 'nx-deck');
    deck.id = 'glimpse-nexus-deck';
    deck.addEventListener('mousedown', (e) => e.stopPropagation());
    deck.addEventListener('click', (e) => e.stopPropagation());
    const rail = el('div', 'nx-rail');
    const body = el('div', 'nx-body');
    body.id = 'glimpse-md-dock-panel';
    body.style.display = 'none';
    const resize = el('div', 'nx-resize');
    const drawer = el('div', 'nx-drawer');
    drawer.setAttribute('data-open', '0');
    const dhead = el('div', 'nx-drawer-head');
    const dtitle = el('div', 'nx-drawer-title', '');
    const dactions = el('div', 'nx-actions');
    const dclose = iconBtn('x', 'Close this drawer', () => closeDrawer());
    dhead.appendChild(dtitle); dhead.appendChild(dactions); dhead.appendChild(dclose);
    const dbody = el('div', 'nx-drawer-body');
    drawer.appendChild(dhead); drawer.appendChild(dbody);
    const reader = buildReader();
    const toasts = el('div', 'nx-toasts');
    const sink = el('div', 'nx-sink');
    const divider = el('div', 'nx-divider'); divider.title = 'Drag to trade width between the panel and the Reader';
    body.appendChild(resize); body.appendChild(drawer); body.appendChild(divider); body.appendChild(reader); body.appendChild(toasts); body.appendChild(sink);
    deck.appendChild(body); deck.appendChild(rail);
    Object.assign(S.els, { deck, rail, body, resize, divider, drawer, drawerTitle: dtitle, drawerActions: dactions, drawerBody: dbody, toasts, sink });
    // The deck lives under <html>, outside <body>: a push narrows <body> and moves Workflowy's fixed
    // chrome off the deck's edge, and none of that may touch the deck. Every other float the
    // extension draws mounts on layer().
    document.documentElement.appendChild(deck);
    layer();
    S.mounted = true;
    // The client shows/hides the body by writing style.display; mirror that.
    try { new MutationObserver(() => syncOpenState()).observe(body, { attributes: true, attributeFilter: ['style'] }); } catch (e) { /* ignore */ }
    // Legacy status writers land in the hidden sink; every change becomes an
    // Activity entry. A multi-line write (one <div> per line, e.g. a resolved
    // UUID path) keeps its line breaks.
    const sinkText = () => (sink.children.length
      ? Array.from(sink.childNodes).map((n) => n.textContent).join('\n')
      : sink.textContent);
    try { new MutationObserver(() => logActivity(sinkText())).observe(sink, { childList: true, characterData: true, subtree: true }); } catch (e) { /* ignore */ }
    applyTheme(); watchTheme();
    buildRail();
    initResize(); initSplitter(); initDivider();
    applyLayout();
    window.addEventListener('resize', () => applyLayout());
    // A transient picker sheet closes on a click outside the deck, or on Escape (a popover's own
    // Escape wins while one is open; Escape inside the sheet's filter box only clears the filter).
    document.addEventListener('mousedown', (e) => {
      if (S.drawerId && isTransient(S.drawerId) && !S.readerHidden && !deck.contains(e.target) && !layer().contains(e.target)) closeDrawer();
    }, true);
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || S.pop || paletteEl || uuidPromptEl) return;
      if (!(S.drawerId && isTransient(S.drawerId) && !S.readerHidden)) return;
      const t = e.target;
      if (t && t.closest && t.closest('.nx-drawer') && /^(INPUT|TEXTAREA)$/.test(t.tagName)) return;
      e.preventDefault(); e.stopPropagation(); closeDrawer();
    }, true);
    // Workflowy writes <body>'s inline style when its own sidebar opens: re-run the push self-check.
    try { new MutationObserver(() => schedulePushCheck()).observe(document.body, { attributes: true, attributeFilter: ['style'] }); } catch (e) { /* ignore */ }
    // Keep a legacy style id around: tests and old code look for it.
    if (!document.getElementById('glimpse-md-dock-style')) {
      const style = el('style'); style.id = 'glimpse-md-dock-style';
      style.textContent = '#glimpse-md-dock-panel, #glimpse-md-dock-panel * { box-sizing: border-box; }';
      document.head.appendChild(style);
    }
    return S.els;
  }

  // ───────────────────────── public API ─────────────────────────
  const publicApi = {
    mount,
    isMounted: () => S.mounted,
    els: () => S.els,
    icon, el, btn, iconBtn, seg, copyText,
    openPop, openMenu, closePop,
    toast,
    logActivity,
    bindActivityHost(hostEl) { S.els.activity = hostEl; renderActivity(); },
    clearActivity() { S.activity = []; renderActivity(); },
    registerDrawer, openDrawer, closeDrawer, toggleDrawer, refreshDrawer,
    currentDrawer: () => S.drawerId,
    isOpen, toggleDeck,
    applyLayout, applyTheme, themeName: () => S.theme,
    updateReader,
    renderMenu,
    // "The Rail, side by side"
    fitDrawer, splitName, nameEl, afterPick, showReaderPane, layer,
    sideNow, isPushing: () => !!(S.pushActive && S.pushPx), pushFellBack: () => S.pushFallback,
    isTransient: () => isTransient(S.drawerId), isPinned: (id) => S.pinned.has(id),
    openPalette, closePalette, openUuidPathPrompt,
    showTips, closeTip,
    setConnected(on) { S.connected = !!on; if (S.els.dot) { S.els.dot.classList.toggle('is-on', S.connected); S.els.dot.title = dotTitle(S.connected); } },
    setJobsCount(n) { S.jobs = Number(n) || 0; if (S.els.jobsBadge) { S.els.jobsBadge.textContent = S.jobs ? String(S.jobs) : ''; S.els.jobsBadge.classList.toggle('is-on', S.jobs > 0); } },
    api: () => S.api,
    state: () => S.lastState,
    hotkeyLabel,
  };
  root.GlimpseUI = publicApi;
})(typeof globalThis !== 'undefined' ? globalThis : window);
