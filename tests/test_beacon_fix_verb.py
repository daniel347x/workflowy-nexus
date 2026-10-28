"""U06 F: public finding requests, real shared planner/writer, doubled Q3 only.

eaf3ac21 lacks this front; isolated baseline collection is missing-front
evidence, not predicate sensitivity. Named implementation mutants separately
exercise freshness, BR-01, admission and never-abandoned cancellation.
All fixture metadata is quoted physical line lists, never live test blocks.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import textwrap
import threading
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
assert (ROOT / "src/workflowy_mcp/beacon_fix.py").is_file(), "U06 beacon.fix front is absent from this source tree"

from coherence_harness import make_boundary_capable, make_coherence_root
from workflowy_mcp import beacon_fix as fix
from workflowy_mcp import beacon_integrity as bi
from workflowy_mcp import server as srv
from workflowy_mcp.carto_job_governance import FootprintUnresolvableError

ROOT_ID = "f1000000-0000-4000-8000-000000000001"
FOLDER_ID = "f2000000-0000-4000-8000-000000000002"
FILE_A = "f3000000-0000-4000-8000-000000000003"
FILE_B = "f4000000-0000-4000-8000-000000000004"
SYMBOL_ID = "f5000000-0000-4000-8000-000000000005"


def _block(name, *, bid=None, comment=None, alias=False, kind="ast"):
    rows = ["# @beacon[", f"#   id={bid or 'demo@' + name},", f"#   kind={kind},", "#   slice_labels=demo,"]
    if comment is not None:
        rows.append(f"#   {'comments' if alias else 'comment'}={comment},")
    return [*rows, "# ]", f"def {name}():", "    return 1"]


def _raw(*blocks):
    return ("\r\n".join(row for block in blocks for row in [*block, ""]) + "\r\n").encode()


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _finding(path, code="empty_comment", bid=None, occurrence=0):
    rows = bi.check_beacon_file(str(path))
    matches = [r for r in rows if r["code"] == code and (bid is None or r["beacon_id"] == bid)]
    assert len(matches) > occurrence, [(r["code"], r["beacon_id"]) for r in rows]
    return matches[occurrence]


def _item(path, row=None, resolution="replace_comment", params=None, **target):
    row = row or _finding(path)
    return {"finding_id": row["finding_id"], "observed": json.loads(json.dumps(row["observed"])),
            "resolution": resolution, "params": {"member": 0, "comment": "Chosen."} if params is None else params,
            **(target or {"file_path": str(path)})}


def _call(items, **kwargs):
    ack = asyncio.run(srv.beacon(verb="fix", params={"items": items, **kwargs}))
    assert "detail" in ack, ack
    assert ack["changed"] == ack["detail"]["changed"]
    return ack["detail"]


def _install_retired_scope_unpack(monkeypatch):
    """CF-46 mutant: restore the retired tuple-unpack inside ``BeaconAdmission.admit``.

    The injected account-scope callback stays scalar, exactly as production's
    ``_carto_account_scope`` now is; only the consumer is mutated back to the
    retired ``(scope, env)`` unpack, compiled in memory from the live method.
    A real successful fix acquisition must then fail its own success oracle
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
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    a = _write(repo / "pkg" / "a.py", _raw(_block("first"), _block("safe")))
    b = _write(repo / "pkg" / "b.py", _raw(_block("other")))
    nodes = [
        {"id": ROOT_ID, "parent_id": None, "name": "Source map", "note": f"Root: {repo}"},
        {"id": FOLDER_ID, "parent_id": ROOT_ID, "name": "pkg", "note": "Path: pkg"},
        {"id": FILE_A, "parent_id": FOLDER_ID, "name": "a.py", "note": "Path: a.py"},
        {"id": FILE_B, "parent_id": FOLDER_ID, "name": "b.py", "note": "Path: b.py"},
        {"id": SYMBOL_ID, "parent_id": FILE_A, "name": "first", "note": "AST_QUALNAME: first"},
    ]
    client = object.__new__(srv.WorkFlowyClient)
    make_boundary_capable(client, nodes=nodes)
    coherence = make_coherence_root(tmp_path)
    client._coherence_paths = lambda: coherence
    monkeypatch.setattr(srv, "get_client", lambda: client)
    monkeypatch.setattr(srv, "_rate_limiter", None)
    state = SimpleNamespace(acquisitions=[], releases=[], held=set(), busy=None, on_acquire=None)

    async def acquire(targets, **kwargs):
        state.acquisitions.append(copy.deepcopy(targets))
        assert not state.held
        if state.busy and any(target["path"] == str(state.busy) for target in targets):
            raise RuntimeError("test busy file")
        if state.on_acquire:
            state.on_acquire(targets)
        leases = [{"lease_id": f"test-{len(state.acquisitions)}", "path": target["path"]} for target in targets]
        state.held.update(lease["lease_id"] for lease in leases)
        return leases

    async def release(leases, *, primary_error=None):
        assert state.held == {lease["lease_id"] for lease in leases}
        state.releases.append(primary_error)
        state.held.clear()

    monkeypatch.setattr(srv, "acquire_q3_leases_async", acquire)
    monkeypatch.setattr(srv, "_release_q3_leases_preserving_primary", release)
    monkeypatch.setattr(srv, "_carto_account_scope", lambda: "test-account")
    _install_retired_scope_unpack(monkeypatch)
    monkeypatch.setattr(srv, "_governance_jobs_root", lambda: "unused-by-double")

    def forbidden(*args, **kwargs):
        raise AssertionError("fix reached a tab, Workflowy write or refresh")

    for name in ("_node_read_impl", "_beacon_symbol_suppress_tags", "_system_cache_refresh_impl", "_start_carto_refresh_job"):
        monkeypatch.setattr(srv, name, forbidden)
    return SimpleNamespace(repo=repo, a=a, b=b, nodes=nodes, client=client, state=state)


