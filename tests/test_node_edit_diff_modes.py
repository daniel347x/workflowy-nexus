"""One public note-report contract, with seam evidence instead of write echoes."""
from __future__ import annotations

import difflib
from unittest.mock import Mock

import pytest

from workflowy_mcp import server
from test_node_edit_return_diff import ROOT, dispatch, parse_report, setup


def render(before, after, mode="seams"):
    return server._render_note_unified_diff(before, after, mode=mode)


@pytest.mark.parametrize("mode", ["seams", "seams_full"])
@pytest.mark.parametrize("kind", ["insert", "replace", "delete"])
def test_block_edges_and_exact_middle_counts(mode, kind):
    old = [f"old {i}\n" for i in range(9)] if kind != "insert" else []
    new = [f"new {i}\n" for i in range(11)] if kind != "delete" else []
    before = "far before\nbefore\n" + "".join(old) + "after\nfar after\n"
    after = "far before\nbefore\n" + "".join(new) + "after\nfar after\n"
    diff = render(before, after, mode)
    assert diff.startswith("--- note (before)\n+++ note (after)\n")
    assert f"(+{len(new)} / -{len(old)})" in diff
    assert " before\n" in diff and " after\n" in diff
    assert "far before" not in diff and "far after" not in diff
    for prefix, block in (("-", old), ("+", new)):
        if not block:
            continue
        assert f"... {len(block) - 4} {prefix} lines elided\n" in diff
        for line in block[:2] + block[-2:]:
            assert prefix + line in diff
        for line in block[2:-2]:
            assert prefix + line not in diff


@pytest.mark.parametrize("length", [1, 2, 3, 4, 5])
def test_short_blocks_are_neither_duplicated_nor_over_elided(length):
    text = "".join(f"new {i}\n" for i in range(length))
    diff = render("before\nafter\n", "before\n" + text + "after\n")
    lines = diff.splitlines()
    for i in set(range(length)) - set(range(2, length - 2)):
        assert lines.count(f"+new {i}") == 1
    assert ("lines elided" in diff) == (length > 4)


def test_independent_blocks_keep_their_own_seams_and_hunks():
    before = "\n".join(f"line {i}" for i in range(30)) + "\n"
    after = before.replace("line 3\n", "three\n").replace("line 24\n", "twenty four\n")
    diff = render(before, after)
    assert diff.count("@@ -") == 2
    for text in (" line 2\n", " line 4\n", " line 23\n", " line 25\n", "+three\n", "+twenty four\n"):
        assert text in diff
    assert "line 10" not in diff


def test_distant_changes_on_one_paragraph_get_separate_merged_windows():
    before = "prefix " + "a" * 250 + " OLD_A " + "b" * 280 + " OLD_B " + "c" * 250 + " suffix\n"
    after = before.replace("OLD_A", "NEW_A").replace("OLD_B", "NEW_B")
    collapsed, whole = render(before, after), render(before, after, "seams_full")
    old_line = next(line[1:] for line in collapsed.splitlines() if line.startswith("-") and not line.startswith("---"))
    new_line = next(line[1:] for line in collapsed.splitlines() if line.startswith("+") and not line.startswith("+++"))
    assert all(x in old_line for x in ("OLD_A", "OLD_B"))
    assert all(x in new_line for x in ("NEW_A", "NEW_B"))
    assert old_line.count("…") == new_line.count("…") == 3
    assert len(old_line) <= 2 * 128 + 3
    assert "-" + before in whole and "+" + after in whole
    assert "…" not in whole


@pytest.mark.parametrize("position", ["start", "middle", "end"])
def test_smart_windows_preserve_near_edges_and_whitespace_change(position):
    left = "left " * (1 if position == "start" else 100)
    right = " right" * (1 if position == "end" else 100)
    before = left + "A B" + right + "\n"
    after = left + "AB" + right + "\n"
    diff = render(before, after)
    old = next(x[1:] for x in diff.splitlines() if x.startswith("-") and not x.startswith("---"))
    new = next(x[1:] for x in diff.splitlines() if x.startswith("+") and not x.startswith("+++"))
    assert "A B" in old and "AB" in new
    assert len(old) <= 130 and len(new) <= 130
    assert old.startswith("…") == (position != "start")
    assert old.endswith("…") == (position != "end")


