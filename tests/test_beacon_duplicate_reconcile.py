"""U03 §6.7: Area-5 Q2/Q2b/P4e refresh identity, not write permissions.

Quoted Area-5 fixtures are deliberate: tests run without git or scratch. Base
004d1ba recreates the second same-level copy on both passes and hijacks P4e.
The four cross-level/span/Vue fixtures and clean renames are compatibility
controls. Ambiguous equal spans must expose candidates, not assign by order.
"""

import copy
import hashlib
import json
from pathlib import Path

import pytest

import nexus_map_codebase as n


FIXTURES = {
    "py_dup2ast.py": [
        "import os", "", "", "# @beacon[", "#   id=p@alpha,",
        "#   slice_labels=lab-a,", "#   kind=ast,", "#   comment=Alpha.,", "# ]",
        "def alpha():", "    return 1", "", "", "# @beacon[", "#   id=p@alpha,",
        "#   slice_labels=lab-b,", "#   kind=ast,", "#   comment=Beta copy-paste.,", "# ]",
        "def beta():", "    return 2", "", "", "def gamma():", "    return 3",
    ],
    "js_dup2ast.js": [
        "const x = 1;", "", "// @beacon[", "//   id=j@alpha,",
        "//   slice_labels=lab-a,", "//   kind=ast,", "//   comment=Alpha.,", "// ]",
        "function alpha() {", "  return 1;", "}", "", "// @beacon[", "//   id=j@alpha,",
        "//   slice_labels=lab-b,", "//   kind=ast,", "//   comment=Beta copy-paste.,", "// ]",
        "function beta() {", "  return 2;", "}",
    ],
    "md_dup.md": [
        "# Title", "", "<!--", "@beacon[", "  id=m@sec,", "  slice_labels=lab-a,",
        "  kind=ast,", "  comment=First.,", "]", "-->", "## Section A", "", "Text A.",
        "", "<!--", "@beacon[", "  id=m@sec,", "  slice_labels=lab-b,", "  kind=ast,",
        "  comment=Second.,", "]", "-->", "## Section B", "", "Text B.",
    ],
    "sql_dup.sql": [
        "SELECT 0;", "-- @beacon[", "--   id=q@s,", "--   slice_labels=lab-a,",
        "--   kind=span,", "-- ]", "SELECT 1;", "-- @beacon-close[", "--   id=q@s,",
        "-- ]", "-- @beacon[", "--   id=q@s,", "--   slice_labels=lab-b,",
        "--   kind=span,", "-- ]", "SELECT 2;", "-- @beacon-close[", "--   id=q@s,", "-- ]",
    ],
    "py_dup_levels.py": [
        "import os", "", "", "class Box:", "    # @beacon[", "    #   id=p@open,",
        "    #   slice_labels=lab-a,", "    #   kind=ast,", "    #   comment=Box.open.,",
        "    # ]", "    def open(self):", "        return 1", "", "", "# @beacon[",
        "#   id=p@open,", "#   slice_labels=lab-b,", "#   kind=ast,",
        "#   comment=Module open.,", "# ]", "def open_file():", "    return 2",
    ],
    "py_ast_span.py": [
        "import os", "", "", "# @beacon[", "#   id=p@x,", "#   slice_labels=lab-a,",
        "#   kind=ast,", "#   comment=An ast block.,", "# ]", "def alpha():",
        "    return 1", "", "", "# @beacon[", "#   id=p@x,", "#   slice_labels=lab-s,",
        "#   kind=span,", "#   comment=A span with the same id.,", "# ]", "VALUE = 1",
        "OTHER = 2", "# @beacon-close[", "#   id=p@x,", "# ]",
    ],
    "py_dup_spans.py": [
        "import os", "", "# @beacon[", "#   id=p@s,", "#   slice_labels=lab-s,",
        "#   kind=span,", "#   comment=First span.,", "# ]", "VALUE = 1",
        "# @beacon-close[", "#   id=p@s,", "# ]", "", "# @beacon[", "#   id=p@s,",
        "#   slice_labels=lab-t,", "#   kind=span,", "#   comment=Second span, same id.,",
        "# ]", "OTHER = 2", "# @beacon-close[", "#   id=p@s,", "# ]",
    ],
    "vue_dup.vue": [
        "<template>", "  <div>", "    <!--", "    @beacon[", "      id=v@shared,",
        "      slice_labels=lab-t,", "      kind=span,", "    ]", "    -->",
        "    <p>hi</p>", "  </div>", "</template>", "", "<script>", "// @beacon[",
        "//   id=v@shared,", "//   slice_labels=lab-s,", "//   kind=ast,",
        "//   comment=Script block with the template id.,", "// ]", "function alpha() {",
        "  return 1;", "}", "</script>",
    ],
}


