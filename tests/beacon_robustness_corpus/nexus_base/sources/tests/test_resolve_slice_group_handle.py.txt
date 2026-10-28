"""Layer-1 lock: the shared slice-group HANDLE resolver.

Background
----------
Two render/projection origins in ``server.py`` -- ``_glimpse_slice_group_from_
cache`` and ``_glimpse_push_slice_to_modal`` -- each carried a BYTE-IDENTICAL
copy of the same slice-group-handle resolution block:

    idx, id_err = _resolve_slice_group_id(groups, supplied)
    if id_err is not None:
        # case-insensitive NAME fallback:
        #   1 hit  -> adopt
        #   >1 hit -> colliding_ids error
        #   0 hit  -> return id_err verbatim
    chosen = groups[idx]

That copy-paste is a real drift seam (the [ap:9QK4MV] "Layer 1" extraction):
the two sites could diverge on the name-fallback wording / colliding-ids shape
under a future edit. This module locks the extracted PURE resolver
``_resolve_slice_group_handle(groups, supplied) -> (group | None, error | None)``
so the exact-id, unique-prefix, case-insensitive-name, ambiguous-name, and
not-found behaviors are pinned and cannot silently drift again.

The resolver wraps the small pure primitive ``_resolve_slice_group_id`` (the
id/prefix kernel, itself shared with the CRUD tools) and adds the two
case-insensitive NAME stages: the whole-string (full stored name) fallback the
two render sites shared, PLUS the later semantic SHORT-name stage (the final
pipe segment, index-stripped -- see ``test_resolve_slice_group_handle_names.py``
for the short-name / expander / precedence locks). It is PURE: no network, no
cache, no I/O -- it matches an in-hand ``groups`` list.

All tests here are pure object-graph calls into ``workflowy_mcp.server``.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import workflowy_mcp.server as srv  # noqa: E402


def _groups():
    """A representative slice-group config list (canonical shape).

    NOTE: the shared prefix kernel (_resolve_uuid_with_prefix_fallback) has a
    min_prefix_len of 8 hex chars (the full first UUID segment), so a UNIQUE
    prefix must be >= 8 hex chars after the 'grp-' strip. The ids below differ
    within their first 8 hex chars precisely so an 8-char prefix is a genuine
    unique-prefix test (mirroring how real 'grp-'+uuid4().hex ids look).
    """
    return [
        {"id": "grp-a1111111aaaaaaaaaaaaaaaaaaaaaaaa", "name": "Alpha",
         "prefixes": ["a-"], "root_uuid": "root-a"},
        {"id": "grp-b2222222bbbbbbbbbbbbbbbbbbbbbbbb", "name": "Beta",
         "prefixes": ["b-"], "root_uuid": "root-b"},
        {"id": "grp-c3333333cccccccccccccccccccccccc", "name": "Gamma",
         "prefixes": ["c-"], "root_uuid": "root-c"},
    ]


# --- EXACT ID -------------------------------------------------------------
def test_exact_id_resolves():
    groups = _groups()
    group, err = srv._resolve_slice_group_handle(
        groups, "grp-b2222222bbbbbbbbbbbbbbbbbbbbbbbb"
    )
    assert err is None
    assert group is groups[1]
    assert group["name"] == "Beta"


# --- UNIQUE PREFIX (via the shared _resolve_uuid_with_prefix_fallback kernel)
def test_unique_prefix_resolves():
    groups = _groups()
    # 'grp-b2222222' is a unique 8-hex-char prefix of exactly the Beta id
    # (>= the kernel's min_prefix_len of 8; the ids diverge in char 1).
    group, err = srv._resolve_slice_group_handle(groups, "grp-b2222222")
    assert err is None
    assert group is groups[1]


# --- CASE-INSENSITIVE NAME FALLBACK --------------------------------------
def test_name_fallback_exact_case():
    groups = _groups()
    group, err = srv._resolve_slice_group_handle(groups, "Gamma")
    assert err is None
    assert group is groups[2]


def test_name_fallback_is_case_insensitive():
    groups = _groups()
    group, err = srv._resolve_slice_group_handle(groups, "aLpHa")
    assert err is None
    assert group is groups[0]


def test_name_fallback_trims_whitespace():
    groups = _groups()
    group, err = srv._resolve_slice_group_handle(groups, "  beta  ")
    assert err is None
    assert group is groups[1]


# --- AMBIGUOUS NAME -> colliding_ids error, NO auto-pick ------------------
def test_ambiguous_name_returns_colliding_ids():
    groups = _groups()
    groups.append(
        {"id": "grp-d4444444dddddddddddddddddddddddd", "name": "Alpha",
         "prefixes": ["d-"], "root_uuid": "root-d"}
    )
    group, err = srv._resolve_slice_group_handle(groups, "alpha")
    assert group is None
    assert isinstance(err, dict)
    assert err.get("success") is False
    assert set(err.get("colliding_ids") or []) == {
        "grp-a1111111aaaaaaaaaaaaaaaaaaaaaaaa",
        "grp-d4444444dddddddddddddddddddddddd",
    }


# --- NOT FOUND -> the id-resolution error verbatim -----------------------
def test_not_found_returns_id_error():
    groups = _groups()
    group, err = srv._resolve_slice_group_handle(groups, "does-not-exist")
    assert group is None
    assert isinstance(err, dict)
    assert err.get("success") is False
    # The not-found error carries an actionable message (from the id kernel).
    assert "error" in err


# --- EQUIVALENCE with the raw id kernel on the exact/prefix paths ---------
def test_matches_id_kernel_on_exact_and_prefix():
    """On id/prefix inputs the handle resolver must return the SAME group the
    raw ``_resolve_slice_group_id`` primitive resolves (the handle resolver
    only ADDS the name fallback; it must not change id/prefix behavior)."""
    groups = _groups()
    for supplied in (
        "grp-c3333333cccccccccccccccccccccccc",  # exact
        "grp-c3333333",                           # unique 8-hex-char prefix
    ):
        idx, id_err = srv._resolve_slice_group_id(groups, supplied)
        group, err = srv._resolve_slice_group_handle(groups, supplied)
        assert id_err is None and err is None
        assert group is groups[idx]


# --- BARE 8-hex prefix (the compact-inventory displayed handle) -----------
def test_bare_8_hex_group_prefix_resolves_without_grp():
    """Compact-inventory regression lock (slice_group.list detail='compact').

    The compact slice-group inventory displays the BARE first-8-hex group
    prefix with the 'grp-' key prefix elided. The shared id kernel tolerates
    the absent 'grp-' (it strips the prefix when present and hyphen-normalizes
    otherwise), so the displayed form must resolve back to its group today.
    """
    groups = _groups()
    group, err = srv._resolve_slice_group_handle(groups, "c3333333")
    assert err is None
    assert group is groups[2]
    idx, id_err = srv._resolve_slice_group_id(groups, "c3333333")
    assert id_err is None
    assert groups[idx] is groups[2]
