"""CARTO job GC follow-up -- terminal jobs are collected again.

RECON AND FIX socket [ap:7125X0] (follow-up, program [ap:HJNU1W]).
``_gc_carto_jobs`` lost its only two callers on 2026-08-22 (commit 6acbea9,
"Cartographer governance Phase C admission", which replaced the launch bodies
with wrappers over the shared launch authority and dropped the "opportunistic
GC" lines).  Every job since stayed on disk (224 JSONs, ~2.5 GB, oldest
20260822-180304 on the live machine).  A latent second fault would have ended
any restored pass early: the cancellation path writes an AWARE ``updated_at``
(``...+00:00``) while every other writer writes a naive UTC one, and comparing
the aware value with the GC's naive cutoff raised TypeError inside the GC's
best-effort outer ``except`` -- the whole pass silently stopped there.

Post-fix: the jobs watcher starts one single-flight GC pass (worker thread) at
startup and every 10 minutes, and timestamps are normalized to naive UTC.
The job JSONs here are shaped like today's worker output (fields copied from
the live 2026-09-28 jobs).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from workflowy_mcp import carto_job_governance as gov  # noqa: E402
from workflowy_mcp import server as srv  # noqa: E402

ACCOUNT = "b" * 32


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Governance config double + a private F12 artifact dir (never the real one)."""
    admission = SimpleNamespace(
        cooldown_seconds=60,
        start_grace_seconds=30,
        spawn_grace_seconds=60,
        orphan_grace_seconds=120,
    )
    monkeypatch.setattr(
        srv,
        "get_server_config",
        lambda: SimpleNamespace(
            admission=admission,
            governance=SimpleNamespace(mutex_wait_seconds=10),
            retention=SimpleNamespace(carto_job_max_age_seconds=3600),
        ),
    )
    f12_dir = tmp_path / "f12"
    f12_dir.mkdir()
    monkeypatch.setattr(srv, "_get_carto_file_refresh_dir", lambda: str(f12_dir))
    monkeypatch.setattr(srv, "log_event", lambda *a, **k: None)


def _naive(dt: datetime) -> str:
    return dt.replace(tzinfo=None).isoformat()


def _aware(dt: datetime) -> str:
    return dt.isoformat()


def _write_real_shaped_job(
    jobs: Path,
    *,
    job_id: str,
    status: str,
    age: timedelta,
    aware_updated_at: bool = False,
    claim_id: str | None = None,
) -> dict:
    """A CARTO_REFRESH job JSON shaped like today's detached-worker output:
    aware ``created_at``, naive-UTC ``updated_at`` (aware on the cancellation
    path), no ``finished_at``, claim identity, a job log and a per-job folder
    named ``YYYYMMDD-HHMMSS_<id>`` from ``created_at``."""
    jobs.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    created = now - age - timedelta(minutes=10)
    updated = now - age
    stem = f"{created.strftime('%Y%m%d-%H%M%S')}_{job_id}"
    log_path = jobs / f"{stem}.log"
    log_path.write_text("[WEAVE_WORKER] Starting CARTO worker\n", encoding="utf-8")
    job_dir = jobs / stem
    job_dir.mkdir()
    (job_dir / "x_file_refresh_ab12_file.py.json").write_text("{}", encoding="utf-8")
    payload = {
        "id": job_id,
        "type": "CARTO_REFRESH",
        "mode": "folder",
        "root_uuid": "7834ca3d-2f6b-47a2-aff4-9138aa57612f",
        "created_at": _aware(created),
        "updated_at": _aware(updated) if aware_updated_at else _naive(updated),
        "cache_refresh_required": True,
        "freshness_disposition": "performed",
        "source_regime": "standard",
        "progress": {
            "total_files": 401,
            "completed_files": 401,
            "current_file": None,
            "current_phase": "completed",
            "nodes_created": 6,
            "nodes_updated": 7,
            "nodes_moved": 156,
            "nodes_deleted": 0,
        },
        "result_summary": {"files_refreshed": 401, "files_deleted": 0, "errors": []},
        "error": None,
        "logs_path": str(log_path),
        "claim_id": claim_id or "75c096239d4f41e9adae5fe8d0e0410a",
        "dedupe_key": "e" * 64,
        "account_scope_id": ACCOUNT,
        "launcher": "system",
        "parent_job_id": None,
        "status": status,
        "handoff_state": "running",
        "spawn_started_at": _aware(created + timedelta(minutes=1)),
        "execution_owner": "detached_worker",
        "pid": 54540,
        "pid_create_time": 1790611062.784156,
        "pid_start_ticks": 17906110627841560,
        "pid_boot_id": None,
    }
    job_path = jobs / f"{stem}.json"
    job_path.write_text(json.dumps(payload), encoding="utf-8")
    return {"json": job_path, "log": log_path, "dir": job_dir}


