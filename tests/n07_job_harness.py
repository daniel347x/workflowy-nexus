"""N07 isolated phase fixtures and real-client, HTTP-only job witnesses.

No fixture can connect to production. The integrated fixture constructs exactly
one client/governor/publisher and replaces only its HTTP transport, retaining
the production CRUD, cache bridge, parser, reconcile and token finalizers.
"""

from __future__ import annotations

import asyncio
import os
import threading
from urllib.parse import urlsplit

import pytest
import pytest_asyncio


TEST_WAIT_SECONDS = 10.0
SUITE_ENDPOINT = "https://n07.invalid/api/v1"
_INHERITED_NAMES = (
    "WORKFLOWY_API_KEY", "WORKFLOWY_NEXUS_CONFIG", "WORKFLOWY_BROKER_TOKEN_FILE",
    "WORKFLOWY_MCP_BEARER", "WORKFLOWY_MCP_HTTP_DIAL_HOST",
)


def assert_endpoint(endpoint, expected=SUITE_ENDPOINT, *, inherited_host=None):
    """Assert fixture identity BEFORE any fake/ephemeral transport is used."""
    actual, wanted = urlsplit(str(endpoint)), urlsplit(str(expected))
    assert actual == wanted, "N07 endpoint differs from its suite-owned fixture"
    assert actual.port not in (8765, 8766), "N07 refuses production ports"
    assert actual.hostname is not None
    if inherited_host:
        inherited = urlsplit(
            inherited_host if "://" in inherited_host else "http://" + inherited_host
        ).hostname
        assert actual.hostname != inherited, "N07 refuses the inherited dial host"


@pytest.fixture
def scrubbed_runtime(monkeypatch, tmp_path):
    """Remove inherited API/config/broker/bearer/dial values before client imports."""
    inherited_host = os.environ.get("WORKFLOWY_MCP_HTTP_DIAL_HOST")
    for name in _INHERITED_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WORKFLOWY_API_KEY", "n07-suite-fake-key-never-connect")
    assert_endpoint(SUITE_ENDPOINT, inherited_host=inherited_host)
    return tmp_path


class ThreadGate:
    """A fixture-local data worker paused until its test explicitly releases it."""

    def __init__(self, path):
        self.path = path
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0

    def write(self):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(TEST_WAIT_SECONDS), "test did not release the disk worker"
        self.path.write_text("joined disk work", encoding="utf-8")
        self.finished.set()
        return self.path

    async def wait_entered(self):
        assert await asyncio.wait_for(
            asyncio.to_thread(self.entered.wait, TEST_WAIT_SECONDS), TEST_WAIT_SECONDS + 1,
        ), "data worker never entered"


def install_carto_reservation(job_file, claim_id=None, *, jobs_root=None, root_uuid=None, job_id=None):
    """Install the actual empty-graph reservation the ownership door now requires.

    Direct context/task fixtures never run public admission, so they install the
    same registry entry admission would have created, under the fixture's own
    deterministic claim label. Production is untouched: the runner's exact
    validation and the region door both consume this real entry.
    """
    import hashlib
    import os
    from pathlib import Path
    from uuid import uuid4
    from workflowy_mcp import mutation_locks as locks
    job_file = Path(job_file)
    claim_id = claim_id or uuid4().hex
    root = Path(jobs_root) if jobs_root is not None else job_file.parent
    normalized_job_file = os.path.normcase(os.path.abspath(os.fspath(job_file)))
    if any(hold.get("job_file") == normalized_job_file and hold.get("claim_id") == claim_id
           for hold in locks._governance_holds(root)):
        return claim_id
    root_uuid = root_uuid or str(uuid4())
    locks._reserve_carto_hold(
        jobs_root=root, account_scope_id="a" * 32,
        footprint={"kind": "file", "path": None, "root_uuid": root_uuid,
                   "ancestor_chain": [root_uuid]},
        job_id=job_id or job_file.parent.name or "fixture-job",
        job_file=job_file, job_type="CARTO_REFRESH", mode="file",
        dedupe_key=hashlib.sha256(str(job_file).encode()).hexdigest(),
        launcher="system", claim_id=claim_id,
    )
    return claim_id


def job_context(path, control, *, claim_id=None):
    from workflowy_mcp.carto_job_runtime import CartoJobContext
    claim_id = install_carto_reservation(path / "job.json", claim_id)
    return CartoJobContext(path / "job.json", path, path / "job.log",
                           job_control=control, claim_id=claim_id)


def joined_file_progress_closure(path, name):
    """LE1PTI: extract the real now-async progress closure, never a reimplementation.

    The three old progress tests keep their assertions and synchronous test
    coordination. Only the moved execution lever joins the actual disk future.
    FILE and folder counters keep their original fixture-local state shapes.
    """
    import ast
    from pathlib import Path
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.carto_job_runtime import current_carto_job_context, run_carto_cpu
    tree = ast.parse(Path(nexus.__file__).read_text())
    definition = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    prefix = ('_carto_ops = 0\n_carto_pending = {"nodes_created": 0, "nodes_updated": 0, "nodes_moved": 0, "nodes_deleted": 0}\n_carto_retry_counts = {}\n'
              if name == "_carto_flush_progress" else
              '_carto_pending_nodes_created = 0\n_carto_bump_counter = 0\n_carto_retry_created = None\nfolder_context = None\n')
    setup = ast.parse(prefix).body
    tail = ast.parse(f'return {name}, locals()').body
    factory = ast.FunctionDef(name="factory", args=ast.arguments(
        posonlyargs=[], args=[ast.arg(arg="carto_job_file")], kwonlyargs=[], kw_defaults=[], defaults=[],
    ), body=setup + [definition] + tail, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[]))
    namespace = {"run_carto_cpu": run_carto_cpu, "current_carto_job_context": current_carto_job_context,
                 "_write_carto_file_artifact": nexus._write_carto_file_artifact}
    exec(compile(module, str(nexus.__file__), "exec"), namespace)
    closure, state = namespace["factory"](str(path))
    def joined(**kwargs):
        return asyncio.run(closure(**kwargs))
    return joined, state


