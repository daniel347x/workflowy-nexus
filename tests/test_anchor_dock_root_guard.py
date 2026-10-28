"""Regression locks for the Anchor-Dock root guard (2026).

An in-band full-dump return (``node.scry`` or a bare-root ``glimpse``
``root_handle`` raw dump) whose resolved root IS an Anchor Dock is refused
with an ``anchor_dock_root_detected`` teaching error -- the 2MB-class
context blowup ``anchor.dock.peek`` / ``anchor.dock.render`` exist to
prevent. A root ABSENT from the local cache is likewise refused
(``root_not_in_cache``): the guard FAILS CLOSED rather than allowing an
unclassifiable root through to the API subtree fetch. Covers: global +
local Docks, AP-ID targeting, the ``allow_anchor_dock=True`` override
(which bypasses even the cache-miss), the out-of-band exemptions
(``output_file``, ``push_to_modal``), the unguarded slice-override cell,
the loud config-failure posture, the one-cache-map / one-config-read
snapshot welding, and the ``anchor.dock.render`` never-self-reject
invariant.
"""

from __future__ import annotations

import asyncio

import pytest

from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import dispatch_operation


DOCK_UUID = "11111111-aaaa-4000-8000-000000000001"
LOCAL_DOCK_UUID = "11111111-bbbb-4000-8000-000000000002"
AP_DOCK_UUID = "11111111-cccc-4000-8000-000000000003"
PLAIN_UUID = "11111111-dddd-4000-8000-000000000004"
MISSING_UUID = "99999999-0000-4000-8000-000000000005"

FAKE_CACHE = {
    DOCK_UUID: {"id": DOCK_UUID, "name": "📜 Test Dock #wfy-ns"},
    LOCAL_DOCK_UUID: {"id": LOCAL_DOCK_UUID, "name": "🏠 Local Dock #wfy-ns--local"},
    AP_DOCK_UUID: {"id": AP_DOCK_UUID, "name": "[ap:K7M2QF] AP Dock #wfy-ns"},
    PLAIN_UUID: {"id": PLAIN_UUID, "name": "Just a plain node"},
}


class _FakeClient:
    def __init__(self):
        self.scry_calls = []

    async def workflowy_scry(
        self, node_id, depth=None, size_limit=1000, output_file=None
    ):
        self.scry_calls.append(
            {
                "node_id": node_id,
                "depth": depth,
                "size_limit": size_limit,
                "output_file": output_file,
            }
        )
        return {"success": True, "node_count": 1}


@pytest.fixture
def guard_env(monkeypatch):
    """Fake resolution + cache + live Dock-tag config around the guard."""

    calls = {"config": 0}

    async def fake_resolve_with_cache(handle, *, label, required=True):
        candidate = (handle or "").strip()
        if candidate in FAKE_CACHE:
            return candidate, FAKE_CACHE
        if candidate == "[ap:K7M2QF]":
            return AP_DOCK_UUID, FAKE_CACHE
        matches = [u for u in FAKE_CACHE if u.startswith(candidate)]
        if len(matches) == 1:
            return matches[0], FAKE_CACHE
        # required=False passthrough (what the guard requests): an
        # unresolvable handle comes back unchanged so the caller's own
        # miss policy applies.
        return candidate, FAKE_CACHE

    async def fake_resolve(handle, *, label, required=True):
        resolved, _nodes = await fake_resolve_with_cache(
            handle, label=label, required=required
        )
        return resolved

    async def fake_config(target_tab=None):
        calls["config"] += 1
        return (
            ["wfy-ns"],
            [],
            None,
            {"note_session_local_postfixes": ["local"]},
        )

    monkeypatch.setattr(
        server, "_resolve_generic_node_handle_with_cache", fake_resolve_with_cache
    )
    monkeypatch.setattr(server, "_resolve_generic_node_handle", fake_resolve)
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: FAKE_CACHE)
    monkeypatch.setattr(server, "_resolve_note_session_config", fake_config)
    monkeypatch.setattr(server, "get_client", lambda: _FakeClient())
    return calls


# ---------------------------------------------------------------------------
# node.scry (impl layer raises the typed guard error)
# ---------------------------------------------------------------------------


