"""U03 physical attachment witnesses; selection/intent cases extend this file.

Fixtures are quoted physical rows. No source is evaluated and no live service
is contacted. Mapper and compatibility writer view must describe one target.
"""
from __future__ import annotations

import copy
import re

import pytest

import nexus_map_codebase as nmc
from beacon_selection_layouts import RECON_LAYOUTS
from beacon_mutation_contract import BeaconCommentIntent
from workflowy_mcp.client import api_client_nexus as acn
from workflowy_mcp.lightning_rod_mutation_spec import BeaconMutationOperation


def block(prefix="#", bid="b@target", kind="ast"):
    rows = [f"{prefix} @beacon[", f"{prefix}   id={bid},", f"{prefix}   slice_labels=old,"]
    if kind is not None:
        rows.append(f"{prefix}   kind={kind},")
    return rows + [f"{prefix} ]"]


PY = block()
JS = block("//")
MD = ["<!--", "@beacon[", "  id=b@target,", "  slice_labels=old,", "  kind=ast,", "]", "-->"]
CASES = [
    ("py-direct", "python", PY + ["def target():", "    pass"], {1: "target"}),
    ("py-comment", "python", PY + ["# ordinary comment", "def target():", "    pass"], {1: "target"}),
    ("py-decorator-comment", "python", PY + ["@deco", "# ordinary", "def target():", "    pass"], {1: "target"}),
    ("py-comment-triple-string", "python", PY + ['# """ not a string', "def target():", "    pass"], {1: "target"}),
    ("py-code-marker", "python", PY + ['print("@beacon")', "def target():", "    pass"], {}),
    ("py-constant-marker", "python", PY + ['MARK = "@beacon"', "def target():", "    pass"], {1: "MARK"}),
    ("py-bare-string", "python", PY + ['"""Not a comment."""', "def target():", "    pass"], {}),
    ("py-embedded-decorator", "python", ["@deco("] + PY + ["    value=True,", ")", "def target():", "    pass"], {}),
    ("py-explicit-span", "python", block(kind="span") + ["def target():", "    pass"], {}),
    ("py-implicit", "python", block(kind=None) + ["# ordinary", "def target():", "    pass"], {1: "target"}),
    ("py-stack", "python", PY + block(bid="b@second") + ["def target():", "    pass"], {1: "target", 6: "target"}),
    ("py-close-gap", "python", PY + ["# @beacon-close[", "# id=other,", "# ]", "def target():", "    pass"], {1: "target"}),
    ("js-comment", "js_ts", JS + ["// ordinary", "function target() {}"], {1: "target"}),
    ("js-real-block-state", "js_ts", JS + ["/*", "not star-prefixed comment text", "*/", "function target() {}"], {1: "target"}),
    ("js-jsdoc", "js_ts", JS + ["/**", " * comment", " */", "function target() {}"], {1: "target"}),
    ("js-block-wrapper", "js_ts", ["/* @beacon[", " id=b@target,", " slice_labels=old,", " kind=ast,", "] */", "function target() {}"], {1: "target"}),
    ("js-code-after-comment", "js_ts", JS + ["/* comment */ const other = 1;", "function target() {}"], {1: "other"}),
    ("js-generator", "js_ts", ["class C {"] + JS + ["*target() {}", "}"], {2: "C.target"}),
    ("ts-owned-decorator", "js_ts", JS + ["@Component()", "export class Target {}"], {1: "Target"}),
    ("ts-owned-multiline", "js_ts", JS + ["@Component({", "  value: true", "})", "// ordinary", "export class Target {}"], {1: "Target"}),
    ("ts-class-includes-decorator", "js_ts", JS + ["@Component()", "class Target {}"], {1: "Target"}),
    ("ts-between-decorators", "js_ts", ["@one"] + JS + ["@two", "export class Target {}"], {2: "Target"}),
    ("ts-method-decorator", "js_ts", ["class C {"] + JS + ["@dec", "target() {}", "}"], {2: "C.target"}),
    ("ts-embedded-decorator", "js_ts", ["@Component({"] + JS + ["  value: true", "})", "export class Target {}"], {}),
    ("js-explicit-span", "js_ts", block("//", kind="span") + ["function target() {}"], {}),
    ("js-implicit", "js_ts", block("//", kind=None) + ["function target() {}"], {1: "target"}),
    ("md-prose-explicit", "markdown", MD + ["", "ordinary prose", "", "# Target"], {2: "# Target"}),
    ("md-adjacent-implicit", "markdown", [r for r in MD if "kind=" not in r] + ["", "<!-- ordinary -->", "", "# Target"], {2: "# Target"}),
    ("md-prose-implicit", "markdown", [r for r in MD if "kind=" not in r] + ["", "ordinary prose", "", "# Target"], {}),
    ("md-explicit-span", "markdown", [r.replace("kind=ast", "kind=span") for r in MD] + ["", "# Target"], {}),
    ("md-strictly-forward", "markdown", ["# Earlier", ""] + MD + ["", "prose", "", "# Target"], {4: "# Target"}),
    ("md-stack", "markdown", MD + [r.replace("b@target", "b@second") for r in MD] + ["", "# Target"], {2: "# Target", 9: "# Target"}),
]


def outline(snapshot, language):
    fn = {"python": nmc.parse_file_outline, "js_ts": nmc.parse_js_ts_outline,
          "markdown": nmc.parse_markdown_structure, "vue": nmc.parse_vue_outline}[language]
    return fn(snapshot.canonical_path, snapshot=snapshot)


def identity(anchor):
    return anchor["ast_qualname"] or anchor["header"]


@pytest.mark.parametrize("case,language,rows,expected", CASES, ids=[c[0] for c in CASES])
def test_attachment_index_and_mapper_writer_agree(tmp_path, case, language, rows, expected):
    suffix = {"python": ".py", "js_ts": ".ts", "markdown": ".md"}[language]
    path = tmp_path / (case + suffix)
    before = ("\r\n".join(rows) + "\r\n").encode("utf-8")
    path.write_bytes(before)
    mtime = path.stat().st_mtime_ns
    snapshot = nmc.read_source_snapshot(path)
    tree = outline(snapshot, language)
    frozen = copy.deepcopy(tree)
    records = nmc._beacon_edit_records(snapshot, language)
    inputs = {"language": language, "records": records}
    index = nmc.beacon_attachment_index(snapshot, tree, inputs)
    actual = {b["record"]["line"]: identity(b["anchor"]) for b in index["blocks"] if b["anchor"]}
    assert actual == expected
    assert tree == frozen  # The index does not decorate or mutate its inputs.
    assert inputs == {"language": language, "records": records}
    lines, beacons = nmc._parse_snapshot_beacons_for_language(snapshot, language)
    for anchor in index["anchors"]:
        matches = nmc._find_anchor_matched_beacons(language, lines, beacons, anchor["outline_line"] - 1)
        expected_lines = {line for line, target in expected.items() if target == identity(anchor)}
        assert {b["comment_line"] for b, _a, _b in matches} == expected_lines
        note_ids = set(re.findall(r"^id: (.+)$", anchor["node"].get("note", ""), re.M))
        assert note_ids == {b["id"] for b, _a, _b in matches}
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


# These are intent/selection expectations, not a generated mapping golden.
# Unlisted targets have an independently attached current beacon and update.
RECON_OUTCOMES = {
    "py_p00_base.py": {"g": "create"},
    "py_p07_string_between.py": {"f": "create"},
    "py_p08a_atbeacon_call.py": {"f": "create"},
    "py_p08b_atbeacon_const.py": {"f": "create"},
    "py_p09_code_between.py": {"f": "create"},
    "py_p12_reordered.py": {"f": "beacon_identity_conflict"},
    "py_p15_span_directly_above.py": {"f": "create"},
    "py_p17_property.py": {"C.x": "create"},
    "py_p18_overload.py": {"f": "create"},
    "py_p20_redefined.py": {"f": "ambiguous_beacon_anchor"},
    "py_p23_block_in_deco_expr.py": {"f": "create"},
    "py_p24_eof.py": {"f": "create"},
    "py_p28_cf19_no_id_code.py": {"f": "create"},
    "js_j00_base.js": {"g": "create"},
    "js_j05_span_above.js": {"f": "create"},
    "js_j07_getset.js": {"C.x": "create"},
    "js_j11_code_between.js": {"f": "create"},
    "md_m00_base.md": {"Section B": "create"},
    "md_m04_ast_no_heading_after.md": {"Section A": "create"},
    "md_m05_implicit_prose.md": {"Section A": "create"},
    "md_m06_heading_renamed.md": {"Section Alpha": "no_beacon_anchor_for_node"},
    "md_m07_heading_moved.md": {"Section A": "beacon_identity_conflict"},
    "md_m08_dup_headings.md": {"Notes": "ambiguous_beacon_anchor"},
    "md_m10_fence_hash.md": {"Section A": "create"},
    "tw_py_cond_else.py": {"f#2": "ambiguous_beacon_anchor"},
    "tw_py_getter_block_setter_tag.py": {"C.x#2": "create"},
    "tw_py_redef_second_tag.py": {"f#2": "ambiguous_beacon_anchor"},
    "tw_js_get_block_set_tag.js": {"C.x#2": "create"},
    "case2_adopt_span.py": {"ƒ f()": "create"},
}


