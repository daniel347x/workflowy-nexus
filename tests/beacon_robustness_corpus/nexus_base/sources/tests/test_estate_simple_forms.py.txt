"""Offline SCRATCH-shaped fixtures; real render funnel, no live daemon calls."""
import asyncio
import json
import types

import pytest
from workflowy_mcp import server as srv
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
from workflowy_mcp.mcp_api_registry import dispatch_operation

DOCK = "c5e51af5-1111-2222-3333-444444444444"
INNER = "b5cb2d0b-1111-2222-3333-444444444444"
ESTATE = "97a26cce-1111-2222-3333-444444444444"
NODES = [
    {"id": DOCK, "name": "[ap:DOCK01] Estate Test Dock #wfy-ns", "note": '"description is a group"', "parent_id": None},
    {"id": "aaaaaaaa-1111-2222-3333-444444444444", "name": "Outer A #estate-test", "note": "outer body", "parent_id": DOCK},
    {"id": INNER, "name": "Inner Dock #wfy-ns--local", "note": "inner dock", "parent_id": DOCK},
    {"id": "cccccccc-1111-2222-3333-444444444444", "name": "Inner C #estate-test", "note": "inner body", "parent_id": INNER},
    {"id": "dddddddd-1111-2222-3333-444444444444", "name": "Inner D", "note": "inner D body", "parent_id": INNER},
    {"id": ESTATE, "name": "[ap:EST001] T2 estate", "note": "", "parent_id": None},
]
GROUP = {"id": "grp-fc6ec7c3111122223333444444444444", "name": "Scratch | Estate Test", "root_uuid": DOCK, "prefixes": ["estate-test"]}


