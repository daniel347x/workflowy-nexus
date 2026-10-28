"""U05 P: complete stateless listing and one-file failure containment.

All source fixtures are quoted lines in tmp_path, never the real repository.
Every check runs with writer traps and the existing write guard; observations
include raw bytes and mtimes, even on the failure and empty-page paths.
"""
from __future__ import annotations

import builtins
from contextlib import contextmanager
import hashlib
import json
import os

import pytest

from test_beacon_check_engine import _FORBIDDEN, _install_write_guard
import nexus_map_codebase as nmc
from workflowy_mcp import beacon_integrity as bi
from workflowy_mcp.client import api_client_nexus as acn


def _source(start, count):
    lines = []
    for i in range(start, start + count):
        lines.extend([
            "# @beacon[", f"#   id=paging@symbol_{i:04d},",
            "#   slice_labels=paging,", "#   kind=ast,", "# ]",
            f"def symbol_{i:04d}():", "    return 1", "",
        ])
    return "\n".join(lines)


def _scope(tmp_path, total, file_count=16):
    entries = []
    start = 0
    for i in range(min(total, file_count)):
        count = total // file_count + (i < total % file_count)
        path = tmp_path / f"part_{i:02d}.py"
        path.write_text(_source(start, count), encoding="utf-8")
        start += count
        entries.append({"abs_path": str(path), "display_path": path.name, "node_id": f"file-{i}"})
    assert start == total
    return entries


def _bytes_and_mtimes(entries):
    return {e["abs_path"]: (open(e["abs_path"], "rb").read(), os.stat(e["abs_path"]).st_mtime_ns)
            if os.path.isfile(e["abs_path"]) else None for e in entries}


@contextmanager
def _guard(monkeypatch):
    entered = []

    def trap(*args, **kwargs):
        entered.append((args, kwargs))
        raise AssertionError("check entered a forbidden writer")

    with monkeypatch.context() as m:
        _install_write_guard(m)
        for name in _FORBIDDEN + tuple(n for n in dir(nmc) if n.startswith("update_beacon_from_node")):
            m.setattr(nmc, name, trap)
        m.setattr(acn, "_compute_beacon_write_plan", trap)
        yield
    assert entered == []


def _run(entries, monkeypatch, **kwargs):
    before = _bytes_and_mtimes(entries)
    with _guard(monkeypatch):
        report = bi.run_beacon_check(entries, **kwargs)
    assert _bytes_and_mtimes(entries) == before
    return report


def _order(row):
    return (bi.SEVERITY_ORDER.index(row["severity"]), row["file"], row["line"], row["code"], row["finding_id"])


@pytest.mark.parametrize("total,page_size", [(250, 200), (1928, 300)])
def test_pages_concatenate_without_loss_at_curation_scale(tmp_path, monkeypatch, total, page_size):
    """Before P: offset is unsupported and the capped suffix is unreachable."""
    entries = _scope(tmp_path, total)
    full = _run(entries, monkeypatch, min_severity="info", max_rows=total + 1)
    assert len(full["rows"]) == full["eligible_total"] == total
    assert {r["code"] for r in full["rows"]} == {"empty_comment"}
    assert full["rows"] == sorted(full["rows"], key=_order)
    combined = []
    offset = 0
    while True:
        page = _run(list(reversed(entries)), monkeypatch, min_severity="info", max_rows=page_size, offset=offset)
        assert page["eligible_total"] == total
        assert page["scope_fingerprint"] == full["scope_fingerprint"]
        assert page["counts"] == full["counts"] and page["summary_line"] == full["summary_line"]
        assert len(page["rows"]) == min(page_size, total - offset)
        remaining = total - offset - len(page["rows"])
        assert page["rows_truncated"] == remaining
        assert page["has_more"] is (remaining > 0)
        assert page["next_offset"] == (offset + len(page["rows"]) if remaining else None)
        combined.extend(page["rows"])
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert combined == full["rows"]
    assert len({r["finding_id"] for r in combined}) == total
    assert sum(c["total"] for c in full["counts"]["by_file"].values()) == total
    for beyond in (total, total + 99, 10**30):
        page = _run(entries, monkeypatch, min_severity="info", offset=beyond, max_rows=10**30)
        assert page["rows"] == [] and page["eligible_total"] == total
        assert page["rows_truncated"] == 0 and page["has_more"] is False and page["next_offset"] is None


