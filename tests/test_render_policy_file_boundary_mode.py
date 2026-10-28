"""ap:MSFBP Step B — RenderPolicy.file_boundary_mode field locks (UNWIRED).

These lock the pure-additive policy field before any producer/consumer reads
it:
  * it DEFAULTS to 'standalone_blocks' (so every existing RenderPolicy() is
    byte-for-byte the old behavior);
  * it round-trips through the FROZEN dataclass (settable at construction,
    immutable afterward);
  * it is orthogonal to the other three fields (setting it does not perturb
    rebase / breadcrumb / deprecated).
"""

from __future__ import annotations

import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workflowy_mcp.cache_render_spec import RenderPolicy  # noqa: E402


def test_default_is_standalone_blocks():
    p = RenderPolicy()
    assert p.file_boundary_mode == "standalone_blocks"


def test_connected_subtree_round_trips_through_frozen_dataclass():
    p = RenderPolicy(file_boundary_mode="connected_subtree")
    assert p.file_boundary_mode == "connected_subtree"
    # Frozen: cannot mutate in place.
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.file_boundary_mode = "standalone_blocks"  # type: ignore[misc]


def test_field_is_orthogonal_to_other_policy_fields():
    # Constructing with a non-default mode leaves the other three fields at
    # their own defaults (no coupling).
    p = RenderPolicy(file_boundary_mode="connected_subtree")
    assert p.rebase_by_render_key == {}
    assert p.breadcrumb_emit_body_false == frozenset()
    assert p.deprecated_emit_body_false == frozenset()
    # And a fully-default policy is unaffected.
    d = RenderPolicy()
    assert d.file_boundary_mode == "standalone_blocks"
    assert d.rebase_by_render_key == {}


def test_replace_preserves_and_overrides_mode():
    # dataclasses.replace (the frozen-safe copy pattern used across the code)
    # honors the new field.
    base = RenderPolicy(
        rebase_by_render_key={(0,): 2},
        breadcrumb_emit_body_false=frozenset({(0,)}),
    )
    assert base.file_boundary_mode == "standalone_blocks"
    connected = dataclasses.replace(base, file_boundary_mode="connected_subtree")
    assert connected.file_boundary_mode == "connected_subtree"
    # Other fields carried over unchanged.
    assert connected.rebase_by_render_key == {(0,): 2}
    assert connected.breadcrumb_emit_body_false == frozenset({(0,)})
    # Original untouched (frozen copy, not mutation).
    assert base.file_boundary_mode == "standalone_blocks"
