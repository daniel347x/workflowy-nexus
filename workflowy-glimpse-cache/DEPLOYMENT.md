# NEXUS for Workflowy — Deployment & Troubleshooting

## Architecture recap

Three worlds, one seam (README.md → Architecture has the full picture):

- `background.js`, the service worker, owns the one real WebSocket to the local NEXUS broker (`ws://localhost:8765`) and `chrome.storage`.
- `content-bridge.js`, in the isolated world, relays frames between the page and the service worker.
- The MAIN-world scripts (`"world": "MAIN"` in `manifest.json`) run inside the Workflowy page: DOM extraction, the F2 / F3 / F8 / F9 / Ctrl+F10 / Ctrl+F12 hotkeys, and the rail with its deck (the Reader and the drawers) are all installed there.

If the script doesn't load, or doesn't connect, the MCP server's `workflowy_glimpse()` tool transparently falls back to the API path and you'll see `"_source": "api"` in responses.

## Prerequisites

- Chrome 111+ (any current Chrome). MV3 `"world": "MAIN"` content scripts have been supported since Chrome 111 (February 2023).
- The `workflowy-nexus-mcp-tools` Python MCP server running in your MCP host, with the `websockets` library installed. The standard `uvx --from git+https://...` install pulls it in automatically.

## Installation steps

### 1. Verify the Python WebSocket server is live

In your MCP host's logs (TypingMind / Claude Desktop / Claude Code), confirm:

```
Starting WebSocket server on ws://localhost:8765
✅ WebSocket server listening on port 8765
```

If you see:

```
websockets library not installed. WebSocket cache unavailable.
```

then the `websockets` dependency isn't on the Python path the MCP server is using. With the standard uvx install path this shouldn't happen; if it does, the fix is normally `uv cache clean workflowy-nexus-mcp-tools` followed by restarting the MCP host so the package rebuilds with full deps.

### 2. Load the extension

`chrome://extensions/` → Developer mode → Load unpacked → select the `workflowy-glimpse-cache/` folder inside your local clone of the `workflowy-nexus-mcp-tools` repo.

The extension should show up as version `3.15.1` with no errors.

### 3. Open Workflowy

https://workflowy.com — sign in to whichever account you use GLIMPSE against.

In the Workflowy tab, open DevTools (F12) → Console. You should see:

```
[GLIMPSE Cache v3.15.1] Standalone client initializing...
[GLIMPSE Cache] Connecting to ws://localhost:8765
[GLIMPSE Cache] ✅ Connected to Python MCP server
[GLIMPSE Cache v3.15.1] ✅ Standalone client loaded
```

If you only see the first line and then nothing, the WebSocket isn't connecting — see troubleshooting below.

### 4. End-to-end test

From the MCP host:

```
workflowy_glimpse()
```

Look for `"_source": "websocket"` in the response. That's the all-clear.

## Troubleshooting

### Extension loads but `_source` is still `"api"`

The most common cause: the WebSocket didn't bind, or the page context isn't reaching it.

1. Confirm the Python side is listening — look for `✅ WebSocket server listening on port 8765` in MCP host logs.
2. Confirm the page context loaded — Workflowy tab DevTools console should show `Standalone client initializing...` and `✅ Connected to Python MCP server`.
3. If both are true but the MCP tool still says `api`, the Python end isn't finding the open WebSocket. Restart the MCP connector (TypingMind: toggle MCP off/on; Claude Desktop: force-quit + restart; Claude Code: `/exit` and re-launch) so the server rebinds the socket with the extension already connected.
4. Check nothing else is on port 8765: `netstat -ano | findstr 8765`.

### DOM extraction returns 0 nodes

You're seeing a successful `websocket` source but the tree is empty.

- Workflowy DOM-extraction only sees what's currently rendered in the visible viewport. If the user has the relevant subtree collapsed (or has scrolled out of view), nothing comes back.
- Scroll to the bottom of the expanded subtree before triggering the GLIMPSE — bottom-out-of-view nodes are silently dropped. (Top-out-of-view is fine.)

### Extension loaded but no hotkeys work

If F2/F3/F8/F9/F12 do nothing in Workflowy:

- DevTools console: do you see the `Standalone client loaded` line? If not, the content script wasn't injected — check that the Workflowy tab URL matches one of the manifest's `host_permissions` patterns (it should match `https://workflowy.com/*` or `https://*.workflowy.com/*`).
- If the console line is there but hotkeys don't fire: try with DevTools closed. DevTools steals document focus when its console is logging, which can suppress keypress events from reaching the page.
- Make sure you don't have another extension intercepting F-keys (Vimium, for example, sometimes does).

### `"world": "MAIN"` not honored (very old Chrome)

If you're on a Chrome version older than 111 (extremely unusual in 2026), the manifest's `"world": "MAIN"` will silently downgrade to isolated world, and `glimpse-cache-client.js` will load but be invisible to Workflowy's page state. Update Chrome.

## Disabling the extension

Two options:

1. `chrome://extensions/` → toggle this extension off. `workflowy_glimpse()` falls back to API automatically.
2. Stop the Python MCP server (or just quit the MCP host). The WebSocket connection drops; the extension stays installed but inert; the MCP tool falls back to API.

## Trampoline / Workflowy Desktop (advanced)

If you specifically want GLIMPSE inside the Workflowy Desktop (Electron) app instead of Chrome, the original deployment path is still available via `glimpse_deploy_trampoline.py` at the repo root. That script packs `glimpse-cache-client.js` into Workflowy Desktop's `app.asar` directly.

Most users should prefer the Chrome extension — it's a one-click reinstall after every Workflowy update, instead of a re-run of the trampoline. The Electron path is documented for completeness, not as the recommended install.

See the trampoline script's header comment for details if you want to go that route.

## Future enhancements

- Chrome Web Store publication (one-click install instead of unpacked).
- Optional icon set (16/32/48/128 px) for store listing.
- Configurable WebSocket port (currently hardcoded to 8765 on both extension and server sides; fine for v1).