def test_public_apply_by_id_exact_bytes_one_file_lease_and_findings(env):
    before = env.a.read_bytes()
    first, safe = _item(env.a, _finding(env.a, bid="demo@first")), _item(env.a, _finding(env.a, bid="demo@safe"))
    result = _call([first, safe])
    assert result["success"] and result["changed"] and not result["partial_success"]
    assert [r["status"] for r in result["rows"]] == ["committed", "committed"]
    assert env.a.read_bytes() == before.replace(b"# ]", b"#   comment=Chosen.,\r\n# ]")
    assert len(env.state.acquisitions) == len(env.state.releases) == 1
    assert env.state.acquisitions[0][0]["root_uuid"] == FILE_A
    assert all(r["findings"] == [] and r["lease"] == "q3" and r["finding"]["code"] == "empty_comment" for r in result["rows"])
    assert result["rows"][1]["before_sha256"] == result["rows"][0]["after_sha256"]
    assert result["files_changed"] == [str(env.a)] and result["findings"] == bi.check_beacon_file(str(env.a)) == []
    reread = _call([first, safe])
    assert all(row["error_kind"] == "stale" for row in reread["rows"])
    assert not reread["changed"]


def test_preview_is_optional_read_only_independent_and_real_apply_rederives(env):
    item = _item(env.a, _finding(env.a, bid="demo@first"))
    raw, mtime = env.a.read_bytes(), env.a.stat().st_mtime_ns
    result = _call([item], dry_run=True)
    row = result["rows"][0]
    assert row["status"] == "preview" and row["would_change"] and not row["changed"]
    assert row["before_sha256"] == hashlib.sha256(raw).hexdigest()
    assert row["candidate_sha256"] == row["after_sha256"] != row["before_sha256"]
    assert "+#   comment=Chosen.," in row["unified_diff"]
    assert row["affected_ids"] == ["demo@first"] and row["affected_symbols"]
    assert row["consequences"] and not env.state.acquisitions
    assert (env.a.read_bytes(), env.a.stat().st_mtime_ns) == (raw, mtime)
    env.a.write_bytes(raw.replace(b"id=demo@first", b"id=demo@renamed"))
    changed = env.a.read_bytes()
    safe = _item(env.a, _finding(env.a, bid="demo@safe"))
    actual = _call([item, safe])
    assert actual["rows"][0]["error_kind"] == "stale"
    assert actual["rows"][1]["changed"] and env.a.read_bytes().startswith(changed.split(b"def first")[0])


def test_replace_comment_ack_has_final_findings_and_preserves_acted_on_finding(env):
    finding = _finding(env.a, bid="demo@first")
    result = _call([_item(env.a, finding)])
    row = result["rows"][0]
    final_sha = hashlib.sha256(env.a.read_bytes()).hexdigest()
    assert row["status"] == "committed" and row["findings"] == []
    assert row["finding"] == finding
    assert result["findings"] == bi.check_beacon_file(str(env.a))
    assert result["findings"] and all(f["observed"]["file_sha256"] == final_sha for f in result["findings"])
    assert not any(f["code"] == "empty_comment" and f["beacon_id"] == "demo@first" for f in result["findings"])