def test_scry_refuses_global_anchor_dock_root(guard_env, monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(server, "get_client", lambda: client)
    with pytest.raises(server._AnchorDockRootDetected) as excinfo:
        asyncio.run(server._node_scry_impl(DOCK_UUID))
    exc = excinfo.value
    assert exc.code == "anchor_dock_root_detected"
    assert "anchor.dock.peek" in exc.message
    assert "anchor.dock.render" in exc.message
    assert "allow_anchor_dock=true" in exc.message
    assert exc.extra["dock_uuid"] == DOCK_UUID
    assert exc.extra["matched_tag"] == "#wfy-ns"
    assert client.scry_calls == []  # the 2MB fetch never ran


def test_scry_refuses_local_anchor_dock_root(guard_env):
    with pytest.raises(server._AnchorDockRootDetected) as excinfo:
        asyncio.run(server._node_scry_impl(LOCAL_DOCK_UUID))
    assert excinfo.value.extra["matched_tag"] == "#wfy-ns--local"


def test_scry_refuses_dock_targeted_by_ap_id(guard_env):
    with pytest.raises(server._AnchorDockRootDetected) as excinfo:
        asyncio.run(server._node_scry_impl("[ap:K7M2QF]"))
    assert excinfo.value.extra["dock_uuid"] == AP_DOCK_UUID


def test_scry_override_allows_dock(guard_env, monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(server, "get_client", lambda: client)
    result = asyncio.run(
        server._node_scry_impl(DOCK_UUID, allow_anchor_dock=True)
    )
    assert result == {"success": True, "node_count": 1}
    assert client.scry_calls[0]["node_id"] == DOCK_UUID


def test_scry_output_file_is_exempt(guard_env, monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(server, "get_client", lambda: client)
    result = asyncio.run(
        server._node_scry_impl(DOCK_UUID, output_file="E:/temp/scry.json")
    )
    assert result["success"] is True
    assert guard_env["config"] == 0  # guard never ran for out-of-band writes


def test_scry_plain_node_guard_passes_and_spent_one_config_read(guard_env, monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(server, "get_client", lambda: client)
    result = asyncio.run(server._node_scry_impl(PLAIN_UUID))
    assert result["success"] is True
    assert guard_env["config"] == 1  # guard ran; verdict was not-an-anchor


def test_scry_cache_missing_root_fails_closed_without_config(guard_env, monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(server, "get_client", lambda: client)
    with pytest.raises(server._FullDumpRootCacheMiss) as excinfo:
        asyncio.run(server._node_scry_impl(MISSING_UUID))
    exc = excinfo.value
    assert exc.code == "root_not_in_cache"
    assert "cache.refresh" in exc.message
    assert guard_env["config"] == 0  # no config read spent on invisible nodes
    assert client.scry_calls == []  # the API subtree fetch never ran


def test_scry_override_bypasses_even_cache_miss(guard_env, monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(server, "get_client", lambda: client)
    result = asyncio.run(
        server._node_scry_impl(MISSING_UUID, allow_anchor_dock=True)
    )
    assert result["success"] is True
    assert guard_env["config"] == 0  # override skips the guard wholesale


def test_guard_classifies_against_resolver_cache_map_not_second_load(
    guard_env, monkeypatch
):
    # The guard must classify against the SAME map the resolver echoed (one
    # welded snapshot) -- a second _anchor_cache_by_id load would be both
    # wasteful and a snapshot-drift crack.
    monkeypatch.setattr(
        server,
        "_anchor_cache_by_id",
        lambda: (_ for _ in ()).throw(AssertionError("second cache load")),
    )
    with pytest.raises(server._AnchorDockRootDetected):
        asyncio.run(server._node_scry_impl(DOCK_UUID))


def test_scry_config_failure_is_loud_not_skipped(guard_env, monkeypatch):
    async def boom(target_tab=None):
        raise RuntimeError("no live tab reachable")

    monkeypatch.setattr(server, "_resolve_note_session_config", boom)
    with pytest.raises(RuntimeError, match="no live tab reachable"):
        asyncio.run(server._node_scry_impl(DOCK_UUID))


# ---------------------------------------------------------------------------
# node.scry (registry envelope: declared domain error code)
# ---------------------------------------------------------------------------


def test_node_registry_scry_guard_becomes_declared_envelope(guard_env):
    registry = server.build_node_registry()
    result = asyncio.run(
        dispatch_operation(registry, "scry", {"root_handle": DOCK_UUID})
    )
    assert result["success"] is False
    assert result["error"]["code"] == "anchor_dock_root_detected"
    assert "anchor.dock.peek" in result["error"]["message"]
    assert result["error"]["extra"]["override_param"] == "allow_anchor_dock"


def test_node_registry_scry_cache_miss_becomes_declared_envelope(guard_env):
    registry = server.build_node_registry()
    result = asyncio.run(
        dispatch_operation(registry, "scry", {"root_handle": MISSING_UUID})
    )
    assert result["success"] is False
    assert result["error"]["code"] == "root_not_in_cache"
    assert "cache.refresh" in result["error"]["message"]


def test_node_registry_scry_accepts_allow_anchor_dock_param(guard_env, monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(server, "get_client", lambda: client)
    registry = server.build_node_registry()
    result = asyncio.run(
        dispatch_operation(
            registry,
            "scry",
            {"root_handle": DOCK_UUID, "allow_anchor_dock": True},
        )
    )
    assert result == {"success": True, "node_count": 1}


# ---------------------------------------------------------------------------
# glimpse root_handle raw dump (guard payload on the one guarded cell)
# ---------------------------------------------------------------------------


def test_glimpse_bare_root_raw_dump_refuses_dock(guard_env, monkeypatch):
    called = []

    async def fake_root(**kwargs):
        called.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(server, "_glimpse_root_uuid_from_cache", fake_root)
    result = asyncio.run(server._glimpse_run_impl(root_uuid=DOCK_UUID))
    assert result["success"] is False
    assert result["code"] == "anchor_dock_root_detected"
    assert "anchor.dock.peek" in result["error"]
    assert result["detail"]["dock_uuid"] == DOCK_UUID
    assert called == []  # the projection never ran


def test_glimpse_override_allows_dock_raw_dump(guard_env, monkeypatch):
    called = []

    async def fake_root(**kwargs):
        called.append(kwargs)
        return {"success": True, "preview_markdown": "# dock"}

    monkeypatch.setattr(server, "_glimpse_root_uuid_from_cache", fake_root)
    asyncio.run(
        server._glimpse_run_impl(root_uuid=DOCK_UUID, allow_anchor_dock=True)
    )
    assert len(called) == 1
    assert called[0]["root_uuid"] == DOCK_UUID


def test_glimpse_output_file_raw_dump_is_exempt(guard_env, monkeypatch):
    called = []

    async def fake_root(**kwargs):
        called.append(kwargs)
        return {"success": True, "mode": "file"}

    monkeypatch.setattr(server, "_glimpse_root_uuid_from_cache", fake_root)
    asyncio.run(
        server._glimpse_run_impl(root_uuid=DOCK_UUID, output_file="E:/temp/g.md")
    )
    assert len(called) == 1
    assert guard_env["config"] == 0


def test_glimpse_push_to_modal_is_exempt(guard_env, monkeypatch):
    pushed = []

    async def fake_push(**kwargs):
        pushed.append(kwargs)
        return {"success": True, "pushed": True}

    monkeypatch.setattr(server, "_glimpse_push_slice_to_modal", fake_push)
    asyncio.run(server._glimpse_run_impl(root_uuid=DOCK_UUID, push_to_modal=True))
    assert len(pushed) == 1
    assert guard_env["config"] == 0  # zero-token push never guarded


def test_glimpse_slice_plus_dock_override_root_is_not_guarded(guard_env, monkeypatch):
    sliced = []

    async def fake_slice(**kwargs):
        sliced.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(server, "_glimpse_slice_group_from_cache", fake_slice)
    asyncio.run(server._glimpse_run_impl(slice_group="grp-x", root_uuid=DOCK_UUID))
    assert len(sliced) == 1  # a curated lens at an override root is no raw dump
    assert guard_env["config"] == 0


def test_glimpse_bare_root_cache_miss_fails_closed(guard_env, monkeypatch):
    called = []

    async def fake_root(**kwargs):
        called.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(server, "_glimpse_root_uuid_from_cache", fake_root)
    result = asyncio.run(server._glimpse_run_impl(root_uuid=MISSING_UUID))
    assert result["success"] is False
    assert result["code"] == "root_not_in_cache"
    assert "cache.refresh" in result["error"]
    assert called == []  # the projection never ran
    assert guard_env["config"] == 0  # no config read spent on invisible nodes


def test_glimpse_boundary_reuses_guard_vocabulary_read(guard_env, monkeypatch):
    called = []

    async def fake_root(**kwargs):
        called.append(kwargs)
        return {"success": True, "preview_markdown": "# plain"}

    monkeypatch.setattr(server, "_glimpse_root_uuid_from_cache", fake_root)
    asyncio.run(
        server._glimpse_run_impl(
            root_uuid=PLAIN_UUID, descend_policy="stop_at_child_anchors"
        )
    )
    assert len(called) == 1
    assert called[0]["boundary_predicate"] is not None
    assert guard_env["config"] == 1  # ONE shared read: guard + boundary welded


def test_glimpse_registry_run_guard_envelope(guard_env):
    registry = server.build_glimpse_registry()
    result = asyncio.run(
        dispatch_operation(registry, "run", {"root_handle": DOCK_UUID})
    )
    assert result["success"] is False
    assert result["error"]["code"] == "glimpse_request_failed"
    assert "anchor.dock.peek" in result["error"]["message"]
    legacy = result["error"]["extra"]["legacy_result"]
    assert legacy["code"] == "anchor_dock_root_detected"


# ---------------------------------------------------------------------------
# Never-self-reject invariant: anchor.dock.render reaches the engine directly
# ---------------------------------------------------------------------------


def test_anchor_dock_render_still_reaches_engine_directly(guard_env, monkeypatch):
    seen = []

    async def fake_root(**kwargs):
        seen.append(kwargs)
        return {"success": True, "preview_markdown": "# Dock"}

    monkeypatch.setattr(server, "_glimpse_root_uuid_from_cache", fake_root)
    result = asyncio.run(server._anchor_dock_render_impl(DOCK_UUID))
    assert result["success"] is True
    assert len(seen) == 1
