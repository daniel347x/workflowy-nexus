"""Pure tests for the JR2 detail pager and summary preview (WaveSpec §2.10.1).

``carto_job_paging`` is presentation only: it turns one complete detail into
pages whose every reply is measured on its own compact UTF-8 encoding, and
joins those pages back into the complete detail. Nothing here reads a file or a
record, so every case is a plain in-memory structure.

The witnesses, one per MUST:
* a small detail comes back whole, in one reply, with the usual fields (no
  wrapper replacing them) and only the page keys added;
* every page of a long walk fits the budget, and the join is deep-equal and in
  key order, at the real 512 KiB budget and at small test budgets;
* a frame, a row and a run-level entry larger than a page are each carried
  alone, sliced in characters, and joined exactly;
* ``None`` versus ``[]``, unknown keys and Unicode survive the walk;
* ``files_limit`` counts file rows and phase 8 file rows;
* pages built from different evidence versions join, and say so;
* the lazy page reads only the rows it carries;
* the summary preview sheds the biggest values first, never touches the
  protected ones, and uses ``progress`` last.
"""
from __future__ import annotations

import gc
import json
import weakref
from pathlib import Path

from workflowy_mcp.carto_job_paging import (
    DETAIL_PAGE_MAX_BYTES,
    DETAIL_PAGED_LIST_PATHS,
    CartoJobDetailPager,
    EncodedJsonUnit,
    LazyRowSource,
    detach_detail_frame,
    encoded_bytes,
    join_carto_job_detail_pages,
    page_carto_job_detail,
    preview_carto_job_summary,
    unit_text,
)

SMALL_BUDGET = 4096


class _WeakDict(dict):
    """A dict ``weakref.ref`` can point at; a plain dict cannot."""

    __slots__ = ("__weakref__",)


def _walk(pager: CartoJobDetailPager, *, files_limit: int | None = None) -> list[dict]:
    """Every page, by the cursor the client walks; never trusts a hidden end."""
    pages: list[dict] = []
    offset, slice_offset = 0, 0
    while True:
        page = pager.page(offset=offset, slice_offset=slice_offset, files_limit=files_limit)
        pages.append(page)
        cursor = page["detail_page"]
        offset = cursor["next_offset"]
        slice_offset = cursor["next_slice_offset"] or 0
        if offset is None:
            return pages
        assert len(pages) < 2000, "the cursor must advance"


def _assert_every_page_fits(pages: list[dict], budget: int) -> None:
    for page in pages:
        size = encoded_bytes(page)
        assert size <= budget, (size, page["detail_page"])


def _detail(rows: int = 6, *, phase8: int = 0) -> dict:
    files = [
        {
            "file": f"/repo/módulo_{i}.py",
            "status": "completed" if i % 3 else "skipped",
            "changed": i % 2 == 0,
            "findings": [] if i % 2 else [{"code": "parse", "message": "linha ✓"}],
            "warnings": [] if i % 4 else [{"type": "note", "message": "cuidado"}],
            "events": [{"type": "kept", "ordinal": i}],
            "projection": None if i % 2 else [],
            "unknown": None if i % 2 else {"nested": []},
        }
        for i in range(rows)
    ]
    result_summary: dict = {
        "files_refreshed": rows,
        "files": files,
        "findings": [{"code": "parse", "severity": "error", "message": "um ✓"}],
        "warnings": ["run-level warning"],
        "events": [{"type": "historic"}],
        "files_changed": [row["file"] for row in files if row["changed"]],
        "errors": ["one run error"],
        "refresh_errors": [],
        "present_paths": [row["file"] for row in files],
        "node_outcomes": [],
        "status": "completed_with_warnings",
        "success": True,
        "unknown_summary_key": {"nullable": None},
    }
    if phase8:
        result_summary["phase8"] = {
            "execution": "markdown", "success": True, "files_total": phase8,
            "files": [{"file": f"/repo/doc_{i}.md", "status": "completed"} for i in range(phase8)],
        }
    else:
        result_summary["phase8"] = None
    return {
        "id": "jr2-paging",
        "type": "CARTO_REFRESH",
        "mode": "folder",
        "status": "completed_with_warnings",
        "outcome": "completed_with_warnings",
        "has_errors": True,
        "has_warnings": True,
        "has_findings": True,
        "error": None,
        "error_kind": None,
        "progress": {"total_files": rows, "completed_files": rows, "current_file": None},
        "warnings": [{"type": "note", "message": "top warning"}],
        "result_summary": result_summary,
        "unrecognized_lines": [],
        "carto_job_file": "/jobs/jr2.json",
    }


