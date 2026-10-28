from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from workflowy_mcp import carto_job_governance as gov

ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32
DEDUPE_A = "c" * 64


def _cache() -> dict[str, dict]:
    return {
        "vault": {"id": "vault"},
        "repo-a": {"id": "repo-a", "parent_id": "vault"},
        "folder-a": {"id": "folder-a", "parent_id": "repo-a"},
        "file-a": {"id": "file-a", "parent_id": "folder-a"},
        "file-b": {"id": "file-b", "parent_id": "folder-a"},
        "repo-b": {"id": "repo-b", "parent_id": "vault"},
        "file-c": {"id": "file-c", "parent_id": "repo-b"},
    }


def _footprint(tmp_path: Path, name: str = "repo/file.py", root: str = "file-a") -> dict:
    return gov.compute_footprint(tmp_path / name, root, _cache(), kind="file")


def _admit(
    tmp_path: Path,
    jobs: Path,
    *,
    job_id: str = "carto-refresh-file-test",
    dedupe: str = DEDUPE_A,
    footprint: dict | None = None,
    job_type: str = "CARTO_REFRESH",
) -> dict:
    jobs.mkdir(parents=True, exist_ok=True)
    return asyncio.run(
        gov.admit_carto_job(
            job_file=jobs / f"{job_id}.json",
            job_payload={"id": job_id, "type": job_type},
            job_type=job_type,
            dedupe_key=dedupe,
            account_scope_id=ACCOUNT_A,
            footprint=footprint or _footprint(tmp_path),
            launcher="system",
            parent_job_id=None,
            prepared=False,
            jobs_root=jobs,
        )
    )


def _holds(jobs: Path) -> tuple[dict, ...]:
    from workflowy_mcp.mutation_locks import _governance_holds

    return tuple(_governance_holds(jobs))


def _cooldowns(jobs: Path) -> tuple[dict, ...]:
    from workflowy_mcp.mutation_locks import _governance_cooldowns

    return tuple(_governance_cooldowns(jobs, now_epoch=0.0, cooldown_seconds=10.0))


def test_jobs_root_uses_authoritative_config_helper(monkeypatch, tmp_path):
    expected = tmp_path / "configured" / "cartographer_jobs"
    monkeypatch.setattr(gov, "get_cartographer_jobs_dir", lambda: expected)
    assert gov.get_jobs_root() == expected
    paths = gov.governance_paths()
    assert paths.jobs_root == expected


