"""N04 G1 real-path witnesses: real CPU + driver + production FILE wrapper.

Contract: [ap:TIGASU] §6.1-6.5, §7, §9.1-9.3 and the LOCK amendments A1/A3.
Owner: N04 G1 (real singleton payoff). Selection and receipts belong to the leg.

WHAT IS REAL HERE (never replaced by a fake):

* the real data-only generator ``carto_reconcile_cpu._reconcile_steps`` -- its
  ``ReconcileCreateRun`` / ``ReconcileCreateResult`` protocol, its placement
  planner, ``_adopt_create_order`` and the unchanged MOVE/REORDER phases;
* the real loop driver ``workflowy_move_reconcile.reconcile_tree`` (typed
  requests, on-loop callbacks, off-loop sequential data advance,
  ``create_parent_done`` / ``conclude_create_parent``);
* the real production wrapper ``WorkFlowyClientNexus.refresh_file_node_beacons``
  -- its nested ``create_wrapper`` and ``conclude_create_parent`` closures, the
  cheap-path selection, the verified-prefix accounting and the J retention and
  status code;
* the real ``TransitionCreateTransaction`` (Layer C), ``LlmInsertAfterTransport``,
  ``PublicV1CreateTransport``, ``_send_workflowy_write`` and a real
  ``CartoWriteGovernor`` on a fake clock;
* one real ``WorkFlowyClientNexus`` whose HTTP client is an
  ``httpx.MockTransport`` over a literal ordered fake account using the
  P0-prime wire shapes (v1 stores one name decode, LLM stores ``n`` directly,
  listings are not wire-sorted, every write renumbers every sibling priority);
* a real N05 grant: ``mutation_region_locks`` over the real
  ``build_region_subtree(FILE)`` geometry ``_run_carto_job`` uses, held for the
  whole refresh exactly as the job runner holds it.

WHAT IS CONTROLLED, and only this: the outer per-file preparation seams
(``_prepare_carto_file_facts`` / ``_prepare_carto_file_tree`` /
``_prepare_carto_source_file`` / ``_log_carto_source_messages`` /
``_plan_carto_file_refresh`` / ``_write_carto_file_artifact`` /
``_log_to_file_helper``) hand the reconcile an explicit ``source_json`` instead
of parsing a source file, so no reconcile, transport or Layer-C behaviour is
faked. ``tc._request_recovery`` is stubbed so an ambiguity witness never
schedules the real coalescer. Nothing else is replaced.

No live Workflowy or LLM call, no credential read, no corpus path is named.

MUTANTS. ``N04_G1_MUTANT=<name>`` (or ``install_n04_mutant``) installs ONE named
counterfactual in the test process only: a source copy of the current disk
source with exactly one decision restored to its old shape. ``_RED_TABLE`` names, for every new
behaviour, the witness and the mutant that must turn it RED; the plain witness
is the GREEN half. Production bytes are never edited.

DISCLOSED GAPS (see the leg Result for the full list):
* G1 is singleton-only here by dispatch: the contiguous-run/k>1 faces are G2
  (``tests/test_n04_create_runs.py``-style cells and T07), not claimed here.
* This file exercises the production FILE wrapper with a real factory grant;
  the real daemon-task bind and forwarding paths have separate T03 witnesses
  in ``test_n04_create_runs.py``. No daemon process or live endpoint starts.
* Cancellation of a possibly-dispatched send, the breakpoint recheck and the
  Phase-B lane fence stay with T09's own cells.
"""
from __future__ import annotations

import asyncio
import ast
import dataclasses
import html
import inspect
import json
import os
import textwrap
from collections import Counter
from uuid import UUID

import httpx
import pytest
from test_carto_write_governor_phase_b import FakeClock

from workflowy_mcp.carto_write_governor import CartoWriteGovernor, WriteGovernorSettings
from workflowy_mcp.client import api_client_nexus as acn
from workflowy_mcp.client import carto_reconcile_cpu as cpu
from workflowy_mcp.client import create_transport as transport_module
from workflowy_mcp.client import workflowy_move_reconcile as driver
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
from workflowy_mcp.client.create_transport import LlmInsertAfterTransport, SendStage
from workflowy_mcp.client.insert_capability import (
    InsertCapabilityController,
    InsertCapabilityState,
)
from workflowy_mcp.coherence import transition_create as tc
from workflowy_mcp.coherence.barrier import hard_stop_state
from workflowy_mcp.coherence.cache_view import CacheView
from workflowy_mcp.coherence.errors import CoherenceBarrierActiveError
from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
from workflowy_mcp.coherence.schema import (
    initialize_manifest_if_absent,
    new_bootstrap_manifest,
    read_external_dirt,
)
from workflowy_mcp.models import (
    APIConfiguration,
    NetworkError,
    NodeCreateRequest,
    NodeListRequest,
    RateLimitError,
)
from workflowy_mcp.mutation_lease_governance import build_region_subtree
from workflowy_mcp.mutation_locks import mutation_region_locks

BASE_URL = "https://workflowy.com/api/v1"
TEST_API_KEY = "n04-g1-offline-key"
LLM_PATH = "/api/llm/doc/edit"

FILE = str(UUID(int=(0xA04E0 << 48) | 1))
OTHER = str(UUID(int=(0xA04E0 << 48) | 2))
EXISTING_PREFIX = 0xA04E1
CREATED_PREFIX = 0xA04E2


# ---------------------------------------------------------------------------
# Literal fake account: real wire shapes, real ordering, counted writes
# ---------------------------------------------------------------------------


def _identifier(prefix: int, index: int) -> str:
    return str(UUID(int=(prefix << 48) | index))


def _row(handle, name, priority, parent, *, note: str = "") -> dict:
    return {"id": handle, "name": name, "note": note, "priority": priority,
            "parent_id": parent, "modifiedAt": 1_700_000_000}