def _expected_units(detail: dict) -> int:
    """The frame plus every paged entry that is present as a list."""
    total = 1
    for path in DETAIL_PAGED_LIST_PATHS:
        node: object = detail
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, list):
            total += len(node)
    return total


def test_a_small_detail_is_one_page_with_the_usual_fields() -> None:
    detail = _detail(3)
    page = page_carto_job_detail(detail, evidence_version="sig-1")

    assert page["detail_page"]["next_offset"] is None
    assert page["detail_page"]["offset"] == 0
    assert page["detail_page"]["evidence_version"] == "sig-1"
    assert page["truncated"] is False
    assert "message" not in page
    # The complete detail's own fields are the page's fields; only the page
    # keys are added -- no wrapper, no renamed key.
    assert set(page) - {"detail_page", "files_page", "truncated"} == set(detail)
    assert join_carto_job_detail_pages([page]) == detail
    assert list(join_carto_job_detail_pages([page])) == list(detail)


def test_every_page_fits_the_real_budget_and_the_join_is_exact() -> None:
    detail = _detail(500, phase8=250)
    pager = CartoJobDetailPager.from_detail(detail, evidence_version="sig-big")
    pages = _walk(pager)

    assert len(pages) > 5
    _assert_every_page_fits(pages, DETAIL_PAGE_MAX_BYTES)
    assert pager.total_units == _expected_units(detail)
    joined = join_carto_job_detail_pages(pages)
    assert joined == detail
    assert list(joined) == list(detail)
    assert [row["file"] for row in joined["result_summary"]["files"]] == [
        row["file"] for row in detail["result_summary"]["files"]
    ]
    # The frame appears on the first page only; every later page is lists.
    assert "id" in pages[0]
    for page in pages[1:]:
        assert "id" not in page and "carto_job_file" not in page
    # Every page carries its share of rows, and the file-row cursor is contiguous.
    rows_per_page = [len(page.get("result_summary", {}).get("files", [])) for page in pages]
    assert sum(rows_per_page) == 500
    offsets = [page["files_page"]["offset"] for page in pages]
    assert offsets == sorted(offsets)
    assert offsets[0] == 0
    assert pages[-1]["files_page"]["next_offset"] is None


def test_entries_follow_the_order_the_detail_holds_its_lists() -> None:
    detail = {
        "id": "order",
        "result_summary": {
            "errors": ["first list in the detail"],
            "files": [{"file": f"/repo/{i}.py"} for i in range(3)],
            "present_paths": ["/repo/0.py", "/repo/1.py", "/repo/2.py"],
        },
    }
    pager = CartoJobDetailPager.from_detail(detail)
    # Unit 1 is the first entry of the detail's first list, and a page that
    # carries several lists keeps the detail's own list order.
    first = pager.page(offset=1)
    assert list(first["result_summary"]) == ["errors", "files", "present_paths"]
    assert first["result_summary"]["errors"] == ["first list in the detail"]
    assert first["result_summary"]["files"][0] == {"file": "/repo/0.py"}
    assert join_carto_job_detail_pages(_walk(pager)) == detail


def test_every_page_fits_a_small_test_budget() -> None:
    detail = _detail(40)
    pager = CartoJobDetailPager.from_detail(detail, budget=SMALL_BUDGET)
    pages = _walk(pager)
    assert len(pages) > 1
    _assert_every_page_fits(pages, SMALL_BUDGET)
    assert join_carto_job_detail_pages(pages) == detail


def test_a_frame_larger_than_a_page_is_sliced_alone() -> None:
    detail = _detail(2)
    detail["error_extra"] = {"trace": "q" * 300_000, "nested": {"list": [1, 2, 3]}}
    pager = CartoJobDetailPager.from_detail(detail, budget=SMALL_BUDGET)
    pages = _walk(pager)
    sliced = [page for page in pages if page["detail_page"].get("slice", {}).get("kind") == "frame"]
    assert len(sliced) > 1
    for page in sliced:
        assert set(page) >= {"$slice", "detail_page"}
        assert "id" not in page
        assert page["detail_page"]["offset"] == 0
        assert page["detail_page"]["slice"]["kind"] == "frame"
        assert page["$slice"]["offset"] + len(page["$slice"]["text"]) <= 300_000 + 1000
    _assert_every_page_fits(pages, SMALL_BUDGET)
    assert join_carto_job_detail_pages(pages) == detail


