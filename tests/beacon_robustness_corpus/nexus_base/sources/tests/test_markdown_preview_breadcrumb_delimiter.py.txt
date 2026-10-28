"""Regression lock: the breadcrumb delimiter is RESET-AWARE through sentinels/files.

THE BUG (the "mirrored-branch sentinel glitch", reported by Dan live): in the
unified composer (_build_markdown_preview_from_tree), the handoff-sec.3
BREADCRUMB DELIMITER (_emit_breadcrumb_delimiter) -- the mirrored ancestor
branch re-rendered when the walk exits a file/MARKDOWN-sentinel block and
resumes in the outer tree -- emitted every ancestor at its RAW ROOT-RELATIVE
level (graph_depth + 1). When the mirrored chain passed THROUGH a MARKDOWN
sentinel (or a standalone-mode file node), the sentinel/file ancestor rendered
at its raw mirrored depth (H4) and its child at H5 -- while the actual content
below resumed at the correct reset levels (H2), because the main walk's
structural-anchor stack already knew the sentinel semantics. Result: an absurd
H5 -> H2 cliff in the rendered pseudo-document. The sentinel was supposed to
mirror the way its block originally rendered: sentinel H1 ("level 0"), its
child H1, grandchild H2.

THE FIX: the ancestor chain tuples now carry a third element,
``is_reset_boundary`` = the ``pushed_structural_anchor`` flag computed during
the node's own visit (True EXACTLY for a MARKDOWN sentinel, always, and for a
file node in standalone mode; False for md-headings / markdown-content / plain
structural / connected-mode files). The delimiter walks the chain with a
running anchor: a flagged ancestor renders at H1 and re-anchors the remainder
of the chain to its child depth -- byte-for-byte the main walk's anchor-push
semantics. Chains with NO boundary ancestor render exactly as before
(backward compatible). Being computed at visit time from classification_name /
raw_note, the flag is immune to the headings-only UUID prefix in the emitted
name (the trap behind the earlier headings-only sentinel fix -- see
test_markdown_preview_headings_only_sentinel.py).

All local: the composer is a classmethod over plain node dicts; no network,
no Workflowy, no cache.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402


def _mirrored_sentinel_tree():
    """Dan's live repro, minimized: an outer MARKDOWN sentinel whose subtree
    contains a deep chain with an inner sentinel; after the inner sentinel's
    block closes, the walk resumes at a child of the outer sentinel's subtree
    root -- the breadcrumb delimiter then re-renders the ancestor chain, which
    passes THROUGH the outer sentinel."""
    return [
        {
            "id": "home",
            "name": "Home",
            "note": "",
            "children": [
                {
                    "id": "root",
                    "name": "Workflowy Root",
                    "note": "",
                    "children": [
                        {
                            "id": "scratch",
                            "name": "SCRATCH",
                            "note": "",
                            "children": [
                                {
                                    "id": "sent-outer",
                                    "name": "MARKDOWN",
                                    "note": "",
                                    "children": [
                                        {
                                            "id": "nested-md",
                                            "name": "Nested Markdown",
                                            "note": "",
                                            "children": [
                                                {
                                                    "id": "st1",
                                                    "name": "Subtest 1",
                                                    "note": "",
                                                    "children": [
                                                        {
                                                            "id": "st2",
                                                            "name": "Subtest 2",
                                                            "note": "",
                                                            "children": [
                                                                {
                                                                    "id": "sent-inner",
                                                                    "name": "MARKDOWN",
                                                                    "note": "",
                                                                    "children": [
                                                                        {
                                                                            "id": "st3",
                                                                            "name": "Subtest 3",
                                                                            "note": "",
                                                                            "children": [],
                                                                        },
                                                                    ],
                                                                },
                                                            ],
                                                        },
                                                    ],
                                                },
                                                {
                                                    "id": "st1a",
                                                    "name": "Subtest 1a",
                                                    "note": "",
                                                    "children": [],
                                                },
                                            ],
                                        },
                                    ],
                                },
                            ],
                        },
                    ],
                },
            ],
        },
    ]


def _file_boundary_tree():
    """A file node whose inner-sentinel block closes; the following structural
    child of the FILE gets a breadcrumb that passes THROUGH the file node."""
    return [
        {
            "id": "home",
            "name": "Home",
            "note": "",
            "children": [
                {
                    "id": "file-1",
                    "name": "foo.md",
                    "note": "Path: foo.md",
                    "children": [
                        {
                            "id": "sent-1",
                            "name": "MARKDOWN",
                            "note": "",
                            "children": [
                                {
                                    "id": "inside-1",
                                    "name": "Inside Sentinel",
                                    "note": "",
                                    "children": [],
                                },
                            ],
                        },
                        {
                            "id": "after-1",
                            "name": "After Sentinel",
                            "note": "",
                            "children": [],
                        },
                    ],
                },
            ],
        },
    ]


def _no_boundary_tree():
    """A breadcrumb chain with NO reset-boundary ancestor: the pre-existing
    root-relative behavior must remain byte-for-byte unchanged."""
    return [
        {
            "id": "home",
            "name": "Home",
            "note": "",
            "children": [
                {
                    "id": "plain-a",
                    "name": "Plain A",
                    "note": "",
                    "children": [
                        {
                            "id": "sent-1",
                            "name": "MARKDOWN",
                            "note": "",
                            "children": [
                                {
                                    "id": "inside-1",
                                    "name": "Inside Sentinel",
                                    "note": "",
                                    "children": [],
                                },
                            ],
                        },
                        {
                            "id": "after-1",
                            "name": "After Sentinel",
                            "note": "",
                            "children": [],
                        },
                    ],
                },
            ],
        },
    ]


def _render(nodes, **kwargs):
    return WorkFlowyClientNexus._build_markdown_preview_from_tree(
        nodes, suppress_metadata=True, **kwargs
    )


def _heading_lines(out):
    return [ln for ln in out.splitlines() if ln.startswith("#")]


def test_breadcrumb_sentinel_reanchors_to_h1():
    out = _render(_mirrored_sentinel_tree())
    lines = _heading_lines(out)

    # The block-1 render (before any breadcrumb) is untouched: outer sentinel
    # and its child at H1, inner sentinel resetting again.
    assert lines[:9] == [
        "# Home",
        "## Workflowy Root",
        "### SCRATCH",
        "# MARKDOWN",
        "# Nested Markdown",
        "## Subtest 1",
        "### Subtest 2",
        "# MARKDOWN",
        "# Subtest 3",
    ]

    # THE GLITCH: the mirrored branch into Subtest 1a must re-render the outer
    # sentinel at H1 ("level 0") and its child at H1 -- not raw H4 / H5.
    idx = lines.index("## Subtest 1a")
    assert lines[idx - 5 : idx] == [
        "# Home",
        "## Workflowy Root",
        "### SCRATCH",
        "# MARKDOWN",  # the mirrored sentinel: reset, was #### MARKDOWN
        "# Nested Markdown",  # its child: reset, was ##### Nested Markdown
    ]
    assert "#### MARKDOWN" not in lines
    assert "##### Nested Markdown" not in lines

    # The content below the breadcrumb was always correct and stays so.
    assert "## Subtest 1a" in lines


def test_breadcrumb_file_node_reanchors_to_h1():
    # Dan: "the same thing applies to file nodes" -- a standalone file node in
    # the mirrored chain renders at H1 and re-anchors the chain below it.
    out = _render(_file_boundary_tree())
    lines = _heading_lines(out)

    idx = lines.index("# After Sentinel")
    assert lines[idx - 2 : idx] == [
        "# Home",
        "# foo.md",  # the mirrored file node: reset, was ## foo.md
    ]
    assert "## foo.md" not in lines


def test_breadcrumb_without_boundary_unchanged():
    # BACKWARD COMPAT: a chain with no sentinel/file ancestor still renders at
    # raw root-relative levels (handoff sec.3 as designed).
    out = _render(_no_boundary_tree())
    lines = _heading_lines(out)

    assert lines == [
        "# Home",
        "## Plain A",
        "# MARKDOWN",
        "# Inside Sentinel",
        "# Home",  # breadcrumb
        "## Plain A",  # breadcrumb: root-relative, unchanged
        "### After Sentinel",
    ]


def test_breadcrumb_sentinel_reset_in_headings_only():
    # The flag is computed from classification_name at visit time, so the
    # headings-only UUID prefix in the emitted name cannot break the reset
    # (the trap behind the earlier headings-only sentinel fix).
    out = _render(_mirrored_sentinel_tree(), headings_only=True)
    lines = _heading_lines(out)

    # No full source-cache operand in this pure renderer fixture: ▸? is
    # deliberate, and must not affect any heading-depth assertion.
    idx = lines.index("## [st1a] Subtest 1a ▸?")
    assert lines[idx - 5 : idx] == [
        "# [home] Home ▸?",
        "## [root] Workflowy Root ▸?",
        "### [scratch] SCRATCH ▸?",
        "# [sent-outer] MARKDOWN ▸?",
        "# [nested-md] Nested Markdown ▸?",
    ]
    assert "#### [sent-outer] MARKDOWN ▸?" not in lines
    assert "##### [nested-md] Nested Markdown ▸?" not in lines
