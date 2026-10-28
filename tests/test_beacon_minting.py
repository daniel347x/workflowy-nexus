"""U03 §6.8: finite allocation and advisory-only file-local resemblance.

Base 004d1ba mints the occupied A5-D12 id on both write paths. It has no
allocator or resemblance evidence; ordinary bytes in advisory cases already
write and are a compatibility control, not a newly required permission gate.
"""

import pytest

import nexus_map_codebase as n
from test_beacon_target_selection import cache_for_nodes, commit_plan, delta_plan


D12 = [
    "import os", "", "", "# @beacon[", "#   id=auto-beacon@alpha-aaaa,",
    "#   slice_labels=lab-b,", "#   kind=ast,",
    "#   comment=Existing block on beta carrying an alpha-shaped auto-beacon id.,",
    "# ]", "def beta():", "    return 2", "", "", "def alpha():", "    return 1",
]


def test_allocator_repeating_random_source_has_finite_deterministic_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(n, "_generate_auto_beacon_hash", lambda: calls.append(1) or "aaaa")
    taken = {"auto-beacon@alpha-aaaa", "auto-beacon@alpha-0000", "auto-beacon@alpha-0000-2"}
    original = set(taken)
    for _run in range(2):
        assert n.allocate_beacon_id(taken, base_id="auto-beacon@alpha-", random_parts=1) == "auto-beacon@alpha-0000-3"
    assert len(calls) == 8
    assert taken == original


def test_allocator_keeps_plain_kit_convention_and_explicit_plan_set():
    taken = {"lab@alpha", "lab@alpha-2", "lab@alpha-4"}
    assert n.allocate_beacon_id(taken, base_id="lab@alpha") == "lab@alpha-3"
    assert n.allocate_beacon_id(taken | {"lab@alpha-3"}, base_id="lab@alpha") == "lab@alpha-5"
    assert n.allocate_beacon_id(set(), base_id="lab@alpha") == "lab@alpha"


@pytest.mark.parametrize("planned", [False, True], ids=["legacy", "executor"])
def test_area5_d12_collision_changes_only_new_id_not_existing_bytes(tmp_path, monkeypatch, planned):
    path = tmp_path / "d12.py"
    before = ("\n".join(D12) + "\n").encode()
    path.write_bytes(before)
    monkeypatch.setattr(n, "_generate_auto_beacon_hash", lambda: "aaaa")
    if planned:
        cache = cache_for_nodes(path, "python")
        key = next(k for k, value in cache.items() if "AST_QUALNAME: alpha" in value.get("note", ""))
        plan = delta_plan(cache, key, tags=("lab-new",))
        result = commit_plan(path, plan, cache[key]["note"])
    else:
        result = n.update_beacon_from_node_python(str(path), "ƒ alpha() #lab-new", "AST_QUALNAME: alpha")
    assert result["operation"] == "created_beacon", result
    assert result["beacon_id"] == "auto-beacon@alpha-0000"
    inserted = ("\n".join(["# @beacon[", "#   id=auto-beacon@alpha-0000,",
                           "#   slice_labels=lab-new,", "#   kind=ast,", "# ]"]) + "\n").encode()
    assert path.read_bytes() == before.replace(b"def alpha():", inserted + b"def alpha():")


@pytest.mark.parametrize("language,filename,rows,qualname", [
    ("python", "incomplete.py", ["# @beacon[", "# id=auto-beacon@alpha-aaaa,",
        "# id=auto-beacon@alpha-0000,", "def beta():", "    pass", "", "def alpha():", "    pass"], "alpha"),
    ("vue", "all-sections.vue", ["<template>", "<!-- @beacon[", "id=auto-beacon@vue:script0:alpha-aaaa,",
        "] -->", "<div/>", "</template>", "<script>", "function alpha() {}", "</script>"], "vue:script0:alpha"),
])
def test_mint_checks_all_id_occurrences_incomplete_and_other_sections(tmp_path, monkeypatch, language, filename, rows, qualname):
    path = tmp_path / filename
    path.write_bytes(("\n".join(rows) + "\n").encode())
    monkeypatch.setattr(n, "_generate_auto_beacon_hash", lambda: "aaaa")
    result = getattr(n, "update_beacon_from_node_" + language)(str(path), "alpha #new", "AST_QUALNAME: " + qualname)
    assert result["operation"] == "created_beacon", result
    assert result["beacon_id"] not in {"auto-beacon@" + qualname + "-aaaa", "auto-beacon@alpha-0000"}
    assert result["beacon_id"].endswith("-0000-2" if language == "python" else "-0000")


def test_two_markdown_creates_on_one_snapshot_use_explicit_planned_ids(tmp_path, monkeypatch):
    path = tmp_path / "plan.md"
    path.write_bytes(b"# First\n\n# Second\n")
    snapshot = n.read_source_snapshot(path)
    structures = n._beacon_selection_structures(snapshot, "markdown")
    taken = {r["fields"].get("id") for r in structures["records"] if r["fields"].get("id")}
    monkeypatch.setattr(n, "_generate_auto_beacon_hash", lambda: "aaaa")
    created = []
    for heading in ("# First", "# Second"):
        selection = n.select_beacon_block_for_node(snapshot, structures,
            {"language": "markdown", "md_path": [heading], "operation": "delta_ensure"})
        bid = n.allocate_beacon_id(taken, base_id="auto-beacon@", random_parts=2)
        mutation = n.plan_beacon_node_mutation(snapshot, selection,
            {"kind": "delta_ensure", "tags": ["new"], "create_id": bid})
        assert mutation["success"] and mutation["edit_plan"] is not None
        created.append(mutation["beacon_id"])
        taken.add(bid)
    assert created == ["auto-beacon@aaaaaaaa", "auto-beacon@00000000"]


