"""[ap:TVIIBN] 4e — the recency header line and the rule's describe texts.

Unit 4d(a) shipped the RULE (``'last N'``/``'newest N'`` SELECT the N newest
by ``recent_by``, ``'bottom N'`` is the positional tail, ``sort_direction``
without a timestamp ``sort_by`` is a loud error). Unit 4e ships the
EXPLANATION: one explicit rule line on every agent-facing recency render,
plus describe texts that teach the rule everywhere it applies.

* The render-verb header lives in the ONE render shaper
  (``server._shape_glimpse_family_return``), composed by the ONE composer
  ``server._recency_header_sentence``: Markdown renders open with
  ``> Recency selection: …``; the structured full/UUID twin carries the same
  sentence as a top-level ``recency_header`` key. The body passes through the
  ONE empty-output stub (``stub_empty_text_result``, unit 9a) BEFORE the
  header is prepended (sequencing caveat C1), so an empty window reads as the
  header plus the literal ``(empty)`` — never a header hiding a blank body.
* The dock.peek twin (``describe_dock_peek_recency_rule``) renders ONE rule
  line directly below the peek header when ``recent=N`` is active; a plain
  inventory peek is byte-identical to before.

All local: no network, no real Workflowy, no disk writes.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.dock_peek_metrics import (  # noqa: E402
    describe_dock_peek_recency_rule,
)


_NEWEST_APPLIED = {
    "form": "item_count",
    "count": 5,
    "selection": "newest",
    "basis": "created",
}


def _markdown_result(since_applied, *, body="# Hi\n\nbody", sort_direction=None):
    result = {
        "success": True,
        "node_count": 3,
        "preview_markdown": body,
        "since_applied": since_applied,
        "render_scope": {"kind": "raw_root", "sort_direction": sort_direction},
    }
    return result


# ---------------------------------------------------------------------------
# The ONE composer + the shaper's Markdown branch.
# ---------------------------------------------------------------------------
def test_markdown_newest_selection_opens_with_the_rule_line():
    out = srv._shape_glimpse_family_return(
        _markdown_result(_NEWEST_APPLIED), output_file=None
    )
    assert out.startswith(
        "> Recency selection: the 5 newest by created; "
        "sort_direction only orders these 5 (asc = oldest first); "
        "it never changes which items were chosen.\n"
    )
    assert out.endswith("# Hi\n\nbody")


def test_markdown_bottom_selection_names_the_positional_tail():
    out = srv._shape_glimpse_family_return(
        _markdown_result({"form": "item_count", "count": 3, "selection": "bottom"}),
        output_file=None,
    )
    first = out.splitlines()[0]
    assert first.startswith("> Positional selection ('bottom 3'):")
    assert "direction-independent selection is 'last N'/'newest N'" in first


def test_markdown_window_selection_says_measured_from_scope_not_clock():
    out = srv._shape_glimpse_family_return(
        _markdown_result({"form": "window", "pivot_ts": 123}), output_file=None
    )
    first = out.splitlines()[0]
    assert "Recency window" in first
    assert "NEWEST ITEM IN SCOPE (not the clock)" in first
    assert "it never changes which items were chosen" in first


def test_markdown_curated_selection_names_the_cursor():
    out = srv._shape_glimpse_family_return(
        _markdown_result({"form": "curated", "pivot_ts": 42}), output_file=None
    )
    first = out.splitlines()[0]
    assert "Curation selection ('curated')" in first
    assert "curated cursor" in first


def test_markdown_desc_direction_flips_the_direction_note():
    out = srv._shape_glimpse_family_return(
        _markdown_result(_NEWEST_APPLIED, sort_direction="desc"), output_file=None
    )
    assert "(desc = newest first)" in out.splitlines()[0]
    assert "(asc = oldest first)" not in out.splitlines()[0]


def test_no_recency_selection_is_byte_identical():
    body = "# Hi\n\nbody"
    out = srv._shape_glimpse_family_return(
        {"success": True, "node_count": 3, "preview_markdown": body},
        output_file=None,
    )
    assert out == body


def test_header_sits_above_the_empty_stub():
    # Sequencing caveat C1: the body passes through 9a's stub helper BEFORE
    # the header is prepended, so an empty window is the header PLUS the
    # literal '(empty)' -- never a header hiding a blank body.
    out = srv._shape_glimpse_family_return(
        _markdown_result(_NEWEST_APPLIED, body=""), output_file=None
    )
    lines = out.splitlines()
    assert lines[0].startswith("> Recency selection:")
    assert lines[1] == "(empty)"


def test_structured_mode_carries_recency_header_key():
    out = srv._shape_glimpse_family_return(
        {
            "success": True,
            "node_count": 3,
            "preview_tree": ["[WG1] Root"],
            "since_applied": _NEWEST_APPLIED,
            "render_scope": {"kind": "raw_root", "sort_direction": "desc"},
            "rows": [1],
        },
        output_file=None,
        headings_only=False,
    )
    assert isinstance(out, dict)
    assert out["recency_header"] == (
        "Recency selection: the 5 newest by created; "
        "sort_direction only orders these 5 (desc = newest first); "
        "it never changes which items were chosen."
    )
    assert "rows" not in out
    assert out["preview_tree"] == ["[WG1] Root"]


def test_structured_mode_without_recency_has_no_header_key():
    out = srv._shape_glimpse_family_return(
        {"success": True, "node_count": 3, "preview_tree": ["[WG1]"], "rows": []},
        output_file=None,
    )
    assert isinstance(out, dict)
    assert "recency_header" not in out


def test_output_file_result_untouched_even_with_a_selection():
    payload = {
        "success": True,
        "output_file": "/tmp/x.md",
        "bytes_written": 12,
        "since_applied": _NEWEST_APPLIED,
    }
    out = srv._shape_glimpse_family_return(payload, output_file="/tmp/x.md")
    assert out is payload


def test_preview_text_gather_is_never_headered():
    # gather (the only preview_text producer) never applies a recency
    # selection; defensively, a raw-text outline stays byte-identical.
    out = srv._shape_glimpse_family_return(
        {"success": True, "preview_text": "#a\n#b", "since_applied": _NEWEST_APPLIED},
        output_file=None,
    )
    assert out == "#a\n#b"


def test_stale_focus_still_loud_and_headerless():
    stale = {
        "success": True,
        "focus_unresolved": "dead-uuid",
        "node_count": 0,
        "depth": 0,
        "error": "The requested focus row could not be resolved.",
        "preview_markdown": "",
        "rows": [],
        "since_applied": _NEWEST_APPLIED,
    }
    out = srv._shape_glimpse_family_return(stale, output_file=None)
    assert isinstance(out, dict)
    assert out["success"] is False
    assert out["focus_unresolved"] == "dead-uuid"
    assert "recency_header" not in out


# ---------------------------------------------------------------------------
# The dock.peek twin: ONE rule line below the header, only when recent is on.
# ---------------------------------------------------------------------------
def _peek(**options):
    return srv._render_dock_peek_raw_text(
        dock_name="Dock",
        root_note="",
        rows=[
            {"uuid": "aaaaaaaa-1111-4111-8111-111111111111", "ap_id": None,
             "name": "Row one", "depth": 0},
        ],
        show_counts=False,
        **options,
    )


def test_peek_recent_renders_the_rule_line_below_the_header():
    out = _peek(
        selection={
            "recent": 20, "recent_by": "modified", "candidates": 149,
            "selected": 20, "completed_excluded": 0,
        },
        sort={"by": "none", "direction": "asc"},
    )
    lines = out.splitlines()
    assert "most recent by modified" in lines[0]  # the existing header phrase
    assert lines[1] == (
        "Recency selection: the 20 newest by modified; "
        "sort_direction only orders these 20 (asc = oldest first); "
        "it never changes which rows were chosen."
    )
    # The body split (blank-line separated) is untouched by the new line.
    body = out.split("\n\n", 1)[1]
    assert "Row one" in body


def test_peek_desc_direction_note():
    out = _peek(
        selection={
            "recent": 2, "recent_by": "created", "candidates": 5,
            "selected": 2, "completed_excluded": 0,
        },
        sort={"by": "created", "direction": "desc"},
    )
    assert "(desc = newest first)" in out.splitlines()[1]


def test_peek_plain_inventory_has_no_rule_line():
    out = _peek()
    assert "Recency selection:" not in out
    assert "sort_direction only orders" not in out


def test_peek_rule_line_is_describe_dock_peek_recency_rule():
    # The peek-side composer is the ONE peek wording authority (its twin on
    # the render verbs is _recency_header_sentence).
    line = describe_dock_peek_recency_rule(
        {"selected": 7, "recent_by": "created"}, {"direction": "asc"}
    )
    assert line == (
        "Recency selection: the 7 newest by created; "
        "sort_direction only orders these 7 (asc = oldest first); "
        "it never changes which rows were chosen."
    )


# ---------------------------------------------------------------------------
# The describe texts: the rule is taught everywhere it applies.
# ---------------------------------------------------------------------------
def test_recent_token_description_teaches_the_rule():
    text = srv._RECENT_TOKEN_DESCRIPTION
    assert "'bottom N'" in text
    assert "N NEWEST leaves by recent_by" in text
    assert "never changes which items were chosen" in text
    assert "newest item in scope, not the clock" in text
    assert "needs no sort" not in text  # the old positional teaching is gone
    assert "leaves of the final render order" not in text


def test_sort_direction_description_is_the_shared_rule():
    text = srv._SORT_DIRECTION_DESCRIPTION
    assert "sort_direction_without_sort_by" in text
    assert "NEVER changes which items a recency selection (recent) chose" in text
    assert "saved default sort would supply one" in text  # the CF-26 gotcha
    # It rides the shared fragments: glimpse/lightning/estate via
    # _render_view_properties, slice_group via common_view_properties, and
    # dock.render's inline schema.
    props = srv._render_view_properties()
    assert props["sort_direction"]["description"] is text
    assert props["sort_by"]["description"] is srv._SORT_BY_DESCRIPTION
    anchor_registry = srv.build_anchor_registry()
    dock_render = anchor_registry.resolve("dock.render")
    assert (
        dock_render.params_schema["properties"]["sort_direction"]["description"]
        is text
    )
    slice_registry = srv.build_slice_group_registry()
    for verb in ("render", "sift", "merge.render"):
        assert (
            slice_registry.resolve(verb).params_schema["properties"][
                "sort_direction"
            ]["description"]
            is text
        ), verb
    lightning_registry = srv.build_lightning_rod_registry()
    for verb in ("render_tags", "render_search", "render_symbols", "render_nodes"):
        assert (
            lightning_registry.resolve(verb).params_schema["properties"][
                "sort_direction"
            ]["description"]
            is text
        ), verb
    # gather has no sort_direction property at all (the guard is inert there).
    assert "sort_direction" not in lightning_registry.resolve("gather").params_schema["properties"]


def test_peek_keeps_its_own_sort_direction_text():
    # peek's integer vocabulary is different BY DESIGN (a peek may order by
    # recent_by without a sort_by); the render-verb rule text must not leak.
    anchor_registry = srv.build_anchor_registry()
    text = anchor_registry.resolve("dock.peek").params_schema["properties"][
        "sort_direction"
    ]["description"]
    assert "asc = oldest first (the default EVERYWHERE" in text
    assert "sort_direction_without_sort_by" not in text


def test_dock_render_summary_teaches_selection_vs_direction():
    anchor_registry = srv.build_anchor_registry()
    summary = anchor_registry.resolve("dock.render").summary
    assert "'bottom N'" in summary
    assert "never changes what was selected" in summary
    # A pinned clause from the dispatch-era tests must survive.
    assert "suppress ancestor notes" in summary


def test_glimpse_run_returns_teaches_the_rule():
    returns = srv.build_glimpse_registry().resolve("run").returns["description"]
    assert "'bottom N' (the positional tail of the current order)" in returns
    assert "never changes which items were chosen" in returns
    assert "tail accepts" not in returns  # pinned negative from 4d's era
    assert "ANY recent selection" in returns  # the ancestor-notes correction


def test_estate_returns_teach_the_loud_direction_guard():
    returns = srv.build_glimpse_registry().resolve("estate").returns["description"]
    assert "sort_direction_without_sort_by error" in returns
    assert "recent / recent_by" in returns


def test_ancestor_notes_description_covers_every_selection():
    text = srv._ANCESTOR_NOTES_DESCRIPTION
    assert "'last N'/'newest N'/'bottom N'" in text
    assert "'last N' tail alone" not in text  # the CF-13-stale sentence is gone
    assert "no sort and no recent selection keep ancestor notes" in text


def test_slice_group_render_summary_teaches_the_rule():
    summary = srv.build_slice_group_registry().resolve("render").summary
    assert "'bottom N' keeps the positional tail" in summary
    assert "never changes what was selected" in summary
