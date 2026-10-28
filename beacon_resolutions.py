"""Pure execution intents for explicitly selected current-disk findings.

The caller re-derives the finding from the supplied snapshot, and owns request
freshness, admission and publication. This module performs no IO. U01 records
own geometry, U03 renders field values, U04 owns alias normalization, and U02
proves every returned plan. No severity gate, automatic repair or preview token.

Structural selections consume U01 pairing, U03 attachment/rendering and U04
grouping. The prepared candidate is checked for the requested attachment/pair
before any caller can publish it; identity changes remain advisory.
"""
from __future__ import annotations

import bisect
import copy
from collections import Counter
import hashlib
import json

import nexus_map_codebase as source
from beacon_edits import BeaconEdit, BeaconEditPlan, source_line_edit, verify_beacon_edit_plan
from beacon_findings import beacon_member_key, canonical_beacon_file
from beacon_mutation_contract import (
    BeaconCommentIntent, parse_beacon_field_lines, resolve_beacon_comment,
    validate_beacon_comment_text,
)


FIELD_RESOLUTIONS = frozenset({
    "normalize", "insert_bracket", "insert_wrapper", "replace_metadata",
    "edit_metadata", "delete_metadata", "select_alternative", "set_field",
    "preserve_opaque", "rename_field", "delete_field", "convert_to_span",
    "edit_descriptive_field", "replace_comment", "append_comment", "keep", "retry",
})
STRUCTURAL_RESOLUTIONS = frozenset({
    "relocate", "create_block", "rename_id", "assign_ids", "merge", "pair_close",
    "retag_close", "add_close",
})


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _schema_errors(value, schema, path="params"):
    """Consume the catalogue's JSON-schema vocabulary, including member ranges.

    This is parameter validation only, not a second action catalogue. In
    particular, allOf/if/then restricts a range to its selected physical member.
    JSON equality distinguishes booleans from integer member indices and accepts
    both the in-process tuple and serialized array spelling of recorded ranges.
    """
    errors = []
    if "enum" in schema and _json(value) not in [_json(v) for v in schema["enum"]]:
        errors.append(path + " is not an offered value")
    if "const" in schema and _json(value) != _json(schema["const"]):
        errors.append(path + " does not match const")
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, (list, tuple)),
             "integer": type(value) is int, "string": isinstance(value, str),
             "boolean": type(value) is bool, "null": value is None}
    if kind and not valid[kind]:
        return errors + [path + " must be " + kind]
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        errors.extend(path + "." + key + " is required" for key in schema.get("required", ()) if key not in value)
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                errors.extend(_schema_errors(item, properties[key], path + "." + key))
            elif extra is False:
                errors.append(path + "." + key + " is not allowed")
            elif isinstance(extra, dict):
                errors.extend(_schema_errors(item, extra, path + "." + key))
    if isinstance(value, (list, tuple)) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(_schema_errors(item, schema["items"], f"{path}[{index}]"))
    if type(value) is int and "minimum" in schema and value < schema["minimum"]:
        errors.append(path + " is below minimum")
    for subschema in schema.get("allOf", ()):
        errors.extend(_schema_errors(value, subschema, path))
    if "anyOf" in schema and all(_schema_errors(value, s, path) for s in schema["anyOf"]):
        errors.append(path + " matches no offered alternative")
    if "if" in schema and not _schema_errors(value, schema["if"], path):
        errors.extend(_schema_errors(value, schema.get("then", {}), path))
    return errors


def _refused(kind, message, **evidence):
    return {"success": False, "status": "stale" if kind == "finding_not_found" else "refused",
            "changed": False, "error_kind": kind, "error": message, "evidence": evidence,
            "edit_plan": None}


def _current_members(snapshot, finding, records):
    """Pin the already re-derived finding to this exact snapshot and U01 ranges."""
    observed = finding.get("observed") or {}
    if (observed.get("file_sha256") != snapshot.sha256 or
            observed.get("canonical_file") != canonical_beacon_file(snapshot.canonical_path)):
        raise ValueError("finding does not belong to the supplied snapshot")
    by_owner = {r["opener_range"][0]: r for r in records}
    bound = []
    members = finding.get("members") or []
    if _json(observed.get("member_fingerprints", [])) != _json([beacon_member_key(m) for m in members]):
        raise ValueError("finding member fingerprints do not match its observed witness")
    for member in members:
        record = by_owner.get(member.get("opener_start_byte"))
        if record is None:
            raise ValueError("finding member has no current physical record")
        owner, stop = record["opener_range"][0], record["read_end"]
        if (member.get("block_hash") != hashlib.sha256(snapshot.raw_bytes[owner:stop]).hexdigest() or
                _json(member.get("read_range")) != _json([owner, stop])):
            raise ValueError("finding member bytes changed")
        for key in ("opener_range", "field_ranges", "write_ranges", "section"):
            if _json(member.get(key)) != _json(record.get(key)):
                raise ValueError("finding member geometry changed: " + key)
        bound.append(record)
    return bound


