# @beacon[
#   id=broker@protocol-constants,
#   role=shared broker protocol constants (host/port/version/token) - single source of truth,
#   slice_labels=nexus--broker,nexus--config,
#   kind=span,
#   show_span=true,
#   comment=One place for the broker's network defaults, protocol identity, and shared-secret env var so broker.py and server.py never duplicate them.,
# ]
"""Shared protocol constants for the WorkFlowy GLIMPSE broker.

Single source of truth imported by BOTH ``broker.py`` (the broker process)
and ``server.py`` (the MCP-server broker client), so the host/port, protocol
identity, and shared-secret configuration are never duplicated or allowed to
drift between the two sides of the connection.

All values are overridable via environment variables so non-programmer
installers can configure them through the MCP host's JSON ``env`` block (and
the broker inherits the same env when auto-launched by the MCP server).
"""

from __future__ import annotations

import os
import secrets

# ---------------------------------------------------------------------------
# Network location of the singleton broker.
# ---------------------------------------------------------------------------
BROKER_HOST: str = os.getenv("WORKFLOWY_BROKER_HOST", "127.0.0.1")
BROKER_PORT: int = int(os.getenv("WORKFLOWY_BROKER_PORT", "8765"))

# Dial host: where CLIENTS connect. BROKER_HOST doubles as the dial host in the
# common same-machine case, but when the broker binds a WILDCARD address
# (0.0.0.0 / ::) — e.g. to accept connections from WSL — clients cannot dial
# the wildcard (WinError 1214 / EADDRNOTAVAIL class). They must dial a concrete
# address instead: localhost on the broker's own machine, or the broker host's
# IP from across a network boundary (WSL -> Windows gateway IP).
BROKER_DIAL_HOST: str = (
    os.getenv("WORKFLOWY_BROKER_DIAL_HOST")
    or ("127.0.0.1" if BROKER_HOST in ("0.0.0.0", "::", "") else BROKER_HOST)
)


# @beacon[
#   id=broker@url,
#   role=⚡ broker_url(): ws:// URL clients dial,
#   slice_labels=nexus--broker,
#   kind=ast,
# ]
def broker_url() -> str:
    """Return the ws:// URL clients should dial (never a wildcard address)."""
    return f"ws://{BROKER_DIAL_HOST}:{BROKER_PORT}"


# ---------------------------------------------------------------------------
# Protocol identity. Both sides advertise these in the hello handshake so a
# version/identity mismatch (or an unrelated process squatting on the port)
# is detected instead of silently misbehaving.
# ---------------------------------------------------------------------------
PROTOCOL_NAME: str = "workflowy-glimpse-broker"
PROTOCOL_VERSION: int = 1


# ---------------------------------------------------------------------------
# Max WebSocket frame size. Mirrors the historical server default; large
# as_markdown GLIMPSEs against whole-repo Cartographer maps can exceed 1 MiB.
# ---------------------------------------------------------------------------
MAX_SIZE_BYTES: int = int(
    os.getenv("WORKFLOWY_WS_MAX_SIZE_BYTES", str(16 * 1024 * 1024))
)

# ---------------------------------------------------------------------------
# WebSocket protocol keepalive.
#
# IMPORTANT SCOPE NOTE: these constants apply ONLY to the MCP-client leg
# (server.connect_to_broker dialing the broker). Protocol-level autoping is
# DELIBERATELY DISABLED on the broker's browser-facing serve() listener
# (ping_interval=None) — do NOT "fix" the browser path to use these values.
#
# History / why the browser path must not autoping: the broker talks to
# Chrome MV3 extension service workers, which are suspendable and can be
# heavily throttled while a busy live page (e.g. the docked Markdown modal
# re-rendering a large slice) hogs the main thread. The websockets library's
# built-in autoping force-closes a connection whose PONG is late — turning a
# slow-but-ALIVE browser tab into a hard disconnect that drops every tab
# endpoint multiplexed behind that one service-worker socket. Raising the
# timeout (this default was bumped 20s -> 120s in an earlier attempt) only
# widens the window; a long live render still trips it. The correct fix is to
# not autoping the browser at all and let the extension's chrome.alarms 30s
# application-level keepalive own browser liveness (see broker.run_broker).
#
# For the MCP leg these values are fine: plain Python clients, not subject to
# MV3 throttling, with their own bounded-backoff reconnect.
# ---------------------------------------------------------------------------
PING_INTERVAL_SECONDS: float = float(os.getenv("WORKFLOWY_WS_PING_INTERVAL_S", "30"))
PING_TIMEOUT_SECONDS: float = float(os.getenv("WORKFLOWY_WS_PING_TIMEOUT_S", "120"))


