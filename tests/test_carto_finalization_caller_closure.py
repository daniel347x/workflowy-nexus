import ast
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ROOT_RUNTIME_MODULES = (
    "beacon_edits.py",
    "beacon_mutation_contract.py",
    "beacon_obtain_code_snippet.py",
    "glimpse_deploy_trampoline.py",
    "markdown_roundtrip.py",
    "nexus_map_codebase.py",
)
RUNTIME_EXCLUDED_PARTS = {
    ".edit_files_tracking",
    ".venv",
    "__pycache__",
    "archive",
    "fixtures",
    "generated",
    "scripts",
    "tests",
}
# CUT-2B 2B-2 (§6.2.2/§6.2.4, §9.7.8): the ONE memory finalization sink is the
# private registry helper in mutation_locks; both retired file finalizers are
# gone, so no claim/Q3 artifact write sits between the decision and memory.
FINALIZATION_SINK = "workflowy_mcp.mutation_locks:_finalize_carto_hold"

LIVE_MCP_OWNER = 1
COMPATIBILITY_WRAPPER = 4

# Ruling-167 lifecycle boundaries. Category-4 functions are traversed
# transitively; category 1 is the surviving lifecycle boundary where the
# caller's cleanup policy is enforced and the graph intentionally stops.
# §9.7.8/§14: the dead-owner sweep and the retired synchronous/retry/spawn
# wrappers are gone; scheduler/runner/finalizer classifications are retained.
FINALIZATION_LIFECYCLE_REGISTRY = {
    "workflowy_mcp.carto_job_governance:admit_carto_job_locked": LIVE_MCP_OWNER,
    "workflowy_mcp.carto_job_governance:finalize_claim_async": COMPATIBILITY_WRAPPER,
    "workflowy_mcp.carto_job_governance:finalize_cancelled_job_claim_async": COMPATIBILITY_WRAPPER,
    "workflowy_mcp.server:_schedule_carto_job": LIVE_MCP_OWNER,
    "workflowy_mcp.server:_run_carto_job": LIVE_MCP_OWNER,
    "workflowy_mcp.server:_finalize_carto_task": COMPATIBILITY_WRAPPER,
}

# Exact transitive graph from the one memory sink through every retained
# compatibility wrapper to a classified lifecycle boundary. §6.2.3: the sink is
# reached from the initial-job-writer rollback inside locked admission and from
# the retained async finalizer that the runner's ordinary/cancel exit uses.
EXPECTED_FINALIZATION_EDGES = Counter(
    {
        (
            "workflowy_mcp.carto_job_governance:admit_carto_job_locked",
            FINALIZATION_SINK,
        ): 1,
        (
            "workflowy_mcp.carto_job_governance:finalize_claim_async",
            FINALIZATION_SINK,
        ): 1,
        (
            "workflowy_mcp.carto_job_governance:finalize_cancelled_job_claim_async",
            "workflowy_mcp.carto_job_governance:finalize_claim_async",
        ): 1,
        (
            "workflowy_mcp.server:_finalize_carto_task",
            "workflowy_mcp.carto_job_governance:finalize_claim_async",
        ): 1,
        (
            "workflowy_mcp.server:_finalize_carto_task",
            "workflowy_mcp.carto_job_governance:finalize_cancelled_job_claim_async",
        ): 1,
        (
            "workflowy_mcp.server:_schedule_carto_job",
            "workflowy_mcp.server:_finalize_carto_task",
        ): 1,
        (
            "workflowy_mcp.server:_run_carto_job",
            "workflowy_mcp.server:_finalize_carto_task",
        ): 3,
    }
)

# Retired claim/Q3/sentinel/tombstone/cleanup artifact vocabulary. §6.2.4/§6.2.6
# delete every file writer and obligation table, so no deletion may be classified
# under these artifact classes any more.
RETIRED_DELETION_ARTIFACTS = frozenset(
    {"claim", "q3_lease", "tombstone", "sentinel", "sentinel_or_heartbeat"}
)

