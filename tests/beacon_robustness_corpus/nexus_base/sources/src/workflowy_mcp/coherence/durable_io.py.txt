"""Coherence durable-publication facade (S01 §9.1, W2 end-state).

There is exactly ONE temp+fsync+atomic-replace retry loop in the
repository: ``carto_job_governance.atomic_write_text``, extended in place
(W2) with injectable seams and opt-in parent-directory durability sync;
``carto_job_governance.atomic_write_json`` is that loop's JSON front.
This module is a THIN FACADE over the JSON front:

- re-exports :class:`DurableFileOps` (the injection adapter surface the
  coherence fault harness builds on — public shape preserved);
- ``atomic_write_json_durable`` adds the coherence error contract
  (failures surface as ``JournalPublicationError``) and turns the parent
  directory durability sync on;
- ``read_json_artifact`` is the shared strict JSON-object reader.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..carto_job_governance import DurableFileOps, atomic_write_json
from .errors import JournalPublicationError

__all__ = ["DurableFileOps", "atomic_write_json_durable", "read_json_artifact"]


# @beacon[
#   id=coherence@durable-io,
#   slice_labels=carto-governance,mci-epoch-paths,
#   kind=ast,
#   comment=S01 coherence durable-publication facade: thin JournalPublicationError-typed wrapper over the ONE audited durable writer (the loop lives in carto_job_governance.atomic_write_text; atomic_write_json is its JSON front, W2-extended), parent-dir sync enabled; DurableFileOps re-exported as the fault-harness injection surface.,
# ]
def atomic_write_json_durable(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    fs_ops: DurableFileOps | None = None,
) -> Path:
    """Durably and atomically publish one JSON artifact (facade).

    Delegates to the ONE audited writer with the coherence durability
    profile (parent-directory durability sync enabled; POSIX fsync-dir,
    documented no-op on Windows).  All failures surface as
    ``JournalPublicationError`` (existing subclasses — e.g. the harness's
    ``InjectedFault`` — pass through unwrapped so fault tests see the exact
    seam); lock-free readers only ever see complete old or complete new
    JSON.
    """

    try:
        return atomic_write_json(path, payload, fs_ops=fs_ops, sync_parent_dir=True)
    except JournalPublicationError:
        raise
    except Exception as exc:
        raise JournalPublicationError(
            f"durable JSON publication failed: {exc}", path=path
        ) from exc


def read_json_artifact(path: str | Path, artifact: str) -> dict[str, Any]:
    """Strict JSON-object read shared by coherence artifact readers."""

    import json

    from .errors import CoherenceSchemaError

    target = Path(path)
    with target.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise CoherenceSchemaError(
            f"{artifact}: top-level JSON must be an object", path=target
        )
    return value