OLD = timedelta(days=3)
RECENT = timedelta(minutes=5)


def _mark_discharged(artifacts, *, state="skipped_policy"):
    """Fixture evidence, not a real policy/publication claim."""
    path = artifacts["json"]
    job = json.loads(path.read_text())
    record = {"schema_version": 2, "job_id": job["id"],
              "incarnation": srv._carto_job_incarnation(job, path), "path": str(path),
              "relevant": True, "account_scope_id": job["account_scope_id"],
              "terminal_proof": {"status": job["status"], "kind": "persisted_terminal"},
              "state": state, "attempt_token": "fixture-attempt",
              "skip_reason": "cache_refresh_skip_toggle_active"}
    marker = path.parent / ".refresh_claims" / f"{job['id']}.claim.json"
    gov.atomic_write_json(marker, record)
    return marker


def test_old_terminal_clean_exits_are_collected_with_log_and_folder(tmp_path):
    jobs = tmp_path / "jobs"
    completed = _write_real_shaped_job(jobs, job_id="carto-refresh-folder-aaaa0001", status="completed", age=OLD)
    cancelled = _write_real_shaped_job(
        jobs, job_id="carto-refresh-folder-aaaa0002", status="cancelled", age=OLD, aware_updated_at=True
    )
    _mark_discharged(completed)
    _mark_discharged(cancelled)
    srv._gc_carto_jobs(str(jobs))
    for artifacts in (completed, cancelled):
        assert not artifacts["json"].exists()
        assert not artifacts["log"].exists()
        assert not artifacts["dir"].exists()


def test_deny_by_default_everything_else_is_kept(tmp_path):
    jobs = tmp_path / "jobs"
    kept = [
        _write_real_shaped_job(jobs, job_id=f"carto-refresh-folder-keep{i:04d}", status=status, age=age)
        for i, (status, age) in enumerate(
            [
                ("failed", OLD),
                ("completed_with_warnings", OLD),
                ("running", OLD),
                ("cancelling", OLD),
                ("some_future_status", OLD),
                ("", OLD),
                ("completed", RECENT),  # terminal but younger than the age limit
            ]
        )
    ]
    for artifacts in kept:
        if json.loads(artifacts["json"].read_text())["status"] in {"failed", "completed_with_warnings", "completed"}:
            _mark_discharged(artifacts)
    srv._gc_carto_jobs(str(jobs))
    for artifacts in kept:
        assert artifacts["json"].exists()
        assert artifacts["log"].exists()
        assert artifacts["dir"].exists()


def test_an_aware_timestamp_no_longer_ends_the_pass(tmp_path):
    """CHANGE-DETECTING: pre-fix the aware cancelled job raised TypeError
    against the naive cutoff before its deletion, and the outer best-effort
    except ended the whole pass (so any job visited after it survived too)."""
    jobs = tmp_path / "jobs"
    first = _write_real_shaped_job(
        jobs, job_id="a-carto-refresh-folder-cancel", status="cancelled", age=OLD, aware_updated_at=True
    )
    second = _write_real_shaped_job(jobs, job_id="b-carto-refresh-folder-done", status="completed", age=OLD)
    _mark_discharged(first)
    _mark_discharged(second)
    srv._gc_carto_jobs(str(jobs))
    assert not first["json"].exists()
    assert not second["json"].exists()


def test_timestamp_parser_normalizes_to_naive_utc():
    parse = srv._parse_carto_job_timestamp_utc_naive
    assert parse("2026-09-25T10:21:32.668256+00:00") == datetime(2026, 9, 25, 10, 21, 32, 668256)
    assert parse("2026-09-25T06:21:32+04:00") == datetime(2026, 9, 25, 2, 21, 32)
    assert parse("2026-09-28T16:06:07.186116") == datetime(2026, 9, 28, 16, 6, 7, 186116)
    assert parse("not a date") is None


