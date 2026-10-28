"""N07 task foundation; integrated C01–C10 witnesses are added with their fronts."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import threading
import json
import os
import textwrap
from dataclasses import FrozenInstanceError

import pytest

from n07_job_harness import job_context, scrubbed_runtime, task_core_environment, claimed_task_environment, real_job_environment, launch_real_job, real_note_edit, wait_job_gate
from workflowy_mcp import carto_job_runtime as runtime


def _probe(name: str) -> bool:
    """Named, env-only test-local neutralization (off in every normal run)."""
    return bool(os.environ.get(name))


def _source_copy(module, name: str, replacements):
    """Compile an in-memory mutant of one production function; disk is untouched."""
    source = textwrap.dedent(inspect.getsource(getattr(module, name)))
    for old, new in replacements:
        assert old in source, f"{module.__name__}.{name} no longer contains the mutation seam"
        source = source.replace(old, new, 1)
    namespace = dict(vars(module))
    exec(compile(source, getattr(module, "__file__", name), "exec"), namespace)
    return namespace[name]


def _release_current_carto_grant():
    """Mutant: release the actual outer grant before the exit door finalizes."""
    from workflowy_mcp import mutation_locks as locks
    context = runtime.current_carto_job_context()
    if context is None:
        return None
    for token, hold in tuple(locks._LOCKS.holders.items()):
        metadata = hold.governance
        if metadata is not None and metadata.claim_id == context.claim_id:
            locks._release_region_hold(token, hold.state)
            return metadata
    return None


def _reservation_after_finalize(server_module):
    """Mutant: leave a fresh CARTO registry reservation after terminal release."""
    real = server_module.finalize_claim_async
    async def mutant(*args, **kwargs):
        from workflowy_mcp import mutation_locks as locks
        context = runtime.current_carto_job_context()
        metadata = next((hold.governance for hold in locks._LOCKS.holders.values()
                         if hold.governance is not None and context is not None
                         and hold.governance.claim_id == context.claim_id), None)
        result = await real(*args, **kwargs)
        if metadata is not None:
            footprint = metadata.footprint
            locks._reserve_carto_hold(
                jobs_root=metadata.jobs_root, account_scope_id=metadata.account_scope_id,
                footprint={"kind": footprint["kind"], "path": footprint["path"],
                           "root_uuid": footprint["root_uuid"],
                           "ancestor_chain": list(footprint["ancestor_chain"])},
                job_id=metadata.job_id, job_file=metadata.job_file,
                job_type=metadata.job_type, mode=metadata.mode,
                dedupe_key=metadata.dedupe_key, launcher=metadata.launcher,
                parent_job_id=metadata.parent_job_id, log_file=metadata.log_file,
                freshness_disposition=metadata.freshness_disposition,
                claim_id=metadata.claim_id)
        return result
    return mutant


# Captured BEFORE extraction, receipt20261018T221414517977-4b8d3440-files.
# H/adf3251b canonical reconciler SHA256:
# ff2602ca0f66bac8a12b91edaf456001fb559ea7976e317d2f8aec9ecb184605.
# Each digest covers ALL ordered requests and payloads, full returned plan,
# fixed-clock journal, phase/error/cancel diagnostics, exception and annotated
# input. No sorting/dropping request fields or source order. Full capture is
# preserved in /mnt/c/tmp/n07-cont11-evidence/h-oracle.json for readable audit.
# N04 retires SIX of the 29 cases into _C05_N04_RETIRED below (the create
# protocol intentionally changed); the remaining 23 keep their exact H bytes.
# N04 measured this split against the migrated fakes: 23 of the 29 cases keep
# their exact H bytes, `create_dry_run` also still matches (a dry run emits no
# create at all), and FIVE create-phase cases changed by design. Only those five
# are retired; no replacement hash is computed from the new engine.
_C05_H_ORACLE = {
    'create_dry_run': '86519fc52463efe80634e56d5cea5830a77006298567a8d3ee3893d8e9a183e8',
    'cross_parent_move': 'e52d1a48a75023dcedb4114f98f961af41b7837e90674dc775b50ef77351bbb2',
    'cross_parent_dry_run': '7c61f13a389946e7aec238d096530909aff7e191b7060e98b877d053d2bf6cc2',
    'pure_reorder': '9f6af8395611ac87eac998fbff77df860150e1fdbc5eca85b88c633058d14633',
    'skipped_reorder': '941ba98719e53e612123e095854ff0eec826293ab140599946a89ece40b4e046',
    'normalized_noop': 'ece9ce4e8f07eabfee60e43f7fdd07a1b5deef3f1d1220e344d9106eaf65d646',
    'content_update': '9f2c16ee46ed82c0fd924680ddbe34db2e1b623a270bf3a7d3cda51718b1cf5e',
    'option_b_delete': 'a3310d5ecab2de18422b6ef25d87909ecbd5e4ccaba228e1a9f4b553e6a3c058',
    'delete_threshold': 'a6c6ed97f7311a48d8c0704fac99fe9fc86fd0c2096f3bb8687156458989ded0',
    'delete_force': 'a3310d5ecab2de18422b6ef25d87909ecbd5e4ccaba228e1a9f4b553e6a3c058',
    'delete_dry_run': '1f058be42181915eaa1444491a3ebfaf7d4a40dbd02ac6a5b4c8c3b837e08d1c',
    'no_ledger_no_delete': 'bed30b4e8d0506961f062ffa4e943d81b601d24eb9d1812832471abbccd59bf2',
    'explicit_preservation': 'af8568b80f3bcbcc59b6f00cfdc3ba2ea79410d4006b96066159563544c92ab0',
    'truncated_ledger': 'd9dc0b8f774f17cfc446b91e5004e3ff07c785c123a73b3413fda87eed9384a1',
    'truncated_unknown': '5951943558adff419a6f211383ce2d2d4e802f5465ff41cd112bb9a86dfdf05f',
    'strict_mismatch': 'aaf16446652dde750554be7209a766fa54191a028f727fd75492987d92b2df72',
    'rebase': 'beca9a2ad3378ae99b1ef7f8444f19b6bff53ed4803eb36a4f0ec004f086cc0d',
    'cancel_probe_failure': '60f1850e8e941c2f6e4cc2fd8d248fb06c110a6657eede98cf92b7d6efc90761',
    'bfs_snapshot': '2faad9565bcae756fa5be2eabacffa29416d9de79a47901a03bfb84934dc661b',
    'existing_export_wait': 'a9a8a7129ac67b1632d314031b733b70a721f2e1f50bd5ff93d6e868878e67a6',
    'move_exception': 'ea37058c0db8c0f59e5c5678ea5ec49fe2134a0998329ef5a2d922270a68e5b1',
    'update_exception': '6839e14e2c67fbcbc22c2170817dbe0844a807559b8e356da4c67597f1a5c2b1',
    'delete_exception': '25bad3f596bb23202ba145368d5031bae4708ae402f60704acf90839368938e8',
    'export_exception': '19a5df8910478bc6ea8f78b17ea467a106b675a99423484ba92fc695efad26de',
}

# The FIVE intentionally changed create-phase cases. These digests are the
# pre-N04 H receipt only: N04 changed the create protocol by design (one plain
# run/result per request, physical top/bottom modelling, direct siblings before
# their descendants, one optional normal parent conclusion), so they are NOT a
# golden and are never re-hashed against the new engine. Their new coverage is
# the behavioural N04 witnesses below, each with its before/after rationale.
# Measured pre-N04 -> current bytes (scratch probe, never installed):
#   create_ids_and_synthetic_priorities f2b181c5aaf0331a599377a041f06406afde0796ee5674d775758d008bd76f0b
#   create_exception                    f8b5068d61a7b6dc759a63388ee4690b8a1c565a8896715711bc2d69c433b790
#   clone                               da63e30c071b1fd612cfea8b28426e292b311d54bfd9c05c0ca38e0cf06e9663
#   cancel_prefix                       77112975008d7b7ddffa317cae498f0305c5cf0ea87d040c46479255b1ecebdb
#   cancel_interval                     e107aee027341e8fa46040f0b90a6c069a100bc4aa93a0e71fd7a00383c5d358
_C05_N04_RETIRED = {
    'create_ids_and_synthetic_priorities': (
        'abd228441978feba2a0f90b6882598b1d8dee6cc2e15686e99c68e49a400b850',
        'run/result protocol, head/top physical model, sibling-first order, parent conclusion',
    ),
    'create_exception': (
        'b9451f370569ca8034c221ebf08253fcf252c497210d4ab97930bbc7d90d8237',
        'the failing request is now a plain run and no parent conclusion follows',
    ),
    'clone': (
        '5b951e5c4ae8b6e8e4d5d8417ee012ce238fda25d6f3d7331380593780a4d07c',
        'one top-placed run instead of a (parent,payload) scalar create',
    ),
    'cancel_prefix': (
        'dfd6be31d84a960772d14261d0b8cd30fa61cd1624372f63357e2c0cc2bde887',
        'the recorded pre-cancel write is a plain run',
    ),
    'cancel_interval': (
        '3700bdfaf870d3e122736e69137beb032d83d6ac25caea1b936704d52573dd51',
        'the recorded pre-cancel write is a plain run',
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["file", "ast", "folder", "persistent_new", "temp_override", "markdown", "bulk"])
async def test_job_hold_covers_actual_roots_and_every_effect(real_job_environment, monkeypatch, family):
    """C03: real parse/reconcile/HTTP/cache bridges, not a fake work method."""
    from j_counterfactual import install_j_counterfactual
    install_j_counterfactual(monkeypatch)
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp import markdown_generate_pipeline as pipeline
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    from workflowy_mcp.mutation_locks import _governance_holds, held_mutation_lock_regions
    env = real_job_environment
    if _probe("CUT2B_NEUTRALIZE_REGISTRY_TRIPWIRE"):
        # A fresh CARTO reservation survives the terminal release: the finalizer
        # ordinal stays real, so only the empty-registry projection rejects it.
        monkeypatch.setattr(env.server, "finalize_claim_async",
                            _reservation_after_finalize(env.server))
    sources = []
    original_cpu = runtime.run_carto_cpu
    async def observed_cpu(phase, function, *args, **kwargs):
        context = runtime.current_carto_job_context()
        if context is not None and str(phase).startswith((
                "file:", "folder:", "materialize-write", "beacon:", "markdown-write", "markdown-render",
                "markdown-observe", "markdown-prepare", "markdown-result")):
            event = env.trace("data-entry", phase)
            assert event["task"] is context.job_control.task
            sources.append(event)
        return await original_cpu(phase, function, *args, **kwargs)
    for module in (env.server, nexus, pipeline):
        monkeypatch.setattr(module, "run_carto_cpu", observed_cpu)
    ack = await launch_real_job(env, family)
    assert ack["success"], ack
    job_id = ack["job_id"]
    task, control = env.tasks[job_id], env.controls[job_id]
    await asyncio.wait_for(task, 20)
    await asyncio.sleep(0)
    # J-2 keeps broken.py local while useful folder work completes honestly.
    expected_status = {"completed_with_warnings"} if family == "folder" else {"completed", "completed_with_warnings"}
    assert control.terminal_result["status"] in expected_status, json.dumps(control.terminal_result, default=str)
    assert not env.probe.violations, env.probe.violations
    assert env.probe.sends, "fixture did not reach a real write"
    actual = [event for event in env.events if event["job"] == job_id]
    protected = [event for event in actual if event["kind"] in {"send", "cache-before", "cache-after", "publication-before", "publication-after", "dirty-patch", "data-entry", "finalizer-enter", "finalizer-exit"}]
    assert protected and all(event["held"] for event in protected), "job released ownership before an effect/finalizer"
    operations = {event["operation"] for event in protected}
    assert len(operations) == 1 and None not in operations, "job repinned during execution"
    grants = [event for event in actual if event["kind"] == "grant-enter"]
    assert len(grants) == 1 and grants[0]["operation"] in operations
    expected = {env.ids["folder"] if family == "folder" else
                env.ids["persistent"] if family == "persistent_new" else
                env.ids["temporary"] if family == "temp_override" else
                env.ids["markdown"] if family == "markdown" else env.ids["file"]}
    if family == "bulk":
        expected.add(env.ids["other-file"])
    # The outer grant is an exact lifetime union, not an account-wide hold or
    # requested-AST subtree. Nested CRUD regions may add covered child tokens.
    for event in protected:
        assert expected <= {region.key for region in event["held"] if region.kind == "subtree"}, (family, event)
        assert env.ids["vault"] not in {region.key for region in event["held"]}
        assert event["owner"] is task
    assert any(event["kind"] == "cache-after" for event in protected), "real cache patch did not execute"
    assert len([event for event in protected if event["kind"] == "finalizer-enter"]) == 1
    assert actual[-1]["kind"] == "grant-before-release" and actual[-2]["kind"] == "finalizer-exit"
    assert len(env.tasks) == 1, "Markdown phase8 scheduled a second job"
    assert _governance_holds(env.jobs) == (), "completed job left a registry reservation"
    assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
    assert not held_mutation_lock_regions() and control.cpu_future is None
    if family == "folder":
        assert env.ids["missing"] not in env.probe.nodes
        assert any(row["name"] == "🐍 new.py" for row in env.probe.nodes.values())
        assert any(row["name"] == "🚨 Parse error: broken.py" and row["parent_id"] == env.ids["folder"] for row in env.probe.nodes.values())
    if family in {"persistent_new", "temp_override"}:
        assert sources, "source preparation was not observed"


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_kind", ["cpu", "governor", "send", "short-first", "racing-short"])
async def test_region_conflicts_both_directions_and_disjoint_progress(real_job_environment, monkeypatch, gate_kind):
    """C04: actual edit/read/send/COW and actual competing scheduled jobs."""
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.mutation_locks import MutationSerializationConflictError
    env = real_job_environment
    entered, release = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    thread_release = threading.Event()
    short = None
    if gate_kind == "cpu":
        original = nexus._prepare_carto_source_file
        def gated_source(path, *args, **kwargs):
            if str(path).endswith("/source.py"):
                loop.call_soon_threadsafe(entered.set)
                assert thread_release.wait(10), "fixture source worker not released"
            return original(path, *args, **kwargs)
        monkeypatch.setattr(nexus, "_prepare_carto_source_file", gated_source)
    elif gate_kind == "governor":
        original = env.client._write_governor.acquire_write_slot
        async def gated_slot(*args, **kwargs):
            context = runtime.current_carto_job_context()
            if context and context.job_control.presentation["root_uuid"] == env.ids["file"] and not entered.is_set():
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(env.client._write_governor, "acquire_write_slot", gated_slot)
    else:
        async def send_gate(request):
            context = runtime.current_carto_job_context()
            relevant = (context is None) if gate_kind in {"short-first", "racing-short"} else (context and context.job_control.presentation["root_uuid"] == env.ids["file"])
            if relevant and not entered.is_set():
                entered.set()
                await release.wait()
        env.send_gate = send_gate
    try:
        if gate_kind == "short-first":
            short = asyncio.create_task(real_note_edit(env, "symbol", "AST_QUALNAME: existing\nshort edit"))
            await asyncio.wait_for(entered.wait(), 10)
            before = len(env.probe.sends)
            ack = await launch_real_job(env, "file")
            assert ack["success"] is False and ack["retry_after"] > 0, ack
            assert not env.tasks and len(env.probe.sends) == before
            return
        if gate_kind == "racing-short":
            original = env.server._schedule_carto_job
            original_facts = nexus._prepare_carto_file_facts
            symbol_id = env.ids["symbol"]
            def fresh_facts(*args, **kwargs):
                assert "short edit" in args[2][symbol_id]["note"], "waiter executed against stale pre-edit pin"
                return original_facts(*args, **kwargs)
            monkeypatch.setattr(nexus, "_prepare_carto_file_facts", fresh_facts)
            async def race_after_admission(spec):
                nonlocal short
                short = asyncio.create_task(real_note_edit(env, "symbol", "AST_QUALNAME: existing\nshort edit"))
                await asyncio.wait_for(entered.wait(), 10)
                return await original(spec)
            monkeypatch.setattr(env.server, "_schedule_carto_job", race_after_admission)
        ack = await launch_real_job(env, "file")
        assert ack["success"], ack
        await wait_job_gate(env, entered, job_id=ack["job_id"])
        job = env.tasks[ack["job_id"]]
        if gate_kind == "racing-short":
            # No job effect can pass the in-flight short edit; current proof
            # after its bridge publication must be used before the grant.
            for _ in range(20):
                await asyncio.sleep(0)
            assert not [event for event in env.events if event["job"] == ack["job_id"] and event["kind"] == "send"]
            release.set()
            await short
            await job
            assert env.controls[ack["job_id"]].terminal_result["status"] == "completed"
            assert not env.probe.violations
            return
        before = len(env.probe.sends)
        with pytest.raises(MutationSerializationConflictError) as refused:
            await real_note_edit(env, "symbol", "must not write")
        assert len(env.probe.sends) == before
        extra = env.server._mutation_refusal_extra(refused.value)
        assert extra["holder_job_id"] == ack["job_id"] and extra["retry_after"] == 60.0
        assert extra["holder_id"] != ack["job_id"] and extra["holder_kind"] == "long_lived"
        assert extra["holder_message"] == f"Held by Cartographer job {ack['job_id']}; check system.job.status before retrying."
        await asyncio.wait_for(real_note_edit(env, "outside", "outside completed while job gated"), 10)
        peer = await launch_real_job(env, "peer")
        assert peer["success"], peer
        await asyncio.wait_for(env.tasks[peer["job_id"]], 10)
        assert env.controls[peer["job_id"]].terminal_result["status"] == "completed"
        assert not job.done(), "outside calls only ran after unrelated job gate opened"
        assert env.probe.nodes[env.ids["outside"]]["note"] == "outside completed while job gated"
        assert env.client._cache_view_publisher.capture().nodes_by_id[env.ids["outside"]]["note"] == "outside completed while job gated"
    finally:
        release.set()
        thread_release.set()
        if short is not None:
            await short
        if env.tasks:
            await asyncio.gather(*env.tasks.values())
    assert not env.probe.violations


@pytest.mark.asyncio
@pytest.mark.parametrize("front", ["file", "ast", "folder", "persistent_new", "temp_override", "bulk-materialize", "map", "legacy", "markdown", "bulk-markdown", "public-bulk"])
async def test_every_launch_form_uses_one_daemon_task(real_job_environment, monkeypatch, front):
    """C01: all seven refresh ingress functions, both MD fronts and bulk."""
    from j_counterfactual import install_j_counterfactual
    install_j_counterfactual(monkeypatch)
    env = real_job_environment
    ack = await launch_real_job(env, front)
    assert ack.get("success", ack.get("state") == "queued"), ack
    assert len(env.tasks) == 1, "front/phase8 created zero or multiple execution tasks"
    job_id, task = next(iter(env.tasks.items()))
    control = env.controls[job_id]
    # Real register call's lifetime reference, not a synthetic task roster.
    assert env.controller._job_stops[job_id][1] is task
    await asyncio.wait_for(task, 20)
    expected = {"completed_with_warnings"} if front == "folder" else {"completed", "completed_with_warnings"}
    assert control.terminal_result["status"] in expected, json.dumps(control.terminal_result, default=str)
    assert control.terminal_result["detached"] is False
    assert control.terminal_result["pid"] == __import__("os").getpid()
    assert control.terminal_result["execution_model"] == "daemon_task"
    assert env.probe.sends and not env.probe.violations
    events = [event for event in env.events if event["job"] == job_id or event["target"] == job_id]
    assert [event["kind"] for event in events].index("ack") < [event["kind"] for event in events].index("send")
    assert all(event["owner"] is task for event in events if event["job"] == job_id)
    rows = list(env.jobs.glob("*.json"))
    assert len(rows) == 1 and json.loads(rows[0].read_text())["id"] == job_id
    from workflowy_mcp.mutation_locks import _governance_holds
    assert _governance_holds(env.jobs) == (), "launch form left a registry reservation"
    assert not list(env.jobs.rglob("*.heartbeat*"))
    if front in {"markdown", "bulk-markdown"}:
        assert control.terminal_result["cache_refresh_required"] is True
        assert "post_refresh_job_id" not in json.dumps(control.terminal_result)
        assert "continuation_live" not in json.dumps(control.terminal_result)


@pytest.mark.asyncio
async def test_prepared_freshness_then_one_boundary_through_exit(real_job_environment, monkeypatch):
    """C02: actual admitted B0, frozen empty fields, real prepare+reconcile."""
    from workflowy_mcp import carto_job_governance as gov
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.models import NodeUpdateRequest
    env = real_job_environment
    b0, release = asyncio.Event(), asyncio.Event()
    selection = env.server._carto_select_snapshot
    async def freshness(**kwargs):
        assert runtime.current_carto_job_context() is not None
        assert current_operation_cache(required=False) is None, "execution pinned before B0"
        b0.set()
        await release.wait()
        return await selection(**kwargs)
    monkeypatch.setattr(env.server, "_carto_select_snapshot", freshness)
    async def prepare(payload, selected):
        assert payload["name"] == payload["note"] == "", "frozen empty intent changed"
        env.trace("prepare")
        await env.client.update_node(env.ids["symbol"], NodeUpdateRequest(note="AST_QUALNAME: existing\nprepared"))
        return {"success": True}
    try:
        async with cache_operation_boundary(env.client, "launch-caller") as caller:
            ack = await env.server._start_carto_refresh_job(env.ids["symbol"], "file", preparation=prepare,
                frozen_payload={"name": "", "note": ""},
                preparation_input_digest=gov.derive_preparation_input_digest("ast_beacon_write", env.ids["symbol"], "", ""),
                initial_footprint=gov.compute_footprint(str(env.repo / "source.py"), env.ids["file"], caller.nodes_by_id, kind="file"))
            assert ack["success"] and not b0.is_set(), ack
            caller_id = caller.operation_id
        await wait_job_gate(env, b0, job_id=ack["job_id"])
        assert not [event for event in env.events if event["kind"] == "send"]
    finally:
        release.set()
        if env.tasks:
            await asyncio.gather(*env.tasks.values())
    control = env.controls[ack["job_id"]]
    assert control.terminal_result["status"] == "completed", json.dumps(control.terminal_result, default=str)
    effects = [event for event in env.events if event["job"] == ack["job_id"] and event["kind"] in {"prepare", "send", "cache-after", "finalizer-enter", "finalizer-exit"}]
    assert effects[0]["kind"] == "prepare" and effects[-1]["kind"] == "finalizer-exit"
    assert len({event["operation"] for event in effects}) == 1
    assert all(event["operation"] != caller_id and event["held"] for event in effects)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "cancelled", "failed"])
async def test_phase8_is_parent_owned_and_retains_refresh_relevance(real_job_environment, monkeypatch, outcome):
    """C10: actual full pipeline, governed sends and synchronous cache patches."""
    from workflowy_mcp import markdown_generate_pipeline as pipeline
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    env = real_job_environment
    entered, release = asyncio.Event(), asyncio.Event()
    inline = pipeline._run_phase8_refresh_inline
    phase8 = []
    async def observed_inline(client, **kwargs):
        assert client is env.client
        phase8.append((asyncio.current_task(), current_operation_cache().operation_id))
        result = await inline(client, **kwargs)
        if outcome == "failed":
            assert env.probe.sends, "failure fixture did not first publish real work"
            raise RuntimeError("injected phase8 failure after real writes")
        return result
    monkeypatch.setattr(pipeline, "_run_phase8_refresh_inline", observed_inline)
    async def send_gate(request):
        if phase8 and not entered.is_set():
            entered.set()
            await release.wait()
    env.send_gate = send_gate
    ack = await launch_real_job(env, "markdown")
    assert ack["success"], ack
    job_id, task = ack["job_id"], env.tasks[ack["job_id"]]
    control = env.controls[job_id]
    cancel = None
    try:
        await wait_job_gate(env, entered, job_id=job_id)
        assert len(env.tasks) == len(phase8) == 1 and phase8[0][0] is task
        assert control.may_have_workflowy_effects
        assert len(list(env.jobs.glob("*.json"))) == 1
        if outcome == "cancelled":
            cancel = asyncio.create_task(env.server._system_job_cancel_impl(job_id))
            for _ in range(1000):
                if control.user_requested:
                    break
                await asyncio.sleep(0.001)
            assert control.user_requested and not task.done()
            assert not any(event["kind"] == "finalizer-enter" for event in env.events)
        assert len([event for event in env.events if event["kind"] == "freshness"]) == 1
    finally:
        release.set()
        await task
        if cancel:
            cancelled = await cancel
            assert cancelled["outcome"] == "cooperative" and task.done()
    assert control.terminal_result["status"] == outcome, json.dumps(control.terminal_result, default=str)
    assert control.terminal_result["cache_refresh_required"] is True
    assert not env.probe.violations
    assert len(env.tasks) == 1 and "post_refresh_job_id" not in json.dumps(control.terminal_result)
    protected = [event for event in env.events if event["job"] == job_id and event["kind"] in {"send", "cache-after", "finalizer-enter", "finalizer-exit"}]
    assert all(event["held"] and event["operation"] == phase8[0][1] for event in protected)
    assert protected[-1]["kind"] == "finalizer-exit"


@pytest.mark.asyncio
async def test_cut2b_markdown_phase8_barrier_retains_typed_terminal_fields(real_job_environment, monkeypatch):
    """CF-44: the full pipeline preserves the phase8 gate's typed refusal until the one held-grant exit."""
    from workflowy_mcp.coherence import admission
    from workflowy_mcp.coherence.errors import CoherenceBarrierActiveError
    import inspect
    import os
    from workflowy_mcp import markdown_generate_pipeline as pipeline
    if os.environ.get("CUT2B_NEUTRALIZE_TYPED_BARRIER"):
        source = inspect.getsource(pipeline.run_markdown_generate_pipeline)
        seam = "    except CoherenceBarrierActiveError:\n        raise\n"
        assert source.count(seam) == 1
        namespace = dict(vars(pipeline))
        exec(compile(source.replace(seam, ""), pipeline.__file__, "exec"), namespace)
        monkeypatch.setattr(pipeline, "run_markdown_generate_pipeline", namespace["run_markdown_generate_pipeline"])
    env = real_job_environment
    gate = admission.require_coherence_mutation_admission
    refusal = CoherenceBarrierActiveError(
        "cut2b phase8 barrier", dirty_generation=17,
        reason_code="journal_publication_failed", transaction_id="cut2b-phase8",
        account_scope_id=env.paths.account_root.name, operation="carto.phase8_refresh",
    )
    observed = []

    async def phase8_gate(*args, **kwargs):
        if kwargs.get("operation") == "carto.phase8_refresh":
            context = runtime.current_carto_job_context()
            event = env.trace("phase8-refusal")
            assert context.job_control.phase == "phase8-refresh"
            assert event["held"]
            observed.append((len(env.probe.sends), (env.repo / "doc.md").read_bytes(), event))
            raise refusal
        return await gate(*args, **kwargs)

    monkeypatch.setattr(admission, "require_coherence_mutation_admission", phase8_gate)
    ack = await launch_real_job(env, "markdown")
    assert ack["success"], ack
    job_id = ack["job_id"]
    await asyncio.wait_for(env.tasks[job_id], 20)
    assert len(env.tasks) == len(observed) == 1, "phase8 acquired a child lifetime or did not reach its real gate"
    assert len(list(env.jobs.glob("*.json"))) == 1
    terminal = json.loads(__import__("pathlib").Path(ack["job_file"]).read_text())
    assert terminal["status"] == "failed"
    assert terminal["error_kind"] == getattr(refusal, "code", "coherence_barrier_active")
    assert terminal["error_extra"]["phase"] == "phase8-refresh", terminal
    for key, value in refusal.refusal_extra().items():
        assert terminal["error_extra"][key] == value
    assert len(env.probe.sends) == observed[0][0], "phase8 sent after its refusing gate"
    assert observed[0][1] != b"# Heading\n\nold body\n", "pipeline did not first write Markdown"
    assert (env.repo / "doc.md").read_bytes() == observed[0][1], "failed phase8 removed written Markdown"
    actual = [event for event in env.events if event["job"] == job_id]
    finalizers = [event for event in actual if event["kind"] in {"finalizer-enter", "finalizer-exit"}]
    assert [event["kind"] for event in finalizers] == ["finalizer-enter", "finalizer-exit"]
    assert all(event["held"] == observed[0][2]["held"] and event["operation"] == observed[0][2]["operation"] for event in finalizers)
    assert actual[-2]["kind"] == "finalizer-exit" and actual[-1]["kind"] == "grant-before-release"
    from workflowy_mcp.mutation_locks import held_mutation_lock_regions
    assert not held_mutation_lock_regions()
    assert not env.probe.violations


