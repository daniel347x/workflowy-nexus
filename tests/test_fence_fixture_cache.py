"""Fixture-speed changes must preserve AST bytes, source-copy damage and ownership."""

import ast
import copy
from pathlib import Path

import pytest

import test_cut_2a_fences as cut2a
import test_cut_2b_fences as cut2b


def _legacy_without_docstrings(node):
    node = copy.deepcopy(node)
    for part in ast.walk(node):
        if isinstance(part, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and part.body and isinstance(part.body[0], ast.Expr) \
                and isinstance(part.body[0].value, ast.Constant) \
                and isinstance(part.body[0].value.value, str):
            part.body.pop(0)
    return ast.dump(node)


@pytest.mark.parametrize("source", [
    '"module doc"\nclass C:\n    "class doc"\n    @staticmethod\n    async def f():\n        "function doc"\n        return "not a doc"\n',
    'if True:\n    def f():\n        "doc"\nelse:\n    class C:\n        "doc"\ntry:\n    pass\nexcept Exception:\n    def g():\n        "doc"\nfinally:\n    class D:\n        "doc"\n',
    'match value:\n    case 1:\n        def f():\n            "doc"\n            return 1\n',
])
def test_docstring_projection_is_byte_identical_without_mutating_input(source):
    tree = ast.parse(source)
    before = ast.dump(tree, include_attributes=True)
    assert cut2a._without_docstrings(tree) == _legacy_without_docstrings(tree)
    assert ast.dump(tree, include_attributes=True) == before


def test_server_docstring_projection_keeps_every_old_pin_byte():
    source = (Path(__file__).resolve().parents[1] / cut2a.SERVER).read_text(encoding="utf-8")
    tree = ast.parse(source)
    before = ast.dump(tree, include_attributes=True)
    assert cut2a._without_docstrings(tree) == _legacy_without_docstrings(tree)
    assert ast.dump(tree, include_attributes=True) == before


def test_preservation_scan_keys_include_content_path_and_skip_sets():
    source = 'async def f(paths):\n    async with coherence_lock_async(paths):\n        pass\n'
    path = cut2a.SERVER
    facts = cut2a._preservation_sites(path, source, frozenset(), frozenset())
    assert facts == cut2a._preservation_sites.__wrapped__(path, source, frozenset(), frozenset())
    assert facts[0] and facts[0][0][:2] == (path, "f")
    changed = source.replace("coherence_lock_async", "omitted_coherence_lock")
    assert not cut2a._preservation_sites(path, changed, frozenset(), frozenset())[0]
    other_path = cut2a.PACKAGE + "different.py"
    assert cut2a._preservation_sites(other_path, source, frozenset(), frozenset())[0][0][0] == other_path
    assert not cut2a._preservation_sites(path, source, frozenset(), frozenset({"f"}))[0]
    assert cut2a._preservation_sites(path, source, frozenset(), frozenset()) is facts
    assert all(isinstance(rows, tuple) for rows in facts)


def test_supplied_definition_map_keeps_hashes_and_separate_mutants():
    source = 'class C:\n    def f(self):\n        "doc"\n        return 1\n'
    definitions = cut2b.qualified_definitions(source)
    expected = cut2b.definition_record(source, "C.f")
    assert cut2b.definition_record(source, "C.f", definitions=definitions) == expected
    changed = source.replace("return 1", "return 2")
    assert cut2b.definition_record(changed, "C.f")["body_sha256"] != expected["body_sha256"]
    assert cut2b.definition_record(source, "C.f", definitions=definitions) == expected
