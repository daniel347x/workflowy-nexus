"""JR2 G2: structural regression tests for the record read side, the detail view and the startup mark.

WaveSpec rev 3 ([ap:JUH8JU]) §2.9-§2.11 and §3.1's test list; contract cut
[ap:C367PW] rev 1; Q1 ruled by [ap:0SNJXU].  Every test is structural -- key
sets, values, decode/open counters, page geometry -- and never timing.

Fixtures are installed the way the daemon writes them: a record file
``<jobs>/<stamp>_<id>.json`` with its append-only sidecars in
``Path.with_suffix("")`` (``files.jsonl``, ``nodes.jsonl``), never inside the
fixture tree.  A ``summary-head/2`` record is installed by a direct JSON write
(G2's rule: no production writer creates one); the funnel is only ever asked to
*preserve* one it reads.

Every test names, in its docstring, the mechanism that makes it fail before the
G2 change (a missing read side, a default that still returns the whole record, a
funnel that lays a ``/2`` record out as ``/1``, a scanner that thaws evidence).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from workflowy_mcp import carto_job_paging as paging
from workflowy_mcp import carto_job_record as record
from workflowy_mcp import config, job_status_view, server as srv

try:  # the record module re-exports the detail view's public names (§2.10.1)
    from workflowy_mcp.carto_job_record import join_carto_job_detail_pages
except ImportError:  # pragma: no cover - until the re-export lands
    from workflowy_mcp.carto_job_paging import join_carto_job_detail_pages

FIXTURES = Path(__file__).parent / "fixtures" / "carto_job_records"
STAMP = "20261026-120000"
#: §2.10.1: every one-job reply's compact UTF-8 ceiling.
REPLY_BUDGET = 524_288
#: The detail view's derived facets (§2.10).
FACETS = ("outcome", "has_errors", "has_warnings", "has_findings")
#: The complete detail's own evidence bookkeeping the summary view need not carry.
SIDECAR_COUNTERS = frozenset({"rows_on_disk", "torn_tail_bytes", "unreadable_lines",
                              "unrecognized_lines", "evidence_missing"})
#: Top-level metadata compared across layouts (never a whole-record equality).
METADATA = ("id", "type", "mode", "root_uuid", "status", "error", "progress")


# ---------------------------------------------------------------------------
# Fixtures installed as real job folders
# ---------------------------------------------------------------------------

def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _basename(value):
    """The tail of a path on either separator (the fixtures carry Windows paths)."""
    return str(value).replace("\\", "/").rsplit("/", 1)[-1]


def _sidecars(path):
    return Path(path).with_suffix("")


def _install(tmp_path, fixture, layout="v2", *, job_id=None, status=None):
    """Copy one golden fixture into a job folder the reader can open.

    ``"v2"`` takes the built ``summary-head/2`` fixture with its sidecars;
    ``"v1"`` lays today's source payload out through the funnel; ``"legacy"``
    writes the same payload in legacy key order with no layout marker.
    Returns ``(jobs_dir, record_path, payload_written)``.
    """
    folder = FIXTURES / fixture
    sidecar_files = {}
    if layout == "v2":
        payload = _load(folder / "record.json")
        sidecar_files = {name: folder / name for name in ("files.jsonl", "nodes.jsonl")
                         if (folder / name).exists()}
    else:
        suffix = ".legacy.record.json" if layout == "legacy" else ".record.json"
        source = folder / "sources" / f"{fixture}{suffix}"
        if layout == "legacy" and not source.exists():
            # One canonical source is enough: legacy order puts the evidence
            # first and omits the layout marker, without changing any value.
            payload = _load(folder / "sources" / f"{fixture}.record.json")
            payload = {"result_summary": payload.pop("result_summary"), **payload}
        else:
            payload = _load(source)
        payload.pop("record_layout", None)
    if status is not None:
        payload["status"] = status
    if job_id is not None:
        payload["id"] = job_id
    jobs = tmp_path / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    path = jobs / f"{STAMP}_{payload['id']}.json"
    if layout == "v1":
        assert config.merge_write_carto_job_status(path, payload)
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    side = _sidecars(path)
    side.mkdir(exist_ok=True)
    for name, source in sidecar_files.items():
        (side / name).write_bytes(source.read_bytes())
    return jobs, path, payload


def _pure_detail(path):
    """The complete detail composed from the read side alone (no server)."""
    cached = record.read_carto_job_record_cached(path)
    if cached.get("record_layout") != record.CARTO_JOB_RECORD_LAYOUT_V2:
        # An old-layout record is its own complete detail, decoded whole (§2.9).
        return record.stitch_carto_job_detail(_load(path), {})
    side = _sidecars(path)
    folded = record.fold_carto_job_evidence(
        (side / "files.jsonl").read_bytes() if (side / "files.jsonl").exists() else None,
        nodes=(side / "nodes.jsonl").read_bytes() if (side / "nodes.jsonl").exists() else None,
        in_flight=cached.get("in_flight"),
        status=cached.get("status"),
        job_type=cached.get("type"),
    )
    return record.stitch_carto_job_detail(cached, folded)


def _status(**params):
    return asyncio.run(srv._system_job_status_impl(**params))


def _pages(job_id, **params):
    """Walk the detail view's cursor to its end (no page is ever assumed)."""
    pages, offset, slice_offset = [], 0, 0
    while True:
        page = _status(job_id=job_id, view="detail", offset=offset,
                       slice_offset=slice_offset, **params)
        pages.append(page)
        meta = page.get("detail_page") or {}
        next_offset = meta.get("next_offset")
        if next_offset is None:
            return pages
        offset, slice_offset = next_offset, meta.get("next_slice_offset") or 0