def test_large_single_line_insert_is_bounded_and_context_faces_the_seam():
    before_line, after_line = "BEFORE " + "x" * 800 + " before-edge", "after-edge " + "y" * 800 + " AFTER"
    text = "new-start " + "z" * 20_000 + " new-end\n"
    diff = render(before_line + "\n" + after_line + "\n", before_line + "\n" + text + after_line + "\n")
    assert " before-edge\n" in diff and " after-edge " in diff
    assert "BEFORE" not in diff and "AFTER" not in diff
    assert "+new-start " in diff and " new-end\n" in diff
    assert len(diff) < 700


def test_long_intraline_replacement_keeps_old_new_transition_characters():
    left, right = "left " * 60 + "BEFORE[", "]AFTER" + " right" * 60
    before, after = left + "X" * 1000 + right + "\n", left + "Z" * 1000 + right + "\n"
    diff = render(before, after)
    assert "BEFORE[XXX" in diff and "XXX]AFTER" in diff
    assert "BEFORE[ZZZ" in diff and "ZZZ]AFTER" in diff
    assert len(diff) < 500


def test_no_newline_marker_makes_a_terminal_seam_visible():
    diff = render("same\n", "same")
    assert "-same\n+same\n\\ No newline at end of note\n" in diff


def test_sparse_changes_in_a_large_repeated_line_keep_each_window():
    # Character SequenceMatcher(autojunk=False) takes >30s on this paragraph;
    # the sparse-edit frontier must preserve all three distant changes too.
    before = ("x" * 20_000 + " OLD ") * 3 + "tail\n"
    after = before.replace(" OLD ", " NEW_TEXT ")
    diff = render(before, after)
    assert diff.count("OLD") == 3 and diff.count("NEW_TEXT") == 3
    assert len(diff) < 1500


@pytest.mark.parametrize("before,after", [("old" * 1000, "new" * 1000), ("", "new" * 1000), ("old" * 1000, "")])
def test_wholesale_long_line_changes_keep_boundaries_without_echoing_bulk(before, after):
    diff = render(before + "\n", after + "\n")
    assert len(diff) < 500


def test_character_spans_cover_the_actual_edits_and_leave_matching_gaps():
    for before, after in (("ab", "ba"), ("abc def ghi", "abcXX defYYY ghi"),
        ("📋 a &lt; b", "📋  a < b"), ("a" * 500 + "b", "a" * 250 + "X" + "a" * 250 + "b")):
        spans = server._note_diff_small_changes(before, after)
        assert spans is not None
        old_end = new_end = 0
        for i1, i2, j1, j2 in spans:
            assert before[old_end:i1] == after[new_end:j1]
            old_end, new_end = i2, j2
        assert before[old_end:] == after[new_end:]


def test_empty_before_still_shows_default_seams_and_full_retains_old_summary():
    after = "".join(f"new {i}\n" for i in range(20))
    diff = render("", after)
    assert "@@ -0,0 +1,20 @@ (+20 / -0)" in diff
    assert "... 16 + lines elided" in diff
    assert "+new 0\n+new 1\n" in diff and "+new 18\n+new 19\n" in diff
    assert render("", after, "full") == "note was empty; 20 lines written"
    assert render("", after, "none") == ""


def test_full_is_the_historical_unified_diff():
    before, after = "before\nold\nafter", "before\nnew\nafter"
    expected = "".join(x if x.endswith("\n") else x + "\n" for x in difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="note (before)", tofile="note (after)", n=3,
    ))
    assert render(before, after, "full") == expected


@pytest.mark.parametrize("mode", ["seams", "seams_full", "full"])
def test_size_guard_keeps_true_counts_after_block_elisions(mode):
    before, after = [], []
    for i in range(100):
        before.extend(f"old {i} {j}\n" for j in range(8))
        after.extend(f"new {i} {j}\n" for j in range(10))
        gap = [f"context {i} {j}\n" for j in range(12)]
        before.extend(gap)
        after.extend(gap)
    diff = render("".join(before), "".join(after), mode)
    assert len(diff.splitlines()) == server.DEFAULT_NOTE_MAX_LINES + 1
    assert "hunks hidden; totals +1000 / -800" in diff
    assert "diff lines not shown" in diff


