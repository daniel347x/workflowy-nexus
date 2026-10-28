"""Chain-1 W4 (F5): sink refusals at every supported read/render front.

For every sink-capable read/render root or handle-list item that misses the
true live universe and names a visible archived node, ``output_file`` or
``push_to_modal=true`` yields the universal ``archived_node_read_only``
envelope with the exact §5.3.2 file/date teaching text BEFORE any file or
modal work — never an ordinary cache-miss hint, never a false
delivery-success envelope.  Handles cover the AP, the full UUID and a
unique-prefix form.  Controls: ``include_archives`` + sink still refuses
with ``invalid_parameter_combination`` before handler work; genuinely
unknown handles and live delivery calls keep their established contracts.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from coherence_harness import make_boundary_capable

from workflowy_mcp import server
from workflowy_mcp.archive_store import ArchiveStore, set_archive_store_for_tests
from workflowy_mcp.client.api_client_etch import WorkFlowyClientEtch
from workflowy_mcp.mcp_api_registry import dispatch_operation
from workflowy_mcp.node_archive import build_archive_document, serialize_archive

SOURCE_CACHE = {"epoch_number": 1, "epoch_id": "e1", "adopted_sequence": 3}

LIVE_ROOT = "dddddddd-0000-0000-0000-000000000001"
LIVE_LEAF = "dddddddd-0000-0000-0000-000000000002"
ARCH_ROOT = "eeeeeeee-0000-0000-0000-0000000000ee"
ARCH_UNIQUE = "cccccccc-0000-0000-0000-000000000003"
AP_CORE = "ARC1"

SINK_MARK = "archived material is never written to a file or pushed to the modal"
CODE = "archived_node_read_only"


def _nest(records):
    return WorkFlowyClientEtch.__new__(WorkFlowyClientEtch)._build_hierarchy(
        list(records)
    )


def _rec(node_id, name="n", parent_id=None, priority=0, note="", **extra):
    record = {
        "id": node_id,
        "name": name,
        "note": note,
        "parent_id": parent_id,
        "priority": priority,
    }
    record.update(extra)
    return record


@pytest.fixture()
def sink_env(tmp_path, monkeypatch):
    document = build_archive_document(
        [
            _rec(ARCH_ROOT, name=f"[ap:{AP_CORE}] Archived root", note="archived"),
            _rec(ARCH_UNIQUE, name="Archived unique head", parent_id=ARCH_ROOT),
        ],
        nest=_nest,
        node_id=ARCH_ROOT,
        name="archived root",
        description=None,
        exported_at="2026-10-01T12:00:00Z",
        source_cache=dict(SOURCE_CACHE),
    )
    (tmp_path / "arch.json").write_bytes(serialize_archive(document).encode("utf-8"))
    store = ArchiveStore(folder_provider=lambda: str(tmp_path))
    set_archive_store_for_tests(store)
    live = [
        _rec(LIVE_ROOT, name="Live root", note="root note"),
        _rec(LIVE_LEAF, name="Live leaf", parent_id=LIVE_ROOT),
    ]
    client = object.__new__(server.WorkFlowyClient)

    async def _get_node(node_id, max_retries=1):
        raise AssertionError(f"get_node must never run for an archived id (got {node_id})")

    client.get_node = _get_node
    make_boundary_capable(client, nodes=live)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)

    async def _no_tags():
        return []

    monkeypatch.setattr(server, "_beacon_symbol_suppress_tags", _no_tags)

    async def _symbol_zero(*a, **k):
        return {"success": True, "match_count": 0, "candidates": [], "truncated": 0}

    client.read_text_snippet_by_symbol = _symbol_zero

    async def _note_session_config(target_tab):
        return ["#wfy-ns"], [], "", {"note_session_tags": ["#wfy-ns"]}

    monkeypatch.setattr(server, "_resolve_note_session_config", _note_session_config)

    async def _slice_group_config(target_tab):
        reply = {"note_session_tags": ["#wfy-ns"], "global_tags": []}
        return [], None, {}, [], reply

    monkeypatch.setattr(server, "_resolve_slice_group_config", _slice_group_config)
    yield {"store": store, "tmp_path": tmp_path, "client": client}
    set_archive_store_for_tests(None)


_REGISTRIES = {
    "node": "NODE_REGISTRY",
    "slice_group": "SLICE_GROUP_REGISTRY",
    "glimpse": "GLIMPSE_REGISTRY",
    "lightning_rod": "LIGHTNING_ROD_REGISTRY",
}


def _run(env, verb, params):
    async def _body():
        return await dispatch_operation(
            getattr(server, _REGISTRIES[verb.split(".")[0]]),
            verb,
            params,
            error_logger=lambda _message: None,
            operation_boundary=server._mcp_operation_boundary,
            mutation_admission=server._mcp_mutation_admission,
            exception_mapper=server._mcp_exception_mapper,
            result_finalizer=server._mcp_operation_result_finalizer,
            operation_invoker=server._mcp_archive_operation_invoker,
        )

    async def _with_boundary():
        from workflowy_mcp.coherence.operation_cache import cache_operation_boundary

        async with cache_operation_boundary(env["client"], "t"):
            return await _body()

    return asyncio.run(_with_boundary())


def _assert_sink_refusal(env, verb, params, handle, out=None):
    result = _run(env, verb, params)
    assert result.get("success") is False, (verb, result)
    error = result["error"]
    assert error["code"] == CODE, (verb, error)
    message = error["message"]
    assert handle in message, (verb, message)
    assert SINK_MARK in message, (verb, message)
    assert "exported 2026-10-01T12:00:00Z)" in message, message
    extra = error.get("extra") or {}
    assert extra.get("archive_file", "").endswith("arch.json"), extra
    assert extra.get("exported_at") == "2026-10-01T12:00:00Z"
    if out is not None:
        assert not out.exists(), (verb, "the sink file must not exist")
    return result


# ---------------------------------------------------------------------------
# node.scry + output_file (the audit's false-success path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handle",
    [
        f"[ap:{AP_CORE}]",  # the AP form
        ARCH_ROOT,  # the full UUID
        ARCH_UNIQUE[:8],  # a unique prefix only the archive answers
    ],
)
def test_scry_output_file_refuses_archived(sink_env, handle):
    out = sink_env["tmp_path"] / "scry-terrain.json"
    _assert_sink_refusal(
        sink_env,
        "node.scry",
        {"root_handle": handle, "output_file": str(out)},
        handle,
        out=out,
    )


def test_scry_unknown_handle_keeps_established_contract(sink_env):
    """A genuinely unknown handle is not the refusal: today's empty/not-found
    flow stands (whatever its historical shape, it is not archived_node_read_only)."""
    out = sink_env["tmp_path"] / "scry-unknown.json"
    result = _run(
        sink_env,
        "node.scry",
        {"root_handle": "99999999-0000-0000-0000-000000009999", "output_file": str(out)},
    )
    error = result.get("error") or {}
    assert error.get("code") != CODE, result


# ---------------------------------------------------------------------------
# glimpse.run — root, render_spec; file and modal
# ---------------------------------------------------------------------------


def test_glimpse_run_root_output_file_refuses(sink_env):
    out = sink_env["tmp_path"] / "glimpse.md"
    _assert_sink_refusal(
        sink_env,
        "glimpse.run",
        {"root_handle": f"[ap:{AP_CORE}]", "output_file": str(out)},
        f"[ap:{AP_CORE}]",
        out=out,
    )


def test_glimpse_run_root_push_to_modal_refuses(sink_env):
    _assert_sink_refusal(
        sink_env,
        "glimpse.run",
        {"root_handle": ARCH_ROOT, "push_to_modal": True},
        ARCH_ROOT,
    )


def test_glimpse_run_render_spec_output_file_refuses(sink_env):
    out = sink_env["tmp_path"] / "glimpse-spec.md"
    spec = {
        "kind": "raw_root",
        "root_uuid": ARCH_ROOT,
        "as_markdown": True,
    }
    _assert_sink_refusal(
        sink_env,
        "glimpse.run",
        {"render_spec": json.dumps(spec), "output_file": str(out)},
        ARCH_ROOT,
        out=out,
    )


# ---------------------------------------------------------------------------
# glimpse.estate — node / bare / qualified entries (file sink)
# ---------------------------------------------------------------------------


def test_estate_node_entry_output_file_refuses(sink_env):
    out = sink_env["tmp_path"] / "estate-node.md"
    _assert_sink_refusal(
        sink_env,
        "glimpse.estate",
        {"handle": {"node": f"[ap:{AP_CORE}]"}, "output_file": str(out)},
        f"[ap:{AP_CORE}]",
        out=out,
    )


def test_estate_bare_handle_output_file_refuses(sink_env):
    out = sink_env["tmp_path"] / "estate-bare.md"
    _assert_sink_refusal(
        sink_env,
        "glimpse.estate",
        {"handle": ARCH_ROOT, "output_file": str(out)},
        ARCH_ROOT,
        out=out,
    )


def test_estate_qualified_subtree_output_file_refuses(sink_env):
    out = sink_env["tmp_path"] / "estate-sub.md"
    _assert_sink_refusal(
        sink_env,
        "glimpse.estate",
        {"handle": {"subtree": ARCH_UNIQUE[:8]}, "output_file": str(out)},
        ARCH_UNIQUE[:8],
        out=out,
    )


# ---------------------------------------------------------------------------
# lightning_rod — render_tags / render_search / render_nodes / render_symbols
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sink", [{"output_file": "lf"}, {"push_to_modal": True}])
def test_render_tags_refuses_archived_root(sink_env, sink):
    params = {"root_handle": f"[ap:{AP_CORE}]", "tags": ["x"]}
    if "output_file" in sink:
        sink["output_file"] = str(sink_env["tmp_path"] / "tags.md")
    params.update(sink)
    _assert_sink_refusal(
        sink_env, "lightning_rod.render_tags", params, f"[ap:{AP_CORE}]"
    )


def test_render_search_output_file_refuses(sink_env):
    out = sink_env["tmp_path"] / "search.md"
    _assert_sink_refusal(
        sink_env,
        "lightning_rod.render_search",
        {
            "root_handle": ARCH_ROOT,
            "query": "anything",
            "output_file": str(out),
        },
        ARCH_ROOT,
        out=out,
    )


@pytest.mark.parametrize(
    "item",
    [f"[ap:{AP_CORE}]", ARCH_ROOT, ARCH_UNIQUE[:8]],
)
@pytest.mark.parametrize("sink", ["file", "modal"])
def test_render_nodes_archived_item_refuses(sink_env, item, sink):
    params = {"symbols": [item]}
    out = None
    if sink == "file":
        out = sink_env["tmp_path"] / "nodes.md"
        params["output_file"] = str(out)
    else:
        params["push_to_modal"] = True
    _assert_sink_refusal(
        sink_env, "lightning_rod.render_nodes", params, item, out=out
    )


def test_render_symbols_archived_root_output_file_refuses(sink_env):
    out = sink_env["tmp_path"] / "symbols.md"
    _assert_sink_refusal(
        sink_env,
        "lightning_rod.render_symbols",
        {
            "symbols": ["anything"],
            "root_handles": [f"[ap:{AP_CORE}]"],
            "output_file": str(out),
        },
        f"[ap:{AP_CORE}]",
        out=out,
    )


def test_render_symbols_archived_item_output_file_refuses(sink_env):
    """A live root with an ARCHIVED item: the item's refusal fires before any
    sink work — one archived item never becomes an empty-success selection."""
    out = sink_env["tmp_path"] / "symbols-item.md"
    _assert_sink_refusal(
        sink_env,
        "lightning_rod.render_symbols",
        {
            "symbols": [ARCH_UNIQUE[:8]],
            "root_handles": [LIVE_ROOT],
            "output_file": str(out),
        },
        ARCH_UNIQUE[:8],
        out=out,
    )


# ---------------------------------------------------------------------------
# slice_group.render — the formal (lens subtree) root entry
# ---------------------------------------------------------------------------


def test_slice_group_render_archived_root_output_file_refuses(sink_env):
    out = sink_env["tmp_path"] / "slice.md"
    _assert_sink_refusal(
        sink_env,
        "slice_group.render",
        {
            "lens": {"kind": "subtree", "root_handle": f"[ap:{AP_CORE}]"},
            "output_file": str(out),
        },
        f"[ap:{AP_CORE}]",
        out=out,
    )


def test_slice_group_render_archived_root_push_to_modal_refuses(sink_env):
    _assert_sink_refusal(
        sink_env,
        "slice_group.render",
        {
            "lens": {"kind": "subtree", "root_handle": ARCH_ROOT},
            "push_to_modal": True,
        },
        ARCH_ROOT,
    )


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


def test_include_archives_with_sink_still_refuses_before_handler(sink_env):
    out = sink_env["tmp_path"] / "include-sink.md"
    result = _run(
        sink_env,
        "glimpse.run",
        {
            "root_handle": LIVE_ROOT,
            "include_archives": True,
            "output_file": str(out),
        },
    )
    assert result.get("success") is False
    error = result["error"]
    assert error["code"] == "invalid_parameter_combination", error
    assert "archived material is never written to a file" in error["message"]
    assert not out.exists()


def test_live_delivery_call_keeps_contract(sink_env):
    """A live root delivered to a file is untouched: success, the file
    written, no archive keys."""
    out = sink_env["tmp_path"] / "live.md"
    result = _run(
        sink_env,
        "glimpse.run",
        {"root_handle": LIVE_ROOT, "output_file": str(out)},
    )
    assert result.get("success") is True, result
    assert out.exists()


# ---------------------------------------------------------------------------
# Chain-1 r2 (F5 remainder) — R2-W1: formal-estate refusals survive every
# render-entry boundary; R2-W2: indirect modal roots/items resolve before
# the command.  Every refusal asserts the exact central envelope, the
# provenance, ZERO file-writer calls (estate/file) or ZERO
# apply_slice_in_dock commands (modal) — never merely an absent file.
# ---------------------------------------------------------------------------


from workflowy_mcp.models.node import WorkFlowyNode  # noqa: E402

ARCH_GROUP_ID = "grp-aaaaaaa1aaaaaaa1aaaaaaa1aaaaaaa1"
LIVE_GROUP_ID = "grp-bbbbbbb2bbbbbbb2bbbbbbb2bbbbbbb2"


@pytest.fixture()
def sink_env_groups(sink_env, monkeypatch):
    """sink_env plus a slice-group config carrying one group whose stored
    root is the ARCHIVE root and one whose stored root is live."""
    groups = [
        {
            "id": ARCH_GROUP_ID,
            "name": "ARCH-GROUP",
            "root_uuid": ARCH_ROOT,
            "prefixes": ["x"],
            "tag_prefixes": ["x"],
        },
        {
            "id": LIVE_GROUP_ID,
            "name": "LIVE-GROUP",
            "root_uuid": LIVE_ROOT,
            "prefixes": ["x"],
            "tag_prefixes": ["x"],
        },
    ]

    async def _config(target_tab):
        reply = {"note_session_tags": ["#wfy-ns"], "global_tags": []}
        return groups, None, {}, [], reply

    monkeypatch.setattr(server, "_resolve_slice_group_config", _config)
    return sink_env


@pytest.fixture()
def writer_calls(sink_env, monkeypatch):
    """Record every estate file write; delegate so live controls still
    produce a real file.  Refusal tests assert ZERO recorded calls."""
    calls: list[str] = []
    real = server._write_resolved_text_file

    def _rec(path, text):
        calls.append(str(path))
        return real(path, text)

    monkeypatch.setattr(server, "_write_resolved_text_file", _rec)
    return calls


def _record_modal_commands(env, monkeypatch):
    """Stub the connected peer + the client's apply_slice_in_dock rail and
    return the recorded command payloads."""
    commands: list = []

    async def _connected(**kwargs):
        return object(), object()

    async def _request(*args, **kwargs):
        commands.append(kwargs.get("payload"))
        return {"success": True, "node_count": 0}

    monkeypatch.setattr(server, "get_ws_connection_with_grace_wait", _connected)
    env["client"]._ws_request_response = _request
    return commands


def _stub_estate_note(env, entries):
    """The live named-estate carrier: LIVE_ROOT's note parses to `entries`."""

    async def live_note(node_id, **kwargs):
        assert node_id == LIVE_ROOT, (
            "get_node must never run for an archived id"
        )
        return WorkFlowyNode.model_validate(
            {
                "id": node_id,
                "name": "Saved estate",
                "note": json.dumps(entries),
            }
        )

    env["client"].get_node = live_note


