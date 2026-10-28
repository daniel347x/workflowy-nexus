"""Paging and preview of one job's detail reply (JR2 · WaveSpec rev 3 §2.10.1).

Pure presentation. Nothing here opens a file, folds evidence, reads a record or
knows what a job is: the caller hands in the complete detail (or its frame plus
lazy row sources) and gets back one page at a time, each measured on its own
compact UTF-8 JSON encoding. ``carto_job_record`` re-exports the three public
names this module owns (``page_carto_job_detail``, ``join_carto_job_detail_pages``
and ``preview_carto_job_summary``); the slow path that must not materialise
every row is ``CartoJobDetailPager``, built from a frame plus list sources.

The shape of a page (§2.10.1):

* The view's content is an ordered sequence of units. Unit 0 is the **frame**:
  the complete detail with every paged list emptied to ``[]`` where it stands
  (a list that is ``None`` or absent stays as it is). Then the entries of each
  paged list, list by list, in the order the detail itself holds those lists
  (``paged_paths_in_document_order``).
* A page carries consecutive units from its cursor while the reply fits the
  budget, and at most ``files_limit`` rows (file rows and phase 8 file rows).
  The frame's keys appear only on the page that carries the frame; every paged
  list on a page holds only that page's entries, at its own path.
* A unit's text is a compact JSON document's text — arbitrary valid UTF-8, as
  the evidence files hold it (a value the pager holds uses ``unit_text``, the
  default ASCII escaping); ``json.loads`` gives the same value back. A unit is
  either such a value (:class:`EncodedJsonUnit.from_value`) or an
  :class:`EncodedJsonUnit` a source hands over by range — the ``/2`` path,
  backed by a piece tree's lengths and byte spans (a ``files.jsonl`` line, a
  frame, a moved value). The **frame** may be an encoded unit too. A page asks a
  source for one bounded range and nothing else — never the whole text of a unit
  it slices — and the pager keeps no text and no row between pages.
* Whether a unit is carried whole is decided by its **exact** byte count
  (``EncodedJsonUnit.total_bytes``) when the source knows it, so a large row or
  frame that fits stays an ordinary value; only a unit that cannot fit is
  carried alone, across as many pages as it needs, as consecutive character
  slices of its text: top-level ``"$slice": {text, offset, total}`` for the
  frame, or the list position holding ``{"$slice": {text, offset, total}}`` for
  an entry. ``detail_page.slice`` says which unit is being sliced; ``total`` is
  the source's own character count. A slice page reads exactly one bounded range
  (its chunk), sized so that the unit's own ``max_wire_bytes_per_char`` (4 for
  arbitrary UTF-8, 2 for text known to be ASCII) times the chunk, plus the
  page's metadata, still fits the budget — no re-measure, no second read.
* ``detail_page`` is the cursor the client walks:
  ``{offset, slice_offset, returned, total, next_offset, next_slice_offset,
  evidence_version, slice?, lists?}``; ``next_offset`` is ``None`` after the
  last page. ``lists`` — ``{path: [start_index, count]}`` — is present while the
  page carries entries, and is what lets :func:`join_carto_job_detail_pages`
  put them at their absolute indices once an entry has been carried across
  pages. ``total`` counts units: the frame plus every paged entry.
* ``files_page`` describes the page's file rows:
  ``{filter: "all", offset, limit, returned, total, next_offset}``.
* ``truncated`` and a one-line ``message`` are present while pages remain.

``join_carto_job_detail_pages`` walks those pages back into the complete detail,
deep-equal and in key order. It never raises and never drops evidence: a unit
whose slices do not add up (a caller walked the wrong cursor) stays as its
``$slice`` placeholder, and pages built from different evidence versions are
joined but flagged with ``evidence_version_mismatch`` (never refused, B3).

The budget is the reply's compact UTF-8 size, cursor metadata included. It is
meant for §2.10.1's 512 KiB; a budget smaller than the cursor metadata itself
(a few hundred bytes) cannot be met — the cursor is not droppable, so the
smallest page that still carries one wins. The pager never refuses and never
stalls: every page advances the cursor.

Nothing in this module is an authority: the caller decides what the summary
view's room is, and the record stays the job's status authority.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

#: §2.10.1's one byte budget for every one-job reply; the same number as
#: ``job_status_view.JOB_REPLY_MAX_BYTES``. Repeated here so this module stays
#: import-free of the view (and of ``config``).
DETAIL_PAGE_MAX_BYTES = 524_288

#: §2.10.1: at most this many rows per page (file rows and phase 8 file rows).
DETAIL_ROW_LIMIT = 100

#: The detail view's paged lists, in the order their entries are carried.
DETAIL_PAGED_LIST_PATHS: tuple[tuple[str, ...], ...] = (
    ("result_summary", "files"),
    ("result_summary", "findings"),
    ("result_summary", "warnings"),
    ("result_summary", "events"),
    ("result_summary", "files_changed"),
    ("result_summary", "errors"),
    ("result_summary", "refresh_errors"),
    ("result_summary", "present_paths"),
    ("result_summary", "node_outcomes"),
    ("result_summary", "phase8", "files"),
    ("unrecognized_lines",),
)

_PAGED_PATHS = frozenset(DETAIL_PAGED_LIST_PATHS)
#: Paths whose entries are "rows" for ``files_limit`` (and ``files_page``).
_ROW_LIST_PATHS = frozenset({("result_summary", "files"), ("result_summary", "phase8", "files")})
_FILES_PATH: tuple[str, ...] = ("result_summary", "files")
#: Page keys that are never part of the joined detail (§2.10.1).
PAGE_METADATA_KEYS = frozenset({"detail_page", "files_page", "truncated", "message", "parameter_warnings"})
#: Summary keys that are never previewed: the identifiers, the status, the
#: outcome, the facets and ``error_kind``. ``progress`` is not protected but is
#: only previewed as a last resort (contract B5).
SUMMARY_PROTECTED_KEYS = frozenset({
    "id", "job_id", "type", "mode", "root_uuid", "record_layout", "carto_job_file",
    "claim_id", "account_scope_id", "status", "outcome", "has_errors", "has_warnings",
    "has_findings", "error_kind",
})
#: Default worst-case bytes one character of a unit's text can cost inside a
#: page: the reply is encoded with ``ensure_ascii=False``, so a character costs
#: its UTF-8 length (at most 4). Text known to be ASCII may say 2.
_DEFAULT_WIRE_BYTES_PER_CHAR = 4

_SUMMARY_PREVIEW_MESSAGE = (
    'Reply over its byte budget: large values are previewed (see "previewed"); '
    'ask with view="detail" for the complete values, a page at a time.'
)


# ---------------------------------------------------------------------------
# Compact encoding and small JSON-path helpers
# ---------------------------------------------------------------------------

def compact_json(value: Any) -> str:
    """The one encoding every size here is measured on (§2.10.1)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def encoded_bytes(value: Any) -> int:
    """UTF-8 byte length of ``value``'s compact encoding."""
    return len(compact_json(value).encode("utf-8"))


