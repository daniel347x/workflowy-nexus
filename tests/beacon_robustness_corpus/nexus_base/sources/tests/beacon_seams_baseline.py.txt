"""U01 §9.1 route (b): the shared-seam baseline, and its generator.

The baseline (``beacon_seams_baseline.json``) is the BASE code's own output,
commit ``BASE_REV``, for the functions U01 edits, over a committed corpus:

* the frozen copies in ``beacon_seams_corpus/`` (the two most beacon-dense
  tracked files of each parser language the repository tracks: Python,
  JavaScript and Markdown; byte-identical to their ``BASE_REV`` blobs, stored
  as ``.txt`` so they are never mapped or parsed as source), and
* every text fixture of ``beacon_check_fixtures``.

Route (a) (``git show BASE_REV`` at test time) cannot run: the test runner's
clone carries no ``.git``. So the baseline is generated once, from the base
code, and committed before the first edit of ``nexus_map_codebase.py``.

Regenerate (only when a fixture changes), from the repository root, with the
test clone's interpreter::

    PYTHONDONTWRITEBYTECODE=1 <clone>/.venv/bin/python -B tests/beacon_seams_baseline.py

It loads ``nexus_map_codebase.py`` and ``beacon_mutation_contract.py`` from
``git show BASE_REV`` (``--worktree`` uses the working tree instead, which is
the base code only before any edit) and writes the JSON file.

The ``compute_*`` functions are shared: the generator passes the base module,
``tests/test_beacon_shared_seams.py`` passes the current one, and both call
the edited functions with their pre-U01 signatures only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
CORPUS_DIR = TESTS_DIR / "beacon_seams_corpus"
GOLDEN_PATH = TESTS_DIR / "beacon_seams_baseline.json"
BASE_REV = "0c2ad8312be620a7b8cb397e271461dc9d50b3c2"

for _p in (str(REPO_ROOT / "src"), str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import beacon_check_fixtures as fx  # noqa: E402

PARSER_FUNCS = {
    "python": "parse_python_beacon_blocks",
    "js": "parse_js_beacon_blocks",
    "markdown": "parse_markdown_beacon_blocks",
    "sql": "parse_sql_beacon_blocks",
    "sh": "parse_sh_beacon_blocks",
    "yaml": "parse_yaml_beacon_blocks",
}

# The writers' extension table (acn _BEACON_LANGUAGE_BY_EXT), restated here so
# the baseline does not depend on the daemon package; the seam test checks
# the two agree.
LANGUAGE_BY_EXT = {
    ".py": "python", ".js": "js_ts", ".jsx": "js_ts", ".ts": "js_ts",
    ".tsx": "js_ts", ".vue": "vue", ".md": "markdown", ".markdown": "markdown",
    ".sql": "sql", ".yml": "yaml", ".yaml": "yaml", ".sh": "shell",
    ".bash": "shell",
}

_TS_RE = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")


def normalize(obj: Any, root: str | None = None) -> Any:
    """JSON round-trip with ``_warn_name``'s timestamp suffix replaced by one
    fixed token and the materialization directory by ``<ROOT>``."""
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    text = _TS_RE.sub("[TS]", text)
    if root:
        text = text.replace(json.dumps(root, ensure_ascii=False)[1:-1], "<ROOT>")
    return json.loads(text)


def digest(obj: Any, root: str | None = None) -> str:
    canon = json.dumps(normalize(obj, root), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def all_sources() -> dict[str, tuple[str, str]]:
    """key -> (file name, exact text), corpus first, then the fixtures."""
    out: dict[str, tuple[str, str]] = {}
    for path in sorted(CORPUS_DIR.glob("*.txt")):
        name = path.name[: -len(".txt")]
        out[f"corpus:{name}"] = (name, path.read_bytes().decode("utf-8"))
    for name, (fname, text) in fx.all_text_fixtures().items():
        out[f"fixture:{name}"] = (fname, text)
    return out


def materialize(root: str, sources: dict[str, tuple[str, str]]) -> dict[str, str]:
    """Write each source into its own directory under ``root``; key -> path."""
    paths: dict[str, str] = {}
    for key, (fname, text) in sources.items():
        d = os.path.join(root, key.replace(":", "_"))
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, fname)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        paths[key] = p
    return paths


def language_of(fname: str) -> str | None:
    return LANGUAGE_BY_EXT.get(os.path.splitext(fname)[1].lower())


def compute_parsers(nmc, text: str) -> dict[str, list]:
    lines = text.splitlines()
    return {short: getattr(nmc, fn)(lines) for short, fn in PARSER_FUNCS.items()}


def compute_disk_parse(nmc, language: str, path: str) -> Any:
    lines, beacons = nmc._parse_disk_beacons_for_language(language, path)
    return {"lines": lines, "beacons": beacons}


def _outline_targets(tree: dict) -> tuple[list[str], list[int]]:
    """(every AST qualname, every 0-based target line) in a mapped tree."""
    quals: list[str] = []
    idxs: set[int] = set()

    def walk(node: dict) -> None:
        q = node.get("ast_qualname")
        if node.get("ast_type") and q:
            quals.append(q)
            ln = node.get("orig_lineno_start_unused")
            if isinstance(ln, int) and ln > 0:
                idxs.add(ln - 1)
        md = node.get("_md_heading_line")
        if isinstance(md, int) and md > 0:
            idxs.add(md - 1)
        for c in node.get("children") or []:
            walk(c)

    walk(tree)
    return sorted(set(quals)), sorted(idxs)


def compute_anchor_view(nmc, language: str, path: str, tree: dict) -> dict:
    """The writer helpers over every target of a mapped file, with their
    pre-U01 signatures: target resolution per qualname, anchor matches per
    target line."""
    lines, beacons = nmc._parse_disk_beacons_for_language(language, path)
    quals, idxs = _outline_targets(tree)
    targets = {
        q: list(nmc._resolve_anchor_target_line(language, path, lines, q, None, None))
        for q in quals
    }
    matches = {
        str(i): [
            [b.get("comment_line"), s, e]
            for b, s, e in nmc._find_anchor_matched_beacons(language, lines, beacons, i)
        ]
        for i in idxs
    }
    return {"targets": targets, "matches": matches}


def base_vue_sections(nmc, source_text: str) -> list[dict]:
    """The top-level SFC section list exactly as parse_vue_outline built it at
    BASE_REV (nmc 7504-7559), restated for the generator only."""
    source_bytes = source_text.encode("utf-8")
    parser = nmc.Parser()
    parser.language = nmc.get_language("vue")
    root = parser.parse(source_bytes).root_node
    blocks: list[dict] = []
    for ch in root.children:
        if ch.type not in {"template_element", "script_element", "style_element"}:
            continue
        start_tag = raw_text = end_tag = None
        for c2 in ch.children:
            if c2.type == "start_tag":
                start_tag = c2
            elif c2.type == "raw_text":
                raw_text = c2
            elif c2.type == "end_tag":
                end_tag = c2
        if start_tag is None:
            continue
        start_tag_text = source_bytes[start_tag.start_byte:start_tag.end_byte].decode("utf-8", errors="ignore")
        start_tag_compact = nmc._vue_compact_start_tag_text(start_tag_text)
        if raw_text is not None:
            content_row0 = int(raw_text.start_point[0])
            content_text = source_bytes[raw_text.start_byte:raw_text.end_byte].decode("utf-8", errors="ignore")
        else:
            if end_tag is None:
                continue
            content_row0 = int(start_tag.end_point[0])
            content_text = source_bytes[start_tag.end_byte:end_tag.start_byte].decode("utf-8", errors="ignore")
        blocks.append({
            "type": ch.type,
            "start_tag": start_tag_compact,
            "content_row0": content_row0,
            "raw_text": content_text,
        })
    return blocks


def base_vue_section_beacons(nmc, section: dict) -> list[dict]:
    """One section's rebased beacons exactly as parse_vue_outline's template /
    script / style branches computed them at BASE_REV."""
    local_lines = str(section.get("raw_text") or "").splitlines()
    row0 = int(section.get("content_row0") or 0)
    if section["type"] == "template_element":
        local = nmc.parse_markdown_beacon_blocks(local_lines)
    else:
        local = nmc.parse_js_beacon_blocks(local_lines)
    return nmc._vue_rebase_beacons(local, row0)


def warning_inputs(nmc) -> dict[str, tuple[list[dict], str]]:
    dup_text = fx.PROBLEM_FIXTURES["duplicate_id"][1]
    return {
        "duplicate_py": (nmc.parse_python_beacon_blocks(dup_text.splitlines()), "PY"),
        "synthetic": (
            [{"id": i} for i in ("b", "a", "b", "", None, "  a ", "c", "c", "c", "  ")],
            "SQL",
        ),
        "no_duplicates": ([{"id": "x"}, {"id": "y"}], "JS/TS"),
        "empty": ([], "MD"),
    }


def compute_warning_nodes(nmc) -> dict[str, list]:
    out: dict[str, list] = {}
    for name, (beacons, language) in warning_inputs(nmc).items():
        children = [{"name": "existing", "note": "", "children": []}]
        nmc._prepend_duplicate_beacon_id_warning_nodes(children, beacons, language=language)
        out[name] = children
    return out


def vue_fixture_keys() -> list[str]:
    return [k for k, (fname, _t) in all_sources().items() if fname.endswith(".vue")]


def compute_baseline(nmc, root: str) -> dict:
    sources = all_sources()
    paths = materialize(root, sources)
    out: dict[str, Any] = {
        "schema": 1,
        "base_rev": BASE_REV,
        "parsers": {},
        "disk_parse": {},
        "map_codebase": {},
        "anchor_view": {},
        "vue": {},
    }
    for key, (fname, text) in sources.items():
        out["parsers"][key] = compute_parsers(nmc, text)
        language = language_of(fname)
        path = paths[key]
        out["disk_parse"][key] = digest(compute_disk_parse(nmc, language, path))
        tree = nmc.map_codebase(path, repo_root=os.path.dirname(path))
        out["map_codebase"][key] = digest(tree, root)
        if key.startswith("fixture:") and language in ("python", "js_ts", "vue", "markdown"):
            out["anchor_view"][key] = compute_anchor_view(nmc, language, path, tree)
        if fname.endswith(".vue"):
            sections = base_vue_sections(nmc, text)
            out["vue"][key] = {
                "outline": normalize(nmc.parse_vue_outline(path), root),
                "sections": sections,
                "section_beacons": [base_vue_section_beacons(nmc, s) for s in sections],
            }
    out["warning_nodes"] = compute_warning_nodes(nmc)
    return out


def compute_vue_parse_error(nmc, path: str, root: str) -> Any:
    """parse_vue_outline with the Vue language loader raising."""
    real = nmc.get_language

    def _boom(name):
        raise RuntimeError("vue language unavailable")

    nmc.get_language = _boom
    try:
        return normalize(nmc.parse_vue_outline(path), root)
    finally:
        nmc.get_language = real


def _load_base_modules(rev: str):
    tmp = tempfile.mkdtemp(prefix="u01-base-")
    for rel in ("nexus_map_codebase.py", "beacon_mutation_contract.py"):
        data = subprocess.run(
            ["git", "show", f"{rev}:{rel}"], cwd=str(REPO_ROOT),
            capture_output=True, check=True,
        ).stdout
        with open(os.path.join(tmp, rel), "wb") as f:
            f.write(data)
    sys.path.insert(0, tmp)
    for name in ("nexus_map_codebase", "beacon_mutation_contract"):
        sys.modules.pop(name, None)
    nmc = importlib.import_module("nexus_map_codebase")
    assert os.path.dirname(os.path.abspath(nmc.__file__)) == tmp, nmc.__file__
    return nmc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--rev", default=BASE_REV)
    ap.add_argument("--worktree", action="store_true",
                    help="use the working tree's modules (the base code only before any edit)")
    ap.add_argument("--out", default=str(GOLDEN_PATH))
    args = ap.parse_args(argv)
    if args.worktree:
        import nexus_map_codebase as nmc
    else:
        nmc = _load_base_modules(args.rev)
    root = tempfile.mkdtemp(prefix="u01-baseline-")
    baseline = compute_baseline(nmc, root)
    vue_outline_path = os.path.join(root, "fixture_vue_outline", "outline.vue")
    baseline["vue_parse_error"] = compute_vue_parse_error(nmc, vue_outline_path, root)
    baseline["base_rev"] = BASE_REV if args.rev == BASE_REV else args.rev
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(baseline, f, ensure_ascii=False, sort_keys=True, indent=0)
        f.write("\n")
    print(f"wrote {args.out}: {len(baseline['parsers'])} sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
