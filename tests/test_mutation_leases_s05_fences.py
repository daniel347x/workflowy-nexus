"""CUT-1 permanent retired-plane, ontology, zero-caller and structural fences."""

from __future__ import annotations

import ast
import inspect
import os
import re
from pathlib import Path

REPO = Path(__file__).parent.parent
SRC = REPO / "src"
RETIRED = set("""
MutationLeaseSet MutationLeaseHandle MutationLeaseAcquisitionResult
MutationLeaseTimingRecord MutationSweepOutcome MutationBarrierTokens
MutationLeaseAcquisitionError MutationLeaseConflictError MutationLeasePhaseError
MutationLeaseReleaseError MutationLeaseFootprintChangedError MutationBarrierRequiredError
MutationLeaseUnclassifiableError acquire_mutation_lease_set_async acquire_revalidated_mutation_lease_async
advance_mutation_lease_phase_async mark_mutation_sent mark_mutation_verified mark_mutation_journaled
release_mutation_lease_set_async scan_mutation_leases_locked recover_dead_mutation_leases_async
advance_mutation_recovery_plans_async mutation_barrier_required_tokens capture_barrier_required_token_ids
clear_covered_barrier_required_tokens sent_verified_transaction_references mutation_lease_path_for
build_mutation_lease_set read_mutation_lease_set write_mutation_lease_set _transaction_journaled_proof
_recovery_plan_for _first_conflict_locked _acquire_lease_with_bounded_wait_async _cancel_check_async
_verify_owner_token _release_mutation_lease_locked_outcome _rescan_conflicts_after_rebase
_release_candidate_lease_after_failure _emit_lease_timing _sweep_with_optional_timing _governance_config
_require_lease_str _require_lease_bool _validate_lease_timestamp _TIMING_KEYS _PHASE_ORDER _OWNER_KINDS
_HOLD_MODES _RELEASE_DISPOSITIONS _HEX_32_RE _REFERENCE_COLLECTION_MUTEX_TIMEOUT_SECONDS
LEASE_OWNER_DIED_AFTER_SEND MUTATION_LEASE_SCHEMA_VERSION MUTATION_LEASE_PHASES
_apply_barrier_required_equivalence _refresh_claim_lease_token_coverage
""".split())
KEEP = set("""LOGGER _REGION_KINDS _REGION_KIND_ORDER _PERSISTED_CORE_RE _UUID_TEXT_RE MutationRegionError
ReservedMutationParent _lookup_region_node _RegionLookupView mutation_parent_identity region_display_key
_validate_uuid_text normalize_ap_core rehydrate_ancestor_chain MutationRegion build_region_exact_node
build_region_child_set build_region_subtree build_region_ap_core normalize_mutation_regions _chain_for
regions_intersect regions_conflict_details mutation_regions_intersect_carto_claim _COVERAGE_WARNED_KEYS
_hint_covers_parent warn_create_governance_coverage""".split())
_HOMONYMS = {"workflowy_mcp/carto_job_governance.py": {"_HEX_32_RE"},
             "workflowy_mcp/coherence/schema.py": {"_OWNER_KINDS"}}
SEAMS = {"rebase_during_admission", "establish_admission_snapshot"}


def _production_files():
    return sorted((SRC / "workflowy_mcp").rglob("*.py"))


def _source_pairs():
    return [(p.relative_to(SRC).as_posix(), p.read_text(encoding="utf-8")) for p in _production_files()]