def unit_text(value: Any) -> str:
    """One unit's text: compact JSON with default ASCII escaping (§2.4 B1).

    ASCII text is what the evidence files hold, and it makes a slice's cost
    predictable: one character costs at most two bytes inside a page (a quote
    or a backslash doubles; nothing else does), so a page can choose its chunk
    without reading or re-measuring anything. ``json.loads`` gives back exactly
    the value the text came from, Unicode included.
    """
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _key_bytes(key: str) -> int:
    return encoded_bytes(key)


def _path_text(path: Sequence[Any]) -> str:
    return ".".join(str(part) for part in path)


def path_parts(path: str | Sequence[str]) -> tuple[str, ...]:
    """A tuple of keys, from a dotted string or a sequence."""
    if isinstance(path, str):
        return tuple(part for part in path.split(".") if part)
    return tuple(str(part) for part in path)


def get_path(document: Any, path: Sequence[str], default: Any = None) -> Any:
    """The value at ``path`` or ``default`` when any level is missing."""
    node = document
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return default
        node = node[key]
    return node


def set_path(document: dict, path: Sequence[str], value: Any) -> None:
    """Set ``path`` in ``document``, creating missing containers, order intact."""
    node = document
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


def detach_detail_frame(detail: Any) -> dict:
    """The complete detail with every paged list emptied to ``[]`` in place.

    Only lists move: a paged key holding ``None``, or absent, stays exactly as
    the detail has it (§2.10.1). Non-paged lists are copied (their entries are
    shared, never mutated).
    """
    if not isinstance(detail, Mapping):
        return {"value": detail}

    def detach(value: Any, path: tuple[str, ...]) -> Any:
        if isinstance(value, Mapping):
            return {key: detach(item, path + (key,)) for key, item in value.items()}
        if isinstance(value, list):
            if path in _PAGED_PATHS:
                return []
            return [detach(item, path) for item in value]
        return value

    return {key: detach(value, (key,)) for key, value in detail.items()}


def paged_paths_in_document_order(detail: Any) -> list[tuple[str, ...]]:
    """The detail's paged lists, in the order the detail holds them."""
    found: list[tuple[str, ...]] = []

    def walk(node: Any, path: tuple[str, ...]) -> None:
        if not isinstance(node, Mapping):
            return
        for key, value in node.items():
            child = path + (key,)
            if child in _PAGED_PATHS and isinstance(value, list):
                found.append(child)
            elif isinstance(value, Mapping):
                walk(value, child)

    walk(detail, ())
    return found


def _ensure_list(page: dict, path: tuple[str, ...]) -> list:
    """The page's list at ``path``, created (with its containers) when absent."""
    node: Any = page
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    entries = node.get(path[-1])
    if not isinstance(entries, list):
        entries = []
        node[path[-1]] = entries
    return entries


def _missing_container_bytes(page: Mapping, path: tuple[str, ...]) -> int:
    """Bytes needed to add the empty list at ``path`` to ``page``."""
    cost = 0
    node: Any = page
    for index, key in enumerate(path):
        last = index == len(path) - 1
        if isinstance(node, Mapping) and key in node:
            node = node[key]
            continue
        cost += (0 if not node else 1) + _key_bytes(key) + 1 + 2
        if last:
            break
        node = {}
    return cost


# ---------------------------------------------------------------------------
# List sources: in-memory sequences, or rows loaded per page
# ---------------------------------------------------------------------------

