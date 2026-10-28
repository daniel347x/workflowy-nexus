"""U01 §9.2 -- the read-only beacon check engine (src/workflowy_mcp/beacon_integrity.py).

Fixtures (``beacon_check_fixtures``) are written under ``tmp_path``. Every
expected row set is the exact ``(line, code)`` set the engine must report.
"""
from __future__ import annotations

import builtins
import hashlib
import os
import re
import shutil
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import nexus_map_codebase as nmc  # noqa: E402
from nexus_map_codebase import _duplicate_beacon_ids, _vue_section_beacons, _vue_sfc_sections  # noqa: E402,F401
from beacon_mutation_contract import beacon_field_unknown_keys  # noqa: E402,F401
import beacon_check_fixtures as fx  # noqa: E402
import beacon_findings as bf  # noqa: E402
from workflowy_mcp import beacon_integrity as bi  # noqa: E402
from workflowy_mcp.client import api_client_nexus as acn  # noqa: E402


def _write(root, fname: str, content) -> str:
    os.makedirs(str(root), exist_ok=True)
    path = os.path.join(str(root), fname)
    if isinstance(content, bytes):
        with open(path, "wb") as f:
            f.write(content)
    else:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    return path


def _check(tmp_path, name: str) -> list[dict]:
    fname, text = {**fx.all_text_fixtures(), **fx.FINDING_FIXTURES}[name]
    sub = tmp_path / name
    path = _write(sub, fname, text)
    return bi.check_beacon_file(path, display_path=fname, repo_root=str(sub))


def _pairs(rows) -> set:
    return {(r["line"], r["code"]) for r in rows}


# ---------------------------------------------------------------------------
# §9.2 item 1: one test per §4 code (exact (line, code) sets).
# ---------------------------------------------------------------------------

EXPECTED_ROWS = {
    # Bounded own fields recover both incomplete records; neither receives
    # a destructive extent or borrows the other's identity/kind.
    "unterminated_block": {(14, "unterminated_block"), (14, "writer_anchor_mismatch"),
                           (23, "unterminated_block"), (23, "writer_anchor_mismatch"),
                           (23, "slice_labels_derived"), (23, "empty_comment")},
    "unknown_kind": {(1, "unknown_kind")},
    "missing_id_py": {(1, "missing_id")},
    "missing_id_md": {(4, "missing_id")},
    "unknown_field_key": {(1, "unknown_field_key"), (1, "slice_labels_derived")},
    "legacy_field": {(1, "legacy_field"), (12, "legacy_field")},
    "duplicate_id": {(11, "duplicate_id")},
    "block_not_mapped_py": {(3, "block_not_mapped")},
    "block_not_mapped_sql": {(1, "block_not_mapped")},
    # U09 aligns .bash dispatch with the existing shell beacon capability.
    "block_not_mapped_bash": set(),
    "two_blocks_one_symbol": {(13, "two_blocks_one_symbol"), (13, "writer_anchor_ambiguous")},
    "writer_anchor_ambiguous": {(1, "noncanonical_terminator"), (13, "two_blocks_one_symbol"), (13, "writer_anchor_ambiguous")},
    "kind_changed_by_mapper": {(6, "kind_changed_by_mapper"), (6, "span_without_close")},
    "no_slice_labels": {(1, "no_slice_labels")},
    "slice_labels_derived": {(1, "slice_labels_derived")},
    "span_without_close": {(1, "span_without_close")},
    "orphan_close_typo": {(1, "span_without_close"), (8, "orphan_close")},
    "orphan_close_mention": set(),
    "orphan_close_above": {(1, "orphan_close")},
    "block_end_comma": {(1, "noncanonical_terminator")},
    "block_end_space": {(1, "noncanonical_terminator")},
    "block_end_js_block_comment": set(),
    "writer_anchor_mismatch": set(),
    "writer_target_ambiguous": {(9, "writer_target_ambiguous"), (13, "writer_target_ambiguous")},
    "ambiguous_js": {(7, "writer_target_ambiguous"), (10, "writer_target_ambiguous")},
    "ambiguous_vue": {(12, "writer_target_ambiguous"), (15, "writer_target_ambiguous")},
    "comment_rejected": {(1, "comment_rejected"), (11, "comment_rejected")},
    "empty_comment": {(1, "empty_comment")},
    "mapper_parse_error": {(0, "mapper_parse_error")},
    "info_rows": {(1, "empty_comment"), (10, "empty_comment"), (19, "empty_comment")},
    "vue_outline": {
        (18, "block_not_mapped"), (39, "empty_comment"), (51, "duplicate_id"), (51, "empty_comment"),
        (4, "vue_capability"), (18, "vue_capability"), (51, "vue_capability"), (51, "span_without_close"),
    },
}


@pytest.mark.parametrize("name", sorted(EXPECTED_ROWS))
def test_fixture_rows_exact(tmp_path, name):
    rows = _check(tmp_path, name)
    assert _pairs(rows) == EXPECTED_ROWS[name]
    for row in rows:
        spec = bi.BEACON_CHECK_CODES[row["code"]]
        assert (row["severity"], row["view"], row["authority"]) == (
            spec["severity"], spec["view"], spec["authority"],
        )
        assert set(row) == {"file", "line", "code", "severity", "view", "authority", "beacon_id", "message", "detail",
                            "finding_id", "kind", "producer", "observed", "members", "fix"}
        assert row["observed"]["file_sha256"]
        assert row["finding_id"].startswith("bf1:")
        assert row["file"] == fx.all_text_fixtures()[name][0] and row["message"].endswith((".", ")"))


