"""Regression test: the CARTO launch refusal/ack client handler exists and is wired.

This test does NOT execute JS; it asserts the four single-shot launch result
actions are dispatched to the shared handler in the extension source.
"""

from pathlib import Path


CLIENT_JS = Path(__file__).resolve().parents[1] / "workflowy-glimpse-cache" / "glimpse-cache-client.js"


def test_carto_launch_result_branches_wired():
    src = CLIENT_JS.read_text(encoding="utf-8")
    for action in (
        "refresh_file_node_result",
        "refresh_folder_node_result",
        "generate_markdown_file_result",
        "materialize_markdown_file_result",
    ):
        assert action in src, f"missing dispatch for {action}"
    assert "handleCartoLaunchResult(request)" in src
    assert "function handleCartoLaunchResult(message)" in src


def test_carto_launch_feedback_parses_refusal_kinds():
    src = CLIENT_JS.read_text(encoding="utf-8")
    # The shared handler must recognize the governance refusal vocabulary.
    for needle in ("cooldown_active", "overlap_conflict", "governance_state_unclassifiable", "deduplicated"):
        assert needle in src, f"handler missing {needle}"


def test_carto_launch_feedback_survives_poll_repaint():
    """The feedback line must be a dedicated child div the poll re-inserts,
    not a textContent write that the ~1Hz carto_list_jobs repaint stomps."""
    src = CLIENT_JS.read_text(encoding="utf-8")
    assert "glimpse-carto-launch-feedback" in src, "missing dedicated feedback line id"
    assert "_ensureCartoLaunchFeedbackLine" in src, "missing re-insert helper"
    # The poll repaint must re-insert the feedback line (else it gets destroyed).
    assert src.count("_ensureCartoLaunchFeedbackLine()") >= 2, (
        "poll repaint paths must call _ensureCartoLaunchFeedbackLine to survive repaint"
    )
    # The handler must NOT write raw text into cartoJobsEl.textContent (the bug).
    # Find the handler body and confirm it does not do a bare textContent write.
    hstart = src.index("function handleCartoLaunchResult(message)")
    body = src[hstart:hstart + 3000]
    assert "cartoJobsEl.textContent = text;" not in body, (
        "handler must not write textContent directly (poll stomps it)"
    )
