"""Unit 4 — Anchor Dock favorites as a tag FAMILY (contract [ap:7CYWWE] rev 1).

Coverage (§9.2):
  * ``WorkFlowyClientNexus.list_note_sessions`` — favorites_only membership,
    descendant rule (b) (locals through an unbroken chain of LOCAL Docks; a
    non-favorite GLOBAL Dock cuts the chain), rule (d) hide, the zero-favorites
    fallback + composed hint/notice, echo counts, is_favorite on rows and tree
    nodes, starred-local visibility with show_local off, and
    ``--deprecated``-only nodes counted nowhere. Fixture shapes model §6.4.5
    (RECON T1–T3 with generic names).
  * ``_render_anchor_tree_raw_text`` — compact ⭐ (before Ⓛ), the favorites
    and fallback header grammar, and the legacy byte lock WITH the new kwargs.
  * ``_anchor_dock_list_impl`` / registry / WS widget endpoint — the
    favorites_only default lives in the impl ONCE; the widget flag defaults
    False when absent.
  * ``_anchor_dock_peek_impl`` — is_favorite_dock rows, ⚓ ⭐ compact marker,
    Key line.
  * Thread-through — root guard refuses a favorite-only root and allows a
    deprecated-only root; touch refuses a favorite-only Dock; the promote
    nearest-Dock-ancestor walk finds a favorite-only ancestor and skips a
    deprecated-only one. Config replies in these tests carry NON-default
    words so a site still using the shipped defaults FAILS.
  * Setters (§6.6/§6.7) — dock.update truth table, families selection,
    create/promote favorite. (Appended with gate G4.3.)

Harness note (§9.2 UNVERIFIED, CONFIRMED): ``_build_path_lines_from_cache``,
``_collect_descendant_tags`` and ``_strip_cartographer_metadata_from_note_for_preview``
run on a ``WorkFlowyClientNexus.__new__`` instance (pure / staticmethod — they
touch no instance state), so no stubbing beyond ``export_nodes`` is needed.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from coherence_harness import make_boundary_capable
from workflowy_mcp import server
from workflowy_mcp.client import api_client_nexus as nx
from workflowy_mcp.client.api_client_nexus import (
    WorkFlowyClientNexus,
    _render_anchor_tree_raw_text,
)
from workflowy_mcp.mcp_api_registry import dispatch_operation, validate_params
from workflowy_mcp.models.node import WorkFlowyNode

BASES = ["wfy-ns"]
LOCAL_WORDS = ["local"]
FAV_WORDS = ["fav"]
DEP_WORDS = ["deprecated"]
REGION_DOCK = "dddddddd-1111-4111-8111-111111111111"
REGION_PARENT = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def _region_boundaries(monkeypatch, tmp_path):
    from test_mutation_regions_n05_fences import legacy_region_fronts
    legacy_region_fronts(monkeypatch, tmp_path,
        nodes=[{"id": key, "parent_id": None} for key in (REGION_DOCK, REGION_PARENT)],
        entries=("_anchor_dock_update_impl", "_anchor_dock_create_impl"))


# ---------------------------------------------------------------------------
# Fixture — §6.4.5 shapes (RECON T1–T3) with generic names.
#
#   ROOT (plain)
#   ├─ ARCH   (G)                       [T2 System Architecture]
#   │  ├─ L1 (L)  ├─ L2 (L) → L2A (L)  ├─ L3 (L)  ├─ L4 (L)
#   │  ├─ CONN (G)                      [connective global, T1's cca92c8f]
#   │  │   └─ MGR (G)                   [T1 MANAGER SESSIONS]
#   │  │       ├─ ARCL (L)              [ARCHIVES]
#   │  │       │   ├─ S1 S2 S3 (L)      [Manager-Session-001…013]
#   │  │       ├─ S4 (L)                [Manager-Session-014…019]
#   │  │       └─ S5 (L)
#   │  │           └─ PROG (L)          [PROGRAM]
#   │  │               └─ WORK (L)      [Working notes]
#   │  └─ GX (G) → GXL (L)              [nested G + its local]
#   ├─ AGENT  (G)                       [T3 AGENT ROOT]
#   │  └─ DASH (L)                      [Dashboard Results]
#   │      ├─ OBTAIN (G)                [How to Obtain Results — G under L]
#   │      ├─ BOX1 (L)  └─ BOX2 (L)     [the five 🗃️ rows]
#   ├─ RETIRED  (#base--dep ONLY — NOT a Dock)
#   └─ DEPL     (L + dep — a local Dock; dep tag rides along)
# ---------------------------------------------------------------------------

def _n(nid, name, parent, priority=0):
    return {"id": nid, "name": name, "note": "", "parent_id": parent,
            "priority": priority}


def make_nodes(base="wfy-ns", local="local", fav="fav", dep="deprecated"):
    """The fixture tree with configurable vocabulary words."""
    G = f"#{base}"
    L = f"#{base}--{local}"
    D = f"#{base}--{dep}"
    return [
        _n("ROOT", "Vault Root", None, 0),
        _n("ARCH", f"Architecture Hub {G}", "ROOT", 0),
        _n("L1", f"Local One {L}", "ARCH", 0),
        _n("L2", f"Local Two {L}", "ARCH", 1),
        _n("L2A", f"Local Two A {L}", "L2", 0),
        _n("L3", f"Local Three {L}", "ARCH", 2),
        _n("L4", f"Local Four {L}", "ARCH", 3),
        _n("CONN", f"Connective Deployment {G}", "ARCH", 4),
        _n("MGR", f"Manager Hub {G}", "CONN", 0),
        _n("ARCL", f"Archives {L}", "MGR", 0),
        _n("S1", f"Session 001 {L}", "ARCL", 0),
        _n("S2", f"Session 002 {L}", "ARCL", 1),
        _n("S3", f"Session 003 {L}", "ARCL", 2),
        _n("S4", f"Session 004 {L}", "MGR", 1),
        _n("S5", f"Session 005 {L}", "MGR", 2),
        _n("PROG", f"Program {L}", "S5", 0),
        _n("WORK", f"Working notes {L}", "PROG", 0),
        _n("GX", f"Nested G X {G}", "ARCH", 5),
        _n("GXL", f"X Local {L}", "GX", 0),
        _n("AGENT", f"Agent Root {G}", "ROOT", 1),
        _n("DASH", f"Dashboard Results {L}", "AGENT", 0),
        _n("OBTAIN", f"How to Obtain {G}", "DASH", 0),
        _n("BOX1", f"Box One {L}", "DASH", 1),
        _n("BOX2", f"Box Two {L}", "DASH", 2),
        _n("RETIRED", f"Retired Dock {D}", "ROOT", 2),
        _n("DEPL", f"Deprecated Local {L} {D}", "ROOT", 3),
    ]


def star_nodes(nodes, ids, fav="fav"):
    """Return a copy of ``nodes`` with #base--fav added to the given ids."""
    out = []
    for n in nodes:
        n = dict(n)
        if n["id"] in ids:
            n["name"] = f"{n['name']} #wfy-ns--{fav}"
        out.append(n)
    return out


def make_client(nodes):
    """Real client via __new__ + fake export_nodes (the _make_client pattern)."""
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)

    async def _fake_export_nodes(node_id=None, use_cache=True, **kw):
        return {"nodes": nodes}

    c.export_nodes = _fake_export_nodes  # type: ignore[assignment]
    return c


def list_sessions(nodes, *, favorites_only=False, show_local=False,
                  bases=BASES, local=LOCAL_WORDS, fav=FAV_WORDS, dep=DEP_WORDS,
                  tree_view=True):
    c = make_client(nodes)
    return asyncio.run(c.list_note_sessions(
        list(bases), [], list(local),
        favorite_postfixes=list(fav),
        deprecated_postfixes=list(dep),
        favorites_only=favorites_only,
        show_local_anchors=show_local,
        tree_view=tree_view,
    ))


