"""The workflowy-nexus-archive format authority.

Exactly one module defines the archive format (a single format authority):
its constants, the record copy rule, the emission order, the document
builder, the serializer, the content hash and the reader.  Every producer
and every consumer of an archive file goes through this module; a second
reader, writer or schema for archives is a specification violation.

The archive file is ONE plain JSON object with exactly two top-level keys:

    {"archive": {...metadata block...}, "nodes": [...node records...]}

``nodes`` holds the node records exactly as the pinned cache view holds
them -- every key, every value, unchanged (only key ORDER is normalized,
which is not semantic in JSON).  All human-facing and provenance metadata
lives in the sibling ``archive`` block, never inside a node record.

The module performs no file I/O, no network I/O and reads no cache:
records, the flat-to-nested builder (passed as ``nest``) and all metadata
arrive as arguments.  The text form of ``name``/``note`` is the cache's
own semantic text (already decoded exactly once upstream); nothing here
re-decodes, encodes, strips or normalizes text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from .coherence.canonical_node import CANONICAL_KEYS

ARCHIVE_FORMAT = "workflowy-nexus-archive"
ARCHIVE_FORMAT_VERSION = 1
TEXT_ENCODING = "cache-semantic"
VAULT_ARCHIVE_NAME = "Workflowy vault"
TOP_LEVEL_KEYS = ("archive", "nodes")
ARCHIVE_KEYS = (
    "format",
    "format_version",
    "name",
    "description",
    "exported_at",
    "root_ids",
    "source_parent_id",
    "node_count",
    "nodes_sha256",
    "text_encoding",
    "source_cache",
)
SOURCE_CACHE_KEYS = ("epoch_number", "epoch_id", "adopted_sequence")

_EXPORTED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ArchiveError(RuntimeError):
    """Base archive error; also raised by the builder for inputs it cannot
    archive.  A raised ArchiveError always means nothing was written."""


class ArchiveFormatError(ArchiveError):
    """The reader refused bytes that do not satisfy the format."""


class ArchiveVerificationError(ArchiveError):
    """An export's own verification failed; the message always contains
    UNVERIFIED and names the file that must not be trusted."""


# ---------------------------------------------------------------------------
# The root rule and the content hash
# ---------------------------------------------------------------------------


def _record_parent(record: Mapping[str, Any]) -> Any:
    """The same parent expression the flat-to-nested builder uses."""
    return record.get("parent_id") or record.get("parentId")


def archive_root_ids(nodes: Sequence[Mapping[str, Any]]) -> list[str]:
    """The ids of the block's roots, in the order they occur in ``nodes``.

    A record is a root exactly when its parent value is falsy or is not
    the ``id`` of another record in the list.  No marker is written into
    any record.
    """
    id_set = {node["id"] for node in nodes}
    roots: list[str] = []
    for node in nodes:
        parent = _record_parent(node)
        if not parent or parent not in id_set:
            roots.append(node["id"])
    return roots


def nodes_sha256(nodes: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 of the canonical JSON encoding of ``nodes`` (lowercase hex).

    Recomputable from the file alone and independent of key order, so the
    record copy rule does not change it.
    """
    return hashlib.sha256(
        json.dumps(
            list(nodes),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# The record copy rule and the emission order
# ---------------------------------------------------------------------------


def _canonical_value(value: Any) -> Any:
    """Return an owned copy of ``value`` with nested object keys sorted.

    A mapping becomes a new ``dict`` with keys in ``sorted()`` order and
    values converted recursively; a list becomes a new ``list`` of
    recursively converted elements; any other value (strings, numbers,
    booleans, None) passes through unchanged.  The copy shares no ``dict``
    or ``list`` object with the caller's containers.
    """
    if isinstance(value, dict):
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    return value


def _copy_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """One new record dict: canonical keys first, then the rest, sorted.

    Key presence and every value are unchanged -- only key order is
    normalized.
    """
    out: dict[str, Any] = {}
    for key in CANONICAL_KEYS:
        if key in record:
            out[key] = _canonical_value(record[key])
    for key in sorted(record):
        if key not in out:
            out[key] = _canonical_value(record[key])
    return out


def _preorder_ids(roots: Sequence[Mapping[str, Any]]) -> list[str]:
    """Preorder walk over an already-nested root list (iterative)."""
    walk: list[str] = []
    stack: list[Mapping[str, Any]] = list(reversed(list(roots)))
    while stack:
        node = stack.pop()
        walk.append(node["id"])
        children = node.get("children") or []
        stack.extend(reversed(list(children)))
    return walk


def _check_source_cache(source_cache: Mapping[str, Any]) -> None:
    if not isinstance(source_cache, Mapping) or set(source_cache) != set(
        SOURCE_CACHE_KEYS
    ):
        raise ArchiveError(
            "source_cache must have exactly the keys "
            f"{list(SOURCE_CACHE_KEYS)}; got {sorted(source_cache) if isinstance(source_cache, Mapping) else type(source_cache).__name__}"
        )
    for int_key in ("epoch_number", "adopted_sequence"):
        value = source_cache[int_key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ArchiveError(
                f"source_cache.{int_key} must be a non-negative int; got {value!r}"
            )
    epoch_id = source_cache["epoch_id"]
    if not isinstance(epoch_id, str) or not epoch_id:
        raise ArchiveError(
            f"source_cache.epoch_id must be a non-empty str; got {epoch_id!r}"
        )


def _order_records(
    records: Sequence[Mapping[str, Any]], nest: Callable[[list], list]
) -> list[str]:
    """The emission order: preorder over the one shared builder's nesting.

    1. presort the records by id (the builder's stable priority sort then
       breaks ties by id order);
    2. nest them (the builder returns shallow copies carrying ``children``
       and sorts siblings by priority, default 0);
    3. sort the roots by ``(priority, id)`` -- the builder does not sort
       roots;
    4. preorder-walk, and require every record to be visited exactly once
       (a parent cycle would leave records unreachable).

    Returns the visited ids in emission order.  Raises ArchiveError when
    the records cannot be ordered or do not form trees.
    """
    by_id = {record["id"]: record for record in records}
    presorted = sorted(records, key=lambda record: record["id"])
    try:
        roots = nest(presorted)
        roots = sorted(
            roots, key=lambda root: (root.get("priority", 0), root["id"])
        )
        walk = _preorder_ids(roots)
    except ArchiveError:
        raise
    except Exception as exc:
        raise ArchiveError(
            f"cannot order records: {exc}; nothing was written"
        ) from exc
    if len(walk) != len(records) or set(walk) != set(by_id):
        unreachable = sorted(set(by_id) - set(walk))
        preview = ", ".join(repr(item) for item in unreachable[:10])
        raise ArchiveError(
            "records do not form trees: "
            f"{len(unreachable)} unreachable from the roots (e.g. {preview}); "
            "nothing was written"
        )
    return walk


# ---------------------------------------------------------------------------
# Build, serialize, parse
# ---------------------------------------------------------------------------


def build_archive_document(
    records: Sequence[Mapping[str, Any]],
    *,
    nest: Callable[[list], list],
    node_id: str | None,
    name: str | None,
    description: str | None,
    exported_at: str,
    source_cache: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the ordered archive document from the selected records.

    ``records`` are the selected record mappings (for a subtree, the root
    and every descendant; for the whole vault, every record).  ``node_id``
    is the resolved root id, or None for the whole vault.  ``exported_at``
    is already formatted ``YYYY-MM-DDTHH:MM:SSZ``.  ``source_cache`` has
    exactly the three provenance keys.  ``nest`` is the one shared
    flat-to-nested builder (a callable taking a list of record mappings
    and returning the nested root list).

    Raises ArchiveError before anything is built when an input is
    unusable.  Never mutates ``records`` or anything reachable from them.
    """
    for record in records:
        if not isinstance(record, Mapping):
            raise ArchiveError(
                "every record must be a mapping; got "
                f"{type(record).__name__}"
            )
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id:
            raise ArchiveError(
                f"every record must carry a non-blank string id; got {record_id!r}"
            )
    ids = [record["id"] for record in records]
    if len(set(ids)) != len(ids):
        raise ArchiveError("duplicate record ids in the selection; nothing was written")
    if node_id is not None and node_id not in set(ids):
        raise ArchiveError(
            f"node_id {node_id!r} is not among the records; nothing was written"
        )
    _check_source_cache(source_cache)
    if not isinstance(exported_at, str) or not _EXPORTED_AT_RE.match(exported_at):
        raise ArchiveError(
            "exported_at must match YYYY-MM-DDTHH:MM:SSZ; got "
            f"{exported_at!r}"
        )

    by_id = {record["id"]: record for record in records}
    walk = _order_records(records, nest)
    nodes = [_copy_record(by_id[record_id]) for record_id in walk]
    root_ids = archive_root_ids(nodes)
    if node_id is not None and root_ids != [node_id]:
        raise ArchiveError(
            f"subtree selection produced roots {root_ids!r}, expected "
            f"[{node_id!r}]; the selection or the tree is broken; "
            "nothing was written"
        )
    try:
        content_hash = nodes_sha256(nodes)
    except ValueError as exc:
        raise ArchiveError(
            f"cannot hash records: {exc}; nothing was written"
        ) from exc

    if name is not None:
        archive_name: str = name
    elif node_id is not None:
        root_name = by_id[node_id].get("name")
        archive_name = root_name if isinstance(root_name, str) else ""
    else:
        archive_name = VAULT_ARCHIVE_NAME
    if node_id is not None:
        parent = _record_parent(by_id[node_id])
        source_parent_id = parent if isinstance(parent, str) and parent else None
    else:
        source_parent_id = None

    return {
        "archive": {
            "format": ARCHIVE_FORMAT,
            "format_version": ARCHIVE_FORMAT_VERSION,
            "name": archive_name,
            "description": description,
            "exported_at": exported_at,
            "root_ids": root_ids,
            "source_parent_id": source_parent_id,
            "scope": "vault" if node_id is None else "subtree",
            "node_count": len(nodes),
            "nodes_sha256": content_hash,
            "text_encoding": TEXT_ENCODING,
            "source_cache": {key: source_cache[key] for key in SOURCE_CACHE_KEYS},
        },
        "nodes": nodes,
    }


def serialize_archive(document: Mapping[str, Any]) -> str:
    """Serialize the document to the file text.

    UTF-8-safe text (the caller encodes with ``text.encode("utf-8")``),
    ``ensure_ascii=False``, two-space indentation, LF line breaks only,
    exactly one trailing newline.  A value JSON cannot carry (NaN,
    infinity) raises ArchiveError: such a file would not load in an
    ordinary JSON parser.
    """
    try:
        return json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    except ValueError as exc:
        raise ArchiveError(f"cannot serialize archive: {exc}; nothing was written") from exc


# ---------------------------------------------------------------------------
# Archive-block readers and the one mount edit (Archive Reads U1)
# ---------------------------------------------------------------------------


def archive_scope(block: Mapping[str, Any]) -> str:
    """Read one parsed document's archive-block ``scope``.

    ``"vault"`` (a whole-vault export: never read by consumers) or
    ``"subtree"``.  An explicit ``scope`` of ``"vault"`` or ``"subtree"``
    wins; any other explicit value raises ``ArchiveFormatError``.  Absent
    (an older exporter) it is inferred: more than one root is a vault;
    a null ``source_parent_id`` with the default vault name is a vault;
    otherwise a subtree.  Pure; never mutates ``block``.
    """
    if "scope" in block:
        value = block["scope"]
        if value == "vault" or value == "subtree":
            return str(value)
        raise ArchiveFormatError(
            f"archive scope must be 'vault' or 'subtree'; got {value!r}"
        )
    root_ids = block.get("root_ids")
    if isinstance(root_ids, Sequence) and not isinstance(root_ids, str):
        if len(root_ids) > 1:
            return "vault"
    if (
        block.get("source_parent_id") is None
        and block.get("name") == VAULT_ARCHIVE_NAME
    ):
        return "vault"
    return "subtree"


def archive_mount_parent(block: Mapping[str, Any]) -> "str | None":
    """Read one parsed document's archive-block ``mount_parent_id``.

    Absent or ``None`` yields ``None`` (no mount point); a non-empty
    string yields it; anything else raises ``ArchiveFormatError``.  Pure;
    never mutates ``block``.
    """
    value = block.get("mount_parent_id")
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    raise ArchiveFormatError(
        "archive mount_parent_id must be null or a non-empty string; got "
        f"{value!r}"
    )


def with_mount_parent(
    document: Mapping[str, Any], parent_id: "str | None"
) -> dict[str, Any]:
    """Return a NEW document with ``mount_parent_id`` set or cleared.

    The only sanctioned edit to an archive document (the mount verb's
    write path): the returned ``archive`` block equals the input's with
    ``mount_parent_id`` set to ``parent_id`` (appended at the end of the
    block when absent), or removed when ``parent_id`` is ``None``.
    ``nodes`` is the SAME list object -- never copied, never touched -- so
    ``nodes_sha256`` still matches and the result re-parses.  The input
    document is never mutated.  Raises ``ArchiveError`` (nothing written)
    on an unusable input.
    """
    archive = document.get("archive")
    if not isinstance(archive, Mapping):
        raise ArchiveError("with_mount_parent: document has no archive block")
    if "nodes" not in document:
        raise ArchiveError("with_mount_parent: document has no nodes list")
    block = dict(archive)
    if parent_id is None:
        block.pop("mount_parent_id", None)
    else:
        if not isinstance(parent_id, str) or not parent_id:
            raise ArchiveError(
                "with_mount_parent: parent_id must be a non-empty string or None; "
                f"got {parent_id!r}"
            )
        block["mount_parent_id"] = parent_id
    return {"archive": block, "nodes": document["nodes"]}


def _refuse_constant(value: str) -> Any:
    raise ArchiveFormatError(f"archive contains the non-JSON literal {value}")


def parse_archive(
    data: bytes, *, nest: Callable[[list], list]
) -> dict[str, Any]:
    """Read and validate one archive document from its bytes.

    Either returns the parsed document or raises ArchiveFormatError naming
    the first rule that failed.  Never modifies anything.  Unknown keys
    inside ``archive`` are ignored (forward compatibility); unknown
    top-level keys, a wrong format, an unknown version, a wrong count, a
    hash mismatch, reordered roots and unreachable records are refused.
    """
    if not isinstance(data, bytes):
        raise ArchiveFormatError("archive data must be bytes")
    if data[:3] == b"\xef\xbb\xbf":
        raise ArchiveFormatError("archive data starts with a UTF-8 byte-order mark")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchiveFormatError(f"archive is not valid UTF-8: {exc}") from exc
    try:
        document = json.loads(text, parse_constant=_refuse_constant)
    except ArchiveFormatError:
        raise
    except ValueError as exc:
        raise ArchiveFormatError(f"archive is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ArchiveFormatError("archive top level must be a JSON object")
    if set(document) != set(TOP_LEVEL_KEYS):
        raise ArchiveFormatError(
            "top-level keys must be exactly "
            f"{list(TOP_LEVEL_KEYS)}; found {sorted(document)}"
        )

    archive = document["archive"]
    if not isinstance(archive, dict):
        raise ArchiveFormatError("the archive block must be a JSON object")
    missing = [key for key in ARCHIVE_KEYS if key not in archive]
    if missing:
        raise ArchiveFormatError(f"archive block is missing keys: {missing}")
    if archive["format"] != ARCHIVE_FORMAT:
        raise ArchiveFormatError(
            f"archive format must be {ARCHIVE_FORMAT!r}; got {archive['format']!r}"
        )
    format_version = archive["format_version"]
    if (
        isinstance(format_version, bool)
        or not isinstance(format_version, int)
        or format_version != ARCHIVE_FORMAT_VERSION
    ):
        raise ArchiveFormatError(
            f"unsupported format_version: {format_version!r} "
            f"(this reader knows version {ARCHIVE_FORMAT_VERSION})"
        )
    if not isinstance(archive["name"], str):
        raise ArchiveFormatError("archive.name must be a string")
    if archive["description"] is not None and not isinstance(
        archive["description"], str
    ):
        raise ArchiveFormatError("archive.description must be a string or null")
    if not isinstance(archive["exported_at"], str) or not _EXPORTED_AT_RE.match(
        archive["exported_at"]
    ):
        raise ArchiveFormatError(
            "archive.exported_at must match YYYY-MM-DDTHH:MM:SSZ; got "
            f"{archive['exported_at']!r}"
        )
    root_ids = archive["root_ids"]
    if not isinstance(root_ids, list) or not all(
        isinstance(item, str) for item in root_ids
    ):
        raise ArchiveFormatError("archive.root_ids must be a list of strings")
    if archive["source_parent_id"] is not None and not isinstance(
        archive["source_parent_id"], str
    ):
        raise ArchiveFormatError(
            "archive.source_parent_id must be a string or null"
        )
    node_count = archive["node_count"]
    if isinstance(node_count, bool) or not isinstance(node_count, int):
        raise ArchiveFormatError("archive.node_count must be an integer")
    if not isinstance(archive["nodes_sha256"], str) or not _SHA256_RE.match(
        archive["nodes_sha256"]
    ):
        raise ArchiveFormatError(
            "archive.nodes_sha256 must be 64 lowercase hex characters"
        )
    if archive["text_encoding"] != TEXT_ENCODING:
        raise ArchiveFormatError(
            f"archive.text_encoding must be {TEXT_ENCODING!r}; got "
            f"{archive['text_encoding']!r}"
        )
    try:
        _check_source_cache(archive["source_cache"])
    except ArchiveError as exc:
        raise ArchiveFormatError(str(exc)) from exc

    nodes = document["nodes"]
    if not isinstance(nodes, list):
        raise ArchiveFormatError("nodes must be a list")
    seen: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ArchiveFormatError(
                "every node record must be a JSON object; got "
                f"{type(node).__name__}"
            )
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise ArchiveFormatError(
                f"every node record must carry a non-blank string id; got {node_id!r}"
            )
        if node_id in seen:
            raise ArchiveFormatError(f"duplicate node id: {node_id!r}")
        seen.add(node_id)
    if node_count != len(nodes):
        raise ArchiveFormatError(
            f"node_count {node_count} != len(nodes) {len(nodes)}"
        )
    try:
        actual_hash = nodes_sha256(nodes)
    except ValueError as exc:
        raise ArchiveFormatError(f"nodes are not hashable JSON: {exc}") from exc
    if actual_hash != archive["nodes_sha256"]:
        raise ArchiveFormatError(
            "nodes_sha256 mismatch: the file's nodes do not match the "
            "recorded hash (the file was hand-edited or damaged)"
        )
    if archive_root_ids(nodes) != root_ids:
        raise ArchiveFormatError(
            f"root_ids {root_ids!r} do not match the file's own roots "
            f"{archive_root_ids(nodes)!r}"
        )
    try:
        walk = _preorder_ids(nest(list(nodes)))
    except Exception as exc:
        raise ArchiveFormatError(f"cannot nest records: {exc}") from exc
    if len(walk) != len(nodes) or set(walk) != seen:
        unreachable = sorted(seen - set(walk))
        preview = ", ".join(repr(item) for item in unreachable[:10])
        raise ArchiveFormatError(
            f"records do not form trees: {len(unreachable)} unreachable "
            f"(e.g. {preview})"
        )
    return document