def _assert_estate_sink_refusal(env, result, handle, out, writer):
    assert result.get("success") is False, result
    error = result["error"]
    assert isinstance(error, dict), result
    assert error["code"] == CODE, error
    message = error["message"]
    assert handle in message, message
    assert SINK_MARK in message, message
    assert "exported 2026-10-01T12:00:00Z)" in message, message
    extra = error.get("extra") or {}
    assert extra.get("archive_file", "").endswith("arch.json"), extra
    assert extra.get("exported_at") == "2026-10-01T12:00:00Z", extra
    assert not out.exists(), "the sink file must not exist"
    assert writer == [], writer


def _formal_spec_entry(alias, root_uuid):
    return {
        "alias": alias,
        "spec": {"kind": "raw_root", "root_uuid": root_uuid, "as_markdown": True},
    }


@pytest.mark.parametrize("carrier", ["note", "json"])
def test_estate_formal_spec_entry_output_file_refuses(
    sink_env, writer_calls, carrier
):
    """(a) a single archived formal raw-root spec refuses the WHOLE file
    sink with the universal envelope — through the live named-estate note
    and a direct formal JSON wrapper alike."""
    entries = [_formal_spec_entry("ARCH", ARCH_ROOT)]
    handle = LIVE_ROOT
    if carrier == "note":
        _stub_estate_note(sink_env, entries)
    else:
        handle = json.dumps(entries)
    out = sink_env["tmp_path"] / f"estate-formal-{carrier}.md"
    result = _run(
        sink_env,
        "glimpse.estate",
        {"handle": handle, "output_file": str(out)},
    )
    _assert_estate_sink_refusal(sink_env, result, ARCH_ROOT, out, writer_calls)


