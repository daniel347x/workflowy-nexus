"""Read-only client/front projections for the single disk snippet authority.

No cache reload, source observation, grammar, writer or suppression lookup lives
here. The client supplies already-resolved identity; bocs owns source evidence.
"""
from __future__ import annotations

from functools import wraps
import json

from beacon_mutation_contract import parse_beacon_note_metadata


def snippet_note_identity(note):
    records = parse_beacon_note_metadata(note)
    ids = {record.get("id") for record in records if record.get("id")}
    kinds = {record.get("kind") for record in records if record.get("kind")}
    return (next(iter(ids)) if len(ids) == 1 else None,
            next(iter(kinds)) if len(kinds) == 1 else None, bool(records))


def mark_snippet_error(error, *, file_path=None, beacon_id=None, cause=None, resolved_node_id=None):
    """Preserve existing exception types/candidates and add typed extraction data."""
    data = {**getattr(cause, "source_extraction", {}), **getattr(error, "source_extraction", {})}
    if file_path is not None:
        data["file_path"] = file_path
    if beacon_id is not None:
        data["beacon_id"] = beacon_id
    if resolved_node_id is not None:
        data["resolved_node_id"] = resolved_node_id
    data.setdefault("exception_type", type(cause or error).__name__)
    data.setdefault("outcome", "ambiguous" if getattr(cause or error, "candidates", None) else "failed")
    if getattr(cause or error, "candidates", None) is not None:
        data["candidates"] = getattr(cause or error, "candidates")
        error.candidates = data["candidates"]
    error.source_extraction_failed = True
    error.source_extraction = data
    return error


# @beacon[
#   id=beacon-snippet@resolved_snippet_errors,
#   slice_labels=ra-read-text-snippet,nexus--mcp-api,
#   kind=ast,
#   comment=Exception-only adapter for the permitted resolved-node extraction seam; preserves its error types and candidates while marking resolved identity so later content or infrastructure failure cannot become a UUID miss or suppression-list lookup.,
# ]
def resolved_snippet_errors(function):
    """The resolved-node method is the boundary, not a handle resolver wrapper."""
    @wraps(function)
    async def extract(self, beacon_node_id, beacon_node, nodes_by_id, context=10):
        try:
            return await function(self, beacon_node_id, beacon_node, nodes_by_id, context)
        except Exception as error:
            bid, _kind, _present = snippet_note_identity(beacon_node.get("note") or beacon_node.get("no") or "")
            raise mark_snippet_error(error, beacon_id=getattr(error, "source_extraction", {}).get("beacon_id", bid),
                                     resolved_node_id=str(beacon_node.get("id") or beacon_node_id))
    return extract


def snippet_target(note, nodes_by_id, node, occurrence):
    """Delegate cached twin evidence to U03; a cache line alone is never used."""
    import nexus_map_codebase as source
    import beacon_obtain_code_snippet as snippets
    return snippets.snippet_target_from_note(note, occurrence=occurrence,
                                             twin_witness=source._beacon_cache_twin_witness(nodes_by_id, node))


def snippet_metadata_projection(metadata):
    return {key: metadata[key] for key in ("location", "findings", "finding", "observed", "analysis_error",
                                           "finding_scope", "outline_errors", "anchor_degradation") if key in metadata}


def snippet_error_code(error):
    return "source_extraction_failed" if getattr(error, "source_extraction_failed", False) else "source_read_failed"


def snippet_error_extra(error, existing=None):
    return {**(existing or {}), **getattr(error, "source_extraction", {})}


# @beacon[
#   id=beacon-snippet@append_snippet_findings,
#   slice_labels=ra-read-text-snippet,nexus--mcp-api,
#   kind=ast,
#   comment=Appends only a notice compact warning/error rows and short degradation diagnostics to source text; full repair evidence stays one check or JSON read away and clean text is byte-identical.,
# ]
def append_snippet_findings(text, read):
    """Compact text only; degradation cannot masquerade as clean.

    Info codes live in the notice, warning/error rows keep actionable identities,
    and check/format=json retain the full findings and fix-resolution payload.
    """
    findings = read.get("findings") or []
    if (not findings and not any(read.get(key) for key in
                                ("analysis_error", "outline_errors", "anchor_degradation"))
            and read.get("resolution_strategy") != "degraded"):
        return text
    codes = sorted({row["code"] for row in findings})
    notice = (f"⚠ Snippet {read.get('resolution_strategy') or 'read'}: {len(findings)} finding(s)"
              + (" (" + ", ".join(codes) + ")" if codes else "")
              + ("; analysis_error" if read.get("analysis_error") else "") + ". Source was not changed.")
    lines = [notice]
    for row in findings:
        if row.get("severity") == "info":
            continue
        parts = [row.get("severity", "warning"), row["code"], f"line {row.get('line', '?')}"]
        if row.get("beacon_id"):
            parts.append(row["beacon_id"])
        parts.extend((row["message"], f"finding_id {row['finding_id']}"))
        lines.append("- " + " · ".join(" ".join(str(part).split()) for part in parts))
    for key in ("analysis_error", "outline_errors", "anchor_degradation"):
        values = read.get(key)
        if not values:
            continue
        for value in values if isinstance(values, (list, tuple)) else [values]:
            if isinstance(value, dict):
                value = " · ".join(str(value[field]) for field in ("code", "exception", "message")
                                   if value.get(field)) or str(value)
            lines.append(f"- {key} · " + " ".join(str(value).split()))
    locator = (read.get("finding_scope") or {}).get("complete_file") or {}
    params = locator.get("params")
    if not params and read.get("file_path"):
        params = {"file_path": read["file_path"], "min_severity": "info"}
    pointer = "Full findings and fix resolutions: "
    if params:
        pointer += "beacon(verb='check', params=" + json.dumps(params, ensure_ascii=False) + ") or "
    lines.append(pointer + "format=json.")
    return text + "\n\n" + "\n".join(lines)


# @beacon[
#   id=beacon-snippet@format_beacon_snippet_result,
#   slice_labels=ra-read-text-snippet,nexus--mcp-api,
#   kind=ast,
#   comment=Projects the shared snippet renderer into compact text notices warnings and degradation diagnostics or unchanged full JSON evidence; clean text and source window guards are unchanged and check retains complete repair detail.,
# ]
def format_beacon_snippet_result(read, view, *, renderer, output_format="text"):
    """Presentation only; shared snippet_view owns all line/window/size guards."""
    text = renderer(read, view)
    if output_format == "json":
        location = read.get("location") or {key: read.get(key) for key in
                    ("file_path", "beacon_id", "beacon_line", "core_start_line", "core_end_line")}
        return {"text": text, "location": location, "resolution_strategy": read.get("resolution_strategy"),
                "findings": read.get("findings") or [], **{key: read[key] for key in
                ("finding", "observed", "analysis_error", "finding_scope", "outline_errors", "anchor_degradation") if key in read}}
    if output_format != "text":
        raise ValueError("format must be text or json")
    return append_snippet_findings(text, read)
