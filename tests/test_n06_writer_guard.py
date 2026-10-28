"""T01/E2/E3 and T02/E4: real account locks, full entries and process death.

Processes run in private Scenario roots and ports; every wait is a hang guard,
not an elapsed acceptance threshold.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import httpx
import pytest

from coherence_harness import launch_subprocess, run_subprocess, touch, wait_for_file
from n06_harness import (
    BUSY, FAKE_KEY, FAKE_URL, SETUP_WAIT, UNAVAILABLE, Scenario,
    cleanup, collect, drain_output, ephemeral_port,
)
from workflowy_mcp.carto_job_governance import derive_account_scope_id
from workflowy_mcp.coherence.errors import CoherenceSchemaError
from workflowy_mcp.coherence.paths import coherence_paths, writer_lock_path


def expected_lock(scenario: Scenario, key: str = FAKE_KEY) -> Path:
    return (Path(scenario.env["WORKFLOWY_MCP_TEST_RUNTIME_DIR"]) / "cartographer_jobs" /
            ".coherence" / derive_account_scope_id(FAKE_URL, key) / "writer.lock").resolve()


def assert_order(events: list[str]) -> None:
    assert events[0] == "wsl-check"
    assert events.count("config") == 1
    assert events.count("guard") == 1
    assert events.index("config") < events.index("guard")
    for event in ("log", "client", "broker", "bind", "service"):
        if event in events:
            assert events.index("guard") < events.index(event), events
    assert {"client", "service"}.issubset(events)


def assert_http_alive(scenario: Scenario, port: str | None = None) -> None:
    response = httpx.post(
        f"http://127.0.0.1:{port or scenario.env['WORKFLOWY_MCP_HTTP_PORT']}/mcp",
        headers={"Authorization": "Bearer n06-fake-broker-token", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "n06-test", "version": "0"},
        }},
        timeout=120,
    )
    assert response.status_code == 200, response.text
    assert '"serverInfo"' in response.text


@pytest.mark.parametrize("entry", ["http", "stdio", "direct", "imported"])
def test_same_account_contender_loses_before_effects(entry):
    scenario = Scenario.create()
    holder = scenario.start("holder")
    contender = None
    try:
        scenario.ready("holder")
        assert_http_alive(scenario)
        contender = scenario.start("contender", entry, WORKFLOWY_MCP_HTTP_PORT=str(ephemeral_port()))
        result = scenario.outcome("contender", contender)
        assert result.returncode == 75, result.stderr
        assert BUSY.format(path=expected_lock(scenario)) in result.stderr
        assert FAKE_KEY not in result.stderr
        assert not {"guard", "client", "recovery", "broker", "bind", "service"}.intersection(scenario.events("contender"))
        assert holder.poll() is None
        assert_http_alive(scenario)
    finally:
        scenario.release("holder")
        if contender is not None:
            cleanup(contender)
        cleanup(holder)


def test_abrupt_death_releases_existing_file_without_finally():
    scenario = Scenario.create()
    holder = scenario.start("holder")
    successor = None
    try:
        scenario.ready("holder")
        path = expected_lock(scenario)
        assert path.is_file()
        original_inode = path.stat().st_ino
        touch(scenario.lane("holder") / "crash")
        scenario.release("holder")
        assert collect(holder).returncode == 73
        assert "lifespan-exit" not in scenario.events("holder")
        assert path.is_file()
        successor = scenario.start("successor")
        scenario.ready("successor")
        assert path.stat().st_ino == original_inode
        assert_order(scenario.events("successor"))
        assert_http_alive(scenario)
    finally:
        scenario.release("successor")
        cleanup(holder)
        if successor is not None:
            cleanup(successor)


def test_alias_urls_contend_but_different_accounts_do_not():
    scenario = Scenario.create()
    holder = scenario.start("holder")
    alias = other = None
    try:
        scenario.ready("holder")
        alias = scenario.start("alias", "stdio", WORKFLOWY_API_URL="https://N06.INVALID:443/api/v1///")
        result = scenario.outcome("alias", alias)
        assert result.returncode == 75, result.stderr
        assert BUSY.format(path=expected_lock(scenario)) in result.stderr
        other_port = str(ephemeral_port())
        other = scenario.start("other", WORKFLOWY_API_KEY="n06-fake-account-b", WORKFLOWY_MCP_HTTP_PORT=other_port)
        scenario.ready("other")
        assert expected_lock(scenario, "n06-fake-account-b") != expected_lock(scenario)
        assert_order(scenario.events("other"))
        assert_http_alive(scenario, other_port)
        assert_http_alive(scenario)
    finally:
        for name, process in (("holder", holder), ("alias", alias), ("other", other)):
            scenario.release(name)
            if process is not None:
                cleanup(process)


@pytest.mark.parametrize("entry", ["http", "stdio", "package-http", "package-stdio", "daemon", "imported"])
def test_every_full_entry_reaches_guard_before_startup_and_service(entry):
    scenario = Scenario.create()
    process = scenario.start("entry", entry)
    try:
        scenario.ready("entry")
        assert_order(scenario.events("entry"))
        assert Path((scenario.lane("entry") / "lock-path").read_text(encoding="utf-8")) == expected_lock(scenario)
    finally:
        scenario.release("entry")
        try:
            result = collect(process)
        finally:
            cleanup(process)
    assert result.returncode == 0, result.stderr


def test_direct_module_stdio_serves_with_binary_stdout_cf25():
    """CF-25 regression: the maintained direct module serves after the shared guard and preserves stderr diagnostics."""
    scenario = Scenario.create()
    process = scenario.start("direct", "direct")
    try:
        scenario.ready("direct")
        events = scenario.events("direct")
        assert_order(events)
        assert Path((scenario.lane("direct") / "lock-path").read_text(encoding="utf-8")) == expected_lock(scenario)
        assert "lifespan-ready" in events
        assert "service" in events
    finally:
        scenario.release("direct")
        try:
            result = collect(process)
        finally:
            cleanup(process)
    assert result.returncode == 0, result.stderr
    assert "'StderrRedirector' object has no attribute 'buffer'" not in result.stderr
    for diagnostic in ("NUCLEAR LOGGING ACTIVE", "STDERR TEST", "Standard Output Redirection Test"):
        assert diagnostic in result.stderr


@pytest.mark.parametrize("entry", ["http", "stdio", "direct", "imported"])
def test_unavailable_lock_is_74_not_busy_and_never_serves(entry):
    scenario = Scenario.create()
    path = expected_lock(scenario)
    path.mkdir(parents=True)  # A directory at the file path is a real open failure on either OS.
    process = scenario.start("entry", entry)
    try:
        result = scenario.outcome("entry", process)
        assert result.returncode == 74, result.stderr
        assert UNAVAILABLE.format(path=path, kind="AdmissionUnavailableError") in result.stderr
        assert FAKE_KEY not in result.stderr
        assert not {"client", "recovery", "broker", "bind", "service"}.intersection(scenario.events("entry"))
    finally:
        cleanup(process)


def test_real_bind_failure_exits_and_successor_acquires():
    scenario = Scenario.create()
    first = successor = None
    try:
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = str(occupied.getsockname()[1])
            assert int(port) not in (8765, 8766)
            first = scenario.start("bind-fail", WORKFLOWY_MCP_HTTP_PORT=port)
            result = scenario.outcome("bind-fail", first)
            assert result.returncode not in (0, 74, 75), result.stderr
            events = scenario.events("bind-fail")
            assert events.index("guard") < events.index("bind")
            assert "service" not in events
        successor = scenario.start("successor", WORKFLOWY_MCP_HTTP_PORT=port)
        scenario.ready("successor")
        assert_order(scenario.events("successor"))
    finally:
        scenario.release("successor")
        for process in (first, successor):
            if process is not None:
                cleanup(process)


def test_guard_survives_lifespan_teardown_until_process_exit():
    scenario = Scenario.create()
    holder = scenario.start("holder", "post-teardown")
    contender = None
    try:
        scenario.ready("holder")
        scenario.release("holder")
        assert wait_for_file(scenario.lane("holder") / "after-teardown", timeout=SETUP_WAIT)
        assert "lifespan-exit" in scenario.events("holder")
        assert holder.poll() is None
        contender = scenario.start("contender", "stdio")
        result = scenario.outcome("contender", contender)
        assert result.returncode == 75, result.stderr
    finally:
        touch(scenario.lane("holder") / "exit")
        cleanup(holder)
        if contender is not None:
            cleanup(contender)


def test_exec_child_cannot_inherit_writer_descriptor():
    scenario = Scenario.create()
    code = r'''
import os, subprocess, sys, time
from pathlib import Path
from workflowy_mcp.config import get_server_config
from workflowy_mcp.server_bootstrap import acquire_single_writer_guard
root = Path(sys.argv[1])
held = acquire_single_writer_guard(get_server_config(reload=True))
assert not os.get_inheritable(held.handle.fileno())
child = """import sys,time
from pathlib import Path
p=Path(sys.argv[1]); (p/'exec-ready').write_text('1')
# Outlive the holder's wait plus the successor's 300s setup budget.
end=time.monotonic()+600
while not (p/'exec-release').exists():
    if time.monotonic()>end: raise RuntimeError('exec test hung')
    time.sleep(.02)
