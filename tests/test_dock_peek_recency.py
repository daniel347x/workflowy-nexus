"""Unit 1 (SkyView): dock.peek most-recent-N / timestamp ordering [ap:SKWSXO],
full names with trailing #tags [ap:WG3QTR], and the `tail` -> `recent`
recency wording with silent aliases [ap:COORGU].

Every test here is mutation-sensitive against the pre-Unit-1 code (dcc6768):
the recency knobs did not exist (schema `additionalProperties: False` rejects
them), rows stored the tag-stripped title as `name`, the compact default
budget was 256, and the public property was `tail` with `since` as its only
alias.

U3 ([ap:SSVIC9] / [ap:BSDRR0]) moved the direction pins: ``recent=N`` no
longer implies ``sort_direction='desc'`` -- ascending is the default
EVERYWHERE and descending is an explicit opt-in; ``recent=N`` still SELECTS
the N newest. The order assertions below lock the new behaviour and are
mutation-sensitive against the pre-U3 code (d6f5b38), where a bare ``recent``
flipped the presentation newest-first.
"""
from __future__ import annotations

import asyncio

import pytest
from workflowy_mcp import server
from workflowy_mcp import dock_peek_metrics as _dpm
from workflowy_mcp.glimpse_since_policy import (
    SinceCodec,
    is_item_count_token,
    parse_since_item_count,
)
from workflowy_mcp.mcp_api_registry import dispatch_operation, validate_params


# Resolved lazily so that, against pre-Unit-1 code, each test fails on its own
# merit (AttributeError / assertion) instead of the whole module erroring at
# import -- that is what makes the stash-verified mutation check per-test.
def apply_dock_peek_recency(*args, **kwargs):
    return _dpm.apply_dock_peek_recency(*args, **kwargs)


def describe_dock_peek_selection(*args, **kwargs):
    return _dpm.describe_dock_peek_selection(*args, **kwargs)


def node_timestamp(*args, **kwargs):
    return _dpm.node_timestamp(*args, **kwargs)


def _async_value(value):
    async def _value(*_args, **_kwargs):
        return value

    return _value()


# ---------------------------------------------------------------------------
# Fixture: a Dock with five direct children (document order A..E), one nested
# local Dock (D) with a child behind it, and one grandchild under B. Created
# and modified timestamps are deliberately NOT in document order and NOT in
# the same order as each other, so a selection on the wrong basis is visible.
# ---------------------------------------------------------------------------

def _cache():
    return {
        "D0": {"id": "D0", "name": "TODO Dock #wfy-ns", "note": "The list."},
        "A": {"id": "A", "name": "[ap:AAAAAA] Alpha #topic-x #due", "parent_id": "D0", "priority": 1,
              "createdAt": 100, "modifiedAt": 500},
        "B": {"id": "B", "name": "Bravo", "parent_id": "D0", "priority": 2,
              "createdAt": 400, "modifiedAt": 450},
        "C": {"id": "C", "name": "Charlie #later", "parent_id": "D0", "priority": 3,
              "createdAt": 300, "modifiedAt": 900},
        "D": {"id": "D", "name": "Delta Dock #wfy-ns--local", "parent_id": "D0", "priority": 4,
              "createdAt": 200, "modifiedAt": 200},
        "E": {"id": "E", "name": "Echo (no timestamps)", "parent_id": "D0", "priority": 5},
        "BB": {"id": "BB", "name": "Bravo child", "parent_id": "B", "priority": 1,
               "createdAt": 950, "modifiedAt": 960},
        "DD": {"id": "DD", "name": "Behind Delta", "parent_id": "D", "priority": 1,
               "createdAt": 990, "modifiedAt": 995},
    }


def _setup(monkeypatch, cache=None):
    config = (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})
    monkeypatch.setattr(server, "_resolve_note_session_config", lambda _tab: _async_value(config))
    monkeypatch.setattr(server, "_resolve_generic_node_handle", lambda value, **_kw: _async_value(value))
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: cache if cache is not None else _cache())


def _peek(**params):
    return asyncio.run(server._anchor_dock_peek_impl("D0", **params))


def _dispatch(**params):
    return asyncio.run(
        dispatch_operation(server.build_anchor_registry(), "dock.peek", {"dock_handle": "D0", **params})
    )


