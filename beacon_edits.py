"""Bounded source-byte edits, one pure proof, and one atomic IO authority.

Selection and field policy belong to callers. This module consumes the shared
scanner's geometry, never infers ownership from text or from a later bracket.
The final hash is a check immediately before replace, not a filesystem CAS:
an uncooperative replacement between those instructions is the stated T2 race.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import shutil
import tempfile
from typing import Callable

from beacon_source import (
    SourceSnapshot, source_snapshot_from_bytes, scan_beacon_structure,
    source_lexical_evidence,
)


# @beacon[
#   id=beacon-edits@BeaconEdit,
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=One half-open original-byte splice with a physical metadata owner or an explicitly authorized insertion/document intent; no text-coordinate conversion.,
# ]
@dataclass(frozen=True)
class BeaconEdit:
    """An edit in the immutable snapshot's byte coordinates.

    Metadata owners are opener byte offsets, not ids (which may be duplicated).
    ``insertion`` and ``document`` are explicit caller intents; the plan must
    also authorize their slots or whole-document generation/repair respectively.
    """
    start_byte: int
    end_byte: int
    replacement: bytes
    owner: int | str


# @beacon[
#   id=beacon-edits@BeaconEditPlan,
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=One snapshot/hash plus physical target witnesses and byte edits; keeps intended field states and permitted structural effects separate from selection and from filesystem IO.,
# ]
@dataclass(frozen=True)
class BeaconEditPlan:
    """Pure commit input; missing source and existing-empty are distinct.

    ``target_witness`` lists selected physical opener offsets. ``intended_state``
    rows name owner, operation (present/absent), and expected fields/marker.
    A scanner adapter may provide the shared scanner's section-aware Vue view;
    it must be pure and consume the supplied snapshot, never reopen the path.
    The explicit ``whole_document`` effect is for generation/C0 repair only.
    Ordinary metadata writers never opt into it.
    """
    snapshot: SourceSnapshot
    language: str
    edits: tuple[BeaconEdit, ...]
    target_witness: tuple[int, ...] = ()
    intended_state: tuple[dict, ...] = ()
    permitted_structural_effects: tuple[str, ...] = ()
    insertion_slots: tuple[int, ...] = ()
    source_exists: bool = True

    @property
    def source_sha256(self) -> str:
        return self.snapshot.sha256


# @beacon[
#   id=beacon-edits@EditOffsetMap,
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Reusable original-to-candidate byte boundary mapping with explicit insertion affinity; replaced interiors have no surviving coordinate and untouched ranges map independently of line-number drift.,
# ]
@dataclass(frozen=True)
class EditOffsetMap:
    """Reusable original-to-candidate boundary map (including insert affinity).

    Interiors of replaced bytes have no surviving coordinate. At an insertion,
    left means before the new bytes, right means after them. Untouched ranges
    use right at their start and left at their end. U06 can reuse this map to
    carry later locators through already committed edits without line drift.
    """
    edits: tuple[BeaconEdit, ...]

    def map_offset(self, offset: int, *, bias: str = "left") -> int | None:
        delta = 0
        for edit in self.edits:
            a, b, size = edit.start_byte, edit.end_byte, len(edit.replacement)
            if offset < a:
                break
            if a == b == offset:
                return offset + delta + (size if bias == "right" else 0)
            if offset == a:
                return a + delta
            if a < offset < b:
                return None
            delta += size - (b - a)
        return offset + delta

    def map_range(self, start: int, end: int) -> tuple[int, int] | None:
        a = self.map_offset(start, bias="right")
        b = self.map_offset(end, bias="left")
        return (a, b) if a is not None and b is not None else None


# @beacon[
#   id=beacon-edits@source_line_edit,
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Turns a selected physical-row interval into one original-byte splice using snapshot offsets and local newline precedence; keeps BOM outside the edit and preserves unterminated EOF without normalizing unrelated rows.,
# ]
def source_line_edit(
    snapshot: SourceSnapshot, start: int, stop: int, rows: list[str], owner: int | str,
) -> BeaconEdit:
    """Serialize replacement physical rows using local endings, not splitlines.

    ``start:stop`` is a half-open physical-row interval supplied by the existing
    selector. Its byte boundaries come only from the snapshot. The BOM is never
    in that interval. An unterminated EOF stays unterminated; inserting after it
    adds the necessary separator before new content, not a global EOF repair.
    """
    endings = snapshot.line_endings
    local = endings[start:stop]
    neighbour = (*endings[start:start + 1], *endings[max(0, start - 1):start])
    ending = next((s for s in (*local, *neighbour, *endings) if s), "\n")
    a, b = snapshot.byte_offsets[start], snapshot.byte_offsets[stop]
    text = ending.join(rows)
    if rows:
        at_eof = b == len(snapshot.raw_bytes)
        terminated = bool(endings and endings[-1])
        if not at_eof or terminated:
            text += ending
        if start == stop == len(snapshot.physical_lines) and endings and not terminated:
            text = ending + text
    return BeaconEdit(a, b, text.encode("utf-8"), owner)


# @beacon[
#   id=beacon-edits@markdown_eof_edit_plan,
#   slice_labels=beacon-writeback,
#   kind=ast,
#   comment=Explicit generation-only EOF adapter: plans just the terminal blank-line/newline bytes from physical snapshot rows using local endings; preserves BOM and all internal source and delegates splicing proof and IO.,
# ]
def markdown_eof_edit_plan(snapshot: SourceSnapshot) -> BeaconEditPlan:
    """Plan the phase-7.5 EOF intent without reserializing internal Markdown.

    Keep the last nonblank row, including its trailing spaces, and exactly
    one local newline; an all-blank document becomes empty apart from its BOM.
    This explicit generation intent is not an ordinary beacon mutation.
    """
    last = len(snapshot.physical_lines) - 1
    while last >= 0 and not snapshot.physical_lines[last].strip():
        last -= 1
    if last < 0:
        start, replacement = snapshot.byte_offsets[0], b""
    else:
        start = snapshot.byte_offset(last + 1, len(snapshot.physical_lines[last]))
        endings = (*snapshot.line_endings[last:last + 1],
                   *snapshot.line_endings[max(0, last - 1):last], *snapshot.line_endings)
        replacement = next((value for value in endings if value), "\n").encode("utf-8")
    edits = () if snapshot.raw_bytes[start:] == replacement else (
        BeaconEdit(start, len(snapshot.raw_bytes), replacement, "document"),
    )
    return BeaconEditPlan(snapshot, "markdown", edits, permitted_structural_effects=("whole_document",))


def _scan_edit_snapshot(snapshot: SourceSnapshot, language: str) -> list[dict]:
    return scan_beacon_structure(snapshot, language, evidence=source_lexical_evidence(snapshot, language))


def _metadata_range(record: dict) -> tuple[int, int]:
    ranges = record.get("write_ranges")
    return (ranges[0][0], ranges[-1][1]) if ranges else (record["opener_range"][0], record["read_end"])


def _without_metadata(raw: bytes, interval: tuple[int, int], records: list[dict], extra_ranges=()) -> bytes:
    a, b = interval
    pieces, cursor = [], a
    for left, right in sorted([*(_metadata_range(r) for r in records), *extra_ranges]):
        left, right = max(a, left), min(b, right)
        if right <= cursor or left >= b:
            continue
        pieces.append(raw[cursor:max(cursor, left)])
        cursor = max(cursor, right)
    pieces.append(raw[cursor:b])
    return b"".join(pieces)


def _following_source(snapshot: SourceSnapshot, record: dict, records: list[dict]) -> tuple[int, int] | None:
    """Physical attachment witness, without another AST selector or grammar.

    Skip metadata and ASCII separators only. The next surviving source byte
    must remain the same byte at its offset-mapped position. Decorators and
    ordinary comments are source too; no private comment/heading policy here.
    Callers retain their symbol-selection policy and intended target witness.
    """
    cursor = _metadata_range(record)[1]
    raw = snapshot.raw_bytes
    end = record["section"].get("end_byte", snapshot.byte_offsets[record["section"]["end_line"]])
    ranges = sorted(_metadata_range(r) for r in records)
    while cursor < end:
        containing = next((b for a, b in ranges if a <= cursor < b), None)
        if containing is not None:
            cursor = containing
        elif raw[cursor] in b" \t\r\n":
            cursor += 1
        else:
            return cursor, cursor + 1
    return None


def _proof_failure(reason: str, expected, actual) -> dict:
    return {"success": False, "error_kind": "plan_mismatch", "error": reason,
            "evidence": {"expected": expected, "actual": actual}}


# @beacon[
#   id=beacon-edits@verify_beacon_edit_plan,
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=The one pure pre-commit proof: scanner-owned ranges or authorized slots; exact untouched bytes and offset-mapped neighbour identity/attachment and span coverage; intended target state; damaged-neighbour improvements reported without a diagnostic gate.,
# ]
def verify_beacon_edit_plan(
    plan: BeaconEditPlan, *, scanner: Callable | None = None,
) -> dict:
    """Return the candidate and reusable offset map, or local expected/actual evidence.

    No file read, write, diagnostic-count comparison or syntax-validity gate.
    Untouched complete blocks must retain bytes, fields, section and coverage;
    incomplete neighbours may improve their interpretation without losing their
    observed identity. Selected repairs are verified through intended states.
    """
    raw = plan.snapshot.raw_bytes
    edits = tuple(sorted(plan.edits, key=lambda e: (e.start_byte, e.end_byte)))
    previous = None
    for edit in edits:
        if not 0 <= edit.start_byte <= edit.end_byte <= len(raw):
            return _proof_failure("edit range outside snapshot", [0, len(raw)], [edit.start_byte, edit.end_byte])
        if previous and (edit.start_byte < previous.end_byte or edit.start_byte == previous.start_byte):
            return _proof_failure("overlapping edits", "nonoverlapping byte intervals", [previous.start_byte, edit.start_byte])
        if plan.snapshot.had_bom and edit.start_byte < 3:
            return _proof_failure("edit includes file BOM", "BOM at byte zero untouched", edit.start_byte)
        previous = edit
    scan = scanner or _scan_edit_snapshot
    before = scan(plan.snapshot, plan.language)
    owners = {r["opener_range"][0]: r for r in before}
    document = "whole_document" in plan.permitted_structural_effects
    for edit in edits:
        if document and edit.owner == "document":
            continue
        record = owners.get(edit.owner)
        allowed = []
        if record and edit.owner in plan.target_witness:
            allowed = list(record.get("write_ranges") or record.get("field_ranges") or ())
        owned = any(a <= edit.start_byte <= edit.end_byte <= b for a, b in allowed)
        inserted = edit.start_byte == edit.end_byte and edit.start_byte in plan.insertion_slots
        if not owned and not inserted:
            return _proof_failure("edit crosses unowned source bytes", allowed or list(plan.insertion_slots),
                                  [edit.start_byte, edit.end_byte, edit.owner])
    candidate = raw
    for edit in reversed(edits):
        candidate = candidate[:edit.start_byte] + edit.replacement + candidate[edit.end_byte:]
    offsets = EditOffsetMap(edits)
    cursor, new_cursor = 0, 0
    for edit in edits:
        count = edit.start_byte - cursor
        if raw[cursor:edit.start_byte] != candidate[new_cursor:new_cursor + count]:
            return _proof_failure("untouched bytes differ", [cursor, edit.start_byte], [new_cursor, new_cursor + count])
        cursor = edit.end_byte
        new_cursor += count + len(edit.replacement)
    if raw[cursor:] != candidate[new_cursor:]:
        return _proof_failure("untouched tail differs", raw[cursor:].hex(), candidate[new_cursor:].hex())
    try:
        after_snapshot = source_snapshot_from_bytes(candidate, plan.snapshot.canonical_path)
    except UnicodeDecodeError as error:
        return _proof_failure("candidate is not UTF-8 source", "valid UTF-8 replacement",
                              {"start": error.start, "end": error.end, "reason": error.reason})
    after = scan(after_snapshot, plan.language)
    effects = []
    if not document:
        replacement_ranges = []
        for edit in edits:
            start = offsets.map_offset(edit.start_byte, bias="left")
            replacement_ranges.append((start, start + len(edit.replacement)))
            introduced_source = _without_metadata(candidate, (start, start + len(edit.replacement)), after)
            if introduced_source.strip(b" \t\r\n"):
                return _proof_failure("replacement introduces non-metadata source", "metadata or separator whitespace", introduced_source.hex())
        by_position = {r["opener_range"][0]: r for r in after}
        for record in before:
            owner = record["opener_range"][0]
            if owner in plan.target_witness:
                continue
            mapped = offsets.map_offset(owner, bias="right")
            current = by_position.get(mapped)
            interval = _metadata_range(record)
            mapped_range = offsets.map_range(*interval)
            if not mapped_range or raw[interval[0]:interval[1]] != candidate[mapped_range[0]:mapped_range[1]]:
                return _proof_failure("untouched block bytes changed", interval, mapped_range)
            if current is None:
                return _proof_failure("untouched block lost its identity", record["fields"], None)
            valid = record["complete"] and not record["id_conflicting"] and record["literal_certainty"] != "uncertain"
            if valid:
                expected = (record["marker"], record["fields"], record["section"]["type"], record["complete"])
                actual = (current["marker"], current["fields"], current["section"]["type"], current["complete"])
                if expected != actual:
                    return _proof_failure("untouched block identity changed", expected, actual)
                if record["pair_state"] in {"closed", "matched"} and current["pair_state"] != record["pair_state"]:
                    return _proof_failure("untouched block lost its span attachment", record["pair_state"], current["pair_state"])
                if record["marker"] == "close" and record["pair_state"] == "matched":
                    opening = next(r for r in before if r["line"] == record["opener_line"] and r["marker"] == "open")
                    attached = next(r for r in after if r["line"] == current["opener_line"] and r["marker"] == "open")
                    old_owner, new_owner = opening["opener_range"][0], attached["opener_range"][0]
                    mapped_owner = offsets.map_offset(old_owner, bias="right")
                    selected_replacement = any(
                        e.owner == old_owner and offsets.map_offset(e.start_byte, bias="left") <= new_owner <
                        offsets.map_offset(e.start_byte, bias="left") + len(e.replacement) for e in edits
                    )
                    if mapped_owner != new_owner and not selected_replacement:
                        return _proof_failure("untouched close attached to a different opener", mapped_owner, new_owner)
                if record["marker"] == "open" and record["fields"].get("kind") == "ast":
                    old_anchor = _following_source(plan.snapshot, record, before)
                    expected_anchor = offsets.map_range(*old_anchor) if old_anchor is not None else None
                    actual_anchor = _following_source(after_snapshot, current, after)
                    if expected_anchor != actual_anchor:
                        return _proof_failure("untouched block physical attachment changed", expected_anchor, actual_anchor)
                # Metadata is not span body: deleting an enclosed beacon is not
                # a change to the outer span's covered source.
                if record.get("inner_range") is not None:
                    old_body = _without_metadata(raw, record["inner_range"], before)
                    new_body = (_without_metadata(candidate, current["inner_range"], after, replacement_ranges)
                                if current.get("inner_range") is not None else None)
                    if old_body != new_body:
                        return _proof_failure("untouched span coverage changed", old_body.hex(), None if new_body is None else new_body.hex())
            else:
                for key in ("id", "kind"):
                    if record["fields"].get(key) != current["fields"].get(key):
                        return _proof_failure("damaged neighbour lost its identity", record["fields"], current["fields"])
            # Its metadata has not moved relative to surviving source. Include
            # changed structural readings, never a diagnostic-count veto.
            if (record["complete"], record["pair_state"], record["terminated_by"]) != (
                current["complete"], current["pair_state"], current["terminated_by"]
            ):
                effects.append({"owner": owner, "effect": "neighbour_interpretation_changed",
                                "before": {k: record[k] for k in ("complete", "pair_state", "terminated_by")},
                                "after": {k: current[k] for k in ("complete", "pair_state", "terminated_by")}})
        for intent in plan.intended_state:
            owner = intent["owner"]
            owned_edits = [e for e in edits if e.owner == owner]
            regions = []
            for edit in owned_edits:
                start = offsets.map_offset(edit.start_byte, bias="left")
                regions.append((start, start + len(edit.replacement)))
            matches = [r for r in after if any(a <= r["opener_range"][0] < b for a, b in regions)
                       and r["marker"] == intent.get("marker", "open")]
            if owner in owners:
                mapped = offsets.map_offset(owner, bias="right")
                remaining = by_position.get(mapped)
                opener_replaced = any(e.start_byte <= owner < e.end_byte for e in edits)
                if not opener_replaced and remaining is not None and remaining["marker"] == intent.get("marker", "open") and remaining not in matches:
                    # The same physical opener survives even when its id field
                    # is explicitly rewritten/deleted. Conversely, a neighbour
                    # at a deleted opener's boundary is not the old owner.
                    matches.append(remaining)
            expected_fields = intent.get("fields", {})
            if intent["operation"] == "absent":
                ok = not matches
            else:
                ok = len(matches) == 1 and all(matches[0]["fields"].get(k) == v for k, v in expected_fields.items())
                if ok and "complete" in intent:
                    ok = matches[0]["complete"] == intent["complete"]
            if not ok:
                return _proof_failure("intended target state not produced", intent, [r["fields"] for r in matches])
    return {"success": True, "candidate_bytes": candidate, "offset_map": offsets,
            "structural_effects": effects}


# @beacon[
#   id=beacon-edits@commit_beacon_edit_plan,
#   slice_labels=beacon-writeback,agent-kit-vendor,
#   kind=ast,
#   comment=Only source publication authority: pure proof then same-directory binary temp flush/fsync and mode copy; existing claimed-repair cancellation callback before the final existence/raw-hash comparison immediately followed by atomic replace; typed item-local failure and temp cleanup without retries or rollback.,
# ]
def commit_beacon_edit_plan(
    plan: BeaconEditPlan, *, scanner: Callable | None = None,
    before_final_hash: Callable[[], None] | None = None,
) -> dict:
    """Publish one verified candidate; never retry or roll back an earlier commit.

    A missing original compares against absence, not sha256(empty). Ordinary
    failures clean this invocation's temp. Hard-kill residue is left alone.
    No daemon lease is acquired here: existing callers own their claims/Q3.
    The optional pre-publication callback preserves an existing claimed
    repair's cancellation check after temp preparation, before the final hash.
    """
    proof = verify_beacon_edit_plan(plan, scanner=scanner)
    if not proof["success"]:
        return proof
    candidate = proof["candidate_bytes"]
    target = os.path.realpath(plan.snapshot.canonical_path)
    before_hash = plan.source_sha256 if plan.source_exists else None
    after_hash = hashlib.sha256(candidate).hexdigest()
    result = {"success": False, "changed": False, "before_sha256": before_hash,
              "candidate_sha256": after_hash,
              "edited_ranges": [{"start_byte": e.start_byte, "end_byte": e.end_byte,
                                 "replacement_bytes": len(e.replacement), "owner": e.owner} for e in plan.edits],
              "structural_effects": proof["structural_effects"]}
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".beacon_atomic_", dir=os.path.dirname(target) or ".")
        with os.fdopen(fd, "wb") as stream:
            stream.write(candidate)
            stream.flush()
            os.fsync(stream.fileno())
        if plan.source_exists:
            try:
                shutil.copymode(target, tmp_path)
            except FileNotFoundError:
                if os.path.exists(target):
                    raise
                # Deletion is a final-fingerprint mismatch, not permission to
                # create the file anew. The absence check below owns it.
        if before_final_hash is not None:
            before_final_hash()
        try:
            with open(target, "rb") as stream:
                actual_hash = hashlib.sha256(stream.read()).hexdigest()
            actual_exists = True
        except FileNotFoundError:
            actual_hash, actual_exists = None, False
        if actual_exists != plan.source_exists or actual_hash != before_hash:
            result.update(error_kind="source_changed_since_plan", error="source_changed_since_plan: final source fingerprint differs; zero bytes written",
                          evidence={"expected": {"exists": plan.source_exists, "sha256": before_hash},
                                    "actual": {"exists": actual_exists, "sha256": actual_hash}})
        else:
            os.replace(tmp_path, target)
            tmp_path = None
            result.update(success=True, changed=True, after_sha256=after_hash)
    except OSError as error:
        result.update(error_kind="disk_write_failed", error=f"disk_write_failed: {error}",
                      evidence={"expected": "whole atomic source publication", "actual": repr(error)})
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError as error:
                result["cleanup_error"] = repr(error)
    return result
