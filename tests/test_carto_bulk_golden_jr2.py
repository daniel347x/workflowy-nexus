"""A1 (iii)-(v) joined bulk witnesses and the frozen fc79975 runner oracle (JR2 §3.1).

Two jobs, and only two:

* **Differential witnesses** (the `async` tests) run ONE scenario TWICE, each
  arm in its own `real_job_environment` (its own client, graph, jobs directory
  and state) at ONE environment path: the frozen arm runs first, its tree is
  removed, and the live arm builds pristine bytes at the same path.  Once on
  today's bulk runner, once on the runner frozen at `fc79975` in
  `tests/fixtures/carto_job_records/g5_baseline.json`.  Only the frozen server
  bulk runner/start/writer helpers and the governance admission are rebound for
  the oracle arm; the client writer, the record funnel and the reader stay
  today's on both sides.  The arms therefore differ in the runner that PRODUCES
  the evidence and in nothing else, and each arm's rows are read through the
  real `system.job.status` `view="detail"` view joined across its pages -- never
  the record folded against itself.  The rows must be equal exactly, once each
  arm's own job id and artifact-name clock stamps are canonicalised; D3-1's
  documented outline marker is applied to both sides, so the transform itself is
  never a differential.
* **Frozen-source checks** (`test_frozen_*`) need no daemon: they rebuild each
  frozen source's `/2` counterpart and prove the fold/stitch against the source
  the harness captured.  A source not captured yet is a SKIP naming the missing
  file, never a silent pass.

No test writes into the repository; the builder and the frozen baseline are read
by path, and every arm builds into `tmp_path`.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import re
import shutil
from pathlib import Path

import pytest
from n07_job_harness import real_job_environment, scrubbed_runtime
# `real_job_environment` is the one real daemon environment; the differential
# builds two independent instances from the fixture's own body (see
# `_isolated_environment`), so both names stay imported.
from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp import server as srv
from workflowy_mcp.carto_job_record import (
    fold_carto_job_evidence,
    join_carto_job_detail_pages,
    stitch_carto_job_detail,
)
from workflowy_mcp.client import api_client_nexus as acn

import nexus_map_codebase as nmc

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "carto_job_records"


def _source(fixture, name):
    """Each fixture's frozen inputs live beside it, in ``<fixture>/sources/``."""
    return FIXTURE_ROOT / fixture / "sources" / name


# ---------------------------------------------------------------------------
# The committed fixture builder (a fixture asset, imported by path)
# ---------------------------------------------------------------------------
_BUILDER: dict[str, object] = {}


def _builder():
    if "module" not in _BUILDER:
        path = FIXTURE_ROOT / "build_fixtures.py"
        spec = importlib.util.spec_from_file_location("carto_fixture_builder", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _BUILDER["module"] = module
    return _BUILDER["module"]


def _captured_records(document):
    """A capture document's write sequence, or None for a plain record."""
    if isinstance(document, dict) and isinstance(document.get("records"), list) and document["records"]:
        return document["records"]
    return None


def _counterpart(document):
    """Build one `/2` counterpart in memory: ``(record, files_text, nodes_text, source)``."""
    builder = _builder()
    records = _captured_records(document)
    if records is not None:
        sidecar = builder.build_sidecar_from_sequence(
            records, kind=str(document.get("kind") or "bulk"),
            aggregate_inputs=document.get("aggregate_inputs") or (),
        )
        source = records[-1]
        in_flight = document.get("in_flight") or sidecar.get("in_flight")
        if isinstance(in_flight, dict):
            in_flight = dict(in_flight)
            if in_flight.get("base_lines") is None:
                in_flight["base_lines"] = len(sidecar["files_lines"])
        record = builder.build_record(source, sidecar, moved=sidecar.get("moved"),
                                      in_flight=in_flight)
        nodes = "".join(sidecar["nodes_lines"]) or None
        return record, "".join(sidecar["files_lines"]), nodes, source
    source = document
    bulk = str(source.get("type") or "") == "CARTO_BULK_APPLY"
    moved = builder.moved_values(source)
    sidecar = builder.build_sidecar(source, bulk=bulk, moved=moved)
    record = builder.build_record(source, sidecar, moved=moved)
    nodes = "".join(sidecar["nodes_lines"]) or None
    return record, "".join(sidecar["files_lines"]), nodes, source


def _fold(record, files_text, nodes_text):
    return fold_carto_job_evidence(
        files_text,
        in_flight=record.get("in_flight"),
        status=record.get("status"),
        nodes=nodes_text,
        job_type=record.get("type"),
    )


def _assert_rows_equal(want_rows, got_rows, *, label=""):
    builder = _builder()
    assert [row.get("file") for row in got_rows] == [row.get("file") for row in want_rows], (
        f"{label}: row order/count differs")
    for want, got in zip(want_rows, got_rows):
        assert list(want) == list(got), f"{label}: per-row key order differs for {want.get('file')}"
        assert builder.compact(want) == builder.compact(got), (
            f"{label}: row differs for {want.get('file')}")


async def _joined_complete_detail(job_id, **params):
    """The detail view's complete evidence, joined across its pages (G2).

    ``system.job.status(job_id)`` is the summary view from JR2 G2; today's
    complete detail is ``view="detail"``, delivered a page at a time
    (WaveSpec rev 3 §2.10.1) and rebuilt by the view's own pure join.
    """
    pages, offset, slice_offset = [], 0, 0
    while True:
        page = await srv._system_job_status_impl(
            job_id=job_id, view="detail", offset=offset, slice_offset=slice_offset, **params)
        pages.append(page)
        cursor = page.get("detail_page") or {}
        offset, slice_offset = cursor.get("next_offset"), cursor.get("next_slice_offset") or 0
        if offset is None:
            return join_carto_job_detail_pages(pages)


# ---------------------------------------------------------------------------
# Rentals: the few values two INDEPENDENT runs cannot share
# ---------------------------------------------------------------------------
# Both arms run at ONE environment path (see `_a1_arms`), so every path, hash and
# finding id in a row is the same bytes on both sides.  What remains is the
# naming of the arm's own job: the random job id and the clock stamps / pid in
# its artifact directory names.  Those are rentals -- real values that
# necessarily differ and that no production rule can make equal.  Everything
# else -- every key, every key order, every list order, ``None`` vs ``[]`` -- is
# compared exactly.
_ISO_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2})?")
_ARTIFACT_STAMP = re.compile(r"(?<!\d)\d{8}-\d{6}(?:-\d{6})?(?!\d)")
_ARTIFACT_PID = re.compile(r"_pid\d+")


