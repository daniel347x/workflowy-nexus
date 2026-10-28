/**
 * Workflowy GLIMPSE Cache - Hotkey actions catalog and binding parser
 *
 * Loaded by BOTH:
 *   - The main-world content script (glimpse-cache-client.js) via the
 *     manifest's content_scripts entry, run BEFORE glimpse-cache-client.js.
 *   - The options page (options.html) via <script src="hotkeys-shared.js">.
 *
 * Single source of truth for:
 *   - The list of customizable actions (HOTKEY_ACTIONS).
 *   - The default binding for each action.
 *   - parseHotkey()       — turn "Ctrl+F12" into { ctrl, alt, shift, meta, key }.
 *   - formatHotkey()      — turn a captured KeyboardEvent into "Ctrl+F12".
 *   - matchesHotkey()     — does a KeyboardEvent match a binding string?
 *   - isHotkeyReserved()  — is this binding string something Chrome will steal?
 *
 * Binding string syntax:
 *   - Modifiers: "Ctrl", "Alt", "Shift", "Meta" (any subset, any order).
 *   - Separator: "+".
 *   - Final key: a JavaScript KeyboardEvent.key value, case-normalized.
 *     Examples: "F12", "Enter", "Escape", "ArrowUp", "K", " " (space), etc.
 *
 * Examples:
 *   "F7"
 *   "F3"
 *   "Ctrl+F12"
 *   "Ctrl+Shift+K"
 *   "Alt+Enter"
 */

