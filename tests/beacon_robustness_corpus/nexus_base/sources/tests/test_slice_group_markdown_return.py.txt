"""U01 §9.2 — slice_group render/sift/merge.render return raw Markdown by default.

The agent-facing return is shaped by the ONE finalizer
``_shape_glimpse_family_return`` (the same seam glimpse/lightning_rod/anchor.dock
already use); the Markdown content is the ``preview_markdown`` the pipeline
already composed, byte-identical. ``as_markdown=false`` keeps the structured
result minus the redundant ``rows`` twin. Errors, push acks and file summaries
pass through unchanged.
"""

from __future__ import annotations

import asyncio

from workflowy_mcp import server
from workflowy_mcp.mcp_api_registry import dispatch_operation


_MARKDOWN_RESULT = {
    "success": True,
    "preview_markdown": "# Lens\n",
    "rows": [{"uuid": "u"}],
    "render_scope": {"kind": "lightning_rod"},
}

_FULL_RESULT = {
    "success": True,
    "preview_tree": [{"id": "R"}],
    "rows": [{"uuid": "u"}],
    "node_count": 3,
    "depth": 2,
    "render_scope": {"kind": "lightning_rod"},
    "empty_selection": False,
    "_source": "lightning_rod_lens",
}

_LENS = {"kind": "tags", "tags": ["x"]}
_UNION_LENS = {
    "kind": "merge",
    "operation": "union",
    "inputs": [
        {"kind": "group", "group_handle": "A"},
        {"kind": "group", "group_handle": "B"},
    ],
}


def _registry():
    return server.build_slice_group_registry()


def _patch_render_lens(monkeypatch, result):
    captured = {}

    async def fake(render_spec, **kwargs):
        captured["render_spec"] = render_spec
        captured["kwargs"] = kwargs
        return result

    monkeypatch.setattr(server, "_lightning_rod_render_lens", fake)
    return captured


def test_markdown_default_for_render_sift_and_merge_render(monkeypatch):
    """§9.2 item 25 — every Markdown-default lens render returns the raw string."""
    registry = _registry()
    _patch_render_lens(monkeypatch, dict(_MARKDOWN_RESULT))

    rendered = asyncio.run(dispatch_operation(registry, "render", {"lens": _LENS}))
    sifted = asyncio.run(
        dispatch_operation(registry, "sift", {"lens": _LENS, "query": "q"})
    )
    merged = asyncio.run(
        dispatch_operation(registry, "merge.render", {"lens": _UNION_LENS})
    )

    assert rendered == "# Lens\n" and isinstance(rendered, str)
    assert sifted == "# Lens\n" and isinstance(sifted, str)
    assert merged == "# Lens\n" and isinstance(merged, str)


def test_as_markdown_false_keeps_structured_result_without_rows(monkeypatch):
    """§9.2 item 26 — full mode keeps the dict, drops only the rows twin."""
    registry = _registry()
    _patch_render_lens(monkeypatch, dict(_FULL_RESULT))

    result = asyncio.run(
        dispatch_operation(
            registry, "render", {"lens": _LENS, "as_markdown": False}
        )
    )

    assert isinstance(result, dict)
    assert "rows" not in result
    expected = {key: value for key, value in _FULL_RESULT.items() if key != "rows"}
    assert result == expected


def test_zero_matches_return_the_markdown_string(monkeypatch):
    """§9.2 item 27 — an empty selection's Markdown is delivered raw."""
    registry = _registry()
    empty = dict(_MARKDOWN_RESULT)
    empty.update(
        {"preview_markdown": "No matches.\n", "rows": [], "empty_selection": True}
    )
    _patch_render_lens(monkeypatch, empty)

    result = asyncio.run(dispatch_operation(registry, "render", {"lens": _LENS}))

    assert result == "No matches.\n"


def test_output_file_push_and_error_returns_pass_through(monkeypatch):
    """§9.2 item 28 — file summaries, push acks and errors are unchanged."""
    registry = _registry()
    summary = {
        "success": True,
        "output_file": "out.md",
        "bytes_written": 5,
        "node_count": 1,
        "depth": 1,
    }
    _patch_render_lens(monkeypatch, summary)
    written = asyncio.run(
        dispatch_operation(
            registry, "render", {"lens": _LENS, "output_file": "out.md"}
        )
    )
    assert written == summary

    ack = {"success": True, "pushed_to_modal": True}

    async def pushed(**kwargs):
        return ack

    monkeypatch.setattr(server, "_glimpse_push_slice_to_modal", pushed)
    push = asyncio.run(
        dispatch_operation(
            registry, "render", {"lens": _LENS, "push_to_modal": True}
        )
    )
    assert push == ack

    failure = {
        "success": False,
        "error": {"code": "invalid_lens_shape", "message": "bad lens"},
    }
    _patch_render_lens(monkeypatch, failure)
    result = asyncio.run(dispatch_operation(registry, "render", {"lens": _LENS}))
    assert isinstance(result, dict) and result["success"] is False
    assert result["error"] == failure["error"]


def test_sift_threads_headings_only_true_to_the_finalizer(monkeypatch):
    """§9.2 item 29 — sift tier='headings' defaults headings_only=True into
    the finalizer. (Since [ap:47B7WO] the DEFAULT tier is 'context', which
    bypasses the finalizer's tree result entirely; the tree render is the
    explicit headings tier.)"""
    registry = _registry()
    _patch_render_lens(monkeypatch, dict(_FULL_RESULT))
    seen = []
    real = server._shape_glimpse_family_return

    def spy(result, *, output_file, headings_only=False):
        seen.append(headings_only)
        return real(result, output_file=output_file, headings_only=headings_only)

    monkeypatch.setattr(server, "_shape_glimpse_family_return", spy)

    asyncio.run(
        dispatch_operation(
            registry,
            "sift",
            {"lens": _LENS, "query": "q", "tier": "headings"},
        )
    )
    assert seen == [True]

    asyncio.run(
        dispatch_operation(
            registry, "render", {"lens": _LENS, "headings_only": True}
        )
    )
    assert seen[-1] is True
