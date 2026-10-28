"""Acceptance tests for the zoom-inside-source-file root-regime seed.

Background
----------
The unified Markdown-preview composer
``WorkFlowyClientNexus._build_markdown_preview_from_tree`` (shared by both
``as_markdown`` GLIMPSE and the docked Markdown modal) decides a node's
file-context regime by threading an ``in_non_md_file`` flag DOWN the visible
tree it is handed. That threading is correct for every Cartographer file
boundary that occurs WITHIN the received tree (each such boundary is itself a
visible node and re-seeds its descendants) -- but it is blind to a non-Markdown
FILE ancestor ABOVE the received root. When the docked modal / a GLIMPSE is
zoomed STRICTLY INSIDE a source file (e.g. ``foo.py``), that file's FILE node is
absent from the sent tree, so the walk historically defaulted the root to
plain/unmapped and WRONGLY promoted source-code ``#``-comment notes to Markdown
headings (emitted UNFENCED).

The fix adds a ``root_in_non_md_file`` seed to the composer, computed by the
caller from the canonical, zoom-proof ``_classify_node_map_state`` over the
/nodes-export cache (which has the full ancestor chain regardless of zoom),
surfaced through the shared helper
``WorkFlowyClientNexus._root_in_non_md_file_from_cache``. Seeding only the ROOT
is provably sufficient because in-tree threading is only ever wrong at the root.

These tests assert: the bug reproduces unseeded; the seed fixes it on the root
AND on siblings; a visible ``.md`` boundary inside the seeded source subtree
still re-seeds correctly; the seed is a no-op when the file node is visible
(Case A); the genuine markdown-content promotion feature still works; and the
shared cache-classification helper returns the right verdict across regimes.
"""
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus as W,
)


# A tree zoomed STRICTLY INSIDE foo.py: the .py FILE node is NOT present.
# The root and a sibling both carry source-code '#'-comment notes; a nested
# .md FILE node carries a genuine Markdown heading.
ZOOM_INSIDE_PY = [
    {
        "id": "r",
        "name": "def outer()",
        "note": "# srccomment\nx=1",
        "children": [
            {
                "id": "mdfile",
                "name": "doc.md",
                "note": "Path: doc.md",
                "children": [
                    {
                        "id": "h",
                        "name": "Real Heading",
                        "note": "MD_PATH:\n# Real Heading\n---\nbody text",
                    }
                ],
            },
            {
                "id": "sib",
                "name": "def sibling()",
                "note": "## another comment",
                "children": [],
            },
        ],
    }
]


def _fake_cache(nodes):
    return {str(n["id"]): n for n in nodes}


def test_unseeded_reproduces_the_bug():
    """Without the seed, source '#'-comment notes are mis-promoted (unfenced)."""
    out = W._build_markdown_preview_from_tree(ZOOM_INSIDE_PY, suppress_metadata=True)
    assert "# srccomment" in out
    assert "```text\n# srccomment" not in out
    assert "## another comment" in out
    assert "```text\n## another comment" not in out


def test_seeded_fences_source_comments_on_root_and_sibling():
    """root_in_non_md_file=True fences source '#'-comment notes everywhere."""
    out = W._build_markdown_preview_from_tree(
        ZOOM_INSIDE_PY, suppress_metadata=True, root_in_non_md_file=True
    )
    assert "```text\n# srccomment\n```" in out
    assert "```text\n## another comment\n```" in out


def test_seeded_in_tree_md_boundary_still_reseeds():
    """A visible .md FILE node inside the seeded source subtree re-seeds, so its
    real Markdown heading renders as a real heading (not fenced)."""
    out = W._build_markdown_preview_from_tree(
        ZOOM_INSIDE_PY, suppress_metadata=True, root_in_non_md_file=True
    )
    assert "# doc.md" in out
    assert "# Real Heading" in out
    assert "```text\n# Real Heading" not in out


def test_case_a_visible_file_node_makes_seed_a_noop():
    """When the .py FILE node IS in the tree (Case A), threading is already
    correct and the seed is a no-op: seeded output == default output."""
    case_a = [
        {
            "id": "f",
            "name": "py-file",
            "note": "Path: foo.py",
            "children": [
                {
                    "id": "r",
                    "name": "def outer()",
                    "note": "# srccomment\nx=1",
                    "children": [],
                }
            ],
        }
    ]
    default = W._build_markdown_preview_from_tree(case_a, suppress_metadata=True)
    seeded = W._build_markdown_preview_from_tree(
        case_a, suppress_metadata=True, root_in_non_md_file=True
    )
    assert "```text\n# srccomment\n```" in default
    assert default == seeded


