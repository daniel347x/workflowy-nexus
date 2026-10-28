"""Global AP-only alignment and explicit cross-Dock peek policy."""
from __future__ import annotations

import copy
from unittest.mock import Mock

import pytest
from workflowy_mcp import server
from workflowy_mcp.coherence.operation_cache import current_operation_cache
from test_dock_peek_metrics import setup, dispatch, rows, R, A, D, X, G, B, H, C, E


def body_rows(source_rows, **options):
    text = server._render_dock_peek_raw_text(
        dock_name="Dock", root_note="", rows=source_rows,
        recursive=True, show_counts=False, **options,
    )
    return text.split("\n\n", 1)[1].splitlines()


@pytest.mark.parametrize("show_ids", [False, True])
def test_one_global_width_across_ten_groups_and_four_levels(show_ids):
    source = []
    for group in range(10):
        for depth in range(5):
            ap_id = f"[ap:G{group}D{depth}]" if (group + depth) % 3 == 0 else None
            if group == 9 and depth == 4:
                ap_id = "[ap:THELONGESTVISIBLECORE]"
            name = f"Group {group} depth {depth}"
            source.append({
                "uuid": f"{group * 5 + depth:08x}-1111-4111-8111-111111111111",
                "ap_id": ap_id, "name": f"  {ap_id + ' ' if ap_id else ''}{name}  ",
                "depth": depth,
            })
    before = copy.deepcopy(source)
    lines = body_rows(source, show_ids=show_ids)
    width = len("[ap:THELONGESTVISIBLECORE]")
    for row, line in zip(source, lines):
        ap = row["ap_id"] or ""
        expected_body = f"Group {source.index(row) // 5} depth {row['depth']}"
        # [ap:0M6VP8] peek half: rows WITHOUT an AP-ID carry the short UUID
        # handle by default; show_ids=true adds it to every row.
        optional_uuid = f"[{row['uuid'][:8]}] " if (show_ids or not ap) else ""
        assert line == ap.ljust(width) + " " + "    " * row["depth"] + optional_uuid + expected_body
        if ap:
            assert line.startswith(ap)  # never indented or padded inside brackets
            assert line.count(ap) == 1
        else:
            assert line[:width].isspace()  # UUID never fills the AP column
    assert source == before


@pytest.mark.parametrize("show_ids", [False, True])
def test_no_ap_anywhere_means_zero_column_width(show_ids):
    source = [
        {"uuid": "12345678-1111-4111-8111-111111111111", "name": "  Parent  ", "depth": 0},
        {"uuid": "abcdef01-1111-4111-8111-111111111111", "name": "\tChild\t", "depth": 4},
    ]
    lines = body_rows(source, show_ids=show_ids)
    # The AP column stays zero-width (no phantom column or separator), but an
    # AP-less row is never handle-less: the short UUID handle is the default.
    assert lines == ["[12345678] Parent", "                [abcdef01] Child"]


def test_ap_decoration_and_internal_title_spaces_survive_outer_trim():
    source = [{"uuid": R, "ap_id": "[ap:ABC123]", "name": "  🔱 [ap:ABC123] Title   with spaces  ", "depth": 2}]
    assert body_rows(source) == ["[ap:ABC123]         🔱 Title   with spaces"]


def test_flat_view_shows_parent_hint_per_row_and_document_order_does_not():
    # UTAERZ: in a flat view (recent-N / timestamp order) every row whose
    # parent is not the peeked Dock carries a ' ← in ‹parent handle›' hint;
    # the plain document-order peek keeps its indentation and adds none.
    source = [
        {"uuid": "aaaaaaaa-1111-4111-8111-111111111111", "ap_id": None,
         "name": "Direct child", "depth": 0, "parent_id": "ROOT"},
        {"uuid": "bbbbbbbb-1111-4111-8111-111111111111", "ap_id": None,
         "name": "Nested child", "depth": 1, "parent_id": "aaaaaaaa-1111-4111-8111-111111111111"},
    ]
    flat = server._render_dock_peek_raw_text(
        dock_name="Dock", root_note="", rows=source, recursive=True, show_counts=False,
        selection={"recent": 2, "recent_by": "modified", "candidates": 2, "selected": 2, "completed_excluded": 0},
        sort={"by": "modified", "direction": "asc"},
        root_uuid="ROOT",
        parent_handles={"aaaaaaaa-1111-4111-8111-111111111111": "[aaaaaaaa]"},
    )
    assert "flat, parent shown per row" in flat.splitlines()[0]
    body = flat.split("\n\n", 1)[1].splitlines()
    # Direct child: parent IS the peeked Dock, so no hint.
    assert body[0] == "[aaaaaaaa] Direct child"
    assert body[1] == "[bbbbbbbb] Nested child ← in [aaaaaaaa]"
    doc_order = server._render_dock_peek_raw_text(
        dock_name="Dock", root_note="", rows=source, recursive=True, show_counts=False,
        root_uuid="ROOT",
    )
    assert "parent shown per row" not in doc_order
    doc_body = doc_order.split("\n\n", 1)[1].splitlines()
    assert doc_body[0] == "[aaaaaaaa] Direct child"
    assert doc_body[1] == "    [bbbbbbbb] Nested child"
    assert "← in" not in doc_order


