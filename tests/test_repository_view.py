"""U1 repository_view: the fixture (MasterSpec §8 T1), goldens (T2), every
verb (T3), screen == file (T4), off == today (T5), refusals (T6) and the
unit pins (T7). House style: embedded literal goldens; the client is a
coherence-harness double over an in-memory cache; no network, no account.
"""
from __future__ import annotations

import asyncio
import copy
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from coherence_harness import make_boundary_capable
from workflowy_mcp import server
from workflowy_mcp.cache_render_spec import RenderPolicy, RenderProjectionSpec
from workflowy_mcp.coherence.read_advisory import COHERENCE_READ_ADVISORY_LINE
from workflowy_mcp.client.api_client_nexus import (
    WorkFlowyClientNexus,
    parse_cartographer_note_for_preview,
    resolve_cartographer_file_relpath,
)
from workflowy_mcp.mcp_api_registry import dispatch_operation

ROOT = "d0000000-0000-4000-8000-000000000000"
PKG = "d0000001-0000-4000-8000-000000000001"
CORE = "d0000002-0000-4000-8000-000000000002"
STORE = "d0000003-0000-4000-8000-000000000003"
GET = "d0000004-0000-4000-8000-000000000004"
SAVE = "d0000005-0000-4000-8000-000000000005"
OUTER = "d0000006-0000-4000-8000-000000000006"
HELPER = "d0000007-0000-4000-8000-000000000007"
VG = "d0000008-0000-4000-8000-000000000008"
LIMITS = "d0000009-0000-4000-8000-000000000009"
VERSION = "d000000a-0000-4000-8000-00000000000a"
APP = "d000000b-0000-4000-8000-00000000000b"
MAIN = "d000000c-0000-4000-8000-00000000000c"
GUIDE = "d000000d-0000-4000-8000-00000000000d"
GUIDE_H1 = "d000000e-0000-4000-8000-00000000000e"
INSTALL = "d000000f-0000-4000-8000-00000000000f"
EMPTY = "d0000010-0000-4000-8000-000000000010"
PLAIN = "d0000011-0000-4000-8000-000000000011"
JSF = "d0000012-0000-4000-8000-000000000012"
WIDGET = "d0000013-0000-4000-8000-000000000013"
RENDER = "d0000014-0000-4000-8000-000000000014"
JMAIN = "d0000015-0000-4000-8000-000000000015"
JSPAN = "d0000016-0000-4000-8000-000000000016"
ROOT2 = "d0000017-0000-4000-8000-000000000017"
F2 = "d0000018-0000-4000-8000-000000000018"
E2 = "d0000019-0000-4000-8000-000000000019"
ESTATE_AP = "eeeeeeee-0000-4000-8000-00000000eeee"


def _ast(qual: str, body: str) -> str:
    return f"AST_QUALNAME: {qual}\n---\n{body}"


def _ast_beacon(qual: str, comment: str, labels: str, body: str, bid: str) -> str:
    return (
        f"AST_QUALNAME: {qual}\n"
        "BEACON (AST)\n"
        f"id: {bid}\n"
        f"slice_labels: {labels}\n"
        "kind: ast\n"
        f"comment: {comment}\n"
        "\n"
        "---\n"
        f"{body}"
    )


def rv_rows():
    """The T1 fixture: the §4.10 map plus the T1 extras (save, outer/helper,
    VERSION, app.js). Priorities = source order."""
    return [
        {"id": ROOT, "name": "Source Map: demo", "note": "Root: E:\\repos\\demo\nLINE COUNT: 1200\n---\nSource map prose.", "parent_id": None, "priority": 0},
        {"id": PKG, "name": "📂 pkg", "note": "Path: pkg", "parent_id": ROOT, "priority": 0},
        {"id": CORE, "name": "🐍 core.py", "note": "Path: core.py\nLINE COUNT: 42\n---\nCore prose.", "parent_id": PKG, "priority": 0},
        {"id": STORE, "name": "📦 class Store", "note": _ast("demo.core.Store", "The store."), "parent_id": CORE, "priority": 0},
        {"id": GET, "name": "ƒ get(self, key: str) 🔱 #demo", "note": _ast_beacon("demo.core.Store.get", "Read one entry [ap:K7M2QF]", "demo", "Return the value for key.", "demo-get@0"), "parent_id": STORE, "priority": 0},
        {"id": SAVE, "name": "⚡ save(self, data) 🔱 #demo", "note": _ast("demo.core.Store.save", "Persist asynchronously."), "parent_id": STORE, "priority": 1},
        {"id": OUTER, "name": "ƒ outer()", "note": _ast("demo.core.outer", "Template <T> holds.\n    Path: x"), "parent_id": CORE, "priority": 1},
        {"id": HELPER, "name": "ƒ helper() 🔱 #demo", "note": _ast_beacon("demo.core.outer.helper", "Helps out.", "demo", "Uses ap:ABC123 internally.", "demo-help@0"), "parent_id": OUTER, "priority": 0},
        {"id": VG, "name": "📦 Group one", "note": "CARTOGRAPHER_VIRTUAL_GROUP: demo.core\nCARTO_GROUP_ID: g1\nCARTO_GROUP_LABEL: Group one\nCARTO_GROUP_MARKER_LINE: 88\n---\nSynthetic Cartographer grouping node.", "parent_id": CORE, "priority": 2},
        {"id": LIMITS, "name": "🔱 limits 🔱 #demo", "note": "BEACON (\nid: demo-limits@0\nrole: limits\nslice_labels: demo\nkind: span\nshow_span: true\ncomment: Size limits\n\nCONTEXT COMMENTS (PYTHON):\nTunable caps.\n\nSPAN TEXT:\nMAX = 10", "parent_id": VG, "priority": 0},
        {"id": VERSION, "name": "💎 VERSION 🔱 #demo", "note": _ast_beacon("demo.core.VERSION", "The version.", "demo", "The version string.", "demo-vers@0"), "parent_id": CORE, "priority": 3},
        {"id": APP, "name": "🐍 app.py", "note": "Path: app.py\nLINE COUNT: 9\n---\nApp prose.", "parent_id": ROOT, "priority": 1},
        {"id": MAIN, "name": "ƒ main(argv: list) 🔱 #demo #demo-cli", "note": _ast_beacon("demo.app.main", "CLI entry point", "demo,demo-cli", "Parse argv and run.", "demo-main@0"), "parent_id": APP, "priority": 0},
        {"id": GUIDE, "name": "📘 guide.md", "note": "Path: guide.md\nLINE COUNT: 5\n---\nGuide prose.", "parent_id": ROOT, "priority": 2},
        {"id": GUIDE_H1, "name": "Guide", "note": "MD_PATH:\n# Guide\n---\nGuide-level prose.", "parent_id": GUIDE, "priority": 0},
        {"id": INSTALL, "name": "Install 🔱 #demo", "note": "MD_PATH:\n# Guide\n## Install\n---\nBEACON (MD AST)\nid: guide-install@0\nslice_labels: demo\nkind: ast\ncomment: How to install the demo.\n---\nHow to install the demo.", "parent_id": GUIDE_H1, "priority": 0},
        {"id": EMPTY, "name": "🐍 empty.py", "note": "Path: empty.py\nLINE COUNT: 0\n---", "parent_id": ROOT, "priority": 3},
        {"id": JSF, "name": "🟨 app.js", "note": "Path: app.js\nLINE COUNT: 30\n---\nJS prose.", "parent_id": ROOT, "priority": 4},
        {"id": WIDGET, "name": "📦 class Widget", "note": _ast("demo.app.Widget", "A widget."), "parent_id": JSF, "priority": 0},
        {"id": RENDER, "name": "ƒ method render() 🔱 #demo", "note": _ast_beacon("demo.app.Widget.render", "Render the widget.", "demo", "Returns the node.", "js-render@0"), "parent_id": WIDGET, "priority": 0},
        {"id": JMAIN, "name": "ƒ function main(argv) 🔱 #demo", "note": _ast_beacon("demo.app.main", "JS entry point.", "demo", "Boot the app.", "js-main@0"), "parent_id": JSF, "priority": 1},
        {"id": JSPAN, "name": "🔱 auto-beacon 🔱 #demo", "note": "BEACON (\nid: js-span@0\nrole: auto-beacon\nslice_labels: demo\nkind: span\nshow_span: false\ncomment: The widget region.", "parent_id": JSF, "priority": 2},
        {"id": PLAIN, "name": "📝 Notes on demo #demo", "note": "Plain thoughts.", "parent_id": ROOT, "priority": 5},
        # The §4.8 multi-root witnesses: a second map root with one file.
        {"id": ROOT2, "name": "Source Map: other", "note": "Root: E:\\repos\\other", "parent_id": None, "priority": 6},
        {"id": F2, "name": "🐍 other.py", "note": "Path: other.py\nLINE COUNT: 2", "parent_id": ROOT2, "priority": 0},
        {"id": E2, "name": "ƒ only() 🔱 #demo", "note": _ast_beacon("demo2.other.only", "Other.", "demo", "Other body.", "other-only@0"), "parent_id": F2, "priority": 0},
        # A named-estate Anchor Point for the estate tests (bare-handle
        # resolution reads the cache; the live note read is mocked).
        {"id": ESTATE_AP, "name": "estate", "note": "placeholder", "parent_id": None, "priority": 9},
    ]


