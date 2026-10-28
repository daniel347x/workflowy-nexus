"""F12+3 Markdown generation inside one daemon-owned Cartographer task (N07).

The launcher owns PRE/GESTURE freshness; the common runner owns the current
cache boundary and whole-life FILE grant. Phases2/5 resolve and export;6/7
observe/render/prepare all beacon and EOF bytes before one atomic publish;8 awaits a
disk-to-Workflowy refresh in the SAME parent task/grant. CPU/disk helpers take
only owned data or immutable view references, never a client in a worker.
Restart stops at phase boundaries; user cancellation never cuts a send.
Threads are not native-parser crash isolation (see carto_job_runtime).
The admission-owned /2 writer streams publication before phase8 and retains
only the primary file's findings until that same file's phase8 handoff.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from beacon_source import read_source_snapshot
from beacon_edits import commit_beacon_edit_plan

from .carto_job_runtime import run_carto_cpu
from .carto_job_record import mark_carto_reconcile_children, record_for
from .coherence.errors import CoherenceBarrierActiveError


# @beacon[
#   id=auto-beacon@_now_iso-uk7j,
#   role=_now_iso,
#   slice_labels=carto-rev-markdown-full,
#   kind=ast,
# ]
def _now_iso() -> str:
    return datetime.utcnow().isoformat()


# @beacon[
#   id=auto-beacon@_read_job_payload-ty36,
#   role=_read_job_payload,
#   slice_labels=carto-rev-markdown-full,
#   kind=ast,
#   comment=Reads one explicit legacy or daemon job artifact; daemon callers invoke it only through the joined data phase.,
# ]
def _read_job_payload(job_file: str) -> dict[str, Any]:
    """Read the CARTO job JSON from disk."""
    with open(job_file, "r", encoding="utf-8") as jf:
        return json.load(jf)


# @beacon[
#   id=auto-beacon@_write_job_payload-sdhd,
#   role=_write_job_payload,
#   slice_labels=carto-rev-markdown-full,
#   kind=ast,
#   comment=Publishes owned Markdown head changes through the job's sole registered writer and status funnel; any write or import failure becomes a record warning and the fallback uses that same writer, never a plain job-file write.,
# ]
def _write_job_payload(job_file: str, payload: dict[str, Any]) -> None:
    """Use the one /2 writer on both best-effort paths, including import faults.

    Admission registers production jobs; explicit non-job callers get that same
    writer, never an old-layout bypass. A failed funnel leaves its warning in
    complete run evidence and the bounded display for this or the next write.
    """
    writer = record_for(job_file)
    if writer._cleared():
        return
    try:
        if writer.update_head(**payload):
            return
        error = "Could not persist Markdown job record"
    except Exception as failure:
        error = f"{type(failure).__name__}: {failure}"
    warning = {"type": "markdown_job_record_write_failed", "error": error}
    logging.getLogger(__name__).warning("Markdown job record %s: %s", job_file, error)
    try:
        writer.append_item("run_warnings", warning)
        writer.update_head(**{**payload, "warnings": writer._head.get("warnings", []),
                              "warning_count": int(writer._head.get("warning_count") or 0) + 1})
    except Exception as failure:
        # The same writer still owns the staged head and warning. A later
        # ordinary flush/finalize uses the same funnel; no new retry cadence.
        logging.getLogger(__name__).warning("Markdown job record warning pending for %s: %s", job_file, failure)


# @beacon[
#   id=auto-beacon@_append_log-8g1y,
#   role=_append_log,
#   slice_labels=carto-rev-markdown-full,
#   kind=ast,
#   comment=Appends one explicit-path best-effort timestamped log line unless its job was cleared; daemon pipeline calls join the data writer and never send a loop callback into a worker.,
# ]
def _append_log(log_file: str | None, message: str) -> None:
    """Append a best-effort timestamped line; never recreate a cleared job's log."""
    if not log_file:
        return
    try:
        from .config import is_carto_job_cleared
        if is_carto_job_cleared(Path(log_file).with_suffix(".json")):
            return
        with open(log_file, "a", encoding="utf-8") as lf:
            lf.write(f"[{_now_iso()}] {message}\n")
    except Exception:
        pass


# @beacon[
#   id=auto-beacon@_set_phase-ci16,
#   role=_set_phase,
#   slice_labels=carto-rev-markdown-full,
#   kind=ast,
#   comment=Updates only the owning task phase and timestamp before joining status then log artifacts in their original order.,
# ]
async def _set_phase(
    job_file: str,
    job: dict[str, Any],
    log_file: str | None,
    phase: str,
    message: str,
) -> None:
    """Update loop-owned phase state, then join its ordered artifact writes."""
    progress = job.setdefault("progress", {})
    progress["current_phase"] = phase
    job["updated_at"] = _now_iso()
    await _persist_markdown_job(job_file, job)
    await _log_markdown_job(log_file, f"[phase={phase}] {message}")