class FakeAccount:
    """An ordered Workflowy account: it stores actual rows and reorders them.

    Counters are taken at the real boundaries: a "top move" is a POST to
    ``/nodes/<id>/move`` with ``position == "top"`` (never parsed from a log),
    an LLM op is one POST to ``/api/llm/doc/edit`` carrying all its items, a v1
    create invocation is one POST to ``/nodes/``.
    """

    def __init__(self, source_path: str = "/tmp/fixture.py") -> None:
        self.source_path = source_path
        self.parents = {
            FILE: _row(FILE, "fixture.py", 0, None, note=f"Path: {source_path}"),
            OTHER: _row(OTHER, "other.py", 0, None, note="Path: /tmp/other.py"),
        }
        self.rows: dict[str, list[dict]] = {FILE: [], OTHER: []}
        self.counts = Counter()
        self.lists = Counter()               # listings per parent
        self.events: list[tuple] = []        # ordered lifecycle records
        self.created: list[str] = []
        self.v1_bodies: list[dict] = []
        self.llm_script: list = []           # consumed per LLM attempt ("ok" default)
        self.get_all_429 = False             # sticky listing rate result
        self.wire_reverse = False            # listing not wire-sorted

    # -- fixture -------------------------------------------------------------
    def seed(self, parent: str, rows: list[dict]) -> None:
        """Store the rows where their own parent_id says they live."""
        buckets = {parent: []}
        for row in rows:
            buckets.setdefault(row.get("parent_id", parent), []).append(dict(row))
        self.rows.update(buckets)

    def export_nodes(self) -> dict:
        nodes = []
        for parent, rows in self.rows.items():
            if parent in self.parents:
                nodes.append(dict(self.parents[parent]))
            nodes.extend(dict(row) for row in rows)
        return {"nodes": nodes}

    def _children(self, parent: str) -> list[dict]:
        return self.rows.setdefault(parent, [])

    def _parent_for_root(self, root: str) -> str:
        """The LLM envelope carries only the last 12 hex digits of the parent."""
        candidates = set(self.parents)
        candidates.update(row.get("parent_id") for rows in self.rows.values()
                          for row in rows if row.get("parent_id"))
        for parent in candidates:
            if UUID(parent).hex[-12:] == root:
                return parent
        raise AssertionError(f"no fake-account parent matches root {root!r}")

    # -- views ---------------------------------------------------------------
    def order(self, parent: str = FILE) -> list[str]:
        return [row["id"] for row in self.rows[parent]]

    def contents(self, parent: str = FILE) -> list[tuple[str, str]]:
        return [(row["name"], row.get("note") or "") for row in self.rows[parent]]

    def _renumber(self, parent: str) -> None:
        for index, row in enumerate(self.rows[parent]):
            row["priority"] = 100 * (index + 1)

    def _new_id(self, parent: str) -> str:
        handle = _identifier(CREATED_PREFIX, len(self.created))
        self.created.append(handle)
        return handle

    # -- HTTP ----------------------------------------------------------------
    async def __call__(self, request: httpx.Request):
        path = request.url.path
        if path.endswith(LLM_PATH):
            return await self._llm(request)
        if request.method == "GET" and path.endswith("/nodes"):
            return self._listing(request)
        if request.method == "POST" and path.endswith("/nodes/"):
            return self._v1_create(request)
        if request.method == "POST" and path.endswith("/move"):
            return self._move(request)
        raise AssertionError(f"unexpected fake-account request {request.method} {path}")

    def _listing(self, request: httpx.Request):
        parent = request.url.params.get("parent_id")
        known = parent in self.parents or any(
            row.get("parent_id") == parent for rows in self.rows.values() for row in rows)
        assert known, f"listing request for an unknown fake-account parent {parent!r}"
        rows_for_parent = self.rows.setdefault(parent, [])
        self.counts["list"] += 1
        self.lists[parent] += 1
        self.events.append(("list", parent))
        if self.get_all_429:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={})
        rows = [dict(row) for row in rows_for_parent]
        if self.wire_reverse:
            rows = list(reversed(rows))
        return httpx.Response(200, json={"nodes": rows})

    def _v1_create(self, request: httpx.Request):
        body = json.loads(request.content)
        self.v1_bodies.append(dict(body))
        parent = body["parent_id"]
        position = body.get("position") or "top"
        self.counts["v1_create"] += 1
        self.events.append(("v1_create", body["name"], position))
        handle = self._new_id(parent)
        row = _row(handle, html.unescape(body["name"]), 0, parent, note=body.get("note") or "")
        if position == "top":
            self.rows.setdefault(parent, []).insert(0, row)
        else:
            self.rows.setdefault(parent, []).append(row)
        self._renumber(parent)
        return httpx.Response(200, json={"item_id": handle})

    async def _llm(self, request: httpx.Request):
        body = json.loads(request.content)
        operation = body["operations"][0]
        parent = self._parent_for_root(body["root"])
        mode = self.llm_script.pop(0) if self.llm_script else "ok"
        self.counts["llm_ops"] += 1
        self.counts["llm_items"] += len(operation["items"])
        self.events.append(("llm", tuple(item["n"] for item in operation["items"]), mode))
        if mode == 429:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        if mode in (401, 403, 404, 405):
            return httpx.Response(mode, json={"message": "LLM endpoint unavailable"})
        if mode == "read_required":
            return httpx.Response(400, json={"code": "read_required"})
        if mode == "op_error":
            return httpx.Response(200, json={"results": [
                {"op": "insert", "status": "error", "error": "not_found"}]})
        if mode == "absent_timeout":
            raise httpx.ReadTimeout("lost reply, no create committed")
        if mode == "schema":
            return httpx.Response(200, json={"unexpected_schema": True})
        index = self.order(parent).index(operation["after"]) + 1
        for item in operation["items"]:
            handle = self._new_id(parent)
            self.rows[parent].insert(
                index, _row(handle, item["n"], 0, parent, note=item.get("d") or ""))
            index += 1
        self._renumber(parent)
        if mode == "duplicate":
            item = operation["items"][0]
            self.rows[parent].insert(index, _row(self._new_id(parent), item["n"], 0, parent,
                                                note=item.get("d") or ""))
            self._renumber(parent)
        if mode == "malformed":
            return httpx.Response(200, json={"created": 1})
        if mode == "decode":
            # The insert committed but the reply could not be decoded.
            raise httpx.DecodingError("reply body could not be decoded")
        if mode == "timeout":
            raise httpx.ReadTimeout("acknowledgement lost after commit")
        return httpx.Response(200, json={"results": [
            {"op": "insert", "status": "ok", "created": len(operation["items"])}]})

    def _move(self, request: httpx.Request):
        handle = request.url.path.split("/")[-2]
        body = json.loads(request.content)
        target = body.get("parent_id") or FILE
        position = body.get("position", "top")
        self.counts["move_top" if position == "top" else "move_bottom"] += 1
        self.events.append(("move", handle, position))
        source = next(parent for parent, rows in self.rows.items()
                      if any(row["id"] == handle for row in rows))
        row = next(row for row in self.rows[source] if row["id"] == handle)
        self.rows[source].remove(row)
        row["parent_id"] = target
        if position == "top":
            self.rows.setdefault(target, []).insert(0, row)
        else:
            self.rows.setdefault(target, []).append(row)
        self._renumber(source)
        if target != source:
            self._renumber(target)
        return httpx.Response(200, json={"status": "ok"})


# ---------------------------------------------------------------------------
# Source JSON helpers
# ---------------------------------------------------------------------------


def _node(name, *, handle=None, children=(), note="", data=None, position_hint=None):
    node = {"name": name, "note": note, "children_status": "complete",
            "children": list(children)}
    if handle is not None:
        node["id"] = handle
    if data is not None:
        node["data"] = data
    if position_hint is not None:
        node["_position_hint"] = position_hint
    return node


def _source(nodes, parent=FILE):
    return {"export_root_id": parent, "export_root_children_status": "complete",
            "nodes": nodes}


class Geometry:
    def __init__(self, source_nodes, rows, expected_names, new_indices):
        self.source_nodes = source_nodes
        self.rows = rows
        self.expected_names = expected_names
        self.new_indices = new_indices

    @property
    def final_children(self) -> int:
        return len(self.expected_names)

    def expected_ids(self) -> list[str]:
        """Existing ids in expected order; created slots are None."""
        out, created = [], 0
        for index, node in enumerate(self.source_nodes):
            if index in self.new_indices:
                out.append(None)
                created += 1
            else:
                out.append(node.get("id"))
        assert len(out) == len(self.expected_names)
        return out


def _linear_geometry(existing_names, insert_after):
    """Existing children in order with one new sibling after each named index."""
    rows = [_row(_identifier(EXISTING_PREFIX, index), name, 100 * (index + 1), FILE)
            for index, name in enumerate(existing_names)]
    source_nodes, expected, new_indices, added = [], [], [], 0
    for index, name in enumerate(existing_names):
        source_nodes.append(_node(name, handle=rows[index]["id"]))
        expected.append(name)
        if index in insert_after:
            new_name = f"added_{added:02d}"
            new_indices.append(len(source_nodes))
            source_nodes.append(_node(new_name))
            expected.append(new_name)
            added += 1
    return Geometry(source_nodes, rows, expected, new_indices)


