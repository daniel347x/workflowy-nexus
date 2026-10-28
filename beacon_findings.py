"""Portable, read-only beacon findings over an already observed source version.

There is no parser, source reader or repair executor here. Callers supply their
outline and diagnostics; one projection consumes shared scanner/attachment and
pure hygiene authorities for both the daemon adapter and standalone kit.
Byte/line ranges locate evidence; only owned bytes and semantic anchors identify
it. This module and its dependency use the standard library only.
"""
from __future__ import annotations

import copy
import bisect
import hashlib
import json
import ntpath
import re

from beacon_mutation_contract import validate_beacon_comment_text


SEVERITY_ORDER = ("error", "warning", "info")


def _code(severity, view, authority):
    return {"severity": severity, "view": view, "authority": authority}


# The original 22 names, severities, views and authority strings are stable.
# New revisions add codes; they never rename/remove existing codes.
BEACON_CHECK_CODES = {
    "file_unreadable": _code("error", "file", "_parse_disk_beacons_for_language / map_codebase (strict UTF-8 read)"),
    "file_missing": _code("warning", "file", "resolve_cartographer_path_from_node"),
    "mapper_parse_error": _code("error", "mapper", "_is_cartographer_parse_error_sentinel over map_codebase"),
    "unterminated_block": _code("error", "parser", "parse_*_beacon_blocks"),
    "unknown_kind": _code("error", "parser", "parse_*_beacon_blocks"),
    "missing_id": _code("error", "parser", "parse_*_beacon_blocks"),
    "unknown_field_key": _code("warning", "parser", "parse_beacon_field_lines / beacon_field_unknown_keys"),
    "legacy_field": _code("info", "parser", "parse_beacon_field_lines"),
    "duplicate_id": _code("error", "mapper", "_duplicate_beacon_ids"),
    "block_not_mapped": _code("error", "mapper", "map_codebase + parse_cartographer_note_for_preview"),
    "two_blocks_one_symbol": _code("error", "mapper", "map_codebase + parse_cartographer_note_for_preview"),
    "kind_changed_by_mapper": _code("warning", "mapper", "map_codebase"),
    "no_slice_labels": _code("warning", "mapper", "map_codebase (_canonicalize_slice_labels)"),
    "slice_labels_derived": _code("info", "mapper", "map_codebase vs parse_*_beacon_blocks"),
    "span_without_close": _code("info", "parser", "parse_*_beacon_blocks"),
    "orphan_close": _code("info", "parser", "parse_*_beacon_blocks"),
    "block_end_mismatch": _code("error", "writer", "_beacon_block_extent_for_language vs parse_*_beacon_blocks"),
    "writer_anchor_mismatch": _code("error", "writer", "beacon_attachment_index via _find_anchor_matched_beacons vs map_codebase"),
    "writer_anchor_ambiguous": _code("error", "writer", "beacon_attachment_index via _find_anchor_matched_beacons"),
    "writer_target_ambiguous": _code("error", "writer", "_resolve_anchor_target_line"),
    "comment_rejected": _code("info", "comment", "validate_beacon_comment_text"),
    "empty_comment": _code("info", "comment", "beacon grammar (comment recommended)"),
    "check_failed": _code("error", "file", "check_beacon_file"),
    "noncanonical_terminator": _code("info", "parser", "scan_beacon_structure bracket_range"),
    "missing_wrapper": _code("error", "parser", "scan_beacon_structure wrapper ownership"),
    "mixed_comment_ownership": _code("warning", "parser", "scan_beacon_structure write_ranges"),
    "duplicate_field": _code("warning", "parser", "parse_beacon_field_lines occurrences"),
    "inline_field_ambiguity": _code("warning", "parser", "parse_beacon_field_lines diagnostics"),
    "pair_ambiguous": _code("warning", "parser", "scan_beacon_structure exact pairing"),
    "lexical_uncertainty": _code("warning", "parser", "scan_beacon_structure lexical evidence"),
    "writer_anchor_foreign": _code("error", "writer", "beacon_attachment_index vs mapper projection"),
    "implicit_kind_promoted": _code("info", "mapper", "beacon_attachment_index effective_kind"),
    "id_unlike_symbol": _code("info", "attachment", "beacon_id_resemblance"),
    "vue_capability": _code("info", "writer", "beacon_attachment_index section capability"),
}


# Catalogue categories are data, not executable repairs. Explicit caller choices
# are still required for auto recommendations on a read. U06 owns execution.
BEACON_REPAIR_CATALOGUE = {
    "normalization": {"default": "auto", "resolutions": ["normalize", "keep"]},
    "missing_bracket": {"default": "choice", "resolutions": ["insert_bracket", "replace_metadata", "keep"]},
    "wrapper": {"default": "choice", "resolutions": ["insert_wrapper", "edit_metadata", "delete_metadata", "keep"]},
    "fields": {"default": "choice", "resolutions": ["select_alternative", "set_field", "preserve_opaque", "rename_field", "delete_field", "keep"]},
    "duplicate_id": {"default": "choice", "resolutions": ["assign_ids", "keep"]},
    "stack": {"default": "choice", "resolutions": ["merge", "convert_to_span", "delete_metadata", "keep"]},
    "attachment": {"default": "choice", "resolutions": ["relocate", "create_block", "convert_to_span", "delete_metadata", "keep"]},
    "resemblance": {"default": "choice", "resolutions": ["rename_id", "relocate", "edit_descriptive_field", "keep"]},
    "pair": {"default": "choice", "resolutions": ["pair_close", "retag_close", "add_close", "delete_metadata", "keep"]},
    "module": {"default": "choice", "resolutions": ["convert_to_span", "relocate", "delete_metadata", "keep"]},
    "comment": {"default": "choice", "resolutions": ["replace_comment", "append_comment", "keep"]},
    "lexical": {"default": "choice", "resolutions": ["edit_metadata", "keep"]},
    "capability": {"default": "choice", "resolutions": ["relocate", "edit_metadata", "keep"]},
    "failure": {"default": "manual", "resolutions": ["retry", "keep"]},
}

