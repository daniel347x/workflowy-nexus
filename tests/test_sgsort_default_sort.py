import asyncio

import pytest

import workflowy_mcp.server as server
from workflowy_mcp.cache_render_spec import RenderProjectionSpec
from workflowy_mcp.server import (
    _compose_render_scope_from_spec,
    _resolve_slice_group_effective_sort,
)


def test_sgsort_normalizer_preserves_none_only_for_slice_specs():
    assert (
        RenderProjectionSpec.normalize_from_glimpse_args(
            slice_group="grp-1", sort_by=None
        ).sort_by
        is None
    )
    assert (
        RenderProjectionSpec.normalize_from_glimpse_args(
            slice_group="grp-1", root_uuid="root", sort_by=None
        ).sort_by
        is None
    )
    assert (
        RenderProjectionSpec.normalize_from_glimpse_args(
            root_uuid="root", sort_by=None
        ).sort_by
        == "none"
    )
    assert (
        RenderProjectionSpec.normalize_from_glimpse_args(
            lightning={"mode": "render_search"}, root_uuid="root", sort_by=None
        ).sort_by
        == "none"
    )
    assert (
        RenderProjectionSpec.normalize_from_glimpse_args(
            slice_group="grp-1", sort_by="none"
        ).sort_by
        == "none"
    )


def test_sgsort_wire_round_trip_preserves_null_vs_none_string():
    inherit = RenderProjectionSpec.normalize_from_glimpse_args(
        slice_group="grp-1", sort_by=None
    )
    explicit_none = RenderProjectionSpec.normalize_from_glimpse_args(
        slice_group="grp-1", sort_by="none"
    )

    assert inherit.to_render_spec_blob()["sort_by"] is None
    assert explicit_none.to_render_spec_blob()["sort_by"] == "none"
    assert (
        RenderProjectionSpec.from_render_spec_blob(
            {"kind": "slice", "group_id": "grp-1", "sort_by": None}
        ).sort_by
        is None
    )
    assert (
        RenderProjectionSpec.from_render_spec_blob(
            {"kind": "slice", "group_id": "grp-1", "sort_by": "none"}
        ).sort_by
        == "none"
    )


def test_sgsort_resolve_helper_inherits_pair_and_preserves_explicit_none():
    chosen = {"default_sort_by": "created", "default_sort_direction": "desc"}

    assert _resolve_slice_group_effective_sort(None, "asc", chosen) == (
        "created",
        "desc",
    )
    assert _resolve_slice_group_effective_sort("none", "desc", chosen) == (
        "none",
        "asc",
    )
    assert _resolve_slice_group_effective_sort("modified", "desc", chosen) == (
        "modified",
        "desc",
    )


def test_sgsort_render_scope_guard_rejects_unresolved_slice_sort():
    unresolved = RenderProjectionSpec.normalize_from_glimpse_args(
        slice_group="grp-1", sort_by=None
    ).with_overrides(root_uuid="root")

    with pytest.raises(RuntimeError, match="Unresolved slice sort"):
        _compose_render_scope_from_spec(unresolved)

    raw = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="root", sort_by=None
    )
    scope = _compose_render_scope_from_spec(raw)
    assert scope["sort_by"] == "none"


