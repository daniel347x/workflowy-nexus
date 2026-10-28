"""The U3 describe texts (Archive Reads, §U3.5 test 5).

§5.7 (the archives.list summary — the canonical doctrine), §5.8 (the ONE
include_archives property), §5.10 (the family sentences), §5.11 (the mount
summary) and ARCHIVE_AUTO_SENTENCE are present exactly where §U3.2 says —
and the one-definition rule holds: every shared text is defined once in
``archive_texts.py`` and referenced everywhere else.
"""

from __future__ import annotations

import pathlib

from workflowy_mcp import server
from workflowy_mcp.archive_texts import (
    ARCHIVE_AUTO_SENTENCE,
    ARCHIVES_LIST_SUMMARY,
    ARCHIVES_MOUNT_SUMMARY,
    BEACON_FAMILY_SENTENCE,
    INCLUDE_ARCHIVES_PROPERTY,
)

SRC = pathlib.Path(server.__file__).parent

# §U3.2 D7: the six verb summaries that gain ARCHIVE_AUTO_SENTENCE.
AUTO_SENTENCE_VERBS = {
    ("node", "read"),
    ("node", "resolve"),
    ("node", "ancestors"),
    ("anchor", "dock.get_description"),
    ("anchor", "point.resolve"),
    ("anchor", "point.read"),
}

# §U3.2 D1: the 16 flag verbs (the property lives in exactly these schemas).
FLAG_VERBS = {
    "node.search",
    "node.scry",
    "anchor.search",
    "anchor.dock.peek",
    "anchor.dock.render",
    "anchor.dock.list",
    "slice_group.render",
    "slice_group.sift",
    "slice_group.merge.render",
    "glimpse.run",
    "glimpse.estate",
    "lightning_rod.gather",
    "lightning_rod.render_tags",
    "lightning_rod.render_search",
    "lightning_rod.render_nodes",
    "lightning_rod.render_symbols",
}


def _all_specs():
    for registry in (
        server.NODE_REGISTRY,
        server.ANCHOR_REGISTRY,
        server.TAG_REGISTRY,
        server.SLICE_GROUP_REGISTRY,
        server.GLIMPSE_REGISTRY,
        server.LIGHTNING_ROD_REGISTRY,
        server.BEACON_REGISTRY,
        server.SYSTEM_REGISTRY,
    ):
        for spec in registry.operations():
            yield spec


def test_archives_list_and_mount_summaries_match_the_constants():
    """§5.7 / §5.11 — registered with the verbs in Gate 3b; this 3a-stage
    guard pins the constants themselves so the registry can only adopt
    them verbatim."""
    import workflowy_mcp.archive_texts as texts

    assert "must stay in Workflowy" in texts.ARCHIVES_LIST_SUMMARY
    assert "It may be moved" in texts.ARCHIVES_LIST_SUMMARY
    assert "⚠️⚠️⚠️ ARCHIVED NODE" in texts.ARCHIVES_LIST_SUMMARY
    assert texts.ARCHIVES_MOUNT_SUMMARY.startswith(
        "Set or clear an archive's mount point"
    )


def test_include_archives_property_on_exactly_the_sixteen():
    carried = set()
    for spec in _all_specs():
        properties = (spec.params_schema or {}).get("properties") or {}
        if "include_archives" in properties:
            carried.add(spec.canonical_name)
            assert dict(properties["include_archives"]) == dict(
                INCLUDE_ARCHIVES_PROPERTY
            ), spec.canonical_name
    assert carried == FLAG_VERBS, sorted(carried ^ FLAG_VERBS)


def test_auto_sentence_on_exactly_the_six_summaries():
    """Manager-Session-147: the sentence rides the six verbs' describe_suffix
    (shown by describe) and never the summary (the always-on tool
    description, where the node / anchor family sentence already says it)."""
    for spec in _all_specs():
        key = (spec.family, spec.canonical_name.split(".", 1)[-1])
        assert ARCHIVE_AUTO_SENTENCE not in (spec.summary or ""), spec.canonical_name
        has = ARCHIVE_AUTO_SENTENCE in (spec.describe_suffix or "")
        if key in AUTO_SENTENCE_VERBS:
            assert has, spec.canonical_name
            registry = getattr(server, f"{spec.family.upper()}_REGISTRY")
            document = registry.describe_full(key[1])
            assert ARCHIVE_AUTO_SENTENCE in document["summary"], spec.canonical_name
        else:
            assert not has, f"unexpected ARCHIVE_AUTO_SENTENCE on {spec.canonical_name}"


def test_family_sentences_on_all_eight_summaries():
    """Manager-Session-147 layout: the full sentence on node, anchor and
    system; the short one on the render families; none on tag (writes only,
    refused with a reason on an archived node); beacon's own sentence."""
    from workflowy_mcp.archive_texts import (
        ARCHIVE_FAMILY_SENTENCE,
        ARCHIVE_FAMILY_SHORT_SENTENCE,
    )

    for registry in (server.NODE_REGISTRY, server.ANCHOR_REGISTRY, server.SYSTEM_REGISTRY):
        assert ARCHIVE_FAMILY_SENTENCE in registry.family_summary, registry.family
    for registry in (
        server.SLICE_GROUP_REGISTRY,
        server.GLIMPSE_REGISTRY,
        server.LIGHTNING_ROD_REGISTRY,
    ):
        assert ARCHIVE_FAMILY_SHORT_SENTENCE in registry.family_summary, registry.family
        assert ARCHIVE_FAMILY_SENTENCE not in registry.family_summary, registry.family
    for registry in (
        server.NODE_REGISTRY,
        server.ANCHOR_REGISTRY,
        server.SLICE_GROUP_REGISTRY,
        server.GLIMPSE_REGISTRY,
        server.LIGHTNING_ROD_REGISTRY,
        server.SYSTEM_REGISTRY,
    ):
        assert "📦 Export archives:" in registry.family_summary, registry.family
        assert "system describe of archives.list" in registry.family_summary
    assert "📦" not in server.TAG_REGISTRY.family_summary
    assert "Beacon never reads export archives" in server.BEACON_REGISTRY.family_summary
    assert "📦 Export archives:" not in server.BEACON_REGISTRY.family_summary


def test_archives_list_rules_live_in_describe():
    """Manager-Session-147: the always-on index line is the short summary;
    describe carries the full rules every family points to."""
    from workflowy_mcp.archive_texts import ARCHIVES_LIST_INDEX_SUMMARY

    spec = server.SYSTEM_REGISTRY.resolve("archives.list")
    assert spec.summary == ARCHIVES_LIST_INDEX_SUMMARY
    assert ARCHIVES_LIST_SUMMARY not in server._SYSTEM_TOOL_DESCRIPTION
    document = server.SYSTEM_REGISTRY.describe_full("archives.list")
    assert ARCHIVES_LIST_SUMMARY in document["summary"]


def test_the_property_description_is_defined_exactly_once():
    """The §5.8 description string exists ONLY in archive_texts.py — every
    schema embeds the constant by reference (the §U3.3 grep postcondition)."""

    needle = "Default false. true adds the active export archives"
    hits = []
    for path in SRC.rglob("*.py"):
        if needle in path.read_text(encoding="utf-8"):
            hits.append(path.name)
    assert hits == ["archive_texts.py"], hits


def test_describe_full_hints_are_wellformed():
    """The family describe sweep stays intact with the appended sentences
    (the §U3.3 measurement-test disposition watches the wire shape)."""
    document = server.NODE_REGISTRY.describe_full("read")
    assert "system describe of archives.list" in str(document)