_CATEGORY = {
    "file_unreadable": "failure", "file_missing": "failure", "check_failed": "failure",
    "mapper_parse_error": "failure", "unterminated_block": "missing_bracket",
    "missing_wrapper": "wrapper", "mixed_comment_ownership": "wrapper",
    "unknown_kind": "fields", "missing_id": "fields", "unknown_field_key": "fields",
    "duplicate_field": "fields", "inline_field_ambiguity": "fields", "legacy_field": "fields",
    "noncanonical_terminator": "normalization", "duplicate_id": "duplicate_id",
    "two_blocks_one_symbol": "stack", "writer_anchor_ambiguous": "stack",
    "block_not_mapped": "module", "kind_changed_by_mapper": "module",
    "writer_anchor_mismatch": "attachment", "writer_anchor_foreign": "attachment",
    "writer_target_ambiguous": "attachment", "block_end_mismatch": "wrapper",
    "no_slice_labels": "fields", "slice_labels_derived": "fields",
    "span_without_close": "pair", "orphan_close": "pair", "pair_ambiguous": "pair",
    "comment_rejected": "comment", "empty_comment": "comment",
    "lexical_uncertainty": "lexical", "implicit_kind_promoted": "module",
    "id_unlike_symbol": "resemblance", "vue_capability": "capability",
}


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_beacon_file(path):
    """Normalize an already real-resolved identity, without filesystem IO.

    Mounted Windows paths and drive spellings share one case-insensitive key.
    Other POSIX paths retain case. The observation adapter resolves symlinks.
    """
    path = str(path)
    mounted = re.match(r"^/mnt/([a-zA-Z])(?:/|$)(.*)", path)
    if mounted:
        path = mounted[1] + ":/" + mounted[2]
    if re.match(r"^[a-zA-Z]:[/\\]", path) or path.startswith("\\\\"):
        return ntpath.normcase(ntpath.normpath(path)).replace("\\", "/")
    return path


def _semantic_anchor(anchor):
    if not anchor:
        return None
    # Line/byte/decorator positions are witnesses, never semantic identity.
    return {key: anchor.get(key) for key in ("ast_qualname", "md_path", "header")}


def beacon_member_key(member):
    """Use U03/U04's raw/owned hashes and semantic anchor, never locations."""
    return {"block_hash": member.get("block_hash"),
            "owned_hashes": [r["sha256"] for r in member.get("owned_range_hashes") or []],
            "anchor": _semantic_anchor(member.get("anchor")),
            "section_type": (member.get("section") or {}).get("type")}


# @beacon[
#   id=beacon-findings@beacon_finding_id,
#   slice_labels=beacon-writeback,carto-fwd-source,
#   kind=ast,
#   comment=Pure versioned finding identity from canonical real file code kind owned byte hashes and semantic anchors; excludes producer display root lines and whole-file drift for block findings and retains explicit ordinals only for indistinguishable occurrences.,
# ]
def beacon_finding_id(canonical_file, code, kind, members=(), *, file_sha256=None,
                      unreadable_identity=None, anchor=None, ordinal=None):
    """Return bf1 plus a full SHA-256; a hash never substitutes for a witness."""
    keys = sorted((beacon_member_key(m) for m in members), key=_canonical_json)
    payload = {"version": 1, "canonical_file": canonical_beacon_file(canonical_file),
               "code": code, "kind": kind, "members": keys,
               "anchor": _semantic_anchor(anchor)}
    if not keys and not anchor:
        payload["file_identity"] = file_sha256 or {"unreadable": unreadable_identity or "unobserved"}
    if ordinal is not None:
        payload["occurrence_ordinal"] = ordinal
    return "bf1:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _params(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}


def _resolution(name, summary, effect, properties=None, required=(), **extra):
    return {"resolution": name, "summary": summary, "effect": effect,
            "params_schema": _params(properties, required), **extra}


