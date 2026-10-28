/**
 * Workflowy GLIMPSE Cache - Background Service Worker
 *
 * Owns the WebSocket connection to the Python MCP server at ws://localhost:8765.
 *
 * Why a service worker (not a main-world WebSocket): Chrome's Private Network
 * Access policy and CSP behaviors increasingly restrict ws://localhost
 * connections from https:// pages. Service workers in extensions are not
 * subject to those restrictions — they connect to localhost without preflight.
 *
 * Architecture:
 *   PAGE CONTEXT (main world)    glimpse-cache-client.js
 *     ↕ window.postMessage
 *   ISOLATED WORLD               content-bridge.js
 *     ↕ chrome.runtime.sendMessage / chrome.tabs.sendMessage
 *   SERVICE WORKER               background.js (this file)
 *     ↕ ws://localhost:8765
 *   PYTHON MCP SERVER
 *
 * The bridge is transparent: whatever JSON envelope the page-context client
 * would have written to a direct WebSocket is forwarded unchanged through
 * the bridge to the Python server, and vice versa.
 */

'use strict';

// Import the shared hotkey catalog + helpers so the service worker can
// merge defaults with the user's stored overrides before broadcasting to
// the page client. Service workers use importScripts() for sync inclusion.
try {
  importScripts('hotkeys-shared.js');
} catch (e) {
  console.error('[GLIMPSE SW] Failed to importScripts hotkeys-shared.js:', e);
}

// @beacon[
//   id=auto-beacon@WS_URL-ww96,
//   role=WS_URL,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
const WS_URL = 'ws://localhost:8765';
// @beacon[
//   id=auto-beacon@RECONNECT_DELAY_MS-vj5w,
//   role=RECONNECT_DELAY_MS,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
const RECONNECT_DELAY_MS = 3000;
// @beacon[
//   id=auto-beacon@HOTKEY_STORAGE_KEY-fv39,
//   role=const HOTKEY_STORAGE_KEY,
//   slice_labels=nexus--broker,f12-popup,
//   kind=ast,
// ]
const HOTKEY_STORAGE_KEY = 'hotkey_config';
// @beacon[
//   id=auto-beacon@SETTINGS_STORAGE_KEY-s4qp,
//   role=SETTINGS_STORAGE_KEY,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
const SETTINGS_STORAGE_KEY = 'extension_settings';

// chrome.storage.LOCAL key holding the F8 slice-group definitions
// ({ expand_groups, active_group }). MOVED OUT of the sync SETTINGS blob
// (Dan, 2026): expand_groups historically lived INSIDE SETTINGS_STORAGE_KEY in
// chrome.storage.SYNC, which is capped at QUOTA_BYTES_PER_ITEM = 8192 bytes per
// key. A slice group with a long tag list (or several groups with long
// descriptions) pushed the whole settings blob past 8192, so
// chrome.storage.sync.set FAILED -- silently, because the write was
// fire-and-forget -- and the group's tags reverted to their last-persisted
// (blank) value on the next storage re-read (tab refresh / new tab / cross-tab
// broadcast). storage.LOCAL is ~10MB with NO per-item cap, so slice-group
// definitions (which grow without bound) belong here. Trade-off: local does
// NOT sync across machines and is wiped on uninstall (surfaced to the user in
// the widget slice-group save-status line).
// @beacon[
//   id=auto-beacon@EXPAND_GROUPS_LOCAL_KEY,
//   role=EXPAND_GROUPS_LOCAL_KEY,
//   slice_labels=nexus--config,
//   kind=ast,
//   comment=chrome.storage.LOCAL key for F8 slice-group defs (expand_groups+active_group); moved out of the 8192-byte-capped sync SETTINGS blob so long tag lists no longer silently fail to persist,
// ]
const EXPAND_GROUPS_LOCAL_KEY = 'expand_groups_local';

// chrome.storage.LOCAL key holding the saved anchor-view definitions
// ({ anchor_views: [...] }). Completely orthogonal to EXPAND_GROUPS_LOCAL_KEY
// — a separate data collection for saved anchor views (root UUID + descend at
// child anchors), stored in local (NOT sync) for the same quota reasons as
// expand_groups. An empty array is a legitimate state (the user has not
// created any anchor views yet).
const ANCHOR_VIEWS_LOCAL_KEY = 'anchor_views_local';

// Broker protocol identity (must match src/workflowy_mcp/broker_protocol.py).
// @beacon[
//   id=auto-beacon@BROKER_PROTOCOL_NAME-3zth,
//   role=BROKER_PROTOCOL_NAME,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
const BROKER_PROTOCOL_NAME = 'workflowy-glimpse-broker';
// @beacon[
//   id=auto-beacon@BROKER_PROTOCOL_VERSION-eb94,
//   role=BROKER_PROTOCOL_VERSION,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
const BROKER_PROTOCOL_VERSION = 1;
// chrome.storage key holding the broker shared-secret token. The user pastes
// the SAME token configured for the MCP server (WORKFLOWY_BROKER_TOKEN, or the
// broker's generated per-machine token file contents) into the extension
// options. A browser extension cannot read the filesystem token file directly,
// so manual paste is how the browser side learns the secret.
//
// IMPORTANT — this token lives in chrome.storage.LOCAL, not sync. It is a
// per-LOCAL-MACHINE pairing secret: the broker generates a different token on
// each machine, so syncing it across machines (via chrome.storage.sync) would
// copy the wrong token to other computers and cause silent bad_token
// rejections. Local-only is both more correct (per-machine) and more secure
// (the secret never leaves this machine via Google sync). The lookup below
// reads local first and falls back to sync ONLY to migrate any token a prior
// build may have written to sync.
// @beacon[
//   id=auto-beacon@BROKER_TOKEN_STORAGE_KEY-1ejb,
//   role=BROKER_TOKEN_STORAGE_KEY,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
const BROKER_TOKEN_STORAGE_KEY = 'broker_token';
// chrome.storage.local key holding this extension/profile's stable connection
// id. Persistent per profile (survives browser restarts), identifying THIS
// browser as a single broker browser-connection that multiplexes many tabs.
// @beacon[
//   id=auto-beacon@BROKER_CONNECTION_ID_KEY-76xb,
//   role=BROKER_CONNECTION_ID_KEY,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
const BROKER_CONNECTION_ID_KEY = 'broker_connection_id';

// ─────────────────────────────────────────────────────────────────────────
// Service-worker keepalive via chrome.alarms.
//
// PROBLEM: MV3 service workers idle out after ~30 seconds of no activity. When
// the SW idles, Chrome closes its WebSocket. If the Workflowy tab is fully
// backgrounded, Chrome throttles or pauses page timers, so the page-side
// CARTO polling that would normally keep the SW alive stops firing. After
// idle, the next workflowy_glimpse() call from the MCP host arrives at the
// Python server with no live browser peer, and fails.
//
// FIX: chrome.alarms fires even when all tabs are backgrounded and the SW is
// asleep — alarm fire is itself a SW-wake event. We register a 30-second
// recurring alarm; on each fire we check WebSocket health, reconnect if it
// has died, or send a ping if it's still alive (which both proves liveness
// to the Python server and exercises the connection so any half-open state
// surfaces immediately).
//
// Minimum alarm period in MV3 is 0.5 minutes (30 seconds) — attempting
// shorter intervals is rounded up silently by Chrome and is Web-Store-
// flagged on review. 30s is sufficient: even in worst-case alarm timing, the
// SW is asleep for at most ~30 seconds before being woken back into a
// connected state.
// ─────────────────────────────────────────────────────────────────────────
// @beacon[
//   id=auto-beacon@KEEPALIVE_ALARM_NAME-u7ln,
//   role=KEEPALIVE_ALARM_NAME,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
const KEEPALIVE_ALARM_NAME = 'glimpse-keepalive';
// @beacon[
//   id=auto-beacon@KEEPALIVE_PERIOD_MINUTES-9148,
//   role=KEEPALIVE_PERIOD_MINUTES,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
const KEEPALIVE_PERIOD_MINUTES = 0.5; // 30 seconds (MV3 minimum)

// @beacon[
//   id=auto-beacon@ensureKeepaliveAlarm-w7qv,
//   role=ensureKeepaliveAlarm,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
function ensureKeepaliveAlarm() {
  try {
    chrome.alarms.create(KEEPALIVE_ALARM_NAME, {
      delayInMinutes: KEEPALIVE_PERIOD_MINUTES,
      periodInMinutes: KEEPALIVE_PERIOD_MINUTES,
    });
    logSw('Keepalive alarm registered (period 30s).');
  } catch (e) {
    logSw('Failed to register keepalive alarm:', e);
  }
}

let ws = null;
let reconnectTimer = null;
let isConnecting = false;
let brokerHandshakeDone = false;

// This extension/profile's stable broker connection id (resolved lazily from
// chrome.storage.local, generated on first use).
let brokerConnectionId = null;

// Track which tab(s) the page-context client lives in.
const workflowyTabs = new Set();

// Endpoint multiplexing: the broker addresses logical Workflowy tabs by
// endpoint_id, but the SW must deliver each targeted frame to the correct
// physical tab. These maps maintain that routing, populated as each tab's
// page client registers its endpoint (relayed through the bridge).
const endpointToTab = new Map(); // endpoint_id -> tabId
const tabToEndpoint = new Map(); // tabId -> endpoint_id

// ─────────────────────────────────────────────────────────────────────────
// Endpoint-roster settling window (Option B: SW-authoritative endpoint
// liveness). The service worker is the ONLY component that can observe which
// Workflowy tabs (endpoints) currently exist behind this one browser
// connection — a killed tab whose `endpoint_unregister` frame was lost (SW
// asleep, socket mid-reconnect, tab hard-killed) otherwise ghosts in the
// broker's registry forever, breaking single-tab auto-select. So the keepalive
// ping now carries the SW's live endpoint roster (endpointToTab), and the
// broker reconciles its endpoint registry against it.
//
// CRITICAL SAFETY (per the design audit): the broker may ONLY prune from an
// AUTHORITATIVE roster. Right after a (re)connect the SW has broadcast
// `broker_request_endpoint_register` but the page clients have not yet
// answered, so endpointToTab may be empty/incomplete. Pruning on THAT would
// wrongly drop live tabs. We therefore stamp `roster_complete:false` on pings
// until a short settling window elapses; the broker refreshes last_seen from a
// non-authoritative roster but NEVER prunes on it. 30s keepalive cadence makes
// a ~2s settling window free.
// ─────────────────────────────────────────────────────────────────────────
const ROSTER_SETTLING_MS = 2000; // grace after (re)connect before roster is authoritative
let rosterSettlingUntil = 0; // epoch ms; roster is authoritative once now >= this

// @beacon[
//   id=broker-ext@current-endpoint-roster,
//   role=currentEndpointRoster(): snapshot the SW's live endpoint->tab map for the broker,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
function currentEndpointRoster() {
  // The SW's endpointToTab map IS the authoritative set of live endpoints for
  // this browser connection (each entry was created by observing a tab's
  // endpoint_register and is torn down on onRemoved / dead-tab send failure).
  const out = [];
  for (const epId of endpointToTab.keys()) {
    out.push(epId);
  }
  return out;
}

// @beacon[
//   id=broker-ext@roster-is-authoritative,
//   role=rosterIsAuthoritative(): true once the post-(re)connect settling window has elapsed,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
function rosterIsAuthoritative() {
  // Not authoritative until tabs have had a chance to (re)register after the
  // last (re)connect. rosterSettlingUntil is (re)armed on hello_ack.
  return rosterSettlingUntil !== 0 && Date.now() >= rosterSettlingUntil;
}

