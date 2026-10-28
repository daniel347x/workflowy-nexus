"""SPEC v5 §6/§10.3/§10.4 -- commit-5 cross-language reconciliation locks.

* delete-on-final-label-removal for sql/shell/yaml/vue (legacy tag-vanish
  path) with @beacon-close cleanup;
* Vue EXACT parsed-id matching (the id=foo vs id=foo2 prefix-collision fix);
* py/js Case-2 anchored-block idempotency guard (update-in-place instead of
  duplicate create);
* legacy py tag-vanish close cleanup.
"""

import os
import sys
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import nexus_map_codebase as nmc  # noqa: E402


@pytest.mark.parametrize('header,expected,removed', [
    ('BEACON (AST)\nid: disk@f\n', 'more', ['old']),
    ('AST_QUALNAME: f\n', 'old,more', []),
])
def test_absolute_intent_name_tags_and_header_adoption_contract(tmp_path, header, expected, removed):
    """The absolute/adoption contract consumed by the shared legacy adapter."""
    from beacon_edits import commit_beacon_edit_plan
    path = tmp_path / 'absolute.py'
    rows = ['# @beacon[', '# id=disk@f,', '# slice_labels=old,', '# role=legacy,',
            '# comment=keep me,', '# kind=ast,', '# ]', 'def f():', '    pass']
    before = ('\n'.join(rows) + '\n').encode()
    path.write_bytes(before)
    snapshot = nmc.read_source_snapshot(path)
    selected = nmc.select_beacon_block_for_node(snapshot, nmc._beacon_selection_structures(snapshot, 'python'),
        {'language': 'python', 'ast_qualname': 'f', 'note_beacon_id': nmc._extract_beacon_id_from_note(header),
         'operation': 'absolute_labels'})
    planned = nmc.plan_beacon_node_mutation(snapshot, selected,
        {'kind': 'absolute_labels', 'name': 'f #more #more', 'note': header})
    assert planned['removed_labels'] == removed
    assert planned['adoption_additive'] is not bool(nmc.parse_beacon_note_metadata(header))
    written = commit_beacon_edit_plan(planned['edit_plan'], scanner=nmc._beacon_edit_records)
    assert written['success'], written
    assert path.read_bytes() == before.replace(b'slice_labels=old,', ('slice_labels=' + expected + ',').encode())


def test_sql_tag_vanish_delete_removes_close_block(tmp_path):
    p = tmp_path / "q.sql"
    p.write_text(
        "-- @beacon[\n--   id=q1,\n--   role=q,\n--   slice_labels=old,\n--   kind=span,\n-- ]\n"
        "SELECT 1;\n"
        "-- @beacon-close[\n--   id=q1,\n-- ]\n",
        encoding="utf-8",
    )
    note = "BEACON (SQL SPAN)\nid: q1\nrole: q\nslice_labels: old\nkind: span\n---\n"
    res = nmc.update_beacon_from_node_sql(str(p), "q", note)  # name with NO tags
    assert res.get("operation") == "deleted_beacon", res
    content = p.read_text(encoding="utf-8")
    assert "@beacon[" not in content
    assert "@beacon-close[" not in content  # no orphaned closer
    assert "SELECT 1;" in content


def test_shell_tag_vanish_delete(tmp_path):
    p = tmp_path / "s.sh"
    p.write_text(
        "# @beacon[\n#   id=s1,\n#   slice_labels=old,\n#   kind=span,\n# ]\necho hi\n",
        encoding="utf-8",
    )
    note = "BEACON (SH SPAN)\nid: s1\nkind: span\n---\n"
    res = nmc.update_beacon_from_node_shell(str(p), "s", note)
    assert res.get("operation") == "deleted_beacon", res
    assert "@beacon[" not in p.read_text(encoding="utf-8")


def test_yaml_tag_vanish_delete(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "# @beacon[\n#   id=y1,\n#   slice_labels=old,\n#   kind=span,\n# ]\nkey: value\n",
        encoding="utf-8",
    )
    note = "BEACON (YAML SPAN)\nid: y1\nkind: span\n---\n"
    res = nmc.update_beacon_from_node_yaml(str(p), "c", note)
    assert res.get("operation") == "deleted_beacon", res
    assert "@beacon[" not in p.read_text(encoding="utf-8")


