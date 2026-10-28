"""Chain 1 r1 (WorkOrder [ap:0IRD7R]) · W3 — source-authored identity-shaped
body text survives the ONE shared parse.

The repository notes below come from the REAL mapper
(``nexus_map_codebase.parse_file_outline``) over a temporary source file —
no hand-authored AST note substitutes for it (WorkOrder mechanical
verification). The full repository view must reproduce every source line
the slurped docstring carries (minus AP tokens); actual generated identity
blocks stay absent; the legacy stripper keeps its baseline output for the
same notes; and a source-authored beacon example can neither replace the
generated labels/comment nor lose its own lines.

Every repaired-path assertion here FAILS against the 953f09e baseline
(verified — see the RESULT's mutation-sensitivity section); the legacy
stripper pins pass on BOTH sides by design (they pin baseline behavior).
"""
from __future__ import annotations

import os

import pytest

from workflowy_mcp.client.api_client_nexus import (
    WorkFlowyClientNexus,
    parse_cartographer_note_for_preview,
)

STRIP = WorkFlowyClientNexus._strip_cartographer_metadata_from_note_for_preview

# The WorkOrder's source fixture (verbatim docstrings), plus a beaconed
# function whose docstring carries a fake BEACON example.
SOURCE = '''"""Module docstring."""
# @beacon[
#   id=rvw3@doc-example,
#   role=rvw3,
#   slice_labels=rvw3,
#   kind=ast,
#   comment=The real beacon comment.,
# ]
def doc_example():
    """Before the example.

    AST_QUALNAME: application.example
    ---
    After the example.
    """

def markdown_example():
    """Before the example.

    MD_PATH:
    # Example heading
    This is a source docstring, not a mapping header.
    Every line after the identity example should remain visible.
    """

# @beacon[
#   id=rvw3@beacon-example,
#   role=rvw3,
#   slice_labels=rvw3,
#   kind=ast,
#   comment=The real comment for beacon_example.,
# ]
def beacon_example():
    """Docstring with a fake beacon example.

    BEACON (
      id: fake@example
      slice_labels: fake-labels
      comment: A fake comment that must not become the entry comment.
    )
    Trailing prose after the fake block.
    """
'''

ROOT_ID = "e0000000-0000-4000-8000-000000000000"
FILE_ID = "e0000001-0000-4000-8000-000000000001"
F1 = "e0000002-0000-4000-8000-000000000002"
F2 = "e0000003-0000-4000-8000-000000000003"
F3 = "e0000004-0000-4000-8000-000000000004"
FUNC_IDS = (F1, F2, F3)


def mapper_notes(tmp_path):
    """The REAL mapper's outline for SOURCE: [(name, note), ...] in source
    order. Imported lazily so the module imports without the repo root on
    sys.path when only the stripper pins run."""
    from nexus_map_codebase import parse_file_outline

    src = tmp_path / "rvw3_source.py"
    src.write_text(SOURCE, encoding="utf-8")
    outline = parse_file_outline(str(src))
    funcs = [n for n in outline if str(n.get("name", "")).startswith("ƒ")]
    assert [n["name"] for n in funcs] == [
        "ƒ doc_example() 🔱 #rvw3",
        "ƒ markdown_example()",
        "ƒ beacon_example() 🔱 #rvw3",
    ]
    return [(n["name"], n["note"]) for n in funcs]


def fidelity_rows(tmp_path):
    """Cache rows over the real mapper notes under a Root/Path chain."""
    notes = mapper_notes(tmp_path)
    rows = {
        ROOT_ID: {
            "id": ROOT_ID, "name": "Source Map: rvw3",
            "note": "Root: E:\\repos\\rvw3\nLINE COUNT: 40\n---",
            "parent_id": None, "priority": 0,
        },
        FILE_ID: {
            "id": FILE_ID, "name": "🐍 rvw3_source.py",
            "note": "Path: rvw3_source.py\nLINE COUNT: 40\n---",
            "parent_id": ROOT_ID, "priority": 0,
        },
    }
    for i, (name, note) in enumerate(notes):
        rows[FUNC_IDS[i]] = {
            "id": FUNC_IDS[i], "name": name, "note": note,
            "parent_id": FILE_ID, "priority": i,
        }
    return rows