class EncodedJsonUnit:
    """One pageable unit as its compact JSON text, readable by range.

    ``total_chars`` is supplied by the source (the text's length), so a page can
    decide whether the unit fits — and slice it — without reading it.
    ``read_range(offset, count)`` returns the text from character ``offset`` for
    at most ``count`` characters. The text is a JSON document's text: arbitrary
    valid UTF-8, with no raw control characters.

    This is the real ``/2`` path: a source backs ``read_range`` with a piece
    tree's lengths and byte spans (``files.jsonl`` line pieces, a frame's
    pieces, a moved value's pieces), so the pager reads only the characters a
    page carries — never the whole unit, never a prefix it then throws away.
    The pager keeps no text and no row between pages.

    Two size facts let a page decide without reading, and they answer different
    questions:

    * ``total_bytes`` — optional, the **exact** UTF-8 byte length of the text.
      It is what decides whether a unit is carried whole as an ordinary entry
      (or a frame is carried whole on its page): a 130 KiB row in a 256 KiB page
      is a row, not a slice. ``from_text`` computes it; a piece tree that knows
      its spans passes it. Without it, the wire bound below is used, which is
      safe but conservative.
    * ``max_wire_bytes_per_char`` — the worst-case cost of one character inside
      a page, used **only to size a slice** (never to decide whether a unit
      fits whole). The page is encoded with ``ensure_ascii=False``, where a
      character costs its UTF-8 length (at most 4) unless it is a quote or a
      backslash (2, both ASCII): 4 is safe for any JSON text, 2 for text known
      to be ASCII (``from_text`` works it out).
    """

    __slots__ = ("total_chars", "read_range", "max_wire_bytes_per_char", "total_bytes")

    def __init__(
        self,
        total_chars: int,
        read_range: Callable[[int, int], str],
        max_wire_bytes_per_char: int = _DEFAULT_WIRE_BYTES_PER_CHAR,
        *, total_bytes: int | None = None,
    ):
        if not callable(read_range):
            raise TypeError("read_range must be callable(offset, count)")
        self.total_chars = max(0, int(total_chars))
        self.read_range = read_range
        self.max_wire_bytes_per_char = max(1, int(max_wire_bytes_per_char))
        self.total_bytes = total_bytes

    @classmethod
    def from_text(cls, text: str) -> EncodedJsonUnit:
        """A unit over a text already in hand (an in-memory value's own text)."""
        escape = 2 if text.isascii() else _DEFAULT_WIRE_BYTES_PER_CHAR
        return cls(len(text), lambda offset, count: text[offset:offset + count], escape,
                   total_bytes=len(text.encode("utf-8")))

    @classmethod
    def from_value(cls, value: Any) -> EncodedJsonUnit:
        """A unit over one in-memory value, as its own compact ASCII JSON text."""
        return cls.from_text(unit_text(value))


class LazyRowSource:
    """A paged list's source: a count plus a loader that fetches rows.

    ``count`` may be an int or a zero-argument callable (a growing file's count
    is re-read per page). ``loader(start, stop)`` returns the entries in
    ``[start, stop)``; the pager asks for exactly the one row a page carries, so
    a cache holding one of these never materialises a row it does not place and
    never reads ahead.

    This is the value path. A source that must not build a giant row's text can
    instead expose ``count()`` and ``encoded(index) -> EncodedJsonUnit``; the
    pager prefers ``encoded`` when a source has it.
    """

    __slots__ = ("_count", "_loader")

    def __init__(self, count: int | Callable[[], int], loader: Callable[[int, int], Sequence[Any]]):
        if not callable(loader):
            raise TypeError("loader must be callable(start, stop)")
        self._count = count
        self._loader = loader

    def count(self) -> int:
        return int(self._count() if callable(self._count) else self._count)

    def slice(self, start: int, stop: int) -> Sequence[Any]:
        return self._loader(int(start), int(stop))


def _source_count(source: Any) -> int:
    if isinstance(source, (str, bytes)):
        return 0
    if isinstance(source, Sequence):
        return len(source)
    counter = getattr(source, "count", None)
    if callable(counter):
        try:
            return int(counter())
        except TypeError:  # a list's .count(value), should it ever be reached
            pass
    return len(source)  # type: ignore[arg-type]


def _source_slice(source: Any, start: int, stop: int) -> Sequence[Any]:
    if isinstance(source, (str, bytes)):
        return ()
    if isinstance(source, Sequence):
        return source[start:stop]
    slicer = getattr(source, "slice", None)
    if callable(slicer):
        return slicer(start, stop)
    return source[start:stop]  # type: ignore[index,return-value]


# ---------------------------------------------------------------------------
# The pager
# ---------------------------------------------------------------------------

