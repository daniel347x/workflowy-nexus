"""Decorator-aware Python beacon reconciliation, using real temporary source.

No Workflowy calls: exercise the shared preflight/JIT planner and disk writer.
Both agent-authored beacons above decorators and older writer placement below
(or between) decorators must resolve to the same AST identity without relaxing
wrong-target, duplicate-ID, or parse-failure refusals.
"""

import ast
import hashlib
import textwrap

import pytest

import nexus_map_codebase as nmc
from beacon_mutation_contract import BeaconCommentIntent
from workflowy_mcp.client import api_client_nexus as acn
from workflowy_mcp.lightning_rod_mutation_spec import BeaconMutationOperation


BEACON_ID = "coherence@cache-view"
BLOCK = (
    "# @beacon[\n"
    f"#   id={BEACON_ID},\n"
    "#   role=keep-role,\n"
    "#   slice_labels=old,keep,\n"
    "#   kind=ast,\n"
    "#   comment=Keep this documentation.,\n"
    "# ]\n"
)

# Undefined decorator names are intentional: fixtures are parsed, NEVER run.
SOURCES = [
    pytest.param(BLOCK + "@dataclass(frozen=True)\nclass CacheView:\n    pass\n", "CacheView", id="dataclass"),
    pytest.param(BLOCK + "@wrapper\ndef wrapped():\n    return 1\n", "wrapped", id="function"),
    pytest.param(BLOCK + "@asynccontextmanager\nasync def boundary():\n    yield\n", "boundary", id="async-contextmanager"),
    pytest.param(BLOCK + "@outer\n@inner\ndef wrapped():\n    return 1\n", "wrapped", id="stacked"),
    pytest.param(BLOCK + "@dataclass(\n    frozen=True,\n    # A comment inside the decorator expression.\n    slots=True,\n)\nclass CacheView:\n    pass\n", "CacheView", id="multiline-call"),
    pytest.param(BLOCK + "@(\n    wrapper\n)\ndef wrapped():\n    return 1\n", "wrapped", id="parenthesized-expression"),
    pytest.param("@dataclass(frozen=True)\n" + BLOCK + "class CacheView:\n    pass\n", "CacheView", id="beacon-after-decorator"),
    pytest.param("@outer\n" + BLOCK + "@inner(\n    value=True,\n)\ndef wrapped():\n    return 1\n", "wrapped", id="beacon-between-decorators"),
    pytest.param(BLOCK + "@outer\n# Comment between owned decorators.\n@inner(\n    value=True,\n)\n# Comment before the definition.\ndef wrapped():\n    return 1\n", "wrapped", id="comments-between-decorators-and-def"),
    pytest.param("class Container:\n" + textwrap.indent(BLOCK + "@property\ndef value(self):\n    return 1\n", "    "), "Container.value", id="nested-property"),
    pytest.param(BLOCK + "\n\nclass CacheView:\n    pass\n", "CacheView", id="undecorated-control"),
]


def _cache(path, qualname, *, cached_id=BEACON_ID):
    note = f"AST_QUALNAME: {qualname}\n"
    if cached_id:
        note += f"BEACON (AST)\nid: {cached_id}\nkind: ast\n"
    return {
        "root": {"id": "root", "name": "Root", "note": "", "parent_id": None},
        "file": {"id": "file", "name": path.name, "note": f"Path: {path}", "parent_id": "root"},
        "node": {"id": "node", "name": qualname, "note": note, "parent_id": "file"},
    }


def _plan(cache, *, tags=("new",), mode="ensure_beacons", fingerprint=True):
    op = BeaconMutationOperation(
        input_index=0,
        handle="node",
        tags=tags,
        comment=BeaconCommentIntent.preserve(),
    )
    return acn._compute_beacon_write_plan(
        cache, "node", op, mode, include_fingerprint=fingerprint
    )


@pytest.mark.parametrize("source,qualname", SOURCES)
@pytest.mark.parametrize("fingerprint", [False, True], ids=["preflight", "jit"])
@pytest.mark.parametrize("cached_id", [BEACON_ID, None, "stale-missing-id"], ids=["known-id", "ast-only", "stale-id"])
def test_existing_decorated_beacon_updates_or_adopts_without_writing(tmp_path, source, qualname, fingerprint, cached_id):
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    before = path.read_bytes()
    plan = _plan(_cache(path, qualname, cached_id=cached_id), fingerprint=fingerprint)
    assert plan.success, (plan.error_kind, plan.error)
    assert plan.planned_operation == "update"  # NEVER a duplicate create
    assert plan.beacon_id == BEACON_ID
    assert plan.intended_slice_labels == ("old", "keep", "new")
    assert plan.source_sha256 == (hashlib.sha256(before).hexdigest() if fingerprint else None)
    assert path.read_bytes() == before


