"""Focused regression locks for the SINCE OBJECT-ONLY contract (v5).

Small, high-value pass covering the load-bearing invariants of the
since↔object refactor: a `since` is a SinceSpec (or None) everywhere in the
interior, the WIRE carries an OBJECT DICT (never a token string, even for
default chronology), the strict wire reader REJECTS strings, and the ONLY
string↔object conversions live at the named edges (public token ingestion +
display). These replace the deleted string-contract suites.

No server / websocket wiring is exercised here -- pure codec + spec + wire
shape, so the file is fast and dependency-light.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from workflowy_mcp.glimpse_since_policy import (  # noqa: E402
    SinceSpec,
    SinceCodec,
    since_from_edge,
    since_from_wire,
    since_to_wire,
)
from workflowy_mcp.cache_render_spec import RenderProjectionSpec  # noqa: E402

import pytest  # noqa: E402


# ---------------------------------------------------------------------------
# EDGE ingestion (string -> object) -- the ONLY string-accepting entry.
# ---------------------------------------------------------------------------
def test_edge_ingest_window_returns_object():
    spec = since_from_edge("1w")
    assert isinstance(spec, SinceSpec)
    assert spec.form == "window"
    assert spec.window_seconds == 604800
    assert spec.axis == "created"
    assert spec.direction == "asc"


def test_edge_ingest_item_count_returns_object():
    spec = since_from_edge("last 10")
    assert isinstance(spec, SinceSpec)
    assert spec.form == "item_count"
    assert spec.count == 10


def test_edge_ingest_curated_returns_object():
    spec = since_from_edge("curated")
    assert isinstance(spec, SinceSpec)
    assert spec.form == "curated"


def test_edge_ingest_blank_and_none_are_none():
    assert since_from_edge("") is None
    assert since_from_edge(None) is None


def test_edge_ingest_fractional_window_python_side():
    # Python has always accepted fractional durations; this locks it so the
    # JS/Python codec parity (the one audit-found mismatch, now fixed on the JS
    # side) does not silently regress on the Python side.
    spec = since_from_edge("1.5h")
    assert isinstance(spec, SinceSpec)
    assert spec.form == "window"
    assert spec.window_seconds == 5400  # 1.5 * 3600


# ---------------------------------------------------------------------------
# WIRE is OBJECT-ONLY (Constraint #1): every non-None SinceSpec -> a dict,
# INCLUDING default created/asc chronology. No string collapse branch.
# ---------------------------------------------------------------------------
def test_wire_write_default_chronology_is_object_dict():
    spec = SinceSpec(form="window", window_seconds=604800)  # created/asc
    wire = since_to_wire(spec)
    assert isinstance(wire, dict)
    assert wire == {
        "form": "window",
        "axis": "created",
        "direction": "asc",
        "window_seconds": 604800,
    }


def test_wire_write_none_is_none():
    assert since_to_wire(None) is None


def test_wire_write_item_count_object():
    # [ap:TVIIBN] 4d(a): the selection rides the wire EXPLICITLY (an old blob
    # without the key still DECODES to the positional 'bottom' -- see
    # test_wire_read_item_count_without_selection_is_positional_back_compat).
    wire = since_to_wire(SinceSpec(form="item_count", count=5, selection="newest"))
    assert wire == {
        "form": "item_count",
        "axis": "created",
        "direction": "asc",
        "count": 5,
        "selection": "newest",
    }


def test_wire_roundtrip_object_in_object_out():
    for spec in (
        SinceSpec(form="window", window_seconds=86400, axis="modified", direction="desc"),
        SinceSpec(form="item_count", count=3),
        SinceSpec(form="curated"),
    ):
        assert since_from_wire(since_to_wire(spec)) == spec


# ---------------------------------------------------------------------------
# WIRE reader REJECTS strings (Constraint #2): a string on the interior wire
# is invalid -- it is not parsed, it raises.
# ---------------------------------------------------------------------------
def test_wire_read_rejects_string():
    with pytest.raises(TypeError):
        since_from_wire("1w")


def test_wire_read_passes_object_and_none():
    assert since_from_wire(None) is None
    obj = {"form": "window", "window_seconds": 3600, "axis": "created", "direction": "asc"}
    assert since_from_wire(obj) == SinceSpec(form="window", window_seconds=3600)


# ---------------------------------------------------------------------------
# DISPLAY (object -> token) -- the SOLE object->string path, and it is lossless.
# ---------------------------------------------------------------------------
def test_display_token_roundtrips_through_edge():
    for token in ("604800s", "last 10", "curated"):
        spec = since_from_edge(token)
        assert spec is not None
        # from_token(to_token(spec)) == spec  (codec bijection invariant)
        assert SinceCodec.from_token(SinceCodec.to_token(spec)) == spec


# ---------------------------------------------------------------------------
# RenderProjectionSpec.since is OBJECT-typed, and the interior refuses strings.
# ---------------------------------------------------------------------------
def test_spec_default_since_is_none():
    assert RenderProjectionSpec().since is None


def test_spec_normalizer_rejects_string_since():
    with pytest.raises(TypeError):
        RenderProjectionSpec.normalize_from_glimpse_args(since="1w")


def test_spec_normalizer_accepts_since_spec():
    spec = RenderProjectionSpec.normalize_from_glimpse_args(
        since=SinceSpec(form="window", window_seconds=604800)
    )
    assert isinstance(spec.since, SinceSpec)
    assert spec.since.form == "window"


def test_spec_blob_carries_since_object_and_roundtrips():
    src = RenderProjectionSpec.normalize_from_glimpse_args(
        root_uuid="root-x",
        since=SinceSpec(form="item_count", count=7),
    )
    blob = src.to_render_spec_blob()
    # The blob's `since` is the OBJECT dict, never a token string.
    assert isinstance(blob["since"], dict)
    assert blob["since"]["form"] == "item_count"
    assert blob["since"]["count"] == 7
    # And it round-trips back to an equal spec.
    back = RenderProjectionSpec.from_render_spec_blob(blob)
    assert back.since == src.since


def test_spec_render_scope_fields_carry_since_object():
    src = RenderProjectionSpec.normalize_from_glimpse_args(
        group_id="grp-x",
        since=SinceSpec(form="window", window_seconds=259200),  # 3d
    )
    fields = src.to_render_scope_fields()
    assert isinstance(fields["since"], dict)
    assert fields["since"]["form"] == "window"
    assert fields["since"]["window_seconds"] == 259200


def test_spec_blob_none_since_stays_none():
    src = RenderProjectionSpec.normalize_from_glimpse_args(root_uuid="root-x")
    blob = src.to_render_spec_blob()
    assert blob["since"] is None
    assert RenderProjectionSpec.from_render_spec_blob(blob).since is None
