"""CUT-G1 E1/E5: real package dispatch and shim body, isolated from all services.

These are process/event witnesses: every real entry runs in a private Scenario
root and port, and its subprocess wait is a hang guard, never an elapsed
acceptance threshold. CUT_G1_TEST_MUTATION is a test-only source-overlay
selector for archived red receipts. It has no production reader and never
changes the working source.
"""
from __future__ import annotations

import ast
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from coherence_harness import run_subprocess
from n06_harness import SETUP_WAIT, WSL, Scenario

ROOT = Path(__file__).resolve().parents[1]
WARNING = "WARNING: unrecognized Workflowy MCP transport; using the stdio shim. Use --transport stdio for a guarded full stdio server or --transport http for the daemon."
DEFAULT_SENTENCE = "`workflowy-mcp` with no transport selector runs the stdio shim by default. Existing host command and env blocks need no change. `python -m workflowy_mcp` has the same behavior; `workflowy-mcp-shim` remains an explicit shim entry. Use `--transport stdio` (or `WORKFLOWY_MCP_TRANSPORT=stdio`) for the maintained guarded full stdio server, or `--transport http` (or env `http`) for the daemon. `--transport shim` explicitly selects the default; unknown selector values warn and use the shim."
LOCALITY_SENTENCE = "With no reachable daemon, a native shim dialing loopback keeps its existing cold-start recovery regardless of any previously observed peer. A Linux-process WSL shim or a shim dialing a non-loopback destination returns `daemon_unavailable` and never launches a local full server for that destination. Start the native-host shim there, or use the local loopback destination for a local daemon. Reachable destinations still forward; no DNS or interface discovery is performed."
TRANSPORT_ROW = "| `WORKFLOWY_MCP_TRANSPORT` | Package/console entry selector; unset or blank defaults to `shim`. `shim` uses the stdio proxy, `stdio` the maintained guarded full stdio server, `http` (also `streamable-http` / `streamable_http`) the guarded HTTP daemon. Values are stripped/lowercased; `--transport` wins, including an explicit blank. Unknown values warn on stderr and use the shim. Does not change `workflowy-mcp-shim`. |"
DIAL_ROW = "| `WORKFLOWY_MCP_HTTP_DIAL_HOST` | Existing concrete client destination override; takes precedence over the bind-host fallback, including with `--host`. Only a native shim dialing loopback (numeric loopback or `localhost`) may spawn a local daemon; WSL and non-loopback destinations are attach-only and use `daemon_unavailable` with the native-host remedy when absent. No DNS/interface discovery. |"