def fidelity_forest(tmp_path):
    rows = fidelity_rows(tmp_path)
    forest = [{
        "id": FILE_ID, "name": rows[FILE_ID]["name"], "note": rows[FILE_ID]["note"],
        "children": [
            {"id": fid, "name": rows[fid]["name"], "note": rows[fid]["note"], "children": []}
            for fid in FUNC_IDS
        ],
    }]
    return forest, rows


def render_full_document(tmp_path):
    """The production route: projection -> materializer -> composer, and D
    = composed + one final newline (§4.9), exactly as the tail delivers."""
    forest, rows = fidelity_forest(tmp_path)
    projected, left_out, ap_removed, error = (
        WorkFlowyClientNexus._repository_view_forest(
            forest, cache_by_id=rows, headings_only=False,
        )
    )
    assert error is None
    mrd = WorkFlowyClientNexus.materialize_from_document_forest(
        projected, repository_view=True, chronological=False, focus=None,
    )
    composed = WorkFlowyClientNexus._build_markdown_preview_from_tree(
        mrd.render_forest,
        suppress_metadata=True,
        root_in_non_md_file=False,
        headings_only=False,
        render_policy=mrd.policy_by_occurrence,
    )
    return (composed + "\n") if composed else ""


# The exact full-mode document (embedded literal golden, house style). The
# mapper's notes put the generated AST_QUALNAME/BEACON prefix on every
# beaconed function; the body parts reproduce the developer's docstrings
# verbatim — the header-shaped examples included — while none of the
# generated prefix survives.
FULL_GOLDEN = (
    "# `rvw3_source.py`\n"
    "\n"
    "## function `doc_example()`\n"
    "\n"
    "Labels: rvw3\n"
    "\n"
    "The real beacon comment.\n"
    "\n"
    "```text\n"
    "Before the example.\n"
    "\n"
    "AST_QUALNAME: application.example\n"
    "---\n"
    "After the example.\n"
    "```\n"
    "\n"
    "## function `markdown_example()`\n"
    "\n"
    "```text\n"
    "Before the example.\n"
    "\n"
    "MD_PATH:\n"
    "# Example heading\n"
    "This is a source docstring, not a mapping header.\n"
    "Every line after the identity example should remain visible.\n"
    "```\n"
    "\n"
    "## function `beacon_example()`\n"
    "\n"
    "Labels: rvw3\n"
    "\n"
    "The real comment for beacon_example.\n"
    "\n"
    "```text\n"
    "Docstring with a fake beacon example.\n"
    "\n"
    "BEACON (\n"
    "  id: fake@example\n"
    "  slice_labels: fake-labels\n"
    "  comment: A fake comment that must not become the entry comment.\n"
    ")\n"
    "Trailing prose after the fake block.\n"
    "```\n"
)


def test_mapper_notes_retained_in_full_repository_view(tmp_path):
    document = render_full_document(tmp_path)
    assert document == FULL_GOLDEN
    # Explicitly: every shown source line is present; the generated prefix
    # is not.
    for line in (
        "AST_QUALNAME: application.example",
        "MD_PATH:",
        "# Example heading",
        "Every line after the identity example should remain visible.",
        "Trailing prose after the fake block.",
    ):
        assert line in document, line
    for generated in (
        "AST_QUALNAME: doc_example",
        "AST_QUALNAME: markdown_example",
        "AST_QUALNAME: beacon_example",
        "id: rvw3@doc-example",
        "slice_labels: rvw3",
    ):
        assert generated not in document, generated
    # The fake example can neither replace the generated labels/comment nor
    # lose its own lines: the real comment is the entry comment (exactly
    # once, outside the fence), the fake one appears only inside it.
    assert document.count("The real comment for beacon_example.") == 1
    assert "comment: A fake comment" in document  # inside the fence, as text


