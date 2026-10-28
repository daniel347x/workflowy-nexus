"""Q4 tag-mutation spec + orchestrator tests — the wire/spec path's first coverage.

Tests the Q4 Workflowy tag mutation pipeline:
  * ``WorkflowyTagMutationSpec.from_wire`` — the §9 client wire frame normalizer.
  * ``WorkflowyTagMutationSpec.normalize_from_tool_args`` — the MCP tool-arg normalizer.
  * ``_normalize_operations`` — the ONE operations-grammar core both ingress
    fronts delegate to.
  * ``_lightning_rod_mutate_tags`` — the shared orchestrator (with a mocked client).

Model on ``tests/test_render_slice_modal_spec_e3.py`` — all local, no network.

⚠️ **Repo-root-module caveat:** all edited Python files live under
``src/workflowy_mcp/`` (covered by the editable install); no repo-root modules
are touched, so the ``pip install -e .``-before-pytest rule is not triggered here.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.lightning_rod_tag_mutation_spec import (  # noqa: E402
    TagMutationOperation,
    WorkflowyTagMutationSpec,
    _coerce_tag_list,
    _normalize_operations,
    _WIRE_FIELDS,
    _WIRE_VERSION,
)


# =============================================================================
# _coerce_tag_list
# =============================================================================

class TestCoerceTagList:
    def test_none_returns_empty(self):
        assert _coerce_tag_list(None) == []

    def test_single_string(self):
        assert _coerce_tag_list("nexus--tags") == ["nexus--tags"]

    def test_hash_prefix_stripped(self):
        assert _coerce_tag_list("#nexus--tags") == ["nexus--tags"]

    def test_list_of_strings(self):
        assert _coerce_tag_list(["#foo", "bar", "#baz"]) == ["foo", "bar", "baz"]

    def test_empty_strings_dropped(self):
        assert _coerce_tag_list(["", "  ", "#foo"]) == ["foo"]

    def test_dedup(self):
        assert _coerce_tag_list(["foo", "foo", "#foo"]) == ["foo"]

    def test_internal_whitespace_rejected(self):
        with pytest.raises(ValueError):
            _coerce_tag_list(["has space"])

    def test_embedded_hash_rejected(self):
        with pytest.raises(ValueError):
            _coerce_tag_list(["has#hash"])

    def test_non_list_non_string_raises(self):
        with pytest.raises(ValueError):
            _coerce_tag_list(42)


# =============================================================================
# _normalize_operations
# =============================================================================

class TestNormalizeOperations:
    def test_non_list_returns_error(self):
        ops, err = _normalize_operations("not a list")
        assert ops is None
        assert "non-empty list" in err

    def test_empty_list_returns_error(self):
        ops, err = _normalize_operations([])
        assert ops is None
        assert "non-empty list" in err

    def test_happy_path_add(self):
        ops, err = _normalize_operations([
            {"handle": "uuid-1", "operation": "add", "tags": ["#foo", "bar"]},
        ])
        assert err is None
        assert len(ops) == 1
        assert ops[0].handle == "uuid-1"
        assert ops[0].operation == "add"
        assert ops[0].tags == ("foo", "bar")
        assert ops[0].old_tag is None
        assert ops[0].new_tag is None

    def test_happy_path_remove(self):
        ops, err = _normalize_operations([
            {"handle": "uuid-1", "operation": "remove", "tags": ["foo"]},
        ])
        assert err is None
        assert ops[0].operation == "remove"
        assert ops[0].tags == ("foo",)

    def test_happy_path_rename(self):
        ops, err = _normalize_operations([
            {"handle": "uuid-1", "operation": "rename", "old_tag": "#old", "new_tag": "#new"},
        ])
        assert err is None
        assert ops[0].operation == "rename"
        assert ops[0].tags == ()
        assert ops[0].old_tag == "old"
        assert ops[0].new_tag == "new"

    def test_rename_takes_first_token_only(self):
        ops, err = _normalize_operations([
            {"handle": "uuid-1", "operation": "rename", "old_tag": ["old", "extra"], "new_tag": ["new", "extra2"]},
        ])
        assert err is None
        assert ops[0].old_tag == "old"
        assert ops[0].new_tag == "new"

    def test_duplicate_handles_rejected(self):
        ops, err = _normalize_operations([
            {"handle": "uuid-1", "operation": "add", "tags": ["foo"]},
            {"handle": "uuid-1", "operation": "add", "tags": ["bar"]},
        ])
        assert ops is None
        assert "duplicate" in err

    def test_missing_handle(self):
        ops, err = _normalize_operations([
            {"operation": "add", "tags": ["foo"]},
        ])
        assert ops is None
        assert "handle" in err

    def test_empty_handle(self):
        ops, err = _normalize_operations([
            {"handle": "  ", "operation": "add", "tags": ["foo"]},
        ])
        assert ops is None
        assert "handle" in err

    def test_add_empty_tags(self):
        ops, err = _normalize_operations([
            {"handle": "uuid-1", "operation": "add", "tags": []},
        ])
        assert ops is None
        assert "tags" in err and "non-empty" in err

    def test_rename_missing_old_tag(self):
        ops, err = _normalize_operations([
            {"handle": "uuid-1", "operation": "rename", "new_tag": "new"},
        ])
        assert ops is None
        assert "old_tag" in err

    def test_rename_missing_new_tag(self):
        ops, err = _normalize_operations([
            {"handle": "uuid-1", "operation": "rename", "old_tag": "old"},
        ])
        assert ops is None
        assert "new_tag" in err

    def test_unknown_verb(self):
        ops, err = _normalize_operations([
            {"handle": "uuid-1", "operation": "frobnicate", "tags": ["foo"]},
        ])
        assert ops is None
        assert "unknown" in err

    def test_unknown_field_add(self):
        ops, err = _normalize_operations([
            {"handle": "uuid-1", "operation": "add", "tags": ["foo"], "comment": "hi"},
        ])
        assert ops is None
        assert "unknown field" in err

    def test_unknown_field_rename(self):
        ops, err = _normalize_operations([
            {"handle": "uuid-1", "operation": "rename", "old_tag": "old", "new_tag": "new", "tags": ["foo"]},
        ])
        assert ops is None
        assert "unknown field" in err

    def test_non_dict_item(self):
        ops, err = _normalize_operations([
            "not an object",
        ])
        assert ops is None
        assert "must be an object" in err

    def test_missing_operation(self):
        ops, err = _normalize_operations([
            {"handle": "uuid-1", "tags": ["foo"]},
        ])
        assert ops is None
        assert "operation" in err and "required" in err


# =============================================================================
# WorkflowyTagMutationSpec.from_wire
# =============================================================================

class TestFromWire:
    def test_happy_path(self):
        spec, err = WorkflowyTagMutationSpec.from_wire({
            "action": "mutate_node_tags",
            "request_id": "test-1",
            "endpoint_id": "ep-1",
            "v": 1,
            "operations": [
                {"handle": "uuid-1", "operation": "add", "tags": ["foo"]},
            ],
        })
        assert err is None
        assert spec is not None
        assert spec.mode == "mutate_node_tags"
        assert len(spec.operations) == 1

    def test_unknown_top_level_field(self):
        spec, err = WorkflowyTagMutationSpec.from_wire({
            "action": "mutate_node_tags",
            "request_id": "test-1",
            "endpoint_id": "ep-1",
            "v": 1,
            "operations": [],
            "extra_field": "bad",
        })
        assert spec is None
        assert "unknown field" in err

    def test_wrong_version(self):
        spec, err = WorkflowyTagMutationSpec.from_wire({
            "action": "mutate_node_tags",
            "request_id": "test-1",
            "endpoint_id": "ep-1",
            "v": 2,
            "operations": [],
        })
        assert spec is None
        assert "version" in err

    def test_non_dict(self):
        spec, err = WorkflowyTagMutationSpec.from_wire("not a dict")
        assert spec is None
        assert "object" in err

    def test_bad_operations_propagate_error(self):
        spec, err = WorkflowyTagMutationSpec.from_wire({
            "action": "mutate_node_tags",
            "request_id": "test-1",
            "endpoint_id": "ep-1",
            "v": 1,
            "operations": [{"handle": "", "operation": "add", "tags": ["foo"]}],
        })
        assert spec is None
        assert "handle" in err


# =============================================================================
# WorkflowyTagMutationSpec.normalize_from_tool_args
# =============================================================================

class TestNormalizeFromToolArgs:
    def test_happy_path(self):
        spec, err = WorkflowyTagMutationSpec.normalize_from_tool_args(
            mode="mutate_node_tags",
            operations=[{"handle": "uuid-1", "operation": "add", "tags": ["foo"]}],
            comment=None,
            symbols=None,
            tags=None,
            root_uuids=None,
            root_uuid=None,
            favor="uuid",
        )
        assert err is None
        assert spec is not None
        assert len(spec.operations) == 1

    def test_rejects_comment(self):
        spec, err = WorkflowyTagMutationSpec.normalize_from_tool_args(
            mode="mutate_node_tags",
            operations=[{"handle": "uuid-1", "operation": "add", "tags": ["foo"]}],
            comment="hi",
            symbols=None,
            tags=None,
            root_uuids=None,
            root_uuid=None,
            favor="uuid",
        )
        assert spec is None
        assert "comment" in err

    def test_rejects_symbols(self):
        spec, err = WorkflowyTagMutationSpec.normalize_from_tool_args(
            mode="mutate_node_tags",
            operations=[{"handle": "uuid-1", "operation": "add", "tags": ["foo"]}],
            comment=None,
            symbols=["my_func"],
            tags=None,
            root_uuids=None,
            root_uuid=None,
            favor="uuid",
        )
        assert spec is None
        assert "symbols" in err

    def test_rejects_top_level_tags(self):
        spec, err = WorkflowyTagMutationSpec.normalize_from_tool_args(
            mode="mutate_node_tags",
            operations=[{"handle": "uuid-1", "operation": "add", "tags": ["foo"]}],
            comment=None,
            symbols=None,
            tags=["foo"],
            root_uuids=None,
            root_uuid=None,
            favor="uuid",
        )
        assert spec is None
        assert "operations-only" in err

    def test_rejects_root_uuid(self):
        spec, err = WorkflowyTagMutationSpec.normalize_from_tool_args(
            mode="mutate_node_tags",
            operations=[{"handle": "uuid-1", "operation": "add", "tags": ["foo"]}],
            comment=None,
            symbols=None,
            tags=None,
            root_uuids=None,
            root_uuid="some-root",
            favor="uuid",
        )
        assert spec is None
        assert "root_uuid" in err

    def test_rejects_favor_symbol(self):
        spec, err = WorkflowyTagMutationSpec.normalize_from_tool_args(
            mode="mutate_node_tags",
            operations=[{"handle": "uuid-1", "operation": "add", "tags": ["foo"]}],
            comment=None,
            symbols=None,
            tags=None,
            root_uuids=None,
            root_uuid=None,
            favor="symbol",
        )
        assert spec is None
        assert "favor" in err


# =============================================================================
# TagMutationOperation.to_dict
# =============================================================================

class TestTagMutationOperationToDict:
    def test_to_dict_add(self):
        op = TagMutationOperation(
            input_index=0,
            handle="uuid-1",
            operation="add",
            tags=("foo", "bar"),
            old_tag=None,
            new_tag=None,
        )
        d = op.to_dict()
        assert d == {
            "input_index": 0,
            "handle": "uuid-1",
            "operation": "add",
            "tags": ["foo", "bar"],
            "old_tag": None,
            "new_tag": None,
        }

    def test_to_dict_rename(self):
        op = TagMutationOperation(
            input_index=1,
            handle="uuid-2",
            operation="rename",
            tags=(),
            old_tag="old",
            new_tag="new",
        )
        d = op.to_dict()
        assert d["operation"] == "rename"
        assert d["tags"] == []
        assert d["old_tag"] == "old"
        assert d["new_tag"] == "new"


# =============================================================================
# WorkflowyTagMutationSpec.to_dict
# =============================================================================

class TestSpecToDict:
    def test_to_dict(self):
        spec, err = WorkflowyTagMutationSpec.from_wire({
            "action": "mutate_node_tags",
            "request_id": "test-1",
            "endpoint_id": "ep-1",
            "v": 1,
            "operations": [
                {"handle": "uuid-1", "operation": "add", "tags": ["foo"]},
                {"handle": "uuid-2", "operation": "rename", "old_tag": "old", "new_tag": "new"},
            ],
        })
        assert err is None
        d = spec.to_dict()
        assert d["mode"] == "mutate_node_tags"
        assert len(d["operations"]) == 2
        assert d["operations"][0]["handle"] == "uuid-1"
        assert d["operations"][1]["old_tag"] == "old"
