"""beacon_obtain_code_snippet.py

Beacon-aware helpers for working with source files and NEXUS beacons.

This module currently focuses on the lowest-risk, most immediately useful
capability:

  • **Extracting code/doc snippets around a given beacon id** from a
    single source file (Python, Markdown, SQL, shell), with context lines.

This is deliberately local-only and has **no Workflowy writes** – it only
reads the filesystem and prints snippets. That gives you a usable, beacon-
aware workflow without needing to depend on the installed workflowy_mcp
configuration in this script.

---------------------------------------------------------------------
PER-FILE REFRESH (historical note)
---------------------------------------------------------------------

The per-file beacon-aware refresh mode envisioned during this script's
early design shipped separately as the Cartographer F12+1 pipeline (see
`refresh_file_node_beacons` in the workflowy_mcp client), driven by
`nexus_map_codebase.py` parsing. The Notes-salvage algorithm once
sketched here (salvaging and re-attaching user-owned Notes[...] subtrees
by emoji/name classification) was RETIRED in S06: Cartographer-mapped
source regions are disk-authoritative, and no emoji/name-based user-node
preservation exists.

For now, this file implements the safer subset:
  **beacon-based snippet extraction from files**.
"""

from __future__ import annotations

import argparse
import json
import hashlib
from bisect import bisect_right
from dataclasses import dataclass, field
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# Ensure we can import nexus_map_codebase from the same directory
# (both scripts live at the project root, alongside each other).
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

# THE ONE shared beacon field parser (multi-line values; stdlib-only module,
# so this import never touches the nexus_map_codebase load cycle).
from beacon_mutation_contract import parse_beacon_note_metadata  # noqa: E402
from beacon_source import (  # noqa: E402
    SourceSnapshot,
    _beacon_comment_rows,
    _source_line_view,
    read_source_snapshot,
    scan_beacon_structure,
    source_snapshot_from_bytes,
    source_lexical_evidence,
)
from beacon_findings import (  # noqa: E402
    analyze_beacon_snapshot, beacon_snapshot_analysis_inputs, finalize_beacon_findings,
    canonical_beacon_file,
)

try:
    # Local Cartographer implementation (no Workflowy calls)
    import nexus_map_codebase  # type: ignore[import]
except Exception as e:  # noqa: BLE001
    print(
        f"[beacon_obtain_code_snippet] ERROR: Could not import nexus_map_codebase from {_THIS_DIR}: {e}",
        file=sys.stderr,
    )
    nexus_map_codebase = None  # type: ignore[assignment]


# SNIPPET ENGINE CORE PLAN:
# This file is the portable snippet-resolution engine for Cartographer-mapped
# files. Preserve multi-language beacon, AST, and MD_PATH snippet extraction
# as a Core v1 capability.
# @beacon[
#   id=beacon-snippet@BeaconSnippetError,
#   slice_labels=ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Typed source extraction failure with file/id, complete candidates and same-snapshot evidence, distinct from handle resolution; its outcome is not a new beacon.check code and its findings remain U05 rows.,
# ]
class BeaconSnippetError(RuntimeError):
    """Typed extraction failure, distinct from failure to resolve a node handle.

    ``outcome`` classifies extraction, not a new beacon.check finding family.
    Findings, when present, are exclusively the shared U05 rows.
    """
    source_extraction_failed = True

    def __init__(self, message: str, *, file_path: str, beacon_id: str | None,
                 outcome: str, candidates=None, evidence=None):
        super().__init__(f"{message} [file={file_path!r}, id={beacon_id!r}]")
        self.candidates = list(candidates or [])
        self.source_extraction = {"file_path": file_path, "beacon_id": beacon_id,
                                  "outcome": outcome, "candidates": self.candidates,
                                  **(evidence or {})}


def _read_snippet_snapshot(path: str, beacon_id: str | None = None) -> SourceSnapshot:
    try:
        return read_source_snapshot(path)
    except (OSError, UnicodeDecodeError) as error:
        identity = {"state": "missing" if isinstance(error, FileNotFoundError) else "unreadable",
                    "exception_type": type(error).__name__}
        if isinstance(error, UnicodeDecodeError):
            identity.update(undecodable_bytes_sha256=hashlib.sha256(error.object).hexdigest(),
                            start=error.start, end=error.end, reason=error.reason)
        raise BeaconSnippetError(str(error), file_path=str(path), beacon_id=beacon_id,
                                 outcome="file_missing" if isinstance(error, FileNotFoundError) else "file_unreadable",
                                 evidence={"exception_type": type(error).__name__, "observed": {
                                     "canonical_file": canonical_beacon_file(os.path.realpath(path)),
                                     "file_sha256": None, "unreadable_identity": identity}}) from error


def _qualname_error(snapshot, qualifier, language, outline, message, outcome):
    errors = _snippet_outline_errors(outline)
    return BeaconSnippetError(message, file_path=snapshot.canonical_path, beacon_id=qualifier,
                               outcome="outline_parse_failed" if errors else outcome,
                               candidates=[{"line": node.get("orig_lineno_start_unused"),
                                            "anchor_symbol": qualifier} for node in _iter_outline_nodes(outline)
                                           if node.get("ast_qualname") == qualifier],
                               evidence=_snippet_finding_evidence(snapshot, language, outline,
                                                                    errors=errors, beacon_id=qualifier))


def snippet_target_from_note(note: str, *, twin_witness=None, occurrence=None) -> dict:
    """Project source identity only; the client supplies its pinned-cache witness."""
    prefix = []
    for line in str(note or "").splitlines():
        if line.strip().startswith(("BEACON (", "SPAN TEXT:", "CONTEXT COMMENTS", "DOCSTRING")):
            break
        prefix.append(line)
    identity_note = "\n".join(prefix)
    return {"ast_qualname": nexus_map_codebase._extract_ast_qualname_from_note(identity_note),
            "md_path": nexus_map_codebase._extract_md_path_from_note(identity_note),
            "occurrence": occurrence, "twin_witness": twin_witness}


# @beacon[
#   id=beacon-snippet@_snippet_analysis_rows,
#   slice_labels=ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Runs the one portable U05 analysis over the supplied read snapshot and caller-owned outline, without reopening source or inventing finding codes; the snippet evidence projector owns locality and analysis-error containment.,
# ]
def _snippet_analysis_rows(snapshot, language, outline, errors=()):
    """The single portable analysis of this read's already-observed bytes."""
    inputs = beacon_snapshot_analysis_inputs(
        snapshot, language, nexus_map_codebase, outline=outline,
        parse_note=lambda note: [("beacon", row) for row in parse_beacon_note_metadata(note)],
        mapper_errors=errors)
    if "section_error" in inputs:
        raise RuntimeError(inputs["section_error"])
    return finalize_beacon_findings(analyze_beacon_snapshot(snapshot, **inputs, producer="beacon.snippet"))


# @beacon[
#   id=beacon-snippet@_snippet_finding_evidence,
#   slice_labels=ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Projects member-linked and file findings from the read's one shared analysis, including orphan-close operands, exact observation identity and complete file counts/locator; analyzer failure remains data and never discards a readable window.,
# ]
def _snippet_finding_evidence(snapshot, language, outline, *, own=None, errors=(), beacon_id=None):
    """Analyze once, without a second read; only shared finding codes/identity.

    The snippet scope includes member-linked close candidates and global rows.
    Complete file counts and the check locator make the scope explicit.
    """
    result = {"findings": [], "observed": {"canonical_file": canonical_beacon_file(snapshot.canonical_path),
                                            "file_sha256": snapshot.sha256, "had_bom": snapshot.had_bom}}
    try:
        rows = _snippet_analysis_rows(snapshot, language, outline, errors)
        owner = own["opener_range"][0] if own else None
        related = [row for row in rows if row.get("line") == 0 or
                   (beacon_id and row.get("beacon_id") == beacon_id) or
                   (owner is not None and any(member.get("opener_start_byte") == owner
                                             for member in row.get("members") or []))]
        result["findings"] = related
        result["finding_scope"] = {"kind": "snippet", "file_total": len(rows), "returned": len(related),
                                   "complete_file": {"family": "beacon", "verb": "check", "params": {
                                       "file_path": snapshot.canonical_path, "min_severity": "info"}}}
        if related:
            result["finding"] = next((row for row in related if row["code"] == "duplicate_id"), related[0])
    except Exception as error:
        result["analysis_error"] = {"exception": type(error).__name__, "message": str(error)}
    return result


# @beacon[
#   id=bocsv1@file-core,
#   role=🔱 core keeper: beacon-aware snippet extraction engine across mapped source files,
#   slice_labels=nexus-portability,nexus-core-v1,carto-fwd-source,
#   kind=span,
#   show_span=false,
# ]
# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


# @beacon[
#   id=beacon-snippet@BeaconSnippetResult,
#   slice_labels=ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=One snippet observation with own block, current anchor, core lines and paired close geometry; shared findings/observation and typed degradation project through legacy tuples and JSON locations without losing useful source.,
# ]
@dataclass
class BeaconSnippetResult:
    """One immutable source observation and its selected readable projection.

    The close record is geometry, never partly included in a closed span's
    text (U07 ruling A). Compatibility tuples are projections of this result.
    """

    snapshot: SourceSnapshot
    file_lines: List[str]
    own_block: dict | None
    selected_anchor: dict | None
    core_start_line: int
    core_end_line: int
    beacon_line: int
    context: int = 0
    resolution_strategy: str = "beacon"
    findings: List[dict] = field(default_factory=list)
    paired_close: dict | None = None
    evidence: dict = field(default_factory=dict)

    def metadata(self) -> dict:
        """Project strategy/findings and observation; original bytes stay internal."""
        return {"resolution_strategy": self.resolution_strategy,
                "confidence": 0.0 if self.resolution_strategy == "degraded" else 1.0,
                "ambiguity": "none", "candidates": None,
                "location": self.location(), "findings": self.findings,
                "observed": {"canonical_file": canonical_beacon_file(self.snapshot.canonical_path),
                             "file_sha256": self.snapshot.sha256, "had_bom": self.snapshot.had_bom}, **self.evidence}

    def location(self) -> dict:
        """Expose current effective kind and U01 own/close geometry without rescanning."""
        record = self.own_block
        kind = ("ast" if self.selected_anchor is not None else
                (record["fields"].get("kind") or "span").strip().lower()) if record else None
        return {"file_path": self.snapshot.canonical_path,
                "kind": kind,
                "beacon_id": record["fields"].get("id") if record else None,
                "beacon_line": self.beacon_line,
                "core_start_line": self.core_start_line, "core_end_line": self.core_end_line,
                "anchor": nexus_map_codebase._beacon_anchor_descriptor(self.selected_anchor),
                "block": _snippet_record_location(record),
                "span": {"pair_state": record["pair_state"], "closed": record["closed"],
                         "inner_lines": record["inner_lines"], "inner_range": record["inner_range"],
                         "section": record["section"], "close": _snippet_record_location(self.paired_close)}
                if record and kind == "span" else None}

    def window_tuple(self) -> Tuple[int, int, List[str], int, int, int]:
        """The historical six-field private window shape, with normal padding."""
        return (max(1, self.core_start_line - self.context),
                min(len(self.file_lines), self.core_end_line + self.context), self.file_lines,
                self.core_start_line, self.core_end_line, self.beacon_line)

    def legacy_tuple(self) -> Tuple[int, int, List[str], int, int, int, dict]:
        """Keep the seven-field public API without a competing window reader."""
        return (*self.window_tuple(), self.metadata())


