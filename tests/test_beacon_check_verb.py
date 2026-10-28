"""U02 §9.2 -- the ``beacon.check`` verb, driven through the real ``beacon``
tool entry (validation, admission, the operation boundary over a pinned
cache, the archive invoker and the read finalizer).

The client double carries the S03-B boundary surface over a synthetic
``Root: -> FOLDER -> FILE`` cache whose ``Path:`` values resolve to files
under ``tmp_path``. Every Workflowy HTTP path, lease, job, cache refresh,
structured node read and the tab-backed suppress list is patched to raise
and record, so a test that succeeds also proves none of them was entered.
Fixture texts come from ``beacon_check_fixtures`` (quoted lines), so this
module holds no beacon comment block.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import sys
import threading

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import beacon_check_fixtures as fx  # noqa: E402
from coherence_harness import make_boundary_capable  # noqa: E402
from workflowy_mcp import beacon_integrity as bi  # noqa: E402
from workflowy_mcp import server  # noqa: E402
from workflowy_mcp import tool_path_resolution as tpr  # noqa: E402
from workflowy_mcp.client import api_client_nexus as acn  # noqa: E402
from workflowy_mcp.coherence.admission import (  # noqa: E402
    ADMISSION_CLASS_PROCEED,
    mutation_admission_class,
)
from workflowy_mcp.mcp_api_registry import _TOLERATED_KEY_SLIPS  # noqa: E402

ROOT = "c1000000-0000-4000-8000-000000000001"
PKG = "c2000000-0000-4000-8000-000000000002"
DUP = "c3000000-0000-4000-8000-000000000003"
CLEAN = "c4000000-0000-4000-8000-000000000004"
QUIET = "c5000000-0000-4000-8000-000000000005"
UNREAD = "c6000000-0000-4000-8000-000000000006"
GONE = "c7000000-0000-4000-8000-000000000007"
NOTES = "c8000000-0000-4000-8000-000000000008"
SYMBOL = "c9000000-0000-4000-8000-000000000009"
PLAIN = "ca000000-0000-4000-8000-00000000000a"
TWIN_A = "cb000000-0000-4000-8000-0000000000a1"
TWIN_B = "cb000000-0000-4000-8000-0000000000b1"

# Every client method that reaches the Workflowy API (the HTTP client
# property below covers any other request path).
NETWORK_METHODS = (
    "export_nodes",
    "get_node",
    "list_nodes",
    "create_node",
    "update_node",
    "delete_node",
    "complete_node",
    "move_node",
    "refresh_nodes_export_cache",
    "update_beacon_from_node",
    "_send_workflowy_write",
)
# Workflowy call, lease, job, cache refresh, and the tab-backed suppress list.
FORBIDDEN_SERVER_CALLABLES = (
    "_node_read_impl",
    "acquire_q3_leases_async",
    "_acquire_single_beacon_q3_lease",
    "_start_carto_refresh_job",
    "_system_cache_refresh_impl",
    "_beacon_symbol_suppress_tags",
    "_resolve_slice_group_config",
)


def _write(path, content) -> str:
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8", "newline": ""}
    with open(str(path), mode, **kwargs) as f:
        f.write(content)
    return str(path)


def _node(nid, parent, name, note):
    return {"id": nid, "parent_id": parent, "name": name, "note": note}


def _repo(tmp_path):
    """repo/: no_comment.py (info row), pkg/{duplicate.py (error),
    clean.py, unreadable.py (error), notes.txt (skipped)}; pkg/gone.py is
    mapped but absent (file_missing)."""
    repo = tmp_path / "repo"
    _write(repo / "no_comment.py", fx.PROBLEM_FIXTURES["empty_comment"][1])
    _write(repo / "pkg" / "duplicate.py", fx.PROBLEM_FIXTURES["duplicate_id"][1])
    _write(repo / "pkg" / "clean.py", fx.all_text_fixtures()["clean_py"][1])
    _write(repo / "pkg" / "unreadable.py", fx.UNREADABLE_FIXTURE[1])
    _write(repo / "pkg" / "notes.txt", "plain text\n")
    nodes = [
        _node(ROOT, None, "[ap:CHKRT1] 🗺️ Source Map: repo", f"Root: {repo}"),
        _node(QUIET, ROOT, "🐍 no_comment.py", "Path: no_comment.py"),
        _node(PKG, ROOT, "📂 pkg", "Path: pkg"),
        _node(DUP, PKG, "🐍 duplicate.py", "Path: duplicate.py\nLINE COUNT: 18"),
        _node(SYMBOL, DUP, "ƒ first()", "AST_QUALNAME: first"),
        _node(CLEAN, PKG, "🐍 clean.py", "Path: clean.py"),
        _node(UNREAD, PKG, "🐍 unreadable.py", "Path: unreadable.py"),
        _node(GONE, PKG, "🐍 gone.py", "Path: gone.py"),
        _node(NOTES, PKG, "📄 notes.txt", "Path: notes.txt"),
        _node(PLAIN, ROOT, "a plain note", "just text"),
        _node(TWIN_A, None, "twin a", ""),
        _node(TWIN_B, None, "twin b", ""),
    ]
    return repo, nodes


class _NoNetworkClient(server.WorkFlowyClient):
    """The real client class with its HTTP client property disabled."""

    calls: list = []

    @property
    def client(self):  # every Workflowy HTTP request goes through this
        type(self).calls.append("client")
        raise AssertionError("beacon.check reached the Workflowy HTTP client")


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo, nodes = _repo(tmp_path)
    calls: list[str] = []
    client = object.__new__(_NoNetworkClient)
    monkeypatch.setattr(_NoNetworkClient, "calls", calls)
    make_boundary_capable(client, nodes=nodes)

    def _forbid(name):
        async def _raise(*_a, **_k):
            calls.append(name)
            raise AssertionError(f"beacon.check reached {name}")

        return _raise

    for name in NETWORK_METHODS:
        assert hasattr(client, name), name
        setattr(client, name, _forbid(f"client.{name}"))
    for name in FORBIDDEN_SERVER_CALLABLES:
        assert hasattr(server, name), name
        monkeypatch.setattr(server, name, _forbid(f"server.{name}"))
    monkeypatch.setattr(acn, "export_nodes_impl", _forbid("acn.export_nodes_impl"))
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    return {"repo": repo, "nodes": nodes, "calls": calls}


def _check(params):
    return asyncio.run(server.beacon(verb="check", params=params))


def _engine_report(nodes, node_id, **kw):
    """The engine's own report for a node, computed directly (the oracle)."""
    entry = bi.collect_mapped_beacon_files({n["id"]: n for n in nodes}, node_id)
    return entry["root_abs"], bi.run_beacon_check(
        entry["files"], repo_root=entry["root_abs"], missing=entry["missing"],
        skipped=entry["skipped"], **kw,
    )