def _symbol_names(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {alias.name.rsplit(".", 1)[-1] for alias in node.names}
    return set()


def _static_value(node, values):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_value(node.left, values) + _static_value(node.right, values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left, right = _static_value(node.left, values), _static_value(node.right, values)
        if (isinstance(left, str) and isinstance(right, int)) or (isinstance(left, int) and isinstance(right, str)):
            return left * right
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "replace"
        and len(node.args) == 2
        and not node.keywords
    ):
        # CF-16: only a statically valued string receiver with two static
        # string arguments is an assembler; anything dynamic stays unresolved.
        if os.environ.get("CUT2B_NEUTRALIZE_ACTOR_REPLACE"):
            raise ValueError("replace evaluator disabled by test probe")
        receiver = _static_value(node.func.value, values)
        old, new = _static_value(node.args[0], values), _static_value(node.args[1], values)
        if not (isinstance(receiver, str) and isinstance(old, str) and isinstance(new, str)):
            raise ValueError("not statically assembled")
        return receiver.replace(old, new)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_static_value(n, values) for n in node.elts]
    if isinstance(node, ast.JoinedStr):
        parts = []
        for n in node.values:
            if not isinstance(n, ast.FormattedValue):
                parts.append(str(_static_value(n, values)))
                continue
            value = _static_value(n.value, values)
            if n.conversion == ord("r"):
                value = repr(value)
            elif n.conversion == ord("a"):
                value = ascii(value)
            elif n.conversion == ord("s"):
                value = str(value)
            parts.append(format(value, "" if n.format_spec is None else _static_value(n.format_spec, values)))
        return "".join(parts)
    raise ValueError("not statically assembled")


def _actor_sources(tree, *, with_origins=False):
    """Resolve the retained static actor strings at their executable sinks.

    Evidence/prohibited-shape literals are not actors. This is a bounded
    static inventory, not a claim to prove arbitrary runtime reflection.
    """
    values = {}
    assignments = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    for _ in range(len(assignments) + 1):
        changed = False
        for n in assignments:
            try:
                value = _static_value(n.value, values)
            except (KeyError, ValueError, TypeError):
                continue
            for target in n.targets:
                if isinstance(target, ast.Name) and target.id not in values:
                    values[target.id] = value
                    changed = True
        if not changed:
            break
    actors = []
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
        if not call.args or name not in {"launch_actor", "run_actor", "launch_subprocess", "Popen", "run", "check_call", "check_output"}:
            continue
        argument = call.args[0]
        if isinstance(argument, (ast.List, ast.Tuple)):
            # Only the -c payload is code. Interpreter/argv expressions such
            # as sys.executable and str(path) need not be statically valued.
            for index, element in enumerate(argument.elts[:-1]):
                if isinstance(element, ast.Constant) and element.value == "-c":
                    try:
                        value = _static_value(argument.elts[index + 1], values)
                    except (KeyError, ValueError, TypeError):
                        break
                    if isinstance(value, str):
                        actors.append((call.lineno, value) if with_origins else value)
                    break
            continue
        try:
            value = _static_value(argument, values)
        except (KeyError, ValueError, TypeError):
            continue
        if name in {"launch_actor", "run_actor", "launch_subprocess"} and isinstance(value, str):
            actors.append((call.lineno, value) if with_origins else value)
        elif isinstance(value, list) and "-c" in value:
            code = value[value.index("-c") + 1]
            actors.append((call.lineno, code) if with_origins else code)
    return actors


CF16_WITNESS = "test_static_actor_sink_scans_replace_assembled_payloads"


def _witness_actor_lines(tree):
    """CF-16 witness sinks only: its own retired-name payload is test data."""
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == CF16_WITNESS:
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
                if name in {"launch_actor", "run_actor", "launch_subprocess", "Popen", "run", "check_call", "check_output"}:
                    lines.add(call.lineno)
    return lines


def _retired_offenders_in(pairs, *, tests=False):
    offenders = []
    for rel, text in pairs:
        banned = RETIRED - _HOMONYMS.get(rel, set())
        tree = ast.parse(text)
        trees = [tree]
        if tests:
            exempt = _witness_actor_lines(tree)
            trees += [
                ast.parse(actor)
                for line, actor in _actor_sources(tree, with_origins=True)
                if line not in exempt
            ]
        for part in trees:
            for node in ast.walk(part):
                for name in _symbol_names(node) & banned:
                    offenders.append(f"{rel}:{getattr(node, 'lineno', 0)} ({name})")
    return offenders


