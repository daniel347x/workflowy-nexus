#!/usr/bin/env python3
"""Build the agent-kit engine (scripts/_engine.py of the code-map skill) from
the functions of this repository that carry the ``agent-kit-vendor`` beacon
label.

The engine is generated, never edited by hand: every symbol in it is
extracted from this repository's own source, stripped of comments and
docstrings, renamed and reworded by a fixed table, and checked so that it
needs nothing beyond the standard library (plus the guarded tree-sitter
import).  ``--check`` rebuilds in memory and reports whether the committed
engine is current.

Usage (from the repository root):
    python scripts/build_agent_kit.py           write the engine
    python scripts/build_agent_kit.py --check   exit 0 when it is current
"""
from __future__ import annotations

import argparse
import ast
import builtins
import io
import re
import subprocess
import sys
import tokenize
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SOURCE_FILES = [
    "beacon_mutation_contract.py",
    "beacon_source.py",
    "beacon_edits.py",
    "beacon_findings.py",
    "nexus_map_codebase.py",
    "src/workflowy_mcp/snippet_view.py",
    "src/workflowy_mcp/f8_slice_policy.py",
    "src/workflowy_mcp/client/api_client_nexus.py",
]
ENGINE_REL = "agent-kit/.claude/skills/code-map/scripts/_engine.py"
VENDOR_LABEL = "agent-kit-vendor"
ENGINE_LINE1 = (
    "# Generated file: do not edit by hand. "
    "Regenerate it with the build script from the upstream source."
)

# The names bound by nexus_map_codebase.py's guarded tree-sitter import; they
# are provided by the generated preamble, never emitted as symbols.
TREE_SITTER_PREAMBLE_NAMES = {"Parser", "get_language", "get_parser", "_HAVE_TREE_SITTER"}

DUnder = {"__name__", "__file__", "__doc__", "__qualname__", "__module__", "__class__"}

# ---------------------------------------------------------------------------
# The rewrite table (spec 9.6).  Every entry must fire exactly as often as its
# "fires" states, or the build stops naming the entry.
# ---------------------------------------------------------------------------

RENAMES = [
    # (old, new)  -- whole-word identifier renames; each must fire at least once.
    ("parse_cartographer_note_for_preview", "parse_mapped_note"),
    ("_strip_workflowy_tag_underline_markup", "_strip_tag_underline_markup"),
]

STRING_REWRITES = [
    {
        "id": "K-file-missing-authority",
        "symbol": "BEACON_CHECK_CODES",
        "old": '"resolve_cartographer_path_from_node"',
        "new": '"code-map repository file inventory"',
    },
    {
        "id": "K-mapper-error-authority",
        "symbol": "BEACON_CHECK_CODES",
        "old": '"_is_cartographer_parse_error_sentinel over map_codebase"',
        "new": '"code-map outline parser diagnostics"',
    },
    {
        "id": "W1",
        "symbol": "_context_directive_lines",
        "old": (
            '    for index, norm in enumerate(cleaned):\n'
            '        if norm.startswith("@carto-group"):\n'
            '            result[index] = ""\n'
        ),
        "new": "",
    },
    {
        "id": "W4a",
        "symbol": "_prepend_duplicate_beacon_id_warning_nodes",
        "old": '                    "- F12 refresh matches nodes by beacon id.\\n"\n',
        "new": '                    "- Blocks are matched by their id.\\n"\n',
    },
    {
        "id": "W4b",
        "symbol": "_prepend_duplicate_beacon_id_warning_nodes",
        "old": '                    "- Then press F12 again."\n',
        "new": '                    "- Then run the check again."\n',
    },
    {
        "id": "W5",
        "symbol": "validate_beacon_comment_text",
        "old": (
            '            f"unsafe beacon comment {comment!r}: contains a Workflowy "\n'
            '            "transport-entity pattern (would drift on the next F12 write cycle)"\n'
        ),
        "new": '            f"unsafe beacon comment {comment!r}: contains an HTML entity pattern"\n',
    },
]

CARTO_ASSIGN = "carto = _import_cartographer_module()"

# ---------------------------------------------------------------------------
# The forbidden-word table (spec 9.8).  One constant; the A4 test reuses it.
# ---------------------------------------------------------------------------

WORD_MASKS = [r"\.claude", r"\bClaude Code\b"]

AGENT_KIT_FORBIDDEN_CI = [
    r"workflowy",
    r"\bwfy\b",
    r"nexus",
    r"cartographer",
    r"carto",
    r"glimpse",
    r"lightning[ _-]?rod",
    r"\bf12\b",
    r"program[ _-]?conductor",
    r"conductor",
    r"toolkit",
    r"skyview",
    r"master[ _-]?spec",
    r"worker[ _-]?clause",
    r"seat[ _-]?base",
    r"lead[ _-]?seat",
    r"anchor[ _-]?(point|dock)",
    r"\bap-?ids?\b",
    r"\[ap:[0-9a-z]+\]",
    r"\bap:[0-9a-z]{3,}\b",
    r"manager[ _-]?session",
    r"session[ _-]?\d{2,3}",
    r"\bms-?\d{3}\b",
    r"anthropic",
    r"claude",
    r"openai",
    r"chatgpt",
    r"\bgpt-?\d",
    r"gemini",
    r"deepseek",
    r"qwen",
    r"kimi",
    r"moonshot",
    r"\bgrok",
    r"\bopus\b",
    r"sonnet",
    r"\bfable\b",
    r"\bastra\b",
    r"\bsol-?\d",
    r"\bglm\b",
    r"zhipu",
    r"minimax",
    r"daniel347x",
    r"\bdaniel\b",
]
AGENT_KIT_FORBIDDEN_CS = [r"\bDan\b", r"\bDan's\b"]