def shown_ids(result):
    return [row["id"] for row in result["sessions"]]


def tree_rows(tree):
    """Flatten an anchor_tree into (id, depth, node) in render order."""
    out = []

    def _walk(node, depth):
        out.append((node["id"], depth, node))
        for ch in node.get("children", []):
            _walk(ch, depth + 1)

    for ch in (tree or {}).get("children", []):
        _walk(ch, 0)
    return out


# ---------------------------------------------------------------------------
# list_note_sessions — favorites_only=False is byte-for-byte today's rule
# ---------------------------------------------------------------------------

def test_favorites_off_matches_todays_rule():
    nodes = star_nodes(make_nodes(), {"MGR"})
    r = list_sessions(nodes, favorites_only=False, show_local=False)
    # Today's rule: every GLOBAL Dock, visual order; locals hidden.
    assert shown_ids(r) == ["ARCH", "CONN", "MGR", "GX", "AGENT", "OBTAIN"]
    assert r["favorites_only"] is False
    assert r["favorites_fallback"] is False
    assert r["favorite_count"] == 1          # all favorites in the vault
    assert r["total_dock_count"] == 24       # 6 global + 18 local
    assert r["shown_count"] == 6 and r["shown_global_count"] == 6
    assert r["shown_local_count"] == 0
    # RETIRED (deprecated-only) is counted NOWHERE.
    assert "RETIRED" not in shown_ids(r)
    assert r["local_anchor_count"] == 18     # DEPL counts; RETIRED does not

    r2 = list_sessions(nodes, favorites_only=False, show_local=True)
    assert shown_ids(r2) == [
        "ARCH", "L1", "L2", "L2A", "L3", "L4", "CONN", "MGR",
        "ARCL", "S1", "S2", "S3", "S4", "S5", "PROG", "WORK",
        "GX", "GXL", "AGENT", "DASH", "OBTAIN", "BOX1", "BOX2", "DEPL",
    ]
    assert r2["shown_count"] == 24
    assert r2["shown_global_count"] == 6 and r2["shown_local_count"] == 18
    # Rows carry is_favorite ALWAYS; is_local only when shown.
    mgr_row = next(row for row in r["sessions"] if row["id"] == "MGR")
    assert mgr_row["is_favorite"] is True
    assert "is_local" not in mgr_row
    mgr_row2 = next(row for row in r2["sessions"] if row["id"] == "MGR")
    assert mgr_row2["is_favorite"] is True and mgr_row2["is_local"] is False


def test_star_manager_hub_scenario_t1():
    # §6.4.5 scenario 1: star MGR (a G under two non-favorite G Docks),
    # favorites_only + include_local: the whole tucked-away local structure
    # surfaces through the unbroken L chain; the G ancestors are connective.
    nodes = star_nodes(make_nodes(), {"MGR"})
    r = list_sessions(nodes, favorites_only=True, show_local=True)
    assert shown_ids(r) == [
        "MGR", "ARCL", "S1", "S2", "S3", "S4", "S5", "PROG", "WORK",
    ]
    depths = {nid: d for nid, d, _ in tree_rows(r["anchor_tree"])}
    assert depths["MGR"] == 0            # connective Gs collapsed
    assert depths["ARCL"] == 1 and depths["S1"] == 2
    assert depths["S5"] == 1 and depths["PROG"] == 2 and depths["WORK"] == 3
    assert r["favorites_only"] is True and r["favorites_fallback"] is False
    assert r["favorite_count"] == 1 and r["shown_count"] == 9
    assert r["shown_global_count"] == 1 and r["shown_local_count"] == 8
    # Tree nodes carry is_favorite / is_local.
    mgr_node = next(n for nid, _, n in tree_rows(r["anchor_tree"]) if nid == "MGR")
    assert mgr_node["is_favorite"] is True and mgr_node["is_local"] is False
    arcl_node = next(n for nid, _, n in tree_rows(r["anchor_tree"]) if nid == "ARCL")
    assert arcl_node["is_favorite"] is False and arcl_node["is_local"] is True


def test_star_manager_hub_without_show_local():
    # show_local off: only the starred Dock itself (no chain step).
    nodes = star_nodes(make_nodes(), {"MGR"})
    r = list_sessions(nodes, favorites_only=True, show_local=False)
    assert shown_ids(r) == ["MGR"]
    assert r["shown_global_count"] == 1 and r["shown_local_count"] == 0


def test_star_architecture_hub_scenario_t2():
    # §6.4.5 scenario 2: star ARCH alone — its direct locals and their local
    # children; NONE of the nested G Docks; none of THEIR locals (the chain
    # through a non-favorite G is cut).
    nodes = star_nodes(make_nodes(), {"ARCH"})
    r = list_sessions(nodes, favorites_only=True, show_local=True)
    assert shown_ids(r) == ["ARCH", "L1", "L2", "L2A", "L3", "L4"]
    depths = {nid: d for nid, d, _ in tree_rows(r["anchor_tree"])}
    assert depths == {"ARCH": 0, "L1": 1, "L2": 1, "L2A": 2, "L3": 1, "L4": 1}


def test_star_agent_root_scenario_t3():
    # §6.4.5 scenario 3: star AGENT — DASH Ⓛ and the two boxes through the
    # local chain; OBTAIN (G under an L) hidden by rule (d).
    nodes = star_nodes(make_nodes(), {"AGENT"})
    r = list_sessions(nodes, favorites_only=True, show_local=True)
    assert shown_ids(r) == ["AGENT", "DASH", "BOX1", "BOX2"]
    assert "OBTAIN" not in shown_ids(r)      # rule (d): connective, hidden
    depths = {nid: d for nid, d, _ in tree_rows(r["anchor_tree"])}
    assert depths == {"AGENT": 0, "DASH": 1, "BOX1": 2, "BOX2": 2}


def test_multiple_stars_compose_scenario_t1_t2():
    # §6.4.5 scenario 4: star BOTH ARCH and MGR — MGR appears beneath ARCH at
    # depth 1 (the connective G collapses), its locals beneath it.
    nodes = star_nodes(make_nodes(), {"ARCH", "MGR"})
    r = list_sessions(nodes, favorites_only=True, show_local=True)
    assert shown_ids(r) == [
        "ARCH", "L1", "L2", "L2A", "L3", "L4", "MGR",
        "ARCL", "S1", "S2", "S3", "S4", "S5", "PROG", "WORK",
    ]
    depths = {nid: d for nid, d, _ in tree_rows(r["anchor_tree"])}
    assert depths["ARCH"] == 0 and depths["MGR"] == 1
    assert depths["ARCL"] == 2 and depths["WORK"] == 4
    assert r["favorite_count"] == 2 and r["shown_count"] == 15


def test_rule_b_intervening_global_dock_cuts_the_chain():
    # GXL is a local under GX (a NON-favorite GLOBAL Dock) under starred ARCH:
    # the chain ARCH -> GX cuts at GX, so GXL does NOT surface. (This is the
    # mutation-sensitivity case against rule (c) "all local descendants".)
    nodes = star_nodes(make_nodes(), {"ARCH"})
    r = list_sessions(nodes, favorites_only=True, show_local=True)
    assert "GX" not in shown_ids(r)
    assert "GXL" not in shown_ids(r)


def test_starred_local_shows_with_show_local_false():
    # A starred LOCAL Dock was starred deliberately: it shows even with
    # show_local=False (and renders ⭐ Ⓛ); its unstarred local children do not.
    nodes = star_nodes(make_nodes(), {"DASH"})
    r = list_sessions(nodes, favorites_only=True, show_local=False)
    assert shown_ids(r) == ["DASH"]
    row = r["sessions"][0]
    assert row["is_favorite"] is True
    # is_local keeps its today-only-when-shown rule on ROWS...
    assert "is_local" not in row
    # ...but the TREE node carries it, so the compact render marks ⭐ Ⓛ.
    dash_node = next(n for nid, _, n in tree_rows(r["anchor_tree"]) if nid == "DASH")
    assert dash_node["is_local"] is True and dash_node["is_favorite"] is True
    # With show_local on, the boxes chain through the starred local.
    r2 = list_sessions(nodes, favorites_only=True, show_local=True)
    assert shown_ids(r2) == ["DASH", "BOX1", "BOX2"]