def test_a_live_claim_still_pins_an_old_terminal_job(tmp_path):
    """An actual admitted CARTO reservation pins its aged terminal job.

    CUT-2B removed the claim-directory read: protection is the immutable set
    captured from the one registry plus the retained final-obligation memory.
    """
    from n05_region_harness import node_uuid

    jobs = tmp_path / "jobs"
    pinned = _write_real_shaped_job(
        jobs,
        job_id="carto-refresh-folder-pinned",
        status="completed",
        age=OLD,
    )
    terminal_payload = json.loads(pinned["json"].read_text())
    cache = {
        node_uuid("vault"): {"id": node_uuid("vault")},
        node_uuid("repo"): {"id": node_uuid("repo"), "parent_id": node_uuid("vault")},
        node_uuid("file"): {"id": node_uuid("file"), "parent_id": node_uuid("repo")},
    }
    footprint = gov.compute_footprint(None, node_uuid("file"), cache, kind="file")
    reservation = gov.admit_carto_job_locked(
        job_file=pinned["json"],
        job_payload={
            "id": "carto-refresh-folder-pinned",
            "type": "CARTO_REFRESH",
            "mode": "folder",
        },
        job_type="CARTO_REFRESH",
        dedupe_key="d" * 64,
        account_scope_id=ACCOUNT,
        footprint=footprint,
        launcher="system",
        parent_job_id=None,
        prepared=False,
        jobs_root=jobs,
    )
    assert reservation["success"] is True and reservation["claim_path"] is None
    # Admission's initial write replaced the aged terminal presentation; restore
    # exactly the artifacts this GC witness reasons about.
    pinned["json"].write_text(json.dumps(terminal_payload), encoding="utf-8")
    protected = srv._carto_gc_protection()
    assert "carto-refresh-folder-pinned" in protected
    srv._gc_carto_jobs(
        str(jobs),
        protected_ids=protected,
        protected_paths=srv._carto_gc_paths(protected),
    )
    assert pinned["json"].exists()
    # The exact finalizer removes this empty-graph reservation (no grant exists);
    # protection then ends independently of job history.
    assert asyncio.run(
        gov.finalize_claim_async(
            reservation["claim"]["claim_id"], jobs, now_epoch=1700000000.0,
        )
    ) is True
    assert "carto-refresh-folder-pinned" not in srv._carto_gc_protection()
    protected = srv._carto_gc_protection()
    srv._gc_carto_jobs(
        str(jobs),
        protected_ids=protected,
        protected_paths=srv._carto_gc_paths(protected),
    )
    assert not pinned["json"].exists()


def test_an_inert_legacy_claim_artifact_does_not_pin(tmp_path, monkeypatch):
    """The retired claim file is inert data; it confers no GC protection."""
    jobs = tmp_path / "jobs"
    artifacts = _write_real_shaped_job(
        jobs,
        job_id="carto-refresh-folder-legacy",
        status="completed",
        age=OLD,
    )
    claims = jobs / ".refresh_locks"
    claims.mkdir(parents=True)
    (claims / f"{'e' * 64}.carto-refresh-folder-legacy.lock.json").write_text(
        json.dumps(
            {
                "claim_id": "legacy-claim",
                "job_id": "carto-refresh-folder-legacy",
                "job_type": "CARTO_REFRESH",
                "dedupe_key": "e" * 64,
                "account_scope_id": ACCOUNT,
                "kind": "file",
                "path": None,
                "root_uuid": "7834ca3d-2f6b-47a2-aff4-9138aa57612f",
                "ancestor_chain": ["7834ca3d-2f6b-47a2-aff4-9138aa57612f"],
                "launcher": "system",
                "parent_job_id": None,
                "handoff_state": "running",
                "execution_owner": "launcher",
            }
        ),
        encoding="utf-8",
    )
    if __import__("os").environ.get("CUT2B_NEUTRALIZE_LEGACY_GC_PIN"):
        actual = srv._carto_gc_protection
        def legacy_protection():
            protection = set(actual())
            for artifact in claims.glob("*.lock.json"):
                record = json.loads(artifact.read_text(encoding="utf-8"))
                protection.add(record["job_id"])
            return frozenset(protection)
        monkeypatch.setattr(srv, "_carto_gc_protection", legacy_protection)
    protected = srv._carto_gc_protection()
    srv._gc_carto_jobs(
        str(jobs),
        protected_ids=protected,
        protected_paths=srv._carto_gc_paths(protected),
    )
    assert not artifacts["json"].exists()
    assert (claims / f"{'e' * 64}.carto-refresh-folder-legacy.lock.json").exists()