def rows_by_id():
    return {row["id"]: row for row in rv_rows()}


def _n(nid: str) -> dict:
    row = rows_by_id()[nid]
    return {"id": nid, "name": row["name"], "note": row["note"], "children": []}


def rv_forest():
    """The #demo selection's Stage-2 forest (members + connective spine)."""
    return [
        {
            "id": ROOT, "name": rows_by_id()[ROOT]["name"], "note": rows_by_id()[ROOT]["note"],
            "children": [
                {
                    "id": PKG, "name": rows_by_id()[PKG]["name"], "note": rows_by_id()[PKG]["note"],
                    "children": [
                        {
                            "id": CORE, "name": rows_by_id()[CORE]["name"], "note": rows_by_id()[CORE]["note"],
                            "children": [
                                {"id": STORE, "name": rows_by_id()[STORE]["name"], "note": rows_by_id()[STORE]["note"], "children": [_n(GET), _n(SAVE)]},
                                {"id": OUTER, "name": rows_by_id()[OUTER]["name"], "note": rows_by_id()[OUTER]["note"], "children": [_n(HELPER)]},
                                {"id": VG, "name": rows_by_id()[VG]["name"], "note": rows_by_id()[VG]["note"], "children": [_n(LIMITS)]},
                                _n(VERSION),
                            ],
                        },
                    ],
                },
                {"id": APP, "name": rows_by_id()[APP]["name"], "note": rows_by_id()[APP]["note"], "children": [_n(MAIN)]},
                {"id": GUIDE, "name": rows_by_id()[GUIDE]["name"], "note": rows_by_id()[GUIDE]["note"], "children": [
                    {"id": GUIDE_H1, "name": rows_by_id()[GUIDE_H1]["name"], "note": rows_by_id()[GUIDE_H1]["note"], "children": [_n(INSTALL)]},
                ]},
                {"id": JSF, "name": rows_by_id()[JSF]["name"], "note": rows_by_id()[JSF]["note"], "children": [
                    {"id": WIDGET, "name": rows_by_id()[WIDGET]["name"], "note": rows_by_id()[WIDGET]["note"], "children": [_n(RENDER)]},
                    _n(JMAIN),
                    _n(JSPAN),
                ]},
                _n(PLAIN),
            ],
        }
    ]


HEADINGS_GOLDEN = """# `app.js`

## class `Widget`

### method `render()`

Labels: demo

Render the widget.

## function `main(argv)`

Labels: demo

JS entry point.

## region `auto-beacon`

Labels: demo

The widget region.

# `app.py`

## function `main(argv: list)`

Labels: demo, demo-cli

CLI entry point

# `guide.md`

## section Guide

### section Install

Labels: demo

How to install the demo.

# `pkg/core.py`

## class `Store`

### method `get(self, key: str)`

Labels: demo

Read one entry

### async method `save(self, data)`

## function `outer()`

### function `helper()`

Labels: demo

Helps out.

## region `limits`

Labels: demo

Size limits

## constant `VERSION`

Labels: demo

The version.
"""

FULL_GOLDEN = """# `app.js`

## class `Widget`

```text
A widget.
```

### method `render()`

Labels: demo

Render the widget.

```text
Returns the node.
```

## function `main(argv)`

Labels: demo

JS entry point.

```text
Boot the app.
```

## region `auto-beacon`

Labels: demo

The widget region.

# `app.py`

## function `main(argv: list)`

Labels: demo, demo-cli

CLI entry point

```text
Parse argv and run.
```

# `guide.md`

## section Guide

```text
Guide-level prose.
```

### section Install

Labels: demo

How to install the demo.

```text
How to install the demo.
```

# `pkg/core.py`

## class `Store`

```text
The store.
```

### method `get(self, key: str)`

Labels: demo

Read one entry

```text
Return the value for key.
```

### async method `save(self, data)`

```text
Persist asynchronously.
```

## function `outer()`

```text
Template <T> holds.
    Path: x
```

### function `helper()`

Labels: demo

Helps out.

```text
Uses internally.
```

## region `limits`

Labels: demo

Size limits

```text
Tunable caps.
```

```text
MAX = 10
```

## constant `VERSION`

Labels: demo

The version.

```text
The version string.
```
"""