@pytest.mark.parametrize("change", ["insert", "identical_insert", "content", "rename"])
def test_external_drift_rebinds_only_unique_unchanged_members_with_safe_neighbour(env, change):
    item = _item(env.a, _finding(env.a, bid="demo@first"))
    original = env.a.read_bytes()
    if change == "insert":
        altered = b"# external line\r\n" + original
    elif change == "identical_insert":
        altered = _raw(_block("first")) + original
    elif change == "content":
        altered = original.replace(b"slice_labels=demo,", b"slice_labels=changed,", 1)
    else:
        altered = original.replace(b"id=demo@first", b"id=demo@renamed", 1)
    env.a.write_bytes(altered)
    safe = _item(env.a, _finding(env.a, bid="demo@safe"))
    result = _call([item, safe])
    assert result["rows"][1]["status"] == "committed"
    if change == "insert":
        assert result["rows"][0]["status"] == "committed"
        assert env.a.read_bytes() == altered.replace(b"# ]", b"#   comment=Chosen.,\r\n# ]")
    else:
        assert result["rows"][0]["error_kind"] == "stale"
        assert env.a.read_bytes().startswith(altered[:altered.index(b"def safe")].rsplit(b"# @beacon[", 1)[0])
        assert result["partial_success"]


def test_identical_members_reject_external_version_but_br01_carries_request_owned_offsets(env):
    repeated = _block("same", bid="demo@same", kind="span")
    env.a.write_bytes(_raw(_block("grow"), repeated, repeated))
    twins = [r for r in bi.check_beacon_file(str(env.a)) if r["code"] == "empty_comment" and r["beacon_id"] == "demo@same"]
    assert len(twins) == 2 and all(r["observed"]["identity_ambiguous"] for r in twins)
    grow = _item(env.a, _finding(env.a, bid="demo@grow"), params={"member": 0, "comment": "Growth adds a physical metadata line."})
    chosen = _item(env.a, twins[1])
    raw = env.a.read_bytes()
    result = _call([grow, chosen])
    assert [r["status"] for r in result["rows"]] == ["committed", "committed"]
    expected = raw.replace(b"# ]", b"#   comment=Growth adds a physical metadata line.,\r\n# ]", 1)
    at = expected.rfind(b"# ]")
    expected = expected[:at] + b"#   comment=Chosen.,\r\n" + expected[at:]
    assert env.a.read_bytes() == expected
    # The identical first twin remained byte-identical; no shifted ordinal guess.
    assert _raw(repeated).strip() in env.a.read_bytes()

    env.a.write_bytes(b"# external insertion\r\n" + raw)
    safe = _item(env.a, _finding(env.a, bid="demo@grow"))
    refused = _call([chosen, safe])
    assert refused["rows"][0]["error_kind"] == "stale"
    assert refused["rows"][1]["changed"]


@pytest.mark.parametrize("bad", ["unoffered", "params", "params_type", "witness", "malformed", "wrong_file"])
def test_bad_item_never_costs_its_committing_same_file_neighbour(env, bad):
    item = _item(env.a, _finding(env.a, bid="demo@first"))
    if bad == "unoffered":
        item["resolution"] = "rename_id"
    elif bad == "params":
        item["params"]["member"] = True
    elif bad == "params_type":
        item["params"] = "not a params object"
    elif bad == "witness":
        start, end = item["observed"]["block_ranges"][0]
        item["observed"]["block_ranges"][0] = [start + 1, end]
    elif bad == "wrong_file":
        item["observed"]["canonical_file"] = str(env.b)
    else:
        item = 123
    raw = env.a.read_bytes()
    safe = _item(env.a, _finding(env.a, bid="demo@safe"))
    result = _call([item, safe])
    assert result["rows"][0]["error_kind"] == {"unoffered": "resolution_not_offered", "params": "invalid_params", "params_type": "invalid_params", "malformed": "item_invalid"}.get(bad, "stale")
    assert result["rows"][1]["status"] == "committed"
    assert env.a.read_bytes() == raw.rsplit(b"# ]", 1)[0] + b"#   comment=Chosen.,\r\n# ]" + raw.rsplit(b"# ]", 1)[1]
    assert result["rows"][1]["findings"] == [] and result["rows"][1]["finding"]["beacon_id"] == "demo@safe"
    assert result["findings"] == bi.check_beacon_file(str(env.a))