def _markdown_job_snapshot(job):
    """Merge this task's acknowledged FILE counts, then detach fixed-shape values.

    Inline phase8 flushes progress through the same job file. The pipeline's
    earlier in-memory phase record must not overwrite those real counts with
    its starting zeros when it next persists success or failure.
    """
    from .carto_job_runtime import current_carto_job_context
    context = current_carto_job_context()
    if context is not None:
        progress = job.setdefault("progress", {})
        for key in ("nodes_created", "nodes_updated", "nodes_moved", "nodes_deleted"):
            if key in context.progress:
                progress[key] = context.progress[key]
    return {**job, "progress": dict(job.get("progress") or {}),
            "result_summary": dict(job.get("result_summary") or {})}


# @beacon[
#   id=carto-runtime@markdown-job-artifact,
#   slice_labels=ra-carto-jobs,carto-rev-markdown-full,
#   kind=ast,
#   comment=Reads the small Markdown record or publishes owned head changes through its registered writer, and appends best-effort logs off-loop; every disk future is joined and cleared jobs stay cleared.,
# ]
def _markdown_job_artifact(operation, path, value=None):
    """Data-only record-writer/log work; never a task, client or callback."""
    if operation == "read":
        return _read_job_payload(path)
    if operation == "status":
        return _write_job_payload(path, value)
    if operation == "log":
        return _append_log(path, value)
    raise ValueError(f"Unknown Markdown artifact operation: {operation}")


async def _persist_markdown_job(job_file, job):
    """Join only an owned small-head snapshot; complete rows use the handoff."""
    snapshot = _markdown_job_snapshot(job)
    await run_carto_cpu("markdown-artifact:status", _markdown_job_artifact,
                        "status", job_file, snapshot)


async def _log_markdown_job(log_file, message):
    await run_carto_cpu("markdown-artifact:log", _markdown_job_artifact,
                        "log", log_file, message)


def _markdown_warning_artifact(operation, log_file, file_path, warnings, dedup=None):
    """Own warning-list traversal/formatting and ordered best-effort disk output."""
    warnings = list(warnings)
    if operation == "deduplicate":
        try:
            if dedup.get("error"):
                _append_log(log_file, f"WARNING: duplicate-beacon-id disambiguation could not run: {dedup.get('error')}")
                warnings.append({
                    "type": "duplicate_beacon_id_disambiguation_failed",
                    "name": os.path.basename(file_path), "error": str(dedup.get("error")),
                    "md_path": None, "operation": None,
                })
            elif dedup.get("changed"):
                renamed_count = int(dedup.get("renamed_count") or 0)
                dup_groups = dedup.get("duplicates") or []
                _append_log(log_file,
                    f"WARNING: repaired {renamed_count} duplicate beacon id(s) "
                    f"in {os.path.basename(file_path)} across {len(dup_groups)} "
                    f"id group(s) (rewrote all-but-first with a --dupN postfix). "
                    f"This file was carrying an intra-file duplicate beacon id "
                    f"that would make F12+1 non-idempotent; the duplicates have "
                    f"been disambiguated on disk.")
                for group in dup_groups:
                    try:
                        renamed_to = ", ".join(str(row.get("to")) for row in (group.get("renamed") or []))
                        _append_log(log_file,
                            f"  duplicate-id repair: id={group.get('beacon_id')!r} "
                            f"appeared {group.get('count')}x -> renamed extras to: {renamed_to}")
                    except Exception:
                        pass
                for group in dup_groups:
                    warnings.append({
                        "type": "duplicate_beacon_id_repaired", "name": os.path.basename(file_path),
                        "error": (
                            f"Beacon id {group.get('beacon_id')!r} appeared "
                            f"{group.get('count')} times in the regenerated file; "
                            f"disambiguated the extras with a --dupN postfix so "
                            f"F12+1 stays idempotent. NOTE: the Workflowy nodes "
                            f"still share this id and will re-introduce the "
                            f"duplicate on the next F12+3 until their notes are "
                            f"given distinct ids."
                        ),
                        "beacon_id": group.get("beacon_id"), "renamed": group.get("renamed"),
                        "md_path": None, "operation": "disambiguated_duplicate_beacon_id",
                    })
        except Exception as exc:
            _append_log(log_file, f"WARNING: duplicate-beacon-id disambiguation raised unexpectedly: {exc}")
        return warnings
    if operation == "summary":
        _append_log(log_file,
            f"WARNING: {len(warnings)} phase-7/7.5 warning(s) "
            f"(per-beacon skips and/or duplicate-beacon-id repairs). "
            f"Run will be marked completed_with_warnings.")
        for warning in warnings:
            try:
                md_path = warning.get("md_path")
                md_path_str = " / ".join(md_path) if isinstance(md_path, list) else "<none>"
                _append_log(log_file,
                    f"  WARNING beacon-skip: name={warning.get('name')!r} "
                    f"error={warning.get('error')!r} md_path={md_path_str}")
            except Exception:
                pass
        return warnings
    raise ValueError(f"Unknown Markdown warning operation: {operation}")