def test_a_row_larger_than_a_page_is_sliced_alone() -> None:
    detail = _detail(1)
    huge = {
        "file": "/repo/huge.py",
        "status": "failed",
        "findings": [{"code": "parse", "message": "é" * 100} for _ in range(200)],
        "warnings": [],
        "events": [],
    }
    detail["result_summary"]["files"] = [huge, {"file": "/repo/after.py", "status": "completed"}]
    pager = CartoJobDetailPager.from_detail(detail, budget=SMALL_BUDGET)
    pages = _walk(pager)
    sliced = [page for page in pages if (page["detail_page"].get("slice") or {}).get("kind") == "entry"]
    assert len(sliced) > 1
    for page in sliced:
        meta = page["detail_page"]
        assert meta["slice"]["list"] == "result_summary.files"
        assert meta["slice"]["index"] == 0
        assert meta["slice"]["total"] == len(unit_text(huge))
        # The unit's chunks are consecutive characters of its own encoding.
        assert meta["slice"]["offset"] == meta["slice_offset"]
    _assert_every_page_fits(pages, SMALL_BUDGET)
    joined = join_carto_job_detail_pages(pages)
    assert joined == detail
    assert [row["file"] for row in joined["result_summary"]["files"]] == [
        "/repo/huge.py", "/repo/after.py",
    ]


def test_a_run_level_entry_larger_than_a_page_is_sliced_alone() -> None:
    detail = _detail(2)
    detail["result_summary"]["warnings"] = ["w" * 40_000, "after the big one"]
    pager = CartoJobDetailPager.from_detail(detail, budget=SMALL_BUDGET)
    pages = _walk(pager)
    sliced = [page for page in pages if (page["detail_page"].get("slice") or {}).get("kind") == "entry"]
    assert sliced, "a 200 KB warning cannot fit a 4 KiB page"
    assert all(page["detail_page"]["slice"]["list"] == "result_summary.warnings" for page in sliced)
    _assert_every_page_fits(pages, SMALL_BUDGET)
    assert join_carto_job_detail_pages(pages) == detail


def test_none_versus_empty_and_unknown_keys_survive_paging() -> None:
    detail = {
        "id": "shapes",
        "job_id": "shapes",
        "status": "completed",
        "unknown_top": {"nullable": None, "empty": [], "text": "ünïcode ✓"},
        "result_summary": {
            "files": None,               # not a list: never paged, stays None
            "findings": [],              # a list: paged, stays []
            "warnings": None,
            "events": [],
            "errors": ["é" * 900],
            "present_paths": ["/repo/ü.py"],
            "node_outcomes": [],
            "unknown_summary": {"nullable": None},
        },
        "unrecognized_lines": [{"k": "mystery", "value": None}],
    }
    pager = CartoJobDetailPager.from_detail(detail, budget=2048)
    pages = _walk(pager)
    _assert_every_page_fits(pages, 2048)
    joined = join_carto_job_detail_pages(pages)

    assert joined == detail
    assert joined["result_summary"]["files"] is None
    assert joined["result_summary"]["warnings"] is None
    assert joined["result_summary"]["findings"] == []
    assert joined["unknown_top"] == {"nullable": None, "empty": [], "text": "ünïcode ✓"}
    assert joined["result_summary"]["unknown_summary"] == {"nullable": None}
    assert joined["unrecognized_lines"] == [{"k": "mystery", "value": None}]


def test_files_limit_counts_file_rows_and_phase8_rows() -> None:
    detail = _detail(4, phase8=11)
    pager = CartoJobDetailPager.from_detail(detail, files_limit=3)
    pages = _walk(pager, files_limit=3)
    for page in pages:
        rows = page.get("result_summary", {})
        carried = len(rows.get("files") or []) + len((rows.get("phase8") or {}).get("files") or [])
        assert carried <= 3, page["detail_page"]
    assert join_carto_job_detail_pages(pages) == detail
    # files_page describes the file rows only; phase 8 rows are never counted
    # into its cursor.
    assert max(page["files_page"]["total"] for page in pages) == 4
    assert pages[-1]["files_page"]["next_offset"] is None


