"""Regression lock: Lightning Rod ``render_tags`` ``postfix_policy='slice'`` (ap:IMPPTX).

THE FEATURE: Lightning Rod ``render_tags`` gains an opt-in ``postfix_policy``
knob. ``'none'`` (default) is the original Phase-1-only prefix-family match
(no ``--expand``/``--suppress``/``--deprecated`` operations, fully
cache-only). ``'slice'`` renders the requested stems as a DISPOSABLE SLICE
GROUP: the projection routes through the SAME full Stage-1 funnel a persisted
slice group uses (``build_stage1_base_tree``) with the requested tags as
``active_prefixes`` and the LIVE postfix words + the REAL
``f8_global_never_expand_tags`` list read from config (ONE config read; loud
failure when no tab). Scoped (frozen-DOM) Lightning loud-rejects ``'slice'``
in v1. The policy rides the opaque lightning selection end-to-end: MCP return,
push, modal render_scope echo, Refresh / per-row, and render_spec re-projection.

Coverage (all local: no network, no real Workflowy, no disk writes):
  * the ONE shared normalizer (_normalize_lightning_postfix_policy);
  * MCP-return path: default performs NO config read; 'slice' performs exactly
    ONE; 'slice' funnel semantics (deprecated leaf excluded, --expand hydration,
    --suppress collapse + zombie prune, global-never absolute suppression via
    the HYDSUP seal, rescued-deprecated header-only body suppression);
  * config failure is loud + actionable; explicit 'none' == omitted (byte
    identity, and the echo omits the key);
  * the render_scope selection echo carries postfix_policy only when set;
  * _glimpse_dispatch_spec re-projects a 'slice' selection (round-trip);
  * _handle_render_slice_modal: 'slice' returns plan metadata (pending_expand,
    pruned zombies) + body suppression, keeps search_string_to_issue EMPTY and
    live_gui_sync_supported=False (unmoored), and echoes postfix_policy in the
    selection; gather + 'slice' is a loud error;
  * _handle_render_lightning_dom_capture (Scoped): 'slice' is a loud error.
"""

import asyncio
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402
from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402


# ---------------------------------------------------------------------------
# Cache fixture: one tree exercising every postfix semantic at once.
#
# root
# |-- EX        "Region #foo--expand"
# |   |-- UNTAGGED   (hydrated by the expansion)
# |   |-- POCKET     "Pocket #foo--suppress"
# |   |   `-- SECRET (collapsed away)
# |   `-- GSUP       "Gsup #wfy-suppress"   (HYDSUP seal: never rehydrated)
# |       `-- GSECRET
# |-- DEP       "Old leaf #foo--deprecated" (fully deprecated -> NOT a member)
# |-- LIVE      "Live #foo"
# `-- RESC      "Superseded region #foo--deprecated" (rescued by live child)
#     `-- RESC_LIVE "Live child #foo"
# ---------------------------------------------------------------------------
_ROOT = "aaaaaaaa-0000-0000-0000-000000000001"
_EX = "aaaaaaaa-0000-0000-0000-000000000002"
_UNTAGGED = "aaaaaaaa-0000-0000-0000-000000000003"
_POCKET = "aaaaaaaa-0000-0000-0000-000000000004"
_SECRET = "aaaaaaaa-0000-0000-0000-000000000005"
_GSUP = "aaaaaaaa-0000-0000-0000-000000000006"
_GSECRET = "aaaaaaaa-0000-0000-0000-000000000007"
_DEP = "aaaaaaaa-0000-0000-0000-000000000008"
_LIVE = "aaaaaaaa-0000-0000-0000-000000000009"
_RESC = "aaaaaaaa-0000-0000-0000-00000000000a"
_RESC_LIVE = "aaaaaaaa-0000-0000-0000-00000000000b"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _EX, "name": "Region #foo--expand", "note": "", "parent_id": _ROOT, "priority": 0},
    {"id": _UNTAGGED, "name": "Untagged child", "note": "", "parent_id": _EX, "priority": 0},
    {"id": _POCKET, "name": "Pocket #foo--suppress", "note": "", "parent_id": _EX, "priority": 1},
    {"id": _SECRET, "name": "Secret", "note": "", "parent_id": _POCKET, "priority": 0},
    {"id": _GSUP, "name": "Gsup #wfy-suppress", "note": "", "parent_id": _EX, "priority": 2},
    {"id": _GSECRET, "name": "GSecret", "note": "", "parent_id": _GSUP, "priority": 0},
    {"id": _DEP, "name": "Old leaf #foo--deprecated", "note": "", "parent_id": _ROOT, "priority": 1},
    {"id": _LIVE, "name": "Live #foo", "note": "", "parent_id": _ROOT, "priority": 2},
    {"id": _RESC, "name": "Superseded region #foo--deprecated", "note": "OLD-BODY", "parent_id": _ROOT, "priority": 3},
    {"id": _RESC_LIVE, "name": "Live child #foo", "note": "NEW-BODY", "parent_id": _RESC, "priority": 0},
]

