"""Account-scoped coherence paths (S01 §5.1).

Pure path computation for the coherence subtree beneath the Cartographer
jobs root.  No directories are created here — only explicit
initialization/write functions create directories.

Canonical layout::

    <jobs_root>/.coherence/<account_scope_id>/
        writer.lock
        manifest.json
        external_dirt.json
        journal/
            00000000000000000001.json
            ...
        quarantine/

Separate accounts never share manifest, journal, external dirt, or writer-lock
files. Short coherence serialization is in process. Account derivation stays with
``carto_job_governance.derive_account_scope_id`` — this module never
re-implements the account hash and never persists credentials.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .. import carto_job_governance as gov
from .errors import CoherenceSchemaError

_ACCOUNT_RE = re.compile(r"^[0-9a-f]{32}$")

_COHERENCE_DIRNAME = ".coherence"
_MANIFEST_NAME = "manifest.json"
_EXTERNAL_DIRT_NAME = "external_dirt.json"
_JOURNAL_DIRNAME = "journal"
_QUARANTINE_DIRNAME = "quarantine"
_WRITER_LOCK_NAME = "writer.lock"


# @beacon[
#   id=coherence@paths,
#   slice_labels=carto-governance,mci-epoch-paths,
#   kind=ast,
#   comment=Account-scoped durable coherence and lifetime writer-lock paths; short coherence serialization and publication ownership have no filesystem path. Pure path computation creates no directories.,
# ]
@dataclass(frozen=True)
class CoherencePaths:
    """Resolved coherence artifacts and lifetime writer-lock path for one account."""

    account_root: Path
    manifest_path: Path
    external_dirt_path: Path
    journal_dir: Path
    quarantine_dir: Path
    writer_lock_path: Path


def validate_account_scope_id(account_scope_id: object) -> str:
    """Validate an account scope as exactly 32 lowercase hexadecimal chars."""

    if not isinstance(account_scope_id, str) or not _ACCOUNT_RE.match(account_scope_id):
        raise CoherenceSchemaError(
            "account scope id must be exactly 32 lowercase hexadecimal characters",
            account_scope_id=None,
        )
    return account_scope_id


def journal_record_filename(sequence: int) -> str:
    """Record filenames derive only from sequence: fixed-width 20-digit decimal."""

    return f"{sequence:020d}.json"


def parse_journal_record_filename(name: str) -> int | None:
    """Parse a journal record filename back to its sequence, or None."""

    if not name.endswith(".json"):
        return None
    stem = name[: -len(".json")]
    if len(stem) != 20 or not stem.isdigit():
        return None
    return int(stem)


def _resolve_physical_normalized(path: Path) -> Path:
    """Physically resolve a path (symlinks/junctions) and normalize the
    Windows extended-length prefix AFTER resolution.

    Physical resolution comes FIRST (containment must be proven against the
    physical target, never the lexical spelling — a directory junction at
    ``.coherence/<account>`` must not masquerade as an in-tree account
    root).  Windows ``Path.resolve`` may return extended-length forms, both
    normalized here:

    - ``\\\\?\\C:\\...``                  -> ``C:\\...``
    - ``\\\\?\\UNC\\server\\share\\...``  -> ``\\\\server\\share\\...``
    """

    resolved = path.resolve(strict=False)
    text = str(resolved)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[len("\\\\?\\UNC\\"):]
    elif text.startswith("\\\\?\\"):
        text = text[len("\\\\?\\"):]
    return Path(text)


def coherence_paths(
    account_scope_id: str, jobs_root: str | os.PathLike[str] | None = None
) -> CoherencePaths:
    """Compute the coherence layout for one account scope.

    Reuses :func:`carto_job_governance.get_jobs_root` so the coherence
    subtree always lives beside the audited governance artifacts.  Every
    returned path remains beneath the PHYSICALLY resolved account root
    after normalization (junction/symlink escapes fail closed). The writer
    file itself is physically resolved too, so an existing file symlink
    cannot redirect the lifetime lock outside that account root.
    """

    scope = validate_account_scope_id(account_scope_id)
    root = gov.get_jobs_root(jobs_root)
    coherence_root = _resolve_physical_normalized(root / _COHERENCE_DIRNAME)
    account_root = _resolve_physical_normalized(coherence_root / scope)
    try:
        account_root.relative_to(coherence_root)
    except ValueError as exc:
        raise CoherenceSchemaError(
            "account root escapes the coherence directory",
            path=account_root,
            account_scope_id=scope,
        ) from exc
    paths = CoherencePaths(
        account_root=account_root,
        manifest_path=account_root / _MANIFEST_NAME,
        external_dirt_path=account_root / _EXTERNAL_DIRT_NAME,
        journal_dir=account_root / _JOURNAL_DIRNAME,
        quarantine_dir=account_root / _QUARANTINE_DIRNAME,
        writer_lock_path=_resolve_physical_normalized(account_root / _WRITER_LOCK_NAME),
    )
    for path in (
        paths.manifest_path,
        paths.external_dirt_path,
        paths.journal_dir,
        paths.quarantine_dir,
        paths.writer_lock_path,
    ):
        normalized = Path(os.path.normpath(str(path)))
        if normalized != path and normalized.parent != path.parent:
            raise CoherenceSchemaError(
                "coherence path escapes account root", path=path, account_scope_id=scope
            )
        try:
            path.relative_to(account_root)
        except ValueError as exc:
            raise CoherenceSchemaError(
                "coherence path escapes account root", path=path, account_scope_id=scope
            ) from exc
    return paths


def writer_lock_path(
    account_scope_id: str, jobs_root: str | os.PathLike[str] | None = None
) -> Path:
    """Return the sole containment-checked writer path without creating anything."""
    return coherence_paths(account_scope_id, jobs_root).writer_lock_path


def ensure_coherence_directories(paths: CoherencePaths) -> None:
    """Create the coherence directory skeleton (explicit init/write only)."""

    paths.journal_dir.mkdir(parents=True, exist_ok=True)
    paths.quarantine_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# In-process coherence lock — §5.2 lock doctrine
# ---------------------------------------------------------------------------
#
# Fixed lock order for all current and future code:
#
#     in-process coherence lock
#         → process-local CacheView publisher mutex
#
# The reverse order is forbidden. Short lock scope must contain only
# bounded local reads, validation, atomic publication, and reference swaps —
# never HTTP, WebSocket, subprocess waiting, large snapshot serialization, or
# sleep-based backoff. Long-lived typed-region holds are a different category
# and legitimately span network I/O. Synchronous no-await transactions run to
# completion on the daemon loop without taking this lock.

from collections.abc import AsyncIterator, Callable  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from .errors import CoherenceLockError  # noqa: E402


_COHERENCE_LOCKS: dict[str, asyncio.Lock] = {}


def _coherence_lock_for(paths: CoherencePaths) -> asyncio.Lock:
    """Return the one short lock for a normalized absolute account root."""
    key = os.path.normcase(os.path.abspath(os.fspath(paths.account_root)))
    if key not in _COHERENCE_LOCKS:
        _COHERENCE_LOCKS[key] = asyncio.Lock()
    return _COHERENCE_LOCKS[key]


# @beacon[
#   id=coherence@inprocess-lock,
#   slice_labels=carto-governance,mci-epoch-paths,
#   kind=ast,
#   comment=One in-process coherence lock per account root with existing cancel-aware wait budget; synchronous no-await transactions run to completion, and coherence precedes the publisher mutex. No file lock or network I/O.,
# ]
@asynccontextmanager
async def coherence_lock_async(
    paths: CoherencePaths,
    *,
    timeout_seconds: float | None = None,
    poll_seconds: float | None = None,
    cancel_check: Callable[[], object] | None = None,
) -> AsyncIterator[object]:
    """Acquire the short lock with no queued or late acquisition transaction."""
    timeout = gov.DEFAULT_MUTEX_WAIT_SECONDS if timeout_seconds is None else float(timeout_seconds)
    poll = gov.DEFAULT_MUTEX_POLL_SECONDS if poll_seconds is None else float(poll_seconds)
    deadline = time.monotonic() + max(0.0, timeout)
    lock = _coherence_lock_for(paths)
    while True:
        await gov._check_cancel_async(cancel_check, paths.account_root)
        if not lock.locked():
            await lock.acquire()  # An unlocked asyncio acquire cannot suspend.
            break
        if time.monotonic() >= deadline:
            raise CoherenceLockError(
                "coherence lock busy", busy=True, path=paths.account_root
            )
        await gov._check_cancel_async(cancel_check, paths.account_root)
        await asyncio.sleep(max(0.0, min(poll, deadline - time.monotonic())))
    try:
        yield lock
    finally:
        lock.release()