def _state(record, fields=None, *, absent=False, complete=None):
    state = {"owner": record["opener_range"][0], "operation": "absent" if absent else "present",
             "marker": record["marker"], "fields": fields or {}}
    if complete is not None:
        state["complete"] = complete
    return state


def _delete_occurrences(record, occurrences):
    return [BeaconEdit(*loc["byte_range"], b"", record["opener_range"][0])
            for occurrence in occurrences for loc in occurrence["locations"]]


def _field_plan(snapshot, language, record, key, value):
    """Explicit values resolve only this field's repeats; U03 renders the value.

    Keep the first physical occurrence (and its exact prefix/delimiters), removing
    only subsequent located field text. Other ambiguous/opaque fields are not
    interpreted, normalized or used as a file-wide veto.
    """
    keys = {"comment", "comments"} if key == "comment" else {key}
    occurrences = [o for o in record["occurrences"] if o["key"] in keys]
    selected = copy.deepcopy(record)
    selected["occurrences"] = [o for o in record["occurrences"] if o["key"] not in keys] + occurrences[:1]
    # The caller supplied the interpretation of precisely this ambiguous field.
    ambiguous_raw = {o["raw"][0] for o in occurrences}
    selected["diagnostics"] = [d for d in record["diagnostics"] if not (
        d["code"] == "inline_field_ambiguity" and d["raw"] in ambiguous_raw)]
    errors = []
    edits = source._build_beacon_block_for_language(
        language, snapshot=snapshot, record=selected, field_updates={key: value}, field_errors=errors)
    if errors:
        raise ValueError("field has no safe rendering: " + str(errors))
    edits.extend(_delete_occurrences(record, occurrences[1:]))
    actual_key = occurrences[0]["key"] if occurrences else key
    expected = {actual_key: value if value is not None else ""}
    for removed in occurrences[1:]:
        if removed["key"] != actual_key:
            expected[removed["key"]] = None
    return edits, _state(record, expected)


def _line_at(snapshot, boundary):
    return min(bisect.bisect_right(snapshot.byte_offsets, boundary) - 1,
               len(snapshot.physical_lines) - 1)


def _prefixes(snapshot, record):
    """Serialize only inserted delimiter/field rows in the recorded comment form."""
    row = record["line"] - 1
    text = snapshot.physical_lines[row]
    indent = text[:len(text) - len(text.lstrip())]
    if record["comment_form"] == "line":
        prefix = snapshot.raw_bytes[snapshot.byte_offsets[row]:record["opener_range"][0]].decode("utf-8")
    else:
        prefix = indent + (" * " if record["comment_form"] == "block" else "")
    return prefix, prefix + "  "


def _newline(snapshot, boundary):
    row = _line_at(snapshot, boundary)
    return snapshot.line_endings[row] or next((e for e in snapshot.line_endings if e), "\n")


def _field_key_at(record, bounds):
    start, _stop = bounds
    occurrence = next((o for o in record["occurrences"] if o["byte_range"][0] == start), None)
    if occurrence:
        return occurrence["key"], occurrence
    row = next((i for i, r in enumerate(record["field_ranges"]) if tuple(r) == tuple(bounds)), None)
    if row is not None:
        # The shared parser, not another key/value regex, recognizes the opaque key.
        diagnostic = next((d for d in record["diagnostics"] if d["code"] == "unknown_field"
                           and d["raw"] == record["field_texts"][row]), None)
        if diagnostic:
            return diagnostic["key"], None
    raise ValueError("selected range does not start at a recorded field key")