_POSTFIXES = {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"}
_GLOBAL_NEVER = ["wfy-suppress"]


def _make_client():
    from coherence_harness import make_boundary_capable

    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    c._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CACHE_NODES]
    return make_boundary_capable(c, nodes=_CACHE_NODES)


def _stub_config(monkeypatch, *, calls=None, error=None, postfixes=None):
    """Stub the ONE config read (_resolve_slice_group_config)."""
    async def _cfg(_target_tab):
        if error is not None:
            raise error
        if calls is not None:
            calls.append(1)
        # (groups, active_id, postfixes, global_never_expand_tags, raw)
        return [], None, dict(postfixes or _POSTFIXES), list(_GLOBAL_NEVER), {}
    monkeypatch.setattr(srv, "_resolve_slice_group_config", _cfg)


def _stub_config_raiser(monkeypatch):
    async def _cfg(_target_tab):
        raise AssertionError("config read must NOT happen under postfix_policy='none'")
    monkeypatch.setattr(srv, "_resolve_slice_group_config", _cfg)


def _run_mcp(monkeypatch, *, postfix_policy=None, tags=("foo",)):
    client = _make_client()
    monkeypatch.setattr(srv, "get_client", lambda: client)
    kwargs = {}
    if postfix_policy is not None:
        kwargs["postfix_policy"] = postfix_policy
    return asyncio.run(
        srv._lightning_rod_from_cache(
            mode="render_tags",
            root_uuid=_ROOT,
            tags=list(tags),
            as_markdown=True,
            output_file=None,
            sort_by=None,
            sort_direction="asc",
            headings_only=False,
            since=None,
            **kwargs,
        )
    )


# ---------------------------------------------------------------------------
# 1. The ONE shared normalizer.
# ---------------------------------------------------------------------------
def test_normalizer_defaults_and_values():
    assert srv._normalize_lightning_postfix_policy(None, "render_tags") == ("none", None)
    assert srv._normalize_lightning_postfix_policy("", "render_tags") == ("none", None)
    assert srv._normalize_lightning_postfix_policy("none", "render_tags") == ("none", None)
    assert srv._normalize_lightning_postfix_policy(" SLICE ", "render_tags") == ("slice", None)
    assert srv._normalize_lightning_postfix_policy("none", "gather") == ("none", None)


def test_normalizer_loud_errors():
    norm, err = srv._normalize_lightning_postfix_policy("bogus", "render_tags")
    assert err and "postfix_policy" in err
    for mode in ("gather", "render_search", "render_symbols", "ensure_beacons"):
        norm, err = srv._normalize_lightning_postfix_policy("slice", mode)
        assert err and "render_tags" in err, mode


# ---------------------------------------------------------------------------
# 2. MCP-return path: DEFAULT performs NO config read (headless preserved).
# ---------------------------------------------------------------------------
def test_default_policy_no_config_read_and_family_semantics(monkeypatch):
    _stub_config_raiser(monkeypatch)  # fails the test if a read is attempted
    res = _run_mcp(monkeypatch)  # postfix_policy omitted -> 'none'
    assert res.get("success") is not False, res
    md = res.get("preview_markdown")
    assert isinstance(md, str), res
    # Original family semantics: postfix variants are ordinary members, and
    # NO expansion hydration happens.
    assert "Region" in md          # #foo--expand is a plain member
    assert "Pocket" in md          # #foo--suppress is a plain member
    assert "Old leaf" in md        # #foo--deprecated is a plain member
    assert "Superseded region" in md
    assert "Untagged child" not in md   # no --expand hydration in 'none'
    assert "Secret" not in md           # not a member, never hydrated
    assert "Gsup" not in md
    # The echo does NOT carry postfix_policy when default.
    scope = res.get("render_scope") or {}
    selection = scope.get("selection") or {}
    assert "postfix_policy" not in selection, selection


def test_explicit_none_is_byte_identical_to_omitted(monkeypatch):
    _stub_config_raiser(monkeypatch)
    a = _run_mcp(monkeypatch)
    b = _run_mcp(monkeypatch, postfix_policy="none")
    assert a.get("preview_markdown") == b.get("preview_markdown")
    assert (a.get("render_scope") or {}).get("selection") == (
        b.get("render_scope") or {}
    ).get("selection")