class BuildError(Exception):
    """Raised with every accumulated reason when the build cannot proceed."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


# ---------------------------------------------------------------------------
# Free-name analysis (spec 9.4): scope-aware, over one symbol's text.
# ---------------------------------------------------------------------------

_FUNCTION_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_COMPREHENSION_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


class _Scope:
    """One lexical scope: what it binds, and what it declares global/nonlocal.

    A binding is recorded in exactly the scope Python binds it in: a
    comprehension target in the comprehension, a nested function's local in
    the nested function, a class-body assignment in the class body.  A store
    whose name the same scope declares ``global`` or ``nonlocal`` writes
    through to the module or an enclosing function at run time and binds
    nothing here, so it can never mask a read in any scope.  A class scope
    also records WHEN each name was bound (the walk's global load/bind
    sequence): a class body executes in order, and a load that runs before
    the class dictionary carries the name falls back to the module
    namespace, so a later class assignment must never mask it."""

    __slots__ = ("kind", "parent", "bound", "bound_at", "global_decl", "nonlocal_decl")

    def __init__(self, kind: str, parent: "_Scope | None"):
        self.kind = kind  # module | function | class | comprehension
        self.parent = parent
        self.bound: set[str] = set()
        self.bound_at: dict[str, int] = {}
        self.global_decl: set[str] = set()
        self.nonlocal_decl: set[str] = set()


class _WalkState:
    """The shared walk sequence: every binding and every load takes the next
    position, so a class scope can tell a load that ran before a binding
    from one that ran after it."""

    __slots__ = ("loads", "pos")

    def __init__(self) -> None:
        self.loads: list = []
        self.pos = 0


def _bind_params(scope: _Scope, args: ast.arguments) -> None:
    for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
        scope.bound.add(arg.arg)
    if args.vararg:
        scope.bound.add(args.vararg.arg)
    if args.kwarg:
        scope.bound.add(args.kwarg.arg)


def _bind_name(name: str, scope: _Scope, state: _WalkState) -> None:
    state.pos += 1
    if scope.kind != "module" and (
        name in scope.global_decl or name in scope.nonlocal_decl
    ):
        return
    scope.bound.add(name)
    if scope.kind == "class":
        scope.bound_at[name] = state.pos


def _walrus_scope(scope: _Scope) -> _Scope:
    """A walrus target binds in the comprehension's containing scope, never
    in the comprehension itself."""
    while scope.kind == "comprehension" and scope.parent is not None:
        scope = scope.parent
    return scope


def _walk_scopes(node: ast.AST, scope: _Scope, state: _WalkState) -> None:
    """Bind every name in the scope Python binds it in, and record every
    load with the scope it resolves from and its position in the walk's
    bind/load sequence.  Decorators, defaults and annotations evaluate where
    the def does, so they are visited in the enclosing scope; a
    comprehension's outermost iterable evaluates in the comprehension's
    containing scope, the rest of it in the comprehension."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for dec in node.decorator_list:
            _walk_scopes(dec, scope, state)
        args = node.args
        for default in list(args.defaults) + [d for d in args.kw_defaults if d is not None]:
            _walk_scopes(default, scope, state)
        params = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
        if args.vararg:
            params.append(args.vararg)
        if args.kwarg:
            params.append(args.kwarg)
        for arg in params:
            if arg.annotation:
                _walk_scopes(arg.annotation, scope, state)
        if node.returns:
            _walk_scopes(node.returns, scope, state)
        child = _Scope("function", scope)
        _bind_params(child, args)
        _bind_name(node.name, scope, state)
        for stmt in node.body:
            _walk_scopes(stmt, child, state)
    elif isinstance(node, ast.Lambda):
        args = node.args
        for default in list(args.defaults) + [d for d in args.kw_defaults if d is not None]:
            _walk_scopes(default, scope, state)
        child = _Scope("function", scope)
        _bind_params(child, args)
        _walk_scopes(node.body, child, state)
    elif isinstance(node, ast.ClassDef):
        for dec in node.decorator_list:
            _walk_scopes(dec, scope, state)
        for base in node.bases:
            _walk_scopes(base, scope, state)
        for keyword in node.keywords:
            _walk_scopes(keyword.value, scope, state)
        _bind_name(node.name, scope, state)
        child = _Scope("class", scope)
        for stmt in node.body:
            _walk_scopes(stmt, child, state)
    elif isinstance(node, _COMPREHENSION_NODES):
        _walk_scopes(node.generators[0].iter, scope, state)
        child = _Scope("comprehension", scope)
        for index, gen in enumerate(node.generators):
            if index:
                _walk_scopes(gen.iter, child, state)
            _walk_scopes(gen.target, child, state)
            for cond in gen.ifs:
                _walk_scopes(cond, child, state)
        if isinstance(node, ast.DictComp):
            _walk_scopes(node.key, child, state)
            _walk_scopes(node.value, child, state)
        else:
            _walk_scopes(node.elt, child, state)
    elif isinstance(node, ast.Assign):
        # Python evaluates the value first, then binds the targets in
        # order; ast's child order is targets-then-value.
        _walk_scopes(node.value, scope, state)
        for target in node.targets:
            _walk_scopes(target, scope, state)
    elif isinstance(node, ast.AnnAssign):
        if node.annotation:
            _walk_scopes(node.annotation, scope, state)
        if node.value is not None:
            _walk_scopes(node.value, scope, state)
        if node.target is not None:
            _walk_scopes(node.target, scope, state)
    elif isinstance(node, ast.AugAssign):
        # x += v loads x (the class dictionary may not hold it yet: that
        # load can fall back to the module namespace), then stores it.
        if node.value is not None:
            _walk_scopes(node.value, scope, state)
        if isinstance(node.target, ast.Name):
            state.pos += 1
            state.loads.append((ast.copy_location(ast.Name(id=node.target.id, ctx=ast.Load()), node.target), scope, state.pos))
            _bind_name(node.target.id, scope, state)
        else:
            _walk_scopes(node.target, scope, state)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        _walk_scopes(node.iter, scope, state)
        _walk_scopes(node.target, scope, state)
        for stmt in node.body:
            _walk_scopes(stmt, scope, state)
        for stmt in node.orelse:
            _walk_scopes(stmt, scope, state)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            _walk_scopes(item.context_expr, scope, state)
            if item.optional_vars is not None:
                _walk_scopes(item.optional_vars, scope, state)
        for stmt in node.body:
            _walk_scopes(stmt, scope, state)
    elif isinstance(node, ast.NamedExpr):
        _walk_scopes(node.value, scope, state)
        _bind_name(node.target.id, _walrus_scope(scope), state)
    elif isinstance(node, ast.Global):
        scope.global_decl.update(node.names)
    elif isinstance(node, ast.Nonlocal):
        scope.nonlocal_decl.update(node.names)
    elif isinstance(node, ast.Name):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            _bind_name(node.id, scope, state)
        else:
            state.pos += 1
            state.loads.append((node, scope, state.pos))
    elif isinstance(node, ast.Import):
        for alias in node.names:
            _bind_name((alias.asname or alias.name).split(".")[0], scope, state)
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            if alias.name != "*":
                _bind_name(alias.asname or alias.name, scope, state)
    elif isinstance(node, ast.ExceptHandler):
        if node.name:
            _bind_name(node.name, scope, state)
        if node.type:
            _walk_scopes(node.type, scope, state)
        for stmt in node.body:
            _walk_scopes(stmt, scope, state)
    elif isinstance(node, ast.MatchAs):
        if node.name:
            _bind_name(node.name, scope, state)
        if node.pattern:
            _walk_scopes(node.pattern, scope, state)
    elif isinstance(node, ast.MatchStar):
        if node.name:
            _bind_name(node.name, scope, state)
    elif isinstance(node, ast.MatchMapping):
        if node.rest:
            _bind_name(node.rest, scope)
        for key in node.keys:
            if key is not None:
                _walk_scopes(key, scope, state)
        for pattern in node.patterns:
            _walk_scopes(pattern, scope, state)
    else:
        for child_node in ast.iter_child_nodes(node):
            _walk_scopes(child_node, scope, state)


