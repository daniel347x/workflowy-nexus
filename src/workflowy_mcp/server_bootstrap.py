"""Process-start platform barrier and the sole lifetime account writer guard.

Library clients and Cartographer workers do not bootstrap a full server. A
successful guard is never released here, including on lifespan teardown:
only process death ends ownership.
"""

from __future__ import annotations

import os
import platform
import re
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .carto_job_governance import _HeldOSLock
    from .models.config import ServerConfig

TEST_ALLOW_WSL_ENV = "WORKFLOWY_MCP_TEST_ALLOW_WSL_SERVER"
_probe_warning_emitted = False
_override_warning_emitted = False
_writer_guard: _HeldOSLock | None = None
_bootstrap_mutex = threading.Lock()


class SingleWriterBusyError(SystemExit):
    """Fatal entry failure, consistently exit 75 on every full transport."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self.message = (
            f"FATAL: another Workflowy full server already holds '{lock_path}'. "
            "This process will not serve. Use workflowy-mcp-shim or connect to the existing daemon."
        )
        print(self.message, file=sys.stderr)
        super().__init__(75)

    def __str__(self) -> str:
        return self.message


class SingleWriterUnavailableError(SystemExit):
    """Fatal entry failure distinct from contention; never expose credentials."""

    def __init__(self, lock_path: Path, exception_type: str) -> None:
        self.lock_path = lock_path
        self.message = (
            f"FATAL: cannot acquire the Workflowy writer guard at '{lock_path}' ({exception_type}). "
            "This process will not serve. Check access to this configured jobs directory; "
            "do not delete writer.lock."
        )
        print(self.message, file=sys.stderr)
        super().__init__(74)

    def __str__(self) -> str:
        return self.message


def _has_wsl_marker(release: str) -> bool:
    folded = release.casefold()
    return "microsoft" in folded or re.search(r"(?<!\w)wsl[12]?(?!\w)", folded) is not None


# @beacon[
#   id=server-bootstrap@is-wsl,
#   role=is_wsl: classify the current kernel without inherited-env guesses,
#   slice_labels=nexus-loading-flow,nexus--daemon,nexus--broker,
#   kind=ast,
#   comment=Linux kernel markers alone identify WSL; non-Linux never probes and unavailable kernel identity warns once then allows.,
# ]
def is_wsl(*, platform_name: str | None = None, kernel_release: str | None = None) -> bool:
    """Recognize WSL specifically; unknown Linux identity is allowed, not refused."""
    global _probe_warning_emitted

    if not (sys.platform if platform_name is None else platform_name).startswith("linux"):
        return False
    if kernel_release is None:
        try:
            kernel_release = platform.release()
        except Exception:  # Unknown identity must not refuse an ordinary Linux host.
            kernel_release = ""
    if kernel_release.strip():
        return _has_wsl_marker(kernel_release)
    try:
        version = Path("/proc/version").read_text(encoding="utf-8")
        if version.strip():
            return _has_wsl_marker(version)
    except (OSError, UnicodeError):
        pass
    if not _probe_warning_emitted:
        _probe_warning_emitted = True
        print(
            "WARNING: WSL kernel identity is unavailable; allowing startup rather than refusing an ordinary Linux host.",
            file=sys.stderr,
        )
    return False


# @beacon[
#   id=server-bootstrap@refuse-wsl,
#   role=refuse_wsl_server: shared early full-server and broker refusal,
#   slice_labels=nexus-loading-flow,nexus--daemon,nexus--broker,
#   kind=ast,
#   comment=Refuses a WSL full server or broker with exit 2; exact suite-only override 1 warns once and never bypasses the writer guard.,
# ]
def refuse_wsl_server(component: str) -> None:
    """Refuse only real WSL; the suite override does not change classification."""
    global _override_warning_emitted

    if not is_wsl():
        return
    if os.environ.get(TEST_ALLOW_WSL_ENV) == "1":
        if not _override_warning_emitted:
            _override_warning_emitted = True
            print(
                "WARNING: WORKFLOWY_MCP_TEST_ALLOW_WSL_SERVER=1 skips only the WSL refusal for tests; the writer guard remains mandatory.",
                file=sys.stderr,
            )
        return
    print(
        f"FATAL: the Workflowy {component} must not run inside WSL. Run the daemon and broker on Windows; "
        "use workflowy-mcp-shim or an HTTP client from WSL.",
        file=sys.stderr,
    )
    raise SystemExit(2)


# @beacon[
#   id=server-bootstrap@single-writer-guard,
#   role=acquire_single_writer_guard: sole account writer acquisition,
#   slice_labels=nexus-loading-flow,nexus--daemon,
#   kind=ast,
#   comment=Derives the existing account scope and takes its one nonblocking OS writer lock before startup; retains a non-inheritable handle until process death and reuses it on same-path entry.,
# ]
def acquire_single_writer_guard(config: ServerConfig) -> _HeldOSLock:
    """Take the lifetime guard once; never release it when a lifespan exits.

    Fatal errors inherit SystemExit so the same typed startup failure carries
    its exit code through both existing FastMCP transport runners. A host
    importing the app can catch that typed failure and owns its own exit.
    """
    global _writer_guard

    from . import carto_job_governance as gov
    from .coherence.errors import CoherenceSchemaError
    from .coherence.paths import writer_lock_path

    api = config.get_api_config()
    scope = gov.derive_account_scope_id(api.base_url, api.api_key.get_secret_value())
    # Only the path authority builds a writer filename. If path resolution
    # itself fails, report its diagnostic path, never an alternate lock path.
    location = gov.get_jobs_root()
    try:
        location = writer_lock_path(scope)
    except (OSError, RuntimeError, CoherenceSchemaError) as exc:
        location = Path(getattr(exc, "path", None) or getattr(exc, "filename", None) or location)
        raise SingleWriterUnavailableError(location, type(exc).__name__) from None

    with _bootstrap_mutex:
        if _writer_guard is not None:
            if _writer_guard.path != location:
                raise SingleWriterUnavailableError(location, "UnsupportedMultiAccountBootstrap")
            return _writer_guard
        try:
            _writer_guard = gov.acquire_os_lock(location, timeout_seconds=0)
            os.set_inheritable(_writer_guard.handle.fileno(), False)
        except gov.AdmissionBusyTimeoutError:
            raise SingleWriterBusyError(location) from None
        except (gov.AdmissionUnavailableError, OSError) as exc:
            raise SingleWriterUnavailableError(location, type(exc).__name__) from None
        return _writer_guard
