"""JR2 G3: ``CartoOutcomeAccumulator`` equals the folded batch at every prefix.

WaveSpec rev 3 ([ap:JUH8JU]) §2.7 and §3.2, the Q3 ruling [ap:K6GVNU] (Option A:
``put`` may pay O(size of row + p·log₂ n) on the in-memory ordered contribution
index) and the locked interface of [ap:47OHOV] working-log items 4–7.

The oracle is never the accumulator's own arithmetic:

* the *composition* keys are ``beacon_job_outcome(aggregate_beacon_outcomes(…))``
  run on the rows produced by the real evidence fold
  (``fold_carto_job_evidence``) over the same lines the accumulator was fed;
* the §2.2 aggregate keys come from the committed fixture builder's
  ``new_aggregates`` (``tests/fixtures/carto_job_records/build_fixtures.py``),
  imported by path and never used by production code.

Every test fails before this change for one reason: ``CartoOutcomeAccumulator``
did not exist -- the batch functions are the only composition, and there was no
incremental form to compare them with, no per-key summary, no ordered index and
no receipt tally that outlives a row.  Nothing here is timing: the index cost is
counted as node visits, memory as live weak references.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import random
import weakref
from pathlib import Path

import pytest

from workflowy_mcp import beacon_refresh_support as support
from workflowy_mcp import carto_job_record as record

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "carto_job_records"
SLIM_FINDING_KEYS = ("code", "severity", "message", "file", "line", "finding_id")
#: The batch's list values, which §2.7 excludes from ``outcome`` (its previews
#: replace ``files_changed`` and ``findings``; the rest belong to the writer).
LIST_VALUES = frozenset({"files", "findings", "warnings", "events", "files_changed", "phase8"})

_BUILDER: dict[str, object] = {}


def _builder():
    """The committed fixture builder, imported by path (a test asset, no writes)."""
    if "module" not in _BUILDER:
        path = FIXTURES / "build_fixtures.py"
        spec = importlib.util.spec_from_file_location("carto_fixture_builder_accumulator", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _BUILDER["module"] = module
    return _BUILDER["module"]


# ---------------------------------------------------------------------------
# The evidence lines the fold reads (the same lines the accumulator is fed)
# ---------------------------------------------------------------------------

def _compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _row_line(row, *, section="files", nodes_reflected=None, delta=None):
    line = {"k": "row", "sec": section, "row": row}
    if nodes_reflected is not None:
        line["nodes"] = int(nodes_reflected)
    if delta is not None:
        line["delta"] = delta
    return _compact(line) + "\n"


def _node_line(file, receipt):
    return _compact({"k": "node", "file": file, "row": receipt}) + "\n"


def _envelope_lines(envelope):
    if envelope is None:
        return []
    head = {key: value for key, value in envelope.items() if key != "files"}
    lines = [_compact({"k": "set", "key": "phase8_envelope", "v": head}) + "\n"]
    for row in envelope.get("files") or ():
        lines.append(_compact({"k": "row", "sec": "phase8", "row": row}) + "\n")
    return lines


def _oracle(files_lines, nodes_lines, *, run_warnings=(), run_events=(),
            infrastructure_error=None, cancelled=False):
    """The batch composition on the folded rows, plus the §2.2 aggregate keys."""
    folded = record.fold_carto_job_evidence("".join(files_lines), nodes="".join(nodes_lines))
    rows = folded["rows"].get("files", [])
    envelope = copy.deepcopy((folded.get("values") or {}).get("phase8_envelope"))
    if folded["rows"].get("phase8"):
        envelope = dict(envelope or {})
        envelope["files"] = copy.deepcopy(folded["rows"]["phase8"])
    aggregate = support.aggregate_beacon_outcomes(rows, warnings=run_warnings,
                                                  events=run_events)
    job = support.beacon_job_outcome(aggregate, infrastructure_error=infrastructure_error,
                                     cancelled=cancelled, phase8=envelope)
    expected = {key: value for key, value in job.items() if key not in LIST_VALUES}
    # ``build_record`` feeds the builder the rows as the aggregate presents them
    # (a legacy record's ``files`` already carry the aggregate's own defaults).
    expected.update(_builder().new_aggregates(
        job, aggregate["files"], node_receipts=folded["node_outcomes"], phase8=envelope))
    expected["files_changed"] = list(job["files_changed"])[:50]
    expected["findings_preview"] = [
        {key: finding.get(key) for key in SLIM_FINDING_KEYS}
        for finding in job["findings"][:20]]
    expected["preview_totals"] = {"files_changed": len(job["files_changed"]),
                                  "findings_preview": job["findings_total"]}
    return expected, folded


def _assert_every_variant(acc, files_lines, nodes_lines, *, run_warnings=(), run_events=()):
    """Every prefix is compared with and without a stop, as §2.7's MUST says."""
    for infrastructure_error, cancelled in ((None, False), ({"kind": "boom"}, False),
                                            (None, True)):
        expected, _folded = _oracle(files_lines, nodes_lines, run_warnings=run_warnings,
                                    run_events=run_events,
                                    infrastructure_error=infrastructure_error,
                                    cancelled=cancelled)
        actual = acc.outcome(infrastructure_error=infrastructure_error,
                             cancelled=cancelled)
        differing = sorted(key for key in set(actual) | set(expected)
                           if actual.get(key) != expected.get(key))
        assert not differing, differing
        assert set(actual) == set(expected)