def setup(monkeypatch):
    client = make_boundary_capable(object.__new__(WorkFlowyClientNexus), nodes=rv_rows())
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    monkeypatch.setattr(
        server, "_resolve_note_session_config",
        AsyncMock(return_value=(["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})),
    )
    monkeypatch.setattr(
        server, "_resolve_slice_group_config",
        AsyncMock(return_value=(
            [{
                "id": "grp-demogroup01",
                "name": "Demo",
                "root_uuid": ROOT,
                "prefixes": ["demo"],
                "default_sort_by": None,
                "default_sort_direction": None,
            }],
            None,
            {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"},
            ["wfy-suppress"],
            {},
        )),
    )
    return client


def run(coro):
    return asyncio.run(coro)


async def inside(client, callback):
    async with server._mcp_operation_boundary("test.repository-view"):
        return callback()


def dispatch(family, verb, params):
    registry = {
        "glimpse": server.GLIMPSE_REGISTRY,
        "lightning_rod": server.LIGHTNING_ROD_REGISTRY,
        "slice_group": server.SLICE_GROUP_REGISTRY,
    }[family]
    return dispatch_operation(
        registry, verb, params,
        operation_boundary=server._mcp_operation_boundary,
        mutation_admission=server._mcp_mutation_admission,
        exception_mapper=server._mcp_exception_mapper,
        result_finalizer=server._mcp_operation_result_finalizer,
        operation_invoker=server._mcp_archive_operation_invoker,
    )


# The reply lines (§6.2). Headings-only processes no part lines, so the
# bare ap:ABC123 docstring token is only removed (and counted) in full mode.
REPLY_HEADINGS = (
    "> Repository view: 1 node(s) that are not mapped source code left out; "
    "1 Anchor Point reference(s) removed."
)
REPLY_FULL = (
    "> Repository view: 1 node(s) that are not mapped source code left out; "
    "2 Anchor Point reference(s) removed."
)

DEMO_PARCEL = {"tags": ["demo"], "root": ROOT}


# ---------------------------------------------------------------------------
# T2 — the goldens, both modes, on the canonical verb.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("headings_only,golden,reply", [(True, HEADINGS_GOLDEN, REPLY_HEADINGS), (False, FULL_GOLDEN, REPLY_FULL)])
def test_t2_render_tags_golden(monkeypatch, headings_only, golden, reply):
    setup(monkeypatch)
    result = run(dispatch("lightning_rod", "render_tags", {
        "root_handle": ROOT, "tags": ["demo"], "headings_only": headings_only,
        "repository_view": True,
    }))
    assert isinstance(result, str)
    assert result.startswith(reply + "\n")
    assert result == reply + "\n" + golden


def test_t2_full_mode_has_no_workflowy_trace(monkeypatch):
    setup(monkeypatch)
    result = run(dispatch("lightning_rod", "render_tags", {
        "root_handle": ROOT, "tags": ["demo"], "repository_view": True,
    }))
    assert "ap:" not in result
    assert "E:\\\\" not in result and "E:\\__" not in result
    assert "▸" not in result
    assert "AST_QUALNAME" not in result and "BEACON (" not in result
    assert "MD_PATH" not in result and "LINE COUNT" not in result
    assert "#demo" not in result and "🔱" not in result
    for glyph in ("📂", "🐍", "📘", "🟨", "📦", "💎", "⚡"):
        assert glyph not in result


# ---------------------------------------------------------------------------
# T3 — every verb of §3.1 over the same selection.
# ---------------------------------------------------------------------------


def _strip_reply(text: str) -> str:
    """Drop the reply framing above the document: the `> Repository view:`
    sentence line and (Chain 1 r2, W1) the canonical coherence advisory line
    with its blank separator, when the reply was delivered under a captured
    hard-dirty boundary. Clean replies carry neither, so existing callers
    are unaffected."""
    from workflowy_mcp.coherence.read_advisory import COHERENCE_READ_ADVISORY_LINE

    lines = text.split("\n")
    if lines and lines[0].startswith("> Repository view:"):
        text = text.split("\n", 1)[1]
        lines = text.split("\n")
    if lines and lines[0] == COHERENCE_READ_ADVISORY_LINE:
        text = text.split("\n", 2)[2] if text.count("\n") >= 2 else ""
    return text


def test_t3_glimpse_run_render_spec_tags(monkeypatch):
    setup(monkeypatch)
    result = run(dispatch("glimpse", "run", {
        "render_spec": DEMO_PARCEL, "headings_only": True, "repository_view": True,
    }))
    assert _strip_reply(result) == HEADINGS_GOLDEN


def test_t3_glimpse_run_render_spec_symbols(monkeypatch):
    setup(monkeypatch)
    result = run(dispatch("glimpse", "run", {
        "render_spec": {"symbols": [GET, SAVE, HELPER, LIMITS, VERSION, MAIN, INSTALL, RENDER, JMAIN, JSPAN], "root": ROOT},
        "headings_only": True, "repository_view": True,
    }))
    assert _strip_reply(result) == HEADINGS_GOLDEN


def test_t3_glimpse_run_group_handle(monkeypatch):
    setup(monkeypatch)
    result = run(dispatch("glimpse", "run", {
        "group_handle": "Demo", "headings_only": True, "repository_view": True,
    }))
    assert _strip_reply(result) == HEADINGS_GOLDEN


def test_t3_glimpse_run_group_saved_chronological_default_is_pinned(monkeypatch):
    # §3.5: a saved default sort never reaches the normalizer and never
    # reorders the view; the document is the document-order golden.
    setup(monkeypatch)

    real_config = server._resolve_slice_group_config

    async def config_with_chrono_default(target_tab):
        groups, active, postfixes, never, raw = await real_config(target_tab)
        groups[0]["default_sort_by"] = "modified"
        groups[0]["default_sort_direction"] = "desc"
        return groups, active, postfixes, never, raw

    monkeypatch.setattr(server, "_resolve_slice_group_config", config_with_chrono_default)
    result = run(dispatch("glimpse", "run", {
        "group_handle": "Demo", "headings_only": True, "repository_view": True,
    }))
    assert _strip_reply(result) == HEADINGS_GOLDEN


def test_t3_glimpse_run_root_handle_raw_dump(monkeypatch):
    # A raw dump of the ROOT admits everything; the view renders the same
    # entries (empty.py has none and does not appear) and counts both
    # non-source forest nodes (empty.py + the plain node).
    setup(monkeypatch)
    result = run(dispatch("glimpse", "run", {
        "root_handle": ROOT, "headings_only": True, "repository_view": True,
    }))
    assert _strip_reply(result) == HEADINGS_GOLDEN
    assert result.startswith(
        "> Repository view: 2 node(s) that are not mapped source code left out; "
        "1 Anchor Point reference(s) removed.\n"
    )


def test_t3_slice_group_render_and_merge_render_and_sift(monkeypatch):
    setup(monkeypatch)
    lens = {"kind": "tags", "root_handle": ROOT, "tags": ["demo"], "match_scope": "name"}
    result = run(dispatch("slice_group", "render", {
        "lens": lens, "headings_only": True, "repository_view": True,
    }))
    assert _strip_reply(result) == HEADINGS_GOLDEN, "render"
    merge_lens = {
        "kind": "merge",
        "operation": "union",
        "inputs": [dict(lens), dict(lens)],
    }
    result = run(dispatch("slice_group", "merge.render", {
        "lens": merge_lens, "headings_only": True, "repository_view": True,
    }))
    assert _strip_reply(result) == HEADINGS_GOLDEN, "merge.render"
    result = run(dispatch("slice_group", "sift", {
        "lens": lens, "query": "#demo", "match_scope": "name",
        "tier": "headings", "headings_only": True, "repository_view": True,
    }))
    assert _strip_reply(result) == HEADINGS_GOLDEN


def test_t3_lightning_render_search_and_render_symbols_and_render_nodes(monkeypatch):
    setup(monkeypatch)
    result = run(dispatch("lightning_rod", "render_search", {
        "root_handle": ROOT, "query": "#demo", "match_scope": "name",
        "headings_only": True, "repository_view": True,
    }))
    assert _strip_reply(result) == HEADINGS_GOLDEN
    symbols = [GET, SAVE, HELPER, LIMITS, VERSION, MAIN, INSTALL, RENDER, JMAIN, JSPAN]
    result = run(dispatch("lightning_rod", "render_symbols", {
        "symbols": symbols, "root_handles": ROOT, "headings_only": True,
        "repository_view": True,
    }))
    # A symbol list admits no plain node: the document is the golden; the
    # left-out count is 0 (nothing non-source was selected).
    assert _strip_reply(result) == HEADINGS_GOLDEN
    assert result.startswith(
        "> Repository view: 1 Anchor Point reference(s) removed.\n"
    )
    result = run(dispatch("lightning_rod", "render_nodes", {
        "symbols": symbols, "root_handles": ROOT, "headings_only": True,
        "repository_view": True,
    }))
    assert _strip_reply(result) == HEADINGS_GOLDEN


def test_t3_render_symbols_rooted_at_get_shows_class_store(monkeypatch):
    setup(monkeypatch)
    result = run(dispatch("lightning_rod", "render_symbols", {
        "symbols": [GET], "root_handles": [CORE], "headings_only": True,
        "repository_view": True,
    }))
    assert "# `pkg/core.py`\n\n## class `Store`\n\n### method `get(self, key: str)`" in result


def test_t3_glimpse_estate_single_parcel(monkeypatch):
    setup(monkeypatch)
    result = run(dispatch("glimpse", "estate", {
        "handle": DEMO_PARCEL, "headings_only": True, "repository_view": True,
    }))
    assert _strip_reply(result) == HEADINGS_GOLDEN


def _estate_read_mock(monkeypatch, note: str):
    from workflowy_mcp.models.node import WorkFlowyNode

    async def fake_read(handle, as_raw):
        return WorkFlowyNode(
            id="eeeeeeee-0000-4000-8000-00000000eeee", name="estate", note=note
        )

    client = make_boundary_capable(object.__new__(WorkFlowyClientNexus), nodes=rv_rows())
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    monkeypatch.setattr(
        server, "_resolve_note_session_config",
        AsyncMock(return_value=(["wfy-ns"], [], None, {"note_session_local_postfixes": ["local"]})),
    )
    monkeypatch.setattr(
        server, "_resolve_slice_group_config",
        AsyncMock(return_value=([], None, {"auto_expand": "expand", "suppress": "suppress", "deprecated": "deprecated"}, ["wfy-suppress"], {})),
    )
    monkeypatch.setattr(server, "_node_read_impl", fake_read)


def test_t3_glimpse_estate_named_estate_two_parcels(monkeypatch, tmp_path):
    note = (
        '{"tags": ["demo"], "root": "%s"}\n'
        '{"tags": ["demo"], "root": "%s"}'
    ) % (ROOT, CORE)
    _estate_read_mock(monkeypatch, note)
    # Named estate: the assembly joins the two clean documents with
    # '\n---\n\n' and no estate header/sections.
    result = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
    }))
    lines = result.split("\n")
    assert lines[0].startswith("> Repository view:")
    doc = result.split("\n", 1)[1]
    assert "\n---\n\n" in doc
    assert "🏛️ ESTATE" not in doc and "render_scope" not in doc
    # Plain-file form: the assembled document on disk, with the ack rollup.
    out = tmp_path / "estate.md"
    ack = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
        "output_file": str(out),
    }))
    assert ack["success"] is True
    assert ack["repository_view"]["entries"][0]["index"] == 1
    assert out.read_text(encoding="utf-8") == doc
    # Directory form: one file per entry, each exactly that entry's document.
    outdir = tmp_path / "entries"
    outdir.mkdir()
    ack2 = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
        "output_file": str(outdir) + "/",
    }))
    assert ack2["success"] is True
    files = sorted(p.name for p in outdir.iterdir())
    assert len(files) == 2
    # The 0-clauses vanish: an all-clean estate prints no Repository view line.
    note2 = '{"symbols": ["%s", "%s"], "root": "%s"}' % (SAVE, MAIN, ROOT)
    _estate_read_mock(monkeypatch, note2)
    clean = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
    }))
    assert clean.startswith("# `")


