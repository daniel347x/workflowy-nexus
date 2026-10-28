"""S03B Chain-3 (W1) — prepared CARTO callbacks own ONE operation boundary.

Prepared CARTO launches execute their preparation callback inside an
isolated task (the inherited OperationCacheSnapshot is cleared by the real
``_isolated_task_factory``).  S03B Chain-3 W1: ``server._start_carto_refresh_job``
wraps every prepared callback in ONE execution-site
``cache_operation_boundary`` — invoked by ``prepared_runner`` only AFTER
``obtain_freshness()`` has selected/published the current basis — so the
callback adopts and pins that generation exactly once, keeps it for the
complete callback, resets on every exit path, and never relies on a
boundary-less ``_nodes_export_cache`` property read (the retired fallback's
route — S03 closure, CF-S03CR-01).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from functools import wraps
from pathlib import Path

from coherence_harness import NOW, publish_fixture_head, s02_node

from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp import server
from workflowy_mcp.coherence import (
    initialize_manifest_if_absent,
    manifest_with_updates,
    new_bootstrap_manifest,
    write_manifest,
)
from workflowy_mcp.coherence.effects import SemanticEffect
from workflowy_mcp.coherence.journal_store import append_journal_transaction
from workflowy_mcp.coherence.operation_cache import (
    cache_operation_boundary,
    current_operation_cache,
)
from workflowy_mcp.coherence.schema import JournalProducer
from workflowy_mcp.coherence.snapshot_store import (
    new_snapshot_candidate_path,
    write_snapshot_candidate,
)


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


BASE_NODES = [
    s02_node("A", name="alpha"),
    s02_node("B", name="beta", parent_id="A"),
    # S05 (A1c): the vault/repo/file chain mirrors _footprint's local cache
    # so the CARTO head-currency gate's revalidation can rehydrate the
    # candidate chain from the adopted view when a journal append lands
    # between the origin boundary and the prepared launch.
    s02_node("vault", name="vault"),
    s02_node("repo", name="repo", parent_id="vault"),
    s02_node("file", name="file", parent_id="repo"),
]


def _producer() -> JournalProducer:
    return JournalProducer(
        pid=os.getpid(), pid_create_time=1.0, owner_kind="process", ownership_kind="none"
    )


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
    config = APIConfiguration(
        api_key=SecretStr("test-key-1234"), base_url="https://workflowy.com/api/v1"
    )
    return WorkFlowyClientCore(config), jobs_root, snapshot_dir


def _seed_lineage(tmp_path: Path, monkeypatch):
    """A warm-started client (adopted seq 0) over a real manifest + base."""
    probe, jobs_root, snapshot_dir = _make_client(tmp_path, monkeypatch)
    paths = probe._coherence_paths()
    account = paths.account_root.name
    manifest = initialize_manifest_if_absent(
        paths, new_bootstrap_manifest(account, now=NOW)
    )
    candidate = write_snapshot_candidate(
        new_snapshot_candidate_path(snapshot_dir),
        {"nodes": [dict(n) for n in BASE_NODES]},
        account_scope_id=account,
        epoch_number=manifest.epoch_number,
        epoch_id=manifest.epoch_id,
        incorporated_through_seq=0,
        publication_kind="full_refresh",
    )
    write_manifest(
        paths,
        manifest_with_updates(
            manifest, base_snapshot_path=candidate, coherence_mode="transition"
        ),
        expected_epoch_id=manifest.epoch_id,
        expected_head_seq=0,
    )
    client, _jr, _sd = _make_client(tmp_path, monkeypatch)
    return client, paths, account, manifest, candidate, jobs_root


def _install_launch_mocks(
    monkeypatch,
    client,
    account,
    manifest,
    candidate,
    jobs_root,
    tmp_path,
    *,
    freshness=None,
):
    """Route the REAL server launch seam at the isolated environment.

    ``freshness`` optionally overrides the default static snapshot
    selection — e.g. a callback that establishes a new journal generation
    while it executes, so a test can prove freshness precedes boundary
    adoption by data dependency rather than fixture arrangement."""
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_governance_jobs_root", lambda: str(jobs_root))
    from n07_job_harness import install_legacy_task_fixture
    fixture = install_legacy_task_fixture(monkeypatch, jobs=jobs_root, account=account, client=client)

    async def _freshness(*, reason: str, source: str):
        return {
            "snapshot_path": str(candidate),
            "freshness_disposition": "performed",
            "snapshot_lineage": {
                "account_scope_id": account,
                "epoch_number": manifest.epoch_number,
                "epoch_id": manifest.epoch_id,
                "incorporated_through_seq": 0,
            },
        }

    monkeypatch.setattr(server, "_carto_select_snapshot", freshness or _freshness)

    return fixture.calls  # actual in-task work replaces the former spawn lever


def _footprint(tmp_path: Path):
    cache = {
        "vault": {"id": "vault"},
        "repo": {"id": "repo", "parent_id": "vault"},
        "file": {"id": "file", "parent_id": "repo"},
    }
    return gov.compute_footprint(tmp_path / "repo" / "file.py", "file", cache, kind="file")


async def _launch_and_drain(preparation, tmp_path: Path):
    """Drive the REAL ``server._start_carto_refresh_job`` seam (which uses
    the real default ``_isolated_task_factory``) and drain the prepared
    runner task the in-process observer registers."""
    result = await server._start_carto_refresh_job(
        "file",
        "file",
        launcher="gui",
        preparation=preparation,
        frozen_payload={},
        preparation_input_digest=hashlib.sha256(b"prepared-boundary-test").hexdigest(),
        initial_footprint=_footprint(tmp_path),
    )
    assert isinstance(result, dict) and result.get("success") is True, result
    job_id = result["job_id"]
    task = server._carto_inprocess_tasks.get(job_id)
    if task is not None:
        await task
    pending = list(server._carto_inprocess_tasks.values())
    while pending:
        await asyncio.gather(*pending, return_exceptions=True)
        pending = list(server._carto_inprocess_tasks.values())
    return result, job_id


# ---------------------------------------------------------------------------
# W1.1 / W1.2 — inherited context cleared; freshness precedes adoption
# ---------------------------------------------------------------------------


@async_test
async def test_prepared_callback_does_not_inherit_caller_snapshot(tmp_path, monkeypatch):
    """W1.1: an originating operation pinned to T1 launches a prepared job;
    the prepared callback (running in the real isolated task) does NOT
    inherit T1 — it pins the freshly published local T2 view."""
    client, paths, account, manifest, candidate, jobs_root = _seed_lineage(tmp_path, monkeypatch)
    _install_launch_mocks(monkeypatch, client, account, manifest, candidate, jobs_root, tmp_path)
    probes: dict = {}

    async def prepare(payload, selection):
        snap = current_operation_cache(required=False)
        probes["snap"] = snap
        probes["view"] = snap.view if snap else None
        return {"success": True, "operation": "probe"}

    async with cache_operation_boundary(client, "origin") as origin:
        t1_view = origin.view
        assert t1_view.adopted_sequence == 0
        # T2 lands BEFORE the prepared launch.
        append_journal_transaction(
            paths,
            operation_kind="test",
            effects=[SemanticEffect.node_create(s02_node("T2", name="t-two"))],
            producer=_producer(),
        )
        publish_fixture_head(client, paths)
        await _launch_and_drain(prepare, tmp_path)
        assert probes["snap"] is not None  # a boundary exists inside the callback
        assert probes["view"] is not t1_view  # NOT the inherited T1 view object
        assert probes["view"].adopted_sequence == 1  # ...but the fresh T2 generation
        assert "T2" in probes["snap"].nodes_by_id
        # The originating operation is still pinned to T1 afterwards.
        assert current_operation_cache().view is t1_view
    assert current_operation_cache(required=False) is None
    # The isolated task welded its own publisher-current generation.
    assert probes["view"] is client._cache_view_publisher.capture()


@async_test
async def test_freshness_precedes_single_prepared_adoption(tmp_path, monkeypatch):
    """W1.2 (Chain-3 Round-2 reshaped): T2 comes into existence DURING the
    mocked freshness phase — the freshness callback itself appends the T2
    journal record, advancing the manifest head while leaving the selected
    base lineage unchanged.  prepared_runner enters the preparation
    boundary only AFTER obtain_freshness(), so the callback can observe T2
    exclusively when boundary capture follows freshness; a boundary
    entered before freshness would pin the T1 head (seq 0) and could never
    see T2 (the data dependency is the ordering proof).  The boundary must
    perform zero adoptions, pin T2 (seq 1) for the complete callback, and a T3
    record appended after boundary entry must stay invisible."""
    client, paths, account, manifest, candidate, jobs_root = _seed_lineage(tmp_path, monkeypatch)
    probes: dict = {}
    order: list[str] = []
    real_capture = client._capture_view_and_dirty

    def observed_capture():
        order.append("capture")
        return real_capture()

    monkeypatch.setattr(client, "_capture_view_and_dirty", observed_capture)

    def _append(node_id: str, name: str) -> None:
        append_journal_transaction(
            paths,
            operation_kind="test",
            effects=[SemanticEffect.node_create(s02_node(node_id, name=name))],
            producer=_producer(),
        )

    async def _freshness_establishes_t2(*, reason: str, source: str):
        # T2 does not exist before this callback runs: freshness is what
        # brings T2 into existence (manifest head T1 -> T2).
        order.append("freshness-start")
        _append("T2", "t-two")
        publish_fixture_head(client, paths)
        order.append("freshness-complete")
        return {
            "snapshot_path": str(candidate),
            "freshness_disposition": "performed",
            "snapshot_lineage": {
                "account_scope_id": account,
                "epoch_number": manifest.epoch_number,
                "epoch_id": manifest.epoch_id,
                "incorporated_through_seq": 0,
            },
        }

    _install_launch_mocks(
        monkeypatch,
        client,
        account,
        manifest,
        candidate,
        jobs_root,
        tmp_path,
        freshness=_freshness_establishes_t2,
    )

    async def prepare(payload, selection):
        snap = current_operation_cache(required=False)
        probes["seq"] = snap.view.adopted_sequence if snap else None
        probes["has_t2"] = snap is not None and "T2" in snap.nodes_by_id
        # Nested helpers reuse the SAME snapshot (no duplicate adoption).
        async with cache_operation_boundary(client, "nested-helper") as nested:
            probes["nested_same"] = nested is snap
        # T3 appended AFTER boundary entry stays invisible (one generation
        # end-to-end) even though the manifest head has since advanced.
        _append("T3", "t-three")
        later = current_operation_cache()
        probes["t3_invisible"] = later.view is snap.view and "T3" not in later.nodes_by_id
        return {"success": True, "operation": "probe"}

    await _launch_and_drain(prepare, tmp_path)
    # [ap:9V5V3G]: N05 can heal an early T1 pin; observe capture order directly.
    assert "capture" in order
    assert order.index("freshness-complete") < order.index("capture"), order
    assert probes["seq"] == 1  # T2 — observable only because freshness ran first
    assert probes["has_t2"] is True
    assert probes["nested_same"] is True  # W1.7: no duplicate adoption in nested helpers
    assert probes["t3_invisible"] is True
    assert current_operation_cache(required=False) is None


# ---------------------------------------------------------------------------
# W1.3 — the pinned generation holds against later appends
# ---------------------------------------------------------------------------


@async_test
async def test_prepared_boundary_pins_t2_against_later_appends(tmp_path, monkeypatch):
    """W1.3: a T3 record appended AFTER the preparation boundary entry stays
    invisible for the complete callback (one generation end-to-end)."""
    client, paths, account, manifest, candidate, jobs_root = _seed_lineage(tmp_path, monkeypatch)
    _install_launch_mocks(monkeypatch, client, account, manifest, candidate, jobs_root, tmp_path)
    probes: dict = {}

    append_journal_transaction(
        paths,
        operation_kind="test",
        effects=[SemanticEffect.node_create(s02_node("T2", name="t-two"))],
        producer=_producer(),
    )
    publish_fixture_head(client, paths)

    async def prepare(payload, selection):
        first = current_operation_cache()
        # T3 appended after boundary entry: the pinned view must not move.
        append_journal_transaction(
            paths,
            operation_kind="test",
            effects=[SemanticEffect.node_create(s02_node("T3", name="t-three"))],
            producer=_producer(),
        )
        publish_fixture_head(client, paths)
        later = current_operation_cache()
        probes["same_view"] = later.view is first.view
        probes["seq"] = later.view.adopted_sequence
        probes["has_t3"] = "T3" in later.nodes_by_id
        return {"success": True, "operation": "probe"}

    await _launch_and_drain(prepare, tmp_path)
    assert probes["same_view"] is True
    assert probes["seq"] == 1  # still T2
    assert probes["has_t3"] is False


# ---------------------------------------------------------------------------
# W1.4 — the real F12-on-AST consumer chain reads through the boundary
# ---------------------------------------------------------------------------


@async_test
async def test_f12_on_ast_chain_reads_cache_through_the_boundary(tmp_path, monkeypatch):
    """W1.4: the F12-on-AST chain's cache-reading end (export_nodes_impl,
    the actual consumer beneath update_beacon_from_node) is served by the
    preparation boundary's snapshot — NEVER by a boundary-less
    ``_nodes_export_cache`` property read (the retired fallback's route;
    the tripwire property fails the test if the property is touched)."""
    client, paths, account, manifest, candidate, jobs_root = _seed_lineage(tmp_path, monkeypatch)
    _install_launch_mocks(monkeypatch, client, account, manifest, candidate, jobs_root, tmp_path)
    probes: dict = {}

    from workflowy_mcp.client.api_client_core import WorkFlowyClientCore

    def _tripwire(self):
        raise AssertionError(
            "boundary-less _nodes_export_cache property read (retired fallback route)"
        )

    monkeypatch.setattr(WorkFlowyClientCore, "_nodes_export_cache", property(_tripwire))

    async def prepare(payload, selection):
        from workflowy_mcp.client.api_client_etch import export_nodes_impl

        probes["snap_active"] = current_operation_cache(required=False) is not None
        data = await export_nodes_impl(client)  # use_cache=True by default
        probes["ids"] = {node.get("id") for node in data.get("nodes", [])}
        return {"success": True, "operation": "probe"}

    await _launch_and_drain(prepare, tmp_path)
    assert probes["snap_active"] is True
    assert {"A", "B"}.issubset(probes["ids"])  # the real snapshot payload was served


# ---------------------------------------------------------------------------
# W1.5 — all three production prepared callbacks route through the seam
# ---------------------------------------------------------------------------


def test_all_three_prepared_callbacks_route_through_the_wrapped_seam() -> None:
    """W1.5 wiring lock: exactly three production ``preparation=`` call
    sites exist (F12-on-AST, single F12+4/F12+5, bulk F12+4/F12+5) and ALL
    route through ``_start_carto_refresh_job`` — the ONE seam that wraps
    every prepared callback in its execution-site boundary (no direct
    ``launch_carto_refresh_job(... preparation= ...)`` bypass)."""
    import ast as _ast

    source = (Path(__file__).resolve().parents[1] / "src/workflowy_mcp/server.py").read_text(
        encoding="utf-8"
    )
    tree = _ast.parse(source)
    callers: list[str] = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        if not any(keyword.arg == "preparation" for keyword in node.keywords):
            continue
        func = node.func
        name = func.id if isinstance(func, _ast.Name) else (
            func.attr if isinstance(func, _ast.Attribute) else ""
        )
        callers.append(name)
    assert callers.count("_start_carto_refresh_job") == 3  # the three production callbacks
    assert callers.count("launch_carto_refresh_job") == 1  # the seam's own forwarding call
    assert set(callers) == {"_start_carto_refresh_job", "launch_carto_refresh_job"}
    # N07 §9.4: the seam delegates; ONE boundary lives in the common job task.
    start = source.index("async def _start_carto_refresh_job(")
    end = source.index("async def _start_carto_markdown_generate_job(")
    seam_body = source[start:end]
    assert "schedule_job=_schedule_carto_job" in seam_body
    runner = source[source.index("async def _run_carto_job("):source.index("async def _execute_carto_job(")]
    assert 'cache_operation_boundary(get_client(), f"carto:{spec.job_id}")' in runner
    assert 'f"carto.prepare:{spec.mode}"' in runner


# ---------------------------------------------------------------------------
# W1.6 — exit behavior: success / ordinary exception / cancellation
# ---------------------------------------------------------------------------


@async_test
async def test_prepared_success_keeps_claim_and_grant_until_task_exit(tmp_path, monkeypatch):
    """W1.6a: success — the prepared task spawns the worker, leaves the
    claim live (the spawned worker owns it; NO finalization on this path),
    and resets the preparation context.  Chain-3 Round-2 (W2): the reset
    is proven AT THE EXECUTION SITE — the worker-spawn seam is intercepted
    inside the isolated prepared task (after the preparation wrapper and
    its boundary have exited) and observes no active operation snapshot;
    the originating task is independently verified empty afterwards."""
    client, paths, account, manifest, candidate, jobs_root = _seed_lineage(tmp_path, monkeypatch)
    spawned = _install_launch_mocks(
        monkeypatch, client, account, manifest, candidate, jobs_root, tmp_path
    )
    # Chain-3 Round-2 (W2): observe the operation context at the
    # worker-spawn seam — awaited by prepared_runner INSIDE the isolated
    # prepared task AFTER the preparation wrapper (and its boundary) has
    # exited — rather than only from this originating task.
    origin_task = asyncio.current_task()
    observed: dict = {}
    real_run = server._run_carto_job
    async def _run_observed(*args):
        try:
            return await real_run(*args)
        finally:
            observed["ctx"] = current_operation_cache(required=False)
            observed["task"] = asyncio.current_task()
    monkeypatch.setattr(server, "_run_carto_job", _run_observed)

    async def prepare(payload, selection):
        assert current_operation_cache(required=False) is not None
        return {"success": True, "operation": "probe"}

    await _launch_and_drain(prepare, tmp_path)
    assert spawned, "the prepared task must proceed to its in-task refresh"
    assert observed.get("task") is not None  # the spawn seam was reached
    assert observed["task"] is not origin_task  # observed inside the prepared task
    assert observed["ctx"] is None  # wrapper reset proven at its execution site
    assert current_operation_cache(required=False) is None
    # Success leaves the actual reservation for the spawned worker (existing behavior).
    assert spawned[0]["holds"] != ()  # retained until the complete refresh
    from workflowy_mcp import mutation_locks as locks
    assert locks._governance_holds(jobs_root) == ()  # actual task exit finalized and released
    # A subsequent operation adopts fresh — no leaked/stale context.
    async with cache_operation_boundary(client, "after") as after:
        assert after.view is not None
    assert current_operation_cache(required=False) is None


@async_test
async def test_prepared_exception_resets_and_finalizes_failed(tmp_path, monkeypatch):
    """W1.6b: an ordinary exception — the job is recorded failed and the
    claim finalized (existing behavior), with the preparation context
    reset.  Chain-3 Round-2 (W2): the reset is proven from within the
    prepared task itself by intercepting the async finalize seam reached
    by prepared_runner after the wrapped preparation raised (no active
    operation snapshot observed there after wrapper exit)."""
    client, paths, account, manifest, candidate, jobs_root = _seed_lineage(tmp_path, monkeypatch)
    spawned = _install_launch_mocks(
        monkeypatch, client, account, manifest, candidate, jobs_root, tmp_path
    )
    # Chain-3 Round-2 (W2): intercept the async finalize seam reached by
    # prepared_runner after the wrapped preparation raises — INSIDE the
    # isolated prepared task — and observe the operation context there.
    origin_task = asyncio.current_task()
    observed: dict = {}
    real_finalize = gov.finalize_claim_async

    async def _finalize_observed(claim_id, finalize_jobs_root):
        observed["finalizer_ctx"] = current_operation_cache(required=False)
        return await real_finalize(claim_id, finalize_jobs_root)

    monkeypatch.setattr(gov, "finalize_claim_async", _finalize_observed)
    real_run = server._run_carto_job
    async def _run_observed(*args):
        try:
            return await real_run(*args)
        finally:
            observed["ctx"] = current_operation_cache(required=False)
            observed["task"] = asyncio.current_task()
    monkeypatch.setattr(server, "_run_carto_job", _run_observed)

    async def prepare(payload, selection):
        assert current_operation_cache(required=False) is not None
        raise RuntimeError("preparation exploded")

    result, job_id = await _launch_and_drain(prepare, tmp_path)
    assert spawned == []  # never reached the spawn step
    assert observed.get("task") is not None  # the finalize seam was reached
    assert observed["task"] is not origin_task  # observed inside the prepared task
    assert observed["ctx"] is None  # wrapper reset proven at its execution site
    assert observed["finalizer_ctx"] is not None  # finalizer stayed INSIDE the job boundary
    job = json.loads(Path(result["job_file"]).read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert "preparation exploded" in str(job.get("error", ""))
    from workflowy_mcp import mutation_locks as locks
    assert locks._governance_holds(jobs_root) == ()  # finalized and released in memory
    assert any(record.get("job_id") == job_id for record in locks._LOCKS.cooldowns.values())  # memory cooldown
    assert current_operation_cache(required=False) is None
    # The next operation adopts fresh (existing claim/cancellation behavior).
    async with cache_operation_boundary(client, "after"):
        pass
    assert current_operation_cache(required=False) is None


@async_test
async def test_prepared_cancellation_resets_and_finalizes_cancelled(tmp_path, monkeypatch):
    """W1.6c: asyncio.CancelledError — the job is finalized through the
    existing cancellation path (status cancelled, claim released), with
    the preparation context reset.  Chain-3 Round-2 (W2): the reset is
    proven from within the prepared task itself by intercepting the async
    cancellation-finalize seam after wrapper exit."""
    client, paths, account, manifest, candidate, jobs_root = _seed_lineage(tmp_path, monkeypatch)
    spawned = _install_launch_mocks(
        monkeypatch, client, account, manifest, candidate, jobs_root, tmp_path
    )
    # Chain-3 Round-2 (W2): intercept the async cancellation-finalize seam
    # reached after the wrapped callback raises CancelledError — INSIDE
    # the isolated prepared task — and observe the operation context there.
    origin_task = asyncio.current_task()
    observed: dict = {}
    real_finalize_cancelled = gov.finalize_cancelled_job_claim_async

    async def _finalize_cancelled_observed(job_file, claim_id, **kwargs):
        observed["finalizer_ctx"] = current_operation_cache(required=False)
        return await real_finalize_cancelled(job_file, claim_id, **kwargs)

    monkeypatch.setattr(
        gov, "finalize_cancelled_job_claim_async", _finalize_cancelled_observed
    )
    real_run = server._run_carto_job
    async def _run_observed(*args):
        try:
            return await real_run(*args)
        finally:
            observed["ctx"] = current_operation_cache(required=False)
            observed["task"] = asyncio.current_task()
    monkeypatch.setattr(server, "_run_carto_job", _run_observed)

    async def prepare(payload, selection):
        assert current_operation_cache(required=False) is not None
        # The loop-owned control request is the cancellation authority; no
        # durable sentinel participates.
        from workflowy_mcp.carto_job_runtime import current_carto_job_context
        control = current_carto_job_context().job_control
        control.request_user_cancel()
        control.checkpoint("test-preparation")

    result, job_id = await _launch_and_drain(prepare, tmp_path)
    assert spawned == []
    assert observed.get("task") is not None  # the cancellation-finalize seam was reached
    assert observed["task"] is not origin_task  # observed inside the prepared task
    assert observed["ctx"] is None  # wrapper reset proven at its execution site
    assert observed["finalizer_ctx"] is not None
    job = json.loads(Path(result["job_file"]).read_text(encoding="utf-8"))
    assert job["status"] == "cancelled"
    from workflowy_mcp import mutation_locks as locks
    assert locks._governance_holds(jobs_root) == ()
    assert current_operation_cache(required=False) is None
    async with cache_operation_boundary(client, "after"):
        pass
    assert current_operation_cache(required=False) is None