def test_scenarios_two_three_four_with_show_local_off():
    # §9.2: each starring scenario with show_local ON and OFF. With the flag
    # off, only the starred Docks themselves show (no chain step) -- starred
    # locals included, unstarred locals never.
    nodes = star_nodes(make_nodes(), {"ARCH"})
    r = list_sessions(nodes, favorites_only=True, show_local=False)
    assert shown_ids(r) == ["ARCH"]
    r = list_sessions(nodes, favorites_only=True, show_local=True)
    assert shown_ids(r) == ["ARCH", "L1", "L2", "L2A", "L3", "L4"]

    nodes = star_nodes(make_nodes(), {"AGENT"})
    r = list_sessions(nodes, favorites_only=True, show_local=False)
    assert shown_ids(r) == ["AGENT"]          # DASH is local, unstarred
    r = list_sessions(nodes, favorites_only=True, show_local=True)
    assert shown_ids(r) == ["AGENT", "DASH", "BOX1", "BOX2"]

    nodes = star_nodes(make_nodes(), {"ARCH", "MGR"})
    r = list_sessions(nodes, favorites_only=True, show_local=False)
    assert shown_ids(r) == ["ARCH", "MGR"]
    depths = {nid: d for nid, d, _ in tree_rows(r["anchor_tree"])}
    assert depths == {"ARCH": 0, "MGR": 1}    # CONN connective even with locals off


def test_zero_favorites_fallback_flags_notice_and_composed_hint():
    nodes = make_nodes()  # nothing starred anywhere
    r = list_sessions(nodes, favorites_only=True, show_local=False)
    assert r["favorites_fallback"] is True
    assert r["favorites_only"] is False       # EFFECTIVE value
    assert r["favorite_count"] == 0
    assert r["favorites_notice"] == (
        "no favorites configured — showing all Docks; tag a Dock "
        "#wfy-ns--fav to shortlist (tag.add, or dock.update favorite=true)"
    )
    assert r["favorite_tag_hint"] == "#wfy-ns--fav"
    # Inclusion is computed exactly as with favorites_only=False.
    assert shown_ids(r) == ["ARCH", "CONN", "MGR", "GX", "AGENT", "OBTAIN"]
    assert all(row["is_favorite"] is False for row in r["sessions"])


def test_fallback_hint_uses_configured_base_and_word_never_hardcoded():
    # NON-default base + words: the hint must compose from the LOOKED-UP
    # vocabulary (a hardcoded 'wfy-ns'/'fav' fails here), and the configured
    # favorite word is what makes a Dock a favorite.
    nodes = make_nodes(base="convrisk-docks", local="nested",
                       fav="star", dep="retired")
    r = list_sessions(
        nodes, favorites_only=True, show_local=False,
        bases=["convrisk-docks"], local=["nested"], fav=["star"], dep=["retired"],
    )
    assert r["favorite_tag_hint"] == "#convrisk-docks--star"
    assert "#convrisk-docks--star" in r["favorites_notice"]
    assert r["favorites_fallback"] is True
    # Now star one with the configured word: it is recognized.
    starred = []
    for n in nodes:
        n = dict(n)
        if n["id"] == "AGENT":
            n["name"] += " #convrisk-docks--star"
        starred.append(n)
    r2 = list_sessions(
        starred, favorites_only=True, show_local=False,
        bases=["convrisk-docks"], local=["nested"], fav=["star"], dep=["retired"],
    )
    assert r2["favorites_fallback"] is False
    assert shown_ids(r2) == ["AGENT"]
    assert r2["note_session_favorite_postfixes"] == ["star"]


def test_echo_keys_and_favorite_postfixes_always_present():
    nodes = star_nodes(make_nodes(), {"MGR"})
    r = list_sessions(nodes, favorites_only=True, show_local=True)
    for key in (
        "favorites_only", "favorites_fallback", "favorite_count",
        "total_dock_count", "shown_count", "shown_global_count",
        "shown_local_count", "favorite_tag_hint",
        "note_session_favorite_postfixes", "favorites_notice",
    ):
        assert key in r, key
    assert r["note_session_favorite_postfixes"] == ["fav"]
    assert r["favorites_notice"] is None       # not the fallback


def test_deprecated_only_node_is_counted_nowhere():
    # RETIRED (#base--dep only): not a Dock — not listed (even with
    # show_local), not in total_dock_count, not a favorite; DEPL (local +
    # dep) IS a local Dock and IS listed with show_local.
    nodes = make_nodes()
    r = list_sessions(nodes, favorites_only=False, show_local=True)
    ids = shown_ids(r)
    assert "RETIRED" not in ids
    assert "DEPL" in ids
    dep_row = next(row for row in r["sessions"] if row["id"] == "DEPL")
    assert dep_row["is_local"] is True and dep_row["is_favorite"] is False
    # 24 = 6 globals + 18 locals; RETIRED contributes to neither.
    assert r["total_dock_count"] == 24


# ---------------------------------------------------------------------------
# _render_anchor_tree_raw_text — compact ⭐ / headers / legacy byte lock
# ---------------------------------------------------------------------------

def _tree_for_render():
    return {
        "root_uuid": "ROOT",
        "root_name": "Root",
        "children": [
            {
                "id": "aaaaaaaa-0000-0000-0000-000000000000",
                "name": "Starred Global",
                "is_local": False,
                "is_favorite": True,
                "children": [
                    {
                        "id": "bbbbbbbb-0000-0000-0000-000000000000",
                        "name": "Starred Local",
                        "is_local": True,
                        "is_favorite": True,
                        "children": [],
                    }
                ],
            },
            {
                "id": "cccccccc-0000-0000-0000-000000000000",
                "name": "Plain Local",
                "is_local": True,
                "is_favorite": False,
                "children": [],
            },
        ],
    }


def test_compact_rows_mark_star_before_local():
    out = _render_anchor_tree_raw_text(
        _tree_for_render(),
        global_count=2, local_count=2, show_local=True,
        compact=True, favorites_only=True, favorite_count=2,
        total_dock_count=9,
    )
    lines = out.splitlines()
    assert lines[2] == "[aaaaaaaa] Starred Global ⭐"
    # ⭐ BEFORE Ⓛ on one row.
    assert lines[3] == "[bbbbbbbb]   Starred Local ⭐ Ⓛ"
    assert lines[4] == "[cccccccc] Plain Local Ⓛ"


def test_compact_favorites_header_grammar():
    out = _render_anchor_tree_raw_text(
        _tree_for_render(),
        global_count=2, local_count=3, show_local=True,
        compact=True, favorites_only=True, favorite_count=2,
        total_dock_count=10,
    )
    hdr = out.splitlines()[0]
    # shown = 2 + 3 = 5; K = 5 − 2 = 3; hidden = 10 − 5 = 5.
    assert hdr == (
        "Anchor Docks — 5 shown: 2 ⭐ favorites + 3 local via favorite chain "
        "· 5 other Docks hidden (favorites_only=false to show) · [xxxxxxxx] "
        "= 8-char handle prefix · detail='full' for JSON"
    )
    # No chain clause when every shown Dock is a favorite.
    out2 = _render_anchor_tree_raw_text(
        _tree_for_render(),
        global_count=2, local_count=0, show_local=False,
        compact=True, favorites_only=True, favorite_count=2,
        total_dock_count=4,
    )
    hdr2 = out2.splitlines()[0]
    assert "2 ⭐ favorites" in hdr2
    assert "local via favorite chain" not in hdr2
    assert "2 other Docks hidden (favorites_only=false to show)" in hdr2


