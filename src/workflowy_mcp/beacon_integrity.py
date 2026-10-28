"""Read-only inventory/observation adapter for the portable beacon findings core.

Each file is observed once. Scanner records, mapper and attachment inputs come
from that immutable snapshot; classification, identity and action data live in
``beacon_findings``. No source writes, leases, refreshes or cache operations.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Mapping, Sequence

from .client.api_client_nexus import (
    _BEACON_LANGUAGE_BY_EXT,
    _extract_path_header_from_note,
    _import_cartographer_module,
    _is_cartographer_parse_error_sentinel,
    _path_value_looks_like_any_file,
    parse_cartographer_note_for_preview,
    resolve_cartographer_path_from_node,
)
from .f8_slice_policy import collect_subtree_ids
from beacon_source import read_source_snapshot
from beacon_findings import (
    BEACON_CHECK_CODES, SEVERITY_ORDER, analyze_beacon_snapshot, canonical_beacon_file,
    make_beacon_finding, beacon_snapshot_analysis_inputs,
)


def _row(file: str, line: int, code: str, beacon_id: Any, message: str, detail: dict | None = None,
         **witness) -> dict:
    """Compatibility builder delegates every wire field to the common core."""
    return make_beacon_finding(file, line, code, beacon_id, message, detail, **witness)


def _sort_key(row: Mapping[str, Any]) -> tuple:
    return (SEVERITY_ORDER.index(row["severity"]), row["file"], row["line"],
            row["code"], row["finding_id"])


def _walk(node: Mapping[str, Any]):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


# @beacon[
#   id=beacon-check@_snapshot_analysis_inputs,
#   slice_labels=carto-fwd-source,beacon-writeback,
#   kind=ast,
#   comment=Supplies the daemon snapshot map and sentinel diagnostics to the portable input projection shared with the kit; no scanner attachment witness duplicate or planner projection is duplicated here.,
# ]
def _snapshot_analysis_inputs(snapshot, language, source, repo_root=None):
    """Build the daemon map view, then delegate all shared input projection."""
    mapper_errors = []
    try:
        tree = source.map_source_snapshot(snapshot, repo_root=repo_root)
    except Exception as error:
        mapper_errors.append({"exception": f"{type(error).__name__}: {error}"})
        tree = {"children": []}
    for node in _walk(tree):
        name = str(node.get("name") or "")
        if _is_cartographer_parse_error_sentinel(name):
            mapper_errors.append({"name": name, "note": str(node.get("note") or ""),
                                  "section": "VUE" in name})
    return beacon_snapshot_analysis_inputs(
        snapshot, language, source, outline=tree.get("children") or [], mapped_roots=[tree],
        parse_note=parse_cartographer_note_for_preview, mapper_errors=mapper_errors)


# @beacon[
#   id=beacon-check@check_beacon_file,
#   slice_labels=carto-fwd-source,beacon-writeback,
#   kind=ast,
#   comment=Delegates to the single observation envelope and returns its physical findings as a list for existing callers; clean files retain their snapshot witness internally and unexpected analyzer exceptions reach the per-file report boundary.,
# ]
def check_beacon_file(
    abs_path: str,
    *,
    display_path: str | None = None,
    repo_root: str | None = None,
) -> list[dict]:
    """List-return compatibility adapter over the single observation mechanism.

    Unsupported extensions return no rows. IO/decode errors have explicit
    missing/unreadable identities. Other analyzer exceptions propagate to
    ``run_beacon_check`` for per-file containment, never an empty map.
    ``repo_root`` affects display at the inventory adapter, never identity.
    """
    return _observe_beacon_file(abs_path, display_path=display_path, repo_root=repo_root)["rows"]


def _unobserved_file(abs_path: str) -> dict:
    return {"canonical_file": canonical_beacon_file(os.path.abspath(abs_path)),
            "file_sha256": None, "unreadable_identity": {"state": "unobserved"}, "rows": []}


# @beacon[
#   id=beacon-check@_observe_beacon_file,
#   slice_labels=carto-fwd-source,beacon-writeback,
#   kind=ast,
#   comment=Reads one immutable source snapshot or returns explicit unreadability then delegates to the supplied-snapshot observation shared by check and fix; retains the caller envelope on analyzer failure without reopening source.,
# ]
def _observe_beacon_file(abs_path: str, *, display_path: str | None = None,
                         repo_root: str | None = None, observation: dict | None = None) -> dict:
    """Read once, then delegate rows and identity to the snapshot-in observer.

    The optional caller-owned envelope is filled before analysis, so the report
    boundary can retain an observation even when an analyzer raises. This is
    the file-read boundary; check and fix share the analysis below it.
    """
    result = observation if observation is not None else _unobserved_file(abs_path)
    file = display_path or abs_path
    language = _BEACON_LANGUAGE_BY_EXT.get(os.path.splitext(abs_path)[1].lower())
    if language is None:
        result["unreadable_identity"] = {"state": "unsupported"}
        return result
    try:
        snapshot = read_source_snapshot(abs_path)
    except (OSError, UnicodeDecodeError) as error:
        exc = f"{type(error).__name__}: {error}"
        vanished = isinstance(error, FileNotFoundError)
        identity = {"state": "missing" if vanished else "unreadable", "exception_type": type(error).__name__}
        if isinstance(error, UnicodeDecodeError):
            identity.update(undecodable_bytes_sha256=hashlib.sha256(error.object).hexdigest(),
                            start=error.start, end=error.end, reason=error.reason)
        else:
            identity["errno"] = error.errno
        result.update(canonical_file=canonical_beacon_file(os.path.realpath(abs_path)),
                      unreadable_identity=identity)
        message = (f"The file disappeared before it could be read ({exc}); restore the file or check the current map."
                   if vanished else f"The file cannot be read as UTF-8 ({exc}); restore/read UTF-8 source and retry.")
        result["rows"] = [_row(file, 0, "file_missing" if vanished else "file_unreadable", None,
                               message, {"exception": exc}, **_observation_witness(result))]
        return result
    return observe_beacon_snapshot(snapshot, language=language, display_path=file,
                                   repo_root=repo_root, observation=result)


# @beacon[
#   id=beacon-check@observe_beacon_snapshot,
#   slice_labels=carto-fwd-source,beacon-writeback,
#   kind=ast,
#   comment=Analyzes the caller supplied immutable snapshot into the common findings envelope; check and fix share this projection with no source read lease write or second analysis authority.,
# ]
def observe_beacon_snapshot(snapshot, *, language, display_path=None, repo_root=None,
                            observation=None):
    """Observe exactly the snapshot the caller read (and may plan against).

    Fill identity before analysis so a contained analyzer exception preserves
    the actual version. Marker-free snapshots retain identity and empty rows.
    """
    result = observation if observation is not None else _unobserved_file(snapshot.canonical_path)
    file = display_path or snapshot.canonical_path
    result.update(canonical_file=canonical_beacon_file(snapshot.canonical_path),
                  file_sha256=snapshot.sha256, rows=[])
    result.pop("unreadable_identity", None)
    if "@beacon" not in snapshot.parse_text:
        return result
    source = _import_cartographer_module()
    inputs = _snapshot_analysis_inputs(snapshot, language, source, repo_root=repo_root)
    if "section_error" in inputs:
        result["rows"] = [_row(file, 0, "mapper_parse_error", None,
                                "The Vue section analysis failed; inspect the exception and retry without changing source.",
                                {"exception": inputs["section_error"]}, snapshot=snapshot)]
    else:
        result["rows"] = _finish(analyze_beacon_snapshot(snapshot, **inputs, display_path=file), file)
    return result


def _observation_witness(observation: Mapping[str, Any]) -> dict:
    return {key: observation[key] for key in ("canonical_file", "file_sha256", "unreadable_identity")
            if key in observation}


def _finish(rows: list[dict], file: str) -> list[dict]:
    for row in rows:
        row["file"] = file
    rows.sort(key=_sort_key)
    return rows


def _display_path(abs_path: str, root_abs: str | None) -> str:
    if root_abs:
        try:
            rel = os.path.relpath(abs_path, root_abs)
        except ValueError:
            rel = ""
        if rel and rel != "." and not rel.startswith("..") and not os.path.isabs(rel):
            return rel.replace(os.sep, "/")
    return abs_path


def _file_header(node: Mapping[str, Any] | None) -> tuple[str, str] | None:
    header = _extract_path_header_from_note(str((node or {}).get("note") or (node or {}).get("no") or ""))
    if header and header[0] == "Path" and _path_value_looks_like_any_file(header[1]):
        return header
    return None


def collect_mapped_beacon_files(nodes_by_id: Mapping[str, Mapping], node_id: str) -> dict:
    """The beacon-language FILE nodes a mapped FILE, FOLDER or ``Root:`` node
    covers in the given cache, resolved to disk.

    Returns ``{"root_abs", "files", "missing", "skipped"}``; each file entry
    is ``{"abs_path", "display_path", "node_id"}`` (missing entries add
    ``"reason"``), sorted by ``display_path``.
    """
    target = nodes_by_id.get(node_id)
    if _file_header(target):
        candidates = [node_id]
    else:
        candidates = [
            cid for cid in collect_subtree_ids(nodes_by_id, [node_id])
            if _file_header(nodes_by_id.get(cid))
        ]

    root_abs = None
    target_abs = None
    try:
        resolved = resolve_cartographer_path_from_node(node_id=node_id, nodes_by_id=nodes_by_id)
        root_abs, target_abs = resolved.get("root_abs"), resolved.get("abs_path")
    except Exception:  # noqa: BLE001 -- an unresolvable target has no root
        pass
    if root_abs is None:
        header = _extract_path_header_from_note(str((target or {}).get("note") or (target or {}).get("no") or ""))
        if header and header[0] == "Root":
            root_abs = target_abs

    files: list[dict] = []
    missing: list[dict] = []
    skipped = 0
    for cid in candidates:
        _kind, value = _file_header(nodes_by_id.get(cid))
        if os.path.splitext(value)[1].lower() not in _BEACON_LANGUAGE_BY_EXT:
            skipped += 1
            continue
        try:
            abs_path = resolve_cartographer_path_from_node(node_id=cid, nodes_by_id=nodes_by_id)["abs_path"]
        except Exception as e:  # noqa: BLE001 -- recorded as missing
            missing.append({"abs_path": None, "display_path": value, "node_id": cid,
                            "reason": f"{type(e).__name__}: {e}"})
            continue
        entry = {"abs_path": abs_path, "display_path": _display_path(abs_path, root_abs), "node_id": cid}
        if not os.path.isfile(abs_path):
            missing.append({**entry, "reason": "no file on disk at the mapped path"})
            continue
        files.append(entry)
    files.sort(key=lambda f: f["display_path"])
    missing.sort(key=lambda f: f["display_path"])
    return {"root_abs": root_abs, "files": files, "missing": missing, "skipped": skipped}


# @beacon[
#   id=beacon-check@run_beacon_check,
#   slice_labels=carto-fwd-source,beacon-writeback,
#   kind=ast,
#   comment=Contains each file failure as one finding and globally orders filters and pages all findings with exact aggregate and per-file counts; hashes every observation including clean files for stateless scope-change detection without rereads writes leases or stored cursors.,
# ]
def run_beacon_check(
    files: Sequence[Mapping[str, Any]],
    *,
    repo_root: str | None = None,
    missing: Sequence[Mapping[str, Any]] = (),
    skipped: int = 0,
    min_severity: str = "warning",
    max_rows: int = 300,
    offset: int = 0,
    codes: list[str] | None = None,
    finding_ids: list[str] | None = None,
) -> dict:
    """Order, filter, then page; counts and summary always cover ALL findings.

    One file's analyzer exception costs one check_failed row, not its neighbours.
    The stateless scope fingerprint covers the actual observations, even files
    with no findings. Callers compare it across pages and restart on change.
    Missing requested identities are data, never a claim of repair.
    """
    _validate_beacon_check_parameters(min_severity, max_rows, offset, codes, finding_ids)
    all_rows: list[dict] = []
    observations: list[dict] = []
    by_file: dict[str, dict] = {}
    for m in missing:
        where = m.get("abs_path") or m.get("display_path")
        row = _row(str(m.get("display_path") or m.get("abs_path") or ""), 0, "file_missing", None,
                   f"The map lists this file but nothing usable is on disk at {where} ({m.get('reason')}); refresh the map or restore the file.",
                   {"node_id": m.get("node_id"), "reason": m.get("reason")}, canonical_file=where,
                   unreadable_identity={"state": "missing", "node_id": m.get("node_id")})
        all_rows.append(row)
        observations.append(_observation_witness(row["observed"]))
        by_file.setdefault(row["file"], {"total": 0, "by_severity": {s: 0 for s in SEVERITY_ORDER}})
    for f in files:
        file = f.get("display_path") or f["abs_path"]
        observation = _unobserved_file(f["abs_path"])
        try:
            _observe_beacon_file(f["abs_path"], display_path=file, repo_root=repo_root, observation=observation)
        except Exception as error:  # noqa: BLE001 -- file-local analyzer failure is data
            exc = f"{type(error).__name__}: {error}"
            observation["rows"] = [_row(file, 0, "check_failed", None,
                                         f"The file check failed ({exc}); inspect the exception and retry; other files are still checked.",
                                         {"exception": exc}, **_observation_witness(observation))]
        all_rows.extend(observation["rows"])
        observations.append(_observation_witness(observation))
        by_file.setdefault(file, {"total": 0, "by_severity": {s: 0 for s in SEVERITY_ORDER}})
    all_rows.sort(key=_sort_key)
    by_severity = {s: 0 for s in SEVERITY_ORDER}
    by_code: dict[str, int] = {}
    for row in all_rows:
        by_severity[row["severity"]] += 1
        by_code[row["code"]] = by_code.get(row["code"], 0) + 1
        by_file[row["file"]]["total"] += 1
        by_file[row["file"]]["by_severity"][row["severity"]] += 1
    counts = {
        "files_checked": len(files), "files_skipped": int(skipped), "files_missing": by_code.get("file_missing", 0),
        "files_unreadable": by_code.get("file_unreadable", 0),
        "files_with_problems": len({r["file"] for r in all_rows if r["severity"] != "info"}),
        "by_severity": by_severity, "by_code": dict(sorted(by_code.items())),
        "by_file": dict(sorted(by_file.items())),
    }
    shown = SEVERITY_ORDER[:SEVERITY_ORDER.index(min_severity) + 1]
    code_filter = None if codes is None else set(codes)
    id_filter = None if finding_ids is None else set(finding_ids)
    eligible = [r for r in all_rows if r["severity"] in shown
                and (code_filter is None or r["code"] in code_filter)
                and (id_filter is None or r["finding_id"] in id_filter)]
    kept = eligible[offset:offset + max_rows]
    truncated = max(0, len(eligible) - offset - len(kept))
    present_ids = {r["finding_id"] for r in all_rows}
    canonical_json = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    fingerprint = hashlib.sha256(canonical_json(sorted(observations, key=canonical_json)).encode("utf-8")).hexdigest()
    return {"summary_line": beacon_check_summary_line(all_rows, len(files)), "counts": counts,
            "rows": kept, "rows_truncated": truncated, "min_severity": min_severity,
            "eligible_total": len(eligible), "has_more": truncated > 0,
            "next_offset": offset + len(kept) if truncated else None,
            "scope_fingerprint": fingerprint, "missing_finding_ids": sorted((id_filter or set()) - present_ids)}


def _validate_beacon_check_parameters(min_severity, max_rows, offset, codes, finding_ids):
    """One parameter policy for direct engine callers and the thin verb front."""
    if min_severity not in SEVERITY_ORDER:
        raise ValueError(f"min_severity must be one of {SEVERITY_ORDER}, not {min_severity!r}")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
        raise ValueError(f"max_rows must be an integer >= 1, not {max_rows!r}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError(f"offset must be an integer >= 0, not {offset!r}")
    if codes is not None and (not isinstance(codes, list) or
                              any(not isinstance(code, str) or code not in BEACON_CHECK_CODES for code in codes)):
        raise ValueError(f"codes must be a list of BEACON_CHECK_CODES names, not {codes!r}")
    if finding_ids is not None and (not isinstance(finding_ids, list) or
                                    any(not isinstance(fid, str) for fid in finding_ids)):
        raise ValueError(f"finding_ids must be a list of strings, not {finding_ids!r}")


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def beacon_check_summary_line(rows: list[dict], files_checked: int) -> str:
    """One line over ALL common findings, never just a selected/capped view."""
    sev = {s: sum(1 for r in rows if r["severity"] == s) for s in SEVERITY_ORDER}
    base = (f"{_count(files_checked, 'file')} · {_count(sev['error'], 'error')} · "
            f"{_count(sev['warning'], 'warning')} · {sev['info']} info")
    problems = [r for r in rows if r["severity"] in ("error", "warning")]
    if not problems:
        return base
    if len(problems) == 1:
        r = problems[0]
        message = r["message"] if len(r["message"]) <= 110 else r["message"][:107] + "..."
        return f"{base} ({r['file']}:{r['line']} {r['code']} — {message})"
    groups: list[str] = []
    for severity in ("error", "warning"):
        codes: dict[str, int] = {}
        for r in problems:
            if r["severity"] == severity:
                codes[r["code"]] = codes.get(r["code"], 0) + 1
        groups.extend(f"{c} ×{n}" for c, n in sorted(codes.items(), key=lambda kv: (-kv[1], kv[0])))
    shown = ", ".join(groups[:4]) + (", ..." if len(groups) > 4 else "")
    return f"{base} ({shown})"
