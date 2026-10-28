"""Configuration management for WorkFlowy MCP server."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .models.config import ServerConfig

# @beacon[
#   id=config-bootstrap@dotenv-gated-load,
#   role=dotenv bootstrap gated by WORKFLOWY_DEV_MODE / WORKFLOWY_LOAD_ENV,
#   slice_labels=nexus--config,nexus-loading-flow,
#   kind=span,
#   show_span=true,
#   comment=Development-only .env loading path that yields to normal environment-driven startup by default,
# ]
# Only load .env in development mode or if explicitly requested
# In production, MCP clients provide environment variables directly
if os.getenv("WORKFLOWY_DEV_MODE") or os.getenv("WORKFLOWY_LOAD_ENV"):
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        # python-dotenv is optional - only needed for development
        pass
# @beacon-close[
#   id=config-bootstrap@dotenv-gated-load,
# ]


_CONFIG_ENV_VAR = "WORKFLOWY_NEXUS_CONFIG"
_MCP_PROFILE_ENV_VAR = "WORKFLOWY_MCP_PROFILE"
_VALID_MCP_REGISTRATION_PROFILES = ("legacy", "consolidated")
# Import-time published-`params`-schema switch for the six family tools
# (anchor/node/tag/beacon/system/slice_group). ENVIRONMENT-ONLY -- there is
# deliberately no raw-config fallback key (ServerConfig is extra="forbid", so
# an undeclared key would crash load_server_config() at lifespan start).
# `object` (default) advertises params as a REQUIRED open object; `union`
# advertises today's anyOf[object,string,null] optional shape. Runtime
# acceptance is identical in both modes (the tool signatures never change).
_PARAMS_SCHEMA_ENV_VAR = "WORKFLOWY_MCP_PARAMS_SCHEMA"
_VALID_PARAMS_SCHEMA_MODES = ("object", "union")
# Test-runtime isolation seam (S02 round 4, recorded deviation): when set,
# get_runtime_base_dir() and get_cartographer_jobs_dir() resolve beneath
# this directory instead of the operator's per-user runtime location.  The
# test suite sets it session-wide so no test — including spawned
# subprocesses, which inherit the environment — can leak artifacts into the
# real runtime/governance directories.  Production never sets it.
_TEST_RUNTIME_ENV_VAR = "WORKFLOWY_MCP_TEST_RUNTIME_DIR"
_CONFIG_CACHE: ServerConfig | None = None
_CONFIG_META: dict[str, Any] = {
    "config_env_var": _CONFIG_ENV_VAR,
    "config_path": None,
    "source": "uninitialized",
    "search_paths": [],
}


def _repo_root() -> Path:
    """Return the package repo root (parent of src/)."""
    return Path(__file__).resolve().parents[2]


def _portable_state_home() -> Path:
    """Return an OS-appropriate per-user state/app-data home."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base).expanduser()
        return Path.home() / "AppData" / "Local"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"

    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state).expanduser()

    return Path.home() / ".local" / "state"


def get_runtime_base_dir() -> Path:
    """Return the portable per-user runtime base dir for Workflowy MCP artifacts.

    Test-runtime seam (S02 round 4, recorded deviation):
    ``WORKFLOWY_MCP_TEST_RUNTIME_DIR`` overrides the per-user location.  It
    exists so the test suite (including real spawned subprocesses, which
    inherit the environment) can never leak artifacts into the operator's
    real runtime directories.  Production never sets it.
    """
    override = os.environ.get(_TEST_RUNTIME_ENV_VAR)
    if override:
        base = Path(os.path.expandvars(override)).expanduser()
    else:
        base = _portable_state_home() / "workflowy-mcp"
    base.mkdir(parents=True, exist_ok=True)
    return base


def resolve_runtime_dir(configured_path: str | None, *fallback_parts: str) -> Path:
    """Return a configured directory or a portable runtime fallback."""
    raw = str(configured_path or "").strip()
    if raw:
        path = Path(os.path.expandvars(raw)).expanduser()
    else:
        path = get_runtime_base_dir().joinpath(*fallback_parts)

    path.mkdir(parents=True, exist_ok=True)
    return path


def get_runtime_subdir(*parts: str) -> Path:
    """Return a portable runtime subdirectory under the Workflowy MCP base dir."""
    return resolve_runtime_dir(None, *parts)


def get_nexus_runs_base_dir() -> Path:
    """Return the configured or portable default NEXUS runs base directory."""
    try:
        config = get_server_config()
        configured = str(config.paths.nexus_runs_base or "").strip()
    except Exception:
        configured = ""
    return resolve_runtime_dir(configured, "nexus_runs")


