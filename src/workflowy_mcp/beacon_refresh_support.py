"""Data-only joins for source preparation, retention and terminal presentation.

The caller owns selection, the live claim, cancellation and Workflowy execution.
These components do not run jobs or acquire claims. Protected gesture callers
consume them only after work package J; no import here changes those callers.
"""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
import os

from beacon_source import read_source_snapshot
from beacon_findings import (
    analyze_beacon_snapshot, beacon_snapshot_analysis_inputs, make_beacon_finding,
)
from .client.api_client_nexus import (
    _BEACON_LANGUAGE_BY_EXT, _import_cartographer_module,
    _is_cartographer_parse_error_sentinel, _WARN_NAME_TIMESTAMP_SUFFIX_RE,
    parse_cartographer_note_for_preview,
)
# The per-key identity and the receipt tally are the fold's own (§2.5): the
# accumulator and the reader must agree on one canonical file key and one tally.
from .carto_job_record import canonical_carto_file, tally_bulk_receipt


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.get("children") or [])


def _parse_errors(tree):
    return [{"name": n.get("name"), "note": n.get("note"),
             "section": bool(_section_key(parent))}
            for parent, n in _parents(tree)
            if _outline_unavailable(n)]


def _outline_unavailable(node):
    name = str(node.get("name") or "").strip()
    if _is_cartographer_parse_error_sentinel(name):
        return True
    match = _WARN_NAME_TIMESTAMP_SUFFIX_RE.match(name)
    base = match.group("base") if match else name
    return base in {"⚠️ JS/TS parsing not available", "⚠️ Vue parsing not available"}


def _parents(node):
    for child in node.get("children") or []:
        yield node, child
        yield from _parents(child)


def _section_key(node):
    # These are the mapper's explicit section identities, not source inference.
    fields = dict(line.split(": ", 1) for line in str(node.get("note") or "").splitlines()
                  if line.startswith(("VUE_SECTION: ", "VUE_START_TAG: ")))
    return (fields.get("VUE_SECTION"), fields.get("VUE_START_TAG")) if fields.get("VUE_SECTION") else None


def _finding_totals(result, rows):
    result.update(findings=rows[:200], findings_total=len(rows),
                  findings_by_code=dict(sorted(Counter(f["code"] for f in rows).items())),
                  findings_truncated=max(0, len(rows) - 200))


# @beacon[
#   id=beacon-refresh@prepare_beacon_source,
#   slice_labels=carto-fwd-source,beacon-writeback,
#   kind=ast,
#   comment=Prepares one final snapshot through claimed C0 alias and group hygiene then the shared map and finding core; optional alias/group exceptions become repair and warning data while later passes continue. Retains physical duplicate evidence and fresh external bytes after a refused commit without rerunning hygiene.,
# ]
def prepare_beacon_source(file_path, *, dry_run=False, claim_id=None, repo_root=None,
                          cancel_check=None, log_callback=None):
    """Return one map and current findings, beside unrecomputable repair events.

    Only a caller-supplied live claim authorizes the existing optional writers.
    Each pass owns its snapshot/commit; this adapter observes once after them,
    never reopens for mapping/checking. A CAS refusal ends optional hygiene and
    that one final read observes the external save (BR-02), not the stale plan.
    Dry/unclaimed paths never call a writer. ``snapshot`` and ``analysis_inputs``
    are in-process handoff values; aggregation omits them from serializable data.
    Physical duplicate occurrences project the existing core's verdict, including
    copies absent from the outline; no mapped-note count or extra read decides it.
    Optional alias/group exceptions are diagnostic data, not failed preparation;
    later passes and the single final observation still run. C0 and cancellation
    retain their existing exception semantics. A file type with no beacon
    language keeps the mapper's plain leaf and an explicit analysis skip
    (``skip_reason``): that is how the system works, never a warning.
    """
    source = _import_cartographer_module()
    path = os.path.realpath(os.fspath(file_path))
    language = _BEACON_LANGUAGE_BY_EXT.get(os.path.splitext(path)[1].lower())
    result = {"file": path, "status": "prepared", "changed": False, "findings": [],
              "warnings": [], "events": [], "repair_outcomes": [], "snapshot": None,
              "analysis_inputs": None, "diagnostics": [],
              "check_target": {"verb": "check", "params": {"file_path": path, "min_severity": "info"}}}
    if language is not None and not dry_run and claim_id:
        passes = []
        if language == "markdown":
            passes.append(("c0", lambda: source.repair_markdown_c0_controls_on_disk(
                path, claim_id=claim_id, cancel_check=cancel_check, log_callback=log_callback)))
        passes.extend([
            ("aliases", lambda: source.normalize_beacon_comment_field_aliases_in_file(path)),
            ("groups", lambda: source.normalize_duplicate_ast_beacons_in_file(path)),
        ])
        for name, run in passes:
            # Cancellation is infrastructure, never a content/analysis finding.
            if cancel_check is not None and cancel_check():
                raise RuntimeError("Beacon preparation cancelled before optional hygiene.")
            if name in {"aliases", "groups"}:
                try:
                    outcome = run()
                except Exception as error:
                    detail = f"{type(error).__name__}: {error}"
                    label = "comment-field alias" if name == "aliases" else "duplicate AST beacon"
                    message = (f"refresh_file_node_beacons: {label} normalization failed "
                               f"for {path}: {detail}")
                    outcome = {"success": False, "changed": False, "error": detail,
                               "error_kind": "optional_hygiene_failed"}
                    result["warnings"].append({"type": "optional_hygiene_failed", "pass": name,
                                               "file": path, "message": message, "error": detail})
                    if log_callback is not None:
                        log_callback(message)
            else:
                outcome = run()
            result["repair_outcomes"].append({"pass": name, **outcome})
            result["events"].extend(copy.deepcopy(outcome.get("events") or []))
            result["warnings"].extend(copy.deepcopy(outcome.get("findings") or []))
            result["changed"] |= bool(outcome.get("success") and outcome.get("changed"))
            if outcome.get("error_kind") == "source_changed_since_plan":
                break
    try:
        snapshot = read_source_snapshot(path)
    except (UnicodeDecodeError, OSError) as error:
        kind = "not_utf8" if isinstance(error, UnicodeDecodeError) else "io_error"
        identity = {"state": "unreadable", "exception_type": type(error).__name__}
        if isinstance(error, UnicodeDecodeError):
            identity.update(undecodable_bytes_sha256=hashlib.sha256(error.object).hexdigest(),
                            start=error.start, end=error.end, reason=error.reason)
        else:
            identity["errno"] = error.errno
        code = "file_missing" if isinstance(error, FileNotFoundError) else "file_unreadable"
        if code == "file_missing":
            identity["state"] = "missing"
        result.update(status="failed", error=str(error), error_kind=kind,
                      unreadable_identity=identity)
        result["findings"] = [make_beacon_finding(
            path, 0, code, None, f"Source preparation cannot read this file ({type(error).__name__}: {error}).",
            {"exception": f"{type(error).__name__}: {error}", "error_kind": kind},
            unreadable_identity=identity, producer="beacon.refresh")]
        # Do not invent a line count or a decoded snapshot (BR-09).
        result["map"] = source._source_file_error(
            path, error, repo_root=repo_root, diagnostics=result["diagnostics"])
        if language is None:
            # Generic binary leaves are not failed semantic outlines. Keep
            # the mapper's existing leaf behavior and an explicit analysis skip.
            result.update(status="skipped", skip_reason="unsupported_extension", findings=[])
        return result
    result["snapshot"] = snapshot
    result["file_sha256"] = snapshot.sha256
    tree = source.map_source_snapshot(snapshot, repo_root=repo_root, diagnostics=result["diagnostics"])
    result["map"] = tree
    errors = _parse_errors(tree)
    if errors:
        result["status"] = "failed" if any(not e["section"] for e in errors) else "partial_section"
    if language is None:
        result.update(status="skipped", skip_reason="unsupported_extension")
        return result
    try:
        inputs = beacon_snapshot_analysis_inputs(
            snapshot, language, source, outline=tree.get("children") or [], mapped_roots=[tree],
            parse_note=parse_cartographer_note_for_preview, mapper_errors=errors)
        result["analysis_inputs"] = inputs
        if "section_error" in inputs:
            result["findings"] = [make_beacon_finding(
                path, 0, "mapper_parse_error", None, "The Vue section analysis failed; independent scopes remain usable.",
                {"exception": inputs["section_error"]}, snapshot=snapshot, producer="beacon.refresh")]
        else:
            result["duplicate_source_beacon_ids"] = {
                bid: [{"name": bid, "path": f"{path}:{record['line']}", "line": record["line"]}
                      for record in inputs["structures"]["records"]
                      if record["marker"] == "open" and str(record["fields"].get("id") or "").strip() == bid]
                for bid in inputs["duplicate_ids"]}
            result["findings"] = analyze_beacon_snapshot(snapshot, **inputs, producer="beacon.refresh")
    except Exception as error:
        result["analysis_error"] = {"exception": type(error).__name__, "message": str(error)}
        result["findings"] = [make_beacon_finding(
            path, 0, "check_failed", None, "The file analyzer failed; its independent map remains applicable.",
            {"exception": f"{type(error).__name__}: {error}"}, snapshot=snapshot, producer="beacon.refresh")]
    if errors:
        result["warnings"].extend(errors)
    return result


