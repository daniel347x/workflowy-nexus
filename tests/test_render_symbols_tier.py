"""54e8356f(ii): ``lightning_rod.render_symbols`` gains ``tier``.

Three layers are locked here:

* ``snippet_view.format_snippet_header`` — the ONE header format, extracted
  verbatim out of ``render_guarded_lines`` (beacon.snippet and the metadata
  tier must print the same line, never two);
* ``snippet_view.render_symbol_tier_note`` — the fenced measure-before-read
  body ('metadata' = exactly the header; 'context' = comment + def + first
  ``context_lines`` lines + one trailer row naming the rest, optional tail);
* the ``render_symbols``/``render_nodes`` MCP surface — tier wiring,
  byte-parity of the default tier, MCP-return-only refusals, and the
  non-fatal WARNING section for failed disk reads.

All local: no network, no real Workflowy, no real source files on disk for
the end-to-end runs (the Layer-4 extraction is stubbed at the client).
"""
import asyncio
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402
from workflowy_mcp.mcp_api_registry import OperationValidationError  # noqa: E402
from workflowy_mcp.snippet_view import (  # noqa: E402
    format_snippet_header,
    render_guarded_lines,
    render_symbol_tier_note,
)


# ---------------------------------------------------------------------------
# Fixture: one tiny "file" the stubbed Layer-4 extraction "reads".
# Lines 1-3 are the beacon comment block, line 4 the def, lines 5-8 the body.
# ---------------------------------------------------------------------------
_FILE_PATH = "/repo/example.py"
_SPAN_LINES = [
    "# @beacon[",
    "#   id=demo-beacon,",
    "# ]",
    "def demo(x):",
    "    return x + 1",
    "    # unreachable comment",
    "    return x + 2",
    "    return x + 3",
]
_CORE_START, _CORE_END = 1, len(_SPAN_LINES)
_DEF_LINE = 4


def _snippet_result():
    return {
        "file_path": _FILE_PATH,
        "file_lines": list(_SPAN_LINES),
        "core_start_line": _CORE_START,
        "core_end_line": _CORE_END,
    }


# ---------------------------------------------------------------------------
# format_snippet_header — the ONE format
# ---------------------------------------------------------------------------


def test_header_numbers_are_hand_computed():
    # 3 ASCII lines of 5 chars each: bytes = 3 * (5 + 1) = 18, tokens = ceil(18/3) = 6.
    header = format_snippet_header("snippet: p | ", ["aaaaa", "bbbbb", "ccccc"], 1, 3)
    assert header == "[snippet: p | lines 1-3 of 3 | 3 lines, 18 bytes, ~6 tokens]"


def test_header_matches_guard_core_output_byte_for_byte():
    # THE parity lock: the header render_guarded_lines prints as its first
    # line is EXACTLY format_snippet_header's output (the extraction must be
    # invisible). test_snippet_view.py pins the guard's own bytes unchanged.
    from workflowy_mcp.snippet_view import SnippetViewOptions

    lines = [f"line {i}" for i in range(1, 41)]
    opts = SnippetViewOptions(
        numbering="file",
        coordinates="file",
        start_line=None,
        end_line=None,
        limit=None,
        context=0,
        max_lines=10,
        size_only=False,
    )
    guarded = render_guarded_lines(lines, 1, len(lines), "snippet: f.py | ", opts, with_header=True)
    first_line = guarded.split("\n", 1)[0]
    assert first_line == format_snippet_header("snippet: f.py | ", lines, 1, len(lines))


def test_header_relative_suffix():
    # A mid-file span over a 10-line document (the helper does not clamp —
    # the guard core clamps before calling; numbers reflect the span given).
    lines = [f"line-{i}" for i in range(1, 11)]
    header = format_snippet_header("snippet: p | ", lines, 7, 8, relative_start=7)
    assert header == (
        "[snippet: p | lines 7-8 of 10 | 2 lines, 14 bytes, ~5 tokens | relative 1 = line 7]"
    )


# ---------------------------------------------------------------------------
# render_symbol_tier_note
# ---------------------------------------------------------------------------


