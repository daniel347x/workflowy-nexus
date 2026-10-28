"""S04 Remediation Chain 5 (work-order item 3, EXPANDED per the authorized
Option-B scope amendment) — the legacy wrapper repaired: the mutation gate
and the read advisory are LIVE on the legacy profile.

Two latent defects made the wrapper's branches dead at pristine `a77e8ea`:

- DEFECT 1 (empty canonical lookup): ``_legacy_canonical_operations()``
  read a nonexistent ``canonical`` attribute off ``LegacyToolMapping``
  (which carries ``routes``/``targets``), so the lookup was EMPTY and
  ``_legacy_admission_spec()`` returned ``None`` for every non-Lightning
  legacy tool — the S04 A4 hard-dirty mutation gate never fired for
  ``workflowy_edit_node`` / ``workflowy_add_tag`` / ``workflowy_etch`` /
  ... on the legacy profile.
- DEFECT 2 (advisory timing): the wrapper applied the read advisory AFTER
  the ``async with _mcp_operation_boundary(...)`` block exited, when the
  ContextVar was already reset and ``current_boundary_status()`` was
  None — the application no-op'd even when a spec resolved.

Chain 5 (Option B): the lookup is derived from ``LegacyToolMapping.routes``
(one unqualified route per ordinary row; malformed rows raise loudly),
the advisory is shaped INSIDE the boundary while the captured status is
live, and the family skip (``glimpse`` / ``lightning_rod``) is retained —
their internal render seams own provenance-sensitive advisory shaping.

The ORIGINAL item-3 premise (a false frozen-DOM warning re-added by the
wrapper) is DISPROVEN at pristine `a77e8ea` (both branches were dead) and
is retracted; the frozen-DOM and cache-derived GLIMPSE tests below are
BEHAVIOR LOCKS, not baseline defect reproductions.

Required-test mutation note (step 4a): the mutation-refusal tests and the
non-render advisory test were verified to FAIL against the pre-fix code
(production stashed to the a77e8ea baseline); see the Chain-5 result
anchor for the full evidence table.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from coherence_harness import (
    ACCOUNT_A,
    make_boundary_capable,
    make_coherence_root,
    s04_boundary_status,
    s04_raise_barrier,
)

import workflowy_mcp.coherence.operation_cache as _opc
from workflowy_mcp import server
from workflowy_mcp.cache_render_spec import RenderProjectionSpec
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
from workflowy_mcp.coherence import COHERENCE_READ_ADVISORY_LINE
from workflowy_mcp.mcp_api_registry import dispatch_operation

HARD = s04_boundary_status(
    hard_dirty=True,
    hard_generation=7,
    hard_reason_code="verified_effect_unpublishable",
    hard_transaction_id="tx-advisory",
)

_ROOT = "33333333-1111-bbbb-cccc-000000000001"
_CHILD_A = "33333333-2222-bbbb-cccc-000000000002"
_CHILD_B = "33333333-3333-bbbb-cccc-000000000003"

_CACHE_NODES = [
    {"id": _ROOT, "name": "Frozen Anchor Root", "note": "", "parent_id": None,
     "priority": 0, "createdAt": 1000, "modifiedAt": 1000},
    {"id": _CHILD_A, "name": "Alpha point", "note": "body A", "parent_id": _ROOT,
     "priority": 0, "createdAt": 2000, "modifiedAt": 2000},
    {"id": _CHILD_B, "name": "Beta point", "note": "body B", "parent_id": _ROOT,
     "priority": 1, "createdAt": 1500, "modifiedAt": 1500},
]


def _client(status, *, nodes=None):
    client = object.__new__(server.WorkFlowyClient)
    return make_boundary_capable(
        client, nodes=(_CACHE_NODES if nodes is None else nodes), boundary_status=status
    )


def _patch_client(monkeypatch, status, *, nodes=None):
    client = _client(status, nodes=nodes)
    monkeypatch.setattr(server, "get_client", lambda: client)
    return client


def _barrier_paths(tmp_path: Path, monkeypatch, *, raise_barrier: bool = True):
    """A REAL coherence root; the barrier is raised (or not) and the
    server's admission-paths hook is routed to it."""
    paths = make_coherence_root(tmp_path / "coh", ACCOUNT_A)
    if raise_barrier:
        s04_raise_barrier(paths, transaction_id="tx-legacy-gate")
    monkeypatch.setattr(server, "_admission_coherence_paths", lambda: paths)
    return paths