# ---------------------------------------------------------------------------
# T4 — screen == file, and the advisory never lands in the document.
# ---------------------------------------------------------------------------


def test_t4_file_bytes_equal_document_and_screen(monkeypatch, tmp_path):
    setup(monkeypatch)
    out = tmp_path / "slice.md"
    result = run(dispatch("lightning_rod", "render_tags", {
        "root_handle": ROOT, "tags": ["demo"], "headings_only": True,
        "repository_view": True, "output_file": str(out),
    }))
    assert result["success"] is True
    assert result["repository_view"] == {"left_out": 1, "ap_ids_removed": 1}
    file_bytes = out.read_bytes().decode("utf-8")
    assert file_bytes == HEADINGS_GOLDEN
    # Screen twin: the '> ' reply line above the same bytes; the in-memory
    # payload additionally carries D under `document` (never advised).
    screen = run(dispatch("lightning_rod", "render_tags", {
        "root_handle": ROOT, "tags": ["demo"], "headings_only": True,
        "repository_view": True,
    }))
    assert screen == REPLY_HEADINGS + "\n" + HEADINGS_GOLDEN


def test_t4_advisory_prefixes_screen_never_the_file(monkeypatch, tmp_path):
    setup(monkeypatch)
    import workflowy_mcp.coherence.read_advisory as ra

    original = ra.apply_coherence_advisory_to_text

    def advising(text):
        return "⚠️ ADVISORY\n" + original(text)

    monkeypatch.setattr(ra, "apply_coherence_advisory_to_text", advising)
    out = tmp_path / "slice.md"
    result = run(dispatch("lightning_rod", "render_tags", {
        "root_handle": ROOT, "tags": ["demo"], "headings_only": True,
        "repository_view": True, "output_file": str(out),
    }))
    assert result["success"] is True
    assert "ADVISORY" not in out.read_text(encoding="utf-8")
    assert out.read_text(encoding="utf-8") == HEADINGS_GOLDEN
    # And the screen twin is advised: the warning rides the body ABOVE the
    # document (below the `> ` reply lines, which are reply, never document).
    screen = run(dispatch("lightning_rod", "render_tags", {
        "root_handle": ROOT, "tags": ["demo"], "headings_only": True,
        "repository_view": True,
    }))
    body = screen.split("\n", 1)[1]
    assert body.startswith("⚠️ ADVISORY\n")


# ---------------------------------------------------------------------------
# T5 — off == today, byte for byte, per verb.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family,verb,params", [
    ("glimpse", "run", {"render_spec": DEMO_PARCEL, "headings_only": True}),
    ("glimpse", "run", {"root_handle": ROOT, "headings_only": True, "allow_anchor_dock": False}),
    ("glimpse", "estate", {"handle": DEMO_PARCEL, "headings_only": True}),
    ("slice_group", "render", {"lens": {"kind": "tags", "root_handle": ROOT, "tags": ["demo"], "match_scope": "name"}, "headings_only": True}),
    ("lightning_rod", "render_tags", {"root_handle": ROOT, "tags": ["demo"], "headings_only": True}),
    ("lightning_rod", "render_search", {"root_handle": ROOT, "query": "#demo", "match_scope": "name", "headings_only": True}),
    ("lightning_rod", "render_symbols", {"symbols": [GET, MAIN], "root_handles": ROOT, "headings_only": True}),
])
def test_t5_off_equals_today(monkeypatch, family, verb, params):
    client = setup(monkeypatch)
    absent = run(dispatch(family, verb, params))
    forced = run(dispatch(family, verb, {**params, "repository_view": False}))
    assert absent == forced
    assert "Repository view:" not in str(absent)


# ---------------------------------------------------------------------------
# T6 — the refusals (§3.3 boundary, §3.4 backstop, §4.8 multi-root).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family,verb,params", [
    ("glimpse", "run", {"root_handle": ROOT, "repository_view": True, "push_to_modal": True}),
    ("glimpse", "run", {"root_handle": ROOT, "repository_view": True, "as_markdown": False}),
    ("glimpse", "run", {"root_handle": ROOT, "repository_view": True, "full_uuids": True}),
    ("glimpse", "run", {"root_handle": ROOT, "repository_view": True, "include_archives": True}),
    ("glimpse", "run", {"root_handle": ROOT, "repository_view": True, "sort_by": "modified"}),
    ("glimpse", "run", {"root_handle": ROOT, "repository_view": True, "recent": "1w"}),
    ("glimpse", "run", {"repository_view": True}),
    ("glimpse", "run", {"repository_view": True, "render_spec": "modal"}),
    ("lightning_rod", "render_symbols", {"symbols": [GET], "root_handles": ROOT, "repository_view": True, "tier": "metadata"}),
    ("lightning_rod", "render_search", {"root_handle": ROOT, "query": "x", "repository_view": True, "tier": "context"}),
    ("slice_group", "sift", {"lens": {"kind": "tags", "root_handle": ROOT, "tags": ["demo"]}, "query": "x", "repository_view": True}),
])
def test_t6_boundary_refusals(monkeypatch, family, verb, params):
    setup(monkeypatch)
    result = run(dispatch(family, verb, params))
    assert isinstance(result, dict) and result.get("success") is False, result
    code = result.get("code") or (result.get("error") or {}).get("code")
    assert code == "invalid_parameter_combination", result


def test_t6_normalizer_backstop(monkeypatch):
    # §3.4: blob-sourced specs the boundary cannot see are refused by the
    # ONE normalizer (ValueError, the recursive=False mechanism).
    with pytest.raises(ValueError):
        RenderProjectionSpec.normalize_from_glimpse_args(
            root_uuid=ROOT, repository_view=True, as_markdown=False,
        )
    with pytest.raises(ValueError):
        RenderProjectionSpec.normalize_from_glimpse_args(
            root_uuid=ROOT, repository_view=True,
            focus={"mode": "row", "uuid": GET, "ancestor_path": [], "occurrence": 0},
        )
    with pytest.raises(ValueError):
        RenderProjectionSpec.normalize_from_glimpse_args(
            root_uuid=ROOT, repository_view=True, full_uuids=True,
        )
    with pytest.raises(ValueError):
        RenderProjectionSpec.normalize_from_glimpse_args(
            root_uuid=ROOT, repository_view=True, sort_by="created",
        )
    with pytest.raises(ValueError):
        from workflowy_mcp.glimpse_since_policy import SinceSpec

        RenderProjectionSpec.normalize_from_glimpse_args(
            root_uuid=ROOT, repository_view=True,
            since=SinceSpec(form="window", window_seconds=60),
        )
    # And the tri-state override seam re-runs the same guard on a
    # blob-sourced spec (the §3.4 catch).
    spec = RenderProjectionSpec.from_render_spec_blob(
        {"kind": "raw_root", "root_uuid": ROOT, "full_uuids": True}
    )
    with pytest.raises(ValueError):
        spec.with_view_overrides(repository_view=True)


