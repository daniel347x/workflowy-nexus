# Quick Installation Guide

## 60-Second Setup

### Prerequisite: the NEXUS daemon running

The Python MCP server (`workflowy-nexus-mcp-tools`) must be running on this machine, because it hosts the local broker (`ws://localhost:8765`) this extension's service worker connects to.

If you installed the MCP server via the standard `uvx --from git+https://github.com/daniel347x/workflowy-nexus.git workflowy-mcp` invocation, the `websockets` dependency is already pulled in. No extra step.

If your MCP host (TypingMind / Claude Desktop / Claude Code) was already running before you installed the extension, restart its MCP connector so the broker gets a chance to bind to port 8765.

You can confirm the server side is alive by looking for these lines in the MCP host's logs:

```
Starting WebSocket server on ws://localhost:8765
✅ WebSocket server listening on port 8765
```

### Step 1: Load the extension unpacked (30 seconds)

1. Open `chrome://extensions/` in Chrome.
2. Toggle **Developer mode** on (top-right).
3. Click **Load unpacked**.
4. Select the `workflowy-glimpse-cache/` folder inside your local clone of the `workflowy-nexus-mcp-tools` repo.
5. Done: the extension appears in the list with the version from `manifest.json` (`3.52.0` on this branch).

### Step 2: Paste the broker token (15 seconds)

Open the extension's **Options** page (Details → Extension options) and, under *Local broker connection*, paste the token from `%LOCALAPPDATA%\workflowy-mcp\broker_token` (or the `WORKFLOWY_BROKER_TOKEN` environment variable). The page confirms the connection.

### Step 3: Open Workflowy in Chrome (5 seconds)

Open https://workflowy.com in a Chrome tab and sign in.

The extension auto-loads on any `workflowy.com` page. You can see it loaded by the narrow **rail** at the right edge of the page (its dot turns on when the broker is connected), or by opening DevTools (F12) on the Workflowy tab and looking in the console for:

```
[GLIMPSE Cache v3.52.0] Standalone client initializing...
```

### Step 4: Smoke test (15 seconds)

From your MCP host (TypingMind / Claude Desktop / Claude Code), call:

```
workflowy_glimpse()
```

The response should include `"_source": "websocket"`. If it says `"_source": "api"` instead, the WebSocket bridge isn't live: see DEPLOYMENT.md troubleshooting.

You can also verify the in-page side:

- Click any Workflowy bullet: the deck opens with that node rendered as Markdown in the Reader.
- Hover any bullet and press **F3**: the full Markdown path (ending with the bullet's UUID) is copied to your clipboard.
- Click the rail's **Anchor Docks** icon: your favorite Docks appear as a wide sheet beside the Reader; click one to open it in the Reader.

If those work, you're done.

## What You Get

✅ **`workflowy_glimpse()` reads from the DOM, not the API**: orders of magnitude less data transferred.

✅ **The rail and the deck**: Anchor Docks, Slice groups, Lightning Rod, Tabs, Jobs, Settings and Help beside the Reader, which renders any subtree, slice group, Anchor Dock or agent push as one Markdown document. On a left or right dock the Reader pushes Workflowy aside instead of covering it.

✅ **F2 / F3 / F8 / F9 / Ctrl+F10 / Ctrl+F12 gestures** on the node under the mouse: path copying, `#nexus--*` expansion, the Windsurf jump, marker tags, the Cartographer menus. **Ctrl+Shift+K** opens the command palette, **Alt+N** toggles the deck; every binding is editable on the Options page.

✅ **Live Cartographer jobs** in the Jobs drawer (with a count badge on the rail), plus the Activity log of everything the extension reports. The list polls only while this tab is visible, labels outcomes and says “N older jobs not shown” when history is omitted. One Details click on a problem row requests its errors, warnings and findings for this tab alone; Load more reaches the rest.

✅ **Graceful fallback**: if the extension isn't loaded or the MCP server isn't running, `workflowy_glimpse()` falls back to the API path automatically.

## Multiple Workflowy Accounts

Chrome's same-origin policy means only one Workflowy account can be signed in at a time per browser profile. If you have a personal and a work Workflowy account and want both active simultaneously, the simplest path is:

- Chrome (with this extension) → the account you use GLIMPSE against.
- Firefox or a separate Chrome profile → the other account.

The extension only activates on the tab where you're signed in; the other browser is unaffected.

## Next Steps

After installation works:

- Read `DEPLOYMENT.md` for full troubleshooting if anything misbehaves.
- The trampoline path (`glimpse_deploy_trampoline.py` at the repo root) is still available if you specifically want GLIMPSE inside Workflowy Desktop (Electron); see that script's header comment.