# ---------------------------------------------------------------------------
# Seeded row generator (§3.2: sampled fixture rows plus synthetics)
# ---------------------------------------------------------------------------

CODES = ("empty_comment", "legacy_field", "writer_target_ambiguous")
SEVERITIES = ("error", "warning", "info")
STATUSES = ("refreshed", "completed", "partial_section", "failed", "skipped",
            "error", "unavailable", "parse_failed", None)
FILES = tuple(f"E:\\work\\job\\file_{index}.py" for index in range(6))


def _finding(rng, file):
    return {"file": file, "line": rng.randrange(1, 40), "code": rng.choice(CODES),
            "severity": rng.choice(SEVERITIES), "message": f"m{rng.randrange(3)}",
            "finding_id": f"f-{rng.randrange(1000)}", "detail": {"x": 1}}


def _warning(rng):
    shape = rng.randrange(4)
    if shape == 0:
        return {"type": rng.choice(("stale", "disk", "parse")), "message": "w"}
    if shape == 1:
        return {"code": rng.choice(("c1", "c2")), "message": "w"}
    if shape == 2:
        return {"reason": "r"}
    return {"message": "bare"}


def _make_row(rng, file):
    row = {"file": file}
    if rng.random() < 0.85:
        row["status"] = rng.choice(STATUSES)
    if rng.random() < 0.3:
        row["error"] = rng.choice((None, "", "boom"))
    if rng.random() < 0.6:
        row["changed"] = rng.random() < 0.5
    row["findings"] = [_finding(rng, file) for _ in range(rng.randrange(4))]
    row["warnings"] = [_warning(rng) for _ in range(rng.randrange(3))]
    row["events"] = [{"type": "ev"} for _ in range(rng.randrange(3))]
    if rng.random() < 0.3:
        row["omissions"] = [{"type": "om"} for _ in range(rng.randrange(2))]
    if rng.random() < 0.3:
        row["relocations"] = [{"type": "rl"} for _ in range(rng.randrange(2))]
    if rng.random() < 0.25:
        row["local_cpu_failure"] = {"phase": "file:source", "pid": rng.randrange(3)}
    if rng.random() < 0.35:
        row["nodes_satisfied"] = rng.randrange(3)
    return row


def _delta_for(rng, file):
    unset, extend, row = [], {}, {"file": file}
    if rng.random() < 0.5:
        unset.append(rng.choice(("status", "changed", "findings", "warnings", "events",
                                 "nodes_satisfied", "error", "local_cpu_failure")))
    if rng.random() < 0.6:
        row["status"] = rng.choice(("refreshed", "failed", "completed"))
    if rng.random() < 0.4:
        row["changed"] = rng.random() < 0.5
    if rng.random() < 0.3:
        row["error"] = rng.choice((None, "late error"))
    if rng.random() < 0.4:
        row["findings"] = [_finding(rng, file) for _ in range(rng.randrange(3))]
    if rng.random() < 0.4:
        extend["findings"] = [_finding(rng, file) for _ in range(rng.randrange(3))]
    if rng.random() < 0.4:
        extend["warnings"] = [_warning(rng)]
    if rng.random() < 0.3:
        extend["events"] = [{"type": "ev"}]
    if rng.random() < 0.2:
        row["nodes_satisfied"] = 1
    return {"unset": unset, "extend": extend}, row


