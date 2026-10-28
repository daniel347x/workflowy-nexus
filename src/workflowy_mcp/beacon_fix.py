"""Explicit finding requests over the shared snapshot, planner, writer and Q3.

No source splice, grammar, selector or governor policy lives here. Preflight
is advisory unless atomic is requested; execution re-observes each item. A
file's thread is never abandoned while its lease is held. The admission
object is also the reusable boundary for the tags batch front.
"""
from __future__ import annotations

from collections import Counter
import asyncio
import copy
from dataclasses import dataclass
import difflib
import hashlib
import json
import os

from beacon_edits import EditOffsetMap, commit_beacon_edit_plan, verify_beacon_edit_plan
from beacon_findings import beacon_finding_id, canonical_beacon_file
from beacon_resolutions import plan_beacon_resolution
from beacon_source import read_source_snapshot, source_snapshot_from_bytes
from nexus_map_codebase import _beacon_edit_records

from .beacon_integrity import observe_beacon_snapshot
from .client.api_client_nexus import (
    _BEACON_LANGUAGE_BY_EXT, _await_thread_to_completion,
    _extract_path_header_from_note, _path_value_looks_like_any_file,
    resolve_cartographer_path_from_node,
)
from .tool_path_resolution import resolve_tool_path


ROW_ERROR_KINDS = (
    "item_invalid", "node_handle_unresolved", "map_target_invalid",
    "invalid_parameter_value", "beacon_target_unsupported", "source_read_failed",
    "analysis_failed", "stale", "resolution_not_offered", "invalid_params",
    "no_safe_geometry", "plan_mismatch", "collision", "overlap_conflict",
    "footprint_unresolvable", "admission_failed", "atomic_aborted",
    "source_changed_since_plan", "disk_write_failed", "fix_failed",
    "governance_cleanup_required",
)

_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "finding_id": {"type": "string"},
        "file_path": {"type": "string"},
        "node_handle": {"type": "string", "description": "A mapped FILE node, not a symbol or folder."},
        "observed": {
            "type": "object", "description": "The unchanged observed witness returned by check, never an arbitrary byte patch.",
            "properties": {"canonical_file": {"type": "string"}, "file_sha256": {"type": ["string", "null"]},
                           "block_ranges": {"type": "array"}, "field_ranges": {"type": "array"},
                           "member_fingerprints": {"type": "array"}, "identity_ambiguous": {"type": "boolean"}},
            "required": ["canonical_file", "file_sha256", "block_ranges", "field_ranges", "member_fingerprints", "identity_ambiguous"],
        },
        "resolution": {"type": "string"},
        "params": {"type": "object", "description": "Operands from that finding's resolution params_schema."},
    },
    "required": ["finding_id", "observed", "resolution"],
    "oneOf": [{"required": ["file_path"]}, {"required": ["node_handle"]}],
    "additionalProperties": False,
}
FIX_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        # The shared envelope validator does not recurse through allOf. Keep
        # the precise item schema here while letting malformed items reach
        # our row-local normalization rather than veto their neighbours.
        "items": {"type": "array", "items": {"allOf": [_ITEM_SCHEMA]}},
        "dry_run": {"type": "boolean", "default": False},
        "atomic": {"type": "boolean", "default": False},
    },
    "required": ["items"],
    "additionalProperties": False,
}


