"""anchor.search: a node.search-shaped grep scoped to ONE Anchor Dock's
subtree ([ap:O3MDOY] half 1, unit G8-5d).

The query machinery and report are node.search's -- the ONE shared
``_note_grep_plan`` / ``_note_grep_report`` tail over
``note_lines.search_note_documents`` (the node.search suite pins that tail
byte-for-byte through the refactor). What is new here is the Dock-native
scope: dock.peek's acquisition (the live Dock vocabulary, the generic
resolver, the pinned operation cache), the ONE Stage-1 walk with the shared
anchor boundary (descending stops at child Docks by default; each child
Dock's own name/note is searched as a signpost), and node.search's
completed-hidden default.

Every scope behaviour below fails against the pre-unit code: the verb did
not exist, so the schema rejected the call outright.
"""
from __future__ import annotations

import asyncio

import pytest
from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import dispatch_operation

DOCK = "d0000000-0000-4000-8000-00000000000d"
KID = "11111111-1111-4111-8111-111111111111"
TOD = "22222222-2222-4222-8222-222222222222"
DEEP = "33333333-3333-4333-8333-333333333333"
BOTH = "77777777-7777-4777-8777-777777777777"
TITLE = "44444444-4444-4444-8444-444444444444"
DONE = "55555555-5555-4555-8555-555555555555"
OUT = "66666666-6666-4666-8666-666666666666"


def _cache():
    return {
        DOCK: {"id": DOCK, "name": "[ap:SRCH01] Search Dock #wfy-ns", "note": "dock note hit", "parent_id": None},
        KID: {"id": KID, "name": "Alpha spec", "note": "line one\nHIT here\nline three", "parent_id": DOCK, "priority": 10},
        TOD: {"id": TOD, "name": "Nested Dock #wfy-ns--local", "note": "nested dock note HIT", "parent_id": DOCK, "priority": 20},
        DEEP: {"id": DEEP, "name": "Deep behind the boundary", "note": "deep HIT", "parent_id": TOD, "priority": 1},
        BOTH: {"id": BOTH, "name": "HIT marker", "note": "HIT in note", "parent_id": DOCK, "priority": 25},
        TITLE: {"id": TITLE, "name": "HITlist runbook", "note": "nothing relevant", "parent_id": DOCK, "priority": 30},
        DONE: {"id": DONE, "name": "Completed child", "note": "HIT done", "parent_id": DOCK, "priority": 40, "completed": True},
        OUT: {"id": OUT, "name": "Outside node", "note": "HIT outside", "parent_id": None},
    }


def _async_value(value):
    async def _value(*_args, **_kwargs):
        return value

    return _value()


def _setup(monkeypatch, cache=None, *, config_error=None):
    if config_error is not None:
        def _boom(_tab):
            raise RuntimeError(config_error)

        monkeypatch.setattr(server, "_resolve_note_session_config", _boom)
    else:
        config = (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})
        monkeypatch.setattr(server, "_resolve_note_session_config", lambda _tab: _async_value(config))
    monkeypatch.setattr(server, "_resolve_generic_node_handle", lambda value, **_kw: _async_value(value))
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: cache if cache is not None else _cache())


def _dispatch_raw(params):
    return asyncio.run(dispatch_operation(server.build_anchor_registry(), "search", params))


def _dispatch(**params):
    return _dispatch_raw({"dock_handle": DOCK, **params})


# ---------------------------------------------------------------------------
# Scope: the Dock's subtree, in walk order, root included
# ---------------------------------------------------------------------------

def test_scope_hits_in_document_order_root_included_outside_excluded(monkeypatch):
    _setup(monkeypatch)
    result = _dispatch(query="hit", context_lines=0)
    assert result.index("dock note hit") < result.index("HIT here") < result.index("nested dock note HIT")
    assert "HIT outside" not in result  # outside the Dock: never a document
    assert result.startswith(
        "Source: pinned operation cache (Anchor Dock subtree; descending stops at child"
        " Docks -- each child Dock's own name/note is searched, its descendants are not"
        " (descend_policy='all' enters them); completed nodes hidden by default --"
        " include_completed=true searches them too)."
    )


def test_child_dock_is_signpost_descendants_behind_boundary(monkeypatch):
    _setup(monkeypatch)
    result = _dispatch(query="HIT", context_lines=0)
    assert "nested dock note HIT" in result  # the child Dock's own note is searched
    assert "deep HIT" not in result          # its descendants are not
    opened = _dispatch(query="HIT", context_lines=0, descend_policy="all")
    assert "deep HIT" in opened              # 'all' enters child Docks
    assert opened.index("nested dock note HIT") < opened.index("deep HIT")


def test_local_child_dock_bounds_too(monkeypatch):
    # The nested Dock carries #wfy-ns--local: locality never weakens the
    # boundary (the shared build_anchor_boundary_predicate is_anchor rule).
    _setup(monkeypatch)
    result = _dispatch(query="deep", context_lines=0)
    assert "deep HIT" not in result
    assert "No matching note lines." in result


# ---------------------------------------------------------------------------
# Title matches: the D6 marker contract
# ---------------------------------------------------------------------------