def _snippet_record_location(record: dict | None) -> dict | None:
    if record is None:
        return None
    return {"id": record["fields"].get("id"), **{key: record.get(key) for key in (
        "marker", "read_end", "diagnostics",
        "line", "end_line", "read_end_line", "line_extent", "opener_range", "bracket_range",
        "wrapper_range", "wrapper_open_range", "wrapper_close_range", "complete",
        "terminated_by", "pair_state", "closed", "close_line", "opener_line", "section")}}


def _paired_span_for_id(lines: List[str], language: str, beacon_id: str) -> Optional[Tuple[int, int]]:
    """Legacy inner-range adapter; all recognition/pairing belongs to U01."""
    snapshot = _source_line_view(lines)
    records = scan_beacon_structure(snapshot, language, evidence=source_lexical_evidence(snapshot, language))
    matches = [record for record in records if record["marker"] == "open"
               and record["fields"].get("id") == beacon_id.strip()]
    if len(matches) != 1 or not matches[0]["closed"]:
        return None
    return matches[0]["inner_lines"]


def _snippet_lines(snapshot: SourceSnapshot, language: str) -> List[str]:
    if language == "markdown":
        return nexus_map_codebase._markdown_source_lines(
            nexus_map_codebase._strip_disallowed_markdown_control_chars(snapshot.parse_text))
    return list(snapshot.physical_lines)


def _snippet_outline_errors(outline, errors=()):
    """Retain parser diagnostics; warning sentinels have no real source anchor.

    A real heading/constant named 'Parse Error' is not a parser failure.
    """
    errors = list(errors)
    for node in _iter_outline_nodes(outline):
        name = str(node.get("name") or "")
        if (node.get("orig_lineno_start_unused") or node.get("_md_heading_line")
                or not name.startswith("⚠️")):
            continue
        if "Parse Error" in name or "parsing not available" in name:
            message = str(node.get("note") or "")
            if not any(error.get("exception") == message for error in errors):
                errors.append({"record": "outline_error", "exception": message})
    return errors


def _snippet_outline(snapshot: SourceSnapshot, language: str) -> tuple[list, list]:
    builder = {"python": nexus_map_codebase.parse_file_outline,
               "js_ts": nexus_map_codebase.parse_js_ts_outline,
               "vue": nexus_map_codebase.parse_vue_outline,
               "markdown": nexus_map_codebase.parse_markdown_structure}.get(language)
    errors = []
    if builder is None:
        return [], errors
    try:
        outline = builder(snapshot.canonical_path, snapshot=snapshot, diagnostics=errors)
        return outline, _snippet_outline_errors(outline, errors)
    except Exception as error:
        return [], [{"record": "outline_error", "exception": f"{type(error).__name__}: {error}"}]


# @beacon[
#   id=beacon-snippet@_snippet_structures,
#   slice_labels=ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Projects one snapshot into U01 structural records and supplied outline; Vue scans only its actual SFC sections and every other language uses the shared lexical-evidence scanner without a second block or close grammar.,
# ]
def _snippet_structures(snapshot: SourceSnapshot, language: str, outline: list) -> dict:
    """Thread the caller's observation/outline through the one section grammar."""
    if language == "vue":
        pairs = nexus_map_codebase._vue_section_records(snapshot)
        records = [record for _section, rows in pairs for record in rows]
        sections = [section for section, _rows in pairs]
    else:
        records = scan_beacon_structure(snapshot, language,
                                        evidence=source_lexical_evidence(snapshot, language))
        sections = None
    return {"language": language, "records": records, "sections": sections, "outline": outline}


def _snippet_read_extent(snapshot: SourceSnapshot, record: dict) -> Tuple[int, int]:
    start, end = record["line"], record["read_end_line"]
    if record["wrapper_dedicated"] and record["wrapper_open_range"]:
        start = bisect_right(snapshot.byte_offsets, record["wrapper_open_range"][0])
        if record["wrapper_range"]:
            end = bisect_right(snapshot.byte_offsets, record["wrapper_range"][1] - 1)
    return start, end


def _markdown_heading_rows(outline: list) -> List[dict]:
    """Project the canonical heading authority, shared by id and MD_PATH reads."""
    import re
    headings = []
    for node in _iter_outline_nodes(outline):
        md_path = nexus_map_codebase._extract_md_path_from_note(node.get("note"))
        line = node.get("_md_heading_line")
        if md_path and isinstance(line, int) and line > 0:
            path = []
            for component in md_path:
                match = re.match(r"^(#{1,64})\s*(.*)$", component.strip())
                if match:
                    path.append((len(match[1]), nexus_map_codebase.whiten_text_for_header_compare(match[2].strip()).casefold()))
            if path:
                headings.append({"level": path[-1][0], "path": path, "start_line": line, "node": node})
    return sorted(headings, key=lambda heading: heading["start_line"])


def _markdown_heading_end(headings: List[dict], index: int, n: int, brief: bool) -> int:
    level = 64 if brief else headings[index]["level"]
    return next((other["start_line"] - 1 for other in headings[index + 1:]
                 if other["level"] <= level), n)


def _snippet_explicit_range(snapshot: SourceSnapshot, language: str, record: dict,
                            records: list, outline: list, lines: list) -> tuple[int, int] | None:
    """Resolve legacy text anchors, never by treating them as beacon metadata."""
    fields = record["fields"]
    needle = nexus_map_codebase.normalize_for_match(fields.get("start_snippet") or "")
    if not needle:
        return None
    if language == "markdown":
        beacon = {**fields, "kind": "span", "comment_line": record["line"]}
        nexus_map_codebase._markdown_beacon_snippet_range(beacon, lines, records)
        a, b = beacon.get("span_lineno_start"), beacon.get("span_lineno_end")
        return (a, b) if isinstance(a, int) and isinstance(b, int) else None
    metadata = {line for r in records for line in range(r["line"], r["read_end_line"] + 1)}
    rows = _beacon_comment_rows(snapshot, language, record["section"]["start_line"],
                                record["section"]["end_line"], source_lexical_evidence(snapshot, language))
    comments = {row["line"] for row in rows if row["form"]}
    candidates = [i for i in range(record["section"]["start_line"], record["section"]["end_line"] + 1)
                  if i not in metadata and i not in comments and needle in nexus_map_codebase.normalize_for_match(lines[i - 1])]
    after = [i for i in candidates if i > record["line"]]
    start = candidates[0] if len(candidates) == 1 else after[0] if len(after) == 1 else None
    if start is None:
        return None
    end_needle = nexus_map_codebase.normalize_for_match(fields.get("end_snippet") or "")
    if end_needle:
        end = next((i for i in range(start, record["section"]["end_line"] + 1)
                    if i not in metadata and end_needle in nexus_map_codebase.normalize_for_match(lines[i - 1])), None)
        return (start, end) if end is not None else None
    enclosing = [node for node in _iter_outline_nodes(outline)
                 if isinstance(node.get("orig_lineno_start_unused"), int)
                 and node["orig_lineno_start_unused"] <= start <= (node.get("orig_lineno_end_unused") or 0)]
    end = min((node["orig_lineno_end_unused"] for node in enclosing), default=record["section"]["end_line"])
    return start, end


# @beacon[
#   id=beacon-snippet@get_beacon_snippet_result,
#   slice_labels=ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Builds one typed read with exact U03 target/witness selection and same-snapshot U05 findings; keeps own metadata, AST/span windows and paired close geometry through degradation, returns complete ambiguity data and contains analyzer failure without a gate.,
# ]
def get_beacon_snippet_result(file_path: str, beacon_id: str, context: int = 0, *,
                              snapshot: SourceSnapshot | None = None, target: dict | None = None) -> BeaconSnippetResult:
    """Resolve a beacon through U01/U03, never a close or substring-id scan.

    A supplied snapshot belongs to the caller and is never reopened. A span's
    open-only state is normal; content degradation does not discard its window.
    Ambiguity/failure carries file/id/candidates; analyzer exceptions remain data.
    """
    if nexus_map_codebase is None:
        raise RuntimeError("nexus_map_codebase could not be imported")
    snapshot = snapshot if snapshot is not None else _read_snippet_snapshot(file_path, beacon_id)
    language = {".py": "python", ".js": "js_ts", ".jsx": "js_ts", ".ts": "js_ts", ".tsx": "js_ts",
                ".vue": "vue", ".md": "markdown", ".markdown": "markdown", ".sql": "sql",
                ".sh": "shell", ".yaml": "yaml", ".yml": "yaml"}.get(Path(file_path).suffix.lower())
    if language is None:
        raise RuntimeError(f"Unsupported file extension for beacon snippets: {Path(file_path).suffix}")
    lines = _snippet_lines(snapshot, language)
    requested_id = str(beacon_id or "").strip()
    outline, errors = _snippet_outline(snapshot, language)
    try:
        structures = _snippet_structures(snapshot, language, outline)
    except Exception as error:
        raise BeaconSnippetError(str(error), file_path=snapshot.canonical_path, beacon_id=beacon_id,
                                 outcome="geometry_unavailable", evidence={"exception_type": type(error).__name__,
                                 "observed": {"canonical_file": canonical_beacon_file(snapshot.canonical_path),
                                              "file_sha256": snapshot.sha256}}) from error
    selection = nexus_map_codebase.select_beacon_block_for_node(
        snapshot, structures, {**(target or {}), "note_beacon_id": requested_id, "operation": "read"})
    anchor_degradation = None
    if errors and selection.get("block") is None:
        # An unavailable outline cannot prove an AST attachment. It also cannot
        # veto an independently unique physical id's bounded metadata/span read.
        # U03 still chooses the physical operand; duplicates still return choices.
        physical = nexus_map_codebase.select_beacon_block_for_node(
            snapshot, structures, {"note_beacon_id": requested_id, "operation": "read"})
        if physical.get("block") is not None or str(physical.get("error_kind", "")).startswith("ambiguous"):
            anchor_degradation = selection.get("error")
            selection = physical
    block = selection.get("block")
    evidence = _snippet_finding_evidence(snapshot, language, outline,
                                         own=block["record"] if block else None,
                                         errors=errors, beacon_id=beacon_id)
    if anchor_degradation:
        evidence["anchor_degradation"] = anchor_degradation
    if block is None or (requested_id and block["record"]["fields"].get("id") != requested_id):
        candidates = selection.get("candidates") or []
        if selection.get("error_kind") == "ambiguous_beacon_id":
            candidates = [candidate for candidate in candidates if candidate.get("id") == requested_id]
        candidates = [{**candidate,
                       "anchor_symbol": (candidate.get("anchor") or {}).get("ast_qualname") or
                                         (candidate.get("anchor") or {}).get("md_path"),
                       "comment_head": str(candidate.get("comment") or "").split("\n", 1)[0]}
                      for candidate in candidates]
        raise BeaconSnippetError(selection.get("error") or f"Beacon id {beacon_id!r} not found",
                                 file_path=snapshot.canonical_path, beacon_id=beacon_id,
                                 outcome="ambiguous" if str(selection.get("error_kind", "")).startswith("ambiguous") else "not_found",
                                 candidates=candidates, evidence=evidence)
    own = block["record"]
    # The selector's id-only leg need not select an anchor; the shared index
    # still carries this physical block's current independent attachment.
    anchor = block.get("anchor")
    top, metadata_end = _snippet_read_extent(snapshot, own)
    grammar = "markdown" if language == "vue" and own["section"]["type"] == "template_element" else "js_ts" if language == "vue" else language
    context_bounds = _comment_block_span_from_records(lines, own["line"], grammar,
                                                      snapshot=snapshot, records=structures["records"], own=own)
    if context_bounds:
        top = min(top, context_bounds[0])
    close = next((r for r in structures["records"] if own["closed"]
                  and r["line"] == own["close_line"] and r["marker"] == "close"
                  and r["section"] == own["section"]), None)
    strategy = "beacon"
    if block["effective_kind"] == "ast" and anchor is not None:
        if language == "markdown":
            headings = _markdown_heading_rows(outline)
            index = next(i for i, h in enumerate(headings) if h["start_line"] == anchor["line"])
            labels = (own["fields"].get("slice_labels") or "").split(",")
            brief = any(label.strip() == "b" or label.strip().startswith("b-") for label in labels)
            end = _markdown_heading_end(headings, index, len(lines), brief)
        else:
            node = anchor["node"]
            top = min(top, int(node.get("orig_lineno_start_unused") or anchor["line"]))
            end = int(node.get("orig_lineno_end_unused") or anchor["line"])
    elif (own["fields"].get("kind") or "span").strip().lower() == "span":
        explicit = _snippet_explicit_range(snapshot, grammar, own, structures["records"], outline, lines)
        if own["fields"].get("start_snippet") and explicit is None:
            strategy = "degraded"
        if explicit is not None:
            top, end = min(top, explicit[0]), explicit[1]
        elif own["closed"]:
            # Ruling A: do not partly show an owned wrapper of the close.
            end = _snippet_read_extent(snapshot, close)[0] - 1
        else:
            end = own["section"]["end_line"]
    else:
        # An outline failure or lost attachment retains readable own metadata,
        # not a fabricated successful AST body or a whole-file refusal.
        end = max(metadata_end, block.get("next_code_line") or metadata_end)
        strategy = "degraded"
    if errors:
        strategy = "degraded"
    if not own["complete"] or own["literal_certainty"] == "uncertain" or own["pair_state"] == "ambiguous":
        strategy = "degraded"
    findings = evidence.pop("findings", [])
    if "analysis_error" in evidence or any(row["code"] in {
            "orphan_close", "unterminated_block", "missing_wrapper", "lexical_uncertainty",
            "mapper_parse_error", "pair_ambiguous"} for row in findings):
        strategy = "degraded"
    return BeaconSnippetResult(snapshot, lines, own, anchor, top, max(metadata_end, end), own["line"],
                               context=context, resolution_strategy=strategy, paired_close=close,
                               findings=findings, evidence={**evidence, **({"outline_errors": errors} if errors else {})})