@pytest.mark.asyncio
@pytest.mark.parametrize("case_name", tuple(_C05_H_ORACLE))
async def test_reconcile_cpu_driver_preserves_plans_and_send_order(scrubbed_runtime, monkeypatch, case_name):
    """C05: exact captured-H byte oracle for the cases N04 did not change.

    `create_dry_run` still matches byte for byte (a dry run never creates), so it
    stays pinned; the FIVE create-phase cases in _C05_N04_RETIRED keep their old
    digests only as a receipt and are witnessed behaviourally below.
    """
    import hashlib
    from n07_job_harness import reconcile_oracle_cases, run_reconcile_oracle
    observed = await run_reconcile_oracle(reconcile_oracle_cases()[case_name], monkeypatch)
    encoded = json.dumps(observed, ensure_ascii=False, separators=(",", ":"))
    assert hashlib.sha256(encoded.encode()).hexdigest() == _C05_H_ORACLE[case_name], encoded


def test_n04_retirement_split_is_exactly_the_changed_create_cases():
    """The 29 H cases split into 24 byte-preserved and 5 intentionally retired."""
    from n07_job_harness import reconcile_oracle_cases
    cases = set(reconcile_oracle_cases())
    assert len(cases) == 29
    assert len(_C05_H_ORACLE) == 24 and len(_C05_N04_RETIRED) == 5
    assert set(_C05_H_ORACLE).isdisjoint(_C05_N04_RETIRED), "a case cannot be both preserved and retired"
    assert set(_C05_H_ORACLE) | set(_C05_N04_RETIRED) == cases, "a case escaped the split"


def _create_events(observed):
    return [event for event in observed["requests"] if event[0] == "create"]


@pytest.mark.asyncio
async def test_n04_create_is_one_plain_run_and_result_per_node(scrubbed_runtime, monkeypatch):
    """N04 create protocol: one plain run in, one plain result out.

    Before: create_node took a (parent_uuid, payload) scalar pair and handed
    back a bare id string; the engine kept its own synthetic priority and no
    parent conclusion existed.
    After: the driver hands one frozen ReconcileCreateRun(parent_uuid, items,
    placement, predecessor_uuid, parent_created) and the engine consumes
    ReconcileCreateResult.created_ids. The item source paths, the journal pairs
    and the ids adopted into the source are the witness that no scalar pair or
    invented parent order survived.
    """
    from n07_job_harness import reconcile_oracle_cases, run_reconcile_oracle
    observed = await run_reconcile_oracle(
        reconcile_oracle_cases()["create_ids_and_synthetic_priorities"], monkeypatch)
    runs = _create_events(observed)
    assert len(runs) == 2, runs
    assert all(len(event[1]) == 1 for event in runs), "create takes exactly one run argument"
    payloads = [event[1][0] for event in runs]
    assert [run["parent_uuid"] for run in payloads] == ["ROOT", "CREATED-1"]
    assert [run["placement"] for run in payloads] == ["top", "bottom"]
    assert [run["predecessor_uuid"] for run in payloads] == [None, None]
    assert [run["parent_created"] for run in payloads] == [False, True]
    assert [run["items"][0]["source_path"] for run in payloads] == [[0], [0, 0]]
    for run in payloads:
        assert set(run["items"][0]) == {
            "name", "note", "data", "completed", "_position_hint", "source_path"}
    nodes = observed["source_after"]["nodes"]
    assert nodes[0]["id"] == "CREATED-1" and nodes[0]["children"][0]["id"] == "CREATED-2"
    assert nodes[1]["id"] == "A"
    entries = [(entry["source_path"], entry["status"], entry["node_id"])
               for entry in observed["journal"] if entry["op"] == "create"]
    assert entries == [([0], "started", None), ([0], "completed", "CREATED-1"),
                       ([0, 0], "started", None), ([0, 0], "completed", "CREATED-2")]
    assert observed["error"] is None and observed["plan"] is None


@pytest.mark.asyncio
async def test_n04_top_placement_models_the_physical_head_without_reorder(scrubbed_runtime, monkeypatch):
    """N04 head effect: a top-placed create is modelled where it physically landed.

    Before: every create was assumed to land at the bottom of the parent, so a
    node belonging at the head was reordered back with two TOP moves.
    After: the run asks for 'top' and the engine adopts the reported physical
    placement, so the desired order already matches and no move is emitted.
    """
    from n07_job_harness import reconcile_oracle_cases, run_reconcile_oracle
    observed = await run_reconcile_oracle(
        reconcile_oracle_cases()["create_ids_and_synthetic_priorities"], monkeypatch)
    runs = _create_events(observed)
    assert runs[0][1][0]["placement"] == "top"
    moves = [event for event in observed["requests"] if event[0] == "move"]
    assert moves == [], f"a head-placed create was modelled bottom and reordered: {moves}"


@pytest.mark.asyncio
async def test_n04_direct_siblings_conclude_before_descendants(scrubbed_runtime, monkeypatch):
    """N04 order: direct siblings and their parent transaction finish first.

    Before: ensure_created descended into a new node's children inside the
    sibling loop, so a child's create was interleaved between its parent's
    siblings and no parent conclusion existed.
    After: the whole sibling row (existing siblings included) is visited first,
    the parent conclusion is emitted, and only then are children descended —
    visible as per-node cancellation probes and the parent's normal conclusion
    before the child's create and its own conclusion.
    """
    from n07_job_harness import reconcile_oracle_cases, run_reconcile_oracle
    observed = await run_reconcile_oracle(
        reconcile_oracle_cases()["create_ids_and_synthetic_priorities"], monkeypatch)
    kinds = [event[0] for event in observed["requests"]]
    conclusions = [index for index, kind in enumerate(kinds) if kind == "create_parent_done"]
    assert conclusions, "a create phase with new nodes emitted no normal parent conclusion"
    first_create = kinds.index("create")
    assert kinds[first_create:conclusions[-1] + 1] == [
        "create", "create_parent_done", "cancel_check", "create", "create_parent_done"], kinds
    # Head creates execute after the existing sibling probes; every direct
    # sibling still gets its original cancellation probe before conclusion.
    assert kinds[:conclusions[0]].count("cancel_check") == 3, kinds
    parents = [event[1][0] for event in observed["requests"] if event[0] == "create_parent_done"]
    assert parents == ["ROOT", "CREATED-1"], parents


@pytest.mark.asyncio
async def test_n04_dry_run_plans_the_create_without_calling_create(scrubbed_runtime, monkeypatch):
    """N04 dry run: the plan is unchanged, and nothing is created or concluded.

    The dry-run case is still byte-pinned in _C05_H_ORACLE (nothing about a dry
    run changed). This control keeps that explicit now that a real create is a
    run/result protocol: the simulated DRYRUN-CREATE id must come from the plan,
    the create callback must stay uncalled and no parent may be concluded.
    """
    from n07_job_harness import reconcile_oracle_cases, run_reconcile_oracle
    observed = await run_reconcile_oracle(reconcile_oracle_cases()["create_dry_run"], monkeypatch)
    assert _create_events(observed) == []
    assert not [event for event in observed["requests"] if event[0] == "create_parent_done"]
    plan = observed["plan"]
    assert plan["counts"]["creates"] == 1
    assert plan["creates"] == [{"id": "DRYRUN-CREATE-1", "name": "new top",
                                "parent": "ROOT", "source_path": [0]}]
    assert observed["source_after"]["nodes"][0]["id"] == "DRYRUN-CREATE-1"
    assert observed["journal"] == [] and observed["error"] is None


@pytest.mark.asyncio
async def test_n04_create_failure_surfaces_before_any_parent_conclusion(scrubbed_runtime, monkeypatch):
    """N04 failure arm: a failed run raises at its original create site.

    Before: the failure was raised out of a (parent,payload) create.
    After: it is raised out of the plain run, the completed journal entry and
    the parent conclusion never appear, and the annotated source keeps no id.
    """
    from n07_job_harness import reconcile_oracle_cases, run_reconcile_oracle
    observed = await run_reconcile_oracle(reconcile_oracle_cases()["create_exception"], monkeypatch)
    runs = _create_events(observed)
    assert len(runs) == 1 and runs[0][1][0]["parent_uuid"] == "ROOT"
    assert observed["error"] == ["ValueError", "fixture create failed"]
    assert not [event for event in observed["requests"] if event[0] == "create_parent_done"]
    assert not [event for event in observed["requests"] if event[0] == "move"]
    assert [(entry["op"], entry["status"]) for entry in observed["journal"]] == [("create", "started")]
    assert observed["source_after"]["nodes"][0]["id"] is None


@pytest.mark.asyncio
async def test_n04_clone_strips_ids_into_one_plain_top_run(scrubbed_runtime, monkeypatch):
    """N04 clone arm: stripped ids become one plain top run, never a scalar pair.

    Before: clone mode turned the stripped node into a (ROOT, payload) create.
    After: one run with no item id, an explicit top placement (the node is the
    whole new leading segment), no delete and the same generic journal pair.
    """
    from n07_job_harness import reconcile_oracle_cases, run_reconcile_oracle
    observed = await run_reconcile_oracle(reconcile_oracle_cases()["clone"], monkeypatch)
    runs = _create_events(observed)
    assert len(runs) == 1
    run = runs[0][1][0]
    assert (run["parent_uuid"], run["predecessor_uuid"], run["parent_created"]) == ("ROOT", None, False)
    # The placement decision itself is witnessed by the head-effect arm; this
    # arm owns the run protocol: one plain run, no leftover id, no delete.
    assert run["placement"] in {"top", "bottom"}
    assert "id" not in run["items"][0]
    assert observed["source_after"]["nodes"][0]["id"] == "CREATED-1"
    assert not [event for event in observed["requests"] if event[0] in {"move", "delete"}]
    assert [(entry["op"], entry["status"]) for entry in observed["journal"]] == [
        ("create", "started"), ("create", "completed")]


@pytest.mark.asyncio
async def test_n04_after_placement_carries_its_predecessor_into_a_v1_boundary(scrubbed_runtime, monkeypatch):
    """N04 after placement: eligible sibling inserts name their predecessor.

    Before: no create request could express a position at all.
    After: an ordinary new node between two existing siblings is planned as one
    'after' run naming its predecessor. The bare-v1 fake cannot insert after, so
    it reports the physical bottom and the engine must reorder the parent — the
    engine never claims an after insert succeeded when the boundary did not.
    """
    from n07_job_harness import run_reconcile_oracle
    case = {
        "source": {"export_root_id": "ROOT", "nodes": [
            {"id": "A", "name": "a", "children_status": "complete", "children": []},
            {"id": None, "name": "inserted", "children_status": "complete", "children": []},
            {"id": "B", "name": "b", "children_status": "complete", "children": []},
            {"id": "C", "name": "c", "children_status": "complete", "children": []}],
            "original_ids_seen": ["A", "B", "C"]},
        "target": [{"id": "A", "name": "a", "note": "", "parent_id": "ROOT", "priority": 100},
                   {"id": "B", "name": "b", "note": "", "parent_id": "ROOT", "priority": 200},
                   {"id": "C", "name": "c", "note": "", "parent_id": "ROOT", "priority": 300}],
        "options": {},
    }
    observed = await run_reconcile_oracle(case, monkeypatch)
    runs = _create_events(observed)
    assert len(runs) == 1
    run = runs[0][1][0]
    assert (run["placement"], run["predecessor_uuid"], run["parent_created"]) == ("after", "A", False)
    assert run["items"][0]["source_path"] == [1]
    moved = [event[1][0] for event in observed["requests"] if event[0] == "move"]
    assert len(moved) == 4 and "CREATED-1" in moved, moved
    assert [(entry["op"], entry["status"]) for entry in observed["journal"]] == [
        ("create", "started"), ("create", "completed")]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_name", ["cancel_prefix", "cancel_interval"])
async def test_n04_cancel_inside_create_records_one_plain_run(scrubbed_runtime, monkeypatch, case_name):
    """N04 cancel arm: the pre-cancel write is one plain run and nothing else.

    Before: the recorded pre-cancel write was a (parent,payload) create.
    After: it is one run carrying one item; no further create, no parent
    conclusion, and the original [CANCEL] diagnostic plus journal stop remain.
    """
    from n07_job_harness import reconcile_oracle_cases, run_reconcile_oracle
    observed = await run_reconcile_oracle(reconcile_oracle_cases()[case_name], monkeypatch)
    runs = _create_events(observed)
    assert len(runs) == 1 and runs[0][1][0]["items"][0]["name"] == "one"
    assert not [event for event in observed["requests"] if event[0] == "create_parent_done"]
    assert observed["plan"] is None and observed["error"] is None
    assert any("[CANCEL]" in line and "(create)" in line for line in observed["diagnostics"])
    assert [(entry["op"], entry["status"]) for entry in observed["journal"]] == [
        ("create", "started"), ("create", "completed"), ("cancel", "requested")]


def _n04_create_mutant(monkeypatch, name):
    """Install one NAMED source-copy mutant of the N04 create phase.

    The production files are never edited: each mutant is compiled from the
    current disk source with exactly one decision restored to its old shape.
    """
    from workflowy_mcp.client import carto_reconcile_cpu as cpu
    from workflowy_mcp.client import workflowy_move_reconcile as driver
    if name == "create-run-protocol":
        mutant = _source_copy(cpu, "_reconcile_steps", [(
            "ReconcileRequest('create', (run,), {})",
            "ReconcileRequest('create', (desired_parent, payload), {})",
        )])
        monkeypatch.setattr(driver, "_reconcile_steps", mutant)
        return
    if name == "head-placement-adoption":
        monkeypatch.setattr(cpu, "_adopt_create_order", _source_copy(cpu, "_adopt_create_order", [(
            '            order.insert(0 if placement == "top" else len(order), identifier)',
            '            order.insert(len(order), identifier)',
        )]))
        return
    if name == "sibling-before-descendants":
        mutant = _source_copy(cpu, "_reconcile_steps", [(
            "            else:\n"
            "                (yield from log(f\"      (already exists, skipping create)\"))\n"
            "        if new_indices and not dry_run:\n"
            "            yield ReconcileRequest('create_parent_done', (desired_parent,), {})\n"
            "        # Direct siblings and their parent transaction are complete first.\n"
            "        for idx, n in enumerate(nodes):\n"
            "            current_path = source_path_prefix + [idx]\n"
            "            (yield from ensure_created(n.get('children', []), n.get('id'), current_path))\n",
            "            else:\n"
            "                (yield from log(f\"      (already exists, skipping create)\"))\n"
            "            (yield from ensure_created(n.get('children', []), n.get('id'), current_path))\n",
        )])
        monkeypatch.setattr(driver, "_reconcile_steps", mutant)
        return
    raise AssertionError(f"unknown N04 create mutant: {name}")


@pytest.mark.asyncio
@pytest.mark.parametrize("mutant,witness", [
    ("create-run-protocol", "test_n04_create_is_one_plain_run_and_result_per_node"),
    ("head-placement-adoption", "test_n04_top_placement_models_the_physical_head_without_reorder"),
    ("sibling-before-descendants", "test_n04_direct_siblings_conclude_before_descendants"),
])
async def test_n04_behavioural_witnesses_reject_their_named_source_copy_mutant(
        scrubbed_runtime, monkeypatch, mutant, witness):
    """Each witness must turn red under its own mutant — teeth, not a new golden.

    No replacement hash is computed for the retired cases: the mutant run proves
    the new create protocol, the head effect and the sibling-before-descendants
    order are each independently observed. (The legacy scalar pair cannot even
    bind the run-only callback, so that arm fails before any create is recorded.)
    """
    _n04_create_mutant(monkeypatch, mutant)
    with pytest.raises((AssertionError, TypeError)):
        await globals()[witness](scrubbed_runtime, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_reconcile_decisions_are_empty_context_data_work_even_when_unchanged(scrubbed_runtime, monkeypatch, dry_run):
    """C06 reconcile arm: offload is structural, not a machine-speed assertion."""
    from workflowy_mcp.client import workflowy_move_reconcile as driver
    from workflowy_mcp import mutation_locks
    from workflowy_mcp.coherence import operation_cache
    loop_thread, owner = threading.get_ident(), asyncio.current_task()
    arbitrary = contextvars.ContextVar("n07-reconcile-worker-marker", default="empty")
    marker_token = arbitrary.set("loop")
    cache_token = operation_cache._OPERATION_CACHE_SNAPSHOT.set(object())
    lock_token = mutation_locks.MUTATION_LOCK_STATE.set(object())
    control = runtime.CartoJobControl(task=owner)
    advances, callbacks = [], []
    original = driver._advance_reconcile_steps

    def advance(steps, response=None, error=None):
        assert threading.get_ident() != loop_thread, "reconcile advanced on event loop"
        assert arbitrary.get() == "empty" and runtime.current_carto_job_context() is None
        assert operation_cache.current_operation_cache(required=False) is None
        assert mutation_locks.MUTATION_LOCK_STATE.get() is None
        # Only source/scalar capabilities are in the initial generator frame.
        if steps.gi_frame is not None:
            for name in ("export_nodes", "cancel_callback", "log_weave_entry"):
                assert type(steps.gi_frame.f_locals[name]) is bool, "callback crossed into data worker"
        advances.append(threading.get_ident())
        return original(steps, response, error)

    def check_loop(kind):
        assert threading.get_ident() == loop_thread and asyncio.current_task() is owner
        assert runtime.current_carto_job_context().job_control is control
        assert arbitrary.get() == "loop"
        callbacks.append(kind)

    async def export(parent):
        check_loop("export")
        return {"nodes": [{"id": "A", "parent_id": "ROOT", "priority": 1,
                            "name": "same", "note": ""}]}
    async def forbidden(*args, **kwargs):
        pytest.fail("unchanged reconcile emitted a write")
    def log(message):
        check_loop("log")
    def cancel():
        check_loop("cancel")
        return False

    monkeypatch.setattr(driver, "_advance_reconcile_steps", advance)
    try:
        with runtime.carto_job_scope(job_context(scrubbed_runtime, control)):
            plan = await driver.reconcile_tree(
                {"nodes": [{"id": "A", "name": "same", "note": "", "children": [],
                            "children_status": "complete"}]}, "ROOT",
                forbidden, forbidden, forbidden, forbidden, forbidden, export,
                dry_run=dry_run, skip_delete_bulk_export_wait=True,
                cancel_callback=cancel, cancel_check_interval=1, log_to_file_msg=log,
            )
        assert len(advances) > 10 and callbacks.count("export") == 2
        assert "cancel" in callbacks and "log" in callbacks
        if dry_run:
            assert plan["counts"] == {"creates": 0, "moves": 0, "reorders": 0, "updates": 0, "deletes": 0}
        else:
            assert plan is None
        assert control.cpu_future is None
    finally:
        mutation_locks.MUTATION_LOCK_STATE.reset(lock_token)
        operation_cache._OPERATION_CACHE_SNAPSHOT.reset(cache_token)
        arbitrary.reset(marker_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_journal", [False, True])
async def test_reconcile_path_sink_is_joined_and_journal_failure_stays_best_effort(scrubbed_runtime, monkeypatch, broken_journal):
    from workflowy_mcp.client import workflowy_move_reconcile as driver
    from workflowy_mcp.client.carto_reconcile_cpu import CartoDiagnosticSink
    loop_thread = threading.get_ident()
    debug_path, journal_path = scrubbed_runtime / "reconcile.log", scrubbed_runtime / "journal.json"
    if not broken_journal:
        journal_path.write_text('{"entries": [], "last_run_completed": false}', encoding="utf-8")
    context = job_context(scrubbed_runtime, runtime.CartoJobControl(task=asyncio.current_task()))
    context.diagnostic_sink = CartoDiagnosticSink(debug_path, journal_path)
    appends, writes = [], []
    original = CartoDiagnosticSink.append_batch

    def append(sink, events):
        assert threading.get_ident() != loop_thread, "diagnostic disk work ran on loop"
        assert runtime.current_carto_job_context() is None
        appends.extend(event[0] for event in events)
        return original(sink, events)
    async def export(parent):
        assert threading.get_ident() == loop_thread
        return {"nodes": []}
    async def create(run):
        from workflowy_mcp.client.carto_reconcile_cpu import ReconcileCreateResult
        assert threading.get_ident() == loop_thread
        writes.append(run)
        return ReconcileCreateResult(("created",), ("bottom",))
    async def forbidden(*args, **kwargs):
        pytest.fail("unexpected non-create write")
    def forbidden_diagnostic(*args):
        pytest.fail("job invoked an arbitrary legacy diagnostic callback")

    monkeypatch.setattr(CartoDiagnosticSink, "append_batch", append)
    with runtime.carto_job_scope(context):
        await driver.reconcile_tree(
            {"nodes": [{"name": "created", "note": "", "children": []}]}, "ROOT",
            forbidden, create, forbidden, forbidden, forbidden, export,
            log_weave_entry=forbidden_diagnostic, log_to_file_msg=forbidden_diagnostic,
            skip_delete_bulk_export_wait=True,
        )
    assert len(writes) == 1 and appends.count("journal") == 2
    assert "[SUMMARY]" in debug_path.read_text(encoding="utf-8")
    assert context.job_control.cpu_failure is None and context.job_control.cpu_future is None
    if broken_journal:
        assert "Failed to append weave journal entry: FileNotFoundError" in debug_path.read_text(encoding="utf-8")
    else:
        entries = json.loads(journal_path.read_text(encoding="utf-8"))["entries"]
        assert [(entry["op"], entry["status"]) for entry in entries] == [("create", "started"), ("create", "completed")]


@pytest.mark.asyncio
async def test_reconcile_nonjob_journal_callback_failure_is_thrown_back_at_original_site(scrubbed_runtime):
    from workflowy_mcp.client.workflowy_move_reconcile import reconcile_tree
    owner, loop_thread = asyncio.current_task(), threading.get_ident()
    logs, writes, journal_attempts = [], [], []
    async def export(parent):
        return {"nodes": []}
    async def create(run):
        from workflowy_mcp.client.carto_reconcile_cpu import ReconcileCreateResult
        writes.append(run)
        return ReconcileCreateResult(("created",), ("bottom",))
    async def forbidden(*args, **kwargs):
        pytest.fail("unexpected non-create write")
    def journal(entry):
        assert asyncio.current_task() is owner and threading.get_ident() == loop_thread
        journal_attempts.append(entry["status"])
        raise ValueError("fixture journal unavailable")
    await reconcile_tree(
        {"nodes": [{"name": "created", "children": []}]}, "ROOT",
        forbidden, create, forbidden, forbidden, forbidden, export,
        log_weave_entry=journal, log_to_file_msg=logs.append, skip_delete_bulk_export_wait=True,
    )
    assert len(writes) == 1 and journal_attempts == ["started", "completed"]
    assert sum("Failed to append weave journal entry: ValueError: fixture journal unavailable" in message for message in logs) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [None, "file", "missing"])
async def test_cached_export_projection_is_off_loop_and_keeps_pinned_aliases(task_core_environment, monkeypatch, selected):
    from n05_region_harness import node_uuid
    from workflowy_mcp.client import api_client_etch as etch
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp import mutation_locks
    env = task_core_environment
    loop_thread = threading.get_ident()
    arbitrary = contextvars.ContextVar("n07-export-marker", default="empty")
    token = arbitrary.set("caller")
    observations = []
    original = etch._project_cached_export

    def project(data, node_id):
        assert threading.get_ident() != loop_thread, "cached projection ran on loop"
        assert current_operation_cache(required=False) is None
        assert runtime.current_carto_job_context() is None and arbitrary.get() == "empty"
        assert mutation_locks.MUTATION_LOCK_STATE.get() is None
        observations.append((data, node_id))
        return original(data, node_id)

    monkeypatch.setattr(etch, "_project_cached_export", project)
    try:
        async with cache_operation_boundary(env.client, "n07-export-projection") as snapshot:
            before = snapshot.view
            selected_id = node_uuid(selected) if selected else None
            result = await etch.export_nodes_impl(env.client, node_id=selected_id)
            assert observations == [(before.nodes_export, selected_id)]
            assert current_operation_cache().view is before
            assert env.client._cache_view_publisher.capture() is before
            if selected is None:
                expected = tuple(before.nodes_by_id)
            elif selected == "file":
                expected = tuple(node_uuid(name) for name in ("file", "symbol", "sibling"))
            else:
                expected = ()
            assert {row["id"] for row in result["nodes"]} == set(expected)
            assert all(row is before.nodes_by_id[row["id"]] for row in result["nodes"])
            assert result["_total_fetched_from_api"] == len(before.nodes_by_id)
        assert arbitrary.get() == "caller"
    finally:
        arbitrary.reset(token)


@pytest.mark.asyncio
async def test_weave_log_scope_interleaves_paths_and_resets_after_failure(scrubbed_runtime):
    from workflowy_mcp.client import api_client_etch as etch, api_client as compatibility
    assert isinstance(etch._current_weave_context, contextvars.ContextVar)
    assert compatibility._current_weave_context is etch._current_weave_context
    assert compatibility.weave_log_scope is etch.weave_log_scope
    assert etch._current_weave_context.get() is None
    first_entered, release_first = asyncio.Event(), asyncio.Event()
    paths = [scrubbed_runtime / name / "source.json" for name in ("first", "second")]
    for path in paths:
        path.parent.mkdir()
        path.write_text("{}", encoding="utf-8")

    async def first():
        assert etch._current_weave_context.get() is None
        with etch.weave_log_scope(str(paths[0])):
            etch._log_to_file_helper("first-before")
            first_entered.set()
            await release_first.wait()
            assert etch._current_weave_context.get() == str(paths[0]), "another task stole the weave path"
            etch._log_to_file_helper("first-after")
        assert etch._current_weave_context.get() is None

    task = asyncio.create_task(first())
    await first_entered.wait()
    try:
        with pytest.raises(ValueError, match="scope failure"):
            with etch.weave_log_scope(str(paths[1])):
                etch._log_to_file_helper("second-before")
                with etch.weave_log_scope(str(paths[0])):
                    assert etch._current_weave_context.get() == str(paths[0])
                assert etch._current_weave_context.get() == str(paths[1])
                raise ValueError("scope failure")
        assert etch._current_weave_context.get() is None
    finally:
        release_first.set()
        await task
    first_log = (paths[0].parent / "reconcile_debug.log").read_text(encoding="utf-8")
    second_log = (paths[1].parent / "reconcile_debug.log").read_text(encoding="utf-8")
    assert "first-before" in first_log and "first-after" in first_log and "second-before" not in first_log
    assert "second-before" in second_log and "first-" not in second_log


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["python", "markdown", "virtual", "unsupported", "helper-error", "noop"])
async def test_disk_beacon_phase_uses_only_snapshot_data_off_loop(task_core_environment, monkeypatch, mode):
    """C06 beacon arm; current task/publisher/grant never enter the data worker."""
    from types import SimpleNamespace
    from n05_region_harness import node_uuid
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp import mutation_locks
    from workflowy_mcp.models import NetworkError
    env = task_core_environment
    loop_thread, owner = threading.get_ident(), asyncio.current_task()
    path = env.path / "beacon-output.txt"
    phase_calls, helper_calls = [], []
    original = nexus._apply_beacon_from_node_data
    context = job_context(env.path, runtime.CartoJobControl(task=owner))

    def phase(nodes_by_id, node_id, name, note, receipt_holder=None):
        assert threading.get_ident() != loop_thread, "beacon phase ran on loop"
        assert current_operation_cache(required=False) is None
        assert mutation_locks.MUTATION_LOCK_STATE.get() is None
        assert runtime.current_carto_job_context() is None
        phase_calls.append(nodes_by_id)
        return original(nodes_by_id, node_id, name, note, receipt_holder)

    def disk_helper(source_path, name, note, **kwargs):
        assert threading.get_ident() != loop_thread
        helper_calls.append((source_path, name, note, kwargs))
        if mode == "helper-error":
            raise ValueError("fixture disk failure")
        path.write_text(name + "\n" + note, encoding="utf-8")
        return {"operation": "noop" if mode == "noop" else "update", "language": mode}

    def target(nodes_by_id, node_id):
        assert mode != "virtual", "virtual group reached disk target resolution"
        language = "markdown" if mode == "markdown" else "python"
        cartographer = SimpleNamespace()
        if mode != "unsupported":
            setattr(cartographer, nexus._BEACON_LANGUAGE_HELPER_NAME[language], disk_helper)
        return {"success": True, "file_node": nodes_by_id[node_uuid("file")],
                "source_path": str(path), "ext": ".md" if mode == "markdown" else ".py",
                "language": language, "cartographer": cartographer}

    monkeypatch.setattr(nexus, "_apply_beacon_from_node_data", phase)
    monkeypatch.setattr(nexus, "_resolve_beacon_target_from_cache", target)
    note = "CARTOGRAPHER_VIRTUAL_GROUP: python" if mode == "virtual" else "BEACON id: n07-fixture"
    with runtime.carto_job_scope(context):
        async with cache_operation_boundary(env.client, "n07-beacon-disk") as snapshot:
            before = snapshot.view
            if mode == "helper-error":
                with pytest.raises(NetworkError, match="helper failed.*fixture disk failure"):
                    await nexus.WorkFlowyClientNexus.update_beacon_from_node(env.client, node_uuid("symbol"), "fixture #tag", note)
            else:
                result = await nexus.WorkFlowyClientNexus.update_beacon_from_node(env.client, node_uuid("symbol"), "fixture #tag", note)
                if mode == "virtual":
                    assert result["success"] and result["reason"] == "virtual_group_noop"
                elif mode == "unsupported":
                    assert result["success"] is False and result["reason"] == "unsupported_extension"
                else:
                    assert result["success"] is (mode != "noop")
                    assert path.read_text(encoding="utf-8") == "fixture #tag\n" + note
            assert phase_calls == [before.nodes_by_id] and phase_calls[0] is before.nodes_by_id
            assert env.client._cache_view_publisher.capture() is before
            assert current_operation_cache().view is before
    if mode not in {"virtual", "unsupported"}:
        assert len(helper_calls) == 1
        assert helper_calls[0][3] == ({"occurrence": None} if mode == "markdown" else {})
    else:
        assert helper_calls == [] and not path.exists()
    assert context.job_control.cpu_future is None


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["py", "md"])
@pytest.mark.parametrize("dry_run,claimed", [(True, None), (True, "fixture-claim"), (False, None), (False, "fixture-claim")])
async def test_source_preparation_keeps_hygiene_gates_order_and_owned_parse(scrubbed_runtime, monkeypatch, extension, dry_run, claimed):
    """Source component gates; the separate FILE integration arm pins its caller."""
    from j_counterfactual import install_j_counterfactual
    install_j_counterfactual(monkeypatch)
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp import beacon_refresh_support as support
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    from workflowy_mcp import mutation_locks
    from test_carto_dry_run_byte_purity import _HYGIENE_FIXTURE
    import nexus_map_codebase as cartographer
    path = scrubbed_runtime / ("source." + extension)
    before = _HYGIENE_FIXTURE if extension == "py" else b"# Heading\n\ncontrol:\x07 text\n"
    path.write_bytes(before)
    loop_thread = threading.get_ident()
    calls = []
    original_import = support._import_cartographer_module

    def check_worker():
        assert threading.get_ident() != loop_thread, "source preparation ran on loop"
        assert runtime.current_carto_job_context() is None
        assert current_operation_cache(required=False) is None
        assert mutation_locks.MUTATION_LOCK_STATE.get() is None

    def imported():
        check_worker()
        calls.append("import")
        return original_import()

    monkeypatch.setattr(support, "_import_cartographer_module", imported)
    for name, label in (("repair_markdown_c0_controls_on_disk", "c0"),
                        ("normalize_beacon_comment_field_aliases_in_file", "alias"),
                        ("normalize_duplicate_ast_beacons_in_file", "duplicates"),
                        ("map_source_snapshot", "parse")):
        original = getattr(cartographer, name)
        def traced(*args, _original=original, _label=label, **kwargs):
            check_worker()
            calls.append(_label)
            if _label == "c0":
                assert kwargs["claim_id"] == claimed
                assert kwargs.get("cancel_check") is None, "live cancellation probe crossed into disk worker"
            return _original(*args, **kwargs)
        monkeypatch.setattr(cartographer, name, traced)
    control = runtime.CartoJobControl(task=asyncio.current_task())
    with runtime.carto_job_scope(job_context(scrubbed_runtime, control)):
        result = await runtime.run_carto_cpu("file:source", nexus._prepare_carto_source_file,
                                             str(path), None, dry_run=dry_run, claimed=claimed)
    expected = ["import"]
    if not dry_run and claimed:
        expected += (["c0"] if extension == "md" else []) + ["alias", "duplicates"]
    expected += ["parse"]
    assert calls == expected
    assert result["new_children"] and result["new_children"] == result["file_map"]["children"]
    assert result["new_children"] is not result["file_map"]["children"]
    assert result["new_children"][0] is not result["file_map"]["children"][0]
    assert isinstance(result["messages"], list) and all(isinstance(message, str) for message in result["messages"])
    if dry_run or not claimed:
        assert path.read_bytes() == before and result["messages"] == []
    elif extension == "md":
        assert b"\x07" not in path.read_bytes() and any("Claimed Markdown C0 repair" in message for message in result["messages"])
    else:
        assert b"comments=legacy alias" not in path.read_bytes()
    assert control.cpu_future is None and control.cpu_failure is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", ["normalize_beacon_comment_field_aliases_in_file", "normalize_duplicate_ast_beacons_in_file"])
