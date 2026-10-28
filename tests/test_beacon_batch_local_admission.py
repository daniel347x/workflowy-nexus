"""U06 B: real temporary bytes, production client seams, injected existing Q3.

Baseline witnesses exercise the former vetoes, not a replacement implementation.
"""
import asyncio
import hashlib
import inspect
import os
import textwrap
import threading
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

import test_lightning_rod_mutation_modes as modes
from workflowy_mcp import server as srv
from workflowy_mcp import beacon_integrity as integrity
from workflowy_mcp.client import api_client_nexus as acn
from workflowy_mcp.beacon_integrity import check_beacon_file
from workflowy_mcp.mcp_api_registry import dispatch_operation


def _block(bid, name, kind="ast", comment=None):
    lines = ["# @beacon[", f"#   id={bid},", "#   slice_labels=old,keep,", f"#   kind={kind},"]
    if comment is not None:
        lines.append(f"#   comment={comment},")
    return lines + ["# ]", f"def {name}():", "    return 1", ""]


class _Client(modes._PipelineClient):
    """Use actual client evidence and non-abandoned commit, not a fake ledger."""
    async def preflight_beacon_write_from_node(self, node_id, operation, mode):
        self.preflight_calls.append(node_id)
        return await acn.WorkFlowyClientNexus.preflight_beacon_write_from_node(self, node_id, operation, mode)

    async def plan_beacon_write_from_node(self, node_id, operation, mode):
        self.plan_calls.append(node_id)
        return await acn.WorkFlowyClientNexus.plan_beacon_write_from_node(self, node_id, operation, mode)

    async def commit_beacon_write_plan(self, *, plan, note):
        self.commit_calls.append(plan.node_id)
        return await acn.WorkFlowyClientNexus.commit_beacon_write_plan(self, plan=plan, note=note)


def _install_retired_scope_unpack(monkeypatch):
    """CF-46 mutant: restore the retired tuple-unpack inside ``BeaconAdmission.admit``.

    The injected account-scope callback stays scalar, exactly as production's
    ``_carto_account_scope`` now is; only the consumer is mutated back to the
    retired ``(scope, env)`` unpack, compiled in memory from the live method.
    A real successful batch acquisition must then fail its own success oracle
    (the admission refusal), never an unrelated import/shim error.
    """
    if not os.environ.get("CUT2B_NEUTRALIZE_SCOPE_ADAPTER"):
        return False
    from workflowy_mcp import beacon_fix
    source = textwrap.dedent(inspect.getsource(beacon_fix.BeaconAdmission.admit))
    scalar = "scope = self.account_scope()"
    assert scalar in source, "BeaconAdmission.admit no longer has the CF-46 scalar scope line"
    namespace = dict(vars(beacon_fix))
    exec(compile(source.replace(scalar, "scope, _env = self.account_scope()", 1),
                 beacon_fix.__file__, "exec"), namespace)
    monkeypatch.setattr(beacon_fix.BeaconAdmission, "admit", namespace["admit"])
    return True


@pytest.fixture
def setup_batch(tmp_path, monkeypatch):
    async def export(client, **kwargs):
        return {"nodes": client.nodes}
    async def suppress():
        return []
    monkeypatch.setattr(acn, "export_nodes_impl", export)
    monkeypatch.setattr(srv, "_beacon_symbol_suppress_tags", suppress)
    monkeypatch.setattr(srv, "_carto_account_scope", lambda: "test-scope")
    _install_retired_scope_unpack(monkeypatch)
    state = SimpleNamespace(acquired=[], released=[], held=set(), busy=None, bad_footprint=None)
    original_footprint = srv.compute_footprint

    def footprint(path, uid, cache, *, kind):
        if state.bad_footprint and str(path) == str(state.bad_footprint):
            from workflowy_mcp.carto_job_governance import FootprintUnresolvableError
            raise FootprintUnresolvableError("fixture footprint unavailable")
        return original_footprint(path, uid, cache, kind=kind)

    async def acquire(targets, **kwargs):
        paths = [t["path"] for t in targets]
        state.acquired.append(paths)
        if state.busy and str(state.busy) in paths:
            raise RuntimeError("fixture existing Q3 conflict")
        assert not state.held
        state.held.update(paths)
        return [{"path": p} for p in paths]

    async def release(leases, *, primary_error=None):
        assert state.held == {token["path"] for token in leases}
        state.released.append(primary_error)
        state.held.clear()

    monkeypatch.setattr(srv, "compute_footprint", footprint)
    monkeypatch.setattr(srv, "acquire_q3_leases_async", acquire)
    monkeypatch.setattr(srv, "_release_q3_leases_preserving_primary", release)

    def build(names=("a", "b", "safe"), second=False):
        path = tmp_path / "one.py"
        path.write_bytes(("\n".join(sum((_block(f"b-{name}", name, comment="disk") for name in names), [])) + "\n").encode())
        ids = {name: str(uuid5(NAMESPACE_URL, str(path) + name)) for name in names}
        nodes = modes._pipeline_nodes(path, [(uid, name, f"AST_QUALNAME: {name}\n") for name, uid in ids.items()])
        paths = [path]
        if second:
            other = tmp_path / "two.py"
            other.write_bytes(("\n".join(_block("b-last", "last")) + "\n").encode())
            file_id, uid = str(uuid5(NAMESPACE_URL, str(other))), str(uuid5(NAMESPACE_URL, str(other) + "last"))
            nodes += [{"id": file_id, "parent_id": modes._ROOT, "note": f"Path: {other}", "name": other.name},
                      {"id": uid, "parent_id": file_id, "note": "AST_QUALNAME: last\n", "name": "last"}]
            ids["last"] = uid
            paths.append(other)
        client = _Client(nodes=nodes, candidates=ids)
        monkeypatch.setattr(srv, "get_client", lambda: client)
        return client, ids, paths
    return build, state