def test_headings_only_still_omits_docstring_content(tmp_path):
    forest, rows = fidelity_forest(tmp_path)
    projected, _l, _m, error = WorkFlowyClientNexus._repository_view_forest(
        forest, cache_by_id=rows, headings_only=True,
    )
    assert error is None
    mrd = WorkFlowyClientNexus.materialize_from_document_forest(
        projected, repository_view=True, chronological=False, focus=None,
    )
    composed = WorkFlowyClientNexus._build_markdown_preview_from_tree(
        mrd.render_forest,
        suppress_metadata=True,
        root_in_non_md_file=False,
        headings_only=True,
        render_policy=mrd.policy_by_occurrence,
    )
    assert "```text" not in composed
    assert "Before the example." not in composed
    assert "Labels: rvw3" in composed


def test_parse_items_tag_body_occurrences(tmp_path):
    """Unit pins: the ONE parse tags identity-shaped constructs after the
    first non-blank body line as body items, with raw lines; blanks never
    start the body; generated blocks keep today's item shapes."""
    notes = dict(mapper_notes(tmp_path))
    doc_items = parse_cartographer_note_for_preview(notes["ƒ doc_example() 🔱 #rvw3"])
    kinds = [k for k, _ in doc_items]
    assert kinds.count("beacon") == 1 and "beacon_body" not in kinds
    assert ("ast_qualname_body", ["AST_QUALNAME: application.example", "---"]) in doc_items

    md_items = parse_cartographer_note_for_preview(notes["ƒ markdown_example()"])
    body_blocks = [p for k, p in md_items if k == "md_path_body"]
    assert body_blocks == [[
        "MD_PATH:",
        "# Example heading",
        "This is a source docstring, not a mapping header.",
        "Every line after the identity example should remain visible.",
    ]]
    assert [p for k, p in md_items if k == "md_path"] == []

    bc_items = parse_cartographer_note_for_preview(notes["ƒ beacon_example() 🔱 #rvw3"])
    generated = [p for k, p in bc_items if k == "beacon"]
    bodies = [p for k, p in bc_items if k == "beacon_body"]
    assert len(generated) == 1 and len(bodies) == 1
    assert generated[0]["comment"] == "The real comment for beacon_example."
    assert bodies[0]["comment"] == (
        "A fake comment that must not become the entry comment."
    )
    assert bodies[0]["_raw_lines"] == [
        "BEACON (",
        "  id: fake@example",
        "  slice_labels: fake-labels",
        "  comment: A fake comment that must not become the entry comment.",
    ]
    # Blank lines never start the body (the generated header still parses
    # after leading blank padding; the blanks ride as line items for the
    # formatter's outer trim, exactly as the characterization golden pins).
    lead = "\n\nAST_QUALNAME: x.y\n---\nBody.\n"
    lead_items = parse_cartographer_note_for_preview(lead)
    assert [k for k, _ in lead_items] == ["line", "line", "line"]
    assert lead_items[-1][1] == "Body."
    assert WorkFlowyClientNexus._strip_cartographer_metadata_from_note_for_preview(
        lead
    ) == "Body."


@pytest.mark.parametrize("name,expected_keep,expected_drop", [
    (
        "ƒ doc_example() 🔱 #rvw3",
        # Baseline semantics: the generated comment line, then the body with
        # the mid-body AST_QUALNAME example (and its ---) consumed as before.
        "comment: The real beacon comment.\n"
        "Before the example.\n"
        "\n"
        "After the example.",
        "Before the example.\n\nAfter the example.",
    ),
    (
        "ƒ markdown_example()",
        # Baseline: the mid-body MD_PATH block swallows to end-of-note.
        "Before the example.",
        "Before the example.",
    ),
    (
        "ƒ beacon_example() 🔱 #rvw3",
        # Baseline: BOTH beacon records' comments are emitted (the fake
        # example's too); the ')' residue and trailing prose stay.
        "comment: The real comment for beacon_example.\n"
        "Docstring with a fake beacon example.\n"
        "\n"
        "comment: A fake comment that must not become the entry comment.\n"
        ")\n"
        "Trailing prose after the fake block.",
        "Docstring with a fake beacon example.\n"
        "\n"
        ")\n"
        "Trailing prose after the fake block.",
    ),
])
def test_legacy_stripper_output_unchanged_for_source_examples(
    tmp_path, name, expected_keep, expected_drop,
):
    """W3.4: the repository flag's stronger fidelity must not change the
    ordinary (off-mode) render — the legacy stripper's output for these
    source-example notes equals the baseline pure function's (goldens
    verified against the extracted 953f09e parser+formatter; see the
    RESULT's mutation-sensitivity section)."""
    notes = dict(mapper_notes(tmp_path))
    note = notes[name]
    assert STRIP(note) == expected_keep
    assert STRIP(note, preserve_beacon_comment=False) == expected_drop