def _body(text: str) -> list[str]:
    return text.split("\n\n", 1)[1].splitlines()


# ---------------------------------------------------------------------------
# Pure policy
# ---------------------------------------------------------------------------

def _rows():
    return [
        {"uuid": "a", "created_at": 100, "modified_at": 500, "depth": 0},
        {"uuid": "b", "created_at": 400, "modified_at": 450, "depth": 0},
        {"uuid": "c", "created_at": 300, "modified_at": 900, "depth": 1},
        {"uuid": "d", "created_at": 200, "modified_at": 200, "depth": 0},
        {"uuid": "e", "created_at": None, "modified_at": None, "depth": 0},
    ]


def test_node_timestamp_reads_own_int_seconds_and_aliases_only():
    assert node_timestamp({"createdAt": 5, "modifiedAt": 9}, "created") == 5
    assert node_timestamp({"createdAt": 5, "modifiedAt": 9}, "modified") == 9
    assert node_timestamp({"created": 7.0, "modified": "9"}, "created") == 7
    assert node_timestamp({"created": 7.0, "modified": "9"}, "modified") is None
    assert node_timestamp({"modifiedAt": True}, "modified") is None
    assert node_timestamp({}, "created") is None
    with pytest.raises(ValueError):
        node_timestamp({}, "touched")


def test_neither_knob_is_identity_in_document_order():
    rows, selection, sort = apply_dock_peek_recency(
        _rows(), recent=None, recent_by=None, sort_by=None, sort_direction=None
    )
    assert [r["uuid"] for r in rows] == ["a", "b", "c", "d", "e"]
    assert selection == {"recent": None, "recent_by": None, "candidates": 5, "selected": 5, "completed_excluded": 0}
    assert sort == {"by": "none", "direction": "asc"}
    assert describe_dock_peek_selection(selection, sort, recursive=False) == ""


def test_recent_defaults_to_modified_selects_newest_presents_ascending_and_sinks_missing():
    rows, selection, sort = apply_dock_peek_recency(
        _rows(), recent=2, recent_by=None, sort_by=None, sort_direction=None
    )
    # U3 (SSVIC9): the SELECTION is still the N newest by modified (c, a);
    # the PRESENTATION is ascending -- a(500) before c(900).
    assert [r["uuid"] for r in rows] == ["a", "c"]
    assert selection == {"recent": 2, "recent_by": "modified", "candidates": 5, "selected": 2, "completed_excluded": 0}
    assert sort == {"by": "modified", "direction": "asc"}
    # The newest N by MODIFIED is not the newest N by CREATED.
    by_created, sel2, sort2 = apply_dock_peek_recency(
        _rows(), recent=2, recent_by="created", sort_by=None, sort_direction=None
    )
    # Selected {b(400), c(300)} on created; presented ascending: c then b.
    assert [r["uuid"] for r in by_created] == ["c", "b"]
    assert sel2["recent_by"] == "created"
    assert sort2 == {"by": "created", "direction": "asc"}
    # N larger than the pool: ascending by modified, the unknown one last.
    everything, _, _ = apply_dock_peek_recency(
        _rows(), recent=10, recent_by=None, sort_by=None, sort_direction=None
    )
    assert [r["uuid"] for r in everything] == ["d", "b", "a", "c", "e"]
    # Descending stays available as an explicit opt-in, never implied.
    newest_first, _, sort3 = apply_dock_peek_recency(
        _rows(), recent=2, recent_by=None, sort_by=None, sort_direction="desc"
    )
    assert [r["uuid"] for r in newest_first] == ["c", "a"]
    assert sort3 == {"by": "modified", "direction": "desc"}


def test_recent_sort_override_keeps_selection_but_changes_presentation():
    doc_order, _, sort = apply_dock_peek_recency(
        _rows(), recent=2, recent_by="modified", sort_by="none", sort_direction=None
    )
    assert [r["uuid"] for r in doc_order] == ["a", "c"]
    assert sort == {"by": "none", "direction": "asc"}
    oldest_first, _, sort2 = apply_dock_peek_recency(
        _rows(), recent=3, recent_by="modified", sort_by=None, sort_direction="asc"
    )
    assert [r["uuid"] for r in oldest_first] == ["b", "a", "c"]
    assert sort2 == {"by": "modified", "direction": "asc"}
    cross_axis, _, sort3 = apply_dock_peek_recency(
        _rows(), recent=3, recent_by="modified", sort_by="created", sort_direction=None
    )
    # Selected on modified (a, b, c) but displayed oldest-created first
    # (U3: the cross-axis default direction is asc too).
    assert [r["uuid"] for r in cross_axis] == ["a", "c", "b"]
    assert sort3 == {"by": "created", "direction": "asc"}


