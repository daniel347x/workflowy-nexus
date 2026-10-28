"""Host-aware file-path resolution for tool parameters — the ONE authority.

The Workflowy MCP daemon runs on ONE host (today: Windows) while agents and
their shells run on another (today: WSL). Every file-path parameter an agent
supplies (``output_file``, ``note_file``, ``nodes_file``) is opened by the
DAEMON'S host, not by the caller's shell: on a Windows daemon a path like
``/tmp/opencode/x.md`` would silently resolve under the current drive
(``C:\\tmp\\opencode\\x.md``) — a location the caller cannot see — and
``/mnt/e/…`` would fail as ``C:\\mnt\\e\\…``.

This module is the ONE seam every agent-supplied tool file path passes
through BEFORE any ``open()``, existence check, temp-file creation, or other
path-derived side effect (UnitSpec [ap:1T7JZ0], host-aware file paths):

* :func:`detect_host` — runtime host family, exactly one of ``windows`` |
  ``wsl`` | ``linux`` (nothing hard-coded; probes ``os.name``/``sys.platform``,
  ``/proc/sys/kernel/osrelease``, and ``WSL_DISTRO_NAME``/``WSL_INTEROP``);
* :func:`resolve_tool_path` — the PURE resolver: classifies the supplied form
  (REGEX-driven — never ``os.path.isabs``, whose verdicts are host- and
  version-dependent), translates it where a prefix mapping exists
  (``/mnt/<drive>/rest`` ⇄ ``<DRIVE>:\\rest``, generic over the drive
  letter), and otherwise refuses loudly with a teaching
  :class:`ToolPathError` BEFORE any side effect. Translation is
  prefix-mechanical and byte-faithful: no ``normpath``, no existence probing,
  no symlink resolution, no case folding beyond the drive letter itself; an
  already-correct path passes through byte-identically with
  ``translated=False``;
* :func:`read_tool_text_file` / :func:`write_tool_text_file` — thin shared
  I/O shapes on top of the resolver (ONE read shape: resolve → isfile →
  strict UTF-8 + universal newlines; ONE exact-byte write shape,
  :func:`_write_resolved_text_file`, ``newline=""`` so ``bytes_written ==
  len(Path(resolved).read_bytes())``);
* :class:`PathResolution` — the structured echo; ``.as_dict()`` is the
  ``path_record`` (``requested`` · ``resolved`` · ``translated`` · ``host``)
  attached to every acknowledgement that consumed a tool path parameter.

The resolver performs NO I/O and owns no success-line display; the only text
it generates is the refusal-message teaching (one builder family, so every
refusal teaches identically, prefixed by the caller's ``context``/``param``).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import Any

# ── Host families (§6.1) ───────────────────────────────────────────────────
HOST_WINDOWS = "windows"
HOST_WSL = "wsl"
HOST_LINUX = "linux"

# ── Path-form classes (§6.2) — REGEX-driven, first match wins. NEVER
# os.path.isabs/ntpath.isabs: their verdicts are host- and version-dependent
# (ntpath.isabs('/tmp/x') is False on Python 3.13 but True on 3.12), and an
# incidental refusal must never become a load-bearing accident.
_FORM_UNC = "unc"                        # C1: \\server\share or //server/share
_FORM_DRIVE_ABS = "drive_abs"            # C2: X:\rest or X:/rest
_FORM_DRIVE_RELATIVE = "drive_relative"  # C3: X:rest or bare X: (no separator)
_FORM_MNT_DRIVE = "mnt_drive"            # C4: /mnt/<single letter>[/rest]
_FORM_POSIX_ABSOLUTE = "posix_absolute"  # C5: any other /…
_FORM_HOME = "home"                      # C6: ~…
_FORM_RELATIVE = "relative"              # C7: everything else

_DRIVE_ABS_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$", re.DOTALL)
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_MNT_DRIVE_RE = re.compile(r"^/mnt/([A-Za-z])(?:/(.*))?$", re.DOTALL)

# The direction only selects the refusal-message verb ("written to" / "read
# from"); it NEVER changes the host × class action table (§6.3) — every cell
# applies to reads and writes alike.
_DIRECTION_VERBS = {"write": "written to", "read": "read from"}


# @beacon[
#   id=path-resolution@ToolPathError,
#   slice_labels=nexus-path-resolution-logic,
#   kind=ast,
#   comment=The loud teaching refusal (I3 — never silent): a ValueError subclass carrying the COMPLETE teaching message (offending path · daemon host · why the form cannot work · what to pass instead). Raised BEFORE any side effect — a refused path writes zero bytes anywhere. Consumers surface it through their ESTABLISHED error shapes (NetworkError at the note_file reader, the kernel operation_failed envelope at node.read, the {success:false,error(s)} dicts at the render tails/etch/estate) — never a new envelope.,
# ]
class ToolPathError(ValueError):
    """A tool file-path parameter the daemon's host cannot honour.

    Carries the COMPLETE teaching message (the offending path, the daemon's
    host, why the form cannot work, and what to pass instead). Raised before
    any side effect; consumers surface it through their ESTABLISHED error
    shapes (never a new envelope).
    """


# @beacon[
#   id=path-resolution@PathResolution,
#   slice_labels=nexus-path-resolution-logic,
#   kind=ast,
#   comment=The structured echo every path-consuming acknowledgement carries (I5): as_dict() IS the path_record payload — requested (the path exactly as supplied, stripped) · resolved (the path actually used on the daemon host) · translated (False ⇔ resolved is byte-identical to requested) · host (windows|wsl|linux) — in this key order. P3 echo rule: argument-echo ack fields carry `requested` byte-identically; `resolved` lives in path_record and in derived-path fields. Frozen dataclass — the record is immutable once minted.,
# ]
@dataclass(frozen=True)
class PathResolution:
    """The structured result of one tool-path resolution (the ``path_record``).

    ``requested`` is the path exactly as supplied (stripped); ``resolved`` is
    the path actually used on the daemon's host; ``translated`` is False
    exactly when ``resolved`` is byte-identical to ``requested``; ``host`` is
    the daemon's detected host family.
    """

    requested: str
    resolved: str
    translated: bool
    host: str

    def as_dict(self) -> dict[str, Any]:
        """The ``path_record`` payload, in the canonical key order."""
        return {
            "requested": self.requested,
            "resolved": self.resolved,
            "translated": self.translated,
            "host": self.host,
        }


# @beacon[
#   id=path-resolution@_detect_host_raw,
#   slice_labels=nexus-path-resolution-logic,
#   kind=ast,
#   comment=The ONE host-detection probe reader (the monkeypatch seam — tests patch THIS). In order: os.name/sys.platform → 'windows'; else WSL iff /proc/sys/kernel/osrelease (read failures → empty string) contains 'microsoft' case-insensitively (catches WSL1 '…-Microsoft' and WSL2 '…-microsoft-standard-WSL2') OR WSL_DISTRO_NAME/WSL_INTEROP is present in the environment; else 'linux'. Darwin falls through to 'linux' (POSIX semantics). Stateless — no cache. The ONLY osrelease/WSL_* reader in the package (spec postcondition P2d).,
# ]
def _detect_host_raw() -> str:
    """Read the host-family probes; the monkeypatch seam for host simulation."""
    if os.name == "nt" or sys.platform == "win32":
        return HOST_WINDOWS
    try:
        with open("/proc/sys/kernel/osrelease", "r", encoding="utf-8", errors="replace") as f:
            osrelease = f.read()
    except OSError:
        osrelease = ""
    if "microsoft" in osrelease.lower():
        return HOST_WSL
    if "WSL_DISTRO_NAME" in os.environ or "WSL_INTEROP" in os.environ:
        return HOST_WSL
    return HOST_LINUX


# @beacon[
#   id=path-resolution@detect_host,
#   slice_labels=nexus-path-resolution-logic,
#   kind=ast,
#   comment=The daemon's runtime host family — exactly one of 'windows' | 'wsl' | 'linux'. Stateless (reads the probes via _detect_host_raw() on every call; no cache, no TTL); nothing is hard-coded to today's topology. WSL and plain Linux ARE distinguishable (the osrelease 'microsoft' marker / WSL_* environment).,
# ]
def detect_host() -> str:
    """Return the daemon's host family: ``windows`` | ``wsl`` | ``linux``."""
    return _detect_host_raw()


