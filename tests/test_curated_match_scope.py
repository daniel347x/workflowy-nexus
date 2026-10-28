"""match_scope plumbing for the ``since/recent='curated'`` curation pivot.

Session-019 follow-up (brief [ap:2NORCM] rev 2, the [ap:SP9EY1] misread):
the server-side render callers (``_glimpse_slice_group_from_cache`` and
``_handle_render_slice_modal``) resolve ``match_scope`` and pass it into
``build_stage1_base_tree`` for Stage-1 slice membership -- but called
``server._compute_curated_high_water_mark`` WITHOUT it, and
``f8_slice_policy.node_is_curated_for_prefixes`` unconditionally read tags
from NAME *and* NOTE (``extract_node_tags``). Consequence: a literal
``#curated`` sitting in a note body -- e.g. the [ap:SP9EY1] explanation AP,
created after the genuine Session-018 checkpoint, whose prose quotes the
tag -- advanced the curation high-water mark even under
``match_scope='name'``, stranding every uncurated AP captured before it
(the Manager Sessions tail rendered nearly empty over a dozen uncurated
captures).

The repair is exactly existing scope plumbing -- no new option, no parallel
matcher::

    resolved match_scope at render caller
      -> _compute_curated_high_water_mark(match_scope=...)
      -> node_is_curated_for_prefixes(match_scope=...)
      -> same extraction choice as is_slice_member:
           name          -> extract_name_tags(name)
           name_and_note -> extract_node_tags(name, note)   (default)

These tests pin the six behaviours the brief requires -- at the predicate
level and through the production server wrapper (the render-caller path,
same binding style as tests/test_curated_cursor_postfix.py). The LIVE-tool
re-check of the real Manager Sessions call awaits Dan's daemon restart:
the running daemon serves pre-fix code until then, so test 6 below is the
in-process code-level equivalent the brief prescribes.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server  # noqa: E402
from workflowy_mcp.f8_slice_policy import (  # noqa: E402
    is_slice_member,
    node_is_curated_for_prefixes,
)

# The Manager Sessions slice group's family: the checkpoint node carries
# plain ``#curated``; [ap:SP9EY1]-shaped explanation APs quote ``#curated``
# in prose inside their NOTE bodies.
ACTIVE_PREFIXES = ["curated"]

NOTE_WITH_LITERAL_TAG = (
    "The #curated cursor semantics: a --deprecated tag still counts as a "
    "curation act, while --suppress/--expand are presentation only. This "
    "prose quotes the literal tag -- under match_scope='name' it must NOT "
    "curate this node."
)


def _manager_sessions_cache() -> dict[str, dict]:
    """Cache shaped like the real Manager Sessions misread (2026-09-20).

    ::

        Manager-Session-019 (Dock root, uncurated)              createdAt=900
        |- Session-018 curation checkpoint  #curated            createdAt=1000
        |- [ap:AAAAAA] uncurated AP                             createdAt=2000
        |- [ap:BBBBBB] uncurated AP                             createdAt=3000
        |- [ap:SP9EY1-shaped] explanation AP -- plain name,     createdAt=4000
           literal '#curated' ONLY in its note body

    Pre-fix (and post-fix under the DEFAULT ``name_and_note`` scope, which
    is unchanged behaviour) the doc node's note mention made it the newest
    "curated" node: mark 4000, stranding the APs at 2000/3000. Under
    ``match_scope='name'`` the mark must stay on the genuine checkpoint
    (1000) so the whole uncurated tail renders.
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
            "name": "Session-018 curation checkpoint #curated",
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
        "doc": {
            "id": "doc",
            "parent_id": "root",
            "name": "[ap:CCCCCC] explanation of deprecated cursor behavior",
            "note": NOTE_WITH_LITERAL_TAG,
            "createdAt": 4000,
        },
    }