def get_cartographer_jobs_dir() -> Path:
    """Return the configured or portable default Cartographer jobs directory.

    The same test-runtime seam applies: when
    ``WORKFLOWY_MCP_TEST_RUNTIME_DIR`` is set it wins over configuration, so
    test client constructions can never reach the operator's real governance
    directory (S02 round 4 isolation).
    """
    override = os.environ.get(_TEST_RUNTIME_ENV_VAR)
    if override:
        return resolve_runtime_dir(
            str(Path(os.path.expandvars(override)).expanduser() / "cartographer_jobs")
        )
    try:
        config = get_server_config()
        configured = str(config.paths.cartographer_jobs_dir or "").strip()
    except Exception:
        configured = ""
    return resolve_runtime_dir(configured, "cartographer_jobs")


def get_cartographer_file_refresh_dir() -> Path:
    """Return the configured or portable default Cartographer file-refresh directory."""
    try:
        config = get_server_config()
        configured = str(config.paths.cartographer_file_refresh_dir or "").strip()
    except Exception:
        configured = ""
    return resolve_runtime_dir(configured, "cartographer_file_refresh")


# @beacon[
#   id=carto-jobstatus@terminal-vocabulary,
#   slice_labels=ra-carto-jobs,ra-workflowy-cache,
#   kind=span,
#   show_span=true,
#   comment=Single shared source of truth for CARTO job-status classification: CARTO_TERMINAL_STATUSES (the complete set of DONE states) and CARTO_GC_ELIGIBLE_STATUSES (the clean-exit subset the age-based GC may auto-delete), plus is_carto_job_terminal()/is_carto_job_active(). Consumed by _gc_carto_jobs AND _clear_carto_job_history so 'is this job still running?' is answered in ONE place. DENY-BY-DEFAULT: any unrecognized/blank/future status is NON-terminal => treated as active => never deleted; it simply surfaces in the widget until we knowingly add it here.,
# ]
# The COMPLETE set of terminal (finished) CARTO job statuses. A job whose
# status is NOT in this set is still in flight (queued / waiting / pending /
# running / cancelling) OR carries a novel/blank/future status we have not
# catalogued yet. Either way it must be treated as ACTIVE and never deleted.
#
# This is the deny-by-default safety primitive: deletion code asks
# ``is_carto_job_terminal(status)`` and only acts on an explicit, known-
# finished state. Adding a brand-new in-flight status in the worker therefore
# CANNOT accidentally make a long-running job deletable -- the unrecognized
# status is non-terminal by construction, the job survives, and it shows up in
# the widget status bar, which is exactly the signal that pulls us here to
# catalogue it deliberately.
CARTO_TERMINAL_STATUSES = frozenset({"completed", "completed_with_warnings", "failed", "cancelled"})

# The subset of terminal statuses eligible for AGE-BASED automatic GC. This is
# deliberately NARROWER than CARTO_TERMINAL_STATUSES: ``failed`` and
# ``completed_with_warnings`` are terminal but are intentionally EXEMPT from
# auto-cleanup so the user can return to an error/warning long after the run
# finished (they persist in the widget until manually cleared). Only the two
# "clean exit" states are auto-aged-out. The manual 'Clear job history' button
# is what clears the exempt error/warning states -- see
# _clear_carto_job_history, which deletes every terminal job regardless of this
# narrower subset.
CARTO_GC_ELIGIBLE_STATUSES = frozenset({"completed", "cancelled"})


def is_carto_job_terminal(status: Any) -> bool:
    """Return True iff ``status`` is a known FINISHED CARTO job status.

    Case-insensitive, whitespace-tolerant, and None-safe. Deny-by-default:
    anything not explicitly in :data:`CARTO_TERMINAL_STATUSES` (including an
    empty/None/unknown/future status) returns False -- i.e. is treated as
    still-active and therefore never eligible for deletion. This is the single
    predicate both the GC and the manual clear consult to answer "is this job
    still running?" so the answer can never drift between them.
    """
    return str(status or "").strip().lower() in CARTO_TERMINAL_STATUSES


def is_carto_job_active(status: Any) -> bool:
    """Return True iff ``status`` is NOT a known terminal status.

    The inverse of :func:`is_carto_job_terminal`; provided for call sites that
    read more naturally as "preserve while active". Same deny-by-default
    semantics: an unrecognized/blank status is considered active.
    """
    return not is_carto_job_terminal(status)


def _select_carto_status(disk_status: str, incoming_status: str) -> str:
    """Apply the exact Phase-D persisted-status precedence table."""

    if disk_status in {"cancelled", "failed"}:
        return disk_status
    if disk_status == "completed_with_warnings":
        return incoming_status if incoming_status in {"cancelled", "failed"} else disk_status
    if disk_status == "completed":
        return (
            incoming_status
            if incoming_status in {"cancelled", "failed", "completed_with_warnings"}
            else disk_status
        )
    if disk_status == "cancelling":
        return incoming_status if is_carto_job_terminal(incoming_status) else disk_status
    return incoming_status


# @beacon-close[
#   id=carto-jobstatus@terminal-vocabulary,
# ]


