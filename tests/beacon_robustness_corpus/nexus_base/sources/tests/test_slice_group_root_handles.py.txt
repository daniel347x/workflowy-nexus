"""Short-root slice-group handles — offline regression suite.

Pins the fix note [ap:1A6512] for the Manager trace [ap:NF21IM]: a slice
group created through ``slice_group.create`` with the house-style short root
(the 8-hex first segment of a UUID) broke the widget's APPLY and copy-search
because those paths read the stored root raw. The fix routes every stored-root
read through the ONE shared resolver and makes create/update store the full
UUID.

All product transports are offline: no network or live Workflowy tab. The production
code was edited by the lead seat and is read live from disk by the test run
(the Cartographer map is deliberately not refreshed mid-unit). The real
standard UUID/prefix/AP-ID resolver is exercised, never mocked: caches are
plain in-memory ``/nodes-export`` node lists and the config/persist transports
are fakes in the repo's established Direction-A idiom.

Coverage map (socket [ap:IO00NV] items 1-7):

1. create / update resolve full UUID | 8-hex segment | unique longer prefix |
   ``[ap:CORE]`` through the real generic-node boundary and persist the FULL
   UUID; unrelated fields and caller-owned groups are untouched; an omitted
   update root leaves a legacy short root stored as-is (readers heal it).
2. missing / ambiguous / empty roots fail loud with ZERO ``set_slice_groups``
   calls.
3. the real ``_handle_render_slice_modal`` body (group cell) resolves a short
   or AP-ID stored root and echoes the full root in ``render_scope``; an
   explicit frame ``root_uuid`` override wins over an unresolvable or shaved
   stored default.
4. the real ``_handle_compile_slice_search`` body compiles from a stored short
   root and enumerates the deprecated middle-segment variant (no silent
   variant loss); a missing root keeps the existing ``variant_incomplete``
   refusal.
5. ``_resolve_stored_slice_group_root`` accepts clean prefixes / AP-IDs,
   rejects a corrective shave of a fabricated UUID tail, and surgically
   discards only that shave's correction echo (caller-owned corrections
   survive).
6. the real Stage-0 inherited lens-root seam adopts a stored short root and
   rejects an inherited shave while explicit roots keep the forgiving policy;
   the scoped-lightning slice-base projection and the group-aware purge
   preview adopt the inherited short root through the same seam.
7. the create/update ``describe`` text teaches every accepted handle form and
   the full-UUID storage promise.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server as srv  # noqa: E402
from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402
from workflowy_mcp.handle_corrections import (  # noqa: E402
    drain_handle_corrections,
    record_handle_correction,
    reset_handle_corrections,
)

# ---------------------------------------------------------------------------
# The incident-shaped cache fixture.
#
# FULL_ROOT begins with the exact 8-hex segment from the Manager trace
# (``c8af1a07``) and carries a leading ``[ap:ROOT01]`` AP-ID in its NAME, so
# one node exercises every accepted handle form. Children deliberately do NOT
# share the 8-hex prefix (otherwise the short form would be ambiguous).
# ---------------------------------------------------------------------------
FULL_ROOT = "c8af1a07-3d81-4fc3-bfbd-652bfbadbac3"
SHORT_ROOT = "c8af1a07"
LONG_PREFIX = "c8af1a07-3d81"
ROOT_AP = "[ap:ROOT01]"
ROOT_NAME = "[ap:ROOT01] Root"
SHARED_8HEX_SIBLING = "c8af1a07-ffff-4aaa-8bbb-000000000009"
OVERRIDE_ROOT = "d0d0cafe-1111-4222-8333-444455556666"
UNKNOWN_ROOT = "ffffffff-ffff-4fff-8fff-ffffffffffff"
# A fabricated UUID tail on a real first segment: the prefix kernel can only
# reach FULL_ROOT by SHAVING the supplied string down to 8 chars.
SHAVED_ROOT = "c8af1a07-dead-beef-4bad-8bad-000000000000"

LIVE_CHILD = "11111111-aaaa-4bbb-8ccc-000000000001"
DEPRECATED_CHILD = "11111111-aaaa-4bbb-8ccc-000000000002"
OVERRIDE_CHILD = "22222222-aaaa-4bbb-8ccc-000000000001"

UNRELATED_FULL = "a2e552e3-eb42-4438-ba75-4ab7f849eb4f"


def _node(nid, name, parent_id=None, priority=0, note=""):
    return {
        "id": nid,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
    }


def _cache_nodes(*, include_shared_sibling=False):
    nodes = [
        _node(FULL_ROOT, ROOT_NAME),
        _node(LIVE_CHILD, "Live child #foo", FULL_ROOT, 0, "live body"),
        _node(
            DEPRECATED_CHILD,
            "Old thing #foo-middle--deprecated",
            FULL_ROOT,
            1,
        ),
        _node(OVERRIDE_ROOT, "Override root"),
        _node(OVERRIDE_CHILD, "Override child #foo", OVERRIDE_ROOT, 0, "override body"),
    ]
    if include_shared_sibling:
        nodes.append(_node(SHARED_8HEX_SIBLING, "Second c8af1a07 node"))
    return nodes


CACHE_NODES = _cache_nodes()
CACHE_MAP = {n["id"]: n for n in CACHE_NODES}
AMBIGUOUS_CACHE_NODES = _cache_nodes(include_shared_sibling=True)


def _group(
    gid="grp-short",
    name="Short Root",
    root_uuid=SHORT_ROOT,
    prefixes=("foo",),
    description="OLD DESC",
):
    return {
        "id": gid,
        "name": name,
        "root_uuid": root_uuid,
        "prefixes": list(prefixes),
        "description": description,
        "default_sort_by": "none",
        "default_sort_direction": "asc",
    }


def _config_reply(groups, active=None):
    """A get_slice_group_config reply in the established test shape."""
    return {
        "expand_groups": copy.deepcopy(list(groups)),
        "active_group": active,
        "f8_postfix_auto_expand": "expand",
        "f8_postfix_suppress": "suppress",
        "f8_postfix_deprecated": "deprecated",
        "f8_global_never_expand_tags": ["wfy-suppress"],
        "note_session_tags": ["wfy-ns"],
        "note_session_local_postfixes": ["local"],
    }


class FakeSliceGroupChannel:
    """Offline Direction-A fake: live node cache + config read/persist.

    ``stored_groups`` advances on every acked ``set_slice_groups``; the
    ``_get_nodes_export_cache_nodes`` cache backs the real resolver
    (create/update now validate the root through it).
    """

    def __init__(self, groups, active_group=None, cache_nodes=None):
        self.stored_groups = copy.deepcopy(groups)
        self.stored_active = active_group
        self._cache_nodes = [dict(n) for n in (cache_nodes or [])]
        self.reads = 0
        self.writes = 0

    def _get_nodes_export_cache_nodes(self):
        return [dict(n) for n in self._cache_nodes]

    async def get_slice_group_config(self, **_kwargs):
        self.reads += 1
        return {
            "success": True,
            "expand_groups": copy.deepcopy(self.stored_groups),
            "active_group": self.stored_active,
        }

    async def set_slice_groups(self, *, expand_groups, active_group=None, **_kwargs):
        self.writes += 1
        self.stored_groups = copy.deepcopy(expand_groups)
        self.stored_active = active_group
        return {
            "success": True,
            "expand_groups": copy.deepcopy(self.stored_groups),
            "active_group": self.stored_active,
        }


def _install_channel(monkeypatch, channel):
    async def _grace(*_args, **_kwargs):
        return object(), object()

    monkeypatch.setattr(srv, "get_client", lambda: channel)
    monkeypatch.setattr(srv, "get_ws_connection_with_grace_wait", _grace)


class _CaptureWS:
    """Captures the single JSON reply a WS handler sends."""

    def __init__(self):
        self.sent = []

    async def send(self, payload):
        try:
            self.sent.append(json.loads(payload))
        except Exception:
            self.sent.append({"_raw": payload})


def _make_cache_client(cache_nodes):
    """A boundary-capable fake client whose /nodes-export cache is real data."""
    from coherence_harness import make_boundary_capable

    client = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    client._get_nodes_export_cache_nodes = lambda: [dict(n) for n in cache_nodes]
    return make_boundary_capable(client, nodes=cache_nodes)


_ENVELOPE_KEYS = frozenset({"action", "request_id", "endpoint_id", "max_prerender_chars"})


def _wire_frame(frame):
    """Pack a test frame's scope keys into the ONE nested render_spec blob."""
    if "render_spec" in frame:
        return dict(frame)
    scope = {k: v for k, v in frame.items() if k not in _ENVELOPE_KEYS}
    envelope = {k: v for k, v in frame.items() if k in _ENVELOPE_KEYS}
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        group_id=scope.get("group_id") or None,
        root_uuid=scope.get("root_uuid") or None,
        match_scope=str(scope.get("match_scope") or "name_and_note"),
        descend_policy=str(scope.get("descend_policy") or "all"),
    )
    return {**envelope, "render_spec": spec.to_render_spec_blob()}


