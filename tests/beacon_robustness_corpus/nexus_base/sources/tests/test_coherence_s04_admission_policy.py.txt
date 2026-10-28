"""S04 §14.4 — pure external-dirt admission matrix tests (A6: table only).

The pure decision table is exhaustive and closed; S04 ships NO production
consumer wiring (the deferred map lives in S07/S10/S12–S14).  External dirt
never becomes a hard barrier, never blocks a read/preview, and never
cancels an intrinsic live preflight.
"""

from __future__ import annotations

import pytest
from coherence_harness import ACCOUNT_A, make_coherence_root, s02_set_external_dirt

from workflowy_mcp.coherence import (
    ADMISSION_CLASS_PROCEED,
    ADMISSION_CLASS_PROCEED_EXCEPTION,
    ADMISSION_CLASS_REFUSE_HARD_DIRT,
    ADMISSION_OP_KINDS,
    DISK_OPERATION_HARD_DIRT_POLICY,
    AdmissionDecision,
    ExternalDirtState,
    admission_policy,
    external_dirt_admission_policy,
    mutation_admission_class,
    read_manifest,
)

P = AdmissionDecision.PROCEED_WITH_ADOPTED_BASELINE
U = AdmissionDecision.UPGRADE_TO_LIVE_PREFLIGHT
R = AdmissionDecision.REFUSE_REFRESH_REQUIRED


def _dirty() -> ExternalDirtState:
    return ExternalDirtState(
        schema_version=1,
        account_scope_id=ACCOUNT_A,
        active=True,
        generation=3,
        observed_at="2026-11-01T00:00:00+00:00",
        mutation_kind="input",
        node_uuid=None,
        parent_uuid=None,
        detail=None,
    )


def test_table_is_closed_and_exhaustive():
    from workflowy_mcp.coherence import admission as admission_module

    assert set(admission_module._EXTERNAL_DIRT_ACTIVE_TABLE) == set(ADMISSION_OP_KINDS)


def test_clean_dirt_proceeds_for_every_kind():
    for kind in sorted(ADMISSION_OP_KINDS):
        assert admission_policy(kind, None) is P
    clean = ExternalDirtState.clean(ACCOUNT_A)
    for kind in sorted(ADMISSION_OP_KINDS):
        assert admission_policy(kind, clean) is P


def test_dirty_matrix_rows():
    dirt = _dirty()
    assert admission_policy("create", dirt) is U
    assert admission_policy("update_complete", dirt) is P
    assert admission_policy("move", dirt) is P
    assert admission_policy("delete", dirt) is P
    assert admission_policy("explicit_ap_id", dirt) is R
    assert admission_policy("auto_ap_id", dirt) is P
    assert admission_policy("cache_compiled_plan", dirt) is R


def test_unknown_op_kind_fails_closed():
    with pytest.raises(ValueError):
        admission_policy("explode", _dirty())
    # Renders/previews are never classifiable-refusable: they are not even
    # in the table (reads stay stale-but-safe under external dirt).
    for renderish in ("render", "preview", "read", "gather"):
        with pytest.raises(ValueError):
            admission_policy(renderish, _dirty())


def test_delegate_is_one_table_one_body():
    dirt = _dirty()
    for kind in sorted(ADMISSION_OP_KINDS):
        assert external_dirt_admission_policy(kind, dirt) is admission_policy(kind, dirt)


