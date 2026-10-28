"""Pure recursive LensSpec grammar for declarative NEXUS selections."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, TypeAlias


LENS_ATOM_KINDS = ("group", "tags", "subtree", "merge")
MERGE_OPERATIONS = ("union", "intersection", "difference")
DESCEND_POLICIES = ("all", "stop_at_child_docks")
MATCH_SCOPES = ("name_and_note", "name")
MAX_LENS_DEPTH = 4
MAX_LENS_ATOMS = 16


class LensSpecError(ValueError):
    """Structured validation/evaluation error for one exact LensSpec path."""

    def __init__(self, code: str, message: str, path: str = "$") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "path": self.path}


@dataclass(frozen=True)
class GroupAtom:
    group_handle: str
    root_handle: str | None = None
    match_scope: str | None = None
    kind: str = "group"


@dataclass(frozen=True)
class TagsAtom:
    root_handle: str
    tags: tuple[str, ...]
    match_scope: str | None = None
    kind: str = "tags"


@dataclass(frozen=True)
class SubtreeAtom:
    root_handle: str
    descend: str = "all"
    kind: str = "subtree"


@dataclass(frozen=True)
class MergeNode:
    operation: str
    inputs: tuple["LensNode", ...]
    kind: str = "merge"


LensNode: TypeAlias = GroupAtom | TagsAtom | SubtreeAtom | MergeNode
LensAtom: TypeAlias = GroupAtom | TagsAtom | SubtreeAtom


def _child_path(parent: str, index: int) -> str:
    return f"inputs[{index}]" if parent == "$" else f"{parent}.inputs[{index}]"


def _error(code: str, message: str, path: str) -> LensSpecError:
    return LensSpecError(code, message, path)


def _require_nonempty_string(value: Any, field: str, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(
            "invalid_lens_shape",
            f"{field} must be a non-empty string.",
            path,
        )
    return value.strip()


def _reject_unknown_keys(
    raw: Mapping[str, Any], allowed: set[str], path: str
) -> None:
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise _error(
            "invalid_lens_shape",
            f"Unknown field(s) for this lens kind: {', '.join(unknown)}.",
            path,
        )


# @beacon[
#   id=mcp-api@parse-lens-spec,
#   role=ƒ parse_lens_spec(raw),
#   slice_labels=nexus--mcp-api,slices,
#   kind=ast,
#   comment=The one pure closed-key LensSpec parser. Accepts either Dock-stop spelling at subtree ingress, storing the historical stop_at_child_docks engine token; no default change. Frozen nodes, depth/atom budgets and exact-path failures remain unchanged.,
# ]
def parse_lens_spec(raw: Mapping[str, Any] | str) -> LensNode:
    """Parse and validate a recursive LensSpec mapping or JSON string."""

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise _error(
                "invalid_lens_shape",
                f"LensSpec JSON could not be decoded: {exc}",
                "$",
            ) from exc
    if not isinstance(raw, Mapping):
        raise _error("invalid_lens_shape", "LensSpec must be an object.", "$")

    atom_count = 0

    def parse_node(value: Any, path: str, depth: int) -> LensNode:
        nonlocal atom_count
        if depth > MAX_LENS_DEPTH:
            raise _error(
                "lens_depth_exceeded",
                f"LensSpec depth exceeds the maximum of {MAX_LENS_DEPTH}.",
                path,
            )
        if not isinstance(value, Mapping):
            raise _error("invalid_lens_shape", "Lens node must be an object.", path)
        kind = value.get("kind")
        if not isinstance(kind, str) or kind not in LENS_ATOM_KINDS:
            raise _error(
                "invalid_lens_kind",
                f"kind must be one of {LENS_ATOM_KINDS!r}.",
                path,
            )

        if kind == "merge":
            _reject_unknown_keys(value, {"kind", "operation", "inputs"}, path)
            operation = value.get("operation")
            if operation not in MERGE_OPERATIONS:
                raise _error(
                    "invalid_merge_operation",
                    f"operation must be one of {MERGE_OPERATIONS!r}.",
                    path,
                )
            inputs = value.get("inputs")
            if not isinstance(inputs, (list, tuple)) or len(inputs) < 2:
                raise _error(
                    "empty_merge_inputs",
                    "A merge requires at least two inputs; use the atom directly otherwise.",
                    path,
                )
            parsed_inputs = tuple(
                parse_node(item, _child_path(path, index), depth + 1)
                for index, item in enumerate(inputs)
            )
            return MergeNode(operation=operation, inputs=parsed_inputs)

        atom_count += 1
        if atom_count > MAX_LENS_ATOMS:
            raise _error(
                "lens_atom_budget_exceeded",
                f"LensSpec contains more than {MAX_LENS_ATOMS} atomic inputs.",
                path,
            )

        if kind == "group":
            _reject_unknown_keys(
                value, {"kind", "group_handle", "root_handle", "match_scope"}, path
            )
            group_handle = _require_nonempty_string(
                value.get("group_handle"), "group_handle", path
            )
            root_value = value.get("root_handle")
            root_handle = (
                None
                if root_value is None
                else _require_nonempty_string(root_value, "root_handle", path)
            )
            match_scope = value.get("match_scope")
            if match_scope is not None and match_scope not in MATCH_SCOPES:
                raise _error(
                    "invalid_match_scope",
                    f"match_scope must be one of {MATCH_SCOPES!r} or null.",
                    path,
                )
            return GroupAtom(group_handle, root_handle, match_scope)

        if kind == "tags":
            _reject_unknown_keys(
                value, {"kind", "root_handle", "tags", "match_scope"}, path
            )
            root_handle = _require_nonempty_string(
                value.get("root_handle"), "root_handle", path
            )
            tags = value.get("tags")
            if not isinstance(tags, (list, tuple)) or not tags:
                raise _error(
                    "invalid_lens_shape", "tags must be a non-empty array.", path
                )
            clean_tags: list[str] = []
            for index, tag in enumerate(tags):
                if not isinstance(tag, str) or not tag.strip():
                    raise _error(
                        "invalid_lens_shape",
                        f"tags[{index}] must be a non-empty string.",
                        path,
                    )
                clean_tags.append(tag.strip())
            match_scope = value.get("match_scope")
            if match_scope is not None and match_scope not in MATCH_SCOPES:
                raise _error(
                    "invalid_match_scope",
                    f"match_scope must be one of {MATCH_SCOPES!r}.",
                    path,
                )
            return TagsAtom(root_handle, tuple(clean_tags), match_scope)

        _reject_unknown_keys(value, {"kind", "root_handle", "descend"}, path)
        root_handle = _require_nonempty_string(
            value.get("root_handle"), "root_handle", path
        )
        descend = value.get("descend", "all")
        if descend == "stop_at_child_anchors":
            descend = "stop_at_child_docks"
        if descend not in DESCEND_POLICIES:
            raise _error(
                "invalid_descend_policy",
                f"descend must be one of {DESCEND_POLICIES!r}.",
                path,
            )
        return SubtreeAtom(root_handle, descend)

    return parse_node(raw, "$", 1)


def lens_spec_to_wire(node: LensNode) -> dict[str, Any]:
    """Serialize a typed LensSpec without changing its recipe semantics."""

    if isinstance(node, GroupAtom):
        out: dict[str, Any] = {"kind": "group", "group_handle": node.group_handle}
        if node.root_handle is not None:
            out["root_handle"] = node.root_handle
        if node.match_scope is not None:
            out["match_scope"] = node.match_scope
        return out
    if isinstance(node, TagsAtom):
        out: dict[str, Any] = {
            "kind": "tags",
            "root_handle": node.root_handle,
            "tags": list(node.tags),
        }
        if node.match_scope is not None:
            out["match_scope"] = node.match_scope
        return out
    if isinstance(node, SubtreeAtom):
        return {
            "kind": "subtree",
            "root_handle": node.root_handle,
            "descend": node.descend,
        }
    if isinstance(node, MergeNode):
        return {
            "kind": "merge",
            "operation": node.operation,
            "inputs": [lens_spec_to_wire(item) for item in node.inputs],
        }
    raise TypeError(f"Unsupported LensSpec node: {type(node).__name__}")


def iter_lens_atoms(node: LensNode) -> Iterator[tuple[str, LensAtom]]:
    """Yield every atomic input in stable depth-first recipe order."""

    def walk(current: LensNode, path: str) -> Iterator[tuple[str, LensAtom]]:
        if isinstance(current, MergeNode):
            for index, child in enumerate(current.inputs):
                yield from walk(child, _child_path(path, index))
            return
        yield path, current

    yield from walk(node, "$")


__all__ = [
    "LENS_ATOM_KINDS",
    "MERGE_OPERATIONS",
    "DESCEND_POLICIES",
    "MATCH_SCOPES",
    "MAX_LENS_DEPTH",
    "MAX_LENS_ATOMS",
    "LensSpecError",
    "GroupAtom",
    "TagsAtom",
    "SubtreeAtom",
    "MergeNode",
    "LensNode",
    "LensAtom",
    "parse_lens_spec",
    "lens_spec_to_wire",
    "iter_lens_atoms",
]