@pytest.fixture(autouse=True)
def _fresh_config_lock(monkeypatch):
    """A per-test slice-group config lock (fresh asyncio loop per test)."""
    monkeypatch.setattr(srv, "_SLICE_GROUP_CONFIG_LOCK", asyncio.Lock(), raising=False)


@pytest.fixture(autouse=True)
def _clean_handle_corrections():
    reset_handle_corrections()
    yield
    reset_handle_corrections()


# ---------------------------------------------------------------------------
# 1. create / update store the FULL UUID for every accepted handle form.
# ---------------------------------------------------------------------------

HANDLE_FORMS = [
    ("full_uuid", FULL_ROOT),
    ("short_8hex", SHORT_ROOT),
    ("unique_longer_prefix", LONG_PREFIX),
    ("ap_id", ROOT_AP),
]


@pytest.mark.parametrize("form,candidate", HANDLE_FORMS, ids=[f[0] for f in HANDLE_FORMS])
def test_create_stores_full_uuid_for_every_handle_form(monkeypatch, form, candidate):
    original_a = _group(gid="g-keep-a", name="Keep A", root_uuid=OVERRIDE_ROOT)
    original_b = _group(gid="g-keep-b", name="Keep B", root_uuid=OVERRIDE_ROOT)
    channel = FakeSliceGroupChannel(
        [original_a, original_b], active_group="g-keep-a", cache_nodes=CACHE_NODES
    )
    _install_channel(monkeypatch, channel)

    result = asyncio.run(
        srv._slice_group_create_core(name="New group", tags=["foo"], root_uuid=candidate)
    )

    assert result["success"] is True, result
    assert channel.writes == 1
    # Caller-owned groups are byte-identical after the append.
    assert channel.stored_groups[0] == original_a
    assert channel.stored_groups[1] == original_b
    created = channel.stored_groups[-1]
    assert created["root_uuid"] == FULL_ROOT
    assert result["slice_group"]["root_uuid"] == FULL_ROOT