// Resolve (and lazily create + persist) the per-profile connection id.
// @beacon[
//   id=broker-ext@get-connection-id,
//   role=getBrokerConnectionId(): resolve/create the per-profile broker connection_id,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
async function getBrokerConnectionId() {
  if (brokerConnectionId) return brokerConnectionId;
  brokerConnectionId = await new Promise((resolve) => {
    try {
      chrome.storage.local.get([BROKER_CONNECTION_ID_KEY], (raw) => {
        let id = raw && raw[BROKER_CONNECTION_ID_KEY];
        if (!id) {
          id = 'chrome-' + (self.crypto && self.crypto.randomUUID
            ? self.crypto.randomUUID()
            : String(Date.now()) + '-' + Math.random().toString(36).slice(2));
          chrome.storage.local.set({ [BROKER_CONNECTION_ID_KEY]: id });
        }
        resolve(id);
      });
    } catch (e) {
      resolve('chrome-' + String(Date.now()));
    }
  });
  return brokerConnectionId;
}

// Read the broker shared-secret token from storage (LOCAL first, then sync).
// Local is the canonical store for this per-machine pairing secret (see the
// BROKER_TOKEN_STORAGE_KEY comment above); the sync fallback exists only to
// migrate a token written by an older build that used sync as primary.
// @beacon[
//   id=broker-ext@get-token,
//   role=getBrokerToken(): read broker shared-secret token from chrome.storage (local-first),
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
async function getBrokerToken() {
  return new Promise((resolve) => {
    try {
      chrome.storage.local.get([BROKER_TOKEN_STORAGE_KEY], (raw) => {
        const tok = raw && raw[BROKER_TOKEN_STORAGE_KEY];
        if (tok && String(tok).trim()) {
          resolve(String(tok).trim());
          return;
        }
        chrome.storage.sync.get([BROKER_TOKEN_STORAGE_KEY], (raw2) => {
          const tok2 = raw2 && raw2[BROKER_TOKEN_STORAGE_KEY];
          resolve(tok2 && String(tok2).trim() ? String(tok2).trim() : null);
        });
      });
    } catch (e) {
      resolve(null);
    }
  });
}

function logSw(msg, ...rest) {
  // Service-worker logs appear in chrome://extensions/ → this extension →
  // "service worker" link → DevTools console.
  console.log(`[GLIMPSE SW] ${msg}`, ...rest);
}

// STEP 4 (WSLIVE) instrumentation flag + sink for the SW leg. Silent by
// default (open-source-clean); flips on only when the extension setting
// `wslive_verbose` is truthy (kept live via the storage.onChanged /
// loadExtensionSettings path below). Correlated [wslive] lines carry the
// request_id / endpoint_id so a reap -> ghost -> black-hole chain can be
// stitched across the broker + SW + page legs.
let wsliveVerbose = false;
function logWslive(msg, ...rest) {
  if (wsliveVerbose) console.log(`[GLIMPSE SW][wslive] ${msg}`, ...rest);
}

/**
 * Broadcast a payload to every known Workflowy tab. Each tab's
 * content-bridge.js will forward it to its main-world client via
 * window.postMessage.
 */
// @beacon[
//   id=auto-beacon@broadcastToTabs-59kx,
//   role=broadcastToTabs,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
async function broadcastToTabs(payload) {
  if (workflowyTabs.size === 0) {
    // No tabs registered — try discovery as a fallback.
    try {
      const tabs = await chrome.tabs.query({
        url: ['https://workflowy.com/*', 'https://*.workflowy.com/*'],
      });
      tabs.forEach((t) => workflowyTabs.add(t.id));
    } catch (e) {
      logSw('tab discovery failed', e);
    }
  }

  const deadTabs = [];
  for (const tabId of workflowyTabs) {
    try {
      await chrome.tabs.sendMessage(tabId, {
        __glimpse_bridge__: true,
        direction: 'sw-to-page',
        payload,
      });
    } catch (e) {
      // Tab closed or no listener — clean up.
      deadTabs.push(tabId);
    }
  }
  deadTabs.forEach((id) => workflowyTabs.delete(id));
}

// @beacon[
//   id=auto-beacon@clearReconnectTimer-04hv,
//   role=clearReconnectTimer,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
function clearReconnectTimer() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

// @beacon[
//   id=auto-beacon@scheduleReconnect-ojt9,
//   role=scheduleReconnect,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
function scheduleReconnect() {
  clearReconnectTimer();
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectWebSocket();
  }, RECONNECT_DELAY_MS);
}

// @beacon[
//   id=auto-beacon@connectWebSocket-zwgn,
//   role=connectWebSocket,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
function connectWebSocket() {
  if (isConnecting) return;
  if (ws && ws.readyState === WebSocket.OPEN) return;
  if (ws && ws.readyState === WebSocket.CONNECTING) return;

  isConnecting = true;
  logSw(`Connecting to ${WS_URL} ...`);

  try {
    ws = new WebSocket(WS_URL);
  } catch (e) {
    logSw('WebSocket construction threw:', e);
    isConnecting = false;
    scheduleReconnect();
    return;
  }

  ws.onopen = async () => {
    isConnecting = false;
    brokerHandshakeDone = false;
    clearReconnectTimer();
    logSw('✅ Connected to GLIMPSE broker; performing hello handshake.');
    // Broker handshake: announce this browser connection (role=browser) with
    // protocol identity + shared-secret token. Endpoints (tabs) register
    // separately via endpoint_register frames once the page clients announce.
    try {
      const [connId, token] = await Promise.all([
        getBrokerConnectionId(),
        getBrokerToken(),
      ]);
      if (!token) {
        logSw('⚠️ No broker token set in extension options; broker will reject. '
          + 'Open the extension options and paste the broker shared-secret token.');
      }
      const hello = {
        action: 'hello',
        role: 'browser',
        connection_id: connId,
        protocol: BROKER_PROTOCOL_NAME,
        protocol_version: BROKER_PROTOCOL_VERSION,
      };
      if (token) hello.token = token;
      ws.send(JSON.stringify(hello));
    } catch (e) {
      logSw('hello handshake send failed:', e);
    }
  };

  ws.onmessage = async (event) => {
    let msg = null;
    try { msg = JSON.parse(event.data); } catch (e) { /* non-JSON */ }

    // Intercept broker handshake ack before forwarding anything to tabs.
    if (msg && msg.type === 'hello_ack') {
      if (msg.accepted === false) {
        logSw(`❌ Broker rejected this browser connection: ${msg.error} [${msg.code}]`);
        brokerHandshakeDone = false;
        // Do not spin-reconnect on auth failure; wait for the user to fix the
        // token. onclose (if the broker closes us) will schedule a retry.
        return;
      }
      brokerHandshakeDone = true;
      logSw('✅ Broker handshake accepted; (re)registering tab endpoints.');
      // Arm the roster settling window: we are about to ask all tabs to
      // re-register, but their endpoint_register frames have not arrived yet,
      // so endpointToTab may be stale/empty for a beat. Until this window
      // elapses the keepalive ping marks the roster non-authoritative so the
      // broker refreshes last_seen but NEVER prunes on an incomplete roster.
      rosterSettlingUntil = Date.now() + ROSTER_SETTLING_MS;
      // Flush any frames that were enqueued while the socket was cold/connecting
      // (cold-SW race). Do this BEFORE re-registration broadcasts so a queued
      // F12/GLIMPSE frame goes out as soon as the link is live.
      flushOutboundQueue();
      broadcastToTabs({ type: 'bridge_event', event: 'open' });
      // Ask every known tab to (re)register its endpoint after a (re)connect.
      // Sent as a ws_message-shaped frame so it reaches the page client's
      // onmessage dispatch (the BridgeSocket only surfaces ws_message +
      // bridge_event payload types to the page).
      broadcastToTabs({
        type: 'ws_message',
        data: JSON.stringify({ type: 'broker_request_endpoint_register' }),
      });
      return;
    }

    // Endpoint-collision notice: route to the specific tab so it can
    // regenerate its endpoint_id.
    if (msg && msg.type === 'endpoint_collision' && msg.endpoint_id) {
      const tabId = endpointToTab.get(msg.endpoint_id);
      if (tabId != null) {
        sendToTab(tabId, { type: 'ws_message', data: event.data });
      }
      return;
    }

    // Targeted frame: deliver only to the owning tab (multiplexing).
    //
    // AMENDMENT A (WSLIVE): the two SILENT BLACK-HOLES healed here. When the
    // broker still has the endpoint but THIS SW's endpointToTab map has lost
    // it (drift), a targeted frame used to be logged-and-dropped with NO
    // reply, and the originating MCP/tab waited out its full timeout ->
    // opaque 'server unavailable'. Two drop cases now emit a CORRELATED
    // `unknown_endpoint` frame back over the broker socket (echoing
    // request_id), which rides the broker's existing request_id->origin rail
    // to the correct waiter (STEP 3 on the MCP side / the tab on the browser
    // side), and tells the tab to re-register:
    //   (1) MAP-MISS: no tabId for target_endpoint_id (endpointToTab drifted).
    //   (2) SEND-FAILURE: a tabId exists but chrome.tabs.sendMessage throws,
    //       which ALSO purges the mapping inside sendToTab (returns false) --
    //       the sendToTab-purges-a-live-tab drift documented in AMENDMENT A.
    if (msg && msg.target_endpoint_id) {
      const tabId = endpointToTab.get(msg.target_endpoint_id);
      if (tabId != null) {
        const delivered = await sendToTab(tabId, { type: 'ws_message', data: event.data });
        if (!delivered) {
          logWslive(
            `targeted-frame sendToTab FAILED (purged mapping) for `
            + `target_endpoint_id=${msg.target_endpoint_id} `
            + `request_id=${msg.request_id}`,
          );
          emitUnknownEndpointToBroker(
            msg,
            'Service worker could not deliver to the target tab (it was '
            + 'purged on a failed send); tab should re-register.',
          );
        }
      } else {
        logWslive(
          `targeted-frame MAP-MISS for target_endpoint_id=`
          + `${msg.target_endpoint_id} request_id=${msg.request_id}`,
        );
        emitUnknownEndpointToBroker(
          msg,
          'Service worker has no live tab for target_endpoint_id; tab should '
          + 're-register.',
        );
      }
      return;
    }

    // Untargeted broker→browser frame (broadcasts like F8/F9 results): fan out.
    broadcastToTabs({ type: 'ws_message', data: event.data });
  };

  ws.onerror = (event) => {
    logSw('WebSocket error', event);
    // Don't broadcast 'error' separately — onclose will follow and the page
    // logic only cares about open/close transitions.
  };

  ws.onclose = () => {
    isConnecting = false;
    brokerHandshakeDone = false;
    logSw('⚠️ Disconnected from GLIMPSE broker; will retry.');
    broadcastToTabs({ type: 'bridge_event', event: 'close' });
    ws = null;
    scheduleReconnect();
  };
}

/**
 * Send a payload to a single tab's content-bridge.js (which forwards to the
 * main-world page client). Used for endpoint-targeted multiplexed delivery.
 */
// @beacon[
//   id=broker-ext@send-to-tab,
//   role=sendToTab(): deliver an endpoint-targeted frame to one Workflowy tab,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
async function sendToTab(tabId, payload) {
  try {
    await chrome.tabs.sendMessage(tabId, {
      __glimpse_bridge__: true,
      direction: 'sw-to-page',
      payload,
    });
    return true;
  } catch (e) {
    // Tab closed / no listener — purge its endpoint mapping.
    workflowyTabs.delete(tabId);
    const ep = tabToEndpoint.get(tabId);
    if (ep) {
      endpointToTab.delete(ep);
      tabToEndpoint.delete(tabId);
    }
    return false;
  }
}