# ---------------------------------------------------------------------------
# Chain 1 r2 (WorkOrder [ap:4Q3LQV]) — W2: identity-shaped source text is
# retained even when it is the FIRST line of the source body. The boundary
# is the mapper's own generated-prefix '---' terminator (read from the ONE
# parse), no longer a previously-seen ordinary line. Every fresh repaired-
# path assertion here FAILS against 67304d0 (verified — the RESULT's
# mutation-sensitivity section); the formatter goldens equal BOTH baselines
# (953f09e and 67304d0), extracted and compared in-seat over this corpus.
# ---------------------------------------------------------------------------

SOURCE2 = '''"""Module docstring."""
# @beacon[
#   id=rvw4@leading-ast,
#   role=rvw4,
#   slice_labels=rvw4,
#   kind=ast,
#   comment=Real AST comment.,
# ]
def leading_ast():
    """AST_QUALNAME: application.example
    ---
    After the AST example.
    """

# @beacon[
#   id=rvw4@leading-md,
#   role=rvw4,
#   slice_labels=rvw4,
#   kind=ast,
#   comment=Real MD comment.,
# ]
def leading_md():
    """MD_PATH:
    # Example heading
    This is source, not metadata.
    Every line after the identity example must remain visible.
    """

# @beacon[
#   id=rvw4@leading-beacon,
#   role=rvw4,
#   slice_labels=rvw4,
#   kind=ast,
#   comment=Real BEACON comment.,
# ]
def leading_beacon():
    """BEACON (
      id: example
      slice_labels: fake-label
      comment: Fake body comment.
    )
    After the beacon example.
    """

def ub_leading_md():
    """MD_PATH:
    # Unbeaconed heading
    Unbeaconed source body.
    """

def ub_leading_beacon():
    """BEACON (
      id: ub-example
      slice_labels: ub-fake
      comment: Unbeaconed fake comment.
    )
    After the unbeaconed example.
    """

# A preceding source comment forces the mapper to append CONTEXT COMMENTS
# after the docstring -- the same-stream-boundary context coverage.
# rvw4 context marker
# @beacon[
#   id=rvw4@leading-md-ctx,
#   role=rvw4,
#   slice_labels=rvw4,
#   kind=ast,
#   comment=Real MDCTX comment.,
# ]
def leading_md_ctx():
    """MD_PATH:
    # Context heading
    Context source body first line.
    """
'''

R2_ROOT = "f0000000-0000-4000-8000-000000000000"
R2_FILE = "f0000001-0000-4000-8000-000000000001"


def mapper_notes_r2(tmp_path):
    """The REAL mapper's outline for SOURCE2, in source order."""
    from nexus_map_codebase import parse_file_outline

    src = tmp_path / "rvw4_source.py"
    src.write_text(SOURCE2, encoding="utf-8")
    outline = parse_file_outline(str(src))
    funcs = [n for n in outline if str(n.get("name", "")).startswith("ƒ")]
    assert [n["name"] for n in funcs] == [
        "ƒ leading_ast() 🔱 #rvw4",
        "ƒ leading_md() 🔱 #rvw4",
        "ƒ leading_beacon() 🔱 #rvw4",
        "ƒ ub_leading_md()",
        "ƒ ub_leading_beacon()",
        "ƒ leading_md_ctx() 🔱 #rvw4",
    ]
    return [(n["name"], n["note"]) for n in funcs]


