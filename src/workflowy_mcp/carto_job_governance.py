"""Dependency-light Cartographer region-governance and launch authority.

Phase A1 supplied durable-write, footprint and OS-lock primitives. CUT-2B keeps
admission and Q3 disk ownership in one in-process registry with memory-only
cooldowns. The shared admission transaction and CARTO_REFRESH launch authority
remain here. All default state derives from
``config.get_cartographer_jobs_dir()``; callers may inject a jobs root for
isolated tests.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import inspect
import json
import logging
import math
import ntpath
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4


from .config import get_cartographer_jobs_dir, get_server_config, merge_write_carto_job_status

LOGGER = logging.getLogger(__name__)


DEFAULT_MUTEX_WAIT_SECONDS = 10.0
DEFAULT_MUTEX_POLL_SECONDS = 0.05
DEFAULT_COOLDOWN_SECONDS = 60.0
CARTO_CANCEL_OUTCOMES = frozenset(
    {
        "cooperative",
        "already_terminal",
        "kill_refused_in_process_job",
    }
)

# Closed §18 registry. A runtime origin is never governed "by analogy": the
# static matrix fence locks the exact caller sets against these IDs.
PHASE_C_ORIGIN_MATRIX = {
    "L1": {
        "origin": "_handle_refresh_file_node:true_file",
        "work": "CARTO_REFRESH",
        "freshness": "PRE",
    },
    "L2": {"origin": "_handle_refresh_folder_node", "work": "CARTO_REFRESH", "freshness": "PRE"},
    "L3": {"origin": "_handle_refresh_file_node:ast", "work": "CARTO_REFRESH", "freshness": "B0"},
    "L4": {"origin": "_beacon_map_refresh_impl", "work": "CARTO_REFRESH", "freshness": "PRE"},
    "L5": {"origin": "cartographer_refresh_async", "work": "CARTO_REFRESH", "freshness": "PRE"},
    "L6": {
        "origin": "_handle_materialize_markdown_file",
        "work": "CARTO_REFRESH",
        "freshness": "B0",
    },
    "L7": {
        "origin": "_handle_bulk_materialize_markdown",
        "work": "CARTO_REFRESH",
        "freshness": "B0",
    },
    "L8": {
        "origin": "_handle_generate_markdown_file",
        "work": "CARTO_MARKDOWN_GENERATE",
        "freshness": "PRE",
    },
    "L9": {
        "origin": "_handle_bulk_generate_markdown",
        "work": "CARTO_MARKDOWN_GENERATE",
        "freshness": "GESTURE",
    },
    "L10": {
        "origin": "websocket_handler:bulk_apply_visible_nodes",
        "work": "CARTO_BULK_APPLY",
        "freshness": "PRE",
    },
    "Q1": {"origin": "_lightning_rod_mutate_beacons", "work": "Q3_LEASE", "freshness": "NONE"},
    "Q2": {"origin": "_beacon_sync_node_impl", "work": "Q3_LEASE", "freshness": "NONE"},
    "Q3": {"origin": "update_beacon_from_node", "work": "Q3_LEASE", "freshness": "NONE"},
    "Q4": {"origin": "_beacon_fix_impl", "work": "Q3_LEASE", "freshness": "NONE"},
    "N1": {
        "origin": "refresh_file_node_beacons:directory_reroute",
        "work": "NESTED",
        "freshness": "INHERIT",
    },
    "N2": {
        "origin": "refresh_folder_cartographer_sync:per_file",
        "work": "NESTED",
        "freshness": "INHERIT",
    },
    "N3": {
        "origin": "run_markdown_generate_pipeline:bulk_apply",
        "work": "NESTED",
        "freshness": "INHERIT",
    },
    "N4": {
        "origin": "_bulk_apply_per_file_inner_loop:reconcile",
        "work": "NESTED",
        "freshness": "INHERIT",
    },
    "N6": {"origin": "_handle_open_node_in_windsurf", "work": "READ_ONLY", "freshness": "NONE"},
    "R1": {"origin": "retired_file_sync", "work": "RETIRED", "freshness": "NONE"},
    "R2": {"origin": "retired_folder_sync", "work": "RETIRED", "freshness": "NONE"},
    "R3": {"origin": "retired_ast_sync", "work": "RETIRED", "freshness": "NONE"},
    "R4": {"origin": "retired_phase8_direct_spawn", "work": "RETIRED", "freshness": "NONE"},
    "R5": {"origin": "retired_writer_auto_refresh", "work": "RETIRED", "freshness": "NONE"},
}

TOMBSTONE_REQUIRED_FIELDS = frozenset(
    {
        "account_scope_id",
        "kind",
        "path",
        "root_uuid",
        "ancestor_chain",
        "dedupe_key",
        "last_terminal_at",
        "job_id",
    }
)

_HEX_32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_CARTO_JOB_TYPES = frozenset({"CARTO_REFRESH", "CARTO_MARKDOWN_GENERATE", "CARTO_BULK_APPLY"})
_SOURCE_REGIMES = frozenset({"standard", "persistent_new", "temp_override"})


class GovernanceError(RuntimeError):
    """Base class for loud, fail-closed governance failures."""

    code = "governance_error"

    def __init__(self, message: str, *, path: str | os.PathLike[str] | None = None):
        self.path = str(path) if path is not None else None
        super().__init__(message)


class AdmissionUnavailableError(GovernanceError):
    """The admission path or lock syscall is broken, not merely busy."""

    code = "admission_unavailable"


class AdmissionBusyTimeoutError(GovernanceError):
    """The admission lock remained routinely busy past its bounded wait."""

    code = "admission_busy_timeout"


class MutexWaitCancelledError(GovernanceError):
    """A cancellation probe stopped a synchronous or asynchronous lock wait."""

    code = "mutex_wait_cancelled"


class FootprintUnresolvableError(GovernanceError):
    """The current cache cannot supply a complete root-to-vault ancestor chain."""

    code = "footprint_unresolvable"


class FootprintComparisonError(GovernanceError):
    """Path normalization/comparison failed for a reason other than cross-drive."""

    code = "footprint_comparison_failed"


class GovernanceStateUnclassifiableError(GovernanceError):
    """A retained governance record cannot safely be decoded."""

    code = "governance_state_unclassifiable"


class GovernanceConflictError(GovernanceError):
    """An active file hold or typed region blocks ownership."""

    code = "overlap_conflict"

    def __init__(self, message: str, *, conflict: Mapping[str, Any] | None = None):
        self.conflict = dict(conflict or {})
        super().__init__(message)


class GovernanceCleanupError(GovernanceError):
    """Inert compatibility taxonomy for a governance cleanup refusal; nothing raises it."""

    code = "governance_cleanup_required"

    def __init__(self, message: str, *, artifact_paths: Sequence[str] = ()):
        self.artifact_paths = tuple(str(path) for path in artifact_paths)
        super().__init__(message, path=self.artifact_paths[0] if self.artifact_paths else None)


class Q3LeaseAcquisitionError(GovernanceError):
    """All-or-nothing Q3 lease creation failed before any source mutation."""

    code = "q3_lease_acquisition_failed"

    def __init__(
        self,
        message: str,
        *,
        created_paths: Sequence[str] = (),
        residual_paths: Sequence[str] = (),
    ):
        self.created_paths = tuple(created_paths)
        self.residual_paths = tuple(residual_paths)
        super().__init__(message)


@dataclass(frozen=True)
class GovernancePaths:
    """The one authoritative Cartographer jobs root; no artifact paths remain."""

    jobs_root: Path


@dataclass
class _HeldOSLock:
    path: Path
    handle: Any
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        try:
            _unlock_handle(self.handle)
        finally:
            try:
                self.handle.close()
            finally:
                self.released = True


# @beacon[
#   id=carto-governance@os-lock,
#   slice_labels=ra-carto-jobs,nexus-core-v1,
#   kind=ast,
#   comment=Cross-platform synchronous OS-lock authority for the lifetime single-writer guard only; per-attempt handles, errno-aware contention and Windows one-byte locking stay unchanged. Short transactions are in-process.,
# ]
def get_jobs_root(jobs_root: str | os.PathLike[str] | None = None) -> Path:
    """Return the exact Cartographer jobs root, never its runtime parent."""

    return Path(jobs_root) if jobs_root is not None else Path(get_cartographer_jobs_dir())


def governance_paths(jobs_root: str | os.PathLike[str] | None = None) -> GovernancePaths:
    """Return the one authoritative jobs root; no retired artifact path is built."""

    return GovernancePaths(jobs_root=get_jobs_root(jobs_root))


def _busy_errnos(platform_name: str | None = None) -> frozenset[int]:
    platform_name = os.name if platform_name is None else platform_name
    if platform_name == "nt":
        return frozenset(
            value
            for value in (
                errno.EACCES,
                getattr(errno, "EDEADLK", None),
                getattr(errno, "EWOULDBLOCK", None),
            )
            if value is not None
        )
    return frozenset({errno.EAGAIN, errno.EWOULDBLOCK})


def _is_busy_lock_error(exc: BaseException, platform_name: str | None = None) -> bool:
    return isinstance(exc, OSError) and exc.errno in _busy_errnos(platform_name)


def _open_lock_handle(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b", buffering=0)
    if os.name == "nt":
        # msvcrt byte-range locking cannot lock an empty file.  Every attempt
        # owns this handle, locks byte zero, unlocks byte zero, then closes it.
        handle.seek(0, os.SEEK_END)
        if handle.tell() < 1:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
    return handle


def _try_lock_handle(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _check_cancel(cancel_check: Callable[[], Any] | None, path: Path) -> None:
    if cancel_check is not None and cancel_check():
        raise MutexWaitCancelledError(
            f"Cancelled while waiting for governance mutex '{path}'.", path=path
        )


async def _check_cancel_async(cancel_check: Callable[[], Any] | None, path: Path) -> None:
    if cancel_check is None:
        return
    result = cancel_check()
    if inspect.isawaitable(result):
        result = await result
    if result:
        raise MutexWaitCancelledError(
            f"Cancelled while waiting for governance mutex '{path}'.", path=path
        )


def acquire_os_lock(
    path: str | os.PathLike[str],
    *,
    timeout_seconds: float = DEFAULT_MUTEX_WAIT_SECONDS,
    poll_seconds: float = DEFAULT_MUTEX_POLL_SECONDS,
    cancel_check: Callable[[], Any] | None = None,
) -> _HeldOSLock:
    """Acquire one OS mutex by bounded non-blocking synchronous polling."""

    lock_path = Path(path)
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        _check_cancel(cancel_check, lock_path)
        handle = None
        try:
            handle = _open_lock_handle(lock_path)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            # An open/create failure is always BROKEN.  EACCES is classified
            # as routine contention on Windows only when LK_NBLCK itself
            # raised it; conflating these stages would misreport an unwritable
            # governance directory as merely busy.
            raise AdmissionUnavailableError(
                f"Admission mutex is unavailable at '{lock_path}': {exc}",
                path=lock_path,
            ) from exc
        try:
            _try_lock_handle(handle)
            return _HeldOSLock(lock_path, handle)
        except BaseException as exc:
            try:
                handle.close()
            except Exception:
                pass
            if not _is_busy_lock_error(exc):
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise AdmissionUnavailableError(
                    f"Admission mutex is unavailable at '{lock_path}': {exc}",
                    path=lock_path,
                ) from exc
            if time.monotonic() >= deadline:
                raise AdmissionBusyTimeoutError(
                    f"Admission mutex remained busy past {timeout_seconds:g}s: '{lock_path}'.",
                    path=lock_path,
                ) from exc
            _check_cancel(cancel_check, lock_path)
            time.sleep(max(0.0, min(float(poll_seconds), deadline - time.monotonic())))


# @beacon-close[
#   id=carto-governance@os-lock,
# ]

_ADMISSION_LOCKS: dict[str, asyncio.Lock] = {}


# @beacon[
#   id=carto-governance@inprocess-admission-lock,
#   slice_labels=carto-governance,mci-lease-carto,ra-carto-jobs,
#   kind=ast,
#   comment=Short cancel-aware in-process admission serialization; the locked decision and registry updates run synchronously on the daemon loop, with no file ownership or network wait.,
# ]
@asynccontextmanager
async def admission_lock(
    jobs_root: str | os.PathLike[str] | None = None,
    *,
    timeout_seconds: float = DEFAULT_MUTEX_WAIT_SECONDS,
    poll_seconds: float = DEFAULT_MUTEX_POLL_SECONDS,
    cancel_check: Callable[[], Any] | None = None,
):
    """Serialize admission and Q3 registry transactions on the daemon loop.

    Acquisition is cancel-aware and bounded: an unlocked lock is taken without
    an intervening scheduling yield, otherwise the caller sleeps for the
    smaller of the existing poll interval and the remaining budget.  Zero
    timeout permits exactly the immediate attempt.  Native cancellation leaves
    no queued acquisition behind.
    """

    root = get_jobs_root(jobs_root)
    key = os.path.normcase(os.path.abspath(os.fspath(root)))
    lock = _ADMISSION_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ADMISSION_LOCKS[key] = lock
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        await _check_cancel_async(cancel_check, root)
        if not lock.locked():
            await lock.acquire()
            break
        if time.monotonic() >= deadline:
            raise AdmissionBusyTimeoutError(
                f"Admission mutex remained busy past {timeout_seconds:g}s: '{root}'.",
                path=root,
            )
        await _check_cancel_async(cancel_check, root)
        await asyncio.sleep(max(0.0, min(float(poll_seconds), deadline - time.monotonic())))
    try:
        yield
    finally:
        lock.release()


# @beacon[
#   id=carto-governance@identity-keys,
#   slice_labels=ra-carto-jobs,nexus-core-v1,
#   kind=ast,
#   comment=Canonical JSON-array hashing authority for account scopes, region/artifact identities, preparation digests, and the closed semantic CARTO dedupe key.,
# ]
def canonical_json_array(values: Sequence[Any]) -> str:
    """Encode governance hash ingredients by the one canonical array rule."""

    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _hash_array(values: Sequence[Any]) -> str:
    return hashlib.sha256(canonical_json_array(values).encode("utf-8")).hexdigest()


def normalize_api_base_url(raw_url: str) -> str:
    """Trim and normalize scheme/host/default-port/trailing slashes."""

    raw = str(raw_url or "").strip()
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.hostname:
        # Credentials are expected to carry an absolute API base URL.  Refuse
        # ambiguous strings rather than minting split coordination domains.
        raise ValueError(f"API base URL must be absolute: {raw!r}")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid API base URL port: {raw!r}") from exc
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and not (
        (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    ):
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, host, path, parsed.query, parsed.fragment))


def derive_account_scope_id(api_base_url: str, api_key: str) -> str:
    normalized_url = normalize_api_base_url(api_base_url)
    return _hash_array([normalized_url, str(api_key)])[:32]


def canonicalize_path(path: str | os.PathLike[str]) -> str:
    raw = os.path.expandvars(os.path.expanduser(str(path)))
    return os.path.normcase(os.path.realpath(os.path.abspath(raw)))


def derive_region_key(path: str | os.PathLike[str] | None, root_uuid: str) -> str:
    material = canonicalize_path(path) if path is not None else f"uuid:{root_uuid}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def derive_artifact_key(account_scope_id: str, region_key_value: str) -> str:
    return _hash_array([str(account_scope_id), str(region_key_value)])


def derive_preparation_input_digest(
    preparation_kind: Literal["ast_beacon_write", "materialize"],
    node_uuid: str,
    *payload_values: Any,
) -> str:
    if preparation_kind not in {"ast_beacon_write", "materialize"}:
        raise ValueError(f"Unsupported preparation kind: {preparation_kind!r}")
    return _hash_array([preparation_kind, str(node_uuid), *payload_values])


def derive_dedupe_key(
    *,
    account_scope_id: str,
    job_type: str,
    mode: str | None,
    root_uuid: str,
    source_regime: str = "standard",
    source_path_override: str | os.PathLike[str] | None = None,
    delete_temp_file_after: bool = False,
    preparation_input_digest: str | None = None,
) -> str:
    """Hash the closed semantic launch-intent ingredient list from §6.6."""

    job_type = str(job_type).upper()
    if job_type not in _CARTO_JOB_TYPES:
        raise ValueError(f"Unsupported CARTO job type: {job_type!r}")
    if job_type == "CARTO_REFRESH":
        normalized_mode = str(mode or "")
        if normalized_mode not in {"file", "folder"}:
            raise ValueError("CARTO_REFRESH mode must be 'file' or 'folder'.")
    elif job_type == "CARTO_BULK_APPLY":
        normalized_mode = str(mode or "")
        if normalized_mode not in {"visible", "all"}:
            raise ValueError("CARTO_BULK_APPLY mode must be 'visible' or 'all'.")
    else:
        normalized_mode = ""
    source_regime = str(source_regime)
    if source_regime not in _SOURCE_REGIMES:
        raise ValueError(f"Unsupported source_regime: {source_regime!r}")
    override = (
        canonicalize_path(source_path_override)
        if source_path_override is not None and str(source_path_override) != ""
        else ""
    )
    digest = str(preparation_input_digest or "")
    if digest:
        _validate_hex(digest, 64, "preparation_input_digest")
    ingredients = [
        str(account_scope_id),
        job_type,
        normalized_mode,
        str(root_uuid),
        source_regime,
        override,
        "true" if bool(delete_temp_file_after) else "false",
        digest,
    ]
    return _hash_array(ingredients)


# @beacon-close[
#   id=carto-governance@identity-keys,
# ]


def utc_now_iso(now_epoch: float | None = None) -> str:
    epoch = time.time() if now_epoch is None else float(now_epoch)
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _parse_time(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite timestamp")
        return parsed
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("missing timestamp")
    try:
        parsed = float(raw)
        if not math.isfinite(parsed):
            raise ValueError("non-finite timestamp")
        return parsed
    except ValueError:
        pass
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _node_parent_id(node: Mapping[str, Any]) -> str | None:
    parent = node.get("parent_id")
    if parent is None:
        parent = node.get("parentId")
    return str(parent) if parent not in (None, "") else None


# @beacon[
#   id=carto-governance@footprint-overlap,
#   slice_labels=ra-carto-jobs,nexus-core-v1,
#   kind=ast,
#   comment=Fail-closed cache ancestor-chain footprint builder plus symmetric machine-wide disk and same-account Workflowy-graph overlap predicate, including explicit cross-drive non-overlap.,
# ]
def compute_footprint(
    path_or_none: str | os.PathLike[str] | None,
    root_uuid: str,
    cache_by_id: Mapping[str, Mapping[str, Any]],
    *,
    kind: Literal["file", "folder"] | None = None,
) -> dict[str, Any]:
    """Compute a canonical path/graph footprint or fail closed."""

    root = str(root_uuid or "")
    if not root or root not in cache_by_id:
        raise FootprintUnresolvableError(
            f"Cannot resolve footprint for root_uuid={root!r}; run system.cache.refresh and retry."
        )
    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = root
    while current is not None:
        if current in seen:
            raise FootprintUnresolvableError(
                f"Ancestor cycle while resolving root_uuid={root!r}; run system.cache.refresh and retry."
            )
        node = cache_by_id.get(current)
        if not isinstance(node, Mapping):
            raise FootprintUnresolvableError(
                f"Missing ancestor {current!r} for root_uuid={root!r}; run system.cache.refresh and retry."
            )
        seen.add(current)
        chain.append(current)
        current = _node_parent_id(node)
    canonical_path = canonicalize_path(path_or_none) if path_or_none is not None else None
    resolved_kind = kind
    if resolved_kind is None:
        resolved_kind = "folder" if canonical_path and os.path.isdir(canonical_path) else "file"
    if resolved_kind not in {"file", "folder"}:
        raise ValueError("Footprint kind must be 'file' or 'folder'.")
    return {
        "kind": resolved_kind,
        "path": canonical_path,
        "root_uuid": root,
        "ancestor_chain": chain,
    }


def validate_footprint(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("kind") not in {"file", "folder"}:
        raise ValueError("invalid footprint kind")
    path = value.get("path")
    if path is not None and (
        not isinstance(path, str) or not (os.path.isabs(path) or ntpath.isabs(path))
    ):
        raise ValueError("footprint path must be canonical absolute text or null")
    root = value.get("root_uuid")
    chain = value.get("ancestor_chain")
    if not isinstance(root, str) or not root:
        raise ValueError("missing footprint root_uuid")
    if not isinstance(chain, list) or not chain or not all(isinstance(x, str) and x for x in chain):
        raise ValueError("invalid footprint ancestor_chain")
    if root not in chain:
        raise ValueError("footprint ancestor_chain must contain root_uuid")
    return {
        "kind": value["kind"],
        "path": path,
        "root_uuid": root,
        "ancestor_chain": list(chain),
    }


def _looks_windows_path(path: str) -> bool:
    drive, _ = ntpath.splitdrive(path)
    return bool(drive) or path.startswith("\\\\")


def _paths_overlap(path_a: str, path_b: str) -> bool:
    path_module = ntpath if _looks_windows_path(path_a) or _looks_windows_path(path_b) else os.path
    a = path_module.normcase(path_module.normpath(path_a))
    b = path_module.normcase(path_module.normpath(path_b))
    try:
        common = path_module.commonpath([a, b])
    except ValueError as exc:
        drive_a = path_module.splitdrive(a)[0]
        drive_b = path_module.splitdrive(b)[0]
        if drive_a and drive_b and path_module.normcase(drive_a) != path_module.normcase(drive_b):
            return False
        raise FootprintComparisonError(
            f"Cannot compare governance paths {path_a!r} and {path_b!r}: {exc}"
        ) from exc
    except Exception as exc:
        raise FootprintComparisonError(
            f"Cannot compare governance paths {path_a!r} and {path_b!r}: {exc}"
        ) from exc
    return common == a or common == b


def footprints_overlap(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Return the symmetric §6.2 overlap decision for two full records."""

    fa = validate_footprint(a)
    fb = validate_footprint(b)
    if fa["path"] is not None and fb["path"] is not None:
        if _paths_overlap(fa["path"], fb["path"]):
            return True
    if str(a.get("account_scope_id") or "") == str(b.get("account_scope_id") or ""):
        a_nodes = set(fa["ancestor_chain"]) | {fa["root_uuid"]}
        b_nodes = set(fb["ancestor_chain"]) | {fb["root_uuid"]}
        if fa["root_uuid"] in b_nodes or fb["root_uuid"] in a_nodes:
            return True
    return False


