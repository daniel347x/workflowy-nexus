"""Estate Simple Forms [ap:XFW9YD]: pure interpretation, no runtime state."""
import json

import pytest

from workflowy_mcp.simple_form import normalize_simple_form, resolve_bare_string_decision, decide_node_role
from workflowy_mcp.glimpse_estate_payload import parse_estate_note


@pytest.mark.parametrize("raw,form", [("Estate Test", "named_group"), ("[ap:K7M2QF]", "bare_handle"), ("c5e51af5", "bare_handle")])
def test_text(raw, form):
    assert normalize_simple_form(raw, surface="estate_note").form == form
    entries, err = parse_estate_note(raw)
    assert err is None and entries[0].valid


@pytest.mark.parametrize("raw", [
    "c5e51af51111",                                  # 12-char prefix (CF-06)
    "c5e51af5a",                                     # 9-char prefix
    "c5e51af5-1111-2222",                            # dashed partial
    "c5e51af5111122223333444444444444",              # 32-hex undashed full UUID
    "[c5e51af51111]",                                # bracketed hex prefix
])
def test_cf06_widened_handle_shapes(raw):
    assert normalize_simple_form(raw, surface="estate_note").form == "bare_handle"
    assert normalize_simple_form(raw, surface="estate_call").form == "bare_handle"


@pytest.mark.parametrize("raw", [
    "c5e51af5-",                                     # trailing dash
    "-c5e51af5",                                     # leading dash
    "c5e51af",                                       # 7 chars: below the kernel floor
    "c5e51af5xyz",                                   # non-hex tail
])
def test_cf06_non_handle_shapes_stay_named_group(raw):
    assert normalize_simple_form(raw, surface="estate_note").form == "named_group"


def test_cf06_quoted_and_json_lane_unchanged():
    # A JSON-quoted hex string decodes to the handle (pre-existing lane).
    assert normalize_simple_form('"c5e51af51111"', surface="estate_call").form == "bare_handle"
    # A quoted group name and a JSON array still parse as JSON, not handles.
    assert normalize_simple_form('"Estate Test"', surface="estate_call").form == "named_group"
    assert normalize_simple_form('["Estate Test"]', surface="estate_call").form == "formal_array"


@pytest.mark.parametrize("descend,policy", [(None, "stop_at_child_anchors"), (False, "stop_at_child_anchors"), (True, "all")])
def test_dock(descend, policy):
    raw = {"dock": "c5e51af5"}
    if descend is not None:
        raw["descend"] = descend
    form = normalize_simple_form(raw, surface="estate_note")
    assert form.spec["descend_policy"] == policy and form.alias == "c5e51af5"
    entries, err = parse_estate_note(__import__("json").dumps(raw))
    assert err is None and entries[0].spec == form.spec


def test_tags_symbols():
    tags = normalize_simple_form({"tags": ["estate-test"], "root": "c5e51af5"}, surface="estate_note")
    assert tags.spec["lightning"] == {"mode": "render_tags", "tags": ["estate-test"], "postfix_policy": "slice", "match_scope": "name"}
    assert tags.lens["match_scope"] == "name"
    raw = {"symbols": ["parse_estate_note"], "root": "7834ca3d"}
    symbols = normalize_simple_form(raw, surface="estate_note")
    assert symbols.spec["lightning"]["root_uuids"] == ["7834ca3d"]
    refused = normalize_simple_form(raw, surface="slice_group")
    assert refused.error_code == "simple_form_wrong_surface" and "lightning_rod.render_symbols" in refused.error_message