@pytest.mark.parametrize("form,candidate", HANDLE_FORMS, ids=[f[0] for f in HANDLE_FORMS])
def test_update_stores_full_uuid_for_every_handle_form(monkeypatch, form, candidate):
    original = _group(gid="g1", root_uuid=OVERRIDE_ROOT, description="WAS")
    channel = FakeSliceGroupChannel([original], active_group="g1", cache_nodes=CACHE_NODES)
    _install_channel(monkeypatch, channel)

    result = asyncio.run(srv._slice_group_update_core(group_id="g1", root_uuid=candidate))

    assert result["success"] is True, result
    assert channel.writes == 1
    stored = channel.stored_groups[0]
    assert stored["root_uuid"] == FULL_ROOT
    # Unrelated fields are untouched by the root-only update.
    assert stored["name"] == original["name"]
    assert stored["prefixes"] == original["prefixes"]
    assert stored["description"] == "WAS"
    assert result["slice_group"]["root_uuid"] == FULL_ROOT


def test_update_omitting_root_keeps_a_legacy_short_stored_root(monkeypatch):
    """Omitting root_handle must not touch the stored value: a legacy short
    root legitimately remains stored (readers heal it at read time)."""
    channel = FakeSliceGroupChannel(
        [_group(gid="g1", root_uuid=SHORT_ROOT, description="WAS")],
        active_group="g1",
        cache_nodes=CACHE_NODES,
    )
    _install_channel(monkeypatch, channel)

    result = asyncio.run(srv._slice_group_update_core(group_id="g1", description="NOW"))

    assert result["success"] is True, result
    stored = channel.stored_groups[0]
    assert stored["root_uuid"] == SHORT_ROOT
    assert stored["description"] == "NOW"


# ---------------------------------------------------------------------------
# 2. missing / ambiguous / empty roots fail loud before any config write.
# ---------------------------------------------------------------------------