// @beacon[
//   id=broker-ext@emit-unknown-endpoint,
//   role=emitUnknownEndpointToBroker(): reply unknown_endpoint on a targeted-frame drop (WSLIVE AMENDMENT A),
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
function emitUnknownEndpointToBroker(msg, errorText) {
  // AMENDMENT A (WSLIVE): send a CORRELATED unknown_endpoint failure back over
  // the broker socket when a targeted frame cannot be delivered to its tab
  // (map-miss or purged-on-failed-send). The broker's request_id->origin map
  // routes this to the correct waiter (the MCP server for a GLIMPSE/apply, or
  // the originating tab for a browser-initiated nested read), so the caller
  // fails LOUD + actionable instead of waiting out an opaque timeout. Rides
  // the SAME code:'unknown_endpoint' rail the broker's own resolve-miss /
  // dead-forward replies use, so STEP 3 (MCP) and the tab handlers react to
  // it identically.
  //
  // A frame with NO request_id has no waiter to answer (it is an untargeted
  // work/broadcast frame that should not have reached this targeted branch
  // anyway), so we skip the emit and only breadcrumb it. The reply itself is
  // best-effort over the live socket; a dead socket here just means the
  // originator's own transport path will surface the failure.
  const requestId = msg && msg.request_id;
  if (requestId == null) {
    logWslive(
      `targeted-frame drop had no request_id (no waiter to answer); `
      + `target_endpoint_id=${msg && msg.target_endpoint_id}`,
    );
    return;
  }
  const reply = {
    success: false,
    code: 'unknown_endpoint',
    request_id: requestId,
    target_endpoint_id: msg && msg.target_endpoint_id,
    error: errorText,
  };
  try {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(reply));
      logWslive(
        `emitted unknown_endpoint reply (request_id=${requestId}, `
        + `target_endpoint_id=${msg && msg.target_endpoint_id})`,
      );
    } else {
      logWslive(
        `could NOT emit unknown_endpoint reply (broker socket not OPEN); `
        + `request_id=${requestId}`,
      );
    }
  } catch (e) {
    logSw('emitUnknownEndpointToBroker send failed:', e);
  }
}

// ---------------------------------------------------------------------------
// Saved Tabs (Tier-0) — cross-tab render-spec collection for the widget's
// Export button. EXTENSION-INTERNAL plumbing only: one in-flight collection
// per Export click, keyed by a fresh request_id; each entry maps tabId -> the
// partially-filled row and holds the originating widget's sendResponse.
// These rows NEVER ride the broker WebSocket and never touch the protected
// render_slice_modal / apply_slice_in_dock server wire — the survey is
// chrome.tabs.query + chrome.tabs.sendMessage (host_permissions already grant
// url/title for workflowy.com; no new manifest permissions) and each tab
// answers from its own page-world stored render_spec blob.
// ---------------------------------------------------------------------------
const SAVED_TABS_COLLECT_TIMEOUT_MS = 6000; // generous: tabs answer in ms
const pendingSavedTabsCollections = new Map(); // request_id -> { state, finish }

// @beacon[
//   id=saved-tabs@collect-orchestrator,
//   role=collectSavedTabsSpecs(sendResponse): survey every Workflowy tab for its stored modal render spec,
//   slice_labels=nexus--broker,saved-tabs,
//   kind=ast,
//   comment=Saved-Tabs EXPORT orchestrator (SW half): chrome.tabs.query the workflowy.com tabs (url/title LIVE from chrome.tabs — the broker roster's urls are register-time and go stale), send each tab a saved_tabs_spec_request over the existing sw-to-page relay, collect the page-world saved_tabs_spec_response replies (endpoint_id/alias/stored render_spec blob VERBATIM) into rows keyed per tab, and answer the originating widget when every tab responded or the collection window elapsed; read-only survey — never purges broker routing state, never touches the server wire,
// ]
async function collectSavedTabsSpecs(sendResponse) {
  const requestId = 'stc-' + Date.now().toString(36) + '-'
    + Math.random().toString(36).slice(2, 8);
  let tabs = [];
  try {
    tabs = await chrome.tabs.query({
      url: ['https://workflowy.com/*', 'https://*.workflowy.com/*'],
    });
  } catch (e) {
    logSw('saved-tabs collect: chrome.tabs.query failed:', e);
    try {
      sendResponse({
        __glimpse_bridge__: true,
        ok: false,
        error: 'chrome.tabs.query failed: ' + String(e),
      });
    } catch (e2) { /* widget timeout will surface it */ }
    return;
  }
  // One row per tab, in query order. endpoint_id is PROVISIONAL from the SW
  // routing map (the tab's own response — its sessionStorage identity —
  // overwrites it); url/title are LIVE from chrome.tabs.
  const byTab = new Map();
  for (const t of tabs) {
    byTab.set(t.id, {
      tab_id: t.id,
      endpoint_id: tabToEndpoint.get(t.id) || ('tab-' + t.id),
      alias: '',
      url: t.url || '',
      title: t.title || '',
      responded: false,
      result: null,
    });
  }
  const state = { done: false, byTab, timer: null };
  const finish = () => {
    if (state.done) return;
    state.done = true;
    if (state.timer) {
      try { clearTimeout(state.timer); } catch (e) { /* ignore */ }
      state.timer = null;
    }
    pendingSavedTabsCollections.delete(requestId);
    const rows = [];
    byTab.forEach((row) => rows.push(row));
    try {
      sendResponse({ __glimpse_bridge__: true, ok: true, request_id: requestId, rows });
    } catch (e) { /* widget already gone; nothing to do */ }
  };
  pendingSavedTabsCollections.set(requestId, { state, finish });
  state.timer = setTimeout(() => {
    logSw('saved-tabs collect: window elapsed; finishing with partial rows.');
    finish();
  }, SAVED_TABS_COLLECT_TIMEOUT_MS);
  if (tabs.length === 0) {
    finish();
    return;
  }
  for (const t of tabs) {
    try {
      await chrome.tabs.sendMessage(t.id, {
        __glimpse_bridge__: true,
        direction: 'sw-to-page',
        payload: { type: 'saved_tabs_spec_request', request_id: requestId },
      });
      // Resolved == the content-script relay exists; the page-world answer
      // arrives separately as a 'saved_tabs_spec_response' message.
    } catch (e) {
      // No content-script listener in this tab (closed, or loaded before the
      // extension was installed). Leave responded:false — the widget lists it
      // as not-exportable. Deliberately does NOT purge workflowyTabs /
      // endpointToTab: this is a read-only survey and must not interfere with
      // broker routing state (sendToTab's purge is for broker-frame delivery).
    }
  }
}

// ---------------------------------------------------------------------------
// Saved Tabs (Tier-2) — the multi-tab DRIVE (spec [ap:7S217K] §6/§8, unit
// T2A; U01 amendments per contract [ap:08ZEAO]; U02 per its fix note
// [ap:DLGU66]). Sequential estate restore: per entry, open a tab at PLAIN
// https://workflowy.com/ (SAVED_TABS_DRIVE_TAB_URL — an entry's saved page
// url, zoom/search included, is IGNORED: cold deep-links to zoomed nodes were
// what failed, U02) — REUSING the previous drive's window when it still
// exists, otherwise ONE new window (never refused because a window exists) —
// await THAT tab's endpoint_register observation (the readiness hook in the
// ONE frame-inspection branch below — event-true, never a timer or a poll;
// the caps here are liveness hygiene only), deliver alias+spec in ONE
// saved_tabs_drive_apply frame, await the correlated terminal response, wait
// the configurable inter-tab delay (saved_tabs_drive_delay_seconds, re-read
// at every gap so a mid-drive change takes effect at the next tab), then
// move on. Cancel is IMMEDIATE (D6 amended): it interrupts whatever the
// drive is waiting on — the in-flight entry is reported failed with a
// cancelled reason and its tab stays open. At most one drive RUNS at a time
// (I4): a new request SUPERSEDES the running one instead of being refused.
// EXTENSION-INTERNAL kinds only (A2): the five saved_tabs_drive_* kinds ride
// the page<->SW bridge and never reach the broker WebSocket; the spec
// payload travels by object identity, never mutated or re-keyed by the drive
// (A1/G4, [ap:HVAWAF]). Strictly sequential (A5/G6); a failing entry never
// aborts the rest (D7); nothing ever closes a tab; MV3 eviction mid-drive
// aborts LOUDLY via the chrome.storage.session marker (D1 — no resume
// machinery, U1).
// ---------------------------------------------------------------------------
const SAVED_TABS_DRIVE_READY_TIMEOUT_MS = 180000;
// Apply cap = the receiver's own renderTimeoutMs (CACHE_RENDER_LONG_TIMEOUT_MS
// = 180s in the page client) + a 30s bridge margin (spec §6).
const SAVED_TABS_DRIVE_APPLY_TIMEOUT_MS = 210000;
const SAVED_TABS_DRIVE_SESSION_KEY = 'glimpse_saved_tabs_drive_active';
// The ONE url every drive tab is opened at (U02, Dan's ruling): plain
// Workflowy, never the entry's saved page url. The tab quiesces (the readiness
// hook) and the saved projection is then applied into its modal as before.
const SAVED_TABS_DRIVE_TAB_URL = 'https://workflowy.com/';

// The single in-flight drive (singleton or null). Core fields per spec §6:
// drive_id / origin_tab_id / entries / index / cancel_requested; plus the
// transient per-drive state the §6 state machine itself requires: window_id
// (the drive window — the previous drive's when reused, else the new one),
// pending_apply (the correlated response waiter for the in-flight entry),
// origin_dead (D8), cancel_signal (the ONE per-drive cancel promise raced
// against every wait — U01), cancel_reason (why a stop fired) and done (this
// drive's chained run promise — a superseding drive awaits it, I4).
let activeSavedTabsDrive = null;

// The window the most recent drive created (U01): the next drive's first
// create re-resolves it and opens its tabs there when it still exists (Dan's
// ruling — never refuse because a drive window exists; USE it). MEMORY-ONLY
// by design: the service worker is kept alive by the 30 s keepalive alarm
// and constant drive-adjacent traffic, so losing the id costs one new
// window, never a refusal. Deliberately NOT persisted — the eviction
// marker's shape is unchanged and no second chrome.storage.session key
// exists.
let savedTabsDriveLastWindowId = null;

// tabId -> readiness waiter. The ONLY drive-readiness mechanism (spec §8.2
// postcondition 2): armed by the driver right after create, notified by the
// ONE hook inside the frame-inspection branch, rejected by the per-entry
// onRemoved listener or the ready cap. Never polled.
const pendingTabReadiness = new Map();

// Display name for progress frames: the alias→title→url fallback (§6; since
// U02 an old entry's saved url is a last-resort DISPLAY name only — the drive
// never navigates to it). A
// degenerate hand-authored entry with none of the three is still NAMEABLE in
// the summary (A9 — failures are always loud). Thin helper; no beacon.
// @beacon[
//   id=auto-beacon@savedTabsDriveEntryName-uf8o,
//   role=savedTabsDriveEntryName,
//   slice_labels=saved-tabs,
//   kind=ast,
// ]
function savedTabsDriveEntryName(entry, index) {
  const e = (entry && typeof entry === 'object') ? entry : {};
  const alias = (typeof e.alias === 'string') ? e.alias.trim() : '';
  if (alias) return alias;
  const title = (typeof e.title === 'string') ? e.title.trim() : '';
  if (title) return title;
  const url = (typeof e.url === 'string') ? e.url.trim() : '';
  if (url) return url;
  return 'entry ' + (Number.isFinite(index) ? index + 1 : '?');
}

// The eviction marker (D1): drive_id / origin_tab_id / total / next_index
// under the ONE chrome.storage.session key. Written at drive start, updated
// at each entry boundary, cleared when the drive ends. Awaited so marker
// writes cannot invert order; a failed write is logged loudly and NEVER
// fails the drive (the marker is abort-reporting hygiene, not correctness).
// @beacon[
//   id=auto-beacon@savedTabsDriveMarkerWrite-4guk,
//   role=savedTabsDriveMarkerWrite,
//   slice_labels=saved-tabs,
//   kind=ast,
// ]
async function savedTabsDriveMarkerWrite(drive, nextIndex) {
  try {
    await chrome.storage.session.set({
      [SAVED_TABS_DRIVE_SESSION_KEY]: {
        drive_id: drive.drive_id,
        origin_tab_id: drive.origin_tab_id,
        total: drive.entries.length,
        next_index: nextIndex,
      },
    });
  } catch (e) {
    logSw('saved-tabs drive: eviction-marker write failed (drive continues):', e);
  }
}

