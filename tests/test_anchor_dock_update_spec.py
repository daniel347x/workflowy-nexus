"""AnchorDockUpdateSpec.from_wire -- the §9 client-frame normalizer.

Mirrors the WorkflowyTagMutationSpec from_wire strictness tests
(tests/test_lightning_rod_tag_mutation_spec.py): unknown top-level field,
wrong version, non-dict, and the concern-specific requirements (missing
dock_handle; neither attribute present; JSON-typed attribute/families).

Model: local, no network.
"""

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.anchor_dock_update_spec import (  # noqa: E402
    AnchorDockUpdateSpec,
    _WIRE_FIELDS,
    _WIRE_VERSION,
)


def _frame(**overrides):
    frame = {
        "action": "anchor_dock_update",
        "request_id": "r-1",
        "endpoint_id": "ep-1",
        "v": 1,
        "dock_handle": "uuid-1",
        "favorite": True,
    }
    frame.update(overrides)
    return frame


class TestFromWire:
    def test_happy_path_favorite(self):
        spec, err = AnchorDockUpdateSpec.from_wire(_frame())
        assert err is None
        assert spec is not None
        assert spec.dock_handle == "uuid-1"
        assert spec.favorite is True
        assert spec.local is None
        assert spec.families is None

    def test_happy_path_local_false(self):
        spec, err = AnchorDockUpdateSpec.from_wire(
            _frame(favorite=None, local=False)
        )
        assert err is None
        assert spec.favorite is None
        assert spec.local is False

    def test_both_attributes_present(self):
        spec, err = AnchorDockUpdateSpec.from_wire(
            _frame(favorite=True, local=True)
        )
        assert err is None
        assert spec.favorite is True
        assert spec.local is True

    def test_families_string_folds_to_one_tuple(self):
        spec, err = AnchorDockUpdateSpec.from_wire(_frame(families="wfy-ns"))
        assert err is None
        assert spec.families == ("wfy-ns",)

    def test_families_list_folds_to_tuple(self):
        spec, err = AnchorDockUpdateSpec.from_wire(
            _frame(families=["wfy-ns", "convrisk-docks"])
        )
        assert err is None
        assert spec.families == ("wfy-ns", "convrisk-docks")

    def test_families_absent_is_none(self):
        spec, err = AnchorDockUpdateSpec.from_wire(_frame())
        assert err is None
        assert spec.families is None

    def test_unknown_top_level_field_rejected(self):
        spec, err = AnchorDockUpdateSpec.from_wire(_frame(extra_field="bad"))
        assert spec is None
        assert "unknown field" in err

    def test_wrong_version_rejected(self):
        spec, err = AnchorDockUpdateSpec.from_wire(_frame(v=2))
        assert spec is None
        assert "version" in err

    def test_non_integer_version_rejected(self):
        spec, err = AnchorDockUpdateSpec.from_wire(_frame(v="one"))
        assert spec is None
        assert "`v` must be an integer" in err

    def test_missing_v_defaults_to_one(self):
        frame = _frame()
        del frame["v"]
        spec, err = AnchorDockUpdateSpec.from_wire(frame)
        assert err is None
        assert spec is not None

    def test_non_dict_rejected(self):
        spec, err = AnchorDockUpdateSpec.from_wire("not a dict")
        assert spec is None
        assert "object" in err

    def test_missing_dock_handle_rejected(self):
        frame = _frame()
        del frame["dock_handle"]
        spec, err = AnchorDockUpdateSpec.from_wire(frame)
        assert spec is None
        assert "dock_handle" in err

    def test_blank_dock_handle_rejected(self):
        spec, err = AnchorDockUpdateSpec.from_wire(_frame(dock_handle="   "))
        assert spec is None
        assert "dock_handle" in err

    def test_non_string_dock_handle_rejected(self):
        spec, err = AnchorDockUpdateSpec.from_wire(_frame(dock_handle=123))
        assert spec is None
        assert "dock_handle" in err

    def test_neither_attribute_present_rejected(self):
        spec, err = AnchorDockUpdateSpec.from_wire(
            _frame(favorite=None, local=None)
        )
        assert spec is None
        assert "favorite" in err and "local" in err

    def test_non_bool_favorite_rejected(self):
        spec, err = AnchorDockUpdateSpec.from_wire(_frame(favorite="yes"))
        assert spec is None
        assert "favorite" in err

    def test_non_bool_local_rejected(self):
        spec, err = AnchorDockUpdateSpec.from_wire(
            _frame(favorite=None, local=[1])
        )
        assert spec is None
        assert "local" in err

    def test_families_bad_type_rejected(self):
        spec, err = AnchorDockUpdateSpec.from_wire(_frame(families={"a": 1}))
        assert spec is None
        assert "families" in err

    def test_families_non_string_entry_rejected(self):
        spec, err = AnchorDockUpdateSpec.from_wire(_frame(families=["ok", 3]))
        assert spec is None
        assert "families" in err

    def test_wire_field_set_is_the_declared_shell(self):
        # Guards against a silent widening/narrowing of the accepted shell.
        assert _WIRE_FIELDS == {
            "action",
            "request_id",
            "endpoint_id",
            "v",
            "dock_handle",
            "favorite",
            "local",
            "families",
        }
        assert _WIRE_VERSION == 1


class TestSpecToDict:
    def test_to_dict_is_json_serializable(self):
        spec, err = AnchorDockUpdateSpec.from_wire(
            _frame(families=["wfy-ns"], local=True)
        )
        assert err is None
        payload = spec.to_dict()
        assert payload == {
            "dock_handle": "uuid-1",
            "favorite": True,
            "local": True,
            "families": ["wfy-ns"],
        }
        # Round-trips through JSON without loss.
        assert json.loads(json.dumps(payload)) == payload

    def test_to_dict_none_families(self):
        spec, _ = AnchorDockUpdateSpec.from_wire(_frame())
        assert spec.to_dict()["families"] is None
