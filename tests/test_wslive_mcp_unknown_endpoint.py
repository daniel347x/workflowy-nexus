"""WSLIVE regression tests — MCP-leg STEP 3 (unknown_endpoint -> loud raise).

Covers the MCP-side behavior of the broker/endpoint-liveness plan (STEP 3 +
REVISION 3): a correlated Direction-A reply carrying the unified ghost code
``unknown_endpoint`` must be recognized in ``_ws_request_response`` and raised
as a distinct, actionable ``NetworkError`` — never returned as success-shaped
data, and never hung to a receive-timeout. REV3: there is NO MCP-side
auto-retry; the raise IS the whole behavior (the broker's detection-site
re-register broadcast is the heal; the retry is the agent / next call).

Two levels:
  * The pure helper ``_raise_if_unknown_endpoint`` (raises on the ghost code;
    passes ordinary routing/config replies through untouched).
  * An end-to-end ``_ws_request_response`` round-trip over a REAL
    ``_WsRequestIdDemux``: the Future resolves via ``put`` (so no timeout) and
    the ghost reply is converted into a raised NetworkError.

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

from workflowy_mcp.server import _WsRequestIdDemux  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402
from workflowy_mcp.models.config import APIConfiguration  # noqa: E402
from workflowy_mcp.models.errors import NetworkError  # noqa: E402


def _client() -> WorkFlowyClientNexus:
    return WorkFlowyClientNexus(APIConfiguration(api_key="test-dummy-key"))


# ---------------------------------------------------------------------------
# The pure helper
# ---------------------------------------------------------------------------

def test_raise_helper_raises_on_unknown_endpoint():
    client = _client()
    with pytest.raises(NetworkError) as ei:
        client._raise_if_unknown_endpoint(
            {"success": False, "code": "unknown_endpoint",
             "error": "tab X not registered; retry"},
            "extract_dom",
        )
    msg = str(ei.value)
    assert "unknown_endpoint" in msg
    assert "extract_dom" in msg
    # The ghost's own actionable message is carried through.
    assert "retry" in msg


def test_raise_helper_default_message_when_no_error_field():
    client = _client()
    with pytest.raises(NetworkError) as ei:
        client._raise_if_unknown_endpoint(
            {"success": False, "code": "unknown_endpoint"}, "get_extension_settings"
        )
    assert "re-register" in str(ei.value).lower()


def test_raise_helper_passes_through_ordinary_errors():
    """Routing/config errors are NOT liveness ghosts -> no raise."""
    client = _client()
    for code in (
        "ambiguous_browser_target",
        "no_browser_connected",
        "unknown_browser_alias",
        "ambiguous_browser_alias",
    ):
        # Must NOT raise.
        client._raise_if_unknown_endpoint(
            {"success": False, "code": code, "error": "pick a tab"}, "extract_dom"
        )


def test_raise_helper_passes_through_success():
    client = _client()
    client._raise_if_unknown_endpoint({"success": True, "tree": {}}, "extract_dom")
    client._raise_if_unknown_endpoint(None, "extract_dom")  # non-dict tolerated


# ---------------------------------------------------------------------------
# End-to-end _ws_request_response over a real demux
# ---------------------------------------------------------------------------

class _SendThenReply:
    """Fake ws_connection whose .send() schedules an unknown_endpoint reply.

    The reply is delivered into the demux (via .put) on the running loop, so
    the waiter Future resolves normally — proving STEP 3 converts a RESOLVED
    (non-timeout) ghost reply into a raise, not a hang.
    """

    def __init__(self, demux: _WsRequestIdDemux, reply_maker) -> None:
        self._demux = demux
        self._reply_maker = reply_maker

    async def send(self, data: str) -> None:
        frame = json.loads(data)
        rid = frame.get("request_id")
        reply = self._reply_maker(rid)
        # Deliver on the loop so wait_for_correlated resolves (no timeout).
        asyncio.get_running_loop().create_task(self._demux.put(reply))


def test_ws_request_response_raises_on_ghost_reply_no_timeout():
    client = _client()

    async def go():
        demux = _WsRequestIdDemux()

        def reply_maker(rid):
            return {
                "success": False,
                "code": "unknown_endpoint",
                "request_id": rid,
                "error": "endpoint not registered; asked to re-register -- retry",
            }

        conn = _SendThenReply(demux, reply_maker)
        # A short timeout: if STEP 3 were broken and this hung, the test would
        # fail via TimeoutError rather than the expected NetworkError.
        await client._ws_request_response(
            "extract_dom", ws_connection=conn, ws_queue=demux, timeout_s=5.0
        )

    with pytest.raises(NetworkError) as ei:
        asyncio.run(go())
    assert "unknown_endpoint" in str(ei.value)


def test_ws_request_response_returns_normal_reply_untouched():
    client = _client()

    async def go():
        demux = _WsRequestIdDemux()

        def reply_maker(rid):
            return {"success": True, "request_id": rid, "tree": {"id": "n1"}}

        conn = _SendThenReply(demux, reply_maker)
        return await client._ws_request_response(
            "extract_dom", ws_connection=conn, ws_queue=demux, timeout_s=5.0
        )

    reply = asyncio.run(go())
    assert reply.get("success") is True
    assert reply.get("tree", {}).get("id") == "n1"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
