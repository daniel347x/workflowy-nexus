"""Handle-correction echo state (to-do 23db916f).

When the shared prefix kernel adopts a non-exact UUID (the caller supplied a
handle that was not an exact cache key and the character-backtrack shaved it),
Layer 1 records ONE correction here so the MCP result finalizer can surface a
single short ``resolved: ‹short› ‹name› (corrected from ‹supplied›)`` line —
the silent-UUID-correction visibility fix. Exact matches, AP-IDs and clean
prefixes (``matched_prefix_len == len(normalized supplied)``) never record.

Pure ``contextvars`` state, one operation per dispatch: ``_dispatch_operation_core``
resets before invoking each handler, and ``_mcp_operation_result_finalizer``
drains and renders. The module imports nothing from the package (no cycles)
and is safe to set from any task; corrections recorded outside a registry
dispatch (e.g. internal callers, legacy-profile tools) are simply never
drained and are cleared by the next operation's reset.
"""

from __future__ import annotations

from contextvars import ContextVar

# Immutable tuple (never a shared mutable list): recording sets a NEW tuple so
# contexts copied by asyncio tasks cannot observe each other's appends.
_CORRECTIONS: "ContextVar[tuple[dict[str, str], ...] | None]" = ContextVar(
    "nexus_handle_corrections", default=None
)

# @beacon[
#   id=handle-resolution@correction-echo,
#   role=ƒ record_handle_correction(supplied, resolved_uuid, name, short),
#   slice_labels=handle-resolution,nexus--mcp-api,
#   kind=ast,
#   comment=THE per-operation handle-correction echo state (23db916f): Layer 1 (resolve_uuid_like_node_handle) records one entry here ONLY when the shared prefix kernel adopted a non-exact UUID (matched_prefix_len < the normalized supplied length -- a shave; an exact hit, an AP-ID and a clean 8-hex-or-longer prefix that matched at its own full length stay silent); _dispatch_operation_core resets the state before each handler invocation and _mcp_operation_result_finalizer drains it, prepending one 'resolved: <short> <name> (corrected from <supplied>)' line per entry to string results and attaching a top-level resolved_corrections list to dict results, always BELOW the coherence read advisory and ABOVE any render-tail header. Pure contextvars (an immutable tuple per record); imports nothing from the package; corrections recorded outside a registry dispatch are never drained and are cleared by the next operation's reset. Chain-2 r2 (R1): a caller that REJECTS a shaved inherited stored-root match discards the speculative echo surgically via discard_last_handle_correction (the ONE matching entry -- never a blanket reset, so a legitimate sibling correction in the same mixed operation survives).,
# ]


def record_handle_correction(
    *, supplied: str, resolved_uuid: str, name: str, short: str
) -> None:
    """Record one correction for this operation's result echo (append-safe)."""

    entry = {
        "supplied": str(supplied or ""),
        "resolved_uuid": str(resolved_uuid or ""),
        "name": str(name or ""),
        "short": str(short or ""),
    }
    current = _CORRECTIONS.get() or ()
    _CORRECTIONS.set(current + (entry,))


def drain_handle_corrections() -> list[dict[str, str]]:
    """Return this operation's recorded corrections (in order) and clear them."""

    current = _CORRECTIONS.get() or ()
    if current:
        _CORRECTIONS.set(())
    return [dict(entry) for entry in current]


def reset_handle_corrections() -> None:
    """Clear any stale corrections before a new operation's handler runs."""

    if _CORRECTIONS.get() is not None:
        _CORRECTIONS.set(())


def discard_last_handle_correction(
    *, supplied: str, resolved_uuid: str
) -> bool:
    """Remove the MOST RECENT correction matching (supplied, resolved_uuid).

    Archive Reads Chain-2 r2 (R1): when a caller REJECTS a shaved corrective
    match — an inherited stored group root must keep its stored identity, not
    retarget to the unrelated live node the prefix kernel reached by shaving
    the stored UUID — the speculative echo Layer 1 recorded for that very
    resolution must never reach the delivered result nor displace a
    legitimate sibling correction.  Surgical by design, never a blanket
    reset: only the LAST matching entry is removed (the one this resolution
    just recorded — a rejection happens immediately after its own resolve),
    so an earlier identical entry adopted elsewhere in the same mixed
    operation (an explicit corrective override) survives, and corrections
    for other handles are untouched.  Pure contextvars; returns True when an
    entry was removed.
    """

    current = _CORRECTIONS.get() or ()
    target_supplied = str(supplied or "")
    target_resolved = str(resolved_uuid or "")
    for index in range(len(current) - 1, -1, -1):
        entry = current[index]
        if (
            entry.get("supplied") == target_supplied
            and entry.get("resolved_uuid") == target_resolved
        ):
            _CORRECTIONS.set(current[:index] + current[index + 1 :])
            return True
    return False


def format_handle_correction_line(entry: dict[str, str]) -> str:
    """The one short echo line: ``resolved: ‹short› ‹name› (corrected from …)``."""

    return (
        f"resolved: {entry.get('short', '')} {entry.get('name', '')} "
        f"(corrected from {entry.get('supplied', '')})"
    )


def has_pending_corrections() -> bool:
    """True when at least one correction is recorded for the current operation."""

    return bool(_CORRECTIONS.get())