@pytest.mark.parametrize("fixture,language,specs,rows", RECON_LAYOUTS, ids=[c[0] for c in RECON_LAYOUTS])
@pytest.mark.parametrize("mode", ["ensure_beacons", "remove_beacon_tags"])
def test_recon_selection_matrix_on_plan_path(tmp_path, fixture, language, specs, rows, mode):
    """All 66 exact inputs, both deltas, physical locality and a safe batch neighbour.

    Field preservation/legacy/rehydrate assertions belong to the later concern
    migrations; this matrix pins selection and U02 publication only.
    """
    path = tmp_path / fixture
    safe_rows = (block(bid="safe@u03") + ["def u03_safe():", "    pass"] if language == "python" else
                 block("//", bid="safe@u03") + ["function u03_safe() {}"] if language == "js_ts" else
                 [r.replace("b@target", "safe@u03") for r in MD] + ["# U03 Safe"])
    pristine = ("\n".join(rows + [""] + safe_rows) + "\n").encode()
    for spec in specs:
        if isinstance(spec, dict) and "remove_targets" in spec:
            continue
        path.write_bytes(pristine)
        cache = cache_for_nodes(path, language)
        target_spec = spec["t"] if isinstance(spec, dict) else spec
        match = re.match(r"^(.*?)(?:#(\d+))?$", target_spec)
        desired, ordinal = match[1], int(match[2] or 1)
        lookup = desired.removeprefix("ƒ ").removesuffix("()")
        matches = []
        for key, node in cache.items():
            if key == "file":
                continue
            md_path = nmc._extract_md_path_from_note(node["note"]) or []
            if (nmc._extract_ast_qualname_from_note(node["note"]) == lookup or
                    md_path and md_path[-1].lstrip("# ") == desired):
                matches.append(key)
        assert len(matches) >= ordinal, (fixture, target_spec, cache)
        key = matches[ordinal - 1]
        if isinstance(spec, dict):
            cache[key]["note"] = spec["note"]
        if fixture == "case2_adopt.py":
            cache[key]["note"] = "AST_QUALNAME: f\n---\n"
        plan = delta_plan(cache, key, mode, ("new",) if mode == "ensure_beacons" else ("lab", "glab", "other"))
        expected = RECON_OUTCOMES.get(fixture, {}).get(target_spec, "update")
        if fixture == 'md_m08_dup_headings.md' and target_spec == 'Notes':
            expected = 'create'  # plan caller carries the heading occurrence
        if mode == "remove_beacon_tags":
            expected = {"update": "delete", "create": "noop"}.get(expected, expected)
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        if expected not in {"update", "delete", "create", "noop"}:
            assert not plan.success and plan.error_kind == expected, (fixture, target_spec, plan)
            assert plan.candidates
            assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
            safe = next(k for k, n in cache.items() if k != "file" and "safe@u03" in n["note"])
            good = delta_plan(cache, safe)
            assert commit_plan(path, good, cache[safe]["note"]).get("success")
            boundary = before.index(("# @beacon[" if language == "python" else "// @beacon[" if language == "js_ts" else "<!--").encode(), len(("\n".join(rows) + "\n").encode()))
            assert path.read_bytes()[:boundary] == before[:boundary]
            continue
        assert plan.success and plan.planned_operation == expected, (fixture, target_spec, plan)
        if expected == "noop":
            assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
            continue
        result = commit_plan(path, plan, cache[key]["note"])
        assert result.get("success"), (fixture, target_spec, result)
        after = path.read_bytes()
        from test_beacon_byte_edits import assert_outside_edits_identical
        assert_outside_edits_identical(before, after, result)
        if expected != "create":
            snapshot = nmc.source_snapshot_from_bytes(before, str(path))
            record = next(r for r in nmc._beacon_edit_records(snapshot, language)
                          if r["opener_range"][0] == plan.physical_witness["opener_start_byte"])
            assert all(snapshot.byte_offsets[record["line_extent"][0]] <= e["start_byte"] <= e["end_byte"] <= snapshot.byte_offsets[record["line_extent"][1] + 1]
                       for e in result["edited_ranges"])


@pytest.mark.parametrize("language,prefix,header", [("python", "#", ["def target():", "    pass"]),
                                                   ("js_ts", "//", ["function target() {}"] )])
def test_paired_implicit_span_never_promotes(language, prefix, header):
    rows = block(prefix, kind=None) + header + [f"{prefix} @beacon-close[", f"{prefix} id=b@target,", f"{prefix} ]"]
    path = "source.py" if language == "python" else "source.ts"
    snapshot = nmc.source_snapshot_from_bytes(("\n".join(rows) + "\n").encode(), path)
    tree = outline(snapshot, language)
    index = nmc.beacon_attachment_index(snapshot, tree, {"language": language, "records": nmc._beacon_edit_records(snapshot, language)})
    assert index["blocks"][0]["anchor"] is None
    assert index["blocks"][0]["effective_kind"] == "span"


def test_gap_evidence_keeps_prose_and_decorator_ownership():
    source = nmc.source_snapshot_from_bytes(("\n".join(MD + ["", "prose", "", "# Target"]) + "\n").encode(), "source.md")
    index = nmc.beacon_attachment_index(source, outline(source, "markdown"), {
        "language": "markdown", "records": nmc._beacon_edit_records(source, "markdown")})
    assert index["blocks"][0]["gap"]["prose_lines"] == [9]
    source = nmc.source_snapshot_from_bytes(("\n".join(PY + ["@deco(", "    value=True", ")", "def target():", "    pass"]) + "\n").encode(), "source.py")
    index = nmc.beacon_attachment_index(source, outline(source, "python"), {
        "language": "python", "records": nmc._beacon_edit_records(source, "python")})
    assert index["blocks"][0]["gap"]["decorator_lines"] == [6, 7, 8]


def test_mapper_and_writer_production_comment_gap_witness(tmp_path):
    """Fails at 004d1ba's competing writer rule, not just a missing new API."""
    path = tmp_path / "source.py"
    path.write_bytes(("\n".join(PY + ["# ordinary", "def target():", "    pass"]) + "\n").encode())
    before = path.read_bytes()
    node = next(n for n in nmc.parse_file_outline(str(path)) if n.get("ast_qualname") == "target")
    cache = {"file": {"id": "file", "name": path.name, "note": f"Path: {path}"},
             "target": {"id": "target", "name": node["name"], "note": node["note"], "parent_id": "file"}}
    op = BeaconMutationOperation(input_index=0, handle="target", tags=("new",), comment=BeaconCommentIntent.preserve())
    plan = acn._compute_beacon_write_plan(cache, "target", op, "ensure_beacons", include_fingerprint=True)
    assert plan.success and plan.planned_operation == "update", (plan.error_kind, plan.error)
    result = nmc.update_beacon_from_node_python(str(path), plan.intended_name, node["note"],
                                               write_intent=plan.write_intent, write_plan=plan)
    assert result.get("success"), result
    assert path.read_bytes() == before.replace(b"slice_labels=old,", b"slice_labels=old,new,")