ENTRY_CHILD = r'''
import ast, contextlib, importlib.abc, importlib.util, io, json, os, runpy, socket, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
case = json.loads(sys.argv[1])
events, stderr, stdout = [], io.StringIO(), io.StringIO()
fault = case["fault"]
package = Path(case["package"])

def changed(text, old, new):
    assert text.count(old) == 1, (fault, old)
    return text.replace(old, new)

class SourceOverlay(importlib.abc.Loader):
    def __init__(self, name):
        self.name = name
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        module.__file__ = str(package / (self.name.rsplit(".", 1)[-1] + ".py"))
        exec(self.get_code(self.name), module.__dict__)
    def get_code(self, fullname):
        path = package / (self.name.rsplit(".", 1)[-1] + ".py")
        text = path.read_text(encoding="utf-8")
        if self.name.endswith(".__main__"):
            if fault == "early_refusal":
                text = changed(text, '    if transport == "shim":', '    from workflowy_mcp.server_bootstrap import refuse_wsl_server\n    refuse_wsl_server("full server")\n    if transport == "shim":')
            elif fault == "eager_server":
                text = changed(text, '    if transport == "shim":', '    from workflowy_mcp.server import mcp\n    if transport == "shim":')
            elif fault == "missing_return":
                text = changed(text, '        _shim.main()\n        return', '        _shim.main()')
            elif fault == "module_full":
                text = changed(text, '    main(sys.argv[1:])', '    main(["--transport", "stdio"])')
            elif fault == "argparse_choices":
                text = changed(text, '        metavar="TRANSPORT",', '        choices=["stdio", "http"],\n        metavar="TRANSPORT",')
            elif fault == "full_to_shim":
                text = changed(text, 'if transport == "shim":', 'if transport in ("shim", "stdio", "http"):')
            elif fault == "import_before_refusal":
                text = changed(text, '    refuse_wsl_server("full server")', '    from workflowy_mcp.server import mcp\n    refuse_wsl_server("full server")')
            elif fault == "drop_flags":
                text = changed(text, '    if args.host is not None:', '    if False:')
                text = changed(text, '    if args.port is not None:', '    if False:')
                text = changed(text, '    if args.path is not None:', '    if False:')
            elif fault == "help_eager":
                text = changed(text, '    args = _parse_args(argv)', '    from workflowy_mcp.server import mcp\n    args = _parse_args(argv)')
        elif self.name.endswith(".daemon"):
            if fault in ("default_stdio", "blank_stdio"):
                text = changed(text, '    if raw in ("", "shim"):', '    if not raw:\n        return "stdio"\n    if raw == "shim":')
            elif fault == "unknown_exit":
                text = changed(text, '    print(\n        "WARNING: unrecognized', '    raise SystemExit(2)\n    print(\n        "WARNING: unrecognized')
            elif fault == "env_first":
                text = changed(text, 'selected = explicit if explicit is not None else os.getenv(TRANSPORT_ENV_VAR)', 'selected = os.getenv(TRANSPORT_ENV_VAR) if os.getenv(TRANSPORT_ENV_VAR) is not None else explicit')
            elif fault == "alias_shim":
                text = changed(text, '("http", "streamable-http", "streamable_http")', '("http",)')
        return compile(text, str(path), "exec")

class ServerLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        module.mcp = SimpleNamespace(run=lambda **kw: events.append(["full-run", kw["transport"]]))

class EntryFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "workflowy_mcp.server":
            events.append(["server-import"])
            if not case["full"]:
                raise AssertionError("forbidden heavy-server import")
            return importlib.util.spec_from_loader(fullname, ServerLoader())
        if fullname in ("workflowy_mcp.config", "workflowy_mcp.models.config"):
            events.append(["config-load"])
            raise AssertionError("forbidden configuration load")
        if fullname in ("workflowy_mcp.__main__", "workflowy_mcp.daemon"):
            return importlib.util.spec_from_loader(fullname, SourceOverlay(fullname))

sys.meta_path.insert(0, EntryFinder())  # BEFORE importing the package entry.
from workflowy_mcp import daemon, server_bootstrap, shim
from workflowy_mcp.daemon_identity import InstalledIdentity
# Execute the harness's actual public assertion without importing its unrelated
# coherence fixtures (which themselves import configuration). Isolation was
# allocated by Scenario.create in the parent; no child socket is ever allowed.
harness = ast.parse((package.parents[1] / "tests/n06_harness.py").read_text(encoding="utf-8"))
scenario_class = next(node for node in harness.body if isinstance(node, ast.ClassDef) and node.name == "Scenario")
endpoint_method = next(node for node in scenario_class.body if isinstance(node, ast.FunctionDef) and node.name == "assert_endpoint")
endpoint_namespace = {}
exec(compile(ast.Module(body=[endpoint_method], type_ignores=[]), "<actual-Scenario.assert_endpoint>", "exec"), endpoint_namespace)
endpoint_namespace["assert_endpoint"](SimpleNamespace(env=case["environment"]))
if case["env"] is None:
    os.environ.pop(daemon.TRANSPORT_ENV_VAR, None)
else:
    os.environ[daemon.TRANSPORT_ENV_VAR] = case["env"]
if case.get("dial_fallback"):
    os.environ.pop(daemon.HTTP_DIAL_HOST_ENV_VAR, None)
server_bootstrap.is_wsl = lambda: case["wsl"]
os.environ.pop(server_bootstrap.TEST_ALLOW_WSL_ENV, None)
real_refusal = server_bootstrap.refuse_wsl_server
def refusal(component):
    events.append(["refusal", component])
    real_refusal(component)
server_bootstrap.refuse_wsl_server = refusal
def forbidden(*args, **kwargs):
    events.append(["forbidden-io"])
    raise AssertionError("forbidden socket/spawn/guard")
socket.create_connection = forbidden
socket.getaddrinfo = forbidden
subprocess.Popen = forbidden
server_bootstrap.acquire_single_writer_guard = forbidden
def ready(kind):
    observed = [kind, daemon.daemon_url(), daemon.http_bind_host(), os.environ[daemon.HTTP_PORT_ENV_VAR], daemon.http_path()]
    events.append(observed)
    if case.get("flags_case"):
        port = case["environment"][daemon.HTTP_PORT_ENV_VAR]
        host = "192.0.2.1" if case["dial_fallback"] else "127.0.0.1"
        assert observed == ["probe" if case["dial_fallback"] else "spawn", f"http://{host}:{port}/g1-test", "192.0.2.1", port, "/g1-test"], "flags must precede shim dial"
    return case["ready"]
daemon.ensure_daemon_running = lambda: ready("spawn")
daemon.is_daemon_listening = lambda: ready("probe")
def token():
    events.append(["token"])
    return "g1-fake-token"
daemon.shared_token = token
daemon.run_http_daemon = lambda mcp: events.append(["full-run", "http"])
shim.installed_identity = lambda: InstalledIdentity("1.0.1", "a"*40, 10, "git", "linux", "test-host", "a"*32)
from fastmcp.server.providers import proxy as proxy_module
class RecordingProxy:
    def __init__(self, **kwargs):
        events.append(["proxy", kwargs["identity"]])
        self.middleware = kwargs["middleware"]
    def run(self, **kwargs):
        events.append(["proxy-run", kwargs["transport"]])
proxy_module.FastMCPProxy = RecordingProxy
original_shim = shim.main
def record_shim():
    events.append(["shim-entry"])
    return original_shim()
shim.main = record_shim
code, exception = None, None
with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
    try:
        if case.get("resolve_only"):
            result = daemon.resolve_transport(case["explicit"])
            events.append(["resolved", result])
        elif case["module"]:
            sys.argv = ["workflowy_mcp", *case["argv"]]
            runpy.run_module("workflowy_mcp", run_name="__main__")
        else:
            from workflowy_mcp.__main__ import main
            main(case["argv"])
    except SystemExit as error:
        code = error.code
    except BaseException as error:
        exception = type(error).__name__ + ": " + str(error)
print(json.dumps({"events": events, "stdout": stdout.getvalue(), "stderr": stderr.getvalue(), "exit": code, "exception": exception}))
'''