def _read_lines(path: str) -> List[str]:
    """Read the shared BOM-free physical-line view, without changing the file."""
    return list(read_source_snapshot(path).physical_lines)


# @beacon[
#   id=beacon-snippet@_comment_block_span_from_records,
#   slice_labels=ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Reuses the supplied snapshot and records for complete own metadata plus ordinary comment context; section-local lexical rows stop at foreign records, stray bracket metadata or code without reopening source or scanning for block ends.,
# ]
def _comment_block_span_from_records(
    lines: List[str], comment_line: int, language: str, *,
    snapshot: SourceSnapshot | None = None, records: list | None = None, own: dict | None = None,
) -> Optional[Tuple[int, int]]:
    """Project one own-block context window from U01's geometry.

    Supplied snapshot/records are reused; legacy rows adapt in memory, never
    another file read. Blank gaps can connect ordinary comments but are never
    themselves used to widen the window. A neighbour's entire readable extent
    (including its owned wrapper) is a boundary in either direction.
    """
    if not 1 <= comment_line <= len(lines):
        return None
    snapshot = snapshot if snapshot is not None else _source_line_view(lines)
    section = own["section"] if own is not None else {"start_line": 1, "end_line": len(lines)}
    first, last = section["start_line"], section["end_line"]
    if section.get("start_byte") is not None:
        left, right = section["start_byte"], section["end_byte"]
        local = source_snapshot_from_bytes(snapshot.raw_bytes[left:right])
        evidence = [{**row, "start": row["start"] + left, "end": row["end"] + left}
                    for row in source_lexical_evidence(local, language)]
    else:
        evidence = source_lexical_evidence(snapshot, language)
    records = records if records is not None else scan_beacon_structure(snapshot, language, evidence=evidence)
    own = own if own is not None else next((r for r in records if r["marker"] == "open" and r["line"] == comment_line), None)
    if own is None:
        return None

    top, bottom = _snippet_read_extent(snapshot, own)
    foreign = set()
    for record in records:
        if record is not own:
            a, b = _snippet_read_extent(snapshot, record)
            foreign.update(range(a, b + 1))
    rows = {row["line"]: row for row in _beacon_comment_rows(
        snapshot, language, first, last, evidence, section.get("start_byte"), section.get("end_byte"))}
    for direction, origin in ((-1, top - 1), (1, bottom + 1)):
        line = origin
        while first <= line <= last:
            if line in foreign:
                break
            if not lines[line - 1].strip():
                line += direction
                continue
            row = rows[line]
            if not row["form"] or row["text"].startswith("]"):
                break
            if direction < 0:
                top = line
            else:
                bottom = line
            line += direction
    return top, bottom


# CORE V1 KEEPER:
# Preserve the multi-language snippet engine: AST heuristics plus beacon/span
# extraction for Python, JS/TS, Vue, Markdown, YAML, SQL, and shell.
# @beacon[
#   id=bocsv1@multilang-resolution-core,
#   role=🔱 core keeper: multi-language ast and beacon span resolution helpers,
#   slice_labels=nexus-portability,nexus-core-v1,carto-fwd-source,
#   kind=span,
#   show_span=false,
# ]
#
# AST node resolver — consolidated multi-language implementation.
#
# The Python and JS/TS resolvers used to be ~300-line near-duplicates of each
# other (see git history before this consolidation). The only material
# differences were:
#   1. Which outline parser to call (parse_file_outline vs parse_js_ts_outline)
#   2. Which language keywords _classify_node_name should recognize
#      (Python: "class", "async def", "def"; JS/TS: "class", "function", "method")
#   3. A cosmetic error-message label
#
# Everything else — the normalize/extract-parent-classes/iter-nodes/build-
# candidates/make-snippet helpers and the 3-stage matching pipeline plus the
# token-search fallback — was byte-for-byte identical between the two
# resolvers. We now share that body via _resolve_ast_node_heuristic_shared and
# keep two thin wrappers so the dispatch site stays readable.


# @beacon[
#   id=auto-beacon@_normalize_core_name-shrd,
#   role=_normalize_core_name,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def _normalize_core_name(raw: str) -> str:
    """Strip leading decoration/emoji and trailing #tags from a node name.

    Identical for Python and JS/TS — emoji conventions, hashtag suffixes, and
    the leading-alnum-or-underscore-or-dollar rule are all language-agnostic.
    """
    name = (raw or "").strip()
    if not name:
        return ""
    # Strip leading non-alphanumeric decoration / emoji.
    i = 0
    while i < len(name) and not (name[i].isalnum() or name[i] in {"_", "$"}):
        i += 1
    name = name[i:].lstrip()
    # Drop trailing hashtag tags (e.g. "#model-forward").
    tokens = name.split()
    while tokens and tokens[-1].startswith("#"):
        tokens.pop()
    return " ".join(tokens).strip()


# @beacon[
#   id=auto-beacon@_classify_python_node_name-shrd,
#   role=_classify_python_node_name,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def _classify_python_node_name(raw: str) -> Tuple[str | None, str | None]:
    """Recognize Python keyword forms: ``class Foo``, ``async def foo``, ``def foo``."""
    core = _normalize_core_name(raw)
    if not core:
        return None, None
    lower = core.lower()
    # class Foo
    if lower.startswith("class "):
        rest = core[len("class ") :].strip()
        simple = rest.split("(", 1)[0].strip() or None
        return simple, "class"
    # async def foo(...)
    if lower.startswith("async def "):
        rest = core[len("async def ") :].strip()
        simple = rest.split("(", 1)[0].strip() or None
        return simple, "async_function"
    # def foo(...)
    if lower.startswith("def "):
        rest = core[len("def ") :].strip()
        simple = rest.split("(", 1)[0].strip() or None
        return simple, "function"
    # Fallback: treat as function if it looks like name(...)
    if "(" in core and ")" in core:
        simple = core.split("(", 1)[0].strip() or None
        return simple, "function"
    # Constant / attribute style (e.g. CONSTANT_NAME)
    return core.split()[0].strip(), "const"


# @beacon[
#   id=auto-beacon@_classify_js_ts_node_name-shrd,
#   role=_classify_js_ts_node_name,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def _classify_js_ts_node_name(raw: str) -> Tuple[str | None, str | None]:
    """Recognize JS/TS keyword forms: ``class Foo``, ``function foo``, ``method foo``."""
    core = _normalize_core_name(raw)
    if not core:
        return None, None
    lower = core.lower()
    if lower.startswith("class "):
        rest = core[len("class ") :].strip()
        simple = rest.split("(", 1)[0].strip() or None
        return simple, "class"
    if lower.startswith("function "):
        rest = core[len("function ") :].strip()
        simple = rest.split("(", 1)[0].strip() or None
        return simple, "function"
    if lower.startswith("method "):
        rest = core[len("method ") :].strip()
        simple = rest.split("(", 1)[0].strip() or None
        return simple, "method"
    if "(" in core and ")" in core:
        simple = core.split("(", 1)[0].strip() or None
        return simple, "function"
    return core.split()[0].strip(), "const"


# @beacon[
#   id=auto-beacon@_extract_parent_classes-shrd,
#   role=_extract_parent_classes,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def _extract_parent_classes(raw_parents: List[str]) -> List[str]:
    """Pull bare class names from a list of parent-node display labels.

    Identical for Python and JS/TS — the parent chain is expressed via the
    same ``class Foo`` prefix in both languages' outline display labels.
    """
    classes: List[str] = []
    for raw in raw_parents:
        core = _normalize_core_name(raw)
        if not core:
            continue
        lower = core.lower()
        if lower.startswith("class "):
            rest = core[len("class ") :].strip()
            cname = rest.split("(", 1)[0].strip()
            if cname:
                classes.append(cname)
    return classes


# @beacon[
#   id=auto-beacon@_iter_outline_nodes-shrd,
#   role=_iter_outline_nodes,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def _iter_outline_nodes(nodes: Iterable[dict]) -> Iterable[dict]:
    """Yield every dict in a NEXUS outline tree (pre-order)."""
    for n in nodes or []:
        if isinstance(n, dict):
            yield n
            for ch in n.get("children") or []:
                if isinstance(ch, dict):
                    yield from _iter_outline_nodes([ch])


# @beacon[
#   id=auto-beacon@_build_candidates_list-shrd,
#   role=_build_candidates_list,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def _build_candidates_list(cands: List[dict]) -> List[dict]:
    """Reduce candidate dicts to the minimal shape returned in ``metadata['candidates']``."""
    out: List[dict] = []
    for c in cands:
        out.append(
            {
                "ast_qualname": c.get("ast_qualname"),
                "ast_type": c.get("ast_type"),
                "start_line": c.get("start"),
                "end_line": c.get("end"),
            }
        )
    return out


