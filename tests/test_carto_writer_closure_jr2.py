"""JR2 G3 closure (leg 4): projection rows, the frozen 1,063-row record, summary-only proof.

Companion to ``test_carto_job_writer_jr2.py`` -- never imported, so its helpers are
not this file's oracle.  Four closure obligations:

* every projection-bearing row of the committed trimmed copy
  (``folder_refresh_trim``, trimmed from the archived 1,063-row folder record)
  replays through the public writer byte for byte, with the row's own key order;
  an explicit (mismatching) projection survives storage and the joined detail;
* the archived folder record itself, when a read-only copy is present, is
  measured: all 1,063 rows appended and all 1,059 projection-bearing rows
  single-stored and re-expanded value for value, key order included.  The
  original is only ever read and copied; nothing is committed and no timing is
  asserted.  This measurement runs under **WSL** (``.venv-wsl``) against
  ``/mnt/c``, never through the Windows daemon;
* a single FILE row with small and huge projection/evidence containers ends in a
  record and a terminal proof that hold no complete tail list at any depth: the
  containers are B1 markers pointing at that row's line, and the complete detail
  is still served from the row;
* an unknown head-metadata value with a nested tail gets exactly one ``set``
  line even when small, so the proof stays summary-only without losing it; and a
  failed terminal ``os.replace`` still serves the complete row through the
  terminal overlay, with no private proof field leaked.

**Would it fail before?**  ``CartoJobRecord`` is resolved per call, so on the JR2
base every test is one named red at that lookup.  Against the pre-closure writer
draft the summary-only tests are red on their own assertions: the FILE summary
re-stored the row's containers (a small nested-tail container was copied inline
into ``result_summary``; a >4 KiB projection was moved into a second ``set``
line), and a small unknown metadata container stayed inline with no ``set`` line.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import runpy
from collections.abc import Mapping
from pathlib import Path

import pytest

from workflowy_mcp import carto_job_record as record
from workflowy_mcp import config as cfg
from workflowy_mcp import job_status_view
from workflowy_mcp.carto_job_paging import join_carto_job_detail_pages

FIXTURES = Path(__file__).parent / "fixtures" / "carto_job_records"
TRIM_FIXTURE = FIXTURES / "folder_refresh_trim"

#: The archived WSL copy the trimmed fixture came from (README "Provenance").
FULL_RECORD_DIR = Path("/mnt/c/tmp/carto-jobfile-eval/snap1")
FULL_RECORD_ROWS = 1063
FULL_RECORD_PROJECTED = 1059
FULL_RECORD_ENV = "CARTO_JR2_FULL_RECORD"

JOB_ID = "carto-refresh-file-9f2c7a11"
ROOT_UUID = "7834ca3d-2f6b-47a2-aff4-9138aa57612f"
STAMP = "20261027-160000"

#: §2.2's count bounds for the record's inline previews (the accumulator's
#: findings preview is the 20-entry member of the same family).
ROOT_PREVIEW_CAPS = {"findings_preview": 20, "files_changed": 50, "errors": 20,
                     "refresh_errors": 20, "nexusignore_sources": 50, "missing_paths": 50}

PROJECTION_SENTINEL = "jr2-closure-projection-sentinel"
DIAGNOSTICS_SENTINEL = "jr2-closure-diagnostics-sentinel"
FILE_PATH = "E:\\work\\closure\\single_file.py"


# ---------------------------------------------------------------------------
# Small helpers (independent of the writer's own tests)
# ---------------------------------------------------------------------------

def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _bytes(path):
    return Path(path).read_bytes()


def _lines(path):
    return Path(path).read_text(encoding="utf-8").splitlines()


def _compact(value):
    """§2.4/B1's one evidence encoding, written here so the oracle is independent."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _writer_class():
    """Resolved per test: a missing G3 class is one named red, not one import error."""
    return getattr(record, "CartoJobRecord")


