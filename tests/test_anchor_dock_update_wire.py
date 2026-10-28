"""AnchorDockUpdate §9 wire path -- _handle_anchor_dock_update + dispatch.

Covers the browser-initiated star-toggle front:
  * a frame reaches the ONE canonical `_anchor_dock_update_impl` seam with the
    right kwargs (K1: dock_handle/favorite/local/families/target_tab);
  * the reply echoes `request_id` with the impl result;
  * `_AnchorOperationError` and `ProtectedNameViolation` surface STRUCTURED
    (error_code + extra intact -- K2);
  * an invalid frame replies an error and never reaches the impl;
  * a frame without `request_id` is dropped;
  * the `websocket_handler` dispatch clause task-dispatches the handler.

Model: local, no network, no real Workflowy.
"""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest
from coherence_harness import make_boundary_capable

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from workflowy_mcp import server as srv  # noqa: E402


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


def _wire_env(monkeypatch):
    """Boundary-capable client + stubbed admission gate (S04 suites own the gate)."""
    monkeypatch.setattr(
        srv,
        "get_client",
        lambda: make_boundary_capable(SimpleNamespace(), nodes=[]),
    )

    async def _noop_mutation_gate(paths, *, operation):
        return None

    monkeypatch.setattr(
        "workflowy_mcp.coherence.admission.require_coherence_mutation_admission",
        _noop_mutation_gate,
    )


def _stub_impl(monkeypatch, *, result=None, raises=None):
    """Stub the canonical seam; return the captured kwargs dict."""
    calls = {}

    async def _fake_impl(
        dock_handle,
        topic=None,
        description=None,
        local=None,
        target_tab=None,
        *,
        favorite=None,
        families=None,
    ):
        calls["dock_handle"] = dock_handle
        calls["topic"] = topic
        calls["description"] = description
        calls["local"] = local
        calls["target_tab"] = target_tab
        calls["favorite"] = favorite
        calls["families"] = families
        if raises is not None:
            raise raises
        if result is not None:
            return result
        return {
            "success": True,
            "no_op": False,
            "favorite": bool(favorite),
            "local": bool(local),
            "attributes": [],
            "families_updated": [],
            "tags": [],
            "warnings": [],
        }

    monkeypatch.setattr(srv, "_anchor_dock_update_impl", _fake_impl)
    return calls


def test_handler_reaches_impl_with_right_kwargs_and_echoes_request_id(monkeypatch):
    _wire_env(monkeypatch)
    calls = _stub_impl(monkeypatch)
    ws = _FakeWebSocket()
    asyncio.run(
        srv._handle_anchor_dock_update(
            {
                "action": "anchor_dock_update",
                "request_id": "r-42",
                "endpoint_id": "ep-7",
                "v": 1,
                "dock_handle": "D",
                "favorite": True,
                "families": "wfy-ns",
            },
            ws,
        )
    )
    assert calls["dock_handle"] == "D"
    assert calls["favorite"] is True
    assert calls["local"] is None
    assert calls["families"] == ["wfy-ns"]
    assert calls["target_tab"] == "ep-7"
    # topic/description are NOT part of this concern.
    assert calls["topic"] is None
    assert calls["description"] is None

    assert len(ws.sent) == 1
    reply = ws.sent[0]
    assert reply["action"] == "anchor_dock_update_result"
    assert reply["request_id"] == "r-42"
    assert reply["success"] is True
    assert reply["favorite"] is True


def test_handler_target_tab_none_when_endpoint_absent(monkeypatch):
    _wire_env(monkeypatch)
    calls = _stub_impl(monkeypatch)
    ws = _FakeWebSocket()
    asyncio.run(
        srv._handle_anchor_dock_update(
            {
                "action": "anchor_dock_update",
                "request_id": "r-1",
                "v": 1,
                "dock_handle": "D",
                "favorite": False,
            },
            ws,
        )
    )
    assert calls["target_tab"] is None
    assert calls["favorite"] is False