def job_region_geometry():
    """Literal real/GUI disagreement; no production selector builds the oracle."""
    from n05_region_harness import graph_cache, node_uuid

    parents = {
        "vault": None, "folder": "vault", "file": "folder", "symbol": "file",
        "sibling": "file", "other-file": "folder", "moved-symbol": "other-file",
        "outside": "vault", "unmapped": "outside", "unmapped-child": "unmapped",
    }
    rows = graph_cache(parents)
    for label, note in (("folder", "Path: /n07-fixture/pkg"),
                        ("file", "Path: source.py"),
                        ("other-file", "Path: other.md")):
        rows[node_uuid(label)]["note"] = note
    return rows


@pytest_asyncio.fixture
async def real_job_environment(scrubbed_runtime, monkeypatch):
    """One real daemon client with a RegionHTTPProbe, never a sleeping job body.

    The local lifecycle object is not a server. Freshness transport and terminal
    healing have separate witnesses; this fixture uses one real warm lineage
    and borrows it for PRE/B0, suppressing only final-refresh delivery. All job
    writes use the real governor with an explicit 1 ms TEST pacing setting.
    """
    import json
    from types import MappingProxyType, SimpleNamespace
    import httpx
    from pydantic import SecretStr
    from coherence_harness import NOW
    from n05_region_harness import RegionHTTPProbe, node_uuid
    from test_coherence_s03_closure_repairs import _seed_lineage
    from workflowy_mcp import carto_job_governance as gov, daemon_lifecycle as lifecycle, mutation_locks as locks, server
    from workflowy_mcp.client import api_client_core as core, api_client_etch as etch, api_client_nexus as nexus
    from workflowy_mcp.carto_job_runtime import current_carto_job_context
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    from workflowy_mcp.models import APIConfiguration
    from workflowy_mcp.models.config import WriteGovernorConfiguration
    from workflowy_mcp.mutation_lease_governance import ReservedMutationParent

    root = scrubbed_runtime
    repo, jobs, snapshots, artifacts = (root / name for name in ("repo", "jobs", "snapshots", "artifacts"))
    for path in (repo, jobs, snapshots, artifacts):
        path.mkdir()
    monkeypatch.setattr(gov, "get_cartographer_jobs_dir", lambda: jobs)
    monkeypatch.setattr(core, "get_runtime_subdir", lambda *parts: snapshots if parts == ("nodes_export_cache",) else root.joinpath(*parts))
    monkeypatch.setattr(nexus, "get_cartographer_file_refresh_dir", lambda: artifacts)
    for module in (etch,):
        monkeypatch.setattr(module, "get_runtime_subdir", lambda *parts: root.joinpath(*parts))
    ids = {key: node_uuid("n07-http-" + key) for key in (
        "vault", "folder", "file", "symbol", "obsolete", "markdown", "heading",
        "persistent", "temporary", "missing", "outside", "outside-file", "other-file", "other-symbol", "broken",
    )}
    source = repo / "source.py"
    source.write_text("def existing():\n    return 2\n\ndef added():\n    return 3\n", encoding="utf-8")
    (repo / "other.py").write_text("def other():\n    return 9\n", encoding="utf-8")
    (repo / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    markdown = repo / "doc.md"
    markdown.write_text("# Heading\n\nold body\n", encoding="utf-8")
    outside_file = root / "peer.py"
    outside_file.write_text("def peer():\n    return 1\n", encoding="utf-8")
    nodes = [
        {"id": ids["vault"], "name": "Account", "parent_id": None},
        {"id": ids["folder"], "name": "📂 repo", "note": f"Root: {repo}", "parent_id": ids["vault"]},
        {"id": ids["file"], "name": "🐍 source.py", "note": "Path: source.py", "parent_id": ids["folder"]},
        {"id": ids["symbol"], "name": "ƒ existing() #n07", "note": "AST_QUALNAME: existing", "parent_id": ids["file"]},
        {"id": ids["obsolete"], "name": "ƒ obsolete()", "note": "AST_QUALNAME: obsolete", "parent_id": ids["file"]},
        {"id": ids["other-file"], "name": "🐍 other.py", "note": "Path: other.py", "parent_id": ids["folder"]},
        {"id": ids["other-symbol"], "name": "ƒ other() #n07", "note": "AST_QUALNAME: other", "parent_id": ids["other-file"]},
        {"id": ids["broken"], "name": "🐍 broken.py", "note": "Path: broken.py", "parent_id": ids["folder"]},
        {"id": ids["markdown"], "name": "📄 doc.md", "note": "Path: doc.md", "parent_id": ids["folder"]},
        {"id": ids["heading"], "name": "Heading", "note": "MD_PATH: Heading\n\nbody from Workflowy", "parent_id": ids["markdown"]},
        {"id": ids["persistent"], "name": "Capture", "note": "# Captured\n\nbody", "parent_id": ids["folder"]},
        {"id": ids["temporary"], "name": "Split", "note": "MD_PATH: Split\n\n# Split heading\n\nbody", "parent_id": ids["markdown"]},
        {"id": ids["missing"], "name": "🐍 missing.py", "note": "Path: missing.py", "parent_id": ids["folder"]},
        {"id": ids["outside"], "name": "Outside", "note": "original", "parent_id": ids["vault"]},
        {"id": ids["outside-file"], "name": "🐍 peer.py", "note": f"Path: {outside_file}", "parent_id": ids["vault"]},
    ]
    # Deliberately create a genuinely new FILE during folder discovery.
    (repo / "new.py").write_text("def discovered():\n    return 1\n", encoding="utf-8")
    assert_endpoint(SUITE_ENDPOINT)
    client = nexus.WorkFlowyClientNexus(APIConfiguration(api_key=SecretStr("n07-fixture-only"), base_url=SUITE_ENDPOINT),
        write_governor_config=WriteGovernorConfiguration(interval_seconds=0.001))
    paths = _seed_lineage(client, snapshots, nodes)
    assert client._load_nodes_export_cache_from_disk()
    publisher, governor = client._cache_view_publisher, client._write_governor
    monkeypatch.setattr(locks, "_LOCKS", locks._RegionRegistry())
    for name in ("_carto_inprocess_tasks", "_carto_job_controls", "_carto_operation_jobs", "_carto_final_obligations", "_carto_job_states", "_inprocess_restart_stops"):
        monkeypatch.setattr(server, name, {})
    monkeypatch.setattr(server, "_carto_startup_failed_jobs", MappingProxyType({}))
    for name in ("_carto_active_cache_jobs", "_carto_pending_terminal_cache_jobs"):
        monkeypatch.setattr(server, name, set())
    monkeypatch.setattr(server, "_carto_jobs_quiescent_event", asyncio.Event())
    monkeypatch.setattr(server, "_ensure_carto_final_delivery", lambda: None)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_governance_jobs_root", lambda: jobs)
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(jobs))
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    monkeypatch.setattr(server, "_rate_limiter", None)
    controller = lifecycle.DaemonLifecycle(identity=SimpleNamespace(warnings=()))
    controller.start()
    monkeypatch.setattr(lifecycle, "active_lifecycle", lambda: controller)
    events, tasks, controls = [], {}, {}
    loop_thread = threading.get_ident()

    def trace(kind, target=None):
        assert threading.get_ident() == loop_thread, "graph effect left the loop"
        context, snapshot = current_carto_job_context(), current_operation_cache(required=False)
        held = tuple(locks.held_mutation_lock_regions())
        if context is not None:
            assert client._cache_view_publisher is publisher and client._write_governor is governor
            if snapshot is not None:
                assert snapshot.owner is publisher
        event = {"kind": kind, "target": target, "job": context.job_control.presentation.get("id") if context else None,
                 "operation": snapshot.operation_id if snapshot else None, "held": held,
                 "task": asyncio.current_task(), "owner": context.job_control.task if context else None}
        events.append(event)
        return event

    probe = RegionHTTPProbe(nodes, held_regions=locks.held_mutation_lock_regions, reserved_parents=ReservedMutationParent)
    env = SimpleNamespace(path=root, repo=repo, jobs=jobs, paths=paths, client=client, probe=probe, ids=ids,
                          server=server, controller=controller, events=events, tasks=tasks, controls=controls,
                          trace=trace, send_gate=None, patch_gate=None, loop_thread=loop_thread)

    from contextlib import asynccontextmanager
    original_door = server.mutation_region_locks
    @asynccontextmanager
    async def observed_door(*args, **kwargs):
        async with original_door(*args, **kwargs) as grant:
            trace("grant-enter")
            try:
                yield grant
            finally:
                trace("grant-before-release")
    monkeypatch.setattr(server, "mutation_region_locks", observed_door)
    original_publish = publisher.publish_with_commit
    def published(*args, **kwargs):
        trace("publication-before")
        value = original_publish(*args, **kwargs)
        trace("publication-after")
        return value
    monkeypatch.setattr(publisher, "publish_with_commit", published)

    async def transport(request):
        assert_endpoint(str(request.url).split("/api/v1", 1)[0] + "/api/v1")
        if request.method != "GET":
            trace("send", request.url.path)
            if env.send_gate is not None:
                await env.send_gate(request)
        result = await probe.request(request)
        if request.method != "GET":
            trace("response", request.url.path)
        return result
    client._client = httpx.AsyncClient(base_url=SUITE_ENDPOINT, transport=httpx.MockTransport(transport))
    for name in ("_apply_create_to_nodes_export_cache", "_apply_update_to_nodes_export_cache", "_apply_delete_to_nodes_export_cache", "_apply_move_to_nodes_export_cache"):
        original = getattr(client, name)
        def patched(*args, _name=name, _original=original, **kwargs):
            target = getattr(args[0], "id", args[0])
            trace("cache-before", target)
            value = _original(*args, **kwargs)
            trace("cache-after", target)
            if env.patch_gate is not None:
                env.patch_gate(_name, target)
            return value
        monkeypatch.setattr(client, name, patched)
    original_dirty = client._mark_nodes_export_dirty
    def dirty(*args, **kwargs):
        trace("dirty-patch")
        return original_dirty(*args, **kwargs)
    monkeypatch.setattr(client, "_mark_nodes_export_dirty", dirty)
    async def selection(**kwargs):
        trace("freshness")
        return await server._carto_select_live_basis(client, "performed")
    monkeypatch.setattr(server, "_carto_select_snapshot", selection)
    async def noop(*args, **kwargs):
        pass
    monkeypatch.setattr(server, "_await_nodes_export_cache_quiescent", noop)
    monkeypatch.setattr(server, "_refresh_carto_job_tracker_once", noop)
    schedule = server._schedule_carto_job
    async def scheduled(spec):
        result = await schedule(spec)
        if result.get("success"):
            tasks[spec.job_id], controls[spec.job_id] = server._carto_inprocess_tasks[spec.job_id], server._carto_job_controls[spec.job_id]
            trace("ack", spec.job_id)
        return result
    monkeypatch.setattr(server, "_schedule_carto_job", scheduled)
    for name in ("finalize_claim_async", "finalize_cancelled_job_claim_async"):
        original = getattr(server, name)
        async def finalized(*args, _original=original, **kwargs):
            trace("finalizer-enter")
            result = await _original(*args, **kwargs)
            trace("finalizer-exit")
            return result
        monkeypatch.setattr(server, name, finalized)
    def forbidden(*args, **kwargs):
        raise AssertionError("real N07 job reached a second client or legacy worker route")
    # The retired job-environment snapshot adapter is absent in production; the
    # capability fence asserts that absence rather than patching a dead name.
    monkeypatch.setattr(nexus.WorkFlowyClientNexus, "__init__", forbidden)
    monkeypatch.setattr(core.WorkFlowyClientCore, "__init__", forbidden)
    monkeypatch.setattr(client, "set_operation_cancel_callback", forbidden)
    env.initial_nodes = nodes
    try:
        yield env
    finally:
        # Tests release deterministic gates in their own finally. Never raw
        # cancel a job, and never let fixture teardown hide a runner exception.
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks.values()), TEST_WAIT_SECONDS)
        await client._client.aclose()


