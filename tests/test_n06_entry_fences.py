"""T02/E4 and N06a's T11 share: alias-aware startup/path ownership fences.

This is deliberately a small, closed startup surface, not a Python security
analyzer. Imports, simple callable aliases, path-producing wrappers and writer
path assignments are followed. Unknown full-transport run receivers fail the
closed entry census instead of being assumed guarded.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest
from test_cut_2b_fences import assert_worker_absent

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "workflowy_mcp"
MODULES = {
    "workflowy_mcp.server_bootstrap": "server_bootstrap.py",
    "workflowy_mcp.coherence.paths": "coherence/paths.py",
    "workflowy_mcp.__main__": "__main__.py",
    "workflowy_mcp.server": "server.py",
    "workflowy_mcp.daemon": "daemon.py",
    "workflowy_mcp.broker": "broker.py",
    "workflowy_mcp.shim": "shim.py",
    "workflowy_mcp.client.api_client_core": "client/api_client_core.py",
}
GUARD = "workflowy_mcp.server_bootstrap.acquire_single_writer_guard"


def sources() -> dict[str, str]:
    assert_worker_absent(PACKAGE / "weave_worker.py")
    return {module: (PACKAGE / path).read_text(encoding="utf-8") for module, path in MODULES.items()}


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return dotted(node.value) + "." + node.attr
    return "<dynamic>"


def imports(tree: ast.Module, module: str) -> dict[str, str]:
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = item.name if item.asname else item.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            package = module.rsplit(".", 1)[0]
            if node.level:
                package = ".".join(package.split(".")[:len(package.split(".")) - node.level + 1])
                prefix = package + ("." + node.module if node.module else "")
            else:
                prefix = node.module or ""
            for item in node.names:
                aliases[item.asname or item.name] = prefix + "." + item.name
    # Fixed point for simple aliases, including aliased imported callables.
    for _ in range(4):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                value = resolve(node.value, aliases)
                if value != "<dynamic>":
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id != value:
                            aliases.setdefault(target.id, value)
    return aliases


def resolve(node: ast.AST, aliases: dict[str, str]) -> str:
    """Resolve simple aliases and constant-name getattr on the bounded entry surface."""
    if (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
        and len(node.args) == 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
    ):
        return resolve(node.args[0], aliases) + "." + node.args[1].value
    value = dotted(node)
    head, *tail = value.split(".")
    return ".".join([aliases.get(head, head), *tail])


def definitions(tree: ast.Module, module: str):
    """Each call keeps its owning function, not merely a matching source string."""
    def walk(node, owner):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            owner = owner + "." + node.name
        yield node, owner
        for child in ast.iter_child_nodes(node):
            yield from walk(child, owner)
    return list(walk(tree, module))


def function(tree: ast.Module, name: str):
    return next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)


def assert_entry_fences(texts: dict[str, str]) -> None:
    trees = {module: ast.parse(text) for module, text in texts.items()}
    acquisitions = []
    guard_edges = []
    definitions_by_module = {}
    for module, tree in trees.items():
        aliases = imports(tree, module)
        owned = definitions(tree, module)
        definitions_by_module[module] = owned
        tainted = set()
        producers = {"workflowy_mcp.coherence.paths.writer_lock_path"}
        def writer(node):
            if isinstance(node, ast.Name) and node.id in tainted:
                return True
            if isinstance(node, ast.Attribute) and node.attr == "writer_lock_path":
                return True
            if isinstance(node, ast.Call) and resolve(node.func, aliases) in producers:
                return True
            return any(writer(child) for child in ast.iter_child_nodes(node))
        for _ in range(4):
            for node, owner in owned:
                if isinstance(node, ast.Assign) and writer(node.value):
                    tainted.update(target.id for target in node.targets if isinstance(target, ast.Name))
                if isinstance(node, ast.Return) and node.value is not None and writer(node.value):
                    producers.add(owner)
                    producers.add(owner.rsplit(".", 1)[-1])
        for node, owner in owned:
            if not isinstance(node, ast.Call):
                continue
            callee = resolve(node.func, aliases)
            if callee == GUARD:
                guard_edges.append(owner)
            if callee.rsplit(".", 1)[-1] in ("acquire_os_lock", "acquire_os_lock_async", "os_lock", "async_os_lock"):
                args = [*node.args, *(kw.value for kw in node.keywords)]
                if any(writer(arg) for arg in args):
                    acquisitions.append(owner)
                    assert owner == GUARD, f"foreign writer acquisition: {owner} -> {callee}"
                    assert {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords} == {"timeout_seconds": 0}
            # This is a maintained, enumerated app surface. A new/aliased app
            # run is not silently presumed to use server.lifespan.
            if callee.rsplit(".", 1)[-1] == "run" and any(
                kw.arg == "transport" and isinstance(kw.value, ast.Constant) and kw.value.value in ("http", "stdio")
                for kw in node.keywords
            ):
                if module == "workflowy_mcp.shim":
                    assert callee.rsplit(".", 1)[0] == "proxy"
                else:
                    assert owner in ("workflowy_mcp.__main__.main", "workflowy_mcp.daemon.run_http_daemon", "workflowy_mcp.server"), f"alternate full entry: {owner}"
                    receiver = callee.rsplit(".", 1)[0]
                    assert receiver in ("mcp", "workflowy_mcp.server.mcp"), f"unknown full app receiver: {receiver}"
            # Follow aliases for release too; accepting a renamed release would
            # make a retained global merely decorative.
            if module == "workflowy_mcp.server_bootstrap" or owner == "workflowy_mcp.server.lifespan":
                terminal = callee.rsplit(".", 1)[-1]
                assert callee == "platform.release" or terminal not in ("release", "_unlock_handle", "os_lock", "async_os_lock"), f"early writer release/context manager: {callee}"
                assert terminal not in ("kill", "terminate", "sleep", "Popen"), f"guard protocol/kill: {callee}"
                assert not (terminal == "close" and ".handle." in callee), f"early handle close: {callee}"
    assert acquisitions == [GUARD], acquisitions
    assert guard_edges == ["workflowy_mcp.server.lifespan"], f"guard outside shared lifespan: {guard_edges}"

    bootstrap = trees["workflowy_mcp.server_bootstrap"]
    guard = function(bootstrap, "acquire_single_writer_guard")
    assert not any(isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv") for node in ast.walk(guard)), "guard override"
    assert any(isinstance(node, ast.Global) and "_writer_guard" in node.names for node in guard.body)
    assert any(isinstance(node, ast.Call) and dotted(node.func) == "os.set_inheritable" and isinstance(node.args[-1], ast.Constant) and node.args[-1].value is False for node in ast.walk(guard))
    assignments = [node for node in ast.walk(guard) if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_writer_guard" for t in node.targets)]
    assert len(assignments) == 1 and isinstance(assignments[0].value, ast.Call), "guard retention reset"
    assert dotted(assignments[0].value.func) == "gov.acquire_os_lock"

    server = trees["workflowy_mcp.server"]
    lifespan = function(server, "lifespan")
    body = [node for node in lifespan.body if not isinstance(node, (ast.Global, ast.Expr)) or not isinstance(getattr(node, "value", None), ast.Constant)]
    calls = [node for node in ast.walk(lifespan) if isinstance(node, ast.Call)]
    first = next(node for node in body if not isinstance(node, ast.Global))
    assert isinstance(first, ast.Expr) and dotted(first.value.func) == "server_bootstrap.refuse_wsl_server", "lifespan refusal first"
    line = {dotted(call.func): call.lineno for call in calls}
    assert line["get_server_config"] < line["server_bootstrap.acquire_single_writer_guard"] < line["WorkFlowyClient"]
    # §14 A 939 removed the sweep wrapper and its startup call; the guard/refusal
    # ordering above is the retained N06 contract.
    assert "_sweep_carto_governance" not in ast.unparse(lifespan), "retired sweep wrapper in lifespan"
    assert sum(dotted(call.func) == "get_server_config" for call in calls) == 1
    app = next(node.value for node in server.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "mcp" for t in node.targets))
    assert any(kw.arg == "lifespan" and dotted(kw.value) == "lifespan" for kw in app.keywords), "unguarded app registration"
    direct = server.body[-1]
    assert isinstance(direct, ast.If)
    assert dotted(direct.body[0].value.func) == "server_bootstrap.refuse_wsl_server", "direct refusal after diagnostics"

    main = function(trees["workflowy_mcp.__main__"], "main")
    refusal = next(node.lineno for node in ast.walk(main) if isinstance(node, ast.Call) and dotted(node.func) == "refuse_wsl_server")
    server_import = next(node.lineno for node in ast.walk(main) if isinstance(node, ast.ImportFrom) and node.module == "workflowy_mcp.server")
    assert refusal < server_import
    broker = function(trees["workflowy_mcp.broker"], "run_broker")
    calls = [node for node in ast.walk(broker) if isinstance(node, ast.Call)]
    refusal = next(node.lineno for node in calls if dotted(node.func) == "refuse_wsl_server")
    assert all(refusal < node.lineno for node in calls if dotted(node.func) in ("get_or_create_token", "ensure_log_dir", "websockets.serve", "_log"))

    paths = trees["workflowy_mcp.coherence.paths"]
    filenames = [(module, node) for module, tree in trees.items() for node in ast.walk(tree) if isinstance(node, ast.Constant) and node.value == "writer.lock"]
    assert len(filenames) == 1 and filenames[0][0] == "workflowy_mcp.coherence.paths"
    assert sum(isinstance(node, ast.FunctionDef) and node.name == "writer_lock_path" for node in ast.walk(paths)) == 1


def injected(texts: dict[str, str], fault: str) -> dict[str, str]:
    changed = dict(texts)
    main = "workflowy_mcp.__main__"
    if fault == "alternate_stdio":
        changed[main] += '\nfrom fastmcp import FastMCP as Alternate\nAlternate("unguarded").run(transport="stdio")\n'
    elif fault == "foreign_lock":
        changed[main] += '\nfrom .coherence.paths import writer_lock_path\nfrom . import carto_job_governance as gov\ndef illegal(scope):\n    path = writer_lock_path(scope)\n    gov.acquire_os_lock(path, timeout_seconds=0)\n'
    elif fault == "alias_bypass":
        changed[main] += '\nfrom .coherence.paths import writer_lock_path as destination\nfrom .carto_job_governance import acquire_os_lock as renamed\ndef hidden(scope):\n    elsewhere = destination(scope)\n    alias = renamed\n    alias(elsewhere, timeout_seconds=0)\n'
    elif fault == "early_release":
        key = "workflowy_mcp.server_bootstrap"
        changed[key] = changed[key].replace('        return _writer_guard\n', '        _writer_guard.release()\n        return _writer_guard\n')
    elif fault == "bound_run_alias":
        changed[main] += '\ndef illegal():\n    runner = mcp.run\n    runner(transport="stdio")\n'
    elif fault == "getattr_writer_lock":
        changed[main] += '\nfrom .coherence.paths import writer_lock_path\nfrom . import carto_job_governance as gov\ndef illegal(scope):\n    path = writer_lock_path(scope)\n    getattr(gov, "acquire_os_lock")(path, timeout_seconds=0)\n'
    elif fault == "getattr_writer_lock_alias":
        changed[main] += '\nfrom .coherence.paths import writer_lock_path\nfrom . import carto_job_governance as gov\ndef illegal(scope):\n    path = writer_lock_path(scope)\n    take = getattr(gov, "acquire_os_lock")\n    take(path, timeout_seconds=0)\n'
    elif fault in {"lock_wrapper", "retire_governor_acquire", "foreign_os_caller"}:
        pass  # The lock-wrapper test mutates its own copy; keep this fixture inert.
    else:
        raise AssertionError(f"unknown fixture fault {fault}")
    return changed


def test_production_entry_fence():
    text = sources()
    mutation = os.environ.get("N06_TEST_MUTATION", "")
    if mutation.startswith("fence_"):
        text = injected(text, mutation.removeprefix("fence_"))
    assert_entry_fences(text)


@pytest.mark.parametrize("fault", ["alternate_stdio", "foreign_lock", "alias_bypass", "early_release", "bound_run_alias", "getattr_writer_lock", "getattr_writer_lock_alias"])
def test_fence_rejects_production_shaped_aliases_and_bypasses(fault):
    with pytest.raises(AssertionError):
        assert_entry_fences(injected(sources(), fault))


@pytest.mark.parametrize("fault", ["bound_run_alias", "getattr_writer_lock", "getattr_writer_lock_alias"])
def test_cf29_old_scanner_accepts_new_shapes_but_current_rejects(fault):
    """The three planted shapes expose precisely the old bounded scanner's gap."""
    import inspect
    old_resolve = '''def resolve(node, aliases):
    value = dotted(node)
    head, *tail = value.split(".")
    return ".".join([aliases.get(head, head), *tail])
'''
    current_run = '''            if callee.rsplit(".", 1)[-1] == "run" and any('''
    old_run = '''            if isinstance(node.func, ast.Attribute) and node.func.attr == "run" and any('''
    text = inspect.getsource(assert_entry_fences)
    assert text.count(current_run) == 1
    text = text.replace(current_run, old_run)
    text = text.replace('assert callee.rsplit(".", 1)[0] == "proxy"', 'assert dotted(node.func.value) == "proxy"')
    text = text.replace('receiver = callee.rsplit(".", 1)[0]', 'receiver = dotted(node.func.value)')
    text = text.replace('receiver in ("mcp", "workflowy_mcp.server.mcp")', 'receiver == "mcp"')
    namespace = dict(globals())
    exec(old_resolve + "\n" + inspect.getsource(imports) + "\n" + text, namespace)
    planted = injected(sources(), fault)
    namespace["assert_entry_fences"](planted)
    expected = "alternate full entry" if fault == "bound_run_alias" else "foreign writer acquisition"
    with pytest.raises(AssertionError, match=expected):
        assert_entry_fences(planted)


