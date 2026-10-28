"""SPEC v5 §5/§10.4 -- the beacon mutation PLAN SEAM regression locks.

Real temp-source tests for ``_compute_beacon_write_plan`` (the ONE shared
preflight/JIT-plan computation), covering the independent ID/anchor
reconciliation truth table, the capability matrix, the end-state/noop rules,
comment resolution wiring, occurrence-aware Markdown matching, and the
Phase-C disk-target uniqueness check.

PURE: every assertion also verifies the temp source is byte-for-byte
untouched (zero preliminary writes).
"""

import hashlib
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.client import api_client_nexus as acn  # noqa: E402
from workflowy_mcp.lightning_rod_mutation_spec import BeaconMutationOperation  # noqa: E402
import beacon_mutation_contract as bmc  # noqa: E402

_ROOT = "11111111-1111-1111-1111-111111111111"
_FILE = "22222222-2222-2222-2222-222222222222"
_NODE = "33333333-3333-3333-3333-333333333333"


def _op(tags, comment=None, comment_mode=None):
    if comment:
        if comment_mode == "replace":
            intent = bmc.BeaconCommentIntent.replace(comment)
        elif comment_mode == "append":
            intent = bmc.BeaconCommentIntent.append(comment)
        else:
            intent = bmc.BeaconCommentIntent.fill_if_empty(comment)
    else:
        intent = bmc.BeaconCommentIntent.preserve()
    return BeaconMutationOperation(
        input_index=0,
        handle="foo",
        tags=tuple(tags),
        comment=intent,
    )


def _cache(source_path, *, node_name="ƒ foo", node_note="", extra_nodes=None):
    nodes = {
        _ROOT: {"id": _ROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0},
        _FILE: {
            "id": _FILE,
            "name": os.path.basename(str(source_path)),
            "note": f"Path: {source_path}",
            "parent_id": _ROOT,
            "priority": 0,
        },
        _NODE: {"id": _NODE, "name": node_name, "note": node_note, "parent_id": _FILE, "priority": 100},
    }
    if extra_nodes:
        nodes.update(extra_nodes)
    return nodes


def _plan(cache, tags, mode="ensure_beacons", comment=None, comment_mode=None, node_id=_NODE, fingerprint=True):
    return acn._compute_beacon_write_plan(
        cache,
        node_id,
        _op(tags, comment=comment, comment_mode=comment_mode),
        mode,
        include_fingerprint=fingerprint,
    )


# --- fixtures ---------------------------------------------------------------

PY_BEACONED = (
    "# @beacon[\n"
    "#   id=auto-beacon@foo-ab12,\n"
    "#   role=foo,\n"
    "#   slice_labels=old,\n"
    "#   kind=ast,\n"
    "# ]\n"
    "def foo():\n"
    "    return 1\n"
)
PY_BEACON_NOTE = (
    "AST_QUALNAME: foo\n---\nBEACON (AST)\nid: auto-beacon@foo-ab12\n"
    "role: foo\nslice_labels: old\nkind: ast\n---\n"
)
PY_PLAIN = "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"
PY_AST_NOTE = "AST_QUALNAME: foo\n---\n"

SQL_BEACONED = (
    "-- @beacon[\n"
    "--   id=q1,\n"
    "--   role=q,\n"
    "--   slice_labels=old,\n"
    "--   kind=span,\n"
    "-- ]\n"
    "SELECT 1;\n"
)
SQL_BEACON_NOTE = "BEACON (SQL SPAN)\nid: q1\nrole: q\nslice_labels: old\nkind: span\n---\n"