// Per-drive cancel signal (U01): ONE promise per drive that rejects when the
// drive is stopped (user Cancel, supersede, origin death). runSavedTabsDrive
// races every wait against it, so a stop interrupts the wait at once.
// Idempotent: created on first use, reused thereafter.
// @beacon[
//   id=auto-beacon@savedTabsDriveCancelSignal-aese,
//   role=savedTabsDriveCancelSignal,
//   slice_labels=saved-tabs,
//   kind=ast,
// ]
function savedTabsDriveCancelSignal(drive) {
  if (!drive.cancel_signal) {
    let fire = null;
    const promise = new Promise((resolve, reject) => { fire = reject; });
    promise.catch(() => {});   // observed only through Promise.race in the loop
    drive.cancel_signal = { promise, fire };
  }
  return drive.cancel_signal;
}

// Stop a drive NOW (U01, overrides D6): mark it cancelled and fire its signal
// so the in-flight wait (delay / readiness / apply response) rejects
// immediately. The ONE writer of the drive's cancelled flag. Never closes a
// tab.
// @beacon[
//   id=auto-beacon@stopSavedTabsDrive-99lp,
//   role=stopSavedTabsDrive,
//   slice_labels=saved-tabs,
//   kind=ast,
// ]
function stopSavedTabsDrive(drive, reason) {
  if (!drive) return;
  if (!drive.cancel_requested) {
    drive.cancel_requested = true;
    drive.cancel_reason = (typeof reason === 'string' && reason) ? reason : 'cancelled';
  }
  savedTabsDriveCancelSignal(drive).fire(new Error(drive.cancel_reason));
}

// Inter-tab delay (U01): read the MERGED extension settings at every gap, so
// a stepper click mid-drive takes effect at the next tab. Default + clamp live
// ONLY in hotkeys-shared.js (DEFAULT_EXTENSION_SETTINGS / mergeExtensionSettings);
// a non-finite value (possible only if the merge is unavailable) means no delay.
// @beacon[
//   id=auto-beacon@savedTabsDriveDelayMs-iori,
//   role=savedTabsDriveDelayMs,
//   slice_labels=saved-tabs,
//   kind=ast,
// ]
async function savedTabsDriveDelayMs() {
  let settings = null;
  try { settings = await loadExtensionSettings(); } catch (e) { settings = null; }
  const s = Number(settings && settings.saved_tabs_drive_delay_seconds);
  return (Number.isFinite(s) && s > 0) ? Math.round(s * 1000) : 0;
}

// Step-4 readiness waiter (D2): the correlated-waiter idiom of
// pendingSavedTabsCollections. Resolved by the ONE notification hook in the
// frame-inspection branch (endpoint_register / endpoint_update observed for
// THIS tab.id); rejected immediately by a one-shot onRemoved listener if the
// created tab dies first, or by the ready cap as liveness hygiene. The
// arm-time map check is a one-shot race guard (a frame observed between the
// create resolving and this arm), NOT a poll: the hook remains the mechanism.
// @beacon[
//   id=auto-beacon@armSavedTabsDriveTabReadiness-yhmh,
//   role=armSavedTabsDriveTabReadiness,
//   slice_labels=saved-tabs,
//   kind=ast,
// ]
function armSavedTabsDriveTabReadiness(tabId) {
  return new Promise((resolve, reject) => {
    const waiter = {
      settled: false, timer: null, removedListener: null, resolve: null, reject: null,
    };
    const settle = (fn, arg) => {
      if (waiter.settled) return;
      waiter.settled = true;
      if (waiter.timer != null) {
        try { clearTimeout(waiter.timer); } catch (e) { /* ignore */ }
        waiter.timer = null;
      }
      if (waiter.removedListener) {
        try { chrome.tabs.onRemoved.removeListener(waiter.removedListener); } catch (e) { /* ignore */ }
        waiter.removedListener = null;
      }
      if (pendingTabReadiness.get(tabId) === waiter) pendingTabReadiness.delete(tabId);
      fn(arg);
    };
    waiter.resolve = (epId) => settle(resolve, epId);
    waiter.reject = (err) => settle(reject, err);
    waiter.timer = setTimeout(() => {
      waiter.reject(new Error('tab did not announce its endpoint within '
        + SAVED_TABS_DRIVE_READY_TIMEOUT_MS + 'ms (ready cap — liveness hygiene, D2)'));
    }, SAVED_TABS_DRIVE_READY_TIMEOUT_MS);
    waiter.removedListener = (removedTabId) => {
      if (removedTabId !== tabId) return;
      waiter.reject(new Error('created tab was closed before it became ready'));
    };
    try {
      chrome.tabs.onRemoved.addListener(waiter.removedListener);
    } catch (e) { /* no listener support here — the cap still bounds the wait */ }
    pendingTabReadiness.set(tabId, waiter);
    const already = tabToEndpoint.get(tabId);
    if (already) waiter.resolve(already);
  });
}

// Step-6 response waiter: the same correlated-waiter idiom, stored on the
// drive singleton (one drive, one in-flight entry ⇒ at most one waiter);
// settled by the saved_tabs_drive_apply_response branch through
// handleSavedTabsDriveApplyResponse, or rejected by the apply cap.
// @beacon[
//   id=auto-beacon@armSavedTabsDriveApplyResponse-g3c3,
//   role=armSavedTabsDriveApplyResponse,
//   slice_labels=saved-tabs,
//   kind=ast,
// ]
function armSavedTabsDriveApplyResponse(drive, entryIndex) {
  return new Promise((resolve, reject) => {
    const waiter = {
      drive_id: drive.drive_id,
      entry_index: entryIndex,
      settled: false,
      timer: null,
      resolve: null,
      reject: null,
    };
    const settle = (fn, arg) => {
      if (waiter.settled) return;
      waiter.settled = true;
      if (waiter.timer != null) {
        try { clearTimeout(waiter.timer); } catch (e) { /* ignore */ }
        waiter.timer = null;
      }
      if (drive.pending_apply === waiter) drive.pending_apply = null;
      fn(arg);
    };
    waiter.resolve = (resp) => settle(resolve, resp);
    waiter.reject = (err) => settle(reject, err);
    waiter.timer = setTimeout(() => {
      waiter.reject(new Error('no saved_tabs_drive_apply_response within '
        + SAVED_TABS_DRIVE_APPLY_TIMEOUT_MS + 'ms (apply cap — liveness hygiene, D2)'));
    }, SAVED_TABS_DRIVE_APPLY_TIMEOUT_MS);
    drive.pending_apply = waiter;
  });
}

// Progress emission (D4): ONE frame per state transition to the ORIGIN tab
// through sendToTab (the ONE targeted-delivery primitive). A failed send
// means the origin tab is gone (D8): behave as if cancelled — the in-flight
// entry completes, the rest are skipped, the summary goes to the SW console.
// Later emits short-circuit (there is nothing left to paint to).
// @beacon[
//   id=auto-beacon@emitSavedTabsDriveProgress-cmxa,
//   role=emitSavedTabsDriveProgress,
//   slice_labels=saved-tabs,
//   kind=ast,
// ]
async function emitSavedTabsDriveProgress(drive, frame) {
  if (drive.origin_dead) return false;
  const payload = Object.assign({
    type: 'saved_tabs_drive_progress',
    drive_id: drive.drive_id,
    total: drive.entries.length,
  }, frame);
  const delivered = await sendToTab(drive.origin_tab_id, payload);
  if (!delivered) {
    drive.origin_dead = true;
    // U01/D8 amended: the origin is gone — STOP the drive at once (the
    // in-flight wait is interrupted, its entry reported failed-cancelled,
    // the rest skipped); the summary goes to the SW console.
    stopSavedTabsDrive(drive, 'origin tab unreachable');
    logSw('saved-tabs drive ' + drive.drive_id + ': origin tab unreachable '
      + '(progress send failed) — interrupting the in-flight entry, skipping '
      + 'the rest (D8 cancellation semantics, U01); summary goes to the SW '
      + 'console.');
  }
  return delivered;
}