_MARKDOWN_DISALLOWED_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _strip_disallowed_markdown_control_chars(text: str | None) -> str:
    """Repair/remove C0 controls that must never be written to Markdown files.

    Preserve TAB, LF, and CR. Convert common accidental Python escape controls
    back to visible literal backslash sequences (BEL -> ``\\a``, BS ->
    ``\\b``, VT -> ``\\v``, FF -> ``\\f``); strip all other illegal C0
    controls. This keeps F12+3 from persisting accidental backslash-escape
    corruption while preserving likely user intent when possible.
    """
    if not isinstance(text, str):
        return ""

    visible_escape_repairs = {
        "\x07": r"\a",
        "\x08": r"\b",
        "\x0b": r"\v",
        "\x0c": r"\f",
    }

    def _replace(match: re.Match[str]) -> str:
        return visible_escape_repairs.get(match.group(0), "")

    return _MARKDOWN_DISALLOWED_CONTROL_CHARS_RE.sub(_replace, text)


def _normalize_markdown_eof(text: str) -> str:
    """Pure physical-newline EOF-only delegate shared with preparation/file EOF.

    Retains BOM, interior Unicode separators and CRLF/CR/LF; only the terminal
    blank rows/final newline change. Generation still prepares these bytes in memory.
    """
    from markdown_roundtrip import _normalize_generated_markdown_eof
    return _normalize_generated_markdown_eof(text)


def _normalize_markdown_file_eof(file_path: str) -> dict[str, Any]:
    """Compatibility projection over the same U02 EOF rule as pure preparation.

    Generation includes EOF before its single publication and never calls this
    compatibility file writer; explicit legacy callers keep the same U02 rule.
    """
    from markdown_roundtrip import _markdown_eof_preparation
    result = {"changed": False, "before_chars": 0, "after_chars": 0, "delta_chars": 0}
    try:
        snapshot = read_source_snapshot(file_path)
        prepared = _markdown_eof_preparation(snapshot)
        result.update({k: v for k, v in prepared.items() if k not in {"edit_plan", "final_bytes", "changed"}})
        if prepared.get("edit_plan") and prepared["edit_plan"].edits:
            committed = commit_beacon_edit_plan(prepared["edit_plan"])
            result.update(committed)
            if not committed["success"]:
                result.update(after_chars=result["before_chars"], delta_chars=0)
    except Exception as error:  # noqa: BLE001
        result.update(success=False, error=f"failed_to_normalize_eof: {error!r}", error_kind="disk_read_failed")
    return result


# @beacon[
#   id=auto-beacon@_disambiguate_duplicate_beacon_ids_in_file-2n7p,
#   role=_disambiguate_duplicate_beacon_ids_in_file,
#   slice_labels=carto-rev-markdown-full,ra-reconcile,ra-carto-jobs,
#   kind=ast,
#   comment=F12+3 phase-7.5 compatibility analyzer returns U05 duplicate choices without renaming; only an explicit selected resolution delegates to U06's paired opener/close plan and U02 publication. Findings never require a keeper or prevent safe regeneration.,
# ]
def _disambiguate_duplicate_beacon_ids_in_file(file_path: str, *, selected_resolution=None) -> dict[str, Any]:
    """Analyze without renaming; an explicit current finding request uses U06.

    Existing changed/duplicates/renamed_count/error keys remain. No request
    returns changed=False plus U05 findings/ambiguities, never --dupN or a
    keeper. A request carries finding_id/resolution/params from those findings;
    the shared resolver owns chosen opener/close edits and their byte proof.
    """
    from markdown_roundtrip import _plan_markdown_duplicate_resolution
    result = {"changed": False, "duplicates": [], "renamed_count": 0}
    try:
        snapshot = read_source_snapshot(file_path)
        prepared = _plan_markdown_duplicate_resolution(snapshot, selected_resolution)
        plan = prepared.pop("edit_plan", None)
        renames = prepared.pop("planned_renames", [])
        result.update(prepared)
        if plan is not None and plan.edits:
            result.update(commit_beacon_edit_plan(plan))
            if result["success"]:
                result["renamed_count"] = len(renames)
                for group in result["duplicates"]:
                    group["renamed"] = [r for r in renames if r["from"] == group["beacon_id"]]
    except Exception as error:  # noqa: BLE001
        result.update(success=False, changed=False, error=f"failed_to_analyze_or_resolve_ids: {error!r}")
    return result


# @beacon[
#   id=auto-beacon@_job_cancelled-62f3,
#   role=_job_cancelled,
#   slice_labels=carto-rev-markdown-full,
#   kind=ast,
#   comment=Uses only the current daemon task's user-cancel control; outside a task context cancellation is false and no artifact is consulted.,
# ]
def _job_cancelled(job_file: str) -> bool:
    """Use this task's control probe; return False outside its context."""
    from .carto_job_runtime import current_carto_job_context
    context = current_carto_job_context()
    if context is not None:
        return context.job_control.user_cancel_requested()
    return False