# @beacon[
#   id=auto-beacon@_resolve_ast_node_heuristic_shared-shrd,
#   role=_resolve_ast_node_heuristic_shared,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Preserves the Python and JS/TS heuristic stages while building outline and physical lines from one binary snapshot; source is never reopened for the outline.,
# ]
def _resolve_ast_node_heuristic_shared(
    file_path: str,
    node_name: str,
    parent_names: List[str],
    context: int,
    *,
    parse_outline_fn,
    classify_node_name_fn,
    language_label: str,
) -> Tuple[int, int, List[str], int, int, int, dict]:
    """Shared AST-node resolver body for Python and JS/TS.

    Args:
        file_path: Absolute path to the source file.
        node_name: Display label for the AST node to resolve (e.g.
            ``"def foo(x)"`` or ``"function foo(x)"``).
        parent_names: Parent-node display labels (used to derive the parent
            class chain for qualname matching).
        context: Number of lines of padding to include above/below the
            resolved core span.
        parse_outline_fn: Language-specific outline parser. Must accept
            ``(file_path, snapshot=...)`` and return outline dicts without IO.
        classify_node_name_fn: Language-specific name classifier. Must accept
            a raw display label and return ``(simple_name, expected_type)``.
        language_label: Human-readable language tag woven into the
            no-search-token RuntimeError message (e.g. ``"AST"`` or
            ``"JS/TS AST"``). Cosmetic only.

    Returns: ``(start, end, lines, core_start, core_end, beacon_line, metadata)``.

    ``metadata`` includes:
        - ``resolution_strategy``: ``"ast_qualname_exact" | "ast_qualname_prefix" |
          "ast_simple_name" | "token_search"``
        - ``confidence``: 0.0–1.0
        - ``ambiguity``: ``"none" | "auto_resolved" | "disambiguation_needed"``
        - ``candidates``: list of dicts (when ambiguous)
    """
    if nexus_map_codebase is None:
        raise RuntimeError("nexus_map_codebase could not be imported")

    snapshot = read_source_snapshot(file_path)
    lines = list(snapshot.physical_lines)
    n = len(lines)

    try:
        outline_nodes = parse_outline_fn(file_path, snapshot=snapshot)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"Failed to parse {language_label} outline for {file_path}: {e}"
        ) from e

    metadata: dict = {
        "resolution_strategy": None,
        "confidence": 0.0,
        "ambiguity": "none",
        "candidates": None,
    }

    simple_name, expected_type = classify_node_name_fn(node_name)
    parent_classes = _extract_parent_classes(parent_names or [])

    qual_parts: List[str] = []
    if parent_classes:
        qual_parts.extend(parent_classes)
    if simple_name:
        qual_parts.append(simple_name)
    intended_qualname = ".".join(qual_parts) if qual_parts else None

    ast_candidates: List[dict] = []
    for node in _iter_outline_nodes(outline_nodes):
        qual = node.get("ast_qualname")
        if not isinstance(qual, str) or not qual:
            continue
        node_type = node.get("ast_type")
        start = node.get("orig_lineno_start_unused")
        end = node.get("orig_lineno_end_unused") or start
        if not isinstance(start, int) or start <= 0:
            continue
        if not isinstance(end, int) or end < start:
            end = start
        simple = qual.split(".")[-1]
        parents = qual.split(".")[:-1]
        ast_candidates.append(
            {
                "node": node,
                "ast_qualname": qual,
                "ast_type": node_type,
                "simple_name": simple,
                "parent_parts": parents,
                "start": start,
                "end": end,
            }
        )

    # @beacon[
    #   id=auto-beacon@_resolve_ast_node_heuristic_shared._make_snippet-shrd,
    #   role=_resolve_ast_node_heuristic_shared._make_snippet,
    #   slice_labels=carto-fwd-source,
    #   kind=ast,
    # ]
    def _make_snippet(
        cand: dict,
        strategy: str,
        confidence: float,
        ambiguity: str,
        all_candidates: List[dict] | None = None,
    ) -> Tuple[int, int, List[str], int, int, int, dict]:
        core_start = int(cand["start"])
        core_end = int(cand["end"])
        start_line = max(1, core_start - context)
        end_line = min(n, core_end + context)
        meta = {
            "resolution_strategy": strategy,
            "confidence": confidence,
            "ambiguity": ambiguity,
            "candidates": _build_candidates_list(all_candidates) if all_candidates else None,
        }
        return start_line, end_line, lines, core_start, core_end, core_start, meta

    # Stage 1: exact ast_qualname match
    if intended_qualname:
        exact = [
            c
            for c in ast_candidates
            if c["ast_qualname"] == intended_qualname
            and (expected_type is None or c.get("ast_type") == expected_type)
        ]
        if exact:
            ambiguity = "none" if len(exact) == 1 else "auto_resolved"
            return _make_snippet(exact[0], "ast_qualname_exact", 1.0, ambiguity, exact)

    # Stage 2: parent class chain prefix + simple name
    if simple_name and parent_classes:
        prefix_matches: List[dict] = []
        for c in ast_candidates:
            if c["simple_name"] != simple_name:
                continue
            parents = c["parent_parts"] or []
            if len(parents) >= len(parent_classes) and parents[-len(parent_classes) :] == parent_classes:
                if expected_type is None or c.get("ast_type") == expected_type:
                    prefix_matches.append(c)
        if prefix_matches:
            ambiguity = "none" if len(prefix_matches) == 1 else "auto_resolved"
            return _make_snippet(prefix_matches[0], "ast_qualname_prefix", 0.9, ambiguity, prefix_matches)

    # Stage 3: simple-name-only match within same ast_type
    if simple_name:
        simple_matches: List[dict] = []
        for c in ast_candidates:
            if c["simple_name"] != simple_name:
                continue
            if expected_type is not None and c.get("ast_type") != expected_type:
                continue
            simple_matches.append(c)
        if simple_matches:
            ambiguity = "none" if len(simple_matches) == 1 else "auto_resolved"
            return _make_snippet(simple_matches[0], "ast_simple_name", 0.7, ambiguity, simple_matches)

    # Final fallback: token search based on node_name
    search_text = _normalize_core_name(node_name)
    if "(" in search_text:
        search_text = search_text.split("(", 1)[0].strip()
    if not search_text:
        raise RuntimeError(
            "Cannot derive search token from node name %r for %s resolution"
            % (node_name, language_label)
        )

    anchor: int | None = None
    for idx, line in enumerate(lines, start=1):
        if search_text in line:
            anchor = idx
            break

    if anchor is None:
        raise RuntimeError(
            f"Search token {search_text!r} not found in file {file_path!r}"
        )

    core_start = anchor
    core_end = anchor
    start_line = max(1, core_start - context)
    end_line = min(n, core_end + context)
    metadata["resolution_strategy"] = "token_search"
    metadata["confidence"] = 0.4
    metadata["ambiguity"] = "none"
    metadata["candidates"] = None
    return start_line, end_line, lines, core_start, core_end, core_start, metadata


# @beacon[
#   id=carto-js-ts@_python_resolve_ast_node_heuristic,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-snippets,ra-snippet-range-ast-py,ra-reconcile,carto-fwd-source,
#   kind=span,
# ]
# Phase 3 JS/TS: Python AST heuristic template.
# Reference for a future JS/TS heuristic helper that will resolve
# JS/TS AST nodes (by node_name + parent_names) to snippets using
# Tree-sitter-based outlines.
# @beacon[
#   id=auto-beacon@_python_resolve_ast_node_heuristic-3t00,
#   role=_python_resolve_ast_node_heuristic,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def _python_resolve_ast_node_heuristic(
    file_path: str,
    node_name: str,
    parent_names: List[str],
    context: int,
) -> Tuple[int, int, List[str], int, int, int, dict]:
    """Resolve a non-beacon Python AST node to a snippet using AST qualname matching.

    Returns: (start, end, lines, core_start, core_end, beacon_line, metadata)

    Where metadata includes:
    - resolution_strategy: "ast_qualname_exact" | "ast_qualname_prefix" | "ast_simple_name" | "token_search"
    - confidence: 0.0-1.0
    - ambiguity: "none" | "auto_resolved" | "disambiguation_needed"
    - candidates: list of dicts (when ambiguous)

    Implementation: thin wrapper over _resolve_ast_node_heuristic_shared with
    the Python outline parser and the Python keyword classifier. See the
    shared resolver's docstring for the matching pipeline.
    """
    if nexus_map_codebase is None:
        raise RuntimeError("nexus_map_codebase could not be imported")

    # @beacon[
    #   id=carto-js-ts@_python_resolve_ast_node_heuristic-TEST,
    #   role=🔱 carto-js-ts-TEST,
    #   slice_labels=carto-js-ts,carto-js-ts-snippets,ra-snippet-range-ast-py,ra-reconcile,carto-fwd-source,
    #   kind=span,
    # ]
    # Span anchor preserved across the consolidation refactor. Used by the
    # carto-js-ts test slice to track this resolver's identity in Workflowy.
    # @beacon-close[
    #   id=carto-js-ts@_python_resolve_ast_node_heuristic-TEST,
    # ]

    return _resolve_ast_node_heuristic_shared(
        file_path,
        node_name,
        parent_names,
        context,
        parse_outline_fn=nexus_map_codebase.parse_file_outline,  # type: ignore[attr-defined]
        classify_node_name_fn=_classify_python_node_name,
        language_label="AST",
    )


# @beacon[
#   id=carto-js-ts@_js_ts_resolve_ast_node_heuristic,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-snippets,ra-snippet-range-ast-js-ts,ra-reconcile,carto-fwd-source,
#   kind=span,
# ]
# Phase 3 JS/TS: JS/TS AST heuristic helper.
# Resolves JS/TS AST nodes (by node_name + parent_names) to snippets using
# Tree-sitter-based outlines produced by parse_js_ts_outline.
# @beacon[
#   id=auto-beacon@_js_ts_resolve_ast_node_heuristic-5vcf,
#   role=_js_ts_resolve_ast_node_heuristic,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def _js_ts_resolve_ast_node_heuristic(
    file_path: str,
    node_name: str,
    parent_names: List[str],
    context: int,
) -> Tuple[int, int, List[str], int, int, int, dict]:
    """Resolve a non-beacon JS/TS AST node to a snippet using AST qualname matching.

    Returns the same tuple shape as :func:`_python_resolve_ast_node_heuristic`.

    Implementation: thin wrapper over _resolve_ast_node_heuristic_shared with
    the JS/TS outline parser and the JS/TS keyword classifier. See the shared
    resolver's docstring for the matching pipeline.
    """
    if nexus_map_codebase is None:
        raise RuntimeError("nexus_map_codebase could not be imported")

    return _resolve_ast_node_heuristic_shared(
        file_path,
        node_name,
        parent_names,
        context,
        parse_outline_fn=nexus_map_codebase.parse_js_ts_outline,  # type: ignore[attr-defined]
        classify_node_name_fn=_classify_js_ts_node_name,
        language_label="JS/TS AST",
    )


# ---------------------------------------------------------------------------
# Python beacon snippet extraction
# ---------------------------------------------------------------------------