def test_metadata_tier_is_exactly_the_fenced_header():
    note = render_symbol_tier_note("metadata", _snippet_result())
    expected_header = format_snippet_header(
        f"snippet: {_FILE_PATH} | ", _SPAN_LINES, _CORE_START, _CORE_END
    )
    assert note == "\n".join(["```", expected_header, "```"])


def test_context_tier_shows_comment_def_and_context_lines_without_trailer_when_complete():
    note = render_symbol_tier_note(
        "context", _snippet_result(), def_line=_DEF_LINE, context_lines=10, tail_lines=0
    )
    body = [
        "# @beacon[",
        "#   id=demo-beacon,",
        "# ]",
        "def demo(x):",
        "    return x + 1",
        "    # unreachable comment",
        "    return x + 2",
        "    return x + 3",
    ]
    assert note == "\n".join(["```", *body, "```"])
    assert "more lines" not in note


def test_context_tier_trailer_names_the_hidden_rest():
    # context_lines=1 -> head = comment block + def + 1 body line (lines 1-5);
    # the hidden rest is lines 6-8.
    note = render_symbol_tier_note(
        "context", _snippet_result(), def_line=_DEF_LINE, context_lines=1, tail_lines=0
    )
    body = [
        "# @beacon[",
        "#   id=demo-beacon,",
        "# ]",
        "def demo(x):",
        "    return x + 1",
        "… 3 more lines; start_line=6, end_line=8",
    ]
    assert note == "\n".join(["```", *body, "```"])


def test_context_tier_tail_lines_land_after_the_trailer():
    # context_lines=1, tail_lines=1 -> hidden middle is lines 6-7, tail is line 8.
    note = render_symbol_tier_note(
        "context", _snippet_result(), def_line=_DEF_LINE, context_lines=1, tail_lines=1
    )
    body = [
        "# @beacon[",
        "#   id=demo-beacon,",
        "# ]",
        "def demo(x):",
        "    return x + 1",
        "… 2 more lines; start_line=6, end_line=7",
        "    return x + 3",
    ]
    assert note == "\n".join(["```", *body, "```"])


def test_context_tier_def_line_defaults_to_first_non_comment_non_blank_line():
    # No mapped line metadata: the scan skips the beacon comment block and
    # lands on the def.
    note = render_symbol_tier_note(
        "context", _snippet_result(), def_line=None, context_lines=0, tail_lines=0
    )
    body = [
        "# @beacon[",
        "#   id=demo-beacon,",
        "# ]",
        "def demo(x):",
        "… 4 more lines; start_line=5, end_line=8",
    ]
    assert note == "\n".join(["```", *body, "```"])


def test_context_tier_fence_dodges_backtick_shaped_body_lines():
    result = _snippet_result()
    # A body line that STARTS with a backtick fence marker could close a
    # ``` fence early (the composer's grammar: "^ {0,3}(`{3,}|~{3,})"), so
    # the chooser must switch markers. A line merely CONTAINING backticks
    # cannot close a fence and keeps the plain ``` fence.
    result["file_lines"] = ["def demo():", "```python", "    return x"]
    result["core_start_line"], result["core_end_line"] = 1, 3
    note = render_symbol_tier_note("context", result, def_line=1, context_lines=5)
    assert note.startswith("~~~~\n")
    assert note.endswith("\n~~~~")
    # A body line that only contains backticks mid-line cannot close a fence.
    result["file_lines"] = ["def demo():", '    x = "```python"', "    return x"]
    note2 = render_symbol_tier_note("context", result, def_line=1, context_lines=5)
    assert note2.startswith("```\n")
    assert note2.endswith("\n```")


def test_unknown_tier_is_a_loud_validation_error():
    try:
        render_symbol_tier_note("everything", _snippet_result())
    except OperationValidationError:
        pass
    else:
        raise AssertionError("unknown tier must raise OperationValidationError")


# ---------------------------------------------------------------------------
# _lightning_rod_render_symbols end-to-end (fixture style of
# test_lightning_rod_render_symbols_resolves.py)
# ---------------------------------------------------------------------------

_ROOT = "bbbbbbbb-0000-0000-0000-000000000001"
_F = "bbbbbbbb-0000-0000-0000-000000000002"
_G = "bbbbbbbb-0000-0000-0000-000000000003"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Symbols Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _F, "name": "def foo()", "note": "body of foo", "parent_id": _ROOT, "priority": 0},
    {"id": _G, "name": "def bar()", "note": "body of bar", "parent_id": _ROOT, "priority": 1},
]