def _row_rentals(arm):
    """One arm's known volatile values: its own random job id."""
    job_id = str(arm["record"].get("id") or "")
    return [(job_id, "<JOB>")] if job_id else []


def _canonical(value, rentals):
    """Substitute one arm's rentals in place of the values, never in place of keys.

    Recursive so the substitution reaches evidence nested inside findings,
    projections and receipts; a value that is not a string is returned as is, so
    a diff outside the rentals is still a diff.
    """
    if isinstance(value, dict):
        return {key: _canonical(item, rentals) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical(item, rentals) for item in value]
    if isinstance(value, str):
        for old, new in rentals:
            value = value.replace(old, new)
        value = _ISO_CLOCK.sub("<TIMESTAMP>", value)
        value = _ARTIFACT_PID.sub("_pid<PID>", value)
        return _ARTIFACT_STAMP.sub("<STAMP>", value)
    return value


def _comparable_rows(arm):
    """One arm's detail rows with that arm's rentals canonicalised."""
    return _canonical(arm["record"]["result_summary"]["files"], _row_rentals(arm))


def _compare_arms(oracle, live, *, label=""):
    """The independent row differential: the fc79975 arm against today's arm.

    D3-1's documented outline marker is applied to BOTH arms through the
    committed builder's own independent ``marker_row`` (never the production
    marker): the transform itself is never a differential, and its count/digest
    keeps the planned outline's evidence, so a real outline difference still
    fails.  Every other value must be equal -- ``node_outcomes`` list order and
    ``None``/``[]`` included.  A1 (iii)-(v) reject no node, so nothing here is
    weakened for the CF94 rejection witnesses.
    """
    builder = _builder()
    want = [builder.marker_row(row) for row in _comparable_rows(oracle)]
    got = [builder.marker_row(row) for row in _comparable_rows(live)]
    assert want, f"{label}: the oracle arm produced no rows"
    _assert_rows_equal(want, got, label=label)
    return want, got


async def _run_bulk(env, root, children):
    """Run one real bulk job over the frozen visible tree; returns ``(job_id, terminal)``."""
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    async with cache_operation_boundary(env.client, "j:golden-bulk-front"):
        ack = await srv._start_carto_bulk_visible_apply_job(
            root_uuid=env.ids["file"], mode="file",
            visible_tree_payload={"root": root, "children": children},
        )
    assert ack["success"], ack
    await asyncio.wait_for(env.tasks[ack["job_id"]], 60)
    assert not env.probe.violations, env.probe.violations
    return ack["job_id"], env.controls[ack["job_id"]].terminal_result


def _file_node(node_id, name, path_name):
    return {"id": node_id, "name": name, "note": f"Path: {path_name}"}


# ---------------------------------------------------------------------------
# The oracle arm: the frozen fc79975 bulk runner, installed for one arm only
# ---------------------------------------------------------------------------
G5_BASELINE = FIXTURE_ROOT / "g5_baseline.json"
_G5: dict[str, object] = {}

# The bulk family's own definitions: start, run, per-file loop, the two record
# writers and the two terminal-payload helpers.  The markdown family's frozen
# definitions are in the same baseline but A1 runs no markdown job, so they are
# deliberately NOT installed (and nothing else of today's code is replaced).
ORACLE_SERVER_DEFINITIONS = (
    "_start_carto_bulk_visible_apply_job",
    "_run_carto_bulk_visible_apply_job",
    "_bulk_apply_per_file_inner_loop",
    "_write_bulk_job_artifacts",
    "_write_carto_job_payload",
    "_carto_task_terminal_payload",
    "_carto_terminal_payload_data",
)
ORACLE_GOVERNANCE_DEFINITIONS = ("admit_carto_job_locked",)


def _g5_baseline():
    """The frozen oracle definitions, read once from the committed baseline."""
    if "document" not in _G5:
        document = json.loads(G5_BASELINE.read_text(encoding="utf-8"))
        assert len(document["commit"]) == 40, document["commit"]
        assert document["modules"], "the G5 baseline carries no modules"
        _G5["document"] = document
    return _G5["document"]