class BeaconFixRequestInvalid(ValueError):
    """Only the whole envelope is invalid; individual items are ledger rows."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _failure(kind, message, **evidence):
    return {"success": False, "changed": False,
            "status": "stale" if kind == "stale" else "error",
            "error_kind": kind, "error": message, "evidence": evidence, "findings": []}


def normalize_fix_request(payload):
    """Validate only the request envelope globally; preserve every item slot."""
    if (not isinstance(payload, dict) or set(payload) - {"items", "dry_run", "atomic"}
            or not isinstance(payload.get("items"), list)
            or any(type(payload.get(key, False)) is not bool for key in ("dry_run", "atomic"))):
        raise BeaconFixRequestInvalid("beacon.fix requires items[], with optional boolean dry_run and atomic")
    normalized, rows = [], []
    for index, item in enumerate(payload["items"]):
        base = {"index": index, "finding_id": item.get("finding_id") if isinstance(item, dict) else None}
        problem = None
        if not isinstance(item, dict):
            problem = "item must be an object"
        elif set(item) - set(_ITEM_SCHEMA["properties"]):
            problem = "item has unknown fields"
        elif any(not isinstance(item.get(k), str) or not item[k].strip()
                 for k in ("finding_id", "resolution")):
            problem = "finding_id and resolution must be nonblank strings"
        elif ("file_path" in item) == ("node_handle" in item):
            problem = "item needs exactly one file_path or FILE node_handle"
        elif any(not isinstance(item[k], str) or not item[k].strip()
                 for k in ("file_path", "node_handle") if k in item):
            problem = "file_path/node_handle must be a nonblank string"
        elif not isinstance(item.get("observed"), dict):
            problem = "observed must be the check witness object"
        else:
            observed = item["observed"]
            if (not isinstance(observed.get("canonical_file"), str)
                    or not isinstance(observed.get("file_sha256"), (str, type(None)))
                    or "file_sha256" not in observed
                    or any(not isinstance(observed.get(k), list)
                           for k in ("member_fingerprints", "block_ranges", "field_ranges"))
                    or type(observed.get("identity_ambiguous")) is not bool):
                problem = "observed is not a complete check witness"
        normalized.append(copy.deepcopy(item) if problem is None else None)
        rows.append({**base, **_failure("item_invalid", problem)} if problem else base)
    return normalized, rows


def mapped_path_index(cache_by_id):
    """Resolve each Path/Root once through the shared mapped-path authority."""
    entries = []
    for uid, node in cache_by_id.items():
        header = _extract_path_header_from_note(str(node.get("note") or node.get("no") or ""))
        if not header:
            continue
        try:
            resolved = resolve_cartographer_path_from_node(node_id=uid, nodes_by_id=cache_by_id)
            path = os.path.realpath(resolved["abs_path"])
        except Exception:
            # An unrelated unresolved map entry is not a target or a veto.
            continue
        entries.append({"node_id": uid, "abs_path": path,
                        "canonical_file": canonical_beacon_file(path), "header": header,
                        "kind": "file" if header[0] == "Path" and _path_value_looks_like_any_file(header[1]) else "folder"})
    return entries


def _covering_entry(path, entries):
    key = canonical_beacon_file(path)
    exact = [entry for entry in entries if entry["kind"] == "file" and entry["canonical_file"] == key]
    if exact:
        return exact[0]
    folders = []
    for entry in entries:
        if entry["kind"] != "folder":
            continue
        root = entry["canonical_file"]
        if key == root or key.startswith(root.rstrip("/\\") + "/"):
            folders.append(entry)
    return max(folders, key=lambda e: len(e["canonical_file"]), default=None)


def _resolve_item(item, cache_by_id, entries, resolve_handle):
    if "file_path" in item:
        resolution = resolve_tool_path(item["file_path"], param="file_path", context="beacon.fix", direction="write")
        path = os.path.realpath(resolution.resolved)
        entry = _covering_entry(path, entries)
        target = {"path_record": resolution.as_dict(), "resolved_uuid": None}
    else:
        uid, outcome = resolve_handle(item["node_handle"])
        if not uid:
            return _failure("node_handle_unresolved", "FILE node_handle did not resolve uniquely",
                            candidates=outcome.get("candidates") or [])
        node = cache_by_id.get(uid) or {}
        header = _extract_path_header_from_note(str(node.get("note") or node.get("no") or ""))
        if not header or header[0] != "Path" or not _path_value_looks_like_any_file(header[1]):
            return _failure("map_target_invalid", "beacon.fix node_handle must name a mapped FILE node", resolved_uuid=uid)
        resolved = resolve_cartographer_path_from_node(node_id=uid, nodes_by_id=cache_by_id)
        path = os.path.realpath(resolved["abs_path"])
        entry = {"node_id": uid}
        target = {"path_record": None, "resolved_uuid": uid}
    language = _BEACON_LANGUAGE_BY_EXT.get(os.path.splitext(path)[1].lower())
    if language is None:
        return _failure("beacon_target_unsupported", "No beacon parser/writer supports this file", file=path)
    if os.path.isdir(path):
        return _failure("invalid_parameter_value", "file_path must name one file, not a directory", file=path)
    return {"abs_path": path, "canonical_file": canonical_beacon_file(path), "language": language,
            "node_id": entry["node_id"] if entry else None, **target}


@dataclass
class BeaconAdmission:
    """Dependency-injected existing Q3 boundary; no server import or new lock."""
    cache_by_id: dict
    compute_footprint: object
    acquire: object
    release: object
    account_scope: object
    jobs_root: object

    # @beacon[
    #   id=beacon-fix@admit-files,
    #   slice_labels=beacon-writeback,
    #   kind=ast,
    #   comment=Reusable canonical-file Q3 admission for fix and batch; unmapped files need no hold. One in-process Q3 footprint per mapped file or covering folder; scalar account-scope callback, all-target atomic mode only when explicitly requested.,
    # ]
    async def admit(self, targets):
        """Acquire the caller's file group with unchanged governor APIs.

        Default callers pass one file; atomic callers pass all eligible files.
        Failures are returned, not a new policy or retry. Release is caller-owned
        and must follow completion of every worker using these tokens.
        """
        footprints, seen = [], set()
        for target in targets:
            if target["canonical_file"] in seen or target.get("node_id") is None:
                continue
            seen.add(target["canonical_file"])
            try:
                footprints.append(self.compute_footprint(target["abs_path"], target["node_id"], self.cache_by_id, kind="file"))
            except Exception as error:
                return [], _failure(getattr(error, "code", None) if getattr(error, "code", None) == "footprint_unresolvable"
                                    else "admission_failed", str(error), file=target["abs_path"], exception=type(error).__name__)
        if not footprints:
            return [], None
        try:
            scope = self.account_scope()
            leases = await self.acquire(footprints, account_scope_id=scope, jobs_root=self.jobs_root())
            return leases, None
        except Exception as error:
            return [], _failure("overlap_conflict", str(error), exception=type(error).__name__)


def _map_chain(observed_hash, snapshot_hash, history):
    """Only this request's contiguous, proved publications authorize BR-01."""
    if not history or history[-1][1] != snapshot_hash:
        return None
    for index, (before, _after, _offsets) in enumerate(history):
        if before == observed_hash:
            chain = history[index:]
            if all(chain[n][1] == chain[n + 1][0] for n in range(len(chain) - 1)):
                return [entry[2] for entry in chain]
    return None