def _offenders_in(pairs, names=SEAMS):
    """Only N05's named orchestrator may call W4, including import aliases."""
    offenders = []
    for rel, text in pairs:
        tree = ast.parse(text)
        aliases = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                   for a in n.names if a.name in names}
        allowed = set()
        if rel == "workflowy_mcp/mutation_locks.py" and names == SEAMS:
            for definition in tree.body:
                if isinstance(definition, ast.AsyncFunctionDef) and definition.name == "mutation_region_locks":
                    allowed.update(ast.walk(definition))
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
            if name in names | aliases and call not in allowed:
                offenders.append(f"{rel}:{call.lineno} ({name})")
    return offenders


def _offenders_for(names=SEAMS):
    return _offenders_in(_source_pairs(), names)


def _held_consult_offenders(pairs):
    """One admission consult plus N06's exact read-only typed-region observer.

    Bounded static closure: imports, simple callable aliases, module attributes
    and constant getattr are followed. A reference escaping direct invocation
    (or the observer's one registration) is not quietly assumed harmless.
    Admission-only two-file fixtures retain their original meaning; whenever
    server.py is in the scope its one observer and registration are required.
    """
    pairs = list(pairs)
    definitions, calls, escapes = [], [], []
    readers = {"registry_held_keys", "registry_held_regions"}
    observer_name = "_node_locks_drain_busy"
    observer_definitions, registrations = [], []
    admission = ("workflowy_mcp/carto_job_governance.py", "admit_carto_job_locked")
    observer = ("workflowy_mcp/server.py", observer_name)
    has_server = any(rel == observer[0] for rel, _ in pairs)
    for rel, text in pairs:
        tree = ast.parse(text)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        module = rel.removesuffix(".py").replace("/", ".")
        aliases = {name: name for name in readers | {observer_name}}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    aliases[item.asname or item.name.split(".")[0]] = item.name if item.asname else item.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    package = module.split(".")[:-node.level]
                    prefix = ".".join([*package, *([prefix] if prefix else [])])
                for item in node.names:
                    aliases[item.asname or item.name] = (prefix + "." + item.name).strip(".")

        def target(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return target(node.value) + "." + node.attr
            if isinstance(node, ast.Call):
                callee = target(node.func)
                if callee == "getattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                    return target(node.args[0]) + "." + node.args[1].value
                if callee in {"__import__", "importlib.import_module"} and node.args and isinstance(node.args[0], ast.Constant):
                    return str(node.args[0].value)
            return "<dynamic>"

        def terminal(node):
            return target(node).rsplit(".", 1)[-1]

        # Resolve alias assignments without mistaking the RESULT of a reader
        # call for another callable. Only actual callable references propagate.
        for _ in range(len(list(ast.walk(tree))) + 1):
            changed = False
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and (
                    terminal(node.value) in readers | {observer_name}
                    or target(node.value) in {"getattr", "__import__", "importlib.import_module", "workflowy_mcp.mutation_locks", "workflowy_mcp.server"}
                ):
                    for lhs in node.targets:
                        if isinstance(lhs, ast.Name) and lhs.id not in aliases:
                            aliases[lhs.id] = target(node.value)
                            changed = True
            if not changed:
                break

        allowed_observer_refs = set()

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def visit_FunctionDef(self, n):
                if n.name in readers:
                    definitions.append((rel, ".".join([*self.stack, n.name])))
                if n.name == observer_name:
                    origin = (rel, ".".join([*self.stack, n.name]))
                    observer_definitions.append(origin)
                    expected = ast.parse("def probe():\n    return bool(registry_held_regions().regions)\n").body[0]
                    if (origin != observer or isinstance(n, ast.AsyncFunctionDef) or n.decorator_list
                            or n.args.args or n.args.posonlyargs or n.args.kwonlyargs or n.args.vararg or n.args.kwarg
                            or [ast.dump(v) for v in n.body] != [ast.dump(v) for v in expected.body]):
                        escapes.append("observer is not the single read-only bool reader")
                if n.name == "admit_carto_job_locked" and (
                    isinstance(n, ast.AsyncFunctionDef) or any(isinstance(x, (ast.Await, ast.Yield, ast.YieldFrom)) for x in ast.walk(n))
                ):
                    escapes.append("admission is not synchronous")
                self.stack.append(n.name)
                self.generic_visit(n)
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, n):
                self.stack.append(n.name)
                self.generic_visit(n)
                self.stack.pop()

            def visit_Lambda(self, n):
                self.stack.append("<lambda>")
                self.generic_visit(n)
                self.stack.pop()

            def visit_Call(self, n):
                name = terminal(n.func)
                origin = (rel, ".".join(self.stack) if self.stack else "<module>")
                if (target(n.func) == "getattr" and n.args and target(n.args[0]) == "workflowy_mcp.mutation_locks"
                        and (len(n.args) < 2 or not isinstance(n.args[1], ast.Constant) or not isinstance(n.args[1].value, str))):
                    escapes.append("unresolved held-state lookup")
                if name in readers:
                    calls.append((name, origin))
                    if target(n.func) != "workflowy_mcp.mutation_locks." + name:
                        escapes.append("foreign or unresolved held-state reader")
                if name in {"to_thread", "run_in_executor"} and any(
                    terminal(x) in readers | {observer_name} for arg in n.args for x in ast.walk(arg)
                ):
                    escapes.append("off-thread consult")
                if name == "register_drain_probe" and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "node_locks":
                    registrations.append(origin)
                    if (origin != (observer[0], "lifespan") or len(n.args) != 2 or n.keywords
                            or not isinstance(n.args[1], ast.Name) or n.args[1].id != observer_name
                            or not isinstance(n.func, ast.Attribute) or not isinstance(n.func.value, ast.Name) or n.func.value.id != "controller"):
                        escapes.append("observer registration is not the named lifespan drain probe")
                    else:
                        allowed_observer_refs.add(n.args[1])
                self.generic_visit(n)

        Visitor().visit(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Name, ast.Attribute, ast.Call)):
                continue
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                continue
            name = terminal(node)
            if name not in readers | {observer_name}:
                continue
            parent = parents.get(node)
            if name == observer_name:
                if node not in allowed_observer_refs:
                    escapes.append("observer escapes its one registered read-only use")
            elif isinstance(parent, ast.Call) and parent.func is node:
                # A direct call is counted above; it must not be awaited.
                ancestor = parent
                while ancestor in parents and not isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    if isinstance(ancestor, ast.Await):
                        escapes.append("awaited consult")
                    ancestor = parents[ancestor]
            elif isinstance(parent, ast.Assign) and parent.value is node:
                pass  # Its eventual aliases are followed, not exempted callers.
            else:
                escapes.append("held reader escapes direct synchronous invocation")
    if sorted(definitions) != [("workflowy_mcp/mutation_locks.py", name) for name in sorted(readers)]:
        escapes.append(f"definitions: {definitions}")
    expected_calls = [("registry_held_regions", admission), *([("registry_held_regions", observer)] if has_server else [])]
    if sorted(calls) != sorted(expected_calls):
        escapes.append(f"call origins: {calls}")
    if has_server and (observer_definitions != [observer] or registrations != [(observer[0], "lifespan")]):
        escapes.append(f"observer definitions/registrations: {observer_definitions}/{registrations}")
    return escapes


