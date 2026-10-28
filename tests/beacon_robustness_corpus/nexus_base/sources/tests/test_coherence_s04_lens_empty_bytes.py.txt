"""S04 Remediation Chain 4 (work-order item 3) — empty-lens output_file
reports the ACTUAL bytes written.

Pre-fix, ``_render_lens_empty_result`` wrote the advisory-framed "No
matches." string to the output file but reported ``bytes_written`` from the
UNFRAMED placeholder, so a hard-dirty empty-lens file (warning line + blank
separator + placeholder) was misreported as 12 bytes.  The final framed
string is now built ONCE, written exactly, and its UTF-8 length reported.

Required tests:
(a) hard-dirty empty-lens output_file:
    ``bytes_written == len(Path(output_file).read_bytes())``;
(b) clean empty-lens output_file: the same equality AND unchanged bytes
    (``b"(empty)"`` exactly -- the D7 shared constant, [ap:2EG7TD]);
(c) the structured advisory remains exactly once.

Mutation note (step 4a): test (a) was verified to FAIL against the pre-fix
code (production stashed to the 6f5a8e2 baseline): pre-fix bytes_written
was 12 while the file held the framed ~400-byte text.
"""

from __future__ import annotations

import asyncio

from coherence_harness import make_boundary_capable, s04_boundary_status

from workflowy_mcp import server
from workflowy_mcp.cache_render_spec import RenderProjectionSpec
from workflowy_mcp.coherence import (
    COHERENCE_READ_ADVISORY_LINE,
    cache_operation_boundary,
)

HARD = s04_boundary_status(
    hard_dirty=True,
    hard_generation=7,
    hard_reason_code="verified_effect_unpublishable",
    hard_transaction_id="tx-advisory",
)

_SOURCE_META = {"_source": "test", "render_scope": {"kind": "lightning_rod"}}


def _double(status):
    client = object.__new__(server.WorkFlowyClient)
    return make_boundary_capable(client, nodes=(), boundary_status=status)


def _run_empty_lens(out_file, status):
    async def _run():
        client = _double(status)
        async with cache_operation_boundary(client, "test-lens-empty"):
            return server._render_lens_empty_result(
                spec=RenderProjectionSpec(root_uuid="root", as_markdown=True),
                output_file=str(out_file),
                source_meta=dict(_SOURCE_META),
            )

    return asyncio.run(_run())


def test_hard_dirty_empty_lens_bytes_written_matches_actual_file_bytes(tmp_path):
    """Required (a) + (c): the advised empty-lens file's actual byte count is
    reported; the structured advisory remains exactly once; the advisory
    framing itself is unchanged (warning line + blank separator + payload)."""
    out = tmp_path / "empty.md"
    payload = _run_empty_lens(out, HARD)
    data = out.read_bytes()
    # (a) bytes_written is the ACTUAL written byte count.
    assert payload["bytes_written"] == len(data)
    # The advisory framing semantics are preserved exactly.
    assert data.startswith(COHERENCE_READ_ADVISORY_LINE.encode("utf-8") + b"\n\n")
    assert data.endswith(b"(empty)")
    assert data.count(b"COHERENCE FAILURE") == 1
    # (c) the structured advisory appears exactly once.
    assert list(payload.keys()).count("coherence_advisory") == 1
    assert payload["coherence_advisory"]["status"] == "hard_dirty"
    assert payload["coherence_advisory"]["dirty_generation"] == 7


def test_clean_empty_lens_bytes_written_matches_and_bytes_unchanged(tmp_path):
    """Required (b): the clean empty-lens file has the same equality and its
    bytes are unchanged (exactly the unframed placeholder)."""
    out = tmp_path / "empty.md"
    payload = _run_empty_lens(out, None)
    data = out.read_bytes()
    assert payload["bytes_written"] == len(data)
    assert data == b"(empty)"
    assert "coherence_advisory" not in payload
