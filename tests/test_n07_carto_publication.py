"""CF21 integrated witnesses for the unchanged synchronous N01 COW bridge.

No publication implementation edit belongs to N07. These tests exercise the
real client-bound bridge under a scheduled job and its real region/token exit.
"""

from __future__ import annotations

import asyncio

import pytest

from n07_job_harness import scrubbed_runtime, real_job_environment, reject_raw_job_cancel
from workflowy_mcp import carto_job_runtime as runtime


@pytest.mark.asyncio
async def test_bridge_build_failure_leaves_publisher_and_dirty_unchanged(real_job_environment, monkeypatch):
    """P01: fail the bridge's OWN successor build after the private transform."""
    from workflowy_mcp import carto_job_governance as gov
    from workflowy_mcp.coherence.cache_view import CacheView
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.mutation_locks import registry_held_regions
    env = real_job_environment
    client, target = env.client, env.ids["file"]
    publisher, policy = client._cache_view_publisher, client._transition_effect_policy
    client._mark_nodes_export_dirty([target])
    before = publisher.capture()
    before_dirty, before_effects = frozenset(client._nodes_export_dirty_ids), dict(policy.snapshot_effects())
    before_timestamp, before_lineage_error = client._cache_view_snapshot_timestamp, client._nodes_export_lineage_error
    commits, prepared, builds, observed = [], [], [], []
    armed = False
    original_build = CacheView.build
    def fail_build(cls, **kwargs):
        if armed:
            assert prepared and kwargs["ownership"] == "transferred"
            candidate = next(row for row in kwargs["nodes_export"]["nodes"] if row["id"] == target)
            assert candidate["note"] == "P01 private candidate"
            assert before.nodes_by_id[target]["note"] != candidate["note"]
            env.trace("bridge-build-failure", target)
            builds.append(kwargs)
            raise RuntimeError("P01 successor build failed after transform")
        return original_build(**kwargs)
    monkeypatch.setattr(CacheView, "build", classmethod(fail_build))
    def transform(working):
        env.trace("bridge-transform", target)
        for index, row in enumerate(working["nodes"]):
            if row["id"] == target:
                working["nodes"][index] = {**row, "note": "P01 private candidate"}
                prepared.append(True)
                return
        pytest.fail("fixture target disappeared")
    def commit():
        commits.append(True)
        client._nodes_export_dirty_ids.discard(target)
    async def preparation(payload, selection):
        nonlocal armed
        context = runtime.current_carto_job_context()
        context.job_control.may_have_workflowy_effects = True  # cache patch may publish
        pin = current_operation_cache()
        assert pin.owner is publisher and publisher.capture() is before
        armed = True
        try:
            client.publish_local_cache_view(transform, reason="N07-P01-build-failure", commit=commit)
        finally:
            armed = False
            observed.append((publisher.capture(), frozenset(client._nodes_export_dirty_ids), dict(policy.snapshot_effects())))
    with reject_raw_job_cancel(env):
        async with cache_operation_boundary(client, "P01-launch") as caller:
            ack = await env.server._start_carto_refresh_job(target, "file", preparation=preparation,
                frozen_payload={"note": ""},
                preparation_input_digest=gov.derive_preparation_input_digest("ast_beacon_write", target, "", ""),
                initial_footprint=gov.compute_footprint(str(env.repo / "source.py"), target, caller.nodes_by_id, kind="file"))
        assert ack["success"], ack
        task, control = env.tasks[ack["job_id"]], env.controls[ack["job_id"]]
        await task
        await asyncio.sleep(0)
    assert len(prepared) == len(builds) == 1 and not commits, "build failure committed private bookkeeping"
    assert len(observed) == 1 and observed[0][0] is before
    assert observed[0][1:] == (before_dirty, before_effects)
    assert publisher.capture() is before and frozenset(client._nodes_export_dirty_ids) == before_dirty
    assert dict(policy.snapshot_effects()) == before_effects
    assert (client._cache_view_snapshot_timestamp, client._nodes_export_lineage_error) == (before_timestamp, before_lineage_error)
    assert control.terminal_result["status"] == "failed" and "P01 successor build failed" in control.terminal_result["error"]
    assert control.terminal_result["cache_refresh_required"] is True
    events = [event for event in env.events if event["job"] == ack["job_id"]]
    assert not any(event["kind"] in {"publication-before", "publication-after", "send"} for event in events)
    assert [event["kind"] for event in events[-3:]] == ["finalizer-enter", "finalizer-exit", "grant-before-release"]
    assert all(event["held"] for event in events if event["kind"].startswith(("bridge", "finalizer")))
    assert not registry_held_regions().regions and not env.controller._busy()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [False, True], ids=["restored-copy", "omit-update-progress-await"])
