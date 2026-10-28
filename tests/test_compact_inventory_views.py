"""Pure serializer/policy locks for the compact inventory views.

The compact inventory views (output-side progressive disclosure) turn the
three INVENTORY operations -- ``anchor.dock.list``, ``anchor.dock.peek``, and
``slice_group.list`` -- into raw-text serializers under ``detail='compact'``
(the consolidated-registry default), with ``detail='full'`` returning the
established structured dictionaries byte-for-byte.

These tests lock the PURE half of the feature, end to end without any
transport: the shared boundary normalizer and display primitives, the legacy
widget serializer's byte-for-byte compatibility, the Dock-list and Dock-peek
serializers, and the slice-group nested-display builder (the exact Python
twin of the client's ``buildNestedDisplayItems``).

All tests here are pure object-graph calls into ``workflowy_mcp.server``,
``workflowy_mcp.client.api_client_nexus``, and
``workflowy_mcp.f8_slice_policy``.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

import workflowy_mcp.server as srv  # noqa: E402
from workflowy_mcp.f8_slice_policy import build_slice_group_display_items  # noqa: E402
from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    _middle_ellipsis,
    _render_anchor_tree_raw_text,
    _short_handle_prefix,
)


# ---------------------------------------------------------------------------
# Shared boundary normalizer + display primitives
# ---------------------------------------------------------------------------


def test_compact_is_the_default_at_every_surface():
    """Dan, 2026: compact is THE default everywhere (single-user system).

    Locks the default flip at every level: the three impls default
    ``detail='compact'`` (the consolidated registry handlers also default
    compact, locked by the family dispatch suites), and the legacy
    ``list_anchors`` / ``list_slice_groups`` delegates flip too.
    """
    import inspect as _inspect

    for fn in (
        srv._anchor_dock_list_impl,
        srv._anchor_dock_peek_impl,
        srv._slice_group_list_impl,
    ):
        assert _inspect.signature(fn).parameters["detail"].default == "compact"


def test_normalize_inventory_view_detail_enum_and_budget_rules():
    # WG3QTR (Dan, 2026-09-16): the default budget is UNLIMITED (0); the
    # middle-ellipsis is an explicit opt-in, never on by default.
    assert srv._normalize_inventory_view("compact", None) == ("compact", 0, None)
    assert srv._normalize_inventory_view("full", 0) == ("full", 0, None)
    assert srv._normalize_inventory_view("COMPACT", None)[2] is not None
    assert srv._normalize_inventory_view(None, None)[2] is not None
    assert srv._normalize_inventory_view("compact", -1)[2][0] == "invalid_parameter_combination"
    assert srv._normalize_inventory_view("compact", True)[2][0] == "invalid_parameter_type"
    assert srv._normalize_inventory_view("compact", 40.0) == ("compact", 40, None)


def test_short_handle_prefix_is_resolver_compatible_form():
    # Node/Dock handles: first eight normalized UUID hex characters.
    assert srv._short_handle_prefix("1126f2b0-aaaa-bbbb-cccc-dddddddddddd") == "1126f2b0"
    # Slice-group ids: the 'grp-' key prefix is stripped, leaving bare hex.
    assert srv._short_handle_prefix("grp-2577886fabcdef", key_prefix="grp-") == "2577886f"


def test_middle_ellipsis_6040_split_budgets_and_unbounded():
    assert srv._middle_ellipsis("short", 256) == "short"
    assert srv._middle_ellipsis("anything at all", 0) == "anything at all"
    assert srv._middle_ellipsis("anything at all", None) == "anything at all"
    assert srv._middle_ellipsis("abcdef", 1) == "…"
    text = "x" * 100
    out = srv._middle_ellipsis(text, 11)
    # Exactly 11 characters: 60/40 split of the 10-char budget around one '…'.
    assert len(out) == 11
    assert out == "xxxxxx…xxxx"
    assert out.count("…") == 1


def test_middle_ellipsis_never_bisects_an_emoji_component():
    # A recognized emoji component (e.g. a variation-selector pair or a
    # supplementary-plane glyph) must never be bisected: a head/tail cut that
    # lands mid-component moves the boundary OFF the component, so neither the
    # head nor the tail ends/starts with a detached combining/variation char.
    emoji = "\u2705\ufe0f"  # ✅ = U+2705 U+FE0F (a VS16-fallback pair)
    s = "x" * 10 + emoji + "y" * 10
    out = srv._middle_ellipsis(s, 11)
    assert out.count("…") == 1
    # No detached variation selector on either side of the ellipsis.
    assert "\ufe0f…" not in out
    assert "…\ufe0f" not in out
    # The component is dropped whole from the truncated head (never split).
    assert "\u2705…" not in out or out.split("…")[0].endswith("\u2705")


def test_middle_ellipsis_tail_never_begins_with_a_combining_char():
    # The tail cut must also move off a straddling component (never begin the
    # tail with a detached variation selector).
    emoji = "\u2705\ufe0f"
    s = "x" * 10 + emoji + "y" * 10
    out = srv._middle_ellipsis(s, 11)
    tail_part = out.split("…", 1)[1]
    assert not tail_part.startswith("\ufe0f")


# ---------------------------------------------------------------------------
# anchor.dock.list -- the compact tree serializer (and the legacy byte lock)
# ---------------------------------------------------------------------------


def _anchor_tree():
    return {
        "root_uuid": "ROOT",
        "root_name": "Root",
        "children": [
            {
                "id": "1126f2b0-aaaa-bbbb-cccc-dddddddddddd",
                "name": "System Architecture",
                "is_local": False,
                "children": [
                    {
                        "id": "0fd089ea-1111-2222-3333-444444444444",
                        "name": "NEXUS architecture",
                        "is_local": False,
                        "children": [],
                    }
                ],
            },
            {
                "id": "77a69aa8-5555-6666-7777-888888888888",
                "name": "Local sub-dock",
                "is_local": True,
                "children": [],
            },
        ],
    }


def test_anchor_tree_legacy_render_stays_byte_identical():
    tree = _anchor_tree()
    expected = (
        "Anchors (5 global; 2 local shown)\n"
        "UUID                                  Anchor\n"
        "------------------------------------  ------\n"
        "1126f2b0-aaaa-bbbb-cccc-dddddddddddd  System Architecture\n"
        "\n"
        "0fd089ea-1111-2222-3333-444444444444    NEXUS architecture\n"
        "\n"
        "77a69aa8-5555-6666-7777-888888888888  Local sub-dock  [local]\n"
    )
    assert srv._render_anchor_tree_raw_text(
        tree, global_count=5, local_count=2, show_local=True
    ) == expected


def test_anchor_tree_compact_geometry_local_markers_and_header_counts():
    tree = _anchor_tree()
    shown = srv._render_anchor_tree_raw_text(
        tree,
        global_count=1,
        local_count=1,
        show_local=True,
        compact=True,
        max_chars=256,
    )
    lines = shown.splitlines()
    assert lines[0] == (
        "Anchor Docks — 2 shown (1 global · 1 local) · [xxxxxxxx] = 8-char "
        "handle prefix · detail='full' for JSON"
    )
    assert lines[2] == "[1126f2b0] System Architecture"
    # UUID column NEVER indents; only the Dock name indents by Dock ancestry.
    assert lines[3] == "[0fd089ea]   NEXUS architecture"
    assert lines[4] == "[77a69aa8] Local sub-dock Ⓛ"

    hidden = srv._render_anchor_tree_raw_text(
        tree,
        global_count=1,
        local_count=3,
        show_local=False,
        compact=True,
        max_chars=256,
    )
    assert hidden.splitlines()[0] == (
        "Anchor Docks — 1 global · 3 local hidden (include_local=true to show) "
        "· [xxxxxxxx] = 8-char handle prefix · detail='full' for JSON"
    )


# ---------------------------------------------------------------------------
# anchor.dock.peek -- the compact direct-child serializer
# ---------------------------------------------------------------------------


def test_peek_compact_global_ap_padding_blank_rows_and_suffixes():
    out = srv._render_dock_peek_raw_text(
        dock_name="TODO",
        root_note="",
        rows=[
            {
                "uuid": "11111111-0000-0000-0000-000000000000",
                "ap_id": "[ap:LONGERCORE9]",
                "name": "Decorated task",
                "is_dock": False,
                "is_local_dock": False,
                "child_count": 0,
            },
            {
                "uuid": "22222222-0000-0000-0000-000000000000",
                "ap_id": "[ap:S]",
                "name": "Sub Dock",
                "is_dock": True,
                "is_local_dock": False,
                "child_count": 4,
            },
            {
                "uuid": "33333333-0000-0000-0000-000000000000",
                "ap_id": None,
                "name": "Local child Dock",
                "is_dock": True,
                "is_local_dock": True,
                "child_count": 0,
            },
        ],
    )
    lines = out.splitlines()
    assert lines[0] == (
        "TODO — 3 children · AP-IDs aligned left · [xxxxxxxx] short UUID handle on rows "
        "without an AP-ID · detail='full' for JSON"
    )
    body = out.split("\n\n", 1)[1].splitlines()
    # Variable-length AP cores pad to the widest displayed id string.
    # These serializer-only fixtures omit new metrics: unknown is explicit.
    assert body[0] == "[ap:LONGERCORE9] Decorated task ▸0 · desc:? · self:? · tree:?"
    assert body[1] == "[ap:S]" + " " * 11 + "Sub Dock ▸4 · desc:? · self:? · tree:? ⚓"
    # [ap:0M6VP8] peek half: the AP-less row carries the short UUID handle
    # by default, so it is never handle-less.
    assert body[2] == " " * 17 + "[33333333] Local child Dock ▸0 · desc:? · self:? · tree:? ⚓ Ⓛ"


def test_peek_compact_omits_ap_column_when_no_points_have_ap_ids():
    out = srv._render_dock_peek_raw_text(
        dock_name="Empty Dock",
        root_note="",
        rows=[
            {
                "uuid": "44444444-0000-0000-0000-000000000000",
                "ap_id": None,
                "name": "Plain child",
                "is_dock": False,
                "is_local_dock": False,
                "child_count": 1,
            }
        ],
    )
    body = out.split("\n\n", 1)[1].splitlines()
    # No APs means width zero: no phantom AP column or separator -- but an
    # AP-less row is never handle-less ([ap:0M6VP8] peek half): the short
    # UUID handle shows by default.
    assert body == ["[44444444] Plain child ▸1 · desc:? · self:? · tree:?"]


def test_peek_compact_ap_shows_once_and_emoji_before_ap_survives():
    # '_split_leading_ap_handle' reuse: the AP token is removed from the
    # displayed name (it already sits in the ID column) while the leading
    # emoji decoration and the ordinary title are rejoined with ONE space.
    out = srv._render_dock_peek_raw_text(
        dock_name="D",
        root_note="",
        rows=[
            {
                "uuid": "55555555-0000-0000-0000-000000000000",
                "ap_id": "[ap:ABC123]",
                "name": "🔱 [ap:ABC123] Decorated task",
                "is_dock": False,
                "is_local_dock": False,
                "child_count": 0,
            }
        ],
    )
    assert out.split("\n\n", 1)[1] == "[ap:ABC123] 🔱 Decorated task ▸0 · desc:? · self:? · tree:?"
    assert out.count("[ap:ABC123]") == 1


def test_peek_compact_uuid_mode_keeps_ap_left_and_uuid_in_body():
    out = srv._render_dock_peek_raw_text(
        dock_name="D",
        root_note="",
        rows=[
            {
                "uuid": "66666666-aaaa-bbbb-cccc-dddddddddddd",
                "ap_id": "[ap:ABC123]",
                "name": "[ap:ABC123] Named point",
                "is_dock": False,
                "is_local_dock": False,
                "child_count": 0,
            }
        ],
        show_ids=True,
    )
    assert out.splitlines()[0].startswith(
        "D — 1 child · AP-IDs aligned left · [xxxxxxxx] short UUID handle on every row"
    )
    # AP is always left; the optional UUID is ordinary body content.
    assert out.split("\n\n", 1)[1] == "[ap:ABC123] [66666666] Named point ▸0 · desc:? · self:? · tree:?"


def test_peek_compact_default_handle_lengthens_when_the_prefix_collides():
    # CF-22/O3: peek routes through the ONE shortener (unique_short_uuid_prefix),
    # so a colliding 8-hex prefix is lengthened just enough to paste back
    # and resolve uniquely -- never silently wrong.
    colliding_a = "deadbeef-1111-4111-8111-111111111111"
    colliding_b = "deadbeef-2222-4222-8222-222222222222"
    out = srv._render_dock_peek_raw_text(
        dock_name="D",
        root_note="",
        rows=[{"uuid": colliding_a, "ap_id": None, "name": "First twin", "child_count": 0}],
        uuid_prefix_collisions=srv.build_uuid_prefix_collisions([colliding_a, colliding_b]),
    )
    body = out.split("\n\n", 1)[1].splitlines()
    assert body == ["[deadbeef1] First twin ▸0 · desc:? · self:? · tree:?"]


def test_peek_compact_description_excerpt_and_knobs():
    note = "MD_PATH: a/b.md\n---\nThe live   TODO list\nfor the project."
    out = srv._render_dock_peek_raw_text(
        dock_name="D",
        root_note=note,
        rows=[],
        show_description=True,
    )
    # Metadata-stripped, whitespace-collapsed one-line excerpt below the header.
    assert out.splitlines()[1] == "> The live TODO list for the project."
    assert "(no direct children)" in out

    no_desc = srv._render_dock_peek_raw_text(
        dock_name="D", root_note=note, rows=[], show_description=False
    )
    assert ">" not in no_desc

    no_counts = srv._render_dock_peek_raw_text(
        dock_name="D",
        root_note="",
        rows=[
            {
                "uuid": "77777777-0000-0000-0000-000000000000",
                "ap_id": None,
                "name": "Kid",
                "is_dock": False,
                "is_local_dock": False,
                "child_count": 9,
            }
        ],
        show_counts=False,
    )
    assert "▸" not in no_counts


# ---------------------------------------------------------------------------
# slice_group.list -- the nested-display builder twin + the compact serializer
# ---------------------------------------------------------------------------


def _group(gid, name, tags=(), active=False, order_index=None):
    return {
        "id": gid,
        "name": name,
        "short_name": name,
        "tags": list(tags),
        "active": active,
        "order_index": order_index,
    }


def test_slice_group_builder_synthetic_categories_and_attachments():
    # No explicit expander rows at all: 'Work' and 'Work|Communications' are
    # auto-vivified synthetic categories; leaves attach to ALL BUT their
    # final segment.
    rows = build_slice_group_display_items(
        [
            _group("grp-1", "Work|Communications|Jim"),
            _group("grp-2", "Work|Recruiting|RECRUITING"),
        ]
    )
    assert [(r["kind"], r["name"], r["depth"]) for r in rows] == [
        ("expander", "Work", 0),
        ("expander", "Communications", 1),
        ("leaf", "Jim", 2),
        ("expander", "Recruiting", 1),
        ("leaf", "RECRUITING", 2),
    ]
    assert all(r["synthetic"] for r in rows if r["kind"] == "expander")


def test_slice_group_builder_explicit_expander_index_wins_and_duplicates():
    rows = build_slice_group_display_items(
        [
            _group("grp-hi", "[9]Work"),
            _group("grp-lo", "[2]Work"),
            _group("grp-leaf", "Work|Leaf"),
        ]
    )
    expanders = [r for r in rows if r["kind"] == "expander"]
    assert len(expanders) == 2
    # The LOWEST-indexed definition is canonical and owns the descendants.
    canonical = expanders[0]
    assert canonical["group"]["id"] == "grp-lo"
    assert canonical["duplicate"] is False
    duplicate = expanders[1]
    assert duplicate["group"]["id"] == "grp-hi"
    assert duplicate["duplicate"] is True
    # The leaf hangs under the canonical definition's subtree only.
    leaf = [r for r in rows if r["kind"] == "leaf"][0]
    assert leaf["depth"] == 1 and leaf["name"] == "Leaf"


def test_slice_group_builder_mixed_ordering_negative_null_last():
    rows = build_slice_group_display_items(
        [
            _group("grp-b", "NoIndex"),
            _group("grp-a", "Alpha [-1]"),
            _group("grp-c", "[3]Work"),
            _group("grp-d", "Work|Zed [0]"),
        ]
    )
    names = [r["name"] for r in rows]
    # Negative index first, unindexed leaf last at its level; alpha tie-break
    # is case-insensitive; the expander rows intermix with leaves and recurse.
    assert names == ["Alpha", "Work", "Zed", "NoIndex"]


def test_slice_group_inventory_compact_rows_and_caps():
    records = [
        _group(
            "grp-2577886faaaaaaaaaaaaaaaaaaaaaaaa",
            "Load GLIMPSE|Load GLIMPSE - SIMPLE [1]",
            tags=("nexus--overview", "nexus--skills"),
            active=True,
        ),
        _group(
            "grp-9b11892dbbbbbbbbbbbbbbbbbbbbbbbb",
            "Load GLIMPSE|Source Code [2]",
            tags=("a", "b", "c", "d", "e"),
        ),
        _group("grp-cccccccccccccccccccccccccccccccc", "[0]Load GLIMPSE"),
    ]
    out = srv._render_slice_group_inventory_raw_text(records)
    lines = out.splitlines()
    assert lines[0] == (
        "Slice Groups — 3 · [xxxxxxxx] = bare 8-char group-handle prefix "
        "('grp-' elided) · short names resolve · ▾ = category · ✅ = active "
        "· detail='full' for JSON"
    )
    # The explicit '[0]Load GLIMPSE' expander sorts first and owns the leaves.
    assert lines[2] == "▾ Load GLIMPSE"
    # Bare 8-hex group prefix (no 'grp-'), tags restored with '#', ✅ active.
    assert (
        lines[3]
        == "  [2577886f] Load GLIMPSE - SIMPLE — #nexus--overview #nexus--skills ✅"
    )
    # Four-tag cap with the '+N more' overflow.
    assert lines[4] == "  [9b11892d] Source Code — #a #b #c #d +1 more"
    # Category rows never carry ids, tags, or the active marker.

    no_tags = srv._render_slice_group_inventory_raw_text(records, show_tags=False)
    assert "#nexus--overview" not in no_tags
    assert "Load GLIMPSE - SIMPLE ✅" in no_tags


def test_slice_group_inventory_duplicate_category_marker_is_visible():
    records = [
        _group("grp-dddddddd000000000000000000000001", "[1]Work"),
        _group("grp-dddddddd000000000000000000000002", "[2]Work"),
    ]
    out = srv._render_slice_group_inventory_raw_text(records)
    assert "▾ Work ⚠ duplicate category" in out


# ---------------------------------------------------------------------------
# Boundary: full-mode compact-knob rejection at the impl layer
# ---------------------------------------------------------------------------


def test_peek_impl_rejects_a_negative_budget_loudly():
    import asyncio

    with pytest.raises(srv._AnchorOperationError) as excinfo:
        asyncio.run(srv._anchor_dock_peek_impl("D", detail="compact", max_chars=-1))
    assert excinfo.value.code == "invalid_parameter_combination"


def test_slice_group_list_impl_rejects_a_bool_budget_loudly():
    import asyncio

    with pytest.raises(srv._S07OperationError) as excinfo:
        asyncio.run(srv._slice_group_list_impl(detail="compact", max_chars=True))
    assert excinfo.value.code == "invalid_parameter_type"


# ---------------------------------------------------------------------------
# [ap:YLHDIN] part 2 -- dock.list compact rows carry first-line descriptions
# ---------------------------------------------------------------------------


def _described_anchor_tree():
    tree = _anchor_tree()
    tree["children"][0]["description"] = (
        "How the daemon is wired.\nSecond line: implementation details."
    )
    tree["children"][1]["description"] = "   \n  \nLocal only, one line."
    return tree


def test_compact_rows_carry_first_description_line_after_markers():
    out = _render_anchor_tree_raw_text(
        _described_anchor_tree(),
        global_count=1,
        local_count=1,
        show_local=True,
        compact=True,
        max_chars=256,
    )
    lines = out.splitlines()
    # First NON-empty line, after the markers; later lines never leak.
    assert lines[2] == (
        "[1126f2b0] System Architecture — How the daemon is wired."
    )
    assert "Second line" not in out
    # A whitespace-only lead: the first non-empty line is shown.
    assert "[77a69aa8] Local sub-dock Ⓛ — Local only, one line." in lines


def test_compact_rows_without_description_stay_byte_identical():
    # The pre-YLHDIN fixture has no description keys at all: every row must
    # render exactly as before (no dangling separator).
    out = _render_anchor_tree_raw_text(
        _anchor_tree(),
        global_count=1,
        local_count=1,
        show_local=True,
        compact=True,
        max_chars=256,
    )
    assert "[1126f2b0] System Architecture" in out.splitlines()
    assert "[77a69aa8] Local sub-dock Ⓛ" in out.splitlines()
    assert "—" not in out.replace("Anchor Docks — ", "", 1)


def test_compact_description_knobs_hide_or_budget_the_excerpt():
    tree = _described_anchor_tree()

    hidden = _render_anchor_tree_raw_text(
        tree, global_count=1, local_count=1, show_local=True,
        compact=True, max_chars=256, show_description=False,
    )
    assert "[1126f2b0] System Architecture" in hidden.splitlines()

    zeroed = _render_anchor_tree_raw_text(
        tree, global_count=1, local_count=1, show_local=True,
        compact=True, max_chars=256, max_description_chars=0,
    )
    assert "[1126f2b0] System Architecture" in zeroed.splitlines()

    budgeted = _render_anchor_tree_raw_text(
        tree, global_count=1, local_count=1, show_local=True,
        compact=True, max_chars=256, max_description_chars=20,
    )
    row = next(l for l in budgeted.splitlines() if l.startswith("[1126f2b0]"))
    excerpt = row.split(" — ", 1)[1]
    assert excerpt.count("…") == 1 and len(excerpt) == 20


def test_legacy_mode_ignores_the_description_kwargs():
    tree = _described_anchor_tree()
    legacy = _render_anchor_tree_raw_text(
        tree, global_count=5, local_count=2, show_local=True,
        max_description_chars=10, show_description=True,
    )
    assert "How the daemon" not in legacy
    assert "Local only" not in legacy


def test_compact_description_excerpt_helper_edges():
    from workflowy_mcp.client.api_client_nexus import (
        _compact_description_excerpt,
    )

    assert _compact_description_excerpt({}, 120, True) == ""
    assert _compact_description_excerpt({"description": ""}, 120, True) == ""
    assert _compact_description_excerpt({"description": None}, 120, True) == ""
    assert (
        _compact_description_excerpt(
            {"description": "   \n \nReal line"}, 120, True
        )
        == " — Real line"
    )
    assert _compact_description_excerpt({"description": "Keep"}, 120, False) == ""
    assert _compact_description_excerpt({"description": "Keep"}, 0, True) == ""
    assert _compact_description_excerpt({"description": "Keep"}, None, True) == " — Keep"
    long = _compact_description_excerpt({"description": "x" * 300}, 120, True)
    assert long.startswith(" — ") and long.count("…") == 1