# ---------------------------------------------------------------------------
# Shared secret (localhost auth). Localhost is NOT a strong security boundary:
# any local process can otherwise connect to the broker and ask the browser
# extension to extract the current Workflowy DOM. A shared token closes that.
#
# Configuration (installer-friendly, in priority order):
#   1. WORKFLOWY_BROKER_TOKEN env var (set in the MCP host JSON `env` block;
#      the auto-launched broker inherits it from the spawning MCP server).
#   2. A token file at WORKFLOWY_BROKER_TOKEN_FILE, or the default path below.
#
# If NO token is configured anywhere, the broker runs in OPEN mode (token not
# required) and logs a one-time warning. This keeps first-run UX frictionless
# for non-programmers while making it trivial to lock down: set one env var.
# ---------------------------------------------------------------------------
TOKEN_ENV_VAR: str = "WORKFLOWY_BROKER_TOKEN"
TOKEN_FILE_ENV_VAR: str = "WORKFLOWY_BROKER_TOKEN_FILE"


# @beacon[
#   id=auto-beacon@_default_token_file-rh0b,
#   role=_default_token_file,
#   slice_labels=nexus--broker,
#   kind=ast,
# ]
def _default_token_file() -> str:
    """Per-user default token file path (created/used if no env token set)."""
    base = (
        os.getenv("LOCALAPPDATA")
        or os.getenv("XDG_CONFIG_HOME")
        or os.path.expanduser("~/.config")
    )
    return os.path.join(base, "workflowy-mcp", "broker_token")


# ---------------------------------------------------------------------------
# Observability: a single shared per-user log directory.
#
# Why this exists
# ---------------
# Operational logs vanish under MCP hosts that don't surface a child's
# stdout/stderr. TypingMind shows the server's stderr in its PowerShell
# window, but Claude Code (and most embedded hosts) swallow it; and the
# AUTO-LAUNCHED broker is spawned with stdout/stderr -> DEVNULL, so its logs
# vanish entirely regardless of host. So BOTH the MCP server and the broker
# ALSO append their logs to files under a shared per-user directory, on by
# default (no debug flag), low-volume, and host-independent.
#
# This is single source of truth so the broker and server never disagree on
# where logs live. Override the directory with WORKFLOWY_LOG_DIR; disable file
# logging entirely with WORKFLOWY_FILE_LOGGING=0 (console stderr is unchanged
# either way, so a visible host like TypingMind loses nothing).
# ---------------------------------------------------------------------------
LOG_DIR_ENV_VAR: str = "WORKFLOWY_LOG_DIR"
FILE_LOGGING_ENV_VAR: str = "WORKFLOWY_FILE_LOGGING"


def _default_log_dir() -> str:
    """Per-user default log directory (sibling of the token file's folder)."""
    base = (
        os.getenv("LOCALAPPDATA")
        or os.getenv("XDG_STATE_HOME")
        or os.getenv("XDG_CONFIG_HOME")
        or os.path.expanduser("~/.config")
    )
    return os.path.join(base, "workflowy-mcp", "logs")


def log_dir() -> str:
    """Return the effective log directory (WORKFLOWY_LOG_DIR override or default)."""
    return os.getenv(LOG_DIR_ENV_VAR) or _default_log_dir()


def file_logging_enabled() -> bool:
    """Return True unless WORKFLOWY_FILE_LOGGING is explicitly falsy.

    On by default (frictionless first-run visibility). Accepts the usual
    falsy spellings ('0', 'false', 'no', 'off') to disable.
    """
    raw = os.getenv(FILE_LOGGING_ENV_VAR)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def ensure_log_dir() -> str | None:
    """Best-effort create + return the log dir, or None if disabled/unwritable.

    Never raises: observability must not be able to crash the server or broker.
    """
    if not file_logging_enabled():
        return None
    d = log_dir()
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return None