def _receipt_for(rng, file):
    receipt = {"file_path": file, "node_id": f"n{rng.randrange(4)}",
               "satisfied": rng.random() < 0.6, "changed": rng.random() < 0.4,
               "success": rng.random() > 0.2}
    if not receipt["success"]:
        receipt["error"] = "Node write not satisfied"
    return receipt


def _phase8_envelope(rng):
    if rng.random() < 0.25:
        return None
    labels = rng.sample(FILES + ("E:\\work\\job\\new_phase8.py",), rng.randrange(1, 4))
    envelope = {
        "execution": "inline",
        "files": [{"file": file, "status": rng.choice(STATUSES),
                   "findings": [_finding(rng, file) for _ in range(rng.randrange(3))],
                   "warnings": [_warning(rng)] if rng.random() < 0.5 else []}
                  for file in labels],
        "warnings": [_warning(rng)] if rng.random() < 0.5 else [],
    }
    if rng.random() < 0.3:
        envelope["success"] = False
        envelope["error"] = "phase 8 failed"
    return envelope


def _random_sequence(seed, steps):
    rng = random.Random(seed)
    run_warnings = [_warning(rng)] if rng.random() < 0.4 else []
    run_events = [{"type": "run"}] if rng.random() < 0.3 else []
    acc = support.CartoOutcomeAccumulator(warnings=run_warnings, events=run_events)
    files_lines, nodes_lines, phase8_lines = [], [], []
    for _ in range(rng.randrange(2)):
        value = _warning(rng)
        run_warnings.append(value)
        acc.add_run_warning(value)
    for _ in range(rng.randrange(2)):
        value = {"type": "run-event"}
        run_events.append(value)
        acc.add_run_event(value)
    for _step in range(steps):
        choice = rng.random()
        if choice < 0.35:
            row = _make_row(rng, rng.choice(FILES))
            nodes = rng.randrange(3) if rng.random() < 0.5 else None
            files_lines.append(_row_line(row, nodes_reflected=nodes))
            acc.put(row, nodes_reflected=nodes)
        elif choice < 0.55:
            delta, row = _delta_for(rng, rng.choice(FILES))
            nodes = rng.randrange(3) if rng.random() < 0.7 else None
            files_lines.append(_row_line(row, nodes_reflected=nodes, delta=delta))
            acc.put(row, delta=delta, nodes_reflected=nodes)
        elif choice < 0.7:
            file = rng.choice(FILES + ("E:\\work\\job\\orphan.py",))
            receipt = _receipt_for(rng, file)
            nodes_lines.append(_node_line(file, receipt))
            acc.put_nodes([{"file": file, "row": receipt}])
        elif choice < 0.85:
            file = rng.choice(FILES + ("E:\\work\\job\\new_phase8.py",))
            if rng.random() < 0.5:
                delta, row = _delta_for(rng, file)
            else:
                delta, row = None, _make_row(rng, file)
            phase8_lines.append(_row_line(row, section="phase8", delta=delta))
            acc.put(row, section="phase8", delta=delta)
        else:
            envelope = _phase8_envelope(rng)
            acc.set_phase8(envelope)
            phase8_lines = _envelope_lines(envelope)
        yield acc, files_lines, phase8_lines, nodes_lines, run_warnings, run_events


# ---------------------------------------------------------------------------
# §2.7's equality MUST, at every prefix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5, 6, 7])
def test_every_prefix_equals_the_folded_batch(seed):
    """Each prefix's ``outcome`` equals the batch on the folded rows (§2.7 MUST).

    Fails before the change: there is no accumulator, and no per-key summary or
    ordered index exists to hold a prefix's composition without its rows.
    """
    steps = 0
    for acc, files_lines, phase8_lines, nodes_lines, run_warnings, run_events in (
            _random_sequence(seed, 40)):
        _assert_every_variant(acc, files_lines + phase8_lines, nodes_lines,
                              run_warnings=run_warnings, run_events=run_events)
        steps += 1
    assert steps == 40