def test_blocked_gap_does_not_hide_safe_neighbour(tmp_path):
    rows = PY + ['MARK = "@beacon"', "def target():", "    pass", ""] + block(bid="b@safe") + ["# ordinary", "def safe():", "    pass"]
    path = tmp_path / "source.py"
    path.write_bytes(("\n".join(rows) + "\n").encode())
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    tree = nmc.parse_file_outline(str(path))
    target = next(n for n in tree if n.get("ast_qualname") == "target")
    assert not nmc._extract_beacon_id_from_note(target["note"])
    lines, beacons = nmc._parse_snapshot_beacons_for_language(nmc.read_source_snapshot(path), "python")
    assert nmc._find_anchor_matched_beacons("python", lines, beacons, target["orig_lineno_start_unused"] - 1) == []
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    safe = next(n for n in tree if n.get("ast_qualname") == "safe")
    cache = {"file": {"id": "file", "note": f"Path: {path}"},
             "safe": {"id": "safe", "name": safe["name"], "note": safe["note"], "parent_id": "file"}}
    op = BeaconMutationOperation(input_index=0, handle="safe", tags=("new",), comment=BeaconCommentIntent.preserve())
    plan = acn._compute_beacon_write_plan(cache, "safe", op, "ensure_beacons", include_fingerprint=True)
    assert plan.success, plan.error
    result = nmc.update_beacon_from_node_python(str(path), plan.intended_name, safe["note"],
                                               write_intent=plan.write_intent, write_plan=plan)
    assert result.get("success"), result
    start = before.index(b"# @beacon[", len(b"# @beacon["))
    assert path.read_bytes() == before[:start] + before[start:].replace(b"slice_labels=old,", b"slice_labels=old,new,")


def test_js_generator_and_exported_decorator_production_mapper_witness():
    for rows, qualname in [(["class C {"] + JS + ["*target() {}", "}"], "C.target"),
                           (JS + ["@Component()", "export class Target {}"], "Target")]:
        snapshot = nmc.source_snapshot_from_bytes(("\n".join(rows) + "\n").encode(), "source.ts")
        node = next(n for n in nmc._iter_ast_outline_nodes(nmc.parse_js_ts_outline("source.ts", snapshot=snapshot))
                    if n["ast_qualname"] == qualname)
        assert nmc._extract_beacon_id_from_note(node["note"]) == "b@target"


def test_missing_wrapper_lexical_tail_cannot_cross_code_to_safe_neighbour(tmp_path):
    rows = ["/* @beacon[", " * id=bad,", " * slice_labels=bad,", " * kind=ast,", " * ]",
            "const UNRELATED = 1;", "/* @beacon[", " * id=good,", " * slice_labels=old,",
            " * kind=ast,", " * ] */", "function safe() {}"]
    path = tmp_path / "source.ts"
    path.write_bytes(("\n".join(rows) + "\n").encode())
    before = path.read_bytes()
    snapshot = nmc.read_source_snapshot(path)
    tree = nmc.parse_js_ts_outline(str(path), snapshot=snapshot)
    records = nmc._beacon_edit_records(snapshot, "js_ts")
    index = nmc.beacon_attachment_index(snapshot, tree, {"language": "js_ts", "records": records})
    bad, good = index["blocks"]
    assert not bad["record"]["complete"]
    assert bad["anchor"] is None and bad["next_code_line"] == 6
    assert good["anchor"]["ast_qualname"] == "safe"
    node = good["anchor"]["node"]
    assert re.findall(r"^id: (.+)$", node["note"], re.M) == ["good"]
    cache = {"file": {"id": "file", "note": f"Path: {path}"},
             "safe": {"id": "safe", "name": node["name"], "note": node["note"], "parent_id": "file"}}
    operation = BeaconMutationOperation(input_index=0, handle="safe", tags=("new",), comment=BeaconCommentIntent.preserve())
    plan = acn._compute_beacon_write_plan(cache, "safe", operation, "ensure_beacons", include_fingerprint=True)
    assert plan.success, plan.error
    result = nmc.update_beacon_from_node_js_ts(str(path), plan.intended_name, node["note"],
                                              write_intent=plan.write_intent, write_plan=plan)
    assert result.get("success"), result
    split = before.index(b"/* @beacon[", 1)
    assert path.read_bytes()[:split] == before[:split]
    assert b"slice_labels=old,new," in path.read_bytes()[split:]


def cache_for_nodes(path, language):
    snapshot = nmc.read_source_snapshot(path)
    cache = {"file": {"id": "file", "note": f"Path: {path}"}}
    tree = outline(snapshot, language)
    if isinstance(tree, dict):
        tree = tree.get("children", [])
    nodes = []
    def walk(children):
        for node in children:
            if node.get("ast_qualname") or node.get("_md_heading_line"):
                nodes.append(node)
            walk(node.get("children") or [])
    walk(tree)
    nodes.sort(key=lambda n: n.get("_md_heading_line") or n.get("orig_lineno_start_unused"))
    for i, node in enumerate(nodes):
        cache[str(i)] = {"id": str(i), "name": node["name"], "note": node["note"],
                         "parent_id": "file", "priority": i * 100}
    return cache


def delta_plan(cache, node_id, mode="ensure_beacons", tags=("new",)):
    op = BeaconMutationOperation(input_index=0, handle=node_id, tags=tags, comment=BeaconCommentIntent.preserve())
    return acn._compute_beacon_write_plan(cache, node_id, op, mode, include_fingerprint=True)


def commit_plan(path, plan, note):
    helper = getattr(nmc, acn._BEACON_LANGUAGE_HELPER_NAME[plan.language])
    return helper(str(path), plan.intended_name, note, write_intent=plan.write_intent, write_plan=plan)


@pytest.mark.parametrize("language,suffix,rows", [
    ("python", ".py", block(bid="copy") + ["def alpha():", "    pass", ""] + block(bid="copy") + ["def beta():", "    pass"]),
    ("js_ts", ".ts", block("//", bid="copy") + ["function alpha() {}", ""] + block("//", bid="copy") + ["function beta() {}"]),
    ("markdown", ".md", [r.replace("b@target", "copy") for r in MD] + ["# Alpha", ""] + [r.replace("b@target", "copy") for r in MD] + ["# Beta"]),
    ("vue", ".vue", ["<script>"] + block("//", bid="copy") + ["function alpha() {}", ""] + block("//", bid="copy") + ["function beta() {}", "</script>"]),
])
@pytest.mark.parametrize("mode,tags", [("ensure_beacons", ("new",)), ("remove_beacon_tags", ("old",))])
def test_anchored_duplicate_plan_and_commit_changes_only_second(tmp_path, language, suffix, rows, mode, tags):
    """004d1ba refuses at pre-anchor cardinality; commit also reselects by count."""
    path = tmp_path / ("copies" + suffix)
    path.write_bytes(("\r\n".join(rows) + "\r\n").encode())
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    cache = cache_for_nodes(path, language)
    alpha, beta = ("0", "1")
    first, second = delta_plan(cache, alpha, mode, tags), delta_plan(cache, beta, mode, tags)
    assert first.success and second.success, (first.error, second.error)
    assert first.target_key != second.target_key
    assert second.physical_witness["opener_start_byte"] > first.physical_witness["opener_start_byte"]
    assert second.diagnostics[0]["type"] == "duplicate_id"
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    snapshot = nmc.read_source_snapshot(path)
    record = next(r for r in nmc._beacon_edit_records(snapshot, language)
                  if r["opener_range"][0] == second.physical_witness["opener_start_byte"])
    a, b = record["line_extent"]
    left, right = snapshot.byte_offsets[a], snapshot.byte_offsets[b + 1]
    result = commit_plan(path, second, cache[beta]["note"])
    assert result.get("success"), result
    after = path.read_bytes()
    assert after[:left] == before[:left]
    assert after.endswith(before[right:])
    if mode == "remove_beacon_tags":
        assert after == before[:left] + before[right:]
    else:
        assert after == before[:left] + before[left:right].replace(b"slice_labels=old,", b"slice_labels=old,new,") + before[right:]


@pytest.mark.parametrize("rows", [
    ["class C:", "    @property", "    def x(self):", "        return 1"] + ["    " + s for s in block(bid="setter")] + ["    @x.setter", "    def x(self, value):", "        pass"],
    ["if flag:", "    def f(a):", "        pass", "else:"] + ["    " + s for s in block(bid="else")] + ["    def f(b, c):", "        pass"],
    ["def f(a):", "    pass"] + block(bid="second") + ["def f(a, b):", "    pass"],
])
def test_identifiable_twin_writes_and_current_signature_witness_creates(tmp_path, rows):
    """Unique note id picks the attached twin; signature/count/order picks its bare sibling."""
    path = tmp_path / "twins.py"
    path.write_bytes(("\n".join(rows + [""] + block(bid="safe") + ["def safe():", "    pass"]) + "\n").encode())
    cache = cache_for_nodes(path, "python")
    twins = [key for key, n in cache.items() if key != "file" and nmc._extract_ast_qualname_from_note(n["note"]) in {"C.x", "f"}]
    bare, attached = twins
    before = path.read_bytes()
    plan = delta_plan(cache, attached)
    assert plan.success and plan.planned_operation == "update", plan.error
    assert commit_plan(path, plan, cache[attached]["note"]).get("success")
    assert path.read_bytes() == before.replace(b"slice_labels=old,", b"slice_labels=old,new,", 1)
    create = delta_plan(cache, bare)
    assert create.success and create.planned_operation == "create", create.error
    old = path.read_bytes()
    assert commit_plan(path, create, cache[bare]["note"]).get("success")
    assert old[old.index(b"# @beacon["):] in path.read_bytes()