def app_js_scattered():
    """§9.3: 102 existing + 9 interior symbols after existing indices 5..93."""
    return _linear_geometry([f"sym_{index:03d}" for index in range(102)],
                            {5, 16, 27, 38, 49, 60, 71, 82, 93})


def server_py_group():
    """§9.3: 259 existing + 1 interior symbol after existing index 128."""
    return _linear_geometry([f"sym_{index:03d}" for index in range(259)], {128})


def small_interior():
    """Three existing children plus one inserted after the first."""
    return _linear_geometry(["alpha", "beta", "gamma"], {0})


# ---------------------------------------------------------------------------
# Harness: one real client, real governor, real grant, faked HTTP account
# ---------------------------------------------------------------------------


def _build_client(tmp_path, account: FakeAccount):
    clock = FakeClock()
    client = WorkFlowyClientNexus(APIConfiguration(api_key=TEST_API_KEY, base_url=BASE_URL))
    governor = CartoWriteGovernor(
        account_scope_id=client._write_governor.paths.account_scope_id,
        jobs_root=tmp_path / "jobs", clock=clock,
        settings=WriteGovernorSettings(
            interval_seconds=1.0, max_wait_before_refusal_seconds=30_000.0,
            breaker_initial_cooldown_seconds=10.0, breaker_max_cooldown_seconds=20.0,
            probe_lease_seconds=5.0, retry_after_fallback_seconds=60.0,
            mutex_wait_seconds=2.0, mutex_poll_seconds=0.001,
        ),
    )
    client._write_governor = governor
    client._insert_capability = InsertCapabilityController(
        governor.paths.account_scope_id, clock=lambda: clock.value, v1_only=lambda: False)
    client._client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(account))
    initialize_manifest_if_absent(
        client._coherence_paths(), new_bootstrap_manifest(governor.paths.account_scope_id))
    view = client._cache_view_publisher.capture()
    published = CacheView.build(
        epoch_number=0, epoch_id="f" * 32, adopted_sequence=0,
        nodes_export=account.export_nodes(), account_scope_id=view.account_scope_id,
    )
    client._cache_view_publisher.publish(published)
    sleeps: list[tuple[float, str]] = []

    async def sleep(seconds, operation):
        client._raise_if_operation_cancelled(operation)
        sleeps.append((seconds, operation))
        clock.advance(seconds)
        await asyncio.sleep(0)
        client._raise_if_operation_cancelled(operation)

    client._sleep_with_operation_cancel = sleep
    before = {node_id: row.get("priority") for node_id, row in published.nodes_by_id.items()}
    return client, governor, clock, sleeps, published, before


def _install_outer_preparation(monkeypatch, tmp_path, source_json, recovery):
    """Control ONLY the per-file preparation seams; the reconcile stays real."""

    def facts(flat_nodes, file_node_id, nodes_by_id_cache, source_path_override):
        return {"source_path": str(tmp_path / "fixture.py"),
                "repo_root_abs": None, "is_directory": False}

    def plan(parsed, ether_tree, metadata):
        if False:  # pragma: no cover - makes this a generator, as production is
            yield None
        return {"source_json": source_json, "pending_file_note": None, "warnings": [],
                "projection": {"state": "complete"},
                "header_freshness": {"available": True, "reason": None}}

    def artifact(kind, values):
        if kind == "source":
            return {"source_json_path": str(tmp_path / "n04-source.json"),
                    "debug_path": str(tmp_path / "n04-debug.log")}
        if kind == "journal-open":
            return str(tmp_path / "journal.json")
        return None

    async def no_recovery():
        recovery.append("scheduled")

    monkeypatch.setattr(acn, "_prepare_carto_file_facts", facts)
    monkeypatch.setattr(acn, "_prepare_carto_file_tree", lambda flat_nodes: [])
    monkeypatch.setattr(
        acn, "_prepare_carto_source_file",
        lambda path, repo_root, **kwargs: {"messages": [], "prepared": {"status": "ok"}})
    monkeypatch.setattr(acn, "_log_carto_source_messages", lambda messages: None)
    monkeypatch.setattr(acn, "_plan_carto_file_refresh", plan)
    monkeypatch.setattr(acn, "_write_carto_file_artifact", artifact)
    monkeypatch.setattr(acn, "_log_to_file_helper",
                        lambda message, log_type="reconcile": None)
    monkeypatch.setattr(tc, "_request_recovery", no_recovery)


class Run:
    def __init__(self, account, client, governor, clock, sleeps, result, error,
                 source_nodes, conclusions, view, priorities_before):
        self.account = account
        self.client = client
        self.governor = governor
        self.clock = clock
        self.sleeps = sleeps
        self.result = result
        self.error = error
        self.source_nodes = source_nodes
        self.conclusions = conclusions
        self.view = view
        self.priorities_before = priorities_before

    def assertion_prelude(self):
        assert self.error is None, self.error
        assert self.result is not None

    @property
    def state(self):
        return self.client._insert_capability.snapshot().state


async def _invoke(client, grant):
    try:
        return await client.refresh_file_node_beacons(FILE, held_grant=grant), None
    except BaseException as error:  # noqa: BLE001 - the witness inspects this exact error
        if not isinstance(error, (Exception, asyncio.CancelledError)):
            raise
        return None, error


def run_refresh(tmp_path, monkeypatch, *, geometry: Geometry, account: FakeAccount | None = None,
                 v1_only=False, prime_available=False, grant=True, dirty_before=(),
                 spy_conclusions=False, controller_mode=None):
    account = account or FakeAccount()
    account.seed(FILE, geometry.rows)
    recovery: list[str] = []
    conclusions: list[tuple] = []

    async def main():
        client, governor, clock, sleeps, view, priorities_before = _build_client(tmp_path, account)
        client._insert_capability = InsertCapabilityController(
            governor.paths.account_scope_id, clock=lambda: clock.value,
            v1_only=lambda: v1_only)
        if prime_available:
            selection = client._insert_capability.select_for_insert()
            client._insert_capability.record_success(selection)
        occupied = None
        if controller_mode == "degraded":
            selection = client._insert_capability.select_for_insert()
            client._insert_capability.record_capability_failure(selection, "unsupported_endpoint")
        elif controller_mode == "occupied":
            occupied = client._insert_capability.select_for_insert()
        for handle in dirty_before:
            client._mark_nodes_export_dirty([handle])
        if spy_conclusions:
            original = tc.TransitionCreateTransaction.conclude

            async def spy(self, *, relist=False):
                mark = len(account.events)
                witness = await original(self, relist=relist)
                assert self._latest is None, "conclusion retained a live CREATE witness"
                conclusions.append((self.parent_id, relist, mark))
                return witness

            monkeypatch.setattr(tc.TransitionCreateTransaction, "conclude", spy)
        _install_outer_preparation(monkeypatch, tmp_path, _source(geometry.source_nodes), recovery)
        async with client._client:
            async with cache_operation_boundary(client, "n04-g1"):
                if grant:
                    async with mutation_region_locks(
                            client=client, operation_label="n04-g1",
                            region_factory=lambda snapshot: (
                                build_region_subtree(FILE, cache_by_id=snapshot.nodes_by_id),),
                    ) as held_grant:
                        result, error = await _invoke(client, held_grant)
                else:
                    result, error = await _invoke(client, None)
        if occupied is not None:
            client._insert_capability.record_neutral(occupied)
        run = Run(account, client, governor, clock, sleeps, result, error,
                  geometry.source_nodes, conclusions, view, priorities_before)
        run.recovery = recovery
        return run

    return asyncio.run(main())