def broker_log_file() -> str:
    """Path to the broker's log file (one shared file; the broker is a singleton)."""
    return os.path.join(log_dir(), "glimpse-broker.log")


def server_log_file() -> str:
    """Path to THIS MCP server's per-process log file.

    Per-PID so concurrent MCP servers (multiple Claude Code sessions, plus
    TypingMind) never interleave into one another's file. The broker, being a
    singleton, gets one shared file instead.
    """
    return os.path.join(log_dir(), f"workflowy-mcp-server-{os.getpid()}.log")


# ---------------------------------------------------------------------------
# Log garbage collection.
#
# The per-PID ``workflowy-mcp-server-<pid>.log`` scheme (server_log_file)
# creates a NEW file on every MCP-server start -- every Claude Code session,
# every TypingMind/Claude Desktop restart -- so without a sweep these
# accumulate without bound in the log dir. The singleton
# ``glimpse-broker.log`` is append-only and likewise grows without bound.
# Neither had any cleanup (unlike the Cartographer F12 job logs, which are
# GC'd by the server). This best-effort sweep, called once at broker/server
# startup, bounds both: it deletes aged-out per-PID server logs and truncates
# an oversized broker log. It NEVER raises (observability hygiene must not be
# able to crash startup) and is env-tunable.
# ---------------------------------------------------------------------------
LOG_RETENTION_DAYS_ENV_VAR: str = "WORKFLOWY_LOG_RETENTION_DAYS"
BROKER_LOG_MAX_BYTES_ENV_VAR: str = "WORKFLOWY_BROKER_LOG_MAX_BYTES"
_DEFAULT_LOG_RETENTION_DAYS: float = 7.0
_DEFAULT_BROKER_LOG_MAX_BYTES: int = 5 * 1024 * 1024  # 5 MiB


