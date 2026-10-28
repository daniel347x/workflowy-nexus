"""T03/E6: kernel-specific refusal, early entries and narrow suite override.

Entries run in private Scenario roots and ports; their waits are hang guards,
not idle-machine speed thresholds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from n06_harness import BUSY, OVERRIDE, PROBE_WARNING, WSL, Scenario, cleanup, ephemeral_port
from workflowy_mcp import server_bootstrap as bootstrap


@pytest.fixture(autouse=True)
def sensitivity_predicate(monkeypatch):
    """Explicit fixture-only neutralizations for archived red sensitivity runs."""
    mutation = os.environ.get("N06_TEST_MUTATION", "")
    if mutation == "predicate_false":
        monkeypatch.setattr(bootstrap, "is_wsl", lambda **kw: False)
    elif mutation == "predicate_not_windows":
        monkeypatch.setattr(bootstrap, "is_wsl", lambda **kw: kw.get("platform_name", sys.platform) != "win32")


@pytest.mark.parametrize("platform_name,release,expected", [
    ("win32", "microsoft-standard-WSL2", False),
    ("darwin", "microsoft WSL", False),
    ("linux", "6.8.0-generic", False),
    ("linux", "4.4.0-19041-Microsoft", True),
    ("linux", "5.15.167.4-microsoft-standard-WSL2", True),
    ("linux", "6.1-WSL", True),
    ("linux", "6.1-wsl1", True),
    ("linux", "WSL2", True),
    ("linux", "6.1-notwsl2suffix", False),
    ("linux", "6.1-my_wsl2_variant", False),
])
def test_kernel_truth_table_ignores_inherited_wsl_env(monkeypatch, platform_name, release, expected):
    monkeypatch.setenv("WSL_DISTRO_NAME", "copied-env")
    monkeypatch.setenv("WSL_INTEROP", "copied-env")
    actual = bootstrap.is_wsl(platform_name=platform_name, kernel_release=release)
    assert actual is expected


@pytest.mark.parametrize("platform_name", ["win32", "darwin"])
def test_non_linux_never_probes_kernel(monkeypatch, platform_name):
    def forbidden(*args, **kwargs):
        raise AssertionError("non-Linux kernel probe")
    monkeypatch.setattr(bootstrap.platform, "release", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert not bootstrap.is_wsl(platform_name=platform_name)


@pytest.mark.parametrize("version,expected", [("Linux version Microsoft WSL2", True), ("Linux ordinary", False)])
def test_empty_or_unavailable_release_reads_proc_once(monkeypatch, version, expected):
    reads = []
    def read(path, **kwargs):
        reads.append(path)
        return version
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(bootstrap.platform, "release", lambda: "")
    assert bootstrap.is_wsl(platform_name="linux") is expected
    assert reads == [Path("/proc/version")]
    reads.clear()
    def unavailable():
        raise OSError("no kernel release")
    monkeypatch.setattr(bootstrap.platform, "release", unavailable)
    assert bootstrap.is_wsl(platform_name="linux") is expected
    assert reads == [Path("/proc/version")]


def test_unreadable_kernel_warns_once_and_allows(monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "_probe_warning_emitted", False)
    monkeypatch.setattr(bootstrap.platform, "release", lambda: "")
    def unreadable(*args, **kwargs):
        raise PermissionError("test kernel unavailable")
    monkeypatch.setattr(Path, "read_text", unreadable)
    assert not bootstrap.is_wsl(platform_name="linux")
    assert not bootstrap.is_wsl(platform_name="linux")
    assert capsys.readouterr().err.splitlines() == [PROBE_WARNING]


@pytest.mark.parametrize("value", [None, "", "0", "true", "01", " 1", "1 ", "1"])
def test_override_is_exact_and_warning_once(monkeypatch, capsys, value):
    monkeypatch.setattr(bootstrap, "is_wsl", lambda **kw: True)
    monkeypatch.setattr(bootstrap, "_override_warning_emitted", False)
    if value is None:
        monkeypatch.delenv(bootstrap.TEST_ALLOW_WSL_ENV, raising=False)
    else:
        monkeypatch.setenv(bootstrap.TEST_ALLOW_WSL_ENV, value)
    if value == "1":
        bootstrap.refuse_wsl_server("full server")
        bootstrap.refuse_wsl_server("full server")
        assert bootstrap.is_wsl()
        assert capsys.readouterr().err.splitlines() == [OVERRIDE]
    else:
        with pytest.raises(SystemExit) as error:
            bootstrap.refuse_wsl_server("full server")
        assert error.value.code == 2
        assert capsys.readouterr().err.strip() == WSL.format(component="full server")


@pytest.mark.parametrize("entry,component", [
    ("wsl-cli-http", "full server"), ("wsl-cli-stdio", "full server"),
    ("wsl-package", "full server"), ("wsl-direct", "full server"),
    ("wsl-imported", "full server"), ("wsl-broker", "GLIMPSE broker"),
    ("wsl-broker-main", "GLIMPSE broker"), ("wsl-broker-module", "GLIMPSE broker"),
])
def test_wsl_entries_refuse_before_any_startup(entry, component):
    scenario = Scenario.create()
    result = scenario.run("entry", entry, WORKFLOWY_MCP_TEST_ALLOW_WSL_SERVER="")
    assert result.returncode == 2, result.stderr
    assert WSL.format(component=component) in result.stderr
    assert scenario.events("entry") == ["wsl-check"]
    for diagnostic in ("NUCLEAR LOGGING ACTIVE", "STDERR TEST", "Standard Output Redirection Test"):
        assert diagnostic not in result.stderr
    assert not Path(scenario.env["WORKFLOWY_BROKER_TOKEN_FILE"]).exists()


def test_exact_override_still_loses_to_real_held_guard():
    scenario = Scenario.create()
    holder = scenario.start("holder", fault="model_wsl")
    contender = None
    try:
        scenario.ready("holder")
        contender = scenario.start("contender", "stdio", fault=os.environ.get("N06_TEST_MUTATION", "model_wsl"),
                                   WORKFLOWY_MCP_HTTP_PORT=str(ephemeral_port()))
        result = scenario.outcome("contender", contender)
        assert result.returncode == 75, result.stderr
        path = (scenario.lane("holder") / "lock-path").read_text(encoding="utf-8")
        assert BUSY.format(path=path) in result.stderr
        if os.environ.get("N06_TEST_MUTATION") != "override_around_guard":
            assert result.stderr.count(OVERRIDE) == 1
        assert not {"client", "recovery", "broker", "bind", "service"}.intersection(scenario.events("contender"))
    finally:
        scenario.release("holder")
        cleanup(holder)
        if contender is not None:
            cleanup(contender)


def test_wsl_shim_is_not_refused(monkeypatch):
    from workflowy_mcp import daemon, shim
    from fastmcp.server.providers import proxy
    scenario = Scenario.create()
    scenario.install_environment(monkeypatch)
    monkeypatch.delenv(bootstrap.TEST_ALLOW_WSL_ENV, raising=False)
    monkeypatch.setattr(bootstrap, "is_wsl", lambda **kw: True)
    calls = []
    class FakeProxy:
        def __init__(self, **kwargs):
            self.middleware = kwargs.get("middleware", [])
            calls.append(("proxy", kwargs["identity"]))
        def run(self, **kwargs):
            calls.append(("serve", kwargs["transport"]))
    monkeypatch.setattr(daemon, "ensure_daemon_running", lambda: True)
    monkeypatch.setattr(daemon, "is_daemon_listening", lambda: True)
    monkeypatch.setattr(daemon, "shared_token", lambda: "n06-fake-broker-token")
    monkeypatch.setattr(proxy, "FastMCPProxy", FakeProxy)
    shim.main()
    assert calls == [("proxy", "upstream"), ("serve", "stdio")]
