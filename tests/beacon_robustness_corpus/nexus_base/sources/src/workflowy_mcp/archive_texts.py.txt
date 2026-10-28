"""Exact user-facing archive texts (Archive Reads, MasterSpec §5).

ONE definition site: every user-visible archive text is produced here and
nowhere else — no other module spells any of them.  Unit 2 carries §5.1
(the banner), §5.2 (the write refusals), §5.3 (the other refusals) and
§5.4 (the light hint); §5.6 arrived with Unit 1; Unit 3 adds the footer
(§5.5), the describe texts (§5.7–§5.11) and §5.12's defaults.  ``{path}``
is the archive file's absolute path as the store listed it;
``{exported_at}`` the archive block's ``exported_at`` verbatim;
``{handle}`` the handle exactly as the caller supplied it; ``{verb}`` the
canonical operation name.
"""

from __future__ import annotations

# §5.2.2 — the how-to-copy sentences, appended to the §5.2.1 text (Axiom 5,
# [ap:JSFXFV] E1).  Leading space: they are appended to the sentence text
# verbatim, one more sentence in the same line.
EXTRACT_COPY_SENTENCE = (
    " To copy its text into Workflowy, read it (node.read and "
    "anchor.point.read serve archived nodes), then capture it "
    "(anchor.point.capture with content)."
)

PROMOTE_COPY_SENTENCE = (
    " To copy it into Workflowy instead, promote it with mode='derivative' "
    "(a copy with fresh AP-IDs; the archive is untouched), or read it and "
    "capture it."
)


def banner_line(archive_file: str, exported_at: str) -> str:
    """§5.1 — one line per archive file that answered at least one handle."""

    return (
        "⚠️⚠️⚠️ ARCHIVED NODE: NOT IN WORKFLOWY. Served read-only from "
        f"export file {archive_file} (exported {exported_at}). Tell the human."
    )


def archived_node_read_only_text(
    verb: str, handle: str, archive_file: str, exported_at: str
) -> str:
    """§5.2.1 — a write aimed at the archived node itself."""

    return (
        f"{verb}: {handle} is an ARCHIVED node, not in Workflowy: it lives "
        f"read-only in export file {archive_file} (exported {exported_at}). "
        "Archived nodes are never changed. No action was taken."
    )


def archived_parent_read_only_text(
    verb: str, role: str, handle: str, archive_file: str, exported_at: str
) -> str:
    """§5.2.3 — an archived parent or destination (``role``: parent|destination)."""

    return (
        f"{verb}: the {role} {handle} is an ARCHIVED node, not in Workflowy "
        f"(export file {archive_file}, exported {exported_at}); nothing can "
        f"be created, moved or promoted under an archived node. Choose a "
        f"live {role}. No action was taken."
    )


def archived_export_refusal_text(
    verb: str, handle: str, archive_file: str, exported_at: str
) -> str:
    """§5.3.1 — ``node.export`` aimed at an archived root."""

    return (
        f"{verb}: {handle} is already archived: it lives in export file "
        f"{archive_file} (exported {exported_at}) and is not in Workflowy, "
        "so it cannot be exported again. Copy that file instead. No file "
        "was written."
    )


def archived_sink_refusal_text(
    verb: str, handle: str, archive_file: str, exported_at: str
) -> str:
    """§5.3.2 — a sink (``output_file`` / ``push_to_modal``) on a read whose
    handle is archived."""

    return (
        f"{verb}: {handle} is an ARCHIVED node (export file {archive_file}, "
        f"exported {exported_at}); archived material is never written to a "
        "file or pushed to the modal. Call again without output_file / "
        "push_to_modal to read it."
    )


def archived_purge_preview_text(
    verb: str, handle: str, archive_file: str, exported_at: str
) -> str:
    """§5.3.3 — a purge preview aimed at an archived root."""

    return (
        f"{verb}: {handle} is an ARCHIVED node (export file {archive_file}, "
        f"exported {exported_at}); archives are never part of a tag purge. "
        "Nothing was previewed."
    )


