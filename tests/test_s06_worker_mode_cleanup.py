"""S06 exact launcher vocabulary remains closed; CUT-2B deletes the worker/parser."""
from __future__ import annotations

import ast
from pathlib import Path

from test_cut_2b_fences import assert_worker_absent

REPO = Path(__file__).resolve().parents[1]
GOVERNANCE = REPO / "src" / "workflowy_mcp" / "carto_job_governance.py"
SERVER = REPO / "src" / "workflowy_mcp" / "server.py"
PIPELINE = REPO / "src" / "workflowy_mcp" / "markdown_generate_pipeline.py"
GOVERNED_MODES = {"carto_refresh", "markdown_generate"}


def test_launch_command_inventory_contains_only_governed_modes() -> None:
    """No production worker launch; retain literal, vocabulary and retired-argument checks."""
    assert_worker_absent(REPO / "src" / "workflowy_mcp" / "weave_worker.py")
    modes_seen: set[str] = set()
    for path in (GOVERNANCE, SERVER, PIPELINE):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg != "worker_mode":
                continue
            assert isinstance(node.value, ast.Constant), (
                f"{path.name}: worker_mode is not a literal (dynamic modes need audit)"
            )
            modes_seen.add(str(node.value.value))
    assert modes_seen <= GOVERNED_MODES, f"dead launch modes present: {modes_seen}"
    assert modes_seen == set(), f"a production worker_mode launcher reappeared: {modes_seen}"
    governance_source = GOVERNANCE.read_text(encoding="utf-8")
    assert '"--dry-run"' not in governance_source
    assert '"--nexus-tag"' not in governance_source
    assert '"--json-file"' not in governance_source


def test_server_launch_env_has_no_nexus_runs_base_producer() -> None:
    source = SERVER.read_text(encoding="utf-8")
    assert 'setdefault("NEXUS_RUNS_BASE"' not in source
    assert "get_nexus_runs_base_dir" not in source
