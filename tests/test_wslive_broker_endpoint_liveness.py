"""WSLIVE regression tests — broker-leg endpoint-liveness hardening.

Covers the broker-side pieces of the broker/endpoint-liveness plan
([ap:WSLIVE2] + REVISION 1/2/3):

  * STEP 1  — ``_safe_send`` returns a bool; the addressed MCP->browser
    resolve-miss (``unknown_browser_endpoint``) is normalized to the unified
    ``code:'unknown_endpoint'`` reply and a dead forward emits the same reply
    (no silent black-hole). REV3.1 code-filter: ordinary routing/config errors
    (ambiguous_browser_target / no_browser_connected / unknown_browser_alias /
    ambiguous_browser_alias) are PRESERVED and NEVER collapsed to
    unknown_endpoint.
  * AMENDMENT B (REV3) — a detected ghost FIRES the bounded + rate-limited
    ``broker_request_endpoint_register`` broadcast AT the detection site, and a
    rapid second hit for the same endpoint_id is rate-limited.
  * Regression lock — the BROADCAST fan-out swallow path (no request_id, no
    target) still swallows a dead peer and does NOT emit unknown_endpoint.

All local: no network, no real Workflowy, no disk. Drives the REAL
``_handle_connection`` coroutine by feeding it framed messages over a fake
socket and capturing what it sends back.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import broker as brk  # noqa: E402
from workflowy_mcp.broker import Broker, _safe_send  # noqa: E402


class _FakeState:
    def __init__(self, name: str) -> None:
        self.name = name


OPEN = _FakeState("OPEN")
CLOSED = _FakeState("CLOSED")


class RecordingWS:
    """Async-send fake socket that records every JSON frame it is sent.

    ``dead=True`` makes ``send`` raise (a closed socket), exercising the
    ``_safe_send`` False path. ``feed()`` queues inbound frames so the socket
    is async-iterable for ``_handle_connection``; after the queue drains the
    iterator ends (the connection closes cleanly).
    """

    def __init__(self, *, dead: bool = False, state: _FakeState = OPEN) -> None:
        self.dead = dead
        self.state = state
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._closed = False

    def feed(self, obj: dict) -> None:
        self._inbox.put_nowait(json.dumps(obj))

    def done(self) -> None:
        # Sentinel to end async iteration.
        self._inbox.put_nowait(None)

    async def send(self, data: str) -> None:
        if self.dead:
            raise RuntimeError("socket is dead")
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        self._closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item

    # Recorded frames of a given type/code, for assertions.
    def sent_with(self, **match) -> list[dict]:
        out = []
        for f in self.sent:
            if all(f.get(k) == v for k, v in match.items()):
                out.append(f)
        return out


def _mk_broker() -> Broker:
    return Broker(required_token="tok")


# ---------------------------------------------------------------------------
# STEP 1 — _safe_send returns a bool
# ---------------------------------------------------------------------------

def test_safe_send_returns_true_on_success():
    ws = RecordingWS()
    ok = asyncio.run(_safe_send(ws, {"hello": "world"}))
    assert ok is True
    assert ws.sent == [{"hello": "world"}]


def test_safe_send_returns_false_on_dead_socket():
    ws = RecordingWS(dead=True)
    ok = asyncio.run(_safe_send(ws, {"hello": "world"}))
    assert ok is False
    assert ws.sent == []


# ---------------------------------------------------------------------------
# Helpers to drive a REAL browser + MCP handshake through _handle_connection.
# ---------------------------------------------------------------------------

async def _register_browser_with_endpoint(broker, *, endpoint_id, alias=None):
    """Run a browser connection through _handle_connection: hello + register.

    Returns the RecordingWS (the browser socket) after the handler has
    processed the hello + endpoint_register and then closed (inbox drained).
    """
    ws = RecordingWS()
    ws.feed({"action": "hello", "role": "browser", "connection_id": "conn-1",
             "token": "tok"})
    ws.feed({"action": "endpoint_register", "endpoint_id": endpoint_id,
             "alias": alias, "tab_marker": "tm-1"})
    ws.done()
    await brk._handle_connection(broker, ws)
    return ws


# ---------------------------------------------------------------------------
# STEP 1 + REV3.1 — resolve-miss ghost -> unified unknown_endpoint reply
# ---------------------------------------------------------------------------

def test_targeted_resolve_miss_emits_unified_unknown_endpoint(monkeypatch):
    """A GLIMPSE targeting an endpoint_id that is NOT registered gets a
    correlated reply whose code is the UNIFIED 'unknown_endpoint' (normalized
    from the resolver's 'unknown_browser_endpoint'), and the AMENDMENT-B
    re-register broadcast fires."""
    broker = _mk_broker()

    async def go():
        # One live browser tab exists (so there IS a connection to broadcast
        # to), but the GLIMPSE targets a DIFFERENT (unregistered) endpoint_id.
        browser = RecordingWS()
        broker.register_browser_connection(browser, "conn-live")
        broker.register_endpoint(browser, "ep-live", None, None, None, "tm")

        mcp = RecordingWS()
        mcp.feed({"action": "hello", "role": "mcp", "client_id": "mcp-1",
                  "token": "tok"})
        mcp.feed({"action": "extract_dom", "request_id": "r1",
                  "target_endpoint_id": "ep-GHOST"})
        mcp.done()
        await brk._handle_connection(broker, mcp)
        return browser, mcp

    browser, mcp = asyncio.run(go())

    # The originating MCP socket got exactly one unknown_endpoint reply.
    ghost_replies = mcp.sent_with(code="unknown_endpoint", request_id="r1")
    assert len(ghost_replies) == 1
    assert ghost_replies[0]["success"] is False
    # The re-register broadcast reached the live browser connection.
    assert browser.sent_with(type="broker_request_endpoint_register")


def test_ordinary_routing_errors_are_not_collapsed_to_unknown_endpoint():
    """REV3.1 code-filter: ambiguous_browser_target / no_browser_connected are
    preserved EXACTLY and never rewritten to unknown_endpoint, and they do NOT
    fire the re-register broadcast."""
    broker = _mk_broker()

    async def ambiguous():
        # Two live tabs, untargeted extract_dom -> ambiguous_browser_target.
        b1 = RecordingWS(); broker.register_browser_connection(b1, "c1")
        broker.register_endpoint(b1, "ep1", "spine", None, None, "tmA")
        b2 = RecordingWS(); broker.register_browser_connection(b2, "c2")
        broker.register_endpoint(b2, "ep2", "debug", None, None, "tmB")

        mcp = RecordingWS()
        mcp.feed({"action": "hello", "role": "mcp", "client_id": "m", "token": "tok"})
        mcp.feed({"action": "extract_dom", "request_id": "rr"})  # no target
        mcp.done()
        await brk._handle_connection(broker, mcp)
        return b1, b2, mcp

    b1, b2, mcp = asyncio.run(ambiguous())
    errs = mcp.sent_with(request_id="rr", success=False)
    assert len(errs) == 1
    assert errs[0]["code"] == "ambiguous_browser_target"
    # Not rewritten, and NO broadcast to either browser.
    assert not b1.sent_with(type="broker_request_endpoint_register")
    assert not b2.sent_with(type="broker_request_endpoint_register")


def test_no_browser_connected_error_preserved():
    broker = _mk_broker()

    async def go():
        mcp = RecordingWS()
        mcp.feed({"action": "hello", "role": "mcp", "client_id": "m", "token": "tok"})
        mcp.feed({"action": "extract_dom", "request_id": "rz"})  # no tabs at all
        mcp.done()
        await brk._handle_connection(broker, mcp)
        return mcp

    mcp = asyncio.run(go())
    errs = mcp.sent_with(request_id="rz", success=False)
    assert len(errs) == 1
    assert errs[0]["code"] == "no_browser_connected"


# ---------------------------------------------------------------------------
# STEP 1 — dead forward emits unknown_endpoint (not a silent black-hole)
# ---------------------------------------------------------------------------

def test_dead_forward_emits_unknown_endpoint_and_untracks():
    """A resolved endpoint whose socket is DEAD: the forward _safe_send fails,
    so the originator gets an unknown_endpoint reply (not a timeout) and the
    request is untracked."""
    broker = _mk_broker()

    async def go():
        # Register a browser + endpoint, THEN kill its socket so resolution
        # succeeds (endpoint present) but the forward send raises.
        browser = RecordingWS()
        broker.register_browser_connection(browser, "conn-1")
        broker.register_endpoint(browser, "ep-target", None, None, None, "tm")
        # A SECOND live browser so the broadcast has a live recipient too.
        live2 = RecordingWS()
        broker.register_browser_connection(live2, "conn-2")
        broker.register_endpoint(live2, "ep-other", None, None, None, "tm2")
        browser.dead = True  # forward to ep-target will fail

        mcp = RecordingWS()
        mcp.feed({"action": "hello", "role": "mcp", "client_id": "m", "token": "tok"})
        mcp.feed({"action": "extract_dom", "request_id": "rf",
                  "target_endpoint_id": "ep-target"})
        mcp.done()
        await brk._handle_connection(broker, mcp)
        return mcp

    mcp = asyncio.run(go())
    replies = mcp.sent_with(request_id="rf", code="unknown_endpoint")
    assert len(replies) == 1
    assert replies[0]["success"] is False
    assert replies[0]["target_endpoint_id"] == "ep-target"
    # The tracked request was cleaned up (no lingering pending entry).
    assert "rf" not in broker.pending


# ---------------------------------------------------------------------------
# AMENDMENT B — rate-limit gate
# ---------------------------------------------------------------------------

def test_reregister_broadcast_allowed_rate_limits_same_endpoint(monkeypatch):
    broker = _mk_broker()
    # First call for an endpoint_id is allowed; an immediate second is not.
    assert broker.reregister_broadcast_allowed("ep-x") is True
    assert broker.reregister_broadcast_allowed("ep-x") is False
    # A DIFFERENT endpoint_id is independently allowed.
    assert broker.reregister_broadcast_allowed("ep-y") is True
    # None/blank collapses to one shared sentinel key (still bounded).
    assert broker.reregister_broadcast_allowed(None) is True
    assert broker.reregister_broadcast_allowed("") is False


def test_reregister_broadcast_allowed_after_cooldown(monkeypatch):
    broker = _mk_broker()
    # Force a tiny cooldown so the window elapses immediately.
    monkeypatch.setattr(brk, "_REREGISTER_BROADCAST_COOLDOWN_S", 0.0)
    assert broker.reregister_broadcast_allowed("ep-z") is True
    # With a zero cooldown the very next call is allowed again.
    assert broker.reregister_broadcast_allowed("ep-z") is True


def test_rapid_second_ghost_hit_does_not_refire_broadcast():
    """Two back-to-back GLIMPSEs at the SAME ghost endpoint_id -> two loud
    replies, but only ONE re-register broadcast (rate-limited)."""
    broker = _mk_broker()

    async def go():
        browser = RecordingWS()
        broker.register_browser_connection(browser, "conn-live")
        broker.register_endpoint(browser, "ep-live", None, None, None, "tm")

        mcp = RecordingWS()
        mcp.feed({"action": "hello", "role": "mcp", "client_id": "m", "token": "tok"})
        mcp.feed({"action": "extract_dom", "request_id": "g1",
                  "target_endpoint_id": "ep-GHOST"})
        mcp.feed({"action": "extract_dom", "request_id": "g2",
                  "target_endpoint_id": "ep-GHOST"})
        mcp.done()
        await brk._handle_connection(broker, mcp)
        return browser, mcp

    browser, mcp = asyncio.run(go())
    # Both requests got their loud reply...
    assert len(mcp.sent_with(code="unknown_endpoint", request_id="g1")) == 1
    assert len(mcp.sent_with(code="unknown_endpoint", request_id="g2")) == 1
    # ...but the broadcast fired only ONCE (second hit rate-limited).
    broadcasts = browser.sent_with(type="broker_request_endpoint_register")
    assert len(broadcasts) == 1


# ---------------------------------------------------------------------------
# Regression lock — the BROADCAST fan-out swallow path is untouched
# ---------------------------------------------------------------------------

def test_broadcast_fanout_swallows_dead_peer_and_no_unknown_endpoint():
    """An MCP frame with NO request_id and NO target is a broadcast fan-out.
    A dead browser peer must be swallowed (routing survives) and NO
    unknown_endpoint reply is emitted for it."""
    broker = _mk_broker()

    async def go():
        dead = RecordingWS(dead=True)
        broker.register_browser_connection(dead, "conn-dead")
        broker.register_endpoint(dead, "ep-dead", None, None, None, "tm")
        live = RecordingWS()
        broker.register_browser_connection(live, "conn-live")
        broker.register_endpoint(live, "ep-live", None, None, None, "tm2")

        mcp = RecordingWS()
        mcp.feed({"action": "hello", "role": "mcp", "client_id": "m", "token": "tok"})
        # A browser-bound RESULT broadcast: no request_id, no target.
        mcp.feed({"action": "carto_list_jobs_result", "success": True})
        mcp.done()
        await brk._handle_connection(broker, mcp)
        return live, mcp

    live, mcp = asyncio.run(go())
    # The live peer received the broadcast; the dead one silently failed.
    assert live.sent_with(action="carto_list_jobs_result")
    # No unknown_endpoint reply was emitted to the MCP for the dead peer.
    assert not mcp.sent_with(code="unknown_endpoint")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
