"""Freeze the independent G5-base definitions for the G5 differential.

Only git's fc799756 source is read; neither current writer nor product code is
imported. The test runner's ext4 mirror intentionally has no .git directory, so
this builder is a development-time generator, not a runtime dependency.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import textwrap

BASE = "fc7997561b7b32973657160463069663324072bd"
ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path(__file__).with_name("g5_baseline.json")

MODULES = {
    "workflowy_mcp.server": (
        "src/workflowy_mcp/server.py",
        (
            "_run_carto_bulk_visible_apply_job",
            "_start_carto_bulk_visible_apply_job",
            "_bulk_apply_per_file_inner_loop",
            "_write_bulk_job_artifacts",
            "_write_carto_job_payload",
            "_carto_task_terminal_payload",
            "_carto_terminal_payload_data",
            "_start_carto_markdown_generate_job",
        ),
    ),
    "workflowy_mcp.carto_job_governance": (
        "src/workflowy_mcp/carto_job_governance.py",
        ("admit_carto_job_locked",),
    ),
    "workflowy_mcp.markdown_generate_pipeline": (
        "src/workflowy_mcp/markdown_generate_pipeline.py",
        (
            "run_markdown_generate_pipeline",
            "_run_phase8_refresh_inline",
            "_write_job_payload",
            "_markdown_job_artifact",
            "_persist_markdown_job",
        ),
    ),
}


def _git_show(repo, path):
    return subprocess.check_output(
        ["git", "show", f"{BASE}:{path}"],
        cwd=repo,
        text=True,
        encoding="utf-8",
    )


def _definition_text(lines, tree, module, name):
    """Exact def..end text of one top-level function, decorators excluded."""
    for node in tree.body:
        if getattr(node, "name", None) == name and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            text = "".join(lines[node.lineno - 1:node.end_lineno])
            return textwrap.dedent(text)
    raise LookupError(f"{module}: no top-level definition named {name!r} in {BASE}")


def build(repo=ROOT):
    result = {"commit": BASE, "modules": {}}
    for module, (path, names) in MODULES.items():
        source = _git_show(repo, path)
        tree = ast.parse(source)
        lines = source.splitlines(keepends=True)
        definitions = {}
        for name in names:
            text = _definition_text(lines, tree, module, name)
            definitions[name] = {
                "source": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        result["modules"][module] = {
            "path": path,
            "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "definitions": definitions,
        }
    return json.dumps(result, indent=2, ensure_ascii=True) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--repo", type=Path, default=ROOT)
    args = parser.parse_args()
    content = build(args.repo.resolve())
    if args.check:
        assert OUTPUT.read_text(encoding="utf-8") == content, (
            f"G5 baseline fixture drifted from {BASE}"
        )
        print(f"G5 baseline matches {BASE}")
    else:
        OUTPUT.write_text(content, encoding="utf-8")
        print(OUTPUT)