RETIRED_LOCK_WRAPPERS = ("os_lock", "async_os_lock", "acquire_os_lock_async")


def _lock_wrapper_definitions(source):
    return [
        node.name for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in RETIRED_LOCK_WRAPPERS
    ]


def _async_os_authority_callers(sources):
    if os.environ.get("N06_NEUTRALIZE_AUTHORITY_SURVIVAL"):
        return set()  # Test-only fence sensitivity.
    callers = set()
    for relative, source in sources.items():
        module = relative.removeprefix("src/").removesuffix(".py").replace("/", ".")
        tree = ast.parse(source)

        def visit(node, scope):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope = (*scope, node.name)
            if isinstance(node, ast.Call):
                leaf = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if leaf == "acquire_os_lock_async":
                    callers.add((module, ".".join(scope)))
            for child in ast.iter_child_nodes(node):
                visit(child, scope)

        visit(tree, ())
    return callers


def test_retired_lock_wrappers_absent_while_guard_and_governor_authority_survive():
    """Historical name: the guard stays; wrappers and governor OS lane are absent."""
    source = (PACKAGE / "carto_job_governance.py").read_text(encoding="utf-8")
    mutation = os.environ.get("N06_TEST_MUTATION", "")
    if mutation == "fence_lock_wrapper":
        source += "\n\ndef os_lock(path, **kwargs):\n    raise AssertionError('restored')\n"
    elif mutation == "fence_retire_governor_acquire":
        source += "\nasync def acquire_os_lock_async(path):\n    pass\n"
    elif mutation == "fence_foreign_os_caller":
        source += "\n\ndef illegal(scope):\n    acquire_os_lock_async(scope)\n"
    defined = _lock_wrapper_definitions(source)
    assert defined == [], f"retired lock wrapper definition: {defined}"
    assert "def acquire_os_lock(" in source, "the singleton OS authority disappeared"
    callers = _async_os_authority_callers({
        "src/workflowy_mcp/server_bootstrap.py": (PACKAGE / "server_bootstrap.py").read_text(encoding="utf-8"),
        "src/workflowy_mcp/carto_write_governor.py": (PACKAGE / "carto_write_governor.py").read_text(encoding="utf-8"),
        "src/workflowy_mcp/carto_job_governance.py": source,
        "src/workflowy_mcp/server.py": (PACKAGE / "server.py").read_text(encoding="utf-8"),
    })
    assert callers == set(), f"retired asynchronous OS authority caller: {callers}"