def _id_updates(snapshot, language, record, value, records, taken, *,
                chosen_close=None, open_only=False, allocate_empty=False):
    """One id intent for fields, renames, assignments and explicit pair choices.

    Unique closes and candidate edges come only from U01. Ambiguous spans need a
    chosen physical close or explicit open_only; empty structural new_id requests
    deterministic allocation against the accumulated current+planned set.
    """
    old = record["fields"].get("id")
    if not value and (not old or allocate_empty):
        value = source.allocate_beacon_id(taken, base_id=old or "auto-beacon", random_parts=0)
    if value != old and value in taken:
        raise ValueError("requested id collides with a current or planned id: " + value)
    edits, intent = _field_plan(snapshot, language, record, "id", value)
    states, owners = [intent], [record["opener_range"][0]]
    if chosen_close is not None and open_only:
        raise ValueError("choose a close or open_only, not both")
    if record["marker"] == "open" and (value != old or chosen_close is not None or open_only):
        if record["pair_state"] == "ambiguous" and chosen_close is None and not open_only:
            raise ValueError("rename needs a selected pair or explicit open-only outcome")
        close = chosen_close
        if record["closed"]:
            unique = next(r for r in records if r["marker"] == "close" and
                          r["line"] == record["close_line"] and r["section"] == record["section"])
            if open_only or (close is not None and close is not unique):
                raise ValueError("the scanner already assigns a unique close; rename that pair together")
            close = unique
        if close is not None:
            _pair_operand(record, close)
            close_edits, close_state = _field_plan(snapshot, language, close, "id", value)
            edits.extend(close_edits)
            states.append(close_state)
            owners.append(close["opener_range"][0])
            states[0]["paired_close_owner"] = close["opener_range"][0]
            states[0]["pair_state"] = "closed"
        elif open_only:
            states[0]["pair_state"] = "open_only"
    return edits, states, owners, value


def _pair_operand(opening, closing):
    """Validate physical operands, then let U01 prove their prepared relation."""
    if (opening["marker"] != "open" or closing["marker"] != "close" or
            (opening["fields"].get("kind") or "span").strip().lower() != "span" or
            opening["section"] != closing["section"] or opening["line"] >= closing["line"]):
        raise ValueError("selected operands are not an opener and later close in the same span section")
    if opening["pair_candidates"] and closing["line"] not in opening["pair_candidates"]:
        raise ValueError("selected close is not one of this scanner's ambiguous edges")
    if closing["pair_candidates"] and opening["line"] not in closing["pair_candidates"]:
        raise ValueError("selected opener is not one of this scanner's ambiguous edges")


def _chosen_close(params, records):
    if "close" not in params:
        return None
    selected = next((r for r in records if r["marker"] == "close" and
                     r["opener_range"][0] == params["close"]), None)
    if selected is None:
        raise ValueError("chosen close byte is not a current scanner close operand")
    return selected


def _attachment_view(snapshot, language, records=None):
    """Use the check's supplied-snapshot map view, never a second outline parser."""
    outline = source.map_source_snapshot(snapshot)["children"]
    structures = {"language": language, "outline": outline,
                  "records": records if records is not None else source._beacon_edit_records(snapshot, language)}
    if language == "vue":
        structures["sections"] = [s for s, _r in source._vue_section_records(snapshot)]
    return structures, source.beacon_attachment_index(snapshot, outline, structures)


def _target_anchor(snapshot, language, target, structures, index):
    """Translate an explicit current header to U03's pinned twin witness."""
    request = {**target, "language": language, "operation": "delta_ensure"}
    selected = source.select_beacon_block_for_node(snapshot, structures, request)
    header = target.get("header")
    if selected.get("anchor") is None and header:
        candidates = selected.get("anchor_candidates") or []
        matches = [i for i, a in enumerate(candidates) if a["header"] == header]
        if len(matches) == 1:
            request["twin_witness"] = {"ast_qualname": target.get("ast_qualname"),
                "md_path": target.get("md_path"), "ordinal": matches[0] + 1,
                "count": len(candidates), "source_sha256": snapshot.sha256,
                "source_headers": [a["header"] for a in candidates]}
            selected = source.select_beacon_block_for_node(snapshot, structures, request)
    anchor = selected.get("anchor")
    if anchor is None or (header is not None and anchor["header"] != header):
        raise ValueError("explicit target has no unique current U03 anchor: " + str(selected.get("anchor_candidates", selected.get("error"))))
    return anchor


# @beacon[
#   id=beacon-resolutions@_render_at_anchor,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Renders an explicit structural insertion in U03 target form and indentation before syntax-owned decorators; relocation keeps scanner raw fields including aliases continuations and opaque metadata rather than losing them to a semantic rebuild.,
# ]
def _render_at_anchor(snapshot, language, anchor, *, record=None, bid=None):
    """U03 supplies target form/indentation; relocation retains every raw field.

    Field texts are scanner operands, including opaque keys and continuations;
    they are not reparsed or reconstructed from a lossy semantic dictionary.
    """
    section = "script"
    if language == "vue":
        section = next(s["type"].removesuffix("_element") for s, _r in source._vue_section_records(snapshot)
                       if s["content_start_line"] <= anchor["line"] <= s["content_end_line"])
    rows = source._build_beacon_block_for_language(language, bid=bid or "", kind=None if record else "ast", section=section)
    if record is not None:
        field_index = next(i for i, row in enumerate(rows) if "id=" in row)
        prefix = rows[field_index].partition("id=")[0]
        texts = list(record["field_texts"])
        if record["field_ranges"] and _line_at(snapshot, record["field_ranges"][0][0]) == record["line"] - 1:
            # Inline comma splitting belongs to the opener row in U01; moving
            # this raw operand to a continuation row would change its meaning.
            rows[field_index - 1] += texts.pop(0)
        rows[field_index:field_index + 1] = [prefix + text for text in texts]
    row = min([anchor["line"] - 1, *[a for a, _b in anchor.get("decorator_ranges", [])]])
    rows = source._indent_beacon_block(rows, list(snapshot.physical_lines), row)
    return row, rows


