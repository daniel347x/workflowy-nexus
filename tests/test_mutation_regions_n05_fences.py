"""N05 raw/W4/reader fences and real-HTTP interactive footprint witnesses."""

from __future__ import annotations

import ast
import asyncio
import json
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path

import pytest

from test_mutation_leases_s05_fences import _held_consult_offenders


def legacy_region_fronts(monkeypatch, tmp_path, *, nodes, entries):
    """Named direct-impl boundary fixtures, with the real plane still live."""
    from coherence_harness import make_boundary_capable
    from n05_region_harness import isolate_claim_scope
    from workflowy_mcp import server
    from workflowy_mcp.coherence.cache_view import CacheViewPublisher
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    for name in entries:
        original = getattr(server, name)
        @wraps(original)
        async def within_boundary(*args, __original=original, **kwargs):
            try:
                client = server.get_client()
            except RuntimeError:
                return await __original(*args, **kwargs)  # pure validation, no client installed
            if not isinstance(getattr(client, "_cache_view_publisher", None), CacheViewPublisher):
                make_boundary_capable(client, nodes=nodes)
            isolate_claim_scope(client, tmp_path)
            async with cache_operation_boundary(client, "n05-legacy-fixture"):
                return await __original(*args, **kwargs)
        monkeypatch.setattr(server, name, within_boundary)


def real_region_probe(tmp_path, monkeypatch, *, completed_source=False):
    """Real lineage/client/governor; production-aware setup stays out of harness."""
    import httpx
    from unittest.mock import AsyncMock
    from n05_region_harness import node_uuid, RegionHTTPProbe
    from test_coherence_s03_closure_repairs import _make_lineage_nexus_client
    from workflowy_mcp import server, mutation_locks
    from workflowy_mcp.mutation_lease_governance import ReservedMutationParent
    ids = {name: node_uuid("http-" + name) for name in ("root", "origin", "dest", "source", "plain", "ap")}
    nodes = [
        {"id": ids["root"], "name": "Root", "parent_id": None},
        {"id": ids["origin"], "name": "Session #wfy-ns", "parent_id": ids["root"]},
        {"id": ids["dest"], "name": "Topic #wfy-ns", "parent_id": ids["root"]},
        {"id": ids["source"], "name": "Source #old", "note": "one\ntwo\nthree", "parent_id": ids["origin"]},
        {"id": ids["plain"], "name": "Plain #old", "parent_id": ids["origin"]},
        {"id": ids["ap"], "name": "[ap:OLD701] Prior #old", "parent_id": ids["origin"]},
    ]
    if completed_source:
        nodes[3].update(completed=True, completedAt=200)
        ids["child"] = node_uuid("http-completed-child")
        nodes.append({"id": ids["child"], "name": "Completed child", "note": "child",
                      "parent_id": ids["source"], "completed": True, "completedAt": 200})
    client, paths = _make_lineage_nexus_client(tmp_path, monkeypatch, nodes=nodes)
    probe = RegionHTTPProbe(nodes, held_regions=mutation_locks.held_mutation_lock_regions, reserved_parents=ReservedMutationParent)
    client._client = httpx.AsyncClient(base_url="https://workflowy.com/api/v1", transport=httpx.MockTransport(probe.request))
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    monkeypatch.setattr(server, "_rate_limiter", None)
    monkeypatch.setattr(server, "_ANCHOR_AP_ID_RESERVED_CORES", set())
    monkeypatch.setattr(server, "_ANCHOR_AP_ID_ALLOCATED_CORES", set())
    monkeypatch.setattr(mutation_locks, "_LOCKS", mutation_locks._RegionRegistry())
    monkeypatch.setattr(server, "_resolve_note_session_config", AsyncMock(return_value=(
        ["wfy-ns"], [], ids["dest"], {"note_session_local_postfixes": ["local"]},
    )))
    monkeypatch.setattr(server, "_resolve_slice_group_config", AsyncMock(return_value=(
        [], None, {"auto_expand": "expand"}, [], {},
    )))
    return client, probe, ids


def _sources():
    root = Path(__file__).parent.parent / "src"
    return [(p.relative_to(root).as_posix(), p.read_text(encoding="utf-8")) for p in (root / "workflowy_mcp").rglob("*.py")]


def _call_offenders(pairs, targets):
    """Follow callable/module aliases and constant getattr; reject escapes."""
    offenders = []
    for rel, text in pairs:
        tree = ast.parse(text)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        module = rel.removesuffix(".py").replace("/", ".")
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    aliases[item.asname or item.name.split(".")[0]] = item.name if item.asname else item.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    prefix = ".".join([*module.split(".")[:-node.level], *([prefix] if prefix else [])])
                for item in node.names:
                    aliases[item.asname or item.name] = prefix + "." + item.name
        def resolve(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return resolve(node.value) + "." + node.attr
            if isinstance(node, ast.Call) and resolve(node.func) == "getattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                return resolve(node.args[0]) + "." + str(node.args[1].value)
            return "<dynamic>"
        for _ in range(len(list(ast.walk(tree))) + 1):
            changed = False
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute, ast.Call)):
                    value = resolve(node.value)
                    if value == "<dynamic>":
                        continue
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id not in aliases:
                            aliases[target.id] = value
                            changed = True
            if not changed:
                break
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Name, ast.Attribute, ast.Call)):
                continue
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                continue
            name = resolve(node).rsplit(".", 1)[-1]
            if name not in targets:
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Assign) and parent.value is node:
                continue
            if not isinstance(parent, ast.Call) or parent.func is not node:
                offenders.append(f"{rel}:{node.lineno} {name} callable escape")
                continue
            ancestry, cursor = [], parent
            while cursor in parents:
                cursor = parents[cursor]
                if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    ancestry.append(getattr(cursor, "name", "<lambda>"))
            origin = (rel, ".".join(reversed(ancestry)))
            # N07 CF33 keeps proof capture lexically inside the orchestrator.
            # Only that exact callback may rebase; it may not establish a new
            # admission or acquire raw regions (conductor ruling GLYSXW).
            proof_callback = (
                name == "rebase_during_admission"
                and origin == ("workflowy_mcp/mutation_locks.py", "mutation_region_locks.refresh_proof")
            )
            if origin != ("workflowy_mcp/mutation_locks.py", "mutation_region_locks") and not proof_callback:
                offenders.append(f"{rel}:{node.lineno} {name} caller {origin[1]}")
    return offenders


