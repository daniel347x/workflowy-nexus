from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path

import pytest

import nexus_map_codebase
from workflowy_mcp.client import api_client_nexus, workflowy_move_reconcile
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus


@pytest.fixture(autouse=True)
def _j_counterfactual(monkeypatch):
    from j_counterfactual import install_j_counterfactual
    install_j_counterfactual(monkeypatch)

_HYGIENE_FIXTURE = b"\n".join([
    b"# @beacon[", b"#   id=first,", b"#   slice_labels=alpha,", b"#   kind=ast,",
    b"#   comments=legacy alias,", b"# ]", b"# @beacon[", b"#   id=second,",
    b"#   slice_labels=beta,", b"#   kind=ast,", b"#   comment=canonical second,", b"# ]",
    b"def target():", b"    return 1", b"",
])


def _run_file_refresh(
    monkeypatch,
    tmp_path: Path,
    *,
    dry_run: bool,
    carto_claim_id: str | None,
    fixture: bytes = _HYGIENE_FIXTURE,
    suffix: str = ".py",
    existing_children: tuple = (),
) -> tuple[Path, bytes, dict, dict]:
    source_path = tmp_path / ("fixture" + suffix)
    source_path.write_bytes(fixture)
    before = source_path.read_bytes()
    file_node = {
        "id": "FILE",
        "name": source_path.name,
        "note": f"Path: {source_path}",
    }
    observed: dict = {"updates": [], "before_mtime_ns": source_path.stat().st_mtime_ns,
                      "before_note": file_node["note"], "file_node": file_node,
                      "children": list(existing_children)}

    async def fake_export_nodes_impl(_client, node_id=None, **_kwargs):
        assert node_id in (None, "FILE")
        return {"nodes": [dict(file_node)] + [dict(child) for child in existing_children]}

    async def fake_update_node(_self, node_id, req):
        # Record the request and patch the in-memory note so a second run can
        # prove the header update is a no-op. FILE-NOTE HEADER FRESHNESS
        # (Dan 2026) routes its one note-only write through update_node.
        observed["updates"].append(
            {
                "node_id": str(node_id),
                "name": req.name,
                "note": req.note,
                "layoutMode": req.layoutMode,
            }
        )
        if str(node_id) == "FILE" and req.note is not None:
            file_node["note"] = req.note

    async def fake_reconcile_tree(**kwargs):
        observed["source_json"] = kwargs["source_json"]
        observed["dry_run"] = kwargs["dry_run"]
        return {"success": True, "planned": True, "dry_run": kwargs["dry_run"]}

    monkeypatch.setattr(api_client_nexus, "export_nodes_impl", fake_export_nodes_impl)
    monkeypatch.setattr(WorkFlowyClientNexus, "update_node", fake_update_node)
    monkeypatch.setattr(
        WorkFlowyClientNexus,
        "_mark_nodes_export_dirty",
        lambda _self, _node_ids: None,
    )
    monkeypatch.setattr(workflowy_move_reconcile, "reconcile_tree", fake_reconcile_tree)
    monkeypatch.setenv("CARTO_JOB_DIR", str(tmp_path))
    monkeypatch.delenv("CARTO_JOB_FILE", raising=False)

    client = object.__new__(WorkFlowyClientNexus)
    result = asyncio.run(
        client.refresh_file_node_beacons(
            "FILE",
            dry_run=dry_run,
            carto_claim_id=carto_claim_id,
        )
    )
    return source_path, before, result, observed


def test_windsurf_dry_run_keeps_both_hygiene_triggers_byte_pure(monkeypatch, tmp_path):
    def forbidden_normalizer(_source_path):  # pragma: no cover - failure sentinel
        raise AssertionError("dry-run source hygiene attempted a persistent write")

    monkeypatch.setattr(
        nexus_map_codebase,
        "normalize_beacon_comment_field_aliases_in_file",
        forbidden_normalizer,
    )
    monkeypatch.setattr(
        nexus_map_codebase,
        "normalize_duplicate_ast_beacons_in_file",
        forbidden_normalizer,
    )

    source_path, before, result, observed = _run_file_refresh(
        monkeypatch,
        tmp_path,
        dry_run=True,
        carto_claim_id=None,
    )

    assert source_path.read_bytes() == before == _HYGIENE_FIXTURE
    assert source_path.stat().st_mtime_ns == observed["before_mtime_ns"]
    assert result["success"] is True
    assert result["dry_run"] is True
    assert observed["dry_run"] is True
    mapped_nodes = observed["source_json"]["nodes"]
    assert len(mapped_nodes) == 1
    assert mapped_nodes[0]["name"].startswith("ƒ target()")
    assert "#alpha" in mapped_nodes[0]["name"]
    assert "#beta" in mapped_nodes[0]["name"]
    assert "comment: legacy alias" in mapped_nodes[0]["note"]
    assert "comment: canonical second" in mapped_nodes[0]["note"]