(p/'exec-done').write_text('1')
"""
subprocess.Popen([sys.executable, '-c', child, str(root)], close_fds=False,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
end=time.monotonic()+300
while not (root/'exec-ready').exists():
    if time.monotonic()>end: raise RuntimeError('exec not ready')
    time.sleep(.02)
os._exit(73)
'''
    holder = successor = None
    try:
        holder = launch_subprocess(code, str(scenario.root), env=scenario.env)
        drain_output(holder)
        # Observe PROCESS death, not capture-pipe EOF. With close_fds=False,
        # Windows can inherit unrelated capture handles even though the writer
        # fd is explicitly non-inheritable; joining those readers would wait on
        # the deliberately surviving exec child and create a false deadlock.
        assert holder.wait(timeout=SETUP_WAIT) == 73
        assert (scenario.root / "exec-ready").exists()
        assert not (scenario.root / "exec-done").exists()
        successor = scenario.start("successor")
        scenario.ready("successor")
        assert not (scenario.root / "exec-done").exists()
    finally:
        touch(scenario.root / "exec-release")
        scenario.release("successor")
        if successor is not None:
            cleanup(successor)
        assert wait_for_file(scenario.root / "exec-done", timeout=SETUP_WAIT)
        if holder is not None:
            cleanup(holder)


def test_reentry_uses_one_attempt_and_second_scope_is_not_swapped():
    scenario = Scenario.create()
    result = run_subprocess(r'''
import os
from pydantic import SecretStr
from workflowy_mcp import carto_job_governance as gov, server_bootstrap as bootstrap
from workflowy_mcp.config import get_server_config
original=gov.acquire_os_lock
attempts=[]
def counted(path, **kw):
    attempts.append((path, kw))
    return original(path, **kw)
gov.acquire_os_lock=counted
config=get_server_config(reload=True)
first=bootstrap.acquire_single_writer_guard(config)
assert bootstrap.acquire_single_writer_guard(config) is first
assert len(attempts)==1 and attempts[0][1]=={'timeout_seconds':0}
assert not first.released and not os.get_inheritable(first.handle.fileno())
other=config.model_copy(update={'workflowy_api_key':SecretStr('n06-different-account')})
try:
    bootstrap.acquire_single_writer_guard(other)
except bootstrap.SingleWriterUnavailableError as exc:
    assert exc.code==74 and 'UnsupportedMultiAccountBootstrap' in str(exc)
else:
    raise AssertionError('silently changed account')
assert bootstrap.acquire_single_writer_guard(config) is first
assert len(attempts)==1 and not first.released
print('reentry-ok')
''', env=scenario.env, timeout=SETUP_WAIT)
    assert result.returncode == 0, result.stderr
    assert "reentry-ok" in result.stdout


def test_writer_path_is_pure_and_relocation_moves_whole_set(tmp_path):
    scope = "a" * 32
    root = tmp_path / "missing" / "jobs"
    paths = coherence_paths(scope, root)
    assert paths.writer_lock_path == root.resolve() / ".coherence" / scope / "writer.lock"
    assert writer_lock_path(scope, root) == paths.writer_lock_path
    assert not root.exists()
    other = coherence_paths(scope, tmp_path / "relocated")
    for field in paths.__dataclass_fields__:
        if field != "account_root":
            assert getattr(paths, field).relative_to(paths.account_root) == getattr(other, field).relative_to(other.account_root)


def test_writer_symlink_escape_is_rejected(tmp_path, monkeypatch):
    from workflowy_mcp.coherence import paths as module
    scope = "a" * 32
    account = tmp_path / "jobs" / ".coherence" / scope
    account.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.touch()
    link = account / "writer.lock"
    try:
        link.symlink_to(outside)
    except OSError:
        # Windows without symlink privilege: still exercise the real containment
        # check with a physically resolved escape, without adding a platform skip.
        physical = module._resolve_physical_normalized
        monkeypatch.setattr(module, "_resolve_physical_normalized", lambda path: outside if path == link else physical(path))
    with pytest.raises(CoherenceSchemaError, match="escapes account root"):
        writer_lock_path(scope, tmp_path / "jobs")


def test_containment_failure_is_typed_74_before_client_or_service():
    scenario = Scenario.create()
    result = run_subprocess(r'''
from pathlib import Path
from workflowy_mcp.config import get_server_config
from workflowy_mcp.carto_job_governance import derive_account_scope_id, get_jobs_root
from workflowy_mcp.coherence import paths
from workflowy_mcp.server_bootstrap import acquire_single_writer_guard
config=get_server_config(reload=True)
api=config.get_api_config()
scope=derive_account_scope_id(api.base_url, api.api_key.get_secret_value())
root=get_jobs_root()
account=root/'.coherence'/scope
account.mkdir(parents=True)
outside=root.parent/'outside-lock'
outside.touch()
link=account/'writer.lock'
try:
    link.symlink_to(outside)
except OSError:
    # Same real containment authority; Windows without symlink privileges
    # models only physical resolution, not lock acquisition or error mapping.
    physical=paths._resolve_physical_normalized
    paths._resolve_physical_normalized=lambda path: outside if path==link else physical(path)
acquire_single_writer_guard(config)
raise AssertionError('containment escape served')
''', env=scenario.env, timeout=SETUP_WAIT)
    assert result.returncode == 74, result.stderr
    assert "(CoherenceSchemaError)" in result.stderr
    assert "outside-lock" in result.stderr
    assert FAKE_KEY not in result.stderr
