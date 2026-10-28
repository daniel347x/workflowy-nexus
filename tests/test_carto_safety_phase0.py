from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import nexus_map_codebase
from workflowy_mcp import server
from workflowy_mcp.client import api_client_nexus
from workflowy_mcp.client.api_client_core import WorkFlowyClientCore
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
from workflowy_mcp.client.rate_limit import AdaptiveRateLimiter
from workflowy_mcp.models import RateLimitError, parse_retry_after_header

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_retry_after_parser_supports_both_rfc_forms_and_fallbacks():
    assert parse_retry_after_header("0", now_epoch=100.0)["delay_seconds"] == 0
    assert parse_retry_after_header("12", now_epoch=100.0) == {
        "delay_seconds": 12.0,
        "used_fallback": False,
        "diagnostic": "delta_seconds",
    }

    date_header = format_datetime(datetime.fromtimestamp(130.0, tz=timezone.utc), usegmt=True)
    parsed_date = parse_retry_after_header(date_header, now_epoch=100.0)
    assert parsed_date["used_fallback"] is False
    assert parsed_date["diagnostic"] == "http_date"
    assert parsed_date["delay_seconds"] == pytest.approx(30.0)

    for malformed in (None, "", "not-a-date", "-4", "nan", "inf"):
        parsed = parse_retry_after_header(malformed, now_epoch=100.0)
        assert parsed["used_fallback"] is True
        assert parsed["delay_seconds"] == 60.0


def test_rate_limit_error_exposes_zero_and_handle_response_uses_shared_parser():
    zero = RateLimitError(0)
    assert zero.retry_after == 0
    assert zero.details["retry_after"] == 0

    client = object.__new__(WorkFlowyClientCore)
    request = httpx.Request("GET", "https://workflowy.test/nodes")
    response = httpx.Response(429, headers={"Retry-After": "7"}, request=request)
    with pytest.raises(RateLimitError) as exc_info:
        asyncio.run(client._handle_response(response))
    assert exc_info.value.retry_after == 7.0

    malformed = httpx.Response(429, headers={"Retry-After": "later-ish"}, request=request)
    with pytest.raises(RateLimitError) as malformed_info:
        asyncio.run(client._handle_response(malformed))
    assert malformed_info.value.retry_after == 60.0


def test_all_eight_crud_loops_use_the_30_second_local_retry_ceiling():
    path = REPO_ROOT / "src" / "workflowy_mcp" / "client" / "api_client_core.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    target_methods = {
        "create_node",
        "update_node",
        "delete_node",
        "complete_node",
        "uncomplete_node",
        "move_node",
        "get_node",
        "list_nodes",
    }
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in target_methods
    }
    assert set(methods) == target_methods
    for name, method in methods.items():
        calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_bounded_rate_limit_retry_delay"
        ]
        assert len(calls) == 1, name

    client = object.__new__(WorkFlowyClientCore)
    client._write_governor = SimpleNamespace(
        settings=SimpleNamespace(
            max_wait_before_refusal_seconds=30.0,
            retry_after_fallback_seconds=60.0,
        )
    )
    with pytest.raises(RateLimitError) as exc_info:
        client._bounded_rate_limit_retry_delay(RateLimitError(21_600), 2.0, "test CRUD")
    assert exc_info.value.retry_after == 21_600
    assert client._bounded_rate_limit_retry_delay(RateLimitError(0), 4.0, "test CRUD") == 4.0


def test_adaptive_rate_limiter_ignores_retry_after_but_keeps_rate_halving():
    limiter = AdaptiveRateLimiter(initial_rate=10.0, min_rate=1.0, max_rate=100.0)
    limiter.on_rate_limit(21_600)
    assert limiter.requests_per_second == 5.0
    assert limiter.retry_after_until is None


def test_legacy_reconcile_fallback_is_excised():
    source = (REPO_ROOT / "src" / "workflowy_mcp" / "client" / "api_client_nexus.py").read_text(
        encoding="utf-8"
    )
    assert "def _refresh_file_node_beacons_legacy" not in source
    assert "falling back to legacy" not in source
    assert "no legacy fallback exists" in source