def _walk(node, path=()):
    yield path, node
    for index, child in enumerate(node.get("children") or []):
        yield from _walk(child, (*path, index))


def _ether(source):
    tree = copy.deepcopy(source)
    for index, (_path, node) in enumerate(_walk(tree)):
        node["id"] = f"uuid-{index}"
    return tree


def _uuid_by_qual(tree):
    return {n._extract_ast_qualname_from_note(node.get("note") or ""): node.get("id")
            for _path, node in _walk(tree)
            if n._extract_ast_qualname_from_note(node.get("note") or "")}


def _assert_no_churn(source, ether):
    created = [path for path, node in _walk(source) if not node.get("id")]
    source_ids = {node["id"] for _path, node in _walk(source) if node.get("id")}
    deleted = [node["id"] for _path, node in _walk(ether) if node["id"] not in source_ids]
    assert created == []
    assert deleted == []


@pytest.mark.parametrize("fixture", FIXTURES)
def test_area5_duplicate_refresh_twice_keeps_every_uuid(tmp_path, fixture):
    path = tmp_path / fixture
    before = ("\n".join(FIXTURES[fixture]) + "\n").encode()
    path.write_bytes(before)
    mtime = path.stat().st_mtime_ns
    mapped = n.map_codebase(str(path), repo_root=str(tmp_path))
    ether = _ether(mapped)
    expected = {p: node["id"] for p, node in _walk(ether)}
    for _run in range(2):
        source = n.map_codebase(str(path), repo_root=str(tmp_path))
        n.reconcile_trees_cartographer(source, ether)
        _assert_no_churn(source, ether)
        assert {p: node["id"] for p, node in _walk(source)} == expected
        assert not source.get("_beacon_reconcile_report")
        ether = copy.deepcopy(source)
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("earlier", [False, True], ids=["Q2b", "P4e-hijack"])
def test_introduced_duplicate_does_not_hijack_other_symbol_uuid(tmp_path, earlier):
    path = tmp_path / "introduced.py"
    duplicate = "\n".join(FIXTURES["py_dup2ast.py"]) + "\n"
    clean = duplicate.replace("id=p@alpha,\n#   slice_labels=lab-b", "id=p@beta,\n#   slice_labels=lab-b")
    path.write_text(clean, encoding="utf-8")
    ether = _ether(n.map_codebase(str(path), repo_root=str(tmp_path)))
    expected = _uuid_by_qual(ether)
    path.write_text(clean.replace("id=p@alpha,", "id=p@beta,") if earlier else duplicate,
                    encoding="utf-8")
    for _run in range(2):
        source = n.map_codebase(str(path), repo_root=str(tmp_path))
        n.reconcile_trees_cartographer(source, ether)
        # A duplicate warning node is legitimately new on the first refresh;
        # identity assertions concern all eligible pre-existing symbol nodes.
        assert _uuid_by_qual(source) == expected
        ether = copy.deepcopy(source)
        for index, (_p, node) in enumerate(_walk(ether)):
            node.setdefault("id", f"new-warning-{index}")


def _node(name, bid, anchor="", **extra):
    return {"name": name, "note": f"BEACON (AST)\nid: {bid}\n\n{anchor}", "children": [], **extra}


@pytest.mark.parametrize("anchor", ["AST_QUALNAME: alpha", "MD_PATH:\n# Title\n## A\n---",
                                    "VUE_SECTION_KEY: script-1"])
def test_unique_id_clean_symbol_rename_and_id_rename_stay_stable(anchor):
    original = _node("old", "p@old", anchor, id="old-uuid")
    for name, bid in [("old", "p@old"), ("renamed", "p@old"), ("old", "p@new")]:
        source = {"name": "file", "children": [_node(name, bid, anchor)]}
        ether = {"id": "file-uuid", "children": [copy.deepcopy(original)]}
        n.reconcile_trees_cartographer(source, ether)
        assert source["children"][0]["id"] == "old-uuid"
        assert not source.get("_beacon_reconcile_report")


