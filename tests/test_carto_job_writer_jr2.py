"""JR2 G3: structural witnesses for the ``CartoJobRecord`` writer (no production switch).

WaveSpec rev 3 ([ap:JUH8JU]) §2.4-§2.6 and §2.8, §3.2's writer test list; contract
cut [ap:C367PW] rev 1, clarifications B1 (CF-167) and B5 (CF-161); "A cleared record
stays cleared" (CF-137, branch 13).  Every test is structural -- bytes, line
grammar, open counters, weak references, record key order -- and never timing.

The oracle is the frozen G2 fixture set in ``tests/fixtures/carto_job_records``
(``build_fixtures.py`` follows §2.2-§2.6 independently of the writer), used in
two ways.  The frozen ``record.json`` and its ``files.jsonl``/``nodes.jsonl``
sidecars are the **replay input and byte oracle**: the writer replays every
frozen line through its real API and must produce identical bytes, and the
original record still feeds the complete reader comparisons.  Beside each
frozen record the independent builder also writes a **writer counterpart**
(``writer.record.json``, Q4 [ap:P0YV8T]): the record the current writer must
produce, corrected for B5's exact ``warning_count`` (CF-161), count-bounded
previews staying inline, the row-line hint across every evidence section and
the up-to-date state of the synthetic interrupted-bulk stop.  Record parity is
asserted against that counterpart, never by copying writer output.  Nothing
here edits the builder, the fixtures or the read side.

**Would it fail before?** No ``CartoJobRecord`` exists at the JR2 base: every test
below dies at the writer's construction, so G3 is their only way to green.
"""
from __future__ import annotations

import copy
import gc
import io
import json
import logging
import ntpath
import os
import weakref
import runpy
from collections import Counter
from pathlib import Path

import pytest

from workflowy_mcp import carto_job_record as record
from workflowy_mcp import config as cfg
from workflowy_mcp import job_status_view
from workflowy_mcp import server as srv
from workflowy_mcp.beacon_refresh_support import (
    aggregate_beacon_outcomes,
    beacon_job_outcome,
)
from workflowy_mcp import beacon_refresh_support as support
from workflowy_mcp.carto_job_paging import join_carto_job_detail_pages

FIXTURES = Path(__file__).parent / "fixtures" / "carto_job_records"
#: Every frozen G2 evidence pair the writer must replay byte for byte.
EVIDENCE_FIXTURES = (
    "folder_refresh_trim",
    "mixed_folder",
    "bulk_receipts_synthetic",
    "bulk_apply",
    "markdown_generate",
)
#: Every frozen record; no stale-hint or warning-field exemption is implicit.
RECORD_FIXTURES = (
    "folder_refresh_trim",
    "mixed_folder",
    "bulk_apply",
    "markdown_generate",
    "bulk_receipts_synthetic",
)
#: The independent builder's corrected record oracle (Q4), beside every frozen
#: ``record.json``.  Record parity compares against this; the original record
#: stays the replay input and the reader golden.
WRITER_RECORD = "marker.writer.record.json"
#: Everything a structured record derives from its own evidence (the batch
#: composition §2.7 pins): never supplied to the writer, so the fixture's record
#: proves the writer computes it rather than copying it.  `findings_preview`,
#: `files_changed` and `preview_totals` are here too — they come from the
#: accumulator's previews, not from the component's payload.
DERIVED_SUMMARY_KEYS = frozenset({
    "status", "success", "cancelled", "successful", "failed", "skipped",
    "partial_success", "changed", "findings_total", "findings_by_code",
    "findings_truncated", "warnings_total", "warnings_by_type", "warnings_truncated",
    "summary_line", "errors_total", "node_errors_total", "phase8_failed",
    "files_changed_total", "events_total", "findings_by_severity", "phase8_summary",
    "findings_preview", "files_changed", "preview_totals", "infrastructure_error",
    "phase8",
})
#: Record fields the writer owns; never handed to it as input.
WRITER_OWNED_KEYS = frozenset({
    "record_layout", "outcome", "has_errors", "has_warnings", "has_findings",
    "evidence", "in_flight", "result_summary",
})
STAMP = "20261027-120000"
JOB_ID = "carto-refresh-folder-0b10e5d1"
ROOT_UUID = "7834ca3d-2f6b-47a2-aff4-9138aa57612f"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _bytes(path):
    return Path(path).read_bytes()


def _writer_class():
    """Resolved per test so a missing G3 class is one named red per test, not one import error."""
    return getattr(record, "CartoJobRecord")


def _job_file(tmp_path, *, job_id=JOB_ID, stamp=STAMP):
    base = tmp_path / "cartographer_jobs"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{stamp}_{job_id}.json"


def _head(job_id=JOB_ID, *, type_="CARTO_REFRESH", mode="folder", status="running",
          logs_path=None, job_file=None):
    """The minimum admission payload a `/2` record write needs (§2.2 item 3)."""
    return {
        "id": job_id,
        "type": type_,
        "mode": mode,
        "root_uuid": ROOT_UUID,
        "status": status,
        "error": None,
        "error_kind": None,
        "created_at": "2026-10-27T12:00:00.000000+00:00",
        "progress": {"total_files": 0, "completed_files": 0, "current_phase": "start"},
        "execution_model": "daemon_task",
        "execution_owner": "launcher",
        "detached": False,
        "logs_path": str(logs_path or (Path(job_file).with_suffix(".log") if job_file else "")),
        "pid": 4321,
    }


def _new_writer(tmp_path, *, job_id=JOB_ID, type_="CARTO_REFRESH", mode="folder"):
    job_file = _job_file(tmp_path, job_id=job_id)
    writer = _writer_class()(job_file)
    assert writer.create(_head(job_id, type_=type_, mode=mode, job_file=job_file)) is True
    return writer, job_file, job_id


class _WeakRow(dict):
    """A weak-referenceable row: exactly the dict the writer is handed (S5)."""


def _row(file, **extra):
    row = {"file": str(file), "status": "refreshed", "changed": False,
           "findings": [], "warnings": [], "events": []}
    row.update(extra)
    return row


def _files_bytes(job_file):
    return _bytes(Path(job_file).with_suffix("") / "files.jsonl")


