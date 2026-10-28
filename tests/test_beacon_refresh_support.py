"""U09 C: executable, data-only gesture joins, not activated protected hooks.

Before U09 these interfaces do not exist. Separate alias witnesses reach the
old mapper directly. All sources are quoted rows; reads assert bytes AND mtime.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

import nexus_map_codebase as nmc
from workflowy_mcp import beacon_refresh_support as support


@pytest.fixture(autouse=True)
def _j_counterfactual(monkeypatch):
    from j_counterfactual import install_j_counterfactual
    install_j_counterfactual(monkeypatch)


def _source(language, *, alias=False):
    prefix = {"python": "#", "markdown": "", "shell": "#", "yaml": "#", "js_ts": "//"}[language]
    kind = "span" if language in {"shell", "yaml"} else "ast"
    rows = ["@beacon[", "id=fixture@good,", "slice_labels=kept,", f"kind={kind},",
            ("comments" if alias else "comment") + "=A good block.,", "]"]
    rows = [f"{prefix} {r}".strip() for r in rows]
    if language == "markdown":
        rows = ["<!--", *rows, "-->", "# Good", "Prose."]
    else:
        rows += {"python": ["def good():", "    return 1"],
                 "js_ts": ["function good() { return 1; }"],
                 "shell": ["good() { echo yes; }"], "yaml": ["good: true"]}[language]
    return ("\r\n".join(rows) + "\r\n").encode()


def _state(path):
    return path.read_bytes(), path.stat().st_mtime_ns


@pytest.mark.parametrize("language,suffix", [("python", ".py"), ("markdown", ".md"),
    ("markdown", ".markdown"), ("shell", ".bash"), ("yaml", ".yaml"), ("js_ts", ".js")])
@pytest.mark.parametrize("dry,claim", [(True, None), (True, "claim"), (False, None), (False, "claim")])
def test_preparation_gates_order_one_final_map_and_analysis(tmp_path, monkeypatch, language, suffix, dry, claim):
    """New API witness: optional IO order; one analyzer call per final hash."""
    path = tmp_path / ("source" + suffix)
    path.write_bytes(_source(language, alias=True))
    before = _state(path)
    calls, hashes = [], []
    for name, label in [("repair_markdown_c0_controls_on_disk", "c0"),
                        ("normalize_beacon_comment_field_aliases_in_file", "aliases"),
                        ("normalize_duplicate_ast_beacons_in_file", "groups")]:
        original = getattr(nmc, name)
        def spy(*a, _original=original, _label=label, **kw):
            calls.append(_label)
            return _original(*a, **kw)
        monkeypatch.setattr(nmc, name, spy)
    original_map, original_analyze = nmc.map_source_snapshot, support.analyze_beacon_snapshot
    def map_spy(snapshot, **kw):
        calls.append("map")
        hashes.append(("map", snapshot.sha256))
        return original_map(snapshot, **kw)
    def analyze_spy(snapshot, **kw):
        calls.append("analyze")
        hashes.append(("analyze", snapshot.sha256))
        return original_analyze(snapshot, **kw)
    monkeypatch.setattr(nmc, "map_source_snapshot", map_spy)
    monkeypatch.setattr(support, "analyze_beacon_snapshot", analyze_spy)
    got = support.prepare_beacon_source(path, dry_run=dry, claim_id=claim)
    expected = (["c0"] if language == "markdown" else []) + ["aliases", "groups"] if claim and not dry else []
    assert calls == expected + ["map", "analyze"]
    final_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    assert hashes == [("map", final_hash), ("analyze", final_hash)]
    assert got["snapshot"].raw_bytes == path.read_bytes()
    assert got["file_sha256"] == final_hash
    assert all(f["observed"]["file_sha256"] == final_hash for f in got["findings"])
    assert got["status"] == "prepared"
    assert got["changed"] is bool(claim and not dry)
    if dry or not claim:
        assert _state(path) == before and got["events"] == []
    else:
        assert path.read_bytes() == before[0].replace(b"comments=", b"comment=")
        assert any(e["status"] == "committed" and e["before_sha256"] != e["after_sha256"] for e in got["events"])
        assert got["repair_outcomes"][0]["pass"] == expected[0]


def test_many_candidates_do_not_multiply_full_analysis(tmp_path, monkeypatch):
    """533 targets remain one full analysis of one prepared final version."""
    path = tmp_path / "many.py"
    path.write_bytes(b"\n".join(_source("python").replace(b"good", f"good_{i}".encode()) for i in range(533)))
    before = _state(path)
    seen = []
    original = support.analyze_beacon_snapshot
    def spy(snapshot, **kwargs):
        seen.append(snapshot.sha256)
        return original(snapshot, **kwargs)
    monkeypatch.setattr(support, "analyze_beacon_snapshot", spy)
    result = support.prepare_beacon_source(path)
    assert seen == [result["file_sha256"]]
    assert len(result["analysis_inputs"]["attachment_index"]["anchors"]) == 533
    assert _state(path) == before


def test_br02_external_save_after_hygiene_plan_is_one_fresh_map_and_check(tmp_path, monkeypatch):
    """CAS refusal keeps external bytes; optional hygiene is never rerun."""
    path = tmp_path / "race.py"
    path.write_bytes(_source("python", alias=True))
    external = _source("python").replace(b"good", b"external")
    original_commit = nmc.commit_beacon_edit_plan
    commits, groups, snapshots = [], [], []
    def external_save(plan, **kwargs):
        commits.append(plan.snapshot.sha256)
        path.write_bytes(external)
        return original_commit(plan, **kwargs)
    monkeypatch.setattr(nmc, "commit_beacon_edit_plan", external_save)
    monkeypatch.setattr(nmc, "normalize_duplicate_ast_beacons_in_file", lambda *a: groups.append(a))
    original_read = support.read_source_snapshot
    def read(*a):
        snap = original_read(*a)
        snapshots.append(snap)
        return snap
    monkeypatch.setattr(support, "read_source_snapshot", read)
    result = support.prepare_beacon_source(path, claim_id="owned")
    after = _state(path)
    assert len(commits) == 1 and groups == [] and len(snapshots) == 1
    assert result["repair_outcomes"][0]["error_kind"] == "source_changed_since_plan"
    assert result["events"] == [] and result["changed"] is False
    assert result["snapshot"].raw_bytes == external == after[0]
    assert any("external" in n["name"] for n in result["map"]["children"])
    assert all(f["observed"]["file_sha256"] == hashlib.sha256(external).hexdigest() for f in result["findings"])
    assert _state(path) == after
    neighbour = tmp_path / "neighbour.py"
    neighbour.write_bytes(_source("python"))
    safe = support.prepare_beacon_source(neighbour)
    assert safe["status"] == "prepared" and safe["map"]["children"]


def test_check_exception_is_data_beside_valid_map_and_safe_neighbour(tmp_path, monkeypatch):
    """An advisory analyzer bug cannot veto its independently valid map."""
    paths = [tmp_path / n for n in ("broken_check.py", "safe.py")]
    for path in paths:
        path.write_bytes(_source("python"))
    before = [_state(p) for p in paths]
    original = support.analyze_beacon_snapshot
    def analyze(snapshot, **kwargs):
        if snapshot.canonical_path.endswith("broken_check.py"):
            raise ArithmeticError("one analyzer")
        return original(snapshot, **kwargs)
    monkeypatch.setattr(support, "analyze_beacon_snapshot", analyze)
    bad, good = [support.prepare_beacon_source(p) for p in paths]
    assert bad["status"] == good["status"] == "prepared"
    assert [f["code"] for f in bad["findings"]] == ["check_failed"]
    assert [n["name"] for n in bad["map"]["children"]] == [n["name"] for n in good["map"]["children"]]
    assert bad["map"]["children"][0]["note"] == good["map"]["children"][0]["note"]
    assert support.beacon_refresh_projection(bad)["state"] == "valid"
    assert [_state(p) for p in paths] == before


@pytest.mark.parametrize("failed", ["aliases", "groups"])
def test_optional_hygiene_exception_is_data_and_final_observation_continues(tmp_path, monkeypatch, failed):
    """NE35OC: removing the optional-pass wrap must raise instead of mapping."""
    path = tmp_path / "optional.py"
    path.write_bytes(_source("python"))
    calls, messages = [], []
    for name, label in (("normalize_beacon_comment_field_aliases_in_file", "aliases"),
                        ("normalize_duplicate_ast_beacons_in_file", "groups")):
        original = getattr(nmc, name)
        def run(*args, _label=label, _original=original, **kwargs):
            calls.append(_label)
            if _label == failed:
                raise ValueError("fixture hygiene unavailable")
            return _original(*args, **kwargs)
        monkeypatch.setattr(nmc, name, run)
    original_map, original_analyze = nmc.map_source_snapshot, support.analyze_beacon_snapshot
    def mapped(*args, **kwargs):
        calls.append("map")
        return original_map(*args, **kwargs)
    def analyzed(*args, **kwargs):
        calls.append("analyze")
        return original_analyze(*args, **kwargs)
    monkeypatch.setattr(nmc, "map_source_snapshot", mapped)
    monkeypatch.setattr(support, "analyze_beacon_snapshot", analyzed)
    result = support.prepare_beacon_source(path, claim_id="owned", log_callback=messages.append)
    assert calls == ["aliases", "groups", "map", "analyze"]
    assert result["status"] == "prepared" and result["map"]["children"]
    assert result["findings"] == [] and result["file_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    outcome = next(row for row in result["repair_outcomes"] if row["pass"] == failed)
    assert outcome == {"pass": failed, "success": False, "changed": False,
                       "error": "ValueError: fixture hygiene unavailable", "error_kind": "optional_hygiene_failed"}
    assert len(result["warnings"]) == len(messages) == 1
    assert result["warnings"][0]["message"] == messages[0]
    assert "normalization failed" in messages[0]


def test_parse_break_restore_retains_every_uuid_and_valid_empty_is_distinct(tmp_path):
    """Fake reconcile boundary consumes None, never replaces it with []."""
    path = tmp_path / "syntax.py"
    good = _source("python")
    path.write_bytes(good)
    existing = [{"id": "uuid-good", "name": "good", "children": [{"id": "uuid-child", "children": []}]}]
    path.write_bytes(good + b"def broken(:\n")
    before = _state(path)
    invalid = support.prepare_beacon_source(path)
    policy = support.beacon_refresh_projection(invalid, existing_children=existing)
    assert invalid["status"] == "failed"
    assert policy["state"] == "parse_failed" and policy["preserve_existing"] is True
    assert policy["reconcile_children"] is None
    assert policy["create_allowed"] is policy["delete_allowed"] is False
    assert policy["retained_uuids"] == ["uuid-good", "uuid-child"]
    assert any(f["code"] == "mapper_parse_error" for f in invalid["findings"])
    deletes = [] if policy["preserve_existing"] else [n["id"] for n in existing]
    assert deletes == [] and _state(path) == before
    path.write_bytes(good)
    restored = support.beacon_refresh_projection(support.prepare_beacon_source(path), existing_children=existing)
    assert restored["state"] == "valid" and restored["reconcile_children"]
    # The test double matches the restored stable symbol to its existing UUID.
    matched = {n["name"]: n["id"] for n in existing}
    assert matched == {"good": "uuid-good"} and existing[0]["children"][0]["id"] == "uuid-child"
    path.write_bytes(b"")
    empty = support.beacon_refresh_projection(support.prepare_beacon_source(path), existing_children=existing)
    assert empty["state"] == "valid" and empty["preserve_existing"] is False
    assert empty["reconcile_children"] == [] and empty["delete_allowed"] is True


def test_vue_failed_section_policy_and_reconcile_ambiguities():
    """Known section retains only that subtree; healthy sections remain plans."""
    failed = {"name": "script", "note": "VUE_SECTION: script_element\nVUE_START_TAG: <script>",
              "children": [{"name": "⚠️ Parse Error", "note": "bad script", "children": []}]}
    healthy = {"name": "style", "note": "VUE_SECTION: style_element\nVUE_START_TAG: <style>", "children": []}
    existing = [{**copy.deepcopy(failed), "id": "section-script", "children": [{"id": "script-symbol", "children": []}]},
                {**healthy, "id": "section-style"}]
    report = {"ambiguous": [{"beacon_id": "twins", "candidates": ["a", "b"]}], "ambiguous_count": 1}
    prepared = {"map": {"children": [failed, healthy], "_beacon_reconcile_report": report}, "findings": [], "warnings": []}
    got = support.beacon_refresh_projection(prepared, existing_children=existing)
    assert got["state"] == "partial_section" and got["preserve_existing"] is False
    assert got["reconcile_children"] == [healthy]
    assert got["retained_uuids"] == ["section-script", "script-symbol"]
    assert got["sections"][0]["existing_uuid"] == "section-script"
    assert got["reconcile_report"] == report and got["ambiguous_count"] == 1
    uncertain = support.beacon_refresh_projection(prepared, existing_children=existing + [existing[0]])
    assert uncertain["state"] == "parse_failed" and uncertain["reconcile_children"] is None


def test_vue_section_parser_exception_keeps_healthy_section_and_existing_uuid(tmp_path, monkeypatch):
    """Fault the existing section parser, not a replacement mapper or grammar."""
    path = tmp_path / "sections.vue"
    path.write_bytes(b"<script>\nfunction first() {}\n</script>\n<script setup>\nfunction second() {}\n</script>\n")
    original = nmc.JsTsOutlineContext.from_source
    def fail_one(**kwargs):
        if "first" in kwargs["source_text"]:
            raise ValueError("one section parser")
        return original(**kwargs)
    monkeypatch.setattr(nmc.JsTsOutlineContext, "from_source", fail_one)
    before = _state(path)
    prepared = support.prepare_beacon_source(path)
    old = [{"id": "first-section", "note": "VUE_SECTION: script_element\nVUE_START_TAG: <script>",
            "children": [{"id": "first-symbol"}]}]
    policy = support.beacon_refresh_projection(prepared, existing_children=old)
    assert prepared["status"] == "partial_section"
    assert policy["state"] == "partial_section" and policy["retained_uuids"] == ["first-section", "first-symbol"]
    assert len(policy["reconcile_children"]) == 1
    assert policy["reconcile_children"][0]["children"][0]["ast_name"] == "second"
    assert any(f["code"] == "mapper_parse_error" for f in prepared["findings"])
    assert _state(path) == before


def test_missing_outline_dependency_is_not_an_empty_authoritative_file(tmp_path, monkeypatch):
    path = tmp_path / "missing.js"
    path.write_bytes(_source("js_ts"))
    safe = tmp_path / "safe.py"
    safe.write_bytes(_source("python"))
    before = [_state(p) for p in (path, safe)]
    monkeypatch.setattr(nmc, "_HAVE_TREE_SITTER", False)
    unavailable, neighbour = [support.prepare_beacon_source(p) for p in (path, safe)]
    assert support.beacon_refresh_projection(unavailable)["preserve_existing"]
    assert support.beacon_refresh_projection(neighbour)["state"] == "valid"
    assert [_state(p) for p in (path, safe)] == before


@pytest.mark.parametrize("suffix,language", [(".markdown", "markdown"), (".bash", "shell"), (".yaml", "yaml")])
def test_extension_aliases_directory_ignore_and_explicit_target(tmp_path, suffix, language):
    """Direct old mapper witness: aliases used to map as generic leaves."""
    path = tmp_path / ("ignored" + suffix)
    path.write_bytes(_source(language))
    safe = tmp_path / ("safe" + suffix)
    safe.write_bytes(_source(language))
    (tmp_path / ".nexusignore").write_text("ignored*\n", encoding="utf-8")
    before = [_state(p) for p in (path, safe)]
    folder = nmc.map_codebase(str(tmp_path))
    assert len(folder["children"]) == 1 and folder["children"][0]["children"]
    assert nmc.map_codebase(str(path))["children"]
    assert support.prepare_beacon_source(path)["map"]["children"]
    assert [_state(p) for p in (path, safe)] == before


def test_br09_non_utf8_markdown_preserves_bytes_mtime_and_existing_nodes(tmp_path):
    """C03 stays typed unreadable; no header workaround activates deletion."""
    corpus = json.loads(Path(__file__).with_name("beacon_robustness_corpus").joinpath("area6/area6_inputs.json").read_text())
    raw = base64.b64decode(corpus["other_inputs"]["c0_case_bytes_base64"]["C03_latin1.md"])
    path = tmp_path / "latin.md"
    path.write_bytes(raw)
    before = _state(path)
    prepared = support.prepare_beacon_source(path, claim_id="owned")
    policy = support.beacon_refresh_projection(prepared, existing_children=[{"id": "retained"}])
    assert prepared["error_kind"] == "not_utf8"
    assert [f["code"] for f in prepared["findings"]] == ["file_unreadable"]
    assert policy["preserve_existing"] and policy["reconcile_children"] is None
    assert policy["retained_uuids"] == ["retained"]
    assert "LINE COUNT" not in prepared["map"]["note"]
    assert _state(path) == before
    neighbour = tmp_path / "safe.md"
    neighbour.write_bytes(_source("markdown"))
    assert support.prepare_beacon_source(neighbour)["status"] == "prepared"


@pytest.mark.parametrize("raw", [b"unchanged\r\n", b"\xff\x00binary"])
def test_unsupported_remains_generic_map_with_visible_skip(tmp_path, raw):
    path = tmp_path / "data.txt"
    path.write_bytes(raw)
    before = _state(path)
    result = support.prepare_beacon_source(path, claim_id="owned")
    assert result["status"] == "skipped" and result["skip_reason"] == "unsupported_extension"
    assert result["map"]["children"] == [] and result["repair_outcomes"] == []
    assert support.beacon_refresh_projection(result)["state"] == "valid"
    # An unsupported type is how the system works, never a warning (Dan, 2026-10-28).
    assert result["warnings"] == [] and _state(path) == before


def test_aggregation_complete_cap_status_and_committed_phase8_failure():
    """Preview cannot erase current rows, historic events, or successful work."""
    findings = [{"code": "duplicate_id", "finding_id": str(i)} for i in range(250)]
    events = [{"operation": "actual_repair", "ordinal": i} for i in range(250)]
    rows = [{"file": "one.py", "status": "completed", "changed": True, "findings": findings, "events": events},
            {"file": "two.py", "status": "failed", "error": "unavailable", "new_file": True},
            {"file": "three.py", "status": "completed"}]
    result = support.beacon_job_outcome(support.aggregate_beacon_outcomes(rows))
    assert result["status"] == "completed_with_warnings" and result["partial_success"] is True
    assert (result["successful"], result["failed"], result["skipped"]) == (2, 1, 0)
    assert result["files"][1]["error"] == "unavailable" and result["files"][1]["new_file"] is True
    assert (result["findings_total"], len(result["findings"]), result["findings_truncated"]) == (250, 200, 50)
    assert result["findings_by_code"] == {"duplicate_id": 250}
    assert result["files"][0]["findings"] == findings and result["events"] == events
    assert result["files"][0]["check_target"]["params"]["file_path"] == "one.py"
    assert support.beacon_job_outcome(support.aggregate_beacon_outcomes(rows[1:2]))["status"] == "failed"
    for kwargs in ({"cancelled": True}, {"infrastructure_error": {"error": "run-wide"}}):
        failed = support.beacon_job_outcome(support.aggregate_beacon_outcomes(rows), **kwargs)
        assert failed["status"] == "failed" and failed["files_changed"] == ["one.py"]
    published = support.beacon_job_outcome(support.aggregate_beacon_outcomes(rows[:1]),
                                           phase8={"success": False, "error": "refresh failed", "warnings": [{"type": "seed_ambiguity"}]})
    assert published["status"] == "completed_with_warnings" and published["partial_success"]
    assert published["files_changed"] == ["one.py"] and published["files"][0]["changed"]
    assert published["phase8"]["success"] is False and published["warnings_total"] == 2
    assert published["warnings_by_type"] == {"phase8_refresh_failed": 1, "seed_ambiguity": 1}
    # Error severity without a work failure is never a failed refresh.
    assert support.beacon_job_outcome(support.aggregate_beacon_outcomes(rows[:1]))["status"] != "failed"


def test_phase8_current_findings_replace_old_version_without_losing_historic_events():
    """R6 full rows, not a capped preview, cross the terminal boundary once."""
    old = [{"code": "duplicate_id", "finding_id": "old"}]
    fresh = [{"code": "orphan_close", "finding_id": str(i)} for i in range(250)]
    aggregate = support.aggregate_beacon_outcomes([
        {"file": "published.md", "status": "committed", "changed": True, "findings": old,
         "events": [{"operation": "published"}], "omissions": [{"operation": "omitted"}],
         "relocations": [{"operation": "relocated"}]}])
    phase8 = {"success": True, "files": [{"file": "published.md", "findings": fresh,
               "warnings": [{"type": "refresh_warning"}]}]}
    got = support.beacon_job_outcome(aggregate, phase8=phase8)
    assert got["findings_total"] == 250 and got["findings_by_code"] == {"orphan_close": 250}
    assert got["findings_truncated"] == 50 and got["files"][0]["findings"] == fresh
    assert got["files_changed"] == ["published.md"] and len(got["events"]) == 3
    assert got["warnings_by_type"] == {"refresh_warning": 1}
    # An explicit empty final set clears old recomputable rows, never events.
    phase8["files"][0]["findings"] = []
    cleared = support.beacon_job_outcome(aggregate, phase8=phase8)
    assert cleared["findings_total"] == 0 and cleared["events"] == got["events"]