def test_raw_acquire_only_orchestrator():
    assert _call_offenders(_sources(), {"_acquire_region_set"}) == []


@pytest.mark.parametrize("body", [
    "from .mutation_locks import _acquire_region_set as take\nasync def bad(): return await take(())",
    "from . import mutation_locks as locks\ntake = locks._acquire_region_set\nasync def bad(): return await take(())",
    "from . import mutation_locks as locks\ntake = getattr(locks, '_acquire_region_set')\nasync def bad(): return await take(())",
    "from .mutation_locks import _acquire_region_set\nbad = lambda: _acquire_region_set(())",
    "from .mutation_locks import _acquire_region_set\nasync def bad(): return await asyncio.to_thread(_acquire_region_set)",
])
def test_raw_acquire_fence_injected_alias(body):
    assert _call_offenders([("workflowy_mcp/server.py", body)], {"_acquire_region_set"})


def test_rebase_only_orchestrator():
    assert _call_offenders(_sources(), {"establish_admission_snapshot", "rebase_during_admission"}) == []


@pytest.mark.parametrize("name", ["establish_admission_snapshot", "rebase_during_admission"])
def test_rebase_fence_injected_bypass(name):
    body = f"from .coherence import operation_cache as snapshots\nreadmit = getattr(snapshots, {name!r})\nasync def bad(): return await readmit(None, 'bad')"
    assert _call_offenders([("workflowy_mcp/server.py", body)], {name})


def test_rebase_fence_accepts_only_prescribed_refresh_proof():
    body = (
        "from .coherence.operation_cache import rebase_during_admission\n"
        "async def mutation_region_locks():\n"
        "    async def refresh_proof(regions, proof):\n"
        "        return await rebase_during_admission(None, 'proof', install=False)\n"
    )
    assert _call_offenders([("workflowy_mcp/mutation_locks.py", body)], {"rebase_during_admission"}) == []


@pytest.mark.parametrize("nested_name", ["other_proof", "refresh_proof.child", "refresh_proof.<lambda>"])
def test_rebase_fence_rejects_other_nested_origin(nested_name):
    if nested_name == "refresh_proof.child":
        statement = (
            "async def refresh_proof(regions, proof):\n"
            "        async def child():\n"
            "            return await rebase_during_admission(None, 'proof', install=False)"
        )
    elif nested_name == "refresh_proof.<lambda>":
        statement = (
            "async def refresh_proof(regions, proof):\n"
            "        return lambda: rebase_during_admission(None, 'proof', install=False)"
        )
    else:
        statement = (
            "async def other_proof(regions, proof):\n"
            "        return await rebase_during_admission(None, 'proof', install=False)"
        )
    body = (
        "from .coherence.operation_cache import rebase_during_admission\n"
        "async def mutation_region_locks():\n    " + statement + "\n"
    )
    offenders = _call_offenders([("workflowy_mcp/mutation_locks.py", body)], {"rebase_during_admission"})
    assert offenders and all("caller mutation_region_locks." + nested_name in hit for hit in offenders)


@pytest.mark.parametrize("name", ["establish_admission_snapshot", "_acquire_region_set"])
def test_rebase_fence_refresh_proof_cannot_establish_or_acquire(name):
    body = (
        f"async def mutation_region_locks():\n"
        f"    async def refresh_proof(regions, proof):\n"
        f"        return await {name}(None, 'proof')\n"
    )
    offenders = _call_offenders([("workflowy_mcp/mutation_locks.py", body)], {name})
    assert offenders and all(f"{name} caller mutation_region_locks.refresh_proof" in hit for hit in offenders)


@pytest.mark.parametrize("statement", [
    "return await asyncio.to_thread(rebase_during_admission)",
    "return await loop.run_in_executor(None, rebase_during_admission)",
    "return await rebase_during_admission",
])
def test_rebase_fence_refresh_proof_still_rejects_callable_escape(statement):
    body = (
        "async def mutation_region_locks():\n"
        "    async def refresh_proof(regions, proof):\n"
        f"        {statement}\n"
    )
    offenders = _call_offenders([("workflowy_mcp/mutation_locks.py", body)], {"rebase_during_admission"})
    assert offenders and all("callable escape" in hit for hit in offenders)


def test_one_typed_consult_reader_and_no_thread_escape():
    sources = _sources()
    assert _held_consult_offenders(sources) == []
    for body in (
        "from .mutation_locks import registry_held_regions as read\ndef bad(): return read()",
        "from .mutation_locks import registry_held_regions as read\nasync def bad(): return await read()",
        "from .mutation_locks import registry_held_regions as read\nasync def bad(): return await asyncio.to_thread(read)",
        "from .mutation_locks import registry_held_regions as read\nasync def bad(): return await loop.run_in_executor(None, read)",
    ):
        assert _held_consult_offenders([*sources, ("workflowy_mcp/unregistered.py", body)])