# @beacon[
#   id=auto-beacon@_python_find_closing_beacon_span-6az7,
#   role=ƒ _python_find_closing_beacon_span(lines: List[str], beacons: List[dict], target_id: str),
#   slice_labels=ra-snippet-range-span-beacon,ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Compatibility delegate only: U01 lexical records decide the unique exact Python pair and its inner range, including an empty pair; production reads consume those records directly.,
# ]
def _python_find_closing_beacon_span(
    lines: List[str],
    beacons: List[dict],
    target_id: str,
) -> Optional[Tuple[int, int]]:
    """Compatibility delegate to U01's exact Python pair, including empty pairs."""
    return _paired_span_for_id(lines, "python", target_id)


# @beacon[
#   id=model:forward@1,
#   role=ƒ _python_find_ast_node_for_beacon(outline_nodes: List[dict], beacon_id: str),
#   slice_labels=model-forward,ra-snippet-range-ast-beacon-py,carto-fwd-source,
#   kind=ast,
#   comment=Compatibility-only unique AST metadata lookup through scoped BEACON header records; never first-match selection or span-text id parsing, and production extraction selects via U03.,
# ]
def _python_find_ast_node_for_beacon(
    outline_nodes: List[dict], beacon_id: str
) -> Optional[dict]:
    """Compatibility metadata lookup, unique only; extraction uses U03 instead."""
    matches = [node for node in _iter_outline_nodes(outline_nodes)
               if any(record.get("id") == beacon_id.strip() and record.get("kind") == "ast"
                      for record in parse_beacon_note_metadata(node.get("note")))]
    return matches[0] if len(matches) == 1 else None


# @beacon[
#   id=auto-beacon@_python_comment_block_span-7bt5,
#   role=ƒ _python_comment_block_span(lines: List[str], comment_line: int),
#   slice_labels=ra-snippet-range-span-beacon,ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Delegates Python own-block and ordinary-comment bounds to shared scanner records, stopping before foreign metadata.,
# ]
def _python_comment_block_span(
    lines: List[str],
    comment_line: int,
) -> Optional[Tuple[int, int]]:
    """Delegate Python own-block/context geometry to the shared record view."""
    return _comment_block_span_from_records(lines, comment_line, "python")


# @beacon[
#   id=auto-beacon@_comment_block_span_js-shrd,
#   role=_comment_block_span_js,
#   slice_labels=ra-snippet-range-span-beacon,ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Delegates JS/TS own-block and ordinary-comment bounds to shared scanner and lexical rows, excluding literals and foreign metadata.,
# ]
def _comment_block_span_js(
    lines: List[str], comment_line: int
) -> Tuple[int, int]:
    """Delegate JS/TS own-block/context geometry to shared lexical records."""
    return _comment_block_span_from_records(lines, comment_line, "js_ts") or (comment_line, comment_line)


# @beacon[
#   id=carto-js-ts@_python_snippet_for_beacon,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-snippets,ra-snippet-range-span-beacon,ra-snippet-range-ast-beacon-py,ra-read-text-snippet,carto-fwd-source,
#   kind=span,
# ]
# Python and JS/TS beacon readers now build the same typed result;
# the public dispatcher alone projects its compatibility tuple.
# @beacon[
#   id=auto-beacon@_python_snippet_for_beacon-xf1j,
#   role=_python_snippet_for_beacon,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Builds a typed Python snippet through the single snapshot reader and U01/U03 geometry; includes own block and full AST or closed/open-only span.,
# ]
def _python_snippet_for_beacon(
    file_path: str,
    beacon_id: str,
    context: int,
) -> BeaconSnippetResult:
    """Build the Python result through the shared snapshot/geometry authority."""
    return get_beacon_snippet_result(file_path, beacon_id, context)


# @beacon[
#   id=auto-beacon@_js_ts_find_closing_beacon_span-9kja,
#   role=ƒ _js_ts_find_closing_beacon_span(lines: List[str], beacons: List[dict], target_id: str),
#   slice_labels=ra-snippet-range-span-beacon,ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Compatibility delegate only: shared lexical records and exact pairing provide the JS/TS inner range, never a private close scan.,
# ]
def _js_ts_find_closing_beacon_span(
    lines: List[str],
    beacons: List[dict],
    target_id: str,
) -> Optional[Tuple[int, int]]:
    """Compatibility delegate to U01's exact JS/TS pair, including empty pairs."""
    return _paired_span_for_id(lines, "js_ts", target_id)


# @beacon[
#   id=carto-js-ts@_js_ts_find_ast_node_for_beacon,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-snippets,ra-snippet-range-ast-beacon-js-ts,carto-fwd-source,
#   kind=span,
# ]
# Phase 3 JS/TS: JS/TS AST node finder for beacons.
# Searches recursively through the outline tree for AST nodes with matching beacon IDs.
# @beacon[
#   id=auto-beacon@_js_ts_find_ast_node_for_beacon-zowk,
#   role=ƒ _js_ts_find_ast_node_for_beacon(outline_nodes: List[dict], beacon_id: str),
#   slice_labels=test-beacon,carto-fwd-source,
#   kind=ast,
#   comment=Compatibility delegate to the scoped unique AST metadata lookup; production beacon reads select physical attachments with U03.,
# ]
def _js_ts_find_ast_node_for_beacon(
    outline_nodes: List[dict],
    beacon_id: str,
) -> Optional[dict]:
    """Compatibility delegate to the shared unique scoped-note lookup."""
    return _python_find_ast_node_for_beacon(outline_nodes, beacon_id)


# @beacon[
#   id=carto-js-ts@_js_ts_snippet_for_beacon,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-snippets,ra-snippet-range-span-beacon,ra-snippet-range-ast-beacon-js-ts,ra-read-text-snippet,carto-fwd-source,
#   kind=span,
# ]
# Phase 3 JS/TS: JS/TS beacon snippet helper.
# Builds the shared typed result; get_snippet_data projects its legacy tuple.
# @beacon[
#   id=auto-beacon@_js_ts_snippet_for_beacon-8x4f,
#   role=_js_ts_snippet_for_beacon,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Builds the typed JS/TS read through the single snapshot and shared geometry, retaining complete AST bodies and section-independent exact span pairing.,
# ]
def _js_ts_snippet_for_beacon(
    file_path: str,
    beacon_id: str,
    context: int,
) -> BeaconSnippetResult:
    """Build the JS/TS result through the shared snapshot/geometry authority."""
    return get_beacon_snippet_result(file_path, beacon_id, context)


# ---------------------------------------------------------------------------
# Markdown beacon snippet extraction
# ---------------------------------------------------------------------------


# @beacon[
#   id=auto-beacon@_markdown_find_closing_beacon_span-r36i,
#   role=ƒ _markdown_find_closing_beacon_span(lines: List[str], beacons: List[dict], target_id: str),
#   slice_labels=ra-snippet-range-span-beacon,ra-read-text-snippet,carto-fwd-markdown,
#   kind=ast,
#   comment=Compatibility delegate only: U01 Markdown records own fence exclusion and exact close pairing; no local delimiter scanner remains.,
# ]
def _markdown_find_closing_beacon_span(
    lines: List[str],
    beacons: List[dict],
    target_id: str,
) -> Optional[Tuple[int, int]]:
    """Compatibility delegate to U01's fence-aware exact Markdown pair."""
    return _paired_span_for_id(lines, "markdown", target_id)


# @beacon[
#   id=auto-beacon@_markdown_comment_block_span-y6pq,
#   role=ƒ _markdown_comment_block_span(lines: List[str], comment_line: int),
#   slice_labels=ra-snippet-range-span-beacon,ra-read-text-snippet,carto-fwd-markdown,
#   kind=ast,
#   comment=Delegates Markdown owned-wrapper and ordinary-comment bounds to shared scanner records, excluding fences and foreign metadata.,
# ]
def _markdown_comment_block_span(
    lines: List[str],
    comment_line: int,
) -> Optional[Tuple[int, int]]:
    """Delegate Markdown own-wrapper/context geometry to shared records."""
    return _comment_block_span_from_records(lines, comment_line, "markdown")


# @beacon[
#   id=auto-beacon@_markdown_snippet_for_beacon-1whb,
#   role=ƒ _markdown_snippet_for_beacon(file_path: str, beacon_id: str, context: int),
#   slice_labels=ra-snippet-range-span-beacon,ra-snippet-range-ast-beacon-md,ra-read-text-snippet,carto-fwd-markdown,
#   kind=ast,
#   comment=Builds a typed Markdown beacon read using own wrapper records and the same canonical heading-section authority as MD_PATH, with explicit text anchors preserved.,
# ]
def _markdown_snippet_for_beacon(
    file_path: str,
    beacon_id: str,
    context: int,
) -> BeaconSnippetResult:
    """Build a Markdown block/heading section from the canonical authorities."""
    return get_beacon_snippet_result(file_path, beacon_id, context)


# ---------------------------------------------------------------------------
# YAML beacon snippet extraction
# ---------------------------------------------------------------------------


# @beacon[
#   id=auto-beacon@_yaml_comment_block_span-1whb,
#   role=ƒ _yaml_comment_block_span(lines: List[str], comment_line: int),
#   slice_labels=ra-snippet-range-span-beacon,ra-snippet-range-ast-beacon-yaml,ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Delegates YAML own-block and ordinary-comment bounds to shared scanner records, stopping before foreign metadata.,
# ]
def _yaml_comment_block_span(
    lines: List[str],
    comment_line: int,
) -> Optional[Tuple[int, int]]:
    """Delegate YAML own-block/context geometry to the shared record view."""
    return _comment_block_span_from_records(lines, comment_line, "yaml")


# @beacon[
#   id=auto-beacon@_yaml_snippet_for_beacon-1whb,
#   role=ƒ _yaml_snippet_for_beacon(file_path: str, beacon_id: str, context: int),
#   slice_labels=ra-snippet-range-span-beacon,ra-snippet-range-ast-beacon-yaml,ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Builds typed YAML windows directly from U01 pairing and own metadata; span text never supplies identity and open-only spans extend to EOF.,
# ]
def _yaml_snippet_for_beacon(
    file_path: str,
    beacon_id: str,
    context: int,
) -> BeaconSnippetResult:
    """Build YAML windows from own records, never projected span line fields."""
    return get_beacon_snippet_result(file_path, beacon_id, context)


# ---------------------------------------------------------------------------
# SQL beacon snippet extraction
# ---------------------------------------------------------------------------


# @beacon[
#   id=auto-beacon@_sql_find_closing_beacon_span-1obv,
#   role=ƒ _sql_find_closing_beacon_span(lines: List[str], beacons: List[dict], target_id: str),
#   slice_labels=ra-snippet-range-span-beacon,ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Compatibility delegate only: U01 SQL lexical records supply exact paired inner lines; no private block end or close scanner remains.,
# ]
def _sql_find_closing_beacon_span(
    lines: List[str],
    beacons: List[dict],
    target_id: str,
) -> Optional[Tuple[int, int]]:
    """Compatibility delegate to U01's exact SQL pair, excluding literals."""
    return _paired_span_for_id(lines, "sql", target_id)