# @beacon[
#   id=beacon-resolutions@_merge_intent,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Executes only a group returned by U04 with the explicit survivor and field choices; identical auto choices reuse U04 edits while explicit choices splice the survivor and delete only other members owned metadata.,
# ]
def _merge_intent(snapshot, language, members, params, structures, index, records, taken):
    """Execute a U04 group; explicit choices replace its auto field verdict only."""
    hygiene = source.plan_beacon_hygiene(snapshot, structures, index)
    owner_set = {r["opener_range"][0] for r in members}
    group = next((g for g in hygiene["applied_groups"] + hygiene["skipped_groups"]
                  if {m["opener_start_byte"] for m in g["members"]} == owner_set), None)
    if group is None:
        raise ValueError("finding members do not constitute one current U04 group")
    survivor = members[params["survivor"]]
    owner = survivor["opener_range"][0]
    choices = dict(params["fields"])
    labels = source._canonicalize_slice_labels_union([r["fields"].get("slice_labels") or "" for r in members] +
                                                       [choices.pop("slice_labels", "")])
    comments = [r["fields"].get("comment") or r["fields"].get("comments") or "" for r in members]
    comment = choices.pop("comment", source._merge_unique_strings(comments))
    if comment:
        validate_beacon_comment_text(comment)
    for key in ("role", "start_snippet", "end_snippet", "show_span"):
        values = {r["fields"].get(key) for r in members} - {None, ""}
        if (len(values) > 1 or values and survivor["fields"].get(key) not in values) and key not in choices:
            raise ValueError("explicit merge needs a field choice for " + key + ": " + str(sorted(values)))
    new_id = choices.pop("id", None)
    updates = {**choices, "slice_labels": labels, "comment": comment or None}
    auto = (group["status"] == "planned" and group["survivor"]["opener_start_byte"] == owner and
            labels == group["labels"] and (comment or None) == group["comment"] and not choices and new_id is None)
    if auto:
        plan = hygiene["edit_plan"]
        return ([e for e in plan.edits if e.owner in owner_set],
                [s for s in plan.intended_state if s["owner"] in owner_set], list(owner_set))
    edits, states = [], []
    if new_id is not None:
        id_edits, id_states, _id_owners, _value = _id_updates(snapshot, language, survivor, new_id,
                                                           records, taken, allocate_empty=True)
        edits.extend(id_edits)
        states.extend(id_states)
    # Consolidate missing-field insertions at one scanner-owned boundary.
    missing = {}
    for key, value in updates.items():
        if not any(o["key"] in ({"comment", "comments"} if key == "comment" else {key}) for o in survivor["occurrences"]):
            missing[key] = value
        else:
            field_edits, state = _field_plan(snapshot, language, survivor, key, value)
            edits.extend(field_edits)
            states.append(state)
    errors = []
    edits.extend(source._build_beacon_block_for_language(language, snapshot=snapshot, record=survivor,
                 field_updates=missing, field_errors=errors))
    if errors:
        raise ValueError("merge field choices have no owned rendering: " + str(errors))
    if missing:
        states.append(_state(survivor, {k: v for k, v in missing.items() if v not in (None, "")}))
    if not states:
        states.append(_state(survivor, survivor["fields"]))
    for record in members:
        if record is survivor:
            continue
        if not record["write_ranges"]:
            raise ValueError("merge member has no whole metadata ownership; repair it and re-list")
        edits.extend(BeaconEdit(a, b, b"", record["opener_range"][0]) for a, b in record["write_ranges"])
        states.append(_state(record, absent=True))
    return edits, states, list(owner_set)


