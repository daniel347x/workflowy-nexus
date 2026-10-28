"""S02 §16.6 — CARTO lineage migration tests + permanent static fences."""

from __future__ import annotations

import asyncio
import json
from functools import wraps
from pathlib import Path

import pytest
from coherence_harness import ACCOUNT_A, NOW, RecordedSleep, s02_node

import workflowy_mcp.server as server
from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp.coherence import (
    manifest_with_updates,
    read_manifest,
    run_full_refresh,
    write_manifest,
)
from workflowy_mcp.coherence.snapshot_store import (
    normalize_snapshot_path,
    write_snapshot_candidate,
)

REPO = Path(__file__).resolve().parents[1]


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _runtime_sources() -> dict[str, str]:
    return {
        path.relative_to(REPO).as_posix(): path.read_text(encoding="utf-8")
        for path in (REPO / "src").rglob("*.py")
        if ".venv" not in path.parts
    }


# ---------------------------------------------------------------------------
# Permanent static fences (H3/H4 postconditions)
# ---------------------------------------------------------------------------


def test_fence_no_newest_timestamp_selector_callers():
    """H4(b): _find_latest_nodes_export_snapshot_path has ZERO production
    callers — newest-timestamp selection can never become authoritative."""
    import re

    for relative, source in _runtime_sources().items():
        # Callers only: an attribute call (`.‑prefix) — never the definition.
        for match in re.finditer(r"\._find_latest_nodes_export_snapshot_path\(", source):
            raise AssertionError(
                f"production caller of _find_latest_nodes_export_snapshot_path "
                f"survives in {relative} at offset {match.start()}"
            )


def test_fence_single_epoch_writer():
    """H4(a): publish_epoch is the sole production writer of epoch lineage
    fields (epoch_number / epoch_id / base_snapshot_path /
    base_incorporated_through_seq / journal_floor_seq)."""
    import ast

    lineage_kwargs = {
        "epoch_number",
        "epoch_id",
        "base_snapshot_path",
        "base_incorporated_through_seq",
        "journal_floor_seq",
    }
    violations: list[str] = []
    for relative, source in _runtime_sources().items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "manifest_with_updates":
                continue
            used = {kw.arg for kw in node.keywords if kw.arg in lineage_kwargs}
            if used and not relative.endswith("coherence/epoch_publisher.py"):
                violations.append(f"{relative}: manifest_with_updates({sorted(used)})")
    assert violations == []


def test_fence_fail_open_lock_excised():
    """H3(d): no proceed-unlocked path remains in production source."""
    for relative, source in _runtime_sources().items():
        assert "proceeding unlocked" not in source, relative


def test_fence_no_direct_legacy_snapshot_writer_calls():
    """H5: _save_nodes_export_cache_to_disk has zero production callers."""
    import re

    for relative, source in _runtime_sources().items():
        for match in re.finditer(r"\._save_nodes_export_cache_to_disk\(", source):
            raise AssertionError(
                f"production caller of _save_nodes_export_cache_to_disk survives "
                f"in {relative} at offset {match.start()}"
            )


def test_fence_folder_sync_no_longer_saves_eager_snapshot():
    """H5: the folder-sync post-structural save call site is removed."""
    source = (REPO / "src/workflowy_mcp/client/api_client_nexus.py").read_text(
        encoding="utf-8"
    )
    tree = __import__("ast").parse(source)
    for node in __import__("ast").walk(tree):
        if isinstance(node, __import__("ast").FunctionDef) or isinstance(
            node, __import__("ast").AsyncFunctionDef
        ):
            if node.name == "refresh_folder_cartographer_sync":
                segment = __import__("ast").get_source_segment(source, node) or ""
                assert "save_nodes_export_cache" not in segment
                return
    raise AssertionError("refresh_folder_cartographer_sync not found")


# ---------------------------------------------------------------------------
# Client factory + manifest fixtures
# ---------------------------------------------------------------------------