def _report_part(result):
    return {k: result[k] for k in ("summary_line", "counts", "rows", "rows_truncated", "min_severity",
                                  "eligible_total", "has_more", "next_offset", "scope_fingerprint", "missing_finding_ids")}


def _triples(result):
    return [(r["file"], r["line"], r["code"]) for r in result["rows"]]


# ---------------------------------------------------------------------------
# §9.2 item 1: file_path on one file with one known problem.
# ---------------------------------------------------------------------------


def test_file_path_returns_the_engines_exact_report(env):
    path = str(env["repo"] / "pkg" / "duplicate.py")
    result = _check({"file_path": path})
    assert result["success"] is True, result
    assert result["operation"] == "beacon.check"
    expected = bi.run_beacon_check([{"abs_path": path, "display_path": path, "node_id": None}])
    assert _report_part(result) == expected
    assert result["summary_line"].startswith(f"1 file · 1 error · 0 warnings · 0 info ({path}:11 duplicate_id — ")
    assert _triples(result) == [(path, 11, "duplicate_id")]
    assert result["target"] == {
        "kind": "file",
        "resolved_uuid": None,
        "header": None,
        "root_abs": None,
        "path_record": tpr.resolve_tool_path(
            path, param="file_path", context="beacon.check", direction="read"
        ).as_dict(),
    }
    assert list(result) == [
        "success", "operation", "summary_line", "target", "counts", "rows", "rows_truncated", "min_severity",
        "eligible_total", "has_more", "next_offset", "scope_fingerprint", "missing_finding_ids",
    ]
    assert env["calls"] == []


# ---------------------------------------------------------------------------
# §9.2 item 2: node_handle on a Root:, a FOLDER and a FILE node.
# ---------------------------------------------------------------------------


