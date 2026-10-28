// The image lightbox for rendered NEXUS documents: host-neutral, beside the shared renderer (glimpse-md-render.js).
// A host renders Markdown with GlimpseMd.toHtml, parses that HTML inertly (a template element), then:
//   mapImages(root, mapSrc)  local image paths (/…, X:/…, file://…) → the host's URL for them, before any request
//   livePaths(root)          the folders (…/) and globs (…/*.png) its live gallery lines name, for the host to list
//   expandLive(root, lookup, mapSrc)  each live line → one image per file the host listed (or a labelled empty/error tile)
//   sheets(root)             a paragraph of two or more images → a contact-sheet grid of captioned tiles
//   collect(root, {title})   the document's images in groups, one group per heading, in document order
//   imageOnly(root)          whether the document is a gallery (headings, images, an intro line or two)
//   wire(container, {onOpen}) a tap on an image opens it; a broken image becomes a labelled missing tile
//   open(groups, options)    the full-screen lightbox (PhotoSwipe 5, vendored in photoswipe/, loaded before this file)
//   compare(groups, options) side by side: two or three groups in panels stepping together (a wide screen's own viewer)
// Nothing here fetches: the host maps paths to URLs, lists live folders and globs, and passes image sizes in (PhotoSwipe
// needs them up front).
// Load after photoswipe/photoswipe.umd.min.js; style with glimpse-lightbox.css after photoswipe/photoswipe.css.
// @beacon[
//   id=md-lightbox@module,
//   slice_labels=nexus--md-preview,
//   kind=ast,
//   comment=Publishes the frozen GlimpseLightbox API (path mapping, live folder/glob expansion from a host's listing, contact sheets, heading groups, the gallery test, tap wiring, the PhotoSwipe lightbox and the side-by-side viewer) for View today and the Markdown modal later; pure helpers are exported for Node tests.,
// ]
(function installGlimpseLightbox() {
  'use strict';

  const esc = text => String(text == null ? '' : text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const HEADING = /^H[1-6]$/;
  const PROSE_MAX = 400;  // the longest paragraph a gallery may still have (an intro line or two), in characters

  // ---- pure helpers (no DOM)

  // The absolute local path an image source names, as a WSL path (/mnt/e/…), or null for a remote, data or blob
  // source. Accepted: /abs/path, X:/path (any drive letter, forward slashes), file:///abs/path, file:///X:/path;
  // percent-escapes are decoded (a space is written %20 in a note).
  function localPath(src) {
    let s = String(src == null ? '' : src).trim();
    if (!s || /^(?:https?|data|blob):/i.test(s)) return null;
    const file = /^file:(?:\/\/(?:localhost)?)?(\/.*)$/i.exec(s);
    if (file) s = file[1];
    else if (/^[a-z][a-z0-9+.-]*:(?![\\/])/i.test(s) && !/^[a-z]:/i.test(s)) return null;  // another URL scheme
    try { s = decodeURIComponent(s); } catch (_) { /* a lone % stays as written */ }
    s = s.replace(/\\/g, '/');
    const drive = /^\/?([a-z]):\/(.*)$/i.exec(s);
    if (drive) return '/mnt/' + drive[1].toLowerCase() + '/' + drive[2];
    if (s.startsWith('//') || !s.startsWith('/')) return null;  // protocol-relative (remote), or relative to nothing
    return s;
  }
  // A path that names a set of files, not one: a folder (ending in /) or a glob (* or ? in its last segment).
  // Live sets are a host's to expand; until it does, they show as a labelled tile and never join the lightbox.
  function liveKind(path) {
    const p = String(path || '');
    if (p.endsWith('/')) return 'folder';
    return /[*?]/.test(p.slice(p.lastIndexOf('/') + 1)) ? 'glob' : null;
  }
  function fileName(src) {
    const s = String(src || '').replace(/[?#].*$/, '').replace(/\/+$/, '');
    let name = s.slice(s.lastIndexOf('/') + 1);
    try { name = decodeURIComponent(name); } catch (_) { /* as written */ }
    return name || s;
  }
  // The caption of an image a folder or glob supplied (it has no alt text of its own): its file name without the
  // extension, dashes and underscores read as spaces (card-dark.png → card dark).
  function fileCaption(path) {
    const name = fileName(path);
    return name.replace(/\.[^./]+$/, '').replace(/[-_]+/g, ' ').replace(/\s+/g, ' ').trim() || name;
  }
  // A group's short label: its heading up to the first ' · ', without a leading [ap:…] handle or trailing #tags.
  function groupLabel(heading) {
    let t = String(heading || '').replace(/\s+/g, ' ').trim().replace(/^\[ap:[a-z0-9]+\]\s*/i, '');
    const cut = t.indexOf(' · ');
    if (cut > 0) t = t.slice(0, cut);
    return t.replace(/(?:\s+#[^\s#]+)+$/, '').trim();
  }
  // Group a document-order sequence of {heading: text[, label]} and {image: record}: every heading starts a group, the
  // images after it (up to the next heading) are its own; images before any heading belong to the document's title. A
  // live line's set (a folder or glob line with alt text) arrives as a heading with its own label. Empty groups are
  // dropped. Each image gets its flat index in the whole sequence (the lightbox is one sequence).
  function groupImages(sequence, title) {
    const groups = [];
    let current = {label: groupLabel(title) || 'Images', heading: String(title || '').trim(), images: []}, index = 0;
    for (const item of sequence || []) {
      if (item && typeof item.heading === 'string') {
        if (current.images.length) groups.push(current);
        const label = typeof item.label === 'string' ? item.label.replace(/\s+/g, ' ').trim() : groupLabel(item.heading);
        current = {label: label || 'Images', heading: item.heading.replace(/\s+/g, ' ').trim(), images: []};
      } else if (item && item.image) {
        item.image.index = index++;
        item.image.group = groups.length;
        current.images.push(item.image);
      }
    }
    if (current.images.length) groups.push(current);
    return groups;
  }
  // A document is a gallery when it is images: besides headings, rules and the daemon's own render_scope lines it has
  // at least one image and at most one short paragraph per heading (plus one), none longer than PROSE_MAX. An estate's
  // document (its entries' "📄 [N] …" headings) is a gallery only when every entry is: each has an image, none failed.
  // blocks: [{kind: 'heading' | 'entry' | 'images' | 'meta' | 'prose', count?, chars?, failed?}]
  function isImageOnly(blocks) {
    let images = 0, headings = 0, prose = 0, entry = null;
    const entries = [];
    for (const b of blocks || []) {
      if (b.kind === 'entry') { headings++; entries.push(entry = {images: 0, failed: !!b.failed}); }
      else if (b.kind === 'images') { images += b.count || 0; if (entry) entry.images += b.count || 0; }
      else if (b.kind === 'heading') headings++;
      else if (b.kind === 'prose') { if ((b.chars || 0) > PROSE_MAX) return false; prose++; }
    }
    return images > 0 && prose <= headings + 1 && entries.every(e => e.images > 0 && !e.failed);
  }
  // The lightbox's key bindings beyond PhotoSwipe's own (← → next/previous, Esc close, Z zoom); null = not ours.
  function keyAction(key, zoomed) {
    switch (key) {
      case 'PageDown': return 'next-group';
      case 'PageUp': return 'prev-group';
      case 'ArrowDown': return zoomed ? null : 'next-group';  // zoomed in, the arrows pan (PhotoSwipe)
      case 'ArrowUp': return zoomed ? null : 'prev-group';
      case 'Home': return 'group-first';
      case 'End': return 'group-last';
      case '+': case '=': return 'zoom-in';
      case '-': case '_': return 'zoom-out';
      case '0': return 'zoom-fit';
      case '1': return 'zoom-actual';
      case ' ': return 'chrome';
      case 'f': case 'F': return 'fullscreen';
      default: return null;
    }
  }

  // ---- side by side, the arithmetic (compare draws it). Panel k shows image position + offsets[k] of its group: the
  // panels step together by position (image 7 of each), and a nudge moves one panel's offset for sets that do not line
  // up one for one. model: {lengths: [images per shown group], offsets: [per panel], position}.
  const COMPARE_MAX = 3;
  // The groups shown at first: the first two or three.
  function comparePicks(count) { return Array.from({length: Math.min(COMPARE_MAX, Math.max(0, count | 0))}, (_, g) => g); }
  // A tap on a group's chip: a shown group goes (one panel always stays); another comes in beside them or, with three
  // shown, in place of the right-most. Panels keep document order.
  function comparePick(picks, g) {
    const now = [...(picks || [])];
    if (now.includes(g)) return now.length > 1 ? now.filter(x => x !== g) : now;
    return (now.length < COMPARE_MAX ? [...now, g] : [...now.slice(0, -1), g]).sort((a, b) => a - b);
  }
  // The positions worth stepping to: from the first where any panel shows its first image to the last where any shows
  // its last.
  function compareRange(model) {
    let lo = Infinity, hi = -Infinity;
    (model.lengths || []).forEach((n, k) => { if (n > 0) { const o = model.offsets[k] || 0; lo = Math.min(lo, 0 - o); hi = Math.max(hi, n - 1 - o); } });
    return lo > hi ? {lo: 0, hi: 0} : {lo, hi};
  }
  // Each panel's image index in its group at the model's position, or -1 (before its first or past its last).
  function compareShown(model) {
    return (model.lengths || []).map((n, k) => { const i = model.position + (model.offsets[k] || 0); return i >= 0 && i < n ? i : -1; });
  }
  // @beacon[
  //   id=md-lightbox@compare-step,
  //   slice_labels=nexus--md-preview,
  //   kind=ast,
  //   comment=The side-by-side viewer's stepping as pure arithmetic (no DOM): next/prev/first/last move the shared position within compareRange (every panel together, a panel past its last image showing none); nudge-next/nudge-prev move one panel's offset by one image, clamped to its own group (from none past its end, a nudge back shows its last); align drops every nudge and keeps the shared position. Returns a new model.,
  // ]
  function compareStep(model, action, panel) {
    const lengths = [...(model.lengths || [])], offsets = lengths.map((_, k) => model.offsets?.[k] || 0);
    let position = Number(model.position) || 0;
    const {lo, hi} = compareRange({lengths, offsets});
    switch (action) {
      case 'next': position = Math.min(hi, position + 1); break;
      case 'prev': position = Math.max(lo, position - 1); break;
      case 'first': position = lo; break;
      case 'last': position = hi; break;
      case 'nudge-next': case 'nudge-prev': {
        const n = lengths[panel];
        if (!(n > 0)) break;
        const to = Math.max(0, Math.min(n - 1, position + offsets[panel] + (action === 'nudge-next' ? 1 : -1)));
        offsets[panel] = to - position;
        break;
      }
      case 'align': offsets.fill(0); position = Math.max(0, Math.min(Math.max(0, ...lengths) - 1, position)); break;
      default: break;
    }
    return {lengths, offsets, position};
  }
  // The side-by-side viewer's keys, shared by every panel; null = not its own.
  function compareKey(key) {
    switch (key) {
      case 'ArrowRight': return 'next';
      case 'ArrowLeft': return 'prev';
      case 'Home': return 'first';
      case 'End': return 'last';
      case '+': case '=': return 'zoom-in';
      case '-': case '_': return 'zoom-out';
      case '0': return 'zoom-fit';
      case '1': return 'zoom-actual';
      case 'a': case 'A': return 'align';
      case ' ': return 'chrome';
      case 'f': case 'F': return 'fullscreen';
      case 'Escape': return 'close';
      default: return null;
    }
  }
  // Where one panel draws its image: fitted (zoom null) or at zoom × its own pixels (1 = actual pixels), the point at
  // (cx, cy) of the image (fractions) in the middle of the panel, never leaving a gap at an edge it overflows; an image
  // smaller than the panel is centred. Every panel shares zoom, cx and cy, so same-size screenshots stay aligned.
  function compareGeometry(view, natural, zoom, cx = 0.5, cy = 0.5) {
    const W = view?.width, H = view?.height, w = natural?.width, h = natural?.height;
    if (!(W > 0 && H > 0 && w > 0 && h > 0)) return null;
    const fit = Math.min(W / w, H / h), scale = zoom > 0 ? zoom : fit, dw = w * scale, dh = h * scale;
    const place = (V, d, c) => (d <= V ? (V - d) / 2 : Math.max(V - d, Math.min(0, V / 2 - c * d)));
    return {fit, scale, width: dw, height: dh, left: place(W, dw, cx), top: place(H, dh, cy)};
  }

  // ---- the document (an inert parse before mounting, or the mounted document)

  // @beacon[
  //   id=md-lightbox@map-images,
  //   slice_labels=nexus--md-preview,
  //   kind=ast,
  //   comment=Before the HTML goes live (call it on a template's content): each image whose source is a local path gets the host's URL for it (mapSrc(path)), its original kept in data-glimpse-src and the path in data-glimpse-path, lazy and async; http(s)/data/blob sources stay; a folder or glob path gets no source and is marked data-glimpse-live for the host to expand.,
  // ]
  function mapImages(root, mapSrc) {
    let mapped = 0;
    for (const img of root?.querySelectorAll?.('img') || []) {
      const raw = img.getAttribute('src');
      if (raw == null || img.hasAttribute('data-glimpse-src')) continue;
      img.setAttribute('data-glimpse-src', raw);
      img.setAttribute('loading', 'lazy');
      img.setAttribute('decoding', 'async');
      const path = localPath(raw);
      if (!path) continue;
      img.setAttribute('data-glimpse-path', path);
      const live = liveKind(path);
      if (live) { img.removeAttribute('src'); img.setAttribute('data-glimpse-live', live); continue; }
      const url = typeof mapSrc === 'function' ? mapSrc(path, raw) : null;
      if (url) { img.setAttribute('src', url); mapped++; }
    }
    return mapped;
  }
  // The folder and glob paths a mapped document's live lines name, each once, in document order (what a host lists).
  function livePaths(root) {
    const paths = new Set();
    for (const img of root?.querySelectorAll?.('img[data-glimpse-live]') || []) {
      const path = img.getAttribute('data-glimpse-path');
      if (path) paths.add(path);
    }
    return [...paths];
  }
  // @beacon[
  //   id=md-lightbox@expand-live,
  //   slice_labels=nexus--md-preview,
  //   kind=ast,
  //   comment=After mapImages and before sheets: each live folder/glob line the host has listed (lookup(path) → {files: [{path, width?, height?}], error?}) becomes one image per file, in the host's order, each mapped through mapSrc and captioned by its file name (fileCaption), the line's alt text kept as data-glimpse-set (collect makes it the group) and its title as theirs; an empty listing or an error stays a labelled tile (data-glimpse-live-state empty|error); a line not listed yet waits. The module fetches nothing.,
  // ]
  function expandLive(root, lookup, mapSrc) {
    const doc = root?.ownerDocument || globalThis.document;
    let made = 0;
    for (const img of [...(root?.querySelectorAll?.('img[data-glimpse-live]') || [])]) {
      if (img.hasAttribute('data-glimpse-live-state')) continue;
      const path = img.getAttribute('data-glimpse-path'), listing = typeof lookup === 'function' ? lookup(path) : null;
      if (!listing) continue;  // not listed yet: its labelled tile waits
      if (listing.error) { img.setAttribute('data-glimpse-live-state', 'error'); img.setAttribute('data-glimpse-live-error', String(listing.error)); continue; }
      const files = (Array.isArray(listing.files) ? listing.files : []).filter(file => file && typeof file.path === 'string' && file.path);
      if (!files.length) { img.setAttribute('data-glimpse-live-state', 'empty'); continue; }
      const set = (img.getAttribute('alt') || '').replace(/\s+/g, ' ').trim(), title = img.getAttribute('title');
      img.replaceWith(...files.map(file => {
        const one = doc.createElement('img');
        one.setAttribute('alt', fileCaption(file.path));
        if (title) one.setAttribute('title', title);
        one.setAttribute('data-glimpse-src', file.path);
        one.setAttribute('data-glimpse-path', file.path);
        one.setAttribute('data-glimpse-from', path);
        if (set) one.setAttribute('data-glimpse-set', set);
        one.setAttribute('loading', 'lazy');
        one.setAttribute('decoding', 'async');
        if (file.width > 0 && file.height > 0) { one.setAttribute('width', String(file.width)); one.setAttribute('height', String(file.height)); }
        const url = typeof mapSrc === 'function' ? mapSrc(file.path, file.path) : null;
        if (url) one.setAttribute('src', url);
        made++;
        return one;
      }));
    }
    return made;
  }
  // The images of one paragraph made only of images (and line breaks, and links around single images), else null.
  function imageRun(p) {
    const images = [];
    for (const node of p.childNodes) {
      if (node.nodeType === 3) { if (node.textContent.trim()) return null; continue; }
      if (node.nodeType !== 1) continue;
      if (node.tagName === 'IMG') images.push(node);
      else if (node.tagName === 'A' && node.children.length === 1 && node.children[0].tagName === 'IMG' && !node.textContent.trim()) images.push(node.children[0]);
      else if (node.tagName !== 'BR') return null;
    }
    return images;
  }
  function tileFor(doc, img) {
    const figure = doc.createElement('figure'), caption = doc.createElement('figcaption');
    figure.className = 'glimpse-lb-tile';
    const live = img.getAttribute('data-glimpse-live'), path = img.getAttribute('data-glimpse-path') || img.getAttribute('data-glimpse-src') || '';
    caption.textContent = img.getAttribute('alt') || fileName(path);
    if (img.getAttribute('title')) caption.title = img.getAttribute('title');
    figure.append(img, caption);
    if (live) {
      // A live line its host has not listed (yet), an empty folder or glob, or why it could not be listed
      const state = img.getAttribute('data-glimpse-live-state'), error = img.getAttribute('data-glimpse-live-error') || '';
      figure.setAttribute('data-live', live);
      if (state) figure.setAttribute('data-live-state', state);
      const note = doc.createElement('span');
      note.className = 'glimpse-lb-note';
      note.textContent = state === 'error' ? '⚠ ' + error + (error.includes(path) ? '' : ' · ' + path)
        : state === 'empty' ? (live === 'folder' ? '📁 no images in this folder · ' : '🔎 no image matches · ') + path
        : (live === 'folder' ? '📁 folder · ' : '🔎 files matching · ') + path;
      figure.insertBefore(note, caption);
    }
    return figure;
  }
  // @beacon[
  //   id=md-lightbox@sheets,
  //   slice_labels=nexus--md-preview,
  //   kind=ast,
  //   comment=Turns every paragraph made only of two or more images into a contact sheet: a grid of tiles (div.glimpse-lb-sheet > figure.glimpse-lb-tile), each image with its caption (the alt text, else the file name) under it; a live folder/glob line's paragraph is a sheet even for one file, and a line not expanded (not listed yet, empty, or why it could not be listed) becomes a labelled tile; other single images and images inside prose stay inline.,
  // ]
  function sheets(root) {
    const doc = root?.ownerDocument || globalThis.document;
    let made = 0;
    for (const p of [...(root?.querySelectorAll?.('p') || [])]) {
      const images = imageRun(p);
      // a live line is a set even when it holds one file (or none): always tiles
      if (!images || images.length < 2 && !images.some(i => i.hasAttribute('data-glimpse-live') || i.hasAttribute('data-glimpse-from'))) continue;
      const sheet = doc.createElement('div');
      sheet.className = 'glimpse-lb-sheet';
      for (const img of images) sheet.append(tileFor(doc, img));
      p.replaceWith(sheet);
      made++;
    }
    return made;
  }
  // @beacon[
  //   id=md-lightbox@collect,
  //   slice_labels=nexus--md-preview,
  //   kind=ast,
  //   comment=Collects every shown image of a (mapped) document into groups by heading, in document order (groupImages): each record {src (as written), path, mapped (the URL shown), alt, title, file, element}; each image element gets data-glimpse-lb, its index in the one sequence; the images an expanded live line with alt text supplied are a group of their own named by that text (inside the heading around it, which the group's heading keeps) until the next heading; a live line not expanded is left out.,
  // ]
  function collect(root, options) {
    const sequence = [];
    let heading = '', set = null;
    for (const node of root?.querySelectorAll?.('h1,h2,h3,h4,h5,h6,img') || []) {
      if (HEADING.test(node.tagName)) { heading = node.textContent || ''; set = null; sequence.push({heading}); continue; }
      if (node.hasAttribute('data-glimpse-live')) continue;
      const shown = node.getAttribute('src');
      if (!shown) continue;
      const from = node.getAttribute('data-glimpse-set');
      if (from && from !== set) {
        set = from;
        const around = heading.replace(/\s+/g, ' ').trim();
        sequence.push({heading: !around || groupLabel(around) === from ? from : around + ' › ' + from, label: from});
      }
      const src = node.getAttribute('data-glimpse-src') || shown, path = node.getAttribute('data-glimpse-path') || null;
      sequence.push({image: {src, path, mapped: shown, alt: node.getAttribute('alt') || '', title: node.getAttribute('title') || '',
        file: fileName(path || src), element: node}});
    }
    const groups = groupImages(sequence, options?.title);
    for (const group of groups) for (const image of group.images) image.element.setAttribute('data-glimpse-lb', String(image.index));
    return groups;
  }
  // The document's top-level blocks as isImageOnly reads them. An estate's entry starts at the daemon's heading for it:
  // "📄 [N] …", or "⚠️ [N] … — ENTRY FAILED".
  const ENTRY = /^(?:📄|⚠️?)\s*\[\d+\]\s/u;
  function blocks(root) {
    const out = [];
    for (const node of root?.children || []) {
      const text = (node.textContent || '').trim();
      if (node.tagName === 'H1' && ENTRY.test(text)) out.push({kind: 'entry', failed: !text.startsWith('📄')});
      else if (HEADING.test(node.tagName)) out.push({kind: 'heading'});
      // a live line's tile (not listed yet, empty, or not listable) shows no image
      else if (node.classList?.contains('glimpse-lb-sheet')) out.push({kind: 'images', count: node.querySelectorAll('img:not([data-glimpse-live])').length});
      else if (node.tagName === 'HR' || node.tagName === 'BLOCKQUOTE' && /^render_scope:/.test(text)) out.push({kind: 'meta'});
      else if (node.tagName === 'P' && !text && !node.querySelector('img')) out.push({kind: 'meta'});
      else {
        const images = node.querySelectorAll ? node.querySelectorAll('img').length : 0;
        if (images && node.tagName === 'P' && text.length <= PROSE_MAX) out.push({kind: 'images', count: images}, ...(text ? [{kind: 'prose', chars: text.length}] : []));
        else out.push({kind: 'prose', chars: text.length + (images ? PROSE_MAX + 1 : 0)});
      }
    }
    return out;
  }
  function imageOnly(root) { return isImageOnly(blocks(root)); }
  // A tile (or inline image) whose file is missing or unreadable: dimmed, with its path, never a broken icon.
  function markMissing(img, why) {
    if (!img || img.getAttribute('data-glimpse-missing')) return;
    img.setAttribute('data-glimpse-missing', why || 'missing');
    const figure = img.closest?.('.glimpse-lb-tile');
    if (!figure) return;
    figure.setAttribute('data-missing', 'true');
    const note = (img.ownerDocument || document).createElement('span');
    note.className = 'glimpse-lb-note';
    note.textContent = '⚠ ' + (why || 'missing') + ' · ' + (img.getAttribute('data-glimpse-path') || img.getAttribute('data-glimpse-src') || '');
    figure.insertBefore(note, figure.querySelector('figcaption'));
  }
  // Sizes and existence from the host (lookup(path) → {exists, width, height, error}): width/height reserve each
  // image's room before it loads; a missing file becomes a missing tile.
  function applySizes(root, lookup) {
    for (const img of root?.querySelectorAll?.('img[data-glimpse-path]') || []) {
      const info = typeof lookup === 'function' ? lookup(img.getAttribute('data-glimpse-path')) : null;
      if (!info) continue;
      if (info.exists === false) { markMissing(img, info.error || 'no such file'); continue; }
      if (info.width > 0 && info.height > 0 && !img.hasAttribute('width') && !img.hasAttribute('height')) {
        img.setAttribute('width', String(info.width)); img.setAttribute('height', String(info.height));
      }
    }
  }
  // @beacon[
  //   id=md-lightbox@wire,
  //   slice_labels=nexus--md-preview,
  //   kind=ast,
  //   comment=Wires one mounted document container once: a plain click or tap on a collected image (or its tile) calls onOpen(index, element) instead of following a link around it; an image that fails to load becomes a missing tile (capturing the error event, which does not bubble).,
  // ]
  function wire(container, options) {
    if (!container?.addEventListener || container.glimpseLightboxWired) return;
    container.glimpseLightboxWired = true;
    container.addEventListener('click', event => {
      if (event.defaultPrevented || event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const tile = event.target?.closest?.('.glimpse-lb-tile'), img = tile ? tile.querySelector('img[data-glimpse-lb]') : event.target?.closest?.('img[data-glimpse-lb]');
      if (!img || !container.contains(img) || img.getAttribute('data-glimpse-missing')) return;
      event.preventDefault();
      options?.onOpen?.(Number(img.getAttribute('data-glimpse-lb')), img);
    });
    container.addEventListener('error', event => {
      if (event.target?.tagName === 'IMG' && event.target.hasAttribute('data-glimpse-src')) markMissing(event.target, 'could not load');
    }, true);
  }

  // ---- the lightbox (PhotoSwipe 5)

  let backing = null;  // the history.back() of a lightbox (or side by side) just closed, until its popstate arrives
  // One history entry while an overlay (the lightbox, side by side) is open, so Back closes it: key names it in the
  // history state (a lightbox opened over side by side keeps both names); onBack runs when Back took it; release() takes
  // it back when the overlay closed some other way. One opened while an earlier one's Back is still on its way adds its
  // entry once that Back has arrived (else that Back would close it).
  function backEntry(key, onBack) {
    const win = globalThis, token = key + '-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
    let pushed = false, done = false;
    const onPop = () => { if (win.history?.state?.[key] !== token) { pushed = false; win.removeEventListener('popstate', onPop); onBack(); } };
    const add = () => {
      if (done) return;
      try { win.history.pushState({...(win.history.state || {}), [key]: token}, ''); pushed = true; win.addEventListener('popstate', onPop); }
      catch (_) { /* a document without session history: Esc and ✕ (and the lightbox's downward drag) still close it */ }
    };
    if (backing) backing.then(add); else add();
    return {release() {
      done = true;
      win.removeEventListener('popstate', onPop);
      if (!pushed || win.history?.state?.[key] !== token) return;
      pushed = false;
      const mine = new Promise(resolve => {
        const finish = () => { win.removeEventListener('popstate', finish); clearTimeout(timer); if (backing === mine) backing = null; resolve(); };
        const timer = setTimeout(finish, 700);
        win.addEventListener('popstate', finish);
      });
      backing = mine;
      win.history.back();
    }};
  }
  const ICON = {
    full: '<svg aria-hidden="true" class="pswp__icn" viewBox="0 0 32 32" width="32" height="32"><path d="M8 13V8h5M19 8h5v5M24 19v5h-5M13 24H8v-5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    actual: '<span class="glimpse-lb-actual-text" aria-hidden="true">1:1</span>'
  };
  // @beacon[
  //   id=md-lightbox@open,
  //   slice_labels=nexus--md-preview,
  //   kind=ast,
  //   comment=Opens the full-screen lightbox over the page on the collected groups as one sequence (PhotoSwipe 5: swipe, pinch to about 4x, double-tap zoom, pan, drag down to close, preloading ±2); top bar: position within the group, group chips with counts (a tap jumps to the group), 1:1, ⛶ where the Fullscreen API is allowed, zoom and close; bottom: caption, title, file name and heading, and the group's filmstrip. Keys beyond PhotoSwipe's: PageUp/Down and Up/Down (unzoomed) groups, Home/End in the group, + − 0 1 zoom, Space chrome, F fullscreen. Sizes come from options.sizes(image) or the loaded image; an unknown size is measured once and the slide refreshed; a missing file is its own slide. Pushes one history entry so Back closes it; onImmersive(true/false) around it; onClose(lastIndex). Returns a controller, or null without PhotoSwipe or images.,
  // ]
  function open(groups, options) {
    const PhotoSwipe = globalThis.PhotoSwipe, opts = options || {};
    const items = [];
    (groups || []).forEach((group, g) => group.images.forEach((image, k) => items.push({image, group: g, k})));
    if (typeof PhotoSwipe !== 'function' || !items.length) return null;
    const doc = globalThis.document, win = globalThis;
    const sizes = typeof opts.sizes === 'function' ? opts.sizes : () => null;
    const measured = new Map(), waiting = new Set();
    const touch = !!win.matchMedia?.('(pointer: coarse)').matches;
    const still = !!win.matchMedia?.('(prefers-reduced-motion: reduce)').matches;  // no fade or zoom animation then
    const ui = {};
    let closed = false, fullscreenEntered = false, lastGroup = -1, index = Math.max(0, Math.min(items.length - 1, Number(opts.start) || 0));
    let pswp = null;
    const chromeShown = () => !!pswp?.element?.classList.contains('pswp--ui-visible');

    function natural(element) {
      return element?.complete && element.naturalWidth > 0 ? {width: element.naturalWidth, height: element.naturalHeight} : null;
    }
    function measure(i) {
      if (measured.has(i)) return;
      measured.set(i, null);
      const probe = new win.Image();
      probe.decoding = 'async';
      probe.onload = () => { measured.set(i, {width: probe.naturalWidth, height: probe.naturalHeight}); if (!closed) pswp.refreshSlideContent(i); };
      probe.onerror = () => { measured.set(i, {failed: true}); if (!closed) pswp.refreshSlideContent(i); };
      probe.src = items[i].image.mapped;
    }
    function gone(image, why) {
      const what = String(why || '').replace(/^./, c => c.toUpperCase());
      return '<div class="glimpse-lb-gone"><div class="glimpse-lb-gone-what">⚠ ' + esc(what) + '</div><code>' + esc(image.path || image.src) + '</code></div>';
    }
    function itemData(i) {
      const {image} = items[i], info = sizes(image) || {};
      if (info.exists === false) return {html: gone(image, info.error || 'No such file'), alt: image.alt};
      let size = info.width > 0 && info.height > 0 ? info : measured.get(i) || natural(image.element);
      if (size?.failed) return {html: gone(image, 'This image could not be loaded'), alt: image.alt};
      if (!(size?.width > 0 && size?.height > 0)) { waiting.add(i); measure(i); return {html: '<div class="glimpse-lb-wait">Loading…</div>', alt: image.alt}; }
      waiting.delete(i);
      return {src: image.mapped, width: size.width, height: size.height, alt: image.alt || image.file};
    }
    // The chrome's room (the bar above, the caption and filmstrip below) while it shows, from the first fit on; none
    // while a tap or Space has hidden it.
    let uiShown = true;
    function padding() {
      if (!uiShown) return {top: 0, bottom: 0, left: 0, right: 0};
      return {top: pswp?.topBar?.offsetHeight || 60, bottom: ui.footHeight || 104, left: 0, right: 0};
    }
    pswp = new PhotoSwipe({
      dataSource: items, index, mainClass: 'glimpse-lb', bgOpacity: 1, showHideAnimationType: still ? 'none' : 'fade', loop: false,
      ...(still ? {zoomAnimationDuration: 0} : {}),
      preload: [2, 2], wheelToZoom: !touch, imageClickAction: 'zoom', tapAction: 'toggle-controls', doubleTapAction: 'zoom',
      bgClickAction: 'close', counter: false, paddingFn: padding, errorMsg: 'This image could not be loaded',
      closeTitle: 'Close (Esc)', zoomTitle: 'Zoom (Z · + − · 0 fit · 1 actual pixels)', arrowPrevTitle: 'Previous (←)', arrowNextTitle: 'Next (→)'
    });
    pswp.addFilter('itemData', (_data, i) => itemData(i));

    const groupOf = i => items[i]?.group ?? 0;
    const firstOf = g => items.findIndex(item => item.group === g);
    const lastOf = g => { for (let i = items.length - 1; i >= 0; i--) if (items[i].group === g) return i; return -1; };
    function go(i) { if (i >= 0 && i < items.length && i !== pswp.currIndex) pswp.goTo(i); }
    function zoomTo(level) {
      const slide = pswp.currSlide;
      if (!slide?.isZoomable()) return;
      const {initial, max} = slide.zoomLevels;
      slide.zoomTo(Math.max(initial, Math.min(Math.max(max, 1), level)), pswp.getViewportCenterPoint(), still ? 0 : 200);
    }
    function toggleFullscreen() {
      if (doc.fullscreenElement) { doc.exitFullscreen?.().catch?.(() => {}); return; }
      const target = opts.fullscreenElement || doc.documentElement;
      target.requestFullscreen?.().then(() => { fullscreenEntered = true; }, () => {});
    }
    function act(action) {
      const slide = pswp.currSlide, g = groupOf(pswp.currIndex);
      switch (action) {
        case 'next-group': go(firstOf(Math.min(groups.length - 1, g + 1))); break;
        case 'prev-group': go(firstOf(Math.max(0, g - 1))); break;
        case 'group-first': go(firstOf(g)); break;
        case 'group-last': go(lastOf(g)); break;
        case 'zoom-in': if (slide) zoomTo(slide.currZoomLevel * 1.5); break;
        case 'zoom-out': if (slide) zoomTo(slide.currZoomLevel / 1.5); break;
        case 'zoom-fit': if (slide) zoomTo(slide.zoomLevels.initial); break;
        case 'zoom-actual': zoomTo(1); break;
        case 'chrome': pswp.element?.classList.toggle('pswp--ui-visible'); break;
        case 'fullscreen': if (doc.fullscreenEnabled) toggleFullscreen(); break;
        default: return false;
      }
      return true;
    }
    pswp.on('keydown', event => {
      const key = event.originalEvent;
      if (key.ctrlKey || key.metaKey || key.altKey) return;
      const slide = pswp.currSlide, zoomed = !!slide && slide.currZoomLevel > slide.zoomLevels.initial + 1e-3;
      if (act(keyAction(key.key, zoomed))) { event.preventDefault(); key.preventDefault(); }
    });
    // Our own chrome never starts a swipe: a drag along the chips or the filmstrip scrolls them.
    const own = element => { element.addEventListener('pointerdown', event => event.stopPropagation()); return element; };

    function paintChips() {
      ui.chips.replaceChildren(...groups.map((group, g) => {
        const chip = doc.createElement('button');
        chip.type = 'button'; chip.className = 'glimpse-lb-chip';
        chip.title = group.heading || group.label;
        chip.innerHTML = esc(group.label) + ' <span class="glimpse-lb-chip-n">' + group.images.length + '</span>';
        chip.addEventListener('click', () => go(firstOf(g)));
        return chip;
      }));
      ui.chips.hidden = groups.length < 2;
    }
    function paintStrip(g) {
      ui.strip.replaceChildren(...items.map((item, i) => [item, i]).filter(([item]) => item.group === g).map(([item, i]) => {
        const thumb = doc.createElement('button'), img = doc.createElement('img');
        thumb.type = 'button'; thumb.className = 'glimpse-lb-thumb'; thumb.dataset.index = String(i);
        thumb.title = item.image.alt || item.image.file;
        img.alt = ''; img.loading = 'lazy'; img.decoding = 'async'; img.src = item.image.mapped;
        thumb.append(img);
        thumb.addEventListener('click', () => go(i));
        return thumb;
      }));
      ui.strip.hidden = ui.strip.children.length < 2;
    }
    function paintCurrent() {
      const i = pswp.currIndex, item = items[i];
      if (!item || !ui.pos || !ui.foot) return;  // the chrome is built during init
      index = i;
      const group = groups[item.group];
      if (item.group !== lastGroup) { lastGroup = item.group; paintStrip(item.group); }
      ui.pos.textContent = (item.k + 1) + ' / ' + group.images.length;
      ui.pos.title = 'Image ' + (i + 1) + ' of ' + items.length + ' in all';
      [...ui.chips.children].forEach((chip, g) => chip.setAttribute('aria-current', String(g === item.group)));
      ui.chips.children[item.group]?.scrollIntoView?.({block: 'nearest', inline: 'nearest'});
      const {image} = item;
      ui.caption.textContent = image.alt || image.file;
      ui.title.textContent = image.title;
      ui.title.hidden = !image.title;
      ui.file.textContent = image.file + (group.heading ? ' · ' + group.heading : '');
      for (const thumb of ui.strip.children) {
        const on = Number(thumb.dataset.index) === i;
        thumb.setAttribute('aria-current', String(on));
        if (on) {
          const left = thumb.offsetLeft - (ui.strip.clientWidth - thumb.offsetWidth) / 2;
          ui.strip.scrollLeft = Math.max(0, left);
        }
      }
      // The foot as painted (a title line, a filmstrip or none) is the room the image leaves free below: refit on a change.
      const footHeight = ui.foot.offsetHeight;
      if (footHeight && footHeight !== ui.footHeight) { ui.footHeight = footHeight; if (uiShown) pswp.updateSize(true); }
    }
    pswp.on('uiRegister', () => {
      pswp.ui.registerElement({name: 'glimpse-pos', order: 5, onInit: el => { ui.pos = el; el.className = 'pswp__counter glimpse-lb-pos'; }});
      pswp.ui.registerElement({name: 'glimpse-chips', order: 6, onInit: el => { ui.chips = own(el); el.className = 'glimpse-lb-chips'; paintChips(); }});
      pswp.ui.registerElement({name: 'glimpse-actual', order: 11, isButton: true, title: 'Actual pixels (1) · fit (0)', html: ICON.actual,
        onClick: () => act('zoom-actual')});
      if (doc.fullscreenEnabled) pswp.ui.registerElement({name: 'glimpse-full', order: 12, isButton: true, title: 'Full screen (F)', html: ICON.full,
        onClick: () => act('fullscreen')});
      pswp.ui.registerElement({name: 'glimpse-foot', order: 30, appendTo: 'root', onInit: el => {
        ui.foot = el; el.className = 'glimpse-lb-foot pswp__hide-on-close';
        const text = doc.createElement('div'); text.className = 'glimpse-lb-text';
        ui.caption = doc.createElement('div'); ui.caption.className = 'glimpse-lb-caption';
        ui.title = doc.createElement('div'); ui.title.className = 'glimpse-lb-title';
        ui.file = doc.createElement('div'); ui.file.className = 'glimpse-lb-file';
        text.append(ui.caption, ui.title, ui.file);
        ui.strip = own(doc.createElement('div')); ui.strip.className = 'glimpse-lb-strip';
        el.append(text, ui.strip);
      }});
    });
    pswp.on('change', paintCurrent);
    pswp.on('afterInit', paintCurrent);
    // Slides are resized only once the lightbox is open: then the fit takes the painted chrome's room.
    pswp.on('openingAnimationEnd', () => { if (ui.foot) ui.footHeight = ui.foot.offsetHeight || ui.footHeight; pswp.updateSize(true); });
    // Showing or hiding the chrome (a tap, Space) gives the image the room back: the fit is recomputed.
    let observer = null;
    pswp.on('afterInit', () => {
      if (!win.MutationObserver || !pswp.element) return;
      observer = new win.MutationObserver(() => {
        if (closed || pswp.opener?.isClosing || !pswp.opener?.isOpen) return;  // the chrome fading in or out with the lightbox
        const now = chromeShown();
        if (now !== uiShown) { uiShown = now; pswp.updateSize(true); }
      });
      observer.observe(pswp.element, {attributes: true, attributeFilter: ['class']});
    });

    // Back closes it: one history entry while it is open; closing it any other way takes that entry back (backEntry).
    const entry = opts.history !== false ? backEntry('glimpseLightbox', () => pswp.close()) : null;
    pswp.on('destroy', () => {
      closed = true;
      observer?.disconnect();
      entry?.release();
      if (fullscreenEntered && doc.fullscreenElement) doc.exitFullscreen?.().catch?.(() => {});
      opts.onImmersive?.(false);
      opts.onClose?.(index, items[index]?.image);
    });
    opts.onImmersive?.(true);
    pswp.init();
    return {
      pswp, items,
      get index() { return index; },
      close: () => { if (!closed) pswp.close(); },
      goTo: go,
      // New sizes from the host (after the lightbox opened): the slides still waiting for theirs are rebuilt.
      refresh: () => { if (closed) return; for (const i of [...waiting]) { measured.delete(i); pswp.refreshSlideContent(i); } },
      isOpen: () => !closed
    };
  }

  // ---- side by side: a simple viewer of its own beside PhotoSwipe (which shows one image at a time)
  const ZOOM_MAX = 8;  // side by side zooms to 8× actual pixels at most
  // @beacon[
  //   id=md-lightbox@compare,
  //   slice_labels=nexus--md-preview,
  //   kind=ast,
  //   comment=Opens the side-by-side viewer over the page for an A/B or A/B/C comparison of the collected groups (a wide screen's; the host decides where to offer it): two or three panels (options.picks, else the first two or three groups; a group chip swaps one in or out), each with its group, position and nudge ‹ › in its head and the image's caption, title and file name under a fitted image; the panels step together by position (compareStep: ← → Home End and the bar's ‹ ›), a nudge moves one panel, A or Line up drops the nudges; one shared zoom (+ − 0 fit, 1 actual pixels, the wheel at the pointer) and pan (drag) keep same-size screenshots aligned; a click on a panel's image opens it in the ordinary lightbox (options.onOpen(index), else open), whose keys then win; Space hides the chrome, F fullscreen, Esc ✕ and Back close it (its own history entry); sizes from options.sizes(image) or the loaded image; neighbours preloaded. Returns a controller, or null without a document or groups.,
  // ]
  function compare(groups, options) {
    const doc = globalThis.document, win = globalThis, opts = options || {};
    const sets = Array.isArray(groups) ? groups : [];
    if (!doc?.body || !sets.some(g => g?.images?.length)) return null;
    const sizes = typeof opts.sizes === 'function' ? opts.sizes : () => null;
    const known = new Set(sets.map((_, g) => g));
    let picks = Array.isArray(opts.picks) ? [...new Set(opts.picks.filter(g => known.has(g)))].sort((a, b) => a - b).slice(0, COMPARE_MAX) : [];
    if (!picks.length) picks = comparePicks(sets.length);
    const nudges = new Map();  // a group's nudge, kept while its panel is swapped out
    let model = {lengths: picks.map(g => sets[g].images.length), offsets: picks.map(() => 0), position: Math.max(0, Number(opts.start) || 0)};
    model = {...model, position: Math.min(compareRange(model).hi, model.position)};
    const view = {zoom: null, cx: 0.5, cy: 0.5};
    let closed = false, fullscreenEntered = false, panels = [];
    const make = (tag, className, text) => { const el = doc.createElement(tag); if (className) el.className = className; if (text != null) el.textContent = text; return el; };
    const root = make('div', 'glimpse-cmp');
    root.tabIndex = -1;
    root.setAttribute('role', 'dialog'); root.setAttribute('aria-modal', 'true'); root.setAttribute('aria-label', 'Side by side');
    // A button acts, then gives the keys back to the viewer (Space never presses it twice).
    const button = (text, title, onClick, className = '') => {
      const b = make('button', 'glimpse-cmp-btn' + (className ? ' ' + className : ''), text);
      b.type = 'button'; b.title = title;
      b.addEventListener('click', event => { event.preventDefault(); onClick(event); root.focus?.({preventScroll: true}); });
      return b;
    };

    // The bar: the group chips (lit with their panel's number), the shared step, Line up, zoom, fullscreen, close
    const bar = make('div', 'glimpse-cmp-bar');
    const chips = make('div', 'glimpse-cmp-chips');
    const chipButtons = sets.map((group, g) => {
      const chip = button('', (group.heading || group.label) + ' · tap to show or hide it (three at most: another then takes the right-most panel\'s place)', () => pickGroup(g), 'glimpse-cmp-chip');
      chip.dataset.group = String(g);
      return chip;
    });
    chips.append(...chipButtons);
    const prev = button('‹', 'Every panel back one image (←)', () => act('prev'));
    const stepPos = make('span', 'glimpse-cmp-steppos');
    const next = button('›', 'Every panel on one image (→)', () => act('next'));
    const step = make('div', 'glimpse-cmp-step'); step.append(prev, stepPos, next);
    const align = button('⟲ Line up', 'Drop every panel\'s nudge: they step by position again (A)', () => act('align'));
    const zoomOut = button('−', 'Zoom out, every panel (−)', () => act('zoom-out'));
    const fit = button('Fit', 'Fit every image to its panel (0)', () => act('zoom-fit'));
    const actual = button('1:1', 'Actual pixels, every panel, aligned (1)', () => act('zoom-actual'));
    const zoomIn = button('+', 'Zoom in, every panel (+; or the wheel)', () => act('zoom-in'));
    const full = doc.fullscreenEnabled ? button('⛶', 'Full screen (F)', () => act('fullscreen')) : null;
    const shut = button('✕', 'Close (Esc)', () => close(), 'glimpse-cmp-close');
    bar.append(make('span', 'glimpse-cmp-title', '⧉ Side by side'), chips, step, align, zoomOut, fit, actual, zoomIn, ...(full ? [full] : []), shut);
    const box = make('div', 'glimpse-cmp-panels');
    root.append(bar, box);

    const shownImage = panel => { const i = compareShown(model)[panel.k]; return i >= 0 ? {i, image: sets[panel.g].images[i]} : {i, image: null}; };
    function natural(panel, image) {
      const info = sizes(image) || {};
      if (info.width > 0 && info.height > 0) return {width: info.width, height: info.height};
      const img = panel.img;
      return img.complete && img.naturalWidth > 0 && img.getAttribute('src') === image.mapped ? {width: img.naturalWidth, height: img.naturalHeight} : null;
    }
    function layout(panel) {
      const {image} = shownImage(panel);
      panel.geometry = null;
      if (!image || panel.img.hidden) return;
      const size = natural(panel, image), rect = {width: panel.view.clientWidth, height: panel.view.clientHeight};
      const g = size ? compareGeometry(rect, size, view.zoom, view.cx, view.cy) : null;
      panel.img.style.visibility = g ? '' : 'hidden';  // its size comes with the image: until then, nothing at a wrong size
      if (!g) return;
      panel.geometry = {...g, natural: size, view: rect};
      Object.assign(panel.img.style, {width: g.width + 'px', height: g.height + 'px', transform: 'translate(' + g.left + 'px, ' + g.top + 'px)'});
    }
    const layoutAll = () => { if (closed) return; for (const panel of panels) layout(panel); root.dataset.zoomed = String(view.zoom != null); };
    // The smallest fit among the panels: zooming out to it (or below) is back to fit.
    const minFit = () => Math.min(...panels.map(p => p.geometry?.fit).filter(f => f > 0), Infinity);
    function gone(panel, what, detail) {
      panel.img.hidden = true; panel.gone.hidden = false;
      panel.gone.replaceChildren(make('div', 'glimpse-cmp-gone-what', what), ...(detail ? [make('code', '', detail)] : []));
    }
    function paintPanel(panel) {
      const {i, image} = shownImage(panel), group = sets[panel.g], n = group.images.length, o = model.offsets[panel.k] || 0;
      panel.pos.textContent = (i >= 0 ? i + 1 : '–') + ' / ' + n;
      panel.off.textContent = o ? (o > 0 ? '+' : '−') + Math.abs(o) : '';
      panel.off.title = o ? 'Nudged ' + Math.abs(o) + (o > 0 ? ' on' : ' back') + ' from the others (Line up drops it)' : '';
      panel.back.disabled = i === 0; panel.fwd.disabled = i === n - 1;
      panel.section.dataset.index = String(i);
      if (!image) {
        panel.img.removeAttribute('src');
        gone(panel, '—', (model.position + o < 0 ? 'before the first of ' : 'past the last of ') + group.label + '\'s ' + n + (n === 1 ? ' image' : ' images'));
        panel.caption.textContent = ''; panel.titleLine.textContent = ''; panel.titleLine.hidden = true; panel.file.textContent = '';
        panel.geometry = null;
        return;
      }
      panel.caption.textContent = image.alt || image.file;
      panel.titleLine.textContent = image.title || ''; panel.titleLine.hidden = !image.title;
      panel.file.textContent = image.file + (group.heading ? ' · ' + group.heading : '');
      panel.file.title = image.path || image.src || '';
      const info = sizes(image) || {};
      if (info.exists === false) { panel.img.removeAttribute('src'); gone(panel, '⚠ ' + String(info.error || 'no such file').replace(/^./, c => c.toUpperCase()), image.path || image.src); return; }
      if (panel.failed === image.mapped) { gone(panel, '⚠ This image could not be loaded', image.path || image.src); return; }
      panel.gone.hidden = true; panel.img.hidden = false;
      panel.img.alt = image.alt || image.file;
      if (panel.img.getAttribute('src') !== image.mapped) { panel.img.style.visibility = 'hidden'; panel.img.src = image.mapped; }
      layout(panel);
    }
    const warm = new Map();  // the neighbours' images, loading ahead of a step or a nudge
    function preload() {
      const want = new Set();
      for (const panel of panels) {
        const i = compareShown(model)[panel.k] >= 0 ? compareShown(model)[panel.k] : model.position + (model.offsets[panel.k] || 0);
        for (const j of [i - 1, i + 1]) { const image = sets[panel.g].images[j]; if (image?.mapped) want.add(image.mapped); }
      }
      for (const url of want) if (!warm.has(url)) { const probe = new win.Image(); probe.decoding = 'async'; probe.src = url; warm.set(url, probe); }
      if (warm.size > 48) for (const url of [...warm.keys()]) { if (warm.size <= 24) break; if (!want.has(url)) warm.delete(url); }
    }
    function paint() {
      if (closed) return;
      const {lo, hi} = compareRange(model);
      stepPos.textContent = (model.position - lo + 1) + ' / ' + (hi - lo + 1);
      stepPos.title = 'Step ' + (model.position - lo + 1) + ' of ' + (hi - lo + 1) + ': each panel shows its own image there';
      prev.disabled = model.position <= lo; next.disabled = model.position >= hi;
      align.hidden = !model.offsets.some(Boolean);
      chipButtons.forEach((chip, g) => {
        const k = picks.indexOf(g);
        chip.setAttribute('aria-pressed', String(k >= 0));
        chip.replaceChildren(...(k >= 0 ? [make('span', 'glimpse-cmp-chip-k', String(k + 1))] : []), doc.createTextNode(sets[g].label + ' '), make('span', 'glimpse-cmp-chip-n', String(sets[g].images.length)));
      });
      for (const panel of panels) paintPanel(panel);
      root.dataset.zoomed = String(view.zoom != null);
      preload();
    }

    // A panel: a drag pans every panel together (when the image is larger than its panel), the wheel zooms them all at
    // the pointer, a click (no drag) opens that image in the ordinary lightbox.
    function centreRange(g, axis) {
      const V = axis === 'x' ? g.view.width : g.view.height, d = axis === 'x' ? g.width : g.height;
      return d <= V ? [0.5, 0.5] : [V / 2 / d, 1 - V / 2 / d];
    }
    const clampTo = (value, [a, b]) => Math.max(a, Math.min(b, value));
    function zoomAt(panel, factor, clientX, clientY) {
      const g = panel.geometry;
      if (!g) return;
      const rect = panel.view.getBoundingClientRect(), mx = clientX - rect.left, my = clientY - rect.top;
      const px = (mx - g.left) / g.width, py = (my - g.top) / g.height, floor = minFit();
      const to = Math.min(ZOOM_MAX, g.scale * factor);
      if (factor < 1 && to <= floor * 1.001) { view.zoom = null; view.cx = view.cy = 0.5; layoutAll(); return; }
      const dw = g.natural.width * to, dh = g.natural.height * to;
      view.zoom = to;
      const after = {view: g.view, width: dw, height: dh};
      view.cx = clampTo((g.view.width / 2 - mx) / dw + px, centreRange(after, 'x'));
      view.cy = clampTo((g.view.height / 2 - my) / dh + py, centreRange(after, 'y'));
      layoutAll();
    }
    function wirePanel(panel) {
      let drag = null;
      panel.view.addEventListener('pointerdown', event => {
        if (event.button) return;
        event.preventDefault();
        try { panel.view.setPointerCapture?.(event.pointerId); } catch (_) { /* the drag still works over the panel */ }
        drag = {id: event.pointerId, x: event.clientX, y: event.clientY, cx: view.cx, cy: view.cy, moved: false};
      });
      panel.view.addEventListener('pointermove', event => {
        if (!drag || drag.id !== event.pointerId) return;
        const dx = event.clientX - drag.x, dy = event.clientY - drag.y, g = panel.geometry;
        if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 5) return;
        drag.moved = true; panel.view.classList.add('dragging');
        if (!g || view.zoom == null && g.width <= g.view.width && g.height <= g.view.height) return;
        view.cx = clampTo(drag.cx - dx / g.width, centreRange(g, 'x'));
        view.cy = clampTo(drag.cy - dy / g.height, centreRange(g, 'y'));
        layoutAll();
      });
      const finish = event => {
        if (!drag || drag.id !== event.pointerId) return;
        const click = !drag.moved && event.type === 'pointerup';
        drag = null; panel.view.classList.remove('dragging');
        if (click) openImage(panel);
      };
      for (const type of ['pointerup', 'pointercancel']) panel.view.addEventListener(type, finish);
      panel.view.addEventListener('wheel', event => {
        if (!panel.geometry) return;
        event.preventDefault();
        zoomAt(panel, event.deltaY < 0 ? 1.2 : 1 / 1.2, event.clientX, event.clientY);
      }, {passive: false});
      panel.img.addEventListener('load', () => { if (panel.failed === panel.img.getAttribute('src')) panel.failed = null; layout(panel); });
      panel.img.addEventListener('error', () => { if (!panel.img.getAttribute('src')) return; panel.failed = panel.img.getAttribute('src'); paintPanel(panel); });
    }
    function buildPanels() {
      panels = picks.map((g, k) => {
        const group = sets[g], panel = {g, k, failed: null, geometry: null};
        panel.section = make('section', 'glimpse-cmp-panel');
        panel.section.dataset.panel = String(k); panel.section.dataset.group = String(g);
        const head = make('div', 'glimpse-cmp-head'), name = make('span', 'glimpse-cmp-name', group.label);
        name.title = group.heading || group.label;
        panel.back = button('‹', 'Nudge this panel back one image (the others stay)', () => act('nudge-prev', k), 'glimpse-cmp-nudge');
        panel.pos = make('span', 'glimpse-cmp-pos');
        panel.fwd = button('›', 'Nudge this panel on one image (the others stay)', () => act('nudge-next', k), 'glimpse-cmp-nudge');
        panel.off = make('span', 'glimpse-cmp-off');
        head.append(name, panel.back, panel.pos, panel.fwd, panel.off);
        panel.view = make('div', 'glimpse-cmp-view');
        panel.view.title = 'Click: this image in the lightbox · drag: pan every panel · wheel: zoom every panel';
        panel.img = make('img', 'glimpse-cmp-img');
        panel.img.decoding = 'async'; panel.img.draggable = false; panel.img.alt = '';
        panel.gone = make('div', 'glimpse-cmp-gone'); panel.gone.hidden = true;
        panel.view.append(panel.img, panel.gone);
        const foot = make('div', 'glimpse-cmp-foot');
        panel.caption = make('div', 'glimpse-cmp-caption'); panel.titleLine = make('div', 'glimpse-cmp-titleline'); panel.file = make('div', 'glimpse-cmp-file');
        foot.append(panel.caption, panel.titleLine, panel.file);
        panel.section.append(head, panel.view, foot);
        wirePanel(panel);
        return panel;
      });
      box.style.setProperty('--glimpse-cmp-n', String(panels.length));
      box.replaceChildren(...panels.map(panel => panel.section));
    }
    // A chip: its group in or out (comparePick); each group keeps its nudge while it is out; the step stays in range.
    function pickGroup(g) {
      const now = comparePick(picks, g);
      if (now.join() === picks.join()) return;
      picks.forEach((x, k) => nudges.set(x, model.offsets[k] || 0));
      picks = now;
      const next = {lengths: picks.map(x => sets[x].images.length), offsets: picks.map(x => nudges.get(x) || 0), position: model.position};
      const {lo, hi} = compareRange(next);
      model = {...next, position: Math.max(lo, Math.min(hi, next.position))};
      buildPanels(); paint();
    }
    function openImage(panel) {
      const {image} = shownImage(panel);
      if (!image || panel.gone.hidden === false) return;
      if (typeof opts.onOpen === 'function') opts.onOpen(image.index, image);
      else open(sets, {sizes, start: image.index, fullscreenElement: opts.fullscreenElement});
    }
    function zoomBy(factor) {
      const base = view.zoom ?? (Number.isFinite(minFit()) ? minFit() : 1), floor = minFit();
      const to = Math.min(ZOOM_MAX, base * factor);
      view.zoom = factor < 1 && Number.isFinite(floor) && to <= floor * 1.001 ? null : to;
      if (view.zoom == null) view.cx = view.cy = 0.5;
      layoutAll();
    }
    function toggleFullscreen() {
      if (doc.fullscreenElement) { doc.exitFullscreen?.().catch?.(() => {}); return; }
      (opts.fullscreenElement || doc.documentElement).requestFullscreen?.().then(() => { fullscreenEntered = true; layoutAll(); }, () => {});
    }
    function act(action, panel) {
      switch (action) {
        case 'next': case 'prev': case 'first': case 'last': case 'align': case 'nudge-next': case 'nudge-prev':
          model = compareStep(model, action, panel); paint(); break;
        case 'zoom-in': zoomBy(1.5); break;
        case 'zoom-out': zoomBy(1 / 1.5); break;
        case 'zoom-fit': view.zoom = null; view.cx = view.cy = 0.5; layoutAll(); break;
        case 'zoom-actual': view.zoom = 1; layoutAll(); break;
        case 'chrome': root.classList.toggle('glimpse-cmp--bare'); layoutAll(); break;
        case 'fullscreen': if (doc.fullscreenEnabled) toggleFullscreen(); break;
        case 'close': close(); break;
        default: return false;
      }
      return true;
    }
    // The keys are every panel's; a lightbox opened over the viewer has them while it is open.
    const onKey = event => {
      if (closed || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || doc.querySelector('.pswp')) return;
      if (event.target?.closest?.('input, textarea, select, [contenteditable="true"]')) return;
      if (act(compareKey(event.key))) event.preventDefault();
    };
    const onResize = () => layoutAll();
    const resized = win.ResizeObserver ? new win.ResizeObserver(onResize) : null;
    let entry = null;
    function close() {
      if (closed) return;
      closed = true;
      doc.removeEventListener('keydown', onKey);
      win.removeEventListener?.('resize', onResize);
      resized?.disconnect();
      entry?.release();
      if (fullscreenEntered && doc.fullscreenElement) doc.exitFullscreen?.().catch?.(() => {});
      root.remove();
      opts.onImmersive?.(false);
      opts.onClose?.();
    }
    doc.body.append(root);
    buildPanels();
    paint();
    doc.addEventListener('keydown', onKey);
    win.addEventListener?.('resize', onResize);
    resized?.observe(box);
    root.focus?.({preventScroll: true});
    if (opts.history !== false) entry = backEntry('glimpseCompare', close);
    opts.onImmersive?.(true);
    return {
      element: root,
      get picks() { return [...picks]; },
      get model() { return {lengths: [...model.lengths], offsets: [...model.offsets], position: model.position}; },
      get view() { return {...view}; },
      // what each panel shows: its group, its image's index in the group (-1: none) and the image
      shown: () => panels.map(panel => ({group: panel.g, ...shownImage(panel)})),
      act, pick: pickGroup, close, isOpen: () => !closed
    };
  }

  globalThis.GlimpseLightbox = Object.freeze({
    localPath, liveKind, fileName, fileCaption, groupLabel, groupImages, isImageOnly, keyAction,
    comparePicks, comparePick, compareRange, compareShown, compareStep, compareKey, compareGeometry,
    mapImages, livePaths, expandLive, sheets, collect, blocks, imageOnly, markMissing, applySizes, wire, open, compare
  });
})();