# @beacon[
#   id=auto-beacon@_sql_comment_block_span-mrwt,
#   role=ƒ _sql_comment_block_span(lines: List[str], comment_line: int),
#   slice_labels=ra-snippet-range-span-beacon,ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Delegates SQL own-block and ordinary-comment bounds to shared scanner records, stopping before foreign metadata.,
# ]
def _sql_comment_block_span(
    lines: List[str],
    comment_line: int,
) -> Optional[Tuple[int, int]]:
    """Delegate SQL own-block/context geometry to the shared record view."""
    return _comment_block_span_from_records(lines, comment_line, "sql")


# @beacon[
#   id=auto-beacon@_sql_snippet_for_beacon-7pxi,
#   role=ƒ _sql_snippet_for_beacon(file_path: str, beacon_id: str, context: int),
#   slice_labels=ra-snippet-range-span-beacon,ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Builds the typed SQL read through shared snapshot geometry; closed and legal open-only spans need no nonexistent compute_span fallback.,
# ]
def _sql_snippet_for_beacon(
    file_path: str,
    beacon_id: str,
    context: int,
) -> BeaconSnippetResult:
    """Build SQL windows through shared records, including open-only spans."""
    return get_beacon_snippet_result(file_path, beacon_id, context)

# ---------------------------------------------------------------------------
# Public CLI: beacon snippet extraction (print helper)
# ---------------------------------------------------------------------------


# @beacon[
#   id=auto-beacon@_print_snippet-84nz,
#   role=_print_snippet,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def _print_snippet(
    file_path: str,
    beacon_id: str,
    start: int,
    end: int,
    lines: List[str],
) -> None:
    rel = os.path.relpath(file_path, start=os.getcwd())
    print(f"File: {file_path} ({rel})")
    print(f"Beacon id: {beacon_id}")
    print(f"Lines: {start}–{end}\n")
    print("```")
    for lineno in range(start, end + 1):
        if 1 <= lineno <= len(lines):
            print(f"{lineno:5d}: {lines[lineno - 1]}")
    print("```")


# ---------------------------------------------------------------------------
# Shell beacon snippet extraction
# ---------------------------------------------------------------------------


# @beacon[
#   id=auto-beacon@_sh_find_closing_beacon_span-4gfg,
#   role=ƒ _sh_find_closing_beacon_span(lines: List[str], beacons: List[dict], target_id: str),
#   slice_labels=ra-snippet-range-span-beacon,ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Compatibility delegate only: U01 shell lexical evidence and exact pairing supply inner lines, excluding heredoc examples without a private scanner.,
# ]
def _sh_find_closing_beacon_span(
    lines: List[str],
    beacons: List[dict],
    target_id: str,
) -> Optional[Tuple[int, int]]:
    """Compatibility delegate to U01's exact shell pair, excluding heredocs."""
    return _paired_span_for_id(lines, "shell", target_id)


# @beacon[
#   id=auto-beacon@_sh_comment_block_span-bul0,
#   role=ƒ _sh_comment_block_span(lines: List[str], comment_line: int),
#   slice_labels=ra-snippet-range-span-beacon,ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Delegates shell own-block and ordinary-comment bounds to shared scanner records, stopping before foreign metadata.,
# ]
def _sh_comment_block_span(
    lines: List[str],
    comment_line: int,
) -> Optional[Tuple[int, int]]:
    """Delegate shell own-block/context geometry to the shared record view."""
    return _comment_block_span_from_records(lines, comment_line, "shell")


# @beacon[
#   id=auto-beacon@_sh_snippet_for_beacon-vegx,
#   role=ƒ _sh_snippet_for_beacon(file_path: str, beacon_id: str, context: int),
#   slice_labels=ra-snippet-range-span-beacon,ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Builds a total typed shell read through the one snapshot/pairing authority; empty spans are closed and open-only spans reach EOF rather than returning None.,
# ]
def _sh_snippet_for_beacon(
    file_path: str,
    beacon_id: str,
    context: int,
) -> BeaconSnippetResult:
    """Build a total shell read through shared pairing; never return None."""
    return get_beacon_snippet_result(file_path, beacon_id, context)

# @beacon[
#   id=carto-js-ts@get_snippet_for_ast_qualname,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-snippets,ra-snippet-range-ast-py,ra-read-text-snippet,carto-fwd-source,
#   kind=span,
# ]
# Phase 3 JS/TS: AST-qualname snippet template.
# Reference for a future get_snippet_for_ast_qualname_js_ts(...)
# that will locate JS/TS AST nodes by Tree-sitter ast_qualname and
# return snippets with the same (start,end,core,beacon_line,metadata)
# contract.
# @beacon[
#   id=auto-beacon@get_snippet_for_ast_qualname-an1g,
#   role=get_snippet_for_ast_qualname,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Resolves one Python AST_QUALNAME from the same snapshot as its physical lines; zero, multiple or failed-outline outcomes are typed with file/id/evidence, never a stale-line guess.,
# ]
def get_snippet_for_ast_qualname(
    file_path: str,
    ast_qualname: str,
    context: int,
) -> Tuple[int, int, List[str], int, int, int, dict]:
    """Resolve snippet for a Python AST node identified by its ast_qualname.

    This is the non-beacon Python path; one snapshot supplies outline and lines.

    Contract:
    - If exactly one AST node in the outline has `ast_qualname == ast_qualname`,
      return a snippet spanning its [start,end] lines with the usual context
      padding.
    - If zero or multiple matches, raise a typed RuntimeError outcome to
      the caller (MCP tool should surface this, not silently guess).
    """
    if nexus_map_codebase is None:
        raise RuntimeError("nexus_map_codebase could not be imported")

    ast_qualname = (ast_qualname or "").strip()
    if not ast_qualname:
        raise RuntimeError("Empty ast_qualname for AST snippet resolution")

    snapshot = _read_snippet_snapshot(file_path, ast_qualname)
    lines = list(snapshot.physical_lines)
    n = len(lines)

    try:
        outline_nodes = nexus_map_codebase.parse_file_outline(file_path, snapshot=snapshot)  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Failed to parse Python AST for {file_path}: {e}") from e

    # @beacon[
    #   id=auto-beacon@get_snippet_for_ast_qualname._iter_nodes-px6e,
    #   role=get_snippet_for_ast_qualname._iter_nodes,
    #   slice_labels=carto-fwd-source,
    #   kind=ast,
    # ]
    def _iter_nodes(nodes: Iterable[dict]) -> Iterable[dict]:
        for node in nodes or []:
            if isinstance(node, dict):
                yield node
                for ch in node.get("children") or []:
                    if isinstance(ch, dict):
                        yield from _iter_nodes([ch])

    matches: List[dict] = []
    for node in _iter_nodes(outline_nodes):
        qual = node.get("ast_qualname")
        if isinstance(qual, str) and qual == ast_qualname:
            matches.append(node)

    if not matches:
        raise _qualname_error(snapshot, ast_qualname, "python", outline_nodes,
                              f"AST_QUALNAME {ast_qualname!r} not found in Python file {file_path!r}", "not_found")
    if len(matches) > 1:
        raise _qualname_error(snapshot, ast_qualname, "python", outline_nodes,
            f"AST_QUALNAME {ast_qualname!r} is ambiguous in Python file {file_path!r} "
            f"({len(matches)} matches); refresh Cartographer mapping.", "ambiguous")

    node = matches[0]
    start = node.get("orig_lineno_start_unused")
    end = node.get("orig_lineno_end_unused") or start
    if not isinstance(start, int) or start <= 0:
        raise RuntimeError(
            f"AST node {ast_qualname!r} has invalid start line {start!r} in {file_path!r}"
        )
    if not isinstance(end, int) or end < start:
        end = start

    core_start = start
    core_end = end
    start_line = max(1, core_start - context)
    end_line = min(n, core_end + context)

    metadata = {
        "resolution_strategy": "ast_qualname_exact",
        "confidence": 1.0,
        "ambiguity": "none",
        "candidates": None,
    }
    return BeaconSnippetResult(snapshot, lines, None, None, core_start, core_end, core_start,
                               context=context, resolution_strategy=metadata["resolution_strategy"]).legacy_tuple()


# @beacon[
#   id=carto-js-ts@get_snippet_for_ast_qualname_js_ts,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-snippets,ra-snippet-range-ast-js-ts,ra-read-text-snippet,carto-fwd-source,
#   kind=span,
# ]
# JS/TS AST-qualname snippet helper.
# Locates JS/TS AST nodes by ast_qualname and returns a snippet
# using the same contract as the Python version.
# @beacon[
#   id=auto-beacon@get_snippet_for_ast_qualname_js_ts-txeb,
#   role=get_snippet_for_ast_qualname_js_ts,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Keeps JS/TS AST_QUALNAME uniqueness and one source observation while projecting typed not-found, ambiguity and outline-failure evidence beside compatible successful tuples.,
# ]
def get_snippet_for_ast_qualname_js_ts(
    file_path: str,
    ast_qualname: str,
    context: int,
) -> Tuple[int, int, List[str], int, int, int, dict]:
    """Resolve one JS/TS qualname or a typed extraction failure; never guess."""
    if nexus_map_codebase is None:
        raise RuntimeError("nexus_map_codebase could not be imported")

    ast_qualname = (ast_qualname or "").strip()
    if not ast_qualname:
        raise RuntimeError("Empty ast_qualname for AST snippet resolution (JS/TS)")

    snapshot = _read_snippet_snapshot(file_path, ast_qualname)
    lines = list(snapshot.physical_lines)
    n = len(lines)

    try:
        outline_nodes = nexus_map_codebase.parse_js_ts_outline(file_path, snapshot=snapshot)  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Failed to parse JS/TS AST for {file_path}: {e}") from e

    # @beacon[
    #   id=auto-beacon@get_snippet_for_ast_qualname_js_ts._iter_nodes-w7ox,
    #   role=get_snippet_for_ast_qualname_js_ts._iter_nodes,
    #   slice_labels=carto-fwd-source,
    #   kind=ast,
    # ]
    def _iter_nodes(nodes: Iterable[dict]) -> Iterable[dict]:
        for node in nodes or []:
            if isinstance(node, dict):
                yield node
                for ch in node.get("children") or []:
                    if isinstance(ch, dict):
                        yield from _iter_nodes([ch])

    matches: List[dict] = []
    for node in _iter_nodes(outline_nodes):
        qual = node.get("ast_qualname")
        if isinstance(qual, str) and qual == ast_qualname:
            matches.append(node)

    if not matches:
        raise _qualname_error(snapshot, ast_qualname, "js_ts", outline_nodes,
                              f"AST_QUALNAME {ast_qualname!r} not found in JS/TS file {file_path!r}", "not_found")
    if len(matches) > 1:
        raise _qualname_error(snapshot, ast_qualname, "js_ts", outline_nodes,
            f"AST_QUALNAME {ast_qualname!r} is ambiguous in JS/TS file {file_path!r} "
            f"({len(matches)} matches); refresh Cartographer mapping.", "ambiguous")

    node = matches[0]
    start = node.get("orig_lineno_start_unused")
    end = node.get("orig_lineno_end_unused") or start
    if not isinstance(start, int) or start <= 0:
        raise RuntimeError(
            f"AST node {ast_qualname!r} has invalid start line {start!r} in {file_path!r}"
        )
    if not isinstance(end, int) or end < start:
        end = start

    core_start = start
    core_end = end
    start_line = max(1, core_start - context)
    end_line = min(n, core_end + context)

    metadata = {
        "resolution_strategy": "js_ts_ast_qualname_exact",
        "confidence": 1.0,
        "ambiguity": "none",
        "candidates": None,
    }
    return BeaconSnippetResult(snapshot, lines, None, None, core_start, core_end, core_start,
                               context=context, resolution_strategy=metadata["resolution_strategy"]).legacy_tuple()