def test_pages_of_different_evidence_versions_join_and_say_so() -> None:
    detail = _detail(4, phase8=3)
    detail["result_summary"]["present_paths"] = [f"/repo/path_{i}.py" for i in range(200)]
    pager_a = CartoJobDetailPager.from_detail(detail, budget=2048, evidence_version="sig-A")
    first = pager_a.page()
    assert first["detail_page"]["next_offset"] is not None, "the walk must need a second page"
    pager_b = CartoJobDetailPager.from_detail(detail, budget=2048, evidence_version="sig-B")
    rest = _walk_from(pager_b, first["detail_page"]["next_offset"], first["detail_page"]["next_slice_offset"] or 0)

    joined = join_carto_job_detail_pages([first, *rest])  # never raises
    assert joined["result_summary"]["files"] == detail["result_summary"]["files"]
    assert joined["evidence_version_mismatch"] == {"versions": ["sig-A", "sig-B"], "page_indices": [[0], list(range(1, 1 + len(rest)))]}


def _walk_from(pager: CartoJobDetailPager, offset: int, slice_offset: int) -> list[dict]:
    pages: list[dict] = []
    while True:
        page = pager.page(offset=offset, slice_offset=slice_offset)
        pages.append(page)
        cursor = page["detail_page"]
        offset, slice_offset = cursor["next_offset"], cursor["next_slice_offset"] or 0
        if offset is None:
            return pages
        assert len(pages) < 2000


def test_a_lazy_source_fetches_one_row_at_a_time_and_never_ahead() -> None:
    rows = [{"file": f"/repo/lazy_{i}.py", "status": "completed", "findings": [], "warnings": [], "events": []}
            for i in range(1000)]
    reads: list[tuple[int, int]] = []

    def loader(start: int, stop: int) -> list[dict]:
        reads.append((start, stop))
        return rows[start:stop]

    pager = CartoJobDetailPager(
        frame={"id": "lazy", "status": "running", "result_summary": {"files": []}},
        lists={"result_summary.files": LazyRowSource(len(rows), loader)},
        evidence_version="growing",
    )
    assert pager.total_units == 1001

    first = pager.page()
    assert len(first["result_summary"]["files"]) == 100
    # One row a time, exactly the rows this page carries: no window, no read ahead.
    assert all(stop - start == 1 for start, stop in reads), reads[:3]
    assert [start for start, stop in reads] == list(range(100)), reads

    reads.clear()
    second = pager.page(offset=first["detail_page"]["next_offset"])
    assert len(second["result_summary"]["files"]) == 100
    assert second["files_page"]["offset"] == 100
    assert [start for start, stop in reads] == list(range(100, 200)), reads

    # A walk over the lazy pager still joins to the complete detail.
    joined = join_carto_job_detail_pages(_walk(pager))
    assert len(joined["result_summary"]["files"]) == 1000
    assert joined["result_summary"]["files"][-1] == rows[-1]


def test_the_pager_does_not_mutate_or_remember_its_frame() -> None:
    detail = _detail(120)
    pager = CartoJobDetailPager.from_detail(detail)
    first = pager.page()
    assert len(first["result_summary"]["files"]) == 100

    # A second walk starts clean: the first page did not grow the frame.
    again = pager.page()
    assert len(again["result_summary"]["files"]) == 100
    assert again == first
    assert detail["result_summary"]["files"] == _detail(120)["result_summary"]["files"]


def test_join_never_mutates_the_pages_and_tolerates_an_incomplete_walk() -> None:
    detail = _detail(30)
    pager = CartoJobDetailPager.from_detail(detail, budget=700)
    pages = _walk(pager)
    snapshot = json.loads(json.dumps(pages))
    join_carto_job_detail_pages(pages)
    assert json.loads(json.dumps(pages)) == snapshot

    # A page without the frame: still a dict, no raise.
    partial = join_carto_job_detail_pages([pages[1]])
    assert isinstance(partial, dict)
    assert join_carto_job_detail_pages([]) == {}


def test_a_budget_below_the_cursor_still_advances() -> None:
    # The cursor is not droppable: below the page's own metadata the smallest
    # page that carries a cursor wins. It never stalls, never fails, and loses
    # nothing; the last unit's slice ends the walk (no empty page after it).
    detail = {"id": "floor", "error_extra": {"blob": "q" * 5000}}
    budget = 600
    pager = CartoJobDetailPager.from_detail(detail, budget=budget)
    pages = _walk(pager)
    assert 1 < len(pages) < 200
    assert all((page["detail_page"].get("slice") or {}).get("kind") == "frame" for page in pages)
    # Every page carries characters, and together they are the frame's own text.
    assert all(page["$slice"]["text"] for page in pages)
    assert "".join(page["$slice"]["text"] for page in pages) == unit_text(detach_detail_frame(detail))
    assert pages[-1]["detail_page"]["next_offset"] is None
    assert join_carto_job_detail_pages(pages) == detail
    assert [page["detail_page"]["offset"] for page in pages] == sorted(
        page["detail_page"]["offset"] for page in pages
    )