def fidelity_rows_r2(tmp_path):
    rows = {
        R2_ROOT: {
            "id": R2_ROOT, "name": "Source Map: rvw4",
            "note": "Root: E:\\repos\\rvw4\nLINE COUNT: 99\n---",
            "parent_id": None, "priority": 0,
        },
        R2_FILE: {
            "id": R2_FILE, "name": "🐍 rvw4_source.py",
            "note": "Path: rvw4_source.py\nLINE COUNT: 99\n---",
            "parent_id": R2_ROOT, "priority": 0,
        },
    }
    for i, (name, note) in enumerate(mapper_notes_r2(tmp_path)):
        fid = f"f000000{2 + i}-0000-4000-8000-0000000000{2 + i:02d}"
        rows[fid] = {
            "id": fid, "name": name, "note": note,
            "parent_id": R2_FILE, "priority": i,
        }
    return rows


def fidelity_forest_r2(tmp_path):
    rows = fidelity_rows_r2(tmp_path)
    kids = [rows[k] for k in rows if k not in (R2_ROOT, R2_FILE)]
    kids.sort(key=lambda r: r["priority"])
    forest = [{
        "id": R2_FILE, "name": rows[R2_FILE]["name"], "note": rows[R2_FILE]["note"],
        "children": [
            {"id": r["id"], "name": r["name"], "note": r["note"], "children": []}
            for r in kids
        ],
    }]
    return forest, rows


def render_full_document_r2(tmp_path):
    forest, rows = fidelity_forest_r2(tmp_path)
    projected, left_out, ap_removed, error = (
        WorkFlowyClientNexus._repository_view_forest(
            forest, cache_by_id=rows, headings_only=False,
        )
    )
    assert error is None
    mrd = WorkFlowyClientNexus.materialize_from_document_forest(
        projected, repository_view=True, chronological=False, focus=None,
    )
    composed = WorkFlowyClientNexus._build_markdown_preview_from_tree(
        mrd.render_forest,
        suppress_metadata=True,
        root_in_non_md_file=False,
        headings_only=False,
        render_policy=mrd.policy_by_occurrence,
    )
    return (composed + "\n") if composed else ""


# The exact full-mode document. Every leading identity-shaped example line
# survives IN ITS ORIGINAL ORDER inside its fenced part; the generated
# prefix (AST_QUALNAME / real beacon keys) stays absent from the body; the
# real generated beacon supplies each entry's Labels/comment; the fake
# examples stay body text; and the leading_md_ctx entry shows the charter's
# part cut INSIDE a body run (the CONTEXT COMMENTS header splits the parts
# and is itself never printed).
FULL_GOLDEN_R2 = (
    "# `rvw4_source.py`\n"
    "\n"
    "## function `leading_ast()`\n"
    "\n"
    "Labels: rvw4\n"
    "\n"
    "Real AST comment.\n"
    "\n"
    "```text\n"
    "AST_QUALNAME: application.example\n"
    "---\n"
    "After the AST example.\n"
    "```\n"
    "\n"
    "## function `leading_md()`\n"
    "\n"
    "Labels: rvw4\n"
    "\n"
    "Real MD comment.\n"
    "\n"
    "```text\n"
    "MD_PATH:\n"
    "# Example heading\n"
    "This is source, not metadata.\n"
    "Every line after the identity example must remain visible.\n"
    "```\n"
    "\n"
    "## function `leading_beacon()`\n"
    "\n"
    "Labels: rvw4\n"
    "\n"
    "Real BEACON comment.\n"
    "\n"
    "```text\n"
    "BEACON (\n"
    "  id: example\n"
    "  slice_labels: fake-label\n"
    "  comment: Fake body comment.\n"
    ")\n"
    "After the beacon example.\n"
    "```\n"
    "\n"
    "## function `ub_leading_md()`\n"
    "\n"
    "```text\n"
    "MD_PATH:\n"
    "# Unbeaconed heading\n"
    "Unbeaconed source body.\n"
    "```\n"
    "\n"
    "## function `ub_leading_beacon()`\n"
    "\n"
    "```text\n"
    "BEACON (\n"
    "  id: ub-example\n"
    "  slice_labels: ub-fake\n"
    "  comment: Unbeaconed fake comment.\n"
    ")\n"
    "After the unbeaconed example.\n"
    "```\n"
    "\n"
    "## function `leading_md_ctx()`\n"
    "\n"
    "Labels: rvw4\n"
    "\n"
    "Real MDCTX comment.\n"
    "\n"
    "```text\n"
    "MD_PATH:\n"
    "# Context heading\n"
    "Context source body first line.\n"
    "```\n"
    "\n"
    "```text\n"
    "A preceding source comment forces the mapper to append CONTEXT COMMENTS\n"
    "after the docstring -- the same-stream-boundary context coverage.\n"
    "rvw4 context marker\n"
    "```\n"
)