def test_colliding_rows_only_fail_before_write_and_safe_neighbour_commits(env):
    first = _item(env.a, _finding(env.a, bid="demo@first"))
    other = copy.deepcopy(first)
    other["params"]["comment"] = "Conflicting."
    raw = env.a.read_bytes()
    safe = _item(env.a, _finding(env.a, bid="demo@safe"))
    result = _call([first, other, safe])
    assert [r.get("error_kind") for r in result["rows"]] == ["collision", "collision", None]
    assert result["rows"][0]["evidence"]["conflicting_indices"] == [1]
    assert result["rows"][2]["changed"]
    assert env.a.read_bytes().startswith(raw[:raw.index(b"def first") + len(b"def first")])


def test_one_busy_file_only_costs_its_rows_other_file_written(env):
    raw, mtime = env.a.read_bytes(), env.a.stat().st_mtime_ns
    env.state.busy = env.a
    result = _call([_item(env.a, _finding(env.a, bid="demo@first")), _item(env.b)])
    assert result["rows"][0]["error_kind"] == "overlap_conflict"
    assert result["rows"][1]["status"] == "committed" and result["partial_success"]
    assert (env.a.read_bytes(), env.a.stat().st_mtime_ns) == (raw, mtime)
    assert len(env.state.acquisitions) == 2 and len(env.state.releases) == 1 and not env.state.held


@pytest.mark.parametrize("branch", ["file", "folder", "root", "unmapped"])
def test_path_admission_uses_file_nearest_folder_root_or_none_unmapped(env, branch):
    path = env.a
    expected = FILE_A
    if branch == "folder":
        path = _write(env.repo / "pkg" / "new.py", _raw(_block("fresh")))
        expected = FOLDER_ID
    elif branch == "root":
        path = _write(env.repo / "new.py", _raw(_block("fresh")))
        expected = ROOT_ID
    elif branch == "unmapped":
        path = _write(env.repo.parent / "unmapped.py", _raw(_block("fresh")))
        expected = None
    result = _call([_item(path, _finding(path, bid="demo@first") if branch == "file" else None)])
    assert result["rows"][0]["status"] == "committed"
    if expected is None:
        assert result["rows"][0]["lease"] == "none_unmapped" and not env.state.acquisitions
    else:
        target = env.state.acquisitions[0][0]
        assert target["root_uuid"] == expected and target["path"] == str(path) and target["kind"] == "file"
        assert env.state.releases == [None]


def test_node_mode_requires_file_and_uses_shared_handle_authority(env):
    raw = env.a.read_bytes()
    bad = _item(env.a, _finding(env.a, bid="demo@first"), node_handle=SYMBOL_ID)
    missing = _item(env.a, _finding(env.a, bid="demo@first"), node_handle="ffffffff")
    good = _item(env.a, _finding(env.a, bid="demo@safe"), node_handle=FILE_A[:8])
    result = _call([bad, missing, good])
    assert [r.get("error_kind") for r in result["rows"]] == ["map_target_invalid", "node_handle_unresolved", None]
    assert result["rows"][2]["changed"] and env.a.read_bytes().startswith(raw[:raw.index(b"def first")])


def test_unresolvable_footprint_is_file_local(env, monkeypatch):
    original = srv.compute_footprint

    def footprint(path, uid, cache, *, kind):
        if uid == FILE_A:
            raise FootprintUnresolvableError("test missing ancestor")
        return original(path, uid, cache, kind=kind)

    monkeypatch.setattr(srv, "compute_footprint", footprint)
    raw = env.a.read_bytes()
    result = _call([_item(env.a, _finding(env.a, bid="demo@first")), _item(env.b)])
    assert result["rows"][0]["error_kind"] == "footprint_unresolvable"
    assert result["rows"][1]["changed"] and env.a.read_bytes() == raw


@pytest.mark.parametrize("veto", ["preflight", "admission"])
def test_atomic_veto_before_any_write_is_not_a_default_mode_gate(env, veto):
    a, b = _item(env.a, _finding(env.a, bid="demo@first")), _item(env.b)
    raw = [path.read_bytes() for path in (env.a, env.b)]
    if veto == "preflight":
        a["resolution"] = "not_offered"
    else:
        env.state.busy = env.a
    result = _call([a, b], atomic=True)
    assert not result["success"] and not result["changed"] and result["batch_aborted"]
    assert [path.read_bytes() for path in (env.a, env.b)] == raw
    assert len(env.state.acquisitions) == (0 if veto == "preflight" else 1)
    if veto == "admission":
        assert len(env.state.acquisitions[0]) == 2
    assert all(row["findings"] for row in result["rows"])
    # The explicit veto does not leak into default mode: the same failed item
    # still has a safe committing neighbour (in the only nonconflicting file).
    local = _call([a, b])
    assert not local["rows"][0]["success"] and local["rows"][1]["changed"]


