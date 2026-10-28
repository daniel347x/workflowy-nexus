"""U01 impl-level integration tests (spec [ap:XKQQUO] §9 tests 8–17).

In-process asyncio with faked clients — the S05 asyncio-level harness
precedent (``tests/mutation_lease_harness.py``): REAL registries, dispatch
kernel, operation boundaries, admission, seams, ledger code and the real
lock module; ONLY the Workflowy HTTP surface is faked.

**The fake's ``get_node`` / ``update_node`` YIELD to the event loop**
(``await asyncio.sleep(0)``) — §9 preamble (A8): without yield points the
unlocked (monkeypatched) run of the incident test would execute its
coroutines to completion in turn and prove nothing. With them, the unlocked
run demonstrably loses writes (the in-suite mutation demos below) exactly
like the [ap:4HOBDR] incident.

Nothing here reaches the network, the browser, or the real Workflowy tree.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from coherence_harness import make_boundary_capable, make_coherence_root
from workflowy_mcp import mutation_locks as ml
from workflowy_mcp import server
from workflowy_mcp.client import api_client_core
from workflowy_mcp.coherence.operation_cache import cache_operation_boundary, create_isolated_context_task
from workflowy_mcp.mcp_api_registry import dispatch_operation
from workflowy_mcp.models.node import WorkFlowyNode
from workflowy_mcp.models.requests import NodeUpdateRequest
from workflowy_mcp.mutation_locks import (
    MUTATION_LOCK_STATE,
    MutationSerializationTimeoutError,
    held_mutation_lock_keys,
    mutation_node_locks,
    registry_held_keys,
)

ROOT = "00000000-0000-4000-8000-000000000000"
NODE = "11111111-1111-4111-8111-111111111111"
NODE_A = "aaaaaaa1-1111-4111-8111-111111111111"
NODE_B = "bbbbbbb2-2222-4222-8222-222222222222"
NODE_C = "ccccccc3-3333-4333-8333-333333333333"
P1 = "ddddddd1-4444-4444-8444-444444444444"
P2 = "ddddddd2-5555-4555-8555-555555555555"
SUB = "eeeeeee1-6666-4666-8666-666666666666"   # plain sub-bullet under DOCK
ITEM1 = "fffffff1-7777-4777-8777-777777777771"
ITEM2 = "fffffff2-7777-4777-8777-777777777772"
DOCK = "99999991-8888-4888-8888-888888888881"  # Anchor Dock (origin, NEVER a key)
DEST = "99999992-8888-4888-8888-888888888882"  # destination Dock

NOTE0 = "line alpha\nline beta\nline gamma\n"
EXTRACT_NOTE = "one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten"


# ---------------------------------------------------------------------------
# The yielding fake server (the §9 A8 requirement)
# ---------------------------------------------------------------------------


class YieldingFakeServer:
    """Per-node evolving state; every read AND write yields to the loop.

    ``update_node`` applies whole-note/whole-name writes (the real POST's
    granularity for the fields it carries) and returns the post-write node
    (mirroring the real client's read-back). ``probes`` records
    ``registry_held_keys()`` at each I/O point so tests can assert WHICH keys
    the calling operation holds while the fake runs.
    """

    def __init__(self, nodes):
        self.state = {}
        for node in nodes:
            self.state[node["id"]] = {
                "name": node.get("name") or "",
                "note": node.get("note") or "",
                "parent_id": node.get("parent_id"),
                "priority": node.get("priority") or 0,
                "createdAt": 100,
                "modifiedAt": 100,
                "completedAt": node.get("completedAt"),
                "data": dict(node.get("data") or {}),
            }
        self.posts: list[tuple[str, dict]] = []
        self.moves: list[tuple[str, str, str]] = []
        self.creates: list[tuple[str, dict]] = []
        self.completes: list[str] = []
        self.deletes: list[str] = []
        self.gets: list[str] = []
        self.probes: list[tuple[str, str, frozenset, frozenset]] = []
        self.fail_update_for: str | None = None
        self.counter = 0
        self._gate = None  # optional async callable(uuid, phase) injected by tests

    def node(self, uuid):
        s = self.state[uuid]
        return WorkFlowyNode(
            id=uuid, name=s["name"], note=s["note"], parentId=s["parent_id"],
            priority=s["priority"], createdAt=s["createdAt"],
            modifiedAt=s["modifiedAt"], completedAt=s["completedAt"],
            data=dict(s["data"]) or None,
        )

    def children(self, parent):
        return sorted(
            (u for u, s in self.state.items() if s["parent_id"] == parent),
            key=lambda u: self.state[u]["priority"],
        )

    async def get_node(self, uuid, max_retries=10):
        await asyncio.sleep(0)  # THE yield point (A8)
        self.gets.append(uuid)
        self.probes.append((uuid, "get", frozenset(registry_held_keys()), frozenset(held_mutation_lock_keys())))
        if self._gate is not None:
            await self._gate(uuid, "get")
        return self.node(uuid)

    async def update_node(self, uuid, request: NodeUpdateRequest, max_retries=10, *, basis=None):
        await asyncio.sleep(0)  # THE yield point (A8)
        body = request.model_dump(exclude_none=True)
        self.posts.append((uuid, body))
        self.probes.append((uuid, "update", frozenset(registry_held_keys()), frozenset(held_mutation_lock_keys())))
        if uuid == self.fail_update_for:
            raise RuntimeError("simulated transport failure on POST")
        if self._gate is not None:
            await self._gate(uuid, "update")
        s = self.state[uuid]
        if "name" in body:
            s["name"] = html.escape(body["name"], quote=False)
        if "note" in body:
            s["note"] = html.escape(body["note"], quote=False)
        if "layoutMode" in body:
            s["data"] = {**(s["data"] or {}), "layoutMode": body["layoutMode"]}
        s["modifiedAt"] += 1
        await asyncio.sleep(0)
        return self.node(uuid)  # the post-write read-back

    async def move_node(self, uuid, parent_id, position="top"):
        await asyncio.sleep(0)
        self.moves.append((uuid, parent_id, position))
        self.probes.append((uuid, "move", frozenset(registry_held_keys()), frozenset(held_mutation_lock_keys())))
        s = self.state[uuid]
        s["parent_id"] = parent_id
        siblings = self.children(parent_id)
        s["priority"] = (max(self.state[x]["priority"] for x in siblings) + 1) if siblings else 0
        return True

    async def complete_node(self, uuid):
        await asyncio.sleep(0)
        self.completes.append(uuid)
        self.probes.append((uuid, "complete", frozenset(registry_held_keys()), frozenset(held_mutation_lock_keys())))
        self.state[uuid]["completedAt"] = 999
        return self.node(uuid)

    async def uncomplete_node(self, uuid):
        await asyncio.sleep(0)
        self.state[uuid]["completedAt"] = None
        return self.node(uuid)

    async def delete_node(self, uuid):
        await asyncio.sleep(0)
        self.deletes.append(uuid)
        self.probes.append((uuid, "delete", frozenset(registry_held_keys()), frozenset(held_mutation_lock_keys())))
        self.state.pop(uuid, None)
        return True

    def _new_id(self):
        self.counter += 1
        return f"c0c0c0c0-{self.counter:04x}-4c0c-8c0c-c0c0c0c0c0c0"

    async def workflowy_etch(self, parent_id, nodes, nodes_file=None, *, fresh_root_hold=None):
        await asyncio.sleep(0)
        self.probes.append((parent_id, "etch", frozenset(registry_held_keys()), frozenset(held_mutation_lock_keys())))
        stats = {"created": 0, "errors": [], "roots": []}

        def create(parent, items, top):
            for item in items:
                uuid = self._new_id()
                kids = self.children(parent)
                self.state[uuid] = {
                    "name": html.escape(item["name"], quote=False),
                    "note": html.escape(item.get("note") or "", quote=False),
                    "parent_id": parent, "priority": len(kids),
                    "createdAt": 200, "modifiedAt": 200, "completedAt": None,
                    "data": {"layoutMode": item["layout_mode"]} if item.get("layout_mode") else {},
                }
                self.creates.append((uuid, dict(item)))
                stats["created"] += 1
                if top:
                    stats["roots"].append(uuid)
                if item.get("children"):
                    create(uuid, item["children"], False)

        create(parent_id, nodes, True)
        return {
            "success": not stats["errors"], "nodes_created": stats["created"],
            "root_node_ids": stats["roots"], "api_calls": stats["created"],
            "retries": 0, "rate_limit_hits": 0, "errors": stats["errors"],
        }

    async def list_nodes(self, request):
        await asyncio.sleep(0)
        return [self.node(u) for u in self.children(request.parentId)], 0


FAKED = (
    "get_node", "update_node", "move_node", "complete_node",
    "uncomplete_node", "delete_node", "workflowy_etch", "list_nodes",
)


def setup_env(monkeypatch, tmp_path, nodes):
    """Boundary-capable client double + yielding fake + isolated governance."""
    paths = make_coherence_root(tmp_path)
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    client = make_boundary_capable(object.__new__(server.WorkFlowyClient), nodes=nodes)
    client.api_key = "u01-fake-key-never-connect"
    client._write_governor = SimpleNamespace(paths=SimpleNamespace(
        jobs_root=tmp_path / "cartographer_jobs", account_scope_id="a" * 32,
    ))
    fake = YieldingFakeServer(nodes)
    for name in FAKED:
        setattr(client, name, AsyncMock(side_effect=getattr(fake, name)))
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    monkeypatch.setattr(ml, "_LOCKS", ml._RegionRegistry())
    return client, fake, paths


async def dispatch(registry, verb, params):
    return await dispatch_operation(
        registry, verb, params,
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    )


def edit_params(node, search, replace):
    return {"node_handle": node, "note_search": search, "note_replace": replace}


def report_header(result):
    """Parse a node.edit note-edit report's JSON metadata header."""
    assert isinstance(result, str)
    header = result.split("\n\n", 1)[0] if "\n\n" in result else result.rstrip("\n")
    return json.loads(header)


@asynccontextmanager
async def _null_locks(*args, **kwargs):
    """The mutation-check monkeypatch: a null context manager (§9 test 8)."""
    yield None


def nullify_locks(monkeypatch):
    monkeypatch.setattr(server, "mutation_region_locks", _null_locks)
    monkeypatch.setattr(api_client_core, "mutation_region_locks", _null_locks)


@asynccontextmanager
async def _locks(client, *keys):
    """The exact-node compatibility form has the same mandatory boundary."""
    async with cache_operation_boundary(client, "u01-squatter"):
        async with mutation_node_locks(*keys, client=client) as grant:
            yield grant


async def _primitive(client, coroutine):
    async with cache_operation_boundary(client, "u01-primitive"):
        return await coroutine


# ---------------------------------------------------------------------------
# Test 8 — THE [ap:4HOBDR] REPRODUCTION (the first test)
# ---------------------------------------------------------------------------


def _incident_nodes(note=NOTE0):
    return [{"id": NODE, "name": "Target note", "note": note, "parent_id": None}]


def test_incident_three_concurrent_edits_all_land(monkeypatch, tmp_path):
    """P2: three concurrent node.edit calls on ONE note in one asyncio.gather
    ALL land; every result is honest; contended reports carry the §6.7 field
    inside their JSON metadata header (A3); an uncontended run carries none."""
    client, fake, _ = setup_env(monkeypatch, tmp_path, _incident_nodes())

    async def main():
        return await asyncio.gather(
            dispatch(server.build_node_registry(), "edit", edit_params(NODE, "alpha", "ALPHA")),
            dispatch(server.build_node_registry(), "edit", edit_params(NODE, "beta", "BETA")),
            dispatch(server.build_node_registry(), "edit", edit_params(NODE, "gamma", "GAMMA")),
        )

    results = asyncio.run(main())

    # ALL THREE edits landed — the incident is fixed
    final_note = html.unescape(fake.state[NODE]["note"])
    assert final_note == "line ALPHA\nline BETA\nline GAMMA\n"
    assert len(fake.posts) == 3

    # every result honest: success, edit applied, no false no_op
    headers = [report_header(r) for r in results]
    for header in headers:
        assert header["success"] is True
        assert header["no_op"] is False
        assert header["edits_applied"] == 1

    # P5/A3: the contended results carry serialization INSIDE the header,
    # naming the contended node's uuid8. (Exactly two of three contend under
    # the yielding fake; >= 1 keeps the assertion scheduling-robust.)
    with_ser = [h for h in headers if "serialization" in h]
    assert len(with_ser) >= 1
    for header in with_ser:
        assert header["serialization"]["waited_on"] == [NODE[:8]]
        assert header["serialization"]["waited_seconds"] >= 0

    # an UNCONTESTED run's header carries no such key (§10 byte-identity)
    solo = asyncio.run(
        dispatch(server.build_node_registry(), "edit", edit_params(NODE, "ALPHA", "alpha"))
    )
    solo_header = report_header(solo)
    assert "serialization" not in solo_header
    assert not ml._LOCKS.holders and not ml._LOCKS.waiters


def test_incident_without_locks_loses_writes(monkeypatch, tmp_path):
    """THE MUTATION CHECK, in-suite (§9 test 8): with mutation_region_locks
    monkeypatched to a null context manager, the SAME scenario loses writes —
    proving the test above would FAIL against pre-fix code. No production
    kill-switch; monkeypatching only."""
    client, fake, _ = setup_env(monkeypatch, tmp_path, _incident_nodes())
    nullify_locks(monkeypatch)

    async def main():
        return await asyncio.gather(
            dispatch(server.build_node_registry(), "edit", edit_params(NODE, "alpha", "ALPHA")),
            dispatch(server.build_node_registry(), "edit", edit_params(NODE, "beta", "BETA")),
            dispatch(server.build_node_registry(), "edit", edit_params(NODE, "gamma", "GAMMA")),
        )

    results = asyncio.run(main())
    final_note = html.unescape(fake.state[NODE]["note"])
    landed = sum(x in final_note for x in ("ALPHA", "BETA", "GAMMA"))
    # the unlocked run demonstrably loses writes (observed: exactly the last
    # writer survives — every result still CLAIMS success: the incident)
    assert landed < 3, f"the unlocked run must lose writes; final note: {final_note!r}"
    for r in results:
        assert report_header(r)["success"] is True  # silent loss — all "honest"


def test_contended_noop_report_carries_serialization(monkeypatch, tmp_path):
    """CF-03 (optional assertion): a contended edit whose search genuinely
    misses (its text was consumed by the serialized predecessor) is an HONEST
    no_op — and its header STILL carries the §6.7 field (all three report
    sites emit it, the two no-op fast paths included)."""
    client, fake, _ = setup_env(monkeypatch, tmp_path, _incident_nodes())

    async def main():
        return await asyncio.gather(
            dispatch(server.build_node_registry(), "edit", edit_params(NODE, "alpha", "ALPHA")),
            # same search text — consumed by whichever edit runs first
            dispatch(server.build_node_registry(), "edit", edit_params(NODE, "alpha", "CONSUMED")),
        )

    first, second = asyncio.run(main())
    headers = [report_header(first), report_header(second)]
    applied = [h for h in headers if h["edits_applied"] == 1]
    noop = [h for h in headers if h["no_op"] is True]
    assert len(applied) == 1 and len(noop) == 1
    # the no-op is TRUE (its search genuinely does not match), never a masked
    # overwrite — and it waited, so its header says so
    assert "CONSUMED" not in html.unescape(fake.state[NODE]["note"])
    assert "ALPHA" in html.unescape(fake.state[NODE]["note"])
    assert "serialization" in noop[0]
    assert noop[0]["serialization"]["waited_on"] == [NODE[:8]]


# ---------------------------------------------------------------------------
# Test 9 — two-node parallelism preserved (the no-global-lock guard)
# ---------------------------------------------------------------------------


def test_two_node_parallelism_preserved(monkeypatch, tmp_path):
    """I2/P3: edits on two DIFFERENT nodes OVERLAP in wall time — both live
    reads are simultaneously in flight (a gate only passes when both are
    inside), and NEITHER result reports a wait (disjoint keys never
    serialize). A global lock would deadlock the gate."""
    nodes = [
        {"id": NODE_A, "name": "A", "note": "a-text one", "parent_id": None},
        {"id": NODE_B, "name": "B", "note": "b-text one", "parent_id": None},
    ]
    client, fake, _ = setup_env(monkeypatch, tmp_path, nodes)

    async def main():
        entered: set[str] = set()
        both_in = asyncio.Event()

        async def gate(uuid, phase):
            if phase != "get":
                return
            entered.add(uuid)
            if len(entered) == 2:
                both_in.set()
            # BOTH reads must be in flight simultaneously to pass — with any
            # global serialization this times out and the test fails loudly
            await asyncio.wait_for(both_in.wait(), timeout=5)

        fake._gate = gate
        return await asyncio.wait_for(
            asyncio.gather(
                dispatch(server.build_node_registry(), "edit", edit_params(NODE_A, "a-text", "A2")),
                dispatch(server.build_node_registry(), "edit", edit_params(NODE_B, "b-text", "B2")),
            ),
            timeout=15,
        )

    r_a, r_b = asyncio.run(main())
    assert "A2" in html.unescape(fake.state[NODE_A]["note"])
    assert "B2" in html.unescape(fake.state[NODE_B]["note"])
    # disjoint keys NEVER serialize: no wait, no field (§6.7 absent-when-
    # uncontended), for either result
    assert "serialization" not in report_header(r_a)
    assert "serialization" not in report_header(r_b)


# ---------------------------------------------------------------------------
# Test 10 — tag.add + tag.remove concurrently on one node
# ---------------------------------------------------------------------------


def test_tag_add_and_remove_concurrent_both_land(monkeypatch, tmp_path):
    """Both tag writes land, in serialized order: the final name is the same
    under either order — base preserved, #t0 gone, #t1 present."""
    nodes = [{"id": NODE, "name": "Target #t0", "note": "", "parent_id": None}]
    client, fake, _ = setup_env(monkeypatch, tmp_path, nodes)

    async def main():
        return await asyncio.gather(
            _primitive(client, client.workflowy_add_tag(NODE, "#t1")),
            _primitive(client, client.workflowy_remove_tag(NODE, "#t0")),
        )

    r_add, r_remove = asyncio.run(main())
    assert r_add["success"] and r_remove["success"]
    final_name = html.unescape(fake.state[NODE]["name"])
    assert final_name == "Target #t1"
    # serialized: two distinct read-modify-write windows, both honored
    assert len(fake.posts) == 2


def test_tag_add_and_remove_without_locks_loses_one(monkeypatch, tmp_path):
    """Mutation evidence for the tag-primitive seam: nulled locks → one of the
    two tag effects is lost (stale-base overwrite)."""
    nodes = [{"id": NODE, "name": "Target #t0", "note": "", "parent_id": None}]
    client, fake, _ = setup_env(monkeypatch, tmp_path, nodes)
    nullify_locks(monkeypatch)

    async def main():
        return await asyncio.gather(
            _primitive(client, client.workflowy_add_tag(NODE, "#t1")),
            _primitive(client, client.workflowy_remove_tag(NODE, "#t0")),
        )

    asyncio.run(main())
    final_name = html.unescape(fake.state[NODE]["name"])
    assert final_name != "Target #t1", (
        f"the unlocked run must lose one effect; got {final_name!r}"
    )


# ---------------------------------------------------------------------------
# Test 11 — edit + complete concurrently on one node
# ---------------------------------------------------------------------------


def test_edit_and_complete_concurrent_both_land(monkeypatch, tmp_path):
    nodes = [{"id": NODE, "name": "Target note", "note": NOTE0, "parent_id": None}]
    client, fake, _ = setup_env(monkeypatch, tmp_path, nodes)

    async def main():
        return await asyncio.gather(
            dispatch(server.build_node_registry(), "edit", edit_params(NODE, "beta", "BETA")),
            dispatch(server.build_node_registry(), "complete", {"node_handle": NODE}),
        )

    r_edit, r_complete = asyncio.run(main())
    assert "BETA" in html.unescape(fake.state[NODE]["note"])
    assert fake.state[NODE]["completedAt"] == 999
    assert report_header(r_edit)["success"] is True
    assert r_complete["success"] is True
    # one of the two waited (same {node} key); the completion ack is a dict —
    # the finalizer branch carries the field when IT is the waiter
    probe_sets = {phase for _u, phase, _reg, _task in fake.probes}
    assert {"get", "complete"} <= probe_sets


# ---------------------------------------------------------------------------
# Test 11b — assign_id + tag.add concurrently (A1's entry point)
# ---------------------------------------------------------------------------


def _assign_id_env(monkeypatch, tmp_path):
    nodes = [{"id": NODE, "name": "Target note", "note": "", "parent_id": None}]
    client, fake, paths = setup_env(monkeypatch, tmp_path, nodes)

    async def _noop_identity_proof(paths_, *, operation):
        return None

    monkeypatch.setattr(
        "workflowy_mcp.coherence.admission.require_cache_identity_proof",
        _noop_identity_proof,
    )
    server._ANCHOR_AP_ID_RESERVED_CORES.clear()
    server._ANCHOR_AP_ID_ALLOCATED_CORES.clear()
    return client, fake


def test_assign_id_and_tag_add_concurrent_both_land(monkeypatch, tmp_path):
    client, fake = _assign_id_env(monkeypatch, tmp_path)

    async def main():
        return await asyncio.gather(
            dispatch(server.build_anchor_registry(), "point.assign_id",
                     {"node_handle": NODE, "ap_id": "auto"}),
            _primitive(client, client.workflowy_add_tag(NODE, "#t1")),
        )

    r_assign, r_tag = asyncio.run(main())
    assert r_assign["success"] is True and r_tag["success"] is True
    final_name = html.unescape(fake.state[NODE]["name"])
    # BOTH effects under either serialized order: the AP-ID is assigned AND
    # the tag survives (the assign leg preserves tags; the tag leg preserves
    # the leading [ap:] handle)
    assert re.match(r"^\[ap:[A-Z0-9]{6}\] Target note #t1$", final_name), final_name


def test_assign_id_and_tag_add_without_locks_loses_the_tag(monkeypatch, tmp_path):
    """§9 test 11b mutation evidence: with the helper nulled, one effect is
    lost (observed: the tag — the assign write lands last from a stale base)."""
    client, fake = _assign_id_env(monkeypatch, tmp_path)
    nullify_locks(monkeypatch)

    async def main():
        return await asyncio.gather(
            dispatch(server.build_anchor_registry(), "point.assign_id",
                     {"node_handle": NODE, "ap_id": "auto"}),
            _primitive(client, client.workflowy_add_tag(NODE, "#t1")),
        )

    asyncio.run(main())
    final_name = html.unescape(fake.state[NODE]["name"])
    has_ap = bool(re.match(r"^\[ap:[A-Z0-9]{6}\]", final_name))
    has_tag = final_name.endswith("#t1")
    assert not (has_ap and has_tag), (
        f"the unlocked run must lose one effect; got {final_name!r}"
    )


# ---------------------------------------------------------------------------
# Test 12 — move vs edit on the same node
# ---------------------------------------------------------------------------


def test_move_and_edit_concurrent_both_land(monkeypatch, tmp_path):
    nodes = [
        {"id": NODE, "name": "Target note", "note": NOTE0, "parent_id": P1},
        {"id": P1, "name": "Parent one", "note": "", "parent_id": None},
        {"id": P2, "name": "Parent two", "note": "", "parent_id": None},
    ]
    client, fake, _ = setup_env(monkeypatch, tmp_path, nodes)

    async def main():
        return await asyncio.gather(
            dispatch(server.build_node_registry(), "move",
                     {"node_handle": NODE, "parent_handle": P2, "position": "bottom"}),
            dispatch(server.build_node_registry(), "edit", edit_params(NODE, "beta", "BETA")),
        )

    r_move, r_edit = asyncio.run(main())
    assert r_move["success"] is True
    assert report_header(r_edit)["success"] is True
    # both land: the node moved AND the note edited — never lost
    assert fake.state[NODE]["parent_id"] == P2
    assert "BETA" in html.unescape(fake.state[NODE]["note"])
    # the move ran under its three sorted keys (node, old parent, new parent)
    move_probes = [reg for u, ph, reg, task in fake.probes if ph == "move"]
    assert move_probes and move_probes[0] == frozenset({NODE, P1, P2})


# ---------------------------------------------------------------------------
# Test 13 — extract vs edit on the same source
# ---------------------------------------------------------------------------


def test_extract_and_edit_concurrent_no_lost_text(monkeypatch, tmp_path):
    """The extract's ONE live snapshot is taken under {source, parent}: a
    concurrent edit either precedes it (the extract carries the edit through)
    or follows it (the edit applies to the post-extract note) — the edit is
    NEVER swallowed by the extract's whole-note replacement."""
    nodes = [
        {"id": NODE, "name": "[ap:SRCX] Source", "note": EXTRACT_NOTE, "parent_id": None},
        {"id": P1, "name": "[ap:DESTX] Destination", "note": "", "parent_id": None},
    ]
    client, fake, _ = setup_env(monkeypatch, tmp_path, nodes)

    captured: list[dict] = []

    async def capture_fake(parent_handle, content=None, name=None, ap_id=None,
                           tags=None, target_tab=None, **kwargs):
        await asyncio.sleep(0)
        new_id = f"newpoint-{len(captured)}-0000-0000-000000000000"
        fake.state[new_id] = {
            "name": html.escape(name or "", quote=False),
            "note": html.escape(content or "", quote=False),
            "parent_id": P1, "priority": len(captured),
            "createdAt": 300, "modifiedAt": 300, "completedAt": None, "data": {},
        }
        captured.append({"parent": parent_handle, "content": content, "name": name})
        return {"success": True, "root_node_ids": [new_id], "ap_id": None, "uuid": new_id}

    monkeypatch.setattr(server, "_anchor_point_capture_impl", capture_fake)

    async def main():
        return await asyncio.gather(
            dispatch(server.build_anchor_registry(), "point.extract",
                     {"node_handle": NODE, "parent_handle": P1,
                      "start_line": 2, "end_line": 4, "name": "Extracted"}),
            dispatch(server.build_node_registry(), "edit", edit_params(NODE, "eight", "EIGHT")),
        )

    r_extract, r_edit = asyncio.run(main())
    assert r_extract["success"] is True
    assert report_header(r_edit)["success"] is True
    # the extracted lines are gone from the source; their text landed in the
    # captured point; the EDIT survived the extract's whole-note replacement
    final_note = html.unescape(fake.state[NODE]["note"])
    assert final_note == "one\nfive\nsix\nseven\nEIGHT\nnine\nten"
    assert len(captured) == 1
    assert captured[0]["content"] == "two\nthree\nfour"


# ---------------------------------------------------------------------------
# Test 14 — promote: overlapping items, per-item keys, re-entrant inners
# ---------------------------------------------------------------------------


def _promote_env(monkeypatch, tmp_path):
    nodes = [
        {"id": ROOT, "name": "Root", "note": "", "parent_id": None},
        {"id": DOCK, "name": "Curation dock #wfy-ns", "note": "", "parent_id": ROOT},
        {"id": SUB, "name": "Plain sub-bullet", "note": "", "parent_id": DOCK},
        {"id": ITEM1, "name": "[ap:ITM1] First item", "note": "first body", "parent_id": SUB},
        {"id": ITEM2, "name": "Second item", "note": "second body", "parent_id": SUB},
        {"id": DEST, "name": "Topic dock #wfy-ns", "note": "", "parent_id": ROOT},
    ]
    client, fake, paths = setup_env(monkeypatch, tmp_path, nodes)
    monkeypatch.setattr(server, "_promote_today", lambda: "2026-09-23")

    async def config(_tab):
        return (["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})

    monkeypatch.setattr(server, "_resolve_note_session_config", AsyncMock(side_effect=config))

    async def _noop_identity_proof(paths_, *, operation):
        return None

    monkeypatch.setattr(
        "workflowy_mcp.coherence.admission.require_cache_identity_proof",
        _noop_identity_proof,
    )
    server._ANCHOR_AP_ID_RESERVED_CORES.clear()
    server._ANCHOR_AP_ID_ALLOCATED_CORES.clear()
    return client, fake


def test_promote_overlapping_items_per_item_keys_and_reentrancy(monkeypatch, tmp_path):
    """Two items from the SAME sub-bullet promoted into the SAME destination:
    per-item MOVE key sets {node, immediate parent, destination}; the inner
    move/edit acquisitions are no-op grants (without §6.5 re-entrancy this
    self-deadlocks); `origin` (the Dock ancestor — NOT the immediate parent
    here) is trail text and NEVER a key (A2)."""
    client, fake = _promote_env(monkeypatch, tmp_path)

    async def main():
        return await asyncio.wait_for(
            dispatch(server.build_anchor_registry(), "point.promote", {
                "items": [
                    {"handle": ITEM1, "destination": DEST},
                    {"handle": ITEM2, "destination": DEST},
                ],
            }),
            timeout=15,  # a re-entrancy bug deadlocks → loud timeout, never a hang
        )

    result = asyncio.run(main())
    assert result["success"] is True and result["changed"] is True
    rows = {r["handle"]: r for r in result["detail"]["results"]}
    assert rows[ITEM1]["status"] == "promoted" and rows[ITEM2]["status"] == "promoted"

    # the inner move ran under EXACTLY the outer per-item key set — it
    # acquired no key the outer did not already hold (asserted from inside
    # the fake, per §9 test 14)
    move_probes = {u: reg for u, ph, reg, task in fake.probes if ph == "move"}
    move_task_probes = {u: task for u, ph, reg, task in fake.probes if ph == "move"}
    assert move_probes[ITEM1] == frozenset({ITEM1, SUB, DEST})
    assert move_probes[ITEM2] == frozenset({ITEM2, SUB, DEST})
    # the TASK view agrees: the inner move runs inside the outer per-item hold
    assert move_task_probes[ITEM1] == frozenset({ITEM1, SUB, DEST})
    assert move_task_probes[ITEM2] == frozenset({ITEM2, SUB, DEST})
    # A2: the ORIGIN (nearest Dock ancestor) is NOT a key — origin ≠ immediate
    # parent in this fixture (the items sit under the plain SUB bullet)
    assert DOCK not in move_probes[ITEM1] and DOCK not in move_probes[ITEM2]
    assert rows[ITEM1]["from"]["uuid"] == DOCK  # …yet it IS the trail origin
    assert "Curation dock" in rows[ITEM1]["trail_line"]
    # trail writes landed on both items (2 governed writes per MOVE item)
    assert len(fake.moves) == 2 and len(fake.posts) == 2
    assert not ml._LOCKS.holders and not ml._LOCKS.waiters


def test_promote_derivative_holds_only_the_destination(monkeypatch, tmp_path):
    """DERIVATIVE items lock {destination} only — the source is never written
    (it is read from the pin), so its key is not taken (§6.3)."""
    client, fake = _promote_env(monkeypatch, tmp_path)

    async def main():
        return await asyncio.wait_for(
            dispatch(server.build_anchor_registry(), "point.promote", {
                "items": [{"handle": ITEM1, "destination": DEST, "mode": "derivative"}],
            }),
            timeout=15,
        )

    result = asyncio.run(main())
    assert result["success"] is True
    row = result["detail"]["results"][0]
    assert row["status"] == "promoted" and row["mode"] == "derivative"
    etch_probes = [reg for u, ph, reg, task in fake.probes if ph == "etch"]
    assert etch_probes == [frozenset({DEST})]  # {destination} alone
    # the source is untouched; the copy landed under DEST
    assert fake.state[ITEM1]["note"] == "first body"
    assert fake.state[ITEM1]["parent_id"] == SUB
    assert len(fake.creates) >= 1
    copied = [u for u, _payload in fake.creates]
    assert any(fake.state[u]["parent_id"] == DEST for u in copied)
    assert not ml._LOCKS.holders and not ml._LOCKS.waiters


# ---------------------------------------------------------------------------
# Test 15 — batches stay partial-by-design; per-item locks; CF-04 timeouts
# ---------------------------------------------------------------------------


def _batch_ops(*uuids):
    return [{"handle": u, "operation": "add", "tags": ["x"]} for u in uuids]


def _batch_nodes():
    return [
        {"id": NODE_A, "name": "A", "note": "", "parent_id": None},
        {"id": NODE_B, "name": "B", "note": "", "parent_id": None},
        {"id": NODE_C, "name": "C", "note": "", "parent_id": None},
    ]


def test_batch_mid_item_failure_records_and_continues_locks_released(monkeypatch, tmp_path):
    """I7: batch semantics UNCHANGED — a mid-batch per-item failure is
    recorded and the batch CONTINUES; locks are per write (each item holds
    only its own node's key; the registry is empty afterwards)."""
    client, fake, _ = setup_env(monkeypatch, tmp_path, _batch_nodes())
    fake.fail_update_for = NODE_B

    async def main():
        return await dispatch(
            server.build_tag_registry(), "batch",
            {"operations": _batch_ops(NODE_A, NODE_B, NODE_C)},
        )

    result = asyncio.run(main())
    assert result["success"] is False
    assert result["detail"]["partial_success"] is True
    rows = result["detail"]["results"]
    assert [r["status"] for r in rows] == ["committed", "error", "committed"]
    assert rows[1]["error_kind"] == "commit_error"
    # per-write locks: every I/O probe saw EXACTLY its own item's node held —
    # never two keys at once (no whole-batch lock, §6.8)
    for uuid, phase, registry_held, task_held in fake.probes:
        assert task_held == frozenset({uuid}), (uuid, phase, task_held)
        assert registry_held == frozenset({uuid}), (uuid, phase, registry_held)
    assert not ml._LOCKS.holders and not ml._LOCKS.waiters


def test_batch_per_item_timeout_recorded_on_item_and_batch_continues(monkeypatch, tmp_path):
    """CF-04: a per-item mutation_serialization_timeout is recorded ON that
    item as timed-out-before-any-write — never write_outcome_unknown, and it
    never stops the batch (I7): the following item still commits."""
    client, fake, _ = setup_env(monkeypatch, tmp_path, _batch_nodes())
    monkeypatch.setattr(ml, "_acquire_timeout_seconds", lambda: 0.05)

    async def main():
        release = asyncio.Event()

        async def squatter():
            # a DIFFERENT operation holding NODE_B's key across the batch
            async with _locks(client, NODE_B):
                await release.wait()

        th = create_isolated_context_task(squatter())
        await asyncio.sleep(0.01)
        try:
            return await dispatch(
                server.build_tag_registry(), "batch",
                {"operations": _batch_ops(NODE_A, NODE_B, NODE_C)},
            )
        finally:
            release.set()
            await th

    result = asyncio.run(main())
    assert result["success"] is False and result["detail"]["partial_success"] is True
    rows = result["detail"]["results"]
    # the batch CONTINUED past the timed-out item
    assert [r["status"] for r in rows] == ["committed", "error", "committed"]
    timed_out = rows[1]
    assert timed_out["error_kind"] == "mutation_serialization_timeout"
    assert timed_out["error_kind"] != "write_outcome_unknown"
    assert NODE_B[:8] in timed_out["error"]
    # the timed-out item attempted NO write (timed out before any write)
    assert not any(u == NODE_B for u, _ in fake.posts)
    # the committed items that waited say so per row; NODE_A/NODE_C did not
    assert "serialization" not in rows[0] and "serialization" not in rows[2]
    assert not ml._LOCKS.holders and not ml._LOCKS.waiters


# ---------------------------------------------------------------------------
# P4 end-to-end — the typed refusal through the FULL dispatch kernel
# ---------------------------------------------------------------------------


def test_dispatch_timeout_produces_the_typed_envelope_end_to_end(monkeypatch, tmp_path):
    """P4/A4 end-to-end: a node.edit whose acquisition exceeds the budget
    fails through the real dispatch kernel with the standard make_error
    envelope (error.code + extra.retry_after/contended_keys/timeout_seconds +
    describe_hint), performs ZERO writes, and leaves the registry consistent
    — refuse, never queue."""
    client, fake, _ = setup_env(monkeypatch, tmp_path, _incident_nodes())
    monkeypatch.setattr(ml, "_acquire_timeout_seconds", lambda: 0.05)
    monkeypatch.setattr(ml, "_governor_interval_seconds", lambda: 1.0)

    async def main():
        release = asyncio.Event()

        async def squatter():
            async with _locks(client, NODE):
                await release.wait()

        th = create_isolated_context_task(squatter())
        await asyncio.sleep(0.01)
        try:
            return await dispatch(
                server.build_node_registry(), "edit",
                edit_params(NODE, "alpha", "ALPHA"),
            )
        finally:
            release.set()
            await th

    result = asyncio.run(main())
    assert result["success"] is False
    assert result["operation"] == "node.edit"
    err = result["error"]
    assert err["code"] == "mutation_serialization_timeout"
    assert err["extra"]["retry_after"] == 2.0
    assert err["extra"]["contended_keys"] == [NODE[:8]]
    assert err["extra"]["timeout_seconds"] == 0.05
    assert err["describe_hint"] == {"tool": "node", "verb": "describe", "of": "edit"}
    assert "budget" in err["message"] or "timed out" in err["message"]
    # ZERO writes, ZERO reads of the note — refused BEFORE the live read
    assert fake.posts == [] and fake.gets == []
    assert not ml._LOCKS.holders and not ml._LOCKS.waiters


# ---------------------------------------------------------------------------
# Test 16 — the WS front inherits the per-item locks
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


def test_ws_tag_front_inherits_per_item_locks(monkeypatch, tmp_path):
    """§9 test 16: a mutate_node_tags wire batch holds PER-ITEM locks (probed
    from inside the fake), rows carry their own attribution, and the reply
    carries the §6.7 top-level field when the WS operation waited."""
    nodes = [
        {"id": NODE_A, "name": "A", "note": "", "parent_id": None},
        {"id": NODE_B, "name": "B", "note": "", "parent_id": None},
    ]
    client, fake, _ = setup_env(monkeypatch, tmp_path, nodes)

    async def main():
        release = asyncio.Event()

        async def squatter():
            async with _locks(client, NODE_B):
                await asyncio.sleep(0.15)
                release.set()

        th = create_isolated_context_task(squatter())
        await asyncio.sleep(0.01)
        ws = _FakeWebSocket()
        await server._handle_mutate_node_tags(
            {
                "action": "mutate_node_tags",
                "request_id": "r-u01",
                "endpoint_id": "ep",
                "v": 1,
                "operations": [
                    {"handle": NODE_A, "operation": "add", "tags": ["x"]},
                    {"handle": NODE_B, "operation": "add", "tags": ["y"]},
                ],
            },
            ws,
        )
        await th
        return ws

    ws = asyncio.run(main())
    assert len(ws.sent) == 1
    reply = ws.sent[0]
    assert reply["action"] == "mutate_node_tags_result" and reply["request_id"] == "r-u01"
    assert reply["success"] is True
    rows = {r["uuid"]: r for r in reply["results"]}
    assert rows[NODE_A]["status"] == "committed" and rows[NODE_B]["status"] == "committed"
    # per-item locks: the BATCH TASK held exactly its own item's key at every
    # I/O point (a whole-batch lock would show both); the registry view may
    # additionally carry the external squatter's key — that is the point of
    # the task-scoped probe
    for uuid, phase, registry_held, task_held in fake.probes:
        assert task_held == frozenset({uuid}), (uuid, phase, task_held)
        assert uuid in registry_held, (uuid, phase, registry_held)
    # the NODE_B item WAITED (the squatter held its key) → its row carries
    # the attribution, and the reply carries the top-level field
    assert "serialization" in rows[NODE_B]
    assert rows[NODE_B]["serialization"]["waited_on"] == [NODE_B[:8]]
    assert "serialization" not in rows[NODE_A]
    assert reply["serialization"]["waited_on"] == [NODE_B[:8]]
    assert reply["serialization"]["waited_seconds"] > 0
    assert not ml._LOCKS.holders and not ml._LOCKS.waiters


def test_ws_tag_front_uncontended_reply_has_no_serialization(monkeypatch, tmp_path):
    """§10: the uncontended WS wire shape is unchanged — no field anywhere."""
    nodes = [{"id": NODE_A, "name": "A", "note": "", "parent_id": None}]
    client, fake, _ = setup_env(monkeypatch, tmp_path, nodes)

    async def main():
        ws = _FakeWebSocket()
        await server._handle_mutate_node_tags(
            {
                "action": "mutate_node_tags",
                "request_id": "r-u01b",
                "endpoint_id": "ep",
                "v": 1,
                "operations": [{"handle": NODE_A, "operation": "add", "tags": ["x"]}],
            },
            ws,
        )
        return ws

    ws = asyncio.run(main())
    reply = ws.sent[0]
    assert reply["success"] is True
    assert "serialization" not in reply
    assert "serialization" not in reply["results"][0]


# ---------------------------------------------------------------------------
# Test 17 — create_isolated_context_task clears the held set (A6 regression)
# ---------------------------------------------------------------------------


def test_create_isolated_context_task_clears_the_held_set(monkeypatch, tmp_path):
    """§6.5's MUST-MODIFY regression: a NEW-operation spawn starts with an
    empty held set — it does NOT inherit its submitter's keys (and therefore
    genuinely waits for them instead of skipping the acquisition), while a
    same-operation subtask (plain create_task) legitimately shares them."""
    monkeypatch.setattr(ml, "_acquire_timeout_seconds", lambda: 0.05)
    client, _fake, _paths = setup_env(monkeypatch, tmp_path, _batch_nodes())

    async def main():
        async with _locks(client, NODE_A):
            # same-operation subtask: shares the held set (intended §6.5)
            inherit = {}

            async def plain_child():
                inherit["held"] = held_mutation_lock_keys()
                inherit["state_shared"] = MUTATION_LOCK_STATE.get() is not None

            await asyncio.create_task(plain_child())
            assert inherit["held"] == frozenset({NODE_A})
            assert inherit["state_shared"] is True

            # NEW-operation spawn: cleared to None — empty held set
            fresh = {}

            async def isolated_child():
                fresh["held"] = held_mutation_lock_keys()
                fresh["state"] = MUTATION_LOCK_STATE.get()
                async with _locks(client, NODE_B):
                    fresh["own"] = held_mutation_lock_keys()

            await create_isolated_context_task(isolated_child())
            assert fresh["held"] == frozenset()
            assert fresh["state"] is None
            assert fresh["own"] == frozenset({NODE_B})

            # the regression that matters: the spawned job does NOT skip the
            # acquisition it does not hold — it genuinely waits and hits the
            # bounded refusal while the submitter holds the key
            async def grab_held_key():
                try:
                    async with _locks(client, NODE_A):
                        return "acquired-without-waiting"  # the §6.5 bug
                except MutationSerializationTimeoutError:
                    return "refused-after-waiting"

            verdict = await create_isolated_context_task(grab_held_key())
            assert verdict == "refused-after-waiting"

    asyncio.run(main())
    assert not ml._LOCKS.holders and not ml._LOCKS.waiters