def _head(*, job_id=JOB_ID, mode="folder", status="running"):
    return {
        "id": job_id,
        "type": "CARTO_REFRESH",
        "mode": mode,
        "root_uuid": ROOT_UUID,
        "status": status,
        "error": None,
        "error_kind": None,
        "created_at": "2026-10-27T16:00:00.000000+00:00",
        "progress": {"total_files": 0, "completed_files": 0, "current_phase": "start"},
        "execution_model": "daemon_task",
        "execution_owner": "launcher",
        "detached": False,
        "logs_path": "",
        "pid": 4321,
    }


def _new_writer(tmp_path, *, job_id=JOB_ID, mode="folder"):
    job_file = tmp_path / "cartographer_jobs" / f"{STAMP}_{job_id}.json"
    job_file.parent.mkdir(parents=True, exist_ok=True)
    head = _head(job_id=job_id, mode=mode)
    head["logs_path"] = str(job_file.with_suffix(".log"))
    writer = _writer_class()(job_file)
    assert writer.create(head) is True
    return writer, job_file, job_id


def _files_sidecar(job_file):
    return Path(job_file).with_suffix("") / "files.jsonl"


def _files_bytes(job_file):
    return _bytes(_files_sidecar(job_file))


def _entries(job_file):
    return [json.loads(line) for line in _lines(_files_sidecar(job_file))]


def _expand(entry):
    """§2.6 re-expansion of one stored line, before any delta overlay."""
    return record.expand_carto_projection(entry)


def _marked(row):
    """The independent builder's D3-1 transformation, not the writer's code."""
    return runpy.run_path(str(FIXTURES / "build_fixtures.py"))["marker_row"](row)


def _detail_options():
    return job_status_view.normalize_job_detail_options("job", view="detail")


def _joined_detail(record_path, job_id, *, overlay=None):
    """Walk the detail cursor to its end and join every page (§2.10.1)."""
    options = dict(_detail_options())
    pages, offset, slice_offset = [], 0, 0
    while True:
        page = record.read_carto_job_view(
            record_path, job_id=job_id, overlay=overlay,
            options={**options, "offset": offset, "slice_offset": slice_offset},
        )
        pages.append(page)
        meta = page.get("detail_page") or {}
        next_offset = meta.get("next_offset")
        if next_offset is None:
            return join_carto_job_detail_pages(pages)
        offset, slice_offset = next_offset, meta.get("next_slice_offset") or 0


def _tail_list_paths(value, path=()):
    """Paths of complete evidence lists still present in a summary.

    §2.12/§2.8: the record and the terminal proof hold no tail list at any depth.
    A root preview is permitted when it is count-bounded (§2.2 says 20/50), and
    an empty list is a presence marker with nothing to lose.  Any other list is
    reported: it is either a §2.4 tail key's complete list or a list no preview
    bound covers.
    """
    found = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            found.extend(_tail_list_paths(item, (*path, str(key))))
        return found
    if isinstance(value, (list, tuple)):
        if not value:
            return found
        if (len(path) == 1 and path[0] in ROOT_PREVIEW_CAPS
                and len(value) <= ROOT_PREVIEW_CAPS[path[0]]):
            return found                       # the bounded root preview §2.2 permits
        found.append(path)
    return found


def _marker(value):
    return isinstance(value, Mapping) and set(value) == {"$evidence", "bytes"}


def _assert_row_marker(marker, *, key, row, row_line):
    """A FILE-summary marker measures its value and points at the existing row."""
    assert _marker(marker), (key, marker)
    assert marker["$evidence"] == "files.jsonl", (key, marker)
    assert isinstance(marker["bytes"], int) and marker["bytes"] > 0, (key, marker)
    assert marker["bytes"] == len(_compact(row[key]).encode("utf-8")), key