# ---------------------------------------------------------------------------
# The trigger: the watcher starts a pass at startup, off the loop thread
# ---------------------------------------------------------------------------


def test_watcher_trigger_runs_one_pass_off_the_loop_then_waits_the_interval(monkeypatch, tmp_path):
    """CHANGE-DETECTING: pre-fix nothing ever called _gc_carto_jobs."""
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(srv, "_carto_job_gc_last_started", float("-inf"))
    monkeypatch.setattr(srv, "_carto_job_gc_task", None)
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(tmp_path / "jobs"))

    def _fake_gc(base, *args, **kwargs):
        calls.append((base, threading.get_ident()))

    monkeypatch.setattr(srv, "_gc_carto_jobs", _fake_gc)

    async def exercise():
        loop_thread = threading.get_ident()
        assert srv._maybe_start_carto_job_gc() is True  # startup pass
        assert srv._maybe_start_carto_job_gc() is False  # single-flight
        await asyncio.wait_for(srv._carto_job_gc_task, timeout=5)
        assert srv._maybe_start_carto_job_gc() is False  # inside the 10-minute interval
        srv._carto_job_gc_last_started -= srv._CARTO_JOB_GC_INTERVAL_S
        assert srv._maybe_start_carto_job_gc() is True  # next interval
        await asyncio.wait_for(srv._carto_job_gc_task, timeout=5)
        return loop_thread

    loop_thread = asyncio.run(exercise())
    assert [base for base, _ in calls] == [str(tmp_path / "jobs")] * 2
    assert all(thread != loop_thread for _, thread in calls)


def test_a_failing_pass_is_logged_not_raised(monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr(srv, "log_event", lambda msg, comp="SERVER": logged.append(msg))

    def _boom(base, *args, **kwargs):
        raise OSError("share violation")

    monkeypatch.setattr(srv, "_gc_carto_jobs", _boom)
    asyncio.run(srv._run_carto_job_gc_pass())
    assert logged == ["CARTO job GC pass failed: share violation"]


def test_watcher_loop_wires_the_gc_trigger():
    import inspect

    assert "_maybe_start_carto_job_gc()" in inspect.getsource(srv._carto_jobs_watcher_loop)


@pytest.mark.parametrize("state", ["pending", "reserved", "published", "skipped_policy"])
@pytest.mark.parametrize("manual", [False, True])
def test_final_responsibility_pins_job_and_aged_marker_until_disposition(tmp_path, monkeypatch, state, manual):
    from workflowy_mcp.carto_job_runtime import CartoFinalObligation
    monkeypatch.setattr(srv, "_carto_final_obligations", {})
    monkeypatch.setattr(srv, "_carto_inprocess_tasks", {})
    jobs = tmp_path / "jobs"
    artifacts = _write_real_shaped_job(jobs, job_id="pin", status="completed", age=OLD)
    protected = state in {"observing", "pending", "reserved"}
    if state in {"observing", "pending", "reserved", "published", "skipped_policy"}:
        srv._carto_final_obligations["pin"] = CartoFinalObligation(
            "pin", str(artifacts["json"]), "claim:fixture", ACCOUNT, relevant=True, state=state,
            terminal_proof=None if state == "observing" else {"kind": "task_terminal", "status": "completed"},
        )
    if manual:
        asyncio.run(srv._clear_carto_job_history(str(jobs)))
    else:
        srv._gc_carto_jobs(str(jobs), protected_ids=srv._carto_gc_protection())
    assert artifacts["json"].exists() is protected


def test_orphaned_unfinished_marker_is_not_age_or_manual_cleaned(tmp_path, monkeypatch):
    # N07 keeps this legacy artifact inert; only loop memory pins live debt.
    monkeypatch.setattr(srv, "_carto_final_obligations", {})
    monkeypatch.setattr(srv, "_carto_inprocess_tasks", {})
    jobs = tmp_path / "jobs"
    artifacts = _write_real_shaped_job(jobs, job_id="orphan", status="completed", age=OLD)
    marker = _mark_discharged(artifacts, state="pending")
    artifacts["json"].unlink()
    os.utime(marker, (1, 1))
    unrelated = _write_real_shaped_job(jobs, job_id="unrelated", status="completed", age=OLD)
    srv._gc_carto_jobs(str(jobs))
    asyncio.run(srv._clear_carto_job_history(str(jobs)))
    assert marker.exists()
    assert not unrelated["json"].exists()
    assert srv._carto_final_obligations == {}, "orphaned marker reconstructed terminal debt"