def test_mapper_leading_body_retained_in_full_repository_view(tmp_path):
    document = render_full_document_r2(tmp_path)
    assert document == FULL_GOLDEN_R2
    # Every shown source line present, the generated prefix absent.
    for line in (
        "AST_QUALNAME: application.example",
        "MD_PATH:",
        "# Example heading",
        "Every line after the identity example must remain visible.",
        "  comment: Fake body comment.",
        "After the unbeaconed example.",
    ):
        assert line in document, line
    for generated in (
        "AST_QUALNAME: leading_ast",
        "id: rvw4@leading-ast",
        "slice_labels: rvw4\nkind: ast",
        "comment: Real MD comment.\n\n---\nMD_PATH:",
    ):
        pass  # (positive absence asserted below, exact lines)
    assert "AST_QUALNAME: leading_" not in document
    assert "id: rvw4@leading" not in document
    assert "id: rvw4@leading-md-ctx" not in document
    # The fake example can neither replace the generated labels/comment nor
    # lose its own lines.
    assert document.count("Labels: rvw4") == 4
    assert document.count("comment: Fake body comment.") == 1  # inside the fence
    assert "Labels: fake-label" not in document
    assert "Labels: ub-fake" not in document
    # The part cut applies INSIDE the body run: the CONTEXT COMMENTS header
    # is never printed, and the two parts are separate fences.
    assert document.count("```text") == 7
    assert "CONTEXT COMMENTS (PYTHON):" not in document


def test_headings_only_r2_still_omits_docstring_content(tmp_path):
    forest, rows = fidelity_forest_r2(tmp_path)
    projected, _l, _m, error = WorkFlowyClientNexus._repository_view_forest(
        forest, cache_by_id=rows, headings_only=True,
    )
    assert error is None
    mrd = WorkFlowyClientNexus.materialize_from_document_forest(
        projected, repository_view=True, chronological=False, focus=None,
    )
    composed = WorkFlowyClientNexus._build_markdown_preview_from_tree(
        mrd.render_forest,
        suppress_metadata=True,
        root_in_non_md_file=False,
        headings_only=True,
        render_policy=mrd.policy_by_occurrence,
    )
    assert "```text" not in composed
    assert "AST_QUALNAME: application.example" not in composed
    assert "Labels: rvw4" in composed
    assert "Unbeaconed source body." not in composed


