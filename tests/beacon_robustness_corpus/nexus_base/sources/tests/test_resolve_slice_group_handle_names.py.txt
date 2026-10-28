"""Layer-1 lock: the slice-group HANDLE resolver's name-grammar behavior.

These tests pin the handle-resolution contract the approved slice-group naming
plan added on top of the original id/prefix/full-name resolver
(``_resolve_slice_group_handle`` in ``server.py``):

  * a semantic SHORT name (final pipe segment, index-stripped) resolves a leaf;
  * name matching is case-insensitive at BOTH name stages;
  * a FULL stored-name hit outranks short-name matches (precedence);
  * expander/category rows are excluded from short-name resolution but remain
    reachable by id and by full name;
  * a short name shared by >1 group fails loudly with colliding_ids;
  * a handle that matches nothing fails with an error naming all accepted forms.

Pure object-graph calls into ``workflowy_mcp.server``; no I/O.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import workflowy_mcp.server as srv  # noqa: E402


def _groups():
    return [
        {"id": "grp-a1111111aaaaaaaaaaaaaaaaaaaaaaaa", "name": "nexus|glimpse|extension [2]",
         "prefixes": ["a-"], "root_uuid": "root-a"},
        {"id": "grp-b2222222bbbbbbbbbbbbbbbbbbbbbbbb", "name": "Backend",
         "prefixes": ["b-"], "root_uuid": "root-b"},
        {"id": "grp-c3333333cccccccccccccccccccccccc", "name": "[1]category",
         "prefixes": ["c-"], "root_uuid": "root-c"},
        {"id": "grp-d4444444dddddddddddddddddddddddd", "name": "dup",
         "prefixes": ["d-"], "root_uuid": "root-d"},
        {"id": "grp-e5555555eeeeeeeeeeeeeeeeeeeeeeee", "name": "nest|dup [3]",
         "prefixes": ["e-"], "root_uuid": "root-e"},
    ]


def test_short_name_resolves_leaf():
    groups = _groups()
    g, e = srv._resolve_slice_group_handle(groups, "extension")
    assert e is None
    assert g is groups[0]


def test_short_name_is_case_insensitive():
    groups = _groups()
    g, e = srv._resolve_slice_group_handle(groups, "EXTENSION")
    assert e is None
    assert g is groups[0]


def test_full_name_beats_short_name():
    # A group literally named "Backend" exists; a bare "Backend" must match it
    # by FULL name (stage 2) even though it is also that same group's short
    # name -- full-name precedence is what makes this unambiguous.
    groups = _groups()
    g, e = srv._resolve_slice_group_handle(groups, "Backend")
    assert e is None
    assert g is groups[1]


def test_expander_excluded_from_short_name():
    groups = _groups()
    g, e = srv._resolve_slice_group_handle(groups, "category")
    assert g is None
    assert e is not None and e.get("success") is False


def test_expander_resolvable_by_full_name():
    groups = _groups()
    g, e = srv._resolve_slice_group_handle(groups, "[1]category")
    assert e is None
    assert g is groups[2]


def test_full_name_precedence_over_colliding_short_name():
    groups = _groups()
    # 'nest|dup [3]' (short 'dup') and the literal 'dup' group share the short
    # name 'dup'; but 'dup' ALSO full-name-matches the literal group, which
    # takes precedence -- so a bare 'dup' resolves the literal group and is NOT
    # a short-name collision. (The ambiguity path is locked separately below.)
    groups.append({"id": "grp-f6666666ffffffffffffffffffffffffffff",
                   "name": "other|dup", "prefixes": ["f-"], "root_uuid": "root-f"})
    g, e = srv._resolve_slice_group_handle(groups, "dup")
    assert e is None and g["name"] == "dup"


def test_short_name_ambiguous_when_no_full_name_winner():
    groups = _groups()
    # Remove the literal 'dup' group; now 'dup' is only a SHORT name shared by
    # 'nest|dup [3]' and (add) 'other|dup' -> ambiguous -> colliding_ids.
    groups = [g for g in groups if g["name"] != "dup"]
    groups.append({"id": "grp-f6666666ffffffffffffffffffffffffffff",
                   "name": "other|dup", "prefixes": ["f-"], "root_uuid": "root-f"})
    g, e = srv._resolve_slice_group_handle(groups, "dup")
    assert g is None
    assert e is not None and e.get("success") is False
    assert set(e.get("colliding_ids", [])) == {
        "grp-e5555555eeeeeeeeeeeeeeeeeeeeeeee",
        "grp-f6666666ffffffffffffffffffffffffffff",
    }


def test_not_found_names_all_accepted_forms():
    groups = _groups()
    g, e = srv._resolve_slice_group_handle(groups, "does-not-exist")
    assert g is None
    assert e is not None and e.get("success") is False
    msg = e.get("error", "")
    assert "short name" in msg


# --- PUBLIC VIEW: derived display-grammar fields ----------------------------
def test_public_view_derived_fields():
    groups = _groups()
    pv = srv._slice_group_public_view(groups[0], None)
    assert pv["name"] == "nexus|glimpse|extension [2]"   # full stored name preserved
    assert pv["short_name"] == "extension"
    assert pv["name_segments"] == ["nexus", "glimpse", "extension"]
    assert pv["order_index"] == 2
    assert pv["is_expander"] is False
    pv_exp = srv._slice_group_public_view(groups[2], None)  # '[1]category'
    assert pv_exp["is_expander"] is True
    assert pv_exp["order_index"] == 1


# --- SHORT-NAME COLLISION ADVISORY ------------------------------------------
def test_short_name_collisions_finds_other_leaves():
    groups = _groups()
    # 'New|dup [9]' collides with both 'dup' (literal) and 'nest|dup [3]'.
    rows = srv._short_name_collisions(groups, "New|dup [9]", None)
    assert {r["id"] for r in rows} == {
        "grp-d4444444dddddddddddddddddddddddd",
        "grp-e5555555eeeeeeeeeeeeeeeeeeeeeeee",
    }


def test_short_name_collisions_excludes_self():
    groups = _groups()
    # Renaming groups[0] to a name whose short name collides only with itself
    # yields no warning when its own id is excluded.
    rows = srv._short_name_collisions(groups, "extension", exclude_id=groups[0]["id"])
    assert rows == []


def test_short_name_collisions_expander_candidate_yields_none():
    groups = _groups()
    # An expander candidate is organizational, not a resolvable short handle.
    assert srv._short_name_collisions(groups, "[9]whatever", None) == []


# --- LENSSPEC: group-atom resolver uses the ONE shared grammar --------------
def _lens_snapshot(groups):
    return srv._build_lens_stage0_snapshot_from_config(
        {"expand_groups": groups}, cache_by_id={}, exclude_completed=True
    )


def test_lens_group_atom_accepts_short_name_case_insensitive():
    groups = [
        {"id": "grp-a1111111aaaaaaaaaaaaaaaaaaaaaaaa", "name": "nexus|glimpse|extension [2]",
         "prefixes": ["a-"], "root_uuid": "root-a"},
    ]
    snapshot = _lens_snapshot(groups)
    resolved = snapshot.context.resolve_group("EXTENSION")  # short name, case-insensitive
    assert str(resolved["id"]).startswith("grp-a")


def test_lens_group_atom_preserves_short_name_ambiguity():
    groups = [
        {"id": "grp-a1111111aaaaaaaaaaaaaaaaaaaaaaaa", "name": "A|dup [1]",
         "prefixes": ["a-"], "root_uuid": "root-a"},
        {"id": "grp-b2222222bbbbbbbbbbbbbbbbbbbbbbbb", "name": "B|dup [2]",
         "prefixes": ["b-"], "root_uuid": "root-b"},
    ]
    snapshot = _lens_snapshot(groups)
    try:
        snapshot.context.resolve_group("dup")
    except ValueError as exc:
        assert "matches 2 groups" in str(exc)
    else:
        raise AssertionError("ambiguous short name should raise, not auto-pick")
