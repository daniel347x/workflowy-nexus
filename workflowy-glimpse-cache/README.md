# NEXUS for Workflowy (the GLIMPSE extension)

*Workflowy context management, with your agents.*

Chrome (MV3) extension that gives the NEXUS Workflowy MCP server a fast, DOM-level view of whatever you currently have expanded in Workflowy, renders any subtree, slice group or Anchor Dock as one Markdown document inside the Workflowy page, and adds the in-page keyboard gestures that make NEXUS/Cartographer work fluid.

This branch (`ui-next/rail-side-by-side`) carries **"The Rail, side by side"**: Fable's rail-and-Reader interface ("The Rail"), with persistent panes that push Workflowy aside instead of covering it, pickers that open as wide sheets beside the Reader, and a self-describing help layer. The capabilities and every agent-facing contract are unchanged; the chrome is new (design note: `docs/ui-bakeoff/rail-side-by-side.md`).

## What it adds to Workflowy

When this extension is installed and you have `https://workflowy.com` open in Chrome:

- **`workflowy_glimpse()` MCP tool** returns `_source: "websocket"`: the server reads the visible tree directly from your browser's DOM instead of fetching the entire vault over the API.
- **The rail**: a narrow vertical bar fixed at the page edge (right by default; left, top, bottom or `Auto`, which docks at the bottom on a window taller than wide). Its connection dot shows the broker socket state; its icons open **the deck**: the Reader alone, a panel alone, or both with a divider. On a left or right dock the Reader and a pinned panel **push Workflowy aside** (the page narrows beside them; a Settings switch makes the deck cover the page instead, and a built-in self-check falls back to covering if Workflowy's header or outline would not fit). The Anchor Docks, Slice groups and Lightning Rod pickers open as a **wide sheet beside the Reader**, over Workflowy and never over the Reader, as wide as the names need; the sheet closes after a pick, and a pin keeps it open as a narrow summary with a widen button. Every drawer title carries a five-word byline; Anchor Docks, Slice groups and Jobs open with one sentence and a *More…* link into the Help drawer; three dismissible first-run tips introduce the rail, Anchor Docks and slice groups.
  - **Reader** (*what your agent sees, rendered*): the Markdown document for the node you click in Workflowy, a slice group, an Anchor Dock, a Lightning Rod search, or whatever an agent pushes (`push_to_modal`). The outline (one row per node) sits beside the document; Rendered | Source modes; find (Ctrl+F while the Reader has focus); Freeze, Refresh, Sync View; a View popover (sort, direction, headings-only, `since`, outline position, dock side); a Copy menu; Tags… for a multi-selection. An empty Reader says what it is and where to start.
  - **Anchor Docks** (*where things go*): your Docks as a tree with expand/collapse carets, a filter box, Favorites / Local / Descend toggles, a ⭐ toggle and copy-handle buttons per row. Every name is shown whole: one line when it fits, otherwise its head plus a small grey second line, never an ellipsis; the Dock's description is the row's tooltip. Click a Dock to open it in the Reader.
  - **Slice groups** (*what you look at*): your slice groups as the nested tree their pipe-path names define; beneath each name, what defines the group: its tags, its default sort, and its root as the 8-hex handle plus the root's title (trailing tags stripped by the daemon's `resolve_node_titles`; the handle alone on an older daemon). Apply opens a popover (1 = Apply, 2 = Apply + make this tab default, Copy search terms); an inline editor, export/import and the syntax help live in the drawer menu.
  - **Lightning Rod** (*one-off search, nothing saved*): text or tag query, title or title+body, global or scoped to the frozen view; Gather tags; Dyn slice.
  - **Tabs** (*name this tab for your agents*): this tab's alias (what agents pass as `target_tab`) and the Default-tab toggle; Saved Tabs export, import and Dock picker.
  - **Jobs** (*what the daemon is doing*): live Cartographer jobs with per-job Cancel, Copy status and Clear history; Refresh cache and its 60 s Skip window; the Activity log (every status line the extension produces, newest first, copyable). Lists poll only while the tab is visible and show outcome labels plus “N older jobs not shown” when history is omitted. On a problem row, one Details click fetches errors, warnings and findings for this tab only, with Load more for the rest.
  - **Settings** (theme, outline position, dock side, pushed aside or covered, the panel side, the first-run tips reset, the read-only global tags) and **Help** (the in-app README: what each part is and how it fits together, reached from any drawer's `?` or *More…*; the key cheat sheet; the F12 menu numbers).
- **Command palette** (Ctrl+Shift+K, rebindable): every command by name, including *Go to Dock…* and *Apply slice group…* (type-to-go, with full names) and *Resolve a UUID path…*, which writes a node's full path into the Activity log. **Alt+N** (rebindable) toggles the deck.
- **F2 / F3**: copy the full ancestor path of the hovered node (with UUIDs at every level / only at the leaf) to the clipboard.
- **F8**: expand every visible `#nexus--*` subtree in place.
- **F9**: open the corresponding source file in Windsurf at the exact line for any Cartographer-mapped node.
- **Ctrl+F10**: toggle a marker tag on the hovered node.
- **Ctrl+F12**: the Cartographer menu for the hovered node: file/folder refresh (F12+1), fast tag-sync (F12+2), Markdown full regenerate (F12+3), plus the bulk operations the target class allows.
- **Ctrl+F1**: clear and close the Reader.

Bindings are customizable on the extension's Options page, which also holds the broker token, limits, F8 postfix words, note-session tags and the deck's appearance settings.

## Architecture

Three worlds, one seam.

- `background.js` is the MV3 service worker. It owns the one real WebSocket to the local NEXUS broker (`ws://localhost:8765`), registers each Workflowy tab as an endpoint (alias, default tab), and owns `chrome.storage` (settings, hotkeys, slice groups, the broker token).
- `content-bridge.js` runs in the isolated world and relays `window.postMessage` frames between the page and the service worker.
- The MAIN-world scripts run inside Workflowy's page, in manifest order: `hotkeys-shared.js` (the settings schema and hotkey actions, shared with the Options page), `marked.min.js`, KaTeX and `glimpse-md-render.js` (the Markdown renderer), `glimpse-ui.js`, `glimpse-ui-help.js` and `glimpse-ui-drawers.js` (the rail, the deck, the Reader chrome, the words, the drawers, the palette, toasts and the cursor menus), then `glimpse-cache-client.js` (transport, the daemon's requests, the render pipeline, the DOM walker, the gestures).