# @beacon[
#   id=broker@gc-logs,
#   slice_labels=nexus--broker,ra-logging,
#   kind=ast,
#   comment=Best-effort startup log GC (2026-07): bounds the two previously-unbounded per-user log sinks. Deletes aged-out per-PID workflowy-mcp-server-<pid>.log files (the real leak: a new file per MCP-server start) past WORKFLOWY_LOG_RETENTION_DAYS (default 7), and size-caps the singleton append-only glimpse-broker.log past WORKFLOWY_BROKER_LOG_MAX_BYTES (default 5 MiB) by truncation. Invoked once at BOTH broker startup (run_broker) and MCP-server startup (lifespan). Never raises, honors WORKFLOWY_FILE_LOGGING=0, each fs op individually guarded (a locked live-server file is skipped; a live file's fresh mtime keeps it out of the candidate set anyway). Complements the Cartographer F12 job-log GC, which was already handled.,
# ]
def gc_logs() -> None:
    """Best-effort prune of accumulated per-user log files. Never raises.

    Two independent sweeps in the effective log dir:

      1. Per-PID SERVER logs (``workflowy-mcp-server-*.log``): any whose mtime
         is older than ``WORKFLOWY_LOG_RETENTION_DAYS`` (default 7) is deleted.
         This is the real leak -- a fresh file per MCP-server start.
      2. The singleton BROKER log (``glimpse-broker.log``): if it exceeds
         ``WORKFLOWY_BROKER_LOG_MAX_BYTES`` (default 5 MiB) it is truncated to
         empty (a size cap, not rotation -- the broker log is operational
         breadcrumbs, not an audit trail, so a hard reset is acceptable).

    Called once at broker startup (run_broker) and once per MCP-server startup
    (lifespan). Cheap: a single directory listing plus a handful of stat/unlink
    calls. Honors WORKFLOWY_FILE_LOGGING=0 (if file logging is disabled there is
    nothing to sweep). Every filesystem operation is individually guarded so one
    unreadable/locked file cannot abort the rest of the sweep.
    """
    if not file_logging_enabled():
        return
    d = log_dir()
    try:
        if not os.path.isdir(d):
            return
    except OSError:
        return

    # Resolve tunables (fail-open to defaults on any parse problem).
    try:
        retention_days = float(
            os.getenv(LOG_RETENTION_DAYS_ENV_VAR) or _DEFAULT_LOG_RETENTION_DAYS
        )
    except (TypeError, ValueError):
        retention_days = _DEFAULT_LOG_RETENTION_DAYS
    try:
        broker_log_max = int(
            os.getenv(BROKER_LOG_MAX_BYTES_ENV_VAR) or _DEFAULT_BROKER_LOG_MAX_BYTES
        )
    except (TypeError, ValueError):
        broker_log_max = _DEFAULT_BROKER_LOG_MAX_BYTES

    import time as _time

    # 1) Aged-out per-PID server logs.
    if retention_days > 0:
        cutoff = _time.time() - retention_days * 86400.0
        try:
            entries = os.listdir(d)
        except OSError:
            entries = []
        for name in entries:
            if not (name.startswith("workflowy-mcp-server-") and name.endswith(".log")):
                continue
            path = os.path.join(d, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                # Locked (a live sibling server's own file) or already gone;
                # skip it. A live server's file has a fresh mtime anyway, so
                # it is never a deletion candidate.
                continue

    # 2) Size-cap the singleton broker log.
    if broker_log_max > 0:
        blog = broker_log_file()
        try:
            if os.path.isfile(blog) and os.path.getsize(blog) > broker_log_max:
                # Truncate to empty (size cap, not rotation).
                with open(blog, "w", encoding="utf-8"):
                    pass
        except OSError:
            pass


# @beacon[
#   id=broker@get-configured-token,
#   role=⚡ get_configured_token(): read-only shared-secret resolver for CLIENTS,
#   slice_labels=nexus--broker,
#   kind=ast,
# ]
def get_configured_token() -> str | None:
    """Resolve the shared secret, or None if none is configured anywhere.

    Resolution order: env var -> explicit token file -> default token file.
    Returns the trimmed token string, or None if nothing is configured.

    NOTE: This is the read-only resolver used by CLIENTS (the MCP server and,
    later, the browser extension). It never creates a token. The BROKER uses
    ``get_or_create_token()`` so that a token always exists and auth is always
    required — see that function.
    """
    tok = os.getenv(TOKEN_ENV_VAR)
    if tok and tok.strip():
        return tok.strip()

    path = os.getenv(TOKEN_FILE_ENV_VAR) or _default_token_file()
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()
            return content or None
    except OSError:
        return None


# @beacon[
#   id=broker@token-file-path,
#   role=⚡ token_file_path(): effective token-file path,
#   slice_labels=nexus--broker,
#   kind=ast,
# ]
def token_file_path() -> str:
    """Return the token-file path in effect (explicit env override or default)."""
    return os.getenv(TOKEN_FILE_ENV_VAR) or _default_token_file()


# @beacon[
#   id=broker@get-or-create-token,
#   role=⚡ get_or_create_token(): BROKER-side shared-secret resolve-or-generate,
#   slice_labels=nexus--broker,
#   kind=ast,
# ]
def get_or_create_token() -> tuple[str, str]:
    """Resolve the shared secret, GENERATING and persisting one if absent.

    Used by the BROKER at startup so that authentication is ALWAYS required —
    there is no open mode. Resolution:

      1. If ``WORKFLOWY_BROKER_TOKEN`` env is set, use it (source="env").
      2. Else if the token file exists and is non-empty, use it (source="file").
      3. Else generate a fresh random token, write it to the token file with
         best-effort owner-only permissions, and use it (source="generated").

    Returns ``(token, source)`` where source is one of
    ``"env" | "file" | "generated"`` for logging/UX.

    The default token-file path is shared with ``get_configured_token()``, so a
    co-located MCP server (and, later, a same-machine browser helper) that read
    the file with no env override automatically agree on the secret — the
    frictionless single-machine first-run path. Cross-machine / explicit setups
    set ``WORKFLOWY_BROKER_TOKEN`` on every participant instead.
    """
    env_tok = os.getenv(TOKEN_ENV_VAR)
    if env_tok and env_tok.strip():
        return env_tok.strip(), "env"

    path = token_file_path()
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content, "file"
    except OSError:
        pass

    # Generate and persist.
    token = secrets.token_urlsafe(32)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Write with restrictive perms where the platform honors them.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, (token + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        # If we cannot persist (rare), still return the in-memory token so the
        # broker can run this session; it just won't be stable across restarts.
        return token, "generated"
    return token, "generated"