def _frozen_definition(owner, module, name):
    """One exact frozen `def`, compiled from the baseline's own text.

    The source is the fixture's copy, never a read of today's file, so editing
    the working tree cannot move the oracle.  ``globals`` is the live module, so
    the frozen body still reaches the collaborators the module binds at call
    time -- and, because the whole bulk set is installed, the other frozen
    members.  The definition's own digest is verified before it is compiled.
    """
    document = _g5_baseline()
    definition = document["modules"][module]["definitions"][name]
    source = definition["source"]
    assert hashlib.sha256(source.encode("utf-8")).hexdigest() == definition["sha256"], (
        f"{module}.{name}: frozen source drifted from its baseline digest")
    namespace: dict[str, object] = {}
    exec(compile(source, f"{document['commit']}:{module}.{name}", "exec"), vars(owner), namespace)
    function = namespace[name]
    assert callable(function), f"{module}.{name}: the frozen source bound no function"
    return function


def install_frozen_bulk_runner(patch):
    """Bind ONLY the frozen bulk runner/writers and the governance admission.

    ``patch`` is the arm's own ``pytest.MonkeyPatch`` context, so the install is
    undone with that arm and today's definitions are live again for the next
    arm.  Returns ``{module.name: co_filename}``: the compiled origin of every
    installed definition, which is how the test proves the oracle arm ran the
    frozen source rather than the working tree's.
    """
    installed = {}
    for name in ORACLE_SERVER_DEFINITIONS:
        function = _frozen_definition(srv, "workflowy_mcp.server", name)
        patch.setattr(srv, name, function)
        installed[f"workflowy_mcp.server.{name}"] = function.__code__.co_filename
    for name in ORACLE_GOVERNANCE_DEFINITIONS:
        function = _frozen_definition(gov, "workflowy_mcp.carto_job_governance", name)
        patch.setattr(gov, name, function)
        installed[f"workflowy_mcp.carto_job_governance.{name}"] = function.__code__.co_filename
    return installed


# The auto-beacon suffix is drawn at random by `allocate_beacon_id`, and it lands
# in the beacon block itself -- so it reaches the file's sha256, the block hashes
# and every finding fingerprint.  The draw is scenario INPUT, not runner
# behaviour, so both arms draw from this one fixed sequence and a random suffix
# can never masquerade as a runner difference.  `allocate_beacon_id`'s own
# deterministic collision fallback still applies if the sequence is exhausted.
AUTO_BEACON_SUFFIXES = ("a1b2", "c3d4", "e5f6", "g7h8", "i9j0", "k1l2", "m3n4", "o5p6",
                        "q7r8", "s9t0", "u1v2", "w3x4", "y5z6", "a7b8", "c9d0", "e1f2")


def _deterministic_auto_beacon_ids(patch):
    """Give both arms the same auto-beacon suffixes."""
    draws = iter(AUTO_BEACON_SUFFIXES)
    patch.setattr(nmc, "_generate_auto_beacon_hash", lambda: next(draws, "z0z0"))


@contextlib.asynccontextmanager
async def _isolated_environment(root):
    """One `real_job_environment`, built from the harness fixture's own body.

    Two arms run in one test, so the fixture cannot supply them: each arm gets a
    separate instance -- its own client, graph, jobs directory and registry at
    the path it is given -- and the first arm's patches are fully undone before
    the second instance is built.  Nothing is reimplemented: both the runtime
    scrub and the environment are the harness's own generated fixtures.
    """
    root.mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as patch:
        runtime = scrubbed_runtime.__wrapped__(patch, root)
        environment = real_job_environment.__wrapped__(runtime, patch)
        env = await environment.__anext__()
        try:
            yield env
        finally:
            await environment.aclose()


async def _arm(root, scenario, *, oracle=False):
    """Run one scenario as one arm; returns the arm's captured evidence."""
    async with _isolated_environment(root) as env:
        with pytest.MonkeyPatch.context() as patch:
            installed = install_frozen_bulk_runner(patch) if oracle else {}
            _deterministic_auto_beacon_ids(patch)
            record, terminal, extras = await scenario(env, patch)
        return {
            "arm": "fc79975" if oracle else "live",
            "installed": installed,
            "ids": dict(env.ids),
            "root": str(env.path),
            "repo": str(env.repo),
            "source_row": str((env.repo / "source.py").resolve()),
            "record": record,
            "terminal": terminal,
            **extras,
        }


async def _a1_arms(tmp_path, scenario):
    """Run one scenario as the frozen arm first, then as the live arm.

    Both arms run at ONE environment path, one after the other, with the first
    arm's tree removed before the second builds its own -- the same reuse the G4
    file differential makes of one directory.  A row carries its file path as
    evidence (``finding_id`` hashes the canonical file, ``file_sha256`` hashes
    the bytes, ``canonical_file`` names it), so two different roots could only be
    compared by erasing evidence.  The same path over pristine bytes leaves the
    arms differing in the runner that produced the row and in nothing else.
    """
    root = tmp_path / "environment"
    oracle = await _arm(root, scenario, oracle=True)
    shutil.rmtree(root)
    live = await _arm(root, scenario)
    return oracle, live


