"""N03 exact production-touch fences; U5ZNEH's narrow G3 census extension."""
import ast
import os
from pathlib import Path

import pytest
import test_cut_2b_fences as cut2b
import test_cut_2a_fences as cut2a

ROOT = Path(__file__).resolve().parents[1]
OLD_LOCK_SITES = {
    "barrier.py::hard_stop_state": 1, "barrier.py::raise_global_dirty_barrier_async": 1,
    "checkpoint.py::_acquire_checkpoint_claim": 1, "checkpoint.py::run_checkpoint_rotation": 3,
    "refresh_authority.py::_write_claim_status": 1, "refresh_authority.py::_acquire_or_join": 1,
    "refresh_authority.py::_own_full_refresh": 1, "refresh_authority.py::run_compat_patch": 1,
    "epoch_publisher.py::publish_epoch": 1,
}
NEW_LOCK_SITE = {"transition_create.py::record_external_additions": 1}


def _check_expected(source):
    fn = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "_coherence_scope_offenders")
    value = next(n.value for n in fn.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "expected" for t in n.targets))
    assert ast.literal_eval(value) == OLD_LOCK_SITES | NEW_LOCK_SITE


def test_exact_historical_plus_one_lock_site_and_short_scope(monkeypatch):
    """An extra/missing row and an await in the new span each fail independently."""
    if os.getenv("N03_G3_MUTANT") == "fence":
        monkeypatch.setitem(globals(), "_check_expected", lambda source: None)
    source = (ROOT / "tests/test_cut_2b_fences.py").read_text()
    _check_expected(source)
    row = '"transition_create.py::record_external_additions": 1,'
    for damaged in (source.replace(row, "", 1), source.replace(row, row + '\n        "rogue.py::writer": 1,', 1)):
        with pytest.raises(AssertionError):
            _check_expected(damaged)
    module = (ROOT / "src/workflowy_mcp/coherence/transition_create.py").read_text()
    assert cut2b._coherence_scope_offenders({"transition_create.py": module}, inventory=False) == []
    damaged = module.replace("async with coherence_lock_async(paths):", "async with coherence_lock_async(paths):\n        await forbidden_network_work()", 1)
    assert any("await in coherence span" in item for item in cut2b._coherence_scope_offenders({"transition_create.py": damaged}, inventory=False))


MANIFEST_PATHS = [
    "src/workflowy_mcp/client/create_transport.py", "src/workflowy_mcp/client/insert_capability.py",
    "src/workflowy_mcp/coherence/transition_create.py", "src/workflowy_mcp/client/api_client_core.py",
    "src/workflowy_mcp/models/config.py", "src/workflowy_mcp/config.py", "CONFIG_REFERENCE.md",
    "tests/coherence_s03b_registry.json", "tests/coherence_s03b_machine_inventory.json",
]