# @beacon[
#   id=beacon-findings@beacon_finding_fix,
#   slice_labels=beacon-writeback,carto-fwd-source,
#   kind=ast,
#   comment=Projects non-executable repair schemas from physical ownership and shared verdicts; new closes require physical line starts within their section or EOF; wrapper/pair/open-only/id choices never imply a hidden rename or keeper.,
# ]
def beacon_finding_fix(code, members, detail):
    """Project owned operands and explicit pairing choices, never plan an edit.

    Pair indices name this finding's members. A rename/assignment's optional
    close is its current physical opener byte (the whole finding pins the file);
    execution must validate that choice against the scanner, not its spelling.
    A supplied add_close boundary must be a physical line start in its span
    section after the opener, or EOF; this schema describes, not enforces, it.
    """
    category = _CATEGORY[code]
    entry = BEACON_REPAIR_CATALOGUE[category]
    klass = entry["default"]
    keep = _resolution("keep", "Keep the observed state.", "No write and no persistent waiver.")
    ranges = []
    for member in members:
        for bounds in (member.get("write_ranges") or []) + (member.get("field_ranges") or []):
            if list(bounds) not in ranges:
                ranges.append(list(bounds))
    member_param = {"type": "integer", "enum": list(range(len(members)))}
    text = {"type": "string"}
    field = {"type": "string", "enum": ["id", "kind", "slice_labels", "comment", "comments", "role", "start_snippet", "end_snippet", "show_span"]}
    named_field = {"unknown_kind": "kind", "missing_id": "id", "no_slice_labels": "slice_labels",
                   "slice_labels_derived": "slice_labels"}.get(code)
    if code == "duplicate_field":
        named_field = detail.get("key")
    if named_field:
        field = {"type": "string", "enum": [named_field]}
    selected = {"member": member_param}
    pair_choice = {"close": {"type": "integer", "minimum": 0},
                   "open_only": {"type": "boolean"}}
    bounded = {**selected, "range": {"enum": ranges}, "text": text}
    target = {"type": "object", "properties": {"ast_qualname": text,
              "md_path": {"type": "array", "items": text}, "header": text},
              "anyOf": [{"required": ["ast_qualname"]}, {"required": ["md_path"]}],
              "additionalProperties": False}
    options = {
        "normalize": _resolution("normalize", "Normalize only the selected metadata syntax.", "Preserve all code, prose and unrequested fields.", selected, ["member"]),
        "insert_bracket": _resolution("insert_bracket", "Insert the missing bracket at a recorded candidate.", "Insert a delimiter; never consume following source.", {**selected, "boundary": {"enum": detail.get("boundaries", [])}}, ["member", "boundary"]),
        "replace_metadata": _resolution("replace_metadata", "Replace explicitly bounded metadata.", "Replace only the chosen metadata bytes.", bounded, ["member", "range", "text"]),
        "insert_wrapper": _resolution("insert_wrapper", "Close the wrapper at the shown metadata boundary.", "Retain ordinary comment text and reveal the recorded headings.", {**selected, "boundary": {"enum": detail.get("boundaries", [])}}, ["member", "boundary"]),
        "edit_metadata": _resolution("edit_metadata", "Rewrite the selected bounded metadata text.", "No source outside the supplied metadata range changes.", bounded, ["member", "range", "text"]),
        "delete_metadata": _resolution("delete_metadata", "Delete only selected metadata, not its body.", "Retain ordinary comments, code and prose.", {**selected, "range": {"enum": ranges}}, ["member", "range"]),
        "select_alternative": _resolution("select_alternative", "Choose a recorded parse alternative.", "Make the selected field interpretation explicit.", {**selected, "alternative": {"enum": detail.get("alternatives", [])}}, ["member", "alternative"]),
        "set_field": _resolution("set_field", "Supply an explicit field value.", "Splice that field alone; preserve opaque text and other occurrences.", {**selected, "field": field, "value": text}, ["member", "field", "value"]),
        "preserve_opaque": _resolution("preserve_opaque", "Keep the selected opaque line literal.", "Preserve its bytes rather than guessing a field value.", {**selected, "range": {"enum": ranges}}, ["member", "range"]),
        "rename_field": _resolution("rename_field", "Rename a selected field key.", "Change the selected key only.", {**selected, "range": {"enum": ranges}, "field": field}, ["member", "range", "field"]),
        "delete_field": _resolution("delete_field", "Delete a selected field occurrence.", "Delete only its recorded metadata range.", {**selected, "range": {"enum": ranges}}, ["member", "range"]),
        "assign_ids": _resolution("assign_ids", "Choose a keeper and new ids for the other members.", "Rename selected metadata and chosen or uniquely paired closes; empty new_id allocates with advisory identity consequences; open_only is explicit, never an automatic keeper.", {"keeper": member_param, "new_ids": {"type": "array", "items": _params({"member": member_param, "new_id": text, **pair_choice}, ["member", "new_id"])}}, ["keeper", "new_ids"]),
        "merge": _resolution("merge", "Choose a survivor and field values; union labels.", "Merge only selected metadata; retain the target source.", {"survivor": member_param, "fields": {"type": "object", "additionalProperties": text}}, ["survivor", "fields"]),
        "convert_to_span": _resolution("convert_to_span", "Make the selected block explicitly a span.", "Change metadata kind only; no FILE-node capability expansion.", selected, ["member"]),
        "relocate": _resolution("relocate", "Relocate metadata to a chosen current target.", "Move metadata only; inspect positional, gap and name evidence first.", {**selected, "target": target}, ["member", "target"]),
        "create_block": _resolution("create_block", "Create a separate block on a current target.", "Retain the observed orphan; insert new metadata only.", {"target": target, "id": text}, ["target", "id"]),
        "rename_id": _resolution("rename_id", "Choose a new id (spelling is advisory).", "Rename metadata and its unique or selected close, or explicitly leave an ambiguous span open; empty new_id allocates and reports identity consequences.", {**selected, "new_id": text, **pair_choice}, ["member", "new_id"]),
        "edit_descriptive_field": _resolution("edit_descriptive_field", "Describe the intended concept instead of renaming it.", "Change the chosen comment/role field only.", {**selected, "field": {"enum": ["comment", "role"]}, "value": text}, ["member", "field", "value"]),
        "pair_close": _resolution("pair_close", "Select a structural pair or an explicit open-only outcome.", "Persist a chosen ambiguous pair with an explicit new_id (empty allocates); no hidden rename, ordinal or edit-distance pairing.", {"opener": member_param, "close": member_param, "open_only": {"type": "boolean"}, "new_id": text}, ["opener"]),
        "retag_close": _resolution("retag_close", "Supply the intended exact close id.", "Change the selected close metadata only.", {**selected, "new_id": text}, ["member", "new_id"]),
        "add_close": _resolution("add_close", "Add an exact close at a supplied valid boundary.", "Insert close metadata without deleting span content.", {**selected, "boundary": {"type": "integer", "minimum": 0, "description": "boundary must be a physical line start within the span section, or EOF, after the opener."}}, ["member", "boundary"]),
        "replace_comment": _resolution("replace_comment", "Replace the comment explicitly.", "Apply comment_mode=replace; independent label work remains valid.", {**selected, "comment": text}, ["member", "comment"], via="tags.ensure where addressable; otherwise shared fix intent"),
        "append_comment": _resolution("append_comment", "Append to the existing comment explicitly.", "Apply comment_mode=append; independent label work remains valid.", {**selected, "comment": text}, ["member", "comment"], via="tags.ensure where addressable; otherwise shared fix intent"),
        "retry": _resolution("retry", "Recover the source/reader and re-list current findings.", "Read-only retry; no structural edit is authorized without parseable evidence."),
    }
    names = list(entry["resolutions"])
    if category == "pair":
        openers = [i for i, m in enumerate(members) if m.get("kind") != "close"]
        closes = [i for i, m in enumerate(members) if m.get("kind") == "close"]
        schema = options["pair_close"]["params_schema"]
        schema["properties"].update(opener={"enum": openers}, close={"enum": closes})
        schema["anyOf"] = [{"required": ["close"]},
                           {"properties": {"open_only": {"const": True}}, "required": ["open_only"]}]
        options["retag_close"]["params_schema"]["properties"]["member"] = {"enum": closes}
        options["add_close"]["params_schema"]["properties"]["member"] = {"enum": openers}
    verdict = detail.get("planner") or {}
    if code == "legacy_field" and verdict.get("status") == "planned":
        klass, names = "auto", ["normalize", "keep"]
    if code == "duplicate_field" and detail.get("identical") and all(m.get("complete") for m in members):
        klass, names = "auto", ["normalize", "keep"]
    if code == "unterminated_block" and members and all(m.get("repair_unambiguous") for m in members):
        klass = "auto"
    if category == "stack" and verdict.get("status") == "planned":
        klass = "auto"
        options["merge"]["params"] = {"survivor": len(members) - 1,
                                      "fields": {key: value for key, value in
                                          {"slice_labels": verdict.get("labels"), "comment": verdict.get("comment")}.items()
                                          if value is not None}}
    # No structural mutation option when neither owned metadata nor field
    # geometry exists. This is absence of evidence, not a file-wide refusal.
    safe = bool(ranges or any(m.get("repair_candidates") for m in members))
    if not safe and category != "failure":
        klass, names = "manual", ["keep"]
    resolutions = []
    for name in names:
        if name == "keep":
            resolutions.append(keep)
            continue
        option = options[name]
        props = option["params_schema"]["properties"]
        field_only = name in {"rename_field", "delete_field"}
        if field_only:
            props["range"] = {"enum": [list(r) for member in members for r in member.get("field_ranges") or []]}
        if any("enum" in value and not value["enum"] for key, value in props.items()
               if key != "close" or name != "pair_close"):
            continue  # no fictitious candidate/range to select
        if "member" in props and "range" in props:
            option["params_schema"]["allOf"] = [
                {"if": {"properties": {"member": {"const": index}}, "required": ["member"]},
                 "then": {"properties": {"range": {"enum": [list(r) for r in
                     ([] if field_only else member.get("write_ranges") or []) + (member.get("field_ranges") or [])]}}}}
                for index, member in enumerate(members)]
        resolutions.append(option)
    if all(r["resolution"] == "keep" for r in resolutions):
        klass = "manual"
    result = {"class": klass, "resolutions": resolutions}
    if klass == "auto":
        result["recommended"] = next(r["resolution"] for r in resolutions if r["resolution"] != "keep")
    if klass == "manual":
        result["instructions"] = ("Inspect the file and the reported exception; restore/read UTF-8 source and retry. "
                                  "No parseable metadata extent is available for structural repair."
                                  if category == "failure" else
                                  "Supply current parseable metadata field/extent evidence, or edit the intended metadata in source manually; "
                                  "the recorded evidence does not establish a safe metadata range.")
    return result


