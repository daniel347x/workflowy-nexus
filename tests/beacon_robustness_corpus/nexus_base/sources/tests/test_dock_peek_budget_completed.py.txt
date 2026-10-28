"""U5 peek follow-up: the Dock description-excerpt budget (CF-MST-015) and
completed children out of the recency view (CF-MST-016) -- plus U6 item 1
(J11): completed rows out of the plain document-order inventory by default
too, through the same ``include_completed`` flag at the same single
chokepoint.

Behaviour tests here are mutation-sensitive against the pre-U5 code (c1bd581):
the compact '> …' description excerpt was unlimited by default (it shared the
name budget, which WG3QTR flipped to unlimited), ``detail='full'`` carried no
``description`` key, ``apply_dock_peek_recency`` had no ``include_completed``
kwarg, completed rows consumed recent slots, and ``selection`` had no
``completed_excluded`` field. Tests that PASS at baseline by design are marked
as invariant guards in their docstrings. The U6 document-order tests are
mutation-sensitive against the U5 code (e560542): the plain inventory listed
completed rows with their ✓ marker there.

U3 ([ap:SSVIC9]) moved the direction pins: ``recent=N`` presents ascending
(oldest first) by default -- descending is an explicit ``sort_direction``
opt-in; the selection of the N newest is unchanged. The recency-order
assertions below lock the new behaviour and are mutation-sensitive against
the pre-U3 code (d6f5b38).
"""
from __future__ import annotations

import asyncio

import pytest
from workflowy_mcp import server
from workflowy_mcp import dock_peek_metrics as _dpm
from workflowy_mcp.mcp_api_registry import dispatch_operation, validate_params


# Resolved lazily so that, against pre-U5 code, each test fails on its own
# merit (TypeError / AttributeError / assertion) instead of the whole module
# erroring at import -- the per-test mutation check.
def apply_dock_peek_recency(*args, **kwargs):
    return _dpm.apply_dock_peek_recency(*args, **kwargs)


def describe_dock_peek_selection(*args, **kwargs):
    return _dpm.describe_dock_peek_selection(*args, **kwargs)


def _async_value(value):
    async def _value(*_args, **_kwargs):
        return value

    return _value()


LONG_NOTE = " ".join(
    f"Sentence {i} of the Dock's own description that keeps going well past any reasonable excerpt budget."
    for i in range(1, 6)
)


# ---------------------------------------------------------------------------
# Fixture: a Dock whose two NEWEST-modified direct children are completed, so
# a recency view that forgets to exclude them visibly returns the wrong set.
#   A (completed, mod 990) · B (mod 500) · C (completed, mod 980) ·
#   D (mod 450) · E (mod 400) · S (nested local Dock, mod 100)
#   BB under B (mod 970) · SB/SC under S (SC completed, mod 995)
# Created order is deliberately different from modified order.
# ---------------------------------------------------------------------------