def _make_client(tmp_path: Path, monkeypatch):
    from pydantic import SecretStr

    from workflowy_mcp.client.api_client_core import WorkFlowyClientCore
    from workflowy_mcp.models import APIConfiguration

    jobs_root = tmp_path / "cartographer_jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "workflowy_mcp.carto_job_governance.get_cartographer_jobs_dir",
        lambda: jobs_root,
    )

    def _runtime_subdir(*parts):
        if parts == ("nodes_export_cache",):
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            return snapshot_dir
        return tmp_path.joinpath(*parts)

    monkeypatch.setattr(
        "workflowy_mcp.client.api_client_core.get_runtime_subdir", _runtime_subdir
    )
    monkeypatch.setattr(server, "get_runtime_subdir", lambda *parts: tmp_path)
    config = APIConfiguration(
        api_key=SecretStr("test-key-1234"), base_url="https://workflowy.com/api/v1"
    )
    return WorkFlowyClientCore(config), jobs_root, snapshot_dir


async def _publish_base_via_refresh(client, tmp_path: Path, nodes):
    paths = client._coherence_paths()

    async def fetch():
        return {"nodes": nodes}

    return await run_full_refresh(
        paths=paths,
        fetch=fetch,
        settle_seconds=0,
        snapshot_dir=tmp_path / "snapshots",
        sleep=RecordedSleep(),
        poll_seconds=0.02,
    )


# ---------------------------------------------------------------------------
# _carto_select_snapshot — manifest-based selection + skip-toggle narrowing
# ---------------------------------------------------------------------------