def test_filters_and_missing_ids_do_not_change_aggregates(tmp_path, monkeypatch):
    """Before P: codes/ids are not accepted; an absent identity has no answer."""
    entries = _scope(tmp_path, 3, file_count=3)
    full = _run(entries, monkeypatch, min_severity="info")
    ids = [r["finding_id"] for r in full["rows"]]
    missing = "bf1:" + "0" * 64
    cases = [
        ({"min_severity": "error"}, []),
        ({"codes": ["empty_comment"], "finding_ids": [ids[1], missing]}, [full["rows"][1]]),
        ({"codes": ["unknown_kind"], "finding_ids": [ids[0], missing]}, []),
        ({"codes": []}, []), ({"finding_ids": []}, []),
        ({"finding_ids": [ids[0], ids[0], missing, missing], "offset": 1}, []),
    ]
    for filters, expected in cases:
        report = _run(entries, monkeypatch, **{"min_severity": "info", **filters})
        assert report["rows"] == expected
        assert report["counts"] == full["counts"] and report["summary_line"] == full["summary_line"]
        assert report["scope_fingerprint"] == full["scope_fingerprint"]
        assert report["missing_finding_ids"] == ([missing] if missing in filters.get("finding_ids", []) else [])
        assert report["has_more"] is False and report["next_offset"] is None
    hidden = _run(entries, monkeypatch, min_severity="error", finding_ids=[ids[0]])
    assert hidden["rows"] == [] and hidden["missing_finding_ids"] == []


def test_global_order_uses_every_tie_breaker(tmp_path, monkeypatch):
    """Before P: file/line precede severity, unlike the complete global order."""
    entries = _scope(tmp_path, 2, file_count=2)

    def synthetic(snapshot, **kwargs):
        file = kwargs["display_path"]
        return [bi._row(file, line, code, None, kind, snapshot=snapshot, kind=kind)
                for line, code, kind in [
                    (4, "empty_comment", "info"), (3, "unknown_kind", "b"),
                    (3, "missing_id", "b"), (3, "missing_id", "a"),
                    (2, "unknown_kind", "a"), (1, "unknown_field_key", "warning"),
                ]]

    monkeypatch.setattr(bi, "analyze_beacon_snapshot", synthetic)
    report = _run(list(reversed(entries)), monkeypatch, min_severity="info", max_rows=100)
    assert len(report["rows"]) == 12
    assert report["rows"] == sorted(report["rows"], key=_order)
    assert report["rows"][0]["file"] == "part_00.py" and report["rows"][0]["line"] == 2
    assert report["rows"][-1]["severity"] == "info"


def test_one_analyzer_exception_costs_one_row_and_retains_observation(tmp_path, monkeypatch):
    """Before P: one analyzer exception aborts the entire folder report."""
    entries = _scope(tmp_path, 3, file_count=3)
    baseline = _run(entries, monkeypatch, min_severity="info")
    real_analyze = bi.analyze_beacon_snapshot

    def broken_middle(snapshot, **kwargs):
        if snapshot.canonical_path == os.path.realpath(entries[1]["abs_path"]):
            raise RuntimeError("injected middle-file failure")
        return real_analyze(snapshot, **kwargs)

    monkeypatch.setattr(bi, "analyze_beacon_snapshot", broken_middle)
    report = _run(entries, monkeypatch, min_severity="info")
    failed = [r for r in report["rows"] if r["code"] == "check_failed"]
    assert len(failed) == 1
    row = failed[0]
    assert row["file"] == entries[1]["display_path"] and row["line"] == 0
    assert row["detail"]["exception"] == "RuntimeError: injected middle-file failure"
    assert row["observed"]["file_sha256"] == hashlib.sha256(open(entries[1]["abs_path"], "rb").read()).hexdigest()
    assert row["observed"]["canonical_file"] == os.path.realpath(entries[1]["abs_path"])
    assert report["scope_fingerprint"] == baseline["scope_fingerprint"]
    assert [r for r in report["rows"] if r["code"] != "check_failed"] == [
        r for r in baseline["rows"] if r["file"] != entries[1]["display_path"]]
    assert report["counts"]["by_file"][entries[1]["display_path"]] == {
        "total": 1, "by_severity": {"error": 1, "warning": 0, "info": 0}}