def test_file_unreadable(tmp_path):
    fname, data = fx.UNREADABLE_FIXTURE
    path = _write(tmp_path, fname, data)
    rows = bi.check_beacon_file(path, display_path=fname)
    assert _pairs(rows) == {(0, "file_unreadable")}
    assert rows[0]["detail"]["exception"].startswith("UnicodeDecodeError: ")
    assert rows[0]["observed"]["file_sha256"] is None
    assert rows[0]["observed"]["unreadable_identity"]["undecodable_bytes_sha256"] == hashlib.sha256(data).hexdigest()
    assert rows[0]["fix"]["class"] == "manual"


def test_every_code_is_exercised(tmp_path):
    covered = {code for rows in EXPECTED_ROWS.values() for _line, code in rows}
    covered |= {"file_unreadable", "file_missing"}  # their own tests
    # These adapter-disagreement branches are retained but the one structural
    # authority no longer disagrees with itself on a comma terminator.
    covered |= {"block_end_mismatch", "writer_anchor_mismatch", "writer_anchor_foreign"}  # injected witnesses below
    covered |= {"check_failed"}  # builder now; containment is the paging concern
    covered |= {r["code"] for name in fx.FINDING_FIXTURES for r in _check(tmp_path, name)}
    covered |= {"missing_wrapper"}  # actual Markdown variant matrix below
    assert covered == set(bi.BEACON_CHECK_CODES)


def test_code_table_is_the_masterspec_table():
    assert bi.SEVERITY_ORDER == ("error", "warning", "info")
    assert bi.BEACON_CHECK_CODES is bf.BEACON_CHECK_CODES
    assert {c: (s["severity"], s["view"]) for c, s in list(bi.BEACON_CHECK_CODES.items())[:22]} == {
        "file_unreadable": ("error", "file"),
        "file_missing": ("warning", "file"),
        "mapper_parse_error": ("error", "mapper"),
        "unterminated_block": ("error", "parser"),
        "unknown_kind": ("error", "parser"),
        "missing_id": ("error", "parser"),
        "unknown_field_key": ("warning", "parser"),
        "legacy_field": ("info", "parser"),
        "duplicate_id": ("error", "mapper"),
        "block_not_mapped": ("error", "mapper"),
        "two_blocks_one_symbol": ("error", "mapper"),
        "kind_changed_by_mapper": ("warning", "mapper"),
        "no_slice_labels": ("warning", "mapper"),
        "slice_labels_derived": ("info", "mapper"),
        "span_without_close": ("info", "parser"),
        "orphan_close": ("info", "parser"),
        "block_end_mismatch": ("error", "writer"),
        "writer_anchor_mismatch": ("error", "writer"),
        "writer_anchor_ambiguous": ("error", "writer"),
        "writer_target_ambiguous": ("error", "writer"),
        "comment_rejected": ("info", "comment"),
        "empty_comment": ("info", "comment"),
    }
    assert {c: (s["severity"], s["view"]) for c, s in list(bi.BEACON_CHECK_CODES.items())[22:]} == {
        "check_failed": ("error", "file"), "noncanonical_terminator": ("info", "parser"),
        "missing_wrapper": ("error", "parser"), "mixed_comment_ownership": ("warning", "parser"),
        "duplicate_field": ("warning", "parser"), "inline_field_ambiguity": ("warning", "parser"),
        "pair_ambiguous": ("warning", "parser"), "lexical_uncertainty": ("warning", "parser"),
        "writer_anchor_foreign": ("error", "writer"), "implicit_kind_promoted": ("info", "mapper"),
        "id_unlike_symbol": ("info", "attachment"), "vue_capability": ("info", "writer"),
    }


def test_row_details(tmp_path):
    dup = _check(tmp_path, "duplicate_id")[0]
    assert dup["beacon_id"] == "fx@dup"
    assert dup["detail"]["lines"] == [1, 11] and dup["detail"]["mapped_records"] == 2
    assert [s["new_id"] for s in dup["detail"]["suggested_ids"]] == ["fx@dup-2", "fx@dup-3"]
    orphan = [r for r in _check(tmp_path, "orphan_close_typo") if r["code"] == "orphan_close"][0]
    assert orphan["beacon_id"] == "fx@span_y"
    assert _check(tmp_path, "writer_anchor_mismatch") == []  # Ordinary comments are shared gap permission.
    assert _pairs(_check(tmp_path, "block_end_comma")) == {(1, "noncanonical_terminator")}
    kinds = [r for r in _check(tmp_path, "unknown_kind")][0]
    assert kinds["detail"] == {"kind_raw": "asts"}
    keys = [r for r in _check(tmp_path, "unknown_field_key") if r["code"] == "unknown_field_key"][0]
    assert keys["detail"] == {"keys": ["slice_label"]}
    legacy = _check(tmp_path, "legacy_field")
    assert [r["detail"]["keys"] for r in legacy] == [["role"], ["comments"]]
    assert legacy[1]["detail"]["planner"]["status"] == "planned"
    rejected = _check(tmp_path, "comment_rejected")
    assert "contains forbidden token '*/'" in rejected[0]["message"]
    assert "HTML" not in rejected[1]["message"] and "transport-entity" in rejected[1]["message"]
    two = [r for r in _check(tmp_path, "two_blocks_one_symbol")][0]
    assert two["detail"]["ids"] == ["fx@stack_b", "fx@stack_a"]
    assert two["detail"]["planner"]["reason"] == "distinct_ids"
    assert "survivor/fields" in two["message"]
    assert _pairs(_check(tmp_path, "writer_anchor_ambiguous")) == EXPECTED_ROWS["writer_anchor_ambiguous"]
    parse_error = _check(tmp_path, "mapper_parse_error")[0]
    assert parse_error["detail"]["name"].startswith("⚠️ Parse Error")


