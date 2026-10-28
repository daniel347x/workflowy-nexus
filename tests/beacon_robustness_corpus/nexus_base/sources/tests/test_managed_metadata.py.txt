"""managed_metadata -- the ONE managed-metadata inventory ([ap:BNNUZ4]).

Pure detector tests: one per managed kind, plus the three queries' guard
behaviour, determinism, and the recall bias documented in the module.
No fixtures beyond strings; no I/O.
"""
from __future__ import annotations

from workflowy_mcp import managed_metadata as mm

PROGRAM_ROOT_NOTE = "Intro paragraph.\n\n```yaml\nprogram_interface:\n  name: p\n  sessions: []\n```"


# --- refuse-mapped kinds (first line only; exact kind per prefix) ----------

def test_refuse_first_line_kinds_match_line_one_with_exact_kind():
    for first, kind in (
        ("AST_QUALNAME: pkg.mod.fn", "ast_header"),
        ("MD_PATH: a/b.md", "md_path_header"),
        ("BEACON (id=auto-beacon@x,", "beacon_block"),
    ):
        assert mm.detect_managed_metadata(first + "\nbody") == kind
    assert mm.first_refuse_guard_hit("AST_QUALNAME: x") == "AST_QUALNAME:"
    assert mm.first_refuse_guard_hit("MD_PATH: a/b.md") == "MD_PATH:"
    assert mm.first_refuse_guard_hit("BEACON (x") == "BEACON ("
    assert mm.first_refuse_guard_hit("plain text") is None
    assert mm.first_refuse_guard_hit("") is None
    # a later line does not count (first-line zone)
    assert mm.detect_managed_metadata("intro\nMD_PATH: a/b.md") is None


def test_trail_skip_query_excludes_refuse_kinds():
    assert mm.detect_managed_metadata("AST_QUALNAME: x") == "ast_header"
    assert mm.detect_trail_skip_metadata("AST_QUALNAME: x") is None
    assert mm.detect_trail_skip_metadata("MD_PATH: x") is None
    assert mm.detect_trail_skip_metadata("BEACON (x") is None


# --- skip-trail kinds ------------------------------------------------------


def test_program_interface_in_a_fence_anywhere_in_the_note():
    assert mm.detect_managed_metadata(PROGRAM_ROOT_NOTE) == "program_interface"
    bare = "program_interface:\n  name: p\n  sessions: []\n"
    assert mm.detect_managed_metadata(bare) == "program_interface"


def test_program_interface_prose_mention_alone_is_not_managed():
    assert mm.detect_managed_metadata("the program_interface key lives in a fenced yaml block") is None
    assert mm.detect_managed_metadata("program_interface: not followed by an indented block") is None


def test_stage_configuration_fence_keys():
    for body in (
        "```yaml\nstanding_tabs:\n  - alias: a\n    mode: live\n```",
        "```yaml\nstanding_estates:\n  - CARTOGRAPHER DOCS\n```",
        "```yaml\nstanding_worker_clauses: []\n```",
        "```yaml\nstanding_attachments: []\n```",
        "```yaml\nseats:\n  - role: RECON\n```",
        "```yaml\ncast:\n  - role: implementer\n```",
        "```yaml\nrequired_conductor_worker_clauses:\n  - [ap:X]\n```",
    ):
        assert mm.detect_managed_metadata("prefix text\n\n" + body) == "stage_configuration", body
    assert mm.detect_managed_metadata("seats: and cast: are ordinary words in prose") is None


def test_conductor_state_fence_keys():
    for body in (
        "```yaml\nversion: 3\nstage: G2\npending_halt: null\nnext_step: merge\n```",
        "```yaml\nin_flight:\n  - seat one\n```",
        "```yaml\nstage: G1\nversion: 1\n```",
    ):
        assert mm.detect_managed_metadata(body) == "conductor_state", body
    # a lone generic key is not enough
    assert mm.detect_managed_metadata("```yaml\nstage: one word in prose\n```") is None


