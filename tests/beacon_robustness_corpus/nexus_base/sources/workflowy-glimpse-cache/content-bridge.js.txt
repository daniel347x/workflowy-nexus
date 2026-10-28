/**
 * Workflowy GLIMPSE Cache - Content Script Bridge (ISOLATED world)
 *
 * Tiny relay between:
 *   - The main-world page client (glimpse-cache-client.js), which talks via
 *     window.postMessage with sentinel { __glimpse_bridge__: true }
 *   - The extension service worker (background.js), which owns the actual
 *     WebSocket to ws://localhost:8765 and talks via chrome.runtime.sendMessage.
 *
 * This file is intentionally minimal. It performs strict sentinel checks
 * on both sides and forwards transparently — no JSON parsing, no
 * application logic.
 */

'use strict';

const BRIDGE_SENTINEL = '__glimpse_bridge__';

// ---------------------------------------------------------------------------
// Page (main world) → Service Worker
// ---------------------------------------------------------------------------
window.addEventListener('message', (event) => {
  // Strict origin + window-source check: only accept messages posted from
  // the same window. Cross-frame messages are dropped.
  if (event.source !== window) return;
  const msg = event.data;
  if (!msg || msg[BRIDGE_SENTINEL] !== true) return;
  if (msg.direction !== 'page-to-sw') return;

  // Relay to the service worker. The SW responds via sendResponse, which we
  // pipe back to the page as a separate postMessage so the main-world
  // client's request/response logic stays callback-friendly.
  const correlationId = msg.correlationId;
  try {
    chrome.runtime.sendMessage(msg, (response) => {
      if (chrome.runtime.lastError) {
        // Service worker hadn't woken in time, or some other transient.
        // Tell the page so its readyState logic can react.
        window.postMessage(
          {
            [BRIDGE_SENTINEL]: true,
            direction: 'sw-to-page',
            correlationId,
            payload: {
              type: 'bridge_response',
              ok: false,
              readyState: WebSocket.CLOSED,
              error: chrome.runtime.lastError.message,
            },
          },
          '*'
        );
        return;
      }
      // Forward any non-internal fields from the SW response transparently
      // alongside the standard bridge envelope. This lets handlers (e.g.
      // get_hotkey_config) receive arbitrary structured data through the
      // same channel without each new action kind needing a bridge edit.
      const innerPayload = {
        type: 'bridge_response',
        ok: response ? response.ok !== false : true,
      };
      // IMPORTANT: readyState is a TRANSPORT/LIFECYCLE signal, not a generic
      // response field. Only include it when the service worker explicitly
      // returned a numeric readyState (query_state / request_connect / send).
      // Many unrelated extension bridge calls (e.g. set_extension_setting from
      // the docked modal) legitimately return no readyState. The old default
      // here was WebSocket.CLOSED, which made the page-side BridgeSocket treat
      // a harmless storage-write ack as a real socket close, detach its
      // listener, and orphan in-flight modal render requests. That is why the
      // modal saw bogus WebSocket timeouts while F3/F12/GLIMPSE appeared stable.
      if (response && typeof response.readyState === 'number') {
        innerPayload.readyState = response.readyState;
      }
      if (response && typeof response === 'object') {
        for (const k of Object.keys(response)) {
          if (k === '__glimpse_bridge__' || k === 'ok' || k === 'readyState') continue;
          innerPayload[k] = response[k];
        }
      }
      window.postMessage(
        {
          [BRIDGE_SENTINEL]: true,
          direction: 'sw-to-page',
          correlationId,
          payload: innerPayload,
        },
        '*'
      );
    });
  } catch (e) {
    // Extension context invalidated (e.g., extension reloaded). Tell the
    // page; it will keep retrying via its reconnect timer.
    window.postMessage(
      {
        [BRIDGE_SENTINEL]: true,
        direction: 'sw-to-page',
        correlationId,
        payload: {
          type: 'bridge_response',
          ok: false,
          readyState: WebSocket.CLOSED,
          error: String(e),
        },
      },
      '*'
    );
  }
});

// ---------------------------------------------------------------------------
// Service Worker → Page (main world)
// ---------------------------------------------------------------------------
chrome.runtime.onMessage.addListener((message, _sender, _sendResponse) => {
  if (!message || message[BRIDGE_SENTINEL] !== true) return false;
  if (message.direction !== 'sw-to-page') return false;

  // Forward to the main-world client. The page-side listener filters by
  // the same sentinel so unrelated postMessages on the workflowy.com tab
  // are ignored.
  window.postMessage(
    {
      [BRIDGE_SENTINEL]: true,
      direction: 'sw-to-page',
      payload: message.payload,
    },
    '*'
  );
  return false;
});

// Signal page presence so the SW can register this tab (and so the SW gets
// a chance to wake from idle if it had ticked over).
try {
  chrome.runtime.sendMessage({
    [BRIDGE_SENTINEL]: true,
    direction: 'page-to-sw',
    kind: 'query_state',
  });
} catch (e) {
  // No-op; the SW will see us when the page posts its first real message.
}
