"""CARTO history records and their complete, on-demand evidence.

The shared head cache is for classification, inventories and summaries only.
Whole old-layout records and evidence are read only for a requested job. The
job-owned writer registry routes switched refreshes without an alternate writer.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import threading
import bisect
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .config import CARTO_TERMINAL_STATUSES, read_carto_job_record_head

CARTO_JOB_RECORD_LAYOUT_V2 = "summary-head/2"
FAILED_FILE_STATUSES = frozenset({"failed", "error", "unavailable", "parse_failed"})
_KNOWN_ACTIVE_STATUSES = frozenset({"queued", "waiting", "pending", "running", "cancelling", "preparing", "starting"})
_SUMMARY_ONLY_KEYS = frozenset({
    "errors_total", "node_errors_total", "phase8_failed", "files_changed_total",
    "events_total", "findings_by_severity", "phase8_summary", "findings_preview", "preview_totals",
})
_TERMINAL_TEXT_FORMATTER = "_carto_terminal_payload_data:str/1"
_LOG = logging.getLogger(__name__)
_record_cache: dict[str, tuple[tuple[int, int], dict | None, str | None]] = {}
_record_cache_lock = threading.RLock()
_record_ids: dict[tuple[str, str], list[str]] = {}
_path_ids: dict[str, tuple[str, str]] = {}


def _count(value: Any) -> int:
    try:
        return int(value or 0)
    except (ValueError, TypeError, OverflowError):
        return 0


def _carto_terminal_text_recipe(record):
    """Recognize Q6's bounded stand-in, never interpret an unknown formatter."""
    marker = record.get("error_derivation")
    recipe = marker.get("$carto_terminal_text") if isinstance(marker, Mapping) else None
    if (isinstance(recipe, Mapping) and recipe.get("formatter") == _TERMINAL_TEXT_FORMATTER
            and recipe.get("source") in ("errors", "refresh_errors")
            and isinstance(recipe.get("prefix"), str) and isinstance(recipe.get("suffix"), str)):
        return recipe
    return None


def carto_job_outcome(record: Mapping[str, Any], *, include_counts=False) -> dict[str, Any]:
    """Derive presentation facets without changing the record's status authority.

    The legacy Markdown failure count survives a head read in
    ``warnings_by_type.phase8_refresh_failed`` (CF-174); ``phase8`` itself is
    an old-layout tail and is not needed by an inventory.
    """
    summary = record.get("result_summary")
    summary = summary if isinstance(summary, Mapping) else {}
    phase8 = summary.get("phase8_summary", summary.get("phase8"))
    phase8_failed = bool(summary.get("phase8_failed") or (
        isinstance(phase8, Mapping) and (phase8.get("success") is False or phase8.get("error"))))
    warning_types = summary.get("warnings_by_type")
    if isinstance(warning_types, Mapping) and _count(warning_types.get("phase8_refresh_failed")):
        phase8_failed = True
    errors = (_count(summary["errors_total"]) if "errors_total" in summary else
              _count(summary.get("failed")) + _count(summary.get("node_errors_total")) + int(phase8_failed))
    findings = _count(summary.get("findings_total"))
    events = summary.get("events")
    has_warnings = bool(_count(summary.get("warnings_total")) or _count(record.get("warning_count")) or
                        _count(summary.get("events_total", len(events) if isinstance(events, (list, tuple)) else 0)) or
                        _count(summary.get("skipped")))
    status = str(record.get("status") or "").strip().lower()
    if status not in CARTO_TERMINAL_STATUSES:
        outcome = "running"
    elif status in {"failed", "cancelled"}:
        outcome = status
    elif errors > 0:
        outcome = "completed_with_errors"
    elif status == "completed_with_warnings" or has_warnings or findings > 0:
        outcome = "completed_with_warnings"
    else:
        outcome = "completed"
    result = {"outcome": outcome, "has_errors": errors > 0,
              "has_warnings": has_warnings, "has_findings": findings > 0}
    if include_counts:
        result.update(errors_total=errors, findings_total=findings)
    return result