# ---------------------------------------------------------------------------
# 3. MCP-return path: 'slice' = the full funnel (ONE config read).
# ---------------------------------------------------------------------------
def test_slice_policy_full_funnel_semantics(monkeypatch):
    calls = []
    _stub_config(monkeypatch, calls=calls)
    res = _run_mcp(monkeypatch, postfix_policy="slice")
    assert res.get("success") is not False, res
    assert len(calls) == 1, "slice policy must perform exactly ONE config read"
    md = res.get("preview_markdown")
    assert isinstance(md, str), res
    # --expand hydration: the untagged child of the expansion region renders.
    assert "Untagged child" in md
    # Active-family --suppress pocket: collapsed + Phase-4 zombie-pruned.
    assert "Pocket" not in md
    assert "Secret" not in md
    assert res.get("pruned_collapsed_zombie_count") == 1, res
    # Global-never absolute suppression under the expansion (HYDSUP seal).
    assert "Gsup" not in md
    assert "GSecret" not in md
    # Fully deprecated leaf is NOT a slice member.
    assert "Old leaf" not in md
    # Rescued deprecated ancestor: heading renders, body withheld.
    assert "Superseded region" in md
    assert "OLD-BODY" not in md
    assert "NEW-BODY" in md
    # The echo carries the policy (request identity for re-projection).
    scope = res.get("render_scope") or {}
    selection = scope.get("selection") or {}
    assert selection.get("postfix_policy") == "slice", selection
    assert scope.get("live_gui_sync_supported") is False
    assert scope.get("live_gui_sync_skipped_reason") == "lightning"


def test_slice_policy_config_failure_is_loud(monkeypatch):
    _stub_config(monkeypatch, error=RuntimeError("no tab"))
    res = _run_mcp(monkeypatch, postfix_policy="slice")
    assert res.get("success") is False
    assert "postfix" in (res.get("error") or "")


# ---------------------------------------------------------------------------
# 4. render_spec round-trip: _glimpse_dispatch_spec re-projects the lens.
# ---------------------------------------------------------------------------
def test_dispatch_spec_reprojects_slice_policy(monkeypatch):
    calls = []
    _stub_config(monkeypatch, calls=calls)
    monkeypatch.setattr(srv, "get_client", _make_client)
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT,
        lightning={
            "mode": "render_tags",
            "tags": ["foo"],
            "postfix_policy": "slice",
            "match_scope": "name_and_note",
        },
    )
    reply = asyncio.run(srv._glimpse_dispatch_spec(spec, output_file=None, target_tab=None))
    assert isinstance(reply, dict), reply
    md = reply.get("preview_markdown")
    assert isinstance(md, str), reply
    assert "Untagged child" in md      # the funnel ran (not the predicate)
    assert "Old leaf" not in md
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 5. The docked-modal handler (_handle_render_slice_modal).
# ---------------------------------------------------------------------------
class _FakeCaptureWebsocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, payload: str):
        try:
            self.sent.append(json.loads(payload))
        except Exception:
            self.sent.append({"_raw": payload})


_ENVELOPE_KEYS = frozenset({"action", "request_id", "endpoint_id", "max_prerender_chars"})


def _to_wire_frame(frame):
    """Pack flat scope keys into the ONE nested render_spec blob (e3 pattern)."""
    if "render_spec" in frame:
        return dict(frame)
    scope = {k: v for k, v in frame.items() if k not in _ENVELOPE_KEYS}
    envelope = {k: v for k, v in frame.items() if k in _ENVELOPE_KEYS}
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        group_id=(scope.get("group_id") or None),
        root_uuid=(scope.get("root_uuid") or None),
        lightning=(scope.get("lightning") if isinstance(scope.get("lightning"), dict) else None),
        match_scope=str(scope.get("match_scope") or "name_and_note"),
        sort_by=scope.get("sort_by"),
        sort_direction=str(scope.get("sort_direction") or "asc"),
    )
    return {**envelope, "render_spec": spec.to_render_spec_blob()}


def _run_modal(frame, monkeypatch):
    """Drive the real handler with a fake client + stubbed nested config read."""
    client = _make_client()

    async def _ws_rr(_action, **kwargs):
        # The nested get_slice_group_config read for postfix words.
        return {
            "expand_groups": [],
            "active_group": None,
            "f8_postfix_auto_expand": "expand",
            "f8_postfix_suppress": "suppress",
            "f8_postfix_deprecated": "deprecated",
            "f8_global_never_expand_tags": list(_GLOBAL_NEVER),
        }

    client._ws_request_response = _ws_rr
    monkeypatch.setattr(srv, "get_client", lambda: client)
    monkeypatch.setattr(srv, "get_ws_connection", lambda: (object(), object()))
    fake_ws = _FakeCaptureWebsocket()
    asyncio.run(srv._handle_render_slice_modal(_to_wire_frame(frame), fake_ws))
    assert fake_ws.sent, "handler sent no reply"
    return fake_ws.sent[-1]