def test_title_only_match_renders_marker_header(monkeypatch):
    _setup(monkeypatch)
    result = _dispatch(query="hit", context_lines=0)
    assert f'Matched 0 lines in {TITLE} "HITlist runbook" (title match):' in result


def test_title_and_note_hit_shows_rows_without_marker(monkeypatch):
    _setup(monkeypatch)
    result = _dispatch(query="hit", context_lines=0)
    section = result[result.index(f"Matched 1 lines in {BOTH}"):]
    assert section.startswith(f'Matched 1 lines in {BOTH} "HIT marker":\n1 | HIT in note')
    # the marker appears exactly once in the report -- on the title-only node
    assert result.count("(title match)") == 1


def test_match_scope_name_reports_title_rows_only(monkeypatch):
    _setup(monkeypatch)
    result = _dispatch(query="hit", match_scope="name", context_lines=0)
    assert f'Matched 0 lines in {TITLE} "HITlist runbook" (title match):' in result
    assert f'Matched 0 lines in {BOTH} "HIT marker" (title match):' in result
    assert result.count("(title match)") == 2
    # note lines never leak under 'name' -- for hits or for the Source line
    assert "HIT here" not in result and "HIT in note" not in result
    assert "dock note hit" not in result and "nested dock note HIT" not in result
    assert "deep HIT" not in result  # the boundary still applies under 'name'


# ---------------------------------------------------------------------------
# Completed default, name_filter, paging, widths, purity
# ---------------------------------------------------------------------------

def test_completed_hidden_by_default_include_completed_restores(monkeypatch):
    _setup(monkeypatch)
    result = _dispatch(query="hit", context_lines=0)
    assert "HIT done" not in result
    full = _dispatch(query="hit", context_lines=0, include_completed=True)
    assert "HIT done" in full
    assert "including completed nodes" in full


def test_completed_root_is_still_searched(monkeypatch):
    cache = _cache()
    cache[DOCK] = {**cache[DOCK], "completed": True}
    _setup(monkeypatch, cache)
    result = _dispatch(query="hit", context_lines=0)
    assert "dock note hit" in result  # the root is exempt from the completed prune


def test_name_filter_selects_documents(monkeypatch):
    _setup(monkeypatch)
    result = _dispatch(query="hit", context_lines=0, name_filter="*spec*")
    assert "HIT here" in result  # "Alpha spec"
    assert "dock note hit" not in result
    assert "nested dock note HIT" not in result
    assert "(title match)" not in result


def test_paging_banner_first_offset_and_next_offset(monkeypatch):
    _setup(monkeypatch)
    result = _dispatch(query="hit", context_lines=0, max_matches=2)
    lines = result.splitlines()
    assert lines[0].startswith("INFORMATIONAL, CRITICAL DETAIL: PAGINATION ACTIVE")
    assert "offset=2" in lines[0]
    assert "Pagination: offset=0; returned_matches=2; returned_occurrences=2; has_more=true; next_offset=2." in result
    page2 = _dispatch(query="hit", context_lines=0, max_matches=2, offset=2)
    assert not page2.splitlines()[0].startswith("INFORMATIONAL")  # last page: no banner
    assert "nested dock note HIT" in page2 and 'HITlist runbook" (title match)' in page2
    assert "has_more=false; next_offset=null." in page2


def test_match_width_crops_the_hit_row(monkeypatch):
    _setup(monkeypatch)
    result = _dispatch(query="HIT", context_lines=0, match_width=4)
    row = next(line for line in result.splitlines() if "| " in line and "HIT" in line)
    assert "…" in row and "HIT here" not in row  # cropped to a centred window


def test_report_shape_is_node_searchs(monkeypatch):
    # One header per node, ripgrep rows, the '> ' hit marker beside context,
    # and the exact footer grammar -- the ONE formatter's shape, not a twin.
    _setup(monkeypatch)
    result = _dispatch(query="HIT here", context_lines=1)
    assert f'Matched 1 lines in {KID} "Alpha spec":' in result
    assert "  1 | line one\n> 2 | HIT here\n  3 | line three" in result
    assert "Pagination: offset=0; returned_matches=1; returned_occurrences=1; has_more=false; next_offset=null." in result


def test_scope_walk_never_mutates_the_pinned_cache(monkeypatch):
    cache = _cache()
    _setup(monkeypatch, cache)
    before = {node_id: dict(node) for node_id, node in cache.items()}
    _dispatch(query="hit", context_lines=0)
    assert {node_id: dict(node) for node_id, node in cache.items()} == before


# ---------------------------------------------------------------------------
# Failure shapes: loud refusals, validation before I/O, the alias
# ---------------------------------------------------------------------------

def test_non_dock_root_refused(monkeypatch):
    _setup(monkeypatch)
    result = _dispatch_raw({"dock_handle": OUT, "query": "hit"})
    assert result["success"] is False
    assert result["error"]["code"] == "operation_failed"
    assert "is not an Anchor Dock" in result["error"]["message"]