def _run(ops, *, atomic=False, mode="ensure_beacons"):
    return asyncio.run(srv._lightning_rod_mutate_beacons(spec=modes._spec_for(mode, operations=ops, atomic=atomic)))


@pytest.mark.parametrize("second", [False, True])
def test_batch_observes_each_final_file_once_not_each_plan(setup_batch, monkeypatch, second):
    build, _ = setup_batch
    names = [f"f{i}" for i in range(5)]
    _client, _ids, paths = build(names, second=second)
    calls = []
    observer = integrity.observe_beacon_snapshot
    def record(snapshot, **kwargs):
        calls.append((snapshot.canonical_path, snapshot.sha256))
        return observer(snapshot, **kwargs)
    monkeypatch.setattr(integrity, "observe_beacon_snapshot", record)
    result = _run([{"symbol": name, "tags": ["new"]} for name in names + (["last"] if second else [])])
    assert all(r["status"] == "committed" for r in result["results"])
    assert len(calls) == len(paths)
    assert dict(calls) == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def test_batch_replaced_empty_comment_is_absent_from_final_findings(setup_batch):
    build, _ = setup_batch
    _client, _ids, (path,) = build()
    before = path.read_bytes().replace(b"#   comment=disk,\n", b"", 1)
    before = before.replace(b"#   id=b-safe,", b"#   id=b-safe,\n#   extra=opaque,")
    path.write_bytes(before)
    assert [f["code"] for f in check_beacon_file(str(path)) if f["beacon_id"] == "b-a"] == ["empty_comment"]
    result = _run([{"symbol": "a", "tags": ["new"], "comment": "Chosen.", "comment_mode": "replace"}])
    assert result["results"][0]["status"] == "committed"
    assert not any(f["code"] == "empty_comment" and f["beacon_id"] == "b-a" for f in result["findings"])
    assert result["findings"] == check_beacon_file(str(path))
    after_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["findings"] and all(f["observed"]["file_sha256"] == after_sha for f in result["findings"])
    assert result["results"][0]["findings"] == []


