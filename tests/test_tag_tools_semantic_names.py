"""Regression lock: tag tools decode the raw wire-form NAME exactly ONE layer.

THE BUG (confirmed visually in the Workflowy GUI, 2026): the three tag tools
(``workflowy_add_tag`` / ``workflowy_remove_tag`` / ``workflowy_rename_tag``)
read the node's NAME via a live ``GET /nodes/{id}`` -- the RAW wire form,
one HTML-entity layer deep (e.g. ``&amp;`` for semantic ``&``) -- treated it
as final semantic text, and fed it back through ``update_node``'s encoder,
which single-encodes ``&`` -> ``&amp;``. The name was stored double-encoded
(``&amp;amp;``), so the GUI decoded once and showed the literal text
``&amp;`` instead of ``&``.

THE FIX: ``_semantic_name_for_tag_merge`` decodes EXACTLY ONE, NON-CASCADING
entity layer before the merge (``&amp;lt;`` -> ``&lt;``, NEVER ``<``), and
the post-update ``new_name`` in the response is normalized into the same
semantic domain. The existing ``_dewhiten_text`` is deliberately NOT reused:
its chained ``str.replace`` calls cascade (``&amp;lt;`` -> ``&lt;`` -> ``<``).

SCOPE (audited): the tag tools are the ONLY name-writers that read a raw
wire-form name and rewrite it. Every other name-writer sources semantic text
(caller-supplied, the dewhitened /nodes-export cache, disk content, or a
synthesized constant) -- that is why the helper is tag-merge-scoped rather
than a general read-side decode, and why ``get_node`` itself must KEEP
returning the raw wire form (the cache patcher stores it verbatim).

All local: no network, no real Workflowy. The client is built with
``__new__`` (no live connection); ``get_node`` / ``update_node`` are stubbed
with a stateful fake wire-form store that runs the REAL
``encode_for_workflowy_api`` on the write side, so these tests exercise the
full semantic -> encode -> store -> read-back loop.
"""

import asyncio
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402
from workflowy_mcp.client.api_client_core import (  # noqa: E402
    WorkFlowyClientCore,
    _semantic_name_for_tag_merge,
)
from workflowy_mcp.models import WorkFlowyNode  # noqa: E402
from coherence_harness import make_boundary_capable
from n05_region_harness import isolate_claim_scope
from workflowy_mcp.coherence.operation_cache import cache_operation_boundary

NODE = "11111111-1111-4111-8111-111111111111"


class _WireStore:
    """Stateful fake of Workflowy's NAME pipeline (wire-form storage).

    ``get_node`` hands back the current wire form verbatim (exactly like the
    real raw GET). ``update_node`` captures the SEMANTIC request name, runs
    the REAL update encoder to compute what Workflowy would store, and
    returns the stored wire form (exactly like the real post-update GET).
    """

    def __init__(self, wire_name: str):
        self.wire_name = wire_name
        self.update_calls: list[str] = []

    def install(self, client) -> None:
        async def _get_node(node_id, max_retries=10):
            return WorkFlowyNode(id=node_id, name=self.wire_name)

        async def _update_node(node_id, request, max_retries=10, *, basis=None):
            assert basis == {"name": self.wire_name}
            assert request.name is not None, "tag tools must always send a name"
            self.update_calls.append(request.name)
            self.wire_name = WorkFlowyClientCore.encode_for_workflowy_api(
                request.name, "name", operation="update"
            )
            return WorkFlowyNode(id=node_id, name=self.wire_name)

        client.get_node = _get_node  # type: ignore[assignment]
        client.update_node = _update_node  # type: ignore[assignment]