# Closed disk-deletion inventory: retained temp writers, job JSON/log/dir GC,
# snapshot GC, journal residue and unrelated beacon/trampoline rows only
# (§9.7.8). A row is (module-qualified caller, deletion kind, normalized target).
DELETION_CALLSITE_REGISTRY = {
    ("workflowy_mcp.server:_delete_carto_temp_source", "attribute.unlink", "Path(path)"): ("temporary_file", 1),
    ("workflowy_mcp.broker_protocol:gc_logs", "os.remove", "path"): (
        "expired_auxiliary_state",
        1,
    ),
    (
        "workflowy_mcp.carto_job_governance:_default_unlink",
        "attribute.unlink",
        "path",
    ): ("temporary_file", 1),
    # node.read(output_file=...) artifact delivery: a same-directory
    # NamedTemporaryFile this frame created itself, removed in `finally` with
    # missing_ok=True after os.replace has normally already consumed it.
    (
        "workflowy_mcp.server:_node_read_impl",
        "attribute.unlink",
        "temporary",
    ): ("temporary_file", 1),
    (
        "workflowy_mcp.config:merge_write_carto_job_status",
        "attribute.unlink",
        "tmp_path",
    ): ("temporary_file", 1),
    (
        "workflowy_mcp.server:_cache_refresh_should_skip",
        "attribute.unlink",
        "_cache_refresh_skip_file()",
    ): ("expired_auxiliary_state", 1),
    (
        "workflowy_mcp.server:_set_cache_refresh_skip",
        "attribute.unlink",
        "path",
    ): ("auxiliary_state", 1),
    # JR2 R-a (WaveSpec §7.2 retention): an unchanged FILE attempt drops
    # the F12 per-file diagnostics it wrote itself — the source JSON snapshot
    # and the `weave_journal.json` opened beside it — at the retention
    # decision. Same artifact family as the age-based F12 GC below; deletion is
    # by the explicit stored path only, for this file's own artifacts.
    (
        "workflowy_mcp.client.api_client_nexus:_write_carto_file_artifact",
        "attribute.unlink",
        "Path(values['source_json_path'])",
    ): ("expired_f12_auxiliary", 1),
    (
        "workflowy_mcp.client.api_client_nexus:_write_carto_file_artifact",
        "attribute.unlink",
        "Path(values['journal_path'])",
    ): ("expired_f12_auxiliary", 1),
    # Retained terminal job JSON deletion under the existing status-writer RLock
    # (§6.2.6): exact re-read then physical delete; no claim/sweep transaction.
    (
        "workflowy_mcp.server:_delete_carto_job_json_if_unclaimed_locked",
        "attribute.unlink",
        "job_path",
    ): ("terminal_job_json", 1),
    (
        "workflowy_mcp.server:_delete_carto_job_artifacts._unlink",
        "attribute.unlink",
        "path",
    ): ("terminal_job_log", 1),
    (
        "workflowy_mcp.server:_delete_carto_job_artifacts",
        "attribute.unlink",
        "job_path",
    ): ("terminal_job_json", 1),
    (
        "workflowy_mcp.server:_delete_carto_job_artifacts",
        "shutil.rmtree",
        "job_dir",
    ): ("terminal_job_directory", 1),
    (
        "workflowy_mcp.server:_gc_carto_jobs",
        "attribute.unlink",
        "Path(str(logs_path))",
    ): ("terminal_job_log", 1),
    (
        "workflowy_mcp.server:_gc_carto_jobs",
        "shutil.rmtree",
        "job_dir",
    ): ("terminal_job_directory", 1),
    (
        "workflowy_mcp.server:_gc_carto_jobs",
        "attribute.unlink",
        "path",
    ): ("expired_f12_auxiliary", 1),
    # JR2 R-c (WaveSpec §7.2): a terminal job folder's per-file debug artifacts
    # (`file_refresh/`) go under the same age rule; the record, `files.jsonl`
    # and the folder itself keep their own rows and rule above.
    (
        "workflowy_mcp.server:_gc_carto_jobs",
        "shutil.rmtree",
        "debug_dir",
    ): ("terminal_job_debug_artifacts", 1),
    # JR2 R-b (WaveSpec §7.2): the debug logs' rotation drops the oldest
    # generation, the generation it is about to overwrite, and the stale `.1`
    # before the current file becomes `.1`. Renames only; no replace site.
    (
        "workflowy_mcp.debug_log_rotation:_rotate",
        "os.remove",
        "oldest",
    ): ("rotated_log_generation", 1),
    (
        "workflowy_mcp.debug_log_rotation:_rotate",
        "os.remove",
        "target",
    ): ("rotated_log_generation", 1),
    (
        "workflowy_mcp.debug_log_rotation:_rotate",
        "os.remove",
        "first",
    ): ("rotated_log_generation", 1),
    (
        "workflowy_mcp.client.api_client_core:WorkFlowyClientCore._gc_nodes_export_snapshots_diagnostic",
        "os.remove",
        "path",
    ): ("expired_snapshot", 1),
    (
        "workflowy_mcp.coherence.journal_store:retire_incorporated_journal_records",
        "attribute.unlink",
        "fs_ops",
    ): ("journal_record_residue", 1),
    (
        "workflowy_mcp.coherence.journal_store:retire_incorporated_journal_records",
        "attribute.unlink",
        "path",
    ): ("journal_record_residue", 1),
    (
        "workflowy_mcp.client.api_client_core:WorkFlowyClientCore._nodes_export_snapshot_lock",
        "os.unlink",
        "lock_path",
    ): ("auxiliary_lock", 2),
    (
        "glimpse_deploy_trampoline:main",
        "shutil.rmtree",
        "staging_dir",
    ): ("deployment_staging", 1),
    (
        "glimpse_deploy_trampoline:main",
        "shutil.rmtree",
        "cache_dir",
    ): ("deployment_cache", 1),
    (
        "beacon_edits:commit_beacon_edit_plan",
        "os.unlink",
        "tmp_path",
    ): ("temporary_file", 1),
}

