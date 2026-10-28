"""Live-shaped regression lock for the Session-019 curated-cursor bug.

``glimpse(group_handle=..., recent='curated')`` pivots on a high-water mark
computed by ``glimpse_since_policy.compute_curated_high_water_mark``: the
branch-date of the newest node the curation predicate calls curated. The
Manager Sessions group (prefix ``curated``) checkpoints with a small node
tagged plain ``#curated``; Session-019 additionally tagged nested program
Dock roots ``#curated--suppress`` at creation (a lens carve-out keeping
conductor produce out of the tail).

The bug: ``f8_slice_policy.node_is_curated_for_prefixes`` counted EVERY
postfixed variant of the family as curated, so each new suppressed Dock root
became the newest "curated" node and the mark jumped to its creation time --
stranding every Anchor Point captured before it. The tail rendered empty
over a dozen uncurated APs.

The fix (brief [ap:BRL6TV]): plain ``#curated`` checkpoints and
``--deprecated`` triage variants still move the cursor; ``--suppress`` /
``--expand`` presentation variants never do.

These tests rebuild that exact situation against a cache fixture -- plain
checkpoint, later APs, later-still suppressed Dock root -- and pin the mark
to the checkpoint, at BOTH the pure-walk level (``compute_curated_high_water_
mark`` with the production predicate injected) and the production-wrapper
level (``server._compute_curated_high_water_mark``).
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server  # noqa: E402
from workflowy_mcp.f8_slice_policy import node_is_curated_for_prefixes  # noqa: E402
from workflowy_mcp.glimpse_since_policy import (  # noqa: E402
    compute_curated_high_water_mark,
)

# The Manager Sessions slice group's family: checkpoint nodes carry plain
# ``#curated``; Session-019's nested program Dock roots carry
# ``#curated--suppress``.
ACTIVE_PREFIXES = ["curated"]


def _production_predicate(name: str, note: str) -> bool:
    """The exact binding ``server._compute_curated_high_water_mark`` injects."""
    return node_is_curated_for_prefixes(name, ACTIVE_PREFIXES, note=note)


def _session019_cache() -> dict[str, dict]:
    """Cache shaped like Manager Session-019 the night the bug fired.

    ::

        Manager-Session-019 (Dock root, uncurated)          createdAt=900
        |- curation checkpoint  #curated                    createdAt=1000
        |- [ap:AAAAAA] uncurated AP                         createdAt=2000
        |- [ap:BBBBBB] uncurated AP                         createdAt=3000
        |- nested program Dock  #curated--suppress          createdAt=4000

    Pre-fix, the suppressed Dock root (4000) was the newest "curated" node
    and the mark jumped to 4000, stranding the APs at 2000/3000. Post-fix
    the mark stays on the checkpoint (1000).
    """
    return {
        "root": {
            "id": "root",
            "name": "Manager-Session-019",
            "note": "",
            "createdAt": 900,
        },
        "chk": {
            "id": "chk",
            "parent_id": "root",
            "name": "curation checkpoint #curated",
            "note": "",
            "createdAt": 1000,
        },
        "ap1": {
            "id": "ap1",
            "parent_id": "root",
            "name": "[ap:AAAAAA] first uncurated AP",
            "note": "",
            "createdAt": 2000,
        },
        "ap2": {
            "id": "ap2",
            "parent_id": "root",
            "name": "[ap:BBBBBB] second uncurated AP",
            "note": "",
            "createdAt": 3000,
        },
        "dock": {
            "id": "dock",
            "parent_id": "root",
            "name": "nested program Dock #curated--suppress",
            "note": "",
            "createdAt": 4000,
        },
    }


def _mark(cache: dict[str, dict]) -> int | None:
    return compute_curated_high_water_mark(
        cache_by_id=cache,
        root_uuid="root",
        curated_predicate=_production_predicate,
        axis="created",
        axis_direction="asc",
    )


def test_suppressed_dock_root_does_not_move_the_cursor():
    # THE bug, live-shaped: the mark must stay on the plain #curated
    # checkpoint (1000), NOT jump to the later #curated--suppress Dock root
    # (4000) -- so the uncurated APs at 2000/3000 remain in the tail.
    assert _mark(_session019_cache()) == 1000


def test_plain_checkpoint_still_moves_the_cursor():
    # Requirement 1, unchanged: a later plain #curated checkpoint IS the mark.
    cache = _session019_cache()
    cache["chk2"] = {
        "id": "chk2",
        "parent_id": "root",
        "name": "next curation checkpoint #curated",
        "note": "",
        "createdAt": 5000,
    }
    assert _mark(cache) == 5000


def test_deprecated_ordinary_node_still_moves_the_cursor():
    # Requirement 2, unchanged: --deprecated on an ordinary node is deliberate
    # triage and still counts as curated.
    cache = _session019_cache()
    cache["dep"] = {
        "id": "dep",
        "parent_id": "root",
        "name": "triaged ordinary node #curated--deprecated",
        "note": "",
        "createdAt": 4500,
    }
    assert _mark(cache) == 4500


def test_only_presentation_variants_means_nothing_curated_yet():
    # A subtree whose only family tags are presentation variants has NO
    # high-water mark -> the whole subtree is the uncurated tail.
    cache = {
        "root": {"id": "root", "name": "Dock", "note": "", "createdAt": 100},
        "d1": {
            "id": "d1",
            "parent_id": "root",
            "name": "program Dock A #curated--suppress",
            "note": "",
            "createdAt": 200,
        },
        "d2": {
            "id": "d2",
            "parent_id": "root",
            "name": "program Dock B #curated--expand",
            "note": "",
            "createdAt": 300,
        },
    }
    assert _mark(cache) is None


def test_server_wrapper_agrees_with_the_pure_walk():
    # The production wrapper binds node_is_curated_for_prefixes to the
    # group's active prefixes and delegates to the pure walk -- pin the same
    # answer through the wrapper so the production binding itself is covered.
    assert (
        server._compute_curated_high_water_mark(
            cache_by_id=_session019_cache(),
            root_uuid="root",
            active_prefixes=ACTIVE_PREFIXES,
            axis="created",
            axis_direction="asc",
        )
        == 1000
    )