# @beacon[
#   id=beacon-refresh@beacon_legacy_outcome,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Delegates legacy evidence projection and classifies only explicit satisfied or safe-absence noops for J; unknown identity and rejected edits stay failures while the branch bulk seam consumes additive data without changing its trigger.,
# ]
def beacon_legacy_outcome(writer_result):
    """Project R7's precise satisfied reasons through U06's evidence adapter.

    The legacy wire synonyms for safe absence project to U06's already_absent
    vocabulary. This does not infer satisfaction from an unknown noop, nor turn
    an error into success. J consumes this classification in protected gates;
    the allowed bulk seam uses only its additive data until that join.
    """
    from .beacon_batch import project_plan_result

    reason = writer_result.get("noop_reason") or writer_result.get("reason")
    if reason in {"labels_already_absent", "no_beacon_at_target"}:
        reason = "already_absent"
    result = project_plan_result(None, writer_result)
    result["noop_reason"] = reason
    satisfied = (result.get("operation") == "noop" and not result.get("error") and
                 reason in {"already_satisfied", "already_absent"})
    result.update(writer_success=writer_result.get("success"), satisfied=satisfied)
    if result.get("error") or result.get("operation") in {None, "noop"}:
        result["success"] = satisfied
    else:
        result.setdefault("success", True)
    return result


# @beacon[
#   id=beacon-refresh@beacon_refresh_projection,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Projects explicit map diagnostics into whole-file retention or section-local reconciliation data before sentinel flattening; valid empty files remain authoritative and existing reconcile ambiguities stay visible without executing Workflowy operations.,
# ]
def beacon_refresh_projection(prepared, *, existing_children=()):
    """Describe the reconcile boundary; never seed, create, delete or write.

    ``reconcile_children=None`` means NO replacement plan for this scope, not
    an empty tree. For Vue, unique mapper section type/start-tag identities
    permit healthy sections to proceed while the failed subtree is retained.
    An unidentifiable failure retains the file, rather than guessing a section.
    J must consume this policy before promoting diagnostics or seeding IDs.
    """
    tree = prepared["map"]
    children = tree.get("children") or []
    failures = [n for n in _walk(children) if _outline_unavailable(n)]
    failed_sections = [n for n in children if _section_key(n) and
                       any(f is sub for sub in _walk([n]) for f in failures)]
    # Every failure must be within a uniquely identifiable top-level section.
    localized = bool(failures) and all(
        sum(any(f is sub for sub in _walk([section])) for section in failed_sections) == 1
        for f in failures)
    existing = list(existing_children)
    retained, sections = [], []
    if localized:
        for section in failed_sections:
            key = _section_key(section)
            matches = [n for n in existing if _section_key(n) == key]
            if sum(_section_key(n) == key for n in children) != 1 or len(matches) > 1:
                localized = False
                break
            retained.extend(matches)
            sections.append({"identity": list(key), "preserve_existing": True,
                             "existing_uuid": matches[0].get("id") if matches else None,
                             "reconcile_children": None, "create_allowed": False, "delete_allowed": False})
    preserve = bool(failures or (prepared.get("status") == "failed" and prepared.get("error"))) and not localized
    if preserve:
        retained = existing
    state = "parse_failed" if preserve else "partial_section" if failures else "valid"
    report = copy.deepcopy(tree.get("_beacon_reconcile_report") or {})
    return {"state": state, "preserve_existing": preserve,
            "reconcile_children": None if preserve else [n for n in children if n not in failed_sections],
            "create_allowed": not preserve, "delete_allowed": not preserve,
            "sections": sections if localized else [],
            "retained_uuids": [n["id"] for n in _walk(retained) if n.get("id")],
            "retained_sections": copy.deepcopy(retained) if localized else [],
            "findings": copy.deepcopy(prepared.get("findings") or []),
            "warnings": copy.deepcopy(prepared.get("warnings") or []),
            "reconcile_report": report, "ambiguities": report.get("ambiguous", []),
            "ambiguous_count": report.get("ambiguous_count", len(report.get("ambiguous", [])))}