def _nodes_bytes(job_file):
    path = Path(job_file).with_suffix("") / "nodes.jsonl"
    return _bytes(path) if path.exists() else b""


def _fixture_nodes(folder):
    """The frozen `nodes.jsonl` bytes, or None when the fixture is not a bulk job."""
    path = Path(folder) / "nodes.jsonl"
    return _bytes(path) if path.exists() else None


def _lines(path):
    return Path(path).read_text(encoding="utf-8").splitlines()


def _expand(entry):
    """The row a frozen/replayed line carries, projection re-expanded (§2.6)."""
    return record.expand_carto_projection(entry)


def _marked(row):
    """Independent builder oracle; never import the production marker here."""
    return runpy.run_path(str(FIXTURES / "build_fixtures.py"))["marker_row"](row)


def _detail_options():
    return job_status_view.normalize_job_detail_options("job", view="detail")


def _joined_detail(record_path, job_id, *, overlay=None):
    """Walk the detail view's cursor to its end and join it (§2.10.1)."""
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


def _record_parity(written, expected):
    """The contract's exact record oracle, with timestamps ONLY removed."""
    left, right = copy.deepcopy(written), copy.deepcopy(expected)
    for value in (left, right):
        for key in ("created_at", "updated_at"):
            value.pop(key, None)
    return left, right





class _Fault:
    """A per-path append/truncate fault seam under the writer's own stream opens."""

    def __init__(self, path, *, partial_bytes=19, fail_truncate=False, poison=None,
                 times=None):
        self.target = os.path.normcase(os.path.abspath(os.fspath(path)))
        self.partial_bytes = partial_bytes
        self.fail_truncate = fail_truncate
        self.poison = poison
        self.remaining = times
        self.disabled = False
        self.appends = 0
        self.truncates = 0

    def matches(self, file):
        try:
            return os.path.normcase(os.path.abspath(os.fspath(file))) == self.target
        except TypeError:
            return False

    def armed(self, data):
        if self.disabled:
            return False
        if self.poison is not None:
            text = data if isinstance(data, str) else data.decode("utf-8", "surrogatepass")
            if self.poison not in text:
                return False
        return self.remaining is None or self.remaining > 0

    def consume(self):
        self.appends += 1
        if self.remaining is not None:
            self.remaining -= 1


class _FaultStream:
    """A real stream whose ``write`` tears and whose ``truncate`` may refuse."""

    def __init__(self, raw, *, binary, fault):
        self._raw = raw
        self._binary = binary
        self._fault = fault

    def write(self, data):
        if not self._fault.armed(data):
            return self._raw.write(data)
        payload = data if self._binary else data.encode("utf-8")
        self._raw.write(payload[:self._fault.partial_bytes])
        self._fault.consume()
        try:
            self._raw.flush()
        except Exception:  # noqa: BLE001 - the tear is the point
            pass
        raise OSError("simulated torn evidence append")

    def truncate(self, size=None):
        if self._fault.fail_truncate:
            self._fault.truncates += 1
            raise OSError("simulated failed truncate")
        return self._raw.truncate(size)

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(self, *exc):
        return self._raw.__exit__(*exc)

    def __iter__(self):
        return iter(self._raw)

    def __getattr__(self, name):
        return getattr(self._raw, name)


def _install_fault(monkeypatch, fault):
    """Patch ``io.open`` (pathlib and plain) and ``os.truncate`` under the path."""
    import builtins

    real_open = io.open
    real_truncate = os.truncate

    def fake_open(file, mode="r", *args, **kwargs):
        raw = real_open(file, mode, *args, **kwargs)
        if fault.matches(file):
            return _FaultStream(raw, binary="b" in mode, fault=fault)
        return raw

    def fake_truncate(path, length):
        if fault.fail_truncate and fault.matches(path):
            fault.truncates += 1
            raise OSError("simulated failed truncate")
        return real_truncate(path, length)

    monkeypatch.setattr(io, "open", fake_open)
    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(os, "truncate", fake_truncate)
    return fault


def _open_spy(monkeypatch):
    """Record ``(path, mode)`` of every open made through ``io.open``/``open``."""
    import builtins

    opened: list[tuple[str, str]] = []
    real_open = io.open

    def fake_open(file, mode="r", *args, **kwargs):
        try:
            opened.append((os.path.normcase(os.path.abspath(os.fspath(file))), mode))
        except TypeError:
            pass
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(io, "open", fake_open)
    monkeypatch.setattr(builtins, "open", fake_open)
    return opened


def _read_opens(opened, path):
    target = os.path.normcase(os.path.abspath(os.fspath(path)))
    return [(name, mode) for name, mode in opened
            if name == target and ("r" in mode and "+" not in mode)]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """No cached record signature/index and no process-local cleared history."""
    # Frozen evidence names physical Windows files. On WSL an E:\\ path is
    # otherwise interpreted as a relative POSIX basename, making a receipt-only
    # base row acquire the clone's cwd. Emulate the fixture's filesystem, not a
    # normalization of output: writer, fold and accumulator share this identity.
    native = record.canonical_carto_file
    def fixture_file(value):
        text = str(value) if value is not None else ""
        return ntpath.realpath(text) if ntpath.splitdrive(text)[0] else native(value)
    monkeypatch.setattr(record, "canonical_carto_file", fixture_file)
    monkeypatch.setattr(support, "canonical_carto_file", fixture_file)
    record._record_cache.clear()
    record._record_ids.clear()
    record._path_ids.clear()
    record._detail_cache.clear()
    cfg._CARTO_CLEARED_JOB_IDS.clear()
    cfg._CARTO_CLEARED_JOB_PATHS.clear()
    cfg._CARTO_CLEARED_WRITE_LOGGED.clear()
    yield
    record._record_cache.clear()
    record._record_ids.clear()
    record._path_ids.clear()
    record._detail_cache.clear()
    cfg._CARTO_CLEARED_JOB_IDS.clear()
    cfg._CARTO_CLEARED_JOB_PATHS.clear()
    cfg._CARTO_CLEARED_WRITE_LOGGED.clear()