def _make_client(store: _WireStore, tmp_path) -> WorkFlowyClientNexus:
    c = WorkFlowyClientNexus.__new__(WorkFlowyClientNexus)
    store.install(c)
    make_boundary_capable(c, nodes=[{"id": NODE, "parent_id": None}])
    isolate_claim_scope(c, tmp_path)
    for name in ("workflowy_add_tag", "workflowy_remove_tag", "workflowy_rename_tag"):
        primitive = getattr(c, name)

        async def within_boundary(*args, __primitive=primitive, **kwargs):
            async with cache_operation_boundary(c, "semantic-tag-fixture"):
                return await __primitive(*args, **kwargs)

        setattr(c, name, within_boundary)
    return c


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helper unit tests (non-cascading, exactly-one-layer contract)
# ---------------------------------------------------------------------------


def test_helper_decodes_exactly_one_layer_never_cascading():
    assert _semantic_name_for_tag_merge("&amp;") == "&"
    assert _semantic_name_for_tag_merge("&lt;") == "<"
    assert _semantic_name_for_tag_merge("&gt;") == ">"
    # The anti-cascade lock: a user-visible literal '&lt;' (stored as
    # '&amp;lt;') decodes to '&lt;' -- NEVER to '<'.
    assert _semantic_name_for_tag_merge("&amp;lt;") == "&lt;"
    # Exactly one layer on a double-encoded form: NO silent repair of
    # already-corrupted names (a literal '&amp;' may be intentional).
    assert _semantic_name_for_tag_merge("&amp;amp;") == "&amp;"


def test_helper_leaves_non_alphabet_sequences_untouched():
    # A bare '&' (not part of the three-entity alphabet) passes through.
    assert _semantic_name_for_tag_merge("Tom & Jerry") == "Tom & Jerry"
    # Entities the NAME encoder never produces are left literal.
    assert _semantic_name_for_tag_merge("&nbsp;") == "&nbsp;"
    assert _semantic_name_for_tag_merge("&#39;") == "&#39;"
    # Empty / whitespace input.
    assert _semantic_name_for_tag_merge("") == ""


# ---------------------------------------------------------------------------
# add_tag
# ---------------------------------------------------------------------------


def test_add_tag_decodes_ampersand_once_no_double_encode(tmp_path):
    store = _WireStore("A &amp; B #old")
    c = _make_client(store, tmp_path)

    result = _run(c.workflowy_add_tag(NODE, "#new"))

    # The merge operated on SEMANTIC text...
    assert store.update_calls == ["A & B #old #new"]
    # ...so the stored wire form holds EXACTLY one entity layer. The bug
    # would have produced 'A &amp;amp; B #old #new' here.
    assert store.wire_name == "A &amp; B #old #new"
    # The response is semantic-domain coherent on both sides.
    assert result["old_name"] == "A & B #old"
    assert result["new_name"] == "A & B #old #new"
    assert result["tags_added"] == ["#new"]
    assert result["no_op"] is False


def test_add_tag_twice_never_compounds_layers(tmp_path):
    store = _WireStore("A &amp; B #old")
    c = _make_client(store, tmp_path)

    _run(c.workflowy_add_tag(NODE, "#two"))
    assert store.wire_name == "A &amp; B #old #two"

    _run(c.workflowy_add_tag(NODE, "#three"))
    assert store.wire_name == "A &amp; B #old #two #three"


def test_add_tag_literal_entity_text_stays_literal(tmp_path):
    store = _WireStore("Literal &amp;lt; token #tag")
    c = _make_client(store, tmp_path)

    result = _run(c.workflowy_add_tag(NODE, "#tag2"))

    # Semantic form keeps the literal '&lt;' (cascade would have made '<').
    assert store.update_calls == ["Literal &lt; token #tag #tag2"]
    # Wire form re-encodes the '&' of '&lt;' -> one layer, byte-stable.
    assert store.wire_name == "Literal &amp;lt; token #tag #tag2"
    assert result["new_name"] == "Literal &lt; token #tag #tag2"


def test_add_tag_angle_bracket_roundtrip(tmp_path):
    store = _WireStore("3 &lt; 5 #tag")
    c = _make_client(store, tmp_path)

    _run(c.workflowy_add_tag(NODE, "#x"))

    assert store.update_calls == ["3 < 5 #tag #x"]
    assert store.wire_name == "3 &lt; 5 #tag #x"


