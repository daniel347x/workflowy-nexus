"""Tests for the protected-name policy on rename surfaces.

A free-form rename (node.edit / workflowy_edit_node, anchor.dock.update topic,
legacy update_anchor description) may replace ONLY the ordinary title text of
a node NAME. The leading [ap:CORE] anchor-point handle and the trailing #tag
region are preserved automatically; supplying them in a rename payload is a
loud ProtectedNameViolation.

Layout:
  * The PURE policy (_compose_protected_name) is exercised directly (no mocks).
  * The async core (_node_edit_core) is exercised with a monkeypatched client,
    handle resolver, and rate limiter (same harness as
    test_edit_node_note_search_replace.py).
  * The legacy update_anchor client path (update_note_session) is exercised
    with instance-level AsyncMocks on an uninitialized client object.

server.py and the client modules live under src/workflowy_mcp/ and are covered
by the editable install; the repo-root modules (e.g. nexus_map_codebase.py,
home of the canonical split_name_and_tags) are COPIED into the venv at install
time, so `pip install -e .` must be re-run after editing them (see the repo's
blazing warning).
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workflowy_mcp import server  # noqa: E402
from workflowy_mcp.client import api_client_nexus  # noqa: E402
from workflowy_mcp.client.api_client_nexus import (  # noqa: E402
    LeadingEmojiMutation,
    ProtectedNameViolation,
    WorkFlowyClientNexus,
    _compose_protected_name,
)
from workflowy_mcp.models.node import WorkFlowyNode  # noqa: E402
from test_mutation_regions_n05_fences import legacy_region_fronts


@pytest.fixture(autouse=True)
def _region_boundary(monkeypatch, tmp_path):
    legacy_region_fronts(monkeypatch, tmp_path,
        nodes=[{"id": "11111111-1111-4111-8111-111111111111", "parent_id": None}],
        entries=("_node_edit_core", "_node_mutate_leading_emoji_impl"))


def _fake_node(node_id="11111111-1111-4111-8111-111111111111", name="Title", note=""):
    """A real WorkFlowyNode so model_copy/model_dump behave genuinely."""
    return WorkFlowyNode(id=node_id, name=name, note=note)


def _patch_boundary(monkeypatch, client):
    """Neutralize the client, handle resolution, and rate limiter."""
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)

    async def _resolve(node_id, *, label, required=True, role=None):
        return node_id

    monkeypatch.setattr(server, "_resolve_mutator_node_id", _resolve)
    monkeypatch.setattr(server, "_resolve_generic_node_handle", _resolve)


# ---------------------------------------------------------------------------
# PURE COMPOSER -- ordinary renames
# ---------------------------------------------------------------------------


def test_plain_rename_untagged_node_is_byte_verbatim():
    r = _compose_protected_name(current_semantic_name="Old title", requested_name="New title")
    assert r["name"] == "New title"
    assert r["tags"] == []
    assert r["ap_id"] is None


def test_existing_trailing_tags_preserved_on_rename():
    r = _compose_protected_name(
        current_semantic_name="Old title #foo #bar", requested_name="New title"
    )
    assert r["name"] == "New title #foo #bar"
    assert r["tags"] == ["#foo", "#bar"]


def test_mid_name_hash_is_not_a_trailing_tag():
    r = _compose_protected_name(current_semantic_name="Old", requested_name="C# notes")
    assert r["name"] == "C# notes"


def test_empty_requested_title_is_allowed():
    # No empty-base guard (Dan, 2026): renaming down to just the protected
    # regions is permitted.
    r = _compose_protected_name(current_semantic_name="[ap:ABC123] Old #t", requested_name=" ")
    assert r["name"] == "[ap:ABC123] #t"


# ---------------------------------------------------------------------------
# PURE COMPOSER -- the tag guard
# ---------------------------------------------------------------------------


def test_supplied_single_tag_rejected_loudly():
    with pytest.raises(ProtectedNameViolation) as exc:
        _compose_protected_name(current_semantic_name="Old title", requested_name="New title #foo")
    assert exc.value.code == "tags_in_name_rejected"
    assert "#foo" in str(exc.value)
    assert "tag.add" in str(exc.value)


def test_supplied_multiple_tags_rejected_and_listed():
    with pytest.raises(ProtectedNameViolation) as exc:
        _compose_protected_name(current_semantic_name="Old #a", requested_name="New #x #y")
    assert exc.value.code == "tags_in_name_rejected"
    assert exc.value.extra["supplied_tags"] == ["#x", "#y"]


def test_supplied_tags_identical_to_current_are_still_rejected():
    # Strict rule (Dan, 2026): edit payloads never carry tags at all -- even
    # the exact tags the node already has.
    with pytest.raises(ProtectedNameViolation) as exc:
        _compose_protected_name(current_semantic_name="Old #foo", requested_name="New #foo")
    assert exc.value.code == "tags_in_name_rejected"


# ---------------------------------------------------------------------------
# PURE COMPOSER -- the AP-ID guard
# ---------------------------------------------------------------------------


def test_existing_ap_id_preserved_when_omitted_from_request():
    r = _compose_protected_name(
        current_semantic_name="[ap:ABC123] Old title", requested_name="New title"
    )
    assert r["name"] == "[ap:ABC123] New title"
    assert r["ap_id"] == "[ap:ABC123]"


def test_existing_ap_id_and_tags_both_preserved():
    r = _compose_protected_name(
        current_semantic_name="[ap:ABC123] Old title #foo", requested_name="New title"
    )
    assert r["name"] == "[ap:ABC123] New title #foo"


def test_identical_ap_id_may_be_repeated_and_is_emitted_once():
    r = _compose_protected_name(
        current_semantic_name="[ap:ABC123] Old title",
        requested_name="[ap:ABC123] New title",
    )
    assert r["name"] == "[ap:ABC123] New title"


def test_identical_ap_id_different_case_keeps_stored_spelling():
    r = _compose_protected_name(
        current_semantic_name="[ap:ABC123] Old title",
        requested_name="[ap:abc123] New title",
    )
    assert r["name"] == "[ap:ABC123] New title"


def test_different_ap_id_is_a_loud_rename_rejection():
    with pytest.raises(ProtectedNameViolation) as exc:
        _compose_protected_name(
            current_semantic_name="[ap:ABC123] Old", requested_name="[ap:XYZ999] New"
        )
    assert exc.value.code == "ap_id_mutation_rejected"
    assert exc.value.extra["current_ap_id"] == "ABC123"
    assert exc.value.extra["supplied_ap_id"] == "XYZ999"


def test_ap_id_supplied_when_node_has_none_is_a_loud_add_rejection():
    with pytest.raises(ProtectedNameViolation) as exc:
        _compose_protected_name(
            current_semantic_name="Plain title", requested_name="[ap:NEW123] Title"
        )
    assert exc.value.code == "ap_id_mutation_rejected"
    assert "anchor.point.assign_id" in str(exc.value)


def test_mid_title_ap_citation_is_allowed_content():
    r = _compose_protected_name(
        current_semantic_name="Old", requested_name="Discussion of [ap:K7M2QF] findings"
    )
    assert r["name"] == "Discussion of [ap:K7M2QF] findings"


def test_mid_title_self_citation_produces_documented_textual_duplicate():
    # Locked-in DOCUMENTED behavior (Fable/Sol review): the leading handle is
    # the identity; a mid-title [ap:] is citation text, so the stored handle
    # is preserved AND the citation text rides along. No whole-title scanning.
    r = _compose_protected_name(
        current_semantic_name="[ap:ABC123] Old",
        requested_name="New mentioning [ap:ABC123]",
    )
    assert r["name"] == "[ap:ABC123] New mentioning [ap:ABC123]"


def test_tags_are_checked_before_ap_id():
    with pytest.raises(ProtectedNameViolation) as exc:
        _compose_protected_name(
            current_semantic_name="Plain", requested_name="[ap:NEW123] Title #tag"
        )
    assert exc.value.code == "tags_in_name_rejected"


# ---------------------------------------------------------------------------
# PURE COMPOSER -- trident / emoji edge table (canonical splitter semantics)
# ---------------------------------------------------------------------------


def test_trident_dropped_and_tags_preserved():
    r = _compose_protected_name(
        current_semantic_name="Old title 🔱 #foo", requested_name="New title"
    )
    assert r["name"] == "New title #foo"


def test_emoji_left_of_trident_is_part_of_the_replaced_title():
    r = _compose_protected_name(
        current_semantic_name="Old title 🚀 🔱 #foo", requested_name="New title"
    )
    assert r["name"] == "New title #foo"


def test_emoji_right_of_trident_drops_with_the_region():
    r = _compose_protected_name(
        current_semantic_name="Old title 🔱 🚀 #foo", requested_name="New title"
    )
    assert r["name"] == "New title #foo"


def test_trailing_content_emoji_without_tags_stays_in_title():
    r = _compose_protected_name(
        current_semantic_name="Ship it 🚀", requested_name="Launch it ✅"
    )
    assert r["name"] == "Launch it ✅"


# ---------------------------------------------------------------------------
# PURE COMPOSER -- leading-emoji preservation (post-audit fix: the leading
# decoration strip is provisional; without a following AP handle it must not
# eat ordinary title content)
# ---------------------------------------------------------------------------


def test_requested_leading_emoji_survives_on_tagged_node():
    r = _compose_protected_name(
        current_semantic_name="Old #tag", requested_name="🚀 New"
    )
    assert r["name"] == "🚀 New #tag"


def test_existing_leading_emoji_survives_a_tag_transform():
    r = _compose_protected_name(
        current_semantic_name="⚡ Topic #wfy-ns", tag_transform=lambda tags: tags
    )
    assert r["name"] == "⚡ Topic #wfy-ns"


def test_existing_leading_emoji_survives_ap_assignment():
    r = _compose_protected_name(
        current_semantic_name="⚡ Name #tag", assign_ap_core="NEW123"
    )
    assert r["name"] == "[ap:NEW123] ⚡ Name #tag"


def test_leading_decoration_still_bundles_with_a_present_ap_handle():
    # The provisional strip still applies WHEN a handle follows: the stored
    # decoration + token form the preserved prefix unit.
    r = _compose_protected_name(
        current_semantic_name="🔱 [ap:ABC123] Old #t", requested_name="New"
    )
    assert r["name"] == "🔱 [ap:ABC123] New #t"


# ---------------------------------------------------------------------------
# PURE COMPOSER -- the narrow internal channels
# ---------------------------------------------------------------------------


def test_tag_transform_channel_rewrites_only_via_callable():
    r = _compose_protected_name(
        current_semantic_name="Topic #wfy-ns #other",
        tag_transform=lambda tags: ["#wfy-ns--local" if t == "#wfy-ns" else t for t in tags],
    )
    assert r["name"] == "Topic #wfy-ns--local #other"


def test_assign_channel_adds_ap_preserving_title_and_tags():
    r = _compose_protected_name(current_semantic_name="Name #tag", assign_ap_core="NEW123")
    assert r["name"] == "[ap:NEW123] Name #tag"
    assert r["ap_id"] == "[ap:NEW123]"


def test_assign_channel_refuses_when_ap_already_exists():
    with pytest.raises(ProtectedNameViolation) as exc:
        _compose_protected_name(current_semantic_name="[ap:ABC123] Name", assign_ap_core="NEW123")
    assert exc.value.code == "ap_id_mutation_rejected"


def test_remove_channel_removes_only_ap_preserving_title_and_tags():
    r = _compose_protected_name(
        current_semantic_name="[ap:ABC123] Name #tag",
        remove_ap_id=True,
    )
    assert r["name"] == "Name #tag"
    assert r["ap_id"] is None
    assert r["tags"] == ["#tag"]


def test_remove_channel_preserves_decoration_that_preceded_ap():
    r = _compose_protected_name(
        current_semantic_name="🔱 [ap:ABC123] Name #tag",
        remove_ap_id=True,
    )
    assert r["name"] == "🔱 Name #tag"


def test_remove_channel_on_apless_node_is_exact_noop():
    current = "⚡ Name 🔱 #tag"
    r = _compose_protected_name(
        current_semantic_name=current,
        remove_ap_id=True,
    )
    assert r["name"] == current
    assert r["ap_id"] is None


def test_assign_and_remove_channels_are_mutually_exclusive():
    with pytest.raises(ValueError, match="mutually exclusive"):
        _compose_protected_name(
            current_semantic_name="Name",
            assign_ap_core="NEW123",
            remove_ap_id=True,
        )


def test_remove_channel_never_combines_with_requested_name():
    with pytest.raises(ValueError, match="remove_ap_id"):
        _compose_protected_name(
            current_semantic_name="[ap:ABC123] Name",
            requested_name="New",
            remove_ap_id=True,
        )


def test_assign_channel_never_combines_with_a_requested_name():
    with pytest.raises(ValueError):
        _compose_protected_name(
            current_semantic_name="Name", requested_name="X", assign_ap_core="NEW123"
        )


# ---------------------------------------------------------------------------
# ASYNC CORE -- public node.edit path
# ---------------------------------------------------------------------------


def test_edit_name_with_tags_fails_loudly_with_zero_write(monkeypatch):
    client = MagicMock()
    client.get_node = AsyncMock(return_value=_fake_node(name="Old #foo"))
    client.update_node = AsyncMock()
    _patch_boundary(monkeypatch, client)

    with pytest.raises(ProtectedNameViolation) as exc:
        asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", name="New #bar"))
    assert exc.value.code == "tags_in_name_rejected"
    client.update_node.assert_not_called()


def test_edit_name_preserves_existing_tags_and_trident_drops(monkeypatch):
    client = MagicMock()
    client.get_node = AsyncMock(return_value=_fake_node(name="Old title 🔱 #foo"))
    client.update_node = AsyncMock(return_value=_fake_node(name="New title #foo"))
    _patch_boundary(monkeypatch, client)

    asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", name="New title"))
    request = client.update_node.call_args[0][1]
    assert request.name == "New title #foo"
    client.get_node.assert_awaited_once()


def test_edit_name_plus_note_search_uses_one_live_get_and_one_write(monkeypatch):
    client = MagicMock()
    client.get_node = AsyncMock(return_value=_fake_node(name="Old #foo", note="alpha beta"))
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", name="New", note_search="alpha", note_replace="gamma"))
    assert client.get_node.await_count == 1
    request = client.update_node.call_args[0][1]
    assert request.name == "New #foo"
    assert request.note == "gamma beta"


def test_edit_name_plus_note_edits_uses_one_live_get_and_one_write(monkeypatch):
    client = MagicMock()
    client.get_node = AsyncMock(return_value=_fake_node(name="[ap:ABC123] Old #foo", note="alpha beta"))
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    asyncio.run(
        server.edit_node(
            "11111111-1111-4111-8111-111111111111",
            name="New",
            note_edits=[{"search": "alpha", "replace": "gamma"}],
        )
    )
    assert client.get_node.await_count == 1
    request = client.update_node.call_args[0][1]
    assert request.name == "[ap:ABC123] New #foo"
    assert request.note == "gamma beta"


def test_plain_note_only_edit_fetches_once_for_the_default_diff(monkeypatch):
    # [ap:4JSBO5]: a whole-note edit reports its size-guarded diff BY
    # DEFAULT, so exactly ONE memoized live GET supplies the before text;
    # the core still fetches nothing beyond it.
    client = MagicMock()
    client.get_node = AsyncMock(return_value=_fake_node(note="old"))
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", note="brand new note"))
    client.get_node.assert_awaited_once()
    client.update_node.assert_awaited_once()
    assert isinstance(result, str) and result.startswith("{")


def test_plain_note_only_edit_with_return_diff_false_returns_only_header(monkeypatch):
    # False now aliases none on every note write: one truthful live snapshot,
    # no rendered diff, and no echoed note body.
    client = MagicMock()
    client.get_node = AsyncMock(return_value=_fake_node(note="old"))
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server._node_edit_impl("11111111-1111-4111-8111-111111111111", note="brand new note", return_diff=False))
    client.get_node.assert_awaited_once()
    client.update_node.assert_awaited_once()
    assert isinstance(result, str) and "brand new note" not in result


def test_layout_only_edit_does_not_fetch_for_name_protection(monkeypatch):
    client = MagicMock()
    client.get_node = AsyncMock()
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", layout_mode="h1"))
    client.get_node.assert_not_called()
    client.update_node.assert_awaited_once()


def test_wire_form_current_name_is_decoded_once_never_double_encoded(monkeypatch):
    # The raw GET name is entity-encoded wire form; composition must decode
    # ONE non-cascading layer first or the re-encode on write double-encodes.
    client = MagicMock()
    client.get_node = AsyncMock(return_value=_fake_node(name="AT&amp;T Dock #wfy-ns"))
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    asyncio.run(server._node_edit_core("11111111-1111-4111-8111-111111111111", _tag_transform=lambda tags: tags))
    request = client.update_node.call_args[0][1]
    assert request.name == "AT&T Dock #wfy-ns"


def test_core_assign_channel_with_injected_snapshot_skips_get(monkeypatch):
    node = _fake_node(name="Name  #tag")  # wire-form name, injected by assign_id
    client = MagicMock()
    client.get_node = AsyncMock()
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    asyncio.run(server._node_edit_core("11111111-1111-4111-8111-111111111111", _assign_ap_core="NEW123", _current_node=node))
    request = client.update_node.call_args[0][1]
    assert request.name == "[ap:NEW123] Name #tag"
    client.get_node.assert_not_called()


def test_core_remove_channel_with_injected_snapshot_skips_get(monkeypatch):
    node = _fake_node(name="🔱 [ap:ABC123] Name #tag")
    client = MagicMock()
    client.get_node = AsyncMock()
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    asyncio.run(server._node_edit_core("11111111-1111-4111-8111-111111111111", _remove_ap_id=True, _current_node=node))
    request = client.update_node.call_args[0][1]
    assert request.name == "🔱 Name #tag"
    client.get_node.assert_not_called()


# ---------------------------------------------------------------------------
# node.update_emoji / prepend_emoji / append_emoji -- pure policy + impl
# ---------------------------------------------------------------------------


def _emoji_mutation(emoji: str, mode: str = "replace") -> LeadingEmojiMutation:
    return LeadingEmojiMutation(mode=mode, emoji=emoji)


def test_update_emoji_inserts_when_no_leading_run_exists():
    r = _compose_protected_name(
        current_semantic_name="Task #tag",
        leading_emoji_mutation=_emoji_mutation("✅"),
    )
    assert r["name"] == "✅ Task #tag"
    assert r["emoji"] == "✅"


def test_update_emoji_replaces_only_leading_run_after_ap():
    r = _compose_protected_name(
        current_semantic_name="[ap:ABC123] 🚀 💡 Task 🧠 end #tag",
        leading_emoji_mutation=_emoji_mutation("✅"),
    )
    assert r["name"] == "[ap:ABC123] ✅ Task 🧠 end #tag"


def test_update_emoji_blank_removes_only_leading_run():
    r = _compose_protected_name(
        current_semantic_name="🚀 Task 💡 end #tag",
        leading_emoji_mutation=_emoji_mutation("   "),
    )
    assert r["name"] == "Task 💡 end #tag"
    assert r["emoji"] == ""


def test_update_emoji_blank_with_no_leading_run_preserves_exact_name_bytes():
    current = "Task 🔱  #tag"
    r = _compose_protected_name(
        current_semantic_name=current,
        leading_emoji_mutation=_emoji_mutation(""),
    )
    assert r["name"] == current


def test_update_emoji_preserves_legacy_decoration_before_ap():
    r = _compose_protected_name(
        current_semantic_name="🔱 [ap:ABC123] 🚀 Task #tag",
        leading_emoji_mutation=_emoji_mutation("✅"),
    )
    assert r["name"] == "🔱 [ap:ABC123] ✅ Task #tag"


def test_update_emoji_accepts_zwj_and_keycap_runs():
    r = _compose_protected_name(
        current_semantic_name="Task",
        leading_emoji_mutation=_emoji_mutation("👩‍💻 1️⃣"),
    )
    assert r["name"] == "👩‍💻 1️⃣ Task"
    assert r["emoji"] == "👩‍💻 1️⃣"


def test_update_emoji_rejects_ordinary_text():
    with pytest.raises(ProtectedNameViolation) as exc:
        _compose_protected_name(
            current_semantic_name="Task",
            leading_emoji_mutation=_emoji_mutation("✅ done"),
        )
    assert exc.value.code == "invalid_emoji_value"
    assert "node.edit" in str(exc.value)


def test_emoji_channel_is_exclusive_with_rename_and_ap_channels():
    for mode in ("replace", "prepend", "append"):
        with pytest.raises(ValueError, match="leading_emoji_mutation"):
            _compose_protected_name(
                current_semantic_name="Task",
                requested_name="New",
                leading_emoji_mutation=_emoji_mutation("✅", mode),
            )
        with pytest.raises(ValueError, match="leading_emoji_mutation"):
            _compose_protected_name(
                current_semantic_name="Task",
                assign_ap_core="ABC123",
                leading_emoji_mutation=_emoji_mutation("✅", mode),
            )


# ---------------------------------------------------------------------------
# node.prepend_emoji / node.append_emoji -- pure policy
# ---------------------------------------------------------------------------


def test_prepend_emoji_inserts_before_existing_run():
    r = _compose_protected_name(
        current_semantic_name="🎫 Task #tag",
        leading_emoji_mutation=_emoji_mutation("✅", "prepend"),
    )
    assert r["name"] == "✅ 🎫 Task #tag"
    assert r["emoji"] == "✅ 🎫"
    assert r["added_emoji"] == "✅"


def test_append_emoji_inserts_after_existing_run():
    r = _compose_protected_name(
        current_semantic_name="🎫 Task #tag",
        leading_emoji_mutation=_emoji_mutation("✅", "append"),
    )
    assert r["name"] == "🎫 ✅ Task #tag"
    assert r["emoji"] == "🎫 ✅"
    assert r["added_emoji"] == "✅"


def test_insert_verbs_match_when_no_existing_run():
    for mode in ("prepend", "append"):
        r = _compose_protected_name(
            current_semantic_name="Task #tag",
            leading_emoji_mutation=_emoji_mutation("✅", mode),
        )
        assert r["name"] == "✅ Task #tag"
        assert r["emoji"] == "✅"
        assert r["added_emoji"] == "✅"


def test_prepend_emoji_after_ap_preserves_handle_and_legacy_decoration():
    r = _compose_protected_name(
        current_semantic_name="[ap:ABC123] 🚀 Task #tag",
        leading_emoji_mutation=_emoji_mutation("✅", "prepend"),
    )
    assert r["name"] == "[ap:ABC123] ✅ 🚀 Task #tag"
    r = _compose_protected_name(
        current_semantic_name="🔱 [ap:ABC123] 🚀 Task #tag",
        leading_emoji_mutation=_emoji_mutation("✅", "prepend"),
    )
    assert r["name"] == "🔱 [ap:ABC123] ✅ 🚀 Task #tag"


def test_append_emoji_after_ap_lands_after_run():
    r = _compose_protected_name(
        current_semantic_name="[ap:ABC123] 🚀 Task #tag",
        leading_emoji_mutation=_emoji_mutation("✅", "append"),
    )
    assert r["name"] == "[ap:ABC123] 🚀 ✅ Task #tag"


def test_insert_verbs_leave_middle_and_right_emojis_untouched():
    r = _compose_protected_name(
        current_semantic_name="🚀 Task 💡 end #tag",
        leading_emoji_mutation=_emoji_mutation("✅", "append"),
    )
    assert r["name"] == "🚀 ✅ Task 💡 end #tag"


def test_insert_verbs_reject_blank_and_ordinary_text():
    for mode in ("prepend", "append"):
        with pytest.raises(ProtectedNameViolation) as exc:
            _compose_protected_name(
                current_semantic_name="Task",
                leading_emoji_mutation=_emoji_mutation("   ", mode),
            )
        assert exc.value.code == "invalid_emoji_value"
        assert "node.update_emoji" in str(exc.value)
        with pytest.raises(ProtectedNameViolation) as exc:
            _compose_protected_name(
                current_semantic_name="Task",
                leading_emoji_mutation=_emoji_mutation("✅ done", mode),
            )
        assert exc.value.code == "invalid_emoji_value"


def test_append_emoji_repeated_calls_stack_by_design():
    first = _compose_protected_name(
        current_semantic_name="🎫 Task",
        leading_emoji_mutation=_emoji_mutation("✅", "append"),
    )
    second = _compose_protected_name(
        current_semantic_name=first["name"],
        leading_emoji_mutation=_emoji_mutation("✅", "append"),
    )
    assert second["name"] == "🎫 ✅ ✅ Task"


# ---------------------------------------------------------------------------
# VS16-fallback emoji bases (the ‼️ class) -- shared scanner repair
# ---------------------------------------------------------------------------


def test_update_emoji_accepts_vs16_fallback_bases():
    r = _compose_protected_name(
        current_semantic_name="Task #tag",
        leading_emoji_mutation=_emoji_mutation("‼️ ‼️ ‼️ 🎫"),
    )
    assert r["name"] == "‼️ ‼️ ‼️ 🎫 Task #tag"
    assert r["emoji"] == "‼️ ‼️ ‼️ 🎫"
    for glyph in ("⁉️", "ℹ️", "↔️", "↕️", "↖️", "↗️", "↘️", "↙️", "⤴️", "⤵️", "〰️", "〽️"):
        r = _compose_protected_name(
            current_semantic_name="Task",
            leading_emoji_mutation=_emoji_mutation(glyph),
        )
        assert r["name"] == f"{glyph} Task"


def test_update_emoji_rejects_bare_fallback_base_without_vs16():
    for bad in ("‼", "‼︎", "‼️ done"):
        with pytest.raises(ProtectedNameViolation) as exc:
            _compose_protected_name(
                current_semantic_name="Task",
                leading_emoji_mutation=_emoji_mutation(bad),
            )
        assert exc.value.code == "invalid_emoji_value"


def test_ap_id_detection_strips_vs16_fallback_decoration():
    stripped = api_client_nexus._lstrip_leading_ws_and_emoji("‼️ [ap:ABC123] T")
    assert stripped.startswith("[ap:ABC123]")


def test_filebase_right_strip_drops_vs16_fallback_token():
    assert api_client_nexus._strip_trailing_tags_and_emoji("Example ‼️") == "Example"
    assert api_client_nexus._strip_trailing_tags_and_emoji("Example") == "Example"


def test_update_emoji_impl_uses_one_read_one_write_and_preserves_regions(monkeypatch):
    node = _fake_node(name="[ap:ABC123] 🚀 Task 💡 end #tag")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=node)
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server._node_update_emoji_impl("11111111-1111-4111-8111-111111111111", "✅"))
    assert client.get_node.await_count == 1
    assert client.update_node.await_count == 1
    request = client.update_node.call_args[0][1]
    assert request.name == "[ap:ABC123] ✅ Task 💡 end #tag"
    assert result["new_name"] == request.name
    assert result["emoji"] == "✅"
    assert result["no_op"] is False


def test_update_emoji_impl_same_run_is_zero_write_noop(monkeypatch):
    node = _fake_node(name="[ap:ABC123] ✅ Task #tag")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=node)
    client.update_node = AsyncMock()
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server._node_update_emoji_impl("11111111-1111-4111-8111-111111111111", "✅"))
    assert client.get_node.await_count == 1
    client.update_node.assert_not_called()
    assert result["no_op"] is True
    assert result["new_name"] == "[ap:ABC123] ✅ Task #tag"


def test_update_emoji_impl_blank_without_existing_run_is_exact_zero_write(monkeypatch):
    node = _fake_node(name="Task 🔱  #tag")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=node)
    client.update_node = AsyncMock()
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server._node_update_emoji_impl("11111111-1111-4111-8111-111111111111", ""))
    assert client.get_node.await_count == 1
    client.update_node.assert_not_called()
    assert result["no_op"] is True
    assert result["new_name"] == "Task 🔱  #tag"


def test_update_emoji_impl_invalid_value_is_zero_write(monkeypatch):
    node = _fake_node(name="Task #tag")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=node)
    client.update_node = AsyncMock()
    _patch_boundary(monkeypatch, client)

    with pytest.raises(ProtectedNameViolation) as exc:
        asyncio.run(server._node_update_emoji_impl("11111111-1111-4111-8111-111111111111", "✅ done"))
    assert exc.value.code == "invalid_emoji_value"
    client.update_node.assert_not_called()


def test_prepend_emoji_impl_one_read_one_write_and_reports_added(monkeypatch):
    node = _fake_node(name="🎫 Task #tag")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=node)
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server._node_prepend_emoji_impl("11111111-1111-4111-8111-111111111111", "✅"))
    assert client.get_node.await_count == 1
    assert client.update_node.await_count == 1
    request = client.update_node.call_args[0][1]
    assert request.name == "✅ 🎫 Task #tag"
    assert result["new_name"] == request.name
    assert result["emoji"] == "✅ 🎫"
    assert result["added_emoji"] == "✅"
    assert result["no_op"] is False


def test_append_emoji_impl_one_read_one_write_and_reports_added(monkeypatch):
    node = _fake_node(name="🎫 Task #tag")
    client = MagicMock()
    client.get_node = AsyncMock(return_value=node)
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    result = asyncio.run(server._node_append_emoji_impl("11111111-1111-4111-8111-111111111111", "✅"))
    assert client.get_node.await_count == 1
    assert client.update_node.await_count == 1
    request = client.update_node.call_args[0][1]
    assert request.name == "🎫 ✅ Task #tag"
    assert result["new_name"] == request.name
    assert result["emoji"] == "🎫 ✅"
    assert result["added_emoji"] == "✅"
    assert result["no_op"] is False


def test_insert_emoji_impl_blank_is_zero_write_loud_error(monkeypatch):
    for impl in (server._node_prepend_emoji_impl, server._node_append_emoji_impl):
        node = _fake_node(name="Task #tag")
        client = MagicMock()
        client.get_node = AsyncMock(return_value=node)
        client.update_node = AsyncMock()
        _patch_boundary(monkeypatch, client)

        with pytest.raises(ProtectedNameViolation) as exc:
            asyncio.run(impl("11111111-1111-4111-8111-111111111111", ""))
        assert exc.value.code == "invalid_emoji_value"
        client.update_node.assert_not_called()


# ---------------------------------------------------------------------------
# LEGACY update_anchor path (client.update_note_session)
# ---------------------------------------------------------------------------


def _legacy_client(monkeypatch, cache_name, live_name):
    client = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    client.export_nodes = AsyncMock(
        return_value={"nodes": [{"id": "N", "name": cache_name, "note": ""}]}
    )
    client.get_node = AsyncMock(return_value=WorkFlowyNode(id="N", name=live_name, note=""))
    client.update_node = AsyncMock(return_value=WorkFlowyNode(id="N"))
    monkeypatch.setattr(
        api_client_nexus, "_classify_node_map_state", lambda *a, **k: {"state": "unmapped"}
    )
    return client


def test_update_note_session_preserves_tags_and_ap_from_one_live_snapshot(monkeypatch):
    client = _legacy_client(
        monkeypatch,
        cache_name="Old topic #wfy-ns",
        live_name="[ap:ABC123] Old topic #wfy-ns",
    )
    result = asyncio.run(client.update_note_session("N", ["wfy-ns"], description="New topic"))
    request = client.update_node.call_args[0][1]
    assert request.name == "[ap:ABC123] New topic #wfy-ns"
    assert result["new_name"] == "[ap:ABC123] New topic #wfy-ns"


def test_update_note_session_rejects_tags_in_description_with_zero_write(monkeypatch):
    client = _legacy_client(
        monkeypatch, cache_name="Old topic #wfy-ns", live_name="Old topic #wfy-ns"
    )
    with pytest.raises(ProtectedNameViolation) as exc:
        asyncio.run(client.update_note_session("N", ["wfy-ns"], description="New topic #evil"))
    assert exc.value.code == "tags_in_name_rejected"
    client.update_node.assert_not_called()


def test_update_note_session_rejects_ap_change_in_description(monkeypatch):
    client = _legacy_client(
        monkeypatch,
        cache_name="Old topic #wfy-ns",
        live_name="[ap:ABC123] Old topic #wfy-ns",
    )
    with pytest.raises(ProtectedNameViolation) as exc:
        asyncio.run(client.update_note_session("N", ["wfy-ns"], description="[ap:XYZ999] New"))
    assert exc.value.code == "ap_id_mutation_rejected"
    client.update_node.assert_not_called()


def test_update_note_session_detail_only_skips_the_live_get(monkeypatch):
    client = _legacy_client(
        monkeypatch, cache_name="Old topic #wfy-ns", live_name="Old topic #wfy-ns"
    )
    asyncio.run(client.update_note_session("N", ["wfy-ns"], detail="replacement note"))
    client.get_node.assert_not_called()
    request = client.update_node.call_args[0][1]
    assert request.note == "replacement note"
    assert request.name is None


# ---------------------------------------------------------------------------
# Leading-emoji-run preservation on renames (Z8Q0PF + LITF2F, ruling
# C609U1 A2): a free-form rename preserves the current leading emoji run
# exactly like the AP-ID and trailing tags, UNLESS the supplied name brings
# its own run (explicit wins); replace_leading_emoji=True hands the run to
# the supplied name wholesale (its run wins; its absence clears the run).
# The byte-verbatim fast path must not bypass the preservation.
# ---------------------------------------------------------------------------


def test_rename_preserves_leading_emoji_after_ap():
    r = _compose_protected_name(
        current_semantic_name="[ap:K7M2QF] 📋 Old", requested_name="New"
    )
    assert r["name"] == "[ap:K7M2QF] 📋 New"


def test_rename_preserves_leading_emoji_on_plain_node():
    r = _compose_protected_name(
        current_semantic_name="📋 Old", requested_name="New"
    )
    assert r["name"] == "📋 New"


def test_preserved_run_rejoins_tags_and_ap():
    r = _compose_protected_name(
        current_semantic_name="[ap:K7M2QF] 📋 Old #foo #bar", requested_name="New"
    )
    assert r["name"] == "[ap:K7M2QF] 📋 New #foo #bar"


def test_supplied_leading_emoji_wins_after_ap():
    # Explicit wins: a rename that brings its own state mark replaces the run.
    r = _compose_protected_name(
        current_semantic_name="[ap:K7M2QF] 📋 Old", requested_name="✅ New"
    )
    assert r["name"] == "[ap:K7M2QF] ✅ New"


def test_supplied_run_wins_over_a_different_current_run():
    r = _compose_protected_name(
        current_semantic_name="🚀 Old", requested_name="✅ New"
    )
    assert r["name"] == "✅ New"


def test_node_without_emoji_rename_unchanged():
    r = _compose_protected_name(
        current_semantic_name="[ap:K7M2QF] Old #t", requested_name="New"
    )
    assert r["name"] == "[ap:K7M2QF] New #t"


def test_byte_verbatim_fast_path_survives_without_emoji():
    r = _compose_protected_name(
        current_semantic_name="Old title", requested_name="  New  title  "
    )
    assert r["name"] == "  New  title  "


def test_fast_path_does_not_bypass_emoji_preservation():
    # The fast path writes the caller's exact bytes when nothing is
    # protected; a preserved emoji run IS protection, so the composition
    # must run even on an untagged, AP-less node.
    r = _compose_protected_name(
        current_semantic_name="📋 Old title", requested_name="New title"
    )
    assert r["name"] == "📋 New title"


def test_multi_glyph_run_preserved_whole():
    r = _compose_protected_name(
        current_semantic_name="👩‍💻 1️⃣ Task", requested_name="Renamed"
    )
    assert r["name"] == "👩‍💻 1️⃣ Renamed"


def test_rename_to_empty_title_keeps_the_run():
    r = _compose_protected_name(
        current_semantic_name="[ap:K7M2QF] 📋 Old #t", requested_name=""
    )
    assert r["name"] == "[ap:K7M2QF] 📋 #t"


def test_replace_leading_emoji_true_clears_run():
    r = _compose_protected_name(
        current_semantic_name="📋 Old", requested_name="New",
        replace_leading_emoji=True,
    )
    assert r["name"] == "New"


def test_replace_leading_emoji_true_supplied_run_wins():
    r = _compose_protected_name(
        current_semantic_name="📋 Old", requested_name="✅ New",
        replace_leading_emoji=True,
    )
    assert r["name"] == "✅ New"


def test_replace_leading_emoji_false_supplied_run_still_wins():
    # The flag's ONLY extra power over the default is CLEARING; a supplied
    # run wins under both settings.
    r = _compose_protected_name(
        current_semantic_name="📋 Old", requested_name="✅ New",
        replace_leading_emoji=False,
    )
    assert r["name"] == "✅ New"


def test_rename_flag_is_inert_without_requested_name():
    # tag/AP channels and the emoji verbs never take the flag; without a
    # requested_name it changes nothing.
    r = _compose_protected_name(
        current_semantic_name="📋 Old #t", tag_transform=lambda tags: tags,
        replace_leading_emoji=True,
    )
    assert r["name"] == "📋 Old #t"


# ASYNC CORE -- the flag rides the public edit surface end to end.


def test_edit_name_preserves_existing_leading_emoji(monkeypatch):
    client = MagicMock()
    client.get_node = AsyncMock(
        return_value=_fake_node(name="[ap:K7M2QF] 📋 Old #foo")
    )
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    asyncio.run(server.edit_node("11111111-1111-4111-8111-111111111111", name="New"))
    request = client.update_node.call_args[0][1]
    assert request.name == "[ap:K7M2QF] 📋 New #foo"
    client.get_node.assert_awaited_once()


def test_edit_impl_accepts_replace_emoji_and_clears_run(monkeypatch):
    client = MagicMock()
    client.get_node = AsyncMock(return_value=_fake_node(name="📋 Old"))
    client.update_node = AsyncMock(return_value=_fake_node())
    _patch_boundary(monkeypatch, client)

    asyncio.run(server._node_edit_impl("11111111-1111-4111-8111-111111111111", name="New", replace_emoji=True))
    request = client.update_node.call_args[0][1]
    assert request.name == "New"


def test_node_edit_schema_declares_replace_emoji_default_false():
    spec = server.build_node_registry().resolve("edit")
    prop = spec.params_schema["properties"]["replace_emoji"]
    assert prop["type"] == "boolean"
    assert prop["default"] is False
    assert "leading emoji run" in spec.summary