def _assert_oracle_arm(oracle, live):
    """The oracle arm really was the frozen runner, and only that arm was."""
    assert oracle["arm"] == "fc79975" and live["arm"] == "live"
    assert live["installed"] == {}, "the live arm must run today's definitions"
    assert oracle["record"]["record_layout"] == "summary-head/1"
    assert live["record"]["record_layout"] == "summary-head/2", "live bulk admission/writer was not switched"
    commit = _g5_baseline()["commit"]
    expected = {f"workflowy_mcp.server.{name}" for name in ORACLE_SERVER_DEFINITIONS} | {
        f"workflowy_mcp.carto_job_governance.{name}" for name in ORACLE_GOVERNANCE_DEFINITIONS}
    assert set(oracle["installed"]) == expected, sorted(oracle["installed"])
    for name, filename in oracle["installed"].items():
        assert filename == f"{commit}:{name}", (name, filename)


# ---------------------------------------------------------------------------
# The three A1 scenarios, each runnable as either arm
# ---------------------------------------------------------------------------
async def _scenario_a1_iii(env, patch):
    """A1(iii): B refreshed by its own group, then finalized by another group."""
    root = _file_node(env.ids["file"], "A source.py", "source.py")
    other = _file_node(env.ids["other-file"], "B other.py", "other.py")
    job_id, terminal = await _run_bulk(env, root, [
        {"id": env.ids["symbol"], "name": "existing #j3", "note": "AST_QUALNAME: existing"},
        dict(other, children=[
            # same node id, different slice tag -> a real second write, into B
            {"id": env.ids["symbol"], "name": "existing #j4", "note": "AST_QUALNAME: existing"},
        ]),
    ])
    return await _joined_complete_detail(job_id), terminal, {}


async def _scenario_a1_iv(env, patch):
    """A1(iv): another group writes into B, then B's own group is cancelled inside."""
    root = _file_node(env.ids["other-file"], "A other.py", "other.py")
    own = _file_node(env.ids["file"], "B source.py", "source.py")
    cancelled: list[str] = []
    writer = acn.WorkFlowyClientNexus.update_beacon_from_node

    async def cancelling(*args, **kwargs):
        from workflowy_mcp.carto_job_runtime import current_carto_job_context
        node_id = str(kwargs.get("node_id") or (args[0] if args else ""))
        if node_id == env.ids["obsolete"] and not cancelled:
            cancelled.append(node_id)
            context = current_carto_job_context()
            assert context is not None
            context.job_control.request_user_cancel()
        return await writer(*args, **kwargs)

    patch.setattr(acn.WorkFlowyClientNexus, "update_beacon_from_node", cancelling)
    job_id, terminal = await _run_bulk(env, root, [
        # other.py's group runs first and writes a node that lives in source.py
        {"id": env.ids["symbol"], "name": "existing #j3", "note": "AST_QUALNAME: existing"},
        dict(own, children=[
            # a real symbol (``added``) on the seeded id, so only the cancel stops it
            {"id": env.ids["obsolete"], "name": "added #j3",
             "note": "AST_QUALNAME: added"},
        ]),
    ])
    return await _joined_complete_detail(job_id), terminal, {"cancelled": cancelled}


async def _scenario_a1_v(env, patch):
    """A1(v): every candidate passed the server's in-sync filter (before the writer)."""
    path = env.repo / "source.py"
    nmc.update_beacon_from_node_python(str(path), "existing #j3", "AST_QUALNAME: existing")
    mapped = [node for node in nmc.parse_file_outline(str(path))
              if node.get("ast_qualname") == "existing"]
    assert mapped, "the mapped outline must carry the synced symbol"
    candidate = {"id": env.ids["symbol"], "name": mapped[0]["name"], "note": mapped[0]["note"]}
    assert "#j3" in candidate["name"] and "slice_labels: j3" in candidate["note"]

    writer_calls: list[str] = []
    original = acn.WorkFlowyClientNexus.update_beacon_from_node

    async def observed(*args, **kwargs):
        writer_calls.append(str(kwargs.get("node_id") or (args[0] if args else "")))
        return await original(*args, **kwargs)

    patch.setattr(acn.WorkFlowyClientNexus, "update_beacon_from_node", observed)
    job_id, _terminal = await _run_bulk(
        env, _file_node(env.ids["file"], "A source.py", "source.py"), [candidate])
    return await _joined_complete_detail(job_id), _terminal, {"writer_calls": writer_calls}



# ---------------------------------------------------------------------------
# The A1 (iii)-(v) witnesses: one scenario, an fc79975 arm and a live arm
# ---------------------------------------------------------------------------
def _assert_a1_iii(arm):
    """A1(iii)'s witness assertions, evaluated on one arm's own detail rows."""
    record, ids = arm["record"], arm["ids"]
    row = next(r for r in record["result_summary"]["files"] if r["file"] == arm["source_row"])
    assert row["file_node_id"] == ids["file"], row.get("file_node_id")
    assert row["status"] in {"refreshed", "partial_section"}, row["status"]
    assert "refresh_outcome" in row, sorted(row)
    for counter in ("nodes_created", "nodes_updated", "nodes_moved", "nodes_deleted"):
        assert counter in row, (counter, sorted(row))
    assert len(row["node_outcomes"]) == 2, row["node_outcomes"]
    assert row["nodes_satisfied"] == sum(bool(receipt.get("satisfied"))
                                         for receipt in row["node_outcomes"])
    assert row["changed"] is True
    assert len(record["result_summary"]["files"]) >= 2, "two frozen groups ran"
    assert arm["terminal"]["status"] == record["status"]