async def launch_real_job(env, family):
    """Invoke actual public fronts (no fake body/selector/claim/lock factory)."""
    import json
    from types import SimpleNamespace
    server, ids = env.server, env.ids
    replies = []
    async def send(text):
        replies.append(json.loads(text))
    websocket = SimpleNamespace(send=send)
    if family in {"file", "ast"}:
        key = "symbol" if family == "ast" else "file"
        await server._handle_refresh_file_node({"node_id": ids[key], "force": True}, websocket)
    elif family == "folder":
        await server._handle_refresh_folder_node({"node_id": ids["folder"], "force": True}, websocket)
    elif family in {"persistent_new", "temp_override"}:
        await server._handle_materialize_markdown_file({"node_id": ids["persistent" if family == "persistent_new" else "temporary"],
            "default_markdown_folder": str(env.repo)}, websocket)
    elif family == "markdown":
        await server._handle_generate_markdown_file({"node_id": ids["markdown"]}, websocket)
    elif family == "bulk-markdown":
        await server._handle_bulk_generate_markdown({"node_id": ids["markdown"], "walk_mode": "all"}, websocket)
        assert replies[0]["launched"] == 1 and not replies[0]["errors"], replies
        return {"success": True, **replies[0]["results"][0]}
    elif family == "bulk-materialize":
        await server._handle_bulk_materialize_markdown({"node_id": ids["persistent"], "walk_mode": "all",
            "default_markdown_folder": str(env.repo)}, websocket)
        assert replies[0]["launched"] == 1, replies
        return replies[0]["results"][0]
    elif family == "public-bulk":
        await server._handle_bulk_apply_visible_nodes({"root_uuid": ids["file"], "root_mode": "file"}, websocket)
    elif family in {"map", "legacy"}:
        from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
        async with cache_operation_boundary(env.client, "n07:mcp-refresh-front"):
            if family == "map":
                return await server._beacon_map_refresh_impl(node_handle=ids["file"], mode="file", force=True)
            return await server.cartographer_refresh_async(root_uuid=ids["file"], mode="file")
    elif family in {"bulk", "bulk-groups"}:
        from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
        async with cache_operation_boundary(env.client, "n07:bulk-front"):
            if family == "bulk-groups":
                return await server._start_carto_bulk_visible_apply_job(root_uuid=ids["folder"], mode="folder", visible_tree_payload={
                    "root": {"id": ids["folder"], "name": "repo", "note": f"Root: {env.repo}"},
                    "children": [
                        {"id": ids["file"], "name": "source.py", "note": "Path: source.py", "children": [
                            {"id": ids["symbol"], "name": "ƒ existing() #n07", "note": "AST_QUALNAME: existing"}]},
                        {"id": ids["other-file"], "name": "other.py", "note": "Path: other.py", "children": [
                            {"id": ids["other-symbol"], "name": "ƒ other() #n07", "note": "AST_QUALNAME: other"}]},
                    ],
                })
            return await server._start_carto_bulk_visible_apply_job(root_uuid=ids["file"], mode="file", visible_tree_payload={
                "root": {"id": ids["file"], "name": "source.py", "note": "Path: source.py"},
                # Cached other-FILE parent intentionally disagrees with GUI nesting.
                "children": [{"id": ids["symbol"], "name": "ƒ existing() #n07", "note": "AST_QUALNAME: existing"},
                             {"id": ids["other-symbol"], "name": "ƒ other() #n07", "note": "AST_QUALNAME: other"}],
            })
    elif family == "peer":
        from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
        async with cache_operation_boundary(env.client, "n07:peer-front"):
            return await server._start_carto_refresh_job(ids["outside-file"], "file")
    else:
        raise AssertionError(f"unknown real family {family}")
    assert len(replies) == 1, replies
    return replies[0]