_CARTO_JOB_STATUS_WRITE_LOCK = threading.RLock()
_CARTO_CLEARED_JOB_IDS: set[str] = set()
_CARTO_CLEARED_JOB_PATHS: set[str] = set()
_CARTO_CLEARED_WRITE_LOGGED: set[str] = set()


def mark_carto_job_cleared(job_file, job_id) -> None:
    """Remember a deleted history identity under the status lock; no tombstone."""
    with _CARTO_JOB_STATUS_WRITE_LOCK:
        _CARTO_CLEARED_JOB_IDS.add(str(job_id))
        _CARTO_CLEARED_JOB_PATHS.add(os.path.normcase(os.path.abspath(os.fspath(job_file))))


def is_carto_job_cleared(job_file, job_id=None) -> bool:
    """Late presentation writes must not recreate a cleared record or folder."""
    with _CARTO_JOB_STATUS_WRITE_LOCK:
        path = os.path.normcase(os.path.abspath(os.fspath(job_file)))
        cleared = path in _CARTO_CLEARED_JOB_PATHS or str(job_id) in _CARTO_CLEARED_JOB_IDS
        if cleared and path not in _CARTO_CLEARED_WRITE_LOGGED:
            _CARTO_CLEARED_WRITE_LOGGED.add(path)
            logging.getLogger(__name__).info("Skipping late write for cleared Cartographer job %s (%s)", job_id, job_file)
        return cleared

# HEAD-FIRST JOB RECORDS (2026-10-26, job-list fix [ap:FK5O8K]). A folder
# refresh keeps per-file evidence in result_summary (tens of MB for ~500
# files), and the widget's once-a-second job list used to decode every record
# in full. Every write now lays the record out summary-first: the layout
# marker, then status, error, progress and warnings, then the remaining small
# top-level fields, and result_summary LAST with its per-file lists at its own
# end. A reader that needs only the summary decodes the head and stops at the
# first tail key (read_carto_job_record_head). Values never change; only key
# order does, so every json.load reader sees the same record.
CARTO_JOB_RECORD_LAYOUT = "summary-head/1"
_CARTO_JOB_RECORD_HEAD_KEYS = (
    "id", "type", "mode", "root_uuid", "status", "error", "error_kind",
    "created_at", "updated_at", "progress", "warning_count", "warnings",
)
# Per-file / per-node evidence inside result_summary, written after every
# aggregate (counts, errors, summary_line, status) and never read by the head.
CARTO_RESULT_SUMMARY_TAIL_KEYS = (
    "warnings", "events", "files_changed", "present_paths",
    "findings", "phase8", "node_outcomes", "files",
)
# A head larger than this (a pathological error or warning list) is decoded
# whole instead; correctness never depends on the window.
CARTO_JOB_HEAD_MAX_CHARS = 8 * 1024 * 1024
_CARTO_JOB_HEAD_FIRST_WINDOW = 64 * 1024


# @beacon[
#   id=carto-jobstatus@record-layout,
#   role=carto_job_record_layout(payload),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=Orders old records summary-first with their compact evidence tail and explicitly marked /2 records as all-head summaries with evidence and in_flight before result_summary; unknown keys and values stay untouched.,
# ]
def carto_job_record_layout(payload: dict[str, Any], *, preserve_v2: bool = False) -> dict[str, Any]:
    """Order an explicit /2 record, preserving /2 on a startup rewrite too.

    An unmarked/legacy record still becomes /1; G3 adds the writer capability,
    not a production caller. ``preserve_v2`` retains Q1's existing-/2 rewrite.
    """
    preserve_v2 = preserve_v2 or payload.get("record_layout") == "summary-head/2"
    layout = "summary-head/2" if preserve_v2 else CARTO_JOB_RECORD_LAYOUT
    record: dict[str, Any] = {"record_layout": layout}
    head_keys = (_CARTO_JOB_RECORD_HEAD_KEYS if not preserve_v2 else (
        "id", "type", "mode", "root_uuid", "status", "outcome", "has_errors", "has_warnings",
        "has_findings", "error", "error_kind", "created_at", "updated_at", "progress", "warning_count", "warnings"))
    for key in head_keys:
        if key in payload:
            record[key] = payload[key]
    for key, value in payload.items():
        if key not in record and key != "result_summary" and (not preserve_v2 or key not in {"evidence", "in_flight"}):
            record[key] = value
    if preserve_v2:
        for key in ("evidence", "in_flight"):
            if key in payload:
                record[key] = payload[key]
    if "result_summary" in payload:
        summary = payload["result_summary"]
        if isinstance(summary, dict) and not preserve_v2:
            ordered = {key: value for key, value in summary.items()
                       if key not in CARTO_RESULT_SUMMARY_TAIL_KEYS}
            ordered.update((key, summary[key]) for key in CARTO_RESULT_SUMMARY_TAIL_KEYS if key in summary)
            summary = ordered
        record["result_summary"] = summary
    return record