def _name_resolves_locally(name: str, scope: _Scope, pos: int) -> bool:
    """True when the load resolves inside the scopes it can see: its own
    bindings, enclosing function scopes (closure), or the class namespaces
    above it while no function boundary has been crossed.  A name declared
    nonlocal resolves only through a binding in an enclosing function; a
    name declared global is always a module-level read.  A class scope
    resolves a load only when its binding ran BEFORE the load in the class
    body's execution order: a class-body load of a name the class dictionary
    does not hold yet falls back to the module namespace, so a later
    class-body assignment must not mask it."""
    if name in scope.nonlocal_decl:
        enclosing = scope.parent
        while enclosing is not None:
            if enclosing.kind == "function" and name in enclosing.bound:
                return True
            enclosing = enclosing.parent
        return False
    if name in scope.global_decl:
        return False
    crossed_function = scope.kind in ("function", "comprehension")
    current: _Scope | None = scope
    while current is not None:
        if (
            not (crossed_function and current.kind == "class")
            and name in current.bound
        ):
            if current.kind == "class" and current.bound_at.get(name, 0) >= pos:
                pass  # bound later in the class body: the load reads the
                # class dictionary before the name exists in it, and Python
                # resolves it in the module namespace instead.
            else:
                return True
        if current.kind in ("function", "comprehension"):
            crossed_function = True
        current = current.parent
    return False


def free_names(text: str) -> set[str]:
    """Every name the text reads that resolves at module level: not bound in
    its own scope, any enclosing function scope, or a class namespace before
    the class body bound it, and not global-declared (a global read is a
    module-level read too).  Builtins and interpreter dunders excluded.  A
    comprehension target, a nested function's local, a class-body assignment
    or a global/nonlocal declaration therefore never masks an external read.
    """
    tree = ast.parse(text)
    module_scope = _Scope("module", None)
    state = _WalkState()
    for stmt in tree.body:
        _walk_scopes(stmt, module_scope, state)
    free: set[str] = set()
    for node, scope, pos in state.loads:
        name = node.id
        if name in dir(builtins) or name in DUnder:
            continue
        if not _name_resolves_locally(name, scope, pos):
            free.add(name)
    return free