# ---------------------------------------------------------------------------
# §2.4 line grammar and B1's ASCII escaping
# ---------------------------------------------------------------------------

def test_evidence_line_is_one_compact_newline_terminated_object_keeping_every_value(tmp_path):
    """One compact JSON object per line; unknown keys, ``None`` versus ``[]``/``{}`` kept.

    Would fail before: no writer exists; a pretty-printed or value-normalising
    serializer would break the byte grammar and the ``None`` versus ``[]`` MUST.
    """
    writer, job_file, _ = _new_writer(tmp_path)
    row = {
        "file": "E:\\work\\flat\\grammar.py",
        "status": "refreshed",
        "changed": True,
        "findings": [],
        "warnings": None,
        "events": [{"type": "node_created", "detail": "two\nlines"}],
        "projection": None,
        "reconcile_children": [],
        "unknown_top_level": {"nested": [1, 2, {"deep": None}]},
        "empty_mapping": {},
    }
    assert writer.append_file_row(row) is True

    raw = _files_bytes(job_file)
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1                  # the row's own newline is escaped
    assert b"\\n" in raw
    line = raw[:-1].decode("utf-8")
    entry = json.loads(line)
    assert list(entry) == ["k", "sec", "row"]
    assert entry["k"] == "row" and entry["sec"] == "files"
    assert list(entry["row"]) == list(row)          # key order preserved
    assert _expand(entry) == _marked(row)           # only D3-1's documented outline marker differs
    assert isinstance(entry["row"]["warnings"], type(None))
    assert entry["row"]["findings"] == [] and entry["row"]["empty_mapping"] == {}
    # The one encoder: compact separators, no ``ensure_ascii=False`` divergence.
    assert raw[:-1] == json.dumps(entry, separators=(",", ":"), ensure_ascii=True).encode()


def test_lone_surrogate_is_ascii_escaped_not_written_raw(tmp_path):
    """B1/CF-167: default ASCII escaping, so a lone surrogate survives as an escape.

    Would fail before: no writer; an ``ensure_ascii=False`` encoder writes a lone
    surrogate as invalid UTF-8 and the line cannot be read back.
    """
    writer, job_file, _ = _new_writer(tmp_path)
    row = _row("E:\\work\\flat\\surrogate.py", message="broken \ud800 half", findings=[])
    assert writer.append_file_row(row) is True

    raw = _files_bytes(job_file)
    assert all(byte < 0x80 for byte in raw)          # ASCII-only bytes
    assert b"\\ud800" in raw                         # the escape, not the code point
    entry = json.loads(raw.decode("ascii"))
    assert entry["row"]["message"] == "broken \ud800 half"


def test_writer_marks_the_planned_outline_without_losing_null_versus_empty(tmp_path):
    """Planned G4 flip: D3-1 keeps None vs [] and list length/digest.

    Would fail at 157041c: all three values were still stored as whole outlines.
    The original no-marker reader fixtures remain unchanged.
    """
    writer, job_file, _ = _new_writer(tmp_path)
    assert writer.append_file_row(_row("a.py", reconcile_children=None)) is True
    assert writer.append_file_row(_row("b.py", reconcile_children=[])) is True
    assert writer.append_file_row(
        _row("c.py", reconcile_children=[{"name": "x", "children": []}])) is True
    stored = [_expand(json.loads(line)) for line in _lines(
        Path(job_file).with_suffix("") / "files.jsonl")]
    assert [row["reconcile_children"] for row in stored] == [
        _marked({"reconcile_children": value})["reconcile_children"]
        for value in (None, [], [{"name": "x", "children": []}])]
    assert stored[0]["reconcile_children"] == {"$reconcile_children": None}
    assert stored[1]["reconcile_children"]["$reconcile_children"]["count"] == 0
    assert stored[2]["reconcile_children"]["$reconcile_children"]["count"] == 1


# ---------------------------------------------------------------------------
# §2.4 rule 3: readers trust complete lines only
# ---------------------------------------------------------------------------

def test_torn_tail_is_ignored_and_reported_never_a_refusal(tmp_path):
    """A partial tail after the last newline is ignored and counted as bytes.

    Would fail before: no writer; a reader that trusts the last chunk would
    either crash or silently merge a partial row into another job's evidence.
    """
    writer, job_file, job_id = _new_writer(tmp_path)
    assert writer.append_file_row(_row("E:\\work\\a.py")) is True
    sidecar = Path(job_file).with_suffix("") / "files.jsonl"
    committed = sidecar.read_bytes()
    with sidecar.open("ab") as handle:
        handle.write(b'{"k":"row","sec":"files","row":{"file":')

    detail = _joined_detail(job_file, job_id)
    assert detail["torn_tail_bytes"] == len(b'{"k":"row","sec":"files","row":{"file":')
    assert [row["file"] for row in detail["result_summary"]["files"]] == ["E:\\work\\a.py"]
    assert sidecar.read_bytes() == committed + b'{"k":"row","sec":"files","row":{"file":'


# ---------------------------------------------------------------------------
# §2.4 rule 5 / B1: append faults, pending rows and their retry
# ---------------------------------------------------------------------------

def test_failed_append_restores_the_committed_bytes_and_keeps_one_pending_line(tmp_path, monkeypatch):
    """A failed append truncates back to the committed size; the row stays pending.

    Would fail before: no writer; without the truncate a torn partial line would
    sit in the evidence file and the next append would extend it.
    """
    writer, job_file, _ = _new_writer(tmp_path)
    assert writer.append_file_row(_row("E:\\work\\first.py")) is True
    sidecar = Path(job_file).with_suffix("") / "files.jsonl"
    before = sidecar.read_bytes()

    fault = _install_fault(monkeypatch, _Fault(sidecar, partial_bytes=23, times=1))
    poisoned = _row("E:\\work\\poison.py", findings=[{"code": "empty_comment"}])
    assert writer.append_file_row(poisoned) is False
    assert fault.appends == 1
    assert sidecar.read_bytes() == before                    # byte-identical

    pending = writer.pending_evidence_lines()
    assert len(pending["files"]) == 1
    entry = pending["files"][0]
    assert entry["k"] == "row" and entry["sec"] == "files"
    assert _expand(entry) == poisoned
    # The contribution is counted exactly once even though the line is not yet durable.
    summary = writer.summary()
    assert summary["successful"] == 2
    assert summary["findings_total"] == 1
    assert summary["files_changed_total"] == 0