# §14 C: only these private mutation_locks helpers may write or delete the one
# registry containers. This is an exact helper closure, never a module-wide
# waiver: a new function inside the same module is rejected by name.
# `_release_q3_holds` removes its exact receipts through `_release_region_hold`,
# so it owns no container write of its own.
REGISTRY_CONTAINERS = ("_LOCKS.holders", "_LOCKS.cooldowns")
REGISTRY_WRITE_CLOSURE = {
    ("workflowy_mcp.mutation_locks", "_acquire_region_set"): {
        ("_LOCKS.holders", "write")
    },
    ("workflowy_mcp.mutation_locks", "_release_region_hold"): {
        ("_LOCKS.holders", "delete")
    },
    ("workflowy_mcp.mutation_locks", "_reserve_carto_hold"): {
        ("_LOCKS.holders", "write")
    },
    ("workflowy_mcp.mutation_locks", "_acquire_q3_holds"): {
        ("_LOCKS.holders", "write")
    },
    ("workflowy_mcp.mutation_locks", "_finalize_carto_hold"): {
        ("_LOCKS.cooldowns", "write")
    },
    ("workflowy_mcp.mutation_locks", "_governance_cooldowns"): {
        ("_LOCKS.cooldowns", "delete")
    },
}

# Every retired cleanup/supervisor/finalizer name (§6.2.4). "finalize_claim" is
# matched as a whole identifier so the retained async fronts never match it.
RETIRED_SUPERVISION_NAMES = frozenset(
    {
        "_PostClaimAdmissionFailure",
        "CleanupAttemptResult",
        "_finalize_claim_locked_outcome",
        "finalize_claim_locked",
        "finalize_claim",
        "_queue_claim_finalize_obligation",
        "_queue_q3_release_obligations",
        "_schedule_cleanup_obligation",
        "_cleanup_obligation_supervisor",
        "_cleanup_supervisor_done",
        "_clear_cleanup_obligation",
        "pending_cleanup_obligations",
        "retry_cleanup_obligations_async",
        "_safe_claim_id",
        "sweep_region_governance",
        "sweep_region_governance_locked",
        "cleanup_carto_auxiliary_artifacts",
        "cleanup_carto_auxiliary_artifacts_locked",
    }
)


@dataclass(frozen=True)
class _CallSite:
    caller: str
    terminal: str
    expression: str
    line: int
    callback_parameters: frozenset[str]
    node: ast.Call


@dataclass
class _Scan:
    definitions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    calls: list[_CallSite]
    by_basename: dict[str, list[str]]


def _runtime_paths() -> list[Path]:
    paths = [path for path in (REPO / "src").rglob("*.py") if path.is_file()]
    paths.extend(REPO / name for name in ROOT_RUNTIME_MODULES)
    result = []
    for path in paths:
        relative = path.relative_to(REPO)
        lowered = path.name.lower()
        if any(part in RUNTIME_EXCLUDED_PARTS for part in relative.parts):
            continue
        if ".stage2-" in lowered or lowered.endswith((".bak.py", ".backup.py")):
            continue
        if "corrupt" in lowered or "corruption" in lowered:
            continue
        result.append(path)
    return sorted(result)