def _classify_path_form(raw: str) -> str:
    """Classify a stripped non-blank path (§6.2; first match wins)."""
    if raw.startswith("\\\\") or raw.startswith("//"):
        return _FORM_UNC
    if _DRIVE_ABS_RE.match(raw):
        return _FORM_DRIVE_ABS
    if _DRIVE_PREFIX_RE.match(raw):
        return _FORM_DRIVE_RELATIVE
    if _MNT_DRIVE_RE.match(raw):
        return _FORM_MNT_DRIVE
    if raw.startswith("/"):
        return _FORM_POSIX_ABSOLUTE
    if raw.startswith("~"):
        return _FORM_HOME
    return _FORM_RELATIVE


def _mnt_to_windows(raw: str) -> str:
    """``/mnt/<letter>[/rest]`` → ``<UPPER>:\\rest`` (prefix-mechanical).

    Only the ``/mnt/<letter>`` prefix is rewritten and separators are
    canonicalized to ``\\`` on the translated side; the remainder's bytes are
    untouched (no normpath, no probing). ``/mnt/e`` → ``E:\\``.
    """
    m = _MNT_DRIVE_RE.match(raw)
    letter = m.group(1).upper()
    rest = m.group(2) or ""
    return f"{letter}:\\" + rest.replace("/", "\\")