async def real_note_edit(env, key, text):
    """Actual interactive exact-node read/write funnel, independently pinned."""
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    async with cache_operation_boundary(env.client, "n07:outside-edit"):
        return await env.server._node_edit_impl(env.ids.get(key, key), note=text, return_diff=False)


async def wait_job_gate(env, event, *, job_id=None):
    """Do not hide a terminal failure behind a fixture's gate timeout."""
    waiter = asyncio.create_task(event.wait())
    jobs = [env.tasks[job_id]] if job_id else list(env.tasks.values())
    try:
        done, _ = await asyncio.wait([waiter, *jobs], timeout=TEST_WAIT_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        assert waiter in done, {key: value.terminal_result for key, value in env.controls.items()}
    finally:
        if not waiter.done():
            waiter.cancel()  # Test-owned notification only, never a job.
        await asyncio.gather(waiter, return_exceptions=True)


def reject_raw_job_cancel(env):
    """Trap the actual task method, not merely a launcher callback spy."""
    from contextlib import contextmanager
    @contextmanager
    def trap():
        loop = asyncio.get_running_loop()
        previous = loop.get_task_factory()
        assert previous is None, "fixture must not replace another task factory"
        class CooperativeJobTask(asyncio.Task):
            def cancel(self, *args, **kwargs):
                assert self not in env.tasks.values(), "production called raw Task.cancel on a job"
                return super().cancel(*args, **kwargs)
        loop.set_task_factory(lambda loop, coroutine, **kwargs: CooperativeJobTask(coroutine, loop=loop, **kwargs))
        try:
            yield
        finally:
            loop.set_task_factory(previous)
    return trap()


@pytest.fixture
def task_core_environment(scrubbed_runtime, monkeypatch):
    """Real N01/N05/N06 core; no public launch, claim process or HTTP endpoint.

    Claim finalizers are spies here because the reverse claim consult remains
    connected until all actual launches have real grants. These tests prove
    the new ownership core, not complete C01/C03 token/family integration.
    """
    from types import SimpleNamespace
    from coherence_harness import make_boundary_capable
    from workflowy_mcp import daemon_lifecycle as lifecycle, mutation_locks as locks, server
    from workflowy_mcp.coherence.operation_cache import current_operation_cache

    monkeypatch.setenv(lifecycle.DRAIN_TIMEOUT_ENV, "3600")
    monkeypatch.setenv(lifecycle._CALL_TIMEOUT_ENV, "0")
    monkeypatch.setattr(locks, "_LOCKS", locks._RegionRegistry())
    monkeypatch.setattr(server, "_carto_inprocess_tasks", {})
    monkeypatch.setattr(server, "_carto_job_controls", {})
    monkeypatch.setattr(server, "_carto_operation_jobs", {})
    monkeypatch.setattr(server, "_carto_final_obligations", {})
    monkeypatch.setattr(server, "_carto_active_cache_jobs", set())
    monkeypatch.setattr(server, "_carto_pending_terminal_cache_jobs", set())
    monkeypatch.setattr(server, "_carto_job_states", {})
    monkeypatch.setattr(server, "_carto_jobs_quiescent_event", asyncio.Event())
    live_final_delivery = server._ensure_carto_final_delivery
    # These core/phase fixtures explicitly do not run final-refresh transport.
    # Final-refresh/D09 fixtures restore the saved real handoff separately.
    monkeypatch.setattr(server, "_ensure_carto_final_delivery", lambda: None)
    from types import MappingProxyType
    monkeypatch.setattr(server, "_carto_startup_failed_jobs", MappingProxyType({}))
    monkeypatch.setattr(server, "_inprocess_restart_stops", {})
    controller = lifecycle.DaemonLifecycle(identity=SimpleNamespace(warnings=()))
    controller.start()  # Lifecycle state only; no daemon, server or transport starts.
    monkeypatch.setattr(lifecycle, "active_lifecycle", lambda: controller)
    client = make_boundary_capable(SimpleNamespace(_write_governor=SimpleNamespace(
        paths=SimpleNamespace(jobs_root=scrubbed_runtime / "claims", account_scope_id="a" * 32),
    )), nodes=job_region_geometry().values())
    monkeypatch.setattr(server, "get_client", lambda: client)
    events = []

    def trace(kind):
        snapshot = current_operation_cache(required=False)
        state = locks.MUTATION_LOCK_STATE.get()
        held = tuple(r for h in state.holds if not h.released for r in h.regions) if state else ()
        events.append((kind, snapshot.operation_id if snapshot else None, held, asyncio.current_task()))

    async def barrier(paths, *, operation):
        trace("barrier")
    monkeypatch.setattr("workflowy_mcp.coherence.admission.require_coherence_mutation_admission", barrier)

    async def finalize(*args, **kwargs):
        trace("finalize")
    async def cancel_finalize(*args, **kwargs):
        trace("cancel-finalize")
    monkeypatch.setattr(server, "finalize_claim_async", finalize)
    monkeypatch.setattr(server, "finalize_cancelled_job_claim_async", cancel_finalize)
    # Every directly scheduled core spec is validated against its actual
    # admitted reservation before the runner's first joined status write.
    schedule = server._schedule_carto_job
    async def admitted_schedule(spec):
        install_carto_reservation(spec.job_file, spec.claim_id, job_id=spec.job_id)
        return await schedule(spec)
    monkeypatch.setattr(server, "_schedule_carto_job", admitted_schedule)
    return SimpleNamespace(path=scrubbed_runtime, server=server, client=client,
                           controller=controller, events=events, trace=trace,
                           live_final_delivery=live_final_delivery,
                           live_final_tracker=server._refresh_carto_job_tracker_once)


@pytest.fixture
def claimed_task_environment(task_core_environment, monkeypatch):
    """Actual admitted claims/finalizers plus the real task/region/lifecycle core.

    Work transport remains test-owned. No server, socket, worker or client is
    constructed; final-refresh tracking is explicitly outside this fixture.
    """
    from workflowy_mcp import carto_job_governance as gov
    env = task_core_environment
    server = env.server
    jobs = env.client._write_governor.paths.jobs_root
    jobs.mkdir()
    monkeypatch.setattr(server, "_governance_jobs_root", lambda: jobs)
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(jobs))
    env.client._coherence_paths = lambda: None
    env.jobs = jobs
    async def selection(**kwargs):
        env.trace("selection")
        return {"snapshot_path": str(jobs / "diagnostic-only-base.json"),
                "freshness_disposition": "performed",
                "cache_by_id": env.client._cache_view_publisher.capture().nodes_by_id,
                 "snapshot_lineage": {"account_scope_id": "a" * 32}}
    env.live_server_selection = server._carto_select_snapshot
    monkeypatch.setattr(server, "_carto_select_snapshot", selection)
    async def noop(*args, **kwargs):
        pass
    monkeypatch.setattr(server, "_await_nodes_export_cache_quiescent", noop)
    monkeypatch.setattr(server, "_refresh_carto_job_tracker_once", noop)
    async def finalize(*args, **kwargs):
        env.trace("real-finalize")
        return await gov.finalize_claim_async(*args, **kwargs)
    async def cancelled(*args, **kwargs):
        env.trace("real-cancel-finalize")
        return await gov.finalize_cancelled_job_claim_async(*args, **kwargs)
    monkeypatch.setattr(server, "finalize_claim_async", finalize)
    monkeypatch.setattr(server, "finalize_cancelled_job_claim_async", cancelled)
    def forbidden(*args, **kwargs):
        raise AssertionError("daemon task reached a worker env, snapshot bootstrap or spawner")
    return env