def _runtime_sources() -> dict[str, str]:
    return {
        path.relative_to(REPO).as_posix(): path.read_text(encoding="utf-8")
        for path in _runtime_paths()
    }


def _module_name(relative: str) -> str:
    normalized = relative.replace("\\", "/")
    if normalized.startswith("src/"):
        normalized = normalized[4:]
    assert normalized.endswith(".py")
    return normalized[:-3].replace("/", ".")


class _RuntimeVisitor(ast.NodeVisitor):
    def __init__(self, module: str):
        self.module = module
        self.stack: list[str] = []
        self.callback_parameter_stack: list[frozenset[str]] = []
        self.definitions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.calls: list[_CallSite] = []

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        parameters = {
            argument.arg
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
        }
        if node.args.vararg is not None:
            parameters.add(node.args.vararg.arg)
        if node.args.kwarg is not None:
            parameters.add(node.args.kwarg.arg)
        self.callback_parameter_stack.append(frozenset(parameters))
        self.definitions[self._identity()] = node
        self.generic_visit(node)
        self.callback_parameter_stack.pop()
        self.stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _identity(self) -> str:
        return f"{self.module}:{'.'.join(self.stack)}"

    def visit_Call(self, node: ast.Call) -> None:
        if self.stack:
            if isinstance(node.func, ast.Name):
                terminal = node.func.id
            elif isinstance(node.func, ast.Attribute):
                terminal = node.func.attr
            else:
                terminal = ""
            if terminal:
                self.calls.append(
                    _CallSite(
                        caller=self._identity(),
                        terminal=terminal,
                        expression=ast.unparse(node.func),
                        line=node.lineno,
                        callback_parameters=(
                            self.callback_parameter_stack[-1]
                            if self.callback_parameter_stack
                            else frozenset()
                        ),
                        node=node,
                    )
                )
        self.generic_visit(node)


def _scan_sources(sources: Mapping[str, str]) -> _Scan:
    definitions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    calls: list[_CallSite] = []
    for relative, source in sorted(sources.items()):
        tree = ast.parse(source, filename=relative)
        visitor = _RuntimeVisitor(_module_name(relative))
        visitor.visit(tree)
        overlap = definitions.keys() & visitor.definitions.keys()
        assert not overlap, f"Duplicate runtime qualnames: {sorted(overlap)}"
        definitions.update(visitor.definitions)
        calls.extend(visitor.calls)
    by_basename: dict[str, list[str]] = defaultdict(list)
    for identity in definitions:
        by_basename[identity.rsplit(".", 1)[-1].split(":")[-1]].append(identity)
    return _Scan(definitions, calls, dict(by_basename))