def _assert_no_cache_rank_write(run: Run) -> None:
    """The published view's own rows keep their server priorities.

    ``_adopt_create_order`` owns the private reconcile model only; a rank must
    never be written through to the published cache payload (which is frozen
    read-only for the whole test session, so a write raises at the site).
    """
    after = {node_id: row.get("priority") for node_id, row in run.view.nodes_by_id.items()}
    assert after == run.priorities_before, "a private order rank was written into the published cache view"


# ---------------------------------------------------------------------------
# T01 · G1 payoff and parity on the exact D11 geometries
# ---------------------------------------------------------------------------


def witness_t01_app_js_scattered_healthy(tmp_path, monkeypatch, *, cold=False, prime=True):
    geometry = app_js_scattered()
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, prime_available=prime,
                      dirty_before=(FILE,) if cold else ())
    run.assertion_prelude()
    account = run.account
    assert run.result["success"] is True
    assert len(account.rows[FILE]) == 111
    assert [name for name, _ in account.contents()] == geometry.expected_names
    assert account.counts["llm_ops"] == 9
    assert account.counts["llm_items"] == 9
    assert account.counts["v1_create"] == 0
    assert account.counts["move_top"] == 0, [event for event in account.events if event[0] == "move"]
    assert account.counts["move_bottom"] == 0
    assert account.lists[FILE] == 9 + (1 if cold else 0), dict(account.lists)
    assert [geometry.source_nodes[index].get("id") for index in geometry.new_indices] == account.created
    assert all(len(handle) == 36 for handle in account.created)
    assert read_external_dirt(run.client._coherence_paths(), required=False).active is False
    _assert_no_cache_rank_write(run)
    return run


def test_t01_app_js_scattered_healthy_available_warm(tmp_path, monkeypatch):
    run = witness_t01_app_js_scattered_healthy(tmp_path, monkeypatch, cold=False, prime=True)
    assert run.state == InsertCapabilityState.AVAILABLE


def test_t01_app_js_scattered_healthy_unknown_cold(tmp_path, monkeypatch):
    run = witness_t01_app_js_scattered_healthy(tmp_path, monkeypatch, cold=True, prime=False)
    assert run.state == InsertCapabilityState.AVAILABLE


def witness_t01_server_py_group_healthy(tmp_path, monkeypatch, *, cold=False, prime=False):
    geometry = server_py_group()
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, prime_available=prime,
                      dirty_before=(FILE,) if cold else ())
    run.assertion_prelude()
    account = run.account
    assert len(account.rows[FILE]) == 260
    assert [name for name, _ in account.contents()] == geometry.expected_names
    assert account.counts["llm_ops"] == 1 and account.counts["llm_items"] == 1
    assert account.counts["v1_create"] == 0
    assert account.counts["move_top"] == 0
    assert account.lists[FILE] == 1 + int(cold)
    assert [geometry.source_nodes[index].get("id") for index in geometry.new_indices] == account.created
    _assert_no_cache_rank_write(run)
    return run


@pytest.mark.parametrize("cold,prime", [(False, False), (False, True), (True, False)])
def test_t01_server_py_group_healthy_singleton(tmp_path, monkeypatch, cold, prime):
    witness_t01_server_py_group_healthy(tmp_path, monkeypatch, cold=cold, prime=prime)


@pytest.mark.parametrize("geometry_factory,expected_moves,expected_children", [
    (app_js_scattered, 111, 111),
    (server_py_group, 260, 260),
])
@pytest.mark.parametrize("cold", [False, True])
def test_t01_forced_v1_reproduces_old_move_counts(tmp_path, monkeypatch, geometry_factory,
                                                  expected_moves, expected_children, cold):
    geometry = geometry_factory()
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, v1_only=True,
                      dirty_before=(FILE,) if cold else ())
    run.assertion_prelude()
    account = run.account
    assert len(account.rows[FILE]) == expected_children
    assert [name for name, _ in account.contents()] == geometry.expected_names
    assert account.counts["llm_ops"] == 0
    assert account.counts["v1_create"] == len(geometry.new_indices)
    assert account.counts["move_top"] == expected_moves, account.counts["move_top"]
    assert account.lists[FILE] == 0
    assert run.state == InsertCapabilityState.FORCED_V1


# ---------------------------------------------------------------------------
# T02 · boundary, cheap paths and no verification listing
# ---------------------------------------------------------------------------


def _ten_existing():
    rows = [_row(_identifier(EXISTING_PREFIX, index), f"sym_{index:03d}",
                 100 * (index + 1), FILE) for index in range(10)]
    return rows


def witness_t02_head_run_top_boundary(tmp_path, monkeypatch):
    rows = [_row(_identifier(EXISTING_PREFIX, index), f"sym_{index:03d}", 100 * (index + 1), FILE)
            for index in range(10)]
    source = [_node("leading")]
    source += [_node(f"sym_{index:03d}", handle=rows[index]["id"]) for index in range(10)]
    geometry = Geometry(source, rows, ["leading"] + [row["name"] for row in rows], [0])
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry)
    run.assertion_prelude()
    account = run.account
    # §6.2: a leading run under a covered existing parent uses v1's top
    # placement -- never a Layer-C listing and never a reorder.
    assert account.counts["llm_ops"] == 0
    assert account.counts["v1_create"] == 1
    assert [event for event in account.events if event[0] == "v1_create"][0][2] == "top"
    assert [name for name, _ in account.contents()] == geometry.expected_names
    assert account.counts["move_top"] == 0
    assert account.lists[FILE] == 0, "a boundary placement must not pay a verification listing"
    return run


def test_t02_head_run_uses_top_boundary_without_listing(tmp_path, monkeypatch):
    witness_t02_head_run_top_boundary(tmp_path, monkeypatch)


def witness_t02_tail_run_bottom_boundary(tmp_path, monkeypatch):
    rows = [_row(_identifier(EXISTING_PREFIX, index), f"sym_{index:03d}", 100 * (index + 1), FILE)
            for index in range(10)]
    source = [_node(f"sym_{index:03d}", handle=rows[index]["id"]) for index in range(10)]
    source.append(_node("trailing"))
    geometry = Geometry(source, rows, [row["name"] for row in rows] + ["trailing"], [10])
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry)
    run.assertion_prelude()
    account = run.account
    assert account.counts["llm_ops"] == 0
    assert account.counts["v1_create"] == 1
    assert [event for event in account.events if event[0] == "v1_create"][0][2] == "bottom"
    assert [name for name, _ in account.contents()] == geometry.expected_names
    assert account.counts["move_top"] == 0
    assert account.lists[FILE] == 0
    return run


def test_t02_tail_run_uses_bottom_boundary_without_listing(tmp_path, monkeypatch):
    witness_t02_tail_run_bottom_boundary(tmp_path, monkeypatch)


def witness_t02_warning_hint_is_honoured(tmp_path, monkeypatch):
    rows = [_row(_identifier(EXISTING_PREFIX, index), f"sym_{index:03d}", 100 * (index + 1), FILE)
            for index in range(6)]
    source = [_node("warning", position_hint="top")]
    source += [_node(f"sym_{index:03d}", handle=rows[index]["id"]) for index in range(6)]
    geometry = Geometry(source, rows, ["warning"] + [row["name"] for row in rows], [0])
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry)
    run.assertion_prelude()
    account = run.account
    creates = [event for event in account.events if event[0] == "v1_create"]
    assert len(creates) == 1 and creates[0][2] == "top", creates
    assert account.rows[FILE][0]["name"] == "warning"
    assert [name for name, _ in account.contents()] == geometry.expected_names
    assert account.counts["move_top"] == 0
    assert account.counts["llm_ops"] == 0 and account.lists[FILE] == 0
    return run