@pytest.mark.parametrize("arch_first", [True, False])
def test_estate_formal_mixed_entries_refuse_both_orders(
    sink_env, writer_calls, arch_first
):
    """(b) one live + one archived formal entry: the archived entry refuses
    the whole call whichever order it rides in — no assembled file, no
    delivery-success envelope."""
    live_entry = _formal_spec_entry("LIVE", LIVE_ROOT)
    arch_entry = _formal_spec_entry("ARCH", ARCH_ROOT)
    entries = [arch_entry, live_entry] if arch_first else [live_entry, arch_entry]
    _stub_estate_note(sink_env, entries)
    out = sink_env["tmp_path"] / (
        "estate-mixed-arch-first.md" if arch_first else "estate-mixed-live-first.md"
    )
    result = _run(
        sink_env,
        "glimpse.estate",
        {"handle": LIVE_ROOT, "output_file": str(out)},
    )
    _assert_estate_sink_refusal(sink_env, result, ARCH_ROOT, out, writer_calls)


def test_estate_named_group_entry_output_file_refuses(
    sink_env_groups, writer_calls
):
    """(c) a stored-GROUP estate entry inherits the refusal through the
    existing funnel (the group's stored root seam)."""
    _stub_estate_note(sink_env_groups, [{"alias": "ARCH", "group": "ARCH-GROUP"}])
    out = sink_env_groups["tmp_path"] / "estate-group.md"
    result = _run(
        sink_env_groups,
        "glimpse.estate",
        {"handle": LIVE_ROOT, "output_file": str(out)},
    )
    _assert_estate_sink_refusal(
        sink_env_groups, result, ARCH_ROOT, out, writer_calls
    )