def test_a_cursor_at_the_end_is_an_empty_final_page() -> None:
    detail = _detail(2)
    pager = CartoJobDetailPager.from_detail(detail)
    end = pager.page(offset=pager.total_units)
    assert end["detail_page"]["next_offset"] is None
    assert end["detail_page"]["returned"] == 0
    assert end["truncated"] is False
    assert set(end) == {"detail_page", "files_page", "truncated"}


# ---------------------------------------------------------------------------
# Encoded units: the /2 path, read by range, never whole
# ---------------------------------------------------------------------------

def _encoded_source(texts: list[str], reads: list[tuple[int, int, int]],
                    asked: list[int] | None = None) -> object:
    """A source over unit texts, counting every range read (a byte-span stand-in)."""
    class _Source:
        def count(self) -> int:
            return len(texts)

        def encoded(self, index: int) -> EncodedJsonUnit:
            if asked is not None:
                asked.append(index)
            text = texts[index]

            def read_range(offset: int, count: int) -> str:
                chunk = text[offset:offset + count]
                reads.append((index, offset, len(chunk)))
                return chunk

            return EncodedJsonUnit(
                len(text), read_range, 2 if text.isascii() else 4,
                total_bytes=len(text.encode("utf-8")),
            )

    return _Source()


def test_a_giant_encoded_unit_is_read_once_per_slice_and_never_whole() -> None:
    giant = {
        "file": "/repo/giant.py",
        "status": "failed",
        "findings": [{"code": "parse", "message": "z" * 400} for _ in range(2000)],
    }
    texts = [unit_text({"file": "/repo/before.py", "status": "completed"}),
             unit_text(giant),
             unit_text({"file": "/repo/after.py", "status": "completed"})]
    assert len(texts[1]) > 500_000
    reads: list[tuple[int, int, int]] = []
    pager = CartoJobDetailPager(
        frame={"id": "encoded", "status": "failed", "result_summary": {"files": []}},
        lists={"result_summary.files": _encoded_source(texts, reads)},
        budget=8192, evidence_version="sig-encoded",
    )
    pages = _walk(pager)
    giant_reads = [(offset, count) for index, offset, count in reads if index == 1]

    assert len(giant_reads) > 3, "the giant unit must be sliced"
    # Every character read exactly once, each read one bounded range.
    assert sum(count for _offset, count in giant_reads) == len(texts[1])
    cursor = 0
    for offset, count in giant_reads:
        assert offset == cursor
        assert 0 < count <= 1 + 8192 // 2
        cursor += count
    _assert_every_page_fits(pages, 8192)
    assert join_carto_job_detail_pages(pages)["result_summary"]["files"][1] == giant


def test_the_pager_keeps_no_text_and_no_row_between_pages() -> None:
    sentinel = "SENTINEL-giant-row"
    giant = {"file": "/repo/giant.py",
             "findings": [{"message": sentinel + "x" * 80} for _ in range(1500)]}
    text = unit_text(giant)
    lived: list[weakref.ReferenceType] = []

    class _Source:
        def count(self) -> int:
            return 1

        def encoded(self, index: int) -> EncodedJsonUnit:
            row = _WeakDict(json.loads(text))   # a decoded row, like a value source's
            lived.append(weakref.ref(row))
            return EncodedJsonUnit(len(text), lambda offset, count: text[offset:offset + count])

    pager = CartoJobDetailPager(
        frame={"id": "hold", "status": "failed", "result_summary": {"files": []}},
        lists={"result_summary.files": _Source()}, budget=4096,
    )
    page = pager.page(offset=1)
    assert page["detail_page"]["slice"]["kind"] == "entry"
    gc.collect()
    assert lived and all(reference() is None for reference in lived)
    for slot in CartoJobDetailPager.__slots__:
        held = getattr(pager, slot)
        if isinstance(held, str):
            assert sentinel not in held and len(held) < 500
        elif isinstance(held, (list, tuple)):
            for item in held:
                assert not isinstance(item, str) or (sentinel not in item and len(item) < 500)


