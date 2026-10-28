"""UnitSpec [ap:1T7JZ0] §9.2 — per-site integration surfaces for the
host-aware tool-path resolver, with the daemon's host SIMULATED (the ONE
monkeypatch seam ``tool_path_resolution._detect_host_raw``).

Covers, per site: the ``path_record`` attachment on the established ack
shapes; the P3 echo rule (argument-echo fields carry the REQUESTED string
byte-identically; derived-path fields carry RESOLVED values); resolution
BEFORE any side effect (P1); the exact-byte contract on resolved paths; the
per-§6.7 refusal surfaces (each in its ESTABLISHED error shape, writing
nothing); the async-etch resolved-forwarding exception; and the string
fallback's silent ToolPathError fall-through.

MUTATION SENSITIVITY (§9.3): against the pre-fix tree this file fails at
extraction — ``path_record`` is absent from every ack, the resolver module
does not exist, and the refused writes silently target the raw string.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from coherence_harness import (  # noqa: E402
    make_boundary_capable,
    make_coherence_root,
    s04_boundary_status,
)
from workflowy_mcp import server  # noqa: E402
from workflowy_mcp import tool_path_resolution as tpr  # noqa: E402
from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402
from workflowy_mcp.client import api_client_etch as etch_mod  # noqa: E402
from workflowy_mcp.client import api_client_nexus as nx  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402
from workflowy_mcp.coherence import (  # noqa: E402
    COHERENCE_READ_ADVISORY_LINE,
    cache_operation_boundary,
)
from workflowy_mcp.mcp_api_registry import dispatch_operation  # noqa: E402
from workflowy_mcp.models import NetworkError  # noqa: E402
from workflowy_mcp.models.node import WorkFlowyNode  # noqa: E402
from workflowy_mcp.tool_path_resolution import ToolPathError  # noqa: E402

HOSTS = ("windows", "wsl", "linux")

HARD = s04_boundary_status(
    hard_dirty=True,
    hard_generation=7,
    hard_reason_code="verified_effect_unpublishable",
    hard_transaction_id="tx-path-record",
)


@pytest.fixture
def host(monkeypatch):
    """Simulate the daemon's host family via the ONE detection seam."""

    def _set(name: str) -> str:
        assert name in HOSTS
        monkeypatch.setattr(tpr, "_detect_host_raw", lambda: name)
        return name

    return _set


def _run(coro):
    return asyncio.run(coro)


def _install_open_spy(monkeypatch, module, texts: dict | None = None):
    """Inject an open() spy into ONE module's globals.

    Writes answer with a fresh StringIO (and record the exact path + mode +
    kwargs, so the newline='' exact-byte contract is assertable); reads answer
    from ``texts`` (path → content) or raise FileNotFoundError. Returns the
    recorded-call list of (path, mode, kwargs).
    """
    calls: list[tuple] = []

    def spy_open(file, mode="r", *args, **kwargs):
        calls.append((str(file), mode, kwargs))
        if "w" in str(mode):
            return io.StringIO()
        content = (texts or {}).get(str(file))
        if content is None:
            raise FileNotFoundError(2, "No such file or directory", str(file))
        return io.StringIO(content)

    monkeypatch.setattr(module, "open", spy_open, raising=False)
    return calls


def _dispatch_node(verb: str, params: dict):
    return asyncio.run(
        dispatch_operation(
            server.build_node_registry(), verb, params,
            operation_boundary=server._mcp_operation_boundary,
            mutation_admission=server._mcp_mutation_admission,
            exception_mapper=server._mcp_exception_mapper,
            result_finalizer=server._mcp_operation_result_finalizer,
        )
    )


# ═════════════════════════════════════════════════════════════════════════
# A · node.read output_file (§13 row 1) — atomic write, ack record, refusal
#     before the API fetch, and the P3 echo under a translating host.
# ═════════════════════════════════════════════════════════════════════════

READ_UUID = "7b97b6a9-467c-43eb-9bab-90f736cedfe6"
READ_NAME = "[ap:FILE01] Specification 📊 #tag"


def _install_read_client(monkeypatch, note: str = "body"):
    node = WorkFlowyNode(id=READ_UUID, name=READ_NAME, note=note)
    client = make_boundary_capable(
        object.__new__(server.WorkFlowyClient),
        nodes=[{"id": READ_UUID, "name": READ_NAME, "note": "", "parent_id": None}],
        boundary_status=None,
    )
    client.get_node = AsyncMock(return_value=node)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    return client


def test_node_read_ack_carries_path_record_and_still_writes_atomically(
    monkeypatch, host, tmp_path
):
    host("linux")
    _install_read_client(monkeypatch, note="# note\n")
    dest = tmp_path / "out.md"
    result = _dispatch_node("read", {"node_handle": READ_UUID, "output_file": str(dest)})
    assert result["success"] is True
    # The established ack keys, unchanged, plus the ONE new key.
    assert result["output_file"] == str(dest)
    assert result["bytes_written"] == len("# note\n".encode("utf-8"))
    assert result["encoding"] == "utf-8"
    assert result["path_record"] == {
        "requested": str(dest), "resolved": str(dest),
        "translated": False, "host": "linux",
    }
    # Still the atomic temp+replace writer: exact bytes, NO temp residue.
    assert dest.read_bytes() == b"# note\n"
    assert list(tmp_path.iterdir()) == [dest]