def _assert_a1_iv(arm):
    """A1(iv)'s witness assertions, evaluated on one arm's own detail rows."""
    record, ids = arm["record"], arm["ids"]
    assert arm["cancelled"] == [ids["obsolete"]]
    assert arm["terminal"]["status"] == "cancelled", arm["terminal"].get("status")
    row = next((r for r in record["result_summary"]["files"]
                if r["file"] == arm["source_row"]), None)
    assert row is not None and row["node_outcomes"], row
    assert row.get("error") != "File attempt interrupted before completion", row
    assert "findings" in row, sorted(row)


def _assert_a1_v(arm):
    """A1(v)'s witness assertions, evaluated on one arm's own detail rows."""
    summary = arm["record"]["result_summary"]
    assert arm["writer_calls"] == [], arm["writer_calls"]
    row = next(r for r in summary["files"] if r["file"] == arm["source_row"])
    assert row["node_outcomes"] == [], row.get("node_outcomes")
    assert row["nodes_satisfied"] == 0
    assert row["changed"] is False
    assert int(summary.get("nodes_skipped_in_sync") or 0) >= 1, summary.get("nodes_skipped_in_sync")
    assert int(summary.get("files_skipped") or 0) >= 1
    assert not (summary.get("node_outcomes") or [])


# ---------------------------------------------------------------------------
# A1 (iii): own group refreshes, then another group finalizes the same file
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a1_iii_own_group_then_other_group_keeps_refresh_keys(tmp_path):
    """A1(iii): B's refresh keys survive the later group's receipts.

    B (source.py) is refreshed by its OWN group first.  The second group's
    candidate is the SAME stable node (``symbol``/``existing``) carrying a
    different #tag: the visible tree's frozen grouping places it under
    other.py while its bytes belong to source.py, which is the case
    ``sync_active_file`` attributes by the receipt's own path (§4 item 1
    "frozen GUI grouping").  A node that no longer exists on disk (``obsolete``)
    cannot serve here: B's own refresh deletes it from Workflowy, so the later
    group would target a stale id -- the earlier failure this witness replaces.

    Both arms run this same scenario in their own environment and are read
    through the joined detail view; the frozen arm is the row oracle.
    """
    oracle, live = await _a1_arms(tmp_path, _scenario_a1_iii)
    _assert_oracle_arm(oracle, live)
    for arm in (oracle, live):
        _assert_a1_iii(arm)
    _compare_arms(oracle, live, label="A1(iii)")


# ---------------------------------------------------------------------------
# A1 (iv): another group writes into B, then B's own group is cancelled inside
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a1_iv_other_group_then_own_group_cancelled_keeps_earlier_evidence(tmp_path):
    """A1(iv): no interrupted row unless today's runner writes one.

    Both arms cancel at the same writer call and are read through the joined
    detail view; the frozen arm is the row oracle.
    """
    oracle, live = await _a1_arms(tmp_path, _scenario_a1_iv)
    _assert_oracle_arm(oracle, live)
    for arm in (oracle, live):
        _assert_a1_iv(arm)
    _compare_arms(oracle, live, label="A1(iv)")


# ---------------------------------------------------------------------------
# A1 (v): every candidate passed the server's in-sync filter (before the writer)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a1_v_in_sync_file_hits_the_filter(tmp_path):
    """A1(v): the in-sync FILTER skips before the writer: ``[]`` and 0, no receipt.

    The filter is ``_prepare_bulk_candidate("filter")`` ->
    ``_node_name_tags_match_beacon_block_in_note``: it compares the candidate's
    NAME #tags with the ``slice_labels:`` of a real BEACON block in that same
    candidate's NOTE (role deliberately ignored).  It is not a disk/cache
    comparison, so the payload must carry the canonical mapped name+note for the
    disk state -- ``nexus_map_codebase.parse_file_outline`` after the disk beacon
    write, which is exactly what the refresh projects into Workflowy.

    Both arms build that payload from their own disk write and are read through
    the joined detail view; the frozen arm is the row oracle.
    """
    oracle, live = await _a1_arms(tmp_path, _scenario_a1_v)
    _assert_oracle_arm(oracle, live)
    for arm in (oracle, live):
        _assert_a1_v(arm)
    _compare_arms(oracle, live, label="A1(v)")



