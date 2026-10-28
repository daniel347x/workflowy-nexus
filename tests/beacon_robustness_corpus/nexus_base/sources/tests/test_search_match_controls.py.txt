"""[ap:O3MDOY] half 2 (unit G11-5e) -- the ONE node-level match-control set.

Covers the pieces the surfaces share:

* ``note_lines.build_search_node_controls`` / ``select_note_documents_for_report``
  (assembly + the ONE recency idiom ``apply_dock_peek_recency``);
* ``glimpse_since_policy.cap_forest_to_first_node_count`` (the headings-tier
  node cap beside the prune family);
* ``server._normalize_note_grep_params`` (the ONE value owner: validation,
  the 'last N'/'newest N' token, the loud refusals);
* ``server._node_limit_sentence`` + the shaper composition (C1: the
  node-limit line opens the Markdown AFTER the recency line).

Every behaviour below fails against the pre-unit code: the controls did not
exist, so the calls refuse outright (unknown parameter / missing function).
"""
from __future__ import annotations

import asyncio
import pytest

from workflowy_mcp import server
from workflowy_mcp.glimpse_since_policy import cap_forest_to_first_node_count
from workflowy_mcp.mcp_api_registry import (
    dispatch_operation,
    validate_params,
)
from workflowy_mcp.note_lines import (
    build_search_node_controls,
    compile_note_query,
    search_note_documents,
    select_note_documents_for_report,
)


def _finder(token):
    return compile_note_query(token, is_regex=False, case_sensitive=False)


def _docs():
    # Five matching docs, oldest first; each has exactly one hit line.
    return [
        ("u1", "one", "hit 1"),
        ("u2", "two", "hit 2"),
        ("u3", "three", "hit 3"),
        ("u4", "four", "hit 4"),
        ("u5", "five", "hit 5"),
    ]


def _timestamps(*pairs):
    return {
        uuid: {"created_at": created, "modified_at": modified}
        for uuid, (created, modified) in pairs
    }


# ---------------------------------------------------------------------------
# build_search_node_controls / select_note_documents_for_report
# ---------------------------------------------------------------------------

def test_build_search_node_controls_inactive_returns_none():
    assert build_search_node_controls() is None
    assert build_search_node_controls(sort_by="none", max_nodes=None) is None
    assert build_search_node_controls(sort_by="none", recent=None, max_nodes=None) is None


def test_recent_keeps_newest_matching_nodes_and_survivors_keep_document_order():
    controls = build_search_node_controls(
        recent=2, timestamps=_timestamps(
            ("u1", (1, 1)), ("u2", (2, 2)), ("u3", (3, 3)),
            ("u4", (4, 4)), ("u5", (5, 5)),
        ),
    )
    ordered, _hits, facts = select_note_documents_for_report(
        _docs(), _finder("hit"), controls,
    )
    # Selection never reorders: the two newest, still in document order.
    assert [doc[0] for doc in ordered] == ["u4", "u5"]
    assert facts == {
        "matching_nodes": 5,
        "returned_nodes": 2,
        "nodes_hidden_by_recent": 3,
        "nodes_hidden_by_max_nodes": 0,
    }


def test_recent_basis_resolves_created_and_missing_timestamps_sink_last():
    # u5 is newest by modified, u1 by created: the basis decides.
    controls = build_search_node_controls(
        recent=1, recent_by="created", timestamps=_timestamps(
            ("u1", (9, 1)), ("u5", (1, 9)),
        ),
    )
    ordered, _h, _f = select_note_documents_for_report(
        [("u1", "one", "hit"), ("u5", "five", "hit")], _finder("hit"), controls,
    )
    assert [doc[0] for doc in ordered] == ["u1"]

    undated = build_search_node_controls(recent=1, timestamps={})
    ordered, _h, _f = select_note_documents_for_report(
        [("u1", "one", "hit"), ("u2", "two", "hit")], _finder("hit"), undated,
    )
    # Missing timestamps sink last under either direction; ties keep document
    # order -- so the first document wins.
    assert [doc[0] for doc in ordered] == ["u1"]


