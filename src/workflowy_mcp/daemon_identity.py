"""Immutable installed-code identity; no server import, runtime git or network.

Source timestamps order releases at equal PEP 440 versions, not git ancestry.
The snapshot is process-local; its random incarnation is never lock ownership.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import math
import re
import socket
import sys
import threading
from dataclasses import dataclass
from typing import Any, Literal, Mapping
from uuid import uuid4

from packaging.version import InvalidVersion, Version

from . import __version__

IDENTITY_PROTOCOL = 1
DAEMON_META_KEY = "workflowy-nexus/daemon"
SHIM_META_KEY = "workflowy-nexus/shim"
IdentityRelation = Literal["equal", "local_newer", "peer_newer", "unknown"]
_KINDS = frozenset({"git", "direct_url", "editable", "dirty", "unknown"})
_PHASES = frozenset({"starting", "running", "draining", "exiting"})
_snapshot_lock = threading.Lock()
_snapshot: InstalledIdentity | None = None


@dataclass(frozen=True)
class IdentityWarning:
    code: str
    message: str
    recovery: str

    def payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "recovery": self.recovery}


def unknown_warning(reason: str) -> IdentityWarning:
    return IdentityWarning(
        "identity_unknown",
        f"WARNING: installed identity cannot be ordered reliably ({reason}); continuing "
        "with the running daemon. No automatic downgrade or restart was attempted.",
        "continue_with_running_daemon",
    )


def valid_commit(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is not None


def valid_commit_time(value: Any) -> bool:
    return type(value) is int and value >= 0


def _known_kind(value: Any) -> bool:
    return isinstance(value, str) and value in _KINDS


@dataclass(frozen=True)
class InstalledIdentity:
    package_version: str
    commit: str | None
    commit_time: int | None
    identity_kind: str
    platform: str
    hostname: str
    instance_id: str
    warnings: tuple[IdentityWarning, ...] = ()

    def source_payload(self) -> dict[str, Any]:
        return {
            "package_version": self.package_version,
            "commit": self.commit,
            "commit_time": self.commit_time,
        }


def _read_direct_url() -> Mapping[str, Any] | None:
    """Read once from our distribution; URLs never enter the public identity."""
    try:
        text = importlib.metadata.distribution("workflowy-nexus-mcp-tools").read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    if text is None:
        return None
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("invalid direct-url metadata")
    return value


def _read_embedded_stamp() -> Mapping[str, Any] | None:
    try:
        stamp = importlib.import_module("workflowy_mcp._build_identity")
    except ModuleNotFoundError as exc:
        if exc.name == "workflowy_mcp._build_identity":
            return None
        raise
    return {
        "schema": getattr(stamp, "SOURCE_IDENTITY_SCHEMA", None),
        "commit": getattr(stamp, "SOURCE_COMMIT", None),
        "commit_time": getattr(stamp, "SOURCE_COMMIT_TIME", None),
        "kind": getattr(stamp, "SOURCE_IDENTITY_KIND", None),
    }


def _load_installed_identity() -> InstalledIdentity:
    diagnostics: list[IdentityWarning] = []
    direct: Mapping[str, Any] = {}
    stamp: Mapping[str, Any] = {}
    try:
        direct = _read_direct_url() or {}
    except Exception:
        diagnostics.append(unknown_warning("unrecognized identity metadata"))
    if set(direct) - {"url", "vcs_info", "archive_info", "dir_info", "subdirectory"}:
        diagnostics.append(unknown_warning("unrecognized identity metadata"))
    editable = isinstance(direct.get("dir_info"), dict) and direct["dir_info"].get("editable") is True
    commit = commit_time = None
    kind = "editable" if editable else "unknown"
    if not editable:
        try:
            stamp = _read_embedded_stamp() or {}
        except Exception:
            diagnostics.append(unknown_warning("unrecognized identity metadata"))
        stamp_valid = (
            type(stamp.get("schema")) is int and stamp["schema"] == IDENTITY_PROTOCOL
            and _known_kind(stamp.get("kind"))
            and (stamp.get("commit") is None or valid_commit(stamp["commit"]))
            and (stamp.get("commit_time") is None or valid_commit_time(stamp["commit_time"]))
            and (stamp.get("commit") is not None or stamp.get("commit_time") is None)
        )
        if stamp and not stamp_valid:
            diagnostics.append(unknown_warning("unrecognized identity metadata"))
        vcs = direct.get("vcs_info")
        direct_commit = vcs.get("commit_id") if isinstance(vcs, dict) else None
        if stamp_valid and valid_commit(stamp.get("commit")):
            commit, commit_time, kind = stamp["commit"], stamp["commit_time"], stamp["kind"]
            if direct_commit is not None and direct_commit != commit:
                diagnostics.append(IdentityWarning(
                    "identity_metadata_mismatch",
                    "WARNING: installer identity disagrees with the loaded package's embedded "
                    "identity; keeping the embedded identity.",
                    "use_loaded_package_identity",
                ))
        elif valid_commit(direct_commit):
            commit, kind = direct_commit, "direct_url"
        elif stamp_valid:
            kind = stamp["kind"]
        if direct_commit is not None and not valid_commit(direct_commit):
            diagnostics.append(unknown_warning("unrecognized identity metadata"))
    if commit is None or commit_time is None:
        diagnostics.append(unknown_warning("source identity unavailable"))
    try:
        hostname = socket.gethostname().strip().casefold()
    except Exception:
        hostname = ""
        diagnostics.append(unknown_warning("unrecognized identity metadata"))
    # Tuples/frozen records keep callers from mutating the startup snapshot.
    return InstalledIdentity(
        __version__, commit, commit_time, kind, sys.platform, hostname, uuid4().hex,
        tuple(dict.fromkeys(diagnostics)),
    )


# @beacon[
#   id=daemon-identity@installed-identity,
#   role=installed_identity: one immutable process-start installed-code snapshot,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Reads installer metadata once and retains immutable diagnostics for startup/attach emission; editable installs are unpinned and embedded source identity wins for packaged code; no server import or runtime git/network.,
# ]
def installed_identity() -> InstalledIdentity:
    """Return one snapshot/diagnostic tuple; the startup or attach owner emits it."""
    global _snapshot
    if _snapshot is None:
        with _snapshot_lock:
            if _snapshot is None:
                _snapshot = _load_installed_identity()
    return _snapshot


def _source(value: InstalledIdentity | Mapping[str, Any]) -> tuple[Any, Any, Any, Any]:
    """Extract source fields; only the exact three-field control target has an implicit git kind, never a full peer record."""
    if isinstance(value, InstalledIdentity):
        return value.package_version, value.commit, value.commit_time, value.identity_kind
    kind = value.get("identity_kind", "git" if set(value) == {"package_version", "commit", "commit_time"} else None)
    return value.get("package_version"), value.get("commit"), value.get("commit_time"), kind


def comparison_warnings(
    local: InstalledIdentity | Mapping[str, Any], peer: InstalledIdentity | Mapping[str, Any]
) -> tuple[IdentityWarning, ...]:
    """Diagnostics for the one attach comparison, without repeating metadata I/O."""
    lv, lc, lt, lk = _source(local)
    pv, pc, pt, pk = _source(peer)
    try:
        versions_equal = Version(lv) == Version(pv)
    except (InvalidVersion, TypeError):
        return (unknown_warning("unrecognized identity metadata"),)
    if not _known_kind(lk) or not _known_kind(pk):
        return (unknown_warning("unrecognized identity metadata"),)
    if versions_equal and valid_commit(lc) and lc == pc and lt != pt:
        return (IdentityWarning(
            "identity_metadata_mismatch",
            "WARNING: identical source commits advertise different source timestamps; "
            "keeping identical code without a restart.",
            "continue_with_running_daemon",
        ),)
    if versions_equal and valid_commit(lc) and valid_commit(pc) and lc != pc and valid_commit_time(lt) and lt == pt:
        return (unknown_warning("different commits share a source timestamp"),)
    if versions_equal and not (valid_commit(lc) and valid_commit(pc) and valid_commit_time(lt) and valid_commit_time(pt)):
        return (unknown_warning("source identity unavailable"),)
    return ()


# @beacon[
#   id=daemon-identity@compare-install-identity,
#   role=compare_install_identity: declared partial release order without hash ordering,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Orders valid PEP 440 versions first; equal-version source order needs an explicit full-record kind or the compact control target; missing full-record kind warns and is unorderable at equal versions.,
# ]
def compare_install_identity(
    local: InstalledIdentity | Mapping[str, Any], peer: InstalledIdentity | Mapping[str, Any]
) -> IdentityRelation:
    """Compare versions first, then known source provenance; compact control targets retain their existing order."""
    lv, lc, lt, lk = _source(local)
    pv, pc, pt, pk = _source(peer)
    try:
        left, right = Version(lv), Version(pv)
    except (InvalidVersion, TypeError):
        return "unknown"
    if left != right:
        return "local_newer" if left > right else "peer_newer"
    if not _known_kind(lk) or not _known_kind(pk) or lk in {"editable", "dirty", "unknown"} or pk in {"editable", "dirty", "unknown"}:
        return "unknown"
    if valid_commit(lc) and valid_commit(pc):
        if lc == pc:
            return "equal"
        if valid_commit_time(lt) and valid_commit_time(pt) and lt != pt:
            return "local_newer" if lt > pt else "peer_newer"
    return "unknown"


# @beacon[
#   id=daemon-identity@can-replace-daemon,
#   role=can_replace_daemon: non-destructive same-host native launch-affinity check,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Only a non-WSL shim with matching platform and casefolded hostname plus protocol-1 graceful-restart metadata may replace a daemon; missing affinity remains attach-only.,
# ]
def can_replace_daemon(
    local_platform: str, local_hostname: str, local_is_wsl: bool,
    peer_metadata: Mapping[str, Any],
) -> bool:
    """Decide launch capability without stop probes, DNS or executable discovery."""
    if local_is_wsl or type(peer_metadata.get("protocol")) is not int or peer_metadata["protocol"] != IDENTITY_PROTOCOL:
        return False
    peer_host = peer_metadata.get("hostname")
    if not isinstance(peer_host, str) or not local_hostname.strip() or local_hostname.strip().casefold() != peer_host.strip().casefold():
        return False
    bound, path = peer_metadata.get("drain_timeout_s"), peer_metadata.get("restart_path")
    instance = peer_metadata.get("instance_id")
    phase, restart_id = peer_metadata.get("phase"), peer_metadata.get("restart_id")
    try:
        valid_bound = isinstance(bound, (int, float)) and not isinstance(bound, bool) and math.isfinite(bound) and bound >= 3600
    except (OverflowError, TypeError):
        valid_bound = False
    return (
        local_platform == peer_metadata.get("platform")
        and isinstance(instance, str) and re.fullmatch(r"[0-9a-f]{32}", instance) is not None
        and isinstance(phase, str) and phase in _PHASES
        and valid_bound
        and isinstance(path, str) and path.startswith("/") and not path.startswith("//")
        and path.endswith("/_nexus/restart") and "?" not in path and "#" not in path
        and "restart_id" in peer_metadata
        and (restart_id is None or isinstance(restart_id, str) and re.fullmatch(r"[0-9a-f]{32}", restart_id) is not None)
    )