def test_estate_lens_group_entry_output_file_refuses(
    sink_env_groups, writer_calls
):
    """(c) a render_lens estate entry whose group atom's STORED root is
    archived refuses the whole call (the lens engine's group-atom seam)."""
    entries = [
        {
            "alias": "LENS",
            "spec": {
                "kind": "lightning_rod",
                "lightning": {
                    "mode": "render_lens",
                    "lens": {"kind": "group", "group_handle": "ARCH-GROUP"},
                },
            },
        }
    ]
    _stub_estate_note(sink_env_groups, entries)
    out = sink_env_groups["tmp_path"] / "estate-lens.md"
    result = _run(
        sink_env_groups,
        "glimpse.estate",
        {"handle": LIVE_ROOT, "output_file": str(out)},
    )
    _assert_estate_sink_refusal(
        sink_env_groups, result, ARCH_ROOT, out, writer_calls
    )


def test_estate_missing_nonarchive_entry_with_live_keeps_isolation(
    sink_env, writer_calls
):
    """Control: an ordinary missing (non-archive) formal entry mixed with a
    live entry keeps today's per-entry isolation — success, the live entry
    delivered, the missing one a failed row, never the refusal."""
    entries = [
        _formal_spec_entry("LIVE", LIVE_ROOT),
        _formal_spec_entry("GONE", "99999999-0000-0000-0000-000000009999"),
    ]
    _stub_estate_note(sink_env, entries)
    out = sink_env["tmp_path"] / "estate-missing-control.md"
    result = _run(
        sink_env,
        "glimpse.estate",
        {"handle": LIVE_ROOT, "output_file": str(out)},
    )
    assert result.get("success") is True, result
    assert out.exists()
    assert len(writer_calls) == 1, writer_calls
    estate = result.get("estate") or {}
    assert estate.get("failed") == 1, estate


