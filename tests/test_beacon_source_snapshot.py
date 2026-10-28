"""U01 §6.1's byte/physical-line primitive and §9's installation guard.

Primitive and production-consumer witnesses. The latter distinguish the old
mapping rereads, BOM rejection and Unicode splitlines geometry. Structural
block recovery is tested separately; no literal live beacon fixture sits here.
"""
from __future__ import annotations

import ast
import builtins
import dataclasses
import hashlib
import importlib
import os
from pathlib import Path
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOM = b"\xef\xbb\xbf"


@pytest.fixture
def source():
    return importlib.import_module("beacon_source")


@pytest.mark.parametrize("text,lines,endings,offsets", [
    ("", (), (), (0,)),
    ("abc", ("abc",), ("",), (0, 3)),
    ("abc\n", ("abc",), ("\n",), (0, 4)),
    ("abc\r", ("abc",), ("\r",), (0, 4)),
    ("abc\r\n", ("abc",), ("\r\n",), (0, 5)),
    ("\n", ("",), ("\n",), (0, 1)),
    ("\r", ("",), ("\r",), (0, 1)),
    ("\r\n", ("",), ("\r\n",), (0, 2)),
    ("\n\n", ("", ""), ("\n", "\n"), (0, 1, 2)),
    ("a\r\nb\rc\nd", ("a", "b", "c", "d"), ("\r\n", "\r", "\n", ""), (0, 3, 5, 7, 8)),
    ("x\f\v\x85\x1c\x1d\x1e\u2028\u2029y", ("x\f\v\x85\x1c\x1d\x1e\u2028\u2029y",), ("",), (0, 15)),
])
def test_physical_lines_only_split_crlf_cr_and_lf(source, text, lines, endings, offsets):
    snapshot = source.source_snapshot_from_bytes(text.encode("utf-8"))
    assert snapshot.physical_lines == lines
    assert snapshot.line_endings == endings
    assert snapshot.byte_offsets == offsets
    assert snapshot.parse_text == text
    assert snapshot.normalized_text == text.replace("\r\n", "\n").replace("\r", "\n")
    assert "".join(line + end for line, end in zip(lines, endings)) == text


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("final_newline", [False, True])
def test_bom_twins_share_parse_lines_but_retain_distinct_bytes(source, ending, final_newline):
    text = ending.join(["# metadata", "def f():", "    return 'é🙂'"])
    if final_newline:
        text += ending
    raw = text.encode("utf-8")
    plain = source.source_snapshot_from_bytes(raw)
    marked = source.source_snapshot_from_bytes(BOM + raw)
    assert plain.had_bom is False and marked.had_bom is True
    assert marked.raw_bytes == BOM + plain.raw_bytes
    assert marked.parse_text == plain.parse_text == text
    assert marked.normalized_text == plain.normalized_text
    assert marked.physical_lines == plain.physical_lines
    assert marked.line_endings == plain.line_endings
    assert marked.byte_offsets == tuple(offset + 3 for offset in plain.byte_offsets)
    assert plain.sha256 == hashlib.sha256(raw).hexdigest()
    assert marked.sha256 == hashlib.sha256(BOM + raw).hexdigest()
    assert marked.sha256 != plain.sha256


def test_only_the_first_bom_is_parse_metadata(source):
    raw = BOM + BOM + b"alpha\n" + BOM + b"omega"
    snapshot = source.source_snapshot_from_bytes(raw)
    assert snapshot.raw_bytes == raw
    assert snapshot.parse_text == "\ufeffalpha\n\ufeffomega"
    assert snapshot.physical_lines == ("\ufeffalpha", "\ufeffomega")
    assert snapshot.byte_offsets[0] == 3
    empty = source.source_snapshot_from_bytes(BOM)
    assert empty.had_bom is True
    assert empty.parse_text == "" and empty.physical_lines == ()
    assert empty.byte_offsets == (3,)


def test_offsets_address_original_utf8_not_normalized_characters(source):
    raw = BOM + "é🙂\r\nΩ\rx\nlast".encode("utf-8")
    snapshot = source.source_snapshot_from_bytes(raw)
    assert snapshot.byte_offsets == (3, 11, 14, 16, 20)
    assert snapshot.byte_offset(1, 0) == 3
    assert snapshot.byte_offset(1, 1) == 5
    assert snapshot.byte_offset(1, 2) == 9
    assert snapshot.byte_offset(2, 1) == 13
    assert snapshot.byte_offset(4, 4) == len(raw)
    for i, (line, ending) in enumerate(zip(snapshot.physical_lines, snapshot.line_endings)):
        start, stop = snapshot.byte_offsets[i:i + 2]
        assert raw[start:stop] == (line + ending).encode("utf-8")