def _without_carto_result_tail(record: Any) -> Any:
    """Strip old-layout evidence; a /2 record is already entirely summary."""
    if (isinstance(record, dict) and record.get("record_layout") != "summary-head/2"
            and isinstance(record.get("result_summary"), dict)):
        record = dict(record)
        record["result_summary"] = {key: value for key, value in record["result_summary"].items()
                                    if key not in CARTO_RESULT_SUMMARY_TAIL_KEYS}
    return record


def _decode_carto_job_head(text: str, at_eof: bool) -> dict[str, Any] | bool | None:
    """Decode a head-first record's head from ``text``.

    Returns the head (every top-level key, result_summary without its tail
    keys), ``False`` when the text is not a head-first record (decode it
    whole), or ``None`` when the head runs past ``text`` (read more).
    """
    decoder = json.JSONDecoder()
    skip = json.decoder.WHITESPACE.match
    # Only the layout marker decides "not head-first"; after it, any decode
    # trouble is a window that ends mid-value (read more), or at end of file
    # a malformed record (decode it whole, which raises like json.load).
    incomplete = False if at_eof else None
    try:
        index = skip(text, 0).end()
        if text[index] != "{":
            return False
        index = skip(text, index + 1).end()
        if text[index] != '"':
            return False
        key, index = decoder.raw_decode(text, index)
        index = skip(text, index).end()
        if key != "record_layout" or text[index] != ":":
            return False
        marker, index = decoder.raw_decode(text, skip(text, index + 1).end())
        if marker not in {CARTO_JOB_RECORD_LAYOUT, "summary-head/2"}:
            return False
    except (IndexError, json.JSONDecodeError):
        return incomplete
    if marker == "summary-head/2":
        # /2 is ALL head. Unlike /1 there is no evidence tail to stop before,
        # and an empty result_summary cannot hide later unknown/evidence keys.
        # Decode once, at EOF; a malformed /2 raises directly rather than
        # performing a second whole decode through the old-layout fallback.
        return json.loads(text) if at_eof else None
    head: dict[str, Any] = {"record_layout": marker}
    summary: dict[str, Any] | None = None
    try:
        while True:
            index = skip(text, index).end()
            if text[index] == "}":
                if summary is not None:
                    head["result_summary"] = summary
                return head
            if text[index] != ",":
                return incomplete
            key, index = decoder.raw_decode(text, skip(text, index + 1).end())
            index = skip(text, index).end()
            if not isinstance(key, str) or text[index] != ":":
                return incomplete
            index = skip(text, index + 1).end()
            if summary is not None and marker == CARTO_JOB_RECORD_LAYOUT and key in CARTO_RESULT_SUMMARY_TAIL_KEYS:
                head["result_summary"] = summary
                return head
            if summary is None and key == "result_summary" and text[index] == "{":
                # result_summary is the last top-level key: its aggregates
                # are read, its per-file evidence never is.
                summary = {}
                index = skip(text, index + 1).end()
                if text[index] == "}":
                    head["result_summary"] = summary
                    return head
                key, index = decoder.raw_decode(text, index)
                index = skip(text, index).end()
                if not isinstance(key, str) or text[index] != ":":
                    return incomplete
                index = skip(text, index + 1).end()
                if marker == CARTO_JOB_RECORD_LAYOUT and key in CARTO_RESULT_SUMMARY_TAIL_KEYS:
                    head["result_summary"] = summary
                    return head
            value, index = decoder.raw_decode(text, index)
            (summary if summary is not None else head)[key] = value
    except (IndexError, json.JSONDecodeError):
        return incomplete


# @beacon[
#   id=carto-jobstatus@read-record-head,
#   role=read_carto_job_record_head(job_file),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=Reads all three layouts without old-layout evidence: /1 stops before its tail and all-head /2 decodes through EOF including fields after an empty summary. Legacy order or an oversized head decodes whole and strips only old-layout tails; unreadable records raise like json.load.,
# ]
def read_carto_job_record_head(job_file: str | os.PathLike[str]) -> Any:
    """Read a /1 head, all /2 keys through EOF, or a stripped legacy record."""
    with Path(job_file).open("r", encoding="utf-8") as handle:
        window = _CARTO_JOB_HEAD_FIRST_WINDOW
        text = handle.read(window)
        while True:
            at_eof = len(text) < window
            head = _decode_carto_job_head(text, at_eof)
            if isinstance(head, dict):
                return head
            if head is False or at_eof or window >= CARTO_JOB_HEAD_MAX_CHARS:
                break
            window = min(window * 4, CARTO_JOB_HEAD_MAX_CHARS)
            text += handle.read(window - len(text))
        text += handle.read()
    return _without_carto_result_tail(json.loads(text))


