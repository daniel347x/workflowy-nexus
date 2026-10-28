"""23db916f — the handle-correction echo (result finalizer + dispatch reset).

Contract under test:

* Layer 1 (``resolve_uuid_like_node_handle``) records ONE correction when the
  shared prefix kernel adopted a SHAVED handle; exact hits, AP-IDs and clean
  prefixes stay silent (covered in ``test_uuid_handle_resolver.py``).
* ``_dispatch_operation_core`` resets the echo state before each handler
  invocation, so a result can only echo THIS operation's corrections.
* ``_mcp_operation_result_finalizer`` drains the state and applies it to
  EVERY family: one ``resolved: ‹short› ‹name› (corrected from ‹supplied›)``
  line per correction prefixed to string results, a top-level
  ``resolved_corrections`` list on dict results — BEFORE the coherence read
  advisory, so a prefixed result reads advisory → correction line → body
  (contract C1), and above any render-tail header (the finalizer runs after
  the render tail by construction).
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from coherence_harness import (  # noqa: E402
    make_boundary_capable,
    s04_boundary_status,
)

from workflowy_mcp import server  # noqa: E402
from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    resolve_uuid_like_node_handle,
)
from workflowy_mcp.coherence import (  # noqa: E402
    COHERENCE_READ_ADVISORY_LINE,
    cache_operation_boundary,
)
from workflowy_mcp.handle_corrections import (  # noqa: E402
    drain_handle_corrections,
    format_handle_correction_line,
    record_handle_correction,
    reset_handle_corrections,
)
from workflowy_mcp.mcp_api_registry import dispatch_operation  # noqa: E402

FULL_A = "a2e552e3-eb42-4438-ba75-4ab7f849eb4f"
FULL_C = "bc137d90-1111-2222-3333-444444444444"
HALLUCINATED = "a2e552e3-eb42-dead-beef-000000000000"

HARD = s04_boundary_status(
    hard_dirty=True,
    hard_generation=7,
    hard_reason_code="verified_effect_unpublishable",
    hard_transaction_id="tx-advisory",
)


def _read_spec():
    return SimpleNamespace(
        family="node", cache_backed=True, side_effects=("pure_read",)
    )


def _render_spec():
    return SimpleNamespace(
        family="glimpse", cache_backed=True, side_effects=("render",)
    )


def _mutation_spec():
    return SimpleNamespace(
        family="node", cache_backed=True, side_effects=("workflowy_mutation",)
    )


def setup_function(_function):
    reset_handle_corrections()


def teardown_function(_function):
    reset_handle_corrections()


# ---------------------------------------------------------------------------
# The echo line + module semantics
# ---------------------------------------------------------------------------


def test_line_format():
    line = format_handle_correction_line(
        {"supplied": "abc", "resolved_uuid": FULL_A, "name": "Old title", "short": "a2e552e3"}
    )
    assert line == "resolved: a2e552e3 Old title (corrected from abc)"


def test_record_drain_order_and_clear():
    record_handle_correction(supplied="s1", resolved_uuid=FULL_A, name="one", short="a2e552e3")
    record_handle_correction(supplied="s2", resolved_uuid=FULL_C, name="two", short="bc137d90")
    drained = drain_handle_corrections()
    assert [entry["supplied"] for entry in drained] == ["s1", "s2"]
    assert drain_handle_corrections() == []
    assert drain_handle_corrections() == []  # idempotent when empty


def test_reset_clears_stale_records():
    record_handle_correction(supplied="s", resolved_uuid=FULL_A, name="n", short="a2e552e3")
    reset_handle_corrections()
    assert drain_handle_corrections() == []


# ---------------------------------------------------------------------------
# The finalizer: string, dict, pass-through, and every-family reach
# ---------------------------------------------------------------------------


def test_no_corrections_is_byte_identical():
    body = "# Heading\n\nBody text.\n"
    assert server._mcp_operation_result_finalizer(_read_spec(), body) == body
    result = {"success": True, "preview_markdown": body}
    assert server._mcp_operation_result_finalizer(_read_spec(), result) == result


def test_string_result_gains_one_line_per_correction():
    record_handle_correction(supplied="s1", resolved_uuid=FULL_A, name="one", short="a2e552e3")
    record_handle_correction(supplied="s2", resolved_uuid=FULL_C, name="two", short="bc137d90")
    out = server._mcp_operation_result_finalizer(_read_spec(), "BODY")
    lines = out.split("\n")
    assert lines[0] == "resolved: a2e552e3 one (corrected from s1)"
    assert lines[1] == "resolved: bc137d90 two (corrected from s2)"
    assert out.endswith("BODY")
    # The corrections were drained: a second finalizer pass adds nothing.
    assert server._mcp_operation_result_finalizer(_read_spec(), "BODY") == "BODY"


def test_dict_result_gains_resolved_corrections():
    record_handle_correction(supplied="s", resolved_uuid=FULL_A, name="n", short="a2e552e3")
    result = {"success": True, "resolved_uuid": FULL_A}
    out = server._mcp_operation_result_finalizer(_read_spec(), result)
    assert out["success"] is True
    assert out["resolved_corrections"] == [
        {
            "supplied": "s",
            "resolved_uuid": FULL_A,
            "name": "n",
            "short": "a2e552e3",
        }
    ]
    # The input dict is not mutated.
    assert "resolved_corrections" not in result


def test_dict_result_extends_existing_resolved_corrections():
    existing = [{"supplied": "old", "resolved_uuid": "u", "name": "o", "short": "oooo"}]
    record_handle_correction(supplied="s", resolved_uuid=FULL_A, name="n", short="a2e552e3")
    out = server._mcp_operation_result_finalizer(
        _read_spec(), {"resolved_corrections": existing}
    )
    assert out["resolved_corrections"][0] == existing[0]
    assert out["resolved_corrections"][1]["supplied"] == "s"


def test_render_family_results_still_get_the_echo():
    # C1: the render families' finalizer guard skips only the coherence
    # advisory (S04 doctrine); the correction echo applies to them too.
    record_handle_correction(supplied="s", resolved_uuid=FULL_A, name="n", short="a2e552e3")
    out = server._mcp_operation_result_finalizer(_render_spec(), "RENDER BODY")
    assert out.startswith("resolved: a2e552e3 n (corrected from s)\n")
    assert out.endswith("RENDER BODY")


def test_mutation_dict_keeps_corrections_beside_the_ack():
    record_handle_correction(supplied="s", resolved_uuid=FULL_A, name="n", short="a2e552e3")
    result = {"success": True, "operation": "tag.add", "changed": True}
    out = server._mcp_operation_result_finalizer(_mutation_spec(), result)
    assert out["resolved_corrections"][0]["supplied"] == "s"
    assert out["operation"] == "tag.add"


def test_non_string_non_dict_result_passes_through():
    record_handle_correction(supplied="s", resolved_uuid=FULL_A, name="n", short="a2e552e3")
    sentinel = ["raw", "rows"]
    assert server._mcp_operation_result_finalizer(_read_spec(), sentinel) is sentinel


# ---------------------------------------------------------------------------
# Contract C1 ordering: coherence advisory -> correction line -> body
# ---------------------------------------------------------------------------


def test_advisory_reads_above_the_correction_line():
    async def _inside():
        client = make_boundary_capable(
            object.__new__(server.WorkFlowyClient), nodes=(), boundary_status=HARD
        )
        async with cache_operation_boundary(client, "test-op"):
            record_handle_correction(
                supplied="s", resolved_uuid=FULL_A, name="n", short="a2e552e3"
            )
            return server._mcp_operation_result_finalizer(_read_spec(), "BODY")

    out = asyncio.run(_inside())
    assert out.startswith(COHERENCE_READ_ADVISORY_LINE + "\n\n")
    assert out.index(COHERENCE_READ_ADVISORY_LINE) < out.index("resolved: ")
    assert out.index("resolved: ") < out.index("BODY")


def test_clean_boundary_leaves_echo_line_at_the_top():
    async def _inside():
        client = make_boundary_capable(
            object.__new__(server.WorkFlowyClient), nodes=(), boundary_status=None
        )
        async with cache_operation_boundary(client, "test-op"):
            record_handle_correction(
                supplied="s", resolved_uuid=FULL_A, name="n", short="a2e552e3"
            )
            return server._mcp_operation_result_finalizer(_read_spec(), "BODY")

    out = asyncio.run(_inside())
    assert out.startswith("resolved: a2e552e3 n (corrected from s)\n")
    assert out.endswith("BODY")


# ---------------------------------------------------------------------------
# End-to-end through the real dispatch funnel (node.resolve)
# ---------------------------------------------------------------------------


def _dispatch(registry, verb, params):
    return dispatch_operation(
        registry,
        verb,
        params,
        error_logger=lambda *_a: None,
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
    )


def test_dispatch_echoes_corrected_resolve(monkeypatch):
    nodes = [
        {"id": FULL_A, "name": "node a2e552e3"},
        {"id": FULL_C, "name": "node bc137d90"},
    ]
    monkeypatch.setattr(
        server, "get_client",
        lambda: make_boundary_capable(object.__new__(server.WorkFlowyClient), nodes=nodes),
    )
    result = asyncio.run(
        _dispatch(server.NODE_REGISTRY, "resolve", {"node_handle": HALLUCINATED})
    )
    assert result["success"] is True
    assert result["resolved_uuid"] == FULL_A
    corrections = result.get("resolved_corrections")
    assert corrections and corrections[0]["supplied"] == HALLUCINATED
    assert corrections[0]["resolved_uuid"] == FULL_A
    assert corrections[0]["short"] == "a2e552e3"


def test_dispatch_reset_scopes_the_echo_to_one_operation(monkeypatch):
    # §5.5a check: with the reset removed from _dispatch_operation_core, the
    # second (exact) result below would inherit the first operation's stale
    # record and this test FAILS. Verified by stashing the reset call.
    nodes = [
        {"id": FULL_A, "name": "node a2e552e3"},
        {"id": FULL_C, "name": "node bc137d90"},
    ]
    monkeypatch.setattr(
        server, "get_client",
        lambda: make_boundary_capable(object.__new__(server.WorkFlowyClient), nodes=nodes),
    )

    async def _two_operations():
        corrected = await _dispatch(
            server.NODE_REGISTRY, "resolve", {"node_handle": HALLUCINATED}
        )
        exact = await _dispatch(
            server.NODE_REGISTRY, "resolve", {"node_handle": FULL_A}
        )
        return corrected, exact

    corrected, exact = asyncio.run(_two_operations())
    assert corrected["resolved_uuid"] == FULL_A
    assert corrected.get("resolved_corrections")
    # The exact-match operation must NOT echo the previous operation's
    # correction: one operation per echo.
    assert "resolved_corrections" not in exact


def test_dispatch_clean_prefix_resolve_is_silent(monkeypatch):
    nodes = [
        {"id": FULL_A, "name": "node a2e552e3"},
        {"id": FULL_C, "name": "node bc137d90"},
    ]
    monkeypatch.setattr(
        server, "get_client",
        lambda: make_boundary_capable(object.__new__(server.WorkFlowyClient), nodes=nodes),
    )
    # A clean 8-hex segment resolves without any correction echo.
    result = asyncio.run(
        _dispatch(server.NODE_REGISTRY, "resolve", {"node_handle": "a2e552e3"})
    )
    assert result["success"] is True
    assert result["resolved_uuid"] == FULL_A
    assert "resolved_corrections" not in result


def test_layer1_records_feed_the_finalizer_after_real_resolution(monkeypatch):
    # Direct proof that the recording site is the REAL Layer-1 wrapper (not a
    # test shim): resolve through it, then finalize a string as a family
    # front would.
    nodes = {FULL_A: {"id": FULL_A, "name": "node a2e552e3"}}
    res = resolve_uuid_like_node_handle(HALLUCINATED, nodes)
    assert res["success"] is True
    out = server._mcp_operation_result_finalizer(_read_spec(), "BODY")
    assert out.startswith("resolved: a2e552e3 node a2e552e3 ")
    assert "(corrected from a2e552e3-eb42-dead-beef-000000000000)" in out