def test_exception_before_observation_has_explicit_unobserved_identity(tmp_path, monkeypatch):
    """Before P: unexpected reader failure also aborts the folder."""
    entries = _scope(tmp_path, 3, file_count=3)
    baseline = _run(entries, monkeypatch, min_severity="info")
    real_read = bi.read_source_snapshot

    def reader(path):
        if path == entries[1]["abs_path"]:
            raise RuntimeError("reader defect before snapshot")
        return real_read(path)

    monkeypatch.setattr(bi, "read_source_snapshot", reader)
    report = _run(entries, monkeypatch, min_severity="info")
    failed = next(r for r in report["rows"] if r["code"] == "check_failed")
    assert failed["observed"]["canonical_file"] == entries[1]["abs_path"]
    assert failed["observed"]["file_sha256"] is None
    assert failed["observed"]["unreadable_identity"] == {"state": "unobserved"}
    assert failed["detail"]["exception"] == "RuntimeError: reader defect before snapshot"
    assert report["scope_fingerprint"] != baseline["scope_fingerprint"]
    assert [r for r in report["rows"] if r["code"] != "check_failed"] == [
        r for r in baseline["rows"] if r["file"] != entries[1]["display_path"]]


def test_vanished_after_collection_is_missing_and_neighbours_continue(tmp_path, monkeypatch):
    """Before P: vanished reads say UTF-8 unreadable, not missing source."""
    entries = _scope(tmp_path, 3, file_count=3)
    nodes = {"root": {"id": "root", "note": f"Root: {tmp_path}", "parent_id": None}}
    nodes.update({e["node_id"]: {"id": e["node_id"], "parent_id": "root", "note": f"Path: {e['display_path']}"}
                  for e in entries})
    with _guard(monkeypatch):
        inventory = bi.collect_mapped_beacon_files(nodes, "root")
    assert len(inventory["files"]) == 3 and inventory["missing"] == []
    baseline = _run(inventory["files"], monkeypatch, min_severity="info")
    os.unlink(entries[1]["abs_path"])  # external checkout race, outside the check/write guard
    report = _run(inventory["files"], monkeypatch, min_severity="info")
    row = next(r for r in report["rows"] if r["file"] == entries[1]["display_path"])
    assert row["code"] == "file_missing" and row["line"] == 0
    assert "disappeared" in row["message"] and row["detail"]["exception"].startswith("FileNotFoundError:")
    assert row["observed"]["file_sha256"] is None and row["observed"]["unreadable_identity"]["state"] == "missing"
    assert report["counts"]["files_missing"] == 1 and report["counts"]["files_unreadable"] == 0
    assert report["counts"]["files_checked"] == 3
    assert report["scope_fingerprint"] != baseline["scope_fingerprint"]
    assert [r for r in report["rows"] if r["file"] != row["file"]] == [
        r for r in baseline["rows"] if r["file"] != row["file"]]