def test_add_tag_whitelisted_markup_preserved_inner_decoded_once(tmp_path):
    store = _WireStore("<b>A &amp; B</b> #tag")
    c = _make_client(store, tmp_path)

    _run(c.workflowy_add_tag(NODE, "#x"))

    # Outer formatting tags survive byte-for-byte; inner '&' decodes once.
    assert store.update_calls == ["<b>A & B</b> #tag #x"]
    assert store.wire_name == "<b>A &amp; B</b> #tag #x"


def test_add_tag_pill_markup_normalized_to_bare_tag(tmp_path):
    # Workflowy may expose a rendered tag as '#<u>old</u>'; the merge
    # canonicalizes it to the bare '#old' form (Workflowy re-pills on
    # display). Locked here as INTENTIONAL, not a defect.
    store = _WireStore("A #<u>old</u>")
    c = _make_client(store, tmp_path)

    _run(c.workflowy_add_tag(NODE, "#new"))

    assert store.update_calls == ["A #old #new"]


def test_add_tag_noop_when_present_no_write_semantic_response(tmp_path):
    store = _WireStore("A &amp; B #x")
    c = _make_client(store, tmp_path)

    result = _run(c.workflowy_add_tag(NODE, "#x"))

    assert store.update_calls == []  # no API write at all
    assert result["no_op"] is True
    assert result["old_name"] == "A & B #x"
    assert result["new_name"] == "A & B #x"
    assert result["tags_already_present"] == ["#x"]


def test_add_tag_entity_bearing_tag_detected_as_present(tmp_path):
    # The decode applies to the trailing TAG region too, so idempotency
    # detection sees the semantic form of an entity-bearing tag.
    store = _WireStore("T #a&amp;b")
    c = _make_client(store, tmp_path)

    result = _run(c.workflowy_add_tag(NODE, "#a&b"))

    assert store.update_calls == []
    assert result["no_op"] is True
    assert result["tags_already_present"] == ["#a&b"]


# ---------------------------------------------------------------------------
# remove_tag
# ---------------------------------------------------------------------------


def test_remove_tag_entity_roundtrip(tmp_path):
    store = _WireStore("A &amp; B #x #y")
    c = _make_client(store, tmp_path)

    result = _run(c.workflowy_remove_tag(NODE, "#y"))

    assert store.update_calls == ["A & B #x"]
    assert store.wire_name == "A &amp; B #x"
    assert result["old_name"] == "A & B #x #y"
    assert result["new_name"] == "A & B #x"
    assert result["tags_removed"] == ["#y"]
    assert result["no_op"] is False


def test_remove_tag_absent_tag_no_write(tmp_path):
    store = _WireStore("A &amp; B #x")
    c = _make_client(store, tmp_path)

    result = _run(c.workflowy_remove_tag(NODE, "#zzz"))

    assert store.update_calls == []
    assert result["no_op"] is True
    assert result["tags_not_present"] == ["#zzz"]
    assert result["new_name"] == "A & B #x"


# ---------------------------------------------------------------------------
# rename_tag
# ---------------------------------------------------------------------------


def test_rename_tag_entity_roundtrip(tmp_path):
    store = _WireStore("A &amp; B #x")
    c = _make_client(store, tmp_path)

    result = _run(c.workflowy_rename_tag(NODE, "#x", "#z"))

    assert store.update_calls == ["A & B #z"]
    assert store.wire_name == "A &amp; B #z"
    assert result["old_name"] == "A & B #x"
    assert result["new_name"] == "A & B #z"
    assert result["renamed"] is True
    assert result["no_op"] is False


def test_rename_tag_then_add_never_compounds(tmp_path):
    store = _WireStore("A &amp; B #x")
    c = _make_client(store, tmp_path)

    _run(c.workflowy_rename_tag(NODE, "#x", "#z"))
    assert store.wire_name == "A &amp; B #z"

    _run(c.workflowy_add_tag(NODE, "#more"))
    assert store.wire_name == "A &amp; B #z #more"