def test_an_encoded_source_is_read_exactly_for_the_rows_a_page_carries() -> None:
    rows = [{"file": f"/repo/{i}.py", "status": "completed", "findings": [], "warnings": [], "events": []}
            for i in range(50)]
    texts = [unit_text(row) for row in rows]
    reads: list[tuple[int, int, int]] = []
    asked: list[int] = []
    pager = CartoJobDetailPager(
        frame={"id": "ahead", "status": "running", "result_summary": {"files": []}},
        lists={"result_summary.files": _encoded_source(texts, reads, asked)},
        files_limit=5,
    )
    page = pager.page(offset=1)
    assert len(page["result_summary"]["files"]) == 5
    assert asked == [0, 1, 2, 3, 4], asked          # nothing ahead of the page
    assert reads == [(i, 0, len(texts[i])) for i in range(5)], reads


def test_value_and_encoded_sources_page_identically() -> None:
    detail = _detail(6, phase8=2)
    paths = [path for path in DETAIL_PAGED_LIST_PATHS if isinstance(_path_value(detail, path), list)]
    texts = {path: [unit_text(row) for row in _path_value(detail, path)] for path in paths}
    reads: list[tuple[int, int, int]] = []
    encoded = CartoJobDetailPager(
        frame=detach_detail_frame(detail),
        lists={path: _encoded_source(texts[path], reads) for path in paths},
        budget=2048,
    )
    values = CartoJobDetailPager.from_detail(detail, budget=2048)
    assert _walk(encoded) == _walk(values)


def _path_value(document: dict, path: tuple[str, ...]) -> object:
    node: object = document
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def test_an_incomplete_frame_walk_keeps_its_text_visible() -> None:
    detail = _detail(3)
    detail["error_extra"] = {"trace": "q" * 5000}
    frame_text = unit_text(detach_detail_frame(detail))
    pager = CartoJobDetailPager.from_detail(detail, budget=2048)
    first = pager.page()
    assert (first["detail_page"].get("slice") or {}).get("kind") == "frame"

    # Only the first frame slice: the bytes are visible, never silently dropped.
    joined = join_carto_job_detail_pages([first])
    assert joined["detail_pages_incomplete"] is True
    held = joined["$slice"]["text"]
    assert isinstance(held, str) and held
    assert frame_text.startswith(held)
    assert joined["$slice"]["offset"] == 0


class _OneEncodedUnit:
    """A one-row source over one encoded unit (the /2 source's shape)."""

    def __init__(self, unit: EncodedJsonUnit):
        self._unit = unit

    def count(self) -> int:
        return 1

    def encoded(self, index: int) -> EncodedJsonUnit:
        assert index == 0
        return self._unit


def _paged_values(detail: dict) -> dict[tuple[str, ...], list]:
    return {
        path: _path_value(detail, path)
        for path in DETAIL_PAGED_LIST_PATHS
        if isinstance(_path_value(detail, path), list)
    }


def test_an_encoded_frame_that_fits_is_read_once_and_carried() -> None:
    detail = _detail(4)
    frame_text = unit_text(detach_detail_frame(detail))
    reads: list[tuple[int, int]] = []

    def read_range(offset: int, count: int) -> str:
        reads.append((offset, count))
        return frame_text[offset:offset + count]

    pager = CartoJobDetailPager(
        frame=EncodedJsonUnit(len(frame_text), read_range),
        lists=_paged_values(detail),
        evidence_version="sig-frame",
    )
    page = pager.page()
    assert page["id"] == detail["id"]
    assert page["detail_page"]["evidence_version"] == "sig-frame"
    assert reads == [(0, len(frame_text))], reads       # one read, the small frame
    assert join_carto_job_detail_pages(_walk(pager)) == detail


def test_a_giant_encoded_frame_streams_by_range() -> None:
    detail = _detail(2)
    detail["error_extra"] = {"trace": "q" * 400_000}
    frame_text = unit_text(detach_detail_frame(detail))
    assert len(frame_text) > 400_000
    reads: list[tuple[int, int]] = []

    def read_range(offset: int, count: int) -> str:
        reads.append((offset, count))
        return frame_text[offset:offset + count]

    pager = CartoJobDetailPager(
        frame=EncodedJsonUnit(len(frame_text), read_range),
        lists=_paged_values(detail),
        budget=8192,
    )
    pages = _walk(pager)
    assert len(reads) > 3, "a 400 KB frame is sliced, not read whole"
    assert sum(count for _offset, count in reads) == len(frame_text)
    cursor = 0
    for offset, count in reads:
        assert offset == cursor
        assert 0 < count <= 1 + 8192 // 4
        cursor += count
    _assert_every_page_fits(pages, 8192)
    joined = join_carto_job_detail_pages(pages)
    assert joined == detail
    assert joined["error_extra"] == detail["error_extra"]


