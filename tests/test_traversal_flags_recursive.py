"""[ap:EPR86W] part a — the traversal pair `recursive` + `descend_policy`.

Unit 4a (group G7) of the MCP API Unification program: `dock.render` gains
`recursive` + `descend_policy` with exactly `dock.peek`'s names and values
(defaults differ by design: recursive=True, stop_at_child_anchors), the ONE
shared cache-render walker gains the depth gate (`max_depth`; the spec's
`recursive=False` maps to 1), the RenderProjectionSpec carries the new
TRAVERSAL field with blob + echo round-trip, and the raw-root surfaces
(dock.render, glimpse.run root_handle, the estate {dock} form, the
push/modal path that re-projects their spec) honour it. A membership
selection (slice / slice_override / lightning_rod) refuses recursive=False
LOUDLY at the ONE normalizer instead of silently ignoring it.

Every test here FAILS against the pre-fix code: the walker had no max_depth,
the spec had no recursive field, the impls had no recursive kwarg, and the
dock form refused the key (would-it-fail check applied per §5.5a of the role
manual).

All local: no network, no real Workflowy, no disk writes.
"""

import asyncio
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pytest  # noqa: E402

from workflowy_mcp.f8_slice_policy import (  # noqa: E402
    build_anchor_boundary_predicate,
    build_selected_subtree_from_cache,
    build_slice_subtree_from_cache,
)
from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402


# ---------------------------------------------------------------------------
# Cache fixtures (the same flat {id: node} shape the walker reads).
# ---------------------------------------------------------------------------

def _node(nid, name, parent_id, priority=0, note=None, completed=False):
    return {
        "id": nid,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
        "completed": completed,
    }


def _flat_cache(nodes):
    return {n["id"]: n for n in nodes}


def _ids(subtree):
    out = []

    def walk(n):
        out.append(n["id"])
        for c in n.get("children") or []:
            walk(c)

    walk(subtree)
    return out


def _deep_cache():
    # root -> a -> a1 -> a2 ; root -> b (leaf)
    return _flat_cache([
        _node("root", "R", None, note="rn"),
        _node("a", "A", "root", note="an"),
        _node("a1", "A1", "a", note="a1n"),
        _node("a2", "A2", "a1", note="a2n"),
        _node("b", "B", "root", priority=1, note="bn"),
    ])


# ---------------------------------------------------------------------------
# 1 · the ONE walker: max_depth (recursive=False maps to 1).
# ---------------------------------------------------------------------------

def test_walker_default_is_unbounded_byte_parity():
    cache = _deep_cache()
    full = build_selected_subtree_from_cache(cache, "root", lambda n: True)
    assert _ids(full) == ["root", "a", "a1", "a2", "b"]
    # The explicit default is the same walk.
    explicit = build_selected_subtree_from_cache(
        cache, "root", lambda n: True, max_depth=None
    )
    assert _ids(explicit) == _ids(full)
    assert full == explicit


def test_walker_depth_one_is_root_plus_direct_children():
    cache = _deep_cache()
    capped = build_selected_subtree_from_cache(
        cache, "root", lambda n: True, max_depth=1
    )
    # Root + direct children (a, b); a's descendants are gone; the capped
    # child is childless in the OUTPUT (the materialize gate), not merely
    # unvisited.
    assert _ids(capped) == ["root", "a", "b"]
    for child in capped["children"]:
        assert child["children"] == []


def test_walker_depth_one_treats_child_docks_and_plain_nodes_alike():
    # The dock.render use case: children of ANY kind render one level, with
    # their notes; nothing deeper. A "child Dock" is just a deeper node here —
    # the cap does not care what the child is.
    cache = _flat_cache([
        _node("dock", "D", None, note="dock note"),
        _node("dockchild", "DC", "dock", note="child note"),
        _node("grandchild", "GC", "dockchild", note="must not render"),
        _node("plain", "P", "dock", priority=1, note="plain note"),
    ])
    capped = build_selected_subtree_from_cache(
        cache, "dock", lambda n: True, max_depth=1
    )
    assert _ids(capped) == ["dock", "dockchild", "plain"]
    names = {c["name"]: c["note"] for c in capped["children"]}
    assert names == {"DC": "child note", "P": "plain note"}