def _make_client(tier_reads=None, fail_for=None):
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]

    async def _ruf(*, file_path=None, symbol=None, symbol_kind="auto"):
        return {"candidates": []}

    c.read_uuid_from_symbol = _ruf

    reads = dict(tier_reads or {})
    fail_for = set(fail_for or ())

    async def _gcsfr(beacon_node_id, beacon_node, nodes_by_id, context=0):
        if beacon_node_id in fail_for:
            raise RuntimeError(f"disk read failed for {beacon_node_id}")
        if beacon_node_id in reads:
            return reads[beacon_node_id]
        name = str((beacon_node or {}).get("name") or "def sym()")
        lines = [f"# @beacon[", f"#   id={beacon_node_id},", "# ]", name, "    body"]
        return {
            "file_path": f"/repo/{beacon_node_id[:8]}.py",
            "file_lines": lines,
            "core_start_line": 1,
            "core_end_line": len(lines),
        }

    c.get_code_snippet_for_resolved_node = _gcsfr
    return c


def _stub_suppress_config(monkeypatch, tags=None):
    async def _cfg(_target_tab):
        return [], None, {}, list(tags or []), {}

    monkeypatch.setattr(srv, "_resolve_slice_group_config", _cfg)


def _run(monkeypatch, *, symbols, root_uuids, client=None, **kwargs):
    client = client or _make_client()
    monkeypatch.setattr(srv, "get_client", lambda: client)
    _stub_suppress_config(monkeypatch)
    params = dict(
        symbols=symbols,
        root_uuids=root_uuids,
        favor="uuid",
        as_markdown=True,
        output_file=None,
        sort_by=None,
        sort_direction="asc",
        headings_only=False,
        since=None,
        focus=None,
    )
    params.update(kwargs)
    return asyncio.run(srv._lightning_rod_render_symbols(**params))


def test_default_tier_slice_is_byte_identical_to_the_explicit_slice_tier(monkeypatch):
    plain = _run(monkeypatch, symbols=[_F, _G], root_uuids=[_ROOT])
    explicit = _run(monkeypatch, symbols=[_F, _G], root_uuids=[_ROOT], tier="slice")
    assert plain == explicit
    # And the stubbed extraction was never consulted on the slice tier.
    assert "snippet:" not in plain["preview_markdown"]


def test_metadata_tier_swaps_member_notes_for_the_fenced_header(monkeypatch):
    res = _run(monkeypatch, symbols=[_F, _G], root_uuids=[_ROOT], tier="metadata")
    assert res["success"] is True
    md = res["preview_markdown"]
    for uuid in (_F, _G):
        expected = format_snippet_header(
            f"snippet: /repo/{uuid[:8]}.py | ",
            ["# @beacon[", f"#   id={uuid},", "# ]", "def foo()" if uuid == _F else "def bar()", "    body"],
            1,
            5,
        )
        assert expected in md
        assert f"```{chr(10)}{expected}" in md
    # The original notes are replaced (measure-before-read).
    assert "body of foo" not in md
    assert "body of bar" not in md


def test_context_tier_body_and_trailer_reach_the_composed_markdown(monkeypatch):
    res = _run(monkeypatch, symbols=[_F], root_uuids=[_ROOT], tier="context", context_lines=0)
    md = res["preview_markdown"]
    assert "def foo()" in md
    # head = comment(3) + def(1) = lines 1-4; hidden = line 5 only.
    assert "… 1 more lines; start_line=5, end_line=5" in md


def test_tier_read_failure_keeps_the_original_note_and_warns(monkeypatch):
    res = _run(
        monkeypatch,
        symbols=[_F, _G],
        root_uuids=[_ROOT],
        tier="metadata",
        client=_make_client(fail_for={_G}),
    )
    assert res["success"] is True
    md = res["preview_markdown"]
    assert "## WARNING Symbol tier reads" in md
    assert "def bar()" in md  # _G keeps its original note
    assert "body of bar" in md  # (its note text)
    assert "disk read failed" in md
    # _F still got its tier body.
    expected_f = format_snippet_header(
        f"snippet: /repo/{_F[:8]}.py | ",
        ["# @beacon[", f"#   id={_F},", "# ]", "def foo()", "    body"],
        1,
        5,
    )
    assert expected_f in md