def test_a1_v_in_flight_row_without_receipts_has_zero_satisfied():
    """A1(v) as the fold sees an ``in_flight`` row: ``[]`` and 0, never ``null``."""
    builder = _builder()
    path = "E:\\work\\bulk\\in_sync_interrupted.py"
    document = {
        "id": "carto-bulk-apply-sync", "type": "CARTO_BULK_APPLY", "mode": "file",
        "root_uuid": "11111111-2222-3333-4444-555555555555", "status": "failed",
        "created_at": "2026-10-27T10:00:00.000000+00:00", "updated_at": "2026-10-27T10:01:00.000000",
        "progress": {"total_files": 1, "completed_files": 0, "current_phase": "running"},
        "result_summary": {"files": [], "errors": [], "node_outcomes": []},
    }
    in_flight = {"file": path, "file_node_id": "id-sync", "status": "failed", "changed": False,
                 "findings": [], "warnings": [], "events": [],
                 "error": "File attempt interrupted before completion", "base_lines": 0}
    sidecar = builder.build_sidecar(document, bulk=True)
    built = builder.build_record(document, sidecar, in_flight=in_flight)
    folded = _fold(built, "".join(sidecar["files_lines"]), None)
    assert folded["rows"]["files"], "the fold must represent the interrupted file"
    folded_row = folded["rows"]["files"][0]
    assert folded_row["node_outcomes"] == [] and folded_row["nodes_satisfied"] == 0, folded_row


# ---------------------------------------------------------------------------
# Frozen-source checks (no daemon)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fixture,source_name", [
    ("folder_refresh_trim", "folder_refresh_trim.record.json"),
    ("folder_refresh_trim", "folder_refresh_trim.legacy.record.json"),
    ("mixed_folder", "mixed_folder.record.json"),
])
def test_frozen_folder_fixture_stitches_to_its_source(fixture, source_name):
    path = _source(fixture, source_name)
    if not path.exists():
        pytest.skip(f"frozen source missing: {path.name}")
    builder = _builder()
    source = json.loads(path.read_text(encoding="utf-8"))
    record, files_text, nodes_text, _ = _counterpart(source)
    detail = stitch_carto_job_detail(record, _fold(record, files_text, nodes_text))
    assert list(detail["result_summary"]) != []
    for key, value in source["result_summary"].items():
        assert builder.compact(detail["result_summary"].get(key)) == builder.compact(value), key


@pytest.mark.parametrize("fixture,source_name", [
    ("bulk_receipts_synthetic", "bulk_receipts_synthetic.capture.json"),
    ("bulk_apply", "bulk_apply.capture.json"),
    ("a1_traces", "test_a1_iii_own_group_then_other_group_keeps_refresh_keys.capture.json"),
    ("a1_traces", "test_a1_iv_other_group_then_own_group_cancelled_keeps_earlier_evidence.capture.json"),
    ("a1_traces", "test_a1_v_in_sync_file_hits_the_filter.capture.json"),
])
def test_frozen_bulk_source_folds_to_its_captured_rows(fixture, source_name):
    path = _source(fixture, source_name)
    if not path.exists():
        pytest.skip(f"frozen source missing (capture owed by the joined harness run): {path.name}")
    document = json.loads(path.read_text(encoding="utf-8"))
    record, files_text, nodes_text, source = _counterpart(document)
    folded = _fold(record, files_text, nodes_text)
    stitched = stitch_carto_job_detail(record, folded)
    want = list(source["result_summary"].get("files") or [])
    got = list(stitched["result_summary"].get("files") or [])
    if isinstance(record.get("in_flight"), dict):
        interrupted = record["in_flight"]["file"]
        want = [row for row in want if row["file"] != interrupted]
        got = [row for row in got if row["file"] != interrupted]
    _assert_rows_equal(want, got, label=source_name)
    builder = _builder()
    interrupted = record["in_flight"]["file"] if isinstance(record.get("in_flight"), dict) else None
    for key in ("warnings", "events", "files_changed"):
        if key not in source["result_summary"]:
            continue
        got = stitched["result_summary"].get(key)
        if interrupted and isinstance(got, list):
            # the abrupt-end fixture represents a file the /1 record never held
            got = [value for value in got if value != interrupted]
        assert builder.compact(got) == builder.compact(source["result_summary"][key]), key


def test_frozen_bulk_sequence_never_rebuilds_a_finalized_row():
    """§4 item 1: a later finalization is a DELTA, and only a real finalization emits.

    The captured A1(iii) trace finalizes B twice (its own group, then the frozen
    grouping write from another group).  Its sidecar must hold one row line for B
    followed by delta lines, never a second full row; and no row line may come
    from a group-head or per-candidate write (the sequence's `cause`).
    """
    path = _source("a1_traces",
                   "test_a1_iii_own_group_then_other_group_keeps_refresh_keys.capture.json")
    if not path.exists():
        pytest.skip(f"capture trace not written yet: {path.name}")
    document = json.loads(path.read_text(encoding="utf-8"))
    builder = _builder()
    sidecar = builder.build_sidecar_from_sequence(
        document["records"], kind=str(document.get("kind") or "bulk"),
        aggregate_inputs=document.get("aggregate_inputs") or (),
    )
    seen, deltas = set(), 0
    for step in sidecar["sequence"]:
        if step["k"] == "row":
            assert step["file"] not in seen, f"rebuilt row for {step['file']}"
            assert step["cause"] in {"group-final", "stop"}
            seen.add(step["file"])
        elif step["k"] == "delta":
            deltas += 1
            assert step["file"] in seen, f"delta before its row line: {step['file']}"
            assert step["cause"] in {"group-final", "stop"}
    assert deltas >= 1, "A1(iii) finalizes its file twice; there must be a delta line"
    lines = [json.loads(line) for line in sidecar["files_lines"]]
    assert sum(1 for entry in lines if entry["k"] == "row" and "delta" not in entry) == len(seen)