The daemon composes every Markdown document; the page only paints it. Agents push into the Reader (`push_to_modal`, with `target_tab`), read back what it shows (`render_spec: 'modal'`, frozen DOM views included) and GLIMPSE the live DOM; none of that depends on the UI.

## Files

```
workflowy-glimpse-cache/
├── manifest.json             # MV3 manifest (service worker, content bridge, MAIN-world scripts, Options page)
├── background.js             # Service worker: the broker socket, endpoint registry, chrome.storage
├── content-bridge.js         # Isolated-world relay between the page and the service worker
├── hotkeys-shared.js         # Settings schema, hotkey actions, merge-and-clamp (shared with Options)
├── glimpse-md-render.js/.css # The Markdown renderer (also used by the Manager App's View pane)
├── glimpse-ui.js             # The Rail: theme, layout and push, deck, rail, Reader chrome, popovers, palette, tips, toasts, cursor menus
├── glimpse-ui-help.js        # The words: bylines, one-liners, the Help drawer's sections, the first-run tips
├── glimpse-ui-drawers.js     # The drawers: Anchor Docks, Slice groups, Lightning Rod, Tabs, Jobs, Settings, Help
├── glimpse-ui.css            # One stylesheet for the deck (light and dark tokens)
├── glimpse-cache-client.js   # The client: WebSocket requests, render pipeline, DOM walker, gestures
├── options.html/.js/.css     # The Options page
├── marked.min.js, marked-katex-init.js, katex/, katex-overrides.css
├── README.md                 # This file
├── INSTALL.md                # Quick install guide
└── DEPLOYMENT.md             # Detailed troubleshooting
```

## Install

See `INSTALL.md` for the walkthrough. The short version: `chrome://extensions/` → Developer mode on → Load unpacked → point at this folder → paste the broker token on the Options page.

## Requirements

- Chrome 111+ (for MV3 `"world": "MAIN"` support; released February 2023, so essentially all current Chrome installs).
- The NEXUS daemon (`workflowy-nexus-mcp-tools`) running on this machine: its broker listens on `ws://localhost:8765`, and the Options page needs its token once (`%LOCALAPPDATA%\workflowy-mcp\broker_token`).

## Status

Working unpacked install path. Chrome Web Store publication is planned but not blocking; private demo and early-user testing happens via Load Unpacked from this folder.
