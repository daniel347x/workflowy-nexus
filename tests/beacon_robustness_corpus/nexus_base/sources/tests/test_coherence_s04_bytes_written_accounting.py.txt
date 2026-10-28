"""S04 Remediation Chain 5 (work-order item 4) — every bytes_written field
matches the actual file.

Several writers counted a pre-write string while appending a trailing
newline and/or allowing Windows newline translation, so the returned
``bytes_written`` could differ from ``len(Path(output_file).read_bytes())``.

Chain 5 applies the ``_render_lens_empty_result`` precedent (Remediation
Chain 4) to every output path returning ``bytes_written``:

- the live-DOM GLIMPSE output writer in ``api_client_nexus.py``;
- ``render_stage2_filter_sort``;
- ``_glimpse_dispatch_dom_capture``;
- ``render_symbols`` (``_lightning_rod_render_symbols``'s own disk write).

Each writer builds the final string ONCE (including any conditional
trailing-newline append), writes those EXACT bytes (``newline=""``
suppresses Windows CRLF translation — the composed UTF-8 bytes land
byte-identically on every platform), and reports the actual artifact
size.  Advisory framing remains exactly once; clean and hard-dirty paths
are both covered.

Required-test mutation note (step 4a): the equality tests below were
verified to FAIL against the pre-fix code (production stashed to the
a77e8ea baseline) with their predicted shapes — see the Chain-5 result
anchor for the full evidence table.
"""

from __future__ import annotations

import asyncio

from coherence_harness import make_boundary_capable, s04_boundary_status

from workflowy_mcp import server
from workflowy_mcp.cache_render_spec import RenderProjectionSpec
from workflowy_mcp.coherence import (
    COHERENCE_READ_ADVISORY_LINE,
    cache_operation_boundary,
)

HARD = s04_boundary_status(
    hard_dirty=True,
    hard_generation=7,
    hard_reason_code="verified_effect_unpublishable",
    hard_transaction_id="tx-advisory",
)


def _double(nodes=(), status=None):
    client = object.__new__(server.WorkFlowyClient)
    return make_boundary_capable(client, nodes=nodes, boundary_status=status)


def _actual_bytes(path) -> bytes:
    from pathlib import Path

    return Path(path).read_bytes()


# ---------------------------------------------------------------------------
# render_stage2_filter_sort (the shared cache-render tail)
# ---------------------------------------------------------------------------


def _stage2_forest():
    return {
        "id": "root",
        "name": "Root",
        "children": [{"id": "leaf", "name": "Leaf", "note": "body", "children": []}],
    }


def _stage2_cache():
    return {
        "root": {"id": "root", "name": "Root", "priority": 100},
        "leaf": {"id": "leaf", "name": "Leaf", "priority": 100, "note": "body"},
    }


def test_stage2_tail_bytes_written_matches_actual_file_hard_dirty(monkeypatch, tmp_path):
    out_file = tmp_path / "out.md"
    spec = RenderProjectionSpec(root_uuid="root", as_markdown=True)

    async def _run():
        client = _double(status=HARD)
        monkeypatch.setattr(server, "get_client", lambda: client)
        async with cache_operation_boundary(client, "test-render"):
            return server.render_stage2_filter_sort(
                spec=spec,
                final_subtree=_stage2_forest(),
                cache_by_id=_stage2_cache(),
                output_file=str(out_file),
                source_meta={"_source": "test"},
            )

    summary = asyncio.run(_run())
    data = _actual_bytes(out_file)
    # THE REQUIRED EQUALITY.
    assert summary["bytes_written"] == len(data)
    # Advisory framing remains exactly once (text + structured).
    assert data.startswith(COHERENCE_READ_ADVISORY_LINE.encode("utf-8"))
    assert data.count(b"COHERENCE FAILURE") == 1
    assert summary["coherence_advisory"]["status"] == "hard_dirty"
    # Exact-byte write: no Windows CRLF translation anywhere.
    assert b"\r\n" not in data


