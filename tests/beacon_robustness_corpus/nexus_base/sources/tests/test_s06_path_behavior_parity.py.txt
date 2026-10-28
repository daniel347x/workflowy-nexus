"""S06 Gate C — path-derivation byte parity.

Locks byte-equivalence of Cartographer path derivation after the
NOTES_MARKER_* → CARTOGRAPHER_COSMETIC_SUFFIX_FIRST_CODEPOINTS rename: the
cosmetic suffix set holds exactly the pre-S06 first-codepoint values, and
resolve_cartographer_path_from_node derives identical segments for decorated
names (emoji prefixes, trailing cosmetic emoji, #tags) across
relative/absolute Path/Root chains.
"""

from __future__ import annotations

import os
from pathlib import Path

from workflowy_mcp.client.api_client_nexus import (
    CARTOGRAPHER_COSMETIC_SUFFIX_FIRST_CODEPOINTS,
    resolve_cartographer_path_from_node,
)

# The pre-S06 NOTES_MARKER_FIRST_CODEPOINTS values (locked for parity).
_ORIGINAL_NOTES_MARKER_FIRST_CODEPOINTS = frozenset(
    p[0]
    for p in frozenset({
        "📝", "🅿️", "🧩", "💥", "🌟", "💡", "📌", "📓", "🧾", "🧠", "🕯️", "🧨",
        "⚠", "❌", "✅", "🚨", "🔴", "🛑", "⛔", "❗",
    })
)


def test_cosmetic_constant_is_byte_identical_to_legacy_marker_set() -> None:
    assert (
        CARTOGRAPHER_COSMETIC_SUFFIX_FIRST_CODEPOINTS
        == _ORIGINAL_NOTES_MARKER_FIRST_CODEPOINTS
    )


def _tree(tmp_path: Path) -> dict[str, dict]:
    root_abs = str(tmp_path)
    return {
        "R": {
            "id": "R",
            "name": "📂 workflowy-nexus-mcp-tools",
            "note": f"Root: {root_abs}",
        },
        "C": {
            "id": "C",
            "name": "📂 client 📌",
            "note": "Path: src",
            "parent_id": "R",
        },
        "F": {
            "id": "F",
            "name": "🐍 api_client_nexus.py #tag1 #tag2",
            "note": "Path: client",
            "parent_id": "C",
        },
        "G": {
            "id": "G",
            "name": "🟨 file.py ⚠️",
            "note": "Path: file.py",
            "parent_id": "F",
        },
    }


def test_golden_decorated_name_segments_unchanged(tmp_path) -> None:
    nodes = _tree(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "client").mkdir()
    target = tmp_path / "src" / "client" / "file.py"
    target.write_bytes(b"x = 1\n")

    resolved = resolve_cartographer_path_from_node(node_id="G", nodes_by_id=nodes)
    assert os.path.normpath(resolved["abs_path"]) == os.path.normpath(str(target))
    assert os.path.normpath(resolved["root_abs"]) == os.path.normpath(str(tmp_path))

    # The name-derived fallback chain strips emoji prefixes, trailing cosmetic
    # emoji (📌, ⚠️), and #tags exactly as before (names, not Path values).
    by_name = resolved["rel_chain_by_name"]
    assert by_name == ["client", "api_client_nexus.py", "file.py"]


def test_legacy_absolute_path_node_resolution_unchanged(tmp_path) -> None:
    target = tmp_path / "standalone.py"
    target.write_bytes(b"x = 1\n")
    nodes = {
        "F": {"id": "F", "name": "🐍 standalone.py 💡", "note": f"Path: {target}"},
    }
    resolved = resolve_cartographer_path_from_node(node_id="F", nodes_by_id=nodes)
    assert os.path.normpath(resolved["abs_path"]) == os.path.normpath(str(target))


def test_segment_stripping_golden_cases(tmp_path) -> None:
    """Representative decorated names from the spec (§9.2) resolve to the
    same segments as before S06."""
    (tmp_path / "client").mkdir()
    (tmp_path / "client" / "file.py").write_bytes(b"x = 1\n")
    cases = [
        ("🐍 api_client_nexus.py #tag", "api_client_nexus.py"),
        ("📂 client 📌", "client"),
        ("🟨 file.py ⚠️", "file.py"),
        ("📂 client", "client"),
        ("🐍 name.py 🧠 💡 #t", "name.py"),
    ]
    for raw_name, expected_seg in cases:
        nodes = {
            "R": {"id": "R", "name": "📂 repo", "note": f"Root: {tmp_path}"},
            "C": {"id": "C", "name": raw_name, "note": "Path: client", "parent_id": "R"},
            "F": {
                "id": "F",
                "name": "🐍 file.py",
                "note": "Path: file.py",
                "parent_id": "C",
            },
        }
        resolved = resolve_cartographer_path_from_node(node_id="F", nodes_by_id=nodes)
        # The note-based chain is primary ('client'); the name-derived chain
        # must carry the same stripped segment for fallbacks.
        assert resolved["rel_chain_by_name"][0] == expected_seg, raw_name


def test_production_cosmetic_sites_use_only_the_neutral_constant() -> None:
    """Every cosmetic-stripping site uses the neutral constant and NOTHING
    marker-ish (A5-tightened): exact AST name census, no notes_* aliases,
    and all three stripping regions covered."""
    import ast

    source = (
        Path(__file__).resolve().parents[1]
        / "src" / "workflowy_mcp" / "client" / "api_client_nexus.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    constant_refs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "CARTOGRAPHER_COSMETIC_SUFFIX_FIRST_CODEPOINTS"
    ]
    # Exactly one definition-site store + five loads (resolver ×2,
    # folder-for-path ×2, folder-relpath ×1).
    stores = [n for n in constant_refs if isinstance(n.ctx, ast.Store)]
    loads = [n for n in constant_refs if isinstance(n.ctx, ast.Load)]
    assert len(stores) == 1
    assert len(loads) == 5, f"unexpected neutral-constant load count: {len(loads)}"

    # No marker-ish aliases survive anywhere.
    banned_names = {"notes_prefixes", "_notes_marker_cps", "NOTES_MARKER_FIRST_CODEPOINTS", "NOTES_MARKER_PREFIXES"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in banned_names:
            raise AssertionError(f"marker-ish alias {node.id} survives at line {node.lineno}")

    # All three cosmetic-stripping regions use the constant.
    for region in (
        "def resolve_cartographer_path_from_node",
        "def _segment_from_node_name_for_path",
        "def _compute_folder_relpath",
    ):
        start = source.index(region)
        window = source[start:start + 6000]
        assert "CARTOGRAPHER_COSMETIC_SUFFIX_FIRST_CODEPOINTS" in window, region