def test_source_hygiene_requires_both_real_run_and_owning_claim(monkeypatch, tmp_path):
    def forbidden_normalizer(_source_path):  # pragma: no cover - failure sentinel
        raise AssertionError("unclaimed or dry-run source hygiene attempted a persistent write")

    monkeypatch.setattr(
        nexus_map_codebase,
        "normalize_beacon_comment_field_aliases_in_file",
        forbidden_normalizer,
    )
    monkeypatch.setattr(
        nexus_map_codebase,
        "normalize_duplicate_ast_beacons_in_file",
        forbidden_normalizer,
    )

    for case_name, dry_run, carto_claim_id in (
        ("claimed-dry", True, "owned-carto-claim"),
        ("unclaimed-real", False, None),
    ):
        case_path = tmp_path / case_name
        case_path.mkdir()
        source_path, before, result, observed = _run_file_refresh(
            monkeypatch,
            case_path,
            dry_run=dry_run,
            carto_claim_id=carto_claim_id,
        )
        assert source_path.read_bytes() == before == _HYGIENE_FIXTURE
        assert source_path.stat().st_mtime_ns == observed["before_mtime_ns"]
        assert result["success"] is True


def test_claimed_real_refresh_still_normalizes_both_hygiene_triggers(monkeypatch, tmp_path):
    calls: list[tuple[str, bool]] = []
    normalize_alias = nexus_map_codebase.normalize_beacon_comment_field_aliases_in_file
    normalize_duplicates = nexus_map_codebase.normalize_duplicate_ast_beacons_in_file

    def tracked_alias(source_path):
        result = normalize_alias(source_path)
        calls.append(("comment_alias", bool(result.get("changed"))))
        return result

    def tracked_duplicates(source_path):
        result = normalize_duplicates(source_path)
        calls.append(("duplicate_ast", bool(result.get("changed"))))
        return result

    monkeypatch.setattr(
        nexus_map_codebase,
        "normalize_beacon_comment_field_aliases_in_file",
        tracked_alias,
    )
    monkeypatch.setattr(
        nexus_map_codebase,
        "normalize_duplicate_ast_beacons_in_file",
        tracked_duplicates,
    )

    source_path, before, result, observed = _run_file_refresh(
        monkeypatch,
        tmp_path,
        dry_run=False,
        carto_claim_id="owned-carto-claim",
        fixture=_HYGIENE_FIXTURE.replace(b"id=second,", b"id=first,"),
    )

    after = source_path.read_bytes()
    normalized = after.decode("utf-8")
    assert calls == [("comment_alias", True), ("duplicate_ast", True)]
    assert after != before
    assert "comments=" not in normalized
    assert normalized.count("# @beacon[") == 1
    assert "slice_labels=alpha,beta," in normalized
    assert "comment=legacy alias | canonical second," in normalized
    assert result["success"] is True
    assert result["dry_run"] is False
    assert observed["dry_run"] is False


def test_claimed_real_distinct_ids_are_choice_and_mapping_still_proceeds(monkeypatch, tmp_path):
    """ab0653d silently picks first; a local choice must not stop the map."""
    normalize = nexus_map_codebase.normalize_duplicate_ast_beacons_in_file
    outcomes = []
    def tracked(path):
        outcome = normalize(path)
        outcomes.append(outcome)
        return outcome
    monkeypatch.setattr(nexus_map_codebase, "normalize_duplicate_ast_beacons_in_file", tracked)
    source_path, before, result, observed = _run_file_refresh(
        monkeypatch, tmp_path, dry_run=False, carto_claim_id="owned-carto-claim")
    assert source_path.read_bytes() == before.replace(b"comments=", b"comment=")
    assert outcomes[0]["success"] and not outcomes[0]["changed"]
    choice = outcomes[0]["skipped_groups"][0]
    assert choice["reason"] == "distinct_ids"
    assert [m["id"] for m in choice["candidates"]] == ["first", "second"]
    assert result["success"]
    mapped = observed["source_json"]["nodes"][0]
    assert "#alpha" in mapped["name"] and "#beta" in mapped["name"]


