"""Regression lock: MARKDOWN sentinel H1 reset works in headings_only renders.

THE BUG: in the unified composer (_build_markdown_preview_from_tree), the
headings_only UUID prefix was applied to ``name`` BEFORE branch
classification, and ``_is_markdown_heading_reset_sentinel(name)`` consumed
that prefixed name. The detector's leading-token test then saw the UUID's
first segment (``[cd4e0811-...] MARKDOWN`` -> lead token ``cd4e0811``)
instead of ``MARKDOWN``, so the sentinel branch never fired in
headings-only renders: the sentinel and its descendants rendered at their
natural root-relative depths (####, #####, ...) instead of resetting to H1.
Non-headings-only renders were unaffected (no prefix applied).

THE FIX: a ``classification_name`` snapshot of the semantic name is taken
BEFORE the emission-only UUID prefix is applied, and the sentinel predicate
consumes that. File nodes / MD_PATH headings were never affected (their
predicates read raw_note, not name) -- the file-node test below guards that
unaffected path against future meddling.

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


def _sentinel_tree():
    return [
        {
            "id": "root-1",
            "name": "Root",
            "note": "",
            "children": [
                {
                    "id": "lvl-1",
                    "name": "Level One",
                    "note": "",
                    "children": [
                        {
                            "id": "lvl-2",
                            "name": "Level Two",
                            "note": "",
                            "children": [
                                {
                                    "id": "sent-1",
                                    "name": "MARKDOWN",
                                    "note": "",
                                    "children": [
                                        {
                                            "id": "child-1",
                                            "name": "Child Alpha",
                                            "note": "alpha body",
                                            "children": [
                                                {
                                                    "id": "grand-1",
                                                    "name": "Grandchild Beta",
                                                    "note": "beta body",
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
        {"id": "sib-1", "name": "Sibling After", "note": "sib body", "children": []},
    ]


def _render(nodes, **kwargs):
    return WorkFlowyClientNexus._build_markdown_preview_from_tree(
        nodes, suppress_metadata=True, **kwargs
    )


def test_headings_only_sentinel_resets_to_h1():
    out = _render(_sentinel_tree(), headings_only=True)
    lines = out.splitlines()

    # The sentinel renders at H1 (the bug rendered it at its natural H4).
    assert "# [sent-1] MARKDOWN ▸?" in lines
    assert "#### [sent-1] MARKDOWN ▸?" not in lines

    # Its descendants re-anchor: direct child H1, grandchild H2.
    assert "# [child-1] Child Alpha ▸?" in lines
    assert "## [grand-1] Grandchild Beta ▸?" in lines
    assert "##### [child-1] Child Alpha ▸?" not in lines
    assert "###### [grand-1] Grandchild Beta ▸?" not in lines


def test_headings_only_uuid_prefixes_intact_everywhere():
    out = _render(_sentinel_tree(), headings_only=True)

    # Every heading still carries its [uuid] prefix (the prefix feature the
    # fix must preserve), including the reset sentinel block.
    for needle in (
        "# [root-1] Root ▸?",
        "## [lvl-1] Level One ▸?",
        "### [lvl-2] Level Two ▸?",
        "# [sent-1] MARKDOWN ▸?",
        "# [child-1] Child Alpha ▸?",
        "## [grand-1] Grandchild Beta ▸?",
    ):
        assert needle in out.splitlines(), needle


def test_headings_only_file_node_still_resets_to_h1():
    # GUARD the unaffected path: file-node detection reads raw_note (never
    # the prefixed name), so file nodes reset correctly in headings_only
    # both before and after the fix. This locks that behavior.
    tree = [
        {
            "id": "outer-1",
            "name": "Outer",
            "note": "",
            "children": [
                {
                    "id": "file-1",
                    "name": "foo.md",
                    "note": "Path: foo.md",
                    "children": [
                        {"id": "fchild-1", "name": "Inside File", "note": "body", "children": []},
                    ],
                },
            ],
        },
    ]
    out = _render(tree, headings_only=True)
    lines = out.splitlines()

    assert "# [outer-1] Outer ▸?" in lines
    # [ap:UH0UOU] the file node's note opens with a Path: header -- a managed
    # source line -- so it carries NO end-of-line metadata (not even ▸?).
    assert "# [file-1] foo.md" in lines
    assert "# [file-1] foo.md ▸" not in out
    assert "# [fchild-1] Inside File ▸?" in lines


def test_non_headings_only_sentinel_unchanged():
    # Lock the pre-existing correct behavior: without headings_only there is
    # no UUID prefix, and the sentinel reset worked all along.
    out = _render(_sentinel_tree())
    lines = out.splitlines()

    assert "# MARKDOWN" in lines
    assert "# Child Alpha" in lines
    assert "## Grandchild Beta" in lines
    assert "[sent-1]" not in out