def test_create_missing_root_fails_loud_with_zero_writes(monkeypatch):
    channel = FakeSliceGroupChannel([_group()], active_group="grp-short", cache_nodes=CACHE_NODES)
    _install_channel(monkeypatch, channel)

    result = asyncio.run(
        srv._slice_group_create_core(name="New group", tags=["foo"], root_uuid=UNKNOWN_ROOT)
    )

    assert result["success"] is False, result
    assert "did not resolve" in result["error"]
    assert channel.writes == 0
    # Resolution happens before the config read, so not even a read occurs.
    assert channel.reads == 0


def test_create_empty_root_fails_loud_with_zero_writes(monkeypatch):
    channel = FakeSliceGroupChannel([_group()], active_group="grp-short", cache_nodes=CACHE_NODES)
    _install_channel(monkeypatch, channel)

    for empty in ("", "   "):
        result = asyncio.run(
            srv._slice_group_create_core(name="New group", tags=["foo"], root_uuid=empty)
        )
        assert result["success"] is False, result
        assert "root_uuid is required" in result["error"]

    assert channel.writes == 0


def test_create_ambiguous_short_root_fails_loud_with_zero_writes(monkeypatch):
    channel = FakeSliceGroupChannel(
        [_group()], active_group="grp-short", cache_nodes=AMBIGUOUS_CACHE_NODES
    )
    _install_channel(monkeypatch, channel)

    result = asyncio.run(
        srv._slice_group_create_core(name="New group", tags=["foo"], root_uuid=SHORT_ROOT)
    )

    assert result["success"] is False, result
    assert "ambiguous" in result["error"].lower()
    assert channel.writes == 0
    assert channel.reads == 0


def test_update_missing_root_fails_loud_with_zero_writes(monkeypatch):
    channel = FakeSliceGroupChannel(
        [_group(gid="g1")], active_group="g1", cache_nodes=CACHE_NODES
    )
    _install_channel(monkeypatch, channel)

    result = asyncio.run(srv._slice_group_update_core(group_id="g1", root_uuid=UNKNOWN_ROOT))

    assert result["success"] is False, result
    assert "did not resolve" in result["error"]
    assert channel.writes == 0
    assert channel.stored_groups[0]["root_uuid"] == SHORT_ROOT


def test_update_empty_root_fails_loud_with_zero_writes(monkeypatch):
    channel = FakeSliceGroupChannel(
        [_group(gid="g1")], active_group="g1", cache_nodes=CACHE_NODES
    )
    _install_channel(monkeypatch, channel)

    result = asyncio.run(srv._slice_group_update_core(group_id="g1", root_uuid="   "))

    assert result["success"] is False, result
    assert "non-empty" in result["error"]
    assert channel.writes == 0


def test_update_ambiguous_short_root_fails_loud_with_zero_writes(monkeypatch):
    channel = FakeSliceGroupChannel(
        [_group(gid="g1")], active_group="g1", cache_nodes=AMBIGUOUS_CACHE_NODES
    )
    _install_channel(monkeypatch, channel)

    result = asyncio.run(srv._slice_group_update_core(group_id="g1", root_uuid=SHORT_ROOT))

    assert result["success"] is False, result
    assert "ambiguous" in result["error"].lower()
    assert channel.writes == 0
    assert channel.stored_groups[0]["root_uuid"] == SHORT_ROOT


# ---------------------------------------------------------------------------
# 3. APPLY slice cell: the real modal handler resolves the inherited root.
# ---------------------------------------------------------------------------


def _run_modal(frame, monkeypatch, *, cache_nodes=None, config=None):
    cache_nodes = CACHE_NODES if cache_nodes is None else cache_nodes
    config = _config_reply([_group()]) if config is None else config
    ws = _CaptureWS()
    client = _make_cache_client(cache_nodes)
    monkeypatch.setattr(srv, "get_client", lambda: client)

    async def read_nested(**_kwargs):
        return copy.deepcopy(config), None

    monkeypatch.setattr(srv, "_read_slice_group_config_nested", read_nested)
    asyncio.run(srv._handle_render_slice_modal(_wire_frame(frame), ws))
    assert ws.sent, "handler sent no reply"
    return ws.sent[-1]


def _apply_frame(group_id="grp-short", **scope):
    return {
        "action": "render_slice_modal",
        "request_id": "req-short-root",
        "endpoint_id": "tab-1",
        "group_id": group_id,
        **scope,
    }