def test_walker_depth_cap_and_boundary_compose_boundary_moot():
    cache = _deep_cache()
    boundary = build_anchor_boundary_predicate(["wfy-ns"])
    cache["b"]["name"] = "B #wfy-ns"  # b is a child anchor boundary
    capped = build_selected_subtree_from_cache(
        cache, "root", lambda n: True,
        boundary_predicate=boundary, max_depth=1,
    )
    # Under the cap the boundary is moot: the depth gate stops the descent
    # first; the output is identical to the cap alone.
    assert _ids(capped) == ["root", "a", "b"]


def test_walker_depth_cap_over_a_boundary_root_still_renders_children():
    # A boundary ROOT is exempt (the boundary is tested on children only), and
    # so is the depth gate: a capped render of a boundary root still shows its
    # direct children.
    cache = _deep_cache()
    cache["root"]["name"] = "R #wfy-ns"
    boundary = build_anchor_boundary_predicate(["wfy-ns"])
    capped = build_selected_subtree_from_cache(
        cache, "root", lambda n: True,
        boundary_predicate=boundary, max_depth=1,
    )
    assert _ids(capped) == ["root", "a", "b"]


def test_walker_depth_cap_never_reaches_completed_test_under_cap():
    cache = _deep_cache()
    cache["a1"]["completed"] = True
    capped = build_selected_subtree_from_cache(
        cache, "root", lambda n: True, max_depth=1
    )
    # a1 is beyond the cap anyway; the completed policy still governs the
    # UNCAPPED walk identically.
    assert _ids(capped) == ["root", "a", "b"]
    full = build_selected_subtree_from_cache(
        cache, "root", lambda n: True, exclude_completed=True
    )
    assert "a1" not in _ids(full) and "a2" not in _ids(full)


def test_wrapper_threads_max_depth():
    cache = _deep_cache()
    raw = build_slice_subtree_from_cache(
        cache, "root", [], member_all=True, max_depth=1
    )
    assert _ids(raw) == ["root", "a", "b"]
    unbounded = build_slice_subtree_from_cache(cache, "root", [], member_all=True)
    assert _ids(unbounded) == ["root", "a", "a1", "a2", "b"]


# ---------------------------------------------------------------------------
# 2 · the spec: field, normalizer, blob, echo, overrides, the loud refusal.
# ---------------------------------------------------------------------------

def test_spec_default_recursive_true_and_explicit_false():
    s = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="r")
    assert s.recursive is True
    s2 = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="r", recursive=False
    )
    assert s2.recursive is False


def test_spec_blob_round_trip_absent_key_is_true():
    s = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="r")
    blob = s.to_render_spec_blob()
    assert blob["recursive"] is True
    assert RenderProjectionSpec.from_render_spec_blob(blob).recursive is True
    # An OLD blob without the key deserializes to True (frozen modals
    # re-project safely).
    assert RenderProjectionSpec.from_render_spec_blob(
        {"root_uuid": "r"}
    ).recursive is True


def test_spec_blob_round_trip_false():
    s = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="r", recursive=False
    )
    assert s.to_render_spec_blob()["recursive"] is False
    assert (
        RenderProjectionSpec.from_render_spec_blob(s.to_render_spec_blob()).recursive
        is False
    )


def test_spec_scope_echo_carries_recursive():
    s = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="r", recursive=False
    )
    assert s.to_render_scope_fields()["recursive"] is False
    s2 = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="r")
    assert s2.to_render_scope_fields()["recursive"] is True


