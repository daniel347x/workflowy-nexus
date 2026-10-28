"""Stage2-17 (Waypoint A locked work order) — memory-record cross-field
legality (CF-S04-H4) + PERMANENT_FENCE mutation sensitivity.

Two halves:

1. ``_PublicationRecord.basis_head_seq`` cross-field legality at construction:
   the
   shipped lifecycle contract is that a ``checkpoint`` claim ALWAYS carries
   the journal head captured at acquisition (the checkpoint↔checkpoint
   dedupe-coverage input), and no other claim kind ever carries the field.

2. Mutation sensitivity of the CUT-1 absolute zero-caller rebase fence,
   including calls injected inside operation_cache itself. Definition-only
   controls remain clean; no module has a caller exemption.
   The single-epoch-writer fence already carries its own injection locks
   (tests/test_coherence_s04_fences.py: rogue writer, field-role
   violation, rogue construction, and the old-scan-misses proof) and is
   cited, not duplicated here.
"""

from __future__ import annotations

import pytest
from coherence_harness import ACCOUNT_A, NOW
from test_mutation_leases_s05_fences import _offenders_in

from workflowy_mcp.coherence import (
    COMPAT_PATCH_KIND,
    new_claim_id,
    new_epoch_id,
)
from workflowy_mcp.coherence.errors import CoherenceSchemaError
from workflowy_mcp.coherence.refresh_authority import _PublicationRecord

# ---------------------------------------------------------------------------
# Part 1 — basis_head_seq cross-field legality (CF-S04-H4)
# ---------------------------------------------------------------------------


def _claim_kwargs(refresh_kind: str, basis_head_seq: int | None) -> dict:
    return {
        "account_scope_id": ACCOUNT_A,
        "claim_id": new_claim_id(),
        "refresh_kind": refresh_kind,
        "status": "claimed",
        "cut_token": None,
        "requested_hard_generation": 0,
        "requested_external_generation": 0,
        "target_epoch_number": 2,
        "target_epoch_id": new_epoch_id(),
        "started_at": NOW,
        "updated_at": NOW,
        "result": None,
        "basis_head_seq": basis_head_seq,
    }


@pytest.mark.parametrize("kind,basis,status,error", [
    ("checkpoint", None, "claimed", "must carry basis_head_seq"),
    ("checkpoint", -1, "claimed", "non-negative integer"),
    ("checkpoint", True, "claimed", "non-negative integer"),
    ("checkpoint", 0, "claimed", None),
    ("checkpoint", 11, "materializing", None),
    ("system", 7, "claimed", "forbidden.*checkpoint-only"),
    (COMPAT_PATCH_KIND, 7, "claimed", "forbidden.*checkpoint-only"),
    ("unknown", None, "claimed", "refresh_kind must be one of"),
    ("system", None, "unknown", "status must be one of"),
    ("system", None, "claimed", None),
    (COMPAT_PATCH_KIND, None, "publishing", None),
])
def test_in_memory_publication_record_checkpoint_basis_rule(kind, basis, status, error):
    """Retained basis legality, with no persistent model or parser."""
    fields = _claim_kwargs(kind, basis)
    fields["status"] = status
    if error:
        with pytest.raises(CoherenceSchemaError, match=error):
            _PublicationRecord(**fields)
    else:
        record = _PublicationRecord(**fields)
        assert record.basis_head_seq == basis
        assert record.refresh_kind == kind and record.status == status


# ---------------------------------------------------------------------------
# Part 2 — fence mutation sensitivity (PERMANENT_FENCE discipline)
# ---------------------------------------------------------------------------

# The prohibited production shape the H5b rebase-confinement fence claims
# to prevent: a mid-operation journal re-adoption outside the orchestrator.
_PROHIBITED_REBASE_SHAPE = '''\
"""A hypothetical production consumer (prohibited shape)."""

from workflowy_mcp.coherence.operation_cache import (
    establish_admission_snapshot,
    rebase_during_admission,
)


async def mid_operation_readmit(client, snapshot):
    await rebase_during_admission(client, snapshot)
    return await establish_admission_snapshot(client, snapshot)
'''

def test_rebase_seam_fence_catches_injected_production_bypass():
    """Non-orchestrator calls reject even inside operation_cache; definitions are clean."""
    for rel in ("workflowy_mcp/server.py", "workflowy_mcp/new.py", "workflowy_mcp/coherence/operation_cache.py"):
        offenders = _offenders_in([(rel, _PROHIBITED_REBASE_SHAPE)])
        assert any("rebase_during_admission" in hit for hit in offenders)
        assert any("establish_admission_snapshot" in hit for hit in offenders)
    definitions = "async def rebase_during_admission(client): pass\nasync def establish_admission_snapshot(client): pass"
    assert _offenders_in([("workflowy_mcp/coherence/operation_cache.py", definitions)]) == []


def test_fences_hold_on_current_production_tree():
    """The companion direction of the introduce-and-restore discipline:
    with no prohibited shape present, the fence reports zero offenders on
    the real production tree (re-verified here alongside the S05 fences)."""
    from test_mutation_leases_s05_fences import _offenders_for

    assert _offenders_for() == []
