# Workflowy GLIMPSE Cache Extension

Chrome (MV3) extension that gives the Python MCP Workflowy server a fast, DOM-level view of whatever the user currently has expanded in Workflowy, plus a set of in-page keyboard shortcuts and widgets that make NEXUS/Cartographer work fluid.

## What it adds to Workflowy

When this extension is installed and you have `https://workflowy.com` open in Chrome:

- **`workflowy_glimpse()` MCP tool** returns `_source: "websocket"` — the server reads the visible tree directly from your browser's DOM instead of fetching the entire vault over the API.
- **F2 / F3** — copy the full ancestor path of the hovered node (with UUIDs at every level / only at the leaf) to the clipboard.
- **F8** — expand every visible `#nexus--*` subtree in place.
- **F9** — open the corresponding source file in Windsurf at the exact line for any Cartographer-mapped node.
- **F12** — Cartographer popup menu: file/folder refresh (F12+1), fast tag-sync (F12+2), Markdown full regenerate (F12+3).
- **UUID Navigator widget** — paste any UUID, see the full path from root.
- **CARTO jobs widget** — live status of running CARTO_REFRESH and CARTO_BULK_APPLY jobs.
- **Ctrl + hover 5s** — copy a node's UUID plus full ancestor UUID chain to the clipboard.

## Architecture

Single file, main-world content script.

The manifest loads `glimpse-cache-client.js` into Workflowy's page context (`"world": "MAIN"`). From there the script opens its own WebSocket to the Python MCP server at `ws://localhost:8765`, walks the page DOM directly, and adds all the in-page widgets and hotkeys. No service worker, no message-passing bridge between isolated and main worlds — the extension is just an injector for one self-contained client.

This is the same `glimpse-cache-client.js` that was historically injected into Workflowy Desktop's Electron `app.asar` via the `glimpse_deploy_trampoline.py` script. The Chrome extension is now the primary install path; the Electron trampoline remains in the repo as an advanced fallback for users who specifically want GLIMPSE inside Workflowy Desktop.

## Files

```
workflowy-glimpse-cache/
├── manifest.json             # MV3 manifest (loads glimpse-cache-client.js into MAIN world)
├── glimpse-cache-client.js   # Standalone client: WebSocket, DOM walker, hotkeys, widgets
├── README.md                 # This file
├── INSTALL.md                # Quick install guide
└── DEPLOYMENT.md             # Detailed troubleshooting
```

## Install

See `INSTALL.md` for the 60-second walkthrough. The short version: `chrome://extensions/` → Developer mode on → Load unpacked → point at this folder.

## Requirements

- Chrome 111+ (for MV3 `"world": "MAIN"` support; released February 2023, so essentially all current Chrome installs)
- Python MCP server (`workflowy-nexus-mcp-tools`) running with the `websockets` library installed — the server hosts the `ws://localhost:8765` endpoint this extension connects to.

## Status

Working unpacked install path. Chrome Web Store publication is planned but not blocking; private demo and early-user testing happens via Load Unpacked from this folder.