def test_node_handle_root_folder_and_file(env):
    repo, nodes = env["repo"], env["nodes"]

    root = _check({"node_handle": ROOT})
    assert root["success"] is True, root
    root_abs, expected = _engine_report(nodes, ROOT)
    assert _report_part(root) == expected
    assert root["target"] == {
        "kind": "folder", "resolved_uuid": ROOT, "header": ["Root", str(repo)],
        "root_abs": str(repo), "path_record": None,
    }
    assert root_abs == str(repo)
    assert _triples(root) == [
        ("pkg/duplicate.py", 11, "duplicate_id"),
        ("pkg/unreadable.py", 0, "file_unreadable"),
        ("pkg/gone.py", 0, "file_missing"),
    ]
    counts = root["counts"]
    assert (counts["files_checked"], counts["files_missing"], counts["files_skipped"], counts["files_unreadable"]) == (4, 1, 1, 1)
    assert counts["by_severity"] == {"error": 2, "warning": 1, "info": 1}

    # The same Root: through its AP-ID and its 8-hex segment.
    assert _report_part(_check({"node_handle": "[ap:CHKRT1]"})) == expected
    assert _report_part(_check({"node_handle": ROOT[:8]})) == expected

    folder = _check({"node_handle": PKG})
    assert folder["success"] is True, folder
    assert _report_part(folder) == _engine_report(nodes, PKG)[1]
    assert folder["target"]["kind"] == "folder" and folder["target"]["header"] == ["Path", "pkg"]
    assert folder["target"]["root_abs"] == str(repo)
    assert {r["file"] for r in folder["rows"]} == {"pkg/duplicate.py", "pkg/gone.py", "pkg/unreadable.py"}
    assert (folder["counts"]["files_checked"], folder["counts"]["files_missing"], folder["counts"]["files_skipped"]) == (3, 1, 1)

    single = _check({"node_handle": DUP})
    assert single["success"] is True, single
    assert _report_part(single) == _engine_report(nodes, DUP)[1]
    assert single["target"] == {
        "kind": "file", "resolved_uuid": DUP, "header": ["Path", "duplicate.py"],
        "root_abs": str(repo), "path_record": None,
    }
    assert _triples(single) == [("pkg/duplicate.py", 11, "duplicate_id")]
    assert (single["counts"]["files_checked"], single["counts"]["files_missing"], single["counts"]["files_skipped"]) == (1, 0, 0)
    assert env["calls"] == []


# ---------------------------------------------------------------------------
# §9.2 item 3: every verb-level error, each with the family's own code.
# ---------------------------------------------------------------------------


def _error(result, code):
    assert result["success"] is False, result
    assert result["operation"] == "beacon.check"
    assert result["error"]["code"] == code, result["error"]
    return result["error"]


@pytest.mark.parametrize(
    "params",
    [
        {"node_handle": ROOT, "file_path": "/mnt/e/repo/app.py"},
        {},
        {"node_handle": "   ", "file_path": ""},
    ],
    ids=["both", "neither", "blank"],
)
def test_target_choice_is_exactly_one(env, params):
    error = _error(_check(params), "invalid_parameter_combination")
    assert "node_handle" in error["message"] and "file_path" in error["message"]


def test_node_errors(env):
    unresolved = _error(_check({"node_handle": "dddd0000"}), "node_handle_unresolved")
    assert unresolved["extra"]["candidates"] == []

    ambiguous = _error(_check({"node_handle": TWIN_A[:8]}), "node_handle_unresolved")
    assert {c["uuid"] for c in ambiguous["extra"]["candidates"]} == {TWIN_A, TWIN_B}

    for handle, header in ((SYMBOL, None), (PLAIN, None)):
        invalid = _error(_check({"node_handle": handle}), "map_target_invalid")
        assert set(invalid["extra"]) == {"node_handle", "resolved_uuid", "header", "name"}
        assert invalid["extra"]["resolved_uuid"] == handle and invalid["extra"]["header"] == header
        assert "FILE node" in invalid["message"] and "FOLDER node" in invalid["message"] and "Root:" in invalid["message"]

    unsupported = _error(_check({"node_handle": NOTES}), "beacon_target_unsupported")
    assert "'.txt'" in unsupported["message"] and unsupported["extra"]["header"] == ["Path", "notes.txt"]