def _joined(job_id, **params):
    return join_carto_job_detail_pages(_pages(job_id, **params))


def _reply_bytes(reply):
    """§2.10.1's measurement: compact UTF-8 JSON, non-ASCII unescaped."""
    return len(json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _open_paths(monkeypatch):
    """Every file opened while the spy is installed (pathlib and plain open)."""
    import builtins
    import io

    opened: list[str] = []
    original = builtins.open

    def spy(file, mode="r", *args, **kwargs):
        opened.append(str(file))
        return original(file, mode, *args, **kwargs)

    monkeypatch.setattr(io, "open", spy)
    monkeypatch.setattr(builtins, "open", spy)
    return opened


def _reset_reader_caches():
    record._record_cache.clear()
    record._record_ids.clear()
    record._detail_cache.clear()


@pytest.fixture(autouse=True)
def _fresh_reader_and_tables(monkeypatch):
    """No cached signature, id index or detail index; no live job tables."""
    _reset_reader_caches()
    for name in ("_jobs", "_carto_inprocess_tasks", "_carto_job_controls",
                 "_carto_final_obligations", "_carto_startup_failed_jobs",
                 "_carto_job_states", "_carto_active_cache_jobs"):
        monkeypatch.setattr(srv, name, {}, raising=False)
    monkeypatch.setattr(srv, "_governance_holds", lambda: (), raising=False)
    monkeypatch.setattr(srv, "_carto_live_job_projection", lambda: {}, raising=False)
    yield


# ---------------------------------------------------------------------------
# The read side: all three layouts stitch the same complete detail
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("layout", ["v2", "v1", "legacy"])
def test_every_layout_stitches_the_source_records_complete_detail(tmp_path, monkeypatch, layout):
    """A /2 record's stitched detail equals its source record's complete values.

    Would fail before: no fold/stitch read side exists, so a /2 record (or a
    head-only read of an old one) cannot reconstruct result_summary at all.
    """
    jobs, path, _ = _install(tmp_path, "folder_refresh_trim", layout)
    source = _load(FIXTURES / "folder_refresh_trim" / "sources" / "folder_refresh_trim.record.json")
    detail = _pure_detail(path)
    assert detail["result_summary"] == source["result_summary"]
    for key in METADATA:
        if key in source:
            assert detail[key] == source[key], key
    assert all(key in detail for key in FACETS)


def test_a_v2_folder_detail_equals_its_source_including_the_folder_lists(tmp_path, monkeypatch):
    """B8/CF-159: FOLDER nexusignore_sources and missing_paths come from evidence.

    The fixture evidences both lists as ``item`` lines (one value each, §2.4)
    while its record keeps only a count-bounded preview, so this is the
    differential oracle for a terminal folder run with run-level items and a
    250-finding row (the complete ``findings`` list is the composer's own
    200-entry preview, as today's record has it).

    Would fail before: nothing folds item lines back into result_summary.
    """
    jobs, path, _ = _install(tmp_path, "mixed_folder", "v2")
    source = _load(FIXTURES / "mixed_folder" / "sources" / "mixed_folder.record.json")["result_summary"]
    detail = _pure_detail(path)["result_summary"]
    assert detail["nexusignore_sources"] == source["nexusignore_sources"]
    assert detail["missing_paths"] == source["missing_paths"]
    assert detail == source


# ---------------------------------------------------------------------------
# Outcome facets (CF-174, §2.3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,summary,outcome,has_errors,has_problem", [
    ("running", {}, "running", False, False),
    ("waiting", {}, "running", False, False),
    ("cancelling", {}, "running", False, False),
    ("future_state", {}, "running", False, False),
    ("failed", {"failed": 2}, "failed", True, True),
    ("cancelled", {}, "cancelled", False, False),
    ("completed", {}, "completed", False, False),
    ("completed", {"failed": 1}, "completed_with_errors", True, True),
    ("completed_with_warnings", {"warnings_total": 3}, "completed_with_warnings", False, True),
    ("completed", {"findings_total": 2}, "completed_with_warnings", False, True),
])
def test_outcome_covers_every_status_class(status, summary, outcome, has_errors, has_problem):
    """§2.3's sticky vocabulary; an unknown status is presented as running.

    Would fail before: carto_job_outcome does not exist (no outcome key at all).
    """
    facets = record.carto_job_outcome({"status": status, "result_summary": summary})
    assert facets["outcome"] == outcome
    assert facets["has_errors"] is has_errors
    assert any(facets[key] for key in ("has_errors", "has_warnings", "has_findings")) is has_problem