def test_retired_plane_and_exact_ontology_keep_set():
    assert len(RETIRED) == 58
    assert _retired_offenders_in(_source_pairs()) == []
    pairs = [(p.relative_to(REPO).as_posix(), p.read_text(encoding="utf-8")) for p in (REPO / "tests").rglob("*.py")]
    assert _retired_offenders_in(pairs, tests=True) == []
    tree = ast.parse((SRC / "workflowy_mcp/mutation_lease_governance.py").read_text(encoding="utf-8"))
    names = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            names.update(t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
    assert names == KEEP


def test_rebase_seam_caller_confinement():
    assert _offenders_for() == []


def test_one_synchronous_consult():
    assert _held_consult_offenders(_source_pairs()) == []


def test_static_actor_sink_coverage():
    """Every retained Stage-2 launch and the neutral harness/owner -c sink is scanned."""
    synthetic = 'PAYLOAD = "print(1)"\nsubprocess.Popen([sys.executable, "-c", PAYLOAD])\n'
    if os.environ.get("CUT2B_NEUTRALIZE_ACTOR_COVERAGE"):
        synthetic_sinks = set()  # Test-only fence sensitivity.
    else:
        synthetic_sinks = {
            line for line, _ in _actor_sources(ast.parse(synthetic), with_origins=True)
        }
    assert 2 in synthetic_sinks, "synthetic actor sink missed"
    paths = sorted((REPO / "tests").glob("test_stage2_*.py"))
    paths += [REPO / "tests/mutation_lease_harness.py"]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        expected = set()
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
            if name == "launch_actor":
                expected.add(call.lineno)
            elif name == "Popen" and call.args and isinstance(call.args[0], (ast.List, ast.Tuple)):
                if any(isinstance(n, ast.Constant) and n.value == "-c" for n in call.args[0].elts):
                    expected.add(call.lineno)
        scanned = {lineno for lineno, _ in _actor_sources(tree, with_origins=True)}
        if os.environ.get("CUT2B_NEUTRALIZE_ACTOR_COVERAGE"):
            scanned = set()
        assert expected <= scanned, (path.name, sorted(expected - scanned))


def _defined_names():
    names = set()
    for _relative, text in _source_pairs():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
    return names


def _class_fields(path, name, source=None):
    if source is None:
        source = (SRC / path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)
    return {
        member.target.id for member in klass.body
        if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name)
    }


