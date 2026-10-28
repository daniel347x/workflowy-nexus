"""The skill's parcel table as executable rows (QBCTWU U3-3 / unit 3e).

One test per row of the Load GLIMPSE skill's §8 parcel table ([ap:CDE6YO]
L194-203) and per item of "The three ways to write a parcel" (§8 L183-192):
the describe texts teach the skill's words, and these tests pin that the CODE
does what the describe says — including the two deliberate deviations recorded
in the unit RESULT (a named slice group renders as the group itself defines,
never a forced headings-only; `{node}` lowers through the render_nodes
mechanism). Offline SCRATCH-shaped fixtures; real render funnel, no live
daemon calls (same pattern as tests/test_estate_simple_forms.py, which owns
the grammar/impl-level coverage).
"""
import asyncio
import json

import pytest
from workflowy_mcp import server as srv
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus
from workflowy_mcp.mcp_api_registry import dispatch_operation

DOCK = "c5e51af5-1111-2222-3333-444444444444"
ESTATE = "97a26cce-1111-2222-3333-444444444444"
PLAIN = "dddddddd-1111-2222-3333-444444444444"  # Inner D: no Dock tag, prose note
NODES = [
    {"id": DOCK, "name": "[ap:DOCK01] Estate Test Dock #wfy-ns", "note": '"description is a group"', "parent_id": None},
    {"id": "aaaaaaaa-1111-2222-3333-444444444444", "name": "Outer A #estate-test", "note": "outer body", "parent_id": DOCK},
    {"id": "b5cb2d0b-1111-2222-3333-444444444444", "name": "Inner Dock #wfy-ns--local", "note": "inner dock", "parent_id": DOCK},
    {"id": "cccccccc-1111-2222-3333-444444444444", "name": "Inner C #estate-test", "note": "inner body", "parent_id": "b5cb2d0b-1111-2222-3333-444444444444"},
    {"id": PLAIN, "name": "Inner D", "note": "inner D body", "parent_id": "b5cb2d0b-1111-2222-3333-444444444444"},
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
        return __import__("types").SimpleNamespace(id=ESTATE, name="T2 estate", note=state["note"])

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


def estate_call(handle, **kw):
    """One public glimpse.estate call through the registry dispatcher."""
    return asyncio.run(dispatch_operation(srv.build_glimpse_registry(), "estate", {"handle": handle, **kw}))


def lens_call(verb, lens, **kw):
    return asyncio.run(dispatch_operation(srv.build_slice_group_registry(), verb, {"lens": lens, **kw}))


# ── Row 1 · Named slice group ────────────────────────────────────────────────
# Skill row: `Recruiting` / "Recruiting" / {group} / {ref,…slice_group}.
# DELIBERATE DEVIATION from the skill's default-read cell (recorded): Dan's
# ruling — a named slice group with no override renders the way the group
# itself defines, never a headings-only promise; a call-level flag overrides.

@pytest.mark.parametrize("form", ["Scratch | Estate Test", '"Scratch | Estate Test"', {"group": "Scratch | Estate Test"}, {"ref": "Scratch | Estate Test", "kind": "slice_group"}])
def test_row_named_slice_group_renders_as_the_group_defines(env, form):
    result = estate_call(form)
    assert isinstance(result, str) and "Outer A" in result
    # The group defines the read: full bodies, no headings-only bake.
    assert "outer body" in result
    # The call-level override still wins.
    compact = estate_call(form, headings_only=True)
    assert "Outer A" in compact and "outer body" not in compact


# ── Row 2 · Unnamed slice group ({tags, root}) ───────────────────────────────

def test_row_unnamed_tags_slice_headings_only_default(env):
    result = estate_call({"tags": ["estate-test"], "root": DOCK})
    assert "Outer A" in result and "outer body" not in result  # headings-only baked
    full = estate_call({"tags": ["estate-test"], "root": DOCK}, headings_only=False)
    assert "outer body" in full  # the call-level flag overrides


# ── Row 3 · Anchor Dock (bare / {dock} / long) ──────────────────────────────

@pytest.mark.parametrize("form", ["[ap:DOCK01]", {"dock": DOCK}, {"ref": DOCK, "kind": "dock"}])
def test_row_anchor_dock_stops_at_child_docks(env, form):
    result = estate_call(form)
    assert "Outer A" in result
    assert "outer body" not in result  # headings-only baked (like a recursive dock.peek)
    assert "inner dock" not in result
    # The child Dock itself is a signpost; its descendants are NOT entered
    # (assert on the heading lines: "Inner D" alone would substring-match
    # the "Inner Dock" signpost's name).
    assert "Inner Dock" in result
    assert "## [cccccccc]" not in result and "## [dddddddd]" not in result


@pytest.mark.parametrize("form", [{"dock": DOCK, "descend": True}, {"ref": DOCK, "kind": "dock", "descend": True}])
def test_row_anchor_dock_descend_traverses_child_docks(env, form):
    result = estate_call(form)
    assert "Inner C" in result and "Inner D" in result
    assert "inner body" not in result  # still headings-only


# ── Row 4 · A node on its own (bare plain-node fallback / {node} / long) ─────

def test_row_node_form_renders_the_node_alone(env):
    result = estate_call({"node": PLAIN})
    assert "Inner D" in result and "inner D body" in result
    assert "Outer A" not in result and "Inner C" not in result  # no descendants
    alias = estate_call({"ref": PLAIN, "kind": "node"})
    assert "Inner D" in alias and "Outer A" not in alias  # long ≡ short


def test_row_bare_plain_node_fallback(env):
    # A bare handle that is neither a Dock nor an estate reads the node on
    # its own (the skill's plain-node row) — never an unknown-group error.
    env["note"] = "prose line one\nprose line two"
    result = estate_call(ESTATE)
    assert isinstance(result, str) and "T2 estate" in result
    assert "Outer A" not in result and len(env["reads"]) == 1


def test_row_one_line_plain_note_is_not_an_estate(env):
    # A one-line prose note resolves as a bare-name parcel, fails the group
    # lookup, and demotes the node to a plain read — it is NOT an estate.
    # The {node} render reads the PINNED CACHE node, so the cache note and
    # the live note (the classifier's operand) must agree here.
    env["note"] = "Just a plain one-line note."
    next(n for n in env["nodes"] if n["id"] == ESTATE)["note"] = env["note"]
    result = estate_call(ESTATE)
    assert isinstance(result, str)
    assert "T2 estate" in result and "Just a plain one-line note." in result
    assert "Outer A" not in result and "ENTRY FAILED" not in result


# ── Row 5 · A node as its whole subtree ({subtree} / long how:subtree) ──────

@pytest.mark.parametrize("form", [{"subtree": DOCK}, {"ref": DOCK, "kind": "node", "how": "subtree"}])
def test_row_subtree_renders_every_descendant_headings_only(env, form):
    result = estate_call(form)
    for marker in ("Outer A", "Inner C", "Inner D", "Inner Dock"):
        assert marker in result, (form, marker)
    assert "outer body" not in result and "inner D body" not in result  # headings-only default
    full = estate_call(form, headings_only=False)
    assert "outer body" in full  # call-level override


# ── Row 6 · Symbol list (a beacon slice) ─────────────────────────────────────

def test_row_symbols_lower_to_the_beacon_slice(env, monkeypatch):
    calls = []

    async def symbols(**kw):
        calls.append(kw)
        return {"success": True, "preview_markdown": "SYMBOLS"}

    monkeypatch.setattr(srv, "_lightning_rod_render_symbols", symbols)
    result = estate_call({"symbols": ["parse_estate_note"], "root": DOCK})
    assert "SYMBOLS" in result and len(calls) == 1
    assert calls[0]["symbols"] == ["parse_estate_note"] and calls[0]["root_uuids"] == [DOCK]


# ── Row 7 · A named estate (parcels one per line, bare or fenced) ────────────

def test_row_named_estate_three_parcels_three_documents(env):
    env["note"] = 'Scratch | Estate Test\n{"dock": "c5e51af5"}\n{"subtree": "c5e51af5"}'
    result = estate_call(ESTATE)
    assert isinstance(result, str) and "3 rendered" in result
    # Parcel 1 (the named group) renders as the group defines — full bodies
    # for its members AND the connective spine it walks through.
    assert "outer body" in result and "inner body" in result and "inner dock" in result
    # Parcels 2-3 are headings-only: "outer body" appears exactly once (the
    # group parcel's), and the subtree-only note never shows at all.
    assert result.count("outer body") == 1 and "inner D body" not in result
    # The subtree parcel still reaches Inner D as a heading.
    assert "## [dddddddd]" in result
    # Order: one document per parcel, in note order.
    assert result.index("Scratch | Estate Test") < result.index("c5e51af5-1111")


def test_row_named_estate_fenced(env):
    env["note"] = 'My estate:\n\n```text\nScratch | Estate Test\n{"subtree": "c5e51af5"}\n```\n'
    result = estate_call(ESTATE)
    assert isinstance(result, str) and "2 rendered" in result and "Inner D" in result


# ── Row 8 · Anything richer: formal JSON renders as saved ────────────────────

@pytest.mark.parametrize("form", [{"spec": {"root_uuid": DOCK, "headings_only": False}}, {"root_uuid": DOCK}])
def test_row_formal_json_renders_as_saved(env, form):
    result = estate_call(form)
    assert isinstance(result, str) and "outer body" in result and "Inner D" in result


# ── Way 2 · qualified-form validation (the long alias and its refusals) ─────

@pytest.mark.parametrize("short_form,long_form", [
    ({"dock": DOCK}, {"ref": DOCK, "kind": "dock"}),
    ({"dock": DOCK, "descend": True}, {"ref": DOCK, "kind": "dock", "descend": True}),
    ({"node": PLAIN}, {"ref": PLAIN, "kind": "node"}),
    ({"subtree": DOCK}, {"ref": DOCK, "kind": "node", "how": "subtree"}),
    ("Scratch | Estate Test", {"ref": "Scratch | Estate Test", "kind": "slice_group"}),
])
def test_way2_long_alias_equals_short(env, short_form, long_form):
    # The meta header echoes the call's own handle, so equality is asserted
    # on the rendered body (everything after the header's blank line).
    short = estate_call(short_form)
    long = estate_call(long_form)
    assert isinstance(short, str) and isinstance(long, str), (short, long)
    assert short.split("\n\n", 1)[1] == long.split("\n\n", 1)[1]


@pytest.mark.parametrize("kind", ["file", "uri"])
def test_way2_file_and_uri_are_not_parcels(env, kind):
    ref = "E:\\docs\\design.md" if kind == "file" else "https://example.com/design"
    result = estate_call({"ref": ref, "kind": kind})
    assert result["error"]["code"] == "simple_form_not_a_parcel"
    assert "directly" in result["error"]["message"] or "file tools" in result["error"]["message"]


@pytest.mark.parametrize("text", ["/mnt/e/docs/design.md", "https://example.com/design"])
def test_way2_path_like_bare_string_is_not_a_parcel(env, text):
    result = estate_call(text)
    assert result["error"]["code"] == "simple_form_not_a_parcel"


def test_way2_settings_keys_refused_inside_a_parcel(env):
    short = estate_call({"dock": DOCK, "read": "required"})
    assert short["error"]["code"] == "simple_form_settings_in_parcel"
    assert "BESIDE the parcel" in short["error"]["message"]
    long = estate_call({"ref": DOCK, "kind": "dock", "view": "headings_only"})
    assert long["error"]["code"] == "simple_form_settings_in_parcel"


def test_way2_yaml_style_entry_is_invalid_json(env):
    result = estate_call('{dock: "c5e51af5-1111-2222-3333-444444444444"}')
    assert result["success"] is False
    assert "invalid JSON" in result["error"]["message"]


def test_way2_long_form_how_and_descend_scoping(env):
    how = estate_call({"ref": DOCK, "kind": "dock", "how": "subtree"})
    assert how["success"] is False and "how" in how["error"]["message"]
    descend = estate_call({"ref": DOCK, "kind": "node", "descend": True})
    assert descend["error"]["code"] == "simple_form_descend_refused"


# ── Estates do not nest (§8 L205) ─────────────────────────────────────────────

def test_estates_do_not_nest_inside_a_note(env):
    env["note"] = '[ap:DOCK01]\n97a26cce'
    result = estate_call(ESTATE)
    assert isinstance(result, str) and "1 rendered" in result
    assert "Outer A" in result  # the Dock line rendered
    assert "estates do not nest" in result  # the non-Dock line failed loudly


# ── The lens surface: {subtree} atom + long alias in; {node}/{symbols} out ──

def test_lens_subtree_atom_and_long_alias(env):
    short = lens_call("render", {"subtree": DOCK})
    assert "Outer A" in short and "outer body" in short  # render defaults to bodies
    alias = lens_call("render", {"ref": DOCK, "kind": "node", "how": "subtree"})
    assert alias == short  # the long alias lowers to the same atom


@pytest.mark.parametrize("verb", ["render", "sift", "merge.render"])
def test_lens_refuses_node_and_symbols(env, verb):
    for form in ({"node": PLAIN}, {"symbols": ["parse_estate_note"], "root": DOCK}):
        params = {"lens": form}
        if verb == "sift":
            params["query"] = "Outer"
        out = lens_call(verb, **params)
        assert out["error"]["code"] == "simple_form_wrong_surface", (verb, form)


# ── The four description sites say the same thing in the skill's words ──────

def test_describe_sites_use_the_skill_words():
    glimpse = srv.build_glimpse_registry()
    lenses = srv.build_slice_group_registry()
    estate_doc = json.dumps(glimpse.describe_full("estate"))
    run_doc = json.dumps(glimpse.describe_full("run"))
    render_doc = json.dumps(lenses.describe_full("render"))
    sift_doc = json.dumps(lenses.describe_full("sift"))
    merge_doc = json.dumps(lenses.describe_full("merge.render"))
    # Vocabulary: parcel · estate · named estate.
    assert "PARCEL" in estate_doc and "NAMED ESTATE" in estate_doc
    # Every site lists {node}, {subtree} and the long {ref, kind} alias.
    for doc in (estate_doc, run_doc, render_doc, sift_doc, merge_doc):
        assert "{node" in doc and "{subtree" in doc and "{ref" in doc, doc[:80]
    # CF-20: the named-group exception is the estate describe's promise.
    assert "renders the way the slice group itself defines" in estate_doc
    assert "never a headings-only promise" in estate_doc
    # Settings beside the parcel; file/uri refused; the plain-node fallback.
    assert "never inside it" in estate_doc
    assert "attachment-only forms" in estate_doc
    assert "plain-node fallback" in estate_doc
    # {node}/{symbols} refused on the lens surfaces (all three describes).
    for doc in (render_doc, sift_doc, merge_doc):
        assert "refused here" in doc