def test_unknown_status_warns_once_per_signature(tmp_path, monkeypatch, caplog):
    """§2.3/§2.9: an unknown status is logged once for a signature, never refused.

    Would fail before: the shared reader does not exist, so there is no
    signature and no warning to take once.
    """
    jobs, path, _ = _install(tmp_path, "mixed_folder", "v2", status="future_state")
    with caplog.at_level("WARNING", logger="workflowy_mcp.carto_job_record"):
        first = record.read_carto_job_record_cached(path)
        record.read_carto_job_record_cached(path)
    messages = [entry for entry in caplog.records if str(path) in entry.getMessage()]
    assert len(messages) == 1, [entry.getMessage() for entry in messages]
    assert "future_state" in messages[0].getMessage()
    assert first["outcome"] == "running"


def test_legacy_markdown_phase8_failure_reads_warnings_by_type(tmp_path, monkeypatch):
    """CF-174: a /1 Markdown record's phase-8 failure is not 'with warnings'.

    Would fail before: a head read of a /1 record never sees phase8 (a tail
    key), so the job is labelled completed_with_warnings.
    """
    payload = {
        "id": "md-1", "type": "CARTO_REFRESH", "mode": "markdown", "status": "completed_with_warnings",
        "progress": {"completed_files": 1},
        "result_summary": {
            "status": "completed_with_warnings", "warnings_total": 1,
            "warnings_by_type": {"phase8_refresh_failed": 1},
            "files": [{"file": "/repo/a.md", "status": "refreshed"}],
            "phase8": {"success": False, "error": "inline refresh failed"},
        },
    }
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    path = jobs / f"{STAMP}_md-1.json"
    assert config.merge_write_carto_job_status(path, payload)
    head = record.read_carto_job_record_cached(path)
    assert "phase8" not in head["result_summary"]  # a tail key, head-only read
    assert head["outcome"] == "completed_with_errors"
    assert head["has_errors"] is True


# ---------------------------------------------------------------------------
# The fold: full/delta rows, projections, bulk receipts
# ---------------------------------------------------------------------------

def _row_line(**row):
    return json.dumps({"k": "row", "sec": "files", "row": row}, ensure_ascii=True)


def test_fold_applies_delta_unset_and_extend_over_a_full_row():
    """§2.4/§2.5: dict pop, update, setdefault+extend, in that order.

    Would fail before: no fold exists.
    """
    full = _row_line(file="/repo/a.py", status="refreshed", changed=False,
                     findings=[{"code": "first"}], warnings=[{"type": "w"}],
                     events=[{"type": "kept"}], projection=[])
    delta = json.dumps({"k": "row", "sec": "files",
                        "row": {"file": "/repo/a.py", "status": "failed", "findings": []},
                        "delta": {"unset": ["warnings"], "extend": {"events": [{"type": "added"}]}}})
    folded = record.fold_carto_job_evidence((full + "\n" + delta + "\n").encode(), status="failed")
    (row,) = folded["rows"]["files"]
    assert row["status"] == "failed"
    assert row["findings"] == []            # an explicit [] survives, never dropped
    assert "warnings" not in row            # unset removes the key
    assert row["events"] == [{"type": "kept"}, {"type": "added"}]
    assert folded["rows_on_disk"] == 2


def test_fold_re_expands_a_single_stored_projection_and_keeps_null_versus_list():
    """§2.6: projection is stored once; null re-expands, [] stays [].

    Would fail before: no projection re-expansion exists.
    """
    implicit = json.dumps({"k": "row", "sec": "files", "row": {
        "file": "/repo/a.py", "status": "refreshed", "projection": None, "keep": 1}, "proj": ["status", "keep"]})
    explicit = json.dumps({"k": "row", "sec": "files", "row": {
        "file": "/repo/b.py", "status": "refreshed", "projection": []}})
    absent = json.dumps({"k": "row", "sec": "files", "row": {"file": "/repo/c.py", "status": "refreshed"}})
    folded = record.fold_carto_job_evidence((implicit + "\n" + explicit + "\n" + absent + "\n").encode())
    rows = {_basename(row["file"]): row for row in folded["rows"]["files"]}
    assert rows["a.py"]["projection"] == {"status": "refreshed", "keep": 1}
    assert rows["b.py"]["projection"] == []
    assert "projection" not in rows["c.py"]