def test_mutex_exclusion_and_busy_taxonomy_across_real_subprocesses(tmp_path):
    """M: drive the actual singleton guard on one isolated ``writer.lock``.

    Admission serialization now lives in memory; this row keeps the real
    two-process exhaustion for the sole writer guard, with the unchanged
    busy-vs-unavailable authority assertions.
    """
    from workflowy_mcp.coherence.paths import writer_lock_path

    runtime = tmp_path / "guard-runtime"
    jobs_root = runtime / "cartographer_jobs"
    scope = gov.derive_account_scope_id(
        gov.normalize_api_base_url("https://workflowy.com/api/v1"),
        "writer-guard-key",
    )
    lock_path = writer_lock_path(scope, jobs_root)
    env = {**os.environ, "WORKFLOWY_MCP_TEST_RUNTIME_DIR": str(runtime)}
    holder_code = """
import time
from pydantic import SecretStr
from workflowy_mcp.models.config import ServerConfig
from workflowy_mcp.server_bootstrap import acquire_single_writer_guard
acquire_single_writer_guard(ServerConfig(
    workflowy_api_key=SecretStr('writer-guard-key'),
    workflowy_api_url='https://workflowy.com/api/v1',
))
print('ACQUIRED', flush=True)
time.sleep(3.0)
"""
    contender_code = """
from pydantic import SecretStr
from workflowy_mcp.models.config import ServerConfig
from workflowy_mcp.server_bootstrap import (
    SingleWriterBusyError,
    SingleWriterUnavailableError,
    acquire_single_writer_guard,
)
config = ServerConfig(
    workflowy_api_key=SecretStr('writer-guard-key'),
    workflowy_api_url='https://workflowy.com/api/v1',
)
try:
    acquire_single_writer_guard(config)
    print('WRONGLY_ACQUIRED')
except SingleWriterBusyError:
    print('BUSY')
except SingleWriterUnavailableError:
    print('BROKEN')
"""
    successor_code = """
from pydantic import SecretStr
from workflowy_mcp.models.config import ServerConfig
from workflowy_mcp.server_bootstrap import acquire_single_writer_guard
acquire_single_writer_guard(ServerConfig(
    workflowy_api_key=SecretStr('writer-guard-key'),
    workflowy_api_url='https://workflowy.com/api/v1',
))
print('ACQUIRED')
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ACQUIRED"
        assert lock_path.exists()
        if os.name == "nt":
            assert lock_path.stat().st_size >= 1
        contender = subprocess.run(
            [sys.executable, "-c", contender_code],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            env=env,
        )
        assert contender.stdout.strip() == "BUSY"
        assert "BROKEN" not in contender.stdout
    finally:
        holder.wait(timeout=5)
    successor = subprocess.run(
        [sys.executable, "-c", successor_code],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        env=env,
    )
    assert successor.stdout.strip() == "ACQUIRED"


def test_mutex_broken_path_fails_closed_and_busy_timeout_is_distinct(tmp_path):
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("x", encoding="utf-8")
    with pytest.raises(gov.AdmissionUnavailableError) as broken:
        gov.acquire_os_lock(parent_file / "writer.lock", timeout_seconds=0)
    assert str(parent_file) in str(broken.value)
    assert not isinstance(broken.value, gov.AdmissionBusyTimeoutError)


def test_open_eacces_is_broken_even_though_windows_lock_eacces_is_busy(monkeypatch, tmp_path):
    def denied(_path):
        raise OSError(errno.EACCES, "open denied")

    monkeypatch.setattr(gov, "_open_lock_handle", denied)
    with pytest.raises(gov.AdmissionUnavailableError):
        gov.acquire_os_lock(tmp_path / "writer.lock", timeout_seconds=0.01)


def test_async_mutex_wait_is_cancellation_aware(tmp_path):
    """C: test-held in-process admission lock, ticker, cancel-aware wait and no
    late transaction; no OS admission lock remains."""
    jobs = tmp_path / "jobs"

    async def scenario() -> None:
        holding = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with gov.admission_lock(jobs):
                holding.set()
                await release.wait()

        holder_task = asyncio.create_task(holder())
        await holding.wait()
        ticks = 0
        stop = asyncio.Event()

        async def ticker() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        ticker_task = asyncio.create_task(ticker())
        started = time.monotonic()

        def cancelled() -> bool:
            return time.monotonic() - started > 0.08

        if os.environ.get("CUT2B_NEUTRALIZE_CANCEL_WAIT"):
            # Probe: the wait ignores cancellation.  Freeing the holder then
            # enters the admissions span instead of raising the cancel error.
            cancel_check = None
            release.set()
            await holder_task
        else:
            cancel_check = cancelled
        with pytest.raises(gov.MutexWaitCancelledError):
            async with gov.admission_lock(
                jobs,
                timeout_seconds=1,
                poll_seconds=0.01,
                cancel_check=cancel_check,
            ):
                pytest.fail("admission body entered after cancellation")

        assert ticks >= 5
        assert tuple(_holds(jobs)) == ()
        release.set()
        await holder_task
        stop.set()
        await ticker_task
        assert tuple(_holds(jobs)) == ()

    asyncio.run(scenario())


def test_contention_errno_taxonomy_is_platform_specific_busy():
    assert gov._is_busy_lock_error(OSError(errno.EAGAIN, "x"), "posix")
    assert gov._is_busy_lock_error(OSError(errno.EACCES, "x"), "nt")
    assert not gov._is_busy_lock_error(OSError(errno.EINVAL, "x"), "posix")


def test_canonical_account_region_artifact_and_dedupe_hashes(tmp_path):
    normalized = "https://api.workflowy.com/v1"
    assert gov.normalize_api_base_url(" HTTPS://API.WORKFLOWY.COM:443/v1/// ") == normalized
    account_expected = hashlib.sha256(
        json.dumps([normalized, "secret"], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    assert (
        gov.derive_account_scope_id(" HTTPS://API.WORKFLOWY.COM:443/v1/// ", "secret")
        == account_expected
    )

    target = tmp_path / "Repo" / "file.py"
    canonical = gov.canonicalize_path(target)
    region_expected = hashlib.sha256(canonical.encode()).hexdigest()
    assert gov.derive_region_key(target, "ignored") == region_expected
    artifact_expected = hashlib.sha256(
        json.dumps([account_expected, region_expected], separators=(",", ":")).encode()
    ).hexdigest()
    assert gov.derive_artifact_key(account_expected, region_expected) == artifact_expected

    override = tmp_path / "source.md"
    ingredients = [
        account_expected,
        "CARTO_REFRESH",
        "file",
        "ROOT",
        "temp_override",
        gov.canonicalize_path(override),
        "true",
        "e" * 64,
    ]
    dedupe_expected = hashlib.sha256(
        json.dumps(ingredients, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert (
        gov.derive_dedupe_key(
            account_scope_id=account_expected,
            job_type="CARTO_REFRESH",
            mode="file",
            root_uuid="ROOT",
            source_regime="temp_override",
            source_path_override=override,
            delete_temp_file_after=True,
            preparation_input_digest="e" * 64,
        )
        == dedupe_expected
    )


def test_dedupe_key_closed_mode_and_source_regime_validation():
    with pytest.raises(ValueError):
        gov.derive_dedupe_key(
            account_scope_id=ACCOUNT_A,
            job_type="CARTO_REFRESH",
            mode="visible",
            root_uuid="x",
        )
    with pytest.raises(ValueError):
        gov.derive_dedupe_key(
            account_scope_id=ACCOUNT_A,
            job_type="CARTO_BULK_APPLY",
            mode="file",
            root_uuid="x",
        )
    with pytest.raises(ValueError):
        gov.derive_dedupe_key(
            account_scope_id=ACCOUNT_A,
            job_type="CARTO_MARKDOWN_GENERATE",
            mode=None,
            root_uuid="x",
            source_regime="invented",
        )


def test_footprint_pathless_markdown_in_markdown_shape_and_fail_closed(tmp_path):
    footprint = gov.compute_footprint(None, "file-a", _cache(), kind="file")
    assert footprint == {
        "kind": "file",
        "path": None,
        "root_uuid": "file-a",
        "ancestor_chain": ["file-a", "folder-a", "repo-a", "vault"],
    }
    with pytest.raises(gov.FootprintUnresolvableError) as missing:
        gov.compute_footprint(tmp_path / "x", "missing", _cache(), kind="file")
    assert "system.cache.refresh" in str(missing.value)
    broken = _cache()
    broken["folder-a"]["parent_id"] = "missing-parent"
    with pytest.raises(gov.FootprintUnresolvableError):
        gov.compute_footprint(None, "file-a", broken, kind="file")


def test_symmetric_overlap_matrix(tmp_path):
    parent_path = tmp_path / "repo"
    child_path = parent_path / "src" / "a.py"
    sibling_path = tmp_path / "other" / "b.py"
    parent = {
        "account_scope_id": ACCOUNT_A,
        **gov.compute_footprint(parent_path, "repo-a", _cache(), kind="folder"),
    }
    child_other_account = {
        "account_scope_id": ACCOUNT_B,
        **gov.compute_footprint(child_path, "file-a", _cache(), kind="file"),
    }
    sibling = {
        "account_scope_id": ACCOUNT_A,
        **gov.compute_footprint(sibling_path, "file-c", _cache(), kind="file"),
    }
    assert gov.footprints_overlap(parent, child_other_account)
    assert gov.footprints_overlap(child_other_account, parent)
    assert not gov.footprints_overlap(parent, sibling)

    pathless_file = {
        "account_scope_id": ACCOUNT_A,
        **gov.compute_footprint(None, "file-a", _cache(), kind="file"),
    }
    pathless_folder = {
        "account_scope_id": ACCOUNT_A,
        **gov.compute_footprint(None, "folder-a", _cache(), kind="folder"),
    }
    cross_account_folder = {
        "account_scope_id": ACCOUNT_B,
        **gov.compute_footprint(None, "folder-a", _cache(), kind="folder"),
    }
    assert gov.footprints_overlap(pathless_file, pathless_folder)
    assert not gov.footprints_overlap(pathless_file, cross_account_folder)

    sibling_a = {
        "account_scope_id": ACCOUNT_A,
        **gov.compute_footprint(None, "file-a", _cache(), kind="file"),
    }
    sibling_b = {
        "account_scope_id": ACCOUNT_A,
        **gov.compute_footprint(None, "file-b", _cache(), kind="file"),
    }
    assert not gov.footprints_overlap(sibling_a, sibling_b)


def test_cross_drive_paths_are_non_overlapping():
    a = {
        "account_scope_id": ACCOUNT_A,
        "kind": "folder",
        "path": r"C:\repo",
        "root_uuid": "a",
        "ancestor_chain": ["a"],
    }
    b = {
        "account_scope_id": ACCOUNT_B,
        "kind": "folder",
        "path": r"D:\repo",
        "root_uuid": "b",
        "ancestor_chain": ["b"],
    }
    assert not gov.footprints_overlap(a, b)


def test_finalize_claim_is_token_safe_max_merges_and_deletes_only_after_tombstone(
    tmp_path, monkeypatch
):
    """C (EXIT/COOL): the real memory finalizer is token-safe, removes the
    exact reservation only after publishing cooldown, and never extends the
    merged terminal timestamp.  File tombstone/delete ordering retires."""
    jobs = tmp_path / "jobs"
    admitted = _admit(tmp_path, jobs)
    claim_id = admitted["claim"]["claim_id"]
    assert admitted["claim_path"] is None
    assert [hold["claim_id"] for hold in _holds(jobs)] == [claim_id]

    async def scenario() -> None:
        # A stale label is an idempotent no-op and cannot remove this entry.
        assert await gov.finalize_claim_async("f" * 32, jobs, now_epoch=100) is True
        assert [hold["claim_id"] for hold in _holds(jobs)] == [claim_id]
        assert await gov.finalize_claim_async(claim_id, jobs, now_epoch=100) is True
        assert tuple(_holds(jobs)) == ()
        first = _cooldowns(jobs)
        assert len(first) == 1
        assert gov._parse_time(first[0]["last_terminal_at"]) == 100

        # A later/absent finalize cannot extend the max-merged timestamp.
        assert await gov.finalize_claim_async(claim_id, jobs, now_epoch=200) is True
        assert _cooldowns(jobs) == first
        if os.environ.get("CUT2B_NEUTRALIZE_FINALIZE_RELEASE_ORDER"):
            from workflowy_mcp import mutation_locks as locks

            locks._LOCKS.cooldowns.clear()
        assert _cooldowns(jobs) == first

    asyncio.run(scenario())


def test_partial_multi_target_q3_lease_create_failure_rolls_back_before_mutation(
    tmp_path, monkeypatch
):
    """C: a blocked final target and duplicate targets insert nothing; the
    provider is the real memory registry, not an injected write failure."""
    jobs = tmp_path / "jobs"
    first = _footprint(tmp_path, "one.py", "file-a")
    second = _footprint(tmp_path, "two.py", "file-b")

    async def scenario() -> None:
        held = await gov.acquire_q3_leases_async(
            [second], account_scope_id=ACCOUNT_A, jobs_root=jobs
        )
        if os.environ.get("CUT2B_NEUTRALIZE_Q3_ALL_OR_NONE"):
            # Probe: insert the first target before validating the last one.
            await gov.acquire_q3_leases_async(
                [first], account_scope_id=ACCOUNT_A, jobs_root=jobs
            )
        try:
            with pytest.raises(gov.GovernanceConflictError):
                await gov.acquire_q3_leases_async(
                    [first, second], account_scope_id=ACCOUNT_A, jobs_root=jobs
                )
        finally:
            assert await gov.release_q3_leases_async(held, jobs_root=jobs) == []

        with pytest.raises(gov.Q3LeaseAcquisitionError):
            await gov.acquire_q3_leases_async(
                [first, first], account_scope_id=ACCOUNT_A, jobs_root=jobs
            )
        assert tuple(_holds(jobs)) == ()

    asyncio.run(scenario())


def test_q3_lease_overlap_is_symmetric_with_active_claim_and_other_lease(
    tmp_path, monkeypatch
):
    """C/MEM (OVERLAP): the admitted job reservation and a live Q3 lease both
    refuse an overlapping Q3 acquisition across accounts, symmetrically."""
    jobs = tmp_path / "jobs"
    footprint = _footprint(tmp_path)
    admitted = _admit(
        tmp_path, jobs, job_id="active", dedupe="c" * 64, footprint=footprint
    )

    async def scenario() -> None:
        if os.environ.get("CUT2B_NEUTRALIZE_Q3_SYMMETRY"):
            monkeypatch.setattr(gov, "footprints_overlap", lambda _left, _right: False)
        with pytest.raises(gov.GovernanceConflictError):
            await gov.acquire_q3_leases_async(
                [footprint], account_scope_id=ACCOUNT_B, jobs_root=jobs
            )

        # Remove the reservation through the one exit door, then acquire a
        # lease and show a second overlapping Q3 operation refuses across
        # accounts while an exact release is permitted.
        assert (
            await gov.finalize_claim_async(admitted["claim"]["claim_id"], jobs) is True
        )
        leases = await gov.acquire_q3_leases_async(
            [footprint], account_scope_id=ACCOUNT_A, jobs_root=jobs
        )
        try:
            with pytest.raises(gov.GovernanceConflictError):
                await gov.acquire_q3_leases_async(
                    [footprint], account_scope_id=ACCOUNT_B, jobs_root=jobs
                )
        finally:
            assert await gov.release_q3_leases_async(leases, jobs_root=jobs) == []

    asyncio.run(scenario())


def test_atomic_governance_writes_never_expose_torn_json(tmp_path):
    path = tmp_path / "claim.json"
    gov.atomic_write_json(path, {"n": 0, "payload": "x" * 10_000})
    stop = threading.Event()
    failures: list[BaseException] = []

    def reader():
        while not stop.is_set():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                assert isinstance(value["n"], int)
                assert len(value["payload"]) == 10_000
            except PermissionError:
                # Windows may briefly deny an ordinary read while os.replace
                # swaps the complete file; retrying is still lock-free and, on
                # every successful open, the JSON must be whole.
                continue
            except BaseException as exc:  # pragma: no cover - failure capture
                failures.append(exc)
                stop.set()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(100):
            gov.atomic_write_json(path, {"n": index, "payload": "y" * 10_000})
    finally:
        stop.set()
        thread.join(timeout=5)
    assert failures == []


def _spawn_source_offenders(sources: dict[str, str]) -> list[str]:
    return [
        name
        for name, source in sources.items()
        if "--carto-job-file" in source or "popen_factory(" in source
    ]


def test_a1_is_wired_into_phase_c_production_launchers():
    import ast
    repo = Path(__file__).resolve().parents[1]
    server_source = (repo / "src/workflowy_mcp/server.py").read_text(encoding="utf-8")
    from test_cut_2b_fences import assert_worker_absent
    assert_worker_absent(repo / "src/workflowy_mcp/weave_worker.py")
    pipeline_source = (repo / "src/workflowy_mcp/markdown_generate_pipeline.py").read_text(
        encoding="utf-8"
    )
    assert "from .carto_job_governance import" in server_source
    assert "adopt_carto_worker_claim" not in server_source
    definitions = {node.name: node for node in ast.parse(server_source).body
                   if isinstance(node, ast.AsyncFunctionDef)}
    for name in ("_start_carto_refresh_job", "_start_carto_markdown_generate_job",
                 "_start_carto_bulk_visible_apply_job"):
        assert "_schedule_carto_job" in ast.get_source_segment(server_source, definitions[name])
    pipeline_definitions = {node.name: node for node in ast.parse(pipeline_source).body
                            if isinstance(node, ast.AsyncFunctionDef)}
    body = ast.get_source_segment(pipeline_source, pipeline_definitions["run_markdown_generate_pipeline"])
    assert "_run_phase8_refresh_inline" in body
    assert "_launch_phase8_refresh_via_authority" not in body

    # No independent writer-process launcher remains reachable; a source-copy
    # reintroduction of the retired spawn literal is rejected.
    from test_n07_carto_fences import _n07_runtime_sources

    sources = _n07_runtime_sources()
    if os.environ.get("CUT2B_NEUTRALIZE_SPAWN_SITE"):
        sources["src/workflowy_mcp/server.py"] += (
            "\ndef _cut2b_restored_worker():\n"
            "    return popen_factory(['--carto-job-file', 'job'])\n"
        )
    assert _spawn_source_offenders(sources) == []
