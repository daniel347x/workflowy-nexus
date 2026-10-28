// @beacon[
//   id=mdpreview@math-katex-init,
//   slice_labels=nexus--md-preview,
//   kind=span,
//   show_span=false,
//   comment=KaTeX math rendering for the docked Markdown modal (Dan, 2026): self-registering bootstrap that installs a SMALL CUSTOM marked extension (defined in THIS file -- the vendored stock marked-katex-extension was tried first, then REPLACED after a live false-positive report) onto the global marked instance at content-script load, so inline $...$ and display $$...$$ in composer strings render as math at the ONE existing choke point (composedMarkdownToHtml -> marked.parse) with ZERO call-site / server / spec / wire changes. GRAMMAR (pandoc-flavored, v2): opening $ must NOT be followed by whitespace; the content may NOT contain '$' or a newline; the content may NOT end in whitespace; the closing $ must NOT be followed by a digit (the currency guard: 'costs $5 and $10' stays plain text). The '$'-free content rule makes the reported failure IMPOSSIBLE BY CONSTRUCTION: the stock grammar REJECTED legitimate math immediately followed by ')' (its trailing lookahead admitted only [ws ?!.,:] or end, so '$0.50$)' failed), and because stock content COULD span a '$', the orphaned '$' then ran away to a distant later '$' and rendered a whole prose paragraph as red broken TeX. A match here can never cross a '$', so a misfire is bounded to one short span, never a paragraph. Also fixed vs stock: math immediately followed by ')' ';' ']' etc. now RENDERS, including parenthesized math like '($p < 0.05$)' (the stock start() required the opening '$' at start-or-after-space and missed that too). Broken TeX still degrades to red source (throwOnError:false) with a try/catch escaped-raw fallback. Block display math is '$$' on its own lines ('$$\n...\n$$', multi-line content allowed); a single-line '$$x$$' renders as inline displayMode. Load order is pinned in manifest.json: marked.min.js -> katex/katex.min.js -> THIS file -> glimpse-cache-client.js; KaTeX css + fonts are injected via the manifest (katex/katex.min.css + katex-overrides.css; fonts via web_accessible_resources). No-ops with a console.warn if marked/katex are absent, so a vendoring slip degrades to the old plain render, never a broken dock.,
// ]
/*
 * KaTeX math bootstrap for the docked Markdown modal (and every other
 * marked.parse consumer in this page context).
 *
 * The server composes Markdown STRINGS only; the browser's single
 * string->HTML step is composedMarkdownToHtml -> marked.parse. Registering
 * the math extension on the global marked instance HERE (at load) means no
 * render call site changes at all.
 *
 * The extension is a LOCAL, pandoc-flavored $-grammar (see the beacon block
 * above for why the stock marked-katex-extension was replaced). KaTeX itself
 * is vendored (katex/katex.min.js -> window.katex; css/fonts via manifest).
 */
(function () {
  'use strict';
  if (typeof window.marked === 'undefined' || typeof window.marked.use !== 'function') {
    console.warn('[GLIMPSE math] marked not loaded; math rendering disabled.');
    return;
  }
  if (typeof window.katex === 'undefined' || typeof window.katex.renderToString !== 'function') {
    console.warn('[GLIMPSE math] katex not loaded; math rendering disabled.');
    return;
  }

  // ── The pandoc-flavored delimiter grammar ────────────────────────────────
  // Inline $...$:  opening '$' not followed by whitespace; content has NO '$'
  // and NO newline (a match can never run away across prose) and does not end
  // in whitespace; closing '$' not followed by a digit (currency guard).
  const INLINE_RULE = /^\$(?!\s)((?:\\.|[^\\$\n])*?(?:\\.|[^\\$\n\s]))\$(?!\d)/;
  // Single-line $$...$$ (inline displayMode): same discipline, doubled sigils.
  const INLINE_DISPLAY_RULE = /^\$\$((?:\\.|[^\\$\n])*?(?:\\.|[^\\$\n\s]))\$\$(?!\d)/;
  // Block display: $$ on its own line, multi-line content (no '$' inside),
  // closing $$ on its own line.
  const BLOCK_RULE = /^\$\$\n([^$]+?)\n\$\$(?:\n|$)/;

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function renderKatex(text, displayMode, raw) {
    try {
      // throwOnError:false -> broken TeX renders as red source, never throws.
      return window.katex.renderToString(text, { displayMode: displayMode, throwOnError: false });
    } catch (err) {
      // Belt-and-suspenders: if KaTeX ever does throw, degrade to the raw
      // delimiters as escaped text instead of breaking the whole render.
      const esc = escapeHtml(raw != null ? raw : text);
      return displayMode
        ? '<pre class="glimpse-math-error">' + esc + '</pre>'
        : '<code class="glimpse-math-error">' + esc + '</code>';
    }
  }

  const inlineMath = {
    name: 'inlineKatexMath',
    level: 'inline',
    start(src) { return src.indexOf('$'); },
    tokenizer(src) {
      // $$..$$ first (longest sigil wins), then $..$.
      let match = src.match(INLINE_DISPLAY_RULE);
      if (match) {
        return { type: 'inlineKatexMath', raw: match[0], text: match[1].trim(), displayMode: true };
      }
      match = src.match(INLINE_RULE);
      if (match) {
        return { type: 'inlineKatexMath', raw: match[0], text: match[1].trim(), displayMode: false };
      }
      return undefined;
    },
    renderer(token) {
      return renderKatex(token.text, token.displayMode, token.raw);
    },
  };

  const blockMath = {
    name: 'blockKatexMath',
    level: 'block',
    tokenizer(src) {
      const match = src.match(BLOCK_RULE);
      if (match) {
        return { type: 'blockKatexMath', raw: match[0], text: match[1].trim(), displayMode: true };
      }
      return undefined;
    },
    renderer(token) {
      return renderKatex(token.text, true, token.raw) + '\n';
    },
  };

  try {
    window.marked.use({ extensions: [inlineMath, blockMath] });
    console.info('[GLIMPSE math] KaTeX math rendering enabled ($...$ inline, $$...$$ display; pandoc-flavored delimiters).');
  } catch (err) {
    console.warn('[GLIMPSE math] failed to register the KaTeX extension; math rendering disabled.', err);
  }
})();
