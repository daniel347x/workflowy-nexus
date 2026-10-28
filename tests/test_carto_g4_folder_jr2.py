"""G4's real FOLDER body: flat boundaries, stops, and the JR2-base differential.

The transport/files are synthetic; the production folder loop, writer, status
funnel, exit door and paged detail are real. The independent old-side oracle is
the exact 157041c folder definition, never a reimplementation or writer output.
"""
from __future__ import annotations

import asyncio
import ast
import copy
import gc
import json
import os
import hashlib
import textwrap
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from workflowy_mcp import carto_job_governance as gov, carto_job_record as records, config as cfg, server as srv
from workflowy_mcp import beacon_refresh_support as support
from workflowy_mcp.client import api_client_nexus as acn
from workflowy_mcp.carto_job_runtime import CartoJobContext, CartoJobControl, CartoJobSpec, carto_job_scope, run_carto_cpu

ROOT = Path(__file__).resolve().parents[1]
BASE = "157041c"


@pytest.fixture(autouse=True)
def isolated_writer_lifetime(monkeypatch):
    monkeypatch.setattr(records, "_job_records", {})
    for name in ("_CARTO_CLEARED_JOB_IDS", "_CARTO_CLEARED_JOB_PATHS", "_CARTO_CLEARED_WRITE_LOGGED"):
        monkeypatch.setattr(cfg, name, set())


def base_folder_definition():
    fixture = json.loads((ROOT / "tests/fixtures/carto_job_records/g4_baseline.json").read_text())
    entry = fixture["definitions"]["refresh_folder_cartographer_sync"]
    source = entry["source"]
    assert hashlib.sha256(source.encode()).hexdigest() == entry["sha256"]
    namespace = {}
    exec(compile(source,
                 "157041c:refresh_folder_cartographer_sync", "exec"), acn.__dict__, namespace)
    return namespace["refresh_folder_cartographer_sync"]


async def joined_detail(job_id):
    pages, offset, slice_offset = [], 0, 0
    while True:
        page = await srv._system_job_status_impl(job_id, view="detail", offset=offset, slice_offset=slice_offset)
        pages.append(page)
        cursor = page.get("detail_page") or {}
        offset, slice_offset = cursor.get("next_offset"), cursor.get("next_slice_offset") or 0
        if offset is None:
            return records.join_carto_job_detail_pages(pages)


class WeakRow(dict):
    pass


class WeakList(list):
    pass


class StopControl(CartoJobControl):
    def checkpoint(self, phase, *, restart_boundary=False):
        seen = self.seen.setdefault(phase, 0) + 1
        self.seen[phase] = seen
        if self.stop == (phase, seen):
            if self.restart:
                self.restart_state["restart_id"] = "fixture-restart"
            else:
                self.request_user_cancel()
        if self.boundary_observer is not None:
            self.boundary_observer(phase)
        super().checkpoint(phase, restart_boundary=restart_boundary)