def test_phase8_rows_stream_around_a_metadata_only_envelope_at_every_prefix():
    """G5's real interface: rows are puts, never hidden inside the envelope."""
    acc = support.CartoOutcomeAccumulator()
    files, phases = [], []
    primary = {"file": "/repo/a.py", "status": "completed", "findings": [
        {"code": "old", "severity": "info", "message": "before"}]}
    acc.put(primary)
    files.append(_row_line(primary))
    _assert_every_variant(acc, files, [])
    envelope = {"execution": "inline", "success": False, "error": "follow-up failed"}
    acc.set_phase8(envelope)
    phases.append(_compact({"k": "set", "key": "phase8_envelope", "v": envelope}) + "\n")
    _assert_every_variant(acc, files + phases, [])
    for row in (
        {"file": "/repo/new.py", "status": "failed", "error": "new", "findings": [
            {"code": "new", "severity": "error"}], "warnings": [{"message": "kept"}]},
        {"file": "/repo/a.py", "status": "completed", "findings": [], "warnings": [
            {"type": "followup"}]},
        {"file": "/repo/new.py", "status": "completed", "findings": []},
    ):
        acc.put(row, section="phase8")
        phases.append(_row_line(row, section="phase8"))
        _assert_every_variant(acc, files + phases, [])
    delta = {"unset": ["status"], "extend": {"warnings": [{"type": "late"}]}}
    row = {"file": "/repo/a.py"}
    acc.put(row, section="phase8", delta=delta)
    phases.append(_row_line(row, section="phase8", delta=delta))
    _assert_every_variant(acc, files + phases, [])
    assert acc.outcome()["phase8_summary"]["files_total"] == 2


def test_an_empty_selection_is_the_batchs_empty_selection():
    """No rows: the batch's ``failed`` selection, which the caller overrides.

    Fails before the change: no accumulator exists to hold the empty selection.
    """
    acc = support.CartoOutcomeAccumulator()
    expected, _folded = _oracle([], [])
    assert acc.outcome() == expected
    assert acc.outcome()["status"] == "failed"           # save_summary's rule is the caller's
    assert acc.outcome()["files_changed"] == []
    assert acc.outcome()["preview_totals"] == {"files_changed": 0, "findings_preview": 0}


# ---------------------------------------------------------------------------
# Frozen fixture rows: the real bulk evidence, replayed put by put
# ---------------------------------------------------------------------------

def test_rows_sampled_from_a_captured_folder_refresh_compose_like_the_fold():
    """Real captured rows (with their own findings, events and warnings) compose.

    Fails before the change: the captured rows had no summary form to compose
    without being held.
    """
    source = json.loads((FIXTURES / "mixed_folder" / "sources"
                         / "mixed_folder.record.json").read_text(encoding="utf-8"))
    rows = [row for row in (source.get("result_summary") or {}).get("files") or []
            if isinstance(row, dict)]
    assert len(rows) >= 3
    sampled = rows[:2] + [next(row for row in rows if row.get("findings"))]
    acc = support.CartoOutcomeAccumulator()
    files_lines = []
    for row in sampled:
        files_lines.append(_row_line(row))
        acc.put(row)
    _assert_every_variant(acc, files_lines, [])
    # A replacement and a delta over the same captured keys, then a full replay.
    replacement = dict(sampled[0])
    replacement["changed"] = not replacement.get("changed")
    delta = {"unset": ["warnings"], "extend": {"findings": sampled[-1]["findings"][:1]}}
    files_lines.append(_row_line(replacement))
    acc.put(replacement)
    _assert_every_variant(acc, files_lines, [])
    files_lines.append(_row_line({"file": sampled[1]["file"]}, delta=delta))
    acc.put({"file": sampled[1]["file"]}, delta=delta)
    _assert_every_variant(acc, files_lines, [])
    # The delta replaced that key's warnings with nothing and appended its finding.
    assert acc.outcome()["findings_total"] == len(sampled[2].get("findings") or []) + 1