RETIRED_STRUCTURAL_TYPES = {
    "SweepResult", "CleanupAttemptResult", "_PostClaimAdmissionFailure",
    # §6.5 2B-5: the whole owner-identity class retires with every consumer; the
    # class absence (not just its removed from_lease/from_claim_dict adapters)
    # is what this fence now requires.
    "StoredOwnerIdentity",
}


def _retired_structural_offenders(defined_names):
    if os.environ.get("CUT2B_NEUTRALIZE_STRUCTURAL_ABSENCE"):
        return []  # Test-only fence sensitivity.
    return sorted(RETIRED_STRUCTURAL_TYPES & set(defined_names))


def test_retired_structural_surface_rejects_source_copy():
    copy_source = (
        "class GovernancePaths:\n    jobs_root: str\n    claim_dir: str\n"
        "class StoredOwnerIdentity:\n    pid: object\n"
        "class SweepResult:\n    pass\n"
        "class CleanupAttemptResult:\n    pass\n"
        "class _PostClaimAdmissionFailure:\n    pass\n"
    )
    fields = _class_fields("workflowy_mcp/carto_job_governance.py", "GovernancePaths", source=copy_source)
    defined = {node.name for node in ast.parse(copy_source).body if isinstance(node, ast.ClassDef)}
    found = _retired_structural_offenders(defined)
    if os.environ.get("CUT2B_NEUTRALIZE_STRUCTURAL_ABSENCE"):
        found, fields = [], {"jobs_root"}
    assert set(found) == RETIRED_STRUCTURAL_TYPES, found
    assert fields != {"jobs_root"}, "GovernancePaths claim-path field probe missed"