def _mark(cache: dict[str, dict], **kwargs) -> int | None:
    """The production pivot path: server wrapper -> pure walk, created/asc
    (the since-object default the render callers pass)."""
    return server._compute_curated_high_water_mark(
        cache_by_id=cache,
        root_uuid="root",
        active_prefixes=ACTIVE_PREFIXES,
        axis="created",
        axis_direction="asc",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Brief test 1: match_scope='name' -- a #curated mention only in a note does
# NOT establish / advance the pivot.
# ---------------------------------------------------------------------------
def test_note_mention_does_not_move_pivot_under_name_scope():
    # Predicate level: the note-body tag is invisible to a title-only read.
    assert not node_is_curated_for_prefixes(
        "[ap:CCCCCC] explanation of deprecated cursor behavior",
        ACTIVE_PREFIXES,
        note=NOTE_WITH_LITERAL_TAG,
        match_scope="name",
    )
    # Pivot level (production wrapper): the mark stays on the genuine
    # checkpoint (1000); the SP9EY1-shaped doc node (4000) does not move it,
    # so the uncurated APs at 2000/3000 remain in the tail.
    assert _mark(_manager_sessions_cache(), match_scope="name") == 1000


# ---------------------------------------------------------------------------
# Brief test 2: match_scope='name_and_note' -- the same note mention DOES
# establish the pivot (Workflowy-search parity, the shipped default).
# ---------------------------------------------------------------------------
def test_note_mention_moves_pivot_under_name_and_note_scope():
    assert node_is_curated_for_prefixes(
        "[ap:CCCCCC] explanation of deprecated cursor behavior",
        ACTIVE_PREFIXES,
        note=NOTE_WITH_LITERAL_TAG,
        match_scope="name_and_note",
    )
    assert _mark(_manager_sessions_cache(), match_scope="name_and_note") == 4000


# ---------------------------------------------------------------------------
# Brief test 3: plain NAME #curated still establishes the pivot under either
# relevant scope.
# ---------------------------------------------------------------------------
def test_plain_name_curated_establishes_pivot_under_both_scopes():
    for scope in ("name", "name_and_note"):
        # Predicate: a title tag curates under both scopes.
        assert node_is_curated_for_prefixes(
            "Session-018 curation checkpoint #curated",
            ACTIVE_PREFIXES,
            match_scope=scope,
        ), scope
        # Pivot: with no note mentions in play, the genuine plain checkpoint
        # IS the mark under both scopes...
        base = _manager_sessions_cache()
        del base["doc"]
        assert _mark(base, match_scope=scope) == 1000, scope
        # ...and a LATER plain checkpoint still moves it under both scopes
        # (even with the note-mention doc node present: max() picks 5000).
        cache = _manager_sessions_cache()
        cache["chk2"] = {
            "id": "chk2",
            "parent_id": "root",
            "name": "Session-019 curation checkpoint #curated",
            "note": "",
            "createdAt": 5000,
        }
        assert _mark(cache, match_scope=scope) == 5000, scope


# ---------------------------------------------------------------------------
# Brief test 4: #curated--deprecated retains the shipped marker behavior
# (a deprecated tag is itself a curation act and still counts), now read
# under the same scope choice as everything else.
# ---------------------------------------------------------------------------
def test_deprecated_retains_shipped_marker_behavior_under_both_scopes():
    # NAME-level deprecated: a marker under BOTH scopes (shipped behavior).
    for scope in ("name", "name_and_note"):
        assert node_is_curated_for_prefixes(
            "triaged ordinary node #curated--deprecated",
            ACTIVE_PREFIXES,
            match_scope=scope,
        ), scope
        # Middle-segment deprecated variant: still a marker (shipped).
        assert node_is_curated_for_prefixes(
            "triaged node #curated-teams--deprecated",
            ACTIVE_PREFIXES,
            match_scope=scope,
        ), scope
    cache = _manager_sessions_cache()
    cache["dep"] = {
        "id": "dep",
        "parent_id": "root",
        "name": "triaged ordinary node #curated--deprecated",
        "note": "",
        "createdAt": 4500,
    }
    for scope in ("name", "name_and_note"):
        assert _mark(cache, match_scope=scope) == 4500, scope
    # NOTE-level deprecated: counted under the default-scope family
    # (name_and_note reads notes -- shipped), invisible under 'name'
    # (title-only read -- the same extraction choice is_slice_member makes).
    assert node_is_curated_for_prefixes(
        "ordinary title",
        ACTIVE_PREFIXES,
        note="prose #curated--deprecated",
        match_scope="name_and_note",
    )
    assert not node_is_curated_for_prefixes(
        "ordinary title",
        ACTIVE_PREFIXES,
        note="prose #curated--deprecated",
        match_scope="name",
    )


# ---------------------------------------------------------------------------
# Brief test 5: #curated--suppress and #curated--expand remain non-markers
# (-001's shipped fix), under BOTH scopes and in BOTH fields.
# ---------------------------------------------------------------------------
def test_presentation_variants_remain_non_markers_under_both_scopes():
    for scope in ("name", "name_and_note"):
        for name in (
            "nested program Dock #curated--suppress",
            "auto-expanded region #curated--expand",
            "middle-segment Dock #curated-teams--suppress",
            "middle-segment region #curated-teams--expand",
        ):
            assert not node_is_curated_for_prefixes(
                name, ACTIVE_PREFIXES, match_scope=scope
            ), (scope, name)
        # Presentation exclusion is orthogonal to scope: a note-level
        # presentation variant is not a marker under name_and_note either.
        assert not node_is_curated_for_prefixes(
            "ordinary title",
            ACTIVE_PREFIXES,
            note="prose #curated--suppress",
            match_scope=scope,
        ), scope
    # Pivot: later presentation-tagged nodes never move the mark, whatever
    # the scope; the plain checkpoint stays the pivot.
    cache = _manager_sessions_cache()
    cache["dock"] = {
        "id": "dock",
        "parent_id": "root",
        "name": "nested program Dock #curated--suppress",
        "note": "",
        "createdAt": 4700,
    }
    cache["reg"] = {
        "id": "reg",
        "parent_id": "root",
        "name": "auto-expanded region #curated--expand",
        "note": "",
        "createdAt": 4800,
    }
    assert _mark(cache, match_scope="name") == 1000
    # Under name_and_note the doc node's note mention (4000) still outranks
    # the presentation nodes -- which contribute nothing at 4700/4800.
    assert _mark(cache, match_scope="name_and_note") == 4000


# ---------------------------------------------------------------------------
# Brief test 6: the real Manager Sessions call, code-level equivalent
# in-process. The daemon serves pre-fix code until Dan restarts it, so the
# LIVE re-check (glimpse group_handle='8d51c010', recent='curated',
# match_scope='name', descend_policy='all' returning the uncurated tail)
# awaits that restart; this reproduces the exact render-caller -> wrapper ->
# predicate path against the [ap:SP9EY1]-shaped fixture instead.
# ---------------------------------------------------------------------------
def test_manager_sessions_render_caller_shape_in_process():
    cache = _manager_sessions_cache()
    # The Manager's call passes match_scope='name': the mark stays on the
    # Session-018 checkpoint, so the Session-019 APs at 2000/3000 AND the
    # doc node at 4000 are all in the uncurated tail -- non-empty.
    assert (
        server._compute_curated_high_water_mark(
            cache_by_id=cache,
            root_uuid="root",
            active_prefixes=ACTIVE_PREFIXES,
            match_scope="name",
            axis="created",
            axis_direction="asc",
        )
        == 1000
    )
    # The name_and_note variant moves the mark to the note mention (4000):
    # that scope deliberately keeps Workflowy-search parity.
    assert (
        server._compute_curated_high_water_mark(
            cache_by_id=cache,
            root_uuid="root",
            active_prefixes=ACTIVE_PREFIXES,
            match_scope="name_and_note",
            axis="created",
            axis_direction="asc",
        )
        == 4000
    )


# ---------------------------------------------------------------------------
# Regression lock: every OTHER slice group's recent:'curated' behaviour is
# exactly as before -- callers that never pass match_scope (and unrecognized
# values) get the shipped name_and_note harvest.
# ---------------------------------------------------------------------------
def test_default_scope_behavior_is_unchanged():
    cache = _manager_sessions_cache()
    # Wrapper without the kwarg == pre-fix behaviour (note mention counts).
    assert _mark(cache) == 4000
    # Predicate without the kwarg == pre-fix behaviour.
    assert node_is_curated_for_prefixes(
        "ordinary title", ACTIVE_PREFIXES, note="prose #curated"
    )
    # Unrecognized values fall safe to the broader name+note harvest -- the
    # same documented fall-safe is_slice_member uses.
    assert node_is_curated_for_prefixes(
        "ordinary title",
        ACTIVE_PREFIXES,
        note="prose #curated",
        match_scope="bogus",
    )
    assert _mark(cache, match_scope="bogus") == 4000


# ---------------------------------------------------------------------------
# Shared-path lock: on the SCOPE axis the curation predicate now makes
# EXACTLY the same extraction choice as is_slice_member (the contract's
# confirm-first duty, asserted rather than narrated). The two predicates
# still differ deliberately on the DEPRECATED axis (that is -001 territory,
# locked in test_curated_cursor_postfix.py / test_f8_slice_policy_middle_
# segments.py) -- here only plain, postfix-free tags are compared.
# ---------------------------------------------------------------------------
def test_scope_axis_agrees_with_is_slice_member():
    cases = [
        # (name, note)
        ("plain checkpoint #curated", ""),
        ("plain checkpoint #curated", "note also #curated"),
        ("ordinary title", "prose #curated"),
        ("ordinary title", ""),
        ("boundary check #curatedx", "prose #curatedx"),
    ]
    for scope in ("name", "name_and_note"):
        for name, note in cases:
            assert node_is_curated_for_prefixes(
                name, ACTIVE_PREFIXES, note=note, match_scope=scope
            ) == is_slice_member(
                name, ACTIVE_PREFIXES, note=note, match_scope=scope
            ), (scope, name, note)