def archived_no_context_text(
    verb: str, handle: str, archive_file: str, exported_at: str
) -> str:
    """§5.3.4 — a not-found site with no operation context (a WebSocket
    handler, a legacy-profile tool); ``verb`` is the site's existing label."""

    return (
        f"{verb}: {handle} is an ARCHIVED node (export file {archive_file}, "
        f"exported {exported_at}); archived nodes are served only by the "
        "MCP read verbs. No action was taken."
    )


# ---------------------------------------------------------------------------
# §5.4 — the light hint (AP-shaped handles only; UUID-shaped misses keep
# today's text exactly, per [ap:O8R74F]).
# ---------------------------------------------------------------------------


def ap_hint_no_folder() -> str:
    """§5.4 state A — no archive folder configured."""

    return (
        "(no node carries this AP-ID; if part of this tree has been "
        "exported, it may live in an export file: no archive folder is "
        "configured, so none was searched)"
    )


def ap_hint_not_found(active_count: int, folder: str) -> str:
    """§5.4 state B — searched, not found."""

    return (
        "(no node carries this AP-ID, in Workflowy or in the "
        f"{active_count} active archive(s) in {folder})"
    )


def ap_hint_unreadable(folder: str) -> str:
    """§5.4 state C — the folder could not be fully read."""

    return (
        "(no node carries this AP-ID in Workflowy; the archive folder "
        f"{folder} could not be fully read, so archives may be missing: "
        "system(verb='archives.list') shows why)"
    )


def ap_hint_beacon() -> str:
    """§5.4 — the beacon family's line (it never consults the store)."""

    return (
        "(no node carries this AP-ID in live Workflowy; beacon never reads "
        "export archives: node.resolve or anchor.point.resolve read them)"
    )


def ap_core_collision_text(core: str, archive_file: str) -> str:
    """§5.6 — an explicit ``ap_id`` whose core an archive already carries."""

    return (
        f"AP-ID [ap:{core}] is already carried by an archived node "
        f"(export file {archive_file}); choose another."
    )


# ---------------------------------------------------------------------------
# §5.5 — the footer (``include_archives`` false, archived material left
# out).  Composed by the result finalizer from the overlay's planted chains;
# ``{n}`` is the number of active/orphaned archives whose planted-parent
# chain contains one of the operation's recorded scope roots.
# ---------------------------------------------------------------------------


def archives_left_out_footer(count: int) -> str:
    """§5.5 — the one-line footer, appended as the LAST line of a string
    result (or carried as ``archives_left_out`` on a dict result)."""

    return (
        f"📦 {count} archived subtree(s) under this scope were left out; "
        "include_archives=true includes them (rules: system(verb='describe', "
        "params={'of': 'archives.list', 'detail': 'full'}))"
    )


# ---------------------------------------------------------------------------
# §5.7 — the inventory's describe: THE canonical doctrine
# (``system.archives.list``'s ``summary``, one line, verbatim).
# ---------------------------------------------------------------------------

ARCHIVES_LIST_SUMMARY = (
    "Export archives, the rules: NEXUS reads the archive files node.export "
    "(output_file) writes, from ONE folder: the GLIMPSE extension's Archive "
    "folder option, which the daemon remembers (reads need no tab). An "
    "archive is ACTIVE once its exported root is gone from Workflowy "
    "(deleted, and the cache has seen it); while the root still lives it is "
    "DORMANT and never read; whole-vault exports are SKIPPED. Archives "
    "answer two ways, both read-only: (1) automatically, any read whose "
    "handle (an AP-ID, a UUID or its 8-hex segment, every handle in a list) "
    "finds nothing in live Workflowy is answered from the active archives, "
    "and the result opens with ⚠️⚠️⚠️ ARCHIVED NODE (tell the human); (2) on "
    "request, include_archives=true on a search, sift, slice group or render "
    "adds each active archive's subtree under the parent it was exported "
    "from, every archived node marked 📦, after the live children. Live "
    "always wins: archives are consulted only when live Workflowy has no "
    "match and never make a live handle ambiguous; an exact archived UUID "
    "beats only a shaved (corrected) live match. The parent rule: for slice "
    "groups, sift and anything that descends from a root, the exported "
    "subtree's parent must stay in Workflowy. It may be moved, but it must "
    "stay under the root you search or sift from, and suppression, "
    "completion and every other rule apply exactly as if nothing had been "
    "exported. An archive whose parent exists nowhere is ORPHANED: it sits "
    "at the top level, reachable by its own handles, and archives.mount "
    "re-roots it. Writes never touch archives: a write aimed at an archived "
    "node is refused with the file and date (batches skip it and carry on); "
    "to copy archived material into Workflowy, read it and capture it, or "
    "anchor.point.promote with mode='derivative'. Never served from "
    "archives: beacon (symbols are live only), node.export, output_file, "
    "push_to_modal, the GUI. New AP-IDs never reuse an archived AP-ID. "
    "States: active · orphaned · dormant · superseded (shadowed by a newer "
    "archive) · skipped (whole vault) · unreadable (with the reason)."
)