def _cache(note: str = "The list."):
    return {
        "D0": {"id": "D0", "name": "TODO Dock #wfy-ns", "note": note},
        "A": {"id": "A", "name": "[ap:AAAAAA] Alpha done #topic-x", "parent_id": "D0", "priority": 1,
              "completed": True, "completedAt": 991, "createdAt": 900, "modifiedAt": 990},
        "B": {"id": "B", "name": "Bravo", "parent_id": "D0", "priority": 2,
              "createdAt": 100, "modifiedAt": 500},
        "C": {"id": "C", "name": "Charlie done", "parent_id": "D0", "priority": 3,
              "completed": True, "completedAt": 981, "createdAt": 850, "modifiedAt": 980},
        "D": {"id": "D", "name": "Delta", "parent_id": "D0", "priority": 4,
              "createdAt": 200, "modifiedAt": 450},
        "E": {"id": "E", "name": "Echo", "parent_id": "D0", "priority": 5,
              "createdAt": 300, "modifiedAt": 400},
        "S": {"id": "S", "name": "Sierra Dock #wfy-ns--local", "parent_id": "D0", "priority": 6,
              "createdAt": 50, "modifiedAt": 100},
        "BB": {"id": "BB", "name": "Bravo child", "parent_id": "B", "priority": 1,
               "createdAt": 60, "modifiedAt": 970},
        "SB": {"id": "SB", "name": "Sierra baby", "parent_id": "S", "priority": 1,
               "createdAt": 70, "modifiedAt": 300},
        "SC": {"id": "SC", "name": "Sierra completed", "parent_id": "S", "priority": 2,
               "completed": True, "completedAt": 996, "createdAt": 80, "modifiedAt": 995},
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


def _row(rows, uuid):
    return next(row for row in rows if row["uuid"] == uuid)


# ---------------------------------------------------------------------------
# CF-MST-015 · the description-excerpt budget
# ---------------------------------------------------------------------------

def test_description_excerpt_is_budgeted_at_256_by_default():
    one_line = " ".join(LONG_NOTE.split())
    assert len(one_line) > 256
    out = server._render_dock_peek_raw_text(
        dock_name="D", root_note=LONG_NOTE, rows=[],
    )
    desc_line = out.splitlines()[1]
    assert desc_line == "> " + server._middle_ellipsis(one_line, 256)
    assert len(desc_line) == 2 + 256
    assert "…" in desc_line


def test_description_budget_reaches_the_impl_and_dispatch(monkeypatch):
    _setup(monkeypatch, cache=_cache(note=LONG_NOTE))
    one_line = " ".join(LONG_NOTE.split())
    for out in (_peek(), _dispatch()):
        desc_line = out.splitlines()[1]
        assert desc_line == "> " + server._middle_ellipsis(one_line, 256)
        assert len(desc_line) == 258


def test_description_budget_is_independent_of_the_name_budget():
    one_line = " ".join(LONG_NOTE.split())
    long_name = "N" * 400 + " #topic-x"
    rows = [
        {
            "uuid": "11111111-0000-0000-0000-000000000000",
            "ap_id": None,
            "name": long_name,
            "is_dock": False,
            "is_local_dock": False,
            "child_count": 0,
        }
    ]
    # Default: the NAME stays unlimited (WG3QTR) while the description budgets.
    out = server._render_dock_peek_raw_text(dock_name="D", root_note=LONG_NOTE, rows=rows)
    assert long_name in out
    assert out.splitlines()[1] == "> " + server._middle_ellipsis(one_line, 256)
    # Explicit name budget: names elide to it; the description keeps its own 256.
    out = server._render_dock_peek_raw_text(
        dock_name="D", root_note=LONG_NOTE, rows=rows, max_chars=100,
    )
    assert server._middle_ellipsis(long_name, 100) in out
    assert out.splitlines()[1] == "> " + server._middle_ellipsis(one_line, 256)


def test_short_description_under_budget_renders_verbatim():
    # INVARIANT GUARD (passes at baseline by design): a description within
    # the budget is untouched, and whitespace still collapses to one line.
    note = "MD_PATH: a/b.md\n---\nThe live   TODO list\nfor the project."
    out = server._render_dock_peek_raw_text(dock_name="D", root_note=note, rows=[])
    assert out.splitlines()[1] == "> The live TODO list for the project."


def test_show_description_false_still_suppresses_the_excerpt():
    # INVARIANT GUARD (passes at baseline by design).
    out = server._render_dock_peek_raw_text(
        dock_name="D", root_note=LONG_NOTE, rows=[], show_description=False,
    )
    assert not any(line.startswith(">") for line in out.splitlines())


def test_dock_description_excerpt_helper_strips_collapses_and_none_when_blank():
    helper = server._dock_peek_description_excerpt
    assert helper("MD_PATH: a/b.md\n---\nThe live   TODO list\nfor the project.") == (
        "The live TODO list for the project."
    )
    assert helper(LONG_NOTE) == " ".join(LONG_NOTE.split())
    assert helper("") is None
    assert helper("   \n\t ") is None
    assert helper(None) is None


def test_full_json_carries_the_untruncated_description(monkeypatch):
    _setup(monkeypatch, cache=_cache(note=LONG_NOTE))
    result = _peek(detail="full")
    assert result["description"] == " ".join(LONG_NOTE.split())
    assert len(result["description"]) > 256
    # A blank note is null, not an empty string.
    _setup(monkeypatch, cache=_cache(note=""))
    assert _peek(detail="full")["description"] is None


def test_max_name_chars_via_dispatch_still_governs_names_only(monkeypatch):
    _setup(monkeypatch, cache=_cache(note=LONG_NOTE))
    one_line = " ".join(LONG_NOTE.split())
    out = _dispatch(max_name_chars=10, show_counts=False)
    assert out.splitlines()[1] == "> " + server._middle_ellipsis(one_line, 256)
    # Row names keep the WG3QTR behaviour: elided only by the explicit knob.
    assert server._middle_ellipsis("Sierra Dock #wfy-ns--local", 10) in out
    assert "Sierra Dock #wfy-ns--local" not in out


# ---------------------------------------------------------------------------
# CF-MST-016 · completed children out of the recency view
# ---------------------------------------------------------------------------

def _policy_rows():
    return [
        {"uuid": "a", "completed": True, "created_at": 900, "modified_at": 990, "depth": 0},
        {"uuid": "b", "completed": False, "created_at": 100, "modified_at": 500, "depth": 0},
        {"uuid": "c", "completed": True, "created_at": 850, "modified_at": 980, "depth": 0},
        {"uuid": "d", "created_at": 200, "modified_at": 450, "depth": 0},  # no flag: NOT completed
        {"uuid": "e", "completed": False, "created_at": None, "modified_at": None, "depth": 0},
    ]


def test_pure_policy_excludes_completed_from_recency_by_default():
    rows, selection, sort = apply_dock_peek_recency(
        _policy_rows(), recent=2, recent_by=None, sort_by=None, sort_direction=None,
    )
    assert [row["uuid"] for row in rows] == ["d", "b"]
    assert selection == {
        "recent": 2, "recent_by": "modified", "candidates": 3, "selected": 2,
        "completed_excluded": 2,
    }
    assert sort == {"by": "modified", "direction": "asc"}


def test_pure_policy_include_completed_restores_the_pre_change_pool():
    rows, selection, _sort = apply_dock_peek_recency(
        _policy_rows(), recent=2, recent_by=None, sort_by=None, sort_direction=None,
        include_completed=True,
    )
    # Selected {a(990), c(980)}; U3 presents ascending: c then a.
    assert [row["uuid"] for row in rows] == ["c", "a"]
    assert selection["candidates"] == 5 and selection["completed_excluded"] == 0


def test_pure_policy_document_order_excludes_completed_by_default():
    # U6 (J11): the plain walk (no recency knobs) NOW filters completed rows
    # by default too -- the inventory no longer lists them.
    rows, selection, sort = apply_dock_peek_recency(
        _policy_rows(), recent=None, recent_by=None, sort_by=None, sort_direction=None,
    )
    assert [row["uuid"] for row in rows] == ["b", "d", "e"]
    assert selection["completed_excluded"] == 2 and selection["candidates"] == 3
    assert sort["by"] == "none"
    # Explicit sort_by='none' is document order, filtered by default as well.
    rows, selection, _ = apply_dock_peek_recency(
        _policy_rows(), recent=None, recent_by=None, sort_by="none", sort_direction=None,
    )
    assert [row["uuid"] for row in rows] == ["b", "d", "e"]
    assert selection["completed_excluded"] == 2
    # The opt-in restores the pre-U6 inventory exactly.
    rows, selection, _ = apply_dock_peek_recency(
        _policy_rows(), recent=None, recent_by=None, sort_by=None, sort_direction=None,
        include_completed=True,
    )
    assert [row["uuid"] for row in rows] == ["a", "b", "c", "d", "e"]
    assert selection["completed_excluded"] == 0 and selection["candidates"] == 5


def test_pure_policy_document_order_describe_phrase_names_hidden_count():
    # U6: the plain path has no selection/order phrase, but the header must
    # never silently omit the hidden rows (U5's J8 honesty principle).
    _rows, selection, sort = apply_dock_peek_recency(
        _policy_rows(), recent=None, recent_by=None, sort_by=None, sort_direction=None,
    )
    assert describe_dock_peek_selection(selection, sort, recursive=False) == " · 2 completed hidden"
    _rows, selection, sort = apply_dock_peek_recency(
        _policy_rows(), recent=None, recent_by=None, sort_by=None, sort_direction=None,
        include_completed=True,
    )
    assert describe_dock_peek_selection(selection, sort, recursive=False) == ""


def test_pure_policy_timestamp_sort_and_recent_with_none_sort_exclude_completed():
    # Bare timestamp sort = a recency-driven view: completed out by default.
    rows, selection, _ = apply_dock_peek_recency(
        _policy_rows(), recent=None, recent_by=None, sort_by="modified", sort_direction="desc",
    )
    assert [row["uuid"] for row in rows] == ["b", "d", "e"]
    assert selection["completed_excluded"] == 2
    # recent=N presented in document order is STILL a recency selection.
    rows, selection, _ = apply_dock_peek_recency(
        _policy_rows(), recent=3, recent_by=None, sort_by="none", sort_direction=None,
    )
    assert sorted(row["uuid"] for row in rows) == ["b", "d", "e"]
    assert selection["completed_excluded"] == 2


def test_pure_policy_does_not_mutate_inputs():
    source = _policy_rows()
    before = [dict(row) for row in source]
    apply_dock_peek_recency(
        source, recent=2, recent_by=None, sort_by=None, sort_direction=None,
    )
    assert source == before


def test_recent_excludes_completed_by_default_and_never_gives_them_slots(monkeypatch):
    _setup(monkeypatch)
    result = _peek(detail="full", recent=2)
    # Selected {B(500), D(450)}; U3 presents ascending: D then B.
    assert [row["uuid"] for row in result["rows"]] == ["D", "B"]
    assert result["selection"] == {
        "recent": 2, "recent_by": "modified", "candidates": 4, "selected": 2,
        "completed_excluded": 2,
    }
    # root_metrics still cover the FULL subtree, completed included.
    assert result["root_metrics"]["child_count"] == 6


def test_include_completed_true_lets_completed_consume_slots(monkeypatch):
    _setup(monkeypatch)
    result = _peek(detail="full", recent=2, include_completed=True)
    # Selected {A(990), C(980)}; U3 presents ascending: C then A.
    assert [row["uuid"] for row in result["rows"]] == ["C", "A"]
    assert _row(result["rows"], "A")["completed"] is True
    assert result["selection"]["completed_excluded"] == 0
    assert result["selection"]["candidates"] == 6


def test_recent_by_created_excludes_completed_too(monkeypatch):
    _setup(monkeypatch)
    # Newest created overall are A(900)/C(850), both completed; the eligible
    # newest-created pair is E(300)/D(200); U3 presents ascending: D then E.
    result = _peek(detail="full", recent=2, recent_by="created")
    assert [row["uuid"] for row in result["rows"]] == ["D", "E"]
    assert result["selection"]["recent_by"] == "created"
    assert result["selection"]["completed_excluded"] == 2


def test_timestamp_sort_excludes_completed_by_default(monkeypatch):
    _setup(monkeypatch)
    result = _peek(detail="full", sort_by="modified", sort_direction="desc")
    assert [row["uuid"] for row in result["rows"]] == ["B", "D", "E", "S"]
    assert result["selection"]["completed_excluded"] == 2
    included = _peek(detail="full", sort_by="modified", sort_direction="desc",
                     include_completed=True)
    assert [row["uuid"] for row in included["rows"]] == ["A", "C", "B", "D", "E", "S"]
    assert included["selection"]["completed_excluded"] == 0


def test_recursive_recent_excludes_completed_descendants_from_the_pool(monkeypatch):
    _setup(monkeypatch)
    # Walked (stop_at_child_anchors): A B BB C D E S -- SB/SC sit behind the
    # nested Dock and are not candidates. Completed walked: A, C.
    # Selected {BB(970), B(500), D(450)}; U3 presents ascending.
    result = _peek(detail="full", recursive=True, recent=3)
    assert [row["uuid"] for row in result["rows"]] == ["D", "B", "BB"]
    assert result["selection"]["candidates"] == 5
    assert result["selection"]["completed_excluded"] == 2
    # descend_policy='all' admits SC (completed, mod 995) to the pool too --
    # still excluded by default.
    result = _peek(detail="full", recursive=True, descend_policy="all", recent=3)
    assert [row["uuid"] for row in result["rows"]] == ["D", "B", "BB"]
    assert result["selection"]["candidates"] == 6  # + SB
    assert result["selection"]["completed_excluded"] == 3  # + SC


def test_document_order_inventory_hides_completed_by_default_and_opt_in_lists_with_marker(monkeypatch):
    # U6 (J11, Dan's ruling): the plain inventory hides completed rows by
    # default; include_completed=true lists them with their ✓ marker (the
    # pre-U6 behavior this test's predecessor locked).
    _setup(monkeypatch)
    result = _peek(detail="full")
    assert [row["uuid"] for row in result["rows"]] == ["B", "D", "E", "S"]
    assert all(not row["completed"] for row in result["rows"])
    out = _peek(show_counts=False)
    body = _body(out)
    assert not any(line.rstrip().endswith("✓") for line in body)
    # Opt-in: completed rows return, ✓ markers shown.
    result = _peek(detail="full", include_completed=True)
    assert [row["uuid"] for row in result["rows"]] == ["A", "B", "C", "D", "E", "S"]
    assert _row(result["rows"], "A")["completed"] is True
    out = _peek(show_counts=False, include_completed=True)
    body = _body(out)
    assert any(line.startswith("[ap:AAAAAA]") and line.rstrip().endswith("✓") for line in body)
    assert any("Charlie done" in line and line.rstrip().endswith("✓") for line in body)


def test_document_order_selection_counts_completed_excluded(monkeypatch):
    _setup(monkeypatch)
    result = _peek(detail="full")
    assert result["selection"] == {
        "recent": None, "recent_by": None, "candidates": 4, "selected": 4,
        "completed_excluded": 2,
    }
    result = _peek(detail="full", include_completed=True)
    assert result["selection"] == {
        "recent": None, "recent_by": None, "candidates": 6, "selected": 6,
        "completed_excluded": 0,
    }


def test_document_order_compact_and_full_select_identical_items(monkeypatch):
    # The one policy feeds BOTH renderers on the plain path too.
    _setup(monkeypatch)
    full = _peek(detail="full")
    compact = _peek(show_counts=False)
    names = [line.strip() for line in _body(compact)]
    # [ap:0M6VP8] peek half: AP-less rows carry their short UUID handle.
    assert names == ["[B] Bravo", "[D] Delta", "[E] Echo", "[S] Sierra Dock #wfy-ns--local ⚓ Ⓛ"]
    assert [row["uuid"] for row in full["rows"]] == ["B", "D", "E", "S"]


def test_recursive_document_order_excludes_completed_descendants(monkeypatch):
    # The filter consumes the walked-row flag under recursion as well:
    # completed descendants behind descend_policy='all' leave the pool.
    _setup(monkeypatch)
    result = _peek(detail="full", recursive=True, descend_policy="all")
    assert [row["uuid"] for row in result["rows"]] == ["B", "BB", "D", "E", "S", "SB"]
    assert result["selection"]["completed_excluded"] == 3  # A, C, SC
    # Under the default boundary the nested Dock S is not entered: only the
    # two direct completed rows are excluded.
    result = _peek(detail="full", recursive=True)
    assert [row["uuid"] for row in result["rows"]] == ["B", "BB", "D", "E", "S"]
    assert result["selection"]["completed_excluded"] == 2
    # root_metrics still count the FULL subtree, completed included.
    assert result["root_metrics"]["descendant_count"] == 9


def test_compact_and_full_select_identical_items_under_completed_exclusion(monkeypatch):
    _setup(monkeypatch)
    full = _peek(detail="full", recent=3)
    compact = _peek(recent=3, show_counts=False)
    names = [line.strip() for line in _body(compact)]
    # Selected {B(500), D(450), E(400)}; U3 presents ascending.
    assert names == ["[E] Echo", "[D] Delta", "[B] Bravo"]
    assert [row["uuid"] for row in full["rows"]] == ["E", "D", "B"]


def test_header_names_the_hidden_completed_count(monkeypatch):
    _setup(monkeypatch)
    out = _peek(recent=2, show_counts=False)
    header = out.splitlines()[0]
    assert "2 most recent by modified of 4 children (oldest first)" in header
    assert "2 completed hidden" in header
    # include_completed=true: nothing hidden, no phrase.
    out = _peek(recent=2, include_completed=True, show_counts=False)
    header = out.splitlines()[0]
    assert "of 6 children" in header and "completed hidden" not in header
    # Document order: the header names the hidden count there too (U6)...
    assert "2 completed hidden" in _peek(show_counts=False).splitlines()[0]
    # ...and include_completed=true on the plain inventory shows them, no phrase.
    assert "completed hidden" not in _peek(show_counts=False, include_completed=True).splitlines()[0]
    # Timestamp sort header carries the phrase too.
    out = _peek(sort_by="modified", sort_direction="desc", show_counts=False)
    assert "sorted by modified (newest first) · 2 completed hidden" in out.splitlines()[0]


def test_include_completed_validation_is_loud(monkeypatch):
    _setup(monkeypatch)
    with pytest.raises(server._AnchorOperationError) as exc:
        _peek(recent=2, include_completed="yes")
    assert exc.value.code == "invalid_parameter_type"
    rejected = _dispatch(recent=2, include_completed="yes")
    assert rejected["error"]["code"] == "invalid_parameter_type"


def test_registry_declares_include_completed_and_describe_teaches_both_changes():
    registry = server.build_anchor_registry()
    spec = registry.resolve("dock.peek")
    full = registry.describe_full("dock.peek")
    props = full["params_schema"]["properties"]
    assert props["include_completed"]["type"] == "boolean"
    assert props["include_completed"]["default"] is False
    assert "completed" in props["include_completed"]["description"]
    assert "document-order" in props["include_completed"]["description"]
    # U6 (J11) teaching: the flag now covers EVERY view, the plain
    # document-order inventory included; the old "ALWAYS lists ... whatever
    # this flag says" no-op documentation is gone.
    assert "EVERY view" in props["include_completed"]["description"]
    assert "ALWAYS lists" not in props["include_completed"]["description"]
    assert "EVERY view" in spec.summary
    # CF-MST-015 teaching: the 256-char description budget and the
    # untruncated full-JSON escape hatch.
    assert "256" in props["show_description"]["description"]
    assert "256" in props["max_name_chars"]["description"]
    assert "unlimited" in props["max_name_chars"]["description"]
    returns = full["returns"]["description"]
    assert "description" in returns and "256" in returns
    assert "completed_excluded" in returns
    assert "include_completed" in returns
    assert "include_completed" in spec.summary
    for example in full["examples"]:
        validate_params(spec, example["call"]["params"])
    assert any(
        example["call"]["params"].get("include_completed") is True
        for example in full["examples"]
    )


def test_family_summary_teaches_the_completed_default():
    description = server._ANCHOR_TOOL_DESCRIPTION
    assert "hide completed" in description
    assert "include_completed=true" in description
    # U6: the family summary teaches that the plain inventory hides them too.
    assert "plain inventory included" in description
