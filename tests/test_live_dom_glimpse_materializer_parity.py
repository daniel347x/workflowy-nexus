"""Unit C (contract [ap:W53A69], gap [ap:PT5FM7] ledger CF-7) — the bare
live-DOM ``workflowy_glimpse`` compose routes through the ONE materializer.

Dan's bar for this unit is BYTE-IDENTITY on the primary render path, so this
file is a golden lock across the live-DOM render matrix, with goldens in two
generations:

  * ``GOLDENS_PRE`` — captured from the PRE-UNIT-C tree (base commit
    ``29a060a``): today's output, byte for byte, for every matrix cell.
  * ``GOLDENS_POST`` — captured from the POST-fix tree for the
    CHRONOLOGICAL cells (the only cells whose bytes may change).

Two lock classes:

  1. PARITY cells (document order and every non-chronological sort/since
     combination, headings-only renders included) assert the output STILL
     equals ``GOLDENS_PRE`` byte for byte. The document-order case is
     guarded explicitly (the rule is inert there: ancestor note bodies
     PRESENT). ``GOLDENS_PRE == GOLDENS_POST`` is asserted for these
     cells, so the pre/post golden generations themselves are parity
     evidence, not just the live run. (The bare item-count tail ``'last
     1'`` alone LEFT this class under CF-13 = Option A — Dan's ruling
     2026-09-22, relayed Manager-Session-033: its excerpt order is still
     document order, but ancestor notes are now withheld, so it is a
     CHRONOLOGICAL cell.)

  2. CHRONOLOGICAL cells (``sort_by`` created|modified × asc|desc; ``'last
     N'`` alone OR + sort — the item-count form counts per CF-13 Option A;
     a ``'1w'`` window with OR without a presentation sort — the window
     form carries its OWN axis per ``apply_since_to_forest`` and is
     chronological per ``render_is_chronological`` either way) assert:
       a. heading lines byte-identical to the PRE golden (headings, rows,
          counts, order — the contract's bar);
       b. every ancestor note body WITHHELD (unique markers absent) while
          every surviving leaf note body PRINTS (markers present);
       c. the exact POST golden bytes (permanent regression lock).

Surface-level sibling of ``tests/test_chronological_ancestor_notes.py``
(which locks the materializer mechanism directly): this file drives the REAL
async client method over a fake WebSocket — the pattern of
``tests/test_headings_child_counts.py::test_live_dom_glimpse_counts_use_cache_not_dom_visibility``
— so the lock covers the producer site end to end: normalization → root
seed → ``_reorder_tree_by_timestamp`` → ``apply_since_to_forest`` →
MATERIALIZE → ``_build_markdown_preview_from_tree``, for BOTH markdown
compose sites (the in-memory compose and the ``output_file`` fallback
re-render, the latter driven by a forced first-compose failure).

The full/UUID (``as_markdown=False``) branch is pinned UNCHANGED: that
branch never reorders and never applies ``since`` (document-order structural
inspection by construction), so the chronological rule never applies to it
and the PRE goldens prove this unit did not touch it.

A second fixture (md-heading notes) exercises the composer's embedded-
Markdown regime and NON-EMPTY ``rebase_by_render_key`` policy entries on
this surface — pinning the Step-4 policy-vs-inline parity promise
(unfocused: a present entry equals the inline computation) end to end, not
only its empty-set common case.

No live account: fake WS + the S03-B boundary-capable client double.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys

import pytest
from coherence_harness import make_boundary_capable

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import server  # noqa: E402
from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    WorkFlowyClientNexus,
)
from workflowy_mcp.glimpse_since_policy import since_from_edge  # noqa: E402

# ---------------------------------------------------------------------------
# fixture 1 — the plain rich tree (the tether's shape)
#
#   R (ROOT-NOTE-BODY …)            created=50        modified=50
#   ├── M (MID-NOTE-BODY)           created=60        modified=60
#   │   └── L1 (LEAF-ONE-BODY)      created=1_000_000 modified=1_000_000
#   └── L2 (LEAF-TWO-BODY)          created=100       modified=2_000_000
#
# created-asc  ⇒ branches L2(100) then L1(1M):  R>L2, R>M>L1 (R re-emitted —
#                the straddled-ancestor occurrence shape)
# created-desc ⇒ R>M>L1, R>L2
# modified-asc ⇒ branch keys 1M (R,M,L1) then 2M (R,L2): R>M>L1, R>L2
# modified-desc⇒ R>L2, R>M>L1
# '1w' window (own axis created/asc; pivot = 1M − 604800) ⇒ only the L1
#                branch survives, with OR without a presentation sort
# 'last 1'     ⇒ document order: keeps L2 (R>L2); after created-asc sort:
#                keeps the sorted order's last leaf L1 (R>M>L1)
# ---------------------------------------------------------------------------

R = "11111111-1111-4111-8111-111111111111"
M = "22222222-2222-4222-8222-222222222222"
L1 = "33333333-3333-4333-8333-333333333333"
L2 = "44444444-4444-4444-8444-444444444444"

ROOT_MARK = "ROOT-NOTE-BODY"
MID_MARK = "MID-NOTE-BODY"
LEAF1_MARK = "LEAF-ONE-BODY"
LEAF2_MARK = "LEAF-TWO-BODY"


def _cache_rows_plain():
    return [
        {"id": R, "name": "Rich root", "parent_id": None, "priority": 0,
         "note": f"{ROOT_MARK}: the whole framing document lives here.\n\n"
                 "Second paragraph of the root note.",
         "createdAt": 50, "modifiedAt": 50},
        {"id": M, "name": "Middle ancestor", "parent_id": R, "priority": 1,
         "note": MID_MARK, "createdAt": 60, "modifiedAt": 60},
        {"id": L1, "name": "Leaf one", "parent_id": M, "priority": 2,
         "note": LEAF1_MARK, "createdAt": 1_000_000, "modifiedAt": 1_000_000},
        {"id": L2, "name": "Leaf two", "parent_id": R, "priority": 3,
         "note": LEAF2_MARK, "createdAt": 100, "modifiedAt": 2_000_000},
    ]


def _dom_forest_plain():
    """The DOM-extracted {root, children} payload shape the extension sends."""
    return {
        "id": R, "name": "Rich root",
        "note": f"{ROOT_MARK}: the whole framing document lives here.\n\n"
                "Second paragraph of the root note.",
        "children": [
            {"id": M, "name": "Middle ancestor", "note": MID_MARK, "children": [
                {"id": L1, "name": "Leaf one", "note": LEAF1_MARK, "children": []},
            ]},
            {"id": L2, "name": "Leaf two", "note": LEAF2_MARK, "children": []},
        ],
    }


# ---------------------------------------------------------------------------
# fixture 2 — md-heading notes (the embedded-Markdown regime + NON-EMPTY
# rebase_by_render_key policy entries on this surface)
#
#   R2 (MD-ROOT-NOTE-BODY)          created=10
#   ├── DA note '# Chapter A …'     created=200
#   └── DB note '# Chapter B …'     created=100
# ---------------------------------------------------------------------------

R2 = "55555555-5555-4555-8555-555555555555"
DA = "66666666-6666-4666-8666-666666666666"
DB = "77777777-7777-4777-8777-777777777777"

MD_ROOT_MARK = "MD-ROOT-NOTE-BODY"
MD_A_MARK = "ALPHA-MD-BODY"
MD_B_MARK = "BETA-MD-BODY"


def _cache_rows_md():
    return [
        {"id": R2, "name": "Md root", "parent_id": None, "priority": 0,
         "note": MD_ROOT_MARK, "createdAt": 10, "modifiedAt": 10},
        {"id": DA, "name": "Chapter A", "parent_id": R2, "priority": 1,
         "note": f"# Chapter A heading\n\n{MD_A_MARK} text.",
         "createdAt": 200, "modifiedAt": 200},
        {"id": DB, "name": "Chapter B", "parent_id": R2, "priority": 2,
         "note": f"# Chapter B heading\n\n{MD_B_MARK} text.",
         "createdAt": 100, "modifiedAt": 100},
    ]


def _dom_forest_md():
    return {
        "id": R2, "name": "Md root", "note": MD_ROOT_MARK,
        "children": [
            {"id": DA, "name": "Chapter A",
             "note": f"# Chapter A heading\n\n{MD_A_MARK} text.", "children": []},
            {"id": DB, "name": "Chapter B",
             "note": f"# Chapter B heading\n\n{MD_B_MARK} text.", "children": []},
        ],
    }


_FIXTURES = {
    "plain": (_cache_rows_plain, _dom_forest_plain, (4, 3)),
    "md": (_cache_rows_md, _dom_forest_md, (3, 2)),
}

# ---------------------------------------------------------------------------
# the render matrix
# ---------------------------------------------------------------------------

MATRIX = {
    # ---- PARITY cells (byte-identical pre == post) ----
    "doc_order": {"kwargs": {}},
    "doc_order_headings": {"kwargs": {"headings_only": True}},
    "headings_created_asc": {"kwargs": {"sort_by": "created", "headings_only": True}},
    "md_doc_order": {"kwargs": {}, "fixture": "md"},
    "full_doc_order": {"kwargs": {"as_markdown": False}, "full": True},
    "full_created_asc": {"kwargs": {"as_markdown": False, "sort_by": "created"},
                         "full": True},
    "full_headings_created_asc": {
        "kwargs": {"as_markdown": False, "sort_by": "created", "headings_only": True},
        "full": True},
    "outfile_doc_order": {"kwargs": {}, "output_file": True, "force_fallback": True},
    # ---- CHRONOLOGICAL cells (ancestor bodies withheld; headings identical) ----
    "created_asc": {"kwargs": {"sort_by": "created"}, "chrono": True},
    "created_desc": {"kwargs": {"sort_by": "created", "sort_direction": "desc"},
                     "chrono": True},
    "modified_asc": {"kwargs": {"sort_by": "modified"}, "chrono": True},
    "modified_desc": {"kwargs": {"sort_by": "modified", "sort_direction": "desc"},
                      "chrono": True},
    "last1_alone": {"kwargs": {"since": since_from_edge("last 1")},
                    "chrono": True},
    "last1_created_asc": {"kwargs": {"sort_by": "created",
                                     "since": since_from_edge("last 1")},
                          "chrono": True},
    "window_modified_desc": {"kwargs": {"sort_by": "modified",
                                        "sort_direction": "desc",
                                        "since": since_from_edge("1w")},
                             "chrono": True},
    "window_no_sort": {"kwargs": {"since": since_from_edge("1w")}, "chrono": True},
    "md_created_asc": {"kwargs": {"sort_by": "created"}, "fixture": "md",
                       "chrono": True},
    "outfile_created_asc": {"kwargs": {"sort_by": "created"}, "output_file": True,
                            "force_fallback": True, "chrono": True},
}

PARITY_CELLS = sorted(n for n, c in MATRIX.items() if not c.get("chrono"))
CHRONO_CELLS = sorted(n for n, c in MATRIX.items() if c.get("chrono"))

# Per-cell body-marker expectations for the chronological cells:
#   absent  — note bodies that must NOT appear (ancestors: suppressed;
#             pruned leaves: not in the final forest at all)
#   present — surviving leaf note bodies that MUST appear
CHRONO_EXPECT = {
    "created_asc": {"absent": [ROOT_MARK, MID_MARK],
                    "present": [LEAF1_MARK, LEAF2_MARK]},
    "created_desc": {"absent": [ROOT_MARK, MID_MARK],
                     "present": [LEAF1_MARK, LEAF2_MARK]},
    "modified_asc": {"absent": [ROOT_MARK, MID_MARK],
                     "present": [LEAF1_MARK, LEAF2_MARK]},
    "modified_desc": {"absent": [ROOT_MARK, MID_MARK],
                      "present": [LEAF1_MARK, LEAF2_MARK]},
    "last1_alone": {"absent": [ROOT_MARK, MID_MARK, LEAF1_MARK],
                    "present": [LEAF2_MARK]},
    "last1_created_asc": {"absent": [ROOT_MARK, MID_MARK, LEAF2_MARK],
                          "present": [LEAF1_MARK]},
    "window_modified_desc": {"absent": [ROOT_MARK, MID_MARK, LEAF2_MARK],
                             "present": [LEAF1_MARK]},
    "window_no_sort": {"absent": [ROOT_MARK, MID_MARK, LEAF2_MARK],
                       "present": [LEAF1_MARK]},
    "md_created_asc": {"absent": [MD_ROOT_MARK],
                       "present": [MD_A_MARK, MD_B_MARK]},
    "outfile_created_asc": {"absent": [ROOT_MARK, MID_MARK],
                            "present": [LEAF1_MARK, LEAF2_MARK]},
}

# --- GOLDENS-PRE-START ---
# Captured verbatim from the PRE-UNIT-C tree at base commit 29a060a
# (main, clean) via the fake-WS driver below — today's output, byte for byte.
# RE-CAPTURED 2026-10-02 for the two MARKDOWN headings cells
# (doc_order_headings, headings_created_asc): [ap:UH0UOU] deliberately changed
# heading bytes (dock.peek's four-field end-of-line metrics replaced the bare
# child count), so the Unit-C pre/post identity was re-established at the new
# bytes. The parity property itself (live-DOM == goldens) is unchanged.
GOLDENS_PRE: dict[str, str] = {
    'created_asc': (
        "# Rich root\n\nROOT-NOTE-BODY: the whole framing document lives here.\n\nSecond paragraph of the root note.\n\n## Leaf two\n\nLEAF-TWO-BODY\n\n## Middle ancestor\n\nMID-NOTE-BODY\n\n### Leaf one\n\nLEAF-ONE-BODY"
    ),
    'created_desc': (
        "# Rich root\n\nROOT-NOTE-BODY: the whole framing document lives here.\n\nSecond paragraph of the root note.\n\n## Middle ancestor\n\nMID-NOTE-BODY\n\n### Leaf one\n\nLEAF-ONE-BODY\n\n## Leaf two\n\nLEAF-TWO-BODY"
    ),
    'doc_order': (
        "# Rich root\n\nROOT-NOTE-BODY: the whole framing document lives here.\n\nSecond paragraph of the root note.\n\n## Middle ancestor\n\nMID-NOTE-BODY\n\n### Leaf one\n\nLEAF-ONE-BODY\n\n## Leaf two\n\nLEAF-TWO-BODY"
    ),
    'doc_order_headings': (
        "# [11111111] Rich root \u25b82 \u00b7 desc:3 \u00b7 self:99B \u00b7 tree:169B\n\n## [22222222] Middle ancestor \u25b81 \u00b7 desc:1 \u00b7 self:28B \u00b7 tree:49B\n\n### [33333333] Leaf one \u25b80 \u00b7 desc:0 \u00b7 self:21B \u00b7 tree:21B\n\n## [44444444] Leaf two \u25b80 \u00b7 desc:0 \u00b7 self:21B \u00b7 tree:21B"
    ),
    'full_created_asc': (
        "[11111111-1111-4111-8111-111111111111] [WG-1    ] \u29bf Rich root [ROOT-NOTE-BODY: the whole framing document lives here.\\n\\nSecond paragraph of the root note.]\n[22222222-2222-4222-8222-222222222222] [WG-1.1  ]     \u29bf Middle ancestor [MID-NOTE-BODY]\n[33333333-3333-4333-8333-333333333333] [WG-1.1.1]         \u2022 Leaf one [LEAF-ONE-BODY]\n[44444444-4444-4444-8444-444444444444] [WG-1.2  ]     \u2022 Leaf two [LEAF-TWO-BODY]"
    ),
    'full_doc_order': (
        "[11111111-1111-4111-8111-111111111111] [WG-1    ] \u29bf Rich root [ROOT-NOTE-BODY: the whole framing document lives here.\\n\\nSecond paragraph of the root note.]\n[22222222-2222-4222-8222-222222222222] [WG-1.1  ]     \u29bf Middle ancestor [MID-NOTE-BODY]\n[33333333-3333-4333-8333-333333333333] [WG-1.1.1]         \u2022 Leaf one [LEAF-ONE-BODY]\n[44444444-4444-4444-8444-444444444444] [WG-1.2  ]     \u2022 Leaf two [LEAF-TWO-BODY]"
    ),
    'full_headings_created_asc': (
        "[11111111-1111-4111-8111-111111111111] [WG-1    ] \u29bf Rich root \u25b82\n[22222222-2222-4222-8222-222222222222] [WG-1.1  ]     \u29bf Middle ancestor \u25b81\n[33333333-3333-4333-8333-333333333333] [WG-1.1.1]         \u2022 Leaf one \u25b80\n[44444444-4444-4444-8444-444444444444] [WG-1.2  ]     \u2022 Leaf two \u25b80"
    ),
    'headings_created_asc': (
        "# [11111111] Rich root \u25b82 \u00b7 desc:3 \u00b7 self:99B \u00b7 tree:169B\n\n## [44444444] Leaf two \u25b80 \u00b7 desc:0 \u00b7 self:21B \u00b7 tree:21B\n\n## [22222222] Middle ancestor \u25b81 \u00b7 desc:1 \u00b7 self:28B \u00b7 tree:49B\n\n### [33333333] Leaf one \u25b80 \u00b7 desc:0 \u00b7 self:21B \u00b7 tree:21B"
    ),
    'last1_alone': (
        "# Rich root\n\nROOT-NOTE-BODY: the whole framing document lives here.\n\nSecond paragraph of the root note.\n\n## Leaf two\n\nLEAF-TWO-BODY"
    ),
    'last1_created_asc': (
        "# Rich root\n\nROOT-NOTE-BODY: the whole framing document lives here.\n\nSecond paragraph of the root note.\n\n## Middle ancestor\n\nMID-NOTE-BODY\n\n### Leaf one\n\nLEAF-ONE-BODY"
    ),
    'md_created_asc': (
        "# Md root\n\nMD-ROOT-NOTE-BODY\n\n## Chapter B\n\n### Chapter B heading\n\nBETA-MD-BODY text.\n\n## Chapter A\n\n### Chapter A heading\n\nALPHA-MD-BODY text."
    ),
    'md_doc_order': (
        "# Md root\n\nMD-ROOT-NOTE-BODY\n\n## Chapter A\n\n### Chapter A heading\n\nALPHA-MD-BODY text.\n\n## Chapter B\n\n### Chapter B heading\n\nBETA-MD-BODY text."
    ),
    'modified_asc': (
        "# Rich root\n\nROOT-NOTE-BODY: the whole framing document lives here.\n\nSecond paragraph of the root note.\n\n## Middle ancestor\n\nMID-NOTE-BODY\n\n### Leaf one\n\nLEAF-ONE-BODY\n\n## Leaf two\n\nLEAF-TWO-BODY"
    ),
    'modified_desc': (
        "# Rich root\n\nROOT-NOTE-BODY: the whole framing document lives here.\n\nSecond paragraph of the root note.\n\n## Leaf two\n\nLEAF-TWO-BODY\n\n## Middle ancestor\n\nMID-NOTE-BODY\n\n### Leaf one\n\nLEAF-ONE-BODY"
    ),
    'outfile_created_asc': (
        "# Rich root\n\nROOT-NOTE-BODY: the whole framing document lives here.\n\nSecond paragraph of the root note.\n\n## Leaf two\n\nLEAF-TWO-BODY\n\n## Middle ancestor\n\nMID-NOTE-BODY\n\n### Leaf one\n\nLEAF-ONE-BODY\n"
    ),
    'outfile_doc_order': (
        "# Rich root\n\nROOT-NOTE-BODY: the whole framing document lives here.\n\nSecond paragraph of the root note.\n\n## Middle ancestor\n\nMID-NOTE-BODY\n\n### Leaf one\n\nLEAF-ONE-BODY\n\n## Leaf two\n\nLEAF-TWO-BODY\n"
    ),
    'window_modified_desc': (
        "# Rich root\n\nROOT-NOTE-BODY: the whole framing document lives here.\n\nSecond paragraph of the root note.\n\n## Middle ancestor\n\nMID-NOTE-BODY\n\n### Leaf one\n\nLEAF-ONE-BODY"
    ),
    'window_no_sort': (
        "# Rich root\n\nROOT-NOTE-BODY: the whole framing document lives here.\n\nSecond paragraph of the root note.\n\n## Middle ancestor\n\nMID-NOTE-BODY\n\n### Leaf one\n\nLEAF-ONE-BODY"
    ),
}
# --- GOLDENS-PRE-END ---

# --- GOLDENS-POST-START ---
# Captured verbatim from the POST-Unit-C tree (the fix). For the PARITY
# cells these equal GOLDENS_PRE byte for byte (the in-suite assertion in
# test_parity_cells_byte_identical_to_pre_unit_golden makes that identity
# permanent evidence); for the CHRONOLOGICAL cells they are the exact
# post-rule bytes (ancestors heading-only) and lock them against regressions.
GOLDENS_POST: dict[str, str] = {
    'created_asc': (
        "# Rich root\n\n## Leaf two\n\nLEAF-TWO-BODY\n\n## Middle ancestor\n\n### Leaf one\n\nLEAF-ONE-BODY"
    ),
    'created_desc': (
        "# Rich root\n\n## Middle ancestor\n\n### Leaf one\n\nLEAF-ONE-BODY\n\n## Leaf two\n\nLEAF-TWO-BODY"
    ),
    'doc_order': (
        "# Rich root\n\nROOT-NOTE-BODY: the whole framing document lives here.\n\nSecond paragraph of the root note.\n\n## Middle ancestor\n\nMID-NOTE-BODY\n\n### Leaf one\n\nLEAF-ONE-BODY\n\n## Leaf two\n\nLEAF-TWO-BODY"
    ),
    'doc_order_headings': (
        "# [11111111] Rich root \u25b82 \u00b7 desc:3 \u00b7 self:99B \u00b7 tree:169B\n\n## [22222222] Middle ancestor \u25b81 \u00b7 desc:1 \u00b7 self:28B \u00b7 tree:49B\n\n### [33333333] Leaf one \u25b80 \u00b7 desc:0 \u00b7 self:21B \u00b7 tree:21B\n\n## [44444444] Leaf two \u25b80 \u00b7 desc:0 \u00b7 self:21B \u00b7 tree:21B"
    ),
    'full_created_asc': (
        "[11111111-1111-4111-8111-111111111111] [WG-1    ] \u29bf Rich root [ROOT-NOTE-BODY: the whole framing document lives here.\\n\\nSecond paragraph of the root note.]\n[22222222-2222-4222-8222-222222222222] [WG-1.1  ]     \u29bf Middle ancestor [MID-NOTE-BODY]\n[33333333-3333-4333-8333-333333333333] [WG-1.1.1]         \u2022 Leaf one [LEAF-ONE-BODY]\n[44444444-4444-4444-8444-444444444444] [WG-1.2  ]     \u2022 Leaf two [LEAF-TWO-BODY]"
    ),
    'full_doc_order': (
        "[11111111-1111-4111-8111-111111111111] [WG-1    ] \u29bf Rich root [ROOT-NOTE-BODY: the whole framing document lives here.\\n\\nSecond paragraph of the root note.]\n[22222222-2222-4222-8222-222222222222] [WG-1.1  ]     \u29bf Middle ancestor [MID-NOTE-BODY]\n[33333333-3333-4333-8333-333333333333] [WG-1.1.1]         \u2022 Leaf one [LEAF-ONE-BODY]\n[44444444-4444-4444-8444-444444444444] [WG-1.2  ]     \u2022 Leaf two [LEAF-TWO-BODY]"
    ),
    'full_headings_created_asc': (
        "[11111111-1111-4111-8111-111111111111] [WG-1    ] \u29bf Rich root \u25b82\n[22222222-2222-4222-8222-222222222222] [WG-1.1  ]     \u29bf Middle ancestor \u25b81\n[33333333-3333-4333-8333-333333333333] [WG-1.1.1]         \u2022 Leaf one \u25b80\n[44444444-4444-4444-8444-444444444444] [WG-1.2  ]     \u2022 Leaf two \u25b80"
    ),
    'headings_created_asc': (
        "# [11111111] Rich root \u25b82 \u00b7 desc:3 \u00b7 self:99B \u00b7 tree:169B\n\n## [44444444] Leaf two \u25b80 \u00b7 desc:0 \u00b7 self:21B \u00b7 tree:21B\n\n## [22222222] Middle ancestor \u25b81 \u00b7 desc:1 \u00b7 self:28B \u00b7 tree:49B\n\n### [33333333] Leaf one \u25b80 \u00b7 desc:0 \u00b7 self:21B \u00b7 tree:21B"
    ),
    'last1_alone': (
        "# Rich root\n\n## Leaf two\n\nLEAF-TWO-BODY"
    ),
    'last1_created_asc': (
        "# Rich root\n\n## Middle ancestor\n\n### Leaf one\n\nLEAF-ONE-BODY"
    ),
    'md_created_asc': (
        "# Md root\n\n## Chapter B\n\n### Chapter B heading\n\nBETA-MD-BODY text.\n\n## Chapter A\n\n### Chapter A heading\n\nALPHA-MD-BODY text."
    ),
    'md_doc_order': (
        "# Md root\n\nMD-ROOT-NOTE-BODY\n\n## Chapter A\n\n### Chapter A heading\n\nALPHA-MD-BODY text.\n\n## Chapter B\n\n### Chapter B heading\n\nBETA-MD-BODY text."
    ),
    'modified_asc': (
        "# Rich root\n\n## Middle ancestor\n\n### Leaf one\n\nLEAF-ONE-BODY\n\n## Leaf two\n\nLEAF-TWO-BODY"
    ),
    'modified_desc': (
        "# Rich root\n\n## Leaf two\n\nLEAF-TWO-BODY\n\n## Middle ancestor\n\n### Leaf one\n\nLEAF-ONE-BODY"
    ),
    'outfile_created_asc': (
        "# Rich root\n\n## Leaf two\n\nLEAF-TWO-BODY\n\n## Middle ancestor\n\n### Leaf one\n\nLEAF-ONE-BODY\n"
    ),
    'outfile_doc_order': (
        "# Rich root\n\nROOT-NOTE-BODY: the whole framing document lives here.\n\nSecond paragraph of the root note.\n\n## Middle ancestor\n\nMID-NOTE-BODY\n\n### Leaf one\n\nLEAF-ONE-BODY\n\n## Leaf two\n\nLEAF-TWO-BODY\n"
    ),
    'window_modified_desc': (
        "# Rich root\n\n## Middle ancestor\n\n### Leaf one\n\nLEAF-ONE-BODY"
    ),
    'window_no_sort': (
        "# Rich root\n\n## Middle ancestor\n\n### Leaf one\n\nLEAF-ONE-BODY"
    ),
}
# --- GOLDENS-POST-END ---


# ---------------------------------------------------------------------------
# driver — the fake-WS pattern of tests/test_headings_child_counts.py
# ---------------------------------------------------------------------------

def _make_client(fixture: str):
    rows_fn, _, _ = _FIXTURES[fixture]
    return make_boundary_capable(
        object.__new__(WorkFlowyClientNexus), nodes=rows_fn()
    )


def _break_first_compose(client):
    """Force the in-memory compose to fail ONCE so the ``output_file``
    fallback re-render (the second live-DOM compose site) fires."""
    real = WorkFlowyClientNexus._build_markdown_preview_from_tree
    calls = {"n": 0}

    def flaky(nodes, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(
                "forced first-compose failure (drives the output_file fallback)"
            )
        return real(nodes, **kwargs)

    client._build_markdown_preview_from_tree = flaky


async def _drive(client, forest_root, stats, **kwargs):
    queue: asyncio.Queue = asyncio.Queue()

    class Socket:
        async def send(self, raw):
            request = json.loads(raw)
            root = copy.deepcopy(forest_root)
            await queue.put({
                "success": True,
                "request_id": request["request_id"],
                "root": root,
                "children": root["children"],
                "node_count": stats[0],
                "depth": stats[1],
            })

    async with server._mcp_operation_boundary("glimpse.run"):
        return await client.workflowy_glimpse(
            _ws_connection=Socket(), _ws_queue=queue, **kwargs
        )


def _run_combo(name: str, tmp_path) -> str:
    combo = MATRIX[name]
    fixture = combo.get("fixture", "plain")
    rows_fn, forest_fn, stats = _FIXTURES[fixture]
    client = _make_client(fixture)
    if combo.get("force_fallback"):
        _break_first_compose(client)
    kwargs = dict(combo["kwargs"])
    outfile = None
    if combo.get("output_file"):
        outfile = tmp_path / f"unitc_{name}.md"
        kwargs["output_file"] = str(outfile)
    # server._mcp_operation_boundary resolves the client via server.get_client
    # — point it at the double for the call (the pattern's setup() does the
    # same via monkeypatch; here it is scoped manually around the drive).
    _orig_get_client = server.get_client
    server.get_client = lambda: client
    try:
        result = asyncio.run(_drive(client, forest_fn(), stats, **kwargs))
    finally:
        server.get_client = _orig_get_client
    assert result["success"] is True, result
    # The envelope counts are the EXTENSION's (this surface never recomputes
    # them — pre- and post-unit alike): pin them so the unit cannot silently
    # start substituting materializer stats.
    assert result["node_count"] == stats[0], result
    assert result["depth"] == stats[1], result
    if outfile is not None:
        data = outfile.read_bytes()
        assert result["bytes_written"] == len(data), result
        return data.decode("utf-8")
    if combo.get("full"):
        return "\n".join(result["preview_tree"])
    return result["preview_markdown"]


def _heading_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("#")]


# ---------------------------------------------------------------------------
# the locks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", PARITY_CELLS)
def test_parity_cells_byte_identical_to_pre_unit_golden(name, tmp_path):
    """Document order and every non-chronological combination reproduce
    today's (pre-unit) output EXACTLY — the contract's byte-identity bar."""
    if not GOLDENS_PRE:
        pytest.skip("PRE goldens not yet embedded (capture phase)")
    out = _run_combo(name, tmp_path)
    assert out == GOLDENS_PRE[name], (
        f"byte-parity broken for {name!r}:\n--- got ---\n{out}\n"
        f"--- golden ---\n{GOLDENS_PRE[name]}"
    )
    if name in GOLDENS_POST:
        # The two golden generations themselves are parity evidence.
        assert GOLDENS_PRE[name] == GOLDENS_POST[name], name


def test_doc_order_rule_is_inert_explicit(tmp_path):
    """The contract's explicit guard: in document order the ancestor notes
    are PRESENT (the rule must not fire there)."""
    if not GOLDENS_PRE:
        pytest.skip("PRE goldens not yet embedded (capture phase)")
    out = _run_combo("doc_order", tmp_path)
    assert ROOT_MARK in out and MID_MARK in out
    assert LEAF1_MARK in out and LEAF2_MARK in out


@pytest.mark.parametrize("name", CHRONO_CELLS)
def test_chronological_cells_suppress_ancestors_keep_headings(name, tmp_path):
    """Chronological cells: heading lines byte-identical to the PRE golden
    (headings, rows, counts, order — the contract's bar); ancestor note
    bodies withheld; surviving leaf bodies printed."""
    if not GOLDENS_PRE:
        pytest.skip("PRE goldens not yet embedded (capture phase)")
    out = _run_combo(name, tmp_path)
    pre = GOLDENS_PRE[name]
    assert _heading_lines(out) == _heading_lines(pre), (
        f"heading/order/counts drift for {name!r}:\n"
        f"got:      {_heading_lines(out)}\n"
        f"pre-unit: {_heading_lines(pre)}"
    )
    expect = CHRONO_EXPECT[name]
    for marker in expect["absent"]:
        assert marker not in out, f"{marker!r} must be withheld in {name!r}"
    for marker in expect["present"]:
        assert marker in out, f"{marker!r} (a surviving leaf body) must print in {name!r}"


@pytest.mark.parametrize("name", CHRONO_CELLS)
def test_chronological_cells_exact_post_bytes(name, tmp_path):
    """Exact post-fix byte lock for every chronological cell (regression)."""
    if not GOLDENS_POST:
        pytest.skip("POST goldens not yet embedded (capture phase)")
    out = _run_combo(name, tmp_path)
    assert out == GOLDENS_POST[name], (
        f"post-fix byte drift for {name!r}:\n--- got ---\n{out}\n"
        f"--- golden ---\n{GOLDENS_POST[name]}"
    )


def test_full_mode_never_chronological_notes_present(tmp_path):
    """The full/UUID branch never reorders ⇒ never chronological ⇒ ancestor
    [note] bodies stay even when a sort is requested (branch untouched)."""
    if not GOLDENS_PRE:
        pytest.skip("PRE goldens not yet embedded (capture phase)")
    out = _run_combo("full_created_asc", tmp_path)
    assert ROOT_MARK in out and MID_MARK in out
    assert LEAF1_MARK in out and LEAF2_MARK in out