def _same_json(left, right):
    """Deep JSON equality: Python's True == 1 is not the projection rule."""
    return _compact(left) == _compact(right) or (
        json.dumps(left, sort_keys=True, separators=(",", ":"), ensure_ascii=True) ==
        json.dumps(right, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def _row_finding(index, *, message_size):
    # The projection sentinel appears exactly once, so a duplicated container is
    # countable in the evidence bytes.
    message = PROJECTION_SENTINEL if index == 0 else "m" * message_size
    return {"code": "empty_comment", "severity": "info", "finding_id": f"f-{index}",
            "line": index + 1, "message": message}


def _single_file_row(scale):
    """One FILE row whose projection/evidence containers nest complete tail lists.

    ``projection`` maps three row keys (in its own key order) and is therefore
    eligible for §2.6's single store; ``diagnostics`` is unknown row metadata
    whose nested ``files`` list is a §2.4 tail.  ``nullable`` / ``empty_mapping``
    / ``empty_list`` pin the None-versus-{} -versus-[] MUST.
    """
    huge = scale == "huge"
    count = 300 if huge else 1
    findings = [_row_finding(index, message_size=300 if huge else 8) for index in range(count)]
    diagnostics = {"files": [{"file": f"E:\\work\\closure\\diag_{index}.py",
                              "note": f"diag-{index}"}
                             for index in range(count)],
                   "note": DIAGNOSTICS_SENTINEL}
    return {
        "file": FILE_PATH,
        "status": "refreshed",
        "changed": True,
        "success": True,
        "findings": findings,
        "warnings": [{"type": "warning", "message": "kept display"}],
        "events": [{"type": "node_updated", "detail": "closure"}],
        "projection": {
            "warnings": [{"type": "warning", "message": "kept display"}],
            "findings": findings,
            "status": "refreshed",
        },
        "diagnostics": diagnostics,
        # A small nested tail (never over §2.2's 4 KiB) so the move decision can
        # only come from the nesting, at either scale.
        "review_notes": {"findings": [{"code": "empty_comment", "severity": "info",
                                       "finding_id": "n-1", "line": 1,
                                       "message": "small nested kept"}],
                         "note": "small container"},
        "nullable": None,
        "empty_mapping": {},
        "empty_list": [],
        "summary_note": "kept",
    }


class _ReplaceFault:
    """A real failed terminal record write: governance's replace seam refuses once."""

    def __init__(self, target, *, real):
        self.target = os.path.normcase(os.path.abspath(os.fspath(target)))
        self._real = real
        self.armed = False
        self.refusals = 0

    def __call__(self, tmp, target, *, ops=None):
        if self.armed and os.path.normcase(os.path.abspath(os.fspath(target))) == self.target:
            self.refusals += 1
            raise PermissionError("simulated terminal record replace failure")
        return self._real(tmp, target, ops=ops)


@pytest.fixture(autouse=True)
def _fresh_state():
    """No cached record signature/index and no process-local cleared history."""
    for cache in (record._record_cache, record._record_ids, record._path_ids,
                  record._detail_cache):
        cache.clear()
    cleared_names = ("_CARTO_CLEARED_JOB_IDS", "_CARTO_CLEARED_JOB_PATHS",
                     "_CARTO_CLEARED_WRITE_LOGGED")
    for name in cleared_names:
        getattr(cfg, name).clear()
    yield
    for cache in (record._record_cache, record._record_ids, record._path_ids, record._detail_cache):
        cache.clear()
    for name in cleared_names:
        getattr(cfg, name).clear()


# ---------------------------------------------------------------------------
# (1) The committed trimmed copy: every projection row, key order, mismatches
# ---------------------------------------------------------------------------

def _committed_projection_lines(fixture=TRIM_FIXTURE, *, marker=False):
    raw = [line for line in _bytes(Path(fixture) / ("marker.files.jsonl" if marker else "files.jsonl")).splitlines(keepends=True)]
    selected = []
    for line in raw:
        entry = json.loads(line)
        if entry.get("k") == "row" and "proj" in entry:
            selected.append(line)
    return selected


def test_every_trimmed_copy_projection_row_replays_byte_for_byte_with_row_key_order(tmp_path):
    """Every committed projection row of the trimmed copy replays through the writer.

    The fixture was trimmed from the archived 1,063-row folder record, so this is
    the committed, deterministic slice of that data rather than a synthetic row.
    Each row keeps its own key order with ``projection`` null at its original
    position, ``proj`` keeps the projection's own key order, and expansion
    reproduces the committed line's row exactly.

    Would fail before: no ``CartoJobRecord`` at the JR2 base; and a projection
    store that reordered ``proj`` or moved ``projection`` would diverge from the
    committed builder bytes.
    """
    committed = _committed_projection_lines()
    marker_committed = _committed_projection_lines(marker=True)
    assert len(committed) == 26                       # the trimmed copy's projection rows
    assert len(marker_committed) == len(committed)
    assert all(json.loads(line)["proj"] for line in committed)

    writer, job_file, _ = _new_writer(tmp_path)
    for line in committed:
        entry = json.loads(line)
        row = _expand(entry)
        assert writer.append_file_row(
            row, section=entry["sec"], delta=entry.get("delta"),
            nodes_reflected=entry.get("nodes")) is True

    produced = _files_bytes(job_file).splitlines(keepends=True)
    assert produced == marker_committed              # original inputs, independent D3-1 byte oracle
    for line, expected_line in zip(produced, marker_committed):
        entry, committed_entry = json.loads(line), json.loads(expected_line)
        assert list(entry["row"]) == list(committed_entry["row"])   # row-key order
        assert entry["row"]["projection"] is None
        assert entry["proj"] == committed_entry["proj"]
        assert _compact(_expand(entry)) == _compact(_expand(committed_entry))


def test_explicit_mismatching_projection_survives_storage_and_joined_detail(tmp_path):
    """A projection that differs from its row stays explicit; an equal one single-stores.

    §2.6's criterion is deep equality, not key order or size: the equal mapping
    (whose own key order differs from the row's field order) stores ``null`` plus
    ``proj`` in the projection's order, and the mismatching mapping is stored
    unchanged at its original position.  The joined public detail serves both
    rows exactly as the job produced them.

    Would fail before: no ``CartoJobRecord`` at the JR2 base; a store that
    single-stored on key equality would serve a projection the row never had.
    """
    writer, job_file, job_id = _new_writer(tmp_path)
    check_target = {"verb": "check",
                    "params": {"file_path": "E:\\work\\closure", "min_severity": "info"}}
    equal_row = {
        "file": "E:\\work\\closure\\equal.py", "status": "refreshed", "changed": True,
        "findings": [], "warnings": [], "events": [],
        "projection": {"status": "refreshed", "changed": True},     # reversed row order
        "check_target": check_target,
        "unknown_empty": {},
    }
    mismatch_row = {
        "file": "E:\\work\\closure\\mismatch.py", "status": "refreshed", "changed": True,
        "findings": [{"code": "empty_comment", "severity": "info", "line": 1,
                      "message": "kept explicitly"}],
        "warnings": [], "events": [],
        "projection": {"status": "refreshed", "changed": False},    # explicitly differs
        "check_target": check_target,
    }
    for row in (equal_row, mismatch_row):
        assert writer.append_file_row(row) is True

    entries = _entries(job_file)
    assert [entry["k"] for entry in entries] == ["row", "row"]
    equal_entry, mismatch_entry = entries

    assert list(equal_entry) == ["k", "sec", "row", "proj"]
    assert equal_entry["proj"] == ["status", "changed"]            # projection's order
    assert equal_entry["row"]["projection"] is None
    assert list(equal_entry["row"]) == list(equal_row)             # row's field order
    assert _compact(_expand(equal_entry)) == _compact(equal_row)

    assert "proj" not in mismatch_entry
    assert mismatch_entry["row"]["projection"] == {"status": "refreshed", "changed": False}
    assert list(mismatch_entry["row"]) == list(mismatch_row)
    assert _compact(_expand(mismatch_entry)) == _compact(mismatch_row)

    detail = _joined_detail(job_file, job_id)
    by_file = {row["file"]: row for row in detail["result_summary"]["files"]}
    assert by_file[equal_row["file"]]["projection"] == equal_row["projection"]
    assert by_file[mismatch_row["file"]]["projection"] == mismatch_row["projection"]
    assert list(by_file[equal_row["file"]]) == list(equal_row)
    assert list(by_file[mismatch_row["file"]]) == list(mismatch_row)


# ---------------------------------------------------------------------------
# (2) The archived 1,063-row record, when its read-only copy is present
# ---------------------------------------------------------------------------

def test_bulk_projection_of_a_receipt_list_stays_explicit_and_lossless(tmp_path):
    """The receipt-null grammar cannot replace a projected list with null."""
    path = tmp_path / "bulk-projection.json"
    writer = _writer_class()(path)
    head = _head()
    head["type"] = "CARTO_BULK_APPLY"
    assert writer.create(head)
    file = "/repo/bulk.py"
    receipt = {"node_id": "n", "file_path": file, "success": True,
               "satisfied": True, "changed": True, "unknown": None}
    assert writer.append_node_rows([{"file": file, "row": receipt}])
    row = {"file": file, "status": "refreshed", "changed": True,
           "findings": [], "warnings": [], "events": [], "nodes_satisfied": 1,
           "node_outcomes": [receipt], "projection": {"node_outcomes": [receipt]}}
    assert writer.append_file_row(row, nodes_reflected=1)
    entry = _entries(path)[0]
    assert entry["row"]["node_outcomes"] is None
    assert "proj" not in entry
    assert entry["row"]["projection"] == row["projection"]
    detail = _joined_detail(path, head["id"])
    saved = detail["result_summary"]["files"][0]
    assert all(saved[key] == value for key, value in row.items())


def _shape(path):
    """(rows, projection-bearing rows) of one frozen record, from result_summary.files."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = (data.get("result_summary") or {}).get("files") or []
    projected = sum(1 for row in rows if isinstance(row, Mapping)
                    and isinstance(row.get("projection"), Mapping))
    return len(rows), projected


def _select_frozen_record():
    """The recorded copy by shape, never by a live path or a job folder."""
    override = os.environ.get(FULL_RECORD_ENV)
    if override:
        candidates = [Path(override).expanduser()]
    elif FULL_RECORD_DIR.is_dir():
        candidates = sorted(FULL_RECORD_DIR.glob("*.json"))
    else:
        pytest.skip(f"frozen {FULL_RECORD_PROJECTED}-projection record absent: "
                    f"{FULL_RECORD_DIR} is not a directory")
    if not candidates:
        pytest.skip(f"frozen record absent: no *.json under {FULL_RECORD_DIR}")
    rejected = []
    for path in candidates:
        try:
            rows, projected = _shape(path)
        except Exception as exc:                       # noqa: BLE001 - report, then skip
            rejected.append(f"{path}: unreadable ({type(exc).__name__}: {exc})")
            continue
        if (rows, projected) == (FULL_RECORD_ROWS, FULL_RECORD_PROJECTED):
            return path
        rejected.append(f"{path}: result_summary.files={rows} rows, {projected} projection-bearing")
    pytest.skip("no frozen record with result_summary.files = "
                f"{FULL_RECORD_ROWS} rows / {FULL_RECORD_PROJECTED} projection-bearing; "
                + "; ".join(rejected))


def test_all_projection_rows_of_the_frozen_folder_record_round_trip(tmp_path):
    """All 1,063 rows of the archived folder record, 1,059 of them projected, round-trip.

    The measured copy is read-only (a byte-identical copy is made into pytest's
    tmp and used from there; the original under /mnt/c and every live job record
    are never written).  Each row is appended through the public writer, then
    every produced line is parsed and expanded: the line grammar, the row's key
    order, the projection's own key order and the single-store byte count are
    checked row by row.  No timing is asserted; this measurement is a WSL run.

    Would fail before: no ``CartoJobRecord`` at the JR2 base; a lossy or
    reordering single store diverges from the appended row on some of these
    1,059 real projections.
    """
    source = _select_frozen_record()
    copy_path = tmp_path / "frozen_folder_record.json"
    shutil.copy2(source, copy_path)
    assert hashlib.sha256(_bytes(copy_path)).hexdigest() == (
        hashlib.sha256(_bytes(source)).hexdigest())

    data = json.loads(copy_path.read_text(encoding="utf-8"))
    rows = list((data.get("result_summary") or {}).get("files") or [])
    del data
    assert len(rows) == FULL_RECORD_ROWS

    writer, job_file, _ = _new_writer(tmp_path)
    expected_lines, expected_rows, expected_proj = [], [], []
    duplicated_total = 0
    for row in rows:
        marked_row = _marked(row)
        stored = {key: value for key, value in marked_row.items()
                  if key not in {"snapshot", "analysis_inputs"}}
        # §2.6, re-derived here: single store only when every projected key is
        # deep-JSON equal to the row's own.
        projection = stored.get("projection")
        proj = None
        projected_key_equal = isinstance(projection, Mapping) and all(
            key in stored and _same_json(value, stored[key]) for key, value in projection.items())
        if projected_key_equal:
            proj = list(projection)
            stored["projection"] = None
        line = {"k": "row", "sec": "files", "row": stored}
        if proj is not None:
            line["proj"] = proj
        expected_lines.append(_compact(line).encode("utf-8"))
        expected_rows.append(_compact(marked_row).encode("utf-8"))
        expected_proj.append(proj)
        duplicated_total += len(
            _compact({"k": "row", "sec": "files", "row": row}).encode("utf-8")) + 1
        assert writer.append_file_row(row) is True

    raw = _files_bytes(job_file)
    produced = raw.split(b"\n")
    assert produced[-1] == b""                     # one newline-terminated line per row
    produced = produced[:-1]
    assert len(produced) == FULL_RECORD_ROWS
    assert sum(1 for proj in expected_proj if proj is not None) == FULL_RECORD_PROJECTED

    for index, line in enumerate(produced):
        assert line == expected_lines[index], index
        entry = json.loads(line)
        assert list(entry) == (["k", "sec", "row", "proj"] if expected_proj[index] is not None
                               else ["k", "sec", "row"]), index
        assert entry["sec"] == "files" and entry["k"] == "row", index
        if expected_proj[index] is not None:
            assert entry["row"]["projection"] is None, index
            assert entry["proj"] == expected_proj[index], index
        else:
            assert "proj" not in entry, index
            if "projection" in rows[index]:
                assert entry["row"]["projection"] is None, index
            else:
                assert "projection" not in entry["row"], index  # absent is not an invented null
        assert _compact(_expand(entry)).encode("utf-8") == expected_rows[index], index
        # Key order is part of the round trip, not just value equality.
        assert list(entry["row"]) == list(json.loads(expected_lines[index])["row"]), index

    single_store_total = len(raw)
    assert single_store_total == sum(len(line) + 1 for line in expected_lines)
    assert duplicated_total - single_store_total > 10_000_000      # single store plus D3-1, original bytes kept as denominator

    stored_record = _load(job_file)
    assert stored_record["evidence"]["files"]["rows"] == FULL_RECORD_ROWS
    # `rows` counts row lines only; a value moved to a `set` line is not a row.
    assert stored_record["evidence"]["files"]["lines"] >= FULL_RECORD_ROWS
    assert stored_record["evidence"]["files"]["pending"] == 0


# ---------------------------------------------------------------------------
# (3) A single FILE row: summary-only record and terminal proof, complete detail
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scale", ["small", "huge"])
def test_file_summary_stays_summary_only_and_serves_the_complete_row(tmp_path, scale):
    """The FILE record and terminal proof carry markers, never the row's containers.

    One FILE row holds a projection/evidence container with a nested complete
    tail list, both small (inline-sized) and huge (well over §2.2's 4 KiB move),
    plus unknown nullable/empty fields.  The saved record and the terminal proof
    must hold no complete tail list at any depth; the containers become
    ``{"$evidence": "files.jsonl", "bytes": n}`` markers pointing at the row's
    existing line, and the projection content exists exactly once (``projection``
    null + ``proj``, no ``set`` duplicate).  The joined public detail still
    serves every row key exactly as the job produced it, None/``{}``/``[]``
    included.

    Would fail before: no ``CartoJobRecord`` at the JR2 base; against the
    pre-closure draft the small container was copied inline into
    ``result_summary`` (a tail list in the summary) and the huge one was
    re-stored in a ``set`` line (two copies of the projection), failing the
    predicate and the single-copy check respectively.
    """
    writer, job_file, job_id = _new_writer(tmp_path, mode="file")
    row = _single_file_row(scale)
    assert writer.append_file_row(row) is True
    terminal = writer.finalize({"status": "completed",
                                "progress": {"total_files": 1, "completed_files": 1}})
    assert isinstance(terminal, dict)

    entries = _entries(job_file)
    row_entries = [entry for entry in entries if entry["k"] == "row"]
    assert len(row_entries) == 1                    # one FILE row, by definition
    entry = row_entries[0]
    row_line = _compact(entry).encode("utf-8")

    # §2.6 single store, with the projection at its original key position.
    assert list(entry) == ["k", "sec", "row", "proj"]
    assert entry["row"]["projection"] is None
    assert entry["proj"] == ["warnings", "findings", "status"]
    assert list(entry["row"]) == list(row)
    assert _compact(_expand(entry)) == _compact(row)

    record_summary = _load(job_file)["result_summary"]
    assert _load(job_file)["evidence"]["files"]["rows"] == 1      # row lines landed, not sets
    assert _tail_list_paths(record_summary) == []
    assert _tail_list_paths(terminal["result_summary"]) == []
    assert _tail_list_paths(writer.summary()) == []

    # The FILE result's own non-list keys stay present with their values ...
    assert record_summary["file"] == row["file"]
    assert record_summary["status"] == "refreshed"
    assert record_summary["changed"] is True
    assert record_summary["summary_note"] == "kept"
    assert record_summary["nullable"] is None and "nullable" in record_summary
    assert record_summary["empty_mapping"] == {}
    # ... and the containers point at the row line instead of being re-stored.
    for key in ("projection", "diagnostics", "review_notes"):
        _assert_row_marker(record_summary[key], key=key, row=row, row_line=row_line)
        assert _compact(terminal["result_summary"][key]) == _compact(record_summary[key])

    # Exactly one copy of the equal projection content (and of the diagnostics
    # container): the row line.  Only §2.2's bounded preview may repeat a
    # finding, never the container.
    raw = _files_bytes(job_file)
    assert raw.count(PROJECTION_SENTINEL.encode()) == 1
    assert raw.count(DIAGNOSTICS_SENTINEL.encode()) == 1
    assert json.dumps(record_summary).count(PROJECTION_SENTINEL) <= (
        ROOT_PREVIEW_CAPS["findings_preview"])
    set_lines = [entry for entry in entries if entry["k"] == "set"]
    assert [item for item in set_lines if "projection" in json.dumps(item["key"])
            or "diagnostics" in json.dumps(item["key"])] == []

    # §2.2's size invariant: the proof's size does not follow the row's size.
    if scale == "huge":
        assert len(_compact(row).encode("utf-8")) > 100_000
        assert len(row_line) > 100_000
    assert len(_compact(terminal["result_summary"]).encode("utf-8")) < 24 * 1024
    assert terminal["warnings"] == row["warnings"]          # the top display is permitted

    # Public detail: every row key exactly as the job gave it.
    detail = _joined_detail(job_file, job_id)
    served = detail["result_summary"]
    for key, value in row.items():
        assert served.get(key) == value, key
    assert served["nullable"] is None
    assert served["empty_list"] == [] and served["empty_mapping"] == {}
    assert served["projection"] == row["projection"]
    assert served["diagnostics"] == row["diagnostics"]
    assert "files" not in served                            # a FILE row is not a files list
    assert "_pending_evidence" not in json.dumps(detail)
    assert "_carto_writer_summary" not in json.dumps(detail)
    assert not [name for name in served if str(name).startswith("_")]
    assert "_carto_writer_summary" not in json.dumps(_load(job_file))
    assert "_pending_evidence" not in json.dumps(_load(job_file))


def test_failed_terminal_replace_still_serves_the_row_through_the_overlay(tmp_path, monkeypatch):
    """A real failed terminal ``os.replace`` keeps every fact, and leaks no private proof.

    The terminal record write fails inside governance's one replace seam (no
    stubbing of the writer's payload construction), so the disk record stays at
    the last successful running write while the terminal proof carries the fresh
    payload.  That proof is summary-only -- markers for the row's containers, no
    tail list anywhere -- and the joined detail read with the terminal overlay
    still serves the complete row, key for key.  Without the overlay the landed
    row is already complete, which is exactly the evidence the overlay protects.

    Would fail before: no ``CartoJobRecord`` at the JR2 base; against the
    pre-closure draft the terminal summary held the small nested-tail container
    inline, so the proof was not summary-only.
    """
    from workflowy_mcp import carto_job_governance as governance

    writer, job_file, job_id = _new_writer(tmp_path, mode="file")
    row = _single_file_row("huge")
    assert writer.append_file_row(row) is True
    running = _load(job_file)
    assert running["status"] == "running"
    assert "status_write_error" not in running

    fault = _ReplaceFault(job_file, real=governance._replace_with_retry)
    monkeypatch.setattr(governance, "_replace_with_retry", fault)
    fault.armed = True
    terminal = writer.finalize({"status": "completed",
                                "progress": {"total_files": 1, "completed_files": 1}})
    assert fault.refusals == 1
    assert terminal.get("status_write_error")

    # The record write really failed: the disk record is the earlier running one.
    on_disk = _load(job_file)
    assert on_disk["status"] == "running"
    assert "status_write_error" not in on_disk

    # The terminal proof is summary-only and carries no private field.
    row_entry = [entry for entry in _entries(job_file) if entry["k"] == "row"][0]
    row_line = _compact(row_entry).encode("utf-8")
    assert _tail_list_paths(terminal["result_summary"]) == []
    assert "_pending_evidence" not in terminal
    assert "_carto_writer_summary" not in json.dumps(terminal)
    for key in ("projection", "diagnostics", "review_notes"):
        _assert_row_marker(terminal["result_summary"][key], key=key, row=row, row_line=row_line)
    assert "path" in terminal

    # Complete row detail through the overlay, and from the landed evidence alone.
    for overlay in (terminal, None):
        detail = _joined_detail(job_file, job_id, overlay=overlay)
        served = detail["result_summary"]
        for key, value in row.items():
            assert served.get(key) == value, (key, overlay is not None)
        assert served["diagnostics"] == row["diagnostics"]
        assert "_pending_evidence" not in json.dumps(detail)
        assert "_carto_writer_summary" not in json.dumps(detail)


def test_unknown_head_metadata_with_a_nested_tail_takes_one_set_line(tmp_path):
    """Unknown metadata with a nested tail is moved even when small, and restored in detail.

    A head field the job itself supplied (not a row key, so no row line can carry
    it) holds a complete tail list.  The writer writes exactly one ``set`` line,
    the record and terminal proof hold only the marker, and the joined detail
    restores the complete value: the proof stays summary-only without losing
    anything.

    Would fail before: no ``CartoJobRecord`` at the JR2 base; against the
    pre-closure draft a small container stayed inline, so the terminal summary
    carried a complete tail list (and a huge one would have been moved anyway,
    hiding the small-value bug).
    """
    writer, job_file, job_id = _new_writer(tmp_path, mode="file")
    row = _single_file_row("small")
    assert writer.append_file_row(row) is True

    metadata = {"files": [{"file": "E:\\work\\closure\\run_0.py", "note": "kept"}],
                "note": "run-level metadata"}
    terminal = writer.finalize({"status": "completed",
                                "progress": {"total_files": 1, "completed_files": 1},
                                "run_diagnostics": metadata})
    assert isinstance(terminal, dict)

    set_lines = [entry for entry in _entries(job_file)
                 if entry["k"] == "set" and entry["key"] == ["run_diagnostics"]]
    assert len(set_lines) == 1                                   # one set line, even when small
    assert set_lines[0]["v"] == metadata

    marker = {"$evidence": "files.jsonl", "bytes": len(_compact(metadata).encode("utf-8"))}
    assert _load(job_file)["run_diagnostics"] == marker
    assert _load(job_file)["evidence"]["files"]["rows"] == 1      # the set line is not a row
    assert terminal["run_diagnostics"] == marker
    assert _tail_list_paths(terminal["result_summary"]) == []
    assert _tail_list_paths(_load(job_file)["result_summary"]) == []
    assert "run_diagnostics" not in json.dumps(_load(job_file)["result_summary"])

    detail = _joined_detail(job_file, job_id)
    assert detail["run_diagnostics"] == metadata                 # complete, from the set line
    for key, value in row.items():
        assert detail["result_summary"].get(key) == value, key
    assert "_pending_evidence" not in json.dumps(detail)