async def test_source_preparation_keeps_known_nonfatal_hygiene_diagnostics(scrubbed_runtime, monkeypatch, failed):
    import nexus_map_codebase as cartographer
    from workflowy_mcp.client import api_client_nexus as nexus
    path = scrubbed_runtime / "source.py"
    path.write_text("def function():\n    return 1\n", encoding="utf-8")
    def unavailable(*args):
        raise ValueError("fixture hygiene unavailable")
    monkeypatch.setattr(cartographer, failed, unavailable)
    control = runtime.CartoJobControl(task=asyncio.current_task())
    with runtime.carto_job_scope(job_context(scrubbed_runtime, control)):
        result = await runtime.run_carto_cpu("file:source", nexus._prepare_carto_source_file,
                                             str(path), None, dry_run=False, claimed="fixture-claim")
    assert result["new_children"]
    assert len(result["messages"]) == 1 and "normalization failed" in result["messages"][0]
    assert "fixture hygiene unavailable" in result["messages"][0]
    assert control.cpu_failure is None, "known nonfatal hygiene warning became a job failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("exception_type", [RuntimeError, SystemExit])
async def test_source_preparation_parse_failure_is_latched_before_caller_catches(scrubbed_runtime, monkeypatch, exception_type):
    from j_counterfactual import install_j_counterfactual
    install_j_counterfactual(monkeypatch)
    import nexus_map_codebase as cartographer
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.models import NetworkError
    path = scrubbed_runtime / "source.py"
    path.write_text("def function():\n    return 1\n", encoding="utf-8")
    def failed(*args, **kwargs):
        raise exception_type("fixture parser failure")
    monkeypatch.setattr(cartographer, "map_source_snapshot", failed)
    control = runtime.CartoJobControl(task=asyncio.current_task())
    with runtime.carto_job_scope(job_context(scrubbed_runtime, control)):
        with pytest.raises(exception_type, match="fixture parser failure"):
            await runtime.run_carto_cpu("file:source", nexus._prepare_carto_source_file,
                                        str(path), None, dry_run=True, claimed=None)
    assert control.cpu_failure is not None and control.cpu_failure[0] == "file:source"
    assert "fixture parser failure" in str(control.cpu_failure[1]) and control.cpu_future is None


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("language", ["python", "markdown"])
async def test_file_pipeline_runs_real_source_and_plan_under_one_loop_hold(task_core_environment, monkeypatch, dry_run, language):
    """C06 FILE arm: real parse/seed; fake loop sends, real boundary and grant."""
    from types import SimpleNamespace
    from coherence_harness import make_boundary_capable
    from n05_region_harness import node_uuid
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.client import workflowy_move_reconcile as driver
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.mutation_locks import mutation_region_locks, held_mutation_lock_regions, MUTATION_LOCK_STATE
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    env = task_core_environment
    path = env.path / ("source.py" if language == "python" else "source.md")
    path.write_text("def function():\n    return 1\n" if language == "python" else "# Heading\n\nbody\n", encoding="utf-8")
    file_id = node_uuid("file")
    client = make_boundary_capable(SimpleNamespace(), nodes=[{
        "id": file_id, "name": path.name, "note": f"Path: {path}\nLINE COUNT: 999\n\nkeep suffix\n",
    }])
    owner, loop_thread = asyncio.current_task(), threading.get_ident()
    marker = contextvars.ContextVar("file-worker-marker", default="empty")
    token = marker.set("loop")
    phases, sends = [], []
    originals = {name: getattr(nexus, name) for name in (
        "_prepare_carto_file_facts", "_prepare_carto_file_tree", "_prepare_carto_source_file",
        "_advance_carto_file_plan", "_log_carto_source_messages", "_write_carto_file_artifact",
        "_carto_list_node_values",
    )}
    map_seen = []
    for name, original in originals.items():
        def traced(*args, _name=name, _original=original, **kwargs):
            assert threading.get_ident() != loop_thread, f"{_name} ran on event loop"
            assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
            assert MUTATION_LOCK_STATE.get() is None and marker.get() == "empty"
            if _name == "_prepare_carto_file_facts":
                map_seen.append(args[2])
            phases.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(nexus, name, traced)
    def forbidden_index(*args):
        raise AssertionError("FILE rebuilt the whole cache despite a same-authority pin")
    monkeypatch.setattr(nexus, "_index_carto_export_nodes", forbidden_index)
    monkeypatch.setattr(nexus, "get_cartographer_file_refresh_dir", lambda: env.path / "artifacts")
    async def update(node_id, request):
        assert asyncio.current_task() is owner and threading.get_ident() == loop_thread
        assert held_mutation_lock_regions() and current_operation_cache().operation_id == operation_id
        sends.append((node_id, request.model_dump()))
    async def reconcile(**kwargs):
        assert asyncio.current_task() is owner and held_mutation_lock_regions()
        assert current_operation_cache().operation_id == operation_id
        assert kwargs["source_json"]["nodes"], "real parse/identity plan disappeared"
        assert kwargs["source_json"]["original_ids_seen"] == [file_id]
        assert kwargs["source_json"]["export_root_id"] == file_id
        assert kwargs["source_json"]["export_root_children_status"] == "complete"
        assert "carto_identity_seed_report" in kwargs["source_json"]
        assert kwargs["dry_run"] is dry_run
        values = await kwargs["list_nodes"](file_id)
        assert values == [{"id": "literal-child", "name": "value", "data": {"nested": [1, 2]}}]
        sends.append(("reconcile", kwargs["source_json"]))
        return {"fixture": "reconcile"}
    class ListedValue:
        def model_dump(self):
            assert threading.get_ident() != loop_thread, "FILE list values serialized on loop"
            assert current_operation_cache(required=False) is None and runtime.current_carto_job_context() is None
            assert marker.get() == "empty"
            return {"id": "literal-child", "name": "value", "data": {"nested": [1, 2]}}
    async def listing(request):
        assert asyncio.current_task() is owner and threading.get_ident() == loop_thread
        assert held_mutation_lock_regions() and current_operation_cache().operation_id == operation_id
        return [ListedValue()], None
    client.list_nodes = listing
    client.update_node = update
    client._mark_nodes_export_dirty = lambda ids: None
    monkeypatch.setattr(driver, "reconcile_tree", reconcile)
    control = runtime.CartoJobControl(task=owner)
    context = job_context(env.path, control)
    try:
        with runtime.carto_job_scope(context):
            async with cache_operation_boundary(client, "n07-real-file-plan") as snapshot:
                operation_id = snapshot.operation_id
                before = snapshot.view
                async with mutation_region_locks(client=client, operation_label="n07-file-plan", region_factory=lambda proof: (
                    build_region_subtree(file_id, cache_by_id=proof.nodes_by_id),
                ), hold_mode="long_lived", expected_duration_seconds=None):
                    result = await nexus.WorkFlowyClientNexus.refresh_file_node_beacons(client, file_id, dry_run=dry_run)
                    assert result["success"] and result["reconcile_result"] == {"fixture": "reconcile"}
                    assert map_seen == [before.nodes_by_id] and map_seen[0] is before.nodes_by_id
                    assert current_operation_cache().view is before
                assert not held_mutation_lock_regions()
    finally:
        marker.reset(token)
    assert set(phases) == set(originals)
    assert phases.index("_prepare_carto_file_tree") < phases.index("_prepare_carto_source_file") < phases.index("_advance_carto_file_plan")
    assert [item[0] for item in sends] == (["reconcile"] if dry_run else [file_id, "reconcile"])
    if not dry_run:
        assert sends[0][1]["note"] == f"Path: {path}\nLINE COUNT: {2 if language == 'python' else 3}\n\nkeep suffix\n"
    assert control.cpu_future is None and control.cpu_failure is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["create", "update", "diagnostic-error", "seed-error", "header-error", "dry", "valid"])
async def test_file_plan_preserves_diagnostic_header_seed_failure_order(scrubbed_runtime, monkeypatch, case):
    """Failed scopes diagnose without seeding; usable maps retain failure order."""
    from j_counterfactual import install_j_counterfactual
    install_j_counterfactual(monkeypatch)
    from types import SimpleNamespace
    import nexus_map_codebase as cartographer
    from workflowy_mcp.client import api_client_nexus as nexus, workflowy_move_reconcile as driver
    from workflowy_mcp.models import NetworkError
    path = scrubbed_runtime / "diagnostic.py"
    path.write_text("fixture\n", encoding="utf-8")
    rows = [{"id": "FILE", "name": path.name, "note": f"Path: {path}\nLINE COUNT: 999\n"}]
    if case == "update":
        rows.append({"id": "DIAG", "parentId": "FOLDER", "name": "🚨 Parse error: old",
                     "no": f"File: {path}\n---\nold"})
    events = []
    owner, loop_thread = asyncio.current_task(), threading.get_ident()
    def parse(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        children = [{"name": "⚠️ User heading", "note": "ordinary child"}]
        if case not in {"seed-error", "header-error", "valid"}:
            children.insert(0, {"name": "⚠️ Parse Error", "note": "syntax diagnostic"})
        return {"note": "missing" if case == "header-error" else "LINE COUNT: 1",
                "children": children}
    def seed(source, target):
        assert threading.get_ident() != loop_thread
        events.append("seed")
        assert [child["name"] for child in source["children"]] == ["⚠️ User heading"]
        if case == "seed-error":
            raise ValueError("fixture seeding failed")
        source["_carto_identity_seed_report"] = {"preserve_ids": ["KEPT"], "ambiguous_count": 1,
                                                  "preserve_count": 1, "seeded_count": 0}
    async def export(*args, **kwargs):
        return {"nodes": rows}
    async def update(node_id, request):
        assert asyncio.current_task() is owner and threading.get_ident() == loop_thread
        events.append("diagnostic" if node_id == "DIAG" else "header")
    async def create(request, **kwargs):
        assert asyncio.current_task() is owner and threading.get_ident() == loop_thread
        assert request.parent_id == "FOLDER" and request.position == "top"
        assert request.name == "🚨 Parse error: diagnostic.py"
        assert request.note == f"File: {path}\n---\nsyntax diagnostic"
        events.append("diagnostic")
        if case == "diagnostic-error":
            raise ValueError("fixture diagnostic write failed")
    async def reconcile(**kwargs):
        assert asyncio.current_task() is owner
        assert kwargs["source_json"]["explicitly_preserved_ids"] == ["KEPT"]
        assert kwargs["source_json"]["carto_identity_seed_report"]["preserve_count"] == 1
        events.append("reconcile")
    monkeypatch.setattr(cartographer, "map_source_snapshot", parse)
    monkeypatch.setattr(cartographer, "reconcile_trees_cartographer", seed)
    monkeypatch.setattr(nexus, "export_nodes_impl", export)
    monkeypatch.setattr(nexus, "get_cartographer_file_refresh_dir", lambda: scrubbed_runtime / "artifacts")
    monkeypatch.setattr(driver, "reconcile_tree", reconcile)
    client = SimpleNamespace(update_node=update, create_node=create, _mark_nodes_export_dirty=lambda ids: None)
    context = job_context(scrubbed_runtime, runtime.CartoJobControl(task=owner))
    with runtime.carto_job_scope(context):
        if case in {"seed-error", "header-error"}:
            with pytest.raises(NetworkError) as raised:
                await nexus.WorkFlowyClientNexus.refresh_file_node_beacons(client, "FILE", diagnostic_parent_id="FOLDER")
            assert ("fixture seeding failed" if case == "seed-error" else "no usable LINE COUNT") in str(raised.value.details)
            assert events == (["seed"] if case == "seed-error" else [])
        else:
            result = await nexus.WorkFlowyClientNexus.refresh_file_node_beacons(
                client, "FILE", diagnostic_parent_id="FOLDER", dry_run=case == "dry",
            )
            if case == "valid":
                assert result["success"] and result["warning_count"] == 1
                assert events == ["seed", "header", "reconcile"]
            else:
                assert result["success"] is False and result["reconcile_children"] is None
                assert events == ([] if case == "dry" else ["diagnostic"])
            assert context.job_control.cpu_failure is None, "nonfatal diagnostic write promoted to planner failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_first", [False, True])
async def test_file_artifacts_and_progress_are_joined_task_local_data(task_core_environment, monkeypatch, fail_first):
    """Real FILE/driver/sink over two disjoint holds and one shared fake client."""
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from coherence_harness import make_boundary_capable
    from n05_region_harness import node_uuid
    from workflowy_mcp.client import api_client_nexus as nexus, api_client_etch as etch
    from workflowy_mcp.client import workflowy_move_reconcile as driver
    from workflowy_mcp.client.carto_reconcile_cpu import CartoDiagnosticSink
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.mutation_locks import mutation_region_locks, MUTATION_LOCK_STATE, held_mutation_lock_regions
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    from workflowy_mcp.models import NetworkError
    env, loop_thread = task_core_environment, threading.get_ident()
    names = ("first", "second")
    paths = {name: env.path / f"{name}.py" for name in names}
    rows = {}
    contexts, owners, results, operations = {}, {}, {}, {}
    for name, path in paths.items():
        path.write_text(f"def {name}():\n    return 1\n", encoding="utf-8")
        rows[node_uuid(name)] = {"id": node_uuid(name), "name": path.name,
                                 "note": f"Path: {path}\nLINE COUNT: 999\n"}
        directory = env.path / name
        directory.mkdir()
        (directory / "job.json").write_text(json.dumps({"job_id": name, "job_type": "CARTO_REFRESH",
            "status": "running", "progress": {"nodes_created": 0, "nodes_updated": 0}}))
    client = make_boundary_capable(SimpleNamespace(), nodes=rows.values())
    original_artifact, original_sink, original_driver = nexus._write_carto_file_artifact, CartoDiagnosticSink._append_batch, driver.reconcile_tree
    entered = {name: threading.Event() for name in names}
    released = threading.Event()
    artifact_kinds, sink_threads = [], []
    marker = contextvars.ContextVar("file-artifact-marker", default="empty")
    def data_context():
        assert threading.get_ident() != loop_thread, "FILE artifact serialized on event loop"
        assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
        assert MUTATION_LOCK_STATE.get() is None and marker.get() == "empty"
    def artifact(kind, values):
        data_context()
        assert not any(callable(value) for value in values.values())
        artifact_kinds.append(kind)
        if kind == "source":
            name = Path(values["source_path"]).stem
            entered[name].set()
            assert released.wait(10), "test failed to release joined source writer"
        return original_artifact(kind, values)
    def sink(self, events):
        data_context()
        sink_threads.append(str(self.journal_path))
        return original_sink(self, events)
    monkeypatch.setattr(nexus, "_write_carto_file_artifact", artifact)
    monkeypatch.setattr(CartoDiagnosticSink, "_append_batch", sink)
    wrong = env.path / "wrong-environment-job.json"
    wrong.write_text("unchanged sentinel")
    monkeypatch.setenv("CARTO_JOB_FILE", str(wrong))
    monkeypatch.setenv("CARTO_JOB_DIR", str(env.path / "wrong-environment-dir"))
    def loop_context():
        context = runtime.current_carto_job_context()
        name = context.job_dir.name
        assert threading.get_ident() == loop_thread and asyncio.current_task() is owners[name]
        assert current_operation_cache().operation_id == operations[name] and held_mutation_lock_regions()
        return name
    async def update(node_id, request):
        loop_context()
    async def create(request, **kwargs):
        name = loop_context()
        return SimpleNamespace(id=node_uuid("created-" + name))
    async def listed(request):
        loop_context()
        return [], None
    async def move(*args, **kwargs):
        loop_context()
    client.update_node, client.create_node, client.list_nodes, client.move_node = update, create, listed, move
    client._mark_nodes_export_dirty = lambda ids: None
    async def reconcile(**kwargs):
        name = loop_context()
        await original_driver(**kwargs)
        if fail_first and name == "first":
            raise ValueError("fixture post-reconcile failure")
    monkeypatch.setattr(driver, "reconcile_tree", reconcile)
    async def run(name):
        owners[name] = asyncio.current_task()
        control = runtime.CartoJobControl(task=owners[name])
        context = contexts[name] = job_context(env.path / name, control)
        previous_sink = object()
        context.diagnostic_sink = previous_sink
        marker_token = marker.set(name)
        try:
            with runtime.carto_job_scope(context), etch.weave_log_scope(str(env.path / (name + "-outer.json"))):
                async with cache_operation_boundary(client, "n07-file-artifacts-" + name) as snapshot:
                    operations[name] = snapshot.operation_id
                    async with mutation_region_locks(client=client, operation_label="n07-artifacts-" + name,
                        region_factory=lambda proof: (build_region_subtree(node_uuid(name), cache_by_id=proof.nodes_by_id),),
                        hold_mode="long_lived", expected_duration_seconds=None):
                        try:
                            results[name] = await nexus.WorkFlowyClientNexus.refresh_file_node_beacons(client, node_uuid(name))
                        except NetworkError as exc:
                            assert fail_first and name == "first" and "fixture post-reconcile failure" in str(exc.details)
                            results[name] = exc
                        assert held_mutation_lock_regions() and control.cpu_future is None
                        assert context.diagnostic_sink is previous_sink, "FILE leaked its diagnostic sink on success/failure"
                        assert etch._current_weave_context.get() == str(env.path / (name + "-outer.json"))
            assert runtime.current_carto_job_context() is None and etch._current_weave_context.get() is None
        finally:
            marker.reset(marker_token)
    tasks = [asyncio.create_task(run(name)) for name in names]
    try:
        for event in entered.values():
            assert await asyncio.to_thread(event.wait, 10), "file artifact worker never became outstanding"
        assert all(not task.done() for task in tasks), "file released ownership before joined artifact completion"
    finally:
        released.set()
        await asyncio.gather(*tasks)
    assert not held_mutation_lock_regions() and runtime.current_carto_job_context() is None
    assert {"source", "log", "progress", "journal-open", "journal-finish"} <= set(artifact_kinds)
    assert "journal-entry" not in artifact_kinds, "job used the loop-side legacy journal callback"
    for name in names:
        artifacts = env.path / name / "file_refresh"
        sources = list(artifacts.glob("*file_refresh*.json"))
        sources = [path for path in sources if not path.name.endswith(".weave_journal.json")]
        assert len(sources) == 1
        source = json.loads(sources[0].read_text())
        assert source["export_root_id"] == node_uuid(name)
        journal_path = sources[0].with_suffix(".weave_journal.json")
        journal = json.loads(journal_path.read_text())
        assert journal["entries"], "joined diagnostic sink lost the real reconcile journal"
        assert journal["last_run_completed"] is (not fail_first or name != "first")
        assert journal["phase"] == ("error" if fail_first and name == "first" else "completed")
        assert str(journal_path) in sink_threads
        progress = json.loads(contexts[name].job_file.read_text())["progress"]
        assert progress["nodes_created"] == 1 and progress["nodes_updated"] == 1
        assert contexts[name].progress["nodes_created"] == 1 and contexts[name].progress["nodes_updated"] == 1
    assert wrong.read_text() == "unchanged sentinel" and not (env.path / "wrong-environment-dir").exists()


@pytest.mark.asyncio
async def test_job_debug_path_failure_keeps_original_best_effort_semantics(scrubbed_runtime, monkeypatch):
    from workflowy_mcp.client.carto_reconcile_cpu import CartoDiagnosticSink
    from n07_job_harness import run_reconcile_oracle, reconcile_oracle_cases
    context = job_context(scrubbed_runtime, runtime.CartoJobControl(task=asyncio.current_task()))
    context.diagnostic_sink = CartoDiagnosticSink(debug_path=scrubbed_runtime / "missing" / "debug.log")
    with runtime.carto_job_scope(context):
        result = await run_reconcile_oracle(reconcile_oracle_cases()["normalized_noop"], monkeypatch)
    assert result["error"] is None
    assert context.job_control.cpu_failure is None, "best-effort debug failure latched as operational failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["normal", "dry", "resolver-update-fail", "poison-update-fail", "threshold"])