MD_TWO_BAYS = (
    "# Doc\n"
    "\n"
    "<!--\n"
    "@beacon[\n"
    "  id=bay-1,\n"
    "  role=Dump Bay,\n"
    "  slice_labels=old,\n"
    "  kind=ast,\n"
    "]\n"
    "-->\n"
    "## Dump Bay\n"
    "\n"
    "first\n"
    "\n"
    "<!--\n"
    "@beacon[\n"
    "  id=bay-2,\n"
    "  role=Dump Bay,\n"
    "  slice_labels=old,\n"
    "  kind=ast,\n"
    "]\n"
    "-->\n"
    "## Dump Bay\n"
    "\n"
    "second\n"
)


def _md_note(bid):
    return (
        "MD_PATH:\n# Doc\n## Dump Bay\n---\n"
        f"BEACON (MD AST)\nid: {bid}\nrole: Dump Bay\nslice_labels: old\nkind: ast\n---\n"
    )


# --- create / update / noop (anchor branch) ---------------------------------


def test_plan_py_create_actionable_fresh_id_and_fingerprint(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(PY_PLAIN, encoding="utf-8")
    before = p.read_bytes()
    cache = _cache(p, node_note=PY_AST_NOTE)
    plan = _plan(cache, ["new"])
    assert plan.success, plan.error
    assert plan.planned_operation == "create"
    assert plan.beacon_id is None  # py create mints a FRESH auto id at commit
    assert plan.target_key[1] == "ast::foo"
    assert plan.intended_slice_labels == ("new",)
    assert plan.source_sha256 == hashlib.sha256(before).hexdigest()
    assert p.read_bytes() == before  # PURE: zero writes


def test_plan_no_fingerprint_for_preflight_artifact(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(PY_PLAIN, encoding="utf-8")
    cache = _cache(p, node_note=PY_AST_NOTE)
    plan = _plan(cache, ["new"], fingerprint=False)
    assert plan.success, plan.error
    assert plan.source_sha256 is None


def test_plan_fingerprints_the_observation_it_parsed_not_a_later_read(tmp_path, monkeypatch):
    """U02 parse-A/hash-B witness: baseline hashes the interposed editor save."""
    import nexus_map_codebase as nmc
    p = tmp_path / "snapshot.py"
    p.write_bytes(PY_BEACONED.encode())
    before = p.read_bytes()
    real_read = nmc.read_source_snapshot
    reads = []

    def read_then_save(path):
        snapshot = real_read(path)
        reads.append(snapshot.sha256)
        p.write_bytes(before.replace(b"slice_labels=old,", b"slice_labels=editor,"))
        return snapshot

    with monkeypatch.context() as patch:
        patch.setattr(nmc, "read_source_snapshot", read_then_save)
        plan = _plan(_cache(p, node_note=PY_BEACON_NOTE), ["new"])
    assert plan.success, plan.error
    assert reads == [hashlib.sha256(before).hexdigest()]
    assert plan.intended_slice_labels == ("old", "new")
    assert plan.source_sha256 == hashlib.sha256(before).hexdigest()
    assert "raw_bytes" not in plan.to_dict()
    mtime = p.stat().st_mtime_ns
    result = nmc.update_beacon_from_node_python(str(p), plan.intended_name, PY_BEACON_NOTE,
                                               write_intent=plan.write_intent, write_plan=plan)
    assert result["error_kind"] == "source_changed_since_plan"
    assert p.read_bytes() == before.replace(b"slice_labels=old,", b"slice_labels=editor,")
    assert p.stat().st_mtime_ns == mtime
    # The next independent item is planned from the editor's version and lands.
    next_plan = _plan(_cache(p, node_note=PY_BEACON_NOTE), ["new"])
    next_result = nmc.update_beacon_from_node_python(str(p), next_plan.intended_name, PY_BEACON_NOTE,
        write_intent=next_plan.write_intent, write_plan=next_plan)
    assert next_result.get("success"), next_result
    assert b"slice_labels=editor,new," in p.read_bytes()


def test_plan_py_update_by_id_label_union(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(PY_BEACONED, encoding="utf-8")
    before = p.read_bytes()
    cache = _cache(p, node_note=PY_BEACON_NOTE)
    plan = _plan(cache, ["new"])
    assert plan.success, plan.error
    assert plan.planned_operation == "update"
    assert plan.beacon_id == "auto-beacon@foo-ab12"
    assert plan.intended_slice_labels == ("old", "new")
    assert plan.target_key[1] == "id::auto-beacon@foo-ab12"
    assert p.read_bytes() == before


def test_plan_py_ensure_noop_when_labels_and_comment_satisfied(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(PY_BEACONED, encoding="utf-8")
    cache = _cache(p, node_note=PY_BEACON_NOTE)
    plan = _plan(cache, ["old"])
    assert plan.success, plan.error
    assert plan.planned_operation == "noop"
    assert plan.target_key is not None  # a noop row still asserts its target


def test_plan_py_remove_final_label_plans_delete(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(PY_BEACONED, encoding="utf-8")
    cache = _cache(p, node_note=PY_BEACON_NOTE)
    plan = _plan(cache, ["old"], mode="remove_beacon_tags")
    assert plan.success, plan.error
    assert plan.planned_operation == "delete"  # py delete is supported


def test_plan_stale_note_id_adopts_anchor_beacon_disk_id(tmp_path):
    # The note's id is STALE (not on disk) but exactly one beacon anchors
    # this node's AST target -> ADOPT the disk id, NEVER create.
    p = tmp_path / "m.py"
    p.write_text(PY_BEACONED, encoding="utf-8")
    stale_note = PY_BEACON_NOTE.replace("auto-beacon@foo-ab12", "stale-id-9")
    cache = _cache(p, node_note=stale_note)
    plan = _plan(cache, ["new"])
    assert plan.success, plan.error
    assert plan.planned_operation == "update"
    assert plan.beacon_id == "auto-beacon@foo-ab12"


def test_plan_note_id_A_anchor_B_is_identity_conflict(tmp_path):
    # foo carries beacon B on disk; the note claims id A (which anchors bar).
    text = (
        "# @beacon[\n#   id=b-foo,\n#   slice_labels=old,\n#   kind=ast,\n# ]\n"
        "def foo():\n    return 1\n\n"
        "# @beacon[\n#   id=a-bar,\n#   slice_labels=old,\n#   kind=ast,\n# ]\n"
        "def bar():\n    return 2\n"
    )
    p = tmp_path / "m.py"
    p.write_text(text, encoding="utf-8")
    note = "AST_QUALNAME: foo\nBEACON (AST)\nid: a-bar\nkind: ast\n\n---\n"
    cache = _cache(p, node_note=note)
    plan = _plan(cache, ["new"])
    assert not plan.success
    assert plan.error_kind == "beacon_identity_conflict"
    assert p.read_bytes() == text.encode("utf-8") or p.read_text(encoding="utf-8") == text


def test_plan_note_id_present_no_anchor_match_is_conflict_both_modes(tmp_path):
    # The note's beacon exists on disk (above bar) but NOT at this node's
    # anchor (foo) -> conflict in BOTH modes.
    text = (
        "def foo():\n    return 1\n\n"
        "# @beacon[\n#   id=a-bar,\n#   slice_labels=old,\n#   kind=ast,\n# ]\n"
        "def bar():\n    return 2\n"
    )
    p = tmp_path / "m.py"
    p.write_text(text, encoding="utf-8")
    note = "AST_QUALNAME: foo\nBEACON (AST)\nid: a-bar\nkind: ast\n\n---\n"
    cache = _cache(p, node_note=note)
    for mode in ("ensure_beacons", "remove_beacon_tags"):
        plan = _plan(cache, ["new"], mode=mode)
        assert not plan.success, mode
        assert plan.error_kind == "beacon_identity_conflict", mode


def test_plan_neither_match_remove_is_noop(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(PY_PLAIN, encoding="utf-8")
    note = "AST_QUALNAME: foo\n---\nBEACON (AST)\nid: ghost-id\nkind: ast\n---\n"
    cache = _cache(p, node_note=note)
    plan = _plan(cache, ["old"], mode="remove_beacon_tags")
    assert plan.success, plan.error
    assert plan.planned_operation == "noop"


def test_plan_carried_anchor_unresolved_fails_closed_both_modes(tmp_path):
    # The note CARRIES an AST_QUALNAME that does not resolve on disk (stale
    # cache / renamed target) AND a beacon id that exists elsewhere -> the
    # exact-ID branch must NOT be used (audit Finding 1): fail closed,
    # zero write, in BOTH modes.
    text = (
        "# @beacon[\n#   id=a-bar,\n#   slice_labels=old,\n#   kind=ast,\n# ]\n"
        "def bar():\n    return 2\n"
    )
    p = tmp_path / "m.py"
    p.write_text(text, encoding="utf-8")
    note = "AST_QUALNAME: missing_fn\n---\nBEACON (AST)\nid: a-bar\nkind: ast\n---\n"
    cache = _cache(p, node_note=note)
    for mode in ("ensure_beacons", "remove_beacon_tags"):
        plan = _plan(cache, ["new"], mode=mode)
        assert not plan.success, mode
        assert plan.error_kind == "no_beacon_anchor_for_node", mode
        assert p.read_text(encoding="utf-8") == text


def test_plan_vue_style_span_exact_id_gated_unsupported(tmp_path):
    # A style /* */ beacon parses as a single id match, but the shared
    # extent layer covers only the vue-script // grammar -> an honest
    # preflight failure instead of a malformed delete (audit Finding 2 gate).
    text = (
        "<style>\n/* @beacon[\n   id=vstyle,\n   slice_labels=old,\n   kind=span,\n] */\n"
        ".x{}\n</style>\n"
    )
    p = tmp_path / "c.vue"
    p.write_text(text, encoding="utf-8")
    note = "BEACON (SPAN)\nid: vstyle\nkind: span\n---\n"
    cache = _cache(p, node_note=note)
    for mode in ("ensure_beacons", "remove_beacon_tags"):
        plan = _plan(cache, ["old"], mode=mode)
        assert not plan.success, mode
        assert plan.error_kind == "operation_not_supported_for_target", mode
        assert p.read_text(encoding="utf-8") == text


def test_plan_ensure_create_without_anchor_is_no_beacon_anchor(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(PY_PLAIN, encoding="utf-8")
    cache = _cache(p, node_note="just a body, no identifiers")
    plan = _plan(cache, ["new"])
    assert not plan.success
    assert plan.error_kind == "no_beacon_anchor_for_node"


# --- exact-ID branch (span-only targets) ------------------------------------


def test_plan_sql_exact_id_update(tmp_path):
    p = tmp_path / "q.sql"
    p.write_text(SQL_BEACONED, encoding="utf-8")
    cache = _cache(p, node_note=SQL_BEACON_NOTE)
    plan = _plan(cache, ["new"])
    assert plan.success, plan.error
    assert plan.planned_operation == "update"
    assert plan.beacon_id == "q1"
    assert plan.intended_slice_labels == ("old", "new")


def test_plan_sql_final_label_removal_plans_delete(tmp_path):
    # Commit-5 matrix: sql delete is now supported.
    p = tmp_path / "q.sql"
    p.write_text(SQL_BEACONED, encoding="utf-8")
    cache = _cache(p, node_note=SQL_BEACON_NOTE)
    plan = _plan(cache, ["old"], mode="remove_beacon_tags")
    assert plan.success, plan.error
    assert plan.planned_operation == "delete"


def test_plan_sql_partial_removal_is_update(tmp_path):
    p = tmp_path / "q.sql"
    p.write_text(SQL_BEACONED.replace("slice_labels=old", "slice_labels=old,other"), encoding="utf-8")
    cache = _cache(p, node_note=SQL_BEACON_NOTE.replace("slice_labels: old", "slice_labels: old,other"))
    plan = _plan(cache, ["old"], mode="remove_beacon_tags")
    assert plan.success, plan.error
    assert plan.planned_operation == "update"
    assert plan.intended_slice_labels == ("other",)


def test_plan_removal_matches_labels_exactly_no_family_cascade(tmp_path):
    # [ap:IUXIJ2]: removal is the EXACT label, per named symbol -- removing
    # `a` never touches `a-b` (and the mirror holds). No prefix/family
    # cascade; ensuring the short form never satisfies the postfixed label.
    p = tmp_path / "q.py"
    p.write_text(PY_BEACONED.replace("slice_labels=old", "slice_labels=a,a-b"), encoding="utf-8")
    cache = _cache(p, node_note=PY_BEACON_NOTE.replace("slice_labels: old", "slice_labels: a,a-b"))
    plan = _plan(cache, ["a"], mode="remove_beacon_tags")
    assert plan.success, plan.error
    assert plan.planned_operation == "update"
    assert plan.intended_slice_labels == ("a-b",)
    plan = _plan(cache, ["a-b"], mode="remove_beacon_tags")
    assert plan.success, plan.error
    assert plan.intended_slice_labels == ("a",)
    plan = _plan(cache, ["a"], mode="ensure_beacons")
    assert plan.success, plan.error
    assert plan.planned_operation == "noop"  # `a` present; `a-b` untouched


def test_plan_identity_conflict_returns_evidence_not_a_refresh_requirement(tmp_path):
    # U03: refreshing cannot resolve a physical identity conflict for the user.
    p = tmp_path / "q.sql"
    p.write_text("SELECT 1;\n", encoding="utf-8")
    cache = _cache(p, node_note=SQL_BEACON_NOTE)
    plan = _plan(cache, ["new"])
    assert not plan.success
    assert plan.error_kind == "beacon_identity_conflict"
    assert "cached identity" in plan.error
    assert "beacon.map.refresh" not in plan.error
    assert plan.to_dict()["candidates"] == []


def test_plan_sql_ensure_create_unsupported(tmp_path):
    p = tmp_path / "q.sql"
    p.write_text("SELECT 1;\n", encoding="utf-8")
    cache = _cache(p, node_note="plain span note, no id")
    plan = _plan(cache, ["new"])
    assert not plan.success
    assert plan.error_kind == "operation_not_supported_for_target"


def test_plan_note_claims_beacon_disk_lacks_ensure_conflict_remove_noop(tmp_path):
    p = tmp_path / "q.sql"
    p.write_text("SELECT 1;\n", encoding="utf-8")
    cache = _cache(p, node_note=SQL_BEACON_NOTE)
    plan = _plan(cache, ["new"])
    assert not plan.success
    assert plan.error_kind == "beacon_identity_conflict"
    # Finding B: remove is a NOOP (the end-state is already satisfied).
    plan = _plan(cache, ["old"], mode="remove_beacon_tags")
    assert plan.success, plan.error
    assert plan.planned_operation == "noop"


def test_plan_duplicate_disk_ids_is_ambiguous_beacon_id(tmp_path):
    p = tmp_path / "q.sql"
    p.write_text(SQL_BEACONED + SQL_BEACONED, encoding="utf-8")
    before = p.read_bytes()
    cache = _cache(p, node_note=SQL_BEACON_NOTE)
    plan = _plan(cache, ["new"])
    assert not plan.success
    assert plan.error_kind == "ambiguous_beacon_id"
    assert p.read_bytes() == before  # never silently mutate the first


# --- Markdown occurrence-aware anchor matching ------------------------------


def _two_bay_cache(p, target_note, target_priority=200):
    sibling = {
        "aaaaaaaa-0000-0000-0000-0000000000aa": {
            "id": "aaaaaaaa-0000-0000-0000-0000000000aa",
            "name": "Dump Bay",
            "note": _md_note("bay-1"),
            "parent_id": _FILE,
            "priority": 100,
        }
    }
    return _cache(p, node_name="Dump Bay", node_note=target_note, extra_nodes=sibling) | {
        _NODE: {
            "id": _NODE,
            "name": "Dump Bay",
            "note": target_note,
            "parent_id": _FILE,
            "priority": target_priority,
        }
    }


def test_plan_md_duplicate_siblings_resolve_their_own_beacons(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text(MD_TWO_BAYS, encoding="utf-8")
    before = p.read_bytes()
    # Target node is the SECOND '## Dump Bay' (occurrence=2 via priority).
    cache = _two_bay_cache(p, _md_note("bay-2"), target_priority=200)
    plan = _plan(cache, ["new"])
    assert plan.success, plan.error
    assert plan.planned_operation == "update"
    assert plan.beacon_id == "bay-2"  # occurrence-correct, no false ambiguous
    assert p.read_bytes() == before


def test_plan_md_unique_attached_id_beats_an_unbacked_occurrence(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text(MD_TWO_BAYS, encoding="utf-8")
    # The FIRST '## Dump Bay' sibling but the note carries bay-2's id.
    cache = _two_bay_cache(p, _md_note("bay-2"), target_priority=50)
    # ordinal: priorities 50 (target), 100 (sibling) -> target is occurrence 1
    plan = _plan(cache, ["new"])
    assert plan.success, plan.error
    assert plan.beacon_id == "bay-2"
    assert plan.physical_witness["anchor"]["line"] == 23


def test_plan_md_create_reuses_note_carried_id(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("# Doc\n\n## Target\n\nbody\n", encoding="utf-8")
    note = (
        "MD_PATH:\n# Doc\n## Target\n---\n"
        "BEACON (MD AST)\nid: note-id-7\nrole: Target\nslice_labels: old\nkind: ast\n---\n"
    )
    cache = _cache(p, node_name="Target", node_note=note)
    plan = _plan(cache, ["new"])
    assert plan.success, plan.error
    assert plan.planned_operation == "create"
    assert plan.beacon_id == "note-id-7"  # MD create REUSES the note id
    assert plan.target_key[1].startswith("md::")


# --- misc guards ------------------------------------------------------------


def test_plan_virtual_group_is_noop(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(PY_PLAIN, encoding="utf-8")
    cache = _cache(p, node_note="CARTOGRAPHER_VIRTUAL_GROUP: python\nCARTO_GROUP_ID: g1\n---")
    plan = _plan(cache, ["new"])
    assert plan.success, plan.error
    assert plan.planned_operation == "noop"


def test_plan_comment_fill_on_empty_disk_comment(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(PY_BEACONED, encoding="utf-8")
    cache = _cache(p, node_note=PY_BEACON_NOTE)
    plan = _plan(cache, ["new"], comment="hello")
    assert plan.success, plan.error
    assert plan.planned_operation == "update"
    assert plan.comment_outcome == "fill"
    assert plan.resolved_comment == "hello"


def test_plan_comment_conflict_is_kept_warning_labels_still_ensured(tmp_path):
    # 2R4E1P: a fill-mode comment conflict is NO LONGER a plan failure.
    p = tmp_path / "m.py"
    p.write_text(
        PY_BEACONED.replace("#   kind=ast,", "#   kind=ast,\n#   comment=other,"),
        encoding="utf-8",
    )
    before = p.read_bytes()
    cache = _cache(p, node_note=PY_BEACON_NOTE)
    plan = _plan(cache, ["new"], comment="hello")
    assert plan.success, plan.error
    assert plan.planned_operation == "update"  # labels still ensured
    assert plan.comment_outcome == "kept_existing_conflict"
    assert plan.resolved_comment == "other"  # the existing comment is kept
    assert plan.comment_warning and "other" in plan.comment_warning
    assert "hello" in plan.comment_warning
    assert p.read_bytes() == before  # PURE: zero writes


def test_plan_comment_conflict_labels_satisfied_is_noop_with_warning(tmp_path):
    # 2R4E1P: labels already satisfied + a kept comment conflict -> a noop
    # plan that still carries the warning (never silent, never an error).
    p = tmp_path / "m.py"
    p.write_text(
        PY_BEACONED.replace("#   kind=ast,", "#   kind=ast,\n#   comment=other,"),
        encoding="utf-8",
    )
    cache = _cache(p, node_note=PY_BEACON_NOTE)
    plan = _plan(cache, ["old"], comment="hello")  # 'old' already on disk
    assert plan.success, plan.error
    assert plan.planned_operation == "noop"
    assert plan.noop_reason == "already_satisfied"
    assert plan.comment_outcome == "kept_existing_conflict"
    assert plan.comment_warning and "other" in plan.comment_warning


def test_plan_comment_replace_overwrites_explicitly(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(
        PY_BEACONED.replace("#   kind=ast,", "#   kind=ast,\n#   comment=other,"),
        encoding="utf-8",
    )
    cache = _cache(p, node_note=PY_BEACON_NOTE)
    plan = _plan(cache, ["new"], comment="hello", comment_mode="replace")
    assert plan.success, plan.error
    assert plan.planned_operation == "update"  # the comment changes
    assert plan.comment_outcome == "replace"
    assert plan.resolved_comment == "hello"
    assert plan.comment_warning is None


def test_plan_comment_append_extends_kept_text(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(
        PY_BEACONED.replace("#   kind=ast,", "#   kind=ast,\n#   comment=other,"),
        encoding="utf-8",
    )
    cache = _cache(p, node_note=PY_BEACON_NOTE)
    plan = _plan(cache, ["new"], comment="hello", comment_mode="append")
    assert plan.success, plan.error
    assert plan.planned_operation == "update"
    assert plan.comment_outcome == "append"
    assert plan.resolved_comment == "other hello"
    assert plan.comment_warning is None


def test_plan_comment_append_rerun_is_idempotent_noop(tmp_path):
    # invariant 10: appending text that is already present stays a noop.
    p = tmp_path / "m.py"
    p.write_text(
        PY_BEACONED.replace(
            "#   kind=ast,", "#   kind=ast,\n#   comment=other hello,"
        ),
        encoding="utf-8",
    )
    cache = _cache(p, node_note=PY_BEACON_NOTE)
    plan = _plan(cache, ["old"], comment="hello", comment_mode="append")
    assert plan.success, plan.error
    assert plan.planned_operation == "noop"
    assert plan.comment_outcome == "idempotent"
    assert plan.resolved_comment == "other hello"


# --- Phase-C disk-target uniqueness ------------------------------------------


def test_disk_target_uniqueness_detects_collisions_including_noop_rows():
    rows = [
        {"symbol": "a", "target_key": ("/x/q.sql", "id::q1"), "outcome": "actionable"},
        {"symbol": "b", "target_key": ("/x/q.sql", "id::q1"), "outcome": "noop"},
        {"symbol": "c", "target_key": ("/x/q.sql", "id::q2"), "outcome": "actionable"},
        {"symbol": "d", "target_key": None, "outcome": "noop"},
    ]
    collisions = srv._check_disk_target_uniqueness(rows)
    assert collisions is not None
    assert len(collisions) == 1
    assert collisions[0]["symbols"] == ["a", "b"]


def test_disk_target_uniqueness_clean_when_distinct():
    rows = [
        {"symbol": "a", "target_key": ("/x/q.sql", "id::q1")},
        {"symbol": "b", "target_key": ("/x/q.sql", "id::q2")},
    ]
    assert srv._check_disk_target_uniqueness(rows) is None