def test_stage2_tail_bytes_written_matches_actual_file_clean(monkeypatch, tmp_path):
    out_file = tmp_path / "out.md"
    spec = RenderProjectionSpec(root_uuid="root", as_markdown=True)

    async def _run():
        client = _double(status=None)
        monkeypatch.setattr(server, "get_client", lambda: client)
        async with cache_operation_boundary(client, "test-render"):
            return server.render_stage2_filter_sort(
                spec=spec,
                final_subtree=_stage2_forest(),
                cache_by_id=_stage2_cache(),
                output_file=str(out_file),
                source_meta={"_source": "test"},
            )

    summary = asyncio.run(_run())
    data = _actual_bytes(out_file)
    assert summary["bytes_written"] == len(data)
    assert "coherence_advisory" not in summary
    assert not data.startswith(COHERENCE_READ_ADVISORY_LINE.encode("utf-8"))
    assert b"\r\n" not in data
    assert b"Leaf" in data


def test_stage2_tail_no_trailing_newline_source_case(monkeypatch, tmp_path):
    """Required test (b)/(c): a composed string WITHOUT a trailing newline
    is written exactly (no silent append on this writer), with no CRLF
    translation, and bytes_written equals the actual file bytes."""
    out_file = tmp_path / "out.md"
    spec = RenderProjectionSpec(root_uuid="root", as_markdown=True)

    async def _run():
        client = _double(status=None)
        client._build_markdown_preview_from_tree = lambda *a, **k: "line1\nline2"
        monkeypatch.setattr(server, "get_client", lambda: client)
        async with cache_operation_boundary(client, "test-render"):
            return server.render_stage2_filter_sort(
                spec=spec,
                final_subtree=_stage2_forest(),
                cache_by_id=_stage2_cache(),
                output_file=str(out_file),
                source_meta={"_source": "test"},
            )

    summary = asyncio.run(_run())
    data = _actual_bytes(out_file)
    assert data == b"line1\nline2"  # exact bytes, no append, no CRLF
    assert summary["bytes_written"] == len(data)


# ---------------------------------------------------------------------------
# _glimpse_dispatch_dom_capture
# ---------------------------------------------------------------------------

_ROOT = "33333333-1111-bbbb-cccc-000000000001"
_CHILD_A = "33333333-2222-bbbb-cccc-000000000002"

_CAPTURE_NODES = [
    {"id": _ROOT, "name": "Frozen Root", "note": "", "parent_id": None,
     "priority": 0, "createdAt": 1000, "modifiedAt": 1000},
    {"id": _CHILD_A, "name": "Alpha point", "note": "body A", "parent_id": _ROOT,
     "priority": 0, "createdAt": 2000, "modifiedAt": 2000},
]


def _captured_tree():
    return {
        "root": {"id": _ROOT, "name": "Frozen Root", "note": ""},
        "children": [
            {"id": _CHILD_A, "name": "Alpha point", "note": "body A", "children": []},
        ],
    }


def _run_dom_capture(monkeypatch, tmp_path, *, status, cache_sourced, name):
    out_file = tmp_path / name

    async def _run():
        client = _double(nodes=_CAPTURE_NODES, status=status)
        monkeypatch.setattr(server, "get_client", lambda: client)
        async with cache_operation_boundary(client, "test-dom-capture-file"):
            return await server._glimpse_dispatch_dom_capture(
                captured_tree=_captured_tree(),
                root_uuid_hint=_ROOT,
                focus=None,
                as_markdown=True,
                headings_only=False,
                sort_by=None,
                sort_direction="asc",
                since=None,
                output_file=str(out_file),
                cache_sourced=cache_sourced,
            )

    summary = asyncio.run(_run())
    return summary, _actual_bytes(out_file)


def test_dom_capture_bytes_written_matches_actual_file_cache_sourced_hard_dirty(
    monkeypatch, tmp_path
):
    summary, data = _run_dom_capture(
        monkeypatch, tmp_path, status=HARD, cache_sourced=True, name="dirty.md"
    )
    assert summary["bytes_written"] == len(data)
    # Advisory framing exactly once (text + structured).
    assert data.startswith(COHERENCE_READ_ADVISORY_LINE.encode("utf-8"))
    assert data.count(b"COHERENCE FAILURE") == 1
    assert summary["coherence_advisory"]["status"] == "hard_dirty"
    assert b"\r\n" not in data