def _windows_to_mnt(raw: str) -> str:
    """``X:\\rest`` / ``X:/rest`` → ``/mnt/<lower(letter)>/rest`` (prefix-mechanical).

    Separators canonicalized to ``/`` on the translated side; the remainder's
    bytes untouched. ``E:\\`` → ``/mnt/e/``.
    """
    m = _DRIVE_ABS_RE.match(raw)
    letter = m.group(1).lower()
    rest = m.group(2)
    return f"/mnt/{letter}/" + rest.replace("\\", "/")


def _host_remedy(host: str) -> str:
    """The 'what to pass instead' sentence, per daemon host (one wording)."""
    if host == HOST_WINDOWS:
        return (
            "On the daemon's 'windows' host, pass '/mnt/<drive>/…' (mapped to "
            "'<DRIVE>:\\…') or a Windows path 'X:\\…'."
        )
    if host == HOST_WSL:
        return (
            "On the daemon's 'wsl' host, pass a POSIX absolute path ('/…'); a "
            "Windows path 'X:\\…' is mapped to '/mnt/<drive>/…'."
        )
    return (
        "On the daemon's 'linux' host, pass a POSIX absolute path ('/…') on "
        "the daemon's own filesystem."
    )


def _silent_windows_example(raw: str) -> str:
    """Where a POSIX path would silently land on a Windows daemon (teaching).

    Derived from the daemon process's current-drive (fallback ``C:``); the
    example shows the exact location a naive ``open()`` would resolve to.
    """
    try:
        drive = os.path.splitdrive(os.getcwd())[0] or "C:"
    except OSError:  # pragma: no cover - cwd vanished; teaching example only
        drive = "C:"
    return f"{drive}{raw}".replace("/", "\\")


def _refuse(
    *,
    context: str,
    param: str,
    raw: str,
    host: str,
    direction: str,
    reason: str,
    remedy: str | None = None,
) -> "ToolPathError":
    """Build the ONE teaching refusal (§6.4): path, host, why, what to pass."""
    verb = _DIRECTION_VERBS.get(direction, _DIRECTION_VERBS["write"])
    message = f"{context} {param} '{raw}' was refused (would be {verb}): {reason} "
    message += remedy if remedy is not None else _host_remedy(host)
    return ToolPathError(message)


