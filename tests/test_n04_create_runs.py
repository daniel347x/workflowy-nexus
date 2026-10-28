"""N04 singleton/k faces: real grants, governed fake HTTP and source copies."""
import asyncio
import ast
import inspect
import os
import textwrap

import httpx
import pytest

from test_n03_transition_create import Account, OTHER, PARENT, PREDECESSOR, setup_client, transaction
from workflowy_mcp.client.create_transport import PlacementIntent
from workflowy_mcp.coherence import transition_create as tc
from workflowy_mcp.models import NodeCreateRequest, RateLimitError
from n07_job_harness import real_job_environment, scrubbed_runtime, launch_real_job


def _damage(monkeypatch, name, old, new):
    source = textwrap.dedent(inspect.getsource(getattr(tc.TransitionCreateTransaction, name)))
    assert old in source
    namespace = dict(vars(tc))
    exec(compile(source.replace(old, new), "<n04-source-copy>", "exec"), namespace)
    monkeypatch.setattr(tc.TransitionCreateTransaction, name, namespace[name])


def _drop_forwarded_grant(module, function, callee, expected=1):
    """Source-copy one real forwarding edge; optional receiver defaults avoid setup errors."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and getattr(node.func, "attr", getattr(node.func, "id", None)) == callee
             and any(keyword.arg == "held_grant" for keyword in node.keywords)]
    if len(calls) != expected:
        raise RuntimeError(f"forwarding mutant seam moved: {function.__name__} / {callee}: {len(calls)} != {expected}")
    for call in calls:
        call.keywords = [keyword for keyword in call.keywords if keyword.arg != "held_grant"]
    namespace = dict(vars(module))
    exec(compile(tree, "<n04-drop-forwarded-grant>", "exec"), namespace)
    return namespace[function.__name__]


@pytest.mark.parametrize("transport", ["llm", "v1"])
@pytest.mark.parametrize("damage", [False, True])
def test_t10_decoding_error_verifies_committed_identity(tmp_path, monkeypatch, transport, damage):
    if damage or os.getenv("N04_MUTANT") == "omit_decoding_verification":
        _damage(monkeypatch, "_attempt", "httpx.DecodingError, ", "")
    async def run():
        account = Account()
        client, _, _, _ = setup_client(tmp_path, account, forced=transport == "v1")
        async def server(request):
            response = await account(request)
            if request.method == "POST":
                raise httpx.DecodingError("reply decoder failed after commit")
            return response
        await client._client.aclose()
        client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(server))
        async with client._client, transaction(client) as tx:
            return await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR)), account
    if damage:
        with pytest.raises(httpx.DecodingError):
            asyncio.run(run())
    else:
        result, account = asyncio.run(run())
        assert result.created_ids == tuple(account.ids)
        assert len(result.created_ids) == 1
        assert account.list_count == 1


@pytest.mark.parametrize("damage", [False, True])
def test_t10_unrelated_adopted_dirt_does_not_add_prelisting(tmp_path, monkeypatch, damage):
    if damage or os.getenv("N04_MUTANT") == "restore_unrelated_dirt_veto":
        _damage(monkeypatch, "_baseline", 'if (not getattr(status, "external_dirty", False)',
                'if (not snapshot.dirty_ids and not getattr(status, "external_dirty", False)')
    async def run():
        account = Account()
        client, _, _, _ = setup_client(tmp_path, account)
        client._mark_nodes_export_dirty([OTHER])
        async with client._client, transaction(client) as tx:
            result = await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR))
            assert len(tx.verified_prefix) == 1
            return result, account.list_count
    _, listings = asyncio.run(run())
    assert listings == (2 if damage else 1)


def test_t03_preselected_probe_is_not_selected_again_and_is_released(tmp_path, monkeypatch):
    async def run():
        account = Account()
        client, _, _, _ = setup_client(tmp_path, account)
        controller = client._insert_capability
        selection = controller.select_for_insert()
        def forbidden():
            raise AssertionError("a supplied one-use selection must not select again")
        monkeypatch.setattr(controller, "select_for_insert", forbidden)
        async with client._client, transaction(client) as tx:
            result = await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"),
                                     PlacementIntent.after(PREDECESSOR), selection=selection)
            assert result.transport == "llm"
            assert controller.snapshot().state.value == "AVAILABLE"
    asyncio.run(run())


def test_a1_only_endpoint_response_tags_rate_error(tmp_path):
    async def run():
        account = Account("rate")
        client, _, _, _ = setup_client(tmp_path, account)
        async with client._client, transaction(client) as tx:
            with pytest.raises(RateLimitError) as caught:
                await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"), PlacementIntent.after(PREDECESSOR))
            assert caught.value.llm_endpoint_429 is True
            assert caught.value.llm_dispatch_started is True
            assert caught.value.retry_after == 3
            assert not getattr(RateLimitError(3), "llm_endpoint_429", False)
            assert account.llm_count == 1 and account.list_count == 0
    asyncio.run(run())


def test_t03_preselected_probe_is_neutral_on_preflight_rate(tmp_path, monkeypatch):
    async def run():
        account = Account()
        client, _, _, _ = setup_client(tmp_path, account)
        controller = client._insert_capability
        selected = controller.select_for_insert()
        original = RateLimitError(3)
        async def refused(self):
            raise original
        monkeypatch.setattr(tc.TransitionCreateTransaction, "_preflight", refused)
        async with client._client, transaction(client) as tx:
            with pytest.raises(RateLimitError) as caught:
                await tx.create(NodeCreateRequest(parent_id=PARENT, name="ours"),
                    PlacementIntent.after(PREDECESSOR), selection=selected)
            assert caught.value is original
            assert not getattr(original, "llm_endpoint_429", False)
            assert controller.snapshot().state.value == "UNKNOWN"
            assert account.llm_count == account.v1_count == account.list_count == 0
    asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["file", "folder", "markdown", "bulk", "persistent_new", "temp_override", "ast"])
async def test_t03_real_job_forwards_the_very_yielded_grant(real_job_environment, monkeypatch, family, mutation=None):
    """Actual job core/family dispatch, with pass-through ownership spies only."""
    from contextlib import asynccontextmanager
    from workflowy_mcp import server, markdown_generate_pipeline as pipeline
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    from workflowy_mcp.mutation_locks import MUTATION_LOCK_STATE
    env = real_job_environment
    yielded, seen = [], []
    implementations = {}
    original_runner = server._run_carto_job
    original_scope = server.mutation_region_locks

    @asynccontextmanager
    async def scope(*args, **kwargs):
        async with original_scope(*args, **kwargs) as held:
            yielded.append(held)
            yield held
    monkeypatch.setattr(server, "mutation_region_locks", scope)

    def spy(owner, name):
        implementations[owner, name] = getattr(owner, name)
        async def forwarded(*args, **kwargs):
            held = kwargs.get("held_grant")
            assert yielded and held is yielded[-1], (name, "dropped or replaced grant")
            assert held.is_active and held._state is MUTATION_LOCK_STATE.get()
            assert held.snapshot.operation_id == current_operation_cache().operation_id
            seen.append(name)
            return await implementations[owner, name](*args, **kwargs)
        monkeypatch.setattr(owner, name, forwarded)

    spy(server, "_execute_carto_job")
    spy(server, "_run_carto_bulk_visible_apply_job")
    spy(WorkFlowyClientNexus, "refresh_file_node_beacons")
    spy(WorkFlowyClientNexus, "refresh_folder_cartographer_sync")
    spy(pipeline, "run_markdown_generate_pipeline")
    spy(pipeline, "_run_phase8_refresh_inline")
    mutation = mutation or os.getenv("N04_FORWARD_MUTANT")
    if mutation in {"runner", "drop_runner_grant"}:
        monkeypatch.setattr(server, "_run_carto_job",
            _drop_forwarded_grant(server, original_runner, "_execute_carto_job", expected=2))
    elif mutation:
        sites = {
            "execute-file": (server, server, "_execute_carto_job", "refresh_file_node_beacons"),
            "execute-folder": (server, server, "_execute_carto_job", "refresh_folder_cartographer_sync"),
            "execute-markdown": (server, server, "_execute_carto_job", "run_markdown_generate_pipeline"),
            "execute-bulk": (server, server, "_execute_carto_job", "_run_carto_bulk_visible_apply_job"),
            "folder-file": (nexus, WorkFlowyClientNexus, "refresh_folder_cartographer_sync", "refresh_file_node_beacons"),
            "pipeline-inline": (pipeline, pipeline, "run_markdown_generate_pipeline", "_run_phase8_refresh_inline"),
            "inline-file": (pipeline, pipeline, "_run_phase8_refresh_inline", "refresh_file_node_beacons"),
            "bulk-file": (server, server, "_run_carto_bulk_visible_apply_job", "refresh_file_node_beacons"),
        }
        module, owner, name, callee = sites[mutation]
        implementations[owner, name] = _drop_forwarded_grant(module, implementations[owner, name], callee)
    acknowledgement = await launch_real_job(env, family)
    assert acknowledgement["success"], acknowledgement
    job_id = acknowledgement["job_id"]
    await asyncio.wait_for(env.tasks[job_id], 20)
    terminal = env.controls[job_id].terminal_result
    assert terminal["status"] in {"completed", "completed_with_warnings"}, terminal
    assert len(yielded) == 1
    assert "_execute_carto_job" in seen and "refresh_file_node_beacons" in seen
    if family == "folder":
        assert "refresh_folder_cartographer_sync" in seen
    elif family == "markdown":
        assert {"run_markdown_generate_pipeline", "_run_phase8_refresh_inline"} <= set(seen)
    elif family == "bulk":
        assert "_run_carto_bulk_visible_apply_job" in seen


@pytest.mark.asyncio
@pytest.mark.parametrize("family,mutation", [
    ("file", "runner"), ("file", "execute-file"), ("folder", "execute-folder"),
    ("markdown", "execute-markdown"), ("bulk", "execute-bulk"),
    ("folder", "folder-file"), ("markdown", "pipeline-inline"),
    ("markdown", "inline-file"), ("bulk", "bulk-file"),
])
async def test_t03_each_real_forwarding_edge_has_a_meaningful_source_copy_red(
        real_job_environment, monkeypatch, family, mutation):
    # The normal family cases above prove the same fixture succeeds. The only
    # changed production statement is the missing keyword; receivers still run
    # and assert the missing grant, rather than failing with a signature error.
    with pytest.raises(AssertionError):
        await test_t03_real_job_forwards_the_very_yielded_grant(
            real_job_environment, monkeypatch, family, mutation=mutation)


def test_t03_file_directory_reroute_forwards_the_same_grant(tmp_path, monkeypatch, mutation=False):
    from test_n04_reconcile_insert_after import (
        FILE, FakeAccount, small_interior, _build_client, _install_outer_preparation, _source,
    )
    from workflowy_mcp.client import api_client_nexus as acn
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    from workflowy_mcp.mutation_locks import mutation_region_locks
    from workflowy_mcp.mutation_lease_governance import build_region_subtree
    if mutation:
        monkeypatch.setattr(acn.WorkFlowyClientNexus, "refresh_file_node_beacons",
            _drop_forwarded_grant(acn, acn.WorkFlowyClientNexus.refresh_file_node_beacons,
                                  "refresh_folder_cartographer_sync"))
    async def run():
        account, geometry = FakeAccount(), small_interior()
        account.seed(FILE, geometry.rows)
        client, _, _, _, _, _ = _build_client(tmp_path, account)
        _install_outer_preparation(monkeypatch, tmp_path, _source(geometry.source_nodes), [])
        monkeypatch.setattr(acn, "_prepare_carto_file_facts", lambda *args: {
            "source_path": str(tmp_path), "repo_root_abs": None, "is_directory": True})
        async with client._client, cache_operation_boundary(client, "n04-reroute"):
            async with mutation_region_locks(client=client, operation_label="n04-reroute",
                    region_factory=lambda snapshot: (build_region_subtree(FILE, cache_by_id=snapshot.nodes_by_id),)) as held:
                async def folder(**kwargs):
                    assert kwargs.get("held_grant") is held and held.is_active
                    return {"success": True}
                monkeypatch.setattr(client, "refresh_folder_cartographer_sync", folder)
                result, failure = None, None
                try:
                    result = await client.refresh_file_node_beacons(FILE, held_grant=held)
                except Exception as error:
                    failure = error
                assert failure is None, failure
                assert result["success"] is True
                assert result["routed_from"] == "refresh_file_node_beacons"
        assert account.counts["llm_ops"] == account.counts["v1_create"] == 0
    asyncio.run(run())


def test_t03_directory_reroute_keyword_has_its_own_source_copy_red(tmp_path, monkeypatch):
    with pytest.raises(AssertionError):
        test_t03_file_directory_reroute_forwards_the_same_grant(tmp_path, monkeypatch, mutation=True)


@pytest.mark.asyncio
async def test_t03_empty_bulk_none_grant_has_no_graph_or_cpu_work(monkeypatch):
    from types import SimpleNamespace
    from workflowy_mcp import server
    monkeypatch.setattr(server, "get_client", lambda: object())
    async def forbidden(*args, **kwargs):
        raise AssertionError("empty bulk performed graph or data work")
    monkeypatch.setattr(server, "_run_carto_bulk_visible_apply_job", forbidden)
    monkeypatch.setattr(server, "run_carto_cpu", forbidden)
    control = SimpleNamespace()
    result = await server._execute_carto_job(
        SimpleNamespace(job_type="CARTO_BULK_APPLY", bulk_groups=()), control, held_grant=None)
    assert result == {"success": True, "message": "No eligible visible AST/beacon nodes found."}


def test_t04_order_adoption_detaches_immutable_snapshot_rows(monkeypatch):
    from types import MappingProxyType
    from workflowy_mcp.client import carto_reconcile_cpu as cpu
    from test_n03_transition_create import TAIL
    from test_n03_create_transport import CREATED
    def witness():
        published = MappingProxyType({"id": PREDECESSOR, "priority": 100, "name": "left"})
        target = {PREDECESSOR: published, TAIL: {"id": TAIL, "priority": 200},
                  CREATED: {"id": CREATED, "priority": 300}}
        children = {PARENT: [PREDECESSOR, TAIL, CREATED]}
        result = cpu.ReconcileCreateResult((CREATED,), ("after",), (PREDECESSOR, CREATED, TAIL))
        failure = None
        try:
            cpu._adopt_create_order(target, children, PARENT, result)
        except TypeError as error:
            failure = error
        assert failure is None, "order adoption attempted to mutate the published row"
        assert published["priority"] == 100
        assert target[PREDECESSOR] is not published
        assert children[PARENT] == [PREDECESSOR, CREATED, TAIL]
    source = textwrap.dedent(inspect.getsource(cpu._adopt_create_order))
    old = 'target[identifier] = {**dict(target[identifier]), "priority": rank}'
    assert source.count(old) == 1
    namespace = dict(vars(cpu))
    exec(compile(source.replace(old, 'target[identifier]["priority"] = rank'),
                 "<n04-mutant-mutate-cache-alias>", "exec"), namespace)
    with monkeypatch.context() as damaged:
        damaged.setattr(cpu, "_adopt_create_order", namespace["_adopt_create_order"])
        with pytest.raises(AssertionError, match="mutate the published row"):
            witness()
    witness()


@pytest.mark.parametrize("count", [1, 5])
def test_t09_cancelled_commit_accounts_prefix_under_hold_and_outside_write_proceeds(tmp_path, monkeypatch, count):
    from test_n04_reconcile_insert_after import (
        FILE, OTHER as OUTSIDE, LLM_PATH, FakeAccount, small_interior, _build_client,
        _install_outer_preparation, _source, _invoke,
    )
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, create_isolated_context_task
    from workflowy_mcp.mutation_locks import mutation_region_locks
    from workflowy_mcp.mutation_lease_governance import build_region_subtree, build_region_child_set
    if os.getenv("N04_MUTANT") == "drop_verified_prefix":
        _damage(monkeypatch, "_verify", "self.verified_prefix += tuple(", "discarded_prefix = tuple(")
    async def run():
        geometry = small_interior() if count == 1 else _run_geometry([f"item_{i}" for i in range(count)])
        account = FakeAccount()
        account.seed(FILE, geometry.rows)
        client, governor, _, _, _, _ = _build_client(tmp_path, account)
        _install_outer_preparation(monkeypatch, tmp_path, _source(geometry.source_nodes), [])
        entered, release = asyncio.Event(), asyncio.Event()
        active_on_post_listing, conclusions, tickets = [], [], []
        held = None
        async def server(request):
            response = await account(request)
            if request.url.path.endswith(LLM_PATH):
                entered.set()
                await release.wait()
            elif request.method == "GET" and request.url.params.get("parent_id") == FILE:
                active_on_post_listing.append(held.is_active)
            return response
        await client._client.aclose()
        client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(server))
        acquire = governor.acquire_write_slot
        async def counted(*args, **kwargs):
            ticket = await acquire(*args, **kwargs)
            tickets.append(ticket)
            return ticket
        monkeypatch.setattr(governor, "acquire_write_slot", counted)
        conclude = tc.TransitionCreateTransaction.conclude
        async def concluded(self, *, relist=False):
            conclusions.append((self.held_region is held, held.is_active, relist))
            return await conclude(self, relist=relist)
        monkeypatch.setattr(tc.TransitionCreateTransaction, "conclude", concluded)
        async def outside():
            async with cache_operation_boundary(client, "n04-outside"):
                async with mutation_region_locks(client=client, operation_label="n04-outside",
                        region_factory=lambda snapshot: (build_region_child_set(OUTSIDE, cache_by_id=snapshot.nodes_by_id),)):
                    return await client.create_node(NodeCreateRequest(parent_id=OUTSIDE, name="outside", position="bottom"),
                                                    _internal_call=True, fetch_created_node=False)
        async with client._client, cache_operation_boundary(client, "n04-cancel"):
            async with mutation_region_locks(client=client, operation_label="n04-cancel",
                    region_factory=lambda snapshot: (build_region_subtree(FILE, cache_by_id=snapshot.nodes_by_id),)) as grant:
                held = grant
                primary = asyncio.create_task(_invoke(client, held))
                await asyncio.wait_for(entered.wait(), 5)
                peer = create_isolated_context_task(outside())
                peer_node = await asyncio.wait_for(peer, 5)
                # The unrelated writer completed while the primary HTTP await
                # was still blocked, through the very same sender/governor.
                assert not primary.done()
                primary.cancel()
                result, error = await asyncio.wait_for(primary, 5)
                assert result is None and isinstance(error, asyncio.CancelledError)
                assert active_on_post_listing == [True]
                assert conclusions == [(True, True, False)]
                inserted = tuple(row["id"] for row in account.rows[FILE][1:1 + count])
                assert tuple(geometry.source_nodes[i].get("id") for i in geometry.new_indices) == inserted
                assert error.reconcile_create_result.created_ids == inserted
                current = client._cache_view_publisher.capture().nodes_by_id
                assert set(inserted) <= current.keys() and peer_node.id in current
                assert FILE in client._capture_view_and_dirty()[1]
                assert account.counts["llm_ops"] == 1
                assert account.counts["v1_create"] == 1  # peer only, never a replacement
                assert account.counts["move_top"] == account.counts["move_bottom"] == 0
                assert len(tickets) == 2
    asyncio.run(run())
def _run_geometry(names, *, tail=False):
    from test_n04_reconcile_insert_after import Geometry, _node, _row, FILE, EXISTING_PREFIX, _identifier
    left, right = (_identifier(EXISTING_PREFIX, i) for i in range(2))
    existing = [_node("left", handle=left), _node("right", handle=right)]
    additions = [_node(name) for name in names]
    nodes = existing + additions if tail else [existing[0], *additions, existing[1]]
    rows = [_row(left, "left", 100, FILE), _row(right, "right", 200, FILE)]
    return Geometry(nodes, rows, [node["name"] for node in nodes],
                    list(range(2 if tail else 1, (2 if tail else 1) + len(names))))


@pytest.mark.parametrize("count,tail", [(1, False), (9, False), (150, False), (5, True)])
def test_t07_maximal_run_is_one_ordered_insert(tmp_path, monkeypatch, count, tail):
    from test_n04_reconcile_insert_after import run_refresh, FILE
    geometry = _run_geometry([f"new_{i}" for i in range(count)], tail=tail)
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry)
    run.assertion_prelude()
    assert run.account.counts["llm_ops"] == 1
    assert run.account.counts["llm_items"] == count
    assert not run.account.counts["v1_create"]
    assert not run.account.counts["move_top"]
    assert run.account.contents(FILE) == [(name, "") for name in geometry.expected_names]
    event = next(event for event in run.account.events if event[0] == "llm")
    assert event[1] == tuple(f"new_{i}" for i in range(count))


def test_t07_repeat_boundary_is_maximal_and_preserves_all_nodes(tmp_path, monkeypatch):
    from test_n04_reconcile_insert_after import run_refresh, FILE
    geometry = _run_geometry(["a", "b", "a", "c", "d", "c"])
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry)
    run.assertion_prelude()
    assert [event[1] for event in run.account.events if event[0] == "llm"] == [
        ("a", "b"), ("a", "c", "d"), ("c",)]
    assert run.account.contents(FILE) == [(name, "") for name in geometry.expected_names]
    assert not run.account.counts["move_top"]


def test_t08_repeated_run_429_never_resends_the_verified_prefix(tmp_path, monkeypatch):
    from test_n04_reconcile_insert_after import run_refresh, FakeAccount, FILE
    geometry = _run_geometry(["a", "b", "a", "c", "c"])
    account = FakeAccount()
    account.llm_script = [429, "ok", 429, "ok", "ok"]
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account)
    run.assertion_prelude()
    assert [event[1] for event in account.events if event[0] == "llm"] == [
        ("a", "b"), ("a", "b"), ("a", "c"), ("a", "c"), ("c",)]
    assert account.contents(FILE) == [(name, "") for name in geometry.expected_names]
    assert len(account.created) == 5
    assert not account.counts["v1_create"] and not account.counts["move_top"]


@pytest.mark.parametrize("mode", ["ok", "timeout", "absent", "op_error", 404])
def test_t06_n03_account_consumes_whole_vector(tmp_path, monkeypatch, mode):
    from test_n03_transition_create import Account, setup_client, transaction
    async def recovery():
        raise AssertionError("a complete vector must not request recovery")
    monkeypatch.setattr(tc, "_request_recovery", recovery)
    async def run():
        account = Account(mode, omitted_note=True)
        client, _, _, _ = setup_client(tmp_path, account)
        requests = tuple(NodeCreateRequest(parent_id=PARENT, name=f"item_{i}", note="") for i in range(5))
        async with client._client, transaction(client) as tx:
            result = await tx.create_many(requests, PlacementIntent.after(PREDECESSOR))
            assert tuple(node.name for node in result.created_nodes) == tuple(r.name for r in requests)
            assert tuple(account.ids) == result.created_ids
            assert tx.verified_prefix == result.created_nodes
            assert account.llm_count == 1
            assert account.v1_count == (0 if mode in ("ok", "timeout") else 5)
            assert account.list_count == 1 + account.v1_count
            assert len({row["id"] for row in client._capture_view_and_dirty()[0].nodes_export["nodes"]}) == 8
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["ok", "absent", "partial", "duplicate", "order", "placement", "content", "malformed", "gui", "old_changes"])
@pytest.mark.parametrize("count", [1, 4, 150])
def test_t06_k_rule_truth_table(count, fault):
    from uuid import UUID
    from test_n03_transition_create import row, TAIL, GUI
    def identifier(i):
        return str(UUID(int=1000 + i))
    baseline_rows = [row(PREDECESSOR, "left", 100), row(TAIL, "right", 200)]
    baseline = tc._order(PARENT, baseline_rows)
    expected = tuple((f"item_{i}", "") for i in range(count))
    additions = [row(identifier(i), name, 0) for i, (name, _) in enumerate(expected)]
    for addition in additions:
        addition.pop("note")  # missing note is the declared null/empty equivalence
        addition["additive"] = {"future": True}
    rows = [baseline_rows[0], *additions, baseline_rows[1]]
    if fault in ("absent", "old_changes"):
        rows = list(reversed(baseline_rows))
        if fault == "old_changes":
            rows[0]["name"] = "GUI edit of old content"
            rows.pop()
    elif fault == "partial":
        # k=1 partial with another addition is ambiguous, not proven absent.
        rows.remove(additions[-1])
        rows.insert(1, row(GUI, "different GUI", 0))
    elif fault == "duplicate":
        rows.insert(1, row(GUI, "item_0", 0))
    elif fault == "order":
        rows = [baseline_rows[0], *reversed(additions), baseline_rows[1]] if count > 1 else [*baseline_rows, *additions]
    elif fault == "placement":
        rows = [*baseline_rows, *additions]
    elif fault == "content":
        additions[-1]["name"] = "wrong content"
    elif fault == "gui":
        rows.insert(1 + count // 2, row(GUI, "different GUI", 0))
    for i, item in enumerate(rows):
        item["priority"] = (i + 1) * 100
    evidence = {"recognized": fault != "malformed", "rows": list(reversed(rows))}
    result = tc.classify_create_run(PARENT, baseline, evidence, expected, PlacementIntent.after(PREDECESSOR))
    verdict = (tc.CreateClassification.COMMITTED if fault in ("ok", "gui") else
               tc.CreateClassification.CONCLUSIVELY_ABSENT if fault in ("absent", "old_changes") else
               tc.CreateClassification.AMBIGUOUS)
    assert result.classification == verdict
    if verdict == tc.CreateClassification.COMMITTED:
        assert result.created_ids == tuple(identifier(i) for i in range(count))
        assert result.external_ids == ((GUI,) if fault == "gui" else ())


@pytest.mark.parametrize("script,ops", [([429, "ok"], 2), ([429, 429], 2), (["timeout"], 1), (["schema"], 1), ([404], 1)])
def test_t08_a1_and_unknown_outcomes_on_runs(tmp_path, monkeypatch, script, ops):
    from test_n04_reconcile_insert_after import run_refresh, FakeAccount, FILE
    geometry = _run_geometry([f"item_{i}" for i in range(5)])
    account = FakeAccount()
    account.llm_script = list(script)
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account)
    run.assertion_prelude()
    assert account.counts["llm_ops"] == ops
    assert account.contents(FILE) == [(name, "") for name in geometry.expected_names]
    fallback = script in ([429, 429], [404], ["schema"])
    assert account.counts["v1_create"] == (5 if fallback else 0)
    assert account.counts["move_top"] == (7 if fallback else 0)


def test_t11_fallback_failure_retains_only_verified_prefix(tmp_path, monkeypatch):
    from test_n04_reconcile_insert_after import run_refresh, FakeAccount
    class FailedSecondV1(FakeAccount):
        def _v1_create(self, request):
            if self.counts["v1_create"] == 1:
                self.failure = httpx.DecodingError("unknown second v1, absent")
                raise self.failure
            return super()._v1_create(request)
    account = FailedSecondV1()
    account.llm_script = [404]
    run = run_refresh(tmp_path, monkeypatch, geometry=_run_geometry(["one", "two", "three"]), account=account)
    assert run.error.__cause__ is account.failure
    evidence = getattr(account.failure, "reconcile_create_result", None)
    assert evidence is not None, "the verified fallback prefix was dropped"
    prefix = evidence.created_ids
    assert len(prefix) == 1
    assert account.counts["llm_ops"] == 1
    assert account.counts["v1_create"] == 1
    assert prefix[0] in run.client._capture_view_and_dirty()[0].nodes_by_id
    assert not account.counts["move_top"]


@pytest.mark.parametrize("fault", ["partial", "order", "duplicate"])
def test_t08_partial_or_reordered_batch_stops_without_fill_in(tmp_path, monkeypatch, fault):
    from test_n04_reconcile_insert_after import run_refresh, FakeAccount, FILE
    from workflowy_mcp.coherence.errors import CoherenceBarrierActiveError
    class BrokenRun(FakeAccount):
        async def _llm(self, request):
            response = await super()._llm(request)
            if fault == "partial":
                self.rows[FILE].pop(2)
            elif fault == "order":
                self.rows[FILE][1:4] = reversed(self.rows[FILE][1:4])
            self._renumber(FILE)
            return response
    account = BrokenRun()
    account.llm_script = ["duplicate" if fault == "duplicate" else "ok"]
    run = run_refresh(tmp_path, monkeypatch, geometry=_run_geometry(["one", "two", "three"]), account=account)
    assert isinstance(run.error.__cause__, CoherenceBarrierActiveError)
    assert run.recovery
    assert account.counts["llm_ops"] == 1
    assert account.counts["v1_create"] == account.counts["move_top"] == 0


def test_t07_new_nodes_children_stay_nonrecursive_bare_v1(tmp_path, monkeypatch):
    from test_n04_reconcile_insert_after import run_refresh, FILE, _node
    geometry = _run_geometry(["new_parent", "next"])
    geometry.source_nodes[1]["children"] = [_node("child_one"), _node("child_two")]
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry)
    run.assertion_prelude()
    assert run.account.counts["llm_ops"] == 1
    assert run.account.counts["v1_create"] == 2
    assert not run.account.counts["move_top"]
    parent = run.account.rows[FILE][1]["id"]
    assert run.account.contents(parent) == [("child_one", ""), ("child_two", "")]


@pytest.mark.parametrize("damage", ["item_zero", "split_run", "no_partition", "ignore_run_order", "drop_prefix", "short_tail", "partial_is_absent", "uninstalled_suffix"])
def test_t06_t07_t11_named_source_copy_negatives(tmp_path, damage):
    """Meaningful assertion RED, then the identical restored real-path witness."""
    from test_n04_reconcile_insert_after import _source_copy
    def install(mp):
        if damage == "item_zero":
            _damage(mp, "_attempt", "placement.predecessor_uuid, requests,", "placement.predecessor_uuid, requests[:1],")
        elif damage == "split_run":
            _damage(mp, "identifiable_runs", "if fingerprint in seen:", "if seen:")
        elif damage == "no_partition":
            _damage(mp, "identifiable_runs", "if fingerprint in seen:", "if False:")
        elif damage == "drop_prefix":
            _damage(mp, "_verify", "self.verified_prefix += tuple(", "discarded = tuple(")
        elif damage == "short_tail":
            from workflowy_mcp.client import carto_reconcile_cpu as cpu
            from workflowy_mcp.client import workflowy_move_reconcile as driver
            mp.setattr(driver, "_reconcile_steps", _source_copy(cpu, cpu, "_reconcile_steps", [(
                "(later_existing or long_tail)", "later_existing")]))
        elif damage == "partial_is_absent":
            mp.setattr(tc, "classify_create_run", _source_copy(tc, tc, "classify_create_run", [(
                'return AttemptClassification(ambiguous, post, detail="nonunique_or_unexpected_additions")',
                'return AttemptClassification(CreateClassification.CONCLUSIVELY_ABSENT, post)')]))
        elif damage == "uninstalled_suffix":
            from workflowy_mcp.client import carto_reconcile_cpu as cpu
            mp.setattr(cpu, "_adopt_create_order", _source_copy(cpu, cpu, "_adopt_create_order", [(
                '            if identifier not in order:\n                # A run\'s verified prefix is installed one source row at a time.\n                continue\n', '')]))
        else:
            mp.setattr(tc, "classify_create_run", _source_copy(tc, tc, "classify_create_run", [(
                "if projected[first:first + len(created_ids)] != created_ids:", "if False:")]))
    if damage == "ignore_run_order":
        witness = lambda path, mp: test_t06_k_rule_truth_table(4, "order")
    elif damage == "partial_is_absent":
        witness = lambda path, mp: test_t06_k_rule_truth_table(4, "partial")
    elif damage == "short_tail":
        witness = lambda path, mp: test_t07_maximal_run_is_one_ordered_insert(path, mp, 5, True)
    elif damage == "no_partition":
        witness = test_t07_repeat_boundary_is_maximal_and_preserves_all_nodes
    elif damage == "drop_prefix":
        witness = test_t11_fallback_failure_retains_only_verified_prefix
    elif damage == "uninstalled_suffix":
        witness = lambda path, mp: test_t08_a1_and_unknown_outcomes_on_runs(path, mp, [429, 429], 2)
    else:
        witness = lambda path, mp: test_t07_maximal_run_is_one_ordered_insert(path, mp, 9, False)
    with pytest.MonkeyPatch.context() as mp:
        install(mp)
        with pytest.raises(AssertionError):
            witness(tmp_path / "damaged", mp)
    with pytest.MonkeyPatch.context() as mp:
        witness(tmp_path / "restored", mp)


@pytest.mark.parametrize("geometry_name,count,index", [("app_js_scattered", 9, 5), ("server_py_group", 3, 128)])
def test_t07_named_large_parent_contiguous_run(tmp_path, monkeypatch, geometry_name, count, index):
    import test_n04_reconcile_insert_after as h
    original = getattr(h, geometry_name)()
    nodes, new_indices = [], []
    for i, row in enumerate(original.rows):
        nodes.append(h._node(row["name"], handle=row["id"]))
        if i == index:
            for added in range(count):
                new_indices.append(len(nodes))
                nodes.append(h._node(f"run_{added}"))
    geometry = h.Geometry(nodes, original.rows, [node["name"] for node in nodes], new_indices)
    run = h.run_refresh(tmp_path, monkeypatch, geometry=geometry)
    run.assertion_prelude()
    assert run.account.counts["llm_ops"] == 1 and run.account.counts["llm_items"] == count
    assert run.account.counts["move_top"] == 0
    assert run.account.contents() == [(name, "") for name in geometry.expected_names]


def _diagnostic_run_witness(tmp_path, monkeypatch, *, damage=False):
    import test_n04_reconcile_insert_after as h
    from workflowy_mcp.client import carto_reconcile_cpu as cpu
    from workflowy_mcp.client import workflowy_move_reconcile as driver
    entries = []
    changes = [("e = dict(entry)", "e = dict(entry); _n04_entries.append(dict(e))")]
    if damage:
        changes.append(('"op": "create",\n                                "status": "completed"',
                        '"op": "create",\n                                "status": "started"'))
    copied = h._source_copy(cpu, cpu, "_reconcile_steps", changes, extra_globals={"_n04_entries": entries})
    monkeypatch.setattr(driver, "_reconcile_steps", copied)
    run = h.run_refresh(tmp_path, monkeypatch, geometry=_run_geometry(["one", "two", "three"]))
    run.assertion_prelude()
    started = [entry for entry in entries if entry.get("op") == "create" and entry["status"] == "started"]
    completed = [entry for entry in entries if entry.get("op") == "create" and entry["status"] == "completed"]
    assert len(started) == len(completed) == 3
    assert [entry["source_path"] for entry in started] == [[1], [2], [3]]
    assert [entry["source_path"] for entry in completed] == [[1], [2], [3]]
    assert [entry["node_id"] for entry in completed] == run.account.created


def test_t11_run_keeps_each_node_diagnostic_pair_and_source_path(tmp_path, monkeypatch):
    _diagnostic_run_witness(tmp_path, monkeypatch)


def test_t11_diagnostic_pair_source_copy_red_then_green(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        with pytest.raises(AssertionError):
            _diagnostic_run_witness(tmp_path / "damaged", mp, damage=True)
    with pytest.MonkeyPatch.context() as mp:
        _diagnostic_run_witness(tmp_path / "restored", mp)


def test_t11_absent_run_then_v1_cancel_keeps_the_verified_prefix(tmp_path, monkeypatch):
    import test_n04_reconcile_insert_after as h
    class CancelSecond(h.FakeAccount):
        def _v1_create(self, request):
            if self.counts["v1_create"] == 1:
                self.cancel = asyncio.CancelledError("known-unsent second fallback item")
                raise self.cancel
            return super()._v1_create(request)
    account = CancelSecond()
    account.llm_script = [404]
    geometry = _run_geometry(["one", "two", "three"])
    run = h.run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account)
    assert run.error is account.cancel
    evidence = getattr(run.error, "reconcile_create_result", None)
    assert evidence is not None and evidence.created_ids == (account.created[0],)
    assert geometry.source_nodes[1]["id"] == account.created[0]
    assert not geometry.source_nodes[2].get("id")
    assert h.FILE in run.client._capture_view_and_dirty()[1]
    assert account.counts["llm_ops"] == account.counts["v1_create"] == 1
    assert account.counts["move_top"] == 0


@pytest.mark.parametrize("seam", ["first_candidate", "a1", "v1_unknown"])
def test_t06_shared_engine_retains_n03_counterfactual_teeth(tmp_path, seam):
    """Retargeted N03 source copies reach the actual engine, not its delegate."""
    import test_n03_transition_create as n03
    import test_n04_reconcile_insert_after as h
    def witness(path, mp):
        recovery = []
        async def schedule():
            recovery.append("fresh recovery scheduled")
        mp.setattr(tc, "_request_recovery", schedule)
        if seam == "first_candidate":
            baseline = tc._order(PARENT, [n03.row(PREDECESSOR, "left", 100)])
            evidence = {"recognized": True, "rows": [n03.row(PREDECESSOR, "left", 100),
                n03.row(n03.CREATED, "ours", 200), n03.row(n03.GUI, "ours", 300)]}
            result = tc.classify_create_attempt(PARENT, baseline, evidence, "ours", "",
                                                PlacementIntent.after(PREDECESSOR))
            assert result.classification == tc.CreateClassification.AMBIGUOUS
        elif seam == "a1":
            n03.test_a1_listing_auth_propagates_original_without_barrier(path, recovery)
        else:
            n03.test_final_v1_unknown_outcome_still_takes_mandatory_listing(path, mp, "network")
    with pytest.MonkeyPatch.context() as mp:
        if seam == "first_candidate":
            mp.setattr(tc, "classify_create_run", h._source_copy(tc, tc, "classify_create_run", [(
                "any(len(matches) != 1 for matches in matching)", "any(not matches for matches in matching)")]))
        elif seam == "a1":
            _damage(mp, "_attempt", "original_auth = response.response", "await self.client._handle_response(response.response)")
        else:
            _damage(mp, "_attempt", 'if transport == "llm" and not stage.dispatch_started:',
                    'if transport != "llm" or not stage.dispatch_started:')
        with pytest.raises(AssertionError):
            witness(tmp_path / "damaged", mp)
    with pytest.MonkeyPatch.context() as mp:
        witness(tmp_path / "restored", mp)


def _degraded_repeat_witness(tmp_path, monkeypatch):
    import test_n04_reconcile_insert_after as h
    geometry = _run_geometry(["a", "b", "a", "c"])
    account = h.FakeAccount()
    account.llm_script = ["malformed"]  # first subrun committed; capability degraded
    run = h.run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account)
    run.assertion_prelude()
    assert account.contents() == [(name, "") for name in geometry.expected_names]
    assert account.counts["llm_ops"] == 1 and account.counts["v1_create"] == 2
    assert account.counts["list"] == 1, "unavailable next subrun took Layer-C verification reads"
    assert account.counts["move_top"] == 6


def test_t08_unavailable_between_identifiable_subruns_is_bare_v1(tmp_path, monkeypatch):
    _degraded_repeat_witness(tmp_path, monkeypatch)


def test_t08_caller_repeat_partition_source_copy_red_then_green(tmp_path):
    import test_n04_reconcile_insert_after as h
    from workflowy_mcp.client import api_client_nexus as acn
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(acn.WorkFlowyClientNexus, "refresh_file_node_beacons",
            h._source_copy(acn, acn.WorkFlowyClientNexus, "refresh_file_node_beacons", [(
                "for group in TransitionCreateTransaction.identifiable_runs(self, requests):",
                "for group in (tuple(requests),):")]))
        with pytest.raises(AssertionError, match="unavailable next subrun"):
            _degraded_repeat_witness(tmp_path / "damaged", mp)
    with pytest.MonkeyPatch.context() as mp:
        _degraded_repeat_witness(tmp_path / "restored", mp)


def _rate_prefix_witness(tmp_path, monkeypatch):
    import test_n04_reconcile_insert_after as h
    class RateSecond(h.FakeAccount):
        def _v1_create(self, request):
            if self.counts["v1_create"] == 1:
                if not hasattr(self, "failure"):
                    self.failure = RateLimitError(retry_after=1)
                raise self.failure
            return super()._v1_create(request)
    account = RateSecond()
    account.llm_script = [404]
    run = h.run_refresh(tmp_path, monkeypatch, geometry=_run_geometry(["one", "two", "three"]), account=account)
    assert hasattr(account, "failure"), run.error
    assert run.error is account.failure or run.error.__cause__ is account.failure
    evidence = getattr(account.failure, "reconcile_create_result", None)
    assert evidence is not None, "an untagged rate failure discarded the verified prefix"
    assert evidence.created_ids == (account.created[0],)
    assert account.counts["llm_ops"] == 1 and account.counts["v1_create"] == 1
    assert not account.counts["move_top"]


def test_t11_untagged_rate_keeps_prefix_without_a1_resend(tmp_path, monkeypatch):
    _rate_prefix_witness(tmp_path, monkeypatch)


def test_t11_rate_prefix_source_copy_red_then_green(tmp_path):
    import test_n04_reconcile_insert_after as h
    from workflowy_mcp.client import api_client_nexus as acn
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(acn.WorkFlowyClientNexus, "refresh_file_node_beacons",
            h._source_copy(acn, acn.WorkFlowyClientNexus, "refresh_file_node_beacons", [(
                "                                await retain_prefix(error)\n",
                "                                pass\n")]))
        with pytest.raises(AssertionError, match="untagged rate failure discarded"):
            _rate_prefix_witness(tmp_path / "damaged", mp)
    with pytest.MonkeyPatch.context() as mp:
        _rate_prefix_witness(tmp_path / "restored", mp)