def test_modal_render_tags_slice_policy(monkeypatch):
    reply = _run_modal({
        "action": "render_slice_modal",
        "request_id": "req-pp-1",
        "endpoint_id": "tab-1",
        "root_uuid": _ROOT,
        "lightning": {
            "mode": "render_tags",
            "tags": ["foo"],
            "postfix_policy": "slice",
            "match_scope": "name_and_note",
        },
    }, monkeypatch)
    assert reply.get("success") is True, reply
    md = reply.get("markdown") or ""
    # Full funnel semantics in the dock.
    assert "Untagged child" in md
    assert "Old leaf" not in md
    assert "Pocket" not in md
    assert "Gsup" not in md
    assert "OLD-BODY" not in md
    assert "NEW-BODY" in md
    # Plan metadata lights up (non-null plan).
    assert _EX in (reply.get("pending_expand_ids") or [])
    assert reply.get("pruned_collapsed_zombie_count") == 1
    # UNMOORED: the live-GUI search stays EMPTY and sync stays unsupported.
    assert reply.get("search_string_to_issue") == ""
    scope = reply.get("render_scope") or {}
    assert scope.get("kind") == "lightning_rod"
    assert scope.get("live_gui_sync_supported") is False
    assert (scope.get("selection") or {}).get("postfix_policy") == "slice"


def test_modal_gather_slice_policy_loud_error(monkeypatch):
    reply = _run_modal({
        "action": "render_slice_modal",
        "request_id": "req-pp-2",
        "endpoint_id": "tab-1",
        "root_uuid": _ROOT,
        "lightning": {"mode": "gather", "postfix_policy": "slice"},
    }, monkeypatch)
    assert reply.get("success") is False
    assert "render_tags" in (reply.get("error") or "")


def test_modal_default_policy_no_config_read(monkeypatch):
    """Default modal render_tags never touches the postfix config read."""
    client = _make_client()

    async def _ws_rr(_action, **kwargs):
        raise AssertionError("no config read expected under postfix_policy='none'")

    client._ws_request_response = _ws_rr
    monkeypatch.setattr(srv, "get_client", lambda: client)
    # get_ws_connection is NOT stubbed: the default cell must not need it.
    fake_ws = _FakeCaptureWebsocket()
    asyncio.run(srv._handle_render_slice_modal(_to_wire_frame({
        "action": "render_slice_modal",
        "request_id": "req-pp-3",
        "endpoint_id": "tab-1",
        "root_uuid": _ROOT,
        "lightning": {"mode": "render_tags", "tags": ["foo"]},
    }), fake_ws))
    reply = fake_ws.sent[-1]
    assert reply.get("success") is True, reply
    md = reply.get("markdown") or ""
    assert "Old leaf" in md           # family semantics: deprecated is a member
    assert "Untagged child" not in md  # no hydration
    assert reply.get("pruned_collapsed_zombie_count") == 0
    scope = reply.get("render_scope") or {}
    assert "postfix_policy" not in (scope.get("selection") or {})