def test_node_read_refusal_precedes_the_api_fetch_and_writes_nothing(
    monkeypatch, host, tmp_path
):
    """P1 + §6.7: ToolPathError propagates → the kernel operation_failed
    envelope; client.get_node is never reached; zero bytes anywhere."""
    host("windows")
    client = _install_read_client(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = _dispatch_node(
        "read", {"node_handle": READ_UUID, "output_file": "/tmp/opencode/x.md"}
    )
    assert result["success"] is False
    message = str(result["error"]["message"])
    assert "/tmp/opencode/x.md" in message
    assert "windows" in message and "/mnt/<drive>" in message
    client.get_node.assert_not_awaited()
    assert list(tmp_path.rglob("*")) == []


def test_node_read_p3_echo_and_resolved_atomic_target_under_translation(
    monkeypatch, host, tmp_path
):
    """P3/§9.2(c): the ack's output_file echoes the REQUESTED string
    byte-identically while path_record.resolved carries the translated form —
    and the atomic writer's temp dir + replace target are the RESOLVED
    destination."""
    host("windows")
    _install_read_client(monkeypatch, note="# note\n")
    requested = "/mnt/e/scratch/out.md"
    resolved = "E:\\scratch\\out.md"

    seen: dict = {}
    real_ntf = tempfile.NamedTemporaryFile

    def fake_ntf(*args, **kwargs):
        seen["dir"] = str(kwargs.get("dir"))
        kwargs = dict(kwargs, dir=str(tmp_path))  # land the temp inside the sandbox
        return real_ntf(*args, **kwargs)

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", fake_ntf)

    def fake_replace(src, dst):
        seen["dst"] = str(dst)
        os.rename(src, tmp_path / "out.md")

    monkeypatch.setattr(os, "replace", fake_replace)

    result = _dispatch_node("read", {"node_handle": READ_UUID, "output_file": requested})
    assert result["success"] is True
    assert result["output_file"] == requested  # REQUESTED, byte-identical
    assert result["path_record"] == {
        "requested": requested, "resolved": resolved,
        "translated": True, "host": "windows",
    }
    # The replace target is the RESOLVED string. (The temp dir derives from
    # Path(resolved).parent — 'E:\scratch' on a real Windows daemon, where
    # pathlib speaks NT; under POSIX simulation pathlib cannot split the
    # Windows form and yields ".", so only the target is asserted here.)
    assert seen["dst"] == resolved
    assert seen["dir"] in ("E:\\scratch", ".")
    assert (tmp_path / "out.md").read_bytes() == b"# note\n"


# ═════════════════════════════════════════════════════════════════════════
# B · render_stage2_filter_sort (§13 row 8) — the shared Markdown render tail
#     (glimpse.run cache · dock.render · slice_group · lightning render_tags/
#     render_search): record before advisory, byte identity, P3 echo.
# ═════════════════════════════════════════════════════════════════════════


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


def _stage2(monkeypatch, output_file, status=None):
    client = make_boundary_capable(
        object.__new__(server.WorkFlowyClient), nodes=(), boundary_status=status
    )
    monkeypatch.setattr(server, "get_client", lambda: client)
    spec = RenderProjectionSpec(root_uuid="root", as_markdown=True)

    async def _go():
        async with cache_operation_boundary(client, "test-render"):
            return server.render_stage2_filter_sort(
                spec=spec,
                final_subtree=_stage2_forest(),
                cache_by_id=_stage2_cache(),
                output_file=output_file,
                source_meta={"_source": "test"},
            )

    return asyncio.run(_go())


def test_render_tail_record_rides_before_advisory_and_bytes_match_file(
    monkeypatch, host, tmp_path
):
    host("linux")
    out = tmp_path / "out.md"
    summary = _stage2(monkeypatch, str(out), status=HARD)
    data = out.read_bytes()
    # The exact-byte contract survives on the resolved path.
    assert summary["bytes_written"] == len(data) == len(Path(summary["path_record"]["resolved"]).read_bytes())
    assert summary["path_record"] == {
        "requested": str(out), "resolved": str(out),
        "translated": False, "host": "linux",
    }
    # The record and the advisory coexist (the record is attached BEFORE
    # apply_coherence_read_advisory; the advisory adds only its own key).
    assert summary["coherence_advisory"]["status"] == "hard_dirty"
    assert data.startswith(COHERENCE_READ_ADVISORY_LINE.encode("utf-8"))
    assert summary["output_file"] == str(out)  # argument echo: REQUESTED


def test_render_tail_p3_echo_and_resolved_open_under_translation(monkeypatch, host):
    """§9.2(c): written['output_file'] echoes the REQUEST; the open() spy
    proves the bytes go to the RESOLVED path with newline='' (exact-byte)."""
    host("windows")
    calls = _install_open_spy(monkeypatch, tpr)
    summary = _stage2(monkeypatch, "/mnt/e/out/render.md")
    assert summary["success"] is True
    assert summary["output_file"] == "/mnt/e/out/render.md"       # REQUESTED echo
    assert summary["path_record"] == {
        "requested": "/mnt/e/out/render.md", "resolved": "E:\\out\\render.md",
        "translated": True, "host": "windows",
    }
    assert [(p, m) for p, m, _k in calls] == [("E:\\out\\render.md", "w")]
    assert calls[0][2].get("newline") == ""                        # exact-byte shape


def test_render_tail_refusal_is_the_established_write_failure_dict(monkeypatch, host):
    """§6.7: a refused tail path is caught into {success: false, error} —
    today's write-failure shape — and writes NOTHING."""
    host("windows")
    calls = _install_open_spy(monkeypatch, tpr)
    summary = _stage2(monkeypatch, "/tmp/opencode/render.md")
    assert summary["success"] is False
    assert "Failed to write output_file" in summary["error"]
    assert "/tmp/opencode/render.md" in summary["error"]
    assert "windows" in summary["error"]
    assert calls == []                                             # zero opens


# ═════════════════════════════════════════════════════════════════════════
# C · _lightning_rod_render_symbols' own disk write (§13 row 9)
# ═════════════════════════════════════════════════════════════════════════

_RS_ROOT = "bbbbbbbb-0000-0000-0000-000000000001"
_RS_F = "bbbbbbbb-0000-0000-0000-000000000002"
_RS_CACHE_NODES = [
    {"id": _RS_ROOT, "name": "Symbols Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _RS_F, "name": "def foo()", "note": "body of foo", "parent_id": _RS_ROOT, "priority": 0},
]


def _run_render_symbols(monkeypatch, output_file):
    client = make_boundary_capable(
        object.__new__(server.WorkFlowyClient), nodes=_RS_CACHE_NODES, boundary_status=None
    )

    async def _ruf(*, file_path=None, symbol=None, symbol_kind="auto"):
        return {"candidates": []}

    client.read_uuid_from_symbol = _ruf
    monkeypatch.setattr(server, "get_client", lambda: client)

    async def _cfg(_target_tab):
        return [], None, {}, [], {}

    monkeypatch.setattr(server, "_resolve_slice_group_config", _cfg)

    async def _go():
        async with cache_operation_boundary(client, "test-render-symbols"):
            return await server._lightning_rod_render_symbols(
                symbols=[_RS_F],
                root_uuids=[_RS_ROOT],
                favor="uuid",
                as_markdown=True,
                output_file=output_file,
                sort_by=None,
                sort_direction="asc",
                headings_only=False,
                since=None,
                focus=None,
            )

    return asyncio.run(_go())


def test_render_symbols_write_record_and_resolved_target(monkeypatch, host, tmp_path):
    host("linux")
    out = tmp_path / "symbols.md"
    summary = _run_render_symbols(monkeypatch, str(out))
    assert summary["success"] is True
    assert summary["bytes_written"] == len(out.read_bytes())
    assert summary["output_file"] == str(out)
    assert summary["path_record"]["resolved"] == str(out)
    assert summary["path_record"]["translated"] is False


def test_render_symbols_refusal_is_the_established_dict_and_writes_nothing(
    monkeypatch, host, tmp_path
):
    host("windows")
    monkeypatch.chdir(tmp_path)
    calls = _install_open_spy(monkeypatch, tpr)
    summary = _run_render_symbols(monkeypatch, "/tmp/symbols.md")
    assert summary["success"] is False
    assert "Failed to write output_file" in summary["error"]
    assert "/tmp/symbols.md" in summary["error"] and "windows" in summary["error"]
    assert calls == []
    assert list(tmp_path.rglob("*")) == []


# ═════════════════════════════════════════════════════════════════════════
# D · _glimpse_dispatch_dom_capture's write (§13 row 10)
# ═════════════════════════════════════════════════════════════════════════

_DC_ROOT = "33333333-1111-bbbb-cccc-000000000001"
_DC_A = "33333333-2222-bbbb-cccc-000000000002"
_DC_CACHE = [
    {"id": _DC_ROOT, "name": "Frozen Root", "note": "", "parent_id": None,
     "priority": 0, "createdAt": 1000, "modifiedAt": 1000},
    {"id": _DC_A, "name": "Alpha point", "note": "body A", "parent_id": _DC_ROOT,
     "priority": 0, "createdAt": 2000, "modifiedAt": 2000},
]


def _captured_tree():
    return {
        "root": {"id": _DC_ROOT, "name": "Frozen Root", "note": ""},
        "children": [{"id": _DC_A, "name": "Alpha point", "note": "body A", "children": []}],
    }


def _run_dom_capture(monkeypatch, output_file):
    client = make_boundary_capable(
        object.__new__(server.WorkFlowyClient), nodes=_DC_CACHE, boundary_status=None
    )
    monkeypatch.setattr(server, "get_client", lambda: client)

    async def _go():
        async with cache_operation_boundary(client, "test-dom-capture"):
            return await server._glimpse_dispatch_dom_capture(
                captured_tree=_captured_tree(),
                root_uuid_hint=_DC_ROOT,
                focus=None,
                as_markdown=True,
                headings_only=False,
                sort_by=None,
                sort_direction="asc",
                since=None,
                output_file=output_file,
                cache_sourced=False,
            )

    return asyncio.run(_go())


def test_dom_capture_write_record_and_bytes(monkeypatch, host, tmp_path):
    host("linux")
    out = tmp_path / "capture.md"
    summary = _run_dom_capture(monkeypatch, str(out))
    assert summary["success"] is True
    assert summary["bytes_written"] == len(out.read_bytes())
    assert b"Alpha point" in out.read_bytes()
    assert summary["output_file"] == str(out)
    assert summary["path_record"] == {
        "requested": str(out), "resolved": str(out),
        "translated": False, "host": "linux",
    }


def test_dom_capture_refusal_is_the_established_dict(monkeypatch, host, tmp_path):
    host("windows")
    monkeypatch.chdir(tmp_path)
    calls = _install_open_spy(monkeypatch, tpr)
    summary = _run_dom_capture(monkeypatch, "/tmp/capture.md")
    assert summary["success"] is False
    assert "Failed to write output_file" in summary["error"]
    assert calls == [] and list(tmp_path.rglob("*")) == []


# ═════════════════════════════════════════════════════════════════════════
# E · _render_lens_empty_result (§13 row 11)
# ═════════════════════════════════════════════════════════════════════════


def test_empty_lens_record_and_bytes(monkeypatch, host, tmp_path):
    host("linux")
    out = tmp_path / "empty.md"
    spec = RenderProjectionSpec(root_uuid="r", as_markdown=True)
    payload = server._render_lens_empty_result(
        spec=spec, output_file=str(out), source_meta={"_source": "test"}
    )
    assert payload["success"] is True and payload["empty_selection"] is True
    assert payload["bytes_written"] == len(out.read_bytes())
    assert payload["output_file"] == str(out)
    assert payload["path_record"]["translated"] is False


def test_empty_lens_refusal_is_the_established_dict(monkeypatch, host, tmp_path):
    host("windows")
    monkeypatch.chdir(tmp_path)
    calls = _install_open_spy(monkeypatch, tpr)
    spec = RenderProjectionSpec(root_uuid="r", as_markdown=True)
    payload = server._render_lens_empty_result(
        spec=spec, output_file="/tmp/empty.md", source_meta={"_source": "test"}
    )
    assert payload["success"] is False
    assert "Failed to write output_file" in payload["error"]
    assert "/tmp/empty.md" in payload["error"]
    assert calls == [] and list(tmp_path.rglob("*")) == []


# ═════════════════════════════════════════════════════════════════════════
# F · node.scry (§13 row 7) — _node_scry_impl → workflowy_scry →
#     bulk_export_to_file_impl: companions derive from RESOLVED, both return
#     dicts carry the record, terrain_file echoes REQUESTED, refusal fails
#     BEFORE the fetch.
# ═════════════════════════════════════════════════════════════════════════

_SCRY_FLAT_NODES = [
    {"id": "ROOT", "name": "Root Node", "note": "root note", "priority": 1},
    {"id": "CHILD", "name": "Child Node", "parent_id": "ROOT", "priority": 1},
]


def _install_scry_stubs(monkeypatch):
    """Real _node_scry_impl → real workflowy_scry → real
    bulk_export_to_file_impl; only the fetch is stubbed (and RECORDS, so the
    refusal-before-fetch ordering is assertable)."""
    fetch_calls: list = []
    client = make_boundary_capable(
        object.__new__(server.WorkFlowyClient), nodes=[], boundary_status=None
    )

    async def _no_export(*_a, **_kw):
        raise RuntimeError("export_nodes unavailable in this test")

    client.export_nodes = _no_export

    async def _export_impl(_client, node_id=None, **_kw):
        fetch_calls.append(node_id)
        return {
            "nodes": [dict(n) for n in _SCRY_FLAT_NODES],
            "_total_fetched_from_api": len(_SCRY_FLAT_NODES),
        }

    monkeypatch.setattr(etch_mod, "export_nodes_impl", _export_impl)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    return client, fetch_calls


def test_scry_summary_record_echoes_and_real_companions(monkeypatch, host, tmp_path):
    host("linux")
    _install_scry_stubs(monkeypatch)
    out = tmp_path / "scry_out.json"
    summary = _dispatch_node("scry", {"root_handle": "ROOT", "output_file": str(out)})
    assert summary["success"] is True and summary["mode"] == "file"
    # P3: terrain_file echoes the REQUESTED argument; markdown_file is the
    # DERIVED companion in resolved form; the record carries the resolution.
    assert summary["terrain_file"] == str(out)
    assert summary["markdown_file"] == str(tmp_path / "scry_out.md")
    assert summary["path_record"] == {
        "requested": str(out), "resolved": str(out),
        "translated": False, "host": "linux",
    }
    # The four files + keystone all landed, companions derived from resolved.
    assert (tmp_path / "scry_out.md").exists()
    assert (tmp_path / "scry_out.original.json").exists()
    assert (tmp_path / "scry_out.original.md").exists()
    terrain = json.loads(out.read_text(encoding="utf-8"))
    assert terrain["nodes"]


def test_scry_companions_derive_from_resolved_under_translation(monkeypatch, host):
    host("windows")
    _install_scry_stubs(monkeypatch)
    opened: list[tuple] = []
    real_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        opened.append((str(file), mode))
        if "w" in str(mode):
            return io.StringIO()
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(etch_mod, "open", spy_open, raising=False)
    copied: list[tuple] = []
    import shutil

    monkeypatch.setattr(shutil, "copy2", lambda src, dst: copied.append((str(src), str(dst))))

    summary = _dispatch_node(
        "scry", {"root_handle": "ROOT", "output_file": "/mnt/e/exports/scry.json"}
    )
    assert summary["success"] is True
    assert summary["terrain_file"] == "/mnt/e/exports/scry.json"   # REQUESTED echo
    assert summary["path_record"]["resolved"] == "E:\\exports\\scry.json"
    assert summary["path_record"]["translated"] is True
    assert summary["markdown_file"] == "E:\\exports\\scry.md"      # derived: RESOLVED
    paths = [p for p, _m in opened]
    assert "E:\\exports\\scry.json" in paths
    assert "E:\\exports\\scry.original.json" in paths
    assert "E:\\exports\\scry.md" in paths
    assert "E:\\exports\\scry.original.md" in paths
    assert not any(p.startswith("/mnt/") for p in paths)           # never the raw form
    assert copied and copied[0][0] == "E:\\exports\\scry.json"     # keystone source


def test_scry_refusal_propagates_before_the_fetch(monkeypatch, host, tmp_path):
    """§6.7 + P1: bulk_export's ToolPathError propagates as today's
    client-layer errors do — the kernel envelope carries the teaching message
    — and the export fetch never runs."""
    host("windows")
    _client, fetch_calls = _install_scry_stubs(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = _dispatch_node(
        "scry", {"root_handle": "ROOT", "output_file": "/tmp/scry.json"}
    )
    assert result["success"] is False
    message = str(result["error"]["message"])
    assert "/tmp/scry.json" in message and "windows" in message
    assert fetch_calls == []
    assert list(tmp_path.rglob("*")) == []


# ═════════════════════════════════════════════════════════════════════════
# G · glimpse.estate (§13 row 13) — the argument is translated ONCE, the
#     per-entry paths derive from RESOLVED, refusal precedes every render.
# ═════════════════════════════════════════════════════════════════════════

_EST_ROOT = "33333333-aaaa-bbbb-cccc-000000000001"
_EST_A = "33333333-aaaa-bbbb-cccc-000000000002"
_EST_CACHE = [
    {"id": "eeeeeeee-aaaa-bbbb-cccc-000000000001", "name": "[ap:ESTATE] Estate AP", "note": "", "parent_id": None},
    {"id": _EST_ROOT, "name": "Estate Root", "note": "root body", "parent_id": None, "priority": 0},
    {"id": _EST_A, "name": "Estate Child", "note": "child body", "parent_id": _EST_ROOT, "priority": 0},
]


def _install_estate_env(monkeypatch, note: str):
    client = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    client._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _EST_CACHE]
    monkeypatch.setattr(server, "get_client", lambda: client)
    # CF-11: the estate's entry-resolution map reads the ONE pinned source
    # (_anchor_cache_by_id), so the fake cache feeds THAT reader.
    monkeypatch.setattr(server, "_anchor_cache_by_id",
                        lambda: {n["id"]: dict(n) for n in _EST_CACHE})
    seen: dict = {}

    async def fake_read(node_id, as_raw_node=True, **kw):
        seen["node_id"] = node_id
        return types.SimpleNamespace(id=node_id, name="Estate AP", note=note)

    monkeypatch.setattr(server, "_node_read_impl", fake_read)
    async def fake_groups(target_tab):
        return [], None, {}, [], {}

    async def fake_vocab(target_tab):
        return ["wfy-ns"], {"local_postfixes": ["local"]}

    monkeypatch.setattr(server, "_resolve_slice_group_config", fake_groups)
    monkeypatch.setattr(server, "_read_anchor_dock_vocabulary", fake_vocab)
    return seen


def _estate_note() -> str:
    entries = [
        {"alias": "ONE", "title": "One title", "url": "https://workflowy.com/#/x",
         "spec": {"kind": "raw_root", "root_uuid": _EST_ROOT}},
        {"alias": "TWO", "title": "Two title", "url": "https://workflowy.com/#/y",
         "spec": {"kind": "raw_root", "root_uuid": _EST_A}},
    ]
    return "Estate note.\n\n```json\n" + json.dumps(entries) + "\n```\n"


def test_estate_pattern_translated_once_with_per_entry_resolved_paths(monkeypatch, host):
    host("windows")
    _install_estate_env(monkeypatch, _estate_note())

    # The existence pre-checks apply to the RESOLVED string: simulate the
    # resolved pattern parent existing on the daemon's host.
    real_isdir = os.path.isdir

    def fake_isdir(p):
        return True if str(p) == "C:\\tmp\\estate" else real_isdir(p)

    monkeypatch.setattr(os.path, "isdir", fake_isdir)

    # Count resolutions of the estate argument: exactly ONE, at the argument.
    real_resolve = server.resolve_tool_path
    estate_resolves: list = []

    def counting_resolve(path, **kw):
        res = real_resolve(path, **kw)
        if kw.get("context") == "glimpse.estate":
            estate_resolves.append(str(path))
        return res

    monkeypatch.setattr(server, "resolve_tool_path", counting_resolve)
    writes = _install_open_spy(monkeypatch, tpr)

    out = _run(server._glimpse_estate_impl(
        handle="[ap:ESTATE]",
        output_file="/mnt/c/tmp/estate/estate-{index}-{alias}.md",
    ))
    assert out["success"] is True, out
    assert out["path_record"] == {
        "requested": "/mnt/c/tmp/estate/estate-{index}-{alias}.md",
        "resolved": "C:\\tmp\\estate\\estate-{index}-{alias}.md",
        "translated": True, "host": "windows",
    }
    # ONE translation — at the argument, never per entry.
    assert estate_resolves == ["/mnt/c/tmp/estate/estate-{index}-{alias}.md"]
    # Per-entry paths derive from the RESOLVED base; the writes hit exactly those.
    paths = [w["path"] for w in out["output_files"]]
    assert len(paths) == 2
    assert all(p.startswith("C:\\tmp\\estate\\estate-") and p.endswith(".md") for p in paths), paths
    assert all("{" not in p for p in paths)
    assert [c[0] for c in writes] == paths


def test_estate_dir_form_real_files_and_record(monkeypatch, host, tmp_path):
    host("linux")
    _install_estate_env(monkeypatch, _estate_note())
    out = _run(server._glimpse_estate_impl(
        handle="[ap:ESTATE]", output_file=str(tmp_path) + os.sep
    ))
    assert out["success"] is True
    assert out["path_record"]["translated"] is False
    assert out["path_record"]["resolved"] == str(tmp_path) + os.sep
    paths = [w["path"] for w in out["output_files"]]
    assert len(paths) == 2
    for p in paths:
        assert Path(p).exists() and Path(p).read_text(encoding="utf-8")


def test_estate_refusal_fails_before_any_entry_renders(monkeypatch, host, tmp_path):
    """P1 + §6.7: a refused estate argument fails before the AP note read,
    before any entry renders, and before any write — in the established
    {success: false, error} dict."""
    host("windows")
    seen = _install_estate_env(monkeypatch, _estate_note())
    monkeypatch.chdir(tmp_path)

    async def boom(*_a, **_k):
        raise AssertionError("no entry may render after a refusal")

    monkeypatch.setattr(server, "_glimpse_estate_render_entry", boom)
    calls = _install_open_spy(monkeypatch, tpr)

    out = _run(server._glimpse_estate_impl(
        handle="[ap:ESTATE]", output_file="/tmp/estate/"
    ))
    assert out["success"] is False
    assert "/tmp/estate/" in out["error"] and "windows" in out["error"]
    assert "node_id" not in seen      # the AP note read never happened
    assert calls == []                # zero writes
    assert list(tmp_path.rglob("*")) == []


# ═════════════════════════════════════════════════════════════════════════
# H · workflowy_glimpse / _write_glimpse_as_terrain (§13 row 12)
# ═════════════════════════════════════════════════════════════════════════


def test_glimpse_dom_refusal_precedes_the_websocket(monkeypatch, host):
    """The DOM GLIMPSE resolves output_file BEFORE any side effect — the
    refusal fires before the WebSocket is even probed (§13 row 12, P1), and
    propagates unwrapped (never laundered into a 'WebSocket error')."""
    host("windows")
    client = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    with pytest.raises(ToolPathError) as ei:
        asyncio.run(client.workflowy_glimpse(node_id=None, output_file="/tmp/x.md"))
    assert "/tmp/x.md" in str(ei.value) and "windows" in str(ei.value)


def test_terrain_writer_uses_resolved_and_echoes_requested(monkeypatch, host):
    host("windows")
    client = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)

    async def no_scry(**_kw):
        raise RuntimeError("no API fetch in this test")

    client.workflowy_scry = no_scry
    client._generate_markdown = lambda *a, **k: "# terrain markdown\n"
    monkeypatch.setattr(
        nx, "merge_glimpses_for_terrain",
        lambda **_kw: ([], "complete", False),
    )
    monkeypatch.setattr(
        nx, "build_original_ids_seen_from_glimpses", lambda **_kw: []
    )
    monkeypatch.setattr(
        nx, "build_terrain_export_from_glimpse", lambda **_kw: {"nodes": []}
    )
    calls = _install_open_spy(monkeypatch, nx)

    requested = "/mnt/e/out/terrain.json"
    res = tpr.resolve_tool_path(requested, param="output_file", context="glimpse.run")
    ws_glimpse = {"root": {"id": "R1", "name": "Root"}, "node_count": 1, "depth": 1}
    ack = asyncio.run(
        client._write_glimpse_as_terrain("R1", ws_glimpse, requested, res)
    )
    assert ack["success"] is True and ack["mode"] == "file"
    assert ack["terrain_file"] == requested                    # REQUESTED echo
    assert ack["markdown_file"] == "E:\\out\\terrain.md"       # derived: RESOLVED
    assert ack["path_record"] == res.as_dict()
    # The JSON + companion Markdown both open on the RESOLVED string.
    assert [c[0] for c in calls] == ["E:\\out\\terrain.json", "E:\\out\\terrain.md"]


# ═════════════════════════════════════════════════════════════════════════
# I · node.edit note_file (§13 row 2) — reads through the resolver; the
#     record rides the ack; refusals surface as NetworkError envelopes.
# ═════════════════════════════════════════════════════════════════════════

EDIT_ROOT = "11111111-1111-4111-8111-111111111111"
EDIT_NAME = "[ap:FILE01] Existing #keep"


def _edit_setup(monkeypatch, tmp_path):
    paths = make_coherence_root(tmp_path / "coherence")
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    client = make_boundary_capable(
        object.__new__(server.WorkFlowyClient),
        nodes=[{"id": EDIT_ROOT, "name": EDIT_NAME, "note": "old note",
                "parent_id": None, "priority": 10}],
    )
    from n05_region_harness import isolate_claim_scope
    isolate_claim_scope(client, tmp_path)
    current = WorkFlowyNode(id=EDIT_ROOT, name=EDIT_NAME, note="old note")
    client.get_node = AsyncMock(return_value=current)

    async def update(node_id, request, *, basis=None):
        return WorkFlowyNode(
            id=node_id,
            name=request.name if request.name is not None else EDIT_NAME,
            note=request.note if request.note is not None else current.note,
        )

    client.update_node = AsyncMock(side_effect=update)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    return client


def _edit_dispatch(params):
    return asyncio.run(
        dispatch_operation(
            server.build_node_registry(), "edit", {"node_handle": EDIT_ROOT, **params},
            operation_boundary=server._mcp_operation_boundary,
            mutation_admission=server._mcp_mutation_admission,
            exception_mapper=server._mcp_exception_mapper,
            result_finalizer=server._mcp_operation_result_finalizer,
        )
    )


def test_node_edit_note_file_reads_through_resolver_record_on_ack(monkeypatch, host, tmp_path):
    """§9.2 (F5 wording): host patched to linux, a real file under tmp_path,
    record translated: false — and the file's text is what lands in the note."""
    host("linux")
    client = _edit_setup(monkeypatch, tmp_path)
    f = tmp_path / "replacement.md"
    f.write_text("new note", encoding="utf-8")
    # File provenance survives in the uniform header-only report too.
    result = json.loads(_edit_dispatch({"note_file": str(f), "return_diff": False}))
    assert result["success"] is True
    assert result["path_record"] == {
        "requested": str(f), "resolved": str(f),
        "translated": False, "host": "linux",
    }
    client.update_node.assert_awaited_once()
    assert client.update_node.await_args.args[1].note == "new note"


def test_node_edit_note_file_translating_host_open_spy(monkeypatch, host, tmp_path):
    """§9.2: a translating host is exercised with an open spy — the reader
    opens the RESOLVED location and the ack record shows the translation."""
    host("windows")
    client = _edit_setup(monkeypatch, tmp_path)
    calls = _install_open_spy(
        monkeypatch, tpr, texts={"E:\\notes\\replacement.md": "translated body"}
    )
    monkeypatch.setattr(os.path, "isfile", lambda p: True)
    result = json.loads(_edit_dispatch({"note_file": "/mnt/e/notes/replacement.md", "return_diff": False}))
    assert result["success"] is True
    assert result["path_record"] == {
        "requested": "/mnt/e/notes/replacement.md",
        "resolved": "E:\\notes\\replacement.md",
        "translated": True, "host": "windows",
    }
    assert [c[0] for c in calls] == ["E:\\notes\\replacement.md"]
    assert client.update_node.await_args.args[1].note == "translated body"


def test_node_edit_note_file_refusal_is_the_networkerror_envelope(monkeypatch, host, tmp_path):
    """§6.7 row 1: ToolPathError → NetworkError (verbatim teaching message) →
    the kernel error envelope; no edit occurs."""
    host("windows")
    client = _edit_setup(monkeypatch, tmp_path)
    result = _edit_dispatch({"note_file": "/tmp/x.md"})
    assert result["success"] is False
    message = str(result["error"]["message"])
    assert "note_file" in message and "/tmp/x.md" in message and "windows" in message
    client.update_node.assert_not_awaited()


# ═════════════════════════════════════════════════════════════════════════
# J · anchor.point.capture note_file (§13 row 3)
# ═════════════════════════════════════════════════════════════════════════


class _CaptureClient:
    def __init__(self):
        self.kwargs = None

    def _coherence_paths(self):
        return None

    async def add_to_anchor(self, **kwargs):
        self.kwargs = kwargs
        return {"success": True, "root_node_ids": ["U1"], "nodes_created": 1}


def test_capture_ack_record_top_level_and_source_echoes_requested(monkeypatch, host, tmp_path):
    host("linux")
    f = tmp_path / "My note.md"
    f.write_text("body text", encoding="utf-8")
    client = _CaptureClient()
    from n05_region_harness import isolate_claim_scope
    key = "22222222-2222-4222-8222-222222222222"
    make_boundary_capable(client, nodes=[{"id": key, "parent_id": None}])
    isolate_claim_scope(client, tmp_path)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_resolve_generic_node_handle", AsyncMock(return_value=key))
    async def capture():
        async with cache_operation_boundary(client, "capture-path-fixture"):
            return await server._anchor_point_capture_impl("P", note_file=str(f))
    result = _run(capture())
    assert result["success"] is True
    # §6.6: the ack gains the ONE top-level path_record...
    assert result["path_record"] == {
        "requested": str(f), "resolved": str(f),
        "translated": False, "host": "linux",
    }
    # ...while _source.note_file keeps exactly its established provenance
    # fields, `path` echoing the REQUESTED string (P3).
    src = result["_source"]["note_file"]
    assert src["path"] == str(f)
    assert "path_record" not in src
    assert src["node_name"] == "My note"
    assert client.kwargs["nodes"] == [{"name": "My note", "note": "body text"}]


def test_capture_note_file_refusal_is_a_loud_network_error(monkeypatch, host):
    host("windows")
    client = _CaptureClient()
    monkeypatch.setattr(server, "get_client", lambda: client)
    with pytest.raises(NetworkError) as ei:
        _run(server._anchor_point_capture_impl("P", note_file="/tmp/x.md"))
    assert "/tmp/x.md" in str(ei.value) and "windows" in str(ei.value)
    assert client.kwargs is None                      # nothing was created


# ═════════════════════════════════════════════════════════════════════════
# K · node.etch nodes_file + the string fallback (§13 rows 4/5/6)
# ═════════════════════════════════════════════════════════════════════════

ETCH_PARENT = "11111111-2222-4333-8444-555555555555"


def _install_etch_stub(monkeypatch, seen: list, tmp_path):
    """Stub client for the etch impls; returns the client so tests can wrap
    their direct impl calls in a cache_operation_boundary (the impls resolve
    the parent handle through the cache, which requires an active snapshot)."""
    client = make_boundary_capable(
        object.__new__(server.WorkFlowyClient),
        nodes=[{"id": ETCH_PARENT, "name": "Parent", "note": "", "parent_id": None}],
    )
    from n05_region_harness import isolate_claim_scope
    isolate_claim_scope(client, tmp_path)

    async def fake_etch(parent_id, nodes, nodes_file=None, *, fresh_root_hold=None):
        seen.append({"parent_id": parent_id, "nodes": nodes, "nodes_file": nodes_file})
        return {
            "success": True, "nodes_created": 1, "root_node_ids": ["r1"],
            "api_calls": 1, "retries": 0, "rate_limit_hits": 0,
        }

    client.workflowy_etch = fake_etch
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    return client


def test_etch_sync_nodes_file_routes_through_the_resolver(monkeypatch, host, tmp_path):
    host("linux")
    seen: list = []
    client = _install_etch_stub(monkeypatch, seen, tmp_path)
    reader = Mock(wraps=tpr.read_tool_text_file)
    monkeypatch.setattr(server, "read_tool_text_file", reader)
    f = tmp_path / "nodes.json"
    f.write_text('[{"name": "A"}]', encoding="utf-8")

    async def scenario():
        async with cache_operation_boundary(client, "test-etch"):
            return await server._node_etch_sync_impl(ETCH_PARENT, None, str(f))

    result = _run(scenario())
    assert result["success"] is True
    # The ONE read shape was used, with the established param/context.
    reader.assert_called_once_with(str(f), param="nodes_file", context="node.etch")
    # The stats dict gained the record; _source echoes the REQUESTED string.
    assert result["path_record"] == {
        "requested": str(f), "resolved": str(f),
        "translated": False, "host": "linux",
    }
    assert result["_source"]["nodes_file"] == str(f)
    assert seen[0]["nodes"] == '[{"name": "A"}]'
    assert seen[0]["nodes_file"] is None     # the sync route passes payload only


def test_etch_sync_string_fallback_routes_through_the_resolver(monkeypatch, host, tmp_path):
    host("linux")
    seen: list = []
    client = _install_etch_stub(monkeypatch, seen, tmp_path)
    f = tmp_path / "payload.json"
    f.write_text('[{"name": "FB"}]', encoding="utf-8")

    async def scenario():
        async with cache_operation_boundary(client, "test-etch"):
            return await server._node_etch_sync_impl(ETCH_PARENT, str(f), None)

    result = _run(scenario())
    assert result["success"] is True
    assert seen[0]["nodes"] == '[{"name": "FB"}]'
    assert result["_source"]["nodes_file"] == str(f)          # REQUESTED echo
    assert result["path_record"]["requested"] == str(f)


@pytest.mark.parametrize("host_name", HOSTS)
def test_etch_inline_json_string_never_leaks_a_refusal(monkeypatch, host, host_name, tmp_path):
    """§9.2(b): an inline JSON `nodes` string still flows to the client under
    EVERY simulated host — the fallback's ToolPathError means 'not a path'
    and falls through to JSON parsing silently (§13 row 5)."""
    host(host_name)
    seen: list = []
    client = _install_etch_stub(monkeypatch, seen, tmp_path)

    async def scenario():
        async with cache_operation_boundary(client, "test-etch"):
            return await server._node_etch_sync_impl(ETCH_PARENT, '[{"name": "A"}]', None)

    result = _run(scenario())
    assert result["success"] is True
    assert "path_record" not in result               # no path was consumed
    assert "_source" not in result
    assert seen[0]["nodes"] == '[{"name": "A"}]'


def test_etch_windows_tmp_json_string_never_probes_the_raw_path(monkeypatch, host, tmp_path):
    """§9.2(b): nodes='/tmp/x.json' on a simulated Windows host NEVER calls
    os.path.exists on the raw string — the resolver refuses first and the
    fallback is skipped silently."""
    host("windows")
    seen: list = []
    client = _install_etch_stub(monkeypatch, seen, tmp_path)
    exists_calls: list = []
    real_exists = os.path.exists
    monkeypatch.setattr(
        os.path, "exists", lambda p: exists_calls.append(str(p)) or real_exists(p)
    )
    monkeypatch.chdir(tmp_path)

    async def scenario():
        async with cache_operation_boundary(client, "test-etch"):
            return await server._node_etch_sync_impl(ETCH_PARENT, "/tmp/x.json", None)

    result = _run(scenario())
    assert "/tmp/x.json" not in exists_calls
    assert exists_calls == []                        # nothing probed at all
    # The string proceeded to the client's JSON parsing exactly as today.
    assert seen[0]["nodes"] == "/tmp/x.json"
    assert "path_record" not in result
    assert list(tmp_path.rglob("*")) == []


def test_etch_sync_refusal_surfaces_in_the_established_errors_dict(monkeypatch, host, tmp_path):
    """§6.7: the sync etch refusal lands in {success: false, errors: [...]}
    with the teaching message, and the client is never reached."""
    host("windows")
    seen: list = []
    client = _install_etch_stub(monkeypatch, seen, tmp_path)

    async def scenario():
        async with cache_operation_boundary(client, "test-etch"):
            return await server._node_etch_sync_impl(ETCH_PARENT, None, "/tmp/nodes.json")

    result = _run(scenario())
    assert result["success"] is False and result["nodes_created"] == 0
    assert "Failed to read nodes_file '/tmp/nodes.json'" in result["errors"][0]
    assert "windows" in result["errors"][0] and "/mnt/<drive>" in result["errors"][0]
    assert seen == []


def test_etch_async_translating_host_forwards_resolved_and_envelope_carries_record(
    monkeypatch, host, tmp_path
):
    """§9.2(a): node.etch ASYNC with nodes_file under a translating host —
    the job payload's nodes_file carries the RESOLVED string (the ONE P3
    exception), the client-level open receives exactly that string (identity
    pass-through re-resolution), and the job envelope carries path_record at
    its TOP LEVEL, beside `job`."""
    host("windows")
    paths = make_coherence_root(tmp_path / "coherence")
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)

    # A REAL client-etch double: the async route re-opens nodes_file inside
    # client.workflowy_etch, so the forwarded string must be the resolvable one.
    client = make_boundary_capable(
        object.__new__(WorkFlowyClientNexus),
        nodes=[{"id": ETCH_PARENT, "name": "Parent", "note": "", "parent_id": None}],
    )
    from n05_region_harness import isolate_claim_scope
    isolate_claim_scope(client, tmp_path)
    client._coherence_paths = lambda: paths
    created: list = []

    async def fake_create_node(request, **_kw):
        created.append(request)
        return types.SimpleNamespace(id=f"created-{len(created)}")

    client.create_node = fake_create_node
    client._log_to_file = lambda *a, **k: None
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)

    payload_text = '[{"name": "ASYNC NODE"}]'
    calls = _install_open_spy(monkeypatch, tpr, texts={"E:\\x\\nodes.json": payload_text})
    monkeypatch.setattr(os.path, "isfile", lambda p: True)

    async def scenario():
        async with cache_operation_boundary(client, "test-etch-async"):
            envelope = await server._node_etch_impl(
                ETCH_PARENT, None, "/mnt/e/x/nodes.json", execution="async"
            )
        job_id = envelope["job"]["id"]
        await server._jobs[job_id]["_task"]
        return envelope, server._jobs[job_id]

    envelope, job = _run(scenario())
    assert envelope["success"] is True and "job" in envelope
    # The record rides the job envelope's TOP LEVEL, beside `job`.
    assert envelope["path_record"] == {
        "requested": "/mnt/e/x/nodes.json", "resolved": "E:\\x\\nodes.json",
        "translated": True, "host": "windows",
    }
    # The job INPUT record carries the RESOLVED string (§13 row 4).
    assert job["payload"]["nodes_file"] == "E:\\x\\nodes.json"
    assert job["status"] == "completed", job.get("error")
    assert job["result"]["success"] is True
    # BOTH opens — the server-level read and the client-level re-read — hit
    # the RESOLVED path; the client's re-resolution was the identity
    # pass-through (no second translation, no raw form anywhere).
    assert [c[0] for c in calls] == ["E:\\x\\nodes.json", "E:\\x\\nodes.json"]
    assert created and created[0].name == "ASYNC NODE"