def test_compact_fallback_header_grammar():
    tree = _tree_for_render()
    out = _render_anchor_tree_raw_text(
        tree, global_count=2, local_count=2, show_local=True,
        compact=True, favorites_only=False, favorites_fallback=True,
        favorite_count=0, total_dock_count=4,
        favorite_tag_hint="#wfy-ns--fav",
    )
    hdr = out.splitlines()[0]
    assert hdr.startswith("Anchor Docks — no favorites configured: showing all ·")
    assert "4 shown (2 global · 2 local)" in hdr
    assert "tag a Dock #wfy-ns--fav to shortlist (tag.add, or dock.update favorite=true)" in hdr
    assert hdr.endswith("· [xxxxxxxx] = 8-char handle prefix · detail='full' for JSON")
    # Hidden-locals count clause in the fallback header (show_local False).
    out2 = _render_anchor_tree_raw_text(
        tree, global_count=2, local_count=3, show_local=False,
        compact=True, favorites_fallback=True,
        favorite_tag_hint="#convrisk-docks--star",
    )
    hdr2 = out2.splitlines()[0]
    assert "2 global · 3 local hidden (include_local=true to show)" in hdr2
    assert "#convrisk-docks--star" in hdr2


def test_compact_header_without_favorites_is_todays_text():
    out = _render_anchor_tree_raw_text(
        _tree_for_render(),
        global_count=1, local_count=1, show_local=True, compact=True,
    )
    assert out.splitlines()[0] == (
        "Anchor Docks — 2 shown (1 global · 1 local) · [xxxxxxxx] = 8-char "
        "handle prefix · detail='full' for JSON"
    )


def test_legacy_render_ignores_the_new_kwargs_byte_identical():
    # §9.2: call legacy mode WITH favorites kwargs and assert the byte-locked
    # legacy string (the same lock test_compact_inventory_views carries).
    tree = {
        "root_uuid": "ROOT",
        "root_name": "Root",
        "children": [
            {
                "id": "1126f2b0-aaaa-bbbb-cccc-dddddddddddd",
                "name": "System Architecture",
                "is_local": False,
                "is_favorite": True,   # even a favorite renders legacy-identical
                "children": [
                    {
                        "id": "0fd089ea-1111-2222-3333-444444444444",
                        "name": "NEXUS architecture",
                        "is_local": False,
                        "is_favorite": False,
                        "children": [],
                    }
                ],
            },
            {
                "id": "77a69aa8-5555-6666-7777-888888888888",
                "name": "Local sub-dock",
                "is_local": True,
                "is_favorite": True,
                "children": [],
            },
        ],
    }
    expected = (
        "Anchors (5 global; 2 local shown)\n"
        "UUID                                  Anchor\n"
        "------------------------------------  ------\n"
        "1126f2b0-aaaa-bbbb-cccc-dddddddddddd  System Architecture\n"
        "\n"
        "0fd089ea-1111-2222-3333-444444444444    NEXUS architecture\n"
        "\n"
        "77a69aa8-5555-6666-7777-888888888888  Local sub-dock  [local]\n"
    )
    assert _render_anchor_tree_raw_text(
        tree, global_count=5, local_count=2, show_local=True,
        favorites_only=True, favorites_fallback=True, favorite_count=99,
        total_dock_count=999, favorite_tag_hint="#wfy-ns--fav",
    ) == expected


# ---------------------------------------------------------------------------
# _anchor_dock_list_impl — the public default lives HERE once
# ---------------------------------------------------------------------------

def _stub_internal(monkeypatch, result=None):
    seen = []

    async def _internal(**kwargs):
        seen.append(kwargs)
        return result if result is not None else {
            "success": True,
            "sessions": [],
            "anchor_tree": {"root_uuid": None, "root_name": "", "children": []},
            "session_count": 0,
            "local_anchor_count": 0,
            "favorites_only": bool(kwargs.get("favorites_only")),
            "favorites_fallback": False,
            "favorite_count": 0,
            "total_dock_count": 0,
            "shown_count": 0,
            "shown_global_count": 0,
            "shown_local_count": 0,
            "favorite_tag_hint": "#wfy-ns--fav",
        }

    monkeypatch.setattr(server, "_list_anchors_internal", _internal)
    return seen


def test_dock_list_impl_defaults_favorites_only_true(monkeypatch):
    seen = _stub_internal(monkeypatch)
    asyncio.run(server._anchor_dock_list_impl())
    assert seen[0]["favorites_only"] is True
    # Explicit false is forwarded.
    asyncio.run(server._anchor_dock_list_impl(favorites_only=False))
    assert seen[1]["favorites_only"] is False


def test_dock_list_impl_full_rows_carry_is_favorite(monkeypatch):
    _stub_internal(monkeypatch, result={
        "success": True,
        "sessions": [
            {"id": "D1", "name": "Starred", "is_local": False, "is_favorite": True},
            {"id": "D2", "name": "Plain", "is_local": False, "is_favorite": False},
        ],
        "favorites_only": True,
        "favorites_fallback": False,
        "favorite_count": 1,
    })
    result = asyncio.run(server._anchor_dock_list_impl(detail="full"))
    assert result["docks"][0]["is_favorite"] is True
    assert result["docks"][1]["is_favorite"] is False
    assert result["docks"][0]["dock_handle"] == "D1"
    # The echo keys ride along on the full result.
    assert result["favorites_only"] is True and result["favorite_count"] == 1


def test_dock_list_impl_compact_header_uses_echoed_shown_counts(monkeypatch):
    # Under favorites the OLD session_count − local_anchor_count arithmetic is
    # wrong (starred locals show with include_local=False); the impl must use
    # the echoed shown counts.
    _stub_internal(monkeypatch, result={
        "success": True,
        "sessions": [],
        "anchor_tree": {"root_uuid": None, "root_name": "", "children": [
            {"id": "dddddddd-0000-0000-0000-000000000000", "name": "Starred Local",
             "is_local": True, "is_favorite": True, "children": []},
        ]},
        "session_count": 99,          # deliberately wrong for the old arithmetic
        "local_anchor_count": 98,
        "favorites_only": True,
        "favorites_fallback": False,
        "favorite_count": 1,
        "total_dock_count": 5,
        "shown_count": 1,
        "shown_global_count": 0,
        "shown_local_count": 1,
        "favorite_tag_hint": "#wfy-ns--fav",
    })
    out = asyncio.run(server._anchor_dock_list_impl())
    hdr = out.splitlines()[0]
    assert "1 shown: 1 ⭐ favorites" in hdr
    assert "4 other Docks hidden (favorites_only=false to show)" in hdr
    assert out.splitlines()[2] == "[dddddddd] Starred Local ⭐ Ⓛ"


def test_registry_omitted_param_uses_impl_default_and_false_forwards(monkeypatch):
    # Omitted → the kwarg is ABSENT (the impl default is the single authority);
    # explicit false → forwarded. Examples validate against the schema.
    calls = []

    async def _impl(*args, **kwargs):
        calls.append(kwargs)
        return "RENDERED"

    monkeypatch.setattr(server, "_anchor_dock_list_impl", _impl)
    registry = server.build_anchor_registry()
    out = asyncio.run(dispatch_operation(registry, "dock.list", {}))
    assert out == "RENDERED"
    assert "favorites_only" not in calls[0]        # omitted → impl default
    asyncio.run(dispatch_operation(registry, "dock.list", {"favorites_only": False}))
    assert calls[1]["favorites_only"] is False
    asyncio.run(dispatch_operation(registry, "dock.list", {"favorites_only": True}))
    assert calls[2]["favorites_only"] is True
    spec = registry.resolve("dock.list")
    assert "favorites_only" in spec.params_schema["properties"]
    examples = registry.describe_full("dock.list")["examples"]
    for example in examples:
        validate_params(spec, example["call"].get("params") or {})
    assert any(
        example["call"]["params"] == {"favorites_only": False}
        for example in examples
    )