async def synthetic_job(monkeypatch, directory, *, switched=True, method=None, cases=("clean",),
                         stop=None, restart=False, sizes=(), observer=None, missing=False, new=False,
                         source_directory=None, mode="folder"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "20261027-000000_g4-folder.json"
    path.with_suffix("").mkdir(exist_ok=True)
    control = StopControl(task=asyncio.current_task())
    control.stop, control.restart, control.seen, control.boundary_observer = stop, restart, {}, observer
    context = CartoJobContext(path, path.with_suffix(""), None, control, claim_id="fixture-claim")
    spec = CartoJobSpec(job_id="g4-folder", job_file=path, log_file=None, job_type="CARTO_REFRESH", mode=mode,
                        root_uuid="root", claim_id="fixture-claim", account_scope_id="scope")
    initial = {"id": spec.job_id, "type": spec.job_type, "mode": spec.mode, "status": "running"}
    writer = records.record_for(path) if switched else None
    if writer is not None:
        writer.create(initial)
    else:
        path.write_text(json.dumps(initial), encoding="utf-8")
    attempted, refs, proofs = [], [], []
    # 'again-*' is a second mapped FILE, not a second physical source. It must
    # run, in order, and retain/replace the old row exactly as setdefault did.
    source_directory = Path(source_directory) if source_directory is not None else directory
    entries = [(str(i), str(source_directory / ("0.py" if case.startswith("again-") else f"{i}.py")), True)
               for i, case in enumerate(cases)]
    plan = {"node_by_id": {}, "parent_of": {}, "children_by_parent": {}, "root_path": str(directory),
            "repo_root_abs": str(directory), "present_files": {p for _, p, _ in entries},
            "present_paths": [p for _, p, _ in entries], "missing_paths": [], "refresh_errors": [],
            "file_nodes_info": entries, "existing_file_nodes": entries, "missing_file_nodes": [],
            "missing_count": 0, "total_existing_files": len(entries)}
    if missing:
        plan["missing_file_nodes"] = [("missing", str(directory / "missing.py"), False)]
        plan["missing_count"], plan["missing_paths"] = 1, ["missing.py"]

    def folder_plan(*args):
        yield from ()
        return copy.deepcopy(plan)

    def scan(action, *args, **kwargs):
        if action == "initialize":
            return "owned-disk-state"
        if action == "existing":
            index = next(i for i, (_, p, _) in enumerate(entries) if p == kwargs["source_path"])
            # For a repeated key use the number of completed mapped attempts.
            case = cases[min(len(attempted), len(cases) - 1)] if cases[index].startswith("again-") or len(set(p for _, p, _ in entries)) < len(entries) else cases[index]
            return (case in {"ignored", "again-ignored"}, "fixture ignore")
        if action == "next":
            return False
        if action == "summary":
            return {"disk_scan_performed": bool(new), "disk_files": {}, "ignored_disk_files": 0,
                    "import_available": True, "ignore_sources": ["fixture.nexusignore"]}
        return None

    async def export(*args, **kwargs):
        return {"nodes": [{"id": "root", "name": "root"}]}

    async def refresh(**kwargs):
        index = int(kwargs["file_node_id"])
        case = cases[index]
        attempted.append(index)
        if sizes or (stop is not None and stop[0] == "test:mid-file"):
            assert all(ref() is None for ref in refs), "an earlier completed row/evidence survived"
            await run_carto_cpu("file:progress", acn._write_carto_file_artifact, "progress", {
                "path": str(path), "pending": {"nodes_created": 1}, "retry_counts": {}, "phase": "apply_reconcile"})
        control.checkpoint("test:mid-file")
        if case in {"local", "again-error"}:
            raise ValueError("synthetic local file failure")
        if case in {"runwide", "again-runwide"}:
            raise gov.AdmissionRefusal("overlap_conflict", "synthetic run-wide failure")
        if case in {"escaped", "again-escaped"}:
            raise SystemExit("synthetic escape")
        row = WeakRow(file=entries[index][1], file_node_id=str(index), status="refreshed", success=True,
                      changed=case == "changed", findings=WeakList(), warnings=[], events=[],
                      unknown={"nil": None, "empty": []}, reconcile_children=[])
        if case == "skipped":
            row.update(status="skipped", success=False)
        if case in {"findings", "changed"}:
            row["findings"].append({"code": "fixture", "severity": "warning", "message": "kept",
                                    "members": [{"text": "complete anatomy"}]})
        if sizes:
            row["payload"] = "x" * sizes[index % len(sizes)]
            refs.extend((weakref.ref(row), weakref.ref(row["findings"])))
        return row

    async def delete(*args, **kwargs):
        control.checkpoint("test:delete")
        return True

    async def create(*args, **kwargs):
        control.checkpoint("test:create")
        return SimpleNamespace(id="created")

    async def noop(*args, **kwargs):
        pass

    fake = SimpleNamespace(refresh_file_node_beacons=refresh, delete_node=delete, create_node=create,
                           _mark_nodes_export_dirty=lambda *args: None)
    with monkeypatch.context() as patch:
        patch.setattr(acn, "export_nodes_impl", export)
        patch.setattr(acn, "_plan_carto_folder_groups", folder_plan)
        patch.setattr(acn, "_scan_carto_folder_disk", scan)
        patch.setattr(acn, "_carto_folder_display_path", lambda *args: None)
        patch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(directory))
        patch.setattr(srv, "_carto_live_job_projection", lambda: {})
        patch.setattr(srv, "finalize_claim_async", noop)
        patch.setattr(srv, "finalize_cancelled_job_claim_async", noop)
        patch.setattr(srv, "_record_carto_task_terminal", lambda *args: proofs.append(args[-1]))
        patch.setattr(srv, "_ensure_carto_final_delivery", lambda: None)
        if new:
            patch.setattr(acn, "_plan_carto_folder_creation", lambda *args: {
                "only_on_disk": {"new.py"}, "folders": {}, "candidates": [(str(directory / "new.py"), [])]})
            patch.setattr(acn, "_carto_folder_child_exists", lambda *args: False)
            def prepare(file, *args, result_holder=None, **kwargs):
                prepared = {"file": file, "status": "prepared", "changed": False, "findings": [], "warnings": [], "events": []}
                if result_holder is not None:
                    result_holder.update(prepared)
                return {"prepared": prepared, "file_map": {"name": "new.py", "children": []}}
            patch.setattr(acn, "_prepare_carto_source_file", prepare)
            patch.setattr(support, "beacon_refresh_projection", lambda *args, **kwargs: {
                "state": "valid", "reconcile_children": [], "findings": [], "warnings": []})
        with carto_job_scope(context):
            outcome = {}
            try:
                outcome["result"] = await (method or acn.WorkFlowyClientNexus.refresh_folder_cartographer_sync)(
                    fake, "root", dry_run=not (new or missing), force=True, carto_claim_id="fixture-claim")
            except BaseException as error:
                outcome["exception"] = error
            control.finalizing = True
            await srv._finalize_carto_task(spec, control, outcome)
        detail = await joined_detail(spec.job_id)
    return SimpleNamespace(path=path, detail=detail, control=control, attempted=attempted, refs=refs, context=context,
                           proof=proofs[-1])