def test_t02_warning_position_hint_reaches_the_real_wrapper(tmp_path, monkeypatch):
    witness_t02_warning_hint_is_honoured(tmp_path, monkeypatch)


def witness_t02_nonbullet_layout_stays_v1(tmp_path, monkeypatch):
    rows = _ten_existing()
    source = [_node(f"sym_{index:03d}", handle=rows[index]["id"]) for index in range(5)]
    source.append(_node("heading", data={"layoutMode": "h1"}))
    source += [_node(f"sym_{index:03d}", handle=rows[index]["id"]) for index in range(5, 10)]
    expected = [row["name"] for row in rows[:5]] + ["heading"] + [row["name"] for row in rows[5:]]
    geometry = Geometry(source, rows, expected, [5])
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry)
    run.assertion_prelude()
    account = run.account
    # Explicit non-bullet layout never becomes an LLM insert and pays no
    # transaction baseline: today's bare-v1 bottom create + existing reorder.
    assert account.counts["llm_ops"] == 0
    assert account.counts["v1_create"] == 1
    assert account.lists[FILE] == 0
    assert [event for event in account.events if event[0] == "v1_create"][0][2] == "bottom"
    assert [name for name, _ in account.contents()] == expected
    assert account.counts["move_top"] == 11
    assert account.v1_bodies[0].get("layoutMode") == "h1"
    return run


def test_t02_nonbullet_layout_never_reaches_layer_c(tmp_path, monkeypatch):
    witness_t02_nonbullet_layout_stays_v1(tmp_path, monkeypatch)


def witness_t02_no_grant_stays_v1(tmp_path, monkeypatch):
    geometry = small_interior()
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, grant=False)
    run.assertion_prelude()
    account = run.account
    assert account.counts["llm_ops"] == 0
    assert account.counts["v1_create"] == 1
    assert account.lists[FILE] == 0
    assert account.counts["move_top"] == 4
    assert [name for name, _ in account.contents()] == geometry.expected_names
    return run


def test_t02_no_grant_interior_insert_stays_bare_v1(tmp_path, monkeypatch):
    witness_t02_no_grant_stays_v1(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# T04 · genuine reorder unchanged, renumber/shuffled wire order adopted
# ---------------------------------------------------------------------------


def witness_t04_genuine_reorder(tmp_path, monkeypatch):
    handles = [_identifier(EXISTING_PREFIX, index) for index in range(3)]
    rows = [_row(handles[0], "alpha", 100, FILE),
            _row(handles[1], "beta", 200, FILE),
            _row(handles[2], "gamma", 300, FILE)]
    # Stored order is alpha,beta,gamma; the source asks for beta,alpha,gamma.
    source = [_node("beta", handle=handles[1]), _node("alpha", handle=handles[0]),
              _node("gamma", handle=handles[2])]
    geometry = Geometry(source, rows, ["beta", "alpha", "gamma"], [])
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry)
    run.assertion_prelude()
    account = run.account
    assert account.counts["v1_create"] == 0 and account.counts["llm_ops"] == 0
    moves = [event for event in account.events if event[0] == "move"]
    # Today's REORDER: every desired child to top in reverse desired order.
    assert [event[1] for event in moves] == [handles[2], handles[0], handles[1]], moves
    assert all(event[2] == "top" for event in moves)
    assert account.order(FILE) == [handles[1], handles[0], handles[2]]
    return run


def test_t04_genuine_reorder_keeps_todays_reverse_desired_top_moves(tmp_path, monkeypatch):
    witness_t04_genuine_reorder(tmp_path, monkeypatch)


def witness_t04_renumbered_shuffled_wire_order(tmp_path, monkeypatch):
    geometry = small_interior()
    account = FakeAccount()
    account.wire_reverse = True     # the mandatory listing is not wire-sorted
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account)
    run.assertion_prelude()
    assert account.rows[FILE][1]["name"] == "added_00"
    assert [name for name, _ in account.contents()] == geometry.expected_names
    assert account.counts["llm_ops"] == 1
    assert account.counts["move_top"] == 0, "the wire order was trusted instead of the priority order"
    assert read_external_dirt(run.client._coherence_paths(), required=False).active is False
    _assert_no_cache_rank_write(run)
    return run


def test_t04_renumbered_shuffled_wire_listing_adopts_verified_order(tmp_path, monkeypatch):
    witness_t04_renumbered_shuffled_wire_order(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# T05 · CF-117 sequential shape, conclusion before MOVE
# ---------------------------------------------------------------------------


def witness_t05_sequential_parent_shape(tmp_path, monkeypatch):
    handles = [_identifier(EXISTING_PREFIX, index) for index in range(6)]
    a, b, c, d, e, f = handles
    a_child, a_gate, a_last = (_identifier(EXISTING_PREFIX, index) for index in (20, 21, 22))
    moved_in = _identifier(EXISTING_PREFIX, 30)
    rows = [_row(a, "a", 100, FILE), _row(b, "b", 200, FILE), _row(c, "c", 300, FILE),
            _row(d, "d", 400, FILE), _row(e, "e", 500, FILE), _row(f, "f", 600, FILE),
            _row(a_child, "a_child", 100, a), _row(a_gate, "a_gate", 200, a),
             _row(a_last, "a_last", 300, a), _row(moved_in, "moved_in", 100, c)]
    source = [
        _node("a", handle=a, children=[
            _node("a_child", handle=a_child),
            _node("a_added_one"),
            _node("a_gate", handle=a_gate),
            _node("a_added_two"),
            _node("a_last", handle=a_last),
        ]),
        _node("inserted_one"),
        _node("b", handle=b),
        _node("c", handle=c),
        _node("d", handle=d),
        _node("e", handle=e),
        _node("f", handle=f),
        _node("moved_in", handle=moved_in),
        _node("warning", position_hint="top"),
        _node("fresh_parent", children=[_node("fresh_child")]),
    ]
    expected = ["a", "inserted_one", "b", "c", "d", "e", "f", "moved_in",
                "warning", "fresh_parent"]
    geometry = Geometry(source, rows, expected, [1, 8, 9])
    account = FakeAccount()
    account.seed(c, [_row(moved_in, "moved_in", 100, c)])
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account,
                      spy_conclusions=True)
    run.assertion_prelude()
    # One uninterrupted transaction per healthy parent: one current-state
    # baseline plus one mandatory post-listing per create -- never a second
    # baseline for the same segment.
    assert dict(account.lists) == {FILE: 1, a: 3}, dict(account.lists)
    assert [parent for parent, _, _ in run.conclusions] == [FILE, a], run.conclusions
    assert all(relist is False for _, relist, _ in run.conclusions)
    # CF-117: every conclusion happened before the first MOVE event, and the
    # parent's conclusion happened before the bare same-parent create.
    bare_warning = next(index for index, event in enumerate(account.events)
                        if event[0] == "v1_create" and event[1] == "warning")
    first_move = next(index for index, event in enumerate(account.events)
                      if event[0] == "move")
    assert all(mark <= first_move for _, _, mark in run.conclusions), (
        run.conclusions, account.events[first_move])
    assert run.conclusions[0][2] <= bare_warning, (run.conclusions[0], account.events[bare_warning])
    # Activity shape: three LLM inserts (the FILE interior and the two nested
    # adds under `a`), three bare v1 creates (warning, fresh_parent,
    # fresh_child) and one structural move.
    assert account.counts["llm_ops"] == 3, account.counts
    assert account.counts["v1_create"] == 3, account.counts
    assert account.counts["move_bottom"] == 1, account.counts
    assert account.counts["move_top"] == len(expected), account.counts
    # The real wrapper honoured the warning hint on its bare boundary create.
    assert ("v1_create", "warning", "top") in account.events
    assert [name for name, _ in account.contents()] == expected
    assert [name for name, _ in account.contents(a)] == [
        "a_child", "a_added_one", "a_gate", "a_added_two", "a_last"]
    return run


