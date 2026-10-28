"""Regression: render_symbols per-item ``expand`` (ap:EXPSWP / [ap:IMPEXP]).

SPEC v1 [ap:EXPSWP]: a Lightning Rod ``render_symbols`` ``symbols`` element may
be a plain string (today's behavior) OR an object
``{"uuid": "HANDLE", "expand": true}`` / ``{"symbol": "NAME", "expand": true}``.
A cleanly-resolved flagged item's ENTIRE cache subtree is swept into the
Stage-1 member set (``collect_subtree_ids``, suppression-guarded) ->
``member_ids`` (resolved ∪ swept); the two call-site predicates (MCP +
docked-modal) test ``member_ids``. Locked semantics: expand-wins on a
plain+flagged duplicate; flagged-but-ambiguous/unresolved/out-of-scope/
all-suppressed is INERT; suppressed descendants excluded AT SWEEP TIME;
completed descendants pruned by Phase 1 at admit time; the spec/blob/echo
carry the VERBATIM request (D5) so every re-projection re-runs
normalize -> resolve -> sweep.

These tests lock:
  * the shared item normalizer (unwrap, defaults, expand-wins, loud errors);
  * ``collect_subtree_ids`` (basic / suppression pocket / dedupe / empty /
    completed-still-collected);
  * the projection sweep (member_ids vs resolved_ids, inert flagged items,
    report fields, empty-expand byte-identity);
  * the MCP end-to-end path (object elements render the swept subtree;
    suppressed + completed content absent);
  * the docked-modal end-to-end path (object elements ride the render_spec
    blob round-trip; the echo carries the verbatim object).

ap:APSYMB coverage (section 8): ``[ap:CORE]`` anchor-point handles straight in
the ``symbols`` list -- both unambiguous forms resolve; a duplicated AP-ID is
ambiguous; a no-match is unresolved; a BARE core is NOT treated as an anchor
point (grammar guard); the object form with ``expand`` sweeps the anchor
point's whole subtree; the beacon-mutation gate (``allow_anchor_points=False``
default) does NOT resolve AP-IDs; the scope gate applies; the blob round-trip
carries the verbatim AP-ID object.

All local: no network, no real Workflowy, no disk.
"""
import asyncio
import json
import os
import sys

import pytest  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402
from workflowy_mcp.f8_slice_policy import collect_subtree_ids  # noqa: E402
from workflowy_mcp.lightning_rod_mutation_spec import LightningRodMutationSpec  # noqa: E402


# --- a tiny in-memory /nodes-export cache -----------------------------------
# ROOT
# |-- F   "def foo()"          (the flagged function)
# |   |-- F1  "helper one"
# |   |   `-- F1A "deep leaf"
# |   |-- F2  "pocket #wfy-suppress"   (suppressed pocket)
# |   |   `-- F2A "suppressed secret"
# |   `-- FC  "done helper" (completed)
# |       `-- FC1 "done child"
# |-- G   "def bar()"
# |-- G2  "def bar() duplicate"        (ambiguity pair for 'ambigname')
# |-- AP  "[ap:K7M2QF] captured anchor point"  (ap:APSYMB)
# |   |-- AP1  "captured sub-note"
# |   |   `-- AP1A "deep captured leaf"
# |-- APD1 "[ap:DUPE12] duplicate anchor point one"  (duplicated AP-ID pair)
# `-- APD2 "[ap:DUPE12] duplicate anchor point two"
_ROOT = "eeeeeeee-1111-0000-0000-000000000001"
_F = "eeeeeeee-1111-0000-0000-000000000002"
_F1 = "eeeeeeee-1111-0000-0000-000000000003"
_F1A = "eeeeeeee-1111-0000-0000-000000000004"
_F2 = "eeeeeeee-1111-0000-0000-000000000005"
_F2A = "eeeeeeee-1111-0000-0000-000000000006"
_FC = "eeeeeeee-1111-0000-0000-000000000007"
_FC1 = "eeeeeeee-1111-0000-0000-000000000008"
_G = "eeeeeeee-1111-0000-0000-000000000009"
_G2 = "eeeeeeee-1111-0000-0000-00000000000a"
_AP = "eeeeeeee-1111-0000-0000-00000000000b"
_AP1 = "eeeeeeee-1111-0000-0000-00000000000c"
_AP1A = "eeeeeeee-1111-0000-0000-00000000000d"
_APD1 = "eeeeeeee-1111-0000-0000-00000000000e"
_APD2 = "eeeeeeee-1111-0000-0000-00000000000f"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Symbols Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _F, "name": "def foo()", "note": "body of foo", "parent_id": _ROOT, "priority": 0},
    {"id": _F1, "name": "helper one", "note": "body one", "parent_id": _F, "priority": 0},
    {"id": _F1A, "name": "deep leaf", "note": "deep body", "parent_id": _F1, "priority": 0},
    {"id": _F2, "name": "pocket #wfy-suppress", "note": "pocket body", "parent_id": _F, "priority": 1},
    {"id": _F2A, "name": "suppressed secret", "note": "secret body", "parent_id": _F2, "priority": 0},
    {"id": _FC, "name": "done helper", "note": "done body", "parent_id": _F, "priority": 2, "completed": True},
    {"id": _FC1, "name": "done child", "note": "done child body", "parent_id": _FC, "priority": 0},
    {"id": _G, "name": "def bar()", "note": "body of bar", "parent_id": _ROOT, "priority": 1},
    {"id": _G2, "name": "def bar() duplicate", "note": "body of bar2", "parent_id": _ROOT, "priority": 2},
    {"id": _AP, "name": "[ap:K7M2QF] captured anchor point", "note": "ap body", "parent_id": _ROOT, "priority": 3},
    {"id": _AP1, "name": "captured sub-note", "note": "sub-note body", "parent_id": _AP, "priority": 0},
    {"id": _AP1A, "name": "deep captured leaf", "note": "deep ap body", "parent_id": _AP1, "priority": 0},
    {"id": _APD1, "name": "[ap:DUPE12] duplicate anchor point one", "note": "dup one", "parent_id": _ROOT, "priority": 4},
    {"id": _APD2, "name": "[ap:DUPE12] duplicate anchor point two", "note": "dup two", "parent_id": _ROOT, "priority": 5},
]