def install_legacy_task_fixture(monkeypatch, *, jobs, account, client=None):
    """Move legacy boundary/error fixtures onto the actual scheduler (71UWQD).

    Those fixtures deliberately use non-UUID sample graphs and fake ALL work;
    their real door holds a test-only AP-core token, not a fabricated graph
    proof. Actual-root/transport coverage belongs to the N07 graph fixtures.
    Existing boundary, failure, token/supervisor and reset assertions survive.
    """
    from types import SimpleNamespace
    from coherence_harness import make_boundary_capable
    from workflowy_mcp import carto_job_governance as gov, server
    from workflowy_mcp.mutation_lease_governance import build_region_ap_core
    from workflowy_mcp.mutation_locks import held_mutation_lock_regions
    if client is None:
        from coherence_harness import make_coherence_root
        paths = make_coherence_root(jobs.parent / "legacy-task-boundary", account)
        client = make_boundary_capable(SimpleNamespace(_write_governor=SimpleNamespace(
            paths=SimpleNamespace(jobs_root=jobs, account_scope_id=account),
        ), _coherence_paths=lambda: paths))
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_carto_account_scope", lambda: account)
    monkeypatch.setattr(server, "_governance_jobs_root", lambda: jobs)
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(jobs))
    monkeypatch.setattr(server, "_carto_final_obligations", {})
    monkeypatch.setattr(server, "_carto_active_cache_jobs", set())
    monkeypatch.setattr(server, "_carto_pending_terminal_cache_jobs", set())
    monkeypatch.setattr(server, "_carto_job_states", {})
    monkeypatch.setattr(server, "_carto_jobs_quiescent_event", asyncio.Event())
    monkeypatch.setattr(server, "_ensure_carto_final_delivery", lambda: None)
    monkeypatch.setattr(server, "_carto_job_regions", lambda spec, proof: (build_region_ap_core("N07LEG"),))
    calls = []
    async def fake_work(**kwargs):
        assert held_mutation_lock_regions(), "legacy task fixture lost its actual grant"
        from workflowy_mcp import mutation_locks as locks
        calls.append({**kwargs, "holds": tuple(
            hold for hold in locks._governance_holds(jobs) if hold["record_kind"] == "carto"
        )})
        return {"success": True}
    monkeypatch.setattr(client, "refresh_file_node_beacons", fake_work, raising=False)
    monkeypatch.setattr(client, "refresh_folder_cartographer_sync", fake_work, raising=False)
    # Old tests spy on the governance authority; server's imported references
    # must forward to those current spies rather than bypassing their seam.
    async def finalize(*args, **kwargs):
        return await gov.finalize_claim_async(*args, **kwargs)
    async def cancel_finalize(*args, **kwargs):
        return await gov.finalize_cancelled_job_claim_async(*args, **kwargs)
    monkeypatch.setattr(server, "finalize_claim_async", finalize)
    monkeypatch.setattr(server, "finalize_cancelled_job_claim_async", cancel_finalize)
    tasks = []
    async def schedule(spec):
        result = await server._schedule_carto_job(spec)
        task = server._carto_inprocess_tasks.get(spec.job_id)
        if task is not None:
            tasks.append(task)
        return result
    return SimpleNamespace(client=client, calls=calls, tasks=tasks, schedule=schedule)