def _captured_tree():
    return {
        "root": {"id": _ROOT, "name": "Frozen Anchor Root", "note": ""},
        "children": [
            {"id": _CHILD_A, "name": "Alpha point", "note": "body A", "children": []},
            {"id": _CHILD_B, "name": "Beta point", "note": "body B", "children": []},
        ],
    }


def _install_modal_reply(monkeypatch, reply: dict) -> None:
    async def _fake_grace(*_a, **_kw):
        return ("ws-conn", "ws-queue")

    monkeypatch.setattr(server, "get_ws_connection_with_grace_wait", _fake_grace)

    async def _fake_ws_request_response(self, action, **k):
        return reply

    monkeypatch.setattr(
        WorkFlowyClientNexus, "_ws_request_response", _fake_ws_request_response
    )


def _dom_capture_modal_reply() -> dict:
    return {
        "success": True,
        "modal_payload_kind": "dom_captured_tree",
        "captured_tree": _captured_tree(),
        "root_uuid": _ROOT,
        "focus": None,
        "sort_by": "none",
        "sort_direction": "asc",
        "since": None,
        "headings_only": False,
    }


def _cache_render_spec_modal_reply() -> dict:
    blob = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=_ROOT
    ).to_render_spec_blob()
    return {
        "success": True,
        "modal_payload_kind": "cache_render_spec",
        "render_spec": blob,
        "focus": None,
        "sort_by": "none",
        "sort_direction": "asc",
        "since": None,
        "headings_only": False,
    }


def _wrapped_legacy_glimpse():
    return server._wrap_legacy_cache_operation_boundary(
        server.glimpse, "workflowy_glimpse"
    )


def _expected_recovery() -> dict:
    return {"family": "system", "verb": "cache.refresh", "params": {}}


# ---------------------------------------------------------------------------
# Required test: legacy workflowy_edit_node under hard dirt — the handler is
# NEVER called and the typed coherence_barrier_active refusal is returned.
# (BASELINE-SENSITIVE: fails at pristine a77e8ea — the gate was dead.)
# ---------------------------------------------------------------------------


def test_legacy_edit_node_refuses_under_hard_dirt_handler_never_called(
    tmp_path, monkeypatch
):
    _barrier_paths(tmp_path, monkeypatch)
    calls: list = []

    async def probe(*_a, **_kw):
        calls.append(1)
        return {"success": True}

    wrapped = server._wrap_legacy_cache_operation_boundary(probe, "workflowy_edit_node")
    result = asyncio.run(wrapped(node_id="A", name="x"))

    assert calls == []  # the handler never executed
    assert result["success"] is False
    assert result["error_code"] == "coherence_barrier_active"
    assert "coherence" in result["error"]
    extra = result["extra"]
    assert extra["refresh_required"] is True
    assert extra["hard_dirty"] is True
    assert extra["dirty_generation"] == 1
    assert extra["reason_code"] == "journal_publication_failed"
    assert extra["transaction_id"] == "tx-legacy-gate"
    assert extra["recovery"] == _expected_recovery()


def test_legacy_add_tag_refuses_under_hard_dirt_handler_never_called(
    tmp_path, monkeypatch
):
    """A second non-Lightning mutation family (required by the expanded
    scope): the gate fires identically."""
    _barrier_paths(tmp_path, monkeypatch)
    calls: list = []

    async def probe(*_a, **_kw):
        calls.append(1)
        return {"success": True}

    wrapped = server._wrap_legacy_cache_operation_boundary(probe, "workflowy_add_tag")
    result = asyncio.run(wrapped(node_id="A", tag="t"))

    assert calls == []
    assert result["success"] is False
    assert result["error_code"] == "coherence_barrier_active"
    assert result["extra"]["transaction_id"] == "tx-legacy-gate"
    assert result["extra"]["recovery"] == _expected_recovery()