def test_py_legacy_tag_vanish_delete_removes_close_block(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(
        "# @beacon[\n#   id=span-1,\n#   slice_labels=old,\n#   kind=span,\n# ]\n"
        "def foo():\n    return 1\n"
        "# @beacon-close[\n#   id=span-1,\n# ]\n",
        encoding="utf-8",
    )
    note = "BEACON (AST)\nid: span-1\nkind: span\n---\n"
    res = nmc.update_beacon_from_node_python(str(p), "foo", note)  # no tags
    assert res.get("operation") == "deleted_beacon", res
    content = p.read_text(encoding="utf-8")
    assert "@beacon[" not in content
    assert "@beacon-close[" not in content


@pytest.mark.skipif(not nmc._HAVE_TREE_SITTER, reason="tree-sitter not available")
def test_vue_tag_vanish_delete_case1(tmp_path):
    p = tmp_path / "c.vue"
    p.write_text(
        "<script>\n"
        "// @beacon[\n//   id=v1,\n//   role=foo,\n//   slice_labels=old,\n//   kind=ast,\n// ]\n"
        "function foo() { return 1; }\n"
        "</script>\n",
        encoding="utf-8",
    )
    note = "AST_QUALNAME: vue:script0:foo\n---\nBEACON (JS/TS AST)\nid: v1\nkind: ast\n---\n"
    res = nmc.update_beacon_from_node_vue(str(p), "Foo", note)  # no tags
    assert res.get("operation") == "deleted_beacon", res
    assert "@beacon[" not in p.read_text(encoding="utf-8")


@pytest.mark.skipif(not nmc._HAVE_TREE_SITTER, reason="tree-sitter not available")
def test_vue_exact_id_no_prefix_collision(tmp_path):
    p = tmp_path / "c.vue"
    p.write_text(
        "<script>\n"
        "// @beacon[\n//   id=foo2,\n//   role=two,\n//   slice_labels=two,\n//   kind=ast,\n// ]\n"
        "function two() { return 2; }\n"
        "// @beacon[\n//   id=foo,\n//   role=one,\n//   slice_labels=one,\n//   kind=ast,\n// ]\n"
        "function one() { return 1; }\n"
        "</script>\n",
        encoding="utf-8",
    )
    note = "BEACON (JS/TS AST)\nid: foo\nkind: ast\n---\n"
    res = nmc.update_beacon_from_node_vue(str(p), "One #new", note)
    assert res.get("operation") == "updated_beacon", res
    content = p.read_text(encoding="utf-8")
    # foo2's block is byte-identical (the prefix collision would have hit it).
    assert "//   id=foo2,\n//   role=two,\n//   slice_labels=two," in content
    # foo's block was the one updated.
    foo_idx = content.index("//   id=foo,")
    foo_block = content[foo_idx : foo_idx + 200]
    assert "slice_labels=new," in foo_block


def test_py_case2_idempotency_guard_updates_instead_of_duplicating(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(
        "# @beacon[\n#   id=existing-1,\n#   role=foo,\n#   slice_labels=old,\n#   kind=ast,\n# ]\n"
        "def foo():\n    return 1\n",
        encoding="utf-8",
    )
    # Case 2 conditions: AST_QUALNAME + tags in the name, NO beacon id in the note.
    note = "AST_QUALNAME: foo\n---\n"
    res = nmc.update_beacon_from_node_python(str(p), "foo #new", note)
    assert res.get("operation") == "updated_beacon", res
    assert res.get("beacon_id") == "existing-1"
    content = p.read_text(encoding="utf-8")
    assert content.count("@beacon[") == 1
    assert "slice_labels=old,new," in content
    assert res['adoption_additive'] and res['removed_labels'] == []


@pytest.mark.skipif(not nmc._HAVE_TREE_SITTER, reason="tree-sitter not available")
def test_js_case2_idempotency_guard_updates_instead_of_duplicating(tmp_path):
    p = tmp_path / "m.js"
    p.write_text(
        "// @beacon[\n//   id=existing-1,\n//   role=foo,\n//   slice_labels=old,\n//   kind=ast,\n// ]\n"
        "function foo() { return 1; }\n",
        encoding="utf-8",
    )
    note = "AST_QUALNAME: foo\n---\n"
    res = nmc.update_beacon_from_node_js_ts(str(p), "foo #new", note)
    assert res.get("operation") == "updated_beacon", res
    assert res.get("beacon_id") == "existing-1"
    content = p.read_text(encoding="utf-8")
    assert content.count("@beacon[") == 1
    assert "slice_labels=old,new," in content
    assert res['adoption_additive'] and res['removed_labels'] == []


@pytest.fixture(params=["nexus", "agent-kit"])
def close_finder(request):
    """Run the safety locks against the source and the generated engine."""
    if request.param == "nexus":
        return nmc._find_beacon_close_block_extent
    path = Path(_REPO_ROOT) / "agent-kit/.claude/skills/code-map/scripts/_engine.py"
    spec = importlib.util.spec_from_file_location("_cf09_engine", path)
    engine = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = engine
    spec.loader.exec_module(engine)
    return engine._find_beacon_close_block_extent


_LINE_LANGUAGES = [
    ("python", "#"), ("js_ts", "//"), ("vue", "//"),
    ("sql", "--"), ("shell", "#"), ("yaml", "#"),
]


@pytest.mark.parametrize("language,prefix", _LINE_LANGUAGES)
def test_close_finder_ignores_mentions_and_other_ids(close_finder, language, prefix):
    lines = [
        f'{prefix} Example: @beacon-close[ is not an opener',
        'value = "@beacon-close["',
        '@beacon-close[',  # No language comment prefix: not a block.
        f'{prefix} @beacon-close[', f'{prefix} id=other,', f'{prefix} ]',
        f'{prefix} id=span-1,',  # Outside the other block: cannot change its id.
        'keep_this_code()',
        f'{prefix} @beacon-close[', f'{prefix} id="span-1",', f'{prefix} ]',
    ]
    # Vue geometry now belongs to actual SFC sections, not a whole-file JS
    # interpretation. Keep the same CF-09 false-close payload in a script.
    offset = int(language == "vue")
    if offset:
        lines = ["<script>", *lines, "</script>"]
    assert close_finder(language, lines, "span-1") == (8 + offset, 10 + offset)
    assert close_finder(language, lines, "span") is None


@pytest.mark.parametrize("language,prefix", _LINE_LANGUAGES)
@pytest.mark.parametrize("interruption", ["code", "opener", "eof"])
def test_close_finder_does_not_borrow_id_or_terminator(
    close_finder, language, prefix, interruption,
):
    lines = [f"{prefix} @beacon-close[", f"{prefix} id=span-1,"]
    if interruption == "eof":
        if language == "vue":
            lines = ["<script>", *lines, "</script>"]
        assert close_finder(language, lines, "span-1") is None
        return
    if interruption == "code":
        lines += ["keep_this_code()", f"{prefix} ]"]
    # A new structural opener cannot finish the preceding malformed block.
    lines += [f"{prefix} @beacon-close[", f"{prefix} id=span-1,", f"{prefix} ]"]
    start = len(lines) - 3
    if language == "vue":
        lines = ["<script>", *lines, "</script>"]
        start += 1
    assert close_finder(language, lines, "span-1") == (start, start + 2)


_MD_REAL_CLOSE = "<!--\n@beacon-close[\n  id=span-1,\n]\n-->\n"
_MD_FALSE_CLOSES = [
    "Prose mentions @beacon-close[\nKeep this text.\n",
    "@beacon-close[\n  id=span-1,\n]\n",  # Bare prose, no HTML comment.
    f"```markdown\n{_MD_REAL_CLOSE}```\n",
    f"~~~markdown\n{_MD_REAL_CLOSE}~~~\n",
    f"````markdown\n```\n{_MD_REAL_CLOSE}````\n",
    "<!--\nExample: @beacon-close[\n  id=span-1,\n]\n-->\n",
    "<!--\n@beacon-close[\n  id=other,\n]\n-->\nid=span-1,\n",
    "<!--\n@beacon-close[\n]\nid=span-1,\n-->\n",
    "<!--\n@beacon-close[\nid=span-1,\n-->\n",  # No metadata terminator.
]


@pytest.mark.parametrize("false_close", _MD_FALSE_CLOSES)
def test_markdown_close_requires_own_html_comment_outside_fences(close_finder, false_close):
    before = false_close + "Keep this paragraph.\n"
    lines = (before + _MD_REAL_CLOSE).splitlines()
    start = len(before.splitlines())
    assert close_finder("markdown", lines, "span-1") == (start, start + 4)


@pytest.mark.parametrize("close", [
    "<!-- @beacon-close[\n id=span-1,\n] -->",
    "<!--\n\n @beacon-close[\n id='span-1',\n]\n\n-->",
])
def test_markdown_close_accepts_inline_wrapper_and_blank_lines(close_finder, close):
    lines = close.splitlines()
    assert close_finder("markdown", lines, "span-1") == (0, len(lines) - 1)


@pytest.mark.parametrize("close", [
    "<!--\n@beacon-close[\nid=span-1,\n]",  # Missing HTML terminator.
    "<!--\n@beacon-close[\nid=span-1,\n]\nReal prose\n-->",
    "<!--\n@beacon-close[\nid=span-1,\n] --> Keep this prose",
])
def test_markdown_malformed_close_does_not_extend_into_prose(close_finder, close):
    assert close_finder("markdown", close.splitlines(), "span-1") is None


@pytest.mark.parametrize("planned", [False, True], ids=["legacy", "planned"])
@pytest.mark.parametrize("language,suffix,prefix,code", [
    ("python", ".py", "#", 'value = "@beacon-close["\nkeep = 1\n'),
    ("js_ts", ".js", "//", 'const value = "@beacon-close[";\nconst keep = 1;\n'),
    ("js_ts", ".ts", "//", 'const value: string = "@beacon-close[";\nconst keep = 1;\n'),
    ("vue", ".vue", "//", 'const value = "@beacon-close[";\nconst keep = 1;\n'),
    ("sql", ".sql", "--", "SELECT '@beacon-close[';\nSELECT 1;\n"),
    ("shell", ".sh", "#", "echo '@beacon-close['\necho keep\n"),
    ("yaml", ".yaml", "#", 'value: "@beacon-close["\nkeep: true\n'),
    ("markdown", ".md", None, "".join(_MD_FALSE_CLOSES)),
])
def test_final_label_delete_preserves_marker_mentions(
    tmp_path, planned, language, suffix, prefix, code,
):
    """Both tags.remove's plan seam and F12/sync_node's legacy writers are safe."""
    if language == "vue" and not nmc._HAVE_TREE_SITTER:
        pytest.skip("tree-sitter not available")
    if prefix:
        opening = "\n".join(
            f"{prefix} {body}" for body in
            ["@beacon[", "id=span-1,", "slice_labels=old,", "kind=span,", "]"]
        ) + "\n"
        closing = f"{prefix} @beacon-close[\n{prefix} id=span-1,\n{prefix} ]\n"
        code += f"{prefix} Example: @beacon-close[ is not a block\n"
    else:
        opening = "<!--\n@beacon[\nid=span-1,\nslice_labels=old,\nkind=span,\n]\n-->\n"
        closing = _MD_REAL_CLOSE
    head, tail = ("<script>\n", "</script>\n") if language == "vue" else ("", "")
    path = tmp_path / ("source" + suffix)
    path.write_text(head + opening + code + closing + tail, encoding="utf-8")
    writer = getattr(nmc, "update_beacon_from_node_" + language)
    kwargs = {}
    if planned:
        from beacon_plan_fixture import physical_operand
        kwargs = {
            "write_intent": object(),
            "write_plan": SimpleNamespace(
                success=True, planned_operation="delete", beacon_id="span-1",
                intended_slice_labels=(),
                **physical_operand(path, language, "span-1"),
            ),
        }
    result = writer(str(path), "source", "BEACON (AST)\nid: span-1\nkind: span\n---\n", **kwargs)
    assert result.get("operation") == "deleted_beacon", result
    assert path.read_text(encoding="utf-8") == head + code + tail