def test_retained_writer_codes_on_injected_adapter_disagreement(tmp_path, monkeypatch):
    """Inject supplied authority evidence, not an obsolete tuple-view parser."""
    with monkeypatch.context() as patch:
        real = nmc._beacon_edit_records
        def no_comma_extent(snapshot, language):
            records = real(snapshot, language)
            for record in records:
                if record["fields"].get("id") == "fx@end_comma":
                    record["line_extent"] = None
            return records
        patch.setattr(nmc, "_beacon_edit_records", no_comma_extent)
        rows = _check(tmp_path, "block_end_comma")
        end = next(r for r in rows if r["code"] == "block_end_mismatch")
        assert end["detail"] == {"parser_end_line": 6, "writer_end_line": None}
        mismatch = next(r for r in rows if r["code"] == "writer_anchor_mismatch")
        assert mismatch["detail"] == {"target_line": 7, "symbol": "end_comma", "writer_extent": None}
        assert "permission to create a second block" in mismatch["message"]
        assert _check(tmp_path, "clean_py") == []
    with monkeypatch.context() as patch:
        real = nmc.map_source_snapshot
        def omit_record(snapshot, *args, **kwargs):
            tree = real(snapshot, *args, **kwargs)
            for node in bi._walk(tree):
                if node.get("ast_qualname") == "gap":
                    node["note"] = "AST_QUALNAME: gap"
            return tree
        patch.setattr(nmc, "map_source_snapshot", omit_record)
        rows = _check(tmp_path, "writer_anchor_mismatch")
        foreign = next(r for r in rows if r["code"] == "writer_anchor_foreign")
        assert foreign["detail"] == {"target_line": 8, "symbol": "gap"}
        assert foreign["members"][0]["anchor"]["ast_qualname"] == "gap"
        assert _check(tmp_path, "clean_py") == []


# ---------------------------------------------------------------------------
# §9.2 items 2-3: clean fixtures and the Vue trap.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(fx.CLEAN_FIXTURES))
def test_clean_fixture_has_no_rows(tmp_path, name):
    rows = _check(tmp_path, name)
    assert _pairs(rows) == ({(4, "vue_capability"), (34, "vue_capability")} if name == "clean_vue" else set())


def test_vue_trap_template_span_is_not_a_duplicate(tmp_path):
    rows = _check(tmp_path, "vue_trap")
    assert _pairs(rows) == {(3, "vue_capability")}
    # Structural JS comment ownership no longer sees an HTML template block.
    # Per-section writer ownership is still separately tested/migrated in U01.
    fname, text = fx.VUE_TRAP_FIXTURE
    _lines, writer_beacons = nmc._parse_disk_beacons_for_language("vue", _write(tmp_path, fname, text))
    assert [b["id"] for b in writer_beacons] == ["trap@tpl_only"]


# ---------------------------------------------------------------------------
# §9.2 items 4-5: read-only proof and forbidden callables.
# ---------------------------------------------------------------------------


def _all_fixture_files(root) -> list[tuple[str, str, str]]:
    out = []
    for name, (fname, text) in {**fx.all_text_fixtures(), **fx.FINDING_FIXTURES}.items():
        sub = os.path.join(str(root), name)
        out.append((name, _write(sub, fname, text), sub))
    fname, data = fx.UNREADABLE_FIXTURE
    out.append(("unreadable", _write(os.path.join(str(root), "unreadable"), fname, data), str(root)))
    return out


def _fingerprint(path: str) -> tuple[str, int]:
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return digest, os.stat(path).st_mtime_ns


class _WriteAttempt(AssertionError):
    pass


def _install_write_guard(monkeypatch):
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(ch in mode for ch in "wax+"):
            raise _WriteAttempt(f"open({file!r}, {mode!r})")
        return real_open(file, mode, *args, **kwargs)

    def refuse(name):
        def _refuse(*a, **k):
            raise _WriteAttempt(f"{name}{a!r}")
        return _refuse

    monkeypatch.setattr(builtins, "open", guarded_open)
    for mod, name in ((os, "replace"), (os, "rename"), (os, "remove"), (os, "unlink"), (shutil, "move")):
        monkeypatch.setattr(mod, name, refuse(f"{mod.__name__}.{name}"))


def test_checks_are_read_only(tmp_path, monkeypatch):
    files = _all_fixture_files(tmp_path)
    before = {path: _fingerprint(path) for _n, path, _r in files}
    results = {}
    with monkeypatch.context() as m:
        _install_write_guard(m)
        for name, path, root in files:
            results[name] = bi.check_beacon_file(path, display_path=os.path.basename(path), repo_root=root)
    assert {path: _fingerprint(path) for _n, path, _r in files} == before
    assert sum(len(r) for r in results.values()) > 50
    # The guard is live: a write attempt raises while it is installed.
    with monkeypatch.context() as m:
        _install_write_guard(m)
        with pytest.raises(_WriteAttempt):
            open(os.path.join(str(tmp_path), "probe.txt"), "w")


