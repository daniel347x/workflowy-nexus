"""Regression locks for the render_spec modal-view AGENT OVERRIDES (Dan +

audit session). The motivating capability: view a clicked modal row, then
re-call the SAME modal view in full/UUID + headings_only mode to harvest the
occurrence-exact UUIDs of exactly those nodes (to tag/edit). Plus the two
LOUD guards (sort / since override rejected when a row focus is present), the
standard `since` preconditions on the merged spec, and the JS<->Python
`kind`-preservation contract.

These exercise the pure, no-I/O seams:
  * RenderProjectionSpec.with_view_overrides  (normalized merge, kind-safe)
  * RenderProjectionSpec.from_render_spec_blob (kind_hint preservation)
  * _apply_modal_view_overrides_and_guard      (the ONE override+guard seam)

(The former WorkFlowyClientNexus._prune_forest_to_focus_index_path full-mode
focus locks were RETIRED in Phase 0.5 Step 12a when that dead-code helper was
deleted; the behavior now lives on the materializer core -- see the retirement
note at the bottom of this file.)
"""

from __future__ import annotations

from workflowy_mcp.cache_render_spec import RenderProjectionSpec
from workflowy_mcp.glimpse_since_policy import since_from_edge


# ---------------------------------------------------------------------------
# with_view_overrides — tri-state, normalized, kind-preserving
# ---------------------------------------------------------------------------

def test_view_overrides_none_inherits_everything():
    """None on every knob leaves the spec byte-for-byte unchanged."""
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        group_id="g1", group_name="G1", root_uuid="root-1",
        headings_only=False, as_markdown=True, sort_by="created",
    )
    out = spec.with_view_overrides()
    assert out == spec  # frozen dataclass equality


def test_view_overrides_full_uuid_plus_headings_the_motivating_case():
    """as_markdown=False + headings_only=True override a markdown modal spec."""
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        group_id="g1", group_name="G1", root_uuid="root-1",
        as_markdown=True, headings_only=False,
    )
    out = spec.with_view_overrides(as_markdown=False, headings_only=True)
    assert out.as_markdown is False
    assert out.headings_only is True
    # scope identity untouched
    assert out.kind == spec.kind == "slice_override"
    assert out.group_id == "g1"
    assert out.root_uuid == "root-1"


def test_view_overrides_headings_false_forces_bodies_back_on():
    """Explicit False overrides a modal that was in headings-only mode."""
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="root-1", headings_only=True,
    )
    out = spec.with_view_overrides(headings_only=False)
    assert out.headings_only is False


def test_view_overrides_coerce_bad_values():
    """A bad sort_direction clamps to asc; a bad sort_by collapses to 'none'."""
    spec = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="root-1")
    out = spec.with_view_overrides(sort_by="sideways", sort_direction="DOWN")
    assert out.sort_by == "none"
    assert out.sort_direction == "asc"


def test_view_overrides_preserve_resolved_slice_kind():
    """A RESOLVED plain slice (group_id + resolved root_uuid, kind='slice')

    must NOT be re-derived to slice_override by the re-normalization.
    """
    # A plain slice whose root_uuid got resolved/filled for echo, but kind
    # stayed 'slice' (the resolved-spec case the with_overrides path creates).
    spec = RenderProjectionSpec(
        kind="slice", group_id="g1", group_name="G1", root_uuid="resolved-root",
    )
    assert spec.kind == "slice"
    out = spec.with_view_overrides(headings_only=True)
    # Re-normalization preserved the resolved kind via kind_hint.
    assert out.kind == "slice"
    assert out.headings_only is True


# ---------------------------------------------------------------------------
# from_render_spec_blob — kind_hint preservation (JS<->Python contract)
# ---------------------------------------------------------------------------

def test_blob_preserves_valid_slice_kind_over_presence_derivation():
    """A blob carrying kind='slice' with BOTH group_id and root_uuid stays

    'slice' (the JS<->Python asymmetry fix), NOT re-derived to slice_override.
    """
    blob = {
        "kind": "slice",
        "group_id": "g1",
        "group_name": "G1",
        "root_uuid": "resolved-root",  # present for echo/reuse
    }
    spec = RenderProjectionSpec.from_render_spec_blob(blob)
    assert spec.kind == "slice"


def test_blob_missing_kind_falls_back_to_presence_table():
    """No kind in the blob -> derive from the (group+root) truth table."""
    blob = {"group_id": "g1", "root_uuid": "root-1"}
    spec = RenderProjectionSpec.from_render_spec_blob(blob)
    assert spec.kind == "slice_override"


def test_blob_invalid_kind_falls_back_to_presence_table():
    """A garbage kind is ignored; presence derivation wins."""
    blob = {"kind": "nonsense", "root_uuid": "root-1"}
    spec = RenderProjectionSpec.from_render_spec_blob(blob)
    assert spec.kind == "raw_root"


# ---------------------------------------------------------------------------
# _apply_modal_view_overrides_and_guard — the ONE override+guard seam
# ---------------------------------------------------------------------------

def _import_guard():
    from workflowy_mcp.server import _apply_modal_view_overrides_and_guard
    return _apply_modal_view_overrides_and_guard