def _carry_range(bounds, maps):
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 2 or any(type(v) is not int for v in bounds):
        return None
    current = tuple(bounds)
    for offsets in maps:
        current = offsets.map_range(*current)
        if current is None:
            return None
    return list(current)


def _rebind(snapshot, findings, item, history):
    """Bind content+semantic witnesses, never an id, ordinal or shifted line alone.

    Return a current finding and translated operands. External drift permits
    unique unchanged members only. Request-owned drift additionally carries
    ambiguous physical choices through U02 maps, checked against fingerprints.
    """
    observed = item["observed"]
    if observed["canonical_file"] != canonical_beacon_file(snapshot.canonical_path):
        return None, None, "observed belongs to a different canonical file"
    same = observed["file_sha256"] == snapshot.sha256
    maps = None if same else _map_chain(observed["file_sha256"], snapshot.sha256, history)
    if not same and maps is None and (observed["identity_ambiguous"] or "occurrence_ordinal" in observed):
        return None, None, "identical members require the original file version or a newly listed choice"
    old_keys = observed["member_fingerprints"]
    old_ranges = observed["block_ranges"]
    if len(old_keys) != len(old_ranges):
        return None, None, "member fingerprints and locators disagree"
    carried = [_carry_range(r, maps or ()) for r in old_ranges]
    if any(r is None for r in carried):
        return None, None, "a selected member locator did not survive this request's edits"
    matches = []
    for finding in findings:
        current = finding["observed"]
        # Reconstruct the old identity with CURRENT content and the old ordinal:
        # this permits BR-01 ordinal changes but never substitutes another code.
        expected_id = beacon_finding_id(current["canonical_file"], finding["code"], finding["kind"], finding["members"],
            file_sha256=observed["file_sha256"], unreadable_identity=observed.get("unreadable_identity"),
            anchor=current.get("anchor"), ordinal=observed.get("occurrence_ordinal"))
        if expected_id != item["finding_id"]:
            continue
        new_keys, new_ranges = current["member_fingerprints"], current["block_ranges"]
        if len(new_keys) != len(old_keys):
            continue
        if same:
            keys = ("canonical_file", "file_sha256", "block_ranges", "field_ranges", "member_fingerprints",
                    "identity_ambiguous", "identical_member_groups", "anchor", "occurrence_ordinal", "identical_occurrences")
            if any(_json(observed.get(k)) != _json(current.get(k)) for k in keys):
                continue
            indices = list(range(len(old_keys)))
        else:
            if maps is None and (current["identity_ambiguous"] or not old_keys):
                continue
            indices = []
            for key, bounds in zip(old_keys, carried):
                candidates = [j for j, value in enumerate(new_keys) if _json(value) == _json(key)
                              and (maps is None or _json(new_ranges[j]) == _json(bounds))]
                if len(candidates) != 1:
                    break
                indices.append(candidates[0])
            if len(indices) != len(old_keys) or len(set(indices)) != len(indices):
                continue
        matches.append((finding, indices))
    if len(matches) != 1:
        return None, None, "finding is absent, changed or not uniquely witnessed on current disk"
    finding, indices = matches[0]
    params = copy.deepcopy(item.get("params", {}))
    if same or not isinstance(params, dict):
        return finding, params, None

    def position(value, bias="left"):
        if type(value) is not int:
            return value  # Leave malformed operands to the planner's schema.
        if maps is not None:
            for offsets in maps:
                value = offsets.map_offset(value, bias=bias)
                if value is None:
                    raise ValueError("selected byte operand was replaced by an earlier item")
            return value
        choices = {finding["observed"]["block_ranges"][indices[i]][0] + value - bounds[0]
                   for i, bounds in enumerate(old_ranges) if bounds[0] <= value <= bounds[1]}
        if len(choices) != 1:
            raise ValueError("byte operand outside an unchanged member needs a newly listed choice")
        return choices.pop()

    def operands(values):
        for key in ("member", "opener", "survivor", "keeper"):
            if type(values.get(key)) is int and 0 <= values[key] < len(indices):
                values[key] = indices[values[key]]
        if "range" in values and isinstance(values["range"], (list, tuple)) and len(values["range"]) == 2:
            values["range"] = [position(values["range"][0], "right"), position(values["range"][1], "left")]
        if "boundary" in values:
            values["boundary"] = position(values["boundary"])
        if "close" in values:
            if item["resolution"] == "pair_close":
                if type(values["close"]) is int and 0 <= values["close"] < len(indices):
                    values["close"] = indices[values["close"]]
            else:
                values["close"] = position(values["close"], "right")
        if isinstance(values.get("new_ids"), list):
            for assignment in values["new_ids"]:
                if isinstance(assignment, dict):
                    operands(assignment)
    try:
        operands(params)
    except (ValueError, TypeError, IndexError) as error:
        return None, None, str(error)
    return finding, params, None