def test_external_dirt_never_touches_the_hard_barrier(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s02_set_external_dirt(paths, 9)
    dirt = read_manifest(paths, required=True).dirty_status
    for kind in sorted(ADMISSION_OP_KINDS):
        admission_policy(kind, ExternalDirtState.clean(ACCOUNT_A))
        admission_policy(kind, _dirty())
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is False
    assert manifest.dirty_status.generation == 0
    assert dirt.active is False


# ---------------------------------------------------------------------------
# Hard-dirt operation classification (§7.2/§7.3)
# ---------------------------------------------------------------------------


def test_mutation_admission_class_workflowy_mutation_refuses():
    assert (
        mutation_admission_class("node.edit", ("workflowy_mutation",), True)
        == ADMISSION_CLASS_REFUSE_HARD_DIRT
    )
    # config+workflowy (merge.persist) refuses via the workflowy leg.
    assert (
        mutation_admission_class(
            "slice_group.merge.persist",
            ("workflowy_mutation", "config_mutation", "local_state_mutation"),
            True,
        )
        == ADMISSION_CLASS_REFUSE_HARD_DIRT
    )


def test_mutation_admission_class_disk_provenance_table():
    assert (
        mutation_admission_class("beacon.tags.ensure", ("disk_mutation",), True)
        == ADMISSION_CLASS_REFUSE_HARD_DIRT
    )
    assert (
        mutation_admission_class("beacon.tags.remove", ("disk_mutation",), True)
        == ADMISSION_CLASS_REFUSE_HARD_DIRT
    )
    assert (
        mutation_admission_class("beacon.sync_node", ("disk_mutation",), True)
        == ADMISSION_CLASS_REFUSE_HARD_DIRT
    )
    # Pure reads proceed (read-only; advisory disclosed).
    assert (
        mutation_admission_class("beacon.snippet", ("pure_read",), True)
        == ADMISSION_CLASS_PROCEED
    )
    assert (
        mutation_admission_class("beacon.resolve", ("pure_read",), True)
        == ADMISSION_CLASS_PROCEED
    )


def test_mutation_admission_class_unregistered_disk_mutation_fails_closed():
    assert (
        mutation_admission_class("future.disk_thing", ("disk_mutation",), True)
        == ADMISSION_CLASS_REFUSE_HARD_DIRT
    )


def test_mutation_admission_class_config_and_read_safe():
    # config_mutation alone proceeds when not cache-dependent (slice_group CRUD).
    assert (
        mutation_admission_class("slice_group.create", ("config_mutation",), False)
        == ADMISSION_CLASS_PROCEED
    )
    # read-safe operations proceed.
    assert (
        mutation_admission_class("node.read", ("pure_read",), True)
        == ADMISSION_CLASS_PROCEED
    )
    # A generic local_state_mutation operation proceeds.
    assert (
        mutation_admission_class("future.local_thing", ("local_state_mutation",), False)
        == ADMISSION_CLASS_PROCEED
    )


def test_mutation_admission_class_named_recovery_control_exceptions():
    """S04 Remediation Chain 4 (work-order item 2): the named §7.2
    recovery/control fronts classify as ``proceed_exception`` (an explicit
    boundary-map class) — runtime refusal behavior unchanged (both
    proceed and proceed_exception are ungated), and the exception never
    outranks a refuse branch."""
    assert (
        mutation_admission_class("system.cache.refresh", ("local_state_mutation",), False)
        == ADMISSION_CLASS_PROCEED_EXCEPTION
    )
    assert (
        mutation_admission_class("system.job.status", ("pure_read",), False)
        == ADMISSION_CLASS_PROCEED_EXCEPTION
    )
    assert (
        mutation_admission_class("system.job.cancel", ("local_state_mutation",), False)
        == ADMISSION_CLASS_PROCEED_EXCEPTION
    )
    # Fail-closed always wins: an exception-named operation carrying a
    # Workflowy mutation still refuses.
    assert (
        mutation_admission_class(
            "system.cache.refresh", ("workflowy_mutation", "local_state_mutation"), False
        )
        == ADMISSION_CLASS_REFUSE_HARD_DIRT
    )


def test_disk_policy_table_matches_frozen_provenance():
    assert DISK_OPERATION_HARD_DIRT_POLICY["beacon.snippet"]["class"] == ADMISSION_CLASS_PROCEED
    assert DISK_OPERATION_HARD_DIRT_POLICY["beacon.resolve"]["class"] == ADMISSION_CLASS_PROCEED
    for op in ("beacon.tags.ensure", "beacon.tags.remove", "beacon.sync_node", "beacon.map.refresh"):
        assert DISK_OPERATION_HARD_DIRT_POLICY[op]["class"] == ADMISSION_CLASS_REFUSE_HARD_DIRT