// The request branch's handler: acceptance ack ONLY, answered IMMEDIATELY
// (§6) — the mutex (G10) and the entries validation are synchronous; every
// outcome of the drive itself rides saved_tabs_drive_progress frames.
// @beacon[
//   id=auto-beacon@handleSavedTabsDriveRequest-r36a,
//   role=handleSavedTabsDriveRequest,
//   slice_labels=saved-tabs,
//   kind=ast,
// ]
function handleSavedTabsDriveRequest(message, sender, sendResponse) {
  const p = (message && message.payload && typeof message.payload === 'object') ? message.payload : {};
  const entries = Array.isArray(p.entries) ? p.entries : [];
  if (entries.length === 0) {
    sendResponse({
      __glimpse_bridge__: true,
      ok: false,
      error: 'saved_tabs_drive_request carries no entries — nothing to drive',
    });
    return;
  }
  const originTabId = (sender && sender.tab && sender.tab.id != null) ? sender.tab.id : null;
  if (originTabId == null) {
    sendResponse({
      __glimpse_bridge__: true,
      ok: false,
      error: 'saved_tabs_drive_request must come from a Workflowy tab (no '
        + 'sender tab id) — the origin tab is the drive progress console (A7)',
    });
    return;
  }
  // Never refuse because a drive is running (U01, Dan's ruling — amends G10):
  // the new request SUPERSEDES it. The running drive is stopped at once and
  // the new run begins only after the old run has returned (I4: at most one
  // drive runs), so marker writes and window use never interleave.
  const prev = activeSavedTabsDrive;
  // A missing drive_id is minted here (a console-posted request works, §7);
  // the ack echoes whichever id the drive carries.
  const driveId = (typeof p.drive_id === 'string' && p.drive_id)
    ? p.drive_id
    : ('std-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8));
  const drive = {
    drive_id: driveId,
    origin_tab_id: originTabId,
    entries,
    index: 0,
    cancel_requested: false,
    window_id: null,
    pending_apply: null,
    origin_dead: false,
    done: null,
  };
  savedTabsDriveCancelSignal(drive);
  activeSavedTabsDrive = drive;
  sendResponse({
    __glimpse_bridge__: true,
    ok: true,
    drive_id: driveId,
    superseded_drive_id: prev ? prev.drive_id : null,
  });
  // The drive runs DETACHED — the ack is an acceptance receipt only (§6).
  drive.done = (async () => {
    if (prev) {
      stopSavedTabsDrive(prev, 'superseded by a new drive request');
      try { await prev.done; } catch (e) { /* prev's own chain logs */ }
    }
    await runSavedTabsDrive(drive);
  })().catch((e) => {
    logSw('saved-tabs drive crashed:', e);
  });
}

// @beacon[
//   id=saved-tabs@drive-orchestrator,
//   role=runSavedTabsDrive(drive): the sequential per-entry create → await-ready → deliver → settle state machine,
//   slice_labels=nexus--broker,saved-tabs,
//   kind=ast,
//   comment=Saved-Tabs Tier-2 DRIVE orchestrator (SW half, spec [ap:7S217K] §6/§8; U01 per contract [ap:08ZEAO]): strictly sequential (A5/G6) — per entry: STEP 0 the configurable inter-tab delay (saved_tabs_drive_delay_seconds re-read per gap via loadExtensionSettings — a mid-drive change takes effect at the next tab; interruptible; never before this drive's first create), the cancel check (a stopped drive reports this and every remaining entry skipped-cancelled, then the summary), STEP 3 create at PLAIN https://workflowy.com/ (SAVED_TABS_DRIVE_TAB_URL — the entry's saved page url is IGNORED since U02 [ap:DLGU66]; no url admission, no entry ever fails over its url) — REUSING the previous drive's window when it still exists (savedTabsDriveLastWindowId via chrome.windows.get; otherwise ONE new window: first entry chrome.windows.create unfocused, later entries chrome.tabs.create in that window, active:false — G1/D5 rev-2 as amended; a failed create unbinds and the next entry re-resolves), STEP 4 await THAT tab's endpoint_register observation via pendingTabReadiness RACED against the drive's cancel signal (the ONE hook in the frame-inspection branch — never a timer/poll, G2/D2; the caps are liveness hygiene; a stop tears the waiter down and reports the entry failed with a cancelled reason — its tab stays open), deliver alias+spec in ONE saved_tabs_drive_apply frame via sendToTab (spec by object identity — A1/G4), STEP 6 await the correlated saved_tabs_drive_apply_response raced against the cancel signal the same way; a failing entry never aborts the rest (D7 — every failure named with phase+reason); progress frames per transition to the ORIGIN tab (D4; origin death now STOPS the drive at once via stopSavedTabsDrive — cancellation semantics, D8 amended); a delaying progress frame announces each inter-tab gap; terminal summary (G7/G8), then singleton + eviction marker cleared; entered via the saved_tabs_drive_request branch (handleSavedTabsDriveRequest — acceptance ack; a concurrent request SUPERSEDES the running drive instead of being refused); extension-INTERNAL kinds only — never the broker wire (A2),
// ]
async function runSavedTabsDrive(drive) {
  // One terminal record per entry: name / state / phase / reason (G7).
  const results = [];
  // U01: every drive wait (delay / readiness / apply response) is raced
  // against this ONE cancel promise, so a stop interrupts the wait at once.
  const cancelled = savedTabsDriveCancelSignal(drive).promise;
  const untilCancelled = (p) => Promise.race([p, cancelled]);
  let tabsCreated = 0;   // tabs this drive has created — gates the inter-tab delay
  try {
    for (let i = 0; i < drive.entries.length; i++) {
      drive.index = i;
      const entry = (drive.entries[i] && typeof drive.entries[i] === 'object') ? drive.entries[i] : {};
      const name = savedTabsDriveEntryName(entry, i);
      // STEP 0 — inter-tab delay (U01): only between tab opens — never before
      // this drive's first create; interruptible — a stop during the sleep
      // falls through to STEP 1, which reports this and every remaining
      // entry skipped-cancelled.
      if (tabsCreated > 0 && !drive.cancel_requested) {
        const delayMs = await savedTabsDriveDelayMs();
        if (delayMs > 0) {
          await emitSavedTabsDriveProgress(drive, {
            kind: 'entry', entry_index: i, name, state: 'delaying',
            detail: 'waiting ' + (delayMs / 1000) + ' s before opening the next tab',
          });
          let timer = null;
          try {
            await untilCancelled(new Promise((r) => { timer = setTimeout(r, delayMs); }));
          } catch (e) {
            /* stopped — STEP 1 reports it */
          } finally {
            if (timer != null) clearTimeout(timer);
          }
        }
      }
      // STEP 1 — cancel check: a stop (user Cancel, supersede, origin death)
      // lands here on the next iteration. This and all remaining entries are
      // reported skipped-cancelled, then the summary.
      if (drive.cancel_requested) {
        for (let j = i; j < drive.entries.length; j++) {
          const skipName = savedTabsDriveEntryName(drive.entries[j], j);
          results.push({ name: skipName, state: 'skipped-cancelled' });
          await emitSavedTabsDriveProgress(drive, {
            kind: 'entry', entry_index: j, name: skipName, state: 'skipped-cancelled',
          });
        }
        break;
      }
      // Entry boundary: the eviction marker advances BEFORE any create (D1).
      await savedTabsDriveMarkerWrite(drive, i);
      // (STEP 2, the old url admission, is gone — U02: the entry's saved
      // page url is IGNORED, so an absent, empty or odd url never fails an
      // entry; every tab opens at SAVED_TABS_DRIVE_TAB_URL.)
      // STEP 3 — create (G1/D5 rev-2, amended by U01 and U02): every tab
      // opens at PLAIN https://workflowy.com/ (SAVED_TABS_DRIVE_TAB_URL) —
      // never the saved page url (U02: cold deep-links to a zoomed node were
      // the entries that failed); the drive REUSES the
      // previous drive's window when it still exists (Dan's ruling — never
      // refuse because a drive window exists; USE it); its FIRST create
      // otherwise opens ONE new window (unfocused — the restored estate lands
      // together and never steals focus); later entries are tabs in that same
      // window. The originating tab is never a drive target (it stays the
      // progress console). Throw → failed (creating); next entry.
      await emitSavedTabsDriveProgress(drive, {
        kind: 'entry', entry_index: i, name, state: 'creating',
      });
      let tabId = null;
      try {
        // Dan's ruling (U01): never refuse because a drive window exists — USE it.
        // The first create of a drive re-resolves the remembered window; if it is
        // gone, the drive opens a new one exactly as before.
        if (drive.window_id == null && savedTabsDriveLastWindowId != null) {
          try {
            const w = await chrome.windows.get(savedTabsDriveLastWindowId);
            if (w && w.id != null) drive.window_id = w.id;
          } catch (e) {
            savedTabsDriveLastWindowId = null;   // the remembered window was closed
          }
        }
        if (drive.window_id == null) {
          const win = await chrome.windows.create({ url: SAVED_TABS_DRIVE_TAB_URL, focused: false });
          if (!win || win.id == null || !Array.isArray(win.tabs)
              || !win.tabs[0] || win.tabs[0].id == null) {
            throw new Error('chrome.windows.create returned no usable window/tab');
          }
          drive.window_id = win.id;
          savedTabsDriveLastWindowId = win.id;
          tabId = win.tabs[0].id;
        } else {
          const tab = await chrome.tabs.create({ url: SAVED_TABS_DRIVE_TAB_URL, windowId: drive.window_id, active: false });
          if (!tab || tab.id == null) {
            throw new Error('chrome.tabs.create returned no tab id');
          }
          tabId = tab.id;
        }
      } catch (e) {
        // A failed create unbinds the drive from its window; the next entry's
        // create re-resolves the remembered window (or opens a new one).
        drive.window_id = null;
        const reason = (e && e.message) ? e.message : String(e);
        results.push({ name, state: 'failed', phase: 'creating', reason });
        await emitSavedTabsDriveProgress(drive, {
          kind: 'entry', entry_index: i, name, state: 'failed',
          detail: 'creating — ' + reason,
        });
        continue;
      }
      tabsCreated += 1;
      // STEP 4 — await readiness (G2/D2): arm the waiter on the created
      // tab.id FIRST — synchronously after the create resolves, so no frame
      // can slip between — then announce the transition. The
      // endpoint_register / endpoint_update observation resolves it (the ONE
      // hook); a removed tab rejects immediately; the cap is liveness hygiene
      // only, never correctness-by-timer. U01: the wait is RACED against the
      // drive's cancel signal — a stop rejects at once and tears the waiter
      // down (no cap timer or onRemoved listener left behind).
      const readyPromise = armSavedTabsDriveTabReadiness(tabId);
      await emitSavedTabsDriveProgress(drive, {
        kind: 'entry', entry_index: i, name, state: 'awaiting-ready',
      });
      try {
        await untilCancelled(readyPromise);
      } catch (e) {
        let reason = (e && e.message) ? e.message : String(e);
        if (drive.cancel_requested) {
          // Tear down the lost waiter at once (clears its cap timer + onRemoved
          // listener) — never leave a 180 s timer behind a cancelled drive.
          const w = pendingTabReadiness.get(tabId);
          if (w) w.reject(new Error('drive stopped'));
          reason = 'cancelled (' + drive.cancel_reason + ') — the tab stays open';
        }
        results.push({ name, state: 'failed', phase: 'awaiting-ready', reason });
        await emitSavedTabsDriveProgress(drive, {
          kind: 'entry', entry_index: i, name, state: 'failed',
          detail: 'awaiting-ready — ' + reason,
        });
        continue;
      }
      // STEP 5 — deliver alias + spec in ONE combined frame (A3/G3/G4): the
      // spec rides BY OBJECT IDENTITY — nothing on this path reads,
      // transforms or re-keys it (A1). sendToTab false = tab dead = failed
      // entry (applying — tab unreachable).
      await emitSavedTabsDriveProgress(drive, {
        kind: 'entry', entry_index: i, name, state: 'applying',
      });
      const delivered = await sendToTab(tabId, {
        type: 'saved_tabs_drive_apply',
        drive_id: drive.drive_id,
        entry_index: i,
        alias: (typeof entry.alias === 'string') ? entry.alias : '',
        spec: entry.spec,
      });
      if (!delivered) {
        const reason = 'tab unreachable (saved_tabs_drive_apply delivery failed)';
        results.push({ name, state: 'failed', phase: 'applying', reason });
        await emitSavedTabsDriveProgress(drive, {
          kind: 'entry', entry_index: i, name, state: 'failed',
          detail: 'applying — ' + reason,
        });
        continue;
      }
      // STEP 6 — await the correlated terminal answer (§6): ok:true →
      // settled; ok:false / cap timeout → failed (applying, detail). The
      // receiver ALWAYS answers (bounded by its own long render timeout plus
      // the socket-death settle, P7); the cap backstops a silently dead tab.
      // U01: the wait is RACED against the cancel signal — a stop rejects at
      // once and tears drive.pending_apply down.
      let response = null;
      try {
        response = await untilCancelled(armSavedTabsDriveApplyResponse(drive, i));
      } catch (e) {
        let reason = (e && e.message) ? e.message : String(e);
        if (drive.cancel_requested) {
          // Tear down the lost waiter at once — no apply-cap timer left
          // behind a cancelled drive. The tab may still finish its render.
          if (drive.pending_apply) drive.pending_apply.reject(new Error('drive stopped'));
          reason = 'cancelled (' + drive.cancel_reason
            + ') while applying — the tab may still finish its render';
        }
        results.push({ name, state: 'failed', phase: 'applying', reason });
        await emitSavedTabsDriveProgress(drive, {
          kind: 'entry', entry_index: i, name, state: 'failed',
          detail: 'applying — ' + reason,
        });
        continue;
      }
      if (response && response.ok === true) {
        results.push({ name, state: 'settled' });
        await emitSavedTabsDriveProgress(drive, {
          kind: 'entry', entry_index: i, name, state: 'settled',
        });
      } else {
        const reason = (response && response.error)
          ? response.error
          : 'apply reported failure with no detail';
        results.push({ name, state: 'failed', phase: 'applying', reason });
        await emitSavedTabsDriveProgress(drive, {
          kind: 'entry', entry_index: i, name, state: 'failed',
          detail: 'applying — ' + reason,
        });
      }
    }
    // STEP 7 — terminal summary (G7/G8/A9): counts + every failure named
    // with phase+reason. The summary only ever echoes what the apply
    // responses reported — never a success the receiver did not confirm.
    const applied = results.filter((r) => r.state === 'settled').length;
    const skipped = results.filter((r) => r.state === 'skipped-cancelled').length;
    const failedList = results.filter((r) => r.state === 'failed')
      .map((r) => ({ name: r.name, phase: r.phase, reason: r.reason }));
    if (drive.origin_dead) {
      // D8: the origin tab is gone — the summary goes to the SW console,
      // plus ONE best-effort aborted frame (§6 kinds table).
      logSw('saved-tabs drive ' + drive.drive_id + ' summary (origin tab '
        + 'gone): applied ' + applied + ', failed ' + failedList.length
        + ', skipped ' + skipped + ' — ' + JSON.stringify(failedList));
      await sendToTab(drive.origin_tab_id, {
        type: 'saved_tabs_drive_progress',
        drive_id: drive.drive_id,
        kind: 'aborted',
        entry_index: drive.index,
        total: drive.entries.length,
        detail: 'origin tab became unreachable mid-drive; the in-flight '
          + 'entry was interrupted and the rest were skipped (D8, U01)',
      });
    } else {
      await emitSavedTabsDriveProgress(drive, {
        kind: 'summary', applied, failed: failedList, skipped,
      });
    }
  } finally {
    // G8: no drive state remains in the SW — singleton cleared, eviction
    // marker cleared, whatever happened above.
    if (activeSavedTabsDrive === drive) activeSavedTabsDrive = null;
    try {
      await chrome.storage.session.remove(SAVED_TABS_DRIVE_SESSION_KEY);
    } catch (e) {
      logSw('saved-tabs drive: eviction-marker clear failed:', e);
    }
  }
}

// U01/D6 amended: cancellation is IMMEDIATE — it interrupts whatever the
// drive is waiting on (delay / readiness / apply response); the in-flight
// entry is reported failed with a cancelled reason and its tab stays open.
// No page-side waiter (the summary reports the cancellation). The drive_id
// must match the active drive — a stale dialog can never cancel a newer
// drive; a cancel naming no running drive is answered with an aborted frame
// so a stale console can never stay frozen.
// @beacon[
//   id=auto-beacon@handleSavedTabsDriveCancel-xv1d,
//   role=handleSavedTabsDriveCancel,
//   slice_labels=saved-tabs,
//   kind=ast,
// ]
function handleSavedTabsDriveCancel(message, sender) {
  const p = (message && message.payload && typeof message.payload === 'object') ? message.payload : {};
  const drive = activeSavedTabsDrive;
  if (!drive) {
    logSw('saved-tabs drive cancel ignored: no active drive.');
  } else if (p.drive_id !== drive.drive_id) {
    logSw('saved-tabs drive cancel ignored: drive_id mismatch (got '
      + String(p.drive_id) + ', active ' + drive.drive_id + ').');
  } else {
    // U01/D6 amended: the stop is IMMEDIATE — the in-flight wait (delay /
    // readiness / apply response) rejects at once; the in-flight entry is
    // reported failed with a cancelled reason and its tab stays open.
    stopSavedTabsDrive(drive, 'cancelled by user');
    logSw('saved-tabs drive ' + drive.drive_id + ': cancel received — stopping '
      + 'NOW (the in-flight entry is interrupted; its tab stays open).');
    return;
  }
  // U01: a cancel naming no running drive (it already ended, or the extension
  // restarted) still answers its sender with ONE aborted frame for that
  // drive_id, so a stale console can never stay frozen. The page ignores it
  // unless it names the drive its console is showing, so it can never disturb
  // a newer drive's console.
  const senderTabId = (sender && sender.tab && sender.tab.id != null) ? sender.tab.id : null;
  if (senderTabId == null || typeof p.drive_id !== 'string' || !p.drive_id) return;
  sendToTab(senderTabId, {
    type: 'saved_tabs_drive_progress',
    drive_id: p.drive_id,
    kind: 'aborted',
    total: 0,
    detail: 'no such drive is running any more (it already ended, or the '
      + 'extension restarted) — the dialog is usable again',
  });
}

// The created tab's terminal answer for ONE entry (§6 step 6): matched to the
// in-flight entry by drive_id + entry_index — the correlated-waiter idiom. A
// late / duplicate / foreign response is ignored loudly (its entry already
// settled or timed out; there is nothing to answer).
// @beacon[
//   id=auto-beacon@handleSavedTabsDriveApplyResponse-evzn,
//   role=handleSavedTabsDriveApplyResponse,
//   slice_labels=saved-tabs,
//   kind=ast,
// ]
function handleSavedTabsDriveApplyResponse(message) {
  const p = (message && message.payload && typeof message.payload === 'object') ? message.payload : {};
  const drive = activeSavedTabsDrive;
  const waiter = drive ? drive.pending_apply : null;
  if (!waiter || waiter.drive_id !== p.drive_id || waiter.entry_index !== p.entry_index) {
    logSw('saved-tabs drive: apply response ignored (no pending entry matches '
      + 'drive_id/entry_index)', p.drive_id, p.entry_index);
    return;
  }
  waiter.resolve({
    ok: p.ok === true,
    error: (p.error != null) ? String(p.error) : undefined,
  });
}

// Runs at top level on EVERY SW wake (bootstrap tail below). A marker
// surviving into a fresh SW lifetime means the previous drive died with the
// evicted worker — its in-memory state cannot exist here (D1). Report the
// interruption LOUDLY to the origin tab (best effort — the tab may be gone),
// clear the marker, and NEVER resume (U1: a rebuild-on-wake resume cannot
// distinguish created-but-unready from delivered-but-unsettled across process
// death; a wrong resume would duplicate a tab against the 1:1 invariant A3 —
// a loud stop is the sanctioned behavior). Tabs already opened are
// unaffected; the remainder is re-runnable by hand.
// @beacon[
//   id=auto-beacon@checkSavedTabsDriveEvictionMarker-97la,
//   role=checkSavedTabsDriveEvictionMarker,
//   slice_labels=saved-tabs,
//   kind=ast,
// ]
async function checkSavedTabsDriveEvictionMarker() {
  let marker = null;
  try {
    const stored = await chrome.storage.session.get(SAVED_TABS_DRIVE_SESSION_KEY);
    marker = (stored && typeof stored === 'object') ? stored[SAVED_TABS_DRIVE_SESSION_KEY] : null;
  } catch (e) {
    logSw('saved-tabs drive: eviction-marker read failed:', e);
    return;
  }
  if (!marker || typeof marker !== 'object') return;
  const total = Number.isFinite(marker.total) ? marker.total : 0;
  const nextIndex = Number.isFinite(marker.next_index) ? marker.next_index : 0;
  logSw('saved-tabs drive ' + marker.drive_id + ' was INTERRUPTED: the '
    + 'service worker was reclaimed mid-drive (entry ' + (nextIndex + 1)
    + ' of ' + total + ' was next). Reporting to the origin tab and clearing '
    + 'the marker; re-run the remaining entries by hand (no resume by '
    + 'design, D1/U1).');
  await sendToTab(marker.origin_tab_id, {
    type: 'saved_tabs_drive_progress',
    drive_id: marker.drive_id,
    kind: 'aborted',
    entry_index: nextIndex,
    total: total,
    detail: 'the extension service worker was reclaimed mid-drive; '
      + 'interrupted before entry ' + (nextIndex + 1) + ' of ' + total
      + '. Tabs already opened are unaffected; re-run the remaining '
      + 'entries (no resume by design).',
  });
  try {
    await chrome.storage.session.remove(SAVED_TABS_DRIVE_SESSION_KEY);
  } catch (e) {
    logSw('saved-tabs drive: eviction-marker clear failed:', e);
  }
}

// ---------------------------------------------------------------------------
// Outbound queue — cold-service-worker / not-yet-OPEN-socket race fix.
//
// MV3 evicts this service worker after ~30s idle; the only real WebSocket to
// the broker lives here. When the SW wakes in response to a page message
// (e.g. an F12 keypress after a long modal-reading idle), the socket is not
// yet OPEN — it must reconnect + perform the hello handshake first. The old
// behavior dropped the frame and returned ok=false, which the page surfaced
// as "GLIMPSE server unavailable". Instead we now ENQUEUE the frame, kick a
// reconnect, and FLUSH the queue once the handshake completes (see
// flushOutboundQueue, called from the hello_ack-accepted path). Each queued
// frame carries a deadline so a never-completing reconnect can't make us
// deliver stale frames indefinitely.
// ---------------------------------------------------------------------------
// @beacon[
//   id=glimpse-ext@outbound-queue,
//   slice_labels=nexus--broker,
//   kind=ast,
//   comment=Outbound frame queue + TTL for the cold-SW race: frames sent while the broker socket is not yet OPEN are buffered here and flushed on hello_ack-accepted instead of being dropped,
// ]
const outboundQueue = []; // [{ jsonString, deadline }]
const OUTBOUND_QUEUE_TTL_MS = 8000; // drop frames not flushed within this window
const OUTBOUND_QUEUE_MAX = 200; // hard cap so a wedged socket can't grow it unbounded

// @beacon[
//   id=glimpse-ext@flush-outbound-queue,
//   slice_labels=nexus--broker,
//   kind=ast,
//   comment=Flush the outbound queue once the broker handshake is live: sends every non-expired buffered frame in order; called from the hello_ack-accepted branch,
// ]
function flushOutboundQueue() {
  if (!ws || ws.readyState !== WebSocket.OPEN || !brokerHandshakeDone) return;
  const now = Date.now();
  let flushed = 0;
  let dropped = 0;
  while (outboundQueue.length) {
    const item = outboundQueue.shift();
    if (!item) continue;
    if (item.deadline && now > item.deadline) { dropped++; continue; }
    try {
      ws.send(item.jsonString);
      flushed++;
    } catch (e) {
      // Socket died mid-flush — put it back at the front and bail; the next
      // reconnect's handshake will trigger another flush.
      outboundQueue.unshift(item);
      logSw('flushOutboundQueue: ws.send threw, will retry on next handshake', e);
      break;
    }
  }
  if (flushed || dropped) {
    logSw(`flushOutboundQueue: sent ${flushed} queued frame(s)`
      + (dropped ? `, dropped ${dropped} expired` : ''));
  }
}

/**
 * Send a payload from the page out to the broker via the live WebSocket.
 *
 * If the socket is OPEN, send immediately. Otherwise — the cold-SW race —
 * ENQUEUE the frame (with a TTL), trigger a reconnect, and return true so the
 * page does NOT surface a spurious "server unavailable". The queue is flushed
 * the instant the broker handshake completes (flushOutboundQueue, called from
 * the hello_ack-accepted path). Returns true when sent-or-queued, false only
 * on a hard send error.
 */
// @beacon[
//   id=auto-beacon@sendToServer-65kn,
//   role=sendToServer,
//   slice_labels=nexus--broker,
//   kind=ast,
// ]
function sendToServer(jsonString) {
  if (ws && ws.readyState === WebSocket.OPEN && brokerHandshakeDone) {
    try {
      ws.send(jsonString);
      return true;
    } catch (e) {
      logSw('ws.send threw; enqueuing for retry', e);
      // Fall through to enqueue so the frame isn't lost.
    }
  }
  // Not (fully) connected — enqueue and make sure a reconnect is in flight.
  // Trim the oldest frame if we somehow exceed the cap (wedged socket).
  if (outboundQueue.length >= OUTBOUND_QUEUE_MAX) {
    outboundQueue.shift();
  }
  outboundQueue.push({ jsonString, deadline: Date.now() + OUTBOUND_QUEUE_TTL_MS });
  if (!ws || ws.readyState === WebSocket.CLOSED) {
    connectWebSocket();
  }
  // Queued (or will be sent on handshake) — treat as success from the page's
  // perspective so it shows "reconnecting" rather than a hard failure.
  return true;
}

// ---------------------------------------------------------------------------
// Hotkey config storage helpers — read from chrome.storage.sync, merge with
// defaults (via GlimpseHotkeys.mergeHotkeyConfig from hotkeys-shared.js),
// broadcast changes to all open Workflowy tabs.
// ---------------------------------------------------------------------------
// @beacon[
//   id=auto-beacon@loadHotkeyConfig-skm1,
//   role=function loadHotkeyConfig(),
//   slice_labels=f12-popup,
//   kind=ast,
// ]
async function loadHotkeyConfig() {
  return new Promise((resolve) => {
    try {
      chrome.storage.sync.get([HOTKEY_STORAGE_KEY], (raw) => {
        const userConfig = (raw && raw[HOTKEY_STORAGE_KEY]) || {};
        try {
          if (self.GlimpseHotkeys && self.GlimpseHotkeys.mergeHotkeyConfig) {
            resolve(self.GlimpseHotkeys.mergeHotkeyConfig(userConfig));
            return;
          }
        } catch (e) {
          // Fall through to raw return.
        }
        resolve(userConfig);
      });
    } catch (e) {
      logSw('loadHotkeyConfig threw:', e);
      resolve({});
    }
  });
}

// @beacon[
//   id=auto-beacon@broadcastHotkeyConfig-qggc,
//   role=function broadcastHotkeyConfig(),
//   slice_labels=f12-popup,
//   kind=ast,
// ]
async function broadcastHotkeyConfig() {
  const merged = await loadHotkeyConfig();
  broadcastToTabs({ type: 'hotkey_config', config: merged });
}


async function loadExtensionSettings() {
  return new Promise((resolve) => {
    // Merge the sync SETTINGS blob with the chrome.storage.LOCAL slice-group
    // defs (expand_groups/active_group) AND the anchor-view defs (anchor_views),
    // both of which were moved out of the sync blob to escape the 8192-byte per-
    // item quota. The in-memory settings object stays UNIFIED (both are still
    // keys on it), so every downstream reader is unchanged; only the physical
    // storage area differs.
    const finish = (effective) => {
      try {
        if (self.GlimpseHotkeys && self.GlimpseHotkeys.mergeExtensionSettings) {
          resolve(self.GlimpseHotkeys.mergeExtensionSettings(effective));
          return;
        }
      } catch (e) {
        // Fall through to raw return.
      }
      resolve(effective);
    };
    try {
      chrome.storage.sync.get([SETTINGS_STORAGE_KEY], (rawSync) => {
        const syncSettings = (rawSync && rawSync[SETTINGS_STORAGE_KEY]) || {};
        try {
          chrome.storage.local.get([EXPAND_GROUPS_LOCAL_KEY, ANCHOR_VIEWS_LOCAL_KEY], (rawLocal) => {
            const local = (rawLocal && rawLocal[EXPAND_GROUPS_LOCAL_KEY]) || null;
            const localAnchorViews = (rawLocal && rawLocal[ANCHOR_VIEWS_LOCAL_KEY]) || null;
            const localHasGroups = !!(local && Array.isArray(local.expand_groups));
            const syncHasGroups = Array.isArray(syncSettings.expand_groups);
            // Anchor views: read from local. Unlike expand_groups, there is no
            // migration from sync (anchor views were never in sync). An empty
            // array is legitimate (no anchor views created yet).
            const anchorViews = (localAnchorViews && Array.isArray(localAnchorViews.anchor_views))
              ? localAnchorViews.anchor_views : [];
            if (localHasGroups) {
              // Steady state: local is authoritative for the slice-group defs.
              finish(Object.assign({}, syncSettings, {
                expand_groups: local.expand_groups,
                active_group: local.active_group,
                anchor_views: anchorViews,
              }));
              return;
            }
            if (syncHasGroups) {
              // One-time migration: copy the sync-blob groups down to local,
              // then strip them from the sync blob (best-effort) to free up its
              // per-item quota. Use the migrated values for THIS load.
              const migrated = {
                expand_groups: syncSettings.expand_groups,
                active_group: syncSettings.active_group,
              };
              try {
                chrome.storage.local.set({ [EXPAND_GROUPS_LOCAL_KEY]: migrated }, () => {
                  const stripped = Object.assign({}, syncSettings);
                  delete stripped.expand_groups;
                  delete stripped.active_group;
                  try {
                    chrome.storage.sync.set({ [SETTINGS_STORAGE_KEY]: stripped }, () => {
                      if (chrome.runtime.lastError) {
                        logSw('expand_groups migration: sync strip failed:',
                          chrome.runtime.lastError);
                      }
                    });
                  } catch (e) { logSw('expand_groups migration strip threw:', e); }
                });
              } catch (e) { logSw('expand_groups migration set threw:', e); }
              finish(Object.assign({}, syncSettings, migrated, { anchor_views: anchorViews }));
              return;
            }
            // Neither area has groups (fresh install): let
            // mergeExtensionSettings materialize the default group in-memory
            // (persisted to local on the first edit), exactly as before.
            finish(Object.assign({}, syncSettings, { anchor_views: anchorViews }));
          });
        } catch (e) {
          // chrome.storage.local unavailable -- degrade to sync-only.
          logSw('loadExtensionSettings local read threw:', e);
          finish(syncSettings);
        }
      });
    } catch (e) {
      logSw('loadExtensionSettings threw:', e);
      resolve({ max_glimpse_payload_kb: 500 });
    }
  });
}

async function broadcastExtensionSettings() {
  const settings = await loadExtensionSettings();
  syncWsliveVerbose(settings);
  broadcastToTabs({ type: 'extension_settings', settings });
}

// STEP 4 (WSLIVE): adopt the `wslive_verbose` setting into the SW-leg
// instrumentation flag. Tolerant of absent/typed values; defaults OFF.
function syncWsliveVerbose(settings) {
  try {
    const v = settings && settings.wslive_verbose;
    wsliveVerbose = v === true || v === 'true' || v === 1 || v === '1';
  } catch (e) {
    wsliveVerbose = false;
  }
}

// Hydrate the flag once at SW startup so verbose mode is live even before the
// first settings-change broadcast (best-effort; never throws into startup).
try {
  loadExtensionSettings().then(syncWsliveVerbose).catch(() => {});
} catch (e) {
  /* ignore */
}

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === 'sync') {
    if (changes[HOTKEY_STORAGE_KEY]) {
      logSw('Hotkey config changed in storage; broadcasting to tabs.');
      broadcastHotkeyConfig();
    }
    if (changes[SETTINGS_STORAGE_KEY]) {
      logSw('Extension settings changed in storage; broadcasting to tabs.');
      broadcastExtensionSettings();
    }
  } else if (areaName === 'local') {
    // Slice-group defs (expand_groups/active_group) now live in storage.LOCAL
    // (see EXPAND_GROUPS_LOCAL_KEY). A change here -- a widget edit, an MCP
    // create/update_slice_group, a sibling tab, or the one-time migration --
    // must re-broadcast the MERGED settings so every tab's in-memory mirror
    // stays current. Since U7 ([ap:256S33]) the mirror is the DISPLAY cache
    // (F8, the widget table); the server's slice-group config reads go to the
    // durable store via get_expand_groups and never consult it, so a late
    // echo can no longer feed a stale merge base.
    if (changes[EXPAND_GROUPS_LOCAL_KEY]) {
      logSw('Slice-group defs changed in local storage; broadcasting to tabs.');
      broadcastExtensionSettings();
    }
    if (changes[ANCHOR_VIEWS_LOCAL_KEY]) {
      logSw('Anchor-view defs changed in local storage; broadcasting to tabs.');
      broadcastExtensionSettings();
    }
  }
});