def test_fold_bulk_receipts_in_both_orderings_in_flight_and_in_sync(tmp_path, monkeypatch):
    """§2.5: receipts combine in return order; the in-flight row is named.

    Would fail before: no fold/receipt tally exists.
    """
    folder = FIXTURES / "bulk_receipts_synthetic"
    data = _load(folder / "record.json")
    files = (folder / "files.jsonl").read_bytes()
    nodes = (folder / "nodes.jsonl").read_bytes()
    folded = record.fold_carto_job_evidence(files, nodes=nodes, in_flight=data["in_flight"],
                                            status=data["status"], job_type=data["type"])
    rows = {_basename(row["file"]): row for row in folded["rows"]["files"]}
    own_then_other = rows["own_then_other.py"]
    assert own_then_other["status"] == "refreshed"
    assert [receipt["node_id"] for receipt in own_then_other["node_outcomes"]] == ["own-a", "other-a"]
    assert own_then_other["changed"] is True
    rejected = rows["own_then_other_rejected.py"]
    assert (rejected["status"], rejected["nodes_satisfied"]) == ("failed", 1)
    other_first = rows["other_then_own_rejected.py"]
    assert other_first["findings"], "the earlier group's findings are kept"
    assert other_first["status"] == "failed"
    assert rows["in_sync.py"]["node_outcomes"] == []
    assert rows["in_sync.py"]["nodes_satisfied"] == 0
    interrupted = rows["interrupted.py"]  # a terminal record: the in-flight row is added
    assert interrupted["status"] == "failed"
    assert interrupted["node_outcomes"][0]["node_id"] == "e-1"
    assert len(folded["node_outcomes"]) == 7
    # A running record marks the same evidence in progress, never as failed (B9).
    running = record.fold_carto_job_evidence(files, nodes=nodes, in_flight=data["in_flight"],
                                             status="running", job_type=data["type"])
    # §2.5's receipt rule gives a key with receipts and no row line today's base
    # row whatever the status; only the PLACEHOLDER is status-gated (B9), so a
    # running record shows the file as in progress, never as failed/interrupted.
    running_row = next(row for row in running["rows"]["files"]
                       if _basename(row["file"]) == "interrupted.py")
    assert running_row["status"] == "completed"
    assert running_row["node_outcomes"][0]["node_id"] == "e-1"
    assert running["in_progress"]["status"] == "running"
    assert running["in_progress"]["error"] is None


# ---------------------------------------------------------------------------
# The pager (pure): joins, slices, versions, previews, budget
# ---------------------------------------------------------------------------

def _detail_mixed(tmp_path, monkeypatch):
    jobs, path, _ = _install(tmp_path, "mixed_folder", "v2")
    return _pure_detail(path)


def test_detail_pages_join_deep_equal_at_the_contract_budget(tmp_path, monkeypatch):
    """§2.10.1: frame then entries; the join is exactly the complete detail.

    Would fail before: no pager and no join exist.
    """
    detail = _detail_mixed(tmp_path, monkeypatch)
    pages, offset, slice_offset = [], 0, 0
    while True:
        page = paging.page_carto_job_detail(detail, offset=offset, slice_offset=slice_offset, files_limit=3)
        assert _reply_bytes(page) <= REPLY_BUDGET
        pages.append(page)
        meta = page["detail_page"]
        assert meta["offset"] == offset and meta["slice_offset"] == slice_offset
        if meta["next_offset"] is None:
            break
        offset, slice_offset = meta["next_offset"], meta["next_slice_offset"] or 0
    assert len(pages) > 1, "three rows a page must page an eight-row detail"
    assert join_carto_job_detail_pages(pages) == detail


def test_a_unit_too_large_for_a_page_is_sliced_and_the_join_restores_it(tmp_path, monkeypatch):
    """§2.10.1: a giant row is carried alone in slices, never dropped.

    Would fail before: no slicing/join exists, so the 121 KB row would have to
    be truncated or refused.
    """
    detail = _detail_mixed(tmp_path, monkeypatch)
    budget = 32_768
    pages, offset, slice_offset = [], 0, 0
    while True:
        page = paging.page_carto_job_detail(detail, offset=offset, slice_offset=slice_offset, budget=budget)
        pages.append(page)
        meta = page["detail_page"]
        if meta["next_offset"] is None:
            break
        offset, slice_offset = meta["next_offset"], meta["next_slice_offset"] or 0
    entry_slices = [meta["slice"] for meta in (page["detail_page"] for page in pages)
                    if meta.get("slice") and meta["slice"].get("kind") == "entry"]
    assert any(slice_.get("list") == "result_summary.files" for slice_ in entry_slices), entry_slices
    assert join_carto_job_detail_pages(pages) == detail