# @beacon-close[
#   id=carto-governance@footprint-overlap,
# ]


def _validate_hex(value: Any, length: int, label: str) -> str:
    raw = str(value or "")
    matcher = _HEX_32_RE if length == 32 else _HEX_64_RE
    if not matcher.fullmatch(raw):
        raise ValueError(f"{label} must be {length} lowercase hex characters")
    return raw


def _validate_safe_component(value: Any, label: str) -> str:
    raw = str(value or "")
    if not raw or not _SAFE_COMPONENT_RE.fullmatch(raw):
        raise ValueError(f"{label} is missing or unsafe")
    return raw


def validate_tombstone(value: Mapping[str, Any]) -> dict[str, Any]:
    missing = TOMBSTONE_REQUIRED_FIELDS - set(value)
    if missing:
        raise ValueError(f"tombstone missing required fields: {sorted(missing)}")
    tombstone = dict(value)
    _validate_hex(tombstone["account_scope_id"], 32, "account_scope_id")
    _validate_hex(tombstone["dedupe_key"], 64, "dedupe_key")
    _validate_safe_component(tombstone["job_id"], "job_id")
    validate_footprint(tombstone)
    _parse_time(tombstone["last_terminal_at"])
    return tombstone


# @beacon[
#   id=carto-governance@durable-writer,
#   slice_labels=ra-carto-jobs,nexus-core-v1,carto-governance,mci-epoch-paths,
#   kind=ast,
#   comment=THE one replace retry loop lives in _replace_with_retry and is shared by atomic_write_text and the job-status merge writer. atomic_write_text owns temp + flush + fsync + cleanup and optional directory sync; atomic_write_json is its compact JSON front. Existing DurableFileOps seams and the two-argument defaults stay unchanged; never an in-place fallback.,
# ]
@dataclass(frozen=True)
class DurableFileOps:
    """Injectable filesystem operations for the durable write loop in :func:`atomic_write_text` (W2).

    Every operation defaults to the real OS primitive.  Fault tests wrap or
    replace individual operations to inject failures at named boundaries.
    """

    write_fsync: Callable[[Path, str], None] | None = None
    replace: Callable[[Path, Path], None] | None = None
    dir_sync: Callable[[Path], None] | None = None
    mkdir: Callable[[Path], None] | None = None
    unlink: Callable[[Path], None] | None = None
    sleep: Callable[[float], None] | None = None
    monotonic: Callable[[], float] | None = None
    tmp_name: Callable[[Path], Path] | None = None


