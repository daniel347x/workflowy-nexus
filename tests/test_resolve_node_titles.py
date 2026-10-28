"""resolve_node_titles wire path -- _handle_resolve_node_titles + dispatch.

Covers the Slice groups drawer's root-title lookup:
  * a known node's title has its trailing tags (and trident) split off, with
    request_id and the result action echoed;
  * a Dock-tagged node reports is_dock: true;
  * an unknown uuid reports exists: false;
  * a failed Dock-vocabulary read degrades is_dock to null, titles still succeed;
  * an invalid frame replies success: false with request_id;
  * a frame without request_id is dropped;
  * the action is a WS work action and the dispatch clause task-dispatches it.

Model: local, no network, no real Workflowy.
"""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

from coherence_harness import make_boundary_capable

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from workflowy_mcp import server as srv  # noqa: E402

MAP_UUID = "11111111-1111-4111-8111-111111111111"
DOCK_UUID = "22222222-2222-4222-8222-222222222222"
MISSING_UUID = "33333333-3333-4333-8333-333333333333"

_NODES = [
    {
        "id": MAP_UUID,
        "name": "🗺️ Repo map (Cartographer) 🔱 #nexus--src--suppress #wfy-sg-001--expand",
    },
    {"id": DOCK_UUID, "name": "📁 Working notes #wfy-ns--local"},
]


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


def _wire_env(monkeypatch, *, config_raises=None):
    """Boundary-capable client with an export_nodes double + stubbed Dock config."""
    client = make_boundary_capable(SimpleNamespace(), nodes=_NODES)

    async def _export_nodes(node_id=None, use_cache=True):
        return {"nodes": [dict(n) for n in _NODES]}

    client.export_nodes = _export_nodes
    monkeypatch.setattr(srv, "get_client", lambda: client)

    calls = {}

    async def _fake_config(target_tab):
        calls["target_tab"] = target_tab
        if config_raises is not None:
            raise config_raises
        return ["wfy-ns"], [], "", {}

    monkeypatch.setattr(srv, "_resolve_note_session_config", _fake_config)
    return calls


def _run(frame):
    ws = _FakeWebSocket()
    asyncio.run(srv._handle_resolve_node_titles(frame, ws))
    return ws.sent


def test_known_node_title_strips_tags_and_echoes_request_id(monkeypatch):
    calls = _wire_env(monkeypatch)
    sent = _run(
        {
            "action": "resolve_node_titles",
            "request_id": "r-1",
            "endpoint_id": "ep-3",
            "uuids": [MAP_UUID],
        }
    )
    assert len(sent) == 1
    reply = sent[0]
    assert reply["action"] == "resolve_node_titles_result"
    assert reply["request_id"] == "r-1"
    assert reply["success"] is True
    assert "truncated" not in reply
    entry = reply["titles"][MAP_UUID]
    assert entry["title"] == "🗺️ Repo map (Cartographer)"
    assert entry["name"] == _NODES[0]["name"]
    assert entry["exists"] is True
    assert entry["is_dock"] is False
    assert calls["target_tab"] == "ep-3"


def test_dock_node_reports_is_dock(monkeypatch):
    _wire_env(monkeypatch)
    sent = _run({"request_id": "r-2", "uuids": [DOCK_UUID]})
    entry = sent[0]["titles"][DOCK_UUID]
    assert entry["is_dock"] is True
    assert entry["title"] == "📁 Working notes"
    assert entry["exists"] is True


def test_unknown_uuid_reports_missing(monkeypatch):
    _wire_env(monkeypatch)
    sent = _run({"request_id": "r-3", "uuids": [MISSING_UUID, MAP_UUID]})
    assert sent[0]["success"] is True
    assert sent[0]["titles"][MISSING_UUID] == {
        "title": None,
        "name": None,
        "exists": False,
        "is_dock": False,
    }
    assert sent[0]["titles"][MAP_UUID]["exists"] is True


def test_config_failure_degrades_is_dock_to_null(monkeypatch):
    _wire_env(monkeypatch, config_raises=RuntimeError("no tab"))
    sent = _run({"request_id": "r-4", "uuids": [MAP_UUID, DOCK_UUID]})
    reply = sent[0]
    assert reply["success"] is True
    assert reply["titles"][DOCK_UUID]["title"] == "📁 Working notes"
    assert reply["titles"][DOCK_UUID]["is_dock"] is None
    assert reply["titles"][MAP_UUID]["is_dock"] is None


def test_invalid_frames_reply_error_with_request_id(monkeypatch):
    _wire_env(monkeypatch)
    for frame in (
        {"request_id": "r-5"},
        {"request_id": "r-5", "uuids": []},
        {"request_id": "r-5", "uuids": MAP_UUID},
        {"request_id": "r-5", "uuids": [MAP_UUID, 7]},
    ):
        sent = _run(frame)
        assert len(sent) == 1, frame
        assert sent[0]["action"] == "resolve_node_titles_result"
        assert sent[0]["success"] is False
        assert sent[0]["request_id"] == "r-5"
        assert sent[0]["error"]


def test_cap_truncates_and_flags(monkeypatch):
    _wire_env(monkeypatch)
    uuids = [MAP_UUID] + [f"00000000-0000-4000-8000-{i:012d}" for i in range(600)]
    sent = _run({"request_id": "r-6", "uuids": uuids})
    assert sent[0]["success"] is True
    assert sent[0]["truncated"] is True
    assert len(sent[0]["titles"]) == srv._RESOLVE_NODE_TITLES_MAX_UUIDS


def test_frame_without_request_id_is_dropped(monkeypatch):
    _wire_env(monkeypatch)
    assert _run({"uuids": [MAP_UUID]}) == []


# ---------------------------------------------------------------------------
# Dispatch: a WS work action, task-dispatched by websocket_handler.
# ---------------------------------------------------------------------------


def test_resolve_node_titles_is_a_ws_work_action():
    assert "resolve_node_titles" in srv._WS_WORK_ACTIONS


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


def test_websocket_handler_task_dispatches_resolve_node_titles(monkeypatch):
    import workflowy_mcp.coherence.operation_cache as opc

    sentinel = {}

    async def _sentinel_handler(data, websocket):
        # Reached only if the dispatch clause calls the handler with the frame.
        sentinel["action"] = data.get("action")

    monkeypatch.setattr(srv, "_handle_resolve_node_titles", _sentinel_handler)

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
                    "action": "resolve_node_titles",
                    "request_id": "r-dispatch",
                    "uuids": [MAP_UUID],
                }
            )
        ]
    )
    asyncio.run(srv.websocket_handler(ws))
    assert captured.get("name") == "_sentinel_handler"
    assert (captured.get("data") or {}).get("action") == "resolve_node_titles"
    assert sentinel == {}  # the stub spawner never awaited the handler
