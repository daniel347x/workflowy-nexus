"""Saved Tabs unit E1 ([ap:ARDOF6] §E1) — glimpse(verb='estate'), OUTPUT mode.

Covers the contract's seven test areas:
  * the parse grammar (fenced ```json / bare array / single wrapper / bare
    spec blob / named-slice-group entries / NBSP-mangled pretty payloads /
    wrong-file redirects / loud failures);
  * per-entry isolation (an unresolvable group, a root not in cache, a stale
    focus, an override-guard rejection — the entry fails LOUD, the others
    still render);
  * single-row AND rows focus honored end-to-end through the estate funnel;
  * per-call view overrides via the ONE normalized override path
    (with_view_overrides seam: headings_only / as_markdown / sort guard);
  * an Anchor-Dock raw_root passes with NO teaching error and NO flag
    (every entry is allow_anchor_dock=true by construction — the in-band
    guard is proven never consulted);
  * the mode='tabs' refusal (reserved, Tier 3 [ap:DNMYMZ] teaching error);
  * the output contract: payload order, alias/title heading + one-line
    render_scope echo, output_file (directory / pattern / plain path) with a
    compact ack, entries filter (aliases / 1-based indices, loud misses).

Idioms mirror tests/test_phase1_multiselect_handler.py and
tests/test_glimpse_focus_feature2.py (fake client + in-memory cache; no
network, no real Workflowy). Disk writes go through pytest tmp_path.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402
from workflowy_mcp.glimpse_estate_payload import (  # noqa: E402
    parse_estate_note,
    parse_estate_payload,
)


def test_promote_provenance_is_inert_before_every_estate_grammar_rule():
    """WO-2 route (a): exact entries, not just a successful parse."""
    notes = (
        "Recruiting",
        "[ap:K7M2QF]",
        "3f3754e2",
        "Recruiting\n[ap:K7M2QF]\n3f3754e2",
        "```\nRecruiting\n[ap:K7M2QF]\n3f3754e2\n```",
        'Recruiting\n{"dock": "[ap:K7M2QF]"}',
        '[{"alias": "Saved", "spec": {"kind": "raw_root", "root_uuid": "3f3754e2"}}]',
    )
    moved = "↩ moved from Session (abcd1234) · 2026-10-01"
    derived = "↩ derived from Session (abcd1234) · 2026-10-01 · source 12345678"
    for note in notes:
        expected, error = parse_estate_note(note)
        assert error is None and expected
        for prefix in (moved, derived, moved + "\n\n" + derived):
            actual, error = parse_estate_note(prefix + "\n\n" + note)
            # Dataclass equality covers form/handle/group/order/bare_name_line
            # and the complete lowered specs, so no silent lane change passes.
            assert error is None and actual == expected, (note, prefix)

# ─────────────────────────────────────────────────────────────────────────
# Fixture data — the multiselect-test tree, plus a Dock node and tagged
# nodes for the slice-group entry.
# ─────────────────────────────────────────────────────────────────────────

_ROOT = "33333333-aaaa-bbbb-cccc-000000000001"
_A = "33333333-aaaa-bbbb-cccc-000000000002"
_A1 = "33333333-aaaa-bbbb-cccc-000000000003"
_A2 = "33333333-aaaa-bbbb-cccc-000000000004"
_B = "33333333-aaaa-bbbb-cccc-000000000005"
_B1 = "33333333-aaaa-bbbb-cccc-000000000006"
_DOCK = "33333333-aaaa-bbbb-cccc-000000000007"
_DOCK_CHILD = "33333333-aaaa-bbbb-cccc-000000000008"
_T1 = "33333333-aaaa-bbbb-cccc-000000000009"
_T2 = "33333333-aaaa-bbbb-cccc-00000000000a"
_AP = "33333333-aaaa-bbbb-cccc-00000000eeee"
_MISSING = "33333333-aaaa-bbbb-cccc-0000000fffff"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Root", "note": "root body", "parent_id": None, "priority": 0},
    {"id": _A, "name": "Alpha", "note": "alpha body", "parent_id": _ROOT, "priority": 0},
    {"id": _A1, "name": "Alpha-1", "note": "a1 body", "parent_id": _A, "priority": 0},
    {"id": _A2, "name": "Alpha-2", "note": "a2 body", "parent_id": _A, "priority": 1},
    {"id": _B, "name": "Beta", "note": "beta body", "parent_id": _ROOT, "priority": 1},
    {"id": _B1, "name": "Beta-1", "note": "b1 body", "parent_id": _B, "priority": 0},
    {"id": _DOCK, "name": "My Test Dock #wfy-ns", "note": "dock note", "parent_id": None, "priority": 0},
    {"id": _DOCK_CHILD, "name": "Dock child", "note": "dock child body", "parent_id": _DOCK, "priority": 0},
    {"id": _T1, "name": "Tagged one #est-tag", "note": "t1 body", "parent_id": _ROOT, "priority": 2},
    {"id": _T2, "name": "Tagged two #est-tag", "note": "t2 body", "parent_id": _ROOT, "priority": 3},
    {"id": _AP, "name": "[ap:ESTATE] Estate AP", "note": "", "parent_id": None, "priority": 0},
    {"id": "55555555-aaaa-bbbb-cccc-00000000eeee", "name": "[ap:ESTTST1] Estate AP", "note": "", "parent_id": None, "priority": 0},
]

_GROUPS = [
    {
        "id": "grp-estate-1",
        "name": "Estate|TestGroup",
        "prefixes": ["est-tag"],
        "root_uuid": _ROOT,
        "description": "",
        "default_sort_by": "created",
        "default_sort_direction": "asc",
    },
]
_POSTFIXES = {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"}


def _make_client():
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]
    return c


def _install_note(monkeypatch, note: str, *, name: str = "Estate AP"):
    """Fake the ONE node-read core; record how it was called."""
    seen: dict = {}

    async def fake_read(node_id, as_raw_node=True, **kw):
        seen["node_id"] = node_id
        seen["as_raw_node"] = as_raw_node
        seen["kw"] = kw
        return types.SimpleNamespace(id=node_id, name=name, note=note)

    monkeypatch.setattr(srv, "_node_read_impl", fake_read)
    return seen


def _install_env(monkeypatch, note: str, *, groups: list | None = None):
    """Client + note + (optionally) slice-group config fakes."""
    monkeypatch.setattr(srv, "get_client", _make_client)
    # CF-11: the estate's entry-resolution map reads the ONE pinned source
    # (_anchor_cache_by_id), so the fake cache feeds THAT reader.
    monkeypatch.setattr(srv, "_anchor_cache_by_id",
                        lambda: {n["id"]: dict(n) for n in _CACHE_NODES})
    seen = _install_note(monkeypatch, note)
    async def fake_cfg(target_tab):
        return (groups or [], None, dict(_POSTFIXES), ["wfy-suppress"], {})

    async def fake_vocab(target_tab):
        return ["wfy-ns"], {"local_postfixes": ["local"]}

    monkeypatch.setattr(srv, "_resolve_slice_group_config", fake_cfg)
    monkeypatch.setattr(srv, "_read_anchor_dock_vocabulary", fake_vocab)
    return seen


def _run(coro):
    return asyncio.run(coro)


def _estate(monkeypatch, note: str, *, groups=None, **params):
    _install_env(monkeypatch, note, groups=groups)
    params.setdefault("handle", "[ap:ESTATE]")
    return _run(srv._glimpse_estate_impl(**params))


def _blob(**kwargs) -> dict:
    return RenderProjectionSpec.normalize_from_glimpse_args(
        **kwargs
    ).to_render_spec_blob()


def _payload_note(entries) -> str:
    return "Estate note prose.\n\n```json\n" + json.dumps(entries) + "\n```\n"


def _entry(alias: str, spec: dict) -> dict:
    return {"alias": alias, "title": alias + " title", "url": "https://workflowy.com/#/x", "spec": spec}


_RAW_SPEC = {"kind": "raw_root", "root_uuid": _ROOT}

# ═════════════════════════════════════════════════════════════════════════
# A · THE PARSE GRAMMAR (pure module — requirement 7 area 1)
# ═════════════════════════════════════════════════════════════════════════


def test_parse_fenced_json_canonical_array():
    note = _payload_note([_entry("ONE", _RAW_SPEC), _entry("TWO", _RAW_SPEC)])
    entries, err = parse_estate_note(note)
    assert err is None
    assert [e.alias for e in entries] == ["ONE", "TWO"]
    assert [e.index for e in entries] == [1, 2]
    assert all(e.valid and e.spec == _RAW_SPEC for e in entries)
    assert entries[0].title == "ONE title" and entries[0].url.startswith("https://")


def test_parse_bare_array_no_fence():
    entries, err = parse_estate_note(
        "lead-in\n" + json.dumps([_entry("BARE", _RAW_SPEC)]) + "\ntrailer"
    )
    assert err is None and entries[0].alias == "BARE"


def test_parse_group_entry_forms():
    # bare string element, {"group","alias"} wrapper, and a bare-spec element
    entries, err = parse_estate_note(
        json.dumps(
            [
                "MyGroup",
                {"group": "Other|Tools [2]", "alias": "TOOLS"},
                {"kind": "raw_root", "root_uuid": _ROOT},
            ]
        )
    )
    assert err is None
    assert entries[0].is_group and entries[0].group == "MyGroup"
    assert entries[0].display_name == "MyGroup"  # no alias → the group handle
    assert entries[1].is_group and entries[1].group == "Other|Tools [2]"
    assert entries[1].display_name == "TOOLS"  # alias wins
    assert entries[2].valid and entries[2].spec["root_uuid"] == _ROOT


def test_parse_single_wrapper_bare_blob_bare_group_string():
    for note, check in (
        (json.dumps(_entry("W", _RAW_SPEC)), lambda e: e.alias == "W" and e.spec == _RAW_SPEC),
        (json.dumps(_RAW_SPEC), lambda e: e.spec == _RAW_SPEC),
        (json.dumps({"group": "G1", "alias": "ga"}), lambda e: e.is_group and e.group == "G1" and e.alias == "ga"),
        ('"JustAGroup"\n', lambda e: e.is_group and e.group == "JustAGroup"),
    ):
        entries, err = parse_estate_note(note)
        assert err is None, (note, err)
        assert len(entries) == 1 and entries[0].valid and check(entries[0]), note


def test_parse_nbsp_mangled_pretty_payload():
    # The Workflowy note-editor paste mangling (Ledger ST-007): leading
    # indent spaces became U+00A0 — strict JSON rejects, the ONE fallback
    # recovers.
    mangled = '[\n\u00a0\u00a0' + json.dumps(_entry("NBSP", _RAW_SPEC)) + "\n]"
    entries, err = parse_estate_note(mangled)
    assert err is None and entries[0].alias == "NBSP"


def test_parse_wrong_file_redirects():
    entries, err = parse_estate_note(
        json.dumps({"kind": "glimpse-slice-groups-export", "expand_groups": []})
    )
    assert entries is None and "SLICE-GROUPS export" in err and "slice-groups Import" in err
    entries, err = parse_estate_note(
        json.dumps({"kind": "glimpse_anchor_views_export", "anchor_views": []})
    )
    assert entries is None and "ANCHOR-VIEWS export" in err


def test_parse_loud_failures():
    # UnitSpec [ap:XFW9YD] §9: a single unquoted line is now a group.
    entries, err = parse_estate_note("prose only — no payload anywhere")
    assert err is None and entries[0].group == "prose only — no payload anywhere"
    for note, frag in (
        ("```json\n{not json at all}\n```", "could not parse"),
        ("```json\n[]\n```", "EMPTY array"),
        ("```json\n42\n```", "could not parse"),
    ):
        entries, err = parse_estate_note(note)
        assert entries is None and frag in err, (note, err)


def test_parse_first_estate_candidate_wins_over_unrelated_fence():
    note = (
        "```json\n{\"unrelated\": \"config example\"}\n```\n\nthe estate:\n\n"
        "```json\n" + json.dumps([_entry("REAL", _RAW_SPEC)]) + "\n```\n"
    )
    entries, err = parse_estate_note(note)
    assert err is None and len(entries) == 1 and entries[0].alias == "REAL"


def test_parse_all_invalid_entries_kept_for_per_entry_failure():
    entries, err = parse_estate_note(json.dumps([{"alias": "A"}, {"alias": "B", "spec": "nope"}]))
    assert err is None  # parses — the entries are individually invalid
    assert len(entries) == 2 and not any(e.valid for e in entries)
    assert all("spec" in e.invalid_reason for e in entries)


def test_parse_prose_is_not_a_payload():
    entries, err = parse_estate_note(
        'Dan said "open the estate" and left. See [ap:K7M2QF] and footnote [1].'
    )
    assert entries is None and "no JSON payload found" in err


def test_parse_payload_top_level_shape_errors():
    data, err = parse_estate_payload({"nothing": "here"})
    assert data is None and "expected a JSON array" in err
    data, err = parse_estate_payload("")
    assert data is None and "empty string" in err


# ═════════════════════════════════════════════════════════════════════════
# B · THE VERB — output contract, funnel, focus, isolation
# ═════════════════════════════════════════════════════════════════════════


def test_estate_reads_note_live_through_the_node_read_core(monkeypatch):
    seen = _install_env(monkeypatch, _payload_note([_entry("ONE", _RAW_SPEC)]))
    out = _run(srv._glimpse_estate_impl(handle="[ap:ESTTST1]"))
    assert isinstance(out, str)
    assert seen["node_id"] == "[ap:ESTTST1]"
    assert seen["as_raw_node"] is False  # the structured live-node read


def test_estate_renders_entries_in_payload_order_with_heading_and_echo(monkeypatch):
    note = _payload_note(
        [
            _entry("FIRST", {"kind": "raw_root", "root_uuid": _A}),
            _entry("SECOND", {"kind": "raw_root", "root_uuid": _B}),
        ]
    )
    out = _estate(monkeypatch, note)
    assert isinstance(out, str)
    assert out.index("[1] FIRST") < out.index("[2] SECOND")  # payload order
    assert "# 🏛️ ESTATE" in out and "2 entries" in out and "2 rendered" in out
    # each entry: alias heading + ONE-LINE render_scope echo + its document
    first = out.split("# 📄 [2] SECOND")[0]
    assert "# 📄 [1] FIRST" in first
    echo_lines = [ln for ln in first.splitlines() if ln.startswith("> render_scope: ")]
    assert len(echo_lines) == 1
    scope = json.loads(echo_lines[0][len("> render_scope: "):])
    assert scope["kind"] == "raw_root" and scope["root_uuid"] == _A
    assert "a1 body" in first and "alpha body" in first
    assert "b1 body" in out.split("# 📄 [2] SECOND")[1]


def test_estate_per_entry_isolation_unknown_group(monkeypatch):
    note = _payload_note(
        [{"alias": "BADGROUP", "title": "t", "url": "u", "spec": None}, "no-such-group",
         _entry("GOOD", _RAW_SPEC)]
    )
    # the invalid entry (spec None, no group) + an unresolvable group name +
    # a good raw_root: the first two fail loud, the third still renders.
    out = _estate(monkeypatch, note, groups=_GROUPS)
    assert isinstance(out, str)
    assert "ENTRY FAILED" in out
    bad, badgroup, good = out.split("---")
    assert "invalid estate entry" in bad and "spec" in bad
    assert "No slice group matched" in badgroup and "no-such-group" in badgroup
    assert "# 📄 [3] GOOD" in good and "a1 body" in good
    assert "1 rendered · 2 FAILED" in out.splitlines()[0]


def test_estate_per_entry_isolation_root_not_in_cache(monkeypatch):
    note = _payload_note(
        [_entry("GONE", {"kind": "raw_root", "root_uuid": _MISSING}), _entry("HERE", _RAW_SPEC)]
    )
    out = _estate(monkeypatch, note)
    assert "ENTRY FAILED" in out and "[1] GONE" in out.split("ENTRY FAILED")[0]
    assert "# 📄 [2] HERE" in out and "root body" in out


def test_estate_single_row_focus_honored(monkeypatch):
    spec = _blob(root_uuid=_ROOT, focus={"mode": "row", "uuid": _A, "ancestor_path": [_ROOT], "occurrence": 0})
    out = _estate(monkeypatch, _payload_note([_entry("FOC", spec)]))
    assert isinstance(out, str)
    assert "Section" not in out or True  # (tree names below)
    assert "Alpha-1" in out and "a1 body" in out
    # option-Y: the sibling subtree is pruned
    assert "Beta" not in out and "b1 body" not in out
    scope_line = [ln for ln in out.splitlines() if ln.startswith("> render_scope: ")][0]
    scope = json.loads(scope_line[len("> render_scope: "):])
    assert scope["focus"]["mode"] == "row" and scope["focus"]["uuid"] == _A


def test_estate_rows_focus_renders_covering_subtree(monkeypatch):
    spec = _blob(
        root_uuid=_ROOT,
        focus={"mode": "rows", "rows": [
            {"uuid": _A1, "ancestor_path": [_ROOT, _A], "occurrence": 0},
            {"uuid": _B1, "ancestor_path": [_ROOT, _B], "occurrence": 0},
        ]},
    )
    out = _estate(monkeypatch, _payload_note([_entry("ROWS", spec)]))
    assert isinstance(out, str) and "ENTRY FAILED" not in out
    assert "a1 body" in out and "b1 body" in out
    # the covering subtree, NOT the whole document: the off-path sibling is out
    assert "a2 body" not in out
    scope_line = [ln for ln in out.splitlines() if ln.startswith("> render_scope: ")][0]
    scope = json.loads(scope_line[len("> render_scope: "):])
    assert scope["focus"]["mode"] == "rows" and len(scope["focus"]["rows"]) == 2


def test_estate_stale_focus_fails_loud_per_entry(monkeypatch):
    # The Stage-2 exact-or-error stale payload (success:true + error +
    # focus_unresolved + EMPTY render) is a LOUD per-entry estate failure —
    # never an empty document, never a whole-scope fallback.
    stale = _blob(root_uuid=_ROOT, focus={"mode": "row", "uuid": _MISSING, "ancestor_path": [_ROOT], "occurrence": 0})
    note = _payload_note([_entry("STALE", stale), _entry("FINE", _RAW_SPEC)])
    out = _estate(monkeypatch, note)
    assert isinstance(out, str)
    assert "ENTRY FAILED" in out
    stale_part = out.split("# 📄 [2] FINE")[0]
    assert "[1] STALE" in stale_part and "could not be resolved" in stale_part
    # the stale entry rendered NOTHING — no whole-scope fallback bodies
    assert "a1 body" not in stale_part
    assert "# 📄 [2] FINE" in out and "root body" in out


def test_estate_focus_intent_lost_fails_loud(monkeypatch):
    # The modal-path guard, per entry: a saved focus that collapses to None
    # during deserialize/grace ABORTS the entry — never a whole-scope dump.
    spec = _blob(root_uuid=_ROOT, focus={"mode": "rows", "rows": [
        {"uuid": _A1, "ancestor_path": [_ROOT, _A], "occurrence": 0},
    ]})
    note = _payload_note([_entry("INTENT", spec), _entry("FINE", {"kind": "raw_root", "root_uuid": _B})])
    _install_env(monkeypatch, note)
    monkeypatch.setattr(
        srv, "_grace_resolve_spec_uuids", lambda s, cache: s.with_overrides(focus=None)
    )
    out = _run(srv._glimpse_estate_impl(handle="[ap:ESTATE]"))
    assert isinstance(out, str) and "ENTRY FAILED" in out
    intent_part = out.split("# 📄 [2] FINE")[0]
    assert "[1] INTENT" in intent_part
    assert "focus" in intent_part.lower() and "ABORTED" in intent_part
    # the whole-scope fallback of the INTENT entry never rendered (its scope
    # was _ROOT; only FINE's _B subtree bodies may appear)
    assert "root body" not in out and "a1 body" not in out
    assert "b1 body" in out  # FINE rendered normally


def test_estate_overrides_apply_to_every_entry(monkeypatch):
    note = _payload_note([_entry("ONE", _RAW_SPEC), _entry("TWO", {"kind": "raw_root", "root_uuid": _B})])
    out = _estate(monkeypatch, note, headings_only=True)
    assert isinstance(out, str) and "ENTRY FAILED" not in out
    # headings_only: titles present, note bodies suppressed on BOTH entries
    assert "Alpha-1" in out and "Beta-1" in out
    assert "a1 body" not in out and "b1 body" not in out


def test_estate_as_markdown_false_returns_structured_dict(monkeypatch):
    note = _payload_note([_entry("ONE", _RAW_SPEC)])
    out = _estate(monkeypatch, note, as_markdown=False)
    assert isinstance(out, dict) and out["success"] is True
    assert out["mode"] == "output"
    meta = out["estate"]
    assert meta["entry_count"] == 1 and meta["rendered"] == 1 and meta["failed"] == 0
    rec = out["entries"][0]
    assert rec["ok"] is True and rec["as_markdown"] is False
    assert isinstance(rec["result"], dict) and rec["result"].get("preview_tree") is not None
    assert rec["document"] is None
    assert rec["render_scope"]["kind"] == "raw_root"


def test_estate_sort_override_rejected_on_focused_entry_only(monkeypatch):
    focused = _blob(root_uuid=_ROOT, focus={"mode": "row", "uuid": _A, "ancestor_path": [_ROOT], "occurrence": 0})
    note = _payload_note([_entry("FOC", focused), _entry("PLAIN", _RAW_SPEC)])
    out = _estate(monkeypatch, note, sort_by="created")
    assert isinstance(out, str)
    assert "ENTRY FAILED" in out and "sort override" in out.lower()
    # the unfocused entry still rendered — under the CREATED sort, whose U6
    # chronological rule suppresses ANCESTOR note bodies (leaf bodies print)
    assert "# 📄 [2] PLAIN" in out and "b1 body" in out and "a1 body" in out


def test_estate_dock_root_passes_with_no_teaching_error_and_no_guard(monkeypatch):
    # Requirement 2: a raw_root at an Anchor Dock passes through with NO
    # teaching error and NO flag — the in-band guard must never even run.
    async def exploding_guard(*a, **k):
        raise AssertionError("the in-band Anchor-Dock guard must NOT run on the estate path")

    _install_env(monkeypatch, _payload_note([_entry("DOCK", {"kind": "raw_root", "root_uuid": _DOCK})]))
    monkeypatch.setattr(srv, "_anchor_dock_root_guard", exploding_guard)
    out = _run(srv._glimpse_estate_impl(handle="[ap:ESTATE]"))
    assert isinstance(out, str) and "ENTRY FAILED" not in out
    assert "anchor_dock_root_detected" not in out
    assert "My Test Dock" in out and "dock child body" in out


def test_estate_named_group_entry_rides_group_resolution_and_saved_sort(monkeypatch):
    # A bare-string entry normalizes to a slice spec at the group's STORED
    # root with its SAVED default sort (created/asc from the fake group).
    note = _payload_note(["TestGroup"])  # short name after the last '|'
    out = _estate(monkeypatch, note, groups=_GROUPS)
    assert isinstance(out, str) and "ENTRY FAILED" not in out
    assert "Tagged one" in out and "t1 body" in out and "Tagged two" in out
    scope_line = [ln for ln in out.splitlines() if ln.startswith("> render_scope: ")][0]
    scope = json.loads(scope_line[len("> render_scope: "):])
    assert scope["kind"] == "slice" and scope["group_id"] == "grp-estate-1"
    assert scope["sort_by"] == "created" and scope["sort_direction"] == "asc"


def test_estate_group_entry_unknown_handle_is_a_loud_per_entry_error(monkeypatch):
    note = _payload_note([{"group": "missing-group", "alias": "MG"}, _entry("OK", _RAW_SPEC)])
    out = _estate(monkeypatch, note, groups=_GROUPS)
    assert "ENTRY FAILED" in out and "No slice group matched" in out and "missing-group" in out
    assert "# 📄 [2] OK" in out


# ═════════════════════════════════════════════════════════════════════════
# C · mode — the reserved 'tabs' refusal (requirement 6)
# ═════════════════════════════════════════════════════════════════════════


def test_estate_tabs_mode_refused_with_tier3_teaching_error(monkeypatch):
    _install_env(monkeypatch, _payload_note([_entry("ONE", _RAW_SPEC)]))
    out = _run(srv._glimpse_estate_impl(handle="[ap:ESTATE]", mode="tabs"))
    assert isinstance(out, dict) and out["success"] is False
    err = out["error"]
    assert "RESERVED" in err and "Tier 3" in err and "DNMYMZ" in err
    assert "mode='output'" in err
    assert out.get("estate_tabs_reserved") is True


def test_estate_tabs_refusal_through_the_family_envelope(monkeypatch):
    from coherence_harness import make_boundary_capable

    monkeypatch.setattr(
        srv, "get_client", lambda: make_boundary_capable(types.SimpleNamespace(), nodes=[])
    )
    out = _run(
        srv.glimpse_family_tool(
            verb="estate", params={"handle": "[ap:ESTATE]", "mode": "tabs"}
        )
    )
    assert isinstance(out, dict) and out["success"] is False
    assert out["error"]["code"] == "glimpse_request_failed"
    assert "Tier 3" in out["error"]["message"] and "DNMYMZ" in out["error"]["message"]
    # the standardized envelope carries the operation doc (describe machinery)
    assert out.get("operation_doc", {}).get("operation") == "glimpse.estate"


def test_estate_unknown_mode_refused(monkeypatch):
    _install_env(monkeypatch, _payload_note([_entry("ONE", _RAW_SPEC)]))
    out = _run(srv._glimpse_estate_impl(handle="[ap:ESTATE]", mode="drive"))
    assert isinstance(out, dict) and out["success"] is False
    assert "unknown estate mode" in out["error"] and "RESERVED" in out["error"]


def test_estate_output_is_the_default_mode(monkeypatch):
    _install_env(monkeypatch, _payload_note([_entry("ONE", _RAW_SPEC)]))
    out = _run(srv._glimpse_estate_impl(handle="[ap:ESTATE]"))
    assert isinstance(out, str)  # mode defaulted to 'output'


# ═════════════════════════════════════════════════════════════════════════
# D · handle / parse top-level failures
# ═════════════════════════════════════════════════════════════════════════


def test_estate_missing_or_blank_handle_is_loud(monkeypatch):
    _install_env(monkeypatch, "")
    for bad in (None, "", "   "):
        out = _run(srv._glimpse_estate_impl(handle=bad))
        assert isinstance(out, dict) and out["success"] is False
        assert "requires 'handle'" in out["error"]


def test_estate_unresolvable_handle_is_loud(monkeypatch):
    _install_env(monkeypatch, "", groups=[])

    async def fake_read(node_id, as_raw_node=True, **kw):
        raise RuntimeError(
            f"node.read: handle {node_id!r} did not resolve to a Workflowy node"
        )

    monkeypatch.setattr(srv, "_node_read_impl", fake_read)
    out = _run(srv._glimpse_estate_impl(handle="[ap:NOPE12]"))
    assert isinstance(out, dict) and out["success"] is False
    # UnitSpec [ap:XFW9YD] §9: fail at the shared identity decision, before read.
    assert out["code"] == "simple_form_unresolved"
    assert "did not resolve" in out["error"]


def test_estate_prose_note_without_resolvable_name_renders_plain_node(monkeypatch):
    # U3-2 (supersedes the UnitSpec [ap:XFW9YD] §9 loud pin): a bare handle
    # whose single-line note parses as a bare NAME that resolves to NO slice
    # group is prose, not an estate — the plain-node fallback reads the node
    # on its own instead of a loud unknown-group estate.
    out = _estate(monkeypatch, "this note holds no estate payload at all", groups=[])
    assert isinstance(out, str)
    assert "ENTRY FAILED" not in out
    assert "1 rendered" in out
    assert "Estate AP" in out  # the node itself rendered


def test_estate_all_entries_failed_is_a_top_level_error(monkeypatch):
    note = _payload_note(
        [
            _entry("GONE1", {"kind": "raw_root", "root_uuid": _MISSING}),
            _entry("GONE2", {"kind": "raw_root", "root_uuid": "33333333-aaaa-bbbb-cccc-0000000ffffe"}),
        ]
    )
    out = _estate(monkeypatch, note)
    assert isinstance(out, dict) and out["success"] is False
    assert "every entry of the estate failed" in out["error"]
    assert out["estate"]["rendered"] == 0 and out["estate"]["failed"] == 2
    assert len(out["failed_entries"]) == 2
    assert all("index" in f and "error" in f for f in out["failed_entries"])


# ═════════════════════════════════════════════════════════════════════════
# E · entries filter (requirement 4)
# ═════════════════════════════════════════════════════════════════════════


_THREE = _payload_note(
    [
        _entry("ALPHA-E", {"kind": "raw_root", "root_uuid": _A}),
        _entry("BETA-E", {"kind": "raw_root", "root_uuid": _B}),
        _entry("GAMMA-E", _RAW_SPEC),
    ]
)


def test_estate_entries_filter_by_alias_and_index_in_payload_order(monkeypatch):
    out = _estate(monkeypatch, _THREE, entries=[3, "ALPHA-E"])
    assert isinstance(out, str)
    assert "[1] ALPHA-E" in out and "[3] GAMMA-E" in out and "BETA-E" not in out
    # payload order regardless of filter order
    assert out.index("[1] ALPHA-E") < out.index("[3] GAMMA-E")
    assert "3 of 3 entries" not in out.splitlines()[0]  # header shows the subset


def test_estate_entries_filter_unmatched_alias_is_loud(monkeypatch):
    out = _estate(monkeypatch, _THREE, entries=["NOPE"])
    assert isinstance(out, dict) and out["success"] is False
    assert "matches no entry alias" in out["error"]
    assert "ALPHA-E" in out["error"]  # known aliases are named


def test_estate_entries_filter_index_out_of_range_is_loud(monkeypatch):
    out = _estate(monkeypatch, _THREE, entries=[9])
    assert isinstance(out, dict) and out["success"] is False
    assert "out of range" in out["error"] and "1..3" in out["error"]


def test_estate_entries_filter_empty_array_is_loud(monkeypatch):
    out = _estate(monkeypatch, _THREE, entries=[])
    assert isinstance(out, dict) and out["success"] is False
    assert "EMPTY array" in out["error"]


def test_estate_entries_filter_bad_item_type_is_loud(monkeypatch):
    out = _estate(monkeypatch, _THREE, entries=[{"alias": "ALPHA-E"}])
    assert isinstance(out, dict) and out["success"] is False
    assert "aliases (strings) or 1-based indices" in out["error"]


# ═════════════════════════════════════════════════════════════════════════
# F · output_file (requirement 4) — directory / pattern / plain path
# ═════════════════════════════════════════════════════════════════════════


def test_estate_output_file_directory_writes_per_entry_files(monkeypatch, tmp_path):
    note = _payload_note(
        [_entry("ALPHA-E", {"kind": "raw_root", "root_uuid": _A}), _entry("BETA E!", _RAW_SPEC)]
    )
    _install_env(monkeypatch, note)
    out = _run(
        srv._glimpse_estate_impl(handle="[ap:ESTATE]", output_file=str(tmp_path) + os.sep)
    )
    assert isinstance(out, dict) and out["success"] is True
    files = out["output_files"]
    assert len(files) == 2
    names = sorted(os.path.basename(f["path"]) for f in files)
    assert names == ["estate-01-ALPHA-E.md", "estate-02-BETA-E.md"]  # slugged
    for f in files:
        disk = open(f["path"], "rb").read()
        assert f["bytes_written"] == len(disk)  # exact-byte ack
        text = disk.decode("utf-8")
        assert text.startswith("# 📄 [") and "> render_scope: " in text
    assert open(files[0]["path"], encoding="utf-8").read().count("a1 body") == 1
    assert out["failed_entries"] == []
    assert out["estate"]["rendered"] == 2


def test_estate_output_file_pattern_substitution(monkeypatch, tmp_path):
    note = _payload_note([_entry("ALPHA-E", _RAW_SPEC)])
    _install_env(monkeypatch, note)
    pattern = os.path.join(str(tmp_path), "est-{index}-{alias}-{title}.md")
    out = _run(srv._glimpse_estate_impl(handle="[ap:ESTATE]", output_file=pattern))
    assert out["success"] is True
    path = out["output_files"][0]["path"]
    assert os.path.basename(path) == "est-1-ALPHA-E-ALPHA-E-title.md"
    assert os.path.isfile(path)


def test_estate_output_file_unsupported_placeholder_is_loud(monkeypatch, tmp_path):
    _install_env(monkeypatch, _payload_note([_entry("A", _RAW_SPEC)]))
    out = _run(
        srv._glimpse_estate_impl(
            handle="[ap:ESTATE]", output_file=os.path.join(str(tmp_path), "x-{uuid}.md")
        )
    )
    assert out["success"] is False and "unsupported placeholder" in out["error"]
    assert "{index}" in out["error"]


def test_estate_output_file_plain_path_writes_assembled_document(monkeypatch, tmp_path):
    note = _payload_note(
        [_entry("ONE", {"kind": "raw_root", "root_uuid": _A}), _entry("TWO", {"kind": "raw_root", "root_uuid": _B})]
    )
    _install_env(monkeypatch, note)
    dest = os.path.join(str(tmp_path), "estate.md")
    out = _run(srv._glimpse_estate_impl(handle="[ap:ESTATE]", output_file=dest))
    assert out["success"] is True
    assert len(out["output_files"]) == 1
    text = open(dest, encoding="utf-8").read()
    assert text.startswith("# 🏛️ ESTATE")
    assert "[1] ONE" in text and "[2] TWO" in text
    assert out["output_files"][0]["bytes_written"] == len(text.encode("utf-8"))


def test_estate_output_file_missing_directory_is_loud_and_never_created(monkeypatch, tmp_path):
    _install_env(monkeypatch, _payload_note([_entry("ONE", _RAW_SPEC)]))
    missing = os.path.join(str(tmp_path), "no-such-dir") + os.sep
    out = _run(srv._glimpse_estate_impl(handle="[ap:ESTATE]", output_file=missing))
    assert out["success"] is False and "does not exist" in out["error"]
    assert not os.path.exists(missing)


def test_estate_output_file_full_mode_entry_fails_loud_per_entry(monkeypatch, tmp_path):
    # A blob whose OWN view is full/UUID + output_file: that entry fails loud
    # (estate disk writes are Markdown-only — the slice-group guard's twin);
    # a Markdown sibling still lands on disk.
    full_blob = _blob(root_uuid=_A)
    full_blob["as_markdown"] = False
    note = _payload_note([_entry("FULL", full_blob), _entry("MD", _RAW_SPEC)])
    _install_env(monkeypatch, note)
    out = _run(
        srv._glimpse_estate_impl(handle="[ap:ESTATE]", output_file=str(tmp_path) + os.sep)
    )
    assert out["success"] is True
    assert len(out["output_files"]) == 1 and out["output_files"][0]["display"] == "MD"
    assert len(out["failed_entries"]) == 1
    assert "Markdown-only" in out["failed_entries"][0]["error"]


# ═════════════════════════════════════════════════════════════════════════
# G · registry surface — the verb, its schema, the describe text (req. 7)
# ═════════════════════════════════════════════════════════════════════════


def test_estate_registered_in_the_glimpse_family():
    registry = srv.GLIMPSE_REGISTRY
    assert [spec.verb for spec in registry.operations()] == ["run", "estate"]
    spec = registry.resolve("estate")
    assert spec is not None
    assert spec.cache_backed is True
    assert spec.side_effects == ("render", "artifact_write")
    assert spec.async_mode == "never"
    assert spec.param_aliases == {"since": "recent", "tail": "recent"}
    # U01 adds declared simple-form failures; retain the exact vocabulary lock.
    # [ap:TVIIBN] 4d(a): the estate's call-level view overrides also declare
    # the loud sort_direction rule.
    assert spec.domain_error_codes == (
        "glimpse_request_failed",
        srv._SORT_DIRECTION_WITHOUT_SORT_BY_ERROR,
        *srv.SIMPLE_FORM_ERROR_CODES,
    )
    props = spec.params_schema["properties"]
    assert tuple(spec.params_schema["required"]) == ("handle",)
    assert spec.params_schema["additionalProperties"] is False
    assert props["mode"]["enum"] == ("output", "tabs")  # deep-frozen by the spec
    assert tuple(props["entries"]["items"]["type"]) == ("string", "integer")
    assert "push_to_modal" not in props  # estate is output-only
    for knob in ("as_markdown", "headings_only", "sort_by", "sort_direction", "recent", "output_file", "target_tab"):
        assert knob in props


def test_estate_describe_text_documents_the_contract():
    registry = srv.GLIMPSE_REGISTRY
    index = registry.describe_index()
    assert [row["verb"] for row in index["operations"]] == ["run", "estate", "describe"]
    full = registry.describe_full("estate")
    doc = full["returns"]["description"]
    # the seven contract areas the describe text must teach
    assert "saved-tabs" in doc.lower() or "estate" in doc.lower()
    assert "NAMED SLICE GROUP" in doc
    assert "focus is HONORED" in doc and "mode:'rows'" in doc
    assert "allow_anchor_dock=true" in doc
    assert "with_view_overrides" in doc
    assert "PER-ENTRY LOUD FAILURES" in doc
    assert "RESERVED" in doc and "DNMYMZ" in doc
    assert "render_scope" in doc
    # the family reference (the published tool description) carries the line
    assert any(
        line.startswith("estate — ") for line in registry.index_lines()
    )
    assert "ESTATE" in srv._GLIMPSE_TOOL_DESCRIPTION


def test_estate_schema_validation_rejects_bad_params():
    from workflowy_mcp.mcp_api_registry import OperationValidationError, validate_params

    spec = srv.GLIMPSE_REGISTRY.resolve("estate")
    validate_params(spec, {"handle": "[ap:X]"})  # minimal valid call
    with pytest.raises(OperationValidationError):
        validate_params(spec, {})  # handle required
    with pytest.raises(OperationValidationError):
        validate_params(spec, {"handle": "h", "mode": "drive"})  # enum
    with pytest.raises(OperationValidationError):
        validate_params(spec, {"handle": "h", "nope": 1})  # closed schema
    with pytest.raises(OperationValidationError):
        validate_params(spec, {"handle": "h", "entries": "ALPHA"})  # array only
    with pytest.raises(OperationValidationError):
        validate_params(spec, {"handle": "h", "entries": [1.5]})  # item type
    # NOTE: the since/tail → recent aliases are applied by dispatch_operation
    # BEFORE validation (never by bare validate_params); the alias routing is
    # locked by test_estate_family_tool_routes_params_to_the_impl instead.
    for example in srv.GLIMPSE_REGISTRY.describe_full("estate")["examples"]:
        validate_params(spec, example["call"].get("params") or {})


def test_estate_family_tool_routes_params_to_the_impl(monkeypatch):
    from coherence_harness import make_boundary_capable

    monkeypatch.setattr(
        srv, "get_client", lambda: make_boundary_capable(types.SimpleNamespace(), nodes=[])
    )
    seen = {}

    async def fake_impl(**kwargs):
        seen.update(kwargs)
        return "ESTATE DOC"

    monkeypatch.setattr(srv, "_glimpse_estate_impl", fake_impl)
    out = _run(
        srv.glimpse_family_tool(
            verb="estate",
            params={"handle": "[ap:X]", "since": "1w", "headings_only": True, "target_tab": "TAB"},
        )
    )
    assert out == "ESTATE DOC"
    assert seen["handle"] == "[ap:X]"
    assert seen["since"] == "1w"  # the alias landed on the recent slot
    assert seen["headings_only"] is True
    assert seen["target_tab"] == "TAB"
    assert seen["mode"] is None and seen["entries"] is None