_FORBIDDEN = (
    "normalize_duplicate_ast_beacons_in_file",
    "_normalize_duplicate_ast_beacons_python",
    "_normalize_duplicate_ast_beacons_js_ts",
    "_normalize_duplicate_ast_beacons_markdown",
    "_normalize_duplicate_ast_beacons",
    "normalize_beacon_comment_field_aliases_in_file",
    "repair_markdown_c0_controls_on_disk",
    "_execute_beacon_write_plan",
    "_write_markdown_lines_sanitized",
    "commit_beacon_edit_plan",
)


def test_forbidden_callables_are_never_entered(tmp_path, monkeypatch):
    # The witness compares behavior with/without forbidden writers, not the
    # wall clock. Crossing a second between two runs must not change its oracle.
    monkeypatch.setattr(nmc, "_warn_name", lambda base: f"{base} [TS]")
    files = _all_fixture_files(tmp_path)
    expected = {
        name: bi.check_beacon_file(path, display_path=os.path.basename(path), repo_root=root)
        for name, path, root in files
    }
    entered: list[str] = []

    def trap(name):
        def _trap(*a, **k):
            entered.append(name)
            raise AssertionError(f"forbidden callable entered: {name}")
        return _trap

    updaters = sorted(n for n in dir(nmc) if n.startswith("update_beacon_from_node"))
    assert len(updaters) >= 7
    for name in _FORBIDDEN + tuple(updaters):
        monkeypatch.setattr(nmc, name, trap(name))
    monkeypatch.setattr(acn, "_compute_beacon_write_plan", trap("_compute_beacon_write_plan"))
    for name, path, root in files:
        got = bi.check_beacon_file(path, display_path=os.path.basename(path), repo_root=root)
        assert got == expected[name], name
    assert entered == []