def test_pending_row_is_reappended_before_the_next_append_without_blocking_it(tmp_path, monkeypatch):
    """§2.4/§B1(2): the pending row is retried first, then the new row lands.

    Would fail before: no writer; a writer that blocks the next append would
    stall the whole refresh over one transient IO fault, and one that drops the
    pending row would lose that file's complete evidence.
    """
    writer, job_file, _ = _new_writer(tmp_path)
    sidecar = Path(job_file).with_suffix("") / "files.jsonl"
    fault = _install_fault(monkeypatch, _Fault(sidecar, partial_bytes=11, times=1))

    first = _row("E:\\work\\retry-a.py", changed=True)
    second = _row("E:\\work\\retry-b.py")
    assert writer.append_file_row(first) is False
    fault.disabled = True
    assert writer.append_file_row(second) is True

    assert _files_bytes(job_file) == (
        json.dumps({"k": "row", "sec": "files", "row": first},
                   separators=(",", ":"), ensure_ascii=True).encode() + b"\n" +
        json.dumps({"k": "row", "sec": "files", "row": second},
                   separators=(",", ":"), ensure_ascii=True).encode() + b"\n")
    assert writer.pending_evidence_lines()["files"] == []

    stored = _load(job_file)
    assert stored["evidence"]["files"]["rows"] == 2
    assert stored["evidence"]["files"]["pending"] == 0
    summary = writer.summary()
    assert summary["successful"] == 2 and summary["files_changed_total"] == 1


def test_failed_truncate_makes_the_next_append_start_on_a_fresh_line(tmp_path, monkeypatch):
    """§2.4 rule 5: when the truncate also fails, the next append is ``\\n``-prefixed.

    Would fail before: no writer; without the prefix the torn bytes merge into
    the next complete line, corrupting a row that would otherwise decode.
    """
    writer, job_file, _ = _new_writer(tmp_path)
    sidecar = Path(job_file).with_suffix("") / "files.jsonl"
    fault = _install_fault(monkeypatch, _Fault(sidecar, partial_bytes=17,
                                               fail_truncate=True, times=1))
    torn = _row("E:\\work\\torn-a.py")
    assert writer.append_file_row(torn) is False
    assert fault.appends == 1 and fault.truncates >= 1

    fault.fail_truncate = False
    fault.disabled = True
    assert writer.append_file_row(_row("E:\\work\\torn-b.py")) is True

    raw = _files_bytes(job_file)
    expected_torn = json.dumps({"k": "row", "sec": "files", "row": torn},
                               separators=(",", ":"), ensure_ascii=True).encode()[:17]
    assert raw.startswith(expected_torn + b"\n")
    parsed = record.parse_carto_evidence_lines(raw)
    assert parsed["unreadable_lines"] == 1
    assert parsed["torn_tail_bytes"] == 0
    folded = record.fold_carto_job_evidence(raw)
    assert [row["file"] for row in folded["rows"]["files"]] == [
        "E:\\work\\torn-a.py", "E:\\work\\torn-b.py"]
    assert writer.pending_evidence_lines()["files"] == []


def test_row_pending_after_finalize_is_served_by_detail_and_named_by_one_warning(tmp_path, monkeypatch, caplog):
    """B1(3)/CF-167: the forever-pending row survives finalize in memory and detail.

    Would fail before: no writer; a finalize that drops the un-appendable row
    loses that file's complete evidence, and one that blocks would never finish.
    """
    caplog.set_level(logging.WARNING)
    writer, job_file, job_id = _new_writer(tmp_path)
    sidecar = Path(job_file).with_suffix("") / "files.jsonl"
    _install_fault(monkeypatch, _Fault(sidecar, partial_bytes=7, poison="pending.py"))
    pending_row = _row("E:\\work\\pending.py", changed=True,
                       findings=[{"code": "empty_comment", "severity": "info",
                                  "message": "kept", "line": 3, "finding_id": "f-1"}])
    assert writer.append_file_row(pending_row) is False
    # A later appendable row is never blocked by the pending one.
    assert writer.append_file_row(_row("E:\\work\\landed.py")) is True

    terminal = writer.finalize({"status": "completed_with_warnings",
                                "progress": {"total_files": 2, "completed_files": 2}})
    assert isinstance(terminal, dict)
    # B1's one named exception: the serialized complete line travels in memory.
    assert terminal.get("_pending_evidence")
    pending_lines = (terminal["_pending_evidence"].get("files") or [])
    decoded = [json.loads(line) if isinstance(line, str) else line for line in pending_lines]
    assert any(_expand(entry)["file"] == pending_row["file"] for entry in decoded)
    assert len(writer.pending_evidence_lines()["files"]) == 1
    stored = _load(job_file)
    assert stored["evidence"]["files"]["pending"] == 1
    # Exactly one warning names the file: B1(3) does not say which carrier, so a
    # record/terminal warning entry and a WARNING log line are both accepted.
    entries = {json.dumps(warning, sort_keys=True)
               for warning in list(terminal.get("warnings") or []) + list(stored.get("warnings") or [])
               if "pending.py" in json.dumps(warning)}
    logged = [record_ for record_ in caplog.records
              if record_.levelno >= logging.WARNING and "pending.py" in record_.getMessage()]
    assert len(entries) + len(logged) == 1

    detail = _joined_detail(job_file, job_id, overlay={**terminal, "path": str(job_file)})
    rows = {row["file"]: row for row in detail["result_summary"]["files"]}
    assert pending_row["file"] in rows
    assert rows[pending_row["file"]]["findings"] == pending_row["findings"]
    assert rows[pending_row["file"]]["changed"] is True
    assert "E:\\work\\landed.py" in rows
    assert "_pending_evidence" not in json.dumps(detail)