_SUPPRESS = ["wfy-suppress"]


def _cache_by_id():
    return {n["id"]: dict(n) for n in _CACHE_NODES}


def _make_client():
    """A real WorkFlowyClientNexus (render methods intact) with a fake cache.

    ``read_uuid_from_symbol`` is only consulted for NON-UUID-shaped items:
    'ambigname' resolves to the TWO bar nodes (the ambiguous pair); anything
    else resolves nothing (the 'unresolved' bucket).
    """
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]

    async def _ruf(*, file_path=None, symbol=None, symbol_kind="auto"):
        if symbol == "ambigname":
            return {"candidates": [
                {"uuid": _G, "name": "def bar()"},
                {"uuid": _G2, "name": "def bar() duplicate"},
            ]}
        return {"candidates": []}

    c.read_uuid_from_symbol = _ruf
    from coherence_harness import make_boundary_capable

    return make_boundary_capable(c, nodes=_CACHE_NODES)


def _patch_client(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(srv, "get_client", lambda: client)
    return client


def _stub_suppress_config(monkeypatch, tags=None):
    """Stub the ONE WebSocket touch (global-suppress read) on the MCP path."""
    async def _cfg(_target_tab):
        # (groups, active_id, postfixes, suppress_tags, raw)
        return [], None, {}, list(tags or []), {}
    monkeypatch.setattr(srv, "_resolve_slice_group_config", _cfg)


# ===========================================================================
# 1. The shared item normalizer (_normalize_render_symbols_items)
# ===========================================================================
def test_normalizer_strings_passthrough():
    out = srv._normalize_render_symbols_items([" alpha ", "beta", "   "])
    assert out["ok"] is True
    assert out["handles"] == ["alpha", "beta"]            # stripped; blanks dropped
    assert out["expand_handles"] == set()
    assert out["raw"] == ["alpha", "beta"]                # echo == historical behavior


def test_normalizer_object_unwrap_and_expand_default_false():
    src = {"uuid": "u1"}
    out = srv._normalize_render_symbols_items([src, {"symbol": "s2", "expand": False}])
    assert out["ok"] is True
    assert out["handles"] == ["u1", "s2"]
    assert out["expand_handles"] == set()                 # absent/false = plain member
    assert out["raw"][0] == {"uuid": "u1"}                # object echoed verbatim ...
    assert out["raw"][0] is not src                       # ... but shallow-copied


def test_normalizer_expand_flag_and_expand_wins_on_dupe():
    out = srv._normalize_render_symbols_items(["u1", {"uuid": "u1", "expand": True}])
    assert out["ok"] is True
    assert out["handles"] == ["u1", "u1"]                 # duplicates kept (historical)
    assert out["expand_handles"] == {"u1"}                # expand WINS on the dupe


def test_normalizer_loud_errors():
    cases = [
        ([{"uuid": "u", "symbol": "s"}], "both"),                 # both keys
        ([{}], "neither"),                                        # neither key
        ([{"uuid": "   "}], "empty"),                             # empty handle
        ([{"symbol": ""}], "empty"),                              # empty handle
        ([{"uuid": "u", "expand": "yes"}], "boolean"),            # non-bool expand
        ([123], "must be a string or an object"),                 # non-str-non-dict
        ([{"uuid": "u", "expnad": True}], "unrecognized"),        # unknown key
    ]
    for raw, needle in cases:
        out = srv._normalize_render_symbols_items(raw)
        assert out["ok"] is False, (raw, out)
        assert needle in out["error"], (raw, out)


# ===========================================================================
# 2. collect_subtree_ids (the pure sweep helper)
# ===========================================================================
def test_collect_subtree_ids_basic_seeds_excluded():
    got = collect_subtree_ids(_cache_by_id(), [_F])
    # every DESCENDANT of F; the seed F itself is NOT included.
    assert got == {_F1, _F1A, _F2, _F2A, _FC, _FC1}


def test_collect_subtree_ids_suppression_pocket_excluded():
    got = collect_subtree_ids(_cache_by_id(), [_F], suppress_tags=_SUPPRESS)
    # The wfy-suppress pocket node AND its descendant are absent (guard
    # refuses to include OR descend); the rest of the subtree is swept.
    assert got == {_F1, _F1A, _FC, _FC1}


def test_collect_subtree_ids_nested_roots_dedupe():
    got = collect_subtree_ids(_cache_by_id(), [_F, _F1])
    assert got == {_F1, _F1A, _F2, _F2A, _FC, _FC1}   # F1's subtree counted once


def test_collect_subtree_ids_empty_input():
    assert collect_subtree_ids(_cache_by_id(), []) == set()


def test_collect_subtree_ids_completed_still_collected():
    # Completed exclusion is a Phase-1 admission policy, NOT a sweep concern:
    # the completed node + its child ARE swept into the set (Phase 1 prunes
    # them at admit time downstream).
    got = collect_subtree_ids(_cache_by_id(), [_F], suppress_tags=_SUPPRESS)
    assert _FC in got and _FC1 in got


# ===========================================================================
# 3. The projection sweep (_project_render_symbols_from_cache)
# ===========================================================================
def _project(monkeypatch, *, symbols, expand_handles=None, suppress=_SUPPRESS):
    _patch_client(monkeypatch)
    kwargs = {}
    if expand_handles is not None:
        kwargs["expand_handles"] = expand_handles
    return asyncio.run(
        srv._project_render_symbols_from_cache(
            cache_by_id=_cache_by_id(),
            symbols=symbols,
            resolved_roots=[_ROOT],
            suppress_tags=list(suppress),
            favor="uuid",
            **kwargs,
        )
    )


def test_projection_flagged_resolved_item_sweeps_subtree(monkeypatch):
    proj = _project(monkeypatch, symbols=[_F], expand_handles={_F})
    assert proj["ok"] is True
    # resolved_ids stays EXPLICIT-ONLY (report semantics stay true) ...
    assert proj["resolved_ids"] == {_F}
    assert proj["expand_ids"] == {_F}
    # ... while member_ids is enlarged by the sweep (suppression-guarded).
    assert proj["member_ids"] == {_F, _F1, _F1A, _FC, _FC1}
    rep = proj["resolution_report"]
    assert rep["expand_ids"] == sorted([_F])
    assert rep["swept_count"] == 4


def test_projection_empty_expand_is_byte_identical(monkeypatch):
    proj = _project(monkeypatch, symbols=[_F])  # no expand_handles kwarg at all
    assert proj["ok"] is True
    assert proj["member_ids"] == proj["resolved_ids"] == {_F}
    assert proj["expand_ids"] == set()
    rep = proj["resolution_report"]
    assert rep["expand_ids"] == []
    assert rep["swept_count"] == 0


def test_projection_flagged_but_ambiguous_is_inert(monkeypatch):
    proj = _project(monkeypatch, symbols=["ambigname"], expand_handles={"ambigname"})
    assert proj["ok"] is True
    assert proj["resolved_ids"] == set()          # ambiguous -> not resolved
    assert proj["expand_ids"] == set()            # NO partial expansion
    assert proj["member_ids"] == set()
    rep = proj["resolution_report"]
    assert rep["swept_count"] == 0
    assert [a["item"] for a in rep["ambiguous"]] == ["ambigname"]


def test_projection_flagged_but_unresolved_is_inert(monkeypatch):
    proj = _project(monkeypatch, symbols=["nosuchname"], expand_handles={"nosuchname"})
    assert proj["ok"] is True
    assert proj["resolved_ids"] == set()
    assert proj["expand_ids"] == set()
    assert proj["member_ids"] == set()
    rep = proj["resolution_report"]
    assert rep["swept_count"] == 0
    assert [u["item"] for u in rep["unresolved"]] == ["nosuchname"]


# ===========================================================================
# 4. End-to-end (MCP path): object elements render the flagged subtree
# ===========================================================================
def _run_mcp(monkeypatch, *, symbols, suppress=_SUPPRESS):
    _patch_client(monkeypatch)
    _stub_suppress_config(monkeypatch, tags=suppress)
    return asyncio.run(
        srv._lightning_rod_render_symbols(
            symbols=symbols,
            root_uuids=[_ROOT],
            favor="uuid",
            as_markdown=True,
            output_file=None,
            sort_by=None,
            sort_direction="asc",
            headings_only=False,
            since=None,
            focus=None,
        )
    )


def test_mcp_object_element_renders_swept_subtree(monkeypatch):
    res = _run_mcp(monkeypatch, symbols=[{"uuid": _F, "expand": True}])
    assert res["success"] is True, res
    md = res["preview_markdown"]
    # The flagged item renders, and its swept descendants come along ...
    assert "def foo()" in md
    assert "helper one" in md
    assert "deep leaf" in md
    # ... but the suppressed pocket is excluded AT SWEEP TIME ...
    assert "suppressed secret" not in md
    assert "pocket" not in md
    # ... and completed descendants are pruned by Phase 1 at admit time (no
    # expand semantics may un-hide completed nodes).
    assert "done helper" not in md
    assert "done child" not in md
    rep = res.get("resolution_report") or {}
    assert rep.get("resolved_count") == 1
    assert rep.get("expand_ids") == [_F]
    assert rep.get("swept_count") == 4


def test_mcp_plain_string_item_no_sweep_regression(monkeypatch):
    # A plain (unflagged) item renders exactly itself -- the sweep is
    # flag-gated, so string-only calls are byte-for-byte the prior behavior.
    res = _run_mcp(monkeypatch, symbols=[_F])
    assert res["success"] is True, res
    md = res["preview_markdown"]
    assert "def foo()" in md
    assert "helper one" not in md
    rep = res.get("resolution_report") or {}
    assert rep.get("expand_ids") == []
    assert rep.get("swept_count") == 0


# ===========================================================================
# 5. End-to-end (docked-modal path): object elements ride the blob round-trip
#    (mirrors the tests/test_render_slice_modal_spec_e3.py pattern)
# ===========================================================================
class _FakeCaptureWebsocket:
    """Captures the single JSON reply the handler sends."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, payload: str):
        try:
            self.sent.append(json.loads(payload))
        except Exception:
            self.sent.append({"_raw": payload})


_ENVELOPE_KEYS = frozenset({"action", "request_id", "endpoint_id", "max_prerender_chars"})


def _to_wire_frame(frame):
    """Pack the frame's scope/view keys into the ONE nested render_spec blob
    (exactly like the client/push send)."""
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec
    scope = {k: v for k, v in frame.items() if k not in _ENVELOPE_KEYS}
    envelope = {k: v for k, v in frame.items() if k in _ENVELOPE_KEYS}
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        group_id=None,
        root_uuid=None,
        lightning=scope.get("lightning"),
        match_scope=str(scope.get("match_scope") or "name_and_note"),
        descend_policy="all",
        sort_by=None,
        sort_direction="asc",
        since=None,
        headings_only=False,
    )
    return {**envelope, "render_spec": spec.to_render_spec_blob()}


def test_modal_object_elements_ride_blob_round_trip(monkeypatch):
    client = _patch_client(monkeypatch)

    # The handler's NESTED suppress-config read (get_slice_group_config) rides
    # the in-band WS rail; stub it + the connection it checks first.
    async def _wsrr(action, **kwargs):
        return {"f8_global_never_expand_tags": list(_SUPPRESS)}
    client._ws_request_response = _wsrr
    monkeypatch.setattr(srv, "get_ws_connection", lambda: (object(), object()))

    frame = {
        "action": "render_slice_modal",
        "request_id": "req-expswp-1",
        "endpoint_id": "tab-1",
        "lightning": {
            "mode": "render_symbols",
            "symbols": [{"uuid": _F, "expand": True}],
            "root_uuids": [_ROOT],
            "favor": "uuid",
            "match_scope": "name_and_note",
        },
    }
    fake_ws = _FakeCaptureWebsocket()
    asyncio.run(srv._handle_render_slice_modal(_to_wire_frame(frame), fake_ws))
    assert fake_ws.sent, "handler sent no reply"
    reply = fake_ws.sent[-1]

    assert reply.get("success") is True, reply
    md = reply.get("markdown") or ""
    # The sweep applied server-side: descendants render; suppression/completed
    # doctrine holds on the modal path exactly as on the MCP path.
    assert "helper one" in md
    assert "deep leaf" in md
    assert "suppressed secret" not in md
    assert "done helper" not in md
    # D5 VERBATIM ROUND-TRIP: the render_scope echo carries the OBJECT element
    # (not a repr-string, not an unwrapped handle), so a Refresh / per-row
    # re-projection re-runs normalize -> resolve -> sweep deterministically.
    sel = (reply.get("render_scope") or {}).get("selection") or {}
    assert {"uuid": _F, "expand": True} in (sel.get("symbols") or [])
    rep = reply.get("resolution_report") or {}
    assert rep.get("expand_ids") == [_F]
    assert rep.get("swept_count") == 4


# ===========================================================================
# 6. Sol-audit hardening locks
# ===========================================================================
def test_normalizer_rejects_non_list_container():
    # The MCP boundary enforces an array, but a hand-pasted render_spec blob
    # can carry a malformed nested container -- the ONE normalizer must guard
    # it (a bare string would otherwise iterate its CHARACTERS as handles).
    assert srv._normalize_render_symbols_items(None)["ok"] is True  # None == empty
    for bad in ("foo", {"uuid": "x"}, 123):
        out = srv._normalize_render_symbols_items(bad)
        assert out["ok"] is False, (bad, out)
        assert "must be a LIST" in out["error"], (bad, out)


def test_mutation_shorthand_rejects_object_elements():
    # Object elements are a render_symbols-only grammar; the mutation ingress
    # funnel must NOT stringify a dict into a junk repr-handle.
    spec, err = LightningRodMutationSpec.normalize_from_tool_args(
        mode="ensure_beacons",
        symbols=[{"uuid": "abc", "expand": True}],
        tags=["x"],
        operations=None,
        comment=None,
        root_uuids=[_ROOT],
        root_uuid=None,
        favor="uuid",
    )
    assert spec is None
    assert err and "must be a STRING" in err


def test_tool_schema_symbols_anyof_string_or_closed_object():
    # Schema-level lock: the LEGACY lightning_rod registration must expose
    # symbols as string|object with a CLOSED object shape (uuid/symbol/expand,
    # additionalProperties false). The consolidated DEFAULT no longer registers
    # the legacy shape in-process, so dump the legacy profile explicitly.
    from pathlib import Path

    from workflowy_mcp.tool_surface_measurement import dump_tool_surface

    repo_root = Path(__file__).resolve().parents[1]
    legacy = dump_tool_surface("legacy", cwd=str(repo_root))
    tool = next((t for t in legacy if t.get("name") == "lightning_rod"), None)
    params = tool.get("inputSchema") if tool is not None else None
    if params is None:
        pytest.skip("legacy tool surface does not expose a schema dict here")
    sym_schema = (params.get("properties", {}) or {}).get("symbols", {})
    branches = (sym_schema.get("anyOf") or [{}])[0].get("items", {}).get("anyOf") or []
    assert any(b.get("type") == "string" for b in branches), sym_schema
    obj = next((b for b in branches if b.get("type") == "object"), None)
    assert obj is not None, sym_schema
    assert obj.get("additionalProperties") is False
    assert set((obj.get("properties") or {}).keys()) == {"uuid", "symbol", "expand"}


def test_projection_flagged_out_of_scope_is_inert(monkeypatch):
    # _F lives under _ROOT; scoping to an unrelated root makes it out-of-scope.
    _patch_client(monkeypatch)
    proj = asyncio.run(
        srv._project_render_symbols_from_cache(
            cache_by_id=_cache_by_id(),
            symbols=[_F],
            resolved_roots=[_G2],
            suppress_tags=list(_SUPPRESS),
            favor="uuid",
            expand_handles={_F},
        )
    )
    assert proj["ok"] is True
    assert proj["expand_ids"] == set()
    assert proj["member_ids"] == set()
    assert [o["item"] for o in proj["resolution_report"]["out_of_scope"]] == [_F]


def test_projection_flagged_all_suppressed_is_inert(monkeypatch):
    # _F2's own NAME carries #wfy-suppress -> its whole resolution is
    # all-suppressed, so a flag on it must be inert (no partial expansion).
    proj = _project(monkeypatch, symbols=[_F2], expand_handles={_F2})
    assert proj["ok"] is True
    assert proj["expand_ids"] == set()
    assert proj["member_ids"] == set()
    assert [a["item"] for a in proj["resolution_report"]["all_suppressed"]] == [_F2]


def test_completed_flagged_root_renders_via_root_exemption(monkeypatch):
    # DOCTRINE LOCK (Dan, 2026-07-29): Phase-1's completed policy tests
    # CHILDREN -- the projection ROOT is exempt by universal design. So a
    # completed FLAGGED root renders itself and its swept NON-completed
    # descendants; a completed DESCENDANT is still pruned at admit time.
    _R = "ffffffff-2222-0000-0000-000000000001"
    _LIVE = "ffffffff-2222-0000-0000-000000000002"
    _DONE = "ffffffff-2222-0000-0000-000000000003"
    _DONE1 = "ffffffff-2222-0000-0000-000000000004"
    nodes = [
        {"id": _R, "name": "completed flagged root", "note": "root body",
         "parent_id": None, "priority": 0, "completed": True},
        {"id": _LIVE, "name": "live swept child", "note": "live body",
         "parent_id": _R, "priority": 0},
        {"id": _DONE, "name": "done swept child", "note": "done body",
         "parent_id": _R, "priority": 1, "completed": True},
        {"id": _DONE1, "name": "done grandchild", "note": "done1 body",
         "parent_id": _DONE, "priority": 0},
    ]
    client = _make_client()
    client._get_nodes_export_cache_nodes = lambda: [dict(n) for n in nodes]
    monkeypatch.setattr(srv, "get_client", lambda: client)
    _stub_suppress_config(monkeypatch)
    res = asyncio.run(
        srv._lightning_rod_render_symbols(
            symbols=[{"uuid": _R, "expand": True}],
            root_uuids=[_R],
            favor="uuid",
            as_markdown=True,
            output_file=None,
            sort_by=None,
            sort_direction="asc",
            headings_only=False,
            since=None,
            focus=None,
        )
    )
    assert res["success"] is True, res
    md = res["preview_markdown"]
    assert "completed flagged root" in md      # root exemption (universal doctrine)
    assert "live swept child" in md           # swept non-completed descendant
    assert "done swept child" not in md       # completed CHILD pruned at admit
    assert "done grandchild" not in md


def test_dispatch_malformed_symbols_container_is_loud(monkeypatch):
    # A hand-pasted render_spec blob carrying a non-list symbols container
    # must fail LOUD through the ONE normalizer -- not iterate its characters.
    _patch_client(monkeypatch)
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        lightning={
            "mode": "render_symbols",
            "symbols": "not-a-list",
            "root_uuids": [_ROOT],
            "favor": "uuid",
            "match_scope": "name_and_note",
        },
    )
    reply = asyncio.run(srv._glimpse_dispatch_spec(spec, output_file=None, target_tab=None))
    assert reply.get("success") is False, reply
    assert "must be a LIST" in (reply.get("error") or "")


def test_push_cell_carries_object_elements_verbatim(monkeypatch):
    # The push cell validates via the shared normalizer and forwards the RAW
    # selection (objects preserved) into the render_spec blob, so the modal
    # handler re-normalizes + re-sweeps on receipt (D5).
    captured: dict = {}

    client = _make_client()

    async def _wsrr(action, **kwargs):
        captured["action"] = action
        captured["payload"] = kwargs.get("payload")
        return {"success": True, "rendering": True}

    client._ws_request_response = _wsrr
    monkeypatch.setattr(srv, "get_client", lambda: client)
    monkeypatch.setattr(
        srv, "get_ws_connection_with_grace_wait",
        lambda max_wait_seconds: _async_return((object(), object())),
    )
    res = asyncio.run(
        srv._glimpse_push_slice_to_modal(
            slice_group=None,
            root_uuid=None,
            sort_by=None,
            sort_direction="asc",
            target_tab=None,
            since=None,
            lightning={
                "mode": "render_symbols",
                "symbols": [{"uuid": _F, "expand": True}],
                "root_uuids": [_ROOT],
                "favor": "uuid",
            },
            match_scope="name_and_note",
            headings_only=False,
            descend_policy="all",
        )
    )
    assert res.get("success") is True, res
    blob = (captured.get("payload") or {}).get("render_spec") or {}
    assert ((blob.get("lightning") or {}).get("symbols")
            == [{"uuid": _F, "expand": True}])
    assert (res.get("lightning") or {}).get("symbols") == [{"uuid": _F, "expand": True}]


async def _async_return(value):
    return value


# ===========================================================================
# 7. Post-Sol-audit Finding 1: a stale echoed top-level root_uuid must NOT
#    override the freshly recomputed render_symbols LCA on a modal re-render
#    (Refresh / per-row after a hierarchy move).
# ===========================================================================
def test_modal_stale_echoed_root_does_not_override_fresh_lca(monkeypatch):
    # The dock adopts the echoed render_scope (root_uuid = the rendered LCA)
    # and sends it back on the next Refresh. If the hierarchy moved in between,
    # the recomputed LCA differs -- the stale echo must not win.
    _OLD = "dddddddd-1111-0000-0000-00000000000d"
    _NEW = "cccccccc-1111-0000-0000-00000000000c"
    _R1 = "aaaaaaaa-1111-0000-0000-00000000000a"
    _R2 = "bbbbbbbb-1111-0000-0000-00000000000b"
    nodes = [
        {"id": _OLD, "name": "STALE OLD LCA", "note": "", "parent_id": None, "priority": 0},
        {"id": _NEW, "name": "CURRENT NEW LCA", "note": "", "parent_id": None, "priority": 1},
        {"id": _R1, "name": "TARGET ONE", "note": "target body", "parent_id": _NEW, "priority": 0},
        {"id": _R2, "name": "SCOPE TWO", "note": "", "parent_id": _NEW, "priority": 1},
    ]
    client = _make_client()
    client._get_nodes_export_cache_nodes = lambda: [dict(n) for n in nodes]
    monkeypatch.setattr(srv, "get_client", lambda: client)

    async def _wsrr(action, **kwargs):
        return {"f8_global_never_expand_tags": []}
    client._ws_request_response = _wsrr
    monkeypatch.setattr(srv, "get_ws_connection", lambda: (object(), object()))

    from workflowy_mcp.cache_render_spec import RenderProjectionSpec
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_OLD,  # the STALE echoed LCA (what a Refresh frame carries)
        lightning={
            "mode": "render_symbols",
            "symbols": [_R1],
            "root_uuids": [_R1, _R2],
            "favor": "uuid",
            "match_scope": "name_and_note",
        },
    )
    frame = {
        "action": "render_slice_modal",
        "request_id": "req-stale-lca",
        "endpoint_id": "tab-1",
        "render_spec": spec.to_render_spec_blob(),
    }
    fake_ws = _FakeCaptureWebsocket()
    asyncio.run(srv._handle_render_slice_modal(frame, fake_ws))
    reply = fake_ws.sent[-1]
    assert reply.get("success") is True, reply
    md = reply.get("markdown") or ""
    assert "TARGET ONE" in md          # the resolved symbol renders
    assert "STALE OLD LCA" not in md   # the stale echo must not win
    rep = reply.get("resolution_report") or {}
    assert rep.get("lca_root") == _NEW
    assert (reply.get("render_scope") or {}).get("root_uuid") == _NEW


# ===========================================================================
# 8. ap:APSYMB -- [ap:CORE] anchor-point handles straight in the symbols list
# ===========================================================================
def test_ap_id_bracketed_and_prefixed_forms_resolve(monkeypatch):
    # Both unambiguous forms (bracketed / prefixed, any case) resolve to the
    # same node via the render wrapper's allow_anchor_points=True gate.
    for form in ("[ap:K7M2QF]", "ap:K7M2QF", "[AP:K7M2QF]", "AP:K7M2QF"):
        proj = _project(monkeypatch, symbols=[form])
        assert proj["ok"] is True
        assert proj["resolved_ids"] == {_AP}, (form, proj["resolution_report"])
        assert proj["member_ids"] == {_AP}


def test_ap_id_duplicate_is_ambiguous(monkeypatch):
    # A duplicated AP-ID (copy/paste accident) -> the existing AMBIGUOUS
    # bucket, all colliding nodes reported, never auto-picked.
    proj = _project(monkeypatch, symbols=["[ap:DUPE12]"])
    assert proj["ok"] is True
    assert proj["resolved_ids"] == set()
    amb = proj["resolution_report"]["ambiguous"]
    assert [a["item"] for a in amb] == ["[ap:DUPE12]"]
    assert {c["uuid"] for c in amb[0]["candidates"]} == {_APD1, _APD2}


def test_ap_id_no_match_is_unresolved(monkeypatch):
    proj = _project(monkeypatch, symbols=["[ap:NOSUCH]"])
    assert proj["resolved_ids"] == set()
    assert [u["item"] for u in proj["resolution_report"]["unresolved"]] == ["[ap:NOSUCH]"]


def test_bare_core_is_not_treated_as_anchor_point(monkeypatch):
    # GRAMMAR GUARD: a bare CORE collides with legitimate symbol/beacon
    # names, so it must fall through to the ordinary UUID-shaped / symbol
    # classification -- it must NOT resolve the [ap:K7M2QF] node. (The
    # brackets-without-ap: form '[K7M2QF]' is likewise not an AP-ID item.)
    for form in ("K7M2QF", "[K7M2QF]"):
        proj = _project(monkeypatch, symbols=[form])
        assert proj["resolved_ids"] == set(), (form, proj["resolution_report"])
        assert [u["item"] for u in proj["resolution_report"]["unresolved"]] == [form]


def test_ap_id_scope_gate_applies(monkeypatch):
    # The scope gate comes free: _AP lives under _ROOT, so scoping to an
    # unrelated root makes the AP-ID item out-of-scope.
    _patch_client(monkeypatch)
    proj = asyncio.run(
        srv._project_render_symbols_from_cache(
            cache_by_id=_cache_by_id(),
            symbols=["[ap:K7M2QF]"],
            resolved_roots=[_G2],
            suppress_tags=list(_SUPPRESS),
            favor="uuid",
        )
    )
    assert proj["ok"] is True
    assert proj["resolved_ids"] == set()
    assert [o["item"] for o in proj["resolution_report"]["out_of_scope"]] == ["[ap:K7M2QF]"]


def test_core_ap_id_branch_gated_on_allow_anchor_points(monkeypatch):
    # THE MUTATION GATE: with the DEFAULT allow_anchor_points=False (what the
    # beacon-mutation call sites pass), an '[ap:CORE]' item falls through to
    # the ordinary classification and does NOT resolve as an anchor point;
    # with True (the render wrapper), it resolves.
    _patch_client(monkeypatch)
    core_default = asyncio.run(
        srv._resolve_lightning_rod_symbol_items_from_cache(
            cache_by_id=_cache_by_id(),
            symbols=["[ap:K7M2QF]"],
            resolved_roots=[_ROOT],
            suppress_tags=[],
        )
    )
    assert core_default["ok"] is True
    assert core_default["resolved_ids"] == set()
    assert [u["item"] for u in core_default["resolution_report"]["unresolved"]] == ["[ap:K7M2QF]"]

    core_allowed = asyncio.run(
        srv._resolve_lightning_rod_symbol_items_from_cache(
            cache_by_id=_cache_by_id(),
            symbols=["[ap:K7M2QF]"],
            resolved_roots=[_ROOT],
            suppress_tags=[],
            allow_anchor_points=True,
        )
    )
    assert core_allowed["ok"] is True
    assert core_allowed["resolved_ids"] == {_AP}


def test_mcp_plain_ap_id_string_renders_itself(monkeypatch):
    # A plain (unflagged) AP-ID item renders exactly itself -- no sweep.
    res = _run_mcp(monkeypatch, symbols=["ap:K7M2QF"])
    assert res["success"] is True, res
    md = res["preview_markdown"]
    assert "captured anchor point" in md
    assert "captured sub-note" not in md
    rep = res.get("resolution_report") or {}
    assert rep.get("resolved_count") == 1
    assert rep.get("expand_ids") == []
    assert rep.get("swept_count") == 0


def test_mcp_ap_id_object_form_with_expand_sweeps_subtree(monkeypatch):
    # The motivating Q2 use case: the object form rides free (v1 unwrap is
    # kind-agnostic), and a flagged anchor point sweeps its WHOLE subtree --
    # 'show me every anchor point that session captured, as one document.'
    res = _run_mcp(monkeypatch, symbols=[{"uuid": "[ap:K7M2QF]", "expand": True}])
    assert res["success"] is True, res
    md = res["preview_markdown"]
    assert "captured anchor point" in md
    assert "captured sub-note" in md
    assert "deep captured leaf" in md
    rep = res.get("resolution_report") or {}
    assert rep.get("resolved_count") == 1
    assert rep.get("expand_ids") == [_AP]
    assert rep.get("swept_count") == 2


def test_modal_ap_id_object_rides_blob_round_trip(monkeypatch):
    # The docked-modal path: the AP-ID object element rides the render_spec
    # blob round-trip VERBATIM, and the sweep applies server-side exactly as
    # on the MCP path (mirrors test_modal_object_elements_ride_blob_round_trip).
    client = _patch_client(monkeypatch)

    async def _wsrr(action, **kwargs):
        return {"f8_global_never_expand_tags": list(_SUPPRESS)}
    client._ws_request_response = _wsrr
    monkeypatch.setattr(srv, "get_ws_connection", lambda: (object(), object()))

    frame = {
        "action": "render_slice_modal",
        "request_id": "req-apsymb-1",
        "endpoint_id": "tab-1",
        "lightning": {
            "mode": "render_symbols",
            "symbols": [{"uuid": "[ap:K7M2QF]", "expand": True}],
            "root_uuids": [_ROOT],
            "favor": "uuid",
            "match_scope": "name_and_note",
        },
    }
    fake_ws = _FakeCaptureWebsocket()
    asyncio.run(srv._handle_render_slice_modal(_to_wire_frame(frame), fake_ws))
    assert fake_ws.sent, "handler sent no reply"
    reply = fake_ws.sent[-1]

    assert reply.get("success") is True, reply
    md = reply.get("markdown") or ""
    assert "captured anchor point" in md
    assert "captured sub-note" in md
    assert "deep captured leaf" in md
    # D5 VERBATIM ROUND-TRIP: the render_scope echo carries the OBJECT element
    # with the AP-ID string intact (not a repr, not a resolved UUID).
    sel = (reply.get("render_scope") or {}).get("selection") or {}
    assert {"uuid": "[ap:K7M2QF]", "expand": True} in (sel.get("symbols") or [])
    rep = reply.get("resolution_report") or {}
    assert rep.get("expand_ids") == [_AP]
    assert rep.get("swept_count") == 2