def test_legacy_list_anchors_compact_passes_favorites_only_false(monkeypatch):
    # The legacy tool keeps the FULL inventory (§6.5.1/§10): its compact path
    # must NOT inherit the impl's favorites default.
    calls = []

    async def _impl(*args, **kwargs):
        calls.append(kwargs)
        return "LEGACY"

    monkeypatch.setattr(server, "_anchor_dock_list_impl", _impl)
    out = asyncio.run(server.list_anchors())
    assert out == "LEGACY"
    assert calls[0]["favorites_only"] is False


# ---------------------------------------------------------------------------
# _handle_list_anchors_for_widget — absent flag → False; true forwarded
# ---------------------------------------------------------------------------

class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


def _widget_env(monkeypatch):
    client = make_boundary_capable(object.__new__(server.WorkFlowyClient), nodes=[])
    monkeypatch.setattr(server, "get_client", lambda: client)
    seen = []

    async def _internal(**kwargs):
        seen.append(kwargs)
        return {"success": True, "sessions": []}

    monkeypatch.setattr(server, "_list_anchors_internal", _internal)
    return seen


def test_widget_endpoint_absent_flag_defaults_false(monkeypatch):
    seen = _widget_env(monkeypatch)
    ws = _FakeWebSocket()
    asyncio.run(server._handle_list_anchors_for_widget(
        {"request_id": "r1", "show_local_anchors": True}, ws
    ))
    assert seen[0]["favorites_only"] is False
    assert ws.sent[0]["action"] == "list_anchors_for_widget_result"
    assert ws.sent[0]["request_id"] == "r1"


def test_widget_endpoint_forwards_true(monkeypatch):
    seen = _widget_env(monkeypatch)
    ws = _FakeWebSocket()
    asyncio.run(server._handle_list_anchors_for_widget(
        {"request_id": "r2", "favorites_only": True}, ws
    ))
    assert seen[0]["favorites_only"] is True


# ---------------------------------------------------------------------------
# dock.peek — is_favorite_dock rows, ⚓ ⭐ marker, Key line (§6.8)
# ---------------------------------------------------------------------------

def _peek_env(monkeypatch, nodes, reply=None):
    config = (
        ["wfy-ns"], [], None,
        reply if reply is not None else {
            "note_session_local_postfixes": ["local"],
            "note_session_favorite_postfixes": ["star"],   # NON-default word
            "note_session_deprecated_postfixes": ["retired"],
        },
    )

    async def _config(_tab):
        return config

    monkeypatch.setattr(server, "_resolve_note_session_config", _config)

    async def _resolve(value, **_kw):
        return value

    monkeypatch.setattr(server, "_resolve_generic_node_handle", _resolve)
    cache = {n["id"]: n for n in nodes}
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: cache)


def test_peek_marks_favorite_child_docks(monkeypatch):
    # NON-default favorite word ("star"): a site still using the default
    # 'fav' would classify the starred child as a plain node and fail here.
    nodes = [
        _n("D", "Root Dock #wfy-ns", None, 0),
        _n("F", "Starred Child #wfy-ns--star", "D", 0),
        _n("LF", "Starred Local #wfy-ns--local #wfy-ns--star", "D", 1),
        _n("P", "Plain Child", "D", 2),
        _n("R", "Retired Dock #wfy-ns--retired", "D", 3),
    ]
    _peek_env(monkeypatch, nodes)
    full = asyncio.run(server._anchor_dock_peek_impl("D", detail="full"))
    by_id = {row["uuid"]: row for row in full["rows"]}
    assert by_id["F"]["is_favorite_dock"] is True and by_id["F"]["is_dock"] is True
    assert by_id["LF"]["is_favorite_dock"] is True and by_id["LF"]["is_local_dock"] is True
    assert by_id["P"]["is_favorite_dock"] is False
    # A --retired-only child is NOT a Dock (no ⚓, not favorite).
    assert by_id["R"]["is_dock"] is False and by_id["R"]["is_favorite_dock"] is False

    compact = asyncio.run(server._anchor_dock_peek_impl("D"))
    lines = compact.splitlines()
    assert any("Key: ⚓ Dock (recursion stops here) · ⭐ favorite Dock · Ⓛ local Dock · ✓ completed" in ln for ln in lines)
    f_line = next(ln for ln in lines if "Starred Child" in ln)
    assert f_line.endswith("⚓ ⭐")
    lf_line = next(ln for ln in lines if "Starred Local" in ln)
    assert lf_line.endswith("⚓ ⭐ Ⓛ")


def test_peek_marks_default_word_favorite_child(monkeypatch):
    # The §9.2 literal case: a #wfy-ns--fav child under the DEFAULT words
    # (the non-default-word variant above proves the vocabulary threading).
    nodes = [
        _n("D", "Root Dock #wfy-ns", None, 0),
        _n("F", "Fav Child #wfy-ns--fav", "D", 0),
        _n("P", "Plain Child", "D", 1),
    ]
    _peek_env(monkeypatch, nodes, reply={"note_session_local_postfixes": ["local"]})
    full = asyncio.run(server._anchor_dock_peek_impl("D", detail="full"))
    by_id = {row["uuid"]: row for row in full["rows"]}
    assert by_id["F"]["is_favorite_dock"] is True
    assert by_id["P"]["is_favorite_dock"] is False
    compact = asyncio.run(server._anchor_dock_peek_impl("D"))
    f_line = next(ln for ln in compact.splitlines() if "Fav Child" in ln)
    assert f_line.endswith("⚓ ⭐")


# ---------------------------------------------------------------------------
# Thread-through — guard / touch / nearest-ancestor with NON-default words
# ---------------------------------------------------------------------------

def test_root_guard_refuses_favorite_only_and_allows_deprecated_only(monkeypatch):
    # NON-default words: favorite "star", deprecated "retired".
    config = (
        ["wfy-ns"], [], None,
        {
            "note_session_local_postfixes": ["local"],
            "note_session_favorite_postfixes": ["star"],
            "note_session_deprecated_postfixes": ["retired"],
        },
    )

    async def _config(_tab):
        return config

    monkeypatch.setattr(server, "_resolve_note_session_config", _config)
    cache = {
        "F": {"id": "F", "name": "Favorite Dock #wfy-ns--star"},
        "R": {"id": "R", "name": "Retired Node #wfy-ns--retired"},
    }
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: cache)

    async def _resolve_with_cache(handle, *, label, required=True):
        return handle, cache

    monkeypatch.setattr(
        server, "_resolve_generic_node_handle_with_cache", _resolve_with_cache
    )

    with pytest.raises(server._AnchorDockRootDetected):
        asyncio.run(server._anchor_dock_root_guard(
            "F", target_tab=None, tool_label="test"
        ))
    # A --deprecated-only node is NOT a Dock: the guard passes it through.
    out = asyncio.run(server._anchor_dock_root_guard(
        "R", target_tab=None, tool_label="test"
    ))
    assert out["resolved"] == "R"