@pytest.mark.parametrize("source,qualname", SOURCES)
@pytest.mark.parametrize(
    "mode,tags,expected,labels",
    [
        ("ensure_beacons", ("old",), "noop", ("old", "keep")),
        ("remove_beacon_tags", ("old",), "update", ("keep",)),
        ("remove_beacon_tags", ("old", "keep"), "delete", ()),
    ],
)
def test_decorated_existing_beacon_noop_and_remove(tmp_path, source, qualname, mode, tags, expected, labels):
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    before = path.read_bytes()
    plan = _plan(_cache(path, qualname), tags=tags, mode=mode)
    assert plan.success, plan.error
    assert plan.planned_operation == expected
    assert plan.beacon_id == BEACON_ID
    assert plan.intended_slice_labels == labels
    assert path.read_bytes() == before


@pytest.mark.parametrize("source,qualname", SOURCES)
def test_decorated_plan_then_commit_preserves_id_metadata_and_code(tmp_path, source, qualname):
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    cache = _cache(path, qualname)
    plan = _plan(cache)
    assert plan.success, plan.error
    result = nmc.update_beacon_from_node_python(
        str(path), plan.intended_name, cache["node"]["note"],
        write_intent=plan.write_intent, write_plan=plan,
    )
    assert result.get("operation") == "updated_beacon", result
    assert result.get("beacon_id") == BEACON_ID
    updated = path.read_text(encoding="utf-8")
    assert updated.count("# @beacon[") == 1
    assert "role=keep-role," in updated
    assert "comment=Keep this documentation.," in updated
    assert "slice_labels=old,keep,new," in updated
    assert ast.dump(ast.parse(updated)) == ast.dump(ast.parse(source))
    assert updated.replace("slice_labels=old,keep,new,", "slice_labels=old,keep,") == source
    again = _plan(cache)
    assert again.success, again.error
    assert again.planned_operation == "noop"


@pytest.mark.parametrize("source,qualname", SOURCES)
def test_legacy_ast_only_writer_also_reuses_decorated_beacon(tmp_path, source, qualname):
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    result = nmc.update_beacon_from_node_python(
        str(path), f"{qualname} #new", f"AST_QUALNAME: {qualname}\n"
    )
    assert result.get("operation") == "updated_beacon", result
    assert result.get("beacon_id") == BEACON_ID
    updated = path.read_text(encoding="utf-8")
    assert updated.count("# @beacon[") == 1
    assert ast.dump(ast.parse(updated)) == ast.dump(ast.parse(source))


@pytest.mark.parametrize("mode", ["ensure_beacons", "remove_beacon_tags"])
@pytest.mark.parametrize(
    "source,qualname,error_kind",
    [
        pytest.param(BLOCK + "@decorator\ndef other():\n    pass\n\n@decorator\ndef target():\n    pass\n", "target", "beacon_identity_conflict", id="wrong-symbol"),
        pytest.param(BLOCK + "SENTINEL = 1\n@decorator\ndef target():\n    pass\n", "target", "beacon_identity_conflict", id="intervening-code"),
        pytest.param(BLOCK + "def outer():\n    @decorator\n    def target():\n        pass\n", "outer.target", "beacon_identity_conflict", id="different-scope"),
        pytest.param(BLOCK + "@decorator\ndef target():\n    pass\n" + BLOCK + "@decorator\ndef other():\n    pass\n", "target", "ambiguous_beacon_id", id="duplicate-id"),
        pytest.param(BLOCK + "@decorator(\ndef target():\n    pass\n", "target", "no_beacon_anchor_for_node", id="invalid-python"),
        pytest.param(BLOCK + "@decorator\ndef renamed():\n    pass\n", "missing", "no_beacon_anchor_for_node", id="missing-symbol"),
    ],
)
def test_decorator_support_preserves_identity_safety(tmp_path, source, qualname, error_kind, mode):
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    before = path.read_bytes()
    plan = _plan(_cache(path, qualname), mode=mode)
    if error_kind == "ambiguous_beacon_id":
        assert plan.success, plan.error
        assert plan.diagnostics[0]["type"] == "duplicate_id"
    else:
        assert not plan.success
        assert plan.error_kind == error_kind
    assert path.read_bytes() == before


