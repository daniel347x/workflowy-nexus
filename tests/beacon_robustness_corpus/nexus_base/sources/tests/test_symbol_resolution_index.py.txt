"""Symbol-name index equivalence (NBBW follow-up, program [ap:3ACUQS]).

``tests/symbol_index_golden.json`` was produced by the PRE-INDEX resolver
(commit 94dadb0, the full-cache scan on every call) over the synthetic corpus
below: every query's candidates-only return and its snippet-mode error text.
The indexed resolver must reproduce it byte for byte -- same candidates, same
order, same descriptors (file_path, via buckets), same errors -- across exact
beacon/ast/name hits, qualified and short AST names, dotted-tail and contains
fallbacks, symbol_kind restrictions and aliases, file_path narrowing, Markdown
headings, HIDE CARTOGRAPHER and #wfy-suppress. (The same comparison over a
copy of the real 36k-node cache is recorded in the program socket [ap:0SXHT8].)
Also: the index is reused for one cache generation and rebuilt for a new one,
and resolution runs off the event loop.
"""

import asyncio
import json
import os
import sys
import threading

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from workflowy_mcp.client import api_client_nexus as acn  # noqa: E402



def build_corpus():
    nodes = []

    def add(nid, name, note, parent):
        nodes.append({"id": nid, "name": name, "note": note, "parent_id": parent})

    R = "00000000-0000-0000-0000-000000000001"
    add(R, "Source Map: repo #nexus--map", "Root: /nbbw/repo", None)
    # server.py (python), nested methods, beacons
    F1 = "00000000-0000-0000-0000-0000000000f1"
    add(F1, "🐍 server.py", "Path: server.py\nLINE COUNT: 10", R)
    add("10000000-0000-0000-0000-000000000001", "📦 class Client", "AST_QUALNAME: Client\n", F1)
    add("10000000-0000-0000-0000-000000000002", "⚡ fetch(self, x) #net", "AST_QUALNAME: Client.fetch\n", "10000000-0000-0000-0000-000000000001")
    add("10000000-0000-0000-0000-000000000003", "ƒ helper()", "AST_QUALNAME: helper\nBEACON (AST)\nid: srv@helper\nrole: helper role text\nkind: ast\n", F1)
    add("10000000-0000-0000-0000-000000000004", "ƒ fetch_all()", "AST_QUALNAME: fetch_all\n", F1)
    add("10000000-0000-0000-0000-000000000005", "ƒ duplicate_name()", "AST_QUALNAME: duplicate_name\n", F1)
    # util.py with a duplicate and a span beacon
    F2 = "00000000-0000-0000-0000-0000000000f2"
    add(F2, "🐍 util.py", "Path: util.py", R)
    add("20000000-0000-0000-0000-000000000001", "ƒ duplicate_name()", "AST_QUALNAME: duplicate_name\n", F2)
    add("20000000-0000-0000-0000-000000000002", "🔱 span thing", "BEACON (SPAN)\nid: util@span-thing\nrole: span role\nkind: span\n", F2)
    add("20000000-0000-0000-0000-000000000003", "ƒ vue_like()", "AST_QUALNAME: vue:script0:vue_like\n", F2)
    # a docs folder with a Markdown file
    D = "00000000-0000-0000-0000-0000000000d0"
    add(D, "📂 docs", "Path: docs", R)
    F3 = "00000000-0000-0000-0000-0000000000f3"
    add(F3, "📄 guide.md", "Path: guide.md", D)
    add("30000000-0000-0000-0000-000000000001", "# Getting Started", "MD_PATH:\n- Getting Started\n", F3)
    add("30000000-0000-0000-0000-000000000002", "## Install - Final Steps #doc", "MD_PATH:\n- Getting Started\n- Install - Final Steps\n", "30000000-0000-0000-0000-000000000001")
    add("30000000-0000-0000-0000-000000000003", "## helper", "MD_PATH:\n- Getting Started\n- helper\n", "30000000-0000-0000-0000-000000000001")
    # a HIDE CARTOGRAPHER sentinel hiding a second mapping
    H = "00000000-0000-0000-0000-0000000000a0"
    add(H, "HIDE CARTOGRAPHER #x", "", None)
    F4 = "00000000-0000-0000-0000-0000000000f4"
    add(F4, "🐍 hidden.py", "Path: /nbbw/other/hidden.py", H)
    add("40000000-0000-0000-0000-000000000001", "ƒ hidden_only()", "AST_QUALNAME: hidden_only\n", F4)
    add("40000000-0000-0000-0000-000000000002", "ƒ helper()", "AST_QUALNAME: helper\n", F4)
    # a #wfy-suppress'd copy (only filtered when suppress_tags is passed)
    S = "00000000-0000-0000-0000-0000000000b0"
    add(S, "Old copy #<u>wfy-suppress</u>", "", None)
    F5 = "00000000-0000-0000-0000-0000000000f5"
    add(F5, "🐍 copy.py", "Path: /nbbw/copy/copy.py", S)
    add("50000000-0000-0000-0000-000000000001", "ƒ fetch_all()", "AST_QUALNAME: fetch_all\n", F5)
    add("50000000-0000-0000-0000-000000000002", "ƒ only_in_copy()", "AST_QUALNAME: only_in_copy\n", F5)
    return nodes