def test_t6_estate_node_entry_fails_others_render(monkeypatch):
    note = '{"node": "%s"}\n{"tags": ["demo"], "root": "%s"}' % (ROOT, ROOT)
    _estate_read_mock(monkeypatch, note)
    result = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
    }))
    lines = result.split("\n")
    assert any(
        line.startswith("> Estate entry [1] failed:") and "repository_view" in line
        for line in lines
    )
    # The good entry still rendered its document.
    assert any(line.startswith("# `") for line in lines)


def test_t6_multi_root_refusal(monkeypatch):
    # §4.8: the projection refuses a render whose files resolve under more
    # than one map root (two maps can collide on relative paths), naming the
    # roots it found. No in-scope verb can legitimately span two maps — the
    # check is exercised at the ONE projection (a synthetic forest over both
    # cached maps).
    setup(monkeypatch)
    rows = rows_by_id()
    spanning = [
        {
            "id": ROOT, "name": rows[ROOT]["name"], "note": rows[ROOT]["note"],
            "children": [
                {"id": CORE, "name": rows[CORE]["name"], "note": rows[CORE]["note"], "children": [_n(GET)]},
                {"id": ROOT2, "name": rows[ROOT2]["name"], "note": rows[ROOT2]["note"], "children": [
                    {"id": F2, "name": rows[F2]["name"], "note": rows[F2]["note"], "children": [_n(E2)]},
                ]},
            ],
        }
    ]
    projected, left_out, ap_removed, error = (
        WorkFlowyClientNexus._repository_view_forest(
            spanning, cache_by_id=rows, headings_only=True,
        )
    )
    assert projected == []
    assert error is not None
    assert "one repository" in error
    assert "E:\\repos\\demo" in error and "E:\\repos\\other" in error


# ---------------------------------------------------------------------------
# T7 — the unit pins.
# ---------------------------------------------------------------------------


def test_t7_kinds_table():
    rows = rows_by_id()
    forest = [
        {"id": CORE, "name": rows[CORE]["name"], "note": rows[CORE]["note"], "children": [
            _n(STORE), _n(SAVE), _n(GET), _n(OUTER), _n(HELPER), _n(LIMITS), _n(VERSION),
            _n(JMAIN), _n(RENDER), _n(JSPAN), _n(GUIDE_H1),
        ]},
    ]
    projected, _left_out, _ap, error = WorkFlowyClientNexus._repository_view_forest(
        forest, cache_by_id=rows, headings_only=True,
    )
    assert error is None
    names = []
    for file_node in projected:
        for h in file_node["children"]:
            names.append(h["name"])
            for c in h.get("children") or []:
                names.append(c["name"])
    assert "class `Store`" in names
    assert "async method `save(self, data)`" in names
    assert "method `get(self, key: str)`" in names
    assert "function `outer()`" in names
    assert "function `helper()`" in names
    assert "region `limits`" in names
    assert "constant `VERSION`" in names
    assert "function `main(argv)`" in names  # JS kind word kept
    assert "method `render()`" in names
    assert "region `auto-beacon`" in names
    assert "section Guide" in names


def test_t7_span():
    from workflowy_mcp.client.api_client_nexus import _repository_view_span

    assert _repository_view_span("app.py") == "`app.py`"
    assert _repository_view_span("f(x)") == "`f(x)`"
    # A backtick inside t: the run grows one longer.
    assert _repository_view_span("a`b") == "``a`b``"
    # t starting AND ending with a backtick: one space of padding both sides.
    assert _repository_view_span("```x") == "```` ```x ````"
    assert _repository_view_span("`y`") == "`` `y` ``"


def test_t7_fence_choice_with_backtick_part():
    note = (
        "AST_QUALNAME: demo.core.tricky\n"
        "BEACON (AST)\n"
        "id: t@0\n"
        "slice_labels: demo\n"
        "kind: ast\n"
        "comment: Tricky.\n"
        "\n"
        "---\n"
        "```\nfenced\n```\n"
    )
    projected, _l, _m, error = WorkFlowyClientNexus._repository_view_forest(
        [{
            "id": CORE, "name": rows_by_id()[CORE]["name"], "note": rows_by_id()[CORE]["note"],
            "children": [{"id": "x1", "name": "ƒ tricky() 🔱 #demo", "note": note, "children": []}],
        }],
        cache_by_id={**rows_by_id(), "x1": {"id": "x1", "name": "ƒ tricky() 🔱 #demo", "note": note, "parent_id": CORE, "priority": 9}},
        headings_only=False,
    )
    assert error is None
    body = projected[0]["children"][0]["note"]
    # The ONE chooser's answer for a body that itself contains a ``` line:
    # the tilde fence (_tier_fence_for), opening + closing, "text" language.
    assert "~~~~text\n```\nfenced\n```\n~~~~" in body


def test_t7_path_helper_and_weave_worker():
    rows = rows_by_id()
    rel, root_abs = resolve_cartographer_file_relpath(file_node_id=CORE, nodes_by_id=rows)
    assert rel == "pkg/core.py"
    assert root_abs == "E:\\repos\\demo"
    rel2, root2 = resolve_cartographer_file_relpath(file_node_id=MAIN, nodes_by_id=rows)
    assert rel2 == "app.py"
    # Legacy absolute-Path file with no Root: ancestor -> the basename.
    legacy = {"id": "lg", "name": "x.py", "note": "Path: E:\\legacy\\x.py", "parent_id": None, "priority": 0}
    rel3, root3 = resolve_cartographer_file_relpath(file_node_id="lg", nodes_by_id={"lg": legacy})
    assert rel3 == "x.py" and root3 is None


def test_t7_projection_is_read_only_and_counts_left_out():
    rows = rows_by_id()
    before = copy.deepcopy(rv_forest())
    projected, left_out, ap_removed, error = WorkFlowyClientNexus._repository_view_forest(
        rv_forest(), cache_by_id=rows, headings_only=True,
    )
    assert error is None
    assert left_out == 1 and ap_removed == 1
    assert rv_forest() == before


# ---------------------------------------------------------------------------
# Chain 1 r1 (WorkOrder [ap:0IRD7R]) — W1 refusals on the non-tail forms and
# W2's empty-selection document/reply split. Each repaired-path test here
# FAILS on the 953f09e baseline (verified, see the RESULT's
# mutation-sensitivity section).
# ---------------------------------------------------------------------------


def _error_code(result) -> str:
    return result.get("code") or (result.get("error") or {}).get("code")


# W1 · the modal sentinel in every spelling the ingress recognizes.


def test_c1_modal_sentinel_refused_in_every_spelling(monkeypatch):
    setup(monkeypatch)
    mock = AsyncMock(return_value={"success": True, "preview_markdown": "m"})
    monkeypatch.setattr(server, "_glimpse_read_modal", mock)
    for spelling in ("modal", "MODAL", " MODAL ", "MoDaL"):
        result = run(dispatch("glimpse", "run", {
            "render_spec": spelling, "repository_view": True,
        }))
        assert result.get("success") is False, (spelling, result)
        assert _error_code(result) == "invalid_parameter_combination", spelling
        assert mock.await_count == 0, spelling