def test_snapshot_is_immutable_and_retains_supplied_path_without_io(source, monkeypatch):
    def no_io(*args, **kwargs):
        raise AssertionError("the in-memory constructor performed IO")

    monkeypatch.setattr(builtins, "open", no_io)
    snapshot = source.source_snapshot_from_bytes(b"x\r\ny", canonical_path="/quoted/source.py")
    assert snapshot.canonical_path == "/quoted/source.py"
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.parse_text = "changed"
    with pytest.raises(TypeError):
        snapshot.physical_lines[0] = "changed"


def test_read_uses_one_binary_observation_and_changes_no_bytes_or_mtime(source, tmp_path, monkeypatch):
    path = tmp_path / "observed.py"
    raw = BOM + "é\r\nx\ry\n".encode("utf-8")
    path.write_bytes(raw)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    real_open = builtins.open
    calls = []

    def observed_open(filename, mode="r", *args, **kwargs):
        calls.append((os.fspath(filename), mode))
        assert mode == "rb", "a snapshot must not use text newline conversion"
        return real_open(filename, mode, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(builtins, "open", observed_open)
        snapshot = source.read_source_snapshot(path)
    assert calls == [(str(path.resolve()), "rb")]
    assert snapshot.canonical_path == str(path.resolve())
    assert snapshot.raw_bytes == raw and snapshot.sha256 == hashlib.sha256(raw).hexdigest()
    assert snapshot.line_endings == ("\r\n", "\r", "\n")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_decode_and_io_errors_remain_typed_not_empty_snapshots(source, tmp_path):
    bad = tmp_path / "bad.sql"
    bad.write_bytes(b"select '\xff';")
    before = (bad.read_bytes(), bad.stat().st_mtime_ns)
    with pytest.raises(UnicodeDecodeError):
        source.read_source_snapshot(bad)
    assert (bad.read_bytes(), bad.stat().st_mtime_ns) == before
    with pytest.raises(FileNotFoundError):
        source.read_source_snapshot(tmp_path / "missing.py")
    with pytest.raises(UnicodeDecodeError):
        source.source_snapshot_from_bytes(b"\xff")
    # A legitimate empty file remains a useful, distinguishable observation.
    empty = tmp_path / "empty.py"
    empty.write_bytes(b"")
    assert source.read_source_snapshot(empty).raw_bytes == b""


def _root_imports(source_paths, root_modules):
    imported = set()
    for path in source_paths:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            imported.update(name.split(".", 1)[0] for name in names
                            if name.split(".", 1)[0] in root_modules)
    return imported


def test_every_imported_root_module_is_in_all_three_distribution_tables():
    """BR-04: include package consumers, not just the wheel's root modules.

    This is static: pytest's root-first import path cannot mask an omitted
    installed root module. No wheel build, pip invocation or git is needed.
    """
    build = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]["build"]
    wheel = build["targets"]["wheel"]["force-include"]
    root_modules = {path.stem: path for path in ROOT.glob("*.py")}
    wheel_roots = [ROOT / rel for rel in wheel if rel.endswith(".py") and "/" not in rel]
    package_sources = [p for package in build["targets"]["wheel"]["packages"]
                       for p in (ROOT / package).rglob("*.py")]
    imported = _root_imports(wheel_roots + package_sources, root_modules)
    required = {"beacon_source", *(path.stem for path in wheel_roots), *imported}
    for name in sorted(required):
        rel = name + ".py"
        assert (ROOT / rel).is_file(), rel
        assert rel in build["include"], (rel, "tool.hatch.build.include")
        assert wheel.get(rel) == rel, (rel, "wheel.force-include")
        assert rel in build["targets"]["sdist"]["include"], (rel, "sdist.include")


def _mapping_source(ext):
    prefix = "//" if ext in {".js", ".ts", ".tsx", ".vue"} else "--" if ext == ".sql" else "#"
    fields = [prefix + " @beacon[", prefix + " id=snapshot@live,",
              prefix + " slice_labels=snapshot-label,", prefix + " kind=span,", prefix + " ]"]
    if ext == ".md":
        fields = ["<!--", "@beacon[", "id=snapshot@live,", "slice_labels=snapshot-label,",
                  "kind=span,", "]", "-->"]
    # Non-physical line separators are data inside quoted strings, not rows.
    specials = "\f\v\x85\x1c\x1d\x1e\u2028\u2029"
    if ext == ".py":
        before, after = ["TEXT = '" + specials + "'"], ["def live():", "    return TEXT"]
    elif ext in {".js", ".ts", ".tsx", ".vue"}:
        before, after = ["const TEXT = '" + specials + "';"], ["function live() { return TEXT; }"]
    elif ext == ".md":
        before, after = ["---", "title: 'a\u2028b'", "---"], ["# Live\u2028heading", "body"]
    elif ext == ".sql":
        before, after = ["SELECT '" + specials + "';"], ["SELECT 1;"]
    elif ext == ".sh":
        before, after = ["TEXT='" + specials + "'"], ["echo ok"]
    else:
        before, after = ["text: 'a\u2028b'"], ["live: yes"]
    lines = before + fields + after
    if ext == ".vue":
        lines = ["<template><p>hi</p></template>", "<script>", *lines, "</script>"]
    return lines