# @beacon[
#   id=beacon-resolutions@_verify_structural_intents,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Checks selected span pairing and predicted target attachment on the bytes already prepared by U02; consumes the same scanner and supplied-snapshot map view and refuses locally before any publication.,
# ]
def _verify_structural_intents(plan, proof, records, language, attachment_checks):
    """Verify selected pair and target mapping on U02's already prepared bytes."""
    candidate = source.source_snapshot_from_bytes(proof["candidate_bytes"], plan.snapshot.canonical_path)
    by_owner = {r["opener_range"][0]: r for r in records}
    offsets = proof["offset_map"]
    for intent in plan.intended_state:
        if "pair_state" not in intent:
            continue
        current = by_owner.get(offsets.map_offset(intent["owner"], bias="right"))
        if current is None or current["pair_state"] != intent["pair_state"]:
            raise ValueError("prepared scanner pairing differs from selected " + intent["pair_state"])
        close_owner = intent.get("paired_close_owner")
        if close_owner is not None:
            close = by_owner.get(offsets.map_offset(close_owner, bias="right"))
            if close is None or current["close_line"] != close["line"] or close["pair_state"] != "matched":
                raise ValueError("prepared scanner did not assign the explicitly selected close")
    if attachment_checks:
        _structures, index = _attachment_view(candidate, language, records)
        for insertion, anchor, fields in attachment_checks:
            start = offsets.map_offset(insertion.start_byte, bias="left")
            blocks = [b for b in index["blocks"] if start <= b["owner"] < start + len(insertion.replacement)]
            expected_byte = offsets.map_offset(plan.snapshot.byte_offsets[anchor["line"] - 1], bias="right")
            targets = [a for a in index["anchors"] if candidate.byte_offsets[a["line"] - 1] == expected_byte and
                       a["header"] == anchor["header"] and a.get("ast_qualname") == anchor.get("ast_qualname")]
            actual = blocks[0].get("anchor") if len(blocks) == 1 else None
            attached = len(targets) == 1 and len(blocks) == 1 and (
                actual == targets[0] or blocks[0]["effective_kind"] == "span" and
                blocks[0]["next_code_line"] == targets[0]["line"])
            if (not attached or any(blocks[0]["record"]["fields"].get(k) != v for k, v in fields.items())):
                raise ValueError("prepared metadata does not map to the explicitly selected current target")


def _identity_consequence(snapshot, old_ids, new_ids):
    return {"potential_workflowy_uuid_change": True, "stale_note_anchors": True,
            "old_ids": sorted(old_ids), "new_ids": sorted(new_ids),
            "in_scope_textual_dependants": [
                {"line": line, "text": text, "ids": sorted(bid for bid in old_ids if bid in text)}
                for line, text in enumerate(snapshot.physical_lines, 1)
                if any(bid in text for bid in old_ids)],
            "advisory": "Source metadata changed; cached note anchors and Workflowy identities may change on refresh."}