def test_genuine_markdown_content_node_embeds_one_level_deeper():
    """handoff sec.4 (retraction of H1 promotion): a real markdown-content node
    OUTSIDE any source file is NO LONGER promoted to a fresh H1 block. Its NAME
    renders at its NORMAL structural level and its body headings are EMBEDDED
    one level deeper (container_level + 1). For a top-level node (level H1) the
    embedded ``# Real MD Heading`` becomes ``## Real MD Heading`` -- NOT H1, and
    NOT fenced (the headings are intentional content)."""
    md_content = [
        {
            "id": "note1",
            "name": "My Note",
            "note": "# Real MD Heading\n\nSome prose.",
            "children": [],
        }
    ]
    out = W._build_markdown_preview_from_tree(md_content, suppress_metadata=True)
    # NAME at its normal (top-level) structural level == H1.
    assert "# My Note" in out
    # Body heading embedded one level deeper (H1 -> H2), not promoted to H1.
    assert "## Real MD Heading" in out
    # Not fenced (headings are intentional content, not source comments).
    assert "```text\n# Real MD Heading" not in out
    assert "```text\n## Real MD Heading" not in out
    # Prose preserved.
    assert "Some prose." in out


# ---------------------------------------------------------------------------
# handoff 2026-06-22 -- file/sentinel EXIT resumes ROOT-RELATIVE structural
# levels (+ heading-only breadcrumb delimiter), and markdown-in-markdown note
# bodies embed one level beneath the containing node (no H1 promotion / no
# structural-boundary reset). Source-file '#'-comment fencing is preserved.
# ---------------------------------------------------------------------------


def test_source_file_comment_stays_fenced_when_root_seeded():
    """handoff sec.5: a source node note containing a '#'-comment line is
    hazard-fenced (rendered as a ```text block), never as a Markdown heading,
    when the render root is seeded inside a non-Markdown source file."""
    tree = [
        {
            "id": "r",
            "name": "def outer()",
            "note": "# source comment\nx = 1",
            "children": [],
        }
    ]
    out = W._build_markdown_preview_from_tree(
        tree, suppress_metadata=True, root_in_non_md_file=True
    )
    assert "```text\n# source comment\n```" in out
    # The '#'-comment must NOT appear as a raw Markdown heading line.
    assert "\n# source comment" not in out.replace("```text\n# source comment", "")


def test_file_block_exit_resumes_root_relative_structural_level():
    """handoff sec.3: after a FILE block closes, the following sibling resumes
    at its natural ROOT-RELATIVE level (child-of-root => H2), NOT promoted back
    to H1."""
    tree = [
        {
            "id": "root",
            "name": "Root Structural",
            "note": "",
            "children": [
                {
                    "id": "f",
                    "name": "doc.md",
                    "note": "Path: doc.md",
                    "children": [
                        {
                            "id": "h",
                            "name": "Heading In File",
                            "note": "MD_PATH:\n# Heading In File\n---\nbody",
                        }
                    ],
                },
                {
                    "id": "after",
                    "name": "After File Sibling",
                    "note": "",
                    "children": [],
                },
            ],
        }
    ]
    out = W._build_markdown_preview_from_tree(tree, suppress_metadata=True)
    lines = out.splitlines()
    # The sibling after the file is a child of the root -> root-relative H2.
    assert "## After File Sibling" in lines
    # It must NOT have been promoted back to H1.
    assert "# After File Sibling" not in lines


def test_file_block_exit_emits_breadcrumb_delimiter():
    """handoff sec.3: when the post-file sibling is deeper than the root, the
    renderer re-emits the heading-only structural ancestor chain (breadcrumb
    delimiter) before it, so the reader is relocated back into the global tree.
    The repeated root heading is the intentional delimiter."""
    tree = [
        {
            "id": "root",
            "name": "Doc Root",
            "note": "",
            "children": [
                {
                    "id": "mid",
                    "name": "Section",
                    "note": "",
                    "children": [
                        {
                            "id": "f",
                            "name": "doc.md",
                            "note": "Path: doc.md",
                            "children": [
                                {
                                    "id": "h",
                                    "name": "H In File",
                                    "note": "MD_PATH:\n# H In File\n---\nbody",
                                }
                            ],
                        },
                        {
                            "id": "after",
                            "name": "After In Section",
                            "note": "",
                            "children": [],
                        },
                    ],
                }
            ],
        }
    ]
    out = W._build_markdown_preview_from_tree(tree, suppress_metadata=True)
    # 'Doc Root' appears once as the document top AND again as the breadcrumb
    # delimiter after the file block closes -> at least twice.
    assert out.count("# Doc Root") >= 2
    # The post-file node resumes at its root-relative level (grandchild => H3).
    assert "### After In Section" in out.splitlines()