def test_the_frozen_bulk_evidence_replays_to_the_same_outcome():
    """The committed bulk fixture's own lines, replayed (receipts, deltas, base row).

    Fails before the change: nothing tallied receipts per key or kept a delta
    overlay without holding the row.
    """
    folder = FIXTURES / "bulk_receipts_synthetic"
    files_text = (folder / "files.jsonl").read_text(encoding="utf-8")
    nodes_text = (folder / "nodes.jsonl").read_text(encoding="utf-8")
    acc = support.CartoOutcomeAccumulator()
    for raw in files_text.splitlines():
        line = json.loads(raw)
        acc.put(line["row"], section=line.get("sec") or "files",
                delta=line.get("delta"), nodes_reflected=line.get("nodes"))
    for raw in nodes_text.splitlines():
        line = json.loads(raw)
        acc.put_nodes([{"file": line["file"], "row": line["row"]}])
    expected, folded = _oracle([files_text], [nodes_text])
    assert acc.outcome() == expected
    # The receipt-only key's base row follows every row line, as the fold places it.
    assert [row["file"] for row in folded["rows"]["files"]][-1].endswith("interrupted.py")
    assert acc.receipt_tally("E:\\work\\bulk\\interrupted.py") == {
        "count": 1, "satisfied": 1, "changed": True, "rejected_error": None}
    assert acc.reflected_nodes("E:\\work\\bulk\\interrupted.py") == 0


# ---------------------------------------------------------------------------
# Receipts: order, tallies that outlive a row, reflected counts
# ---------------------------------------------------------------------------

def test_receipt_only_base_rows_follow_every_primary_row_even_received_first():
    """A receipt that arrives first still yields a base row after every row line.

    Fails before the change: there was no base-row summary and no receipt tally
    to place one.
    """
    early, late = "E:\\work\\job\\early.py", "E:\\work\\job\\late.py"
    acc = support.CartoOutcomeAccumulator()
    node_lines = [_node_line(early, {"node_id": "e1", "satisfied": True, "changed": True,
                                     "success": True})]
    acc.put_nodes([{"file": early, "row": {"node_id": "e1", "satisfied": True,
                                           "changed": True, "success": True}}])
    files_lines = []
    for file in (late, "E:\\work\\job\\third.py"):
        row = {"file": file, "status": "refreshed", "changed": True, "findings": []}
        files_lines.append(_row_line(row))
        acc.put(row)
    expected, folded = _oracle(files_lines, node_lines)
    assert [row["file"] for row in folded["rows"]["files"]][-1] == record.canonical_carto_file(early)
    assert acc.outcome() == expected
    assert acc.outcome()["files_changed"] == [
        late, "E:\\work\\job\\third.py", record.canonical_carto_file(early)]


def test_a_receipt_tally_is_cumulative_and_survives_replacement_and_unsets():
    """Tally and reflected count are read-only for G5 and never reset by a put.

    Fails before the change: nothing kept a tally past the row that reflected it.
    """
    file = "E:\\work\\job\\bulk.py"
    acc = support.CartoOutcomeAccumulator()
    acc.put({"file": file, "status": "refreshed", "changed": False, "findings": [],
             "nodes_satisfied": 1}, nodes_reflected=1)
    receipts = [
        {"node_id": "n1", "satisfied": True, "changed": True, "success": True},
        {"node_id": "n2", "satisfied": False, "success": False,
         "error": "Node write not satisfied"},
    ]
    acc.put_nodes([{"file": file, "row": receipt} for receipt in receipts])
    assert acc.receipt_tally(file) == {"count": 2, "satisfied": 1, "changed": True,
                                       "rejected_error": "Node write not satisfied"}
    assert acc.reflected_nodes(file) == 1
    assert acc.outcome()["failed"] == 1
    # A later replacement line that reflects the whole tally does not reset it.
    acc.put({"file": file, "status": "refreshed", "changed": True, "findings": [],
             "nodes_satisfied": 1}, nodes_reflected=2)
    assert acc.receipt_tally(file)["count"] == 2
    assert acc.reflected_nodes(file) == 2
    assert acc.outcome()["failed"] == 0
    assert acc.outcome()["changed"] is True
    # A delta line's unset touches the row part only; the tally is unchanged.
    acc.put({"file": file, "changed": False, "findings": []},
            delta={"unset": ["status"], "extend": {}}, nodes_reflected=2)
    assert acc.receipt_tally(file)["count"] == 2
    assert acc.reflected_nodes(file) == 2
    # Mutating the caller's receipt object afterwards changes nothing.
    receipts[0]["changed"] = False
    assert acc.receipt_tally(file)["count"] == 2


def test_a_receipt_is_not_retained_by_the_accumulator():
    """A receipt object is dead after ``put_nodes`` (§2.7's no-evidence rule).

    Fails before the change: no receipt tally existed, so no such promise did.
    """
    receipt = _WeakRow({"node_id": "n1", "satisfied": True, "changed": True,
                        "success": True})
    alive = weakref.ref(receipt)
    acc = support.CartoOutcomeAccumulator()
    acc.put_nodes([{"file": "E:\\work\\job\\r.py", "row": receipt}])
    del receipt
    assert alive() is None