# @beacon[
#   id=beacon-findings@make_beacon_finding,
#   slice_labels=beacon-writeback,carto-fwd-source,
#   kind=ast,
#   comment=Constructs the common serializable row with U03/U04 physical members observed hash and catalogue actions; display and producer never enter its content-derived identity and no resolution executes.,
# ]
def make_beacon_finding(file, line, code, beacon_id, message, detail=None, *,
                        snapshot=None, canonical_file=None, file_sha256=None,
                        members=(), anchor=None, producer="beacon.check", kind=None,
                        unreadable_identity=None):
    """Keep old row keys and add the complete current-observation wire witness."""
    members = copy.deepcopy(list(members))
    detail = copy.deepcopy(detail or {})
    canonical = canonical_beacon_file(snapshot.canonical_path if snapshot is not None else canonical_file or file)
    sha = snapshot.sha256 if snapshot is not None else file_sha256
    kind = kind or code
    observed = {"canonical_file": canonical, "file_sha256": sha,
                "block_ranges": [m.get("read_range") for m in members],
                "field_ranges": [r for m in members for r in m.get("field_ranges") or []],
                "member_fingerprints": [beacon_member_key(m) for m in members],
                "identity_ambiguous": False}
    equal_members = {}
    for index, member in enumerate(members):
        equal_members.setdefault(_canonical_json(beacon_member_key(member)), []).append(index)
    ambiguous_members = [indices for indices in equal_members.values() if len(indices) > 1]
    if ambiguous_members:
        observed.update(identity_ambiguous=True, identical_member_groups=ambiguous_members)
    if anchor:
        observed["anchor"] = copy.deepcopy(anchor)
    if unreadable_identity is not None:
        observed["unreadable_identity"] = copy.deepcopy(unreadable_identity)
    return {"file": file, "line": int(line or 0), "code": code, **BEACON_CHECK_CODES[code],
            "beacon_id": beacon_id if beacon_id not in ("", None) else None,
            "message": message, "detail": detail, "kind": kind, "producer": producer,
            "observed": observed, "members": members, "fix": beacon_finding_fix(code, members, detail),
            "finding_id": beacon_finding_id(canonical, code, kind, members, file_sha256=sha,
                                            unreadable_identity=unreadable_identity, anchor=anchor)}