async def test_folder_group_plan_keeps_loop_migrations_and_strict_file_decisions(task_core_environment, monkeypatch, case):
    from types import SimpleNamespace
    from coherence_harness import make_boundary_capable
    from n05_region_harness import node_uuid
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.mutation_locks import mutation_region_locks, held_mutation_lock_regions, MUTATION_LOCK_STATE
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    from workflowy_mcp.models import NetworkError
    env = task_core_environment
    root = env.path / "repository"
    root.mkdir()
    present = root / "present.py"
    present.write_text("def present():\n    return 1\n", encoding="utf-8")
    folder_id, file_id, missing_id = (node_uuid(name) for name in ("folder", "file", "missing"))
    old_note = f"Path: {present}\nLINE COUNT: 2\n\nuser suffix\n"
    if case == "poison-update-fail":
        old_note = "Path: repository\nLINE COUNT: 2\n\nuser suffix\n"
    if case == "threshold":
        old_note = "Path: present.py\nLINE COUNT: 2\n\nuser suffix\n"
    nodes = [{"id": folder_id, "name": "📂 repository", "note": f"Root: {root}"},
             {"id": file_id, "name": "🐍 present.py #cosmetic", "note": old_note, "parent_id": folder_id},
             {"id": node_uuid("prose"), "name": "notes", "note": "prose\nPath: missing-example.py", "parent_id": folder_id}]
    if case == "threshold":
        nodes.append({"id": missing_id, "name": "🐍 missing.py", "note": "Path: missing.py", "parent_id": folder_id})
    client = make_boundary_capable(SimpleNamespace(), nodes=nodes)
    original_advance, original_plan = nexus._advance_carto_folder_groups, nexus._plan_carto_folder_groups
    owner, loop_thread = asyncio.current_task(), threading.get_ident()
    maps, events, advances = [], [], []
    def planned(*args):
        # Constructing an unstarted data generator is constant loop work.
        maps.append(args[1])
        return original_plan(*args)
    def advance(*args):
        assert threading.get_ident() != loop_thread, "folder topology planned on event loop"
        assert current_operation_cache(required=False) is None and runtime.current_carto_job_context() is None
        assert MUTATION_LOCK_STATE.get() is None
        advances.append(True)
        return original_advance(*args)
    def no_index(*args):
        raise AssertionError("folder rebuilt the global index despite a valid authority pin")
    monkeypatch.setattr(nexus, "_plan_carto_folder_groups", planned)
    monkeypatch.setattr(nexus, "_advance_carto_folder_groups", advance)
    monkeypatch.setattr(nexus, "_index_carto_export_nodes", no_index)
    monkeypatch.setattr(nexus, "get_server_config", lambda: SimpleNamespace(governance=SimpleNamespace(carto_folder_file_delete_threshold=0)))
    async def update(node_id, request):
        assert asyncio.current_task() is owner and held_mutation_lock_regions()
        assert current_operation_cache().operation_id == operation_id and node_id == file_id
        assert request.name is None and request.layoutMode is None
        # H's segment migration uses splitlines/join (unlike FILE count merge).
        assert request.note == "Path: present.py\nLINE COUNT: 2\n\nuser suffix"
        events.append("migration")
        if case.endswith("update-fail"):
            raise RuntimeError("fixture migration update failed")
    async def refresh(**kwargs):
        assert asyncio.current_task() is owner and held_mutation_lock_regions()
        assert current_operation_cache().operation_id == operation_id
        assert kwargs["file_node_id"] == file_id and kwargs["diagnostic_parent_id"] == folder_id
        events.append("refresh")
        return {"success": True}
    client.update_node, client.refresh_file_node_beacons = update, refresh
    client._mark_nodes_export_dirty = lambda ids: None
    control = runtime.CartoJobControl(task=owner)
    with runtime.carto_job_scope(job_context(env.path, control)):
        async with cache_operation_boundary(client, "n07-folder-plan") as snapshot:
            operation_id = snapshot.operation_id
            initial_view = snapshot.view
            async with mutation_region_locks(client=client, operation_label="n07-folder-plan",
                region_factory=lambda proof: (build_region_subtree(folder_id, cache_by_id=proof.nodes_by_id),),
                hold_mode="long_lived", expected_duration_seconds=None):
                if case == "poison-update-fail":
                    with pytest.raises(RuntimeError, match="fixture migration update failed"):
                        await nexus.WorkFlowyClientNexus.refresh_folder_cartographer_sync(client, folder_id)
                    assert events == ["migration"]
                elif case == "threshold":
                    with pytest.raises(NetworkError, match="mass-deletion guard"):
                        await nexus.WorkFlowyClientNexus.refresh_folder_cartographer_sync(client, folder_id)
                    assert events == []
                else:
                    result = await nexus.WorkFlowyClientNexus.refresh_folder_cartographer_sync(client, folder_id, dry_run=case == "dry")
                    assert result["success"] and result["refreshed_file_nodes"] == 1
                    assert result["missing_paths"] == [] and result["files_deleted"] == 0
                    assert result["present_paths"] == [str(present)]
                    assert events == (["refresh"] if case == "dry" else ["migration", "refresh"])
                assert maps == [initial_view.nodes_by_id] and maps[0] is initial_view.nodes_by_id
                assert advances and current_operation_cache().view is initial_view
    assert control.cpu_future is None


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_folder_disk_pipeline_rechecks_ignore_and_owns_all_data_phases(task_core_environment, monkeypatch, dry_run):
    import json
    from types import SimpleNamespace
    from coherence_harness import make_boundary_capable
    from n05_region_harness import node_uuid
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.mutation_locks import mutation_region_locks, held_mutation_lock_regions, MUTATION_LOCK_STATE
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    env = task_core_environment
    root = env.path / "repository"
    root.mkdir()
    for name in ("first", "second", "ignored", "new"):
        (root / f"{name}.py").write_text(f"def {name}():\n    return 1\n", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "inner.py").write_text("def inner():\n    return 2\n", encoding="utf-8")
    (root / "skipdir").mkdir()
    (root / "skipdir" / "hidden.py").write_text("pass\n", encoding="utf-8")
    ignore = root / ".nexusignore"
    ignore.write_text("ignored.py\nskipdir\n", encoding="utf-16")
    folder_id = node_uuid("folder")
    nodes = [{"id": folder_id, "name": "📂 repository", "note": f"Root: {root}"}]
    for name in ("first", "second", "ignored", "missing"):
        nodes.append({"id": node_uuid(name), "parent_id": folder_id, "name": f"🐍 {name}.py", "note": f"Path: {name}.py"})
    client = make_boundary_capable(SimpleNamespace(), nodes=nodes)
    owner, loop_thread = asyncio.current_task(), threading.get_ident()
    marker = contextvars.ContextVar("n07-folder-disk-marker", default="empty")
    token = marker.set("loop")
    helpers, operations, events = [], [], []
    names = ("_scan_carto_folder_disk", "_plan_carto_folder_creation", "_format_carto_folder_note",
             "_carto_folder_child_exists", "_carto_creation_values", "_carto_folder_segment_values",
             "_prepare_carto_source_file", "_write_carto_file_artifact", "_carto_folder_display_path",
             "_merge_carto_folder_warnings")
    for helper_name in names:
        original = getattr(nexus, helper_name)
        def traced(*args, _name=helper_name, _original=original, **kwargs):
            assert threading.get_ident() != loop_thread, f"{_name} ran on event loop"
            assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
            assert MUTATION_LOCK_STATE.get() is None and marker.get() == "empty"
            helpers.append(_name)
            if _name == "_scan_carto_folder_disk":
                operations.append(args[0])
                if len(args) > 1 and args[1] is not None:
                    assert not any(value is client or callable(value) for value in vars(args[1]).values())
            return _original(*args, **kwargs)
        monkeypatch.setattr(nexus, helper_name, traced)
    def loop_check():
        assert asyncio.current_task() is owner and threading.get_ident() == loop_thread
        assert held_mutation_lock_regions() and current_operation_cache().operation_id == operation_id
    async def refresh(**kwargs):
        loop_check()
        assert kwargs["cancel_callback"].__self__ is context.job_control and kwargs["cancel_callback"]() is False
        events.append(("refresh", kwargs["file_node_id"]))
        # A real disk edit between FILE groups must affect the NEXT group.
        old_mtime = ignore.stat().st_mtime
        ignore.write_text("second.py\nignored.py\nskipdir\n", encoding="utf-16")
        import os
        os.utime(ignore, (old_mtime + 2, old_mtime + 2))
        return {"success": True, "warnings": [{"message": "fixture nonfatal warning"}]}
    async def delete(node_id):
        loop_check()
        events.append(("delete", node_id))
        return True
    async def create(request, **kwargs):
        loop_check()
        events.append(("create", request.name, request.parent_id, request.note))
        return SimpleNamespace(id=node_uuid("new-" + str(len(events))))
    client.refresh_file_node_beacons, client.delete_node, client.create_node = refresh, delete, create
    client._mark_nodes_export_dirty = lambda ids: None
    control = runtime.CartoJobControl(task=owner)
    context = job_context(env.path, control)
    context.job_file.write_text(json.dumps({"id": "folder-fixture", "status": "running", "progress": {}}))
    bogus = env.path / "bogus-job.json"
    bogus.write_text("must stay unchanged")
    monkeypatch.setenv("CARTO_JOB_FILE", str(bogus))
    try:
        with runtime.carto_job_scope(context):
            async with cache_operation_boundary(client, "n07-folder-disk") as snapshot:
                operation_id = snapshot.operation_id
                async with mutation_region_locks(client=client, operation_label="n07-folder-disk",
                    region_factory=lambda proof: (build_region_subtree(folder_id, cache_by_id=proof.nodes_by_id),),
                    hold_mode="long_lived", expected_duration_seconds=None):
                    result = await nexus.WorkFlowyClientNexus.refresh_folder_cartographer_sync(client, folder_id, dry_run=dry_run)
                    assert held_mutation_lock_regions() and control.cpu_future is None
    finally:
        marker.reset(token)
    assert result["refreshed_file_nodes"] == 1 and result["ignored_existing_files"] == 2
    assert result["warnings"] == [{"message": "fixture nonfatal warning"}]
    assert result["nexusignore_sources"] == [str(ignore)]
    assert context.relpath_memo == {node_uuid(name): name + ".py" for name in ("first", "second", "ignored")}
    assert bogus.read_text() == "must stay unchanged"
    assert "_scan_carto_folder_disk" in helpers and "_carto_folder_display_path" in helpers
    if dry_run:
        assert events == [("refresh", node_uuid("first"))]
        assert result["disk_scan_performed"] is False and result["new_files_created"] == 0 and result["files_deleted"] == 0
        assert set(operations) == {"initialize", "existing", "summary"}
    else:
        assert events[:2] == [("refresh", node_uuid("first")), ("delete", node_uuid("missing"))]
        creates = [event for event in events if event[0] == "create"]
        assert [event[1] for event in creates] == ["📂 nested", "🐍 inner.py", "ƒ inner()", "🐍 new.py", "ƒ new()"]
        assert result["new_files_created"] == 2 and result["new_folders_created"] == 1 and result["files_deleted"] == 1
        assert result["disk_scan_performed"] and result["disk_files_considered"] == 3 and result["ignored_disk_files"] == 2
        assert set(helpers) == set(names)
        assert {"start", "next", "row", "close"} <= set(operations)
        persisted = json.loads(context.job_file.read_text())
        assert persisted["result_summary"]["files_refreshed"] == 1 and persisted["result_summary"]["new_folders_created"] == 1
        # H's disk-create progress counter covers FILE subtrees, while the
        # intermediate-folder count remains its own original summary field.
        assert persisted["progress"]["nodes_created"] == 4
    assert control.cpu_failure is None and control.cpu_future is None


@pytest.mark.asyncio
@pytest.mark.parametrize("abandon", [False, True])
async def test_folder_restart_observes_only_the_next_complete_group_with_real_counters(task_core_environment, monkeypatch, abandon):
    import json
    from types import SimpleNamespace
    from coherence_harness import make_boundary_capable
    from n05_region_harness import node_uuid
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    from workflowy_mcp.mutation_locks import mutation_region_locks, held_mutation_lock_regions
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    env = task_core_environment
    root = env.path / "restart-fixture"
    root.mkdir()
    nodes = [{"id": node_uuid("folder"), "name": "📂 restart", "note": f"Root: {root}"}]
    for name in ("first", "second"):
        (root / (name + ".py")).write_text("pass\n", encoding="utf-8")
        nodes.append({"id": node_uuid(name), "name": name + ".py", "note": "Path: " + name + ".py", "parent_id": node_uuid("folder")})
    client = make_boundary_capable(SimpleNamespace(), nodes=nodes)
    context = job_context(env.path, runtime.CartoJobControl(task=asyncio.current_task()))
    context.job_file.write_text(json.dumps({"id": "restart-fixture", "status": "running", "progress": {}}))
    refreshed = []
    async def refresh(**kwargs):
        assert held_mutation_lock_regions()
        refreshed.append(kwargs["file_node_id"])
        if len(refreshed) == 1:
            context.job_control.restart_state["restart_id"] = "fixture-restart"
            assert context.job_control.user_cancel_requested() is False
            if abandon:
                context.job_control.restart_state["restart_id"] = None
        return {"success": True}
    client.refresh_file_node_beacons = refresh
    client._mark_nodes_export_dirty = lambda ids: None
    with runtime.carto_job_scope(context):
        async with cache_operation_boundary(client, "n07-folder-restart"):
            async with mutation_region_locks(client=client, operation_label="n07-folder-restart",
                region_factory=lambda proof: (build_region_subtree(node_uuid("folder"), cache_by_id=proof.nodes_by_id),),
                hold_mode="long_lived", expected_duration_seconds=None):
                if abandon:
                    result = await nexus.WorkFlowyClientNexus.refresh_folder_cartographer_sync(client, node_uuid("folder"))
                    assert result["refreshed_file_nodes"] == 2 and len(refreshed) == 2
                    assert context.job_control.restart_state["observed"] is False
                else:
                    with pytest.raises(asyncio.CancelledError, match="restart boundary"):
                        await nexus.WorkFlowyClientNexus.refresh_folder_cartographer_sync(client, node_uuid("folder"))
                    assert refreshed == [node_uuid("first")] and context.job_control.restart_state["observed"]
                    persisted = json.loads(context.job_file.read_text())
                    assert persisted["status"] == "cancelling" and persisted["result_summary"]["files_refreshed"] == 1
                assert held_mutation_lock_regions() and context.job_control.cpu_future is None
    assert context.job_control.user_requested is False and not list(env.path.glob("*.cancel*"))


@pytest.mark.asyncio
async def test_folder_disk_source_rediscovery_and_unconsumed_cancel_row(scrubbed_runtime):
    import os
    from workflowy_mcp.client import api_client_nexus as nexus
    root = scrubbed_runtime / "discovery"
    root.mkdir()
    blocked = root / "blocked.py"
    blocked.write_text("pass\n", encoding="utf-8")
    (root / "nested").mkdir()
    hidden = root / "nested" / "hidden.py"
    hidden.write_text("pass\n", encoding="utf-8")
    state = await runtime.run_carto_cpu("fixture:disk-init", nexus._scan_carto_folder_disk, "initialize",
                                        root_path=str(root), repo_root_abs=None)
    ignore = root / ".nexusignore"
    ignore.write_text("blocked.py\n", encoding="utf-16")
    for _ in range(24):
        assert await runtime.run_carto_cpu("fixture:disk-recheck", nexus._scan_carto_folder_disk,
                                           "existing", state, source_path=str(blocked)) == (False, "")
    ignored, reason = await runtime.run_carto_cpu("fixture:disk-recheck", nexus._scan_carto_folder_disk,
                                                 "existing", state, source_path=str(blocked))
    assert ignored and reason == "segment:blocked.py base=folder_root rel=blocked.py"
    previous_mtime = ignore.stat().st_mtime
    ignore.write_text("/nested/*.py\n", encoding="utf-16")
    os.utime(ignore, (previous_mtime + 2, previous_mtime + 2))
    ignored, reason = await runtime.run_carto_cpu("fixture:disk-recheck", nexus._scan_carto_folder_disk,
                                                 "existing", state, source_path=str(hidden))
    assert ignored and reason == "path:/nested/*.py base=folder_root rel=nested/hidden.py"
    await runtime.run_carto_cpu("fixture:disk-start", nexus._scan_carto_folder_disk, "start", state)
    loop_thread = threading.get_ident()
    class CloseSpy:
        def __init__(self, iterator):
            self.iterator, self.closed = iterator, False
        def close(self):
            assert threading.get_ident() != loop_thread, "folder walk closed on loop"
            self.closed = True
            self.iterator.close()
    close_spy = None
    try:
        assert await runtime.run_carto_cpu("fixture:disk-next", nexus._scan_carto_folder_disk, "next", state)
        # The real loop may now observe the legacy cancel callback: the fetched
        # row must not have entered the disk index before that observation.
        assert state.disk_files == {} and state.walk_row is not None
        close_spy = CloseSpy(state.walk)
        state.walk = close_spy
    finally:
        await runtime.run_carto_cpu("fixture:disk-close", nexus._scan_carto_folder_disk, "close", state)
    try:
        assert close_spy is not None and close_spy.closed, "folder discarded its unclosed walk"
    finally:
        if close_spy is not None and not close_spy.closed:
            await runtime.run_carto_cpu("fixture:disk-cleanup", close_spy.close)
    summary = await runtime.run_carto_cpu("fixture:disk-summary", nexus._scan_carto_folder_disk,
                                        "summary", state, performed=True)
    assert summary["disk_scan_performed"] and summary["disk_files"] == {}
    assert state.walk is None and state.walk_row is None


@pytest.mark.asyncio
@pytest.mark.parametrize("child,expected", [
    ({"name": "ordinary", "note": "Path: new.py"}, True),
    ({"name": "ordinary", "no": "Path: new.py\nLINE COUNT: 1"}, True),
    ({"name": "User notes on new.py", "note": "not mapped"}, True),
    ({"name": "📂 new.py", "note": "Path: new.py"}, False),
    ({"name": "ordinary", "note": "prose\nPath: new.py"}, False),
    ({"name": "ordinary", "note": "Path: other.py"}, False),
])
async def test_folder_duplicate_child_guard_keeps_exact_header_name_and_folder_rules(scrubbed_runtime, child, expected):
    from workflowy_mcp.client import api_client_nexus as nexus
    context = job_context(scrubbed_runtime, runtime.CartoJobControl(task=asyncio.current_task()))
    with runtime.carto_job_scope(context):
        actual = await runtime.run_carto_cpu("fixture:duplicate-child", nexus._carto_folder_child_exists,
                                              [child], str(scrubbed_runtime / "new.py"))
    assert actual is expected
    assert context.job_control.cpu_future is None and context.job_control.cpu_failure is None


@pytest.mark.asyncio
async def test_folder_cancel_during_walk_start_joins_and_closes_before_release(task_core_environment, monkeypatch):
    from types import SimpleNamespace
    from coherence_harness import make_boundary_capable
    from n05_region_harness import node_uuid
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    from workflowy_mcp.mutation_locks import mutation_region_locks, held_mutation_lock_regions
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    env = task_core_environment
    root = env.path / "cancel-start"
    root.mkdir()
    client = make_boundary_capable(SimpleNamespace(), nodes=[{
        "id": node_uuid("folder"), "name": "📂 fixture", "note": f"Root: {root}",
    }])
    original = nexus._scan_carto_folder_disk
    entered, release = threading.Event(), threading.Event()
    states, controls = [], []
    def disk(operation, state=None, **values):
        result = original(operation, state, **values)
        if operation == "start":
            states.append(state)
            entered.set()
            assert release.wait(10), "test did not release folder start future"
        return result
    monkeypatch.setattr(nexus, "_scan_carto_folder_disk", disk)
    async def run():
        control = runtime.CartoJobControl(task=asyncio.current_task())
        controls.append(control)
        with runtime.carto_job_scope(job_context(env.path, control)):
            async with cache_operation_boundary(client, "n07-cancel-walk-start"):
                async with mutation_region_locks(client=client, operation_label="n07-cancel-walk-start",
                    region_factory=lambda proof: (build_region_subtree(node_uuid("folder"), cache_by_id=proof.nodes_by_id),),
                    hold_mode="long_lived", expected_duration_seconds=None):
                    with pytest.raises(asyncio.CancelledError):
                        await nexus.WorkFlowyClientNexus.refresh_folder_cartographer_sync(client, node_uuid("folder"))
                    assert held_mutation_lock_regions() and control.cpu_future is None
                    assert states[0].walk is None, "walk-start cancellation released a live owned iterator"
                    assert control.user_observed
    task = asyncio.create_task(run())
    try:
        assert await asyncio.to_thread(entered.wait, 10), "folder start never became outstanding"
        controls[0].request_user_cancel()
        await asyncio.sleep(0)
        assert not task.done(), "user request detached the outstanding disk future"
    finally:
        release.set()
        try:
            await task
        finally:
            if states and states[0].walk is not None:
                await runtime.run_carto_cpu("fixture:walk-cleanup", original, "close", states[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "slow", "filter-error", "missing", "cache-error", "result-error", "non-dict", "cancel"])
async def test_bulk_candidate_decisions_are_data_work_with_original_loop_order(task_core_environment, monkeypatch, mode):
    from n05_region_harness import node_uuid
    from workflowy_mcp import server
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.mutation_locks import mutation_region_locks, held_mutation_lock_regions, MUTATION_LOCK_STATE
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    env, owner, loop_thread = task_core_environment, asyncio.current_task(), threading.get_ident()
    note = ("BEACON (MD AST)\nid: md@heading\nrole: Old role\nslice_labels: fixture\nkind: ast\n---\n"
            if mode == "sync" else "body without a beacon " * 100)
    candidate = {"id": "  " if mode == "missing" else node_uuid("symbol"),
                 "name": "Renamed role #fixture", "note": note}
    summary = {"node_updates": 0, "nodes_skipped": 0, "nodes_skipped_in_sync": 0, "errors": []}
    events, phases, messages = [], [], []
    original, original_filter = server._prepare_bulk_candidate, server._node_name_tags_match_beacon_block_in_note
    def data(operation, value):
        assert threading.get_ident() != loop_thread, "bulk candidate data work ran on loop"
        assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
        assert MUTATION_LOCK_STATE.get() is None
        assert value is candidate
        phases.append(operation)
        return original(operation, value)
    def filtered(value):
        assert threading.get_ident() != loop_thread
        assert events and events[-1] in {"cache", "log"}, "semantic filter ran before the original cache patch"
        events.append("filter")
        if mode == "filter-error":
            raise ValueError("fixture filter unavailable")
        return original_filter(value)
    monkeypatch.setattr(server, "_prepare_bulk_candidate", data)
    monkeypatch.setattr(server, "_node_name_tags_match_beacon_block_in_note", filtered)
    def loop():
        assert asyncio.current_task() is owner and threading.get_ident() == loop_thread
        assert held_mutation_lock_regions() and current_operation_cache().operation_id == operation_id
    async def cached(**kwargs):
        loop()
        assert kwargs == {"node_id": node_uuid("symbol"), "new_name": candidate["name"]}
        events.append("cache")
        if mode == "cache-error":
            raise ValueError("fixture cache patch unavailable")
    async def disk(**kwargs):
        loop()
        assert kwargs.pop("receipt_holder") == {}
        assert kwargs == {"node_id": node_uuid("symbol"), "name": candidate["name"], "note": note}
        events.append("disk")
        if mode == "non-dict":
            return "opaque"
        return {"success": mode != "result-error", "operation": "update", "error": "fixture disk refused" if mode == "result-error" else None}
    def log(message):
        loop()
        events.append("log")
        messages.append(message)
        if message.startswith("HELPER RESULT"):
            assert summary["node_updates"] == 0, "log/progress counter order changed"
    def progress():
        loop()
        events.append("progress")
        assert summary["node_updates"] == 1
    env.client.update_cached_node_name, env.client.update_beacon_from_node = cached, disk
    control = runtime.CartoJobControl(task=owner)
    with runtime.carto_job_scope(job_context(env.path, control)):
        async with cache_operation_boundary(env.client, "n07-bulk-candidate") as snapshot:
            operation_id = snapshot.operation_id
            async with mutation_region_locks(client=env.client, operation_label="n07-bulk-candidate",
                region_factory=lambda proof: (build_region_subtree(node_uuid("file"), cache_by_id=proof.nodes_by_id),),
                hold_mode="long_lived", expected_duration_seconds=None):
                result = await server._bulk_apply_per_file_inner_loop(env.client, [candidate], "source.md", summary,
                                                                    lambda: mode == "cancel", log, progress)
    if mode == "cancel":
        assert result == (0, True) and phases == [] and events == []
    elif mode == "missing":
        assert result == (0, False) and phases == ["values"] and events == []
        assert summary["nodes_skipped"] == 1 and summary["errors"] == ["Skipped visible node with missing id under file source.md."]
    elif mode == "sync":
        assert result == (0, False) and summary["nodes_skipped_in_sync"] == 1
        assert phases == ["values", "filter"] and events == ["cache", "filter"]
    else:
        assert phases == ["values", "filter"] and events[0] == "cache"
        assert events.index("cache") < events.index("filter") < events.index("disk")
        assert any(message.startswith("HELPER RESULT") for message in messages)
        if mode == "result-error":
            assert result == (0, False) and summary["nodes_skipped"] == 1 and summary["node_updates"] == 0
            assert "fixture disk refused" in summary["errors"][0] and "progress" not in events
        else:
            assert result == (1, False) and summary["node_updates"] == 1 and events[-1] == "progress"
        if mode in {"filter-error", "cache-error"}:
            assert messages[0].startswith("WARNING:")
    assert control.cpu_failure is None and control.cpu_future is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["clean", "warnings", "publish-error", "render-error", "phase8-error", "broken-log"])
async def test_markdown_pipeline_artifacts_are_joined_data_and_keep_phase_order(task_core_environment, monkeypatch, outcome):
    from j_counterfactual import install_j_counterfactual
    install_j_counterfactual(monkeypatch)
    from workflowy_mcp import markdown_generate_pipeline as pipeline, beacon_refresh_support as support
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.mutation_locks import mutation_region_locks, held_mutation_lock_regions, MUTATION_LOCK_STATE
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    from n05_region_harness import node_uuid
    env, owner, loop_thread = task_core_environment, asyncio.current_task(), threading.get_ident()
    output, path, log = env.path / "generated.md", env.path / "markdown.json", env.path / "markdown.log"
    if outcome == "broken-log":
        log.mkdir()
    path.write_text(json.dumps({"id": "markdown", "status": "preparing", "logs_path": str(log),
                                "progress": {}, "result_summary": {}}))
    import markdown_roundtrip as mdr
    events, artifacts, writes, plans, receipts = [], [], [], [], []
    originals = (pipeline._markdown_job_artifact, pipeline._write_generated_markdown,
                 pipeline._render_markdown_from_tree)
    original_prepare, original_aggregate = mdr.prepare_generated_markdown_beacons, support.aggregate_beacon_outcomes
    from workflowy_mcp.carto_job_record import CartoJobRecord
    original_append = CartoJobRecord.append_file_row
    def worker():
        assert threading.get_ident() != loop_thread, "Markdown data phase ran on loop"
        assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
        assert MUTATION_LOCK_STATE.get() is None
    def artifact(operation, target, value=None):
        worker()
        artifacts.append(operation)
        if operation == "status":
            assert value is not plans[-1] and value["progress"] is not plans[-1]["progress"]
            assert value["result_summary"] is not plans[-1]["result_summary"]
            writes.append(value["progress"]["current_phase"])
        return originals[0](operation, target, value)
    original_phase = pipeline._set_phase
    async def phase(*args):
        assert asyncio.current_task() is owner and held_mutation_lock_regions()
        plans.append(args[1])
        events.append(args[3])
        return await original_phase(*args)
    # Capture the initial status snapshot too, without making it a worker-owned task dict.
    original_snapshot = pipeline._markdown_job_snapshot
    def snapshot(job):
        assert asyncio.current_task() is owner
        if not plans or plans[-1] is not job:
            plans.append(job)
        return original_snapshot(job)
    def render(root):
        worker()
        if outcome == "render-error":
            raise ValueError("fixture renderer failed")
        return originals[2](root)
    def prepare(raw, forest, previous, **kwargs):
        worker()
        return original_prepare(raw, forest, previous, **kwargs)
    def aggregate(rows, **kwargs):
        worker()
        return original_aggregate(rows, **kwargs)
    def append_row(writer, row, **kwargs):
        worker()  # G5's result handoff, replacing the old batch aggregate seam
        return original_append(writer, row, **kwargs)
    def disk(target, text=None, **kwargs):
        worker()
        operation = kwargs.get("operation", "write")
        events.append(operation)
        if operation == "publish":
            holder = kwargs.get("receipt_holder")
            assert type(holder) is dict and holder == {}
            if outcome == "publish-error":
                return {"success": False, "error_kind": "plan_mismatch",
                        "error": "fixture publication refused", "evidence": {"injected": True}}
        result = originals[1](target, text, **kwargs)
        if operation == "publish" and outcome != "publish-error":
            # Capture the receipt before the producer consumes/releases its
            # holder; an observer must not demand retained completed evidence.
            receipts.append(dict(holder))
        return result
    async def exported(**kwargs):
        assert asyncio.current_task() is owner and current_operation_cache().operation_id == operation_id
        return {"nodes": [{"id": node_uuid("file"), "name": "file", "parent_id": None},
                          {"id": node_uuid("symbol"), "name": "Heading", "note": "Body", "parent_id": node_uuid("file")}]}
    async def inline(client, **kwargs):
        assert asyncio.current_task() is owner and held_mutation_lock_regions()
        assert current_operation_cache().operation_id == operation_id and output.read_text().endswith("\n")
        assert json.loads(path.read_text())["progress"]["current_phase"] == "phase8-refresh"
        kwargs["job_context"].progress.update({"nodes_created": 1, "nodes_updated": 2, "nodes_moved": 3, "nodes_deleted": 4})
        events.append("inline")
        if outcome == "phase8-error":
            raise RuntimeError("fixture phase8 failed after committed disk")
        row = {"file": os.path.realpath(str(output)), "status": "failed" if outcome == "warnings" else "refreshed",
               "success": outcome != "warnings", "changed": False, "findings": []}
        if outcome == "warnings":
            row.update(error="fixture refresh failed", warnings=[{"type": "file_refresh_failed", "error": "fixture refresh failed"}])
        return {"execution": "inline", "status": "failed" if outcome == "warnings" else "completed",
                "success": outcome != "warnings", "error": row.get("error"), "warnings": [], "files": [row], "result": row}
    monkeypatch.setattr(pipeline, "_markdown_job_artifact", artifact)
    monkeypatch.setattr(pipeline, "_markdown_job_snapshot", snapshot)
    monkeypatch.setattr(pipeline, "_set_phase", phase)
    monkeypatch.setattr(pipeline, "_render_markdown_from_tree", render)
    monkeypatch.setattr(pipeline, "_write_generated_markdown", disk)
    monkeypatch.setattr(mdr, "prepare_generated_markdown_beacons", prepare)
    monkeypatch.setattr(support, "aggregate_beacon_outcomes", aggregate)
    monkeypatch.setattr(CartoJobRecord, "append_file_row", append_row)
    monkeypatch.setattr(pipeline, "_run_phase8_refresh_inline", inline)
    monkeypatch.setattr(nexus, "resolve_cartographer_path_from_node", lambda **kwargs: {"abs_path": str(output)})
    env.client.export_nodes = exported
    control = runtime.CartoJobControl(task=owner)
    with runtime.carto_job_scope(job_context(env.path, control)):
        async with cache_operation_boundary(env.client, "markdown-artifacts") as proof:
            operation_id = proof.operation_id
            async with mutation_region_locks(client=env.client, operation_label="markdown-artifacts",
                region_factory=lambda view: (build_region_subtree(node_uuid("file"), cache_by_id=view.nodes_by_id),),
                hold_mode="long_lived", expected_duration_seconds=None):
                result = await pipeline.run_markdown_generate_pipeline(client=env.client, root_uuid=node_uuid("file"), job_file=str(path))
    returned = result
    from workflowy_mcp.carto_job_record import read_carto_job_view, join_carto_job_detail_pages
    pages, offset, slice_offset = [], 0, 0
    while True:
        page = await runtime.run_carto_cpu("markdown-detail-read", read_carto_job_view, path, job_id="markdown",
            options={"selection": "detail", "files_limit": 100, "offset": offset, "slice_offset": slice_offset})
        pages.append(page)
        cursor = page.get("detail_page") or {}
        offset, slice_offset = cursor.get("next_offset"), cursor.get("next_slice_offset") or 0
        if offset is None:
            break
    result = {**returned, **join_carto_job_detail_pages(pages)["result_summary"]}
    assert result["success"] is returned["success"]
    stored = json.loads(path.read_text())
    assert stored["record_layout"] == "summary-head/2"
    assert artifacts[0] == "read" and artifacts.count("status") == (5 if outcome in {"render-error", "publish-error"} else 6)
    assert writes[:2] == ["starting", "phase-2-resolve-path"]
    assert result["success"] is (outcome not in {"render-error", "publish-error", "phase8-error"})
    assert stored["status"] == ("failed" if not result["success"] else "completed_with_warnings" if outcome == "warnings" else "completed")
    expected = ["phase-2-resolve-path", "phase-5-re-export", "phase-6-write-markdown"]
    if outcome != "render-error":
        expected += ["phase-7-reapply-beacons", "publish"]
    if outcome in {"clean", "warnings", "phase8-error", "broken-log"}:
        expected += ["phase8-refresh", "inline"]
        assert {key: stored["progress"][key] for key in ("nodes_created", "nodes_updated", "nodes_moved", "nodes_deleted")} == {
            "nodes_created": 1, "nodes_updated": 2, "nodes_moved": 3, "nodes_deleted": 4,
        }, "Markdown terminal artifact overwrote its inline FILE progress"
        assert output.read_text() == "# Heading\n\nBody\n"
    assert events == expected
    assert len(receipts) == (0 if outcome in {"render-error", "publish-error"} else 1)
    if receipts:
        assert result["publication"] == receipts[0]["publication"] and receipts[0]["publication"]["success"]
    if outcome == "warnings":
        assert stored["warning_count"] == 2 and result["ast_beacons_skipped"] == 0
        assert result["phase8"]["success"] is False and result["partial_success"]
        assert stored["warnings"][1]["type"] == "phase8_refresh_failed"
        text = log.read_text()
        assert text.index("[phase=phase-7-reapply-beacons]") < text.index("[phase=phase8-refresh]")
    elif outcome == "publish-error":
        assert stored["error"] == "fixture publication refused" and not output.exists()
    elif outcome == "phase8-error":
        assert result["partial_success"] and result["publication"]["success"]
        assert result["files_changed"] == [os.path.realpath(str(output))]
        assert "fixture phase8 failed after committed disk" in stored["error"]
    if outcome == "render-error":
        assert control.cpu_failure[0] == "markdown-render" and stored["error"] == "fixture renderer failed"
    else:
        assert control.cpu_failure is None
    assert control.cpu_future is None and runtime.current_carto_job_context() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["read", "status", "log", "prepare", "publication", "aggregation"])
