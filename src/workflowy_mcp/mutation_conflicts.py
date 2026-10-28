"""Observed name/note collisions, independent of admission and transport errors."""

from collections.abc import Mapping, Sequence


# @beacon[
#   id=node-ops@concurrent-edit-detected,
#   slice_labels=node-ops,
#   kind=ast,
#   comment=Typed written-field collision with raw observed and expected wire values; distinguishes an unsent attempt from an already-sent update without retrying.,
# ]
class ConcurrentEditDetectedError(RuntimeError):
    """A differing live written-field observation; never a transport retry signal."""

    code = "concurrent_edit_detected"

    def __init__(
        self,
        *,
        node_id: str,
        fields: Sequence[str],
        live: Mapping[str, str | None],
        expected: Mapping[str, str | None],
        detected_at: str,
        previously_sent: Mapping[str, str | None] | None = None,
    ) -> None:
        self.node_id = node_id
        self.fields = list(fields)
        self.live = {field: live[field] for field in self.fields}
        self.expected = {field: expected[field] for field in self.fields}
        self.detected_at = detected_at
        self.retry_after = 0.0
        self.previously_sent = dict(previously_sent) if previously_sent is not None else None
        if detected_at == "preflight":
            outcome = "This attempt did not send."
            if previously_sent is not None:
                outcome += " An earlier attempt may already have sent the update."
        else:
            outcome = "The update was sent; the observed state differs from what was sent."
        super().__init__(
            f"The node changed during this edit. {outcome} "
            "Re-read it and recompute the edit before retrying."
        )

    @property
    def extra(self) -> dict:
        """Return a detached observation payload for the shared error formatter."""
        payload = {
            "node_id": self.node_id,
            "fields": list(self.fields),
            "live": dict(self.live),
            "expected": dict(self.expected),
            "retry_after": self.retry_after,
            "detected_at": self.detected_at,
        }
        if self.previously_sent is not None:
            payload["previously_sent"] = dict(self.previously_sent)
        return payload