def _default_write_fsync(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _default_replace(tmp: Path, target: Path) -> None:
    os.replace(tmp, target)


def _default_dir_sync(directory: Path) -> None:
    """Best-effort parent-directory durability sync.

    POSIX: fsync the directory fd so the rename itself is durable.  Windows:
    directory fsync is not supported by the OS/Python; the file-level fsync
    plus atomic replace remains the durability contract there.  Failures are
    deliberately swallowed: this step is best-effort hardening on top of the
    fsynced file, never a new failure mode for an otherwise successful
    publication.
    """

    if os.name == "nt":
        return
    fd: int | None = None
    try:
        fd = os.open(str(directory), os.O_RDONLY)
        os.fsync(fd)
    except OSError:
        pass
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _default_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _default_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _default_tmp_name(target: Path) -> Path:
    return target.with_name(f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp")


def _resolve_durable_ops(fs_ops: DurableFileOps | None) -> dict[str, Any]:
    ops = fs_ops or DurableFileOps()
    return {
        "write_fsync": ops.write_fsync or _default_write_fsync,
        "replace": ops.replace or _default_replace,
        "dir_sync": ops.dir_sync or _default_dir_sync,
        "mkdir": ops.mkdir or _default_mkdir,
        "unlink": ops.unlink or _default_unlink,
        "sleep": ops.sleep or time.sleep,
        "monotonic": ops.monotonic or time.monotonic,
        "tmp_name": ops.tmp_name or _default_tmp_name,
    }


def _replace_with_retry(tmp: Path, target: Path, *, ops: Mapping[str, Any] | None = None) -> None:
    """Replace one already-written temp using the shared Windows collision policy.

    The caller owns writing and cleanup. Resolved durable ops pass through
    unchanged; other callers get the same defaults, with replace looked up at
    call time by _default_replace. No write, fsync or in-place fallback here.
    """
    if ops is None:
        ops = _resolve_durable_ops(None)
    replace_deadline = ops["monotonic"]() + 2.0
    while True:
        try:
            ops["replace"](tmp, target)
            break
        except PermissionError:
            # Windows readers do not necessarily open with FILE_SHARE_DELETE,
            # so a lock-free scanner can briefly deny replacement while it
            # has the old complete file open. Retry the atomic replace only;
            # never expose torn text with an in-place fallback.
            if ops["monotonic"]() >= replace_deadline:
                raise
            ops["sleep"](0.005)


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    fs_ops: DurableFileOps | None = None,
    sync_parent_dir: bool = False,
) -> Path:
    """Atomically replace one text artifact for lock-free readers.

    THE one durable temp-write-fsync-replace loop: same-directory unique
    temp naming, flush+fsync, bounded Windows sharing retry, own-temp
    cleanup, never an in-place fallback.  W2 extension (in place, never a
    rewrite): optional injectable seams (``fs_ops``) for fault-injection
    tests and an opt-in best-effort parent-directory durability sync
    (``sync_parent_dir``; POSIX fsync-dir, documented no-op on Windows).

    ``text`` must be a ``str`` (a non-str raises TypeError before the
    directory is created).  :func:`atomic_write_json` is this loop's JSON
    front; callers with a different serialization hand the finished text
    here directly. The replace-only retry is shared through
    :func:`_replace_with_retry`; this function retains temp and fsync ownership.
    """

    if not isinstance(text, str):
        raise TypeError(
            f"atomic_write_text text must be str, got {type(text).__name__}"
        )
    target = Path(path)
    ops = _resolve_durable_ops(fs_ops)
    ops["mkdir"](target.parent)
    tmp = ops["tmp_name"](target)
    try:
        ops["write_fsync"](tmp, text)
        _replace_with_retry(tmp, target, ops=ops)
        if sync_parent_dir:
            ops["dir_sync"](target.parent)
        return target
    finally:
        try:
            ops["unlink"](tmp)
        except Exception:
            pass


def atomic_write_json(
    path: str | os.PathLike[str],
    payload: Mapping[str, Any],
    *,
    fs_ops: DurableFileOps | None = None,
    sync_parent_dir: bool = False,
) -> Path:
    """Atomically replace one JSON artifact for lock-free readers.

    The durable loop lives in :func:`atomic_write_text`; this function is
    its JSON front: same-directory unique temp naming, flush+fsync,
    bounded Windows sharing retry, own-temp cleanup, never an in-place
    fallback, with compact separators and no trailing newline -- the
    historical bytes, unchanged.  ``fs_ops`` and ``sync_parent_dir`` pass
    through unchanged.
    """

    text = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
    return atomic_write_text(
        path, text, fs_ops=fs_ops, sync_parent_dir=sync_parent_dir
    )


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("JSON artifact must contain an object")
    return value


def build_tombstone_from_claim(
    claim: Mapping[str, Any], *, now_epoch: float | None = None
) -> dict[str, Any]:
    """Project a live registry hold into the memory-history cooldown record."""

    return validate_tombstone(
        {
            "account_scope_id": claim["account_scope_id"],
            "kind": claim["kind"],
            "path": claim.get("path"),
            "root_uuid": claim["root_uuid"],
            "ancestor_chain": list(claim.get("ancestor_chain") or ()),
            "dedupe_key": claim["dedupe_key"],
            "last_terminal_at": utc_now_iso(now_epoch),
            "job_id": claim["job_id"],
        }
    )


def _emit(
    result: Any | None, message: str, diagnostic: Callable[[str], Any] | None
) -> None:
    if result is not None:
        result.diagnostics.append(message)
    if diagnostic is not None:
        diagnostic(message)
    else:
        LOGGER.warning(message)


def _read_job_payload(path: str | os.PathLike[str]) -> dict[str, Any]:
    value = _read_json(Path(path))
    if not isinstance(value, dict):
        raise GovernanceStateUnclassifiableError(
            f"CARTO job JSON is not an object: {path}", path=path
        )
    return value


async def finalize_cancelled_job_claim_async(
    job_file: str | os.PathLike[str],
    claim_id: str,
    *,
    jobs_root: str | os.PathLike[str] | None = None,
    restart_boundary_stop: bool = False,
    diagnostic: Callable[[str], Any] | None = None,
) -> str:
    """Finalize the runner's observed user/restart stop once, max-merge memory
    cooldown before release, and leave a bound graph grant to its existing
    outer finally. No artifact write, wait or cleanup supervisor.
    """

    root = get_jobs_root(jobs_root or Path(job_file).parent)
    await finalize_claim_async(claim_id, root, diagnostic=diagnostic)
    return "cancelled"


def _write_task_cancellation_request(path, job):
    """Merge loop-owned task progress and the cancelling status; no sentinel."""
    try:
        cancelling = _read_job_payload(path)
    except Exception:
        cancelling = {}
    progress = {**(cancelling.get("progress") if isinstance(cancelling.get("progress"), Mapping) else {}),
                **(job.get("progress") if isinstance(job.get("progress"), Mapping) else {})}
    cancelling.update(job)
    if progress:
        cancelling["progress"] = progress
    cancelling["status"] = "cancelling"
    cancelling["updated_at"] = utc_now_iso()
    merge_write_carto_job_status(path, cancelling)


# @beacon[
#   id=carto-governance@cancellation-controller,
#   slice_labels=carto-governance,ra-carto-jobs,ra-reconcile,ra-workflowy-cache,
#   kind=ast,
#   comment=Loop-owned cooperative cancellation uses actual task completion and the current control before joined presentation writes. The existing response window may report pending; no sentinel, PID check or process escalation.,
# ]
async def cancel_carto_job(
    job_file: str | os.PathLike[str],
    *,
    requested_by: str,
    jobs_root: str | os.PathLike[str] | None = None,
    task_cancel: Callable[[], Any] | None = None,
    task_done: Callable[[], bool] | None = None,
    task_job: Mapping[str, Any] | None = None,
    cooperative_timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.1,
    sleep_fn: Callable[[float], Any] = asyncio.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    diagnostic: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Cancel one registered daemon task; presentation is never stop truth."""

    path = Path(job_file)
    job = dict(task_job) if task_job is not None else {}
    if task_job is None:
        try:
            job = _read_job_payload(path)
        except Exception:
            job = {}
    job_id = str(job.get("id") or path.stem)
    if task_done is None or task_done():
        return {"success": True, "job_id": job_id, "outcome": "already_terminal"}

    # The loop-owned control request precedes every joined presentation write.
    try:
        if task_cancel is not None:
            task_cancel()
    except Exception as exc:
        _emit(
            None, f"In-process cancellation delivery failed for '{job_id}': {exc}", diagnostic
        )
    from .carto_job_runtime import run_carto_cpu

    await run_carto_cpu("cancel-task-request", _write_task_cancellation_request, path, job)

    deadline = monotonic_fn() + max(0.0, float(cooperative_timeout_seconds))
    while not task_done():
        if monotonic_fn() >= deadline:
            return {
                "success": True, "job_id": job_id, "outcome": "kill_refused_in_process_job",
                "status": "cancelling", "cancellation_requested": True,
                "cancellation_pending": True,
                "message": "Cancellation requested; the daemon task will stop at its next safe point. No process was killed. Poll system.job.status.",
            }
        awaited = sleep_fn(float(poll_interval_seconds))
        if inspect.isawaitable(awaited):
            await awaited
    return {"success": True, "job_id": job_id, "outcome": "cooperative"}


# @beacon[
#   id=carto-governance@q3-leases,
#   slice_labels=ra-carto-jobs,nexus-core-v1,carto-governance,mci-lease-carto,
#   kind=ast,
#   comment=All-or-none disk-footprint admission under the short in-process admission lock; validates every target and checks symmetric CARTO/Q3 conflicts before inserting any entry in the one registry.,
# ]
def _acquire_q3_leases_impl(
    targets: Iterable[Mapping[str, Any]],
    *,
    account_scope_id: str,
    jobs_root: str | os.PathLike[str] | None = None,
    lease_id: str | None = None,
) -> list[dict[str, Any]]:
    """Insert every Q3 file hold in the one registry, or nothing at all."""

    materialized = list(targets)
    if not materialized:
        return []
    from .mutation_locks import _acquire_q3_holds

    receipts = _acquire_q3_holds(
        materialized,
        account_scope_id=account_scope_id,
        jobs_root=get_jobs_root(jobs_root),
        lease_id=lease_id,
    )
    return [dict(receipt) for receipt in receipts]


# @beacon[
#   id=carto-governance@q3-async-acquire,
#   slice_labels=carto-governance,ra-carto-jobs,nexus-core-v1,mci-lease-carto,
#   kind=ast,
#   comment=All-or-none disk-footprint admission under the short in-process admission lock; validates every target and checks symmetric CARTO/Q3 conflicts before inserting any entry in the one registry.,
# ]
async def acquire_q3_leases_async(
    targets: Iterable[Mapping[str, Any]],
    *,
    account_scope_id: str,
    jobs_root: str | os.PathLike[str] | None = None,
    lease_id: str | None = None,
    mutex_wait_seconds: float = DEFAULT_MUTEX_WAIT_SECONDS,
    cancel_check: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    """Acquire Q3 file holds without synchronous polling on the event loop."""

    materialized = list(targets)
    if not materialized:
        return []
    root = get_jobs_root(jobs_root)
    async with admission_lock(
        root,
        timeout_seconds=mutex_wait_seconds,
        cancel_check=cancel_check,
    ):
        return _acquire_q3_leases_impl(
            materialized,
            account_scope_id=account_scope_id,
            jobs_root=root,
            lease_id=lease_id,
        )


# @beacon[
#   id=carto-governance@q3-release-recovery,
#   slice_labels=carto-governance,ra-carto-jobs,nexus-core-v1,
#   kind=ast,
#   comment=Exact-acquisition Q3 memory release after joined disk work; idempotent, no cooldown, file unlink, retry supervisor or ownership expiry.,
# ]
async def release_q3_leases_async(
    leases: Iterable[Mapping[str, Any]],
    *,
    jobs_root: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Release exactly the receipts this acquisition produced; never suspend."""

    materialized = [dict(lease) for lease in leases]
    if not materialized:
        return []
    from .mutation_locks import _release_q3_holds

    _release_q3_holds(materialized, jobs_root=get_jobs_root(jobs_root))
    return []


# @beacon[
#   id=carto-governance@claim-finalize-recovery,
#   slice_labels=carto-governance,ra-carto-jobs,nexus-core-v1,mci-lease-carto,
#   kind=ast,
#   comment=Finalize the matching job reservation once, max-merge memory cooldown before release, and leave a bound graph grant to its existing outer finally. No artifact write, wait or cleanup supervisor.,
# ]
async def finalize_claim_async(
    claim_id: str,
    jobs_root: str | os.PathLike[str] | None = None,
    *,
    write_tombstone: bool = True,
    now_epoch: float | None = None,
    diagnostic: Callable[[str], Any] | None = None,
) -> bool:
    """Finalize the exact reservation in memory; absence is successful idempotence."""

    from .mutation_locks import _finalize_carto_hold

    return _finalize_carto_hold(
        claim_id,
        get_jobs_root(jobs_root),
        write_tombstone=write_tombstone,
        now_epoch=now_epoch,
    )


class AdmissionRefusal(GovernanceError):
    """Structured, fail-closed CARTO admission refusal."""

    def __init__(self, kind: str, message: str, **details: Any):
        self.kind = str(kind)
        self.details = dict(details)
        super().__init__(message, path=details.get("path"))

    def to_result(self) -> dict[str, Any]:
        return {
            "success": False,
            "error_kind": self.kind,
            "error": str(self),
            **self.details,
        }


def _job_ack_from_claim(
    claim: Mapping[str, Any],
    paths: GovernancePaths,
    *,
    deduplicated: bool,
) -> dict[str, Any]:
    """Build the unchanged J-consumed ack from the reservation projection."""

    job_path = Path(claim["job_file"]) if claim.get("job_file") else None
    payload: dict[str, Any] = {}
    if job_path is not None:
        try:
            payload = _read_json(job_path)
        except Exception:
            payload = {}
    log_file = claim.get("log_file") or payload.get("logs_path")
    freshness = claim.get("freshness_disposition")
    if freshness is None:
        freshness = payload.get("freshness_disposition")
    return {
        "success": True,
        "job_id": claim["job_id"],
        "pid": payload.get("pid"),
        "detached": False,
        "deduplicated": bool(deduplicated),
        "root_uuid": claim["root_uuid"],
        "job_file": str(job_path) if job_path is not None else None,
        "log_file": log_file,
        "freshness_disposition": freshness,
        "note": (
            "Existing active governed job reused; no second preparation ran."
            if deduplicated
            else "Governed CARTO job admitted."
        ),
    }


def _hold_footprint(hold: Mapping[str, Any]) -> dict[str, Any]:
    """Project one registry hold into the validated footprint mapping shape."""

    return {
        "kind": hold.get("kind"),
        "path": hold.get("path"),
        "root_uuid": hold.get("root_uuid"),
        "ancestor_chain": list(hold.get("ancestor_chain") or ()),
    }


def _configured_cooldown_seconds() -> float:
    """Read the existing admission cooldown field with its historical fallback."""

    try:
        return float(get_server_config().admission.cooldown_seconds)
    except Exception:
        return DEFAULT_COOLDOWN_SECONDS


# @beacon[
#   id=carto-governance@admission-authority,
#   slice_labels=carto-governance,ra-carto-jobs,ra-reconcile,ra-workflowy-cache,
#   kind=ast,
#   comment=Negative-only exact-intent dedupe/cooldown before freshness; the full synchronous admission decision consults the one registry, current topology and non-owning cooldown history, never a disk sweep.,
# ]
def negative_exact_intent_gate_locked(
    *,
    dedupe_key: str,
    jobs_root: str | os.PathLike[str] | None = None,
    now_epoch: float | None = None,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
) -> dict[str, Any] | None:
    """Return only a predetermined negative outcome; never admit or overlap-decide."""

    paths = governance_paths(jobs_root)
    now = time.time() if now_epoch is None else float(now_epoch)
    from .mutation_locks import _governance_cooldowns, _governance_holds

    for hold in _governance_holds(paths.jobs_root):
        if (
            hold.get("record_kind") == "carto"
            and not hold.get("finalized")
            and hold.get("dedupe_key") == dedupe_key
        ):
            return _job_ack_from_claim(hold, paths, deduplicated=True)
    for record in _governance_cooldowns(
        paths.jobs_root, now_epoch=now, cooldown_seconds=cooldown_seconds
    ):
        if record.get("dedupe_key") != dedupe_key:
            continue
        elapsed = max(0.0, now - _parse_time(record["last_terminal_at"]))
        retry_after = max(0.0, float(cooldown_seconds) - elapsed)
        return {
            "success": False,
            "error_kind": "cooldown_active",
            "error": "An identical CARTO intent is inside the post-terminal cooldown.",
            "retry_after": retry_after,
            "conflict": dict(record),
        }
    return None


async def negative_exact_intent_gate(
    *,
    dedupe_key: str,
    jobs_root: str | os.PathLike[str] | None = None,
    now_epoch: float | None = None,
    cooldown_seconds: float | None = None,
    mutex_wait_seconds: float = DEFAULT_MUTEX_WAIT_SECONDS,
    cancel_check: Callable[[], Any] | None = None,
) -> dict[str, Any] | None:
    root = get_jobs_root(jobs_root)
    resolved_cooldown = float(
        _configured_cooldown_seconds()
        if cooldown_seconds is None
        else cooldown_seconds
    )
    async with admission_lock(
        root,
        timeout_seconds=mutex_wait_seconds,
        cancel_check=cancel_check,
    ):
        return negative_exact_intent_gate_locked(
            dedupe_key=dedupe_key,
            jobs_root=root,
            now_epoch=now_epoch,
            cooldown_seconds=resolved_cooldown,
        )


# @beacon[
#   id=auto-beacon@admit_carto_job_locked-5te7,
#   role=admit_carto_job_locked,
#   slice_labels=mci-lease-carto,
#   kind=ast,
#   comment=One synchronous current-view typed consult plus symmetric active file-footprint exclusion and configured cooldown. Admission reserves before its one injectable atomic initial write; FILE FOLDER bulk and Markdown jobs seed their sole summary-head/2 writer without changing rollback or reservation release.,
# ]
def admit_carto_job_locked(
    *,
    job_file: str | os.PathLike[str],
    job_payload: Mapping[str, Any],
    job_type: str,
    dedupe_key: str,
    account_scope_id: str,
    footprint: Mapping[str, Any],
    launcher: str,
    parent_job_id: str | None,
    prepared: bool,
    jobs_root: str | os.PathLike[str] | None = None,
    now_epoch: float | None = None,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    atomic_writer: Callable[[str | os.PathLike[str], Mapping[str, Any]], Any] = atomic_write_json,
    footprint_basis_sequence: int | None = None,
    footprint_basis_epoch_id: str | None = None,
    live_mutation_lease_view: Mapping[str, Any] | None = None,
    mutation_lease_retry_after_seconds: float = 1.0,
) -> dict[str, Any]:
    """Run the complete admission decision while the admission lock is held.

    Exact active dedupe runs first, then the kept head-currency gate, then the
    symmetric active file-footprint exclusion, then the one synchronous typed
    held-region consult.  In-memory cooldown is checked only for candidates
    still alive, after which the same registry entry is reserved and the
    initial job JSON is written. FILE/FOLDER and bulk seed their one /2 writer
    without an extra write, including the one-file Markdown pipeline.
    This remains an admission decision, with no job lifetime hold.
    """

    paths = governance_paths(jobs_root)
    now = time.time() if now_epoch is None else float(now_epoch)
    normalized_footprint = validate_footprint(footprint)
    from .mutation_locks import (
        _governance_cooldowns,
        _governance_holds,
        _long_lived_retry_after_seconds,
        _reserve_carto_hold,
    )

    holds = _governance_holds(paths.jobs_root)
    for hold in holds:
        if (
            hold.get("record_kind") == "carto"
            and not hold.get("finalized")
            and hold.get("dedupe_key") == dedupe_key
        ):
            return _job_ack_from_claim(hold, paths, deduplicated=True)

    # S05 (A1c): the head-currency validity invariant — the overlap decision
    # must not rest on a topology basis older than THIS admission.  The
    # manifest read is lock-free (never a nested coherence mutex under the
    # admission lock, invariant 12).
    if footprint_basis_sequence is not None:
        from .coherence.paths import coherence_paths as _coherence_paths
        from .coherence.schema import read_manifest as _read_coherence_manifest

        _coh = _coherence_paths(account_scope_id, jobs_root=paths.jobs_root)
        _manifest = _read_coherence_manifest(_coh, required=False)
        if _manifest is not None and (
            (
                footprint_basis_epoch_id is not None
                and _manifest.epoch_id != footprint_basis_epoch_id
            )
            or _manifest.journal_head_seq > int(footprint_basis_sequence)
        ):
            raise AdmissionRefusal(
                "head_basis_stale",
                "The manifest journal head advanced past the CARTO footprint's "
                "basis view; adopt the head and re-derive the footprint before "
                "retrying (S05 A1c head-currency).",
                retry_after=0.0,
                conflict={
                    "current_epoch_id": _manifest.epoch_id,
                    "current_head": _manifest.journal_head_seq,
                    "basis_epoch_id": footprint_basis_epoch_id,
                    "basis_sequence": footprint_basis_sequence,
                },
            )

    candidate = {"account_scope_id": account_scope_id, **normalized_footprint}
    for hold in holds:
        active = {"account_scope_id": hold.get("account_scope_id"), **_hold_footprint(hold)}
        if not footprints_overlap(candidate, active):
            continue
        owner = hold.get("job_id") or hold.get("lease_id")
        message = (
            f"Active owner '{owner}' overlaps "
            f"'{normalized_footprint.get('path') or normalized_footprint['root_uuid']}'."
        )
        if hold.get("record_kind") == "carto":
            if job_payload.get("preparation_input_digest"):
                message += (
                    " Your latest name/note changes were NOT synchronized; the running "
                    "disk-authoritative refresh may overwrite current Workflowy edits. "
                    "Verify after terminal and retry; use F12+2 Bulk Apply for several nodes."
                )
            advice = _long_lived_retry_after_seconds()
            message += (
                f" Retry after about {advice:g}s; check system.job.status because the job may still be running."
            )
        else:
            advice = None
        raise AdmissionRefusal("overlap_conflict", message, retry_after=advice, conflict=dict(hold))

    # One synchronous typed consult; no continuation co-claim exempts it.
    from .mutation_locks import registry_held_regions

    held = registry_held_regions()
    if held.regions:
        from .mutation_lease_governance import (
            MutationRegionError,
            ReservedMutationParent,
            mutation_regions_intersect_carto_claim,
            rehydrate_ancestor_chain,
            region_display_key,
        )

        regions = tuple(r for r in held.regions if r.kind != "ap_core" and not isinstance(r.key, ReservedMutationParent))
    else:
        regions = ()
    if regions:
        if live_mutation_lease_view is None:
            raise AdmissionRefusal(
                "governance_state_unclassifiable",
                "Held mutation-lock keys exist but no current adopted view was supplied "
                "for chain rehydration; refusing to admit on unproved topology. "
                "Retry with a current admission view.",
            )
        if not held.authority_consistent or held.view is None:
            raise AdmissionRefusal(
                "governance_state_unclassifiable",
                "Held mutation regions have no single current cache authority; retry with the daemon's current admission view.",
            )
        current_view = held.view.nodes_by_id
        candidate_display = normalized_footprint.get("path") or normalized_footprint["root_uuid"]
        for region in regions:
            key, display = region.key, region_display_key(region)
            try:
                rehydrate_ancestor_chain(key, current_view)
            except MutationRegionError:
                message = (
                    f"Held mutation lock '{display}' cannot be placed in the current "
                    f"admission view for CARTO candidate '{candidate_display}'; retry shortly (refuse, never queue)."
                    if region.kind == "exact_node" else
                    f"Held mutation region '{region.kind}:{display}' cannot be placed in the current admission view "
                    f"for CARTO candidate '{candidate_display}'; retry shortly (refuse, never queue)."
                )
                raise AdmissionRefusal(
                    "overlap_conflict",
                    message,
                    retry_after=float(mutation_lease_retry_after_seconds),
                    conflict={
                        "owner_kind": "mutation_lock", "node_id": key, "region": None,
                        "reason": "held_key_not_in_admission_view",
                    },
                )
        region = mutation_regions_intersect_carto_claim(
            regions, candidate, cache_by_id=current_view
        )
        if region is not None:
            display = region_display_key(region)
            message = (
                f"Active mutation lock '{display}' overlaps CARTO candidate '{candidate_display}'; retry shortly (refuse, never queue)."
                if region.kind == "exact_node" else
                f"Active mutation region '{region.kind}:{display}' overlaps CARTO candidate '{candidate_display}'; retry shortly (refuse, never queue)."
            )
            raise AdmissionRefusal(
                "overlap_conflict",
                message,
                retry_after=float(mutation_lease_retry_after_seconds),
                conflict={
                    "owner_kind": "mutation_lock", "node_id": region.key,
                    "region": region.to_dict(),
                },
            )

    for record in _governance_cooldowns(
        paths.jobs_root, now_epoch=now, cooldown_seconds=cooldown_seconds
    ):
        active = {"account_scope_id": record.get("account_scope_id"), **_hold_footprint(record)}
        if not footprints_overlap(candidate, active):
            continue
        elapsed = max(0.0, now - _parse_time(record["last_terminal_at"]))
        retry_after = max(0.0, float(cooldown_seconds) - elapsed)
        raise AdmissionRefusal(
            "cooldown_active",
            "The target overlaps a recently-terminal CARTO region; retry after cooldown. "
            "For several node beacon edits, use F12+2 Bulk Apply.",
            retry_after=retry_after,
            conflict=dict(record),
        )

    initial_state = "preparing"
    reservation = _reserve_carto_hold(
        jobs_root=paths.jobs_root,
        account_scope_id=account_scope_id,
        footprint=normalized_footprint,
        job_id=str(job_payload["id"]),
        job_file=str(job_file),
        job_type=job_type,
        mode=str(job_payload.get("mode") or ""),
        dedupe_key=dedupe_key,
        launcher=str(launcher),
        parent_job_id=parent_job_id,
        log_file=str(job_payload.get("logs_path")) if job_payload.get("logs_path") else None,
        freshness_disposition=(
            str(job_payload["freshness_disposition"])
            if job_payload.get("freshness_disposition") is not None
            else None
        ),
    )
    claim = {**reservation, "ancestor_chain": list(reservation["ancestor_chain"])}
    payload = dict(job_payload)
    payload.update(
        {
            "claim_id": claim["claim_id"],
            "dedupe_key": dedupe_key,
            "account_scope_id": account_scope_id,
            "footprint": normalized_footprint,
            "launcher": launcher,
            "parent_job_id": parent_job_id,
            "status": initial_state,
            "handoff_state": initial_state,
            "execution_model": "daemon_task",
            "execution_owner": "launcher",
            "detached": False,
            "pid": os.getpid(),
        }
    )
    try:
        if (job_type == "CARTO_REFRESH" and str(payload.get("mode") or "").lower() in {"file", "folder"}) or job_type in {"CARTO_BULK_APPLY", "CARTO_MARKDOWN_GENERATE"}:
            from .carto_job_record import record_for
            payload = record_for(job_file).initial_payload(payload)
        atomic_writer(job_file, payload)
    except BaseException as exc:
        target = normalized_footprint.get("path") or normalized_footprint["root_uuid"]
        primary_text = (
            f"Initial CARTO job JSON write failed after admission reservation for "
            f"claim_id={claim['claim_id']} at {job_file}; affected region={target!r}: {exc}"
        )
        try:
            from .mutation_locks import _finalize_carto_hold

            _finalize_carto_hold(
                str(claim["claim_id"]),
                paths.jobs_root,
                write_tombstone=True,
                now_epoch=now,
            )
        except Exception:
            LOGGER.exception(
                "Exact reservation rollback failed after initial CARTO job JSON write failure: %s",
                claim["claim_id"],
            )
        try:
            if Path(job_file).exists():
                _persist_terminal_job(Path(job_file), "failed", primary_text)
        except Exception:
            LOGGER.exception(
                "Best-effort failed-job mark could not be persisted at %s", job_file
            )
        from .carto_job_record import release_carto_job_record
        release_carto_job_record(job_file)
        raise
    return {
        "success": True,
        "admitted": True,
        "deduplicated": False,
        "claim": claim,
        "claim_path": None,
        "job_file": str(job_file),
        "job_payload": payload,
        "continuation_parent": None,
    }


# @beacon[
#   id=auto-beacon@admit_carto_job-2yqp,
#   role=admit_carto_job,
#   slice_labels=mci-lease-carto,
#   kind=ast,
# ]
async def admit_carto_job(**kwargs: Any) -> dict[str, Any]:
    paths = governance_paths(kwargs.get("jobs_root"))
    mutex_wait = float(kwargs.pop("mutex_wait_seconds", DEFAULT_MUTEX_WAIT_SECONDS))
    cancel_check = kwargs.pop("cancel_check", None)
    cooldown = kwargs.pop("cooldown_seconds", None)
    kwargs["cooldown_seconds"] = float(
        _configured_cooldown_seconds() if cooldown is None else cooldown
    )
    # S05 (A1c): the caller-supplied recapture hook — captures the current local view
    # and returns {"view": nodes_by_id, "basis_sequence": int,
    # "basis_epoch_id": str}.  On a head-stale refusal the wrapper ITSELF
    # re-derives the footprint from the fresh view via compute_footprint over
    # the candidate's stored canonical path/root/kind (the disk path does not
    # move with graph topology), then retries the admission ONCE (the chosen
    # A1c shape: re-derive + rerun the overlap scan BEFORE proceeding).
    adopt_fn = kwargs.pop("adopt_fn", None)
    kwargs["jobs_root"] = paths.jobs_root
    attempt = 0
    max_attempts = 2 if adopt_fn is not None else 1
    while True:
        attempt += 1
        try:
            async with admission_lock(
                paths.jobs_root,
                timeout_seconds=mutex_wait,
                cancel_check=cancel_check,
            ):
                return admit_carto_job_locked(**kwargs)
        except AdmissionRefusal as refusal:
            if (
                refusal.kind == "head_basis_stale"
                and adopt_fn is not None
                and attempt < max_attempts
            ):
                # The admission lock is released: recapture + re-derive + retry once.
                refreshed = adopt_fn()
                if inspect.isawaitable(refreshed):
                    refreshed = await refreshed
                if refreshed is None:
                    raise
                fresh_view = refreshed["view"]
                current = validate_footprint(kwargs["footprint"])
                kwargs["footprint"] = compute_footprint(
                    current.get("path"),
                    current["root_uuid"],
                    fresh_view,
                    kind=current["kind"],
                )
                kwargs["live_mutation_lease_view"] = fresh_view
                kwargs["footprint_basis_sequence"] = refreshed.get("basis_sequence")
                kwargs["footprint_basis_epoch_id"] = refreshed.get("basis_epoch_id")
                continue
            raise


def _persist_terminal_job(
    job_file: Path, status: str, error: str | None = None, **extra: Any
) -> None:
    try:
        payload = _read_json(job_file)
    except Exception:
        payload = {}
    payload.update(extra)
    payload["status"] = status
    payload["updated_at"] = utc_now_iso()
    if error is not None:
        payload["error"] = str(error)
        summary = payload.setdefault("result_summary", {})
        if isinstance(summary, dict):
            summary.setdefault("errors", []).append(str(error))
    merge_write_carto_job_status(job_file, payload)


def _governance_cleanup_launch_result(
    exc: GovernanceCleanupError,
    *,
    root_uuid: str,
    mode: str,
) -> dict[str, Any]:
    """Preserve cleanup-required classification at the public launch boundary."""

    result: dict[str, Any] = {
        "success": False,
        "error_kind": exc.code,
        "error": str(exc),
        "blocking_artifacts": list(exc.artifact_paths),
        "root_uuid": str(root_uuid),
        "mode": str(mode),
    }
    if exc.__cause__ is not None:
        result["primary_error"] = str(exc.__cause__)
    return result


def _isolated_task_factory(coro: Any) -> Any:
    """S03 (H5): default production task spawner for governed background work.

    Background preparation/preparation-runner tasks are NEW operations and
    must not pin the spawner's operation generation; tests may inject their
    own factory (``task_factory=``) and are unaffected.
    """

    from .coherence.operation_cache import create_isolated_context_task

    return create_isolated_context_task(coro)


# @beacon[
#   id=carto-governance@refresh-launch-authority,
#   slice_labels=carto-governance,ra-carto-jobs,ra-reconcile,ra-workflowy-cache,
#   kind=ast,
#   comment=Shared refresh admission freshness and ack authority; preserves negative exact-intent PRE versus task-owned B0 and frozen preparation gates while every admitted execution uses the mandatory daemon scheduler. Legacy worker arguments are inert and no snapshot is loaded or writer process spawned here.,
# ]
async def launch_carto_refresh_job(
    *,
    root_uuid: str,
    mode: Literal["file", "folder"],
    account_scope_id: str,
    footprint_factory: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    jobs_root: str | os.PathLike[str],
    schedule_job: Callable[[Any], Any],
    worker_script: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    launcher: str = "system",
    extra_job_fields: Mapping[str, Any] | None = None,
    parent_job_id: str | None = None,
    preparation: Callable[[Mapping[str, Any], Mapping[str, Any]], Any] | None = None,
    preparation_gate: Callable[[Any], Mapping[str, Any]] | None = None,
    frozen_payload: Mapping[str, Any] | None = None,
    preparation_input_digest: str | None = None,
    source_regime: str = "standard",
    source_path_override: str | os.PathLike[str] | None = None,
    delete_temp_file_after: bool = False,
    freshness_callback: Callable[[], Any] | None = None,
    inherited_snapshot_path: str | os.PathLike[str] | None = None,
    freshness_disposition: str | None = None,
    initial_footprint: Mapping[str, Any] | None = None,
    popen_factory: Callable[..., Any] | None = None,
    task_factory: Callable[[Any], Any] | None = None,
    cancel_scope_factory: Callable[[Callable[[], Any]], Any] | None = None,
    task_observer: Callable[[str, Any], Any] | None = None,
    now_epoch: float | None = None,
    footprint_basis_sequence: int | None = None,
    footprint_basis_epoch_id: str | None = None,
    live_mutation_lease_view: Mapping[str, Any] | None = None,
    adopt_fn: Callable[[], Any] | None = None,
    mutation_lease_retry_after_seconds: float = 1.0,
) -> dict[str, Any]:
    """Admit once and delegate the complete lifetime to the required scheduler.

    worker_script/env/Popen/task/cancel-scope/observer members are retained,
    unused legacy signature only. None of them selects an execution route.
    """
    from .carto_job_runtime import CartoJobSpec, _freeze_job_payload, run_carto_cpu

    if mode not in {"file", "folder"}:
        return {"success": False, "error_kind": "invalid_mode", "error": f"Invalid mode {mode!r}."}
    jobs = get_jobs_root(jobs_root)
    jobs.mkdir(parents=True, exist_ok=True)
    prepared = preparation is not None
    override = source_path_override
    if override is None and extra_job_fields:
        override = extra_job_fields.get("source_path_override")
    delete_after = bool(
        delete_temp_file_after or (extra_job_fields or {}).get("delete_temp_file_after", False)
    )
    dedupe_key = derive_dedupe_key(
        account_scope_id=account_scope_id,
        job_type="CARTO_REFRESH",
        mode=mode,
        root_uuid=root_uuid,
        source_regime=source_regime,
        source_path_override=override,
        delete_temp_file_after=delete_after,
        preparation_input_digest=preparation_input_digest,
    )
    now = time.time() if now_epoch is None else float(now_epoch)
    job_id = f"carto-refresh-{mode}-{uuid4().hex[:8]}"
    prefix = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    job_file = jobs / f"{prefix}_{job_id}.json"
    log_file = jobs / f"{prefix}_{job_id}.log"
    job_payload: dict[str, Any] = {
        "id": job_id,
        "type": "CARTO_REFRESH",
        "mode": mode,
        "root_uuid": str(root_uuid),
        "created_at": utc_now_iso(now),
        "updated_at": utc_now_iso(now),
        "execution_model": "daemon_task",
        "execution_owner": "launcher",
        "detached": False,
        "cache_refresh_required": True,
        "freshness_disposition": freshness_disposition,
        "snapshot_path": str(inherited_snapshot_path) if inherited_snapshot_path else None,
        # Manifest base diagnostics only; the task pins its own live view.
        "snapshot_lineage": None,
        "preparation_input_digest": str(preparation_input_digest or ""),
        "source_regime": source_regime,
        "progress": {
            "total_files": 1 if mode == "file" else 0,
            "completed_files": 0,
            "current_file": None,
            "current_phase": "preparing" if prepared else "waiting",
        },
        "result_summary": {"files_refreshed": 0, "files_deleted": 0, "errors": []},
        "error": None,
        "logs_path": str(log_file),
    }
    for key, value in dict(extra_job_fields or {}).items():
        if key not in job_payload and key not in {"status", "pid", "claim_id"}:
            job_payload[key] = value

    async def obtain_freshness() -> Mapping[str, Any]:
        if freshness_callback is None:
            raise RuntimeError("No freshness authority was supplied for this launch.")
        selected = freshness_callback()
        if inspect.isawaitable(selected):
            selected = await selected
        if not isinstance(selected, Mapping) or not selected.get("snapshot_path"):
            raise RuntimeError("Freshness authority did not select a usable snapshot.")
        return {
            **dict(selected),
            "freshness_disposition": str(
                selected.get("freshness_disposition")
                or ("inherited_parent" if inherited_snapshot_path else "performed")
            ),
        }

    selection: Mapping[str, Any] = {}
    try:
        if not prepared:
            early = await negative_exact_intent_gate(
                dedupe_key=dedupe_key,
                jobs_root=jobs,
            )
            if early is not None:
                return early
            selection = await obtain_freshness()
            footprint = validate_footprint(footprint_factory(selection))
            job_payload["freshness_disposition"] = selection["freshness_disposition"]
            job_payload["snapshot_path"] = selection["snapshot_path"]
            job_payload["snapshot_lineage"] = selection.get("snapshot_lineage")
            job_payload["live_basis_lineage"] = selection.get("live_basis_lineage")
        elif initial_footprint is None:
            return {"success": False, "error_kind": "footprint_unresolvable",
                    "error": "Prepared launch requires a pre-B0 footprint."}
        else:
            footprint = initial_footprint
        admission = await admit_carto_job(
            job_file=job_file,
            job_payload=job_payload,
            job_type="CARTO_REFRESH",
            dedupe_key=dedupe_key,
            account_scope_id=account_scope_id,
            footprint=footprint,
            launcher=launcher,
            parent_job_id=parent_job_id,
            # All daemon tasks start at preparing, never the worker spawning
            # vocabulary. The common scheduler owns the running transition.
            prepared=True,
            jobs_root=jobs,
            footprint_basis_sequence=footprint_basis_sequence,
            footprint_basis_epoch_id=footprint_basis_epoch_id,
            live_mutation_lease_view=live_mutation_lease_view,
            adopt_fn=adopt_fn,
            mutation_lease_retry_after_seconds=mutation_lease_retry_after_seconds,
        )
    except AdmissionRefusal as exc:
        return exc.to_result()
    except GovernanceCleanupError as exc:
        return _governance_cleanup_launch_result(
            exc,
            root_uuid=root_uuid,
            mode=mode,
        )
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        return {"success": False,
                "error_kind": "admission_failed" if prepared else "launch_preflight_failed",
                "error": str(exc)}
    if admission.get("deduplicated"):
        return admission
    claim = admission["claim"]

    # Payload freezing can scale with a GUI preparation; keep it off the loop.
    intent = await run_carto_cpu("refresh-intent", _freeze_job_payload, frozen_payload or {})
    spec = CartoJobSpec(
        job_id=job_id, job_file=job_file, log_file=log_file,
        job_type="CARTO_REFRESH", mode=mode, root_uuid=str(root_uuid),
        claim_id=str(claim["claim_id"]), claim_path=admission["claim_path"],
        account_scope_id=account_scope_id, source_regime=source_regime,
        source_path_override=str(override) if override is not None else None,
        delete_temp_file_after=delete_after, force=bool((extra_job_fields or {}).get("force")),
        selection_metadata={key: value for key, value in selection.items() if key not in {"cache_by_id", "nodes"}},
        selection_nodes_by_id=selection.get("cache_by_id"),
        preparation_payload=intent, preparation=preparation,
        preparation_gate=preparation_gate, freshness_callback=obtain_freshness if prepared else None,
    )
    scheduled = await schedule_job(spec)
    if not scheduled.get("success"):
        return scheduled
    ack = _job_ack_from_claim(claim, governance_paths(jobs), deduplicated=False)
    ack.update(pid=scheduled["pid"], detached=False, mode="carto_refresh",
               freshness_disposition=selection.get("freshness_disposition"),
               note=("Prepared CARTO_REFRESH admitted; background freshness and preparation scheduled."
                     if prepared else "Governed CARTO_REFRESH daemon task queued."))
    if prepared:
        ack["preparing"] = True
    return ack

__all__ = [
    "AdmissionBusyTimeoutError",
    "AdmissionRefusal",
    "AdmissionUnavailableError",
    "CARTO_CANCEL_OUTCOMES",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_MUTEX_WAIT_SECONDS",
    "FootprintComparisonError",
    "FootprintUnresolvableError",
    "GovernanceConflictError",
    "GovernanceCleanupError",
    "GovernancePaths",
    "GovernanceStateUnclassifiableError",
    "MutexWaitCancelledError",
    "PHASE_C_ORIGIN_MATRIX",
    "Q3LeaseAcquisitionError",
    "TOMBSTONE_REQUIRED_FIELDS",
    "acquire_os_lock",
    "acquire_q3_leases_async",
    "admit_carto_job",
    "admit_carto_job_locked",
    "admission_lock",
    "atomic_write_json",
    "build_tombstone_from_claim",
    "cancel_carto_job",
    "canonical_json_array",
    "canonicalize_path",
    "compute_footprint",
    "derive_account_scope_id",
    "derive_artifact_key",
    "derive_dedupe_key",
    "derive_preparation_input_digest",
    "derive_region_key",
    "finalize_cancelled_job_claim_async",
    "finalize_claim_async",
    "footprints_overlap",
    "get_jobs_root",
    "governance_paths",
    "launch_carto_refresh_job",
    "negative_exact_intent_gate",
    "negative_exact_intent_gate_locked",
    "normalize_api_base_url",
    "release_q3_leases_async",
    "utc_now_iso",
    "validate_footprint",
    "validate_tombstone",
]