def test_dom_capture_bytes_written_matches_actual_file_dom_frozen_hard_dirty(
    monkeypatch, tmp_path
):
    summary, data = _run_dom_capture(
        monkeypatch, tmp_path, status=HARD, cache_sourced=False, name="frozen.md"
    )
    assert summary["bytes_written"] == len(data)
    assert b"COHERENCE FAILURE" not in data
    assert "coherence_advisory" not in summary
    assert b"\r\n" not in data


def test_dom_capture_bytes_written_matches_actual_file_clean(monkeypatch, tmp_path):
    summary, data = _run_dom_capture(
        monkeypatch, tmp_path, status=None, cache_sourced=False, name="clean.md"
    )
    assert summary["bytes_written"] == len(data)
    assert "coherence_advisory" not in summary
    assert b"\r\n" not in data
    assert b"Alpha point" in data


# ---------------------------------------------------------------------------
# render_symbols (_lightning_rod_render_symbols's own disk write)
# ---------------------------------------------------------------------------

_RS_ROOT = "bbbbbbbb-0000-0000-0000-000000000001"
_RS_F = "bbbbbbbb-0000-0000-0000-000000000002"

_RS_CACHE_NODES = [
    {"id": _RS_ROOT, "name": "Symbols Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _RS_F, "name": "def foo()", "note": "body of foo", "parent_id": _RS_ROOT, "priority": 0},
]


def _rs_client(status):
    client = _double(nodes=_RS_CACHE_NODES, status=status)

    async def _ruf(*, file_path=None, symbol=None, symbol_kind="auto"):
        return {"candidates": []}

    client.read_uuid_from_symbol = _ruf
    return client


def _stub_rs_config(monkeypatch):
    async def _cfg(_target_tab):
        return [], None, {}, [], {}

    monkeypatch.setattr(server, "_resolve_slice_group_config", _cfg)


def _run_render_symbols(monkeypatch, tmp_path, *, status, symbols, name):
    out_file = tmp_path / name
    client = _rs_client(status)
    monkeypatch.setattr(server, "get_client", lambda: client)
    _stub_rs_config(monkeypatch)

    async def _run():
        async with cache_operation_boundary(client, "test-render-symbols"):
            return await server._lightning_rod_render_symbols(
                symbols=symbols,
                root_uuids=[_RS_ROOT],
                favor="uuid",
                as_markdown=True,
                output_file=str(out_file),
                sort_by=None,
                sort_direction="asc",
                headings_only=False,
                since=None,
                focus=None,
            )

    summary = asyncio.run(_run())
    assert summary["success"] is True, summary
    return summary, _actual_bytes(out_file)


def test_render_symbols_bytes_written_matches_actual_file_hard_dirty(
    monkeypatch, tmp_path
):
    # Include an unresolved symbol so the in-band report is appended BEFORE
    # the write (the report-append path owns this writer).
    summary, data = _run_render_symbols(
        monkeypatch, tmp_path, status=HARD,
        symbols=[_RS_F, "does_not_exist_fn"], name="symbols_dirty.md",
    )
    assert summary["bytes_written"] == len(data)
    # Advisory framing exactly once (the shared tail prefixed the preview;
    # this writer must not add a second).
    assert data.startswith(COHERENCE_READ_ADVISORY_LINE.encode("utf-8"))
    assert data.count(b"COHERENCE FAILURE") == 1
    assert summary["coherence_advisory"]["status"] == "hard_dirty"
    assert b"foo" in data
    assert b"\r\n" not in data


def test_render_symbols_bytes_written_matches_actual_file_clean(monkeypatch, tmp_path):
    summary, data = _run_render_symbols(
        monkeypatch, tmp_path, status=None, symbols=[_RS_F], name="symbols_clean.md"
    )
    assert summary["bytes_written"] == len(data)
    assert "coherence_advisory" not in summary
    assert b"\r\n" not in data
    assert b"foo" in data


def test_render_symbols_no_trailing_newline_source_case(monkeypatch, tmp_path):
    """Required test (b)/(c) for the append-owning writer: a composed
    Markdown WITHOUT a trailing newline gets the conditional append — and
    the append is COUNTED, with exact bytes (no CRLF translation)."""
    client = _rs_client(None)
    client._build_markdown_preview_from_tree = lambda *a, **k: "no-newline"
    monkeypatch.setattr(server, "get_client", lambda: client)
    _stub_rs_config(monkeypatch)
    out_file = tmp_path / "symbols_no_newline.md"

    async def _run():
        async with cache_operation_boundary(client, "test-render-symbols"):
            return await server._lightning_rod_render_symbols(
                symbols=[_RS_F],
                root_uuids=[_RS_ROOT],
                favor="uuid",
                as_markdown=True,
                output_file=str(out_file),
                sort_by=None,
                sort_direction="asc",
                headings_only=False,
                since=None,
                focus=None,
            )

    summary = asyncio.run(_run())
    assert summary["success"] is True, summary
    data = _actual_bytes(out_file)
    assert data == b"no-newline\n"  # the append landed and is exact
    assert summary["bytes_written"] == len(data)


# ---------------------------------------------------------------------------
# The live-DOM GLIMPSE output writer (api_client_nexus.py)
# ---------------------------------------------------------------------------


class _FakeWsConnection:
    def __init__(self):
        self.sent = None

    async def send(self, payload):
        self.sent = payload


class _FakeWsQueue:
    """The correlated request-id demux the live-DOM GLIMPSE prefers."""

    def __init__(self, response: dict):
        self._response = response

    def register_correlated_waiter(self, request_id):
        pass

    async def wait_for_correlated(self, request_id, timeout_s):
        return self._response

    def unregister_correlated_waiter(self, request_id):
        pass


def _live_dom_response() -> dict:
    return {
        "success": True,
        "request_id": "whatever",
        "root": {"id": "root", "name": "Root", "note": ""},
        "children": [
            {"id": "a", "name": "Alpha", "note": "", "children": []},
        ],
    }


def _run_live_dom_glimpse(monkeypatch, tmp_path, *, status, name, compose=None):
    out_file = tmp_path / name
    client = _double(nodes=[], status=status)
    if compose is not None:
        client._build_markdown_preview_from_tree = compose

    async def _run():
        async with cache_operation_boundary(client, "test-live-dom"):
            return await client.workflowy_glimpse(
                output_file=str(out_file),
                as_markdown=True,
                _ws_connection=_FakeWsConnection(),
                _ws_queue=_FakeWsQueue(_live_dom_response()),
            )

    result = asyncio.run(_run())
    assert result["success"] is True, result
    return result, _actual_bytes(out_file)


def test_live_dom_writer_bytes_written_matches_actual_file_clean(monkeypatch, tmp_path):
    result, data = _run_live_dom_glimpse(
        monkeypatch, tmp_path, status=None, name="live_clean.md"
    )
    assert result["bytes_written"] == len(data)
    assert b"\r\n" not in data
    assert b"Alpha" in data
    # A live read is never advised (even though a boundary exists).
    assert "coherence_advisory" not in result
    assert b"COHERENCE FAILURE" not in data


def test_live_dom_writer_bytes_written_matches_actual_file_hard_dirty_unadvised(
    monkeypatch, tmp_path
):
    """The live-DOM GLIMPSE is a pure LIVE read — never advised, even under
    hard dirt; the equality still holds."""
    result, data = _run_live_dom_glimpse(
        monkeypatch, tmp_path, status=HARD, name="live_dirty.md"
    )
    assert result["bytes_written"] == len(data)
    assert b"COHERENCE FAILURE" not in data
    assert "coherence_advisory" not in result
    assert b"\r\n" not in data


def test_live_dom_writer_no_trailing_newline_source_case(monkeypatch, tmp_path):
    """Required test (b)/(c) for this writer: a preview WITHOUT a trailing
    newline gets the conditional append — counted — with exact bytes."""
    result, data = _run_live_dom_glimpse(
        monkeypatch, tmp_path, status=None, name="live_no_newline.md",
        compose=lambda *a, **k: "no-newline",
    )
    assert data == b"no-newline\n"
    assert result["bytes_written"] == len(data)


def test_live_dom_writer_trailing_newline_no_append(monkeypatch, tmp_path):
    result, data = _run_live_dom_glimpse(
        monkeypatch, tmp_path, status=None, name="live_newline.md",
        compose=lambda *a, **k: "has-newline\n",
    )
    assert data == b"has-newline\n"
    assert result["bytes_written"] == len(data)
