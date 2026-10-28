"""N04 G1 structural fences: one CREATE protocol, one Layer-C caller, one grant.

Contract: [ap:TIGASU] §5.1-5.2, §6.1-6.3, §6.6, §14.1/§14.4 and LOCK A1.
These are exact production-shape fences, not a golden hash: every checker is a
pure function over the source texts, so each one is proved to have teeth by a
source-copy damage case in ``test_n04_fences_reject_source_copy_damage`` (the
production files are never edited).

What the fences pin:
1. exactly one typed CREATE dispatch in the data-only generator, carrying one
   ``ReconcileCreateRun``; no scalar ``(parent, payload)`` create route survives;
   one ``create_parent_done`` dispatch in the driver;
2. the generator stays data-only (no client, transport, coherence, httpx,
   asyncio, await, publisher or lock-plane name), and its private order helper
   writes no cache;
3. the parent conclusion is emitted inside ``ensure_created`` and before the
   MOVE phase;
4. all six receivers accept the real ``held_grant`` keyword and every edge
   forwards the same object (the exact counts below);
5. ``create_wrapper`` is the one production Layer-C construction site and the
   driver call supplies both the typed create callback and the conclusion;
6. A1's endpoint-429 origin tag is written only at the LLM response branch.

Out of scope here (owned elsewhere, see the leg Result): the CUT-2A/2B pins,
J/U09 hashes, the C05 oracle split and the N03 caller census -- those are
re-derived by the same commit's own files, not duplicated in this module.
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
from pathlib import Path

import pytest

from workflowy_mcp.client.carto_reconcile_cpu import ReconcileCreateResult, ReconcileCreateRun

ROOT = Path(__file__).resolve().parents[1]

PATHS = {
    "cpu": "src/workflowy_mcp/client/carto_reconcile_cpu.py",
    "driver": "src/workflowy_mcp/client/workflowy_move_reconcile.py",
    "acn": "src/workflowy_mcp/client/api_client_nexus.py",
    "tc": "src/workflowy_mcp/coherence/transition_create.py",
    "transport": "src/workflowy_mcp/client/create_transport.py",
    "server": "src/workflowy_mcp/server.py",
    "mgp": "src/workflowy_mcp/markdown_generate_pipeline.py",
}

GRANT_RECEIVERS = {
    "server": ("_execute_carto_job", "_run_carto_bulk_visible_apply_job"),
    "acn": ("refresh_file_node_beacons", "refresh_folder_cartographer_sync"),
    "mgp": ("run_markdown_generate_pipeline", "_run_phase8_refresh_inline"),
}

GRANT_EDGES = {
    ("server", "_run_carto_job"): {"_execute_carto_job": 2},
    ("server", "_execute_carto_job"): {
        "refresh_folder_cartographer_sync": 1, "refresh_file_node_beacons": 1,
        "run_markdown_generate_pipeline": 1, "_run_carto_bulk_visible_apply_job": 1,
    },
    ("acn", "refresh_file_node_beacons"): {"refresh_folder_cartographer_sync": 1},
    ("acn", "refresh_folder_cartographer_sync"): {"refresh_file_node_beacons": 1},
    ("mgp", "run_markdown_generate_pipeline"): {"_run_phase8_refresh_inline": 1},
    ("mgp", "_run_phase8_refresh_inline"): {"refresh_file_node_beacons": 1},
    ("server", "_run_carto_bulk_visible_apply_job"): {"refresh_file_node_beacons": 1},
}

FORBIDDEN_CPU_MODULES = ("httpx", "asyncio")
FORBIDDEN_CPU_NAMES = {
    "create_node", "list_nodes", "move_node", "_send_workflowy_write",
    "CacheViewPublisher", "mutation_region_locks", "current_operation_cache",
    "TransitionCreateTransaction", "LlmInsertAfterTransport",
}


def _sources() -> dict[str, str]:
    return {key: (ROOT / relative).read_text() for key, relative in PATHS.items()}


def _parsed(sources) -> dict[str, ast.Module]:
    return {key: ast.parse(text) for key, text in sources.items()}


def _callee(call) -> str | None:
    return getattr(call.func, "id", None) or getattr(call.func, "attr", None)


def _enclosing_functions(tree):
    """Map every node to the names of the functions that contain it."""
    owners: dict[int, list[str]] = {}

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            next_stack = stack + [node.name] if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)) else stack
            owners[id(child)] = next_stack
            walk(child, next_stack)

    walk(tree, [])
    return owners


def _compact(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# 1 · one CREATE protocol, no scalar route
# ---------------------------------------------------------------------------


def _ancestors(tree):
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def create_protocol_offenders(sources):
    offenders = []
    tree = ast.parse(sources["cpu"])
    dispatches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee(node) == "ReconcileRequest"
        and node.args and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "create"
    ]
    if len(dispatches) != 1:
        offenders.append(f"cpu has {len(dispatches)} typed 'create' dispatches; the contract has one")
    for node in dispatches:
        shape_ok = (
            len(node.args) == 3 and not node.keywords
            and isinstance(node.args[1], ast.Tuple) and len(node.args[1].elts) == 1
            and isinstance(node.args[1].elts[0], ast.Name)
        )
        if not shape_ok:
            offenders.append("the create dispatch must carry one run object in a one-element tuple")
    if "ReconcileRequest('create', (desired_parent" in sources["cpu"]:
        offenders.append("a scalar (parent, payload) create route is still reachable in cpu")

    driver = ast.parse(sources["driver"])
    kinds = [
        node.comparators[0].value for node in ast.walk(driver)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute)
        and node.left.attr == "kind" and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Constant)
    ]
    if kinds.count("create") != 1:
        offenders.append(f"driver dispatches the create kind {kinds.count('create')} times; expected one")
    if kinds.count("create_parent_done") != 1:
        offenders.append("driver does not dispatch exactly one create_parent_done kind")
    compact = _compact(sources["driver"])
    if "create_node: Callable[[ReconcileCreateRun], ReconcileCreateResult]" not in compact:
        offenders.append("the create callback is not typed with one run object and one result")
    if "conclude_create_parent: Callable[..., Any] | None = None" not in compact:
        offenders.append("the optional conclusion callback is missing from the driver signature")
    return offenders


# ---------------------------------------------------------------------------
# 2 · the generator stays data-only and writes no cache
# ---------------------------------------------------------------------------


def data_only_offenders(sources):
    offenders = []
    tree = ast.parse(sources["cpu"])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [f"cpu imports {alias.name}" for alias in node.names
                          if alias.name.split(".")[0] in FORBIDDEN_CPU_MODULES]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            # JR2 R-b: this exact stdlib-only (os/threading) diagnostic gate
            # shares every debug writer's one rotation lock. No callback or
            # live client/cache object crosses the data worker boundary.
            if (node.level == 2 and module == "debug_log_rotation"
                    and {alias.name for alias in node.names} <= {"append_debug_line", "reset_debug_log"}):
                continue
            if node.level or (
                    module.split(".")[0] not in {
                        "collections", "dataclasses", "datetime", "functools", "itertools",
                        "json", "math", "pathlib", "re", "typing", "uuid"}
                    and module != "__future__"):
                offenders.append(f"cpu imports {'.' * node.level}{module}")
        elif isinstance(node, ast.AsyncFunctionDef):
            offenders.append(f"cpu defines async function {node.name}")
        elif isinstance(node, ast.Await):
            offenders.append("cpu awaits")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_CPU_NAMES:
            offenders.append(f"cpu references {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_CPU_NAMES:
            offenders.append(f"cpu references .{node.attr}")
    return offenders


def order_adoption_offenders(sources):
    offenders = []
    tree = ast.parse(sources["cpu"])
    definitions = [node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "_adopt_create_order"]
    if len(definitions) != 1:
        offenders.append(f"cpu defines {len(definitions)} order-adoption helpers; expected one")
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and _callee(node) == "_adopt_create_order"]
    if len(calls) != 2:
        offenders.append(f"the order-adoption helper has {len(calls)} call sites; expected two")
    if definitions:
        body = ast.get_source_segment(sources["cpu"], definitions[0]) or ""
        for forbidden in ("nodes_export", "publish", "_apply_", "canonical"):
            if forbidden in body:
                offenders.append(f"the order-adoption helper touches {forbidden}")
    for retired in ("synthetic_priority", "_existing_sibling_priority"):
        if retired in sources["cpu"]:
            offenders.append(f"the retired bottom-only model {retired} is still present")
    return offenders


# ---------------------------------------------------------------------------
# 3 · parent conclusion before MOVE
# ---------------------------------------------------------------------------


def conclusion_before_move_offenders(sources):
    offenders = []
    text = sources["cpu"]
    tree = ast.parse(text)
    owners = _enclosing_functions(tree)
    conclusions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Yield) and isinstance(node.value, ast.Call)
        and _callee(node.value) == "ReconcileRequest"
        and node.value.args and isinstance(node.value.args[0], ast.Constant)
        and node.value.args[0].value == "create_parent_done"
    ]
    if len(conclusions) != 1:
        offenders.append(f"cpu emits {len(conclusions)} parent conclusions; expected one")
    for node in conclusions:
        if (owners.get(id(node)) or [None])[-1] != "ensure_created":
            offenders.append("the parent conclusion is not emitted from ensure_created")
    if "create_parent_done" in text and "[PHASE 2] START - MOVE" in text:
        if text.index("create_parent_done") > text.index("[PHASE 2] START - MOVE"):
            offenders.append("the parent conclusion appears after the MOVE phase banner")
    else:
        offenders.append("the conclusion or the MOVE phase banner is missing")
    return offenders


# ---------------------------------------------------------------------------
# 4 · the real held grant on six receivers and every edge
# ---------------------------------------------------------------------------


def grant_threading_offenders(sources):
    offenders = []
    trees = _parsed(sources)
    for key, names in GRANT_RECEIVERS.items():
        for name in names:
            found = [node for node in ast.walk(trees[key])
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
            if len(found) != 1:
                offenders.append(f"{key}::{name} not found exactly once")
                continue
            args = {arg.arg for arg in found[0].args.args + found[0].args.kwonlyargs}
            if "held_grant" not in args:
                offenders.append(f"{key}::{name} does not accept the held_grant keyword")

    bound = [
        node for node in ast.walk(trees["server"])
        if isinstance(node, ast.AsyncWith)
        and any(item.optional_vars is not None and isinstance(item.optional_vars, ast.Name)
                and item.optional_vars.id == "held_grant" for item in node.items)
    ]
    if len(bound) != 1:
        offenders.append(f"server binds the grant with 'as held_grant' {len(bound)} times; expected one")

    for (key, name), expected in GRANT_EDGES.items():
        found = [node for node in ast.walk(trees[key])
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
        if len(found) != 1:
            offenders.append(f"{key}::{name} missing for its forwarding census")
            continue
        edges: dict[str, int] = {}
        for call in ast.walk(found[0]):
            if not isinstance(call, ast.Call):
                continue
            for keyword in call.keywords:
                if keyword.arg == "held_grant" and isinstance(keyword.value, ast.Name) \
                        and keyword.value.id == "held_grant":
                    callee = _callee(call) or "<dynamic>"
                    edges[callee] = edges.get(callee, 0) + 1
        if edges != expected:
            offenders.append(f"{key}::{name} forwards {edges}, expected {expected}")
    return offenders


# ---------------------------------------------------------------------------
# 5 · the one production Layer-C construction site
# ---------------------------------------------------------------------------


def layer_c_caller_offenders(sources):
    offenders = []
    tree = ast.parse(sources["acn"])
    wrappers = [node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "create_wrapper"]
    if len(wrappers) != 1:
        offenders.append(f"acn defines {len(wrappers)} create_wrapper closures; expected exactly one")
    elif [arg.arg for arg in wrappers[0].args.args] != ["run"]:
        offenders.append("create_wrapper must take exactly one typed run argument")
    else:
        engine_calls = [_callee(node) for node in ast.walk(wrappers[0]) if isinstance(node, ast.Call)]
        if engine_calls.count("create_many") != 1 or "create" in engine_calls:
            offenders.append("all production runs, singleton included, must use the one create_many call")
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee(node) in {"_reconcile_tree_for_f12", "reconcile_tree"}
        and any(keyword.arg == "create_node" and isinstance(keyword.value, ast.Name)
                and keyword.value.id == "create_wrapper" for keyword in node.keywords)
    ]
    if len(calls) != 1:
        offenders.append(f"acn wires the typed create callback {len(calls)} times; expected one")
        return offenders
    keywords = {keyword.arg for keyword in calls[0].keywords}
    if "conclude_create_parent" not in keywords:
        offenders.append("the driver call does not supply conclude_create_parent")
    if "build_region_child_set(" not in sources["acn"]:
        offenders.append("acn never builds the caller-owned parent child-set region")
    if "_regions_cover_request(" not in sources["acn"]:
        offenders.append("acn never checks the supplied grant's coverage")
    return offenders


# ---------------------------------------------------------------------------
# 6 · A1's origin tag belongs to the endpoint response branch only
# ---------------------------------------------------------------------------


def a1_origin_tag_offenders(sources):
    offenders = []
    tree = ast.parse(sources["transport"])
    parents = _ancestors(tree)
    owners = _enclosing_functions(tree)
    assignments = [
        node for node in ast.walk(tree) if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Attribute) and target.attr == "llm_endpoint_429"
                for target in node.targets)
    ]
    if len(assignments) != 1:
        offenders.append(f"the transport writes the endpoint tag {len(assignments)} times; expected one")
    for node in assignments:
        if (owners.get(id(node)) or [None])[-1] != "insert_after":
            offenders.append("the endpoint tag is written outside insert_after")
        inside_429 = False
        parent = parents.get(id(node))
        while parent is not None:
            if isinstance(parent, ast.If) and any(
                    isinstance(item, ast.Constant) and item.value == 429
                    for item in ast.walk(parent.test)):
                inside_429 = True
                break
            parent = parents.get(id(parent))
        if not inside_429:
            offenders.append("the endpoint tag is not written under the 429 response branch")
    # The one legitimate READER is the N04 caller (its provenance check); no
    # other production module may WRITE the tag.
    for key, text in sources.items():
        if key == "transport":
            continue
        other = ast.parse(text)
        for node in ast.walk(other):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Attribute) and target.attr == "llm_endpoint_429"
                   for target in targets):
                offenders.append(f"{key} also writes the endpoint tag")
    return offenders


# ---------------------------------------------------------------------------
# Dataclass shape (the stable handoff names)
# ---------------------------------------------------------------------------


def test_n04_typed_run_and_result_fields_are_the_stable_handoff():
    assert [field.name for field in dataclasses.fields(ReconcileCreateRun)] == [
        "parent_uuid", "items", "placement", "predecessor_uuid", "parent_created"]
    assert [field.name for field in dataclasses.fields(ReconcileCreateResult)] == [
        "created_ids", "physical_placements", "ordered_child_ids", "priority_by_id",
        "reorder_required", "outcome"]
    assert dataclasses.fields(ReconcileCreateResult)[4].default is False
    assert dataclasses.fields(ReconcileCreateResult)[5].default == "v1_boundary"


def test_n04_one_create_protocol_and_no_scalar_route():
    assert create_protocol_offenders(_sources()) == []


def test_n04_cpu_stays_data_only_and_writes_no_cache():
    sources = _sources()
    assert data_only_offenders(sources) == []
    assert order_adoption_offenders(sources) == []


def test_n04_debug_rotation_admission_is_exact_and_stdlib_only():
    """The R-b diagnostic admission is not a relative-import exemption."""
    text = (ROOT / "src/workflowy_mcp/debug_log_rotation.py").read_text()
    imports = [node for node in ast.walk(ast.parse(text))
               if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert all(isinstance(node, ast.Import) for node in imports)
    assert {alias.name for node in imports for alias in node.names} == {"os", "threading"}
    for hook in ("append_debug_line", "reset_debug_log"):
        assert data_only_offenders({"cpu": f"from ..debug_log_rotation import {hook}\n"}) == []
    for source in ("from ..debug_log_rotation import *\n",
                   "from ..debug_log_rotation import live_client\n",
                   "from .debug_log_rotation import append_debug_line\n",
                   "from ..server import log_event\n", "import httpx\n"):
        assert data_only_offenders({"cpu": source}), source


def test_n04_parent_conclusion_precedes_move():
    assert conclusion_before_move_offenders(_sources()) == []


def test_n04_six_receivers_bind_and_forward_the_real_grant():
    assert grant_threading_offenders(_sources()) == []


def test_n04_the_file_wrapper_is_the_one_layer_c_caller():
    assert layer_c_caller_offenders(_sources()) == []


def test_n04_a1_origin_tag_belongs_to_the_endpoint_response():
    assert a1_origin_tag_offenders(_sources()) == []


# ---------------------------------------------------------------------------
# Teeth: every fence is a closed set, not a subset
# ---------------------------------------------------------------------------

DAMAGE = {
    "create_protocol": (
        create_protocol_offenders,
        lambda sources: {
            **sources,
            "cpu": sources["cpu"] + (
                "\n\ndef _rogue_scalar_create(desired_parent, payload):\n"
                "    yield ReconcileRequest('create', (desired_parent, payload), {})\n"),
        }),
    "cpu_data_only": (
        data_only_offenders,
        lambda sources: {**sources, "cpu": sources["cpu"] + "\nimport httpx\n"}),
    "order_adoption": (
        order_adoption_offenders,
        lambda sources: {**sources, "cpu": sources["cpu"] + (
            "\n\ndef _extra(_a=None):\n    return _adopt_create_order(None, None, None, None)\n")}),
    "conclusion_position": (
        conclusion_before_move_offenders,
        lambda sources: {**sources, "cpu": sources["cpu"].replace(
            "create_parent_done", "create_parent_done_retired", 1)}),
    "grant_forwarding": (
        grant_threading_offenders,
        lambda sources: {**sources, "mgp": sources["mgp"].replace(
            "            held_grant=held_grant,\n", "", 1)}),
    "layer_c_caller": (
        layer_c_caller_offenders,
        lambda sources: {**sources, "acn": sources["acn"].replace(
            "                    conclude_create_parent=conclude_create_parent,\n", "", 1)}),
    "a1_origin": (
        a1_origin_tag_offenders,
        lambda sources: {**sources, "tc": sources["tc"] + (
                "\n\ndef _rogue_tag(self):\n    self.llm_endpoint_429 = True\n")}),
    "singleton_alternate": (
        layer_c_caller_offenders,
        lambda sources: {**sources, "acn": sources["acn"].replace(
            "active_create.create_many(requests", "active_create.create(requests", 1)}),
}


@pytest.mark.parametrize("name", sorted(DAMAGE))
def test_n04_fences_reject_source_copy_damage(name):
    checker, damage = DAMAGE[name]
    assert checker(_sources()) == []
    assert checker(damage(_sources())) != [], f"{name} fence accepted its own damage"


# CUECDH: the changed endpoint branch gets its own current algorithm pin;
# the v1 adapter retains its historical e1334e9 algorithm, never re-blessed.
N04_LLM_INSERT_AFTER_ALGORITHM = "1b039f15bbffe1448e659eff2669f0b982f6029b9e20a761a759902b08a0c596"
N04_PUBLIC_V1_BASE_ALGORITHM = "cd4c9ecc923eee356005224f0efd71ff0854651816475766c3e51d91b0599be6"


def _transport_algorithm(source, owner, method):
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner)
    body = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == method)
    if (body.body and isinstance(body.body[0], ast.Expr)
            and isinstance(body.body[0].value, ast.Constant) and isinstance(body.body[0].value.value, str)):
        body.body.pop(0)
    return hashlib.sha256(ast.dump(body, include_attributes=False).encode()).hexdigest()


def test_a1_endpoint_current_algorithm_and_historical_v1_pin_reject_damage():
    source = _sources()["transport"]
    def verify(text):
        assert _transport_algorithm(text, "LlmInsertAfterTransport", "insert_after") == N04_LLM_INSERT_AFTER_ALGORITHM
        assert _transport_algorithm(text, "PublicV1CreateTransport", "create") == N04_PUBLIC_V1_BASE_ALGORITHM
    verify(source)
    assert source.count("error.llm_endpoint_429 = True") == 1
    with pytest.raises(AssertionError):
        verify(source.replace("error.llm_endpoint_429 = True", "error.llm_endpoint_429 = False"))
    assert source.count("owned, _internal_call=True, fetch_created_node=False,") == 1
    with pytest.raises(AssertionError):
        verify(source.replace("owned, _internal_call=True, fetch_created_node=False,",
                              "owned, _internal_call=True, fetch_created_node=True,"))
    verify(source)