# The 22 direct seams of §6.5. Expected identities are literal fixture facts,
# not factories copied from production. Inherited fronts/nest rows are separate.
INTERACTIVE_SEAMS = (
    "tag.add", "tag.remove", "tag.rename", "node.edit", "node.emoji", "node.note",
    "node.delete", "node.complete", "node.move", "node.etch", "node.etch_async",
    "dock.create", "dock.update", "point.capture", "point.extract", "point.assign",
    "point.remove", "point.touch", "point.promote", "legacy.create", "legacy.update", "legacy.add",
)


def _expected_outer(case, ids):
    exact = {"tag.add", "tag.remove", "tag.rename", "node.edit", "node.emoji", "node.note", "node.complete", "point.touch"}
    if case in exact:
        return {("exact_node", ids["source"])}
    if case in {"dock.update", "legacy.update"}:
        return {("exact_node", ids["dest"])}
    if case == "point.remove":
        return {("exact_node", ids["ap"])}
    if case == "point.assign":
        return {("exact_node", ids["plain"]), ("ap_core", "NEW701")}
    if case == "node.delete":
        return {("subtree", ids["source"]), ("child_set", ids["origin"])}
    if case in {"node.move", "point.promote"}:
        return {("exact_node", ids["source"]), ("child_set", ids["origin"]), ("child_set", ids["dest"])}
    if case == "point.extract":
        return {("exact_node", ids["source"]), ("child_set", ids["dest"]), ("ap_core", "NEW701")}
    return {("child_set", ids["dest"]), ("ap_core", "NEW701")}


async def _run_interactive_seam(case, client, ids):
    from workflowy_mcp import server
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    from test_mutation_serialization import dispatch

    if case.startswith("tag."):
        async with cache_operation_boundary(client, case):
            if case == "tag.rename":
                return await client.workflowy_rename_tag(ids["source"], "old", "new")
            return await getattr(client, "workflowy_" + case.split(".")[1] + "_tag")(ids["source"], "new" if case == "tag.add" else "old")
    node = {
        "node.edit": ("edit", {"node_handle": ids["source"], "note": "replaced", "return_diff": False}),
        "node.emoji": ("update_emoji", {"node_handle": ids["source"], "emoji": "✅"}),
        "node.note": ("append_note", {"node_handle": ids["source"], "text": "tail"}),
        "node.delete": ("delete", {"node_handle": ids["source"]}),
        "node.complete": ("complete", {"node_handle": ids["source"]}),
        "node.move": ("move", {"node_handle": ids["source"], "parent_handle": ids["dest"], "position": "bottom"}),
        "node.etch": ("etch", {"parent_handle": ids["dest"], "nodes": [{"name": "[ap:NEW701] New", "note": "body"}]}),
        "node.etch_async": ("etch", {"parent_handle": ids["dest"], "nodes": [{"name": "[ap:NEW701] New", "note": "body"}], "execution": "async"}),
    }
    if case in node:
        verb, params = node[case]
        result = await dispatch(server.build_node_registry(), verb, params)
        if case == "node.etch_async":
            job = server._jobs[result["job"]["id"]]
            await job["_task"]
            return job["result"]
        return result
    anchor = {
        "dock.create": ("dock.create", {"topic": "[ap:NEW701] New", "description": "body", "parent_handle": ids["dest"]}),
        "dock.update": ("dock.update", {"dock_handle": ids["dest"], "description": "updated"}),
        "point.capture": ("point.capture", {"parent_handle": ids["dest"], "name": "New", "content": "body", "ap_id": "NEW701"}),
        "point.extract": ("point.extract", {"node_handle": ids["source"], "parent_handle": ids["dest"], "start_line": 2, "end_line": 2, "name": "New", "ap_id": "NEW701"}),
        "point.assign": ("point.assign_id", {"node_handle": ids["plain"], "ap_id": "NEW701"}),
        "point.remove": ("point.remove_id", {"node_handle": ids["ap"]}),
        "point.touch": ("point.touch", {"handles": [ids["source"]]}),
        "point.promote": ("point.promote", {"handles": [ids["source"]], "destination": ids["dest"]}),
    }
    if case in anchor:
        verb, params = anchor[case]
        return await dispatch(server.build_anchor_registry(), verb, params)
    if case == "legacy.create":
        return await server.create_anchor("[ap:NEW701] New", "body", parent_uuid=ids["dest"])
    if case == "legacy.update":
        return await server.update_anchor(ids["dest"], detail="updated")
    return await server.add_to_anchor(ids["dest"], nodes=[{"name": "[ap:NEW701] New", "note": "body"}])


async def _seam_witness(case, tmp_path, monkeypatch):
    from workflowy_mcp import mutation_locks
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    probe.outer = frozenset(_expected_outer(case, ids))
    try:
        result = await _run_interactive_seam(case, client, ids)
        if case == "node.edit":
            # diff=none / return_diff=false now returns the uniform JSON
            # header, not a node acknowledgement. The footprint proof below
            # still observes every actual GET/update send independently.
            result = json.loads(result)
        assert isinstance(result, dict) and result.get("success") is True, result
        assert probe.sends, "the real front never reached a fake HTTP send"
        assert probe.violations == [], f"interactive footprint was uncovered: {probe.violations}"
        assert not mutation_locks._LOCKS.holders and not mutation_locks._LOCKS.waiters
    finally:
        await client.close()


@pytest.mark.parametrize("case", INTERACTIVE_SEAMS)
def test_all_interactive_seams_declare_regions(case, tmp_path, monkeypatch):
    asyncio.run(_seam_witness(case, tmp_path, monkeypatch))