# @beacon[
#   id=carto-jobstatus@merge_write,
#   slice_labels=ra-carto-jobs,ra-workflowy-cache,
#   kind=ast,
#   comment=The one status funnel keeps sticky precedence under its existing RLock, suppresses cleared identities and caps every /2 warning display at 200 with its exact total. Explicit /2 records stay all-head, old records keep /1 compact tails, and replacement uses governance's one retry loop; never reads evidence.,
# ]
def merge_write_carto_job_status(job_file: str | os.PathLike[str], payload: dict[str, Any], *, _record_out=None) -> bool:
    """Best-effort atomic CARTO status merge under the existing data-writer lock.

    Persisted precedence is exact: failed/cancelled are sticky; warnings beat
    completed/non-terminal writes; completed beats non-terminal writes; and
    cancelling beats every non-terminal write. Loop-owned control supplies
    current task truth independently of this presentation transaction.
    Reads only head keys for precedence/warnings. Old evidence stays in its
    compact tail; /2 evidence belongs to the job's append-only streams. Every
    /2 write caps the display, including the first; warning_count is the exact
    warnings_total, never the capped or deduplicated display length (B5).
    Only a replace collision enters governance's shared retry.
    Returns False instead of raising, logging a failed write.
    The job-owned writer may receive the selected small record in _record_out,
    avoiding a third confirming read or a second status/warning implementation.
    """
    with _CARTO_JOB_STATUS_WRITE_LOCK:
        job_path = Path(job_file)
        if is_carto_job_cleared(job_path, payload.get("id")):
            return False

        # Re-read current on-disk state (best-effort; absent/corrupt => no sticky).
        on_disk: dict[str, Any] | None = None
        try:
            loaded = read_carto_job_record_head(job_path)
            if isinstance(loaded, dict):
                on_disk = loaded
        except Exception:
            on_disk = None

        incoming_status = str(payload.get("status") or "").strip().lower()

        if on_disk is not None:
            disk_status = str(on_disk.get("status") or "").strip().lower()
            selected_status = _select_carto_status(disk_status, incoming_status)
            if selected_status != incoming_status:
                payload = dict(payload)
                payload["status"] = on_disk.get("status")
                incoming_status = selected_status

            # WARNING ACCUMULATION (Dan, 2026): NEVER let a later write DROP
            # warnings that already exist on disk. A caller that does not know
            # about warnings (its payload has no 'warnings' key, or an empty one)
            # must not erase the persisted list -- otherwise a fresh single warning
            # from one phase could be overwritten by a warning-blind finalize from
            # another. We MERGE the on-disk warnings with the incoming ones,
            # de-duplicating structurally-identical entries and capping the total
            # so a pathological run cannot bloat the JSON. This is what makes
            # multiple warnings CONCATENATE (persist) rather than overwrite.
            disk_warnings = on_disk.get("warnings")
            if (isinstance(disk_warnings, list) and disk_warnings
                    and on_disk.get("record_layout") != "summary-head/2"):
                incoming_warnings = payload.get("warnings")
                if not isinstance(incoming_warnings, list):
                    incoming_warnings = []
                merged: list[Any] = []
                seen_keys: set[str] = set()
                for w in list(disk_warnings) + list(incoming_warnings):
                    try:
                        key = json.dumps(w, sort_keys=True, default=str)
                    except Exception:
                        key = str(w)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    merged.append(w)
                    if len(merged) >= 200:
                        break
                payload = dict(payload)
                payload["warnings"] = merged
                payload["warning_count"] = len(merged)

        preserve_v2 = bool(payload.get("record_layout") == "summary-head/2" or
                           (on_disk is not None and on_disk.get("record_layout") == "summary-head/2"))
        if preserve_v2:
            payload = dict(payload)
            incoming = payload.get("warnings")
            incoming = incoming if isinstance(incoming, list) else []
            previous = (on_disk or {}).get("warnings")
            previous = previous if isinstance(previous, list) else []
            display, seen = [], set()
            for warning in previous + incoming:
                key = json.dumps(warning, sort_keys=True, default=str)
                if key not in seen:
                    seen.add(key)
                    if len(display) < 200:
                        display.append(warning)
            summary = payload.get("result_summary")
            summary = summary if isinstance(summary, dict) else {}
            total = summary.get("warnings_total")
            if total is None:
                total = max(int(payload.get("warning_count") or 0),
                            int((on_disk or {}).get("warning_count") or 0), len(seen))
            payload.update(warnings=display, warning_count=total)
            from .carto_job_record import carto_job_outcome
            payload.update(carto_job_outcome(payload))

        # Atomic write: temp file in the same dir + os.replace so readers never
        # observe a partially-written file. Head-first, so the widget's job
        # list reads the summary without decoding the per-file evidence.
        try:
            job_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = job_path.with_name(f"{job_path.name}.{os.getpid()}.{__import__('uuid').uuid4().hex}.tmp")
            with tmp_path.open("w", encoding="utf-8") as jf:
                record = carto_job_record_layout(payload, preserve_v2=preserve_v2)
                if _record_out is not None:
                    _record_out.update(record)
                summary = record.get("result_summary")
                tail = ({key: value for key, value in summary.items() if key in CARTO_RESULT_SUMMARY_TAIL_KEYS}
                        if isinstance(summary, dict) and not preserve_v2 else {})
                if tail:
                    head_summary = {key: value for key, value in summary.items() if key not in tail}
                    record["result_summary"] = head_summary
                    # Remove only the last two object closers (summary, record).
                    # dumps with no indent uses CPython's C encoder for the tail;
                    # dump, even without indent, would stream in Python instead.
                    head = json.dumps(record, indent=2).rsplit("}", 2)[0].rstrip()
                    jf.write(head + ("," if head_summary else "") + "\n    ")
                    jf.write(json.dumps(tail, separators=(",", ":"))[1:-1])
                    jf.write("\n  }\n}")
                else:
                    jf.write(json.dumps(record, indent=2))
            # Local import avoids governance's module-level config dependency.
            # Reuse only replacement: keep this writer's temp and no-fsync IO.
            from .carto_job_governance import _replace_with_retry
            _replace_with_retry(tmp_path, job_path)

            return True
        except Exception as exc:
            logging.getLogger(__name__).warning("Could not persist Cartographer job status to %s: %s", job_path, exc)
            try:
                if "tmp_path" in locals() and tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            return False