# @beacon[
#   id=auto-beacon@get_snippet_for_ast_qualname_vue-3v1c,
#   role=ƒ get_snippet_for_ast_qualname_vue(file_path: str, ast_qualname: str, context: int),
#   slice_labels=carto-js-ts,ra-read-text-snippet,carto-fwd-source,
#   kind=ast,
#   comment=Keeps unique Vue AST_QUALNAME resolution and current rebased section lines, with typed not-found, ambiguity and outline-failure outcomes carrying the same source observation.,
# ]
def get_snippet_for_ast_qualname_vue(
    file_path: str,
    ast_qualname: str,
    context: int,
) -> Tuple[int, int, List[str], int, int, int, dict]:
    """Resolve snippet for a Vue script AST node identified by its AST_QUALNAME.

    Strategy:
    - Use the same snapshot in parse_vue_outline(...) to obtain the rebased
      JS/TS AST nodes (which already have absolute file line numbers).
    - Match ast_qualname exactly; failed extraction retains typed evidence.
    """
    if nexus_map_codebase is None:
        raise RuntimeError("nexus_map_codebase could not be imported")

    ast_qualname = (ast_qualname or "").strip()
    if not ast_qualname:
        raise RuntimeError("Empty ast_qualname for Vue AST snippet resolution")

    snapshot = _read_snippet_snapshot(file_path, ast_qualname)
    lines = list(snapshot.physical_lines)
    n = len(lines)

    try:
        outline_nodes = nexus_map_codebase.parse_vue_outline(file_path, snapshot=snapshot)  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Failed to parse Vue outline for {file_path}: {e}") from e

    # @beacon[
    #   id=auto-beacon@get_snippet_for_ast_qualname_vue._iter_nodes-nrz9,
    #   role=get_snippet_for_ast_qualname_vue._iter_nodes,
    #   slice_labels=carto-fwd-source,
    #   kind=ast,
    # ]
    def _iter_nodes(nodes: Iterable[dict]) -> Iterable[dict]:
        for node in nodes or []:
            if isinstance(node, dict):
                yield node
                for ch in node.get("children") or []:
                    if isinstance(ch, dict):
                        yield from _iter_nodes([ch])

    matches: List[dict] = []
    for node in _iter_nodes(outline_nodes):
        qual = node.get("ast_qualname")
        if isinstance(qual, str) and qual == ast_qualname:
            matches.append(node)

    if not matches:
        raise _qualname_error(snapshot, ast_qualname, "vue", outline_nodes,
                              f"AST_QUALNAME {ast_qualname!r} not found in Vue file {file_path!r}", "not_found")
    if len(matches) > 1:
        raise _qualname_error(snapshot, ast_qualname, "vue", outline_nodes,
            f"AST_QUALNAME {ast_qualname!r} is ambiguous in Vue file {file_path!r} "
            f"({len(matches)} matches); refresh Cartographer mapping.", "ambiguous")

    node = matches[0]
    start = node.get("orig_lineno_start_unused")
    end = node.get("orig_lineno_end_unused") or start
    if not isinstance(start, int) or start <= 0:
        raise RuntimeError(
            f"AST node {ast_qualname!r} has invalid start line {start!r} in {file_path!r}"
        )
    if not isinstance(end, int) or end < start:
        end = start

    core_start = start
    core_end = end
    start_line = max(1, core_start - context)
    end_line = min(n, core_end + context)

    metadata = {
        "resolution_strategy": "vue_ast_qualname_exact",
        "confidence": 1.0,
        "ambiguity": "none",
        "candidates": None,
    }
    return BeaconSnippetResult(snapshot, lines, None, None, core_start, core_end, core_start,
                               context=context, resolution_strategy=metadata["resolution_strategy"]).legacy_tuple()


# @beacon[
#   id=auto-beacon@_vue_snippet_for_beacon-b4vr,
#   role=_vue_snippet_for_beacon,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Builds a typed Vue read from exact ids and actual U01 SFC section records, including script AST bodies and section-bounded spans; no substring id or inline close/window scanner remains.,
# ]
def _vue_snippet_for_beacon(
    file_path: str,
    beacon_id: str,
    context: int,
) -> BeaconSnippetResult:
    """Build a Vue read from exact-id, section-bounded U01 records and outline."""
    return get_beacon_snippet_result(file_path, beacon_id, context)


# @beacon-close[
#   id=bocsv1@multilang-resolution-core,
# ]
# CORE V1 KEEPER:
# Keep the public snippet surface centered on get_snippet_data, AST_QUALNAME
# resolution, and MD_PATH heading resolution. These are the installable API
# contract that other tools and agents build on.
# @beacon[
#   id=bocsv1@public-snippet-api-core,
#   role=🔱 core keeper: public snippet api plus ast-qualname and md-path resolution,
#   slice_labels=nexus-portability,nexus-core-v1,carto-fwd-source,
#   kind=span,
#   show_span=false,
# ]
# ---------------------------------------------------------------------------
# Public library API: beacon snippet extraction
# ---------------------------------------------------------------------------


# @beacon[
#   id=carto-js-ts@get_snippet_data,
#   role=🔱 carto-js-ts,
#   slice_labels=carto-js-ts,carto-js-ts-snippets,nexus-md-header-path,ra-snippet-range,ra-read-text-snippet,carto-fwd-source,
#   kind=span,
# ]
# Phase 3 JS/TS: central dispatch point for snippet resolution.
# This will be extended so that .js/.ts/.tsx files route to JS/TS-
# specific helpers alongside the existing Python/Markdown/SQL/SH
# paths.
# Likely new helpers (names tentative):
#   - _js_ts_snippet_for_beacon(file_path, beacon_id, context)
#   - _js_ts_resolve_ast_node_heuristic(file_path, node_name, parent_names, context)
#   - get_snippet_for_ast_qualname_js_ts(file_path, ast_qualname, context)
# @beacon[
#   id=auto-beacon@get_snippet_data-bk2v,
#   role=get_snippet_data,
#   slice_labels=carto-fwd-source,
#   kind=ast,
#   comment=Seven-tuple adapter over typed readers with optional independent U03 target evidence; projects observation, findings and close geometry, preserving source-extraction errors instead of making every result appear clean.,
# ]
def get_snippet_data(
    file_path: str,
    beacon_id: str,
    context: int,
    node_name: str | None = None,
    parent_names: List[str] | None = None,
    *, target: dict | None = None,
) -> Tuple[int, int, List[str], int, int, int, dict]:
    """Return (start_line, end_line, lines, core_start, core_end, beacon_line, metadata).

    - `start_line` / `end_line` include the requested `context` lines around
      the core region.
    - `core_start` / `core_end` mark the minimal span that always contains the
      associated beacon comment block (if any) and the relevant code span.
    - `beacon_line` is the exact line of the @beacon[...] tag itself (for
      editor navigation independent of context/core) *or* the primary anchor
      line for non-beacon AST nodes.
    - `metadata` projects resolution, location/span geometry and findings from
      the typed result (or the preserved non-beacon heuristic path).
      `target` carries current independent source identity, never a cache line.

    Callers that only care about the visible window can ignore `core_*`,
    `beacon_line`, and `metadata`. Callers that want to drive an editor to the
    beacon tag line (e.g., Windsurf `-g file:line`) should use `beacon_line`.
    """
    file_path = os.path.abspath(file_path)
    ext = Path(file_path).suffix.lower()

    if not os.path.isfile(file_path):
        raise BeaconSnippetError("Source file not found", file_path=file_path,
                                 beacon_id=beacon_id, outcome="file_missing")

    # Non-beacon AST node path: node_name provided → use AST-first
    # resolver. For beacon-based lookups we call this function without
    # node_name, which triggers the original beacon-centric behavior.
    if ext == ".py" and node_name is not None:
        return _python_resolve_ast_node_heuristic(
            file_path,
            node_name,
            parent_names or [],
            context,
        )
    if ext in {".js", ".jsx", ".ts", ".tsx"} and node_name is not None:
        return _js_ts_resolve_ast_node_heuristic(
            file_path,
            node_name,
            parent_names or [],
            context,
        )

    reader = {".py": _python_snippet_for_beacon,
              ".js": _js_ts_snippet_for_beacon, ".jsx": _js_ts_snippet_for_beacon,
              ".ts": _js_ts_snippet_for_beacon, ".tsx": _js_ts_snippet_for_beacon,
              ".vue": _vue_snippet_for_beacon,
              ".md": _markdown_snippet_for_beacon, ".markdown": _markdown_snippet_for_beacon,
              ".sql": _sql_snippet_for_beacon, ".yml": _yaml_snippet_for_beacon,
              ".yaml": _yaml_snippet_for_beacon, ".sh": _sh_snippet_for_beacon}.get(ext)
    if reader is None:
        raise RuntimeError(f"Unsupported file extension for beacon snippets: {ext}")
    if target is not None:
        return get_beacon_snippet_result(file_path, beacon_id, context, target=target).legacy_tuple()
    return reader(file_path, beacon_id, context).legacy_tuple()