@pytest.mark.parametrize("raw,code", [
    ({"group": "Estate Test", "descend": True}, "simple_form_descend_refused"),
    ({"dock": "c5e51af5", "root_uuid": "c5e51af5"}, "simple_form_unresolved"),
    ({"dock": "c5e51af5", "descend": "true"}, "simple_form_unresolved"),
    ({"tags": [], "root": "r"}, "simple_form_unresolved"),
    ({"symbols": ["f"]}, "simple_form_unresolved"),
    ({"dock": "r", "typo": True}, "simple_form_unresolved"),
])
def test_refusals(raw, code):
    result = normalize_simple_form(raw, surface="estate_note")
    assert not result.ok and result.error_code == code and "describe" in result.error_message


def test_decisions():
    result = resolve_bare_string_decision("c5e51af5", node_hits=["node1"], group_hits=["group1"])
    assert result.error_code == "simple_form_double_match"
    assert "node1" in result.error_message and "group1" in result.error_message
    assert decide_node_role(is_anchor=True, note_parses_as_estate=True, inside_note=False).role == "dock"
    assert decide_node_role(is_anchor=False, note_parses_as_estate=True, inside_note=True).error_code == "simple_form_nesting_refused"


def test_descend_alias_and_unchanged_defaults():
    from workflowy_mcp.cache_render_spec import RenderProjectionSpec as Spec
    from workflowy_mcp.lens_spec import parse_lens_spec
    assert Spec.normalize_from_glimpse_args(descend_policy="stop_at_child_docks").descend_policy == "stop_at_child_anchors"
    assert Spec.from_render_spec_blob({"descend_policy": "stop_at_child_docks"}).descend_policy == "stop_at_child_anchors"
    assert Spec.from_render_spec_blob({}).descend_policy == "all"
    assert Spec.normalize_from_glimpse_args().descend_policy == "all"
    assert parse_lens_spec({"kind": "subtree", "root_handle": "c5e51af5"}).descend == "all"
    assert parse_lens_spec({"kind": "subtree", "root_handle": "c5e51af5", "descend": "stop_at_child_anchors"}).descend == "stop_at_child_docks"
    assert normalize_simple_form({"dock": "c5e51af5"}, surface="slice_group").lens["descend"] == "stop_at_child_anchors"


# ── G2-3c · QBCTWU U3-1 + GPLENH + G1TOPY CF-10/14 ────────────────────────

def test_node_form_lowers_to_render_symbols():
    form = normalize_simple_form({"node": "[ap:DOCK01]"}, surface="estate_note")
    assert form.ok and form.form == "node" and form.root == "[ap:DOCK01]"
    assert form.requires_dock is False and form.lens is None
    assert form.spec == {"lightning": {"mode": "render_symbols",
                                       "symbols": ["[ap:DOCK01]"], "root_uuids": ["[ap:DOCK01]"]}}
    entries, err = parse_estate_note(json.dumps({"node": "[ap:DOCK01]"}))
    assert err is None and entries[0].valid and entries[0].spec == form.spec


def test_node_form_refused_on_slice_group_surface():
    refused = normalize_simple_form({"node": "c5e51af5"}, surface="slice_group")
    assert refused.error_code == "simple_form_wrong_surface"
    assert "render_symbols" in refused.error_message and "glimpse.estate" in refused.error_message


def test_subtree_form_lowers_to_raw_root_all():
    form = normalize_simple_form({"subtree": "c5e51af5"}, surface="estate_note")
    assert form.ok and form.form == "subtree" and form.root == "c5e51af5"
    assert form.requires_dock is False
    # U3-2 per-kind default read: a {subtree} parcel on an ESTATE surface
    # defaults to a headings-only render, baked into the lowered spec.
    assert form.spec == {"root_uuid": "c5e51af5", "descend_policy": "all", "headings_only": True}
    assert form.lens == {"kind": "subtree", "root_handle": "c5e51af5", "descend": "all"}
    entries, err = parse_estate_note(json.dumps({"subtree": "c5e51af5"}))
    assert err is None and entries[0].valid and entries[0].spec == form.spec
    # The bake is ESTATE-surface-only: glimpse_run/slice_group lowerings
    # carry no default view (Dan: "estate verb ONLY, not glimpse.run").
    run_form = normalize_simple_form({"subtree": "c5e51af5"}, surface="glimpse_run")
    assert run_form.spec == {"root_uuid": "c5e51af5", "descend_policy": "all"}
    lens_form = normalize_simple_form({"subtree": "c5e51af5"}, surface="slice_group")
    assert lens_form.lens["descend"] == "all"
    assert lens_form.spec == {"root_uuid": "c5e51af5", "descend_policy": "all"}