def test_legacy_mutation_proceeds_when_clean(tmp_path, monkeypatch):
    """Clean control: with NO barrier the gate passes, the handler runs,
    and the clean result shape is unchanged (no refusal keys)."""
    _barrier_paths(tmp_path, monkeypatch, raise_barrier=False)
    _patch_client(monkeypatch, None)
    calls: list = []

    async def probe(*_a, **_kw):
        calls.append(1)
        return {"success": True, "echo": 1}

    wrapped = server._wrap_legacy_cache_operation_boundary(probe, "workflowy_edit_node")
    result = asyncio.run(wrapped(node_id="A", name="x"))

    assert calls == [1]
    assert result == {"success": True, "echo": 1}


# ---------------------------------------------------------------------------
# Required test: a non-render legacy cache read under hard dirt receives
# EXACTLY ONE advisory while the boundary is active.  (BASELINE-SENSITIVE:
# fails at pristine a77e8ea — spec was None and the application no-op'd
# outside the boundary.)
# ---------------------------------------------------------------------------


def test_non_render_cache_read_receives_exactly_one_advisory(tmp_path, monkeypatch):
    _patch_client(monkeypatch, HARD)

    async def probe(node_id=None, **_kwargs):
        return {"success": True, "data": 1}

    wrapped = server._wrap_legacy_cache_operation_boundary(probe, "workflowy_scry")
    result = asyncio.run(wrapped(node_id="A"))

    advisory = result["coherence_advisory"]
    assert advisory["status"] == "hard_dirty"
    assert advisory["refresh_required"] is True
    assert advisory["dirty_generation"] == 7
    assert advisory["reason_code"] == "verified_effect_unpublishable"
    assert advisory["transaction_id"] == "tx-advisory"
    assert advisory["message"] == COHERENCE_READ_ADVISORY_LINE
    # Exactly one advisory object (idempotent single application).
    assert result["data"] == 1


def test_non_render_cache_read_clean_unchanged(tmp_path, monkeypatch):
    _patch_client(monkeypatch, None)

    async def probe(node_id=None, **_kwargs):
        return {"success": True, "data": 1}

    wrapped = server._wrap_legacy_cache_operation_boundary(probe, "workflowy_scry")
    result = asyncio.run(wrapped(node_id="A"))

    assert result == {"success": True, "data": 1}


# ---------------------------------------------------------------------------
# BEHAVIOR LOCK: the frozen-DOM legacy GLIMPSE remains UNADVISED after the
# wrapper becomes live (the family skip — internal render seams own
# provenance-sensitive advisory shaping).
# ---------------------------------------------------------------------------


def test_legacy_frozen_dom_modal_read_hard_dirt_no_warning(tmp_path, monkeypatch):
    _patch_client(monkeypatch, HARD)
    _install_modal_reply(monkeypatch, _dom_capture_modal_reply())

    wrapped = _wrapped_legacy_glimpse()
    reply = asyncio.run(wrapped(render_spec="modal"))

    assert isinstance(reply, str), reply
    assert "COHERENCE FAILURE" not in reply
    assert not reply.startswith(COHERENCE_READ_ADVISORY_LINE)
    assert "Alpha point" in reply and "Beta point" in reply


# ---------------------------------------------------------------------------
# BEHAVIOR LOCK: a cache-derived legacy GLIMPSE render remains advised
# EXACTLY ONCE through its internal seam (the wrapper adds nothing).
# ---------------------------------------------------------------------------