def test_crash_between_append_and_record_write_shows_the_row_and_rows_on_disk(tmp_path, monkeypatch):
    """§2.4 rule 6: a landed line is never hidden by a stale ``evidence.rows`` hint.

    Would fail before: no writer; detail derived from the record alone would
    report 0 rows while a complete row sits in `files.jsonl`.
    """
    writer, job_file, job_id = _new_writer(tmp_path)
    real_write = writer._write_record
    state = {"calls": 0}

    def flaky(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            return False                       # the record write is lost
        return real_write(*args, **kwargs)

    monkeypatch.setattr(writer, "_write_record", flaky)
    row = _row("E:\\work\\crash.py", changed=True)
    writer.append_file_row(row)                # the append itself succeeds

    stored = _load(job_file)
    assert stored["evidence"]["files"]["rows"] == 0      # stale hint
    assert writer.pending_evidence_lines()["files"] == []  # nothing pending: it landed
    detail = _joined_detail(job_file, job_id)
    assert [item["file"] for item in detail["result_summary"]["files"]] == ["E:\\work\\crash.py"]
    assert detail["rows_on_disk"] == 1
    assert detail["result_summary"]["files"][0]["changed"] is True


# ---------------------------------------------------------------------------
# §2.6 single-store projection
# ---------------------------------------------------------------------------

def test_projection_is_single_stored_only_when_every_projected_key_is_equal(tmp_path):
    """§2.6: equal mapping -> null + ``proj``; a mismatch stays explicit; order kept.

    Would fail before: no writer; a lossy single store would serve a projection
    that differs from the row (or re-expand at the wrong key position).
    """
    writer, job_file, _ = _new_writer(tmp_path)
    equal = _row("E:\\work\\equal.py", changed=True, status="refreshed", extra={"k": 1})
    equal["projection"] = {"changed": True, "status": "refreshed"}
    mismatched = _row("E:\\work\\mismatch.py", changed=True)
    mismatched["projection"] = {"changed": False}
    empty = _row("E:\\work\\empty.py")
    empty["projection"] = {}
    genuine_null = _row("E:\\work\\null.py")
    genuine_null["projection"] = None
    for row in (equal, mismatched, empty, genuine_null):
        assert writer.append_file_row(row) is True

    lines = [json.loads(line) for line in _lines(
        Path(job_file).with_suffix("") / "files.jsonl")]
    by_file = {entry["row"]["file"]: entry for entry in lines}

    stored_equal = by_file[equal["file"]]
    assert list(stored_equal) == ["k", "sec", "row", "proj"]
    assert stored_equal["proj"] == ["changed", "status"]
    assert stored_equal["row"]["projection"] is None
    assert list(stored_equal["row"]) == list(equal)          # original key position

    stored_mismatch = by_file[mismatched["file"]]
    assert "proj" not in stored_mismatch
    assert stored_mismatch["row"]["projection"] == {"changed": False}

    stored_empty = by_file[empty["file"]]
    assert stored_empty["proj"] == []
    assert stored_empty["row"]["projection"] is None

    stored_null = by_file[genuine_null["file"]]
    assert "proj" not in stored_null
    assert stored_null["row"]["projection"] is None

    # §2.6 round trip: re-expansion reproduces every original row byte for byte.
    for row in (equal, mismatched, empty, genuine_null):
        assert _expand(by_file[row["file"]]) == row
        assert json.dumps(_expand(by_file[row["file"]]), separators=(",", ":"),
                          ensure_ascii=True) == json.dumps(row, separators=(",", ":"),
                                                           ensure_ascii=True)


# ---------------------------------------------------------------------------
# G2's frozen fixtures are the writer's byte oracle
# ---------------------------------------------------------------------------

def _replay(writer, folder, fixture_record, *, finish=True):
    """Feed one frozen evidence pair through the writer's public API, in line order.

    Projections are expanded per line before the append (§2.6 re-expands per line
    before the overlay), so the writer re-derives the single store itself.  The
    placeholder is set after the lines, exactly where the frozen record's
    ``base_lines`` says it was created, and the terminal payload carries the
    component's non-aggregate summary values (§2.2 item 6) while the record-only
    aggregates and ``preview_totals`` are left for the writer to compute.
    """
    parsed = [json.loads(line) for line in _lines(folder / "files.jsonl")]
    nodes = [json.loads(line) for line in _lines(folder / "nodes.jsonl")] \
        if (folder / "nodes.jsonl").exists() else []
    for entry in parsed:
        if entry["k"] == "row":
            assert writer.append_file_row(
                _expand(entry), section=entry["sec"], delta=entry.get("delta"),
                nodes_reflected=entry.get("nodes")) is True
        elif entry["k"] == "item":
            writer.append_item(entry["key"], entry["v"])
        elif entry["k"] == "set":
            key = tuple(entry["key"]) if isinstance(entry["key"], list) else entry["key"]
            writer.set_value(key, entry["v"])
        else:  # pragma: no cover - the grammar has no other kind
            raise AssertionError(f"unexpected evidence line {entry['k']}")
    if nodes:
        assert writer.append_node_rows(
            [{"file": entry["file"], "row": entry["row"]} for entry in nodes]) is True
    if fixture_record.get("in_flight"):
        placeholder = {key: copy.deepcopy(value) for key, value in
                       fixture_record["in_flight"].items() if key != "base_lines"}
        assert writer.set_in_flight(placeholder) is True
    if not finish:
        return None
    return writer.finalize(_terminal_payload(fixture_record))


def _head_of(fixture_record):
    """The record's own small fields, exactly as the job carried them (§2.2 item 3)."""
    return {key: copy.deepcopy(value) for key, value in fixture_record.items()
            if key not in WRITER_OWNED_KEYS}


def _terminal_payload(fixture_record):
    """The head plus the component summary values the writer must keep.

    Every value the record *derives* from evidence (``DERIVED_SUMMARY_KEYS``) is
    withheld, so the fixture's record proves the writer produced it.  An
    **empty** preview list is kept because an empty run list has no ``item``
    line: its presence is the component's fact (B8/CF-159); a non-empty one is
    carried completely by its ``item`` lines and is not supplied.
    """
    payload = _head_of(fixture_record)
    summary = {}
    for key, value in (fixture_record.get("result_summary") or {}).items():
        if key in DERIVED_SUMMARY_KEYS:
            continue
        if isinstance(value, list) and value:
            continue
        summary[key] = copy.deepcopy(value)
    payload["result_summary"] = summary
    return payload


def _replayed(tmp_path, fixture, *, finish=True):
    folder = FIXTURES / fixture
    fixture_record = _load(folder / "record.json")
    job_file = _job_file(tmp_path, job_id=fixture_record["id"])
    writer = _writer_class()(job_file)
    assert writer.create(_head_of(fixture_record)) is True
    _replay(writer, folder, fixture_record, finish=finish)
    return writer, job_file


@pytest.mark.parametrize("fixture", EVIDENCE_FIXTURES)
def test_writer_lines_are_byte_identical_to_every_frozen_fixture(tmp_path, fixture):
    """§3.2: every frozen ``files.jsonl`` / ``nodes.jsonl`` is reproduced byte for byte.

    Would fail before: no writer; any divergence from §2.4/§2.6 (metadata order
    ``k/sec/row/nodes/delta/proj``, ASCII escaping, single store, delta overlay
    rendering) changes those bytes.
    """
    folder = FIXTURES / fixture
    _, job_file = _replayed(tmp_path, fixture)
    assert _files_bytes(job_file) == _bytes(folder / "marker.files.jsonl")
    assert _nodes_bytes(job_file) == (
        _bytes(folder / "nodes.jsonl") if (folder / "nodes.jsonl").exists() else b"")


@pytest.mark.parametrize("fixture", RECORD_FIXTURES)
def test_writer_record_matches_its_independent_counterpart(tmp_path, fixture):
    """§3.2 + B5: the written record equals the builder's writer counterpart.

    ``writer.record.json`` is built by the independent fixture builder (never
    from writer output) and is the record oracle: Q4 ruled that the frozen G2
    ``record.json`` stays the replay input and the reader golden, while B5's
    exact ``warning_count``, the count-bounded inline previews and the row-line
    hint are compared against this counterpart instead.

    Would fail before: no writer; every value below is derived from the frozen
    evidence -- the record-only aggregates (``errors_total``,
    ``findings_by_severity``, ``files_changed_total``, ``events_total``,
    ``phase8_summary``), the previews' ``preview_totals`` and B5's warning
    fields -- so copying the component summary cannot satisfy it.

    The interrupted bulk fixture is NOT exempted: its counterpart is the
    up-to-date writer state over the landed rows and receipts (Q4(3)), so a
    stale terminal-only summary cannot pass either.
    """
    folder = FIXTURES / fixture
    _, job_file = _replayed(tmp_path, fixture)
    written, expected = _record_parity(_load(job_file), _load(folder / WRITER_RECORD))
    assert written == expected                       # every value, key order independent
    # §2.2 pins the top-level key order (head, other fields, evidence, in_flight,
    # result_summary last); the order *inside* result_summary is the builder's
    # stated choice, so it is not asserted here.
    assert list(written) == list(expected)


@pytest.mark.parametrize("fixture", RECORD_FIXTURES)
def test_every_fixture_carries_its_builder_counterpart_b5_and_counts(fixture):
    """Q4: all five fixtures have a counterpart obeying B5 and the row-line hint.

    The counterpart is a NEW committed record beside every frozen one; the
    originals (``record.json`` and both sidecars) remain untouched reader
    fixtures.  ``warning_count`` is the exact ``warnings_total`` on every write
    and ``evidence.files.rows`` counts every ``row`` line landed, both sections.

    Would fail before: no counterpart exists, and the frozen folder records
    carry no `warning_count`/`warnings` at all while the Markdown fixture's hint
    stops at its primary row.
    """
    folder = FIXTURES / fixture
    counterpart = _load(folder / WRITER_RECORD)
    lines = [json.loads(line) for line in _lines(folder / "marker.files.jsonl")]
    rows = [line for line in lines if line["k"] == "row"]
    assert counterpart["record_layout"] == "summary-head/2"
    assert counterpart["warning_count"] == counterpart["result_summary"]["warnings_total"]
    assert len(counterpart["warnings"]) <= 200
    assert counterpart["evidence"]["files"]["rows"] == len(rows)
    assert counterpart["evidence"]["files"]["lines"] == len(lines)
    assert counterpart["evidence"]["files"]["bytes"] == (folder / "marker.files.jsonl").stat().st_size
    # The original G2 record stays a readable /2 reader fixture.
    assert _load(folder / "record.json")["record_layout"] == "summary-head/2"


def test_oversized_preview_stays_inline_with_no_preview_set_line(tmp_path):
    """Q4(1): a count-bounded preview stays inline however large it is encoded.

    `folder_refresh_trim`'s 20-entry `findings_preview` is 7,543 B.  §2.2's 4 KiB
    move is for values with **no count bound** (a launch field, `error_extra`,
    `infrastructure_error`, `publication`, …), and Q4(1) ruled that the record's
    count-bounded aggregates and previews stay inline whatever their size.  So
    the record carries the preview itself, no preview `set` line is written, and
    the evidence file stays exactly the frozen 30 lines / 930,786 bytes.

    Would fail before: the draft writer applied the move to the preview, adding
    one `set` line per preview growth (33 lines / 948,777 B) and replacing the
    preview with an `{"$evidence": ...}` marker -- an incompatible carrier, and
    a normalization the strict oracles no longer hide.
    """
    folder = FIXTURES / "folder_refresh_trim"
    fixture = _load(folder / "record.json")
    inline = fixture["result_summary"]["findings_preview"]
    inline_bytes = len(json.dumps(inline, separators=(",", ":"), ensure_ascii=True).encode())
    assert inline_bytes == 7543
    assert fixture["evidence"]["files"]["lines"] == 30

    _, job_file = _replayed(tmp_path, "folder_refresh_trim")
    written = _load(job_file)
    assert written["result_summary"]["findings_preview"] == inline
    assert "$evidence" not in json.dumps(written["result_summary"]["findings_preview"])
    assert written["result_summary"]["preview_totals"] == fixture["result_summary"]["preview_totals"]

    sidecar = [json.loads(line) for line in _lines(
        Path(job_file).with_suffix("") / "files.jsonl")]
    assert [line for line in sidecar if line["k"] == "set"] == []
    assert written["evidence"] == _load(folder / WRITER_RECORD)["evidence"]
    assert written["evidence"]["files"]["lines"] == 30  # marker changes bytes, not line count
    assert _files_bytes(job_file) == _bytes(folder / "marker.files.jsonl")
    detail = _joined_detail(job_file, fixture["id"])
    assert "findings_preview" not in detail["result_summary"]


def test_funnel_caps_every_explicit_v2_write_even_without_an_existing_record(tmp_path):
    """The existing API's first-write B5 case, independent of the new class."""
    path = _job_file(tmp_path, job_id="funnel-first")
    warnings = [{"type": "test", "message": str(index)} for index in range(250)]
    payload = {"id": "funnel-first", "record_layout": "summary-head/2", "status": "running",
               "warnings": warnings, "result_summary": {"warnings_total": 500}}
    assert cfg.merge_write_carto_job_status(path, payload)
    stored = _load(path)
    assert len(stored["warnings"]) == 200
    assert stored["warning_count"] == 500
    assert stored["record_layout"] == "summary-head/2"


def test_interrupted_bulk_stop_record_matches_its_counterpart_whole(tmp_path):
    """Q4(3): the synthetic stop record equals its up-to-date counterpart whole.

    The frozen ``record.json`` here is a READER-ONLY crash fixture (its summary
    was composed from the files that record held, with no row line for the
    interrupted file).  The counterpart is the writer's actual state over the
    7 landed rows and 7 receipts: the interrupted file enters the summary as the
    fold's receipt-only base row, so ``successful`` is 3 and ``files_changed``
    holds 4 entries, while the crash head and the ``in_flight`` placeholder stay
    exactly the frozen fixture's.

    Would fail before: no writer; a terminal write derived from the frozen
    record alone keeps the stale 2-successful / 3-changed summary, and one that
    drops ``in_flight`` loses the interrupted file's representation (§2.5).
    """
    folder = FIXTURES / "bulk_receipts_synthetic"
    original = _load(folder / "record.json")
    _, job_file = _replayed(tmp_path, "bulk_receipts_synthetic")
    written, expected = _record_parity(_load(job_file), _load(folder / WRITER_RECORD))
    assert written == expected                          # the whole record, timestamps only
    assert list(written) == list(expected)
    assert written["evidence"] == expected["evidence"]
    assert written["in_flight"] == expected["in_flight"] == _load(
        folder / "record.json")["in_flight"]            # the placeholder, keys in order
    # The frozen record's complete folded detail stays a working reader oracle.
    detail = _joined_detail(job_file, original["id"])
    fixture_detail = record.stitch_carto_job_detail(
        original, record.fold_carto_job_evidence(
            _bytes(folder / "files.jsonl"), in_flight=original.get("in_flight"),
            status=original.get("status"), nodes=_bytes(folder / "nodes.jsonl"),
            job_type=original.get("type")))
    assert detail["result_summary"]["files"] == [_marked(row) for row in fixture_detail["result_summary"]["files"]]


@pytest.mark.parametrize("fixture", RECORD_FIXTURES)
def test_writer_summary_equals_the_batch_composition_for_the_frozen_fixtures(tmp_path, fixture):
    """§2.7 equality on real data: the writer's summary equals the batch composition.

    Would fail before: no writer/accumulator; an incremental form that drifts
    from ``aggregate_beacon_outcomes`` + ``beacon_job_outcome`` on real rows
    (status classes, findings multiplicities, phase-8 rows) is exactly the bug
    this property pins.
    """
    folder = FIXTURES / fixture
    fixture_record = _load(folder / "record.json")
    writer, _ = _replayed(tmp_path, fixture, finish=False)
    folded = record.fold_carto_job_evidence(
        _bytes(folder / "files.jsonl"), in_flight=None,
        status=fixture_record.get("status"), nodes=_fixture_nodes(folder),
        job_type=fixture_record.get("type"))
    items = folded["items"]
    phase8 = folded["values"].get("phase8_envelope")
    if folded["rows"].get("phase8"):
        phase8 = dict(phase8 or {})
        phase8["files"] = copy.deepcopy(folded["rows"]["phase8"])
    summary = fixture_record.get("result_summary") or {}
    batch = beacon_job_outcome(
        aggregate_beacon_outcomes(folded["rows"].get("files") or [],
                                  warnings=items.get("run_warnings", ()),
                                  events=items.get("run_events", ())),
        infrastructure_error=summary.get("infrastructure_error"),
        cancelled=bool(summary.get("cancelled")), phase8=phase8, owns_aggregate=True)
    actual = writer.summary(infrastructure_error=summary.get("infrastructure_error"),
                            cancelled=bool(summary.get("cancelled")))
    for key in ("status", "successful", "failed", "skipped", "partial_success", "success",
                "cancelled", "warnings_total", "warnings_by_type", "warnings_truncated",
                "findings_total", "findings_by_code", "findings_truncated", "changed",
                "summary_line"):
        assert actual.get(key) == batch.get(key), key
    # summary() classifies the rows/receipts actually put, not a terminal-only
    # folded placeholder that the caller has not appended. Stale fixture hints
    # are tested by the strict record oracle, never copied into this property.
    phase_failed = bool(phase8 is not None and (phase8.get("success") is False or phase8.get("error")))
    node_errors = sum(receipt.get("success") is False or bool(receipt.get("error"))
                      for receipt in folded["node_outcomes"])
    assert actual.get("errors_total") == batch["failed"] + node_errors + int(phase_failed)
    primary_files = {row["file"] for row in batch["files"]}
    findings = [finding for row in batch["files"] for finding in row["findings"]]
    findings.extend(finding for row in (phase8 or {}).get("files", []) if row["file"] not in primary_files
                    for finding in row.get("findings", []))
    assert actual.get("findings_by_severity") == dict(sorted(Counter(
        str(finding.get("severity") or "info") for finding in findings).items()))
    assert actual.get("events_total") == len(batch["events"])
    assert actual.get("files_changed_total") == len(batch["files_changed"])
    if "phase8_summary" in summary:
        assert actual.get("phase8_summary") == summary.get("phase8_summary")


# ---------------------------------------------------------------------------
# §2.2 size invariant, §2.12 memory, §2.8 no evidence read
# ---------------------------------------------------------------------------

def test_record_size_is_flat_over_two_thousand_appends_without_touching_evidence(tmp_path, monkeypatch):
    """2,000 appends: flat record bytes, no sidecar read-open, no retained row.

    Would fail before: no writer; a record that re-aggregated or re-read the run
    would grow with the file index, and one that kept completed rows would keep
    the weak references alive (§2.12).
    """
    writer, job_file, job_id = _new_writer(tmp_path)
    sidecar = Path(job_file).with_suffix("") / "files.jsonl"
    opened = _open_spy(monkeypatch)
    refs = []
    size_at_50 = None
    sampled = []
    for index in range(2000):
        row = _WeakRow(_row(f"E:\\work\\flat\\f{index:04d}.py"))
        refs.append(weakref.ref(row))
        assert writer.append_file_row(row) is True
        del row
        if index == 49:
            size_at_50 = job_file.stat().st_size
        elif index >= 49 and (index + 1) % 100 == 0:
            sampled.append(job_file.stat().st_size)
    final = job_file.stat().st_size

    assert size_at_50 is not None
    assert final - size_at_50 <= 256, (size_at_50, final)
    assert max(sampled) - size_at_50 <= 256, (size_at_50, max(sampled))
    assert final < 64 * 1024                          # §2.2's typical bound
    assert _read_opens(opened, sidecar) == []          # zero read-opens during the run
    assert _json_count(_files_bytes(job_file)) == 2000
    gc.collect()
    alive = [index for index, ref in enumerate(refs) if ref() is not None]
    assert alive == []
    stored = _load(job_file)
    assert stored["evidence"]["files"]["rows"] == 2000
    assert stored["evidence"]["files"]["pending"] == 0


def _json_count(raw):
    return sum(1 for line in raw.split(b"\n") if line.strip())


# ---------------------------------------------------------------------------
# §2.8/CF-137 branch 13: a cleared record stays cleared
# ---------------------------------------------------------------------------

def test_cleared_job_is_not_recreated_by_a_late_terminal_write(tmp_path, caplog):
    """Clearing history keeps the record, log and folder absent; one INFO line says so.

    Would fail before: no writer; a late terminal write would recreate the
    record (and, through the evidence append, the folder) that Dan just cleared.
    """
    writer, job_file, job_id = _new_writer(tmp_path)
    assert writer.append_file_row(_row("E:\\work\\cleared.py")) is True
    job_dir = job_file.with_suffix("")
    log_path = job_file.with_suffix(".log")
    log_path.write_text("log\n", encoding="utf-8")
    assert job_dir.is_dir() and log_path.exists()

    base = job_file.parent
    assert writer.update_head(status="completed") is True
    stored = _load(job_file)
    job = {"id": stored["id"], "type": stored["type"], "status": stored["status"],
           "created_at": stored["created_at"], "logs_path": str(log_path)}
    assert srv._delete_carto_job_json_if_unclaimed(
        base, job_file, job, protected_ids=frozenset(), protected_paths=frozenset()) is True
    assert srv._delete_carto_job_artifacts(base, job_file, job)["errors"] == []
    assert not job_file.exists() and not job_dir.exists() and not log_path.exists()

    caplog.set_level(logging.INFO, logger="workflowy_mcp.config")
    writer.finalize({"status": "completed"})                      # the late write: no raise
    assert writer.update_head(status="failed") is False            # and a later one
    assert not job_file.exists()                                   # not recreated
    assert not job_dir.exists()
    assert not log_path.exists()
    cleared = [entry for entry in caplog.records
               if "cleared" in entry.getMessage().lower() and job_id in entry.getMessage()]
    assert len(cleared) == 1
    assert cleared[0].levelno == logging.INFO


# ---------------------------------------------------------------------------
# §2.8's record write: B5's cap, sticky status, current disk, read bound
# ---------------------------------------------------------------------------

def test_first_write_caps_the_warning_display_at_two_hundred_with_the_exact_total(tmp_path):
    """B5/CF-161: the cap applies to the first write, and ``warning_count`` is exact.

    Would fail before: no writer; the pre-G3 funnel only deduped/capped when the
    on-disk list was already non-empty, so a first write could persist 250.
    """
    writer, job_file, _ = _new_writer(tmp_path)
    warnings = [{"type": "warning", "message": f"w{index}"} for index in range(250)]
    assert writer.update_head(warnings=warnings, warning_count=250) is True
    stored = _load(job_file)
    assert stored["warnings"] == warnings[:200]
    assert stored["warning_count"] == 250

    # A warning-blind later write must not drop the display or the exact total.
    assert writer.update_head(progress={"total_files": 3, "completed_files": 1}) is True
    stored = _load(job_file)
    assert stored["warnings"] == warnings[:200]
    assert stored["warning_count"] == 250


def test_record_write_keeps_sticky_status_and_current_disk_unknown_keys_within_two_reads(tmp_path, monkeypatch):
    """§2.8: the write starts from the current disk record, keeps sticky status, ≤ 2 reads.

    Would fail before: no writer; a write built only from the writer's own state
    would erase a governance/claim key another writer added and could flip a
    sticky ``failed`` back to ``completed``.
    """
    writer, job_file, _ = _new_writer(tmp_path)
    stored = _load(job_file)
    stored.update({"status": "failed", "error": "governance refused",
                   "error_kind": "carto_job_exception",
                   "current_disk_unknown": {"from": "another writer"}})
    assert cfg.merge_write_carto_job_status(job_file, stored) is True

    opened = _open_spy(monkeypatch)
    assert writer.update_head(status="completed",
                              progress={"total_files": 4, "completed_files": 4}) is True
    assert _read_opens(opened, job_file) and len(_read_opens(opened, job_file)) <= 2
    sidecar_reads = [name for name, mode in opened
                     if name.endswith("files.jsonl") and "r" in mode]
    assert sidecar_reads == []
    written = _load(job_file)
    assert written["status"] == "failed"                       # sticky precedence
    assert written["current_disk_unknown"] == {"from": "another writer"}
    assert written["progress"]["completed_files"] == 4