def test_atomic_jit_failure_does_not_roll_back_prior_commits(env, monkeypatch):
    items = [_item(env.a, _finding(env.a, bid="demo@first")), _item(env.b)]
    commit = fix.commit_beacon_edit_plan

    def fail_second(plan, **kwargs):
        if plan.snapshot.canonical_path == str(env.b):
            return {"success": False, "changed": False, "error_kind": "disk_write_failed", "error": "injected"}
        assert env.state.held
        return commit(plan, **kwargs)

    monkeypatch.setattr(fix, "commit_beacon_edit_plan", fail_second)
    before = [p.read_bytes() for p in (env.a, env.b)]
    result = _call(items, atomic=True)
    assert result["rows"][0]["changed"] and result["rows"][1]["error_kind"] == "disk_write_failed"
    assert env.a.read_bytes() != before[0] and env.b.read_bytes() == before[1]
    assert "after_sha256" not in result["rows"][1]
    assert len(env.state.acquisitions) == len(env.state.releases) == 1


def test_atomic_sequence_preflight_catches_known_cross_file_id_collision(env):
    env.a.write_bytes(env.a.read_bytes().replace(b"id=demo@first,", b"id=,", 1))
    env.b.write_bytes(_raw(_block("other"), _block("safe")).replace(b"id=demo@other,", b"id=,", 1))
    args = {"member": 0, "field": "id", "value": "chosen@shared"}
    first = _item(env.a, _finding(env.a, "missing_id"), "set_field", args)
    second = _item(env.b, _finding(env.b, "missing_id"), "set_field", args)
    safe = _item(env.b, _finding(env.b, bid="demo@safe"))
    before = env.a.read_bytes(), env.b.read_bytes()
    result = _call([first, second, safe], atomic=True)
    assert not result["changed"] and result["batch_aborted"] and not env.state.acquisitions
    assert result["rows"][1]["error_kind"] == "no_safe_geometry"
    assert result["rows"][1]["phase"] == "atomic_sequence_preflight"
    assert (env.a.read_bytes(), env.b.read_bytes()) == before
    local = _call([first, second, safe])
    assert local["rows"][0]["changed"] and local["rows"][2]["changed"]
    assert local["rows"][1]["error_kind"] == "no_safe_geometry"


@pytest.mark.parametrize("atomic", [False, True])
def test_allocator_accumulates_planned_ids_across_files_without_extra_gate(env, atomic):
    env.a.write_bytes(env.a.read_bytes().replace(b"id=demo@first,", b"id=,", 1))
    env.b.write_bytes(env.b.read_bytes().replace(b"id=demo@other,", b"id=,", 1))
    args = {"member": 0, "field": "id", "value": ""}
    items = [_item(p, _finding(p, "missing_id"), "set_field", args) for p in (env.a, env.b)]
    result = _call(items, atomic=atomic)
    assert result["success"] and all(r["changed"] for r in result["rows"])
    assert b"id=auto-beacon," in env.a.read_bytes()
    assert b"id=auto-beacon-2," in env.b.read_bytes()


def test_jit_source_change_and_final_hash_failure_stay_local(env, monkeypatch):
    first, safe = _item(env.a, _finding(env.a, bid="demo@first")), _item(env.a, _finding(env.a, bid="demo@safe"))
    commit = fix.commit_beacon_edit_plan
    fired = False

    def race(plan, **kwargs):
        nonlocal fired
        if not fired:
            fired = True
            return commit(plan, before_final_hash=lambda: env.a.write_bytes(b"# editor\r\n" + env.a.read_bytes()), **kwargs)
        return commit(plan, **kwargs)

    monkeypatch.setattr(fix, "commit_beacon_edit_plan", race)
    result = _call([first, safe])
    assert result["rows"][0]["error_kind"] == "source_changed_since_plan"
    assert not result["rows"][0]["changed"] and result["rows"][1]["status"] == "committed"