def test_marker_free_fingerprint_retains_single_binary_read(tmp_path, monkeypatch):
    """Before P: the clean snapshot is discarded; scope has no fingerprint."""
    path = tmp_path / "plain.py"
    path.write_bytes(b"x = 1\r\n")
    entries = [{"abs_path": str(path), "display_path": path.name}]
    calls = []
    real_open = builtins.open

    def spy(file, mode="r", *args, **kwargs):
        if os.fspath(file) == str(path):
            calls.append(mode)
        return real_open(file, mode, *args, **kwargs)

    # Capture only the check, not the test's before/after byte witnesses.
    before = path.read_bytes(), path.stat().st_mtime_ns
    with monkeypatch.context() as m:
        m.setattr(builtins, "open", spy)
        with _guard(m):
            report = bi.run_beacon_check(entries, min_severity="info")
    assert calls == ["rb"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert report["rows"] == [] and report["eligible_total"] == 0
    expected_observations = [{"canonical_file": str(path.resolve()), "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]
    canonical = json.dumps(expected_observations, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert report["scope_fingerprint"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert report["counts"]["by_file"][path.name] == {"total": 0, "by_severity": {"error": 0, "warning": 0, "info": 0}}
    stable = _run(entries, monkeypatch, offset=100)
    assert stable["scope_fingerprint"] == report["scope_fingerprint"]
    path.write_bytes(b"x = 2\r\n")
    changed = _run(entries, monkeypatch)
    assert changed["rows"] == [] and changed["scope_fingerprint"] != report["scope_fingerprint"]
    other = tmp_path / "renamed.py"
    path.rename(other)
    renamed = _run([{"abs_path": str(other), "display_path": path.name}], monkeypatch)
    assert renamed["scope_fingerprint"] != changed["scope_fingerprint"]


def test_unreadable_identity_is_honest_and_fingerprinted(tmp_path, monkeypatch):
    """Control: C already reports decode errors; P retains them in scope data."""
    entries = _scope(tmp_path, 3, file_count=3)
    with open(entries[1]["abs_path"], "wb") as stream:
        stream.write(b"\xffbad utf8\n")
    report = _run(entries, monkeypatch, min_severity="info")
    row = next(r for r in report["rows"] if r["code"] == "file_unreadable")
    assert row["observed"]["file_sha256"] is None
    assert row["observed"]["unreadable_identity"]["exception_type"] == "UnicodeDecodeError"
    assert len(report["rows"]) == 3
    with open(entries[1]["abs_path"], "wb") as stream:
        stream.write(b"\xfedifferent invalid bytes\n")
    changed = _run(entries, monkeypatch, min_severity="info")
    assert changed["scope_fingerprint"] != report["scope_fingerprint"]
    assert [r for r in changed["rows"] if r["code"] != "file_unreadable"] == [
        r for r in report["rows"] if r["code"] != "file_unreadable"]


def test_folder_check_quiet_but_general_mapping_keeps_ignore_log(tmp_path, monkeypatch, capsys):
    """CF-01 control: C snapshot input is already quiet; no global logger edit."""
    entries = _scope(tmp_path, 3, file_count=3)
    (tmp_path / ".nexusignore").write_text("ignored.py\n", encoding="utf-8")
    nodes = {"root": {"id": "root", "note": f"Root: {tmp_path}", "parent_id": None}}
    nodes.update({e["node_id"]: {"id": e["node_id"], "parent_id": "root", "note": f"Path: {e['display_path']}"}
                  for e in entries})
    capsys.readouterr()
    with _guard(monkeypatch):
        inventory = bi.collect_mapped_beacon_files(nodes, "root")
    report = _run(inventory["files"], monkeypatch, min_severity="info", repo_root=str(tmp_path))
    assert len(report["rows"]) == 3
    captured = capsys.readouterr()
    assert "Using .nexusignore patterns" not in captured.out + captured.err
    with _guard(monkeypatch):
        nmc.map_codebase(str(tmp_path), repo_root=str(tmp_path))
    captured = capsys.readouterr()
    assert "[NEXUS MAP] Using .nexusignore patterns" in captured.err