@pytest.mark.parametrize("case,kind", [
    ("node.edit", "exact_node"), ("node.delete", "subtree"),
    ("node.move", "child_set"), ("node.etch", "child_set"),
    ("point.capture", "ap_core"), ("legacy.update", "exact_node"),
    ("point.promote", "child_set"), ("point.extract", "child_set"),
])
def test_interactive_seam_omission_sensitivity(case, kind, tmp_path, monkeypatch):
    from workflowy_mcp import server
    from workflowy_mcp.client import api_client_core
    from workflowy_mcp import mutation_locks
    from workflowy_mcp.mutation_lease_governance import MutationRegion
    real = mutation_locks.mutation_region_locks

    @asynccontextmanager
    async def wrong(*, region_factory, **kwargs):
        def factory(snapshot):
            requested = tuple(region_factory(snapshot))
            changed = []
            for region in requested:
                if region.kind == kind:
                    if kind == "ap_core":
                        continue  # C(parent) remains, so this is not empty refusal.
                    replacement = "child_set" if kind == "exact_node" else "exact_node"
                    changed.append(MutationRegion.from_dict({"kind": replacement,
                        "parent_uuid" if replacement == "child_set" else "node_uuid": region.key,
                        "ancestor_chain": list(region.ancestor_chain)}))
                else:
                    changed.append(region)
            return tuple(changed)
        async with real(region_factory=factory, **kwargs) as grant:
            yield grant

    with monkeypatch.context() as patch:
        patch.setattr(server, "mutation_region_locks", wrong)
        patch.setattr(api_client_core, "mutation_region_locks", wrong)
        with pytest.raises(AssertionError, match="interactive footprint was uncovered"):
            asyncio.run(_seam_witness(case, tmp_path / "broken", patch))
    asyncio.run(_seam_witness(case, tmp_path / "restored", monkeypatch))


async def _recursive_fresh_witness(tmp_path, monkeypatch, *, disable_hook=False, leaf=False):
    from test_mutation_serialization import dispatch
    from workflowy_mcp import server, mutation_locks
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    probe.outer = frozenset({("child_set", ids["dest"])})
    at_x, release_x, delete_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    decision = asyncio.Event()
    labels = []
    original_plane = server.mutation_region_locks

    @asynccontextmanager
    async def record_plane(**kwargs):
        labels.append(kwargs["operation_label"])
        async with original_plane(**kwargs) as grant:
            yield grant

    monkeypatch.setattr(server, "mutation_region_locks", record_plane)
    if disable_hook:
        etch = client.workflowy_etch

        async def no_fresh_factory(*args, **kwargs):
            kwargs["fresh_root_hold"] = None
            return await etch(*args, **kwargs)

        monkeypatch.setattr(client, "workflowy_etch", no_fresh_factory)
    waiting = mutation_locks._wait_for_region_change

    async def observed_wait(*args, **kwargs):
        decision.set()
        return await waiting(*args, **kwargs)

    monkeypatch.setattr(mutation_locks, "_wait_for_region_change", observed_wait)

    async def at_send(request, held, required):
        if request.method == "POST" and request.url.path.rstrip("/").endswith("nodes") and len(probe.created) == 3:
            at_x.set()
            await release_x.wait()
        if request.method == "DELETE":
            delete_started.set()
            decision.set()
            if not release_x.is_set():
                # The negative probe observes admission at the send, then delays
                # its fake response until CREATE finishes so teardown is not an
                # unrelated missing-node/retry failure.
                await release_x.wait()
                await tasks[0]

    probe.before_send = at_send
    payload = [{"name": "R"}] if leaf else [{"name": "R", "children": [{"name": "P", "children": [{"name": "X"}]}]}]
    tasks = []
    try:
        create = asyncio.create_task(dispatch(server.build_node_registry(), "etch", {"parent_handle": ids["dest"], "nodes": payload}))
        tasks.append(create)
        if leaf:
            result = await create
            assert result["success"] is True, result
            assert not any(label.endswith(":fresh-root") for label in labels)
            assert len(probe.created) == 1 and probe.violations == []
            return
        await asyncio.wait_for(at_x.wait(), 10)
        root, parent = probe.created[:2]
        assert parent in client._cache_view_publisher.capture().nodes_by_id
        # Stop checking the creating operation's outer set on another owner.
        # Each actual send is still independently checked for its own footprint.
        probe.outer = frozenset()
        delete = asyncio.create_task(dispatch(server.build_node_registry(), "delete", {"node_handle": parent}))
        tasks.append(delete)
        await asyncio.wait_for(decision.wait(), 10)
        assert not delete_started.is_set(), "DELETE entered while fresh recursion was pending"
        assert ("subtree", root) in next(held for method, path, required, held in probe.sends if method == "POST" and required == [("child_set", parent)])
        release_x.set()
        made, deleted = await asyncio.gather(create, delete)
        assert made["success"] is True and deleted["success"] is True, (made, deleted)
        assert delete_started.is_set() and probe.violations == []
        assert len([label for label in labels if label.endswith(":fresh-root")]) == 1
        assert not mutation_locks._LOCKS.holders and not mutation_locks._LOCKS.waiters
    finally:
        release_x.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()


@pytest.mark.parametrize("leaf", [False, True])
def test_recursive_etch_fresh_root_covers_descendant_create_and_releases(tmp_path, monkeypatch, leaf):
    asyncio.run(_recursive_fresh_witness(tmp_path, monkeypatch, leaf=leaf))