def reconcile_oracle_cases():
    """Literal C05 inputs; one delete root removes hash-sensitive set ordering.

    Expected outputs are captured from H's unchanged reconciler, NOT computed
    from the extracted engine. In particular these fixtures preserve H's
    existing reorder swarm and Option B behavior rather than improving either.
    """
    from copy import deepcopy

    def source(node_id, name, children=(), **extra):
        return {"id": node_id, "name": name, "note": "", "children_status": "complete",
                "children": list(children), **extra}

    def target(node_id, name, parent="ROOT", priority=100, **extra):
        return {"id": node_id, "name": name, "note": "", "parent_id": parent,
                "priority": priority, **extra}

    cases = {}
    def add(name, nodes, rows, *, root="ROOT", ledger=(), **options):
        cases[name] = {"source": {"export_root_id": root, "nodes": deepcopy(nodes),
                                  "original_ids_seen": list(ledger)},
                       "target": deepcopy(rows), "options": options}

    add("create_ids_and_synthetic_priorities",
        [source(None, "new top", [source(None, "new child")], _position_hint="top"),
         source("A", "existing")], [target("A", "existing", priority=700)])
    add("create_dry_run", [source(None, "new top"), source("A", "existing")],
        [target("A", "existing", priority=700)], dry_run=True)
    moving = [source("P2", "second", [source("A", "moving")]), source("P1", "first")]
    rows = [target("P1", "first"), target("P2", "second", priority=200),
            target("A", "moving", "P1")]
    add("cross_parent_move", moving, rows)
    add("cross_parent_dry_run", moving, rows, dry_run=True)
    # Intentionally export in reverse priority order: snapshot sorting matters.
    reversed_rows = [target("B", "second", priority=200), target("A", "first")]
    add("pure_reorder", [source("B", "second"), source("A", "first")], reversed_rows)
    add("skipped_reorder", [source("B", "second"), source("A", "first")],
        reversed_rows, allow_pure_reorder=False)
    add("normalized_noop", [source("A", "  A & B  ", note=" x > y ", data={"layoutMode": "x"})],
        [target("A", "A &amp; B", note="x &gt; y", data={"layoutMode": "y"})])
    add("content_update", [source("A", "new", note="new note", completed=True)],
        [target("A", "old", note="old note", completed=False)])
    deleting = [target("P", "removed parent"), target("D", "removed child", "P")]
    for name, options in (("option_b_delete", {}), ("delete_threshold", {"max_delete_threshold": 0}),
                          ("delete_force", {"max_delete_threshold": 0, "force": True}),
                          ("delete_dry_run", {"dry_run": True})):
        add(name, [], deleting, ledger=("P", "D"), **options)
    add("no_ledger_no_delete", [], deleting)
    add("explicit_preservation", [], deleting, ledger=("P", "D"))
    cases["explicit_preservation"]["source"]["explicitly_preserved_ids"] = ["D"]
    add("truncated_ledger", [source("P", "parent", children_status="partial")],
        [target("P", "parent"), target("D", "visible then removed", "P")], ledger=("P", "D"))
    cases["truncated_ledger"]["source"]["export_root_children_status"] = "partial"
    add("truncated_unknown", [source("P", "parent", children_status="partial")],
        [target("P", "parent"), target("D", "never loaded", "P")])
    add("strict_mismatch", [source("A", "new")], [target("A", "old", "OTHER")], root="OTHER")
    add("rebase", [source("A", "new")], [target("A", "old", "OTHER")], root="OTHER", import_policy="rebase")
    add("clone", [source("A", "clone")], [target("A", "old")], import_policy="clone", ledger=("A",))
    add("cancel_prefix", [source(None, "one"), source(None, "two"), source(None, "three")], [], stop_after=1)
    add("cancel_interval", [source(None, "one"), source(None, "two"), source(None, "three")], [],
        stop_after=1, cancel_check_interval=3)
    add("cancel_probe_failure", [source("A", "new")], [target("A", "old")], probe_error=True)
    add("bfs_snapshot", [source("P", "parent", [source("A", "same")])],
        [target("P", "parent"), target("A", "same", "P")], bfs=True)
    add("existing_export_wait", [], [], skip_delete_bulk_export_wait=False)
    for operation in ("create", "move", "update", "delete", "export"):
        template = {"create": "create_ids_and_synthetic_priorities", "move": "cross_parent_move",
                    "update": "content_update", "delete": "option_b_delete", "export": "content_update"}[operation]
        cases[operation + "_exception"] = deepcopy(cases[template])
        cases[operation + "_exception"]["options"]["fail_operation"] = operation
    return cases