def test_equal_named_shared_spans_are_ambiguity_not_order_and_neighbour_matches():
    spans = [_node("🔱 p #lab", "p@s"), _node("🔱 p #lab", "p@s")]
    safe = _node("ƒ safe()", "p@safe", "AST_QUALNAME: safe")
    source = {"name": "file", "children": [*spans, safe]}
    ether = _ether(source)
    # Reordering otherwise indistinguishable nodes cannot justify a UUID swap.
    ether["children"][0], ether["children"][1] = ether["children"][1], ether["children"][0]
    expected_candidates = [node["id"] for node in ether["children"][:2]]
    n.reconcile_trees_cartographer(source, ether)
    assert all("id" not in node for node in spans)
    assert safe["id"] == ether["children"][2]["id"]
    report = source["_beacon_reconcile_report"]
    assert report["ambiguous_count"] == 2
    for row in report["ambiguous"]:
        assert row["beacon_id"] == "p@s"
        assert row["source_names"] == ["🔱 p #lab", "🔱 p #lab"]
        assert row["ether_ids"] == expected_candidates
    assert "_carto_identity_seed_report" not in source


def test_shared_spans_use_unique_names_not_changed_order():
    children = [_node("🔱 p #one", "p@s"), _node("🔱 p #two", "p@s")]
    ether = _ether({"children": children})
    source = {"children": list(reversed(copy.deepcopy(children)))}
    n.reconcile_trees_cartographer(source, ether)
    assert [node["id"] for node in source["children"]] == ["uuid-2", "uuid-1"]
    assert not source.get("_beacon_reconcile_report")


def test_shared_ast_twin_does_not_become_unique_after_consumption():
    source = {"children": [_node("same", "p@same", "AST_QUALNAME: same") for _ in range(2)]}
    ether = _ether(source)
    n.reconcile_trees_cartographer(source, ether)
    assert all("id" not in node for node in source["children"])
    assert source["_beacon_reconcile_report"]["ambiguous_count"] == 2


def test_nested_duplicate_ambiguity_reaches_root_without_preserve_ids():
    source = {"children": [{"name": "parent", "children": [
        _node("same", "p@s"), _node("same", "p@s")]}]}
    ether = _ether(source)
    n.reconcile_trees_cartographer(source, ether)
    assert source["_beacon_reconcile_report"]["ambiguous_count"] == 2
    assert "_carto_identity_seed_report" not in source


# Small, fixed compatibility sample from the exact-byte corpus, not generated
# fixtures or a new expected-change baseline. The whole corpus is characterized
# separately; these files keep routine reconcile coverage below a few seconds.
CLEAN_CORPUS_SAMPLE = (
    "src/workflowy_mcp/__init__.py",
    "src/workflowy_mcp/ancestor_path.py",
    "src/workflowy_mcp/coherence/durable_io.py",
    "src/workflowy_mcp/job_status_view.py",
    "src/workflowy_mcp/models/errors.py",
    "workflowy-glimpse-cache/background.js",
    "workflowy-glimpse-cache/hotkeys-shared.js",
    "workflowy-glimpse-cache/marked-katex-init.js",
)


@pytest.mark.parametrize("relative", CLEAN_CORPUS_SAMPLE)
def test_clean_corpus_self_reconcile_keeps_every_uuid(tmp_path, relative):
    """Unique-id corpus compatibility: both refreshes preserve all assignments."""
    corpus = Path(__file__).parent / "beacon_robustness_corpus" / "nexus_base"
    entries = json.loads((corpus / "sources_manifest.json").read_text(encoding="utf-8"))["entries"]
    entry = next(row for row in entries if row["path"] == relative)
    before = (corpus / entry["frozen"]).read_bytes()
    assert hashlib.sha256(before).hexdigest() == entry["sha256"]
    path = tmp_path / relative  # Restore the real extension; .txt maps differently.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(before)
    mtime = path.stat().st_mtime_ns
    mapped = n.map_codebase(str(path), repo_root=str(tmp_path))
    beacon_ids = [n._extract_beacon_id_from_note(node.get("note") or "")
                  for _p, node in _walk(mapped)]
    beacon_ids = [bid for bid in beacon_ids if bid]
    assert beacon_ids, relative
    assert len(beacon_ids) == len(set(beacon_ids)), relative
    ether = _ether(mapped)
    expected = {p: node["id"] for p, node in _walk(ether)}
    for _run in range(2):
        source = copy.deepcopy(mapped)
        n.reconcile_trees_cartographer(source, ether)
        _assert_no_churn(source, ether)
        assert {p: node["id"] for p, node in _walk(source)} == expected
        assert not source.get("_beacon_reconcile_report")
        ether = copy.deepcopy(source)
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime
