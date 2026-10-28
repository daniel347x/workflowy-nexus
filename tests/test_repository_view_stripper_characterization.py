"""Characterization pin: the ONE note parse behind the preview stripper.

U1 (MasterSpec §8 T7, gate G3): the identity-block loop of
``_strip_cartographer_metadata_from_note_for_preview`` was split into the
module-level :func:`parse_cartographer_note_for_preview` (moved, not copied),
and the stripper became a formatter over it. Every golden below was captured
from the PRE-move in-line loop on ff4bb84 and must stay byte-identical after
the move -- for both ``preserve_beacon_comment`` settings. The notes are
real-shaped cache notes (AST with/without beacon, span with CONTEXT COMMENTS
+ SPAN TEXT, MD_PATH heading, JS block, virtual group, no-separator beacon,
file plumbing, alias key, blank padding, plain prose, empty).
"""
from __future__ import annotations

import pytest

from workflowy_mcp.client.api_client_nexus import (
    WorkFlowyClientNexus,
    parse_cartographer_note_for_preview,
)

STRIP = WorkFlowyClientNexus._strip_cartographer_metadata_from_note_for_preview

# name -> (note, expected_keep, expected_drop). Goldens captured from the
# pre-move loop (ff4bb84); nothing here is invented.
_NOTES: dict[str, tuple[str, str, str]] = {
    "ast_with_beacon_and_docstring": (
        "AST_QUALNAME: pkg.mod.Store.get\n"
        "---\n"
        "BEACON (\n"
        "  id: demo-get@0,\n"
        "  slice_labels: demo,\n"
        "  kind: ast,\n"
        "  comment: Read one entry,\n"
        ")\n"
        "---\n"
        "Return the value for key.\n",
        "comment: Read one entry,\n)\n---\nReturn the value for key.",
        ")\n---\nReturn the value for key.",
    ),
    "ast_without_beacon": (
        "AST_QUALNAME: pkg.mod.plain\n---\nJust a docstring.\n",
        "Just a docstring.",
        "Just a docstring.",
    ),
    "ast_beacon_no_comment": (
        "AST_QUALNAME: pkg.mod.noc\n"
        "---\n"
        "BEACON (\n"
        "  id: noc@0,\n"
        "  slice_labels: demo,\n"
        "  kind: ast,\n"
        ")\n"
        "---\n"
        "Body after blockless comment.\n",
        ")\n---\nBody after blockless comment.",
        ")\n---\nBody after blockless comment.",
    ),
    "span_with_context_and_span_text": (
        "BEACON (\n"
        "  id: limits@0,\n"
        "  slice_labels: demo,\n"
        "  kind: span,\n"
        "  show_span: true,\n"
        "  comment: Size limits,\n"
        ")\n"
        "\n"
        "CONTEXT COMMENTS (PYTHON):\n"
        "Tunable caps.\n"
        "\n"
        "SPAN TEXT:\n"
        "MAX = 10\n"
        "MIN = 1\n",
        "comment: Size limits,\n"
        ")\n\nCONTEXT COMMENTS (PYTHON):\nTunable caps.\n\nSPAN TEXT:\nMAX = 10\nMIN = 1",
        ")\n\nCONTEXT COMMENTS (PYTHON):\nTunable caps.\n\nSPAN TEXT:\nMAX = 10\nMIN = 1",
    ),
    "md_path_heading": (
        "MD_PATH:\n# Guide\n## Install\n---\nHow to install the demo.\n",
        "How to install the demo.",
        "How to install the demo.",
    ),
    "md_heading_with_md_ast_beacon": (
        "MD_PATH:\n"
        "# Guide\n"
        "---\n"
        "BEACON (MD AST)\n"
        "  id: guide-h@0,\n"
        "  slice_labels: demo,\n"
        "  kind: ast,\n"
        "  comment: The guide header,\n"
        "---\n"
        "Guide prose.\n",
        "comment: The guide header,\nGuide prose.",
        "Guide prose.",
    ),
    "js_beacon_block": (
        "AST_QUALNAME: app.main\n"
        "---\n"
        "@beacon[\n"
        "  id=main@5,\n"
        "  slice_labels=demo,demo-cli,\n"
        "  kind=ast,\n"
        "  comment=CLI entry point,\n"
        "]\n"
        "---\n"
        "Parses argv.\n",
        "  id=main@5,\n"
        "  slice_labels=demo,demo-cli,\n"
        "  kind=ast,\n"
        "  comment=CLI entry point,\n"
        "]\n---\nParses argv.",
        "  id=main@5,\n"
        "  slice_labels=demo,demo-cli,\n"
        "  kind=ast,\n"
        "  comment=CLI entry point,\n"
        "]\n---\nParses argv.",
    ),
    "virtual_group_note": (
        "CARTOGRAPHER_VIRTUAL_GROUP: pkg\n"
        "CARTO_GROUP_ID: g-1\n"
        "CARTO_GROUP_LABEL: Group one\n"
        "CARTO_GROUP_MARKER_LINE: 12\n"
        "---\n"
        "Synthetic Cartographer grouping node.\n",
        "CARTOGRAPHER_VIRTUAL_GROUP: pkg\n"
        "CARTO_GROUP_ID: g-1\n"
        "CARTO_GROUP_LABEL: Group one\n"
        "CARTO_GROUP_MARKER_LINE: 12\n"
        "---\n"
        "Synthetic Cartographer grouping node.",
        "CARTOGRAPHER_VIRTUAL_GROUP: pkg\n"
        "CARTO_GROUP_ID: g-1\n"
        "CARTO_GROUP_LABEL: Group one\n"
        "CARTO_GROUP_MARKER_LINE: 12\n"
        "---\n"
        "Synthetic Cartographer grouping node.",
    ),
    "no_separator_beacon": (
        "AST_QUALNAME: pkg.mod.tight\n"
        "---\n"
        "BEACON (\n"
        "  id: tight@0,\n"
        "  slice_labels: demo,\n"
        "  kind: ast,\n"
        "  comment: Tight comment\n"
        "Body right after keys, no separator.\n",
        "comment: Tight comment\nBody right after keys, no separator.",
        "Body right after keys, no separator.",
    ),
    "file_header_note": (
        "Path: E:\\__daniel347x\\__Dan_Root\\__repos\\demo\\pkg\\core.py\n"
        "LINE COUNT: 42\n"
        "---\n"
        "Prose under the file header.\n",
        "Path: E:\\__daniel347x\\__Dan_Root\\__repos\\demo\\pkg\\core.py\n"
        "LINE COUNT: 42\n"
        "---\n"
        "Prose under the file header.",
        "Path: E:\\__daniel347x\\__Dan_Root\\__repos\\demo\\pkg\\core.py\n"
        "LINE COUNT: 42\n"
        "---\n"
        "Prose under the file header.",
    ),
    "beacon_comments_alias": (
        "AST_QUALNAME: pkg.mod.alias\n"
        "---\n"
        "BEACON (\n"
        "  id: alias@0,\n"
        "  slice_labels: demo,\n"
        "  kind: ast,\n"
        "  comments: Alias-key comment,\n"
        ")\n"
        "---\n"
        "Doc.\n",
        "comment: Alias-key comment,\n)\n---\nDoc.",
        ")\n---\nDoc.",
    ),
    "beacon_then_blank_then_body": (
        "AST_QUALNAME: pkg.mod.sep\n"
        "---\n"
        "BEACON (\n"
        "  id: sep@0,\n"
        "  slice_labels: demo,\n"
        "  kind: ast,\n"
        "  comment: Sep comment,\n"
        ")\n"
        "---\n"
        "\n"
        "Body after blank.\n",
        "comment: Sep comment,\n)\n---\n\nBody after blank.",
        ")\n---\n\nBody after blank.",
    ),
    "leading_blank_body": (
        "\n\nAST_QUALNAME: pkg.mod.lead\n---\nLead body.\n\n",
        "Lead body.",
        "Lead body.",
    ),
    "plain_prose": (
        "Plain note with no metadata at all.\nSecond line.\n",
        "Plain note with no metadata at all.\nSecond line.",
        "Plain note with no metadata at all.\nSecond line.",
    ),
    "root_header_note": (
        "Root: E:\\__daniel347x\\__Dan_Root\\__repos\\demo\n"
        "LINE COUNT: 1234\n"
        "---\n"
        "Root prose.\n",
        "Root: E:\\__daniel347x\\__Dan_Root\\__repos\\demo\n"
        "LINE COUNT: 1234\n"
        "---\n"
        "Root prose.",
        "Root: E:\\__daniel347x\\__Dan_Root\\__repos\\demo\n"
        "LINE COUNT: 1234\n"
        "---\n"
        "Root prose.",
    ),
}