def test_estate_default_reads_baked_per_kind():
    # U3-2: {dock} and {tags} bake headings-only on the estate surfaces;
    # {node} and {symbols} do NOT (their bodies ARE the read); a NAMED slice
    # group renders as the group defines (Dan's ruling — no default baked).
    dock = normalize_simple_form({"dock": "c5e51af5"}, surface="estate_call")
    assert dock.spec["headings_only"] is True and dock.spec["descend_policy"] == "stop_at_child_anchors"
    tags = normalize_simple_form({"tags": ["x"], "root": "c5e51af5"}, surface="estate_note")
    assert tags.spec["headings_only"] is True
    node = normalize_simple_form({"node": "c5e51af5"}, surface="estate_note")
    assert "headings_only" not in node.spec
    symbols = normalize_simple_form({"symbols": ["f"], "root": "c5e51af5"}, surface="estate_note")
    assert "headings_only" not in symbols.spec
    # The call-level override still wins: the tri-state view override rides
    # with_view_overrides at render time (covered end-to-end in
    # test_estate_simple_forms); here: the lowered spec is the default only.
    dock_run = normalize_simple_form({"dock": "c5e51af5"}, surface="glimpse_run")
    assert "headings_only" not in dock_run.spec


def test_subtree_is_a_lens_atom_on_slice_group_surface():
    lens = normalize_simple_form({"subtree": "c5e51af5"}, surface="slice_group")
    assert lens.ok and lens.lens["kind"] == "subtree" and lens.lens["descend"] == "all"


@pytest.mark.parametrize("long_form,short_form", [
    ({"ref": "Estate Test", "kind": "slice_group"}, {"group": "Estate Test"}),
    ({"ref": "c5e51af5", "kind": "dock"}, {"dock": "c5e51af5"}),
    ({"ref": "c5e51af5", "kind": "dock", "descend": True}, {"dock": "c5e51af5", "descend": True}),
    ({"ref": "c5e51af5", "kind": "node"}, {"node": "c5e51af5"}),
    ({"ref": "c5e51af5", "kind": "node", "how": "note"}, {"node": "c5e51af5"}),
    ({"ref": "c5e51af5", "kind": "node", "how": "subtree"}, {"subtree": "c5e51af5"}),
])
def test_long_form_is_alias_of_short_form(long_form, short_form):
    a = normalize_simple_form(long_form, surface="estate_note")
    b = normalize_simple_form(short_form, surface="estate_note")
    assert a.ok and b.ok and a.form == b.form
    assert a.spec == b.spec and a.lens == b.lens and a.root == b.root and a.alias == b.alias


def test_long_form_file_uri_and_shape_errors():
    for kind in ("file", "uri"):
        refused = normalize_simple_form({"ref": "/tmp/x", "kind": kind}, surface="estate_note")
        assert refused.error_code == "simple_form_not_a_parcel"
        assert "file tools" in refused.error_message or "fetch a URI" in refused.error_message
    unknown = normalize_simple_form({"ref": "x", "kind": "bogus"}, surface="estate_note")
    assert not unknown.ok and "unknown kind" in unknown.error_message
    no_ref = normalize_simple_form({"ref": "  ", "kind": "node"}, surface="estate_note")
    assert not no_ref.ok and "'ref'" in no_ref.error_message
    bad_how = normalize_simple_form({"ref": "x", "kind": "dock", "how": "note"}, surface="estate_note")
    assert not bad_how.ok and "'how'" in bad_how.error_message
    bad_how_value = normalize_simple_form({"ref": "x", "kind": "node", "how": "tree"}, surface="estate_note")
    assert not bad_how_value.ok and "unknown how" in bad_how_value.error_message
    bad_descend = normalize_simple_form({"ref": "x", "kind": "node", "descend": True}, surface="estate_note")
    assert bad_descend.error_code == "simple_form_descend_refused"
    extra_key = normalize_simple_form({"ref": "x", "kind": "dock", "bogus": 1}, surface="estate_note")
    assert not extra_key.ok and "unknown keys" in extra_key.error_message