def test_batch_reports_empty_comment_created_by_new_block(setup_batch, tmp_path, monkeypatch):
    path = tmp_path / "created.py"
    path.write_bytes(b"def h():\n    return 2\n")
    uid = str(uuid5(NAMESPACE_URL, str(path) + "h"))
    client = _Client(nodes=modes._pipeline_nodes(path, [(uid, "h", "AST_QUALNAME: h\n")]), candidates={"h": uid})
    monkeypatch.setattr(srv, "get_client", lambda: client)
    result = _run([{"symbol": "h", "tags": ["x"]}])
    row = result["results"][0]
    assert row["status"] == "committed" and row["beacon_id"]
    assert result["findings"] == check_beacon_file(str(path))
    assert any(f["code"] == "empty_comment" and f["beacon_id"] == row["beacon_id"] for f in result["findings"])
    assert all(f["observed"]["file_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest() for f in result["findings"])
    assert row["findings"] == result["findings"] and row["finding"]["code"] == "empty_comment"


def test_batch_final_analysis_exception_is_data_not_a_write_veto(setup_batch, monkeypatch):
    build, _ = setup_batch
    _client, _ids, (path,) = build()
    before = path.read_bytes()
    observer = integrity.observe_beacon_snapshot
    def fail_after_write(snapshot, **kwargs):
        if snapshot.sha256 != hashlib.sha256(before).hexdigest():
            raise RuntimeError("final analysis failed")
        return observer(snapshot, **kwargs)
    monkeypatch.setattr(integrity, "observe_beacon_snapshot", fail_after_write)
    result = _run([{"symbol": n, "tags": ["new"]} for n in ("a", "safe")])
    assert all(r["status"] == "committed" for r in result["results"]) and result["success"]
    assert all(r["analysis_error"]["message"] == "final analysis failed" for r in result["results"])
    assert result["findings"] == []
    assert path.read_bytes() != before


def test_fifty_items_one_invalid_tag_keeps_49_exact_deltas(setup_batch):
    build, state = setup_batch
    names = [f"fn_{i}" for i in range(50)]
    client, ids, (path,) = build(names)
    before = path.read_bytes()
    ops = [{"symbol": name, "tags": ["bad tag" if i == 25 else "new"]} for i, name in enumerate(names)]
    result = _run(ops)
    assert [r["index"] for r in result["results"]] == list(range(50))
    assert result["results"][25]["error_kind"] == "item_invalid"
    assert result["summary"]["committed"] == 49 and result["partial_success"]
    expected = before
    for name in names:
        if name != names[25]:
            old = ("\n".join(_block(f"b-{name}", name, comment="disk"))).encode()
            expected = expected.replace(old, old.replace(b"slice_labels=old,keep,", b"slice_labels=old,keep,new,"))
    assert path.read_bytes() == expected
    assert ids[names[25]] not in client.commit_calls
    assert len(state.acquired) == len(state.released) == 1


def test_invalid_comment_preserves_disk_and_applies_labels(setup_batch):
    build, _ = setup_batch
    _client, _ids, (path,) = build()
    before = path.read_bytes()
    result = _run([{"symbol": n, "tags": ["new"], **({"comment": "bad --> comment"} if n == "b" else {})}
                   for n in ("a", "b", "safe")])
    row = result["results"][1]
    assert row["status"] == "committed" and row["disk_changed"]
    assert row["field_outcomes"][0]["outcome"] == "preserved_disk"
    assert row["field_outcomes"][0]["error_kind"] == "invalid_comment"
    assert not result["success"] and result["partial_success"]
    assert path.read_bytes() == before.replace(b"slice_labels=old,keep,", b"slice_labels=old,keep,new,")


@pytest.mark.parametrize("failure", ["busy", "footprint"])
def test_file_local_admission_keeps_busy_bytes_and_commits_free_file(setup_batch, failure):
    build, state = setup_batch
    _client, _ids, paths = build(second=True)
    before, mtime = paths[0].read_bytes(), paths[0].stat().st_mtime_ns
    free = paths[1].read_bytes()
    if failure == "busy":
        state.busy = paths[0]
    else:
        state.bad_footprint = paths[0]
    result = _run([{"symbol": n, "tags": ["new"]} for n in ("a", "last", "b")])
    assert [r["status"] for r in result["results"]] == ["error", "committed", "error"]
    assert result["results"][0]["error_kind"] == ("overlap_conflict" if failure == "busy" else "footprint_unresolvable")
    assert paths[0].read_bytes() == before and paths[0].stat().st_mtime_ns == mtime
    assert paths[1].read_bytes() == free.replace(b"slice_labels=old,keep,", b"slice_labels=old,keep,new,")
    assert result["partial_success"] and not state.held


@pytest.mark.parametrize("collision", ["same_handle", "resolved_alias", "physical"])
def test_colliding_pair_only_leaves_safe_neighbour(setup_batch, collision):
    build, _ = setup_batch
    client, ids, (path,) = build()
    before = path.read_bytes()
    other = "a" if collision == "same_handle" else "alias" if collision == "resolved_alias" else "b"
    if collision == "resolved_alias":
        client.candidates[other] = ids["a"]
    if collision == "physical":
        client.cache_by_id[ids["b"]]["note"] = "AST_QUALNAME: a\n"
    result = _run([{"symbol": n, "tags": ["new"]} for n in ("a", other, "safe")])
    assert [r["status"] for r in result["results"]] == (["error", "error", "committed"] if collision == "physical" else
                                                        ["duplicate_resolved_uuid", "duplicate_resolved_uuid", "committed"])
    safe = "\n".join(_block("b-safe", "safe", comment="disk")).encode()
    assert path.read_bytes() == before.replace(safe, safe.replace(b"slice_labels=old,keep,", b"slice_labels=old,keep,new,"))
    assert result["partial_success"]


def test_raised_item_exception_is_local_after_shared_infrastructure(setup_batch, monkeypatch):
    build, _ = setup_batch
    client, ids, (path,) = build()
    before = path.read_bytes()
    original = client.preflight_beacon_write_from_node
    async def fail(uid, op, mode):
        if uid == ids["b"]:
            raise RuntimeError("one item computation")
        return await original(uid, op, mode)
    monkeypatch.setattr(client, "preflight_beacon_write_from_node", fail)
    result = _run([{"symbol": n, "tags": ["new"]} for n in ("a", "b", "safe")])
    assert [r["status"] for r in result["results"]] == ["committed", "error", "committed"]
    assert result["results"][1]["error_kind"] == "preflight_error"
    b = "\n".join(_block("b-b", "b", comment="disk")).encode()
    assert b in path.read_bytes() and path.read_bytes() != before


@pytest.mark.parametrize("failure", ["cache", "root", "resolver", "suppression"])
def test_shared_infrastructure_failure_is_distinct_and_writes_nothing(setup_batch, monkeypatch, failure):
    build, state = setup_batch
    client, _ids, (path,) = build()
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    def broken():
        raise RuntimeError("shared infrastructure unavailable")
    async def broken_async(**kwargs):
        raise RuntimeError("shared infrastructure unavailable")
    roots = ["missing"] if failure == "root" else None
    if failure == "cache":
        monkeypatch.setattr(client, "_get_nodes_export_cache_nodes", broken)
    elif failure == "resolver":
        monkeypatch.setattr(srv, "_resolve_lightning_rod_symbol_items_from_cache", broken_async)
    elif failure == "suppression":
        monkeypatch.setattr(srv, "_beacon_symbol_suppress_tags", broken_async)
    spec = modes._spec_for("ensure_beacons", symbols=["a", "safe"], tags=["new"], roots=roots)
    result = asyncio.run(srv._lightning_rod_mutate_beacons(spec=spec))
    assert all(r["status"] == "not_run_due_to_batch_preflight_failure" for r in result["results"])
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    assert state.acquired == [] and client.commit_calls == []


@pytest.mark.parametrize("failure", ["text", "comment", "collision", "busy", "footprint", "exception"])
def test_explicit_atomic_preflight_or_admission_vetoes_all_writes(setup_batch, monkeypatch, failure):
    build, state = setup_batch
    client, ids, paths = build(second=True)
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
    ops = [{"symbol": n, "tags": ["new"]} for n in ("a", "last", "b")]
    if failure == "text":
        ops[0]["tags"] = ["bad tag"]
    elif failure == "comment":
        ops[0]["comment"] = "bad --> comment"
    elif failure == "collision":
        client.cache_by_id[ids["b"]]["note"] = "AST_QUALNAME: a\n"
    elif failure == "busy":
        state.busy = paths[0]
    elif failure == "footprint":
        state.bad_footprint = paths[0]
    else:
        async def broken(*args):
            raise RuntimeError("item preflight")
        monkeypatch.setattr(client, "preflight_beacon_write_from_node", broken)
    result = _run(ops, atomic=True)
    assert not result["success"] and not result["partial_success"] and result["files_changed"] == []
    assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths] == before
    assert client.commit_calls == []
    if failure == "busy":
        assert len(state.acquired) == 1 and len(state.acquired[0]) == 2
    elif failure != "footprint":
        assert state.acquired == []


def test_ambiguous_row_matches_check_finding_id_and_public_ack_keeps_it(setup_batch):
    build, _ = setup_batch
    client, ids, (path,) = build()
    source = _block("same", "a", "span") + _block("same", "b", "span") + _block("b-safe", "safe")
    path.write_bytes(("\n".join(source) + "\n").encode())
    before = path.read_bytes()
    client.cache_by_id[ids["a"]]["note"] = "BEACON (SPAN)\nid: same\nkind: span\n---\n"
    expected = next(f for f in check_beacon_file(str(path)) if f["code"] == "duplicate_id")
    ack = asyncio.run(dispatch_operation(srv.build_beacon_registry(), "tags.ensure", {
        "operations": [{"symbol": n, "tags": ["new"]} for n in ("a", "safe")], "root_handles": [modes._ROOT]}))
    row = ack["detail"]["results"][0]
    assert row["status"] == "error" and row["error_kind"] == "ambiguous_beacon_id"
    assert row["finding"]["finding_id"] == expected["finding_id"]
    assert row["source_path"] == str(path) and row["language"] == "python" and row["beacon_id"] == "same"
    assert row["candidate_lines"][:2] == [1, 9]
    final = next(f for f in check_beacon_file(str(path)) if f["code"] == "duplicate_id")
    assert final in row["findings"] and final in ack["detail"]["findings"]
    assert final["observed"]["file_sha256"] != expected["observed"]["file_sha256"]
    assert ack["changed"] and not ack["success"]
    prefix = ("\n".join(_block("same", "a", "span") + _block("same", "b", "span"))).encode()
    assert path.read_bytes().startswith(prefix) and path.read_bytes() != before


def test_stale_span_remove_is_not_a_satisfied_noop_and_lists_rename_candidates(setup_batch):
    build, _ = setup_batch
    client, ids, (path,) = build()
    path.write_bytes(("\n".join(_block("renamed", "a", "span") + _block("b-safe", "safe")) + "\n").encode())
    client.cache_by_id[ids["a"]]["note"] = "BEACON (SPAN)\nid: old-name\nkind: span\n---\n"
    before = path.read_bytes()
    result = _run([{"symbol": n, "tags": ["old"]} for n in ("a", "safe")], mode="remove_beacon_tags")
    row = result["results"][0]
    assert row["status"] == "noop" and row["noop_reason"] == "note_beacon_id_missing_on_disk"
    assert row["warning"] == "note_beacon_id_missing_on_disk" and row["rename_candidates"][0]["id"] == "renamed"
    assert not row["satisfied"] and not result["success"] and result["partial_success"]
    first = ("\n".join(_block("renamed", "a", "span"))).encode()
    assert path.read_bytes().startswith(first) and path.read_bytes() != before


@pytest.mark.parametrize("phase", ["preflight", "jit", "commit"])
def test_noop_and_commit_keep_findings_and_field_candidates(setup_batch, monkeypatch, phase):
    build, _ = setup_batch
    client, _ids, (path,) = build()
    # Duplicate unknown fields are advisory findings; they never gate labels.
    before = path.read_bytes().replace(b"#   kind=ast,", b"#   kind=ast,\n#   extra=x,")
    path.write_bytes(before)
    if phase == "jit":
        original = client.preflight_beacon_write_from_node
        async def actionable(*args):
            pf = await original(*args)
            return {**pf, "outcome": "actionable"}
        monkeypatch.setattr(client, "preflight_beacon_write_from_node", actionable)
    tag = "new" if phase == "commit" else "old"
    result = _run([{"symbol": n, "tags": [tag]} for n in ("a", "safe")])
    final = check_beacon_file(str(path))
    for row in result["results"]:
        assert row["findings"] and row["candidates"] and "diagnostics" in row and "field_outcomes" in row
        assert row["findings"] == [f for f in final if f["beacon_id"] == row["beacon_id"]]
        if phase != "commit":
            assert row["noop_reason"] == "already_satisfied"
    expected = before
    if phase == "commit":
        for name in ("a", "safe"):
            block = "\n".join(_block(f"b-{name}", name, comment="disk")).encode().replace(b"#   kind=ast,", b"#   kind=ast,\n#   extra=x,")
            expected = expected.replace(block, block.replace(b"slice_labels=old,keep,", b"slice_labels=old,keep,new,"))
    assert path.read_bytes() == expected


def test_cancelled_commit_finishes_before_shared_lease_release(setup_batch, monkeypatch):
    build, state = setup_batch
    client, _ids, (path,) = build()
    entered, finish = threading.Event(), threading.Event()
    original = acn._commit_beacon_write_plan_over_nodes
    def blocked(*args):
        entered.set()
        assert finish.wait(10)
        assert state.held
        return original(*args)
    monkeypatch.setattr(acn, "_commit_beacon_write_plan_over_nodes", blocked)
    before = path.read_bytes()
    async def exercise():
        task = asyncio.create_task(srv._lightning_rod_mutate_beacons(spec=modes._spec_for("ensure_beacons", symbols=["a", "safe"], tags=["new"])))
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and state.held and not state.released
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    try:
        asyncio.run(exercise())
    finally:
        finish.set()
    assert not state.held and isinstance(state.released[0], asyncio.CancelledError)
    assert path.read_bytes() != before and len(client.commit_calls) == 1


def test_snapshot_handoff_observes_exactly_the_planned_bytes_without_second_read(setup_batch, monkeypatch):
    build, _ = setup_batch
    client, ids, (path,) = build()
    path.write_bytes(path.read_bytes().replace(b"#   kind=ast,", b"#   kind=ast,\n#   extra=x,"))
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    original = acn._import_cartographer_module().read_source_snapshot
    snapshots = []
    def read(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        snapshots.append(snapshot)
        return snapshot
    monkeypatch.setattr(acn._import_cartographer_module(), "read_source_snapshot", read)
    operation = modes._spec_for("ensure_beacons", symbols=["a"], tags=["new"]).operations[0]
    plan = asyncio.run(client.plan_beacon_write_from_node(ids["a"], operation, "ensure_beacons"))
    assert len(snapshots) == 1
    assert plan.evidence["observed"]["file_sha256"] == snapshots[0].sha256 == plan.source_sha256
    assert plan.evidence["findings"] == []  # Plans retain only target-local evidence, not full-file findings.
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


def test_pre_snapshot_jit_failure_retains_labelled_preflight_findings(setup_batch, monkeypatch):
    build, _ = setup_batch
    client, ids, (path,) = build()
    path.write_bytes(path.read_bytes().replace(b"#   kind=ast,", b"#   kind=ast,\n#   extra=x,"))
    before = path.read_bytes()
    original = client.plan_beacon_write_from_node
    async def failed(uid, op, mode):
        if uid == ids["a"]:
            return SimpleNamespace(success=False, error="read failed", error_kind="disk_read_failed")
        return await original(uid, op, mode)
    monkeypatch.setattr(client, "plan_beacon_write_from_node", failed)
    result = _run([{"symbol": n, "tags": ["new"]} for n in ("a", "safe")])
    row = result["results"][0]
    assert row["status"] == "error" and row["findings"] and row["candidates"]
    assert row["retained_preflight_observed"]["file_sha256"] == hashlib.sha256(before).hexdigest()
    assert row["findings"][0]["observed"]["file_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert row["retained_preflight_observed"]["file_sha256"] != row["findings"][0]["observed"]["file_sha256"]
    assert result["results"][1]["status"] == "committed" and path.read_bytes() != before


def test_safe_label_absence_has_reason_and_keeps_independent_neighbour(setup_batch):
    build, _ = setup_batch
    _client, _ids, (path,) = build()
    first = "\n".join(_block("b-a", "a", comment="disk")).encode()
    result = _run([{"symbol": "a", "tags": ["absent"]}, {"symbol": "safe", "tags": ["old"]}], mode="remove_beacon_tags")
    assert result["results"][0]["status"] == "noop" and result["results"][0]["noop_reason"] == "already_absent"
    assert result["results"][1]["status"] == "committed" and path.read_bytes().startswith(first)


def test_sync_noop_preserves_fail_closed_success_and_adds_current_findings(setup_batch, monkeypatch):
    build, _ = setup_batch
    client, ids, (path,) = build()
    path.write_bytes(path.read_bytes().replace(b"#   kind=ast,", b"#   kind=ast,\n#   extra=x,"))
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    async def read(*args, **kwargs):
        return SimpleNamespace(name="a #old #keep", note="AST_QUALNAME: a\n")
    async def legacy(**kwargs):
        return {"success": False, "operation": "noop", "noop_reason": "already_satisfied", "source_path": str(path),
                "candidates": [{"line": 1}], "diagnostics": [{"field": "extra"}]}
    async def lease(*args):
        return []
    monkeypatch.setattr(srv, "_resolve_beacon_graph_handle", lambda _: (ids["a"], {}))
    monkeypatch.setattr(srv, "_node_read_impl", read)
    monkeypatch.setattr(srv, "_acquire_single_beacon_q3_lease", lease)
    monkeypatch.setattr(client, "update_beacon_from_node", legacy)
    ack = asyncio.run(srv._beacon_sync_node_impl(ids["a"]))
    assert not ack["success"] and not ack["changed"]
    assert ack["detail"]["noop_reason"] == "already_satisfied" and ack["detail"]["findings"]
    assert ack["detail"]["findings_basis"] == "current_disk_after_legacy_writer"
    assert ack["detail"]["candidates"] == [{"line": 1}]
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