def test_t05_conclusion_precedes_bare_create_and_move(tmp_path, monkeypatch):
    witness_t05_sequential_parent_shape(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# T08 · A1 endpoint provenance and the no-resend rule
# ---------------------------------------------------------------------------


def test_t08_endpoint_429_is_tagged_and_listing_rate_error_is_not(tmp_path, monkeypatch):
    account = FakeAccount()
    geometry = small_interior()
    account.seed(FILE, geometry.rows)

    async def main():
        client, _, _, _, _, _ = _build_client(tmp_path, account)
        async with client._client:
            stage = SendStage()
            account.llm_script = [429]
            with pytest.raises(RateLimitError) as endpoint:
                await LlmInsertAfterTransport(client).insert_after(
                    FILE, geometry.rows[0]["id"],
                    [NodeCreateRequest(parent_id=FILE, name="ours")], send_stage=stage)
            assert endpoint.value.llm_endpoint_429 is True
            assert endpoint.value.llm_dispatch_started is True
            assert endpoint.value.retry_after == 3.0
            account.get_all_429 = True
            with pytest.raises(RateLimitError) as listing:
                await client.list_nodes(NodeListRequest(parentId=FILE), max_retries=1)
            assert getattr(listing.value, "llm_endpoint_429", False) is False

    asyncio.run(main())


def witness_t08_a1_single_endpoint_429(tmp_path, monkeypatch, *, prime=False):
    geometry = small_interior()
    account = FakeAccount()
    account.seed(FILE, geometry.rows)
    account.llm_script = [429, "ok"]
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account,
                      prime_available=prime)
    run.assertion_prelude()
    assert run.result["success"] is True
    # A1: exactly one re-send of the known-unsent run, then a healthy commit.
    assert account.counts["llm_ops"] == 2, account.counts
    assert account.counts["v1_create"] == 0
    assert account.counts["move_top"] == 0
    assert account.lists[FILE] == 1
    assert [name for name, _ in account.contents()] == geometry.expected_names
    # The re-send waited for the governor floor the endpoint's Retry-After set.
    assert any(seconds >= 3.0 - 1e-9 for seconds, _ in run.sleeps), run.sleeps
    return run


def test_t08_a1_endpoint_429_resends_once_then_healthy(tmp_path, monkeypatch):
    run = witness_t08_a1_single_endpoint_429(tmp_path, monkeypatch, prime=True)
    assert run.state == InsertCapabilityState.AVAILABLE


def test_t08_a1_endpoint_429_probe_is_replaced_then_healthy(tmp_path, monkeypatch):
    run = witness_t08_a1_single_endpoint_429(tmp_path, monkeypatch, prime=False)
    assert run.state == InsertCapabilityState.AVAILABLE


def witness_t08_two_endpoint_429s(tmp_path, monkeypatch):
    geometry = small_interior()
    account = FakeAccount()
    account.seed(FILE, geometry.rows)
    account.llm_script = [429, 429]
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account)
    run.assertion_prelude()
    assert account.counts["llm_ops"] == 2
    assert account.counts["v1_create"] == 1
    assert [event for event in account.events if event[0] == "v1_create"][0][2] == "bottom"
    assert account.counts["move_top"] == 4
    assert [name for name, _ in account.contents()] == geometry.expected_names
    return run


def test_t08_two_endpoint_429s_continue_on_v1(tmp_path, monkeypatch):
    witness_t08_two_endpoint_429s(tmp_path, monkeypatch)


def witness_t08_no_resend_on_listing_rate(tmp_path, monkeypatch):
    geometry = small_interior()
    account = FakeAccount()
    account.seed(FILE, geometry.rows)
    account.get_all_429 = True
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account,
                      dirty_before=(FILE,))
    # A baseline-listing rate result is pre-dispatch and carries no endpoint
    # provenance: no re-send, no v1 continuation; the lane refusal propagates.
    assert run.result is None
    assert isinstance(run.error, NetworkError)
    assert isinstance(run.error.__cause__, RateLimitError)
    assert getattr(run.error.__cause__, "llm_endpoint_429", False) is False
    assert account.counts["llm_ops"] == 0, "a pre-dispatch listing rate error was read as known-unsent"
    assert account.counts["v1_create"] == 0
    assert account.lists[FILE] == 10, account.lists  # the real bounded retry policy
    return run


def test_t08_unproven_rate_error_never_resends(tmp_path, monkeypatch):
    witness_t08_no_resend_on_listing_rate(tmp_path, monkeypatch)


def witness_t08_post_listing_rate_is_ambiguity(tmp_path, monkeypatch):
    geometry = small_interior()
    account = FakeAccount()
    account.seed(FILE, geometry.rows)
    account.get_all_429 = True
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account)
    # N03 keeps its own classification: an unreliable listing after a possibly
    # committed insert is ambiguous. No re-send and no v1 replacement.
    assert run.result is None
    assert isinstance(run.error, NetworkError)
    assert isinstance(run.error.__cause__, CoherenceBarrierActiveError)
    assert account.counts["llm_ops"] == 1
    assert account.counts["v1_create"] == 0
    assert (asyncio.run(hard_stop_state(run.client._coherence_paths()))).hard_dirty
    return run


def test_t08_post_listing_rate_error_raises_without_resend(tmp_path, monkeypatch):
    witness_t08_post_listing_rate_is_ambiguity(tmp_path, monkeypatch)


@pytest.mark.parametrize("mode", [401, 403, 404, 405, "read_required", "op_error", "schema", "absent_timeout"])
def test_t08_real_wrapper_absence_and_endpoint_refusal_complete_on_v1(tmp_path, monkeypatch, mode):
    geometry, account = small_interior(), FakeAccount()
    account.llm_script = [mode]
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account)
    run.assertion_prelude()
    assert run.result["success"] is True
    assert account.counts["llm_ops"] == 1
    assert account.counts["v1_create"] == 1
    assert account.lists[FILE] == 2  # failed LLM verification, then verified v1
    assert account.counts["move_top"] == 4
    assert [name for name, _ in account.contents()] == geometry.expected_names
    assert run.recovery == []


@pytest.mark.parametrize("mode", ["timeout", "malformed"])
def test_t08_unknown_or_unusable_ack_committed_create_is_never_replaced(tmp_path, monkeypatch, mode):
    geometry, account = small_interior(), FakeAccount()
    account.llm_script = [mode]
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account)
    run.assertion_prelude()
    assert account.counts["llm_ops"] == 1 and account.counts["v1_create"] == 0
    assert account.lists[FILE] == 1 and account.counts["move_top"] == 0
    assert [name for name, _ in account.contents()] == geometry.expected_names


def test_t08_ambiguity_stops_before_move_or_replacement_and_requests_healing(tmp_path, monkeypatch):
    geometry, account = small_interior(), FakeAccount()
    account.llm_script = ["duplicate"]
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account)
    assert run.result is None and isinstance(run.error.__cause__, CoherenceBarrierActiveError)
    assert account.counts["llm_ops"] == 1 and account.counts["v1_create"] == 0
    assert account.counts["move_top"] == account.counts["move_bottom"] == 0
    assert run.recovery == ["scheduled"]


