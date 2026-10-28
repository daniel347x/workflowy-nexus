"""U01 §9.1 — pure tests for the snippet_view module (numbering, guard, windows).

Fixture: a 200-line file (``L1``..``L200``), symbol span 51–150 unless stated.
"""

from __future__ import annotations

import pytest

from workflowy_mcp import snippet_view
from workflowy_mcp.mcp_api_registry import OperationValidationError
from workflowy_mcp.note_lines import format_note_lines
from workflowy_mcp.snippet_view import (
    render_snippet_view,
    normalize_snippet_view_params,
)


def _lines():
    return [f"L{i}" for i in range(1, 201)]


def _result(lines, s, e, path="X.py"):
    return {
        "file_lines": lines,
        "core_start_line": s,
        "core_end_line": e,
        "file_path": path,
    }


def _norm(**params):
    return normalize_snippet_view_params(params)


def _code(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except OperationValidationError as exc:
        return exc.code
    raise AssertionError("expected OperationValidationError")


def test_short_span_defaults_number_all_rows():
    lines = _lines()
    out = render_snippet_view(_result(lines, 51, 60), _norm())
    header, *rows = out.split("\n")
    assert header == "[snippet: X.py | lines 51-60 of 200 | 10 lines, 40 bytes, ~14 tokens]"
    assert rows == format_note_lines(lines[50:60], start_line=51, width=2).split("\n")
    assert rows[0] == "51 | L51" and rows[-1] == "60 | L60"
    assert len(rows) == 10 and not any("..." in row for row in rows)


def test_guard_shows_head_and_tail_with_one_elision_row():
    lines = _lines()
    out = render_snippet_view(_result(lines, 51, 150), _norm())
    header, *rows = out.split("\n")
    assert header == (
        "[snippet: X.py | lines 51-150 of 200 | 100 lines, 451 bytes, ~151 tokens]"
    )
    expected = (
        format_note_lines(lines[50:98], start_line=51, width=3).split("\n")
        + [
            "... | ... 36 lines not shown (99-134); "
            "read them with start_line=99, end_line=134"
        ]
        + format_note_lines(lines[134:150], start_line=135, width=3).split("\n")
    )
    assert rows == expected
    assert rows[0] == " 51 | L51" and rows[47] == " 98 | L98"
    assert rows[49] == "135 | L135" and rows[-1] == "150 | L150"
    assert len(rows) == 48 + 1 + 16


def test_guard_disabled_by_max_lines_zero():
    lines = _lines()
    out = render_snippet_view(_result(lines, 51, 150), _norm(max_lines=0))
    header, *rows = out.split("\n")
    assert header.startswith("[snippet: X.py | lines 51-150 of 200 |")
    assert len(rows) == 100 and not any(row.startswith("...") for row in rows)


def test_guard_threshold_edge_at_64_and_65_lines():
    lines = _lines()
    out64 = render_snippet_view(_result(lines, 51, 114), _norm())
    assert len(out64.split("\n")) == 65  # header + 64 rows, no guard
    out65 = render_snippet_view(_result(lines, 51, 115), _norm())
    _, *rows = out65.split("\n")
    assert "... | ... 1 lines not shown (99-99); read them with start_line=99, end_line=99" in rows
    assert len(rows) == 48 + 1 + 16


def test_explicit_limit_alone_beats_the_guard():
    lines = _lines()
    out = render_snippet_view(_result(lines, 51, 150), _norm(limit=5))
    header, *rows = out.split("\n")
    assert header.startswith("[snippet: X.py | lines 51-150 of 200 |")
    assert rows == ["51 | L51", "52 | L52", "53 | L53", "54 | L54", "55 | L55"]


def test_relative_numbering_and_elision_hint():
    lines = _lines()
    out = render_snippet_view(_result(lines, 51, 150), _norm(numbering="relative"))
    header, *rows = out.split("\n")
    assert header == (
        "[snippet: X.py | lines 51-150 of 200 | 100 lines, 451 bytes, ~151 tokens"
        " | relative 1 = line 51]"
    )
    assert rows[0] == "  1 | L51" and rows[47] == " 48 | L98"
    assert rows[48] == (
        "... | ... 36 lines not shown (49-84); read them with start_line=49, end_line=84"
    )
    assert rows[49] == " 85 | L135" and rows[-1] == "100 | L150"
    assert len(rows) == 65


def test_relative_window_reaches_above_the_span():
    lines = _lines()
    out = render_snippet_view(
        _result(lines, 51, 150),
        _norm(numbering="relative", start_line=-2, end_line=3),
    )
    _, *rows = out.split("\n")
    assert rows == [
        "-2 | L48",
        "-1 | L49",
        " 0 | L50",
        " 1 | L51",
        " 2 | L52",
        " 3 | L53",
    ]


def test_file_window_outside_the_span_is_honored():
    lines = _lines()
    out = render_snippet_view(_result(lines, 51, 150), _norm(start_line=40, end_line=45))
    _, *rows = out.split("\n")
    assert rows == [f"{n} | L{n}" for n in range(40, 46)]

    out = render_snippet_view(_result(lines, 51, 150), _norm(start_line=160))
    _, *rows = out.split("\n")
    assert rows == ["160 | L160"]


def test_context_adds_lines_outside_the_guard_count():
    lines = _lines()
    out = render_snippet_view(_result(lines, 51, 150), _norm(context=2))
    _, *rows = out.split("\n")
    assert rows[0] == " 49 | L49" and rows[49] == " 98 | L98"
    assert "... | ... 36 lines not shown (99-134); read them with start_line=99, end_line=134" in rows
    assert rows[-1] == "152 | L152"
    assert len(rows) == 50 + 1 + 18


def test_clamping_at_file_edges():
    lines = _lines()
    out = render_snippet_view(_result(lines, 1, 10), _norm(context=5))
    _, *rows = out.split("\n")
    assert rows == format_note_lines(lines[0:15], start_line=1).split("\n")
    out = render_snippet_view(_result(lines, 195, 200), _norm(context=5))
    _, *rows = out.split("\n")
    assert rows == format_note_lines(lines[189:200], start_line=190).split("\n")


def test_numbering_none_guard_keeps_sentence_row_without_gutter():
    lines = _lines()
    out = render_snippet_view(_result(lines, 51, 150), _norm(numbering="none"))
    rows = out.split("\n")
    assert rows == (
        lines[50:98]
        + [
            "... 36 lines not shown (99-134); read them with start_line=99, end_line=134"
        ]
        + lines[134:150]
    )


@pytest.mark.parametrize("span", [(51, 60), (1, 10), (195, 200)])
def test_legacy_parity_none_max_lines_zero_context_twenty(span):
    lines = _lines()
    s, e = span
    out = render_snippet_view(
        _result(lines, s, e), _norm(numbering="none", max_lines=0, context=20)
    )
    assert out == "\n".join(lines[max(1, s - 20) - 1 : min(200, e + 20)])


@pytest.mark.parametrize("numbering", ["file", "relative", "none"])
def test_size_only_returns_the_header_alone(numbering):
    lines = _lines()
    out = render_snippet_view(
        _result(lines, 51, 150), _norm(numbering=numbering, size_only=True)
    )
    expected = (
        "[snippet: X.py | lines 51-150 of 200 | 100 lines, 451 bytes, ~151 tokens]"
    )
    if numbering == "relative":
        expected = expected[:-1] + " | relative 1 = line 51]"
    assert out == expected


def test_empty_file_header_and_none_mode():
    lines = _lines()
    out = render_snippet_view(_result([], 1, 1), _norm())
    assert out == "[snippet: X.py | empty file]"
    out = render_snippet_view(_result([], 1, 1), _norm(numbering="none"))
    assert out == ""
    out = render_snippet_view(_result([], 1, 1), _norm(numbering="none", size_only=True))
    assert out == "[snippet: X.py | empty file]"


def test_normalizer_validation_codes():
    assert _code(_norm, numbering="bogus") == "invalid_enum_value"
    assert _code(_norm, coordinates="bogus") == "invalid_enum_value"
    assert _code(_norm, start_line=0) == "invalid_parameter_value"
    assert _code(_norm, end_line=0) == "invalid_parameter_value"
    assert _code(_norm, start_line=True) == "invalid_parameter_type"
    assert _code(_norm, start_line="5") == "invalid_parameter_type"
    assert _code(_norm, limit=0) == "invalid_parameter_value"
    assert _code(_norm, context=-1) == "invalid_parameter_value"
    assert _code(_norm, max_lines=-1) == "invalid_parameter_value"
    assert _code(_norm, size_only="yes") == "invalid_parameter_type"
    assert _code(_norm, end_line=5, limit=5) == "invalid_parameter_combination"
    assert _code(_norm, size_only=True, limit=5) == "invalid_parameter_combination"
    assert _code(_norm, size_only=True, start_line=5) == "invalid_parameter_combination"
    assert _code(_norm, start_line=10, end_line=5) == "invalid_parameter_value"


def test_renderer_window_bounds_codes():
    lines = _lines()
    assert (
        _code(render_snippet_view, _result(lines, 51, 150), _norm(start_line=201))
        == "invalid_parameter_value"
    )
    assert (
        _code(
            render_snippet_view,
            _result(lines, 51, 150),
            _norm(coordinates="relative", end_line=-250),
        )
        == "invalid_parameter_value"
    )


def test_missing_line_data_raises_key_error():
    with pytest.raises(KeyError):
        render_snippet_view({"success": True, "snippet": "x"}, _norm())


def test_numbered_rows_come_from_the_one_formatter(monkeypatch):
    calls = []
    real = snippet_view.format_note_lines

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(snippet_view, "format_note_lines", spy)
    out = render_snippet_view(_result(_lines(), 51, 60), _norm())
    assert calls, "numbered output must reuse format_note_lines"
    assert calls[0][1]["start_line"] == 51 and calls[0][1]["width"] == 2
    assert "51 | L51" in out


# ---------------------------------------------------------------------------
# Integration: beacon.snippet through the registry (§9.1 items 17–20, 22)
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

from workflowy_mcp import server  # noqa: E402
from workflowy_mcp.mcp_api_registry import dispatch_operation  # noqa: E402


@pytest.fixture(autouse=True)
def _no_workflowy_tab_suppress_list(monkeypatch):
    """NBBW [ap:3ACUQS]: beacon symbol resolution reads the live #wfy-suppress
    list from a Workflowy tab; these tests have no tab, so it is stubbed empty."""

    async def _empty():
        return []

    monkeypatch.setattr("workflowy_mcp.server._beacon_symbol_suppress_tags", _empty)


_UUID = "12345678-1234-1234-1234-123456789abc"


def _client_dict(context_width=0):
    lines = _lines()
    return {
        "success": True,
        "snippet": "\n".join(lines[99:110]),
        "file_path": "X.py",
        "file_lines": lines,
        "core_start_line": 100,
        "core_end_line": 110,
        "start_line": max(1, 100 - context_width),
        "end_line": min(200, 110 + context_width),
        "beacon_line": 100,
    }


def _snippet_client(calls):
    class Client:
        async def read_text_snippet_by_symbol(self, *, file_path, symbol, symbol_kind, context, suppress_tags=None, scope_root_ids=None):
            calls.append(
                {
                    "file_path": file_path,
                    "symbol": symbol,
                    "symbol_kind": symbol_kind,
                    "context": context,
                }
            )
            return _client_dict()

    return Client()


def test_symbol_leg_default_numbered_with_render_context(monkeypatch):
    """§9.1 item 17 — defaults number rows; context moves to the render layer."""
    calls = []
    monkeypatch.setattr(server, "get_client", lambda: _snippet_client(calls))
    monkeypatch.setattr(server, "_rate_limiter", None)
    registry = server.build_beacon_registry()

    out = asyncio.run(
        dispatch_operation(
            registry, "snippet", {"source_handle": "my_func", "context": 7}
        )
    )

    assert out.startswith("[snippet: X.py | lines")
    assert "100 | L100" in out and "110 | L110" in out
    # The client was called with context=0 even though the caller passed 7...
    assert calls[0]["context"] == 0
    # ...and the 7 context lines are rendered around the span instead.
    assert " 93 | L93" in out and "117 | L117" in out


def test_uuid_leg_uses_zero_context_and_explicit_uuid(monkeypatch):
    """§9.1 item 18 — the UUID leg resolves through the shared result impl."""
    calls = []

    async def reader(input_str, context, *, explicit_uuid=False):
        calls.append((input_str, context, explicit_uuid))
        return _client_dict()

    monkeypatch.setattr(server, "_read_text_snippet_result_impl", reader)
    registry = server.build_beacon_registry()

    out = asyncio.run(
        dispatch_operation(registry, "snippet", {"source_handle": _UUID})
    )

    assert calls == [(_UUID, 0, True)]
    assert out.startswith("[snippet: X.py | lines")
    assert "100 | L100" in out


def test_missing_line_data_is_a_source_read_failure(monkeypatch):
    """§9.1 item 19 — an old fake without line data fails loudly, no traceback."""

    class OldClient:
        async def read_text_snippet_by_symbol(self, *, file_path, symbol, symbol_kind, context, suppress_tags=None, scope_root_ids=None):
            return {"success": True, "snippet": "x"}

    monkeypatch.setattr(server, "get_client", lambda: OldClient())
    monkeypatch.setattr(server, "_rate_limiter", None)
    registry = server.build_beacon_registry()

    result = asyncio.run(
        dispatch_operation(registry, "snippet", {"source_handle": "my_func"})
    )
    assert result["error"]["code"] == "source_read_failed"
    assert "lacks the line data" in result["error"]["message"]


def test_parameter_errors_reach_the_agent_as_envelopes(monkeypatch):
    """§9.1 item 20 — invalid windows/enums return typed error envelopes."""
    monkeypatch.setattr(server, "get_client", lambda: _snippet_client([]))
    monkeypatch.setattr(server, "_rate_limiter", None)
    registry = server.build_beacon_registry()

    past = asyncio.run(
        dispatch_operation(
            registry, "snippet", {"source_handle": "fn", "start_line": 0}
        )
    )
    assert past["error"]["code"] == "invalid_parameter_value"

    combo = asyncio.run(
        dispatch_operation(
            registry,
            "snippet",
            {"source_handle": "fn", "end_line": 5, "limit": 5},
        )
    )
    assert combo["error"]["code"] == "invalid_parameter_combination"

    bogus = asyncio.run(
        dispatch_operation(
            registry, "snippet", {"source_handle": "fn", "numbering": "bogus"}
        )
    )
    assert bogus["error"]["code"] == "invalid_enum_value"


def test_legacy_raw_text_wrapper_paths_unchanged(monkeypatch):
    """§9.1 item 22 (compat) — _read_text_snippet_impl still returns raw text."""
    class Client:
        def __init__(self):
            self.candidates_called = 0

        async def read_text_snippet_by_symbol(self, *, file_path=None, symbol=None, symbol_kind="auto", context=10, return_candidates_only=False, suppress_tags=None, scope_root_ids=None):
            if return_candidates_only:
                self.candidates_called += 1
                return {
                    "success": True,
                    "match_count": 1,
                    "candidates": [{"uuid": _UUID, "name": "fn"}],
                }
            return {"success": True, "snippet": "SYMBOL RAW"}

        async def beacon_get_code_snippet(self, *, beacon_node_id, context=10):
            if beacon_node_id == _UUID and self.candidates_called:
                return {"success": True, "snippet": "FALLBACK RAW"}
            raise RuntimeError("uuid miss")

    client = Client()
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)

    # Auto-symbol path.
    assert asyncio.run(server._read_text_snippet_impl("fn", 10)) == "SYMBOL RAW"
    # UUID success path.
    assert (
        asyncio.run(server._read_text_snippet_impl(_UUID, 10, explicit_uuid=True))
        == "FALLBACK RAW"
    )
    # Unique-fallback path: UUID miss -> candidates -> resolved UUID read.
    client.candidates_called = 0
    assert (
        asyncio.run(server._read_text_snippet_impl(_UUID, 10, explicit_uuid=True))
        == "FALLBACK RAW"
    )