def test_apply_modal_resolves_stored_short_root(monkeypatch):
    reply = _run_modal(_apply_frame(), monkeypatch)

    assert reply.get("success") is True, reply
    assert reply["render_scope"]["root_uuid"] == FULL_ROOT
    markdown = reply.get("markdown") or ""
    assert "Live child" in markdown


def test_apply_modal_resolves_stored_ap_id_root(monkeypatch):
    config = _config_reply([_group(root_uuid=ROOT_AP)])
    reply = _run_modal(_apply_frame(), monkeypatch, config=config)

    assert reply.get("success") is True, reply
    assert reply["render_scope"]["root_uuid"] == FULL_ROOT
    assert "Live child" in (reply.get("markdown") or "")


@pytest.mark.parametrize(
    "stored_root", [UNKNOWN_ROOT, SHAVED_ROOT], ids=["unresolvable", "shaved"]
)
def test_apply_modal_frame_root_override_wins_over_bad_stored_default(
    monkeypatch, stored_root
):
    """A frame root_handle (the push/override cell) keeps its explicit-handle
    precedence and its forgiving policy, even when the group's stored default
    is unresolvable or a corrective shave."""
    config = _config_reply([_group(root_uuid=stored_root)])
    reply = _run_modal(
        _apply_frame(root_uuid=OVERRIDE_ROOT), monkeypatch, config=config
    )

    assert reply.get("success") is True, reply
    assert reply["render_scope"]["root_uuid"] == OVERRIDE_ROOT
    markdown = reply.get("markdown") or ""
    assert "Override child" in markdown
    assert "Live child" not in markdown


# ---------------------------------------------------------------------------
# 4. Copy-search cell: the real compile handler resolves the inherited root.
# ---------------------------------------------------------------------------


def _run_compile(monkeypatch, *, config, group_id="grp-short"):
    ws = _CaptureWS()
    client = _make_cache_client(CACHE_NODES)
    monkeypatch.setattr(srv, "get_client", lambda: client)

    async def read_nested(**_kwargs):
        return copy.deepcopy(config), None

    monkeypatch.setattr(srv, "_read_slice_group_config_nested", read_nested)
    asyncio.run(
        srv._handle_compile_slice_search(
            {
                "action": "compile_slice_search",
                "request_id": "req-compile",
                "endpoint_id": "tab-1",
                "group_id": group_id,
            },
            ws,
        )
    )
    assert ws.sent, "handler sent no reply"
    return ws.sent[-1]


def test_compile_search_resolves_stored_short_root_and_keeps_middle_variants(monkeypatch):
    reply = _run_compile(monkeypatch, config=_config_reply([_group()]))

    assert reply.get("success") is True, reply
    expression = reply.get("expression") or ""
    assert "#foo" in expression
    # The base negation AND the middle-segment variant negation are both
    # present: no silent variant loss on a short-rooted group.
    assert "-#foo--deprecated" in expression
    assert "-#foo-middle--deprecated" in expression


@pytest.mark.parametrize(
    "stored_root", ["", UNKNOWN_ROOT], ids=["no_root", "absent_root"]
)
def test_compile_search_missing_root_keeps_variant_incomplete_refusal(
    monkeypatch, stored_root
):
    reply = _run_compile(
        monkeypatch, config=_config_reply([_group(root_uuid=stored_root)])
    )

    assert reply.get("success") is False, reply
    assert reply.get("variant_incomplete") is True


# ---------------------------------------------------------------------------
# 5. The shared stored-root policy seam itself.
# ---------------------------------------------------------------------------


def test_resolve_stored_root_accepts_clean_prefix_and_ap_id():
    clean = srv._resolve_stored_slice_group_root(LONG_PREFIX, CACHE_MAP)
    assert clean["resolved_uuid"] == FULL_ROOT
    assert clean["input_kind"] == "uuid_prefix"
    # A clean prefix matched at its own full length: no correction echo.
    assert drain_handle_corrections() == []

    ap = srv._resolve_stored_slice_group_root(ROOT_AP, CACHE_MAP)
    assert ap["resolved_uuid"] == FULL_ROOT
    assert ap["resolved_via"] == "anchor_point"
    assert drain_handle_corrections() == []