def test_c1_modal_spelling_off_reaches_the_reader_unchanged(monkeypatch):
    # A2: with the flag absent the case-insensitive sentinel behavior is
    # today's -- the modal reader is called exactly once.
    setup(monkeypatch)
    mock = AsyncMock(return_value={"success": True, "preview_markdown": "m"})
    monkeypatch.setattr(server, "_glimpse_read_modal", mock)
    result = run(dispatch("glimpse", "run", {"render_spec": "MODAL"}))
    assert mock.await_count == 1
    assert isinstance(result, str) and result == "m"


# W1 · the formal gather lowering (run call refusal; estate entry failure).


@pytest.mark.parametrize("as_json", [False, True])
def test_c1_formal_gather_refused_on_run(monkeypatch, as_json):
    setup(monkeypatch)
    spec = {"root_uuid": ROOT, "lightning": {"mode": "gather"}}
    result = run(dispatch("glimpse", "run", {
        "render_spec": json.dumps(spec) if as_json else spec,
        "repository_view": True,
    }))
    assert result.get("success") is False, result
    assert _error_code(result) == "invalid_parameter_combination", result
    # No #tag vocabulary anywhere in the refusal.
    assert "#demo" not in json.dumps(result)


def test_c1_gather_off_renders_today(monkeypatch):
    # A2: the same formal gather spec without the flag is today's outline.
    setup(monkeypatch)
    result = run(dispatch("glimpse", "run", {
        "render_spec": {"root_uuid": ROOT, "lightning": {"mode": "gather"}},
    }))
    assert isinstance(result, str)
    assert "#demo" in result


def test_c1_gather_estate_entry_fails_good_entry_renders(monkeypatch, tmp_path):
    gather = json.dumps({"root_uuid": ROOT, "lightning": {"mode": "gather"}})
    good = json.dumps({"tags": ["demo"], "root": ROOT})
    _estate_read_mock(monkeypatch, gather + "\n" + good)
    result = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
    }))
    lines = result.split("\n")
    assert any(
        line.startswith("> Estate entry [1] failed:") and "gather" in line
        for line in lines
    ), lines[:3]
    # The valid entry still renders its exact D; the reply carries no tag
    # vocabulary document.
    doc = "\n".join(l for l in lines if not l.startswith("> "))
    assert doc == HEADINGS_GOLDEN
    # Plain-file form: exactly the good entry's document bytes on disk.
    out = tmp_path / "estate.md"
    ack = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
        "output_file": str(out),
    }))
    assert ack["success"] is True
    assert out.read_text(encoding="utf-8") == HEADINGS_GOLDEN


def test_c1_normalizer_refuses_gather_backstop(monkeypatch):
    # §3.4 backstop: the gather combination raises from the ONE normalizer,
    # directly and through the tri-state override seam (the estate-entry
    # route -- its per-entry isolation turns the raise into a failed entry).
    with pytest.raises(ValueError):
        RenderProjectionSpec.normalize_from_glimpse_args(
            root_uuid=ROOT, lightning={"mode": "gather"}, repository_view=True,
        )
    spec = RenderProjectionSpec.from_render_spec_blob(
        {"root_uuid": ROOT, "lightning": {"mode": "gather"}}
    )
    with pytest.raises(ValueError):
        spec.with_view_overrides(repository_view=True)
    # Off: the same spec normalizes fine.
    ok = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid=ROOT, lightning={"mode": "gather"},
    )
    assert ok.lightning["mode"] == "gather"


# W1 · the {node} run parcel (a bare plain-node handle is already refused on
# run by the pre-existing read_note guard, flag-independent).


def test_c1_node_parcels_refused_on_run(monkeypatch):
    setup(monkeypatch)
    for node in (GET, PLAIN):
        result = run(dispatch("glimpse", "run", {
            "render_spec": {"node": node}, "repository_view": True,
        }))
        assert result.get("success") is False, (node, result)
        assert _error_code(result) == "invalid_parameter_combination", node


def test_c1_node_parcel_off_renders_today(monkeypatch):
    # A2: without the flag the {node} parcel still renders (the node-alone
    # read), byte-identical to baseline.
    setup(monkeypatch)
    result = run(dispatch("glimpse", "run", {"render_spec": {"node": GET}}))
    assert isinstance(result, str)
    assert result.startswith("# ")


# W2 · the empty-selection document/reply split, through the shared producer.


def _hard_boundary():
    from coherence_harness import s04_boundary_status

    return s04_boundary_status(
        hard_dirty=True,
        hard_generation=7,
        hard_reason_code="verified_effect_unpublishable",
        hard_transaction_id="tx-advisory",
    )


EMPTY_SEARCH = {
    "root_handle": ROOT,
    "query": "zzz-no-such-symbol",
    "match_scope": "name",
    "headings_only": True,
}


def test_c1_empty_search_file_zero_bytes_and_ack_counts(monkeypatch, tmp_path):
    setup(monkeypatch)
    out = tmp_path / "empty.md"
    ack = run(dispatch("lightning_rod", "render_search", {
        **EMPTY_SEARCH, "repository_view": True, "output_file": str(out),
    }))
    assert ack["success"] is True
    assert ack["repository_view"] == {"left_out": 0, "ap_ids_removed": 0}
    assert ack["document"] == ""
    assert ack["bytes_written"] == 0
    assert out.read_bytes() == b""
    assert "(empty)" not in out.read_text(encoding="utf-8")


def test_c1_empty_search_hard_dirty_advisory_never_in_file(monkeypatch, tmp_path):
    client = setup(monkeypatch)
    client._coherence_boundary_status = lambda: _hard_boundary()
    out = tmp_path / "empty.md"
    ack = run(dispatch("lightning_rod", "render_search", {
        **EMPTY_SEARCH, "repository_view": True, "output_file": str(out),
    }))
    assert out.read_bytes() == b""
    assert ack["repository_view"] == {"left_out": 0, "ap_ids_removed": 0}
    # The structured advisory stays on the acknowledgement (the reply).
    assert ack["coherence_advisory"]["status"] == "hard_dirty"
    assert ack["coherence_advisory"]["dirty_generation"] == 7
    # The screen reply keeps the canonical warning line (framing, not D).
    screen = run(dispatch("lightning_rod", "render_search", {**EMPTY_SEARCH, "repository_view": True}))
    assert "COHERENCE FAILURE" in screen
    assert "(empty)" in screen


def test_c1_empty_producer_internal_payload(monkeypatch):
    # The in-memory internal result carries BOTH the clean document ("") and
    # the view counts, beside the advised "(empty)" screen framing.
    setup(monkeypatch)

    async def _inside():
        async with server._mcp_operation_boundary("test.c1-empty"):
            return server._render_lens_empty_result(
                spec=RenderProjectionSpec(
                    root_uuid=ROOT, as_markdown=True, repository_view=True,
                ),
                output_file=None,
                source_meta={"_source": "test"},
            )

    payload = run(_inside())
    assert payload["document"] == ""
    assert payload["repository_view"] == {"left_out": 0, "ap_ids_removed": 0}
    assert payload["preview_markdown"] == "(empty)"
    assert payload["empty_selection"] is True


