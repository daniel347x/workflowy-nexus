"""N07 production job reachability, context and exact registration fences.

The existing S03 scanner, identity grammar and assertions are not modified.
6LXJMA grants one exact scheduler data registration, not a broader escape.
"""

from __future__ import annotations

import pytest

import coherence_s03_fence_scanner as scanner
from n07_job_harness import scrubbed_runtime, task_core_environment, claimed_task_environment, real_job_environment, launch_real_job, reject_raw_job_cancel
from test_n07_carto_final_refresh import final_environment


_JOB_FRONTS = (
    "_handle_refresh_file_node", "_handle_refresh_folder_node", "_handle_materialize_markdown_file",
    "_handle_bulk_materialize_markdown", "_beacon_map_refresh_impl", "cartographer_refresh_async",
    "_handle_generate_markdown_file", "_handle_bulk_generate_markdown", "_handle_bulk_apply_visible_nodes",
)
_PROCESS_MEMBERS = {
    "subprocess": {"Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "multiprocessing": {"Process", "Pool", "get_context"},
    "concurrent.futures": {"ProcessPoolExecutor"},
    "sys": {"exit"},
    "os": {"system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "startfile",
           "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
           "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe"},
}


def _n07_runtime_sources():
    """The established source inventory includes packaged and root vendor code."""
    return {path.relative_to(scanner.REPO).as_posix(): path.read_text(encoding="utf-8")
            for path in scanner.production_files()}


def _process_graph(sources):
    """Conservative AST closure, including function values and nested callbacks.

    This is not Python type inference. Unresolved method receivers connect to
    every definition with that method name; function values are edges even
    before a callback is invoked. Imported/local callable aliases and constant
    getattr resolve transitively. Dynamic selection from a process module is a
    forbidden factory, never an excuse to drop an edge. No file is exempted.
    """
    import ast
    from collections import defaultdict
    definitions, by_name, nodes, modules, imports, parents = {}, defaultdict(set), {}, {}, {}, {}
    module_assignments, classes = {}, {}
    for rel, text in sources.items():
        module = rel.removesuffix(".py").removeprefix("src/").replace("/", ".")
        tree = ast.parse(text, filename=rel)
        module_assignments[module] = [node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))]
        parent_map = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        aliases = defaultdict(set)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    aliases[item.asname or item.name.split(".")[0]].add(item.name if item.asname else item.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    prefix = ".".join([*module.split(".")[:-node.level], *([prefix] if prefix else [])])
                for item in node.names:
                    aliases[item.asname or item.name].add(prefix + "." + item.name)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            lineage, cursor = [node.name], node
            while cursor in parent_map:
                cursor = parent_map[cursor]
                if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    lineage.append(cursor.name)
            qualified = ".".join(reversed(lineage))
            key = module + ":" + qualified
            definitions[key] = (rel, node.lineno)
            by_name[node.name].add(key)
            nodes[key], modules[key], imports[key], parents[key] = node, module, aliases, parent_map
            if qualified.endswith(".__init__") or qualified.endswith(".__new__"):
                class_name = qualified.rsplit(".", 1)[0]
                classes.setdefault(module + "." + class_name, set()).add(key)
                by_name[class_name.rsplit(".", 1)[-1]].add(key)
    edges, sinks = defaultdict(set), defaultdict(set)
    canonical = {key.replace(":", "."): key for key in definitions}

    def process_symbol(name):
        if name.rsplit(".", 1)[-1] in {"_spawn_governed_worker", "popen_factory", "Popen", "ProcessPoolExecutor",
                                          "adopt_carto_worker_claim", "pin_worker_operation_cache", "CartoHeartbeatController", "_adopt_phase8"}:
            return True
        if name.startswith("workflowy_mcp.weave_worker.main"):
            return True
        return any(name == prefix + "." + member or name == "<dynamic:" + prefix + ">"
                   for prefix, members in _PROCESS_MEMBERS.items() for member in members)

    for key, function in nodes.items():
        module, parent_map = modules[key], parents[key]
        aliases = {name: set(values) for name, values in imports[key].items()}
        lexical = key.rsplit(":", 1)[1].split(".")[:-1]
        # Traverse nested callback bodies as their own nodes; constructing a
        # callback still connects its parent, conservatively even if conditional.
        body = []
        def visit(node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not function:
                child = next(candidate for candidate in by_name[node.name] if nodes[candidate] is node)
                edges[key].add(child)
                return
            body.append(node)
            for child in ast.iter_child_nodes(node):
                visit(child)
        visit(function)
        if key == "workflowy_mcp.weave_worker:main":
            sinks[key].add((function.lineno, "retained worker main"))
        def resolve(node):
            if isinstance(node, ast.Name):
                if node.id in aliases:
                    return aliases[node.id]
                for depth in range(len(lexical) + 1, -1, -1):
                    candidate = module + "." + ".".join([*lexical[:depth], node.id])
                    if candidate in canonical or candidate in classes:
                        return {candidate}
                return {node.id}
            if isinstance(node, ast.Attribute):
                return {value + "." + node.attr for value in resolve(node.value)}
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
                bases = resolve(node.args[0])
                if isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                    return {value + "." + node.args[1].value for value in bases}
                return {"<dynamic:" + value + ">" for value in bases}
            return set()
        # Fixed point handles chains of imported callable/module aliases. The
        # finite AST vocabulary bounds this analysis, not production execution.
        for _ in range(len(body) + 1):
            changed = False
            for node in [*module_assignments[module], *body]:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    values = resolve(node.value)
                    for target in targets:
                        if isinstance(target, ast.Name) and values:
                            if any(isinstance(part, ast.Name) and part.id == target.id for part in ast.walk(node.value)):
                                continue
                            values = {value for value in values if value in canonical or value in classes or process_symbol(value)
                                      or value.split(".", 1)[0] in {"workflowy_mcp", "subprocess", "asyncio", "os", "multiprocessing", "concurrent"}
                                      or value.startswith("<dynamic:")}
                            prior = aliases.setdefault(target.id, set())
                            additions = values - prior
                            if additions:
                                prior.update(additions)
                                changed = True
            if not changed:
                break
        for node in body:
            if not isinstance(node, (ast.Name, ast.Attribute, ast.Call)):
                continue
            if isinstance(node, (ast.Name, ast.Attribute)) and not isinstance(node.ctx, ast.Load):
                continue
            values = resolve(node)
            if isinstance(node, ast.Call):
                values |= resolve(node.func)
            for value in values:
                if process_symbol(value):
                    sinks[key].add((getattr(node, "lineno", function.lineno), value))
                if value in canonical:
                    edges[key].add(canonical[value])
                elif value in classes:
                    edges[key].update(classes[value])
                else:
                    # Conservatively follow receiver methods and unresolved
                    # function values; this also includes scheduled callbacks.
                    edges[key].update(by_name.get(value.rsplit(".", 1)[-1], ()))
    return definitions, edges, sinks


def _assert_no_job_process(sources):
    definitions, edges, sinks = _process_graph(sources)
    roots = {"workflowy_mcp.server:" + name for name in _JOB_FRONTS}
    roots.add("workflowy_mcp.markdown_generate_pipeline:_run_phase8_refresh_inline")
    assert roots <= definitions.keys(), "a production CARTO front disappeared from the census"
    reached, pending, predecessor = set(), list(sorted(roots)), {}
    while pending:
        key = pending.pop()
        if key in reached:
            continue
        reached.add(key)
        if sinks.get(key):
            chain, cursor = [key], key
            while cursor in predecessor:
                cursor = predecessor[cursor]
                chain.append(cursor)
            raise AssertionError(f"production job reaches writer process: {' -> '.join(reversed(chain))}: {sorted(sinks[key])}")
        for child in edges.get(key, ()):
            if child not in reached:
                predecessor.setdefault(child, key)
                pending.append(child)
    return definitions, reached, sinks


def test_no_production_carto_path_reaches_writer_process():
    """X01: all fronts, transitive helpers, callbacks and local/import aliases."""
    _, reached, _ = _assert_no_job_process(_n07_runtime_sources())
    expected = {"workflowy_mcp.server:" + name for name in (
        "_start_carto_refresh_job", "_start_carto_markdown_generate_job", "_start_carto_bulk_visible_apply_job",
        "_schedule_carto_job", "_run_carto_job", "_execute_carto_job", "_finalize_carto_task")}
    expected |= {"workflowy_mcp.carto_job_governance:launch_carto_refresh_job",
                 "workflowy_mcp.markdown_generate_pipeline:run_markdown_generate_pipeline",
                 "workflowy_mcp.client.api_client_nexus:WorkFlowyClientNexus.refresh_file_node_beacons",
                 "workflowy_mcp.client.api_client_nexus:WorkFlowyClientNexus.refresh_folder_cartographer_sync"}
    assert expected <= reached, f"scheduler/callback closure lost required edges: {sorted(expected - reached)}"


@pytest.mark.parametrize("origin", ["_start_carto_refresh_job", "_start_carto_markdown_generate_job", "_run_phase8_refresh_inline"])
@pytest.mark.parametrize("body", [
    "_spawn_governed_worker()",
    "from subprocess import Popen as launch\nspawn = launch\nspawn(['python', '-m', 'workflowy_mcp.weave_worker'])",
    "import subprocess as process\nfactory = getattr(process, chosen_factory)\nfactory(['alternate-writer'])",
])
def test_process_reachability_rejects_each_front_alias_and_dynamic_factory(origin, body):
    import ast
    sources = _n07_runtime_sources()
    relative = "src/workflowy_mcp/" + ("markdown_generate_pipeline.py" if origin == "_run_phase8_refresh_inline" else "server.py")
    lines = sources[relative].splitlines()
    definition = next(node for node in ast.parse(sources[relative]).body if isinstance(node, ast.AsyncFunctionDef) and node.name == origin)
    insertion = definition.body[0].lineno - 1
    lines[insertion:insertion] = ["    " + line for line in body.splitlines()]
    sources[relative] = "\n".join(lines) + "\n"
    with pytest.raises(AssertionError, match="production job reaches writer process"):
        _assert_no_job_process(sources)


@pytest.mark.parametrize("addition", [
    "import subprocess as process\n_n07_factory = process.Popen\nasync def _n07_extra(): return _n07_factory(['writer'])\n",
    "import os as process\n_n07_factory = getattr(process, 'system')\nasync def _n07_extra(): return _n07_factory('alternate-writer')\n",
    "async def _n07_runner(callback): return callback()\nasync def _n07_spawn(): return _spawn_governed_worker()\nasync def _n07_extra(): return await _n07_runner(_n07_spawn)\n",
    "import subprocess\ndef _n07_factory(): return subprocess.Popen\nasync def _n07_extra(): return _n07_factory()(['writer'])\n",
    "import subprocess\nclass AlternateWriter:\n    def __init__(self): subprocess.Popen(['writer'])\nasync def _n07_extra(): return AlternateWriter()\n",
    "from .weave_worker import main as old_main\nasync def _n07_extra(): return await old_main()\n",
    "from .weave_worker import CartoHeartbeatController as Heartbeat\nasync def _n07_extra(): return Heartbeat('job')\n",
    "from .coherence.operation_cache import pin_worker_operation_cache as pin\nasync def _n07_extra(): return await pin(client, 'job')\n",
])
def test_process_reachability_follows_helpers_module_aliases_constructors_and_callback_values(addition):
    import ast
    sources = _n07_runtime_sources()
    relative = "src/workflowy_mcp/server.py"
    lines = sources[relative].splitlines()
    definition = next(node for node in ast.parse(sources[relative]).body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_start_carto_refresh_job")
    lines[definition.body[0].lineno - 1:definition.body[0].lineno - 1] = ["    await _n07_extra()"]
    sources[relative] = "\n".join(lines) + "\n" + addition
    with pytest.raises(AssertionError, match="production job reaches writer process"):
        _assert_no_job_process(sources)


_JOB_CAPABILITY_ORIGINS = {
    *_JOB_FRONTS, "_start_carto_refresh_job", "_start_carto_markdown_generate_job", "_start_carto_bulk_visible_apply_job",
    "launch_carto_refresh_job", "_schedule_carto_job", "_run_carto_job", "_execute_carto_job", "_finalize_carto_task",
    "_system_job_cancel_impl", "_request_inprocess_restart_stop", "_run_phase8_refresh_inline",
    "run_markdown_generate_pipeline", "refresh_file_node_beacons", "refresh_folder_cartographer_sync",
    "_run_carto_bulk_visible_apply_job", "_bulk_apply_per_file_inner_loop", "run_carto_cpu",
}
_THREAD_FORBIDDEN = {
    "get_client", "current_operation_cache", "current_carto_job_context", "mutation_region_locks",
    "_acquire_region_set", "registry_held_regions", "registry_held_keys", "held_mutation_lock_regions",
    "publish_local_cache_view", "publish_with_commit", "publish", "capture", "request", "update_node",
    "create_node", "delete_node", "move_node", "export_nodes", "list_nodes",
    "_reserve_carto_hold", "_finalize_carto_hold", "_acquire_q3_holds", "_release_q3_holds",
    "_governance_holds", "_carto_hold_protection", "_require_carto_hold",
}


def _job_capability_offenders(sources):
    """Direct job edges plus data-worker roots discovered at every CPU submit.

    Follow imported/local aliases, including workers introduced in new files.
    Thin Python/data callees of those workers are traversed as well. Dynamic
    process selection is independently rejected by X01; C06 supplies the
    substantive per-entry empty-Context/real-work witness for all table rows.
    """
    import ast
    from collections import defaultdict
    definitions, by_name, worker_names, calls, offenders = {}, defaultdict(set), set(), {}, []
    def authority_value(node):
        # Evaluate the expressly immutable presentation projection ON LOOP;
        # the containing dictionary, not its control owner, enters the worker.
        if isinstance(node, ast.Attribute) and node.attr == "presentation" and isinstance(node.value, ast.Name) and node.value.id == "control":
            return False
        if isinstance(node, ast.Name) and node.id in {"client", "publisher", "registry", "control", "self"}:
            return True
        return any(authority_value(child) for child in ast.iter_child_nodes(node))
    for rel, text in sources.items():
        tree = ast.parse(text, filename=rel)
        parent_map = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    aliases[item.asname or item.name.split(".")[0]] = item.name if item.asname else item.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                for item in node.names:
                    aliases[item.asname or item.name] = (node.module or "") + "." + item.name
        def resolve(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return resolve(node.value) + "." + node.attr
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                return resolve(node.args[0]) + "." + str(node.args[1].value)
            return "<dynamic>"
        # Alias values are capabilities, not arbitrary runtime object copies.
        for _ in range(8):
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
                    value = resolve(node.value)
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id not in {part.id for part in ast.walk(node.value) if isinstance(part, ast.Name)}:
                            aliases.setdefault(target.id, value)
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            lineage, cursor = [function.name], function
            while cursor in parent_map:
                cursor = parent_map[cursor]
                if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    lineage.append(cursor.name)
            key = (rel, function.lineno, ".".join(reversed(lineage)))
            definitions[key] = function
            by_name[function.name].add(key)
            body_calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
            calls[key] = [(node, resolve(node.func)) for node in body_calls]
            for call, name in calls[key]:
                if name.rsplit(".", 1)[-1] == "run_carto_cpu" and len(call.args) >= 2:
                    worker = call.args[1]
                    if isinstance(worker, ast.Lambda):
                        if authority_value(worker) or any(isinstance(part, ast.Call) and resolve(part.func).rsplit(".", 1)[-1] in _THREAD_FORBIDDEN for part in ast.walk(worker)):
                            offenders.append((key, call.lineno, "worker lambda captures authority"))
                    else:
                        worker_names.add(resolve(worker).rsplit(".", 1)[-1])
                    for argument in call.args[1:]:
                        if authority_value(argument):
                            offenders.append((key, call.lineno, "authority crosses worker boundary"))
    worker_keys, pending = set(), [key for name in worker_names for key in by_name.get(name, ())]
    while pending:
        key = pending.pop()
        if key in worker_keys:
            continue
        worker_keys.add(key)
        for call, name in calls[key]:
            # Named Python helpers are data dependencies. Do not invent type
            # inference for arbitrary dict/list methods or external parsers.
            if isinstance(call.func, ast.Name):
                pending.extend(by_name.get(name.rsplit(".", 1)[-1], ()))
    constructors = {"WorkFlowyClient", "WorkFlowyClientNexus", "WorkFlowyClientCore", "CacheViewPublisher", "_RegionRegistry"}
    for key, function in definitions.items():
        job = function.name in _JOB_CAPABILITY_ORIGINS
        worker = key in worker_keys
        if not job and not worker:
            continue
        noncarto_calls = set()
        if key[0] == "src/workflowy_mcp/server.py" and key[2] == "_system_job_cancel_impl":
            # Precisely the unchanged non-CARTO registry branch is outside the
            # cooperative CARTO policy. No whole-function cancel exemption.
            expected = ast.parse('''if job is not None:
    task = job.get("_task")
    if task is None:
        return {"success": False, "error": "Job has no associated task (cannot cancel)."}
    if task.done():
        return {"success": False, "error": "Job already completed."}
    job["status"] = "cancelling"
    task.cancel()
    return {"success": True, "job_id": job_id, "status": "cancelling"}
''').body[0]
            for branch in function.body:
                if ast.dump(branch) == ast.dump(expected):
                    noncarto_calls.update(part for part in ast.walk(branch) if isinstance(part, ast.Call))
        for call, name in calls[key]:
            leaf = name.rsplit(".", 1)[-1]
            if leaf in constructors or leaf in {"set_operation_cancel_callback", "load_usable_nodes_export_snapshot", "_select_manifest_linked_snapshot", "pin_worker_operation_cache"}:
                offenders.append((key, call.lineno, "client/cache/bootstrap authority: " + name))
            if job and leaf in {"cancel", "terminate", "kill", "killpg", "taskkill"} and call not in noncarto_calls:
                offenders.append((key, call.lineno, "noncooperative job stop: " + name))
            if job and leaf in {"close", "aclose"} and any(part in name for part in ("client", "get_client", "self.")):
                offenders.append((key, call.lineno, "shared client close: " + name))
            if worker and leaf in _THREAD_FORBIDDEN:
                offenders.append((key, call.lineno, "worker reaches loop authority: " + name))
            if job and leaf == "mutation_region_locks" and (key[0], key[2]) != ("src/workflowy_mcp/server.py", "_run_carto_job"):
                offenders.append((key, call.lineno, "per-file/phase grant reacquisition"))
            if job and name in {"os.environ.update", "os.environ.setdefault", "os.environ.pop", "os.environ.__setitem__", "os.putenv", "os.unsetenv"}:
                offenders.append((key, call.lineno, "daemon job mutates process environment"))
        for node in ast.walk(function):
            if isinstance(node, ast.Name) and worker and isinstance(node.ctx, ast.Load) and node.id in {"_carto_job_controls", "_carto_inprocess_tasks", "_carto_operation_jobs", "_LOCKS"}:
                offenders.append((key, node.lineno, "worker captures mutable task/registry state"))
            if isinstance(node, ast.Attribute) and worker and node.attr in {"_cache_view_publisher", "_write_governor"}:
                offenders.append((key, node.lineno, "worker captures client authority"))
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)) and ast.unparse(node.value) == "os.environ":
                offenders.append((key, node.lineno, "daemon job writes process environment"))
    return offenders


@pytest.mark.asyncio
async def test_job_tasks_share_client_and_never_transfer_ownership_to_threads(real_job_environment, monkeypatch):
    """X02: static capability fence paired with substantive real threaded parse."""
    import asyncio
    import threading
    from workflowy_mcp import carto_job_runtime as runtime, mutation_locks as locks, server
    from workflowy_mcp.client import api_client_nexus as nexus, api_client_core as core
    from workflowy_mcp.coherence.operation_cache import current_operation_cache
    assert _job_capability_offenders(_n07_runtime_sources()) == []
    env = real_job_environment
    observed = []
    source = nexus._prepare_carto_source_file
    def data(*args, **kwargs):
        assert threading.get_ident() != env.loop_thread, "actual parser ran inline"
        assert runtime.current_carto_job_context() is None and current_operation_cache(required=False) is None
        assert locks.MUTATION_LOCK_STATE.get() is None and core._AMBIENT_OPERATION_CANCEL_PROBE.get() is None
        value = source(*args, **kwargs)
        observed.append(value)
        return value
    monkeypatch.setattr(nexus, "_prepare_carto_source_file", data)
    for name in ("registry_held_regions", "held_mutation_lock_regions", "_acquire_region_set"):
        original = getattr(locks, name)
        if asyncio.iscoroutinefunction(original):
            async def on_loop(*args, _original=original, **kwargs):
                assert threading.get_ident() == env.loop_thread
                return await _original(*args, **kwargs)
        else:
            def on_loop(*args, _original=original, **kwargs):
                assert threading.get_ident() == env.loop_thread
                return _original(*args, **kwargs)
        monkeypatch.setattr(locks, name, on_loop)
    ownership_actions = []
    for name in ("_reserve_carto_hold", "_finalize_carto_hold", "_governance_holds",
                 "_carto_hold_protection"):
        original = getattr(locks, name)
        def registry_on_loop(*args, _name=name, _original=original, **kwargs):
            assert threading.get_ident() == env.loop_thread, f"{_name} left the daemon loop"
            ownership_actions.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(locks, name, registry_on_loop)
    server_require = server._require_carto_hold
    def require_on_loop(*args, **kwargs):
        assert threading.get_ident() == env.loop_thread, "_require_carto_hold left the daemon loop"
        ownership_actions.append("_require_carto_hold")
        return server_require(*args, **kwargs)
    monkeypatch.setattr(server, "_require_carto_hold", require_on_loop)
    with reject_raw_job_cancel(env):
        ack = await launch_real_job(env, "file")
        assert ack["success"], ack
        await env.tasks[ack["job_id"]]
    assert {"_reserve_carto_hold", "_finalize_carto_hold", "_require_carto_hold"} <= set(ownership_actions), (
        "real job did not exercise the actual registry admission/finalization actions")
    assert len(observed) == 1 and env.controls[ack["job_id"]].terminal_result["status"] == "completed"
    assert env.probe.sends and not env.probe.violations
    rows = env.client._cache_view_publisher.capture().nodes_by_id.values()
    assert any("added()" in row.get("name", "") for row in rows), "positive offload returned no real parse/reconcile work"


@pytest.mark.parametrize("body", [
    "from .client.api_client import WorkFlowyClient as Factory\n    client = Factory()",
    "client.aclose()", "client.set_operation_cancel_callback(probe)",
    "os.environ['CARTO_JOB_FILE'] = path", "task.cancel()", "os.kill(pid, 15)",
    "os.environ.update({'CARTO_JOB_DIR': path})",
    "_LOCKS = _RegionRegistry()", "load_usable_nodes_export_snapshot(path)",
    "async with mutation_region_locks(client=client):\n        pass",
    "await run_carto_cpu('bad', lambda: publisher.publish(view))",
    "await run_carto_cpu('bad', lambda: registry_held_regions())",
    "await run_carto_cpu('bad', lambda: _reserve_carto_hold(jobs_root=root))",
    "await run_carto_cpu('bad', lambda: _finalize_carto_hold(claim_id, root))",
    "await run_carto_cpu('bad', helper, client)",
])
def test_job_capability_fence_rejects_each_forbidden_edge(body):
    # These are AST-only fixtures. No constructor, thread, signal or process is
    # ever executed, including when deliberately neutralizing the checker.
    body = "\n".join("    " + line if index == 0 else line for index, line in enumerate(body.splitlines()))
    source = "async def refresh_file_node_beacons(client, task, publisher, view):\n" + body + "\n"
    assert _job_capability_offenders({"src/workflowy_mcp/client/api_client_nexus.py": source}), "forbidden job capability escaped the fence"


_MEMORY_FINALIZERS = {
    "_claim_carto_terminal_refresh_jobs", "_observe_carto_final_obligations", "_refresh_carto_job_tracker_once",
    "_finish_carto_final_batch_locked", "_finish_carto_final_batch", "_register_carto_final_obligation",
    "_record_carto_task_terminal", "_ensure_carto_final_delivery", "_deliver_carto_final_obligations",
}


def _memory_refresh_offenders(source):
    """No file/PID authority in memory adapters or retired marker code anywhere."""
    import ast
    tree, offenders = ast.parse(source), []
    forbidden = {"_carto_refresh_claims_dir", "_carto_final_record_path", "_carto_final_cleanup_permitted_locked",
                 "_delete_carto_final_marker_if_discharged", "_scan_carto_job_inventory_locked", "_scan_carto_job_files",
                 "inspect_carto_job_liveness", "process_identity", "verify_carto_job_process_identity", "Process",
                 "os_lock", "async_os_lock", "open", "read_text", "write_text", "read_bytes", "write_bytes",
                 "unlink", "mkdir", "makedirs", "glob", "rglob", "stat", "to_thread", "run_carto_cpu"}
    retired = {"_carto_refresh_claims_dir", "_carto_final_record_path",
               "_carto_final_cleanup_permitted_locked", "_delete_carto_final_marker_if_discharged"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in retired:
            offenders.append((node.name, node.lineno, "retired marker definition"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and ".refresh_claims" in node.value:
            offenders.append(("<module>", node.lineno, "retired marker path"))
    for function in tree.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if function.name in _MEMORY_FINALIZERS:
            aliases = {}
            for node in ast.walk(function):
                if isinstance(node, ast.ImportFrom):
                    aliases.update({part.asname or part.name: part.name for part in node.names})
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                    aliases.update({target.id: aliases.get(node.value.id, node.value.id) for target in node.targets if isinstance(target, ast.Name)})
            for node in ast.walk(function):
                if isinstance(node, ast.Call):
                    name = (aliases.get(node.func.id, node.func.id) if isinstance(node.func, ast.Name) else
                            node.func.attr if isinstance(node.func, ast.Attribute) else "<dynamic>")
                    if name in forbidden:
                        offenders.append((function.name, node.lineno, name))
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and ".refresh_claims" in node.value:
                    offenders.append((function.name, node.lineno, "marker authority"))
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in forbidden:
                    offenders.append((function.name, node.lineno, "dynamic terminal authority selection"))
        if function.name in {"_clear_carto_job_history_data", "_gc_carto_jobs"}:
            for node in ast.walk(function):
                if not isinstance(node, ast.Call) or not any(isinstance(part, ast.Name) and part.id in {"claims_dir", "claim_path", "_delete_carto_final_marker_if_discharged"} for part in ast.walk(node)):
                    continue
                offenders.append((function.name, node.lineno, "retired marker operation"))
    return offenders


@pytest.mark.asyncio
async def test_memory_refresh_has_no_disk_authority_or_pid_terminality(final_environment, monkeypatch):
    """X03: trap marker APIs and PID classification across actual task exit."""
    import asyncio
    import json
    from pathlib import Path
    from n05_region_harness import node_uuid
    from test_n07_carto_final_refresh import receipt
    from workflowy_mcp import carto_job_governance as gov
    env, calls = final_environment, []
    server = env.server
    source = _n07_runtime_sources()["src/workflowy_mcp/server.py"]
    assert _memory_refresh_offenders(source) == []
    def forbidden(*args, **kwargs):
        raise AssertionError("X03 new task used disk/PID terminal authority")
    for name in ("_carto_refresh_claims_dir", "_carto_final_record_path", "_carto_final_cleanup_permitted_locked", "_delete_carto_final_marker_if_discharged"):
        assert not hasattr(server, name)
    for module, names in (
        (server, ("inspect_carto_job_liveness", "create_cancellation_sentinel",
                  "carto_cancel_requested", "_read_carto_heartbeat_diagnostic",
                  "_carto_account_scope_and_env")),
        (gov, ("inspect_carto_job_liveness", "read_claim", "write_claim",
               "create_cancellation_sentinel", "carto_cancel_requested")),
    ):
        for name in names:
            assert not hasattr(module, name), f"retired terminal authority {name} is importable"
    for name in ("open", "read_text", "write_text", "unlink", "glob", "rglob"):
        original = getattr(Path, name)
        def guarded(path, *args, _original=original, **kwargs):
            assert ".refresh_claims" not in str(path), "X03 marker read/write/reserve/reap escaped"
            assert not str(path).endswith(".lock.json"), "X03 claim artifact read/write escaped"
            return _original(path, *args, **kwargs)
        monkeypatch.setattr(Path, name, guarded)
    entered, release = asyncio.Event(), asyncio.Event()
    async def work(**kwargs):
        env.trace("work")
        entered.set()
        await release.wait()
        return {"success": True, "nodes_updated": 1}
    async def request(**kwargs):
        calls.append(kwargs)
        return {"success": True, "scheduled": True}
    monkeypatch.setattr(env.client, "refresh_file_node_beacons", work, raising=False)
    monkeypatch.setattr(server, "_request_nodes_export_cache_refresh_async", request)
    ack = await server._start_carto_refresh_job(node_uuid("file"), "file")
    assert ack["success"], ack
    job_id = ack["job_id"]
    task, control = server._carto_inprocess_tasks[job_id], server._carto_job_controls[job_id]
    try:
        await entered.wait()
        path = Path(control.presentation["path"])
        row = json.loads(path.read_text())
        row.update(status="completed", pid=0)
        path.write_text(json.dumps(row))
        server._carto_job_states[job_id] = "completed"  # presentation cannot finish a task
        server._carto_active_cache_jobs.clear()
        assert server._daemon_drain_snapshot()["carto_inprocess"] and not task.done()
        await server._refresh_carto_job_tracker_once()
        assert not calls and server._carto_final_obligations[job_id].state == "observing"
        status = await server._system_job_status_impl(job_id=job_id)
        assert status["status"] == "running" and server._daemon_drain_snapshot()["carto_inprocess"]
    finally:
        release.set()
        await task
    assert server._carto_final_obligations[job_id].state == "pending"
    await server._refresh_carto_job_tracker_once()
    assert len(calls) == 1 and server._daemon_drain_snapshot()["carto_final_delivery"]
    batch, token = calls[0]["final_obligations"], calls[0]["attempt_token"]
    await server._finish_carto_final_batch(batch, token, receipt=receipt())
    assert server._carto_final_obligations[job_id].state == "published"
    await server._run_carto_job_gc_pass()
    await server._clear_carto_job_history(str(env.jobs))
    assert not server._daemon_drain_snapshot()["carto_inprocess"]


@pytest.mark.parametrize("body", [
    "open(_carto_final_record_path(base, job_id))", "inspect_carto_job_liveness(row)",
    "from .carto_job_governance import inspect_carto_job_liveness as classify\n    classify(row)",
    "await asyncio.to_thread(_carto_refresh_claims_dir)", "Path(base, '.refresh_claims').mkdir()",
    "old = getattr(server, '_carto_final_record_path')\n    old(base, job_id)",
    "Path(base, '.refresh_claims', 'old.claim.json').read_text()",
    "Path(base, '.refresh_claims', 'old.claim.json').unlink()",
    "if False:\n        def _carto_final_record_path(base, job_id):\n            return None",
])
def test_memory_adapter_fence_rejects_marker_and_pid_neutralizations(body):
    body = "\n".join("    " + line if index == 0 else line for index, line in enumerate(body.splitlines()))
    assert _memory_refresh_offenders("async def _observe_carto_final_obligations():\n" + body + "\n")


def test_job_diagnostic_and_existing_domain_error_contracts(scrubbed_runtime, monkeypatch):
    """X04: all affected declarations/mappings, exact opaque-holder extension."""
    from workflowy_mcp import server
    from workflowy_mcp.mutation_locks import MutationSerializationConflictError
    from test_mutation_locks import test_every_affected_verb_declares_the_timeout_code as declarations
    from test_mutation_regions_n05_fences import _refusal
    declarations()  # The unchanged N05 declaration sweep, including non-writers.
    expected = {
        "node": {"edit", "update_emoji", "prepend_emoji", "append_emoji", "append_note", "prepend_note", "complete", "uncomplete", "delete", "move", "etch"},
        "tag": {"add", "remove", "rename", "batch", "purge.apply"},
        "anchor": {"dock.create", "dock.update", "dock.promote", "dock.demote", "point.capture", "point.extract", "point.assign_id", "point.remove_id", "point.touch", "point.promote"},
        "slice_group": {"merge.persist", "purge.apply"},
    }
    registries = {name: getattr(server, "build_" + name + "_registry")() for name in
                  ("node", "tag", "anchor", "slice_group", "system", "beacon", "glimpse", "lightning_rod")}
    status_only = {"carto_job_exception", "daemon_restarted", "scheduling_failed"}
    for registry in registries.values():
        assert all(not status_only.intersection(spec.domain_error_codes) for spec in registry.operations()), "a status diagnostic became a thrown domain code"
    opaque, job_id = "opaque-operation-not-a-job-id", "diagnostic-job-id"
    monkeypatch.setattr(server, "_carto_operation_jobs", {opaque: job_id})
    for holder, kind in ((opaque, "long_lived"), ("unknown-operation", "long_lived"), (opaque, "short")):
        conflict = MutationSerializationConflictError("existing N05 conflict base", retry_after=60,
            contended_keys=["typed-region"], holder_kind=kind, holder_id=holder, reason="long_lived_holder")
        baseline = {"retry_after": 60.0, "contended_keys": ["typed-region"], "holder_kind": kind,
                    "holder_id": holder, "reason": "long_lived_holder"}
        extra = server._mutation_refusal_extra(conflict)
        if holder == opaque and kind == "long_lived":
            baseline.update(holder_job_id=job_id, holder_message=f"Held by Cartographer job {job_id}; check system.job.status before retrying.")
        assert extra == baseline and conflict.holder_id == holder
        for family, verbs in expected.items():
            for verb in verbs:
                spec = registries[family].resolve(verb)
                mapped = server._mcp_exception_mapper(spec, conflict)
                assert mapped["error"]["code"] == conflict.code and mapped["error"]["message"] == str(conflict)
                assert mapped["error"]["extra"] == baseline
    for code in ("mutation_serialization_timeout", "mutation_region_error"):
        error = _refusal(code)
        for family, verbs in expected.items():
            for verb in verbs:
                mapped = server._mcp_exception_mapper(registries[family].resolve(verb), error)
                assert mapped["error"]["code"] == code and mapped["error"]["message"] == str(error)
                assert mapped["error"]["extra"] == server._mutation_refusal_extra(error)
    assert server._mcp_exception_mapper(registries["node"].resolve("edit"), RuntimeError("carto_job_exception")) is None


@pytest.mark.parametrize("target", ["registry", "readme", "config-retry", "config-jobs"])
def test_repository_documentation_matches_n07_task_contract(scrubbed_runtime, target):
    """The three repository targets in §14.3, preserving their exact text."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    if target == "registry":
        from workflowy_mcp import server
        assert server.build_system_registry().resolve("job.cancel").returns["description"] == (
            "Request cooperative cancellation of one asynchronous job. Current Cartographer jobs run as daemon tasks: "
            "cooperative and already_terminal mean the task has ended; kill_refused_in_process_job means cancellation "
            "is still pending at a safe point, never a kill attempt. An acknowledgement is not proof of stop; poll "
            "job.status. Cancelling a MARKDOWN_GENERATE job also stops its inline phase-8 refresh. Pending Cartographer "
            "cancellation additionally carries cancellation_pending=true beside the retained "
            "kill_refused_in_process_job outcome; no PID is signalled. Poll job.status until actual task completion.")
    elif target == "readme":
        text = " ".join((root / "docs" / "REFERENCE.md").read_text().replace("`", "").split())
        assert ("The detached WEAVE enchanted/direct worker modes and the JEWEL UUID write-back machinery were removed "
                "in that earlier pass. Cartographer refresh, Markdown generation (with inline phase 8), and bulk apply "
                "now run as daemon tasks; CUT-2B removes the unused weave_worker entry and the remaining "
                "claim/PID/heartbeat/file-lock coordination. The one in-process registry carries job/Q3 footprints and "
                "memory cooldowns; writes keep their shared 1.0 s schedule and persisted breaker/Retry-After protection. "
                "Old coordination artifacts are ignored and need no cleanup. Pending cancel replies retain their legacy "
                "outcome and add cancellation_pending: true.") in text
    else:
        field = "mutationLockLongLivedRetryAfterSeconds" if target == "config-retry" else "cartographerJobsDir"
        row = next(line for line in (root / "CONFIG_REFERENCE.md").read_text().splitlines() if line.startswith(f"| `{field}` |"))
        expected = ("Default 60.0, positive; retry advice for long-lived holders and Cartographer launch conflicts when no usable completion estimate exists. "
                    "It never expires, releases or kills a holder, and it is not an acquisition wait bound." if target == "config-retry" else
                    "Directory for Cartographer job JSON files and the whole account coherence artifact set, including `.coherence/<account>/writer.lock`. "
                    "All full entries for that account use the same configured root; relocation is an offline move of the whole set, not a second writer domain.")
        assert row.split(" | ")[-1] == expected + " |"


@pytest.mark.parametrize("origin,prefix", [
    ("_schedule_carto_job", "new_operation_fresh_context + cache_operation_boundary inside _run_carto_job after B0"),
    ("_ensure_carto_final_delivery", "new_operation_fresh_context (cache_free: exact N07 final-delivery responsibility"),
])
def test_scheduler_spawn_has_exact_registered_fresh_context_disposition(origin, prefix):
    registry = scanner.load_registry()
    key = "src/workflowy_mcp/server.py::" + origin
    assert key in registry["task_spawns"], "required scheduler spawn is unregistered"
    assert registry["task_spawns"][key].startswith(prefix)
    source = """
from workflowy_mcp.coherence.operation_cache import create_isolated_context_task
async def _schedule_carto_job(coro):
    return create_isolated_context_task(coro)
"""
    assert not scanner.scan_source("src/workflowy_mcp/server.py", source.replace("_schedule_carto_job", origin), registry)


@pytest.mark.parametrize("source", [
    "async def unregistered(coro):\n    return create_isolated_context_task(coro)\n",
    "async def _schedule_carto_job_extra(coro):\n    return create_isolated_context_task(coro)\n",
    # An import alias spelling the permitted function name is still an asyncio
    # spawn in a different origin, not permission borrowed from that spelling.
    "from asyncio import create_task as _schedule_carto_job\n"
    "async def alias_user(coro):\n    return _schedule_carto_job(coro)\n",
    "class Alias:\n    async def _schedule_carto_job(coro):\n"
    "        return create_isolated_context_task(coro)\n",
])
@pytest.mark.parametrize("origin", ["_schedule_carto_job", "_ensure_carto_final_delivery"])
def test_scheduler_registration_cannot_clear_unregistered_near_name_or_alias(source, origin):
    source = source.replace("_schedule_carto_job", origin)
    violations = scanner.scan_source("src/workflowy_mcp/server.py", source, scanner.load_registry())
    assert any(v.kind == "unclassified_spawn" for v in violations), (
        "scheduler registration admitted an unregistered, near-name or aliased spawn"
    )


@pytest.mark.parametrize("expression,marker", [
    (None, None),
    ("'carto_refresh'", "a production worker_mode launcher reappeared"),
    ("'markdown_generate'", "a production worker_mode launcher reappeared"),
    ("'enchanted'", "dead launch modes present"),
    ("selected_mode", "worker_mode is not a literal"),
])
def test_zero_worker_mode_fence_keeps_all_literal_and_vocabulary_checks(tmp_path, monkeypatch, expression, marker):
    """U3KYFS: exact existing oracle, not a second more-permissive scanner."""
    import test_s06_worker_mode_cleanup as fence
    source = tmp_path / "governance.py"
    source.write_text("" if expression is None else f"spawn(worker_mode={expression})\n")
    empty = tmp_path / "empty.py"
    empty.write_text("")
    monkeypatch.setattr(fence, "GOVERNANCE", source)
    monkeypatch.setattr(fence, "SERVER", empty)
    monkeypatch.setattr(fence, "PIPELINE", empty)
    if marker is None:
        fence.test_launch_command_inventory_contains_only_governed_modes()
    else:
        with pytest.raises(AssertionError, match=marker):
            fence.test_launch_command_inventory_contains_only_governed_modes()


_BEACON_CLIENT_SOURCE = "src/workflowy_mcp/client/api_client_nexus.py"
_BEACON_OWNER_CHECK = "return snapshot.owner is (getattr(self, '_cache_view_publisher', None) or self)"


@pytest.mark.parametrize("method", ["update_beacon_from_node", "refresh_file_node_beacons", "refresh_folder_cartographer_sync"])
def test_beacon_snapshot_owner_has_exact_read_only_registration(method):
    """CUAC55 declares identity observation, never publisher or worker writes."""
    registry = scanner.load_registry()
    entries = [entry for entry in registry["allowlist"]
               if entry["file"] == _BEACON_CLIENT_SOURCE
               and entry["function"] == "WorkFlowyClientNexus." + method]
    assert len(entries) == 1 and entries[0]["access_kind"] == "publisher_read"
    source = ("class WorkFlowyClientNexus:\n"
              f"    async def {method}(self, snapshot):\n"
              f"        {_BEACON_OWNER_CHECK}\n")
    assert scanner.scan_source(_BEACON_CLIENT_SOURCE, source, registry) == []


@pytest.mark.parametrize("source", [
    "class Other:\n    async def update_beacon_from_node(self, snapshot):\n"
    f"        {_BEACON_OWNER_CHECK}\n",
    "class WorkFlowyClientNexus:\n    async def update_beacon_from_node_extra(self, snapshot):\n"
    f"        {_BEACON_OWNER_CHECK}\n",
    "class WorkFlowyClientNexus:\n    async def update_beacon_from_node(self, snapshot):\n"
    f"        def worker():\n            {_BEACON_OWNER_CHECK}\n",
    "class WorkFlowyClientNexus:\n    async def update_beacon_from_node(self, snapshot):\n"
    "        self._cache_view_publisher = snapshot.owner\n",
])
@pytest.mark.parametrize("method", ["update_beacon_from_node", "refresh_file_node_beacons", "refresh_folder_cartographer_sync"])
def test_beacon_snapshot_permission_rejects_wrong_origin_and_store(source, method):
    source = source.replace("update_beacon_from_node", method)
    violations = scanner.scan_source(_BEACON_CLIENT_SOURCE, source, scanner.load_registry())
    assert any(violation.kind == "attr_access" for violation in violations), (
        "identity-only beacon permission admitted a wrong-class near-name nested or store access"
    )


def _check_bulk_tree_data_definition(definition):
    """Narrow E0VOGK X02 arm, not the complete unit-wide forbidden-edge census."""
    import ast
    arguments, calls = {
        "_build_carto_cache_subtree": (("all_nodes", "root_uuid"), {"str", "_build"}),
        "_collect_carto_bulk_nodes": (("root", "kind"), {"_collect_bulk_f12p3_markdown_file_nodes", "_collect_bulk_materialize_leaves"}),
        "_build_carto_materialize_index": (("all_nodes", "walk_root"), {"str", "dict", "isinstance", "overlay"}),
    }[definition.name]
    assert isinstance(definition, ast.FunctionDef), "bulk data phase became an async client wrapper"
    assert tuple(argument.arg for argument in definition.args.args) == arguments, "bulk data phase accepts an authority or callback"
    bound = {node.id for node in ast.walk(definition) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    bound.update(node.arg for node in ast.walk(definition) if isinstance(node, ast.arg))
    bound.update(node.name for node in ast.walk(definition) if isinstance(node, ast.FunctionDef))
    allowed_loads = bound | calls | {"str", "dict", "list", "int", "Any", "Mapping"}
    for node in ast.walk(definition):
        assert not isinstance(node, (ast.Import, ast.ImportFrom, ast.Await, ast.Global, ast.Nonlocal)), "bulk data phase gained an implicit authority"
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            assert node.id in allowed_loads, "bulk data phase captures implicit client cache or task-table state"
        if isinstance(node, ast.Attribute):
            assert node.attr in {"get", "setdefault", "append"}, "bulk data phase reaches client publisher registry or executor state"
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id in calls, "bulk data phase calls an undeclared authority or aliased callback"
            else:
                assert isinstance(node.func, ast.Attribute), "bulk data phase calls a dynamic factory"


@pytest.mark.parametrize("name", ["_build_carto_cache_subtree", "_collect_carto_bulk_nodes", "_build_carto_materialize_index"])
def test_bulk_tree_offload_definitions_have_only_explicit_data_capabilities(name):
    import ast
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / "src/workflowy_mcp/server.py"
    tree = ast.parse(source.read_text())
    definition = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    _check_bulk_tree_data_definition(definition)


@pytest.mark.parametrize("source", [
    "def _build_carto_cache_subtree(client, root_uuid):\n    return []\n",
    "async def _build_carto_cache_subtree(all_nodes, root_uuid):\n    return []\n",
    "def _build_carto_cache_subtree(all_nodes, root_uuid):\n    return get_client().export_nodes()\n",
    "def _build_carto_cache_subtree(all_nodes, root_uuid):\n    callback = get_client\n    return callback()\n",
    "def _build_carto_cache_subtree(all_nodes, root_uuid):\n    return registry_held_regions()\n",
    "def _build_carto_cache_subtree(all_nodes, root_uuid):\n    return publisher.capture()\n",
    "def _build_carto_cache_subtree(all_nodes, root_uuid):\n    return executor.submit(all_nodes)\n",
    "def _collect_carto_bulk_nodes(root, kind):\n    from workflowy_mcp.coherence.operation_cache import current_operation_cache\n    return current_operation_cache()\n",
    "def _collect_carto_bulk_nodes(root, kind):\n    _carto_job_controls['job'] = root\n    return []\n",
])
def test_bulk_tree_data_fence_rejects_authority_alias_async_and_thread_escapes(source):
    import ast
    with pytest.raises(AssertionError):
        _check_bulk_tree_data_definition(ast.parse(source).body[0])
