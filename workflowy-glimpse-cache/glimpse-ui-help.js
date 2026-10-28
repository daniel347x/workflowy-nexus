/*
 * glimpse-ui-help.js — the WORDS of "The Rail": bylines, one-line descriptions,
 * the Help drawer's sections (the in-app README), the first-run tips and the
 * handful of UI strings the drawers paint. R2's text verbatim ([ap:PA0EIU], with
 * the synthesis' two one-liner edits, [ap:HFOLEJ]). Data only, no DOM code: it
 * publishes `GlimpseUiHelp` for glimpse-ui-drawers.js (loaded after this file).
 */
(function installGlimpseUiHelp(root) {
  'use strict';

  // ───────────────────────── tip images (inline SVG; theme-following) ─────────────────────────
  // 1 · The rail: the Workflowy outline beside the Reader, the rail at the edge.
  const SVG_RAIL = '<svg viewBox="0 0 268 92" width="268" height="92" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round">'
    + '<rect x="1" y="1" width="266" height="90" rx="6" opacity="0.35"/>'
    + '<g opacity="0.45"><line x1="14" y1="18" x2="80" y2="18"/><line x1="22" y1="30" x2="96" y2="30"/><line x1="22" y1="42" x2="70" y2="42"/><line x1="14" y1="56" x2="88" y2="56"/><line x1="22" y1="68" x2="64" y2="68"/></g>'
    + '<rect x="122" y="8" width="112" height="76" rx="4"/>'
    + '<line x1="132" y1="22" x2="196" y2="22" stroke-width="2.4"/><line x1="132" y1="36" x2="222" y2="36"/><line x1="132" y1="46" x2="214" y2="46"/><line x1="132" y1="56" x2="204" y2="56"/><line x1="132" y1="66" x2="218" y2="66"/>'
    + '<rect x="240" y="8" width="20" height="76" rx="4" stroke="var(--nx-accent)" stroke-width="2"/>'
    + '<circle cx="250" cy="18" r="2.4" fill="var(--nx-accent)" stroke="none"/><rect x="245" y="27" width="10" height="8" rx="2"/><rect x="245" y="40" width="10" height="8" rx="2"/><rect x="245" y="53" width="10" height="8" rx="2"/><rect x="245" y="66" width="10" height="8" rx="2"/>'
    + '<path d="M214 78 Q 232 88 238 76" stroke="var(--nx-accent)"/><path d="M234 74 l4 2 l-1 4" stroke="var(--nx-accent)"/>'
    + '</svg>';

  // 2 · A Dock: five outline lines; the second ends in a small rounded tag box,
  //     the two beneath it are what agents filed there (the arrow).
  const SVG_DOCK = '<svg viewBox="0 0 268 92" width="268" height="92" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round">'
    + '<rect x="1" y="1" width="266" height="90" rx="6" opacity="0.35"/>'
    + '<g opacity="0.45"><circle cx="22" cy="18" r="2" fill="currentColor" stroke="none"/><line x1="32" y1="18" x2="104" y2="18"/>'
    + '<circle cx="22" cy="74" r="2" fill="currentColor" stroke="none"/><line x1="32" y1="74" x2="92" y2="74"/></g>'
    + '<circle cx="22" cy="32" r="2.4" fill="var(--nx-accent)" stroke="none"/><line x1="32" y1="32" x2="138" y2="32" stroke-width="2"/>'
    + '<rect x="146" y="24" width="40" height="16" rx="5" stroke="var(--nx-accent)" stroke-width="1.6"/>'
    + '<path d="M156 29 l-1.5 6 M161 29 l-1.5 6 M153.5 31.2 h9.5 M152.5 33.8 h9.5" stroke="var(--nx-accent)" stroke-width="1.2"/><line x1="167" y1="32" x2="180" y2="32" stroke="var(--nx-accent)" stroke-width="1.6"/>'
    + '<circle cx="40" cy="46" r="2" fill="currentColor" stroke="none"/><line x1="50" y1="46" x2="126" y2="46"/>'
    + '<circle cx="40" cy="60" r="2" fill="currentColor" stroke="none"/><line x1="50" y1="60" x2="110" y2="60"/>'
    + '<path d="M246 70 C 232 70 214 66 186 48" stroke="var(--nx-accent)" stroke-dasharray="3 3"/><path d="M190 46 l-5 2 l2 5" stroke="var(--nx-accent)"/>'
    + '<rect x="214" y="12" width="40" height="22" rx="4" opacity="0.5"/><line x1="221" y1="20" x2="247" y2="20" opacity="0.5"/><line x1="221" y1="26" x2="239" y2="26" opacity="0.5"/>'
    + '</svg>';

  // 3 · A slice group: a tree with three tagged nodes and one folded subtree;
  //     at the right, the three tagged nodes rendered as one document.
  const SVG_SLICES = '<svg viewBox="0 0 268 92" width="268" height="92" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round">'
    + '<rect x="1" y="1" width="266" height="90" rx="6" opacity="0.35"/>'
    + '<circle cx="18" cy="16" r="2" fill="currentColor" stroke="none"/><line x1="28" y1="16" x2="84" y2="16"/>'
    + '<circle cx="34" cy="30" r="2.4" fill="var(--nx-accent)" stroke="none"/><line x1="44" y1="30" x2="96" y2="30" stroke-width="2"/><rect x="102" y="24" width="18" height="12" rx="4" stroke="var(--nx-accent)"/>'
    + '<circle cx="34" cy="44" r="3.2" stroke-width="1.6" opacity="0.7"/><line x1="44" y1="44" x2="90" y2="44" opacity="0.7"/><path d="M96 41 l3 3 l-3 3" opacity="0.7"/>'
    + '<circle cx="34" cy="58" r="2.4" fill="var(--nx-accent)" stroke="none"/><line x1="44" y1="58" x2="108" y2="58" stroke-width="2"/><rect x="114" y="52" width="18" height="12" rx="4" stroke="var(--nx-accent)"/>'
    + '<circle cx="50" cy="72" r="2.4" fill="var(--nx-accent)" stroke="none"/><line x1="60" y1="72" x2="100" y2="72" stroke-width="2"/><rect x="106" y="66" width="18" height="12" rx="4" stroke="var(--nx-accent)"/>'
    + '<path d="M138 46 h22" stroke="var(--nx-accent)"/><path d="M156 42 l4 4 l-4 4" stroke="var(--nx-accent)"/>'
    + '<rect x="168" y="10" width="90" height="72" rx="4"/>'
    + '<line x1="178" y1="26" x2="236" y2="26" stroke="var(--nx-accent)" stroke-width="2.4"/><line x1="178" y1="38" x2="246" y2="38" opacity="0.5"/>'
    + '<line x1="178" y1="50" x2="240" y2="50" stroke="var(--nx-accent)" stroke-width="2.4"/><line x1="178" y1="62" x2="230" y2="62" opacity="0.5"/>'
    + '<line x1="186" y1="72" x2="236" y2="72" stroke="var(--nx-accent)" stroke-width="2.4"/>'
    + '</svg>';

  root.GlimpseUiHelp = Object.freeze({
    systemLine: 'Workflowy context management, with your agents.',

    helpIntro: 'Your agents read and write your Workflowy outline through the NEXUS daemon. This extension shows you exactly what they see, and gives you the same controls: named containers (Anchor Docks), saved views (slice groups), one-off searches (the Lightning Rod), and the Reader that renders any of them as one document.',

    bylines: Object.freeze({
      reader: 'what your agent sees, rendered',
      anchors: 'where things go',
      slices: 'what you look at',
      rod: 'one-off search, nothing saved',
      tabs: 'name this tab for your agents',
      jobs: 'what the daemon is doing',
      settings: 'appearance here; everything else in Options',
      help: 'keys, menus, and how it fits together',
    }),

    titles: Object.freeze({
      anchors: 'Anchor Docks',
      slices: 'Slice groups',
    }),

    oneLiners: Object.freeze({
      anchors: 'A Dock is a container in your outline: a node with a tag on the end of its name. Your agents find it by name and file things there; you set it up once. Click one to read it.',
      slices: 'A slice group is a saved view that cuts across your Docks: the nodes under a root that carry its tags, rendered as one document, exactly as your agents see it. Apply one to read it.',
      jobs: 'Cartographer jobs map files into Workflowy; the nodes cache is what every render reads from; the Activity log keeps every status line.',
    }),

    readerEmpty: '**Reader** — click a node in Workflowy to read it here as one document: the same render your agents get from GLIMPSE.\n\nOr open a Dock or a slice group from the rail, or press {hotkey} for every command.',

    sections: Object.freeze([
      Object.freeze({
        id: 'nx-help-reader',
        heading: 'Reader',
        byline: 'what your agent sees, rendered',
        backId: 'reader',
        backLabel: 'Reader',
        paras: Object.freeze([
          'Any node, Dock, slice group or search, as one document. LIVE follows your clicks in Workflowy; FROZEN keeps a snapshot you can sort and filter; SLICE is an applied group. Rendered or Source, find in text, and an outline of rows: select rows to copy their ids or edit their tags in one batch.',
        ]),
      }),
      Object.freeze({
        id: 'nx-help-docks',
        heading: 'Anchor Docks',
        byline: 'where things go',
        backId: 'anchors',
        backLabel: 'Anchor Docks',
        paras: Object.freeze([
          '**What a Dock is.** A Dock is an ordinary Workflowy node, with everything under it, that carries the Dock tag at the end of its name (`#wfy-ns` by default; the tag is yours to set in Options). It has a name and a description (its note). That is all an agent needs: it lists your Docks, picks the right one by name, reads the description, and puts things there. You set a Dock up once; afterwards things appear where you expect them.',
          '**Making one is trivial.** Put the tag on the end of any node’s name. That node is now a Dock; this list shows it after the next cache refresh. Agents can do it too (`anchor(verb=\'dock.create\')`).',
          '**Favorites.** The ⭐ on a row adds a second tag (`#wfy-ns--fav`). Starred Docks are the shortlist this drawer and the agents’ `dock.list` show by default: the Docks in play right now. `Favorites` off shows every Dock.',
          '**Peeking.** A Dock is also an inbox and a to-do list. Agents *peek* a Dock to see its direct children, or the newest few by modified or created time, without reading everything; *render* reads it whole, which is what clicking a Dock here does.',
          '**Nested Docks: a view stops at the next Dock.** Docks nest. Opening a Dock renders everything under it but stops where a child Dock begins: the child appears as a signpost, and you go down into it (click it here) rather than reading it through its parent. `Descend` turns that off and renders through child Docks. Agents see the same boundary.',
          '**Local Docks.** A Dock tagged `#wfy-ns--local` is a real Dock (it nests, it bounds a view) but stays off the top-level list, so the inventory shows topics, not every working folder. `Local` shows them.',
          '**Docks and slice groups.** A Dock is a *place*; a slice group is a *view*. A slice group collects by tags across Docks, or across all of Workflowy; a Dock is where things live. They combine: a slice group whose root is a Dock means “this view, inside this container”.',
          '**Curation.** Because agents file into Docks, a Dock fills up. A curation pass moves the keepers into their topic Docks (agents do it with `point.promote`, which leaves a `↩ moved from` line) and completes the rest. `since: curated` in the View popover shows what has arrived since the last pass.',
          '**The same words on both sides.** What you see here is what agents see: `dock.list` is this list, `dock.peek` the recent children, `dock.render` the Reader’s view, `anchor.search` a search inside one Dock, `point.capture` filing something here. The names match on purpose.',
          'More: the README, section *Anchor Docks*.',
        ]),
      }),
      Object.freeze({
        id: 'nx-help-slices',
        heading: 'Slice groups',
        byline: 'what you look at',
        backId: 'slices',
        backLabel: 'Slice groups',
        paras: Object.freeze([
          '**What a slice group is.** A few tags and a root, saved under a name. Applying it renders every node beneath the root that carries one of the tags, as one document, in document, created or modified order. It cuts across containers: a group over the whole outline collects a topic from every Dock it touches.',
          '**Start simply.** One tag, no root. Tag three nodes `#topic`, make a group with the tag `topic`, Apply. That already works, and it is what most groups are.',
          '**The power is in the postfixes.** Add a word after a tag with two hyphens and the same tag starts to shape the view: `#topic--expand` marks what opens, `#topic--suppress` folds a subtree out of the view, `#topic--deprecated` keeps a node in the family but drops it from the default view. Tags are matched as prefixes, so `#topic-anything` is still a member. This is how you control what agents see, node by node, with no code: an agent asking for the group gets exactly this view.',
          '**Apply, and the agent.** Apply renders from the nodes cache: complete, and frozen until you unfreeze. An agent’s `glimpse(group_handle=…)` returns the same document; `push_to_modal` puts it in this Reader. `Sync view` reshapes the live Workflowy pane to match, only when you ask.',
          '**Names make a tree.** A name like `Programs|Conductor Toolkit|Results [1]` files the group under two categories and orders it first. The `?` beside `New` explains the grammar and shows your current arrangement.',
          '**Lightning Rod or slice group?** A slice group is the tidy garage you come back to. The Lightning Rod is a day’s work on the bench: a search or a tag filter over whatever the Reader shows, nothing saved.',
          'More: the README, section *Slice groups and postfixes*.',
        ]),
      }),
      Object.freeze({
        id: 'nx-help-rod',
        heading: 'Lightning Rod',
        byline: 'one-off search, nothing saved',
        backId: 'rod',
        backLabel: 'Lightning Rod',
        paras: Object.freeze([
          'A search or a tag filter beneath the Reader’s current root, rendered like a slice but kept nowhere. Scoped searches only the frozen view; Gather tags lists every tag beneath the root; Dyn slice is a one-off slice group from the selected row.',
        ]),
      }),
      Object.freeze({
        id: 'nx-help-tabs',
        heading: 'Tabs',
        byline: 'name this tab for your agents',
        backId: 'tabs',
        backLabel: 'Tabs',
        paras: Object.freeze([
          'Give this tab an alias and agents reach it as target_tab; the Default tab answers calls that name none. Saved tabs export and import a whole working set of tabs, each with what its Reader shows.',
        ]),
      }),
      Object.freeze({
        id: 'nx-help-jobs',
        heading: 'Jobs',
        byline: 'what the daemon is doing',
        backId: 'jobs',
        backLabel: 'Jobs',
        paras: Object.freeze([
          'Cartographer maps a repository into Workflowy, one node per file and symbol; its jobs run here with Cancel. The nodes cache is the snapshot every render reads; refresh it after editing Workflowy. Skip refreshes lets parallel runs share one cache. The Activity log keeps every status line.',
          'The list polls only while this tab is visible; outcome labels distinguish completed with warnings or errors, and “N older jobs not shown” marks omitted history.',
          'On a problem row, one Details click requests its errors, warnings and findings for this tab only; Load more reaches every problem row and item.',
        ]),
      }),
    ]),

    f12Numbers: Object.freeze([
      Object.freeze(['File', '1 async refresh from disk · 2 bulk visible beacon/tag apply · 3 regenerate the Markdown file · 0 forced refresh (two-step)']),
      Object.freeze(['Folder', '1 async folder refresh · 2 bulk visible beacon/tag apply · 3 bulk regenerate Markdown files (when offered) · 0 forced refresh (press twice)']),
      Object.freeze(['Node', '1 fast beacon update · 2 async containing-file refresh · 3 bulk apply on the containing file · 4 materialize / explode (when offered)']),
      Object.freeze(['Note', '4 materialize into a Markdown file · 5 explode into a subtree · bulk variants when the server offers them']),
      Object.freeze(['Second step', 'F12+4 asks keep or delete the generated file; the ⚠ 0 row needs a second press.']),
    ]),

    syntaxHelp: Object.freeze({
      title: 'How a group’s name becomes a tree',
      paras: Object.freeze([
        '**A group’s name is a path.** `NEXUS|Source map|Slices` files the group under *NEXUS › Source map*; only the last part is shown as its name.',
        '**Order with [n].** `…|Slices [2]` puts it second among its siblings. No number: after the numbered ones, alphabetically.',
        '**A category of its own.** A row named `[1]NEXUS` is a header, not a group: it only orders the *NEXUS* category. Categories you merely refer to are created for you.',
        '**Two headers for one category:** the lower number wins and the other shows empty; delete or renumber it.',
      ]),
      note: 'A plain picker that needs none of this is a planned enhancement.',
    }),

    tips: Object.freeze([
      Object.freeze({
        id: 'tip-rail',
        title: '1 of 3 · The rail',
        body: 'Everything NEXUS adds to Workflowy lives in this rail. Click any node to read it in the Reader. The icons open your Anchor Docks, slice groups, the Lightning Rod, this tab’s name for agents, and the daemon’s jobs.',
        image: SVG_RAIL,
      }),
      Object.freeze({
        id: 'tip-docks',
        title: '2 of 3 · Anchor Docks',
        body: 'A Dock is a Workflowy node with a tag on the end of its name. Your agents find it by name and file things there; you set it up once, and things appear where you expect.',
        image: SVG_DOCK,
      }),
      Object.freeze({
        id: 'tip-slices',
        title: '3 of 3 · Slice groups',
        body: 'A slice group is a saved view: a few tags and a root. Apply it and read it as one document. A postfix on a tag (‑‑expand, ‑‑suppress) shapes what you and your agents see, node by node.',
        closing: 'That is the whole idea. When you want more, the Help drawer (?) explains each part, and the README has the agent side.',
        image: SVG_SLICES,
      }),
    ]),

    strings: Object.freeze({
      sliceNotice: '🔒 Applied slice: a complete document from the nodes cache. Clicks in Workflowy do not disturb it. Edited Workflowy? Refresh the cache (Jobs), then ↻. Unfreeze returns to the live view.',
      savedToast: 'Saved on this machine (slice groups are not synced; Export backs them up).',
      applyHint: 'Renders this group from the nodes cache into the Reader: complete, frozen until you unfreeze, and exactly what an agent’s push would show.',
      editorHead: 'Tags, root and default sort are what define a group. Saved on this machine only.',
      filterSlices: 'Filter slice groups…',
      palettePlaceholder: 'Type a command… (↑ ↓ choose, Enter runs, Esc closes)',
      dotConnected: 'Linked to the NEXUS daemon through its broker',
      dotDisconnected: 'Not linked (the service worker may be waking, or the daemon is down)',
      readerRailTooltip: 'Reader — what your agent sees, rendered (click a Workflowy node to show it here)',
      paletteRailTooltip: 'Command palette — every command, by name ({hotkey})',
      favoritesNone: 'No favorites yet, so every Dock is shown. Star one (⭐) to start your shortlist.',
      optionsSubline: 'Workflowy context management, with your agents',
      manifestName: 'NEXUS for Workflowy',
      manifestDescription: 'Workflowy context management, with your agents',
    }),
  });
})(typeof globalThis !== 'undefined' ? globalThis : window);