@pytest.mark.parametrize("family,verb,params", [
    ("slice_group", "render", {
        "lens": {"kind": "tags", "root_handle": ROOT, "tags": ["zzz-none"], "match_scope": "name"},
        "headings_only": True,
    }),
    ("slice_group", "merge.render", {
        "lens": {"kind": "merge", "operation": "union", "inputs": [
            {"kind": "tags", "root_handle": ROOT, "tags": ["zzz-none"], "match_scope": "name"},
            {"kind": "tags", "root_handle": ROOT, "tags": ["zzz-none"], "match_scope": "name"},
        ]},
        "headings_only": True,
    }),
    ("slice_group", "sift", {
        "lens": {"kind": "tags", "root_handle": ROOT, "tags": ["demo"], "match_scope": "name"},
        "query": "zzz-no-such-symbol", "match_scope": "name", "tier": "headings",
        "headings_only": True,
    }),
    ("glimpse", "run", {
        "render_spec": {"root_uuid": ROOT, "lightning": {
            "mode": "render_search", "query": "zzz-no-such-symbol",
            "query_mode": "smart", "match_scope": "name",
        }},
        "headings_only": True,
    }),
])
def test_c1_empty_lens_and_search_producers_share_the_contract(
    monkeypatch, tmp_path, family, verb, params,
):
    # Clean state: D == "" and the file receives exactly zero document bytes.
    setup(monkeypatch)
    out = tmp_path / "empty.md"
    ack = run(dispatch(family, verb, {**params, "repository_view": True, "output_file": str(out)}))
    assert ack["success"] is True, ack
    assert ack["document"] == ""
    assert ack["repository_view"] == {"left_out": 0, "ap_ids_removed": 0}
    assert ack["bytes_written"] == 0
    assert out.read_bytes() == b""
    # Hard-dirty state: the file is still exactly zero bytes; the warning
    # rides the reply only.
    client = setup(monkeypatch)
    client._coherence_boundary_status = lambda: _hard_boundary()
    out2 = tmp_path / "empty-hard.md"
    ack2 = run(dispatch(family, verb, {**params, "repository_view": True, "output_file": str(out2)}))
    assert out2.read_bytes() == b""
    assert ack2["coherence_advisory"]["status"] == "hard_dirty"
    assert "COHERENCE FAILURE" not in out2.read_text(encoding="utf-8")


def test_c1_estate_empty_entry_zero_counts_and_files(monkeypatch, tmp_path):
    empty_search = json.dumps({
        "root_uuid": ROOT, "lightning": {
            "mode": "render_search", "query": "zzz-no-such-symbol",
            "query_mode": "smart", "match_scope": "name",
        },
    })
    good = json.dumps({"tags": ["demo"], "root": ROOT})
    note = empty_search + "\n" + good
    client = setup(monkeypatch)
    client._coherence_boundary_status = lambda: _hard_boundary()
    from workflowy_mcp.models.node import WorkFlowyNode

    async def fake_read(handle, as_raw):
        return WorkFlowyNode(
            id="eeeeeeee-0000-4000-8000-00000000eeee", name="estate", note=note
        )

    monkeypatch.setattr(server, "_node_read_impl", fake_read)
    # Directory form: the empty entry's file is empty; the good entry's file
    # is its exact document; the rollup includes the successful empty entry.
    outdir = tmp_path / "entries"
    outdir.mkdir()
    ack = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
        "output_file": str(outdir) + "/",
    }))
    assert ack["success"] is True
    assert ack["repository_view"]["entries"] == [
        {"index": 1, "left_out": 0, "ap_ids_removed": 0, "unresolved_items": 0},
        {"index": 2, "left_out": 1, "ap_ids_removed": 1, "unresolved_items": 0},
    ]
    by_index = {w["index"]: w for w in ack["output_files"]}
    assert by_index[1]["bytes_written"] == 0
    assert by_index[1]["path"].endswith(".md")
    assert open(by_index[1]["path"], "rb").read() == b""
    assert open(by_index[2]["path"], "rb").read().decode("utf-8") == HEADINGS_GOLDEN
    # Plain-file form: the assembly joins only nonempty entry documents.
    plain = tmp_path / "estate.md"
    ack2 = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
        "output_file": str(plain),
    }))
    assert plain.read_text(encoding="utf-8") == HEADINGS_GOLDEN
    assert "COHERENCE FAILURE" not in plain.read_text(encoding="utf-8")
    assert ack2["repository_view"]["entries"][0]["index"] == 1
    # Chain 1 r2 (W1): the successful file acknowledgement carries the
    # structured advisory from the operation's captured status.
    assert ack2["coherence_advisory"]["status"] == "hard_dirty"
    assert ack2["coherence_advisory"]["dirty_generation"] == 7
    # Screen: the summed sentence + the document; the empty entry injects
    # neither (empty) nor an advisory into the assembly. Chain 1 r2 (W1):
    # under this hard-dirty boundary the canonical warning IS part of the
    # screen reply (below the `> ` line, above the document) and the file
    # acks carry the structured advisory -- the r1 assertion that the reply
    # framed only the view sentence was the F2 gap, now asserted positively.
    screen = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
    }))
    assert screen.startswith("> Repository view: 1 node(s) that are not mapped source code left out; 1 Anchor Point reference(s) removed.\n")
    assert COHERENCE_READ_ADVISORY_LINE in screen
    assert _strip_reply(screen) == HEADINGS_GOLDEN


def test_c1_empty_off_is_today(monkeypatch, tmp_path):
    # A2: with the flag absent (or false) the empty path keeps today's
    # placeholder and advisory-file behavior byte for byte.
    client = setup(monkeypatch)
    out = tmp_path / "off.md"
    absent = run(dispatch("lightning_rod", "render_search", {
        **EMPTY_SEARCH, "output_file": str(out),
    }))
    assert absent["success"] is True
    assert "repository_view" not in absent and "document" not in absent
    assert out.read_bytes() == b"(empty)"
    forced = run(dispatch("lightning_rod", "render_search", {
        **EMPTY_SEARCH, "repository_view": False, "output_file": str(tmp_path / "off2.md"),
    }))
    _echo = ("output_file", "path_record")
    strip_echo = lambda d: {k: v for k, v in d.items() if k not in _echo}
    assert strip_echo(forced) == strip_echo(absent)
    # Hard-dirty + OFF: the advisory lands in the file, exactly as today.
    client._coherence_boundary_status = lambda: _hard_boundary()
    out3 = tmp_path / "off-hard.md"
    run(dispatch("lightning_rod", "render_search", {
        **EMPTY_SEARCH, "output_file": str(out3),
    }))
    data = out3.read_bytes()
    assert data.startswith(
        b"\xe2\x9a\xa0\xef\xb8\x8f COHERENCE FAILURE"
    ) and data.endswith(b"(empty)")


# ---------------------------------------------------------------------------
# Chain 1 r2 (WorkOrder [ap:4Q3LQV]) — W1: the estate delivers the ONE
# coherence advisory authority's warning on its REPLY (screen line, structured
# ack key) while every repository-view file byte and assembled D stays clean,
# for direct and named estates, all-empty and mixed. Fresh assertions FAIL on
# 67304d0 (the estate screen carried no warning and the ack no advisory).
# ---------------------------------------------------------------------------

R2_EMPTY_SPEC = {
    "root_uuid": ROOT,
    "lightning": {
        "mode": "render_search", "query": "zzz-no-such-symbol",
        "query_mode": "smart", "match_scope": "name",
    },
}


def _fake_estate_node(monkeypatch, note: str) -> None:
    from workflowy_mcp.models.node import WorkFlowyNode

    async def fake_read(handle, as_raw):
        return WorkFlowyNode(id=ESTATE_AP, name="estate", note=note)

    monkeypatch.setattr(server, "_node_read_impl", fake_read)


def test_c1r2_direct_empty_entry_screen_warns_file_clean_ack_advises(monkeypatch, tmp_path):
    """Direct formal zero-hit search parcel: the screen reply carries the
    exact canonical warning; the file is exactly zero bytes; the ack carries
    the structured advisory with the captured generation."""
    client = setup(monkeypatch)
    client._coherence_boundary_status = lambda: _hard_boundary()
    handle = json.dumps(R2_EMPTY_SPEC)
    out = tmp_path / "direct.md"
    ack = run(dispatch("glimpse", "estate", {
        "handle": handle, "headings_only": True, "repository_view": True,
        "output_file": str(out),
    }))
    assert ack["success"] is True, ack
    assert out.read_bytes() == b""
    assert ack["output_files"][0]["bytes_written"] == 0
    assert ack["coherence_advisory"]["status"] == "hard_dirty"
    assert ack["coherence_advisory"]["dirty_generation"] == 7
    assert ack["coherence_advisory"]["reason_code"] == "verified_effect_unpublishable"
    assert ack["repository_view"]["entries"] == [
        {"index": 1, "left_out": 0, "ap_ids_removed": 0, "unresolved_items": 0},
    ]
    # Screen twin: the reply IS the canonical warning (nothing else renders),
    # framing only -- and the file above proves it never reached the disk.
    screen = run(dispatch("glimpse", "estate", {
        "handle": handle, "headings_only": True, "repository_view": True,
    }))
    assert screen == COHERENCE_READ_ADVISORY_LINE + "\n\n"
    assert "(empty)" not in screen