def test_resolve_stored_root_rejects_shave_and_discards_only_its_echo():
    # A legitimate correction recorded earlier in the same mixed operation
    # must survive the surgical discard.
    record_handle_correction(
        supplied="unrelated-handle",
        resolved_uuid=UNRELATED_FULL,
        name="Unrelated",
        short="a2e552e3",
    )

    outcome = srv._resolve_stored_slice_group_root(SHAVED_ROOT, CACHE_MAP)

    assert outcome.get("resolved_uuid") is None
    assert outcome.get("success") is False
    assert outcome.get("nodes_by_id") == CACHE_MAP
    # The rejected shave's speculative echo is gone; the unrelated entry stays.
    drained = drain_handle_corrections()
    assert [entry["supplied"] for entry in drained] == ["unrelated-handle"]


# ---------------------------------------------------------------------------
# 6. Stage-0 inherited lens root + scoped-lightning + group-aware purge.
# ---------------------------------------------------------------------------


def test_stage0_inherited_root_adopts_short_and_rejects_shave():
    snapshot = srv._build_lens_stage0_snapshot_from_config(
        _config_reply([_group()]), cache_by_id=CACHE_MAP
    )

    assert snapshot.context.resolve_inherited_root(SHORT_ROOT) == FULL_ROOT
    assert snapshot.context.resolve_root(SHORT_ROOT) == FULL_ROOT
    assert drain_handle_corrections() == []

    # An inherited stored identity never retargets through a shave...
    with pytest.raises(ValueError):
        snapshot.context.resolve_inherited_root(SHAVED_ROOT)
    assert drain_handle_corrections() == []

    # ...while an explicit override root keeps the forgiving policy (echo kept).
    assert snapshot.context.resolve_root(SHAVED_ROOT) == FULL_ROOT
    assert [entry["supplied"] for entry in drain_handle_corrections()] == [SHAVED_ROOT]


def _tree_names(tree):
    names = [str(tree.get("name") or "")]
    for child in tree.get("children") or []:
        if isinstance(child, dict):
            names.extend(_tree_names(child))
    return names


def test_scoped_lightning_slice_base_adopts_inherited_short_root(monkeypatch):
    client = _make_cache_client(CACHE_NODES)
    monkeypatch.setattr(srv, "get_client", lambda: client)

    async def read_nested(**_kwargs):
        return _config_reply([_group()]), None

    monkeypatch.setattr(srv, "_read_slice_group_config_nested", read_nested)
    spec = RenderProjectionSpec.normalize_from_glimpse_args(group_id="grp-short")

    tree, err = asyncio.run(
        srv._project_slice_base_tree_for_scoped_lightning(
            base_render_spec=spec.to_render_spec_blob(), endpoint_id=None
        )
    )

    assert err is None, err
    assert tree["id"] == FULL_ROOT
    assert any("Live child" in name for name in _tree_names(tree))


def test_group_aware_purge_resolves_inherited_short_root(monkeypatch):
    snapshot = srv._build_lens_stage0_snapshot_from_config(
        _config_reply([_group()]), cache_by_id=CACHE_MAP
    )

    async def fake_snapshot(*_args, **_kwargs):
        return snapshot

    monkeypatch.setattr(srv, "_build_lens_stage0_snapshot", fake_snapshot)

    preview = asyncio.run(srv._purge_preview_impl(group_handle="grp-short"))

    assert preview.get("success") is True, preview
    assert preview["scope"]["start_ids"] == [FULL_ROOT]


# ---------------------------------------------------------------------------
# 7. Registry describe text for create / update.
# ---------------------------------------------------------------------------


def test_create_and_update_describe_teach_handle_forms_and_storage_promise():
    registry = srv.build_slice_group_registry()

    create_schema = json.dumps(registry.describe_full("create")["params_schema"])
    for expected in (
        "8-hex first segment",
        "unique longer prefix",
        "[ap:CORE]",
        "resolved through the standard node-handle resolver",
        "stored as the full UUID",
        "unresolvable or ambiguous",
    ):
        assert expected in create_schema, (expected, create_schema)

    update_schema = json.dumps(registry.describe_full("update")["params_schema"])
    for expected in (
        "8-hex first segment",
        "unique longer prefix",
        "[ap:CORE]",
        "stored as the full UUID",
        "unresolvable or ambiguous",
    ):
        assert expected in update_schema, (expected, update_schema)
