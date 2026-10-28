"""Phase A lock for the RenderProjectionSpec object + render_scope.descend_policy.

Background
----------
Phase A of the cache-render-request refactor (see
``__CURATION__/HANDOFF_cache_render_request_refactor_spec.md``) introduces ONE
normalized cache-render request object -- ``RenderProjectionSpec`` -- and adds a
``descend_policy`` field to the server-authoritative ``render_scope`` echo
composed by ``server._compose_render_scope``. Phase A must change NO behavior:
the object exists and round-trips through ``render_scope`` with all defaults,
and every existing cache-projected MODAL render still echoes ``descend_policy:
'all'`` (the raw-dump-with-boundary feature currently lives only on the
content-return GLIMPSE path, which composes no render_scope).

These tests lock:
  1. RenderProjectionSpec default field values (the safe-defaults contract).
  2. The (slice_group, root_uuid, lightning) -> kind truth table in the two
     normalizers (glimpse-args + modal-frame), so a later migration can drop
     them in with no behavior change.
  3. The wire-vs-internal ``sort_by`` translation ('none' <-> None).
  4. Lenient, safe fallback of unknown flat-knob values.
  5. Frozen (immutable) dataclass.
  6. ``_compose_render_scope`` now emits ``descend_policy`` -- defaulting to
     'all', honoring an explicit value, and coercing garbage back to 'all'.
  7. The object's ``to_render_scope_fields()`` agrees with what
     ``_compose_render_scope`` emits for the shared flat keys (so the two
     spellings can never drift).

All pure: no network, no cache, no I/O.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import dataclasses  # noqa: E402

import pytest  # noqa: E402

import workflowy_mcp.server as srv  # noqa: E402
from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402
from workflowy_mcp.f8_slice_policy import (  # noqa: E402
    SERVER_SIDE_GLIMPSE_EXCLUDE_COMPLETED,
)


# ---------------------------------------------------------------------------
# 1. Default field values
# ---------------------------------------------------------------------------
def test_default_spec_fields():
    s = RenderProjectionSpec()
    assert s.kind == "raw_root"
    assert s.root_uuid is None
    assert s.group_id is None
    assert s.group_name is None
    assert s.lightning is None
    assert s.match_scope == "name_and_note"
    assert s.descend_policy == "all"
    # exclude_completed mirrors the pipeline's fixed policy constant.
    assert s.exclude_completed == SERVER_SIDE_GLIMPSE_EXCLUDE_COMPLETED
    assert s.sort_by == "none"
    assert s.sort_direction == "asc"
    assert s.since is None
    assert s.headings_only is False
    assert s.as_markdown is True
    assert s.content_format == "markdown"
    assert s.tree_mode == "render_tree"


def test_sort_by_internal_translation():
    assert RenderProjectionSpec().sort_by_internal() is None
    assert RenderProjectionSpec(sort_by="created").sort_by_internal() == "created"
    assert RenderProjectionSpec(sort_by="modified").sort_by_internal() == "modified"
    # wire 'none' -> internal None
    assert RenderProjectionSpec(sort_by="none").sort_by_internal() is None


# ---------------------------------------------------------------------------
# 2. (slice_group, root_uuid, lightning) -> kind truth table
# ---------------------------------------------------------------------------
def test_normalize_from_glimpse_args_truth_table():
    # neither -> raw_root
    assert RenderProjectionSpec.normalize_from_glimpse_args().kind == "raw_root"
    # root only -> raw_root
    assert (
        RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="u").kind == "raw_root"
    )
    # slice only -> slice
    assert (
        RenderProjectionSpec.normalize_from_glimpse_args(slice_group="g").kind == "slice"
    )
    # slice + root -> slice_override
    assert (
        RenderProjectionSpec.normalize_from_glimpse_args(slice_group="g", root_uuid="u").kind
        == "slice_override"
    )
    # lightning present -> lightning_rod (takes precedence over everything)
    assert (
        RenderProjectionSpec.normalize_from_glimpse_args(
            lightning={"mode": "gather"}, root_uuid="u"
        ).kind
        == "lightning_rod"
    )
    assert (
        RenderProjectionSpec.normalize_from_glimpse_args(
            lightning={"mode": "render_tags", "tags": ["x"]}, slice_group="g", root_uuid="u"
        ).kind
        == "lightning_rod"
    )


def test_normalize_glimpse_args_group_id_alias():
    # slice_group and explicit group_id are the same slot; explicit wins.
    s = RenderProjectionSpec.normalize_from_glimpse_args(group_id="gid", root_uuid="u")
    assert s.kind == "slice_override"
    assert s.group_id == "gid"
    s2 = RenderProjectionSpec.normalize_from_glimpse_args(slice_group="sg")
    assert s2.group_id == "sg"


def test_normalize_from_modal_frame_truth_table():
    # The frame carries the WHOLE request as ONE nested `render_spec` blob (the
    # sole scope/view contract on the wire); normalize_from_modal_frame reads
    # ONLY that blob. A frame with no (or an empty) render_spec -> raw_root
    # defaults.
    f0 = RenderProjectionSpec.normalize_from_modal_frame({})
    assert f0.kind == "raw_root"
    assert f0.descend_policy == "all"

    f1 = RenderProjectionSpec.normalize_from_modal_frame(
        {"render_spec": {
            "group_id": "g", "root_uuid": "u",
            "descend_policy": "stop_at_child_anchors",
            "sort_by": "created", "match_scope": "name",
        }}
    )
    assert f1.kind == "slice_override"
    assert f1.descend_policy == "stop_at_child_anchors"
    assert f1.sort_by == "created"
    assert f1.sort_by_internal() == "created"
    assert f1.match_scope == "name"

    f2 = RenderProjectionSpec.normalize_from_modal_frame(
        {"render_spec": {"lightning": {"mode": "render_search", "query": "hi"}}}
    )
    assert f2.kind == "lightning_rod"
    assert f2.lightning == {"mode": "render_search", "query": "hi"}

    # a non-dict lightning inside the blob is ignored (not a lightning render)
    f3 = RenderProjectionSpec.normalize_from_modal_frame(
        {"render_spec": {"lightning": "nope"}}
    )
    assert f3.kind == "raw_root"
    assert f3.lightning is None


# ---------------------------------------------------------------------------
# 3/4. exclude_completed default + lenient fallbacks
# ---------------------------------------------------------------------------
def test_exclude_completed_default_and_override():
    assert (
        RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="u").exclude_completed
        == SERVER_SIDE_GLIMPSE_EXCLUDE_COMPLETED
    )
    assert (
        RenderProjectionSpec.normalize_from_glimpse_args(
            root_uuid="u", exclude_completed=False
        ).exclude_completed
        is False
    )


def test_lenient_fallbacks_on_garbage_values():
    s = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="u",
        match_scope="BOGUS",
        descend_policy="BOGUS",
        sort_by="BOGUS",
        sort_direction="BOGUS",
        content_format="BOGUS",
        tree_mode="BOGUS",
    )
    assert s.match_scope == "name_and_note"
    assert s.descend_policy == "all"
    assert s.sort_by == "none"
    assert s.sort_direction == "asc"
    assert s.content_format == "markdown"
    assert s.tree_mode == "render_tree"


# ---------------------------------------------------------------------------
# 5. Frozen / immutable
# ---------------------------------------------------------------------------
def test_spec_is_frozen():
    s = RenderProjectionSpec()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.kind = "slice"  # type: ignore[misc]


def test_with_overrides_returns_new_copy():
    s = RenderProjectionSpec()
    s2 = s.with_overrides(descend_policy="stop_at_child_anchors")
    assert s.descend_policy == "all"  # original unchanged
    assert s2.descend_policy == "stop_at_child_anchors"
    assert s2 is not s


# ---------------------------------------------------------------------------
# 6. _compose_render_scope now emits descend_policy
# ---------------------------------------------------------------------------
def _compose(**kw):
    base = dict(
        group_id=None,
        group_name=None,
        root_uuid="root-uuid",
        is_raw_dump=True,
        root_overridden=False,
        sort_by=None,
        sort_direction="asc",
        since=None,
        headings_only=False,
    )
    base.update(kw)
    return srv._compose_render_scope(**base)


def test_compose_render_scope_defaults_descend_policy_all():
    scope = _compose()
    assert scope["descend_policy"] == "all"
    assert scope["kind"] == "raw_root"


def test_compose_render_scope_honors_explicit_descend_policy():
    scope = _compose(descend_policy="stop_at_child_anchors")
    assert scope["descend_policy"] == "stop_at_child_anchors"


def test_compose_render_scope_coerces_bad_descend_policy():
    scope = _compose(descend_policy="garbage")
    assert scope["descend_policy"] == "all"


def test_compose_render_scope_descend_policy_present_for_slice_and_lightning():
    slice_scope = srv._compose_render_scope(
        group_id="g", group_name="G", root_uuid="r", is_raw_dump=False,
        root_overridden=False, sort_by=None, sort_direction="asc", since=None,
        headings_only=False,
    )
    assert slice_scope["kind"] == "slice"
    assert slice_scope["descend_policy"] == "all"

    lr_scope = srv._compose_render_scope(
        group_id=None, group_name=None, root_uuid="r", is_raw_dump=False,
        root_overridden=False, sort_by=None, sort_direction="asc", since=None,
        headings_only=False, lightning={"mode": "gather", "match_scope": "name_and_note"},
    )
    assert lr_scope["kind"] == "lightning_rod"
    assert lr_scope["descend_policy"] == "all"
    # descend_policy is TOP-LEVEL, never inside the opaque selection.
    assert "descend_policy" not in lr_scope.get("selection", {})


# ---------------------------------------------------------------------------
# 7. spec.to_render_scope_fields() agrees with _compose_render_scope
# ---------------------------------------------------------------------------
def test_spec_fields_agree_with_compose_for_shared_keys():
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="root-uuid", descend_policy="stop_at_child_anchors"
    )
    fields = spec.to_render_scope_fields()
    scope = _compose(descend_policy="stop_at_child_anchors")
    shared = ("kind", "group_id", "group_name", "root_uuid", "match_scope",
              "descend_policy", "sort_by", "sort_direction", "since",
              "headings_only", "content_format", "tree_mode")
    for k in shared:
        assert fields[k] == scope[k], f"mismatch on {k!r}: spec={fields[k]!r} scope={scope[k]!r}"


# ---------------------------------------------------------------------------
# 8. Phase C -- the UNMOORED SIGNAL (live_gui_sync_supported) is emitted at the
#    TOP LEVEL for EVERY kind, with a reason naming WHY sync is skipped. This is
#    the flag the client reads to indicate an intentionally-unmoored render on
#    the yellow bar instead of silently doing nothing.
# ---------------------------------------------------------------------------
def _compose_slice(**kw):
    base = dict(
        group_id="g", group_name="G", root_uuid="r", is_raw_dump=False,
        root_overridden=False, sort_by=None, sort_direction="asc", since=None,
        headings_only=False,
    )
    base.update(kw)
    return srv._compose_render_scope(**base)


def test_unmoored_plain_slice_is_synced():
    # A plain slice (default descend, no variant explosion) CAN sync -> True,
    # no reason.
    scope = _compose_slice()
    assert scope["live_gui_sync_supported"] is True
    assert "live_gui_sync_skipped_reason" not in scope


def test_unmoored_raw_dump():
    scope = _compose(is_raw_dump=True)
    assert scope["live_gui_sync_supported"] is False
    assert scope["live_gui_sync_skipped_reason"] == "raw_dump"


def test_unmoored_descend_policy_on_slice():
    scope = _compose_slice(descend_policy="stop_at_child_anchors")
    assert scope["live_gui_sync_supported"] is False
    assert scope["live_gui_sync_skipped_reason"] == "descend_policy"


def test_unmoored_too_many_variants_on_slice():
    scope = _compose_slice(search_too_many_variants=True)
    assert scope["live_gui_sync_supported"] is False
    assert scope["live_gui_sync_skipped_reason"] == "too_many_variants"


def test_unmoored_lightning_always_false():
    scope = srv._compose_render_scope(
        group_id=None, group_name=None, root_uuid="r", is_raw_dump=False,
        root_overridden=False, sort_by=None, sort_direction="asc", since=None,
        headings_only=False, lightning={"mode": "render_tags", "tags": ["x"], "match_scope": "name_and_note"},
    )
    assert scope["live_gui_sync_supported"] is False
    assert scope["live_gui_sync_skipped_reason"] == "lightning"


def test_unmoored_reason_precedence_descend_beats_raw():
    # A raw dump that also stops at child anchors reports the more-specific
    # 'descend_policy' reason (it precedes the raw_dump fallback), so the client
    # shows the descend-policy message rather than the generic raw-dump quiet.
    scope = _compose(is_raw_dump=True, descend_policy="stop_at_child_anchors")
    assert scope["live_gui_sync_supported"] is False
    assert scope["live_gui_sync_skipped_reason"] == "descend_policy"