def _preview(snapshot, planned, finding):
    plan = planned.get("edit_plan")
    candidate = planned.get("proof", {}).get("candidate_bytes", snapshot.raw_bytes)
    return {"before_sha256": snapshot.sha256, "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
            "after_sha256": hashlib.sha256(candidate).hexdigest(),
            "unified_diff": "".join(difflib.unified_diff(
                snapshot.raw_bytes.decode("utf-8").splitlines(keepends=True),
                candidate.decode("utf-8").splitlines(keepends=True),
                fromfile=snapshot.canonical_path, tofile=snapshot.canonical_path)),
            "affected_ids": planned.get("affected_ids", []),
            "affected_symbols": [m["anchor"] for m in finding["members"] if m.get("anchor")]
                                + [c["target"] for c in planned.get("consequences", []) if c.get("target")],
            "consequences": planned.get("consequences", []), "would_change": bool(plan and plan.edits)}


def _plan_item(target, item, history, planned_ids, snapshot=None, findings=None):
    """One read and common analysis per planning round, then the pure planner."""
    if snapshot is None:
        snapshot, findings, failure = _observe_target(target)
        if failure:
            return failure, None
    try:
        finding, params, reason = _rebind(snapshot, findings, item, history)
    except (ValueError, TypeError, KeyError, IndexError) as error:
        return {**_failure("item_invalid", f"invalid observed witness: {error}"), "findings": findings}, None
    if finding is None:
        return {**_failure("stale", reason, observed=item["observed"], current_sha256=snapshot.sha256),
                "findings": findings}, None
    try:
        planned = plan_beacon_resolution(snapshot, finding, item["resolution"], params,
                                         language=target["language"], planned_ids=planned_ids)
    except Exception as error:
        return {**_failure("fix_failed", f"{type(error).__name__}: {error}"), "findings": [finding]}, None
    public = {key: value for key, value in planned.items() if key not in {"edit_plan", "proof", "finding"}}
    if public.get("error_kind") == "finding_not_found":
        public.update(error_kind="stale", status="stale")
    public["findings"] = [finding]
    if planned.get("success"):
        public.update(_preview(snapshot, planned, finding))
        public["changed"] = False
    return public, planned.get("edit_plan")