def test_recursive_etch_fresh_root_neutralization(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        with pytest.raises(AssertionError, match="DELETE entered while fresh recursion was pending"):
            asyncio.run(_recursive_fresh_witness(tmp_path / "missing", patch, disable_hook=True))
    asyncio.run(_recursive_fresh_witness(tmp_path / "restored", monkeypatch))


async def _recomplete_witness(tmp_path, monkeypatch, *, disable_extension=False):
    from test_mutation_serialization import dispatch
    from workflowy_mcp import server, mutation_locks
    client, probe, ids = real_region_probe(tmp_path, monkeypatch, completed_source=True)
    probe.outer = frozenset({("child_set", ids["dest"])})
    original = server.mutation_region_locks
    phases = []

    @asynccontextmanager
    async def phase(**kwargs):
        is_completion = kwargs["operation_label"] == "anchor.point.promote:recomplete"
        if is_completion and disable_extension:
            yield None
            return
        async with original(**kwargs) as grant:
            if is_completion:
                phases.append(grant)
            yield grant

    monkeypatch.setattr(server, "mutation_region_locks", phase)
    try:
        result = await dispatch(server.build_anchor_registry(), "point.promote", {
            "handles": [ids["source"]], "destination": ids["dest"],
            "mode": "derivative", "include_completed": True,
        })
        assert result["success"] is True, result
        completes = [entry for entry in probe.sends if entry[1].endswith("/complete")]
        assert len(completes) == 2
        assert probe.violations == [], f"re-completion footprint was uncovered: {probe.violations}"
        assert all(("subtree", probe.created[0]) in entry[3] for entry in completes)
        assert len(phases) == 1 and not phases[0].is_active
        assert not mutation_locks._LOCKS.holders and not mutation_locks._LOCKS.waiters
    finally:
        await client.close()


def test_derivative_recompletion_fresh_subtree_covers_every_send(tmp_path, monkeypatch):
    asyncio.run(_recomplete_witness(tmp_path, monkeypatch))


def test_derivative_recompletion_fresh_subtree_neutralization(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        with pytest.raises(AssertionError, match="re-completion footprint was uncovered"):
            asyncio.run(_recomplete_witness(tmp_path / "missing", patch, disable_extension=True))
    asyncio.run(_recomplete_witness(tmp_path / "restored", monkeypatch))


async def _inherited_front(case, client, probe, ids):
    from workflowy_mcp import server
    from test_mutation_serialization import dispatch, _FakeWebSocket
    if case.startswith("node."):
        verb = case.removeprefix("node.")
        params = {"node_handle": ids["source"]}
        if verb in {"prepend_emoji", "append_emoji"}:
            params["emoji"] = "✅"
        elif verb == "prepend_note":
            params["text"] = "head"
        return await dispatch(server.build_node_registry(), verb, params)
    if case in {"dock.promote", "dock.demote"}:
        params = {"node_handle": ids["plain"]} if case == "dock.promote" else {"dock_handle": ids["dest"]}
        return await dispatch(server.build_anchor_registry(), case, params)
    if case in ("tag.add", "tag.remove", "tag.rename"):
        params = {"node_handle": ids["source"]}
        params.update({"old_tag": "old", "new_tag": "new"} if case == "tag.rename"
                      else {"tags": ["new" if case == "tag.add" else "old"]})
        return await dispatch(server.build_tag_registry(), case.split(".")[1], params)
    if case in ("tag.batch", "ws.tags"):
        operations = [{"handle": ids[name], "operation": "add", "tags": ["new"]} for name in ("source", "plain")]
        if case == "tag.batch":
            return await dispatch(server.build_tag_registry(), "batch", {"operations": operations})
        ws = _FakeWebSocket()
        await server._handle_mutate_node_tags({"action": "mutate_node_tags", "request_id": "n05-tags",
                                               "operations": operations}, ws)
        assert len(ws.sent) == 1 and ws.sent[0]["request_id"] == "n05-tags"
        return ws.sent[0]
    if case == "ws.dock":
        ws = _FakeWebSocket()
        await server._handle_anchor_dock_update({"action": "anchor_dock_update", "request_id": "n05-dock",
                                                "dock_handle": ids["dest"], "favorite": True}, ws)
        assert len(ws.sent) == 1 and ws.sent[0]["request_id"] == "n05-dock"
        return ws.sent[0]
    if case == "capture.tags":
        return await dispatch(server.build_anchor_registry(), "point.capture", {
            "parent_handle": ids["dest"], "name": "New", "content": "body",
            "ap_id": "NEW701", "tags": ["new"],
        })
    return await server.add_to_anchor(ids["dest"], nodes=[{"name": "[ap:NEW701] New"}], tags=["new"])


EXTRA_INHERITED_FRONTS = ("node.prepend_emoji", "node.append_emoji", "node.prepend_note", "node.uncomplete",
                          "dock.promote", "dock.demote", "tag.purge.apply", "slice_group.purge.apply",
                          "slice_group.merge.persist", "ws.merge")


async def _expanded_inherited_front(case, client, probe, ids, monkeypatch):
    """Config transport alone is faked; projection/Q4/CRUD/plane stay real."""
    from unittest.mock import AsyncMock
    from workflowy_mcp import server
    from test_mutation_serialization import dispatch, _FakeWebSocket
    if case in EXTRA_INHERITED_FRONTS[:6]:
        return await _inherited_front(case, client, probe, ids)
    if case.endswith("purge.apply"):
        if case.startswith("tag."):
            registry, params = server.build_tag_registry(), {"family": "old", "root_handle": ids["origin"]}
        else:
            group = {"id": "grp-n05", "name": "N05", "prefixes": ["old"], "root_uuid": ids["origin"],
                     "description": "", "default_sort_by": "none", "default_sort_direction": "asc"}
            monkeypatch.setattr(server, "_resolve_slice_group_config", AsyncMock(return_value=(
                [group], None, {"auto_expand": "expand"}, [], {})))
            registry, params = server.build_slice_group_registry(), {"group_handle": "grp-n05"}
        preview = await dispatch(registry, "purge.preview", params)
        assert preview["success"] is True and not probe.sends, preview
        return await dispatch(registry, "purge.apply", {**params, "fingerprint": preview["fingerprint"]})
    if case == "slice_group.merge.persist":
        async def write_config(groups, active, target_tab):
            return {"expand_groups": groups, "active_group": active}
        monkeypatch.setattr(server, "_write_slice_group_config", write_config)
        return await dispatch(server.build_slice_group_registry(), "merge.persist", {
            "lens": {"kind": "merge", "operation": "union", "inputs": [
                {"kind": "tags", "root_handle": ids["origin"], "tags": ["old"]},
                {"kind": "tags", "root_handle": ids["origin"], "tags": ["old"]}]}, "tag": "merged"})
    async def read_nested(**kwargs):
        return ({"expand_groups": [], "active_group": None, "f8_postfix_auto_expand": "expand",
                 "f8_postfix_suppress": "suppress", "f8_postfix_deprecated": "deprecated"}, None)
    async def create_group(**kwargs):
        return {"success": True, "slice_group": {"id": "generated-n05", "name": kwargs["name"]}}
    monkeypatch.setattr(server, "_read_slice_group_config_nested", read_nested)
    monkeypatch.setattr(server, "create_slice_group", create_group)
    ws = _FakeWebSocket()
    await server._handle_merge_slice_groups({"action": "merge_slice_groups", "request_id": "n05-merge",
        "endpoint_id": "n05-private-fixture", "v": 1,
        "groups": [{"root_uuid": ids["origin"], "tags": ["old"]},
                   {"root_uuid": ids["origin"], "tags": ["old"]}],
        "mode": "union", "tag": "merged"}, ws)
    assert len(ws.sent) == 1 and ws.sent[0]["request_id"] == "n05-merge"
    assert ws.sent[0]["action"] == "merge_slice_groups_result"
    return ws.sent[0]


@pytest.mark.parametrize("case", EXTRA_INHERITED_FRONTS)
def test_expanded_inherited_fronts_keep_per_item_ownership(case, tmp_path, monkeypatch):
    async def run():
        client, probe, ids = real_region_probe(tmp_path, monkeypatch, completed_source=case == "node.uncomplete")
        try:
            result = await _expanded_inherited_front(case, client, probe, ids, monkeypatch)
            assert result["success"] is True, result
            assert probe.sends and not probe.violations, "inherited front sent without its declared footprint"
            assert all(held == frozenset(required) for method, path, required, held in probe.sends), "batch acquired more than its item"
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("case", EXTRA_INHERITED_FRONTS)
@pytest.mark.parametrize("code", ["mutation_serialization_timeout", "mutation_serialization_conflict", "mutation_region_error"])
def test_expanded_inherited_fronts_carry_plane_codes(case, code, tmp_path, monkeypatch):
    from workflowy_mcp import server
    from workflowy_mcp.client import api_client_core
    async def run():
        client, probe, ids = real_region_probe(tmp_path, monkeypatch, completed_source=case == "node.uncomplete")
        exc = _refusal(code)
        @asynccontextmanager
        async def refuse(**kwargs):
            raise exc
            yield
        monkeypatch.setattr(server, "mutation_region_locks", refuse)
        monkeypatch.setattr(api_client_core, "mutation_region_locks", refuse)
        try:
            result = await _expanded_inherited_front(case, client, probe, ids, monkeypatch)
            assert result["success"] is False, result
            expected = ({"retry_after": 2., "contended_keys": ["n05-test"], "timeout_seconds": 60.}
                if code == "mutation_serialization_timeout" else
                {"retry_after": 60., "contended_keys": ["n05-test"], "holder_kind": "long_lived", "holder_id": "n05-holder", "reason": "long_lived_holder"}
                if code == "mutation_serialization_conflict" else {"reason": "malformed_region", "retry_after": exc.retry_after})
            if case in EXTRA_INHERITED_FRONTS[:6]:
                assert result["error"]["code"] == code and result["error"]["extra"] == expected
            else:
                # These pre-existing compound reports retain their own failure
                # code and carry the plane refusal on each Q4 item, not a new
                # top-level schema code on read/config-only preview verbs.
                report = result if case == "ws.merge" else result["detail"]
                rows = report["mutation"]["results"]
                assert rows and all(row["error_code"] == code for row in rows), "compound ledger lost the plane code"
                assert all(row["extra"] == expected and row["retry_after"] == exc.retry_after for row in rows)
            assert probe.sends == probe.reads == []
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["tag.add", "tag.remove", "tag.rename", "tag.batch", "ws.tags", "ws.dock", "capture.tags", "legacy.tags"])
def test_inherited_dispatch_and_ws_fronts_use_actual_primitive_holds(case, tmp_path, monkeypatch):
    from workflowy_mcp import mutation_locks
    async def run():
        client, probe, ids = real_region_probe(tmp_path, monkeypatch)
        try:
            result = await _inherited_front(case, client, probe, ids)
            assert result["success"] is True, result
            assert probe.sends and probe.violations == []
            if case in ("tag.batch", "ws.tags"):
                assert len(probe.sends) == 2
                for _method, _path, required, held in probe.sends:
                    assert held == frozenset(required)  # per item, not a batch union
            if case in ("capture.tags", "legacy.tags"):
                updates = [held for method, path, required, held in probe.sends if method == "POST" and not path.rstrip("/").endswith("nodes")]
                assert updates and all(("exact_node", probe.created[0]) in held for held in updates)
            assert not mutation_locks._LOCKS.holders and not mutation_locks._LOCKS.waiters
        finally:
            await client.close()
    asyncio.run(run())


def _refusal(code):
    from workflowy_mcp import mutation_locks
    from workflowy_mcp.mutation_lease_governance import MutationRegionError
    if code == "mutation_serialization_timeout":
        return mutation_locks.MutationSerializationTimeoutError(retry_after=2.0, contended_keys=["n05-test"], timeout_seconds=60.0)
    if code == "mutation_serialization_conflict":
        return mutation_locks.MutationSerializationConflictError(
            "Mutation region is held by long-lived operation 'n05-holder'; no write was attempted. Retry after about 60s; the holder may still be running.",
            retry_after=60.0, contended_keys=["n05-test"], holder_kind="long_lived",
            holder_id="n05-holder", reason="long_lived_holder",
        )
    return mutation_locks._public_region_error(MutationRegionError("bad test declaration", reason="malformed_region"))


async def _refusal_surface_witness(front, code, tmp_path, monkeypatch):
    from workflowy_mcp import server
    from workflowy_mcp.client import api_client_core
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    exc = _refusal(code)

    @asynccontextmanager
    async def refuse(**kwargs):
        raise exc
        yield  # pragma: no cover — async-context-manager shape, no send path

    monkeypatch.setattr(server, "mutation_region_locks", refuse)
    monkeypatch.setattr(api_client_core, "mutation_region_locks", refuse)
    try:
        if front in ("tag.add", "tag.remove", "tag.rename", "tag.batch", "ws.tags", "ws.dock"):
            result = await _inherited_front(front, client, probe, ids)
        elif front == "node.etch_async":
            # The submitting call owns no execution hold and must acknowledge
            # the job. The existing independent runner preserves its terminal
            # error-string carrier, rather than returning a fictional refusal
            # from the already-successful submitting operation.
            from test_mutation_serialization import dispatch
            submitted = await dispatch(server.build_node_registry(), "etch", {
                "parent_handle": ids["dest"], "nodes": [{"name": "New"}], "execution": "async",
            })
            assert submitted["success"] is True
            job = server._jobs[submitted["job"]["id"]]
            await job["_task"]
            assert job["status"] == "failed" and job["result"] is None
            assert job["error"] == f"{type(exc).__name__}: {exc}"
            assert probe.sends == probe.reads == []
            return
        else:
            result = await _run_interactive_seam(front, client, ids)
        assert result["success"] is False, result
        expected = (
            {"retry_after": 2.0, "contended_keys": ["n05-test"], "timeout_seconds": 60.0}
            if code == "mutation_serialization_timeout" else
            {"retry_after": 60.0, "contended_keys": ["n05-test"], "holder_kind": "long_lived",
             "holder_id": "n05-holder", "reason": "long_lived_holder"}
            if code == "mutation_serialization_conflict" else
            {"reason": "malformed_region", "retry_after": exc.retry_after}
        )
        if front in ("point.touch", "point.promote", "tag.batch", "ws.tags"):
            rows = result["results"] if front == "ws.tags" else result["detail"]["results"]
            assert rows and all(row["error_code"] == code for row in rows), "plane code was lost on item"
            assert all(row["extra"] == expected and row["retry_after"] == exc.retry_after for row in rows)
        elif front.startswith("legacy.") or front == "ws.dock":
            assert result["error_code"] == code, "plane code was lost on front"
            assert result["extra"] == expected
        else:
            assert result["error"]["code"] == code, "plane code was lost on consolidated dispatch"
            assert result["error"]["extra"] == expected
        assert probe.sends == [] and probe.reads == []
    finally:
        await client.close()


@pytest.mark.parametrize("code", ["mutation_serialization_timeout", "mutation_serialization_conflict", "mutation_region_error"])
@pytest.mark.parametrize("front", [*INTERACTIVE_SEAMS, "tag.batch", "ws.tags", "ws.dock"])
def test_new_codes_declared_and_mapped_all_surfaces(front, code, tmp_path, monkeypatch):
    asyncio.run(_refusal_surface_witness(front, code, tmp_path, monkeypatch))


@pytest.mark.parametrize("code", ["mutation_serialization_conflict", "mutation_region_error"])
def test_new_code_mapping_neutralization(code, tmp_path, monkeypatch):
    from workflowy_mcp import server
    with monkeypatch.context() as patch:
        patch.setattr(server, "_mcp_exception_mapper", lambda spec, exc: {
            "success": False, "error": {"code": "operation_failed", "message": "lost classification"},
        })
        with pytest.raises(AssertionError, match="plane code was lost on consolidated dispatch"):
            asyncio.run(_refusal_surface_witness("node.edit", code, tmp_path / "generic", patch))
    asyncio.run(_refusal_surface_witness("node.edit", code, tmp_path / "restored", monkeypatch))


def _scalar_plane_offenders(pairs):
    bad = []
    for rel, text in pairs:
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "_KeyEntry":
                bad.append((rel, node.lineno, "scalar entry"))
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and any(
                marker in node.value for marker in (".mutation_leases", ".lease.json", "mutation_leases_dir")
            ):
                bad.append((rel, node.lineno, "persisted mutation ownership"))
            if rel == "workflowy_mcp/mutation_locks.py" and isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "Lock":
                    bad.append((rel, node.lineno, "per-key/global mutex"))
        if rel == "workflowy_mcp/mutation_locks.py":
            assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == "_LOCKS" for target in node.targets)]
            if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Call) or not isinstance(assignments[0].value.func, ast.Name) or assignments[0].value.func.id != "_RegionRegistry":
                bad.append((rel, 0, "not the one typed registry"))
    return bad