def test_spec_with_view_overrides_carries_recursive():
    s = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="r", recursive=False
    )
    s2 = s.with_view_overrides(headings_only=True, sort_by="none")
    assert s2.recursive is False and s2.headings_only is True


@pytest.mark.parametrize("kwargs", [
    {"group_id": "G"},
    {"group_id": "G", "root_uuid": "r"},
    {"lightning": {"mode": "gather"}},
])
def test_spec_refuses_recursive_false_on_membership_kinds(kwargs):
    with pytest.raises(ValueError, match="RAW-ROOT traversal policy"):
        RenderProjectionSpec.normalize_from_glimpse_args(
            recursive=False, **kwargs
        )


def test_spec_blob_path_refuses_recursive_false_on_slice():
    with pytest.raises(ValueError, match="RAW-ROOT traversal policy"):
        RenderProjectionSpec.from_render_spec_blob(
            {"group_id": "G", "recursive": False}
        )


# ---------------------------------------------------------------------------
# 3 · the render_scope echo (the scalar composer + the spec bridge).
# ---------------------------------------------------------------------------

def test_compose_render_scope_echoes_recursive_default_true():
    from workflowy_mcp import server as srv
    scope = srv._compose_render_scope(
        group_id=None, group_name=None, root_uuid="r",
        is_raw_dump=True, root_overridden=False,
        sort_by=None, sort_direction="asc", since=None,
        headings_only=False,
    )
    assert scope["recursive"] is True
    scope2 = srv._compose_render_scope(
        group_id=None, group_name=None, root_uuid="r",
        is_raw_dump=True, root_overridden=False,
        sort_by=None, sort_direction="asc", since=None,
        headings_only=False, recursive=False,
    )
    assert scope2["recursive"] is False


def test_compose_render_scope_from_spec_echoes_spec_recursive():
    from workflowy_mcp import server as srv
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="r", recursive=False
    )
    scope = srv._compose_render_scope_from_spec(spec)
    assert scope["recursive"] is False


# ---------------------------------------------------------------------------
# 4 · glimpse.run: guards + raw-root threading.
# ---------------------------------------------------------------------------

def test_run_impl_refuses_recursive_false_with_slice_group():
    from workflowy_mcp import server as srv
    result = asyncio.run(srv._glimpse_run_impl(slice_group="Arch", recursive=False))
    assert result["success"] is False
    assert "RAW-ROOT traversal policy" in result["error"]


def test_run_impl_refuses_recursive_false_on_live_dom():
    from workflowy_mcp import server as srv
    result = asyncio.run(srv._glimpse_run_impl(recursive=False))
    assert result["success"] is False
    assert "server-side cache" in result["error"]


def test_run_impl_threads_recursive_true_default_and_false_to_raw_root(monkeypatch):
    from workflowy_mcp import server as srv
    seen = []

    async def raw(**kwargs):
        seen.append(kwargs)
        return {"success": True, "preview_markdown": "# R"}

    monkeypatch.setattr(srv, "_glimpse_root_uuid_from_cache", raw)
    # allow_anchor_dock skips the in-band Anchor-Dock root guard (a live
    # client read) -- irrelevant to the depth leg under test.
    asyncio.run(srv._glimpse_run_impl(root_uuid="r", allow_anchor_dock=True))
    assert seen[-1]["recursive"] is True
    asyncio.run(srv._glimpse_run_impl(
        root_uuid="r", recursive=False, allow_anchor_dock=True
    ))
    assert seen[-1]["recursive"] is False


# ---------------------------------------------------------------------------
# 5 · dock.render: impl threading + descend_policy validation + push.
# ---------------------------------------------------------------------------

def _dock_render_env(monkeypatch):
    from workflowy_mcp import server as srv
    config = (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})
    monkeypatch.setattr(
        srv, "_resolve_note_session_config", lambda _tab: _async_value(config)
    )
    monkeypatch.setattr(
        srv, "_resolve_generic_node_handle",
        lambda value, **_kw: _async_value("D"),
    )
    monkeypatch.setattr(
        srv, "_anchor_cache_by_id",
        lambda: {"D": {"id": "D", "name": "Dock #wfy-ns"}},
    )