def get_windsurf_exe_candidates() -> list[str]:
    """Return configured-first, generic fallback WindSurf executable candidates."""
    configured: list[str] = []
    try:
        config = get_server_config()
        configured = [
            str(Path(os.path.expandvars(p)).expanduser())
            for p in (config.paths.windsurf_exe_candidates or [])
            if str(p or "").strip()
        ]
    except Exception:
        configured = []

    local_appdata = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("PROGRAMFILES")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)")
    home = Path.home()

    generic_candidates = [
        str(home / "AppData" / "Local" / "Programs" / "Windsurf" / "Windsurf.exe"),
        (
            str(Path(local_appdata) / "Programs" / "Windsurf" / "Windsurf.exe")
            if local_appdata
            else ""
        ),
        str(Path(program_files) / "Windsurf" / "Windsurf.exe") if program_files else "",
        str(Path(program_files_x86) / "Windsurf" / "Windsurf.exe") if program_files_x86 else "",
        r"C:\Program Files\Windsurf\Windsurf.exe",
    ]

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in configured + generic_candidates:
        raw = str(candidate or "").strip()
        if not raw:
            continue
        key = os.path.normcase(raw)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(raw)
    return deduped


# @beacon[
#   id=config-bootstrap@candidate-config-paths,
#   role=_candidate_config_paths,
#   slice_labels=nexus--config,nexus-loading-flow,
#   kind=ast,
#   comment=Build default search order for workflowy_nexus config files when WORKFLOWY_NEXUS_CONFIG is not set,
# ]
def _candidate_config_paths() -> list[Path]:
    """Return default config file candidates in priority order."""
    cwd = Path.cwd()
    repo_root = _repo_root()

    candidates = [
        cwd / "workflowy_nexus.config.local.json",
        cwd / "workflowy_nexus.config.json",
        repo_root / "workflowy_nexus.config.local.json",
        repo_root / "workflowy_nexus.config.json",
    ]

    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


# @beacon[
#   id=config-bootstrap@resolve-config-path,
#   role=resolve_server_config_path,
#   slice_labels=nexus--config,nexus-loading-flow,
#   kind=ast,
#   comment=Resolve explicit WORKFLOWY_NEXUS_CONFIG path or first discovered default config candidate,
# ]
def resolve_server_config_path() -> Path | None:
    """Resolve the runtime config file path, if any."""
    explicit = (os.getenv(_CONFIG_ENV_VAR) or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()

    for candidate in _candidate_config_paths():
        if candidate.is_file():
            return candidate.resolve()

    return None


# @beacon[
#   id=config-bootstrap@load-json-config-file,
#   role=_load_json_config_file,
#   slice_labels=nexus--config,nexus-loading-flow,
#   kind=ast,
#   comment=Read and validate the external workflowy_nexus JSON config file before model validation,
# ]
def _load_json_config_file(path: Path) -> dict[str, Any]:
    """Load a JSON config file and require a top-level object."""
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"Config file must contain a top-level JSON object: {path}")
    return payload