def test_no_scalar_plane_or_persisted_mutation_lock():
    pairs = _sources()
    assert _scalar_plane_offenders(pairs) == []
    assert _call_offenders(pairs, {"mutation_node_locks"}) == []
    for text in (
        "class _KeyEntry: pass",
        "def own(): return '.mutation_leases/held.lease.json'",
    ):
        assert _scalar_plane_offenders([("workflowy_mcp/client/injected.py", text)])
    assert _scalar_plane_offenders([("workflowy_mcp/mutation_locks.py", "_LOCKS = {}")])
    assert _scalar_plane_offenders([("workflowy_mcp/mutation_locks.py", "_LOCKS = _RegionRegistry()\na = asyncio.Lock()")])


def test_harness_has_no_production_import_or_second_registry():
    def offenders(text):
        result = []
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("workflowy_mcp"):
                result.append(node.lineno)
            if isinstance(node, ast.Import) and any(item.name.startswith("workflowy_mcp") for item in node.names):
                result.append(node.lineno)
            if isinstance(node, ast.ClassDef) and node.name in {"_KeyEntry", "_RegionRegistry", "_RegionHold"}:
                result.append(node.lineno)
        return result
    assert offenders(Path(__file__).with_name("n05_region_harness.py").read_text(encoding="utf-8")) == []
    for injected in ("from workflowy_mcp import mutation_locks", "import workflowy_mcp.mutation_locks as locks", "class _RegionRegistry: pass"):
        assert offenders(injected)