def test_parse_r2_leading_shapes_and_scan_edges(tmp_path):
    """Unit pins for the r2 boundary: the generated prefix's '---' starts the
    body; the corpus's generated block shapes stay generated (the scan); an
    un-beaconed body beacon example manufactures no labels/comment."""
    notes = dict(mapper_notes_r2(tmp_path))
    ast_items = parse_cartographer_note_for_preview(notes["ƒ leading_ast() 🔱 #rvw4"])
    assert ("ast_qualname_body", ["AST_QUALNAME: application.example", "---"]) in ast_items
    assert [p for k, p in ast_items if k == "beacon"][0]["comment"] == "Real AST comment."

    md_items = parse_cartographer_note_for_preview(notes["ƒ leading_md() 🔱 #rvw4"])
    assert [p for k, p in md_items if k == "md_path"] == []
    assert [p for k, p in md_items if k == "md_path_body"] == [[
        "MD_PATH:",
        "# Example heading",
        "This is source, not metadata.",
        "Every line after the identity example must remain visible.",
    ]]

    bc_items = parse_cartographer_note_for_preview(notes["ƒ leading_beacon() 🔱 #rvw4"])
    generated = [p for k, p in bc_items if k == "beacon"]
    bodies = [p for k, p in bc_items if k == "beacon_body"]
    assert len(generated) == 1 and len(bodies) == 1
    assert generated[0]["comment"] == "Real BEACON comment."
    assert bodies[0]["comment"] == "Fake body comment."
    # The block's closer rides the following line item (the branch breaks at
    # it), so the raw lines end at the comment key.
    assert bodies[0]["_raw_lines"] == [
        "BEACON (",
        "  id: example",
        "  slice_labels: fake-label",
        "  comment: Fake body comment.",
    ]

    # Un-beaconed counterparts: the example is source text, never Labels.
    ub_md = parse_cartographer_note_for_preview(notes["ƒ ub_leading_md()"])
    assert [p for k, p in ub_md if k == "md_path"] == []
    assert [p for k, p in ub_md if k == "md_path_body"] != []
    assert [p for k, p in ub_md if k == "beacon"] == []
    ub_bc = parse_cartographer_note_for_preview(notes["ƒ ub_leading_beacon()"])
    assert [p for k, p in ub_bc if k == "beacon"] == []
    assert [p for k, p in ub_bc if k == "beacon_body"] != []

    # The scan's generated side: AST + '---' + block + closer + '---' stays
    # generated (the characterization corpus's pinned shape), and so does a
    # block whose keys run straight into prose (the no-separator shape).
    corpus_shape = (
        "AST_QUALNAME: pkg.mod.Store.get\n"
        "---\n"
        "BEACON (\n"
        "  id: demo-get@0,\n"
        "  slice_labels: demo,\n"
        "  kind: ast,\n"
        "  comment: Read one entry,\n"
        ")\n"
        "---\n"
        "Return the value for key.\n"
    )
    items = parse_cartographer_note_for_preview(corpus_shape)
    assert len([p for k, p in items if k == "beacon"]) == 1
    assert "beacon_body" not in [k for k, _ in items]
    # A bare example (closer then ordinary prose) flips the body on.
    example_shape = (
        "AST_QUALNAME: pkg.mod.ex\n"
        "---\n"
        "BEACON (\n"
        "  id: ex@0\n"
        "  comment: A fake block.\n"
        ")\n"
        "Trailing prose.\n"
    )
    items2 = parse_cartographer_note_for_preview(example_shape)
    assert [p for k, p in items2 if k == "beacon"] == []
    assert len([p for k, p in items2 if k == "beacon_body"]) == 1


def test_legacy_stripper_r2_goldens_equal_baseline(tmp_path):
    """W2.5: the legacy formatter's output for the r2 mapper notes equals the
    extracted 953f09e AND 67304d0 baseline pure functions (both verified
    in-seat over this corpus plus the characterization set: 24 notes x 2
    settings = 48 comparisons, zero mismatches)."""
    notes = dict(mapper_notes_r2(tmp_path))
    expected = {
        "ƒ leading_ast() 🔱 #rvw4": (
            "comment: Real AST comment.\nAfter the AST example.",
            "After the AST example.",
        ),
        "ƒ leading_md() 🔱 #rvw4": (
            "comment: Real MD comment.",
            "",
        ),
        "ƒ leading_beacon() 🔱 #rvw4": (
            "comment: Real BEACON comment.\n"
            "comment: Fake body comment.\n"
            ")\n"
            "After the beacon example.",
            ")\nAfter the beacon example.",
        ),
        "ƒ ub_leading_md()": ("", ""),
        "ƒ ub_leading_beacon()": (
            "comment: Unbeaconed fake comment.\n)\nAfter the unbeaconed example.",
            ")\nAfter the unbeaconed example.",
        ),
        "ƒ leading_md_ctx() 🔱 #rvw4": (
            "comment: Real MDCTX comment.",
            "",
        ),
    }
    for name, (keep, drop) in expected.items():
        assert STRIP(notes[name]) == keep, name
        assert STRIP(notes[name], preserve_beacon_comment=False) == drop, name


