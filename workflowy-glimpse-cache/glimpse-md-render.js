// Shared Markdown and row-list owner for the dock and standalone document hosts.
// Load after marked.min.js, katex/katex.min.js and marked-katex-init.js.
// @beacon[
//   id=md-render@module,
//   slice_labels=nexus--md-preview,
//   kind=ast,
//   comment=Publishes the frozen GlimpseMd API; moved dock Markdown/source/search/escape and row-list bodies with host state passed at paint time.,
// ]
(function installGlimpseMd() {
  'use strict';

  // @beacon[
  //   id=md-render@escape-html,
  //   slice_labels=nexus--md-preview,
  //   kind=ast,
  //   comment=Moved escapeHtmlForPreview body; preserves null handling and ampersand/angle-bracket escaping for shared rendering and the dock's external escape callers.,
  // ]
  function escapeHtml(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // @beacon[
  //   id=md-render@to-html,
  //   slice_labels=nexus--md-preview,
  //   kind=ast,
  //   comment=Moved composedMarkdownToHtml body: same marked.parse breaks:true and escaped-pre fallback; source/raw_text route through sourceHtml with optional search-state callback.,
  // ]
  function toHtml(composed, options) {
    const opts = options || {};
    if (opts.view === 'source' || opts.contentFormat === 'raw_text') {
      return sourceHtml(composed || '', opts);
    }
    try {
      if (typeof marked !== 'undefined' && marked && typeof marked.parse === 'function') {
        // breaks: true — treat single '\n' as <br> so line-oriented Workflowy
        // note bodies (bullet lists, numbered lists, multi-line text) render
        // each line on its own line instead of collapsing into one run-on
        // paragraph (CommonMark's default soft-break behavior).
        return marked.parse(composed, { breaks: true });
      }
    } catch (err) {
      /* fall through to plain */
    }
    return `<pre class="glimpse-md-preview-plain">${escapeHtml(composed)}</pre>`;
  }

  // @beacon[
  //   id=md-render@compile-source-search,
  //   slice_labels=nexus--md-preview,
  //   kind=ast,
  //   comment=Moved compileDockSourceSearch smart-highlight grammar; positive terms/phrases/globs only with legacy blank and malformed-query results.,
  // ]
  function compileSourceSearch(query) {
    const q = (typeof query === 'string') ? query.trim() : '';
    if (!q) return { ok: true, regex: null };
    // Tokenize: whitespace-delimited, but keep "quoted phrases" as one token.
    // An unbalanced quote is a loud error (mirrors the server's shlex failure).
    const tokens = [];
    let cur = '';
    let inQuotes = false;
    for (let i = 0; i < q.length; i++) {
      const ch = q[i];
      if (ch === '"') { inQuotes = !inQuotes; cur += ch; continue; }
      if (!inQuotes && (ch === ' ' || ch === '\t')) {
        if (cur) { tokens.push(cur); cur = ''; }
        continue;
      }
      cur += ch;
    }
    if (inQuotes) return { ok: false, error: 'unbalanced quotes' };
    if (cur) tokens.push(cur);
    // Collect POSITIVE terms only (OR/AND + negation skipped).
    const pats = [];
    for (const tok of tokens) {
      const upper = tok.toUpperCase();
      if (upper === 'OR' || upper === 'AND') continue;
      if (tok.startsWith('-') || tok.startsWith('!')) continue;
      let term = tok;
      if (term.length >= 2 && term.startsWith('"') && term.endsWith('"')) {
        term = term.slice(1, -1);
      }
      if (!term) continue;
      // Escape every regex metachar, then restore the two GLOB wildcards.
      let p = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      p = p.replace(/\\\*/g, '.*').replace(/\\\?/g, '.');
      // Skip a pattern that could match the empty string (defensive; a bare
      // '*' is the realistic offender).
      let emptyOk = false;
      try { emptyOk = new RegExp('^' + p + '$').test(''); } catch (e) { emptyOk = true; }
      if (emptyOk) continue;
      pats.push(p);
    }
    if (pats.length === 0) return { ok: true, regex: null };
    let rx = null;
    try {
      rx = new RegExp('(' + pats.join('|') + ')', 'gi');
    } catch (e) {
      return { ok: false, error: 'bad query' };
    }
    return { ok: true, regex: rx };
  }

  // @beacon[
  //   id=md-render@source-html,
  //   slice_labels=nexus--md-preview,
  //   kind=ast,
  //   comment=Moved dockedSourceHtml text paint: exact escaped pre and find marks with first/current semantics and regex lastIndex handling; reports legacy counter writes through onSearchState without holding host state.,
  // ]
  function sourceHtml(text, options) {
    const opts = options || {};
    const src = text || '';
    const rx = opts.findRegex;
    let inner;
    if (!rx) {
      inner = escapeHtml(src);
      if (opts.onSearchState) opts.onSearchState({ count: 0, current: -1 });
    } else {
      let out = '';
      let last = 0;
      let count = 0;
      rx.lastIndex = 0;
      let m;
      while ((m = rx.exec(src)) !== null) {
        if (m.index > last) out += escapeHtml(src.slice(last, m.index));
        const cls = (count === 0)
          ? 'glimpse-dock-src-hit glimpse-dock-src-hit--current'
          : 'glimpse-dock-src-hit';
        out += '<mark class="' + cls + '">' + escapeHtml(m[0]) + '</mark>';
        count += 1;
        last = m.index + m[0].length;
        if (m[0].length === 0) rx.lastIndex += 1;  // defensive zero-length guard
      }
      if (last < src.length) out += escapeHtml(src.slice(last));
      inner = out;
      if (opts.onSearchState) opts.onSearchState({ count, current: (count > 0) ? 0 : -1 });
    }
    return `<pre class="glimpse-md-preview-plain">${inner}</pre>`;
  }

  // TIMELINE / HOVER helpers (Feature 1+2, Dan 2026). Presentation-only: they
  // format a node's own timestamp for the row hover tooltip and compute the
  // LOCAL-day ordinal/label that drives the leaf-driven day-change separators
  // in renderRowList. Timestamps are epoch-SECONDS (server-joined from the
  // /nodes-export cache); NOTHING here touches selection/occurrence identity.
  function formatDockTimestamp(epochSec, label) {
    try {
      const d = new Date(epochSec * 1000);
      const s = d.toLocaleString(undefined, {
        year: 'numeric', month: 'short', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
      return label ? (label + ': ' + s) : s;
    } catch (e) { return ''; }
  }
  function localDayOrdinal(epochSec) {
    // Days since epoch in the viewer's LOCAL timezone (so a day boundary is
    // local midnight). Used only for the monotonic 'has the day advanced?'
    // comparison that gates a separator; never for identity.
    const ms = epochSec * 1000;
    const d = new Date(ms);
    return Math.floor((ms - d.getTimezoneOffset() * 60000) / 86400000);
  }
  function localDayLabel(epochSec) {
    try {
      return new Date(epochSec * 1000).toLocaleDateString(undefined, {
        weekday: 'short', year: 'numeric', month: 'short', day: '2-digit',
      });
    } catch (e) { return ''; }
  }

  // Render the flat list rows into the list region. Each row is indented by
  // its depth and carries a column-0 marker slot (the glowing ball appears on
  // the selected row only). Clicking a row invokes the host's selection
  // handler (never enters edit mode — the list is inert).
  // @beacon[
  //   id=md-render@row-list,
  //   slice_labels=nexus--md-preview,
  //   kind=ast,
  //   comment=Moved renderDockedList painter with injected container/rows/sort/selection/click/emoji; preserves child DOM and _rowEl effects with canonical name alias and no live-DOM discovery.,
  // ]
  function renderRowList(container, rows, { sortBy, sortDir, selected, primary, onClick, markdownEmoji = '📘' }) {
    if (!container) return;
    container.innerHTML = '';
    const INDENT_PX = 14;
    // TIMELINE/HOVER sort context (Feature 1+2). The hover tooltip shows each
    // node's OWN timestamp on the active axis (created sort -> created;
    // modified sort -> modified; unsorted -> created). The day-change timeline
    // is drawn ONLY under a chronological sort, and ONLY leaf rows drive it.
    const _tlSortBy = sortBy;     // 'none' | 'created' | 'modified'
    const _tlSortDir = sortDir;   // 'asc' | 'desc'
    const _tlOn = (_tlSortBy === 'created' || _tlSortBy === 'modified');
    let _tlLastDayOrd = null;                    // monotonic day cursor
    rows.forEach((row, idx) => {
      const rowEl = document.createElement('div');
      rowEl.className = 'glimpse-md-dock-row';
      // Row layout (left → right):
      //   [marker]  selection dot, pinned at TRUE column 0 (NOT indented)
      //   [indent]  per-depth spacer
      //   [icon]    \ud83d\udcd8 for md-file, bullet for md heading node, blank otherwise
      //   [label]   raw node text (no wrap; overflows right)
      // paddingLeft stays 0 so the marker truly sits at column 0; indentation
      // is achieved by an inline spacer span instead.
      rowEl.style.paddingLeft = '0';

      const marker = document.createElement('span');
      marker.className = 'glimpse-md-dock-marker';
      // PHASE 1 MULTI-SELECT (ap:MULTISEL): a list REBUILD must restore the
      // FULL multi-selection, not just the primary row. Paint the marker from
      // selected (the set) exactly as updateDockRowMarkers does --
      // filled dot for the primary (primary), hollow dot for other
      // selected rows, blank otherwise -- and re-apply the '.selected' CLASS to
      // every set member. Previously this line consulted ONLY
      // primary, so any rebuild between gestures silently dropped
      // every non-primary selection's marker AND class (and left the fresh
      // rowEl without the .selected class entirely), desyncing the set from the
      // DOM -- the root of the 'picked 4, DOM shows 2' bug and the downstream
      // focus:null whole-tree fallback.
      const _isSelInSet = (selected && selected.has(idx));
      const _isPrimary = (idx === primary);
      marker.textContent = _isPrimary ? '\u25CF' : (_isSelInSet ? '\u25CB' : '\u00A0');

      const indent = document.createElement('span');
      indent.className = 'glimpse-md-dock-indent';
      indent.style.display = 'inline-block';
      indent.style.width = `${row.depth * INDENT_PX}px`;
      // INDENT GUIDES: hand the row's depth to the ::before guide overlay via
      // a custom property (see the .glimpse-md-dock-row::before CSS rule).
      // INDENT_PX (14) must stay in sync with that rule's 14px/56px periods.
      rowEl.style.setProperty('--glimpseDockDepth', String(row.depth || 0));

      const icon = document.createElement('span');
      icon.className = 'glimpse-md-dock-icon';
      // row.collapsed is really "has HIDDEN descendants" (see
      // detectCollapsedChildren). When true we add the halo ring class onto
      // WHATEVER icon glyph this row uses, so the haloed-bullet visual is
      // consistent across ALL node kinds, not just in-file Markdown headings.
      var hasHidden = !!row.collapsed;
      if (row.kind === 'mdfile') {
        // Markdown FILE node: keep the file icon; add the halo when it has
        // hidden descendants too.
        icon.textContent = markdownEmoji;
        if (hasHidden) icon.classList.add('glimpse-md-dock-bullet--halo');
      } else if (row.kind === 'mdnode') {
        // In-file Markdown heading node: a normal small bullet, haloed when
        // it has hidden descendants (mimicking Workflowy's haloed bullet).
        icon.textContent = '\u2022';
        icon.classList.add('glimpse-md-dock-bullet');
        if (hasHidden) icon.classList.add('glimpse-md-dock-bullet--halo');
      } else {
        // Non-Markdown node: normally NO icon. But if it has hidden
        // descendants, show a dimmer bullet WITH the halo so hidden content
        // is indicated consistently here too. Leaf / fully-shown non-md
        // nodes stay icon-free.
        if (hasHidden) {
          icon.textContent = '\u2022';
          icon.classList.add('glimpse-md-dock-bullet');
          icon.classList.add('glimpse-md-dock-bullet--halo');
        } else {
          icon.textContent = '';
        }
      }

      const label = document.createElement('span');
      label.className = 'glimpse-md-dock-label';
      label.textContent = (row.text !== undefined ? row.text : row.name) || '\u00A0';

      // Re-apply the selection highlight class on rebuild for EVERY set member
      // (see the multi-select marker note above).
      if (_isSelInSet || _isPrimary) rowEl.classList.add('selected');
      rowEl.appendChild(marker);
      rowEl.appendChild(indent);
      rowEl.appendChild(icon);
      rowEl.appendChild(label);
      rowEl.addEventListener('click', (e) => {
        e.stopPropagation();
        // PHASE 1 MULTI-SELECT (ap:MULTISEL): a modified click (Ctrl/Cmd toggle
        // or Shift range) enters/extends a multi-selection in SLICE mode; a
        // plain click is single-select (byte-for-byte the prior behavior).
        onClick(idx, e);
        if (container) container.focus();
      });
      // HOVER TOOLTIP (Feature 1): the node's OWN timestamp on the active axis,
      // human-readable down to the second. Applies to ALL rows -- a branch row
      // correctly shows its own (possibly months-old) timestamp. Unsorted
      // defaults to the creation timestamp.
      const _hoverTs = (_tlSortBy === 'modified') ? row.modified : row.created;
      if (typeof _hoverTs === 'number' && _hoverTs > 0) {
        rowEl.title = formatDockTimestamp(
          _hoverTs, (_tlSortBy === 'modified') ? 'Modified' : 'Created');
      }

      // TIMELINE DAY-SEPARATOR (Feature 2): LEAF rows ONLY, chronological sort
      // ONLY. A leaf is a row with no deeper row immediately following it (the
      // established contiguous-depth-run rule -- see frozenSubtreeHasFollowing
      // Node). When a leaf's LOCAL day advances in the sort direction, insert a
      // thin full-width rule with a tiny left-aligned day label IMMEDIATELY
      // BEFORE that leaf -- never keyed to or positioned around branch rows.
      // The separator is PURELY PRESENTATIONAL: it is NOT an entry in
      // rows, has no _rowEl / click handler, and never affects
      // selection, occurrence, multi-select, or per-row re-projection. The
      // monotonic 'advanced' guard makes the timeline immune to any
      // non-monotonic blip (e.g. a modified-sort leaf whose own mtime trails
      // its branch-max sort key) -- it can never render a backward day line.
      if (_tlOn) {
        const _next = rows[idx + 1];
        const _isLeaf = !_next ||
          (typeof _next.depth === 'number' && _next.depth <= row.depth);
        const _tlTs = (_tlSortBy === 'modified') ? row.modified : row.created;
        if (_isLeaf && typeof _tlTs === 'number' && _tlTs > 0) {
          const _ord = localDayOrdinal(_tlTs);
          let _advanced;
          if (_tlLastDayOrd === null) _advanced = true;
          else _advanced = (_tlSortDir === 'desc')
            ? (_ord < _tlLastDayOrd) : (_ord > _tlLastDayOrd);
          if (_advanced) {
            const sep = document.createElement('div');
            sep.className = 'glimpse-md-dock-daysep';
            sep.style.display = 'flex';
            sep.style.alignItems = 'center';
            sep.style.gap = '6px';
            sep.style.margin = '5px 0 1px 0';
            sep.style.pointerEvents = 'none';
            sep.style.userSelect = 'none';
            const lab = document.createElement('span');
            lab.className = 'glimpse-md-dock-daysep-label';
            lab.textContent = localDayLabel(_tlTs);
            lab.style.flex = '0 0 auto';
            lab.style.fontSize = '9px';
            lab.style.fontWeight = '600';
            lab.style.whiteSpace = 'nowrap';
            lab.style.color = 'rgba(120,140,170,0.95)';
            lab.style.letterSpacing = '0.3px';
            const line = document.createElement('span');
            line.style.flex = '1 1 auto';
            line.style.height = '0';
            line.style.borderTop = '1px solid rgba(120,140,170,0.4)';
            sep.appendChild(lab);
            sep.appendChild(line);
            container.appendChild(sep);
            _tlLastDayOrd = _ord;
          }
        }
      }

      row._rowEl = rowEl;
      container.appendChild(rowEl);
    });
  }

  globalThis.GlimpseMd = Object.freeze({ toHtml, sourceHtml, compileSourceSearch, escapeHtml, renderRowList });
})();
