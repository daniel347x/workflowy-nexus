"""CF-38 browser-side cancel feedback -- executed-harness wrapper.

Spec ``[ap:1770RA]`` §6.5 (318-322) and §9.5 (550-552): the extension's
``carto_cancel_job_result`` branch must call the existing shared
``handleCartoLaunchResult`` after its unchanged error log, and that handler
must render the five prescribed frames -- modern pending, legacy pending,
cooperative, already_terminal and failed -- on the existing dedicated feedback
line, survive the ~1Hz poll repaint, use the one existing 8000 ms fade and
create no job row.

The oracle is ``tests/js/test_carto_cancel_pending.js``: it reads the REAL
client bytes, extracts the real handler and WebSocket branch, and executes
them against a fake DOM (whose ``textContent`` destroys children exactly like
the poll's repaint) and fake timers. This wrapper makes that harness part of
the pytest tier, invoking Node exactly as ``scripts/run_tests.py``'s
``run_js_harness`` does for every repository JS harness -- ``node <harness>``
from the repository root, exit 0 = all pass -- with the five live
credential/dial variables scrubbed from the child environment.

Probe: running this test with ``CUT2B_NEUTRALIZE_PENDING_FEEDBACK`` set makes
the harness route cancel replies through the generic launch branch. The
pending-text/no-stop assertions then fail (Node exit 1) -- a behavioral
failure, never a source-extraction error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HARNESS = "tests/js/test_carto_cancel_pending.js"

# The WSL shell exports Dan's live credentials and dial host. The Node child
# needs none of them; scrubbing these five keeps the harness credential-free
# while still passing the test-only neutralization probe through.
SCRUBBED_VARIABLES = (
    "WORKFLOWY_API_KEY",
    "WORKFLOWY_NEXUS_CONFIG",
    "WORKFLOWY_BROKER_TOKEN_FILE",
    "WORKFLOWY_MCP_BEARER",
    "WORKFLOWY_MCP_HTTP_DIAL_HOST",
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_real_cancel_feedback_handles_pending_and_legacy_frames() -> None:
    node = shutil.which("node")
    assert node is not None
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in SCRUBBED_VARIABLES
    }
    proc = subprocess.run(
        [node, HARNESS],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        f"{HARNESS} failed (node exit {proc.returncode})\n{output}"
    )
    assert "ALL PASS" in output