def comparable_summary(detail, directory):
    summary = copy.deepcopy(detail["result_summary"])
    # The same physical synthetic paths are used by both runs. Only the
    # documented D3-1 transform is applied to the JR2-base side.
    if isinstance(summary.get("files"), list):
        summary["files"] = [records.mark_carto_reconcile_children(row) for row in summary["files"]]
    return summary


def comparable_detail(detail, directory):
    # Storage metadata and clocks necessarily change with the layout. All
    # application fields, progress, list values and unknown keys are compared.
    result = copy.deepcopy(detail)
    for key in ("record_layout", "evidence", "in_flight", "outcome", "has_errors", "has_warnings", "has_findings",
                "rows_on_disk", "torn_tail_bytes", "unreadable_lines", "unrecognized_lines", "updated_at"):
        result.pop(key, None)
    result["result_summary"] = comparable_summary(detail, directory)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("cases", [
    ("clean", "clean"), ("clean", "local", "skipped"), ("findings",), (),
    ("clean", "runwide"), ("changed", "again-clean"), ("changed", "again-error"),
    ("changed", "again-ignored"),
    ("changed", "again-runwide"), ("changed", "again-escaped"),
])
async def test_folder_detail_equals_jr2_base_with_repeated_keys(monkeypatch, tmp_path, cases):
    # Use identical paths/ids in both runs, retaining the old result as data
    # before removing only these test-owned files for the new independent run.
    old = await synthetic_job(monkeypatch, tmp_path, switched=False, method=base_folder_definition(), cases=cases)
    expected = comparable_summary(old.detail, tmp_path)
    expected_detail = comparable_detail(old.detail, tmp_path)
    old.path.unlink()
    current = await synthetic_job(monkeypatch, tmp_path, cases=cases)
    assert json.loads(current.path.read_text())["record_layout"] == "summary-head/2"
    assert comparable_summary(current.detail, tmp_path) == expected
    assert comparable_detail(current.detail, tmp_path) == expected_detail
    assert current.attempted == old.attempted
    assert "files" not in current.control.terminal_result.get("result_summary", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,nth,repeated,missing,new,restart", [
    ("folder:file-head", 2, False, False, False, False),
    ("folder:file-head", 2, True, False, False, False),
    ("folder:file-head", 2, True, False, False, True),
    ("folder:ignore-existing", 2, True, False, False, False),
    ("test:mid-file", 2, False, False, False, False),
    ("test:mid-file", 2, True, False, False, False),
    ("folder:file-complete", 2, True, False, False, False),
    ("folder:file-complete", 1, False, False, False, False),
    ("folder:delete-head", 1, False, True, False, False),
    ("test:delete", 1, False, True, False, False),
    ("folder:delete-complete", 1, False, True, False, False),
    ("folder:disk-scan", 1, False, False, True, True),
    ("folder:create-file-head", 1, False, False, True, False),
    ("test:create", 1, False, False, True, False),
    ("folder:create-file-complete", 1, False, False, True, False),
    ("folder:complete", 1, True, False, False, False),
])
async def test_folder_stop_boundaries_equal_base_and_keep_landed_rows(monkeypatch, tmp_path, phase, nth, repeated, missing, new, restart):
    cases = ("changed", "again-clean" if repeated else "clean")
    if phase == "folder:complete":
        cases = ("changed", "again-ignored")
    options = dict(cases=cases, stop=(phase, nth), restart=restart, missing=missing, new=new)
    old = await synthetic_job(monkeypatch, tmp_path, switched=False, method=base_folder_definition(), **options)
    expected = comparable_summary(old.detail, tmp_path)
    expected_detail = comparable_detail(old.detail, tmp_path)
    old.path.unlink()
    current = await synthetic_job(monkeypatch, tmp_path, **options)
    assert json.loads(current.path.read_text())["record_layout"] == "summary-head/2"
    assert current.control.terminal_result["status"] == old.control.terminal_result["status"] == "cancelled"
    assert comparable_summary(current.detail, tmp_path) == expected
    assert comparable_detail(current.detail, tmp_path) == expected_detail
    record = json.loads(current.path.read_text())
    assert record["in_flight"] is None
    if repeated:
        row = current.detail["result_summary"]["files"][0]
        assert "interrupted" not in str(row.get("error") or "").lower()
    assert current.attempted == old.attempted