def test_retired_lock_wrapper_absence_rejects_source_copy():
    defined = _lock_wrapper_definitions("async def async_os_lock(path):\n    pass\n")
    if os.environ.get("N06_NEUTRALIZE_LOCK_WRAPPER_ABSENCE"):
        defined = []  # Test-only fence sensitivity.
    assert defined == ["async_os_lock"]


def test_async_os_authority_caller_inventory_rejects_source_copy():
    sources = {
        "src/workflowy_mcp/server_bootstrap.py": "",
        "src/workflowy_mcp/carto_write_governor.py": "",
        "src/workflowy_mcp/carto_job_governance.py": "",
        "src/workflowy_mcp/server.py": "",
        "src/workflowy_mcp/injected.py": "def leaked(scope):\n    acquire_os_lock_async(scope)\n",
    }
    callers = _async_os_authority_callers(sources)
    assert ("workflowy_mcp.injected", "leaked") in callers, callers


def test_scripts_and_guard_environment_contract():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for script, target in (("workflowy-mcp", "workflowy_mcp.__main__:main"), ("workflowy-mcp-shim", "workflowy_mcp.shim:main"), ("workflowy-glimpse-broker", "workflowy_mcp.broker:main")):
        assert re.search(rf'^{re.escape(script)}\s*=\s*"{re.escape(target)}"', pyproject, re.M)
    daemon = ast.parse((PACKAGE / "daemon.py").read_text(encoding="utf-8"))
    resolver = function(daemon, "resolve_transport")
    assert any(isinstance(node, ast.Constant) and node.value == "stdio" for node in ast.walk(resolver))
    bootstrap = (PACKAGE / "server_bootstrap.py").read_text(encoding="utf-8")
    envs = set(re.findall(r'WORKFLOWY_[A-Z0-9_]+', bootstrap))
    assert envs == {"WORKFLOWY_MCP_TEST_ALLOW_WSL_SERVER"}
    conftest = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert 'os.environ["WORKFLOWY_MCP_TEST_ALLOW_WSL_SERVER"] = "1"' in conftest
    assert 'os.environ.pop("WORKFLOWY_MCP_TEST_ALLOW_WSL_SERVER", None)' in conftest