# @beacon[
#   id=path-resolution@resolve_tool_path,
#   slice_labels=nexus-path-resolution-logic,
#   kind=ast,
#   comment=THE ONE host-aware path authority: every agent-supplied tool file-path parameter (output_file, note_file, nodes_file) resolves here BEFORE any open(), existence check, temp-file creation, or path-derived side effect. PURE — path in, PathResolution or ToolPathError out; no I/O, no display text beyond the refusal builder. Classification is REGEX-driven (§6.2 C1–C7, first match wins; never os.path.isabs). Action table (§6.3, host × class): windows passes UNC/drive forms verbatim and translates /mnt/<drive>/rest → <UPPER>:\rest, refusing every other POSIX-absolute path (WSL-only locations like /tmp would silently resolve under the daemon's current drive), drive-relative, ~ and relative forms; wsl translates X:\rest|X:/rest → /mnt/<lower>/rest, passes /mnt/<drive> and POSIX absolute verbatim, refuses UNC (no /mnt equivalent), drive-relative, ~ and relative; linux passes /mnt/<drive> and POSIX absolute verbatim and refuses every Windows form, drive-relative, ~ and relative. Translation is prefix-mechanical and byte-faithful (no normpath/probing/symlinks/case-folding beyond the drive letter); an already-correct path passes through byte-identically with translated=False. direction ('write'|'read') selects ONLY the refusal-message verb, never the action. Refusals are loud, teaching (offending path · daemon host · why · what to pass instead), side-effect-free, and prefixed '{context} {param}' exactly as the established note_file messages are.,
# ]
def resolve_tool_path(
    path: str,
    *,
    param: str,
    context: str,
    direction: str = "write",
) -> PathResolution:
    """Resolve one agent-supplied tool file path against the daemon's host.

    Returns a :class:`PathResolution`; raises :class:`ToolPathError` (with the
    complete teaching message) where the form cannot work on this host. Never
    performs I/O; a refused path has zero side effects anywhere.
    """
    host = detect_host()
    raw = str(path if path is not None else "").strip()
    prefix = f"{context} {param}"

    if not raw:
        raise ToolPathError(
            f"{prefix}: empty path. Pass an ABSOLUTE path — the daemon's "
            "working directory is host-dependent, so an empty or relative "
            f"form resolves unpredictably. {_host_remedy(host)}"
        )

    form = _classify_path_form(raw)

    def _pass() -> PathResolution:
        return PathResolution(requested=raw, resolved=raw, translated=False, host=host)

    def _translate(resolved: str) -> PathResolution:
        return PathResolution(requested=raw, resolved=resolved, translated=True, host=host)

    # ── C1 UNC ────────────────────────────────────────────────────────────
    if form == _FORM_UNC:
        if host == HOST_WINDOWS:
            return _pass()
        if host == HOST_WSL:
            raise _refuse(
                context=context, param=param, raw=raw, host=host, direction=direction,
                reason=(
                    "is a Windows UNC share path, but UNC shares have no "
                    "'/mnt' equivalent, so the daemon's 'wsl' host cannot open it."
                ),
                remedy=(
                    "Pass a POSIX absolute path ('/…'), or a Windows drive path "
                    "'X:\\…' (mapped to '/mnt/<drive>/…')."
                ),
            )
        raise _refuse(
            context=context, param=param, raw=raw, host=host, direction=direction,
            reason=(
                "is a Windows path but the daemon runs on plain 'linux', which "
                "cannot open Windows UNC/drive forms."
            ),
        )

    # ── C2 DRIVE_ABS ──────────────────────────────────────────────────────
    if form == _FORM_DRIVE_ABS:
        if host == HOST_WINDOWS:
            return _pass()
        if host == HOST_WSL:
            return _translate(_windows_to_mnt(raw))
        raise _refuse(
            context=context, param=param, raw=raw, host=host, direction=direction,
            reason=(
                "is a Windows path but the daemon runs on plain 'linux', which "
                "cannot open Windows UNC/drive forms."
            ),
        )

    # ── C3 DRIVE_RELATIVE (refused everywhere) ────────────────────────────
    if form == _FORM_DRIVE_RELATIVE:
        raise ToolPathError(
            f"{prefix} must be an ABSOLUTE path (got '{raw}'): a drive letter "
            "without a separator is drive-RELATIVE — it would resolve against "
            f"the daemon's working directory on that drive (host '{host}'), "
            f"which is host-dependent. {_host_remedy(host)}"
        )

    # ── C4 MNT_DRIVE ──────────────────────────────────────────────────────
    if form == _FORM_MNT_DRIVE:
        if host == HOST_WINDOWS:
            return _translate(_mnt_to_windows(raw))
        return _pass()  # wsl and plain linux: a valid local POSIX path

    # ── C5 POSIX_ABSOLUTE ─────────────────────────────────────────────────
    if form == _FORM_POSIX_ABSOLUTE:
        if host == HOST_WINDOWS:
            verb = _DIRECTION_VERBS.get(direction, _DIRECTION_VERBS["write"])
            raise ToolPathError(
                f"{prefix} '{raw}' names a location that exists only inside "
                "WSL ('/tmp', '/home', …); the daemon runs on 'windows' and "
                "cannot see it — it would silently resolve under the daemon's "
                f"current drive and be {verb} "
                f"'{_silent_windows_example(raw)}', a location the caller "
                f"cannot see. Nothing was {verb}. Pass '/mnt/<drive>/…' "
                "(mapped to '<DRIVE>:\\…') or a Windows path 'X:\\…'."
            )
        return _pass()  # wsl and plain linux

    # ── C6 HOME (refused everywhere — never expanded) ─────────────────────
    if form == _FORM_HOME:
        raise ToolPathError(
            f"{prefix} must be an ABSOLUTE path (got '{raw}'): '~' home forms "
            "are never expanded by the daemon — its home directory is "
            f"host-dependent (the daemon runs on '{host}', which is not your "
            f"shell's host). {_host_remedy(host)}"
        )

    # ── C7 RELATIVE (refused everywhere) ──────────────────────────────────
    raise ToolPathError(
        f"{prefix} must be an ABSOLUTE path (got '{raw}'): the daemon's "
        "working directory is host-dependent, so a relative path would "
        f"resolve unpredictably (the daemon runs on '{host}'). "
        + _host_remedy(host)
    )