// ---------------------------------------------------------------------------
// Message routing — chrome.runtime.onMessage handles requests from the
// isolated-world content-bridge.js (which itself proxies the page-context
// main-world client).
// ---------------------------------------------------------------------------
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.__glimpse_bridge__ !== true) return false;

  if (sender && sender.tab && sender.tab.id != null) {
    workflowyTabs.add(sender.tab.id);
  }

  if (message.direction === 'page-to-sw') {
    if (message.kind === 'send') {
      // Inspect outbound frames so the SW can maintain the endpoint->tab
      // routing table used for multiplexed delivery of broker responses.
      try {
        const parsed = JSON.parse(message.payload);
        const tabId = sender && sender.tab ? sender.tab.id : null;
        if (parsed && tabId != null) {
          const act = parsed.action;
          if (act === 'endpoint_register' || act === 'endpoint_update'
              || act === 'browser_endpoint_register') {
            const epId = parsed.endpoint_id;
            if (epId) {
              // Maintain bidirectional map; drop any prior endpoint for this tab.
              const prior = tabToEndpoint.get(tabId);
              if (prior && prior !== epId) endpointToTab.delete(prior);
              endpointToTab.set(epId, tabId);
              tabToEndpoint.set(tabId, epId);
              // Saved-Tabs Tier-2 drive readiness (spec [ap:7S217K] G2/D2):
              // the ONE notification site — a drive-created tab's
              // endpoint_register / endpoint_update observation resolves its
              // pendingTabReadiness waiter here, immediately after the routing
              // map gains the endpoint. Event-true by construction: never a
              // timer, never a poll (the ready cap is liveness hygiene only).
              // browser_endpoint_register is deliberately NOT a readiness
              // signal (G2 names register/update).
              if (act === 'endpoint_register' || act === 'endpoint_update') {
                const readyWaiter = pendingTabReadiness.get(tabId);
                if (readyWaiter) readyWaiter.resolve(epId);
              }
            }
          } else if (act === 'endpoint_unregister'
              || act === 'browser_endpoint_unregister') {
            const epId = parsed.endpoint_id;
            if (epId) {
              endpointToTab.delete(epId);
              if (tabToEndpoint.get(tabId) === epId) tabToEndpoint.delete(tabId);
            }
          }
        }
      } catch (e) { /* non-JSON or no tab; forward as-is */ }
      const ok = sendToServer(message.payload);
      sendResponse({
        __glimpse_bridge__: true,
        ok,
        readyState: ws ? ws.readyState : WebSocket.CLOSED,
      });
      return true;
    }
    if (message.kind === 'query_state') {
      sendResponse({
        __glimpse_bridge__: true,
        readyState: ws ? ws.readyState : WebSocket.CLOSED,
      });
      return true;
    }
    if (message.kind === 'request_connect') {
      connectWebSocket();
      sendResponse({
        __glimpse_bridge__: true,
        readyState: ws ? ws.readyState : WebSocket.CLOSED,
      });
      return true;
    }
    if (message.kind === 'set_extension_setting') {
      // Persist a single extension_settings key written by the page client
      // (e.g. the MD-preview dock toggle / width / split fraction adjusted
      // from the docked panel's ribbon). We read the RAW stored object (not
      // the merged view) so we round-trip exactly what the user has, set the
      // one key, and write it back. The chrome.storage.onChanged listener
      // above then broadcasts the freshly-merged settings to every tab.
      try {
        const key = message.payload && message.payload.key;
        const value = message.payload && message.payload.value;
        if (!key) {
          sendResponse({ __glimpse_bridge__: true, ok: false, error: 'missing key' });
          return true;
        }
        chrome.storage.sync.get([SETTINGS_STORAGE_KEY], (raw) => {
          const current = (raw && raw[SETTINGS_STORAGE_KEY]) || {};
          current[key] = value;
          chrome.storage.sync.set({ [SETTINGS_STORAGE_KEY]: current }, () => {
            sendResponse({ __glimpse_bridge__: true, ok: !chrome.runtime.lastError,
              error: chrome.runtime.lastError ? String(chrome.runtime.lastError) : undefined });
          });
        });
      } catch (e) {
        sendResponse({ __glimpse_bridge__: true, ok: false, error: String(e) });
      }
      return true; // async response
    }
    if (message.kind === 'set_extension_settings') {
      // Persist MULTIPLE extension_settings keys in ONE atomic read-modify-write.
      // This exists because writing several keys via separate
      // 'set_extension_setting' (singular) messages RACES: each message does an
      // independent async get->set of the whole settings object, so two
      // near-simultaneous writes can each read the pre-write object and the
      // second clobbers the first. (Symptom: adding an F8 expand group flashed
      // in and immediately reverted, because the 'active_group' write reverted
      // the 'expand_groups' write.) Callers that change >1 key at once MUST use
      // this plural form so the patch is applied as a single get->set.
      try {
        const patch = message.payload && message.payload.patch;
        if (!patch || typeof patch !== 'object') {
          sendResponse({ __glimpse_bridge__: true, ok: false, error: 'missing patch' });
          return true;
        }
        chrome.storage.sync.get([SETTINGS_STORAGE_KEY], (raw) => {
          const current = (raw && raw[SETTINGS_STORAGE_KEY]) || {};
          for (const k of Object.keys(patch)) {
            current[k] = patch[k];
          }
          chrome.storage.sync.set({ [SETTINGS_STORAGE_KEY]: current }, () => {
            sendResponse({ __glimpse_bridge__: true, ok: !chrome.runtime.lastError,
              error: chrome.runtime.lastError ? String(chrome.runtime.lastError) : undefined });
          });
        });
      } catch (e) {
        sendResponse({ __glimpse_bridge__: true, ok: false, error: String(e) });
      }
      return true; // async response
    }
    if (message.kind === 'set_expand_groups') {
      // Persist the F8 slice-group defs ({ expand_groups, active_group }) to
      // chrome.storage.LOCAL (see EXPAND_GROUPS_LOCAL_KEY). This is the write
      // twin of the sync 'set_extension_settings' path, deliberately kept
      // SEPARATE so slice-group defs land in local (10MB, no per-item cap)
      // while everything else stays in sync. It is the ONLY writer of the local
      // key, driven by the client's persistExpandGroups (widget edits AND the
      // MCP create/update_slice_group tools both funnel through it). The ack
      // carries ok + any chrome.runtime.lastError so the client can surface a
      // loud save-status message instead of silently losing data (the old bug).
      try {
        const patch = message.payload && message.payload.patch;
        if (!patch || typeof patch !== 'object') {
          sendResponse({ __glimpse_bridge__: true, ok: false, error: 'missing patch' });
          return true;
        }
        chrome.storage.local.get([EXPAND_GROUPS_LOCAL_KEY], (raw) => {
          const current = (raw && raw[EXPAND_GROUPS_LOCAL_KEY]) || {};
          for (const k of Object.keys(patch)) {
            current[k] = patch[k];
          }
          chrome.storage.local.set({ [EXPAND_GROUPS_LOCAL_KEY]: current }, () => {
            sendResponse({ __glimpse_bridge__: true, ok: !chrome.runtime.lastError,
              error: chrome.runtime.lastError ? String(chrome.runtime.lastError) : undefined });
          });
        });
      } catch (e) {
        sendResponse({ __glimpse_bridge__: true, ok: false, error: String(e) });
      }
      return true; // async response
    }
    if (message.kind === 'get_expand_groups') {
      // U7 ([ap:256S33]): the durable READ twin of set_expand_groups. Answers
      // the CURRENT expand_groups/active_group from chrome.storage via the ONE
      // merge authority loadExtensionSettings() (sync blob + local key + the
      // one-time migration fallback — no new merge logic here, and no new copy
      // of the settings anywhere). Deliberately NOT sourced from any tab's
      // in-memory mirror: the page-world get_slice_group_config handler asks
      // for this so the server's read-modify-write merge base is the durable
      // store every writer completes into — a late broadcast echo (which can
      // revert a tab's mirror) or a broker route to a lagging tab can no
      // longer feed a stale base. The content-bridge forwards
      // expand_groups/active_group transparently into the page's
      // bridge_response payload; ok:false makes the page handler fail loudly
      // (never fall back to the mirror for group state).
      try {
        loadExtensionSettings().then((settings) => {
          sendResponse({
            __glimpse_bridge__: true,
            ok: true,
            expand_groups: Array.isArray(settings && settings.expand_groups)
              ? settings.expand_groups
              : [],
            active_group: (settings && settings.active_group != null)
              ? settings.active_group
              : null,
          });
        }).catch((e) => {
          sendResponse({ __glimpse_bridge__: true, ok: false,
            error: (e && e.message) ? e.message : String(e) });
        });
      } catch (e) {
        sendResponse({ __glimpse_bridge__: true, ok: false, error: String(e) });
      }
      return true; // async response
    }
    if (message.kind === 'set_anchor_views') {
      // Persist the saved anchor-view defs ({ anchor_views }) to
      // chrome.storage.LOCAL (see ANCHOR_VIEWS_LOCAL_KEY). Completely
      // orthogonal to set_expand_groups — a separate local-storage key for a
      // separate data collection. The ack carries ok + any
      // chrome.runtime.lastError so the client can surface a loud save-status
      // message.
      try {
        const patch = message.payload && message.payload.patch;
        if (!patch || typeof patch !== 'object') {
          sendResponse({ __glimpse_bridge__: true, ok: false, error: 'missing patch' });
          return true;
        }
        chrome.storage.local.get([ANCHOR_VIEWS_LOCAL_KEY], (raw) => {
          const current = (raw && raw[ANCHOR_VIEWS_LOCAL_KEY]) || {};
          for (const k of Object.keys(patch)) {
            current[k] = patch[k];
          }
          chrome.storage.local.set({ [ANCHOR_VIEWS_LOCAL_KEY]: current }, () => {
            sendResponse({ __glimpse_bridge__: true, ok: !chrome.runtime.lastError,
              error: chrome.runtime.lastError ? String(chrome.runtime.lastError) : undefined });
          });
        });
      } catch (e) {
        sendResponse({ __glimpse_bridge__: true, ok: false, error: String(e) });
      }
      return true; // async response
    }
    if (message.kind === 'get_hotkey_config') {
      Promise.all([loadHotkeyConfig(), loadExtensionSettings()]).then(([config, settings]) => {
        sendResponse({
          __glimpse_bridge__: true,
          ok: true,
          config,
          settings,
        });
      }).catch((e) => {
        sendResponse({
          __glimpse_bridge__: true,
          ok: false,
          error: String(e),
        });
      });
      return true; // async response
    }
    if (message.kind === 'collect_saved_tabs') {
      // Saved Tabs (Tier-0) EXPORT: survey every Workflowy tab for its stored
      // modal render spec (see collectSavedTabsSpecs). Async: the widget's
      // sendResponse fires when every tab answered or the collection window
      // elapsed; the guarded respond() makes a double-sendResponse impossible.
      let responded = false;
      const respond = (obj) => {
        if (responded) return;
        responded = true;
        try { sendResponse(obj); } catch (e) { /* widget already gone */ }
      };
      collectSavedTabsSpecs(respond).catch((e) => {
        logSw('saved-tabs collect failed:', e);
        respond({
          __glimpse_bridge__: true,
          ok: false,
          error: 'collect failed: ' + String(e),
        });
      });
      return true; // async response
    }
    if (message.kind === 'saved_tabs_spec_response') {
      // One tab's page-world answer to the saved_tabs_spec_request survey
      // (endpoint_id + alias + the stored render_spec blob, VERBATIM). Matched
      // to its collection by request_id; the row is located by sender.tab.id.
      // When every surveyed tab has answered, the collection finishes early.
      try {
        const p = (message.payload && typeof message.payload === 'object') ? message.payload : {};
        const tabId = (sender && sender.tab && sender.tab.id != null) ? sender.tab.id : null;
        const coll = pendingSavedTabsCollections.get(p.request_id);
        if (coll && tabId != null) {
          const row = coll.state.byTab.get(tabId);
          if (row && !row.responded) {
            row.responded = true;
            row.result = (p.result && typeof p.result === 'object') ? p.result : null;
            // The tab's OWN endpoint identity overwrites the provisional map value.
            if (typeof p.endpoint_id === 'string' && p.endpoint_id) row.endpoint_id = p.endpoint_id;
            if (typeof p.alias === 'string') row.alias = p.alias;
          }
          let allDone = true;
          coll.state.byTab.forEach((r) => { if (!r.responded) allDone = false; });
          if (allDone) coll.finish();
        }
        sendResponse({ __glimpse_bridge__: true, ok: true });
      } catch (e) {
        sendResponse({ __glimpse_bridge__: true, ok: false, error: String(e) });
      }
      return true;
    }
    if (message.kind === 'saved_tabs_drive_request') {
      // Saved Tabs (Tier-2) DRIVE start (spec [ap:7S217K] §6; U01): the ack
      // is IMMEDIATE — acceptance only (the entries validation is
      // synchronous; a running drive is SUPERSEDED, never refused); every
      // outcome of the drive itself rides saved_tabs_drive_progress frames
      // to the origin tab. Extension-INTERNAL kind — never the broker wire
      // (A2).
      handleSavedTabsDriveRequest(message, sender, sendResponse);
      return true;
    }
    if (message.kind === 'saved_tabs_drive_cancel') {
      // U01/D6 amended: cancel stops the drive NOW — the in-flight wait is
      // interrupted; the in-flight entry is reported failed with a cancelled
      // reason and its tab stays open. A cancel naming no running drive is
      // answered with an aborted frame so a stale console can never stay
      // frozen. No page-side waiter — the bridge-level ack mirrors the
      // saved_tabs_spec_response idiom.
      handleSavedTabsDriveCancel(message, sender);
      sendResponse({ __glimpse_bridge__: true, ok: true });
      return true;
    }
    if (message.kind === 'saved_tabs_drive_apply_response') {
      // The created tab's terminal answer for ONE entry (§6 step 6): matched
      // to the in-flight entry by drive_id + entry_index (the
      // correlated-waiter idiom of saved_tabs_spec_response).
      handleSavedTabsDriveApplyResponse(message);
      sendResponse({ __glimpse_bridge__: true, ok: true });
      return true;
    }
  }
  return false;
});