def test_lists_order_and_counts_need_no_frame_decode() -> None:
    def never(offset: int, count: int) -> str:
        raise AssertionError("the frame was read to serve a page that does not carry it")

    pager = CartoJobDetailPager(
        frame=EncodedJsonUnit(10_000_000, never),
        lists={
            "result_summary.errors": ["first list"],
            "result_summary.files": [{"file": "/a.py"}, {"file": "/b.py"}],
        },
        evidence_version="sig-order",
    )
    assert pager.total_units == 4
    assert pager.counts() == {
        ("result_summary", "errors"): 1,
        ("result_summary", "files"): 2,
    }
    first = pager.page(offset=1)
    assert first["result_summary"]["errors"] == ["first list"]
    assert list(first["result_summary"]) == ["errors", "files"]


def test_a_large_encoded_row_that_fits_stays_a_normal_row() -> None:
    row = {"file": "/repo/large.py", "status": "completed", "message": "m" * 130_000}
    text = unit_text(row)
    byte_count = len(text.encode("utf-8"))
    assert 130_000 < byte_count < 256 * 1024
    reads: list[tuple[int, int]] = []

    def read_range(offset: int, count: int) -> str:
        reads.append((offset, count))
        return text[offset:offset + count]

    pager = CartoJobDetailPager(
        frame={"id": "large", "status": "completed", "result_summary": {"files": []}},
        lists={"result_summary.files": _OneEncodedUnit(
            EncodedJsonUnit(len(text), read_range, 2, total_bytes=byte_count))},
        budget=256 * 1024,
    )
    page = pager.page(offset=1)
    assert "slice" not in page["detail_page"], page["detail_page"]
    assert page["result_summary"]["files"] == [row]
    assert reads == [(0, len(text))], reads          # one read, carried whole
    assert encoded_bytes(page) <= 256 * 1024


def test_a_near_budget_encoded_frame_is_carried_in_one_page() -> None:
    detail = {
        "id": "near",
        "status": "completed",
        "error_extra": {"trace": "q" * 200_000},
        "result_summary": {"files": [], "findings": [], "warnings": [], "events": [],
                           "files_changed": [], "errors": [], "refresh_errors": [],
                           "present_paths": [], "node_outcomes": []},
    }
    frame_text = unit_text(detach_detail_frame(detail))
    frame_bytes = len(frame_text.encode("utf-8"))
    # The slicing bound alone (4 bytes a character) would call this frame too
    # big; its exact byte count fits, so it is carried whole on one page.
    assert 4 * len(frame_text) > 256 * 1024 > frame_bytes
    reads: list[tuple[int, int]] = []

    def read_range(offset: int, count: int) -> str:
        reads.append((offset, count))
        return frame_text[offset:offset + count]

    pager = CartoJobDetailPager(
        frame=EncodedJsonUnit(len(frame_text), read_range, 4, total_bytes=frame_bytes),
        lists=_paged_values(detail),
        budget=256 * 1024,
    )
    page = pager.page()
    assert "slice" not in page["detail_page"], page["detail_page"]
    assert page["id"] == "near"
    assert page["detail_page"]["next_offset"] is None
    assert page["truncated"] is False
    assert reads == [(0, len(frame_text))], reads
    assert encoded_bytes(page) <= 256 * 1024
    assert join_carto_job_detail_pages([page]) == detail


def test_utf8_encoded_text_slices_with_four_bytes_a_character() -> None:
    row = {"file": "/repo/😀.py", "status": "completed", "message": "é" * 40_000}
    text = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    assert not text.isascii()
    reads: list[tuple[int, int]] = []

    def read_range(offset: int, count: int) -> str:
        reads.append((offset, count))
        return text[offset:offset + count]

    pager = CartoJobDetailPager(
        frame={"id": "utf8", "status": "completed", "result_summary": {"files": []}},
        lists={"result_summary.files": _OneEncodedUnit(EncodedJsonUnit(len(text), read_range))},
        budget=4096,
    )
    pages = _walk(pager)
    assert len(reads) > 3
    assert sum(count for _offset, count in reads) == len(text)
    assert max(count for _offset, count in reads) <= 1 + 4096 // 4
    _assert_every_page_fits(pages, 4096)
    assert join_carto_job_detail_pages(pages)["result_summary"]["files"] == [row]


