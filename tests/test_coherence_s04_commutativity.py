"""S04 §12.3 — the Axiom-7 commutativity ORDER matrix (sequential ordering
coverage, honestly labeled).

A concurrent exact-node UPDATE (owns name/note/completion) and a sibling
CREATE's child-order publication (owns membership/order/priority ranks),
replayed in BOTH sequence orders through checkpoint + epoch reload, must
converge to identical cache state.

⚠️ HONEST SCOPE LABEL (S04 Remediation Chain 2, work-order item 3): this is
SEQUENTIAL reducer-ORDER coverage, NOT concurrent-process evidence.  Each
subprocess below performs BOTH appends itself, one subprocess per ordering
— "two real processes" in the S04 result meant the two order-run
subprocesses, never two live concurrent actors.  The GENUINE multi-process
acceptance matrix (separate live U/C actor subprocesses publishing only
their own transaction, forced orders, same three oracles) lives in
``tests/test_coherence_s04_multiprocess_acceptance.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from coherence_harness import assert_semantic_cache_equal, run_subprocess

_SCENARIO = r"""
import json
import sys

sys.path.insert(0, sys.argv[2] + "/tests")

from coherence_harness import (
    ACCOUNT_A,
    make_coherence_root,
    s02_node,
    s04_append,
    s04_checkpoint_config,
    s04_install_base_manifest,
    s04_write_base_snapshot,
)
from workflowy_mcp.coherence import (
    SemanticEffect,
    materialize_current_payload,
    read_manifest,
    run_checkpoint_rotation,
)
import asyncio


def main() -> None:
    order = sys.argv[1]
    paths = make_coherence_root(__import__("pathlib").Path(sys.argv[3]), ACCOUNT_A)
    snapshot_dir = __import__("pathlib").Path(sys.argv[3]) / "nodes_export_cache"
    # Base: parent P with children A (p100) and B (p200).
    nodes = [
        s02_node("P", name="Parent", priority=100),
        s02_node("A", name="A-original", parent_id="P", priority=100),
        s02_node("B", name="B", parent_id="P", priority=200),
    ]
    base = s04_write_base_snapshot(
        paths, snapshot_dir, nodes, epoch_number=0, epoch_id="b" * 32
    )
    s04_install_base_manifest(paths, base_snapshot_path=base, epoch_id="b" * 32)

    def update_a():
        # U owns A's name/note content fields (exact-node content effect).
        # ('completed' is canonical but deliberately NOT maskable in
        # node_fields — the S01/S12 field-authority contract.)
        s04_append(
            paths,
            operation_kind="update",
            effects=[
                SemanticEffect.node_fields(
                    "A",
                    ["name", "note"],
                    {"name": "A-updated", "note": "note-from-U"},
                )
            ],
            transaction_id="tx-U",
        )

    def create_c():
        # C owns parent membership/order/priority ranks (structural effect);
        # the FULL canonical record rides ONLY for the node C created — via a
        # node_create in the SAME transaction (the Axiom-7 agreement rule).
        s04_append(
            paths,
            operation_kind="create",
            effects=[
                SemanticEffect.node_create(
                    s02_node("C", name="C-created", parent_id="P", priority=150)
                ),
                SemanticEffect.child_order(
                    "P",
                    ["A", "C", "B"],
                    priority_by_id={"A": 100, "C": 150, "B": 200},
                    canonical_records={
                        "C": s02_node("C", name="C-created", parent_id="P", priority=150)
                    },
                ),
            ],
            transaction_id="tx-C",
        )

    if order == "UC":
        update_a()
        create_c()
    else:
        create_c()
        update_a()

    manifest = read_manifest(paths, required=True)
    replayed = materialize_current_payload(paths, manifest).payload
    result = asyncio.run(
        run_checkpoint_rotation(
            paths, snapshot_dir=snapshot_dir, config=s04_checkpoint_config(record_threshold=1)
        )
    )
    assert result.status == "published", result.warnings
    manifest = read_manifest(paths, required=True)
    reloaded = materialize_current_payload(paths, manifest).payload
    print(json.dumps({"replayed": replayed, "reloaded": reloaded}))


main()
"""


def _run_order(tmp_path: Path, order: str, label: str) -> dict:
    root = tmp_path / label
    root.mkdir()
    completed = run_subprocess(_SCENARIO, order, str(Path.cwd()), str(root), timeout=120.0)
    assert completed.returncode == 0, (
        f"scenario {order} failed:\nSTDOUT:{completed.stdout}\nSTDERR:{completed.stderr}"
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_update_vs_sibling_create_commute_through_checkpoint_in_both_orders(tmp_path):
    """SEQUENTIAL reducer-order coverage: one actor per ordering run (see the
    module docstring for the honest scope label)."""
    uc = _run_order(tmp_path, "UC", "order_uc")
    cu = _run_order(tmp_path, "CU", "order_cu")

    # Both publication orders converge to IDENTICAL cache state — replayed
    # AND checkpoint-reloaded.
    assert_semantic_cache_equal(uc["replayed"], cu["replayed"])
    assert_semantic_cache_equal(uc["reloaded"], cu["reloaded"])
    assert_semantic_cache_equal(uc["replayed"], uc["reloaded"])

    by_id = {n["id"]: n for n in cu["reloaded"]["nodes"]}
    # A's content comes from U (the exact-node UPDATE), never overwritten by
    # the sibling CREATE's child-order canonical records.
    assert by_id["A"]["name"] == "A-updated"
    assert by_id["A"]["note"] == "note-from-U"
    # Membership/order/priority come from C's structural effect: A(p100),
    # C(p150), B(p200) — C was created and inserted between A and B.
    assert set(by_id) == {"P", "A", "B", "C"}
    assert by_id["C"]["name"] == "C-created"
    assert by_id["C"]["parent_id"] == "P"
    children = [n for n in cu["reloaded"]["nodes"] if n.get("parent_id") == "P"]
    ordered = sorted(children, key=lambda n: n.get("priority", 0))
    assert [n["id"] for n in ordered] == ["A", "C", "B"]