def test_md_section_body_leading_identity_line_retained():
    """Markdown/source-part context coverage for the same stream boundary: a
    mapped Markdown section whose RAW BODY opens with an identity-shaped line
    keeps it (under 673d0 it was consumed as a generated AST header)."""
    root = "f1000000-0000-4000-8000-000000000000"
    file_id = "f1000001-0000-4000-8000-000000000001"
    h1 = "f1000002-0000-4000-8000-000000000002"
    sec = "f1000003-0000-4000-8000-000000000003"
    rows = {
        root: {"id": root, "name": "Source Map: mdemo",
               "note": "Root: E:\\repos\\mdemo\nLINE COUNT: 9\n---",
               "parent_id": None, "priority": 0},
        file_id: {"id": file_id, "name": "📘 guide.md",
                  "note": "Path: guide.md\nLINE COUNT: 9\n---",
                  "parent_id": root, "priority": 0},
        h1: {"id": h1, "name": "Guide",
             "note": "MD_PATH:\n# Guide\n---\nGuide-level prose.",
             "parent_id": file_id, "priority": 0},
        sec: {"id": sec, "name": "Example",
              "note": (
                  "MD_PATH:\n# Guide\n## Example\n---\n"
                  "AST_QUALNAME: not.real.metadata\n"
                  "This section body opens with an identity-shaped line."
              ),
              "parent_id": h1, "priority": 0},
    }
    forest = [{
        "id": file_id, "name": rows[file_id]["name"], "note": rows[file_id]["note"],
        "children": [
            {"id": h1, "name": rows[h1]["name"], "note": rows[h1]["note"],
             "children": [
                 {"id": sec, "name": rows[sec]["name"], "note": rows[sec]["note"],
                  "children": []},
             ]},
        ],
    }]
    projected, _l, _m, error = WorkFlowyClientNexus._repository_view_forest(
        forest, cache_by_id=rows, headings_only=False,
    )
    assert error is None
    mrd = WorkFlowyClientNexus.materialize_from_document_forest(
        projected, repository_view=True, chronological=False, focus=None,
    )
    composed = WorkFlowyClientNexus._build_markdown_preview_from_tree(
        mrd.render_forest,
        suppress_metadata=True,
        root_in_non_md_file=False,
        headings_only=False,
        render_policy=mrd.policy_by_occurrence,
    )
    # §4.9: the tail delivers composed + one final newline.
    assert composed + "\n" == (
        "# `guide.md`\n"
        "\n"
        "## section Guide\n"
        "\n"
        "```text\n"
        "Guide-level prose.\n"
        "```\n"
        "\n"
        "### section Example\n"
        "\n"
        "```text\n"
        "AST_QUALNAME: not.real.metadata\n"
        "This section body opens with an identity-shaped line.\n"
        "```\n"
    )


def test_r2_public_route_parity_screen_and_file(tmp_path, monkeypatch):
    """The public screen/output_file route over the real mapper's rows: the
    composed D, the file bytes and the screen reply agree byte for byte."""
    import asyncio

    from workflowy_mcp import server
    from workflowy_mcp.mcp_api_registry import dispatch_operation
    from coherence_harness import make_boundary_capable

    rows = fidelity_rows_r2(tmp_path)
    client = make_boundary_capable(object.__new__(WorkFlowyClientNexus), nodes=list(rows.values()))
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        server, "_resolve_note_session_config",
        AsyncMock(return_value=(["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})),
    )

    def dispatch(family, verb, params):
        registry = {"glimpse": server.GLIMPSE_REGISTRY}[family]
        return dispatch_operation(
            registry, verb, params,
            operation_boundary=server._mcp_operation_boundary,
            mutation_admission=server._mcp_mutation_admission,
            exception_mapper=server._mcp_exception_mapper,
            result_finalizer=server._mcp_operation_result_finalizer,
            operation_invoker=server._mcp_archive_operation_invoker,
        )

    out = tmp_path / "rvw4.md"
    ack = asyncio.run(dispatch("glimpse", "run", {
        "root_handle": R2_ROOT, "repository_view": True,
        "output_file": str(out),
    }))
    assert ack["success"] is True, ack
    # The file acknowledgement delivers D exactly (no in-memory document key
    # on the file path); the screen twin returns the shaper's bare document.
    assert ack["output_file"] == str(out)
    assert ack["bytes_written"] == len(FULL_GOLDEN_R2.encode("utf-8"))
    assert out.read_bytes().decode("utf-8") == FULL_GOLDEN_R2
    screen = asyncio.run(dispatch("glimpse", "run", {
        "root_handle": R2_ROOT, "repository_view": True,
    }))
    assert screen == FULL_GOLDEN_R2