def test_whole_inventory_sort_without_recent_defaults_ascending():
    rows, selection, sort = apply_dock_peek_recency(
        _rows(), recent=None, recent_by=None, sort_by="created", sort_direction=None
    )
    assert [r["uuid"] for r in rows] == ["a", "d", "c", "b", "e"]
    assert selection["recent"] is None and selection["selected"] == 5
    assert sort == {"by": "created", "direction": "asc"}
    desc, _, sort2 = apply_dock_peek_recency(
        _rows(), recent=None, recent_by=None, sort_by="modified", sort_direction="desc"
    )
    assert [r["uuid"] for r in desc] == ["c", "a", "b", "d", "e"]
    assert sort2["direction"] == "desc"


def test_ties_keep_document_order_and_inputs_are_not_mutated():
    source = [
        {"uuid": "x", "created_at": 1, "modified_at": 7},
        {"uuid": "y", "created_at": 1, "modified_at": 7},
        {"uuid": "z", "created_at": 1, "modified_at": 8},
    ]
    import copy
    before = copy.deepcopy(source)
    rows, _, _ = apply_dock_peek_recency(
        source, recent=3, recent_by="modified", sort_by=None, sort_direction=None
    )
    # U3: ascending presentation; equal timestamps keep document order.
    assert [r["uuid"] for r in rows] == ["x", "y", "z"]
    assert source == before
    rows[0]["uuid"] = "mutated"
    assert source[0]["uuid"] == "x"


def test_selection_phrase_names_basis_direction_and_pool():
    sel = {"recent": 2, "recent_by": "modified", "candidates": 5, "selected": 2}
    assert describe_dock_peek_selection(sel, {"by": "modified", "direction": "desc"}, recursive=False) == (
        "2 most recent by modified of 5 children (newest first)"
    )
    assert describe_dock_peek_selection(sel, {"by": "none", "direction": "asc"}, recursive=True) == (
        "2 most recent by modified of 5 descendants, in document order"
    )
    assert describe_dock_peek_selection(sel, {"by": "created", "direction": "asc"}, recursive=False) == (
        "2 most recent by modified of 5 children, sorted by created (oldest first)"
    )
    plain = {"recent": None, "recent_by": None, "candidates": 5, "selected": 5}
    assert describe_dock_peek_selection(plain, {"by": "created", "direction": "asc"}, recursive=False) == (
        "sorted by created (oldest first)"
    )


# ---------------------------------------------------------------------------
# The impl: compact and full select the same items; recursive composition
# ---------------------------------------------------------------------------

def test_direct_children_recent_by_modified_full_json(monkeypatch):
    _setup(monkeypatch)
    result = _peek(detail="full", recent=2)
    # U3: selection unchanged (the two newest by modified: C, A);
    # presentation ascending: A(500) then C(900).
    assert [row["uuid"] for row in result["rows"]] == ["A", "C"]
    assert result["count"] == 2
    assert result["selection"] == {"recent": 2, "recent_by": "modified", "candidates": 5, "selected": 2, "completed_excluded": 0}
    assert result["sort"] == {"by": "modified", "direction": "asc"}
    charlie = result["rows"][1]
    assert charlie["created_at"] == 300 and charlie["modified_at"] == 900
    assert charlie["name"] == "Charlie #later"
    assert charlie["title"] == "Charlie" and charlie["tags"] == ["#later"]
    # root_metrics are unaffected by the selection: they cover the whole subtree.
    assert result["root_metrics"]["child_count"] == 5
    assert result["root_metrics"]["descendant_count"] == 7


def test_direct_children_recent_by_created_is_a_different_set(monkeypatch):
    _setup(monkeypatch)
    result = _peek(detail="full", recent=2, recent_by="created")
    # Selected {B(400), C(300)} on created; U3 presents ascending: C then B.
    assert [row["uuid"] for row in result["rows"]] == ["C", "B"]
    assert result["selection"]["recent_by"] == "created"


