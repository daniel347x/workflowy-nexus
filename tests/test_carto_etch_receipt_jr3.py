"""JR3 / CF-151 branch 4 witness: ``workflowy_etch``'s OUTER catch keeps the receipt.

CF-151 (Ledger [ap:USEC6S]) branch 4: ``workflowy_etch``'s outer catch returned
``{success, nodes_created, errors}`` and DROPPED ``root_node_ids``, while the
success path returned it. A raise that lands AFTER ``create_tree`` returned (the
"ETCH complete" debug log is the ordinary one) therefore reported a failure
whose receipt could not name the top-level roots the server had already
created -- ``point.capture`` / ``point.extract`` read ``root_node_ids`` to know
what landed, and an absent field reads as "nothing landed".

Design (fixed by the lead): ``root_ids`` is bound to ``[]`` immediately before
the outer ``try``; the successful path is untouched; the catch returns
``root_node_ids: root_ids``. Binding before the ``try`` is load-bearing twice --
it yields ``[]`` when the raise precedes any root, and it keeps a raise in the
catch from turning into an ``UnboundLocalError`` that would mask the real error.

The real async method is driven with a stubbed ``create_node`` that hands back
actual node ids, so the receipt is asserted against ids that were "created",
not against a mock constant.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workflowy_mcp.client.api_client_etch import (  # noqa: E402
    WorkFlowyClientEtch,
)
from workflowy_mcp.models.node import WorkFlowyNode  # noqa: E402

_PARENT = "11111111-2222-3333-4444-555555555555"

# Receipt field set of the outer catch: the three it always carried, PLUS
# root_node_ids. Locked exactly so a field silently appearing or vanishing
# fails here rather than in a caller.
_CATCH_RECEIPT_FIELDS = {"success", "nodes_created", "root_node_ids", "errors"}


def _make_client(*, raise_on: str | None = None):
    """Real WorkFlowyClientEtch via __new__, create_node stubbed to real ids.

    ``raise_on`` makes ``_log_to_file`` raise whenever the log message contains
    that marker, which is how the branch-4 failure is produced: the forest is
    fully created and THEN the completion log explodes.
    """
    client = WorkFlowyClientEtch.__new__(WorkFlowyClientEtch)
    created: list[str] = []
    logs: list[str] = []

    async def _fake_create_node(request, _internal_call=False, **kw):
        node_id = f"stub-node-{len(created) + 1}"
        created.append(node_id)
        return WorkFlowyNode(id=node_id, name=request.name, note=request.note)

    def _log_to_file(message, *args, **kwargs):
        logs.append(message)
        if raise_on is not None and raise_on in message:
            raise RuntimeError(f"debug-log sink exploded: {message}")

    client.create_node = _fake_create_node  # type: ignore[assignment]
    client._log_to_file = _log_to_file  # type: ignore[assignment]
    client._mark_nodes_export_dirty = lambda ids: None  # type: ignore[assignment]
    return client, created, logs


def test_etch_outer_catch_reports_created_roots_after_completion_log_raises():
    """The branch-4 defect: roots land, the completion log raises, and the
    failure receipt used to have no ``root_node_ids`` at all."""
    client, created, logs = _make_client(raise_on="ETCH complete")
    forest = [
        {"name": "Root A", "children": [{"name": "A.1"}]},
        {"name": "Root B"},
    ]

    receipt = asyncio.run(client.workflowy_etch(_PARENT, forest))

    # The raise really happened after create_tree returned: all three nodes
    # exist and the completion log was reached.
    assert created == ["stub-node-1", "stub-node-2", "stub-node-3"]
    assert any("ETCH complete" in message for message in logs)

    assert set(receipt) == _CATCH_RECEIPT_FIELDS
    assert receipt["success"] is False
    assert receipt["nodes_created"] == 3
    # ROOTS only (not the child), in creation order: exactly what a caller may
    # legitimately retry against or record as landed.
    assert receipt["root_node_ids"] == ["stub-node-1", "stub-node-3"]
    assert len(receipt["errors"]) == 1
    assert "ETCH failed:" in receipt["errors"][0]
    assert "debug-log sink exploded" in receipt["errors"][0]


def test_etch_outer_catch_reports_empty_roots_when_raise_precedes_any_create():
    """The initialisation half: a raise before any create must report ``[]``,
    not omit the field and not raise UnboundLocalError out of the catch."""
    client, created, _logs = _make_client(raise_on="ETCH start")

    receipt = asyncio.run(client.workflowy_etch(_PARENT, [{"name": "Root"}]))

    assert created == []  # no create_node call ever happened
    assert set(receipt) == _CATCH_RECEIPT_FIELDS
    assert receipt["success"] is False
    assert receipt["nodes_created"] == 0
    assert receipt["root_node_ids"] == []
    assert "ETCH failed:" in receipt["errors"][0]