# @beacon[
#   id=mcp-api@discover-raw-config,
#   role=_discover_raw_config,
#   slice_labels=nexus--config,nexus-loading-flow,nexus--mcp-api,
#   kind=ast,
#   comment=One import-safe raw JSON discovery authority shared by full ServerConfig loading and the narrow MCP registration-profile bootstrap,
# ]
def _discover_raw_config() -> tuple[dict[str, Any], dict[str, Any]]:
    """Discover and load raw JSON config without model validation or caching."""
    explicit = (os.getenv(_CONFIG_ENV_VAR) or "").strip()
    config_path = resolve_server_config_path()
    if explicit and (config_path is None or not config_path.is_file()):
        raise FileNotFoundError(f"{_CONFIG_ENV_VAR} points to a missing config file: {config_path}")

    config_data = (
        _load_json_config_file(config_path)
        if config_path is not None and config_path.is_file()
        else {}
    )
    source = (
        "explicit-env-path"
        if explicit
        else "discovered-default-path" if config_path is not None else "env-and-defaults-only"
    )
    search_candidates = [Path(explicit)] if explicit else _candidate_config_paths()
    meta = {
        "config_env_var": _CONFIG_ENV_VAR,
        "config_path": str(config_path) if config_path else None,
        "source": source,
        "search_paths": [
            str(path.expanduser().resolve() if path.exists() else path.expanduser())
            for path in search_candidates
        ],
    }
    return config_data, meta


# @beacon[
#   id=mcp-api@normalize-registration-profile,
#   role=normalize_mcp_registration_profile,
#   slice_labels=nexus--config,nexus-loading-flow,nexus--mcp-api,
#   kind=ast,
#   comment=Normalize and fail-loud validate the two supported import-time MCP registration profiles,
# ]
def normalize_mcp_registration_profile(value: Any) -> str:
    """Return a supported normalized registration profile or fail loudly."""
    normalized = str(value).strip().lower()
    if normalized not in _VALID_MCP_REGISTRATION_PROFILES:
        valid = ", ".join(_VALID_MCP_REGISTRATION_PROFILES)
        raise ValueError(f"Invalid MCP registration profile {value!r}; valid values: {valid}")
    return normalized


# @beacon[
#   id=mcp-api@resolve-registration-profile,
#   role=resolve_mcp_registration_profile,
#   slice_labels=nexus--config,nexus-loading-flow,nexus--mcp-api,
#   kind=ast,
#   comment=Credential-free import-time profile bootstrap: non-blank environment wins over raw mcpProfile config, then consolidated default; a blank/whitespace env value is treated as unset; never instantiates ServerConfig or touches its cache,
# ]
def resolve_mcp_registration_profile() -> str:
    """Resolve the import-time MCP profile without validating full config."""
    env_profile = os.environ.get(_MCP_PROFILE_ENV_VAR)
    if env_profile is not None and env_profile.strip():
        raw_profile: Any = env_profile
    else:
        raw_config, _meta = _discover_raw_config()
        raw_profile = raw_config.get("mcpProfile", "consolidated")
    return normalize_mcp_registration_profile(raw_profile)


# @beacon[
#   id=mcp-api@normalize-params-schema-mode,
#   role=normalize_params_schema_mode,
#   slice_labels=nexus--config,nexus-loading-flow,nexus--mcp-api,
#   kind=ast,
#   comment=Normalize (strip+lower) and fail-loud validate the two published-params-schema modes (object | union) for the six family tools,
# ]
def normalize_params_schema_mode(value: Any) -> str:
    """Return a supported normalized params-schema mode or fail loudly."""
    normalized = str(value).strip().lower()
    if normalized not in _VALID_PARAMS_SCHEMA_MODES:
        valid = ", ".join(_VALID_PARAMS_SCHEMA_MODES)
        raise ValueError(f"Invalid MCP params schema mode {value!r}; valid values: {valid}")
    return normalized


# @beacon[
#   id=mcp-api@resolve-params-schema-mode,
#   role=resolve_params_schema_mode,
#   slice_labels=nexus--config,nexus-loading-flow,nexus--mcp-api,
#   kind=ast,
#   comment=Credential-free import-time bootstrap of the published-params-schema mode: a non-blank WORKFLOWY_MCP_PARAMS_SCHEMA wins (invalid raises at import); blank/whitespace/absent is unset and yields the `object` default; environment-only by decision -- no config-file key,
# ]
def resolve_params_schema_mode() -> str:
    """Resolve the import-time published-params-schema mode (env-only)."""
    env_mode = os.environ.get(_PARAMS_SCHEMA_ENV_VAR)
    if env_mode is not None and env_mode.strip():
        return normalize_params_schema_mode(env_mode)
    return "object"