# Closed held→requested inventory, including the conductor's two additions.
# Every row invokes an actual front/HTTP witness below, never merely mentions it.
NESTED_EDGES = {
    "emoji -> edit (covered E)": "node.emoji",
    "note concat -> edit (covered E)": "node.note",
    "Dock update -> edit (covered E)": "dock.update",
    "assign -> edit (covered E, explicit A already held)": "point.assign",
    "remove -> edit (covered E)": "point.remove",
    "touch -> edit (covered E, per item)": "point.touch",
    "promote move -> move/edit (covered E+C+C)": "point.promote",
    "extract -> capture/source edit (covered E+C+A)": "point.extract",
    "derivative -> ETCH then re-complete (HG2R5B fresh S)": "recomplete",
    "capture -> new-root tag (fresh E)": "capture.tags",
    "legacy add -> new-root tag (fresh E)": "legacy.tags",
    "ETCH -> fresh-root recursion (VA5ZZO fresh S)": "recursive",
    "tag batch -> per-item primitive": "tag.batch",
}


@pytest.mark.parametrize("edge", tuple(NESTED_EDGES))
def test_existing_nested_edges_are_covered_or_fresh(edge, tmp_path, monkeypatch):
    case = NESTED_EDGES[edge]
    if case == "recomplete":
        asyncio.run(_recomplete_witness(tmp_path, monkeypatch))
    elif case == "recursive":
        asyncio.run(_recursive_fresh_witness(tmp_path, monkeypatch))
    elif case in ("capture.tags", "legacy.tags", "tag.batch"):
        test_inherited_dispatch_and_ws_fronts_use_actual_primitive_holds(case, tmp_path, monkeypatch)
    else:
        asyncio.run(_seam_witness(case, tmp_path, monkeypatch))