def _observe_target(target):
    """The single source read/analysis funnel for preflight and actual rounds."""
    try:
        snapshot = read_source_snapshot(target["abs_path"])
    except Exception as error:
        return None, None, _failure("source_read_failed", f"{type(error).__name__}: {error}")
    findings, failure = _snapshot_findings(snapshot, target)
    if failure:
        return None, None, failure
    return snapshot, findings, None


def _snapshot_findings(snapshot, target):
    try:
        return observe_beacon_snapshot(snapshot, language=target["language"])["rows"], None
    except Exception as error:
        return None, _failure("analysis_failed", f"{type(error).__name__}: {error}", file_sha256=snapshot.sha256)


def _domains_overlap(left, right):
    for a in left.edits:
        for b in right.edits:
            if a.start_byte == b.start_byte or (a.start_byte < b.end_byte and b.start_byte < a.end_byte):
                return True
    return False


def _preflight_file(target, indexed, planned_ids):
    """One supplied snapshot for all independent previews and overlap domains."""
    snapshot, findings, failure = _observe_target(target)
    if failure:
        return {i: (failure, None) for i, _ in indexed}, None, None
    results = {}
    plans = []
    for index, item in indexed:
        # Speculative ids do not outlive preflight, and a refused item cannot
        # reserve an id for the actual request's later committing neighbour.
        result, plan = _plan_item(target, item, [], set(planned_ids), snapshot, findings)
        results[index] = (result, plan)
        if plan is not None:
            plans.append((index, plan))
    collisions = {}
    for offset, (index, plan) in enumerate(plans):
        for other_index, other in plans[offset + 1:]:
            if _domains_overlap(plan, other):
                collisions.setdefault(index, []).append(other_index)
                collisions.setdefault(other_index, []).append(index)
    for index, others in collisions.items():
        result, _plan = results[index]
        results[index] = ({**_failure("collision", "requested edit domains overlap", conflicting_indices=sorted(others)),
                           "findings": result["findings"]}, None)
    return results, snapshot, findings


def _atomic_sequence_preflight(groups):
    """Prove the requested execution order in memory before atomic admission.

    Independent edit domains are already known. This second planning round
    uses U02's prepared bytes (never a splice or publication) so accumulated
    ids and request-local drift cannot conceal a knowable atomic veto. Any
    candidate findings are labelled sequence-preview evidence, not disk state.
    """
    reserved, failures = set(), {}
    for group in groups:
        snapshot, findings, history = group["snapshot"], group["findings"], []
        for offset, (index, item) in enumerate(group["eligible"]):
            result, plan = _plan_item(group["target"], item, history, reserved, snapshot, findings)
            if not result.get("success"):
                failures[index] = result
                continue
            if plan is None:
                continue
            proof = verify_beacon_edit_plan(plan, scanner=_beacon_edit_records)
            if not proof["success"]:
                failures[index] = {**proof, "changed": False, "status": "error"}
                continue
            candidate = source_snapshot_from_bytes(proof["candidate_bytes"], snapshot.canonical_path)
            history.append((snapshot.sha256, candidate.sha256, proof["offset_map"]))
            snapshot = candidate
            if offset + 1 < len(group["eligible"]):
                findings, failure = _snapshot_findings(snapshot, group["target"])
                if failure:
                    for later_index, _ in group["eligible"][offset + 1:]:
                        failures[later_index] = failure
                    break
    return failures


