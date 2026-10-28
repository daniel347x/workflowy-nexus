"""Serial sensitivity probe: W1_PLANT_DEEP_FREEZE_LEAK=1 must make test 02 RED.

Run this file alone with --serial so both tests share a worker, in order.
Without the environment gate these are ordinary GREEN fence witnesses.
"""

import os

from workflowy_mcp.coherence.cache_view import (
    is_test_deep_freeze_active,
    set_test_deep_freeze,
)


def test_01_planted_non_restoring_toggle():
    assert is_test_deep_freeze_active()
    if os.environ.get("W1_PLANT_DEEP_FREEZE_LEAK") == "1":
        # Deliberately broken only for the committed fence-sensitivity probe.
        set_test_deep_freeze(False)


def test_02_fence_detects_preceding_test_leak():
    assert is_test_deep_freeze_active()