// Drop tabs from our registry as they close, and tell the broker to drop the
// corresponding endpoint so it no longer routes to a dead tab.
chrome.tabs.onRemoved.addListener((tabId) => {
  workflowyTabs.delete(tabId);
  const ep = tabToEndpoint.get(tabId);
  if (ep) {
    endpointToTab.delete(ep);
    tabToEndpoint.delete(tabId);
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({ action: 'endpoint_unregister', endpoint_id: ep }));
      } catch (e) { /* best-effort */ }
    }
  }
});

// Keepalive alarm handler: wakes the SW every 30s, checks WebSocket health,
// reconnects if needed, otherwise sends a ping to exercise the connection.
chrome.alarms.onAlarm.addListener((alarm) => {
  if (!alarm || alarm.name !== KEEPALIVE_ALARM_NAME) return;

  if (!ws || ws.readyState !== WebSocket.OPEN) {
    logSw('Keepalive alarm: WebSocket not open; reconnecting.');
    connectWebSocket();
    return;
  }

  try {
    // Option B: carry the SW's authoritative live endpoint roster on the
    // keepalive ping so the broker can reconcile away ghost endpoints (killed
    // tabs whose endpoint_unregister was lost). roster_complete gates whether
    // the broker may PRUNE from it (only after the post-connect settling
    // window); before that it refreshes last_seen but never prunes.
    ws.send(JSON.stringify({
      action: 'ping',
      source: 'glimpse-sw-keepalive',
      endpoint_roster: currentEndpointRoster(),
      roster_complete: rosterIsAuthoritative(),
    }));
  } catch (e) {
    logSw('Keepalive ping failed; closing and reconnecting:', e);
    try { ws.close(); } catch (_) { /* no-op */ }
    ws = null;
    connectWebSocket();
  }
});