def _render_markdown_from_tree(root_node):
    """Pure owned-tree rendering and validation, including lazy parser imports."""
    project_root = Path(__file__).resolve().parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    markdown_roundtrip = importlib.import_module("markdown_roundtrip")
    from markdown_it import MarkdownIt
    frontmatter_text, root_for_render = markdown_roundtrip.detach_yaml_frontmatter_child(root_node)
    raw_markdown = "\n".join(markdown_roundtrip.nexus_to_tokens(root_for_render, depth=0))
    MarkdownIt("commonmark").parse(raw_markdown)
    final_markdown = markdown_roundtrip.clean_html_entities(raw_markdown)
    if frontmatter_text is not None:
        frontmatter_clean = markdown_roundtrip.clean_html_entities(frontmatter_text).strip("\n")
        body_clean = final_markdown.lstrip("\n")
        final_markdown = f"---\n{frontmatter_clean}\n---\n\n{body_clean}"
    return _normalize_markdown_eof(_strip_disallowed_markdown_control_chars(final_markdown)), root_for_render


# @beacon[
#   id=carto-runtime@write-generated-markdown,
#   slice_labels=ra-carto-jobs,carto-rev-markdown-full,
#   kind=ast,
#   comment=Publishes one prepared generation plan through U08 and transfers its real receipt into an owned holder before the CPU checkpoint; the future returns no completed evidence when a holder is supplied. Materialization and explicit no-holder operations retain their returns with no client or task state in the worker.,
# ]
def _write_generated_markdown(path, text=None, *, operation="write", ensure_newline=False, root=None, log_file=None, publication_plan=None, receipt_holder=None):
    """One ordered disk phase on owned data; no client, task or HTTP callback.

    Generation uses only publish, after complete in-memory preparation. Its
    caller-owned plain receipt survives cancellation at the joined CPU return.
    With that holder the future returns no receipt, so a retained cancellation
    traceback cannot keep a second completed evidence owner past its append.
    Materialization and legacy explicit operations retain their old semantics.
    """
    if operation == "write":
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
            if ensure_newline and not text.endswith("\n"):
                handle.write("\n")
        return None
    if operation == "publish":
        from markdown_roundtrip import publish_generated_markdown_beacons
        result = publish_generated_markdown_beacons(publication_plan)
        if receipt_holder is not None:
            receipt_holder["publication"] = result
            return None
        return result
    if operation == "reapply":
        markdown_roundtrip = importlib.import_module("markdown_roundtrip")
        return markdown_roundtrip.reapply_markdown_ast_beacons(
            path, root, log_callback=lambda message: _append_log(log_file, message),
        )
    if operation == "deduplicate":
        try:
            return _disambiguate_duplicate_beacon_ids_in_file(path)
        except Exception as exc:
            return {"changed": False, "error": str(exc)}
    if operation == "eof":
        try:
            return _normalize_markdown_file_eof(path)
        except Exception as exc:
            return {"changed": False, "error": str(exc)}
    raise ValueError(f"Unknown generated-Markdown disk phase {operation!r}")


def _pipeline_checkpoint(phase: str) -> None:
    from .carto_job_runtime import current_carto_job_context
    context = current_carto_job_context()
    if context is not None:
        context.job_control.checkpoint(phase, restart_boundary=True)


# @beacon[
#   id=carto-runtime@phase8-inline,
#   slice_labels=ra-carto-jobs,ra-reconcile,carto-rev-markdown-full,
#   kind=ast,
#   comment=Forwards the parent-held grant to one awaited FILE refresh; a local exception keeps the one in-flight publication's complete findings and unknown changed state. The owned outcome survives the checkpoint and all local evidence aliases are released on every exit.,
# ]
async def _run_phase8_refresh_inline(client, *, job_context, root_uuid: str, file_path=None, outcome_holder=None, held_grant=None, generation_findings=None) -> dict[str, Any]:
    """Return the actual inline outcome; only an exception reuses old findings.

    The parent holds this one file's findings through its phase-8 handoff
    ([ap:65DTIL]); normal returned findings, including [], remain authoritative.
    A failed refresh may already have written: absent a receipt, changed is
    unknown, never a fabricated False. Tracebacks must retain no evidence alias.
    """
    from .coherence.admission import require_coherence_mutation_admission
    control = job_context.job_control if job_context is not None else None
    result = row = envelope = current = failure = None
    chain = []
    try:
        if control is not None:
            control.checkpoint("phase8-refresh", restart_boundary=True)
        await require_coherence_mutation_admission(client._coherence_paths, operation="carto.phase8_refresh")
        if control is not None:
            control.may_have_workflowy_effects = True
        try:
            result = await client.refresh_file_node_beacons(
                file_node_id=str(root_uuid), dry_run=False,
                cancel_callback=control.user_cancel_requested if control is not None else None,
                carto_claim_id=job_context.claim_id if job_context is not None else None,
                held_grant=held_grant,
            )
        except Exception as error:
            from .carto_job_governance import GovernanceError
            current = error
            while current is not None and all(current is not item for item in chain):
                chain.append(current)
                if getattr(current, "code", None) == "OPERATION_CANCELLED" and control is not None:
                    control.checkpoint("phase8-complete", restart_boundary=True)
                if (isinstance(current, GovernanceError) or hasattr(current, "refusal_extra")
                        or getattr(current, "code", None) in {
                            "mutation_region_error", "mutation_serialization_conflict", "mutation_serialization_timeout",
                            "ACCOUNT_WRITE_BLOCKED", "WRITE_GOVERNOR_UNAVAILABLE", "WRITE_GOVERNOR_DEFERRED",
                            "WRITE_SLOT_BUSY_TIMEOUT", "AUTH_ERROR", "RATE_LIMIT_ERROR", "OPERATION_CANCELLED"}):
                    raise current
                current = current.__cause__
            if file_path is None:
                raise
            row = {"file": os.path.realpath(file_path), "status": "failed", "success": False,
                   "changed": None, "findings": generation_findings if generation_findings is not None else [],
                   "error": str(error), "warnings": [{"type": "file_refresh_failed", "error": str(error)}]}
            latch = control.cpu_failure if control is not None else None
            if latch is not None:
                phase, failure = latch
                if (isinstance(failure, Exception) and any(failure is item for item in chain)
                        and phase in {"file:source", "file:plan"}):
                    row["local_cpu_failure"] = {"file": row["file"], "phase": phase,
                        "exception_type": type(failure).__name__, "message": str(failure)}
            result = row
        failed = isinstance(result, Mapping) and (result.get("success") is False or result.get("refresh_errors") or result.get("errors"))
        row = dict(result or {})
        row.setdefault("file", row.get("source_path") or (os.path.realpath(file_path) if file_path else None))
        row.setdefault("status", "failed" if failed else "refreshed")
        row.setdefault("findings", [])
        row = {key: value for key, value in row.items() if key not in {"snapshot", "analysis_inputs", "map"}}
        envelope = {"execution": "inline", "status": "failed" if failed else "completed",
                "success": not failed, "error": result.get("error") if failed else None,
                # FILE warnings belong to its full row, not a second global copy.
                "warnings": [], "files": [row], "result": row}
        if outcome_holder is not None:
            outcome_holder["phase8"] = envelope
        if control is not None:
            control.checkpoint("phase8-complete", restart_boundary=True)
        return envelope
    finally:
        generation_findings = result = row = envelope = current = failure = None
        chain.clear()