@pytest.mark.parametrize("item", [ARCH_UNIQUE[:8], f"[ap:{AP_CORE}]"])
def test_render_symbols_push_archived_item_refuses(
    sink_env, monkeypatch, item
):
    """R2-W2: an archived item (unique-prefix or AP form) with a LIVE scope
    root refuses before any apply_slice_in_dock command."""
    commands = _record_modal_commands(sink_env, monkeypatch)
    result = _run(
        sink_env,
        "lightning_rod.render_symbols",
        {
            "symbols": [item],
            "root_handles": [LIVE_ROOT],
            "push_to_modal": True,
        },
    )
    assert result.get("success") is False, result
    error = result["error"]
    assert error["code"] == CODE, error
    assert item in error["message"], error["message"]
    assert SINK_MARK in error["message"], error["message"]
    extra = error.get("extra") or {}
    assert extra.get("archive_file", "").endswith("arch.json"), extra
    assert commands == [], commands


def test_render_symbols_push_archived_root_refuses(sink_env, monkeypatch):
    """R2-W2: an archived root in a render-symbols ROOT list refuses before
    any command."""
    commands = _record_modal_commands(sink_env, monkeypatch)
    result = _run(
        sink_env,
        "lightning_rod.render_symbols",
        {
            "symbols": ["anything"],
            "root_handles": [ARCH_ROOT],
            "push_to_modal": True,
        },
    )
    assert result.get("success") is False, result
    error = result["error"]
    assert error["code"] == CODE, error
    assert ARCH_ROOT in error["message"], error["message"]
    assert commands == [], commands