// ---------------------------------------------------------------------------
// Bootstrap — connect as soon as the service worker starts (which happens on
// extension install, browser startup, or whenever the SW wakes from idle in
// response to a tab event).
// ---------------------------------------------------------------------------
chrome.runtime.onStartup.addListener(() => {
  logSw('Extension started (browser launch)');
  ensureKeepaliveAlarm();
  connectWebSocket();
});

chrome.runtime.onInstalled.addListener(() => {
  logSw('Extension installed/updated');
  ensureKeepaliveAlarm();
  connectWebSocket();
});

// Eager initial connection attempt (the SW also starts when the first
// Workflowy tab fires a message; this catches the case where the SW is
// alive before any tab message arrives). Also register the keepalive alarm
// at top-level so it gets re-registered on every SW wake — chrome.alarms
// persists across SW restarts, but belt-and-suspenders since alarm
// registration is idempotent (chrome.alarms.create with an existing name
// just updates the schedule).
ensureKeepaliveAlarm();
connectWebSocket();
// Saved-Tabs Tier-2 (spec [ap:7S217K] D1): runs on EVERY SW wake (top level)
// — a drive marker surviving into a fresh SW lifetime means the previous
// drive died with the evicted worker: report the interruption loudly to the
// origin tab (best effort) and clear the marker. Never resumes (U1).
checkSavedTabsDriveEvictionMarker();