def test_documentation_describes_delivered_n06_guard_and_handshake():
    readme = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
    config = (ROOT / "CONFIG_REFERENCE.md").read_text(encoding="utf-8")
    assert "<jobs_root>/.coherence/<32-hex account>/writer.lock" in readme
    assert "FATAL: another Workflowy full server already holds '{lock_path}'." in readme
    assert "FATAL: cannot acquire the Workflowy writer guard at '{lock_path}' ({exception_type})." in readme
    assert "Native Linux" in readme and "from a WSL shell" in readme
    assert "automatically drains/replaces an older" in readme
    assert "running old shim never fetches" in readme
    assert "upstream_outcome_unknown" in readme and "not retryable" in readme
    assert "Linux-process WSL shim" in readme and "Windows-side shim" in readme
    assert "separate maintenance" in readme and "without a cache clear" in readme
    assert "WORKFLOWY_MCP_TEST_ALLOW_WSL_SERVER=1" in config
    assert "exact value `1`" in config and "never the writer guard" in config
    assert "whole account coherence artifact set" in config
    assert "WORKFLOWY_MCP_DAEMON_DRAIN_TIMEOUT_S" in config
    assert "default/floor 3600" in config and "without an upper cap" in config
    assert "never admitted work" in config and "two spawn budgets" in config