def test_stale_twin_signature_order_returns_candidates_and_safe_neighbour_commits(tmp_path):
    rows = ["def f(a):", "    pass", "def f(b, c):", "    pass", ""] + block(bid="safe") + ["def safe():", "    pass"]
    path = tmp_path / "stale.py"
    path.write_bytes(("\n".join(rows) + "\n").encode())
    cache = cache_for_nodes(path, "python")
    path.write_bytes(("\n".join(rows[2:4] + rows[:2] + rows[4:]) + "\n").encode())
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    bad = delta_plan(cache, "0")
    assert not bad.success and bad.error_kind == "ambiguous_beacon_anchor"
    assert {c["line"] for c in bad.candidates if c.get("id") is None} == {1, 3}
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    good = delta_plan(cache, "2")
    assert commit_plan(path, good, cache["2"]["note"]).get("success")
    assert path.read_bytes() == before.replace(b"slice_labels=old,", b"slice_labels=old,new,")


def test_duplicate_id_twins_use_matching_source_signature_order(tmp_path):
    path = tmp_path / "twin_copies.py"
    indented = ["    " + row for row in block(bid="copy")]
    rows = ["class C:"] + indented + ["    @property", "    def x(self):", "        return 1", ""] + indented + ["    @x.setter", "    def x(self, value):", "        pass"]
    path.write_bytes(("\n".join(rows) + "\n").encode())
    before = path.read_bytes()
    cache = cache_for_nodes(path, "python")
    getter, setter = delta_plan(cache, "1"), delta_plan(cache, "2")
    assert getter.success and setter.success, (getter.error, setter.error)
    assert getter.target_key != setter.target_key
    assert setter.physical_witness["anchor"]["header"] == "    def x(self, value):"
    assert commit_plan(path, setter, cache["2"]["note"]).get("success")
    split = before.index(b"    # @beacon[", before.index(b"    # @beacon[") + 1)
    assert path.read_bytes() == before[:split] + before[split:].replace(b"slice_labels=old,", b"slice_labels=old,new,")


def test_ordinal_and_old_line_do_not_disambiguate_identical_twins(tmp_path):
    rows = ["def f():", "    return 1", "def f():", "    return 2", ""] + block(bid="safe") + ["def safe():", "    pass"]
    path = tmp_path / "identical.py"
    path.write_bytes(("\n".join(rows) + "\n").encode())
    snapshot = nmc.read_source_snapshot(path)
    target = {"language": "python", "ast_qualname": "f", "operation": "delta_ensure",
              "twin_witness": {"ordinal": 2, "line": 3}}
    selection = nmc.select_beacon_block_for_node(snapshot, nmc._beacon_selection_structures(snapshot, "python"), target)
    assert selection["status"] == "candidates"
    # A source-backed witness under this exact observation is sufficient.
    target["twin_witness"].update(ast_qualname="f", count=2, source_sha256=snapshot.sha256,
                                  source_headers=["def f():", "def f():"])
    selected = nmc.select_beacon_block_for_node(snapshot, nmc._beacon_selection_structures(snapshot, "python"), target)
    assert selected["status"] == "create" and selected["anchor"]["line"] == 3
    cache = cache_for_nodes(path, "python")
    assert commit_plan(path, delta_plan(cache, "2"), cache["2"]["note"]).get("success")


def test_pinned_markdown_twin_witness_belongs_to_its_heading_path(tmp_path):
    path = tmp_path / "md_twins.md"
    rows = ["# Doc", "", "## Notes", "", "one", "", "## Notes", "", "two", ""] + [r.replace("b@target", "safe") for r in MD] + ["# Safe"]
    path.write_bytes(("\n".join(rows) + "\n").encode())
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    snapshot = nmc.read_source_snapshot(path)
    structures = nmc._beacon_selection_structures(snapshot, "markdown")
    witness = {"source_sha256": snapshot.sha256, "md_path": ["# Other", "## Notes"],
               "count": 2, "ordinal": 2, "source_headers": ["## Notes", "## Notes"]}
    target = {"language": "markdown", "md_path": ["# Doc", "## Notes"], "operation": "delta_ensure", "twin_witness": witness}
    refused = nmc.select_beacon_block_for_node(snapshot, structures, target)
    assert refused["status"] == "candidates" and len(refused["anchor_candidates"]) == 2
    witness["md_path"] = target["md_path"]
    selected = nmc.select_beacon_block_for_node(snapshot, structures, target)
    assert selected["status"] == "create" and selected["anchor"]["line"] == 7
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, mtime)
    cache = cache_for_nodes(path, "markdown")
    safe = next(k for k, n in cache.items() if k != "file" and "id: safe" in n["note"])
    assert commit_plan(path, delta_plan(cache, safe), cache[safe]["note"]).get("success")


@pytest.mark.parametrize("terminator", [None, "# ],"])
def test_incomplete_claim_is_selected_not_absent_and_neighbour_is_writable(tmp_path, terminator):
    rows = ["# @beacon[", "# id=damaged,", "# slice_labels=old,", "# kind=ast,"]
    if terminator:
        rows.append(terminator)
    rows += ["def f():", "    pass", ""] + block(bid="safe") + ["def safe():", "    pass"]
    path = tmp_path / "incomplete.py"
    path.write_bytes(("\n".join(rows) + "\n").encode())
    cache = cache_for_nodes(path, "python")
    cache["0"]["note"] = "AST_QUALNAME: f\nBEACON (AST)\nid: damaged\nkind: ast\n---\n"
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    snapshot = nmc.read_source_snapshot(path)
    selection = nmc.select_beacon_block_for_node(snapshot, nmc._beacon_selection_structures(snapshot, "python"),
        {"language": "python", "ast_qualname": "f", "note_beacon_id": "damaged", "operation": "delta_ensure"})
    assert selection["status"] == "selected"
    plan = delta_plan(cache, "0")
    if terminator is None:
        assert not selection["block"]["record"]["complete"]
        assert selection["block"]["record"]["write_ranges"] is None
        # A known field range is owned even when the destructive tail is not.
        assert plan.success and plan.planned_operation == "update"
    else:
        # U01 already accepts canonical ],; x_no_later_bracket must update
        # that physical block, not create beside the old tuple adapter's miss.
        assert selection["block"]["record"]["complete"]
        assert plan.success and plan.planned_operation == "update"
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    assert commit_plan(path, plan, cache["0"]["note"]).get("success")
    before = before.replace(b"slice_labels=old,", b"slice_labels=old,new,", 1)
    good = delta_plan(cache, "1")
    assert commit_plan(path, good, cache["1"]["note"]).get("success")
    split = before.index(b"# @beacon[", 1)
    assert path.read_bytes() == before[:split] + before[split:].replace(b"slice_labels=old,", b"slice_labels=old,new,")


def test_unmapped_orphan_reports_candidates_but_does_not_block_create(tmp_path):
    rows = block(bid="orphan") + ['print("real code")', "def f():", "    pass"]
    path = tmp_path / "orphan.py"
    path.write_bytes(("\n".join(rows) + "\n").encode())
    before = path.read_bytes()
    cache = cache_for_nodes(path, "python")
    # Even a stale note naming the orphan cannot turn it into an attachment.
    cache["0"]["note"] += "\nBEACON (AST)\nid: orphan\nkind: ast\n"
    plan = delta_plan(cache, "0")
    assert plan.success and plan.planned_operation == "create", plan.error
    assert plan.diagnostics[0]["type"] == "detached_candidates"
    assert plan.diagnostics[0]["candidates"][0]["id"] == "orphan"
    assert commit_plan(path, plan, cache["0"]["note"]).get("success")
    split = before.index(b"def f")
    assert path.read_bytes().startswith(before[:split]) and path.read_bytes().endswith(before[split:])


