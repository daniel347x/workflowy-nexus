"""The GLIMPSE widget's job list: compact rows from job-record heads.

The widget in every Workflowy tab asks for ``carto_list_jobs`` once a second.
Its rows need a job's identity, status, progress and warnings, never the
per-file evidence a folder refresh keeps in ``result_summary`` (tens of MB for
~500 files). Until 2026-10-26 every poll decoded every record in full and sent
it whole: a 522-file refresh's reply passed the 16 MiB WebSocket limit, and the
decoding starved the refresh itself ([ap:3VXWG7]).

Here records are read through the shared ``read_carto_job_record_cached`` and remembered per path
by (mtime_ns, size): an unchanged record is never read again, a changing one
costs only its head. Rows carry only the fields the widget renders, and the
reply has a hard byte budget. ``system.job.status(job_id, view="detail")``
reads one job's complete evidence; its default is the cheap summary view.
"""
from __future__ import annotations

import json
import hashlib
import os
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .config import is_carto_job_terminal
from .carto_job_record import (
    read_carto_job_record_cached, prune_carto_job_record_cache,
    _record_cache, _record_cache_lock,
)

# Hard budget for one carto_list_jobs_result frame: far below the broker's
# 16 MiB WebSocket limit, and cheap to broadcast to every tab each second.
CARTO_WIDGET_REPLY_BUDGET_BYTES = 512 * 1024
# The scanner already caps forwarded warnings at 200; the widget's tooltip
# lists 40 of them. Only a reply over budget trims lists to the tooltip's 40.
_WIDGET_WARNINGS_CAP = 200
_WIDGET_WARNINGS_TRIMMED = 40
_WIDGET_TEXT_CAP = 2000
_WIDGET_WARNING_TEXT_CAP = 500
# What handleCartoJobsResult / formatCartoHeartbeatDiagnostic read, plus the
# timestamps and error kind that identify a row. Never result_summary.
_WIDGET_ROW_FIELDS = frozenset({
    "job_id", "kind", "status", "mode", "root_uuid", "detached", "created_at", "updated_at",
    "progress", "error", "error_kind", "warning_count", "warnings", "heartbeat",
})
_WIDGET_WARNING_FIELDS = ("type", "message", "error", "file", "source_path")
_TRUNCATION_NOTE_RESERVE = 512

# @beacon[
#   id=carto-widget@read-record,
#   role=read_carto_widget_record(job_path),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=Compatibility entry point into the shared signature-validated job head cache; the widget and every other inventory reader now share one decode per signature and never open evidence.,
# ]
def read_carto_widget_record(job_path: str | os.PathLike[str]) -> dict[str, Any]:
    """One shared cached head; retained as the widget's compatibility entry."""
    return read_carto_job_record_cached(job_path)


def prune_carto_widget_record_cache() -> None:
    """Compatibility entry into shared vanished-path pruning."""
    prune_carto_job_record_cache()


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _clip(value: Any, cap: int) -> Any:
    if not _is_scalar(value):
        value = str(value)
    if isinstance(value, str) and len(value) > cap:
        return value[:cap] + "…"
    return value


def _widget_warning(entry: Any) -> Any:
    if isinstance(entry, Mapping):
        return {key: _clip(entry[key], _WIDGET_WARNING_TEXT_CAP)
                for key in _WIDGET_WARNING_FIELDS if key in entry}
    return _clip(entry, _WIDGET_WARNING_TEXT_CAP)


# @beacon[
#   id=carto-widget@compact-row,
#   role=compact_carto_widget_row(job),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=One scanner row reduced to what the widget renders (id kind status mode root progress scalars error warnings heartbeat timestamps), in the row's own key order; strings clipped, warnings to type/message/error/file/source_path. result_summary and other evidence never pass.,
# ]
def compact_carto_widget_row(job: Mapping[str, Any]) -> dict[str, Any]:
    """The widget's view of one scanner row; small values pass unchanged."""
    row: dict[str, Any] = {}
    for key, value in job.items():
        if key not in _WIDGET_ROW_FIELDS:
            continue
        if key in {"progress", "heartbeat"}:
            if isinstance(value, Mapping):
                value = {name: _clip(item, _WIDGET_TEXT_CAP) for name, item in value.items() if _is_scalar(item)}
            elif not _is_scalar(value):
                continue
        elif key == "warnings" and isinstance(value, list):
            value = [_widget_warning(entry) for entry in value[:_WIDGET_WARNINGS_CAP]]
        else:
            value = _clip(value, _WIDGET_TEXT_CAP)
        row[key] = value
    return row


def _reply_text(rows: list[dict[str, Any]], truncated: dict[str, Any] | None) -> str:
    reply: dict[str, Any] = {"action": "carto_list_jobs_result", "success": True, "jobs": rows}
    if truncated is not None:
        reply["truncated"] = truncated
    return json.dumps(reply)