def test_handler_surfaces_anchor_operation_error_structured(monkeypatch):
    _wire_env(monkeypatch)
    extra = {"families_present": ["wfy-ns", "convrisk-docks"]}
    _stub_impl(
        monkeypatch,
        raises=srv._AnchorOperationError(
            "dock_families_ambiguous", "two families", extra=extra
        ),
    )
    ws = _FakeWebSocket()
    asyncio.run(
        srv._handle_anchor_dock_update(
            {
                "action": "anchor_dock_update",
                "request_id": "r-err",
                "v": 1,
                "dock_handle": "D",
                "favorite": True,
            },
            ws,
        )
    )
    reply = ws.sent[0]
    assert reply["action"] == "anchor_dock_update_result"
    assert reply["request_id"] == "r-err"
    assert reply["success"] is False
    assert reply["error_code"] == "dock_families_ambiguous"
    assert reply["extra"] == extra  # K2: extra.families_present survives


def test_handler_surfaces_protected_name_violation_structured(monkeypatch):
    from workflowy_mcp.client.api_client_nexus import ProtectedNameViolation

    _wire_env(monkeypatch)
    _stub_impl(
        monkeypatch,
        raises=ProtectedNameViolation(
            "protected_name_violation", "topic carries a tag", extra={"x": 1}
        ),
    )
    ws = _FakeWebSocket()
    asyncio.run(
        srv._handle_anchor_dock_update(
            {
                "action": "anchor_dock_update",
                "request_id": "r-pv",
                "v": 1,
                "dock_handle": "D",
                "local": True,
            },
            ws,
        )
    )
    reply = ws.sent[0]
    assert reply["success"] is False
    assert reply["error_code"] == "protected_name_violation"
    assert reply["extra"] == {"x": 1}


def test_handler_invalid_frame_replies_error_without_reaching_impl(monkeypatch):
    _wire_env(monkeypatch)
    calls = _stub_impl(monkeypatch)
    ws = _FakeWebSocket()
    asyncio.run(
        srv._handle_anchor_dock_update(
            {
                "action": "anchor_dock_update",
                "request_id": "r-bad",
                "v": 1,
                "dock_handle": "D",
                "favorite": "yes",  # not a JSON boolean
            },
            ws,
        )
    )
    assert len(ws.sent) == 1
    reply = ws.sent[0]
    assert reply["success"] is False
    assert "favorite" in reply["error"]
    assert "dock_handle" not in calls  # impl never called


def test_handler_drops_frames_without_request_id(monkeypatch):
    _wire_env(monkeypatch)
    _stub_impl(monkeypatch)
    ws = _FakeWebSocket()
    asyncio.run(
        srv._handle_anchor_dock_update(
            {
                "action": "anchor_dock_update",
                "v": 1,
                "dock_handle": "D",
                "favorite": True,
            },
            ws,
        )
    )
    assert ws.sent == []


# ---------------------------------------------------------------------------
# Dispatch clause: websocket_handler task-dispatches anchor_dock_update.
# ---------------------------------------------------------------------------


class _FakeDispatchWebSocket:
    def __init__(self, frames):
        self._frames = list(frames)
        self.remote_address = ("127.0.0.1", 0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        raise StopAsyncIteration

    async def send(self, payload):
        pass


def test_websocket_handler_task_dispatches_anchor_dock_update(monkeypatch):
    import workflowy_mcp.coherence.operation_cache as opc

    sentinel = {}

    async def _sentinel_handler(data, websocket):
        # Reached only if the dispatch clause calls the handler with the frame.
        sentinel["action"] = data.get("action")

    # Replace the (boundary-wrapped) handler with a name-distinct sentinel so
    # the spawned coroutine is identifiable by its code object.
    monkeypatch.setattr(srv, "_handle_anchor_dock_update", _sentinel_handler)

    captured = {}

    def _fake_spawn(coro):
        captured["name"] = getattr(getattr(coro, "cr_code", None), "co_name", None)
        captured["data"] = coro.cr_frame.f_locals.get("data")
        try:
            coro.close()
        except Exception:
            pass
        return None

    monkeypatch.setattr(opc, "create_isolated_context_task", _fake_spawn)
    ws = _FakeDispatchWebSocket(
        [
            json.dumps(
                {
                    "action": "anchor_dock_update",
                    "request_id": "r-dispatch",
                    "v": 1,
                    "dock_handle": "D",
                    "favorite": True,
                }
            )
        ]
    )
    asyncio.run(srv.websocket_handler(ws))
    assert captured.get("name") == "_sentinel_handler"
    assert (captured.get("data") or {}).get("action") == "anchor_dock_update"
    assert sentinel == {}  # the stub spawner never awaited the handler