# ---------------------------------------------------------------------------
# 6. Scoped (frozen-DOM) Lightning loud-rejects 'slice'.
# ---------------------------------------------------------------------------
def test_scoped_lightning_slice_policy_loud_error(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(srv, "get_client", lambda: client)
    fake_ws = _FakeCaptureWebsocket()
    asyncio.run(srv._handle_render_lightning_dom_capture({
        "action": "render_lightning_dom_capture",
        "request_id": "req-pp-4",
        "endpoint_id": "tab-1",
        "captured_tree": {"id": _ROOT, "name": "Root", "children": []},
        "lightning": {"mode": "render_tags", "tags": ["foo"], "postfix_policy": "slice"},
    }, fake_ws))
    reply = fake_ws.sent[-1]
    assert reply.get("success") is False
    assert "Scoped" in (reply.get("error") or "")


# ---------------------------------------------------------------------------
# POST-AUDIT REMEDIATION (GPT 5.6 audit of 9371f60, Findings 1/3/5):
#   * '#' / whitespace-wrapped tag stems MUST normalize through the SAME
#     canonical normalizer a persisted slice group uses
#     (normalize_slice_group_tags, LIVE postfix words) before the funnel;
#   * custom postfix words, multiple prefixes + per-prefix deprecation,
#     match_scope='name' + note-authored operations, completed-under-expand,
#     stop_at_child_anchors, push-to-modal selection preservation, and the
#     ONE shared get_slice_group_config reply parser are all locked here.
# ---------------------------------------------------------------------------


def test_hash_and_whitespace_tags_normalize_to_base_prefixes(monkeypatch):
    calls = []
    _stub_config(monkeypatch, calls=calls)
    a = _run_mcp(monkeypatch, postfix_policy="slice")
    b = _run_mcp(monkeypatch, postfix_policy="slice", tags=["#foo"])
    c = _run_mcp(monkeypatch, postfix_policy="slice", tags=["  #foo  ", "foo"])
    # '#' / whitespace variants now derive the SAME active prefixes.
    assert b.get("preview_markdown") == a.get("preview_markdown")
    assert c.get("preview_markdown") == a.get("preview_markdown")
    assert "Untagged child" in (b.get("preview_markdown") or "")


def test_postfix_variant_token_collapses_to_base(monkeypatch):
    _stub_config(monkeypatch)
    res = _run_mcp(monkeypatch, postfix_policy="slice", tags=["foo--expand"])
    # foo--expand collapses to the base stem foo (disposable-slice-group
    # semantics, same as a persisted group's stored prefixes).
    assert res.get("success") is not False, res
    assert "Untagged child" in (res.get("preview_markdown") or "")


def test_all_tokens_normalize_away_is_loud(monkeypatch):
    _stub_config(monkeypatch)
    res = _run_mcp(monkeypatch, postfix_policy="slice", tags=["--expand"])
    assert res.get("success") is False
    assert "prefixes" in (res.get("error") or "")


# --- custom postfix words (through BOTH orchestrators) ----------------------
# Fixture isolates each custom word: the expansion region is a SUBTREE (so
# membership vs hydration stays distinguishable): --open hydrates the untagged
# child; --hide collapses its pocket; --retired drops membership entirely (the
# retired leaf sits OUTSIDE the expansion, so it is never rehydrated either).
_CUSTOM_POSTFIXES = {"auto_expand": "open", "suppress": "hide", "deprecated": "retired"}
_CX_ROOT = "cccccccc-0000-0000-0000-000000000001"
_CX_EX = "cccccccc-0000-0000-0000-000000000002"
_CX_HIDE = "cccccccc-0000-0000-0000-000000000004"
_CX_NODES = [
    {"id": _CX_ROOT, "name": "Root", "note": "", "parent_id": None, "priority": 0},
    {"id": _CX_EX, "name": "Region #foo--open", "note": "", "parent_id": _CX_ROOT, "priority": 0},
    {"id": "cccccccc-0000-0000-0000-000000000003", "name": "Untagged", "note": "", "parent_id": _CX_EX, "priority": 0},
    {"id": _CX_HIDE, "name": "Hidden pocket #foo--hide", "note": "", "parent_id": _CX_EX, "priority": 1},
    {"id": "cccccccc-0000-0000-0000-000000000005", "name": "Secret", "note": "", "parent_id": _CX_HIDE, "priority": 0},
    {"id": "cccccccc-0000-0000-0000-000000000006", "name": "Old leaf #foo--retired", "note": "", "parent_id": _CX_ROOT, "priority": 1},
    {"id": "cccccccc-0000-0000-0000-000000000007", "name": "Live #foo", "note": "", "parent_id": _CX_ROOT, "priority": 2},
]


def _assert_custom_words_semantics(md):
    assert "Region" in md
    assert "Untagged" in md          # --open hydrates the untagged child
    assert "Hidden pocket" not in md  # --hide collapses the pocket
    assert "Secret" not in md
    assert "Old leaf" not in md       # --retired drops membership (and the leaf
                                      # is outside the expansion, so it is
                                      # never rehydrated either)
    assert "Live" in md


def test_custom_postfix_words_mcp(monkeypatch):
    _stub_config(monkeypatch, postfixes=_CUSTOM_POSTFIXES)
    client = _make_client()
    client._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CX_NODES]
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = asyncio.run(srv._lightning_rod_from_cache(
        mode="render_tags", root_uuid=_CX_ROOT, tags=["foo"], postfix_policy="slice",
    ))
    assert res.get("success") is not False, res
    _assert_custom_words_semantics(res.get("preview_markdown") or "")


def test_custom_postfix_words_modal(monkeypatch):
    client = _make_client()
    client._get_nodes_export_cache_nodes = lambda: [dict(n) for n in _CX_NODES]

    async def _ws_rr(_action, **kwargs):
        return {
            "expand_groups": [],
            "active_group": None,
            "f8_postfix_auto_expand": "open",
            "f8_postfix_suppress": "hide",
            "f8_postfix_deprecated": "retired",
            "f8_global_never_expand_tags": [],
        }

    client._ws_request_response = _ws_rr
    monkeypatch.setattr(srv, "get_client", lambda: client)
    monkeypatch.setattr(srv, "get_ws_connection", lambda: (object(), object()))
    fake_ws = _FakeCaptureWebsocket()
    asyncio.run(srv._handle_render_slice_modal(_to_wire_frame({
        "action": "render_slice_modal",
        "request_id": "req-cx-1",
        "endpoint_id": "tab-1",
        "root_uuid": _CX_ROOT,
        "lightning": {"mode": "render_tags", "tags": ["foo"], "postfix_policy": "slice"},
    }), fake_ws))
    reply = fake_ws.sent[-1]
    assert reply.get("success") is True, reply
    _assert_custom_words_semantics(reply.get("markdown") or "")