def _definition(source, qualified):
    scope = ast.parse(source).body
    for name in qualified.split("."):
        found = [n for n in scope if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
        assert len(found) == 1, qualified
        target = found[0]
        scope = target.body
    return target


def _calls(node):
    return [getattr(n.func, "id", None) or getattr(n.func, "attr", None) for n in ast.walk(node) if isinstance(n, ast.Call)]


# N04 G1 (spec [ap:TIGASU] §14.1): the exact permitted imports of the LLM
# transport and the transition engine. `coherence.transition_create` keeps its
# one transport import; `client.api_client_nexus` is the one Layer-C driver, so
# it imports the transport and the transition engine. No other module may reach
# either, and a relative spelling resolves to the same dotted module.
N04_TRANSPORT_IMPORTS = {
    ("src/workflowy_mcp/coherence/transition_create.py", "workflowy_mcp.client.create_transport"),
    ("src/workflowy_mcp/client/api_client_nexus.py", "workflowy_mcp.client.create_transport"),
    ("src/workflowy_mcp/client/api_client_nexus.py", "workflowy_mcp.coherence.transition_create"),
}


TRANSPORT_MODULES = {"create_transport", "transition_create"}


def _dotted_module(path, level, module):
    """Resolve a relative `from . import x` spelling to its dotted module."""
    package = list(path.relative_to(ROOT / "src").with_suffix("").parts[:-1])
    base = package[: len(package) - (level - 1)] if level else []
    return ".".join(base + ([module] if module else []))


def _transport_import_edges():
    edges = []
    for path in (ROOT / "src/workflowy_mcp").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                edges.extend((relative, alias.name) for alias in node.names
                             if alias.name.rsplit(".", 1)[-1] in TRANSPORT_MODULES)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.rsplit(".", 1)[-1] in TRANSPORT_MODULES:
                    edges.append((relative, _dotted_module(path, node.level, module)))
                elif not module:
                    edges.extend((relative, _dotted_module(path, node.level, alias.name))
                                 for alias in node.names if alias.name in TRANSPORT_MODULES)
    return sorted(edges)


REQUIRED_BODIES = [
    (MANIFEST_PATHS[0], "PublicV1CreateTransport.create", {"create_node"}),
    (MANIFEST_PATHS[0], "LlmInsertAfterTransport.insert_after", {"_send_workflowy_write"}),
    (MANIFEST_PATHS[1], "InsertCapabilityController.select_for_insert", {"_v1_only", "InsertSelection"}),
    (MANIFEST_PATHS[2], "TransitionCreateTransaction.create", {"create_many"}),
    (MANIFEST_PATHS[2], "TransitionCreateTransaction.create_many", {"_create_run", "identifiable_runs"}),
    (MANIFEST_PATHS[2], "TransitionCreateTransaction.identifiable_runs", {"_content"}),
    (MANIFEST_PATHS[2], "TransitionCreateTransaction._create_run", {"_attempt"}),
    (MANIFEST_PATHS[2], "TransitionCreateTransaction._attempt", {"_joined_verification", "LlmInsertAfterTransport", "PublicV1CreateTransport"}),
    (MANIFEST_PATHS[2], "TransitionCreateTransaction._verify", {"classify_create_run", "_apply_create_to_nodes_export_cache"}),
    (MANIFEST_PATHS[2], "classify_create_attempt", {"classify_create_run"}),
    (MANIFEST_PATHS[2], "classify_create_run", {"_order", "_content"}),
    (MANIFEST_PATHS[2], "record_external_additions", {"coherence_lock_async", "read_external_dirt", "write_external_dirt"}),
    (MANIFEST_PATHS[2], "_request_recovery", {"_request_nodes_export_cache_refresh_async"}),
    (MANIFEST_PATHS[2], "TransitionCreateTransaction._ambiguous", {"raise_global_dirty_barrier_async", "_request_recovery", "CoherenceBarrierActiveError"}),
    (MANIFEST_PATHS[2], "TransitionCreateTransaction._dirty", {"_mark_nodes_export_dirty"}),
    (MANIFEST_PATHS[3], "WorkFlowyClientCore.__init__", {"InsertCapabilityController"}),
    (MANIFEST_PATHS[3], "WorkFlowyClientCore._send_workflowy_write", {"request", "acquire_write_slot", "recheck_before_send"}),
    (MANIFEST_PATHS[3], "WorkFlowyClientCore.list_nodes", {"deepcopy", "update"}),
]


def _assert_manifest(sources):
    import json
    for path, name, required in REQUIRED_BODIES:
        assert required <= set(_calls(_definition(sources[path], name))), (path, name)
    for name in ("PlacementIntent", "CreateTransport", "PublicV1CreateTransport", "LlmInsertAfterTransport"):
        _definition(sources[MANIFEST_PATHS[0]], name)
    for name in ("TransitionCreateTransaction", "ParentOrderWitness", "TransitionCreateResult", "classify_create_attempt", "classify_create_run"):
        _definition(sources[MANIFEST_PATHS[2]], name)
    sender = _definition(sources[MANIFEST_PATHS[3]], "WorkFlowyClientCore._send_workflowy_write")
    assert any(a.arg == "_send_stage" for a in sender.args.kwonlyargs)
    assert any(isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and n.value.value is True
        and any(isinstance(t, ast.Attribute) and t.attr == "dispatch_started" for t in n.targets) for n in ast.walk(sender))
    listing = _definition(sources[MANIFEST_PATHS[3]], "WorkFlowyClientCore.list_nodes")
    assert any(a.arg == "_listing_evidence" for a in listing.args.kwonlyargs)
    config = _definition(sources[MANIFEST_PATHS[4]], "ServerConfig")
    fields = [n for n in config.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "v1_only"]
    assert len(fields) == 1 and ast.unparse(fields[0].annotation) == "bool"
    assert isinstance(fields[0].value, ast.Call) and fields[0].value.args[0].value is False
    env = _definition(sources[MANIFEST_PATHS[5]], "_merge_env_overrides")
    assert any(isinstance(n, ast.Constant) and n.value == "WORKFLOWY_V1_ONLY" for n in ast.walk(env))
    assert "| `v1_only` |" in sources["CONFIG_REFERENCE.md"]
    assert "| `WORKFLOWY_V1_ONLY` |" in sources["CONFIG_REFERENCE.md"]
    registry = json.loads(sources[MANIFEST_PATHS[7]])
    key = "src/workflowy_mcp/coherence/transition_create.py::TransitionCreateTransaction._joined_verification"
    assert registry["task_spawns"].get(key, "").startswith("same_operation_inherit")
    inventory = json.loads(sources[MANIFEST_PATHS[8]])
    assert key + " [create_task]" in inventory["task_spawn_sites"]


def test_prescribed_production_touches_not_a_test_only_sidecar(monkeypatch):
    sources = {p: (ROOT / p).read_text() for p in MANIFEST_PATHS}
    if os.getenv("N03_G4_MUTANT") == "manifest":
        monkeypatch.setitem(globals(), "_assert_manifest", lambda sources: None)
    _assert_manifest(sources)
    for path, name, _ in REQUIRED_BODIES:
        damaged = dict(sources)
        tree = ast.parse(damaged[path])
        scope = tree.body
        for part in name.split("."):
            target = next(n for n in scope if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == part)
            scope = target.body
        target.body = [ast.Pass()]
        damaged[path] = ast.unparse(tree)
        with pytest.raises(AssertionError): _assert_manifest(damaged)
    for prefix in ("| `v1_only` |", "| `WORKFLOWY_V1_ONLY` |"):
        damaged = dict(sources);damaged["CONFIG_REFERENCE.md"] = damaged["CONFIG_REFERENCE.md"].replace(prefix, "removed-row", 1)
        with pytest.raises(AssertionError): _assert_manifest(damaged)
    damaged = dict(sources)
    damaged[MANIFEST_PATHS[4]] = damaged[MANIFEST_PATHS[4]].replace("v1_only: bool = Field(False,", "v1_only: bool = Field(True,", 1)
    with pytest.raises(AssertionError): _assert_manifest(damaged)
    damaged = dict(sources)
    tree = ast.parse(damaged[MANIFEST_PATHS[5]])
    next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_merge_env_overrides").body = [ast.Pass()]
    damaged[MANIFEST_PATHS[5]] = ast.unparse(tree)
    with pytest.raises(AssertionError): _assert_manifest(damaged)
    import json
    damaged = dict(sources)
    registry = json.loads(damaged[MANIFEST_PATHS[7]])
    del registry["task_spawns"]["src/workflowy_mcp/coherence/transition_create.py::TransitionCreateTransaction._joined_verification"]
    damaged[MANIFEST_PATHS[7]] = json.dumps(registry)
    with pytest.raises(AssertionError): _assert_manifest(damaged)


def test_one_endpoint_one_lane_and_no_production_caller(monkeypatch):
    if os.getenv("N03_G4_MUTANT") == "census":
        original = Path.read_text
        def source_copy(path, *args, **kwargs):
            source = original(path, *args, **kwargs)
            return source + "\nhttpx.AsyncClient()\n" if path == ROOT / MANIFEST_PATHS[2] else source
        monkeypatch.setattr(Path, "read_text", source_copy)
    source_dir = ROOT / "src/workflowy_mcp"
    definitions = []
    for path in source_dir.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "LLM_DOC_EDIT_PATH" for t in node.targets):
                definitions.append((path.relative_to(ROOT).as_posix(), ast.literal_eval(node.value)))
    assert definitions == [(MANIFEST_PATHS[0], "/api/llm/doc/edit")]
    assert _transport_import_edges() == sorted(N04_TRANSPORT_IMPORTS)
    for path in MANIFEST_PATHS[:3]:
        calls = _calls(ast.parse((ROOT / path).read_text()))
        assert not ({"AsyncClient", "CartoWriteGovernor", "_WriteSchedule", "derive_account_scope_id", "acquire_write_slot", "sleep"} & set(calls))