def test_etch_async_refusal_is_the_established_error_dict_and_enqueues_nothing(
    monkeypatch, host, tmp_path
):
    host("windows")
    seen: list = []
    client = _install_etch_stub(monkeypatch, seen, tmp_path)
    jobs_before = len(server._jobs)

    async def scenario():
        async with cache_operation_boundary(client, "test-etch"):
            return await server._node_etch_async_impl(ETCH_PARENT, None, "/tmp/nodes.json")

    result = _run(scenario())
    assert result["success"] is False
    assert "Failed to read nodes_file '/tmp/nodes.json'" in result["error"]
    assert "windows" in result["error"]
    assert len(server._jobs) == jobs_before          # no job was created


# ═════════════════════════════════════════════════════════════════════════
# L · The client-level workflowy_etch nodes_file branch (§13 row 6)
# ═════════════════════════════════════════════════════════════════════════


def test_client_etch_nodes_file_branch_routes_through_the_read_shape(monkeypatch, host, tmp_path):
    host("linux")
    client = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    client._log_to_file = lambda *a, **k: None
    created: list = []

    async def fake_create_node(request, **_kw):
        created.append(request)
        return types.SimpleNamespace(id=f"created-{len(created)}")

    client.create_node = fake_create_node
    reader = Mock(wraps=tpr.read_tool_text_file)
    monkeypatch.setattr(tpr, "read_tool_text_file", reader)
    f = tmp_path / "client_nodes.json"
    f.write_text('[{"name": "CLIENT"}]', encoding="utf-8")
    result = _run(client.workflowy_etch(ETCH_PARENT, [], nodes_file=str(f)))
    assert result["success"] is True
    reader.assert_called_once_with(str(f), param="nodes_file", context="workflowy_etch")
    assert created and created[0].name == "CLIENT"


def test_client_etch_nodes_file_refusal_is_the_established_errors_dict(monkeypatch, host):
    host("windows")
    client = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    client._log_to_file = lambda *a, **k: None
    result = _run(client.workflowy_etch(ETCH_PARENT, [], nodes_file="/tmp/nodes.json"))
    assert result["success"] is False
    assert "Failed to read nodes_file '/tmp/nodes.json'" in result["errors"][0]
    assert "windows" in result["errors"][0]