def test_every_public_beacon_declares_its_role() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "workflowy_mcp" / "carto_job_paging.py"
    text = source.read_text(encoding="utf-8")
    blocks = text.split("# @beacon[")[1:]
    assert len(blocks) >= 4
    for block in blocks:
        head = block.split("# ]", 1)[0]
        assert "role=" in head, head
        assert "slice_labels=ra-carto-jobs" in head, head


# ---------------------------------------------------------------------------
# The summary view's preview
# ---------------------------------------------------------------------------

def _big_summary() -> dict:
    return {
        "id": "preview",
        "job_id": "preview",
        "type": "CARTO_REFRESH",
        "mode": "folder",
        "root_uuid": "root",
        "status": "failed",
        "outcome": "failed",
        "has_errors": True,
        "has_warnings": True,
        "has_findings": False,
        "error_kind": "io",
        "progress": {"completed_files": 3, "current_file": "/repo/c.py"},
        "error": "boom " * 150_000,
        "error_extra": {"trace": "t" * 2_000_000, "nested": {"values": list(range(500))}},
        "result_summary": {
            "files_refreshed": 3,
            "status": "failed",
            "errors": [f"error {i}" for i in range(50_000)],
            "warnings": ["w" * 400 for _ in range(200)],
            "files": [{"file": f"/repo/{i}.py"} for i in range(500)],
            "message": "done",
        },
    }


def test_summary_that_fits_is_returned_unchanged() -> None:
    summary = {"id": "small", "status": "completed", "result_summary": {"files_refreshed": 2}}
    out = preview_carto_job_summary(summary, budget=DETAIL_PAGE_MAX_BYTES)
    assert out == summary
    assert "previewed" not in out and "truncated" not in out


def test_summary_preview_fits_the_budget_and_never_touches_protected_keys() -> None:
    summary = _big_summary()
    original = json.loads(json.dumps(summary))
    out = preview_carto_job_summary(summary, budget=DETAIL_PAGE_MAX_BYTES)

    assert encoded_bytes(out) <= DETAIL_PAGE_MAX_BYTES
    assert out["truncated"] is True
    assert out["message"]
    previewed = out["previewed"]
    assert previewed, "a 2 MB value must be previewed"
    # The largest value went first, and its marker carries the original size.
    assert previewed["error_extra"] == encoded_bytes(original["error_extra"])
    assert out["error_extra"] == {"$in_detail": {"bytes": encoded_bytes(original["error_extra"])}}
    # Protected: identifiers, status, outcome, the facets, error_kind, progress.
    for key in ("id", "job_id", "type", "mode", "root_uuid", "status", "outcome",
                "has_errors", "has_warnings", "has_findings", "error_kind", "progress"):
        assert out[key] == original[key]
    assert out["result_summary"]["status"] == "failed"
    assert out["result_summary"]["files_refreshed"] == 3
    # Lists keep their leading entries; strings their leading characters.
    assert 0 <= len(out["result_summary"]["errors"]) < 50_000
    assert isinstance(out["error"], str) and len(out["error"]) < len(original["error"])
    assert len(out["result_summary"]["files"]) == 500
    # Nothing is dropped from the input.
    assert summary == original


def test_summary_preview_uses_progress_only_as_a_last_resort() -> None:
    big_progress = {
        "id": "p", "status": "running", "progress": {"current_file": "y" * 900_000, "completed_files": 1},
    }
    out = preview_carto_job_summary(big_progress, budget=DETAIL_PAGE_MAX_BYTES)
    assert encoded_bytes(out) <= DETAIL_PAGE_MAX_BYTES
    assert list(out["previewed"]) == ["progress.current_file"]
    assert out["progress"]["completed_files"] == 1
    assert len(out["progress"]["current_file"]) < 900_000

    # With a big non-progress value present, that one is shed first.
    both = {
        "id": "p", "status": "running",
        "progress": {"current_file": "y" * 300_000, "completed_files": 1},
        "error_extra": {"blob": "z" * 900_000},
    }
    out = preview_carto_job_summary(both, budget=DETAIL_PAGE_MAX_BYTES)
    assert encoded_bytes(out) <= DETAIL_PAGE_MAX_BYTES
    assert out["progress"] == both["progress"]                  # untouched
    assert out["error_extra"]["$in_detail"]["bytes"] == encoded_bytes(both["error_extra"])


def test_summary_preview_never_raises_even_for_an_impossible_budget() -> None:
    summary = _big_summary()
    out = preview_carto_job_summary(summary, budget=120)
    assert isinstance(out, dict)
    assert out["status"] == "failed"
    assert out["previewed"]
