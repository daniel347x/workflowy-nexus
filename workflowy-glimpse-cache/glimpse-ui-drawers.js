/*
 * glimpse-ui-drawers.js — the drawers of "The Rail": Anchors, Slices, Lightning
 * Rod, Tabs, Jobs, Settings, Help. Each registers with GlimpseUI (glimpse-ui.js,
 * loaded just before this file) and talks to the client only through the `api`
 * object the client passes at mount (GlimpseUI.api()). Design note: [ap:VX7YBY].
 */
(function installGlimpseUiDrawers(root) {
  'use strict';
  const UI = root.GlimpseUI;
  if (!UI) return;
  const { el, btn, iconBtn, icon, seg, copyText } = UI;
  const api = () => UI.api();
  const LS = {
    get(key, fallback) { try { const v = localStorage.getItem(key); return v == null ? fallback : JSON.parse(v); } catch (e) { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* ignore */ } },
  };
  function toast(msg, ms, kind) { UI.toast(msg, ms, kind); }

  // ───────────────────────── the words (glimpse-ui-help.js, loaded just before this file) ─────────────────────────
  // R2's text verbatim ([ap:PA0EIU]), published as GlimpseUiHelp. Every read is guarded, so the
  // drawers still work, with their built-in fallbacks, if the module is missing.
  const HELP = (root.GlimpseUiHelp && typeof root.GlimpseUiHelp === 'object') ? root.GlimpseUiHelp : null;
  const WORDS = (HELP && HELP.strings) || {};
  function word(key, fallback) { return (typeof WORDS[key] === 'string') ? WORDS[key] : fallback; }
  // Tiny inline Markdown (**bold**, *italic*, `code`) for the Help sections and the Syntax help.
  function mdInline(target, text) {
    String(text == null ? '' : text).split(/(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)/).forEach((part) => {
      if (!part) return;
      if (part.startsWith('**') && part.endsWith('**') && part.length > 4) target.appendChild(el('b', '', part.slice(2, -2)));
      else if (part.startsWith('`') && part.endsWith('`') && part.length > 2) target.appendChild(el('code', 'nx-help-code', part.slice(1, -1)));
      else if (part.startsWith('*') && part.endsWith('*') && part.length > 2) target.appendChild(el('i', '', part.slice(1, -1)));
      else target.appendChild(document.createTextNode(part));
    });
    return target;
  }
  // The one sentence at the top of a drawer, ending in More…, which opens the Help drawer at that
  // drawer's section with a Back link (the reveal: the drawer itself never grows a second paragraph).
  function oneLiner(key, sectionId, backId) {
    const text = HELP && HELP.oneLiners && HELP.oneLiners[key];
    if (!text) return null;
    const n = el('div', 'nx-note nx-oneliner', text + ' ');
    const more = el('a', 'nx-link', 'More…'); more.href = '#';
    more.title = 'More about this, in the Help drawer';
    more.addEventListener('click', (ev) => { ev.preventDefault(); ev.stopPropagation(); UI.openHelpAt(sectionId, backId); });
    n.appendChild(more);
    return n;
  }
  // The ? in a drawer's head: the same reveal as More….
  function helpBtn(sectionId, backId) {
    return iconBtn('help', 'What this is and how it fits (Help)', () => UI.openHelpAt(sectionId, backId));
  }

  // ───────────────────────── status lines (shared by the client's status writers) ─────────────────────────
  const statusEls = new Map();
  const statusPending = new Map();
  UI.registerStatus = function registerStatus(area, element) {
    statusEls.set(area, element);
    const p = statusPending.get(area);
    if (p) { UI.setStatus(area, p.message, p.ok); statusPending.delete(area); }
  };
  UI.setStatus = function setStatus(area, message, ok) {
    const e = statusEls.get(area);
    if (!e) { statusPending.set(area, { message, ok }); if (ok === false && message) toast('⚠ ' + message, 6000, 'warn'); return; }
    e.textContent = message || '';
    e.className = 'nx-status' + (message ? (ok === false ? ' is-err' : ' is-ok') : '');
    if (ok === false && message && UI.currentDrawer() !== area) toast('⚠ ' + message, 6000, 'warn');
  };

  // ───────────────────────── a generic keyboard-navigable tree host ─────────────────────────
  function makeTreeHost(container, opts) {
    const host = el('div', 'nx-tree');
    host.tabIndex = 0;
    let focus = -1;
    const rows = () => Array.from(host.querySelectorAll('.nx-tree-row'));
    function setFocus(i) {
      const rs = rows();
      if (!rs.length) return;
      focus = Math.max(0, Math.min(rs.length - 1, i));
      rs.forEach((r, k) => r.classList.toggle('is-focus', k === focus));
      try { rs[focus].scrollIntoView({ block: 'nearest' }); } catch (e) { /* ignore */ }
    }
    host.addEventListener('keydown', (e) => {
      const rs = rows();
      if (!rs.length) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); setFocus(focus + 1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); setFocus(focus - 1); }
      else if (e.key === 'Enter') { e.preventDefault(); const r = rs[focus]; if (r && r._primary) r._primary(); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); const r = rs[focus]; if (r && r._expand) r._expand(true); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); const r = rs[focus]; if (r && r._expand) r._expand(false); }
      else if (e.key === 'Escape') { e.preventDefault(); UI.closeDrawer(); }
      e.stopPropagation();
    });
    host.addEventListener('keyup', (e) => e.stopPropagation());
    container.appendChild(host);
    return { host, setFocus, rows };
  }
  function makeRow(opts) {
    const row = el('div', 'nx-tree-row');
    row.style.paddingLeft = (4 + (opts.depth || 0) * 14) + 'px';
    const caret = el('button', 'nx-caret' + (opts.hasChildren ? (opts.open ? ' is-open' : '') : ' is-leaf'));
    caret.type = 'button'; caret.innerHTML = icon('chevron', 12);
    caret.title = opts.open ? 'Collapse' : 'Expand';
    if (opts.hasChildren && opts.onToggle) caret.addEventListener('click', (ev) => { ev.stopPropagation(); opts.onToggle(!opts.open); });
    row.appendChild(caret);
    const label = el('span', 'nx-tree-label' + (opts.labelClass ? ' ' + opts.labelClass : ''));
    // A picker row's name is fit-or-split (glimpse-ui.js nameEl): whole on one line when it fits,
    // else a head plus a small grey second line, never an ellipsis.
    if (opts.nameSplit && UI.nameEl) label.appendChild(UI.nameEl(opts.label)); else label.textContent = opts.label;
    label.title = opts.title || opts.label;
    if (opts.sub) { const s = el('span', 'nx-tree-sub', opts.sub); label.appendChild(s); }
    row.appendChild(label);
    if (opts.extra) row.appendChild(opts.extra);
    const actions = el('span', 'nx-row-actions');
    (opts.actions || []).forEach((a) => actions.appendChild(a));
    row.appendChild(actions);
    if (opts.onPrimary) { row.addEventListener('click', (ev) => { ev.stopPropagation(); opts.onPrimary(ev); }); row._primary = opts.onPrimary; }
    if (opts.hasChildren && opts.onToggle) row._expand = (open) => opts.onToggle(open);
    return row;
  }
  function matchText(hay, q) { return !q || String(hay || '').toLowerCase().includes(q); }

  // ═════════════════════════ ANCHORS ═════════════════════════
  const anchors = {
    res: null, loading: false, filter: '',
    opts: { favoritesOnly: true, showLocal: false },
    collapsed: new Set(LS.get('glimpse_anchor_collapsed', [])),
    tree: null, notice: null, status: null, toggles: {},
  };
  function anchorsLoad() {
    if (anchors.loading) return;
    anchors.loading = true;
    if (anchors.status) UI.setStatus('anchors', 'Loading Anchor Docks…', true);
    Promise.resolve(api().listAnchors({ favoritesOnly: anchors.opts.favoritesOnly, showLocalAnchors: anchors.opts.showLocal }))
      .then((res) => {
        anchors.loading = false;
        if (!res || res.success !== true) {
          UI.setStatus('anchors', 'Could not list Anchor Docks: ' + ((res && res.error) ? res.error : 'server unavailable or timed out'), false);
          return;
        }
        anchors.res = res;
        UI.setStatus('anchors', '', true);
        anchorsRender();
        if (anchors.onLoaded) { const f = anchors.onLoaded; anchors.onLoaded = null; try { f(); } catch (e) { /* ignore */ } }
      })
      .catch((e) => { anchors.loading = false; UI.setStatus('anchors', 'Could not list Anchor Docks: ' + (e && e.message ? e.message : String(e)), false); });
  }
  // The favorites line, composed here from the counts the daemon already sends (R2, S): under the
  // zero-favorites fallback R2's sentence; otherwise '⭐ N favorites · K local hidden'. A daemon
  // that sends neither still has its own favorites_notice painted verbatim (the pre-favorites shape).
  function anchorsFavoritesLine(res) {
    if (res && res.favorites_fallback === true && WORDS.favoritesNone) return WORDS.favoritesNone;
    if (res && res.favorites_fallback === false && typeof res.favorite_count === 'number') {
      const n = res.favorite_count, k = (typeof res.local_anchor_count === 'number') ? res.local_anchor_count : 0;
      if (n === 0 && WORDS.favoritesNone) return WORDS.favoritesNone;
      const bits = ['⭐ ' + n + ' favorite' + (n === 1 ? '' : 's')];
      if (k > 0) bits.push(k + ' local' + (anchors.opts.showLocal ? '' : ' hidden'));
      return bits.join(' · ');
    }
    return (res && typeof res.favorites_notice === 'string') ? res.favorites_notice.trim() : '';
  }
  function anchorsRender() {
    const t = anchors.tree;
    if (!t) return;
    t.host.innerHTML = '';
    const res = anchors.res;
    const q = anchors.filter.trim().toLowerCase();
    const notice = anchorsFavoritesLine(res);
    anchors.notice.textContent = notice;
    anchors.notice.style.display = notice ? '' : 'none';
    Object.keys(anchors.toggles).forEach((k) => anchors.toggles[k].classList.toggle('is-on', k === 'descend' ? !!api().readAnchorOpts().descendThroughDocks : !!anchors.opts[k]));
    const list = res ? ((res.anchor_tree && Array.isArray(res.anchor_tree.children)) ? res.anchor_tree.children : (Array.isArray(res.sessions) ? res.sessions : [])) : [];
    const matches = (node) => {
      if (matchText(node.name, q) || matchText(node.id || node.uuid || node.node_id, q)) return true;
      return Array.isArray(node.children) && node.children.some(matches);
    };
    let count = 0;
    const walk = (nodes, depth) => {
      nodes.forEach((node) => {
        if (q && !matches(node)) return;
        const id = String(node.id || node.uuid || node.node_id || '');
        const kids = Array.isArray(node.children) ? node.children : [];
        const hasKids = kids.length > 0;
        const open = q ? true : !anchors.collapsed.has(id);
        const name = String(node.name || '(unnamed)');
        // The Dock's description (its note, as the daemon sends it) is the row's tooltip; the
        // 8-hex id is no longer a second line (the copy buttons and the tooltip still carry it).
        const desc = (typeof node.description === 'string') ? node.description.trim().split('\n')[0] : '';
        const star = iconBtn('star', node.is_favorite ? 'Remove from favorites' : 'Add to favorites', () => anchorsToggleFavorite(node, star), 'nx-star' + (node.is_favorite ? ' is-on' : ''));
        const copyBtn = iconBtn('copy', 'Copy `name` and `uuid`', () => copyText('`' + name + '`\n`' + id + '`', (ok) => toast(ok ? 'Copied name + uuid.' : 'Copy failed.', 1800)));
        const idBtn = iconBtn('hash', 'Copy the Workflowy search string id:' + id, () => copyText('id:' + id, (ok) => toast(ok ? 'Copied id:' + id : 'Copy failed.', 1800)));
        const row = makeRow({
          depth, hasChildren: hasKids, open,
          label: name, nameSplit: true, labelClass: node.is_local ? 'is-local' : '',
          title: name + (node.is_local ? '  ·  local Dock' : '') + (desc ? '\n' + desc : '') + '\n' + id,
          onToggle: (o) => { if (o) anchors.collapsed.delete(id); else anchors.collapsed.add(id); LS.set('glimpse_anchor_collapsed', Array.from(anchors.collapsed)); anchorsRender(); },
          onPrimary: () => anchorsOpen(id, name),
          actions: [star, copyBtn, idBtn],
        });
        row.querySelector('.nx-tree-label').style.flex = '1 1 auto';
        t.host.appendChild(row);
        count++;
        if (hasKids && open) walk(kids, depth + 1);
      });
    };
    walk(list, 0);
    if (!count) t.host.appendChild(el('div', 'nx-empty', res ? (q ? 'No Dock matches the filter.' : 'No Anchor Docks returned.') : 'Loading…'));
    try { UI.fitDrawer(); } catch (e) { /* ignore */ }
  }
  function anchorsOpen(uuid, name) {
    if (!uuid) return;
    toast('Opening ' + (name || uuid.slice(0, 8)) + '…', 2500);
    UI.afterPick();
    try { api().renderAnchor(uuid); } catch (e) { toast('⚠ Render failed: ' + (e && e.message ? e.message : String(e)), 6000, 'warn'); }
  }
  // The Dock list as a flat array for the palette's type-to-go ({id, name, path}); null until loaded.
  function anchorsFlat() {
    const res = anchors.res;
    if (!res) return null;
    const list = (res.anchor_tree && Array.isArray(res.anchor_tree.children)) ? res.anchor_tree.children : (Array.isArray(res.sessions) ? res.sessions : []);
    const out = [];
    const walk = (nodes, path) => nodes.forEach((n) => {
      const id = String(n.id || n.uuid || n.node_id || ''); const name = String(n.name || '(unnamed)');
      out.push({ id, name, path: path.join(' › ') });
      if (Array.isArray(n.children)) walk(n.children, path.concat([name]));
    });
    walk(list, []);
    return out;
  }
  function anchorsEnsure(cb) {
    if (anchors.res) { if (cb) cb(); return; }
    anchors.onLoaded = cb || null;
    anchorsLoad();
  }
  function anchorsToggleFavorite(node, starBtn, families) {
    const uid = String(node.id || node.uuid || node.node_id || '');
    if (!uid) return;
    const desired = !node.is_favorite;
    starBtn.disabled = true;
    Promise.resolve(api().toggleFavorite(uid, desired, families)).then((res) => {
      starBtn.disabled = false;
      if (res == null) { toast('⚠ Favorite toggle unavailable — retry.', 6000, 'warn'); return; }
      if (res.success === false) {
        if (res.error_code === 'dock_families_ambiguous') {
          const fams = (res.extra && Array.isArray(res.extra.families_present)) ? res.extra.families_present.slice() : [];
          UI.openMenu(starBtn, fams.map((f) => ({ label: String(f), run: () => anchorsToggleFavorite(node, starBtn, f) })), { title: 'This Dock carries tags from more than one family. Pick the family to update:' });
          return;
        }
        toast('⚠ ' + (res.error || 'Favorite toggle failed.'), 6000, 'warn');
        return;
      }
      toast(desired ? '⭐ Added to favorites' : '☆ Removed from favorites', 3000);
      anchorsLoad();
    });
  }
  UI.registerDrawer('anchors', {
    title: 'Anchor Docks', icon: 'anchor', order: 10, picker: true, hint: 'where things go',
    build(container) {
      const intro = oneLiner('anchors', 'nx-help-docks', 'anchors'); if (intro) container.appendChild(intro);
      const tools = el('div', 'nx-drawer-tools');
      const filter = el('input', 'nx-input is-grow'); filter.type = 'text'; filter.placeholder = 'Filter Docks…';
      filter.addEventListener('input', () => { anchors.filter = filter.value; anchorsRender(); });
      filter.addEventListener('keydown', (e) => { if (e.key === 'Escape') { filter.value = ''; anchors.filter = ''; anchorsRender(); } e.stopPropagation(); });
      filter.addEventListener('keyup', (e) => e.stopPropagation());
      tools.appendChild(filter);
      const fav = btn('Favorites', 'Show only ⭐ favorite Docks (the default shortlist)', () => { anchors.opts.favoritesOnly = !anchors.opts.favoritesOnly; anchorsLoad(); anchorsRender(); }, '', 'star');
      const loc = btn('Local', 'Include local (hidden) Docks', () => { anchors.opts.showLocal = !anchors.opts.showLocal; anchorsLoad(); anchorsRender(); }, '', 'eye');
      const desc = btn('Descend', 'When opening a Dock, descend into its child Docks (off = stop at child Docks)', () => { const o = api().readAnchorOpts(); api().saveAnchorOpts({ descendThroughDocks: !o.descendThroughDocks }); anchorsRender(); }, '', 'layers');
      anchors.toggles = { favoritesOnly: fav, showLocal: loc, descend: desc };
      const row2 = el('div', 'nx-drawer-tools'); row2.appendChild(fav); row2.appendChild(loc); row2.appendChild(desc);
      container.appendChild(tools); container.appendChild(row2);
      anchors.notice = el('div', 'nx-note'); anchors.notice.style.display = 'none'; container.appendChild(anchors.notice);
      anchors.status = el('div', 'nx-status'); container.appendChild(anchors.status); UI.registerStatus('anchors', anchors.status);
      anchors.tree = makeTreeHost(container, {});
      anchorsRender();
    },
    headActions(host) { host.appendChild(helpBtn('nx-help-docks', 'anchors')); host.appendChild(iconBtn('refresh', 'Reload the Dock list', () => anchorsLoad())); },
    onOpen() { anchorsLoad(); },
    commands() { return [{ label: 'Reload the Anchor Dock list', run: () => { UI.openDrawer('anchors'); anchorsLoad(); } }]; },
  });
  UI.anchors = { reload: anchorsLoad, render: anchorsRender, flat: anchorsFlat, ensure: anchorsEnsure, open: anchorsOpen };

  // ═════════════════════════ SLICES ═════════════════════════
  const slices = {
    collapsed: new Set(LS.get('glimpse_slice_collapsed', [])), filter: '', editingId: null, tree: null, status: null,
    rootTitles: {},            // uuid -> {title, exists, is_dock} from the daemon's resolve_node_titles
    titlesPending: new Set(),
  };
  function groupPathSegments(g) { try { return api().parseName(g.name).segments || []; } catch (e) { return [String(g.name || '')]; } }
  function sliceSortLabel(g) {
    const sb = g.default_sort_by || 'none', sd = g.default_sort_direction || 'asc';
    if (sb === 'none') return 'doc order';
    return (sd === 'desc' ? '▼ ' : '▲ ') + (sb === 'created' ? 'created' : 'modified');
  }
  // Ask the daemon for the root titles the rows still lack. An older daemon never answers (the
  // request times out to null): the rows keep the 8-hex handle alone, nothing waits on it.
  function slicesRequestTitles(uuids) {
    const need = Array.from(new Set(uuids)).filter((u) => !(u in slices.rootTitles) && !slices.titlesPending.has(u));
    if (!need.length || typeof api().resolveNodeTitles !== 'function') return;
    need.forEach((u) => slices.titlesPending.add(u));
    Promise.resolve(api().resolveNodeTitles(need)).then((res) => {
      need.forEach((u) => slices.titlesPending.delete(u));
      if (!res || res.success !== true || !res.titles || typeof res.titles !== 'object') return;
      Object.keys(res.titles).forEach((u) => { slices.rootTitles[u] = res.titles[u]; });
      slicesRender();
    }).catch(() => { need.forEach((u) => slices.titlesPending.delete(u)); });
  }
  // Middle elision to a pixel width (the beginning and the end both stay visible).
  function elideMiddleToWidth(s, maxPx, measure) {
    const cps = Array.from(String(s || ''));
    if (!cps.length || measure(cps.join('')) <= maxPx) return cps.join('');
    let lo = 1, hi = cps.length - 1, best = '…';
    while (lo <= hi) {
      const k = (lo + hi) >> 1;
      const head = Math.ceil(k * 0.6), tail = k - head;
      const cand = cps.slice(0, head).join('').trimEnd() + ' … ' + cps.slice(cps.length - tail).join('').trimStart();
      if (measure(cand) <= maxPx) { best = cand; lo = k + 1; } else hi = k - 1;
    }
    return best;
  }
  // After the rows are laid out: decide per row whether the root part fits beside the tags and
  // sort or takes its own line, then elide the root title in the middle to the width it has.
  let metaCtx = null;
  function slicesFitMeta() {
    if (!slices.tree) return;
    if (!metaCtx) metaCtx = document.createElement('canvas').getContext('2d');
    slices.tree.host.querySelectorAll('.nx-slice-meta').forEach((m) => {
      const a = m.querySelector('.nx-slice-meta-a'), b = m.querySelector('.nx-slice-meta-b');
      const nm = m.querySelector('.nx-tree-root-name');
      if (!a || !b) return;
      const total = m.clientWidth || 0;
      if (!total) return;
      const idEl = m.querySelector('.nx-tree-root-id');
      const fixedB = (idEl ? idEl.offsetWidth : 0) + 26;   // the dot, the gaps
      const full = nm ? (nm.getAttribute('data-full') || '') : '';
      let fullW = 0;
      if (nm) { const cs = getComputedStyle(nm); metaCtx.font = cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily; fullW = metaCtx.measureText(full).width; }
      const besideW = total - a.offsetWidth - 8;
      const wrapped = nm ? (fullW + fixedB > besideW) : (b.offsetWidth > besideW);
      m.classList.toggle('is-wrapped', wrapped);
      if (nm) nm.textContent = elideMiddleToWidth(full, Math.max(60, (wrapped ? total : besideW) - fixedB), (s) => metaCtx.measureText(s).width);
    });
  }
  function slicesRender() {
    const t = slices.tree;
    if (!t) return;
    t.host.innerHTML = '';
    const groups = api().groups();
    const q = slices.filter.trim().toLowerCase();
    let items = [];
    try { items = api().buildSliceTree(groups); } catch (e) { items = groups.map((g) => ({ kind: 'leaf', group: g, name: g.name, depth: 0 })); }
    let skipBelow = null;
    let count = 0;
    const wantTitles = [];
    items.forEach((item) => {
      if (skipBelow != null) { if (item.depth > skipBelow) return; skipBelow = null; }
      if (item.kind === 'expander') {
        const key = item.pathStr || item.name;
        const collapsed = !q && slices.collapsed.has(key);
        if (q) {
          // While filtering, show a category only when some group beneath it matches.
          const anyMatch = groups.some((g) => (groupPathSegments(g).join('|') + ' ' + (g.prefixes || []).join(' ')).toLowerCase().includes(q) && groupPathSegments(g).join('|').startsWith(key));
          if (!anyMatch) return;
        }
        const actions = [];
        if (item.group) {
          actions.push(iconBtn('pencil', 'Edit this category’s path and order', () => slicesEdit(item.group.id)));
          actions.push(iconBtn('trash', 'Delete this category row (its groups stay)', () => slicesDelete(item.group)));
        }
        const row = makeRow({
          depth: item.depth, hasChildren: !!item.childNode || item.duplicate, open: !collapsed,
          label: item.name + (item.duplicate ? '  (duplicate header — empty)' : ''), labelClass: 'is-category',
          title: item.group ? item.group.name : (item.name + ' (auto-created category)'),
          onToggle: (o) => { if (o) slices.collapsed.delete(key); else slices.collapsed.add(key); LS.set('glimpse_slice_collapsed', Array.from(slices.collapsed)); slicesRender(); },
          onPrimary: () => { if (collapsed) slices.collapsed.delete(key); else slices.collapsed.add(key); LS.set('glimpse_slice_collapsed', Array.from(slices.collapsed)); slicesRender(); },
          actions,
        });
        t.host.appendChild(row); count++;
        if (collapsed) skipBelow = item.depth;
        return;
      }
      const g = item.group;
      if (q && !(groupPathSegments(g).join('|') + ' ' + (g.prefixes || []).join(' ') + ' ' + (g.description || '')).toLowerCase().includes(q)) return;
      const tags = el('span', 'nx-tree-tags');
      const prefixes = g.prefixes || [];
      prefixes.forEach((p) => { const c = el('span', 'nx-tag', '#' + p); c.title = 'Members carry a tag starting with ' + p + ' (postfixes --expand / --suppress / --deprecated shape the view)'; tags.appendChild(c); });
      if (!prefixes.length) tags.appendChild(el('span', 'nx-tree-meta-dim', 'no tags yet'));
      const play = iconBtn('play', 'Apply this slice group into the Reader', (ev) => slicesApplyPop(g, ev.currentTarget.closest('.nx-tree-row')));
      const edit = iconBtn('pencil', 'Edit path, description, tags, root, default sort', () => slicesEdit(g.id));
      const row = makeRow({
        depth: item.depth, hasChildren: false, label: item.name || g.name, nameSplit: true, title: (g.name || '') + (g.description ? '\n' + g.description : ''),
        extra: tags, actions: [play, edit],
        onPrimary: (ev) => slicesApplyPop(g, ev && ev.currentTarget ? ev.currentTarget : null),
      });
      row.setAttribute('data-group-id', g.id);
      // The definition line (R2, Dan's item 4): beneath the name, smaller and grey, what defines
      // the group: its tags, its default sort, and its root as the 8-hex handle plus the root's
      // title (trailing tags stripped by the daemon's resolve_node_titles; the handle alone until
      // it answers), elided in the middle so the beginning and the end both show.
      row.classList.add('has-meta');
      const meta = el('div', 'nx-slice-meta');
      const partA = el('span', 'nx-slice-meta-a');
      partA.appendChild(tags);   // the chips move from the row's extra slot into the definition line
      partA.appendChild(el('span', 'nx-meta-dot', '·'));
      const sortEl = el('span', 'nx-tree-sort', sliceSortLabel(g));
      sortEl.title = 'Default sort when applied (the View popover can override it for one view)';
      partA.appendChild(sortEl);
      const partB = el('span', 'nx-slice-meta-b');
      partB.appendChild(el('span', 'nx-meta-dot', '·'));
      const rootU = String(g.root_uuid || '').trim();
      if (rootU) {
        const known = slices.rootTitles[rootU];
        const rootName = el('span', 'nx-tree-root-name', '');
        rootName.setAttribute('data-full', known ? (known.exists === false ? '(not in the nodes cache)' : String(known.title || '')) : '');
        partB.appendChild(el('span', 'nx-tree-root-id', rootU.slice(0, 8)));
        partB.appendChild(rootName);
        partB.title = 'Root: the group is taken beneath this node' + (known && known.title ? '\n' + known.title : '') + '\n' + rootU + '\nClick to read the root in the Reader';
        partB.classList.add('is-root');
        partB.addEventListener('click', (ev) => { ev.stopPropagation(); UI.afterPick(); try { api().renderAnchor(rootU); } catch (e) { /* ignore */ } });
        wantTitles.push(rootU);
      } else {
        partB.appendChild(el('span', 'nx-tree-meta-dim', 'whole outline'));
        partB.title = 'No root set: the group is taken over your whole Workflowy outline';
      }
      meta.appendChild(partA); meta.appendChild(partB);
      row.querySelector('.nx-tree-label').appendChild(meta);
      if (slices.editingId === g.id) row.classList.add('is-open');
      t.host.appendChild(row); count++;
      if (slices.editingId === g.id) t.host.appendChild(slicesEditPanel(g));
    });
    if (!count) t.host.appendChild(el('div', 'nx-empty', q ? 'No slice group matches the filter.' : 'No slice groups yet. Use + New.'));
    try { UI.fitDrawer(); } catch (e) { /* ignore */ }
    slicesFitMeta(); setTimeout(slicesFitMeta, 0); setTimeout(slicesFitMeta, 200);
    slicesRequestTitles(wantTitles);
  }
  // Apply a group with the remembered Apply options (the palette's type-to-go path).
  function slicesApplyDefault(gid) {
    const g = api().groups().find((x) => x.id === gid);
    if (!g) return;
    const opts = api().readApplyOpts();
    UI.afterPick();
    toast('Applying “' + api().displayName(g.name) + '”…', 2500);
    api().applyGroup(gid, { matchScope: opts.matchTitleBody ? 'name_and_note' : 'name', descendPolicy: opts.descendThroughDocks ? 'all' : 'stop_at_child_anchors' });
  }
  function slicesEdit(gid) { slices.editingId = (slices.editingId === gid) ? null : gid; slicesRender(); }
  function slicesEditPanel(g) {
    const p = el('div', 'nx-edit');
    if (WORDS.editorHead) p.appendChild(el('div', 'nx-edit-head', WORDS.editorHead));   // what defines a group (R2)
    const mk = (labelText, inputEl) => { p.appendChild(el('label', '', labelText)); p.appendChild(inputEl); return inputEl; };
    const name = mk('Path', el('input', 'nx-input')); name.type = 'text'; name.value = g.name || '';
    name.title = 'Pipe | nests categories; a trailing [n] on the last segment sets the order; a leading [n] makes this row a category header.';
    const desc = mk('Description', el('textarea', 'nx-textarea')); desc.value = g.description || '';
    const tags = mk('Tags', el('input', 'nx-input')); tags.type = 'text'; tags.value = (g.prefixes || []).join(', ');
    tags.placeholder = 'comma-separated tag prefixes (# optional)';
    const rootU = mk('Root', el('input', 'nx-input')); rootU.type = 'text'; rootU.value = g.root_uuid || ''; rootU.placeholder = 'root UUID (optional)';
    const sortWrap = el('span'); sortWrap.style.display = 'inline-flex'; sortWrap.style.gap = '6px';
    const sortSel = el('select', 'nx-select');
    [['none', 'Document order'], ['created', 'Created'], ['modified', 'Modified']].forEach(([v, l]) => { const o = el('option', '', l); o.value = v; sortSel.appendChild(o); });
    sortSel.value = g.default_sort_by || 'none';
    const dirSel = el('select', 'nx-select');
    [['asc', 'Ascending'], ['desc', 'Descending']].forEach(([v, l]) => { const o = el('option', '', l); o.value = v; dirSel.appendChild(o); });
    dirSel.value = g.default_sort_direction || 'asc';
    sortWrap.appendChild(sortSel); sortWrap.appendChild(dirSel);
    mk('Default sort', sortWrap);
    const actions = el('div', 'nx-edit-actions');
    actions.appendChild(btn('Copy ID', 'Copy this slice group’s id', () => copyText(g.id, (ok) => toast(ok ? 'Copied ' + g.id : 'Copy failed.', 1800)), 'is-ghost', 'hash'));
    actions.appendChild(btn('Delete', 'Delete this slice group', () => slicesDelete(g), 'is-ghost is-danger', 'trash'));
    actions.appendChild(btn('Cancel', '', () => { slices.editingId = null; slicesRender(); }, 'is-ghost'));
    actions.appendChild(btn('Save', 'Save (Ctrl+Enter)', () => save(), 'is-primary', 'check'));
    p.appendChild(actions);
    const save = () => {
      const groups = api().groups();
      const idx = groups.findIndex((x) => x.id === g.id);
      if (idx < 0) return;
      const prefixes = tags.value.split(',').map((s) => s.trim().replace(/^#/, '')).filter(Boolean);
      groups[idx] = Object.assign({}, groups[idx], {
        name: name.value.trim() || groups[idx].name, description: desc.value.trim(), prefixes,
        root_uuid: rootU.value.trim(), default_sort_by: sortSel.value, default_sort_direction: dirSel.value,
      });
      api().persistGroups(groups, api().activeGroupId());
      slices.editingId = null;
      slicesRender();
      toast(word('savedToast', 'Saved. Slice groups live on this machine only (storage.local).'), 3000);
    };
    p.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); save(); } else if (e.key === 'Escape') { e.preventDefault(); slices.editingId = null; slicesRender(); } e.stopPropagation(); });
    p.addEventListener('keyup', (e) => e.stopPropagation());
    p.addEventListener('click', (e) => e.stopPropagation());
    setTimeout(() => { try { name.focus(); } catch (e) { /* ignore */ } }, 0);
    return p;
  }
  function slicesDelete(g) {
    const groups = api().groups().filter((x) => x.id !== g.id);
    api().persistGroups(groups, api().activeGroupId());
    slices.editingId = null;
    slicesRender();
  }
  function slicesNew() {
    const groups = api().groups();
    const id = api().genGroupId();
    groups.push({ id, name: 'new slice', prefixes: [] });
    api().persistGroups(groups, api().activeGroupId());
    slices.editingId = id;
    slicesRender();
  }
  function slicesApplyPop(g, anchorEl) {
    const gid = g && g.id;
    if (!gid) return;
    const opts = api().readApplyOpts();
    const leaf = api().displayName(g.name);
    let shortcut = null;
    const close = () => { UI.closePop(); };
    const doApply = (makeDefault) => {
      close();
      if (makeDefault) { try { api().setThisTabAsDefault(); } catch (e) { /* ignore */ } }
      UI.afterPick();
      toast('Applying “' + leaf + '”…', 2500);
      api().applyGroup(gid, {
        matchScope: opts.matchTitleBody ? 'name_and_note' : 'name',
        descendPolicy: opts.descendThroughDocks ? 'all' : 'stop_at_child_anchors',
        hooks: { onGlow: (g2) => { const r = slices.tree && slices.tree.host.querySelector('[data-group-id="' + g2 + '"]'); if (r) { r.classList.remove('nx-glow'); void r.offsetWidth; r.classList.add('nx-glow'); } } },
      });
    };
    const doCopy = () => {
      close(); UI.afterPick();
      api().copySearch(gid, (txt) => { if (txt && txt !== '✓') toast('Copy search terms: ' + txt, 3000); });
    };
    const executeKey = (key) => {
      if (key === 'Escape') { close(); return true; }
      if (key === '1') { doApply(false); return true; }
      if (key === '2') { doApply(true); return true; }
      return false;
    };
    UI.openPop(anchorEl || (slices.tree && slices.tree.host), (pop) => {
      pop.appendChild(el('div', 'nx-pop-title', 'Apply · ' + leaf));
      if (WORDS.applyHint) pop.appendChild(el('div', 'nx-pop-hint is-apply', WORDS.applyHint));   // what Apply does (R2)
      const row = el('div', 'nx-pop-row');
      shortcut = el('input', 'nx-input nx-apply-shortcut'); shortcut.type = 'text'; shortcut.id = 'glimpse-sliceapply-shortcut';
      shortcut.setAttribute('maxlength', '8'); shortcut.setAttribute('autocomplete', 'off'); shortcut.placeholder = '1·2';
      shortcut.setAttribute('aria-label', 'Type 1 to Apply, 2 to Apply and make this tab default, Escape to cancel');
      shortcut.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape' || ev.key === '1' || ev.key === '2') { ev.preventDefault(); ev.stopPropagation(); executeKey(ev.key); return; }
        if (ev.key && ev.key.length === 1 && !ev.ctrlKey && !ev.altKey && !ev.metaKey) ev.preventDefault();
        ev.stopPropagation();
      });
      shortcut.addEventListener('keyup', (ev) => ev.stopPropagation());
      shortcut.addEventListener('input', () => { const v = shortcut.value || ''; const hit = v.includes('1') ? '1' : (v.includes('2') ? '2' : ''); shortcut.value = ''; if (hit) executeKey(hit); });
      row.appendChild(shortcut);
      row.appendChild(el('span', 'nx-pop-hint is-inline', 'press 1 or 2 · Esc cancels'));
      pop.appendChild(row);
      const mkCheck = (labelText, title, checked, onChange) => {
        const l = el('label', 'nx-check'); l.title = title;
        const c = el('input'); c.type = 'checkbox'; c.checked = !!checked;
        c.addEventListener('change', () => { onChange(c.checked); try { shortcut.focus(); } catch (e) { /* ignore */ } });
        l.appendChild(c); l.appendChild(el('span', '', labelText));
        return l;
      };
      pop.appendChild(mkCheck('Title + body search', 'Match slice tags in titles AND note bodies (Workflowy-search parity). Off = trailing tag region of the title only.', opts.matchTitleBody, (v) => { opts.matchTitleBody = v; api().saveApplyOpts({ matchTitleBody: v }); }));
      pop.appendChild(mkCheck('Descend through child Anchor Docks', 'Off = stop at child Docks; nested Docks render as leaf bullets.', opts.descendThroughDocks, (v) => { opts.descendThroughDocks = v; api().saveApplyOpts({ descendThroughDocks: v }); }));
      const acts = el('div', 'nx-apply-actions');
      [['1', 'Apply', () => doApply(false)], ['2', 'Apply + make this tab default', () => doApply(true)]].forEach(([k, label, run]) => {
        const b = el('button', 'nx-menu-item'); b.type = 'button';
        const kbd = el('kbd', 'nx-kbd', k); kbd.style.marginLeft = '0'; b.appendChild(kbd); b.appendChild(el('span', '', label));
        b.addEventListener('click', (ev) => { ev.stopPropagation(); run(); });
        acts.appendChild(b);
      });
      pop.appendChild(acts);
      pop.appendChild(el('div', 'nx-menu-sep'));
      const copyB = el('button', 'nx-menu-item'); copyB.type = 'button'; copyB.innerHTML = icon('copy', 15);
      const cl = el('span', '', 'Copy search terms'); cl.appendChild(el('span', 'nx-menu-detail', 'Boolean search for the Workflowy search box'));
      copyB.appendChild(cl); copyB.addEventListener('click', (ev) => { ev.stopPropagation(); doCopy(); });
      pop.appendChild(copyB);
    }, { width: 320, align: 'left', focus: '#glimpse-sliceapply-shortcut', onClose: () => { try { api().setSliceApplyDialogState(null); } catch (e) { /* ignore */ } } });
    try { api().setSliceApplyDialogState({ executeKey: (key) => executeKey(key) }); } catch (e) { /* ignore */ }
  }
  function slicesMenu(anchor) {
    const a = api();
    UI.openMenu(anchor, [
      { label: 'New slice group', icon: 'plus', run: () => slicesNew() },
      { sep: true },
      { label: 'Export to clipboard', detail: 'Every group as JSON (a backup: groups live on this machine only)', icon: 'upload', run: () => copyText(a.exportJson(), (ok) => toast(ok ? 'Exported to the clipboard.' : 'Copy failed.', 2500)) },
      { label: 'Export to a .json file', icon: 'download', run: () => { try { a.downloadText(a.exportJson(), a.exportFilename()); toast('Downloaded.', 2000); } catch (e) { toast('⚠ Export failed.', 4000, 'warn'); } } },
      { label: 'Import from the clipboard', detail: 'Replaces the groups on this machine (asks first)', icon: 'download', run: () => {
        if (!(navigator.clipboard && navigator.clipboard.readText)) { toast('⚠ Clipboard read unavailable; import from a file instead.', 5000, 'warn'); return; }
        navigator.clipboard.readText().then((text) => slicesImport(text, 'clipboard'), () => toast('⚠ Could not read the clipboard.', 5000, 'warn'));
      } },
      { label: 'Import from a .json file', icon: 'download', run: () => {
        const inp = el('input'); inp.type = 'file'; inp.accept = '.json,application/json'; inp.style.display = 'none';
        inp.addEventListener('change', () => { const f = inp.files && inp.files[0]; if (!f) { inp.remove(); return; } const r = new FileReader(); r.onload = () => { slicesImport(String(r.result || ''), 'file “' + f.name + '”'); inp.remove(); }; r.onerror = () => { toast('⚠ Could not read the file.', 5000, 'warn'); inp.remove(); }; r.readAsText(f); });
        document.body.appendChild(inp); inp.click();
      } },
      { sep: true },
      { label: 'Syntax and your current layout', icon: 'help', run: () => slicesHelp(anchor) },
    ], { title: 'Slice groups' });
  }
  function slicesImport(text, source) {
    const res = api().importSliceGroups(text, source);
    if (!res || !res.ok) { toast('⚠ Import failed: ' + ((res && res.error) || 'invalid JSON'), 6000, 'warn'); return; }
    if (res.cancelled) return;
    toast('Imported ' + res.count + ' slice group' + (res.count === 1 ? '' : 's') + '.', 3000);
    slicesRender();
  }
  // The Syntax help: R2's four paragraphs (bold leads, inline code) and its note that a plain
  // picker is a planned enhancement; the live explanation of the current arrangement stays.
  function slicesHelp(anchor) {
    const sh = (HELP && HELP.syntaxHelp) || null;
    UI.openPop(anchor, (pop) => {
      pop.appendChild(el('div', 'nx-pop-title', sh ? sh.title : 'Slice-group paths'));
      const ul = el('div', 'nx-note nx-help-text');
      if (sh) {
        (sh.paras || []).forEach((t) => ul.appendChild(mdInline(el('p', 'nx-help-p'), t)));
        if (sh.note) ul.appendChild(el('p', 'nx-help-p nx-help-note', sh.note));
      } else {
        [
          'A group’s name is a path: pipes nest it under categories (nexus|glimpse|extension).',
          'A trailing [n] on the last segment orders it among its siblings; no [n] sorts last, alphabetically.',
          'A leading [n] makes the row itself a category header; categories referenced only by children are created automatically.',
          'Two headers for the same category: the lowest [n] wins; the other renders empty (delete or renumber it).',
        ].forEach((t) => { const d = el('div', '', '• ' + t); d.style.margin = '2px 0'; ul.appendChild(d); });
      }
      pop.appendChild(ul);
      pop.appendChild(el('div', 'nx-drawer-section', 'Your current arrangement'));
      const host = el('div', 'nx-note'); host.style.maxHeight = '40vh'; host.style.overflow = 'auto';
      try { api().explainer(host); } catch (e) { host.textContent = '(could not build the explanation)'; }
      pop.appendChild(host);
    }, { width: 460, align: 'left' });
  }
  UI.registerDrawer('slices', {
    title: 'Slice groups', icon: 'layers', order: 20, picker: true, hint: 'what you look at',
    build(container) {
      const intro = oneLiner('slices', 'nx-help-slices', 'slices'); if (intro) container.appendChild(intro);
      const tools = el('div', 'nx-drawer-tools');
      const filter = el('input', 'nx-input is-grow'); filter.type = 'text'; filter.placeholder = word('filterSlices', 'Filter slice groups…');
      filter.addEventListener('input', () => { slices.filter = filter.value; slicesRender(); });
      filter.addEventListener('keydown', (e) => { if (e.key === 'Escape') { filter.value = ''; slices.filter = ''; slicesRender(); } e.stopPropagation(); });
      filter.addEventListener('keyup', (e) => e.stopPropagation());
      tools.appendChild(filter);
      tools.appendChild(iconBtn('plus', 'New slice group', () => slicesNew()));
      container.appendChild(tools);
      slices.status = el('div', 'nx-status'); container.appendChild(slices.status); UI.registerStatus('slices', slices.status);
      slices.tree = makeTreeHost(container, {});
      // The refit changes row heights, so it runs on the next frame (a refit inside the observer's
      // own callback trips "ResizeObserver loop completed with undelivered notifications").
      try { new ResizeObserver(() => { requestAnimationFrame(slicesFitMeta); }).observe(slices.tree.host); } catch (e) { /* ignore */ }
      slicesRender();
    },
    headActions(host) { host.appendChild(helpBtn('nx-help-slices', 'slices')); host.appendChild(iconBtn('more', 'New, export, import, syntax help', (ev) => slicesMenu(ev.currentTarget))); },
    refresh() { slicesRender(); },
    commands() { return [{ label: 'New slice group', run: () => { UI.openDrawer('slices'); slicesNew(); } }, { label: 'Export slice groups to the clipboard', run: () => copyText(api().exportJson(), (ok) => toast(ok ? 'Exported.' : 'Copy failed.', 2500)) }]; },
  });
  UI.slices = { refresh: slicesRender, applyDefault: slicesApplyDefault };

  // ═════════════════════════ LIGHTNING ROD ═════════════════════════
  const rod = { mode: 'search', scope: 'name', scoped: false, els: {} };
  function rodFire() {
    const mode = rod.mode === 'tags' ? 'render_tags' : 'render_search';
    api().lightning(mode, rod.els.query.value, rod.scope, rod.scoped);
    UI.afterPick();   // a fired search is the Rod's pick: a transient sheet closes, a pinned one stays
  }
  function rodPaint() {
    const st = UI.state() || {};
    const frozen = !!st.frozen;
    if (!frozen && rod.scoped) rod.scoped = false;
    rod.els.scopedSeg.querySelectorAll('button').forEach((b) => { b.disabled = (b.dataset.v === 'scoped') && !frozen; b.classList.toggle('is-on', (b.dataset.v === 'scoped') === rod.scoped); });
    rod.els.query.placeholder = rod.mode === 'tags' ? 'tag stems (e.g. comm-jim monitor)…' : 'search (e.g. invoice OR receipt -draft)…';
    rod.els.modeSeg.querySelectorAll('button').forEach((b) => b.classList.toggle('is-on', b.dataset.v === rod.mode));
    rod.els.scopeSeg.querySelectorAll('button').forEach((b) => b.classList.toggle('is-on', b.dataset.v === rod.scope));
    rod.els.hint.textContent = frozen ? 'Scoped searches only the tree frozen in the Reader; Global walks the cache under the Reader’s root.' : 'Global walks the cache under the Reader’s root. Freeze the Reader (or apply a slice) to enable Scoped.';
  }
  function segOf(pairs, onPick) {
    const s = el('span', 'nx-seg');
    pairs.forEach(([v, label, title]) => { const b = el('button', '', label); b.type = 'button'; b.dataset.v = v; if (title) b.title = title; b.addEventListener('click', (ev) => { ev.stopPropagation(); onPick(v); rodPaint(); }); s.appendChild(b); });
    return s;
  }
  UI.registerDrawer('rod', {
    title: 'Lightning Rod', icon: 'zap', order: 30, picker: true, hint: 'one-off search, nothing saved',
    build(container) {
      container.appendChild(el('div', 'nx-note', 'A temporary, in-memory selection over the node currently shown in the Reader: search it, filter it by tags, or gather every tag beneath it.'));
      const q = el('div', 'nx-drawer-tools');
      const query = el('input', 'nx-input is-grow'); query.type = 'text';
      query.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); rodFire(); } e.stopPropagation(); });
      query.addEventListener('keyup', (e) => e.stopPropagation());
      q.appendChild(query);
      q.appendChild(btn('Go', 'Run the Lightning Rod (Enter)', () => rodFire(), 'is-primary', 'zap'));
      container.appendChild(q);
      const r1 = el('div', 'nx-drawer-tools');
      const modeSeg = segOf([['search', 'Text', 'Smart free-text search'], ['tags', 'Tags', 'Prefix-family tag filter']], (v) => { rod.mode = v; });
      const scopeSeg = segOf([['name', 'Title', 'Match in the trailing tag region of titles'], ['name_and_note', 'Title + body', 'Match anywhere in title and note']], (v) => { rod.scope = v; });
      r1.appendChild(modeSeg); r1.appendChild(scopeSeg);
      container.appendChild(r1);
      const r2 = el('div', 'nx-drawer-tools');
      const scopedSeg = segOf([['global', 'Global', 'The whole cache subtree under the Reader’s root'], ['scoped', 'Scoped', 'Only the tree currently frozen in the Reader']], (v) => { rod.scoped = (v === 'scoped'); });
      r2.appendChild(scopedSeg);
      container.appendChild(r2);
      const hint = el('div', 'nx-note'); container.appendChild(hint);
      container.appendChild(el('div', 'nx-drawer-section', 'More'));
      const r3 = el('div', 'nx-drawer-tools');
      r3.appendChild(btn('Gather tags', 'List every tag under the Reader’s root as a prefix-grouped outline', () => { api().lightning('gather', '', rod.scope, rod.scoped); UI.afterPick(); }, '', 'tag'));
      r3.appendChild(btn('Dyn slice', 'Render a dynamic slice group rooted at the one selected Reader row', () => api().dynSlice(), '', 'layers'));
      container.appendChild(r3);
      rod.els = { query, modeSeg, scopeSeg, scopedSeg, hint };
      rodPaint();
    },
    headActions(host) { host.appendChild(helpBtn('nx-help-rod', 'rod')); },
    onOpen() { rodPaint(); try { rod.els.query.focus(); } catch (e) { /* ignore */ } },
    refresh() { if (rod.els.query) rodPaint(); },
    commands() { return [{ label: 'Gather tags under the Reader’s root', run: () => api().lightning('gather', '', 'name', false) }, { label: 'Dynamic slice group from the selected row', run: () => api().dynSlice() }]; },
  });
  UI.rod = { refresh: () => { if (rod.els.query) rodPaint(); } };

  // ═════════════════════════ TABS ═════════════════════════
  const tabs = { els: {} };
  UI.registerDrawer('tabs', {
    title: 'Tabs', icon: 'tabs', order: 40, opensAlone: true, hint: 'name this tab for your agents',
    build(container) {
      container.appendChild(el('div', 'nx-drawer-section', 'This tab'));
      const r1 = el('div', 'nx-drawer-tools');
      const alias = el('input', 'nx-input is-grow'); alias.type = 'text'; alias.placeholder = 'tab alias (e.g. spine)';
      alias.title = 'A name agents use as target_tab to reach THIS tab. Enter or clicking away commits.';
      alias.addEventListener('input', () => api().markAliasEdited());
      alias.addEventListener('keydown', (e) => { if (e.key === 'Enter') { api().commitAlias(alias, true); alias.blur(); } e.stopPropagation(); });
      alias.addEventListener('keyup', (e) => e.stopPropagation());
      alias.addEventListener('blur', () => api().commitAlias(alias, false));
      r1.appendChild(alias);
      const def = btn('Default tab', 'Make THIS tab the target of GLIMPSEs that omit target_tab; click again to clear', () => api().toggleDefaultTab(), '', 'star');
      r1.appendChild(def);
      container.appendChild(r1);
      const idRow = el('div', 'nx-note');
      const idLabel = el('span', '', 'endpoint ');
      const idVal = el('span', 'nx-mono', ''); idVal.style.cursor = 'pointer'; idVal.title = 'Click to copy the endpoint id';
      idVal.addEventListener('click', () => copyText(idVal.textContent, (ok) => toast(ok ? 'Copied endpoint id.' : 'Copy failed.', 1800)));
      idRow.appendChild(idLabel); idRow.appendChild(idVal);
      container.appendChild(idRow);
      container.appendChild(el('div', 'nx-drawer-section', 'Saved tabs'));
      container.appendChild(el('div', 'nx-note', 'Export the render state of every Workflowy tab, import a saved set into this tab or drive new tabs, and pick the Anchor Dock the dock routes use.'));
      const r2 = el('div', 'nx-drawer-tools');
      r2.appendChild(btn('Export', 'Survey every Workflowy tab and copy the chosen render states as JSON', () => api().savedTabsExport(), '', 'upload'));
      r2.appendChild(btn('Import', 'Import saved tabs (paste, file or the latest from a Dock); Shift+click = file', (ev) => api().savedTabsImport(!!(ev && ev.shiftKey)), '', 'download'));
      r2.appendChild(btn('Pick dock', 'Choose the last-used Anchor Dock for the Saved-Tabs dock routes', () => api().savedTabsPickDock(), '', 'anchor'));
      container.appendChild(r2);
      const status = el('div', 'nx-status'); container.appendChild(status); UI.registerStatus('tabs', status);
      tabs.els = { alias, def, idVal };
      UI.els().aliasInput = alias;
      try { api().bindAliasInput(alias); } catch (e) { /* ignore */ }
      tabsPaint();
    },
    headActions(host) { host.appendChild(helpBtn('nx-help-tabs', 'tabs')); },
    onOpen() { tabsPaint(); },
    refresh() { tabsPaint(); },
    commands() { return [{ label: 'Set this tab as the default GLIMPSE target', run: () => api().setThisTabAsDefault() }, { label: 'Saved tabs: export', run: () => api().savedTabsExport() }, { label: 'Saved tabs: import', run: () => api().savedTabsImport(false) }]; },
  });
  function tabsPaint() {
    if (!tabs.els.def) return;
    const isDef = !!api().isDefaultTab();
    tabs.els.def.classList.toggle('is-on', isDef);
    tabs.els.def.querySelector('span').textContent = isDef ? 'Default tab ✓' : 'Default tab';
    tabs.els.idVal.textContent = api().getEndpointId() || '';
    if (document.activeElement !== tabs.els.alias) tabs.els.alias.value = api().getAlias() || '';
  }
  UI.tabs = { update: tabsPaint };

  // ═════════════════════════ JOBS ═════════════════════════
  const jobs = { els: {}, skipActive: false, skipDeadline: 0, skipTimer: null };
  function jobsPaintSkip() {
    const b = jobs.els.skip;
    if (!b) return;
    if (!jobs.skipActive) { b.classList.remove('is-on'); b.querySelector('span').textContent = 'Skip refreshes 60 s'; return; }
    const rem = Math.max(0, Math.ceil((jobs.skipDeadline - Date.now()) / 1000));
    if (rem <= 0) { jobs.skipActive = false; clearInterval(jobs.skipTimer); jobs.skipTimer = null; api().setCacheSkipState(false, 0); jobsPaintSkip(); return; }
    b.classList.add('is-on');
    b.querySelector('span').textContent = 'Skipping refreshes · ' + rem + ' s';
  }
  // @beacon[
  //   id=carto-jobs@drawer,
  //   slice_labels=nexus--explorer,
  //   kind=span,
  //   comment=The Rail Jobs drawer keeps the list, omitted-history notice and on-demand Details in separate hosts; opening paints the latest cached list without fetching details. Copy, cancel, clear history and cache controls keep their existing owners.,
  // ]
  UI.registerDrawer('jobs', {
    title: 'Jobs', icon: 'activity', order: 50, opensAlone: true, hint: 'what the daemon is doing',
    build(container) {
      const intro = oneLiner('jobs', 'nx-help-jobs', 'jobs'); if (intro) container.appendChild(intro);
      container.appendChild(el('div', 'nx-drawer-section', 'Cartographer jobs'));
      const host = el('div', 'nx-jobs'); host.id = 'glimpse-carto-jobs'; host.textContent = 'No active CARTO jobs.';
      container.appendChild(host);
      const truncated = el('div', 'nx-note'); truncated.id = 'glimpse-carto-jobs-truncated'; truncated.style.display = 'none';
      container.appendChild(truncated);
      const r1 = el('div', 'nx-drawer-tools');
      r1.appendChild(btn('Copy status', 'Copy the job status lines shown above', () => api().copyJobStatus((ok) => toast(ok ? 'Copied job status.' : 'Copy failed.', 1800)), '', 'copy'));
      r1.appendChild(btn('Clear history', 'Delete finished job history (running jobs are kept)', () => api().clearJobHistory(), '', 'trash'));
      container.appendChild(r1);
      const details = el('div', 'nx-jobs'); details.id = 'glimpse-carto-job-details';
      details.setAttribute('aria-live', 'polite');
      container.appendChild(details);
      container.appendChild(el('div', 'nx-drawer-section', 'Nodes cache'));
      const r2 = el('div', 'nx-drawer-tools');
      r2.appendChild(btn('Refresh cache', 'Refresh the daemon’s /nodes-export cache now', () => { api().refreshCache(); UI.openDrawer('jobs'); }, '', 'refresh'));
      const skip = btn('Skip refreshes 60 s', 'Bypass the automatic pre/post cache refreshes for 60 s (for parallel F12+1 runs on different files). Click again to stop early.', () => {
        const next = !jobs.skipActive;
        if (!api().setCacheSkip(next)) return;
        jobs.skipActive = next; jobs.skipDeadline = next ? Date.now() + 60000 : 0;
        api().setCacheSkipState(next, jobs.skipDeadline);
        if (jobs.skipTimer) { clearInterval(jobs.skipTimer); jobs.skipTimer = null; }
        if (next) jobs.skipTimer = setInterval(jobsPaintSkip, 1000);
        jobsPaintSkip();
      }, '', 'clock');
      r2.appendChild(skip);
      container.appendChild(r2);
      const sec = el('div', 'nx-drawer-section', 'Activity');
      sec.style.display = 'flex'; sec.style.alignItems = 'center';
      const clear = iconBtn('trash', 'Clear the activity log', () => UI.clearActivity()); clear.style.marginLeft = 'auto';
      sec.appendChild(clear);
      container.appendChild(sec);
      const act = el('div', 'nx-activity'); container.appendChild(act);
      UI.bindActivityHost(act);
      jobs.els = { host, skip, truncated, details };
      UI.els().jobs = host;
      try { api().bindJobsHost(host, truncated, details); } catch (e) { /* ignore */ }
      jobsPaintSkip();
    },
    headActions(host) { host.appendChild(helpBtn('nx-help-jobs', 'jobs')); },
    onOpen() { api().renderLatestCartoJobs(); },
    commands() { return [{ label: 'Refresh the nodes cache', run: () => api().refreshCache() }, { label: 'Clear finished Cartographer jobs', run: () => api().clearJobHistory() }]; },
  });
  // @beacon-close[
  //   id=carto-jobs@drawer,
  // ]

  // ═════════════════════════ SETTINGS ═════════════════════════
  let settingsPaint = null;
  UI.registerDrawer('settings', {
    title: 'Settings', icon: 'settings', order: 90, bottom: true, opensAlone: true, hint: 'appearance here; everything else in Options',
    build(container) {
      const paint = () => {
        container.innerHTML = '';
        const s = api().settings();
        container.appendChild(el('div', 'nx-drawer-section', 'Appearance'));
        const r1 = el('div', 'nx-drawer-tools'); r1.appendChild(el('span', 'nx-note', 'Theme'));
        r1.appendChild(seg([{ value: 'auto', label: 'Follow Workflowy' }, { value: 'light', label: 'Light' }, { value: 'dark', label: 'Dark' }], String(s.glimpse_ui_theme || 'auto'), (v) => { api().persistSetting('glimpse_ui_theme', v); UI.applyTheme(); paint(); }));
        container.appendChild(r1);
        const r2 = el('div', 'nx-drawer-tools'); r2.appendChild(el('span', 'nx-note', 'Outline'));
        r2.appendChild(seg([{ value: 'auto', label: 'Auto' }, { value: 'side', label: 'Beside' }, { value: 'top', label: 'Above' }], String(s.glimpse_ui_outline || 'auto'), (v) => { api().persistSetting('glimpse_ui_outline', v); UI.applyLayout(); paint(); }));
        container.appendChild(r2);
        const r3 = el('div', 'nx-drawer-tools'); r3.appendChild(el('span', 'nx-note', 'Dock side'));
        r3.appendChild(seg([{ value: 'left', label: 'Left' }, { value: 'top', label: 'Top' }, { value: 'bottom', label: 'Bottom' }, { value: 'right', label: 'Right' }, { value: 'auto', label: 'Auto', title: 'Bottom on a window taller than wide (pickers get the width), right otherwise' }], String(s.md_preview_dock_position || 'right'), (v) => { api().persistSetting('md_preview_dock_position', v); UI.applyLayout(); paint(); }));
        container.appendChild(r3);
        // "The Rail, side by side": push and the panel side.
        const r4 = el('div', 'nx-drawer-tools'); r4.appendChild(el('span', 'nx-note', 'Workflowy'));
        r4.appendChild(seg([{ value: true, label: 'Pushed aside', title: 'The Reader and a pinned panel take real space: Workflowy narrows beside them (left and right docks)' }, { value: false, label: 'Covered', title: 'The deck covers the page, as the old modal did' }], s.glimpse_ui_push !== false, (v) => { api().persistSetting('glimpse_ui_push', !!v); UI.applyLayout(); paint(); }));
        container.appendChild(r4);
        if (UI.pushFellBack && UI.pushFellBack()) container.appendChild(el('div', 'nx-note', 'Push is off in this tab: Workflowy’s header or outline did not fit beside the panel, so the deck covers the page (details in the Activity log). Reload the tab to try again.'));
        const r5 = el('div', 'nx-drawer-tools'); r5.appendChild(el('span', 'nx-note', 'Panels'));
        r5.appendChild(seg([{ value: false, label: 'Inner side', title: 'A panel opens between the Reader and the page' }, { value: true, label: 'Outer side', title: 'A panel opens between the Reader and the screen edge' }], !!s.glimpse_ui_flip, (v) => { api().persistSetting('glimpse_ui_flip', !!v); UI.applyLayout(); paint(); }));
        container.appendChild(r5);
        container.appendChild(el('div', 'nx-note', 'Width and height: drag the deck’s inner edge. A pinned panel: drag the divider. Outline share: drag the splitter. Top offset (when the deck covers the page): Options page.'));
        // The first-run tips come back on request only (R2): clear glimpse_help_dismissed and show card 1 now.
        const r6 = el('div', 'nx-drawer-tools'); r6.appendChild(el('span', 'nx-note', 'First-run tips'));
        r6.appendChild(btn('Show the first-run tips again', 'Three short cards shown the first time the rail appears: the rail, Anchor Docks, slice groups', () => {
          api().persistSetting('glimpse_help_dismissed', []);
          if (UI.showTips) UI.showTips(true);
        }, '', 'help'));
        container.appendChild(r6);
        container.appendChild(el('div', 'nx-drawer-section', 'Global tags'));
        const tags = Array.isArray(s.global_tags) ? s.global_tags : [];
        const tw = el('div', 'nx-note');
        if (!tags.length) tw.textContent = 'None configured (Options page → Note sessions → Global tags).';
        else tags.forEach((t) => { const c = el('span', 'nx-tag', '#' + t); c.style.margin = '2px 4px 2px 0'; tw.appendChild(c); });
        container.appendChild(tw);
        container.appendChild(el('div', 'nx-drawer-section', 'Everything else'));
        container.appendChild(el('div', 'nx-note', 'Hotkeys, the broker token, limits, F8 postfix words, note-session tags and the tab-title affix live on the extension’s Options page: chrome://extensions → ' + word('manifestName', 'NEXUS for Workflowy') + ' → Details → Extension options.'));
      };
      paint();
      settingsPaint = paint;
    },
    refresh() { if (settingsPaint) settingsPaint(); },
    onOpen() { },
  });

  // ═════════════════════════ HELP ═════════════════════════
  // More… / ? : open the Help drawer scrolled to one section, with that section's '← Back to …'
  // link shown (every other Back link hidden). From the rail, Help opens at its top with no Back.
  UI.openHelpAt = function openHelpAt(sectionId, backId) {
    UI.openDrawer('help');
    const body = UI.els().drawerBody;
    if (!body) return;
    body.querySelectorAll('.nx-help-back').forEach((b) => { b.style.display = 'none'; });
    const sec = sectionId ? body.querySelector('#' + sectionId) : null;
    if (!sec) return;
    const back = sec.querySelector('.nx-help-back');
    if (back && backId) back.style.display = '';
    const scroll = () => { try { body.scrollTop = sec.getBoundingClientRect().top - body.getBoundingClientRect().top + body.scrollTop - 4; } catch (e) { /* ignore */ } };
    scroll(); setTimeout(scroll, 0);
  };
  UI.registerDrawer('help', {
    title: 'Help', icon: 'help', order: 95, bottom: true, opensAlone: true, hint: 'keys, menus, and how it fits together',
    build(container) {
      const hk = api().hotkeys() || {};
      const labels = (root.GlimpseHotkeys && root.GlimpseHotkeys.HOTKEY_ACTIONS) || {};
      // The in-app README (R2): the system line, one paragraph, then one section per rail item
      // (Reader, Anchor Docks, Slice groups, Lightning Rod, Tabs, Jobs), each a Back link to the
      // drawer it explains (shown only when More… or ? brought us here), its heading, byline and
      // paragraphs. The keys, the F12 numbers and the deck's own keys follow, as before.
      if (HELP) {
        container.appendChild(el('div', 'nx-help-system', HELP.systemLine));
        container.appendChild(el('div', 'nx-note nx-help-intro', HELP.helpIntro));
        (HELP.sections || []).forEach((sec) => {
          const s = el('div', 'nx-help-sec'); s.id = sec.id;
          const back = el('a', 'nx-link nx-help-back', '← Back to ' + sec.backLabel); back.href = '#'; back.style.display = 'none';
          back.addEventListener('click', (ev) => { ev.preventDefault(); ev.stopPropagation(); if (sec.backId === 'reader') UI.closeDrawer(); else UI.openDrawer(sec.backId); });
          s.appendChild(back);
          s.appendChild(el('div', 'nx-help-h', sec.heading));
          if (sec.byline) s.appendChild(el('div', 'nx-drawer-byline', sec.byline));
          (sec.paras || []).forEach((p) => s.appendChild(mdInline(el('p', 'nx-help-p'), p)));
          container.appendChild(s);
        });
      }
      container.appendChild(el('div', 'nx-drawer-section', 'Keys (act on the node under the mouse)'));
      const list = el('div', 'nx-note');
      Object.keys(hk).forEach((action) => {
        const row = el('div', 'nx-pop-row');
        const k = el('kbd', 'nx-kbd', hk[action]); k.style.flex = '0 0 auto'; row.appendChild(k);
        row.appendChild(el('span', '', (labels[action] && labels[action].label) ? labels[action].label.replace(/\s*\(see .*?\)/, '') : action));
        list.appendChild(row);
      });
      container.appendChild(list);
      container.appendChild(el('div', 'nx-drawer-section', 'Ctrl+F12 menu numbers'));
      const f12 = el('div', 'nx-note');
      // R2's corrected numbers (the Folder row and the second-step line), from glimpse-ui-help.js.
      const f12Rows = (HELP && Array.isArray(HELP.f12Numbers)) ? HELP.f12Numbers : [
        ['File', '1 async refresh from disk · 2 bulk visible beacon/tag apply · 3 regenerate the Markdown file · 0 forced refresh (two-step)'],
        ['Folder', '1 async folder refresh · 2 bulk visible beacon/tag apply · 3 bulk regenerate Markdown files (when offered) · 0 forced refresh (press twice)'],
        ['Node', '1 fast beacon update · 2 async containing-file refresh · 3 bulk apply on the containing file · 4 materialize / explode (when offered)'],
        ['Note', '4 materialize into a Markdown file · 5 explode into a subtree · bulk variants when the server offers them'],
      ];
      f12Rows.forEach(([k, v]) => { const d = el('div', ''); d.style.margin = '3px 0'; const b = el('b', '', k + ': '); d.appendChild(b); d.appendChild(document.createTextNode(v)); f12.appendChild(d); });
      container.appendChild(f12);
      container.appendChild(el('div', 'nx-drawer-section', 'Inside the deck'));
      container.appendChild(el('div', 'nx-note', 'Drawers: ↑ ↓ move, Enter opens, ← → fold, Esc closes. Apply popover: 1 / 2 / Esc. Reader: ↑ ↓ move the selection, Ctrl+F finds, Ctrl-click and Shift-click select several rows in a slice or frozen view. Palette: ' + (hk.command_palette || 'Ctrl+Shift+K') + ' (every command by name, including “Resolve a UUID path…”, whose answer lands in the Jobs drawer’s Activity log).'));
    },
    onOpen() { try { UI.els().drawerBody.querySelectorAll('.nx-help-back').forEach((b) => { b.style.display = 'none'; }); } catch (e) { /* ignore */ } },
  });
})(typeof globalThis !== 'undefined' ? globalThis : window);
