"""CFGOP1 transport-consolidation tests.

Locks the ONE nested get_slice_group_config transport helper
(_read_slice_group_config_nested): the no-tab envelope, the WSLIVE2 ghost
envelope (code='unknown_endpoint'), the generic-exception envelope, per-site
context phrasing, error_type folding (the scoped-Lightning site), endpoint
routing + tab_optional semantics, and the success passthrough. All local:
no network, no real Workflowy, no disk.
"""
import asyncio
import os
import sys

import pytest  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server as srv  # noqa: E402


def _stub_client(monkeypatch, *, raise_exc=None, capture=None):
    class _C:
        async def _ws_request_response(self, action, **kwargs):
            if capture is not None:
                capture["action"] = action
                capture["kwargs"] = kwargs
            if raise_exc is not None:
                raise raise_exc
            return {"expand_groups": [], "active_group": None}

    c = _C()
    monkeypatch.setattr(srv, "get_client", lambda: c)
    return c


def test_no_tab_envelope_carries_context(monkeypatch):
    _stub_client(monkeypatch)
    monkeypatch.setattr(srv, "get_ws_connection", lambda: (None, None))
    reply, err = asyncio.run(srv._read_slice_group_config_nested(
        endpoint_id="tab-1", context="read slice-group config to render the slice",
    ))
    assert reply is None
    assert err["success"] is False
    assert "No live Workflowy tab connected" in err["error"]
    assert "read slice-group config to render the slice" in err["error"]
    assert "error_type" not in err


def test_ghost_envelope_has_unknown_endpoint_code_and_context(monkeypatch):
    _stub_client(monkeypatch, raise_exc=RuntimeError("broker: unknown_endpoint 'tab-1'"))
    monkeypatch.setattr(srv, "get_ws_connection", lambda: (object(), object()))
    reply, err = asyncio.run(srv._read_slice_group_config_nested(
        endpoint_id="tab-1", context="read slice-group config to compute the F8 expansion plan",
    ))
    assert reply is None
    assert err["success"] is False
    assert err["code"] == "unknown_endpoint"
    assert "read slice-group config to compute the F8 expansion plan" in err["error"]
    assert "re-register" in err["error"]


def test_generic_exception_envelope(monkeypatch):
    _stub_client(monkeypatch, raise_exc=RuntimeError("boom"))
    monkeypatch.setattr(srv, "get_ws_connection", lambda: (object(), object()))
    reply, err = asyncio.run(srv._read_slice_group_config_nested(
        endpoint_id=None, context="read slice-group config to render the slice",
    ))
    assert reply is None
    assert err["success"] is False
    assert "boom" in err["error"]
    assert "code" not in err  # generic failures carry no ghost code


def test_error_type_folds_into_both_envelopes(monkeypatch):
    monkeypatch.setattr(srv, "get_ws_connection", lambda: (object(), object()))
    for exc in (RuntimeError("unknown_endpoint"), RuntimeError("boom")):
        _stub_client(monkeypatch, raise_exc=exc)
        reply, err = asyncio.run(srv._read_slice_group_config_nested(
            endpoint_id="tab-1",
            context="read slice-group config to re-project the frozen slice",
            error_type="scoped_lightning_config_read_failed",
        ))
        assert reply is None
        assert err["error_type"] == "scoped_lightning_config_read_failed", err


def test_endpoint_routing_and_tab_optional(monkeypatch):
    capture = {}
    _stub_client(monkeypatch, capture=capture)
    monkeypatch.setattr(srv, "get_ws_connection", lambda: ("WS", "Q"))
    reply, err = asyncio.run(srv._read_slice_group_config_nested(
        endpoint_id="tab-9", context="read slice-group config to render the slice",
    ))
    assert err is None
    assert reply == {"expand_groups": [], "active_group": None}
    kw = capture["kwargs"]
    assert capture["action"] == "get_slice_group_config"
    assert kw["target_endpoint_id"] == "tab-9"
    assert kw["tab_optional"] is False          # routed at the ORIGINATING tab
    assert kw["ws_connection"] == "WS" and kw["ws_queue"] == "Q"

    # Endpoint-absent (defensive) case: tab_optional True, no target.
    capture.clear()
    reply, err = asyncio.run(srv._read_slice_group_config_nested(
        endpoint_id=None, context="read slice-group config to render the slice",
    ))
    assert err is None
    assert capture["kwargs"]["target_endpoint_id"] is None
    assert capture["kwargs"]["tab_optional"] is True