def test_sort_by_orders_groups_and_direction_flips_without_reselecting():
    controls = build_search_node_controls(
        sort_by="created", sort_direction="desc", timestamps=_timestamps(
            ("u1", (1, 1)), ("u2", (2, 2)), ("u3", (3, 3)),
        ),
    )
    ordered, _h, _f = select_note_documents_for_report(
        _docs()[:3], _finder("hit"), controls,
    )
    assert [doc[0] for doc in ordered] == ["u3", "u2", "u1"]

    asc = build_search_node_controls(
        sort_by="modified", timestamps=_timestamps(
            ("u1", (1, 3)), ("u2", (2, 2)), ("u3", (3, 1)),
        ),
    )
    ordered, _h, _f = select_note_documents_for_report(
        _docs()[:3], _finder("hit"), asc,
    )
    assert [doc[0] for doc in ordered] == ["u3", "u2", "u1"]


def test_max_nodes_caps_positionally_and_counts_hidden():
    controls = build_search_node_controls(max_nodes=2)
    ordered, _h, facts = select_note_documents_for_report(
        _docs(), _finder("hit"), controls,
    )
    assert [doc[0] for doc in ordered] == ["u1", "u2"]
    assert facts["returned_nodes"] == 2
    assert facts["nodes_hidden_by_max_nodes"] == 3
    assert facts["nodes_hidden_by_recent"] == 0


def test_controls_compose_selection_then_order_then_cap():
    controls = build_search_node_controls(
        recent=3, sort_by="created", sort_direction="asc", max_nodes=1,
        timestamps=_timestamps(
            ("u1", (1, 1)), ("u2", (2, 2)), ("u3", (3, 3)),
            ("u4", (4, 4)), ("u5", (5, 5)),
        ),
    )
    ordered, _h, facts = select_note_documents_for_report(
        _docs(), _finder("hit"), controls,
    )
    # recent selects {u3, u4, u5}; created asc orders u3, u4, u5; the cap
    # keeps the first displayed node.
    assert [doc[0] for doc in ordered] == ["u3"]
    assert facts["nodes_hidden_by_recent"] == 2
    assert facts["nodes_hidden_by_max_nodes"] == 2


def test_marker_documents_are_matching_nodes_and_page_facts_reach_collect():
    docs = [
        ("u1", "one", "hit one"),
        ("u2", "two", "", "(title match)"),
        ("u3", "three", "hit three"),
    ]
    controls = build_search_node_controls(
        recent=1, timestamps=_timestamps(("u1", (1, 1)), ("u2", (2, 2)), ("u3", (3, 3))),
    )
    ordered, _h, facts = select_note_documents_for_report(docs, _finder("hit"), controls)
    # u3 is the newest matching node; the marker doc and u1 were trimmed.
    assert [doc[0] for doc in ordered] == ["u3"]
    assert facts["matching_nodes"] == 3
    assert facts["nodes_hidden_by_recent"] == 2

    collect: dict = {}
    report = search_note_documents(
        docs, _finder("hit"), context_lines=0, max_matches=10,
        node_controls=build_search_node_controls(max_nodes=2),
        collect=collect,
    )
    # The cap is positional: the first two candidates in document order stay
    # (u1's hit rows and u2's marker header); u3 is hidden and counted.
    assert "u1" in report and "(title match)" in report
    assert "u3" not in report
    assert collect["returned_nodes"] == 2
    assert collect["matching_nodes"] == 3
    assert collect["nodes_hidden_by_max_nodes"] == 1


def test_report_footer_counts_hidden_nodes_and_omits_zero_facts():
    docs = _docs()
    report = search_note_documents(
        docs, _finder("hit"), context_lines=0, max_matches=10,
        node_controls=build_search_node_controls(
            recent=2, timestamps=_timestamps(
                ("u1", (1, 1)), ("u2", (2, 2)), ("u3", (3, 3)),
                ("u4", (4, 4)), ("u5", (5, 5)),
            ),
        ),
    )
    assert "returned_nodes=2" in report
    assert "matching_nodes=5" in report
    assert "nodes_hidden_by_recent=3" in report
    assert "nodes_hidden_by_max_nodes" not in report  # zero facts stay absent

    # Without controls the footer is byte-identical to the pre-unit shape.
    plain = search_note_documents(docs, _finder("hit"), context_lines=0, max_matches=10)
    assert "returned_nodes" not in plain
    assert "Pagination: offset=0; returned_matches=5; returned_occurrences=5; has_more=false; next_offset=null." in plain