def _async_value(value):
    async def _v(*_a, **_kw):
        return value
    return _v()


def test_dock_render_impl_threads_traversal_pair_to_raw_root(monkeypatch):
    from workflowy_mcp import server as srv
    _dock_render_env(monkeypatch)
    seen = []

    async def raw(**kwargs):
        seen.append(kwargs)
        return {"success": True, "preview_markdown": "# D"}

    monkeypatch.setattr(srv, "_glimpse_root_uuid_from_cache", raw)
    # Defaults = today's behavior, unchanged: the child-Dock boundary is
    # BUILT (descend_policy defaults stop_at_child_anchors) and the depth leg
    # is unbounded.
    asyncio.run(srv._anchor_dock_render_impl("D"))
    assert seen[-1]["recursive"] is True
    assert callable(seen[-1]["boundary_predicate"])
    # descend_policy='all' must not silently keep the boundary: no predicate,
    # so the walk descends into child Docks.
    asyncio.run(
        srv._anchor_dock_render_impl("D", recursive=False, descend_policy="all")
    )
    assert seen[-1]["recursive"] is False
    assert seen[-1]["boundary_predicate"] is None
    # recursive=False with the default descend_policy still passes the
    # boundary (moot under the cap; the walker gate decides).
    asyncio.run(srv._anchor_dock_render_impl("D", recursive=False))
    assert seen[-1]["recursive"] is False
    assert callable(seen[-1]["boundary_predicate"])


def test_dock_render_impl_refuses_unknown_descend_policy(monkeypatch):
    from workflowy_mcp import server as srv
    _dock_render_env(monkeypatch)
    from workflowy_mcp.server import _AnchorOperationError
    with pytest.raises(_AnchorOperationError, match="descend_policy"):
        asyncio.run(
            srv._anchor_dock_render_impl("D", descend_policy="sometimes")
        )


def test_dock_render_impl_threads_recursive_to_push(monkeypatch):
    from workflowy_mcp import server as srv
    _dock_render_env(monkeypatch)
    seen = []

    async def push(**kwargs):
        seen.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(srv, "_glimpse_push_slice_to_modal", push)
    asyncio.run(srv._anchor_dock_render_impl("D", push_to_modal=True))
    assert seen[-1]["recursive"] is True
    assert seen[-1]["descend_policy"] == "stop_at_child_anchors"
    asyncio.run(
        srv._anchor_dock_render_impl(
            "D", push_to_modal=True, recursive=False, descend_policy="all"
        )
    )
    assert seen[-1]["recursive"] is False
    assert seen[-1]["descend_policy"] == "all"


# ---------------------------------------------------------------------------
# 6 · the registry surface (schemas + dispatch routing).
# ---------------------------------------------------------------------------

def test_dock_render_schema_exposes_traversal_pair(monkeypatch):
    from workflowy_mcp import server as srv
    from workflowy_mcp.mcp_api_registry import dispatch_operation, validate_params
    registry = srv.build_anchor_registry()
    spec = registry.resolve("dock.render")
    props = spec.params_schema["properties"]
    assert "recursive" in props and props["recursive"]["type"] == "boolean"
    assert "descend_policy" in props
    assert list(props["descend_policy"]["enum"]) == ["all", "stop_at_child_anchors"]
    validate_params(spec, {"dock_handle": "D", "recursive": False})
    validate_params(
        spec, {"dock_handle": "D", "recursive": True, "descend_policy": "all"}
    )
    with pytest.raises(Exception):
        validate_params(spec, {"dock_handle": "D", "descend_policy": "sometimes"})

    seen = []

    async def render_impl(handle, **kwargs):
        seen.append((handle, kwargs))
        return {"success": True, "preview_markdown": "# D"}

    monkeypatch.setattr(srv, "_anchor_dock_render_impl", render_impl)
    asyncio.run(dispatch_operation(registry, "dock.render", {"dock_handle": "D"}))
    assert seen[-1][1]["recursive"] is True
    assert seen[-1][1]["descend_policy"] == "stop_at_child_anchors"
    asyncio.run(dispatch_operation(
        registry, "dock.render",
        {"dock_handle": "D", "recursive": False, "descend_policy": "all"},
    ))
    assert seen[-1][1]["recursive"] is False
    assert seen[-1][1]["descend_policy"] == "all"


