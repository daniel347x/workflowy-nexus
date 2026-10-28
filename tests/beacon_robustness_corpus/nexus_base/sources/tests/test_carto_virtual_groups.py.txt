import os
import tempfile

import nexus_map_codebase as carto


def _tmp_source(text: str, suffix: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return f.name


def test_python_carto_group_markers_wrap_top_level_nodes_only():
    path = _tmp_source(
        '# @carto-group id=grp-a label="Group A"\n\n'
        'def a():\n'
        '    def nested():\n'
        '        pass\n\n'
        '# @carto-group id=grp-b label="Group B"\n\n'
        'def b():\n'
        '    pass\n',
        ".py",
    )
    try:
        out = carto.parse_file_outline(path)
    finally:
        os.unlink(path)

    assert [n["name"] for n in out] == ["📦 Group A", "📦 Group B"]
    assert [c["name"] for c in out[0]["children"]] == ["ƒ a()"]
    # Nested functions stay inside their AST parent, not lifted/grouped by line.
    assert [c["name"] for c in out[0]["children"][0]["children"]] == ["ƒ nested()"]
    assert [c["name"] for c in out[1]["children"]] == ["ƒ b()"]


def test_js_ts_carto_group_markers_wrap_top_level_nodes():
    path = _tmp_source(
        '// @carto-group id=grp-a label="Group A"\n'
        'function a() {}\n\n'
        '// @carto-group id=grp-b label="Group B"\n'
        'function b() {}\n',
        ".js",
    )
    try:
        out = carto.parse_js_ts_outline(path)
    finally:
        os.unlink(path)

    assert [n["name"] for n in out] == ["📦 Group A", "📦 Group B"]
    assert [c["name"] for c in out[0]["children"]] == ["ƒ function a()"]
    assert [c["name"] for c in out[1]["children"]] == ["ƒ function b()"]


def test_vue_script_carto_group_markers_are_scoped_under_script_section():
    path = _tmp_source(
        '<template><div/></template>\n'
        '<script setup>\n'
        '// @carto-group id=grp-a label="Group A"\n'
        'function a() {}\n'
        '</script>\n',
        ".vue",
    )
    try:
        out = carto.parse_vue_outline(path)
    finally:
        os.unlink(path)

    script = next(node for node in out if "script" in node["name"])
    assert [n["name"] for n in script["children"]] == ["📦 Group A"]
    assert [c["name"] for c in script["children"][0]["children"]] == ["ƒ function a()"]


def test_any_depth_identity_seed_preserves_ast_uuid_across_new_group_parent():
    source = {
        "name": "file",
        "note": "",
        "children": [
            {
                "name": "📦 Group A",
                "note": "CARTOGRAPHER_VIRTUAL_GROUP: python\nCARTO_GROUP_ID: grp-a\n---",
                "children": [
                    {"name": "ƒ a()", "note": "AST_QUALNAME: a", "children": []},
                ],
            }
        ],
    }
    ether = {
        "id": "file-id",
        "name": "file",
        "note": "",
        "children": [
            {"id": "old-a", "name": "ƒ a()", "note": "AST_QUALNAME: a", "children": []},
        ],
    }

    report = carto.seed_cartographer_ids_by_identity_any_depth(source, ether)

    assert source["children"][0]["children"][0]["id"] == "old-a"
    assert report["seeded_count"] == 1
    assert report["preserve_ids"] == []


def test_group_node_identity_seed_preserves_virtual_group_uuid_on_second_run():
    source = {
        "name": "file",
        "note": "",
        "children": [
            {
                "name": "📦 Group A",
                "note": "CARTOGRAPHER_VIRTUAL_GROUP: python\nCARTO_GROUP_ID: grp-a\n---",
                "children": [
                    {"name": "ƒ a()", "note": "AST_QUALNAME: a", "children": []},
                ],
            }
        ],
    }
    ether = {
        "id": "file-id",
        "name": "file",
        "note": "",
        "children": [
            {
                "id": "old-group-a",
                "name": "📦 Group A",
                "note": "CARTOGRAPHER_VIRTUAL_GROUP: python\nCARTO_GROUP_ID: grp-a\n---",
                "children": [
                    {"id": "old-a", "name": "ƒ a()", "note": "AST_QUALNAME: a", "children": []},
                ],
            }
        ],
    }

    report = carto.seed_cartographer_ids_by_identity_any_depth(source, ether)

    assert source["children"][0]["id"] == "old-group-a"
    assert source["children"][0]["children"][0]["id"] == "old-a"
    assert report["seeded_count"] == 2
    assert report["preserve_ids"] == []


def test_python_duplicate_const_assignments_get_line_suffixed_ast_qualnames():
    path = _tmp_source(
        'try:\n'
        '    FLAG = True\n'
        'except Exception:\n'
        '    FLAG = False\n',
        ".py",
    )
    try:
        out = carto.parse_file_outline(path)
    finally:
        os.unlink(path)

    quals = [node.get("ast_qualname") for node in out if node.get("ast_type") == "const"]
    assert len(quals) == 2
    assert len(set(quals)) == 2
    assert all(str(q).startswith("FLAG@line") for q in quals)
    assert [line for node in out for line in str(node.get("note") or "").splitlines() if line.startswith("AST_QUALNAME:")] == [
        f"AST_QUALNAME: {quals[0]}",
        f"AST_QUALNAME: {quals[1]}",
    ]


def test_reconcile_skips_any_depth_seed_when_no_virtual_groups():
    source = {
        "name": "file",
        "note": "",
        "children": [
            {"name": "💎 FLAG", "note": "AST_QUALNAME: FLAG", "children": []},
            {"name": "💎 FLAG", "note": "AST_QUALNAME: FLAG", "children": []},
        ],
    }
    ether = {
        "id": "file-id",
        "name": "file",
        "note": "",
        "children": [
            {"id": "old-flag-1", "name": "💎 FLAG", "note": "AST_QUALNAME: FLAG", "children": []},
            {"id": "old-flag-2", "name": "💎 FLAG", "note": "AST_QUALNAME: FLAG", "children": []},
        ],
    }

    carto.reconcile_trees_cartographer(source, ether)

    assert "_carto_identity_seed_report" not in source


def test_ambiguous_identity_seed_preserves_old_ether_ids_instead_of_guessing():
    source = {
        "name": "file",
        "note": "",
        "children": [
            {"name": "ƒ a()", "note": "AST_QUALNAME: a", "children": []},
            {"name": "ƒ a()", "note": "AST_QUALNAME: a", "children": []},
        ],
    }
    ether = {
        "id": "file-id",
        "name": "file",
        "note": "",
        "children": [
            {"id": "old-a1", "name": "ƒ a()", "note": "AST_QUALNAME: a", "children": []},
            {"id": "old-a2", "name": "ƒ a()", "note": "AST_QUALNAME: a", "children": []},
        ],
    }

    report = carto.seed_cartographer_ids_by_identity_any_depth(source, ether)

    assert "id" not in source["children"][0]
    assert "id" not in source["children"][1]
    assert report["seeded_count"] == 0
    assert report["ambiguous_count"] == 1
    assert set(report["preserve_ids"]) == {"old-a1", "old-a2"}