def test_sentinel_exit_resumes_root_relative_structural_level():
    """handoff sec.3: same as the file-block case but with a MARKDOWN reset
    sentinel. After the sentinel subtree closes, the following sibling resumes
    at its root-relative level (child-of-root => H2), not H1."""
    tree = [
        {
            "id": "root",
            "name": "Root Structural",
            "note": "",
            "children": [
                {
                    "id": "sent",
                    "name": "MARKDOWN: reset here",
                    "note": "",
                    "children": [
                        {
                            "id": "under",
                            "name": "Under Sentinel",
                            "note": "",
                            "children": [],
                        }
                    ],
                },
                {
                    "id": "after",
                    "name": "After Sentinel Sibling",
                    "note": "",
                    "children": [],
                },
            ],
        }
    ]
    out = W._build_markdown_preview_from_tree(tree, suppress_metadata=True)
    lines = out.splitlines()
    # Sentinel renders at H1 and re-anchors its descendant to H1.
    assert "# MARKDOWN: reset here" in lines
    assert "# Under Sentinel" in lines
    # The sibling after the sentinel resumes its root-relative level (H2).
    assert "## After Sentinel Sibling" in lines
    assert "# After Sentinel Sibling" not in lines


def test_markdown_in_markdown_embeds_one_level_deeper_nested():
    """handoff sec.4: a markdown-content node nested at H3 embeds its body
    headings at H4/H5 (container_level + 1, relative structure preserved),
    NOT H1/H2, and NOT fenced."""
    tree = [
        {
            "id": "root",
            "name": "Root",
            "note": "",
            "children": [
                {
                    "id": "l2",
                    "name": "Level Two",
                    "note": "",
                    "children": [
                        {
                            "id": "l3",
                            "name": "Container At H3",
                            "note": "# Embedded\n\n## Detail",
                            "children": [],
                        }
                    ],
                }
            ],
        }
    ]
    out = W._build_markdown_preview_from_tree(tree, suppress_metadata=True)
    lines = out.splitlines()
    # Container renders at its normal structural level (grandchild => H3).
    assert "### Container At H3" in lines
    # Body headings embedded one level deeper: H1->H4, H2->H5.
    assert "#### Embedded" in lines
    assert "##### Detail" in lines
    # Not promoted to H1/H2, not fenced.
    assert "# Embedded" not in lines
    assert "```text\n# Embedded" not in out


def test_markdown_in_markdown_is_not_a_structural_boundary():
    """handoff sec.4: a markdown-content node does NOT reset the structural run.
    A sibling AFTER it resumes the surrounding (root-relative) level rather
    than starting a fresh H1 run."""
    tree = [
        {
            "id": "root",
            "name": "Root",
            "note": "",
            "children": [
                {
                    "id": "mdc",
                    "name": "MD Content Child",
                    "note": "# Body Heading\n\nprose",
                    "children": [],
                },
                {
                    "id": "sib",
                    "name": "Plain Sibling",
                    "note": "",
                    "children": [],
                },
            ],
        }
    ]
    out = W._build_markdown_preview_from_tree(tree, suppress_metadata=True)
    lines = out.splitlines()
    # Both children of root resume at root-relative H2 (no boundary reset, no
    # breadcrumb delimiter triggered by the markdown-content node).
    assert "## MD Content Child" in lines
    assert "## Plain Sibling" in lines
    assert "# Plain Sibling" not in lines


@pytest.mark.parametrize(
    "label, cache, root_id, expected",
    [
        (
            "inside .py -> True",
            _fake_cache(
                [
                    {"id": "filenode", "note": "Path: foo.py", "parent_id": None},
                    {"id": "r", "note": "# srccomment", "parent_id": "filenode"},
                ]
            ),
            "r",
            True,
        ),
        (
            "inside .md -> False",
            _fake_cache(
                [
                    {"id": "filenode", "note": "Path: doc.md", "parent_id": None},
                    {"id": "h", "note": "MD_PATH:\n# H\n---", "parent_id": "filenode"},
                ]
            ),
            "h",
            False,
        ),
        (
            "the .py FILE node itself (state=file) -> False",
            _fake_cache(
                [
                    {"id": "filenode", "note": "Path: foo.py", "parent_id": None},
                ]
            ),
            "filenode",
            False,
        ),
        (
            "inside a folder (no file) -> False",
            _fake_cache(
                [
                    {"id": "root", "note": "Root: E:/repo", "parent_id": None},
                    {"id": "sub", "note": "", "parent_id": "root"},
                ]
            ),
            "sub",
            False,
        ),
        (
            "unmapped -> False",
            _fake_cache(
                [
                    {"id": "x", "note": "", "parent_id": None},
                    {"id": "y", "note": "plain", "parent_id": "x"},
                ]
            ),
            "y",
            False,
        ),
    ],
)
def test_root_in_non_md_file_from_cache(label, cache, root_id, expected):
    assert W._root_in_non_md_file_from_cache(root_id, cache) is expected


def test_root_in_non_md_file_from_cache_failsoft():
    """Empty cache / None root -> False (fail-soft to prior behavior)."""
    assert W._root_in_non_md_file_from_cache("r", {}) is False
    assert W._root_in_non_md_file_from_cache(None, {"r": {"id": "r"}}) is False