def test_supplied_duplicate_markdown_id_is_not_allocated(tmp_path, monkeypatch):
    path = tmp_path / "supplied.md"
    rows = ["<!--", "@beacon[", "id=supplied@same,", "slice_labels=old,", "kind=ast,", "]", "-->",
            "# First", "", "# Second"]
    path.write_bytes(("\n".join(rows) + "\n").encode())
    before = path.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError("supplied ids must not be minted")
    monkeypatch.setattr(n, "allocate_beacon_id", forbidden)
    result = n.update_beacon_from_node_markdown(str(path), "Second #new",
        "MD_PATH:\n# Second\n---\nBEACON (MD AST)\nid: supplied@same\n", intent="rehydrate")
    assert result["operation"] == "created_beacon", result
    assert result["beacon_id"] == "supplied@same"
    assert path.read_bytes().count(b"id=supplied@same,") == 2
    assert path.read_bytes().startswith(before[:before.index(b"# Second")])


@pytest.mark.parametrize("bid,qualname,body,relation", [
    ("p@Old.alpha-aaaa", "New.alpha", ["class New:", "    def alpha(self):", "        pass"], "R3"),
    ("p@alhpa-aaaa", "alpha", ["def alpha():", "    pass"], "R7"),
    ("p@beta-aaaa", "alpha", ["def alpha():", "    pass", "", "def beta():", "    pass"], "R8"),
])
def test_resemblance_suggestions_never_change_plain_edit_bytes(tmp_path, monkeypatch, bid, qualname, body, relation):
    path = tmp_path / "advisory.py"
    prefix = "    " if "." in qualname else ""
    block = [prefix + row for row in ["# @beacon[", f"# id={bid},", "# slice_labels=old,", "# kind=ast,", "# ]"]]
    rows = body[:1] + block + body[1:] if prefix else block + body
    before = ("\n".join(rows) + "\n").encode()
    path.write_bytes(before)
    snapshot = n.read_source_snapshot(path)
    structures = n._beacon_selection_structures(snapshot, "python")
    mtime = path.stat().st_mtime_ns
    # Pure selection/planning cannot perform another observation for evidence.
    with monkeypatch.context() as patch:
        patch.setattr(n, "read_source_snapshot", lambda *a: pytest.fail("extra evidence read"))
        selection = n.select_beacon_block_for_node(snapshot, structures,
            {"language": "python", "ast_qualname": qualname, "note_beacon_id": bid, "operation": "delta_ensure"})
        mutation = n.plan_beacon_node_mutation(snapshot, selection, {"kind": "delta_ensure", "tags": ["new"]})
    evidence = mutation["resemblance_evidence"]
    assert len(evidence) == 1 and evidence[0]["relation"] == relation
    assert evidence[0]["advisory"] is True and "keep" in evidence[0]["suggestions"]
    if relation == "R8":
        assert evidence[0]["named_symbols"][0]["ast_qualname"] == "beta"
        assert evidence[0]["named_symbols"][0]["line"] == len(rows) - 1
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    note = f"AST_QUALNAME: {qualname}\nBEACON (AST)\nid: {bid}\n"
    result = n.update_beacon_from_node_python(str(path), "alpha #old #new", note)
    assert result["operation"] == "updated_beacon", result
    assert result["resemblance_evidence"][0]["relation"] == relation
    expected = before.replace(b"slice_labels=old,", b"slice_labels=old,new,")
    assert path.read_bytes() == expected
    path.write_bytes(before)
    monkeypatch.setattr(n, "beacon_id_resemblance", lambda *a: [])
    plain = n.update_beacon_from_node_python(str(path), "alpha #old #new", note)
    assert plain["operation"] == "updated_beacon" and path.read_bytes() == expected


@pytest.mark.parametrize("bid,symbol", [("p@alpha", "alpha"), ("p@ALPHA", "alpha"), ("p@_alpha_", "alpha"),
    ("p@read-rows", "ReadRows"), ("p@ReadRows", "read_rows"), ("p@_read_rows_", "_readRows_"),
    ("p@alpha-phaseA", "alpha"), ("concept@durable-writer", "alpha")])
def test_compatible_and_unrelated_concept_ids_have_no_resemblance_policy(tmp_path, bid, symbol):
    path = tmp_path / "quiet.py"
    rows = ["# @beacon[", f"# id={bid},", "# slice_labels=old,", "# kind=ast,", "# ]", f"def {symbol}():", "    pass"]
    path.write_bytes(("\n".join(rows) + "\n").encode())
    result = n.update_beacon_from_node_python(str(path), symbol + " #old #new", f"AST_QUALNAME: {symbol}\nBEACON (AST)\nid: {bid}\n")
    assert result["operation"] == "updated_beacon", result
    assert result["resemblance_evidence"] == []
