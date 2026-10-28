"""Grammar lock: the server-side slice-group NAME parser.

``parse_slice_group_name`` (``f8_slice_policy.py``) is the server-side port of
the client's ``parseSliceGroupName`` (glimpse-cache-client.js) -- the ONE
pipe-path / ``[n]``-index display grammar, implemented once per runtime. These
tests pin the JS behavior bug-for-bug so server-side handle resolution and the
agent-facing public view interpret a stored name EXACTLY the way the widget
displays it, and so the two runtimes cannot silently drift on the grammar.

Pure function; no I/O. If the grammar ever changes, BOTH runtimes must change
together -- these cases are the mirror of the JS behavior.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.f8_slice_policy import parse_slice_group_name  # noqa: E402


def test_leaf_full_path_with_trailing_index():
    p = parse_slice_group_name("nexus|glimpse|extension [2]")
    assert p["is_expander"] is False
    assert p["segments"] == ["nexus", "glimpse", "extension"]
    assert p["index"] == 2
    assert p["short_name"] == "extension"


def test_plain_name_no_index():
    p = parse_slice_group_name("Architecture")
    assert p == {
        "is_expander": False,
        "segments": ["Architecture"],
        "index": None,
        "short_name": "Architecture",
    }


def test_negative_trailing_index():
    p = parse_slice_group_name("leaf [-3]")
    assert p["index"] == -3
    assert p["short_name"] == "leaf"


def test_multi_bracket_tail_uses_last_bracket():
    # JS: only the FINAL bracket is the index; an earlier bracket is literal.
    p = parse_slice_group_name("a [1][2]")
    assert p["index"] == 2
    assert p["segments"] == ["a [1]"]
    assert p["short_name"] == "a [1]"


def test_expander_leading_index():
    p = parse_slice_group_name("[2]nexus")
    assert p["is_expander"] is True
    assert p["segments"] == ["nexus"]
    assert p["index"] == 2
    assert p["short_name"] == "nexus"


def test_expander_path():
    p = parse_slice_group_name("[1]nexus|glimpse")
    assert p["is_expander"] is True
    assert p["segments"] == ["nexus", "glimpse"]
    assert p["index"] == 1
    assert p["short_name"] == "glimpse"


def test_expander_branch_does_not_strip_trailing_index():
    # JS quirk, mirrored verbatim: the expander branch splits on '|' but does
    # NOT run the trailing-index strip, so a trailing [n] on the last segment
    # survives as literal text.
    p = parse_slice_group_name("[1]nexus [2]")
    assert p["is_expander"] is True
    assert p["segments"] == ["nexus [2]"]
    assert p["index"] == 1
    assert p["short_name"] == "nexus [2]"


def test_degenerate_index_only_is_not_an_expander():
    # '[2]' with no path -> not a usable expander; a plain single segment.
    p = parse_slice_group_name("[2]")
    assert p["is_expander"] is False
    assert p["segments"] == ["[2]"]
    assert p["index"] is None
    assert p["short_name"] == "[2]"


def test_unparseable_bracket_is_literal():
    p = parse_slice_group_name("[foo]nexus")
    assert p["is_expander"] is False
    assert p["segments"] == ["[foo]nexus"]
    assert p["index"] is None
    assert p["short_name"] == "[foo]nexus"


def test_empty_segment_survives_trailing_pipe():
    # 'nexus|' -> segments ['nexus', '']; the empty short name is excluded
    # from short-name resolution by callers, not by the parser.
    p = parse_slice_group_name("nexus|")
    assert p["segments"] == ["nexus", ""]
    assert p["short_name"] == ""


def test_segments_are_trimmed():
    p = parse_slice_group_name("  alpha  |  beta [1]  ")
    assert p["segments"] == ["alpha", "beta"]
    assert p["index"] == 1
    assert p["short_name"] == "beta"


def test_non_string_input_is_safe():
    # Non-string input degrades to the empty-string parse (mirrors the JS,
    # which coerces a non-string to '').
    assert parse_slice_group_name(None)["short_name"] == ""
    assert parse_slice_group_name(None)["segments"] == [""]
    assert parse_slice_group_name(123)["segments"] == [""]
    assert parse_slice_group_name(123)["short_name"] == ""