def test_multiple_prefixes_per_prefix_deprecation(monkeypatch):
    # Per-prefix deprecation semantics (current compatibility truth):
    #   * A -- deprecated on foo with NO live prefix -> NOT a member at all.
    #   * B -- deprecated on foo but live via bar, WITH a live descendant ->
    #     the deprecated-collapse rescue saves it; it renders HEADER-ONLY
    #     (body suppressed). (A no-descendant B would instead collapse --
    #     the node itself being deprecated does not count as live for the
    #     rescue.)
    #   * C -- deprecated on bar (its only prefix) -> NOT a member.
    #   * D -- plain live member.
    _stub_config(monkeypatch)
    root = "dddddddd-0000-0000-0000-000000000001"
    b_node = "dddddddd-0000-0000-0000-000000000003"
    nodes = [
        {"id": root, "name": "Root", "note": "", "parent_id": None, "priority": 0},
        {"id": "dddddddd-0000-0000-0000-000000000002", "name": "A #foo--deprecated", "note": "", "parent_id": root, "priority": 0},
        {"id": b_node, "name": "B #foo--deprecated #bar", "note": "B-BODY", "parent_id": root, "priority": 1},
        {"id": "dddddddd-0000-0000-0000-000000000004", "name": "Blive #bar", "note": "", "parent_id": b_node, "priority": 0},
        {"id": "dddddddd-0000-0000-0000-000000000005", "name": "C #bar--deprecated", "note": "", "parent_id": root, "priority": 2},
        {"id": "dddddddd-0000-0000-0000-000000000006", "name": "D #foo", "note": "", "parent_id": root, "priority": 3},
    ]
    client = _make_client()
    client._get_nodes_export_cache_nodes = lambda: [dict(n) for n in nodes]
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = asyncio.run(srv._lightning_rod_from_cache(
        mode="render_tags", root_uuid=root, tags=["foo", "bar"], postfix_policy="slice",
    ))
    md = res.get("preview_markdown") or ""
    assert "A #foo--deprecated" not in md     # foo deprecated on foo, no live prefix
    assert "B #foo--deprecated #bar" in md    # rescued by its live #bar descendant
    assert "B-BODY" not in md                # ...but renders HEADER-ONLY
    assert "Blive" in md
    assert "C #bar--deprecated" not in md     # bar deprecated on bar
    assert "D #foo" in md


def test_match_scope_name_note_authored_operation_is_inert(monkeypatch):
    # 084 ruling (reversed lock [ap:T9M4KW]): under match_scope='name' the
    # Phase-2 OPERATION policy reads ONLY the name's TRAILING tag region --
    # a connective ancestor's note-authored --suppress is prose, not an
    # operation, so nothing collapses. (Under the default 'name_and_note'
    # scope the old NAME+NOTE reading still holds -- locked in
    # test_phase3_hydration_suppression.)
    _stub_config(monkeypatch)
    root = "eeeeeeee-0000-0000-0000-000000000001"
    nodes = [
        {"id": root, "name": "Root", "note": "", "parent_id": None, "priority": 0},
        {"id": "eeeeeeee-0000-0000-0000-000000000002", "name": "Mid", "note": "ops #foo--suppress", "parent_id": root, "priority": 0},
        {"id": "eeeeeeee-0000-0000-0000-000000000003", "name": "Leaf #foo", "note": "", "parent_id": "eeeeeeee-0000-0000-0000-000000000002", "priority": 0},
    ]
    client = _make_client()
    client._get_nodes_export_cache_nodes = lambda: [dict(n) for n in nodes]
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = asyncio.run(srv._lightning_rod_from_cache(
        mode="render_tags", root_uuid=root, tags=["foo"], postfix_policy="slice",
        match_scope="name",
    ))
    md = res.get("preview_markdown") or ""
    assert "Mid" in md and "Leaf" in md
    assert (res.get("pruned_collapsed_zombie_count") or 0) == 0, res


def test_completed_descendant_under_expand_pruned(monkeypatch):
    _stub_config(monkeypatch)
    root = "ffffffff-0000-0000-0000-000000000001"
    nodes = [
        {"id": root, "name": "Root #foo--expand", "note": "", "parent_id": None, "priority": 0},
        {"id": "ffffffff-0000-0000-0000-000000000002", "name": "Done child", "note": "", "parent_id": root, "priority": 0, "completed": True},
        {"id": "ffffffff-0000-0000-0000-000000000003", "name": "Grandchild", "note": "", "parent_id": "ffffffff-0000-0000-0000-000000000002", "priority": 0},
        {"id": "ffffffff-0000-0000-0000-000000000004", "name": "Live #foo", "note": "", "parent_id": root, "priority": 1},
    ]
    client = _make_client()
    client._get_nodes_export_cache_nodes = lambda: [dict(n) for n in nodes]
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = asyncio.run(srv._lightning_rod_from_cache(
        mode="render_tags", root_uuid=root, tags=["foo"], postfix_policy="slice",
    ))
    md = res.get("preview_markdown") or ""
    # The universal completed-exclusion doctrine holds under the funnel's
    # expansion hydration: no --expand semantics may un-hide completed nodes.
    assert "Done child" not in md
    assert "Grandchild" not in md
    assert "Live" in md