def _focused(spec: RenderProjectionSpec) -> RenderProjectionSpec:
    return spec.with_overrides(
        focus={"mode": "row", "uuid": "n-2", "ancestor_path": ["root-1"], "occurrence": 0}
    )


def test_guard_allows_full_headings_override_on_focused_row():
    """The motivating call: focused row + as_markdown=False + headings_only=True

    is ALLOWED (harvest the focused subtree's UUIDs).
    """
    guard = _import_guard()
    spec = _focused(RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="root-1"))
    out, err = guard(
        spec, as_markdown=False, headings_only=True,
        sort_by=None, sort_direction=None, since=None,
    )
    assert err is None
    assert out.as_markdown is False
    assert out.headings_only is True
    assert out.focus and out.focus["uuid"] == "n-2"


def test_guard_rejects_sort_override_with_focus():
    guard = _import_guard()
    spec = _focused(RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="root-1"))
    out, err = guard(
        spec, as_markdown=None, headings_only=None,
        sort_by="created", sort_direction=None, since=None,
    )
    assert err is not None and err["success"] is False
    assert "FOCUSED" in err["error"]


def test_guard_rejects_since_override_with_focus():
    guard = _import_guard()
    spec = _focused(RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="root-1"))
    out, err = guard(
        spec, as_markdown=None, headings_only=None,
        sort_by=None, sort_direction=None, since=since_from_edge("last 10"),
    )
    assert err is not None and err["success"] is False
    assert "`since`" in err["error"]


def test_guard_allows_sort_override_when_no_focus():
    guard = _import_guard()
    spec = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="root-1")
    out, err = guard(
        spec, as_markdown=None, headings_only=None,
        sort_by="created", sort_direction="desc", since=None,
    )
    assert err is None
    assert out.sort_by == "created"
    assert out.sort_direction == "desc"


def test_guard_window_since_no_longer_requires_sort_decouple():
    """DECOUPLE (SEAM B removed): a window `since` override with NO presentation
    sort now SUCCEEDS -- the SINCE object carries its own axis (default
    created/asc), so it no longer needs an active created/modified presentation
    sort. (This test previously asserted the OPPOSITE; the since<->sort decouple
    intentionally reverses it -- the override-path half of the payoff.)"""
    guard = _import_guard()
    spec = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="root-1")
    out, err = guard(
        spec, as_markdown=None, headings_only=None,
        sort_by=None, sort_direction=None, since=since_from_edge("1w"),
    )
    assert err is None
    assert out.since is not None and out.since.form == "window"
    # sort stays document order (None) -- the window filter is independent of it.
    assert out.sort_by_internal() is None


def test_guard_window_since_ok_with_overridden_sort():
    guard = _import_guard()
    spec = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="root-1")
    out, err = guard(
        spec, as_markdown=None, headings_only=None,
        sort_by="modified", sort_direction=None, since=since_from_edge("1w"),
    )
    assert err is None
    assert out.since is not None and out.since.form == "window"
    assert out.sort_by == "modified"


def test_guard_curated_requires_slice_scope():
    """since='curated' on a raw_root modal scope is rejected."""
    guard = _import_guard()
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="root-1", sort_by="created",
    )
    out, err = guard(
        spec, as_markdown=None, headings_only=None,
        sort_by=None, sort_direction=None, since=since_from_edge("curated"),
    )
    assert err is not None and err["success"] is False
    assert "curated" in err["error"]


def test_guard_item_count_since_needs_no_sort():
    """'last N' is orthogonal to sort and needs no sort_by."""
    guard = _import_guard()
    spec = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="root-1")
    out, err = guard(
        spec, as_markdown=None, headings_only=None,
        sort_by=None, sort_direction=None, since=since_from_edge("last 5"),
    )
    assert err is None
    assert out.since is not None and out.since.form == "item_count" and out.since.count == 5


# ---------------------------------------------------------------------------
# _prune_forest_to_focus_index_path — RETIRED in Phase 0.5 Step 12a.
#
# The standalone full/UUID-mode focus-prune helper was DELETED as dead code:
# once Steps 6/7 routed the cache-tail (and Step 8 the DOM-capture producer)
# through the shared materializer core (materialize_from_document_forest ->
# _focused_render_forest_with_origin), the helper had no production caller
# (its last call site in render_stage2_filter_sort only recomputed stats
# the materializer immediately overwrote). The full/UUID occurrence-exact
# focus BEHAVIOR these four tests locked is now covered by the materializer
# locks: tests/test_phase05_step7_full_uuid_policy_aware.py (occurrence-keyed
# breadcrumb suppression + note-preserving abs-leader) and
# tests/test_phase05_step8_dom_capture_markdown_materializer.py. The Job-A vs
# Job-B contrast is locked in tests/test_scoped_lightning_rod.py
# (test_jobA_vs_jobB_deliberate_contrast_same_index_path), now asserted
# against the surviving Job-A surface (the composer) + the Job-B primitive
# clone_subtree_at_index_path. Nothing lost; the coverage moved to the
# universal/shared surfaces.
# ---------------------------------------------------------------------------