def test_tier_refuses_headings_only_output_file_and_bad_values(monkeypatch):
    for kwargs in (
        {"tier": "metadata", "headings_only": True},
        {"tier": "metadata", "output_file": "/tmp/x.md"},
        {"tier": "banana"},
        {"tier": "context", "context_lines": -1},
        {"tier": "context", "tail_lines": -2},
        {"tier": "context", "context_lines": True},
    ):
        res = _run(monkeypatch, symbols=[_F], root_uuids=[_ROOT], **kwargs)
        assert res["success"] is False, kwargs
        assert "error" in res, kwargs


def test_tier_is_refused_on_push_to_modal_at_the_impl_boundary(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(srv, "get_client", lambda: client)
    _stub_suppress_config(monkeypatch)
    res = asyncio.run(
        srv._lightning_rod_legacy_impl(
            mode="render_symbols",
            symbols=[_F],
            root_uuids=[_ROOT],
            push_to_modal=True,
            tier="metadata",
        )
    )
    assert res["success"] is False
    assert "MCP-return only" in res["error"]


# ---------------------------------------------------------------------------
# Registry surface: the tier rides the shared symbols_properties vocabulary
# (render_symbols AND render_nodes), and _call forwards it to the impl.
# ---------------------------------------------------------------------------


def test_registry_schema_publishes_the_shared_tier_vocabulary():
    registry = srv.build_lightning_rod_registry()
    for verb in ("render_symbols", "render_nodes"):
        spec = registry._ops[verb]
        props = spec.params_schema["properties"]
        assert list(props["tier"]["enum"]) == ["slice", "metadata", "context"]
        assert "context_lines" in props and "tail_lines" in props
    # The vocabulary is documented ONCE: both verbs publish the identical
    # tier description (OperationSpec deep-freezes each schema, so compare
    # the text, not object identity).
    assert (
        registry._ops["render_symbols"].params_schema["properties"]["tier"]["description"]
        == registry._ops["render_nodes"].params_schema["properties"]["tier"]["description"]
    )


def test_call_forwards_tier_params_to_the_impl(monkeypatch):
    import workflowy_mcp.server as server_module

    calls = []

    async def spy(**kwargs):
        calls.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(server_module, "_lightning_rod_legacy_impl", spy)
    registry = srv.build_lightning_rod_registry()
    spec = registry._ops["render_symbols"]
    res = asyncio.run(spec.handler({"symbols": [_F], "root_handles": _ROOT, "tier": "context", "context_lines": 2, "tail_lines": 3}))
    assert res == {"success": True}
    assert calls[-1]["tier"] == "context"
    assert calls[-1]["context_lines"] == 2
    assert calls[-1]["tail_lines"] == 3


def test_typed_extraction_failure_keeps_siblings_and_current_disk_anchor():
    from workflowy_mcp.beacon_snippet import mark_snippet_error

    class Client:
        async def get_code_snippet_for_resolved_node(self, handle, node, nodes, context):
            if handle == "bad":
                raise mark_snippet_error(RuntimeError("vanished source"), file_path="bad.py",
                                         beacon_id="gone@0", resolved_node_id=handle)
            return {**_snippet_result(), "beacon_line": _DEF_LINE,
                    "location": {"anchor": {"outline_line": _DEF_LINE}}}

    shadow = {"id": "root", "children": [
        {"id": "bad", "note": "keep original", "children": []},
        {"id": "good", "note": "old", "children": []}]}
    cache = {"bad": {"id": "bad", "name": "bad"},
             "good": {"id": "good", "name": "good", "orig_lineno_start_unused": 999}}
    failures = asyncio.run(srv._apply_render_symbols_tier(
        shadow, cache, Client(), {"bad", "good"}, tier="context", context_lines=2, tail_lines=0))
    assert len(failures) == 1 and failures[0]["uuid"] == "bad"
    assert failures[0]["source_extraction"]["file_path"] == "bad.py"
    assert shadow["children"][0]["note"] == "keep original"
    assert "return x + 1" in shadow["children"][1]["note"]
    assert "999" not in shadow["children"][1]["note"]