def test_markdown_parser_sanitizes_c0_in_memory_without_writing(tmp_path):
    path = tmp_path / "poisoned.md"
    original = b"# Heading\n\nBody before\x0bafter\n"
    path.write_bytes(original)

    tree = nexus_map_codebase.parse_markdown_structure(str(path))

    assert path.read_bytes() == original
    assert tree and tree[0]["name"] == "Heading"
    assert "before\\vafter" in str(tree[0].get("note") or "")


def test_low_level_beacon_writer_is_disk_only(monkeypatch):
    calls: list[tuple[str, str, str]] = []

    async def fake_export(*_args, **_kwargs):
        return {"nodes": [{"id": "AST", "name": "old", "note": "AST_QUALNAME: f"}]}

    def helper(path, name, note):
        calls.append((path, name, note))
        return {
            "success": True,
            "operation": "updated_beacon",
            "base_name": "f",
            "language": "python",
        }

    target = {
        "success": True,
        "file_node": {"id": "FILE"},
        "file_node_id": "FILE",
        "source_path": "x.py",
        "ext": ".py",
        "language": "python",
        "cartographer": SimpleNamespace(update_beacon_from_node_python=helper),
    }
    monkeypatch.setattr(api_client_nexus, "export_nodes_impl", fake_export)
    monkeypatch.setattr(
        api_client_nexus,
        "_resolve_beacon_target_from_cache",
        lambda _cache, _node_id: target,
    )

    async def forbidden(*_args, **_kwargs):  # pragma: no cover - failure sentinel
        raise AssertionError("disk-only writer attempted a cache or Workflowy mutation")

    monkeypatch.setattr(WorkFlowyClientNexus, "update_cached_node_name", forbidden)
    monkeypatch.setattr(WorkFlowyClientNexus, "refresh_file_node_beacons", forbidden)

    client = object.__new__(WorkFlowyClientNexus)
    result = asyncio.run(
        client.update_beacon_from_node(node_id="AST", name="f #topic", note="fresh note")
    )
    assert calls == [("x.py", "f #topic", "fresh note")]
    assert result["success"] is True
    assert "cache_name_updated" not in result
    assert "cache_note_updated" not in result
    assert "auto_refresh" not in result


def test_virtual_group_discriminator_preserves_present_empty_frozen_note(monkeypatch):
    calls: list[tuple[str, str, str | None]] = []

    async def fake_export(*_args, **_kwargs):
        return {
            "nodes": [
                {
                    "id": "AST",
                    "name": "old",
                    "note": "CARTOGRAPHER_VIRTUAL_GROUP: python",
                }
            ]
        }

    def helper(path, name, note):
        calls.append((path, name, note))
        return {
            "success": True,
            "operation": "updated_beacon",
            "base_name": "f",
            "language": "python",
        }

    target = {
        "success": True,
        "file_node": {"id": "FILE"},
        "file_node_id": "FILE",
        "source_path": "x.py",
        "ext": ".py",
        "language": "python",
        "cartographer": SimpleNamespace(update_beacon_from_node_python=helper),
    }
    monkeypatch.setattr(api_client_nexus, "export_nodes_impl", fake_export)
    monkeypatch.setattr(
        api_client_nexus,
        "_resolve_beacon_target_from_cache",
        lambda _cache, _node_id: target,
    )

    client = object.__new__(WorkFlowyClientNexus)

    # A present frozen empty string is mutation-defining data. It must neither
    # inherit the cache marker for discrimination nor change on helper dispatch.
    result = asyncio.run(client.update_beacon_from_node(node_id="AST", name="f #topic", note=""))
    assert result["success"] is True
    assert calls == [("x.py", "f #topic", "")]

    # None is the retained compatibility signal for an absent frozen field, so
    # only that shape falls back to the cache note and sees the virtual marker.
    compatibility_result = asyncio.run(
        client.update_beacon_from_node(node_id="AST", name="f #topic", note=None)
    )
    assert compatibility_result["reason"] == "virtual_group_noop"
    assert calls == [("x.py", "f #topic", "")]