def test_node_limit_line_composes_after_the_recency_line_in_the_shaper():
    result = {
        "preview_markdown": "body",
        "since_applied": {"form": "item_count", "count": 3, "selection": "newest", "basis": "modified"},
        "render_scope": {"sort_direction": "asc"},
        "node_limit_applied": {"max_nodes": 1, "matched": 5, "shown": 1, "hidden": 4},
    }
    shaped = server._shape_glimpse_family_return(result, output_file=None)
    lines = shaped.splitlines()
    assert lines[0].startswith("> Recency selection: the 3 newest by modified")
    assert lines[1].startswith("> Node limit (max_nodes=1): showing the first 1 of 5 matching nodes")
    assert lines[2] == "body"

    # The structured twin carries the header key and the raw echo (a
    # structured payload carries NO preview_markdown: the Markdown branch
    # would win otherwise).
    structured = server._shape_glimpse_family_return(
        {
            "preview_tree": [{"id": "x"}],
            "since_applied": result["since_applied"],
            "render_scope": result["render_scope"],
            "node_limit_applied": result["node_limit_applied"],
        },
        output_file=None,
        headings_only=True,
    )
    assert structured["node_limit_applied"]["hidden"] == 4
    assert structured["node_limit_header"].startswith("Node limit (max_nodes=1)")

    # No cap fired -> byte-identical to before.
    assert server._shape_glimpse_family_return({"preview_markdown": "body"}, output_file=None) == "body"


# ---------------------------------------------------------------------------
# cap_forest_to_first_node_count (the headings-tier prune)
# ---------------------------------------------------------------------------

def _tree(node_id, children=None, *, matched=True):
    return {"id": node_id, "name": node_id, "note": "", "children": children or []}


def test_cap_forest_counts_matching_nodes_only_and_keeps_connective():
    forest = [_tree("R", [_tree("A", [_tree("A1")]), _tree("B", [_tree("B1")])])]
    def predicate(node):
        return node["id"] in {"A", "A1", "B", "B1"}

    # Budget 1: display order A (consume) then A1 (dropped with its subtree);
    # R stays as A's connective parent; B's whole branch drops.
    capped, echo = cap_forest_to_first_node_count(forest, 1, member_predicate=predicate)
    assert echo == {"max_nodes": 1, "matched": 4, "shown": 1, "hidden": 3}
    top = capped[0]
    assert top["id"] == "R"
    assert [child["id"] for child in top["children"]] == ["A"]
    assert top["children"][0]["children"] == []  # A kept, childless

    # Budget 2 keeps A and A1 (display order: A precedes its own child A1);
    # B's whole branch drops.
    capped, echo = cap_forest_to_first_node_count(forest, 2, member_predicate=predicate)
    assert echo["hidden"] == 2
    assert [child["id"] for child in capped[0]["children"]] == ["A"]
    assert [child["id"] for child in capped[0]["children"][0]["children"]] == ["A1"]


def test_cap_forest_returns_none_echo_when_everything_fits_and_never_mutates():
    import copy

    forest = [_tree("R", [_tree("A")])]
    snapshot = copy.deepcopy(forest)
    capped, echo = cap_forest_to_first_node_count(forest, 5)
    assert echo is None
    assert capped[0]["id"] == "R"
    assert forest == snapshot


def test_cap_forest_counts_every_node_without_a_predicate():
    forest = [_tree("R", [_tree("A"), _tree("B"), _tree("C")])]
    capped, echo = cap_forest_to_first_node_count(forest, 2)
    assert echo == {"max_nodes": 2, "matched": 4, "shown": 2, "hidden": 2}
    top = capped[0]
    assert [child["id"] for child in top["children"]] == ["A"]


# ---------------------------------------------------------------------------
# server._normalize_note_grep_params -- the ONE value owner
# ---------------------------------------------------------------------------

def test_match_controls_default_inactive():
    normalized = server._normalize_note_grep_params({})
    for name in ("sort_by", "sort_direction", "recent", "recent_by", "max_nodes"):
        assert normalized[name] is None


def test_match_controls_parse_tokens_integers_and_enums():
    normalized = server._normalize_note_grep_params({
        "sort_by": "created", "sort_direction": "desc",
        "recent": "last 3", "recent_by": "modified", "max_nodes": 10,
    })
    assert normalized["sort_by"] == "created"
    assert normalized["sort_direction"] == "desc"
    assert normalized["recent"] == 3
    assert normalized["recent_by"] == "modified"
    assert normalized["max_nodes"] == 10

    assert server._normalize_note_grep_params({"recent": 7})["recent"] == 7
    assert server._normalize_note_grep_params({"recent": "newest 2"})["recent"] == 2
    assert server._normalize_note_grep_params({"sort_by": "none"})["sort_by"] is None