# @beacon[
#   id=beacon-refresh@aggregate_beacon_outcomes,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Shallow-copies each attempted file row and supplies missing defaults without changing caller rows; keeps complete evidence and historical events beside a 200-finding preview with exact totals; performs no IO or analysis.,
# ]
def aggregate_beacon_outcomes(outcomes, *, warnings=(), events=()):
    """Aggregate final per-file observations, not every candidate's analysis.

    Supply one final finding set per file version. Per-node historical outcomes
    belong inside that file's detail. Neither the preview nor a severity changes
    file success. Full current findings and all historic events remain in files.
    Rows are shallow copies: only their missing top-level defaults are written.
    """
    files = []
    findings, all_warnings, all_events = [], list(copy.deepcopy(warnings)), list(copy.deepcopy(events))
    for outcome in outcomes:
        row = {k: v for k, v in outcome.items() if k not in {"snapshot", "analysis_inputs"}}
        row.setdefault("status", "failed" if row.get("error") else "completed")
        row.setdefault("changed", False)
        row.setdefault("findings", [])
        row.setdefault("check_target", {"verb": "check", "params": {"file_path": row["file"], "min_severity": "info"}})
        files.append(row)
        findings.extend(row["findings"])
        all_warnings.extend(row.get("warnings") or [])
        all_events.extend(row.get("events") or [])
        all_events.extend(row.get("omissions") or [])
        all_events.extend(row.get("relocations") or [])
    changed = list(dict.fromkeys(r["file"] for r in files if r["changed"]))
    result = {"files": files, "warnings": all_warnings, "warnings_total": len(all_warnings),
              "warnings_by_type": dict(sorted(Counter(
                  str(w.get("type") or w.get("code") or w.get("reason") or "warning")
                  if isinstance(w, dict) else "message" for w in all_warnings).items())),
              "warnings_truncated": 0, "events": all_events, "files_changed": changed,
              "changed": bool(changed)}
    _finding_totals(result, findings)
    result["summary_line"] = f"{len(files)} files · {len(findings)} findings · {len(all_events)} events"
    return result


# @beacon[
#   id=beacon-refresh@beacon_job_outcome,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Presents complete file outcomes with exact success failure skip counts; an owned aggregate avoids a run-wide deep copy and copies only phase8-mutated containers and rows; cancellation and infrastructure stay failures while committed disk work survives refresh failure.,
# ]
def beacon_job_outcome(aggregate, *, infrastructure_error=None, cancelled=False, phase8=None,
                       owns_aggregate=False):
    """Format terminal data independently of work already completed.

    Failed/skipped files never erase successful neighbours. Error-severity
    findings alone are advisory. ``phase8`` is a separate follow-up outcome,
    not a rollback of ``files_changed`` or of a committed file's disk outcome.
    Its ``files`` carry complete final findings (even [] for a clean file), not
    a capped top-level preview. They replace that file's pre-refresh finding
    set; historic disk events and the original publication outcome remain.
    ``owns_aggregate`` consumes a just-built aggregate the caller discards;
    phase8 still copies the containers and rows it changes, never caller rows.
    """
    result = aggregate if owns_aggregate else copy.deepcopy(aggregate)
    files = result["files"]
    failed = sum(r["status"] in {"failed", "error", "unavailable", "parse_failed"} for r in files)
    skipped = sum(r["status"] == "skipped" for r in files)
    successful = len(files) - failed - skipped
    useful = successful > 0 or bool(result["files_changed"])
    phase8_failed = bool(phase8 is not None and (phase8.get("success") is False or phase8.get("error")))
    if phase8 is not None:
        if owns_aggregate:
            result["warnings"] = list(result["warnings"])
            files = result["files"] = list(files)
        result["phase8"] = copy.deepcopy(phase8)
        result["warnings"].extend(copy.deepcopy(phase8.get("warnings") or []))
        refreshed = {r["file"]: r for r in phase8.get("files") or []}
        for index, row in enumerate(files):
            current = refreshed.pop(row["file"], None)
            if current is not None:
                if owns_aggregate:
                    row = files[index] = dict(row)
                row["findings"] = copy.deepcopy(current.get("findings") or [])
                row["refresh_outcome"] = copy.deepcopy(current)
                result["warnings"].extend(copy.deepcopy(current.get("warnings") or []))
        # Newly discovered refresh files remain complete under phase8.files.
        for current in refreshed.values():
            result["warnings"].extend(copy.deepcopy(current.get("warnings") or []))
        full = [f for row in files for f in row["findings"]]
        full.extend(f for row in refreshed.values() for f in row.get("findings") or [])
        _finding_totals(result, full)
        if phase8_failed:
            result["warnings"].append({"type": "phase8_refresh_failed", "error": phase8.get("error"),
                                       "disk_committed": bool(result["files_changed"])})
    problematic = bool(failed or skipped or result["findings_total"] or result["warnings"] or result["events"])
    status = ("failed" if infrastructure_error is not None or cancelled or not useful else
              "completed_with_warnings" if problematic else "completed")
    result.update(status=status, successful=successful, failed=failed, skipped=skipped,
                  partial_success=bool(useful and (failed or skipped or phase8_failed or infrastructure_error is not None or cancelled)),
                  success=status != "failed", cancelled=bool(cancelled))
    if infrastructure_error is not None:
        result["infrastructure_error"] = copy.deepcopy(infrastructure_error)
    result["warnings_total"] = len(result["warnings"])
    result["warnings_by_type"] = dict(sorted(Counter(
        str(w.get("type") or w.get("code") or w.get("reason") or "warning")
        if isinstance(w, dict) else "message" for w in result["warnings"]).items()))
    result["summary_line"] = (f"{successful} successful · {failed} failed · {skipped} skipped · "
                              f"{result['findings_total']} findings · {len(result['events'])} events")
    return result


