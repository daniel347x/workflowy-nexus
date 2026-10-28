"""Remediation-1/2: bounded shared-analysis cost without changing source results.

The wall-clock budgets run in the full tier's serial timing tail. The
unmarked witnesses count actual lexer/range work and verify mutation/thread
isolation; they do not depend on machine speed or any external source copy.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import shutil
import threading
import time
from uuid import NAMESPACE_URL, uuid5

import pytest

import beacon_source as source
import nexus_map_codebase as nmc
from test_beacon_batch_local_admission import _Client, _run, modes, setup_batch, srv
from test_beacon_mutation_plan_seam import PY_BEACONED, PY_BEACON_NOTE, _cache, _plan


@pytest.mark.timing
def test_server_outline_cost_budget(tmp_path, capsys):
    path = tmp_path / "server.py"
    shutil.copyfile(Path(srv.__file__), path)
    before = path.read_bytes(), path.stat().st_mtime_ns
    started = time.perf_counter()
    outline = nmc.parse_file_outline(str(path))
    elapsed = time.perf_counter() - started
    with capsys.disabled():
        print("BEACON_COST " + json.dumps({"operation": "parse_file_outline", "seconds": elapsed,
              "budget_s": 4.0, "source_sha256": hashlib.sha256(before[0]).hexdigest()}), flush=True)
    assert outline and (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert elapsed <= 4.0, f"parse_file_outline(server.py copy): {elapsed:.3f}s > 4.0s"


@pytest.mark.timing
def test_server_three_item_batch_cost_budget(setup_batch, tmp_path, monkeypatch, capsys):
    path = tmp_path / "server.py"
    shutil.copyfile(Path(srv.__file__), path)
    names = ["build_beacon_registry", "_beacon_fix_impl", "_read_text_snippet_impl"]
    ids = {name: str(uuid5(NAMESPACE_URL, str(path) + name)) for name in names}
    nodes = modes._pipeline_nodes(path, [(uid, name, f"AST_QUALNAME: {name}\n") for name, uid in ids.items()])
    client = _Client(nodes=nodes, candidates=ids)
    monkeypatch.setattr(srv, "get_client", lambda: client)
    builds = []
    builder = nmc._beacon_attachment_outline
    def record(snapshot, language):
        builds.append((snapshot.sha256, snapshot.canonical_path, language))
        return builder(snapshot, language)
    monkeypatch.setattr(nmc, "_beacon_attachment_outline", record)
    started = time.perf_counter()
    result = _run([{"symbol": name, "tags": ["audit-probe"]} for name in names])
    elapsed = time.perf_counter() - started
    statuses = [row["status"] for row in result["results"]]
    with capsys.disabled():
        print("BEACON_COST " + json.dumps({"operation": "tags.ensure_3", "seconds": elapsed,
              "budget_s": 30.0, "statuses": statuses, "selection_builds": len(builds),
              "final_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}), flush=True)
    assert statuses == ["committed"] * 3
    assert elapsed <= 30.0, f"3-item tags.ensure(server.py copy): {elapsed:.3f}s > 30s"


@pytest.mark.timing
def test_server_ten_item_batch_cost_budget(setup_batch, tmp_path, monkeypatch, capsys):
    path = tmp_path / "server.py"
    shutil.copyfile(Path(srv.__file__), path)
    names = ["get_client", "_ws_cache_boundary", "get_ws_connection", "get_and_clear_ws_close_reason",
             "get_ws_connection_with_grace_wait", "_send_ws_json_best_effort", "_cache_refresh_skip_file",
             "_cache_refresh_skip_until_epoch", "_cache_refresh_should_skip", "_cache_refresh_remaining_seconds"]
    ids = {name: str(uuid5(NAMESPACE_URL, str(path) + name)) for name in names}
    nodes = modes._pipeline_nodes(path, [(uid, name, f"AST_QUALNAME: {name}\n") for name, uid in ids.items()])
    client = _Client(nodes=nodes, candidates=ids)
    monkeypatch.setattr(srv, "get_client", lambda: client)
    builds = []
    builder = nmc._beacon_attachment_outline
    def record(snapshot, language):
        builds.append((snapshot.sha256, snapshot.canonical_path, language))
        return builder(snapshot, language)
    monkeypatch.setattr(nmc, "_beacon_attachment_outline", record)
    started = time.perf_counter()
    result = _run([{"symbol": name, "tags": ["audit-probe"]} for name in names])
    elapsed = time.perf_counter() - started
    statuses = [row["status"] for row in result["results"]]
    with capsys.disabled():
        print("BEACON_COST " + json.dumps({"operation": "tags.ensure_10", "seconds": elapsed,
              "per_item_s": elapsed / 10, "budget_s": 75.0, "statuses": statuses,
              "selection_builds": len(builds),
              "final_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}), flush=True)
    assert statuses == ["committed"] * 10
    assert elapsed <= 75.0, f"10-item tags.ensure(server.py copy): {elapsed:.3f}s > 75s"


@pytest.mark.parametrize("mode", ["ensure_beacons", "remove_beacon_tags"])
def test_committing_five_item_batch_builds_at_most_once_per_version(setup_batch, monkeypatch, capsys, mode):
    build, _ = setup_batch
    names = [f"f{i}" for i in range(5)]
    client, _, _ = build(names)
    calls = []
    builder = nmc._beacon_attachment_outline
    def record(snapshot, language):
        calls.append((snapshot.sha256, snapshot.canonical_path, language))
        return builder(snapshot, language)
    monkeypatch.setattr(nmc, "_beacon_attachment_outline", record)
    tags = ["new"] if mode == "ensure_beacons" else ["old"]
    result = _run([{"symbol": name, "tags": tags} for name in names], mode=mode)
    with capsys.disabled():
        print("BEACON_BUILDS " + json.dumps({"mode": mode, "items": 5, "builds": len(calls),
              "statuses": [r["status"] for r in result["results"]]}), flush=True)
    assert [r["status"] for r in result["results"]] == ["committed"] * 5
    assert len(client.preflight_calls) == len(client.plan_calls) == len(client.commit_calls) == 5
    assert len(calls) <= 6, calls


def test_selection_memo_returns_isolated_whole_structures_for_each_content_key(tmp_path, monkeypatch):
    snapshot = source.source_snapshot_from_bytes(("\n".join([
        "# @beacon[", "# id=memo@f,", "# kind=ast,", "# slice_labels=old,", "# ]",
        "def f():", "    return 1", ""]) ).encode(), str(tmp_path / "memo.py"))
    calls = []
    builder = nmc._beacon_attachment_outline
    def record(current, language):
        calls.append((current.sha256, current.canonical_path, language))
        return builder(current, language)
    monkeypatch.setattr(nmc, "_beacon_attachment_outline", record)
    first = nmc._beacon_selection_structures(snapshot, "python")
    expected = copy.deepcopy(first)
    first["records"][0]["fields"]["id"] = "changed"
    first["beacons"][0]["comment"] = "changed"
    first["outline"][0]["note"] = "changed"
    first["outline"].append({"children": [{"name": "changed"}]})
    first["language"] = "changed"
    clone = source.source_snapshot_from_bytes(snapshot.raw_bytes, snapshot.canonical_path)
    assert nmc._beacon_selection_structures(clone, "python") == expected
    changed = source.source_snapshot_from_bytes(snapshot.raw_bytes + b"value = 2\n", snapshot.canonical_path)
    elsewhere = source.source_snapshot_from_bytes(snapshot.raw_bytes, str(tmp_path / "elsewhere.py"))
    for current, language in ((changed, "python"), (elsewhere, "python"), (snapshot, "js_ts")):
        value = nmc._beacon_selection_structures(current, language)
        assert nmc._beacon_selection_structures(current, language) == value
    assert len(calls) == len(set(calls)) == 4, calls


def test_selection_memo_preserves_aliases_in_one_deepcopy_and_isolates_first_return(tmp_path, monkeypatch):
    snapshot = source.source_snapshot_from_bytes(b"value = 1\n", str(tmp_path / "aliases.py"))
    shared = {"fields": {"id": "own"}, "children": [{"name": "own"}]}
    calls = []
    monkeypatch.setattr(nmc, "_beacon_edit_records", lambda *_: [shared])
    monkeypatch.setattr(nmc, "_parse_snapshot_beacons_for_language", lambda *_: ([], [shared]))
    def outline(*_):
        calls.append(1)
        return [shared]
    monkeypatch.setattr(nmc, "_beacon_attachment_outline", outline)
    first = nmc._beacon_selection_structures(snapshot, "python")
    assert first["records"][0] is first["beacons"][0] is first["outline"][0]
    first["outline"][0]["children"][0]["name"] = "changed"
    second = nmc._beacon_selection_structures(snapshot, "python")
    assert second["outline"][0]["children"][0]["name"] == "own"
    assert second["records"][0] is second["beacons"][0] is second["outline"][0]
    assert calls == [1]


def test_concurrent_selection_calls_build_once_with_independent_returns(tmp_path, monkeypatch):
    snapshot = source.source_snapshot_from_bytes(b"def f():\n    return 1\n", str(tmp_path / "threads.py"))
    builder = nmc._beacon_attachment_outline
    calls = []
    def record(current, language):
        calls.append(current.sha256)
        time.sleep(0.02)
        return builder(current, language)
    monkeypatch.setattr(nmc, "_beacon_attachment_outline", record)
    start = threading.Barrier(6)
    def read(_):
        start.wait(timeout=10)
        return nmc._beacon_selection_structures(snapshot, "python")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(read, range(6)))
    expected = copy.deepcopy(results[0])
    assert all(result == expected for result in results)
    results[0]["outline"][0]["name"] = "changed"
    assert all(result == expected for result in results[1:])
    assert nmc._beacon_selection_structures(snapshot, "python") == expected
    assert calls == [snapshot.sha256], calls


def test_selection_memo_evicts_least_recent_of_four_keys(tmp_path, monkeypatch):
    snapshots = [source.source_snapshot_from_bytes(f"value = {i}\n".encode(), str(tmp_path / "lru.py"))
                 for i in range(5)]
    calls = []
    builder = nmc._beacon_attachment_outline
    def record(current, language):
        calls.append(current.sha256)
        return builder(current, language)
    monkeypatch.setattr(nmc, "_beacon_attachment_outline", record)
    for snapshot in snapshots[:4]:
        nmc._beacon_selection_structures(snapshot, "python")
    nmc._beacon_selection_structures(snapshots[0], "python")  # Touch eldest.
    nmc._beacon_selection_structures(snapshots[4], "python")  # Evict key 1.
    nmc._beacon_selection_structures(snapshots[0], "python")
    assert calls == [s.sha256 for s in snapshots]
    nmc._beacon_selection_structures(snapshots[1], "python")
    assert calls == [s.sha256 for s in snapshots] + [snapshots[1].sha256]


def test_explicit_outline_keeps_supplied_observation_and_bypasses_memo(tmp_path, monkeypatch):
    snapshot = source.source_snapshot_from_bytes(b"value = 1\n", str(tmp_path / "explicit.py"))
    outline = [{"name": "supplied"}]
    calls = []
    scanner = nmc._beacon_edit_records
    def record(current, language):
        calls.append(current.sha256)
        return scanner(current, language)
    monkeypatch.setattr(nmc, "_beacon_edit_records", record)
    for _ in range(2):
        assert nmc._beacon_selection_structures(snapshot, "python", outline=outline)["outline"] is outline
    assert calls == [snapshot.sha256] * 2


def test_one_plan_does_not_recompute_python_lexical_evidence(tmp_path, monkeypatch):
    path = tmp_path / "one_plan.py"
    path.write_bytes(PY_BEACONED.encode())
    calls = []
    lexer = source.python_lexical_evidence
    def record(snapshot):
        calls.append((snapshot.sha256, snapshot.canonical_path, "python"))
        return lexer(snapshot)
    monkeypatch.setattr(source, "python_lexical_evidence", record)
    plan = _plan(_cache(path, node_note=PY_BEACON_NOTE), ["new"])
    assert plan.success and path.read_bytes() == PY_BEACONED.encode()
    # The original snapshot and its pathless legacy line view are distinct keys.
    assert len(calls) <= 2, calls
    assert calls and len(calls) == len(set(calls)), calls


def test_lexical_memo_returns_mutation_isolated_results_for_each_content_key(tmp_path, monkeypatch):
    snapshot = source.source_snapshot_from_bytes(b"# comment\nvalue = 'text'\n", str(tmp_path / "memo.py"))
    calls = []
    lexer = source.python_lexical_evidence
    def record(current):
        calls.append((current.sha256, current.canonical_path))
        return lexer(current)
    monkeypatch.setattr(source, "python_lexical_evidence", record)
    expected = lexer(snapshot)
    first = source.source_lexical_evidence(snapshot, "python")
    first.append({"start": -10, "end": -1, "certainty": "sentinel"})
    first[0]["start"] = -99
    clone = source.source_snapshot_from_bytes(snapshot.raw_bytes, snapshot.canonical_path)
    assert source.source_lexical_evidence(clone, "python") == expected
    changed = source.source_snapshot_from_bytes(b"# other comment\nvalue = 2\n", snapshot.canonical_path)
    elsewhere = source.source_snapshot_from_bytes(snapshot.raw_bytes, str(tmp_path / "elsewhere.py"))
    for current in (changed, elsewhere):
        assert source.source_lexical_evidence(current, "python") == lexer(current)
        assert source.source_lexical_evidence(current, "python") == lexer(current)
    # Copy isolation must hold on real cache hits, not merely on recomputation.
    assert len(calls) == len(set(calls)) == 3, calls


def test_concurrent_lexical_calls_compute_once_and_return_independent_values(tmp_path, monkeypatch):
    snapshot = source.source_snapshot_from_bytes(b"# concurrent\nvalue = 'text'\n", str(tmp_path / "threads.py"))
    lexer = source.python_lexical_evidence
    expected = lexer(snapshot)
    calls = []
    def record(current):
        calls.append(current.sha256)
        time.sleep(0.02)  # Let simultaneous misses reach the memo, not a timing budget.
        return lexer(current)
    monkeypatch.setattr(source, "python_lexical_evidence", record)
    start = threading.Barrier(6)
    def read(_):
        start.wait(timeout=10)
        return source.source_lexical_evidence(snapshot, "python")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(read, range(6)))
    assert all(result == expected for result in results)
    results[0][0]["start"] = -1
    results[0].append({"certainty": "sentinel"})
    assert all(result == expected for result in results[1:])
    assert source.source_lexical_evidence(snapshot, "python") == expected
    assert calls == [snapshot.sha256], calls


class _CountedRange(dict):
    def __init__(self, counter, **values):
        super().__init__(values)
        self.counter = counter

    def __getitem__(self, key):
        self.counter[0] += 1
        return super().__getitem__(key)


def test_attachment_comment_walk_is_linear_in_lines_and_regions(monkeypatch):
    lines = [f"# ordinary comment {i}" for i in range(400)] + ["value = 1"]
    snapshot = source.source_snapshot_from_bytes(("\n".join(lines) + "\n").encode())
    reads = [0]
    evidence = [_CountedRange(reads, start=snapshot.byte_offsets[i],
                end=snapshot.byte_offsets[i] + len(line), certainty="comment")
                for i, line in enumerate(lines[:-1])]
    # Overlap/nesting must not make the sweep revisit the interior on every line.
    evidence.append(_CountedRange(reads, start=0, end=snapshot.byte_offsets[300], certainty="comment"))
    monkeypatch.setattr(nmc, "source_lexical_evidence", lambda *_: evidence)
    actual = nmc._attachment_comment_lines(snapshot, "python")
    assert actual == [True] * 400 + [False]
    assert reads[0] <= 20 * (len(lines) + len(evidence)), reads[0]


def test_structure_evidence_index_keeps_original_overlap_priority(monkeypatch):
    lines = [f"# @beacon[id=demo@span{i},kind=span]" for i in range(64)]
    snapshot = source.source_snapshot_from_bytes(("\n".join(lines) + "\n").encode())
    reads = [0]
    offsets = [snapshot.byte_offsets[i] + 2 for i in range(len(lines))]
    evidence = [_CountedRange(reads, start=offsets[0], end=offsets[0] + 1, certainty="comment"),
                _CountedRange(reads, start=offsets[1], end=offsets[1] + 1, certainty="literal")]
    evidence.extend(_CountedRange(reads, start=len(snapshot.raw_bytes) + i * 3,
                    end=len(snapshot.raw_bytes) + i * 3 + 1, certainty="comment") for i in range(2000))
    # Sorted-start first-match would wrongly choose these later original entries.
    evidence.extend([_CountedRange(reads, start=0, end=offsets[0] + 1, certainty="literal"),
                     _CountedRange(reads, start=offsets[1] - 1, end=offsets[1] + 1, certainty="comment"),
                     _CountedRange(reads, start=offsets[2], end=offsets[2] + 1, certainty="uncertain")])
    rows = source._beacon_comment_rows
    def unmasked_rows(snapshot, language, start, end, evidence, byte_start, byte_end):
        # Isolate scanner priority from the separate literal row-mask authority.
        return rows(snapshot, language, start, end, (), byte_start, byte_end)
    monkeypatch.setattr(source, "_beacon_comment_rows", unmasked_rows)
    records = source.scan_beacon_structure(snapshot, "python", evidence=evidence)
    assert [r["fields"]["id"] for r in records] == [f"demo@span{i}" for i in range(64) if i != 1]
    assert records[0]["literal_certainty"] == "comment"
    assert records[1]["literal_certainty"] == "uncertain" and records[1]["write_ranges"] is None
    assert reads[0] <= 20 * (len(evidence) + len(lines)), reads[0]