def test_a_page_carries_the_evidence_version_and_a_change_is_visible_not_refused():
    """B3: pages name their evidence version; a moved version is flagged.

    Would fail before: pages and versions do not exist.
    """
    detail = {"id": "j", "status": "completed",
              "result_summary": {"files": [{"file": "/a.py"}, {"file": "/b.py"}]}}
    first = paging.page_carto_job_detail(detail, files_limit=1, evidence_version="version-1")
    assert first["detail_page"]["evidence_version"] == "version-1"
    second = paging.page_carto_job_detail(detail, files_limit=1, evidence_version="version-2",
                                          offset=first["detail_page"]["next_offset"], slice_offset=0)
    joined = join_carto_job_detail_pages([first, second])
    assert joined["evidence_version_mismatch"]["versions"] == ["version-1", "version-2"]
    assert joined["result_summary"]["files"] == [{"file": "/a.py"}, {"file": "/b.py"}]


def test_summary_over_its_room_is_previewed_and_the_detail_carries_it_whole():
    """§2.10.1/B5: largest value first; identifiers stay; detail says it whole.

    Would fail before: no preview exists, so an oversized head is either sent
    whole or truncated.
    """
    summary = {
        "id": "j", "type": "CARTO_REFRESH", "status": "failed", "error_kind": "carto_job_exception",
        "outcome": "failed", "has_errors": True, "has_warnings": False, "has_findings": False,
        "error": "boom", "progress": {"completed_files": 3},
        "result_summary": {"errors": ["e" * 2_000 for _ in range(200)], "unknown_head": "x" * 40_000},
    }
    budget = 8_192
    preview = paging.preview_carto_job_summary(summary, budget=budget)
    assert _reply_bytes(preview) <= budget
    assert preview["previewed"], "the over-room values must be named"
    for key in paging.SUMMARY_PROTECTED_KEYS:
        if key in summary:
            assert preview[key] == summary[key], key
    assert preview["truncated"] is True and preview["message"]
    # The detail view carries every byte the summary previewed away.
    pages, offset, slice_offset = [], 0, 0
    while True:
        page = paging.page_carto_job_detail(summary, offset=offset, slice_offset=slice_offset, budget=budget)
        pages.append(page)
        meta = page["detail_page"]
        if meta["next_offset"] is None:
            break
        offset, slice_offset = meta["next_offset"], meta["next_slice_offset"] or 0
    assert join_carto_job_detail_pages(pages) == summary


def test_the_reply_budget_constant_is_the_contract_number():
    """§2.10.1: one budget for one job's replies, 512 KiB.

    Would fail before: JOB_REPLY_MAX_BYTES does not exist.
    """
    assert job_status_view.JOB_REPLY_MAX_BYTES == REPLY_BUDGET
    assert paging.DETAIL_PAGE_MAX_BYTES == job_status_view.JOB_REPLY_MAX_BYTES


# ---------------------------------------------------------------------------
# The one-job view through the tool surface
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("layout", ["v2", "v1", "legacy"])
def test_default_one_job_reply_is_the_summary_view_without_evidence(tmp_path, monkeypatch, layout):
    """§2.10: no view means the summary; no evidence list and no sidecar read.

    Would fail before: the default one-job reply is the whole record, so it
    carries every per-file row and opens the evidence.
    """
    jobs, path, _ = _install(tmp_path, "mixed_folder", layout)
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(jobs))
    job_id = "carto-refresh-folder-a9b44230"
    detail = _joined(job_id)
    _reset_reader_caches()
    opened = _open_paths(monkeypatch)
    summary = _status(job_id=job_id)
    side = _sidecars(path)
    assert not [name for name in opened if Path(name).parent == side], "the summary view read the evidence"
    assert [name for name in opened if name == str(path)], "the summary view never read the record"
    # A /2 record is all head, so its bounded aggregate files_changed stays; the
    # per-file / per-node evidence lists never do (§2.2, §2.10).
    forbidden = set(config.CARTO_RESULT_SUMMARY_TAIL_KEYS)
    if layout == "v2":
        forbidden.discard("files_changed")
    for key in forbidden:
        assert key not in (summary.get("result_summary") or {}), key
    missing = set(detail) - set(summary) - SIDECAR_COUNTERS
    assert not missing, f"the summary view dropped top-level keys: {sorted(missing)}"
    assert summary["status"] == detail["status"] == "completed_with_warnings"


