"""Unit 4d(a) ([ap:TVIIBN]) — the ONE recency rule for the GLIMPSE render verbs.

The rule (DECISION [ap:RFBCYW], Dan's ruling [ap:C609U1] §A.1):

* ``'last N'`` / ``'newest N'`` (and every old blob that decodes positional)
  -- ``'last N'`` is a SYNONYM of ``'newest N'``: both SELECT the N newest
  leaves by an explicit recency basis (``recent_by``, defaulting to the
  presentation ``sort_by`` axis when it is a timestamp, else ``'modified'``),
  whatever the ``sort_direction``; the direction only ORDERS what is shown.
* ``'bottom N'`` is the POSITIONAL tail of the current order -- exactly the
  legacy item-count behavior, and the meaning of every old wire blob with no
  ``selection`` field (wire back-compat).
* ``sort_direction`` without a timestamp ``sort_by`` is a LOUD error
  (``sort_direction_without_sort_by``) at the public MCP render-verb boundary
  only; internal specs and stored blobs keep accepting it.

The trap table from the to-do (each row = one test below): on the OLD code,
``recent='last 3'`` under ``sort_by='created', sort_direction='desc'``
returned the 3 OLDEST items, and ``newest 3`` returned them too. Every such
test here is written so it would FAIL against the pre-fix code (the
would-it-fail check is noted per test in the RESULT).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from workflowy_mcp import server  # noqa: E402
from workflowy_mcp.glimpse_since_policy import (  # noqa: E402
    SinceCodec,
    SinceSpec,
    apply_since_to_forest,
    reorder_tree_by_timestamp,
    classify_since_token,
    is_item_count_token,
    parse_since_item_count,
    prune_tree_to_newest_leaf_count,
    resolve_item_count_basis,
    since_from_edge,
    since_from_wire,
    since_to_wire,
)
from workflowy_mcp.server import prepare_stage2_render_forest  # noqa: E402


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def _leaf(uid: str) -> dict:
    return {"id": uid, "name": uid, "note": "", "children": []}


def _doc_tree() -> dict:
    """root with five leaves L1..L5 in document order."""
    return {
        "id": "root",
        "name": "root",
        "note": "",
        "children": [_leaf(f"L{i}") for i in range(1, 6)],
    }


def _cache() -> dict:
    """Timestamps deliberately scrambled so created-order, modified-order and
    document order are three DIFFERENT orders:
      document order : L1 L2 L3 L4 L5
      created order  : L1(100) L2(200) L3(300) L4(400) L5(500)  == document
      modified order : L1(900) L3(800) L5(700) L4(300) L2(200)
    """
    created = {"L1": 100, "L2": 200, "L3": 300, "L4": 400, "L5": 500}
    modified = {"L1": 900, "L2": 200, "L3": 800, "L4": 300, "L5": 700}
    cache: dict[str, dict] = {"root": {"id": "root", "createdAt": 50, "modifiedAt": 50}}
    for uid in created:
        cache[uid] = {
            "id": uid,
            "createdAt": created[uid],
            "modifiedAt": modified[uid],
        }
    return cache


def _kept_leaves(forest) -> list[str]:
    def walk(node, acc):
        kids = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        if not kids:
            acc.append(node["id"])
        for k in kids:
            walk(k, acc)

    acc: list[str] = []
    for top in forest or []:
        walk(top, acc)
    return acc


# --------------------------------------------------------------------------
# 1. token grammar (SinceCodec / parse / classify)
# --------------------------------------------------------------------------
def test_last_and_newest_tokens_select_the_newest():
    # WOULD FAIL PRE-FIX? -- the token OBJECT is new (selection field); the
    # pre-fix codec produced the same form/count but no selection. The
    # selection value is what the new rule is: pinned here.
    for token in ("last 3", "newest 3", "LAST 3 ITEMS"):
        spec, err = SinceCodec.from_token_result(token)
        assert err is None
        assert spec.form == "item_count" and spec.count == 3
        assert spec.selection == "newest"


def test_bottom_token_selects_the_positional_tail():
    spec, err = SinceCodec.from_token_result("bottom 4")
    assert err is None
    assert spec.form == "item_count" and spec.count == 4
    assert spec.selection == "bottom"
    assert is_item_count_token("bottom 4") and parse_since_item_count("bottom 4") == 4


def test_bottom_malformed_tokens_are_loud_item_count_errors():
    for token in ("bottom", "bottom banana", "bottom 0", "BOTTOM 12"):
        assert is_item_count_token(token), token
        if token == "BOTTOM 12":
            spec, err = SinceCodec.from_token_result(token)
            assert err is None and spec.count == 12 and spec.selection == "bottom"
            continue
        assert parse_since_item_count(token) is None, token
        _spec, err = SinceCodec.from_token_result(token)
        assert err and "bottom N" in err, (token, err)


def test_bottom_prefix_without_a_space_is_not_an_item_count_token():
    assert not is_item_count_token("bottomline 3")
    assert not is_item_count_token("7")


def test_to_token_spelling_newest_is_last_and_bottom_is_bottom():
    # 'last N' stays the canonical display spelling for the newest selection
    # (it is now TRUTHFUL: last N means the newest N); the positional tail
    # spells 'bottom N' so the extension's positional ribbon round-trips.
    assert SinceCodec.to_token(SinceSpec(form="item_count", count=5, selection="newest")) == "last 5"
    assert SinceCodec.to_token(SinceSpec(form="item_count", count=5, selection="bottom")) == "bottom 5"
    # A field-less construction keeps the LEGACY positional meaning end to end.
    assert SinceCodec.to_token(SinceSpec(form="item_count", count=5)) == "bottom 5"


def test_axis_direction_suffix_stays_inert_for_item_count():
    # The @axis/direction suffix never changes WHAT an item-count selects.
    spec, err = SinceCodec.from_token_result("last 2@modified desc")
    assert err is None and spec.form == "item_count" and spec.count == 2
    assert spec.selection == "newest"
    assert spec.axis == "created" and spec.direction == "asc"


def test_classify_since_token_routes_bottom_as_item_count():
    cls = classify_since_token("bottom 3")
    assert cls["form"] == "item_count" and cls["error"] is None
    assert cls["requires_axis"] is False and cls["requires_slice_group"] is False


# --------------------------------------------------------------------------
# 2. wire back-compat (the auditor's explicit check)
# --------------------------------------------------------------------------
def test_wire_write_emits_selection_explicitly():
    wire = since_to_wire(SinceSpec(form="item_count", count=2, selection="newest"))
    assert wire == {
        "form": "item_count",
        "axis": "created",
        "direction": "asc",
        "count": 2,
        "selection": "newest",
    }
    wire_b = since_to_wire(SinceSpec(form="item_count", count=2, selection="bottom"))
    assert wire_b["selection"] == "bottom"
    # Non-item_count forms carry no selection key (unchanged shape).
    assert "selection" not in since_to_wire(SinceSpec(form="window", window_seconds=60))


def test_wire_read_old_blob_without_selection_stays_positional():
    # THE wire-compat mandate: an old item_count blob with NO selection field
    # keeps its POSITIONAL meaning. WOULD FAIL PRE-FIX (pre-fix code had no
    # selection at all); the pre-fix EQUIVALENT of this behavior is the
    # prune_tree positional tail asserted in the next section.
    spec = since_from_wire({"form": "item_count", "count": 3, "axis": "created", "direction": "asc"})
    assert spec is not None and spec.selection == "bottom"


def test_wire_read_unknown_selection_degrades_to_positional():
    spec = since_from_wire({"form": "item_count", "count": 3, "selection": "banana"})
    assert spec is not None and spec.selection == "bottom"


def test_wire_roundtrip_preserves_both_selections():
    for selection in ("newest", "bottom"):
        spec = SinceSpec(form="item_count", count=4, selection=selection)
        assert since_from_wire(since_to_wire(spec)) == spec


# --------------------------------------------------------------------------
# 3. the recency basis rule (ONE resolution, shared)
# --------------------------------------------------------------------------
def test_resolve_item_count_basis_rule():
    assert resolve_item_count_basis("created", "modified") == "created"
    assert resolve_item_count_basis(None, "modified") == "modified"
    assert resolve_item_count_basis(None, None) == "modified"
    assert resolve_item_count_basis(None, "none") == "modified"
    assert resolve_item_count_basis("banana", "created") == "created"
    assert resolve_item_count_basis(None, "created") == "created"


# --------------------------------------------------------------------------
# 4. the selection prunes (pure policy, then through the ONE Stage-2 point)
# --------------------------------------------------------------------------
def test_apply_since_newest_selection_is_direction_independent():
    # THE TRAP ROW: 'last 3' under created + desc returned the 3 OLDEST.
    # WOULD FAIL PRE-FIX: pre-fix code positional-tailed the desc-sorted order.
    tree = _doc_tree()
    for direction in ("asc", "desc"):
        forest, echo, err = apply_since_to_forest(
            [tree],
            since_spec=since_from_edge("last 3"),
            sort_by="created",
            direction=direction,
            cache_by_id=_cache(),
        )
        assert err is None
        assert _kept_leaves(forest) == ["L3", "L4", "L5"]
        assert echo == {"form": "item_count", "count": 3, "selection": "newest", "basis": "created"}


def test_apply_since_newest_default_basis_is_sort_axis_else_modified():
    tree = _doc_tree()
    # No sort -> default basis 'modified' (NOT the document-order tail the old
    # code produced). WOULD FAIL PRE-FIX (pre-fix kept L4,L5).
    forest, echo, err = apply_since_to_forest(
        [tree],
        since_spec=since_from_edge("newest 2"),
        sort_by=None,
        direction="asc",
        cache_by_id=_cache(),
    )
    assert err is None
    assert _kept_leaves(forest) == ["L1", "L3"]
    assert echo["basis"] == "modified" and echo["selection"] == "newest"
    # An explicit recent_by wins over the sort axis.
    forest2, echo2, _ = apply_since_to_forest(
        [tree],
        since_spec=since_from_edge("newest 2"),
        sort_by="created",
        direction="asc",
        cache_by_id=_cache(),
        recent_by="modified",
    )
    assert _kept_leaves(forest2) == ["L1", "L3"]
    assert echo2["basis"] == "modified"


def test_apply_since_bottom_keeps_positional_semantics_everywhere():
    # PRODUCTION ORDER: the caller sorts FIRST (Stage 2 runs sort, then the
    # since prune on the sorted forest), so the positional tail is the tail of
    # the SORTED order -- this test mirrors that sequencing exactly.
    # 'bottom N' with NO sort = the document-order tail (the legacy behavior).
    tree = _doc_tree()
    forest, echo, err = apply_since_to_forest(
        [tree],
        since_spec=since_from_edge("bottom 2"),
        sort_by=None,
        direction="asc",
        cache_by_id=_cache(),
    )
    assert err is None
    assert _kept_leaves(forest) == ["L4", "L5"]
    assert echo == {"form": "item_count", "count": 2, "selection": "bottom"}
    # 'bottom N' under created+desc = the tail of the DESC order = the oldest.
    # This is EXACTLY what old 'last N under desc' did (now spelled bottom).
    desc_sorted = reorder_tree_by_timestamp(
        [tree], sort_by="created", direction="desc", cache_by_id=_cache()
    )
    forest2, echo2, _ = apply_since_to_forest(
        desc_sorted,
        since_spec=since_from_edge("bottom 2"),
        sort_by="created",
        direction="desc",
        cache_by_id=_cache(),
    )
    # The KEPT SET is {L1, L2} (the tail of the desc order = the two oldest);
    # they display in the desc order the sort produced (L2 before L1) -- the
    # selection never reorders, the direction does.
    assert _kept_leaves(forest2) == ["L2", "L1"]
    assert echo2["selection"] == "bottom"


def test_prune_newest_counts_leaf_branches_and_rebuilds_spines():
    # A subtree with K leaves consumes K of the count; connective ancestors
    # survive; the survivors keep the forest's CURRENT (document) order --
    # the selection never reorders.
    tree = {
        "id": "root",
        "name": "root",
        "note": "",
        "children": [
            _leaf("A"),
            {
                "id": "B",
                "name": "B",
                "note": "",
                "children": [_leaf("B1"), _leaf("B2")],
            },
            _leaf("C"),
        ],
    }
    cache = {
        "root": {"id": "root", "createdAt": 1, "modifiedAt": 1},
        "A": {"id": "A", "createdAt": 100, "modifiedAt": 100},
        "B": {"id": "B", "createdAt": 2, "modifiedAt": 500},
        "B1": {"id": "B1", "createdAt": 300, "modifiedAt": 300},
        "B2": {"id": "B2", "createdAt": 400, "modifiedAt": 400},
        "C": {"id": "C", "createdAt": 200, "modifiedAt": 200},
    }
    # newest 2 by created = B2(400) and B1(300); B is connective; order stays.
    forest = prune_tree_to_newest_leaf_count([tree], count=2, basis="created", cache_by_id=cache)
    assert _kept_leaves(forest) == ["B1", "B2"]
    assert forest[0]["children"][0]["id"] == "B"  # the spine survived


def test_prune_newest_undated_leaves_rank_last():
    # A leaf with no resolvable timestamp is selected only when N exceeds the
    # dated count (dock.peek's missing-timestamps-sink-last rule).
    tree = _doc_tree()
    cache = _cache()
    del cache["L5"]  # L5 becomes undated
    forest = prune_tree_to_newest_leaf_count([tree], count=4, basis="created", cache_by_id=cache)
    assert _kept_leaves(forest) == ["L1", "L2", "L3", "L4"]
    # N exceeds the DATED count (4 dated) -> the undated leaf is admitted.
    forest2 = prune_tree_to_newest_leaf_count([tree], count=5, basis="created", cache_by_id=cache)
    assert _kept_leaves(forest2) == ["L1", "L2", "L3", "L4", "L5"]


def test_both_selections_noop_when_n_ge_leaves():
    tree = _doc_tree()
    for spec in (
        SinceSpec(form="item_count", count=9, selection="newest"),
        SinceSpec(form="item_count", count=9, selection="bottom"),
    ):
        forest, echo, err = apply_since_to_forest(
            [tree], since_spec=spec, sort_by=None, direction="asc", cache_by_id=_cache()
        )
        assert err is None and forest == [tree]
        assert echo["form"] == "item_count" and echo["count"] == 9


# --------------------------------------------------------------------------
# 5. through the ONE Stage-2 point (prepare_stage2_render_forest)
# --------------------------------------------------------------------------
@pytest.fixture()
def _capture_apply(monkeypatch):
    from types import SimpleNamespace

    seen: dict = {}

    # The sort path acquires the client for reorder; a client double keeps the
    # fixture pure (the reorder itself is covered by the broader suites).
    monkeypatch.setattr(
        server,
        "get_client",
        lambda: SimpleNamespace(_reorder_tree_by_timestamp=lambda *a, **k: None),
    )

    def fake_apply(nodes, *, since_spec, sort_by, direction, cache_by_id, curated_pivot_ts, recent_by=None):
        seen["recent_by"] = recent_by
        seen["sort_by"] = sort_by
        return nodes, None, None

    monkeypatch.setattr(server, "_apply_since_to_forest", fake_apply)
    return seen


def test_stage2_prep_threads_recent_by_to_the_one_apply(_capture_apply):
    tree = _doc_tree()
    prepare_stage2_render_forest(
        final_subtree=tree,
        cache_by_id=_cache(),
        sort_by="created",
        sort_direction="desc",
        since_spec=since_from_edge("last 2"),
        recent_by="modified",
    )
    assert _capture_apply["recent_by"] == "modified"

    prepare_stage2_render_forest(
        final_subtree=tree,
        cache_by_id=_cache(),
        sort_by=None,
        sort_direction="asc",
        since_spec=since_from_edge("last 2"),
    )
    assert _capture_apply["recent_by"] is None


# --------------------------------------------------------------------------
# 6. the loud public sort_direction rule (one guard, every render verb)
# --------------------------------------------------------------------------
def test_glimpse_run_handler_refuses_direction_without_sort_by(monkeypatch):
    """WOULD FAIL PRE-FIX: the pre-fix handler silently accepted (and the
    render silently ignored) a direction with no sort_by."""
    async def impl(**kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("the guard must fire before the impl")

    monkeypatch.setattr(server, "_glimpse_run_impl", impl)
    res = asyncio.run(
        server.GLIMPSE_REGISTRY.resolve("run").handler(
            {"root_handle": "R", "sort_direction": "desc"}
        )
    )
    assert res["success"] is False
    assert res["error"]["code"] == "sort_direction_without_sort_by"
    assert "sort_by" in res["error"]["message"]


def test_glimpse_run_dispatch_envelope_and_accept_paths(monkeypatch):
    from types import SimpleNamespace

    from coherence_harness import make_boundary_capable

    monkeypatch.setattr(
        server,
        "get_client",
        lambda: make_boundary_capable(SimpleNamespace(), nodes=[]),
    )
    async def impl(**kwargs):
        return "RAW MARKDOWN"

    monkeypatch.setattr(server, "_glimpse_run_impl", impl)
    from workflowy_mcp.mcp_api_registry import dispatch_operation

    # Dispatch-level: the refusal rides the family error envelope with the
    # DECLARED code (a code the spec does not declare would fail-closed).
    res = asyncio.run(
        dispatch_operation(
            server.GLIMPSE_REGISTRY,
            "run",
            {"sort_direction": "desc"},
        )
    )
    assert res["error"]["code"] == "sort_direction_without_sort_by"
    # sort_by='none' is explicitly NOT a timestamp -> still loud.
    res2 = asyncio.run(
        dispatch_operation(
            server.GLIMPSE_REGISTRY,
            "run",
            {"sort_direction": "desc", "sort_by": "none"},
        )
    )
    assert res2["error"]["code"] == "sort_direction_without_sort_by"
    # The accept path: a timestamp sort_by passes the guard.
    assert (
        asyncio.run(
            dispatch_operation(
                server.GLIMPSE_REGISTRY,
                "run",
                {"sort_direction": "desc", "sort_by": "created"},
            )
        )
        == "RAW MARKDOWN"
    )
    # A direction with NO direction given at all is untouched.
    assert (
        asyncio.run(dispatch_operation(server.GLIMPSE_REGISTRY, "run", {}))
        == "RAW MARKDOWN"
    )


def test_dock_render_handler_refuses_direction_without_sort_by(monkeypatch):
    async def impl(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("the guard must fire before the impl")

    monkeypatch.setattr(server, "_anchor_dock_render_impl", impl)
    registry = server.build_anchor_registry()
    res = asyncio.run(
        registry.resolve("dock.render").handler(
            {"dock_handle": "D", "sort_direction": "desc"}
        )
    )
    assert res["success"] is False
    assert res["error"]["code"] == "sort_direction_without_sort_by"


def test_slice_group_render_handler_refuses_direction_without_sort_by(monkeypatch):
    monkeypatch.setattr(
        server,
        "_lightning_rod_render_lens",
        _never_called,
    )
    registry = server.build_slice_group_registry()
    res = asyncio.run(
        registry.resolve("render").handler(
            {"lens": "Architecture", "sort_direction": "desc"}
        )
    )
    assert res["success"] is False
    assert res["error"]["code"] == "sort_direction_without_sort_by"


def test_lightning_render_tags_handler_refuses_direction_without_sort_by(monkeypatch):
    async def impl(**kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("the guard must fire before the impl")

    monkeypatch.setattr(server, "_lightning_rod_legacy_impl", impl)
    registry = server.build_lightning_rod_registry()
    res = asyncio.run(
        registry.resolve("render_tags").handler(
            {"root_handle": "R", "tags": ["t"], "sort_direction": "desc"}
        )
    )
    assert res["success"] is False
    assert res["error"]["code"] == "sort_direction_without_sort_by"


async def _never_called(*args, **kwargs):  # pragma: no cover - shared guard
    raise AssertionError("the guard must fire before the impl")


def test_recent_by_param_is_accepted_by_the_registry_schemas():
    """The recency basis is a first-class param on every render verb that takes
    `recent` (validate_params is the closed-schema gate)."""
    from workflowy_mcp.mcp_api_registry import validate_params

    for registry, verb, params in (
        (server.GLIMPSE_REGISTRY, "run", {"root_handle": "R", "recent": "last 2", "recent_by": "created"}),
        (server.build_anchor_registry(), "dock.render", {"dock_handle": "D", "recent": "last 2", "recent_by": "modified"}),
        (server.build_slice_group_registry(), "render", {"lens": "Architecture", "recent": "last 2", "recent_by": "created"}),
        (server.build_slice_group_registry(), "sift", {"lens": "Architecture", "query": "x", "recent": "last 2", "recent_by": "modified"}),
        (server.build_lightning_rod_registry(), "render_tags", {"root_handle": "R", "tags": ["t"], "recent": "last 2", "recent_by": "created"}),
    ):
        validate_params(registry.resolve(verb), params)  # must not raise


def test_recent_by_rides_the_spec_and_the_wire_blob():
    spec = server.RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="R",
        sort_by="created",
        since=since_from_edge("last 2"),
        recent_by="modified",
    )
    assert spec.recent_by == "modified"
    blob = spec.to_render_spec_blob()
    assert blob["recent_by"] == "modified"
    rt = server.RenderProjectionSpec.from_render_spec_blob(blob)
    assert rt.recent_by == "modified"
    # An old blob without the key -> None (resolve at apply).
    blob.pop("recent_by")
    assert server.RenderProjectionSpec.from_render_spec_blob(blob).recent_by is None
    # A garbage value degrades to None (lenient like the other knobs).
    assert (
        server.RenderProjectionSpec.normalize_from_glimpse_args(
            root_uuid="R", recent_by="banana"
        ).recent_by
        is None
    )
    # The render_scope echo carries the basis beside `since`.
    fields = spec.to_render_scope_fields()
    assert fields["recent_by"] == "modified"


def test_render_scope_echo_none_basis_stays_none():
    spec = server.RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="R")
    assert spec.to_render_scope_fields()["recent_by"] is None


def test_spec_recency_selection_round_trips_through_the_view_overrides():
    """A 'newest' selection survives the ONE override seam (the modal path)."""
    spec = server.RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="R", since=since_from_edge("last 3")
    )
    merged = spec.with_view_overrides(sort_by="created", sort_direction="desc")
    assert merged.since is not None and merged.since.selection == "newest"
    # The basis is carried through unchanged (a tri-state override never
    # silently re-decides it).
    assert merged.recent_by is None
    merged2 = spec.with_view_overrides(recent_by="created")
    assert merged2.recent_by == "created"


def test_render_is_chronological_unchanged_for_both_selections():
    from workflowy_mcp.glimpse_since_policy import render_is_chronological

    for selection in ("newest", "bottom"):
        spec = SinceSpec(form="item_count", count=2, selection=selection)
        assert render_is_chronological(None, spec) is True
    assert render_is_chronological(None, None) is False