def test_lens_sugar_lowers_exactly():
    lens = {"kind": "group", "group_handle": "Recruiting"}
    form = normalize_simple_form({"lens": lens}, surface="estate_call")
    assert form.ok and form.form == "formal_spec"
    assert form.spec == {"lightning": {"mode": "render_lens", "lens": lens}}
    aliased = normalize_simple_form({"alias": "M", "lens": lens}, surface="estate_note")
    assert aliased.ok and aliased.alias == "M"
    assert aliased.spec == {"lightning": {"mode": "render_lens", "lens": lens}}
    entries, err = parse_estate_note(json.dumps({"lens": lens}))
    assert err is None and entries[0].valid
    assert entries[0].spec == {"lightning": {"mode": "render_lens", "lens": lens}}
    extra = normalize_simple_form({"lens": lens, "bogus": 1}, surface="estate_note")
    assert not extra.ok and "unknown keys" in extra.error_message
    not_dict = normalize_simple_form({"lens": "Recruiting"}, surface="estate_note")
    assert not not_dict.ok and "LensSpec" in not_dict.error_message
    # The sugar is estate-only: the other surfaces keep their own spellings.
    other = normalize_simple_form({"lens": lens}, surface="slice_group")
    assert not other.ok


@pytest.mark.parametrize("parcel", [
    {"dock": "c5e51af5", "view": "headings_only"},
    {"group": "Estate Test", "read": "required"},
    {"node": "c5e51af5", "note": "x"},
    {"subtree": "c5e51af5", "entries": [1]},
    {"tags": ["t"], "root": "r", "load": "before"},
    {"ref": "c5e51af5", "kind": "dock", "view": "headings_only"},
])
def test_settings_inside_parcel_refused(parcel):
    refused = normalize_simple_form(parcel, surface="estate_note")
    assert refused.error_code == "simple_form_settings_in_parcel"
    assert "BESIDE" in refused.error_message


@pytest.mark.parametrize("text", [
    "/mnt/e/repo/file.py",
    "~/notes.md",
    "E:\\__repos\\x.py",
    "https://example.com/page",
])
def test_path_or_uri_not_a_parcel_on_estate_surfaces(text):
    for surface in ("estate_call", "estate_note"):
        refused = normalize_simple_form(text, surface=surface)
        assert refused.error_code == "simple_form_not_a_parcel"
        assert "file path or URI" in refused.error_message
    # Other surfaces are untouched: a group may legitimately look like this.
    assert normalize_simple_form(text, surface="slice_group").form == "named_group"


def test_cf14_quote_in_unquoted_group_name():
    # CF-14: the one-line note gate excludes only the JSON structure openers
    # ({ and [); a double quote is legal inside an unquoted group name.
    entries, err = parse_estate_note('Scratch | Estate "Test"')
    assert err is None and entries and entries[0].valid
    assert entries[0].group == 'Scratch | Estate "Test"'


def test_cf10_one_blob_recogniser():
    from workflowy_mcp import glimpse_estate_payload as payload
    from workflowy_mcp import simple_form as sf
    assert payload._looks_like_spec_blob is sf._looks_like_spec_blob
    assert payload._SPEC_KINDS is sf._SPEC_KINDS