def _language_hygiene_fixture(suffix):
    if suffix == ".py":
        return _HYGIENE_FIXTURE.replace(b"id=second,", b"id=first,")
    if suffix == ".ts":
        return _language_hygiene_fixture(".py").replace(b"#", b"//").replace(
            b"def target():\n    return 1", b"function target(): number { return 1; }")
    return b"\n".join([
        b"<!--", b"@beacon[", b"id=first,", b"slice_labels=alpha,", b"kind=ast,", b"comments=legacy alias,", b"]", b"-->",
        b"<!--", b"@beacon[", b"id=first,", b"slice_labels=beta,", b"kind=ast,", b"comment=canonical second,", b"]", b"-->",
        b"# Target", b"Whole-document C0\x07 here.", b"",
    ])


@pytest.mark.parametrize("suffix", [".py", ".md", ".ts"])
@pytest.mark.parametrize("claimed", [False, True], ids=["unclaimed-control", "claimed"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-control"])
def test_claim_dry_language_matrix_keeps_gates_order_and_latest_snapshots(monkeypatch, tmp_path, suffix, claimed, dry_run):
    """Gate cells are controls; ab0653d's real cells lack typed pass evidence."""
    seen = []
    def track(name, original):
        def invoke(path, *args, **kwargs):
            before = Path(path).read_bytes()
            outcome = original(path, *args, **kwargs)
            seen.append({"name": name, "before": before, "after": Path(path).read_bytes(), "outcome": outcome})
            return outcome
        return invoke
    for name in ("repair_markdown_c0_controls_on_disk", "normalize_beacon_comment_field_aliases_in_file",
                 "normalize_duplicate_ast_beacons_in_file"):
        monkeypatch.setattr(nexus_map_codebase, name, track(name, getattr(nexus_map_codebase, name)))
    original_map = nexus_map_codebase.map_source_snapshot
    def mapped(snapshot, **kwargs):
        outcome = original_map(snapshot, **kwargs)
        seen.append({"name": "map_source_snapshot", "before": snapshot.raw_bytes,
                     "after": snapshot.raw_bytes, "outcome": outcome})
        return outcome
    monkeypatch.setattr(nexus_map_codebase, "map_source_snapshot", mapped)
    raw = _language_hygiene_fixture(suffix)
    path, before, result, observed = _run_file_refresh(
        monkeypatch, tmp_path, suffix=suffix, fixture=raw, dry_run=dry_run,
        carto_claim_id="owned-carto-claim" if claimed else None)
    assert result["success"], result
    assert observed["source_json"]["nodes"]
    if dry_run or not claimed:
        assert [s["name"] for s in seen] == ["map_source_snapshot"]
        assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, observed["before_mtime_ns"])
        return
    names = (["repair_markdown_c0_controls_on_disk"] if suffix == ".md" else []) + [
        "normalize_beacon_comment_field_aliases_in_file", "normalize_duplicate_ast_beacons_in_file", "map_source_snapshot"]
    assert [s["name"] for s in seen] == names
    previous = before
    for step in seen[:-1]:
        outcome = step["outcome"]
        assert step["before"] == previous  # the passes re-read; no stale coordinates
        assert outcome["success"] and outcome["changed"] and not outcome["findings"], outcome
        assert outcome["before_sha256"] == hashlib.sha256(previous).hexdigest()
        assert outcome["after_sha256"] == hashlib.sha256(step["after"]).hexdigest()
        assert len(outcome["events"]) == 1 and outcome["events"][0]["status"] == "committed"
        previous = step["after"]
    assert seen[-1]["before"] == previous == path.read_bytes()
    assert b"comments=" not in previous and b"slice_labels=alpha,beta," in previous
    assert b"comment=legacy alias | canonical second," in previous
    if suffix == ".md":
        assert b"\x07" not in previous and b"\\a" in previous


def test_claimed_real_non_utf8_markdown_retains_existing_scope_without_repair_abort(monkeypatch, tmp_path):
    """C03 patch correspondence: diagnostic sentinel is data, not replacement symbols."""
    corpus = Path(__file__).parent / "beacon_robustness_corpus/area6/area6_inputs.json"
    raw = base64.b64decode(json.loads(corpus.read_text(encoding="utf-8"))["other_inputs"]["c0_case_bytes_base64"]["C03_latin1.md"])
    outcomes, order, maps = [], [], []
    def track(name, original):
        def invoke(path, *args, **kwargs):
            order.append(name)
            outcome = original(path, *args, **kwargs)
            if name != "_source_file_error":
                outcomes.append(outcome)
            else:
                maps.append(outcome)
            return outcome
        return invoke
    names = ["repair_markdown_c0_controls_on_disk", "normalize_beacon_comment_field_aliases_in_file",
             "normalize_duplicate_ast_beacons_in_file", "_source_file_error"]
    for name in names:
        monkeypatch.setattr(nexus_map_codebase, name, track(name, getattr(nexus_map_codebase, name)))
    def forbidden(*args, **kwargs):
        pytest.fail("unreadable source entered decoded mapping or identity seeding")
    for name in ("map_codebase", "map_source_snapshot", "reconcile_trees_cartographer"):
        monkeypatch.setattr(nexus_map_codebase, name, forbidden)
    children = ({"id": "old-heading", "parent_id": "FILE", "name": "Old heading", "note": "MD_PATH: Old heading"},
                {"id": "old-child", "parent_id": "old-heading", "name": "Old child"})
    path, before, result, observed = _run_file_refresh(
        monkeypatch, tmp_path, suffix=".md", fixture=raw, dry_run=False,
        carto_claim_id="owned-carto-claim", existing_children=children)
    assert result["success"] is False and result["status"] == "failed"
    assert result["error_kind"] == "not_utf8"
    assert result["header_freshness"]["available"] is False
    assert set(result["retained_uuids"]) == {"old-heading", "old-child"}
    assert observed["updates"] == [] and "source_json" not in observed
    assert observed["file_node"]["note"] == observed["before_note"]
    assert observed["children"] == list(children)
    assert result["findings"][0]["code"] == "file_unreadable"
    assert len(result["repair_outcomes"]) == 3
    assert order == names
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, observed["before_mtime_ns"])
    assert all(not o["success"] and not o["changed"] and o["error_kind"] == "not_utf8" and o["findings"] for o in outcomes)
    assert any("⚠️ Parse Error" in node["name"] for node in maps[0]["children"])
    assert "LINE COUNT:" not in maps[0]["note"]
    assert api_client_nexus._is_cartographer_parse_error_sentinel("⚠️ MD Parse Error")