async def test_markdown_artifact_boundary_witness_rejects_inline_work(task_core_environment, monkeypatch, target):
    """Retired warning phases are replaced by U08 preparation, publication and R3 save."""
    from workflowy_mcp import markdown_generate_pipeline as pipeline
    original = pipeline.run_carto_cpu
    async def inline(phase, function, *args, **kwargs):
        selected = phase == {"read": "markdown-artifact:read", "status": "markdown-artifact:status",
                            "log": "markdown-artifact:log", "prepare": "markdown-prepare",
                            "publication": "markdown-write", "aggregation": "markdown-result"}[target]
        if selected:
            return function(*args, **kwargs)
        return await original(phase, function, *args, **kwargs)
    monkeypatch.setattr(pipeline, "run_carto_cpu", inline)
    with pytest.raises(AssertionError):
        await test_markdown_pipeline_artifacts_are_joined_data_and_keep_phase_order(
            task_core_environment, monkeypatch, "warnings",
        )


@pytest.mark.asyncio
async def test_markdown_status_future_is_joined_and_disjoint_job_state_stays_local(task_core_environment, monkeypatch):
    from workflowy_mcp import markdown_generate_pipeline as pipeline
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.mutation_locks import mutation_region_locks, held_mutation_lock_regions
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    from n05_region_harness import node_uuid
    env, loop_thread = task_core_environment, threading.get_ident()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = pipeline._markdown_job_artifact
    states, controls = {}, {}
    def artifact(operation, path, value=None):
        assert threading.get_ident() != loop_thread and runtime.current_carto_job_context() is None
        assert current_operation_cache(required=False) is None
        if operation == "status" and value["id"] == "first":
            entered.set()
            assert release.wait(10), "fixture did not release Markdown status write"
            try:
                return original(operation, path, value)
            finally:
                finished.set()
        return original(operation, path, value)
    monkeypatch.setattr(pipeline, "_markdown_job_artifact", artifact)
    async def run(label, root):
        path, log = env.path / f"{label}.json", env.path / f"{label}.log"
        control = controls[label] = runtime.CartoJobControl(task=asyncio.current_task())
        job = {"id": label, "status": "running", "progress": {}, "result_summary": {"errors": []},
               "warnings": [{"name": label}] * 1000}
        try:
            with runtime.carto_job_scope(job_context(env.path / label, control)):
                async with cache_operation_boundary(env.client, label):
                    async with mutation_region_locks(client=env.client, operation_label=label,
                        region_factory=lambda proof: (build_region_subtree(node_uuid(root), cache_by_id=proof.nodes_by_id),),
                        hold_mode="long_lived", expected_duration_seconds=None):
                        states[label] = (asyncio.current_task(), held_mutation_lock_regions())
                        await pipeline._set_phase(str(path), job, str(log), "working", label)
                        job["status"] = "completed"
                        await pipeline._persist_markdown_job(str(path), job)
            return job
        finally:
            if label == "first":
                assert finished.is_set(), "Markdown job released its grant with a live disk future"
            assert control.cpu_future is None and runtime.current_carto_job_context() is None
    first = asyncio.create_task(run("first", "file"))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 10), 11)
        controls["first"].request_user_cancel()
        second = await run("second", "other-file")
        assert second["status"] == "completed" and not first.done()
        assert states["first"][1] and states["second"][1] and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert controls["first"].user_observed and not controls["second"].user_observed
        for label, status in (("first", "running"), ("second", "completed")):
            stored = json.loads((env.path / f"{label}.json").read_text())
            assert stored["id"] == label and stored["status"] == status
            assert all(row["name"] == label for row in stored["warnings"])
        assert not (env.path / "first.log").exists()
        assert "[phase=working] second" in (env.path / "second.log").read_text()
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "result-error", "cache-error", "refresh-error", "cancel-during-progress", "empty"])
async def test_bulk_job_artifacts_join_real_callback_counts_before_next_effect(task_core_environment, monkeypatch, outcome):
    from workflowy_mcp import server
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.mutation_locks import mutation_region_locks, held_mutation_lock_regions, MUTATION_LOCK_STATE
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    from n05_region_harness import node_uuid
    env, loop_thread = task_core_environment, threading.get_ident()
    path, log = env.path / "bulk-artifact.json", env.path / "bulk-artifact.log"
    from workflowy_mcp.carto_job_record import record_for
    record_for(path).create({"id": "bulk-artifact", "type": "CARTO_BULK_APPLY", "mode": "all",
                            "claim_id": "fixture", "root_uuid": node_uuid("file"),
                            "status": "preparing", "logs_path": str(log)})
    events, batches, disk_events, live_jobs = [], [], [], []
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = server._write_bulk_job_artifacts
    original_snapshot = server._bulk_job_snapshot
    original_write, original_log = server._write_carto_job_payload, server._append_carto_job_log
    original_result_text, original_warning_values = server._bulk_result_log_text, server._bulk_group_warning_values
    control = runtime.CartoJobControl()
    def worker():
        assert threading.get_ident() != loop_thread, "bulk result/warning formatting ran on loop"
        assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
        assert MUTATION_LOCK_STATE.get() is None
    def result_text(*args):
        worker()
        return original_result_text(*args)
    def warning_values(*args):
        worker()
        return original_warning_values(*args)
    monkeypatch.setattr(server, "_bulk_result_log_text", result_text)
    monkeypatch.setattr(server, "_bulk_group_warning_values", warning_values)
    def snapshot(job):
        assert asyncio.current_task() is control.task
        live_jobs.append(job)
        return original_snapshot(job)
    def status_write(target, value):
        disk_events.append(("status", value))
        assert value is not live_jobs[-1] and value["progress"] is not live_jobs[-1]["progress"]
        assert value["result_summary"] is not live_jobs[-1]["result_summary"]
        return original_write(target, value)
    def log_write(target, value):
        disk_events.append(("log", value))
        return original_log(target, value)
    monkeypatch.setattr(server, "_bulk_job_snapshot", snapshot)
    monkeypatch.setattr(server, "_write_carto_job_payload", status_write)
    monkeypatch.setattr(server, "_append_carto_job_log", log_write)
    def artifacts(job_file, log_file, batch):
        assert threading.get_ident() != loop_thread, "bulk artifact serialized on loop"
        assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
        assert MUTATION_LOCK_STATE.get() is None
        # The writer consumes the owned batch before its return checkpoint.
        # Keep the original callback projection, not completed evidence rows.
        callbacks = [(op, value) for op, value in batch if op in {"result-log", "status", "log"}]
        batches.append(callbacks)
        has_result = any(op == "result-log" for op, _value in callbacks)
        if has_result:
            assert callbacks[0][0] == "result-log" and callbacks[0][1][0] == node_uuid("symbol")
            if outcome != "result-error":
                assert callbacks[1][0] == "status" and callbacks[1][1]["progress"]["nodes_updated"] == 1
            assert batch[0][0] == "nodes", "receipt append must precede this batch's head write"
            if outcome == "cancel-during-progress":
                entered.set()
                assert release.wait(10), "fixture did not release bulk progress write"
        before = len(disk_events)
        result = original(job_file, log_file, batch)
        expected = []
        for operation, value in callbacks:
            if operation == "result-log":
                message = (
                    f"HELPER RESULT node_id={node_uuid('symbol')} success={outcome != 'result-error'} "
                    "op='update' lang=None beacon_id=None slice_labels=None tags=None base_name=None "
                    "source_path=None error='fixture disk refused' cache_name_updated=None "
                    "cache_note_updated=None cache_skip=None"
                )
                expected.append(("log", message))
            else:
                expected.append((operation, value))
        assert disk_events[before:] == expected, "bulk artifact batch order or result text changed"
        if has_result:
            finished.set()
        return result
    monkeypatch.setattr(server, "_write_bulk_job_artifacts", artifacts)
    async def noop(*args, **kwargs):
        pass
    monkeypatch.setattr(server, "_await_nodes_export_cache_quiescent", noop)
    monkeypatch.setattr(server, "_refresh_carto_job_tracker_once", noop)
    def loop():
        assert asyncio.current_task() is control.task and threading.get_ident() == loop_thread
        assert held_mutation_lock_regions() and current_operation_cache().operation_id == operation_id
    async def cached(**kwargs):
        loop()
        events.append("cache")
        if outcome == "cache-error":
            raise ValueError("fixture cache patch failed")
    async def disk(**kwargs):
        loop()
        events.append("disk")
        return {"success": outcome != "result-error", "error": "fixture disk refused", "operation": "update"}
    async def refresh(**kwargs):
        loop()
        assert finished.is_set(), "FILE refresh overtook candidate artifacts"
        assert json.loads(path.read_text())["progress"]["nodes_updated"] == 1
        events.append("refresh")
        if outcome == "refresh-error":
            raise ValueError("fixture refresh failed")
        return {"success": True, "nodes_created": 2, "nodes_updated": 3, "nodes_moved": 4, "nodes_deleted": 5}
    env.client.update_cached_node_name, env.client.update_beacon_from_node = cached, disk
    env.client.refresh_file_node_beacons = refresh
    groups = {} if outcome == "empty" else {node_uuid("file"): {
        "file_node": {"name": "source.py"}, "nodes": [{"id": node_uuid("symbol"), "name": "Changed #fixture", "note": "body"}],
    }}
    async def run():
        nonlocal operation_id
        control.task = asyncio.current_task()
        with runtime.carto_job_scope(job_context(env.path, control)):
            async with cache_operation_boundary(env.client, "bulk-artifact") as snapshot:
                operation_id = snapshot.operation_id
                async with mutation_region_locks(client=env.client, operation_label="bulk-artifact",
                    region_factory=lambda proof: (build_region_subtree(node_uuid("file"), cache_by_id=proof.nodes_by_id),),
                    hold_mode="long_lived", expected_duration_seconds=None):
                    try:
                        await server._run_carto_bulk_visible_apply_job(
                            str(path), {}, "file", prepared_groups=groups,
                            grouping_warnings=("fixture grouping one", "fixture grouping two"),
                        )
                    finally:
                        if outcome == "cancel-during-progress":
                            assert finished.is_set(), "bulk grant released before progress disk future joined"
    operation_id = None
    task = asyncio.create_task(run())
    try:
        if outcome == "cancel-during-progress":
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 10), 11)
            control.request_user_cancel()
            assert not task.done() and not finished.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    stored = json.loads(path.read_text())
    from test_beacon_integration_contract import _joined_complete_detail
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(env.path))
    detail = await _joined_complete_detail("bulk-artifact")
    summary, progress = detail["result_summary"], stored["progress"]
    assert summary["warnings"][:2] == ["fixture grouping one", "fixture grouping two"]
    assert len(summary["warnings"]) == (3 if outcome == "refresh-error" else 2)
    if outcome == "refresh-error":
        assert summary["warnings"][-1] == {"type": "file_refresh_failed", "error": "fixture refresh failed"}
    log_text = log.read_text()
    assert log_text.index("WARNING: fixture grouping one") < log_text.index("WARNING: fixture grouping two") < log_text.index("Starting bulk")
    if outcome == "empty":
        assert events == [] and stored["cache_refresh_required"] is False and summary["node_updates"] == 0
    elif outcome == "cancel-during-progress":
        assert events == ["cache", "disk"] and summary["node_updates"] == progress["nodes_updated"] == 1
        assert stored["status"] == "cancelling" and control.user_observed
    elif outcome == "result-error":
        assert events == ["cache", "disk"] and summary["node_updates"] == 0 and summary["nodes_skipped"] == 1
        assert stored["status"] == "failed" and "fixture disk refused" in summary["errors"][0]
    else:
        assert events == ["cache", "disk", "refresh"] and summary["node_updates"] == 1
        assert summary["files_refreshed"] == (0 if outcome == "refresh-error" else 1)
        assert progress["nodes_created"] == (0 if outcome == "refresh-error" else 2)
        # The deliberately contradictory fake still returns success=True WITH
        # an error. Legacy counters/trigger/callbacks above remain unchanged;
        # R4's evidence correctly classifies that file failed, not clean work.
        assert stored["status"] == "failed"
    assert control.cpu_failure is None and control.cpu_future is None
    assert runtime.current_carto_job_context() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "result-error", "cache-error", "refresh-error", "empty"])