@pytest.mark.asyncio
async def test_2000_folder_files_have_flat_bytes_no_evidence_reads_or_batch_aggregates_and_release_rows(monkeypatch, tmp_path):
    fixture = json.loads((ROOT / "tests/fixtures/carto_job_records/folder_refresh_trim/sources/folder_refresh_trim.record.json").read_text())
    distribution = [len(json.dumps(row, separators=(",", ":"))) for row in fixture["result_summary"]["files"]]
    sizes = [distribution[i % len(distribution)] for i in range(100)]
    totals = {"write": 0, "read": 0, "evidence_read_opens": 0}
    samples, last = [], {"write": 0, "read": 0}
    original = Path.open

    class Meter:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, *args, **kwargs):
            value = self.stream.read(*args, **kwargs)
            totals["read"] += len(value.encode() if isinstance(value, str) else value)
            return value
        def write(self, value):
            totals["write"] += len(value.encode() if isinstance(value, str) else value)
            return self.stream.write(value)

    running = [True]
    def opened(path, mode="r", *args, **kwargs):
        stream = original(path, mode, *args, **kwargs)
        if not running[0] or not str(path).startswith(str(tmp_path)):
            return stream
        if path.name == "files.jsonl" and "r" in mode:
            totals["evidence_read_opens"] += 1
        return Meter(stream)

    def observe(phase):
        if phase == "folder:file-complete":
            samples.append({key: totals[key] - last[key] for key in last})
            last.update({key: totals[key] for key in last})
        elif phase == "folder:ignore-summary":
            # Requested detail is allowed to read evidence, not part of run IO.
            running[0] = False

    def forbidden(*args, **kwargs):
        assert not running[0], "batch aggregate reached a running FOLDER boundary"
        return original_aggregate(*args, **kwargs)

    original_aggregate = support.aggregate_beacon_outcomes
    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(support, "aggregate_beacon_outcomes", forbidden)
    result = await synthetic_job(monkeypatch, tmp_path, cases=("clean",) * 2000, sizes=sizes, observer=observe)
    assert len(samples) == 2000
    assert sum(s["write"] for s in samples[-100:]) <= 1.1 * sum(s["write"] for s in samples[:100])
    assert max(s["read"] for s in samples) < 64 * 1024
    assert totals["evidence_read_opens"] == 0
    assert all(ref() is None for ref in result.refs)
    saved = json.loads(result.path.read_text())
    assert "files" not in saved["result_summary"] and saved["evidence"]["files"]["rows"] == 2000
    assert len(result.detail["result_summary"]["files"]) == 2000