(function (root) {
  'use strict';

  // ---------------------------------------------------------------------------
  // Canonical actions catalog. The order here is the display order in the
  // options page.
  //
  // To add a new customizable action:
  //   1. Add a new entry to HOTKEY_ACTIONS below.
  //   2. In glimpse-cache-client.js, replace the hardcoded event.key check
  //      with: if (matchesHotkey(event, hotkeyConfig.<your_action_name>)) {...}
  //   3. Done. Defaults + options-page UI + storage all pick it up
  //      automatically.
  // ---------------------------------------------------------------------------
  // @beacon[
  //   id=auto-beacon@__lambdao_1.HOTKEY_ACTIONS@1-lejo,
  //   role=const HOTKEY_ACTIONS,
  //   slice_labels=f12-popup,
  //   kind=ast,
  // ]
  const HOTKEY_ACTIONS = Object.freeze({
    uuid_copy_verbose: {
      label: 'UUID copy — full path with UUIDs at every level',
      default: 'F2',
    },
    uuid_copy_compact: {
      label: 'UUID copy — path, UUID only at leaf',
      default: 'F3',
    },
    uuid_copy_bare: {
      label: 'UUID copy — bare UUID, no path',
      default: 'F4',
    },
    expand_nexus_roots: {
      label: 'Expand every visible slice-tagged root in place (see “F8 postfixes”)',
      default: 'F8',
    },
    open_in_windsurf: {
      label: 'Open associated source file in Windsurf',
      default: 'F9',
    },
    toggle_marker_tag: {
      label: 'Toggle marker tag on hovered node (see “Marker tags” below)',
      // Chrome assigns bare F10 to the window/menu-bar focus action, which
      // hijacks the keypress before the page sees it, so the default is
      // Ctrl+F10.
      default: 'Ctrl+F10',
    },
    cartographer_popup: {
      label: 'Cartographer menu for the hovered node (then a number)',
      // Chrome reserves bare F12 for DevTools, so the default is Ctrl+F12.
      default: 'Ctrl+F12',
    },
    markdown_preview_close: {
      label: 'Clear the Reader and close the deck',
      default: 'Ctrl+F1',
    },
    command_palette: {
      label: 'Open the NEXUS command palette (every deck command by name)',
      default: 'Ctrl+Shift+K',
    },
    toggle_deck: {
      label: 'Open / close the NEXUS deck (the Reader and its drawers)',
      default: 'Alt+N',
    },
  });

  // ---------------------------------------------------------------------------
  // Reserved bindings — bare keys that Chrome intercepts before the page
  // sees them. We refuse to capture these in the options page UI; the user
  // can still use them with a modifier prefix.
  // ---------------------------------------------------------------------------
  // @beacon[
  //   id=auto-beacon@__lambdao_1.RESERVED_BARE_KEYS@1-q6ft,
  //   role=const RESERVED_BARE_KEYS,
  //   slice_labels=f12-popup,
  //   kind=ast,
  // ]
  const RESERVED_BARE_KEYS = new Set([
    'F1',   // Help
    'F5',   // Reload
    'F6',   // Address bar focus
    'F11',  // Fullscreen
    'F12',  // DevTools
  ]);


  // ---------------------------------------------------------------------------
  // Extension-wide settings (non-hotkey). Kept here because this file is
  // already shared by the service worker, options page, and main-world client.
  // ---------------------------------------------------------------------------
  const DEFAULT_EXTENSION_SETTINGS = Object.freeze({
    // Maximum serialized GLIMPSE payload size sent from the page client to the
    // service worker / MCP server. 500 KB is intentionally conservative: large
    // enough for Dan's normal usage, small enough to avoid mysterious Chrome
    // extension message drops/timeouts on huge expanded forests.
    max_glimpse_payload_kb: 500,
    // Width (in CSS pixels) of the Markdown note preview popup. 720 matches
    // the original hardcoded max-width and is a good default; users can widen
    // or narrow it from the options page. The popup still clamps itself into
    // the viewport horizontally, so an over-wide value is harmless.
    md_preview_width_px: 720,
    // ── Docked split-panel MD preview (Dan, June 2026) ──────────────────
    // Whether the MD preview renders as a full-height panel docked to the
    // right edge. As of June 2026 this defaults to TRUE and is the ONLY
    // surface: the legacy floating above/below-node popup was removed (see
    // showMarkdownPreview in glimpse-cache-client.js, now an unconditional
    // dock router). The flag is retained for the dock ON/OFF toggle + the
    // visual-hidden persistence, not as a popup-vs-dock switch anymore.
    md_preview_docked: true,
    // Width (CSS px) of the docked panel. Separate from md_preview_width_px
    // (the floating popup width) because the two surfaces want different
    // comfortable widths. Adjusted via the panel's [−]/[+] ribbon buttons in
    // 25px steps; clamped into the viewport at render time.
    md_preview_dock_width_px: 640,
    // Dock position: 'right' | 'left' | 'top' | 'bottom'. Controls which
    // edge of the viewport the panel docks to. The [−]/[+] ribbon buttons
    // adjust width for left/right and height for top/bottom.
    md_preview_dock_position: 'right',
    // Height (CSS px) of the docked panel when docked at top or bottom.
    // The width setting (md_preview_dock_width_px) is used for left/right.
    md_preview_dock_height_px: 400,
    // Fraction (0..1) of the docked panel's content height given to the top
    // LIST region; the bottom RENDER region gets the rest. Adjusted via the
    // draggable splitter. Clamped to [0.12, 0.88].
    md_preview_dock_list_fraction: 0.4,
    // Top offset (CSS px) of the docked panel from the top of the viewport.
    // The panel is right-docked full-height; this pushes its TOP edge down so
    // it clears Workflowy's own search box (into which the user pastes boolean
    // tag queries). Without the offset the panel covers the search box and the
    // query text is hidden. Default 100 (a safe clearance); user-configurable
    // in the options page. Clamped to [0, viewportHeight-120] at render time.
    md_preview_dock_top_offset_px: 100,
    // Visual-only hidden state for the docked panel: when true, the panel is
    // collapsed out of view but all its state (root, rows, selection, frozen
    // snapshot) is preserved; re-showing restores it. Distinct from
    // md_preview_docked (which is whether dock MODE is the chosen surface).
    md_preview_dock_hidden: false,
    // Max characters rendered into the MD preview (floating popup AND docked
    // render region) before truncating with a footer note. Default 2000000
    // (raised from 200000 in June 2026 so very large files preview in full out
    // of the box). Dan previews very large pseudo-files, so this is
    // intentionally tunable with no hard upper ceiling (only a sane lower
    // floor of 1000 in the client/merge).
    md_preview_max_chars: 2000000,
    // Threshold (in characters) on the TOTAL visible-tree text size (sum of
    // every visible node's name + note length in the docked modal) that
    // selects the modal's server-render STRATEGY. At or below this size, the
    // server PRE-RENDERS every visible row's per-row view once per
    // root-set/refresh and the browser caches them, so clicking a row in the
    // top-half tree view is an instant cache lookup (the O(N^2) pre-render is
    // cheap when the tree is small). Above this size, that O(N^2) pre-render
    // would be wasteful for the large pseudo-documents Dan views (100 KB+), so
    // the modal instead renders ONLY the selected row's view on demand — one
    // server round trip per row click (O(N) each; per-click latency is
    // acceptable and barely noticeable, with a timer-driven "freeze to speed
    // this up" nudge if a render runs long). Default 100000 (~100 KB). Raise
    // it to pre-render larger trees for snappier clicks; lower it to switch to
    // on-demand sooner. Clamped to a sane floor (0 = always on-demand) in the
    // merge. Set it in the options page.
    md_modal_prerender_max_chars: 100000,
    // ── Docked-modal + GLIMPSE timestamp sort / since / headings ──────────
    // NOTE (Dan, July 2026 de-globalization): the dock's timestamp-sort mode +
    // direction, the `since` recent-tail token, and the headings-only toggle
    // are TRANSITORY, PER-TAB, SESSION-LIVED VIEW knobs. They are NO LONGER
    // stored in these cross-tab/cross-machine synced extension settings — a
    // synced global for a per-tab transient view state never made sense
    // (different tabs need different sorts; the state dies on reload anyway).
    // They now live ONLY in per-tab sessionStorage (glimpse_modal_state, see
    // getTabModalState/setTabModalState in glimpse-cache-client.js), with the
    // accessors defaulting a fresh tab to natural sort / no since / headings
    // off. So md_preview_sort_by / _sort_dir / _since / _headings_only are
    // intentionally ABSENT here (defaults) and from the sanitizer below.
    // (The other md_preview_* keys — width, dock fraction, max_chars, etc. —
    // are genuine cross-session layout prefs and legitimately REMAIN global.)
    // List of tag PREFIXES (stored WITHOUT the leading '#') that the
    // "expand visible roots" hotkey (default F8) acts on. A visible node is
    // treated as an expand-root when ANY of its tags begins with ANY of
    // these prefixes. Default ['wfy-'] (single hyphen). The user may add
    // multiple prefixes via the options page (comma-separated). Workflowy
    // permits emojis in tags, so emojis are allowed inside a prefix; only
    // whitespace-bearing or empty prefixes are rejected by the sanitizer.
    //
    // ── LEGACY (pre-group) ──────────────────────────────────────────────
    // This flat list is SUPERSEDED by expand_groups / active_group below as
    // of July 2026. It is retained ONLY so any settings object written by an
    // older build still parses cleanly (mergeExtensionSettings no longer
    // reads it for F8; getExpandTagPrefixes() resolves the ACTIVE GROUP's
    // prefixes instead). New code should not consume this key.
    expand_tag_prefixes: ['wfy-'],
    // ── F8 expand-tag-prefix GROUPS (Dan, July 2026) ────────────────────
    // Replaces the single flat expand_tag_prefixes list with NAMED GROUPS,
    // exactly ONE of which is active at a time. F8 acts on the ACTIVE
    // group's prefix list. Each group is { id, name, prefixes }:
    //   • id       — STABLE hidden identity (minted via genGroupId(); never
    //                changes once created). active_group stores an id, NOT a
    //                name, so renaming a group never dangles the active
    //                pointer and duplicate display names are harmless.
    //   • name     — human display label shown in the widget (freely
    //                renamable; not required to be unique).
    //   • prefixes — canonical ('#'-less) prefix array, same form/sanitize
    //                rules as the legacy expand_tag_prefixes.
    // If no groups exist when settings load, mergeExtensionSettings()
    // auto-creates ONE default group named 'default' carrying the single
    // prefix 'wfy-' (the project's three-char Workflowy tag). The default's
    // id is generated, so there is nothing special about it — it may be
    // renamed or deleted freely; if it ends up the only group and is
    // deleted, a fresh default is recreated on the next load.
    expand_groups: [{ id: 'grp-default', name: 'default', prefixes: ['wfy-'] }],
    // ID of the currently-active expand group (see expand_groups). Stores a
    // group ID (never a name). Normalized at merge time: if it does not
    // resolve to a live group, it is reset to the first group's id.
    active_group: 'grp-default',
    // ── F8 SLICE POSTFIXES (Dan, 2026) ───────────────────────────
    // A slice tag may carry a hyphen-POSTFIX that modulates F8 behavior for
    // that node WITHOUT changing slice membership. Given a base slice prefix
    // P (e.g. 'tag--a'), the postfixed variant is P + '--' + <postfix>:
    //   • auto-expand  (P--expand)     -> F8 actually expands this subtree
    //                                      (bare P alone is membership/marker
    //                                      only). The postfixed tag still
    //                                      matches a bare-P Workflowy click by
    //                                      prefix, so it stays in the slice.
    //   • suppress     (P--suppress)   -> F8 collapses this subtree (and does
    //                                      not expand anything beneath it).
    //                                      Suppression of an ancestor ABOVE
    //                                      the slice-group root is inert (you
    //                                      can zoom inside to un-suppress).
    //   • deprecated   (P--deprecated) -> node stays IN the slice (still
    //                                      matches a bare-P click) but is
    //                                      excluded from the default
    //                                      membership view via the compiled
    //                                      search's negation
    //                                      (#P -#P--deprecated), and F8
    //                                      collapses it (unless it would hide
    //                                      a live, non-deprecated descendant).
    // These are stored as the bare POSTFIX WORD (no leading '--', no '#'); the
    // server's compiler/policy joins them as P + '--' + word. The server is
    // the SINGLE consumer of these (it reads them live via the settings
    // round-trip and compiles the Workflowy search string + the F8 plan), so
    // there is exactly one place the postfix semantics live. Empty/whitespace
    // values fall back to the shipped defaults at merge time so F8 always has
    // a usable postfix vocabulary.
    f8_postfix_auto_expand: 'expand',
    f8_postfix_suppress: 'suppress',
    f8_postfix_deprecated: 'deprecated',
    // ── F8 GLOBAL NEVER-EXPAND TAGS (WS-1.9; Dan, 2026) ──────────────
    // A blanket suppressor list (canonical, '#'-less), INDEPENDENT of any
    // slice/topic: F8 never auto-expands a subtree rooted at a node carrying
    // ANY of these tags, for ANY active slice group. Same parse/sanitize rules
    // as expand_tag_prefixes (array-or-comma-string; '#'-stripped; de-duped;
    // emojis allowed; empty/internal-whitespace tokens dropped). An EMPTY list
    // is a legitimate value (no global suppressors), so — unlike the postfix
    // words — an explicitly-empty result is honored (no fallback default).
    f8_global_never_expand_tags: [],
    // List of MARKER TAGS (stored WITHOUT the leading '#') that the
    // "toggle marker tag" hotkey (default F10) can add/remove on the hovered
    // node's trailing tag region. These are temporary, Workflowy-only marker
    // tags used to isolate a small number of subtree roots (via Workflowy's
    // native tag-click filter) so the expand hotkey (F8) acts on just those.
    // Default ['wfy-\uD83C\uDFF7\uFE0F'] (i.e. 'wfy-\uD83C\uDFF7\uFE0F', which renders as
    // wfy- + label emoji). The default intentionally carries the 'wfy-'
    // expand prefix so a marked node is also F8-expandable. When more than
    // one marker tag is configured, F10 shows an inline picker popup.
    // Same sanitize rules as expand_tag_prefixes: emojis allowed, internal
    // whitespace / stray '#' rejected.
    marker_tags: ['wfy-\uD83C\uDFF7\uFE0F'],
    // ── Tab rename on alias (Dan, June 2026) ───────────────────────────
    // When true, setting a tab alias in the UUID Explorer widget's "Set
    // alias" control ALSO renames the browser tab to 'Workflowy - <alias>'.
    // Clearing the alias restores Workflowy's own native title management.
    // Implemented by re-asserting document.title via a MutationObserver on
    // the <title> element (Workflowy is a SPA that rewrites its own title on
    // navigation, so a one-shot assignment would not stick). Cross-browser:
    // document.title is the universal tab-title mechanism in Chrome, Firefox,
    // and Electron, and the MAIN-world content script can set it directly
    // with no extra permissions. Default true so the click-to-rename works
    // out of the box; turn it off here to keep Workflowy's native titles.
    rename_tab_with_alias: true,
    // The AFFIX word combined with the alias to form the tab title. 'Affix'
    // is the generic term covering both prefix and suffix; tab_title_affix_
    // position decides which side it sits on. Default 'Workflowy', giving
    // 'Workflowy - <alias>' (prefix) or '<alias> - Workflowy' (suffix).
    // May be multi-word; leading/trailing whitespace is trimmed. An empty
    // affix falls back to the default at merge time.
    tab_title_affix: 'Workflowy',
    // Where the affix sits relative to the alias: 'prefix' -> '<affix> - <alias>',
    // 'suffix' -> '<alias> - <affix>'. Any other value normalizes to 'prefix'.
    tab_title_affix_position: 'prefix',
    // ── Default Markdown folder for F12+4 (Dan, June 2026) ─────────────
    // Absolute directory where the F12+4 "materialize note as Markdown file"
    // action writes a NEW .md when the hovered node's ANCESTOR chain has no
    // Cartographer Root:/folder header to anchor to (the standalone case).
    // When an ancestor header DOES exist, F12+4 ignores this and writes the
    // file relative to that ancestor instead. Empty string = unset: in the
    // standalone case F12+4 then errors with an actionable message asking the
    // user to set this or place the node under a Root:/folder subtree.
    // Stored verbatim (trimmed); no syntactic validation here — the server
    // enforces that the directory actually exists on disk before writing.
    default_markdown_folder: '',
    // ── Export archives (Archive Reads, Sep 2026) ──────────────────────
    // The ONE folder NEXUS reads export archives from when a handle is not
    // in Workflowy (node.export output_file writes them). Empty string =
    // archive reads off (the shipped default): the daemon then performs no
    // archive I/O at all. Stored trimmed; the daemon remembers the last
    // value it saw so archive reads never need a connected tab.
    archive_folder: '',
    // ── Note sessions (Dan, 2026) ──────────────────────────────────────
    // A NOTE SESSION is a durable, discoverable TOPIC CONTAINER (one
    // Workflowy node + its subtree), marked ONLY by carrying a configured
    // note-session tag in its NAME's trailing tag region. The literal words
    // "note session" never appear in the node name — the tag alone defines
    // it; the node name is the topic. Storage = chronology, meaning = tags.
    //
    // note_session_tags: comma-separated list of tags (stored canonical,
    // '#'-less; same parse/sanitize rules as expand_tag_prefixes via
    // parseExpandTagPrefixes). A node is a note session when its trailing
    // tag region contains ANY of these (Boolean OR). The FIRST tag is the
    // DEFAULT applied when creating a new session. Seeded with 'wfy-ns'.
    //
    // FORWARD-COMPAT (Q3 / v2): keep this a FLAT list now. The data model is
    // shaped so named TAG GROUPS can be layered on later (mirroring the F8
    // expand_groups mechanism) WITHOUT re-architecting — a flat list is the
    // degenerate "one group" case. Do NOT build groups yet.
    note_session_tags: ['wfy-ns'],
    // global_tags: the flat list of tags the note-session system "knows
    // about." When capturing into a session, the agent chooses from these
    // (merged with any tags already present in that session). Tags scattered
    // elsewhere in Workflowy that are not listed here are invisible to this
    // system. Stored canonical, '#'-less. (Same v2-groups forward-compat
    // note as note_session_tags applies.)
    global_tags: [],
    // default_note_sessions_parent: UUID of the Workflowy node under which a
    // new note session is created when create_note_session is called without
    // an explicit parent_uuid. Empty string = unset (the MCP tool then fails
    // loudly rather than silently falling back to a root). Stored verbatim
    // (trimmed); the options page renders its full root->node path for
    // confirmation via the existing Show-Path logic.
    default_note_sessions_parent: '',
    // project_maps_location: a Workflowy NODE POINTER where the user keeps a
    // Project Map — the node's UUID, the 8-hex start of it, or an [ap:…]
    // AP-ID. A pointer and nothing more: NEXUS never interprets, validates or
    // requires anything inside that node, and what a Project Map holds (or how
    // many there are) is entirely up to the user. Agents find it through
    // system.config.get (project_maps_location). Empty string = none. Stored
    // trimmed; never resolved here.
    project_maps_location: '',
    // note_session_local_postfixes: the LOCAL-ANCHOR postfix words (stored as
    // bare words, no leading '--', no '#'; same parse/sanitize as the F8
    // postfix words via sanitizeF8PostfixWord). Joined to a base anchor tag as
    // base + '--' + word (e.g. 'wfy-ns' -> 'wfy-ns--local') to mark an anchor
    // as LOCAL: still a real anchor (it nests and still bounds a 'descendants'
    // GLIMPSE) but HIDDEN from the top-level list_anchors enumeration. A LIST
    // for forward-compat + parity with the slice postfix design, EXACTLY as
    // note_session_tags is a list: the FIRST word is the default applied on
    // create (create_anchor local=true), and ALL words are recognized on read
    // (list_anchors / the descendants boundary). Distinct semantic domain from
    // the F8 slice postfixes (expand/suppress/deprecated) -- deliberately NOT
    // conflated. Seeded with ['local']; an emptied/malformed value falls back
    // to ['local'] at merge time so the local feature always has a usable word.
    note_session_local_postfixes: ['local'],
    // note_session_favorite_postfixes: the FAVORITE-ANCHOR postfix words
    // (stored as bare words, no leading '--', no '#'; same parse/sanitize as
    // the F8 postfix words via sanitizeF8PostfixWord). Joined to a base
    // anchor tag as base + '--' + word (e.g. 'wfy-ns' -> 'wfy-ns--fav') to
    // mark an anchor as a FAVORITE (starred): it stays in every listing AND
    // anchors the favorites shortlist the server's dock.list returns by
    // default (falling back to the full inventory, with a notice, when
    // nothing is starred). A LIST for forward-compat + parity with the
    // local-postfix design, EXACTLY as note_session_tags is a list: the
    // FIRST word is the default applied on create (dock.create/promote
    // favorite=true), and ALL words are recognized on read. Distinct
    // semantic domain from the F8 slice postfixes (expand/suppress/
    // deprecated) AND from the local-anchor words -- deliberately NOT
    // conflated (one tag per attribute; a favorite LOCAL anchor carries
    // both postfix tags). Seeded with ['fav']; an emptied/malformed value
    // falls back to ['fav'] at merge time so the favorites feature always
    // has a usable word.
    note_session_favorite_postfixes: ['fav'],
    // ── Tier-2 Apply-slice live-GUI-sync watcher timeout (Dan, July 2026) ─
    // After an 'Apply slice', the MODAL is correct (cache-sourced) but the live
    // Workflowy GUI is NOT yet in the sliced shape. The client installs a
    // short-lived 'active slice watcher' that, as the user scrolls the GUI,
    // fires each pending expand/collapse op by UUID (a nested collapse gated
    // until its enclosing expansion(s) have fired). This is the watcher's
    // auto-expiry, in SECONDS: after it elapses, ALL still-pending triggers are
    // disarmed and the GUI-sync countdown stops flashing (the modal stays
    // correct regardless). Default 30. Clamped to a sane [5, 600] range in the
    // merge; 0 is NOT allowed (a zero-timeout watcher would never fire).
    slice_watcher_timeout_seconds: 30,
    // ── Saved Tabs import: pause between tab opens (Dan, 2026-09-28, U01) ──
    // Seconds the Tier-2 drive waits between opening one tab and the next
    // (Dan's theory: opening Workflowy tabs too fast can trigger its
    // desktop-variant re-login). Whole seconds, clamped to [0, 60] in the
    // merge; 0 = no delay. Editable on the Options page and with the import
    // dialog's stepper; the drive re-reads it at every gap.
    saved_tabs_drive_delay_seconds: 5,
    // ── WSLIVE STEP 4 instrumentation toggle (Dan, 2026) ─────────────────
    // Opt-in-verbose correlated diagnostics for the broker/endpoint-liveness
    // path, mirrored from this synced setting into BOTH the service worker
    // (background.js: wsliveVerbose/logWslive) and the page client
    // (glimpse-cache-client.js: wsliveVerbose/logWslive), and matched on the
    // broker/MCP side by the WORKFLOWY_BROKER_VERBOSE env flag. SILENT BY
    // DEFAULT (open-source-clean); flip to true to emit [wslive] lines that
    // trace a reap -> ghost -> re-register chain across all three legs.
    // Boolean; anything truthy enables. Default false.
    wslive_verbose: false,
    // ── Anchor views (Dan, 2026) ──────────────────────────────────────────
    // Saved anchor views: named entries that render the complete subtree under
    // a stored root UUID, stopping at child anchors. Stored in
    // chrome.storage.local (NOT sync) under 'anchor_views_local' — NOT in this
    // synced settings blob. This default key is [] and is ALWAYS OVERRIDDEN by
    // the local-storage merge in background.js / adoptExtensionSettings, so it
    // is a placeholder for in-memory settings object shape compatibility only.
    // The actual data lives exclusively in local storage.
    anchor_views: [],
    // ── "The Rail" UI (designer F) ───────────────────────────────────────
    // glimpse_ui_theme: 'auto' follows Workflowy's page theme; 'light' / 'dark' force one.
    glimpse_ui_theme: 'auto',
    // glimpse_ui_outline: where the Reader's outline sits: 'auto' (beside on a wide deck), 'side', 'top'.
    glimpse_ui_outline: 'auto',
    // ── "The Rail, side by side" (ui-next/rail-side-by-side) ─────────────
    // glimpse_ui_push: the Reader and a pinned panel PUSH Workflowy aside (left/right docks)
    // instead of covering it. On by default (Dan's call); the deck's self-check falls back to the
    // overlay by itself when Workflowy's header or outline would not fit beside the panel.
    glimpse_ui_push: true,
    // glimpse_ui_flip: a slide-out opens on the Reader's OUTER side (towards the screen edge)
    // instead of its inner side. Off by default.
    glimpse_ui_flip: false,
    // glimpse_help_dismissed: ids of the first-run tips the user has dismissed
    // ('tip-rail', 'tip-docks', 'tip-slices'); [] shows them again.
    glimpse_help_dismissed: [],
  });

  // ---------------------------------------------------------------------------
  // Expand-tag-prefix helpers — shared so the options page, the main-world
  // client, and mergeExtensionSettings all agree on parse/format/sanitize.
  //
  // Canonical stored form: an array of non-empty strings, each WITHOUT a
  // leading '#'. The options page shows them comma-joined; the user may type
  // them with or without '#', with arbitrary surrounding whitespace.
  // ---------------------------------------------------------------------------

  // Sanitize a single prefix token. Returns the canonical (#-stripped,
  // trimmed) string, or null if it is empty or contains internal whitespace.
  function sanitizeExpandTagPrefix(token) {
    if (typeof token !== 'string') return null;
    let t = token.trim();
    if (!t) return null;
    // Allow (and strip) a single leading '#'. The canonical stored form has
    // no '#'; the F8 matcher compares against the '#'-prefixed tag text by
    // re-adding '#' at match time.
    if (t.startsWith('#')) t = t.slice(1);
    if (!t) return null;
    // Reject any internal whitespace (Workflowy tags cannot contain spaces).
    // Emojis and other non-space characters are deliberately allowed.
    if (/\s/.test(t)) return null;
    // A stray '#' anywhere other than the (already-stripped) leading position
    // is not a valid tag character; reject it rather than silently keeping it.
    if (t.includes('#')) return null;
    return t;
  }

  // Parse a user-entered comma-separated string into a canonical, de-duped
  // array of prefixes. Invalid tokens are dropped. Splitting on commas first
  // means surrounding whitespace per element is fine; only whitespace WITHIN
  // a token (after trimming) causes that token to be rejected.
  function parseExpandTagPrefixes(str) {
    if (Array.isArray(str)) {
      const out = [];
      for (const tok of str) {
        const clean = sanitizeExpandTagPrefix(tok);
        if (clean && !out.includes(clean)) out.push(clean);
      }
      return out;
    }
    if (typeof str !== 'string') return [];
    const out = [];
    for (const tok of str.split(',')) {
      const clean = sanitizeExpandTagPrefix(tok);
      if (clean && !out.includes(clean)) out.push(clean);
    }
    return out;
  }

  // Format a canonical prefix array back into the comma-separated string
  // shown in the options-page text field.
  function formatExpandTagPrefixes(arr) {
    if (!Array.isArray(arr)) return '';
    return arr.join(', ');
  }

  // ---------------------------------------------------------------------------
  // Expand-GROUP helpers (Dan, July 2026) — shared so the options page, the
  // main-world client, and mergeExtensionSettings all agree on group identity,
  // shape, and normalization.
  //
  // A group is { id, name, prefixes }. Identity is the STABLE id (minted by
  // genGroupId); active_group references an id, never a name. The widget /
  // options page edit name + prefixes; id is opaque and never user-visible.
  // ---------------------------------------------------------------------------
  // Mint a stable, opaque group id. Uses crypto.randomUUID where available
  // (service worker, modern page contexts), falling back to a time+random
  // string. Mirrors glimpse-cache-client.js's genId() but is defined here so
  // the shared module is self-contained in the SW and options-page contexts
  // (which do not load the main-world client).
  // @beacon[
  //   id=auto-beacon@genGroupId,
  //   role=function genGroupId(),
  //   slice_labels=nexus--config,
  //   kind=ast,
  //   comment=Mint a stable opaque expand-group id (crypto.randomUUID with time+random fallback); used so active_group references an id never a name,
  // ]
  function genGroupId() {
    const g = (typeof self !== 'undefined' && self.crypto && self.crypto.randomUUID)
      ? self.crypto.randomUUID()
      : (typeof crypto !== 'undefined' && crypto.randomUUID)
        ? crypto.randomUUID()
        : String(Date.now()) + '-' + Math.random().toString(36).slice(2);
    return 'grp-' + g;
  }

  // Build the shipped default group: one group named 'default' carrying the
  // single prefix 'wfy-'. Minted with a FRESH id each call (callers that want
  // a stable sentinel id can override). Used by normalizeExpandGroups when no
  // groups exist.
  // @beacon[
  //   id=auto-beacon@makeDefaultExpandGroup,
  //   role=function makeDefaultExpandGroup(),
  //   slice_labels=nexus--config,
  //   kind=ast,
  //   comment=Build the shipped default expand group { id, name:'default', prefixes:['wfy-'], root_uuid:'', description:'', default_sort_by:'none', default_sort_direction:'asc' } with a fresh id; used by normalizeExpandGroups when none exist,
  // ]
  function makeDefaultExpandGroup() {
    return {
      id: genGroupId(),
      name: 'default',
      prefixes: ['wfy-'],
      root_uuid: '',
      description: '',
      default_sort_by: 'none',
      default_sort_direction: 'asc',
    };
  }

  // Sanitize a single F8 POSTFIX WORD (the bare word joined as P + '--' +
  // word, e.g. 'deprecated'). Reuses the prefix-token validator: strips a
  // single leading '#', trims, rejects empty / internal-whitespace / embedded
  // '#'. Returns the canonical word, or null if unusable. Callers that want a
  // guaranteed value fall back to their shipped default on null.
  // @beacon[
  //   id=f8policy@sanitize-postfix-word,
  //   role=function sanitizeF8PostfixWord(token),
  //   slice_labels=nexus--config,
  //   kind=ast,
  //   comment=Sanitize one F8 postfix word (auto-expand/suppress/deprecated) via the shared prefix-token validator; returns canonical word or null.,
  // ]
  function sanitizeF8PostfixWord(token) {
    return sanitizeExpandTagPrefix(token);
  }

  // Sanitize a single group object into canonical { id, name, prefixes,
  // root_uuid, description, default_sort_by, default_sort_direction } form,
  // or return null if it cannot be salvaged into something useful. Rules:
  //   • id       — non-empty string; back-filled via genGroupId() if missing
  //                (forward-compat for hand-edited / legacy group objects).
  //   • name     — trimmed string; falls back to 'group' if empty (never
  //                rejected — a nameless group is still a usable group).
  //   • prefixes — run through parseExpandTagPrefixes (accepts array OR
  //                comma-separated string; '#'-stripped, de-duped, internal-
  //                whitespace tokens dropped). A group with ZERO valid
  //                prefixes is allowed to exist (it simply matches nothing on
  //                F8) so the user can build it up incrementally in the UI.
  //   • default_sort_by / default_sort_direction — per-slice saved default
  //                render sort. Missing/invalid legacy values back-fill to
  //                document order ('none'/'asc') so old groups keep today's
  //                behavior until the user opts in to chronological sorting.
  // @beacon[
  //   id=auto-beacon@sanitizeExpandGroup,
  //   role=function sanitizeExpandGroup(group),
  //   slice_labels=nexus--config,
  //   kind=ast,
  //   comment=Coerce one raw group object into canonical { id, name, prefixes, root_uuid, description, default_sort_by, default_sort_direction } (back-fills id, defaults empty name, sanitizes prefixes/default sort); zero-prefix groups are allowed,
  // ]
  function sanitizeExpandGroup(group) {
    if (!group || typeof group !== 'object') return null;
    const id = (typeof group.id === 'string' && group.id.trim())
      ? group.id.trim()
      : genGroupId();
    const name = (typeof group.name === 'string' && group.name.trim())
      ? group.name.trim()
      : 'group';
    const prefixes = parseExpandTagPrefixes(group.prefixes);
    // root_uuid (Dan, 2026): the Workflowy node UUID that scopes this slice
    // group's server-side search/F8 projection (Phase 1 starts the cache walk
    // here; Phase 2 treats it as the subtree root, so suppression ABOVE it is
    // inert). Stored verbatim (trimmed); empty string = unset. No UUID-shape
    // validation here — the server fails loudly if it cannot resolve it.
    // Back-fills '' on legacy group objects that predate this field.
    const root_uuid = (typeof group.root_uuid === 'string')
      ? group.root_uuid.trim()
      : '';
    // description (Dan, 2026): a free-text, multi-line human description of
    // what this slice group is for. Stored verbatim (only trimmed of outer
    // whitespace; INTERNAL newlines are preserved so multi-line descriptions
    // survive). Empty string = unset. Purely informational today (shown in the
    // UUID Explorer slice-group table as a first-line preview sub-row with an
    // inline editor); a forthcoming list_slice_groups MCP tool will surface it.
    // Back-fills '' on legacy group objects that predate this field.
    const description = (typeof group.description === 'string')
      ? group.description.trim()
      : '';
    // default_sort_* (Dan, SGSORT 2026): per-slice saved default render sort.
    // This is STORAGE ONLY at this layer: missing/invalid legacy fields are
    // back-filled to explicit document order ('none'/'asc'), preserving old
    // behavior exactly. The semantic distinction between request null (inherit)
    // and string 'none' (explicit document order) is handled later by the
    // RenderProjectionSpec/server resolve-point pipeline, never here.
    const _defaultSortByRaw = (typeof group.default_sort_by === 'string')
      ? group.default_sort_by.trim().toLowerCase()
      : '';
    const default_sort_by = (
      _defaultSortByRaw === 'none'
      || _defaultSortByRaw === 'created'
      || _defaultSortByRaw === 'modified'
    ) ? _defaultSortByRaw : 'none';
    const _defaultSortDirRaw = (typeof group.default_sort_direction === 'string')
      ? group.default_sort_direction.trim().toLowerCase()
      : '';
    const default_sort_direction = (_defaultSortDirRaw === 'desc') ? 'desc' : 'asc';
    return { id, name, prefixes, root_uuid, description, default_sort_by, default_sort_direction };
  }

  // Normalize a raw expand_groups value into a canonical, non-empty array of
  // sanitized groups. Drops unsalvageable entries; if NOTHING valid remains
  // (missing key, empty array, all-garbage), returns a single fresh default
  // group so F8 always has at least one group to act on.
  // @beacon[
  //   id=auto-beacon@normalizeExpandGroups,
  //   role=function normalizeExpandGroups(rawGroups),
  //   slice_labels=nexus--config,
  //   kind=ast,
  //   comment=Normalize a raw expand_groups value into a non-empty canonical array; auto-creates a fresh 'default' group when nothing valid remains so F8 always has a group,
  // ]
  function normalizeExpandGroups(rawGroups) {
    const out = [];
    if (Array.isArray(rawGroups)) {
      for (const g of rawGroups) {
        const clean = sanitizeExpandGroup(g);
        if (clean) out.push(clean);
      }
    }
    if (!out.length) out.push(makeDefaultExpandGroup());
    return out;
  }

  // Resolve the active-group id against a (already-normalized) groups array.
  // If the supplied id resolves to a live group, keep it; otherwise fall back
  // to the FIRST group's id. Returns the id to store in active_group.
  // @beacon[
  //   id=auto-beacon@resolveActiveGroupId,
  //   role=function resolveActiveGroupId(rawActiveId, groups),
  //   slice_labels=nexus--config,
  //   kind=ast,
  //   comment=Resolve an active-group id against a normalized groups array; keeps it if it resolves else falls back to the first group's id (active pointer can never dangle),
  // ]
  function resolveActiveGroupId(rawActiveId, groups) {
    if (typeof rawActiveId === 'string' && groups.some((g) => g.id === rawActiveId)) {
      return rawActiveId;
    }
    return groups.length ? groups[0].id : null;
  }

  // Resolve the canonical prefix array for the active group given a settings
  // object. The single source of truth used by F8 (getExpandTagPrefixes in
  // the client calls through here). Defensive: normalizes on the fly so it is
  // correct even if handed a partially-merged settings object. Falls back to
  // ['wfy-'] only if everything is somehow empty.
  // @beacon[
  //   id=auto-beacon@activeExpandPrefixesFromSettings,
  //   role=function activeExpandPrefixesFromSettings(settings),
  //   slice_labels=nexus--config,
  //   kind=ast,
  //   comment=THE single source of truth for F8: resolve the active group's canonical prefix array from a settings object (normalizes on the fly); falls back to ['wfy-'] only if everything is empty,
  // ]
  function activeExpandPrefixesFromSettings(settings) {
    const groups = normalizeExpandGroups(settings && settings.expand_groups);
    const activeId = resolveActiveGroupId(settings && settings.active_group, groups);
    const active = groups.find((g) => g.id === activeId) || groups[0];
    const prefixes = (active && Array.isArray(active.prefixes)) ? active.prefixes : [];
    return prefixes.length ? prefixes : ['wfy-'];
  }

  // ---------------------------------------------------------------------------
  // Anchor-view helpers (Dan, 2026) — parallel to the expand-group helpers but
  // for saved ANCHOR VIEWS. An anchor view is a named saved view that renders
  // the complete subtree under a stored root UUID, stopping at child anchors
  // (descend_policy='stop_at_child_anchors'). It has NO tag prefixes, NO
  // match_scope, and NO CPY/search-string — just a root UUID + optional
  // description + default sort. Stored in chrome.storage.local (NOT sync) under
  // a SEPARATE key (anchor_views_local), completely orthogonal to
  // expand_groups. The canonical object is:
  //   { id, name, root_uuid, description, default_sort_by, default_sort_direction }
  // Identity is the STABLE id (minted by genAnchorViewId). No active-view
  // pointer is needed (there is no F8 analog for anchor views).
  // ---------------------------------------------------------------------------

  // Mint a stable, opaque anchor-view id. Uses crypto.randomUUID where
  // available, falling back to a time+random string. Prefixed 'ancv-' to
  // distinguish from expand-group ids ('grp-') in logs and debugging.
  // @beacon[
  //   id=auto-beacon@genAnchorViewId,
  //   role=function genAnchorViewId(),
  //   slice_labels=nexus--config,
  //   kind=ast,
  //   comment=Mint a stable opaque anchor-view id (ancv- + crypto.randomUUID); parallels genGroupId for expand groups,
  // ]
  function genAnchorViewId() {
    const g = (typeof self !== 'undefined' && self.crypto && self.crypto.randomUUID)
      ? self.crypto.randomUUID()
      : (typeof crypto !== 'undefined' && crypto.randomUUID)
        ? crypto.randomUUID()
        : String(Date.now()) + '-' + Math.random().toString(36).slice(2);
    return 'ancv-' + g;
  }

  // Build a fresh empty anchor view. Used by the + anchor button. Unlike
  // expand groups, an empty anchor_views array is a LEGITIMATE state (the user
  // may not have created any yet), so normalizeAnchorViews returns [] rather
  // than auto-creating a default. This function is only called by the UI's
  // add-button.
  // @beacon[
  //   id=auto-beacon@makeDefaultAnchorView,
  //   role=function makeDefaultAnchorView(),
  //   slice_labels=nexus--config,
  //   kind=ast,
  //   comment=Build a fresh empty anchor view {id,name:'new anchor',root_uuid:'',...} with a fresh genAnchorViewId; used by the + Add anchor button,
  // ]
  function makeDefaultAnchorView() {
    return {
      id: genAnchorViewId(),
      name: 'new anchor',
      root_uuid: '',
      description: '',
      default_sort_by: 'none',
      default_sort_direction: 'asc',
    };
  }

  // Sanitize a single anchor-view object into canonical
  // { id, name, root_uuid, description, default_sort_by, default_sort_direction }
  // form, or return null if it cannot be salvaged. Rules:
  //   • id       — non-empty string; back-filled via genAnchorViewId() if missing.
  //   • name     — trimmed string; falls back to 'anchor' if empty.
  //   • root_uuid — trimmed string; empty string = unset (the server fails loudly
  //                if it cannot resolve it). No UUID-shape validation here.
  //   • description — trimmed string (internal newlines preserved); empty = unset.
  //   • default_sort_by / default_sort_direction — same back-fill rules as
  //                expand groups (missing/invalid -> 'none'/'asc').
  // There are NO tag prefixes and NO match_scope for anchor views.
  // @beacon[
  //   id=auto-beacon@sanitizeAnchorView,
  //   role=function sanitizeAnchorView(view),
  //   slice_labels=nexus--config,
  //   kind=ast,
  //   comment=Coerce one raw anchor-view object into canonical {id,name,root_uuid,description,default_sort_by,default_sort_direction} (back-fills id, defaults empty name, sanitizes default sort); parallels sanitizeExpandGroup but has NO prefixes,
  // ]
  function sanitizeAnchorView(view) {
    if (!view || typeof view !== 'object') return null;
    const id = (typeof view.id === 'string' && view.id.trim())
      ? view.id.trim()
      : genAnchorViewId();
    const name = (typeof view.name === 'string' && view.name.trim())
      ? view.name.trim()
      : 'anchor';
    const root_uuid = (typeof view.root_uuid === 'string')
      ? view.root_uuid.trim()
      : '';
    const description = (typeof view.description === 'string')
      ? view.description.trim()
      : '';
    const _defaultSortByRaw = (typeof view.default_sort_by === 'string')
      ? view.default_sort_by.trim().toLowerCase()
      : '';
    const default_sort_by = (
      _defaultSortByRaw === 'none'
      || _defaultSortByRaw === 'created'
      || _defaultSortByRaw === 'modified'
    ) ? _defaultSortByRaw : 'none';
    const _defaultSortDirRaw = (typeof view.default_sort_direction === 'string')
      ? view.default_sort_direction.trim().toLowerCase()
      : '';
    const default_sort_direction = (_defaultSortDirRaw === 'desc') ? 'desc' : 'asc';
    return { id, name, root_uuid, description, default_sort_by, default_sort_direction };
  }

  // Normalize a raw anchor_views value into a canonical array of sanitized
  // anchor views. Drops unsalvageable entries. Unlike normalizeExpandGroups,
  // an empty result is returned as-is (an empty array is a legitimate state —
  // the user has not created any anchor views yet). No default is auto-created.
  // @beacon[
  //   id=auto-beacon@normalizeAnchorViews,
  //   role=function normalizeAnchorViews(rawViews),
  //   slice_labels=nexus--config,
  //   kind=ast,
  //   comment=Normalize a raw anchor_views value into a canonical array (empty is legitimate, no default auto-created); parallels normalizeExpandGroups but for anchor views,
  // ]
  function normalizeAnchorViews(rawViews) {
    const out = [];
    if (Array.isArray(rawViews)) {
      for (const v of rawViews) {
        const clean = sanitizeAnchorView(v);
        if (clean) out.push(clean);
      }
    }
    return out;
  }

  // ---------------------------------------------------------------------------
  // Marker-tag helpers — shared so the options page and the main-world client
  // agree on parse/format/sanitize. Marker tags follow the SAME canonical form
  // and sanitization rules as expand-tag prefixes (stored '#'-less; emojis
  // allowed; empty / internal-whitespace / stray-'#' tokens rejected), so we
  // reuse sanitizeExpandTagPrefix as the single token validator.
  // ---------------------------------------------------------------------------

  // Sanitize a single marker tag token. Alias of sanitizeExpandTagPrefix:
  // identical rules (strip a single leading '#', reject empty / internal
  // whitespace / embedded '#', allow emojis).
  function sanitizeMarkerTag(token) {
    return sanitizeExpandTagPrefix(token);
  }

  // Parse a user-entered comma-separated string (or array) into a canonical,
  // de-duped array of marker tags.
  function parseMarkerTags(str) {
    return parseExpandTagPrefixes(str);
  }

  // Format a canonical marker-tag array back into the comma-separated string
  // shown in the options-page text field.
  function formatMarkerTags(arr) {
    return formatExpandTagPrefixes(arr);
  }

  function mergeExtensionSettings(userSettings) {
    const out = Object.assign({}, DEFAULT_EXTENSION_SETTINGS);
    if (userSettings && typeof userSettings === 'object') {
      const raw = userSettings.max_glimpse_payload_kb;
      const n = Number(raw);
      // Clamp to a sane public-extension range. 64 KB minimum prevents
      // accidental unusably-low values; 10240 KB (10 MB) leaves room for power
      // users while making them explicitly opt into huge payloads.
      if (Number.isFinite(n)) {
        out.max_glimpse_payload_kb = Math.max(64, Math.min(10240, Math.round(n)));
      }
      // Markdown preview popup width. Clamp to a sane range: 280px floor
      // (narrower than the popup's own min-width would be pointless) and
      // 2000px ceiling (well beyond any realistic monitor's usable width;
      // the viewport clamp handles the rest).
      const w = Number(userSettings.md_preview_width_px);
      if (Number.isFinite(w)) {
        out.md_preview_width_px = Math.max(280, Math.min(2000, Math.round(w)));
      }
      // Docked panel: boolean dock flag, panel width, and list/render split
      // fraction. Each is independently validated and clamped so a malformed
      // stored value can never break the panel layout.
      if (userSettings.md_preview_docked !== undefined) {
        out.md_preview_docked = !!userSettings.md_preview_docked;
      }
      const dw = Number(userSettings.md_preview_dock_width_px);
      if (Number.isFinite(dw)) {
        out.md_preview_dock_width_px = Math.max(320, Math.min(2400, Math.round(dw)));
      }
      // Dock position: one of the four sides, or 'auto' (the deck picks: bottom on a
      // window taller than wide, so pickers get the width; right otherwise).
      const dp = String(userSettings.md_preview_dock_position || '').toLowerCase();
      if (dp === 'right' || dp === 'left' || dp === 'top' || dp === 'bottom' || dp === 'auto') {
        out.md_preview_dock_position = dp;
      }
      // Dock height (for top/bottom docking).
      const dh = Number(userSettings.md_preview_dock_height_px);
      if (Number.isFinite(dh)) {
        out.md_preview_dock_height_px = Math.max(200, Math.min(1600, Math.round(dh)));
      }
      const lf = Number(userSettings.md_preview_dock_list_fraction);
      if (Number.isFinite(lf)) {
        out.md_preview_dock_list_fraction = Math.max(0.12, Math.min(0.88, lf));
      }
      // Docked-panel top offset (px). Floor at 0 (flush to top) and a generous
      // 1000px ceiling; the client additionally clamps to the live viewport
      // height at render time so the panel never collapses to nothing.
      const to = Number(userSettings.md_preview_dock_top_offset_px);
      if (Number.isFinite(to)) {
        out.md_preview_dock_top_offset_px = Math.max(0, Math.min(1000, Math.round(to)));
      }
      if (userSettings.md_preview_dock_hidden !== undefined) {
        out.md_preview_dock_hidden = !!userSettings.md_preview_dock_hidden;
      }
      // MD preview char cap. Floor at 1000 so a tiny value can't disable the
      // preview; no hard ceiling (Dan intentionally previews huge files).
      const mc = Number(userSettings.md_preview_max_chars);
      if (Number.isFinite(mc)) {
        out.md_preview_max_chars = Math.max(1000, Math.round(mc));
      }
      // Docked-modal pre-render threshold. 0 = always on-demand (no all-rows
      // pre-render); no hard upper ceiling (mirrors md_preview_max_chars).
      const mpc = Number(userSettings.md_modal_prerender_max_chars);
      if (Number.isFinite(mpc)) {
        out.md_modal_prerender_max_chars = Math.max(0, Math.round(mpc));
      }
      // Tier-2 Apply-slice live-GUI-sync watcher timeout (seconds). Clamp to
      // [5, 600]: a floor of 5s so the watcher is never effectively-instant,
      // a 10-minute ceiling so a stray huge value can't keep a watcher armed
      // indefinitely. 0/garbage falls back to the shipped 30s default.
      const swt = Number(userSettings.slice_watcher_timeout_seconds);
      if (Number.isFinite(swt)) {
        out.slice_watcher_timeout_seconds = Math.max(5, Math.min(600, Math.round(swt)));
      }
      // Saved Tabs drive inter-tab delay (seconds, U01). Clamp to [0, 60]:
      // 0 = no delay; a one-minute ceiling so a stray value can't stall a
      // ten-tab import for ten minutes. Garbage falls back to the shipped 5 s.
      const stdd = Number(userSettings.saved_tabs_drive_delay_seconds);
      if (Number.isFinite(stdd)) {
        out.saved_tabs_drive_delay_seconds = Math.max(0, Math.min(60, Math.round(stdd)));
      }
      // WSLIVE STEP 4 instrumentation toggle (boolean). Preserve an explicit
      // user value; absent -> the shipped default (false). Anything truthy
      // enables the [wslive] correlated diagnostics in the SW + page legs.
      if (userSettings.wslive_verbose !== undefined) {
        out.wslive_verbose = !!userSettings.wslive_verbose;
      }
      // Anchor views (Dan, 2026). Normalize via the shared helper. The actual
      // data lives in chrome.storage.local and is merged into the in-memory
      // settings object by background.js on load; this merge call handles the
      // case where the in-memory settings already carry a local copy (e.g. a
      // partially-merged settings object). An empty array is legitimate.
      if (userSettings.anchor_views !== undefined) {
        out.anchor_views = normalizeAnchorViews(userSettings.anchor_views);
      }
      // (Timestamp-sort mode/direction, `since`, and headings-only are NO
      // LONGER synced settings — they moved to per-tab sessionStorage in the
      // July-2026 de-globalization, so there is nothing to sanitize here. See
      // the defaults note above + getTabModalState in glimpse-cache-client.js.)
      // Expand-tag prefixes. Accept either a canonical array or a raw
      // comma-separated string; sanitize either way. Only override the
      // default when the user actually provided at least one valid prefix,
      // so a malformed/empty value falls back to the shipped default
      // rather than disabling F8 entirely.
      if (userSettings.expand_tag_prefixes !== undefined) {
        const prefixes = parseExpandTagPrefixes(userSettings.expand_tag_prefixes);
        if (prefixes.length) {
          out.expand_tag_prefixes = prefixes;
        }
      }
      // F8 expand-tag-prefix GROUPS. ALWAYS normalize (even when the user
      // never supplied the key) so the auto-create-default-if-empty guarantee
      // holds: normalizeExpandGroups returns a fresh 'default' group ['wfy-']
      // when expand_groups is missing/empty/garbage, and back-fills a stable
      // id on any group object lacking one. active_group is then resolved
      // against the normalized groups by id, falling back to the first
      // group's id if it doesn't resolve (so the active pointer can never
      // dangle). Unlike the legacy flat list above, this runs unconditionally
      // because the default itself must be materialized into the merged
      // settings the client/options page read.
      out.expand_groups = normalizeExpandGroups(userSettings.expand_groups);
      out.active_group = resolveActiveGroupId(userSettings.active_group, out.expand_groups);
      // F8 SLICE POSTFIX WORDS (auto-expand / suppress / deprecated). Each is
      // a single bare word; sanitize via the shared token validator. An
      // empty/whitespace/invalid value falls back to the shipped default so
      // F8 always has a usable postfix vocabulary (the server joins them as
      // P + '--' + word).
      {
        const _ae = sanitizeF8PostfixWord(userSettings.f8_postfix_auto_expand);
        if (_ae) out.f8_postfix_auto_expand = _ae;
        const _su = sanitizeF8PostfixWord(userSettings.f8_postfix_suppress);
        if (_su) out.f8_postfix_suppress = _su;
        const _dp = sanitizeF8PostfixWord(userSettings.f8_postfix_deprecated);
        if (_dp) out.f8_postfix_deprecated = _dp;
      }
      // F8 GLOBAL NEVER-EXPAND TAGS (WS-1.9). Accept array or comma string;
      // sanitize like prefixes. An EMPTY list is legitimate (no global
      // suppressors), so honor an explicitly-provided empty result rather
      // than falling back — mirroring global_tags, not the postfix words.
      if (userSettings.f8_global_never_expand_tags !== undefined) {
        out.f8_global_never_expand_tags =
          parseExpandTagPrefixes(userSettings.f8_global_never_expand_tags);
      }
      // Marker tags. Same accept-array-or-string + sanitize behavior, and the
      // same fall-back-to-default-on-empty discipline so F10 always has at
      // least one marker tag to toggle.
      if (userSettings.marker_tags !== undefined) {
        const markers = parseMarkerTags(userSettings.marker_tags);
        if (markers.length) {
          out.marker_tags = markers;
        }
      }
      // Tab-rename-on-alias toggle. Plain boolean coercion; any stored value
      // is acceptable, so this never falls back to a 'malformed' default.
      if (userSettings.rename_tab_with_alias !== undefined) {
        out.rename_tab_with_alias = !!userSettings.rename_tab_with_alias;
      }
      // Tab-title affix word. Trim surrounding whitespace; an empty/whitespace
      // value falls back to the shipped default so the title is never just
      // ' - <alias>'. Any non-empty string (including multi-word) is allowed.
      if (userSettings.tab_title_affix !== undefined) {
        const affix = typeof userSettings.tab_title_affix === 'string'
          ? userSettings.tab_title_affix.trim()
          : '';
        if (affix) {
          out.tab_title_affix = affix;
        }
      }
      // Affix position. Only 'prefix' or 'suffix' are valid; anything else
      // (including a malformed stored value) normalizes to 'prefix'.
      if (userSettings.tab_title_affix_position !== undefined) {
        out.tab_title_affix_position =
          userSettings.tab_title_affix_position === 'suffix' ? 'suffix' : 'prefix';
      }
      // Default Markdown folder (F12+4). Trim surrounding whitespace; store
      // verbatim. An empty/whitespace value is allowed and means "unset" (the
      // shipped default), so — unlike the affix word — we do NOT fall back to
      // a non-empty default. No path-shape validation here; the server checks
      // that the directory exists on disk at materialize time.
      if (userSettings.default_markdown_folder !== undefined) {
        out.default_markdown_folder =
          typeof userSettings.default_markdown_folder === 'string'
            ? userSettings.default_markdown_folder.trim()
            : '';
      }
      // Archive folder (Archive Reads). Trim surrounding whitespace; store
      // verbatim; non-string falls back to the shipped '' (archive reads
      // off). Same shape as default_markdown_folder — the daemon validates
      // the path when it lists the folder.
      if (userSettings.archive_folder !== undefined) {
        out.archive_folder =
          typeof userSettings.archive_folder === 'string'
            ? userSettings.archive_folder.trim()
            : '';
      }
      // Note-session tags. Accept array or comma-separated string; sanitize
      // via the shared prefix parser (canonical, '#'-less, de-duped). Only
      // override the default when at least one valid tag is provided, so a
      // malformed/empty value falls back to the seeded ['wfy-ns'] rather than
      // leaving the system unable to identify any note sessions.
      if (userSettings.note_session_tags !== undefined) {
        const tags = parseExpandTagPrefixes(userSettings.note_session_tags);
        if (tags.length) {
          out.note_session_tags = tags;
        }
      }
      // Global tags. Same accept-array-or-string + sanitize, but an EMPTY
      // list is a legitimate value (no global tags known yet), so — unlike
      // note_session_tags — we honor an explicitly-provided empty result
      // instead of falling back to a default.
      if (userSettings.global_tags !== undefined) {
        out.global_tags = parseExpandTagPrefixes(userSettings.global_tags);
      }
      // Default note-sessions parent UUID. Trim; store verbatim. Empty string
      // = unset (the create_note_session MCP tool fails loudly rather than
      // falling back to a root). No UUID-shape validation here; the options
      // page renders the resolved path for the user to confirm.
      if (userSettings.default_note_sessions_parent !== undefined) {
        out.default_note_sessions_parent =
          typeof userSettings.default_note_sessions_parent === 'string'
            ? userSettings.default_note_sessions_parent.trim()
            : '';
      }
      // Project Maps location. Same shape as default_note_sessions_parent:
      // trim surrounding whitespace, store verbatim, empty = none. It is only
      // a pointer — never resolved, validated or interpreted, here or
      // anywhere server-side.
      if (userSettings.project_maps_location !== undefined) {
        out.project_maps_location =
          typeof userSettings.project_maps_location === 'string'
            ? userSettings.project_maps_location.trim()
            : '';
      }
      // LOCAL-ANCHOR postfix words. Accept array OR comma-separated string;
      // sanitize each token via sanitizeF8PostfixWord (the SAME validator the
      // F8 postfix words use: strips a single leading '#', trims, rejects
      // empty / internal-whitespace / embedded '#'). De-dupe, order-preserving.
      // Fall back to the seeded ['local'] when nothing valid is provided so
      // the local-anchor feature always has a usable postfix word (mirrors the
      // fallback-to-default discipline of the F8 postfix words + note_session_
      // tags, NOT the honor-empty discipline of global_tags -- an empty local
      // postfix set would silently disable local anchors).
      if (userSettings.note_session_local_postfixes !== undefined) {
        const raw = userSettings.note_session_local_postfixes;
        const arr = Array.isArray(raw)
          ? raw
          : (typeof raw === 'string' ? raw.split(',') : []);
        const words = [];
        const seen = new Set();
        for (const tok of arr) {
          const w = sanitizeF8PostfixWord(tok);
          if (w && !seen.has(w)) {
            seen.add(w);
            words.push(w);
          }
        }
        if (words.length) {
          out.note_session_local_postfixes = words;
        }
      }
      // FAVORITE-ANCHOR postfix words. Accept array OR comma-separated
      // string; sanitize each token via sanitizeF8PostfixWord (the SAME
      // validator the F8 postfix words + the local-anchor words use: strips
      // a single leading '#', trims, rejects empty / internal-whitespace /
      // embedded '#'). De-dupe, order-preserving. Fall back to the seeded
      // ['fav'] when nothing valid is provided so the favorites feature
      // always has a usable postfix word (mirrors the fallback-to-default
      // discipline of the local-anchor words above, NOT the honor-empty
      // discipline of global_tags -- an empty favorite-postfix set would
      // silently disable favorites).
      if (userSettings.note_session_favorite_postfixes !== undefined) {
        const raw = userSettings.note_session_favorite_postfixes;
        const arr = Array.isArray(raw)
          ? raw
          : (typeof raw === 'string' ? raw.split(',') : []);
        const words = [];
        const seen = new Set();
        for (const tok of arr) {
          const w = sanitizeF8PostfixWord(tok);
          if (w && !seen.has(w)) {
            seen.add(w);
            words.push(w);
          }
        }
        if (words.length) {
          out.note_session_favorite_postfixes = words;
        }
      }
      // "The Rail" UI preferences: closed vocabularies, default on anything else.
      if (userSettings.glimpse_ui_theme !== undefined) {
        const t = String(userSettings.glimpse_ui_theme || '').toLowerCase();
        out.glimpse_ui_theme = (t === 'light' || t === 'dark') ? t : 'auto';
      }
      if (userSettings.glimpse_ui_outline !== undefined) {
        const o = String(userSettings.glimpse_ui_outline || '').toLowerCase();
        out.glimpse_ui_outline = (o === 'side' || o === 'top') ? o : 'auto';
      }
      // "The Rail, side by side": two booleans and the dismissed-tips list.
      if (userSettings.glimpse_ui_push !== undefined) {
        out.glimpse_ui_push = !!userSettings.glimpse_ui_push;
      }
      if (userSettings.glimpse_ui_flip !== undefined) {
        out.glimpse_ui_flip = !!userSettings.glimpse_ui_flip;
      }
      if (userSettings.glimpse_help_dismissed !== undefined) {
        const known = ['tip-rail', 'tip-docks', 'tip-slices'];
        const raw = Array.isArray(userSettings.glimpse_help_dismissed) ? userSettings.glimpse_help_dismissed : [];
        const seen = [];
        raw.forEach((id) => { const s = String(id || '').trim(); if (known.includes(s) && !seen.includes(s)) seen.push(s); });
        out.glimpse_help_dismissed = seen;
      }
    }
    return out;
  }

  // Compose the browser-tab title from an affix word, a position, and the
  // alias. Shared so the main-world client and the options-page live example
  // render the EXACT same string. Falls back to sane defaults for empty input.
  //   position 'prefix' -> '<affix> - <alias>'
  //   position 'suffix' -> '<alias> - <affix>'
  function composeTabTitle(affix, position, alias) {
    const a = (typeof affix === 'string' && affix.trim()) ? affix.trim() : 'Workflowy';
    const al = (typeof alias === 'string') ? alias : '';
    return position === 'suffix' ? `${al} - ${a}` : `${a} - ${al}`;
  }

  /**
   * Return the default config map { action_name: binding_string, ... }.
   */
  // @beacon[
  //   id=auto-beacon@__lambdao_1.defaultHotkeyConfig-5cnb,
  //   role=function defaultHotkeyConfig(),
  //   slice_labels=f12-popup,
  //   kind=ast,
  // ]
  function defaultHotkeyConfig() {
    const out = {};
    for (const [actionName, meta] of Object.entries(HOTKEY_ACTIONS)) {
      out[actionName] = meta.default;
    }
    return out;
  }

  /**
   * Merge user overrides on top of defaults. Unknown action names in the
   * user config are dropped silently (forward-compat: ignore actions this
   * version doesn't recognize). Missing actions fall back to default.
   */
  // @beacon[
  //   id=auto-beacon@__lambdao_1.mergeHotkeyConfig-1yqw,
  //   role=function mergeHotkeyConfig(userConfig),
  //   slice_labels=f12-popup,
  //   kind=ast,
  // ]
  function mergeHotkeyConfig(userConfig) {
    const out = defaultHotkeyConfig();
    if (userConfig && typeof userConfig === 'object') {
      for (const actionName of Object.keys(out)) {
        const override = userConfig[actionName];
        if (typeof override === 'string' && override.length > 0) {
          out[actionName] = override;
        }
      }
    }
    return out;
  }

  /**
   * Normalize a single key token. Letters become uppercase ("k" → "K"),
   * function keys stay as-is, special keys keep their KeyboardEvent.key
   * spelling.
   */
  function normalizeKeyToken(token) {
    if (!token) return '';
    const t = String(token).trim();
    if (t.length === 0) return '';
    if (t.length === 1) return t.toUpperCase();
    // Function keys: F1..F24 — canonicalize the case.
    if (/^[fF]\d{1,2}$/.test(t)) return 'F' + t.slice(1);
    // Special keys (Enter, Escape, Tab, ArrowUp, etc.) — return as-is.
    return t;
  }

  /**
   * Parse a binding string like "Ctrl+Shift+F12" into:
   *   { ctrl: true, alt: false, shift: true, meta: false, key: 'F12' }
   *
   * Returns null on parse failure.
   */
  function parseHotkey(bindingString) {
    if (typeof bindingString !== 'string') return null;
    const parts = bindingString
      .split('+')
      .map((p) => p.trim())
      .filter((p) => p.length > 0);
    if (parts.length === 0) return null;

    const result = { ctrl: false, alt: false, shift: false, meta: false, key: '' };

    for (let i = 0; i < parts.length; i++) {
      const part = parts[i];
      const partLower = part.toLowerCase();
      const isLast = i === parts.length - 1;

      if (!isLast && (partLower === 'ctrl' || partLower === 'control')) {
        result.ctrl = true;
      } else if (!isLast && partLower === 'alt') {
        result.alt = true;
      } else if (!isLast && partLower === 'shift') {
        result.shift = true;
      } else if (!isLast && (partLower === 'meta' || partLower === 'cmd' || partLower === 'command' || partLower === 'win')) {
        result.meta = true;
      } else if (isLast) {
        result.key = normalizeKeyToken(part);
      } else {
        // Unknown modifier token.
        return null;
      }
    }

    if (!result.key) return null;
    return result;
  }

  /**
   * Build a canonical binding string from a captured KeyboardEvent.
   * Returns null if the event is just a bare modifier press (no real key).
   */
  function formatHotkey(event) {
    if (!event) return null;
    const key = event.key;
    if (!key) return null;
    // Ignore bare modifier-key events (when the user presses just Ctrl alone).
    if (key === 'Control' || key === 'Alt' || key === 'Shift' || key === 'Meta') {
      return null;
    }
    const parts = [];
    if (event.ctrlKey) parts.push('Ctrl');
    if (event.altKey) parts.push('Alt');
    if (event.shiftKey) parts.push('Shift');
    if (event.metaKey) parts.push('Meta');
    parts.push(normalizeKeyToken(key));
    return parts.join('+');
  }

  /**
   * Does this KeyboardEvent match the given binding string?
   * Returns false on any parse/match failure (safe default).
   */
  function matchesHotkey(event, bindingString) {
    if (!event) return false;
    const parsed = parseHotkey(bindingString);
    if (!parsed) return false;
    if (!!event.ctrlKey !== parsed.ctrl) return false;
    if (!!event.altKey !== parsed.alt) return false;
    if (!!event.shiftKey !== parsed.shift) return false;
    if (!!event.metaKey !== parsed.meta) return false;
    return normalizeKeyToken(event.key) === parsed.key;
  }

  /**
   * Is this binding one that Chrome will intercept before the page sees it?
   * Only flags BARE function keys reserved by Chrome — any binding with
   * a modifier prefix is safe.
   */
  function isHotkeyReserved(bindingString) {
    const parsed = parseHotkey(bindingString);
    if (!parsed) return false;
    if (parsed.ctrl || parsed.alt || parsed.shift || parsed.meta) return false;
    return RESERVED_BARE_KEYS.has(parsed.key);
  }

  // ---------------------------------------------------------------------------
  // Export — exposed on whichever global the loader uses. In a Chrome
  // content script (MAIN world) this is `window`. In the options page
  // it's also `window`. In the service worker it's `self`.
  // ---------------------------------------------------------------------------
  const api = {
    HOTKEY_ACTIONS,
    RESERVED_BARE_KEYS,
    defaultHotkeyConfig,
    mergeHotkeyConfig,
    parseHotkey,
    formatHotkey,
    matchesHotkey,
    isHotkeyReserved,
    normalizeKeyToken,
    DEFAULT_EXTENSION_SETTINGS,
    mergeExtensionSettings,
    sanitizeExpandTagPrefix,
    parseExpandTagPrefixes,
    formatExpandTagPrefixes,
    // F8 expand-group helpers (Dan, July 2026)
    genGroupId,
    makeDefaultExpandGroup,
    sanitizeExpandGroup,
    normalizeExpandGroups,
    resolveActiveGroupId,
    activeExpandPrefixesFromSettings,
    // Anchor-view helpers (Dan, 2026)
    genAnchorViewId,
    makeDefaultAnchorView,
    sanitizeAnchorView,
    normalizeAnchorViews,
    sanitizeF8PostfixWord,
    sanitizeMarkerTag,
    parseMarkerTags,
    formatMarkerTags,
    composeTabTitle,
  };

  root.GlimpseHotkeys = api;
})(typeof self !== 'undefined' ? self : this);