def _execute_item(target, item, history, planned_ids):
    # Reservation changes become request state only after a successful actual
    # publication (or a verified no-write state), never from a failed commit.
    reserved = set(planned_ids)
    result, plan = _plan_item(target, item, history, reserved)
    if plan is None:
        if result.get("success"):
            planned_ids.update(reserved)
        return result
    committed = commit_beacon_edit_plan(plan, scanner=_beacon_edit_records)
    result.update(committed)
    result["status"] = "committed" if committed.get("success") else "error"
    if not committed.get("success"):
        result.pop("after_sha256", None)
    if committed.get("success"):
        planned_ids.update(reserved)
        history.append((plan.snapshot.sha256, committed["after_sha256"],
                        EditOffsetMap(tuple(sorted(plan.edits, key=lambda e: (e.start_byte, e.end_byte))))))
    return result


def _outcome(row, result):
    """Replace speculative outcome fields; keep request identity and target only."""
    return {**{key: row[key] for key in ("index", "finding_id", "file", "target", "resolution", "lease") if key in row},
            **result}


async def _release_file(admission, leases, rows, indexed, primary_error):
    """Keep cleanup evidence file-local without hiding actual committed bytes."""
    try:
        await admission.release(leases, primary_error=primary_error)
    except Exception as error:
        if primary_error is not None:
            raise
        for index, _ in indexed:
            rows[index].update(success=False, status="error", error_kind="governance_cleanup_required",
                               error=f"Q3 release failed: {error}", cleanup_error=repr(error))


def _finish(rows, dry_run, atomic, final_findings=None):
    """Use final observations for written files, retaining other row evidence."""
    counts = Counter(row["status"] for row in rows)
    changed = [row for row in rows if row.get("changed")]
    succeeded = sum(bool(row.get("success")) for row in rows)
    files_changed = list(dict.fromkeys(row["file"] for row in changed))
    findings = {}
    for row in rows:
        if canonical_beacon_file(row.get("file") or "") in (final_findings or {}):
            continue
        for finding in row.get("findings", []):
            findings[finding["finding_id"]] = finding
    for file_rows in (final_findings or {}).values():
        for finding in file_rows:
            findings[finding["finding_id"]] = finding
    return {"rows": rows, "files_changed": files_changed, "success": succeeded == len(rows),
            "changed": bool(changed), "partial_success": bool(changed or succeeded) and succeeded < len(rows),
            "summary": dict(counts),
            "summary_line": f"{len(rows)} items · {len(changed)} committed · {counts.get('preview', 0)} previews · {len(rows) - succeeded} failed",
            "findings": list(findings.values()), "dry_run": dry_run, "atomic": atomic,
            "batch_aborted": any(row.get("error_kind") == "atomic_aborted" for row in rows)}


