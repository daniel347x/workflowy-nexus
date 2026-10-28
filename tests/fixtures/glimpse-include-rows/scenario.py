"""Shared scenario harness for the S1 ``include_rows`` regression tests.

This module is deliberately import-agnostic about WHICH checkout's
``workflowy_mcp`` it exercises: the caller puts the intended ``src`` on
``sys.path`` first (the live worktree for tests, the ``git archive`` of the
pre-S1 base for the capture script).  It owns:

  * the tiny in-memory ``/nodes-export`` corpus (rich enough for timestamps,
    repeated occurrences, a slice group, an override root and an Anchor Dock);
  * the minimal, transport-free fakes for every config read the cache-render
    paths touch (no live tab, no network);
  * the SCENARIO table -- one entry per public dispatch call the S1 protocol
    requires -- and ``run_scenario`` which returns the real agent-facing
    result from ``dispatch_operation``.

The test file and ``capture_legacy.py`` both import this module, so the
pre-change capture and the later assertion exercise the exact same calls.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest import mock

# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------
R = "10000000-0000-4000-8000-000000000001"
A = "10000000-0000-4000-8000-000000000002"
A1 = "10000000-0000-4000-8000-000000000003"
A2 = "10000000-0000-4000-8000-000000000004"
B = "10000000-0000-4000-8000-000000000005"
B1 = "10000000-0000-4000-8000-000000000006"
DOCK = "20000000-0000-4000-8000-000000000001"
DA = "20000000-0000-4000-8000-000000000002"
DB = "20000000-0000-4000-8000-000000000003"
CHILD_DOCK = "20000000-0000-4000-8000-000000000004"
INSIDE_CHILD_DOCK = "20000000-0000-4000-8000-000000000005"

# createdAt / modifiedAt are epoch seconds.  The A branch's leaves and the B
# branch's leaf are DELIBERATELY interleaved in time (created: A1=15 <
# B1=35 < A2=50; modified branch-max: A1=90 < B1=95 < A2=100) so a timestamp
# sort must straddle the shared ancestor "Architecture" and RE-EMIT it -- the
# repeated-occurrence corpus the row list has to mirror.
CACHE_NODES = [
    {"id": R, "name": "Project Root", "note": "Root body", "parent_id": None,
     "priority": 0, "createdAt": 1, "modifiedAt": 1},
    {"id": A, "name": "Architecture #arch--expand", "note": "Arch body",
     "parent_id": R, "priority": 0, "createdAt": 10, "modifiedAt": 40},
    {"id": A1, "name": "Resolver", "note": "Resolver body", "parent_id": A,
     "priority": 0, "createdAt": 15, "modifiedAt": 90},
    {"id": A2, "name": "Cache", "note": "Cache body", "parent_id": A,
     "priority": 1, "createdAt": 50, "modifiedAt": 100},
    {"id": B, "name": "Deployment #deploy", "note": "Deploy body",
     "parent_id": R, "priority": 1, "createdAt": 30, "modifiedAt": 60},
    {"id": B1, "name": "Deploy step", "note": "Deploy step body", "parent_id": B,
     "priority": 0, "createdAt": 35, "modifiedAt": 95},
    {"id": DOCK, "name": "Project Dock #wfy-ns", "note": "Dock body",
     "parent_id": None, "priority": 0, "createdAt": 5, "modifiedAt": 5},
    {"id": DA, "name": "Dock Alpha", "note": "Dock Alpha body", "parent_id": DOCK,
     "priority": 0, "createdAt": 6, "modifiedAt": 7},
    {"id": DB, "name": "Dock Beta", "note": "Dock Beta body", "parent_id": DOCK,
     "priority": 1, "createdAt": 8, "modifiedAt": 9},
    {"id": CHILD_DOCK, "name": "Child Dock #wfy-ns", "note": "Child Dock body",
     "parent_id": DOCK, "priority": 2, "createdAt": 12, "modifiedAt": 13},
    {"id": INSIDE_CHILD_DOCK, "name": "Inside Child Dock",
     "note": "must never render in a Dock render",
     "parent_id": CHILD_DOCK, "priority": 0, "createdAt": 14, "modifiedAt": 15},
]

GROUPS = [
    {"id": "grp-arch", "name": "Architecture", "root_uuid": R,
     "prefixes": ["arch"], "description": ""},
    {"id": "grp-deploy", "name": "Deployment", "root_uuid": R,
     "prefixes": ["deploy"], "description": ""},
]

SLICE_REPLY = {
    "expand_groups": GROUPS,
    "active_group": None,
    "f8_postfix_auto_expand": "expand",
    "f8_postfix_suppress": "suppress",
    "f8_postfix_deprecated": "deprecated",
    "f8_global_never_expand_tags": ["wfy-suppress"],
    "note_session_tags": ["wfy-ns"],
    "note_session_local_postfixes": ["local"],
}


# ---------------------------------------------------------------------------
# Environment patch
# ---------------------------------------------------------------------------
def _make_client(server, boundary_status=None):
    from coherence_harness import make_boundary_capable
    from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus

    client = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    client._get_nodes_export_cache_nodes = lambda: [dict(n) for n in CACHE_NODES]
    return make_boundary_capable(
        client, nodes=CACHE_NODES, boundary_status=boundary_status
    )


@contextlib.contextmanager
def patched_environment(server, *, boundary_status=None):
    """Patch every non-cache read the required scenarios touch."""

    async def fake_note_session_config(target_tab=None):
        return (
            ["wfy-ns"],
            [],
            None,
            {"note_session_local_postfixes": ["local"]},
        )

    async def fake_slice_config(target_tab=None):
        return (
            [dict(g) for g in GROUPS],
            None,
            {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"},
            ["wfy-suppress"],
            dict(SLICE_REPLY),
        )

    async def fake_dock_vocabulary(target_tab=None):
        return (
            ["wfy-ns"],
            {"local_postfixes": ["local"]},
        )

    client = _make_client(server, boundary_status=boundary_status)
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(server, "get_client", lambda: client))
        stack.enter_context(mock.patch.object(server, "_rate_limiter", None))
        stack.enter_context(mock.patch.object(
            server, "_resolve_note_session_config", fake_note_session_config))
        stack.enter_context(mock.patch.object(
            server, "_resolve_slice_group_config", fake_slice_config))
        stack.enter_context(mock.patch.object(
            server, "_read_anchor_dock_vocabulary", fake_dock_vocabulary))
        yield client


# ---------------------------------------------------------------------------
# Scenario table
# ---------------------------------------------------------------------------
# Each entry: (family registry attribute, verb, params).  Everything is a
# cache-backed in-memory Markdown render (the only result shape the switch
# applies to), except the explicitly-named non-Markdown/negative scenarios.
SCENARIOS = {
    # -- required positive scope matrix -----------------------------------
    "raw_root": ("GLIMPSE_REGISTRY", "run", {"root_handle": R}),
    "raw_root_headings": (
        "GLIMPSE_REGISTRY", "run", {"root_handle": R, "headings_only": True}),
    "raw_root_created_asc": (
        "GLIMPSE_REGISTRY", "run",
        {"root_handle": R, "sort_by": "created", "sort_direction": "asc"}),
    "raw_root_modified_desc": (
        "GLIMPSE_REGISTRY", "run",
        {"root_handle": R, "sort_by": "modified", "sort_direction": "desc"}),
    "raw_root_recent": (
        "GLIMPSE_REGISTRY", "run",
        {"root_handle": R, "recent": "newest 2"}),
    "raw_root_empty_window": (
        "GLIMPSE_REGISTRY", "run",
        {"root_handle": R, "recent": "1s"}),
    "slice_group": (
        "GLIMPSE_REGISTRY", "run", {"group_handle": "Architecture"}),
    "slice_override": (
        "GLIMPSE_REGISTRY", "run",
        {"group_handle": "Architecture", "root_handle": A}),
    "simple_dock": (
        "GLIMPSE_REGISTRY", "run", {"render_spec": {"dock": DOCK}}),
    "anchor_dock": (
        "ANCHOR_REGISTRY", "dock.render", {"dock_handle": DOCK}),
    "node_parcel": (
        "GLIMPSE_REGISTRY", "run", {"render_spec": {"node": A1}}),
    "subtree_parcel": (
        "GLIMPSE_REGISTRY", "run", {"render_spec": {"subtree": A}}),
    "tags_parcel": (
        "GLIMPSE_REGISTRY", "run",
        {"render_spec": {"tags": ["arch"], "root": R}}),
    "lens_parcel": (
        "GLIMPSE_REGISTRY", "run",
        {"render_spec": {
            "lightning": {
                "mode": "render_lens",
                "lens": {
                    "kind": "merge",
                    "operation": "union",
                    "inputs": [
                        {"kind": "group", "group_handle": "Architecture"},
                        {"kind": "group", "group_handle": "Deployment"},
                    ],
                },
            },
        }}),
    # -- non-Markdown / incompatible shapes: the switch must NOT create a mode
    "raw_root_full": (
        "GLIMPSE_REGISTRY", "run", {"root_handle": R, "as_markdown": False}),
    "raw_root_output_file": (
        "GLIMPSE_REGISTRY", "run", {"root_handle": R, "output_file": "__TMP__/out.md"}),
    "raw_root_full_headings": (
        "GLIMPSE_REGISTRY", "run",
        {"root_handle": R, "as_markdown": False, "headings_only": True}),
    # -- errors stay errors, never an empty success envelope
    "stale_focus": (
        "GLIMPSE_REGISTRY", "run",
        {"render_spec": {
            "kind": "raw_root",
            "root_uuid": R,
            "focus": {
                "mode": "row",
                "uuid": "99999999-0000-4000-8000-000000000000",
                "ancestor_path": [R],
                "occurrence": 0,
            },
        }}),
    "bad_group": (
        "GLIMPSE_REGISTRY", "run", {"group_handle": "No Such Group"}),
    # -- advisory-active legacy bytes (the SAME render with a hard-dirty
    #    coherence status, so the advisory prefix is part of the string)
    "raw_root_advisory": ("GLIMPSE_REGISTRY", "run", {"root_handle": R}),
}

ADVISORY_SCENARIOS = frozenset({"raw_root_advisory"})


def scenario_params(name, tmp_dir=None):
    """Return a fresh params dict (``__TMP__`` substituted for tmp_dir)."""
    _family, _verb, base = SCENARIOS[name]
    import copy

    params = copy.deepcopy(base)
    if tmp_dir is not None:
        out = params.get("output_file")
        if isinstance(out, str):
            params["output_file"] = out.replace("__TMP__", str(tmp_dir))
    return params


def _registry(server, family):
    return getattr(server, family)


async def _dispatch(server, family, verb, params):
    from workflowy_mcp.mcp_api_registry import dispatch_operation

    return await dispatch_operation(
        _registry(server, family),
        verb,
        params,
        error_logger=lambda *_a: None,
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    )


HARD_STATUS = None


def hard_boundary_status():
    """The advisory-active boundary status the S04 tests use."""
    global HARD_STATUS
    if HARD_STATUS is None:
        from coherence_harness import s04_boundary_status

        HARD_STATUS = s04_boundary_status(
            hard_dirty=True,
            hard_generation=7,
            hard_reason_code="verified_effect_unpublishable",
            hard_transaction_id="tx-include-rows",
        )
    return HARD_STATUS


def run_scenario(server, name, *, include_rows=None, boundary_status=None,
                 tmp_dir=None):
    """Run one public dispatch call and return the agent-facing result."""
    family, verb, _base = SCENARIOS[name]
    params = scenario_params(name, tmp_dir=tmp_dir)
    if include_rows is not None:
        params["include_rows"] = include_rows
    if boundary_status is None and name in ADVISORY_SCENARIOS:
        boundary_status = hard_boundary_status()
    with patched_environment(server, boundary_status=boundary_status):
        return asyncio.run(_dispatch(server, family, verb, params))