def test_one_job_detail_opens_only_that_jobs_record_and_sidecar(tmp_path, monkeypatch):
    """§2.9: a one-job call hydrates only that job, after no scan.

    Would fail before: every one-job call scans every record, so both jobs'
    files are opened.
    """
    jobs, first, _ = _install(tmp_path, "mixed_folder", "v2", job_id="job-a")
    _install(tmp_path, "bulk_receipts_synthetic", "v2", job_id="job-b")
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(jobs))
    opened = _open_paths(monkeypatch)
    _joined("job-a")
    allowed = {str(first), str(_sidecars(first) / "files.jsonl")}
    job_files = [name for name in opened if Path(name).parent == first.parent
                 or str(_sidecars(first)) in name]
    assert job_files, "the detail view read no job file at all"
    assert all(name in allowed for name in job_files), job_files


def test_detail_view_joins_to_the_complete_detail_and_pages_within_budget(tmp_path, monkeypatch):
    """§2.10/§2.10.1: today's complete detail, page by page, within 512 KiB.

    Would fail before: there is no detail view, no cursor and no version.
    """
    jobs, path, _ = _install(tmp_path, "mixed_folder", "v2")
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(jobs))
    pages = _pages("carto-refresh-folder-a9b44230", files_limit=3)
    assert len(pages) > 1
    versions = set()
    for page in pages:
        assert _reply_bytes(page) <= job_status_view.JOB_REPLY_MAX_BYTES
        meta = page["detail_page"]
        versions.add(meta["evidence_version"])
        assert isinstance(meta["evidence_version"], str) and meta["evidence_version"]
        assert page["truncated"] == (meta["next_offset"] is not None)
    assert len(versions) == 1, "one walk must see one evidence version"
    joined = join_carto_job_detail_pages(pages)
    expected = _pure_detail(path)
    assert joined["result_summary"] == expected["result_summary"]
    for key in METADATA:
        if key in expected:
            assert joined[key] == expected[key], key
    assert all(key in joined for key in FACETS)