def test_admission_writes_every_registered_kind_v2_once_with_its_writer(monkeypatch, tmp_path):
    from workflowy_mcp import mutation_locks as locks
    monkeypatch.setattr(locks, "_LOCKS", locks._RegionRegistry())
    for index, (kind, mode) in enumerate((("CARTO_REFRESH", "folder"), ("CARTO_REFRESH", "file"),
                                         ("CARTO_BULK_APPLY", "folder"), ("CARTO_MARKDOWN_GENERATE", "file"))):
        path = tmp_path / f"job-{index}.json"
        calls = []
        result = gov.admit_carto_job_locked(job_file=path, job_payload={"id": f"admit-{index}", "type": kind, "mode": mode},
            job_type=kind, dedupe_key=f"{index:064x}", account_scope_id="a" * 32,
            footprint={"kind": "file", "path": str(tmp_path / f"{index}.py"), "root_uuid": str(index), "ancestor_chain": [str(index)]},
            launcher="test", parent_job_id=None, prepared=True, jobs_root=tmp_path,
            atomic_writer=lambda file, payload: (calls.append(copy.deepcopy(payload)), file.write_text(json.dumps(payload))))
        assert result["success"] and len(calls) == 1
        record = json.loads(path.read_text())
        assert record["record_layout"] == "summary-head/2"
        assert records.record_for(path, create=False) is not None
        assert record == calls[0]
        assert record["outcome"] == "running" and not any(record[key] for key in ("has_errors", "has_warnings", "has_findings"))
        assert record["in_flight"] is None and record["evidence"]["files"]["rows"] == 0


def test_writer_finalization_keeps_exact_preview_totals_not_its_preview_lengths(tmp_path):
    writer = records.record_for(tmp_path / "preview.json")
    writer.create({"id": "preview", "type": "CARTO_REFRESH", "mode": "folder"})
    for i in range(75):
        writer.append_file_row({"file": str(tmp_path / f"{i}.py"), "status": "refreshed", "changed": True})
        writer.append_item("errors", str(i))
    result = writer.finalize({"status": "completed", "result_summary": writer.summary()})
    assert result["result_summary"]["preview_totals"]["files_changed"] == 75
    assert result["result_summary"]["preview_totals"]["errors"] == 75


@pytest.mark.asyncio
async def test_b9_running_folder_attempt_is_in_progress_not_failed_and_has_no_previous_row_in_memory(monkeypatch, tmp_path):
    seen = []
    path = tmp_path / "20261027-000000_g4-folder.json"
    def observe(phase):
        if phase != "folder:in-flight":
            return
        record = json.loads(path.read_text())
        placeholder = record["in_flight"]
        assert placeholder is not None
        evidence = path.with_suffix("") / "files.jsonl"
        folded = records.fold_carto_job_evidence(evidence.read_bytes() if evidence.exists() else b"",
            in_flight=placeholder, status=record["status"])
        assert folded["in_progress"]["status"] == "running" and folded["in_progress"]["error"] is None
        assert all(row["file"] != placeholder["file"] for row in folded["rows"].get("files", ()))
        seen.append(placeholder["file"])
    result = await synthetic_job(monkeypatch, tmp_path, cases=("clean", "clean"), observer=observe)
    assert len(seen) == 2 and len(result.detail["result_summary"]["files"]) == 2