# @beacon[
#   id=carto-widget@encode-reply,
#   role=encode_carto_widget_reply(jobs),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=Encodes the widget's carto_list_jobs_result from compact rows in scanner order under a hard 512 KiB budget. Over budget it omits the oldest finished jobs first then trims warning lists to the tooltip's 40 then omits the oldest remaining, and says so in a truncated field.,
# ]
def encode_carto_widget_reply(
    jobs: Iterable[Mapping[str, Any]], *, budget: int = CARTO_WIDGET_REPLY_BUDGET_BYTES,
) -> str:
    """The widget reply text (``action``, ``success``, ``jobs``), never over ``budget`` bytes."""
    rows = [compact_carto_widget_row(job) for job in jobs]
    text = _reply_text(rows, None)
    if len(text) <= budget:
        return text
    # Oldest first, finished jobs before active ones.
    order = sorted(range(len(rows)), key=lambda i: (
        not is_carto_job_terminal(rows[i].get("status")), str(rows[i].get("created_at") or ""), i))
    sizes = [len(json.dumps(row)) + 2 for row in rows]
    total = len(text) + _TRUNCATION_NOTE_RESERVE
    dropped: set[int] = set()
    for index in order:
        if total <= budget or not is_carto_job_terminal(rows[index].get("status")):
            break
        dropped.add(index)
        total -= sizes[index]
    trimmed = False
    if total > budget:
        trimmed = True
        for index, row in enumerate(rows):
            warnings = row.get("warnings")
            if index not in dropped and isinstance(warnings, list) and len(warnings) > _WIDGET_WARNINGS_TRIMMED:
                rows[index] = {**row, "warnings": warnings[:_WIDGET_WARNINGS_TRIMMED]}
                size = len(json.dumps(rows[index])) + 2
                total -= sizes[index] - size
                sizes[index] = size
    while True:
        for index in order:
            if total <= budget:
                break
            if index not in dropped:
                dropped.add(index)
                total -= sizes[index]
        kept = [row for index, row in enumerate(rows) if index not in dropped]
        text = _reply_text(kept, {
            "jobs_omitted": len(dropped),
            "warnings_trimmed_to": _WIDGET_WARNINGS_TRIMMED if trimmed else None,
            "budget_bytes": budget,
            "detail": ("Reply over its byte budget: the oldest finished jobs were left out first. "
                       "system.job.status lists every job and its full record."),
        })
        if len(text) <= budget or not kept:
            return text
        total = len(text) + 1  # the estimate was short: keep omitting oldest first


CARTO_LIST_PROTOCOL_2_ACTION = "carto_list_jobs_result_v2"
_PROTOCOL_2_FIELDS = (
    "job_id", "kind", "status", "outcome", "mode", "root_uuid", "has_errors", "has_warnings",
    "has_findings", "errors_total", "findings_total", "warning_count",
)
_PROTOCOL_2_PROGRESS = (
    "total_files", "completed_files", "current_phase", "nodes_created", "nodes_updated",
    "nodes_moved", "nodes_deleted", "current_file", "current_file_relpath",
)
_PROTOCOL_2_HEARTBEAT = ("applicable", "state", "age_seconds", "identity_mismatch")


# @beacon[
#   id=carto-widget@encode-protocol-2,
#   role=encode_carto_widget_reply_v2(jobs),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=Encodes only the protocol-2 list allowlist with capped tooltip warnings and a content-derived generation under the existing byte budget; its distinct action is ignored by pre-G6 pages and unchanged replies cost under 120 bytes.,
# ]
def encode_carto_widget_reply_v2(jobs: Iterable[Mapping[str, Any]], *,
                                budget: int = CARTO_WIDGET_REPLY_BUDGET_BYTES) -> tuple[str, str]:
    """Return (full reply text, generation); equal wire content has equal generation."""
    jobs = list(jobs)
    rows = []
    for job in jobs:
        row = {key: _clip(job.get(key), _WIDGET_TEXT_CAP) for key in _PROTOCOL_2_FIELDS}
        for field, allowed in (("progress", _PROTOCOL_2_PROGRESS), ("heartbeat", _PROTOCOL_2_HEARTBEAT)):
            values = job.get(field)
            row[field] = ({key: _clip(values[key], _WIDGET_TEXT_CAP) for key in allowed
                           if key in values and _is_scalar(values[key])} if isinstance(values, Mapping) else None)
        row["warnings"] = [_widget_warning(entry) for entry in (job.get("warnings") or [])[:40]]
        rows.append(row)
    order = sorted(range(len(rows)), key=lambda i: (
        not is_carto_job_terminal(jobs[i].get("status")), str(jobs[i].get("created_at") or ""), i))
    omitted: set[int] = set()
    while True:
        content = {"jobs": [row for i, row in enumerate(rows) if i not in omitted]}
        if omitted:
            content["truncated"] = {"jobs_omitted": len(omitted), "budget_bytes": budget}
        encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        generation = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
        reply = {"action": CARTO_LIST_PROTOCOL_2_ACTION, "success": True, **content, "generation": generation}
        text = json.dumps(reply, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) <= budget or len(omitted) == len(rows):
            return text, generation
        omitted.add(next(index for index in order if index not in omitted))


def encode_carto_widget_unchanged(generation: str) -> str:
    """The broadcast no-redraw reply, with its own pre-G6-safe action."""
    return json.dumps({"action": CARTO_LIST_PROTOCOL_2_ACTION, "success": True,
                       "unchanged": True, "generation": generation}, separators=(",", ":"))