async def test_cancel_after_synchronous_publication_preserves_write_and_resets_scopes(real_job_environment, monkeypatch, damage):
    """P02: stop at the FIRST actual safe await after the real HTTP/cache write.

    Observe resets inside the owning task, not just in the isolated observer.
    Nested real archive scopes carry distinct identities across that unwind.
    """
    from workflowy_mcp import carto_job_governance as gov
    from workflowy_mcp.archive_context import ArchiveOperationContext, current_archive_context
    from workflowy_mcp.client import api_client_core as core, api_client_nexus as nexus
    from workflowy_mcp.coherence.operation_cache import archive_operation_scope, cache_operation_boundary, current_operation_cache
    from workflowy_mcp.mutation_locks import registry_held_regions
    env = real_job_environment
    client, target = env.client, env.ids["file"]
    publisher, policy = client._cache_view_publisher, client._transition_effect_policy
    before = publisher.capture()
    publication, scope_resets, run_resets, stops = [], [], [], []
    inner = ArchiveOperationContext(mode="none", reason="write", operation="P02-inner-write")
    outer = ArchiveOperationContext(mode="none", reason="write", operation="P02-launch-caller")
    original_execute, original_run = env.server._execute_carto_job, env.server._run_carto_job
    def scopes():
        return runtime.current_carto_job_context(), current_operation_cache(required=False), current_archive_context(), core._AMBIENT_OPERATION_CANCEL_PROBE.get()
    async def run(spec, control):
        prior = scopes()
        try:
            return await original_run(spec, control)
        finally:
            after = scopes()
            run_resets.append((prior, after))
            assert all(left is right for left, right in zip(prior, after)), "P02 runner leaked a job/operation/cancel scope"
    async def execute(spec, control, *, held_grant=None):
        assert held_grant is not None and held_grant.is_active
        prior = current_archive_context()
        try:
            with archive_operation_scope(inner):
                assert current_archive_context() is inner
                return await original_execute(spec, control, held_grant=held_grant)
        finally:
            scope_resets.append((prior, current_archive_context()))
            assert current_archive_context() is prior, "P02 cancellation leaked its nested archive scope"
    monkeypatch.setattr(env.server, "_run_carto_job", run)
    monkeypatch.setattr(env.server, "_execute_carto_job", execute)
    # G1 threaded the grant; the old two-argument spy stopped at TypeError,
    # before ANY publication or progress await. Production still has the safe
    # update-progress await. Remove only that await in the named source copy.
    from test_n04_reconcile_insert_after import _source_copy
    changes = [('_carto_pending["nodes_updated"] += 1\n                await _carto_flush_progress(phase="apply_reconcile")',
                '_carto_pending["nodes_updated"] += 1\n                pass')] if damage else []
    monkeypatch.setattr(nexus.WorkFlowyClientNexus, "refresh_file_node_beacons",
        _source_copy(nexus, nexus.WorkFlowyClientNexus, "refresh_file_node_beacons", changes))
    def after_patch(kind, node):
        if not publication:
            assert kind == "_apply_update_to_nodes_export_cache" and node == target
            assert current_archive_context() is inner
            view = publisher.capture()
            assert view is not before and "LINE COUNT:" in view.nodes_by_id[target]["note"]
            assert current_operation_cache().view is view
            publication.append((view, frozenset(client._nodes_export_dirty_ids), dict(policy.snapshot_effects())))
            env.trace("P02-synchronous-patch-returned", target)
    env.patch_gate = after_patch
    original_cpu = runtime.run_carto_cpu
    async def first_safe_await(phase, function, *args, **kwargs):
        context = runtime.current_carto_job_context()
        if publication and not stops and context is not None and not context.job_control.finalizing:
            # This is an actual FILE progress await, AFTER update_node's real
            # governed send and synchronous COW publication/cache patch.
            assert phase == "file:progress" and current_archive_context() is inner
            assert publisher.capture() is publication[0][0]
            assert not context.job_control.user_observed
            context.job_control.request_user_cancel()
            stops.append(phase)
            env.trace("P02-first-safe-await", phase)
        return await original_cpu(phase, function, *args, **kwargs)
    monkeypatch.setattr(nexus, "run_carto_cpu", first_safe_await)
    with reject_raw_job_cancel(env):
        async with cache_operation_boundary(client, "P02-launch") as caller:
            with archive_operation_scope(outer):
                ack = await env.server._start_carto_refresh_job(target, "file")
                assert ack["success"], ack
                task, control = env.tasks[ack["job_id"]], env.controls[ack["job_id"]]
                await task
                await asyncio.sleep(0)
                assert current_archive_context() is outer and current_operation_cache().operation_id == caller.operation_id
            assert current_operation_cache() is caller and current_archive_context() is None
    assert publication, control.terminal_result  # never count a fixture failure as RED
    def safe_await_witness():
        assert stops == ["file:progress"] and control.user_observed and control.terminal_result["status"] == "cancelled", "P02 safe await was not observed"
    if damage:
        with pytest.raises(AssertionError, match="P02 safe await"):
            safe_await_witness()
        return
    safe_await_witness()
    assert scope_resets == [(None, None)] and len(run_resets) == 1
    assert all(value is None for value in run_resets[0][1])
    assert len(publication) == 1 and publisher.capture() is publication[0][0], "cancellation rolled back an acknowledged cache write"
    assert frozenset(client._nodes_export_dirty_ids) == publication[0][1]
    assert dict(policy.snapshot_effects()) == publication[0][2] and target in publication[0][2]
    assert env.probe.nodes[target]["note"] == publisher.capture().nodes_by_id[target]["note"]
    events = [event for event in env.events if event["job"] == ack["job_id"]]
    kinds = [event["kind"] for event in events]
    assert kinds.index("response") < kinds.index("publication-after") < kinds.index("cache-after") < kinds.index("P02-synchronous-patch-returned") < kinds.index("P02-first-safe-await") < kinds.index("finalizer-enter")
    assert kinds.count("send") == 1 and kinds[-1] == "grant-before-release"
    assert control.cpu_future is None and not registry_held_regions().regions and not env.probe.violations
    assert not env.controller._busy() and scopes() == (None, None, None, None)