@pytest.mark.parametrize("mode", ["degraded", "occupied"])
def test_t02_unavailable_controller_takes_immediate_bare_v1(tmp_path, monkeypatch, mode):
    geometry = small_interior()
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, controller_mode=mode)
    run.assertion_prelude()
    assert run.account.counts["llm_ops"] == 0 and run.account.lists[FILE] == 0
    assert run.account.counts["v1_create"] == 1 and run.account.counts["move_top"] == 4
    assert [name for name, _ in run.account.contents()] == geometry.expected_names


@pytest.mark.parametrize("factory,moves", [(app_js_scattered, 111), (server_py_group, 260)])
@pytest.mark.parametrize("mode", ["refused", "two_rates"])
def test_t08_concluding_c_keeps_both_d11_fallback_move_counts(tmp_path, monkeypatch, factory, moves, mode):
    geometry, account = factory(), FakeAccount()
    account.llm_script = [404] if mode == "refused" else [429, 429]
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account)
    run.assertion_prelude()
    assert run.result["success"] is True
    assert account.counts["move_top"] == moves
    assert account.counts["move_bottom"] == 0
    assert [name for name, _ in account.contents()] == geometry.expected_names
    assert run.recovery == []


@pytest.mark.parametrize("grant", [False, True])
def test_t02_existing_empty_parent_is_sequential_bare_v1_without_listings(tmp_path, monkeypatch, grant):
    geometry = Geometry([_node("first"), _node("second")], [], ["first", "second"], [0, 1])
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, grant=grant)
    run.assertion_prelude()
    assert run.account.counts["v1_create"] == 2
    assert run.account.counts["llm_ops"] == run.account.counts["move_top"] == run.account.lists[FILE] == 0
    assert [name for name, _ in run.account.contents()] == geometry.expected_names


def test_t02_markdown_heading_with_unset_layout_is_eligible(tmp_path, monkeypatch):
    geometry = small_interior()
    index = geometry.new_indices[0]
    geometry.source_nodes[index].update(name="New Markdown heading", note="MD_PATH: Section / New Markdown heading")
    geometry.expected_names[index] = "New Markdown heading"
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry)
    run.assertion_prelude()
    assert run.account.counts["llm_ops"] == 1
    assert run.account.counts["v1_create"] == run.account.counts["move_top"] == 0


def witness_t08_governor_rate_is_not_endpoint_provenance(tmp_path, monkeypatch, *, where="post_response"):
    geometry, account = small_interior(), FakeAccount()
    account.llm_script = [429]
    original, rate_calls = RateLimitError(3), []
    async def refused(self, *args, **kwargs):
        rate_calls.append(where)
        raise original
    method = "defer_global_writes_until" if where == "post_response" else "recheck_before_send"
    monkeypatch.setattr(CartoWriteGovernor, method, refused)
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account)
    assert run.result is None
    assert run.error.__cause__ is original
    assert not getattr(original, "llm_endpoint_429", False)
    assert rate_calls == [where]
    assert account.counts["llm_ops"] == int(where == "post_response")
    assert account.counts["v1_create"] == account.lists[FILE] == 0
    return run


@pytest.mark.parametrize("where", ["pre_dispatch", "post_response"])
def test_t08_governor_rate_error_is_never_an_a1_resend(tmp_path, monkeypatch, where):
    witness_t08_governor_rate_is_not_endpoint_provenance(tmp_path, monkeypatch, where=where)


# ---------------------------------------------------------------------------
# T10 · CF-118 decoding verification and CF-122 unrelated dirt
# ---------------------------------------------------------------------------


def witness_t10_decoding_error_after_commit(tmp_path, monkeypatch):
    geometry = small_interior()
    account = FakeAccount()
    account.seed(FILE, geometry.rows)
    account.llm_script = ["decode"]
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, account=account)
    run.assertion_prelude()
    # The reply was undecodable but the insert committed: the mandatory listing
    # returns the verified identity and the run stays healthy.
    assert run.result["success"] is True
    assert account.counts["llm_ops"] == 1
    assert account.counts["v1_create"] == 0
    assert account.lists[FILE] == 1
    assert account.counts["move_top"] == 0
    assert [name for name, _ in account.contents()] == geometry.expected_names
    assert [geometry.source_nodes[1].get("id")] == account.created
    return run


def test_t10_cf118_decoding_error_after_commit_takes_mandatory_listing(tmp_path, monkeypatch):
    witness_t10_decoding_error_after_commit(tmp_path, monkeypatch)


def witness_t10_cf122_unrelated_dirt(tmp_path, monkeypatch):
    geometry = small_interior()
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, dirty_before=(OTHER,),
                      prime_available=True)
    run.assertion_prelude()
    assert run.account.lists[FILE] == 1, "unrelated dirt bought a baseline listing"
    assert run.account.counts["move_top"] == 0
    return run


def test_t10_cf122_unrelated_dirt_does_not_buy_a_baseline_listing(tmp_path, monkeypatch):
    witness_t10_cf122_unrelated_dirt(tmp_path, monkeypatch)


def test_t10_cf122_parent_dirt_still_pays_the_current_baseline(tmp_path, monkeypatch):
    # Control: the retained current-view / parent / sentinel dirt check keeps its
    # cost when the parent itself is dirty.
    geometry = small_interior()
    run = run_refresh(tmp_path, monkeypatch, geometry=geometry, dirty_before=(FILE,),
                      prime_available=True)
    run.assertion_prelude()
    assert run.account.lists[FILE] == 2  # one current-state baseline + one post-listing
    assert run.account.counts["move_top"] == 0


# ---------------------------------------------------------------------------
# Named source-copy / behavioural counterfactuals (test process only)
# ---------------------------------------------------------------------------


def _source_copy(module, owner, name, changes, extra_globals=None):
    source = textwrap.dedent(inspect.getsource(getattr(owner, name)))
    for old, new in changes:
        assert source.count(old) == 1, (
            f"N04 G1 mutant seam moved in {name}: {old!r} is not present exactly once "
            "-- re-point the counterfactual, never weaken it")
        source = source.replace(old, new, 1)
    class LiveModuleGlobals(dict):
        # A copied body must see preparation patches installed AFTER the copy,
        # just as the real function does. A frozen globals snapshot can turn a
        # missing fixture path into a false feature RED.
        def __missing__(self, key):
            try:
                return getattr(module, key)
            except AttributeError:
                raise KeyError(key) from None
    namespace = LiveModuleGlobals({key: vars(module)[key] for key in
                                  ("__name__", "__package__", "__spec__") if key in vars(module)})
    namespace.update(extra_globals or {})
    exec(compile(source, f"<n04-g1-mutant:{name}>", "exec"), namespace)
    return namespace[name]


def _rewrite_create_requests(monkeypatch, rewrite):
    """Copy the real generator and alter only its plain CREATE payload seam."""
    copied = _source_copy(cpu, cpu, "_reconcile_steps", [(
        "ReconcileRequest('create', (run,), {})",
        "ReconcileRequest('create', (_n04_rewrite(run),), {})",
    )], extra_globals={"_n04_rewrite": rewrite})
    monkeypatch.setattr(driver, "_reconcile_steps", copied)


def _force_bottom(run):
    return dataclasses.replace(run, placement="bottom", predecessor_uuid=None)


def _strip_position_hint(run):
    items = tuple({**item, "_position_hint": None} for item in run.items)
    return dataclasses.replace(run, items=items, placement="bottom", predecessor_uuid=None)


def _make_layer_c_eligible(run):
    items = tuple({**item, "data": {**(item.get("data") or {}), "layoutMode": None}}
                  for item in run.items)
    return dataclasses.replace(run, items=items, placement="after",
                               predecessor_uuid=_ten_existing()[4]["id"])