def test_glimpse_run_schema_exposes_recursive_and_routes_it(monkeypatch):
    from workflowy_mcp import server as srv
    from workflowy_mcp.mcp_api_registry import dispatch_operation, validate_params
    registry = srv.build_glimpse_registry()
    spec = registry.resolve("run")
    props = spec.params_schema["properties"]
    assert "recursive" in props and props["recursive"]["type"] == "boolean"
    validate_params(spec, {"root_handle": "r", "recursive": False})
    # [ap:EPR86W] 4b: the pair now RIDES the shared selection fragment, so
    # the Lightning Rod walking verbs carry it too (their boundary refuses
    # recursive=False loudly; see test_traversal_flags_walking_verbs.py).
    lr = srv.build_lightning_rod_registry()
    lr_spec = lr.resolve("render_tags")
    assert "recursive" in lr_spec.params_schema["properties"]

    seen = []

    async def run_impl(**kwargs):
        seen.append(kwargs)
        return {"success": True, "preview_markdown": "# R"}

    monkeypatch.setattr(srv, "_glimpse_run_impl", run_impl)
    asyncio.run(dispatch_operation(
        registry, "run", {"root_handle": "r", "recursive": False}
    ))
    assert seen[-1]["recursive"] is False
    asyncio.run(dispatch_operation(registry, "run", {"root_handle": "r"}))
    assert seen[-1]["recursive"] is True


# ---------------------------------------------------------------------------
# 7 · the estate {dock} form lowers recursive onto the spec.
# ---------------------------------------------------------------------------

def test_simple_form_dock_recursive_false_lowers_into_spec():
    from workflowy_mcp.simple_form import normalize_simple_form
    form = normalize_simple_form(
        {"dock": "[ap:D]", "recursive": False}, surface="estate_call"
    )
    assert form.ok and form.form == "dock"
    # Estate surfaces bake the per-kind headings-only default (3d) beside the
    # traversal fields.
    assert form.spec == {
        "root_uuid": "[ap:D]",
        "descend_policy": "stop_at_child_anchors",
        "recursive": False,
        "headings_only": True,
    }
    # The glimpse_run surface bakes no headings default.
    form2 = normalize_simple_form(
        {"dock": "[ap:D]", "recursive": False}, surface="glimpse_run"
    )
    assert form2.ok and form2.spec["recursive"] is False
    assert "headings_only" not in form2.spec


def test_simple_form_dock_without_recursive_is_byte_identical():
    from workflowy_mcp.simple_form import normalize_simple_form
    form = normalize_simple_form({"dock": "[ap:D]"}, surface="estate_call")
    assert form.ok
    assert form.spec == {
        "root_uuid": "[ap:D]",
        "descend_policy": "stop_at_child_anchors",
        "headings_only": True,
    }
    assert "recursive" not in form.spec


def test_simple_form_dock_recursive_true_is_inert_and_valid():
    from workflowy_mcp.simple_form import normalize_simple_form
    form = normalize_simple_form(
        {"dock": "[ap:D]", "recursive": True}, surface="estate_note"
    )
    assert form.ok
    assert "recursive" not in form.spec


