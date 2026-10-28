"""Rotation for the two daemon-wide debug logs (WaveSpec §7.2 R-b).

`logs/reconcile_debug.log` and `logs/etch_debug.log` are append-only
diagnostics written by several daemon paths (the etch log helper, the
Cartographer diagnostic sink used by the per-file artifact fallback and by the
reconcile debug fallback, and the server's import-time startup line).  On a
long-lived daemon they would otherwise grow without bound, so every one of
those writers goes through :func:`append_debug_line` here:

* a daemon-wide log (`<...>/logs/reconcile_debug.log` or
  `<...>/logs/etch_debug.log`) is rotated once its current size reaches the
  10 MiB budget, into at most five generations (`…log.1` .. `…log.5`);
* any other path -- a per-weave log, a per-file refresh log, a per-run job
  log, or another file name -- is appended exactly as before, never rotated.

Rotation is renames only: the oldest generation is removed, each surviving
generation moves up one, and the current file becomes `.1`. There is no copy,
no truncation and no retry beyond the single rotation itself; the caller keeps
its own error policy (the log writers swallow a best-effort failure).

The gate is serialised by one process-local lock, so concurrent writers in the
daemon cannot rotate the same file twice or append into a file being renamed.
"""

import os
import threading

# Names that rotate, and only when their parent directory is the daemon-wide
# `logs` directory.  Everything else is an ordinary append.
DAEMON_LOG_NAMES = frozenset({"reconcile_debug.log", "etch_debug.log"})
DAEMON_LOG_DIR_NAME = "logs"

ROTATION_MAX_BYTES = 10 * 1024 * 1024
ROTATION_GENERATIONS = 5

_ROTATION_LOCK = threading.Lock()


def is_daemon_debug_log(path) -> bool:
    """True when `path` is one of the daemon-wide logs this module rotates."""
    text = os.fspath(path)
    name = os.path.basename(text).lower()
    if name not in DAEMON_LOG_NAMES:
        return False
    parent = os.path.basename(os.path.dirname(os.path.abspath(text)))
    return parent.lower() == DAEMON_LOG_DIR_NAME


def _rotate(path: str, *, generations: int) -> None:
    """Shift the generations by one rename each, dropping the oldest."""
    oldest = f"{path}.{generations}"
    if os.path.exists(oldest):
        os.remove(oldest)
    for index in range(generations - 1, 0, -1):
        source = f"{path}.{index}"
        if not os.path.exists(source):
            continue
        target = f"{path}.{index + 1}"
        if os.path.exists(target):
            os.remove(target)
        os.rename(source, target)
    first = f"{path}.1"
    if os.path.exists(first):
        os.remove(first)
    os.rename(path, first)


def _rotate_if_due(path: str, incoming: int, max_bytes: int, generations: int) -> bool:
    """Rotate `path` when this append would cross the byte budget."""
    try:
        current = os.path.getsize(path)
    except OSError:
        # No file yet (or unreadable size): the append creates it.
        return False
    if current == 0 or current + incoming <= max_bytes:
        return False
    _rotate(path, generations=generations)
    return True


def _append_unlocked(path: str, payload: str) -> None:
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(payload)


# @beacon[
#   id=auto-beacon@append_debug_line-rb,
#   role=append_debug_line,
#   slice_labels=ra-logging,ra-reconcile,
#   kind=ast,
#   comment=The one rotation gate for the daemon-wide logs/reconcile_debug.log and logs/etch_debug.log: 10 MiB then five .1-.5 generations by rename under a process-local lock; every writer passes its path here, while per-weave, per-file and per-job logs are appended unchanged and never rotate.,
# ]
def append_debug_line(path, line: str, *, max_bytes: int | None = None,
                      generations: int | None = None) -> None:
    """Append one debug line, rotating a daemon-wide log when it is due.

    Args:
        path: destination log path (str or os.PathLike)
        line: the complete line; a trailing newline is added when missing
        max_bytes: rotation budget override (tests inject a small one)
        generations: generation count override (tests inject a small one)

    A daemon-wide path is written under the module lock, so the size check,
    the rotation and the append cannot interleave with another writer. Any
    other path keeps the plain append. Filesystem errors are raised to the
    caller, which owns its best-effort policy.
    """
    text = os.fspath(path)
    payload = line if line.endswith("\n") else line + "\n"
    if not is_daemon_debug_log(text):
        _append_unlocked(text, payload)
        return
    budget = ROTATION_MAX_BYTES if max_bytes is None else max_bytes
    count = ROTATION_GENERATIONS if generations is None else generations
    with _ROTATION_LOCK:
        _rotate_if_due(text, len(payload.encode("utf-8")), budget, count)
        _append_unlocked(text, payload)


def reset_debug_log(path) -> None:
    """Empty one debug log in place, preserving the numbered generations.

    This is the legacy truncate-before-first-log meaning of
    `_reset_reconcile_debug`: the current contents are discarded, the backups
    are left alone.  Daemon-wide paths truncate under the rotation lock so a
    concurrent writer cannot rotate the emptied file mid-reset.
    """
    text = os.fspath(path)
    if not is_daemon_debug_log(text):
        with open(text, "w", encoding="utf-8"):
            pass
        return
    with _ROTATION_LOCK:
        with open(text, "w", encoding="utf-8"):
            pass