def test_compact_and_full_select_identical_items_and_header_names_selection(monkeypatch):
    _setup(monkeypatch)
    full = _peek(detail="full", recent=3)
    compact = _peek(detail="compact", recent=3, show_counts=False)
    header = compact.splitlines()[0]
    assert header.startswith("TODO Dock #wfy-ns — 3 most recent by modified of 5 children (oldest first) · ")
    body = _body(compact)
    assert len(body) == 3 == full["count"]
    assert [row["uuid"] for row in full["rows"]] == ["B", "A", "C"]
    # Same items, ascending order, full names with their tags.
    assert body[0].endswith("Bravo")
    assert body[1] == "[ap:AAAAAA] Alpha #topic-x #due"
    assert body[2].endswith("Charlie #later")
    assert "Delta" not in compact and "Echo" not in compact


def test_recursive_recent_selects_from_all_walked_rows_flat_with_depth_as_data(monkeypatch):
    _setup(monkeypatch)
    # stop_at_child_anchors: BB (depth 1, modified 960) is walked; DD is behind
    # the local Dock D and is NOT a candidate.
    result = _peek(detail="full", recursive=True, recent=2)
    # U3: selection unchanged (BB 960, C 900); presentation ascending.
    assert [(row["uuid"], row["depth"]) for row in result["rows"]] == [("C", 0), ("BB", 1)]
    # UTAERZ: every row carries its walked parent, in the JSON too.
    assert {row["uuid"]: row["parent_id"] for row in result["rows"]} == {"C": "D0", "BB": "B"}
    assert result["selection"]["candidates"] == 6  # A B BB C D E -- ancestors never re-injected
    # Ancestors are not counted: B (BB's parent) is not in the result.
    assert "B" not in {row["uuid"] for row in result["rows"]}
    compact = _peek(detail="compact", recursive=True, recent=2, show_counts=False)
    body = _body(compact)
    # Flat: no four-space indentation even though BB has depth 1 -- UTAERZ
    # shows the nesting per row instead (BB's hint names its parent B) and
    # AP-less rows carry their short UUID handle by default.
    assert body[0] == "[C] Charlie #later"
    assert body[1] == "[BB] Bravo child ← in [B]"
    assert "flat, parent shown per row" in compact.splitlines()[0]
    assert "2 most recent by modified of 6 descendants" in compact.splitlines()[0]


def test_parent_id_survives_the_recency_policy_row_copies():
    rows = [
        {"uuid": "r", "parent_id": "ROOT", "created_at": 100, "modified_at": 500, "depth": 0},
        {"uuid": "n", "parent_id": "r", "created_at": 400, "modified_at": 450, "depth": 1},
    ]
    selected, _selection, _sort = apply_dock_peek_recency(
        rows, recent=1, recent_by=None, sort_by=None, sort_direction=None
    )
    # UTAERZ: the policy returns owned row COPIES; parent_id must survive
    # them so the flat compact view can hint and the JSON can carry it.
    assert [row["uuid"] for row in selected] == ["r"]
    assert selected[0]["parent_id"] == "ROOT"


def test_recursive_recent_respects_descend_policy_all(monkeypatch):
    _setup(monkeypatch)
    result = _peek(detail="full", recursive=True, descend_policy="all", recent=2)
    # U3: selection unchanged (DD 995, BB 960); presentation ascending.
    assert [(row["uuid"], row["depth"]) for row in result["rows"]] == [("BB", 1), ("DD", 1)]
    assert result["selection"]["candidates"] == 7


def test_whole_inventory_timestamp_sort_reorders_everything_and_flattens(monkeypatch):
    _setup(monkeypatch)
    result = _peek(detail="full", sort_by="created", sort_direction="desc")
    assert [row["uuid"] for row in result["rows"]] == ["B", "C", "D", "A", "E"]
    assert result["selection"]["recent"] is None and result["count"] == 5
    assert result["sort"] == {"by": "created", "direction": "desc"}
    compact = _peek(detail="compact", recursive=True, sort_by="modified", show_counts=False)
    header = compact.splitlines()[0]
    assert "6 rows (recursive; stops at nested Docks) · sorted by modified (oldest first) · flat, parent shown per row" in header
    # Flat: BB (depth 1) sits directly after the padded AP column, no indent,
    # with its default short handle and its UTAERZ parent hint.
    pad = " " * len("[ap:AAAAAA]") + " "
    assert pad + "[BB] Bravo child ← in [B]" in _body(compact)
    assert pad + "    Bravo child" not in _body(compact)