def _resolve_call(scan: _Scan, call: _CallSite) -> str | None:
    module = call.caller.split(":", 1)[0]
    same_module_top_level = f"{module}:{call.terminal}"
    if same_module_top_level in scan.definitions:
        return same_module_top_level
    candidates = scan.by_basename.get(call.terminal, [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def _finalization_inventory(
    sources: Mapping[str, str],
) -> tuple[Counter[tuple[str, str]], list[str], set[str]]:
    scan = _scan_sources(sources)
    assert FINALIZATION_SINK in scan.definitions
    resolved = [(call, _resolve_call(scan, call)) for call in scan.calls]
    edges: Counter[tuple[str, str]] = Counter()
    targets = {FINALIZATION_SINK}
    pending = [FINALIZATION_SINK]
    while pending:
        target = pending.pop()
        for call, callee in resolved:
            if callee != target or call.caller == target:
                continue
            edges[(call.caller, target)] += 1
            category = FINALIZATION_LIFECYCLE_REGISTRY.get(call.caller)
            if category == COMPATIBILITY_WRAPPER or category is None:
                if call.caller not in targets:
                    targets.add(call.caller)
                    pending.append(call.caller)

    relevant_names = {identity.rsplit(".", 1)[-1].split(":")[-1] for identity in targets}
    unresolved = [
        (
            f"{call.caller}:{call.line} calls {call.expression!r}; "
            f"candidates={scan.by_basename.get(call.terminal, [])}"
        )
        for call, callee in resolved
        if callee is None and call.terminal in relevant_names
    ]
    return edges, unresolved, targets


def _assert_finalization_fence(sources: Mapping[str, str]) -> None:
    if os.environ.get("CUT2B_NEUTRALIZE_FINALIZATION_CLOSURE"):
        return  # Test-only fence sensitivity.
    edges, unresolved, _targets = _finalization_inventory(sources)
    callers = {caller for caller, _callee in edges}
    missing_categories = sorted(callers - FINALIZATION_LIFECYCLE_REGISTRY.keys())
    stale_categories = sorted(FINALIZATION_LIFECYCLE_REGISTRY.keys() - callers)
    new_edges = edges - EXPECTED_FINALIZATION_EDGES
    missing_edges = EXPECTED_FINALIZATION_EDGES - edges
    problems = []
    if new_edges:
        problems.append(f"new finalization edges: {dict(new_edges)}")
    if missing_edges:
        problems.append(f"missing finalization edges: {dict(missing_edges)}")
    if missing_categories:
        problems.append(f"missing lifecycle classifications: {missing_categories}")
    if stale_categories:
        problems.append(f"stale lifecycle classifications: {stale_categories}")
    if unresolved:
        problems.append(f"unresolved finalization-shaped calls: {unresolved}")
    assert not problems, "Finalization caller fence drifted. " + " | ".join(problems)


def _deletion_shape(call: _CallSite) -> tuple[str, str] | None:
    node = call.node
    if isinstance(node.func, ast.Attribute):
        owner = node.func.value
        if node.func.attr == "unlink":
            if isinstance(owner, ast.Name) and owner.id == "os":
                target = ast.unparse(node.args[0]) if node.args else "<missing>"
                return "os.unlink", target
            return "attribute.unlink", ast.unparse(owner)
        if node.func.attr == "remove" and isinstance(owner, ast.Name) and owner.id == "os":
            target = ast.unparse(node.args[0]) if node.args else "<missing>"
            return "os.remove", target
        if node.func.attr == "rmtree":
            target = ast.unparse(node.args[0]) if node.args else "<missing>"
            return "shutil.rmtree", target
        if node.func.attr == "rmdir":
            return "attribute.rmdir", ast.unparse(owner)
    if isinstance(node.func, ast.Name):
        name = node.func.id
        callback_like = name == "remover" or (
            name in call.callback_parameters
            and any(token in name.lower() for token in ("remove", "unlink"))
        )
        if callback_like:
            target = ast.unparse(node.args[0]) if node.args else "<missing>"
            return f"callback.{name}", target
    return None


def _deletion_inventory(sources: Mapping[str, str]) -> Counter[tuple[str, str, str]]:
    scan = _scan_sources(sources)
    inventory: Counter[tuple[str, str, str]] = Counter()
    for call in scan.calls:
        shape = _deletion_shape(call)
        if shape is not None:
            kind, target = shape
            inventory[(call.caller, kind, target)] += 1
    return inventory


def _assert_deletion_fence(sources: Mapping[str, str]) -> None:
    if os.environ.get("CUT2B_NEUTRALIZE_DELETION_CLOSURE"):
        return  # Test-only fence sensitivity.
    actual = _deletion_inventory(sources)
    expected = Counter(
        {key: count for key, (_artifact, count) in DELETION_CALLSITE_REGISTRY.items()}
    )
    new_calls = actual - expected
    missing_calls = expected - actual
    artifacts = {artifact for artifact, _count in DELETION_CALLSITE_REGISTRY.values()}
    problems = []
    if new_calls:
        problems.append(f"unclassified runtime deletions: {dict(new_calls)}")
    if missing_calls:
        problems.append(f"missing classified runtime deletions: {dict(missing_calls)}")
    retired = sorted(artifacts & RETIRED_DELETION_ARTIFACTS)
    if retired:
        problems.append(f"retired artifact deletions remain classified: {retired}")
    assert not problems, "Governance deletion fence drifted. " + " | ".join(problems)


def _registry_writes(sources: Mapping[str, str]) -> dict[tuple[str, str], set[tuple[str, str]]]:
    """Every write/deletion of the one registry's containers, by exact helper."""
    found: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for relative, source in sorted(sources.items()):
        tree = ast.parse(source, filename=relative)
        module = _module_name(relative)

        def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = (*scope, node.name)
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                container = ast.unparse(node.value)
                if container in REGISTRY_CONTAINERS:
                    operation = "write" if isinstance(node.ctx, ast.Store) else "delete"
                    found[(module, ".".join(scope))].add((container, operation))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                container = ast.unparse(node.func.value)
                if container in REGISTRY_CONTAINERS and node.func.attr in {
                    "pop", "popitem", "clear", "update", "setdefault"
                }:
                    operation = "write" if node.func.attr in {"update", "setdefault"} else "delete"
                    found[(module, ".".join(scope))].add((container, operation))
            for child in ast.iter_child_nodes(node):
                visit(child, scope)

        visit(tree, ())

    return found


def _assert_registry_write_closure(sources: Mapping[str, str]) -> None:
    if os.environ.get("CUT2B_NEUTRALIZE_REGISTRY_CLOSURE"):
        return  # Test-only fence sensitivity.
    declared = {key: set(value) for key, value in REGISTRY_WRITE_CLOSURE.items()}
    actual = _registry_writes(sources)
    missing = sorted(set(declared) - set(actual))
    extra = sorted(set(actual) - set(declared))
    shape_changes = sorted(
        (key, sorted(actual[key]), sorted(declared[key]))
        for key in set(declared) & set(actual)
        if actual[key] != declared[key]
    )
    foreign = sorted(key for key in actual if key[0] != "workflowy_mcp.mutation_locks")
    problems = []
    if missing:
        problems.append(f"missing registry writers: {missing}")
    if extra:
        problems.append(f"unregistered registry writers: {extra}")
    if shape_changes:
        problems.append(f"registry writer shape changes: {shape_changes}")
    if foreign:
        problems.append(f"registry writes outside mutation_locks: {foreign}")
    assert not problems, "Registry write closure drifted. " + " | ".join(problems)


def _retired_supervision_offenders(sources: Mapping[str, str]) -> list[str]:
    if os.environ.get("CUT2B_NEUTRALIZE_SUPERVISION_ABSENCE"):
        return []  # Test-only fence sensitivity.
    offenders = []
    for relative, source in sorted(sources.items()):
        tree = ast.parse(source, filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in RETIRED_SUPERVISION_NAMES:
                    offenders.append(f"retired definition {relative}:{node.name}")
            elif isinstance(node, ast.Name) and node.id in RETIRED_SUPERVISION_NAMES:
                offenders.append(f"retired reference {relative}:{node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in RETIRED_SUPERVISION_NAMES:
                offenders.append(f"retired reference {relative}:{node.attr}")
            elif isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                (alias.asname or alias.name) in RETIRED_SUPERVISION_NAMES for alias in node.names
            ):
                offenders.append(f"retired import {relative}")
    return offenders


def _mutated_sources(relative: str, addition: str) -> dict[str, str]:
    sources = _runtime_sources()
    sources[relative] += "\n\n" + addition
    return sources


def test_finalization_runtime_caller_graph_is_transitively_closed_and_classified():
    sources = _runtime_sources()
    _assert_finalization_fence(sources)
    edges, unresolved, _targets = _finalization_inventory(sources)
    assert edges == EXPECTED_FINALIZATION_EDGES
    assert not unresolved
    assert {FINALIZATION_LIFECYCLE_REGISTRY[caller] for caller, _callee in edges} == {
        LIVE_MCP_OWNER,
        COMPATIBILITY_WRAPPER,
    }
    assert FINALIZATION_SINK in {target for _caller, target in edges} and (
        "workflowy_mcp.carto_job_governance:admit_carto_job_locked",
        FINALIZATION_SINK,
    ) in edges, "the memory sink must stay reachable from locked admission rollback"


def test_runtime_deletion_inventory_is_closed_and_claim_deletion_has_one_authority():
    sources = _runtime_sources()
    _assert_deletion_fence(sources)
    _assert_registry_write_closure(sources)
    artifacts = {artifact for artifact, _count in DELETION_CALLSITE_REGISTRY.values()}
    assert {
        "terminal_job_json",
        "terminal_job_log",
        "terminal_job_directory",
        "temporary_file",
        "journal_record_residue",
    } <= artifacts, sorted(artifacts)
    # §6.2.4/§6.2.6: no file authority for claims/leases/sentinels/tombstones.
    assert RETIRED_DELETION_ARTIFACTS.isdisjoint(artifacts), sorted(
        RETIRED_DELETION_ARTIFACTS & artifacts
    )
    assert _retired_supervision_offenders(sources) == []


def test_retired_supervision_mutations_are_rejected():
    """A reintroduced cleanup/supervisor/finalizer identity is a named offender."""
    sources = {
        "src/workflowy_mcp/injected.py":
            "def finalize_claim_locked(claim):\n"
            "    return _finalize_claim_locked_outcome(claim)\n"
    }
    found = _retired_supervision_offenders(sources)
    assert any("finalize_claim_locked" in row for row in found), found


@pytest.mark.parametrize(
    ("addition", "diagnostic"),
    [
        (
            "def audit_live_sync(job_file, claim_id):\n"
            "    return _finalize_carto_hold(job_file, claim_id)\n",
            "audit_live_sync",
        ),
        (
            "async def audit_live_async(job_file, claim_id):\n"
            "    return await _finalize_carto_hold(job_file, claim_id)\n",
            "audit_live_async",
        ),
        (
            "def audit_cancel_wrapper(job_file, claim_id):\n"
            "    return _finalize_carto_hold(job_file, claim_id)\n\n"
            "def audit_cancel_live(job_file, claim_id):\n"
            "    return audit_cancel_wrapper(job_file, claim_id)\n",
            "audit_cancel_live",
        ),
        (
            "def audit_finalize_wrapper(claim_id, jobs_root):\n"
            "    return finalize_claim_async(claim_id, jobs_root)\n\n"
            "def audit_finalize_live(claim_id, jobs_root):\n"
            "    return audit_finalize_wrapper(claim_id, jobs_root)\n",
            "audit_finalize_live",
        ),
        (
            "def audit_unclassified_wrapper(claim_id, jobs_root):\n"
            "    return finalize_claim_async(claim_id, jobs_root)\n",
            "audit_unclassified_wrapper",
        ),
        (
            "async def audit_daemon_caller(claim_id, jobs_root):\n"
            "    return await finalize_claim_async(claim_id, jobs_root)\n",
            "audit_daemon_caller",
        ),
    ],
)
def test_finalization_mutations_reject_new_callers_wrappers_and_workers(
    addition: str, diagnostic: str
):
    sources = _mutated_sources("src/workflowy_mcp/server.py", addition)
    with pytest.raises(AssertionError, match=diagnostic):
        _assert_finalization_fence(sources)


@pytest.mark.parametrize(
    ("relative", "addition", "diagnostic"),
    [
        (
            "src/workflowy_mcp/server.py",
            "def audit_direct_job_delete(job_path):\n    job_path.unlink()\n",
            "audit_direct_job_delete",
        ),
        (
            "src/workflowy_mcp/server.py",
            "def audit_os_job_delete(job_path):\n    os.unlink(job_path)\n",
            "audit_os_job_delete",
        ),
        (
            "src/workflowy_mcp/server.py",
            "def audit_injected_job_delete(job_path, remover):\n    remover(job_path)\n",
            "audit_injected_job_delete",
        ),
        (
            "src/workflowy_mcp/carto_job_governance.py",
            "def audit_retired_claim_delete(claim_path):\n    claim_path.unlink()\n",
            "audit_retired_claim_delete",
        ),
    ],
)
def test_deletion_mutations_reject_direct_and_injected_claim_deletion(
    relative: str, addition: str, diagnostic: str
):
    sources = _mutated_sources(relative, addition)
    with pytest.raises(AssertionError, match=diagnostic):
        _assert_deletion_fence(sources)


@pytest.mark.parametrize(
    ("relative", "addition", "diagnostic"),
    [
        (
            "src/workflowy_mcp/mutation_locks.py",
            "def audit_unregistered_release(token):\n    del _LOCKS.holders[token]\n",
            "unregistered registry writers",
        ),
        (
            "src/workflowy_mcp/mutation_locks.py",
            "def audit_unregistered_cooldown(key, record):\n    _LOCKS.cooldowns[key] = record\n",
            "unregistered registry writers",
        ),
        (
            "src/workflowy_mcp/mutation_locks.py",
            "def audit_second_cooldown_prune(key):\n    del _LOCKS.cooldowns[key]\n",
            "unregistered registry writers",
        ),
        (
            "src/workflowy_mcp/carto_job_governance.py",
            "def audit_foreign_cooldown_prune(key):\n    del _LOCKS.cooldowns[key]\n",
            "registry writes outside mutation_locks",
        ),
        (
            "src/workflowy_mcp/carto_job_governance.py",
            "def audit_foreign_registry_delete(token):\n    del _LOCKS.holders[token]\n",
            "registry writes outside mutation_locks",
        ),
        (
            "src/workflowy_mcp/server.py",
            "def audit_foreign_registry_insert(token, hold):\n    _LOCKS.holders[token] = hold\n",
            "registry writes outside mutation_locks",
        ),
    ],
)
def test_registry_write_closure_rejects_new_helpers_and_foreign_modules(
    relative: str, addition: str, diagnostic: str
):
    sources = _mutated_sources(relative, addition)
    with pytest.raises(AssertionError, match=diagnostic):
        _assert_registry_write_closure(sources)


def _called_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    names = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.append(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.append(node.func.attr)
    return names


def _call_nodes(function: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and _called_names_of(node) == name
    ]


def _called_names_of(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _inside_except(function: ast.AST, target: ast.Call) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                for child in ast.walk(handler):
                    if child is target:
                        return True
    return False


def _assert_live_launcher_locked_outcomes(sources: Mapping[str, str]) -> None:
    """§6.2.3: reserve, write the initial job JSON, and on writer failure roll
    back THIS exact reservation synchronously. No obligation/supervisor plane."""
    if os.environ.get("CUT2B_NEUTRALIZE_LAUNCHER_ROLLBACK"):
        return  # Test-only fence sensitivity.

    scan = _scan_sources(sources)
    module = "workflowy_mcp.carto_job_governance"
    admission = scan.definitions[f"{module}:admit_carto_job_locked"]
    reserve = _call_nodes(admission, "_reserve_carto_hold")
    finalize = _call_nodes(admission, "_finalize_carto_hold")
    writer = [
        node
        for node in ast.walk(admission)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "atomic_writer"
        and node.args
        and ast.unparse(node.args[0]) == "job_file"
    ]
    assert len(reserve) == 1, "locked admission must install one exact reservation"
    assert len(writer) == 1, "locked admission must write the initial job JSON once"
    assert len(finalize) == 1, "the initial-writer rollback must be one exact finalization"
    assert reserve[0].lineno < writer[0].lineno < finalize[0].lineno, (
        "reserve before initial job JSON, rollback after the failed write"
    )
    assert _inside_except(admission, finalize[0]), (
        "the exact reservation rollback must run on the initial writer's failure path"
    )
    for name in ("_queue_claim_finalize_obligation", "_schedule_cleanup_obligation"):
        assert name not in _called_names(admission), (
            f"no cleanup obligation/supervisor may be queued: {name}"
        )

    wrapper = scan.definitions[f"{module}:admit_carto_job"]
    assert "admission_lock" in _called_names(wrapper)
    assert "async_os_lock" not in _called_names(wrapper)
    for name in ("_queue_claim_finalize_obligation", "_schedule_cleanup_obligation"):
        assert name not in _called_names(wrapper), (
            f"no cleanup obligation/supervisor may be queued: {name}"
        )
    assert "_PostClaimAdmissionFailure" not in _called_names(wrapper)
    assert not any(isinstance(node, ast.ExceptHandler) and node.type is not None
                   and ast.unparse(node.type) == "_PostClaimAdmissionFailure"
                   for node in ast.walk(wrapper))


@pytest.mark.parametrize(
    ("old", "new", "diagnostic"),
    [
        (
            "        atomic_writer(job_file, payload)",
            "        _queue_claim_finalize_obligation('job_file', 'claim_id')\n"
            "        atomic_writer(job_file, payload)",
            "no cleanup obligation/supervisor",
        ),
        ("_reserve_carto_hold(", "_reserve_carto_hold_omitted(", "one exact reservation"),
        ("_finalize_carto_hold(", "_finalize_carto_hold_omitted(", "one exact finalization"),
    ],
)
def test_live_launcher_mutations_are_rejected(old: str, new: str, diagnostic: str):
    path = "src/workflowy_mcp/carto_job_governance.py"
    sources = _runtime_sources()
    assert old in sources[path], old
    sources[path] = sources[path].replace(old, new)
    with pytest.raises(AssertionError, match=diagnostic):
        _assert_live_launcher_locked_outcomes(sources)


def test_live_launcher_locked_outcomes_are_bound_and_queued_only_after_lock_scope():
    _assert_live_launcher_locked_outcomes(_runtime_sources())