def test_planner_returns_the_single_u02_edit_plan_without_io(tmp_path):
    rows = block() + ["def target():", "    pass"]
    path = tmp_path / "pure.py"
    path.write_bytes(("\r\n".join(rows) + "\r\n").encode())
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    snapshot = nmc.read_source_snapshot(path)
    selection = nmc.select_beacon_block_for_node(snapshot, nmc._beacon_selection_structures(snapshot, "python"),
        {"language": "python", "ast_qualname": "target", "note_beacon_id": "b@target", "operation": "delta_ensure"})
    mutation = nmc.plan_beacon_node_mutation(snapshot, selection, {"kind": "delta_ensure", "tags": ["new"]})
    from beacon_edits import BeaconEditPlan, verify_beacon_edit_plan
    assert isinstance(mutation["edit_plan"], BeaconEditPlan)
    proof = verify_beacon_edit_plan(mutation["edit_plan"], scanner=nmc._beacon_edit_records)
    assert proof["success"], proof
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


LEGACY_LAYOUTS = [c for c in RECON_LAYOUTS if c[1] in {'python', 'js_ts'}]
LEGACY_BARE_TWINS = {
    ('py_p17_property.py', 'C.x'), ('py_p18_overload.py', 'f'),
    ('js_j07_getset.js', 'C.x'),
    ('tw_py_getter_block_setter_tag.py', 'C.x#2'),
    ('tw_js_get_block_set_tag.js', 'C.x#2'),
}


@pytest.mark.parametrize('fixture,language,specs,rows', LEGACY_LAYOUTS, ids=[c[0] for c in LEGACY_LAYOUTS])
@pytest.mark.parametrize('delete', [False, True], ids=['absolute', 'tagless'])
def test_recon_layouts_legacy_and_plan_select_the_same_physical_target(tmp_path, monkeypatch, fixture, language, specs, rows, delete):
    """Same quoted source and carried evidence agree across both front ends.

    The three-argument legacy API has no cache sibling witness: a bare twin
    without its own note id returns candidates, not a guessed first qualname.
    The comparison plan deliberately receives that same evidence, not a richer
    sibling-cache witness. Independently id-attached twins do write.
    """
    path = tmp_path / fixture
    prefix = '#' if language == 'python' else '//'
    safe_rows = block(prefix, bid='safe@legacy') + (['def u03_safe():', '    pass'] if language == 'python' else ['function u03_safe() {}'])
    pristine = ('\n'.join(rows + [''] + safe_rows) + '\n').encode()
    writer = getattr(nmc, 'update_beacon_from_node_' + language)
    monkeypatch.setattr(nmc, '_generate_auto_beacon_hash', lambda: 'leg4')
    for spec in specs:
        if isinstance(spec, dict) and 'remove_targets' in spec:
            continue
        path.write_bytes(pristine)
        cache = cache_for_nodes(path, language)
        target_spec = spec['t'] if isinstance(spec, dict) else spec
        match = re.match(r'^(.*?)(?:#(\d+))?$', target_spec)
        lookup, ordinal = match[1].removeprefix('ƒ ').removesuffix('()'), int(match[2] or 1)
        keys = [k for k, n in cache.items() if k != 'file' and nmc._extract_ast_qualname_from_note(n['note']) == lookup]
        key = keys[ordinal - 1]
        note = spec['note'] if isinstance(spec, dict) else cache[key]['note']
        if fixture == 'case2_adopt.py':
            note = 'AST_QUALNAME: f\n---\n'
        # Remove siblings only from the comparison cache: the physical file
        # and both operations' note remain identical.
        equal_evidence = {'file': cache['file'], key: {**cache[key], 'note': note}}
        plan = delta_plan(equal_evidence, key, 'remove_beacon_tags' if delete else 'ensure_beacons',
                          ('lab', 'glab', 'other') if delete else ('new',))
        expected = RECON_OUTCOMES.get(fixture, {}).get(target_spec, 'update')
        if (fixture, target_spec) in LEGACY_BARE_TWINS:
            expected = 'ambiguous_beacon_anchor'
        if delete:
            expected = {'update': 'delete', 'create': 'noop'}.get(expected, expected)
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        result = writer(str(path), lookup if delete else lookup + ' #new #new', note)
        if expected not in {'update', 'delete', 'create', 'noop'}:
            assert not plan.success and plan.error_kind == expected, (fixture, target_spec, plan)
            assert result['error_kind'] == expected and result['candidates'], (fixture, target_spec, result)
            assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, mtime)
            good = writer(str(path), 'u03_safe #new', 'AST_QUALNAME: u03_safe\nBEACON (AST)\nid: safe@legacy\n')
            assert good['operation'] == 'updated_beacon', good
            boundary = len(('\n'.join(rows) + '\n\n').encode())
            assert path.read_bytes()[:boundary] == before[:boundary]
            continue
        assert plan.success and plan.planned_operation == expected, (fixture, target_spec, plan)
        assert result['operation'] == {'update': 'updated_beacon', 'delete': 'deleted_beacon', 'create': 'created_beacon', 'noop': 'noop'}[expected], result
        assert result['physical_witness'] == plan.physical_witness
        if expected == 'noop':
            assert result['noop_reason'] == 'no_beacon_at_target'
            assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, mtime)
        else:
            from test_beacon_byte_edits import assert_outside_edits_identical
            assert_outside_edits_identical(before, path.read_bytes(), result)
            if expected == 'create':
                assert result['beacon_id'] == 'auto-beacon@' + lookup + '-leg4'
                assert result['role'] == ''
            if expected == 'update':
                assert result['slice_labels'] == ('lab,new' if fixture == 'case2_adopt.py' else 'new')


@pytest.mark.parametrize('language,prefix,suffix,code', [
    ('python', '#', '.py', ['def safe():', '    pass']),
    ('js_ts', '//', '.ts', ['function safe() {}']),
    ('sql', '--', '.sql', ['SELECT 1;']),
    ('shell', '#', '.sh', ['echo safe']),
    ('yaml', '#', '.yaml', ['safe: true']),
])
@pytest.mark.parametrize('delete', [False, True])
def test_legacy_exact_id_duplicate_spans_refuse_locally_and_safe_neighbour_writes(tmp_path, language, prefix, suffix, code, delete):
    rows = block(prefix, bid='copy', kind='span') + code + block(prefix, bid='copy', kind='span') + code + block(prefix, bid='safe', kind='span') + code
    path = tmp_path / ('spans' + suffix)
    before = ('\r\n'.join(rows) + '\r\n').encode()
    path.write_bytes(before)
    mtime = path.stat().st_mtime_ns
    writer = getattr(nmc, 'update_beacon_from_node_' + language)
    result = writer(str(path), 'span' if delete else 'span #new', 'BEACON (SPAN)\nid: copy\n')
    assert result['error_kind'] == 'ambiguous_beacon_id', result
    assert [c['id'] for c in result['candidates']].count('copy') == 2
    assert all({'line', 'kind', 'labels', 'comment', 'section'} <= c.keys() for c in result['candidates'])
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, mtime)
    good = writer(str(path), 'safe #new', 'BEACON (SPAN)\nid: safe\n')
    assert good['operation'] == 'updated_beacon', good
    boundary = before.index((prefix + ' @beacon[').encode(), before.index((prefix + ' @beacon[').encode(), 1) + 1)
    assert path.read_bytes() == before[:boundary] + before[boundary:].replace(b'slice_labels=old,', b'slice_labels=new,')