@pytest.mark.parametrize("policy", ["stop_at_child_anchors", "all"])
@pytest.mark.parametrize("recursive", [False, True])
def test_policy_only_controls_existing_recursive_walk(monkeypatch, policy, recursive):
    client, config = setup(monkeypatch)
    view = client._cache_view_publisher.capture()
    before = copy.deepcopy(view.nodes_export)
    import workflowy_mcp.dock_peek_metrics as metrics
    builder = Mock(wraps=metrics.build_dock_peek_index)
    monkeypatch.setattr(metrics, "build_dock_peek_index", builder)
    # include_completed=True: the subject is descend_policy traversal, not
    # the completed policy (U6 hides the completed row by default).
    result = dispatch(detail="full", recursive=recursive, descend_policy=policy, include_completed=True)
    if not recursive:
        expected = [A, B, C]
        depths = [0, 0, 0]
    elif policy == "all":
        expected = [A, D, X, G, B, H, C, E]
        depths = [0, 1, 2, 1, 0, 1, 0, 1]
    else:
        expected = [A, D, G, B, C, E]
        depths = [0, 1, 1, 0, 0, 1]
    assert result["success"] is True and result["descend_policy"] == policy
    assert [row["uuid"] for row in result["rows"]] == expected
    assert [row["depth"] for row in result["rows"]] == depths
    assert result["root_metrics"]["descendant_count"] == 8
    assert next(row for row in result["rows"] if row["uuid"] == B)["is_dock"]
    builder.assert_called_once()
    config.assert_awaited_once()
    client.get_node.assert_not_awaited()
    client.update_node.assert_not_awaited()
    client.create_node.assert_not_awaited()
    assert client._cache_view_publisher.capture() is view
    assert view.nodes_export == before
    assert current_operation_cache(required=False) is None


def test_open_docks_remain_marked_and_legend_does_not_claim_they_stop(monkeypatch):
    setup(monkeypatch)
    output = dispatch(recursive=True, descend_policy="all")
    assert "recursive; includes nested Docks" in output
    assert "Key: ⚓ Dock · ⭐ favorite Dock · Ⓛ local Dock · ✓ completed" in output
    assert "recursion stops here" not in output
    lines = output.split("\n\n", 1)[1].splitlines()
    assert next(line for line in lines if "Local Dock" in line).endswith("⚓ Ⓛ")
    assert next(line for line in lines if "Global Dock" in line).endswith("⚓")
    assert any("Behind local" in line for line in lines)
    assert any("Behind global" in line for line in lines)


def test_hidden_ap_does_not_widen_column_until_its_dock_is_opened(monkeypatch):
    nodes = rows()
    next(node for node in nodes if node["id"] == X)["name"] = "[ap:LONGHIDDENANCHORPOINT] Behind local"
    setup(monkeypatch, nodes=nodes)
    closed = dispatch(recursive=True, show_counts=False)
    open_view = dispatch(recursive=True, descend_policy="all", show_counts=False)
    closed_rows = closed.split("\n\n", 1)[1].splitlines()
    open_rows = open_view.split("\n\n", 1)[1].splitlines()
    # WG3QTR: the full name, trailing #tags included, is what peek shows.
    assert closed_rows[0] == "[ap:ALPHA] Alpha 📊 #keep"
    assert open_rows[0] == "[ap:ALPHA]".ljust(len("[ap:LONGHIDDENANCHORPOINT]")) + " Alpha 📊 #keep"
    assert "[ap:LONGHIDDENANCHORPOINT]" not in closed


def test_nested_dock_depth_is_actual_node_depth_not_compressed_dock_ancestry(monkeypatch):
    nodes = [
        {"id": R, "name": "[ap:ROOT01] Root #dock", "parent_id": None},
        {"id": A, "name": "ordinary 1", "parent_id": R},
        {"id": G, "name": "ordinary 2", "parent_id": A},
        {"id": D, "name": "nested Dock #dock--local", "parent_id": G},
        {"id": X, "name": "inside Dock", "parent_id": D},
    ]
    setup(monkeypatch, nodes=nodes)
    closed = dispatch(detail="full", recursive=True)
    opened = dispatch(detail="full", recursive=True, descend_policy="all")
    assert [(row["uuid"], row["depth"]) for row in closed["rows"]] == [(A, 0), (G, 1), (D, 2)]
    assert [(row["uuid"], row["depth"]) for row in opened["rows"]] == [(A, 0), (G, 1), (D, 2), (X, 3)]
    assert opened["root_metrics"] == closed["root_metrics"]