_MAP_EXTENSIONS = [".py", ".js", ".ts", ".tsx", ".md", ".vue", ".sql", ".sh", ".yaml", ".yml"]


@pytest.mark.parametrize("ext", _MAP_EXTENSIONS)
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_mapping_snapshot_is_in_memory_and_bom_physical_coordinates_agree(source, tmp_path, monkeypatch, ext, ending):
    import nexus_map_codebase as nmc

    path = tmp_path / ("absent" + ext)
    lines = _mapping_source(ext)
    raw = (ending.join(lines) + ending).encode("utf-8")
    plain = source.source_snapshot_from_bytes(raw, str(path))
    marked = source.source_snapshot_from_bytes(BOM + raw, str(path))

    def no_io(*args, **kwargs):
        raise AssertionError("mapping supplied bytes must not reopen their path")

    with monkeypatch.context() as m:
        m.setattr(builtins, "open", no_io)
        unmarked = nmc.map_source_snapshot(plain, repo_root=str(tmp_path))
        got = nmc.map_source_snapshot(marked, repo_root=str(tmp_path))
    assert got == unmarked
    assert got["note"] == f"Path: absent{ext}\nLINE COUNT: {len(lines)}"
    assert "snapshot@live" in str(got) and "snapshot-label" in str(got)
    assert "Parse Error" not in str(got)
    if ext in {".py", ".js", ".ts", ".tsx"}:
        live = next(node for node in got["children"] if node.get("ast_qualname") == "live")
        assert live["orig_lineno_start_unused"] == lines.index(next(s for s in lines if s.startswith(("def live", "function live")))) + 1


@pytest.mark.parametrize("ext", _MAP_EXTENSIONS)
def test_path_mapping_uses_exactly_one_binary_source_observation(source, tmp_path, monkeypatch, ext):
    import nexus_map_codebase as nmc

    path = tmp_path / ("live" + ext)
    raw = BOM + ("\r\n".join(_mapping_source(ext)) + "\r\n").encode("utf-8")
    path.write_bytes(raw)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    real_open = builtins.open
    reads = []

    def observed(filename, mode="r", *args, **kwargs):
        if os.fspath(filename) == str(path):
            reads.append(mode)
        return real_open(filename, mode, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(builtins, "open", observed)
        # Keep the pre-U01 call shape: before migration the read-count witness
        # must fail on the reread itself, not a newly added keyword argument.
        got = nmc.map_codebase(str(path), repo_root=str(tmp_path))
    assert reads == ["rb"]
    assert "snapshot@live" in str(got)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert got == nmc.map_source_snapshot(source.read_source_snapshot(path), repo_root=str(tmp_path))


@pytest.mark.parametrize("ext", [".py", ".js", ".vue", ".md", ".sql", ".sh", ".yaml"])
def test_directory_bad_entry_keeps_later_siblings_and_reports_failure(source, tmp_path, ext):
    import nexus_map_codebase as nmc

    bad = tmp_path / ("a_bad" + ext)
    bad.write_bytes(b"\xff\n")
    good = tmp_path / "b_good.py"
    good.write_text("def safe():\n    pass\n", encoding="utf-8")
    later = tmp_path / "c_good.sh"
    later.write_text("\n".join(_mapping_source(".sh")), encoding="utf-8")
    originals = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (bad, good, later)}
    sink = []
    tree = nmc.map_codebase(str(tmp_path), diagnostics=sink)
    assert len(tree["children"]) == 3
    assert "Parse Error" in str(tree["children"][0])
    assert "safe" in str(tree["children"][1]) and "snapshot@live" in str(tree["children"][2])
    assert any(d["file"] == str(bad) and d["record"] == "source_error" for d in sink)
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in originals} == originals