def entry_case(*, argv=(), env=None, wsl=False, module=False, full=False, ready=True, **updates):
    scenario = Scenario.create()
    argv = [scenario.env["WORKFLOWY_MCP_HTTP_PORT"] if arg == "G1_PORT" else arg for arg in argv]
    case = dict(argv=argv, env=env, wsl=wsl, module=module, full=full, ready=ready,
                fault=os.environ.get("CUT_G1_TEST_MUTATION", ""), package=str(ROOT / "src/workflowy_mcp"),
                root=str(scenario.root), environment=scenario.env, **updates)
    result = run_subprocess(ENTRY_CHILD, json.dumps(case), env=scenario.environment(), timeout=SETUP_WAIT)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    value["port"] = scenario.env["WORKFLOWY_MCP_HTTP_PORT"]
    return value


def kinds(value):
    return [row[0] for row in value["events"]]


def assert_clean_shim(value, wsl=False):
    names = kinds(value)
    assert "refusal" not in names, "forbidden full-server refusal"
    assert "server-import" not in names, "forbidden heavy-server import"
    assert "config-load" not in names and "forbidden-io" not in names, "forbidden configuration/guard/I/O"
    assert "full-run" not in names, "forbidden full-app runner"
    assert value["exception"] is None and value["exit"] is None, value
    assert names.count("shim-entry") == names.count("proxy") == names.count("proxy-run") == 1, value
    assert ["proxy", "upstream"] in value["events"] and ["proxy-run", "stdio"] in value["events"]
    assert names.count("spawn") == (0 if wsl else 1)
    assert names.count("probe") == (1 if wsl else 0)


@pytest.mark.parametrize("wsl", [False, True])
def test_main_empty_argv_runs_real_shim_path(wsl):
    assert_clean_shim(entry_case(wsl=wsl), wsl)