def test_dispatch_block_fenced_or_bare():
    assert mm.detect_managed_metadata("```yaml\ndispatch:\n  role: IMPLEMENTER\n```") == "dispatch_block"
    assert mm.detect_managed_metadata("dispatch:\n  role: IMPLEMENTER\n") == "dispatch_block"
    assert mm.detect_managed_metadata("the dispatch: key mentioned mid-line") is None


def test_estate_payload_json():
    fenced = '```json\n[{"group": "Recruiting"}, {"dock": "[ap:X]"}]\n```'
    assert mm.detect_managed_metadata(fenced) == "estate_payload"
    bare_json = '[{"spec": {"kind": "raw_root", "root_uuid": "3f3754e2"}}]'
    assert mm.detect_managed_metadata(bare_json) == "estate_payload"
    # The old private key list mistook this promote-item JSON for an estate.
    # WO-2 and the conductor's continuation require the grammar's verdict.
    assert mm.detect_managed_metadata('[{"handle": "[ap:K7M2QF]", "destination": "[ap:T]"}]') is None
    # a bare JSON string (the one-group shorthand) is deliberately not detected
    assert mm.detect_managed_metadata('"Recruiting"') is None
    # unrelated JSON does not count
    assert mm.detect_managed_metadata('{"answer": 42}') is None


def test_seat_manifest_lines():
    assert mm.detect_managed_metadata("map [ap:P1YHVF]\nskyview [ap:J3DRZ0]") == "seat_manifest"
    assert mm.detect_managed_metadata("skyview [ap:J3DRZ0]") == "seat_manifest"
    assert mm.detect_managed_metadata("map this idea to that one") is None


def test_snapshot_node_names():
    assert mm.detect_managed_metadata("x", name="📸 Snapshot · program monitoring · 2026-09-27") == "snapshot_node"
    assert mm.detect_managed_metadata(name="📸 Shelf Snapshot · 2026-09-27 · 3 shelves") == "snapshot_node"
    assert mm.detect_managed_metadata("x", name="ordinary name") is None
    assert mm.detect_managed_metadata("x") is None  # no name given


def test_ledger_name_tag_via_the_trailing_tag_region():
    assert mm.detect_managed_metadata("x", name="Ledger · MCP program #prog-ledger") == "ledger"
    assert mm.detect_managed_metadata("x", name="🔱 [ap:LEDG1] Ledger #prog-ledger #pct") == "ledger"
    assert mm.detect_managed_metadata("x", name="talking about #prog-ledger mid-title prose") is None
    assert mm.detect_managed_metadata("x", name="plain name") is None


# --- query behaviour --------------------------------------------------------


def test_first_match_follows_registry_order():
    # both program_interface (skip) and an estate payload present: the
    # registry's earlier skip kind wins, deterministically.
    note = PROGRAM_ROOT_NOTE + '\n\n```json\n[{"group": "Recruiting"}]\n```'
    assert mm.detect_managed_metadata(note) == "program_interface"
    assert mm.detect_trail_skip_metadata(note) == "program_interface"
    # name kinds come after note kinds
    assert mm.detect_managed_metadata("map [ap:X]", name="Ledger #prog-ledger") == "seat_manifest"


def test_empty_inputs_are_never_managed():
    assert mm.detect_managed_metadata("") is None
    assert mm.detect_trail_skip_metadata("") is None
    assert mm.detect_managed_metadata() is None
    assert mm.detect_managed_metadata("plain note about code") is None


def test_structural_kind_has_no_detector():
    kinds = {kind.kind: kind for kind in mm._KINDS}
    assert "cartographer_mapped_node" in kinds
    assert kinds["cartographer_mapped_node"].detect is None
    assert kinds["cartographer_mapped_node"].guard == "structural"


def test_invalid_estate_candidates_are_not_managed():
    for note in (
        'Payload {"title": "x"} inline',
        '```json\n[{"handle": "[ap:K7M2QF]", "destination": "[ap:T]"}]\n```',
        'Payload {"answer": 42} inline',
    ):
        assert mm.detect_trail_skip_metadata(note) is None, note