def test_path_errors(env, monkeypatch):
    repo = env["repo"]
    relative = _error(_check({"file_path": "pkg/duplicate.py"}), "invalid_parameter_value")
    assert "must be an ABSOLUTE path" in relative["message"]

    directory = _error(_check({"file_path": str(repo / "pkg")}), "invalid_parameter_value")
    assert "is a directory" in directory["message"]
    assert "FOLDER node" in directory["message"] and "Root: node" in directory["message"]
    assert directory["extra"]["path_record"]["resolved"] == str(repo / "pkg")

    missing = _error(_check({"file_path": str(repo / "pkg" / "nope.py")}), "invalid_parameter_value")
    assert "is not an existing file" in missing["message"]

    unsupported = _error(_check({"file_path": str(repo / "pkg" / "notes.txt")}), "beacon_target_unsupported")
    assert "'.txt'" in unsupported["message"]

    # A WSL-only location on a Windows daemon: the resolver's own teaching
    # refusal, complete, as invalid_parameter_value.
    monkeypatch.setattr(tpr, "_detect_host_raw", lambda: "windows")
    wsl_only = _error(_check({"file_path": "/tmp/x.py"}), "invalid_parameter_value")
    with pytest.raises(tpr.ToolPathError) as refusal:
        tpr.resolve_tool_path("/tmp/x.py", param="file_path", context="beacon.check", direction="read")
    assert wsl_only["message"] == str(refusal.value)
    assert env["calls"] == []


def test_parameter_errors(env):
    path = str(env["repo"] / "pkg" / "duplicate.py")
    rows = _error(_check({"file_path": path, "max_rows": 0}), "invalid_parameter_value")
    assert "max_rows" in rows["message"]
    _error(_check({"file_path": path, "min_severity": "fatal"}), "invalid_enum_value")
    for value in (-1,):
        error = _error(_check({"file_path": path, "offset": value}), "invalid_parameter_value")
        assert "offset" in error["message"]
    _error(_check({"file_path": path, "codes": ["misspelt_code"]}), "invalid_enum_value")
    for key, value in (("offset", True), ("offset", "1"), ("codes", "unknown_kind"),
                       ("codes", [1]), ("finding_ids", "bf1:x"), ("finding_ids", [1])):
        _error(_check({"file_path": path, key: value}), "invalid_parameter_type")
    _error(_check({"file_path": path, "folder": "pkg"}), "forbidden_parameter")


# ---------------------------------------------------------------------------
# §9.2 item 4: aliases (and the registered aliases/codes, §11 item 2).
# ---------------------------------------------------------------------------


def test_aliases(env):
    path = str(env["repo"] / "pkg" / "duplicate.py")
    assert _report_part(_check({"path": path})) == _report_part(_check({"file_path": path}))
    by_handle = _report_part(_check({"node_handle": PKG}))
    assert _report_part(_check({"node_id": PKG})) == by_handle
    assert _report_part(_check({"root_uuid": PKG})) == by_handle

    spec = server.BEACON_REGISTRY.resolve("check")
    assert dict(spec.param_aliases) == {"node_id": "node_handle", "root_uuid": "node_handle"}
    # `path` reaches file_path through the ONE shared tolerated-slip table,
    # which forbids repeating a slip as a declared alias.
    assert _TOLERATED_KEY_SLIPS["path"] == ("file_path",)
    assert spec.domain_error_codes == (
        "node_handle_unresolved", "map_target_invalid", "beacon_target_unsupported", "invalid_parameter_value",
    )
    assert spec.related == ("beacon.map.refresh", "beacon.tags.ensure", "beacon.snippet")


# ---------------------------------------------------------------------------
# §9.2 items 5 and 6: no tab; no Workflowy call, lease, job or refresh.
# ---------------------------------------------------------------------------


def test_no_tab_needed(env, monkeypatch):
    async def no_tab(*_a, **_k):
        env["calls"].append("tab")
        raise RuntimeError("No live Workflowy tab is connected to the GLIMPSE broker")

    monkeypatch.setattr(server, "_beacon_symbol_suppress_tags", no_tab)
    monkeypatch.setattr(server, "_resolve_slice_group_config", no_tab)
    assert _check({"node_handle": ROOT})["success"] is True
    assert _check({"file_path": str(env["repo"] / "pkg" / "duplicate.py")})["success"] is True
    assert env["calls"] == []


def test_no_workflowy_call_lease_job_or_refresh(env):
    # The env fixture patched every one of them to raise and record.
    assert _check({"node_handle": ROOT})["success"] is True
    assert _check({"node_handle": DUP})["success"] is True
    assert _check({"file_path": str(env["repo"] / "pkg" / "duplicate.py")})["success"] is True
    assert env["calls"] == []


