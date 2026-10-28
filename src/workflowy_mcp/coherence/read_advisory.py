"""The ONE standardized hard-barrier read advisory (S04 §7.5).

While the hard barrier is set, every cache-backed read carries exactly ONE
standardized advisory — a structured field on structured replies, or one
standard warning line (plus ONE blank separator line) atop raw-text/Markdown
output.  The advisory is FRAMING, exactly like the established
``[name: not editable]`` prefix on ``node.read``: self-declaring as
advisory-only, never part of the returned content.  There is deliberately
NO byte-surface carve-out (A1, Dan-ruled).

Single-authority rules (H1(b)):

- the canonical warning literal/template exists at exactly ONE production
  definition site (this module);
- ONE authority decides applicability (:func:`apply_coherence_read_advisory`);
- per-shape thin adapters place the shared result into established envelopes
  but never copy the literal or independently decide barrier applicability;
- application is IDEMPOTENT (an internal render seam and a family finalizer
  may both legitimately touch the same result — the second is a no-op);
- clean runs are byte-identical to the pre-S04 output (no field, no prefix).

PROVENANCE (S04 Remediation Chain 4, work-order item 1): the advisory makes
a CACHE-PROVENANCE claim ("this cache-backed result may be stale"), so
content that is NOT /nodes-export-derived must never carry it.  Callers
declare that provenance EXPLICITLY via ``cache_sourced=False`` — the one
authority still owns the literal and all shaping; no caller copies warning
logic.  Browser-captured frozen DOM content (a ``render_spec="modal"``
``dom_captured_tree`` read, a Scoped Lightning run over a genuinely
DOM-frozen source, a live-DOM modal render) is cache-free and passes
``cache_sourced=False``; a Scoped Lightning run whose source was a
cache-projected slice passes ``cache_sourced=True`` and stays advised.

The advisory reflects the manifest state captured at the operation's
adoption boundary (§7.6): a barrier raised after that linearization point
need not retroactively alter the in-flight result.
"""

from __future__ import annotations

from typing import Any

from .barrier import CoherenceBoundaryStatus

# The exact frozen warning line (§7.5).  ONE definition site (H1(b); grep
# must find exactly one).  Em-dash and wording are part of the contract.
COHERENCE_READ_ADVISORY_LINE = (
    "⚠️ COHERENCE FAILURE — advisory only; NOT part of the returned content. "
    "This cache-backed result may be stale; Workflowy mutations are blocked "
    "until a full system.cache.refresh succeeds."
)


def current_boundary_status() -> CoherenceBoundaryStatus | None:
    """The operation's CAPTURED boundary status (§7.6), or None.

    Reads the task-local operation snapshot only — never the manifest,
    never a fresh hard-stop query (H4 boundary timing).  None outside an
    operation boundary or when the capture hook was unavailable.
    """

    from .operation_cache import current_operation_cache

    snapshot = current_operation_cache(required=False)
    if snapshot is None:
        return None
    return getattr(snapshot, "boundary_status", None)


# @beacon[
#   id=auto-beacon@coherence_advisory_payload-ctfv,
#   role=coherence_advisory_payload,
#   slice_labels=mci-barrier-advisory,
#   kind=ast,
# ]
def coherence_advisory_payload(
    status: CoherenceBoundaryStatus,
) -> dict[str, Any]:
    """The one structured advisory object for ``status`` (which must be
    hard-dirty). Omits unavailable diagnostic values (never fabricated)."""

    advisory: dict[str, Any] = {
        "status": "hard_dirty",
        "refresh_required": True,
        "dirty_generation": status.hard_generation,
        "message": COHERENCE_READ_ADVISORY_LINE,
    }
    if status.hard_reason_code is not None:
        advisory["reason_code"] = status.hard_reason_code
    if status.hard_transaction_id is not None:
        advisory["transaction_id"] = status.hard_transaction_id
    return {"coherence_advisory": advisory}


def _prefix_text(text: str) -> str:
    """Warning line + ONE blank separator line + payload (idempotent)."""

    if text.startswith(COHERENCE_READ_ADVISORY_LINE):
        return text
    return COHERENCE_READ_ADVISORY_LINE + "\n\n" + text


def apply_coherence_advisory_to_text(
    text: str,
    *,
    status: CoherenceBoundaryStatus | None = None,
    cache_sourced: bool = True,
) -> str:
    """Thin raw-text adapter: prefix the standard warning under hard dirt.

    Clean status or unavailable capture returns the input byte-identically.
    ``cache_sourced=False`` declares cache-free (browser-captured) content
    and returns the input byte-identically even under hard dirt (S04
    Remediation Chain 4, item 1 — the advisory is a cache-provenance claim
    and must never ride content that is not /nodes-export-derived).
    """

    if not cache_sourced:
        return text
    if status is None:
        status = current_boundary_status()
    if status is None or not status.hard_dirty:
        return text
    return _prefix_text(text)


# @beacon[
#   id=coherence@read-advisory,
#   slice_labels=ra-coherence,ra-workflowy-cache,mci-barrier-advisory,
#   kind=ast,
#   comment=S04 ONE hard-barrier read-advisory authority (H1(b)): the exact frozen warning literal at one definition site; idempotent per-shape application (raw string, preview_markdown/preview_text render dicts, generic structured dicts); errors never double-wrapped; pure live/API or disk reads and clean runs byte-identical; consumes only the operation's CAPTURED CoherenceBoundaryStatus (H4); the advisory is a CACHE-PROVENANCE claim — callers declare cache-free browser-captured content explicitly via cache_sourced=False (frozen DOM captures, genuinely DOM-frozen Scoped Lightning) while cache-derived renders stay advised, one literal + one shaping authority with provenance carried explicitly (Remediation Chain 4).,
# ]
def apply_coherence_read_advisory(
    result: Any,
    *,
    status: CoherenceBoundaryStatus | None = None,
    cache_sourced: bool = True,
) -> Any:
    """Apply the standardized hard-barrier advisory to one operation result.

    Shapes:

    - raw string  → warning line + one blank separator + the original bytes;
    - dict with ``preview_markdown`` / ``preview_text`` (internal render
      dictionaries) → the preview field is prefixed BEFORE agent shaping,
      modal delivery, or file writing, and the dict gains the structured
      ``coherence_advisory`` object;
    - any other structured dict → gains ``coherence_advisory`` only;
    - error envelopes (``success: False`` or an ``error`` payload) →
      returned unchanged: errors remain errors and are never wrapped in a
      second warning envelope;
    - anything else → returned unchanged.

    Idempotent: a result already carrying the advisory (field present, or a
    preview/string already prefixed) is returned unchanged.  Clean status
    returns the input byte-identically.  ``cache_sourced=False`` declares
    cache-free (browser-captured) content and returns the input
    byte-identically even under hard dirt (S04 Remediation Chain 4,
    item 1).
    """

    if not cache_sourced:
        return result
    if status is None:
        status = current_boundary_status()
    if status is None or not status.hard_dirty:
        return result
    if isinstance(result, str):
        return _prefix_text(result)
    if not isinstance(result, dict):
        return result
    if result.get("success") is False or result.get("error") is not None:
        return result
    if "coherence_advisory" in result:
        return result
    out = dict(result)
    preview = out.get("preview_markdown")
    if isinstance(preview, str):
        out["preview_markdown"] = _prefix_text(preview)
    preview_text = out.get("preview_text")
    if isinstance(preview_text, str):
        out["preview_text"] = _prefix_text(preview_text)
    out.update(coherence_advisory_payload(status))
    return out