class _Socket:
    def __init__(self):
        self.payloads: list[dict] = []

    async def send(self, raw: str):
        import json

        self.payloads.append(json.loads(raw))


def _install_handler_fakes(monkeypatch, client):
    launches: list[dict] = []

    async def launch(root_uuid: str, mode: str, **kwargs):
        launches.append({"root_uuid": root_uuid, "mode": mode, **kwargs})
        return {"success": True, "job_id": "JOB", "mode": mode, "root_uuid": root_uuid}

    # S03-B: the F12 WS fronts wrap handlers in a cache_operation_boundary;
    # install the boundary surface on the double (seeded with its own nodes).
    from coherence_harness import make_boundary_capable

    make_boundary_capable(client, nodes=client._get_nodes_export_cache_nodes())
    monkeypatch.setattr(server, "_start_carto_refresh_job", launch)
    monkeypatch.setattr(server, "get_client", lambda: client)
    return launches


def test_false_or_missing_async_flags_never_execute_file_or_folder_reconcile_inline(monkeypatch):
    class Client:
        def _get_nodes_export_cache_nodes(self):
            return []

        async def refresh_file_node_beacons(self, *_args, **_kwargs):
            raise AssertionError("FILE handler reconciled inline")

        async def refresh_folder_cartographer_sync(self, *_args, **_kwargs):
            raise AssertionError("FOLDER handler reconciled inline")

    launches = _install_handler_fakes(monkeypatch, Client())

    file_socket = _Socket()
    asyncio.run(
        server._handle_refresh_file_node(
            {"node_id": "FILE", "node_note": "Path: file.py", "carto_async": False},
            file_socket,
        )
    )
    folder_socket = _Socket()
    asyncio.run(server._handle_refresh_folder_node({"node_id": "FOLDER"}, folder_socket))

    assert [(row["root_uuid"], row["mode"]) for row in launches] == [
        ("FILE", "file"),
        ("FOLDER", "folder"),
    ]
    assert file_socket.payloads[-1]["job_id"] == "JOB"
    assert folder_socket.payloads[-1]["job_id"] == "JOB"


@pytest.mark.parametrize(
    ("result", "should_launch"),
    [
        (None, False),
        ({"success": False, "operation": None}, False),
        ({"success": True, "operation": "noop"}, False),
        ({"success": True, "operation": "updated_beacon"}, True),
        ({"success": True, "operation": "created_beacon"}, True),
        ({"success": True, "operation": "deleted_beacon"}, True),
    ],
)
def test_ast_prepared_gate_allows_only_named_disk_operations(
    monkeypatch, tmp_path, result, should_launch
):
    source_path = tmp_path / "m.py"
    source_path.write_text("def f():\n    pass\n", encoding="utf-8")

    class Client:
        def _get_nodes_export_cache_nodes(self):
            return [
                {"id": "ROOT", "name": "root", "note": f"Root: {tmp_path}"},
                {"id": "FILE", "parent_id": "ROOT", "name": "m.py", "note": "Path: m.py"},
                {"id": "AST", "parent_id": "FILE", "name": "f", "note": "AST_QUALNAME: f"},
            ]

        async def update_beacon_from_node(self, **_kwargs):
            return dict(result) if isinstance(result, dict) else result

    launches = _install_handler_fakes(monkeypatch, Client())
    socket = _Socket()
    asyncio.run(
        server._handle_refresh_file_node(
            {
                "node_id": "AST",
                "node_name": "f #topic",
                "node_note": "AST_QUALNAME: f",
                "carto_async": False,
            },
            socket,
        )
    )

    assert len(launches) == 1
    launch = launches[0]
    assert (launch["root_uuid"], launch["mode"]) == ("AST", "file")
    prepared_result = asyncio.run(
        launch["preparation"]({**launch["frozen_payload"], "_job_file": ""}, {})
    )
    gate = launch["preparation_gate"](prepared_result)
    assert bool(gate.get("proceed")) is should_launch
    assert socket.payloads[-1]["job_id"] == "JOB"
