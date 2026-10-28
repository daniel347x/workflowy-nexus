"""SPEC v5 §6/§10.3/§10.5/§10.7 -- the beacon mutation COMMIT SEAM locks.

Real temp-source tests for the plan-branch execution engine
(``_execute_beacon_write_plan`` via the 7 language helpers) and
``commit_beacon_write_plan``: CAS + plan_mismatch verification, §5 step 10
metadata sourcing (labels/comment from the plan; everything else from the
fresh disk parse), span-delete @beacon-close cleanup, the both-or-neither
assert, marker removal, and the legacy byte-for-byte requirement.
"""

import asyncio
import hashlib
import os
import sys
from types import SimpleNamespace
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.client import api_client_nexus as acn  # noqa: E402
from workflowy_mcp.lightning_rod_mutation_spec import BeaconWritePlan  # noqa: E402
import beacon_mutation_contract as bmc  # noqa: E402
import nexus_map_codebase as nmc  # noqa: E402

_ROOT = "11111111-1111-1111-1111-111111111111"
_FILE = "22222222-2222-2222-2222-222222222222"
_NODE = "33333333-3333-3333-3333-333333333333"


def _intent(comment=None):
    return bmc.BeaconWriteIntent(
        comment=(
            bmc.BeaconCommentIntent.fill_if_empty(comment)
            if comment
            else bmc.BeaconCommentIntent.preserve()
        ),
        source="lightning_rod",
    )


def _plan_for(path, *, op, beacon_id=None, labels=(), resolved_comment=None, comment=None, occurrence=None, sha=None):
    if sha is None:
        sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
    language = acn._BEACON_LANGUAGE_BY_EXT[path.suffix]
    snapshot = nmc.read_source_snapshot(path)
    target = {"language": language, "operation": "delta_ensure", "note_beacon_id": beacon_id}
    if op == "create":
        target.update(md_path=["# Doc", "## Target"] if language == "markdown" else None,
                      ast_qualname="foo" if language != "markdown" else None, occurrence=occurrence)
    selection = nmc.select_beacon_block_for_node(snapshot, nmc._beacon_selection_structures(snapshot, language), target)
    return BeaconWritePlan(
        success=True,
        planned_operation=op,
        node_id=_NODE,
        target_key=None,
        cached_name="foo",
        intended_name="foo " + " ".join(f"#{t}" for t in labels),
        write_intent=_intent(comment),
        occurrence=occurrence,
        file_node_id=_FILE,
        source_path=str(path),
        language=None,
        beacon_id=beacon_id,
        note_comment=None,
        disk_comment=None,
        comment_outcome=None,
        resolved_comment=resolved_comment,
        intended_slice_labels=tuple(labels),
        source_sha256=sha,
        selection_target=target,
        physical_witness=selection["physical_witness"],
    )


PY_SPAN_BEACONED = (
    "# @beacon[\n"
    "#   id=span-1,\n"
    "#   role=myrole,\n"
    "#   slice_labels=old,\n"
    "#   kind=span,\n"
    "#   start_snippet=\"def foo(\",\n"
    "#   end_snippet=\"return 1\",\n"
    "#   show_span=false,\n"
    "# ]\n"
    "def foo():\n"
    "    return 1\n"
    "# @beacon-close[\n"
    "#   id=span-1,\n"
    "# ]\n"
)


# --- plan-branch verification (CAS / plan_mismatch) -------------------------