# ---------------------------------------------------------------------------
# G3 · the incremental terminal accumulator (§2.7)
#
# The two functions above stay exactly as JR1 wrote them: they are the batch
# composition the accumulator must equal at every prefix.  Everything below is
# a summary kept per key and counters updated only where a put touches them --
# no row, finding, receipt or event object outlives the call that supplied it.
# ---------------------------------------------------------------------------

_FAILED_ROW_STATUSES = frozenset({"failed", "error", "unavailable", "parse_failed"})
_SLIM_FINDING_KEYS = ("code", "severity", "message", "file", "line", "finding_id")
_FINDINGS_PREVIEW = 20
_FILES_CHANGED_PREVIEW = 50
_EMPTY_TALLY = {"count": 0, "satisfied": 0, "changed": False, "rejected_error": None}


def _slim_finding(finding):
    """The six fields §2.2's ``findings_preview`` carries."""
    return {key: finding.get(key) for key in _SLIM_FINDING_KEYS}


def _warning_type(warning):
    """``beacon_job_outcome``'s one warning-type expression."""
    return (str(warning.get("type") or warning.get("code") or warning.get("reason") or "warning")
            if isinstance(warning, dict) else "message")


def _empty_findings_part():
    return {"count": 0, "codes": Counter(), "severities": Counter(), "preview": []}


def _empty_warnings_part():
    return {"count": 0, "types": Counter()}


def _extend_findings(part, values):
    """Add findings to a contribution, keeping the first ≤20 slim ones."""
    for finding in values or ():
        finding = finding if isinstance(finding, dict) else {}
        part["count"] += 1
        part["codes"][finding.get("code", "")] += 1
        part["severities"][str(finding.get("severity") or "info")] += 1
        if len(part["preview"]) < _FINDINGS_PREVIEW:
            part["preview"].append(_slim_finding(finding))
    return part


def _findings_part(values):
    return _extend_findings(_empty_findings_part(), values)


def _copy_findings_part(part):
    return {"count": part["count"], "codes": Counter(part["codes"]),
            "severities": Counter(part["severities"]), "preview": list(part["preview"])}


def _extend_warnings(part, values):
    """Add warnings to a contribution, keeping the count and the type tally."""
    for warning in values or ():
        part["count"] += 1
        part["types"][_warning_type(warning)] += 1
    return part


def _warnings_part(values):
    return _extend_warnings(_empty_warnings_part(), values)


def _add_warning_type(part, warning_type):
    part["count"] += 1
    part["types"][warning_type] += 1


def _part_add(target, part):
    """Add one findings contribution into an aggregate one (counts only)."""
    if part is None:
        return
    target["count"] += part["count"]
    for code, count in part["codes"].items():
        target["codes"][code] += count
    for severity, count in part["severities"].items():
        target["severities"][severity] += count


def _part_sub(target, part):
    """Remove one findings contribution from an aggregate one."""
    if part is None:
        return
    target["count"] -= part["count"]
    for source, destination in ((part["codes"], target["codes"]),
                                (part["severities"], target["severities"])):
        for key, count in source.items():
            remaining = destination.get(key, 0) - count
            if remaining:
                destination[key] = remaining
            else:
                destination.pop(key, None)


def _warning_part_add(target, part):
    if part is None:
        return
    target["count"] += part["count"]
    for name, count in part["types"].items():
        target["types"][name] += count


def _warning_part_sub(target, part):
    if part is None:
        return
    target["count"] -= part["count"]
    for name, count in part["types"].items():
        remaining = target["types"].get(name, 0) - count
        if remaining:
            target["types"][name] = remaining
        else:
            target["types"].pop(name, None)


def _event_counts(row):
    """The three event sources the aggregate reads, counted per source key."""
    return {name: len(list(row.get(name) or ()))
            for name in ("events", "omissions", "relocations")}


def _combined(*counters):
    total = Counter()
    for counter in counters:
        total.update(counter)
    return dict(sorted(total.items()))


class _PreviewNode:
    """One segment of the ordered contribution index with a bounded prefix."""

    __slots__ = ("lo", "hi", "left", "right", "changed", "findings")

    def __init__(self, lo, hi):
        self.lo, self.hi = lo, hi
        self.left = None
        self.right = None
        self.changed = []
        self.findings = []


def _merge_preview(node):
    """Recompute one node's two bounded prefixes from its children."""
    left, right = node.left, node.right
    changed = list(left.changed) if left is not None else []
    if right is not None and len(changed) < _FILES_CHANGED_PREVIEW:
        seen = set(changed)
        for label in right.changed:
            if label in seen:
                continue
            seen.add(label)
            changed.append(label)
            if len(changed) >= _FILES_CHANGED_PREVIEW:
                break
    node.changed = changed
    findings = ((left.findings if left is not None else [])
                + (right.findings if right is not None else []))
    node.findings = findings[:_FINDINGS_PREVIEW]