def test_frozen_markdown_source_stitches_to_its_source():
    path = _source("markdown_generate", "markdown_generate.record.json")
    if not path.exists():
        pytest.skip(f"frozen source missing (capture owed by the J-4 joined run): {path.name}")
    builder = _builder()
    source = json.loads(path.read_text(encoding="utf-8"))
    record, files_text, nodes_text, _ = _counterpart(source)
    detail = stitch_carto_job_detail(record, _fold(record, files_text, nodes_text))
    # The contract pins deep JSON values and per-row field order, not the
    # position where a phase8 envelope's removed files key is reinserted.
    actual, expected = detail["result_summary"], source["result_summary"]
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
    for left, right in ((actual.get("files") or [], expected.get("files") or []),
                        ((actual.get("phase8") or {}).get("files") or [],
                         (expected.get("phase8") or {}).get("files") or [])):
        assert [list(row) for row in left] == [list(row) for row in right]


# ---------------------------------------------------------------------------
# The writer counterpart: ``--check``'s second half, proven without writing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fixture", [
    "folder_refresh_trim", "mixed_folder", "bulk_receipts_synthetic",
    "bulk_apply", "markdown_generate",
])
def test_committed_writer_counterpart_matches_the_independent_builder(fixture):
    """Q4: every fixture's committed ``writer.record.json`` is reproducible.

    ``build_fixtures.py --check`` verifies the counterpart bytes as well as the
    frozen G2 pair, without rewriting any original: this proves that half in
    memory.  The counterpart carries B5's exact ``warning_count`` and Q4(4)'s
    row-line hint, and the builder never imports the writer it predicts.
    """
    builder = _builder()
    text = builder.writer_counterpart_text(fixture)
    if text is None:
        pytest.skip(f"fixture source missing: {fixture}")
    path = FIXTURE_ROOT / fixture / "writer.record.json"
    assert path.exists(), f"no writer counterpart for {fixture}"
    assert path.read_text(encoding="utf-8") == text
    counterpart = json.loads(text)
    assert counterpart["warning_count"] == counterpart["result_summary"]["warnings_total"]
    assert counterpart["evidence"]["files"]["rows"] == builder._row_line_count(
        builder.fixture_inputs(fixture)["sidecar"])
    source = (FIXTURE_ROOT / "build_fixtures.py").read_text(encoding="utf-8")
    assert "import carto_job_record" not in source
    assert "from workflowy_mcp.carto_job_record" not in source
    assert "CartoJobRecord(" not in source


def test_committed_writer_counterpart_differs_only_by_q4_rules():
    """Q4: the counterpart corrects exactly the ruled values, nothing else.

    Every fixture's counterpart differs from the frozen reader record only by
    B5's ``warning_count``/``warnings`` head fields (a folder record carries
    neither), the Markdown row-line hint and the interrupted bulk summary that
    Q4(3) re-derives from the landed rows and receipts.  No other value, sidecar
    byte or original record may move -- the reader fixtures stay the frozen G2
    ones.
    """
    def flat(value, prefix=""):
        out = {}
        for key, item in value.items():
            if isinstance(item, dict):
                out.update(flat(item, f"{prefix}{key}."))
            else:
                out[f"{prefix}{key}"] = item
        return out

    expected_diffs = {
        "folder_refresh_trim": {"warning_count", "warnings"},
        "mixed_folder": {"warning_count", "warnings"},
        "bulk_receipts_synthetic": {
            "warning_count", "warnings", "result_summary.files_changed",
            "result_summary.files_changed_total", "result_summary.preview_totals.files_changed",
            "result_summary.successful", "result_summary.summary_line"},
        "markdown_generate": {"evidence.files.rows"},
        "bulk_apply": set(),
    }
    for fixture, allowed in expected_diffs.items():
        folder = FIXTURE_ROOT / fixture
        frozen = flat(json.loads((folder / "record.json").read_text(encoding="utf-8")))
        counterpart = flat(json.loads((folder / "writer.record.json").read_text(encoding="utf-8")))
        changed = {key for key in set(frozen) | set(counterpart)
                   if frozen.get(key, "<absent>") != counterpart.get(key, "<absent>")}
        assert changed <= allowed, (fixture, sorted(changed - allowed))


# ---------------------------------------------------------------------------
# The committed builder's own grammar (pure, no daemon): the /2 evidence
# contract's edge cases §3.1 pins for the golden fixtures.
# ---------------------------------------------------------------------------
def test_builder_empty_projection_round_trips_through_proj():
    """§2.6: an EMPTY projection mapping single-stores as ``projection: null`` + ``proj: []``."""
    builder = _builder()
    row = {"file": "E:\\work\\p.py", "value": 1, "projection": {}}
    stored, proj_keys = builder.store_row(row)
    assert proj_keys == [], proj_keys
    assert stored["projection"] is None
    assert builder.expand_row(stored, proj_keys) == row
    source = {"type": "CARTO_REFRESH", "mode": "file", "status": "completed",
              "result_summary": {"files": [row]}}
    sidecar = builder.build_sidecar(source)
    entry = json.loads(sidecar["files_lines"][0])
    assert entry.get("proj") == [], entry
    assert entry["row"]["projection"] is None


