"""N06 isolated real-process entries; only external Workflowy/broker work is fake.

Faults are fixture arguments, never production environment settings. The
optional N06_TEST_MUTATION variable is consumed only by these tests to obtain
explicit red sensitivity receipts; it has no production reader.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from coherence_harness import launch_subprocess, run_subprocess, touch, wait_for_file

WAIT = 120
SETUP_WAIT = 300
FAKE_KEY = "n06-fake-account-a"
FAKE_URL = "https://n06.invalid/api/v1"
BUSY = "FATAL: another Workflowy full server already holds '{path}'. This process will not serve. Use workflowy-mcp-shim or connect to the existing daemon."
UNAVAILABLE = "FATAL: cannot acquire the Workflowy writer guard at '{path}' ({kind}). This process will not serve. Check access to this configured jobs directory; do not delete writer.lock."
WSL = "FATAL: the Workflowy {component} must not run inside WSL. Run the daemon and broker on Windows; use workflowy-mcp-shim or an HTTP client from WSL."
OVERRIDE = "WARNING: WORKFLOWY_MCP_TEST_ALLOW_WSL_SERVER=1 skips only the WSL refusal for tests; the writer guard remains mandatory."
PROBE_WARNING = "WARNING: WSL kernel identity is unavailable; allowing startup rather than refusing an ordinary Linux host."


def ephemeral_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert port not in (8765, 8766)
    return port


@dataclass
class Scenario:
    root: Path
    env: dict[str, str]
    processes: dict[str, subprocess.Popen[str]] = field(default_factory=dict)

    @classmethod
    def create(cls) -> Scenario:
        root = Path(os.environ["WORKFLOWY_MCP_TEST_RUNTIME_DIR"]) / "n06" / uuid.uuid4().hex
        root.mkdir(parents=True)
        config = root / "fake.config.json"
        config.write_text(json.dumps({"schemaVersion": 1, "paths": {"cartographerJobsDir": str(root / "configured-jobs")}}), encoding="utf-8")
        env = {
            # The SDK's stdio environment is a whitelist, not os.environ.copy.
            # Pin child encoding too, including Windows redirected stderr logs.
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "WORKFLOWY_MCP_TEST_RUNTIME_DIR": str(root / "runtime"),
            "WORKFLOWY_NEXUS_CONFIG": str(config),
            "WORKFLOWY_API_KEY": FAKE_KEY,
            "WORKFLOWY_API_URL": FAKE_URL,
            "WORKFLOWY_BROKER_TOKEN": "n06-fake-broker-token",
            "WORKFLOWY_BROKER_TOKEN_FILE": str(root / "never-read-token"),
            "WORKFLOWY_BROKER_HOST": "127.0.0.1",
            "WORKFLOWY_BROKER_DIAL_HOST": "127.0.0.1",
            "WORKFLOWY_BROKER_PORT": str(ephemeral_port()),
            "WORKFLOWY_MCP_HTTP_HOST": "127.0.0.1",
            "WORKFLOWY_MCP_HTTP_PORT": str(ephemeral_port()),
            "WORKFLOWY_MCP_HTTP_PATH": "/mcp",
            "WORKFLOWY_MCP_HTTP_STATELESS": "1",
            "WORKFLOWY_MCP_HTTP_DIAL_HOST": "127.0.0.1",
            "WORKFLOWY_MCP_BEARER": "n06-fake-broker-token",
            "WORKFLOWY_MCP_SHIM_CALL_TIMEOUT_S": "900",
            "WORKFLOWY_MCP_DAEMON_SPAWN_TIMEOUT_S": "90",
            "WORKFLOWY_MCP_DAEMON_DRAIN_TIMEOUT_S": "3600",
            "WORKFLOWY_MCP_PROFILE": "consolidated",
            "WORKFLOWY_MCP_TEST_ALLOW_WSL_SERVER": "1",
            "WORKFLOWY_FILE_LOGGING": "0",
            "WORKFLOWY_LOG_DIR": str(root / "logs"),
            "HOME": str(root / "home"),
            "USERPROFILE": str(root / "home"),
            "LOCALAPPDATA": str(root / "local"),
            "APPDATA": str(root / "appdata"),
            "XDG_STATE_HOME": str(root / "state"),
        }
        # Replace any inherited config/path overrides, without reading them.
        env["CARTOGRAPHER_JOBS_DIR"] = str(root / "configured-jobs")
        return cls(root, env)

    def lane(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(exist_ok=True)
        return path

    def environment(self, **updates: str) -> dict[str, str]:
        return {**self.env, **updates}

    def install_environment(self, monkeypatch) -> None:
        """Override inherited live endpoints/credentials before in-process N06 work."""
        for name, value in self.env.items():
            monkeypatch.setenv(name, value)
        self.assert_endpoint()

    def assert_endpoint(self) -> None:
        """No N06 connect may use the exported Windows endpoint or live ports."""
        from workflowy_mcp.daemon import daemon_url

        port = int(self.env["WORKFLOWY_MCP_HTTP_PORT"])
        assert port not in (8765, 8766)
        assert daemon_url() == f"http://127.0.0.1:{port}/mcp"

    def installation_overlay(self, name: str, *, commit: str, source_time: int) -> Path:
        """Real package overlay/stamp, not a production fake-identity setting."""
        source = Path(__file__).resolve().parents[1] / "src" / "workflowy_mcp"
        overlay = self.lane("install-" + name)
        shutil.copytree(source, overlay / "workflowy_mcp", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (overlay / "workflowy_mcp" / "_build_identity.py").write_text(
            f"SOURCE_IDENTITY_SCHEMA=1\nSOURCE_COMMIT={commit!r}\nSOURCE_COMMIT_TIME={source_time}\nSOURCE_IDENTITY_KIND='git'\n", encoding="utf-8",
        )
        distribution = overlay / "workflowy_nexus_mcp_tools-1.0.1.dist-info"
        distribution.mkdir()
        (distribution / "METADATA").write_text("Metadata-Version: 2.1\nName: workflowy-nexus-mcp-tools\nVersion: 1.0.1\n", encoding="utf-8")
        (distribution / "direct_url.json").write_text("{}", encoding="utf-8")
        (overlay / "sitecustomize.py").write_text(RESTART_BOOTSTRAP, encoding="utf-8")
        if os.environ.get("N06_TEST_MUTATION") == "shim_cached_cold_veto":
            shim_source = overlay / "workflowy_mcp" / "shim.py"
            text = shim_source.read_text(encoding="utf-8")
            old = "allow_spawn = not state.local_is_wsl\n"
            assert text.count(old) == 1
            shim_source.write_text(text.replace(old, "allow_spawn = not state.local_is_wsl and (state.instance_id is None or state.replacement_capable)\n"), encoding="utf-8")
        return overlay

    def overlay_environment(self, overlay: Path) -> dict[str, str]:
        """Prefer the stamped package, then the checkout's unmodified root helpers.

        sitecustomize runs before ``python -m`` adds the working directory.
        Native editable installs do not refresh force-included root modules;
        an overlay must not accidentally load those stale installed copies.
        """
        repo = Path(__file__).resolve().parents[1]
        return self.environment(
            PYTHONPATH=os.pathsep.join((str(overlay), str(repo / "tests"), str(repo))),
            _N06_OVERLAY_LANE=str(self.lane("upgrade")),
        )

    def start_overlay(self, environment: dict[str, str]) -> subprocess.Popen[str]:
        """Keep the actual package process handle on Windows as well as POSIX.

        An exec trampoline cannot supply the same process-death evidence on
        Windows. This child is the real package HTTP entry, not a new runner.
        """
        process = subprocess.Popen(
            [sys.executable, "-m", "workflowy_mcp", "--transport", "http"],
            env={**os.environ, **environment}, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, close_fds=True,
        )
        drain_output(process)
        return process

    def start(self, name: str, entry: str = "http", *, fault: str = "", **updates: str) -> subprocess.Popen[str]:
        fault = fault or os.environ.get("N06_TEST_MUTATION", "")
        process = launch_subprocess(CHILD, str(self.lane(name)), entry, fault, env=self.environment(**updates))
        drain_output(process)
        self.processes[name] = process
        return process

    def run(self, name: str, entry: str = "http", *, fault: str = "", **updates: str) -> subprocess.CompletedProcess[str]:
        fault = fault or os.environ.get("N06_TEST_MUTATION", "")
        return run_subprocess(CHILD, str(self.lane(name)), entry, fault, env=self.environment(**updates), timeout=SETUP_WAIT)

    def ready(self, name: str) -> None:
        deadline = time.monotonic() + SETUP_WAIT
        marker = self.lane(name) / "ready"
        while not marker.exists():
            process = self.processes.get(name)
            if process is not None and process.poll() is not None:
                result = collect(process)
                raise AssertionError(f"entry exited {result.returncode} before ready: {result.stderr}")
            assert time.monotonic() < deadline, self.events(name)
            time.sleep(0.02)

    def events(self, name: str) -> list[str]:
        path = self.lane(name) / "events"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def release(self, name: str) -> None:
        touch(self.lane(name) / "release")

    def outcome(self, name: str, process: subprocess.Popen[str]) -> subprocess.CompletedProcess[str]:
        """Observe exit OR forbidden readiness, without waiting for a live loser."""
        deadline = time.monotonic() + SETUP_WAIT
        while process.poll() is None:
            if (self.lane(name) / "ready").exists():
                raise AssertionError(f"contender served: {self.events(name)}")
            if time.monotonic() >= deadline:
                raise AssertionError(f"entry did not exit or serve: {self.events(name)}")
            time.sleep(0.02)
        return collect(process)


def drain_output(process: subprocess.Popen[str]) -> None:
    """Drain both pipes while alive: Windows pipes can fill before an exit marker."""
    captured = [[], []]
    threads = []
    for stream, chunks in zip((process.stdout, process.stderr), captured):
        def read(stream=stream, chunks=chunks):
            try:
                chunks.append(stream.read())
            finally:
                stream.close()
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        threads.append(thread)
    process._n06_output = captured, threads


def collect(process: subprocess.Popen[str], *, timeout: int = SETUP_WAIT) -> subprocess.CompletedProcess[str]:
    if hasattr(process, "_n06_output"):
        process.wait(timeout=timeout)
        captured, threads = process._n06_output
        for thread in threads:
            thread.join(timeout=timeout)
            assert not thread.is_alive(), "test-child output pipe did not close"
        stdout, stderr = ("".join(chunks) for chunks in captured)
    else:
        stdout, stderr = process.communicate(timeout=timeout)
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def cleanup(process: subprocess.Popen[str]) -> None:
    """Harness-only cleanup of our own test child, never a production restart."""
    if process.poll() is None:
        process.terminate()
    try:
        collect(process, timeout=WAIT)
    except subprocess.TimeoutExpired:
        process.kill()
        collect(process, timeout=WAIT)


# Executed only by the suite, with fake credentials and private runtime/home.
CHILD = r'''
import asyncio, ast, io, json, os, runpy, subprocess, sys, time
started_at = time.perf_counter()
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
lane, entry, fault = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
def record(event):
    with (lane / "events").open("a", encoding="utf-8") as stream:
        stream.write(event + "\n")
def ready():
    (lane / "ready").write_text("ready", encoding="utf-8")
async def released():
    deadline = time.monotonic() + 300
    while not (lane / "release").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("test release barrier timed out")
        await asyncio.sleep(0.02)
from workflowy_mcp import server_bootstrap as bootstrap
original_refusal = bootstrap.refuse_wsl_server
def checked_refusal(component):
    record("wsl-check")
    return original_refusal(component)
bootstrap.refuse_wsl_server = checked_refusal
if fault == "predicate_false":
    bootstrap.is_wsl = lambda **kw: False
elif fault == "predicate_not_windows":
    bootstrap.is_wsl = lambda **kw: sys.platform != "win32"
elif entry.startswith("wsl-") or fault == "model_wsl":
    # Kernel injection models WSL even in the native-Windows test run.
    bootstrap.is_wsl = lambda **kw: True

if entry in ("wsl-cli-http", "wsl-cli-stdio", "wsl-package"):
    # No server import: a broken early refusal is caught by the import oracle.
    import importlib.abc
    class ForbidServer(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname == "workflowy_mcp.server":
                record("forbidden-server-import")
                raise RuntimeError("WSL reached heavy server import")
    sys.meta_path.insert(0, ForbidServer())
    if entry == "wsl-package":
        sys.argv = ["workflowy_mcp", "--transport", "http"]
        runpy.run_module("workflowy_mcp", run_name="__main__")
    else:
        from workflowy_mcp.__main__ import main
        main(["--transport", entry.rsplit("-", 1)[1]])
    raise RuntimeError("WSL full entry returned")

if entry in ("wsl-broker", "wsl-broker-main", "wsl-broker-module"):
    from workflowy_mcp import broker
    def forbidden(*args, **kwargs):
        record("forbidden-broker-startup")
        raise RuntimeError("WSL reached broker side effect")
    broker.get_or_create_token = forbidden
    broker.ensure_log_dir = forbidden
    broker._log = forbidden
    if broker.websockets is not None:
        broker.websockets.serve = forbidden
    if entry == "wsl-broker":
        asyncio.run(broker.run_broker())
    elif entry == "wsl-broker-main":
        broker.main()
    else:
        # Exercise the actual module entry block in its real namespace.
        source = ast.parse(Path(broker.__file__).read_text(encoding="utf-8"))
        code = ast.Module(body=[source.body[-1]], type_ignores=[])
        namespace = dict(vars(broker), __name__="__main__")
        exec(compile(code, broker.__file__, "exec"), namespace)
    raise RuntimeError("WSL broker returned")

held_acquire = bootstrap.acquire_single_writer_guard
def acquiring(config):
    if fault in ("no_lock", "override_around_guard") and (
        fault == "no_lock" or os.environ.get(bootstrap.TEST_ALLOW_WSL_ENV) == "1"
    ):
        record("fault-no-lock")
        return SimpleNamespace(path="no-lock")
    held = held_acquire(config)
    record("guard")
    (lane / "lock-path").write_text(str(held.path), encoding="utf-8")
    if fault == "early_release":
        held.release()
        record("fault-release")
    return held
bootstrap.acquire_single_writer_guard = acquiring

http_server = None
stdin_writer = None
import fastmcp
fastmcp.settings.show_server_banner = False
from fastmcp.server.mixins import transport as framework_transport
real_stdio = framework_transport.stdio_server
@asynccontextmanager
async def stdio(*args, **kwargs):
    # Observe the real SDK stream entry, not merely the earlier lifespan yield.
    # No stream adapter or transport authority is replaced.
    async with real_stdio(*args, **kwargs) as streams:
        record("service")
        ready()
        yield streams
framework_transport.stdio_server = stdio
import uvicorn
real_startup = uvicorn.Server.startup
async def startup(self, *args, **kwargs):
    global http_server
    http_server = self
    record("bind")
    await real_startup(self, *args, **kwargs)
    if self.started:
        record("service")
        (lane / "cold-start.json").write_text(json.dumps({"seconds": time.perf_counter() - started_at}), encoding="utf-8")
        ready()
uvicorn.Server.startup = startup

def install(namespace):
    from workflowy_mcp import broker_protocol
    broker_protocol.gc_logs = lambda: None
    original_config = namespace["get_server_config"]
    def config(**kw):
        record("config")
        return original_config(**kw)
    namespace["get_server_config"] = config
    namespace["setup_logging"] = lambda config: record("log")
    class FakeClient:
        def __init__(self, config, **kw):
            record("client")
            self.config = config
            snapshot = os.environ.get('_N06_SYNTHETIC_SNAPSHOT')
            if snapshot:
                from coherence_harness import make_boundary_capable
                nodes = json.loads(Path(snapshot).read_text(encoding='utf-8'))['nodes']
                make_boundary_capable(self, nodes=nodes)
                record('cache-bootstrap')
                (lane / 'node-count').write_text(str(len(self._cache_view_publisher.capture().nodes_by_id)), encoding='utf-8')
        async def close(self):
            record("client-close")
    namespace["WorkFlowyClient"] = FakeClient
    async def broker():
        record("broker")
        await asyncio.Event().wait()
    namespace["connect_to_broker"] = broker
    async def watcher():
        await released()
        if (lane / "crash").exists():
            os._exit(73)
        if http_server is not None:
            http_server.should_exit = True
        elif stdin_writer is not None:
            os.close(stdin_writer)
    namespace["_carto_jobs_watcher_loop"] = watcher
    original = namespace["lifespan"]
    @asynccontextmanager
    async def recorded(app):
        async with original(app):
            record("lifespan-ready")
            if entry in ("imported", "post-teardown"):
                record("service")
                ready()
            yield
        record("lifespan-exit")
    namespace["mcp"]._lifespan = recorded

if entry in ("direct", "wsl-direct"):
    # Run the real direct-module entry. Hook only after definitions/registration
    # have executed, before its module guard; external seams alone are replaced.
    import importlib.util
    target = importlib.util.find_spec("workflowy_mcp.server").origin
    source = ast.parse(Path(target).read_text(encoding="utf-8"))
    guard_line = source.body[-1].lineno
    def trace(frame, event, arg):
        if event == "line" and frame.f_code.co_filename == target and frame.f_lineno == guard_line:
            install(frame.f_globals)
            sys.settrace(None)
            return None
        return trace
    sys.settrace(trace)
else:
    from workflowy_mcp import server
    install(vars(server))

if entry in ("stdio", "package-stdio", "direct"):
    reader, stdin_writer = os.pipe()
    sys.stdin = io.TextIOWrapper(os.fdopen(reader, "rb", buffering=0), encoding="utf-8")
if entry in ("direct", "wsl-direct"):
    runpy.run_module("workflowy_mcp.server", run_name="__main__")
elif entry == "wsl-imported":
    async def run():
        async with server.mcp._lifespan_manager():
            raise RuntimeError("WSL imported app served")
    asyncio.run(run())
elif entry in ("imported", "post-teardown"):
    async def run():
        async with server.mcp._lifespan_manager():
            await released()
        if entry == "post-teardown":
            (lane / "after-teardown").write_text("still alive", encoding="utf-8")
            deadline = time.monotonic() + 300
            while not (lane / "exit").exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("test exit barrier timed out")
                await asyncio.sleep(0.02)
    asyncio.run(run())
elif entry == "daemon":
    from workflowy_mcp.daemon import run_http_daemon
    run_http_daemon(server.mcp)
elif entry.startswith("package-"):
    sys.argv = ["workflowy_mcp", "--transport", entry.split("-", 1)[1]]
    runpy.run_module("workflowy_mcp", run_name="__main__")
else:
    from workflowy_mcp.__main__ import main
    main(["--transport", entry])
record("returned")
'''


# Only test-overlay processes load this file. It changes external dependencies
# and records real authorities; package entries/guard/lifecycle/runner/shim are
# production code. No daemon identity is supplied through an environment flag.
RESTART_BOOTSTRAP = r'''
import asyncio, atexit, json, os, sys, time
from pathlib import Path
from workflowy_mcp import daemon, server_bootstrap
# Every overlay child (including the shim before main) verifies its ACTUAL
# configured endpoint before any probe, connection or listener is possible.
assert daemon.http_dial_host() == '127.0.0.1'
assert daemon.http_port() not in (8765, 8766)
assert daemon.daemon_url() == f'http://127.0.0.1:{daemon.http_port()}/mcp'
# Fixture-only platform inputs, independent of inherited WSL shell variables.
server_bootstrap.is_wsl = lambda **kwargs: os.environ.get('_N06_SHIM_IS_WSL') == '1'
if os.environ.get('_N06_SHIM_PLATFORM'):
    from dataclasses import replace
    from workflowy_mcp import daemon_identity
    daemon_identity._snapshot = replace(daemon_identity.installed_identity(), platform=os.environ['_N06_SHIM_PLATFORM'])
if '--transport' in sys.argv and 'http' in sys.argv:
    server_bootstrap.is_wsl = lambda **kwargs: False
    from workflowy_mcp import daemon, daemon_identity, daemon_lifecycle
    lane = Path(os.environ['_N06_OVERLAY_LANE'])
    assert daemon.http_dial_host() == '127.0.0.1'
    assert daemon.http_port() not in (8765, 8766)
    identity = daemon_identity.installed_identity()
    if os.environ.get('_N06_DAEMON_PLATFORM'):
        from dataclasses import replace
        identity = replace(identity, platform=os.environ['_N06_DAEMON_PLATFORM'])
        daemon_identity._snapshot = identity
    suffix = identity.commit[0]
    def record(kind, **fields):
        with (lane / ('events-' + suffix)).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'kind':kind, 'instance_id':identity.instance_id, **fields}) + '\n')
    served = False
    atexit.register(lambda: (lane / ('exited-' + suffix)).write_text('exited', encoding='utf-8') if served else None)
    original_guard = server_bootstrap.acquire_single_writer_guard
    def guard(config):
        try:
            held = original_guard(config)
        except SystemExit as error:
            if error.code == 75:
                record('guard-busy')
                (lane / 'guard-busy').write_text('busy', encoding='utf-8')
            raise
        record('guard', path=str(held.path))
        return held
    server_bootstrap.acquire_single_writer_guard = guard
    original_request = daemon_lifecycle.DaemonLifecycle.request_restart
    def request(self, body):
        status, reply = original_request(self, body)
        if reply.get('accepted'):
            record('accepted', restart_id=reply['restart_id'])
            (lane / 'draining').write_text(reply['restart_id'], encoding='utf-8')
            if os.environ.get('N06_TEST_MUTATION') == 'restart_early_exit':
                self._exit_callback()  # fixture-only neutralization of drain-before-exit
        return status, reply
    daemon_lifecycle.DaemonLifecycle.request_restart = request
    import fastmcp, uvicorn
    fastmcp.settings.show_server_banner = False
    from coherence_harness import make_boundary_capable
    from workflowy_mcp import server, broker_protocol, mutation_locks
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary
    broker_protocol.gc_logs = lambda: None
    server.setup_logging = lambda config: None
    class FakeClient:
        def __init__(self, config, **kwargs):
            self.config = config
            from types import SimpleNamespace
            self.api_key = 'n06-fake-key-never-connect'
            self._write_governor = SimpleNamespace(paths=SimpleNamespace(
                jobs_root=lane / 'n06-private-claims', account_scope_id='e' * 32))
            make_boundary_capable(self, nodes=[{'id':'06060606-0000-4000-8000-000000000003','nm':'fake','parent_id':None}])
            record('bootstrap')
        async def close(self):
            record('client-close')
    server.WorkFlowyClient = FakeClient
    async def idle():
        await asyncio.Event().wait()
    server.connect_to_broker = idle
    if os.environ.get('_N06_HIDE_RESTART_METADATA') == '1':
        from fastmcp.server.middleware import Middleware
        class LegacyNegotiation(Middleware):
            async def on_initialize(self, context, call_next):
                result = await call_next(context)
                return result.model_copy(update={'meta': {key:value for key,value in (result.meta or {}).items() if key != daemon_identity.DAEMON_META_KEY}})
        # Fixture-only metadata-less peer; keep the real guard/runner/lifespan.
        server.mcp.middleware.insert(0, LegacyNegotiation())
    http_server = None
    async def watcher():
        deadline = time.monotonic() + 600
        while not (lane / 'quit').exists():
            if suffix == 'a' and (lane / 'crash').exists():
                record('crash')
                os._exit(73)  # fixture-only abrupt death, no finally/unlock
            if suffix == 'a' and (lane / 'retire-old').exists():
                break  # Fixture-only idle legacy daemon disappearance.
            if time.monotonic() >= deadline:
                raise RuntimeError('test cleanup barrier timed out')
            await asyncio.sleep(0.02)
        # Test cleanup only, not automatic replacement. Automatic exit is the
        # untouched lifecycle/owned runner's actual coordinator callback.
        if http_server is not None:
            http_server.should_exit = True
    server._carto_jobs_watcher_loop = watcher
    original_setattr = uvicorn.Server.__setattr__
    def setattr_server(self, name, value):
        if name == 'should_exit' and value is True:
            record('exit-request')
            (lane / ('exit-request-' + suffix)).write_text('requested', encoding='utf-8')
        original_setattr(self, name, value)
    uvicorn.Server.__setattr__ = setattr_server
    original_startup = uvicorn.Server.startup
    async def startup(self, *args, **kwargs):
        global http_server, served
        await original_startup(self, *args, **kwargs)
        http_server = self
        if self.started:
            served = True
            public = daemon_lifecycle.active_lifecycle().metadata()
            record('service', daemon=public, interpreter=sys.executable)
            (lane / ('ready-' + suffix)).write_text(json.dumps(public), encoding='utf-8')
    uvicorn.Server.startup = startup
    if os.environ.get('_N06_HOLD_EXIT') == '1':
        original_serve = uvicorn.Server.serve
        async def serve(self, *args, **kwargs):
            await original_serve(self, *args, **kwargs)
            (lane / 'port-closed').write_text('closed; guard retained', encoding='utf-8')
            deadline = time.monotonic() + 300
            while not (lane / 'release-exit').exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError('test guard gap barrier timed out')
                await asyncio.sleep(0.02)
        uvicorn.Server.serve = serve
    @server.mcp.tool(name='n06_test_write')
    async def write(value: str) -> dict:
        async with cache_operation_boundary(server.get_client(), 'n06-test-write'):
            async with mutation_locks.mutation_node_locks('06060606-0000-4000-8000-000000000003', client=server.get_client()):
                record('effect', value=value)
                if value == 'held':
                    (lane / 'effect-held').write_text('sent', encoding='utf-8')
                    deadline = time.monotonic() + 300
                    while not (lane / 'release-write').exists():
                        if time.monotonic() >= deadline:
                            raise RuntimeError('test write barrier timed out')
                        await asyncio.sleep(0.02)
                record('publish', value=value)
                return {'value':value, 'commit':identity.commit}
else:
    # Observe the public base connection seam, never replace negotiation or
    # expose exception strings/headers. Useful for rare concurrent exit races.
    from fastmcp.server.providers.proxy import ProxyClient
    from uuid import uuid4
    negotiation_log = Path(os.environ['_N06_OVERLAY_LANE']) / ('negotiations-' + uuid4().hex + '.jsonl')
    original_enter = ProxyClient.__aenter__
    async def observed_enter(self):
        try:
            return await original_enter(self)
        except BaseException as error:
            try:
                pending, seen, types = [error], set(), []
                while pending:
                    current = pending.pop()
                    if id(current) in seen:
                        continue
                    seen.add(id(current))
                    types.append(type(current).__module__ + '.' + type(current).__name__)
                    pending.extend(child for child in getattr(current, 'exceptions', ()) if isinstance(child, BaseException))
                    pending.extend(child for child in (current.__cause__, current.__context__) if child is not None)
                with negotiation_log.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({'exceptions': types, 'listening':daemon.is_daemon_listening()}) + '\n')
            except Exception:
                pass
            raise
    ProxyClient.__aenter__ = observed_enter
    original_call = ProxyClient.call_tool_mcp
    async def observed_call(self, *args, **kwargs):
        try:
            return await original_call(self, *args, **kwargs)
        except BaseException as error:
            # The production adapter intentionally emits only the honest
            # unknown-outcome envelope. Keep original failure locations here,
            # in the isolated fixture, without messages, headers or payloads.
            pending, seen, failures = [error], set(), []
            while pending:
                current = pending.pop()
                if id(current) in seen:
                    continue
                seen.add(id(current))
                frames, trace = [], current.__traceback__
                while trace is not None:
                    frames.append({'function':trace.tb_frame.f_code.co_name,'line':trace.tb_lineno})
                    trace = trace.tb_next
                failures.append({'type':type(current).__module__+'.'+type(current).__name__,'frames':frames})
                pending.extend(child for child in getattr(current, 'exceptions', ()) if isinstance(child, BaseException))
                pending.extend(child for child in (current.__cause__, current.__context__) if child is not None)
            with negotiation_log.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'forward_failure':failures}) + '\n')
            raise
    ProxyClient.call_tool_mcp = observed_call
'''
