"""U05 content identity and current-observation witnesses (no persistent store)."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

import beacon_findings as bf
import nexus_map_codebase as nmc
from beacon_source import source_snapshot_from_bytes
from workflowy_mcp import beacon_integrity as bi


def _source(symbol="f", comment=None):
    lines = ["# @beacon[", "#   id=demo@f,", "#   slice_labels=demo,", "#   kind=ast,"]
    if comment is not None:
        lines.append(f"#   comment={comment},")
    return ("\n".join([*lines, "# ]", f"def {symbol}():", "    return 1"]) + "\n").encode()


def _analyze(path, raw, *, display="shown.py", producer="beacon.check"):
    snapshot = source_snapshot_from_bytes(raw, str(path))
    inputs = bi._snapshot_analysis_inputs(snapshot, "python", nmc)
    return bf.analyze_beacon_snapshot(snapshot, **inputs, display_path=display, producer=producer)


def _empty(rows):
    return next(r for r in rows if r["code"] == "empty_comment")


def test_finding_identity_survives_prepended_unrelated_lines_and_pins_current_witness(tmp_path):
    path = tmp_path / "source.py"
    raw = _source()
    before = _empty(_analyze(path, raw))
    after = _empty(_analyze(path, b"\n\n\n" + raw))
    assert before["finding_id"] == after["finding_id"]
    assert before["observed"]["file_sha256"] != after["observed"]["file_sha256"]
    assert before["members"][0]["opener_start_byte"] != after["members"][0]["opener_start_byte"]
    assert before["members"][0]["anchor"]["line"] + 3 == after["members"][0]["anchor"]["line"]
    assert len(before["finding_id"]) == len("bf1:") + 64


@pytest.mark.parametrize("change", ["bytes", "anchor"])
def test_relevant_block_bytes_or_semantic_anchor_change_the_identity(tmp_path, change):
    path = tmp_path / "source.py"
    before = _empty(_analyze(path, _source()))
    raw = _source().replace(b"slice_labels=demo", b"slice_labels=other") if change == "bytes" else _source("g")
    after = _empty(_analyze(path, raw))
    assert before["finding_id"] != after["finding_id"]


def test_display_root_producer_and_path_entry_form_do_not_change_identity(tmp_path):
    path = tmp_path / "source.py"
    path.write_bytes(_source())
    before = bi.check_beacon_file(str(path), display_path="source.py", repo_root=str(tmp_path))
    direct = bi.check_beacon_file(str(path))
    other = _analyze(path, path.read_bytes(), display="some/other/view.py", producer="kit.check")
    assert {r["finding_id"] for r in before} == {r["finding_id"] for r in direct} == {r["finding_id"] for r in other}
    assert {r["producer"] for r in other} == {"kit.check"}


def test_equal_relative_files_in_different_worktrees_are_different_findings(tmp_path):
    left = _empty(_analyze(tmp_path / "one" / "a.py", _source()))
    right = _empty(_analyze(tmp_path / "two" / "a.py", _source()))
    assert left["finding_id"] != right["finding_id"]


def test_windows_and_wsl_spellings_of_the_same_real_file_agree():
    left = _empty(_analyze("E:\\Repo\\Source.py", _source()))
    right = _empty(_analyze("/mnt/e/repo/source.py", _source()))
    assert left["finding_id"] == right["finding_id"]
    assert bf.canonical_beacon_file("/case/A.py") != bf.canonical_beacon_file("/case/a.py")


def test_realpath_alias_uses_the_observation_identity(tmp_path):
    path, alias = tmp_path / "source.py", tmp_path / "alias.py"
    path.write_bytes(_source())
    alias.symlink_to(path)
    assert _empty(bi.check_beacon_file(str(path)))["finding_id"] == _empty(bi.check_beacon_file(str(alias)))["finding_id"]


def test_identical_members_are_explicitly_ambiguous_and_ordinals_are_not_proof(tmp_path):
    source = _empty(_analyze(tmp_path / "a.py", _source()))
    members = [source["members"][0], copy.deepcopy(source["members"][0])]
    row = bf.make_beacon_finding("a.py", 1, "duplicate_id", "demo@f", "Duplicate.",
                                canonical_file=str(tmp_path / "a.py"), file_sha256="a" * 64, members=members)
    assert row["observed"]["identity_ambiguous"] is True
    assert row["observed"]["identical_member_groups"] == [[0, 1]]
    rows = bf.finalize_beacon_findings([copy.deepcopy(source), copy.deepcopy(source)])
    assert len({r["finding_id"] for r in rows}) == 2
    assert [r["observed"]["occurrence_ordinal"] for r in rows] == [0, 1]
    assert all(r["observed"]["identity_ambiguous"] for r in rows)
    assert all(r["observed"]["file_sha256"] == source["observed"]["file_sha256"] for r in rows)


def test_whole_file_failures_use_hash_or_explicit_unreadable_identity(tmp_path):
    kwargs = {"canonical_file": str(tmp_path / "a.py")}
    def row(**extra):
        return bf.make_beacon_finding("a.py", 0, "check_failed", None, "Failed.", **kwargs, **extra)
    assert row(file_sha256="a")["finding_id"] != row(file_sha256="b")["finding_id"]
    assert row(unreadable_identity={"errno": 2})["finding_id"] != row(unreadable_identity={"errno": 13})["finding_id"]
    assert row(file_sha256="a")["fix"]["class"] == "manual"


def test_core_has_no_io_and_does_not_mutate_supplied_inputs(tmp_path, monkeypatch):
    snapshot = source_snapshot_from_bytes(_source(), str(tmp_path / "a.py"))
    inputs = bi._snapshot_analysis_inputs(snapshot, "python", nmc)
    before = copy.deepcopy(inputs)
    def refuse(*args, **kwargs):
        raise AssertionError("pure analysis performed IO")
    monkeypatch.setattr("builtins.open", refuse)
    rows = bf.analyze_beacon_snapshot(snapshot, **inputs)
    assert inputs == before
    assert _empty(rows)["observed"]["file_sha256"] == hashlib.sha256(snapshot.raw_bytes).hexdigest()
    json.dumps(rows, allow_nan=False)