def test_builder_deep_equality_never_compacts_different_json_values():
    """§2.6 deep JSON equality: ``{}`` != ``[]`` and ``true`` != ``1`` (tagged kinds)."""
    builder = _builder()
    assert builder.same_json({"a": []}, {"a": {}}) is False
    assert builder.same_json({"a": True}, {"a": 1}) is False
    assert builder.same_json({"a": [1], "b": {"c": 2}}, {"b": {"c": 2}, "a": [1]}) is True
    # a projection that is NOT deep-equal to the row's value stays explicit
    row = {"file": "E:\\work\\e.py", "value": [], "projection": {"value": {}}}
    stored, proj_keys = builder.store_row(row)
    assert proj_keys is None
    assert stored["projection"] == {"value": {}}


def test_builder_presence_sets_keep_empty_present_paths_and_unknown_lists():
    """§2.2/§2.4: an empty run-level list keeps its presence through a `set` line.

    `present_paths` never enters the structured head; `nexusignore_sources`
    keeps its B8 bounded preview instead (so no duplicate set line).
    """
    builder = _builder()
    source = {"type": "CARTO_REFRESH", "mode": "folder", "status": "completed",
              "result_summary": {"files": [], "present_paths": [],
                                 "nexusignore_sources": [], "some_new_list": []}}
    sidecar = builder.build_sidecar(source)
    sets = [json.loads(line) for line in sidecar["files_lines"]
            if json.loads(line)["k"] == "set"]
    paths = {tuple(entry["key"]): entry["v"] for entry in sets}
    assert paths.get(("result_summary", "present_paths")) == []
    assert paths.get(("result_summary", "some_new_list")) == []
    assert ("result_summary", "nexusignore_sources") not in paths
    record = builder.build_record(source, sidecar)
    assert "present_paths" not in record["result_summary"]
    assert record["result_summary"]["nexusignore_sources"] == []


def _bulk_sequence_record(builder, job_rows, receipts, *, completed_files, status="running",
                          current_file=None, phase="update-beacons"):
    """One captured bulk record, its rows exactly the aggregate's copy of ``job_rows``."""
    return {
        "id": "carto-bulk-apply-fixture", "type": "CARTO_BULK_APPLY", "mode": "file",
        "status": status,
        "progress": {"total_files": len(job_rows), "completed_files": completed_files,
                     "current_file": current_file, "current_phase": phase},
        "result_summary": {"files": builder._aggregate_row_transform(job_rows),
                           "node_outcomes": list(receipts)},
    }


def _bulk_fixture_receipt(node, path):
    return {"file_path": path, "source_path": path, "node_id": node, "changed": False,
            "satisfied": True, "writer_success": True, "success": True, "noop_reason": None}


def test_builder_delta_emits_when_only_the_receipt_count_changes():
    """§4 item 1: a later finalization is a DELTA, even when only its count moved."""
    builder = _builder()
    path = "E:\\work\\bulk\\one.py"
    first_receipt = _bulk_fixture_receipt("n1", path)
    later_receipt = _bulk_fixture_receipt("n2", path)
    first = {"file": path, "file_node_id": "id-one", "status": "completed", "changed": True,
             "findings": [], "warnings": [], "events": [],
             "node_outcomes": [first_receipt], "nodes_satisfied": 1, "final_observation": True}
    later = dict(first, node_outcomes=[first_receipt, later_receipt], nodes_satisfied=2)
    records = [
        _bulk_sequence_record(builder, [first], [first_receipt], completed_files=1),
        _bulk_sequence_record(builder, [later], [first_receipt, later_receipt],
                              completed_files=2, status="completed_with_warnings"),
    ]
    sidecar = builder.build_sidecar_from_sequence(records)
    rows = [json.loads(line) for line in sidecar["files_lines"]
            if json.loads(line)["k"] == "row"]
    assert len(rows) == 2, rows
    assert "delta" not in rows[0]
    assert "delta" in rows[1]
    assert rows[1]["nodes"] == 2
    assert rows[1]["delta"]["unset"] == [] and not rows[1]["delta"]["extend"]
    assert [step["k"] for step in sidecar["sequence"]
            if step["k"] in ("row", "delta")] == ["row", "delta"]


def test_builder_keeps_a_job_set_check_target_and_drops_the_aggregate_default():
    """§4 item 1: a job-set ``check_target`` survives; the aggregate's default is not evidence."""
    builder = _builder()
    path = "E:\\work\\bulk\\target.py"
    job_set = {"verb": "check", "params": {"file_path": path, "min_severity": "warning"}}
    first = {"file": path, "file_node_id": "id-target", "status": "completed",
             "changed": False, "findings": [], "warnings": [], "events": [],
             "final_observation": True, "node_outcomes": [], "nodes_satisfied": 0}
    later = dict(first, final_observation=False, check_target=job_set, state="valid")
    records = [
        _bulk_sequence_record(builder, [first], [], completed_files=1),
        _bulk_sequence_record(builder, [later], [], completed_files=2,
                              status="completed_with_warnings"),
    ]
    sidecar = builder.build_sidecar_from_sequence(
        records, aggregate_inputs=[[first], [later]])
    rows = [json.loads(line) for line in sidecar["files_lines"]
            if json.loads(line)["k"] == "row"]
    assert "check_target" not in rows[0]["row"]
    assert rows[1]["row"].get("check_target") == job_set