def test_a_delta_recomputes_only_the_parts_whose_source_keys_it_touches(monkeypatch):
    """An overlay keeps every part the line does not name (§2.7's source keys).

    Fails before the change: no per-part contribution existed to keep.
    """
    file = "E:\\work\\job\\delta.py"
    acc = support.CartoOutcomeAccumulator()
    acc.put({"file": file, "status": "refreshed", "changed": True,
             "findings": [{"code": "kept", "severity": "warning"}],
             "warnings": [{"type": "stale"}],
             "events": [{"type": "ev"}], "omissions": [{"type": "om"}]})
    key = acc._keys[("files", record.canonical_carto_file(file))]
    before = key.findings
    with monkeypatch.context() as scoped:
        for name, untouched in (("_part_add", before), ("_part_sub", before),
                                ("_warning_part_add", key.warnings), ("_warning_part_sub", key.warnings)):
            original = getattr(support, name)
            def guarded(target, part, original=original, untouched=untouched):
                assert part is not untouched, "delta revisited an untouched contribution"
                return original(target, part)
            scoped.setattr(support, name, guarded)
        acc.put({"file": file, "changed": False}, delta={"unset": [], "extend": {}})
    assert key.findings is before                    # not recomputed, not copied
    assert key.warnings is not None and key.warnings["count"] == 1
    assert key.events == {"events": 1, "omissions": 1, "relocations": 0}
    assert acc.outcome()["findings_total"] == 1
    assert acc.outcome()["events_total"] == 2
    assert acc.outcome()["findings_preview"][0]["code"] == "kept"
    # An extend of findings keeps the earlier entries and appends the new ones.
    acc.put({"file": file}, delta={"unset": [], "extend": {
        "findings": [{"code": "late", "severity": "error"}]}})
    assert acc.outcome()["findings_total"] == 2
    assert [finding["code"] for finding in acc.outcome()["findings_preview"]] == [
        "kept", "late"]
    assert acc.outcome()["findings_by_severity"] == {"error": 1, "warning": 1}


# ---------------------------------------------------------------------------
# Phase 8
# ---------------------------------------------------------------------------

def test_phase8_replaces_primary_findings_and_appends_newcomers_in_envelope_order():
    """Matched blocks replace their row's findings; newcomers follow every block.

    Fails before the change: no slim envelope and no ordered finding segments.
    """
    matched = "E:\\work\\job\\matched.py"
    acc = support.CartoOutcomeAccumulator()
    files_lines = []
    for index, file in enumerate((matched, "E:\\work\\job\\other.py")):
        row = {"file": file, "status": "refreshed", "changed": index == 0,
               "findings": [_finding(random.Random(index), file) for _ in range(2)]}
        files_lines.append(_row_line(row))
        acc.put(row)
    envelope = {"execution": "inline", "success": False, "error": "phase 8 failed",
                "warnings": [{"type": "disk"}],
                "files": [{"file": "E:\\work\\job\\new_a.py", "status": "failed",
                           "findings": [{"code": "brand_new", "severity": "error"}]},
                          {"file": matched, "status": "refreshed",
                           "findings": [{"code": "phase8_only", "severity": "warning"}],
                           "warnings": [{"type": "stale"}]}]}
    phase8_lines = _envelope_lines(envelope)
    acc.set_phase8(envelope)
    expected, _folded = _oracle(files_lines + phase8_lines, [])
    assert acc.outcome() == expected
    codes = [finding["code"] for finding in acc.outcome()["findings_preview"]]
    assert "phase8_only" in codes and "brand_new" in codes
    assert acc.outcome()["phase8_summary"] == {
        "execution": "inline", "success": False, "error": "phase 8 failed",
        "files_total": 2, "files_failed": 1}
    assert acc.outcome()["phase8_failed"] is True
    assert acc.outcome()["errors_total"] == expected["errors_total"]
    assert acc.outcome(infrastructure_error={"kind": "x"})["phase8_failed"] is True