@pytest.fixture
def env(monkeypatch):
    state = {"note": json.dumps({"dock": DOCK}), "groups": [GROUP], "reads": [], "nodes": [dict(n) for n in NODES]}
    client = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    client._get_nodes_export_cache_nodes = lambda: state["nodes"]
    monkeypatch.setattr(srv, "get_client", lambda: client)
    monkeypatch.setattr(srv, "_anchor_cache_by_id", lambda: {n["id"]: n for n in state["nodes"]})

    async def cfg(target_tab):
        return state["groups"], None, {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"}, ["wfy-suppress"], {"note_session_tags": ["wfy-ns"], "local_anchor_postfixes": ["local"]}

    async def vocab(target_tab):
        return ["wfy-ns"], {"local_postfixes": ["local"]}

    async def note_cfg(target_tab):
        return ["wfy-ns"], ["wfy-suppress"], None, {"local_anchor_postfixes": ["local"]}

    async def note(handle, as_raw_node=True, **kw):
        state["reads"].append(handle)
        return types.SimpleNamespace(id=ESTATE, name="T2 estate", note=state["note"])

    async def snapshot(target_tab=None, exclude_completed=None):
        return srv._build_lens_stage0_snapshot_from_config(
            {"expand_groups": state["groups"], "note_session_tags": ["wfy-ns"], "local_anchor_postfixes": ["local"]},
            cache_by_id={n["id"]: n for n in state["nodes"]}, exclude_completed=exclude_completed,
        )

    monkeypatch.setattr(srv, "_resolve_slice_group_config", cfg)
    monkeypatch.setattr(srv, "_read_anchor_dock_vocabulary", vocab)
    monkeypatch.setattr(srv, "_resolve_note_session_config", note_cfg)
    monkeypatch.setattr(srv, "_node_read_impl", note)
    monkeypatch.setattr(srv, "_build_lens_stage0_snapshot", snapshot)
    return state


def estate(handle="[ap:EST001]", **kw):
    return asyncio.run(srv._glimpse_estate_impl(handle=handle, **kw))


def test_plain_name_note(env):
    env["note"] = "Estate Test"
    result = estate()
    assert isinstance(result, str) and "Outer A" in result and "Inner C" in result


@pytest.mark.parametrize("descend", [False, True])
def test_dock_note(env, descend):
    env["note"] = json.dumps({"dock": "c5e51af5", "descend": descend})
    result = estate()
    # U3-2 default read: a {dock} parcel renders headings-only (the skill's
    # Dock row); descend still decides WHAT is walked — the child Dock's
    # descendants appear as headings only when descend=true, and note bodies
    # never ride the default read.
    assert isinstance(result, str) and "Outer A" in result and "Inner Dock" in result
    assert ("Inner C" in result and "Inner D" in result) == descend
    assert "inner body" not in result and "inner D body" not in result


@pytest.mark.parametrize("handle", ["c5e51af5", "[ap:DOCK01]", DOCK])
def test_bare_dock_first(env, handle):
    result = estate(handle)
    assert isinstance(result, str) and "Outer A" in result and "Inner C" not in result
    assert not env["reads"]  # Dock description never parsed/read as an estate.


@pytest.mark.parametrize("handle", ["c5e51af5", "[ap:DOCK01]"])
def test_bare_dock_note(env, handle):
    env["note"] = handle
    result = estate()
    assert isinstance(result, str) and "Outer A" in result and "Inner C" not in result
    assert len(env["reads"]) == 1


def test_nesting_refused(env):
    env["note"] = '"97a26cce"'
    result = estate()
    assert result["success"] is False and "simple_form_nesting_refused" in str(result)
    assert result["failed_entries"][0]["error_extra"]["code"] == "simple_form_nesting_refused"
    assert len(env["reads"]) == 1


def test_double_match(env):
    env["groups"] = [{**GROUP, "name": "c5e51af5"}]
    result = estate("c5e51af5")
    assert result["code"] == "simple_form_double_match"
    assert DOCK in result["error"] and GROUP["id"] in result["error"]
    assert not env["reads"]


@pytest.mark.parametrize("handle", [
    "c5e51af51111",                       # 12-char prefix (CF-06)
    "c5e51af5-1111-2222",                 # dashed partial
    "c5e51af5111122223333444444444444",   # 32-hex undashed full UUID
    "[c5e51af51111]",                     # bracketed hex prefix
])
def test_cf06_prefix_handles_render_the_dock(env, handle):
    # CF-06: these used to classify named_group and fail as an unknown group.
    result = estate(handle)
    assert isinstance(result, str) and "Outer A" in result and "Inner C" not in result
    assert not env["reads"]


def test_cf06_prefix_handle_note(env):
    # The parse_estate_note one-line gate inherits the widened _HANDLE.
    env["note"] = "c5e51af51111"
    result = estate()
    assert isinstance(result, str) and "Outer A" in result and "Inner C" not in result
    assert len(env["reads"]) == 1


def test_cf06_hexlike_group_name_double_match(env):
    # A group literally NAMED like a 12-char prefix collides with the node
    # the same prefix resolves to: the double-match rule arbitrates, loud.
    env["groups"] = [{**GROUP, "name": "c5e51af51111"}]
    result = estate("c5e51af51111")
    assert result["code"] == "simple_form_double_match"
    assert DOCK in result["error"] and GROUP["id"] in result["error"]
    assert not env["reads"]


def test_cf06_hexlike_group_name_wins_without_node(env):
    # A hex-looking name with NO node collision stays a group (no guessing).
    env["groups"] = [{**GROUP, "name": "123456789abc"}]
    result = estate("123456789abc")
    assert isinstance(result, str) and "Outer A" in result


def test_bad_entry_isolated(env):
    env["note"] = json.dumps(["97a26cce", {"dock": "c5e51af5"}])
    result = estate()
    assert isinstance(result, str) and "simple_form_nesting_refused" in result and "Outer A" in result


def test_declared_code_and_legacy_boundary():
    registry = srv.build_glimpse_registry()
    for payload, code in [({"success": False, "error": "old"}, "glimpse_request_failed"),
                          ({"success": False, "code": "undeclared", "error": "old"}, "glimpse_request_failed"),
                          ({"success": False, "code": "simple_form_double_match", "error": "collision"}, "simple_form_double_match")]:
        out = srv._render_family_result_or_error(registry, "run", payload)
        assert out["error"]["code"] == code and out["error"]["extra"]["legacy_result"] == payload


@pytest.mark.parametrize("form", ["Estate Test", '"Estate Test"', {"group": "Estate Test"}, {"dock": "c5e51af5"}, '{"dock":"c5e51af5"}'])
def test_direct_estate_forms(env, form):
    result = asyncio.run(dispatch_operation(srv.build_glimpse_registry(), "estate", {"handle": form}))
    assert isinstance(result, str) and "Outer A" in result
    assert not env["reads"]


@pytest.mark.parametrize("form", ["Estate Test", {"group": "Estate Test"}, "[ap:DOCK01]", {"dock": "c5e51af5"}])
def test_run_simple_form(env, form):
    result = asyncio.run(srv._glimpse_run_impl(render_spec=form))
    assert isinstance(result, str) and "Outer A" in result
    if form != "Estate Test" and form != {"group": "Estate Test"}:
        assert "inner body" not in result


def test_modal_is_first(env, monkeypatch):
    async def modal(**kw):
        return {"success": True, "preview_markdown": "MODAL"}
    monkeypatch.setattr(srv, "_glimpse_read_modal", modal)
    assert asyncio.run(srv._glimpse_run_impl(render_spec="modal")) == "MODAL"


@pytest.mark.parametrize("handle", ["97a26cce", '"97a26cce"', "[ap:EST001]"])
def test_run_estate_refused(env, handle):
    result = asyncio.run(dispatch_operation(srv.build_glimpse_registry(), "run", {"render_spec": handle}))
    assert result["error"]["code"] == "simple_form_wrong_surface" and "estate" in result["error"]["message"]


@pytest.mark.parametrize("verb", ["render", "sift", "merge.render"])
@pytest.mark.parametrize("form", ["Scratch | Estate Test", {"dock": "c5e51af5"}, {"tags": ["estate-test"], "root": "c5e51af5"}, "[ap:DOCK01]"])
def test_lens_simple_forms(env, verb, form):
    params = {"lens": form}
    if verb == "sift":
        # [ap:47B7WO]: the tree render is the explicit headings tier now
        # (the default tier is the line report).
        params.update(query="Outer", tier="headings")
    # U01: render/sift/merge.render return the raw Markdown string by default.
    out = asyncio.run(dispatch_operation(srv.build_slice_group_registry(), verb, params))
    assert isinstance(out, str), out
    assert "Outer A" in out
    if form in ({"dock": "c5e51af5"}, "[ap:DOCK01]"):
        assert "inner body" not in out


@pytest.mark.parametrize("verb", ["render", "sift", "merge.render"])
@pytest.mark.parametrize("form", [{"symbols": ["parse_estate_note"], "root": "c5e51af5"}, "97a26cce"])
def test_lens_wrong_surface(env, verb, form):
    params = {"lens": form, **({"query": "Outer"} if verb == "sift" else {})}
    out = asyncio.run(dispatch_operation(srv.build_slice_group_registry(), verb, params))
    assert out["error"]["code"] == "simple_form_wrong_surface"


def test_mutating_merge_does_not_gain_simple_forms(env):
    out = asyncio.run(dispatch_operation(srv.build_slice_group_registry(), "merge.persist", {"lens": "Estate Test", "tag": "test"}))
    assert out["success"] is False and out["error"]["code"] == "invalid_lens_shape"


@pytest.mark.parametrize("surface", ["note", "estate", "run"])
def test_tags_reach_existing_funnel(env, monkeypatch, surface):
    form = {"tags": ["estate-test"], "root": "[ap:DOCK01]"}
    calls = []
    async def tags(**kw):
        calls.append(kw)
        return {"success": True, "preview_markdown": "TAGS"}
    monkeypatch.setattr(srv, "_lightning_rod_from_cache", tags)
    if surface == "note":
        env["note"] = json.dumps(form)
        result = estate()
    elif surface == "estate":
        result = estate(form)
    else:
        result = asyncio.run(srv._glimpse_run_impl(render_spec=form))
    assert "TAGS" in result and len(calls) == 1
    assert calls[0]["root_uuid"] == DOCK and calls[0]["postfix_policy"] == "slice" and calls[0]["match_scope"] == "name"


@pytest.mark.parametrize("surface", ["note", "estate", "run"])
def test_symbols_reach_existing_funnel(env, monkeypatch, surface):
    form = {"symbols": ["parse_estate_note"], "root": "[ap:DOCK01]"}
    calls = []
    async def symbols(**kw):
        calls.append(kw)
        return {"success": True, "preview_markdown": "SYMBOLS"}
    monkeypatch.setattr(srv, "_lightning_rod_render_symbols", symbols)
    if surface == "note":
        env["note"] = json.dumps(form)
        result = estate()
    elif surface == "estate":
        result = estate(form)
    else:
        result = asyncio.run(srv._glimpse_run_impl(render_spec=form))
    assert "SYMBOLS" in result and len(calls) == 1
    assert calls[0]["root_uuids"] == [DOCK] and calls[0]["symbols"] == ["parse_estate_note"]


def test_error_codes_survive_note_and_top_level(env):
    form = {"group": "Estate Test", "descend": True}
    assert estate(form)["code"] == "simple_form_descend_refused"
    env["note"] = json.dumps(form)
    assert "simple_form_descend_refused" in str(estate())
    assert not estate({"dock": ESTATE})["success"]
    assert "simple_form_unresolved" in str(estate({"dock": ESTATE}))


def test_all_collision_shapes_and_non_docks(env):
    env["groups"] = [{**GROUP, "name": "[ap:DOCK01]"}]
    assert estate("[ap:DOCK01]")["code"] == "simple_form_double_match"
    env["groups"] = [{**GROUP, "id": "grp-fc6ec7c3aaaa22223333444444444444"}, GROUP]
    assert estate("fc6ec7c3")["code"] == "simple_form_double_match"
    env["nodes"].append({**NODES[0], "id": "c5e51af5-9999-2222-3333-444444444444"})
    assert estate("c5e51af5")["code"] == "simple_form_double_match"


def test_dock_form_never_uses_raw_dump_guard(env, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("simple form routed to in-band raw dump guard")
    monkeypatch.setattr(srv, "_anchor_dock_root_guard", forbidden)
    assert "Outer A" in asyncio.run(srv._glimpse_run_impl(render_spec={"dock": DOCK}))


def test_direct_array_and_view_override(env):
    result = estate(json.dumps([{"dock": DOCK}, {"dock": DOCK, "descend": True}]), headings_only=True)
    assert "2 rendered" in result and "Inner C" in result and "outer body" not in result


def test_describe_is_the_documentation():
    glimpse = srv.build_glimpse_registry()
    lenses = srv.build_slice_group_registry()
    estate_doc = json.dumps(glimpse.describe_full("estate")).lower()
    run_doc = json.dumps(glimpse.describe_full("run")).lower()
    lens_doc = json.dumps(lenses.describe_full("render")).lower()
    assert "estates do not nest" in estate_doc and "never overridden by the call" in estate_doc
    assert "a render specification is exactly one entry of an estate" in run_doc
    assert "a lens is more general than a slice group" in lens_doc
    for doc in (estate_doc, run_doc, lens_doc):
        assert "slice groups (and also lenses)" in doc and "decoupl" not in doc
    for field in ("kind", "root_uuid", "group_id", "group_name", "lightning", "focus", "match_scope", "descend_policy", "sort_by", "sort_direction", "since", "headings_only", "ancestor_notes", "as_markdown", "content_format", "tree_mode"):
        assert field in estate_doc
    assert "stop_at_child_anchors" in estate_doc
    assert tuple(glimpse.resolve("estate").params_schema["properties"]["handle"]["type"]) == ("string", "object")


@pytest.mark.parametrize("spelling", ["stop_at_child_docks", "stop_at_child_anchors"])
def test_descend_alias_in_real_blob_and_lens(env, spelling):
    result = asyncio.run(srv._glimpse_run_impl(render_spec={"root_uuid": DOCK, "descend_policy": spelling}))
    assert isinstance(result, str) and "Outer A" in result and "inner body" not in result
    # U01: the lens leg returns the raw Markdown string by default.
    result = asyncio.run(dispatch_operation(srv.build_slice_group_registry(), "render", {"lens": {"kind": "subtree", "root_handle": DOCK, "descend": spelling}}))
    assert isinstance(result, str) and "Outer A" in result and "inner body" not in result


def test_public_render_parameter_alias_reaches_normalizer(env, monkeypatch):
    calls = []
    async def run(**kw):
        calls.append(kw)
        return "rendered"
    monkeypatch.setattr(srv, "_glimpse_run_impl", run)
    monkeypatch.setattr(srv, "_lightning_rod_legacy_impl", run)
    assert asyncio.run(dispatch_operation(srv.build_glimpse_registry(), "run", {"root_handle": DOCK, "descend_policy": "stop_at_child_docks"})) == "rendered"
    assert asyncio.run(dispatch_operation(srv.build_lightning_rod_registry(), "gather", {"root_handle": DOCK, "descend_policy": "stop_at_child_docks"})) == "rendered"
    assert [kw["descend_policy"] for kw in calls] == ["stop_at_child_anchors"] * 2


# ── G2-3c · QBCTWU U3-1 + GPLENH + G1TOPY CF-11/13/14 ─────────────────────

def test_node_estate_resolves_symbols_and_scope(env, monkeypatch):
    # {node: H}: the resolved handle replaces BOTH the symbol and the scope
    # root (QBCTWU D3), then rides the existing render_symbols mechanism.
    calls = []
    async def symbols(**kw):
        calls.append(kw)
        return {"success": True, "preview_markdown": "NODE"}
    monkeypatch.setattr(srv, "_lightning_rod_render_symbols", symbols)
    result = estate({"node": "[ap:DOCK01]"})
    assert "NODE" in result and len(calls) == 1
    assert calls[0]["symbols"] == [DOCK] and calls[0]["root_uuids"] == [DOCK]


def test_node_estate_renders_node_alone(env):
    result = estate({"node": "c5e51af5"})
    assert isinstance(result, str) and "Estate Test Dock" in result
    # One node alone: no descendants ride along.
    assert "Outer A" not in result and "inner body" not in result


def test_subtree_estate_renders_all_descendants(env):
    # U3-2 default read: {subtree} renders every descendant, headings-only;
    # the call-level headings_only=False override brings the bodies back.
    result = estate({"subtree": "[ap:DOCK01]"})
    assert isinstance(result, str) and "Outer A" in result and "Inner D" in result
    assert "inner body" not in result and "outer body" not in result
    result = estate({"subtree": "[ap:DOCK01]"}, headings_only=False)
    assert isinstance(result, str) and "Outer A" in result and "inner D body" in result


def test_long_form_estate_dock_alias(env):
    # The long form is an alias of {dock}: same default headings-only read,
    # descend=true walks through the child Dock (headings, not bodies).
    result = estate({"ref": "[ap:DOCK01]", "kind": "dock", "descend": True})
    assert isinstance(result, str) and "Outer A" in result and "Inner D" in result
    assert "inner D body" not in result


def test_lens_sugar_estate(env):
    result = estate({"alias": "L", "lens": {"kind": "subtree", "root_handle": "[ap:DOCK01]", "descend": "all"}})
    assert isinstance(result, str) and "L" in result and "Outer A" in result and "inner D body" in result


def test_lens_sugar_in_note_array(env):
    env["note"] = json.dumps([{"lens": {"kind": "subtree", "root_handle": "[ap:DOCK01]", "descend": "all"}}])
    result = estate()
    assert isinstance(result, str) and "Outer A" in result


@pytest.mark.parametrize("verb", ["render", "sift", "merge.render"])
def test_subtree_is_a_lens_atom(env, verb):
    params = {"lens": {"subtree": "c5e51af5"}}
    if verb == "sift":
        # [ap:47B7WO]: the tree render is the explicit headings tier now.
        params.update(query="Outer", tier="headings")
    out = asyncio.run(dispatch_operation(srv.build_slice_group_registry(), verb, params))
    assert isinstance(out, str), out
    assert "Outer A" in out
    if verb != "sift":
        # sift's query filters the subtree to matching nodes; the render and
        # merge.render legs carry every descendant (descend: all).
        assert "inner D body" in out


def test_node_refused_on_lens_surfaces(env):
    out = asyncio.run(dispatch_operation(srv.build_slice_group_registry(), "render", {"lens": {"node": "c5e51af5"}}))
    assert out["error"]["code"] == "simple_form_wrong_surface"


def test_file_uri_refused_with_teaching_error(env):
    result = estate({"ref": "/tmp/x.md", "kind": "file"})
    assert result["success"] is False and result["code"] == "simple_form_not_a_parcel"
    assert "file tools" in result["error"]
    result = estate({"ref": "https://example.com/x", "kind": "uri"})
    assert result["success"] is False and result["code"] == "simple_form_not_a_parcel"


def test_path_handle_refused_with_teaching_error(env):
    result = estate("/mnt/e/repo/file.py")
    assert result["success"] is False and result["code"] == "simple_form_not_a_parcel"
    assert "file path or URI" in result["error"]


def test_settings_inside_parcel_refused(env):
    result = estate({"dock": DOCK, "view": "headings_only"})
    assert result["success"] is False and result["code"] == "simple_form_settings_in_parcel"
    assert "BESIDE" in result["error"]


def test_cf11_one_cache_source(env, monkeypatch):
    # CF-11: entry resolution reads the ONE pinned source the run/lens paths
    # use (_anchor_cache_by_id) — a stale/empty get_client cache list must
    # not matter. Fails against the pre-fix code (the estate read
    # get_client's list, so resolution failed "absent from the cache").
    client = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    client._get_nodes_export_cache_nodes = lambda: []
    monkeypatch.setattr(srv, "get_client", lambda: client)

    async def symbols(**kw):
        return {"success": True, "preview_markdown": "NODE"}
    monkeypatch.setattr(srv, "_lightning_rod_render_symbols", symbols)
    result = estate({"node": "[ap:DOCK01]"})
    assert "NODE" in result


def test_plain_node_fallback_non_dock_non_estate(env):
    # U3-2 (supersedes the CF-13 neither-branch pin): a bare handle whose
    # note is prose (multi-line, unresolvable as group names) is neither a
    # Dock nor an estate — it reads the NODE ON ITS OWN, after ONE read,
    # through the same {node} lowering/render machinery as the direct form.
    env["note"] = "prose line one\nprose line two"
    result = estate("97a26cce")
    assert isinstance(result, str) and "T2 estate" in result
    assert "Outer A" not in result  # the node alone: no descendants
    assert len(env["reads"]) == 1


def test_named_estate_one_parcel_per_line(env):
    # U3-2 headline: a multi-line note listing parcels ONE PER LINE parses
    # as a named estate; every parcel renders, one document each, in order.
    env["note"] = 'Estate Test\n{"dock": "c5e51af5"}\n[ap:DOCK01]'
    result = estate()
    assert isinstance(result, str)
    assert "3 entries" in result and "3 rendered" in result
    assert result.index("# 📄 [1] Estate Test") < result.index("# 📄 [2] c5e51af5") < result.index("# 📄 [3] [ap:DOCK01]")
    assert "Outer A" in result  # the named group rendered


def test_named_estate_fenced_one_parcel_per_line(env):
    # The same, fenced in a code block (the skill: "bare, or fenced").
    env["note"] = (
        "My estate:\n\n```text\nEstate Test\n{\"dock\": \"c5e51af5\"}\n```\n"
    )
    result = estate()
    assert isinstance(result, str) and "2 entries" in result and "Outer A" in result


def test_named_estate_bare_names_must_resolve(env):
    # U3-2 classification: a note whose bare-name lines do NOT resolve to
    # existing slice groups is NOT an estate — the plain-node fallback
    # renders the node on its own instead of a loud unknown-group estate.
    env["groups"] = []  # nothing resolves
    env["note"] = "no such group at all\nnor this one"
    result = estate()
    assert isinstance(result, str) and "T2 estate" in result
    assert "ENTRY FAILED" not in result


def test_named_estate_explicit_parcels_stay_estates_when_unresolvable(env):
    # Explicit parcels ({group}/{dock} JSON) are deliberate: an unresolvable
    # explicit group keeps the loud PER-ENTRY estate error (the classification
    # only demotes prose-ambiguous BARE-NAME lines, and a legacy JSON payload
    # is never demoted at all).
    env["note"] = '{"group": "missing-group"}\n{"dock": "c5e51af5"}'
    result = estate()
    assert isinstance(result, str) and "ENTRY FAILED" in result and "Outer A" in result


def test_named_estate_one_bad_line_falls_back_to_payload_scan(env):
    # One NON-PARCEL line (here: a bare JSON scalar) → NOT a per-line estate;
    # the tolerant candidate scan still finds the embedded parcel JSON, so a
    # note that is not a clean per-line listing keeps working. (A PROSE line
    # is a valid bare-name parcel — the resolution classification, not the
    # parser, demotes those.)
    env["note"] = "Recruiting\n42\n" + json.dumps({"dock": "c5e51af5"})
    result = estate()
    assert isinstance(result, str) and "Outer A" in result and "1 rendered" in result


def test_legacy_saved_tabs_export_stays_estate(env):
    # A fenced Saved-Tabs export (whole-note JSON) is the legacy path: an
    # unresolvable BARE-STRING element inside it fails as a loud per-entry
    # error — the classification never demotes a legacy payload.
    env["groups"] = []
    env["note"] = "```json\n[\"missing-group\", {\"dock\": \"c5e51af5\"}]\n```"
    result = estate()
    assert isinstance(result, str) and "ENTRY FAILED" in result and "Outer A" in result


def test_group_entry_renders_as_the_group_defines(env, monkeypatch):
    # Dan's ruling: a NAMED slice group with no override renders as the
    # group defines — no headings-only default is baked onto its spec.
    specs = []

    async def dispatch(spec, **kw):
        specs.append(spec)
        return {"success": True, "preview_markdown": "GROUP"}

    monkeypatch.setattr(srv, "_glimpse_dispatch_spec", dispatch)
    result = estate("Estate Test")
    assert "GROUP" in result and len(specs) == 1
    assert specs[0].headings_only is False


def test_call_level_headings_only_override_wins(env, monkeypatch):
    # The baked default is a DEFAULT: the call-level tri-state override
    # (headings_only=False) restores full bodies on a {subtree} parcel.
    specs = []

    async def dispatch(spec, **kw):
        specs.append(spec)
        return {"success": True, "preview_markdown": "SUB"}

    monkeypatch.setattr(srv, "_glimpse_dispatch_spec", dispatch)
    result = estate({"subtree": "[ap:DOCK01]"}, headings_only=False)
    assert "SUB" in result and len(specs) == 1
    assert specs[0].headings_only is False
    specs.clear()
    result = estate({"subtree": "[ap:DOCK01]"})
    assert "SUB" in result and len(specs) == 1
    assert specs[0].headings_only is True  # the baked default


def test_cf13_double_match_inside_note(env):
    # CF-13: the double-match arbitration arising INSIDE a note (a bare
    # handle line that matches both a node and a slice-group name).
    env["groups"] = [{**GROUP, "name": "c5e51af5"}]
    env["note"] = "c5e51af5"
    result = estate()
    assert result["success"] is False
    failed = result["failed_entries"][0]
    assert failed["error_extra"]["code"] == "simple_form_double_match"
    assert "simple_form_double_match" in failed["error"]


def test_cf14_quote_group_name_note_renders(env):
    # CF-14: an unquoted group name containing a double quote parses from a
    # one-line note (the gate excludes only { and [).
    env["groups"] = [{**GROUP, "name": 'Scratch | Estate "Test"'}]
    env["note"] = 'Scratch | Estate "Test"'
    result = estate()
    assert isinstance(result, str) and "Outer A" in result


def test_gplenh_describe_example():
    spec = srv.build_glimpse_registry().resolve("estate")
    # OperationSpec registration metadata is deep-frozen (mappingproxies):
    # repr, not json.dumps.
    examples = repr(list(spec.examples))
    assert "render_lens" in examples and "Lens sugar" in examples
    assert "a bare {lens:LensSpec}" in spec.returns["description"]