def test_c1r2_named_estate_all_empty_hard_dirty(monkeypatch, tmp_path):
    """A named estate whose EVERY entry is an empty supported search: the
    warning rides the screen and the ack; both per-entry files are empty."""
    client = setup(monkeypatch)
    client._coherence_boundary_status = lambda: _hard_boundary()
    _fake_estate_node(monkeypatch, json.dumps(R2_EMPTY_SPEC) + "\n" + json.dumps(R2_EMPTY_SPEC))
    outdir = tmp_path / "entries"
    outdir.mkdir()
    ack = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
        "output_file": str(outdir) + "/",
    }))
    assert ack["success"] is True, ack
    assert [w["bytes_written"] for w in ack["output_files"]] == [0, 0]
    for w in ack["output_files"]:
        assert open(w["path"], "rb").read() == b""
    assert ack["coherence_advisory"]["status"] == "hard_dirty"
    screen = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
    }))
    assert screen == COHERENCE_READ_ADVISORY_LINE + "\n\n"


def test_c1r2_named_estate_mixed_hard_dirty_all_sinks(monkeypatch, tmp_path):
    """Mixed empty+good named estate across directory, {index} pattern and
    plain-file sinks: warning on screen + ack; zero warning or (empty) bytes
    in ANY file; the good entry's golden unchanged; rollup rows complete."""
    client = setup(monkeypatch)
    client._coherence_boundary_status = lambda: _hard_boundary()
    note = json.dumps(R2_EMPTY_SPEC) + "\n" + json.dumps({"tags": ["demo"], "root": ROOT})
    _fake_estate_node(monkeypatch, note)
    # Directory sink.
    outdir = tmp_path / "d1"
    outdir.mkdir()
    ack = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
        "output_file": str(outdir) + "/",
    }))
    assert ack["success"] is True, ack
    assert ack["coherence_advisory"]["status"] == "hard_dirty"
    assert ack["repository_view"]["entries"] == [
        {"index": 1, "left_out": 0, "ap_ids_removed": 0, "unresolved_items": 0},
        {"index": 2, "left_out": 1, "ap_ids_removed": 1, "unresolved_items": 0},
    ]
    assert open(ack["output_files"][0]["path"], "rb").read() == b""
    assert open(ack["output_files"][1]["path"], "rb").read().decode("utf-8") == HEADINGS_GOLDEN
    # {index} pattern sink.
    pattern = str(tmp_path / "entry-{index}.md")
    ack2 = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
        "output_file": pattern,
    }))
    assert ack2["success"] is True, ack2
    assert ack2["coherence_advisory"]["dirty_generation"] == 7
    by_index = {w["index"]: w for w in ack2["output_files"]}
    assert by_index[1]["bytes_written"] == 0
    assert open(by_index[2]["path"], "rb").read().decode("utf-8") == HEADINGS_GOLDEN
    # Plain-file sink: the assembled document holds the good entry only.
    plain = tmp_path / "estate.md"
    ack3 = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
        "output_file": str(plain),
    }))
    assert plain.read_text(encoding="utf-8") == HEADINGS_GOLDEN
    assert ack3["coherence_advisory"]["status"] == "hard_dirty"
    # Screen: the summed sentence, then the canonical warning, then the doc.
    screen = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
    }))
    assert screen.startswith(
        "> Repository view: 1 node(s) that are not mapped source code left out; "
        "1 Anchor Point reference(s) removed.\n"
    )
    body = screen.split("\n", 1)[1]
    assert body.startswith(COHERENCE_READ_ADVISORY_LINE + "\n\n")
    assert _strip_reply(screen) == HEADINGS_GOLDEN


def test_c1r2_estate_clean_status_twin_no_invented_warning(monkeypatch, tmp_path):
    """Clean-status twin: no warning anywhere, no advisory key, and every
    reply byte-identical to the r1 shape."""
    setup(monkeypatch)
    handle = json.dumps(R2_EMPTY_SPEC)
    screen = run(dispatch("glimpse", "estate", {
        "handle": handle, "headings_only": True, "repository_view": True,
    }))
    # The empty assembled D shapes to the dispatcher's ONE empty-output stub
    # reply — framing only, and no warning is invented under a clean status.
    assert screen == "(empty)"
    assert "COHERENCE" not in screen
    out = tmp_path / "clean.md"
    ack = run(dispatch("glimpse", "estate", {
        "handle": handle, "headings_only": True, "repository_view": True,
        "output_file": str(out),
    }))
    assert out.read_bytes() == b""
    assert "coherence_advisory" not in ack
    # Mixed named estate, clean: exactly the r1 reply shape (sentence + doc).
    _fake_estate_node(monkeypatch, json.dumps(R2_EMPTY_SPEC) + "\n" + json.dumps({"tags": ["demo"], "root": ROOT}))
    screen2 = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
    }))
    assert screen2 == (
        "> Repository view: 1 node(s) that are not mapped source code left out; "
        "1 Anchor Point reference(s) removed.\n" + HEADINGS_GOLDEN
    )
    assert "COHERENCE" not in screen2


def test_c1r2_estate_off_mode_hard_dirty_unchanged(monkeypatch, tmp_path):
    """A2 collateral guard: with the flag off, the estate's hard-dirty framing
    is today's exactly -- the advisory rides INSIDE the advised entry documents
    (file included) and the ack gains no coherence_advisory key."""
    client = setup(monkeypatch)
    client._coherence_boundary_status = lambda: _hard_boundary()
    _fake_estate_node(monkeypatch, json.dumps({"tags": ["demo"], "root": ROOT}))
    plain = tmp_path / "off.md"
    ack = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "output_file": str(plain),
    }))
    assert ack["success"] is True, ack
    assert "coherence_advisory" not in ack
    data = plain.read_text(encoding="utf-8")
    # Today's off-mode framing: the estate header leads, and the advisory
    # rides INSIDE the advised entry document (never a top-level ack key).
    # (The entry renders today's ordinary view here — ids and metrics — not
    # the repository golden; off-vs-today byte equality is pinned by T5.)
    assert data.startswith("# 🏛️ ESTATE")
    assert "⚠️ COHERENCE FAILURE" in data


def test_c1r2_estate_warning_rides_the_one_authority(monkeypatch, tmp_path):
    """Provenance: the estate's warning comes from the ONE advisory authority
    (a wrapper marker shows up on the screen, never in a file) -- no copied
    literal, no private formatter."""
    client = setup(monkeypatch)
    client._coherence_boundary_status = lambda: _hard_boundary()
    import workflowy_mcp.coherence.read_advisory as ra

    original = ra.apply_coherence_advisory_to_text

    def advising(text):
        return "🧪 R2-AUTHORITY\n" + original(text)

    monkeypatch.setattr(ra, "apply_coherence_advisory_to_text", advising)
    _fake_estate_node(monkeypatch, json.dumps(R2_EMPTY_SPEC) + "\n" + json.dumps({"tags": ["demo"], "root": ROOT}))
    outdir = tmp_path / "d2"
    outdir.mkdir()
    ack = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
        "output_file": str(outdir) + "/",
    }))
    assert ack["success"] is True, ack
    for w in ack["output_files"]:
        assert "R2-AUTHORITY" not in open(w["path"], "rb").read().decode("utf-8")
    screen = run(dispatch("glimpse", "estate", {
        "handle": ESTATE_AP, "headings_only": True, "repository_view": True,
    }))
    assert screen.startswith("> Repository view: 1 node(s) that are not mapped source code left out; 1 Anchor Point reference(s) removed.\n🧪 R2-AUTHORITY\n")