def test_phase8_last_row_wins_for_a_repeated_label_and_newcomers_keep_first_position():
    """The batch maps the envelope by ``file``: last row wins, first position stays.

    Fails before the change: no envelope was kept at all.
    """
    file = "E:\\work\\job\\dup.py"
    rows = [{"file": file, "status": "refreshed", "changed": False,
             "findings": [{"code": "row_own", "severity": "info"}]}]
    envelope = {"execution": "inline",
                "files": [
                    {"file": "E:\\work\\job\\new.py", "status": "completed",
                     "findings": [{"code": "new_one", "severity": "info"}]},
                    {"file": file, "status": "refreshed",
                     "findings": [{"code": "first_dup", "severity": "warning"}]},
                    {"file": file, "status": "failed",
                     "findings": [{"code": "last_dup", "severity": "error"}]},
                    {"file": "E:\\work\\job\\new.py", "status": "failed",
                     "findings": [{"code": "new_two", "severity": "error"}]},
                ]}
    acc = support.CartoOutcomeAccumulator()
    acc.put(rows[0])
    acc.set_phase8(envelope)
    aggregate = support.aggregate_beacon_outcomes(rows)
    job = support.beacon_job_outcome(aggregate, phase8=envelope)
    expected = {key: value for key, value in job.items() if key not in LIST_VALUES}
    expected.update(_builder().new_aggregates(job, aggregate["files"], phase8=envelope))
    expected["files_changed"] = list(job["files_changed"])[:50]
    expected["findings_preview"] = [{key: finding.get(key) for key in SLIM_FINDING_KEYS}
                                    for finding in job["findings"][:20]]
    expected["preview_totals"] = {"files_changed": len(job["files_changed"]),
                                  "findings_preview": job["findings_total"]}
    assert acc.outcome() == expected
    assert expected["phase8_summary"]["files_total"] == 4
    assert expected["phase8_summary"]["files_failed"] == 2
    codes = [finding["code"] for finding in acc.outcome()["findings_preview"]]
    assert "last_dup" in codes and "first_dup" not in codes and "row_own" not in codes
    assert codes.count("new_two") == 1 and "new_one" not in codes


def test_a_key_that_arrives_after_the_envelope_still_takes_its_phase8_row():
    """The batch matches the folded rows, so a later row consumes its envelope row.

    Fails before the change: no envelope state existed to match later rows.
    """
    early, late = "E:\\work\\job\\early.py", "E:\\work\\job\\late.py"
    acc = support.CartoOutcomeAccumulator()
    files_lines = []
    row = {"file": early, "status": "refreshed", "changed": False, "findings": []}
    files_lines.append(_row_line(row))
    acc.put(row)
    envelope = {"execution": "inline",
                "files": [{"file": late, "status": "refreshed",
                           "findings": [{"code": "late_one", "severity": "info"}]}]}
    phase8_lines = _envelope_lines(envelope)
    acc.set_phase8(envelope)
    _assert_every_variant(acc, files_lines + phase8_lines, [])
    later = {"file": late, "status": "failed", "changed": True,
             "findings": [{"code": "own", "severity": "info"}]}
    files_lines.append(_row_line(later))
    acc.put(later)
    _assert_every_variant(acc, files_lines + phase8_lines, [])
    assert acc.outcome()["findings_total"] == 1


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def test_local_cpu_failure_is_the_exact_folded_predicate():
    """Failed + error + the identity for the row's own file, primary or phase 8.

    Fails before the change: no per-key identity survived the put.
    """
    identity = {"phase": "file:source", "exception": "ValueError"}
    inside, outside = "E:\\work\\job\\inside.py", "E:\\work\\job\\outside.py"
    acc = support.CartoOutcomeAccumulator()
    acc.put({"file": inside, "status": "failed", "error": "boom",
             "local_cpu_failure": {**identity, "file": inside}, "findings": []})
    acc.put({"file": outside, "status": "failed", "error": "boom",
             "local_cpu_failure": {**identity, "file": "elsewhere.py"}, "findings": []})
    acc.put({"file": "E:\\work\\job\\clean.py", "status": "failed", "findings": []})
    assert acc.local_cpu_failure(identity) is True
    assert acc.local_cpu_failure({**identity, "exception": "KeyError"}) is False
    assert acc.local_cpu_failure({}) is False
    # A clean replacement of the attributed row removes the attribution.
    acc.put({"file": inside, "status": "refreshed", "changed": False, "findings": []})
    assert acc.local_cpu_failure(identity) is False
    # A phase-8 row OF A PRIMARY FILE counts (its own file, its own identity)...
    acc.set_phase8({"execution": "inline",
                    "files": [{"file": outside, "status": "failed", "error": "late",
                               "local_cpu_failure": {**identity, "file": outside},
                               "findings": []}]})
    assert acc.local_cpu_failure(identity) is True
    # ...a phase-8-only file does not, and a non-failed phase-8 row does not.
    acc.set_phase8({"execution": "inline",
                    "files": [{"file": "E:\\work\\job\\new.py", "status": "failed",
                               "error": "late",
                               "local_cpu_failure": {**identity,
                                                     "file": "E:\\work\\job\\new.py"},
                               "findings": []}]})
    assert acc.local_cpu_failure(identity) is False
    acc.set_phase8({"execution": "inline",
                    "files": [{"file": outside, "status": "refreshed",
                               "local_cpu_failure": {**identity, "file": outside},
                               "findings": []}]})
    assert acc.local_cpu_failure(identity) is False
    # §3.3's predicate is on the folded row, so a receipt rejection makes a row
    # failed with an error and can carry the identity through a delta.
    acc.put({"file": outside, "status": "failed", "findings": [],
             "local_cpu_failure": {**identity, "file": outside}},
            delta={"unset": [], "extend": {}}, nodes_reflected=2)
    acc.put_nodes([{"file": outside, "row": {"node_id": "n1", "success": True}},
                   {"file": outside, "row": {"node_id": "n2", "success": False}}])
    assert acc.local_cpu_failure(identity) is True


