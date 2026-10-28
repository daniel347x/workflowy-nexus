#!/usr/bin/env python3
"""Golden job-record fixtures for the CARTO job-record wave (JR2).

This builder is the fixture *producer*: it turns a frozen source record (the
layout the daemon writes today -- `summary-head/1`, or legacy key order) into
its `summary-head/2` counterpart, the small record plus the append-only
evidence sidecars, following WaveSpec rev 3 §2.2-§2.6.

It is deliberately **independent of the production writer** (`CartoJobRecord`,
G3): the fixtures are G3's byte oracle, so nothing here may call that writer.
The one production dependency is the *read/compose* side that already exists
and that this wave does not change (`aggregate_beacon_outcomes` +
`beacon_job_outcome`), used to recompose a trimmed source record's summary so
the source is the record today's `save_summary` / `save_bulk_batch` /
`save_result` would have written for the same rows. Every rule the new layout
adds (§2.2's aggregates and previews, §2.3's outcome, §2.6's single store, the
line grammar) is implemented here from the contract text, never imported.

Two records per fixture
-----------------------
``record.json`` is the frozen G2 oracle the reader tests use; it is never
rewritten to a newer rule.  ``writer.record.json`` beside it is the corrected
oracle for the current G3 writer, built here and compared only against the
writer's own output: B5's exact ``warning_count`` and its bounded display on
every write, count-bounded previews inline (Q4(1)), the row-line hint counted
across every evidence section (Q4(4)) and, for the interrupted synthetic bulk
case, the up-to-date writer state over the landed rows and receipts (Q4(3)) --
the frozen G2 crash record stays a reader-only fixture.  Per-fixture reasons are
in ``WRITER_COUNTERPART_REASONS`` and ``writer_counterpart``'s docstring; the
sidecars are shared by both records and are never rebuilt by the counterpart.

Contract points this file implements
------------------------------------
* §2.4 line grammar: one compact JSON object per line, ``\\n``-terminated.
  Contract B1 (CF-167), which overrides §2.4 rule 1: evidence lines use JSON's
  **default ASCII escaping** (`ensure_ascii=True`), as every job-record writer
  does today.  :func:`compact` is the only encoder for lines.
* §2.6 single-store projection: a row whose ``projection`` mapping equals its
  top-level values key for key is stored with ``"projection": null`` **in its
  original key position** and ``"proj": [keys in order]`` beside ``row``.
  :func:`store_row` also requires the canonical encodings to be identical, so
  the re-expansion (:func:`expand_row`) reproduces the original row byte for
  byte, key order included -- the round trip the spec pins.  (On the archived
  1,063-row record both halves hold in 1,059 of 1,059 projecting rows, so the
  stricter test never changes what is stored.)
* §2.2 record: key order ``record_layout``, the head, the remaining top-level
  fields, ``evidence``, ``in_flight``, ``result_summary`` last; the summary
  carries every non-list value, the new aggregates, the previews and
  ``preview_totals``; a value over 4 KiB moves to a ``set`` line; the
  per-file/per-node lists never stay in the record.
* §2.5 receipt rule for bulk: a bulk file row stores ``node_outcomes`` as
  ``null`` at its key position and ``"nodes": N``; every receipt is a ``node``
  line naming its physical file; a later finalization of the same file is a
  ``delta`` line (``{"unset": [...], "extend": {...}}``) derived from the
  captured write sequence, never invented.
* Original G2/G3 fixtures keep the whole outline. G4's separate ``marker.*``
  counterparts apply D3-1 independently: null or count/digest, before projection
  equality; every finding's anatomy and every original fixture byte stay fixed.

Determinism
-----------
No timestamps are generated: every value comes from the source record, so two
runs produce identical bytes, and ``--check`` rebuilds everything in memory
and compares it with the committed files.

Usage
-----
    python3 tests/fixtures/carto_job_records/build_fixtures.py
    python3 tests/fixtures/carto_job_records/build_fixtures.py --check
    python3 tests/fixtures/carto_job_records/build_fixtures.py --only mixed_folder
    python3 tests/fixtures/carto_job_records/build_fixtures.py --trim-source <copy of the 1,063-row record>
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE_ROOT = HERE
SOURCES = HERE / "sources"
REPO_ROOT = HERE.parents[2]

LAYOUT_2 = "summary-head/2"
LAYOUT_1 = "summary-head/1"
PROJECTION_KEY = "projection"
PROJ_KEYS_KEY = "proj"
EVIDENCE_FILE = "files.jsonl"
NODES_FILE = "nodes.jsonl"
EVIDENCE_PLACEHOLDER = "$evidence"
# Q4: the writer's own oracle, built here beside the frozen G2 record.  The
# original ``record.json``/sidecars stay untouched and keep serving the reader
# tests; ``writer.record.json`` is the record the current G3 writer produces
# (B5's exact ``warning_count``, Q4(4)'s row-line hint, count-bounded previews
# inline, and the up-to-date bulk stop state).  See ``writer_counterpart``.
WRITER_RECORD_FILE = "writer.record.json"

# WaveSpec §2.2 head order for summary-head/2.
HEAD_ORDER_2 = (
    "id", "type", "mode", "root_uuid", "status", "outcome", "has_errors",
    "has_warnings", "has_findings", "error", "error_kind", "created_at",
    "updated_at", "progress", "warning_count", "warnings",
)
# summary-head/1's head (config._CARTO_JOB_RECORD_HEAD_KEYS; layout only).
HEAD_ORDER_1 = (
    "id", "type", "mode", "root_uuid", "status", "error", "error_kind",
    "created_at", "updated_at", "progress", "warning_count", "warnings",
)
# config.CARTO_RESULT_SUMMARY_TAIL_KEYS (layout only).
TAIL_KEYS_1 = (
    "warnings", "events", "files_changed", "present_paths", "findings",
    "phase8", "node_outcomes", "files",
)
# §2.4: run-level list keys that become `item` lines.
ITEM_KEYS = ("errors", "refresh_errors", "run_warnings", "run_events", "present_paths")
# §2.2/§2.7: failed-class file statuses (beacon_job_outcome's set).
FAILED_STATUSES = frozenset({"failed", "error", "unavailable", "parse_failed"})
# §2.2 previews and their caps.
PREVIEW_LIMITS = {"files_changed": 50, "errors": 20, "refresh_errors": 20,
                  "findings_preview": 20}
# CF-159/B8: a folder's other run-level lists (nexusignore_sources,
# missing_paths) keep a bounded preview in the record and their complete list in
# the evidence file.  `present_paths` is NOT one of them: §2.2 keeps it out of
# the structured head, and an empty one keeps its presence with a `set` line.
CF159_PREVIEW_LIMIT = 20
B8_PREVIEW_KEYS = ("nexusignore_sources", "missing_paths")
# The current writer's own preview caps (§2.2): a counterpart must use the caps
# of the record it predicts.  They differ from the G2 builder's only for the B8
# folder lists (50, not 20), and no frozen fixture reaches either bound.
WRITER_PREVIEW_LIMITS = {"files_changed": 50, "errors": 20, "refresh_errors": 20,
                         "findings_preview": 20,
                         "nexusignore_sources": 50, "missing_paths": 50}
# B5/CF-161: the head warning display is deduplicated and capped at 200.
WRITER_WARNING_DISPLAY_LIMIT = 200
# §2.7's accumulator owns these summary values; a counterpart recomputes them
# from the landed evidence and never copies a component or G2 record value.
WRITER_DERIVED_SUMMARY_KEYS = frozenset({
    "status", "success", "cancelled", "successful", "failed", "skipped",
    "partial_success", "changed", "findings_total", "findings_by_code",
    "findings_truncated", "warnings_total", "warnings_by_type", "warnings_truncated",
    "summary_line", "errors_total", "node_errors_total", "phase8_failed",
    "files_changed_total", "events_total", "findings_by_severity", "phase8_summary",
    "findings_preview", "files_changed", "preview_totals", "infrastructure_error",
    "phase8",
})
SLIM_FINDING_KEYS = ("code", "severity", "message", "file", "line", "finding_id")
# §2.2 large-value rule.
MOVE_LIMIT = 4096
# §2.6: the projection the archived folder record carries (its own key order).
FOLDER_PROJECTION_KEYS = (
    "state", "preserve_existing", "reconcile_children", "create_allowed",
    "delete_allowed", "sections", "retained_uuids", "retained_sections",
    "findings", "warnings", "reconcile_report", "ambiguities", "ambiguous_count",
)
SOURCE_MAX_BYTES = 2_000_000
TERMINAL_STATUSES = frozenset({"completed", "completed_with_warnings", "failed", "cancelled"})


# ---------------------------------------------------------------------------
# Encoding and JSON equality
# ---------------------------------------------------------------------------
def compact(value) -> str:
    """The ONE evidence-line encoder (§2.4 + B1: compact, ensure_ascii=True)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _line(obj) -> str:
    return compact(obj) + "\n"


def _structural(value):
    """Normalize a decoded JSON value so dict key order cannot affect equality.

    Every JSON kind is TAGGED before comparison: an object and an array are
    never equal even when both are empty, and a boolean is never equal to the
    number 1.  (A bare ``()`` for ``{}`` and ``[]``, or Python's ``True == 1``,
    would silently compact two different JSON values -- and a single-store
    ``projection: null`` would then re-expand to the wrong one.)
    """
    if isinstance(value, dict):
        return ("object", tuple(sorted((key, _structural(item)) for key, item in value.items())))
    if isinstance(value, list):
        return ("array", tuple(_structural(item) for item in value))
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", value)
    if isinstance(value, str):
        return ("string", value)
    if value is None:
        return ("null", None)
    return (type(value).__name__, value)


def same_json(left, right) -> bool:
    """Deep JSON equality (§2.6): dict key order never affects the answer."""
    return _structural(left) == _structural(right)


# §2.6's ONE criterion is literal deep JSON equality (``same_json``), so a
# deep-equal projection always single-stores -- empty mapping included
# (``proj: []``).  A nested VALUE whose own key order would not survive
# re-expansion is the only case where a null+proj store is not byte-lossless;
# such a row keeps its projection explicit (lossless) and the discrepancy is
# REPORTED here, never refused and never conditionally opted out.  The archived
# 1,063-row record has none (see the build log).
PROJECTION_ORDER_NOTES: list[str] = []
# Records whose job rows came from the builder's derivative rule rather than a
# captured aggregate INPUT list (see ``_job_rows``): reported by the build.
JOB_ROW_PROVENANCE_NOTES: list[str] = []
# Keys a finalization re-inserted (a pop followed by a set, which moves the key
# to the row's end); the row order proves it and it is reported here.
ROW_ORDER_NOTES: list[str] = []


# ---------------------------------------------------------------------------
# §2.6 single-store projection
# ---------------------------------------------------------------------------
def store_row(row, *, null_keys=()):
    """Return ``(stored_row, proj_keys_or_None)`` per §2.6.

    A mapping (an EMPTY one included) whose every key's value is deep-equal to
    the row's top-level value of that name is stored as ``null`` in its
    original key position with ``proj`` beside ``row`` (``proj: []`` for an
    empty mapping).  ``null_keys`` names values the grammar stores as ``null``
    at their key position for another reason (a bulk row's ``node_outcomes``,
    §2.4).
    """
    proj = row.get(PROJECTION_KEY)
    proj_keys = None
    if isinstance(proj, dict) and all(
        isinstance(key, str) and key in row and key not in null_keys
        and same_json(row[key], value) for key, value in proj.items()
    ):
        # §2.6's ONE criterion is deep JSON equality: such a row single-stores.
        # If a projected VALUE's own nested key order would not survive
        # re-expansion, that is REPORTED (below) -- never turned into a stricter
        # opt-out: "per-row key order" is the row's field order, and `proj` is
        # what preserves the projection's own key order.
        proj_keys = list(proj.keys())
        rebuilt = {key: row[key] for key in proj_keys}
        if compact(rebuilt) != compact(proj):
            PROJECTION_ORDER_NOTES.append(str(row.get("file")))
    stored = {}
    for key, value in row.items():
        stored[key] = None if (key in null_keys or (key == PROJECTION_KEY and proj_keys is not None)) else value
    return stored, proj_keys