# ---------------------------------------------------------------------------
# §5.8 — the ONE ``include_archives`` parameter description.  Defined ONCE
# here; every schema embeds this same dict object by reference, so the
# description string exists exactly once in the source tree.
# ---------------------------------------------------------------------------

INCLUDE_ARCHIVES_PROPERTY: dict = {
    "type": "boolean",
    "description": (
        "Default false. true adds the active export archives to this read: "
        "archived subtrees appear under the parent they were exported from, "
        "each archived node marked 📦, read-only. The exported subtree's "
        "parent must stay in Workflowy (it may be moved, but it must stay "
        "under the root you search or sift from); every other rule applies "
        "exactly as if nothing had been exported. A handle that misses live "
        "Workflowy is answered from the archives automatically, with or "
        "without this flag. Cannot combine with output_file or "
        "push_to_modal. Rules: system(verb='describe', params={'of': "
        "'archives.list', 'detail': 'full'})."
    ),
}


# ---------------------------------------------------------------------------
# §5.9 — the refused-combination messages (raised by the §4.3.2 classifier
# before any handler work; code invalid_parameter_combination).
# ---------------------------------------------------------------------------


def flag_output_file_refusal_text() -> str:
    """§5.9 — ``include_archives`` with ``output_file``."""

    return (
        "include_archives cannot be combined with output_file: archived "
        "material is never written to a file. Drop one of them."
    )


def flag_push_to_modal_refusal_text() -> str:
    """§5.9 — ``include_archives`` with ``push_to_modal: true``."""

    return (
        "include_archives cannot be combined with push_to_modal: the docked "
        "modal never shows archived nodes. Drop one of them."
    )


def flag_needs_scope_refusal_text() -> str:
    """§5.9 — ``glimpse.run`` with the flag and no server-side scope."""

    return (
        "include_archives needs a server-side scope (root_handle, "
        "group_handle or render_spec); the live DOM view is live by "
        "definition."
    )


# Manager-Session-147: the one-line summary archives.list shows in the
# always-on system tool description. describe shows it followed by
# ARCHIVES_LIST_SUMMARY (registered as the verb's describe_suffix), so the
# rules every family points to ("rules: system describe of archives.list")
# are exactly where those pointers say.
ARCHIVES_LIST_INDEX_SUMMARY = (
    "List the export archives, one row each with its state (active · "
    "orphaned · dormant · superseded · skipped · unreadable) and why; "
    "describe this verb for the archive rules (how archived nodes are read, "
    "the parent rule, mounting)."
)


# ---------------------------------------------------------------------------
# §5.10 — the top-level tool descriptions (one sentence each), appended to
# the family summaries.
# ---------------------------------------------------------------------------

ARCHIVE_FAMILY_SENTENCE = (
    " 📦 Export archives: a read whose handle misses live Workflowy is "
    "answered read-only from the configured archive folder under a ⚠️ "
    "banner; include_archives=true adds archived subtrees to searches and "
    "renders; writes never touch archives (rules: system describe of "
    "archives.list)."
)

BEACON_FAMILY_SENTENCE = (
    " Beacon never reads export archives: symbols, snippets and resolves "
    "see live Workflowy only."
)

# Manager-Session-147 (token pass, Dan's low-risk set): the render families
# (glimpse, lightning_rod, slice_group) carry this short form; node, anchor
# and system keep ARCHIVE_FAMILY_SENTENCE; tag (writes only, refused with a
# reason on an archived node) carries none.
ARCHIVE_FAMILY_SHORT_SENTENCE = (
    " 📦 Export archives: include_archives=true adds archived subtrees, "
    "read-only (rules: system describe of archives.list)."
)