@pytest.mark.parametrize("name", sorted(_NOTES))
def test_stripper_byte_identical_after_the_parse_split(name: str) -> None:
    note, expected_keep, expected_drop = _NOTES[name]
    assert STRIP(note) == expected_keep
    assert STRIP(note, preserve_beacon_comment=False) == expected_drop


def test_empty_and_non_string_notes() -> None:
    assert STRIP("") == ""
    assert STRIP("", preserve_beacon_comment=False) == ""
    assert parse_cartographer_note_for_preview("") == []
    assert parse_cartographer_note_for_preview(None) == []  # type: ignore[arg-type]


def test_parse_stream_shapes() -> None:
    # The ONE parse returns the ordered interleaved stream: body lines,
    # beacon records (labels/comment readable), md_path records.
    items = parse_cartographer_note_for_preview(_NOTES["ast_with_beacon_and_docstring"][0])
    beacons = [p for k, p in items if k == "beacon"]
    assert len(beacons) == 1
    # The record carries the block's RAW stripped values (no comma-munging);
    # the view's label split applies _slice_label_tags on top.
    assert beacons[0]["slice_labels"] == "demo,"
    assert beacons[0]["comment"] == "Read one entry,"
    assert beacons[0]["kind"] == "ast,"
    assert beacons[0]["id"] == "demo-get@0,"
    # The residue lines the pre-move loop kept stay in the body, in order.
    body = [p for k, p in items if k == "line"]
    assert body == [")", "---", "Return the value for key."]

    md_items = parse_cartographer_note_for_preview(_NOTES["md_path_heading"][0])
    md_blocks = [p for k, p in md_items if k == "md_path"]
    assert md_blocks == [["# Guide", "## Install"]]

    # comments: alias lands under the canonical comment key; last-seen wins.
    alias = parse_cartographer_note_for_preview(_NOTES["beacon_comments_alias"][0])
    assert [p for k, p in alias if k == "beacon"][0]["comment"] == "Alias-key comment,"