class _PreviewSegment:
    """First-appearance order with an exact bounded prefix at every node.

    A leaf is one key's block (its changed file label and its first ≤20 slim
    findings).  ``set_block`` repairs the leaf-to-root path only, so a put costs
    O(p·log₂ n) for p ≤ 70 bounded entries and never scans a key; the root's
    caches are the previews, which ``outcome`` reads without touching a node.
    """

    __slots__ = ("_root", "_capacity", "node_visits", "_changed", "_findings")

    def __init__(self):
        self._root = _PreviewNode(0, 1)
        self._capacity = 1
        self.node_visits = 0
        self._changed, self._findings = [], []

    @property
    def changed(self):
        return list(self._changed)

    @property
    def findings(self):
        return list(self._findings)

    def _expand(self):
        """Double the root in O(1): the old root becomes the new left half."""
        root = _PreviewNode(0, self._capacity * 2)
        root.left = self._root
        _merge_preview(root)
        self._root, self._capacity = root, self._capacity * 2

    def set_block(self, ordinal, *, changed_label=None, findings=()):
        while self._capacity <= ordinal:
            self._expand()
        node = self._root
        self.node_visits += 1
        path = [node]
        while node.hi - node.lo > 1:
            middle = (node.lo + node.hi) // 2
            if ordinal < middle:
                if node.left is None:
                    node.left = _PreviewNode(node.lo, middle)
                node = node.left
            else:
                if node.right is None:
                    node.right = _PreviewNode(middle, node.hi)
                node = node.right
            self.node_visits += 1
            path.append(node)
        node.changed = [changed_label] if changed_label is not None else []
        node.findings = list(findings)[:_FINDINGS_PREVIEW]
        for parent, child in zip(reversed(path[:-1]), reversed(path[1:])):
            # Count distinct examined nodes, not only the descent: the other
            # child's bounded cache is read too. Ancestors/path children were
            # already counted on descent. At depth h this is at most 2h+1.
            sibling = parent.right if parent.left is child else parent.left
            self.node_visits += int(sibling is not None)
            _merge_preview(parent)
        self._changed = list(self._root.changed)
        self._findings = list(self._root.findings)


class _KeySummary:
    """One key's contribution summary -- never a row (§2.7)."""

    __slots__ = ("identity", "section", "file", "segment", "ordinal", "status",
                 "status_present", "error", "changed", "findings", "warnings", "events",
                 "local_cpu_failure", "nodes_satisfied", "reflected", "bulk", "tally",
                 "phase8", "registered")

    def __init__(self, section, file):
        self.identity = None
        self.section = section
        self.file = file
        self.segment = "rows"
        self.ordinal = 0
        self.status = None
        self.status_present = False
        self.error = False
        self.changed = False
        self.findings = _empty_findings_part()
        self.warnings = _empty_warnings_part()
        self.events = {"events": 0, "omissions": 0, "relocations": 0}
        self.local_cpu_failure = None
        self.nodes_satisfied = False
        self.reflected = 0
        self.bulk = False
        self.tally = None
        self.phase8 = None
        self.registered = None