@async_test
async def test_carto_select_snapshot_never_uses_latest_timestamp(tmp_path, monkeypatch):
    """§13.1/N07: select the daemon's live view, never a newer disk file."""
    from coherence_harness import publish_fixture_head

    client, jobs_root, snapshot_dir = _make_client(tmp_path, monkeypatch)
    await _publish_base_via_refresh(client, tmp_path, [s02_node("base-node")])
    publish_fixture_head(client, client._coherence_paths())
    # A lexically NEWER unreferenced legacy file must never be selected.
    newer = snapshot_dir / "99999999-999999-999999_pid1_nodes_export_snapshot.json"
    newer.write_text(
        json.dumps(
            {
                "snapshot_timestamp": NOW,
                "nodes_export": {"nodes": [s02_node("poisoned-newer")]},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(server, "get_client", lambda: client)
    calls: list[str] = []

    async def fake_request(**kwargs):
        calls.append("request")
        return {"success": True}

    async def fake_quiescent(_reason):
        return None

    monkeypatch.setattr(server, "_request_nodes_export_cache_refresh_async", fake_request)
    monkeypatch.setattr(server, "_await_nodes_export_cache_quiescent", fake_quiescent)

    selection = await server._carto_select_snapshot(reason="test", source="test")
    assert calls == ["request"]
    ids = set(selection["cache_by_id"])
    assert ids == {"base-node"}
    assert selection["snapshot_lineage"]["epoch_number"] == 1
    assert selection["freshness_disposition"] == "performed"
    assert selection["cache_by_id"] is client._cache_view_publisher.capture().nodes_by_id


@async_test
async def test_optional_skip_selects_manifest_base(tmp_path, monkeypatch):
    """§8/§13.1/N07: skip only freshness; borrow the live manifest-linked view."""
    from coherence_harness import publish_fixture_head

    client, jobs_root, snapshot_dir = _make_client(tmp_path, monkeypatch)
    await _publish_base_via_refresh(client, tmp_path, [s02_node("base-node")])
    publish_fixture_head(client, client._coherence_paths())
    monkeypatch.setattr(server, "get_client", lambda: client)
    server._set_cache_refresh_skip(True, 60.0)
    try:
        async def fail_request(**kwargs):
            raise AssertionError("no refresh may be requested when skipped")

        monkeypatch.setattr(
            server, "_request_nodes_export_cache_refresh_async", fail_request
        )
        selection = await server._carto_select_snapshot(reason="t", source="t")
        assert selection["freshness_disposition"] == "skipped"
        assert set(selection["cache_by_id"]) == {"base-node"}
        assert selection["snapshot_lineage"]
        assert selection["cache_by_id"] is client._cache_view_publisher.capture().nodes_by_id
    finally:
        server._set_cache_refresh_skip(False, 0.0)


@async_test
async def test_skip_without_usable_manifest_base_still_refreshes(tmp_path, monkeypatch):
    """§8: toggle ON but NO usable manifest base → the preflight refresh is
    required anyway (the toggle never suppresses into emptiness)."""
    from coherence_harness import publish_fixture_head

    client, jobs_root, snapshot_dir = _make_client(tmp_path, monkeypatch)
    # No manifest base exists (bootstrap state).
    monkeypatch.setattr(server, "get_client", lambda: client)
    server._set_cache_refresh_skip(True, 60.0)
    calls: list[str] = []

    async def fake_request(**kwargs):
        calls.append("request")
        return {"success": True}

    async def fake_quiescent(_reason):
        # Simulate the refresh having populated a manifest base.
        paths = client._coherence_paths()
        manifest = read_manifest(paths, required=True)
        account = manifest.account_scope_id
        base = write_snapshot_candidate(
            snapshot_dir / "20260101-000000-000000_pid1_nodes_export_snapshot.json",
            {"nodes": [s02_node("after-refresh")]},
            account_scope_id=account,
            epoch_number=manifest.epoch_number,
            epoch_id=manifest.epoch_id,
            incorporated_through_seq=0,
            publication_kind="full_refresh",
        )
        write_manifest(
            paths,
            manifest_with_updates(
                manifest,
                base_snapshot_path=normalize_snapshot_path(base),
                coherence_mode="transition",
            ),
            expected_epoch_id=manifest.epoch_id,
            expected_head_seq=manifest.journal_head_seq,
        )
        publish_fixture_head(client, paths)

    monkeypatch.setattr(server, "_request_nodes_export_cache_refresh_async", fake_request)
    monkeypatch.setattr(server, "_await_nodes_export_cache_quiescent", fake_quiescent)
    try:
        server._set_cache_refresh_skip(True, 60.0)
        selection = await server._carto_select_snapshot(reason="t", source="t")
        assert calls == ["request"]
        assert selection["freshness_disposition"] == "performed"
        assert set(selection["cache_by_id"]) == {"after-refresh"}
        assert selection["cache_by_id"] is client._cache_view_publisher.capture().nodes_by_id
    finally:
        server._set_cache_refresh_skip(False, 0.0)


@async_test
async def test_recovery_and_widget_requests_ignore_the_toggle(tmp_path, monkeypatch):
    """§8: non-toggle-eligible kinds schedule even with the toggle active;
    only the legacy terminal-drain path remains toggle-eligible."""
    _make_client(tmp_path, monkeypatch)
    server._set_cache_refresh_skip(True, 60.0)

    async def fake_worker():
        return None

    monkeypatch.setattr(server, "_nodes_export_cache_refresh_worker", fake_worker)
    try:
        widget = await server._request_nodes_export_cache_refresh_async(
            "widget click", source="websocket:refresh_button", refresh_kind="widget"
        )
        assert widget.get("skipped") is not True
        skipped = await server._request_nodes_export_cache_refresh_async(
            "drain", source="carto:detached-drain", toggle_eligible=True
        )
        assert skipped.get("skipped") is True
    finally:
        server._set_cache_refresh_skip(False, 0.0)
        # Let the scheduled worker task settle.
        await asyncio.sleep(0)
        task = server._nodes_export_refresh_task
        if task is not None:
            await task


# ---------------------------------------------------------------------------
# Diagnostic job lineage metadata (no per-job basis writer)
# ---------------------------------------------------------------------------


@async_test
async def test_launch_job_carries_lineage_metadata(tmp_path):
    """§13.1: base metadata remains diagnostic; launch writes no job basis."""
    jobs_root = tmp_path / "jobs"
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    snapshot_path = write_snapshot_candidate(
        snapshot_dir / "20260101-000000-000000_pid1_nodes_export_snapshot.json",
        {"nodes": [s02_node("file")]},
        account_scope_id=ACCOUNT_A,
        epoch_number=1,
        epoch_id="f" * 32,
        incorporated_through_seq=7,
        publication_kind="full_refresh",
    )

    async def freshness():
        return {"snapshot_path": snapshot_path, "freshness_disposition": "performed",
                "snapshot_lineage": {"account_scope_id": ACCOUNT_A, "epoch_number": 1,
                                     "epoch_id": "f" * 32, "incorporated_through_seq": 7}}

    async def capture_task(spec):
        return {"success": True, "pid": 4242, "detached": False}

    await gov.launch_carto_refresh_job(
        root_uuid="file",
        mode="file",
        account_scope_id=ACCOUNT_A,
        footprint_factory=lambda _selection: gov.compute_footprint(
            tmp_path / "repo" / "a.py", "file", {"file": {"id": "file"}}, kind="file"
        ),
        jobs_root=jobs_root,
        schedule_job=capture_task,
        env={},
        freshness_callback=freshness,
    )
    assert list(snapshot_dir.glob("*.json")) == [Path(snapshot_path)]
    # The job status carries base metadata, never independent execution state.
    job_files = list(jobs_root.glob("*.json"))
    assert job_files
    payload = json.loads(job_files[0].read_text(encoding="utf-8"))
    lineage = payload.get("snapshot_lineage")
    assert lineage is not None
    assert lineage["epoch_number"] == 1
    assert lineage["epoch_id"] == "f" * 32
    assert lineage["incorporated_through_seq"] == 7
    assert payload.get("snapshot_path")


@async_test
async def test_bulk_f12p3_gesture_performs_one_selection(tmp_path, monkeypatch):
    """§13.4: bulk F12+3 performs ONE gesture-level freshness selection,
    shared by every launched child."""
    selections: list[str] = []
    launched: list[dict] = []

    async def fake_select(**kwargs):
        selections.append(kwargs.get("reason", ""))
        return {
            "snapshot_path": str(tmp_path / "gesture.json"),
            "payload": {"nodes_export": {"nodes": []}},
            "nodes": [],
            "cache_by_id": {
                "root": {"id": "root", "note": f"Root: {tmp_path}"},
                "f1": {"id": "f1", "parent_id": "root", "note": "Path: a.md"},
                "f2": {"id": "f2", "parent_id": "root", "note": "Path: b.md"},
            },
            "freshness_disposition": "gesture_performed",
            "snapshot_lineage": {"epoch_id": "f" * 32},
        }

    async def capture_task(spec):
        launched.append({"root_uuid": spec.root_uuid, "selected_snapshot": dict(spec.selection_metadata)})
        return {"success": True, "pid": 12345}

    async def no_launch_dirt(*args, **kwargs):
        return None

    async def admit(**kwargs):
        return {"claim": {"claim_id": "fixture-claim"}, "claim_path": str(tmp_path / "claim.json")}

    monkeypatch.setattr(server, "_carto_select_snapshot", fake_select)
    monkeypatch.setattr(server, "_schedule_carto_job", capture_task)
    monkeypatch.setattr(server, "_require_carto_launch_admission", no_launch_dirt)
    monkeypatch.setattr(server, "_carto_account_scope", lambda: "a" * 32)
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(server, "_carto_admission_coherence_kwargs", lambda **kwargs: {})
    monkeypatch.setattr(server, "admit_carto_job", admit)

    # S03-B: the bulk F12+3 WS front wraps the handler in a
    # cache_operation_boundary; install a boundary-capable client double.
    from types import SimpleNamespace

    from coherence_harness import make_boundary_capable

    monkeypatch.setattr(
        server,
        "get_client",
        lambda: make_boundary_capable(SimpleNamespace(), nodes=[]),
    )

    class _WS:
        def __init__(self):
            self.messages: list[dict] = []

        async def send(self, text: str) -> None:
            self.messages.append(json.loads(text))

    ws = _WS()
    visible_tree = {
        "root": {"id": "root", "name": "root", "note": ""},
        "children": [
            {
                "id": "f1",
                "name": "a.md",
                "note": "Path: C\\x\\a.md",
                "children": [],
            },
            {
                "id": "f2",
                "name": "b.md",
                "note": "Path: C\\x\\b.md",
                "children": [],
            },
        ],
    }
    await server._handle_bulk_generate_markdown(
        {"node_id": "root", "walk_mode": "visible", "visible_tree": visible_tree}, ws
    )
    assert len(selections) == 1  # exactly ONE gesture-level selection
    assert len(launched) == 2, json.dumps(ws.messages)
    for child in launched:
        assert child["selected_snapshot"] is not None
        assert child["selected_snapshot"].get("snapshot_lineage") == {"epoch_id": "f" * 32}
    result_msgs = [m for m in ws.messages if m.get("action") == "bulk_generate_markdown_result"]
    assert result_msgs and result_msgs[-1].get("success") is True


@async_test
async def test_phase8_inline_inherits_parent_without_refresh(tmp_path, monkeypatch):
    """N07 §9.4: parent pin/task/claim, ZERO new freshness or child artifact."""
    from types import SimpleNamespace
    from coherence_harness import make_boundary_capable, make_coherence_root
    from workflowy_mcp.carto_job_runtime import CartoJobControl, CartoJobContext, carto_job_scope
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.markdown_generate_pipeline import _run_phase8_refresh_inline
    from workflowy_mcp.mutation_locks import mutation_region_locks
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    file_uuid = "c02cdc51-0000-4000-8000-000000000001"
    parent_grant = None
    seen = []
    async def refresh(**kwargs):
        assert kwargs["held_grant"] is parent_grant and parent_grant.is_active
        seen.append((asyncio.current_task(), current_operation_cache(), kwargs))
        return {"success": True}
    client = make_boundary_capable(SimpleNamespace(_coherence_paths=lambda: paths,
                                                  refresh_file_node_beacons=refresh), nodes=[s02_node(file_uuid)])
    def forbidden(*args, **kwargs):
        raise AssertionError("inline phase8 requested freshness, a child or file bootstrap")
    monkeypatch.setattr(server, "_carto_select_snapshot", forbidden)
    monkeypatch.setattr(gov, "launch_carto_refresh_job", forbidden)
    control = CartoJobControl(task=asyncio.current_task())
    context = CartoJobContext(tmp_path / "parent.json", tmp_path, None, control, claim_id="parent-claim")
    async with cache_operation_boundary(client, "parent-markdown") as parent:
        async with mutation_region_locks(client=client, operation_label="parent-markdown",
                region_factory=lambda snapshot: (build_region_subtree(file_uuid, cache_by_id=snapshot.nodes_by_id),)) as held:
            parent_grant = held
            with carto_job_scope(context):
                result = await _run_phase8_refresh_inline(client, job_context=context,
                                                         root_uuid=file_uuid, held_grant=held)
                assert seen == [(asyncio.current_task(), parent, {"file_node_id": file_uuid, "dry_run": False,
                    "cancel_callback": control.user_cancel_requested, "carto_claim_id": "parent-claim",
                    "held_grant": held})]
                assert seen[0][2]["held_grant"] is held
                assert current_operation_cache() is parent
    assert result["execution"] == "inline" and result["status"] == "completed"
    assert control.may_have_workflowy_effects is True
    assert not list(tmp_path.glob("*.json"))
    assert "post_refresh_job_id" not in result and "continuation_live" not in result