def test_direct_module_authorized_hunk_preserves_all_existing_diagnostics():
    tree = ast.parse((PACKAGE / "server.py").read_text(encoding="utf-8"))
    direct = tree.body[-1]
    text = ast.unparse(direct)
    assert text.index("refuse_wsl_server") < text.index("root = logging.getLogger")
    assert 'mcp.run(transport=\'stdio\')' in text
    for diagnostic in ("NUCLEAR LOGGING ACTIVE", "STDERR TEST", "Standard Output Redirection Test"):
        assert diagnostic in text


def assert_ws_dispatch_fence(source: str) -> None:
    """Exact branch census, including one guarded protocol-2 ingress, no shadow pump."""
    from collections import Counter
    tree = ast.parse(source)
    expected = set("resolve_uuid_path list_anchors_for_widget refresh_nodes_export_cache set_cache_refresh_skip save_nodes_export_cache open_node_in_windsurf refresh_file_node refresh_folder_node bulk_apply_visible_nodes generate_markdown_file materialize_markdown_file classify_bulk_materialize bulk_materialize_markdown classify_bulk_f12p3 bulk_generate_markdown render_markdown_modal compile_slice_search f8_expansion_plan render_slice_modal render_slice_row render_lightning_dom_capture mutate_node_tags merge_slice_groups anchor_dock_update carto_list_jobs carto_clear_job_history carto_cancel_job carto_job_detail resolve_node_titles".split())
    constant = next(node.value for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "_WS_WORK_ACTIONS" for target in node.targets))
    assert isinstance(constant, ast.Call) and dotted(constant.func) == "frozenset"
    assert set(ast.literal_eval(constant.args[0])) == expected
    dispatcher = function(tree, "_dispatch_ws_work")
    body = next(node for node in dispatcher.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "dispatch")
    def actions(node):
            return [term.value for child in ast.walk(node) if isinstance(child, ast.Compare) and all(isinstance(op, ast.Eq) for op in child.ops) and dotted(child.left) == "action" for term in child.comparators if isinstance(term, ast.Constant) and isinstance(term.value, str)]
    actual = actions(body)
    assert set(actual) == expected, "missing or unregistered work branch"
    counts = Counter(actual)
    assert counts.pop("carto_list_jobs", 0) == 2, "legacy poll and protocol-2 ingress must both survive"
    assert all(count == 1 for count in counts.values()), "duplicated work branch"
    ingress = [node for node in body.body if isinstance(node, ast.If) and isinstance(node.test, ast.BoolOp)
               and actions(node.test) == ["carto_list_jobs"]]
    assert len(ingress) == 1, "exactly one guarded protocol-2 ingress"
    assert ast.dump(ingress[0].test) == ast.dump(ast.parse(
        "action == 'carto_list_jobs' and 'since' in data", mode="eval").body), "protocol ingress must test since presence"
    pump = function(tree, "websocket_handler")
    assert set(actions(pump)) == {"hello", "ping", "notify_node_mutated"}
    calls = [node for node in ast.walk(pump) if isinstance(node, ast.Call)]
    assert sum(dotted(call.func) == "_dispatch_ws_work" for call in calls) == 1
    assert not any(dotted(call.func).startswith("_handle_") or dotted(call.func) == "create_isolated_context_task" for call in calls)
    assert len([node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_dispatch_ws_work"]) == 1
    assert any(isinstance(node, ast.Call) and dotted(node.func) == "controller.run_admitted" for node in ast.walk(dispatcher))


def test_ws_one_body_and_exact_known_action_table():
    text = (PACKAGE / "server.py").read_text(encoding="utf-8")
    fault = os.environ.get("N06_TEST_MUTATION", "")
    if fault == "ws_missing_action":
        text = text.replace('"carto_list_jobs", "carto_clear_job_history", "carto_cancel_job",', '"carto_list_jobs", "carto_clear_job_history",')
    assert_ws_dispatch_fence(text)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "pump_branch", "unguarded_protocol", "missing_details"])