async def test_bulk_artifact_witness_rejects_inline_serialization(task_core_environment, monkeypatch, outcome):
    from workflowy_mcp import server
    original = server.run_carto_cpu
    async def inline(phase, function, *args, **kwargs):
        if phase == "bulk-artifact:batch":
            return function(*args, **kwargs)
        return await original(phase, function, *args, **kwargs)
    monkeypatch.setattr(server, "run_carto_cpu", inline)
    with pytest.raises(AssertionError, match="bulk artifact serialized on loop"):
        await test_bulk_job_artifacts_join_real_callback_counts_before_next_effect(
            task_core_environment, monkeypatch, outcome,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_kind", ["file", "markdown"])
async def test_disjoint_jobs_keep_cancel_progress_and_artifacts_task_local(task_core_environment, monkeypatch, cancel_kind):
    """C07: real FILE and full Markdown/inline FILE over one fake client, no sockets."""
    import os
    from pathlib import Path
    from types import SimpleNamespace
    from coherence_harness import make_boundary_capable
    from n05_region_harness import node_uuid
    from workflowy_mcp import markdown_generate_pipeline as pipeline
    from workflowy_mcp.client import api_client_nexus as nexus, api_client_etch as etch
    from workflowy_mcp.client.api_client_core import operation_cancel_scope
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.mutation_locks import mutation_region_locks, held_mutation_lock_regions
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    env, loop_thread = task_core_environment, threading.get_ident()
    paths = {"file": env.path / "source.py", "markdown": env.path / "generated.md"}
    paths["file"].write_text("def fixture_function():\n    return 1\n")
    paths["markdown"].write_text("old disk content\n")
    rows = [{"id": node_uuid(kind), "name": path.name, "note": f"Path: {path}\nLINE COUNT: 999\n"}
            for kind, path in paths.items()]
    rows.append({"id": node_uuid("heading"), "parent_id": node_uuid("markdown"),
                 "name": "Sample", "note": "Body", "priority": 0})
    client = make_boundary_capable(SimpleNamespace(_coherence_paths=lambda: None), nodes=rows)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    peer_entered, peer_release = threading.Event(), threading.Event()
    original_file, original_markdown = nexus._write_carto_file_artifact, pipeline._write_generated_markdown
    contexts, controls, operations, effects, results = {}, {}, {}, [], {}
    wrong = env.path / "wrong-job.json"
    wrong.write_text("wrong environment path must stay untouched")
    monkeypatch.setenv("CARTO_JOB_FILE", str(wrong))
    monkeypatch.setenv("CARTO_JOB_DIR", str(env.path / "wrong-directory"))
    def wait_data():
        assert threading.get_ident() != loop_thread and runtime.current_carto_job_context() is None
        assert current_operation_cache(required=False) is None
        entered.set()
        assert release.wait(10), "fixture did not release the cancelled job data phase"
    def file_artifact(kind, data):
        if cancel_kind == "file" and kind == "source" and Path(data["source_path"]) == paths["file"]:
            wait_data()
            try:
                return original_file(kind, data)
            finally:
                finished.set()
        if kind == "journal-finish" and str(env.path / other_kind) in str(data["path"]):
            peer_entered.set()
            assert peer_release.wait(10), "fixture did not release peer journal writer"
        return original_file(kind, data)
    def markdown_disk(path, text=None, **kwargs):
        if cancel_kind == "markdown" and kwargs.get("operation", "write") in {"write", "publish"}:
            wait_data()
            try:
                return original_markdown(path, text, **kwargs)
            finally:
                finished.set()
        return original_markdown(path, text, **kwargs)
    monkeypatch.setattr(nexus, "_write_carto_file_artifact", file_artifact)
    monkeypatch.setattr(pipeline, "_write_generated_markdown", markdown_disk)
    def loop(*, write=True):
        context = runtime.current_carto_job_context()
        kind = context.job_dir.name
        assert asyncio.current_task() is controls[kind].task and threading.get_ident() == loop_thread
        assert current_operation_cache().operation_id == operations[kind] and held_mutation_lock_regions()
        assert not controls[kind].user_cancel_requested(), "peer or stopped job reached another send"
        if write:
            effects.append(kind)
        return kind
    async def export(node_id=None):
        loop(write=False)
        return await etch.export_nodes_impl(client, node_id=node_id)
    async def update(node_id, request):
        loop()
    async def create(request, **kwargs):
        kind = loop()
        return SimpleNamespace(id=node_uuid("created-" + kind))
    async def listed(request):
        loop(write=False)
        return [], None
    async def moved(*args, **kwargs):
        loop()
    async def removed(*args, **kwargs):
        loop()
    async def refresh(**kwargs):
        return await nexus.WorkFlowyClientNexus.refresh_file_node_beacons(client, **kwargs)
    def forbidden_setter(*args):
        raise AssertionError("daemon job installed shared client cancellation state")
    client.export_nodes, client.update_node, client.create_node = export, update, create
    client.list_nodes, client.move_node, client.delete_node = listed, moved, removed
    client.refresh_file_node_beacons = refresh
    client._mark_nodes_export_dirty = lambda ids: None
    client.set_operation_cancel_callback = forbidden_setter
    async def run(kind):
        directory = env.path / kind
        directory.mkdir()
        control = controls[kind] = runtime.CartoJobControl(task=asyncio.current_task())
        context = contexts[kind] = job_context(directory, control)
        context.job_file.write_text(json.dumps({"id": kind, "status": "running", "logs_path": str(context.log_file),
                                                "progress": {}, "result_summary": {}}))
        previous_sink = object()
        context.diagnostic_sink = previous_sink
        try:
            with runtime.carto_job_scope(context), operation_cancel_scope(control.user_cancel_requested), etch.weave_log_scope(str(directory / "outer.json")):
                async with cache_operation_boundary(client, "c07-" + kind) as proof:
                    operations[kind] = proof.operation_id
                    async with mutation_region_locks(client=client, operation_label="c07-" + kind,
                        region_factory=lambda view: (build_region_subtree(node_uuid(kind), cache_by_id=view.nodes_by_id),),
                        hold_mode="long_lived", expected_duration_seconds=None):
                        try:
                            if kind == "file":
                                results[kind] = await refresh(file_node_id=node_uuid(kind), cancel_callback=control.user_cancel_requested)
                            else:
                                results[kind] = await pipeline.run_markdown_generate_pipeline(client=client, root_uuid=node_uuid(kind), job_file=str(context.job_file))
                        finally:
                            assert context.diagnostic_sink is previous_sink
                            assert etch._current_weave_context.get() == str(directory / "outer.json"), "C07 task-local weave path was overwritten"
                            assert control.cpu_future is None
                            if kind == cancel_kind and entered.is_set():
                                assert finished.is_set(), "C07 released ownership before its exact disk worker joined"
        finally:
            assert runtime.current_carto_job_context() is None and etch._current_weave_context.get() is None, "C07 task scopes did not reset"
    stopped = asyncio.create_task(run(cancel_kind))
    other_kind = "markdown" if cancel_kind == "file" else "file"
    async def gate_or_task(event, task):
        waiter = asyncio.create_task(asyncio.to_thread(event.wait, 10))
        try:
            await asyncio.wait((waiter, task), return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                await task
                raise AssertionError("C07 job ended before its deterministic data gate")
            assert await waiter, "C07 data gate never entered"
        finally:
            event.set()
            await waiter
    other = None
    try:
        await gate_or_task(entered, stopped)
        controls[cancel_kind].request_user_cancel()
        other = asyncio.create_task(run(other_kind))
        await gate_or_task(peer_entered, other)
        assert not other.done() and not stopped.done() and not finished.is_set()
        before = contexts[other_kind].job_file.read_bytes()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stopped
        assert not other.done() and controls[other_kind].cpu_future is not None
        assert contexts[other_kind].job_file.read_bytes() == before, "cancelled job rewrote its peer progress"
        peer_release.set()
        await other
        assert results[other_kind]["success"]
        assert controls[cancel_kind].user_observed and not controls[other_kind].user_requested
        assert effects and set(effects) == {other_kind}
        peer_progress = json.loads(contexts[other_kind].job_file.read_text())["progress"]
        assert peer_progress["nodes_created"] + peer_progress["nodes_updated"] >= 1
        assert wrong.read_text() == "wrong environment path must stay untouched"
        assert not (env.path / "wrong-directory").exists()
        assert os.environ["CARTO_JOB_FILE"] == str(wrong) and os.environ["CARTO_JOB_DIR"] == str(env.path / "wrong-directory")
        for kind in ("file", "markdown"):
            artifacts = contexts[kind].job_dir / "file_refresh"
            if artifacts.exists():
                for source in artifacts.glob("*file_refresh*.json"):
                    if not source.name.endswith(".weave_journal.json"):
                        assert json.loads(source.read_text())["export_root_id"] == node_uuid(kind)
    finally:
        release.set()
        peer_release.set()
        await asyncio.gather(*(task for task in (stopped, other) if task is not None), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mechanism", ["weave-global", "client-setter"])
async def test_c07_witness_rejects_legacy_process_state(task_core_environment, monkeypatch, mechanism):
    """Run C07 with one test-local restoration of the retired process assumption."""
    from workflowy_mcp.client import api_client_etch as etch, api_client_nexus as nexus
    if mechanism == "weave-global":
        class ProcessGlobalPath:
            value = None
            def get(self):
                return self.value
            def set(self, value):
                previous, self.value = self.value, value
                return previous
            def reset(self, previous):
                self.value = previous
        monkeypatch.setattr(etch, "_current_weave_context", ProcessGlobalPath())
    else:
        original = nexus.WorkFlowyClientNexus.refresh_file_node_beacons
        async def shared_setter(client, *args, **kwargs):
            client.set_operation_cancel_callback(kwargs.get("cancel_callback"))
            return await original(client, *args, **kwargs)
        monkeypatch.setattr(nexus.WorkFlowyClientNexus, "refresh_file_node_beacons", shared_setter)
    message = "C07 task-local weave path|C07 task scopes" if mechanism == "weave-global" else "shared client cancellation state"
    with pytest.raises(AssertionError, match=message):
        await test_disjoint_jobs_keep_cancel_progress_and_artifacts_task_local(
            task_core_environment, monkeypatch, "file",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("front", ["markdown", "materialize"])
@pytest.mark.parametrize("walk_mode", ["all", "visible"])
async def test_bulk_ingress_collects_data_off_loop_with_original_front_order(task_core_environment, monkeypatch, front, walk_mode):
    from types import SimpleNamespace
    from coherence_harness import make_boundary_capable
    from n05_region_harness import node_uuid
    from workflowy_mcp import server
    from workflowy_mcp import markdown_generate_pipeline as pipeline
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    from workflowy_mcp.mutation_locks import MUTATION_LOCK_STATE, mutation_region_locks, held_mutation_lock_regions
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    env, owner, loop_thread = task_core_environment, asyncio.current_task(), threading.get_ident()
    root_id, file_id, leaf_id = (node_uuid(name) for name in ("ingress-root", "ingress-md", "ingress-leaf"))
    second_leaf_id = node_uuid("ingress-second-leaf")
    rows = [{"id": root_id, "name": "Folder", "note": f"Root: {env.path}"},
            {"id": file_id, "parentId": root_id, "name": "document.md", "note": "Path: document.md"},
            {"id": leaf_id, "parent_id": file_id, "name": "Leaf", "note": "# Heading\n\nBody\n"},
            {"id": second_leaf_id, "parent_id": file_id, "name": "Leaf", "note": "# Heading\n\nOther\n"}]
    client = make_boundary_capable(SimpleNamespace(), nodes=rows)
    events, replies, launches, getters = [], [], [], []
    def getter():
        assert threading.get_ident() == loop_thread and asyncio.current_task() is owner
        assert current_operation_cache(required=False) is not None
        getters.append(True)
        return rows
    client._get_nodes_export_cache_nodes = getter
    monkeypatch.setattr(server, "get_client", lambda: client)
    original_tree, original_collect = server._build_carto_cache_subtree, server._collect_carto_bulk_nodes
    original_write = pipeline._write_generated_markdown
    for name, marker in (("_build_carto_materialize_index", "index"), ("_carto_materialize_target", "target"),
                         ("_assign_collision_safe_md_filenames", "names"), ("_carto_materialize_note_values", "note"),
                         ("derive_preparation_input_digest", "digest"), ("_serialize_carto_bulk_reply", "serialize")):
        original = getattr(server, name)
        def traced(*args, _original=original, _marker=marker, **kwargs):
            data()
            events.append(_marker)
            return _original(*args, **kwargs)
        monkeypatch.setattr(server, name, traced)
    def data():
        assert threading.get_ident() != loop_thread, "bulk ingress traversal ran on loop"
        assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
        assert MUTATION_LOCK_STATE.get() is None
    def tree(values, root):
        data()
        assert values is rows and root == root_id
        events.append("tree")
        return original_tree(values, root)
    def collect(root, kind):
        data()
        assert kind == front
        events.append("collect")
        return original_collect(root, kind)
    def write(*args, **kwargs):
        data()
        events.append("write")
        return original_write(*args, **kwargs)
    monkeypatch.setattr(server, "_build_carto_cache_subtree", tree)
    monkeypatch.setattr(server, "_collect_carto_bulk_nodes", collect)
    monkeypatch.setattr(pipeline, "_write_generated_markdown", write)
    selection = {"snapshot_path": "diagnostic-only", "cache_by_id": client._cache_view_publisher.capture().nodes_by_id}
    async def freshness(**kwargs):
        assert asyncio.current_task() is owner
        events.append("freshness")
        return selection
    async def markdown_start(*, root_uuid, selected_snapshot):
        assert asyncio.current_task() is owner and selected_snapshot is selection
        events.append("launch")
        launches.append(root_uuid)
        return {"success": True, "job_id": "fixture-md", "detached": False}
    async def materialize_start(root_uuid, mode, extra=None, **kwargs):
        assert asyncio.current_task() is owner and mode == "file"
        assert kwargs["frozen_payload"]["note_to_write"].startswith("# Heading")
        assert kwargs["source_regime"] == "temp_override"
        from pathlib import Path
        assert Path(kwargs["frozen_payload"]["abs_path"]).name == ("Leaf.md" if not launches else "Leaf_2.md")
        events.append("launch")
        launches.append(root_uuid)
        control = runtime.CartoJobControl(task=owner)
        context = job_context(env.path / "materialize-scope", control)
        with runtime.carto_job_scope(context):
            async with mutation_region_locks(client=client, operation_label="C06-materialize",
                region_factory=lambda proof: (build_region_subtree(root_uuid, cache_by_id=proof.nodes_by_id),),
                hold_mode="long_lived", expected_duration_seconds=None):
                value = await kwargs["preparation"]({**kwargs["frozen_payload"], "_job_file": str(context.job_file)}, {})
                assert kwargs["preparation_gate"](value) == {"proceed": True}
                assert control.may_have_workflowy_effects and control.cpu_future is None
        return {"success": True, "job_id": "fixture-materialize", "detached": False}
    async def update(node_id, request):
        assert asyncio.current_task() is owner and held_mutation_lock_regions()
        assert node_id in {leaf_id, second_leaf_id} and request.note == ""
        events.append("update")
    def dirty(ids):
        assert asyncio.current_task() is owner and held_mutation_lock_regions() and len(ids) == 1 and ids[0] in {leaf_id, second_leaf_id}
    client.update_node, client._mark_nodes_export_dirty = update, dirty
    monkeypatch.setattr(server, "_carto_select_snapshot", freshness)
    monkeypatch.setattr(server, "_start_carto_markdown_generate_job", markdown_start)
    monkeypatch.setattr(server, "_start_carto_refresh_job", materialize_start)
    monkeypatch.setattr(nexus, "resolve_new_markdown_dir_from_ancestors", lambda **kwargs: {"target_dir": str(env.path), "is_temporary": True})
    class Socket:
        async def send(self, text):
            assert asyncio.current_task() is owner and threading.get_ident() == loop_thread
            events.append("reply")
            replies.append(json.loads(text))
    payload = {"node_id": root_id, "walk_mode": walk_mode, "visible_tree": {
        "root": rows[0], "children": [{**rows[1], "children": rows[2:]}],
    }}
    handler = server._handle_bulk_generate_markdown if front == "markdown" else server._handle_bulk_materialize_markdown
    await handler(payload, Socket())
    assert launches == ([file_id] if front == "markdown" else [leaf_id, second_leaf_id])
    assert len(replies) == 1 and replies[0]["success"] is True and replies[0]["launched"] == len(launches)
    per_leaf = ["note", "digest", "launch", "write", "update"]
    expected = (["freshness"] if front == "markdown" else []) + (["tree"] if walk_mode == "all" else []) + ["collect"] + (["index", "target", "names"] + per_leaf * 2 if front == "materialize" else ["launch"]) + ["serialize", "reply"]
    assert events == expected, "bulk selection/tree/collection/admission order changed"
    assert len(getters) == (int(walk_mode == "all") + int(front == "materialize"))


@pytest.mark.asyncio
async def test_materialize_index_retains_overlay_alias_and_parent_policy(task_core_environment, monkeypatch):
    from workflowy_mcp import server
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    rows = [{"id": "root", "name": "cached root", "note": "old"},
            {"id": "child", "parentId": "cached-parent", "name": "cached child", "data": {"shared": [1]}},
            {"name": "ignored"}]
    visible = {"id": "root", "name": "visible root", "note": "new", "children": [
        {"id": "child", "name": "visible child", "note": "unsaved"},
        {"id": "new", "name": "new child", "note": "body"},
        {"name": "no id", "children": [{"id": "ignored-descendant"}]},
    ]}
    loop_thread = threading.get_ident()
    original = server._build_carto_materialize_index
    def data(*args):
        assert threading.get_ident() != loop_thread, "materialize index ran on loop"
        assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
        return original(*args)
    monkeypatch.setattr(server, "_build_carto_materialize_index", data)
    result = await server.run_carto_cpu("fixture:materialize-index", server._build_carto_materialize_index, rows, visible)
    assert set(result) == {"root", "child", "new"}
    assert result["root"] == {"id": "root", "name": "visible root", "note": "new"}
    assert result["child"]["name"] == "visible child" and result["child"]["note"] == "unsaved"
    assert result["child"]["parentId"] == "cached-parent" and "parent_id" not in result["child"]
    assert result["child"] is not rows[1] and result["child"]["data"] is rows[1]["data"]
    assert result["new"] == {"id": "new", "name": "new child", "note": "body", "parent_id": "root"}
    assert rows[0]["name"] == "cached root" and rows[1]["name"] == "cached child"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["bulk-materialize:index", "bulk-materialize:target", "bulk-materialize:names",
                                  "bulk-materialize:note", "bulk-materialize:digest", "bulk-materialize:reply", "bulk-markdown:reply"])
async def test_bulk_materialize_value_phases_reject_individual_inline_work(task_core_environment, monkeypatch, phase):
    from workflowy_mcp import server
    original = server.run_carto_cpu
    reached = []
    async def inline(selected, function, *args, **kwargs):
        if selected == phase:
            reached.append(selected)
            return function(*args, **kwargs)
        return await original(selected, function, *args, **kwargs)
    monkeypatch.setattr(server, "run_carto_cpu", inline)
    with pytest.raises(AssertionError):
        await test_bulk_ingress_collects_data_off_loop_with_original_front_order(
            task_core_environment, monkeypatch, "markdown" if phase.startswith("bulk-markdown") else "materialize", "visible",
        )
    assert reached == [phase]


@pytest.mark.asyncio
async def test_materialize_front_witness_rejects_lost_in_set_filename_reservation(task_core_environment, monkeypatch):
    from workflowy_mcp import server
    original = server._assign_collision_safe_md_filenames
    assigned = []
    def reset_each_leaf(*, leaves, **kwargs):
        for leaf in leaves:
            original(leaves=[leaf], **kwargs)
        assigned.extend(leaf["filename"] for leaf in leaves)
    monkeypatch.setattr(server, "_assign_collision_safe_md_filenames", reset_each_leaf)
    with pytest.raises(AssertionError):
        await test_bulk_ingress_collects_data_off_loop_with_original_front_order(task_core_environment, monkeypatch, "materialize", "visible")
    assert assigned == ["Leaf.md", "Leaf.md"], "filename probe did not remove only the in-set reservation"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["aliases-order", "missing", "depth64"])
async def test_bulk_cache_tree_keeps_original_alias_order_missing_and_depth(task_core_environment, monkeypatch, case):
    from workflowy_mcp import server
    loop_thread = threading.get_ident()
    rows = [{"id": "root", "nm": "compact-root", "no": "compact-note"},
            {"id": "later-priority", "parentId": "root", "name": "First in export", "priority": 99},
            {"id": "earlier-priority", "parent_id": "root", "nm": "Second in export", "no": "alias", "priority": 0},
            {"name": "ignored missing id"}]
    if case == "depth64":
        rows = [{"id": f"n{index}", "parent_id": f"n{index-1}" if index else None, "name": f"level{index}"}
                for index in range(67)]
    def getter():
        assert threading.get_ident() == loop_thread
        return rows
    task_core_environment.client._get_nodes_export_cache_nodes = getter
    original = server._build_carto_cache_subtree
    def projected(*args):
        assert threading.get_ident() != loop_thread, "bulk cache index/tree ran on loop"
        return original(*args)
    monkeypatch.setattr(server, "_build_carto_cache_subtree", projected)
    result = await server._build_cache_subtree_for_bulk_materialize("absent" if case == "missing" else "n0" if case == "depth64" else "root")
    if case == "missing":
        assert result is None
    elif case == "depth64":
        for index in range(65):
            assert result["id"] == f"n{index}"
            assert len(result["children"]) == int(index < 64)
            if index < 64:
                result = result["children"][0]
    else:
        assert result == {"id": "root", "name": "compact-root", "note": "compact-note", "children": [
            {"id": "later-priority", "name": "First in export", "note": "", "children": []},
            {"id": "earlier-priority", "name": "Second in export", "note": "alias", "children": []},
        ]}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["resolved", "missing", "failure"])
async def test_materialize_target_keeps_existing_soft_resolution_results(task_core_environment, monkeypatch, case):
    from workflowy_mcp import server
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    loop_thread = threading.get_ident()
    target_dir = task_core_environment.path if case == "resolved" else task_core_environment.path / "missing"
    def resolve(**kwargs):
        assert threading.get_ident() != loop_thread, "materialize target resolved on loop"
        assert current_operation_cache(required=False) is None and runtime.current_carto_job_context() is None
        assert kwargs == {"node_id": "leaf", "nodes_by_id": {"leaf": {"id": "leaf"}}, "default_folder": "fixture-default"}
        if case == "failure":
            raise ValueError("fixture target unavailable")
        return {"target_dir": str(target_dir), "is_temporary": True}
    monkeypatch.setattr(nexus, "resolve_new_markdown_dir_from_ancestors", resolve)
    control = runtime.CartoJobControl(task=asyncio.current_task())
    with runtime.carto_job_scope(job_context(task_core_environment.path, control)):
        value = await server.run_carto_cpu("fixture:target", server._carto_materialize_target, "leaf", {"leaf": {"id": "leaf"}}, "fixture-default")
    if case == "failure":
        assert value == {"error": "fixture target unavailable"}
    else:
        assert value == {"target": {"target_dir": str(target_dir), "is_temporary": True},
                         "target_dir": str(target_dir), "exists": case == "resolved"}
    assert control.cpu_failure is None and control.cpu_future is None


_C06_ENTRIES = (
    "export", "file-python", "file-markdown-dry", "source-claimed", "folder", "folder-dry",
    "beacon-python", "beacon-markdown", "markdown", "bulk-candidate", "bulk-artifacts",
    "bulk-visible", "reconcile", "reconcile-dry", "markdown-all", "markdown-visible",
    "materialize-all", "materialize-visible", "materialize-single", "terminal-normalize",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", _C06_ENTRIES)
async def test_all_proportional_phases_run_off_loop(claimed_task_environment, monkeypatch, entry):
    """C06 table entry points reuse the real, separately asserted phase witnesses.

    Each invoked witness checks worker thread/empty Contexts and literal data
    results; sends/publication/registry stay in its owning loop. This is not the
    serial large-repository responsiveness witness or a whole-job sleep mock.
    """
    env = claimed_task_environment
    if entry == "export":
        await test_cached_export_projection_is_off_loop_and_keeps_pinned_aliases(env, monkeypatch, "file")
    elif entry.startswith("file-"):
        await test_file_pipeline_runs_real_source_and_plan_under_one_loop_hold(
            env, monkeypatch, entry.endswith("dry"), "python" if entry == "file-python" else "markdown",
        )
    elif entry == "source-claimed":
        await test_source_preparation_keeps_hygiene_gates_order_and_owned_parse(env.path, monkeypatch, "py", False, "fixture-claim")
    elif entry.startswith("folder"):
        await test_folder_disk_pipeline_rechecks_ignore_and_owns_all_data_phases(env, monkeypatch, entry.endswith("dry"))
    elif entry.startswith("beacon-"):
        await test_disk_beacon_phase_uses_only_snapshot_data_off_loop(env, monkeypatch, entry.removeprefix("beacon-"))
    elif entry == "markdown":
        await test_markdown_pipeline_artifacts_are_joined_data_and_keep_phase_order(env, monkeypatch, "warnings")
    elif entry == "bulk-candidate":
        await test_bulk_candidate_decisions_are_data_work_with_original_loop_order(env, monkeypatch, "slow")
    elif entry == "bulk-artifacts":
        await test_bulk_job_artifacts_join_real_callback_counts_before_next_effect(env, monkeypatch, "success")
    elif entry == "bulk-visible":
        from workflowy_mcp import server
        from workflowy_mcp.coherence.operation_cache import current_operation_cache
        from workflowy_mcp.mutation_locks import MUTATION_LOCK_STATE
        loop_thread, seen = threading.get_ident(), []
        for module, name in ((server, "_build_bulk_visible_root_from_payload"),
                             (server, "_collect_bulk_visible_apply_groups"), (runtime, "_freeze_job_payload")):
            original = getattr(module, name)
            def data(*args, _original=original, _name=name, **kwargs):
                assert threading.get_ident() != loop_thread, "bulk visible collection ran on loop"
                assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
                assert MUTATION_LOCK_STATE.get() is None
                seen.append(_name)
                return _original(*args, **kwargs)
            monkeypatch.setattr(module, name, data)
        await test_claimed_public_launch_owns_task_grant_and_real_exit(env, monkeypatch, "bulk")
        assert set(seen) == {"_build_bulk_visible_root_from_payload", "_collect_bulk_visible_apply_groups", "_freeze_job_payload"}
    elif entry == "terminal-normalize":
        await test_terminal_result_normalization_is_data_work_and_preserves_precedence(env, monkeypatch, "returned-errors")
    elif entry == "materialize-single":
        await test_single_materialize_ingress_keeps_data_phases_and_real_preparation_order(env, monkeypatch, "persistent")
    elif entry.startswith("reconcile"):
        await test_reconcile_decisions_are_empty_context_data_work_even_when_unchanged(env.path, monkeypatch, entry.endswith("dry"))
    else:
        front, mode = entry.split("-")
        await test_bulk_ingress_collects_data_off_loop_with_original_front_order(env, monkeypatch, front, mode)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", _C06_ENTRIES)
async def test_c06_every_entry_rejects_inline_executor_neutralization(claimed_task_environment, monkeypatch, entry):
    from workflowy_mcp import server, markdown_generate_pipeline as pipeline
    from workflowy_mcp.client import api_client_nexus as nexus, api_client_etch as etch, workflowy_move_reconcile as driver
    reached = []
    async def inline(phase, function, *args, **kwargs):
        reached.append(phase)
        return function(*args, **kwargs)
    for module in (runtime, server, pipeline, nexus, etch, driver):
        monkeypatch.setattr(module, "run_carto_cpu", inline)
    with pytest.raises(Exception):
        await test_all_proportional_phases_run_off_loop(claimed_task_environment, monkeypatch, entry)
    assert reached, "inline probe did not reach a data phase"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["persistent", "temporary", "keep-temporary", "discard", "children", "missing-note", "empty", "resolver-error", "missing-dir"])
async def test_single_materialize_ingress_keeps_data_phases_and_real_preparation_order(task_core_environment, monkeypatch, case):
    from pathlib import Path
    from types import SimpleNamespace
    from coherence_harness import make_boundary_capable
    from n05_region_harness import node_uuid
    from workflowy_mcp import server, markdown_generate_pipeline as pipeline
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    from workflowy_mcp.mutation_locks import MUTATION_LOCK_STATE, mutation_region_locks, held_mutation_lock_regions
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    env, owner, loop_thread = task_core_environment, asyncio.current_task(), threading.get_ident()
    root, leaf = node_uuid("single-root"), node_uuid("single-leaf")
    rows = [{"id": root, "name": "Root", "note": f"Root: {env.path}"},
            {"id": leaf, "parentId": root, "name": "Leaf", "note": "# Heading\n\nBody\n"}]
    if case == "children":
        rows.append({"id": node_uuid("child"), "parent_id": leaf})
    if case == "missing-note":
        rows.pop()
    if case == "empty":
        rows[1]["note"] = "MD_PATH:\nPath: old.md\n---\n"
    client = make_boundary_capable(SimpleNamespace(), nodes=rows)
    monkeypatch.setattr(server, "get_client", lambda: client)
    events, replies = [], []
    def on_loop():
        assert threading.get_ident() == loop_thread and asyncio.current_task() is owner
        assert current_operation_cache(required=False) is not None
    def data():
        assert threading.get_ident() != loop_thread, "single materialize data phase ran on loop"
        assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
        assert MUTATION_LOCK_STATE.get() is None
    def getter():
        on_loop()
        events.append("get")
        return rows
    client._get_nodes_export_cache_nodes = getter
    original_cpu = server.run_carto_cpu
    async def trace_cpu(phase, function, *args, **kwargs):
        def work(*values, **options):
            data()
            events.append(phase)
            return function(*values, **options)
        return await original_cpu(phase, work, *args, **kwargs)
    monkeypatch.setattr(server, "run_carto_cpu", trace_cpu)
    def resolve(**kwargs):
        data()
        assert kwargs["nodes_by_id"][leaf]["parentId"] == root
        if case == "resolver-error":
            raise ValueError("fixture resolver error")
        return {"target_dir": str(env.path / "absent" if case == "missing-dir" else env.path),
                "is_temporary": case in {"temporary", "keep-temporary"}, "path_value_is_relative": True}
    monkeypatch.setattr(nexus, "resolve_new_markdown_dir_from_ancestors", resolve)
    monkeypatch.setattr(nexus, "find_filebase_from_ancestors", lambda **kwargs: "Base")
    # H's existing filesystem collision policy, not a new filename reservation.
    (env.path / "Base_-_Leaf.md").write_text("existing", encoding="utf-8")
    actual_file = env.path / "Base_-_Leaf_2.md"
    expected_regime = "temp_override" if case in {"temporary", "keep-temporary", "discard"} else "persistent_new"
    async def start(root_uuid, mode, extra=None, **kwargs):
        on_loop()
        events.append("launch")
        assert root_uuid == leaf and mode == "file" and kwargs["source_regime"] == expected_regime
        assert not actual_file.exists(), "pre-admission plan wrote the source"
        payload = kwargs["frozen_payload"]
        assert payload["note_to_write"] == "# Heading\n\nBody\n" and Path(payload["abs_path"]) == actual_file
        if case == "persistent":
            assert payload["new_note"] == "Path: Base_-_Leaf_2.md" and extra is None
        elif case == "keep-temporary":
            assert payload["new_note"] == f"MD_PATH:\nPath: {actual_file}\n{nexus._F12P4_DUMMY_BLOCK_COMMENT}\n---"
            assert extra == {"source_path_override": str(actual_file), "delete_temp_file_after": False}
        else:
            assert payload["new_note"] == "" and extra == {"source_path_override": str(actual_file), "delete_temp_file_after": True}
        control = runtime.CartoJobControl(task=owner)
        with runtime.carto_job_scope(job_context(env.path / "single-job", control)):
            async with mutation_region_locks(client=client, operation_label="single-prep",
                region_factory=lambda proof: (build_region_subtree(leaf, cache_by_id=proof.nodes_by_id),),
                hold_mode="long_lived", expected_duration_seconds=None):
                result = await kwargs["preparation"](payload, {})
                assert kwargs["preparation_gate"](result) == {"proceed": True}
                assert control.may_have_workflowy_effects and control.cpu_future is None
        return {"success": True, "job_id": "single-fixture", "detached": False}
    async def update(node_id, request):
        on_loop()
        assert held_mutation_lock_regions() and node_id == leaf and actual_file.read_text() == "# Heading\n\nBody\n"
        events.append("update")
    async def patch(values):
        on_loop()
        assert held_mutation_lock_regions() and values[0]["id"] == leaf and values[0]["note"]
        events.append("patch")
        return {"success": True}
    def dirty(ids):
        on_loop()
        assert held_mutation_lock_regions() and ids == [leaf]
        events.append("dirty")
    monkeypatch.setattr(server, "_start_carto_refresh_job", start)
    client.update_node, client.patch_nodes_export_snapshot_on_disk, client._mark_nodes_export_dirty = update, patch, dirty
    original_write = pipeline._write_generated_markdown
    def write(*args, **kwargs):
        data()
        events.append("write")
        return original_write(*args, **kwargs)
    monkeypatch.setattr(pipeline, "_write_generated_markdown", write)
    class Socket:
        async def send(self, text):
            on_loop()
            events.append("reply")
            replies.append(json.loads(text))
    await server._handle_materialize_markdown_file({"node_id": leaf,
        "keep_generated_file": case == "keep-temporary", "discard_generated_file": case == "discard"}, Socket())
    assert len(replies) == 1 and replies[0]["node_id"] == leaf
    errors = {"children": "Refusing to materialize a node that has children.",
              "missing-note": "node_note was omitted and could not be resolved once before digest.",
              "empty": "No Markdown body remains after stripping Cartographer metadata.",
              "resolver-error": "fixture resolver error", "missing-dir": f"Resolved target directory does not exist: {str(env.path / 'absent')!r}"}
    if case in errors:
        assert replies[0] == {"action": "materialize_markdown_file_result", "success": False, "node_id": leaf, "error": errors[case]}
        assert events == ["get", "materialize:plan", "materialize:reply", "reply"] and not actual_file.exists()
    else:
        assert replies[0] == {"action": "materialize_markdown_file_result", "node_id": leaf, "file_path": str(actual_file),
                              "is_temporary": case != "persistent", "filebase": "Base", "success": True,
                              "job_id": "single-fixture", "detached": False}
        assert events == ["get", "materialize:plan", "materialize:digest", "launch", "materialize-write", "write", "update"] + (
            ["patch"] if case in {"persistent", "keep-temporary"} else []) + ["dirty", "materialize:reply", "reply"]
    assert rows[0]["name"] == "Root" and (env.path / "Base_-_Leaf.md").read_text() == "existing"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["materialize:plan", "materialize:digest", "materialize:reply"])
async def test_single_materialize_witness_rejects_each_inline_phase(task_core_environment, monkeypatch, phase):
    from workflowy_mcp import server
    original, reached = server.run_carto_cpu, []
    async def inline(selected, function, *args, **kwargs):
        if selected == phase:
            reached.append(selected)
            return function(*args, **kwargs)
        return await original(selected, function, *args, **kwargs)
    monkeypatch.setattr(server, "run_carto_cpu", inline)
    with pytest.raises(AssertionError, match="single materialize data phase ran on loop"):
        await test_single_materialize_ingress_keeps_data_phases_and_real_preparation_order(task_core_environment, monkeypatch, "persistent")
    assert reached == [phase]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["success", "returned-errors", "typed-return", "raised-typed", "raised", "warnings", "cancel", "restart", "malformed"])