def test_n03_transport_import_census_is_exact(monkeypatch):
    """The census is a closed set, not a subset: one extra importer fails it."""
    original = Path.read_text
    rogue = ROOT / "src/workflowy_mcp/coherence/checkpoint.py"

    def source_copy(path, *args, **kwargs):
        text = original(path, *args, **kwargs)
        if Path(path) == rogue:
            return text + "\nfrom workflowy_mcp.client.create_transport import LlmInsertAfterTransport\n"
        return text

    monkeypatch.setattr(Path, "read_text", source_copy)
    edges = _transport_import_edges()
    assert ("src/workflowy_mcp/coherence/checkpoint.py", "workflowy_mcp.client.create_transport") in edges
    assert edges != sorted(N04_TRANSPORT_IMPORTS)


def test_existing_neighbor_preservation_pin_still_rejects_damage(monkeypatch):
    if os.getenv("N03_G4_MUTANT") == "immutable":
        monkeypatch.setattr(cut2a, "_preservation_offenders", lambda sources: [])
    sources = cut2a._sources()
    assert cut2a._preservation_offenders(sources) == []
    damaged = dict(sources)
    damaged[cut2a.CLIENT] = cut2a._insert_in_function(damaged[cut2a.CLIENT], "WorkFlowyClientCore.create_node", "pass")
    assert "client:other methods" in cut2a._preservation_offenders(damaged)