def test_decorator_gap_does_not_accept_beacon_inside_expression(tmp_path):
    source = "@decorator(\n" + BLOCK + "    value=True,\n)\ndef target():\n    pass\n"
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    before = path.read_bytes()
    plan = _plan(_cache(path, "target"))
    assert plan.success and plan.planned_operation == "create", plan.error
    assert plan.diagnostics[0]["type"] == "detached_candidates"
    assert path.read_bytes() == before


def test_anchor_matcher_keeps_cardinality_and_language_boundaries():
    source = BLOCK + "@decorator(\n    value=True,\n)\ndef target():\n    pass\n"
    lines = source.splitlines()
    target_idx0 = next(i for i, line in enumerate(lines) if line.startswith("def target"))
    beacons = nmc.parse_python_beacon_blocks(lines)
    assert len(beacons) == 1
    # The matcher must not silently pick the first of multiple candidates.
    assert len(nmc._find_anchor_matched_beacons("python", lines, beacons * 2, target_idx0)) == 2
    # '#' is also the shell grammar, but Python syntax permissions MUST NOT
    # bleed into that language. Its original strict gap rule remains intact.
    assert nmc._find_anchor_matched_beacons("shell", lines, beacons, target_idx0) == []


def test_decorator_range_failure_grants_no_gap_permission():
    lines = (BLOCK + "@decorator(\ndef target():\n    pass\n").splitlines()
    target_idx0 = next(i for i, line in enumerate(lines) if line.startswith("def target"))
    assert nmc._python_decorator_line_ranges(lines, target_idx0) == []
    beacons = nmc.parse_python_beacon_blocks(lines)
    assert nmc._find_anchor_matched_beacons("python", lines, beacons, target_idx0) == []