def test_no_knobs_is_unchanged_document_order_with_indentation(monkeypatch):
    _setup(monkeypatch)
    compact = _peek(detail="compact", recursive=True, show_counts=False)
    body = _body(compact)
    assert body[0] == "[ap:AAAAAA] Alpha #topic-x #due"
    assert body[1].endswith("[B] Bravo")  # AP-less rows carry the short UUID handle by default
    assert body[2].endswith("    [BB] Bravo child")  # document order keeps the indent
    assert "← in" not in compact  # and the plain document-order peek never hints
    assert compact.splitlines()[0].startswith("TODO Dock #wfy-ns — 6 rows (recursive; stops at nested Docks) · ")


@pytest.mark.parametrize(
    "params,code",
    [
        ({"recent": 0}, "invalid_parameter_combination"),
        ({"recent": -3}, "invalid_parameter_combination"),
        ({"recent": True}, "invalid_parameter_type"),
        ({"recent": 2.5}, "invalid_parameter_type"),
        ({"recent_by": "modified"}, "invalid_parameter_combination"),
        ({"recent": 2, "recent_by": "touched"}, "invalid_enum_value"),
        ({"sort_by": "priority"}, "invalid_enum_value"),
        ({"sort_direction": "newest"}, "invalid_enum_value"),
    ],
)
def test_recency_knob_validation_is_loud(monkeypatch, params, code):
    _setup(monkeypatch)
    with pytest.raises(server._AnchorOperationError) as exc:
        _peek(detail="full", **params)
    assert exc.value.code == code


def test_registry_dispatch_threads_recency_knobs_and_rejects_unknown(monkeypatch):
    _setup(monkeypatch)
    result = _dispatch(detail="full", recent=1, recent_by="created")
    assert [row["uuid"] for row in result["rows"]] == ["B"]
    # Schema-level validation for the enum happens before the impl.
    bad = _dispatch(detail="full", recent=1, recent_by="touched")
    assert bad["error"]["code"] == "invalid_enum_value"
    unknown = _dispatch(detail="full", most_recent=3)
    assert unknown["error"]["code"] in ("forbidden_parameter", "unknown_parameter", "invalid_parameter")


# ---------------------------------------------------------------------------
# WG3QTR: full names + trailing tags, no default elision, opt-in budget
# ---------------------------------------------------------------------------

def test_long_names_are_never_truncated_by_default_in_compact_or_full(monkeypatch):
    long_title = "Meeting politics " + "x" * 400 + " CLOSEOUT BATON."
    cache = _cache()
    cache["A"]["name"] = f"[ap:AAAAAA] {long_title} #conv-convrisk #topic-legal"
    _setup(monkeypatch, cache)
    compact = _peek(detail="compact", show_counts=False)
    body = _body(compact)
    assert body[0] == f"[ap:AAAAAA] {long_title} #conv-convrisk #topic-legal"
    assert "…" not in compact
    full = _peek(detail="full")
    assert full["rows"][0]["name"] == f"[ap:AAAAAA] {long_title} #conv-convrisk #topic-legal"
    assert full["rows"][0]["tags"] == ["#conv-convrisk", "#topic-legal"]
    assert full["rows"][0]["title"] == f"[ap:AAAAAA] {long_title}"


def test_max_name_chars_is_an_explicit_opt_in_and_max_chars_is_a_silent_alias(monkeypatch):
    long_title = "Meeting politics " + "x" * 400 + " CLOSEOUT BATON."
    cache = _cache()
    cache["A"]["name"] = f"[ap:AAAAAA] {long_title} #conv-convrisk"
    _setup(monkeypatch, cache)
    budget = _dispatch(detail="compact", max_name_chars=40, show_counts=False)
    row = _body(budget)[0]
    assert row.startswith("[ap:AAAAAA] ") and "…" in row
    assert len(row) == len("[ap:AAAAAA] ") + 40
    aliased = _dispatch(detail="compact", max_chars=40, show_counts=False)
    assert aliased == budget
    rejected = _dispatch(detail="full", max_name_chars=40)
    assert rejected["error"]["code"] == "invalid_parameter_combination"
    assert "max_name_chars" in rejected["error"]["message"]