def test_engine_source_has_no_second_authority():
    """Both modules consume recorded geometry, with one code table and no IO in core."""
    import ast
    paths = [os.path.join(_SRC, "workflowy_mcp", "beacon_integrity.py"), os.path.join(_REPO_ROOT, "beacon_findings.py")]
    sources = [open(path, encoding="utf-8").read() for path in paths]
    for source in sources:
        code = "\n".join(l for l in source.splitlines() if not l.lstrip().startswith("#"))
        assert not re.search(r'startswith\("\]"\)|== "\]"|@beacon-close\[|@beacon\[|fnmatch|nexusignore|splitlines', code)
        calls = [n.func.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        assert not set(calls) & set(_FORBIDDEN)
    assert "def _code(" not in sources[0] and sources[1].count("BEACON_CHECK_CODES =") == 1
    assert "open(" not in sources[1] and "workflowy_mcp" not in sources[1]
    for retired in ("_parse_disk_beacons_for_language(", "map_codebase(", "_find_anchor_matched_beacons("):
        assert retired not in sources[0]


# ---------------------------------------------------------------------------
# §9.2 item 6: the prefilter.
# ---------------------------------------------------------------------------


def test_marker_free_file_is_never_mapped(tmp_path, monkeypatch):
    calls: list = []
    real = nmc.map_source_snapshot

    def spy(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(nmc, "map_source_snapshot", spy)
    assert _check(tmp_path, "marker_free") == []
    assert calls == []
    _check(tmp_path, "empty_comment")
    assert len(calls) == 1


def test_extension_outside_the_writers_table_is_skipped(tmp_path):
    path = _write(tmp_path, "notes.txt", "# @beacon[\n#   id=x@1,\n# ]\n")
    assert bi.check_beacon_file(path) == []


# ---------------------------------------------------------------------------
# Fault matrix (U01 §7).
# ---------------------------------------------------------------------------


def test_vue_script_section_sentinel_is_a_mapper_parse_error(tmp_path, monkeypatch):
    # tree-sitter does not raise on bad script text (it builds ERROR nodes),
    # so the section sentinel is provoked by making the shared JS/TS outline
    # context raise.
    def boom(**_k):
        raise RuntimeError("script outline failed")

    monkeypatch.setattr(nmc.JsTsOutlineContext, "from_source", staticmethod(boom))
    rows = _check(tmp_path, "clean_vue")
    errors = [r for r in rows if r["code"] == "mapper_parse_error"]
    assert len(errors) == 1 and errors[0]["line"] == 0
    assert errors[0]["detail"]["name"].startswith("⚠️ Parse Error (VUE script JS/TS)")
    assert "section" in errors[0]["message"]
    # the script block no longer reaches the map; template and style still do
    assert _pairs(rows) == {(0, "mapper_parse_error"), (21, "block_not_mapped"), (4, "vue_capability"), (34, "vue_capability")}


def test_vue_section_split_failure_is_one_row(tmp_path, monkeypatch):
    def boom(_source_bytes):
        raise RuntimeError("no vue grammar")

    monkeypatch.setattr(nmc, "_parse_vue_sfc_tree", boom)
    rows = _check(tmp_path, "clean_vue")
    assert _pairs(rows) == {(0, "mapper_parse_error")}
    assert rows[0]["detail"] == {"exception": "RuntimeError: no vue grammar"}


def test_map_source_snapshot_exception_is_a_row_without_source_rereads(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("mapper down")

    monkeypatch.setattr(nmc, "map_source_snapshot", boom)
    rows = _check(tmp_path, "block_end_comma")
    assert _pairs(rows) == {(0, "mapper_parse_error"), (1, "noncanonical_terminator")}


# ---------------------------------------------------------------------------
# §9.2 item 7: collect_mapped_beacon_files.
# ---------------------------------------------------------------------------


def _cache(root: str) -> dict:
    def node(nid, parent, name, note):
        return {"id": nid, "parent_id": parent, "name": name, "note": note}

    nodes = [
        node("root", None, "🗺️ Source Map: repo", f"Root: {root}"),
        node("pkg", "root", "📂 pkg", "Path: pkg"),
        node("a_py", "pkg", "🐍 a.py", "Path: a.py\nLINE COUNT: 3"),
        node("a_md", "pkg", "📘 a.md", "Path: a.md"),
        node("notes", "pkg", "📄 notes.txt", "Path: notes.txt"),
        node("gone", "pkg", "🐍 gone.py", "Path: gone.py"),
        node("fn", "a_py", "ƒ f()", "AST_QUALNAME: f\nPath: not-a-header.py"),
        node("top_py", "root", "🐍 top.py", "Path: top.py"),
        node("lost", None, "🐍 lost.py", "Path: lost.py"),
    ]
    return {n["id"]: n for n in nodes}


def test_collect_mapped_beacon_files(tmp_path):
    root = str(tmp_path / "repo")
    _write(os.path.join(root, "pkg"), "a.py", "x = 1\n")
    _write(os.path.join(root, "pkg"), "a.md", "# A\n")
    _write(os.path.join(root, "pkg"), "notes.txt", "plain\n")
    _write(root, "top.py", "y = 2\n")
    cache = _cache(root)

    whole = bi.collect_mapped_beacon_files(cache, "root")
    assert whole["root_abs"] == root
    assert [(f["display_path"], f["node_id"]) for f in whole["files"]] == [
        ("pkg/a.md", "a_md"), ("pkg/a.py", "a_py"), ("top.py", "top_py"),
    ]
    assert all(os.path.isfile(f["abs_path"]) for f in whole["files"])
    assert [(m["display_path"], m["node_id"]) for m in whole["missing"]] == [("pkg/gone.py", "gone")]
    assert whole["skipped"] == 1

    folder = bi.collect_mapped_beacon_files(cache, "pkg")
    assert folder["root_abs"] == root
    assert [f["node_id"] for f in folder["files"]] == ["a_md", "a_py"]

    single = bi.collect_mapped_beacon_files(cache, "a_py")
    assert [(f["display_path"], f["node_id"]) for f in single["files"]] == [("pkg/a.py", "a_py")]
    assert single["missing"] == [] and single["skipped"] == 0

    # A FILE node with no Root: ancestor: the resolver raises -> missing.
    lost = bi.collect_mapped_beacon_files(cache, "lost")
    assert lost["root_abs"] is None and lost["files"] == []
    assert [m["node_id"] for m in lost["missing"]] == ["lost"] and lost["missing"][0]["reason"]


def test_missing_mapped_file_is_a_file_missing_row(tmp_path):
    root = str(tmp_path / "repo")
    _write(os.path.join(root, "pkg"), "a.py", fx.PROBLEM_FIXTURES["empty_comment"][1])
    _write(os.path.join(root, "pkg"), "a.md", "# A\n")
    collected = bi.collect_mapped_beacon_files(_cache(root), "pkg")
    report = bi.run_beacon_check(collected["files"], repo_root=root, missing=collected["missing"],
                                 skipped=collected["skipped"], min_severity="info")
    assert [(r["file"], r["line"], r["code"]) for r in report["rows"]] == [
        ("pkg/gone.py", 0, "file_missing"), ("pkg/a.py", 1, "empty_comment"),
    ]
    assert report["counts"]["files_missing"] == 1 and report["counts"]["files_skipped"] == 1


# ---------------------------------------------------------------------------
# §9.2 item 8: run_beacon_check and the summary line.
# ---------------------------------------------------------------------------


def _entries(tmp_path, *names_as) -> list[dict]:
    out = []
    for name, as_name in names_as:
        _fname, text = fx.all_text_fixtures()[name]
        path = _write(tmp_path / "files", as_name, text)
        out.append({"abs_path": path, "display_path": as_name, "node_id": None})
    return out


def test_summary_line_forms(tmp_path, monkeypatch):
    clean = bi.run_beacon_check(_entries(tmp_path, ("clean_py", "clean.py")))
    assert clean["summary_line"] == "1 file · 0 errors · 0 warnings · 0 info"
    assert clean["rows"] == [] and clean["rows_truncated"] == 0

    one = bi.run_beacon_check(_entries(tmp_path, ("no_slice_labels", "n.py"), ("empty_comment", "e.py")))
    assert one["summary_line"] == (
        "2 files · 0 errors · 1 warning · 1 info "
        "(n.py:1 no_slice_labels — Block plainid maps with no slice label, so it is in no slice.)"
    )

    # The public disagreement code remains testable without keeping an obsolete
    # ordinary-comment disagreement in production.
    long_one = bi.run_beacon_check(_entries(tmp_path, ("block_not_mapped_py", "g.py")))
    message = long_one["rows"][0]["message"]
    assert len(message) > 110
    assert long_one["summary_line"] == (
        f"1 file · 1 error · 0 warnings · 0 info (g.py:3 block_not_mapped — {message[:107]}...)"
    )

    many = bi.run_beacon_check(_entries(
        tmp_path,
        ("block_end_comma", "a.py"), ("block_end_space", "b.py"), ("duplicate_id", "c.py"),
        ("unknown_kind", "d.py"), ("missing_id_py", "e.py"), ("unknown_field_key", "f.py"),
    ))
    assert many["summary_line"] == (
        "6 files · 3 errors · 1 warning · 3 info "
        "(duplicate_id ×1, missing_id ×1, unknown_kind ×1, unknown_field_key ×1)"
    )
    two_codes = bi.run_beacon_check(_entries(tmp_path, ("block_end_comma", "a.py")))
    assert two_codes["summary_line"] == (
        "1 file · 0 errors · 0 warnings · 1 info"
    )


def test_min_severity_filters_rows_not_counts(tmp_path, monkeypatch):
    entries = _entries(tmp_path, ("unknown_field_key", "f.py"), ("unknown_kind", "k.py"))
    with monkeypatch.context() as m:
        _install_write_guard(m)
        by = {s: bi.run_beacon_check(entries, min_severity=s) for s in bi.SEVERITY_ORDER}
        selected = bi.run_beacon_check(entries, min_severity="info", codes=["unknown_kind"],
                                       finding_ids=[by["error"]["rows"][0]["finding_id"]])
    assert [r["code"] for r in by["error"]["rows"]] == ["unknown_kind"]
    assert [r["code"] for r in by["warning"]["rows"]] == ["unknown_kind", "unknown_field_key"]
    assert [r["code"] for r in by["info"]["rows"]] == ["unknown_kind", "unknown_field_key", "slice_labels_derived"]
    assert selected["rows"] == by["error"]["rows"] and selected["eligible_total"] == 1
    assert selected["counts"] == by["info"]["counts"] and selected["summary_line"] == by["info"]["summary_line"]
    counts = [by[s]["counts"] for s in bi.SEVERITY_ORDER]
    assert counts[0] == counts[1] == counts[2]
    assert counts[0]["by_severity"] == {"error": 1, "warning": 1, "info": 1}
    assert counts[0]["by_code"] == {"slice_labels_derived": 1, "unknown_field_key": 1, "unknown_kind": 1}
    assert counts[0]["files_checked"] == 2 and counts[0]["files_with_problems"] == 2
    assert all(by[s]["min_severity"] == s for s in bi.SEVERITY_ORDER)


def test_max_rows_keeps_errors_first_and_file_order(tmp_path, monkeypatch):
    entries = _entries(tmp_path, ("info_rows", "a_info.py"), ("unknown_kind", "z_error.py"))
    with monkeypatch.context() as m:
        _install_write_guard(m)
        report = bi.run_beacon_check(entries, min_severity="info", max_rows=2)
        next_page = bi.run_beacon_check(entries, min_severity="info", max_rows=2, offset=report["next_offset"])
        first = bi.run_beacon_check(entries, min_severity="info", max_rows=1)
    assert [(r["file"], r["line"], r["code"]) for r in report["rows"]] == [
        ("z_error.py", 1, "unknown_kind"), ("a_info.py", 1, "empty_comment"),
    ]
    assert report["rows_truncated"] == 2
    assert report["counts"]["by_severity"] == {"error": 1, "warning": 0, "info": 3}
    assert report["eligible_total"] == 4 and report["has_more"] is True and report["next_offset"] == 2
    assert [(r["line"], r["code"]) for r in next_page["rows"]] == [(10, "empty_comment"), (19, "empty_comment")]
    assert next_page["has_more"] is False and next_page["next_offset"] is None
    assert next_page["counts"] == report["counts"] and next_page["summary_line"] == report["summary_line"]
    assert first["rows"][0]["code"] == "unknown_kind"


@pytest.mark.parametrize("kwargs", [
    {"min_severity": "fatal"}, {"min_severity": None}, {"max_rows": 0}, {"max_rows": -1},
    {"max_rows": True}, {"max_rows": "3"},
    {"offset": -1}, {"offset": True}, {"offset": "1"}, {"offset": None},
    {"codes": "unknown_kind"}, {"codes": ["typo"]}, {"codes": [3]}, {"codes": [[]]},
    {"finding_ids": "bf1:x"}, {"finding_ids": [1]}, {"finding_ids": [None]},
])
def test_run_beacon_check_rejects_bad_parameters(kwargs):
    with pytest.raises(ValueError):
        bi.run_beacon_check([], **kwargs)


def test_summary_line_bounds_the_code_list():
    def row(code, severity):
        return {"severity": severity, "code": code, "file": "f", "line": 1, "message": "m"}

    rows = [row("e1", "error")] * 3 + [row("e2", "error")] + [row("w1", "warning")] * 5 + [
        row("w2", "warning"), row("w3", "warning"), row("i1", "info"),
    ]
    assert bi.beacon_check_summary_line(rows, 3) == (
        "3 files · 4 errors · 7 warnings · 1 info (e1 ×3, e2 ×1, w1 ×5, w2 ×1, ...)"
    )


@pytest.mark.parametrize("fixture,code,klass,action", [
    ("block_end_comma", "noncanonical_terminator", "auto", "normalize"),
    ("legacy_field", "legacy_field", "auto", "normalize"),
    ("identical_field", "duplicate_field", "auto", "normalize"),
    ("unterminated_block", "unterminated_block", "auto", "insert_bracket"),
    ("mixed_wrapper", "mixed_comment_ownership", "choice", "edit_metadata"),
    ("inline_ambiguity", "inline_field_ambiguity", "choice", "select_alternative"),
    ("conflicting_field", "duplicate_field", "choice", "set_field"),
    ("unknown_field_key", "unknown_field_key", "choice", "rename_field"),
    ("duplicate_id", "duplicate_id", "choice", "assign_ids"),
    ("same_id_stack", "two_blocks_one_symbol", "auto", "merge"),
    ("two_blocks_one_symbol", "two_blocks_one_symbol", "choice", "merge"),
    ("module_ast", "block_not_mapped", "choice", "relocate"),
    ("unlike_symbol", "id_unlike_symbol", "choice", "rename_id"),
    ("span_without_close", "span_without_close", "choice", "add_close"),
    ("orphan_close_typo", "orphan_close", "choice", "retag_close"),
    ("pair_ambiguity", "pair_ambiguous", "choice", "pair_close"),
    ("kind_changed_by_mapper", "kind_changed_by_mapper", "choice", "convert_to_span"),
    ("comment_rejected", "comment_rejected", "choice", "replace_comment"),
    ("empty_comment", "empty_comment", "choice", "append_comment"),
    ("uncertain", "lexical_uncertainty", "choice", "edit_metadata"),
    ("clean_vue", "vue_capability", "choice", "relocate"),
    ("mapper_parse_error", "mapper_parse_error", "manual", "retry"),
])
def test_catalogue_action_schema_and_witness(tmp_path, monkeypatch, fixture, code, klass, action):
    fname, text = {**fx.all_text_fixtures(), **fx.FINDING_FIXTURES}[fixture]
    path = _write(tmp_path, fname, text)
    before = _fingerprint(path)
    with monkeypatch.context() as patch:
        _install_write_guard(patch)
        rows = bi.check_beacon_file(path)
    assert _fingerprint(path) == before
    row = next(r for r in rows if r["code"] == code and r["fix"]["class"] == klass)
    assert row["observed"]["file_sha256"] == before[0]
    offered = {r["resolution"]: r for r in row["fix"]["resolutions"]}
    assert {"keep", action} <= offered.keys()
    for resolution in offered.values():
        assert {"resolution", "summary", "effect", "params_schema"} <= resolution.keys()
        schema = resolution["params_schema"]
        assert schema["type"] == "object" and set(schema["required"]) <= schema["properties"].keys()
    if row["members"]:
        assert all(m["block_hash"] and m["opener_start_byte"] is not None and m["read_range"] for m in row["members"])
    if code == "id_unlike_symbol":
        assert row["severity"] == "info" and row["detail"]["advisory"] is True
        assert row["detail"]["named_symbols"][0]["ast_qualname"] == "g"
    if code == "two_blocks_one_symbol":
        assert len(row["members"]) == 2 and row["detail"]["planner"]["status"] == ("planned" if klass == "auto" else "skipped")


def test_physical_duplicate_keeps_unmapped_copy_findings(tmp_path):
    rows = _check(tmp_path, "unmapped_duplicate")
    assert {(10, "block_not_mapped"), (10, "empty_comment"), (10, "duplicate_id")} <= _pairs(rows)
    duplicate = next(r for r in rows if r["code"] == "duplicate_id")
    assert duplicate["detail"]["lines"] == [1, 10] and duplicate["detail"]["mapped_records"] == 1
    assert len(duplicate["members"]) == 2
    assert duplicate["members"][0]["anchor"]["ast_qualname"] == "f"
    assert duplicate["members"][1]["anchor"] is None
    assert not any(r["code"] == "block_not_mapped" and r["line"] == 1 for r in rows)


def test_duplicate_stack_label_findings_use_the_correct_physical_copy(tmp_path):
    text = "\n".join([
        "# @beacon[id=same, kind=ast, slice_labels=#valid, comment=Identical.]",
        "# @beacon[id=same, kind=ast, comment=Identical.]", "def f():", "    return 1", "",
    ])
    path = _write(tmp_path, "physical.py", text)
    rows = bi.check_beacon_file(path)
    labels = [r for r in rows if r["code"] == "no_slice_labels"]
    assert [r["line"] for r in labels] == [2]
    assert labels[0]["members"][0]["fields"].get("slice_labels") is None
    assert next(r for r in rows if r["code"] == "duplicate_id")["detail"]["mapped_records"] == 2


def test_pair_actions_name_actual_open_and_close_members(tmp_path):
    open_only = next(r for r in _check(tmp_path, "span_without_close") if r["code"] == "span_without_close")
    offered = {r["resolution"]: r for r in open_only["fix"]["resolutions"]}
    assert "add_close" in offered and "retag_close" not in offered and "pair_close" in offered
    schema = offered["pair_close"]["params_schema"]
    assert schema["properties"]["close"]["enum"] == []
    assert "open_only" in schema["properties"] and "new_id" in schema["properties"]
    typo = _check(tmp_path, "orphan_close_typo")
    opening = next(r for r in typo if r["code"] == "span_without_close")
    offered = {r["resolution"]: r for r in opening["fix"]["resolutions"]}
    schema = offered["pair_close"]["params_schema"]["properties"]
    assert schema["opener"]["enum"] == [0] and schema["close"]["enum"] == [1]
    assert opening["members"][1]["kind"] == "close"
    assert opening["members"][1]["id"] != opening["members"][0]["id"]
    assert {"new_id", "open_only"} <= schema.keys()
    duplicate = next(r for r in _check(tmp_path, "pair_ambiguity") if r["code"] == "duplicate_id")
    assignment = next(o for o in duplicate["fix"]["resolutions"] if o["resolution"] == "assign_ids")
    item = assignment["params_schema"]["properties"]["new_ids"]["items"]["properties"]
    assert {"member", "new_id", "close", "open_only"} <= item.keys()
    assert len(duplicate["detail"]["close_candidates"]) == 1
    assert duplicate["detail"]["close_candidates"][0]["kind"] == "close"
    resemblance = next(r for r in _check(tmp_path, "unlike_symbol") if r["code"] == "id_unlike_symbol")
    rename = next(o for o in resemblance["fix"]["resolutions"] if o["resolution"] == "rename_id")
    assert {"close", "open_only", "new_id"} <= rename["params_schema"]["properties"].keys()


def test_uncertain_marker_without_field_geometry_has_exact_manual_guidance(tmp_path, monkeypatch):
    path = _write(tmp_path, "uncertain_marker.py", 'example = """\n# @beacon[\n')
    before = _fingerprint(path)
    with monkeypatch.context() as patch:
        _install_write_guard(patch)
        rows = bi.check_beacon_file(path)
    row = next(r for r in rows if r["code"] == "lexical_uncertainty")
    assert row["fix"]["class"] == "manual"
    assert [r["resolution"] for r in row["fix"]["resolutions"]] == ["keep"]
    assert "does not establish a safe metadata range" in row["fix"]["instructions"]
    assert _fingerprint(path) == before


@pytest.mark.parametrize("variant", ["prose_arrow", "mermaid", "html_comment", "heading_after_stray", "canonical", "inline", "blank_gap"])
def test_markdown_wrapper_ownership_variants_and_controls(tmp_path, monkeypatch, variant):
    opening = ["<!--", "@beacon[", "  id=demo@heading,", "  kind=ast,", "  slice_labels=demo,", "  comment=Description.,", "]"]
    damaged = {
        "prose_arrow": ["# Hidden", "ordinary prose -->"],
        "mermaid": ["# Hidden", "```mermaid", "A --> B", "```"],
        "html_comment": ["# Hidden", "<!-- later comment -->"],
        "heading_after_stray": ["# Hidden", "ordinary prose -->", "# Later"],
    }
    if variant == "inline":
        lines = ["<!-- @beacon[id=demo@heading, kind=ast, slice_labels=demo, comment=Description.] -->", "# Heading"]
    elif variant in {"canonical", "blank_gap"}:
        lines = opening + (["", ""] if variant == "blank_gap" else []) + ["-->", "# Heading"]
    else:
        lines = opening + damaged[variant]
    path = _write(tmp_path, "wrapper.md", "\n".join(lines) + "\n")
    before = _fingerprint(path)
    with monkeypatch.context() as patch:
        _install_write_guard(patch)
        rows = bi.check_beacon_file(path)
    assert _fingerprint(path) == before
    wrappers = [r for r in rows if r["code"] == "missing_wrapper"]
    assert bool(wrappers) == (variant in damaged)
    if wrappers:
        row = wrappers[0]
        assert row["fix"]["class"] == "choice"
        assert {r["resolution"] for r in row["fix"]["resolutions"]} >= {"keep", "insert_wrapper", "edit_metadata"}
        assert row["detail"]["boundaries"] and row["members"][0]["wrapper_open_range"]
        assert all(boundary >= row["members"][0]["bracket_range"][1] for boundary in row["detail"]["boundaries"])
        assert row["detail"]["hidden_or_revealed_headings"] == ["# Hidden"]
    else:
        assert rows == []


def test_one_snapshot_with_no_legacy_reread_and_stable_parse_message(tmp_path, monkeypatch):
    path = _write(tmp_path, "snapshot.py", fx.PROBLEM_FIXTURES["empty_comment"][1].replace("\n", "\r\n"))
    before = _fingerprint(path)
    opens = []
    def refuse(*a, **k):
        raise AssertionError("legacy source reread entered")
    with monkeypatch.context() as patch:
        _install_write_guard(patch)
        guarded = builtins.open
        def watch_open(file, mode="r", *args, **kwargs):
            if os.fspath(file) == path:
                opens.append(mode)
            return guarded(file, mode, *args, **kwargs)
        patch.setattr(builtins, "open", watch_open)
        patch.setattr(nmc, "_parse_disk_beacons_for_language", refuse)
        patch.setattr(nmc, "map_codebase", refuse)
        rows = bi.check_beacon_file(path)
    assert opens == ["rb"] and _fingerprint(path) == before
    assert rows and {r["observed"]["file_sha256"] for r in rows} == {before[0]}
    monkeypatch.setattr(nmc, "_warn_name", lambda base: base + " [2026-10-19 01:02:03]")
    first = _check(tmp_path, "mapper_parse_error")[0]
    monkeypatch.setattr(nmc, "_warn_name", lambda base: base + " [2026-10-19 01:02:04]")
    second = _check(tmp_path, "mapper_parse_error")[0]
    assert first["message"] == second["message"] and first["finding_id"] == second["finding_id"]
    assert first["detail"]["name"] != second["detail"]["name"]