def test_outline_failure_is_not_a_valid_empty_projection(source, tmp_path):
    import nexus_map_codebase as nmc

    diagnostics = []
    broken = source.source_snapshot_from_bytes(b"def broken(:\n", str(tmp_path / "bad.py"))
    failed = nmc.map_source_snapshot(broken, diagnostics=diagnostics)
    empty = nmc.map_source_snapshot(source.source_snapshot_from_bytes(b"", str(tmp_path / "empty.py")))
    assert "Parse Error" in failed["children"][0]["name"]
    assert empty["children"] == []
    assert diagnostics == [{"record": "outline_error", "file": broken.canonical_path,
                            "language": "python", "exception": failed["children"][0]["note"]}]


def test_directory_parser_exception_is_entry_local_and_generic_binary_is_still_a_leaf(source, tmp_path, monkeypatch):
    import nexus_map_codebase as nmc

    for name in ("a_fail.py", "b_safe.py"):
        (tmp_path / name).write_text("def safe(): pass\n", encoding="utf-8")
    (tmp_path / "c_binary.dat").write_bytes(b"\xff\x00")
    real = nmc.map_source_snapshot

    def fail_one(snapshot, *args, **kwargs):
        if snapshot.canonical_path.endswith("a_fail.py"):
            raise ValueError("entry-local test failure")
        return real(snapshot, *args, **kwargs)

    monkeypatch.setattr(nmc, "map_source_snapshot", fail_one)
    sink = []
    tree = nmc.map_codebase(str(tmp_path), diagnostics=sink)
    assert len(tree["children"]) == 3
    assert "entry-local test failure" in str(tree["children"][0])
    assert "safe" in str(tree["children"][1])
    assert tree["children"][2] == {"name": "📄 c_binary.dat", "note": "Path: c_binary.dat", "children": []}


def test_text_outline_adapters_keep_physical_rows_and_strip_only_file_bom(source, tmp_path):
    import nexus_map_codebase as nmc

    py = tmp_path / "physical.py"
    js = tmp_path / "physical.js"
    py.write_bytes(BOM + "TEXT = 'a\u2028b'\rdef live():\r    pass\r".encode("utf-8"))
    js.write_bytes(BOM + "const TEXT = 'a\u2028b';\rfunction live() {}\r".encode("utf-8"))
    py_nodes, py_lines = nmc.parse_file_outline_core(str(py))
    js_nodes, js_lines = nmc.parse_js_ts_outline_core(str(js))
    assert len(py_lines) == 3 and len(js_lines) == 2
    assert next(n for n in py_nodes if n.get("ast_qualname") == "live")["orig_lineno_start_unused"] == 2
    assert next(n for n in js_nodes if n.get("ast_qualname") == "live")["orig_lineno_start_unused"] == 2
    context = nmc.JsTsOutlineContext.from_source(source_text="const X = 'a\u2028b';\rfunction live() {}\r",
                                              file_path=str(js), language_name="javascript", line_offset0=5,
                                              qual_prefix="section")
    assert len(context.lines) == 2 and context.abs_line(2) == 7


@pytest.mark.parametrize("ext", _MAP_EXTENSIONS)
def test_directory_and_file_mapping_use_the_same_dispatch(source, tmp_path, ext):
    import nexus_map_codebase as nmc

    path = tmp_path / ("live" + ext)
    path.write_text("\n".join(_mapping_source(ext)) + "\n", encoding="utf-8")
    tree = nmc.map_codebase(str(tmp_path))
    assert tree["children"] == [nmc.map_codebase(str(path), repo_root=str(tmp_path))]
    assert "snapshot@live" in str(tree)


def test_vue_outline_failure_keeps_independent_script_section(source, tmp_path, monkeypatch):
    import nexus_map_codebase as nmc

    raw = "\r".join(["<template><p>ok</p></template>", "<script>", "function broken() {}",
                       "</script>", "<script setup>", *_mapping_source(".js"), "</script>"])
    snapshot = source.source_snapshot_from_bytes(BOM + raw.encode("utf-8"), str(tmp_path / "absent.vue"))
    real = nmc.JsTsOutlineContext.from_source

    def fail_one(**kwargs):
        if "function broken" in kwargs["source_text"]:
            raise ValueError("one script cannot be outlined")
        return real(**kwargs)

    monkeypatch.setattr(nmc.JsTsOutlineContext, "from_source", staticmethod(fail_one))
    diagnostics = []
    tree = nmc.map_source_snapshot(snapshot, diagnostics=diagnostics)
    assert len(tree["children"]) == 3
    assert "Parse Error (VUE script JS/TS)" in str(tree["children"][1])
    assert "snapshot@live" in str(tree["children"][2]) and "live" in str(tree["children"][2])
    assert diagnostics == [{"record": "outline_error", "file": snapshot.canonical_path,
                            "language": "vue_script", "exception": "one script cannot be outlined",
                            "line_offset0": 1}]