def test_legacy_cache_derived_modal_read_hard_dirt_exactly_one_warning(
    tmp_path, monkeypatch
):
    _patch_client(monkeypatch, HARD)
    _install_modal_reply(monkeypatch, _cache_render_spec_modal_reply())

    wrapped = _wrapped_legacy_glimpse()
    reply = asyncio.run(wrapped(render_spec="modal"))

    assert isinstance(reply, str), reply
    assert reply.startswith(COHERENCE_READ_ADVISORY_LINE + "\n\n")
    assert reply.count("COHERENCE FAILURE") == 1
    assert "Alpha point" in reply


def test_legacy_frozen_dom_modal_read_clean_unchanged(tmp_path, monkeypatch):
    _patch_client(monkeypatch, None)
    _install_modal_reply(monkeypatch, _dom_capture_modal_reply())

    wrapped = _wrapped_legacy_glimpse()
    reply = asyncio.run(wrapped(render_spec="modal"))

    assert isinstance(reply, str), reply
    assert "COHERENCE FAILURE" not in reply
    assert not reply.startswith(COHERENCE_READ_ADVISORY_LINE)
    assert "Alpha point" in reply and "Beta point" in reply


def test_legacy_cache_derived_modal_read_clean_unchanged(tmp_path, monkeypatch):
    _patch_client(monkeypatch, None)
    _install_modal_reply(monkeypatch, _cache_render_spec_modal_reply())

    wrapped = _wrapped_legacy_glimpse()
    reply = asyncio.run(wrapped(render_spec="modal"))

    assert isinstance(reply, str), reply
    assert "COHERENCE FAILURE" not in reply
    assert not reply.startswith(COHERENCE_READ_ADVISORY_LINE)
    assert "Alpha point" in reply


# ---------------------------------------------------------------------------
# Required test: the optional-async mutation fronts RETAIN their internal
# runner recheck (the Chain-3 machinery is untouched by the wrapper
# repair).  Queue clean, raise the barrier before the runner executes:
# zero impl calls and the typed job contract.
# ---------------------------------------------------------------------------


def test_optional_async_front_retains_internal_runner_recheck(tmp_path, monkeypatch):
    _barrier_paths(tmp_path, monkeypatch, raise_barrier=False)
    _patch_client(monkeypatch, None)
    server._jobs.clear()
    impl_calls: list = []

    async def _impl_spy(**kwargs):
        impl_calls.append(kwargs)
        return {"success": True, "changed": True}

    monkeypatch.setattr(server, "_purge_apply_impl", _impl_spy)
    registry = server.build_tag_registry()

    async def _scenario():
        release = asyncio.Event()
        real_spawn = _opc.create_isolated_context_task

        def _holding_spawn(coro, *, name=None):
            async def _held():
                await release.wait()
                return await coro

            return real_spawn(_held(), name=name)

        _opc.create_isolated_context_task = _holding_spawn
        try:
            ack = await dispatch_operation(
                registry,
                "purge.apply",
                {
                    "family": "fam",
                    "root_handle": "r",
                    "fingerprint": "fp",
                    "execution": "async",
                },
                error_logger=lambda *_a: None,
                operation_boundary=server._mcp_operation_boundary,
                mutation_admission=server._mcp_mutation_admission,
                exception_mapper=server._mcp_exception_mapper,
                result_finalizer=server._mcp_operation_result_finalizer,
            )
            job_id = ack["job"]["id"]  # make_job_record's normalized shape
            # Barrier raised AFTER the job was queued clean.
            s04_raise_barrier(
                server._admission_coherence_paths(), transaction_id="tx-async-recheck"
            )
            release.set()
            await server._jobs[job_id]["_task"]
            return job_id
        finally:
            _opc.create_isolated_context_task = real_spawn

    job_id = asyncio.run(_scenario())
    assert impl_calls == []  # zero mutations through the optional-async front
    job = server._jobs[job_id]
    assert job["status"] == "failed"
    assert job["error_code"] == "coherence_barrier_active"
    assert job["error_extra"]["transaction_id"] == "tx-async-recheck"
    assert job["error_extra"]["recovery"] == _expected_recovery()