# @beacon[
#   id=beacon-refresh@carto-outcome-accumulator,
#   role=CartoOutcomeAccumulator(warnings, events).put(row, section, delta, nodes_reflected),
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=The batch composition in incremental form: one contribution summary per key and counters updated only for the parts a put, a delta, a receipt or a phase-8 envelope touches; first-appearance order is repaired along one leaf-to-root path per bounded-prefix segment, so outcome reads counters and materialized previews only and never a row, a key summary or an index node.,
# ]
class CartoOutcomeAccumulator:
    """The batch composition for every prefix, without holding a single row.

    ``put`` keeps one summary per ``(section, canonical file)`` -- status
    presence and raw class, error presence, ``changed``, the findings
    contribution (count, code and severity multiplicities, the first ≤20 slim
    findings), the warnings contribution (count and type multiplicities), the
    event counts per source key, a failed row's ``local_cpu_failure`` identity,
    and, for a bulk key, its receipt tally and the receipt count its latest line
    reflects.  A full put replaces every part; a delta line's ``{unset, extend}``
    overlays only the parts whose source keys it touches, in the fold's
    unset → set → extend order.

    Order is kept by three bounded-prefix segments: the rows' first-put order,
    the receipt-only base rows' first-receipt order (which follow every row, as
    the fold places them), and the phase-8-only finding blocks (which follow
    both, in the envelope's own order). Phase-8 rows may arrive individually
    before or after their envelope. Only replacing an explicit envelope's
    complete files list walks that list; a put never walks earlier keys.
    """

    def __init__(self, *, warnings=(), events=()):
        self._keys = {}
        self._row_order = []
        self._base_order = []
        self._row_ordinals = 0
        self._base_ordinals = 0
        self._row_count = 0
        self._failed_rows = 0
        self._skipped_rows = 0
        self._node_errors = 0
        self._label_changed = Counter()
        self._findings = _empty_findings_part()
        self._key_warnings = _empty_warnings_part()
        self._key_events = 0
        self._local_failures = {}
        self._local_failure_counts = Counter()
        self._primary_by_label = {}
        self._rows = _PreviewSegment()
        self._receipt_bases = _PreviewSegment()
        self._phase8_only = _PreviewSegment()
        self._phase8_findings = _empty_findings_part()
        self._phase8_warnings = _empty_warnings_part()
        self._phase8_rows = []
        self._phase8_pending = {}
        self._phase8_envelope = None
        self._phase8_matched = []
        self._phase8_failed = False
        self._phase8_keys = {}
        self._phase8_by_label = {}
        self._phase8_ordinals = 0
        self._phase8_failed_rows = 0
        self._phase8_envelope_warnings = _empty_warnings_part()
        self._run_warnings = _warnings_part(warnings)
        self._run_events = len(list(events or ()))
        self._findings_preview = []
        self._files_changed_preview = []
        self._refresh_previews()

    # -- run-level entries --------------------------------------------------
    def add_run_warning(self, warning):
        """Add one run-level warning (the aggregate's ``warnings=`` list)."""
        _extend_warnings(self._run_warnings, (warning,))

    def add_run_event(self, event):
        """Count one run-level event (the aggregate's ``events=`` list)."""
        self._run_events += 1

    # -- previews -----------------------------------------------------------
    def _refresh_previews(self):
        """Materialize the two previews ``outcome`` returns, from the roots."""
        findings = (self._rows.findings + self._receipt_bases.findings
                    + self._phase8_only.findings)[:_FINDINGS_PREVIEW]
        self._findings_preview = findings
        changed = list(self._rows.changed)
        if len(changed) < _FILES_CHANGED_PREVIEW:
            seen = set(changed)
            for label in self._receipt_bases.changed:
                if label in seen:
                    continue
                seen.add(label)
                changed.append(label)
                if len(changed) >= _FILES_CHANGED_PREVIEW:
                    break
        self._files_changed_preview = changed

    # -- the key summary ----------------------------------------------------
    def _key_for(self, section, canonical, file):
        key = self._keys.get((section, canonical))
        if key is not None:
            return key, False
        key = _KeySummary(section, file)
        key.identity = (section, canonical)
        self._keys[(section, canonical)] = key
        if section == "files":
            key.segment, key.ordinal = "rows", self._row_ordinals
            self._row_ordinals += 1
            self._row_order.append(key)
            self._row_count += 1
        return key, True

    def _segment_for(self, key):
        return self._receipt_bases if key.segment == "bases" else self._rows

    def _composed_status(self, key):
        status = key.status if key.status_present else ("failed" if key.error else "completed")
        error = key.error
        if self._applies_receipts(key) and (key.tally or _EMPTY_TALLY)["rejected_error"]:
            status, error = "failed", True
        return status, error

    def _applies_receipts(self, key):
        """§2.5's recompute condition, from presence bits alone."""
        if key.tally is None and not key.bulk:
            return False
        return ((key.tally or _EMPTY_TALLY)["count"] > key.reflected
                or not key.nodes_satisfied)

    def _composed_changed(self, key):
        changed = bool(key.changed)
        if self._applies_receipts(key):
            changed = changed or bool((key.tally or _EMPTY_TALLY)["changed"])
        return changed

    def _register(self, key):
        """Recompute one key's composition into the counters and the index."""
        if key.section == "phase8":
            self._register_phase8(key)
            return
        previous = key.registered
        if key.section == "files":
            old_file = previous.get("file") if previous is not None else None
            if old_file != key.file or previous is None:
                if old_file is not None and self._primary_by_label.get(old_file) is key:
                    self._primary_by_label.pop(old_file, None)
                self._primary_by_label[key.file] = key
                key.phase8 = None
                for label in dict.fromkeys((old_file, key.file)):
                    phase = self._phase8_by_label.get(label)
                    if phase is not None:
                        self._register_phase8(phase, skip_primary=key)
        status, error = self._composed_status(key)
        changed = self._composed_changed(key)
        findings = key.phase8 if key.phase8 is not None else key.findings
        files_section = key.section == "files"
        findings_changed = files_section and (previous is None or previous["findings"] is not findings)
        warnings_changed = files_section and (previous is None or previous["warnings"] is not key.warnings)
        if previous is not None:
            self._failed_rows -= previous["failed"]
            self._skipped_rows -= previous["skipped"]
            if previous["label"] is not None:
                self._label_changed[previous["label"]] -= 1
                if not self._label_changed[previous["label"]]:
                    del self._label_changed[previous["label"]]
            if findings_changed:
                _part_sub(self._findings, previous["findings"])
            if warnings_changed:
                _warning_part_sub(self._key_warnings, previous["warnings"])
            self._key_events -= previous["events"]
        failed = int(files_section and status in _FAILED_ROW_STATUSES)
        skipped = int(files_section and status == "skipped")
        events = sum(key.events.values()) if files_section else 0
        self._failed_rows += failed
        self._skipped_rows += skipped
        self._key_events += events
        if files_section:
            if changed:
                self._label_changed[key.file] += 1
            if findings_changed:
                _part_add(self._findings, findings)
            if warnings_changed:
                _warning_part_add(self._key_warnings, key.warnings)
        key.registered = {"failed": failed, "skipped": skipped,
                          "file": key.file,
                          "label": (key.file if changed else None) if files_section else None,
                          "findings": findings if files_section else None,
                          "warnings": key.warnings if files_section else None,
                          "events": events}
        self._set_local_failure(key.identity, key.file, status, error, key.local_cpu_failure,
                                enabled=files_section)
        if files_section:
            self._segment_for(key).set_block(
                key.ordinal, changed_label=(key.file if changed else None),
                findings=findings["preview"])
            self._refresh_previews()

    # -- putting rows -------------------------------------------------------
    def put(self, row, *, section="files", delta=None, nodes_reflected=None):
        """Add or replace (or, with ``delta``, overlay) one key's contribution."""
        if not isinstance(row, dict):
            raise TypeError("put() needs a row mapping")
        section = str(section or "files")
        canonical = canonical_carto_file(row.get("file"))
        key, created = self._key_for(section, canonical, row.get("file"))
        if section == "phase8" and created:
            key.ordinal = self._phase8_ordinals
            self._phase8_ordinals += 1
            self._phase8_keys[canonical] = key
        # A delta line with no earlier line for its key (a lost line, or a key
        # the fold knows only from node receipts) is overlaid on §2.5's base row.
        seed_base = delta is not None and (created or key.segment == "bases")
        if key.segment == "bases":
            # The fold places a key with a row line in first-line order, whatever
            # came before it: a promoted base row leaves its receipt-order slot.
            self._receipt_bases.set_block(key.ordinal)
            key.segment, key.ordinal = "rows", self._row_ordinals
            self._row_ordinals += 1
            self._row_order.append(key)
        if seed_base:
            self._seed_base_parts(key)
        if delta is None:
            self._replace_parts(key, row, nodes_reflected)
        else:
            self._overlay_parts(key, row, delta, nodes_reflected)
        self._register(key)

    def _replace_parts(self, key, row, nodes_reflected):
        key.file = row.get("file")
        key.status_present = "status" in row
        key.status = row.get("status")
        key.error = bool(row.get("error"))
        key.changed = bool(row.get("changed"))
        key.findings = _findings_part(row.get("findings"))
        key.warnings = _warnings_part(row.get("warnings"))
        key.events = _event_counts(row)
        key.local_cpu_failure = copy.deepcopy(row.get("local_cpu_failure"))
        key.nodes_satisfied = "nodes_satisfied" in row
        key.reflected = int(nodes_reflected or 0) if nodes_reflected is not None else 0
        if nodes_reflected is not None:
            key.bulk = True

    def _seed_base_parts(self, key):
        """§2.5's ``_bulk_base_row``: a delta line with no earlier line for its key."""
        key.status_present, key.status = True, "completed"
        key.error = False
        key.changed = False
        key.findings = _empty_findings_part()
        key.warnings = _empty_warnings_part()
        key.events = {"events": 0, "omissions": 0, "relocations": 0}
        key.local_cpu_failure = None
        key.nodes_satisfied = False

    def _overlay_parts(self, key, row, delta, nodes_reflected):
        """Recompute only the parts whose source keys this delta line touches."""
        unset = set(delta.get("unset") or ())
        extend = delta.get("extend") or {}
        if "file" in row:
            key.file = row.get("file")
        if "status" in row:
            key.status_present, key.status = True, row["status"]
        elif "status" in unset:
            key.status_present, key.status = False, None
        if "error" in row:
            key.error = bool(row["error"])
        elif "error" in unset:
            key.error = False
        if "changed" in row:
            key.changed = bool(row["changed"])
        elif "changed" in unset:
            key.changed = False
        if "local_cpu_failure" in row:
            key.local_cpu_failure = copy.deepcopy(row["local_cpu_failure"])
        elif "local_cpu_failure" in unset:
            key.local_cpu_failure = None
        if "nodes_satisfied" in row:
            key.nodes_satisfied = True
        elif "nodes_satisfied" in unset:
            key.nodes_satisfied = False
        if "findings" in row:
            key.findings = _findings_part(row["findings"])
        elif "findings" in unset:
            key.findings = _empty_findings_part()
        elif "findings" in extend:
            key.findings = _copy_findings_part(key.findings)
        if "findings" in extend:
            _extend_findings(key.findings, extend["findings"])
        if "warnings" in row:
            key.warnings = _warnings_part(row["warnings"])
        elif "warnings" in unset:
            key.warnings = _empty_warnings_part()
        elif "warnings" in extend:
            key.warnings = {"count": key.warnings["count"],
                            "types": Counter(key.warnings["types"])}
        if "warnings" in extend:
            _extend_warnings(key.warnings, extend["warnings"])
        for name in ("events", "omissions", "relocations"):
            if name in row:
                key.events[name] = len(list(row[name] or ()))
            elif name in unset:
                key.events[name] = 0
            if name in extend:
                key.events[name] += len(list(extend[name] or ()))
        key.reflected = int(nodes_reflected or 0) if nodes_reflected is not None else 0
        if nodes_reflected is not None:
            key.bulk = True

    # -- bulk receipts ------------------------------------------------------
    def put_nodes(self, receipts):
        """Tally bulk node receipts by physical file, in return order (§2.5)."""
        for entry in receipts or ():
            if not isinstance(entry, dict):
                continue
            receipt = entry.get("row")
            if not isinstance(receipt, dict):
                continue
            canonical = canonical_carto_file(entry.get("file"))
            key, created = self._key_for("files", canonical, canonical)
            if created:
                key.segment, key.ordinal = "bases", self._base_ordinals
                self._base_ordinals += 1
                self._base_order.append(key)
            key.bulk = True
            key.tally = tally_bulk_receipt(key.tally, receipt)
            if receipt.get("success") is False or receipt.get("error"):
                self._node_errors += 1
            self._register(key)

    def receipt_tally(self, file):
        """The key's whole tally so far (G5 passes it to ``apply_bulk_receipts``)."""
        key = self._keys.get(("files", canonical_carto_file(file)))
        return dict(key.tally) if key is not None and key.tally is not None else dict(_EMPTY_TALLY)

    def reflected_nodes(self, file):
        """The receipt count the key's latest line reflects (G5's ``reflected``)."""
        key = self._keys.get(("files", canonical_carto_file(file)))
        return key.reflected if key is not None else 0

    # -- phase 8 ------------------------------------------------------------
    def set_phase8(self, envelope):
        """Update envelope metadata, or replace an explicit complete files list.

        G5 stores an envelope without files and appends phase-8 rows separately;
        those puts are incremental, not a rebuild of earlier row contributions.
        An explicit files list replaces that section, in the supplied order.
        """
        if not isinstance(envelope, dict):
            self._clear_phase8()
            self._refresh_previews()
            return
        if "files" in envelope:
            self._clear_phase8()
        _warning_part_sub(self._phase8_warnings, self._phase8_envelope_warnings)
        self._phase8_envelope_warnings = _warnings_part(envelope.get("warnings"))
        self._phase8_failed = bool(envelope.get("success") is False or envelope.get("error"))
        if self._phase8_failed:
            _add_warning_type(self._phase8_envelope_warnings, "phase8_refresh_failed")
        _warning_part_add(self._phase8_warnings, self._phase8_envelope_warnings)
        self._phase8_envelope = {
            "execution": envelope.get("execution"),
            "success": envelope.get("success"),
            "error": envelope.get("error"),
            "files_total": len(self._phase8_keys),
            "files_failed": self._phase8_failed_rows,
        }
        if "files" in envelope:
            rows = list(envelope.get("files") or ())
            for row in rows:
                self.put(row, section="phase8")
            # An explicitly supplied envelope may repeat a label. The batch's
            # findings/warnings are last-row-wins, while its envelope still
            # reports the raw row count/failures (not the folded key count).
            self._phase8_envelope.update(files_total=len(rows), files_failed=sum(
                row.get("status") in _FAILED_ROW_STATUSES for row in rows))
        self._refresh_previews()

    def _register_phase8(self, key, *, skip_primary=None):
        """Replace one phase-8 contribution, touching only its matched primary."""
        previous = key.registered
        old_primary = previous.get("primary") if previous is not None else None
        primary = self._primary_by_label.get(key.file)
        warnings_changed = previous is None or previous["warnings"] is not key.warnings
        findings_changed = (previous is None or previous["findings"] is not key.findings
                            or (old_primary is None) != (primary is None))
        if previous is not None:
            if warnings_changed:
                _warning_part_sub(self._phase8_warnings, previous["warnings"])
            self._phase8_failed_rows -= previous["failed"]
            if old_primary is None and findings_changed:
                _part_sub(self._phase8_findings, previous["findings"])
            old_file = previous["file"]
            if old_file != key.file and self._phase8_by_label.get(old_file) is key:
                self._phase8_by_label.pop(old_file, None)
        self._phase8_by_label[key.file] = key
        if old_primary is not None and old_primary is not primary:
            old_primary.phase8 = None
            if old_primary is not skip_primary:
                self._register(old_primary)
        if primary is not None:
            primary.phase8 = key.findings
            if primary is not skip_primary:
                self._register(primary)
        elif findings_changed:
            _part_add(self._phase8_findings, key.findings)
        self._phase8_only.set_block(key.ordinal, findings=key.findings["preview"] if primary is None else ())
        if warnings_changed:
            _warning_part_add(self._phase8_warnings, key.warnings)
        failed = int(key.status_present and key.status in _FAILED_ROW_STATUSES)
        self._phase8_failed_rows += failed
        key.registered = {"primary": primary, "findings": key.findings, "warnings": key.warnings,
                          "file": key.file, "failed": failed}
        self._set_local_failure(key.identity, key.file, key.status, key.error, key.local_cpu_failure,
                                enabled=primary is not None)
        if self._phase8_envelope is None:
            self._phase8_envelope = {"execution": None, "success": None, "error": None}
        self._phase8_envelope.update(files_total=len(self._phase8_keys), files_failed=self._phase8_failed_rows)
        self._refresh_previews()

    def _clear_phase8(self):
        previous = list(self._phase8_keys.values())
        self._phase8_by_label = {}
        self._phase8_keys = {}
        for key in previous:
            primary = key.registered.get("primary") if key.registered is not None else None
            if primary is not None:
                primary.phase8 = None
                self._register(primary)
            self._set_local_failure(key.identity, key.file, None, False, None, enabled=False)
            self._keys.pop(key.identity, None)
        self._phase8_matched = []
        self._phase8_rows = []
        self._phase8_pending = {}
        self._phase8_envelope = None
        self._phase8_failed = False
        self._phase8_ordinals = 0
        self._phase8_failed_rows = 0
        self._phase8_findings = _empty_findings_part()
        self._phase8_warnings = _empty_warnings_part()
        self._phase8_envelope_warnings = _empty_warnings_part()
        self._phase8_only = _PreviewSegment()

    def _files_keys_in_order(self):
        """The fold's row order: first-put rows, then receipt-only base rows."""
        for key in self._row_order:
            if key.segment == "rows":
                yield key
        for key in self._base_order:
            if key.segment == "bases":
                yield key

    # -- attribution --------------------------------------------------------
    def _set_local_failure(self, key, file, status, error, identity, *, enabled):
        previous = self._local_failures.pop(key, None)
        if previous is not None:
            self._local_failure_counts[previous] -= 1
            if not self._local_failure_counts[previous]:
                del self._local_failure_counts[previous]
        if (enabled and status == "failed" and error and isinstance(identity, dict)
                and identity.get("file") == file):
            signature = self._failure_signature(identity)
            self._local_failures[key] = signature
            self._local_failure_counts[signature] += 1

    @staticmethod
    def _failure_signature(identity):
        import json
        return json.dumps({key: value for key, value in identity.items() if key != "file"},
                          sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def local_cpu_failure(self, identity):
        """Exact identity lookup without walking primary or phase-8 summaries."""
        return bool(self._local_failure_counts.get(self._failure_signature(identity or {})))

    # -- reads --------------------------------------------------------------
    @property
    def index_visits(self):
        """Index nodes touched by puts so far (a structural counter, never timing)."""
        return (self._rows.node_visits + self._receipt_bases.node_visits
                + self._phase8_only.node_visits)

    def reset_index_visits(self):
        """Zero the three segments' visit counters."""
        for segment in (self._rows, self._receipt_bases, self._phase8_only):
            segment.node_visits = 0

    def outcome(self, *, infrastructure_error=None, cancelled=False):
        """``beacon_job_outcome(aggregate_beacon_outcomes(rows, …))``, minus its lists."""
        findings_total = self._findings["count"] + self._phase8_findings["count"]
        warnings_total = (self._run_warnings["count"] + self._key_warnings["count"]
                          + self._phase8_warnings["count"])
        events_total = self._run_events + self._key_events
        successful = self._row_count - self._failed_rows - self._skipped_rows
        files_changed_total = len(self._label_changed)
        changed = bool(files_changed_total)
        phase8_failed = self._phase8_failed
        problematic = bool(self._failed_rows or self._skipped_rows or findings_total
                           or warnings_total or events_total)
        useful = successful > 0 or changed
        status = ("failed" if infrastructure_error is not None or cancelled or not useful
                  else "completed_with_warnings" if problematic else "completed")
        result = {
            "status": status,
            "success": status != "failed",
            "cancelled": bool(cancelled),
            "successful": successful,
            "failed": self._failed_rows,
            "skipped": self._skipped_rows,
            "partial_success": bool(useful and (
                self._failed_rows or self._skipped_rows or phase8_failed
                or infrastructure_error is not None or cancelled)),
            "changed": changed,
            "findings_total": findings_total,
            "findings_by_code": _combined(self._findings["codes"],
                                          self._phase8_findings["codes"]),
            "findings_truncated": max(0, findings_total - 200),
            "warnings_total": warnings_total,
            "warnings_by_type": _combined(self._run_warnings["types"],
                                          self._key_warnings["types"],
                                          self._phase8_warnings["types"]),
            "warnings_truncated": 0,
            "summary_line": (f"{successful} successful · {self._failed_rows} failed · "
                             f"{self._skipped_rows} skipped · {findings_total} findings · "
                             f"{events_total} events"),
            "errors_total": self._failed_rows + self._node_errors + int(phase8_failed),
            "node_errors_total": self._node_errors,
            "phase8_failed": phase8_failed,
            "files_changed_total": files_changed_total,
            "events_total": events_total,
            "findings_by_severity": _combined(self._findings["severities"],
                                              self._phase8_findings["severities"]),
            "files_changed": list(self._files_changed_preview),
            "findings_preview": [dict(finding) for finding in self._findings_preview],
            "preview_totals": {"files_changed": files_changed_total,
                               "findings_preview": findings_total},
        }
        if self._phase8_envelope is not None:
            result["phase8_summary"] = dict(self._phase8_envelope)
        if infrastructure_error is not None:
            result["infrastructure_error"] = copy.deepcopy(infrastructure_error)
        return result
