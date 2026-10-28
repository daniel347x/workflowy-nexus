"""U1 build tests (spec 12.2, B1-B8): the generated engine and the build
script that produces it, on the committed tree and on synthetic input.

F1 is the fixture repository of spec 14 (its five files, written exactly);
its goldens live with the parity tests (U2).  Nothing here touches a
Workflowy account: NEXUS is imported from the working tree and driven at its
pure parse functions.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "agent-kit" / ".claude" / "skills" / "code-map" / "scripts"))

import build_agent_kit as bak  # noqa: E402

ENGINE_PATH = REPO_ROOT / "agent-kit" / ".claude" / "skills" / "code-map" / "scripts" / "_engine.py"

import _engine  # noqa: E402
import nexus_map_codebase as nmc  # noqa: E402
from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    repository_view_entry_display,
)


def test_byte_edit_core_comes_from_the_canonical_vendor_module():
    """42131b6: the kit has no shared byte plan/proof/publisher module."""
    assert "beacon_edits.py" in bak.SOURCE_FILES
    assert bak.SOURCE_MODULE_BY_NAME["beacon_edits"] == "beacon_edits.py"
    roots = bak.discover_roots(REPO_ROOT, [])
    for name in ("BeaconEdit", "BeaconEditPlan", "EditOffsetMap", "source_line_edit",
                 "verify_beacon_edit_plan", "commit_beacon_edit_plan"):
        assert roots[name] == "beacon_edits.py"
        assert callable(getattr(_engine, name))


def test_findings_core_comes_from_the_canonical_vendor_module():
    """P lacks the shared findings module and its pure projection/planner roots."""
    assert "beacon_findings.py" in bak.SOURCE_FILES
    assert bak.SOURCE_MODULE_BY_NAME["beacon_findings"] == "beacon_findings.py"
    roots = bak.discover_roots(REPO_ROOT, [])
    for name in ("beacon_snapshot_analysis_inputs", "analyze_beacon_snapshot"):
        assert roots[name] == "beacon_findings.py"
        assert callable(getattr(_engine, name))
    for name in ("plan_beacon_hygiene", "plan_beacon_comment_aliases", "_parse_snapshot_beacons_for_language"):
        assert roots[name] == "nexus_map_codebase.py"
        assert callable(getattr(_engine, name))
    for forbidden_root in ("map_source_snapshot", "_beacon_selection_structures"):
        assert not hasattr(_engine, forbidden_root)

# ---------------------------------------------------------------------------
# Fixture F1 (spec 14): five files, each exactly this text, LF, one final \n.
# ---------------------------------------------------------------------------

F1_CORE_PY = '''"""Core of the demo package."""


# @beacon[
#   id=demo@LIMIT,
#   slice_labels=demo,
#   kind=ast,
#   comment=Largest batch, in items,
# ]
LIMIT = 50


class Registry:
    """Keeps every registered handler.

    Template <T> holds.
        Path: x
    """

    # @beacon[
    #   id=demo@Registry.register,
    #   slice_labels=demo,demo-cli,
    #   kind=ast,
    #   comment=Add one handler,
    # ]
    def register(self, name: str, handler=None):
        """Store the handler under its name."""
        return name


# Reads are cheap; writes take the lock.
# @beacon[
#   id=demo@Store,
#   slice_labels=demo,
#   kind=ast,
#   comment=The one store,
# ]
class Store:
    """In-memory store."""

    # @beacon[
    #   id=demolition@Store.drop,
    #   slice_labels=demolition,
    #   kind=ast,
    #   comment=Not in the demo family,
    # ]
    def drop(self):
        """Drop everything."""

    def get(self, key: str):
        """Untagged; never shown."""
        return key


# @beacon[
#   id=demo@serve,
#   slice_labels=demo--x,
#   kind=ast,
#   comment=Serve requests,
# ]
@decorator
def serve(port: int = 8080) -> None:
    """Start serving."""

    # @beacon[
    #   id=demo@serve.handle,
    #   slice_labels=demo,
    #   kind=ast,
    #   comment=Handle one request,
    # ]
    def handle(request):
        """Nested handler."""
        return request

    return handle


# @beacon[
#   id=demo@fetch,
#   slice_labels=demo,
#   kind=ast,
#   comment=Fetch one page,
# ]
async def fetch(url: str):
    """Fetch it."""
    return url


# @beacon[
#   id=demo@limits,
#   role=limits,
#   slice_labels=demo,
#   kind=span,
#   show_span=true,
#   comment=Size limits,
# ]
# Tunable caps.
MAX_A = 10
MAX_B = 20
# @beacon-close[
#   id=demo@limits,
# ]


# @beacon[
#   id=demo@quiet,
#   role=quiet,
#   slice_labels=demo,
#   kind=span,
#   show_span=false,
#   comment=Hidden region,
# ]
QUIET = 1
# @beacon-close[
#   id=demo@quiet,
# ]


class L1:
    class L2:
        class L3:
            class L4:
                class L5:
                    # @beacon[
                    #   id=demo@deep,
                    #   slice_labels=demo,
                    #   kind=ast,
                    #   comment=Six levels down,
                    # ]
                    def deep(self):
                        """Deepest."""
'''

F1_APP_TS = '''/**
 * Greets people.
 */
// @beacon[
//   id=web@Greeter,
//   slice_labels=web,
//   kind=ast,
//   comment=The greeter,
// ]
export class Greeter {
  // @beacon[
  //   id=web@Greeter.greet,
  //   slice_labels=web,
  //   kind=ast,
  //   comment=Say hello,
  // ]
  greet(name: string = `world`): string {
    return `hello ${name}`;
  }
}

// @beacon[
//   id=web@loadAll,
//   slice_labels=web,
//   kind=ast,
//   comment=Load everything,
// ]
export const loadAll = async (limit: number) => {
  return limit;
};

// @beacon[
//   id=web@Shape,
//   slice_labels=web,
//   kind=ast,
//   comment=A shape,
// ]
export interface Shape {
  sides: number;
}
'''

F1_SCHEMA_SQL = '''-- Customer records.
-- @beacon[
--   id=demo@customers,
--   role=customers,
--   slice_labels=demo,
--   kind=span,
--   show_span=true,
--   comment=Customer table,
-- ]
CREATE TABLE customers (
  id INTEGER PRIMARY KEY
);
-- @beacon-close[
--   id=demo@customers,
-- ]
'''

F1_BUILD_SH = '''#!/bin/sh
# Build everything.
# @beacon[
#   id=demo@build,
#   role=build,
#   slice_labels=demo,
#   kind=span,
#   show_span=true,
#   comment=Build script,
# ]
make all
# @beacon-close[
#   id=demo@build,
# ]
'''

F1_CONFIG_YAML = '''# Service settings.
# @beacon[
#   id=demo@service,
#   role=service,
#   slice_labels=demo,
#   kind=span,
#   show_span=true,
#   comment=Service block,
# ]
service:
  port: 8080
# @beacon-close[
#   id=demo@service,
# ]
'''

F1_FILES = {
    "pkg/core.py": F1_CORE_PY,
    "web/app.ts": F1_APP_TS,
    "db/schema.sql": F1_SCHEMA_SQL,
    "ops/build.sh": F1_BUILD_SH,
    "config.yaml": F1_CONFIG_YAML,
}


@pytest.fixture()
def f1_root(tmp_path: Path) -> Path:
    for rel, text in F1_FILES.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    return tmp_path


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def names_notes(nodes):
    out = []
    for n in nodes:
        out.append((n.get("name"), n.get("note")))
        out.extend(names_notes(n.get("children") or []))
    return out


def grouped_like_wrapper(core_nodes, lines, language, comment_style):
    nodes = nmc.group_outline_by_carto_markers(
        lines=lines,
        outline_nodes=core_nodes,
        language=language,
        comment_style=comment_style,
    )
    return nodes


def span_children(engine_parse, engine_apply, nexus_parse, nexus_apply, lines):
    be = engine_parse(list(lines))
    ch_eng = []
    engine_apply(list(lines), ch_eng, be)
    bn = nexus_parse(list(lines))
    ch_nex = []
    nexus_apply(list(lines), ch_nex, bn)
    return names_notes(ch_eng), names_notes(ch_nex)


def iter_entries(nodes):
    for n in nodes:
        note = str(n.get("note") or "")
        if _engine.repository_view_note_is_entry(note):
            yield n
        yield from iter_entries(n.get("children") or [])


# ---------------------------------------------------------------------------
# B1 --check exits 0 on the committed tree (the drift alarm)
# ---------------------------------------------------------------------------

def test_b1_check_exits_zero_on_committed_tree():
    proc = subprocess.run(
        [sys.executable, "scripts/build_agent_kit.py", "--check"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


# ---------------------------------------------------------------------------
# B2 two in-memory builds are identical
# ---------------------------------------------------------------------------

def test_b2_two_builds_identical():
    first = bak.build_engine(REPO_ROOT).text
    second = bak.build_engine(REPO_ROOT).text
    assert first == second


# ---------------------------------------------------------------------------
# B3 the committed engine: parses at 3.10, imports fresh, no docstring,
# exactly one comment token on line 1
# ---------------------------------------------------------------------------

def test_b3_engine_shape():
    text = ENGINE_PATH.read_text(encoding="utf-8")
    ast.parse(text, feature_version=(3, 10))  # raises on failure
    tree = ast.parse(text)
    with_docs = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and ast.get_docstring(n) is not None
    ]
    assert with_docs == []
    comments = [t for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type == tokenize.COMMENT]
    assert len(comments) == 1
    assert comments[0].start[0] == 1
    proc = subprocess.run(
        [sys.executable, "-c", "import _engine"],
        cwd=str(ENGINE_PATH.parent), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# B4 a synthetic root referencing an unbound name stops the closure check,
# naming the name and the root
# ---------------------------------------------------------------------------

def test_b4_unresolved_name_stops_closure():
    texts = {rel: (REPO_ROOT / rel).read_text(encoding="utf-8") for rel in bak.SOURCE_FILES}
    symbols = bak.extract_symbols(texts)
    rel = "nexus_map_codebase.py"
    synthetic = bak.Symbol(rel, "zz_synth_root", 1, "def zz_synth_root():\n    return zz_no_such_helper_zzz()\n")
    synthetic.prepared = synthetic.text
    symbols = {k: dict(v) for k, v in symbols.items()}
    symbols[rel]["zz_synth_root"] = synthetic
    stdlib_bindings = bak.module_import_bindings(texts)
    source_bindings = bak.source_import_bindings(texts)
    reasons: list[str] = []
    bak.compute_closure(
        {"zz_synth_root": rel}, symbols, stdlib_bindings, source_bindings, {}, reasons
    )
    assert reasons, "the closure check passed a root referencing an unbound name"
    joined = "\n".join(reasons)
    assert "zz_no_such_helper_zzz" in joined
    assert "zz_synth_root" in joined


# ---------------------------------------------------------------------------
# B5 a rewrite entry that does not fire stops the build, naming the entry
# ---------------------------------------------------------------------------

def test_b5_rewrite_entry_that_does_not_fire_names_the_entry():
    reasons = bak.check_rewrite_fires({}, {})
    joined = "\n".join(reasons)
    for entry in ("W1", "W4a", "W4b", "W5"):
        assert entry in joined
    for old, _new in bak.RENAMES:
        assert old in joined
    # and the real build's counts satisfy the check
    assert bak.check_rewrite_fires(
        {e["id"]: 1 for e in bak.STRING_REWRITES},
        {old: 1 for old, _n in bak.RENAMES},
    ) == []


# ---------------------------------------------------------------------------
# B6 a forbidden word planted in a synthetic symbol stops the build; the A4
# word check over every file under agent-kit/
# ---------------------------------------------------------------------------

def test_b6_forbidden_word_planted_in_synthetic_symbol_is_caught():
    hits = bak.forbidden_word_hits("def zz_synth():\n    note = 'built for workflowy users'\n")
    assert hits, "the word check missed a planted forbidden word"
    assert any("workflowy" in h for h in hits)


def test_b6_agent_kit_files_match_nothing_forbidden():
    kit = REPO_ROOT / "agent-kit"
    assert kit.is_dir()
    checked = 0
    for path in sorted(kit.rglob("*")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if any(part == "__pycache__" for part in path.parts):
            continue  # bytecode artifacts, not the kit's files
        assert bak.forbidden_word_hits(rel) == [], f"forbidden word in file name: {rel}"
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # a binary artifact, not a kit source file
            hits = bak.forbidden_word_hits(text)
            assert hits == [], f"forbidden word in {rel}: {hits}"
            checked += 1
    assert checked > 5


# ---------------------------------------------------------------------------
# B7 engine equivalence on F1
# ---------------------------------------------------------------------------

def test_b7_python_core_equals_nexus_wrapper(f1_root: Path):
    path = f1_root / "pkg/core.py"
    lines = path.read_text(encoding="utf-8").splitlines()
    eng_nodes, eng_lines = _engine.parse_file_outline_core(str(path))
    assert eng_lines == lines
    grouped = grouped_like_wrapper(eng_nodes, eng_lines, "python", "python")
    nexus_nodes = nmc.parse_file_outline(str(path))
    assert names_notes(grouped) == names_notes(nexus_nodes)


def test_b7_js_core_equals_nexus_wrapper(f1_root: Path):
    pytest.importorskip("tree_sitter_language_pack")
    path = f1_root / "web/app.ts"
    lines = path.read_text(encoding="utf-8").splitlines()
    eng_nodes, eng_lines = _engine.parse_js_ts_outline_core(str(path))
    assert eng_lines == lines
    grouped = grouped_like_wrapper(eng_nodes, eng_lines, "js_ts", "js_ts")
    nexus_nodes = nmc.parse_js_ts_outline(str(path))
    assert names_notes(grouped) == names_notes(nexus_nodes)


def test_b7_span_parsers_equal_nexus(f1_root: Path):
    cases = [
        ("db/schema.sql", _engine.parse_sql_beacon_blocks, _engine.apply_sql_beacons,
         nmc.parse_sql_beacon_blocks, nmc.apply_sql_beacons),
        ("ops/build.sh", _engine.parse_sh_beacon_blocks, _engine.apply_sh_beacons,
         nmc.parse_sh_beacon_blocks, nmc.apply_sh_beacons),
        ("config.yaml", _engine.parse_yaml_beacon_blocks, _engine.apply_yaml_beacons,
         nmc.parse_yaml_beacon_blocks, nmc.apply_yaml_beacons),
    ]
    for rel, eng_parse, eng_apply, nex_parse, nex_apply in cases:
        lines = (f1_root / rel).read_text(encoding="utf-8").splitlines()
        eng, nex = span_children(eng_parse, eng_apply, nex_parse, nex_apply, lines)
        assert eng == nex, rel


def test_b7_entry_display_parity_on_every_f1_entry(f1_root: Path):
    paths = [
        f1_root / "pkg/core.py",
        f1_root / "db/schema.sql",
        f1_root / "ops/build.sh",
        f1_root / "config.yaml",
    ]
    if importlib.util.find_spec("tree_sitter_language_pack"):
        paths.append(f1_root / "web/app.ts")
    identity = lambda s: s  # noqa: E731
    checked = 0
    for path in paths:
        if path.suffix == ".py":
            nodes, _lines = _engine.parse_file_outline_core(str(path))
        elif path.suffix in (".js", ".jsx", ".ts", ".tsx"):
            nodes, _lines = _engine.parse_js_ts_outline_core(str(path))
        elif path.suffix == ".sql":
            nodes = []
            be = _engine.parse_sql_beacon_blocks(path.read_text(encoding="utf-8").splitlines())
            _engine.apply_sql_beacons(path.read_text(encoding="utf-8").splitlines(), nodes, be)
        elif path.suffix == ".sh":
            nodes = []
            be = _engine.parse_sh_beacon_blocks(path.read_text(encoding="utf-8").splitlines())
            _engine.apply_sh_beacons(path.read_text(encoding="utf-8").splitlines(), nodes, be)
        elif path.suffix in (".yml", ".yaml"):
            nodes = []
            be = _engine.parse_yaml_beacon_blocks(path.read_text(encoding="utf-8").splitlines())
            _engine.apply_yaml_beacons(path.read_text(encoding="utf-8").splitlines(), nodes, be)
        else:
            continue

        def walk(nodes, parent_kind):
            nonlocal checked
            for n in nodes:
                name = str(n.get("name") or "")
                note = str(n.get("note") or "")
                if _engine.repository_view_note_is_entry(note):
                    for headings_only in (False, True):
                        eng = _engine.repository_view_entry_display(
                            name, note, parent_kind,
                            headings_only=headings_only, strip_text=identity,
                        )
                        nex = repository_view_entry_display(
                            name, note, parent_kind,
                            headings_only=headings_only, strip_text=identity,
                        )
                        checked += 1
                        assert eng == nex, (path, name, headings_only)
                    walk(n.get("children") or [], _engine.repository_view_heading_kind(
                        _engine.repository_view_entry_display(
                            name, note, parent_kind, headings_only=True, strip_text=identity,
                        )[0]
                    ))
                else:
                    walk(n.get("children") or [], parent_kind)

        walk(nodes, None)
    assert checked > 20


# ---------------------------------------------------------------------------
# B8 the engine imports nothing but stdlib modules and the guarded
# tree-sitter names
# ---------------------------------------------------------------------------

def test_b8_engine_imports_are_stdlib_or_tree_sitter():
    text = ENGINE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)
    allowed = set(sys.stdlib_module_names) | {"tree_sitter", "tree_sitter_language_pack"}
    seen = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen.extend(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                seen.append(node.module.split(".")[0])
    assert seen, "the engine imports nothing at all"
    bad = sorted({m for m in seen if m not in allowed})
    assert bad == []


# ---------------------------------------------------------------------------
# W1: the closure is complete and source-bound.  Each synthetic case runs
# the real full build path (all checks, the fresh-interpreter import and
# --check) inside a scratch repository holding the real five SOURCE_FILES,
# the build script and the src/ tree, with one vendor-labelled synthetic
# root appended.  The negative cases must fail the build itself: an engine
# that merely imports is not evidence.
# ---------------------------------------------------------------------------

import shutil  # noqa: E402

VENDOR_BLOCK = (
    "\n\n# @beacon[\n#   id=agent-kit@zz_synth_root,\n"
    "#   slice_labels=agent-kit-vendor,\n#   kind=ast,\n"
    "#   comment=Synthetic closure probe root,\n# ]\n"
)


def _w1_scratch(tmp_path: Path) -> Path:
    """A scratch repository holding the real five SOURCE_FILES, the build
    script and the src/ tree, so `import_nexus` and the build run entirely
    inside it."""
    scratch = tmp_path / "repo"
    for rel in bak.SOURCE_FILES:
        target = scratch / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, target)
    (scratch / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / "scripts" / "build_agent_kit.py", scratch / "scripts" / "build_agent_kit.py")
    shutil.copytree(
        REPO_ROOT / "src" / "workflowy_mcp",
        scratch / "src" / "workflowy_mcp",
        ignore=shutil.ignore_patterns("__pycache__"),
        dirs_exist_ok=True,
    )
    return scratch


def _append(scratch: Path, rel: str, text: str) -> None:
    path = scratch / rel
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def _run_build(scratch: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(scratch / "scripts" / "build_agent_kit.py")],
        cwd=str(scratch), capture_output=True, text=True, timeout=600,
    )


def _call_engine(scratch: Path, expression: str) -> subprocess.CompletedProcess:
    scripts = scratch / "agent-kit" / ".claude" / "skills" / "code-map" / "scripts"
    return subprocess.run(
        [sys.executable, "-c", "import _engine; " + expression],
        cwd=str(scripts), capture_output=True, text=True, timeout=300,
    )


def _engine_text(scratch: Path) -> str:
    return (
        scratch / "agent-kit" / ".claude" / "skills" / "code-map" / "scripts" / "_engine.py"
    ).read_text(encoding="utf-8")


def _engine_module_names(scratch: Path) -> set[str]:
    tree = ast.parse(_engine_text(scratch))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_w1_comprehension_masked_global_is_extracted_and_runs(tmp_path: Path):
    scratch = _w1_scratch(tmp_path)
    _append(
        scratch, "nexus_map_codebase.py",
        "\n\nzz_global = 3\n" + VENDOR_BLOCK
        + "def zz_synth_root():\n"
          "    return [zz_global for zz_global in []] + [zz_global]\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 0, proc.stderr
    assert "\nzz_global = 3\n" in _engine_text(scratch)
    called = _call_engine(scratch, "print(_engine.zz_synth_root())")
    assert called.returncode == 0, called.stderr
    assert called.stdout.strip() == "[3]"
    check = subprocess.run(
        [sys.executable, str(scratch / "scripts" / "build_agent_kit.py"), "--check"],
        cwd=str(scratch), capture_output=True, text=True, timeout=600,
    )
    assert check.returncode == 0, check.stderr


def test_w1_nested_local_store_does_not_mask(tmp_path: Path):
    scratch = _w1_scratch(tmp_path)
    _append(
        scratch, "nexus_map_codebase.py",
        "\n\nzz_global = 3\n" + VENDOR_BLOCK
        + "def zz_synth_root():\n"
          "    def zz_inner():\n"
          "        zz_global = 1\n"
          "        return zz_global\n"
          "    return zz_global\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 0, proc.stderr
    assert "\nzz_global = 3\n" in _engine_text(scratch)
    called = _call_engine(scratch, "print(_engine.zz_synth_root())")
    assert called.returncode == 0, called.stderr
    assert called.stdout.strip() == "3"


def test_w1_global_read_before_store_is_extracted(tmp_path: Path):
    scratch = _w1_scratch(tmp_path)
    _append(
        scratch, "nexus_map_codebase.py",
        "\n\nzz_global = 3\n" + VENDOR_BLOCK
        + "def zz_synth_root():\n"
          "    global zz_global\n"
          "    first = zz_global\n"
          "    zz_global = 1\n"
          "    return first\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 0, proc.stderr
    assert "\nzz_global = 3\n" in _engine_text(scratch)
    called = _call_engine(scratch, "print(_engine.zz_synth_root())")
    assert called.returncode == 0, called.stderr
    assert called.stdout.strip() == "3"


def test_w1_missing_same_file_global_stops_the_build(tmp_path: Path):
    scratch = _w1_scratch(tmp_path)
    _append(
        scratch, "nexus_map_codebase.py",
        VENDOR_BLOCK
        + "def zz_synth_root():\n"
          "    return [zz_global for zz_global in []] + [zz_global]\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 1, f"the build passed an incomplete closure: {proc.stdout}"
    assert "zz_global" in proc.stderr
    assert "zz_synth_root" in proc.stderr


def test_w1_cross_file_name_without_import_is_rejected(tmp_path: Path):
    scratch = _w1_scratch(tmp_path)
    _append(scratch, "beacon_mutation_contract.py", "\n\nzz_from_other_zzz = 41\n")
    _append(
        scratch, "nexus_map_codebase.py",
        VENDOR_BLOCK + "def zz_synth_root():\n    return zz_from_other_zzz\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 1, f"the union supplied a name the file never imported: {proc.stdout}"
    assert "zz_from_other_zzz" in proc.stderr
    assert "zz_synth_root" in proc.stderr


def test_w1_cross_file_import_resolves_and_carto_supply_holds(tmp_path: Path):
    scratch = _w1_scratch(tmp_path)
    _append(scratch, "beacon_mutation_contract.py", "\n\nzz_shared_value_zzz = 7\n")
    _append(
        scratch, "nexus_map_codebase.py",
        "\n\nfrom beacon_mutation_contract import zz_shared_value_zzz\n" + VENDOR_BLOCK
        + "def zz_synth_root():\n    return zz_shared_value_zzz\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 0, proc.stderr
    engine = _engine_text(scratch)
    assert "\nzz_shared_value_zzz = 7\n" in engine
    # the carto alias's supply still holds: a real engine user of the
    # rewritten names resolves, and the symbol is present
    assert "\ndef split_name_and_tags(" in engine
    called = _call_engine(scratch, "print(_engine.zz_synth_root())")
    assert called.returncode == 0, called.stderr
    assert called.stdout.strip() == "7"


def test_w1_no_spurious_symbols_from_comprehensions_nonlocal_and_params(
    tmp_path: Path
):
    scratch = _w1_scratch(tmp_path)
    _append(
        scratch, "nexus_map_codebase.py",
        VENDOR_BLOCK
        + "def zz_synth_root():\n"
          "    count = 0\n"
          "    def zz_inc():\n"
          "        nonlocal count\n"
          "        count += 1\n"
          "    zz_inc()\n"
          "    return [q * 2 for q in range(3)] + [count]\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 0, proc.stderr
    names = _engine_module_names(scratch)
    for spurious in ("zz_inc", "count", "q"):
        assert spurious not in names, spurious
    assert "zz_synth_root" in names
    called = _call_engine(scratch, "print(_engine.zz_synth_root())")
    assert called.returncode == 0, called.stderr
    assert called.stdout.strip() == "[0, 2, 4, 1]"


def test_w1_class_body_assignment_does_not_mask(tmp_path: Path):
    scratch = _w1_scratch(tmp_path)
    _append(
        scratch, "nexus_map_codebase.py",
        "\n\nzz_cls_value = 'module'\n" + VENDOR_BLOCK
        + "def zz_synth_root():\n"
          "    class ZzHolder:\n"
          "        zz_cls_value = 'class'\n"
          "        def get(self):\n"
          "            return zz_cls_value\n"
          "    return ZzHolder().get()\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 0, proc.stderr
    assert "\nzz_cls_value = 'module'\n" in _engine_text(scratch)
    called = _call_engine(scratch, "print(_engine.zz_synth_root())")
    assert called.returncode == 0, called.stderr
    assert called.stdout.strip() == "module"


def test_w1_own_file_binding_wins_over_the_stdlib_binding():
    """A free name that the file both imports from the standard library and
    defines as its own module symbol resolves to the own symbol, and the
    stdlib binding stays out of the preamble."""
    texts = {rel: (REPO_ROOT / rel).read_text(encoding="utf-8") for rel in bak.SOURCE_FILES}
    symbols = bak.extract_symbols(texts)
    rel = "nexus_map_codebase.py"
    synthetic = bak.Symbol(rel, "zz_json_user", 1, "def zz_json_user():\n    return json\n")
    synthetic.prepared = synthetic.text
    own = bak.Symbol(rel, "json", 1, "def json():\n    return 'own'\n")
    own.prepared = own.text
    symbols = {k: dict(v) for k, v in symbols.items()}
    symbols[rel]["zz_json_user"] = synthetic
    symbols[rel]["json"] = own
    stdlib_bindings = bak.module_import_bindings(texts)
    source_bindings = bak.source_import_bindings(texts)
    reasons: list[str] = []
    needed, stdlib_used, _ts = bak.compute_closure(
        {"zz_json_user": rel}, symbols, stdlib_bindings, source_bindings, {}, reasons
    )
    assert reasons == [], reasons
    assert "json" in needed
    assert ("json", None, "zz_json_user") not in stdlib_used
    # the control, without the own symbol: the import's binding is used
    symbols2 = {k: dict(v) for k, v in symbols.items()}
    symbols2[rel].pop("json")
    reasons2: list[str] = []
    _needed2, stdlib_used2, _ts2 = bak.compute_closure(
        {"zz_json_user": rel}, symbols2, stdlib_bindings, source_bindings, {}, reasons2
    )
    assert reasons2 == [], reasons2
    assert ("json", None, "json") in stdlib_used2


# ---------------------------------------------------------------------------
# r2 W1: the closure is source-bound in the class and import dimensions too
# (WorkOrder [ap:J7STY1] W1).  Every full-build case below goes through the
# real build path: the real five SOURCE_FILES, real root discovery, stripping
# and rewrites, the closure, the syntax/word checks and a fresh import.
# ---------------------------------------------------------------------------


def _vendor_block(bid: str, comment: str) -> str:
    return (
        "\n\n# @beacon[\n#   id=agent-kit@" + bid + ",\n"
        "#   slice_labels=agent-kit-vendor,\n#   kind=ast,\n"
        "#   comment=" + comment + ",\n# ]\n"
    )


def test_w1_class_body_read_before_assignment_is_extracted_and_runs(tmp_path: Path):
    """A class-body read of a module value, masked by the class's own later
    assignment of the same name, must not hide the module binding: the build
    carries it and the generated root returns the source's value."""
    scratch = _w1_scratch(tmp_path)
    _append(
        scratch, "nexus_map_codebase.py",
        "\n\nzz_global = 3\n" + _vendor_block("zz_class_reader", "Class-body read-back probe root")
        + "def zz_class_reader():\n"
          "    class ZzHolder:\n"
          "        zz_global = zz_global\n"
          "    return ZzHolder.zz_global\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 0, proc.stderr
    assert "\nzz_global = 3\n" in _engine_text(scratch)
    called = _call_engine(scratch, "print(_engine.zz_class_reader())")
    assert called.returncode == 0, called.stderr
    assert called.stdout.strip() == "3"
    check = subprocess.run(
        [sys.executable, str(scratch / "scripts" / "build_agent_kit.py"), "--check"],
        cwd=str(scratch), capture_output=True, text=True, timeout=600,
    )
    assert check.returncode == 0, check.stderr


def test_w1_class_body_read_before_a_later_class_assignment_keeps_the_module_binding(tmp_path: Path):
    """A plain class-body read (``y = zz_global``) that runs before a later
    class-body assignment of the same name resolves in the module namespace:
    the module binding is extracted and both values come back as in the
    source."""
    scratch = _w1_scratch(tmp_path)
    _append(
        scratch, "nexus_map_codebase.py",
        "\n\nzz_global = 3\n" + _vendor_block("zz_read_first", "Read-before-class-store probe root")
        + "def zz_read_first():\n"
          "    class ZzLate:\n"
          "        y = zz_global\n"
          "        zz_global = 9\n"
          "    return ZzLate.y, ZzLate.zz_global\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 0, proc.stderr
    assert "\nzz_global = 3\n" in _engine_text(scratch)
    called = _call_engine(scratch, "print(_engine.zz_read_first())")
    assert called.returncode == 0, called.stderr
    assert called.stdout.strip() == "(3, 9)"


def test_w1_class_body_read_with_no_module_binding_stops_the_build(tmp_path: Path):
    """Removing the only legitimate module binding must stop the complete
    build, naming the read and its user; a green build followed by a
    NameError at call time is the defect."""
    scratch = _w1_scratch(tmp_path)
    _append(
        scratch, "nexus_map_codebase.py",
        _vendor_block("zz_class_reader", "Class-body read-back probe root")
        + "def zz_class_reader():\n"
          "    class ZzHolder:\n"
          "        zz_global = zz_global\n"
          "    return ZzHolder.zz_global\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 1, f"the build passed an incomplete closure: {proc.stdout}"
    assert "zz_global" in proc.stderr
    assert "zz_class_reader" in proc.stderr


def test_w1_class_local_positive_control_same_line(tmp_path: Path):
    """A genuine class-local read of an earlier class-body binding -- the
    same-line ``x = 2; y = x`` shape -- stays local: no module requirement is
    invented, and the root returns the class value."""
    scratch = _w1_scratch(tmp_path)
    _append(
        scratch, "nexus_map_codebase.py",
        _vendor_block("zz_class_local", "Class-local positive control root")
        + "def zz_class_local():\n"
          "    class ZzLocal:\n"
          "        x = 2; y = x\n"
          "    return ZzLocal.y\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 0, proc.stderr
    engine = _engine_text(scratch)
    assert "\nx = 2" not in engine, "a class-body binding leaked into the module preamble"
    called = _call_engine(scratch, "print(_engine.zz_class_local())")
    assert called.returncode == 0, called.stderr
    assert called.stdout.strip() == "2"


def test_w1_aliased_stdlib_from_import_keeps_the_bound_alias(tmp_path: Path):
    """A module-level ``from collections import OrderedDict as zz_od`` used
    by a labelled root builds, and the generated preamble binds the alias
    the source bound -- never the bare original without it."""
    scratch = _w1_scratch(tmp_path)
    _append(
        scratch, "nexus_map_codebase.py",
        "\n\nfrom collections import OrderedDict as zz_od\n"
        + _vendor_block("zz_od_user", "Aliased stdlib import probe root")
        + "def zz_od_user():\n"
          "    return list(zz_od(a=1).items())\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 0, proc.stderr
    engine = _engine_text(scratch)
    assert "\nfrom collections import OrderedDict as zz_od\n" in engine
    called = _call_engine(scratch, "print(_engine.zz_od_user())")
    assert called.returncode == 0, called.stderr
    assert called.stdout.strip() == "[('a', 1)]"


def test_w1_aliased_stdlib_module_import_keeps_the_bound_alias(tmp_path: Path):
    """``import json as zz_json`` on the sibling path: the bound-name
    identity cannot be dropped, and the module import renders as an aliased
    module import."""
    scratch = _w1_scratch(tmp_path)
    _append(
        scratch, "nexus_map_codebase.py",
        "\n\nimport json as zz_json\n"
        + _vendor_block("zz_json_user", "Aliased module import probe root")
        + "def zz_json_user():\n"
          "    return zz_json.dumps({'a': 1})\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 0, proc.stderr
    engine = _engine_text(scratch)
    assert "\nimport json as zz_json\n" in engine
    called = _call_engine(scratch, "print(_engine.zz_json_user())")
    assert called.returncode == 0, called.stderr
    assert called.stdout.strip() == '{"a": 1}'


def test_w1_name_bound_only_in_another_source_file_is_rejected(tmp_path: Path):
    """A name whose only standard-library binding lives in a different
    SOURCE_FILES module is never supplied across files: the build stops
    naming the name and its user."""
    scratch = _w1_scratch(tmp_path)
    _append(scratch, "src/workflowy_mcp/snippet_view.py", "\n\nimport zlib\n")
    _append(
        scratch, "nexus_map_codebase.py",
        _vendor_block("zz_zlib_user", "Cross-file stdlib probe root")
        + "def zz_zlib_user():\n"
          "    return zlib.crc32(b'x')\n",
    )
    proc = _run_build(scratch)
    assert proc.returncode == 1, f"the cross-file stdlib fallback still supplies the name: {proc.stdout}"
    assert "zlib" in proc.stderr
    assert "zz_zlib_user" in proc.stderr


def test_w1_renamed_cross_file_import_resolves_through_the_record():
    """A cross-file import of a build-renamed symbol resolves through the
    import record of its original name: the post-rename free name must not
    be refused as 'does not import'."""
    texts = {rel: (REPO_ROOT / rel).read_text(encoding="utf-8") for rel in bak.SOURCE_FILES}
    texts["nexus_map_codebase.py"] += (
        "\n\nfrom workflowy_mcp.client.api_client_nexus import parse_cartographer_note_for_preview\n"
    )
    symbols = bak.extract_symbols(texts)
    rel = "nexus_map_codebase.py"
    synthetic = bak.Symbol(rel, "zz_note_user", 1, "def zz_note_user():\n    return parse_mapped_note('x')\n")
    synthetic.prepared = synthetic.text
    symbols = {k: dict(v) for k, v in symbols.items()}
    symbols[rel]["zz_note_user"] = synthetic
    stdlib_bindings = bak.module_import_bindings(texts)
    source_bindings = bak.source_import_bindings(texts)
    reasons: list[str] = []
    needed, _stdlib_used, _ts = bak.compute_closure(
        {"zz_note_user": rel}, symbols, stdlib_bindings, source_bindings, {}, reasons
    )
    assert reasons == [], reasons
    assert "parse_cartographer_note_for_preview" in needed


def test_w1_source_import_provenance_is_consumed():
    """An import record is a claim about where the name comes from: a
    binding that names a file which does not define the symbol is not a
    legitimate binding, and the build says so naming the user."""
    texts = {rel: (REPO_ROOT / rel).read_text(encoding="utf-8") for rel in bak.SOURCE_FILES}
    texts["src/workflowy_mcp/snippet_view.py"] += "\n\nzz_only_in_b = 5\n"
    texts["nexus_map_codebase.py"] += (
        "\n\nfrom beacon_mutation_contract import zz_only_in_b\n"
    )
    symbols = bak.extract_symbols(texts)
    rel = "nexus_map_codebase.py"
    synthetic = bak.Symbol(rel, "zz_prov_user", 1, "def zz_prov_user():\n    return zz_only_in_b\n")
    synthetic.prepared = synthetic.text
    symbols = {k: dict(v) for k, v in symbols.items()}
    symbols[rel]["zz_prov_user"] = synthetic
    stdlib_bindings = bak.module_import_bindings(texts)
    source_bindings = bak.source_import_bindings(texts)
    reasons: list[str] = []
    bak.compute_closure(
        {"zz_prov_user": rel}, symbols, stdlib_bindings, source_bindings, {}, reasons
    )
    assert reasons, "a mis-filed import was accepted as a binding"
    joined = "\n".join(reasons)
    assert "zz_only_in_b" in joined
    assert "zz_prov_user" in joined


def test_w1_preamble_colliding_alias_bindings_stop_the_build():
    """One flat engine module cannot honour a bound alias two files bind to
    different things: the closure stops the build naming the name."""
    texts = {rel: (REPO_ROOT / rel).read_text(encoding="utf-8") for rel in bak.SOURCE_FILES}
    texts["src/workflowy_mcp/snippet_view.py"] += (
        "\n\nfrom collections import OrderedDict as zz_coll\n"
    )
    texts["src/workflowy_mcp/f8_slice_policy.py"] += (
        "\n\nfrom collections import defaultdict as zz_coll\n"
    )
    symbols = bak.extract_symbols(texts)
    rel_a = "src/workflowy_mcp/snippet_view.py"
    rel_b = "src/workflowy_mcp/f8_slice_policy.py"
    synth_a = bak.Symbol(rel_a, "zz_coll_a", 1, "def zz_coll_a():\n    return zz_coll\n")
    synth_a.prepared = synth_a.text
    synth_b = bak.Symbol(rel_b, "zz_coll_b", 1, "def zz_coll_b():\n    return zz_coll\n")
    synth_b.prepared = synth_b.text
    symbols = {k: dict(v) for k, v in symbols.items()}
    symbols[rel_a]["zz_coll_a"] = synth_a
    symbols[rel_b]["zz_coll_b"] = synth_b
    stdlib_bindings = bak.module_import_bindings(texts)
    source_bindings = bak.source_import_bindings(texts)
    reasons: list[str] = []
    bak.compute_closure(
        {"zz_coll_a": rel_a, "zz_coll_b": rel_b},
        symbols, stdlib_bindings, source_bindings, {}, reasons,
    )
    joined = "\n".join(reasons)
    assert "zz_coll" in joined
    assert "different things" in joined
