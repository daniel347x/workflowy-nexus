"""Pure tag-family purge planning and fingerprint policy.

This module owns no transport and performs no mutation.  It scans immutable
/nodes-export snapshots, builds one Q4 removal operation per node, reports
non-mutable occurrences, detects shared persisted-family use, and fingerprints
the exact semantic state an apply operation must revalidate.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from .client.api_client_core import _resolve_split_name_and_tags
from .f8_slice_policy import (
    DEFAULT_AUTO_EXPAND_POSTFIX,
    DEFAULT_DEPRECATED_POSTFIX,
    DEFAULT_SUPPRESS_POSTFIX,
    POSTFIX_SEP,
    extract_tags_from_text,
    slice_tag_variant_match,
)


# @beacon[
#   id=mcp-api@normalize-base-tag-family,
#   role=ƒ normalize_base_tag_family(raw),
#   slice_labels=nexus--mcp-api,nexus--tags,slices,
#   kind=ast,
#   comment=Strict pure base-family normalizer shared by merge.persist and purge. Accepts one optional leading #, preserves semantic internal double-hyphen vocabularies, and rejects empty bodies, embedded #, whitespace, empty -- tails, and trailing LIVE operation postfixes.,
# ]
def normalize_base_tag_family(
    raw: Any,
    live_postfixes: Iterable[str] = (
        DEFAULT_AUTO_EXPAND_POSTFIX,
        DEFAULT_SUPPRESS_POSTFIX,
        DEFAULT_DEPRECATED_POSTFIX,
    ),
) -> str:
    """Normalize one strict base family or raise ``ValueError``.

    Double hyphens are valid inside base vocabularies (for example
    ``nexus--architecture``).  Only an empty ``--`` tail or a trailing LIVE
    operation postfix is rejected.
    """

    if not isinstance(raw, str):
        raise ValueError("tag family must be a string")
    value = raw.strip()
    if value.startswith("#"):
        value = value[1:]
    postfixes = tuple(
        str(item).strip() for item in live_postfixes if str(item).strip()
    )
    has_operation_tail = value.endswith(POSTFIX_SEP) or any(
        value.endswith(f"{POSTFIX_SEP}{postfix}") for postfix in postfixes
    )
    if (
        not value
        or "#" in value
        or any(ch.isspace() for ch in value)
        or has_operation_tail
    ):
        raise ValueError(
            "tag family must be exactly one non-empty base token: optional leading '#', "
            "no embedded '#', whitespace, or live operation-postfix tail"
        )
    return value


def _node_name(node: Mapping[str, Any]) -> str:
    return str(node.get("name") or node.get("nm") or "")


def _node_note(node: Mapping[str, Any]) -> str:
    return str(node.get("note") or node.get("no") or "")


def _node_priority(node: Mapping[str, Any]) -> tuple[float, str]:
    raw = node.get("priority")
    priority = float(raw) if isinstance(raw, (int, float)) else 0.0
    return priority, str(node.get("id") or "")


def _walk_scope_ids(
    cache_by_id: Mapping[str, Mapping[str, Any]], start_ids: Iterable[str]
) -> list[str]:
    children: dict[str, list[str]] = {}
    for node_id, node in cache_by_id.items():
        parent_id = node.get("parent_id") or node.get("parentId")
        if parent_id:
            children.setdefault(str(parent_id), []).append(str(node_id))
    for parent_id in children:
        children[parent_id].sort(
            key=lambda node_id: _node_priority(cache_by_id.get(node_id) or {})
        )

    ordered: list[str] = []
    seen: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in seen or node_id not in cache_by_id:
            return
        seen.add(node_id)
        ordered.append(node_id)
        for child_id in children.get(node_id, ()):
            visit(child_id)

    for start_id in start_ids:
        visit(str(start_id))
    return ordered


def _matching_family_tags(
    tags: Iterable[str], family: str, live_postfixes: Iterable[str]
) -> list[str]:
    postfixes = tuple(str(item).strip() for item in live_postfixes if str(item).strip())
    matched: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = str(raw).lstrip("#").strip()
        if not tag or tag in seen:
            continue
        if slice_tag_variant_match(tag, family, postfix_words=postfixes) is not None:
            seen.add(tag)
            matched.append(tag)
    return matched


# @beacon[
#   id=mcp-api@scan-tag-family-occurrences,
#   role=ƒ scan_tag_family_occurrences(cache_by_id, start_ids, family, live_postfixes),
#   slice_labels=nexus--mcp-api,nexus--tags,slices,
#   kind=ast,
#   comment=Pure completed-and-suppression-inclusive descendant scan for one strict tag family. Classifies exact matching variants independently as mutable trailing-name tags or report-only inline-name/note occurrences.,
# ]
def scan_tag_family_occurrences(
    cache_by_id: Mapping[str, Mapping[str, Any]],
    start_ids: Iterable[str],
    family: str,
    live_postfixes: Iterable[str],
) -> list[dict[str, Any]]:
    """Return every matching occurrence under ``start_ids``.

    Completion and suppression are deliberately ignored: cleanup must include
    hidden historical nodes.  Trailing-name occurrences are mutable; inline
    name and note occurrences are report-only.
    """

    normalized = normalize_base_tag_family(family, live_postfixes)
    rows: list[dict[str, Any]] = []
    for node_id in _walk_scope_ids(cache_by_id, start_ids):
        node = cache_by_id[node_id]
        name = _node_name(node)
        note = _node_note(node)
        base_name, trailing_raw = _resolve_split_name_and_tags(name)
        regions = (
            (
                "name_trailing",
                _matching_family_tags(trailing_raw, normalized, live_postfixes),
            ),
            (
                "name_inline",
                _matching_family_tags(
                    extract_tags_from_text(base_name), normalized, live_postfixes
                ),
            ),
            (
                "note",
                _matching_family_tags(
                    extract_tags_from_text(note), normalized, live_postfixes
                ),
            ),
        )
        for region, tags in regions:
            for tag in tags:
                rows.append(
                    {
                        "uuid": node_id,
                        "name": name,
                        "family": normalized,
                        "tag": tag,
                        "source_region": region,
                        "mutable": region == "name_trailing",
                        "completed": bool(
                            node.get("completed")
                            or node.get("is_completed")
                            or node.get("_completed")
                        ),
                    }
                )
    return rows


# @beacon[
#   id=mcp-api@plan-tag-family-purge,
#   role=ƒ plan_tag_family_purge(cache_by_id, start_ids, families, live_postfixes),
#   slice_labels=nexus--mcp-api,nexus--tags,slices,
#   kind=ast,
#   comment=Pure multi-family purge compiler. De-duplicates overlapping families and batches every exact trailing variant on one node into exactly one Q4 remove operation, while preserving report-only rows and deterministic counts.,
# ]
def plan_tag_family_purge(
    cache_by_id: Mapping[str, Mapping[str, Any]],
    start_ids: Iterable[str],
    families: Iterable[str],
    live_postfixes: Iterable[str],
) -> dict[str, Any]:
    """Build a deterministic one-operation-per-node Q4 purge plan."""

    normalized_families = tuple(
        dict.fromkeys(
            normalize_base_tag_family(item, live_postfixes) for item in families
        )
    )
    starts = tuple(dict.fromkeys(str(item) for item in start_ids if str(item)))
    occurrences: list[dict[str, Any]] = []
    occurrence_keys: set[tuple[str, str, str]] = set()
    for family in normalized_families:
        for row in scan_tag_family_occurrences(
            cache_by_id, starts, family, live_postfixes
        ):
            key = (row["uuid"], row["source_region"], row["tag"])
            if key in occurrence_keys:
                continue
            occurrence_keys.add(key)
            occurrences.append(row)

    mutable_by_node: dict[str, set[str]] = {}
    report_only: list[dict[str, Any]] = []
    for row in occurrences:
        if row["mutable"]:
            mutable_by_node.setdefault(row["uuid"], set()).add(row["tag"])
        else:
            report_only.append(dict(row))

    scope_order = {node_id: index for index, node_id in enumerate(_walk_scope_ids(cache_by_id, starts))}
    ops = [
        {
            "handle": node_id,
            "operation": "remove",
            "tags": sorted(tags),
        }
        for node_id, tags in sorted(
            mutable_by_node.items(),
            key=lambda item: (scope_order.get(item[0], 10**12), item[0]),
        )
    ]
    mutable_rows = [row for row in occurrences if row["mutable"]]
    variant_counts = Counter(row["tag"] for row in occurrences)
    return {
        "start_ids": list(starts),
        "families": list(normalized_families),
        "ops": ops,
        "mutable": mutable_rows,
        "report_only": report_only,
        "counts": {
            "nodes_with_mutable_tags": len(ops),
            "mutable_occurrences": len(mutable_rows),
            "report_only_occurrences": len(report_only),
            "total_occurrences": len(occurrences),
            "per_variant": dict(sorted(variant_counts.items())),
        },
    }


# @beacon[
#   id=mcp-api@find-purge-shared-use,
#   role=ƒ find_purge_shared_use(families, groups_cfg, excluded_group_ids, live_postfixes),
#   slice_labels=nexus--mcp-api,nexus--tags,nexus--config,slices,
#   kind=ast,
#   comment=Pure bidirectional overlap detector for purge consent. Reports every other persisted slice-group prefix that shares any requested family, excluding only explicitly selected group ids.,
# ]
def find_purge_shared_use(
    families: Iterable[str],
    groups_cfg: Iterable[Mapping[str, Any]],
    excluded_group_ids: Iterable[str] = (),
    live_postfixes: Iterable[str] = (),
) -> list[dict[str, str]]:
    """Return canonical rows for persisted groups sharing a purge family."""

    requested = tuple(
        dict.fromkeys(
            normalize_base_tag_family(item, live_postfixes) for item in families
        )
    )
    excluded = {str(item) for item in excluded_group_ids}
    postfixes = tuple(str(item) for item in live_postfixes)
    rows: set[tuple[str, str, str, str]] = set()
    for group in groups_cfg:
        group_id = str(group.get("id") or "")
        if group_id in excluded:
            continue
        group_name = str(group.get("name") or "")
        for raw_prefix in group.get("prefixes") or ():
            prefix = str(raw_prefix).lstrip("#").strip()
            if not prefix:
                continue
            for family in requested:
                if (
                    slice_tag_variant_match(
                        family, prefix, postfix_words=postfixes
                    )
                    is not None
                    or slice_tag_variant_match(
                        prefix, family, postfix_words=postfixes
                    )
                    is not None
                ):
                    rows.add((group_id, group_name, prefix, family))
    return [
        {
            "group_id": group_id,
            "group_name": group_name,
            "prefix": prefix,
            "family": family,
        }
        for group_id, group_name, prefix, family in sorted(rows)
    ]


# @beacon[
#   id=mcp-api@purge-fingerprint,
#   role=ƒ purge_fingerprint(scope, families, plan, shared_use, live_postfixes),
#   slice_labels=nexus--mcp-api,nexus--tags,slices,
#   kind=ast,
#   comment=Deterministic sha256 consent fingerprint over only semantic purge state: resolved roots, families normalized against the live postfix vocabulary, exact mutable/report-only occurrences, exact planned operations, and canonical persisted shared-use rows.,
# ]
def purge_fingerprint(
    scope: Mapping[str, Any],
    families: Iterable[str],
    plan: Mapping[str, Any],
    shared_use: Iterable[Mapping[str, Any]],
    live_postfixes: Iterable[str] = (
        DEFAULT_AUTO_EXPAND_POSTFIX,
        DEFAULT_SUPPRESS_POSTFIX,
        DEFAULT_DEPRECATED_POSTFIX,
    ),
) -> str:
    """Hash the frozen semantic state that purge.apply must revalidate."""

    payload = {
        "start_ids": sorted(
            str(item) for item in scope.get("start_ids", ()) if str(item)
        ),
        "families": sorted(
            dict.fromkeys(
                normalize_base_tag_family(item, live_postfixes)
                for item in families
            )
        ),
        "mutable": sorted(
            {
                (str(row.get("uuid") or ""), str(row.get("tag") or ""))
                for row in plan.get("mutable", ())
            }
        ),
        "report_only": sorted(
            {
                (
                    str(row.get("uuid") or ""),
                    str(row.get("source_region") or ""),
                    str(row.get("tag") or ""),
                )
                for row in plan.get("report_only", ())
            }
        ),
        "ops": sorted(
            (
                str(row.get("handle") or ""),
                tuple(sorted(str(tag) for tag in row.get("tags", ()))),
            )
            for row in plan.get("ops", ())
        ),
        "shared_use": sorted(
            {
                (
                    str(row.get("group_id") or ""),
                    str(row.get("prefix") or ""),
                )
                for row in shared_use
            }
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "normalize_base_tag_family",
    "scan_tag_family_occurrences",
    "plan_tag_family_purge",
    "find_purge_shared_use",
    "purge_fingerprint",
]