@pytest.mark.parametrize('language,prefix,suffix,alpha,beta', [
    ('python', '#', '.py', ['def alpha():', '    pass'], ['def beta():', '    pass']),
    ('js_ts', '//', '.ts', ['function alpha() {}'], ['function beta() {}']),
])
@pytest.mark.parametrize('case', ['duplicate_update', 'duplicate_delete', 'swap', 'rename', 'gone_create', 'tagless_adopt'])
def test_legacy_duplicate_stale_rename_and_missing_id_contract(tmp_path, language, prefix, suffix, alpha, beta, case):
    rows = block(prefix, bid='copy') + alpha + block(prefix, bid='copy' if case.startswith('duplicate') else 'new@beta') + beta
    if case == 'gone_create':
        rows = block(prefix, bid='copy') + alpha + beta
    path = tmp_path / ('identity' + suffix)
    before = ('\n'.join(rows) + '\n').encode()
    path.write_bytes(before)
    mtime = path.stat().st_mtime_ns
    note = 'AST_QUALNAME: beta\nBEACON (AST)\nid: ' + ('copy' if case.startswith('duplicate') or case == 'swap' else 'old@beta') + '\n'
    if case == 'tagless_adopt':
        note = 'AST_QUALNAME: beta\n'
    writer = getattr(nmc, 'update_beacon_from_node_' + language)
    result = writer(str(path), 'beta' if case in {'duplicate_delete', 'tagless_adopt'} else 'beta #new', note)
    if case == 'swap':
        assert result['error_kind'] == 'beacon_identity_conflict' and result['noop_reason'] == 'beacon_identity_conflict'
        assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, mtime)
        assert writer(str(path), 'alpha #new', 'AST_QUALNAME: alpha\nBEACON (AST)\nid: copy\n')['operation'] == 'updated_beacon'
        return
    assert result['operation'] == ('created_beacon' if case == 'gone_create' else 'deleted_beacon' if case in {'duplicate_delete', 'tagless_adopt'} else 'updated_beacon'), result
    if case == 'rename':
        assert result['beacon_id'] == 'new@beta'
    split = before.index((prefix + ' @beacon[').encode(), 1) if case != 'gone_create' else before.index(beta[0].encode())
    assert path.read_bytes().startswith(before[:split])
    if case == 'duplicate_update':
        assert path.read_bytes() == before[:split] + before[split:].replace(b'slice_labels=old,', b'slice_labels=new,')
    if case in {'duplicate_delete', 'tagless_adopt'}:
        assert path.read_bytes() == before[:split] + ('\n'.join(beta) + '\n').encode()


@pytest.mark.parametrize('language', ['python', 'js_ts', 'sql'])
@pytest.mark.parametrize('delete', [False, True])
def test_original_p4a_legacy_and_p4b_plan_duplicate_witnesses(tmp_path, language, delete):
    from beacon_legacy_layouts import P4_PY, P4_JS, P4_SQL
    rows = {'python': P4_PY, 'js_ts': P4_JS, 'sql': P4_SQL}[language]
    prefix = {'python': '#', 'js_ts': '//', 'sql': '--'}[language]
    bid = {'python': 'p@alpha', 'js_ts': 'j@alpha', 'sql': 'q@s'}[language]
    path = tmp_path / {'python': 'p4.py', 'js_ts': 'p4.js', 'sql': 'p4.sql'}[language]
    before = ('\n'.join(rows) + '\n').encode()
    path.write_bytes(before)
    mtime = path.stat().st_mtime_ns
    note = ('AST_QUALNAME: beta\n' if language != 'sql' else '') + 'BEACON (AST)\nid: ' + bid + '\n'
    if language != 'sql':
        note += 'comment: Beta copy-paste.\n'
    writer = getattr(nmc, 'update_beacon_from_node_' + language)
    result = writer(str(path), 'beta' if delete else 'beta #lab-b #lab-new', note)
    if language == 'sql':
        assert result['error_kind'] == 'ambiguous_beacon_id' and len(result['candidates']) == 2, result
        assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, mtime)
        # Same-file positive control: a uniquely named span remains writable.
        safe_rows = block('--', bid='safe', kind='span') + ['SELECT 3;']
        path.write_bytes(before + ('\n'.join(safe_rows) + '\n').encode())
        assert writer(str(path), 'safe #new', 'BEACON (SPAN)\nid: safe\n')['operation'] == 'updated_beacon'
        assert path.read_bytes().startswith(before)
        return
    split = before.index((prefix + ' @beacon[').encode(), before.index((prefix + ' @beacon[').encode()) + 1)
    assert result['operation'] == ('deleted_beacon' if delete else 'updated_beacon'), result
    assert path.read_bytes().startswith(before[:split])
    if not delete:
        assert path.read_bytes() == before[:split] + before[split:].replace(b'slice_labels=lab-b,', b'slice_labels=lab-b,lab-new,')
    # The plan front end receives exactly the same source/node evidence.
    path.write_bytes(before)
    cache = {'file': {'id': 'file', 'note': 'Path: ' + str(path)},
             'beta': {'id': 'beta', 'name': 'beta #lab-b', 'note': note, 'parent_id': 'file'}}
    plan = delta_plan(cache, 'beta', 'remove_beacon_tags' if delete else 'ensure_beacons', ('lab-b',) if delete else ('lab-new',))
    assert plan.success and plan.physical_witness == result['physical_witness'], plan
    assert commit_plan(path, plan, note)['operation'] == result['operation']


@pytest.mark.parametrize('case', ['rename', 'swap', 'reuse', 'span_rename'])
def test_original_p4c_p4d_stale_identity_witnesses(tmp_path, case):
    from beacon_legacy_layouts import P4_PY, P4_SPANS
    path = tmp_path / 'p4.py'
    base = ('\n'.join(P4_SPANS if case == 'span_rename' else P4_PY) + '\n').encode()
    if case != 'span_rename':
        base = base.replace(b'id=p@alpha,\n#   slice_labels=lab-b', b'id=p@beta,\n#   slice_labels=lab-b')
    if case == 'rename':
        before = base.replace(b'id=p@alpha,', b'id=p@alpha2,')
    elif case == 'swap':
        before = base.replace(b'id=p@alpha,', b'id=TMP,').replace(b'id=p@beta,', b'id=p@alpha,').replace(b'id=TMP,', b'id=p@beta,')
    elif case == 'reuse':
        before = base.replace(b'id=p@alpha,', b'id=p@alpha2,').replace(b'id=p@beta,', b'id=p@alpha,')
    else:
        before = base.replace(b'id=p@s1,', b'id=p@s1b,')
    path.write_bytes(before)
    mtime = path.stat().st_mtime_ns
    note = ('BEACON (SPAN)\nid: p@s1\n' if case == 'span_rename' else
            'AST_QUALNAME: alpha\nBEACON (AST)\nid: p@alpha\ncomment: Alpha.\n')
    result = nmc.update_beacon_from_node_python(str(path), 'alpha #lab-a #lab-new', note)
    if case == 'rename':
        assert result['operation'] == 'updated_beacon' and result['beacon_id'] == 'p@alpha2', result
        assert path.read_bytes() == before.replace(b'slice_labels=lab-a,', b'slice_labels=lab-a,lab-new,')
    else:
        assert result['error_kind'] == 'beacon_identity_conflict' and result['candidates'], result
        assert result['noop_reason'] == ('missing_beacon_id' if case == 'span_rename' else 'beacon_identity_conflict')
        assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, mtime)
        safe_note = 'BEACON (SPAN)\nid: p@s2\n' if case == 'span_rename' else 'AST_QUALNAME: beta\nBEACON (AST)\nid: p@alpha\n'
        good = nmc.update_beacon_from_node_python(str(path), 'safe #new', safe_note)
        assert good['operation'] == 'updated_beacon', good


@pytest.mark.parametrize('fixture,language,specs,rows', [r for r in RECON_LAYOUTS if r[1] == 'markdown'], ids=lambda r: str(r)[:70])
@pytest.mark.parametrize('delete', [False, True])
def test_markdown_recon_legacy_uses_the_shared_heading_selection(tmp_path, fixture, language, specs, rows, delete):
    """004d1ba's first-id/recreate/case2 walks disagree with m02/m07/m11.
    No occurrence is invented for ambiguous headings; safe same-file rows run.
    """
    path = tmp_path / fixture
    safe_rows = [r.replace('b@target', 'safe@md') for r in MD] + ['# U03 Safe']
    pristine = ('\n'.join(rows + [''] + safe_rows) + '\n').encode()
    for spec in specs:
        if isinstance(spec, dict) and 'remove_targets' in spec:
            continue
        path.write_bytes(pristine)
        cache = cache_for_nodes(path, language)
        target = spec['t'] if isinstance(spec, dict) else spec
        match = re.match(r'^(.*?)(?:#(\d+))?$', target)
        lookup, ordinal = match[1], int(match[2] or 1)
        keys = [k for k, n in cache.items() if k != 'file' and
                (nmc._extract_md_path_from_note(n['note']) or [''])[-1].lstrip('# ') == lookup]
        key = keys[ordinal - 1]
        note = spec['note'] if isinstance(spec, dict) else cache[key]['note']
        expected = RECON_OUTCOMES.get(fixture, {}).get(target, 'update')
        if delete:
            expected = {'update': 'delete', 'create': 'noop'}.get(expected, expected)
        stamp = path.stat().st_mtime_ns
        result = nmc.update_beacon_from_node_markdown(str(path), lookup if delete else lookup + ' #new', note)
        if expected not in {'update', 'create', 'delete', 'noop'}:
            assert result['error_kind'] == expected and result['candidates'], (fixture, result)
            assert (path.read_bytes(), path.stat().st_mtime_ns) == (pristine, stamp)
            good = nmc.update_beacon_from_node_markdown(str(path), 'safe #new',
                'MD_PATH:\n# U03 Safe\n---\nBEACON (MD AST)\nid: safe@md\n')
            assert good['operation'] == 'updated_beacon', good
            boundary = len(('\n'.join(rows) + '\n\n').encode())
            assert path.read_bytes()[:boundary] == pristine[:boundary]
        else:
            assert result['operation'] == {'update': 'updated_beacon', 'create': 'created_beacon',
                'delete': 'deleted_beacon', 'noop': 'noop'}[expected], (fixture, result)
            if expected == 'noop':
                assert (path.read_bytes(), path.stat().st_mtime_ns) == (pristine, stamp)
            else:
                from test_beacon_byte_edits import assert_outside_edits_identical
                assert_outside_edits_identical(pristine, path.read_bytes(), result)