async def run_reconcile_oracle(case, monkeypatch, *, engine=None):
    """Record exact request bytes/order/plans with no network or real clock wait.

    N04: create_node receives exactly one plain ReconcileCreateRun per request
    and must return one plain ReconcileCreateResult. The fake below owns only
    the bare-v1 boundary — it appends a bottom create and honours an explicit
    top, implements no insert-after, and returns no verified parent listing — so
    its `physical_placements` states what physically happened and the engine has
    to model the resulting parent order from that, not from a made-up order.
    """
    from copy import deepcopy
    from dataclasses import asdict
    from datetime import datetime
    import json
    from workflowy_mcp.client import workflowy_move_reconcile as reconcile
    from workflowy_mcp.client.carto_reconcile_cpu import ReconcileCreateRun, ReconcileCreateResult

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2020, 1, 2, 3, 4, 5, 678000, tzinfo=tz)

    monkeypatch.setattr(reconcile, "datetime", FrozenClock, raising=False)
    # The extracted module is absent during H capture; no engine-dependent oracle.
    import sys
    cpu = sys.modules.get("workflowy_mcp.client.carto_reconcile_cpu")
    if cpu is not None:
        monkeypatch.setattr(cpu, "datetime", FrozenClock)
    real_sleep = asyncio.sleep
    events, journals, diagnostics = [], [], []
    store = {row["id"]: deepcopy(row) for row in case["target"]}
    source = deepcopy(case["source"])
    options = dict(case["options"])
    failed_operation = options.pop("fail_operation", None)
    stop_after = options.pop("stop_after", None)
    probe_error = options.pop("probe_error", False)
    bfs = options.pop("bfs", False)
    writes = 0

    def record(kind, *args, **kwargs):
        nonlocal writes
        # JSON serialization is an exact, scalar-preserving payload oracle.
        events.append(json.loads(json.dumps([kind, args, kwargs], ensure_ascii=False)))
        if kind == failed_operation:
            raise ValueError("fixture " + kind + " failed")
        if kind in {"move", "update", "delete"}:
            writes += 1

    async def listing(parent):
        record("list", parent)
        return deepcopy([row for row in store.values() if row["parent_id"] == parent])

    async def export(parent):
        record("export", parent)
        return {"nodes": deepcopy(list(store.values()))}

    created_total = 0

    def place(parent, node_id, payload, placement):
        """Store the row where this boundary create physically put it."""
        siblings = [row for row in store.values() if row["parent_id"] == parent]
        if placement == "top":
            priorities = [int(row.get("priority") or 0) for row in siblings]
            priority, reported = ((min(priorities) - 100) if priorities else 0), "top"
        else:
            # No insert-after here: the bare-v1 boundary appends, and the
            # reported placement is that physical bottom, never the wish.
            priority = max([int(row.get("priority") or 0) for row in siblings] or [0]) + 100
            reported = "bottom"
        store[node_id] = {"id": node_id, "parent_id": parent, "priority": priority,
                          **deepcopy(payload)}
        return reported

    async def create(run):
        nonlocal writes, created_total
        assert isinstance(run, ReconcileCreateRun), (
            "N04 driver must hand create_node exactly one plain ReconcileCreateRun")
        record("create", asdict(run))  # a failing create raises before any row exists
        writes += len(run.items)  # stop_after counts created nodes, never events
        created_ids, placements = [], []
        for payload in run.items:
            created_total += 1
            node_id = "CREATED-" + str(created_total)
            created_ids.append(node_id)
            placements.append(place(run.parent_uuid, node_id, payload, run.placement))
        return ReconcileCreateResult(tuple(created_ids), tuple(placements), outcome="v1_boundary")

    async def conclude(parent_uuid):
        """Normal parent conclusion for parents with new nodes.

        The driver also calls this once with None in its finally block to close
        whatever create transaction it opened; that is transport life-cycle, not
        a normal parent-done request, so it is never recorded here (recording it
        would move every H-preserved digest).
        """
        if parent_uuid is None:
            return
        record("create_parent_done", parent_uuid)

    async def move(node_id, parent, *, position):
        record("move", node_id, parent, position=position)
        store[node_id]["parent_id"] = parent

    async def update(node_id, payload):
        record("update", node_id, payload)
        store[node_id].update(deepcopy(payload))

    async def delete(node_id):
        record("delete", node_id)
        store.pop(node_id)

    def cancelled():
        record("cancel_check")
        if probe_error:
            raise ValueError("fixture cancel probe failed")
        return stop_after is not None and writes >= stop_after

    async def sleep(seconds):
        if seconds:
            record("sleep", seconds)
        await real_sleep(0)

    def diagnostic(message):
        if "[PHASE " in message or "[CANCEL]" in message or "FAILED" in message or "cancel_callback raised" in message:
            diagnostics.append(message)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    settings = {"skip_delete_bulk_export_wait": True, "allow_pure_reorder": True,
                "cancel_check_interval": 1, **options}
    error = None
    try:
        result = await (engine or reconcile.reconcile_tree)(
            source, "ROOT", listing, create, update, delete, move,
            export_nodes=None if bfs else export, cancel_callback=cancelled,
            log_weave_entry=journals.append, log_to_file_msg=diagnostic,
            conclude_create_parent=conclude, **settings,
        )
    except Exception as exc:
        result, error = None, [type(exc).__name__, str(exc)]
    return {"requests": events, "plan": result, "journal": journals,
            "diagnostics": diagnostics, "error": error, "source_after": source}