@pytest.mark.parametrize("kind", ["single", "multi", "note", "note_file"])
@pytest.mark.parametrize("mode", [None, "seams", "seams_full", "full", "none"])
def test_every_public_write_kind_has_the_same_modes_and_match_header(monkeypatch, tmp_path, kind, mode):
    before = "before\nold\nafter\n"
    after = "before\n" + "".join(f"new {i}\n" for i in range(12)) + "after\n"
    client, _, _ = setup(monkeypatch, tmp_path, before)
    replacement = after[len("before\n"):-len("after\n")]
    if kind == "single":
        params = {"note_search": "old\n", "note_replace": replacement}
    elif kind == "multi":
        params = {"note_edits": [{"search": "old\n", "replace": replacement}, {"search": "not present"}]}
    elif kind == "note_file":
        path = tmp_path / "input.md"
        path.write_text(after, encoding="utf-8")
        params = {"note_file": str(path)}
    else:
        params = {"note": after}
    formatter = Mock(wraps=server._render_note_unified_diff)
    monkeypatch.setattr(server, "_render_note_unified_diff", formatter)
    meta, diff = parse_report(dispatch(**params, **({"diff": mode} if mode else {})))
    assert meta["success"] and meta["note_changed"] and not meta["no_op"]
    assert meta["note_chars"] == len(after)
    if kind in ("single", "multi"):
        assert meta["edits_applied"] == 1
        assert meta["edit_results"][0] == {"ordinal": 0, "matched": True, "index": len("before\n")}
        if kind == "multi":
            assert meta["edit_results"][1] == {"ordinal": 1, "matched": False, "index": -1}
    else:
        assert "edit_results" not in meta
    if mode == "none":
        assert diff == ""
        formatter.assert_not_called()
    else:
        assert diff == render(before, after, mode or "seams")
        assert ("lines elided" in diff) == (mode != "full")
    client.get_node.assert_awaited_once_with(ROOT)
    client.update_node.assert_awaited_once()
    assert client.update_node.await_args.args[1].note == after


@pytest.mark.parametrize("alias,mode", [(True, "full"), (False, "none")])
def test_boolean_alias_is_exact_for_search_replace(monkeypatch, tmp_path, alias, mode):
    setup(monkeypatch, tmp_path, "before\nold\nafter\n")
    params = {"note_search": "old", "note_replace": "new"}
    assert dispatch(**params, return_diff=alias) == dispatch(**params, diff=mode)
    assert dispatch(**params, diff=mode, return_diff=alias) == dispatch(**params, diff=mode)


@pytest.mark.parametrize("bad,code", [(None, "invalid_parameter_type"), (True, "invalid_parameter_type"),
    (0, "invalid_parameter_type"), ([], "invalid_parameter_type"), ("compact", "invalid_enum_value")])
def test_invalid_enum_rejected_before_file_or_live_work(monkeypatch, tmp_path, bad, code):
    client, _, _ = setup(monkeypatch, tmp_path)
    result = dispatch(note_file="not read", diff=bad)
    assert result["error"]["code"] == code
    client.get_node.assert_not_awaited()
    client.update_node.assert_not_awaited()


def test_alias_conflict_is_zero_write_and_none_keeps_misses(monkeypatch, tmp_path):
    client, _, _ = setup(monkeypatch, tmp_path, "old note")
    result = dispatch(note_file="not read", diff="seams", return_diff=False)
    assert result["success"] is False and "conflicts" in result["error"]["message"]
    client.get_node.assert_not_awaited()
    client.update_node.assert_not_awaited()
    meta, diff = parse_report(dispatch(note_search="missing", diff="none"))
    assert meta["edits_applied"] == 0 and meta["edit_results"][0]["matched"] is False
    assert meta["note_chars"] == len("old note") and meta["no_op"] and diff == ""
    client.update_node.assert_not_awaited()