def test_touch_refuses_favorite_only_dock(monkeypatch):
    # NON-default favorite word: touch's Dock refusal must see #wfy-ns--star
    # as a Dock (a site still passing only local words would try to WRITE it).
    config = (
        ["wfy-ns"], [], None,
        {
            "note_session_local_postfixes": ["local"],
            "note_session_favorite_postfixes": ["star"],
        },
    )

    async def _config(_tab):
        return config

    monkeypatch.setattr(server, "_resolve_note_session_config", _config)

    async def _resolve(value, **_kw):
        return value

    monkeypatch.setattr(server, "_resolve_generic_node_handle", _resolve)
    cache = {"F": {"id": "F", "name": "Favorite Dock #wfy-ns--star", "note": ""}}
    monkeypatch.setattr(server, "_anchor_cache_by_id", lambda: cache)
    monkeypatch.setattr(
        server, "_classify_node_map_state", lambda *_a: {"state": "unmapped"}
    )
    monkeypatch.setattr(server, "_rate_limiter", None)
    writes = []

    class _Client:
        async def get_node(self, node_id):
            writes.append(node_id)
            raise AssertionError("a refused Dock row must never be written")

    monkeypatch.setattr(server, "get_client", lambda: _Client())
    ledger = asyncio.run(server._anchor_point_touch_impl(["F"]))
    row = ledger["results"][0]
    assert row["status"] == "skipped"
    assert row["reason"] == "anchor_dock_refused"
    assert writes == []


def test_nearest_anchor_dock_ancestor_finds_favorite_and_skips_deprecated():
    # Pure walk with NON-default words: a #wfy-ns--star-only ancestor IS the
    # nearest Dock; a #wfy-ns--retired-only ancestor is skipped (not a Dock).
    nodes = {
        "TOP": {"id": "TOP", "name": "Top Dock #wfy-ns", "parent_id": None},
        "FAV": {"id": "FAV", "name": "Favorite #wfy-ns--star", "parent_id": "TOP"},
        "RET": {"id": "RET", "name": "Retired #wfy-ns--retired", "parent_id": "FAV"},
        "LEAF": {"id": "LEAF", "name": "Leaf item", "parent_id": "RET"},
        "PLAIN": {"id": "PLAIN", "name": "Plain node", "parent_id": "TOP"},
        "LEAF2": {"id": "LEAF2", "name": "Leaf two", "parent_id": "PLAIN"},
    }
    kwargs = {
        "local_postfixes": ["local"],
        "favorite_postfixes": ["star"],
        "deprecated_postfixes": ["retired"],
    }
    # LEAF's nearest Dock ancestor: RET is skipped (deprecated-only) → FAV.
    origin = server.nearest_anchor_dock_ancestor(nodes, "LEAF", ["wfy-ns"], **kwargs)
    assert origin["uuid"] == "FAV" and origin["is_dock"] is True
    # LEAF2: PLAIN is not a Dock → TOP.
    origin2 = server.nearest_anchor_dock_ancestor(nodes, "LEAF2", ["wfy-ns"], **kwargs)
    assert origin2["uuid"] == "TOP" and origin2["is_dock"] is True


# ---------------------------------------------------------------------------
# dock.update — the §6.6.4 truth table (real _node_edit_core + MagicMock
# client, the test_update_locality_canonicalizes… pattern). Words default.
# ---------------------------------------------------------------------------

def _update_env(monkeypatch, live_name, reply=None, bases=("wfy-ns", "convrisk-docks")):
    config = (
        list(bases), [], None,
        reply if reply is not None else {"note_session_local_postfixes": ["local"]},
    )

    async def _config(_tab):
        return config

    monkeypatch.setattr(server, "_resolve_note_session_config", _config)

    async def _resolve(value, **_kw):
        return REGION_DOCK if value == "D" else value

    monkeypatch.setattr(server, "_resolve_generic_node_handle", _resolve)
    monkeypatch.setattr(
        server, "_anchor_cache_by_id",
        lambda: {REGION_DOCK: {"id": REGION_DOCK, "name": live_name}},
    )
    monkeypatch.setattr(
        server, "_classify_node_map_state", lambda *_a: {"state": "unmapped"}
    )
    monkeypatch.setattr(server, "_rate_limiter", None)

    async def _resolve_mutator(node_id, *, label, required=True, role=None):
        return node_id

    monkeypatch.setattr(server, "_resolve_mutator_node_id", _resolve_mutator)

    client = MagicMock()
    client.get_node = AsyncMock(return_value=WorkFlowyNode(id=REGION_DOCK, name=live_name))
    client.update_node = AsyncMock(return_value=WorkFlowyNode(id=REGION_DOCK))
    monkeypatch.setattr(server, "get_client", lambda: client)
    return client


# (live tags, call kwargs, expected final tag list OR an error code)
UPDATE_TABLE = [
    (["#wfy-ns"], {"local": True}, ["#wfy-ns--local"]),
    (["#wfy-ns--local"], {"favorite": True}, ["#wfy-ns--local", "#wfy-ns--fav"]),
    (["#wfy-ns--local", "#wfy-ns--fav"], {"favorite": False}, ["#wfy-ns--local"]),
    (["#wfy-ns--local", "#wfy-ns--fav"], {"local": False}, ["#wfy-ns--fav"]),
    (["#wfy-ns--fav"], {"favorite": False}, ["#wfy-ns"]),
    (["#wfy-ns--local"], {"local": False}, ["#wfy-ns"]),
    (["#wfy-ns", "#other", "#wfy-ns--local"], {"local": True},
     ["#wfy-ns--local", "#other"]),
    (["#wfy-ns--fav", "#wfy-ns--fav"], {"favorite": True}, ["#wfy-ns--fav"]),
    (["#wfy-ns--nested"], {"favorite": True}, ["#wfy-ns--nested", "#wfy-ns--fav"]),
    (["#convrisk-docks", "#other"], {"local": True},
     ["#convrisk-docks--local", "#other"]),
    (["#wfy-ns", "#wfy-ns--deprecated"], {"favorite": True},
     ["#wfy-ns--fav", "#wfy-ns--deprecated"]),
    (["#wfy-ns", "#convrisk-docks"], {"favorite": True}, "dock_families_ambiguous"),
    (["#wfy-ns", "#convrisk-docks"],
     {"favorite": True, "families": "convrisk-docks"},
     ["#wfy-ns", "#convrisk-docks--fav"]),
    (["#wfy-ns--local", "#convrisk-docks"],
     {"favorite": True, "families": ["wfy-ns", "convrisk-docks"]},
     ["#wfy-ns--local", "#wfy-ns--fav", "#convrisk-docks--fav"]),
    (["#wfy-ns"], {"favorite": True, "families": "convrisk-docks"},
     "dock_family_not_on_node"),
    (["#wfy-ns--local", "#convrisk-docks--deprecated"],
     {"favorite": True, "families": "convrisk-docks"}, "dock_family_not_on_node"),
    (["#wfy-ns--fav", "#wfy-ns--expand"], {"favorite": False},
     ["#wfy-ns", "#wfy-ns--expand"]),
    (["#wfy-ns--local"], {"local": True}, "no_op"),
]

# Row 9 needs a SECOND configured local word.
_ROW_REPLY = {
    8: {"note_session_local_postfixes": ["local", "nested"]},
}


@pytest.mark.parametrize("index", range(len(UPDATE_TABLE)))
def test_dock_update_truth_table(monkeypatch, index):
    live_tags, call, expected = UPDATE_TABLE[index]
    live_name = "Topic " + " ".join(live_tags)
    client = _update_env(monkeypatch, live_name, reply=_ROW_REPLY.get(index))
    if isinstance(expected, str) and expected.startswith("dock_"):
        with pytest.raises(server._AnchorOperationError) as excinfo:
            asyncio.run(server._anchor_dock_update_impl("D", **call))
        assert excinfo.value.code == expected
        assert "families_present" in (excinfo.value.extra or {})
        client.update_node.assert_not_called()   # zero writes on error (§7)
        return
    result = asyncio.run(server._anchor_dock_update_impl("D", **call))
    if expected == "no_op":
        assert result["no_op"] is True
        client.update_node.assert_not_called()
        return
    assert result["no_op"] is False
    assert client.update_node.await_count == 1
    request = client.update_node.call_args[0][1]
    assert request.name == "Topic " + " ".join(expected)
    assert result["tags"] == expected