@pytest.mark.parametrize("params,code", [
    ({"sort_by": "newest"}, "invalid_enum_value"),
    ({"sort_direction": "desc"}, "sort_direction_without_sort_by"),
    ({"sort_by": "none", "sort_direction": "desc"}, "sort_direction_without_sort_by"),
    ({"sort_direction": "sideways", "sort_by": "created"}, "invalid_enum_value"),
    ({"recent_by": "touched"}, "invalid_enum_value"),
    ({"max_nodes": 0}, "invalid_parameter_value"),
    ({"max_nodes": True}, "invalid_parameter_type"),
    ({"recent": "1w"}, "invalid_parameter_value"),
    ({"recent": "bottom 3"}, "invalid_parameter_value"),
    ({"recent": "curated"}, "invalid_parameter_value"),
    ({"recent": "last 0"}, "invalid_parameter_value"),
    ({"recent": True}, "invalid_parameter_type"),
])
def test_match_controls_reject_bad_values_loudly(params, code):
    with pytest.raises(Exception) as excinfo:
        server._normalize_note_grep_params(params)
    assert excinfo.value.code == code


def test_node_search_schema_carries_the_shared_control_vocabulary():
    spec = server.build_node_registry().resolve("search")
    validate_params(spec, {
        "root_handle": "7b97b6a9-467c-43eb-9bab-90f736cedfe6",
        "query": "q", "recent": "last 10", "sort_by": "modified",
        "sort_direction": "desc", "recent_by": "created", "max_nodes": 5,
    })
    anchor_spec = server.build_anchor_registry().resolve("search")
    for name in ("sort_by", "sort_direction", "recent", "recent_by", "max_nodes"):
        assert name in anchor_spec.params_schema["properties"]
    # One wording, shared: the fragment's descriptions are the same objects
    # node.search carries.
    node_props = spec.params_schema["properties"]
    anchor_props = anchor_spec.params_schema["properties"]
    assert node_props["recent"]["description"] == anchor_props["recent"]["description"]
    assert "sort_direction_without_sort_by" in spec.domain_error_codes
    assert "sort_direction_without_sort_by" in anchor_spec.domain_error_codes


def test_node_search_recent_cuts_to_newest_nodes_and_counts_the_footer(monkeypatch):
    from tests.test_node_lines import client_for, dispatch

    nodes = [
        {"id": "0a0a0a0a-0000-4000-8000-000000000001", "name": "old one", "note": "hit old", "parent_id": None, "createdAt": 10, "modifiedAt": 10},
        {"id": "0b0b0b0b-0000-4000-8000-000000000002", "name": "mid", "note": "hit mid", "parent_id": "0a0a0a0a-0000-4000-8000-000000000001", "priority": 1, "createdAt": 20, "modifiedAt": 20},
        {"id": "0c0c0c0c-0000-4000-8000-000000000003", "name": "new one", "note": "hit new", "parent_id": "0a0a0a0a-0000-4000-8000-000000000001", "priority": 2, "createdAt": 30, "modifiedAt": 30},
    ]
    client_for(monkeypatch, nodes=nodes)
    result = dispatch(
        "search",
        {"root_handle": "0a0a0a0a", "query": "hit", "context_lines": 0, "recent": "last 1"},
    )
    assert "hit new" in result
    assert "hit old" not in result and "hit mid" not in result
    assert "returned_nodes=1" in result
    assert "matching_nodes=3" in result
    assert "nodes_hidden_by_recent=2" in result


def test_node_search_sort_direction_without_sort_by_is_loud(monkeypatch):
    from tests.test_node_lines import client_for, dispatch

    client_for(monkeypatch, nodes=[
        {"id": "0a0a0a0a-0000-4000-8000-000000000001", "name": "one", "note": "hit", "parent_id": None},
    ])
    result = dispatch(
        "search",
        {"root_handle": "0a0a0a0a", "query": "hit", "sort_direction": "desc"},
    )
    assert result["error"]["code"] == "sort_direction_without_sort_by"