# @beacon[
#   id=carto-record@read-cached,
#   role=read_carto_job_record_cached(path),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=The one signature-validated history head reader for lists classification cleanup and summaries; derives outcome and warns on unknown status or terminal-text markers once per signature. Remembers malformed JSON but not IO failures and never opens evidence.,
# ]
def read_carto_job_record_cached(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read all three layouts, with at most one head decode per file signature.

    The lock covers the decode, not just publication: concurrent loop/worker
    readers cannot both decode a newly observed signature.  The id index is
    disposable and populated only by these reads; lookup never scans contents.
    """
    path = Path(path)
    name = str(path)
    with _record_cache_lock:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = _record_cache.get(name)
        if cached is None or cached[0] != signature:
            try:
                record = read_carto_job_record_head(path)
                if not isinstance(record, dict):
                    raise ValueError("job record is not an object")
                record = {**record, **carto_job_outcome(record)}
                status = str(record.get("status") or "").strip().lower()
                if status not in CARTO_TERMINAL_STATUSES | _KNOWN_ACTIVE_STATUSES:
                    _LOG.warning("Unknown Cartographer job status %r in %s; presented as running", status, path)
                if (record.get("record_layout") == CARTO_JOB_RECORD_LAYOUT_V2 and
                        "error_derivation" in record and _carto_terminal_text_recipe(record) is None):
                    _LOG.warning("Unknown Cartographer terminal-text derivation in %s; bounded error and marker retained", path)
                cached = (signature, record, None)
                jid = str(record.get("id") or path.stem)
                key = (str(path.parent), jid)
                old_key = _path_ids.get(name)
                if old_key is not None and old_key != key:
                    old_paths = _record_ids.get(old_key, [])
                    if name in old_paths:
                        old_paths.remove(name)
                    if not old_paths:
                        _record_ids.pop(old_key, None)
                _path_ids[name] = key
                paths = _record_ids.setdefault(key, [])
                if name in paths:
                    paths.remove(name)
                paths.append(name)
            except OSError:
                raise
            except Exception as exc:
                cached = (signature, None, str(exc))
            _record_cache[name] = cached
        if cached[1] is None:
            raise ValueError(cached[2])
        return dict(cached[1])


def prune_carto_job_record_cache(paths: Iterable[Path] | None = None, *, directory=None) -> None:
    """Forget vanished paths; a scan passes its names to avoid a second stat."""
    with _record_cache_lock:
        if paths is None:
            gone = {name for name in _record_cache if not os.path.exists(name)}
        else:
            present = {str(path) for path in paths}
            gone = {name for name in _record_cache if (directory is None or Path(name).parent == Path(directory))
                    and name not in present}
        for name in gone:
            _record_cache.pop(name, None)
            _path_ids.pop(name, None)
        for key, names in list(_record_ids.items()):
            names[:] = [name for name in names if name not in gone]
            if not names:
                del _record_ids[key]


def find_carto_job_record_path(base, job_id: str) -> Path | None:
    """Match a job's filename first, then the shared reader's cached id index.

    The last matching filename (or last indexed duplicate) wins, matching the
    inventory's ordering without opening any unrelated record.  Noncanonical
    historical filenames become discoverable when startup/the inventory reads
    them; this lookup never performs a cold content scan.
    """
    base = Path(base)
    # Compare names rather than interpolating a user id into a glob grammar.
    matches = [path for path in base.glob("*.json")
               if path.name == f"{job_id}.json" or path.name.endswith(f"_{job_id}.json")]
    if matches:
        return matches[-1]
    with _record_cache_lock:
        names = _record_ids.get((str(base), str(job_id)), ())
        # The disposable index is not history authority: a cleared record may
        # still be indexed until the next list scan prunes it.
        return next((Path(name) for name in reversed(names) if Path(name).exists()), None)


def canonical_carto_file(value: Any) -> str:
    """The physical-file identity used by rows and receipt tallies."""
    return os.path.realpath(str(value)) if value is not None else ""


def tally_bulk_receipt(tally: Mapping[str, Any] | None, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Add one receipt, in return order, retaining the last rejection's text."""
    tally = dict(tally or {"count": 0, "satisfied": 0, "changed": False, "rejected_error": None})
    tally["count"] += 1
    tally["satisfied"] += int(bool(receipt.get("satisfied")))
    tally["changed"] = bool(tally["changed"] or (receipt.get("changed") and not receipt.get("error")))
    if receipt.get("success") is False or receipt.get("error"):
        tally["rejected_error"] = receipt.get("error") or receipt.get("noop_reason") or "Node write not satisfied"
    return tally


def apply_bulk_receipts(row: Mapping[str, Any], tally: Mapping[str, Any], *, reflected: int,
                        node_outcomes: list | None) -> dict[str, Any]:
    """Apply sync_active_file's effects in its original insertion/update order."""
    result = dict(row)
    if tally["count"] != reflected:
        result["final_observation"] = False
    result["node_outcomes"] = node_outcomes
    result["nodes_satisfied"] = tally["satisfied"]
    result["changed"] = bool(result.get("changed") or tally["changed"])
    if tally["rejected_error"]:
        result.update(status="failed", success=False, error=tally["rejected_error"])
    return result


def expand_carto_projection(line: Mapping[str, Any]) -> dict[str, Any]:
    """Expand only a line's explicit proj marker, before any delta overlay."""
    row = copy.deepcopy(line["row"])
    if "proj" in line:
        row["projection"] = {key: copy.deepcopy(row[key]) for key in line["proj"]}
    return row


def overlay_carto_row(previous: Mapping[str, Any], row: Mapping[str, Any], delta: Mapping[str, Any]) -> dict:
    """Exactly dict pop, update, then setdefault(list).extend, in that order."""
    result = dict(previous)
    for key in delta.get("unset") or ():
        result.pop(key, None)
    result.update(row)
    for key, values in (delta.get("extend") or {}).items():
        result[key] = list(result.get(key) or []) + list(values)
    return result


def _bulk_base_row(file) -> dict:
    return {"file": file, "status": "completed", "changed": False, "findings": [], "warnings": [], "events": []}


def parse_carto_evidence_lines(text_or_lines) -> dict[str, Any]:
    """Decode complete JSONL lines only; preserve unknown objects as data.

    Offsets and lengths are UTF-8 byte positions, suitable for a later row
    read.  An iterable of already decoded objects is useful for pure tests.
    A torn tail is deliberately not decoded.
    """
    if isinstance(text_or_lines, str):
        text_or_lines = text_or_lines.encode("utf-8", errors="surrogatepass")
    lines, unreadable, torn = [], 0, 0
    if isinstance(text_or_lines, (bytes, bytearray)):
        committed, sep, tail = bytes(text_or_lines).rpartition(b"\n")
        if not sep:
            return {"lines": [], "torn_tail_bytes": len(text_or_lines), "unreadable_lines": 0}
        torn = len(tail)
        position = 0
        for index, raw in enumerate(committed.split(b"\n")):
            length = len(raw) + 1
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("evidence line is not an object")
                lines.append((index, position, length, value))
            except (ValueError, UnicodeError):
                unreadable += 1
            position += length
    else:
        for index, value in enumerate(text_or_lines or ()):
            if isinstance(value, Mapping):
                lines.append((index, None, None, dict(value)))
            else:
                try:
                    value = json.loads(value)
                    if not isinstance(value, dict):
                        raise ValueError("evidence line is not an object")
                    lines.append((index, None, None, value))
                except (ValueError, UnicodeError):
                    unreadable += 1
    return {"lines": lines, "torn_tail_bytes": torn, "unreadable_lines": unreadable}


# @beacon[
#   id=carto-record@fold-evidence,
#   role=fold_carto_job_evidence(text_or_lines, in_flight, status, nodes, job_type),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=Pure ordered evidence fold: projection expansion precedes full/delta overlays, receipts attach by physical file and interrupted placeholders enter only terminal jobs. Complete landed lines remain visible beside torn and unreadable counters.,
# ]
def fold_carto_job_evidence(text_or_lines, *, in_flight=None, status=None, nodes=None, job_type=None) -> dict[str, Any]:
    """Fold ordered full/delta rows, values, lists and bulk receipts losslessly.

    CF-160: an in-flight placeholder becomes an interrupted row only for a
    terminal record.  A running record exposes it separately as in_progress,
    never as a failed file.  Callers pass the record's status explicitly.
    """
    parsed = parse_carto_evidence_lines(text_or_lines)
    parsed_nodes = parse_carto_evidence_lines(nodes)
    rows: dict[tuple[str, str], dict] = OrderedDict()
    latest, reflected, bulk_keys, items, values, unknown = {}, {}, set(), {}, {}, []
    rows_on_disk = 0
    for index, _position, _length, line in parsed["lines"]:
        kind = line.get("k")
        if kind == "row" and isinstance(line.get("row"), Mapping):
            row = expand_carto_projection(line)
            key = (str(line.get("sec") or "files"), canonical_carto_file(row.get("file")))
            if "delta" in line:
                row = overlay_carto_row(rows.get(key, _bulk_base_row(row.get("file"))), row, line["delta"])
            rows[key] = row
            latest[key] = index
            reflected[key] = _count(line.get("nodes"))
            if "nodes" in line:
                bulk_keys.add(key[1])
            rows_on_disk += 1
        elif kind == "item" and "key" in line:
            items.setdefault(line["key"], []).append(copy.deepcopy(line.get("v")))
        elif kind == "set" and "key" in line:
            key = tuple(line["key"]) if isinstance(line["key"], list) else line["key"]
            values[key] = copy.deepcopy(line.get("v"))
        else:
            unknown.append(copy.deepcopy(line))
    in_progress = None
    if isinstance(in_flight, Mapping):
        row = {key: copy.deepcopy(value) for key, value in in_flight.items() if key != "base_lines"}
        key = ("files", canonical_carto_file(row.get("file")))
        if latest.get(key, -1) < _count(in_flight.get("base_lines")):
            if str(status or "").strip().lower() in CARTO_TERMINAL_STATUSES:
                rows[key] = row
                reflected[key] = 0
                if job_type == "CARTO_BULK_APPLY":
                    bulk_keys.add(key[1])
            else:
                in_progress = {**row, "status": "running", "error": None}
    receipts: dict[str, list] = OrderedDict()
    tallies, global_receipts = {}, []
    for _index, _position, _length, line in parsed_nodes["lines"]:
        if line.get("k") != "node" or not isinstance(line.get("row"), Mapping):
            unknown.append(copy.deepcopy(line))
            continue
        file = canonical_carto_file(line.get("file"))
        receipt = copy.deepcopy(line["row"])
        receipts.setdefault(file, []).append(receipt)
        tallies[file] = tally_bulk_receipt(tallies.get(file), receipt)
        global_receipts.append(receipt)
        bulk_keys.add(file)
    for file in list(receipts) + [key[1] for key in rows if key[0] == "files" and key[1] in bulk_keys and key[1] not in receipts]:
        key = ("files", file)
        row = rows.get(key, _bulk_base_row(file))
        tally = tallies.get(file, {"count": 0, "satisfied": 0, "changed": False, "rejected_error": None})
        count = reflected.get(key, 0)
        if tally["count"] > count or "nodes_satisfied" not in row:
            row = apply_bulk_receipts(row, tally, reflected=count, node_outcomes=receipts.get(file, []))
        else:
            row = {**row, "node_outcomes": receipts.get(file, [])}
        rows[key] = row
    sections: dict[str, list] = {}
    for (section, _file), row in rows.items():
        sections.setdefault(section, []).append(row)
    return {"rows": sections, "items": items, "values": values, "node_outcomes": global_receipts,
            "rows_on_disk": rows_on_disk,
            "torn_tail_bytes": parsed["torn_tail_bytes"] + parsed_nodes["torn_tail_bytes"],
            "unreadable_lines": parsed["unreadable_lines"] + parsed_nodes["unreadable_lines"],
            "unrecognized_lines": unknown, "in_progress": in_progress}


def _restore_carto_value(record, key, value):
    if isinstance(key, tuple):
        path = key
    elif isinstance(key, str) and key.startswith("result_summary."):
        path = ("result_summary", key[len("result_summary."):])
    elif key in record:
        path = (key,)
    else:
        path = ("result_summary", key)
    if len(path) == 2 and path[0] == "result_summary" and path[1] in _SUMMARY_ONLY_KEYS:
        # CF-168: moving a record-only aggregate/preview does not turn that
        # presentation value into a new key of the old complete detail.
        return
    target = record
    for part in path[:-1]:
        if not isinstance(target.get(part), dict):
            target[part] = {}
        target = target[part]
    target[path[-1]] = copy.deepcopy(value)


# @beacon[
#   id=carto-record@stitch-detail,
#   role=stitch_carto_job_detail(record, folded),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=Rebuilds FILE rows and FOLDER or bulk aggregates including empty selections and receipt-free files, restoring projections receipts run lists moved values and Q6 terminal text from complete error items; record-only previews and recognized derivation markers stay out of complete detail.,
# ]
def stitch_carto_job_detail(record: Mapping[str, Any], folded: Mapping[str, Any]) -> dict[str, Any]:
    """Restore complete evidence without changing any old result's values.

    Rule for every kind (CF-168): new record-only aggregates and bounded
    previews are summary-view presentation, not additions to complete detail;
    original FILE-row/batch keys and unknown keys keep their original values.
    Single-FILE result_summary is its one folded row, overlaid additively.

    CF-159: FOLDER nexusignore_sources and missing_paths live completely in
    evidence item/set lines and in stitched result_summary; the record holds
    at most count-bounded previews, never their complete growing lists.
    An empty FOLDER selection still has the batch's empty lists in detail,
    without storing those tail lists in its small record.
    Bulk detail likewise keeps an empty global node_outcomes without requiring
    an artificial node stream for a file whose candidates were all in sync.
    A FILE gesture routed to a directory remains an aggregate; an actual FILE
    keeps its original row fields, not the accumulator's classification keys.
    Q6: a recognized error_derivation rebuilds the base terminal string with
    Python str on its complete run list, then disappears from complete detail.
    Unknown recipes or missing source evidence keep the readable bounded error
    and marker, with a warning; legacy and /1 values are never reinterpreted.
    """
    result = copy.deepcopy(dict(record))
    result.pop("_pending_evidence", None)
    if result.get("record_layout") != CARTO_JOB_RECORD_LAYOUT_V2:
        result.update(carto_job_outcome(result))
        return result
    rows = folded.get("rows") or {}
    files = rows.get("files") or []
    summary = result.get("result_summary")
    summary = summary if isinstance(summary, dict) else {}
    if (files or rows.get("phase8") or (result.get("type") == "CARTO_BULK_APPLY" and "successful" in summary) or (result.get("type") == "CARTO_REFRESH"
            and "successful" in summary and (str(result.get("mode") or "").lower() == "folder" or "file" not in summary))):
        from .beacon_refresh_support import aggregate_beacon_outcomes, beacon_job_outcome
        items, values = folded.get("items") or {}, folded.get("values") or {}
        phase8 = copy.deepcopy(values.get("phase8_envelope"))
        if rows.get("phase8"):
            phase8 = dict(phase8 or {})
            phase8["files"] = copy.deepcopy(rows["phase8"])
        mode = str(result.get("mode") or "").lower()
        single_file = (result.get("type") == "CARTO_REFRESH" and mode == "file" and len(files) == 1
                       and "file" in summary)
        if single_file:
            rebuilt = copy.deepcopy(files[0])
        else:
            rebuilt = beacon_job_outcome(aggregate_beacon_outcomes(files,
                warnings=items.get("run_warnings", ()), events=items.get("run_events", ())),
                infrastructure_error=summary.get("infrastructure_error"),
                cancelled=bool(summary.get("cancelled")), phase8=phase8, owns_aggregate=True)
        complete = {key: value for key, value in summary.items()
                    if key not in ((_INLINE_SUMMARY_KEYS | {"infrastructure_error"}) if single_file else
                                   _SUMMARY_ONLY_KEYS | {"files_changed", "errors", "refresh_errors"})}
        # Lists are rebuilt from complete evidence, never a head preview. Old
        # non-list values remain exactly what the producing component saved.
        for key, value in rebuilt.items():
            if key not in complete or isinstance(value, list) or key == "phase8" or single_file:
                complete[key] = value
        # A present-but-empty source list emits no item lines. Presence is
        # carried by its bounded head preview; absence must stay absence.
        for key in ("errors", "refresh_errors", "present_paths", "nexusignore_sources", "missing_paths"):
            if isinstance(summary.get(key), list):
                complete[key] = copy.deepcopy(items.get(key, []))
        if result.get("type") == "CARTO_BULK_APPLY" or (result.get("evidence") or {}).get("nodes") is not None:
            complete["node_outcomes"] = copy.deepcopy(folded.get("node_outcomes") or [])
        result["result_summary"] = complete
    for key, values in (folded.get("items") or {}).items():
        if key not in {"run_warnings", "run_events"}:
            result.setdefault("result_summary", {})[key] = copy.deepcopy(values)
    for key, value in (folded.get("values") or {}).items():
        if key != "phase8_envelope":
            _restore_carto_value(result, key, value)
    if "error_derivation" in result:
        recipe = _carto_terminal_text_recipe(result)
        if recipe is not None and recipe["source"] in (folded.get("items") or {}):
            result["error"] = recipe["prefix"] + str(result["result_summary"][recipe["source"]]) + recipe["suffix"]
            result.pop("error_derivation")
        else:
            _LOG.warning("Cartographer terminal-text derivation unavailable or unknown; bounded error and marker retained")
    expected = ((record.get("evidence") or {}).get("files") or {}).get("rows")
    if expected is not None and expected != folded.get("rows_on_disk"):
        result["rows_on_disk"] = folded.get("rows_on_disk", 0)
    for key in ("torn_tail_bytes", "unreadable_lines", "unrecognized_lines"):
        if folded.get(key):
            result[key] = copy.deepcopy(folded[key])
    result.update(carto_job_outcome(record))
    return result


def _clip_detail(value):
    if isinstance(value, (Mapping, list, tuple)):
        value = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return value[:1999] + "…" if isinstance(value, str) and len(value) > 2000 else value


def _slim_detail_row(row, *, section="files"):
    """Presentation only: the complete row remains recoverable by its offsets."""
    def entries(values, fields):
        return [{key: _clip_detail(value.get(key)) for key in fields if key in value}
                if isinstance(value, Mapping) else {"message": _clip_detail(str(value))}
                for value in values or ()]
    return {"sec": section, "file": _clip_detail(row.get("file")), "status": _clip_detail(row.get("status")),
            "changed": _clip_detail(row.get("changed", False)), "error": _clip_detail(row.get("error")),
            "error_kind": _clip_detail(row.get("error_kind")),
            "warnings": entries(row.get("warnings"), ("type", "message", "error", "file", "source_path")),
            "findings": entries(row.get("findings"), ("code", "severity", "message", "line", "finding_id")),
            # Events have no closed field vocabulary. A wide event is one
            # clipped JSON text (the same 2,000-character presentation rule),
            # not an unpageable object or a dropped item. Small objects retain
            # their shape; complete event data stays in file/detail evidence.
            "events": [_slim_detail_event(item) for item in (
                           list(row.get("events") or ()) + list(row.get("omissions") or ()) + list(row.get("relocations") or ()))],
            "node_errors": [], "errors": []}


def _slim_detail_event(item):
    if isinstance(item, Mapping):
        result = {key: _clip_detail(value) for key, value in item.items()}
        if len(json.dumps(result, ensure_ascii=True, separators=(",", ":"))) <= 2000:
            return result
    return _clip_detail(item)


def _is_problem_row(row):
    return (str(row.get("status") or "").lower() in FAILED_FILE_STATUSES | {"skipped"} or
            any(row.get(key) for key in ("warnings", "findings", "events", "omissions", "relocations", "node_errors", "errors")) or
            any(isinstance(receipt, Mapping) and (receipt.get("success") is False or receipt.get("error"))
                for receipt in row.get("node_outcomes") or ()))


class _JsonSpan:
    """A JSON value's byte location, never its contents, retained by the index."""

    def __init__(self, path, start, raw):
        self.path, self.start = Path(path), start
        text = raw.decode("utf-8")
        self.length = len(text)
        self.byte_length = len(raw)
        self.wide_positions, self.wide_extra = [], []
        extra = 0
        if not raw.isascii():
            for index, char in enumerate(text):
                size = len(char.encode("utf-8"))
                if size > 1:
                    extra += size - 1
                    self.wide_positions.append(index + 1)
                    self.wide_extra.append(extra)

    def _byte(self, char_offset):
        index = bisect.bisect_right(self.wide_positions, char_offset)
        return char_offset + (self.wide_extra[index - 1] if index else 0)

    def read(self, start, count):
        stop = min(self.length, start + count)
        first, last = self._byte(start), self._byte(stop)
        with self.path.open("rb") as handle:
            handle.seek(self.start + first)
            return handle.read(last - first).decode("utf-8")

    def byte_count(self, start, count):
        return self._byte(min(self.length, start + count)) - self._byte(start)


class _JsonParts:
    """Concatenated JSON syntax and offset references; no complete row cache."""

    def __init__(self, parts):
        self.parts, self.ends = [], []
        self.length = 0
        self.byte_length = 0
        self.entries = None
        for part in parts:
            size = len(part) if isinstance(part, str) else part.length
            if size:
                self.parts.append(part)
                self.length += size
                self.byte_length += len(part.encode("utf-8")) if isinstance(part, str) else part.byte_length
                self.ends.append(self.length)

    def read(self, start, count):
        stop, chunks = min(self.length, start + count), []
        index = bisect.bisect_right(self.ends, start)
        while index < len(self.parts) and start < stop:
            begin = self.ends[index - 1] if index else 0
            part = self.parts[index]
            take = min(stop, self.ends[index]) - start
            offset = start - begin
            chunks.append(part[offset:offset + take] if isinstance(part, str) else part.read(offset, take))
            start += take
            index += 1
        return "".join(chunks)

    def byte_count(self, start, count):
        stop, total = min(self.length, start + count), 0
        index = bisect.bisect_right(self.ends, start)
        while index < len(self.parts) and start < stop:
            begin = self.ends[index - 1] if index else 0
            part = self.parts[index]
            take, offset = min(stop, self.ends[index]) - start, start - begin
            total += (len(part[offset:offset + take].encode("utf-8")) if isinstance(part, str)
                      else part.byte_count(offset, take))
            start += take
            index += 1
        return total


class _JsonWindow:
    """A subrange (for array append/delta) without reading or storing its text."""

    def __init__(self, source, start, length):
        self.source, self.start, self.length = source, start, max(0, length)
        self.byte_length = source.byte_count(start, self.length)

    def read(self, start, count):
        return self.source.read(self.start + start, min(count, self.length - start))

    def byte_count(self, start, count):
        return self.source.byte_count(self.start + start, min(count, self.length - start))


def _json_literal(value):
    return _JsonParts([json.dumps(value, ensure_ascii=True, separators=(",", ":"))])


def _json_object(values):
    parts = ["{"]
    for index, (key, value) in enumerate(values.items()):
        if index:
            parts.append(",")
        parts.extend((json.dumps(key, ensure_ascii=True) + ":", value))
    return _JsonParts([*parts, "}"])


def _json_array(values):
    values = list(values)
    parts = ["["]
    for index, value in enumerate(values):
        if index:
            parts.append(",")
        parts.append(value)
    result = _JsonParts([*parts, "]"])
    result.entries = values
    return result


def _json_extend(previous, appended):
    if getattr(previous, "entries", None) is not None and getattr(appended, "entries", None) is not None:
        return _json_array([*previous.entries, *appended.entries])
    left = _JsonWindow(previous, 1, previous.length - 2)
    right = _JsonWindow(appended, 1, appended.length - 2)
    return _JsonParts(["[", left, "," if left.length and right.length else "", right, "]"])


def _json_member_spans(raw, start=0, *, array=False):
    """Locate an already-decoded object's members; this is geometry, not validation.

    The one JSON decode has already accepted the complete line. This lexical
    pass records byte ranges during that same read, so later pages can seek
    directly to a value rather than decoding a giant row again per slice.
    """
    def skip(index):
        while index < len(raw) and raw[index] in b" \r\n\t":
            index += 1
        return index

    def string_end(index):
        index += 1
        while index < len(raw):
            if raw[index] == 92:
                index += 2
            elif raw[index] == 34:
                return index + 1
            else:
                index += 1
        return index

    def value_end(index):
        if raw[index] == 34:
            return string_end(index)
        if raw[index] not in (123, 91):
            end = index
            while end < len(raw) and raw[end] not in b",}] \r\n\t":
                end += 1
            return end
        depth = 1
        index += 1
        while index < len(raw) and depth:
            if raw[index] == 34:
                index = string_end(index)
                continue
            if raw[index] in (123, 91):
                depth += 1
            elif raw[index] in (125, 93):
                depth -= 1
            index += 1
        return index

    result = [] if array else OrderedDict()
    index = skip(start) + 1
    while (index := skip(index)) < len(raw) and raw[index] != (93 if array else 125):
        if array:
            first = index
        else:
            end_key = string_end(index)
            key = json.loads(raw[index:end_key])
            first = skip(skip(end_key) + 1)
        end = value_end(first)
        if array:
            result.append((first, end))
        else:
            result[key] = (first, end)
        index = skip(end)
        if raw[index] == 44:
            index += 1
    return result


# @beacon[
#   id=carto-record@evidence-index,
#   role=_CartoEvidenceIndex(path),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=The four-job detail cache retains byte-span row plans, stitched list parts and slim problems; growing streams extend from the committed newline and pages seek their own bytes. Only B1 terminal pending lines may carry memory-backed complete evidence until restart.,
# ]
class _CartoEvidenceIndex:
    """Four-job read cache: line positions, stitched lists and slim problems.

    No ordinary complete file row or receipt is retained. B1 terminal lines
    that could not append are the explicit memory-backed exception. Row parts are only
    the sources of stitched aggregate lists; full row reads follow offsets.
    Growing evidence is consumed from the last newline, including a prior
    torn tail, never from byte zero merely because progress changed.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.signatures = {}
        self.offsets = {"files": 0, "nodes": 0}
        self.row_lines = OrderedDict()
        self.row_refs = OrderedDict()
        self.latest = {}
        self.reflected = {}
        self.bulk_keys = set()
        self.parts = OrderedDict()
        self.slim = OrderedDict()
        self.items, self.values = {}, {}
        self.item_refs, self.value_refs = {}, {}
        self.node_refs = []
        self.receipt_refs = {}
        self.node_lines, self.receipts, self.tallies = [], OrderedDict(), {}
        self.unknown = []
        self.unreadable = {"files": 0, "nodes": 0}
        self.torn = {"files": 0, "nodes": 0}
        self.line_counts = {"files": 0, "nodes": 0}
        self.rows_on_disk = 0
        self.version = None
        self.pager = None
        self.problem_rows = None

    @staticmethod
    def signature(path):
        try:
            stat = Path(path).stat()
            return (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            return None

    def sidepath(self, section):
        return self.path.with_suffix("") / ("nodes.jsonl" if section == "nodes" else "files.jsonl")

    def extend(self, signatures):
        for section in ("files", "nodes"):
            signature = signatures[section]
            if signature is None or self.signatures.get(section) == signature:
                continue
            start = self.offsets[section]
            with self.sidepath(section).open("rb") as handle:
                handle.seek(start)
                raw = handle.read()
            parsed = parse_carto_evidence_lines(raw)
            self.torn[section] = parsed["torn_tail_bytes"]
            self.unreadable[section] += parsed["unreadable_lines"]
            if parsed["unreadable_lines"]:
                _LOG.warning("Skipped %d unreadable complete evidence lines in %s", parsed["unreadable_lines"], self.sidepath(section))
            base_line = self.line_counts[section]
            for index, position, length, line in parsed["lines"]:
                self._accept(section, base_line + index, start + position, length, line,
                             raw[position:position + length])
            committed = len(raw) - parsed["torn_tail_bytes"]
            self.offsets[section] += committed
            self.line_counts[section] += raw[:committed].count(b"\n")
        self.signatures = signatures
        self.pager = None
        self.problem_rows = None

    def _accept(self, stream, index, position, length, line, raw):
        kind = line.get("k")
        geometry = _json_member_spans(raw)
        def reference(span):
            first, last = span
            if position is None:
                # B1: an unappendable terminal line lives only in the proof.
                # Its encoded pieces are the named pending-evidence exception,
                # not a cache of ordinary completed rows.
                return _json_literal(json.loads(raw[first:last]))
            return _JsonSpan(self.sidepath(stream), position + first, raw[first:last])
        if stream == "nodes" and kind == "node" and isinstance(line.get("row"), Mapping):
            file = canonical_carto_file(line.get("file"))
            self.node_lines.append((file, position, length))
            self.node_refs.append((file, reference(geometry["row"])))
            self.receipt_refs.setdefault(file, []).append(self.node_refs[-1][1])
            self.receipts.setdefault(file, []).append((position, length))
            self.tallies[file] = tally_bulk_receipt(self.tallies.get(file), line["row"])
            self.bulk_keys.add(file)
            if line["row"].get("success") is False or line["row"].get("error"):
                key = ("files", file)
                # Only slim receipt errors are cached, never whole receipts.
                if key not in self.slim:
                    self.slim[key] = _slim_detail_row(_bulk_base_row(file))
                self.slim[key]["node_errors"].append({key: _clip_detail(value) for key, value in line["row"].items()
                                                       if key in {"node_id", "error", "noop_reason", "success"}})
            return
        if stream == "files" and kind == "row" and isinstance(line.get("row"), Mapping):
            row = expand_carto_projection(line)
            key = (str(line.get("sec") or "files"), canonical_carto_file(row.get("file")))
            positions = self.row_lines.setdefault(key, [])
            if "delta" not in line:
                positions.clear()
            positions.append((position, length))
            first, _last = geometry["row"]
            members = _json_member_spans(raw, first)
            refs = OrderedDict((name, reference(span)) for name, span in members.items())
            for name in ("files", "findings", "warnings", "events", "files_changed", "errors", "refresh_errors", "present_paths", "node_outcomes"):
                if isinstance(line["row"].get(name), list):
                    refs[name] = _json_array(reference(span) for span in
                        _json_member_spans(raw, members[name][0], array=True))
            if "proj" in line:
                refs["projection"] = _json_object(OrderedDict((name, refs[name]) for name in line["proj"]))
            if "delta" in line:
                if key not in self.row_refs:
                    _LOG.warning("Delta evidence has no earlier row for %s", row.get("file"))
                previous_refs = OrderedDict(self.row_refs.get(key, ((name, _json_literal(value))
                    for name, value in _bulk_base_row(row.get("file")).items())))
                for name in line["delta"].get("unset") or ():
                    previous_refs.pop(name, None)
                previous_refs.update(refs)
                delta_at = geometry["delta"][0]
                delta_geometry = _json_member_spans(raw, delta_at)
                if "extend" in delta_geometry:
                    extended = _json_member_spans(raw, delta_geometry["extend"][0])
                    for name, span in extended.items():
                        appended = _json_array(reference(item) for item in _json_member_spans(raw, span[0], array=True))
                        previous_refs[name] = _json_extend(previous_refs.get(name, _json_array([])), appended)
                refs = previous_refs
            self.row_refs[key] = refs
            self.latest[key] = index
            self.reflected[key] = _count(line.get("nodes"))
            if "nodes" in line:
                self.bulk_keys.add(key[1])
            part_keys = ("file", "status", "changed", "error", "error_kind", "warnings", "events", "omissions", "relocations", "findings")
            if "delta" in line:
                previous = self.parts.get(key, _bulk_base_row(row.get("file")))
                part = overlay_carto_row(previous, {k: v for k, v in row.items() if k in part_keys}, line["delta"])
                old_slim = self.slim.get(key, _slim_detail_row(previous, section=key[0]))
                new_slim = _slim_detail_row(row, section=key[0])
                for name in ("file", "status", "changed", "error", "error_kind", "warnings", "findings"):
                    if name in row:
                        old_slim[name] = new_slim[name]
                    elif name in (line["delta"].get("unset") or ()):
                        old_slim[name] = [] if name in {"warnings", "findings"} else None
                for name, values in (line["delta"].get("extend") or {}).items():
                    if name in {"warnings", "findings"}:
                        old_slim[name].extend(_slim_detail_row({name: values})[name])
                old_slim["events"] = _slim_detail_row(part)["events"]
                slim = old_slim
            else:
                part = {k: copy.deepcopy(v) for k, v in row.items() if k in part_keys}
                slim = _slim_detail_row(row, section=key[0])
                if key in self.slim:
                    slim["node_errors"] = self.slim[key]["node_errors"]
            # The complete aggregate preview is at most 200 findings. Details
            # keeps only their slim presentation, not their complete anatomy.
            part["findings"] = list(part.get("findings") or ())[:200]
            self.parts[key], self.slim[key] = part, slim
            self.rows_on_disk += 1
            return
        if stream == "files" and kind == "item" and "key" in line:
            self.items.setdefault(line["key"], []).append(copy.deepcopy(line.get("v")))
            self.item_refs.setdefault(line["key"], []).append(reference(geometry["v"]))
            return
        if stream == "files" and kind == "set" and "key" in line:
            key = tuple(line["key"]) if isinstance(line["key"], list) else line["key"]
            self.values[key] = copy.deepcopy(line.get("v"))
            self.value_refs[key] = reference(geometry["v"])
            return
        self.unknown.append(copy.deepcopy(line))
        _LOG.warning("Unrecognized evidence line in %s: %r", self.sidepath(stream), kind)

    def row(self, key, record):
        """Hydrate a row only for callers explicitly requesting that complete row."""
        source = self.row_source(key, record)
        return json.loads(source.read(0, source.length))

    def row_values(self, key, record, *, defaults=False):
        """Build only an offset-backed JSON plan, including projection/receipt effects."""
        refs = OrderedDict(self.row_refs.get(key, ((name, _json_literal(value))
            for name, value in _bulk_base_row(key[1]).items())))
        part = self.parts.get(key, _bulk_base_row(key[1]))
        inflight = record.get("in_flight")
        if (isinstance(inflight, Mapping) and key == ("files", canonical_carto_file(inflight.get("file")))
                and self.latest.get(key, -1) < _count(inflight.get("base_lines"))
                and str(record.get("status") or "").lower() in CARTO_TERMINAL_STATUSES):
            refs = OrderedDict((name, _json_literal(value)) for name, value in inflight.items() if name != "base_lines")
            part = inflight
        if key[0] == "files" and (key[1] in self.bulk_keys or record.get("type") == "CARTO_BULK_APPLY"):
            tally = self.tallies.get(key[1], {"count": 0, "satisfied": 0, "changed": False, "rejected_error": None})
            reflected = self.reflected.get(key, 0)
            if tally["count"] > reflected or "nodes_satisfied" not in refs:
                if tally["count"] != reflected:
                    refs["final_observation"] = _json_literal(False)
                refs["node_outcomes"] = _json_array(self.receipt_refs.get(key[1], ()))
                refs["nodes_satisfied"] = _json_literal(tally["satisfied"])
                refs["changed"] = _json_literal(bool(part.get("changed") or tally["changed"]))
                if tally["rejected_error"]:
                    for name, value in (("status", "failed"), ("success", False), ("error", tally["rejected_error"])):
                        refs[name] = _json_literal(value)
            else:
                refs["node_outcomes"] = _json_array(self.receipt_refs.get(key[1], ()))
        if defaults:
            for name, value in (("status", "failed" if part.get("error") else "completed"), ("changed", False),
                                ("findings", []), ("check_target", {"verb": "check", "params": {
                                    "file_path": part.get("file", key[1]), "min_severity": "info"}})):
                refs.setdefault(name, _json_literal(value))
        phase_key = ("phase8", key[1])
        if key[0] == "files" and phase_key in self.row_refs:
            phase_refs = self.row_refs[phase_key]
            refs["findings"] = phase_refs.get("findings", _json_literal([]))
            refs["refresh_outcome"] = self.row_source(phase_key, record)
        return refs

    def row_source(self, key, record, *, defaults=False):
        return _json_object(self.row_values(key, record, defaults=defaults))

    def row_keys(self, record):
        keys = list(self.row_lines)
        inflight = record.get("in_flight")
        if isinstance(inflight, Mapping) and str(record.get("status") or "").lower() in CARTO_TERMINAL_STATUSES:
            key = ("files", canonical_carto_file(inflight.get("file")))
            if key not in keys:
                keys.append(key)
        keys.extend(("files", file) for file in self.receipts if ("files", file) not in keys)
        return keys

    def folded_parts(self, record):
        sections = {}
        for key in self.row_keys(record):
            part = dict(self.parts.get(key, _bulk_base_row(key[1])))
            inflight = record.get("in_flight")
            if (isinstance(inflight, Mapping) and key == ("files", canonical_carto_file(inflight.get("file")))
                    and self.latest.get(key, -1) < _count(inflight.get("base_lines"))
                    and str(record.get("status") or "").lower() in CARTO_TERMINAL_STATUSES):
                part = {name: value for name, value in inflight.items() if name != "base_lines"}
            if key[1] in self.bulk_keys:
                tally = self.tallies.get(key[1], {"count": 0, "satisfied": 0, "changed": False, "rejected_error": None})
                part = apply_bulk_receipts(part, tally, reflected=self.reflected.get(key, 0), node_outcomes=None)
            sections.setdefault(key[0], []).append(part)
        return {"rows": sections, "items": self.items, "values": self.values, "node_outcomes": [],
                "rows_on_disk": self.rows_on_disk, "torn_tail_bytes": sum(self.torn.values()),
                "unreadable_lines": sum(self.unreadable.values()), "unrecognized_lines": self.unknown}


_detail_cache: OrderedDict[str, _CartoEvidenceIndex] = OrderedDict()
_detail_cache_lock = threading.RLock()


def read_carto_job_evidence_index(path, record):
    """Extend one job's index, including B1's terminal-only pending-line overlay."""
    path = Path(path)
    name = str(path)
    with _detail_cache_lock:
        index = _detail_cache.get(name)
        if not isinstance(index, _CartoEvidenceIndex):
            index = None
        probe = index or _CartoEvidenceIndex(path)
        signatures = {section: probe.signature(probe.sidepath(section)) for section in ("files", "nodes")}
        pending = record.get("_pending_evidence") or {}
        pending_signature = _version(pending)
        if index is not None and (getattr(index, "pending_signature", _version({})) != pending_signature or
                (pending and index.signatures != signatures)):
            index = None
        if index is not None and any(
            old is not None and (signatures[section] is None or signatures[section][1] < old[1] or
                                 (signatures[section][1] == old[1] and signatures[section] != old))
            for section, old in index.signatures.items()):
            index = None
        index = index or _CartoEvidenceIndex(path)
        record_signature = _CartoEvidenceIndex.signature(path)
        version = hashlib.sha256(json.dumps([record_signature, signatures, path.with_suffix("").is_dir(), pending_signature],
                                            sort_keys=True).encode()).hexdigest()[:16]
        if index.version != version:
            index.extend(signatures)
            if pending and not getattr(index, "pending_applied", False):
                durable_rows = index.rows_on_disk
                for stream in ("files", "nodes"):
                    for ordinal, text in enumerate(pending.get(stream) or ()):
                        raw = text.encode("utf-8")
                        index._accept(stream, index.line_counts[stream] + ordinal, None, len(raw), json.loads(text), raw)
                index.rows_on_disk = durable_rows
                index.pending_applied = True
            index.pending_signature = pending_signature
            index.version = version
            index.pager = None
            index.problem_rows = None
        _detail_cache[name] = index
        _detail_cache.move_to_end(name)
        while len(_detail_cache) > 4:
            _detail_cache.popitem(last=False)
        return index


def _encoded_unit(source):
    from .carto_job_paging import EncodedJsonUnit
    return EncodedJsonUnit(source.length, source.read, total_bytes=source.byte_length)


class _IndexedRows:
    """A list of row identities plus an offset-backed JSON plan per row."""

    def __init__(self, index, keys, record, *, defaults=False):
        self.index, self.keys, self.record, self.defaults = index, keys, record, defaults

    def count(self):
        return len(self.keys)

    def encoded(self, ordinal):
        return _encoded_unit(self.index.row_source(self.keys[ordinal], self.record, defaults=self.defaults))

    def slice(self, start, stop):
        return [self.index.row(key, self.record) for key in self.keys[start:stop]]


class _IndexedValues:
    """Run-level items or receipts backed by their original value spans."""

    def __init__(self, values):
        self.values = values

    def count(self):
        return len(self.values)

    def encoded(self, ordinal):
        return _encoded_unit(self.values[ordinal])

    def slice(self, start, stop):
        return [json.loads(value.read(0, value.length)) for value in self.values[start:stop]]


def _public_job_record(record, path, job_id):
    """Preserve the head's unknown keys beside the existing per-job aliases."""
    result = dict(record)
    result.pop("_pending_evidence", None)
    result.update(job_id=record.get("id") or job_id, kind=record.get("type") or record.get("kind"),
                  detached=bool(record.get("detached", False)), carto_job_file=str(path),
                  log_file=record.get("logs_path"), heartbeat=None, pid=record.get("pid"),
                  mode=record.get("mode"), root_uuid=record.get("root_uuid"),
                  created_at=record.get("created_at"), updated_at=record.get("updated_at"),
                  progress=record.get("progress"), result_summary=record.get("result_summary"),
                  error=record.get("error"), warnings=record.get("warnings", []),
                  warning_count=record.get("warning_count", len(record.get("warnings") or [])),
                  cache_refresh_required=record.get("cache_refresh_required", record.get("type") == "CARTO_REFRESH"))
    result.update(carto_job_outcome(record))
    result["success"] = True
    return result


def _overlay_record(record, overlay, *, complete=False):
    """Only a requested job's live proof may replace its saved result summary."""
    if overlay is None:
        return dict(record)
    from .carto_job_runtime import _thaw_job_data
    from .config import CARTO_RESULT_SUMMARY_TAIL_KEYS
    values = {key: value for key, value in overlay.items() if key != "result_summary"}
    result = {**record, **_thaw_job_data(values)}
    if "progress" in values and isinstance(record.get("progress"), Mapping):
        result["progress"] = {**record["progress"], **(result.get("progress") or {})}
    if "result_summary" in overlay:
        summary = overlay["result_summary"]
        if not complete and isinstance(summary, Mapping):
            summary = {key: value for key, value in summary.items() if key not in CARTO_RESULT_SUMMARY_TAIL_KEYS}
            previous = record.get("result_summary")
            result["result_summary"] = {**(previous if isinstance(previous, Mapping) else {}), **_thaw_job_data(summary)}
        else:
            # JR1: the full proof replaces, not merges, so stale disk-only
            # keys cannot leak and a failed terminal write cannot lose rows.
            result["result_summary"] = _thaw_job_data(summary)
    return result


def _frame_json(value, *, path=(), paged=frozenset(), overrides=None):
    if path in paged:
        return _json_array([])
    if overrides and path in overrides:
        return overrides[path]
    if isinstance(value, Mapping):
        return _json_object(OrderedDict((key, _frame_json(item, path=(*path, key), paged=paged,
                                                         overrides=overrides)) for key, item in value.items()))
    return _json_literal(value)


def _prepare_indexed_detail(index, record, path, job_id):
    """Compile one version's frame/lists from metadata, never retained full rows."""
    from .carto_job_paging import CartoJobDetailPager, paged_paths_in_document_order
    folded = index.folded_parts(record)
    detail = stitch_carto_job_detail(record, folded)
    detail = _public_job_record(detail, path, job_id)
    detail.update(carto_job_outcome(record))
    if _evidence_is_missing(index, record):
        detail["evidence_missing"] = True
    keys = index.row_keys(record)
    primary = [key for key in keys if key[0] == "files"]
    phase8 = [key for key in keys if key[0] == "phase8"]
    is_file = (record.get("type") == "CARTO_REFRESH" and str(record.get("mode") or "").lower() == "file"
               and len(primary) == 1 and "file" in (record.get("result_summary") or {}))
    overrides = {}
    sources = {}
    if is_file:
        values = index.row_values(primary[0], record)
        summary = OrderedDict((name, [] if getattr(source, "entries", None) is not None else None)
                              for name, source in values.items())
        for name, value in (detail.get("result_summary") or {}).items():
            if name not in summary and name not in _SUMMARY_ONLY_KEYS | {"errors", "refresh_errors", "files_changed"}:
                summary[name] = value
        detail["result_summary"] = summary
        for name, source in values.items():
            field_path = ("result_summary", name)
            overrides[field_path] = source
            if name in {"files", "findings", "warnings", "events", "files_changed", "errors", "refresh_errors", "present_paths", "node_outcomes"} and getattr(source, "entries", None) is not None:
                sources[field_path] = _IndexedValues(source.entries)
    else:
        if primary and isinstance((detail.get("result_summary") or {}).get("files"), list):
            sources[("result_summary", "files")] = _IndexedRows(index, primary, record, defaults=True)
        phase = (detail.get("result_summary") or {}).get("phase8")
        if isinstance(phase, Mapping) and isinstance(phase.get("files"), list):
            sources[("result_summary", "phase8", "files")] = _IndexedRows(index, phase8, record)
    if (record.get("evidence") or {}).get("nodes") is not None:
        sources[("result_summary", "node_outcomes")] = _IndexedValues([value for _file, value in index.node_refs])
    for name, values in index.item_refs.items():
        if name in {"errors", "refresh_errors", "present_paths"}:
            sources[("result_summary", name)] = _IndexedValues(values)
    for name, source in index.value_refs.items():
        if name == "phase8_envelope":
            continue
        if isinstance(name, tuple):
            value_path = name
        elif isinstance(name, str) and name.startswith("result_summary."):
            value_path = ("result_summary", name[len("result_summary."):])
        else:
            value_path = (name,) if name in record else ("result_summary", name)
        overrides[value_path] = source
    paths = paged_paths_in_document_order(detail)
    from .carto_job_paging import get_path
    lists = OrderedDict((list_path, sources.get(list_path, get_path(detail, list_path))) for list_path in paths)
    frame = _frame_json(detail, paged=frozenset(paths), overrides=overrides)
    index.pager = CartoJobDetailPager(frame=_encoded_unit(frame), lists=lists, evidence_version=getattr(index, "page_version", index.version),
                                    budget=_reply_budget())
    return index.pager


def _reply_budget():
    from .job_status_view import JOB_REPLY_MAX_BYTES
    return JOB_REPLY_MAX_BYTES


def _byte_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _page_budget(options, *, budget=None):
    """Reserve only the warning envelope's bytes; evidence still pages whole."""
    budget = _reply_budget() if budget is None else budget
    warnings = options.get("parameter_warnings")
    return budget - (_byte_size({"parameter_warnings": warnings}) - 1 if warnings else 0)


def _evidence_is_missing(index, record):
    """An empty, never-written stream is not lost evidence; a missing bundle is."""
    if not index.path.with_suffix("").is_dir():
        return True
    for name, declared in (record.get("evidence") or {}).items():
        if isinstance(declared, Mapping) and any(declared.get(key) for key in ("rows", "lines", "bytes")):
            if index.signatures.get(name) is None:
                return True
    return False


def _summary_reply(summary, options, *, budget=None):
    from .carto_job_paging import preview_carto_job_summary, page_carto_job_detail
    budget = budget or _reply_budget()
    result = preview_carto_job_summary(summary, budget=budget)
    warnings = options.get("parameter_warnings")
    if warnings:
        result["parameter_warnings"] = warnings
        result = preview_carto_job_summary(result, budget=budget)
    # A protected identifier alone can exceed a page. It is never clipped or
    # refused: preserve it as a sliced frame, under the same reply budget.
    if _byte_size(result) > budget or options.get("offset") or options.get("slice_offset"):
        result = page_carto_job_detail(summary, offset=options.get("offset", 0),
            slice_offset=options.get("slice_offset", 0), budget=_page_budget(options, budget=budget))
        if warnings:
            result["parameter_warnings"] = warnings
    return result


def present_memory_job(job, options):
    """Keep generic-job redaction and page/preview its existing complete reply."""
    from .carto_job_paging import page_carto_job_detail
    reply = {"success": True, **job}
    if options.get("selection") == "detail":
        result = page_carto_job_detail(reply, offset=options.get("offset", 0),
            slice_offset=options.get("slice_offset", 0), files_limit=options.get("files_limit", 100),
            budget=_page_budget(options), evidence_version=hashlib.sha256(json.dumps(reply, default=str).encode()).hexdigest()[:16])
        if options.get("parameter_warnings"):
            result["parameter_warnings"] = options["parameter_warnings"]
        return result
    if options.get("selection") in {"files", "file"}:
        return _row_view(reply, None, reply, options, complete_detail=reply)
    return _summary_reply(reply, options)


def _merge_view_page(summary, page):
    result = dict(summary)
    for key, value in page.items():
        if key == "result_summary":
            base = dict(result.get(key) or {})
            for name, item in value.items():
                if name == "phase8" and isinstance(item, Mapping):
                    base[name] = {**(base.get(name) or {}), **item}
                else:
                    base[name] = item
            result[key] = base
        else:
            result[key] = value
    return result


def _row_view(summary, index, record, options, *, complete_detail=None):
    from .carto_job_paging import CartoJobDetailPager
    selection, limit = options["selection"], options["files_limit"]
    head = _summary_reply(summary, {**options, "offset": 0, "slice_offset": 0}, budget=_reply_budget() // 2 - 2048)
    lists = OrderedDict()
    if index is not None:
        keys = index.row_keys(record)
        file_result = (record.get("type") == "CARTO_REFRESH" and str(record.get("mode") or "").lower() == "file"
                       and "file" in (record.get("result_summary") or {}))
        if selection == "file":
            wanted = canonical_carto_file(options["file"])
            for section, field in (("files", ("result_summary", "files")), ("phase8", ("result_summary", "phase8", "files"))):
                selected = [key for key in keys if key == (section, wanted)]
                if selected:
                    lists[field] = _IndexedRows(index, selected, record, defaults=section == "files" and not file_result)
            lists[("result_summary", "node_outcomes")] = _IndexedValues(index.receipt_refs.get(wanted, ()))
        else:
            selected = [key for key in keys if key[0] == "files" and (options["files"] == "all" or
                        _is_problem_row(_problem_row(index, key, record)))]
            lists[("result_summary", "files")] = _IndexedRows(index, selected, record, defaults=not file_result)
        version = getattr(index, "page_version", index.version)
    else:
        detail_summary = (complete_detail or {}).get("result_summary") or {}
        rows = detail_summary.get("files")
        rows = rows if isinstance(rows, list) else ([detail_summary] if str(record.get("mode") or "").lower() == "file" else [])
        wanted = canonical_carto_file(options["file"]) if selection == "file" else None
        lists[("result_summary", "files")] = [row for row in rows if (
            canonical_carto_file(row.get("file")) == wanted if selection == "file" else
            options["files"] == "all" or _is_problem_row(row))]
        if selection == "file":
            phase8 = detail_summary.get("phase8") or {}
            if isinstance(phase8, Mapping) and isinstance(phase8.get("files"), list):
                lists[("result_summary", "phase8", "files")] = [row for row in phase8["files"] if canonical_carto_file(row.get("file")) == wanted]
            # Old receipts may omit a physical path: sync_active_file then
            # attributed them to its active row. That row's complete receipt
            # list is the lossless per-file source, not a path-only filter of
            # the global list.
            lists[("result_summary", "node_outcomes")] = [receipt for row in rows
                if canonical_carto_file(row.get("file")) == wanted
                for receipt in row.get("node_outcomes") or ()]
        version = _version(record)
    pager = CartoJobDetailPager(frame={}, lists=lists, budget=_reply_budget() // 2,
                               files_limit=limit, evidence_version=version)
    cursor = options["files_offset"] if selection == "files" else options["offset"]
    page = pager.page(offset=cursor + 1, slice_offset=options["slice_offset"], files_limit=limit)
    meta = page.pop("detail_page")
    next_offset = meta["next_offset"] - 1 if meta["next_offset"] is not None else None
    if selection == "files":
        page["files_page"].update(filter=options["files"], offset=cursor, limit=limit,
            next_offset=next_offset, next_slice_offset=meta["next_slice_offset"], evidence_version=version)
        if meta.get("slice"):
            holder = ((page.get("result_summary") or {}).get("files") or [{}])[0]
            page["files_page"]["slice"] = holder.get("$slice")
            # The sliced unit travels once, at the files-view location named
            # by the contract; do not duplicate its text in a row placeholder.
            page["result_summary"]["files"] = []
        else:
            page.setdefault("result_summary", {}).setdefault("files", [])
    else:
        page.pop("files_page", None)
        page["file_page"] = {**meta, "offset": cursor, "total": meta["total"] - 1, "next_offset": next_offset}
    result = _merge_view_page(head, page)
    if index is not None and _evidence_is_missing(index, record):
        result["evidence_missing"] = True
    if options.get("parameter_warnings"):
        result["parameter_warnings"] = options["parameter_warnings"]
    return result


def _version(value):
    return hashlib.sha256(json.dumps(value, default=str, separators=(",", ":")).encode()).hexdigest()[:16]


# @beacon[
#   id=carto-record@read-job-view,
#   role=read_carto_job_view(path, job_id, overlay, options),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=Hydrates only the requested job: a shared-head summary by default or lossless versioned detail/file pages. Live and failed-write proof evidence survives, normally cleared terminal history stays gone, and receipt-only bulk evidence is still folded.,
# ]
def read_carto_job_view(path, *, job_id, overlay=None, options):
    """Read one job, preserving its own proof and every landed evidence stream."""
    from .carto_job_paging import page_carto_job_detail
    selection = options.get("selection", "summary")
    if path is None:
        return _summary_reply({"success": False, "error": f"Unknown job_id: {job_id}"}, options)
    path = Path(path)
    if overlay is not None and str(overlay.get("path")) != str(path):
        overlay = None
    try:
        head = read_carto_job_record_cached(path)
    except Exception as exc:
        if isinstance(exc, FileNotFoundError) and (overlay is None or (
                str(overlay.get("status") or "").lower() in CARTO_TERMINAL_STATUSES
                and not overlay.get("status_write_error") and not overlay.get("terminal_projection_error"))):
            return _summary_reply({"success": False, "error": f"Unknown job_id: {job_id}"}, options)
        if overlay is None:
            return _summary_reply({"success": False, "error": str(exc)}, options)
        head = {}
    record = _overlay_record(head, overlay)
    summary = _public_job_record(record, path, job_id)
    if selection == "summary":
        return _summary_reply(summary, options)
    if record.get("record_layout") == CARTO_JOB_RECORD_LAYOUT_V2:
        with _detail_cache_lock:
            index = read_carto_job_evidence_index(path, record)
            # The live overlay can change even after a failed record write.
            token = _version([index.version, record])
            if getattr(index, "overlay_version", None) != token:
                index.pager = None
                index.overlay_version = token
            index.page_version = token
            if selection in {"files", "file"}:
                return _row_view(summary, index, record, options)
            pager = index.pager or _prepare_indexed_detail(index, record, path, job_id)
            result = pager.page(offset=options.get("offset", 0), slice_offset=options.get("slice_offset", 0),
                                files_limit=options.get("files_limit", 100), budget=_page_budget(options))
    else:
        try:
            with path.open("r", encoding="utf-8") as handle:
                complete = json.load(handle)
        except Exception as exc:
            if overlay is None:
                return _summary_reply({"success": False, "error": str(exc)}, options)
            complete = {}
        complete = _overlay_record(complete, overlay, complete=True)
        detail = _public_job_record(complete, path, job_id)
        if selection in {"files", "file"}:
            return _row_view(summary, None, complete, options, complete_detail=detail)
        result = page_carto_job_detail(detail, offset=options.get("offset", 0),
            slice_offset=options.get("slice_offset", 0), files_limit=options.get("files_limit", 100),
            evidence_version=_version([_CartoEvidenceIndex.signature(path), complete]), budget=_page_budget(options))
    if options.get("parameter_warnings"):
        result["parameter_warnings"] = options["parameter_warnings"]
    return result


def _problem_row(index, key, record):
    """One slim indexed row, with current receipt/phase8/in-flight effects."""
    row = dict(index.slim.get(key, _slim_detail_row(_bulk_base_row(key[1]), section=key[0])))
    inflight = record.get("in_flight")
    if (isinstance(inflight, Mapping) and key == ("files", canonical_carto_file(inflight.get("file")))
            and index.latest.get(key, -1) < _count(inflight.get("base_lines"))
            and str(record.get("status") or "").lower() in CARTO_TERMINAL_STATUSES):
        row = _slim_detail_row(inflight)
    tally = index.tallies.get(key[1])
    if key[0] == "files" and tally is not None:
        row["changed"] = bool(row.get("changed") or tally["changed"])
        if tally["rejected_error"]:
            row.update(status="failed", error=_clip_detail(tally["rejected_error"]))
        row["node_errors"] = index.slim.get(key, {}).get("node_errors", [])
    phase = index.slim.get(("phase8", key[1]))
    if key[0] == "files" and phase is not None:
        row["findings"] = phase["findings"]
    return row


def _run_problem_row(*, warnings=(), errors=(), events=()):
    row = _slim_detail_row({"file": None, "warnings": warnings, "events": events}, section="run")
    row["status"], row["changed"] = None, None
    row["errors"] = [_clip_detail(value) for value in errors]
    return row


def _legacy_problem_rows(record):
    summary = record.get("result_summary")
    summary = summary if isinstance(summary, Mapping) else {}
    primary = summary.get("files")
    primary = primary if isinstance(primary, list) else ([summary] if str(record.get("mode") or "").lower() == "file" else [])
    rows = []
    for section, values in (("files", primary), ("phase8", (summary.get("phase8") or {}).get("files", ())
                              if isinstance(summary.get("phase8"), Mapping) else ())):
        for value in values or ():
            row = _slim_detail_row(value, section=section)
            row["node_errors"] = [{key: _clip_detail(item) for key, item in receipt.items()
                                   if key in {"node_id", "success", "error", "noop_reason"}}
                                  for receipt in value.get("node_outcomes") or ()
                                  if receipt.get("success") is False or receipt.get("error")]
            rows.append(row)
    errors = list(summary.get("errors") or ())
    for error in summary.get("refresh_errors") or ():
        if error not in errors:
            errors.append(error)
    warnings = []
    if record.get("type") == "CARTO_BULK_APPLY":
        all_warnings = summary.get("warnings") or ()
        count = len(all_warnings) - sum(len(row.get("warnings") or ()) for row in primary)
        warnings = list(all_warnings[:max(0, count)])
    run = _run_problem_row(warnings=warnings, errors=errors)
    return ([run] if _is_problem_row(run) else []) + rows


def _indexed_problem_rows(index, record):
    if index.problem_rows is not None:
        return index.problem_rows
    errors = index.items.get("errors", ())
    events = [event for event in index.items.get("run_events", ()) if not (
        isinstance(event, Mapping) and event.get("type") in {"bulk_error", "file_refresh_error"}
        and event.get("error") in errors and set(event) <= {"type", "error", "message"})]
    run = _run_problem_row(warnings=index.items.get("run_warnings", ()), errors=errors, events=events)
    rows = ([run] if _is_problem_row(run) else []) + [_problem_row(index, key, record) for key in index.row_keys(record)]
    index.problem_rows = rows
    return rows


def _problem_header(record):
    summary = record.get("result_summary") or {}
    result = {key: _clip_detail(record.get(key)) for key in ("status", "error", "error_kind")}
    result.update(carto_job_outcome(record, include_counts=True))
    for key in ("summary_line", "warnings_total"):
        result[key] = _clip_detail(summary.get(key, record.get("warning_count", 0) if key == "warnings_total" else 0))
    result["error"] = _clip_detail(result.get("error"))
    result["error_kind"] = _clip_detail(result.get("error_kind"))
    return result


def _page_problems(rows, record, data, version):
    """Item cursors, not a findings cap: every slim item is reachable once/version."""
    warnings = []
    filter_name = data.get("filter", "problems")
    if filter_name not in {"all", "problems"}:
        warnings.append("Unknown filter value; using problems")
        filter_name = "problems"
    def number(name, default):
        value = data.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (name == "limit" and value == 0):
            warnings.append(f"Unknown {name} value; using {default}")
            return default
        return value
    offset, item_offset, limit = number("offset", 0), number("item_offset", 0), number("limit", 50)
    selected = rows[filter_name] if isinstance(rows, Mapping) else (rows if filter_name == "all" else [row for row in rows if _is_problem_row(row)])
    total = len(selected)
    result = {"success": True, "job": _problem_header(record), "rows": [], "page": {
        "filter": filter_name, "offset": offset, "item_offset": item_offset, "returned": 0,
        "total_rows": total, "next": None, "evidence_version": version}}
    envelope = {"action": "carto_job_detail_result", "request_id": data.get("request_id"), "job_id": data.get("job_id")}
    if data.get("endpoint_id"):
        envelope["target_endpoint_id"] = data["endpoint_id"]
    size = _byte_size(result) + _byte_size(envelope) + 1024
    budget = _reply_budget()
    fields = ("warnings", "findings", "events", "node_errors", "errors")
    cursor, inner = offset, item_offset
    while cursor < total and len(result["rows"]) < limit:
        source = selected[cursor]
        row = {key: ([] if key in fields else value) for key, value in source.items()}
        row_cost = _byte_size(row) + (1 if result["rows"] else 0)
        if size + row_cost > budget:
            break
        sizes = [(name, len(source.get(name) or ())) for name in fields]
        items_total = sum(count for _name, count in sizes)
        accepted = 0
        row_size = row_cost
        skip = inner
        for field, count in sizes:
            if skip >= count:
                skip -= count
                continue
            values = source.get(field) or ()
            for index in range(skip, count):
                item = values[index]
                item_cost = _byte_size(item) + (1 if row[field] else 0)
                if size + row_size + item_cost > budget:
                    break
                row[field].append(item)
                row_size += item_cost
                accepted += 1
            else:
                skip = 0
                continue
            break
        if not accepted and inner < items_total:
            break
        result["rows"].append(row)
        size += row_size
        if inner + accepted < items_total:
            inner += accepted
            break
        cursor += 1
        inner = 0
    result["page"]["returned"] = len(result["rows"])
    if cursor < total:
        result["page"]["next"] = {"offset": cursor, "item_offset": inner}
    if warnings:
        result["parameter_warnings"] = warnings
    return result


def read_carto_problem_page(base, data, *, overlay=None):
    """The four-job shared Details cache; no disk read on an unchanged second click."""
    job_id = data.get("job_id")
    path = find_carto_job_record_path(base, job_id)
    if path is None and overlay is not None and (
            str(overlay.get("status") or "").lower() not in CARTO_TERMINAL_STATUSES
            or overlay.get("status_write_error") or overlay.get("terminal_projection_error")):
        path = Path(overlay["path"])
    if path is None:
        return {"success": False, "error": f"Unknown job_id: {job_id}"[:2000]}
    if overlay is not None and str(overlay.get("path")) != str(path):
        overlay = None
    with _detail_cache_lock:
        try:
            head = read_carto_job_record_cached(path)
        except Exception as exc:
            if overlay is None:
                return {"success": False, "error": str(exc)[:2000]}
            head = {}
        record = _overlay_record(head, overlay)
        if record.get("record_layout") == CARTO_JOB_RECORD_LAYOUT_V2:
            index = read_carto_job_evidence_index(path, record)
            version = _version([index.version, record])
            if getattr(index, "problem_version", None) != version:
                index.problem_rows = None
                index.problem_version = version
            rows = _indexed_problem_rows(index, record)
            if getattr(index, "problem_views_version", None) != version:
                index.problem_views = {"all": rows, "problems": [row for row in rows if _is_problem_row(row)]}
                index.problem_views_version = version
            rows = index.problem_views
        else:
            signature = _CartoEvidenceIndex.signature(path)
            version = _version([signature, id(overlay.get("result_summary")) if overlay else None,
                                record.get("status")])
            cached = _detail_cache.get(str(path))
            if not isinstance(cached, dict) or cached.get("version") != version:
                try:
                    with Path(path).open("r", encoding="utf-8") as handle:
                        full = json.load(handle)
                except Exception as exc:
                    if overlay is None:
                        return {"success": False, "error": str(exc)[:2000]}
                    full = {}
                full = _overlay_record(full, overlay, complete=True)
                rows = _legacy_problem_rows(full)
                cached = {"version": version, "rows": {"all": rows, "problems": [row for row in rows if _is_problem_row(row)]}}
                _detail_cache[str(path)] = cached
            rows = cached["rows"]
            _detail_cache.move_to_end(str(path))
            while len(_detail_cache) > 4:
                _detail_cache.popitem(last=False)
        return _page_problems(rows, record, data, version)


# The public read-side API is owned here; the pure presentation module has no
# record/cache dependency and therefore cannot accidentally hydrate evidence.
from .carto_job_paging import page_carto_job_detail, join_carto_job_detail_pages  # noqa: E402,F401


_WRITER_SUMMARY_MARKER = "_carto_writer_summary"
_RECORD_PREVIEW_CAPS = {"files_changed": 50, "errors": 20, "refresh_errors": 20,
                        "nexusignore_sources": 50, "missing_paths": 50}
_STRUCTURED_TAIL_KEYS = frozenset({"files", "findings", "warnings", "events", "present_paths",
                                  "phase8", "node_outcomes"})
_MOVE_VALUE_BYTES = 4096
_INLINE_SUMMARY_KEYS = (_SUMMARY_ONLY_KEYS | set(_RECORD_PREVIEW_CAPS) | {
    "status", "success", "cancelled", "successful", "failed", "skipped", "partial_success", "changed",
    "findings_total", "findings_by_code", "findings_truncated", "warnings_total", "warnings_by_type",
    "warnings_truncated", "summary_line",
})


def _contains_carto_tail(value):
    """Recognize nested full evidence lists, not bounded root summary previews."""
    if isinstance(value, Mapping):
        return any((key in _STRUCTURED_TAIL_KEYS | set(_RECORD_PREVIEW_CAPS) and isinstance(item, (list, tuple)))
                   or _contains_carto_tail(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_carto_tail(item) for item in value)
    return False


def _compact_evidence(value):
    # B1 deliberately uses JSON's default ASCII escaping, including lone
    # surrogates; no new encoding restriction on evidence.
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _same_json_value(left, right):
    # Python considers True == 1; the projection rule is deep JSON equality.
    return _compact_evidence(left) == _compact_evidence(right) or (
        json.dumps(left, sort_keys=True, separators=(",", ":"), ensure_ascii=True) ==
        json.dumps(right, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def mark_carto_reconcile_children(row):
    """D3-1: keep outline presence/count/digest, and every finding's anatomy.

    Canonical digest bytes are sorted-key compact default-ASCII JSON. Transform
    both sides before projection equality; unknown values and existing markers
    are preserved, never refused. The input's live working plan is untouched.
    """
    def mark(value):
        if value is None:
            return {"$reconcile_children": None}
        if isinstance(value, list):
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            return {"$reconcile_children": {"count": len(value), "sha256": hashlib.sha256(encoded).hexdigest()}}
        return value
    result = dict(row)
    if "reconcile_children" in result:
        result["reconcile_children"] = mark(result["reconcile_children"])
    projection = result.get("projection")
    if isinstance(projection, Mapping) and "reconcile_children" in projection:
        result["projection"] = {**projection, "reconcile_children": mark(projection["reconcile_children"])}
    return result


def _store_carto_row(row, *, null_keys=()):
    stored = {key: value for key, value in row.items() if key not in {"snapshot", "analysis_inputs"}}
    projection = stored.get("projection")
    keys = None
    if isinstance(projection, Mapping) and all(
            key in stored and key not in null_keys and _same_json_value(value, stored[key])
            for key, value in projection.items()):
        keys = list(projection)
        stored["projection"] = None
    for key in null_keys:
        if key in stored:
            stored[key] = None
    return stored, keys


# @beacon[
#   id=carto-record@writer,
#   role=CartoJobRecord(job_file),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=One job-owned append-only evidence writer: preserves same-key pending order and per-call head ownership, including a node-first structured transition and exact unstructured terminal replacement. Marks outlines before lossless projection compaction; Q6 text stays readable and B1 pending terminal lines remain memory-backed.,
# ]
class CartoJobRecord:
    """The /2 writer core, owned once per job through record_for.

    One writer owns a new job for its lifetime, never resumes old evidence.
    Each disk transaction uses the existing status RLock and funnel. A base
    read of the small current record plus the funnel's precedence read are the
    only record reads; evidence is opened for append/truncate only.

    Complete rows are released after serialization. The only exception is B1:
    serialized lines still pending after finalize travel in _pending_evidence
    on the in-memory terminal proof, not on disk; detail indexes their pieces
    until restart. A failed pending retry blocks only later lines of its own
    logical key, never another key. No ordering field is stored in evidence.
    Head writes overlay only keys this call sets, so external running/freshness
    writes survive; the existing funnel alone selects status precedence.
    Node-first structured jobs keep the seed's empty-list presence; a terminal
    acknowledgement with no row replaces that seed exactly, not additively.

    New aggregates/previews are summary-view presentation, not extra complete
    detail keys (CF-168). FOLDER nexusignore_sources/missing_paths live fully in
    item/set evidence and have only count-bounded head previews (CF-159).
    Q6's preview-derived terminal error stays inline even over 4 KiB: it is
    count-bounded like its source preview, with an O(1) derivation marker, not
    another copy of the complete list or a sidecar read by this writer.
    """

    def __init__(self, job_file):
        from .beacon_refresh_support import CartoOutcomeAccumulator
        self.path = Path(job_file)
        self.accumulator = CartoOutcomeAccumulator()
        self._head = {}
        self._dirty_head = set()
        self._dirty_progress = set()
        self._summary_values = {}
        self._has_summary = False
        self._moved_values = {}
        self._last_payload = None
        self._previews = {}
        self._preview_totals = {}
        self._pending = []
        self._pending_warned = set()
        self._bytes = {"files": 0, "nodes": 0}
        self._lines = {"files": 0, "nodes": 0}
        self._rows = {"files": 0, "nodes": 0}
        self._prefix_newline = {"files": False, "nodes": False}
        self._in_flight = None
        self._structured = False
        self._is_file = False
        self._file_row_keys = set()

    def _cleared(self):
        from .config import is_carto_job_cleared
        return is_carto_job_cleared(self.path, self._head.get("id"))

    def create(self, payload):
        """Write initial /2 state; merely constructing a writer writes nothing."""
        self._is_file = payload.get("type") == "CARTO_REFRESH" and str(payload.get("mode") or "").lower() == "file"
        return self.update_head(**payload)

    def initial_payload(self, payload):
        """Seed admission's sole writer and lay out its one atomic initial write.

        Admission keeps its injectable atomic_writer and reservation rollback.
        This prepares the same head as create without a second record write or
        a read/resume of old evidence. Subsequent writes use the status funnel.
        """
        from .config import _CARTO_JOB_STATUS_WRITE_LOCK, carto_job_record_layout
        with _CARTO_JOB_STATUS_WRITE_LOCK:
            self._is_file = payload.get("type") == "CARTO_REFRESH" and str(payload.get("mode") or "").lower() == "file"
            self._take_head(dict(payload))
            result = carto_job_record_layout(self._record_payload())
            # Admission makes the sole initial write from this payload. Its
            # seed is not a new change at the first later progress flush.
            self._dirty_head.clear()
            self._dirty_progress.clear()
            return result

    def _append_bytes(self, name, data):
        """Append atomically as a line, repairing an injected partial failure.

        No read-open: append's tell supplies the current physical size, even
        after a failed truncate. A fresh newline isolates torn bytes then.
        """
        path = self.path.with_suffix("") / (name + ".jsonl")
        start = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as stream:
                start = stream.tell()
                payload = (b"\n" if self._prefix_newline[name] else b"") + data
                written = stream.write(payload)
                if written != len(payload):
                    raise OSError("short evidence append")
                stream.flush()
                self._bytes[name] = stream.tell()
            self._lines[name] += 1 + int(self._prefix_newline[name])
            self._prefix_newline[name] = False
            return True
        except Exception as exc:
            _LOG.warning("Could not append Cartographer evidence %s: %s", path, exc)
            if start is not None:
                try:
                    with path.open("ab") as stream:
                        stream.truncate(start)
                    self._bytes[name] = start
                except Exception as truncate_error:
                    self._prefix_newline[name] = True
                    _LOG.warning("Could not truncate failed Cartographer append %s: %s", path, truncate_error)
            return False

    def _land(self, entry):
        name, text, kind, _file, _key = entry
        if self._append_bytes(name, text.encode("utf-8")):
            if kind in {"row", "node"}:
                self._rows[name] += 1
            return True
        return False

    def _evidence_key(self, name, line):
        """Only pending-line dependency metadata, never an on-disk index/order."""
        kind = line.get("k")
        if kind == "row":
            return (name, kind, str(line.get("sec") or "files"),
                    canonical_carto_file((line.get("row") or {}).get("file")))
        if kind == "node":
            return (name, kind, canonical_carto_file(line.get("file")))
        key = line.get("key")
        if kind == "set":
            key = (tuple(key) if isinstance(key, list) else
                   ("result_summary", key[len("result_summary."):]) if isinstance(key, str) and key.startswith("result_summary.") else
                   ("result_summary", key))
        return (name, kind, key)

    def _retry_pending(self):
        previous, self._pending = self._pending, []
        blocked = set()
        for entry in previous:
            if entry[4] in blocked or not self._land(entry):
                self._pending.append(entry)
                blocked.add(entry[4])

    def _append(self, name, line):
        from .config import _CARTO_JOB_STATUS_WRITE_LOCK
        with _CARTO_JOB_STATUS_WRITE_LOCK:
            if self._cleared():
                return False
            self._retry_pending()
            row = line.get("row") or {}
            key = self._evidence_key(name, line)
            entry = (name, _compact_evidence(line) + "\n", line.get("k"), row.get("file", line.get("file")), key)
            # A later delta/set/receipt must never overtake a still-pending
            # line of its key. Other keys keep appending independently (B1).
            landed = not any(old[4] == key for old in self._pending) and self._land(entry)
            if not landed:
                self._pending.append(entry)
            return landed

    def _bounded_value(self, path, value):
        # Q4: count-bounded previews AND aggregate counters stay inline. The
        # byte move is for unbounded values, not preview growth at boundaries.
        if len(path) == 2 and path[0] == "result_summary" and path[1] in _INLINE_SUMMARY_KEYS:
            return copy.deepcopy(value)
        text = _compact_evidence(value)
        if (len(text.encode("utf-8")) > _MOVE_VALUE_BYTES or
                (self._structured and _contains_carto_tail(value))):
            signature = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if self._moved_values.get(tuple(path)) != signature:
                self.set_value(tuple(path), value)
            return {"$evidence": "files.jsonl", "bytes": len(text.encode("utf-8"))}
        if tuple(path) in self._moved_values:
            # Supersede the old moved value even when this version is inline.
            # Both detail paths may then keep their ordinary last-set rule;
            # FILE-row pointers (which have no set line) are not reinterpreted.
            signature = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if self._moved_values[tuple(path)] != signature:
                self.set_value(tuple(path), value)
        return copy.deepcopy(value)

    def _take_summary(self, values):
        self._has_summary = True
        own_summary = values.get(_WRITER_SUMMARY_MARKER) is True
        for key, value in values.items():
            if self._is_file and self._structured and key in self._file_row_keys:
                # The FILE row is authoritative for its own keys, including
                # original refreshed/partial_section scalars. Its non-list
                # evidence is already stored by that row, not in another set.
                continue
            if key == _WRITER_SUMMARY_MARKER:
                continue
            if isinstance(value, list) and self._structured:
                if own_summary:
                    # The exit door returns our already bounded previews, not
                    # complete lists. Never replace their exact totals with
                    # the preview lengths during finalization.
                    continue
                if key in _RECORD_PREVIEW_CAPS:
                    self._previews[key] = copy.deepcopy(value[:_RECORD_PREVIEW_CAPS[key]])
                    self._preview_totals[key] = len(value)
                # Lists supplied here already live in row/item evidence. Empty
                # present_paths has no item to carry its presence.
                if not value and key == "present_paths":
                    self.set_value(("result_summary", key), [])
                elif key not in _STRUCTURED_TAIL_KEYS and key not in _RECORD_PREVIEW_CAPS:
                    self.set_value(("result_summary", key), value)
                continue
            if self._structured and key in _STRUCTURED_TAIL_KEYS:
                continue
            self._summary_values[key] = self._bounded_value(("result_summary", key), value)

    def _take_file_row_summary(self, row):
        """Point large/nested FILE evidence at its existing row, never duplicate it."""
        self._has_summary = True
        self._file_row_keys = set(row) - {"snapshot", "analysis_inputs"}
        for key, value in row.items():
            if key in {"snapshot", "analysis_inputs"} or isinstance(value, (list, tuple)):
                continue
            encoded = _compact_evidence(value).encode("utf-8")
            if key not in _INLINE_SUMMARY_KEYS and (len(encoded) > _MOVE_VALUE_BYTES or _contains_carto_tail(value)):
                self._summary_values[key] = {"$evidence": "files.jsonl", "bytes": len(encoded)}
            else:
                self._summary_values[key] = copy.deepcopy(value)

    def _warning_display(self, values):
        display = list(self._head.get("warnings") or [])
        seen = {_compact_evidence(value) for value in display}
        for value in values or ():
            key = _compact_evidence(value)
            if key not in seen and len(display) < 200:
                display.append(copy.deepcopy(value))
                seen.add(key)
        self._head["warnings"] = display

    def _take_head(self, changes):
        values = changes.pop("result_summary", None)
        derived_error = _carto_terminal_text_recipe(changes) is not None
        for key, value in changes.items():
            if key in {"record_layout", "evidence", "in_flight", "_pending_evidence"}:
                continue
            if key == "progress" and isinstance(value, Mapping):
                self._dirty_progress.update(value)
                value = {**(self._head.get("progress") or {}), **value}
            if key == "warnings" and isinstance(value, list):
                # Display only; the accumulator/evidence carries all entries.
                self._head["warning_count"] = changes.get("warning_count", len(value))
                value = value[:200]
            self._head[key] = (copy.deepcopy(value) if key in {"warnings", "progress"} or (key == "error" and derived_error) else
                                self._bounded_value((key,), value))
            self._dirty_head.add(key)
        if isinstance(values, Mapping):
            self._take_summary(values)

    def update_head(self, **changes):
        """Merge small head changes; preserve current on-disk foreign keys."""
        from .config import _CARTO_JOB_STATUS_WRITE_LOCK
        with _CARTO_JOB_STATUS_WRITE_LOCK:
            if self._cleared():
                return False
            self._take_head(changes)
            return self._write_record()

    def set_in_flight(self, row, *, progress=None):
        """Persist only the placeholder; a producer may keep its existing progress.

        Folder boundaries display the mapped FILE UUID (and its relpath), not
        the evidence's physical file key. Preserve that caller-owned progress
        without changing the default standalone writer interface.
        """
        from .config import _CARTO_JOB_STATUS_WRITE_LOCK
        with _CARTO_JOB_STATUS_WRITE_LOCK:
            self._in_flight = None if row is None else {
                key: copy.deepcopy(row[key]) for key in ("file", "file_node_id") if key in row}
            if self._in_flight is not None:
                self._in_flight.update(status="failed", changed=False, findings=[], warnings=[], events=[],
                    error="File attempt interrupted before completion", base_lines=self._lines["files"])
            progress = ({"current_file": row.get("file") if row is not None else None} if progress is None else progress)
            self._dirty_progress.update(progress)
            self._head["progress"] = {**(self._head.get("progress") or {}), **progress}
            return self._write_record()

    def append_file_row(self, row, *, section="files", delta=None, nodes_reflected=None):
        """Mark its outline, append one final row, then publish its small summary."""
        from .config import _CARTO_JOB_STATUS_WRITE_LOCK
        with _CARTO_JOB_STATUS_WRITE_LOCK:
            if self._cleared():
                return False
            receipt_count = nodes_reflected
            if receipt_count is None and "node_outcomes" in row:
                receipts = row["node_outcomes"]
                receipt_count = len(receipts or ()) if isinstance(receipts, (list, tuple)) else self.accumulator.reflected_nodes(row.get("file"))
            evidence_row = mark_carto_reconcile_children(row)
            stored, proj = _store_carto_row(evidence_row, null_keys=("node_outcomes",) if receipt_count is not None else ())
            line = {"k": "row", "sec": section, "row": stored}
            if receipt_count is not None:
                line["nodes"] = receipt_count
            if delta is not None:
                line["delta"] = delta
            if proj is not None:
                line["proj"] = proj
            landed = self._append("files", line)
            if not self._structured:
                # A pre-first-file boundary carries empty list presence. Keep
                # that presence when switching to structured summaries; later
                # item lines already own any complete entries and exact totals.
                for key, value in self._summary_values.items():
                    if key in _RECORD_PREVIEW_CAPS and isinstance(value, list):
                        self._previews.setdefault(key, copy.deepcopy(value[:_RECORD_PREVIEW_CAPS[key]]))
                        self._preview_totals.setdefault(key, len(value))
            self._structured = True
            self._summary_values = {key: value for key, value in self._summary_values.items()
                                    if key not in _STRUCTURED_TAIL_KEYS and not isinstance(value, list)}
            self.accumulator.put(row, section=section, delta=delta, nodes_reflected=receipt_count)
            self._warning_display(row.get("warnings"))
            self._warning_display(((delta or {}).get("extend") or {}).get("warnings"))
            if self._is_file and section == "files":
                self._take_file_row_summary(evidence_row)
            if self._in_flight is not None and canonical_carto_file(self._in_flight.get("file")) == canonical_carto_file(row.get("file")):
                self._in_flight = None
            saved = self._write_record()
            return landed and saved

    def append_node_rows(self, rows):
        """Append returned receipts before one record write, preserving empty-list presence."""
        from .config import _CARTO_JOB_STATUS_WRITE_LOCK
        with _CARTO_JOB_STATUS_WRITE_LOCK:
            if self._cleared():
                return False
            landed = True
            for entry in rows:
                if not self._structured:
                    for key, value in self._summary_values.items():
                        if key in _RECORD_PREVIEW_CAPS and isinstance(value, list):
                            self._previews.setdefault(key, copy.deepcopy(value[:_RECORD_PREVIEW_CAPS[key]]))
                            self._preview_totals.setdefault(key, len(value))
                line = {"k": "node", "file": entry["file"], "row": entry["row"]}
                landed = self._append("nodes", line) and landed
                self.accumulator.put_nodes([line])
                self._structured = True
            saved = self._write_record()
            return landed and saved

    def append_item(self, key, value):
        """Append a complete run item; the next boundary publishes its counts."""
        from .config import _CARTO_JOB_STATUS_WRITE_LOCK
        with _CARTO_JOB_STATUS_WRITE_LOCK:
            if self._cleared():
                return False
            landed = self._append("files", {"k": "item", "key": key, "v": value})
            if key == "run_warnings":
                self.accumulator.add_run_warning(value)
                self._warning_display([value])
            elif key == "run_events":
                self.accumulator.add_run_event(value)
            elif key in _RECORD_PREVIEW_CAPS:
                self._preview_totals[key] = self._preview_totals.get(key, 0) + 1
                preview = self._previews.setdefault(key, [])
                if len(preview) < _RECORD_PREVIEW_CAPS[key]:
                    preview.append(copy.deepcopy(value))
            return landed

    def set_value(self, key, value):
        """Append one last-wins complete value, without a record rewrite."""
        from .config import _CARTO_JOB_STATUS_WRITE_LOCK
        with _CARTO_JOB_STATUS_WRITE_LOCK:
            if self._cleared():
                return False
            if key == "phase8_envelope":
                self.accumulator.set_phase8(value)
                self._warning_display(value.get("warnings"))
            path = key if isinstance(key, tuple) else ("result_summary", key)
            encoded = _compact_evidence(value).encode("utf-8")
            self._moved_values[path] = hashlib.sha256(encoded).hexdigest()
            if key != "phase8_envelope":
                target = self._head if len(path) == 1 else self._summary_values
                self._has_summary = self._has_summary or len(path) > 1
                target[path[-1]] = {"$evidence": "files.jsonl", "bytes": len(encoded)} if len(encoded) > _MOVE_VALUE_BYTES else copy.deepcopy(value)
                if len(path) == 1:
                    self._dirty_head.add(path[0])
            return self._append("files", {"k": "set", "key": list(key) if isinstance(key, tuple) else key, "v": value})

    def summary(self, *, infrastructure_error=None, cancelled=False):
        """Transient marked writer outcome; no tail rows or full run lists."""
        result = {key: value for key, value in self._summary_values.items()
                  if not self._structured or key not in _STRUCTURED_TAIL_KEYS}
        if self._structured:
            if infrastructure_error is None:
                infrastructure_error = self._summary_values.get("infrastructure_error")
            cancelled = bool(cancelled or self._summary_values.get("cancelled"))
            aggregates = self.accumulator.outcome(infrastructure_error=infrastructure_error, cancelled=cancelled)
            result.update(aggregates)
            result.update(copy.deepcopy(self._previews))
            totals = {**result.get("preview_totals", {}), **self._preview_totals}
            if totals:
                result["preview_totals"] = totals
            result[_WRITER_SUMMARY_MARKER] = True
        return result

    def _record_payload(self):
        result = dict(self._head)
        result["record_layout"] = CARTO_JOB_RECORD_LAYOUT_V2
        result["in_flight"] = copy.deepcopy(self._in_flight)
        summary = self.summary()
        summary.pop(_WRITER_SUMMARY_MARKER, None)
        if self._is_file and self._structured:
            # Only the saved FILE summary preserves its original scalar keys;
            # the transient marked summary carries component classification.
            summary.update(self._summary_values)
        if self._structured:
            summary = {key: value for key, value in summary.items() if key not in _STRUCTURED_TAIL_KEYS}
            for key, value in list(summary.items()):
                if not (isinstance(value, dict) and "$evidence" in value):
                    summary[key] = self._bounded_value(("result_summary", key), value)
        if summary or self._has_summary or self._structured:
            result["result_summary"] = summary
        # Large values may just have appended set lines. Snapshot counters
        # afterwards so this same boundary reports every landed line.
        result["evidence"] = {"files": {"path": "files.jsonl", "rows": self._rows["files"],
            "lines": self._lines["files"], "bytes": self._bytes["files"],
            "pending": sum(entry[0] == "files" for entry in self._pending)},
            "nodes": ({"path": "nodes.jsonl", "rows": self._rows["nodes"], "bytes": self._bytes["nodes"]}
                      if self._rows["nodes"] or any(entry[0] == "nodes" for entry in self._pending) else None)}
        result.update(carto_job_outcome(result))
        return result

    def _write_record(self):
        from .config import _CARTO_JOB_STATUS_WRITE_LOCK, merge_write_carto_job_status
        with _CARTO_JOB_STATUS_WRITE_LOCK:
            if self._cleared():
                return False
            try:
                current = read_carto_job_record_head(self.path)
            except (OSError, ValueError):
                current = {}
            own = self._record_payload()
            owned = {"record_layout", "evidence", "in_flight", "result_summary", "warnings", "warning_count"}
            payload = {**own, **current, **{key: value for key, value in own.items()
                                         if key in self._dirty_head | owned and key != "progress"}}
            progress = own.get("progress")
            if isinstance(progress, Mapping):
                payload["progress"] = {**progress, **(current.get("progress") or {}),
                    **{key: value for key, value in progress.items() if key in self._dirty_progress}}
            payload.update(carto_job_outcome(payload))
            selected = {}
            saved = merge_write_carto_job_status(self.path, payload, _record_out=selected)
            self._last_payload = selected or payload
            if selected:
                self._head["status"] = selected.get("status")
                self._head["warnings"] = selected.get("warnings", [])
            # Ownership belongs to this call, not to a failed earlier write:
            # a subsequent foreign value must survive a later progress call.
            self._dirty_head.clear()
            self._dirty_progress.clear()
            return saved

    def pending_evidence_lines(self):
        """Decoded complete pending data for tests/explicit detail, never a run read."""
        return {name: [json.loads(entry[1]) for entry in self._pending if entry[0] == name]
                for name in ("files", "nodes")}

    def finalize(self, payload):
        """Retry pending lines once, publish terminal summary, return its proof."""
        from .config import _CARTO_JOB_STATUS_WRITE_LOCK
        with _CARTO_JOB_STATUS_WRITE_LOCK:
            if not self._cleared():
                self._retry_pending()
            if not self._structured and isinstance(payload.get("result_summary"), Mapping):
                # An unstructured terminal result replaces admission's seed as
                # the old funnel did (notably bulk's no-target fast path).
                self._summary_values.clear()
            saved = self.update_head(**payload)
            result = dict(self._last_payload) if self._last_payload is not None else self._record_payload()
            result["path"] = str(self.path)
            if not saved:
                result["status_write_error"] = "Could not persist terminal Cartographer record"
            if self._pending:
                result["_pending_evidence"] = {name: [entry[1] for entry in self._pending if entry[0] == name]
                                               for name in ("files", "nodes")}
                for entry in self._pending:
                    identity = entry[3] or "run evidence"
                    if identity not in self._pending_warned:
                        _LOG.warning("Cartographer terminal evidence for %s remains pending in memory until restart", identity)
                        self._pending_warned.add(identity)
            return result


_job_records = {}


# @beacon[
#   id=carto-record@registry,
#   role=record_for(job_file, create=True),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=One process-local writer per normalized job path under the existing status lock; lookup-only callers never construct or resume a writer and the common exit releases it after terminal persistence.,
# ]
def record_for(job_file, *, create=True):
    """Return this job's sole writer, or lookup only with create=False.

    Registration never reads or resumes saved evidence. Admission owns creation;
    shared seams only look up. Use the status RLock, not a second lock order.
    """
    from .config import _CARTO_JOB_STATUS_WRITE_LOCK
    key = os.path.normcase(os.path.abspath(os.fspath(job_file)))
    with _CARTO_JOB_STATUS_WRITE_LOCK:
        writer = _job_records.get(key)
        if writer is None and create:
            writer = _job_records[key] = CartoJobRecord(job_file)
        return writer


def release_carto_job_record(job_file):
    """Release the registered writer after its joined terminal write attempt."""
    from .config import _CARTO_JOB_STATUS_WRITE_LOCK
    key = os.path.normcase(os.path.abspath(os.fspath(job_file)))
    with _CARTO_JOB_STATUS_WRITE_LOCK:
        return _job_records.pop(key, None)