def expand_row(stored_row, proj_keys):
    """Re-expand one stored row (§2.6): the inverse of :func:`store_row`.

    ``proj_keys is None`` means the line carried no ``proj`` marker (a genuine
    ``null`` projection); ``[]`` means an empty projection mapping came back.
    """
    if proj_keys is None:
        return copy.deepcopy(stored_row)
    expanded = {}
    for key, value in stored_row.items():
        if key == PROJECTION_KEY:
            expanded[key] = {name: copy.deepcopy(stored_row[name]) for name in proj_keys}
        else:
            expanded[key] = copy.deepcopy(value)
    return expanded


# ---------------------------------------------------------------------------
# Source-side composition (the read side this wave does not change)
# ---------------------------------------------------------------------------
def _composer():
    """Import the pinned batch composer from the repository under test."""
    source_dir = REPO_ROOT / "src"
    for path in (str(REPO_ROOT), str(source_dir)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from workflowy_mcp.beacon_refresh_support import (  # noqa: PLC0415
        aggregate_beacon_outcomes,
        beacon_job_outcome,
    )

    return aggregate_beacon_outcomes, beacon_job_outcome


def compose_source_summary(rows, *, run_warnings=(), run_events=(), phase8=None,
                           cancelled=False, counters=None):
    """The summary today's writer produces for ``rows`` (same call shape).

    Mirrors ``save_summary`` (folder), ``save_bulk_batch`` (bulk) and
    ``save_result`` (Markdown): aggregate the final rows with the run-level
    ``warnings=`` / ``events=`` arguments, then ``beacon_job_outcome``, then
    the caller's counters.
    """
    aggregate, outcome = _composer()
    summary = outcome(
        aggregate(rows, warnings=list(run_warnings), events=list(run_events)),
        phase8=phase8,
        cancelled=cancelled,
        owns_aggregate=True,
    )
    if counters:
        summary.update(counters)
    return summary


# ---------------------------------------------------------------------------
# §2.3 outcome and facets (independent implementation: a fixture cannot ask
# the reader it is used to test)
# ---------------------------------------------------------------------------
def derive_outcome(record):
    """§2.3: derive ``outcome`` and the three facets from status + counts.

    CF-174: a legacy `/1` Markdown record's phase-8 failure survives a head read
    only in ``result_summary.warnings_by_type.phase8_refresh_failed`` (``phase8``
    is a tail key), so that flag also counts as a phase-8 failure here.
    """
    summary = record.get("result_summary") or {}
    status = str(record.get("status") or "").strip().lower()
    phase8 = summary.get("phase8_summary") or summary.get("phase8") or {}
    phase8_failed = bool(isinstance(phase8, dict)
                         and (phase8.get("success") is False or phase8.get("error")))
    warning_types = summary.get("warnings_by_type")
    if isinstance(warning_types, dict) and int(warning_types.get("phase8_refresh_failed") or 0) > 0:
        phase8_failed = True
    errors_total = summary.get("errors_total")
    if errors_total is None:
        errors_total = (int(summary.get("failed") or 0)
                        + int(summary.get("node_errors_total") or 0)
                        + (1 if phase8_failed else 0))
    findings_total = int(summary.get("findings_total") or 0)
    warnings_total = int(summary.get("warnings_total") or 0)
    warning_count = int(record.get("warning_count") or 0)
    events_total = summary.get("events_total")
    if events_total is None:
        events = summary.get("events")
        events_total = len(events) if isinstance(events, list) else 0
    skipped = int(summary.get("skipped") or 0)
    has_errors = int(errors_total) > 0
    has_findings = findings_total > 0
    has_warnings = bool(warnings_total or warning_count or int(events_total or 0) or skipped)
    if status not in TERMINAL_STATUSES:
        outcome = "running"
    elif status == "failed":
        outcome = "failed"
    elif status == "cancelled":
        outcome = "cancelled"
    elif has_errors:
        outcome = "completed_with_errors"
    elif status == "completed_with_warnings" or has_warnings or has_findings:
        outcome = "completed_with_warnings"
    else:
        outcome = "completed"
    return {"outcome": outcome, "has_errors": has_errors,
            "has_warnings": has_warnings, "has_findings": has_findings}


# ---------------------------------------------------------------------------
# §2.2/§2.7 new aggregates and previews
# ---------------------------------------------------------------------------
def _all_findings(rows):
    return [finding for row in rows for finding in (row.get("findings") or [])]


def _composed_findings(rows, phase8=None):
    """The batch's final findings list, exactly as ``beacon_job_outcome`` builds it.

    A phase-8 row for a file that also has a primary row REPLACES that row's
    findings (it does not add to them); phase-8-only files append theirs.  This
    is what keeps a severity count from double counting after a refresh.
    """
    refreshed = {}
    if isinstance(phase8, dict):
        refreshed = {row.get("file"): row for row in phase8.get("files") or [] if isinstance(row, dict)}
    findings = []
    for row in rows:
        current = refreshed.pop(row.get("file"), None)
        source = current if current is not None else row
        findings.extend(source.get("findings") or [])
    for current in refreshed.values():
        findings.extend(current.get("findings") or [])
    return findings


def _slim_finding(finding):
    if not isinstance(finding, dict):
        return finding
    return {key: finding.get(key) for key in SLIM_FINDING_KEYS}


def new_aggregates(summary, rows, *, node_receipts=(), phase8=None):
    """§2.2's new aggregates, from the same values the detail re-derives."""
    failed_rows = sum(row.get("status") in FAILED_STATUSES for row in rows)
    node_errors = sum(1 for receipt in node_receipts
                      if receipt.get("success") is False or receipt.get("error"))
    phase8_failed = bool(summary.get("phase8_failed") or (
        isinstance(phase8, dict) and (phase8.get("success") is False or phase8.get("error"))))
    warning_types = summary.get("warnings_by_type")
    if isinstance(warning_types, dict) and int(warning_types.get("phase8_refresh_failed") or 0) > 0:
        phase8_failed = True
    publication = summary.get("publication")
    publication_ok = not (isinstance(publication, dict) and publication.get("success") is False)
    severities = {}
    for finding in _composed_findings(rows, phase8):
        key = str(finding.get("severity") or "info") if isinstance(finding, dict) else "info"
        severities[key] = severities.get(key, 0) + 1
    events = summary.get("events")
    aggregates = {
        "errors_total": failed_rows + node_errors + (1 if phase8_failed and publication_ok else 0),
        "node_errors_total": node_errors,
        "phase8_failed": phase8_failed,
        "files_changed_total": len(summary.get("files_changed") or []),
        "events_total": len(events) if isinstance(events, list) else 0,
        "findings_by_severity": dict(sorted(severities.items())),
    }
    if isinstance(phase8, dict):
        phase8_files = phase8.get("files") or []
        aggregates["phase8_summary"] = {
            "execution": phase8.get("execution"),
            "success": phase8.get("success"),
            "error": phase8.get("error"),
            "files_total": len(phase8_files),
            "files_failed": sum(row.get("status") in FAILED_STATUSES for row in phase8_files),
        }
    return aggregates


def previews(summary, rows, *, limits=None):
    """§2.2's count-bounded previews and their exact totals.

    Honest key presence: a preview is emitted only for a list the producing
    component's result actually carries (`files_changed` always; `errors` /
    `refresh_errors` per kind -- a bulk result has `errors` and no
    `refresh_errors`; the B8 folder lists when present).  `findings_preview` is
    new and always present.  `present_paths` is NOT previewed: §2.2 keeps it
    out of the structured head entirely, and an empty one keeps its presence
    through a `set` line (see ``presence_sets``).

    ``limits`` lets the writer counterpart use the writer's own caps
    (:data:`WRITER_PREVIEW_LIMITS`); the G2 fixtures keep this builder's.
    """
    limits = dict(limits or PREVIEW_LIMITS)
    values = {"findings_preview": [_slim_finding(finding)
                                   for finding in _composed_findings(rows, summary.get("phase8"))]}
    for key in ("files_changed", "errors", "refresh_errors") + B8_PREVIEW_KEYS:
        if key in summary:
            values[key] = list(summary.get(key) or [])
    bounded, totals = {}, {}
    for key, value in values.items():
        limit = limits.get(key, CF159_PREVIEW_LIMIT)
        bounded[key] = value[:limit]
        totals[key] = len(value)
    bounded["preview_totals"] = totals
    return bounded


# ---------------------------------------------------------------------------
# Evidence sidecar (§2.4, §2.5, §2.6)
# ---------------------------------------------------------------------------
def _job_row(row, *, bulk=False, job_set_check_target=False):
    """The row as the JOB gives it to the aggregate (§2.4), not the aggregate's copy.

    ``aggregate_beacon_outcomes`` copies each row and ``setdefault``s
    ``status``/``changed``/``findings``/``check_target``; only the copies are in
    the record, so the evidence file must not record a default the job's own row
    did not carry.  For a bulk row that is exactly the generated
    ``check_target``.

    Provenance of a ``check_target`` equal to that generated default (§4 item 1:
    ``refresh_data.update`` writes it before ``refresh_outcome``; the observe
    path always sets ``final_observation`` after it).  It is the JOB's own key,
    and must survive a later ``final_observation: false``, when any of these
    holds:
      * it was already known to be the job's (``job_set_check_target``);
      * the row's ``refresh_outcome`` carries the same ``check_target``;
      * it is not the aggregate-appendeed LAST key (the aggregate only appends).
    Only a first placeholder's aggregate-only default is dropped.
    """
    row = dict(row)
    if bulk and "check_target" in row:
        default = {"verb": "check",
                   "params": {"file_path": row.get("file"), "min_severity": "info"}}
        if same_json(row["check_target"], default):
            refresh_outcome = row.get("refresh_outcome")
            proven = bool(
                job_set_check_target
                or bool(row.get("final_observation"))
                or (isinstance(refresh_outcome, dict)
                    and same_json(refresh_outcome.get("check_target"), row["check_target"]))
                or list(row)[-1] != "check_target"
            )
            if proven:
                return row, True
            row.pop("check_target")
            return row, False
        return row, True
    return row, job_set_check_target


def _aggregate_row_transform(rows):
    """The rows ``aggregate_beacon_outcomes`` returns for these inputs.

    Its row rule, restated for fixture provenance: a shallow copy without
    ``snapshot``/``analysis_inputs``, then ``setdefault`` for ``status``
    (``failed`` when the row carries ``error``), ``changed``, ``findings`` and
    ``check_target``.  Used to match a captured aggregate INPUT list to the
    record it produced.
    """
    transformed = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        copy_row = {key: copy.deepcopy(value) for key, value in row.items()
                    if key not in ("snapshot", "analysis_inputs")}
        copy_row.setdefault("status", "failed" if copy_row.get("error") else "completed")
        copy_row.setdefault("changed", False)
        copy_row.setdefault("findings", [])
        copy_row.setdefault("check_target", {"verb": "check", "params": {
            "file_path": copy_row.get("file"), "min_severity": "info"}})
        transformed.append(copy_row)
    return transformed


def _job_rows(record, aggregate_inputs=(), proven=None):
    """The JOB's own rows for one captured record, and how they were obtained.

    PREFERRED: the real aggregate INPUT list captured beside this record (the
    plugin observes the call, non-mutating): the exact rows today's writer gave
    the aggregate -- ``check_target`` present only when the job set it, in the
    job's own field order.  A candidate matches when applying the aggregate's
    row rule to it reproduces this record's rows byte for byte; the candidate
    needing the fewest synthesized defaults wins (the earliest on a tie).

    FALLBACK (a capture frozen before the instrumentation): ``_job_row``'s
    derivation, reported through ``JOB_ROW_PROVENANCE_NOTES`` -- never silent.

    ``proven`` is the run-level set of file keys whose ``check_target`` is known
    to be the JOB's own; it is updated in place so a later finalization keeps
    that key even when ``final_observation`` has since flipped false (§4 item 1).
    """
    if proven is None:
        proven = set()
    rows = _canonical_rows(record)
    best = None
    for candidate in aggregate_inputs or ():
        if not isinstance(candidate, (list, tuple)):
            continue
        transformed = _aggregate_row_transform(candidate)
        if len(transformed) != len(rows):
            continue
        if not all(same_json(left.get("file"), right.get("file"))
                   and compact(left) == compact(right)
                   for left, right in zip(transformed, rows)):
            continue
        synthesized = 0
        for index, row in enumerate(candidate):
            if not isinstance(row, dict):
                continue
            kept = len([key for key in row if key not in ("snapshot", "analysis_inputs")])
            synthesized += len(transformed[index]) - kept
        if best is None or synthesized < best[0]:
            best = (synthesized, candidate)
    if best is not None:
        job_rows = [{key: copy.deepcopy(value) for key, value in row.items()
                     if key not in ("snapshot", "analysis_inputs")}
                    for row in best[1] if isinstance(row, dict)]
        for row in job_rows:
            if "check_target" in row:
                proven.add(row.get("file"))
        return job_rows, "aggregate-input"
    JOB_ROW_PROVENANCE_NOTES.append(str(record.get("id")))
    job_rows = []
    for row in rows:
        proven_row, job_set = _job_row(row, bulk=True,
                                       job_set_check_target=row.get("file") in proven)
        job_rows.append(proven_row)
        if job_set:
            proven.add(row.get("file"))
    return job_rows, "derived"


def _canonical_rows(source):
    summary = source.get("result_summary") or {}
    return [row for row in (summary.get("files") or []) if isinstance(row, dict)]


def _bulk_receipt_files(rows, global_receipts):
    """Map receipts to their physical file key, in return order (§2.5).

    Today's ``sync_active_file`` builds each row's ``node_outcomes`` from a
    slice of the run's global receipt list, so the global list is the return
    order and each row lists its own receipts in that order.  Matching uses a
    per-row cursor over deep-equal receipts; a receipt no row lists falls back
    to its own ``source_path``/``file_path``.
    """
    cursors = {row["file"]: 0 for row in rows}
    attributed = []
    for receipt in global_receipts:
        chosen = None
        for row in rows:
            own = row.get("node_outcomes") or []
            index = cursors[row["file"]]
            if index < len(own) and same_json(own[index], receipt):
                chosen = row["file"]
                cursors[row["file"]] = index + 1
                break
        if chosen is None:
            fallback = receipt.get("source_path") or receipt.get("file_path")
            chosen = str(fallback) if fallback else None
        attributed.append((chosen, receipt))
    return attributed


def _error_events(errors, *, bulk):
    kind = "bulk_error" if bulk else "file_refresh_error"
    return [{"type": kind, "error": message} for message in errors]


def _run_level(source, *, bulk):
    """Recover the aggregate's run-level arguments from the source summary.

    Folder's ``save_summary`` passes ``events=`` only.  Bulk's
    ``save_bulk_batch`` passes ``warnings=group_warnings`` and ``events=``; the
    run warnings are the first ``k`` entries of the composed ``warnings`` list,
    with ``k = len(warnings) - sum(len(row['warnings']))`` -- §2.10's rule.
    """
    summary = source.get("result_summary") or {}
    rows = _canonical_rows(source)
    errors = list(summary.get("errors") or [])
    run_events = _error_events(errors, bulk=bulk)
    run_warnings = []
    if bulk:
        composed = list(summary.get("warnings") or [])
        row_warnings = sum(len(row.get("warnings") or []) for row in rows)
        run_warnings = composed[:max(0, len(composed) - row_warnings)]
    return {"run_warnings": run_warnings, "run_events": run_events, "errors": errors}


_MISSING = object()


def _delta_against(previous, current):
    """One delta line's ``{unset, extend}`` and the keys it sets (§2.4/§2.5).

    §4 item 1: a later finalization of a file that already has a row line is a
    DELTA, never a rebuilt row -- each key the group sets, each key it removes
    (today's ``active_file.pop("error", None)``) and each list entry it appends
    to a list it did not set.  ``node_outcomes`` is the receipt rule's business,
    never a delta key.

    A row's field order is the position of each key's LAST insertion (a row is
    built by ``setdefault`` / ``dict.update`` / ``pop``, and the aggregate only
    shallow-copies it), so the observed order itself proves which keys this
    finalization (re)inserted: take the longest prefix of ``current`` whose keys
    already sit in ``previous`` in the same relative order.  From the first key
    that is new or out of order onward, an old key CANNOT have kept its position
    (a dict only appends new keys) -- it was popped and set again, recorded as
    ``unset`` + set, the only transition dicts allow; a new key is a set.  Keys
    before that boundary keep their positions: a changed value is an in-place
    set, list-only growth is an ``extend``.
    """
    previous_keys = [key for key in previous if key != "node_outcomes"]
    current_keys = [key for key in current if key != "node_outcomes"]
    kept, last_position = 0, -1
    for index, key in enumerate(current_keys):
        if key not in previous_keys:
            break
        position = previous_keys.index(key)
        if position <= last_position:
            break
        last_position = position
        kept = index + 1
    tail = current_keys[kept:]
    reinserted = [key for key in tail if key in previous]
    unset = [key for key in previous if key not in current and key != "node_outcomes"]
    unset.extend(reinserted)
    set_keys = set(tail)
    extend = {}
    for key in current_keys[:kept]:
        old = previous.get(key, _MISSING)
        value = current[key]
        if old is _MISSING:
            set_keys.add(key)
        elif same_json(old, value):
            continue
        elif (isinstance(old, list) and isinstance(value, list) and len(value) > len(old)
              and all(same_json(left, right) for left, right in zip(old, value))):
            if value[len(old):]:
                extend[key] = value[len(old):]
        else:
            set_keys.add(key)
    row = {"file": current.get("file")}
    for key in current_keys:                 # the line's own key order
        if key in set_keys:
            row[key] = copy.deepcopy(current[key])
    if reinserted:
        ROW_ORDER_NOTES.append(f"{previous.get('file')}: {', '.join(reinserted)}")
    return row, unset, extend


def _active_file(row_by_file, rows_now, group_file_id, previous_final_observation):
    """The file a group finalizes (§4 item 1), from the record's own state.

    The group's file is the row whose ``file_node_id`` is the ``current_file``
    the run just processed; fallback: the single row whose ``final_observation``
    just flipped to true (today's code sets it immediately before the group-end
    write).
    """
    if group_file_id:
        for file_key, row in row_by_file.items():
            if str(row.get("file_node_id") or "") == str(group_file_id):
                return file_key
    flipped = [row.get("file") for row in rows_now
               if row.get("final_observation") is True
               and previous_final_observation.get(row.get("file")) is not True]
    if len(flipped) == 1:
        return flipped[0]
    return None


def build_sidecar_from_sequence(records, *, kind="bulk", aggregate_inputs=()):
    """Evidence lines from a captured *write sequence* (see capture_joined_fixtures).

    What the BASE runtime produced was a whole JSON record at every persist, not
    JSONL lines; this is a NORMALIZED RECONSTRUCTION of the lines a §4-item-1
    writer would have appended, derived only from the captured records (and, for
    a capture taken with the plugin's aggregate observer, from the real
    aggregate INPUT rows).  A limitation is reported, never invented away.

    * ``node`` lines: each receipt's first appearance, in return order, before
      that batch's record write (§4 item 1).
    * ``row`` / delta lines: ONE per group finalization -- ``progress.
      completed_files`` grew and the group-end write already carries
      ``final_observation: true`` -- plus the terminal/stop snapshot.  A
      group-head placeholder and a per-candidate progress write are NOT file
      finalizations and emit no row line.  A file already carrying a row line
      gets a DELTA line, never a rebuilt row.
    * Order: at a normal boundary the active file first, then each other file the
      group touched, in today's ``result_summary.files`` order; at a stop, that
      order as it stands (§4's one rule).
    * ``item`` lines at first appearance; ``set`` lines for presence and for
      §2.2's >4 KiB moved values.
    * ``sequence``: the ordered call sequence this reconstruction stands for
      (kind, file, cause, reflected receipt count) -- the metadata G3's byte
      oracle compares, so a writer's own line order can be checked without
      pretending the capture was JSONL.
    """
    if not records:
        raise ValueError("empty capture sequence")
    provenance_start, order_start = len(JOB_ROW_PROVENANCE_NOTES), len(ROW_ORDER_NOTES)
    final = records[-1]
    rows_final = _canonical_rows(final)
    summary_final = final.get("result_summary") or {}
    receipts_final = list(summary_final.get("node_outcomes") or [])
    attributed = _bulk_receipt_files(rows_final, receipts_final)
    if any(file_key is None for file_key, _ in attributed):
        raise ValueError("a captured receipt names no physical file key")
    receipt_files = [file_key for file_key, _ in attributed]

    files_lines, nodes_lines, sequence = [], [], []
    counts = {"rows": 0, "nodes": 0}
    seen_rows, seen_counts, seen_receipts = {}, {}, 0
    seen_items = {key: 0 for key in ITEM_KEYS}
    marker_present = any("final_observation" in row
                         for snapshot in records for row in _canonical_rows(snapshot))
    last_completed, group_file_id, previous_final_observation = 0, None, {}
    proven_check_targets = set()

    for index, snapshot in enumerate(records):
        summary = snapshot.get("result_summary") or {}
        progress = snapshot.get("progress") or {}
        if progress.get("current_file"):
            group_file_id = str(progress.get("current_file"))

        # Receipts are durable as each batch returns, before that batch's write.
        global_receipts = list(summary.get("node_outcomes") or [])
        if len(global_receipts) < seen_receipts:
            raise ValueError("captured receipt list shrank between writes")
        if not all(same_json(a, b) for a, b in zip(global_receipts, receipts_final[:len(global_receipts)])):
            raise ValueError("captured receipts are not an append-only prefix of the final list")
        for receipt_index in range(seen_receipts, len(global_receipts)):
            nodes_lines.append(_line({"k": "node", "file": receipt_files[receipt_index],
                                      "row": global_receipts[receipt_index]}))
            counts["nodes"] += 1
            sequence.append({"k": "node", "file": receipt_files[receipt_index],
                             "cause": "receipt", "receipts": receipt_index + 1})
        seen_receipts = len(global_receipts)

        run = _run_level(snapshot, bulk=(kind == "bulk"))
        pending_items = []
        for key in ("run_warnings", "run_events"):
            values = run[key]
            for value in values[seen_items[key]:]:
                pending_items.append({"k": "item", "key": key, "v": value})
            seen_items[key] = max(seen_items[key], len(values))
        for key in _item_keys(summary):
            values = list(summary.get(key) or [])
            for value in values[seen_items.get(key, 0):]:
                pending_items.append({"k": "item", "key": key, "v": value})
            seen_items[key] = max(seen_items.get(key, 0), len(values))

        rows_now = _canonical_rows(snapshot)
        completed = progress.get("completed_files")
        completed = int(completed) if isinstance(completed, (int, float)) else None
        boundary = False
        if (completed is not None and completed > last_completed
                and not progress.get("current_file")):
            if not marker_present or any(row.get("final_observation") is True for row in rows_now):
                boundary = True
                last_completed = completed
        status_now = str(snapshot.get("status") or "").strip().lower()
        stop = index == len(records) - 1 and status_now in TERMINAL_STATUSES
        if boundary or stop:
            cause = "group-final" if boundary else "stop"
            job_rows, _origin = _job_rows(snapshot, aggregate_inputs, proven_check_targets)
            row_by_file = {row.get("file"): row for row in job_rows if row.get("file")}
            order = [file_key for file_key in row_by_file]
            if boundary:
                active = _active_file(row_by_file, rows_now, group_file_id,
                                      previous_final_observation)
                if active in row_by_file:
                    order = [active] + [key for key in order if key != active]
            for file_key in order:
                job_row = row_by_file[file_key]
                nodes_count = len(job_row.get("node_outcomes") or [])
                previous = seen_rows.get(file_key)
                if previous is None:
                    stored, proj_keys = store_row(job_row, null_keys=("node_outcomes",))
                    entry = {"k": "row", "sec": "files", "row": stored, "nodes": nodes_count}
                    line_kind = "row"
                else:
                    delta_row, unset, extend = _delta_against(previous, job_row)
                    delta_stored, delta_proj = store_row(delta_row)
                    entry = {"k": "row", "sec": "files", "row": delta_stored,
                             "nodes": nodes_count,
                             "delta": {"unset": unset, "extend": extend}}
                    line_kind = "delta"
                    if not (unset or extend or len(delta_row) > 1
                            or nodes_count != seen_counts.get(file_key)):
                        # Nothing this group did to the file: no line (§4),
                        # except when its REFLECTED receipt count changed.
                        continue
                if line_kind == "row" and proj_keys is not None:
                    entry[PROJ_KEYS_KEY] = proj_keys
                elif line_kind == "delta" and delta_proj is not None:
                    entry[PROJ_KEYS_KEY] = delta_proj
                files_lines.append(_line(entry))
                counts["rows"] += 1
                seen_rows[file_key] = job_row
                seen_counts[file_key] = nodes_count
                sequence.append({"k": line_kind, "file": file_key, "cause": cause,
                                 "active": boundary and file_key == order[0],
                                 "completed_files": completed, "receipts": nodes_count})
        for entry in pending_items:
            files_lines.append(_line(entry))
            sequence.append({"k": "item", "key": entry["key"]})
        previous_final_observation = {row.get("file"): row.get("final_observation")
                                      for row in rows_now}

    presence = presence_sets(summary_final)
    for path, value in presence.items():
        files_lines.append(_line({"k": "set", "key": list(path), "v": value}))
        sequence.append({"k": "set", "key": list(path), "cause": "presence"})
    moved = moved_values(final)
    for key in sorted(moved, key=lambda path: tuple(map(str, path))):
        files_lines.append(_line({"k": "set", "key": list(key), "v": moved[key]}))
        sequence.append({"k": "set", "key": list(key), "cause": "moved-value"})

    in_flight = None
    if str(final.get("status") or "").strip().lower() not in TERMINAL_STATUSES:
        rows_now = _canonical_rows(final)
        job_rows, _origin = _job_rows(final, aggregate_inputs, proven_check_targets)
        row_by_file = {row.get("file"): row for row in job_rows if row.get("file")}
        active = _active_file(row_by_file, rows_now, group_file_id, previous_final_observation)
        row = row_by_file.get(active)
        if row is not None and active not in seen_rows:
            in_flight = {key: copy.deepcopy(value) for key, value in row.items()
                         if key != "node_outcomes"}
            in_flight["base_lines"] = len(files_lines)

    return {"files_lines": files_lines, "nodes_lines": nodes_lines,
            "counts": counts, "attributed": attributed,
            "run": _run_level(final, bulk=(kind == "bulk")),
            "moved": moved, "presence_sets": presence, "sequence": sequence,
            "in_flight": in_flight,
            "landed_rows": [copy.deepcopy(row) for row in seen_rows.values()],
            "derived_job_rows": JOB_ROW_PROVENANCE_NOTES[provenance_start:],
            "reinserted_keys": ROW_ORDER_NOTES[order_start:]}


def _item_keys(summary):
    """Run-level list keys that become `item` lines (§2.4, CF-159).

    The grammar's own keys (`errors`, `refresh_errors`, `present_paths`) plus
    any other list value the folder result carries -- ``nexusignore_sources``
    and ``missing_paths`` above all -- so a folder's complete lists live in the
    evidence file and a count-bounded preview is the only thing a record holds.
    """
    keys = ["errors", "refresh_errors", "present_paths"]
    for key, value in summary.items():
        if not isinstance(value, list) or key in TAIL_KEYS_1 or key in keys:
            continue
        keys.append(key)
    return tuple(keys)


def presence_sets(summary):
    """`set` lines that keep a present-but-EMPTY run-level list alive (§2.2/§2.4).

    A non-empty list lives completely in `item` lines.  An EMPTY one has no item
    to carry its presence, and §2.2 keeps `present_paths` out of the structured
    head entirely (it is neither a head list nor a preview), so a last-wins
    `set` line with an explicit tuple path preserves it -- the read side's
    ``_restore_carto_value`` takes tuple paths, so ``["result_summary", key]``
    can never be confused with a top-level field of the same name.  Any other
    list that is neither a tail key, nor a previewed key, nor carried by `item`
    lines keeps its presence the same way.  Absent keys stay absent.
    """
    present = {}
    if isinstance(summary, dict) and isinstance(summary.get("present_paths"), list) \
            and not summary["present_paths"]:
        present[("result_summary", "present_paths")] = []
    for key, value in (summary or {}).items():
        if key in TAIL_KEYS_1 or key in ITEM_KEYS:
            continue
        if key in B8_PREVIEW_KEYS:
            # its bounded head preview already carries presence (B8/CF-159)
            continue
        if isinstance(value, list) and not value:
            present[("result_summary", key)] = []
    return present


def moved_values(source):
    """§2.2's 4 KiB rule: values that move to a `set` line, with UNAMBIGUOUS paths.

    Each key is a tuple path the read side restores directly (its
    ``_restore_carto_value`` takes tuple paths): ``("result_summary", name)``
    for a summary value, ``(name,)`` for another top-level field -- so a name
    that exists in both places is never ambiguous.  Top-level ``warnings``,
    ``progress`` and the record's own layout keys stay in the record.
    """
    moved = {}
    summary = source.get("result_summary") or {}
    for key, value in summary.items():
        if key in TAIL_KEYS_1 or isinstance(value, list):
            continue
        if len(compact(value).encode("utf-8")) > MOVE_LIMIT:
            moved[("result_summary", key)] = value
    for key, value in source.items():
        if key in HEAD_ORDER_2 or key in ("record_layout", "result_summary", "warnings",
                                          "progress", "evidence", "in_flight"):
            continue
        if len(compact(value).encode("utf-8")) > MOVE_LIMIT:
            moved[(key,)] = value
    return moved


def build_sidecar(source, *, bulk=False, moved=None):
    """Return the evidence lines for one `/2` counterpart.

    Lines follow the canonical order this builder documents: the run-level
    `set` values the kind writes before anything else (``publication`` before
    phase 8), then `row` lines in fold order, then `node` lines (bulk, in
    return order), then `item` lines, then `phase8` row lines, then any
    `set` line for a value moved out by §2.2's 4 KiB rule.  A live G5 writer
    emits its own chronological order; the fixture is the canonical
    reconstruction of the same folded state.
    """
    summary = source.get("result_summary") or {}
    rows = _canonical_rows(source)
    moved = dict(moved or {})
    files_lines, nodes_lines = [], []
    counts = {"rows": 0, "nodes": 0}

    if "publication" in summary and "publication" not in moved:
        files_lines.append(_line({"k": "set", "key": "publication",
                                  "v": summary["publication"]}))
    phase8 = summary.get("phase8")
    if isinstance(phase8, dict):
        envelope = {key: value for key, value in phase8.items() if key != "files"}
        files_lines.append(_line({"k": "set", "key": "phase8_envelope", "v": envelope}))

    global_receipts = list(summary.get("node_outcomes") or []) if bulk else []
    attributed = _bulk_receipt_files(rows, global_receipts) if bulk else []

    for row in rows:
        job_row, _proven = _job_row(row, bulk=bulk)
        stored, proj_keys = store_row(job_row, null_keys=("node_outcomes",) if bulk else ())
        entry = {"k": "row", "sec": "files", "row": stored}
        if proj_keys is not None:
            entry[PROJ_KEYS_KEY] = proj_keys
        if bulk:
            entry["nodes"] = len(row.get("node_outcomes") or [])
        files_lines.append(_line(entry))
        counts["rows"] += 1

    if bulk:
        for file_key, receipt in attributed:
            if file_key is None:
                raise ValueError(f"bulk receipt carries no physical file key: {compact(receipt)[:200]}")
            nodes_lines.append(_line({"k": "node", "file": file_key, "row": receipt}))
            counts["nodes"] += 1

    run = _run_level(source, bulk=bulk)
    for value in run["run_warnings"]:
        files_lines.append(_line({"k": "item", "key": "run_warnings", "v": value}))
    for value in run["run_events"]:
        files_lines.append(_line({"k": "item", "key": "run_events", "v": value}))
    for key in _item_keys(summary):
        for value in summary.get(key) or []:
            files_lines.append(_line({"k": "item", "key": key, "v": value}))

    if isinstance(phase8, dict):
        for row in phase8.get("files") or []:
            if not isinstance(row, dict):
                continue
            stored, proj_keys = store_row(row)
            entry = {"k": "row", "sec": "phase8", "row": stored}
            if proj_keys is not None:
                entry[PROJ_KEYS_KEY] = proj_keys
            files_lines.append(_line(entry))

    present = presence_sets(summary)
    for path, value in present.items():
        files_lines.append(_line({"k": "set", "key": list(path), "v": value}))

    for key in sorted(moved, key=lambda path: tuple(map(str, key))):
        files_lines.append(_line({"k": "set", "key": list(key), "v": moved[key]}))

    return {"files_lines": files_lines, "nodes_lines": nodes_lines,
            "counts": counts, "attributed": attributed, "run": run,
            "moved": moved, "presence_sets": present, "sequence": []}


def build_record(source, sidecar, *, files_name=EVIDENCE_FILE, nodes_name=NODES_FILE,
                 moved=None, in_flight=None, preview_limits=None):
    """Build the `/2` record for one source record (§2.2 key order)."""
    summary = source.get("result_summary") or {}
    rows = _canonical_rows(source)
    phase8 = summary.get("phase8")
    moved = dict(moved or {})

    aggregate = new_aggregates(
        summary, rows,
        node_receipts=[receipt for _, receipt in sidecar.get("attributed") or []],
        phase8=phase8,
    )
    preview_values = previews(summary, rows, limits=preview_limits)

    record = {"record_layout": LAYOUT_2}
    outcome = derive_outcome(_with_aggregates(source, aggregate))
    for key in HEAD_ORDER_2:
        if key in outcome:
            record[key] = outcome[key]
        elif key in source:
            record[key] = source[key]

    for key, value in source.items():
        if key in record or key == "result_summary":
            continue
        if (key,) in moved:
            record[key] = {EVIDENCE_PLACEHOLDER: files_name,
                           "bytes": len(compact(value).encode("utf-8"))}
        else:
            record[key] = value

    files_text = "".join(sidecar["files_lines"])
    evidence = {"files": {"path": files_name, "rows": sidecar["counts"]["rows"],
                          "lines": len(sidecar["files_lines"]),
                          "bytes": len(files_text.encode("utf-8")), "pending": 0}}
    if sidecar.get("nodes_lines"):
        nodes_text = "".join(sidecar["nodes_lines"])
        evidence["nodes"] = {"path": nodes_name, "rows": sidecar["counts"]["nodes"],
                             "bytes": len(nodes_text.encode("utf-8"))}
    else:
        evidence["nodes"] = None
    record["evidence"] = evidence
    record["in_flight"] = in_flight

    ordered = {}
    for key, value in summary.items():
        if key in TAIL_KEYS_1 or key in ("errors", "files"):
            continue
        if isinstance(value, list):
            continue
        path = ("result_summary", key)
        if path in moved:
            ordered[key] = {EVIDENCE_PLACEHOLDER: files_name,
                            "bytes": len(compact(value).encode("utf-8"))}
        else:
            ordered[key] = value
    ordered.update(aggregate)
    ordered.update(preview_values)
    record["result_summary"] = ordered
    return record


def _with_aggregates(source, aggregate):
    """A shallow copy of ``source`` whose summary carries the new aggregates."""
    probe = dict(source)
    summary = dict(source.get("result_summary") or {})
    summary.update(aggregate)
    probe["result_summary"] = summary
    return probe


# ---------------------------------------------------------------------------
# The writer counterpart (`writer.record.json`): Q4's corrected oracle
# ---------------------------------------------------------------------------
def _row_line_count(sidecar) -> int:
    """Q4(4): ``evidence.files.rows`` counts landed ``row`` lines, every section."""
    return sum(1 for line in sidecar.get("files_lines") or ()
               if json.loads(line).get("k") == "row")


def writer_warning_display(source, sidecar):
    """B5/CF-161: the writer's top-level ``warnings`` display for one replay.

    The current writer keeps the component's own head warnings, appends each row
    line's ``warnings`` and each ``run_warnings`` item in replay order,
    deduplicates on the compact encoding and caps the display at 200.  A folder
    run's G2 record carries no display at all, so this value is the
    counterpart's own construction -- never a copy of writer output.
    """
    display, seen = [], set()

    def add(values):
        items = values if isinstance(values, list) else ([] if values is None else [values])
        for value in items:
            key = compact(value)
            if key in seen:
                continue
            if len(display) >= WRITER_WARNING_DISPLAY_LIMIT:
                return
            seen.add(key)
            display.append(copy.deepcopy(value))

    add(source.get("warnings"))
    for line in sidecar.get("files_lines") or ():
        entry = json.loads(line)
        if entry.get("k") == "row":
            add((entry.get("row") or {}).get("warnings"))
            add(((entry.get("delta") or {}).get("extend") or {}).get("warnings"))
        elif entry.get("k") == "item" and entry.get("key") == "run_warnings":
            add(entry.get("v"))
        elif entry.get("k") == "set" and entry.get("key") == "phase8_envelope":
            add((entry.get("v") or {}).get("warnings"))
    return display


def writer_warning_total(summary, source) -> int:
    """B5/CF-161: the exact ``warning_count`` a ``/2`` write stores.

    The status funnel takes the summary's exact ``warnings_total``, never the
    capped or deduplicated display length.  A component that carries no total
    falls back to the head count it arrived with (the funnel's own fallback).
    """
    total = summary.get("warnings_total")
    if total is None:
        total = source.get("warning_count")
    return int(total or 0)


def _receipt_base_rows(sidecar, landed_rows):
    """§2.5: a file known only from receipts is the base row the batch composes.

    The fold registers such a file as today's base row (``status: completed``,
    ``changed`` from its receipt tally) with its receipts attached, and the
    writer's accumulator holds the same key -- so an up-to-date stop record's
    summary counts it.  A file that already has a row line is never repeated.
    """
    landed_files = {row.get("file") for row in landed_rows}
    by_file = {}
    for file_key, receipt in sidecar.get("attributed") or ():
        by_file.setdefault(file_key, []).append(receipt)
    bases = []
    for file_key, receipts in by_file.items():
        if file_key in landed_files:
            continue
        base = {
            "file": file_key,
            "status": "completed",
            "changed": any(receipt.get("changed") and not receipt.get("error")
                           for receipt in receipts),
            "findings": [], "warnings": [], "events": [],
            "node_outcomes": list(receipts),
            "nodes_satisfied": sum(bool(receipt.get("satisfied")) for receipt in receipts),
            "final_observation": False,
        }
        rejected = [receipt.get("error") or receipt.get("noop_reason") or "Node write not satisfied"
                    for receipt in receipts if receipt.get("success") is False or receipt.get("error")]
        if rejected:
            base.update(status="failed", success=False, error=rejected[-1])
        bases.append(base)
    return bases


def _up_to_date_stop_source(source, sidecar):
    """Q4(3): the up-to-date writer state of a crash fixture, never its G2 copy.

    The G2 bulk fixture is a READER-ONLY crash fixture: its summary was the
    terminal-only composition of the rows that record held, while the writer's
    accumulator publishes the landed rows and every returned receipt.  So the
    counterpart composes its summary from the rows that landed in
    ``files.jsonl`` plus each receipt-only base row (``_receipt_base_rows``),
    keeps the component's non-derived scalars (its own counters, and the empty
    run lists that carry presence), and leaves the crash head and the
    ``in_flight`` placeholder alone.
    """
    landed = list(sidecar.get("landed_rows") or ())
    rows = landed + _receipt_base_rows(sidecar, landed)
    run = sidecar.get("run") or {}
    summary = compose_source_summary(
        rows, run_warnings=run.get("run_warnings") or (),
        run_events=run.get("run_events") or ())
    for key, value in (source.get("result_summary") or {}).items():
        if key in WRITER_DERIVED_SUMMARY_KEYS or key in TAIL_KEYS_1 or key == "files":
            continue
        if isinstance(value, list) and value:
            continue                     # complete in `item` lines, never re-supplied
        summary[key] = copy.deepcopy(value)
    derived = dict(source)
    derived["result_summary"] = summary
    return derived


#: One line per corrected counterpart; the same reasons are in
#: :func:`writer_counterpart`'s docstring and are printed by every build.
WRITER_COUNTERPART_REASONS = {
    "folder_refresh_trim": (
        "B5: exact warning_count 6 and the 6-entry warning display the frozen G2 "
        "record does not carry; the 7,543-byte findings_preview stays inline "
        "(Q4(1)), so the 30 evidence lines / 930,786 bytes are unchanged"),
    "mixed_folder": (
        "B5: exact warning_count 2 and its 2-entry warning display; every other "
        "value and the 25 evidence lines are unchanged"),
    "bulk_receipts_synthetic": (
        "Q4(3): the original is a reader-only crash fixture, so the counterpart is "
        "the up-to-date writer state over its 7 landed rows and 7 receipts (the "
        "interrupted file becomes the receipt-only base row), keeping the crash "
        "head and placeholder; B5 adds warning_count 0"),
    "bulk_apply": (
        "B5: exact warning_count 1 and the display its head already carried; every "
        "other value and the 5 evidence lines are unchanged"),
    "markdown_generate": (
        "Q4(4): the rows hint is 2 (primary and phase-8 row lines, not 1); B5 keeps "
        "warning_count 2 and its display, and the 4 evidence lines are unchanged"),
}


def writer_counterpart(source, sidecar, *, bulk=False, in_flight=None):
    """The ``/2`` record the CURRENT writer produces for one fixture's replay.

    The frozen ``record.json`` is the G2 oracle and is never rewritten; this is
    its corrected counterpart, built here (never from writer output) and used by
    the G3 tests and by ``--check``.  Four Q4 rules ([ap:P0YV8T]):

    * **B5/CF-161** applies to every ``/2`` write: ``warning_count`` is the exact
      total (``result_summary.warnings_total``) and ``warnings`` is the deduped,
      200-capped display -- the component's head warnings first, then row-line
      and ``run_warnings`` entries in replay order.
    * **Q4(1)**: count-bounded previews and aggregates stay inline whatever
      their encoded size, so no preview ``set`` lines exist.
    * **Q4(4)**: ``evidence.files.rows`` counts the ``row`` lines landed in every
      section, a phase-8 row line included.
    * **Q4(3)**: a crash fixture's counterpart is the up-to-date writer state
      (``_up_to_date_stop_source``), with its crash head and placeholder kept.

    Per-fixture reasons (also in ``WRITER_COUNTERPART_REASONS``):

    * ``folder_refresh_trim`` -- B5: exact warning_count 6 and the 6-entry
      display; the 7,543-byte ``findings_preview`` stays inline (Q4(1)), and the
      30 evidence lines / 930,786 bytes are unchanged.
    * ``mixed_folder`` -- B5: exact warning_count 2 and its 2-entry display.
    * ``bulk_receipts_synthetic`` -- Q4(3): the original is reader-only; the
      counterpart is the up-to-date writer state over the 7 landed rows and 7
      receipts (the interrupted file is the receipt-only base row), keeping the
      crash head and placeholder; B5 adds warning_count 0.
    * ``bulk_apply`` -- B5: exact warning_count 1 and the display its head
      already carried.
    * ``markdown_generate`` -- Q4(4): the rows hint is 2 (primary and phase-8
      row lines, not 1); B5 keeps warning_count 2 and its display.

    ``bulk`` is accepted for symmetry with :func:`build_record`; the evidence
    sidecar already decides which sections exist, so it does not change a value.
    """
    document = source
    if in_flight is not None:
        # A stop record is the state over what landed, never the terminal-only
        # composition the G2 crash fixture froze.
        source = _up_to_date_stop_source(source, sidecar)
    moved = sidecar.get("moved") or moved_values(source)
    summary = source.get("result_summary") or {}
    head = dict(source)
    head["warnings"] = writer_warning_display(document, sidecar)
    head["warning_count"] = writer_warning_total(summary, document)
    record = build_record(head, sidecar, moved=moved, in_flight=in_flight,
                          preview_limits=WRITER_PREVIEW_LIMITS)
    record["evidence"]["files"]["rows"] = _row_line_count(sidecar)
    return record


def fixture_inputs(name):
    """The frozen input state one fixture build and ``--check`` need.

    ``{"bulk", "document", "source", "sidecar", "moved", "in_flight"}``, or
    ``None`` when the fixture's source is not captured yet.  ``source`` is the
    final record of a capture, never a derived counterpart.
    """
    targets = fixture_targets()
    if name not in targets:
        return None
    source_path, bulk = targets[name]
    if not source_path.exists():
        return None
    document = load_source(source_path)
    records = _capture_records(document)
    if records is not None:
        sidecar = build_sidecar_from_sequence(
            records, kind=str(document.get("kind") or ("bulk" if records else "")),
            aggregate_inputs=document.get("aggregate_inputs") or (),
        )
        source = records[-1]
        moved = sidecar.get("moved") or moved_values(source)
    else:
        source = document
        moved = moved_values(source)
        sidecar = build_sidecar(source, bulk=bulk, moved=moved)
    in_flight = document.get("in_flight") if isinstance(document, dict) else None
    if in_flight is None and sidecar.get("in_flight"):
        in_flight = sidecar["in_flight"]
    if in_flight is not None:
        in_flight = dict(in_flight)
        if in_flight.get("base_lines") is None:
            in_flight["base_lines"] = len(sidecar["files_lines"])
    return {"bulk": bulk, "document": document, "source": source,
            "sidecar": sidecar, "moved": moved, "in_flight": in_flight}


def writer_counterpart_text(name):
    """The exact committed ``writer.record.json`` text for one fixture, or None."""
    inputs = fixture_inputs(name)
    if inputs is None:
        return None
    return dump_record(writer_counterpart(
        inputs["source"], inputs["sidecar"], bulk=inputs["bulk"],
        in_flight=inputs["in_flight"]))


def marker_row(row):
    """G4 D3-1 oracle, independent of the production marker/writer.

    Digest canonical JSON as compact sorted keys with default ASCII escaping.
    Only the two named outline positions change; finding anatomy stays whole.
    """
    result = copy.deepcopy(row)
    for target in (result, result.get("projection")):
        if not isinstance(target, dict) or "reconcile_children" not in target:
            continue
        value = target["reconcile_children"]
        if value is None:
            target["reconcile_children"] = {"$reconcile_children": None}
        elif isinstance(value, list):
            text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            target["reconcile_children"] = {"$reconcile_children": {
                "count": len(value), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}}
    return result


def marker_counterpart(source, sidecar, *, bulk=False, in_flight=None):
    """New marker variants; never rewrite the original G2/G3 reader oracles."""
    marked = copy.deepcopy(sidecar)
    lines = []
    for text in sidecar["files_lines"]:
        entry = json.loads(text)
        if entry.get("k") == "row":
            row = marker_row(expand_row(entry["row"], entry.get("proj")))
            stored, keys = store_row(row, null_keys=("node_outcomes",) if "nodes" in entry else ())
            entry["row"] = stored
            entry.pop("proj", None)
            if keys is not None:
                entry["proj"] = keys
        lines.append(_line(entry))
    marked["files_lines"] = lines
    record = writer_counterpart(source, marked, bulk=bulk, in_flight=in_flight)
    return record, "".join(lines)


# ---------------------------------------------------------------------------
# Writing, layout, verification
# ---------------------------------------------------------------------------
def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def dump_record(record) -> str:
    """A `/2` record: ``indent=2``, UTF-8, no trailing newline (writer parity)."""
    return json.dumps(record, indent=2, ensure_ascii=True)


def dump_source_1(record) -> str:
    """A `summary-head/1` source in the runtime's own head/tail layout.

    Layout only (the writer these fixtures feed is G3's): the head is indented
    and `result_summary`'s tail keys are compact, exactly as
    ``merge_write_carto_job_status`` lays a `/1` record out today, so the head
    readers' one-window path is exercised on a realistic file.
    """
    ordered = {"record_layout": LAYOUT_1}
    for key in HEAD_ORDER_1:
        if key in record:
            ordered[key] = record[key]
    for key, value in record.items():
        if key in ordered or key == "result_summary":
            continue
        ordered[key] = value
    summary = record.get("result_summary") or {}
    tail = {key: value for key, value in summary.items() if key in TAIL_KEYS_1}
    head_summary = {key: value for key, value in summary.items() if key not in TAIL_KEYS_1}
    if not tail:
        ordered["result_summary"] = summary
        return json.dumps(ordered, indent=2)
    ordered["result_summary"] = head_summary
    head = json.dumps(ordered, indent=2).rsplit("}", 2)[0].rstrip()
    return head + ("," if head_summary else "") + "\n    " + compact(tail)[1:-1] + "\n  }\n}"


def legacy_order(record):
    """The same payload in legacy key order (§2.11's admission form).

    The layout marker is gone and ``result_summary`` is not last, so no reader
    can mistake the file for a head-first record; every value is identical.
    """
    ordered = {}
    for key, value in record.items():
        if key in ("record_layout", "result_summary"):
            continue
        ordered[key] = value
        if key == "progress":
            ordered["result_summary"] = record["result_summary"]
    ordered.setdefault("result_summary", record["result_summary"])
    return ordered


def roundtrip_row_check(source, sidecar, *, bulk=False):
    """§2.6 round trip over every row the sidecar stores."""
    rows = _canonical_rows(source)
    entries = [json.loads(line) for line in sidecar["files_lines"]
               if json.loads(line).get("k") == "row" and json.loads(line).get("sec") == "files"]
    if len(entries) != len(rows):
        raise AssertionError(f"row line count {len(entries)} != source rows {len(rows)}")
    for row, entry in zip(rows, entries):
        restored = expand_row(entry["row"], entry.get(PROJ_KEYS_KEY))
        want = {**row, "node_outcomes": None} if bulk else row
        # §2.6 pin: the row's FIELD ORDER and every value (deep JSON equality).
        # A projected value's own nested key order is `proj`'s business and is
        # reported by PROJECTION_ORDER_NOTES, never refused.
        if list(restored) != list(want):
            raise AssertionError(f"§2.6 round trip field order differs for {row.get('file')!r}")
        for key, value in want.items():
            if not same_json(restored[key], value):
                raise AssertionError(f"§2.6 round trip value differs at {key!r} for {row.get('file')!r}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def source_dir(name: str) -> Path:
    """Each fixture's frozen inputs live beside it, in ``<fixture>/sources/``."""
    return FIXTURE_ROOT / name / "sources"


TRIM_SOURCE = source_dir("folder_refresh_trim") / "folder_refresh_trim.record.json"
LEGACY_SOURCE = source_dir("folder_refresh_trim") / "folder_refresh_trim.legacy.record.json"
MIXED_SOURCE = source_dir("mixed_folder") / "mixed_folder.record.json"
BULK_SOURCE = source_dir("bulk_apply") / "bulk_apply.capture.json"
MARKDOWN_SOURCE = source_dir("markdown_generate") / "markdown_generate.record.json"
SYNTHETIC_BULK_SOURCE = (source_dir("bulk_receipts_synthetic")
                         / "bulk_receipts_synthetic.capture.json")
# The legacy-order twin has no separate `/2` pair: its counterpart is
# `folder_refresh_trim/`, which the build proves byte-identical.
LEGACY_TWIN = "folder_refresh_trim_legacy"


def fixture_targets():
    """Every fixture this builder owns: name -> (source path, bulk?)."""
    return {
        "folder_refresh_trim": (TRIM_SOURCE, False),
        LEGACY_TWIN: (LEGACY_SOURCE, False),
        "mixed_folder": (MIXED_SOURCE, False),
        "bulk_receipts_synthetic": (SYNTHETIC_BULK_SOURCE, True),
        "bulk_apply": (BULK_SOURCE, True),
        "markdown_generate": (MARKDOWN_SOURCE, False),
    }


def load_source(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _capture_records(document):
    """The write sequence of a capture document, or None for a plain record.

    A capture (see ``capture_joined_fixtures.py``) is
    ``{"kind": "bulk"|"markdown", "test": ..., "commit": ..., "records": [...]}``
    where each element is one post-write on-disk record, oldest first.
    """
    if isinstance(document, dict) and isinstance(document.get("records"), list) and document["records"]:
        return document["records"]
    return None


def ensure_legacy_source(*, check=False):
    """Write the legacy-order twin source from the trimmed `/1` source."""
    if not TRIM_SOURCE.exists():
        return False
    text = compact(legacy_order(load_source(TRIM_SOURCE)))
    if check:
        return LEGACY_SOURCE.exists() and LEGACY_SOURCE.read_text(encoding="utf-8") == text
    write_text(LEGACY_SOURCE, text)
    return True


def build_one(name, source_path: Path, bulk: bool, *, check=False, only=None):
    if only and name not in only:
        return None
    if not source_path.exists():
        print(f"MISSING {name}: no source at {source_path.relative_to(REPO_ROOT)}")
        return {"name": name, "missing": str(source_path.relative_to(REPO_ROOT))}
    inputs = fixture_inputs(name)
    if inputs is None:
        print(f"MISSING {name}: no source at {source_path.relative_to(REPO_ROOT)}")
        return {"name": name, "missing": str(source_path.relative_to(REPO_ROOT))}
    bulk = inputs["bulk"]
    source, sidecar = inputs["source"], inputs["sidecar"]
    moved, in_flight = inputs["moved"], inputs["in_flight"]
    record = build_record(source, sidecar, moved=moved, in_flight=in_flight)
    counterpart = writer_counterpart(source, sidecar, bulk=bulk, in_flight=in_flight)
    if _capture_records(inputs["document"]) is None and not any(
            json.loads(line).get("delta") for line in sidecar["files_lines"]):
        # §2.6 round-trip pin on a single-row-per-file sidecar.  A captured
        # sequence proves the same property through the fold (a delta-aware
        # round trip); its stored rows are the JOB's rows, not the record's copy.
        roundtrip_row_check(source, sidecar, bulk=bulk)
    files_text = "".join(sidecar["files_lines"])
    record_text = dump_record(record)
    counterpart_text = dump_record(counterpart)

    if name == LEGACY_TWIN:
        # No duplicated `/2` pair: the legacy source's counterpart IS the
        # deterministic `folder_refresh_trim/` pair.  Prove it byte for byte.
        twin = FIXTURE_ROOT / "folder_refresh_trim"
        stale = []
        for path, text in ((twin / "record.json", record_text),
                           (twin / EVIDENCE_FILE, files_text),
                           (twin / WRITER_RECORD_FILE, counterpart_text)):
            committed = path.read_text(encoding="utf-8") if path.exists() else None
            if committed != text:
                stale.append(str(path.relative_to(REPO_ROOT)))
        suffix = f" STALE {', '.join(stale)}" if stale else ""
        print(f"{'CHECK' if check else 'PROVEN'} {name}: source only; "
              f"counterpart == folder_refresh_trim/ byte for byte{suffix}")
        return {"name": name, "stale": stale, "bytes": 0}

    out_dir = FIXTURE_ROOT / name
    outputs = {out_dir / "record.json": record_text, out_dir / EVIDENCE_FILE: files_text,
               out_dir / WRITER_RECORD_FILE: counterpart_text}
    marker_record, marker_text = marker_counterpart(source, sidecar, bulk=bulk, in_flight=in_flight)
    outputs[out_dir / "marker.writer.record.json"] = dump_record(marker_record)
    outputs[out_dir / "marker.files.jsonl"] = marker_text
    if bulk:
        outputs[out_dir / NODES_FILE] = "".join(sidecar["nodes_lines"])
    stale = []
    for path, text in outputs.items():
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(str(path.relative_to(REPO_ROOT)))
        else:
            write_text(path, text)
    size = sum(len(text.encode("utf-8")) for text in outputs.values())
    suffix = f" STALE {', '.join(stale)}" if stale else ""
    print(f"{'CHECK' if check else 'BUILT'} {name}: rows={sidecar['counts']['rows']} "
          f"nodes={sidecar['counts']['nodes']} lines={len(sidecar['files_lines'])} "
          f"bytes={size}{suffix}")
    reason = WRITER_COUNTERPART_REASONS.get(name)
    if reason:
        print(f"   WRITER COUNTERPART ({WRITER_RECORD_FILE}): {reason}")
    for note in sidecar.get("derived_job_rows") or []:
        print(f"   JOB ROW PROVENANCE: derived, no captured aggregate input ({note})")
    for note in sidecar.get("reinserted_keys") or []:
        print(f"   ROW ORDER: re-inserted key(s) recorded as unset+set: {note}")
    if sidecar.get("sequence"):
        kinds = {}
        for step in sidecar["sequence"]:
            key = f"{step.get('cause') or 'run'}:{step['k']}"
            kinds[key] = kinds.get(key, 0) + 1
        print("   SEQUENCE: " + ", ".join(f"{key}={value}" for key, value in sorted(kinds.items())))
    return {"name": name, "stale": stale, "bytes": size}


# ---------------------------------------------------------------------------
# The synthetic mixed folder (clean, corrupt, unreadable, new, failed, ignored)
# ---------------------------------------------------------------------------
def _uuid(tag: str) -> str:
    import hashlib

    digest = hashlib.sha256(tag.encode("utf-8")).hexdigest()
    return f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def _sha(tag: str) -> str:
    import hashlib

    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


def _finding(name, line, code, severity, message):
    return {"file": name, "line": line, "code": code, "severity": severity,
            "view": "mapper", "authority": "map_source_snapshot", "beacon_id": None,
            "message": message, "detail": {}, "kind": code, "producer": "beacon.refresh",
            "observed": {"canonical_file": name.lower(), "file_sha256": _sha(name + code)},
            "members": [], "fix": None,
            "finding_id": f"f-{_sha(name + code + str(line))[:16]}"}


def _folder_row(name, **overrides):
    """One folder-run row in today's shape, with a projection of the same values."""
    row = {
        "file": name,
        "status": "refreshed",
        "changed": False,
        "findings": [],
        "warnings": [],
        "events": [],
        "repair_outcomes": [],
        "diagnostics": [],
        "check_target": {"verb": "check", "params": {"file_path": name, "min_severity": "info"}},
        "file_sha256": _sha(name),
        "duplicate_source_beacon_ids": {},
        "state": "valid",
        "preserve_existing": False,
        "reconcile_children": [],
        "create_allowed": True,
        "delete_allowed": True,
        "sections": [],
        "retained_uuids": [],
        "retained_sections": [],
        "reconcile_report": {"nodes_planned": 0},
        "ambiguities": [],
        "ambiguous_count": 0,
        "success": True,
        "file_node_id": _uuid(name),
        "source_path": name,
        "dry_run": False,
        "reconcile_result": None,
        "nodes_created": 0,
        "nodes_updated": 0,
        "nodes_deleted": 0,
        "nodes_moved": 0,
        "api_calls": 1,
        "_incremental_probe": {"used": False},
    }
    row.update(overrides)
    row["projection"] = {key: copy.deepcopy(row[key]) for key in FOLDER_PROJECTION_KEYS}
    return row


def mixed_folder_source():
    """The synthetic mixed folder: every outcome class in one small record."""
    base = "E:\\work\\mixed"
    clean = base + "\\clean.py"
    corrupt = base + "\\corrupt.py"
    unreadable = base + "\\unreadable.md"
    new = base + "\\new.py"
    failed = base + "\\failed.py"
    ignored = base + "\\tests\\.last_run.json"
    warned = base + "\\warned.css"
    many = base + "\\many_findings.py"
    e1 = "OSError: [Errno 13] Permission denied"
    e2 = "'E:\\work\\mixed\\unreadable.md' is not valid UTF-8"
    rows = [
        _folder_row(clean, nodes_created=2, nodes_updated=1, api_calls=3),
        _folder_row(
            corrupt,
            state="parse_failed",
            preserve_existing=True,
            create_allowed=False,
            delete_allowed=False,
            reconcile_children=None,
            findings=[_finding(corrupt, 4, "mapper_parse_error", "error",
                               "The mapper rejected the file; existing nodes are preserved.")],
        ),
        _folder_row(unreadable, status="failed", success=False, error=e2,
                    error_kind="not_utf8", file_sha256=None),
        _folder_row(new, changed=True, new_file=True, nodes_created=3, api_calls=2),
        _folder_row(
            failed,
            status="failed",
            success=False,
            error=e1,
            error_kind="io_error",
            warnings=[{"type": "file_refresh_failed", "error": e1}],
        ),
        _folder_row(ignored, status="completed", skipped_by_ignore=True,
                    skip_reason="segment:.last_run.json base=repo_root rel=tests/.last_run.json"),
        _folder_row(warned, warnings=[{"file": warned, "reason": "unsupported_extension"}]),
        _folder_row(
            many,
            findings=[_finding(many, line, "empty_comment", "info",
                               f"Beacon block {line} carries no comment.")
                      for line in range(1, 251)],
        ),
    ]
    messages = [e1, e2]
    summary = compose_source_summary(
        rows,
        run_events=_error_events(messages, bulk=False),
        counters={
            "files_refreshed": 5,
            "files_deleted": 0,
            "new_files_created": 1,
            "new_folders_created": 0,
            "errors": messages,
            "refresh_errors": messages,
            "present_paths": [clean, corrupt, unreadable, new, failed, ignored, warned, many],
            # CF-159/B8: a folder's complete ignore-source and missing-path lists
            # live in the evidence file as item lines, never whole in a record.
            "nexusignore_sources": [base + "\\scratch\\*.log", base + "\\tests\\.last_run.*"],
            "missing_paths": [base + "\\gone.py"],
        },
    )
    return {
        "id": f"carto-refresh-folder-{_sha('mixed-folder')[:8]}",
        "type": "CARTO_REFRESH",
        "mode": "folder",
        "root_uuid": _uuid("mixed-folder"),
        "status": "completed_with_warnings",
        "error": None,
        "created_at": "2026-10-26T11:49:08.564321+00:00",
        "updated_at": "2026-10-26T13:46:47.711093",
        "progress": {"total_files": len(rows), "completed_files": len(rows),
                     "current_phase": "folder:complete", "nodes_created": 5,
                     "nodes_updated": 1, "nodes_moved": 0, "nodes_deleted": 0},
        "execution_model": "daemon_task",
        "execution_owner": "launcher",
        "detached": False,
        "cache_refresh_required": True,
        "freshness_disposition": "current",
        "snapshot_path": "C:\\Users\\daniel\\AppData\\Local\\workflowy-mcp\\nodes_export_cache\\nodes.json",
        "logs_path": "C:\\Users\\daniel\\AppData\\Local\\workflowy-mcp\\cartographer_jobs\\mixed.log",
        "claim_id": _uuid("mixed-claim"),
        "dedupe_key": "mixed-folder-fixture",
        "account_scope_id": "fixture-account",
        "footprint": {"root_uuid": _uuid("mixed-folder"), "path": base},
        "launcher": "system.job.status",
        "parent_job_id": None,
        "handoff_state": "completed",
        "pid": 4242,
        "result_summary": summary,
    }


def write_mixed_source(*, check=False):
    path = MIXED_SOURCE
    text = dump_source_1(mixed_folder_source())
    if check:
        return path.exists() and path.read_text(encoding="utf-8") == text
    write_text(path, text)
    print(f"WROTE {path.relative_to(REPO_ROOT)} ({path.stat().st_size} bytes)")
    return True


# ---------------------------------------------------------------------------
# The synthetic bulk receipt evidence (WaveSpec §3.1 *Bulk receipts*, A1 (iii)-(v))
# ---------------------------------------------------------------------------
def _bulk_receipt(node, path, *, changed=False, satisfied=True, success=True, error=None):
    receipt = {"file_path": path, "source_path": path, "node_id": node, "changed": changed,
               "satisfied": bool(success and not error), "writer_success": bool(success and not error),
               "success": bool(success), "noop_reason": None}
    if error:
        receipt.update(error=error, success=False, satisfied=False, writer_success=False)
    return receipt


def _bulk_row(path, **extra):
    row = {"file": path, "file_node_id": "id-" + path[-16:], "status": "completed",
           "changed": False, "findings": [], "warnings": [], "events": []}
    row.update(extra)
    return row


def _bulk_placeholder(path):
    """Today's group-head placeholder (``file_outcomes.setdefault``, server.py 13068)."""
    return {"file": path, "file_node_id": "id-" + path[-16:], "status": "failed",
            "changed": False, "findings": [], "warnings": [], "events": [],
            "error": "File attempt interrupted before completion"}


def _replay(row, *, sets=(), pops=(), extends=()):
    """One group's in-place updates on a row, in the order today's code applies them.

    ``pop`` first (``active_file.pop("error", None)``), then each assignment /
    ``dict.update`` entry in the order the group makes it, then each list
    append (``setdefault(key, []).append`` / slice assignment).  Building the
    synthetic rows this way is what makes their field order — and therefore the
    delta lines and the per-row key-order oracle — truthful rather than
    hand-ordered.
    """
    result = dict(row)
    for key in pops:
        result.pop(key, None)
    for key, value in sets:
        result[key] = value
    for key, values in extends:
        result[key] = list(result.get(key) or []) + list(values)
    return result


def _refresh_data(path, *, nodes_created=0, status="refreshed"):
    """The follow-up FILE refresh's returned data (the keys ``active_file.update`` takes)."""
    return {
        "success": True, "repair_outcomes": [], "diagnostics": [],
        "check_target": {"verb": "check", "params": {"file_path": path, "min_severity": "info"}},
        "file_sha256": _sha(path), "duplicate_source_beacon_ids": {},
        "state": "valid", "preserve_existing": False, "reconcile_children": [],
        "create_allowed": True, "delete_allowed": True, "sections": [],
        "retained_uuids": [], "retained_sections": [],
        "reconcile_report": {"nodes_planned": 0}, "ambiguities": [], "ambiguous_count": 0,
        "source_path": path, "dry_run": False, "reconcile_result": None,
        "nodes_created": nodes_created, "nodes_updated": 0, "nodes_deleted": 0,
        "nodes_moved": 0, "api_calls": 1, "_incremental_probe": {"used": False},
        "status": status, "changed": False,
    }


def _finalize_own_group(path, receipt, *, findings=(), refresh=True, nodes_created=0):
    """The row today's code leaves at the end of a file's OWN group (server.py 13234–13321)."""
    row = _bulk_placeholder(path)
    row = _replay(row, sets=[("status", "completed"), ("success", True)], pops=["error"]) 
    row = _replay(row, sets=[("final_observation", False),
                             ("node_outcomes", list(receipt)), ("nodes_satisfied", 1),
                             ("changed", True)])
    if refresh:
        data = _refresh_data(path, nodes_created=nodes_created)
        row = _replay(row, sets=[(key, value) for key, value in data.items()
                                 if key not in {"changed", "file", "node_outcomes", "nodes_satisfied"}])
        row = _replay(row, sets=[("refresh_outcome", data)])
    row = _replay(row, sets=[("final_observation", True)])
    if findings:
        row["findings"] = list(findings)
    return row


def _observed_other_group(row, receipts):
    """What a LATER group's ``sync_active_file`` + cross-file observation does to a row."""
    row = _replay(row, sets=[("final_observation", False),
                             ("node_outcomes", list(receipts)),
                             ("nodes_satisfied", sum(bool(item.get("satisfied")) for item in receipts)),
                             ("changed", True)])
    return _replay(row, sets=[("final_observation", True)])


def _bulk_summary(rows, receipts, errors=(), run_warnings=()):
    aggregate, outcome = _composer()
    summary = outcome(
        aggregate(rows, warnings=list(run_warnings),
                  events=[{"type": "bulk_error", "error": message} for message in errors]),
        owns_aggregate=True,
    )
    summary["node_outcomes"] = list(receipts)
    summary["errors"] = list(errors)
    summary["node_updates"] = 3
    summary["nodes_skipped"] = 2
    summary["nodes_satisfied"] = sum(bool(item.get("satisfied")) for item in receipts)
    return summary


def _bulk_record(summary, *, status="running", completed_files=4, current_file=None,
                 phase="update-beacons", **extra):
    payload = {
        "id": "carto-bulk-apply-7f3c21d0", "type": "CARTO_BULK_APPLY", "mode": "file",
        "root_uuid": _uuid("bulk-synthetic-root"), "status": status, "error": None,
        "created_at": "2026-10-27T10:00:00.000000+00:00",
        "updated_at": "2026-10-27T10:10:00.000000",
        "progress": {"total_files": 5, "completed_files": completed_files,
                     "current_file": current_file, "current_phase": phase,
                     "nodes_created": 0, "nodes_updated": 3, "nodes_moved": 0, "nodes_deleted": 0},
        "result_summary": summary,
    }
    payload.update(extra)
    return payload


def synthetic_bulk_source():
    """Synthetic bulk evidence for WaveSpec §3.1's *Bulk receipts* cases.

    Synthetic, and labelled so: this is not harness provenance.  Its records are
    GROUP FINALIZATIONS, not every persist: each group head carries
    ``progress.current_file`` (so the fixture also proves a group-head
    placeholder is not a row line) and each boundary carries ``completed_files``
    one higher with ``final_observation: true`` on every finalized row.  It
    covers

    * (i)  an abrupt end: ``interrupted`` has receipts and no row line, and the
           record names it in ``in_flight``;
    * (ii) one physical file finalized under two groups, one receipt rejected,
           in both orderings: ``own_then_other_rejected`` (own group first, the
           later group's receipt rejected) and ``other_then_own_rejected``
           (other group first, which keeps the earlier ``findings``, then its
           own group's receipt rejected);
    * (iii) A1(iii): ``own_then_other`` is refreshed by its own group and then
           written by another group; its refresh keys survive beside both
           receipts;
    * (v)  ``in_sync``: every candidate passed the in-sync filter, so the row
           carries ``node_outcomes: []`` and ``nodes_satisfied: 0``.

    A1(iv) (a cancellation inside a group, whose stop-path row the G5 writer
    chooses) is deliberately absent: it needs the harness case and the writer's
    choice of what a stop leaves, and this fixture must not invent either.
    """
    own_then_other = "E:\\work\\bulk\\own_then_other.py"
    own_then_other_rejected = "E:\\work\\bulk\\own_then_other_rejected.py"
    other_then_own_rejected = "E:\\work\\bulk\\other_then_own_rejected.py"
    interrupted = "E:\\work\\bulk\\interrupted.py"
    in_sync = "E:\\work\\bulk\\in_sync.py"
    rejection = "Node write not satisfied"
    node_id = lambda path: "id-" + path[-16:]

    a_own = _bulk_receipt("own-a", own_then_other, changed=True)
    a_other = _bulk_receipt("other-a", own_then_other)
    c_own = _bulk_receipt("own-c", own_then_other_rejected, changed=True)
    c_reject = _bulk_receipt("other-c", own_then_other_rejected, error=rejection)
    d_other = _bulk_receipt("other-d", other_then_own_rejected, changed=True)
    d_reject = _bulk_receipt("own-d", other_then_own_rejected, error=rejection)
    e_receipt = _bulk_receipt("e-1", interrupted, changed=True)
    kept_finding = _finding(other_then_own_rejected, 2, "empty_comment", "info", "kept finding")

    # Group 1: own_then_other's own group refreshes it; other_then_own_rejected is
    # touched in another FILE (its first receipt) and observed.
    row_a1 = _finalize_own_group(own_then_other, [a_own], nodes_created=1)
    row_c1 = _replay(_bulk_placeholder(other_then_own_rejected),
                     sets=[("status", "completed"), ("success", True)], pops=["error"])
    row_c1 = _replay(row_c1, sets=[("findings", [kept_finding]),
                                   ("final_observation", False),
                                   ("node_outcomes", [d_other]), ("nodes_satisfied", 1),
                                   ("changed", True)])
    row_c1 = _replay(row_c1, sets=[("state", "valid"), ("preserve_existing", False),
                                   ("check_target", {"verb": "check", "params": {
                                       "file_path": other_then_own_rejected,
                                       "min_severity": "info"}}),
                                   ("file_sha256", _sha(other_then_own_rejected)),
                                   ("source_path", other_then_own_rejected), ("dry_run", True),
                                   ("_incremental_probe", {"used": False}),
                                   ("final_observation", True)])
    # Group 2: own_then_other_rejected's own group; own_then_other is written from it.
    row_b1 = _finalize_own_group(own_then_other_rejected, [c_own])
    row_a2 = _observed_other_group(row_a1, [a_own, a_other])
    # Group 3: other_then_own_rejected's own group (rejected receipt, earlier
    # findings kept) and own_then_other_rejected's later rejected receipt.
    row_c2 = _replay(row_c1, sets=[("status", "completed"), ("success", True)])
    row_c2 = _replay(row_c2, sets=[("final_observation", False),
                                   ("node_outcomes", [d_other, d_reject]),
                                   ("nodes_satisfied", 1), ("changed", True)])
    row_c2 = _replay(row_c2, sets=[("status", "failed"), ("success", False),
                                   ("error", rejection), ("final_observation", True)])
    row_b2 = _replay(row_b1, sets=[("final_observation", False),
                                   ("node_outcomes", [c_own, c_reject]),
                                   ("nodes_satisfied", 1), ("changed", True)])
    row_b2 = _replay(row_b2, sets=[("status", "failed"), ("success", False),
                                   ("error", rejection), ("final_observation", True)])
    # Group 4: in_sync's own group — no candidate passed the filter.
    row_in_sync = _replay(_bulk_placeholder(in_sync),
                          sets=[("status", "completed"), ("success", True)], pops=["error"])
    row_in_sync = _replay(row_in_sync, sets=[("node_outcomes", []), ("nodes_satisfied", 0)])
    row_in_sync = _replay(row_in_sync, sets=[("state", "valid"), ("preserve_existing", False),
                                             ("check_target", {"verb": "check", "params": {
                                                 "file_path": in_sync, "min_severity": "info"}}),
                                             ("file_sha256", _sha(in_sync)),
                                             ("source_path", in_sync), ("dry_run", True),
                                             ("_incremental_probe", {"used": False}),
                                             ("final_observation", True)])

    receipts_1 = [a_own, d_other]
    receipts_2 = receipts_1 + [a_other, c_own]
    receipts_3 = receipts_2 + [c_reject, d_reject]
    # §2.5's row position is its FIRST appearance in today's `file_outcomes`
    # (`setdefault` order): group 1 inserts own_then_other (its own file) and
    # other_then_own_rejected (a cross-file receipt), group 2 inserts
    # own_then_other_rejected, group 4 inserts in_sync.
    rows_1 = [row_a1, row_c1]
    rows_2 = [row_a2, row_c1, row_b1]
    rows_3 = [row_a2, row_c2, row_b2]
    rows_4 = rows_3 + [row_in_sync]
    snapshots = [
        # group 1 head (placeholder only — NOT a row line)
        _bulk_record(_bulk_summary([_bulk_placeholder(own_then_other)], []),
                     completed_files=0, current_file=node_id(own_then_other),
                     phase="update-beacons"),
        _bulk_record(_bulk_summary(rows_1, receipts_1), completed_files=1,
                     phase="refresh-file"),
        # group 2 head
        _bulk_record(_bulk_summary([row_a1, row_c1,
                                    _bulk_placeholder(own_then_other_rejected)],
                                   receipts_1),
                     completed_files=1, current_file=node_id(own_then_other_rejected),
                     phase="update-beacons"),
        _bulk_record(_bulk_summary(rows_2, receipts_2), completed_files=2,
                     phase="refresh-file"),
        # group 3 head
        _bulk_record(_bulk_summary(rows_2, receipts_2), completed_files=2,
                     current_file=node_id(other_then_own_rejected), phase="update-beacons"),
        _bulk_record(_bulk_summary(rows_3, receipts_3), completed_files=3,
                     phase="refresh-file"),
        # group 4 head
        _bulk_record(_bulk_summary(rows_3, receipts_3), completed_files=3,
                     current_file=node_id(in_sync), phase="update-beacons"),
        _bulk_record(_bulk_summary(rows_4, receipts_3), completed_files=4,
                     phase="update-beacons"),
        # abrupt end inside the fifth group: receipts, no row line, in_flight
        _bulk_record(_bulk_summary(rows_4, receipts_3 + [e_receipt]),
                     status="failed", error="File attempt interrupted before completion",
                     error_kind="carto_job_exception", completed_files=4,
                     current_file=node_id(interrupted), phase="update-beacons"),
    ]

    return {
        "kind": "bulk",
        "reconstruction": "normalized from recorded group finalizations (the BASE runtime "
                          "emitted whole JSON records, not JSONL lines)",
        "synthetic": {
            "cases": {
                "own_then_other": "(iii) own group refreshes, then another group writes; "
                                  "refresh keys survive beside both receipts",
                "own_then_other_rejected": "(ii) own group first, other group's receipt rejected",
                "other_then_own_rejected": "(ii) other group first (keeps its findings), "
                                           "own group's receipt rejected",
                "interrupted": "(i) abruptly ended: receipts, no row line, in_flight",
                "in_sync": "(v) every candidate in sync: no receipts, nodes_satisfied 0",
            },
            "absent": "A1(iv): a cancellation inside a group; needs the harness case and the "
                      "G5 writer's stop-path row, so it is not invented here.",
        },
        "in_flight": {"file": interrupted, "file_node_id": node_id(interrupted),
                      "status": "failed", "changed": False, "findings": [], "warnings": [],
                      "events": [], "error": "File attempt interrupted before completion",
                      "base_lines": None},
        "records": snapshots,
    }


def write_synthetic_bulk_source(*, check=False):
    path = SYNTHETIC_BULK_SOURCE
    document = synthetic_bulk_source()
    text = json.dumps(document, indent=1, ensure_ascii=True)
    if check:
        return path.exists() and path.read_text(encoding="utf-8") == text
    write_text(path, text)
    print(f"WROTE {path.relative_to(REPO_ROOT)} ({path.stat().st_size} bytes)")
    return True


# ---------------------------------------------------------------------------
# Trimming the archived 1,063-row folder record into a source fixture
# ---------------------------------------------------------------------------
def trim_source(path: Path):
    """Write ``sources/folder_refresh_trim.record.json`` from the archived copy.

    Never edits the copy or the live ``cartographer_jobs`` tree.  The head
    fields are the captured record's; the row selection is a deterministic
    slice of the real rows -- the 4 ignore-skip rows, every changed row, three
    clean rows, the smallest row carrying an error-severity finding, and then
    the smallest finding-bearing rows that still fit the 2 MB cap.  The summary
    is recomposed with today's composer over exactly those rows, so the source
    is the record the runtime would have written for them (never a hand-edited
    total).
    """
    record = load_source(path)
    rows = record["result_summary"]["files"]
    sizes = [len(compact(row).encode("utf-8")) for row in rows]
    skip = [i for i, row in enumerate(rows) if row.get("status") != "refreshed"]
    changed = [i for i, row in enumerate(rows) if row.get("changed")]
    finding = [i for i, row in enumerate(rows) if row.get("findings")]
    clean = [i for i, row in enumerate(rows)
             if not row.get("changed") and not row.get("findings")
             and row.get("status") == "refreshed"]
    error_rows = [i for i in finding
                  if any(f.get("severity") == "error" for f in rows[i]["findings"])]
    base = list(skip) + list(changed)
    base.extend(sorted(clean, key=lambda i: (sizes[i], i))[:3])
    base.append(min(error_rows, key=lambda i: (sizes[i], i)))
    base = list(dict.fromkeys(base))

    def assemble(indices):
        kept_rows = [rows[index] for index in sorted(indices)]
        summary = compose_source_summary(
            kept_rows,
            counters={
                "files_refreshed": sum(1 for row in kept_rows if row.get("status") == "refreshed"),
                "files_deleted": 0,
                "new_files_created": sum(1 for row in kept_rows if row.get("new_file")),
                "new_folders_created": 0,
                "errors": [],
                "refresh_errors": [],
            },
        )
        trimmed = {key: value for key, value in record.items() if key != "result_summary"}
        progress = dict(record.get("progress") or {})
        progress["total_files"] = len(kept_rows)
        progress["completed_files"] = len(kept_rows)
        trimmed["progress"] = progress
        trimmed["result_summary"] = summary
        return kept_rows, dump_source_1(trimmed)

    _, base_text = assemble(base)
    if len(base_text.encode("utf-8")) > SOURCE_MAX_BYTES:
        raise SystemExit(f"the mandatory trim base is {len(base_text.encode('utf-8'))} bytes, "
                         f"over the {SOURCE_MAX_BYTES}-byte cap")
    selected = list(base)
    for index in sorted(finding, key=lambda i: (sizes[i], i)):
        if index in selected:
            continue
        trial = sorted(selected + [index])
        _, trial_text = assemble(trial)
        if len(trial_text.encode("utf-8")) <= SOURCE_MAX_BYTES:
            selected = trial
    kept, text = assemble(selected)
    size = len(text.encode("utf-8"))
    write_text(TRIM_SOURCE, text)
    print(f"WROTE {TRIM_SOURCE.relative_to(REPO_ROOT)}: {len(kept)} rows, {size} bytes "
          f"({len(changed)} changed, {len(skip)} skip, "
          f"{sum(1 for i in selected if i in set(finding))} with findings)")
    for index in selected:
        row = rows[index]
        print(f"   row {index:4d} {sizes[index]:8d} findings={len(row.get('findings') or []):3d} "
              f"changed={bool(row.get('changed'))} status={row.get('status')}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="verify committed bytes instead of writing")
    parser.add_argument("--only", nargs="+", default=None, help="build only these fixtures")
    parser.add_argument("--trim-source", metavar="PATH",
                        help="(re)write the trimmed folder source from an archived record copy")
    parser.add_argument("--write-mixed-source", action="store_true",
                        help="(re)write the synthetic mixed-folder source record")
    parser.add_argument("--write-synthetic-bulk-source", action="store_true",
                        help="(re)write the synthetic bulk-receipt capture")
    args = parser.parse_args(argv)

    if args.trim_source:
        trim_source(Path(args.trim_source))
    if args.write_mixed_source:
        write_mixed_source()
    if args.write_synthetic_bulk_source:
        write_synthetic_bulk_source()
    missing, stale = [], []
    generated = args.trim_source or args.write_mixed_source or args.write_synthetic_bulk_source
    if not generated:
        for path, writer in ((MIXED_SOURCE, write_mixed_source),
                             (SYNTHETIC_BULK_SOURCE, write_synthetic_bulk_source)):
            if args.check:
                if not writer(check=True):
                    stale.append(str(path.relative_to(REPO_ROOT)))
            else:
                writer()
    if not ensure_legacy_source(check=args.check):
        if args.check:
            stale.append(str(LEGACY_SOURCE.relative_to(REPO_ROOT)))
        else:
            missing.append(str(LEGACY_SOURCE.relative_to(REPO_ROOT)))
    for name, (source_path, bulk) in fixture_targets().items():
        result = build_one(name, source_path, bulk, check=args.check, only=args.only)
        if not result:
            continue
        if result.get("missing"):
            missing.append(result["missing"])
        stale.extend(result.get("stale") or [])
    if PROJECTION_ORDER_NOTES:
        print(f"PROJECTION ORDER NOTES: {len(PROJECTION_ORDER_NOTES)} row(s) kept explicit "
              f"to stay lossless: {PROJECTION_ORDER_NOTES[:3]}")
    else:
        print("PROJECTION ORDER NOTES: none (every single-stored row re-expands byte-identically)")
    if JOB_ROW_PROVENANCE_NOTES:
        print(f"JOB ROW PROVENANCE: {len(JOB_ROW_PROVENANCE_NOTES)} record(s) derived job rows "
              f"(no captured aggregate INPUT): {JOB_ROW_PROVENANCE_NOTES[:3]}")
    else:
        print("JOB ROW PROVENANCE: every job row is a captured aggregate INPUT")
    if ROW_ORDER_NOTES:
        print(f"ROW ORDER: {len(ROW_ORDER_NOTES)} finalization(s) re-inserted a key "
              f"(recorded as unset+set): {ROW_ORDER_NOTES[:3]}")
    else:
        print("ROW ORDER: no finalization moved a key (no pop/re-insert to record)")
    if missing:
        print("MISSING FIXTURE SOURCES (produced once by the J-3/J-4 joined harness, then frozen):")
        for item in missing:
            print(f"   {item}")
    if args.check and stale:
        print(f"CHECK FAILED: {len(stale)} stale file(s)")
        return 1
    if args.check:
        print("CHECK OK: every committed fixture matches its source"
              + (f" (INCOMPLETE: {len(missing)} fixture source(s) still missing)" if missing else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