# SPLIT BOUNDARY:
# The thin human CLI is convenient, but it can move to a wrapper script if
# Core v1 wants the library API packaged separately from a standalone command.
# @beacon[
#   id=bocsv1@cli-wrapper-boundary,
#   role=boundary split: thin standalone cli wrapper around core snippet library,
#   slice_labels=nexus-portability,nexus-split-boundary,
#   kind=span,
#   show_span=false,
# ]
# @beacon[
#   id=auto-beacon@extract_snippet-tecr,
#   role=extract_snippet,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def extract_snippet(file_path: str, beacon_id: str, context: int) -> None:
    """CLI wrapper: resolve snippet and print with line numbers for humans."""
    start, end, lines, _core_start, _core_end, _beacon_line, _meta = get_snippet_data(
        file_path, beacon_id, context
    )
    _print_snippet(os.path.abspath(file_path), beacon_id, start, end, lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


# @beacon[
#   id=auto-beacon@main-qhb6,
#   role=main,
#   slice_labels=carto-fwd-source,
#   kind=ast,
# ]
def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Beacon-aware helpers for source files: extract snippets around "
            "a given beacon id (Python/Markdown/SQL)."
        )
    )

    parser.add_argument(
        "file",
        help="Path to the source file (Python/Markdown/SQL)",
    )
    parser.add_argument(
        "beacon_id",
        help="Beacon id to locate (e.g. model:forward@1)",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=10,
        help="Number of context lines before/after the beacon (default: 10)",
    )

    args = parser.parse_args(argv)

    try:
        extract_snippet(args.file, args.beacon_id, args.context)
    except Exception as e:  # noqa: BLE001
        print(f"[beacon_obtain_code_snippet] ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()


# @beacon-close[
#   id=bocsv1@cli-wrapper-boundary,
# ]
# @beacon[
#   id=auto-beacon@get_snippet_for_md_path-8iyg,
#   role=ƒ _md_beacon_has_brief_label(file_path: str, heading_line: int),
#   slice_labels=nexus-md-header-path,ra-snippet-range-ast-md,ra-read-text-snippet,carto-fwd-markdown,
#   kind=ast,
#   comment=Checks opt-in Markdown brief labels through U01 owned block/wrapper extents from the caller snapshot; preserves the six-line blank-gap rule without a wrapper-close scan or second read.,
# ]
def _md_beacon_has_brief_label(file_path: str, heading_line: int, *,
                               snapshot: SourceSnapshot | None = None) -> bool:
    """Check whether the on-disk MD beacon decorating a heading has a brief label.

    "Brief" labels are slice_labels that are exactly ``b`` or start with ``b-``.
    When present on the beacon directly above a heading, the snippet retrieval
    for that heading should stop at the next heading of ANY level (rather than
    the default "next heading of same-or-higher level" which includes nested
    subsections).

    This is a Markdown-only feature controlled by the user adding ``#b`` or
    ``#b-*`` as a Workflowy tag on the heading node; F12 then promotes those
    tags to slice_labels on the on-disk beacon block, where this function
    discovers them.

    Own block/wrapper ends come from U01 records. A supplied snapshot is never
    reopened; the six-line blank-gap compatibility rule is unchanged.
    """
    if heading_line <= 1:
        return False

    if nexus_map_codebase is None:
        return False

    snapshot = snapshot if snapshot is not None else read_source_snapshot(file_path)
    lines = _snippet_lines(snapshot, "markdown")
    candidates = []
    for record in scan_beacon_structure(snapshot, "markdown"):
        if record["marker"] != "open":
            continue
        _start, end = _snippet_read_extent(snapshot, record)
        if 0 < heading_line - end <= 6 and not any(line.strip() for line in lines[end:heading_line - 1]):
            candidates.append((end, record))
    if not candidates:
        return False
    record = max(candidates, key=lambda item: item[0])[1]
    labels = (record["fields"].get("slice_labels") or "").split(",")
    return any(label.strip() == "b" or label.strip().startswith("b-") for label in labels)


# @beacon[
#   id=auto-beacon@AmbiguousMdPathError-amb1,
#   role=AmbiguousMdPathError,
#   slice_labels=carto-fwd-markdown,
#   kind=ast,
# ]
class AmbiguousMdPathError(RuntimeError):
    """Raised when an MD_PATH chain matches >1 heading and no ordinal was given.

    Carries a structured ``candidates`` list (one dict per matching heading,
    in document order) so a caller / UI can present a disambiguation picker
    instead of silently guessing. Each candidate dict has:
        {"occurrence": 1-based int, "line": int, "hint": str}
    The string form remains human-readable for logs.
    """

    def __init__(self, message: str, candidates: List[dict]):
        super().__init__(message)
        self.candidates = candidates


# @beacon[
#   id=auto-beacon@_build_ambiguous_md_path_error-amb2,
#   role=_build_ambiguous_md_path_error,
#   slice_labels=carto-fwd-markdown,
#   kind=ast,
# ]
def _build_ambiguous_md_path_error(
    md_path_lines: List[str],
    file_path: str,
    candidates: List[tuple],
    lines: List[str],
    extra: str | None = None,
) -> "AmbiguousMdPathError":
    """Construct an AmbiguousMdPathError with per-candidate line + hint.

    ``candidates`` is the list of ``(heading_index, heading_dict)`` tuples from
    the resolver (already in document order). For each, derive a one-line hint
    from the file body under that heading: prefer a ``FILEBASE:`` line, else the
    first non-blank, non-metadata, non-fence content line; fall back to empty.
    """
    cand_info: List[dict] = []
    n = len(lines)
    for ordinal, (hidx, h) in enumerate(candidates, start=1):
        start = int(h.get("start_line") or 0)
        hint = ""
        if start > 0:
            # Scan a bounded window of body lines below the heading for a hint.
            for ln in range(start, min(start + 40, n)):
                if ln == start - 1:
                    continue
                raw = lines[ln] if 0 <= ln < n else ""
                s = raw.strip()
                if not s:
                    continue
                if s.startswith("FILEBASE:"):
                    hint = s
                    break
                # Skip heading lines, HTML comments, fences, and beacon markers.
                if s.startswith("#") or s.startswith("<!--") or s.startswith("-->"):
                    continue
                if s.startswith("```") or s.startswith("~~~"):
                    continue
                if s == "---" or s.startswith("MD_PATH") or s.startswith("BEACON ("):
                    continue
                # First real content line: use as the hint (trimmed).
                hint = s[:120]
                break
        cand_info.append({"occurrence": ordinal, "line": start, "hint": hint})

    summary = ", ".join(
        f"#{c['occurrence']}@line {c['line']}" + (f" ({c['hint']})" if c['hint'] else "")
        for c in cand_info
    )
    msg = (
        f"MD_PATH {md_path_lines!r} is ambiguous in Markdown file {file_path!r} "
        f"({len(cand_info)} matches): {summary}."
    )
    if extra:
        msg += f" [{extra}]"
    return AmbiguousMdPathError(msg, cand_info)


# @beacon[
#   id=auto-beacon@get_snippet_for_md_path-p8m9,
#   role=get_snippet_for_md_path,
#   slice_labels=carto-fwd-markdown,
#   kind=ast,
#   comment=Projects Markdown sections through one C0 snapshot/heading authority and typed-backed tuples; preserves duplicate-path occurrence/candidates and brief bounds while extraction failures retain file/id evidence.,
# ]
def get_snippet_for_md_path(
    file_path: str,
    md_path_lines: List[str],
    context: int,
    occurrence: int | None = None,
) -> Tuple[int, int, List[str], int, int, int, dict]:
    """Resolve snippet for a Markdown heading node identified by its MD_PATH.

    IMPORTANT: this function intentionally uses Cartographer's
    ``parse_markdown_structure(...)`` output rather than maintaining a separate
    markdown-it heading stack. F9/F10 snippet navigation must agree with F12+1
    and F12+3 about what counts as the Markdown outline. In May 2026 a separate
    snippet-only markdown-it parser failed on README-SCRATCH because blockquoted
    headings (e.g. ``> # ...``) reset the local stack even though Cartographer's
    parser correctly treats them as body content. Result: MD_PATH lookup failed
    and F9 fell through to a bad name-search guess. Keeping MD_PATH resolution
    anchored to Cartographer's parsed tree prevents parser drift.

    Outline, brief labels and displayed C0 line view share one binary snapshot.
    Failed path extraction has typed file/id evidence; ambiguity keeps its picker.

    DUPLICATE-SIBLING DISAMBIGUATION (Option C, Dan June 2026)
    ---------------------------------------------------------
    MD_PATH is a *semantic path of heading texts*, NOT a unique key: Markdown
    legally allows two sibling headings with the same text under the same
    parent (e.g. two ``## Dump Bay`` under one Curation Capsule), so the same
    MD_PATH chain can match N>1 headings in document order. When the caller
    knows WHICH duplicate it wants (because it started from a specific
    Workflowy UUID and computed that node's ordinal among same-name siblings,
    ordered by the cache ``priority`` field), it passes ``occurrence`` (1-based
    document-order index). We then select the ``occurrence``-th matching
    candidate instead of raising on ambiguity. This is the disk-side twin of
    the Workflowy-side sibling ordinal; Workflowy order == disk document order
    is maintained by F12 reconcile.

    When ``occurrence`` is None and the chain is ambiguous we still RAISE, but
    the error now carries the candidate list (line + one-line hint per match)
    so a caller/UI can present a picker. We never silently guess.
    """
    if nexus_map_codebase is None:
        raise RuntimeError("nexus_map_codebase is not available for Markdown MD_PATH resolution")

    import re

    # Heading parsing, beacon geometry and the C0-sanitized display view all
    # belong to this one binary observation; Markdown is never repaired on disk.
    snapshot = _read_snippet_snapshot(file_path, " | ".join(md_path_lines))
    lines = _snippet_lines(snapshot, "markdown")
    n = len(lines)

    desired: List[tuple[int, str]] = []
    for raw in md_path_lines:
        s = (raw or "").strip()
        if not s:
            continue
        m = re.match(r"^(#{1,64})\s*(.*)$", s)
        if not m:
            raise RuntimeError(f"Invalid MD_PATH component {raw!r} in note")
        level = len(m.group(1))
        text = (m.group(2) or "").strip()
        norm = nexus_map_codebase.whiten_text_for_header_compare(text).casefold()
        desired.append((level, norm))

    if not desired:
        raise RuntimeError("Empty MD_PATH for Markdown snippet resolution")

    target_depth = len(desired)
    target_level = desired[-1][0]

    # Parse with Cartographer's canonical Markdown parser (same as F12+1).
    outline_nodes = nexus_map_codebase.parse_markdown_structure(file_path, snapshot=snapshot)
    if not outline_nodes:
        raise _qualname_error(snapshot, " | ".join(md_path_lines), "markdown", outline_nodes,
                              f"No headings found while resolving MD_PATH for {file_path!r}", "not_found")

    headings = _markdown_heading_rows(outline_nodes)

    candidates: List[tuple[int, dict]] = []
    for i, h in enumerate(headings):
        path_chain = h.get("path") or []
        if len(path_chain) != target_depth:
            continue
        if all(
            path_chain[j][0] == desired[j][0] and path_chain[j][1] == desired[j][1]
            for j in range(target_depth)
        ):
            candidates.append((i, h))

    if not candidates:
        raise _qualname_error(snapshot, " | ".join(md_path_lines), "markdown", outline_nodes,
                              f"MD_PATH {md_path_lines!r} not found in Markdown file {file_path!r}", "not_found")
    if len(candidates) > 1:
        # Ambiguous: same MD_PATH chain matches multiple sibling headings in
        # document order. If the caller supplied a 1-based occurrence ordinal
        # (Option C duplicate-sibling disambiguation), pick that candidate.
        if isinstance(occurrence, int) and occurrence >= 1:
            if occurrence > len(candidates):
                raise _build_ambiguous_md_path_error(
                    md_path_lines, file_path, candidates, lines,
                    extra=(
                        f"requested occurrence {occurrence} exceeds "
                        f"{len(candidates)} matches (cache/disk out of sync; "
                        f"refresh nodes cache or F12+1 this file)"
                    ),
                )
            match_index, match = candidates[occurrence - 1]
        else:
            # No ordinal hint: do NOT guess. Raise a rich, picker-friendly error.
            raise _build_ambiguous_md_path_error(
                md_path_lines, file_path, candidates, lines
            )
    else:
        match_index, match = candidates[0]
    heading_line = int(match.get("start_line") or 1)

    brief_mode = _md_beacon_has_brief_label(file_path, heading_line, snapshot=snapshot)

    core_start = heading_line
    core_end = _markdown_heading_end(headings, match_index, n, brief_mode)

    if core_end < core_start:
        core_end = core_start

    start_line = max(1, core_start - context)
    end_line = min(n, core_end + context)

    metadata = {
        "resolution_strategy": "md_path_carto_brief" if brief_mode else "md_path_carto",
        "confidence": 1.0,
        "ambiguity": "none",
        "candidates": None,
        "brief_mode": brief_mode,
    }
    return BeaconSnippetResult(snapshot, lines, None, None, core_start, core_end, core_start,
                               context=context, resolution_strategy=metadata["resolution_strategy"],
                               evidence={"brief_mode": brief_mode}).legacy_tuple()


# @beacon-close[
#   id=bocsv1@public-snippet-api-core,
# ]
# @beacon-close[
#   id=bocsv1@file-core,
# ]