def test_dock_header_shows_the_dock_name_in_full(monkeypatch):
    _setup(monkeypatch)
    compact = _peek(detail="compact", show_counts=False)
    assert compact.splitlines()[0].startswith("TODO Dock #wfy-ns — 5 children")


# ---------------------------------------------------------------------------
# COORGU: `recent` is the public property; `tail` / `since` are silent aliases
# ---------------------------------------------------------------------------

def test_recent_is_canonical_and_tail_since_are_aliases_on_every_surface():
    surfaces = [
        (server.GLIMPSE_REGISTRY, "run"),
        (server.ANCHOR_REGISTRY, "dock.render"),
        (server.SLICE_GROUP_REGISTRY, "render"),
        (server.SLICE_GROUP_REGISTRY, "sift"),
        (server.SLICE_GROUP_REGISTRY, "merge.render"),
        (server.LIGHTNING_ROD_REGISTRY, "render_tags"),
        (server.LIGHTNING_ROD_REGISTRY, "render_search"),
        (server.LIGHTNING_ROD_REGISTRY, "render_symbols"),
    ]
    for registry, verb in surfaces:
        spec = registry.resolve(verb)
        props = spec.params_schema["properties"]
        assert "recent" in props, (registry.family, verb)
        assert "tail" not in props and "since" not in props, (registry.family, verb)
        assert spec.param_aliases.get("tail") == "recent", (registry.family, verb)
        assert spec.param_aliases.get("since") == "recent", (registry.family, verb)
        assert "recent" in props["recent"]["description"].lower() or "most-recent" in props["recent"]["description"].lower()
        for example in registry.describe_full(verb)["examples"]:
            validate_params(spec, example["call"].get("params") or {})
    # No family exposes a canonical `tail` property anywhere any more.
    for registry in (server.GLIMPSE_REGISTRY, server.ANCHOR_REGISTRY, server.SLICE_GROUP_REGISTRY, server.LIGHTNING_ROD_REGISTRY):
        for spec in registry.operations():
            assert "tail" not in spec.params_schema.get("properties", {}), (registry.family, spec.verb)


def test_dock_render_accepts_tail_since_and_recent_and_impl_sees_recent(monkeypatch):
    seen = []

    async def render(handle, **kwargs):
        seen.append(kwargs)
        return {"success": True, "preview_markdown": "# Dock"}

    monkeypatch.setattr(server, "_anchor_dock_render_impl", render)
    registry = server.build_anchor_registry()
    for key in ("tail", "since", "recent"):
        assert asyncio.run(dispatch_operation(registry, "dock.render", {"dock_handle": "D", key: "newest 2"})) == "# Dock"
    assert [k["recent"] for k in seen] == ["newest 2"] * 3
    conflict = asyncio.run(dispatch_operation(registry, "dock.render", {"dock_handle": "D", "tail": "1w", "recent": "1w"}))
    assert conflict["error"]["code"] == "forbidden_parameter"


def test_glimpse_run_and_slice_group_render_accept_tail_alias(monkeypatch):
    calls = []

    async def run_impl(**kwargs):
        calls.append(("glimpse", kwargs))
        return {"success": True, "preview_markdown": "# G"}

    monkeypatch.setattr(server, "_glimpse_run_impl", run_impl)
    glimpse = server.build_glimpse_registry()
    asyncio.run(dispatch_operation(glimpse, "run", {"root_handle": "R", "tail": "last 3"}))
    asyncio.run(dispatch_operation(glimpse, "run", {"root_handle": "R", "since": "newest 3"}))
    assert calls[0][1]["since"] == "last 3" and calls[1][1]["since"] == "newest 3"

    seen = []

    async def lightning(**kwargs):
        seen.append(kwargs)
        return "RAW"

    monkeypatch.setattr(server, "_lightning_rod_legacy_impl", lightning)
    rod = server.build_lightning_rod_registry()
    asyncio.run(dispatch_operation(rod, "render_search", {"root_handle": "R", "query": "q", "tail": "1w"}))
    assert seen[-1]["since"] == "1w"