@pytest.mark.parametrize("wsl", [False, True])
def test_package_module_empty_argv_runs_real_shim_path(wsl):
    assert_clean_shim(entry_case(wsl=wsl, module=True), wsl)


@pytest.mark.parametrize(("explicit", "env", "expected", "warn"), [
    (None, None, "shim", False), (None, "", "shim", False), (None, " \t", "shim", False),
    ("", "stdio", "shim", False), (" \t", "http", "shim", False),
    ("shim", "http", "shim", False), (" SHIM ", "stdio", "shim", False),
    ("stdio", "http", "stdio", False), (" StDiO ", "shim", "stdio", False),
    ("http", "shim", "http", False), (" HTTP ", "stdio", "http", False),
    ("streamable-http", "shim", "http", False), (" STREAMABLE_HTTP ", "shim", "http", False),
    (None, "shim", "shim", False), (None, " STDIO ", "stdio", False),
    (None, "http", "http", False), (None, "Streamable-HTTP", "http", False),
    (None, "streamable_http", "http", False),
    ("g1-secret-sentinel", None, "shim", True), (None, "g1-secret-sentinel", "shim", True),
    ("http", "g1-secret-sentinel", "http", False),
])
def test_transport_selection_cli_env_and_warnings(explicit, env, expected, warn):
    direct = entry_case(env=env, resolve_only=True, explicit=explicit)
    assert direct["events"] == [["resolved", expected]], direct
    assert direct["stdout"] == "" and direct["stderr"] == (WARNING + "\n" if warn else "")
    value = entry_case(argv=() if explicit is None else ("--transport", explicit), env=env, full=expected != "shim")
    assert value["exception"] is None and value["exit"] is None, value
    assert "g1-secret-sentinel" not in value["stderr"] + value["stdout"]
    assert value["stderr"].count(WARNING) == int(warn) and value["stdout"] == ""
    if expected == "shim":
        assert_clean_shim(value)
    else:
        assert ["full-run", expected] in value["events"] and "shim-entry" not in kinds(value)


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("via_env", [False, True])
@pytest.mark.parametrize("wsl", [False, True])
def test_explicit_full_modes_dispatch_after_refusal(transport, via_env, wsl):
    value = entry_case(argv=() if via_env else ("--transport", transport), env=transport if via_env else "shim", wsl=wsl, full=not wsl)
    assert "shim-entry" not in kinds(value), "explicit full mode entered shim"
    assert value["exception"] is None, value
    if wsl:
        assert value["exit"] == 2 and value["stderr"] == WSL.format(component="full server") + "\n"
        assert value["events"] == [["refusal", "full server"]], "server imported before WSL refusal"
    else:
        assert value["events"] == [["refusal", "full server"], ["server-import"], ["full-run", transport]], "full dispatch order"


@pytest.mark.parametrize("dial_fallback", [False, True])
def test_shim_http_flags_preserve_resolver_precedence(dial_fallback):
    # Only no-network recorders see these values; every real socket is forbidden.
    value = entry_case(argv=("--host", "192.0.2.1", "--port", "G1_PORT", "--path", "g1-test"), dial_fallback=dial_fallback, flags_case=True)
    host = "192.0.2.1" if dial_fallback else "127.0.0.1"
    row = next(row for row in value["events"] if row[0] in ("probe", "spawn"))
    port = value["port"]
    assert row == ["probe" if dial_fallback else "spawn", f"http://{host}:{port}/g1-test", "192.0.2.1", port, "/g1-test"], "flags must precede shim dial"
    assert value["exception"] is None and ["proxy-run", "stdio"] in value["events"]


@pytest.mark.parametrize("wsl", [False, True])
def test_default_wsl_unreachable_has_native_host_remedy(wsl):
    value = entry_case(wsl=wsl, ready=False)
    names = kinds(value)
    assert "refusal" not in names and "server-import" not in names
    assert names == ["shim-entry", "probe" if wsl else "spawn"]
    assert value["exit"] == 2 and value["exception"] is None
    url = f"http://127.0.0.1:{value['port']}/mcp"
    from workflowy_mcp.daemon_lifecycle import DaemonUnavailableError, RestartFailedError
    expected = DaemonUnavailableError(url) if wsl else RestartFailedError("spawn_failed", url=url)
    assert value["stderr"] == f"[workflowy-mcp shim] {expected}\n"
    if wsl:
        assert expected.error == {"code": "daemon_unavailable", "message": f"Workflowy daemon is not reachable at {url}; this shim cannot start it on that host OS. Start the native-host shim with workflowy-mcp-shim. No local full-server fallback was attempted.", "retryable": True, "retry_after": 30, "outcome": "not_sent", "recovery": "start_native_host_shim"}