def test_missing_tab_fails_loud(monkeypatch):
    _setup(monkeypatch, config_error="no live Workflowy tab is connected")
    result = _dispatch(query="hit")
    assert result["success"] is False
    assert "no live Workflowy tab is connected" in result["error"]["message"]


def test_invalid_query_fails_before_any_io(monkeypatch):
    def _boom(_tab):
        raise AssertionError("the config read must not happen before validation")

    monkeypatch.setattr(server, "_resolve_note_session_config", _boom)
    monkeypatch.setattr(server, "_resolve_generic_node_handle", lambda value, **_kw: _async_value(value))
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: _cache())
    result = _dispatch(query="(", is_regex=True)
    assert result["error"]["code"] == "invalid_query"


@pytest.mark.parametrize("params,code", [
    ({"query": "x", "match_scope": "note"}, "invalid_enum_value"),
    # The schema enum rejects a bad descend_policy before the impl (whose own
    # check -- dock.peek's wording -- stays as defense in depth for direct
    # impl callers, exactly like dock.peek's shadowed impl check).
    ({"query": "x", "descend_policy": "none"}, "invalid_enum_value"),
    ({"query": "x", "is_regex": "yes"}, "invalid_parameter_type"),
    ({"query": "x", "include_completed": 1}, "invalid_parameter_type"),
    ({"query": "x", "context_lines": 11}, "invalid_parameter_value"),
    ({"query": "x", "max_matches": 0}, "invalid_parameter_value"),
    ({"query": "x", "context_width": -1}, "invalid_parameter_value"),
    ({"query": "(", "is_regex": True}, "invalid_query"),
])
def test_param_validation_codes(monkeypatch, params, code):
    _setup(monkeypatch)
    result = _dispatch(**params)
    assert result["error"]["code"] == code


def test_registry_level_validation_and_alias(monkeypatch):
    _setup(monkeypatch)
    missing = _dispatch_raw({"dock_handle": DOCK})
    assert missing["error"]["code"] == "missing_parameter"
    unknown = _dispatch_raw({"dock_handle": DOCK, "query": "x", "since": "last 1"})
    assert unknown["error"]["code"] == "forbidden_parameter"
    via_alias = _dispatch_raw({"root_handle": DOCK, "query": "hit", "context_lines": 0})
    assert "HIT here" in via_alias
    conflict = _dispatch_raw({"dock_handle": DOCK, "root_handle": DOCK, "query": "hit"})
    assert conflict["error"]["code"] == "forbidden_parameter"


# ---------------------------------------------------------------------------
# [ap:O3MDOY] half 2 (unit G11-5e): the ONE node-level match controls.
# Every assertion below fails against the pre-unit code: the controls did
# not exist, so the schema refused the call outright.
# ---------------------------------------------------------------------------

def _dated_cache():
    cache = _cache()
    cache[KID]["createdAt"] = 10
    cache[KID]["modifiedAt"] = 10
    cache[TOD]["createdAt"] = 20
    cache[TOD]["modifiedAt"] = 20
    cache[BOTH]["createdAt"] = 30
    cache[BOTH]["modifiedAt"] = 30
    # TITLE carries no timestamp: it sinks last, never newest.
    return cache


def test_recent_keeps_newest_matching_nodes_and_counts_the_footer(monkeypatch):
    _setup(monkeypatch, _dated_cache())
    result = _dispatch(query="HIT", context_lines=0, recent="last 1")
    # BOTH (modified 30) is the newest matching node; the child-Dock
    # signpost (20), the kid (10) and the two undated markers (the Dock
    # root's own note hit, the title match) sink away -- undated sinks last.
    assert "HIT in note" in result
    assert "HIT here" not in result
    assert "nested dock note HIT" not in result
    assert "(title match)" not in result
    assert "dock note hit" not in result
    assert "returned_nodes=1" in result
    assert "matching_nodes=5" in result
    assert "nodes_hidden_by_recent=4" in result


def test_sort_by_orders_groups_and_max_nodes_caps(monkeypatch):
    _setup(monkeypatch, _dated_cache())
    ordered = _dispatch(query="HIT", context_lines=0, sort_by="created", sort_direction="desc")
    # Descending by the node's own createdAt: BOTH, TOD, KID, then the
    # undated TITLE marker last.
    assert ordered.index("HIT in note") < ordered.index("nested dock note HIT")
    assert ordered.index("nested dock note HIT") < ordered.index("HIT here")

    capped = _dispatch(
        query="HIT", context_lines=0,
        sort_by="created", sort_direction="desc", max_nodes=2,
    )
    assert "HIT in note" in capped
    assert "nested dock note HIT" in capped
    assert "HIT here" not in capped
    assert "(title match)" not in capped
    assert "returned_nodes=2" in capped
    assert "matching_nodes=5" in capped
    # BOTH and TOD kept; KID plus the two undated markers capped away.
    assert "nodes_hidden_by_max_nodes=3" in capped


def test_sort_direction_without_sort_by_is_loud(monkeypatch):
    _setup(monkeypatch)
    result = _dispatch(query="hit", sort_direction="desc")
    assert result["error"]["code"] == "sort_direction_without_sort_by"