def _parse_env_bool(raw: str) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _set_nested_value(container: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cur = container
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


# @beacon[
#   id=config-bootstrap@merge-env-overrides,
#   role=_merge_env_overrides,
#   slice_labels=nexus--config,nexus-loading-flow,
#   kind=ast,
#   comment=Apply nonblank environment precedence over JSON/default values including the boolean WORKFLOWY_V1_ONLY placement setting; blank remains unset.,
# ]
def _merge_env_overrides(config_data: dict[str, Any]) -> dict[str, Any]:
    """Apply nonblank environment overrides, including v1_only, over file/defaults."""
    merged: dict[str, Any] = dict(config_data or {})

    env_map: list[tuple[str, tuple[str, ...], Any]] = [
        ("WORKFLOWY_API_KEY", ("WORKFLOWY_API_KEY",), str),
        ("WORKFLOWY_API_URL", ("WORKFLOWY_API_URL",), str),
        ("WORKFLOWY_TIMEOUT", ("WORKFLOWY_TIMEOUT",), int),
        ("WORKFLOWY_MAX_RETRIES", ("WORKFLOWY_MAX_RETRIES",), int),
        ("WORKFLOWY_V1_ONLY", ("v1_only",), _parse_env_bool),
        ("DEBUG", ("DEBUG",), _parse_env_bool),
        ("LOG_LEVEL", ("LOG_LEVEL",), str),
        ("WORKFLOWY_MCP_PROFILE", ("mcpProfile",), str),
        ("NEXUS_RUNS_BASE", ("paths", "nexusRunsBase"), str),
        ("CARTOGRAPHER_JOBS_DIR", ("paths", "cartographerJobsDir"), str),
        ("CARTOGRAPHER_FILE_REFRESH_DIR", ("paths", "cartographerFileRefreshDir"), str),
        ("WORKFLOWY_WS_HOST", ("websocket", "host"), str),
        ("WORKFLOWY_WS_PORT", ("websocket", "port"), int),
        ("WORKFLOWY_WS_RECONNECT_DELAY_MS", ("timing", "websocketReconnectDelayMs"), int),
        ("WORKFLOWY_CARTO_JOBS_POLL_INTERVAL_MS", ("timing", "cartoJobsPollIntervalMs"), int),
        ("WORKFLOWY_CARTO_JOB_MAX_AGE_SECONDS", ("retention", "cartoJobMaxAgeSeconds"), int),
        ("WINDSURF_EXE", ("paths", "windsurfExeCandidates"), lambda raw: [str(raw)]),
    ]

    for env_name, field_path, parser in env_map:
        if env_name not in os.environ:
            continue
        raw_val = os.environ.get(env_name)
        if raw_val is None or not str(raw_val).strip():
            # A present-but-blank environment variable is treated as UNSET
            # rather than overriding file/default values with an empty string
            # (which would fail loudly downstream, e.g. mcpProfile/int fields).
            continue
        value = parser(raw_val)
        _set_nested_value(merged, field_path, value)

    return merged


# @beacon[
#   id=config-bootstrap@load-server-config,
#   role=load_server_config,
#   slice_labels=nexus--config,nexus-loading-flow,
#   kind=ast,
#   comment=Central loader implementing config-file discovery, environment precedence, validation, and cached resolved config state,
# ]
def load_server_config(*, reload: bool = False) -> ServerConfig:
    """Load and validate the resolved server configuration."""
    global _CONFIG_CACHE, _CONFIG_META

    if _CONFIG_CACHE is not None and not reload:
        return _CONFIG_CACHE

    config_data, config_meta = _discover_raw_config()
    merged = _merge_env_overrides(config_data)
    config = ServerConfig.model_validate(merged)

    _CONFIG_CACHE = config
    _CONFIG_META = config_meta
    return config


# @beacon[
#   id=config-bootstrap@get-server-config,
#   role=get_server_config,
#   slice_labels=nexus--config,nexus-loading-flow,
#   kind=ast,
#   comment=Cached access wrapper for the resolved ServerConfig used by server and worker bootstrap flows,
# ]
def get_server_config(*, reload: bool = False) -> ServerConfig:
    """Return the cached resolved server configuration."""
    return load_server_config(reload=reload)


def get_server_config_meta() -> dict[str, Any]:
    """Return metadata about how the runtime configuration was resolved."""
    return dict(_CONFIG_META)


# @beacon[
#   id=config-bootstrap@setup-logging,
#   role=setup_logging,
#   slice_labels=nexus--config,nexus-loading-flow,
#   kind=ast,
#   comment=Startup logging helper that lazily loads the resolved config when no config is supplied,
# ]
def setup_logging(config: ServerConfig | None = None) -> None:
    """Setup logging configuration.

    Args:
        config: Optional resolved server configuration. If not provided,
            will attempt to load from the standard loader.
    """
    if config is None:
        try:
            config = get_server_config()
        except Exception:
            config = None

    if config is None:
        log_level = os.getenv("LOG_LEVEL", "INFO")
        log_file = os.getenv("LOG_FILE")
    else:
        log_level = config.log_level
        log_file = os.getenv("LOG_FILE")

    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }

    if isinstance(log_level, str):
        log_level = level_map.get(log_level.upper(), logging.INFO)  # type: ignore[assignment]

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if log_file:
        try:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10 * 1024 * 1024, backupCount=5
            )
            file_handler.setLevel(log_level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except Exception as e:
            logging.warning(f"Failed to setup file logging: {str(e)}")

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logging.info(f"Logging configured at level: {logging.getLevelName(log_level)}")