def _display_path(resolution: PathResolution) -> str:
    """How a message names the checked location (resolved, + the mapping)."""
    if not resolution.translated:
        return f"'{resolution.resolved}'"
    return (
        f"'{resolution.requested}' (mapped to '{resolution.resolved}' on the "
        f"daemon's '{resolution.host}' host)"
    )


# @beacon[
#   id=path-resolution@read_tool_text_file,
#   slice_labels=nexus-path-resolution-logic,
#   kind=ast,
#   comment=The ONE shared read shape for tool file-path parameters (note_file, nodes_file): resolve_tool_path FIRST (loud teaching ToolPathError before any side effect), then os.path.isfile on the RESOLVED path (missing/directory → ToolPathError with the established 'not an existing file' wording class), then strict UTF-8 with universal newlines (CRLF/CR → LF; never errors='replace'). Returns (PathResolution, text) so the caller can attach the path_record. UnicodeDecodeError/OSError from the read itself propagate for the consumer's established error mapping (NetworkError at the note_file reader; the etch error dicts).,
# ]
def read_tool_text_file(
    path: str,
    *,
    param: str,
    context: str,
) -> tuple[PathResolution, str]:
    """Resolve + read one tool text file; returns ``(resolution, text)``."""
    resolution = resolve_tool_path(path, param=param, context=context, direction="read")
    if not os.path.isfile(resolution.resolved):
        raise ToolPathError(
            f"{context} {param} {_display_path(resolution)} is not an existing "
            "file on the MCP server host (missing, or a directory)."
        )
    with open(resolution.resolved, "r", encoding="utf-8", newline=None) as f:
        text = f.read()
    return resolution, text


# @beacon[
#   id=path-resolution@_write_resolved_text_file,
#   slice_labels=nexus-path-resolution-logic,
#   kind=ast,
#   comment=The ONE exact-byte text writer (rehomed from server.py's _glimpse_estate_write_bytes — move, not copy; exactly one definition site per spec postcondition P2c). newline='' suppresses Windows CRLF translation, so the file holds the EXACT composed bytes and the returned count equals len(Path(resolved).read_bytes()) on every platform. Takes an ALREADY-RESOLVED path string (callers reach it through resolve_tool_path / write_tool_text_file, or hold a resolution-derived companion path); it performs no resolution of its own.,
# ]
def _write_resolved_text_file(resolved: str, text: str) -> int:
    """Exact-byte write to an already-resolved path; returns bytes written."""
    with open(resolved, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return len(text.encode("utf-8"))


# @beacon[
#   id=path-resolution@write_tool_text_file,
#   slice_labels=nexus-path-resolution-logic,
#   kind=ast,
#   comment=The ONE shared write shape for tool output_file parameters whose byte contract is the plain exact-byte text write: resolve_tool_path FIRST (a refusal writes ZERO bytes anywhere — never a silent write to a location the caller cannot see), then the single exact-byte writer (_write_resolved_text_file, newline=''). Returns (PathResolution, bytes_written) so the caller can attach the path_record and keep bytes_written == len(Path(resolved).read_bytes()). Sites with a unique write shape (node.read's atomic temp+os.replace, the json.dump fan-outs) resolve through resolve_tool_path and open the RESOLVED string themselves.,
# ]
def write_tool_text_file(
    path: str,
    text: str,
    *,
    param: str,
    context: str,
) -> tuple[PathResolution, int]:
    """Resolve + exact-byte write one tool text file; returns ``(resolution, bytes_written)``."""
    resolution = resolve_tool_path(path, param=param, context=context, direction="write")
    nbytes = _write_resolved_text_file(resolution.resolved, text)
    return resolution, nbytes
