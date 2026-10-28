"""Bounded MCP job inventories over the existing status scanner's records.

Presentation only: compact inventories, one-job option normalization and the
shared one-job reply budget. Complete detail is on demand; the one-job default is its
head-only summary, never an implicit evidence read.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from .config import CARTO_TERMINAL_STATUSES, is_carto_job_active
from .mcp_api_registry import OperationValidationError

JOB_LIST_MAX_BYTES = 32_768
JOB_REPLY_MAX_BYTES = 524_288
JOB_LIST_DEFAULT_LIMIT = 50
JOB_LIST_MAX_LIMIT = 200


def normalize_job_status_options(
    job_id: str | None, *, status: str | None = None,
    kind: str | None = None, limit: int | None = None,
    view=None, files=None, file=None, files_offset=None, files_limit=None, offset=None, slice_offset=None,
) -> tuple[str, str | None, int]:
    """One pre-I/O validator for consolidated and legacy fronts."""
    for field, value in (("job_id", job_id), ("status", status), ("kind", kind)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise OperationValidationError("invalid_parameter_type", f"{field} must be a non-blank string")
    if job_id is not None and any(value is not None for value in (status, kind, limit)):
        raise OperationValidationError(
            "invalid_parameter_combination", "job_id selects one job (summary by default, view='detail' for complete evidence); status/kind/limit are list-only filters",
        )
    if limit is None:
        limit = JOB_LIST_DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise OperationValidationError("invalid_parameter_type", "limit must be an integer")
    if not 1 <= limit <= JOB_LIST_MAX_LIMIT:
        raise OperationValidationError("invalid_parameter_value", "limit must be between 1 and 200")
    normalize_job_detail_options(job_id, view=view, files=files, file=file, files_offset=files_offset,
                                 files_limit=files_limit, offset=offset, slice_offset=slice_offset)
    return (status or "active").strip().lower(), kind.strip().lower() if kind is not None else None, limit


def normalize_job_detail_options(job_id, *, view=None, files=None, file=None, files_offset=None,
                                 files_limit=None, offset=None, slice_offset=None):
    """Unknown values warn and serve summary; file > files > view, with ignored selectors named.

    These are views of the same status authority, not schema modes. The public
    schema remains closed and type validation still applies; unknown values
    never refuse a job read. One-job options without an id are ignored visibly.
    """
    supplied = {key: value for key, value in locals().copy().items() if key != "job_id" and value is not None}
    warnings = []
    for key, value in supplied.items():
        expected = str if key in {"view", "files", "file"} else int
        if not isinstance(value, expected) or isinstance(value, bool):
            raise OperationValidationError("invalid_parameter_type", f"{key} must be {'a string' if expected is str else 'an integer'}")
    if job_id is None:
        return {"selection": "summary", "parameter_warnings": [f"Ignored {key} without job_id" for key in supplied],
                "offset": 0, "slice_offset": 0, "files_offset": 0, "files_limit": 100}
    selection = "file" if file is not None else "files" if files is not None else (view or "summary")
    if view is not None and view not in {"summary", "detail"}:
        warnings.append("Unknown view value; using summary unless file/files selects the view")
        if selection not in {"files", "file"}:
            selection = "summary"
    if files is not None and files not in {"all", "problems"}:
        warnings.append("Unknown files filter; using summary unless file selects the view")
        if selection == "files":
            selection = "summary"
    if file is not None and not file:
        warnings.append("Empty file value; using summary")
        selection = "summary"
    for key, value in (("offset", offset), ("slice_offset", slice_offset), ("files_offset", files_offset), ("files_limit", files_limit)):
        if value is not None and (value < 0 or (key == "files_limit" and value == 0)):
            warnings.append(f"Unknown {key} value; using summary")
            selection = "summary"
    if selection == "file":
        warnings.extend(f"Ignored {key}: file selects the view" for key in ("files", "view") if key in supplied)
    elif selection == "files" and view is not None:
        warnings.append("Ignored view: files selects the view")
    if files_offset is not None and selection != "files":
        warnings.append("Ignored files_offset outside the files view")
    if files_limit is not None and selection == "summary":
        warnings.append("Ignored files_limit in the summary view")
    return {"selection": selection, "file": file, "files": files,
            "offset": max(0, offset or 0), "slice_offset": max(0, slice_offset or 0),
            "files_offset": max(0, files_offset or 0), "files_limit": max(1, files_limit or 100),
            "parameter_warnings": warnings}


def _stamp(job: Mapping[str, Any]) -> float:
    """Newest-created/started first, tolerant of legacy naive ISO timestamps."""
    value = job.get("created_at") or job.get("started_at")
    if not isinstance(value, str):
        return 0.0
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _compact_job(job: Mapping[str, Any], detached: bool) -> dict[str, Any]:
    # Closed scalar allowlist: never echo progress/result/warnings/payload/error
    # trees, log paths, heartbeat details or task objects in an inventory.
    row: dict[str, Any] = {"detached": bool(job.get("detached", detached))}
    for key in ("job_id", "kind", "status", "mode", "root_uuid", "created_at", "started_at", "finished_at", "error_kind"):
        value = job.get(key)
        if not isinstance(value, str):
            continue
        if key not in ("job_id", "root_uuid"):
            value = value[:160] + ("…" if len(value) > 160 else "")
            value = " ".join(value.splitlines())
        # Handles stay exact. If a corrupt identifier is huge, the overall
        # byte budget omits its row instead of returning a misleading handle.
        row[key] = value
    return row


# @beacon[
#   id=job-status@compact-inventory,
#   slice_labels=nexus--mcp-api,ra-carto-jobs,
#   kind=ast,
#   comment=Pure MCP inventory with active/inactive counts, scalar rows and newest-first filters; the existing 32-KiB UTF-8 ceiling includes ignored-parameter warnings. Unknown statuses remain active and no lifecycle or single-job data is changed.,
# ]
def summarize_job_inventory(
    in_memory: Iterable[Mapping[str, Any]], detached: Iterable[Mapping[str, Any]], *,
    status: str = "active", kind: str | None = None, limit: int = JOB_LIST_DEFAULT_LIMIT,
    parameter_warnings: Iterable[str] = (),
) -> dict[str, Any]:
    """Counts describe the WHOLE inventory, independent of list filters/caps.

    ``matched`` is the number selected by status/kind before limiting;
    ``returned`` and ``omitted`` make partial lists impossible to mistake for
    a full answer. Unknown/blank status is active by the existing authority.
    Parameter-warning metadata shares the same byte budget as the rows.
    """
    inactive_by_status = {name: 0 for name in sorted(CARTO_TERMINAL_STATUSES)}
    active_count = 0
    selected = []
    total = 0
    for source, is_detached in ((in_memory, False), (detached, True)):
        for job in source:
            total += 1
            raw_status = job.get("status")
            normalized_status = str(raw_status or "").strip().lower()
            active = is_carto_job_active(raw_status)
            if active:
                active_count += 1
            else:
                inactive_by_status[normalized_status] += 1
            if status == "active":
                include = active
            elif status == "inactive":
                include = not active
            elif status == "all":
                include = True
            else:
                include = normalized_status == status
            if include and (kind is None or str(job.get("kind") or "").strip().lower() == kind):
                selected.append((_stamp(job), str(job.get("job_id") or ""), job, is_detached))
    selected.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    result = {
        "success": True,
        "in_memory_jobs": [], "detached_jobs": [],
        "total": total, "active_count": active_count, "inactive_count": total - active_count,
        "inactive_by_status": inactive_by_status,
        "matched": len(selected), "returned": 0, "omitted": len(selected), "truncated": bool(selected),
        "detail_hint": "Pass job_id for its summary, with view='detail' for complete paged evidence. List filters: status (active/inactive/all/exact), kind, limit (1-200). Counts cover all jobs; lists are newest first. Terminal status does not prove claim/lease release.",
    }
    if parameter_warnings:
        result["parameter_warnings"] = list(parameter_warnings)
    for _time, _id, job, is_detached in selected[:limit]:
        rows = result["detached_jobs" if is_detached else "in_memory_jobs"]
        rows.append(_compact_job(job, is_detached))
        result["returned"] += 1
        result["omitted"] -= 1
        result["truncated"] = result["omitted"] > 0
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > JOB_LIST_MAX_BYTES:
            rows.pop()
            result["returned"] -= 1
            result["omitted"] += 1
            result["truncated"] = True
            break
    return result