def test_help_starts_nothing():
    value = entry_case(argv=("--help",))
    assert value["events"] == [], "help reached dispatch/import/I/O"
    assert value["exit"] == 0 and value["exception"] is None and value["stderr"] == ""
    text = " ".join(value["stdout"].split())
    for phrase in ("stdio shim by default", "shim (default), stdio (guarded full server)", "unknown values warn and use shim", "--transport TRANSPORT"):
        assert phrase in text


def test_direct_redirector_preserves_binary_stdout():
    tree = ast.parse((ROOT / "src/workflowy_mcp/server.py").read_text(encoding="utf-8"))
    adapter = next(node for node in tree.body[-1].body if isinstance(node, ast.ClassDef) and node.name == "StderrRedirector")
    fault = os.environ.get("CUT_G1_TEST_MUTATION", "")
    if fault == "redirector_no_buffer":
        init = next(node for node in adapter.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        init.body = [ast.Pass()]
    elif fault == "redirector_stderr_buffer":
        init = next(node for node in adapter.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        init.body = ast.parse("self.buffer = sys.stderr.buffer").body
    binary, errors = io.BytesIO(), io.BytesIO()
    original, stderr = io.TextIOWrapper(binary, encoding="utf-8"), io.TextIOWrapper(errors, encoding="utf-8")
    namespace = {"sys": SimpleNamespace(stderr=stderr)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[adapter], type_ignores=[])), "<actual-direct-adapter>", "exec"), namespace)
    redirector = namespace["StderrRedirector"](original)
    assert getattr(redirector, "buffer", None) is original.buffer, "binary stdout identity"
    sentinel = b"\x00g1-protocol\xff\n"
    redirector.buffer.write(sentinel)
    redirector.buffer.flush()
    assert binary.getvalue() == sentinel and errors.getvalue() == b"", "protocol bytes left stdout"
    redirector.write("diagnostic")
    redirector.flush()
    assert b"diagnostic" in errors.getvalue() and sentinel not in errors.getvalue()


def test_default_entry_documentation_and_script_bindings():
    import tomllib
    readme = " ".join((ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8").split())
    config = (ROOT / "CONFIG_REFERENCE.md").read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    main = (ROOT / "src/workflowy_mcp/__main__.py").read_text(encoding="utf-8")
    fault = os.environ.get("CUT_G1_TEST_MUTATION", "")
    if fault == "docs_old_default":
        readme = readme.replace(DEFAULT_SENTENCE, "`workflowy-mcp` still defaults to full stdio; the shim remains the separate client entry.")
    elif fault == "docs_old_locality":
        readme = readme.replace(LOCALITY_SENTENCE, "With no reachable daemon, a native shim keeps its existing cold-start recovery.")
    elif fault == "docs_old_help":
        main = main.replace("stdio shim by default", "stdio by default")
    elif fault == "docs_remap":
        project = project.replace('workflowy-mcp = "workflowy_mcp.__main__:main"', 'workflowy-mcp = "workflowy_mcp.shim:main"')
    assert DEFAULT_SENTENCE in readme and LOCALITY_SENTENCE in readme
    assert TRANSPORT_ROW in config and DIAL_ROW in config
    scripts = tomllib.loads(project)["project"]["scripts"]
    assert scripts == {"workflowy-mcp": "workflowy_mcp.__main__:main", "workflowy-mcp-shim": "workflowy_mcp.shim:main", "workflowy-glimpse-broker": "workflowy_mcp.broker:main"}
    assert "stdio shim by default" in main and "unknown values warn and use shim" in main
    for obsolete in ("still defaults to full stdio", "stdio (default; unchanged behaviour)", "Host JSON blobs change by one word", "Full server. stdio by default"):
        assert obsolete not in readme + project + main