def test_cancellation_does_not_release_lease_before_commit_thread_finishes(env, monkeypatch):
    item = _item(env.a, _finding(env.a, bid="demo@first"))
    entered, finish, completed = threading.Event(), threading.Event(), threading.Event()
    commit = fix.commit_beacon_edit_plan

    def slow(plan, **kwargs):
        assert env.state.held
        entered.set()
        assert finish.wait(10), "test failed to release commit thread"
        result = commit(plan, **kwargs)
        completed.set()
        return result

    monkeypatch.setattr(fix, "commit_beacon_edit_plan", slow)

    async def scenario():
        task = asyncio.create_task(srv.beacon(verb="fix", params={"items": [item]}))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            task.cancel()
            await asyncio.sleep(0.03)
            assert not task.done(), "caller abandoned the committing thread"
            assert env.state.held and not env.state.releases and not completed.is_set()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed.is_set() and not env.state.held
        assert len(env.state.releases) == 1 and isinstance(env.state.releases[0], asyncio.CancelledError)

    asyncio.run(scenario())


def test_fifty_items_one_stale_one_unoffered_other_48_commit(env):
    env.a.write_bytes(_raw(*[_block(f"f{i}") for i in range(50)]))
    rows = bi.check_beacon_file(str(env.a))
    items = [_item(env.a, next(r for r in rows if r["code"] == "empty_comment" and r["beacon_id"] == f"demo@f{i}")) for i in range(50)]
    items[7]["finding_id"] = "bf1:" + "0" * 64
    items[13]["resolution"] = "unoffered"
    result = _call(items)
    assert sum(r["status"] == "committed" for r in result["rows"]) == 48
    assert result["rows"][7]["error_kind"] == "stale" and result["rows"][13]["error_kind"] == "resolution_not_offered"
    assert result["partial_success"] and len(env.state.acquisitions) == 1


def test_noop_error_commit_public_ack_keeps_findings(env):
    first = _finding(env.a, bid="demo@first")
    noop = _item(env.a, first, "keep", {})
    bad = _item(env.a, first, "not_offered", {})
    safe = _item(env.a, _finding(env.a, bid="demo@safe"))
    result = _call([noop, bad, safe])
    assert [r["status"] for r in result["rows"]] == ["noop", "refused", "committed"]
    assert result["rows"][0]["noop_reason"] == "kept"
    assert all(r["findings"] for r in result["rows"][:2]) and result["rows"][2]["findings"] == []
    assert result["findings"] == bi.check_beacon_file(str(env.a)) and len(result["findings"]) == 1


def test_distinct_worktrees_cannot_reuse_relative_file_identity(env):
    copy_path = _write(env.repo.parent / "other-worktree" / "pkg" / "a.py", env.a.read_bytes())
    one = _finding(env.a, bid="demo@first")
    two = _finding(copy_path, bid="demo@first")
    assert one["finding_id"] != two["finding_id"]
    wrong = _item(copy_path, one)
    safe = _item(copy_path, _finding(copy_path, bid="demo@safe"))
    result = _call([wrong, safe])
    assert result["rows"][0]["error_kind"] == "stale" and result["rows"][1]["changed"]


def test_explicit_rename_reports_identity_consequences_without_veto(env):
    env.a.write_bytes(_raw(_block("first", bid="demo@other", comment="Concept."), _block("other", bid="safe@neighbour")))
    row = _finding(env.a, "id_unlike_symbol")
    rename = _item(env.a, row, "rename_id", {"member": 0, "new_id": "unrelated@chosen"})
    result = _call([rename])
    assert result["success"] and result["rows"][0]["changed"]
    assert result["rows"][0]["consequences"][0]["potential_workflowy_uuid_change"]
    assert result["rows"][0]["consequences"][0]["stale_note_anchors"]
    assert b"id=unrelated@chosen," in env.a.read_bytes()


def test_snapshot_in_observation_has_one_read_and_analysis_per_round(env, monkeypatch):
    item = _item(env.a, _finding(env.a, bid="demo@first"))
    counts = CounterForObservation()
    read, analyze = fix.read_source_snapshot, bi.analyze_beacon_snapshot

    def counted_read(path):
        counts.reads += 1
        snapshot = read(path)
        counts.snapshots.append(snapshot)
        return snapshot

    def counted_analysis(snapshot, **kwargs):
        assert snapshot is counts.snapshots[-1]
        counts.analyses += 1
        return analyze(snapshot, **kwargs)

    monkeypatch.setattr(fix, "read_source_snapshot", counted_read)
    monkeypatch.setattr(bi, "analyze_beacon_snapshot", counted_analysis)
    result = _call([item])
    assert result["success"] and counts.reads == counts.analyses == 2  # preflight + actual