# @beacon[
#   id=auto-beacon@run_markdown_generate_pipeline-9qq5,
#   role=run_markdown_generate_pipeline,
#   slice_labels=carto-rev-markdown-full,ra-carto-jobs,ra-reconcile,
#   kind=ast,
#   comment=Prepares one U08 publication then streams its receipt and primary row before inline phase8 through the admission-owned writer. Only that file's findings stay in flight until the phase8 rows/envelope handoff; every exit releases owned evidence and terminal summaries never re-aggregate rows.,
# ]
async def run_markdown_generate_pipeline(
    *,
    client: Any,
    root_uuid: str,
    job_file: str,
    held_grant=None,
) -> dict[str, Any]:
    """Publish one primary file, then stream its separate inline refresh.

    Args:
        client: The daemon's existing WorkFlowyClient and current task snapshot.
        root_uuid: Workflowy UUID of the file/subtree to generate Markdown for.
        job_file: Path to the CARTO job JSON file for status/progress updates.

    Returns:
        A marked summary-only result; complete publication and refresh evidence
        is in this job's append-only files.jsonl, joined by the detail view.
        Typed coherence-barrier refusals propagate to the existing task finalizer with their phase and extras intact.

    The one primary file stays in flight through phase 8 ([ap:65DTIL]). Its
    findings list alone is held after the publication append, then released at
    the phase-8 handoff or every earlier stop/error, including traceback paths.
    """
    from .carto_job_governance import GovernanceError
    from .mutation_locks import MutationRegionError, MutationSerializationConflictError, MutationSerializationTimeoutError
    # Initial job state.
    job = await run_carto_cpu("markdown-artifact:read", _markdown_job_artifact, "read", job_file)
    job["_job_file"] = str(job_file)
    log_file = job.get("logs_path")

    job["status"] = "running"
    job["pid"] = os.getpid()
    job["updated_at"] = _now_iso()
    progress = job.setdefault("progress", {})
    progress.setdefault("current_phase", "starting")
    progress.setdefault("nodes_created", 0)
    progress.setdefault("nodes_updated", 0)
    progress.setdefault("nodes_moved", 0)
    progress.setdefault("nodes_deleted", 0)
    result_summary = job.setdefault("result_summary", {})
    result_summary.setdefault("errors", [])
    await _persist_markdown_job(job_file, job)
    await _log_markdown_job(
        log_file,
        f"Starting F12+3 markdown generate pipeline for root_uuid={root_uuid}",
    )

    pending = {}
    findings_hold = {}
    publication_receipt = {}
    phase8_receipt = {}
    phase8 = None
    prepared = observation = publication = None

    def observe_target(path):
        from beacon_source import source_snapshot_from_bytes
        try:
            return {"snapshot": read_source_snapshot(path), "source_exists": True}
        except FileNotFoundError:
            return {"snapshot": source_snapshot_from_bytes(b"", os.path.realpath(path)), "source_exists": False}
        except (OSError, UnicodeError) as error:
            return {"error": f"{type(error).__name__}: {error}", "error_kind": "source_observation_failed"}

    def save_result(job_snapshot, batch, *, stopping=False, infrastructure_error=None):
        """Consume only this handoff's evidence, then publish incremental counts.

        Pop the owned containers so a CPU-return stop or retained traceback
        cannot keep completed rows/receipts alive. The writer never reads an
        earlier line. Publication precedes every phase-8 row and its envelope.
        """
        writer = record_for(job_file)
        row = batch.pop("row", None)
        refresh = batch.pop("phase8", None)
        hold = batch.pop("findings_hold", None)
        terminal = bool(stopping or infrastructure_error is not None or refresh is not None)
        publication = envelope = rows = metadata = None
        try:
            if row is not None:
                publication = row.get("publication") or {}
                terminal = terminal or not publication.get("success")
                if publication.get("success") is False:
                    row.update({key: publication[key] for key in ("error", "error_kind", "evidence") if key in publication})
                for outcome in row.get("outcomes") or []:
                    if not outcome.get("success"):
                        row.setdefault("warnings", []).append({"type": "heading_preparation_failed", "error": outcome.get("error"),
                            **{key: outcome[key] for key in ("name", "md_path", "node_id", "beacon_id") if key in outcome}})
                outcome = None
                metadata = {"file_path": row["file"], "publication_policy": row.get("publication_policy"),
                    "ast_beacons_reapplied": sum(bool(o.get("success")) for o in row.get("outcomes") or []) if publication.get("success") else 0,
                    "ast_beacons_skipped": sum(not o.get("success") for o in row.get("outcomes") or []),
                    "message": (f"Generated Markdown at {row['file']}; publication and inline refresh are separate outcomes"
                                if publication.get("success") else f"Markdown publication failed at {row['file']}; no generated publication was committed"),
                    "errors": []}
                for key in ("error", "error_kind"):
                    if key in row:
                        metadata[key] = row[key]
                writer.set_value("publication", publication)
                writer.append_file_row(row)
                row = publication = None
                # Metadata is bounded/moved by the existing writer, never a
                # retained primary row or a second copy of its findings.
                writer.update_head(result_summary=metadata)
                metadata = None
            if refresh is not None:
                rows = refresh.pop("files", [])
                for row in rows:
                    writer.append_file_row(row, section="phase8")
                row = None
                rows.clear()
                envelope = dict(refresh)
                if isinstance(envelope.get("result"), Mapping):
                    envelope["result"] = mark_carto_reconcile_children(envelope["result"])
                writer.set_value("phase8_envelope", envelope)
                if envelope.get("success") is False or envelope.get("error"):
                    # The accumulator already counts this component warning;
                    # retain its original top-level display too, not another
                    # run item (which would count it twice).
                    writer._warning_display([{"type": "phase8_refresh_failed", "error": envelope.get("error"),
                                             "disk_committed": bool(writer.summary().get("files_changed_total"))}])
                refresh.clear()
                envelope.clear()
                refresh = envelope = rows = None
            if terminal and hold is not None:
                hold.clear()
            if infrastructure_error is not None:
                writer.append_item("errors", infrastructure_error["error"])
                writer.update_head(result_summary={"error": infrastructure_error["error"],
                                                  "infrastructure_error": infrastructure_error})
            summary = writer.summary(cancelled=stopping, infrastructure_error=infrastructure_error)
            summary["completed_with_warnings"] = summary["status"] == "completed_with_warnings"
            job_snapshot["result_summary"] = summary
            job_snapshot["status"] = "cancelling" if stopping else summary["status"] if terminal else "running"
            job_snapshot["warnings"] = writer._head.get("warnings", [])
            job_snapshot["warning_count"] = summary["warnings_total"]
            if summary.get("error"):
                job_snapshot["error"] = summary["error"]
            _write_job_payload(job_file, job_snapshot)
            return writer.summary(cancelled=stopping, infrastructure_error=infrastructure_error)
        finally:
            if terminal and hold is not None:
                hold.clear()
            batch.clear()
            row = refresh = publication = envelope = rows = metadata = None

    try:
        if _job_cancelled(job_file):
            await _log_markdown_job(log_file, "Cancellation observed before pipeline started.")
            return await _finish_cancelled(job_file, job, log_file)

        # ====================================================================
        # PHASE [2]: Resolve path from this task's current immutable view.
        # ====================================================================
        _pipeline_checkpoint("phase-2-resolve-path")
        await _set_phase(
            job_file, job, log_file, "phase-2-resolve-path", "Resolving file path from cache"
        )

        # Import the path resolver + the F12+4 saved-regime dummy-block reader.
        from .client.api_client_nexus import (
            resolve_cartographer_path_from_node,
            _extract_dummy_path_from_metadata_block,
        )

        from .coherence.operation_cache import current_operation_cache
        snapshot = current_operation_cache(required=False)
        if snapshot is not None:
            nodes_by_id = snapshot.nodes_by_id
        else:
            all_nodes = client._get_nodes_export_cache_nodes()
            nodes_by_id = await run_carto_cpu(
                "markdown-cache-index", lambda rows: {str(n.get("id")): n for n in rows if n.get("id")}, all_nodes,
            )

        # F12+4 "saved generated file" regime (Dan, 2026): a node that lives
        # INSIDE a mapped Markdown file and was materialized with "keep the
        # generated .md" carries a DUMMY metadata block whose body holds an
        # ABSOLUTE Path: line (see _extract_dummy_path_from_metadata_block). We
        # must intercept BEFORE the ancestor walk: resolve_cartographer_path_
        # from_node would otherwise climb into the primary file's heading
        # ancestors (which carry no folder Root:) and either resolve to the
        # WRONG file (the primary .md) or raise. When the dummy block is
        # present we use its absolute path VERBATIM and skip ancestor
        # resolution entirely — mirroring the identical short-circuit in
        # WorkFlowyClientNexus.refresh_file_node_beacons (the F12+1 twin).
        # The dummy block lives only on the root node's own note; F12+3 emits
        # that note nowhere (depth==0 recurses into children only) and
        # _strip_md_path_block strips any leading MD_PATH: block regardless, so
        # the dummy Path: line is never written to the regenerated file.
        root_node_for_path = nodes_by_id.get(str(root_uuid)) or {}
        root_note_for_path = root_node_for_path.get("note") or root_node_for_path.get("no") or ""
        dummy_saved_path = _extract_dummy_path_from_metadata_block(root_note_for_path)
        if dummy_saved_path:
            # Absolute by contract; used verbatim. No ancestor-chain resolution.
            file_path = dummy_saved_path
            await _log_markdown_job(
                log_file,
                f"Resolved file_path via F12+4 dummy-block (saved-generated-file "
                f"regime; no ancestor resolution): {file_path}",
            )
        else:
            resolved = resolve_cartographer_path_from_node(
                node_id=str(root_uuid),
                nodes_by_id=nodes_by_id,
            )
            file_path = str(resolved.get("abs_path") or "")
            await _log_markdown_job(log_file, f"Resolved file_path: {file_path}")
        if not file_path:
            raise ValueError(f"Could not resolve absolute path for node {root_uuid}")

        # ====================================================================
        # PHASE [5]: Re-export from the preflight-fresh cache.
        # ====================================================================
        # The governed launcher owns admission (the region claim) and the
        # mandatory freshness before scheduling this task. This task consumes
        # its current immutable view and does not perform a
        # second refresh.
        # If the snapshot is missing or malformed, export_nodes/_build_hierarchy
        # will fail closed below rather than continuing with unknown state.
        if _job_cancelled(job_file):
            return await _finish_cancelled(job_file, job, log_file)

        _pipeline_checkpoint("phase-5-re-export")
        await _set_phase(
            job_file,
            job,
            log_file,
            "phase-5-re-export",
            "Re-exporting subtree from preflight-fresh cache",
        )

        export_result = await client.export_nodes(node_id=root_uuid)
        flat_nodes = export_result.get("nodes", []) or []
        if not flat_nodes:
            raise ValueError(f"Re-export returned empty nodes for {root_uuid}")

        from .client.api_client_nexus import _prepare_carto_file_tree
        hierarchical_tree = await run_carto_cpu("markdown-hierarchy", _prepare_carto_file_tree, flat_nodes)
        if not hierarchical_tree:
            raise ValueError("Failed to rebuild hierarchical tree from preflight-fresh cache.")
        root_node = hierarchical_tree[0]

        # ====================================================================
        # PHASES [6] + [7]: observe, render and prepare WITHOUT source writes.
        # ====================================================================
        if _job_cancelled(job_file):
            return await _finish_cancelled(job_file, job, log_file)

        _pipeline_checkpoint("phase-6-write-markdown")
        await _set_phase(
            job_file,
            job,
            log_file,
            "phase-6-write-markdown",
            "Preparing Markdown and beacons before one atomic publication",
        )

        observation = await run_carto_cpu("markdown-observe", observe_target, file_path)
        if observation.get("error"):
            pending["row"] = {"file": os.path.realpath(file_path), "status": "failed", "changed": False,
                              "findings": [], **observation}
            observation = None
            return await run_carto_cpu("markdown-result", save_result, _markdown_job_snapshot(job), pending)
        final_markdown, root_for_render = await run_carto_cpu("markdown-render", _render_markdown_from_tree, root_node)

        _pipeline_checkpoint("phase-7-reapply-beacons")
        await _set_phase(
            job_file,
            job,
            log_file,
            "phase-7-reapply-beacons",
            "Preparing heading beacons, duplicate evidence and EOF in memory",
        )

        from markdown_roundtrip import prepare_generated_markdown_beacons
        prepared = await run_carto_cpu("markdown-prepare", prepare_generated_markdown_beacons,
            final_markdown.encode("utf-8"), root_for_render, observation["snapshot"],
            source_exists=observation["source_exists"])
        pending["row"] = {"file": observation["snapshot"].canonical_path, "status": "failed", "changed": False,
                          **{key: value for key, value in prepared.items() if key not in {"final_bytes", "publication_plan"}}}
        publication = await run_carto_cpu("markdown-write", _write_generated_markdown, file_path,
            operation="publish", publication_plan=prepared["publication_plan"], receipt_holder=publication_receipt)
        publication = publication_receipt.get("publication", publication) or {}
        pending["row"].update(publication=publication, changed=bool(publication.get("success") and publication.get("changed")),
                              status="committed" if publication.get("success") else "failed")
        publication_succeeded = bool(publication.get("success"))
        findings_hold["findings"] = pending["row"].get("findings")
        pending["findings_hold"] = findings_hold
        # No prepared envelope, primary row or receipt alias crosses its append.
        prepared = observation = publication = root_for_render = final_markdown = None
        publication_receipt.clear()
        result_summary = await run_carto_cpu("markdown-result", save_result, _markdown_job_snapshot(job), pending)
        job["result_summary"] = result_summary
        if not publication_succeeded:
            return result_summary

        # ====================================================================
        # PHASE [8]: Post-flight file refresh (disk -> Workflowy).
        #
        # Awaited inside the parent's task and long grant. There is no child
        # claim/artifact/row and no second freshness or execution boundary.
        # ====================================================================
        await _set_phase(
            job_file,
            job,
            log_file,
            "phase8-refresh",
            "Refreshing the generated Markdown in this job; cancelling this job also stops phase 8.",
        )
        from .carto_job_runtime import current_carto_job_context
        phase8 = await _run_phase8_refresh_inline(
            client, job_context=current_carto_job_context(), root_uuid=root_uuid, file_path=file_path,
            outcome_holder=phase8_receipt,
            held_grant=held_grant,
            generation_findings=findings_hold.get("findings"),
        )
        progress["current_phase"] = "done"
        job["cache_refresh_required"] = True  # Inline phase8 may have written.
        job["updated_at"] = _now_iso()
        pending.update(phase8=phase8_receipt.pop("phase8", phase8), findings_hold=findings_hold)
        phase8 = None
        return await run_carto_cpu("markdown-result", save_result, _markdown_job_snapshot(job), pending)

    except CoherenceBarrierActiveError:
        raise
    except (GovernanceError, MutationRegionError, MutationSerializationConflictError, MutationSerializationTimeoutError):
        raise
    except asyncio.CancelledError:
        publication = publication_receipt.pop("publication", None)
        if "row" in pending and publication is not None:
            pending["row"].update(publication=publication, changed=bool(publication.get("success") and publication.get("changed")),
                                  status="committed" if publication.get("success") else "failed")
        prepared = observation = publication = None
        writer = record_for(job_file, create=False)
        if pending or (writer is not None and writer._structured):
            pending.update(phase8=phase8_receipt.pop("phase8", phase8), findings_hold=findings_hold)
            phase8 = None
            await run_carto_cpu("markdown-result", save_result, _markdown_job_snapshot(job), pending, stopping=True)
        raise
    except Exception as e:
        if getattr(e, "code", None) in {
                "ACCOUNT_WRITE_BLOCKED", "WRITE_GOVERNOR_UNAVAILABLE", "WRITE_GOVERNOR_DEFERRED",
                "WRITE_SLOT_BUSY_TIMEOUT", "AUTH_ERROR", "RATE_LIMIT_ERROR", "OPERATION_CANCELLED"}:
            raise
        import traceback

        tb = traceback.format_exc()
        prepared = observation = publication = None
        writer = record_for(job_file, create=False)
        if pending or (writer is not None and writer._structured):
            pending.update(phase8=phase8_receipt.pop("phase8", phase8), findings_hold=findings_hold)
            phase8 = None
            return await run_carto_cpu("markdown-result", save_result, _markdown_job_snapshot(job), pending,
                                       infrastructure_error={"type": type(e).__name__, "error": str(e)})
        await _log_markdown_job(log_file, f"ERROR: F12+3 pipeline failed: {e}\n{tb}")
        progress["current_phase"] = "failed"
        job["status"] = "failed"
        job["error"] = str(e)
        result_summary.setdefault("errors", []).append(str(e))
        job["updated_at"] = _now_iso()
        await _persist_markdown_job(job_file, job)
        return {"success": False, "error": str(e)}
    finally:
        findings_hold.clear()
        pending.clear()
        phase8_receipt.clear()
        publication_receipt.clear()
        phase8 = prepared = observation = publication = None


# @beacon[
#   id=auto-beacon@_finish_cancelled-vcda,
#   role=_finish_cancelled,
#   slice_labels=carto-rev-markdown-full,
#   kind=ast,
#   comment=Observes the owning safe checkpoint then joins legacy cancellation presentation artifacts; the common daemon exit remains the only task finalizer.,
# ]
async def _finish_cancelled(job_file: str, job: dict[str, Any], log_file: str | None) -> dict[str, Any]:
    """Observe a safe task stop; the common exit door owns finalization."""
    _pipeline_checkpoint("markdown-cancelled")
    progress = job.setdefault("progress", {})
    progress["current_phase"] = "cancelling"
    job["status"] = "cancelling"
    job["cache_refresh_required"] = False
    job["updated_at"] = _now_iso()
    await _persist_markdown_job(job_file, job)
    await _log_markdown_job(log_file, "Pipeline stopped for cancellation; finalization pending.")
    return {"success": False, "cancelled": True}
