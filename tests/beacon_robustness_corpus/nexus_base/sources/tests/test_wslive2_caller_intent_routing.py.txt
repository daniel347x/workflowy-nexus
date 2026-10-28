"""WSLIVE2 regression tests — caller-intent endpoint routing + the ONE ghost classifier.

Covers the WSLIVE2 fix ([ap:WSLIV2] sky view, [ap:WSLIV3] plan E1-E7,
[ap:WSLIV4] amendment) that makes endpoint LIVENESS a single owned invariant:

  * E1  — ``_ws_request_response`` stamps a CALLER-HELD endpoint_id as
    ``target_endpoint_id`` (routed via ``resolve_target_endpoint`` -> the
    ``unknown_browser_endpoint`` ghost rail), NEVER laundered through
    ``target_tab`` (routed via ``resolve_target_tab`` -> the ordinary
    ``unknown_target_tab`` routing error). If BOTH are passed,
    ``target_endpoint_id`` wins and ``target_tab`` is omitted.
  * E5  — ``Broker.miss_is_liveness_ghost`` is the ONE classifier: True iff
    ``code == 'unknown_browser_endpoint'``; False for the five ordinary codes
    (incl. ``unknown_target_tab``). This is what DISCRIMINATES a stale endpoint
    (heal + re-register) from a human/agent ``target_tab`` typo (plain error).
  * Both sides end-to-end through the REAL broker ``_handle_connection``:
    (a) a ghost via ``target_endpoint_id`` -> unified ``unknown_endpoint`` +
        the re-register broadcast fires; and
    (b) a ``target_tab`` typo -> ``unknown_target_tab`` preserved, NOT collapsed
        to ``unknown_endpoint``, and NO broadcast.
  * Regression — a LIVE ``target_endpoint_id`` still resolves + forwards.

All local: no network, no real Workflowy, no disk.
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
from workflowy_mcp.server import _WsRequestIdDemux  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402
from workflowy_mcp.models.config import APIConfiguration  # noqa: E402


def _client() -> WorkFlowyClientNexus:
    return WorkFlowyClientNexus(APIConfiguration(api_key="test-dummy-key"))


def _mk_broker() -> Broker:
    return Broker(required_token="tok")


# ===========================================================================
# E5 — the ONE owned classifier: miss_is_liveness_ghost truth table (DoD-A)
# ===========================================================================

def test_miss_is_liveness_ghost_truth_table():
    """Only a TARGETED endpoint miss (unknown_browser_endpoint) is a ghost.

    Every other resolver code (the four from resolve_target_endpoint plus
    resolve_target_tab's unknown_target_tab) is an ordinary routing/config
    error and must NOT be classified as a liveness ghost. Non-dict inputs are
    tolerated (False)."""
    broker = _mk_broker()

    # The one True case.
    assert broker.miss_is_liveness_ghost({"code": "unknown_browser_endpoint"}) is True

    # The five False cases (all real resolver codes).
    for code in (
        "unknown_browser_alias",
        "ambiguous_browser_alias",
        "no_browser_connected",
        "ambiguous_browser_target",
        "unknown_target_tab",
    ):
        assert broker.miss_is_liveness_ghost({"code": code}) is False, code

    # Degenerate / defensive inputs.
    assert broker.miss_is_liveness_ghost({}) is False
    assert broker.miss_is_liveness_ghost({"code": ""}) is False
    assert broker.miss_is_liveness_ghost(None) is False
    assert broker.miss_is_liveness_ghost("unknown_browser_endpoint") is False


# ===========================================================================
# E1 — _ws_request_response caller-intent frame stamping
# ===========================================================================

class _CaptureSendThenSucceed:
    """Fake ws_connection: records the sent frame, then replies success.

    The reply is delivered into the demux so the waiter Future resolves (no
    timeout); ``sent`` holds the exact outbound frame for assertions.
    """

    def __init__(self, demux: _WsRequestIdDemux) -> None:
        self._demux = demux
        self.sent: list[dict] = []

    async def send(self, data: str) -> None:
        frame = json.loads(data)
        self.sent.append(frame)
        rid = frame.get("request_id")
        asyncio.get_running_loop().create_task(
            self._demux.put({"success": True, "request_id": rid, "ok": 1})
        )


def _roundtrip_capture(**kwargs) -> dict:
    """Run one _ws_request_response and return the single sent frame."""
    client = _client()

    async def go() -> dict:
        demux = _WsRequestIdDemux()
        conn = _CaptureSendThenSucceed(demux)
        await client._ws_request_response(
            "get_slice_group_config",
            ws_connection=conn,
            ws_queue=demux,
            timeout_s=5.0,
            **kwargs,
        )
        assert conn.sent, "no frame was sent"
        return conn.sent[-1]

    return asyncio.run(go())


def test_ws_request_response_stamps_target_endpoint_id_not_target_tab():
    """A caller passing target_endpoint_id gets it on the frame; NO target_tab."""
    frame = _roundtrip_capture(target_endpoint_id="ep-x")
    assert frame.get("target_endpoint_id") == "ep-x"
    assert "target_tab" not in frame


def test_ws_request_response_prefers_endpoint_id_when_both_passed():
    """Contract: if BOTH are passed, target_endpoint_id wins, target_tab omitted."""
    frame = _roundtrip_capture(target_endpoint_id="ep-x", target_tab="spine")
    assert frame.get("target_endpoint_id") == "ep-x"
    assert "target_tab" not in frame


def test_ws_request_response_still_stamps_target_tab_when_alone():
    """Regression: a plain target_tab (no endpoint_id) is unchanged behavior."""
    frame = _roundtrip_capture(target_tab="spine")
    assert frame.get("target_tab") == "spine"
    assert "target_endpoint_id" not in frame


# ===========================================================================
# Broker end-to-end harness (mirrors test_wslive_broker_endpoint_liveness.py)
# ===========================================================================

class RecordingWS:
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


# ---- (a) GHOST via target_endpoint_id -> unified unknown_endpoint + broadcast

def test_endpoint_id_ghost_emits_unknown_endpoint_and_broadcasts():
    """A nested read routed by an ORIGINATING endpoint_id that has ghosted:
    resolve_target_endpoint misses -> miss_is_liveness_ghost True -> the reply
    is normalized to code:'unknown_endpoint' AND the re-register broadcast
    fires against the live browser connection."""
    broker = _mk_broker()

    async def go():
        # A live tab exists (broadcast target), but the read targets a
        # DIFFERENT, unregistered (ghosted) endpoint_id.
        browser = RecordingWS()
        broker.register_browser_connection(browser, "conn-live")
        broker.register_endpoint(browser, "ep-live", None, None, None, "tm")

        mcp = RecordingWS()
        mcp.feed({"action": "hello", "role": "mcp", "client_id": "m", "token": "tok"})
        mcp.feed({"action": "get_slice_group_config", "request_id": "n1",
                  "target_endpoint_id": "ep-GHOST"})
        mcp.done()
        await brk._handle_connection(broker, mcp)
        return browser, mcp

    browser, mcp = asyncio.run(go())
    ghost = mcp.sent_with(code="unknown_endpoint", request_id="n1")
    assert len(ghost) == 1, mcp.sent
    assert ghost[0]["success"] is False
    # NOT masked as an ordinary error, and the heal broadcast reached the tab.
    assert browser.sent_with(type="broker_request_endpoint_register")


# ---- (b) target_tab TYPO -> unknown_target_tab, NOT a ghost, NO broadcast

def test_target_tab_typo_stays_unknown_target_tab_and_no_broadcast():
    """The discrimination test (GPT-5.5 #1): a human/agent target_tab TYPO
    resolves via resolve_target_tab to 'unknown_target_tab' -- which
    miss_is_liveness_ghost classifies as NOT a ghost -- so the reply preserves
    that code, is NOT collapsed to unknown_endpoint, and fires NO re-register
    broadcast."""
    broker = _mk_broker()

    async def go():
        browser = RecordingWS()
        broker.register_browser_connection(browser, "conn-live")
        broker.register_endpoint(browser, "ep-live", "spine", None, None, "tm")

        mcp = RecordingWS()
        mcp.feed({"action": "hello", "role": "mcp", "client_id": "m", "token": "tok"})
        # A target_tab that is neither a known alias nor an endpoint_id.
        mcp.feed({"action": "extract_dom", "request_id": "t1",
                  "target_tab": "no-such-tab"})
        mcp.done()
        await brk._handle_connection(broker, mcp)
        return browser, mcp

    browser, mcp = asyncio.run(go())
    errs = mcp.sent_with(request_id="t1", success=False)
    assert len(errs) == 1, mcp.sent
    assert errs[0]["code"] == "unknown_target_tab"
    # NOT rewritten to the ghost code, and NO heal broadcast.
    assert not mcp.sent_with(code="unknown_endpoint", request_id="t1")
    assert not browser.sent_with(type="broker_request_endpoint_register")


# ---- (d) Regression: a LIVE target_endpoint_id resolves + forwards

def test_live_target_endpoint_id_forwards_to_that_tab():
    """A nested read routed by a LIVE endpoint_id forwards to that tab (tagged
    with target_endpoint_id) and emits NO error / NO broadcast."""
    broker = _mk_broker()

    async def go():
        browser = RecordingWS()
        broker.register_browser_connection(browser, "conn-1")
        broker.register_endpoint(browser, "ep-live", None, None, None, "tm")

        mcp = RecordingWS()
        mcp.feed({"action": "hello", "role": "mcp", "client_id": "m", "token": "tok"})
        mcp.feed({"action": "get_slice_group_config", "request_id": "L1",
                  "target_endpoint_id": "ep-live"})
        mcp.done()
        await brk._handle_connection(broker, mcp)
        return browser, mcp

    browser, mcp = asyncio.run(go())
    # The frame was forwarded to the live tab, tagged with the endpoint.
    fwd = browser.sent_with(action="get_slice_group_config", request_id="L1")
    assert len(fwd) == 1, browser.sent
    assert fwd[0]["target_endpoint_id"] == "ep-live"
    # No ghost reply, no heal broadcast.
    assert not mcp.sent_with(code="unknown_endpoint")
    assert not browser.sent_with(type="broker_request_endpoint_register")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