def test_ws_fence_detects_missing_duplicated_or_shadow_dispatch(fault):
    text = (PACKAGE / "server.py").read_text(encoding="utf-8")
    if fault == "missing":
        text = text.replace("if action == 'anchor_dock_update':", "if action == 'unregistered_alias':")
    elif fault == "duplicate":
        text += '\nasync def _dispatch_ws_work(data, websocket):\n    pass\n'
    elif fault == "pump_branch":
        text = text.replace("action in _WS_WORK_ACTIONS", "action == 'carto_list_jobs'")
    elif fault == "unguarded_protocol":
        text = text.replace("action == 'carto_list_jobs' and 'since' in data", "action == 'carto_list_jobs'")
    else:
        text = text.replace("action == 'carto_job_detail'", "action == 'unregistered_details'")
    with pytest.raises(AssertionError):
        assert_ws_dispatch_fence(text)


def restart_sources():
    return {"workflowy_mcp." + name: (PACKAGE / (name + ".py")).read_text(encoding="utf-8") for name in ("server_bootstrap", "daemon_lifecycle", "daemon", "shim")}


def assert_restart_no_cut_or_replay(texts):
    """Closed restart surface: aliases/getattr cannot hide a cut or forward retry."""
    for module, source in texts.items():
        tree = ast.parse(source)
        aliases = imports(tree, module)
        def callee(value):
            if isinstance(value, ast.Call) and resolve(value.func, aliases) == "getattr" and len(value.args) >= 2 and isinstance(value.args[1], ast.Constant):
                return resolve(value.args[0], aliases) + "." + str(value.args[1].value)
            return resolve(value, aliases)
        for _ in range(4):
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    value = callee(node.value)
                    if value != "<dynamic>":
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                aliases.setdefault(target.id, value)
        for node, owner in definitions(tree, module):
            if isinstance(node, ast.Call):
                called = callee(node.func)
                assert called.rsplit(".", 1)[-1] not in {"kill", "killpg", "terminate", "taskkill", "raise_signal", "signal", "cancel"}, f"restart cut: {owner} -> {called}"
                if called == "uvicorn.Config":
                    values = {kw.arg: kw.value for kw in node.keywords}
                    assert "timeout_graceful_shutdown" in values and isinstance(values["timeout_graceful_shutdown"], ast.Constant) and values["timeout_graceful_shutdown"].value is None, "HTTP graceful cut"
                if called == "self._exit_callback":
                    assert owner == "workflowy_mcp.daemon_lifecycle.DaemonLifecycle._coordinate_restart", "early exit request"
            if isinstance(node, ast.Attribute) and node.attr in {"SIGTERM", "SIGKILL"}:
                raise AssertionError("restart signal constant")
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Attribute) and target.attr == "should_exit" for target in node.targets):
                assert owner == "workflowy_mcp.daemon.run_http_daemon_async.request_exit", "foreign should_exit writer"
        if module.endswith(".shim"):
            client = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "HandshakingProxyClient")
            forward = next(node for node in client.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "call_tool_mcp")
            calls = [node for node in ast.walk(forward) if isinstance(node, ast.Call)]
            assert sum(isinstance(node.func, ast.Attribute) and node.func.attr == "call_tool_mcp" for node in calls) == 1, "forwarded invocation resent"
            assert not any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(forward)), "forward retry loop"
            for call in calls:
                assert callee(call.func).rsplit(".", 1)[-1] not in {"control", "ping", "installed_identity", "_load_installed_identity", "distribution", "read_text", "check_output", "run", "Popen"}, "per-call identity/control I/O"
            assert any(kw.arg == "mode" and isinstance(kw.value, ast.Constant) and kw.value.value == "legacy" for node in ast.walk(tree) if isinstance(node, ast.Call) for kw in node.keywords) or 'kwargs["mode"] = "legacy"' in source


