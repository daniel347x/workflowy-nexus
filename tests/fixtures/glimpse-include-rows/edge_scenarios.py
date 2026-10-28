"""Supplemental empty/estate/old-daemon captures, always from pinned bytes.

Reproduce from the S1 worktree after extracting ``git archive 0c56c96``:
  <warm-python> tests/fixtures/glimpse-include-rows/edge_scenarios.py \
      --root tmp/pre --out tests/fixtures/glimpse-include-rows/edge_outputs.json

The original 20-result oracle and its scenario module are not regenerated.
"""
from __future__ import annotations

import asyncio
import copy
from unittest import mock

import scenario

EMPTY_LENS = {
    "lightning": {
        "mode": "render_lens",
        "lens": {
            "kind": "merge",
            "operation": "difference",
            "inputs": [
                {"kind": "subtree", "root_handle": scenario.R},
                {"kind": "subtree", "root_handle": scenario.R},
            ],
        },
    },
}


def run(server, name, include_rows=None):
    family, verb, params = {
        "empty_window": ("GLIMPSE_REGISTRY", "run", {
            "group_handle": "Architecture", "recent": "curated"}),
        "empty_lens": ("GLIMPSE_REGISTRY", "run", {"render_spec": EMPTY_LENS}),
        "estate_node": ("GLIMPSE_REGISTRY", "estate", {"handle": {"node": scenario.A1}}),
        "estate_empty_lens": ("GLIMPSE_REGISTRY", "estate", {"handle": EMPTY_LENS}),
        "forbidden_run": ("GLIMPSE_REGISTRY", "run", {
            "root_handle": scenario.R, "include_rows": True}),
        "forbidden_dock": ("ANCHOR_REGISTRY", "dock.render", {
            "dock_handle": scenario.DOCK, "include_rows": True}),
    }[name]
    params = copy.deepcopy(params)
    if include_rows is not None:
        params["include_rows"] = include_rows
    with scenario.patched_environment(server), mock.patch.object(
        server, "_compute_curated_high_water_mark", return_value=1000,
    ):
        # Fixed upstream curation pivot, newer than every fixture node. The
        # real selection/pruning/empty-window/shaper pipeline remains intact.
        return asyncio.run(scenario._dispatch(server, family, verb, params))


def main():
    import argparse
    import hashlib
    import json
    from pathlib import Path
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    for path in (root, root / "tests", root / "src"):
        sys.path.insert(0, str(path))
    from workflowy_mcp import server

    path = Path(server.__file__).resolve()
    assert path == root / "src/workflowy_mcp/server.py", path
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == "a225702518284d179e886847ebd82129a8bffc4f7172ef455aae8fbb89c182c0", digest
    values = {}
    for name in ("empty_window", "empty_lens", "estate_node", "estate_empty_lens",
                 "forbidden_run", "forbidden_dock"):
        value = run(server, name)
        if isinstance(value, dict):
            # Generated describe documents necessarily gain the new option;
            # the original error envelope itself is retained exactly here.
            value.pop("operation_doc", None)
        values[name] = value
    Path(args.out).write_text(json.dumps({
        "base_commit": "0c56c969dc9e58f6695f6d132780e6bc9fdee007",
        "server_sha256": digest,
        "values": values,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