def _taint_every_rate_error(monkeypatch):
    """Source-copy the caller without its endpoint-origin and stage guard."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(acn.WorkFlowyClientNexus.refresh_file_node_beacons)))
    guards = [node for node in ast.walk(tree) if isinstance(node, ast.If)
              and {"llm_endpoint_429", "llm_dispatch_started"} <= {
                  item.value for item in ast.walk(node.test) if isinstance(item, ast.Constant)
                  and isinstance(item.value, str)}]
    if len(guards) != 1:
        raise RuntimeError("A1 caller guard mutant seam moved")
    guards[0].test = ast.Constant(False)
    namespace = dict(vars(acn))
    exec(compile(ast.fix_missing_locations(tree), "<n04-unguarded-rate-caller>", "exec"), namespace)
    monkeypatch.setattr(acn.WorkFlowyClientNexus, "refresh_file_node_beacons", namespace["refresh_file_node_beacons"])


def _trust_wire_order(monkeypatch):
    monkeypatch.setattr(tc, "_order", _source_copy(tc, tc, "_order", [(
        'owned.sort(key=lambda row: row["priority"])', 'pass',
    )]))


N04_MUTANTS = {
    "force-bottom-placement": lambda mp: _rewrite_create_requests(mp, _force_bottom),
    "discard-verified-order": lambda mp: mp.setattr(
        cpu, "_adopt_create_order", _source_copy(cpu, cpu, "_adopt_create_order", [(
            "if result.ordered_child_ids:", "if False:",
        )])),
    "trust-wire-order": _trust_wire_order,
    "ignore-position-hint": lambda mp: _rewrite_create_requests(mp, _strip_position_hint),
    "bypass-nonbullet-cheap-path": lambda mp: _rewrite_create_requests(mp, _make_layer_c_eligible),
    "taint-rate-provenance": _taint_every_rate_error,
    "skip-parent-conclusion": lambda mp: mp.setattr(
        tc.TransitionCreateTransaction, "conclude", _source_copy(tc, tc.TransitionCreateTransaction, "conclude", [(
            "self._latest = None", "pass",
        )])),
    "conclude-after-bare": lambda mp: mp.setattr(
        acn.WorkFlowyClientNexus, "refresh_file_node_beacons",
        _source_copy(acn, acn.WorkFlowyClientNexus, "refresh_file_node_beacons", [(
            "                await conclude_create_parent(parent_uuid)\n                if not identifiers:",
            "                pass\n                if not identifiers:",
        )])),
    "conclude-after-move": lambda mp: mp.setattr(
        driver, "reconcile_tree", _source_copy(driver, driver, "reconcile_tree", [(
            "response = (await conclude_create_parent(*request.args, **request.kwargs)\n"
            "                                    if conclude_create_parent is not None else None)",
            "response = None",
        )])),
    "drop-endpoint-429-tag": lambda mp: mp.setattr(
        transport_module.LlmInsertAfterTransport, "insert_after",
        _source_copy(transport_module, transport_module.LlmInsertAfterTransport,
                     "insert_after", [("error.llm_endpoint_429 = True",
                                       "error.llm_endpoint_429 = False")])),
    "omit-decoding-verification": lambda mp: mp.setattr(
        tc.TransitionCreateTransaction, "_attempt",
        _source_copy(tc, tc.TransitionCreateTransaction, "_attempt", [(
            "            except (httpx.TransportError, httpx.DecodingError, NetworkError, WorkflowyTimeoutError,\n",
            "            except (httpx.TransportError, NetworkError, WorkflowyTimeoutError,\n",
        ), (
            '                if transport == "llm" and not isinstance(exc, (httpx.TransportError, httpx.DecodingError, NetworkError, WorkflowyTimeoutError)):\n',
            '                if transport == "llm" and not isinstance(exc, (httpx.TransportError, NetworkError, WorkflowyTimeoutError)):\n',
        )])),
    "restore-account-wide-dirt-veto": lambda mp: mp.setattr(
        tc.TransitionCreateTransaction, "_baseline",
        _source_copy(tc, tc.TransitionCreateTransaction, "_baseline", [(
            '        if (not getattr(status, "external_dirty", False)\n'
            '                and parent_clean_now and self.parent_id in snapshot.nodes_by_id):\n',
            '        if (not snapshot.dirty_ids and not getattr(status, "external_dirty", False)\n'
            '                and parent_clean_now and self.parent_id in snapshot.nodes_by_id):\n',
        )])),
}


def install_n04_mutant(monkeypatch, name):
    N04_MUTANTS[name](monkeypatch)


@pytest.fixture(autouse=True)
def named_mutant(monkeypatch):
    """A targeted RED receipt selects one mutant for the whole module."""
    name = os.getenv("N04_G1_MUTANT")
    if name:
        install_n04_mutant(monkeypatch, name)


# Each witness must survive its own named counterfactual only as an ASSERTION
# failure -- never as a fixture/setup error.
_RED_TABLE = [
    ("force-bottom-placement", witness_t01_app_js_scattered_healthy, {"cold": False, "prime": True}),
    ("discard-verified-order", witness_t01_app_js_scattered_healthy, {"cold": False, "prime": True}),
    ("discard-verified-order", witness_t01_server_py_group_healthy, {}),
    ("trust-wire-order", witness_t04_renumbered_shuffled_wire_order, {}),
    ("ignore-position-hint", witness_t02_warning_hint_is_honoured, {}),
    ("bypass-nonbullet-cheap-path", witness_t02_nonbullet_layout_stays_v1, {}),
    ("skip-parent-conclusion", witness_t05_sequential_parent_shape, {}),
    ("conclude-after-bare", witness_t05_sequential_parent_shape, {}),
    ("conclude-after-move", witness_t05_sequential_parent_shape, {}),
    ("drop-endpoint-429-tag", witness_t08_a1_single_endpoint_429, {"prime": True}),
    ("taint-rate-provenance", witness_t08_no_resend_on_listing_rate, {}),
    ("taint-rate-provenance", witness_t08_governor_rate_is_not_endpoint_provenance, {"where": "post_response"}),
    ("omit-decoding-verification", witness_t10_decoding_error_after_commit, {}),
    ("restore-account-wide-dirt-veto", witness_t10_cf122_unrelated_dirt, {}),
]


@pytest.mark.parametrize(
    "mutant,witness,kwargs", _RED_TABLE,
    ids=[f"{mutant}::{witness.__name__}" for mutant, witness, _ in _RED_TABLE])
def test_n04_named_mutants_turn_their_witnesses_red(tmp_path_factory, mutant, witness, kwargs):
    """Assertion RED under the named mutant; the plain witness above is GREEN."""
    if os.getenv("N04_G1_MUTANT"):
        pytest.skip("targeted N04_G1_MUTANT receipt run")
    path = tmp_path_factory.mktemp("mutant-" + mutant)
    with pytest.MonkeyPatch.context() as monkeypatch:
        install_n04_mutant(monkeypatch, mutant)
        with pytest.raises(AssertionError):
            witness(path, monkeypatch, **kwargs)
    # A fixture/setup defect must never masquerade as RED: the same witness
    # must succeed after restoration, on a fresh offline account.
    with pytest.MonkeyPatch.context() as restored:
        # Exercise the copier on restoration too, not only the original body.
        restored.setattr(acn.WorkFlowyClientNexus, "refresh_file_node_beacons",
            _source_copy(acn, acn.WorkFlowyClientNexus, "refresh_file_node_beacons", []))
        witness(tmp_path_factory.mktemp("restored-" + mutant), restored, **kwargs)