# @beacon[
#   id=beacon-fix@run-beacon-fix,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Groups explicit finding choices under existing Q3 with fresh per-item planning and request-owned offsets; builds the all-path index only for path inputs. After writes observes each committed file once for final full findings and affected-id subsets; previews remain read-only and atomic never rolls back.,
# ]
async def run_beacon_fix(payload, *, admission, resolve_handle):
    """Apply explicit choices, containing each item/file failure in its own row.

    Files run in first-appearance order, items within a file in input order;
    returned rows retain global input order. Dry-run previews are independent
    against one snapshot per file, never a hidden apply prerequisite. Atomic
    preflight/admission also proves the execution sequence in prepared memory,
    but JIT failures never roll back prior writes.
    Written files are observed once more at call end; their final rows replace
    planning findings in the aggregate and each committed row's affected ids,
    while the acted-on finding remains historical evidence under ``finding``.
    Handle-only requests resolve their mapped chain without an all-path index.
    """
    items, rows = normalize_fix_request(payload)
    dry_run, atomic = payload.get("dry_run", False), payload.get("atomic", False)
    entries = (mapped_path_index(admission.cache_by_id)
               if any(item is not None and "file_path" in item for item in items) else [])
    files = {}
    for index, item in enumerate(items):
        if item is None:
            continue
        try:
            target = _resolve_item(item, admission.cache_by_id, entries, resolve_handle)
        except Exception as error:
            target = _failure("invalid_parameter_value", f"{type(error).__name__}: {error}")
        if target.get("error_kind"):
            rows[index].update(target)
            continue
        rows[index].update(file=target["abs_path"], target=target,
                           resolution=item["resolution"], lease="none_unmapped" if target["node_id"] is None else "not_acquired")
        group = files.setdefault(target["canonical_file"], {"target": target, "items": []})
        group["items"].append((index, item))
    planned_ids = set()
    eligible = []
    for group in files.values():
        preflight, group["snapshot"], group["findings"] = await _await_thread_to_completion(
            _preflight_file, group["target"], group["items"], planned_ids)
        group["eligible"] = []
        for index, item in group["items"]:
            result, plan = preflight[index]
            rows[index].update(result)
            if plan is not None and result.get("success"):
                group["eligible"].append((index, item))
                if dry_run:
                    rows[index].update(status="preview", changed=False)
        if group["eligible"]:
            eligible.append(group)
    if dry_run:
        return _finish(rows, True, atomic)
    if atomic and not any(not row.get("success") for row in rows):
        failures = await _await_thread_to_completion(_atomic_sequence_preflight, eligible)
        for index, failure in failures.items():
            rows[index] = _outcome(rows[index], {
                **{key: value for key, value in failure.items() if key != "findings"},
                "findings": rows[index]["findings"], "phase": "atomic_sequence_preflight",
            })
    if atomic and any(not row.get("success") for row in rows):
        for group in eligible:
            for index, _ in group["eligible"]:
                if not rows[index].get("success"):
                    continue
                rows[index] = _outcome(rows[index], {**_failure("atomic_aborted", "atomic preflight veto: another row failed"),
                                                    "findings": rows[index]["findings"]})
        report = _finish(rows, False, True)
        report["batch_aborted"] = True
        return report

    all_leases = []
    primary_error = None
    try:
        if atomic and eligible:
            all_leases, failure = await admission.admit([group["target"] for group in eligible])
            if failure:
                for group in eligible:
                    for index, _ in group["eligible"]:
                        rows[index] = _outcome(rows[index], {**failure, "findings": rows[index]["findings"]})
                report = _finish(rows, False, True)
                report["batch_aborted"] = True
                return report
        for group in eligible:
            leases, file_error = [], None
            try:
                if not atomic:
                    leases, failure = await admission.admit([group["target"]])
                    if failure:
                        for index, _ in group["eligible"]:
                            rows[index] = _outcome(rows[index], {**failure, "findings": rows[index]["findings"]})
                        continue
                history = []
                for index, item in group["eligible"]:
                    rows[index]["lease"] = "q3" if group["target"]["node_id"] is not None else "none_unmapped"
                    try:
                        result = await _await_thread_to_completion(_execute_item, group["target"], item, history, planned_ids)
                    except Exception as error:
                        result = {**_failure("fix_failed", f"{type(error).__name__}: {error}"), "findings": rows[index]["findings"]}
                    if result.get("status") == "committed" and result.get("findings"):
                        result["finding"] = result["findings"][0]
                    rows[index] = _outcome(rows[index], result)
            except BaseException as error:
                file_error = error
                raise
            finally:
                if leases:
                    await _release_file(admission, leases, rows, group["eligible"], file_error)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if all_leases:
            await _release_file(admission, all_leases, rows,
                                [entry for group in eligible for entry in group["eligible"]], primary_error)
    from .beacon_integrity import _observe_beacon_file
    final_findings = {}
    for path, group in files.items():
        if not any(rows[index].get("changed") for index, _ in group["items"]):
            continue
        observation = {}
        analysis_error = None
        try:
            await asyncio.to_thread(_observe_beacon_file, group["target"]["abs_path"], observation=observation)
        except Exception as error:
            analysis_error = {"exception": type(error).__name__, "message": str(error)}
        final_findings[path] = observation.get("rows", [])
        for index, _ in group["items"]:
            row = rows[index]
            if row["status"] != "committed":
                continue
            row["findings"] = [f for f in final_findings[path] if f.get("beacon_id") in row.get("affected_ids", [])]
            row["findings_observation"] = {k: v for k, v in observation.items() if k != "rows"}
            if analysis_error:
                row["analysis_error"] = analysis_error
    return _finish(rows, False, atomic, final_findings)