def test_front_source_names_no_forbidden_callable():
    """§11 item 4 (and §6.1 item 8), mechanically."""
    source = inspect.getsource(server._beacon_check_impl) + inspect.getsource(server._beacon_check_run)
    for name in (
        "nexus_map_codebase", "_beacon_symbol_suppress_tags", "acquire_q3_leases_async",
        "_acquire_single_beacon_q3_lease", "_node_read_impl", "_start_carto_refresh_job",
        "_system_cache_refresh_impl", "refresh_nodes_export_cache", "get_client",
        "_import_cartographer_module", "summary_line(",
    ):
        assert name not in source, name


# ---------------------------------------------------------------------------
# §9.2 item 7: the engine runs off the event loop.
# ---------------------------------------------------------------------------


def test_engine_runs_off_the_event_loop(env, monkeypatch):
    seen = []
    real_run, real_collect = bi.run_beacon_check, bi.collect_mapped_beacon_files

    def run_spy(*args, **kwargs):
        seen.append(("run", threading.current_thread() is threading.main_thread()))
        return real_run(*args, **kwargs)

    def collect_spy(*args, **kwargs):
        seen.append(("collect", threading.current_thread() is threading.main_thread()))
        return real_collect(*args, **kwargs)

    monkeypatch.setattr(bi, "run_beacon_check", run_spy)
    monkeypatch.setattr(bi, "collect_mapped_beacon_files", collect_spy)
    assert _check({"node_handle": ROOT})["success"] is True
    assert _check({"file_path": str(env["repo"] / "pkg" / "duplicate.py")})["success"] is True
    assert seen == [("collect", False), ("run", False), ("run", False)]


# ---------------------------------------------------------------------------
# §9.2 item 8: every swept file's bytes and mtime are unchanged.
# ---------------------------------------------------------------------------


def _fingerprints(root) -> dict:
    out = {}
    for dirpath, _dirs, files in os.walk(str(root)):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            out[path] = (digest, os.stat(path).st_mtime_ns)
    return out


def test_checked_files_are_byte_and_mtime_unchanged(env):
    before = _fingerprints(env["repo"])
    assert len(before) == 5
    assert _check({"node_handle": ROOT, "min_severity": "info"})["success"] is True
    assert _check({"file_path": str(env["repo"] / "pkg" / "duplicate.py")})["success"] is True
    assert _fingerprints(env["repo"]) == before


# ---------------------------------------------------------------------------
# §9.2 item 9: min_severity and max_rows pass through to the engine.
# ---------------------------------------------------------------------------


def test_min_severity_and_max_rows_pass_through(env):
    nodes = env["nodes"]
    info = _check({"node_handle": ROOT, "min_severity": "info"})
    assert _report_part(info) == _engine_report(nodes, ROOT, min_severity="info")[1]
    assert ("no_comment.py", 1, "empty_comment") in _triples(info) and info["rows_truncated"] == 0

    cut = _check({"node_handle": ROOT, "min_severity": "info", "max_rows": 2})
    assert _report_part(cut) == _engine_report(nodes, ROOT, min_severity="info", max_rows=2)[1]
    assert [r["severity"] for r in cut["rows"]] == ["error", "error"] and cut["rows_truncated"] == 2
    assert cut["counts"] == info["counts"] and cut["summary_line"] == info["summary_line"]

    errors = _check({"node_handle": ROOT, "min_severity": "error"})
    assert _report_part(errors) == _engine_report(nodes, ROOT, min_severity="error")[1]
    assert {r["severity"] for r in errors["rows"]} == {"error"} and errors["min_severity"] == "error"

    missing_id = "bf1:" + "0" * 64
    requested = [r["finding_id"] for r in info["rows"]] + [missing_id]
    filters = {"min_severity": "info", "max_rows": 1, "offset": 1,
               "codes": ["duplicate_id", "file_unreadable"], "finding_ids": requested}
    page = _check({"node_handle": ROOT, **filters})
    assert _report_part(page) == _engine_report(nodes, ROOT, **filters)[1]
    assert page["eligible_total"] == 2 and page["has_more"] is False and page["next_offset"] is None
    assert page["rows"][0]["code"] == "file_unreadable" and page["missing_finding_ids"] == [missing_id]
    assert page["counts"] == info["counts"] and page["summary_line"] == info["summary_line"]
    path = str(env["repo"] / "pkg" / "duplicate.py")
    path_filters = {"min_severity": "info", "offset": 1, "codes": ["duplicate_id"], "finding_ids": requested}
    direct = _check({"file_path": path, **path_filters})
    assert _report_part(direct) == bi.run_beacon_check([{"abs_path": path, "display_path": path}], **path_filters)
    assert direct["rows"] == [] and direct["eligible_total"] == 1
    assert env["calls"] == []