def test_dock_update_echo_keys_and_favorite_only_call(monkeypatch):
    # A favorite-only call (no local) sets ONE attribute and echoes the
    # recomputed verdict (§6.6.5).
    client = _update_env(monkeypatch, "Topic #wfy-ns--local")
    result = asyncio.run(server._anchor_dock_update_impl("D", favorite=True))
    assert result["local"] is True
    assert result["favorite"] is True
    assert result["attributes"] == ["favorite", "local"]   # sorted list
    assert result["families_updated"] == ["wfy-ns"]
    assert result["tags"] == ["#wfy-ns--local", "#wfy-ns--fav"]
    assert result["renamed"] is True
    # Unsetting the last attribute leaves the bare base and echoes it.
    client.update_node.reset_mock()
    client2 = _update_env(monkeypatch, "Topic #wfy-ns--fav")
    result2 = asyncio.run(server._anchor_dock_update_impl("D", favorite=False))
    assert result2["attributes"] == []
    assert result2["local"] is False and result2["favorite"] is False
    assert result2["tags"] == ["#wfy-ns"]


def test_dock_update_deprecated_only_target_is_refused(monkeypatch):
    # A --deprecated-only node is NOT a Dock (I4): refused, zero writes.
    client = _update_env(monkeypatch, "Topic #wfy-ns--deprecated")
    with pytest.raises(RuntimeError, match="not an Anchor Dock"):
        asyncio.run(server._anchor_dock_update_impl("D", favorite=True))
    client.update_node.assert_not_called()


def test_dock_update_families_shape_errors_are_loud_before_io(monkeypatch):
    client = _update_env(monkeypatch, "Topic #wfy-ns")
    # families with neither local nor favorite (topic-only call: the
    # nothing-to-do ValueError legitimately fires first when NOTHING is
    # requested, §6.6.1 order).
    with pytest.raises(server._AnchorOperationError) as excinfo:
        asyncio.run(server._anchor_dock_update_impl("D", topic="Topic", families="wfy-ns"))
    assert excinfo.value.code == "invalid_parameter_combination"
    # empty list / blank entry
    with pytest.raises(server._AnchorOperationError) as excinfo:
        asyncio.run(server._anchor_dock_update_impl("D", favorite=True, families=[]))
    assert excinfo.value.code == "invalid_parameter_combination"
    with pytest.raises(server._AnchorOperationError) as excinfo:
        asyncio.run(server._anchor_dock_update_impl("D", favorite=True, families=[" # "]))
    assert excinfo.value.code == "invalid_parameter_combination"
    client.update_node.assert_not_called()


def test_dock_update_nothing_to_do_message_names_favorite(monkeypatch):
    _update_env(monkeypatch, "Topic #wfy-ns")
    with pytest.raises(ValueError, match="topic, description, local and/or favorite"):
        asyncio.run(server._anchor_dock_update_impl("D"))


def test_dock_update_uses_configured_favorite_word_not_the_default(monkeypatch):
    # NON-default favorite word 'star': setting favorite emits #wfy-ns--star;
    # an existing --star tag is preserved (word kept) and unsettable.
    reply = {
        "note_session_local_postfixes": ["local"],
        "note_session_favorite_postfixes": ["star"],
    }
    client = _update_env(monkeypatch, "Topic #wfy-ns", reply=reply)
    result = asyncio.run(server._anchor_dock_update_impl("D", favorite=True))
    assert result["tags"] == ["#wfy-ns--star"]
    client.update_node.reset_mock()
    client = _update_env(monkeypatch, "Topic #wfy-ns--star", reply=reply)
    result = asyncio.run(server._anchor_dock_update_impl("D", favorite=False))
    assert result["tags"] == ["#wfy-ns"]
    assert result["favorite"] is False


def test_dock_update_never_switches_families(monkeypatch):
    # The §6.6.2 bug, locked: a convrisk-docks Dock updated with local=true
    # stays a convrisk-docks Dock (the old transform canonicalised to
    # base_tags[0] and silently switched the family).
    client = _update_env(monkeypatch, "Topic #convrisk-docks")
    result = asyncio.run(server._anchor_dock_update_impl("D", local=True))
    assert result["tags"] == ["#convrisk-docks--local"]
    request = client.update_node.call_args[0][1]
    assert "wfy-ns" not in request.name


# ---------------------------------------------------------------------------
# dock.update — leading-emoji-run preservation on a topic rename
# (Z8Q0PF + LITF2F, ruling C609U1 A2). Default: the existing leading run
# survives exactly like tags and the AP-ID; replace_emoji=True hands the
# run to the supplied topic wholesale (its run wins; its absence clears).
# ---------------------------------------------------------------------------


def test_dock_update_topic_preserves_leading_emoji(monkeypatch):
    client = _update_env(monkeypatch, "📋 Topic #wfy-ns")
    result = asyncio.run(
        server._anchor_dock_update_impl("D", topic="New Topic")
    )
    assert result["renamed"] is True
    request = client.update_node.call_args[0][1]
    assert request.name == "📋 New Topic #wfy-ns"
    assert result["new_name"] == "📋 New Topic #wfy-ns"


def test_dock_update_replace_emoji_true_clears_run(monkeypatch):
    client = _update_env(monkeypatch, "📋 Topic #wfy-ns")
    result = asyncio.run(
        server._anchor_dock_update_impl("D", topic="New Topic", replace_emoji=True)
    )
    assert result["renamed"] is True
    request = client.update_node.call_args[0][1]
    assert request.name == "New Topic #wfy-ns"


def test_dock_update_supplied_emoji_wins_explicitly(monkeypatch):
    client = _update_env(monkeypatch, "📋 Topic #wfy-ns")
    result = asyncio.run(
        server._anchor_dock_update_impl("D", topic="✅ New")
    )
    request = client.update_node.call_args[0][1]
    assert request.name == "✅ New #wfy-ns"
    assert result["new_name"] == "✅ New #wfy-ns"


def test_dock_update_topic_only_rename_to_same_composed_name_is_noop(monkeypatch):
    # Preservation makes 'New' on '📋 New' a TRUE no-op: the composed name
    # equals the current name, so no name write happens.
    client = _update_env(monkeypatch, "📋 Topic #wfy-ns")
    result = asyncio.run(server._anchor_dock_update_impl("D", topic="Topic"))
    assert result["no_op"] is True
    client.update_node.assert_not_called()


def test_dock_update_schema_declares_replace_emoji_default_false():
    registry = server.build_anchor_registry()
    spec = registry.resolve("dock.update")
    prop = spec.params_schema["properties"]["replace_emoji"]
    assert prop["type"] == "boolean"
    assert prop["default"] is False
    assert "leading emoji run" in spec.summary


# ---------------------------------------------------------------------------
# dock.create / create_note_session (§6.7)
# ---------------------------------------------------------------------------

def _make_create_client(monkeypatch, etch_capture):
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)

    async def _fake_export_nodes(node_id=None, use_cache=True, **kw):
        return {"nodes": [
            {"id": "PARENT", "name": "Anchor", "note": "", "parent_id": None},
        ]}

    c.export_nodes = _fake_export_nodes  # type: ignore[assignment]
    monkeypatch.setattr(
        nx, "_classify_node_map_state",
        lambda uuid, nodes_by_id: {"state": "unmapped", "file_path": None},
    )
    monkeypatch.setattr(nx, "_anchor_target_guard_error", lambda verdict: None)

    async def _fake_etch(parent_id, nodes, nodes_file=None):
        etch_capture["nodes"] = nodes
        return {"success": True, "nodes_created": 1,
                "root_node_ids": ["root-1"], "errors": []}

    c.workflowy_etch = _fake_etch  # type: ignore[assignment]
    return c