# ---------------------------------------------------------------------------
# Cost and memory: pinned structurally, never by timing
# ---------------------------------------------------------------------------

class _WeakRow(dict):
    """A dict a weak reference can point at: the trap §2.7 pins on a put row."""


def _synthetic_rows(count, *, findings=1):
    for index in range(count):
        file = f"E:\\work\\job\\synthetic_{index}.py"
        yield file, _WeakRow({"file": file, "status": "refreshed",
                              "changed": index % 3 == 0,
                              "findings": [{"code": f"c{index % 5}", "severity": "warning",
                                            "message": "m", "file": file, "line": index,
                                            "finding_id": f"f-{index}"}] * findings})


def test_two_thousand_puts_visit_one_path_and_hold_no_row():
    """2,000 puts: bounded index visits, dead rows, an outcome that reads no index.

    Fails before the change: no index, no visit counter and no summary-only
    ``outcome`` existed to make either claim.
    """
    acc = support.CartoOutcomeAccumulator()
    files_lines = []
    alive = []
    bound = 2 * (2000 - 1).bit_length() + 1        # 2⌈log₂ n⌉ + 1 = 23
    for _file, row in _synthetic_rows(2000):
        alive.append(weakref.ref(row))
        files_lines.append(_row_line(row))
        acc.put(row)
        del row
    assert all(ref() is None for ref in alive)
    expected, _folded = _oracle(files_lines, [])
    assert acc.outcome() == expected
    kept = {"file": "E:\\work\\job\\synthetic_0.py", "status": "refreshed",
            "changed": True,
            "findings": [{"code": "c0", "severity": "warning", "message": "m",
                          "file": "E:\\work\\job\\synthetic_0.py", "line": 0,
                          "finding_id": "f-0"}]}
    # An ordinary put whose preview window does not move: one leaf-to-root path.
    acc.reset_index_visits()
    acc.put(dict(kept))
    assert acc.index_visits <= bound
    # A repair that moves the window: the first changed file stops being changed.
    acc.reset_index_visits()
    acc.put({**kept, "changed": False})
    assert acc.index_visits <= bound
    assert acc.outcome()["files_changed"][0] == "E:\\work\\job\\synthetic_3.py"
    assert acc.outcome()["files_changed_total"] == expected["files_changed_total"] - 1
    # ``outcome`` touches no index node and no per-key summary.
    acc.reset_index_visits()
    before = acc.outcome()
    assert acc.index_visits == 0
    acc._rows = acc._receipt_bases = acc._phase8_only = _Poison()
    acc._keys = _Poison()
    assert acc.outcome() == before


class _Poison:
    """Any attribute read is a failure: ``outcome`` must not need this object."""

    def __getattr__(self, name):
        raise AssertionError(f"outcome touched {name}")