def test_simple_form_dock_recursive_non_bool_refused():
    from workflowy_mcp.simple_form import normalize_simple_form
    form = normalize_simple_form(
        {"dock": "[ap:D]", "recursive": "nope"}, surface="estate_call"
    )
    assert not form.ok and "recursive" in (form.error_message or "")


def test_simple_form_dock_recursive_refused_on_slice_group_surface():
    # The lens subtree atom has no recursion knob: the slice_group surface
    # refuses the key via the usual unknown-key error instead of dropping it.
    from workflowy_mcp.simple_form import normalize_simple_form
    form = normalize_simple_form(
        {"dock": "[ap:D]", "recursive": False}, surface="slice_group"
    )
    assert not form.ok and "recursive" in (form.error_message or "")


def test_estate_dock_blob_round_trips_recursive():
    from workflowy_mcp.simple_form import normalize_simple_form
    lowered = normalize_simple_form(
        {"dock": "[ap:D]", "recursive": False}, surface="estate_call"
    ).spec
    spec = RenderProjectionSpec.from_render_spec_blob(dict(lowered))
    assert spec.recursive is False and spec.kind == "raw_root"


# ---------------------------------------------------------------------------
# 8 · the modal handler's raw-dump cell honours the spec's depth cap.
# ---------------------------------------------------------------------------

_MODAL_ROOT = "11111111-aaaa-bbbb-cccc-000000000001"
_MODAL_A = "11111111-aaaa-bbbb-cccc-000000000002"
_MODAL_A1 = "11111111-aaaa-bbbb-cccc-000000000004"
_MODAL_B = "11111111-aaaa-bbbb-cccc-000000000003"

_MODAL_NODES = [
    {"id": _MODAL_ROOT, "name": "Anchor Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _MODAL_A, "name": "First point", "note": "body A", "parent_id": _MODAL_ROOT, "priority": 0},
    {"id": _MODAL_A1, "name": "Nested point", "note": "body A1", "parent_id": _MODAL_A, "priority": 0},
    {"id": _MODAL_B, "name": "Second point", "note": "body B", "parent_id": _MODAL_ROOT, "priority": 1},
]


class _FakeCaptureWebsocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        try:
            self.sent.append(json.loads(payload))
        except Exception:
            self.sent.append({"_raw": payload})


def _run_modal_handler(render_spec_blob, monkeypatch):
    from workflowy_mcp import server as srv
    from coherence_harness import make_boundary_capable
    from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus

    def _make_client():
        c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
        c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _MODAL_NODES]
        return make_boundary_capable(c, nodes=_MODAL_NODES)

    fake_ws = _FakeCaptureWebsocket()
    monkeypatch.setattr(srv, "get_client", _make_client)
    frame = {
        "action": "render_slice_modal",
        "request_id": "rid-recursive",
        "render_spec": render_spec_blob,
    }
    asyncio.run(srv._handle_render_slice_modal(frame, fake_ws))
    assert fake_ws.sent, "handler sent no reply"
    return fake_ws.sent[-1]


def test_modal_raw_dump_recursive_false_renders_one_level(monkeypatch):
    reply = _run_modal_handler(
        {"kind": "raw_root", "root_uuid": _MODAL_ROOT, "recursive": False},
        monkeypatch,
    )
    assert reply.get("success") is True
    markdown = reply.get("markdown") or ""
    assert "First point" in markdown and "Second point" in markdown
    # The depth-capped render never reaches the grandchild.
    assert "Nested point" not in markdown
    # The echo carries the cap so a Refresh re-projects the SAME shape.
    assert reply["render_scope"]["recursive"] is False


def test_modal_raw_dump_default_renders_everything(monkeypatch):
    reply = _run_modal_handler(
        {"kind": "raw_root", "root_uuid": _MODAL_ROOT}, monkeypatch
    )
    assert reply.get("success") is True
    markdown = reply.get("markdown") or ""
    assert "Nested point" in markdown
    assert reply["render_scope"]["recursive"] is True