@pytest.mark.parametrize("kwargs", [{"offset": -1}, {"codes": ["typo"]}, {"finding_ids": [1]}])
def test_direct_check_front_maps_bad_values_to_parameter_error(env, kwargs):
    with pytest.raises(server._BeaconOperationError) as caught:
        asyncio.run(server._beacon_check_impl(None, str(env["repo"] / "pkg" / "duplicate.py"), **kwargs))
    assert caught.value.code == "invalid_parameter_value"


# ---------------------------------------------------------------------------
# §9.2 item 10: an engine exception is a failed envelope, never a success.
# ---------------------------------------------------------------------------


def test_engine_exception_is_operation_failed(env, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("engine defect")

    monkeypatch.setattr(bi, "run_beacon_check", boom)
    for params in ({"node_handle": ROOT}, {"file_path": str(env["repo"] / "pkg" / "duplicate.py")}):
        result = _check(params)
        assert result["success"] is False
        assert result["error"]["code"] == "operation_failed"
        assert "engine defect" in result["error"]["message"]


# ---------------------------------------------------------------------------
# §9.2 item 11: admission classifies the verb as a plain read.
# ---------------------------------------------------------------------------


def test_admission_is_proceed():
    spec = server.BEACON_REGISTRY.resolve("check")
    assert mutation_admission_class("beacon.check", ("pure_read",), True) == ADMISSION_CLASS_PROCEED
    assert mutation_admission_class(spec.canonical_name, spec.side_effects, spec.cache_backed) == ADMISSION_CLASS_PROCEED


# ---------------------------------------------------------------------------
# describe and the in-repo reference (§6.2, §11 item 6).
# ---------------------------------------------------------------------------


def test_describe_teaches_every_code_and_names_the_doc():
    registry = server.BEACON_REGISTRY
    full = registry.describe_full("check")
    description = full["returns"]["description"]
    for code in bi.BEACON_CHECK_CODES:
        assert code in description, code
        assert bi.BEACON_CHECK_CODES[code]["authority"] in description
    for field in ("finding_id", "observed", "members", "params_schema"):
        assert field in description
    for field in ("resolved_uuid", "eligible_total", "has_more", "next_offset", "by_file", "scope_fingerprint", "missing_finding_ids"):
        assert field in description
    props = full["params_schema"]["properties"]
    assert set(props) == {"node_handle", "file_path", "min_severity", "max_rows", "offset", "codes", "finding_ids"}
    assert props["codes"]["items"] == {"type": "string", "enum": list(bi.BEACON_CHECK_CODES)}
    assert props["finding_ids"]["items"] == {"type": "string"}
    assert props["offset"]["minimum"] == 0 and "maximum" not in props["offset"]
    assert props["max_rows"]["minimum"] == 1 and "maximum" not in props["max_rows"]
    assert "node_uuid" not in str(full)
    assert "docs/BEACON-CHECK.md" in full["summary"]
    assert "\n" not in registry.resolve("check").summary and len(registry.resolve("check").summary) <= 300
    assert "ADVISORY FRAMING" in description and "min_severity: 'info'" in description
    assert "sync_node, map.refresh and check target node handles (check also an absolute file path)" in registry.family_summary
    assert any(line.startswith("check — ") for line in registry.index_lines())
    assert "check — " in server._BEACON_TOOL_DESCRIPTION


def test_reference_doc_lists_every_code():
    """§11 item 6: docs/BEACON-CHECK.md carries the whole code table."""
    path = os.path.join(_REPO_ROOT, "docs", "BEACON-CHECK.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    table_rows = [line for line in text.splitlines() if line.startswith("| `")]
    assert [line.split("`")[1] for line in table_rows] == list(bi.BEACON_CHECK_CODES)
    for line in table_rows:
        code = line.split("`")[1]
        assert f"| {bi.BEACON_CHECK_CODES[code]['severity']} | {bi.BEACON_CHECK_CODES[code]['view']} |" in line, code
        assert f"`{bi.BEACON_CHECK_CODES[code]['authority']}`" in line, code
    assert "docs/BEACON-CHECK.md" in server.BEACON_REGISTRY.resolve("check").describe_suffix
    for field in ("resolved_uuid", "eligible_total", "next_offset", "scope_fingerprint", "missing_finding_ids", "counts.by_file"):
        assert field in text
    assert "node_uuid" not in text