def test_files_view_all_and_problems_page_every_row_exactly_once(tmp_path, monkeypatch):
    """§2.10: `files` pages the same folded rows, in order, each once.

    Would fail before: no `files` parameter exists.
    """
    jobs, path, _ = _install(tmp_path, "mixed_folder", "v2")
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(jobs))
    job_id = "carto-refresh-folder-a9b44230"
    rows = _pure_detail(path)["result_summary"]["files"]

    all_rows, offset = [], 0
    while True:
        reply = _status(job_id=job_id, files="all", files_offset=offset, files_limit=2)
        page = reply["files_page"]
        assert page["filter"] == "all" and page["limit"] == 2 and page["total"] == len(rows)
        all_rows.extend(reply["result_summary"]["files"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert all_rows == rows

    expected = [row for row in rows if _is_problem(row)]
    assert expected, "the fixture must hold at least one problem row"
    problems, offset = [], 0
    while True:
        reply = _status(job_id=job_id, files="problems", files_offset=offset, files_limit=2)
        page = reply["files_page"]
        assert page["filter"] == "problems" and page["total"] == len(expected)
        problems.extend(reply["result_summary"]["files"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert problems == expected


def test_file_view_returns_that_paths_folded_rows(tmp_path, monkeypatch):
    """§2.10: `file=` returns that canonical path's row(s), nothing else.

    Would fail before: no `file` parameter exists.
    """
    jobs, path, _ = _install(tmp_path, "mixed_folder", "v2")
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(jobs))
    rows = _pure_detail(path)["result_summary"]["files"]
    target = rows[1]["file"]
    reply = _status(job_id="carto-refresh-folder-a9b44230", file=target)
    returned = reply["result_summary"]["files"]
    assert [row["file"] for row in returned] == [target]
    assert returned[0] == next(row for row in rows if row["file"] == target)


def test_bulk_detail_keeps_row_receipts_and_the_global_receipt_list(tmp_path, monkeypatch):
    """§2.5/§2.10: a bulk detail keeps every receipt, per row and run-wide.

    Would fail before: no fold exists, so a /2 bulk record cannot rebuild its
    rows or its global node_outcomes list.
    """
    jobs, path, _ = _install(tmp_path, "bulk_receipts_synthetic", "v2", job_id="bulk-a")
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(jobs))
    detail = _joined("bulk-a")
    expected = _pure_detail(path)
    assert detail["result_summary"] == expected["result_summary"]
    rows = {_basename(row["file"]): row for row in detail["result_summary"]["files"]}
    assert [receipt["node_id"] for receipt in rows["own_then_other.py"]["node_outcomes"]] == ["own-a", "other-a"]
    assert rows["own_then_other_rejected.py"]["status"] == "failed"
    assert rows["in_sync.py"]["node_outcomes"] == [] and rows["in_sync.py"]["nodes_satisfied"] == 0
    assert rows["interrupted.py"]["node_outcomes"][0]["node_id"] == "e-1"
    assert len(detail["result_summary"]["node_outcomes"]) == 7


def _is_problem(row):
    """§2.10's problem row: a failed-class/skipped status or any problem list."""
    failed = {"failed", "error", "unavailable", "parse_failed", "skipped"}
    return (str(row.get("status") or "").lower() in failed
            or any(row.get(key) for key in ("warnings", "findings", "events", "node_errors", "errors")))


# ---------------------------------------------------------------------------
# Q1: the startup abandonment mark ([ap:0SNJXU])
# ---------------------------------------------------------------------------

def test_startup_mark_preserves_an_existing_v2_record_and_its_evidence(tmp_path, monkeypatch):
    """Q1 + Q4/B5: preserve /2 and evidence; materialize exact warning fields.

    Would fail before: the funnel lays every rewrite out as summary-head/1, so
    a /2 record loses its layout and its evidence reassembly after a restart.
    """
    jobs, path, _ = _install(tmp_path, "folder_refresh_trim", "v2")
    side = _sidecars(path)
    before_file = _load(path)
    before_detail = _pure_detail(path)
    before_bytes = (side / "files.jsonl").read_bytes()

    records, warnings = srv._read_and_mark_abandoned_carto_tasks(jobs)
    assert warnings == []
    assert set(records) == {before_file["id"]}

    after_file = _load(path)
    mark = srv._carto_abandoned_status(before_file)
    assert after_file["record_layout"] == "summary-head/2"
    assert after_file["evidence"] == before_file["evidence"]
    assert after_file["in_flight"] == before_file["in_flight"]
    assert after_file["result_summary"] == before_file["result_summary"]
    # Q4(2): B5 postdates the frozen G2 fixtures and applies even to the
    # startup mark. Exactly these two presentation fields may be added/capped.
    assert after_file["warning_count"] == before_file["result_summary"]["warnings_total"]
    assert after_file["warnings"] == (before_file.get("warnings") or [])[:200]
    assert (side / "files.jsonl").read_bytes() == before_bytes, "the mark rewrote evidence"
    assert after_file["status"] == "failed" and after_file["error_kind"] == "daemon_restarted"
    assert after_file["error"] == mark["error"] and after_file["error_extra"] == mark["error_extra"]
    assert after_file["path"] == str(path)

    after_detail = _pure_detail(path)
    allowed = set(mark) | {"path", *FACETS, "warning_count", "warnings"}
    changed = {key for key in set(before_detail) | set(after_detail)
               if before_detail.get(key) != after_detail.get(key)}
    assert changed <= allowed, f"the mark changed more than its own keys: {sorted(changed - allowed)}"
    assert after_detail["result_summary"] == before_detail["result_summary"]
    assert after_detail["outcome"] == "failed"


@pytest.mark.parametrize("layout", ["v1", "legacy"])
def test_startup_mark_preserves_a_500_row_tail_of_old_layouts(tmp_path, monkeypatch, layout):
    """§2.11: marking an old record keeps every key, every tail row, each value.

    Base-compatible control: a mistaken cached-head rewrite would lose the
    500-row tail. Only the existing abandonment mark's keys may change.
    """
    jobs, path, _ = _install(tmp_path, "folder_refresh_trim", layout)
    rows = [{"file": f"/repo/file_{index:03d}.py", "status": "refreshed", "changed": False,
             "findings": [], "warnings": [], "events": [], "projection": None,
             "unknown": {"nullable": None, "empty": []}}
            for index in range(500)]
    data = _load(path)
    data["result_summary"] = dict(data["result_summary"], files=rows)
    if layout == "v1":
        assert config.merge_write_carto_job_status(path, data)
    else:
        data.pop("record_layout", None)
        path.write_text(json.dumps(data), encoding="utf-8")
    before = _load(path)

    records, warnings = srv._read_and_mark_abandoned_carto_tasks(jobs)
    assert warnings == []
    after = _load(path)
    assert after["status"] == "failed" and after["error_kind"] == "daemon_restarted"
    mark_keys = set(srv._carto_abandoned_status(before)) | {"path", "record_layout"}
    assert set(after) - set(before) <= mark_keys
    assert set(before) <= set(after)
    for key, value in before.items():
        if key not in mark_keys:
            assert after[key] == value, key
    assert after["result_summary"] == before["result_summary"]
    assert len(after["result_summary"]["files"]) == 500
    assert after["result_summary"]["files"][0]["projection"] is None
    assert after["result_summary"]["files"][0]["unknown"] == {"nullable": None, "empty": []}
    assert records[before["id"]]["status"] == "failed"


def test_the_funnel_creates_only_v1_and_preserves_v2_only_when_already_v2(tmp_path, monkeypatch):
    """§2.11 + Q1: no new /2 record; a /2 rewrite keeps /2 and its values.

    Would fail before: the funnel writes /1 for every payload (no preserve_v2
    read) and `_without_carto_result_tail` restrips a /2 summary.
    """
    payload = {"id": "legacy-1", "type": "CARTO_REFRESH", "status": "completed",
               "result_summary": {"files": [{"file": "/a.py"}], "status": "completed"}}
    assert config.carto_job_record_layout(payload)["record_layout"] == config.CARTO_JOB_RECORD_LAYOUT
    assert config.carto_job_record_layout(payload, preserve_v2=True)["record_layout"] == "summary-head/2"

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    legacy_path = jobs / f"{STAMP}_legacy-1.json"
    legacy_path.write_text(json.dumps(payload), encoding="utf-8")
    # The funnel is handed the whole record, as the startup mark hands it one.
    assert config.merge_write_carto_job_status(legacy_path, payload)
    written = legacy_path.read_text(encoding="utf-8")
    assert '"summary-head/2"' not in written and '"summary-head/1"' in written

    # The /2 twin is only ever rewritten in place; every value comes back.
    _, v2_path, v2_payload = _install(tmp_path / "v2", "mixed_folder", "v2")
    assert config.merge_write_carto_job_status(v2_path, v2_payload)
    reread = _load(v2_path)
    assert reread["record_layout"] == "summary-head/2"
    assert reread["evidence"] == v2_payload["evidence"]
    assert reread["in_flight"] == v2_payload["in_flight"]
    assert reread["result_summary"] == v2_payload["result_summary"]
    assert config._without_carto_result_tail(reread) == reread


# ---------------------------------------------------------------------------
# Shared-reader counters: one decode per signature, never a sidecar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("layout", ["v2", "v1", "legacy"])
def test_list_cleanup_gc_and_cancel_share_one_read_per_signature(tmp_path, monkeypatch, layout):
    """§2.9: every classifying path shares one read (and decode) per signature.

    A decode needs a read, so exactly one read of the one record across the
    scan, the status list, the history candidates, the cancel lookup and GC is
    the structural bound; an unchanged signature adds none, and no sidecar is
    ever opened.  Counts are structural, never timing.

    Would fail before: each path decodes the whole record for itself, and a
    /2 job's evidence is opened by a scan.
    """
    jobs, path, payload = _install(tmp_path, "folder_refresh_trim", layout)
    job_id = payload["id"]
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(jobs))
    side = _sidecars(path)
    opened = _open_paths(monkeypatch)

    srv._scan_carto_job_files(str(jobs))
    _status(status="all")
    candidates = srv._read_carto_history_candidates(str(jobs))
    srv._find_carto_job_on_disk(str(jobs), job_id)
    asyncio.run(srv._system_job_cancel_impl(job_id))
    srv._gc_carto_jobs(str(jobs), max_age_seconds=10 ** 9,
                       protected_ids=frozenset(), protected_paths=frozenset())
    settled = list(opened)

    # A repeated list adds no read at all.
    srv._scan_carto_job_files(str(jobs))
    _status(status="all")
    assert opened == settled

    assert candidates["rows"] and candidates["errors"] == []
    assert [name for name in opened if Path(name).parent == side] == [], "a classifying path opened evidence"
    assert [name for name in opened if Path(name).parent == path.parent] == [str(path)]


def test_startup_whole_input_decode_is_separate_from_the_shared_reader(tmp_path, monkeypatch):
    """§2.9's one exception: the startup mark reads its own whole input.

    The mark opens its record (its rewrite input, decoded whole); the rewritten
    signature is then read once by the shared reader, and no sidecar is touched.

    Would fail before: nothing distinguishes the rewrite's input from a reader
    decode, so the mark's read and the reader's are indistinguishable and no
    signature is shared.
    """
    jobs, path, _ = _install(tmp_path, "folder_refresh_trim", "v1")
    side = _sidecars(path)
    opened = _open_paths(monkeypatch)

    srv._read_and_mark_abandoned_carto_tasks(jobs)
    mark_opens = [name for name in opened if name == str(path)]
    assert mark_opens, "the mark must read its own record"
    assert [name for name in opened if Path(name).parent == side] == []

    # The rewritten signature is read once by the shared reader, after.
    before = len(opened)
    srv._scan_carto_job_files(str(jobs))
    assert [name for name in opened[before:] if name == str(path)] == [str(path)]
    assert [name for name in opened if Path(name).parent == side] == []