async def test_terminal_result_normalization_is_data_work_and_preserves_precedence(task_core_environment, monkeypatch, case):
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    from workflowy_mcp.mutation_locks import mutation_region_locks, MUTATION_LOCK_STATE, held_mutation_lock_regions
    from workflowy_mcp.carto_job_governance import AdmissionRefusal
    env, owner, loop_thread = task_core_environment, asyncio.current_task(), threading.get_ident()
    spec = _region_spec(env.path)
    control = runtime.CartoJobControl(task=owner, phase="fixture-phase", finalizing=True, may_have_workflowy_effects=True)
    outcome, result = {}, {"success": True, "nodes_updated": 42}
    errors = [f"problem-{index:04}" for index in range(4096)]
    if case == "returned-errors":
        result["refresh_errors"] = errors
    elif case == "typed-return":
        result.update(success=False, error="typed base", error_kind="coherence_barrier_active", error_extra={"coherence_refresh_required": True})
    elif case == "raised-typed":
        outcome["exception"] = AdmissionRefusal("overlap_conflict", "typed claim refusal", retry_after=60)
    elif case == "raised":
        outcome["exception"] = RuntimeError("fixture exception")
    elif case == "warnings":
        result["warnings"] = [{"message": "known nonfatal"}]
    elif case == "cancel":
        control.user_observed = True
        result.update(success=False, error="work failed before stop")
    elif case == "restart":
        control.restart_state["observed"] = True
    elif case == "malformed":
        result["result_summary"] = "bad summary"
    outcome["result"] = result
    original, seen = env.server._carto_terminal_payload_data, []
    def pure(value):
        if isinstance(value, dict):
            for key, item in value.items():
                pure(key)
                pure(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                pure(item)
        else:
            assert value is None or type(value) in (str, bool, int, float), "normalizer received a live capability"
    def data(metadata, values, outcome_values):
        assert threading.get_ident() != loop_thread, "terminal result formatted on loop"
        assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
        assert MUTATION_LOCK_STATE.get() is None
        pure((metadata, values, outcome_values))
        seen.append(True)
        return original(metadata, values, outcome_values)
    monkeypatch.setattr(env.server, "_carto_terminal_payload_data", data)
    context = job_context(env.path, control)
    async with cache_operation_boundary(env.client, "terminal-data") as pin:
        async with mutation_region_locks(client=env.client, operation_label="terminal-data",
            region_factory=lambda proof: env.server._carto_job_regions(spec, proof), hold_mode="long_lived", expected_duration_seconds=None):
            with runtime.carto_job_scope(context):
                if case == "malformed":
                    with pytest.raises(AttributeError):
                        await env.server._carto_task_terminal_payload(spec, control, outcome)
                    assert control.cpu_failure[0] == "terminal-normalize"
                else:
                    payload = await env.server._carto_task_terminal_payload(spec, control, outcome)
                    expected = ("cancelled" if case in {"cancel", "restart"} else "completed_with_warnings" if case == "warnings"
                                else "completed" if case == "success" else "failed")
                    assert payload["status"] == expected and payload["cache_refresh_required"] is True
                    assert payload["result_summary"]["nodes_updated"] == 42
                    if case == "returned-errors":
                        assert payload["error"] == f"Cartographer job {spec.job_id} failed during fixture-phase: RuntimeError: {errors!r}. The daemon is still serving; inspect the job result before retrying."
                    elif case == "typed-return":
                        assert payload["error_kind"] == "coherence_barrier_active" and payload["error"] == "typed base"
                        assert payload["error_extra"]["coherence_refresh_required"] is True
                    elif case == "raised-typed":
                        assert payload["error_kind"] == "overlap_conflict" and payload["error"] == "typed claim refusal"
                        assert payload["error_extra"]["retry_after"] == 60
                    elif case == "raised":
                        assert payload["error_kind"] == "carto_job_exception" and "RuntimeError: fixture exception." in payload["error"]
                    if expected == "failed":
                        assert payload["error_extra"]["phase"] == "fixture-phase" and payload["error_extra"]["job_id"] == spec.job_id
                    if case == "restart":
                        assert payload["result_summary"]["message"] == "Stopped at a safe job boundary for daemon restart; completed work is retained. Re-run the job if needed."
                assert current_operation_cache() is pin and held_mutation_lock_regions()
                assert runtime.current_carto_job_context() is context and control.cpu_future is None
    assert seen == [True] and runtime.current_carto_job_context() is None


def test_runtime_spec_freezes_intent_without_copying_view(scrubbed_runtime):
    from n05_region_harness import LookupOnlyMap
    selection = {"snapshot_lineage": {"epoch_id": "selected"}}
    payload = {"name": "", "note": "", "children": [{"name": "frozen"}]}
    visible = {"root": {"id": "root"}, "children": [{"id": "child"}]}
    borrowed = LookupOnlyMap({"root": {"id": "root"}})
    spec = runtime.CartoJobSpec(
        job_id="n07-test", job_file=scrubbed_runtime / "job.json", log_file=None,
        job_type="CARTO_REFRESH", mode="file", root_uuid="root", claim_id="claim",
        account_scope_id="0" * 32, selection_metadata=selection,
        selection_nodes_by_id=borrowed, preparation_payload=payload,
        visible_tree_payload=visible,
    )
    selection["snapshot_lineage"]["epoch_id"] = "mutated"
    payload["children"][0]["name"] = "mutated"
    visible["children"][0]["id"] = "mutated"
    assert spec.selection_metadata["snapshot_lineage"]["epoch_id"] == "selected"
    assert spec.preparation_payload["name"] == spec.preparation_payload["note"] == ""
    assert spec.preparation_payload["children"][0]["name"] == "frozen"
    assert spec.visible_tree_payload["children"][0]["id"] == "child"
    assert spec.selection_nodes_by_id is borrowed and borrowed.lookups == 0
    with pytest.raises(TypeError):
        spec.preparation_payload["name"] = "not frozen"
    with pytest.raises(FrozenInstanceError):
        spec.root_uuid = "not frozen"


@pytest.mark.asyncio
async def test_runtime_scope_is_task_local_and_resets(scrubbed_runtime):
    ready, release = asyncio.Event(), asyncio.Event()
    first = job_context(scrubbed_runtime / "first", runtime.CartoJobControl())
    second = job_context(scrubbed_runtime / "second", runtime.CartoJobControl())
    assert runtime.current_carto_job_context() is None
    async def own(context, waits):
        assert runtime.current_carto_job_context() is None
        context.job_control.task = asyncio.current_task()
        with runtime.carto_job_scope(context):
            assert runtime.current_carto_job_context() is context
            context.progress["own"] = str(context.job_dir)
            context.relpath_memo["file"] = str(context.job_dir)
            if waits:
                ready.set()
                await release.wait()
            else:
                with pytest.raises(ValueError):
                    with runtime.carto_job_scope(first):
                        assert runtime.current_carto_job_context() is first
                        raise ValueError("scope reset")
                assert runtime.current_carto_job_context() is context
            assert runtime.current_carto_job_context() is context
        assert runtime.current_carto_job_context() is None
    task = asyncio.create_task(own(first, True))
    await ready.wait()
    try:
        await own(second, False)
        assert first.progress != second.progress
        assert first.relpath_memo != second.relpath_memo
    finally:
        release.set()
        await task
    assert runtime.current_carto_job_context() is None


@pytest.mark.asyncio
async def test_runtime_cpu_uses_empty_context_and_loop_owned_result(scrubbed_runtime):
    from workflowy_mcp import mutation_locks
    from workflowy_mcp.coherence import operation_cache
    marker = contextvars.ContextVar("n07-test-marker", default="empty")
    marker_token = marker.set("caller")
    cache_token = operation_cache._OPERATION_CACHE_SNAPSHOT.set(object())
    lock_token = mutation_locks.MUTATION_LOCK_STATE.set(object())
    loop_thread = threading.get_ident()
    control = runtime.CartoJobControl(task=asyncio.current_task())
    context = job_context(scrubbed_runtime, control)
    def work(values, *, add):
        return (sum(values) + add, threading.get_ident(), marker.get(),
                runtime.current_carto_job_context(),
                operation_cache.current_operation_cache(required=False),
                mutation_locks.MUTATION_LOCK_STATE.get())
    try:
        with runtime.carto_job_scope(context):
            result = await runtime.run_carto_cpu("planning", work, (1, 2, 3), add=4)
            assert result == (10, result[1], "empty", None, None, None)
            assert result[1] != loop_thread, "CPU helper ran on the event loop"
            assert control.phase == "planning" and control.cpu_future is None
            assert runtime.current_carto_job_context() is context
            assert marker.get() == "caller"
    finally:
        mutation_locks.MUTATION_LOCK_STATE.reset(lock_token)
        operation_cache._OPERATION_CACHE_SNAPSHOT.reset(cache_token)
        marker.reset(marker_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("exception_type", [RuntimeError, SystemExit, KeyboardInterrupt])
async def test_runtime_cpu_latches_failure_before_broad_catch(scrubbed_runtime, exception_type):
    control = runtime.CartoJobControl(task=asyncio.current_task())
    failure = exception_type("fixture parse failure")
    def fail():
        raise failure
    with runtime.carto_job_scope(job_context(scrubbed_runtime, control)):
        try:
            await runtime.run_carto_cpu("parse", fail)
        except BaseException as exc:
            assert exc is failure
        else:
            pytest.fail("data failure disappeared")
        assert control.cpu_failure == ("parse", failure)
        assert control.cpu_future is None
        assert await runtime.run_carto_cpu("subsequent-data", sum, (2, 3)) == 5
        assert control.cpu_failure == ("parse", failure), "later work erased the failure latch"


@pytest.mark.parametrize("regime,requested,override,expected", [
    ("standard", "symbol", None, "file"),
    ("standard", "file", None, "file"),
    ("standard", "folder", None, "folder"),
    ("standard", "vault", None, "vault"),
    ("standard", "inner-symbol", None, "inner-file"),
    ("standard", "alias-note", None, "alias-note"),
    ("standard", "alias-parent", None, "file"),
    ("standard", "nontext-note", None, "file"),
    ("standard", "symbol", "/n07-fixture/temporary.md", "symbol"),
    ("standard", "symbol", "   ", "file"),
    ("temp_override", "symbol", None, "symbol"),
    ("persistent_new", "symbol", None, "symbol"),
    ("standard", "missing", None, "missing"),
    ("standard", "broken", None, "broken"),
    ("standard", "cycle-a", None, "cycle-a"),
])
def test_refresh_root_resolver_preserves_bounded_existing_rules(
    scrubbed_runtime, monkeypatch, regime, requested, override, expected,
):
    """Effective FILE root selection preserves H's pure, keyed ancestor rules."""
    from types import MappingProxyType
    from n05_region_harness import LookupOnlyMap, node_uuid
    from workflowy_mcp.client import api_client_nexus as nexus

    parents = {"vault": None, "folder": "vault", "file": "folder", "scope": "file",
               "symbol": "scope", "inner-file": "file", "inner-symbol": "inner-file",
               "alias-note": "file", "alias-parent": "file", "nontext-note": "file",
               "broken": "missing", "cycle-a": "cycle-b", "cycle-b": "cycle-a"}
    rows = {node_uuid(name): {"id": node_uuid(name), "note": "",
                             "parent_id": node_uuid(parent) if parent else None}
            for name, parent in parents.items()}
    for name, note in {"vault": "Root: /n07-fixture", "folder": "Path: pkg",
                       "file": "Path: module.py", "inner-file": "Path: nested.md"}.items():
        rows[node_uuid(name)]["note"] = note
    rows[node_uuid("alias-note")]["no"] = "Path: compact.py"
    rows[node_uuid("alias-parent")]["parentId"] = rows[node_uuid("alias-parent")].pop("parent_id")
    rows[node_uuid("nontext-note")]["note"] = 7
    borrowed = LookupOnlyMap(MappingProxyType(rows))
    def forbidden(*args, **kwargs):
        raise AssertionError("pure root proof performed disk, client or path-resolution work")
    with monkeypatch.context() as patch:
        patch.setattr(nexus.os.path, "exists", forbidden)
        patch.setattr(nexus.os.path, "isdir", forbidden)
        patch.setattr(nexus, "resolve_cartographer_path_from_node", forbidden)
        patch.setattr(nexus, "export_nodes_impl", forbidden)
        actual = nexus._resolve_carto_refresh_root(
            borrowed, node_uuid(requested), source_regime=regime, source_path_override=override,
        )
    assert actual == node_uuid(expected), "refresh selected the wrong effective graph root"
    assert borrowed.lookups <= 4, "root proof was not bounded to the ancestor chain"


def _region_spec(path, **changes):
    from n05_region_harness import node_uuid
    fields = dict(job_id="n07-region", job_file=path / "job.json", log_file=None,
                  job_type="CARTO_REFRESH", mode="file", root_uuid=node_uuid("symbol"),
                  claim_id="d" * 32, account_scope_id="a" * 32)
    return runtime.CartoJobSpec(**(fields | changes))


@pytest.mark.parametrize("job_type,mode,regime,override,requested,expected", [
    ("CARTO_REFRESH", "file", "standard", None, "symbol", "file"),
    ("CARTO_REFRESH", "file", "standard", None, "file", "file"),
    ("CARTO_REFRESH", "folder", "standard", None, "folder", "folder"),
    ("CARTO_REFRESH", "file", "persistent_new", None, "symbol", "symbol"),
    ("CARTO_REFRESH", "file", "temp_override", "/n07-fixture/temp.md", "symbol", "symbol"),
    ("CARTO_MARKDOWN_GENERATE", "file", "standard", None, "file", "file"),
])
def test_job_region_factory_uses_actual_family_root(
    scrubbed_runtime, monkeypatch, job_type, mode, regime, override, requested, expected,
):
    from types import SimpleNamespace
    from n05_region_harness import LookupOnlyMap, node_uuid
    from n07_job_harness import job_region_geometry
    from workflowy_mcp import server

    proof = SimpleNamespace(nodes_by_id=LookupOnlyMap(job_region_geometry()))
    spec = _region_spec(scrubbed_runtime, job_type=job_type, mode=mode,
                        source_regime=regime, source_path_override=override,
                        root_uuid=node_uuid(requested))
    def forbidden(*args, **kwargs):
        raise AssertionError("job geometry observed a client, filesystem or ownership")
    monkeypatch.setattr(server, "get_client", forbidden)
    monkeypatch.setattr(server, "registry_held_regions", forbidden)
    result = server._carto_job_regions(spec, proof)
    assert [(region.kind, region.key) for region in result] == [("subtree", node_uuid(expected))], (
        "job grant selected the requested AST instead of its actual execution root"
    )
    assert all(node_uuid("vault") == region.ancestor_chain[-1] for region in result)


def test_bulk_region_factory_reproves_frozen_membership_not_gui_containment(scrubbed_runtime):
    from types import SimpleNamespace
    from n05_region_harness import LookupOnlyMap, node_uuid
    from n07_job_harness import job_region_geometry
    from workflowy_mcp import server

    # The GUI puts all candidates in FILE, but one is in OTHER-FILE and one
    # is in an entirely unmapped subtree. Expected roots come from this literal
    # geometry, never from the production resolver/intersection implementation.
    group = {node_uuid("file"): {"file_node": {"id": node_uuid("file")}, "nodes": [
        {"id": node_uuid("symbol")}, {"id": node_uuid("moved-symbol")},
        {"id": node_uuid("unmapped-child")},
    ]}}
    spec = _region_spec(scrubbed_runtime, job_type="CARTO_BULK_APPLY", bulk_groups=group)
    group[node_uuid("file")]["nodes"].clear()
    rows = job_region_geometry()
    proof = SimpleNamespace(nodes_by_id=LookupOnlyMap(rows))
    result = server._carto_job_regions(spec, proof)
    assert {(r.kind, r.key) for r in result} == {
        ("subtree", node_uuid("file")), ("subtree", node_uuid("other-file")),
        ("exact_node", node_uuid("unmapped-child")),
    }, "bulk grant trusted GUI nesting or omitted an uncovered cache-patch target"
    assert all(r.key != node_uuid("vault") for r in result), "bulk grant widened to the account"

    # Membership remains frozen, but containment is recomputed on each proof.
    moved = {key: dict(row) for key, row in rows.items()}
    moved[node_uuid("moved-symbol")]["parent_id"] = node_uuid("file")
    moved[node_uuid("unmapped-child")]["parent_id"] = node_uuid("other-file")
    current = server._carto_job_regions(spec, SimpleNamespace(nodes_by_id=LookupOnlyMap(moved)))
    assert {(r.kind, r.key) for r in current} == {
        ("subtree", node_uuid("file")), ("subtree", node_uuid("other-file")),
    }, "bulk factory reused an older proof's containment or exact additions"


def test_bulk_region_factory_empty_membership_has_no_account_hold(scrubbed_runtime):
    from types import SimpleNamespace
    from n05_region_harness import LookupOnlyMap
    from workflowy_mcp import server
    proof = SimpleNamespace(nodes_by_id=LookupOnlyMap({}))
    assert server._carto_job_regions(
        _region_spec(scrubbed_runtime, job_type="CARTO_BULK_APPLY"), proof,
    ) == ()
    assert proof.nodes_by_id.lookups == 0


@pytest.mark.parametrize("holder_kind,associated", [
    ("long_lived", True), ("long_lived", False), ("carto_claim", True),
])
def test_job_refusal_diagnostics_preserve_opaque_holder(scrubbed_runtime, monkeypatch, holder_kind, associated):
    from workflowy_mcp import server
    owner = "opaque-operation-id-not-a-job-id"
    monkeypatch.setattr(server, "_carto_operation_jobs", {owner: "fixture-job"} if associated else {})
    exc = server.MutationSerializationConflictError(
        "unchanged base message", retry_after=60.0, contended_keys=["fixture"],
        holder_kind=holder_kind, holder_id=owner, reason="unchanged-reason",
    )
    extra = server._mutation_refusal_extra(exc)
    assert extra["holder_id"] == owner and str(exc) == "unchanged base message"
    assert extra["retry_after"] == 60.0 and extra["reason"] == "unchanged-reason"
    if holder_kind == "long_lived" and associated:
        assert extra["holder_job_id"] == "fixture-job"
        assert extra["holder_message"] == (
            "Held by Cartographer job fixture-job; check system.job.status before retrying."
        )
    else:
        assert "holder_job_id" not in extra and "holder_message" not in extra


@pytest.mark.parametrize("advice", [120.0, 60.0])
def test_job_overlap_advice_is_positive_before_typed_consult(scrubbed_runtime, monkeypatch, advice):
    from types import SimpleNamespace
    from n05_region_harness import node_uuid
    from n07_job_harness import job_region_geometry
    from workflowy_mcp import carto_job_governance as gov, config, mutation_locks, server
    from workflowy_mcp.models.config import ServerConfig

    if _probe("CUT2B_NEUTRALIZE_ADMISSION_ORDER"):
        # Q8 mutant: the one typed registry consult is moved before the
        # active-file-overlap refusal.
        real_admit = gov.admit_carto_job_locked
        def consult_first(**kwargs):
            mutation_locks.registry_held_regions()
            return real_admit(**kwargs)
        monkeypatch.setattr(gov, "admit_carto_job_locked", consult_first)

    assert ServerConfig.model_fields["mutation_lock_long_lived_retry_after_seconds"].default == 60.0
    monkeypatch.setattr(config, "get_server_config", lambda: SimpleNamespace(
        mutation_lock_long_lived_retry_after_seconds=advice,
    ))
    forwarding = server._carto_admission_coherence_kwargs(
        root_uuid=node_uuid("file"), mode="file", source_regime="standard",
    )
    assert forwarding == {"mutation_lease_retry_after_seconds": advice}
    footprint = gov.compute_footprint(None, node_uuid("file"), job_region_geometry(), kind="file")
    fields = dict(job_type="CARTO_REFRESH", account_scope_id="a" * 32,
                  footprint=footprint, launcher="system", parent_job_id=None,
                  prepared=True, jobs_root=scrubbed_runtime, **forwarding)
    first = gov.admit_carto_job_locked(
        job_file=scrubbed_runtime / "first.json", job_payload={"id": "first", "type": "CARTO_REFRESH"},
        dedupe_key="b" * 64, **fields,
    )
    assert not first.get("deduplicated")
    assert first["claim_path"] is None
    assert {"record_kind", "claim_id", "job_id", "job_file", "job_type", "mode",
            "dedupe_key", "launcher", "finalized", "root_uuid", "path", "ancestor_chain"} <= set(first["claim"])
    assert "pid" not in first["claim"] and "execution_owner" not in first["claim"]
    holds = mutation_locks._governance_holds(scrubbed_runtime)
    assert len(holds) == 1 and holds[0]["claim_id"] == first["claim"]["claim_id"]
    assert holds[0]["finalized"] is False and holds[0]["job_file"] == str(scrubbed_runtime / "first.json")
    def forbidden():
        raise AssertionError("claim conflict made an additional typed-registry observation")
    monkeypatch.setattr(mutation_locks, "registry_held_regions", forbidden)
    with pytest.raises(gov.AdmissionRefusal) as caught:
        gov.admit_carto_job_locked(
            job_file=scrubbed_runtime / "second.json",
            job_payload={"id": "second", "type": "CARTO_REFRESH", "preparation_input_digest": "changed"},
            dedupe_key="c" * 64, **fields,
        )
    result = caught.value.to_result()
    assert result["error_kind"] == "overlap_conflict"
    assert result["retry_after"] == advice > 0, "claim conflict lacked configured positive retry advice"
    assert result["error"].startswith("Active owner 'first' overlaps '")
    assert "latest name/note changes were NOT synchronized" in result["error"]
    assert result["error"].endswith(
        f" Retry after about {advice:g}s; check system.job.status because the job may still be running."
    )


@pytest.mark.asyncio
async def test_task_core_registration_boundary_grant_and_exit(task_core_environment):
    from n05_region_harness import node_uuid
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, current_operation_cache
    env = task_core_environment
    server = env.server
    send_entered, release = asyncio.Event(), asyncio.Event()
    captured = []

    async def freshness():
        assert current_operation_cache(required=False) is None, "B0 inherited the launching pin"
        env.trace("B0")
        return {"freshness_disposition": "performed", "cache_by_id": {}}
    async def prepare(payload, selection):
        assert payload["name"] == payload["note"] == "", "frozen empty bytes were lost"
        assert payload["_claim_id"] == spec.claim_id == "d" * 32
        env.trace("prepare")
        return {"prepared": True}
    async def refresh(**kwargs):
        env.trace("send")
        assert kwargs["file_node_id"] == node_uuid("symbol")
        assert kwargs["force"] is True
        send_entered.set()
        await release.wait()
        env.trace("send-complete")
        return {"success": True, "nodes_updated": 1}
    env.client.refresh_file_node_beacons = refresh
    spec = _region_spec(env.path, force=True, preparation=prepare,
                        preparation_payload={"name": "", "note": ""}, freshness_callback=freshness)
    async with cache_operation_boundary(env.client, "launch-caller") as caller:
        ack = await server._schedule_carto_job(spec)
        assert ack["success"] and ack["detached"] is False
        task = server._carto_inprocess_tasks[spec.job_id]
        control = server._carto_job_controls[spec.job_id]
        captured.append(control)
        assert control.task is task and not task.done()
        assert server._inprocess_restart_stops[task] is control.restart_state
        assert env.controller._job_stops[spec.job_id][1] is task
        assert not env.events, "job body ran before synchronous registration/ack"
        assert current_operation_cache() is caller
    await asyncio.wait_for(send_entered.wait(), 10)
    try:
        assert env.controller._busy()
        assert server._carto_operation_jobs[env.events[-1][1]] == spec.job_id
        assert all(r.key == node_uuid("file") for r in env.events[-1][2]), "AST sibling hold missing"
    finally:
        release.set()
        await task
        await asyncio.sleep(0)  # let actual N06 and lookup done callbacks finish
    assert [event[0] for event in env.events] == ["B0", "barrier", "barrier", "prepare", "send", "send-complete", "finalize"]
    body = env.events[2:]
    assert len({event[1] for event in body}) == 1, "job repinned between preparation/send/finalizer"
    assert all(event[2] and event[3] is task for event in body), "grant dropped before finalizer"
    assert current_operation_cache(required=False) is None
    assert runtime.current_carto_job_context() is None
    assert not server._carto_inprocess_tasks and not server._carto_job_controls
    assert not server._carto_operation_jobs and not server._inprocess_restart_stops
    assert not env.controller._busy()
    assert control.terminal_result["status"] == "completed" and control.cpu_future is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raise", "system-exit", "returned", "collected", "latched", "warnings"])
async def test_task_core_normalizes_body_outcomes_inside_hold(task_core_environment, failure):
    env = task_core_environment
    async def refresh(**kwargs):
        env.trace("work")
        if failure == "raise":
            raise RuntimeError("fixture work error")
        if failure == "system-exit":
            raise SystemExit("fixture job-only exit")
        if failure == "returned":
            return {"success": False, "error": "returned failure"}
        if failure == "collected":
            return {"success": True, "refresh_errors": ["per-file operational failure"]}
        if failure == "latched":
            def bad_plan():
                raise RuntimeError("swallowed planner failure")
            try:
                await runtime.run_carto_cpu("plan", bad_plan)
            except RuntimeError:
                pass
            return {"success": True}
        return {"success": True, "warnings": [{"message": "nonfatal parse sentinel"}]}
    env.client.refresh_file_node_beacons = refresh
    spec = _region_spec(env.path)
    errors = []
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        ack = await env.server._schedule_carto_job(spec)
        assert ack["success"]
        task = env.server._carto_inprocess_tasks[spec.job_id]
        control = env.server._carto_job_controls[spec.job_id]
        await task
        await asyncio.sleep(0)
        terminal = control.terminal_result
        assert terminal["status"] == ("completed_with_warnings" if failure == "warnings" else "failed")
        assert terminal["cache_refresh_required"] is True
        assert [e[0] for e in env.events][-1] == "finalize" and env.events[-1][2]
        if failure != "warnings":
            assert terminal["error_kind"] == "carto_job_exception"
            assert terminal["error_extra"]["job_id"] == spec.job_id
            assert terminal["error_extra"]["may_have_workflowy_effects"] is True
            assert "The daemon is still serving; inspect the job result before retrying." in terminal["error"]
        assert not errors, "job exception escaped into the daemon event loop"
        assert not env.controller._busy()
    finally:
        loop.set_exception_handler(old_handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["spawn", "registration"])
async def test_task_core_scheduling_failure_has_one_effect_free_exit(task_core_environment, monkeypatch, where):
    env = task_core_environment
    def broken(*args, **kwargs):
        raise RuntimeError("fixture scheduler failure")
    monkeypatch.setattr(env.controller, "spawn_admitted_child" if where == "spawn" else "register_job_boundary_stop", broken)
    spec = _region_spec(env.path)
    result = await env.server._schedule_carto_job(spec)
    await asyncio.sleep(0)
    assert result["success"] is False and result["error_kind"] == "scheduling_failed"
    assert [event[0] for event in env.events] == ["finalize"]
    assert not env.events[0][2], "failed setup acquired a grant or executed work"
    assert not env.controller._busy()
    assert not env.server._carto_inprocess_tasks and not env.server._carto_job_controls


@pytest.mark.asyncio
async def test_task_core_finalization_failure_retains_primary_and_releases_hold(task_core_environment, monkeypatch):
    """A terminal-status write failure leaves the primary error and one exit door.

    The artifact-cleanup arm is retired with its subject: finalization is memory
    only, so the only remaining failure is the presentation write, which must not
    block the exactly-once finalizer or the outer release.
    """
    env = task_core_environment
    async def refresh(**kwargs):
        env.trace("work")
        raise RuntimeError("primary fixture error")
    env.client.refresh_file_node_beacons = refresh
    write_status = env.server.merge_write_carto_job_status
    monkeypatch.setattr(env.server, "merge_write_carto_job_status",
                        lambda path, payload: False if payload.get("status") == "failed"
                        else write_status(path, payload))
    if _probe("CUT2B_NEUTRALIZE_PRIMARY_ERROR_MASK"):
        # A secondary status-write failure replaces the primary work error.
        monkeypatch.setattr(env.server, "_finalize_carto_task", _source_copy(
            env.server, "_finalize_carto_task", [(
                'payload["error"] = payload.get("error") or str(status_error)',
                'payload["error"] = str(status_error)',
            )]))
    spec = _region_spec(env.path)
    await env.server._schedule_carto_job(spec)
    control = env.server._carto_job_controls[spec.job_id]
    await control.task
    await asyncio.sleep(0)
    terminal = control.terminal_result
    assert terminal["status"] == "failed" and "primary fixture error" in terminal["error"]
    assert [event[0] for event in env.events].count("finalize") == 1
    assert env.events[-1][2], "finalization moved outside the whole-life grant"
    assert "status_write_error" in terminal
    assert not env.controller._busy() and not env.server._carto_operation_jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_task_core_preparation_noop_gate_never_dispatches(task_core_environment, status):
    env = task_core_environment
    def prepare(payload, selection):
        env.trace("prepare")
        return {"frozen": payload["note"]}
    spec = _region_spec(env.path, preparation=prepare, preparation_payload={"note": ""},
                        preparation_gate=lambda value: {"proceed": False, "status": status,
                            "error": None if status == "completed" else "preparation refused",
                            "result_summary": {"preparation": value}})
    await env.server._schedule_carto_job(spec)
    control = env.server._carto_job_controls[spec.job_id]
    await control.task
    await asyncio.sleep(0)
    assert control.terminal_result["status"] == status
    assert control.terminal_result["cache_refresh_required"] is False
    assert [event[0] for event in env.events] == ["barrier", "barrier", "prepare", "finalize"]
    assert env.events[-1][2], "preparation gate escaped the held exit door"


@pytest.mark.asyncio
async def test_task_core_user_stop_finalizes_without_reentering_checkpoint(task_core_environment):
    env = task_core_environment
    async def refresh(**kwargs):
        env.trace("work")
        context = runtime.current_carto_job_context()
        context.job_control.request_user_cancel()
        return {"success": True}
    env.client.refresh_file_node_beacons = refresh
    spec = _region_spec(env.path)
    await env.server._schedule_carto_job(spec)
    control = env.server._carto_job_controls[spec.job_id]
    await control.task
    await asyncio.sleep(0)
    assert control.user_observed and control.finalizing and control.cpu_future is None
    assert control.terminal_result["status"] == "cancelled"
    assert [event[0] for event in env.events][-1] == "cancel-finalize"
    assert env.events[-1][2] and not env.controller._busy()


@pytest.mark.asyncio
async def test_task_core_malformed_result_still_finalizes_exactly_once(task_core_environment):
    env = task_core_environment
    async def refresh(**kwargs):
        return {"success": True, "result_summary": ["invalid result summary shape"]}
    env.client.refresh_file_node_beacons = refresh
    spec = _region_spec(env.path)
    await env.server._schedule_carto_job(spec)
    control = env.server._carto_job_controls[spec.job_id]
    await control.task
    await asyncio.sleep(0)
    assert control.terminal_result["status"] == "failed"
    assert control.terminal_result["error_kind"] == "carto_job_exception"
    assert [event[0] for event in env.events].count("finalize") == 1
    assert env.events[-1][2] and not env.controller._busy()


@pytest.mark.asyncio
async def test_live_basis_borrows_one_publisher_and_truthful_base_diagnostics(scrubbed_runtime, monkeypatch):
    from types import SimpleNamespace
    from coherence_harness import make_coherence_root
    from workflowy_mcp import server, carto_job_governance as gov
    from workflowy_mcp.coherence.cache_view import CacheView, CacheViewPublisher
    from workflowy_mcp.coherence.schema import read_manifest, manifest_with_updates, write_manifest
    from workflowy_mcp.coherence import snapshot_store

    paths = make_coherence_root(scrubbed_runtime)
    bootstrap = read_manifest(paths, required=True)
    # A real schema-valid fixture manifest but deliberately NO basis file:
    # Current welded capture supplies the live map, never a selected-file bootstrap.
    base_path = str((scrubbed_runtime / "not-loaded-base.json").resolve())
    manifest = manifest_with_updates(bootstrap, base_snapshot_path=base_path,
                                    coherence_mode="transition", journal_head_seq=7)
    write_manifest(paths, manifest, expected_epoch_id=bootstrap.epoch_id, expected_head_seq=0)
    view = CacheView.build(epoch_number=manifest.epoch_number, epoch_id=manifest.epoch_id,
                           adopted_sequence=7, nodes_export={"nodes": [{"id": "root"}]},
                           account_scope_id=manifest.account_scope_id)
    publisher = CacheViewPublisher(view)
    captured = []
    def capture():
        current = publisher.capture()
        captured.append(current)
        return current, frozenset()
    client = SimpleNamespace(_coherence_paths=lambda: paths,
                             _cache_view_publisher=publisher, _capture_view_and_dirty=capture)
    def forbidden(*args, **kwargs):
        raise AssertionError("live basis loaded or wrote an independent execution snapshot")
    assert not hasattr(server, "_select_manifest_linked_snapshot")
    assert not hasattr(gov, "load_usable_nodes_export_snapshot")
    monkeypatch.setattr(snapshot_store, "write_snapshot_candidate", forbidden)
    selected = await server._carto_select_live_basis(client, "skipped")
    assert captured == [view]  # Exactly one welded local capture, no adoption adapter.
    assert selected["cache_by_id"] is view.nodes_by_id, "live basis copied the account ID map"
    assert selected["snapshot_path"] == base_path
    assert selected["snapshot_lineage"] == {
        "account_scope_id": manifest.account_scope_id, "epoch_number": manifest.epoch_number,
        "epoch_id": manifest.epoch_id, "incorporated_through_seq": 0,
    }, "base lineage was mislabeled with the live journal head"
    assert selected["live_basis_lineage"]["adopted_sequence"] == 7
    assert selected["freshness_disposition"] == "skipped"
    successor = CacheView.build(epoch_number=view.epoch_number, epoch_id=view.epoch_id,
                                adopted_sequence=8, nodes_export={"nodes": [{"id": "root"}, {"id": "local"}]},
                                account_scope_id=view.account_scope_id)
    publisher.publish(successor)
    next_selection = await server._carto_select_live_basis(client, "gesture_performed")
    assert captured == [view, successor]
    assert next_selection["cache_by_id"] is successor.nodes_by_id
    assert selected["cache_by_id"] is view.nodes_by_id and set(selected["cache_by_id"]) == {"root"}
    assert not (scrubbed_runtime / "not-loaded-base.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["pin", "override", "fallback", "admission", "adopt"])
async def test_admission_adapters_borrow_pinned_override_and_adopted_maps(scrubbed_runtime, monkeypatch, case):
    from types import SimpleNamespace
    from n05_region_harness import LookupOnlyMap
    from workflowy_mcp import server
    from workflowy_mcp.coherence import operation_cache
    from workflowy_mcp.client import api_client_nexus as nexus

    pinned_map = LookupOnlyMap({"pinned": {"id": "pinned"}})
    override_map = LookupOnlyMap({"override": {"id": "override"}})
    selection_map = LookupOnlyMap({"selected": {"id": "selected"}})
    fresh_map = LookupOnlyMap({"fresh": {"id": "fresh"}})
    def capture():
        return SimpleNamespace(nodes_by_id=fresh_map, adopted_sequence=8, epoch_id="fixture"), frozenset()
    monkeypatch.setattr(server, "get_client", lambda: SimpleNamespace(_capture_view_and_dirty=capture))
    pinned = SimpleNamespace(nodes_by_id=pinned_map, view=SimpleNamespace(adopted_sequence=7, epoch_id="fixture"))
    token = operation_cache._OPERATION_CACHE_SNAPSHOT.set(None if case == "fallback" else pinned)
    seen = []
    monkeypatch.setattr(nexus, "resolve_cartographer_path_from_node", lambda **kwargs: {"abs_path": "/n07-fixture.py"})
    def footprint(path, root, nodes, *, kind):
        seen.append(nodes)
        return {"root_uuid": root}
    monkeypatch.setattr(server, "compute_footprint", footprint)
    try:
        if case in {"pin", "override", "fallback"}:
            server._carto_footprint_from_selection(
                root_uuid="root", mode="file", source_regime="standard",
                selection={"cache_by_id": selection_map},
                cache_by_id_override=override_map if case == "override" else None,
            )
            expected = {"pin": pinned_map, "override": override_map, "fallback": selection_map}[case]
            assert len(seen) == 1 and seen[0] is expected
        else:
            forwarding = server._carto_admission_coherence_kwargs(root_uuid="root", mode="file", source_regime="standard")
            if case == "admission":
                assert forwarding["live_mutation_lease_view"] is pinned_map
            else:
                assert (await forwarding["adopt_fn"]())["view"] is fresh_map
    finally:
        operation_cache._OPERATION_CACHE_SNAPSHOT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["PRE", "B0", "GESTURE", "INHERIT"])
async def test_live_basis_uses_one_publisher_without_worker_bootstrap(claimed_task_environment, monkeypatch, form):
    """C08: actual launch/selection/pin/bridge; phase8 inherits its Markdown parent."""
    from types import SimpleNamespace
    from pathlib import Path
    from coherence_harness import make_coherence_root
    from n05_region_harness import node_uuid
    from workflowy_mcp import carto_job_governance as gov, markdown_generate_pipeline as pipeline
    from workflowy_mcp.client.api_client_etch import export_nodes_impl
    from workflowy_mcp.coherence.cache_view import CacheView
    from workflowy_mcp.coherence.operation_cache import current_operation_cache, publish_local_cache_view
    from workflowy_mcp.coherence.schema import read_manifest, write_manifest, manifest_with_updates
    from workflowy_mcp.mutation_locks import _governance_holds, held_mutation_lock_regions
    env, server, client = claimed_task_environment, claimed_task_environment.server, claimed_task_environment.client
    paths = make_coherence_root(env.path / "coherence", "a" * 32)
    old_manifest = read_manifest(paths, required=True)
    base_path = str(env.path / "diagnostic-base-never-loaded.json")
    manifest = manifest_with_updates(old_manifest, base_snapshot_path=base_path, journal_head_seq=7, coherence_mode="transition")
    write_manifest(paths, manifest, expected_epoch_id=old_manifest.epoch_id, expected_head_seq=0)
    publisher = client._cache_view_publisher
    initial = CacheView.build(epoch_number=manifest.epoch_number, epoch_id=manifest.epoch_id,
        adopted_sequence=7, account_scope_id=manifest.account_scope_id,
        nodes_export=publisher.capture().nodes_export)
    publisher.publish(initial)
    client._coherence_paths = lambda: paths
    fresh_calls, selections, bodies, inherited = [], [], [], []
    fresh_entered, fresh_release = asyncio.Event(), asyncio.Event()
    # The fixture installs a fake selector for older tests. Here restore the
    # actual public selection function and its actual borrowed-map helper.
    monkeypatch.setattr(server, "_carto_select_snapshot", env.live_server_selection)
    original_basis = server._carto_select_live_basis
    async def basis(authority, freshness_disposition):
        assert authority is client and authority._cache_view_publisher is publisher
        value = await original_basis(authority, freshness_disposition)
        selections.append(value)
        assert value["cache_by_id"] is publisher.capture().nodes_by_id
        return value
    async def fresh(**kwargs):
        assert current_operation_cache(required=False) is None and not held_mutation_lock_regions()
        fresh_calls.append(kwargs)
        if form == "B0":
            assert runtime.current_carto_job_context() is not None
            fresh_entered.set()
            await fresh_release.wait()
        return {"success": True}
    monkeypatch.setattr(server, "_carto_select_live_basis", basis)
    monkeypatch.setattr(server, "_cache_refresh_should_skip", lambda: False)
    monkeypatch.setattr(server, "_request_nodes_export_cache_refresh_async", fresh)
    def forbidden(*args, **kwargs):
        raise AssertionError("C08 reached a client constructor or independent worker snapshot bootstrap")
    assert not hasattr(server, "_select_manifest_linked_snapshot")
    assert not hasattr(gov, "load_usable_nodes_export_snapshot")
    monkeypatch.setattr(server, "WorkFlowyClient", forbidden)
    from workflowy_mcp.coherence import snapshot_store
    monkeypatch.setattr(snapshot_store, "read_snapshot_envelope", forbidden)
    async def work(**kwargs):
        context = runtime.current_carto_job_context()
        pin = current_operation_cache()
        assert context.job_control.task is asyncio.current_task() and held_mutation_lock_regions()
        assert client._cache_view_publisher is publisher and client._write_governor is governor
        assert (pin.view.epoch_id, pin.view.adopted_sequence) == (manifest.epoch_id, 7)
        bodies.append(pin.operation_id)
        file_id = node_uuid("file")
        def own_write(payload):
            payload["nodes"] = [{**row, "name": "own visible write"} if str(row.get("id")) == file_id else row
                                for row in payload["nodes"]]
        publish_local_cache_view(publisher, own_write, reason="C08-own-write")
        assert current_operation_cache().nodes_by_id[file_id]["name"] == "own visible write", "C08 own cache write was not visible in its operation"
        latest = publisher.capture()
        newer = CacheView.build(epoch_number=latest.epoch_number + 1, epoch_id="f" * 32,
            adopted_sequence=0, account_scope_id=latest.account_scope_id,
            nodes_export={"nodes": [*latest.nodes_export["nodes"], {"id": node_uuid("new-epoch-only"), "name": "unrelated"}]})
        publisher.publish(newer)
        await asyncio.sleep(0)
        exported = await export_nodes_impl(client, node_id=file_id)
        assert exported["nodes"][0]["name"] == "own visible write"
        assert node_uuid("new-epoch-only") not in current_operation_cache().nodes_by_id, "C08 imported an unrelated newer generation mid-job"
        assert current_operation_cache().view.epoch_id == manifest.epoch_id
        return {"success": True, "nodes_updated": 1}
    client.refresh_file_node_beacons = work
    governor = client._write_governor
    async def markdown(**kwargs):
        before = len(fresh_calls), len(selections), current_operation_cache().operation_id, asyncio.current_task()
        phase8 = await pipeline._run_phase8_refresh_inline(client, job_context=runtime.current_carto_job_context(), root_uuid=kwargs["root_uuid"])
        after = len(fresh_calls), len(selections), current_operation_cache().operation_id, asyncio.current_task()
        assert after == before, "inline INHERIT performed another selection or pin"
        inherited.append(True)
        return {"success": phase8["status"] == "completed", "phase8": phase8}
    monkeypatch.setattr(pipeline, "run_markdown_generate_pipeline", markdown)
    async def preparation(payload, selection):
        assert payload["name"] == payload["note"] == ""
        assert selection["cache_by_id"] is initial.nodes_by_id and held_mutation_lock_regions()
        return {"success": True}
    if form in {"GESTURE", "INHERIT"}:
        selection = await server._carto_select_snapshot(reason="C08 gesture", source="C08", disposition_when_performed="gesture_performed") if form == "GESTURE" else None
        ack = await server._start_carto_markdown_generate_job(node_uuid("file"), selected_snapshot=selection)
    elif form == "B0":
        ack = await server._start_carto_refresh_job(node_uuid("symbol"), "file", preparation=preparation,
            frozen_payload={"name": "", "note": ""},
            initial_footprint=gov.compute_footprint(str(env.path / "fixture.py"), node_uuid("file"), initial.nodes_by_id, kind="file"),
            preparation_input_digest=gov.derive_preparation_input_digest("ast_beacon_write", node_uuid("symbol"), "", ""))
        assert fresh_calls == [], "B0 did freshness before the ack"
    else:
        ack = await server._start_carto_refresh_job(node_uuid("file"), "file")
    assert ack["success"], ack
    task, control = server._carto_inprocess_tasks[ack["job_id"]], server._carto_job_controls[ack["job_id"]]
    try:
        if form == "B0":
            await asyncio.wait_for(fresh_entered.wait(), 10)
            assert not bodies and not task.done()
            fresh_release.set()
        await task
    finally:
        fresh_release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert len(fresh_calls) == len(selections) == len(bodies) == 1
    assert selections[0]["snapshot_path"] == base_path and not Path(base_path).exists()
    assert selections[0]["snapshot_lineage"]["incorporated_through_seq"] == 0
    assert selections[0]["live_basis_lineage"]["adopted_sequence"] == 7
    assert selections[0]["freshness_disposition"] == ("gesture_performed" if form == "GESTURE" else "performed")
    assert bool(inherited) is (form in {"GESTURE", "INHERIT"})
    assert control.terminal_result["status"] == "completed", control.terminal_result.get("error") or control.terminal_result
    assert not server._carto_inprocess_tasks and _governance_holds(env.jobs) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("mechanism", ["worker-bootstrap", "no-own-overlay", "import-new-epoch"])
async def test_c08_witness_rejects_bootstrap_and_pin_neutralizations(claimed_task_environment, monkeypatch, mechanism):
    from workflowy_mcp import server
    from workflowy_mcp.coherence import operation_cache
    if mechanism == "worker-bootstrap":
        # Retired bootstrap remains a source-copy mutation, never a stub
        # installed back onto production modules.
        assert not hasattr(server, "_carto_account_scope_and_env"), (
            "retired worker bootstrap env adapter is importable")
        from test_n07_carto_fences import _job_capability_offenders
        source = "async def _run_carto_job(spec):\n    return _select_manifest_linked_snapshot(client, freshness_disposition='performed')\n"
        found = _job_capability_offenders({"src/workflowy_mcp/server.py": source})
        if os.environ.get("CUT2B_NEUTRALIZE_C08_BOOTSTRAP_TRIPWIRE"):
            found = []
        assert any("bootstrap authority" in str(row) for row in found), found
        return
    elif mechanism == "no-own-overlay":
        monkeypatch.setattr(operation_cache, "_advance_task_snapshot", lambda *args, **kwargs: None)
        message = "C08 own cache write was not visible"
    else:
        from dataclasses import replace
        from workflowy_mcp.coherence.cache_view import CacheViewPublisher
        original = CacheViewPublisher.publish
        def repin(publisher, view, *args, **kwargs):
            result = original(publisher, view, *args, **kwargs)
            pin = operation_cache.current_operation_cache(required=False)
            if pin is not None and pin.owner is publisher:
                operation_cache._OPERATION_CACHE_SNAPSHOT.set(replace(
                    pin, view=view, nodes_export=view.nodes_export, nodes_by_id=view.nodes_by_id,
                ))
            return result
        monkeypatch.setattr(CacheViewPublisher, "publish", repin)
        message = "C08 imported an unrelated newer generation"
    with pytest.raises(AssertionError, match=message):
        await test_live_basis_uses_one_publisher_without_worker_bootstrap(claimed_task_environment, monkeypatch, "PRE")


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["file", "folder", "prepared-ast", "markdown", "bulk"])
async def test_claimed_public_launch_owns_task_grant_and_real_exit(claimed_task_environment, monkeypatch, family):
    """Real admission/token finalizer, not the earlier core finalizer spies."""
    from n05_region_harness import node_uuid
    from workflowy_mcp import carto_job_governance as gov, markdown_generate_pipeline as pipeline
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    from workflowy_mcp.mutation_locks import _governance_holds, held_mutation_lock_regions
    env = claimed_task_environment
    server = env.server
    if _probe("CUT2B_NEUTRALIZE_FINALIZE_RELEASE_ORDER"):
        # Release the actual outer grant before the exit door finalizes memory.
        real_finalize = server.finalize_claim_async
        async def release_before_finalize(*args, **kwargs):
            _release_current_carto_grant()
            return await real_finalize(*args, **kwargs)
        monkeypatch.setattr(server, "finalize_claim_async", release_before_finalize)
    if _probe("CUT2B_NEUTRALIZE_REGISTRY_TRIPWIRE"):
        # A fresh CARTO reservation survives the terminal release.
        monkeypatch.setattr(server, "finalize_claim_async",
                            _reservation_after_finalize(server))
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []
    async def refresh(**kwargs):
        context = runtime.current_carto_job_context()
        env.trace("send")
        holds = tuple(hold for hold in _governance_holds(env.jobs)
                      if hold["record_kind"] == "carto"
                      and hold["job_id"] == context.job_control.presentation["id"])
        assert len(holds) == 1, "running job has no single actual registry reservation"
        hold = holds[0]
        assert hold["claim_id"] == context.claim_id and hold["finalized"] is False
        assert hold["job_file"] == str(context.job_file)
        assert isinstance(hold["ancestor_chain"], tuple) and hold["root_uuid"] in hold["ancestor_chain"]
        seen.append((asyncio.current_task(), current_operation_cache().operation_id,
                     held_mutation_lock_regions(), context))
        entered.set()
        await release.wait()
        env.trace("send-complete")
        return {"success": True, "nodes_updated": 1}
    env.client.refresh_file_node_beacons = refresh
    env.client.refresh_folder_cartographer_sync = refresh
    async def markdown(**kwargs):
        context = runtime.current_carto_job_context()
        phase8 = await pipeline._run_phase8_refresh_inline(env.client, job_context=context, root_uuid=kwargs["root_uuid"])
        return {"success": phase8["status"] == "completed", "phase8": phase8}
    monkeypatch.setattr(pipeline, "run_markdown_generate_pipeline", markdown)
    async def inner(**kwargs):
        env.trace("bulk-candidate")
        kwargs["summary"]["node_updates"] += 1
        return 1, False
    monkeypatch.setattr(server, "_bulk_apply_per_file_inner_loop", inner)
    prepared_called = []
    async def prepare(payload, selection):
        env.trace("prepare")
        assert payload["name"] == payload["note"] == ""
        assert runtime.current_carto_job_context().claim_id == payload["_claim_id"]
        prepared_called.append(True)
        return {"success": True}
    file_id = node_uuid("file")
    if family == "markdown":
        ack = await server._start_carto_markdown_generate_job(file_id)
    elif family == "bulk":
        ack = await server._start_carto_bulk_visible_apply_job(
            root_uuid=file_id, mode="file", visible_tree_payload={
                "root": {"id": file_id, "name": "source.py", "note": "Path: source.py"},
                "children": [{"id": node_uuid("symbol"), "name": "symbol #n07", "note": "AST_QUALNAME: symbol"}],
            },
        )
    else:
        fields = {}
        root = node_uuid("folder") if family == "folder" else file_id
        if family == "prepared-ast":
            root = node_uuid("symbol")
            fields = {"preparation": prepare, "frozen_payload": {"name": "", "note": ""},
                      "initial_footprint": gov.compute_footprint(
                          str(env.path / "fixture.py"), file_id,
                          env.client._cache_view_publisher.capture().nodes_by_id, kind="file"),
                      "preparation_input_digest": gov.derive_preparation_input_digest("ast_beacon_write", root, "", "")}
        ack = await server._start_carto_refresh_job(root, "folder" if family == "folder" else "file", **fields)
    assert ack["success"], ack
    task = server._carto_inprocess_tasks[ack["job_id"]]
    control = server._carto_job_controls[ack["job_id"]]
    assert env.controller._job_stops[ack["job_id"]][1] is task
    assert not entered.is_set(), "work ran before registration/ack"
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert len(server._carto_inprocess_tasks) == 1, "phase8 created a child task"
        assert len(seen) == 1 and seen[0][0] is task
        roots = {region.key for region in seen[0][2]}
        assert roots == {node_uuid("folder") if family == "folder" else file_id}
        assert bool(prepared_called) == (family == "prepared-ast")
        assert not (env.jobs / "diagnostic-only-base.json").exists()
    finally:
        release.set()
        await task
        await asyncio.sleep(0)
    assert control.terminal_result["status"] == "completed", control.terminal_result
    assert control.terminal_result["detached"] is False
    assert control.terminal_result["execution_model"] == "daemon_task"
    assert control.terminal_result["cache_refresh_required"] is True
    final = [event for event in env.events if event[0] == "real-finalize"]
    assert len(final) == 1 and final[0][1] == seen[0][1] and final[0][2]
    assert _governance_holds(env.jobs) == (), "finalized job left its registry reservation"
    assert not held_mutation_lock_regions()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,extra_key", [("mutation_region_error", "error_extra"), ("coherence_barrier_active", "coherence_admission")])
async def test_task_returned_typed_failure_preserves_code_base_and_extras(task_core_environment, kind, extra_key):
    env = task_core_environment
    async def refresh(**kwargs):
        return {"success": False, "error_kind": kind, "error": "unchanged typed base message",
                extra_key: {"reason": "fixture-refusal", "recovery": "refresh"}}
    env.client.refresh_file_node_beacons = refresh
    spec = _region_spec(env.path)
    await env.server._schedule_carto_job(spec)
    control = env.server._carto_job_controls[spec.job_id]
    await control.task
    payload = control.terminal_result
    assert payload["status"] == "failed"
    assert payload["error_kind"] == kind and payload["error"] == "unchanged typed base message"
    assert payload["error_extra"]["reason"] == "fixture-refusal"
    assert payload["error_extra"]["job_id"] == spec.job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [False, True])