def test_slice_group_render_group_atom_push_refuses(
    sink_env_groups, monkeypatch
):
    """R2-W2: a named slice-group modal push through a lens GROUP atom —
    the stored root resolves and refuses before the command."""
    commands = _record_modal_commands(sink_env_groups, monkeypatch)
    result = _run(
        sink_env_groups,
        "slice_group.render",
        {
            "lens": {"kind": "group", "group_handle": "ARCH-GROUP"},
            "push_to_modal": True,
        },
    )
    assert result.get("success") is False, result
    error = result["error"]
    assert error["code"] == CODE, error
    assert ARCH_ROOT in error["message"], error["message"]
    assert SINK_MARK in error["message"], error["message"]
    assert commands == [], commands


def test_slice_group_render_nested_group_atom_push_refuses(
    sink_env_groups, monkeypatch
):
    """R2-W2: a recursive merged lens cannot hide an archived effective
    root inside a nested group atom."""
    commands = _record_modal_commands(sink_env_groups, monkeypatch)
    result = _run(
        sink_env_groups,
        "slice_group.render",
        {
            "lens": {
                "kind": "merge",
                "operation": "union",
                "inputs": [
                    {"kind": "subtree", "root_handle": LIVE_ROOT},
                    {"kind": "group", "group_handle": "ARCH-GROUP"},
                ],
            },
            "push_to_modal": True,
        },
    )
    assert result.get("success") is False, result
    error = result["error"]
    assert error["code"] == CODE, error
    assert ARCH_ROOT in error["message"], error["message"]
    assert commands == [], commands


