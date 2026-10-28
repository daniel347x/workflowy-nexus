"""Pure estate simple-form interpretation. No acquisition or rendering here."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

SIMPLE_FORM_ERROR_CODES = (
    "simple_form_double_match", "simple_form_descend_refused",
    "simple_form_nesting_refused", "simple_form_unresolved",
    "simple_form_wrong_surface", "simple_form_not_a_parcel",
    "simple_form_settings_in_parcel",
)
# Handle-shaped bare strings (CF-06): [ap:CORE] AP-IDs, or a UUID-hex run of
# >= 8 chars (hex at both ends), optionally dashed and optionally bracketed —
# exactly the shapes the ONE shared resolver accepts downstream
# (resolve_uuid_or_anchor_point_handle: prefix kernel floor 8 + the
# unwrap_bracketed_uuid_handle bracket grammar). A 9-35-char prefix, a dashed
# partial or a 32-hex undashed UUID used to fall through to named_group and
# fail as an unknown group. A group whose name looks like hex is arbitrated
# by resolve_bare_string_decision (the double-match rule), never guessed.
_HANDLE = re.compile(
    r"(?:\[ap:[A-Za-z0-9]+\]"
    r"|\[[0-9a-fA-F]{8}(?:[0-9a-fA-F-]*[0-9a-fA-F])?\]"
    r"|[0-9a-fA-F]{8}(?:[0-9a-fA-F-]*[0-9a-fA-F])?"
    r")\Z",
    re.I,
)
_FORMS = {"group", "dock", "tags", "symbols", "node", "subtree"}
_BLOB_KEYS = {"kind", "group_id", "root_uuid", "lightning", "selection"}
# Reader SETTINGS (Dispatch Schema / StageConfiguration): legal only BESIDE a
# parcel (on the dispatch line or as call arguments), never inside one. A
# parcel carrying one is a loud teaching error (QBCTWU addendum item 3,
# strict-JSON ruling: settings are split from the parcel by KEY NAME).
_SETTINGS_KEYS = {"roles", "read", "load", "note", "view", "entries"}
# The long qualified parcel's kind vocabulary (QBCTWU): an alias of the short
# forms. file/uri are NOT parcels (attachment-only) and get their own teaching
# error before the unknown-kind refusal.
_LONG_FORM_KINDS = {"slice_group", "dock", "node"}
# The four RenderProjectionSpec scope kinds (cache_render_spec.SPEC_KINDS).
# ONE recogniser home (CF-10): glimpse_estate_payload imports these two names
# from here (the payload module already imports this one, so no import cycle),
# and this module's own formal-blob route uses the helper below — the blob
# recognition can never drift between the estate payload parser and the
# grammar again. The vocabulary is the frozen wire contract of the protected
# spec system.
_SPEC_KINDS = ("raw_root", "slice", "slice_override", "lightning_rod")


# @beacon[
#   id=simple-form@blob-recogniser,
#   slice_labels=saved-tabs,
#   kind=ast,
#   comment=THE one bare render_spec-blob recogniser (CF-10): an explicit spec kind or any scope field (group_id/root_uuid/lightning/selection). glimpse_estate_payload imports it from here so the estate payload parser and the grammar can never drift.,
# ]
def _looks_like_spec_blob(data: Any) -> bool:
    """Recognize a BARE render_spec blob by an explicit kind or scope field.

    Ported from the widget's ``parseSavedTabsImport`` recognition set: an
    explicit spec kind, or any of the scope fields a spec blob carries.
    """
    if not isinstance(data, dict):
        return False
    kind_str = str(data.get("kind") or "").strip().lower()
    if kind_str in _SPEC_KINDS:
        return True
    return (
        data.get("group_id") is not None
        or data.get("root_uuid") is not None
        or data.get("lightning") is not None
        or data.get("selection") is not None
    )


def _looks_like_path_or_uri(text: str) -> bool:
    """True for a bare string that reads as a file path or URI, not a parcel.

    A URI (``scheme://…``), an absolute POSIX path (``/…``, ``~/…``) or any
    backslash path (``E:\\…``). Used on the ESTATE surfaces only: such a
    string there is almost certainly an attachment-style ref passed to the
    wrong surface, so the error teaches the direct read instead of failing
    as an unknown group (QBCTWU addendum item 2).
    """
    if "://" in text:
        return True
    if text.startswith(("/", "~")):
        return True
    return "\\" in text


@dataclass(frozen=True)
class SimpleFormResult:
    ok: bool
    form: str
    text: str = ""
    alias: str = ""
    spec: dict | None = None
    lens: dict | None = None
    # Resolution operands, not a second grammar. Caller replaces these via
    # its ordinary UUID grace/resolver before using the lowered request.
    root: str | None = None
    requires_dock: bool = False
    raw: Any = None
    error_code: str = ""
    error_message: str = ""
    is_simple: bool = False
    # Archive Reads Chain-1 (W4/F5): when the failure is the archive
    # read-only refusal (error_code == "archived_node_read_only"), the
    # matched record's provenance dict rides here so the consumer can raise
    # ArchivedNodeReadOnlyError with error.extra = provenance.  None for
    # every ordinary grammar/resolution failure.
    archive: dict | None = None


@dataclass(frozen=True)
class BareStringDecision:
    role: str
    hit: Any = None
    error_code: str = ""
    error_message: str = ""


@dataclass(frozen=True)
class NodeRoleDecision:
    role: str
    error_code: str = ""
    error_message: str = ""


def _error(message: str, code: str = "simple_form_unresolved") -> SimpleFormResult:
    return SimpleFormResult(False, "error", error_code=code,
                            error_message=f"{message}; see describe for the accepted simple forms or full render_spec.")


def _lower_long_form(data: dict, surface: str) -> SimpleFormResult:
    """Lower the long qualified parcel ``{ref, kind, how?, descend?}`` (QBCTWU).

    The long form is an ALIAS of the short forms, so it delegates: kind
    ``slice_group``/``dock``/``node`` select the ``{group}``/``{dock}``/
    ``{node}``/``{subtree}`` lowering (``how`` — default ``note`` — picks the
    node parcel's read shape). ``descend`` is Dock-only. ``kind: file`` /
    ``kind: uri`` are NOT estate parcels (they are attachment-only long
    forms): a loud teaching error pointing at a direct read. Settings keys
    refuse exactly as on the short forms, before anything else.
    """
    _settings = _SETTINGS_KEYS.intersection(data)
    if _settings:
        return _error(
            f"settings keys {sorted(_settings)} belong BESIDE the parcel (on the "
            "dispatch/attachment line or as call arguments), never inside it; "
            "pass the bare parcel",
            "simple_form_settings_in_parcel",
        )
    kind = str(data.get("kind") or "").strip().lower()
    if kind in {"file", "uri"}:
        return _error(
            f"kind '{kind}' is not an estate parcel: read a file with your file "
            "tools or fetch a URI directly (kind: file / kind: uri are "
            "attachment-only forms, never a glimpse estate / render_spec parcel)",
            "simple_form_not_a_parcel",
        )
    extra = set(data) - {"ref", "kind", "how", "descend", "alias"}
    if extra:
        return _error(f"unknown keys on the qualified form: {sorted(map(str, extra))}")
    if kind not in _LONG_FORM_KINDS:
        return _error(
            f"unknown kind {data.get('kind')!r} on the qualified form; "
            "expected slice_group, dock or node"
        )
    ref = data.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        return _error("the qualified form requires a non-empty string 'ref' handle")
    if "how" in data and kind != "node":
        return _error("'how' is legal only with kind 'node' (note | subtree)")
    if "descend" in data and kind != "dock":
        return _error(
            "'descend' is legal only with kind 'dock' on the qualified form",
            "simple_form_descend_refused",
        )
    how = str(data.get("how") or "note").strip().lower()
    if how not in {"note", "subtree"}:
        return _error(f"unknown how {data.get('how')!r}; expected 'note' or 'subtree'")
    ref = ref.strip()
    if kind == "slice_group":
        short: dict = {"group": ref}
    elif kind == "dock":
        short = {"dock": ref}
        if "descend" in data:
            short["descend"] = data["descend"]
    else:
        short = {"node": ref} if how == "note" else {"subtree": ref}
    if isinstance(data.get("alias"), str):
        short["alias"] = data["alias"]
    return normalize_simple_form(short, surface=surface)


# @beacon[
#   id=simple-form@normalize,
#   slice_labels=nexus--mcp-api,
#   kind=ast,
#   comment=ONE pure simple-form grammar for estate notes/calls, glimpse render_spec and render-only lens ingress. Parcels: group/dock/tags/symbols/node/subtree simple forms, the {ref,kind} qualified alias, and the estate-only {lens} sugar; the {dock} parcel also takes recursive (ap:EPR86W part a: the depth leg, spec-consuming surfaces only -- the lens subtree atom has no recursion knob) beside descend; file/uri, path/URI strings and in-parcel settings keys refuse with teaching errors. Lowers to existing requests; no cache, transport, Dock classification or rendering.,
# ]
def normalize_simple_form(raw: Any, *, surface: str) -> SimpleFormResult:
    """Interpret one entry, retaining formal requests verbatim and owning copies."""
    if surface not in {"estate_note", "estate_call", "glimpse_run", "slice_group"}:
        raise ValueError(f"unknown simple-form surface: {surface}")
    # Notes already supply JSON-decoded values. Other ingress surfaces also
    # accept JSON strings; never mistake malformed JSON for a group name.
    if isinstance(raw, str) and surface != "estate_note":
        encoded = raw.strip()
        if "{" in encoded and not encoded.startswith(('{', '[', '"')):
            return _error("unquoted text with a JSON brace is not a group-name simple form")
        if encoded and encoded[0] in '{["' and not _HANDLE.fullmatch(encoded):
            try:
                raw = json.loads(encoded)
            except ValueError as exc:
                return _error(f"invalid JSON: {exc}")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return _error("empty string: expected a group name, Dock or estate handle")
        if surface in {"estate_call", "estate_note"} and _looks_like_path_or_uri(text):
            # QBCTWU addendum item 2: an attachment-style ref passed to the
            # estate surface teaches the direct read instead of failing as
            # an unknown group, so the worker falls back cleanly.
            return _error(
                f"{text!r} is not an estate parcel: it looks like a file path or "
                "URI — read it with your file tools or fetch it directly "
                "(attachments accept kind: file / kind: uri; the estate verb renders parcels only)",
                "simple_form_not_a_parcel",
            )
        if _HANDLE.fullmatch(text):
            return SimpleFormResult(True, "bare_handle", text=text, alias=text, raw=text)
        return SimpleFormResult(True, "named_group", text=text, raw=raw,
                                lens={"kind": "group", "group_handle": text, "match_scope": "name"})
    if isinstance(raw, list):
        return SimpleFormResult(True, "formal_array", raw=deepcopy(raw))
    if not isinstance(raw, dict):
        return _error(f"entry is {type(raw).__name__}; expected a group name, simple-form object or render_spec")
    data = deepcopy(raw)
    kind = str(data.get("kind") or "").strip().lower()
    # The long qualified parcel {ref, kind, how?, descend?} (QBCTWU) and the
    # attachment-only kinds: handled BEFORE the LensSpec route so {ref,
    # kind: node} can never be misread as a formal LensSpec on the
    # slice_group surface, and before the blob/form-key checks.
    if "ref" in data or kind in {"file", "uri"}:
        return _lower_long_form(data, surface)
    # GPLENH estate sugar: a bare {"lens": L} (optionally alias/title)
    # lowers to EXACTLY {"lightning": {"mode": "render_lens", "lens": L}} —
    # the formal wrapper without learning where it goes. Estate surfaces
    # only: the other surfaces own their lens spellings unchanged.
    if "lens" in data and surface in {"estate_call", "estate_note"}:
        lens_extra = set(data) - {"lens", "alias", "title"}
        if lens_extra:
            return _error(f"unknown keys on the lens shorthand: {sorted(map(str, lens_extra))}")
        lens_value = data.get("lens")
        if not isinstance(lens_value, dict):
            return _error(
                "the lens shorthand takes a LensSpec object ({'lens': {...}}); "
                "see slice_group.render describe for the recursive LensSpec grammar"
            )
        alias = data["alias"] if isinstance(data.get("alias"), str) else ""
        return SimpleFormResult(
            True, "formal_spec", alias=alias, raw=data,
            spec={"lightning": {"mode": "render_lens", "lens": deepcopy(lens_value)}},
        )
    # A formal LensSpec has its own closed-key parser. Its 'tags' key and
    # subtree 'descend' are NOT shorthand keys. Keep that grammar separate.
    if surface == "slice_group" and "kind" in data and not {"dock", "symbols", "group"}.intersection(data):
        return SimpleFormResult(True, "formal_spec", lens=data, raw=data)
    def invalid(message, code="simple_form_unresolved"):
        from dataclasses import replace
        return replace(_error(message, code), is_simple=bool(_FORMS.intersection(data)))

    if "descend" in data and "dock" not in data:
        return invalid("descend is legal only on the Dock form, not a group/dock/node/subtree/tags/symbols or formal spec", "simple_form_descend_refused")
    form_keys = _FORMS.intersection(data)
    if form_keys and _BLOB_KEYS.intersection(data):
        return invalid("both simple-form and render_spec keys supplied; choose one form or a full spec")
    # Preserve the established spec-first Saved-Tabs wrapper contract.
    if isinstance(data.get("spec"), dict):
        return SimpleFormResult(True, "formal_spec", spec=data["spec"], raw=data)
    if kind in {"glimpse-slice-groups-export", "glimpse_anchor_views_export"} or isinstance(data.get("expand_groups"), list) or isinstance(data.get("anchor_views"), list):
        return SimpleFormResult(True, "formal_spec", raw=data)
    if len(form_keys) > 1:
        return invalid("multiple simple-form keys supplied; choose one of group, dock, node, subtree, tags or symbols")
    if not form_keys:
        if _looks_like_spec_blob(data):  # CF-10: the ONE blob recogniser.
            return SimpleFormResult(True, "formal_spec", spec=data, raw=data)
        return _error('no "spec" object or simple form; expected a JSON array, group name or render_spec blob')
    form = next(iter(form_keys))
    _settings = _SETTINGS_KEYS.intersection(data)
    if _settings:
        # QBCTWU addendum item 3 (strict-JSON ruling): settings are split
        # from the parcel by key name; inside one they are a loud teaching
        # error, never stripped silently.
        return invalid(
            f"settings keys {sorted(_settings)} belong BESIDE the parcel (on the "
            "dispatch/attachment line or as call arguments), never inside it; "
            "pass the bare parcel",
            "simple_form_settings_in_parcel",
        )
    if form == "group":
        allowed = {"group", "alias", "title", "url"}
    elif form in {"node", "subtree"}:
        allowed = {form, "alias"}
    else:
        allowed = {form, "alias", "descend" if form == "dock" else "root"}
        # [ap:EPR86W] part a: the depth leg of the traversal pair rides the
        # dock parcel on the spec-consuming surfaces only. The lens subtree
        # atom has no recursion knob (a membership/slice render is unbounded
        # by construction), so the slice_group surface refuses the key via
        # the usual unknown-key error rather than silently dropping it.
        if form == "dock" and surface in {"estate_call", "estate_note", "glimpse_run"}:
            allowed.add("recursive")
    if set(data) - allowed:
        return invalid(f"unknown keys on {form} form: {sorted(map(str, set(data) - allowed))}")
    value = data[form]
    if form == "group":
        if not isinstance(value, str) or not value.strip():
            return invalid("group must be a non-empty name or group id")
        return SimpleFormResult(True, "passthrough_group", text=value.strip(), raw=data,
                                lens={"kind": "group", "group_handle": value.strip(), "match_scope": "name"})
    if form in {"node", "subtree"}:
        if form == "node" and surface == "slice_group":
            return invalid("node is not a LensSpec atom; use glimpse.estate or lightning_rod.render_symbols to read one node", "simple_form_wrong_surface")
        if not isinstance(value, str) or not value.strip():
            return invalid(f"{form} must be a non-empty node handle")
        root = text = value.strip()
        if form == "node":
            # QBCTWU D3: the node ALONE through the existing render_symbols
            # mechanism (the resolved handle replaces BOTH symbols and
            # root_uuids at resolution; one node renders alone with its
            # note, no descendants). No lens atom.
            spec = {"lightning": {"mode": "render_symbols", "symbols": [root], "root_uuids": [root]}}
            lens = None
        else:
            # Every descendant, headings-only by default on the estate
            # surfaces (the per-kind default read below — this unit, 3d).
            spec = {"root_uuid": root, "descend_policy": "all"}
            lens = {"kind": "subtree", "root_handle": root, "descend": "all"}
    elif form == "dock":
        if not isinstance(value, str) or not value.strip():
            return invalid("dock must be a non-empty node handle")
        if "descend" in data and not isinstance(data["descend"], bool):
            return invalid("Dock descend must be a JSON boolean (true or false)")
        if "recursive" in data and not isinstance(data["recursive"], bool):
            return invalid("Dock recursive must be a JSON boolean (true or false)")
        root = text = value.strip()
        policy = "all" if data.get("descend", False) else "stop_at_child_anchors"
        spec = {"root_uuid": root, "descend_policy": policy}
        # [ap:EPR86W] part a: the depth leg of the traversal pair, named and
        # valued exactly like dock.peek's. Emitted ONLY when explicitly False
        # (the deserializer treats an absent key as True -- the default), so
        # today's parcels lower to byte-identical specs. The lens atom has no
        # recursion knob; the key is only accepted on the spec-consuming
        # surfaces (see the allowed-keys note above), where the lens is
        # unused.
        if data.get("recursive", True) is False:
            spec["recursive"] = False
        # The public token is canonical; parse_lens_spec owns conversion to
        # the historical engine token. No engine comparison changes.
        lens = {"kind": "subtree", "root_handle": root, "descend": policy}
    else:
        if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
            return invalid(f"{form} must be a non-empty list of strings")
        root = data.get("root")
        if not isinstance(root, str) or not root.strip():
            return invalid(f"{form} requires a non-empty root handle")
        root = root.strip()
        value = [v.strip() for v in value]
        text = ",".join(value)
        if form == "tags":
            spec = {"root_uuid": root, "match_scope": "name", "lightning": {"mode": "render_tags", "tags": value, "postfix_policy": "slice", "match_scope": "name"}}
            lens = {"kind": "tags", "root_handle": root, "tags": value, "match_scope": "name"}
        else:
            if surface == "slice_group":
                return invalid("symbols are not a LensSpec atom; use lightning_rod.render_symbols", "simple_form_wrong_surface")
            spec = {"lightning": {"mode": "render_symbols", "symbols": value, "root_uuids": [root]}}
            lens = None
    # QBCTWU U3-2 · per-kind default reads (the skill §8 table's Default-read
    # column; Dan: "headings-only first where it makes sense — estate verb
    # ONLY, not glimpse.run"): on the estate surfaces a {tags}/{dock}/
    # {subtree} parcel defaults to a headings-only render, baked into the
    # lowered spec so the call-level headings_only override still wins
    # (the tri-state with_view_overrides). Deliberately NOT baked: a NAMED
    # slice group renders as the group defines (Dan's ruling — its stored
    # defaults are the read), and {node}/{symbols} whose bodies ARE the read
    # (the node alone; a beacon slice of signatures + comments).
    if surface in {"estate_call", "estate_note"} and form in {"dock", "tags", "subtree"}:
        spec["headings_only"] = True
    alias = data["alias"] if isinstance(data.get("alias"), str) else text
    return SimpleFormResult(True, form, text=text, alias=alias, spec=spec, lens=lens,
                            root=root, requires_dock=form == "dock", raw=data)


# @beacon[
#   id=simple-form@bare-decision,
#   slice_labels=nexus--mcp-api,
#   kind=ast,
#   comment=Pure node/group hit decision over caller-acquired candidates; refuses any ambiguous hit or cross-domain collision instead of guessing.,
# ]
def resolve_bare_string_decision(text, *, node_hits, group_hits) -> BareStringDecision:
    """Choose a unique identity only after BOTH resolver results are known."""
    if len(node_hits) > 1 or len(group_hits) > 1 or (node_hits and group_hits):
        return BareStringDecision("error", error_code="simple_form_double_match", error_message=f"{text!r} matches nodes {node_hits!r} and groups {group_hits!r}; choose an explicit group or Dock form; see describe")
    if node_hits:
        return BareStringDecision("node", node_hits[0])
    if group_hits:
        return BareStringDecision("group", group_hits[0])
    return BareStringDecision("error", error_code="simple_form_unresolved", error_message=f"{text!r} did not resolve to a node or slice group; check the handle or system.cache.refresh; see describe")


# @beacon[
#   id=simple-form@node-role,
#   slice_labels=nexus--mcp-api,
#   kind=ast,
#   comment=Pure Dock-first node-role decision: dock > estate > plain node at the top level (QBCTWU U3-2 — a bare handle that is neither a Dock nor an estate reads the node on its own); a non-Dock inside an estate note is never another estate. Acquisition and note parsing remain with the originator.,
# ]
def decide_node_role(*, is_anchor, note_parses_as_estate, inside_note) -> NodeRoleDecision:
    """Dock identity wins even when its description happens to parse."""
    if is_anchor:
        return NodeRoleDecision("dock")
    if inside_note:
        return NodeRoleDecision("error", "simple_form_nesting_refused", "estates do not nest: a bare handle inside a note must be an Anchor Dock, not an estate/plain node; see describe")
    if note_parses_as_estate is None:
        return NodeRoleDecision("read_note")
    if note_parses_as_estate:
        return NodeRoleDecision("estate")
    # QBCTWU U3-2: at the TOP level a node that is neither an Anchor Dock nor
    # an estate is read on its own (the skill's plain-node row); the caller
    # lowers one {"node": uuid} entry through the render_nodes machinery.
    # Inside a note the nesting refusal above already spoke.
    return NodeRoleDecision("node")