@pytest.mark.parametrize("operand", ["range", "boundary"])
def test_external_line_growth_translates_only_witnessed_byte_operands(env, operand):
    if operand == "range":
        raw = _raw(_block("first")[:-3] + ["#   opaque=literal,", *_block("first")[-3:]], _block("safe"))
        # Put the opaque field inside the complete metadata, not in source.
        env.a.write_bytes(raw)
        finding = _finding(env.a, "unknown_field_key", "demo@first")
        bounds = next(r for r in finding["members"][0]["field_ranges"] if b"opaque=" in raw[r[0]:r[1]])
        item = _item(env.a, finding, "delete_field", {"member": 0, "range": list(bounds)})
        expected = raw[:bounds[0]] + raw[bounds[1]:]
    else:
        raw = _raw(["# @beacon[", "#   id=demo@first,", "#   kind=ast,", "#   slice_labels=demo,",
                    "def first():", "    return 1"], _block("safe"))
        env.a.write_bytes(raw)
        finding = _finding(env.a, "unterminated_block", "demo@first")
        boundary = finding["detail"]["boundaries"][0]
        item = _item(env.a, finding, "insert_bracket", {"member": 0, "boundary": boundary})
        expected = raw[:boundary] + b"\r\n# ]" + raw[boundary:]
    insertion = "# external π\r\n".encode()
    env.a.write_bytes(insertion + raw)
    safe = _item(env.a, _finding(env.a, bid="demo@safe"))
    result = _call([item, safe])
    assert [r["status"] for r in result["rows"]] == ["committed", "committed"], result
    # Only the independent safe member gains its comment after the first edit.
    stop = expected.rfind(b"# ]")
    expected = expected[:stop] + b"#   comment=Chosen.,\r\n" + expected[stop:]
    assert env.a.read_bytes() == insertion + expected


def test_request_maps_do_not_authorize_an_external_shift_of_identical_members(env, monkeypatch):
    repeated = _block("same", bid="demo@same", kind="span")
    env.a.write_bytes(_raw(_block("grow"), repeated, repeated, _block("safe")))
    grow = _item(env.a, _finding(env.a, bid="demo@grow"))
    twin = _item(env.a, _finding(env.a, bid="demo@same", occurrence=1))
    safe = _item(env.a, _finding(env.a, bid="demo@safe"))
    commit, once = fix.commit_beacon_edit_plan, False

    def external_after_commit(plan, **kwargs):
        nonlocal once
        result = commit(plan, **kwargs)
        if not once:
            once = True
            env.a.write_bytes(b"# outside-request\r\n" + env.a.read_bytes())
        return result

    monkeypatch.setattr(fix, "commit_beacon_edit_plan", external_after_commit)
    result = _call([grow, twin, safe])
    assert result["rows"][0]["changed"]
    assert result["rows"][1]["error_kind"] == "stale"
    assert result["rows"][2]["changed"] and result["partial_success"]


def test_unexpected_plan_exception_is_item_local_but_cache_failure_is_global(env, monkeypatch):
    first, safe = _item(env.a, _finding(env.a, bid="demo@first")), _item(env.a, _finding(env.a, bid="demo@safe"))
    plan = fix.plan_beacon_resolution

    def broken(snapshot, finding, *args, **kwargs):
        if finding["beacon_id"] == "demo@first":
            raise RuntimeError("local plan fault")
        return plan(snapshot, finding, *args, **kwargs)

    monkeypatch.setattr(fix, "plan_beacon_resolution", broken)
    result = _call([first, safe])
    assert result["rows"][0]["error_kind"] == "fix_failed" and result["rows"][0]["findings"]
    assert result["rows"][1]["changed"]
    before = env.a.read_bytes(), env.b.read_bytes()

    def failed_cache():
        raise RuntimeError("global cache fault")

    monkeypatch.setattr(srv, "_current_cache_by_id", failed_cache)
    ack = asyncio.run(srv.beacon(verb="fix", params={"items": [first, _item(env.b)]}))
    assert ack["error"]["code"] == "operation_failed"
    assert (env.a.read_bytes(), env.b.read_bytes()) == before