def test_descend_policy_stop_at_child_anchors_through_slice_lens(monkeypatch):
    _stub_config(monkeypatch)

    async def _fake_note_session_config(_target_tab):
        return (["wfy-ns"], [], None, {})

    monkeypatch.setattr(srv, "_resolve_note_session_config", _fake_note_session_config)
    root = "99999999-0000-0000-0000-000000000001"
    anchor = "99999999-0000-0000-0000-000000000002"
    nodes = [
        {"id": root, "name": "Root #topic--expand", "note": "", "parent_id": None, "priority": 0},
        {"id": anchor, "name": "Child anchor #wfy-ns #topic", "note": "", "parent_id": root, "priority": 0},
        {"id": "99999999-0000-0000-0000-000000000003", "name": "Deep #topic", "note": "", "parent_id": anchor, "priority": 0},
        {"id": "99999999-0000-0000-0000-000000000004", "name": "Live #topic", "note": "", "parent_id": root, "priority": 1},
    ]
    client = _make_client()
    client._get_nodes_export_cache_nodes = lambda: [dict(n) for n in nodes]
    monkeypatch.setattr(srv, "get_client", lambda: client)
    res = asyncio.run(srv._lightning_rod_from_cache(
        mode="render_tags", root_uuid=root, tags=["topic"], postfix_policy="slice",
        descend_policy="stop_at_child_anchors",
    ))
    md = res.get("preview_markdown") or ""
    # The anchor-boundary seal holds through the funnel's Phase-1 admit AND
    # Phase-3 hydration: the anchor renders as a childless signpost; the
    # behind-the-boundary descendant is never re-inflated.
    assert "Child anchor" in md
    assert "Deep" not in md
    assert "Live" in md


def test_push_to_modal_preserves_postfix_policy_in_blob(monkeypatch):
    client = _make_client()
    sent = {}

    async def _ws_rr(action, **kwargs):
        if action == "apply_slice_in_dock":
            sent.update(kwargs.get("payload") or {})
            return {"success": True}
        raise AssertionError(action)

    client._ws_request_response = _ws_rr
    monkeypatch.setattr(srv, "get_client", lambda: client)

    async def _grace(**_kwargs):
        return object(), object()

    monkeypatch.setattr(srv, "get_ws_connection_with_grace_wait", _grace)
    res = asyncio.run(srv._glimpse_push_slice_to_modal(
        slice_group=None,
        root_uuid=_ROOT,
        sort_by=None,
        sort_direction="asc",
        target_tab=None,
        lightning={
            "mode": "render_tags",
            "tags": ["foo"],
            "postfix_policy": "slice",
            "match_scope": "name_and_note",
        },
    ))
    assert res.get("success") is True, res
    # The pushed render_spec blob carries the policy inside the opaque
    # lightning selection (absence == 'none').
    spec = RenderProjectionSpec.from_render_spec_blob(sent.get("render_spec") or {})
    assert (spec.lightning or {}).get("postfix_policy") == "slice"
    # ...and the ack echoes the pushed selection with the policy.
    assert ((res.get("lightning") or {}).get("postfix_policy")) == "slice"


# ---------------------------------------------------------------------------
# The ONE shared get_slice_group_config reply parser (Finding 5).
# ---------------------------------------------------------------------------
def test_parse_slice_policy_config_defaults_and_validation():
    from workflowy_mcp.f8_slice_policy import parse_slice_policy_config
    d = parse_slice_policy_config({})
    assert d == {
        "auto_expand": "expand",
        "suppress": "suppress",
        "deprecated": "deprecated",
        "global_never_expand": [],
    }
    d2 = parse_slice_policy_config({
        "f8_postfix_auto_expand": "open",
        "f8_postfix_suppress": "hide",
        "f8_postfix_deprecated": "retired",
        # malformed string -> [] (NEVER character-split into bogus stems)
        "f8_global_never_expand_tags": "wfy-suppress",
    })
    assert d2["auto_expand"] == "open"
    assert d2["suppress"] == "hide"
    assert d2["deprecated"] == "retired"
    assert d2["global_never_expand"] == []
    d3 = parse_slice_policy_config({"f8_global_never_expand_tags": ["a", " ", "b"]})
    assert d3["global_never_expand"] == ["a", "b"]