def finalize_beacon_findings(rows):
    """Disambiguate identical occurrences, explicitly not proof across drift."""
    groups = {}
    for row in rows:
        groups.setdefault(row["finding_id"], []).append(row)
    for group in groups.values():
        if len(group) < 2:
            continue
        for ordinal, row in enumerate(group):
            witness = row["observed"]
            witness.update(identity_ambiguous=True, occurrence_ordinal=ordinal,
                           identical_occurrences=len(group))
            row["finding_id"] = beacon_finding_id(
                witness["canonical_file"], row["code"], row["kind"], row["members"],
                file_sha256=witness["file_sha256"], unreadable_identity=witness.get("unreadable_identity"),
                anchor=witness.get("anchor"), ordinal=ordinal)
    return rows


def _bid(value):
    return str(value or "").strip()


def _mapped_blocks(blocks, mapped_nodes, language):
    """Match physical AST anchors first; never let an equal id hide a copy.

    Span projections without positions (some section renderers) are matched by
    their field evidence. Equal indistinguishable spans have equal map facts;
    their members still retain physical witnesses and explicit ambiguity.
    """
    available = [(node, rec) for node in mapped_nodes for rec in node["records"]]
    result = {}
    for block in blocks:
        beacon = block.get("beacon")
        if not beacon:
            continue
        anchor, kind = block.get("anchor"), block["effective_kind"]
        candidates = [(i, node, rec) for i, (node, rec) in enumerate(available)
                      if _bid(rec.get("id")) == _bid(beacon.get("id")) and
                      ((anchor and node["line"] == anchor["outline_line"] and rec.get("kind") == "ast") or
                       (not anchor and rec.get("kind") == "span" and
                        (kind == "span" or language == "markdown") and
                        (not node.get("section_type") or node["section_type"] == block["record"]["section"]["type"]))) ]
        if not candidates:
            continue
        exact = [c for c in candidates if str(c[2].get("comment") or "").strip() == str(block.get("mapper_comment", beacon.get("comment")) or "").strip()
                 and str(c[2].get("slice_labels") or "") == str(block.get("mapper_labels", beacon.get("slice_labels")) or "")]
        index, node, rec = (exact or candidates)[0]
        result[block["owner"]] = {"node": node, "record": rec}
        available.pop(index)
    return result


def _finding_outline_nodes(roots):
    for node in roots:
        yield node
        yield from _finding_outline_nodes(node.get("children") or [])


def _finding_node_line(node):
    for key in ("orig_lineno_start_unused", "_md_heading_line"):
        value = node.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 0


# @beacon[
#   id=beacon-findings@beacon_snapshot_analysis_inputs,
#   slice_labels=carto-fwd-source,beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Projects one supplied snapshot and caller-owned outline into shared scanner attachments physical witnesses duplicate and resemblance verdicts and pure planner records for both check adapters; never reads source or invents grouping policy.,
# ]
def beacon_snapshot_analysis_inputs(snapshot, language, source, *, outline, parse_note,
                                   mapped_roots=None, mapper_errors=()):
    """Project shared authorities once for either caller's supplied map view.

    The caller owns outline building and its diagnostics. ``source`` supplies
    the existing pure scanner, attachment, duplicate and planner functions;
    ``parse_note`` supplies that caller's generated-note reader. Neither map
    construction nor source observation happens in this portable projection.
    """
    if language == "vue":
        try:
            sections = [s for s, _r in source._vue_section_records(snapshot)]
        except Exception as error:
            return {"section_error": f"{type(error).__name__}: {error}"}
    else:
        sections = None
    _lines, beacons = source._parse_snapshot_beacons_for_language(snapshot, language)
    records = source._beacon_edit_records(snapshot, language)
    structures = {"language": language, "records": records, "beacons": beacons, "sections": sections}
    index = source.beacon_attachment_index(snapshot, outline, structures)
    for block in index["blocks"]:
        beacon = block.get("beacon")
        if beacon:
            bid = (beacon.get("id") or "").strip()
            role = (beacon.get("role") or "").strip() or (bid.split("@", 1)[0] if "@" in bid else "")
            block["mapper_labels"] = source._canonicalize_slice_labels(beacon.get("slice_labels") or "", role)
            block["mapper_comment"] = source.strip_leading_decoration_symbols(beacon.get("comment"))
    for anchor in index["anchors"]:
        anchor["descriptor"] = source._beacon_anchor_descriptor(anchor)
    members = {b["owner"]: source._hygiene_member(snapshot, b) for b in index["blocks"]}
    for record in records:
        owner = record["opener_range"][0]
        if owner not in members:
            members[owner] = source._hygiene_member(snapshot, {
                "record": record, "owner": owner, "effective_kind": "close",
                "anchor": None, "gap": {},
            })
    mapped_nodes = []
    for node in _finding_outline_nodes(outline if mapped_roots is None else mapped_roots):
        recs = [payload for kind, payload in parse_note(str(node.get("note") or "")) if kind == "beacon"]
        if recs:
            note = str(node.get("note") or "")
            section = ("template_element" if "BEACON (VUE TEMPLATE" in note else
                       "style_element" if "BEACON (VUE STYLE" in note else "script_element") if language == "vue" else None
            mapped_nodes.append({"line": _finding_node_line(node), "symbol": node.get("ast_qualname"),
                                 "records": recs, "section_type": section})
    resemblance = [e for block in index["blocks"] for e in source.beacon_id_resemblance(
        snapshot, block, block["anchor"], index["anchors"], index["blocks"])]
    duplicate_ids = source._duplicate_beacon_ids([
        {"id": r["fields"].get("id")} for r in records if r["marker"] == "open"])
    taken = {r["fields"].get("id") for r in records if r["fields"].get("id")}
    id_suggestions = {}
    for bid in duplicate_ids:
        suggestions = []
        for block in index["blocks"]:
            if (block["record"]["fields"].get("id") or "").strip() == bid:
                new_id = source.allocate_beacon_id(taken, base_id=bid)
                taken.add(new_id)
                suggestions.append({"opener_start_byte": block["owner"], "new_id": new_id})
        id_suggestions[bid] = suggestions
    return {"structures": structures, "outline": outline, "attachment_index": index, "members": members,
            "duplicate_ids": duplicate_ids, "id_suggestions": id_suggestions,
            "mapped_nodes": mapped_nodes, "mapper_errors": list(mapper_errors),
            "hygiene": source.plan_beacon_hygiene(snapshot, structures, index),
            "aliases": source.plan_beacon_comment_aliases(snapshot, language), "resemblance": resemblance}