def test_sgsort_push_path_preserves_explicit_none_vs_inherit(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.payloads = []

        async def _ws_request_response(self, action, **kwargs):
            self.payloads.append(kwargs["payload"])
            return {"success": True}

    async def fake_resolve_slice_group_config(target_tab):
        return (
            [
                {
                    "id": "grp-1",
                    "name": "G",
                    "prefixes": ["x"],
                    "root_uuid": "root",
                    "default_sort_by": "created",
                    "default_sort_direction": "desc",
                }
            ],
            None,
            {},
            [],
            {},
        )

    async def fake_get_ws_connection_with_grace_wait(*args, **kwargs):
        return object(), object()

    fake_client = FakeClient()
    monkeypatch.setattr(server, "get_client", lambda: fake_client)
    monkeypatch.setattr(server, "_resolve_slice_group_config", fake_resolve_slice_group_config)
    monkeypatch.setattr(
        server,
        "get_ws_connection_with_grace_wait",
        fake_get_ws_connection_with_grace_wait,
    )

    async def run_push(sort_by):
        return await server._glimpse_push_slice_to_modal(
            slice_group="grp-1",
            root_uuid=None,
            sort_by=sort_by,
            sort_direction="asc",
            target_tab=None,
        )

    explicit = asyncio.run(run_push("none"))
    inherit = asyncio.run(run_push(None))

    assert explicit["success"] is True
    assert inherit["success"] is True
    assert fake_client.payloads[0]["render_spec"]["sort_by"] == "none"
    assert fake_client.payloads[1]["render_spec"]["sort_by"] is None


def test_sgsort_dispatch_slice_passes_raw_sort_by_not_internal(monkeypatch):
    """SGSORT3 §4 / SGSORT4 §4 lock: _glimpse_dispatch_spec must hand the RAW
    spec.sort_by (four-state) to the slice resolve-point, NOT sort_by_internal()
    (which would collapse an inherit-null BEFORE the resolver). A pasted blob
    carrying sort_by=None must reach _glimpse_slice_group_from_cache as None.
    """
    captured = {}

    async def fake_slice_from_cache(**kwargs):
        captured["sort_by"] = kwargs.get("sort_by")
        return {"success": True}

    monkeypatch.setattr(server, "_glimpse_slice_group_from_cache", fake_slice_from_cache)

    # An inherit (null) slice request -- the whole point is that None survives.
    inherit_spec = RenderProjectionSpec.from_render_spec_blob(
        {"kind": "slice", "group_id": "grp-1", "sort_by": None}
    )
    assert inherit_spec.sort_by is None  # guard the precondition
    asyncio.run(
        server._glimpse_dispatch_spec(inherit_spec, output_file=None, target_tab=None)
    )
    assert captured["sort_by"] is None

    # An explicit document-order slice request stays the string 'none'.
    explicit_spec = RenderProjectionSpec.from_render_spec_blob(
        {"kind": "slice", "group_id": "grp-1", "sort_by": "none"}
    )
    asyncio.run(
        server._glimpse_dispatch_spec(explicit_spec, output_file=None, target_tab=None)
    )
    assert captured["sort_by"] == "none"


def test_sgsort_update_slice_group_preserves_default_sort(monkeypatch):
    """SGSORT5 §2 lock: a harmless description/tag update must NOT erase a
    saved default sort. The read->modify->persist path carries the two fields
    through for a group that is not being edited on those fields.
    """
    stored = {
        "id": "grp-1",
        "name": "G",
        "prefixes": ["x"],
        "root_uuid": "root",
        "description": "old",
        "default_sort_by": "created",
        "default_sort_direction": "desc",
    }

    async def fake_resolve_slice_group_config(target_tab):
        return (
            [dict(stored)],
            None,
            {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"},
            [],
            {},
        )

    async def fake_get_ws_connection_with_grace_wait(*args, **kwargs):
        return object(), object()

    class FakeClient:
        def __init__(self):
            self.persisted = None

        async def set_slice_groups(self, **kwargs):
            self.persisted = kwargs["expand_groups"]
            # Mirror the tab's persist-then-echo: return the array verbatim.
            return {"expand_groups": kwargs["expand_groups"], "active_group": kwargs.get("active_group")}

    fake_client = FakeClient()
    monkeypatch.setattr(server, "get_client", lambda: fake_client)
    monkeypatch.setattr(server, "_resolve_slice_group_config", fake_resolve_slice_group_config)
    monkeypatch.setattr(
        server,
        "get_ws_connection_with_grace_wait",
        fake_get_ws_connection_with_grace_wait,
    )

    result = asyncio.run(
        server.update_slice_group(group_id="grp-1", description="new description")
    )

    assert result["success"] is True
    # Persisted array retains the untouched default sort pair.
    persisted_group = fake_client.persisted[0]
    assert persisted_group["default_sort_by"] == "created"
    assert persisted_group["default_sort_direction"] == "desc"
    # And the returned public record echoes it.
    assert result["slice_group"]["default_sort_by"] == "created"
    assert result["slice_group"]["default_sort_direction"] == "desc"


def test_sgsort_update_slice_group_edits_only_supplied_default_sort_fields(monkeypatch):
    """Companion to the preservation lock: supplying only default_sort_by edits
    that field while leaving default_sort_direction at its stored value.
    """
    stored = {
        "id": "grp-1",
        "name": "G",
        "prefixes": ["x"],
        "root_uuid": "root",
        "default_sort_by": "none",
        "default_sort_direction": "desc",
    }

    async def fake_resolve_slice_group_config(target_tab):
        return (
            [dict(stored)],
            None,
            {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"},
            [],
            {},
        )

    async def fake_get_ws_connection_with_grace_wait(*args, **kwargs):
        return object(), object()

    class FakeClient:
        async def set_slice_groups(self, **kwargs):
            return {"expand_groups": kwargs["expand_groups"], "active_group": kwargs.get("active_group")}

    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    monkeypatch.setattr(server, "_resolve_slice_group_config", fake_resolve_slice_group_config)
    monkeypatch.setattr(
        server,
        "get_ws_connection_with_grace_wait",
        fake_get_ws_connection_with_grace_wait,
    )

    result = asyncio.run(
        server.update_slice_group(group_id="grp-1", default_sort_by="created")
    )

    assert result["success"] is True
    assert result["slice_group"]["default_sort_by"] == "created"
    # Direction was NOT supplied, so the stored 'desc' is preserved.
    assert result["slice_group"]["default_sort_direction"] == "desc"


def test_sgsort_resolved_slice_sort_writeback_passes_guard():
    """SGSORT6 §1 happy-path lock (the write-back anti-regression, distinct
    from SGSORT4's guard-DOES-raise-on-None lock).

    The modal handler resolves an inherited default sort into a LOCAL and MUST
    write it back onto the render_spec (via raw with_overrides) BEFORE
    _compose_render_scope_from_spec runs. This locks the invariant at its true
    seam: a slice spec whose sort has been RESOLVED to a wire value composes
    render_scope WITHOUT the guard firing, and the echoed sort matches the
    resolved value -- exactly what the handler's write-back must produce for
    the group's default (e.g. 'created') to survive to the echo.
    """
    # An unresolved inherit-null slice spec (what arrives before resolution).
    incoming = RenderProjectionSpec.normalize_from_glimpse_args(
        slice_group="grp-1", sort_by=None
    ).with_overrides(root_uuid="root")
    assert incoming.sort_by is None

    chosen = {"default_sort_by": "created", "default_sort_direction": "desc"}
    resolved_by, resolved_dir = _resolve_slice_group_effective_sort(
        incoming.sort_by, incoming.sort_direction, chosen
    )
    assert (resolved_by, resolved_dir) == ("created", "desc")

    # The handler's write-back: raw with_overrides onto the resolved SCOPE +
    # the resolved wire sort (created/modified stay as-is; 'none' stays 'none').
    render_spec = incoming.with_overrides(
        sort_by=(resolved_by if resolved_by in ("created", "modified") else "none"),
        sort_direction=resolved_dir,
    )
    # Happy path: the §7 guard does NOT raise on the resolved slice spec.
    scope = _compose_render_scope_from_spec(render_spec)
    assert scope["sort_by"] == "created"
    assert scope["sort_direction"] == "desc"

    # And an explicit document-order slice resolves to 'none' and also passes.
    explicit = RenderProjectionSpec.normalize_from_glimpse_args(
        slice_group="grp-1", sort_by="none"
    ).with_overrides(root_uuid="root")
    e_by, e_dir = _resolve_slice_group_effective_sort(
        explicit.sort_by, explicit.sort_direction, chosen
    )
    assert (e_by, e_dir) == ("none", "asc")
    explicit_scope = _compose_render_scope_from_spec(
        explicit.with_overrides(
            sort_by=(e_by if e_by in ("created", "modified") else "none"),
            sort_direction=e_dir,
        )
    )
    assert explicit_scope["sort_by"] == "none"
