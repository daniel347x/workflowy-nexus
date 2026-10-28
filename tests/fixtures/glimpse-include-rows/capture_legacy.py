#!/usr/bin/env python3
"""Capture the PRE-S1 (legacy) outputs this unit's tests are locked to.

WHY: §9.1 requires legacy byte comparisons against pre-change source bytes,
never merely equality between two post-change paths
(which could drift together).  The recording is produced from a ``git
archive`` of the S1 base commit, extracted OUTSIDE any checkout, and is
checked in beside this script as ``legacy_outputs.json``.

USAGE (the recording already in the repo was produced exactly this way):

    git archive 0c56c96 | tar -x -C tmp/pre
    GLIMPSE_CAPTURE_ROOT=tmp/pre \
      /home/daniel347x/.cache/wnmt-tests/<warm>/bin/python \
      tests/fixtures/glimpse-include-rows/capture_legacy.py \
      --out tests/fixtures/glimpse-include-rows/legacy_outputs.json

The script refuses to run unless the imported ``workflowy_mcp`` comes from
``GLIMPSE_CAPTURE_ROOT`` (so a recording can never be silently taken from the
worktree under edit).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_COMMIT = "0c56c969dc9e58f6695f6d132780e6bc9fdee007"


def _sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _norm(value, tmp_dir: str):
    """JSON-serializable copy with the throwaway temp path normalized."""
    if isinstance(value, str):
        return value.replace(str(tmp_dir), "<TMP>")
    if isinstance(value, list):
        return [_norm(v, tmp_dir) for v in value]
    if isinstance(value, dict):
        return {str(k): _norm(v, tmp_dir) for k, v in value.items()}
    return value


def _record(value, tmp_dir):
    return {"type": type(value).__name__, "value": _norm(value, tmp_dir)}


async def _modal_authority(server, scenario, spec, *, tmp_dir):
    """Run the REAL Apply/push modal handler for its authoritative rows."""
    import unittest.mock as mock

    class _WS:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    async def fake_nested(**_kwargs):
        return dict(scenario.SLICE_REPLY), None

    ws = _WS()
    with scenario.patched_environment(server):
        with mock.patch.object(server, "_read_slice_group_config_nested", fake_nested):
            await server._handle_render_slice_modal(
                {
                    "action": "render_slice_modal",
                    "request_id": "capture-authority",
                    "endpoint_id": "tab-1",
                    "render_spec": spec.to_render_spec_blob(),
                },
                ws,
            )
    assert ws.sent, "modal handler sent no reply"
    reply = ws.sent[-1]
    assert reply.get("success") is True, reply
    return {
        "rows": _norm(reply.get("rows") or [], tmp_dir),
        "markdown": _norm(reply.get("markdown") or "", tmp_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--root", default=os.environ.get("GLIMPSE_CAPTURE_ROOT"))
    args = parser.parse_args()
    if not args.root:
        parser.error("set GLIMPSE_CAPTURE_ROOT (or pass --root) to the extracted archive")

    root = os.path.abspath(args.root)
    sys.path.insert(0, HERE)
    sys.path.insert(0, root)
    sys.path.insert(0, os.path.join(root, "tests"))
    sys.path.insert(0, os.path.join(root, "src"))

    import workflowy_mcp  # noqa: E402
    import scenario  # noqa: E402
    from workflowy_mcp import server as srv  # noqa: E402
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402

    imported = os.path.abspath(workflowy_mcp.__file__)
    if not imported.startswith(root + os.sep):
        raise SystemExit(
            f"refusing: workflowy_mcp imported from {imported!r}, not the archive {root!r}"
        )
    server_file = os.path.abspath(srv.__file__)
    if not server_file.startswith(root + os.sep):
        raise SystemExit(f"refusing: server module {server_file!r} is outside {root!r}")

    results = {}
    tmp_dir = tempfile.mkdtemp(prefix="glimpse-include-rows-")
    for name in scenario.SCENARIOS:
        value = scenario.run_scenario(srv, name, tmp_dir=tmp_dir)
        results[name] = _record(value, tmp_dir)

    authority_specs = {
        "raw_root": RenderProjectionSpec.normalize_from_glimpse_args(
            root_uuid=scenario.R),
        "slice_group": RenderProjectionSpec.normalize_from_glimpse_args(
            group_id="grp-arch", match_scope="name"),
        "slice_override": RenderProjectionSpec.normalize_from_glimpse_args(
            group_id="grp-arch", root_uuid=scenario.A, match_scope="name"),
        "simple_dock": RenderProjectionSpec.normalize_from_glimpse_args(
            root_uuid=scenario.DOCK, descend_policy="stop_at_child_anchors"),
        "raw_root_created_asc": RenderProjectionSpec.normalize_from_glimpse_args(
            root_uuid=scenario.R, sort_by="created", sort_direction="asc"),
        "raw_root_modified_desc": RenderProjectionSpec.normalize_from_glimpse_args(
            root_uuid=scenario.R, sort_by="modified", sort_direction="desc"),
    }
    authority = {}
    for name, spec in authority_specs.items():
        authority[name] = asyncio.run(_modal_authority(srv, scenario, spec, tmp_dir=tmp_dir))

    # A focused re-render of one real row (same triple a tap sends), captured
    # from the legacy plain call: the focused document is part of the "tap
    # target" contract the include_rows envelope must not change.
    focus_row = authority["raw_root"]["rows"][2]  # Resolver under Architecture
    focus_spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=scenario.R,
        focus={
            "mode": "row",
            "uuid": focus_row["uuid"],
            "ancestor_path": focus_row["ancestor_path"],
            "occurrence": focus_row["occurrence"],
        },
    )
    focused = {
        "spec_blob": focus_spec.to_render_spec_blob(),
        "result": _record(
            asyncio.run(_run_focused(srv, scenario, focus_spec, tmp_dir)), tmp_dir
        ),
    }

    provenance = {
        "base_commit": BASE_COMMIT,
        "extracted_from": "git archive <base_commit> | tar -x -C tmp/pre",
        "captured_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "capture_command": (
            "GLIMPSE_CAPTURE_ROOT=tmp/pre <warm-venv>/bin/python "
            "tests/fixtures/glimpse-include-rows/capture_legacy.py "
            "--out tests/fixtures/glimpse-include-rows/legacy_outputs.json"
        ),
        "python": sys.version,
        "server_sha256": _sha256(server_file),
        "scenario_sha256": _sha256(os.path.join(HERE, "scenario.py")),
    }
    payload = {
        "provenance": provenance,
        "results": results,
        "modal_authority": authority,
        "focused": focused,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"wrote {args.out}")
    print(f"server sha256 {provenance['server_sha256']}")
    print(f"scenarios {len(results)} modal_authority {len(authority)}")
    return 0


async def _run_focused(server, scenario, spec, tmp_dir):
    """Focused render through the public dispatch path (render_spec blob)."""
    with scenario.patched_environment(server):
        from workflowy_mcp.mcp_api_registry import dispatch_operation

        return await dispatch_operation(
            server.GLIMPSE_REGISTRY,
            "run",
            {"render_spec": spec.to_render_spec_blob()},
            error_logger=lambda *_a: None,
            operation_boundary=server._mcp_operation_boundary,
            mutation_admission=server._mcp_mutation_admission,
            exception_mapper=server._mcp_exception_mapper,
            result_finalizer=server._mcp_operation_result_finalizer,
        )


if __name__ == "__main__":
    raise SystemExit(main())