def test_resolve_slice_group_config_uses_shared_parser(monkeypatch):
    client = _make_client()

    async def _gsgc(**_kwargs):
        return {
            "expand_groups": [],
            "active_group": None,
            "f8_global_never_expand_tags": "wfy-suppress",  # malformed string
        }

    client.get_slice_group_config = _gsgc
    monkeypatch.setattr(srv, "get_client", lambda: client)

    async def _grace(**_kwargs):
        return object(), object()

    monkeypatch.setattr(srv, "get_ws_connection_with_grace_wait", _grace)
    groups, active_id, postfixes, global_never, _raw = asyncio.run(
        srv._resolve_slice_group_config(None)
    )
    assert global_never == []  # malformed string is never character-split
    assert postfixes == {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"}


# ---------------------------------------------------------------------------
# POST-SOL-AUDIT Finding 2: Phase 2's deprecated-RESCUE liveness is a
# MEMBERSHIP question and must honor match_scope. Pure-funnel tests (no WS).
# ---------------------------------------------------------------------------
def _funnel_ids(x):
    if not x:
        return set()
    out = {x["id"]}
    for c in x.get("children", []):
        out |= _funnel_ids(c)
    return out


def _mkf(i, name, parent=None, note=""):
    return {"id": i, "name": name, "note": note, "parent_id": parent, "priority": 0}


def test_match_scope_name_deprecated_rescue_uses_title_membership():
    # Under match_scope='name', a note-only tag must NOT rescue a deprecated
    # branch: the connective chain is not self-live, and the only title-member
    # below is inside a suppressed pocket (never live-for-rescue).
    from workflowy_mcp.f8_slice_policy import build_stage1_base_tree
    cache = {n["id"]: n for n in [
        _mkf("r", "Root #foo"),
        _mkf("d", "Dep #foo--deprecated", "r"),
        _mkf("a", "Connective", "d", note="note tag #foo"),
        _mkf("s", "Sup #foo--suppress", "a"),
        _mkf("l", "Live #foo", "s"),
    ]}
    final, plan, meta = build_stage1_base_tree(cache, "r", ["foo"], match_scope="name")
    assert "d" in plan["collapse_deprecated"]          # NOT rescued by the note tag
    assert "d" not in plan["body_suppressed_ids"]
    assert _funnel_ids(final) == {"r"}                  # the dead branch collapses away
    assert meta["pruned_zombie_count"] == 1


def test_match_scope_name_expansion_ancestor_still_wins_over_deprecated():
    # The companion boundary: under an EXPANSION ancestor the deprecated
    # PRE-FILTER (not the rescue) decides -- the node is shown regardless of
    # liveness. The match_scope fix must not disturb that rule.
    from workflowy_mcp.f8_slice_policy import build_stage1_base_tree
    cache = {n["id"]: n for n in [
        _mkf("r", "Root #foo--expand"),
        _mkf("d", "Dep #foo--deprecated", "r"),
        _mkf("a", "Connective", "d", note="note tag #foo"),
        _mkf("s", "Sup #foo--suppress", "a"),
        _mkf("l", "Live #foo", "s"),
    ]}
    final, plan, meta = build_stage1_base_tree(cache, "r", ["foo"], match_scope="name")
    assert "d" not in plan["collapse_deprecated"]      # expansion wins (pre-filter)
    assert "d" in plan["body_suppressed_ids"]          # header-only survivor
    assert _funnel_ids(final) == {"r", "d", "a"}        # suppress pocket carved out
    assert meta["pruned_zombie_count"] == 1


# ---------------------------------------------------------------------------
# POST-SOL-AUDIT follow-up: the promotion pass's ACTIVE-self check also
# honors match_scope (title-only slices are title-only everywhere). The
# deprecated/suppression axes stay operation-policy NAME+NOTE / NAME-only.
# ---------------------------------------------------------------------------
def _promotion_tree():
    # root -> P (note-only #foo) -> D #foo--deprecated -> S #foo--suppress -> M #foo
    return {n["id"]: n for n in [
        _mkf("r", "Root #foo"),
        _mkf("P", "Blank parent", "r", note="note tag #foo"),
        _mkf("D", "Dep #foo--deprecated", "P"),
        _mkf("S", "Sup #foo--suppress", "D"),
        _mkf("M", "Live #foo", "S"),
    ]}


def test_match_scope_name_promotion_not_blocked_by_note_only_parent():
    # Under 'name', P's note-only tag does NOT make it live-for-promotion, so
    # D's dead-limb collapse promotes UP to P (title-only purity): both are
    # pruned, leaving only the root.
    from workflowy_mcp.f8_slice_policy import build_stage1_base_tree
    final, plan, meta = build_stage1_base_tree(
        _promotion_tree(), "r", ["foo"], match_scope="name")
    assert "P" in plan["collapse_deprecated"]          # promoted up the dead limb
    assert "D" not in plan["collapse_deprecated"]
    assert _funnel_ids(final) == {"r"}
    assert meta["pruned_zombie_count"] == 1


def test_match_scope_name_and_note_promotion_blocked_by_live_parent():
    # The SAME tree under the default scope: P's note tag makes it a live
    # member, so the promotion is BLOCKED and the collapse stays at D -- P
    # survives as a connective heading. (The pre-follow-up behavior, locked so
    # the scoped change is confined to the 'name' path.)
    from workflowy_mcp.f8_slice_policy import build_stage1_base_tree
    final, plan, meta = build_stage1_base_tree(
        _promotion_tree(), "r", ["foo"], match_scope="name_and_note")
    assert "D" in plan["collapse_deprecated"]          # collapse stays at D
    assert "P" not in plan["collapse_deprecated"]
    assert _funnel_ids(final) == {"r", "P"}
    assert meta["pruned_zombie_count"] == 1