# @beacon[
#   id=beacon-findings@analyze_beacon_snapshot,
#   slice_labels=beacon-writeback,carto-fwd-source,agent-kit-vendor,
#   kind=ast,
#   comment=Shared daemon and kit findings from one scanner attachment and planner view; wrapper-close candidates follow their existing bracket and duplicate groups retain actual close choices without changing identity or pairing policy.,
# ]
def analyze_beacon_snapshot(snapshot, structures, outline, attachment_index, *,
                            members, duplicate_ids, mapped_nodes=(), mapper_errors=(),
                            hygiene=None, aliases=None, resemblance=(),
                            id_suggestions=None,
                            display_path=None, producer="beacon.check", writer_authority=None):
    """Analyze either caller's immutable evidence; no IO, parser, writer or lease.

    ``members`` is keyed by opener byte and uses the U03/U04 witness vocabulary.
    ``duplicate_ids`` is THE shared duplicate rule's verdict over all physical
    open records. Planner group/alias verdicts determine automatic eligibility;
    this core does not invent a second merge/alias policy. ``outline`` is kept
    explicit for callers; every actual target comes from the supplied index.
    Wrapper-close offers never precede the existing structural bracket. Duplicate
    id rows expose scanner close operands for explicit pair/open-only assignments.
    """
    rows = []
    file = display_path or snapshot.canonical_path
    records, blocks = structures["records"], attachment_index["blocks"]
    mapped = _mapped_blocks(blocks, mapped_nodes, structures["language"])
    block_by_line = {b["record"]["line"]: b for b in blocks}
    groups = list((hygiene or {}).get("applied_groups", [])) + list((hygiene or {}).get("skipped_groups", []))
    alias_rows = list((aliases or {}).get("applied_aliases", [])) + list((aliases or {}).get("skipped_aliases", []))

    def emit(code, record=None, detail=None, message=None, operands=None, anchor=None, line=None, bid=None, kind=None):
        detail = detail or {}
        if operands is None:
            operands = [members[record["opener_range"][0]]] if record is not None else []
        row = make_beacon_finding(file, record["line"] if record and line is None else line or 0,
                                  code, record["fields"].get("id") if record and bid is None else bid,
                                  message or code.replace("_", " ").capitalize() + ".", detail,
                                  snapshot=snapshot, members=operands, anchor=anchor, producer=producer, kind=kind)
        if writer_authority and row["view"] == "writer":
            row["authority"] = writer_authority + ": " + row["authority"]
        rows.append(row)

    for error in mapper_errors:
        emit("mapper_parse_error", detail=error,
             message="The outline builder failed for this " + ("section" if error.get("section") else "file") +
                     "; inspect the recorded exception and retry; independent files/sections remain checkable.")

    for record in records:
        fields, ds = record["fields"], record["diagnostics"]
        owner, bid = record["opener_range"][0], _bid(fields.get("id"))
        boundaries = sorted(set(record.get("repair_candidates", {}).values()))
        structural = {"boundaries": boundaries, "terminated_by": record["terminated_by"],
                      "offending_line": record.get("offending_line")}
        if not record["bracket_range"]:
            emit("unterminated_block", record, structural,
                 "This record has no closing bracket; its own bounded fields remain visible and later independent blocks are still analyzed.")
        if record["wrapper_open_range"] and not record["wrapper_range"]:
            a = record["read_end_line"]
            close = record.get("wrapper_close_range")
            b = (bisect.bisect_right(snapshot.byte_offsets, close[1]) if close else
                 record["section"]["end_line"])
            retained = list(snapshot.physical_lines[a:b])
            wrapper_boundaries = [boundary for boundary in boundaries
                                  if record["bracket_range"] and boundary >= record["bracket_range"][1]]
            emit("missing_wrapper", record, {**structural, "boundaries": wrapper_boundaries, "retained_text": retained,
                 "hidden_or_revealed_headings": [target["header"] for target in attachment_index["anchors"]
                                                if a < target["line"] <= b],
                 "diagnostics": ds},
                 "The wrapper has no owned close at this metadata boundary; later prose, headings and arrows are not a beacon extent.")
        elif record["wrapper_range"] and not record["wrapper_dedicated"]:
            a, b = record["wrapper_range"]
            emit("mixed_comment_ownership", record, {**structural,
                 "ordinary_comment_before": snapshot.raw_bytes[a:record["opener_range"][0]].decode("utf-8"),
                 "ordinary_comment_after": snapshot.raw_bytes[record["read_end"]:b].decode("utf-8")},
                 "Metadata shares an ordinary comment; only the recorded metadata subranges may change, not the surrounding text.")
        if record["literal_certainty"] != "comment":
            emit("lexical_uncertainty", record, {"diagnostics": ds},
                 "Lexical ownership is uncertain for this record; select bounded metadata evidence or edit that metadata manually.")
        if record["complete"] and not record["wrapper_open_range"]:
            tail = snapshot.raw_bytes[record["bracket_range"][1]:snapshot.byte_offset(record["end_line"], len(snapshot.physical_lines[record["end_line"] - 1]))]
            if tail and not tail.strip(b" ,\t"):
                emit("noncanonical_terminator", record, {},
                     "A complete comma/space terminator is accepted; canonical normalization changes metadata only.")
        for diagnostic in ds:
            code = diagnostic["code"]
            if code in {"duplicate_field", "inline_field_ambiguity"}:
                emit(code, record, {**diagnostic, "occurrences": record["occurrences"],
                     "raw": record["field_texts"], "identical": code == "duplicate_field" and not diagnostic["conflicting"],
                     "alternatives": diagnostic.get("canonical_multiline", diagnostic.get("alternatives", []))},
                     "This metadata field has repeated or ambiguous interpretations; choose its value explicitly and preserve independent fields.",
                     kind=code + ":" + diagnostic.get("key", "inline"))
        if record["pair_state"] == "ambiguous":
            peer_lines = set(record["pair_candidates"])
            peers = [members[r["opener_range"][0]] for r in records if r["line"] in peer_lines]
            emit("pair_ambiguous", record, {"pair_candidates": record["pair_candidates"], "exact_id": bid},
                 "Several exact-id span operands are possible; select a physical pair, never an edit-distance guess.",
                 operands=[members[owner]] + peers)
        if record["marker"] == "close":
            if record["pair_state"] != "matched":
                candidates = [members[r["opener_range"][0]] for r in records if r["marker"] == "open" and
                              r["section"] == record["section"] and not r["closed"] and
                              (r["fields"].get("kind") or "span").strip().lower() == "span"]
                emit("orphan_close", record, {"exact_id": bid, "opener_candidates": candidates},
                     "This structural close matches no unique eligible exact-id span opener; ordinary marker mentions are not close candidates.",
                     operands=[members[owner]] + candidates)
            continue
        kind = (fields.get("kind") or "span").strip().lower() or "span"
        if kind not in {"ast", "span"}:
            emit("unknown_kind", record, {"kind_raw": fields.get("kind")},
                 f"Block {bid or '(no id)'} has kind={fields.get('kind')!r}; the parser skips this block, not its neighbours.")
        if not bid:
            emit("missing_id", record, {},
                 "This block has no id; give it an explicit id to make it addressable (Markdown drops an id-less block).")
        legacy = [key for key in ("comments", "role") if key in fields]
        if legacy:
            planner = next((a for a in alias_rows if a.get("owner") == owner), {})
            detail = {"keys": legacy}
            if planner:
                detail["planner"] = {k: v for k, v in planner.items() if k not in {"members", "candidates"}}
            emit("legacy_field", record, detail,
                 "Legacy fields remain readable: comments aliases comment; deprecated role is preserved unless explicitly changed.")
        unknown = list(dict.fromkeys(d["key"] for d in ds if d["code"] == "unknown_field"))
        if unknown:
            emit("unknown_field_key", record, {"keys": unknown},
                 "Unknown field keys remain opaque; correct a selected key or explicitly retain its text without changing independent fields.")
        block = block_by_line.get(record["line"])
        beacon = block.get("beacon") if block else None
        if not beacon:
            continue
        comment = beacon.get("comment")
        if comment is None or not str(comment).strip():
            emit("empty_comment", record, {"existing": comment, "requested": None, "note": None},
                 "This block has no comment; labels still work, and a description can be supplied explicitly.")
        else:
            try:
                validate_beacon_comment_text(comment)
            except ValueError as error:
                emit("comment_rejected", record, {"existing": comment, "requested": None, "note": None, "exception": str(error)},
                     f"The comment would be refused as new input ({error}); existing disk text is kept and label work may proceed.")
        projection = mapped.get(owner)
        if not projection:
            if not any(not e.get("section") for e in mapper_errors):
                emit("block_not_mapped", record, {"gap": block["gap"], "next_code_line": block["next_code_line"],
                     "candidates": [a.get("descriptor") for a in attachment_index["anchors"]]},
                     "This physical block reaches no mapped node, even if another copy of its id maps; relocate or convert metadata, never add FILE capability implicitly.")
        else:
            rec = projection["record"]
            if fields.get("kind") and kind != rec.get("kind"):
                emit("kind_changed_by_mapper", record, {"block_kind": kind, "mapped_kind": rec.get("kind")},
                     f"The block declares {kind} but maps as {rec.get('kind')}; choose span conversion or a current target rather than silently relocating it.")
            labels = str(rec.get("slice_labels") or "").strip()
            if not labels:
                emit("no_slice_labels", record, {}, f"Block {bid or '(no id)'} maps with no slice label, so it is in no slice.")
            elif not str(fields.get("slice_labels") or "").strip():
                emit("slice_labels_derived", record, {"slice_labels": labels},
                     "The mapper derived labels from the role/id prefix; supply explicit slice_labels to make the intended slice unambiguous.")
        effective_kind = projection["record"].get("kind") if projection else block["effective_kind"]
        if bid and effective_kind == "span" and not record["closed"] and not fields.get("start_snippet"):
            close_candidates = [members[r["opener_range"][0]] for r in records if r["marker"] == "close" and r["section"] == record["section"]]
            emit("span_without_close", record, {"exact_id": bid, "section": record["section"],
                 "close_candidates": close_candidates},
                 "This legal open-only span runs to its file/section end; keep it or explicitly choose an exact close.",
                 operands=[members[owner]] + close_candidates)
        if not fields.get("kind") and block["effective_kind"] == "ast":
            emit("implicit_kind_promoted", record, {},
                 "The shared attachment promotes this implicit block to AST; make the kind explicit or retain that interpretation.")
        if record["end_line"] is not None and record["line_extent"] is None:
            emit("block_end_mismatch", record, {"parser_end_line": record["end_line"], "writer_end_line": None},
                 "The bounded record has no whole-line writer extent; field/subrange work may still proceed without rewriting ordinary source.")
        if structures["language"] == "vue" and record["section"].get("type") != "script_element":
            emit("vue_capability", record, {"section": record["section"], "tags_delta_supported": False,
                 "legacy_absolute_supported": True},
                 "This actual Vue section is not script: tags deltas are script-only; legacy section metadata actions remain available.")

    for bid in duplicate_ids:
        copies = [b for b in blocks if _bid(b["record"]["fields"].get("id")) == bid]
        operands = [members[b["owner"]] for b in copies]
        lines = [b["record"]["line"] for b in copies]
        emit("duplicate_id", detail={"lines": lines, "mapped_records": sum(b["owner"] in mapped for b in copies),
              "suggested_ids": (id_suggestions or {}).get(bid, []),
              "close_candidates": [members[r["opener_range"][0]] for r in records
                                   if r["marker"] == "close" and any(
                                       r["section"] == b["record"]["section"] and
                                       (r["line"] == b["record"].get("close_line") or
                                        r["line"] in b["record"].get("pair_candidates", [])) for b in copies)]},
             message=f"Id {bid} has {len(copies)} physical copies; independently anchored copies remain writable, and an id-only request needs a member choice.",
             operands=operands, line=lines[1] if len(lines) > 1 else 0, bid=bid)

    # Every current target, untagged twins included. Tuple absence does not mean
    # an incomplete claimed block is absent; the index is the richer authority.
    for anchor in attachment_index["anchors"]:
        attached = [b for b in blocks if b.get("anchor") is anchor]
        operands = [members[b["owner"]] for b in attached]
        witness = next((m["anchor"] for m in operands if m.get("anchor")), None)
        if witness is None:
            # Callers supply the serializable descriptor for untagged targets.
            witness = anchor.get("descriptor")
        symbol = anchor.get("ast_qualname") or anchor["header"]
        twins = [a for a in attachment_index["anchors"] if a.get("ast_qualname") and a["ast_qualname"] == anchor.get("ast_qualname")]
        if len(twins) > 1:
            emit("writer_target_ambiguous", detail={"symbol": symbol, "candidates": [a.get("descriptor") for a in twins]}, operands=operands,
                 anchor=witness, line=anchor["line"],
                 message="This qualified name has several current targets; an independent physical witness can still select a member.")
        recs = next((n["records"] for n in mapped_nodes if n["line"] == anchor["outline_line"] and n.get("symbol") == anchor.get("ast_qualname")), [])
        if len(recs) > 1:
            planner = next((g for g in groups if any(m.get("opener_start_byte") in {b["owner"] for b in attached} for m in g["members"])), {})
            emit("two_blocks_one_symbol", detail={"ids": [_bid(r.get("id")) for r in recs],
                 "planner": {k: v for k, v in planner.items() if k not in {"members", "candidates"}}},
                 operands=operands, anchor=witness, line=anchor["line"],
                 message="Several physical blocks map to this target; only a shared-planner compatible same-id merge is automatic, otherwise choose the survivor/fields.")
        complete = [b for b in attached if b["record"]["line_extent"]]
        if len(complete) > 1:
            planner = next((g for g in groups if any(m.get("opener_start_byte") in {b["owner"] for b in attached} for m in g["members"])), {})
            emit("writer_anchor_ambiguous", detail={"ids": [b["record"]["fields"].get("id") for b in complete],
                 "planner": {k: v for k, v in planner.items() if k not in {"members", "candidates"}}}, operands=operands,
                 anchor=witness, line=anchor["line"],
                 message="Several complete blocks attach to this target; physical members remain selectable and incompatible merges need explicit choices.")
        for block in attached:
            record = block["record"]
            if block["owner"] not in mapped:
                emit("writer_anchor_foreign", record, {"target_line": anchor["line"], "symbol": symbol},
                     "The attachment index sees metadata on this target that the mapper does not carry; inspect the physical member and capability separately.")
            elif record["line_extent"] is None:
                emit("writer_anchor_mismatch", record, {"target_line": anchor["line"], "symbol": symbol, "writer_extent": None},
                     "The mapper attaches this bounded block but no whole-line writer tuple exists; inspect field/subrange repair instead of treating it as permission to create a second block.")
    for evidence in resemblance:
        block = block_by_line.get(evidence["line"])
        if block:
            emit("id_unlike_symbol", block["record"], evidence,
                 "This id resembles a different symbol or enclosing path; this is advisory only: keep, rename, relocate or edit its description explicitly.")
    return finalize_beacon_findings(rows)