@pytest.mark.parametrize('language', ['vue', 'markdown'])
@pytest.mark.parametrize('case', ['duplicate_update', 'duplicate_delete', 'swap', 'rename', 'gone_create', 'tagless_adopt'])
def test_vue_markdown_legacy_physical_identity_contract(tmp_path, language, case):
    md = language == 'markdown'
    first = [r.replace('b@target', 'copy') for r in MD] if md else block('//', bid='copy')
    second_id = 'copy' if case.startswith('duplicate') else 'new@beta'
    second = [r.replace('b@target', second_id) for r in MD] if md else block('//', bid=second_id)
    alpha, beta = ['# Alpha'] if md else ['function alpha() {}'], ['# Beta'] if md else ['function beta() {}']
    rows = first + alpha + ([] if case == 'gone_create' else second) + beta
    rows = rows if md else ['<script>'] + rows + ['</script>']
    path = tmp_path / ('identity.md' if md else 'identity.vue')
    before = ('\r\n'.join(rows) + '\r\n').encode()
    path.write_bytes(before)
    stamp = path.stat().st_mtime_ns
    anchor = 'MD_PATH:\n# Beta\n---\n' if md else 'AST_QUALNAME: vue:script0:beta\n'
    note = anchor + 'BEACON (AST)\nid: ' + ('copy' if case.startswith('duplicate') or case == 'swap' else 'old@beta') + '\n'
    if case == 'tagless_adopt':
        note = anchor
    writer = getattr(nmc, 'update_beacon_from_node_' + language)
    result = writer(str(path), 'beta' if case in {'duplicate_delete', 'tagless_adopt'} else 'beta #new', note)
    if case == 'swap':
        assert result['error_kind'] == 'beacon_identity_conflict' and result['candidates'], result
        assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, stamp)
        safe_anchor = 'MD_PATH:\n# Alpha\n---\n' if md else 'AST_QUALNAME: vue:script0:alpha\n'
        assert writer(str(path), 'alpha #new', safe_anchor + 'BEACON (AST)\nid: copy\n')['operation'] == 'updated_beacon'
        return
    assert result['operation'] == ('created_beacon' if case == 'gone_create' else
        'deleted_beacon' if case in {'duplicate_delete', 'tagless_adopt'} else 'updated_beacon'), result
    if case == 'rename':
        assert result['beacon_id'] == 'new@beta'
    if case == 'gone_create' and md:
        assert result['beacon_id'] == 'old@beta'  # phase 7 compatibility, not remint
    from test_beacon_byte_edits import assert_outside_edits_identical
    assert_outside_edits_identical(before, path.read_bytes(), result)
    if case.startswith('duplicate'):
        boundary = before.index(('<!--' if md else '// @beacon[').encode(), before.index(('<!--' if md else '// @beacon[').encode()) + 1)
        assert path.read_bytes()[:boundary] == before[:boundary]


@pytest.mark.parametrize('intent', ['absolute_labels', 'rehydrate'])
def test_markdown_phase7_shape_recreates_note_ids_at_occurrence_over_all_headings(tmp_path, intent):
    """Phase-7 caller contract, without modifying mdr's phase orchestration.
    004d1ba reuses ids but a second equal id redirects to the first block.
    """
    path = tmp_path / 'phase7.md'
    pristine = b'# Doc\n\n## Notes\n\nuntagged\n\n## Notes\n\nsecond\n\n## Notes\n\nthird\n'
    path.write_bytes(pristine)
    for occurrence, bid in [(2, 'shared@notes'), (3, 'shared@notes' if intent == 'rehydrate' else 'third@notes')]:
        note = 'MD_PATH:\n# Doc\n## Notes\n---\nBEACON (MD AST)\nid: ' + bid + '\nkind: ast\nshow_span: false\ncomment: heading ' + str(occurrence) + '\n'
        result = nmc.update_beacon_from_node_markdown(str(path), 'Notes #tag-' + str(occurrence), note,
            occurrence=occurrence, intent=intent)
        assert result['operation'] == 'created_beacon' and result['beacon_id'] == bid, result
    snapshot = nmc.read_source_snapshot(path)
    index = nmc.beacon_attachment_index(snapshot, outline(snapshot, 'markdown'),
        nmc._beacon_selection_structures(snapshot, 'markdown'))
    assert [(b['record']['fields']['id'], b['record']['fields']['slice_labels'],
             b['record']['fields']['comment'], b['record']['fields']['show_span']) for b in index['blocks']] == [
        ('shared@notes', 'tag-2', 'heading 2', 'false'),
        ('shared@notes' if intent == 'rehydrate' else 'third@notes', 'tag-3', 'heading 3', 'false')]
    assert [b['anchor']['line'] for b in index['blocks']] == [a['line'] for a in index['anchors'] if a['header'] == '## Notes'][1:]
    assert b'role=' not in path.read_bytes()
    # Removing only scanner-owned block bytes exactly recovers the beacon-free file.
    raw = path.read_bytes()
    for b in reversed(index['blocks']):
        a, z = b['record']['write_ranges'][0]
        raw = raw[:a] + raw[z:]
    assert raw == pristine


@pytest.mark.parametrize('section', ['template', 'style'])
@pytest.mark.parametrize('delete', [False, True])
def test_vue_legacy_template_style_capability_and_wrapper_idempotence(tmp_path, section, delete):
    from beacon_field_layouts import VUE
    path = tmp_path / 'capability.vue'
    before = ('\n'.join(VUE) + '\n').encode()
    path.write_bytes(before)
    bid, old = ('tpl@1', 't1') if section == 'template' else ('sty@1', 'st')
    note = 'BEACON (SPAN)\nid: ' + bid + '\n'
    result = nmc.update_beacon_from_node_vue(str(path), 'renamed' if delete else 'renamed #new', note)
    assert result['operation'] == ('deleted_beacon' if delete else 'updated_beacon'), result
    from test_beacon_byte_edits import assert_outside_edits_identical
    assert_outside_edits_identical(before, path.read_bytes(), result)
    if not delete:
        assert path.read_bytes() == before.replace(('slice_labels=' + old + ',').encode(), b'slice_labels=new,')
        current, stamp = path.read_bytes(), path.stat().st_mtime_ns
        for _ in range(3):
            again = nmc.update_beacon_from_node_vue(str(path), 'renamed #new', note)
            assert again['operation'] == 'noop' and again['noop_reason'] == 'already_satisfied', again
            assert (path.read_bytes(), path.stat().st_mtime_ns) == (current, stamp)


@pytest.mark.parametrize('delete', [False, True])
def test_original_p4_markdown_duplicate_legacy_and_plan_write_only_section_b(tmp_path, delete):
    from beacon_legacy_layouts import P4_MD
    path = tmp_path / 'p4.md'
    before = ('\n'.join(P4_MD) + '\n').encode()
    path.write_bytes(before)
    note = 'MD_PATH:\n# Title\n## Section B\n---\nBEACON (MD AST)\nid: m@sec\ncomment: Second.\n'
    result = nmc.update_beacon_from_node_markdown(str(path), 'Section B' if delete else 'Section B #lab-b #lab-new', note)
    assert result['operation'] == ('deleted_beacon' if delete else 'updated_beacon'), result
    split = before.index(b'<!--', before.index(b'<!--') + 1)
    expected = (before[:split] + b'## Section B\n\nText B.\n' if delete else
                before[:split] + before[split:].replace(b'slice_labels=lab-b,', b'slice_labels=lab-b,lab-new,'))
    assert path.read_bytes() == expected
    path.write_bytes(before)
    cache = {'file': {'id': 'file', 'note': 'Path: ' + str(path)},
             'beta': {'id': 'beta', 'name': 'Section B #lab-b', 'note': note, 'parent_id': 'file'}}
    plan = delta_plan(cache, 'beta', 'remove_beacon_tags' if delete else 'ensure_beacons', ('lab-b',) if delete else ('lab-new',))
    assert plan.success and plan.physical_witness == result['physical_witness'], plan
    assert commit_plan(path, plan, note)['operation'] == result['operation']
    assert path.read_bytes() == expected


