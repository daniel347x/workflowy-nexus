"""Tags-batch ledger and evidence projection over existing planning and Q3.

No selector, source writer or governor lives here. Server-owned infrastructure
is injected; file admission is the same BeaconAdmission used by beacon.fix.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import Counter


def disk_target_collisions(rows):
    """Group physical claims, not ids; retain input indices for repeated handles."""
    claims = {}
    for row in rows:
        key = row.get("target_key")
        if not key:
            continue
        witness = row.get("physical_witness")
        if witness:
            physical = (witness.get("section"), witness.get("opener_start_byte"),
                        witness.get("anchor") if witness.get("opener_start_byte") is None else None)
            identity = (os.path.normcase(os.path.realpath(str(key[0]))),
                        json.dumps(physical, sort_keys=True))
        else:
            identity = tuple(key)  # Compatibility with old callers' full physical keys.
        claims.setdefault(identity, []).append(row)
    return [{"target_key": list(group[0]["target_key"]),
             "symbols": [r.get("symbol") or r.get("node_id") or "?" for r in group],
             "indices": [r["index"] for r in group if "index" in r]}
            for group in claims.values() if len(group) > 1] or None


# @beacon[
#   id=beacon-batch@project-snapshot-evidence,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Projects only target-local planning evidence and snapshot identity with physical candidates field diagnostics and honest absent-id noop reasons; full-file findings belong to the batch's one final observation per canonical file.,
# ]
def project_snapshot_evidence(snapshot, language, selection, mutation, *, requested_tags=()):
    """Keep target-local evidence and planning identity, without full analysis."""
    candidates = list(mutation.get("candidates") or selection.get("candidates") or [])
    bid = mutation.get("beacon_id") or (selection.get("target") or {}).get("note_beacon_id") or None
    evidence = {"source_path": snapshot.canonical_path, "language": language,
                "beacon_id": bid, "candidate_lines": [c["line"] for c in candidates if "line" in c],
                "candidates": candidates, "diagnostics": list(mutation.get("diagnostics") or []),
                "physical_witness": mutation.get("physical_witness") or selection.get("physical_witness"),
                "field_errors": list(mutation.get("field_errors") or []),
                "field_outcomes": [{**f, "status": "error"} for f in mutation.get("field_errors") or []], "findings": [],
                "observed": {"canonical_file": snapshot.canonical_path, "file_sha256": snapshot.sha256}}
    reason = mutation.get("noop_reason")
    if mutation.get("planned_operation") == "noop":
        if reason in {"labels_already_absent", "no_beacon_at_target"}:
            remaining = [c for c in candidates if c.get("kind") == "span" and
                         set(str(c.get("labels") or "").split(",")) & set(requested_tags)]
            if selection.get("status") == "satisfied_noop" and selection.get("anchor") is None and bid and remaining:
                reason = "note_beacon_id_missing_on_disk"
                evidence.update(warning=reason, rename_candidates=remaining, satisfied=False)
            else:
                reason = "already_absent"
        evidence["noop_reason"] = reason or "already_satisfied"
    return evidence


def project_plan_result(plan, result=None):
    """Keep evidence on every classification and commit outcome, without IO."""
    evidence = dict(getattr(plan, "evidence", None) or {})
    for name in ("source_path", "language", "beacon_id", "physical_witness", "selection_target",
                 "comment_outcome", "comment_warning", "noop_reason"):
        value = getattr(plan, name, None)
        if value is not None and name not in evidence:
            evidence[name] = value
    for name in ("candidates", "diagnostics"):
        evidence.setdefault(name, list(getattr(plan, name, ()) or ()))
    evidence.setdefault("findings", [])
    if result is not None:
        evidence.update(result)
    if evidence.get("operation") == "noop":
        evidence.setdefault("noop_reason", "already_satisfied")
    return evidence


def project_sync_result(result, *, client, node_id):
    """Add an explicitly current observation after the protected legacy writer.

    This is not the writer's precommit witness. Protected F12 success mapping
    is unchanged; current-disk findings are additive and never authorize writes.
    """
    from .beacon_integrity import _observe_beacon_file

    result = dict(result)
    path = result.get("source_path")
    result.setdefault("findings", [])
    if not path:
        try:
            from .client.api_client_nexus import _resolve_beacon_target_from_cache
            nodes = client._get_nodes_export_cache_nodes() or []
            cache = {str(n["id"]): n for n in nodes if isinstance(n, dict) and n.get("id")}
            path = _resolve_beacon_target_from_cache(cache, node_id).get("source_path")
        except Exception as error:
            result["analysis_error"] = {"exception": type(error).__name__, "message": str(error)}
    if path:
        result.setdefault("source_path", path)
        try:
            observation = _observe_beacon_file(path)
            result["findings"] = observation["rows"]
            result["findings_observation"] = {k: v for k, v in observation.items() if k != "rows"}
            result["findings_basis"] = "current_disk_after_legacy_writer"
        except Exception as error:
            result["analysis_error"] = {"exception": type(error).__name__, "message": str(error)}
    if result.get("operation") == "noop" or result.get("reason") == "virtual_group_noop":
        result.setdefault("noop_reason", result.get("reason") or "already_satisfied")
        if result["noop_reason"] in {"labels_already_absent", "no_beacon_at_target"}:
            result["noop_reason"] = "already_absent"
    return result


def _row(op, uid=None, evidence=None, **outcome):
    row = {"index": op.input_index, "symbol": op.handle, "uuid": uid,
           "operation": None, "beacon_id": None, "source_path": None,
           "disk_changed": False, "comment_outcome": None, "findings": [],
           **(evidence or {}), **outcome}
    row["field_outcomes"] = [*(row.get("field_outcomes") or []),
                             *(getattr(op, "field_outcomes", ()) or ())]
    if row.get("comment_warning"):
        row.setdefault("warning", row["comment_warning"])
    return row


def _field_failed(row):
    return any(f.get("status") == "error" for f in row.get("field_outcomes", []))


def _finish(spec, rows, report, files_changed, preflight_failures, commit_failures, *, final_findings=(), aborted=False, error=None):
    """Summarize actual row outcomes and the union of final file observations."""
    successful = [r for r in rows if r["status"] in {"committed", "noop"}
                  and r.get("satisfied", True) and not _field_failed(r)]
    failed = [r for r in rows if r not in successful]
    counts = Counter(r["status"] for r in rows)
    line = f"{counts['committed']} committed · {counts['noop']} noop · {len(failed)} failed"
    if len(failed) == 1:
        row = failed[0]
        kind = row.get("error_kind") or ("field_error" if _field_failed(row) else row["status"])
        reason = str(row.get("error") or "").strip()
        line += f" ({row['symbol']}: {kind}" + (f" — {reason[:110]}" if reason else "") + ")"
    elif failed:
        line += " (" + ", ".join(dict.fromkeys(r.get("error_kind") or r["status"] for r in failed)) + ")"
    findings = {finding["finding_id"]: finding for finding in final_findings}
    result = {"success": bool(rows) and not failed, "changed": bool(files_changed),
              "partial_success": bool((successful or files_changed) and failed and not aborted),
              "mode": spec.mode, "results": rows, "files_changed": sorted(files_changed),
              "warnings": [f"{r['symbol']}: {r['warning']}" for r in rows if r.get("warning")],
              "summary": {"requested": len(rows), "committed": counts["committed"], "noop": counts["noop"],
                          "preflight_failures": preflight_failures, "commit_failures": commit_failures,
                          "comment_conflicts_kept": sum(r.get("comment_outcome") == "kept_existing_conflict" for r in rows)},
              "summary_line": line, "resolution_report": report, "findings": list(findings.values())}
    if error:
        result["error"] = error
    return result


# @beacon[
#   id=beacon-batch@run-beacon-batch,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Ordered tags ledger keeps target-local plans and existing per-file Q3/atomic admission; after all rows run each resolved canonical file is read and observed once for final full findings and row-local subsets; analysis errors stay data and leases outlive commit workers.,
# ]
async def run_beacon_batch(*, spec, client, admission, resolve_root, root_error, suppress_tags,
                           resolve_items, resolve_target, check_uniqueness):
    """Preflight all, then JIT per canonical file; return rows in input order.

    Shared cache/root/suppression/resolver failures are global. Once available,
    a raised per-item computation is local. No rollback, retry or refresh.
    Full findings come once per resolved file at call end, including files with
    only noop/error rows; each row keeps its own beacon's subset and candidates.
    """
    ops = list(spec.operations)
    rows = [None] * len(ops)
    report = None
    preflight_failures = commit_failures = 0
    files_changed = set()
    cache = {}

    async def finish(**kwargs):
        from .beacon_integrity import _observe_beacon_file

        by_file = {}
        for row in rows:
            path = row.get("source_path")
            if not path and row.get("uuid"):
                try:
                    target = resolve_target(cache, row["uuid"])
                    path = target.get("source_path") if target.get("success") else None
                except Exception:
                    path = None  # An unresolved file cannot supply an observation.
                if path:
                    row["source_path"] = path
            if path:
                canonical = os.path.normcase(os.path.realpath(str(path)))
                by_file.setdefault(canonical, []).append(row)
        final_findings = []
        for path, file_rows in by_file.items():
            observation = {}
            analysis_error = None
            try:
                await asyncio.to_thread(_observe_beacon_file, path, observation=observation)
            except Exception as error:
                analysis_error = {"exception": type(error).__name__, "message": str(error)}
            findings = observation.get("rows", [])
            final_findings.extend(findings)
            for row in file_rows:
                related = [f for f in findings if f.get("beacon_id") == row.get("beacon_id")]
                row["findings"] = related
                row.pop("finding", None)
                if related:
                    row["finding"] = next((f for f in related if f["code"] == "duplicate_id"), related[0])
                row["findings_observation"] = {k: v for k, v in observation.items() if k != "rows"}
                if analysis_error:
                    row["analysis_error"] = analysis_error
        return _finish(spec, rows, report, files_changed, preflight_failures, commit_failures,
                       final_findings=final_findings, **kwargs)

    async def abort(message):
        nonlocal preflight_failures
        preflight_failures += 1
        rows[:] = [_row(op, status="not_run_due_to_batch_preflight_failure") for op in ops]
        return await finish(aborted=True, error=message)

    try:
        nodes = client._get_nodes_export_cache_nodes() or []
        cache = {str(n["id"]): n for n in nodes if isinstance(n, dict) and n.get("id")}
        admission.cache_by_id = cache
        roots = []
        for handle in spec.root_handles:
            resolved = resolve_root(str(handle).strip(), cache)
            if not resolved.get("resolved_uuid"):
                return await abort(root_error(resolved, display=str(handle), label="root_uuid").get("error") or "Invalid root")
            roots.append(str(resolved["resolved_uuid"]))
        suppressed = await suppress_tags()
        core = await asyncio.to_thread(asyncio.run, resolve_items(
            cache_by_id=cache, symbols=[op.handle for op in ops], resolved_roots=list(dict.fromkeys(roots)),
            suppress_tags=suppressed, favor=spec.favor))
        if not core.get("ok"):
            return await abort(core.get("error") or "symbol resolution failed")
    except Exception as exc:
        return await abort(f"Shared cache/root/suppression/resolver unavailable: {exc}")
    report = core.get("resolution_report")
    records = list(core.get("records") or [])
    duplicate_uids = {uid for uid, count in Counter(str(r["uuid"]) for r in records
                      if r.get("status") == "resolved" and r.get("uuid")).items() if count > 1}
    eligible, claims = [], []
    for index, op in enumerate(ops):
        rec = records[index] if index < len(records) else {}
        uid = str(rec.get("uuid") or "") or None
        if getattr(op, "item_error", None):
            rows[index] = _row(op, uid, status="error", error=op.item_error, error_kind="item_invalid")
        elif rec.get("status") != "resolved":
            rows[index] = _row(op, status=rec.get("status") or "unresolved", candidates=rec.get("candidates") or [])
        elif uid in duplicate_uids:
            rows[index] = _row(op, uid, status="duplicate_resolved_uuid", error_kind="duplicate_resolved_uuid",
                               error="multiple mutation inputs resolve to the same Workflowy node")
        else:
            try:
                pf = await client.preflight_beacon_write_from_node(uid, op, spec.mode)
            except Exception as exc:
                pf = {"outcome": "failure", "error_kind": "preflight_error", "error": f"preflight failed: {exc}"}
            outcome = pf.get("outcome")
            rows[index] = _row(op, uid, pf, status="error" if outcome == "failure" else
                               "noop" if outcome == "noop" else "pending")
            if outcome == "noop":
                rows[index].setdefault("noop_reason", "already_satisfied")
            if outcome != "failure":
                claims.append({**pf, "symbol": op.handle, "index": index})
                if outcome != "noop":
                    eligible.append(index)
    collisions = check_uniqueness(claims)
    for collision in collisions or []:
        for index in collision["indices"]:
            rows[index].update(status="error", error_kind="disk_target_collision",
                               error="disk_target_collision: multiple rows claim the same physical disk target")
    eligible = [i for i in eligible if rows[i]["status"] == "pending"]
    groups = {}
    for index in eligible:
        row = rows[index]
        try:
            target = resolve_target(cache, row["uuid"])
            if not target.get("success"):
                raise ValueError(target.get("error") or "Cannot resolve mapped target")
            path = os.path.normcase(os.path.realpath(str(target["source_path"])))
            entry = {"abs_path": str(target["source_path"]), "canonical_file": path,
                     "node_id": target.get("file_node_id") or row["uuid"]}
            groups.setdefault(path, {"target": entry, "indices": []})["indices"].append(index)
        except Exception as exc:
            row.update(status="error", error_kind="footprint_unresolvable", error=str(exc))
    preflight_failures = sum(r["status"] not in {"pending", "noop"} or _field_failed(r) for r in rows)
    if spec.atomic and preflight_failures:
        for row in rows:
            if row["status"] in {"pending", "noop"} and not _field_failed(row):
                row["status"] = "not_run_due_to_batch_preflight_failure"
            elif _field_failed(row):
                row.update(status="error", error_kind="invalid_comment", error="atomic=true: invalid comment field")
        return await finish(aborted=True, error="atomic=true: item preflight failures present; zero writes and no lease")

    async def execute(indices):
        nonlocal commit_failures
        for index in indices:
            op, row = ops[index], rows[index]
            uid = row["uuid"]
            try:
                plan = await client.plan_beacon_write_from_node(uid, op, spec.mode)
            except Exception as exc:
                row.update(status="error", error_kind="jit_plan_error", error=f"plan_beacon_write_from_node failed: {exc}")
                commit_failures += 1
                continue
            evidence = project_plan_result(plan)
            if not evidence.get("observed"):
                retained = False
                for name in ("source_path", "language", "beacon_id", "physical_witness",
                             "candidates", "candidate_lines", "diagnostics", "field_errors"):
                    if not evidence.get(name) and row.get(name):
                        evidence[name] = row[name]
                        retained = True
                if retained:
                    evidence["retained_preflight_observed"] = row.get("observed")
            if not getattr(plan, "success", False):
                rows[index] = _row(op, uid, evidence, status="error", error=getattr(plan, "error", None) or "JIT plan failed",
                                   error_kind=getattr(plan, "error_kind", None) or "jit_plan_error")
                commit_failures += 1
                continue
            if plan.planned_operation == "noop":
                rows[index] = _row(op, uid, evidence, status="noop", operation="noop",
                                   noop_reason=evidence.get("noop_reason") or "already_satisfied")
                continue
            try:
                node = cache.get(uid) or {}
                result = await client.commit_beacon_write_plan(plan=plan, note=str(node.get("note") or node.get("no") or ""))
            except Exception as exc:
                result = {"success": False, "error_kind": "commit_error", "error": f"commit_beacon_write_plan failed: {exc}"}
            evidence.update(result)
            if result.get("success"):
                src = result.get("source_path") or plan.source_path
                if result.get("disk_changed", True) and src:
                    files_changed.add(str(src))
                rows[index] = _row(op, uid, evidence, status="committed", source_path=src,
                                   disk_changed=bool(result.get("disk_changed", True)))
            else:
                rows[index] = _row(op, uid, evidence, status="error", error=result.get("error") or "commit failed")
                commit_failures += 1

    groups_to_run = ([{"target": [g["target"] for g in groups.values()],
                       "indices": [i for g in groups.values() for i in g["indices"]]}] if spec.atomic and groups else
                     [{"target": [g["target"]], "indices": g["indices"]} for g in groups.values()])
    for group in groups_to_run:
        leases, failure = await admission.admit(group["target"])
        if failure:
            for index in group["indices"]:
                rows[index].update(status="error", error_kind=failure["error_kind"], error=failure["error"])
                preflight_failures += 1
            if spec.atomic:
                return await finish(aborted=True, error=f"atomic=true: admission refused: {failure['error']}")
            continue
        primary = None
        try:
            await execute(group["indices"])
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if leases:
                await admission.release(leases, primary_error=primary)
    return await finish()