def test_dock_render_impl_maps_newest_token_to_item_count_since_edge(monkeypatch):
    config = (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})
    monkeypatch.setattr(server, "_resolve_note_session_config", lambda _tab: _async_value(config))
    monkeypatch.setattr(server, "_resolve_generic_node_handle", lambda value, **_kw: _async_value("D"))
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: {"D": {"id": "D", "name": "Dock #wfy-ns"}})
    seen = []

    async def raw(**kwargs):
        seen.append(kwargs)
        return {"success": True, "preview_markdown": "# Dock"}

    monkeypatch.setattr(server, "_glimpse_root_uuid_from_cache", raw)
    asyncio.run(server._anchor_dock_render_impl("D", recent="newest 7"))
    assert seen[0]["since"].form == "item_count" and seen[0]["since"].count == 7


# ---------------------------------------------------------------------------
# Token grammar: `newest N` is a synonym of `last N`; curated stays creation-time
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token,n", [("newest 5", 5), ("NEWEST 12 items", 12), ("newest 1 item", 1), ("last 5", 5)])
def test_newest_n_is_an_item_count_synonym(token, n):
    assert is_item_count_token(token)
    assert parse_since_item_count(token) == n
    spec, error = SinceCodec.from_token_result(token)
    assert error is None and spec.form == "item_count" and spec.count == n
    # The canonical display token is unchanged: 'last N'.
    assert SinceCodec.to_token(spec) == f"last {n}"


@pytest.mark.parametrize("token", ["newest", "newest banana", "newest 0", "newest -2"])
def test_malformed_newest_tokens_are_loud_item_count_errors_not_windows(token):
    # Same routing as the established 'last ...' family: the keyword claims the
    # token for the item-count grammar, and a bad count is a loud error rather
    # than a silent fall-through to the window parser.
    assert is_item_count_token(token)
    assert parse_since_item_count(token) is None
    _spec, error = SinceCodec.from_token_result(token)
    assert error and "newest N" in error


def test_newest_prefix_without_a_space_is_not_an_item_count_token():
    assert not is_item_count_token("newestly 3")
    assert not is_item_count_token("7")


def test_curated_checkpoint_keeps_creation_time_axis_by_default():
    spec, error = SinceCodec.from_token_result("curated")
    assert error is None and spec.form == "curated"
    assert spec.axis == "created" and spec.direction == "asc"
    window, _ = SinceCodec.from_token_result("1w")
    assert window.form == "window" and window.axis == "created"


# ---------------------------------------------------------------------------
# Discoverability (D): describe + the top-level MCP description teach the workflow
# ---------------------------------------------------------------------------

def test_describe_and_top_level_description_teach_recency_and_full_names():
    registry = server.ANCHOR_REGISTRY
    full = registry.describe_full("dock.peek")
    props = full["params_schema"]["properties"]
    for key in ("recent", "recent_by", "sort_by", "sort_direction", "max_name_chars"):
        assert key in props, key
    assert "max_chars" not in props
    assert props["recent_by"]["enum"] == ["created", "modified", None]
    assert props["sort_by"]["enum"] == ["none", "created", "modified", None]
    assert "unlimited" in props["max_name_chars"]["description"]
    assert "#tags" in full["returns"]["description"]
    assert "selection" in full["returns"]["description"]
    example_params = [ex["call"]["params"] for ex in full["examples"]]
    assert any(p.get("recent") == 20 for p in example_params)
    assert any(p.get("recent_by") == "created" for p in example_params)
    assert any(p.get("sort_by") == "modified" for p in example_params)
    assert any(p.get("recursive") and p.get("recent") for p in example_params)
    index_line = next(line for line in registry.index_lines() if line.startswith("dock.peek"))
    assert "recent=N" in index_line and "modified" in index_line and "created" in index_line
    description = server._ANCHOR_TOOL_DESCRIPTION
    assert "to-do" in description
    assert "recent_by='created'" in description and "recent_by='modified'" in description
    assert "anchor.point.touch" in description
    assert "tags included" in description
    # The GLIMPSE family's own describe says recent / most-recent, not tail.
    glimpse_full = server.GLIMPSE_REGISTRY.describe_full("run")
    assert "recent" in glimpse_full["returns"]["description"]
    assert "tail accepts" not in glimpse_full["returns"]["description"]