# ---------------------------------------------------------------------------
# Stripping (spec 9.5): comments, then docstrings; token-level surgery.
# ---------------------------------------------------------------------------

def remove_comment_tokens(text: str) -> str:
    src_lines = text.split("\n")
    lines = list(src_lines)
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type != tokenize.COMMENT:
            continue
        row, col = tok.start
        line = lines[row - 1]
        s = col
        while s > 0 and line[s - 1] in " \t":
            s -= 1
        lines[row - 1] = line[:s]
    out = []
    for raw, new in zip(src_lines, lines):
        if new.strip() == "" and raw.lstrip().startswith("#"):
            continue  # a line that held only a comment is removed whole
        out.append(new)
    return "\n".join(out)


def remove_docstrings(text: str) -> str:
    lines = text.split("\n")
    tree = ast.parse(text)
    edits: list[tuple[int, int, str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        if len(body) == 1 and not isinstance(node, ast.Module):
            edits.append((first.lineno - 1, first.end_lineno - 1, " " * first.col_offset + "pass"))
        else:
            edits.append((first.lineno - 1, first.end_lineno - 1, None))
    for start, end, repl in sorted(edits, reverse=True):
        if repl is None:
            del lines[start:end + 1]
        else:
            lines[start:end + 1] = [repl]
    return "\n".join(lines)


def strip_symbol(text: str) -> str:
    return remove_docstrings(remove_comment_tokens(text))


# ---------------------------------------------------------------------------
# Rewrites (spec 9.6).
# ---------------------------------------------------------------------------

def apply_renames(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for old, new in RENAMES:
        n = len(re.findall(r"\b%s\b" % re.escape(old), text))
        counts[old] = n
        if n:
            text = re.sub(r"\b%s\b" % re.escape(old), new, text)
    return text, counts


def apply_string_rewrites(symbol_name: str, text: str, fire_counts: dict[str, int]) -> str:
    for entry in STRING_REWRITES:
        if entry["symbol"] != symbol_name:
            continue
        n = text.count(entry["old"])
        fire_counts[entry["id"]] = fire_counts.get(entry["id"], 0) + n
        text = text.replace(entry["old"], entry["new"])
    return text


def check_rewrite_fires(fire_counts: dict[str, int], rename_counts: dict[str, int]) -> list[str]:
    """Every table entry must fire exactly as often as stated (spec 9.6)."""
    reasons: list[str] = []
    for entry in STRING_REWRITES:
        got = fire_counts.get(entry["id"], 0)
        if got != 1:
            reasons.append(f"rewrite {entry['id']} fired {got} times, expected exactly 1")
    for old, _new in RENAMES:
        if rename_counts.get(old, 0) < 1:
            reasons.append(f"rename {old!r} never fired")
    return reasons


def apply_binding_rewrites(
    text: str, extracted_names: set[str]
) -> tuple[str, list[str], list[str]]:
    """carto.X -> X (recording every X: the carto alias is a legitimate
    binding to nexus_map_codebase's symbol); drop the carto assignment; drop
    function-local imports whose every name is extracted (recording the
    deleted names: their import was a real binding); keep function-local
    stdlib imports."""
    carto_names: list[str] = []

    def _strip_alias(match: re.Match) -> str:
        carto_names.append(match.group(1))
        return match.group(1)

    text = re.sub(r"\bcarto\.([A-Za-z_][A-Za-z0-9_]*)", _strip_alias, text)
    lines = [l for l in text.split("\n") if l.strip() != CARTO_ASSIGN]
    text = "\n".join(lines)
    leftover = [l for l in lines if re.search(r"\bcarto\b", l)]
    if leftover:
        raise BuildError([f"unexpected use of 'carto' after the alias rewrite: {leftover[0].strip()!r}"])

    tree = ast.parse(text)
    drop: list[tuple[int, int]] = []
    deleted: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level > 0:
                missing = [a.asname or a.name for a in node.names if (a.asname or a.name) not in extracted_names]
                if missing:
                    raise BuildError(
                        [f"function-local relative import carries non-extracted name(s) {missing}"]
                    )
                drop.append((node.lineno - 1, node.end_lineno - 1))
                deleted.extend(a.asname or a.name for a in node.names)
                continue
            if mod.split(".")[0] in sys.stdlib_module_names:
                continue  # a function-local standard-library import is kept
            raise BuildError([f"function-local import of non-stdlib module {mod!r} cannot be carried into the engine"])
        elif isinstance(node, ast.Import):
            top = node.names[0].name.split(".")[0]
            if top in sys.stdlib_module_names:
                continue
            raise BuildError([f"function-local import of non-stdlib module {top!r} cannot be carried into the engine"])
    for start, end in sorted(drop, reverse=True):
        del lines[start:end + 1]
    return "\n".join(lines), deleted, carto_names


# ---------------------------------------------------------------------------
# The word check (spec 9.8).
# ---------------------------------------------------------------------------

def forbidden_word_hits(text: str) -> list[str]:
    masked = text
    for m in WORD_MASKS:
        masked = re.sub(m, "", masked, flags=re.IGNORECASE)
    hits = []
    for pattern in AGENT_KIT_FORBIDDEN_CI:
        m = re.search(pattern, masked, re.IGNORECASE)
        if m:
            hits.append(f"/{pattern}/ matched {m.group(0)!r}")
    for pattern in AGENT_KIT_FORBIDDEN_CS:
        m = re.search(pattern, masked)
        if m:
            hits.append(f"/{pattern}/ (case-sensitive) matched {m.group(0)!r}")
    return hits


# ---------------------------------------------------------------------------
# Root discovery (spec 9.2) and extraction (spec 9.3).
# ---------------------------------------------------------------------------

def git_tracked_py_files(repo_root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "*.py"], cwd=str(repo_root),
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Not a git checkout (the test harness runs builds in a gitless clone
        # of the working tree): walk the tree, skipping dot-directories and
        # the same exclusions.  A tracked file under a dot-directory would be
        # missed here; no such file carries the vendor label.
        result = []
        for path in sorted(repo_root.rglob("*.py")):
            rel = path.relative_to(repo_root).as_posix()
            parts = rel.split("/")
            if any(part.startswith(".") or part == "__pycache__" for part in parts[:-1]):
                continue
            if parts[0] in ("tests", "agent-kit"):
                continue
            if rel == "scripts/build_agent_kit.py":
                continue
            result.append(rel)
        return sorted(result)
    files = [f for f in out.stdout.split("\0") if f]
    result = []
    for rel in files:
        p = rel.replace("\\", "/")
        if p.startswith("tests/") or p.startswith("agent-kit/") or p == "scripts/build_agent_kit.py":
            continue
        result.append(p)
    return sorted(result)


def import_nexus(repo_root: Path):
    for p in (str(repo_root / "src"), str(repo_root)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import nexus_map_codebase as nmc
    from workflowy_mcp.client.api_client_nexus import parse_cartographer_note_for_preview
    return nmc, parse_cartographer_note_for_preview


def discover_roots(repo_root: Path, reasons: list[str]) -> dict[str, str]:
    """qualname -> repository-relative source file, from the live labels."""
    nmc, parse_mapped_note = import_nexus(repo_root)
    slice_label_tags = nmc._slice_label_tags
    roots: dict[str, str] = {}
    for rel in git_tracked_py_files(repo_root):
        text = (repo_root / rel).read_text(encoding="utf-8")
        if VENDOR_LABEL not in text:
            continue
        outline = nmc.parse_file_outline(str(repo_root / rel))
        ids_in_notes: set[str] = set()
        found: list[tuple[str, dict]] = []

        def walk(nodes):
            for n in nodes or []:
                note = str(n.get("note") or "")
                items = parse_mapped_note(note)
                records = [payload for kind, payload in items if kind == "beacon"]
                for rec in records:
                    ids_in_notes.add(str(rec.get("id") or ""))
                    labels = [t[1:] for t in slice_label_tags(rec.get("slice_labels") or "").split()]
                    if VENDOR_LABEL in labels:
                        found.append((str(n.get("ast_qualname") or ""), rec))
                walk(n.get("children") or [])

        walk(outline)
        for qualname, rec in found:
            if rec.get("kind") == "span":
                reasons.append(f"{rel}: span block {rec.get('id')!r} carries {VENDOR_LABEL}; span blocks never vendor")
                continue
            if not qualname or "." in qualname:
                reasons.append(f"{rel}: root {qualname!r} is not module-level")
                continue
            if rec.get("id") not in ids_in_notes:
                reasons.append(f"{rel}: block {rec.get('id')!r} carries {VENDOR_LABEL} but attaches to nothing")
                continue
            if rel not in SOURCE_FILES:
                reasons.append(f"{rel}: root {qualname!r} is outside SOURCE_FILES")
                continue
            roots[qualname] = rel
    return roots


@dataclass
class Symbol:
    rel: str
    name: str
    lineno: int
    text: str
    prepared: str | None = None


def extract_symbols(texts: dict[str, str]) -> dict[str, dict[str, Symbol]]:
    """file rel -> {symbol name: Symbol}, in source order.  Module-level
    functions, classes and simple-name assignments; the guarded tree-sitter
    try binds preamble names only."""
    result: dict[str, dict[str, Symbol]] = {}
    for rel in SOURCE_FILES:
        text = texts[rel]
        lines = text.split("\n")
        tree = ast.parse(text)
        syms: dict[str, Symbol] = {}
        order: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
                start = node.lineno
                if node.decorator_list:
                    start = min([d.lineno for d in node.decorator_list] + [start])
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name, start = node.targets[0].id, node.lineno
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name, start = node.target.id, node.lineno
            else:
                continue
            end = node.end_lineno or start
            syms[name] = Symbol(rel, name, start, "\n".join(lines[start - 1:end]))
            order.append(name)
        result[rel] = {n: syms[n] for n in order}
    return result


def find_tree_sitter_guard(texts: dict[str, str]) -> str:
    text = texts["nexus_map_codebase.py"]
    lines = text.split("\n")
    for node in ast.parse(text).body:
        if not isinstance(node, ast.Try):
            continue
        src = "\n".join(lines[node.lineno - 1:node.end_lineno])
        if "tree_sitter" in src and "_HAVE_TREE_SITTER" in src:
            return strip_symbol(src)
    raise BuildError(["the guarded tree-sitter import was not found in nexus_map_codebase.py"])


# ---------------------------------------------------------------------------
# The closure (spec 9.4).
# ---------------------------------------------------------------------------

def module_import_bindings(texts: dict[str, str]) -> dict[str, list[tuple[str, str | None, str]]]:
    """binding name -> [(module, original name or None for `import M`, file)],
    over every source file's module-level standard-library imports.  A name
    resolves through the binding of the symbol's own file first; a name
    needed with two different bindings and no own-file binding stops the build."""
    bindings: dict[str, list[tuple[str, str | None, str]]] = {}

    def record(name: str, mod: str, orig: str | None, rel: str) -> None:
        entry = (mod, orig, rel)
        if entry not in bindings.setdefault(name, []):
            bindings[name].append(entry)

    for rel in SOURCE_FILES:
        tree = ast.parse(texts[rel])
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        record(alias.asname, alias.name, alias.name, rel)
                    else:
                        top = alias.name.split(".")[0]
                        record(top, top, None, rel)
            elif isinstance(node, ast.ImportFrom):
                if node.level != 0 or not node.module:
                    continue
                if node.module.split(".")[0] not in sys.stdlib_module_names:
                    continue  # cross-source imports are recorded separately
                for alias in node.names:
                    record(alias.asname or alias.name, node.module, alias.name, rel)
    return bindings


# The dotted module each SOURCE_FILES file provides, for resolving imports.
SOURCE_MODULE_BY_NAME = {
    "beacon_mutation_contract": "beacon_mutation_contract.py",
    "beacon_source": "beacon_source.py",
    "beacon_edits": "beacon_edits.py",
    "beacon_findings": "beacon_findings.py",
    "nexus_map_codebase": "nexus_map_codebase.py",
    "workflowy_mcp.snippet_view": "src/workflowy_mcp/snippet_view.py",
    "workflowy_mcp.f8_slice_policy": "src/workflowy_mcp/f8_slice_policy.py",
    "workflowy_mcp.client.api_client_nexus": "src/workflowy_mcp/client/api_client_nexus.py",
}


def _dotted_package(rel: str) -> str:
    """The dotted package a SOURCE_FILES file lives in ('' at the root)."""
    package = rel.rsplit("/", 1)[0] if "/" in rel else ""
    parts = []
    for part in package.split("/"):
        if part == "src":
            continue
        parts.append(part)
    return ".".join(parts)


def _absolute_module(node: ast.ImportFrom, package: str) -> str | None:
    if node.level == 0:
        return node.module
    parts = package.split(".") if package else []
    if node.level - 1 > len(parts):
        return None
    base = parts[: len(parts) - (node.level - 1)]
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base) if base else None


def source_import_bindings(texts: dict[str, str]) -> dict[str, dict[str, str]]:
    """file -> {imported name: the SOURCE_FILES file it is imported from},
    over module-level imports of one source file from another (absolute or
    relative).  A cross-file free name is supplied only through such a real
    import, or through the build's own binding rewrites (the carto alias and
    the deleted function-local relative imports) -- never merely because the
    name exists in the flattened union."""
    bindings: dict[str, dict[str, str]] = {}
    for rel in SOURCE_FILES:
        package = _dotted_package(rel)
        tree = ast.parse(texts[rel])
        for node in tree.body:
            candidates: list[tuple[str, str | None]] = []
            if isinstance(node, ast.ImportFrom):
                target = _absolute_module(node, package)
                if target is None:
                    continue
                for alias in node.names:
                    if alias.name != "*":
                        candidates.append((alias.asname or alias.name, target))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    candidates.append((alias.asname or alias.name, alias.name))
            for bound_name, target in candidates:
                target_rel = SOURCE_MODULE_BY_NAME.get(target)
                if target_rel is None or target_rel == rel:
                    continue
                # Python semantics: a later import of the same name shadows
                # an earlier one in the same file.
                bindings.setdefault(rel, {})[bound_name] = target_rel
    return bindings


def render_imports(plain: set[tuple[str, str]], froms: dict[str, set[tuple[str, str]]]) -> list[str]:
    """`import X` lines sorted by module, then `from M import a, b` lines
    sorted by module with the names sorted (spec 9.7.2).  An aliased binding
    renders WITH its bound name -- `from collections import OrderedDict as
    zz_od`, `import json as zz_json` -- so the engine binds what the source
    bound; an unaliased binding renders exactly as before."""
    out = []
    for mod, bound in sorted(plain):
        out.append(f"import {mod}" if bound == mod else f"import {mod} as {bound}")
    for mod in sorted(froms):
        parts = [
            orig if bound == orig else f"{orig} as {bound}"
            for orig, bound in sorted(froms[mod])
        ]
        out.append(f"from {mod} import {', '.join(parts)}")
    return out


def compute_closure(
    roots: dict[str, str],
    symbols_by_file: dict[str, dict[str, Symbol]],
    stdlib_bindings: dict[str, list[tuple[str, str | None, str]]],
    source_bindings: dict[str, dict[str, str]],
    rewrites: dict[tuple[str, str], dict[str, set[str]]],
    reasons: list[str],
) -> tuple[set[str], set[tuple[str, str | None, str]], set[str]]:
    """Returns (needed symbol names, the concrete stdlib bindings used as
    (module, original-name-or-None, bound-name) triples, and the tree-sitter
    preamble names the closure reads).

    A free name of the symbol's own file resolves, in order: the file's own
    module-level symbol; a real import (a module-level import from another
    SOURCE_FILES file, resolved through its recorded source file, the
    build's carto alias, or a function-local import the rewrite deleted);
    the file's own standard-library import; a builtin.  A name defined in
    another source file is NOT supplied merely because it exists in the
    flattened union, and neither is a standard-library binding that only
    another source file holds: both stop the build naming the name and its
    users."""
    union: dict[str, Symbol] = {}
    for rel in SOURCE_FILES:
        for name, sym in symbols_by_file[rel].items():
            if name in union:
                raise BuildError([f"symbol {name!r} is defined in two source files: {union[name].rel} and {sym.rel}"])
            union[name] = sym

    aliases = {new: old for old, new in RENAMES}

    needed: set[str] = set()
    stdlib_used: set[tuple[str, str | None, str]] = set()
    ts_used: set[str] = set()
    stdlib_bindings_by_file: dict[str, dict[str, tuple[str, str | None]]] = {}
    for bname, entries in stdlib_bindings.items():
        for mod, orig, brel in entries:
            stdlib_bindings_by_file.setdefault(brel, {})[bname] = (mod, orig)
    missing: dict[str, set[str]] = {}
    unimported: dict[str, set[str]] = {}
    pending = deque(sorted((roots[q], q) for q in roots))
    while pending:
        rel, name = pending.popleft()
        if name in needed:
            continue
        needed.add(name)
        sym = union[name]
        assert sym.rel == rel
        own_symbols = symbols_by_file[rel]
        own_source_imports = source_bindings.get(rel, {})
        own_stdlib = stdlib_bindings_by_file.get(rel, {})
        per_symbol = rewrites.get((rel, name), {})
        carto_names = per_symbol.get("carto", set())
        deleted_imports = per_symbol.get("deleted_imports", set())
        for fname in sorted(free_names(sym.prepared or sym.text)):
            if fname in TREE_SITTER_PREAMBLE_NAMES:
                ts_used.add(fname)
                continue
            target = aliases.get(fname, fname)
            supplied: Symbol | None = None
            if target in own_symbols:
                supplied = own_symbols[target]
            elif target in own_source_imports or fname in carto_names or fname in deleted_imports:
                # A build-renamed name resolves through the import record of
                # its original name (the records hold what the source
                # imports); the explicit rewrites are their own provenance.
                supplied = union.get(target)
                if supplied is None:
                    missing.setdefault(fname, set()).add(f"{rel}:{name}")
                    continue
                recorded = own_source_imports.get(target)
                if recorded is not None and supplied.rel != recorded:
                    # the import names a file that does not define the
                    # symbol: not a legitimate binding
                    missing.setdefault(fname, set()).add(f"{rel}:{name}")
                    continue
            if supplied is not None:
                if target not in needed:
                    pending.append((supplied.rel, target))
                continue
            if fname in own_stdlib:
                stdlib_used.add(own_stdlib[fname] + (fname,))
                continue
            if fname in DUnder:
                continue
            if target in union:
                unimported.setdefault(fname, set()).add(f"{rel}:{name}")
            else:
                missing.setdefault(fname, set()).add(f"{rel}:{name}")
    if unimported:
        for fname in sorted(unimported):
            users = ", ".join(sorted(unimported[fname]))
            reasons.append(
                f"name {fname!r} (used by: {users}) is defined in another source file "
                f"that its own file does not import"
            )
    if missing:
        for fname in sorted(missing):
            users = ", ".join(sorted(missing[fname]))
            reasons.append(f"unresolved name {fname!r} (used by: {users})")
    # One flat engine module: a bound name two files bind to different
    # things could only be honoured for one of them (spec 9.7.2).
    bindings_by_bound: dict[str, set[tuple[str, str | None]]] = {}
    for mod, orig, bound in stdlib_used:
        bindings_by_bound.setdefault(bound, set()).add((mod, orig))
    for bound, entries in sorted(bindings_by_bound.items()):
        if len(entries) > 1:
            reasons.append(
                f"name {bound!r} is bound to different things in the source "
                f"files: {sorted(entries)}"
            )
    return needed, stdlib_used, ts_used


# ---------------------------------------------------------------------------
# The build (spec 9.1).
# ---------------------------------------------------------------------------

@dataclass
class BuildResult:
    text: str
    n_symbols: int = field(default=0)
    n_roots: int = field(default=0)


def build_engine(repo_root: Path) -> BuildResult:
    """Build the exact closure, ordering mixed plain/aliased imports deterministically."""
    reasons: list[str] = []

    texts = {rel: (repo_root / rel).read_text(encoding="utf-8") for rel in SOURCE_FILES}
    symbols_by_file = extract_symbols(texts)
    extracted_names = {name for rel in SOURCE_FILES for name in symbols_by_file[rel]}

    roots = discover_roots(repo_root, reasons)
    if not roots and not reasons:
        reasons.append(f"no {VENDOR_LABEL} roots discovered")

    # strip, rename, rewrite, drop bindings -- per symbol.  A binding error
    # (a function-local import the engine could not carry) is DEFERRED until
    # the closure decides whether the symbol reaches the engine at all.
    fire_counts: dict[str, int] = {}
    rename_counts: dict[str, int] = {old: 0 for old, _n in RENAMES}
    deferred: dict[tuple[str, str], list[str]] = {}
    carto_names: dict[tuple[str, str], set[str]] = {}
    deleted_imports: dict[tuple[str, str], set[str]] = {}
    for rel in SOURCE_FILES:
        for name, sym in symbols_by_file[rel].items():
            text = strip_symbol(sym.text)
            text, counts = apply_renames(text)
            for old, n in counts.items():
                rename_counts[old] += n
            text = apply_string_rewrites(name, text, fire_counts)
            try:
                text, deleted, carto = apply_binding_rewrites(text, extracted_names)
                deleted_imports[(rel, name)] = set(deleted)
                carto_names[(rel, name)] = set(carto)
            except BuildError as e:
                deferred[(rel, name)] = e.reasons
            try:
                ast.parse(text, feature_version=(3, 10))
            except SyntaxError as e:
                reasons.append(f"{rel}:{name}: does not parse after stripping/rewrites: {e}")
            sym.prepared = text

    reasons.extend(check_rewrite_fires(fire_counts, rename_counts))
    if reasons:
        raise BuildError(reasons)

    stdlib_bindings = module_import_bindings(texts)
    source_bindings = source_import_bindings(texts)
    rewrites = {
        key: {
            "carto": carto_names.get(key, set()),
            "deleted_imports": deleted_imports.get(key, set()),
        }
        for key in set(carto_names) | set(deleted_imports)
    }
    needed, stdlib_used, ts_used = compute_closure(
        roots, symbols_by_file, stdlib_bindings, source_bindings, rewrites, reasons
    )
    for (rel, name), errs in sorted(deferred.items()):
        if name in needed:
            reasons.extend(f"{rel}:{name}: {e}" for e in errs)
    if reasons:
        raise BuildError(reasons)

    plain: set[tuple[str, str]] = set()
    froms: dict[str, set[tuple[str, str]]] = {}
    for mod, orig, bound in sorted(stdlib_used, key=lambda entry: (entry[0], entry[1] or "", entry[2])):
        if orig is None or orig == mod:
            # `import M` or `import M as B`
            plain.add((mod, bound))
        else:
            froms.setdefault(mod, set()).add((orig, bound))

    ts_guard = ""
    if ts_used:
        ts_guard = find_tree_sitter_guard(texts)

    body: list[str] = []
    for rel in SOURCE_FILES:
        for name, sym in symbols_by_file[rel].items():
            if name in needed:
                body.append(sym.prepared or "")

    pieces: list[str] = [ENGINE_LINE1]
    imports_block = "\n".join(render_imports(plain, froms))
    if imports_block:
        pieces.append(imports_block)
    if ts_guard:
        pieces.append(ts_guard)
    pieces.append("\n\n\n".join(body))
    text = "\n\n".join(pieces) + "\n"

    hits = forbidden_word_hits(text)
    if hits:
        raise BuildError([f"forbidden word in engine: {h}" for h in hits])
    try:
        ast.parse(text, feature_version=(3, 10))
    except SyntaxError as e:
        raise BuildError([f"the generated engine does not parse: {e}"])
    return BuildResult(text=text, n_symbols=len(body), n_roots=len(roots))


def engine_path(repo_root: Path) -> Path:
    return repo_root / ENGINE_REL


def run_import_check(repo_root: Path) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-c", "import _engine"],
        cwd=str(engine_path(repo_root).parent),
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return [f"the engine does not import in a fresh interpreter: {proc.stderr.strip()}"]
    return []


def build_or_fail(repo_root: Path) -> BuildResult:
    try:
        return build_engine(repo_root)
    except BuildError as e:
        for reason in e.reasons:
            print(reason, file=sys.stderr)
        raise SystemExit(1)


# @beacon[
#   id=agent-kit@build_entry,
#   slice_labels=agent-kit,
#   kind=ast,
#   comment=The build's entry point: regenerates the code-map skill's generated engine from the agent-kit-vendor-labelled sources of this repository (--check reports whether the committed engine is current),
# ]
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the code-map skill's generated engine from the labelled sources.")
    parser.add_argument("--check", action="store_true", help="compare the committed engine with a fresh build")
    args = parser.parse_args(argv)

    result = build_or_fail(REPO_ROOT)

    if args.check:
        committed = engine_path(REPO_ROOT)
        current = committed.read_text(encoding="utf-8") if committed.exists() else ""
        if current == result.text:
            print("engine is current")
            return 0
        print("engine is stale: run python scripts/build_agent_kit.py")
        return 1

    target = engine_path(REPO_ROOT)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result.text, encoding="utf-8", newline="\n")
    failures = run_import_check(REPO_ROOT)
    if failures:
        target.unlink(missing_ok=True)
        for reason in failures:
            print(reason, file=sys.stderr)
        return 1
    print(
        f"engine: {result.n_symbols} symbols from {result.n_roots} roots, "
        f"{len(result.text.splitlines())} lines"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