def test_file_read_and_analysis_exceptions_do_not_abort_neighbours(env, monkeypatch):
    item_a, item_b = _item(env.a, _finding(env.a, bid="demo@first")), _item(env.b)
    observer = fix.observe_beacon_snapshot

    def bad_analysis(snapshot, **kwargs):
        if snapshot.canonical_path == str(env.a):
            raise RuntimeError("analyzer fault")
        return observer(snapshot, **kwargs)

    monkeypatch.setattr(fix, "observe_beacon_snapshot", bad_analysis)
    result = _call([item_a, item_b])
    assert result["rows"][0]["error_kind"] == "analysis_failed" and result["rows"][1]["changed"]
    monkeypatch.setattr(fix, "observe_beacon_snapshot", observer)
    # A vanished path and an unsupported item remain rows, not envelope errors.
    env.b.write_bytes(_raw(_block("other")))
    item_b = _item(env.b)
    missing = copy.deepcopy(item_a)
    missing["file_path"] = str(env.repo / "missing.py")
    result = _call([missing, item_b])
    assert result["rows"][0]["error_kind"] == "source_read_failed" and result["rows"][1]["changed"]


def test_release_failure_keeps_written_rows_honest_and_other_file_proceeds(env, monkeypatch):
    release = srv._release_q3_leases_preserving_primary

    async def bad_release(leases, *, primary_error=None):
        await release(leases, primary_error=primary_error)
        if leases[0]["path"] == str(env.a):
            raise RuntimeError("cleanup fault")

    monkeypatch.setattr(srv, "_release_q3_leases_preserving_primary", bad_release)
    result = _call([_item(env.a, _finding(env.a, bid="demo@first")), _item(env.b)])
    assert result["rows"][0]["error_kind"] == "governance_cleanup_required"
    assert result["rows"][0]["changed"] and result["rows"][0]["after_sha256"] == hashlib.sha256(env.a.read_bytes()).hexdigest()
    assert result["rows"][1]["status"] == "committed" and not env.state.held
    assert result["files_changed"] == [str(env.a), str(env.b)]


@pytest.mark.parametrize("language", ["python", "js_ts", "markdown", "vue"])
def test_public_fix_uses_the_planners_section_aware_scanner_for_publication(env, language):
    if language == "python":
        rows, ext = _block("fresh", comment="Alias.", alias=True), ".py"
    elif language == "js_ts":
        rows, ext = ["// @beacon[", "//   id=demo@fresh,", "//   kind=ast,", "//   slice_labels=demo,", "//   comments=Alias.,", "// ]", "function fresh() {}"], ".ts"
    elif language == "markdown":
        rows, ext = ["<!--", "@beacon[", "  id=demo@fresh,", "  kind=ast,", "  slice_labels=demo,", "  comments=Alias.,", "]", "-->", "# Fresh", "prose"], ".md"
    else:
        rows, ext = ["<template>", "<div>unchanged</div>", "</template>", '<script lang="ts">',
                     "// @beacon[", "//   id=demo@fresh,", "//   kind=ast,", "//   slice_labels=demo,", "//   comments=Alias.,", "// ]", "function fresh() {}", "</script>"], ".vue"
    path = _write(env.repo / "pkg" / ("fresh" + ext), _raw(rows))
    item = _item(path, _finding(path, "legacy_field"), "normalize", {"member": 0})
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    preview = _call([item], dry_run=True)
    assert preview["success"] and preview["rows"][0]["would_change"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, mtime)
    result = _call([item])
    assert result["success"] and result["rows"][0]["status"] == "committed", result
    assert path.read_bytes() == before.replace(b"comments=", b"comment=")


class CounterForObservation:
    def __init__(self):
        self.reads = 0
        self.analyses = 0
        self.snapshots = []


def test_fix_describe_and_envelope_contract(env, monkeypatch):
    spec = srv.build_beacon_registry().describe_full("fix")
    assert spec["side_effects"] == ["disk_mutation"] and spec["async"] == "never"
    description = spec["returns"]["description"]
    for text in ("dry_run is optional", "none_unmapped", "newly listed choice", "not rollback", "two-step route", "tags.remove", "write_ranges"):
        assert text in description
    with pytest.raises(fix.BeaconFixRequestInvalid):
        fix.normalize_fix_request({"items": "not-a-list"})
    monkeypatch.setattr(srv, "_current_cache_by_id", lambda: {n["id"]: n for n in env.nodes})
    with pytest.raises(srv._BeaconOperationError) as caught:
        asyncio.run(srv._beacon_fix_impl({"items": [], "atomic": "bad"}))
    assert caught.value.code == "beacon_fix_request_invalid"
    bad_only = _call([123], atomic=True)
    assert bad_only["batch_aborted"] and not bad_only["changed"] and not env.state.acquisitions