# @beacon[
#   id=carto-paging@pager,
#   role=CartoJobDetailPager(frame, lists, files_limit, budget, evidence_version),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=Serves lossless frame/list units with one bounded read per sliced page and no row cache; each call's row limit and envelope budget are reflected in its metadata without re-folding evidence. Frame keys occur only on their own page and the last unit ends the walk.,
# ]
class CartoJobDetailPager:
    """Serves the detail view's pages from a frame plus per-list sources.

    ``frame`` is the complete detail with its paged lists emptied to ``[]``
    (``detach_detail_frame``) — a mapping, or an :class:`EncodedJsonUnit` when
    the frame itself is too big to hold (a FILE detail's frame can be most of a
    giant row, and a moved value can make any frame giant). An encoded frame is
    decoded only when the frame's page carries it and it fits; otherwise it is
    sliced by range like any other unit. Its paged lists' *order and counts*
    never need the frame: they come from ``lists``.

    ``lists`` maps each paged path — a dotted string such as
    ``"result_summary.files"`` or a tuple — to its entries (a
    :class:`~collections.abc.Sequence`, a :class:`LazyRowSource`, or a source
    with ``encoded(index)``). A page reads only what it carries, so a cache that
    keeps the frame and the sources can serve every page without holding a row.
    A path outside :data:`DETAIL_PAGED_LIST_PATHS` is ignored: such a value
    stays in the frame, exactly as the detail view leaves it.

    The one-shot path is :meth:`from_detail`, which derives the frame and the
    list sources from one complete detail.
    Per-call row limits and warning-envelope room borrow these same sources;
    they do not rebuild the evidence index or retain a second row copy.
    """

    __slots__ = ("_frame", "_sources", "_files_limit", "_budget", "_evidence_version")

    def __init__(
        self,
        *,
        frame: Mapping[str, Any] | EncodedJsonUnit,
        lists: Mapping[str | Sequence[str], Any],
        files_limit: int = DETAIL_ROW_LIMIT,
        budget: int = DETAIL_PAGE_MAX_BYTES,
        evidence_version: str | None = None,
    ):
        if isinstance(frame, EncodedJsonUnit):
            self._frame: Mapping[str, Any] | EncodedJsonUnit = frame
        elif isinstance(frame, Mapping):
            self._frame = dict(frame)
        else:
            self._frame = {"value": frame}
        sources: dict[tuple[str, ...], Any] = {}
        for key, source in (lists or {}).items():
            lists_path = path_parts(key)
            if lists_path and lists_path in _PAGED_PATHS:
                sources[lists_path] = source
        # The caller's order is the document's order: the detail view carries
        # each paged list's entries in the order the lists appear. A path
        # outside the contract's set is ignored (it stays whole in the frame).
        self._sources = [(path, sources[path]) for path in sources if path in _PAGED_PATHS]
        self._files_limit = max(1, int(files_limit))
        self._budget = int(budget)
        self._evidence_version = evidence_version

    # -- construction -----------------------------------------------------

    @classmethod
    def from_detail(
        cls,
        detail: Any,
        *,
        files_limit: int = DETAIL_ROW_LIMIT,
        budget: int = DETAIL_PAGE_MAX_BYTES,
        evidence_version: str | None = None,
    ) -> CartoJobDetailPager:
        """A pager over one complete detail (every row already in memory)."""
        if not isinstance(detail, Mapping):
            return cls(frame={"value": detail}, lists={}, files_limit=files_limit,
                       budget=budget, evidence_version=evidence_version)
        lists: dict[tuple[str, ...], Any] = {}
        for path in paged_paths_in_document_order(detail):
            lists[path] = get_path(detail, path)
        return cls(frame=detach_detail_frame(detail), lists=lists, files_limit=files_limit,
                   budget=budget, evidence_version=evidence_version)

    # -- geometry ---------------------------------------------------------

    @property
    def budget(self) -> int:
        return self._budget

    @property
    def files_limit(self) -> int:
        return self._files_limit

    @property
    def evidence_version(self) -> str | None:
        return self._evidence_version

    def counts(self) -> dict[tuple[str, ...], int]:
        """Each paged list's entry count, read fresh on every page."""
        return {path: max(0, _source_count(source)) for path, source in self._sources}

    @property
    def total_units(self) -> int:
        return 1 + sum(self.counts().values())

    def _unit_of(self, path: tuple[str, ...], index: int, counts: Mapping[tuple[str, ...], int]) -> int:
        position = 1
        for candidate, _source in self._sources:
            if candidate == path:
                return position + index
            position += counts.get(candidate, 0)
        return position + index

    def _locate(
        self, offset: int, counts: Mapping[tuple[str, ...], int],
    ) -> tuple[str, tuple[str, ...] | None, int | None]:
        if offset <= 0:
            return ("frame", None, None)
        remaining = offset - 1
        for path, _source in self._sources:
            count = counts.get(path, 0)
            if remaining < count:
                return ("entry", path, remaining)
            remaining -= count
        return ("end", None, None)

    def _row_cursor(self, offset: int, counts: Mapping[tuple[str, ...], int]) -> int:
        """File rows fully behind the unit cursor (``files_page.offset``)."""
        if offset <= 0:
            return 0
        position = 1
        for path, _source in self._sources:
            count = counts.get(path, 0)
            if path == _FILES_PATH:
                return max(0, min(count, offset - position))
            position += count
        return counts.get(_FILES_PATH, 0)

    def _entry(self, path: tuple[str, ...], index: int) -> Any:
        for candidate, source in self._sources:
            if candidate == path:
                window = _source_slice(source, index, index + 1)
                return window[0] if window else None
        return None

    def _frame_value(self, room: int) -> Mapping[str, Any] | None:
        """The frame to carry on the frame's page, or ``None`` to slice it.

        The fit is decided on the frame's **exact** byte count when the unit
        knows it (``total_bytes``), exactly as an ordinary entry is: a frame
        nearly the page's size is carried whole, even though the slicing bound
        (4 bytes a character) alone would call it too big. The wire bound is
        only what a *slice* is sized by.
        """
        frame = self._frame
        if isinstance(frame, Mapping):
            return frame
        needed = frame.total_bytes if frame.total_bytes is not None else (
            frame.max_wire_bytes_per_char * frame.total_chars
        )
        if needed + 2 > room:
            return None  # bigger than an empty page can carry: slice it
        text = frame.read_range(0, frame.total_chars)  # one read
        value, decoded = _decode_text(text)
        return value if decoded and isinstance(value, Mapping) else None

    def _source_for(self, path: tuple[str, ...]) -> Any:
        for candidate, source in self._sources:
            if candidate == path:
                return source
        return None

    @staticmethod
    def _is_encoded(source: Any) -> bool:
        """True when the source hands over unit text instead of values."""
        return callable(getattr(source, "encoded", None))

    def _encoded_unit(self, path: tuple[str, ...], index: int) -> EncodedJsonUnit | None:
        source = self._source_for(path)
        if source is None or not self._is_encoded(source):
            return None
        unit = source.encoded(index)
        if unit is None:
            return None
        if isinstance(unit, EncodedJsonUnit):
            return unit
        return EncodedJsonUnit(int(unit.total_chars), unit.read_range)

    # -- pages ------------------------------------------------------------

    def page(self, *, offset: int = 0, slice_offset: int = 0, files_limit: int | None = None,
             budget: int | None = None) -> dict:
        """One page using this call's limit/budget, without copying or folding row sources."""
        budget = self._budget if budget is None else int(budget)
        row_limit = self._files_limit if files_limit is None else max(1, int(files_limit))
        if budget != self._budget or row_limit != self._files_limit:
            # A cheap presentation instance borrows the SAME frame and sources.
            # In particular a cached /2 pager reports this request's limit, not
            # the limit of the request that first opened the evidence index.
            return CartoJobDetailPager(frame=self._frame, lists=dict(self._sources),
                files_limit=row_limit, budget=budget, evidence_version=self._evidence_version).page(
                    offset=offset, slice_offset=slice_offset)
        offset = max(0, int(offset or 0))
        slice_offset = max(0, int(slice_offset or 0))
        counts = self.counts()
        total_units = 1 + sum(counts.values())

        if offset == 0 and slice_offset > 0:
            return self._slice_frame_page(slice_offset, counts, budget)
        if offset >= total_units:
            return self._empty_page(offset, counts)
        if slice_offset > 0:
            # Resuming a sliced entry: the cursor names the unit, not a page.
            kind, path, index = self._locate(offset, counts)
            if kind == "entry" and path is not None and index is not None:
                return self._slice_entry_page(path, index, slice_offset, counts, budget)
            return self._empty_page(offset, counts)

        reserve = self._metadata_reserve()
        body: dict[str, Any] = {}
        if offset == 0:
            frame = self._frame_value(budget - reserve)
            if frame is None:
                return self._slice_frame_page(0, counts, budget)
            # A structural copy: entries are appended into the frame's paged
            # lists, and the pager's own frame must never grow a row.
            body = _structural_copy(frame)
            if encoded_bytes(body) > budget - reserve:
                return self._slice_frame_page(0, counts, budget)

        size = encoded_bytes(body)
        accepted: list[tuple[tuple[str, ...], int]] = []
        carried: dict[tuple[str, ...], int] = {}
        rows = 0
        room = budget - reserve
        cursor = offset + (1 if offset == 0 else 0)
        while cursor < total_units:
            kind, path, index = self._locate(cursor, counts)
            if kind != "entry" or path is None or index is None:
                cursor += 1
                continue
            if path in _ROW_LIST_PATHS and rows >= row_limit:
                break
            source = self._source_for(path)
            container = 0 if carried.get(path) else _missing_container_bytes(body, path)
            if source is not None and self._is_encoded(source):
                unit = self._encoded_unit(path, index)
                if unit is None:
                    entry: Any = None
                    cost = container + len("null")
                elif size + container + (unit.total_bytes if unit.total_bytes is not None else
                                        unit.max_wire_bytes_per_char * unit.total_chars) + 2 > room:
                    # Cannot fit this page even at its best case: do not read it
                    # here. A page of its own slices it, one bounded read a page.
                    if not accepted and offset != 0:
                        return self._slice_entry_page(path, index, 0, counts, budget)
                    break
                else:
                    text = unit.read_range(0, unit.total_chars)   # one read
                    value, decoded = _decode_text(text)
                    entry = value if decoded else {
                        "$slice": {"text": text, "offset": 0, "total": unit.total_chars},
                    }
                    cost = container + (1 if carried.get(path) else 0) + encoded_bytes(entry)
            else:
                entry = self._entry(path, index)
                cost = container + (1 if carried.get(path) else 0) + encoded_bytes(entry)
            if size + cost > room:
                if not accepted and offset != 0:
                    return self._slice_entry_page(path, index, 0, counts, budget)
                break
            _ensure_list(body, path).append(entry)
            size += cost
            accepted.append((path, index))
            carried[path] = carried.get(path, 0) + 1
            cursor += 1
            if path in _ROW_LIST_PATHS:
                rows += 1

        page = self._finish_page(body, offset, slice_offset, accepted, cursor, total_units, counts, rows)
        while encoded_bytes(page) > budget and accepted:
            # The reservation should make this unreachable; if a pathological
            # frame or a huge int ever beats it, give back the last unit.
            path, _index = accepted.pop()
            entries = get_path(page, path)
            if isinstance(entries, list) and entries:
                entries.pop()
            cursor -= 1
            if path in _ROW_LIST_PATHS:
                rows -= 1
            page = self._finish_page(body, offset, slice_offset, accepted, cursor, total_units, counts, rows)
        return page

    def _finish_page(
        self,
        body: dict,
        offset: int,
        slice_offset: int,
        accepted: Sequence[tuple[tuple[str, ...], int]],
        cursor: int,
        total_units: int,
        counts: Mapping[tuple[str, ...], int],
        rows: int,
    ) -> dict:
        returned = len(accepted) + (1 if offset == 0 else 0)
        next_offset = cursor if cursor < total_units else None
        page = dict(body)
        detail_meta: dict[str, Any] = {
            "offset": offset,
            "slice_offset": slice_offset,
            "returned": returned,
            "total": total_units,
            "next_offset": next_offset,
            "next_slice_offset": 0 if next_offset is not None else None,
            "evidence_version": self._evidence_version,
        }
        spans: dict[str, list[int]] = {}
        for path, index in accepted:
            span = spans.setdefault(_path_text(path), [index, 0])
            span[0] = min(span[0], index)
            span[1] += 1
        if spans:
            # Where each paged list's entries sit: the join needs absolute
            # indices as soon as one entry has been carried across pages.
            detail_meta["lists"] = spans
        page["detail_page"] = detail_meta
        files_total = counts.get(_FILES_PATH, 0)
        files_offset = self._row_cursor(offset, counts)
        files_returned = sum(path == _FILES_PATH for path, _index in accepted)
        files_next = files_offset + files_returned
        page["files_page"] = {
            "filter": "all",
            "offset": files_offset,
            "limit": self._files_limit,
            "returned": files_returned,
            "total": files_total,
            "next_offset": files_next if files_next < files_total else None,
        }
        page["truncated"] = next_offset is not None
        page["message"] = (
            f"Page limit {self._budget} bytes: continue with offset={next_offset}, "
            f"slice_offset={page['detail_page']['next_slice_offset']}."
            if next_offset is not None else None
        )
        if page["message"] is None:
            del page["message"]
        return page

    def _empty_page(self, offset: int, counts: Mapping[tuple[str, ...], int]) -> dict:
        """A cursor already at the end: no content, no next page."""
        return {
            "detail_page": {
                "offset": offset,
                "slice_offset": 0,
                "returned": 0,
                "total": 1 + sum(counts.values()),
                "next_offset": None,
                "next_slice_offset": None,
                "evidence_version": self._evidence_version,
            },
            "files_page": {
                "filter": "all",
                "offset": counts.get(_FILES_PATH, 0),
                "limit": self._files_limit,
                "returned": 0,
                "total": counts.get(_FILES_PATH, 0),
                "next_offset": None,
            },
            "truncated": False,
        }

    def _metadata_reserve(self) -> int:
        """Worst-case bytes the page's metadata can add (cursor values vary)."""
        placeholder = {
            "detail_page": {
                "offset": 9_999_999_999,
                "slice_offset": 9_999_999_999,
                "returned": 9_999_999_999,
                "total": 9_999_999_999,
                "next_offset": 9_999_999_999,
                "next_slice_offset": 9_999_999_999,
                "evidence_version": self._evidence_version,
            },
            "files_page": {
                "filter": "all",
                "offset": 9_999_999_999,
                "limit": 9_999_999_999,
                "returned": 9_999_999_999,
                "total": 9_999_999_999,
                "next_offset": 9_999_999_999,
            },
            "truncated": True,
            "message": "x" * 240,
        }
        placeholder["detail_page"]["lists"] = {
            _path_text(path): [9_999_999_999, 9_999_999_999] for path in DETAIL_PAGED_LIST_PATHS
        }
        return encoded_bytes(placeholder) + 1  # +1: the comma before the metadata

    # -- slices -----------------------------------------------------------

    def _slice_frame_page(self, slice_offset: int, counts: Mapping, budget: int) -> dict:
        frame = self._frame
        unit = frame if isinstance(frame, EncodedJsonUnit) else EncodedJsonUnit.from_value(frame)
        return self._sliced_page(
            kind="frame", path=None, index=None, unit_offset=0,
            unit=unit, start=slice_offset,
            counts=counts, budget=budget,
        )

    def _slice_entry_page(
        self,
        path: tuple[str, ...],
        index: int,
        slice_offset: int,
        counts: Mapping,
        budget: int,
    ) -> dict:
        unit_offset = self._unit_of(path, index, counts)
        source = self._source_for(path)
        if source is not None and self._is_encoded(source):
            unit = self._encoded_unit(path, index)
            if unit is None:
                unit = EncodedJsonUnit.from_value(None)
        else:
            unit = EncodedJsonUnit.from_value(self._entry(path, index))
        return self._sliced_page(
            kind="entry", path=path, index=index, unit_offset=unit_offset,
            unit=unit, start=slice_offset, counts=counts, budget=budget,
        )

    def _sliced_page(
        self,
        *,
        kind: str,
        path: tuple[str, ...] | None,
        index: int | None,
        unit_offset: int,
        unit: EncodedJsonUnit,
        start: int,
        counts: Mapping,
        budget: int,
    ) -> dict:
        """One slice of one oversized unit: ONE bounded read, never a re-measure."""
        total = unit.total_chars
        start = min(max(0, int(start)), total)
        total_units = 1 + sum(counts.values())

        def build(chunk: str, done: bool, worst: bool = False) -> dict:
            page: dict[str, Any] = {}
            holder = {"$slice": {"text": chunk, "offset": start, "total": total}}
            if kind == "frame":
                page["$slice"] = holder["$slice"]
            elif path is not None:
                _ensure_list(page, path).append(holder)
            page["detail_page"] = {
                "offset": unit_offset,
                "slice_offset": start,
                "returned": 1,
                "total": total_units,
                "next_offset": (
                    None if done and unit_offset + 1 >= total_units
                    else (unit_offset + 1 if done else unit_offset)
                ),
                "next_slice_offset": 0 if done else (9_999_999_999 if worst else start + len(chunk)),
                "evidence_version": self._evidence_version,
                "slice": {
                    "kind": kind,
                    "list": _path_text(path) if path is not None else None,
                    "index": index,
                    "offset": start,
                    "total": total,
                },
            }
            files_total = counts.get(_FILES_PATH, 0)
            files_offset = self._row_cursor(unit_offset, counts)
            page["files_page"] = {
                "filter": "all",
                "offset": files_offset,
                "limit": self._files_limit,
                "returned": 0,
                "total": files_total,
                "next_offset": files_offset if files_offset < files_total else None,
            }
            truncated = (not done) or unit_offset + 1 < total_units
            page["truncated"] = truncated
            if truncated:
                page["message"] = (
                    f"Row larger than one {budget}-byte page: continue with "
                    f"offset={page['detail_page']['next_offset']}, "
                    f"slice_offset={page['detail_page']['next_slice_offset']}."
                )
            return page

        # What the page costs with nothing in the text, at the widest cursor
        # numbers it can carry; every character then costs at most the unit's
        # own wire bound, and the chunk is chosen so that it still fits.
        overhead = encoded_bytes(build("", False, worst=True))
        allowed = max(0, budget - overhead)
        count = min(total - start, max(1, allowed // unit.max_wire_bytes_per_char)) if total > start else 0
        chunk = unit.read_range(start, count)[:count] if count else ""
        done = start + len(chunk) >= total or not chunk
        return build(chunk, done)


# ---------------------------------------------------------------------------
# The two public functions of the detail view
# ---------------------------------------------------------------------------

# @beacon[
#   id=carto-paging@detail-page,
#   role=page_carto_job_detail(detail, offset, slice_offset, files_limit, evidence_version, budget),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=One page of the detail view (WaveSpec §2.10.1): frame first, then list entries one list at a time, at most files_limit file rows, a unit too large for a page sliced alone across pages. Frame keys only on the frame's page; each paged list holds only that page's entries; every reply measured on compact UTF-8 JSON and capped at the budget.,
# ]
def page_carto_job_detail(
    detail: Mapping[str, Any],
    *,
    offset: int = 0,
    slice_offset: int = 0,
    files_limit: int = DETAIL_ROW_LIMIT,
    evidence_version: str | None = None,
    budget: int = DETAIL_PAGE_MAX_BYTES,
) -> dict:
    """One page of the complete detail (§2.10.1); a small detail comes whole."""
    pager = CartoJobDetailPager.from_detail(
        detail, files_limit=files_limit, budget=budget, evidence_version=evidence_version,
    )
    return pager.page(offset=offset, slice_offset=slice_offset, files_limit=files_limit)


# @beacon[
#   id=carto-paging@join,
#   role=join_carto_job_detail_pages(pages),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=The detail view's pure join (WaveSpec §2.10.1): concatenate each unit's slices, decode, and splice every paged list back at its path, giving the complete detail deep-equal and in key order. Page keys are dropped; evidence versions across pages are flagged in evidence_version_mismatch rather than refused, and an incomplete unit keeps its $slice placeholder instead of losing bytes.,
# ]
def join_carto_job_detail_pages(pages: Sequence[Mapping[str, Any]]) -> dict:
    """Rebuild the complete detail from the detail view's pages, in order.

    Never raises and never drops evidence: a page whose cursor says a unit was
    sliced keeps its placeholder when the slices do not add up, and pages built
    from different evidence versions are still joined (with
    ``evidence_version_mismatch`` naming the versions and their page indices).
    """
    frame: dict[str, Any] | None = None
    frame_chunks: list[str] = []
    frame_total: int | None = None
    entries: dict[tuple[str, ...], list[Any]] = {}
    open_slices: dict[tuple[tuple[str, ...] | None, int | None], str] = {}
    slice_totals: dict[tuple[tuple[str, ...] | None, int | None], int] = {}
    versions: list[Any] = []

    for _page_index, page in enumerate(pages or ()):
        if not isinstance(page, Mapping):
            continue
        meta = page.get("detail_page")
        if not isinstance(meta, Mapping):
            meta = {}
        if "evidence_version" in meta:
            versions.append(meta.get("evidence_version"))
        sliced = meta.get("slice")
        if isinstance(sliced, Mapping):
            kind = sliced.get("kind")
            if kind == "frame":
                holder = page.get("$slice")
                if isinstance(holder, Mapping) and isinstance(holder.get("text"), str):
                    frame_chunks.append(holder["text"])
                    total = holder.get("total")
                    if isinstance(total, int):
                        frame_total = total
            else:
                path = path_parts(sliced.get("list") or ())
                index = sliced.get("index")
                holder = _find_slice_holder(page, path)
                if path and isinstance(index, int) and isinstance(holder, Mapping) and isinstance(holder.get("text"), str):
                    key = (path, index)
                    open_slices[key] = open_slices.get(key, "") + holder["text"]
                    total = holder.get("total")
                    if isinstance(total, int):
                        slice_totals[key] = total
            continue

        body = {key: value for key, value in page.items()
                if key not in PAGE_METADATA_KEYS and key != "$slice"}
        spans = meta.get("lists")
        placed = False
        if isinstance(spans, Mapping):
            for list_text, span in spans.items():
                path = path_parts(list_text)
                value = get_path(body, path)
                if not path or not isinstance(value, list):
                    continue
                start = span[0] if isinstance(span, (list, tuple)) and span else 0
                _place_entries(entries.setdefault(path, []), int(start), value)
                placed = True
        if meta.get("offset") in (0, None) and body:
            frame = _shallow_clone(body)
            for path in DETAIL_PAGED_LIST_PATHS:
                if isinstance(get_path(frame, path), list):
                    entries.setdefault(path, [])
                    set_path(frame, path, [])
            if not placed:
                for path in DETAIL_PAGED_LIST_PATHS:
                    value = get_path(body, path)
                    if isinstance(value, list) and value:
                        _place_entries(entries.setdefault(path, []), 0, value)
        elif not placed:
            for path in DETAIL_PAGED_LIST_PATHS:
                value = get_path(body, path)
                if isinstance(value, list) and value:
                    _place_entries(entries.setdefault(path, []), 0, value)

    if frame is None:
        frame_text = "".join(frame_chunks)
        if frame_text:
            decoded, ok = _decode_text(frame_text)
            if ok and isinstance(decoded, dict):
                frame = decoded
            else:
                # A wrong cursor or an unreadable span: the bytes stay visible
                # rather than being dropped into an empty detail.
                frame = {
                    "$slice": {
                        "text": frame_text,
                        "offset": 0,
                        "total": frame_total if frame_total is not None else len(frame_text),
                    },
                    "detail_pages_incomplete": True,
                }
        else:
            frame = {}

    for (path, index), text in open_slices.items():
        if path is None or not isinstance(index, int):
            continue
        total = slice_totals.get((path, index))
        decoded, ok = _decode_text(text)
        if ok and (total is None or len(text) >= total):
            bucket = entries.setdefault(path, [])
            while len(bucket) <= index:
                bucket.append(None)
            bucket[index] = decoded
        else:
            bucket = entries.setdefault(path, [])
            while len(bucket) <= index:
                bucket.append(None)
            bucket[index] = {"$slice": {"text": text, "offset": 0, "total": total if total is not None else len(text)}}

    for path in DETAIL_PAGED_LIST_PATHS:
        if path in entries:
            set_path(frame, path, entries[path])

    distinct: list[Any] = []
    indices: list[list[int]] = []
    for page_index, version in enumerate(versions):
        if version not in distinct:
            distinct.append(version)
            indices.append([])
        indices[distinct.index(version)].append(page_index)
    if len(distinct) > 1:
        frame["evidence_version_mismatch"] = {"versions": distinct, "page_indices": indices}
    return frame


def _place_entries(bucket: list[Any], start: int, values: Sequence[Any]) -> None:
    """Put ``values`` at their absolute indices, leaving gaps as ``None``."""
    if start < 0:
        start = 0
    while len(bucket) < start:
        bucket.append(None)
    for offset, item in enumerate(values):
        position = start + offset
        if position < len(bucket):
            bucket[position] = item
        else:
            bucket.append(item)


def _find_slice_holder(page: Mapping, path: tuple[str, ...]) -> Any:
    node: Any = page
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    if isinstance(node, list):
        for item in node:
            if isinstance(item, Mapping) and set(item) == {"$slice"}:
                return item["$slice"]
    return None


def _decode_text(text: str) -> tuple[Any, bool]:
    try:
        return json.loads(text), True
    except (ValueError, TypeError):
        return text, False


# ---------------------------------------------------------------------------
# The summary view's preview
# ---------------------------------------------------------------------------

# @beacon[
#   id=carto-paging@summary-preview,
#   role=preview_carto_job_summary(summary, budget),
#   slice_labels=ra-carto-jobs,
#   kind=ast,
#   comment=Presentation-only preview of a summary view that would exceed its room (WaveSpec §2.10.1): largest value first in two tiers (result_summary's values, then progress as a last resort), a list keeps the leading entries that fit, a string its leading characters, any other mapping becomes {$in_detail:{bytes:n}}; identifiers, status, outcome, the facets and error_kind are never touched, and previewed names each value's original size. The complete values stay in the detail view.,
# ]
def preview_carto_job_summary(summary: Mapping[str, Any], *, budget: int = DETAIL_PAGE_MAX_BYTES) -> dict:
    """The summary view, previewed until it fits ``budget`` (never refused)."""
    if not isinstance(summary, Mapping):
        return summary  # type: ignore[return-value]
    budget = int(budget)
    document = _shallow_clone(summary)
    size = encoded_bytes(document)
    if size <= budget:
        return document

    previewed: dict[str, int] = {}
    candidates = _preview_candidates(document)
    # Room for the bookkeeping the previews themselves add.
    reserve = encoded_bytes({
        "previewed": {_path_text(path): 99_999_999 for path, _size in candidates},
        "truncated": True,
        "message": _SUMMARY_PREVIEW_MESSAGE,
    })
    for path, original_bytes in candidates:
        if size + reserve <= budget:
            break
        container = get_path(document, path[:-1])
        if not isinstance(container, dict) or path[-1] not in container:
            continue
        value = container[path[-1]]
        target = original_bytes - (size + reserve - budget)
        if isinstance(value, list):
            replacement: Any = _prefix_list(value, target)
        elif isinstance(value, str):
            replacement = _prefix_text(value, target)
        elif isinstance(value, Mapping):
            replacement = {"$in_detail": {"bytes": original_bytes}}
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and original_bytes > 64:
            replacement = {"$in_detail": {"bytes": original_bytes}}
        else:
            continue
        if replacement == value and type(replacement) is type(value):
            continue
        container[path[-1]] = replacement
        previewed[_path_text(path)] = original_bytes
        size = encoded_bytes(document)

    if previewed:
        document["previewed"] = previewed
        document["truncated"] = True
        document["message"] = _SUMMARY_PREVIEW_MESSAGE
    return document


def _shallow_clone(value: Any) -> Any:
    """Copy the containers it will edit; leaves are shared and never mutated."""
    if isinstance(value, Mapping):
        return {key: _shallow_clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return list(value)
    return value


def _structural_copy(value: Any) -> Any:
    """A copy deep enough that appending a row cannot reach the original."""
    if isinstance(value, Mapping):
        return {key: _structural_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_structural_copy(item) for item in value]
    return value


def _preview_candidates(document: Mapping[str, Any]) -> list[tuple[tuple[str, ...], int]]:
    """Previewable values, largest first; ``progress`` only as a last resort."""
    primary: list[tuple[tuple[str, ...], int]] = []
    fallback: list[tuple[tuple[str, ...], int]] = []
    for key, value in document.items():
        if key in SUMMARY_PROTECTED_KEYS or key in ("previewed", "truncated", "message"):
            continue
        if key in ("result_summary", "progress") and isinstance(value, Mapping):
            # Their own values are the candidates, largest first; a summary
            # that carries one of these as a non-mapping is a value like any other.
            continue
        if isinstance(value, (list, str, Mapping)):
            primary.append(((key,), encoded_bytes(value)))
    result_summary = document.get("result_summary")
    if isinstance(result_summary, Mapping):
        for key, value in result_summary.items():
            if key in SUMMARY_PROTECTED_KEYS:
                continue
            if isinstance(value, (list, str, Mapping)):
                primary.append((("result_summary", key), encoded_bytes(value)))
    progress = document.get("progress")
    if isinstance(progress, Mapping):
        for key, value in progress.items():
            if isinstance(value, (list, str, Mapping)):
                fallback.append((("progress", key), encoded_bytes(value)))
    primary.sort(key=lambda row: (-row[1], row[0]))
    fallback.sort(key=lambda row: (-row[1], row[0]))
    return primary + fallback


def _prefix_list(items: Sequence[Any], target_bytes: int) -> list[Any]:
    """The longest leading slice of ``items`` whose encoded list fits."""
    if not items:
        return []
    if encoded_bytes([]) > target_bytes:
        return []
    low, high, best = 0, len(items), 0
    while low <= high:
        middle = (low + high) // 2
        if encoded_bytes(items[:middle]) <= target_bytes:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return list(items[:best])


def _prefix_text(text: str, target_bytes: int) -> str:
    """The longest leading slice of ``text`` whose encoded string fits."""
    if not text:
        return ""
    if encoded_bytes("") > target_bytes:
        return ""
    low, high, best = 0, len(text), 0
    while low <= high:
        middle = (low + high) // 2
        if encoded_bytes(text[:middle]) <= target_bytes:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return text[:best]


__all__ = [
    "CartoJobDetailPager",
    "EncodedJsonUnit",
    "DETAIL_PAGE_MAX_BYTES",
    "DETAIL_PAGED_LIST_PATHS",
    "DETAIL_ROW_LIMIT",
    "LazyRowSource",
    "PAGE_METADATA_KEYS",
    "SUMMARY_PROTECTED_KEYS",
    "compact_json",
    "detach_detail_frame",
    "encoded_bytes",
    "get_path",
    "join_carto_job_detail_pages",
    "page_carto_job_detail",
    "path_parts",
    "preview_carto_job_summary",
    "set_path",
    "unit_text",
]