def test_glimpse_run_group_handle_push_refuses(sink_env_groups, monkeypatch):
    """R2-W2: a direct named-group modal push with an archived stored root
    refuses — the config id alone never satisfies the push."""
    commands = _record_modal_commands(sink_env_groups, monkeypatch)
    result = _run(
        sink_env_groups,
        "glimpse.run",
        {"group_handle": "ARCH-GROUP", "push_to_modal": True},
    )
    assert result.get("success") is False, result
    error = result["error"]
    assert error["code"] == CODE, error
    assert ARCH_ROOT in error["message"], error["message"]
    assert SINK_MARK in error["message"], error["message"]
    assert commands == [], commands


def test_glimpse_run_live_group_push_succeeds(sink_env_groups, monkeypatch):
    """Control: a LIVE stored group pushes exactly one command."""
    commands = _record_modal_commands(sink_env_groups, monkeypatch)
    result = _run(
        sink_env_groups,
        "glimpse.run",
        {"group_handle": "LIVE-GROUP", "push_to_modal": True},
    )
    assert result.get("success") is True, result
    assert len(commands) == 1, commands


def test_glimpse_run_override_root_wins_over_archived_default(
    sink_env_groups, monkeypatch
):
    """Control: a live override root is the effective target — the unused
    archived stored default is not classified and the push proceeds."""
    commands = _record_modal_commands(sink_env_groups, monkeypatch)
    result = _run(
        sink_env_groups,
        "glimpse.run",
        {
            "group_handle": "ARCH-GROUP",
            "root_handle": LIVE_ROOT,
            "push_to_modal": True,
        },
    )
    assert result.get("success") is True, result
    assert len(commands) == 1, commands


def test_render_symbols_push_live_symbol_wins(
    sink_env, monkeypatch
):
    """Control: a genuine live symbol match is never refused merely because
    an archive shares a UUID-shaped spelling — the router's live legs run
    first and the push proceeds."""
    commands = _record_modal_commands(sink_env, monkeypatch)

    async def _symbol_live(*args, **kwargs):
        return {
            "success": True,
            "match_count": 1,
            "candidates": [
                {
                    "uuid": LIVE_LEAF,
                    "name": "cccccccc.py",
                    "file_path": "/x/cccccccc.py",
                }
            ],
        }

    sink_env["client"].read_uuid_from_symbol = _symbol_live
    result = _run(
        sink_env,
        "lightning_rod.render_symbols",
        {
            "symbols": [ARCH_UNIQUE[:8]],
            "root_handles": [LIVE_ROOT],
            "push_to_modal": True,
        },
    )
    assert result.get("success") is True, result
    assert len(commands) == 1, commands


def test_slice_group_render_group_atom_output_file_refuses(
    sink_env_groups, writer_calls
):
    """R2-W2 (file side): a lens GROUP atom whose stored root is archived
    refuses the file sink before any render or write."""
    out = sink_env_groups["tmp_path"] / "slice-group-atom.md"
    result = _run(
        sink_env_groups,
        "slice_group.render",
        {
            "lens": {"kind": "group", "group_handle": "ARCH-GROUP"},
            "output_file": str(out),
        },
    )
    _assert_estate_sink_refusal(
        sink_env_groups, result, ARCH_ROOT, out, writer_calls
    )


def test_glimpse_run_group_handle_output_file_refuses(
    sink_env_groups, writer_calls
):
    """R2-W2 (file side): glimpse.run's group scope with an archived stored
    root refuses before any file work (the _glimpse_slice_group_from_cache
    stored-root seam)."""
    out = sink_env_groups["tmp_path"] / "slice-group-file.md"
    result = _run(
        sink_env_groups,
        "glimpse.run",
        {"group_handle": "ARCH-GROUP", "output_file": str(out)},
    )
    _assert_estate_sink_refusal(
        sink_env_groups, result, ARCH_ROOT, out, writer_calls
    )