def test_alias_cas_skip_still_maps_newest_source(monkeypatch, tmp_path):
    """ab0653d's alias CAS has no finding; map must still read the editor save."""
    publish = nexus_map_codebase.commit_beacon_edit_plan
    normalize = nexus_map_codebase.normalize_beacon_comment_field_aliases_in_file
    outcomes = []
    external = _HYGIENE_FIXTURE.replace(b"def target()", b"def editor_target()")
    def save_then_publish(plan, **kwargs):
        Path(plan.snapshot.canonical_path).write_bytes(external)
        return publish(plan, **kwargs)
    def tracked(path):
        outcome = normalize(path)
        outcomes.append(outcome)
        return outcome
    monkeypatch.setattr(nexus_map_codebase, "commit_beacon_edit_plan", save_then_publish)
    monkeypatch.setattr(nexus_map_codebase, "normalize_beacon_comment_field_aliases_in_file", tracked)
    path, _before, result, observed = _run_file_refresh(
        monkeypatch, tmp_path, dry_run=False, carto_claim_id="owned-carto-claim")
    assert result["success"] and path.read_bytes() == external
    assert outcomes[0]["error_kind"] == "source_changed_since_plan" and outcomes[0]["findings"]
    assert not outcomes[0]["events"]
    assert "editor_target" in observed["source_json"]["nodes"][0]["name"]