QUERIES = [
    {"symbol": "helper"},
    {"symbol": "srv@helper"},
    {"symbol": "helper role text"},
    {"symbol": "Client.fetch"},
    {"symbol": "fetch"},
    {"symbol": "class Client"},
    {"symbol": "ƒ fetch_all(self)"},
    {"symbol": "duplicate_name"},
    {"symbol": "duplicate_name", "file_path": "util.py"},
    {"symbol": "duplicate_name", "file_path": "util"},
    {"symbol": "duplicate_name", "file_path": "/nbbw/repo/server.py"},
    {"symbol": "duplicate_name", "file_path": "repo/util.py"},
    {"symbol": "duplicate_name", "file_path": "nope.py"},
    {"symbol": "Wrong.fetch_all"},
    {"symbol": "Other.fetch"},
    {"symbol": "etch_a"},
    {"symbol": "span"},
    {"symbol": "util@span-thing"},
    {"symbol": "vue_like"},
    {"symbol": "vue:script0:vue_like"},
    {"symbol": "server.py"},
    {"symbol": "util"},
    {"symbol": "Getting Started"},
    {"symbol": "Install - Final Steps"},
    {"symbol": "Final Steps"},
    {"symbol": "hidden_only"},
    {"symbol": "only_in_copy"},
    {"symbol": "fetch_all"},
    {"symbol": "fetch_all", "suppress_tags": ["wfy-suppress"]},
    {"symbol": "only_in_copy", "suppress_tags": ["wfy-suppress"]},
    {"symbol": "zz_missing_symbol"},
    {"symbol": "helper", "symbol_kind": "beacon"},
    {"symbol": "helper", "symbol_kind": "ast"},
    {"symbol": "helper", "symbol_kind": "name"},
    {"symbol": "srv@helper", "symbol_kind": "ast"},
    {"symbol": "fetch", "symbol_kind": "function"},
    {"symbol": "Client.fetch", "symbol_kind": "qualname"},
    {"symbol": "etch_a", "symbol_kind": "beacon"},
    {"symbol": "helper", "symbol_kind": "bogus"},
]


class _Client(acn.WorkFlowyClientNexus):
    def __init__(self):  # noqa: D401 - skips the real ctor (no network)
        pass


def _install(monkeypatch, nodes):
    async def _export(_client, node_id=None, use_cache=True, force_refresh=False, **_kw):
        return {"nodes": nodes}

    monkeypatch.setattr(acn, "export_nodes_impl", _export)
    return _Client()


def _run_queries(client):
    out = []

    async def main():
        for q in QUERIES:
            kw = {"suppress_tags": q["suppress_tags"]} if "suppress_tags" in q else {}
            try:
                r = await client.read_text_snippet_by_symbol(
                    file_path=q.get("file_path"), symbol=q["symbol"],
                    symbol_kind=q.get("symbol_kind", "auto"),
                    return_candidates_only=True, **kw)
                out.append(["ok", r])
            except Exception as e:  # noqa: BLE001
                out.append(["err", type(e).__name__, str(e)])
            try:
                await client.read_text_snippet_by_symbol(
                    file_path=q.get("file_path"), symbol=q["symbol"],
                    symbol_kind=q.get("symbol_kind", "auto"), **kw)
                out[-1].append("snippet-ok")
            except Exception as e:  # noqa: BLE001
                out[-1].append(["snippet-err", type(e).__name__, str(e)])

    asyncio.run(main())
    return json.loads(json.dumps(out, ensure_ascii=False))


def test_indexed_resolver_matches_the_pre_index_golden(monkeypatch):
    golden_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "symbol_index_golden.json")
    with open(golden_path, encoding="utf-8") as f:
        golden = json.load(f)
    client = _install(monkeypatch, build_corpus())
    got = _run_queries(client)
    assert len(got) == len(golden) == len(QUERIES)
    for q, g, n in zip(QUERIES, golden, got):
        assert n == g, f"query {q!r} diverged from the pre-index resolver"


def test_index_is_reused_per_generation_and_rebuilt_for_a_new_one(monkeypatch):
    nodes = build_corpus()
    client = _install(monkeypatch, nodes)
    builds = []
    real_init = acn._SymbolResolutionIndex.__init__

    def counting_init(self, all_nodes):
        builds.append(threading.current_thread() is threading.main_thread())
        real_init(self, all_nodes)

    monkeypatch.setattr(acn._SymbolResolutionIndex, "__init__", counting_init)
    monkeypatch.setattr(acn, "_SYMBOL_INDEX_SLOT", [])
    for sym in ("helper", "fetch", "duplicate_name"):
        asyncio.run(client.read_uuid_from_symbol(None, sym))
    assert builds == [False]  # one build, in a worker thread
    # A new generation (a new immutable nodes list) rebuilds, and sees the change.
    renamed = [dict(n) for n in nodes]
    for n in renamed:
        if n["id"] == "10000000-0000-0000-0000-000000000004":
            n["note"] = "AST_QUALNAME: fetch_everything\n"
            n["name"] = "ƒ fetch_everything()"
    client = _install(monkeypatch, renamed)
    r = asyncio.run(client.read_uuid_from_symbol(None, "fetch_everything"))
    assert builds == [False, False]
    assert [c["uuid"] for c in r["candidates"]] == ["10000000-0000-0000-0000-000000000004"]