def test_cas_mismatch_is_source_changed_since_plan_zero_write(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(PY_SPAN_BEACONED, encoding="utf-8")
    before = p.read_bytes()
    plan = _plan_for(p, op="update", beacon_id="span-1", labels=("new",), sha="0" * 64)
    res = nmc.update_beacon_from_node_python(
        str(p), "foo #new", "note", write_intent=plan.write_intent, write_plan=plan
    )
    assert res.get("success") is not True
    assert res.get("error_kind") == "source_changed_since_plan"
    assert p.read_bytes() == before


def test_planned_target_missing_is_plan_mismatch(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(PY_SPAN_BEACONED, encoding="utf-8")
    before = p.read_bytes()
    plan = _plan_for(p, op="update", beacon_id="ghost-id", labels=("new",))
    res = nmc.update_beacon_from_node_python(
        str(p), "foo #new", "note", write_intent=plan.write_intent, write_plan=plan
    )
    assert res.get("success") is not True
    assert res.get("error_kind") == "plan_mismatch"
    assert p.read_bytes() == before


@pytest.mark.parametrize("component", ["section", "opener_start_byte", "block_hash", "anchor"])
def test_executor_revalidates_every_physical_witness_component(tmp_path, component):
    from dataclasses import replace
    p = tmp_path / "physical.py"
    p.write_bytes(PY_SPAN_BEACONED.encode() + PY_SPAN_BEACONED.replace("span-1", "safe").encode())
    before, mtime = p.read_bytes(), p.stat().st_mtime_ns
    plan = _plan_for(p, op="update", beacon_id="span-1", labels=("new",))
    witness = dict(plan.physical_witness)
    witness[component] = {"moved": True} if component in {"section", "anchor"} else -1 if component == "opener_start_byte" else "0" * 64
    bad = replace(plan, physical_witness=witness)
    result = nmc.update_beacon_from_node_python(str(p), "foo", "note", write_intent=bad.write_intent, write_plan=bad)
    assert result.get("error_kind") == "plan_mismatch", result
    assert (p.read_bytes(), p.stat().st_mtime_ns) == (before, mtime)
    good = _plan_for(p, op="update", beacon_id="safe", labels=("new",))
    result = nmc.update_beacon_from_node_python(str(p), "foo", "note", write_intent=good.write_intent, write_plan=good)
    assert result.get("success"), result
    split = before.index(b"# @beacon[", 1)
    assert p.read_bytes() == before[:split] + before[split:].replace(b"slice_labels=old,", b"slice_labels=new,")


def test_executor_consumes_physical_copy_not_id_cardinality(tmp_path):
    """Existing executor witness: 004d1ba ignores the physical operand and refuses two ids."""
    rows = ["# @beacon[", "#   id=copy,", "#   slice_labels=old,", "#   kind=ast,", "# ]"]
    p = tmp_path / "copies.py"
    p.write_bytes(("\n".join(rows + ["def alpha():", "    pass", ""] + rows + ["def beta():", "    pass"]) + "\n").encode())
    before = p.read_bytes()
    snapshot = nmc.read_source_snapshot(p)
    record = nmc._beacon_edit_records(snapshot, "python")[1]
    line = list(snapshot.physical_lines).index("def beta():") + 1
    owner = record["opener_range"][0]
    witness = {"section": dict(record.get("section") or {}), "opener_start_byte": owner,
               "block_hash": hashlib.sha256(before[owner:record["read_end"]]).hexdigest(),
               "anchor": {"line": line, "outline_line": line, "ast_qualname": "beta", "md_path": None,
                          "header": "def beta():", "decorator_ranges": []}}
    intent = _intent()
    plan = SimpleNamespace(success=True, planned_operation="update", source_path=str(p), source_sha256=snapshot.sha256,
        intended_slice_labels=("old", "new"), resolved_comment=None, beacon_id="copy", physical_witness=witness,
        selection_target={"language": "python", "ast_qualname": "beta", "note_beacon_id": "copy", "operation": "delta_ensure"})
    result = nmc.update_beacon_from_node_python(str(p), "beta", "AST_QUALNAME: beta\n", write_intent=intent, write_plan=plan)
    assert result.get("success"), result
    boundary = snapshot.byte_offsets[record["line_extent"][0]]
    assert p.read_bytes() == before[:boundary] + before[boundary:].replace(b"slice_labels=old,", b"slice_labels=old,new,")


def test_plan_commit_never_reaches_global_sanitizer(tmp_path, monkeypatch):
    p = tmp_path / "local.py"
    p.write_bytes(PY_SPAN_BEACONED.encode() + b'TEXT = "a\x1bb"\r\n\r\n')
    before = p.read_bytes()
    plan = _plan_for(p, op="update", beacon_id="span-1", labels=("new",))
    def forbidden(*_args, **_kwargs):
        raise AssertionError("ordinary plan edit reached global serializer")
    monkeypatch.setattr(nmc, "_write_markdown_lines_sanitized", forbidden)
    result = nmc.update_beacon_from_node_python(str(p), "foo", "note",
                                               write_intent=plan.write_intent, write_plan=plan)
    assert result.get("success"), result
    assert p.read_bytes() == before.replace(b"slice_labels=old,", b"slice_labels=new,")


def test_executor_io_failure_has_kind_and_next_item_commits(tmp_path, monkeypatch):
    p = tmp_path / "failed.py"
    p.write_bytes(PY_SPAN_BEACONED.encode())
    before, mtime = p.read_bytes(), p.stat().st_mtime_ns
    plan = _plan_for(p, op="update", beacon_id="span-1", labels=("new",))
    def fail(*_args, **_kwargs):
        raise PermissionError("injected replace failure")
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail)
        result = nmc.update_beacon_from_node_python(str(p), "foo", "note",
            write_intent=plan.write_intent, write_plan=plan)
    assert result["error_kind"] == "disk_write_failed"
    assert (p.read_bytes(), p.stat().st_mtime_ns) == (before, mtime)
    good = nmc.update_beacon_from_node_python(str(p), "foo", "note",
        write_intent=plan.write_intent, write_plan=plan)
    assert good.get("success"), good


def test_both_or_neither_asserted(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("def foo():\n    return 1\n", encoding="utf-8")
    try:
        nmc.update_beacon_from_node_python(
            str(p), "foo #new", "note", write_intent=_intent(), write_plan=None
        )
    except AssertionError:
        pass
    else:  # pragma: no cover
        raise AssertionError("intent-without-plan was not rejected")


# --- §5 step 10 metadata sourcing --------------------------------------------


def test_py_planned_update_labels_come_from_plan_not_name(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(PY_SPAN_BEACONED, encoding="utf-8")
    plan = _plan_for(p, op="update", beacon_id="span-1", labels=("old", "new"))
    # The legacy name arg carries DIFFERENT tags; the plan branch must ignore them.
    res = nmc.update_beacon_from_node_python(
        str(p), "Something #else", "note", write_intent=plan.write_intent, write_plan=plan
    )
    assert res.get("operation") == "updated_beacon", res
    content = p.read_text(encoding="utf-8")
    assert "slice_labels=old,new," in content
    assert "#else" not in content


def test_labels_only_update_preserves_role_kind_showspan_snippets(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(PY_SPAN_BEACONED, encoding="utf-8")
    plan = _plan_for(p, op="update", beacon_id="span-1", labels=("old", "new"))
    res = nmc.update_beacon_from_node_python(
        str(p), "foo #new", "note", write_intent=plan.write_intent, write_plan=plan
    )
    assert res.get("operation") == "updated_beacon", res
    content = p.read_text(encoding="utf-8")
    assert "role=myrole," in content
    assert "kind=span," in content
    assert 'start_snippet="def foo(",' in content
    assert 'end_snippet="return 1",' in content
    assert "show_span=false," in content
    assert "slice_labels=old,new," in content


def test_py_planned_delete_removes_matching_beacon_close(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(PY_SPAN_BEACONED, encoding="utf-8")
    plan = _plan_for(p, op="delete", beacon_id="span-1")
    res = nmc.update_beacon_from_node_python(
        str(p), "foo", "note", write_intent=plan.write_intent, write_plan=plan
    )
    assert res.get("operation") == "deleted_beacon", res
    content = p.read_text(encoding="utf-8")
    assert "@beacon[" not in content
    assert "@beacon-close[" not in content  # no orphaned closer
    assert "def foo():" in content


def test_py_planned_create_with_comment(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("# header\ndef foo():\n    return 1\n", encoding="utf-8")
    note = "AST_QUALNAME: foo\n---\n"
    plan = _plan_for(p, op="create", labels=("new",), resolved_comment="hello", comment="hello")
    res = nmc.update_beacon_from_node_python(
        str(p), "foo #new", note, write_intent=plan.write_intent, write_plan=plan
    )
    assert res.get("operation") == "created_beacon", res
    content = p.read_text(encoding="utf-8")
    assert "@beacon[" in content
    assert "slice_labels=new," in content
    assert "comment=hello," in content
    assert content.splitlines()[0] == "# header"  # anchored above the def, not top-of-file


def test_fill_defense_existing_comment_present_no_write(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(
        PY_SPAN_BEACONED.replace("#   show_span=false,", "#   show_span=false,\n#   comment=other,"),
        encoding="utf-8",
    )
    before = p.read_bytes()
    plan = _plan_for(p, op="update", beacon_id="span-1", labels=("old", "new"), comment="req")
    res = nmc.update_beacon_from_node_python(
        str(p), "foo #new", "note", write_intent=plan.write_intent, write_plan=plan
    )
    assert res.get("success") is not True
    assert res.get("error") == "existing_comment_present"
    assert res.get("existing_comment") == "other"
    assert p.read_bytes() == before


def test_sql_planned_update(tmp_path):
    p = tmp_path / "q.sql"
    p.write_text(
        "-- @beacon[\n--   id=q1,\n--   role=q,\n--   slice_labels=old,\n--   kind=span,\n-- ]\nSELECT 1;\n",
        encoding="utf-8",
    )
    plan = _plan_for(p, op="update", beacon_id="q1", labels=("old", "new"))
    res = nmc.update_beacon_from_node_sql(
        str(p), "q #new", "note", write_intent=plan.write_intent, write_plan=plan
    )
    assert res.get("operation") == "updated_beacon", res
    content = p.read_text(encoding="utf-8")
    assert "--   slice_labels=old,new," in content
    assert "--   role=q," in content


def test_js_planned_update(tmp_path):
    p = tmp_path / "m.js"
    p.write_text(
        "// @beacon[\n//   id=j1,\n//   role=foo,\n//   slice_labels=old,\n//   kind=ast,\n// ]\n"
        "function foo() { return 1; }\n",
        encoding="utf-8",
    )
    plan = _plan_for(p, op="update", beacon_id="j1", labels=("old", "new"))
    res = nmc.update_beacon_from_node_js_ts(
        str(p), "foo #new", "note", write_intent=plan.write_intent, write_plan=plan
    )
    assert res.get("operation") == "updated_beacon", res
    content = p.read_text(encoding="utf-8")
    assert "//   slice_labels=old,new," in content
    assert "//   role=foo," in content


def test_md_planned_update_and_create_with_occurrence(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text(
        "# Doc\n\n## Target\n\nbody\n",
        encoding="utf-8",
    )
    note = "MD_PATH:\n# Doc\n## Target\n---\n"
    plan = _plan_for(p, op="create", beacon_id="note-id-7", labels=("new",))
    res = nmc.update_beacon_from_node_markdown(
        str(p), "Target #new", note, occurrence=None, write_intent=plan.write_intent, write_plan=plan
    )
    assert res.get("operation") == "created_beacon", res
    content = p.read_text(encoding="utf-8")
    assert "id=note-id-7," in content  # MD create reuses the plan's id
    assert "  slice_labels=new," in content
    assert content.index("id=note-id-7,") < content.index("## Target")


def test_md_planned_delete_removes_close_block_wrapper(tmp_path):
    # HTML-style span + @beacon-close: the delete must remove the WHOLE
    # <!-- ... --> wrappers for BOTH blocks -- no orphan comment shell
    # (audit Finding 4).
    p = tmp_path / "doc.md"
    p.write_text(
        "<!--\n@beacon[\n  id=mspan,\n  slice_labels=old,\n  kind=span,\n]\n-->\n"
        "# H\n\nbody\n"
        "<!--\n@beacon-close[\n  id=mspan,\n]\n-->\n",
        encoding="utf-8",
    )
    plan = _plan_for(p, op="delete", beacon_id="mspan")
    res = nmc.update_beacon_from_node_markdown(
        str(p), "H", "note", write_intent=plan.write_intent, write_plan=plan
    )
    assert res.get("operation") == "deleted_beacon", res
    content = p.read_text(encoding="utf-8")
    assert "@beacon" not in content
    assert "<!--" not in content
    assert "-->" not in content


# --- legacy byte-for-byte (write_intent=None / write_plan=None) --------------


def test_legacy_create_writes_no_comment_after_marker_deletion(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("def foo():\n    return 1\n", encoding="utf-8")
    # A note that would once have carried the synthetic marker must NOT be
    # scavenged: legacy Case-2 creation writes NO comment now.
    note = "AST_QUALNAME: foo\n---\nLIGHTNING_ROD_BEACON_COMMENT: stale\n"
    res = nmc.update_beacon_from_node_python(str(p), "foo #new", note)
    assert res.get("operation") == "created_beacon", res
    content = p.read_text(encoding="utf-8")
    assert "comment=" not in content


# --- §10.7 marker removal ---------------------------------------------------


def test_marker_machinery_removed_everywhere():
    assert not hasattr(nmc, "_LIGHTNING_ROD_BEACON_COMMENT_NOTE_KEY")
    assert not hasattr(nmc, "_extract_lightning_rod_beacon_comment_marker")
    assert not hasattr(srv, "_LIGHTNING_ROD_BEACON_COMMENT_NOTE_KEY")
    assert not hasattr(srv, "_append_lightning_rod_beacon_comment_marker")


# --- commit_beacon_write_plan end-to-end -------------------------------------


def _cache_for(path, node_note):
    return {
        _ROOT: {"id": _ROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0},
        _FILE: {
            "id": _FILE,
            "name": os.path.basename(str(path)),
            "note": f"Path: {path}",
            "parent_id": _ROOT,
            "priority": 0,
        },
        _NODE: {"id": _NODE, "name": "ƒ foo", "note": node_note, "parent_id": _FILE, "priority": 100},
    }


def test_commit_beacon_write_plan_end_to_end(tmp_path, monkeypatch):
    p = tmp_path / "m.py"
    p.write_text("def foo():\n    return 1\n", encoding="utf-8")
    note = "AST_QUALNAME: foo\n---\n"
    cache = _cache_for(p, note)

    async def _fake_export(client, node_id=None, use_cache=True, force_refresh=False):
        return {"nodes": list(cache.values())}

    monkeypatch.setattr(acn, "export_nodes_impl", _fake_export)
    client = acn.WorkFlowyClientNexus.__new__(acn.WorkFlowyClientNexus)

    op = SimpleNamespace(
        handle="foo",
        tags=("new",),
        comment=bmc.BeaconCommentIntent.preserve(),
    )
    plan = acn._compute_beacon_write_plan(cache, _NODE, op, "ensure_beacons", include_fingerprint=True)
    assert plan.success, plan.error
    assert plan.planned_operation == "create"

    res = asyncio.run(client.commit_beacon_write_plan(plan=plan, note=note))
    assert res.get("success"), res
    assert res.get("operation") == "created_beacon"
    assert res.get("file_node_id") == _FILE
    assert res.get("language") == "python"
    assert res.get("disk_changed") is True
    content = p.read_text(encoding="utf-8")
    assert "slice_labels=new," in content
    # no auto-refresh / cache patching on the mutation path
    assert res["auto_refresh"]["reason"] == "mutation_path_no_refresh"


def test_commit_beacon_write_plan_path_divergence_is_plan_mismatch(tmp_path, monkeypatch):
    p = tmp_path / "m.py"
    p.write_text("def foo():\n    return 1\n", encoding="utf-8")
    before = p.read_bytes()
    note = "AST_QUALNAME: foo\n---\n"
    cache = _cache_for(p, note)

    async def _fake_export(client, node_id=None, use_cache=True, force_refresh=False):
        return {"nodes": list(cache.values())}

    monkeypatch.setattr(acn, "export_nodes_impl", _fake_export)
    client = acn.WorkFlowyClientNexus.__new__(acn.WorkFlowyClientNexus)

    op = SimpleNamespace(
        handle="foo",
        tags=("new",),
        comment=bmc.BeaconCommentIntent.preserve(),
    )
    plan = acn._compute_beacon_write_plan(cache, _NODE, op, "ensure_beacons", include_fingerprint=True)
    assert plan.success, plan.error
    # Forge a divergent source path on the plan.
    bad_plan = BeaconWritePlan(
        **{**plan.to_dict(), "write_intent": plan.write_intent, "source_path": str(tmp_path / "other.py")}
    )
    res = asyncio.run(client.commit_beacon_write_plan(plan=bad_plan, note=note))
    assert res.get("success") is False
    assert res.get("error_kind") == "plan_mismatch"
    assert res.get("disk_changed") is False
    assert p.read_bytes() == before
