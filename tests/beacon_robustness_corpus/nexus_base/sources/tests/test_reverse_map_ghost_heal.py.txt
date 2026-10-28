"""WSREVMP2 regression tests — SW reverse-map ghost heal (broker leg).

Covers [ap:WSREVMP2] + addendum [ap:WSRVP2A]: route the SW-originated
reverse-map ``code:'unknown_endpoint'`` ghost reply into the broker's ONE
existing owned heal path, so a reverse-map drop fails LOUD *and* self-heals.

Mechanism recap (candidate B, broker-owned):
  * The service worker cannot deliver a targeted frame to its tab (an
    ``endpointToTab`` MAP-MISS, or a purged-on-failed-send SEND-FAILURE), so it
    sends a CORRELATED ``{success:false, code:'unknown_endpoint', request_id,
    target_endpoint_id}`` frame back over the broker socket
    (``emitUnknownEndpointToBroker``).
  * The broker's ``_handle_connection`` browser->MCP path routes that frame to
    the waiter via ``resolve_request`` (the resolve-HIT branch) → the caller
    fails LOUD via ``_raise_if_unknown_endpoint`` (already shipped).
  * WSREVMP2 adds: at that same routing point, recognize the frame as a
    reverse-map liveness ghost (``frame_is_liveness_ghost``) and fire the SAME
    rate-limited ``_broadcast_request_endpoint_register`` the broker's own two
    ghost sites use — BEFORE forwarding the loud reply UNCHANGED.

Assertions (parent STEP 5 (a)-(d) + addendum (e)-(f)):
  (a) the loud reply is PRESERVED (routed to the waiter, not swallowed);
  (b) that same frame fires the re-register broadcast exactly ONCE;
  (c) a second identical frame within the cooldown does NOT re-fire it;
  (d) frame_is_liveness_ghost True for the ghost / False for ordinary replies;
  (e) frame_is_liveness_ghost STRICTNESS (needs code + request_id + a
      non-blank target_endpoint_id; rejects the resolver-shaped dict);
  (f) BRANCH ISOLATION — a request_id-bearing frame that is NOT a tracked
      outstanding request (the browser-INITIATED / MISS branch) routes to the
      primary MCP worker and fires NO broadcast, even with code:'unknown_endpoint'.

All local: no network, no real Workflowy, no disk. Drives the REAL
``_handle_connection`` coroutine over fake sockets (mirrors
tests/test_wslive_broker_endpoint_liveness.py).
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
from workflowy_mcp.broker import Broker  # noqa: E402


class RecordingWS:
    """Async-send fake socket that records every JSON frame it is sent.

    ``feed()`` queues inbound frames so the socket is async-iterable for
    ``_handle_connection``; after the queue drains the iterator ends (the
    connection closes cleanly). A socket used purely as a routing ORIGIN (an
    MCP waiter or the primary worker) is never iterated — it just records what
    the broker forwards to it.
    """

    def __init__(self, *, dead: bool = False) -> None:
        self.dead = dead
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue = asyncio.Queue()

    def feed(self, obj: dict) -> None:
        self._inbox.put_nowait(json.dumps(obj))

    def done(self) -> None:
        self._inbox.put_nowait(None)

    async def send(self, data: str) -> None:
        if self.dead:
            raise RuntimeError("socket is dead")
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def sent_with(self, **match) -> list[dict]:
        return [f for f in self.sent if all(f.get(k) == v for k, v in match.items())]


def _mk_broker() -> Broker:
    return Broker(required_token="tok")


# The exact wire shape the SW emits on a reverse-map drop
# (emitUnknownEndpointToBroker).
def _ghost_frame(request_id: str, endpoint_id: str = "ep-target") -> dict:
    return {
        "success": False,
        "code": "unknown_endpoint",
        "request_id": request_id,
        "target_endpoint_id": endpoint_id,
        "error": (
            "The target Workflowy tab's endpoint is not reachable; it has "
            "been asked to re-register -- retry."
        ),
    }


# ---------------------------------------------------------------------------
# (d) + (e) — frame_is_liveness_ghost classification + STRICTNESS
# ---------------------------------------------------------------------------

def test_frame_is_liveness_ghost_true_for_full_shape():
    broker = _mk_broker()
    assert broker.frame_is_liveness_ghost(_ghost_frame("r1")) is True


def test_frame_is_liveness_ghost_strictness_missing_fields():
    broker = _mk_broker()
    # (e) missing request_id -> not a ghost.
    assert broker.frame_is_liveness_ghost(
        {"code": "unknown_endpoint", "target_endpoint_id": "ep"}
    ) is False
    # (e) blank / absent target_endpoint_id -> not a ghost.
    assert broker.frame_is_liveness_ghost(
        {"code": "unknown_endpoint", "request_id": "r", "target_endpoint_id": ""}
    ) is False
    assert broker.frame_is_liveness_ghost(
        {"code": "unknown_endpoint", "request_id": "r", "target_endpoint_id": "   "}
    ) is False
    assert broker.frame_is_liveness_ghost(
        {"code": "unknown_endpoint", "request_id": "r"}
    ) is False


def test_frame_is_liveness_ghost_false_for_ordinary_and_resolver_shapes():
    broker = _mk_broker()
    # (d) an ordinary correlated success reply (e.g. an extract_dom result).
    assert broker.frame_is_liveness_ghost(
        {"success": True, "request_id": "r", "tree": {"id": "n1"}}
    ) is False
    # (d) an ordinary routing error code.
    assert broker.frame_is_liveness_ghost(
        {"success": False, "code": "unknown_target_tab", "request_id": "r",
         "target_endpoint_id": "ep"}
    ) is False
    # (e) the RESOLVER-shaped dict (miss_is_liveness_ghost's input, NOT this
    # predicate's) — different code, must not be misread as a wire ghost.
    assert broker.frame_is_liveness_ghost(
        {"code": "unknown_browser_endpoint", "request_id": "r",
         "target_endpoint_id": "ep"}
    ) is False
    # Non-dict tolerated.
    assert broker.frame_is_liveness_ghost(None) is False

    # Cross-check: the resolver dict IS a miss ghost (the sibling predicate),
    # proving the two predicates own their two distinct shapes.
    assert broker.miss_is_liveness_ghost(
        {"code": "unknown_browser_endpoint"}
    ) is True
    assert broker.miss_is_liveness_ghost(
        {"code": "unknown_endpoint"}
    ) is False


# ---------------------------------------------------------------------------
# (a) + (b) — loud reply preserved AND heal fires exactly once
# ---------------------------------------------------------------------------

def test_reverse_map_ghost_preserves_loud_reply_and_fires_heal_once():
    broker = _mk_broker()

    async def go():
        # A live browser connection to receive the re-register broadcast.
        browser = RecordingWS()
        broker.register_browser_connection(browser, "conn-live")
        broker.register_endpoint(browser, "ep-target", None, None, None, "tm")

        # The MCP waiter that originally issued the targeted request. It is
        # recorded as the origin for request_id 'r1' (as the broker would have
        # done when it forwarded the targeted GLIMPSE to the SW).
        waiter = RecordingWS()
        broker.track_request("r1", waiter)

        # The SW connection sends its correlated reverse-map ghost reply back.
        sw = RecordingWS()
        sw.feed({"action": "hello", "role": "browser",
                 "connection_id": "conn-sw", "token": "tok"})
        sw.feed(_ghost_frame("r1", "ep-target"))
        sw.done()
        await brk._handle_connection(broker, sw)
        return browser, waiter

    browser, waiter = asyncio.run(go())

    # (a) the loud reply reached the ORIGINAL waiter, unchanged (still the
    # ghost code, so the MCP side raises loud via _raise_if_unknown_endpoint).
    loud = waiter.sent_with(code="unknown_endpoint", request_id="r1")
    assert len(loud) == 1
    assert loud[0]["success"] is False
    assert loud[0]["target_endpoint_id"] == "ep-target"

    # (b) the heal fired exactly once to the live browser connection.
    broadcasts = browser.sent_with(type="broker_request_endpoint_register")
    assert len(broadcasts) == 1


# ---------------------------------------------------------------------------
# (c) — a second identical ghost within the cooldown does NOT re-fire
# ---------------------------------------------------------------------------

def test_second_reverse_map_ghost_is_rate_limited():
    broker = _mk_broker()

    async def go():
        browser = RecordingWS()
        broker.register_browser_connection(browser, "conn-live")
        broker.register_endpoint(browser, "ep-target", None, None, None, "tm")

        w1 = RecordingWS(); broker.track_request("g1", w1)
        w2 = RecordingWS(); broker.track_request("g2", w2)

        sw = RecordingWS()
        sw.feed({"action": "hello", "role": "browser",
                 "connection_id": "conn-sw", "token": "tok"})
        sw.feed(_ghost_frame("g1", "ep-target"))
        sw.feed(_ghost_frame("g2", "ep-target"))
        sw.done()
        await brk._handle_connection(broker, sw)
        return browser, w1, w2

    browser, w1, w2 = asyncio.run(go())

    # Both waiters still got their loud reply...
    assert len(w1.sent_with(code="unknown_endpoint", request_id="g1")) == 1
    assert len(w2.sent_with(code="unknown_endpoint", request_id="g2")) == 1
    # ...but the broadcast fired only ONCE (same endpoint_id, within cooldown).
    broadcasts = browser.sent_with(type="broker_request_endpoint_register")
    assert len(broadcasts) == 1


# ---------------------------------------------------------------------------
# (f) — BRANCH ISOLATION: a NON-tracked request_id is the browser-INITIATED
# (MISS) branch -> routed to the primary MCP worker, NO heal broadcast, even
# if it carries code:'unknown_endpoint'.
# ---------------------------------------------------------------------------

def test_untracked_request_id_frame_routes_to_primary_and_does_not_heal():
    broker = _mk_broker()

    async def go():
        # A live browser connection (would receive a broadcast IF one fired).
        browser = RecordingWS()
        broker.register_browser_connection(browser, "conn-live")
        broker.register_endpoint(browser, "ep-live", None, None, None, "tm")

        # A primary MCP worker to receive the browser-initiated frame.
        # Registered directly (not via a hello/teardown round-trip) so it
        # stays the live primary worker while the SW frame is processed --
        # otherwise the MCP iterator would drain, unregister, and leave no
        # primary, and the frame would be answered with failure to the SW.
        mcp = RecordingWS()
        broker.register_mcp(mcp, "mcp-1")

        # The SW sends a request_id-bearing frame whose request_id is NOT a
        # tracked outstanding request -> the browser-INITIATED (MISS) branch.
        # (Shaped like a ghost on purpose, to prove the heal is HIT-branch-only.)
        sw = RecordingWS()
        sw.feed({"action": "hello", "role": "browser",
                 "connection_id": "conn-sw", "token": "tok"})
        sw.feed(_ghost_frame("not-tracked", "ep-live"))
        sw.done()
        await brk._handle_connection(broker, sw)
        return browser, mcp

    browser, mcp = asyncio.run(go())

    # It was routed to the primary MCP worker (the MISS branch), verbatim.
    routed = mcp.sent_with(code="unknown_endpoint", request_id="not-tracked")
    assert len(routed) == 1
    # And NO re-register broadcast fired (heal lives in the resolve-HIT branch).
    assert not browser.sent_with(type="broker_request_endpoint_register")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