def test_create_note_session_attribute_tags(monkeypatch):
    etch = {}
    c = _make_create_client(monkeypatch, etch)

    def _create(**kw):
        return asyncio.run(c.create_note_session(
            parent_uuid="PARENT", description="Topic", detail="note",
            session_tags=["wfy-ns"], **kw,
        ))

    # favorite alone
    _create(favorite_postfix="fav")
    assert etch["nodes"][0]["name"] == "Topic #wfy-ns--fav"
    # local + favorite: TWO tags, order local then favorite
    _create(local_postfix="local", favorite_postfix="fav")
    assert etch["nodes"][0]["name"] == "Topic #wfy-ns--local #wfy-ns--fav"
    # neither: byte-identical to today
    _create()
    assert etch["nodes"][0]["name"] == "Topic #wfy-ns"
    # local only: byte-identical to today
    _create(local_postfix="local")
    assert etch["nodes"][0]["name"] == "Topic #wfy-ns--local"
    # extra tags are never postfixed
    _create(extra_tags=["keep"], favorite_postfix="fav")
    assert etch["nodes"][0]["name"] == "Topic #wfy-ns--fav #keep"
    # idempotence guard: a session tag already ending in the word is not
    # double-postfixed
    asyncio.run(c.create_note_session(
        parent_uuid="PARENT", description="Topic", detail="note",
        session_tags=["wfy-ns--fav"], favorite_postfix="fav",
    ))
    assert etch["nodes"][0]["name"] == "Topic #wfy-ns--fav"


def test_dock_create_impl_resolves_configured_favorite_word(monkeypatch):
    # NON-default favorite word: the impl passes the FIRST configured word.
    config = (
        ["wfy-ns"], [], "PARENT",
        {
            "note_session_local_postfixes": ["local"],
            "note_session_favorite_postfixes": ["star", "heart"],
        },
    )

    async def _config(_tab):
        return config

    monkeypatch.setattr(server, "_resolve_note_session_config", _config)
    seen = {}

    class _Client:
        async def create_note_session(self, **kwargs):
            seen.update(kwargs)
            return {"success": True, "nodes_created": 1, "root_node_ids": ["N"]}

    client = _Client()
    monkeypatch.setattr(server, "get_client", lambda: client)
    async def _resolve_parent(value, **_kw):
        return REGION_PARENT
    monkeypatch.setattr(server, "_resolve_generic_node_handle", _resolve_parent)
    asyncio.run(server._anchor_dock_create_impl(
        "Topic", "desc", local=True, favorite=True,
    ))
    assert seen["local_postfix"] == "local"
    assert seen["favorite_postfix"] == "star"
    # favorite=False -> None (byte-identical path)
    seen.clear()
    asyncio.run(server._anchor_dock_create_impl("Topic", "desc"))
    assert seen["favorite_postfix"] is None and seen["local_postfix"] is None


# ---------------------------------------------------------------------------
# dock.promote (§6.7)
# ---------------------------------------------------------------------------

def _promote_env(monkeypatch, cache_name="Plain", reply=None):
    config = (
        ["wfy-ns"], [], None,
        reply if reply is not None else {"note_session_local_postfixes": ["local"]},
    )

    async def _config(_tab):
        return config

    monkeypatch.setattr(server, "_resolve_note_session_config", _config)

    async def _resolve(value, **_kw):
        return value

    monkeypatch.setattr(server, "_resolve_generic_node_handle", _resolve)
    monkeypatch.setattr(
        server, "_anchor_cache_by_id",
        lambda: {"N": {"id": "N", "name": cache_name, "parent_id": None}},
    )
    monkeypatch.setattr(
        server, "_classify_node_map_state", lambda *_a: {"state": "unmapped"}
    )
    added = []

    async def _add(handle, tags):
        added.append((handle, tags))
        return {"success": True, "no_op": False, "tags_added": tags}

    monkeypatch.setattr(server, "_tag_add_impl", _add)
    return added


def test_promote_favorite_and_local_attribute_lists(monkeypatch):
    added = _promote_env(monkeypatch)
    result = asyncio.run(server._anchor_dock_promote_impl("N", favorite=True))
    assert result["success"] is True
    # ONE call with the LIST: favorite alone.
    assert added == [("N", ["#wfy-ns--fav"])]

    added.clear()
    asyncio.run(server._anchor_dock_promote_impl("N", local=True, favorite=True))
    assert added == [("N", ["#wfy-ns--local", "#wfy-ns--fav"])]

    added.clear()
    asyncio.run(server._anchor_dock_promote_impl("N"))
    assert added == [("N", ["#wfy-ns"])]


def test_promote_already_dock_with_favorite_is_noop_and_teaches_update(monkeypatch):
    added = _promote_env(monkeypatch, cache_name="Dock #wfy-ns")
    result = asyncio.run(server._anchor_dock_promote_impl("N", favorite=True))
    assert result["no_op"] is True
    assert added == []                       # zero writes
    assert "use dock.update(favorite=true) to add an attribute" in result["warnings"][0]
    # Without favorite the warning text is unchanged from today.
    result2 = asyncio.run(server._anchor_dock_promote_impl("N"))
    assert result2["no_op"] is True
    assert result2["warnings"] == ["already an Anchor Dock"]


def test_promote_uses_configured_favorite_word(monkeypatch):
    added = _promote_env(monkeypatch, reply={
        "note_session_local_postfixes": ["local"],
        "note_session_favorite_postfixes": ["star"],
    })
    asyncio.run(server._anchor_dock_promote_impl("N", favorite=True))
    assert added == [("N", ["#wfy-ns--star"])]


# ---------------------------------------------------------------------------
# Registry describe (DoD 7): dock.update shows favorite + families + the two
# new error codes; create/promote schemas carry favorite.
# ---------------------------------------------------------------------------

def test_registry_describe_discloses_the_favorite_surface():
    registry = server.build_anchor_registry()
    update = registry.describe_full("dock.update")
    props = update["params_schema"]["properties"]
    assert "favorite" in props and props["favorite"]["type"] == ["boolean", "null"]
    assert "families" in props
    assert props["families"]["type"] == ["string", "array", "null"]
    assert props["families"]["items"] == {"type": "string"}
    codes = registry.resolve("dock.update").domain_error_codes
    assert "dock_families_ambiguous" in codes
    assert "dock_family_not_on_node" in codes
    assert "favorite" in registry.resolve("dock.create").params_schema["properties"]
    assert "favorite" in registry.resolve("dock.promote").params_schema["properties"]
    # The family summary teaches the tag family (one sentence, §6.10).
    summary = registry.family_summary
    assert "Dock tags are a family" in summary
    assert "#‹base›--deprecated is in the family but is NOT a Dock" in summary
    # Every dock.update example validates against the schema.
    spec = registry.resolve("dock.update")
    for example in registry.describe_full("dock.update")["examples"]:
        validate_params(spec, example["call"].get("params") or {})


# ---------------------------------------------------------------------------
# [ap:YLHDIN] part 2 -- tree nodes carry the description; compact rows show
# its first line
# ---------------------------------------------------------------------------


def test_tree_nodes_carry_description_and_compact_rows_show_first_line():
    # Give two Docks notes (the fixture helper returns fresh dicts).
    nodes = make_nodes()
    for n in nodes:
        if n["id"] == "ARCH":
            n["note"] = "How the daemon is wired.\nSecond line: implementation details."
        if n["id"] == "MGR":
            n["note"] = "Connective hub; its own first line."

    result = list_sessions(nodes)
    rows = {nid: node for nid, _depth, node in tree_rows(result["anchor_tree"])}
    # The SAME metadata-stripped text the structured rows carry.
    assert (
        rows["ARCH"]["description"]
        == "How the daemon is wired.\nSecond line: implementation details."
    )
    assert rows["MGR"]["description"] == "Connective hub; its own first line."

    out = _render_anchor_tree_raw_text(
        result["anchor_tree"],
        global_count=len(result["sessions"]),
        local_count=0,
        show_local=False,
        compact=True,
    )
    arch_line = next(
        line for line in out.splitlines() if "Architecture Hub" in line
    )
    assert " — How the daemon is wired." in arch_line
    assert "Second line" not in out