async def _nonfresh_outer_witness(tmp_path, monkeypatch, *, omit_outer=False):
    """An inner E(existing target) must be covered BEFORE its admission."""
    from workflowy_mcp import server, mutation_locks
    from workflowy_mcp.mutation_lease_governance import build_region_child_set
    client, probe, ids = real_region_probe(tmp_path, monkeypatch)
    actual = mutation_locks.mutation_region_locks
    violations, inner_grants = [], []

    @asynccontextmanager
    async def inventory(*, region_factory, **kwargs):
        label = kwargs["operation_label"]
        if label == "node.edit":
            held = {(r.kind, r.key) for r in mutation_locks.held_mutation_lock_regions()}
            if ("exact_node", ids["plain"]) not in held:
                violations.append("missing pre-existing exact region")
        def declared(snapshot):
            requested = tuple(region_factory(snapshot))
            if omit_outer and label == "anchor.point.assign_id":
                return tuple(build_region_child_set(r.key, cache_by_id=snapshot.nodes_by_id)
                             if r.kind == "exact_node" else r for r in requested)
            return requested
        async with actual(region_factory=declared, **kwargs) as grant:
            if label == "node.edit":
                inner_grants.append(grant.token)
            yield grant

    monkeypatch.setattr(server, "mutation_region_locks", inventory)
    try:
        result = await _run_interactive_seam("point.assign", client, ids)
        assert result["success"] is True, result
        assert violations == [], "non-fresh nested region was missing from the outer set"
        assert inner_grants == [None] and probe.sends and not probe.violations
    finally:
        await client.close()


def test_nested_nonfresh_request_is_covered_by_outer_before_inner_admission(tmp_path, monkeypatch):
    asyncio.run(_nonfresh_outer_witness(tmp_path, monkeypatch))


def test_nested_nonfresh_outer_omission_neutralization(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        with pytest.raises(AssertionError, match="non-fresh nested region was missing from the outer set"):
            asyncio.run(_nonfresh_outer_witness(tmp_path / "missing", patch, omit_outer=True))
    asyncio.run(_nonfresh_outer_witness(tmp_path / "restored", monkeypatch))


@pytest.mark.parametrize("code", ["mutation_serialization_conflict", "mutation_region_error"])
def test_missing_plane_code_declaration_neutralization(monkeypatch, code):
    from types import SimpleNamespace
    from workflowy_mcp import server
    from test_mutation_locks import test_every_affected_verb_declares_the_timeout_code as witness
    build = server.build_node_registry
    def omitted():
        registry = build()
        resolve = registry.resolve
        # Deep-frozen schemas cannot be dataclasses.replace'd (their
        # MappingProxyType children deliberately reject deepcopy). The
        # declaration witness reads ONLY this field; don't rebuild a spec.
        registry.resolve = lambda verb: (SimpleNamespace(domain_error_codes=tuple(
            c for c in resolve(verb).domain_error_codes if c != code)) if verb == "edit" else resolve(verb))
        return registry
    with monkeypatch.context() as patch:
        patch.setattr(server, "build_node_registry", omitted)
        with pytest.raises(AssertionError, match="node.edit is missing the declaration"):
            witness()
    witness()