# @beacon[
#   id=beacon-resolutions@plan_beacon_resolution,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Pure catalogue dispatch validates physical members and explicit choices; add_close inserts one metadata line only at a physical line start or EOF; shared byte proof and prepared attachment/pair checks precede plans with advisory identity effects and no IO or admission.,
# ]
def plan_beacon_resolution(snapshot, finding, resolution, params=None, *, language, planned_ids=None):
    """Return a verified U02 plan or a local refusal for one current finding.

    ``finding`` must already be re-derived from ``snapshot``. Request-level
    freshness/rebinding belongs to the fix front, not this pure component.
    ``planned_ids`` is the caller's accumulating request set, updated only after
    proof and id-collision checks succeed, including ids introduced by a bounded
    metadata replacement. Identity effects are advisory. Keep/retry/preserve_opaque
    have no plan and never publish.
    Structural work uses the scanner's pairing, U03 targets/rendering and U04
    groups; prepared pair/attachment checks happen before returning a plan.
    A new span close occupies one physical line at a line start or EOF, retaining
    the original file's terminated or unterminated EOF state without blank lines.
    """
    option = next((o for o in finding.get("fix", {}).get("resolutions", ())
                   if o["resolution"] == resolution), None)
    if option is None:
        return _refused("resolution_not_offered", "resolution is not offered by this finding", resolution=resolution)
    params = {} if params is None else params
    try:
        errors = _schema_errors(params, option["params_schema"])
    except (TypeError, ValueError) as error:
        errors = [str(error)]
    if errors:
        return _refused("invalid_params", "parameters do not match this finding's schema", errors=errors)
    records = source._beacon_edit_records(snapshot, language)
    try:
        members = _current_members(snapshot, finding, records)
    except ValueError as error:
        return _refused("finding_not_found", str(error), observed=finding.get("observed"))
    if resolution in {"keep", "retry", "preserve_opaque"}:
        return {"success": True, "status": "noop", "changed": False, "edit_plan": None,
                "noop_reason": {"keep": "kept", "retry": "retry_read_only", "preserve_opaque": "opaque_preserved"}[resolution],
                "finding": finding, "consequences": []}
    if resolution not in FIELD_RESOLUTIONS | STRUCTURAL_RESOLUTIONS:
        return _refused("resolution_not_offered", "unknown executable catalogue name", resolution=resolution)
    record = members[params.get("member", params.get("opener", params.get("survivor", params.get("keeper", 0))))] if members else None
    owner = record["opener_range"][0] if record else None
    edits, states, owners, slots, consequences, reserved, attachment_checks = [], [], [], [], [], set(), []
    if owner is not None:
        owners.append(owner)
    taken = {r["fields"].get("id") for r in records if r["fields"].get("id")} | set(planned_ids or ())
    try:
        if resolution in {"rename_id", "assign_ids", "pair_close"}:
            if resolution == "assign_ids":
                assignments = params["new_ids"]
                indices = [a["member"] for a in assignments]
                expected = set(range(len(members))) - {params["keeper"]}
                if len(indices) != len(set(indices)) or set(indices) != expected:
                    raise ValueError("assign_ids names each nonkeeper exactly once; the explicitly chosen keeper is untouched")
            elif resolution == "pair_close":
                opening = members[params["opener"]]
                close = members[params["close"]] if "close" in params else None
                if close is not None:
                    _pair_operand(opening, close)
                value = params.get("new_id")
                if value is None:
                    if opening["pair_state"] == "ambiguous" or params.get("open_only"):
                        raise ValueError("persisting an ambiguous pair/open-only choice needs explicit new_id; empty requests allocation")
                    value = opening["fields"].get("id")
                assignments = [{"member": params["opener"], "new_id": value,
                                **({"close": close["opener_range"][0]} if close is not None else {}),
                                "open_only": params.get("open_only", False)}]
            else:
                assignments = [params]
            edits, states, owners = [], [], []
            for assignment in assignments:
                selected = members[assignment["member"]]
                if resolution == "assign_ids" and assignment["new_id"] and assignment["new_id"] in taken | reserved:
                    raise ValueError("assigned id collides with a current or planned id: " + assignment["new_id"])
                close = _chosen_close(assignment, records)
                new_edits, new_states, new_owners, value = _id_updates(snapshot, language, selected,
                    assignment["new_id"], records, taken | reserved, chosen_close=close,
                    open_only=assignment.get("open_only", False), allocate_empty=True)
                edits.extend(new_edits)
                states.extend(new_states)
                owners.extend(new_owners)
                reserved.add(value)
                old = selected["fields"].get("id")
                if old != value:
                    consequences.append(_identity_consequence(snapshot, {old} if old else set(), {value}))
                if assignment.get("open_only"):
                    consequences.append({"open_only": True, "opener": selected["opener_range"][0],
                                         "new_id": value, "advisory": "The selected opener remains an open-only span; no close was renamed."})
        elif resolution == "retag_close":
            if record["marker"] != "close":
                raise ValueError("retag_close requires a current structural close")
            edits, state = _field_plan(snapshot, language, record, "id", params["new_id"])
            states = [state]
            eligible = [r for r in records if r["marker"] == "open" and not r["closed"] and
                        r["section"] == record["section"] and r["line"] < record["line"] and
                        (r["fields"].get("kind") or "span").strip().lower() == "span" and
                        r["fields"].get("id") == params["new_id"]]
            if len(eligible) > 1:
                raise ValueError("close id names several scanner operands; select pair_close with an explicit new_id")
            if eligible:
                opening = eligible[0]
                owners.append(opening["opener_range"][0])
                intent = _state(opening)
                intent.update(pair_state="closed", paired_close_owner=owner)
                states.append(intent)
        elif resolution == "add_close":
            boundary = params["boundary"]
            if (record["marker"] != "open" or (record["fields"].get("kind") or "span").strip().lower() != "span" or
                    not record["fields"].get("id")):
                raise ValueError("add_close needs an identified span opener")
            section = record["section"]
            if (not record["read_end"] <= boundary <= section.get("end_byte", snapshot.byte_offsets[section["end_line"]]) or
                    boundary not in snapshot.byte_offsets[:len(snapshot.physical_lines)] and boundary != len(snapshot.raw_bytes)):
                raise ValueError("boundary must be a physical line start within the span section, or EOF")
            prefix, _field_prefix = _prefixes(snapshot, record)
            bid = record["fields"]["id"]
            close = "@beacon-close[id=" + bid + "]"
            if record["comment_form"] == "html":
                text = "<!-- " + close + " -->"
            elif record["comment_form"] == "block":
                indent = snapshot.physical_lines[record["line"] - 1]
                indent = indent[:len(indent) - len(indent.lstrip())]
                text = indent + "/* " + close + " */"
            else:
                text = prefix + close
            ending = _newline(snapshot, boundary)
            text = (ending + text if boundary == len(snapshot.raw_bytes) and not snapshot.line_endings[-1]
                    else text + ending)
            insertion = BeaconEdit(boundary, boundary, text.encode("utf-8"), "close_insertion")
            edits, slots = [insertion], [boundary]
            states = [_state(record), {"owner": "close_insertion", "operation": "present", "marker": "close",
                                      "fields": {"id": bid}, "complete": True}]
            # The selected close must actually close this opener, not a duplicate.
            states[0].update(pair_state="closed")
        elif resolution in {"relocate", "create_block", "merge"}:
            structures, index = _attachment_view(snapshot, language, records)
            if resolution == "merge":
                edits, states, owners = _merge_intent(snapshot, language, members, params, structures, index, records, taken)
            else:
                anchor = _target_anchor(snapshot, language, params["target"], structures, index)
                if resolution == "relocate":
                    if not record["write_ranges"]:
                        raise ValueError("relocation needs this member's owned metadata extent; repair it and re-list")
                    states = [_state(record, record["fields"], complete=True)]
                    intended_fields = record["fields"]
                    current_anchor = index["by_opener"][owner].get("anchor")
                    if current_anchor and current_anchor["line"] == anchor["line"]:
                        insertion = None
                    else:
                        row, rendered = _render_at_anchor(snapshot, language, anchor, record=record)
                        insertion = source_line_edit(snapshot, row, row, rendered, owner)
                        edits = [BeaconEdit(a, b, b"", owner) for a, b in record["write_ranges"]]
                        edits.append(insertion)
                else:
                    bid = params["id"] or source.allocate_beacon_id(taken, base_id="auto-beacon", random_parts=0)
                    if bid in taken:
                        raise ValueError("requested create id collides with a current or planned id: " + bid)
                    reserved.add(bid)
                    row, rendered = _render_at_anchor(snapshot, language, anchor, bid=bid)
                    insertion = source_line_edit(snapshot, row, row, rendered, "insertion")
                    edits, owners = [insertion], []
                    intended_fields = {"id": bid, "kind": "ast"}
                    states = [{"owner": "insertion", "operation": "present", "marker": "open",
                               "fields": intended_fields, "complete": True}]
                if insertion is not None:
                    slots = [insertion.start_byte]
                    attachment_checks.append((insertion, anchor, intended_fields))
                consequences.append({"target": source._beacon_anchor_descriptor(anchor),
                                     "explicit_selection": True, "relocated": resolution == "relocate"})
        elif resolution in {"set_field", "edit_descriptive_field", "convert_to_span", "replace_comment", "append_comment"}:
            key = "kind" if resolution == "convert_to_span" else "comment" if resolution.endswith("_comment") else params["field"]
            value = "span" if resolution == "convert_to_span" else params.get("value")
            if resolution in {"replace_comment", "append_comment"}:
                intent = (BeaconCommentIntent.replace(params["comment"]) if resolution == "replace_comment"
                          else BeaconCommentIntent.append(params["comment"]))
                resolved = resolve_beacon_comment(intent=intent, note_comment=None,
                    disk_comment=record["fields"].get("comment") or record["fields"].get("comments"),
                    beacon_exists_on_disk=True)
                value = resolved.resolved
                consequences.append({"comment": resolved.to_dict()})
            elif key in {"comment", "comments"}:
                value = validate_beacon_comment_text(value)
            if key == "id":
                edits, states, owners, value = _id_updates(snapshot, language, record, value, records, taken)
                reserved.add(value)
                old = record["fields"].get("id")
                if old and old != value:
                    consequences.append(_identity_consequence(snapshot, {old}, {value} if value else set()))
            else:
                edits, state = _field_plan(snapshot, language, record, key, value)
                states.append(state)
        elif resolution == "normalize":
            if finding["code"] == "legacy_field":
                alias = source.plan_beacon_comment_aliases(snapshot, language)["edit_plan"]
                edits = [e for e in alias.edits if e.owner == owner]
                states = [s for s in alias.intended_state if s["owner"] == owner]
                if not edits:
                    raise ValueError("shared alias planner offers no normalization for this member")
            elif finding["code"] == "duplicate_field" and finding["detail"].get("identical"):
                key = finding["detail"]["key"]
                occurrences = [o for o in record["occurrences"] if o["key"] == key]
                edits = _delete_occurrences(record, occurrences[1:])
                states = [_state(record, {key: occurrences[0]["value"]})]
            elif finding["code"] == "noncanonical_terminator":
                a = record["bracket_range"][1]
                row = _line_at(snapshot, a)
                b = snapshot.byte_offset(row + 1, len(snapshot.physical_lines[row]))
                edits = [BeaconEdit(a, b, b"", owner)]
                states = [_state(record, record["fields"], complete=True)]
            else:
                raise ValueError("shared evidence offers no canonical normalization for this member")
        elif resolution in {"insert_bracket", "insert_wrapper"}:
            boundary = params["boundary"]
            if boundary not in record["repair_candidates"].values():
                raise ValueError("boundary belongs to a different member")
            if resolution == "insert_bracket":
                prefix, _field_prefix = _prefixes(snapshot, record)
                text = _newline(snapshot, boundary) + prefix + "]"
                complete = not record["wrapper_open_range"] or bool(record["wrapper_range"])
            else:
                if not record["wrapper_open_range"]:
                    raise ValueError("selected member has no recorded wrapper opener")
                text = " -->" if record["comment_form"] == "html" else " */"
                complete = bool(record["bracket_range"])
            edits = [BeaconEdit(boundary, boundary, text.encode("utf-8"), owner)]
            slots = [boundary]
            states = [_state(record, record["fields"], complete=complete)]
        elif resolution in {"replace_metadata", "edit_metadata", "delete_metadata", "delete_field"}:
            a, b = params["range"]
            text = "" if resolution in {"delete_metadata", "delete_field"} else params["text"]
            edits = [BeaconEdit(a, b, text.encode("utf-8"), owner)]
            states = [_state(record, absent=(a <= owner < b))] if not text else [_state(record)]
        elif resolution == "rename_field":
            bounds, key = params["range"], params["field"]
            old_key, occurrence = _field_key_at(record, bounds)
            a = bounds[0]
            edits = [BeaconEdit(a, a + len(old_key.encode("utf-8")), key.encode("utf-8"), owner)]
            expected = {key: occurrence["value"]} if occurrence else {}
            states = [_state(record, expected)]
        elif resolution == "select_alternative":
            diagnostic = next(d for d in record["diagnostics"] if d["code"] == "inline_field_ambiguity"
                              and params["alternative"] in d["canonical_multiline"])
            occurrence = next(o for o in record["occurrences"] if o["raw"][0] == diagnostic["raw"])
            prefix, field_prefix = _prefixes(snapshot, record)
            ending = _newline(snapshot, occurrence["byte_range"][0])
            rows = params["alternative"]
            replacement = ending + ending.join(field_prefix + row for row in rows) + ending + prefix
            edits = [BeaconEdit(occurrence["byte_range"][0], occurrence["locations"][-1]["byte_range"][1],
                                replacement.encode("utf-8"), owner)]
            states = [_state(record, parse_beacon_field_lines(rows))]
        slots.extend(e.start_byte for e in edits if e.start_byte == e.end_byte)
        # Already-satisfied field values return no publishable plan (including
        # no byte-identical replace that would needlessly change mtime).
        edits = [e for e in edits if snapshot.raw_bytes[e.start_byte:e.end_byte] != e.replacement]
        plan = BeaconEditPlan(snapshot, language, tuple(edits), tuple(owners), tuple(states),
                              insertion_slots=tuple(sorted(set(slots))))
        proof = verify_beacon_edit_plan(plan, scanner=source._beacon_edit_records)
        if not proof["success"]:
            return {**proof, "status": "refused", "changed": False, "edit_plan": None}
        # Even a caller-selected raw metadata correction is not a back door
        # around the catalogue's current+planned id collision semantics. Count
        # openers only: an opener and its assigned close intentionally share id.
        candidate = source.source_snapshot_from_bytes(proof["candidate_bytes"], snapshot.canonical_path)
        after_records = source._beacon_edit_records(candidate, language)
        _verify_structural_intents(plan, proof, after_records, language, attachment_checks)
        old_counts = Counter(r["fields"].get("id") for r in records if r["marker"] == "open" and r["fields"].get("id"))
        new_counts = Counter(r["fields"].get("id") for r in after_records if r["marker"] == "open" and r["fields"].get("id"))
        introduced = set(new_counts) - set(old_counts)
        collisions = {bid for bid, count in new_counts.items() if count > 1 and count > old_counts[bid]}
        collisions.update(introduced & taken)
        if collisions:
            raise ValueError("requested metadata ids collide with current or planned ids: " + str(sorted(collisions)))
        reserved.update(introduced)
        removed = set(old_counts) - set(new_counts)
        if (introduced or removed) and not consequences and record and record["fields"].get("id"):
            consequences.append(_identity_consequence(snapshot, removed, introduced))
    except (ValueError, StopIteration, KeyError, IndexError) as error:
        return _refused("no_safe_geometry", str(error), member=params.get("member"), resolution=resolution)
    if planned_ids is not None:
        planned_ids.update(reserved)
    return {"success": True, "status": "planned" if edits else "noop", "changed": bool(edits),
            "noop_reason": None if edits else "already_satisfied", "edit_plan": plan if edits else None,
            "proof": proof, "finding": finding, "consequences": consequences,
             "affected_ids": sorted({v for v in [*[r["fields"].get("id") for r in members], *reserved] if v})}