def _mapped_node(path, qualname):
    matches = [
        node for node in nmc._iter_ast_outline_nodes(nmc.parse_file_outline(str(path)))
        if node.get("ast_qualname") == qualname
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize("source,qualname", SOURCES)
def test_forward_mapping_attaches_the_same_decorated_beacon(tmp_path, source, qualname):
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    before = path.read_bytes()
    node = _mapped_node(path, qualname)
    assert nmc._extract_beacon_id_from_note(node["note"]) == BEACON_ID
    assert "🔱" in node["name"]
    assert "#old" in node["name"] and "#keep" in node["name"]
    assert "comment: Keep this documentation." in node["note"]
    # Real emitted metadata, not a hand-authored cache fixture, must roundtrip.
    cache = _cache(path, qualname)
    cache["node"].update(name=node["name"], note=node["note"])
    plan = _plan(cache)
    assert plan.success, plan.error
    assert plan.planned_operation == "update"
    assert plan.beacon_id == BEACON_ID
    assert path.read_bytes() == before  # Forward mapping and planning are pure reads.
    result = nmc.update_beacon_from_node_python(
        str(path), plan.intended_name, node["note"],
        write_intent=plan.write_intent, write_plan=plan,
    )
    assert result.get("operation") == "updated_beacon", result
    after = _mapped_node(path, qualname)
    assert nmc._extract_beacon_id_from_note(after["note"]) == BEACON_ID
    assert "#new" in after["name"]
    updated = path.read_text(encoding="utf-8")
    assert updated.replace("slice_labels=old,keep,new,", "slice_labels=old,keep,") == source
    # The first forward parse + plan were reads; the only change is the commit.
    assert before.decode("utf-8").replace("\r\n", "\n") == source


@pytest.mark.parametrize("source,qualname", SOURCES)
def test_legacy_ast_only_delete_finds_beacon_through_forward_mapper(tmp_path, source, qualname):
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    result = nmc.update_beacon_from_node_python(
        str(path), qualname, f"AST_QUALNAME: {qualname}\n"
    )
    assert result.get("operation") == "deleted_beacon", result
    assert result.get("beacon_id") == BEACON_ID
    remaining = path.read_text(encoding="utf-8")
    assert "@beacon[" not in remaining
    assert ast.dump(ast.parse(remaining)) == ast.dump(ast.parse(source))


@pytest.mark.parametrize("declaration,qualname", [("class Example:\n    pass\n", "Example"), ("def example():\n    pass\n", "example")])
def test_forward_parenthesized_decorator_keeps_true_start_and_leading_comments(tmp_path, declaration, qualname):
    source = "# Important leading context.\n" + BLOCK + "@(\n    wrapper\n)\n" + declaration
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    before = path.read_bytes()
    node = _mapped_node(path, qualname)
    assert nmc._extract_beacon_id_from_note(node["note"]) == BEACON_ID
    assert node["decor_start_lineno"] == source.splitlines().index("@(") + 1
    assert "Important leading context." in node["note"]
    assert path.read_bytes() == before


def test_forward_anchor_does_not_attach_an_embedded_decorator_beacon(tmp_path):
    source = "@decorator(\n" + BLOCK + "    value=True,\n)\ndef target():\n    pass\n"
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    before = path.read_bytes()
    node = _mapped_node(path, "target")
    assert not nmc._extract_beacon_id_from_note(node["note"])
    assert "🔱" not in node["name"]
    assert path.read_bytes() == before


def test_forward_explicit_span_stays_a_span_above_multiline_decorator(tmp_path):
    source = BLOCK.replace("kind=ast,", "kind=span,") + "@decorator(\n    value=True,\n)\ndef target():\n    pass\n"
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    before = path.read_bytes()
    outline = nmc.parse_file_outline(str(path))
    nodes = list(nmc._iter_ast_outline_nodes(outline))
    target = next(node for node in nodes if node.get("ast_qualname") == "target")
    assert not nmc._extract_beacon_id_from_note(target["note"])
    assert sum("BEACON (SPAN)" in node.get("note", "") for node in outline) == 1
    assert path.read_bytes() == before


def test_forward_builds_shared_index_once_from_existing_ast(tmp_path, monkeypatch):
    source = BLOCK + "@(\n    wrapper\n)\nclass Example:\n    pass\n"
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    original = nmc._python_decorator_ranges_by_definition
    calls = []

    def record(lines, *, tree=None):
        calls.append(tree)
        return original(lines, tree=tree)

    monkeypatch.setattr(nmc, "_python_decorator_ranges_by_definition", record)
    node = _mapped_node(path, "Example")
    assert nmc._extract_beacon_id_from_note(node["note"]) == BEACON_ID
    assert len(calls) == 1  # No re-tokenization or reparse for each beacon.
    assert isinstance(calls[0], ast.Module)


def test_standalone_forward_attachment_uses_same_syntax_authority(tmp_path, monkeypatch):
    source = BLOCK + "@(\n    wrapper\n)\nclass Example:\n    pass\n"
    lines = source.splitlines()
    definition_line = next(i + 1 for i, line in enumerate(lines) if line.startswith("class Example"))
    node = {
        "name": "class Example", "note": "AST_QUALNAME: Example",
        "ast_type": "class", "ast_qualname": "Example", "children": [],
        "orig_lineno_start_unused": definition_line,
        "orig_lineno_end_unused": len(lines),
    }
    original = nmc._python_decorator_ranges_by_definition
    calls = []

    def record(snapshot, *, tree=None):
        calls.append((snapshot, tree))
        return original(snapshot, tree=tree)

    monkeypatch.setattr(nmc, "_python_decorator_ranges_by_definition", record)
    nmc.apply_python_beacons(str(tmp_path / "not-written.py"), lines, [node])
    assert nmc._extract_beacon_id_from_note(node["note"]) == BEACON_ID
    assert len(calls) == 1
    assert calls[0][0] is lines and calls[0][1] is None


def test_forward_index_keeps_decorator_ownership_across_definitions(tmp_path):
    source = (
        BLOCK + "@(\n    outer\n)\nclass First:\n    pass\n\n"
        "@different(\n    value=True,\n)\nclass Second:\n    pass\n"
    )
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    before = path.read_bytes()
    first = _mapped_node(path, "First")
    second = _mapped_node(path, "Second")
    assert nmc._extract_beacon_id_from_note(first["note"]) == BEACON_ID
    assert not nmc._extract_beacon_id_from_note(second["note"])
    assert "🔱" not in second["name"]
    plan = _plan(_cache(path, "Second"))  # Wrong ID must STILL be rejected.
    assert not plan.success and plan.error_kind == "beacon_identity_conflict"
    assert path.read_bytes() == before


def test_decorated_create_then_update_remains_supported(tmp_path):
    source = "@dataclass(\n    frozen=True,\n)\nclass CacheView:\n    pass\n"
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    cache = _cache(path, "CacheView", cached_id=None)
    plan = _plan(cache)
    assert plan.success, plan.error
    assert plan.planned_operation == "create"
    result = nmc.update_beacon_from_node_python(
        str(path), plan.intended_name, cache["node"]["note"],
        write_intent=plan.write_intent, write_plan=plan,
    )
    assert result.get("operation") == "created_beacon", result
    followup = _plan(cache, tags=("another",))
    assert followup.success, followup.error
    assert followup.planned_operation == "update"
    assert followup.beacon_id == result["beacon_id"]
    assert path.read_text(encoding="utf-8").count("# @beacon[") == 1