def test_restart_surface_has_no_cut_replay_or_per_call_identity_io():
    texts = restart_sources()
    fault = os.environ.get("N06_TEST_MUTATION", "")
    if fault.startswith("restart_fence_"):
        texts = injected_restart(texts, fault.removeprefix("restart_fence_"))
    assert_restart_no_cut_or_replay(texts)


def injected_restart(texts, fault):
    texts = dict(texts)
    key = "workflowy_mcp.shim"
    if fault == "kill_alias":
        texts[key] += '\nfrom os import kill as stop\nalias = stop\nalias(123, 9)\n'
    elif fault == "getattr_terminate":
        texts[key] += '\nstop = getattr(child, "terminate")\nstop()\n'
    elif fault == "cancel_alias":
        texts[key] += '\nstop = admitted_task.cancel\nstop()\n'
    elif fault == "early_exit":
        texts["workflowy_mcp.daemon_lifecycle"] += '\ndef illegal(self):\n    self._exit_callback()\n'
    elif fault == "short_grace":
        texts["workflowy_mcp.daemon"] = texts["workflowy_mcp.daemon"].replace('timeout_graceful_shutdown=None', 'timeout_graceful_shutdown=2')
    elif fault == "per_call_identity":
        texts[key] = texts[key].replace('    async def call_tool_mcp(self, *args, **kwargs):\n', '    async def call_tool_mcp(self, *args, **kwargs):\n        loader = installed_identity\n        loader()\n', 1)
    elif fault == "resend":
        texts[key] = texts[key].replace('result = await super().call_tool_mcp(*args, **kwargs)', 'await super().call_tool_mcp(*args, **kwargs)\n            result = await super().call_tool_mcp(*args, **kwargs)')
    else:
        raise AssertionError(fault)
    return texts


@pytest.mark.parametrize("fault", ["kill_alias", "getattr_terminate", "cancel_alias", "early_exit", "short_grace", "per_call_identity", "resend"])
def test_restart_fence_rejects_production_shaped_aliases(fault):
    with pytest.raises(AssertionError):
        assert_restart_no_cut_or_replay(injected_restart(restart_sources(), fault))