def test_retired_structural_surface_n1_n7():
    from coherence_harness import S02_ARTIFACTS, S02_PHASES
    from workflowy_mcp.coherence import barrier, refresh_authority
    from workflowy_mcp.models.config import GovernanceConfiguration

    sources = "\n".join(text for _, text in _source_pairs())
    assert not re.search(r'mutation_leases_dir|\.mutation_leases|\.lease\.json|hard_dirty_equivalent|barrier_required_clear|"barrier_required"|mutation_lease_release|mutation_lease_recovery|"mutation_lease"', sources)
    # §14 A 929/932: the retired sweep and post-claim result types have no
    # definition left anywhere; source absence replaces runtime attribute access.
    assert _retired_structural_offenders(_defined_names()) == []
    # GovernancePaths keeps jobs_root only (no claim/lease/tombstone pathname).
    assert _class_fields("workflowy_mcp/carto_job_governance.py", "GovernancePaths") == {"jobs_root"}
    for cls, forbidden in (
        (barrier.CoherenceBoundaryStatus, {"hard_dirty_equivalent", "blocking_artifacts"}),
        (refresh_authority.RefreshOutcome, {"barrier_required_cleared", "barrier_required_clear_failed"}),
    ):
        assert forbidden.isdisjoint(cls.__dataclass_fields__)
    # §6.5/§9.9.1: the whole owner-identity family retires with every consumer.
    # The class itself — not only its removed from_lease/from_claim_dict
    # adapters — has no definition or reference left in production.
    assert "StoredOwnerIdentity" not in _defined_names()
    gov_source = (SRC / "workflowy_mcp/carto_job_governance.py").read_text(encoding="utf-8")
    assert not re.search(r"StoredOwnerIdentity|from_claim_dict|from_lease", gov_source)
    assert not re.search(r"\bpsutil\b|classify_owner_liveness", gov_source)
    assert list(inspect.signature(refresh_authority._cut_covers).parameters) == ["cut", "req_hard", "req_external"]
    assert "skip_if_lease_token_covered" not in inspect.signature(barrier.raise_global_dirty_barrier_async).parameters
    assert "admission_mutex" not in inspect.getsource(refresh_authority._acquire_or_join)
    for rel, text in _source_pairs():
        if rel.startswith("workflowy_mcp/coherence/"):
            assert not re.search("mutation_lease_governance|skip_if_lease_token_covered|req_lease_ids", text), rel
    assert not any("mutation_lease" in n or "MutationLease" in str(f.alias) for n, f in GovernanceConfiguration.model_fields.items())
    assert S02_ARTIFACTS == ("record", "manifest", "dirt", "candidate")
    assert S02_PHASES == ("temp_write", "fsynced", "replace", "unlink")


def test_static_actor_sink_scans_replace_assembled_payloads():
    """CF-16: a `.replace`-assembled -c payload is still one resolved sink."""
    payload = (
        "import workflowy_mcp.coherence.adoption as retired\n"
        "retired.adopt_manifest_head()\n"
        "from workflowy_mcp.carto_job_governance import build_claim\n"
    )
    source = (
        "TEMPLATE = 'import workflowy_mcp.coherence.adoption as retired\\n"
        "retired.adopt_manifest_head()\\n{cut2b}\\n'\n"
        "CUT2B = 'from workflowy_mcp.carto_job_governance import build_claim'\n"
        "PAYLOAD = TEMPLATE.replace('{cut2b}', CUT2B)\n"
        "subprocess.Popen([sys.executable, '-c', PAYLOAD])\n"
    )
    actors = _actor_sources(ast.parse(source))
    assert actors == [payload], "replace-assembled payload was not resolved to its sink"
    assert "adopt_manifest_head" in actors[0] and "build_claim" in actors[0]



def test_create_node_invokes_coverage_warning_itself():
    """H5c: WorkFlowyClientCore.create_node ITSELF invokes the coverage
    warning authority (a warning installed only in outer wrappers does not
    satisfy S05)."""
    source = (SRC / "workflowy_mcp" / "client" / "api_client_core.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "create_node":
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            assert "warn_create_governance_coverage" in segment, (
                "create_node no longer invokes warn_create_governance_coverage"
            )
            assert "_governance_coverage_hint" in segment
            return
    raise AssertionError("create_node not found in api_client_core.py")


def test_no_second_hard_stop_predicate():
    """H2b: the S04 sole-predicate rule is intact — no NEW module-level
    `hard_stop_state` definition exists anywhere in production."""
    definitions: list[str] = []
    for path in _production_files():
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "hard_stop_state":
                definitions.append(rel)
    assert definitions == ["workflowy_mcp/coherence/barrier.py"]