def test_markdown_headerless_mint_keeps_convention_and_supplied_id_never_mints(tmp_path, monkeypatch):
    path = tmp_path / 'mint.md'
    path.write_bytes(b'# Fresh\n\n# Supplied\n')
    values = iter(['aaaa', 'bbbb'])
    monkeypatch.setattr(nmc, '_generate_auto_beacon_hash', lambda: next(values))
    fresh = nmc.update_beacon_from_node_markdown(str(path), 'Fresh #new', 'MD_PATH:\n# Fresh\n---\n')
    assert fresh['operation'] == 'created_beacon' and fresh['beacon_id'] == 'auto-beacon@aaaabbbb', fresh
    # The exhausted iterator makes an accidental mint fail loudly.
    supplied = nmc.update_beacon_from_node_markdown(str(path), 'Supplied #new',
        'MD_PATH:\n# Supplied\n---\nBEACON (MD AST)\nid: supplied@heading\n')
    assert supplied['operation'] == 'created_beacon' and supplied['beacon_id'] == 'supplied@heading', supplied


def test_markdown_outline_path_lookup_matches_compatibility_parser_without_reparse(tmp_path, monkeypatch):
    """Vendoring needs a pure selector, not a second Markdown parser or policy."""
    rows = ['# **Title**', '', '> ## Quoted', '', '- item', '  ### Nested body', '',
            '## `Repeat`', '', 'Text', '', '## Repeat', '', 'Setext', '------', '',
            '######## Deep', '', '## Mixed_CASE']
    path = tmp_path / 'paths.md'
    path.write_bytes(('\n'.join(rows) + '\n').encode())
    snapshot = nmc.read_source_snapshot(path)
    structures = nmc._beacon_selection_structures(snapshot, 'markdown')
    index = nmc.beacon_attachment_index(snapshot, structures['outline'], structures)
    paths = [nmc._extract_md_path_from_note(a['node']['note']) for a in index['anchors']]
    expected = {}
    for md_path in paths:
        for ordinal in range(1, len(paths) + 1):
            found = nmc._find_markdown_heading_insert_idx(list(snapshot.physical_lines), md_path, ordinal)
            if found is not None:
                expected[(tuple(md_path), ordinal)] = found + 1
    monkeypatch.setattr(nmc, 'MarkdownIt', lambda *a, **kw: pytest.fail('selector reparsed Markdown'))
    for (md_path, ordinal), line in expected.items():
        selection = nmc.select_beacon_block_for_node(snapshot, structures,
            {'language': 'markdown', 'md_path': list(md_path), 'occurrence': ordinal, 'operation': 'rehydrate'})
        assert selection['status'] == 'create' and selection['anchor']['line'] == line


@pytest.mark.parametrize('intent', ['absolute_labels', 'rehydrate'])
def test_markdown_no_heading_identity_never_uses_a_duplicate_id_to_guess_and_neighbour_runs(tmp_path, intent):
    path = tmp_path / 'ambiguous.md'
    rows = ['# Doc', '', '## Notes', ''] + [r.replace('b@target', 'copy') for r in MD] + [
        '## Notes', ''] + [r.replace('b@target', 'safe') for r in MD] + ['# Safe']
    before = ('\n'.join(rows) + '\n').encode()
    path.write_bytes(before)
    stamp = path.stat().st_mtime_ns
    # No id in the note can identify either identical bare heading.
    result = nmc.update_beacon_from_node_markdown(str(path), 'Notes #new',
        'MD_PATH:\n# Doc\n## Notes\n---\n', intent=intent)
    assert result['error_kind'] == 'ambiguous_beacon_anchor' and result['candidates'], result
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, stamp)
    good = nmc.update_beacon_from_node_markdown(str(path), 'Safe #new',
        'MD_PATH:\n# Safe\n---\nBEACON (MD AST)\nid: safe\n', intent=intent)
    assert good['operation'] == 'updated_beacon', good
    split = before.rindex(b'<!--')
    assert path.read_bytes() == before[:split] + before[split:].replace(b'slice_labels=old,', b'slice_labels=new,')


def test_rehydrate_ambiguous_heading_cannot_redirect_through_unique_note_id(tmp_path):
    path = tmp_path / 'rehydrate-ambiguous.md'
    rows = ['# Doc', '', '## Notes', ''] + [r.replace('b@target', 'copy') for r in MD] + [
        '## Notes', ''] + [r.replace('b@target', 'safe') for r in MD] + ['# Safe']
    before = ('\n'.join(rows) + '\n').encode()
    path.write_bytes(before)
    stamp = path.stat().st_mtime_ns
    result = nmc.update_beacon_from_node_markdown(str(path), 'Notes #new',
        'MD_PATH:\n# Doc\n## Notes\n---\nBEACON (MD AST)\nid: copy\n', intent='rehydrate')
    assert result['error_kind'] == 'ambiguous_beacon_anchor' and result['candidates'], result
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, stamp)
    assert nmc.update_beacon_from_node_markdown(str(path), 'Safe #new',
        'MD_PATH:\n# Safe\n---\nBEACON (MD AST)\nid: safe\n', intent='rehydrate')['operation'] == 'updated_beacon'


@pytest.mark.parametrize('language', ['vue', 'markdown'])
@pytest.mark.parametrize('delete', [False, True])
def test_vue_markdown_exact_id_span_ambiguity_is_local(tmp_path, language, delete):
    if language == 'markdown':
        span = [r.replace('kind=ast', 'kind=span').replace('b@target', 'copy') for r in MD]
        rows = span + ['body one'] + span + ['body two'] + [r.replace('copy', 'safe') for r in span] + ['safe body']
    else:
        span = block('//', bid='copy', kind='span')
        rows = ['<script>'] + span + ['const one=1;'] + span + ['const two=2;'] + [r.replace('copy', 'safe') for r in span] + ['const safe=3;', '</script>']
    path = tmp_path / ('spans.md' if language == 'markdown' else 'spans.vue')
    before = ('\n'.join(rows) + '\n').encode()
    path.write_bytes(before)
    stamp = path.stat().st_mtime_ns
    writer = getattr(nmc, 'update_beacon_from_node_' + language)
    result = writer(str(path), 'span' if delete else 'span #new', 'BEACON (SPAN)\nid: copy\n')
    assert result['error_kind'] == 'ambiguous_beacon_id' and [c['id'] for c in result['candidates']].count('copy') == 2, result
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, stamp)
    good = writer(str(path), 'safe #new', 'BEACON (SPAN)\nid: safe\n')
    assert good['operation'] == 'updated_beacon', good
    split = before.rindex(('<!--' if language == 'markdown' else '// @beacon[').encode())
    assert path.read_bytes() == before[:split] + before[split:].replace(b'slice_labels=old,', b'slice_labels=new,')


def test_rehydrate_incomplete_claim_at_heading_is_not_absence_and_neighbour_runs(tmp_path):
    path = tmp_path / 'claimed.md'
    rows = ['<!--', '@beacon[', ' id=damaged,', ' slice_labels=old,', ' kind=span,', '-->',
            '# Target', ''] + [r.replace('b@target', 'safe') for r in MD] + ['# Safe']
    before = ('\n'.join(rows) + '\n').encode()
    path.write_bytes(before)
    stamp = path.stat().st_mtime_ns
    result = nmc.update_beacon_from_node_markdown(str(path), 'Target #new',
        'MD_PATH:\n# Target\n---\nBEACON (MD AST)\nid: damaged\nkind: ast\n', intent='rehydrate')
    assert result['error_kind'] == 'beacon_extent_unreadable' and result['candidates'], result
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, stamp)
    assert nmc.update_beacon_from_node_markdown(str(path), 'Safe #new',
        'MD_PATH:\n# Safe\n---\nBEACON (MD AST)\nid: safe\nkind: ast\n', intent='rehydrate')['operation'] == 'updated_beacon'
    split = before.rindex(b'<!--')
    assert path.read_bytes() == before[:split] + before[split:].replace(b'slice_labels=old,', b'slice_labels=new,')