async def test_task_helper_cancel_signal_is_observed_only_for_own_user_request(task_core_environment, requested):
    from workflowy_mcp.models.errors import OperationCancelledError
    env = task_core_environment
    async def refresh(**kwargs):
        if requested:
            runtime.current_carto_job_context().job_control.request_user_cancel()
        raise OperationCancelledError("fixture safe helper return")
    env.client.refresh_file_node_beacons = refresh
    spec = _region_spec(env.path)
    await env.server._schedule_carto_job(spec)
    control = env.server._carto_job_controls[spec.job_id]
    await control.task
    assert control.user_observed is requested
    assert control.terminal_result["status"] == ("cancelled" if requested else "failed")
    assert env.events[-1][0] == ("cancel-finalize" if requested else "finalize")
    assert env.events[-1][2], "helper stop finalized after releasing its grant"


@pytest.mark.asyncio
@pytest.mark.parametrize("front", ["widget", "widget-error", "compact", "detail", "missing-detail"])
async def test_job_presentation_finishes_proportional_work_off_loop(scrubbed_runtime, monkeypatch, front):
    """Residual §6.7/C06: exact old presentation, joined empty-Context work.

    The scanner already returns owned rows. Presentation must not bring their
    filtering/sort/byte-budget/JSON work back onto the loop, or transfer generic
    job task/payload/progress objects merely because the view ignores them.
    From JR2 G2 a one-job call reads only that job: the detail leg's job is a
    real record on disk (located by its file name) and asks for
    ``view="detail"``; even an unknown job's filename lookup is off-loop, but
    it scans no record contents and opens no unrelated record.
    """
    import contextvars
    import json
    import threading
    from types import SimpleNamespace
    from workflowy_mcp import server, mutation_locks
    from workflowy_mcp.coherence import operation_cache
    from workflowy_mcp.job_status_view import summarize_job_inventory
    marker = contextvars.ContextVar("n07-presentation-witness", default=None)
    loop, loop_thread = asyncio.get_running_loop(), threading.get_ident()
    entered, release = asyncio.Event(), threading.Event()
    private = object()
    memory = {"memory-active": {"kind": "TEST", "status": "running", "started_at": "2026-10-01T00:00:00",
                                "_task": private, "payload": private, "result": private, "progress": private},
              "memory-done": {"kind": "TEST", "status": "completed", "_task": private}}
    rows = [{"job_id": f"history-{index:04d}", "kind": "CARTO_REFRESH", "status": (
        "completed" if index % 3 == 0 else "cancelled" if index % 3 == 1 else "completed_with_warnings"),
        "created_at": "2026-10-01T00:00:00", "error_kind": "carto_job_exception",
        "error_extra": {"phase": "fixture", "detail": ["λ\n" * 1024]}, "detached": False}
        for index in range(600)]
    rows += [{"job_id": "duplicate", "status": "running", "error": "first"},
             {"job_id": "duplicate", "status": "unknown future value", "error": "last wins"}]
    monkeypatch.setattr(server, "_jobs", memory)
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(scrubbed_runtime))
    monkeypatch.setattr(server, "_carto_list_jobs_in_flight", True)
    # The one-job detail path locates its record by file name (*_<job_id>.json)
    # and reads only that record; the list paths keep the scanned rows above.
    detail_record = {"id": "duplicate", "type": "CARTO_REFRESH", **rows[-1]}
    (scrubbed_runtime / "20261001-000000_duplicate.json").write_text(
        json.dumps(detail_record), encoding="utf-8")
    scans, workers, sent = [], [], []
    async def scan(*args, **kwargs):
        assert threading.get_ident() == loop_thread and marker.get() == "caller"
        scans.append(True)
        if front == "widget-error":
            raise RuntimeError("fixture inventory error")
        return rows
    monkeypatch.setattr(server, "_scan_carto_job_files_async", scan)
    for name in ("_project_carto_job_status", "_project_carto_widget_reply"):
        original = getattr(server, name)
        def data(*args, _name=name, _original=original, **kwargs):
            assert threading.get_ident() != loop_thread, "presentation phase ran inline"
            assert marker.get() is None and runtime.current_carto_job_context() is None
            assert operation_cache.current_operation_cache(required=False) is None and mutation_locks.MUTATION_LOCK_STATE.get() is None
            if _name == "_project_carto_job_status":
                for value in args:
                    if isinstance(value, (list, tuple)):
                        assert all(not {"_task", "payload", "result", "progress"}.intersection(row)
                                   for row in value if hasattr(row, "keys")), \
                            "private job execution state crossed into presentation worker"
            workers.append(_name)
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "fixture did not release presentation worker"
            return _original(*args, **kwargs)
        monkeypatch.setattr(server, name, data)
    async def send(text):
        assert threading.get_ident() == loop_thread and marker.get() == "caller"
        sent.append(text)
    if front == "widget":
        # Job-list fix [ap:FK5O8K]: the widget gets compact rows (no
        # error_extra / result_summary evidence), same filter and order.
        expected = json.dumps({"action": "carto_list_jobs_result", "success": True,
            "jobs": [{key: value for key, value in row.items() if key != "error_extra"}
                     for row in rows if str(row.get("status") or "unknown").lower() not in {"completed", "cancelled"}]})
    elif front == "widget-error":
        expected = json.dumps({"action": "carto_list_jobs_result", "success": False, "error": "fixture inventory error"})
    elif front == "compact":
        expected = summarize_job_inventory([{**row, "job_id": row.get("job_id") or key, "detached": False} for key, row in memory.items()], rows)
    elif front == "detail":
        # Asserted against its record below: a detail page carries the complete
        # detail's own values (the frame plus this page's units), never a rewrite.
        expected = None
    else:
        expected = {"success": False, "error": "Unknown job_id: not-present"}
    async def invoke():
        if front.startswith("widget"):
            await server._handle_carto_list_jobs(SimpleNamespace(send=send))
            return sent[-1] if sent else None
        if front == "compact":
            return await server._system_job_status_impl(None)
        if front == "detail":
            return await server._system_job_status_impl("duplicate", view="detail")
        return await server._system_job_status_impl("not-present")
    marker_token = marker.set("caller")
    cache_token = operation_cache._OPERATION_CACHE_SNAPSHOT.set(object())
    lock_token = mutation_locks.MUTATION_LOCK_STATE.set(object())
    task = asyncio.create_task(invoke())
    waiter = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait([waiter, task], timeout=10, return_when=asyncio.FIRST_COMPLETED)
        assert waiter in done, "presentation worker never entered its real off-loop seam"
        # Mutating the live table while its worker is outstanding must not
        # rewrite the earlier scalar snapshot, or expose private objects.
        memory["memory-active"]["status"] = "completed"
        assert not task.done()
    finally:
        release.set()
        if not waiter.done():
            waiter.cancel()  # fixture notification, never a Cartographer task
        await asyncio.gather(waiter, return_exceptions=True)
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        mutation_locks.MUTATION_LOCK_STATE.reset(lock_token)
        operation_cache._OPERATION_CACHE_SNAPSHOT.reset(cache_token)
        marker.reset(marker_token)
    if front == "detail":
        # Complete evidence: every value the record carries reaches the client
        # whole, beside only §2.10's added and page keys.
        assert result["success"] is True
        assert {key: result[key] for key in detail_record} == detail_record
    else:
        assert result == expected, result
    # G2's one-job path scans no record: the list fronts scan once, the detail
    # and unknown-job fronts not at all; the presentation/lookup worker runs
    # once for every front (filename enumeration is filesystem work too).
    assert len(scans) == (0 if front in {"detail", "missing-detail"} else 1)
    assert len(workers) == 1
    if front.startswith("widget"):
        assert server._carto_list_jobs_in_flight is False and sent == [expected]