# §U3.2 deliverable 7: the ONE shared sentence appended to the summaries of
# node.read, anchor.point.read, anchor.dock.get_description, node.resolve,
# anchor.point.resolve and node.ancestors.
ARCHIVE_AUTO_SENTENCE = (
    " A handle that misses live Workflowy is answered read-only from the "
    "export archives (rules: system describe of archives.list)."
)


# ---------------------------------------------------------------------------
# §5.11 — the mount verb (``system.archives.mount``).
# ---------------------------------------------------------------------------

ARCHIVES_MOUNT_SUMMARY = (
    "Set or clear an archive's mount point, the parent its root is planted "
    "under when the parent it was exported from is gone: writes only the "
    "archive block's mount_parent_id, through the format authority (nodes "
    "and their SHA-256 untouched), verified like an export; "
    "parent_handle=null clears it. Rules: system describe of archives.list."
)


def mount_no_such_archive_text(archive: str, folder: str) -> str:
    """§5.11 refusal — no archive file of that name in the folder."""

    return (
        f"archives.mount: no archive file named {archive} in {folder} "
        "(system archives.list lists them)."
    )


def mount_unreadable_text(archive: str, reason: str) -> str:
    """§5.11 refusal — the file cannot be read."""

    return (
        f"archives.mount: {archive} cannot be read ({reason}); nothing "
        "was written."
    )


def mount_vault_text(archive: str) -> str:
    """§5.11 refusal — a whole-vault export takes no mount point."""

    return (
        f"archives.mount: {archive} is a whole-vault export; vault exports "
        "are never read, so they take no mount point."
    )


def mount_parent_nowhere_text(handle: str) -> str:
    """§5.11 refusal — the parent is in neither Workflowy nor an active
    archive."""

    return (
        f"archives.mount: parent {handle} is neither in Workflowy nor in "
        "an active archive; nothing was written."
    )


def mount_parent_inside_text(handle: str, archive: str) -> str:
    """§5.11 refusal — the parent is inside the archive itself."""

    return (
        f"archives.mount: parent {handle} is inside {archive} itself; "
        "nothing was written."
    )


def mount_cycle_text(archive: str, handle: str, other: str) -> str:
    """§5.11 refusal — mounting would close a cycle."""

    return (
        f"archives.mount: mounting {archive} under {handle} would close a "
        f"cycle with {other}; nothing was written."
    )


def mount_unverified_text(archive: str) -> str:
    """§5.11 — the write did not read back byte-identical."""

    return (
        f"archives.mount: {archive} was written but did not read back "
        "byte-identical; it is UNVERIFIED, check the file before relying "
        "on it."
    )


# ---------------------------------------------------------------------------
# §5.12 — defaults that are part of the contract.
# ---------------------------------------------------------------------------

ARCHIVE_FOLDER_OPTION_LABEL = "Archive folder"

ARCHIVE_FOLDER_OPTION_HELP = (
    "Export archives the daemon reads when a handle is not in Workflowy "
    "(node.export output_file writes them). One absolute folder; leave "
    "empty to turn archive reads off."
)


def archives_inventory_header(
    folder: str,
    learned_at: "str | None",
    counts: "dict[str, int]",
) -> str:
    """§5.12 — the inventory compact text's ONE header line.

    ``counts`` carries the six state counts (active, orphaned, dormant,
    superseded, skipped, unreadable); ``folder`` is the remembered folder
    value or "" (rendered ``(none configured)``); ``learned_at`` the memory
    file's timestamp or None (rendered ``never``).
    """

    return (
        f"📦 Archive folder: {folder or '(none configured)'}"
        f" · learned {learned_at or 'never'}"
        f" · {int(counts.get('total', 0))} file(s):"
        f" {int(counts.get('active', 0))} active"
        f" · {int(counts.get('orphaned', 0))} orphaned"
        f" · {int(counts.get('dormant', 0))} dormant"
        f" · {int(counts.get('superseded', 0))} superseded"
        f" · {int(counts.get('skipped', 0))} skipped"
        f" · {int(counts.get('unreadable', 0))} unreadable"
    )
