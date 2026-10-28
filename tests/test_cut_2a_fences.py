"""CUT-2A final subtraction and BASE-preservation fences; source copies only."""
from __future__ import annotations

import ast
from collections import Counter
import copy
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "src/workflowy_mcp/"
RA = PACKAGE + "coherence/refresh_authority.py"
CP = PACKAGE + "coherence/checkpoint.py"
EP = PACKAGE + "coherence/epoch_publisher.py"
SCHEMA = PACKAGE + "coherence/schema.py"
OP = PACKAGE + "coherence/operation_cache.py"
SERVER = PACKAGE + "server.py"
CLIENT = PACKAGE + "client/api_client_core.py"
CUT_FIELDS = (
    "account_scope_id", "epoch_number", "epoch_id", "journal_head_seq",
    "hard_barrier_generation", "external_dirt_generation", "captured_at",
)
RECORD_FIELDS = (
    "account_scope_id", "claim_id", "refresh_kind", "status", "cut_token",
    "requested_hard_generation", "requested_external_generation",
    "target_epoch_number", "target_epoch_id", "started_at", "updated_at",
    "result", "candidate_snapshot_path", "basis_head_seq",
)
MARKERS = {
    "_carto_final_cleanup_permitted_locked", "_delete_carto_final_marker_if_discharged",
    "_carto_refresh_claims_dir", "_carto_final_record_path",
}
RETIRED = MARKERS | {
    "RefreshPublicationClaim", "read_refresh_claim", "write_refresh_claim",
    "_recover_dead_owner_locked", "refresh_publication_path", "claim_with_updates",
    "_validate_lease_id_tuple", "from_refresh_claim", "REFRESH_CUT_SCHEMA_VERSION",
    "_REFRESH_CUT_V1_KEYS", "_REFRESH_PUBLICATION_NAME",
}
ADOPTION = {"adopt_manifest_head", "_adopt_coherence_head"}
ADOPTION_CALLS = Counter()
WORKER_REFERENCES = set()
RETIRED |= ADOPTION | {"pin_worker_operation_cache", "_launch_phase8_refresh_via_authority"}
# §6.5 2B-5: the cjg owner-identity family is deleted together with every
# consumer (sweep/claim/cancel/Q3/startup). A reintroduced definition, helper,
# attribute or import is a retired carrier, whatever the path.
OWNER_IDENTITY_RETIRED = {
    "ProcessIdentityError", "ProcessIdentity", "_linux_process_identity",
    "_windows_process_identity", "read_process_identity", "current_process_identity",
    "StoredOwnerIdentity", "classify_owner_liveness", "_stored_identity_matches_current",
    "_caller_identity_or_defaults", "classify_legacy_pid_liveness",
}
RETIRED |= OWNER_IDENTITY_RETIRED
READERS = {
    (RA, "_store_publication_record"), (RA, "_acquire_or_join"),
    (RA, "_join_refresh"), (CP, "_acquire_checkpoint_claim"), (EP, "publish_epoch"),
}
STORERS = {(RA, "_write_claim_status"), (RA, "_acquire_or_join"), (CP, "_acquire_checkpoint_claim")}


def _sources():
    paths = list((REPO / "src/workflowy_mcp").rglob("*.py"))
    paths.extend(REPO / name for name in (
        "beacon_mutation_contract.py", "beacon_obtain_code_snippet.py",
        "glimpse_deploy_trampoline.py", "markdown_roundtrip.py", "nexus_map_codebase.py",
        "hatch_build.py",
    ))
    # Compare source bytes in one LF representation: Git blobs and the Windows
    # checkout legitimately differ in CRLF spelling, never in the bodies pinned.
    return {p.relative_to(REPO).as_posix(): p.read_text(encoding="utf-8") for p in paths}


def _walk(tree):
    """Yield real syntax with qualified origins; exclude only actual docstrings."""
    def visit(node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
        yield node, ".".join(scope) or "<module>"
        for field, value in ast.iter_fields(node):
            values = value if isinstance(value, list) else [value]
            for index, child in enumerate(values):
                if not isinstance(child, ast.AST):
                    continue
                if field == "body" and index == 0 and isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant) and isinstance(child.value.value, str):
                    continue
                yield from visit(child, scope)
    yield from visit(tree, ())


def _aliases(rows):
    bindings = {}
    for node, owner in rows:
        table = bindings.setdefault(owner, {})
        if isinstance(node, ast.ImportFrom):
            table.update({part.asname or part.name: part.name for part in node.names})
        elif isinstance(node, ast.Import):
            table.update({part.asname or part.name.split(".")[0]: part.name for part in node.names})
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and node.value is not None:
                    table[target.id] = node.value
    return bindings


def _resolve(node, owner, bindings, seen=frozenset()):
    if isinstance(node, ast.Name):
        key = owner, node.id
        if key in seen:
            return node.id
        scope = owner
        while True:
            value = bindings.get(scope, {}).get(node.id)
            if value is not None:
                return value if isinstance(value, str) else _resolve(value, scope, bindings, seen | {key})
            if scope == "<module>":
                return node.id
            scope = scope.rsplit(".", 1)[0] if "." in scope else "<module>"
    if isinstance(node, ast.Attribute):
        return _resolve(node.value, owner, bindings, seen) + "." + node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
        return _resolve(node.args[1], owner, bindings, seen)
    return "<dynamic>"


def _definitions(source):
    lines = source.splitlines(keepends=True)
    return {owner: (node, "".join(lines[node.lineno - 1:node.end_lineno]))
            for node, owner in _walk(ast.parse(source))
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}


def _negative_offenders(sources):
    if os.environ.get("CUT2B_NEUTRALIZE_NEGATIVE_CARRIERS"):
        return []  # Test-only fence sensitivity.
    offenders, calls = [], Counter()
    for path in (PACKAGE + "coherence/adoption.py", PACKAGE + "weave_worker.py"):
        if path in sources:
            offenders.append((path, "retired module"))
    for path, source in sources.items():
        if not path.startswith(PACKAGE):
            continue
        if re.search(r"refresh_publication|refresh_claims", source):
            offenders.append((path, "retired artifact path"))
        rows = list(_walk(ast.parse(source)))
        bindings = _aliases(rows)
        parents = {child: node for node, _ in rows for child in ast.iter_child_nodes(node)}
        for node, owner in rows:
            leaves = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in RETIRED or (path == RA and node.name == "_owner_identity"):
                    offenders.append((path, owner, "retired definition", node.name))
            if isinstance(node, ast.ImportFrom):
                leaves.extend(part.name for part in node.names)
                if path == PACKAGE + "coherence/__init__.py" and any(part.name in {"_PublicationRecord", "_PUBLICATION_RECORDS", "_read_publication_record", "_store_publication_record", "_publication_key", "_active_candidate_snapshot_paths"} for part in node.names):
                    offenders.append((path, "private publication export"))
            elif isinstance(node, (ast.Name, ast.Attribute)) and (
                isinstance(node.ctx, ast.Load)
                or (isinstance(node, ast.Name) and node.id in RETIRED)
            ):
                leaves.append(node.id if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
                              else _resolve(node, owner, bindings).rsplit(".", 1)[-1])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
                leaves.append(_resolve(node, owner, bindings).rsplit(".", 1)[-1])
            for leaf in leaves:
                if leaf in RETIRED or (path == RA and leaf == "_owner_identity"):
                    offenders.append((path, owner, "retired reference", leaf))
                if path in {RA, CP} and leaf in {"classify_owner_liveness", "StoredOwnerIdentity", "current_process_identity", "process_identity", "getpid"}:
                    offenders.append((path, owner, "refresh liveness", leaf))
                exported = path == PACKAGE + "coherence/__init__.py" and owner == "<module>"
                if leaf in ADOPTION and not exported and (path, owner, leaf) not in ADOPTION_CALLS:
                    offenders.append((path, owner, "daemon adoption", leaf))
                if leaf in {"pin_worker_operation_cache", "_adopt_phase8"} and not exported and (path, owner, leaf) not in WORKER_REFERENCES:
                    offenders.append((path, owner, "worker-root edge", leaf))
                if leaf == "_PUBLICATION_RECORDS" and (path != RA or owner not in {"<module>", "_read_publication_record", "_store_publication_record", "_active_candidate_snapshot_paths"}):
                    offenders.append((path, owner, "record table escape"))
            if isinstance(node, ast.Call):
                leaf = _resolve(node.func, owner, bindings).rsplit(".", 1)[-1]
                if path in {RA, CP, EP} and leaf in {"Lock", "Event", "Queue"}:
                    offenders.append((path, owner, "second publication lock/wake"))
                if leaf in ADOPTION:
                    calls[path, owner, leaf] += 1
                if leaf == "_read_publication_record" and (path, owner) not in READERS:
                    offenders.append((path, owner, "record reader/persistence escape"))
                if leaf == "_store_publication_record" and (path, owner) not in STORERS:
                    offenders.append((path, owner, "record store escape"))
                if path in {RA, CP} and leaf in {"RefreshCutToken", "_PublicationRecord"}:
                    cursor, locked = node, False
                    while cursor in parents:
                        cursor = parents[cursor]
                        if isinstance(cursor, ast.AsyncWith):
                            locked |= any("coherence_lock_async" in ast.unparse(item.context_expr) for item in cursor.items)
                    if not locked:
                        offenders.append((path, owner, "cut/record acquisition outside coherence lock"))
    if calls != ADOPTION_CALLS:
        offenders.append(("adoption call set", dict(calls)))
    package = {p: s for p, s in sources.items() if p.startswith(PACKAGE)}
    legacy = "covered_barrier_required_lease_ids"
    if sum(s.count(legacy) for s in package.values()) != 1:
        offenders.append(("legacy receipt occurrence count",))
    if sum(s.count("barrier_required_lease_ids") for s in package.values()) != 1:
        offenders.append(("retired cut receipt suffix",))
    schema = _definitions(sources[SCHEMA])
    body = schema["CoherenceManifest.from_dict"][0].body
    expected = ast.parse('payload = {k: v for k, v in payload.items() if k != "covered_barrier_required_lease_ids"}').body[0]
    if len(body) < 3 or ast.dump(body[1]) != ast.dump(expected) or body[1].lineno != body[1].end_lineno:
        offenders.append(("legacy one-line input filter",))
    for path, name, fields, methods in (
        (SCHEMA, "RefreshCutToken", CUT_FIELDS, {"__post_init__"}),
        (RA, "_PublicationRecord", RECORD_FIELDS, {"__post_init__", "is_terminal"}),
    ):
        definition = _definitions(sources[path]).get(name)
        if definition is None:
            offenders.append((path, name, "missing memory record/cut definition"))
            continue
        node = definition[0]
        actual = tuple(n.target.id for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name))
        actual_methods = {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        extra_members = [n for n in node.body if not isinstance(n, (ast.AnnAssign, ast.FunctionDef, ast.AsyncFunctionDef)) and not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))]
        if actual != fields or actual_methods != methods or extra_members:
            offenders.append((path, name, "record/cut field or serializer shape", actual, actual_methods))
    declarations = [n for n in ast.parse(sources[RA]).body
                    if isinstance(n, (ast.AnnAssign, ast.Assign))
                    and any(isinstance(target, ast.Name) and target.id == "_PUBLICATION_RECORDS"
                            for target in ([n.target] if isinstance(n, ast.AnnAssign) else n.targets))]
    if len(declarations) != 1 or not isinstance(declarations[0], ast.AnnAssign) or ast.unparse(declarations[0].annotation) != "dict[str, _PublicationRecord]" or not isinstance(declarations[0].value, ast.Dict):
        offenders.append((RA, "process-global record table declaration"))
    return offenders


def _without_docstrings(node):
    """The same AST dump, copying only paths that actually lose a docstring.

    Never mutate the caller's AST. A full deepcopy of every definition made
    every one-file negative repeat most of server.py's allocation work.
    """
    scopes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

    def strip(part):
        updates = {}
        for field, original in ast.iter_fields(part):
            if isinstance(original, ast.AST):
                value = strip(original)
                if value is not original:
                    updates[field] = value
            elif isinstance(original, list):
                children = original
                if field == "body" and isinstance(part, scopes) and children \
                        and isinstance(children[0], ast.Expr) \
                        and isinstance(children[0].value, ast.Constant) \
                        and isinstance(children[0].value.value, str):
                    children = children[1:]
                value = [strip(child) if isinstance(child, ast.AST) else child for child in children]
                if children is not original or any(a is not b for a, b in zip(value, original)):
                    updates[field] = value
        if not updates:
            return part
        clone = copy.copy(part)
        for field, value in updates.items():
            setattr(clone, field, value)
        return clone

    return ast.dump(strip(node))


UNCHANGED_FILES = (
    "mutation_lease_governance.py", "carto_job_runtime.py",
    "__main__.py", "daemon.py", "shim.py", "daemon_identity.py", "server_bootstrap.py",
    "daemon_lifecycle.py", "broker.py", "transport.py",
)
PROTECTED = {
    RA: ("_cut_covers", "RefreshOutcome", "bootstrap_coherence_from_legacy", "_required_generations"),
    PACKAGE + "coherence/snapshot_store.py": ("validate_snapshot_lineage",),
    SCHEMA: ("_reject_unknown_keys", "manifest_with_updates", "write_manifest", "initialize_manifest_if_absent", "new_claim_id"),
    OP: ("establish_admission_snapshot", "_capture_current_admission_snapshot", "rebase_during_admission", "publish_local_cache_view", "create_isolated_context_task"),
    CP: ("request_checkpoint_if_due", "_scheduled_checkpoint"),
}
SERVER_REMOVED_OR_MODIFIED = MARKERS | {
    "_carto_select_live_basis", "_carto_admission_coherence_kwargs", "_clear_carto_job_history_data", "_gc_carto_jobs",
} | {
    # 2B-2 (§14 A server rows 937-943): immutable data-only history scan, W12
    # classification, GC protection capture, deletion helpers, cancellation and
    # the runner/status carriers lose their claim/OS/process plane. None of the
    # retained CARTO semantics in these definitions is pinned by byte, so each
    # is excluded by name from the unchanged-definition pin below.
    "_read_carto_heartbeat_diagnostic", "_scan_carto_job_files", "_scan_carto_job_inventory_locked",
    "_read_and_mark_abandoned_carto_tasks", "_mark_abandoned_carto_tasks_at_startup",
    "_carto_live_job_projection", "_scan_carto_job_files_async", "_carto_gc_protection",
    "_carto_gc_paths", "_delete_carto_job_json_if_unclaimed_locked",
    "_delete_carto_job_json_if_unclaimed", "_delete_carto_job_json_if_unclaimed_async",
    "_delete_carto_job_artifacts", "_clear_carto_job_history", "_sweep_carto_governance",
    "_carto_account_scope_and_env", "_schedule_carto_job",
    "_run_carto_job", "_write_carto_task_running", "_finalize_carto_task",
    "_carto_job_cancel_requested", "_system_job_cancel_impl", "_lightning_rod_mutate_beacons",
    "_acquire_single_beacon_q3_lease", "_beacon_fix_impl", "_release_q3_leases_preserving_primary",
    "_handle_carto_cancel_job", "build_system_registry", "_run_carto_job_gc_pass", "lifespan",
    "_select_manifest_linked_snapshot",
}
# §6.4 deletes the selector; all other BASE definitions remain independently pinned.
# 2B-2 (§14 A 928): the one registry's own six converted definitions. Every
# other definition in mutation_locks.py keeps its BASE bytes.
MUTATION_LOCKS_EVOLVED = {
    "_RegionHold", "_RegionRegistry", "_acquire_region_set", "_release_region_hold",
    "mutation_region_locks", "registry_held_regions",
}
# §14 A 928/§14 C: the one registry's new private state helpers. The three pure
# projections/normalizations (`_normalized_governance_root`, `_immutable_footprint`,
# `_project_governance_hold`) are read-only carriers of the mechanical-helper
# clause; none of them is a direct container writer (the exact writer closure is
# pinned in the caller-closure fence). This is the closed added-definition list.
MUTATION_LOCKS_ADDED = {
    "_GovernanceHold", "_governance_holds", "_reserve_carto_hold", "_acquire_q3_holds",
    "_release_q3_holds", "_finalize_carto_hold", "_carto_hold_protection",
    "_require_carto_hold", "_reset_carto_region_binding", "_governance_cooldowns",
    "_normalized_governance_root", "_immutable_footprint", "_project_governance_hold",
}
# The only module-level mutation_locks declarations the conversion may not touch.
MUTATION_LOCKS_CONSTANTS = ("LOGGER", "_DEFAULT_ACQUIRE_TIMEOUT_SECONDS", "__all__", "_LOCKS")
# The exact BASE inventory these pins cover (never inferred from the final
# tree, so an omitted or new definition cannot silently disappear).
MUTATION_LOCKS_PINNED = (
    "MutationSerializationTimeoutError", "MutationSerializationConflictError",
    "_TaskLockState", "_RegionWaiter", "RegistryMutationRegions", "_RegionAcquireResult",
    "MutationRegionGrant", "_acquire_timeout_seconds", "_governor_interval_seconds",
    "_long_lived_retry_after_seconds", "_real_graph_region", "_identity",
    "_regions_cover_request", "_current_chain", "_scan_region_blockers",
    "_wake_region_waiters", "_wait_for_region_change", "_public_region_error",
    "mutation_node_locks", "held_mutation_lock_regions", "held_mutation_lock_keys",
    "registry_held_keys",
)
# Admission evolves only as named; scheduler normalization below permits only
# its one caller-window default literal and dated docstring.
CONFIG_EVOLVED = {"AdmissionConfiguration"}
CONFIG_PINNED = (
    "APIConfiguration", "PathsConfiguration", "WebSocketConfiguration",
    "TimingConfiguration", "RetentionConfiguration", "WriteGovernorConfiguration",
    "GovernanceConfiguration", "CoherenceConfiguration", "ServerConfig",
)
CONFIG_RETIRED_ADMISSION_FIELDS = ("start_grace_seconds", "spawn_grace_seconds", "orphan_grace_seconds")
# §14 A 930 declares `admission_lock`; `_configured_cooldown_seconds` and
# `_hold_footprint` are the thin lookup/projection carriers authorized by the
# mechanical-helper clause (PZJMSK), logged by the implementer with the exact
# edges they serve. Any other new top-level definition must be declared here
# (the concatenated pin is a closed inventory, never an inferred current-tree
# list).
GOVERNANCE_ADDED_DEFINITIONS = {
    "admission_lock", "_configured_cooldown_seconds", "_hold_footprint",
    "_replace_with_retry",
}
# §14 A 938 adds the job-directory helper used by the startup/GC path.
SERVER_ADDED_DEFINITIONS = {"_ensure_carto_job_directory"}
# Real code changes in otherwise-retained definitions (never prose-only: the
# pins below use docstring-free AST, so a docstring edit alone cannot need an
# exclusion). `_emit`/`governance_paths` lose their artifact/claim fields;
# `_carto_jobs_watcher_loop` loses its sweep/cleanup maintenance calls.
# §6.2.4 retires the cleanup flags/branch from `_carto_scheduling_failure_result`
# (the retained scheduling-failure presentation keeps its success/error fields).
GOVERNANCE_RETAINED_SHAPE_CHANGED = {"_emit", "governance_paths"}
SERVER_RETAINED_SHAPE_CHANGED = {"_carto_jobs_watcher_loop", "_carto_scheduling_failure_result"}
# §5G5AWG / §6.2.3: the ruled admission decision order, from the surviving
# stage calls. The initial job JSON write is the only `atomic_writer(job_file…)`.
ADMISSION_DECISION_STAGES = (
    "validate_footprint", "footprints_overlap", "registry_held_regions",
    "_governance_cooldowns", "_reserve_carto_hold", "atomic_writer",
)

# 2026-10-20: the separately authorized note-report/text-file feature evolves
# these content/parameter definitions, not CUT-2A's preserved CARTO planes.
# The remaining server pin is derived from the merged pre-J 481c27e bytes with
# only these named definitions excluded, never blessed from the changed tree.
NOTE_WRITE_EVOLUTION = {
    "_resolve_note_diff_mode", "_note_diff_small_changes", "_note_diff_change_spans",
    "_collapse_note_diff_line", "_note_diff_seam_lines", "_render_note_unified_diff",
    "_node_edit_impl", "_node_edit_core", "build_node_registry",
}
# 2026-10-21: the separately certified Beacon integrity check and Beacon
# Robustness programs, merged with main 9fbf777, add or evolve these beacon
# verb/front definitions plus J's data-only bulk inner-loop seam, not CUT-2A's
# preserved CARTO planes. Same rule: the pin is recomputed from merged 481c27e with
# only these named definitions excluded.
BEACON_PROGRAM_EVOLUTION = {
    "_beacon_check_impl", "_beacon_check_run", "_beacon_fix_impl",
    "_apply_render_symbols_tier", "_beacon_snippet_impl", "_beacon_sync_node_impl",
    "_beacon_tags_mutate_impl", "_bulk_apply_per_file_inner_loop", "_check_disk_target_uniqueness",
    "_lightning_rod_mutate_beacons", "_read_text_snippet_result_impl", "build_beacon_registry",
    "update_beacon_from_node",
}
# J-2/J-3's named data/gate/bulk joins only; refresh launcher stays pinned.
J_SERVER_EVOLUTION = {
    "_handle_refresh_file_node", "_carto_task_terminal_payload", "_carto_terminal_payload_data",
    "_run_carto_bulk_visible_apply_job", "_start_carto_bulk_visible_apply_job",
}
# JR2 G4 C1 (§append-only job record core): J's data-only terminal projection
# now recognizes the writer-summary marker, so it takes its own current
# `server:algorithm:` pin in the same exact-name loop as every JR2 admission;
# its J ingress stays excluded by J_SERVER_EVOLUTION and the aggregate
# remainder keeps the historical 481c27e bytes.
J_SERVER_ALGORITHM_EVOLUTION = {"_carto_terminal_payload_data"}

# JR3 G5 C2: exact-name admissions, each with its own current algorithm pin.
# The unchanged dispatcher's small bulk return is normalized by the J ingress.
# The historical remainder still comes from 481c27e, never the changed tree.
JR3_SERVER_EVOLUTION = {
    "_bulk_apply_per_file_inner_loop", "_carto_task_terminal_payload",
    "_run_carto_bulk_visible_apply_job", "_start_carto_bulk_visible_apply_job",
    "_write_bulk_job_artifacts", "_write_carto_job_payload",
    "_start_carto_markdown_generate_job",
}
# N02 G1 evolves exactly these existing methods. Their algorithm pins below
# protect the new N02 AST; only the remainder uses historical b1c6f35b bytes.
N02_CLIENT_EVOLUTION = {"_send_workflowy_write", "update_node", "workflowy_add_tag", "workflowy_remove_tag", "workflowy_rename_tag", "_apply_update_to_nodes_export_cache"}
# N03 admits only controller ownership and the private raw-listing carrier.
N03_CLIENT_EVOLUTION = {"__init__", "list_nodes"}
# N02 G2: exactly the newly changed server bodies. Remainder re-derived
# independently from 481c27e, never from these evolved implementation bodies.
N02_SERVER_EVOLUTION = {
    "_mutation_refusal_extra", "_mcp_exception_mapper", "_lightning_rod_mutate_tags",
    "_anchor_point_touch_impl", "_touch_content_fingerprint", "_anchor_point_extract_impl",
    "_anchor_point_promote_impl", "_handle_anchor_dock_update", "build_tag_registry", "build_anchor_registry",
}
# N04 G1 ([ap:TIGASU] §14.1): `_run_carto_job` binds the job's own admitted
# reservation to the one whole-job graph grant and `_execute_carto_job`
# forwards that grant to the file/folder/bulk bodies. `_run_carto_job` is
# already excluded from the server aggregate (SERVER_REMOVED_OR_MODIFIED);
# `_execute_carto_job` is admitted here by exact name only, no prefix or
# directory exclusion, and `server:other definitions` below is re-derived from
# merged pre-J 481c27e with exactly these names excluded.
N04_SERVER_EVOLUTION = {"_execute_carto_job"}
# 2026-10-26 job-list fix ([ap:FK5O8K]): the widget's carto_list_jobs answer
# reads record heads and sends compact, byte-budgeted rows. Exactly these two
# presentation definitions change; `server:other definitions` is re-derived
# from merged pre-J 481c27e with them excluded (the changed tree agrees).
JOB_LIST_EVOLUTION = {"_project_carto_widget_reply", "_handle_carto_list_jobs"}
# JR1 W1 ([ap:NERMWX]): list scans skip terminal proof evidence while these
# two existing detail definitions restore the requested job's full summary.
# Exclude only their names from historical 481c27e; pin each current algorithm.
JR1_DETAIL_EVOLUTION = {"_project_carto_job_status", "_system_job_status_impl"}
# JR1 W5 fix-forward ([ap:H80Q82]): the audited writer delegates replacement
# to one governance-owned helper. Historical remainder is derived from b1c6f35b;
# both the evolved writer and the declared new helper get current algorithm pins.
JR1_DURABLE_WRITE_EVOLUTION = {"atomic_write_text"}
# 2026-10-26 The Rail ([ap:Q7K9VR], merged at 82fc0f3): the GLIMPSE drawer's
# `resolve_node_titles` WebSocket action. Exactly one definition is added (the
# task-dispatched `_handle_resolve_node_titles`) and one gains a single branch
# (`_dispatch_ws_work`, whose remainder CUT-2B's kept-computation fence still
# holds to its BASE pin). `server:other definitions` is re-derived from merged
# pre-J 481c27e with them excluded (the changed tree agrees).
RAIL_SERVER_EVOLUTION = {"_handle_resolve_node_titles", "_dispatch_ws_work"}
# 79016ec adds only the Project Maps pointer to the public config read, outside
# CUT-2A's CARTO/coherence planes. Admit exactly that definition, with its full
# current source text pinned separately below; derive the remaining aggregate
# from historical 481c27e, never by blessing the changed tree.
PROJECT_MAPS_EVOLUTION = {"_system_config_get_impl"}
# STTL8I metrics performance changes only these read/projection definitions.
# Re-derive the aggregate from historical 481c27e excluding these exact names,
# and pin each current docstring-free AST below. This is a content re-pin,
# not an exemption; cache/coherence/write-governance bodies stay historical.
METRICS_SERVER_EVOLUTION = {
    "_resolve_uuid_path_and_respond", "_handle_render_slice_modal",
    "_glimpse_root_uuid_from_cache", "_lightning_rod_render_symbols",
    "_lightning_rod_from_cache", "_glimpse_slice_group_from_cache",
    "_current_cache_by_id", "_anchor_cache_by_id",
    "_anchor_dock_get_description_impl", "_anchor_dock_peek_impl",
    "_anchor_point_read_impl", "_build_lens_stage0_snapshot_from_config",
}
# I0 [ap:V93M2G]: topology-only derivative enumeration and frontier counting.
# The two existing point/description pins also evolve. Admit no publication
# definition; derive the remainder from 481c27e excluding only these names,
# and give both derivative owners their own current algorithm pins.
I0_SERVER_EVOLUTION = {"_promote_enumerate_subtree", "_anchor_point_promote_impl"}
# JR2 G2 (WaveSpec §3.1, contract [ap:C367PW]): the one-job detail surface, the
# shared record cache behind every list/startup/cancel reader, the coalesced
# list poll (protocol 2 adds its request payload), the configured GC age with
# R-c's terminal-folder debug cleanup, the generic cancel status, and the legacy
# `mcp_job_status` front that forwards to the new parameters. Exactly these
# existing or new definitions are admitted **by exact name**; the aggregate
# remainder is re-derived from merged pre-J 481c27e with them excluded (never
# from the changed tree), and each carries its own current docstring-free AST
# pin (`server:algorithm:*`) so an admission is a re-pin, never an exemption.
JR2_SERVER_EVOLUTION = {
    "_scan_carto_job_files", "_scan_carto_job_inventory_locked",
    "_scan_carto_job_files_async", "_read_and_mark_abandoned_carto_tasks",
    "_read_carto_history_candidates", "_delete_carto_job_json_if_unclaimed_locked",
    "_find_carto_job_on_disk", "_carto_list_jobs_poll", "_gc_carto_jobs",
    "_start_background_job", "_handle_carto_job_detail", "mcp_job_status",
    # JR2 G4 C1 (append-only job record core): the one exit door finalizes
    # registered writers to summary-only proof and the legacy saved-record read
    # gains its strict recovery leg. Both are existing definitions admitted by
    # exact name, so each carries its own current algorithm pin.
    "_finalize_carto_task", "_read_carto_job_payload",
}

# N04 G1 ([ap:TIGASU] §14.1): `client/carto_reconcile_cpu.py` leaves
# UNCHANGED_FILES, so its whole-file byte guarantee is replaced by three
# narrower ones measured from `git show
# b1c6f35b:src/workflowy_mcp/client/carto_reconcile_cpu.py` (the pre-N04 tree):
# every BASE definition keeps its exact bytes except the CREATE surface N04
# rewrites, every phase region of `_reconcile_steps` is pinned separately, and
# the module's import/declaration surface stays data-only. A region N04 must
# touch is disposed by name with its reason and its own current-algorithm pin
# (`cpu:algorithm-region:*`), never silently re-hashed; the generator
# remainder (everything outside the disposed span) is a historical pin, so a
# CREATE rewrite that spills past phase 1 still fails after the current pins
# are blessed.
CPU = PACKAGE + "client/carto_reconcile_cpu.py"
CPU_BASE_DEFINITION_PINS = {
    "ReconcileRequest": "01aa623bd7df35f629edee68c73962db6a234346aa237c3cfeb410bd87560e81",
    "ReconcileStep": "0544dd36c83a7ba9d7a9ee8091c4e85c35de578e12535f526993b969d58a9459",
    "CartoDiagnosticSink": "4b392e089dfd1ef107135f558f39a982d72d09b2012007219fb2b489727b7d58",
    "CartoDiagnosticSink.append_batch": "c1a63aef42b3da876d66a861e7e06aa15a573868d41f0ad0c309164e88728e8e",
    "CartoDiagnosticSink._append_batch": "8f262e75591456f56139bb436c0b03b36270fa753bf7d9ba0feef6dd9f28043c",
    "_reset_reconcile_debug": "efd0dd3c734a88e938e1c8a7a506c45107c7a345783d862c1780d64bee339a96",
    "_advance_reconcile_steps": "deaa54dd2e69806f6a9dff76260b0020734d153b763ebb09335ce8b50d8c733c",
    # N04 disposes this generator (CREATE planning + parent conclusion): the
    # BASE text hash below stays as the historical record only.
    "_reconcile_steps": "23ada1ad1025a0e2540fb19ed3525d8e88cdd051852add967e9c61fbe21d99e2",
    "_reconcile_steps.log": "19b827598ed992b74a1aeb2f68d1e9601f97bf4d60e5f46d0f6bb38221791d73",
    "_reconcile_steps.journal": "bb2afea9c963018217c92afbed02e98f27a08fbccc232fca33fd4c623a05f015",
    "_reconcile_steps._check_cancel": "69a55c5e0ad5df978896647ac55947579d6ca01a452cef71d9b8b295efa937ff",
    "_reconcile_steps.build_plan": "7c7299e2cdf58311eb1c4bd164476eb21950560827a0979bcb5e2caf143905f3",
    "_reconcile_steps.log_summary_and_close": "2fa01371655934d041e8f032eb43c78c34a7ef9284d5ec4834e7c21125e8aba0",
    "_reconcile_steps.infer_intended_parent": "66192c8c5f8ee5bdb6c86e013d7cb956d956853ed5662298897f819e3f78c300",
    "_reconcile_steps.strip_ids": "c92daa70c726cddfb76fab0268ba6406d29e5f825d05b694c39a8b48c8b40452",
    "_reconcile_steps.index_source": "ad444848ebaa4eb623972e746a8f728f6ca566c172cd00a594d20d0e9048419e",
    "_reconcile_steps.snapshot_target": "46b33a642a7abcacf2e0c3f57fb2ebad1b953b8451d0989d27a8e539f42cb0b9",
    "_reconcile_steps.snapshot_target._priority_of": "8e9cf3271cea9b40a9e18fa0acca325a57dd2fecf9c893924d3f0ab7979b9b8f",
    "_reconcile_steps.desired_maps": "0be28801d5a8eca79ef24c82ed5eaeebb52a99d385d9d144921fbdd3eb1399a6",
    "_reconcile_steps.compute_delete_roots": "7f7aee8b15b609f7312fd6d6d1324168df6717efbe44ae172f86b1265063dd71",
    "_reconcile_steps.normalize_html_entities": "22dde4ef12982ec752c62c808370c82b1bcfed9c2a48303b047a1f80d9e48678",
    "_reconcile_steps.node_data_equal": "49c1397b73503c6712c21678b77ae11e674b613e55834cf4efedd7354e6889a8",
    "_reconcile_steps.node_data_equal._strip_or_empty": "4e4b55c7370cdbef8d1ce1ebfce9c94dea728ffd63ba583fda9cbb33bc0937bf",
    "_reconcile_steps.node_data_equal._strip_or_none": "274559474de392db72c74aeaaf99c850043743401744f8c0406ab645355879cd",
    "_reconcile_steps.preorder": "2f0bee1de96fcbf19a5160ed3646dc80f270763d34c5f11340ba434f14a68b9c",
    # N04 disposes the CREATE body: its two nested synthetic-priority helpers
    # retire with the unconditional bottom-last rule (see CPU_RETIRED).
    "_reconcile_steps.ensure_created": "f1adadf3e3023da8184e1c2a74572586b6f3ac166d8dbd9862fb0856b4ff89e6",
    "_reconcile_steps.ensure_created._existing_sibling_priority": "cf6557ff539a8d72826b2868acc16e8da4dceb869c6291b5bf38a45560a6e55b",
    "_reconcile_steps.ensure_created._existing_sibling_priority_dry": "bc40509e04fbb890cc8a3765c9cc54aa9cc67aed91b2b65f8197de9d4d0c1f31",
    "_reconcile_steps._priority_of": "263ea099e77e96dcb90571714c170e7386310ebd925fe4572bb3dab7023b9e56",
    "_reconcile_steps.is_hidden_descendant": "0aac88211cd83e13a6612761e657bd6003be092374a4b6ef589c65ff9f2bd634",
    "_reconcile_steps.parent_all_children_known": "ccfebc38229a984759fc18137fc22a44f7828deb9cdb3c0ec8e00305df91c862",
    "_reconcile_steps.can_delete_candidate": "e5a5b73cd7b4ac7704906c6070b20b8bb0121a3811ced37244d33a37222cdc6b",
}
# The closed N04 CREATE surface: the two evolved BASE definitions, the two
# retired nested priority helpers, and the run/result data plus the one
# order-adoption helper. Nothing else may appear or disappear in the module.
CPU_EVOLVED = {"_reconcile_steps", "_reconcile_steps.ensure_created"}
# JR2 R-b (WaveSpec §7.2): the two writers of the daemon-wide debug logs are
# routed through the shared rotation gate. Their b1c6f35b text stays below as
# the historical record only; each carries its own current `cpu:algorithm:` pin
# and stays covered by the module import/declaration surface plus every
# untouched definition and phase region. The class pin covers the rewritten
# nested writers; `_reset_reconcile_debug` keeps its own exact pin.
# JR2 R-a (WaveSpec §7.2 retention) adds exactly one method, `finish_trace`,
# which joins one file's owned trace buffer at the retention decision. The new
# definition is admitted by exact name in the closed addition set below and
# carries its own current `cpu:algorithm:` pin; every appended-batch/reset,
# generator and phase region stays at its historical bytes.
CPU_EVOLVED_RB = {
    "CartoDiagnosticSink", "CartoDiagnosticSink._append_batch", "_reset_reconcile_debug",
}
CPU_EVOLVED = CPU_EVOLVED | CPU_EVOLVED_RB
CPU_RETIRED = {
    "_reconcile_steps.ensure_created._existing_sibling_priority",
    "_reconcile_steps.ensure_created._existing_sibling_priority_dry",
}
CPU_ADDED = {
    "ReconcileCreateRun", "ReconcileCreateResult", "_adopt_create_order",
    "CartoDiagnosticSink.finish_trace",
}
# The disposed surface, by name: these carry an exact current algorithm pin
# (`cpu:algorithm:*` in BASE_PINS), docstring-free AST, measured once after the
# production edit by the N04 gate's regeneration script — never from an
# unchanged tree, and never silently: the historical remainder, region and
# module pins below still bound them.
CPU_CURRENT_ALGORITHM_NAMES = CPU_EVOLVED | CPU_ADDED
# Region markers match full lines only: the dry-run branch's 8-space
# `return (yield from log_summary_and_close())` must not resolve the 4-space
# final return, so these are never substring searches.
CPU_PHASE0 = "    # Normalize input: accept either list of nodes or dict with metadata + nodes"
CPU_PHASE1 = "    # ------------- phase 1: CREATE (top-down) -------------"
CPU_PHASE2 = "    # ------------- phase 2: MOVE (root-to-leaf following source) -------------"
CPU_PHASE3 = "    # ------------- phase 3: REORDER siblings per parent -------------"
CPU_PHASE4 = "    # ------------- phase 4: UPDATE content -------------"
CPU_PHASE5 = "    # ------------- phase 5: DELETE (delete-roots only) -------------"
CPU_FINAL_RETURN = "    return (yield from log_summary_and_close())"
# Untouched regions keep their complete historical text, beacons included.
# A required edit must be disposed by name with a reason and a current pin;
# removing prose from every region would silently weaken the historical fence.
CPU_REGIONS = {
    "cpu:region:phase0-preamble": (CPU_PHASE0, CPU_PHASE1),
    "cpu:region:phase2-move": (CPU_PHASE2, CPU_PHASE3),
    "cpu:region:phase3-reorder": (CPU_PHASE3, CPU_PHASE4),
    "cpu:region:phase4-update": (CPU_PHASE4, CPU_PHASE5),
    "cpu:region:phase5-delete": (CPU_PHASE5, CPU_FINAL_RETURN),
}
# Disposed by name, with reason: phase 1 is where N04 replaces the
# unconditional bottom-last CREATE with run planning and authoritative-order
# adoption, and where the per-parent conclusion is emitted before phase 2.
CPU_DISPOSED_REGIONS = {
    "cpu:algorithm-region:phase1-create": (CPU_PHASE1, CPU_PHASE2),
}
# The module stays data-only: no client, publisher, registry or task import.
CPU_IMPORT_ROOTS = {"__future__", "collections", "dataclasses", "datetime", "pathlib", "typing"}
CLIENT_MODIFIED = {"_adopt_coherence_head", "_gc_nodes_export_snapshots_diagnostic", "patch_nodes_export_snapshot_on_disk", "refresh_nodes_export_cache"} | N02_CLIENT_EVOLUTION | N03_CLIENT_EVOLUTION
# §14.3 CF-47/53: only these seven method docstrings lose false worker
# consumers. Preserve their complete executable AST from the recorded BASE;
# every other method retains its existing byte pin.
CLIENT_PROSE_ONLY = frozenset({
    "_publish_lineage_view", "set_operation_cancel_callback", "update_cached_node_note",
    "create_node", "complete_node", "uncomplete_node",
})
CUT2B_GOVERNANCE_DELETIONS = {
    "SpawnRollbackResult", "finalize_cancelled_job_claim", "_merge_job_identity",
    "_transition_to_spawning", "_find_spawn_claim_locked", "_detached_worker_owns_claim",
    "_record_spawned_worker", "_rollback_spawn_failure_if_launcher_owned",
    "_spawn_governed_worker", "adopt_carto_worker_claim",
    # 2B-2 (§14 A 929-936 / §14 B): exact BASE definitions deleted or converted
    # by the one-registry admission/claim/Q3/cooldown conversion. The comment
    # above each group is the §14 row that authorizes it; nothing else is
    # excluded from the unchanged-definition pin below.
    "CleanupAttemptResult", "_PostClaimAdmissionFailure", "GovernancePaths", "SweepResult",
    "validate_claim", "validate_cancellation_sentinel", "validate_q3_lease", "validate_tombstone",
    "os_lock", "async_os_lock",
    "_artifact_key_for_record", "build_claim", "claim_path_for", "write_claim", "read_claim",
    "build_tombstone_from_claim", "tombstone_path_for", "write_tombstone_max_merge", "read_tombstone",
    "cancellation_sentinel_path", "create_cancellation_sentinel", "read_cancellation_sentinel",
    "delete_cancellation_sentinel", "build_q3_lease", "q3_lease_path_for", "write_q3_lease",
    "read_q3_lease",
    "_InteractiveClaimSnapshot", "_interactive_claim_paths", "_claim_directory_stamp",
    "_project_interactive_claim", "_warn_interactive_claim_problems",
    "read_interactive_carto_claims", "invalidate_interactive_carto_claims",
    "publish_interactive_carto_claims",
    "_find_claim_files", "_find_lease_files", "_find_tombstone_files", "_read_carto_jobs",
    "_job_by_id", "_finalize_claim_locked_outcome", "finalize_claim_locked", "finalize_claim",
    "_claim_spawn_grace_active", "_persist_reaped_status", "_legacy_job_age",
    "sweep_region_governance_locked", "sweep_region_governance", "_claim_candidates_for_job",
    "inspect_carto_job_liveness", "carto_cancel_requested",
    "_exact_job_file_in_cmdline", "_write_task_cancellation_request", "cancel_carto_job",
    "cleanup_carto_auxiliary_artifacts_locked", "cleanup_carto_auxiliary_artifacts",
    "_acquire_q3_leases_impl", "acquire_q3_leases", "acquire_q3_leases_locked",
    "acquire_q3_leases_async", "_release_q3_leases_locked_outcomes", "_release_q3_leases_impl",
    "release_q3_leases", "release_q3_leases_locked", "release_q3_leases_async",
    "pending_cleanup_obligations", "_clear_cleanup_obligation", "_cleanup_obligation_supervisor",
    "_cleanup_supervisor_done", "_schedule_cleanup_obligation",
    "_queue_q3_release_obligations", "_queue_claim_finalize_obligation",
    "finalize_claim_async", "finalize_cancelled_job_claim_async", "_safe_claim_id",
    "retry_cleanup_obligations_async", "_find_job_file_by_id", "_job_ack_from_claim",
    "_raise_for_sweep_blockers", "negative_exact_intent_gate_locked", "negative_exact_intent_gate",
    "_validated_continuation_parent", "admit_carto_job_locked", "admit_carto_job",
    "_persist_governance_cleanup_failure",
    # 2B-5 (§6.5 / §14 A 929): the eleven owner-identity definitions retire with
    # their sweep/claim/cancel/Q3/startup consumers; no definition or adapter
    # (from_lease/from_claim_dict) is pinned here any more.
    "ProcessIdentityError", "ProcessIdentity", "_linux_process_identity",
    "_windows_process_identity", "read_process_identity", "current_process_identity",
    "StoredOwnerIdentity", "classify_owner_liveness", "_stored_identity_matches_current",
    "_caller_identity_or_defaults", "classify_legacy_pid_liveness",
    "load_usable_nodes_export_snapshot",
    "acquire_os_lock_async",
}
# JR2 G4 C2 (folder /2 admission): `admit_carto_job_locked` keeps its surviving
# decision stages, but the initial writer now seeds the registered /2 record so
# FOLDER refreshes start at summary-head. The converted definition is therefore
# admitted here BY EXACT NAME with its own current docstring-free AST pin; it
# stays in the deletion bucket for the syntax scans, and `governance:other
# definitions` is re-derived from b1c6f35b with it excluded, never from the
# changed tree.
GOVERNANCE_ADMISSION_EVOLUTION = {"admit_carto_job_locked"}


def _module_assignments(source, names):
    """Exact source text of named module-level assignments, in file order."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    found = {}
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        for target in targets:
            if target in names:
                found[target] = "".join(lines[node.lineno - 1:node.end_lineno])
    assert set(found) == set(names), (sorted(found), sorted(names))
    return found


def _normalized_admission_members(source):
    """BASE AdmissionConfiguration with only the two retained members.

    §14.2 retires exactly start/spawn/orphan grace (read-discard only) and adds
    a before-validator; the cooldown field and the extra-key policy stay.
    """
    node = copy.deepcopy(_definitions(source)["AdmissionConfiguration"][0])
    kept = []
    for member in node.body:
        if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
            if member.target.id == "cooldown_seconds":
                kept.append(member)
        elif isinstance(member, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "model_config" for target in member.targets
        ):
            kept.append(member)
    assert len(kept) == 2, [ast.unparse(member) for member in kept]
    node.body = kept
    return _without_docstrings(node)


def _normalized_write_governor(source):
    """Normalize only the §14.1 caller-window default to its BASE literal."""
    node = copy.deepcopy(_definitions(source)["WriteGovernorConfiguration"][0])
    member = next(n for n in node.body if isinstance(n, ast.AnnAssign)
                  and isinstance(n.target, ast.Name) and n.target.id == "max_wait_before_refusal_seconds")
    assert isinstance(member.value, ast.Call) and isinstance(member.value.func, ast.Name) and member.value.func.id == "Field"
    assert len(member.value.args) == 1 and member.value.args[0].value in {30.0, 3600.0}
    member.value.args[0] = ast.Constant(30.0)
    return _without_docstrings(node)


def _normalized_governance_members(source):
    """W-2 / RGFF3N: omit only the added retired-lease validator from BASE text."""
    node, text = _definitions(source)["GovernanceConfiguration"]
    member = next((member for member in node.body
                   if isinstance(member, ast.FunctionDef)
                   and member.name == "_discard_retired_lease_fields"), None)
    if member is None:
        return text
    lines = source.splitlines(keepends=True)
    start = min([member.lineno, *(d.lineno for d in member.decorator_list)]) - 1
    end = member.end_lineno
    # Drop the added method's trailing separator, retaining the BASE separator
    # before it. All retained members and the original text hash stay exact.
    if end < len(lines) and not lines[end].strip():
        end += 1
    return "".join(lines[node.lineno - 1:start] + lines[end:node.end_lineno])


def _n03_server_config_projection(source):
    """Retain every historical member except precisely the new v1_only field."""
    node = copy.deepcopy(_definitions(source)["ServerConfig"][0])
    node.body = [member for member in node.body if not (
        isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name)
        and member.target.id == "v1_only"
    )]
    return _without_docstrings(node)


def _cpu_region(source, start_marker, end_marker):
    """Exact text between two unique full-line markers, start exclusive of end."""
    lines = source.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == start_marker]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == end_marker]
    assert len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0], (
        start_marker, end_marker, starts, ends)
    return "".join(lines[starts[0]:ends[0]])


def _cpu_region_ast(text):
    """House current-algorithm pin for a disposed region."""
    return ast.dump(ast.parse(textwrap.dedent(text)), include_attributes=False)


def _cpu_generator_remainder(source, disposed):
    """`_reconcile_steps` minus its disposed spans: the historical remainder.

    Measured from b1c6f35b; N04 changes only phase 1, so an edit that spills
    into UPDATE/DELETE/REORDER or into the prologue still fails it.
    """
    node = _definitions(source)["_reconcile_steps"][0]
    lines = source.splitlines(keepends=True)
    keep = lines[node.lineno - 1:node.end_lineno]
    for start, end in disposed:
        begin = next(i for i, line in enumerate(keep) if line.rstrip("\r\n") == start)
        finish = next(i for i, line in enumerate(keep) if line.rstrip("\r\n") == end)
        assert begin < finish, (start, end)
        del keep[begin:finish]
    return "".join(keep)


def _cpu_module_surface(source):
    """The two module-level surfaces that replace the whole-file byte pin."""
    tree = ast.parse(source)
    imports = ast.dump(ast.Module(
        body=[n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))],
        type_ignores=[]), include_attributes=False)
    declarations = ast.dump(ast.Module(
        body=[n for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))],
        type_ignores=[]), include_attributes=False)
    return imports, declarations


def _cpu_import_root_offenders(source):
    """Data-only by construction: every import root is a stdlib data module."""
    offenders = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom):
            roots = [(node.module or "").split(".")[0]]
        elif isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        else:
            continue
        offenders.extend(root for root in roots if root not in CPU_IMPORT_ROOTS)
    return offenders


def _cpu_definition_inventory_offenders(sources):
    """§14.1: every top-level and nested CPU definition is BASE-pinned, the
    disposed pair, or one of the closed additions — never inferred from the
    current tree, so an omitted or smuggled definition cannot disappear."""
    if os.environ.get("CUT2B_NEUTRALIZE_CPU_DEFINITION_INVENTORY"):
        return []  # Test-only fence sensitivity.
    current = set(_definitions(sources[CPU]))
    declared = (set(CPU_BASE_DEFINITION_PINS) | CPU_ADDED) - CPU_RETIRED
    return sorted(current ^ declared)


# @beacon[
#   id=nexus-tests@preservation_sites,
#   slice_labels=mci-harness,
#   kind=ast,
#   comment=Memoizes immutable per-file lock and enforcement facts by exact source text and skip sets; source-copy damage is always a different key and no AST is shared.,
# ]
@lru_cache(maxsize=256)
def _preservation_sites(path, source, governance_deletions, markers):
    """One file's unchanged scan, cached as tuples/strings, never mutable ASTs."""
    coherence_calls, n03_dirt_calls, os_authority_calls, toctou, enforced = [], [], [], [], []
    os_authority_callers = {(PACKAGE + "server_bootstrap.py", "acquire_single_writer_guard")}
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node, owner in _walk(tree):
        if path == PACKAGE + "carto_job_governance.py" and owner.split(".", 1)[0] in governance_deletions:
            continue
        if owner.split(".", 1)[0] in markers and path == SERVER:
            continue
        if isinstance(node, ast.Call):
            leaf = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if leaf == "coherence_lock_async":
                calls = n03_dirt_calls if (path, owner) == (
                    PACKAGE + "coherence/transition_create.py", "record_external_additions"
                ) else coherence_calls
                calls.append((path, owner, ast.dump(node)))
            if leaf in {"acquire_os_lock", "acquire_os_lock_async"} and (path, owner) in os_authority_callers:
                os_authority_calls.append((path, owner, leaf, ast.dump(node)))
            if path == EP and owner == "publish_epoch" and leaf == "validate_snapshot_lineage":
                cursor, under_lock = node, False
                while cursor in parents:
                    cursor = parents[cursor]
                    if isinstance(cursor, ast.AsyncWith):
                        under_lock |= any("coherence_lock_async" in ast.unparse(item.context_expr) for item in cursor.items)
                toctou.append((leaf, under_lock, ast.dump(node)))
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Compare, ast.keyword)):
            dumped = ast.dump(node)
            if "coherence_mode" in dumped and "'enforced'" in dumped:
                enforced.append((path, owner, dumped))
    return tuple(tuple(rows) for rows in (coherence_calls, n03_dirt_calls, os_authority_calls, toctou, enforced))


def _preservation_parts(sources):
    """Keep all BASE pins; reuse only unchanged per-file syntax-scan facts."""
    parts = {"file:" + name: sources[PACKAGE + name] for name in UNCHANGED_FILES}
    # 2B-2 normalizes exactly the named mutation_locks/config definitions and
    # module constants from BASE: every other definition keeps its BASE bytes,
    # so a whole-file rehash of the changed tree can never bless a regression.
    locks = _definitions(sources[PACKAGE + "mutation_locks.py"])
    for name in MUTATION_LOCKS_PINNED:
        assert name not in MUTATION_LOCKS_EVOLVED, name
        parts["mutation_locks.py:" + name] = locks[name][1]
    for name, text in _module_assignments(
        sources[PACKAGE + "mutation_locks.py"], MUTATION_LOCKS_CONSTANTS
    ).items():
        parts["mutation_locks.py:constant:" + name] = text
    config = _definitions(sources[PACKAGE + "models/config.py"])
    for name in CONFIG_PINNED:
        assert name not in CONFIG_EVOLVED, name
        parts["models/config.py:" + name] = (
            _normalized_write_governor(sources[PACKAGE + "models/config.py"])
            if name == "WriteGovernorConfiguration" else
            _normalized_governance_members(sources[PACKAGE + "models/config.py"])
             if name == "GovernanceConfiguration" else
             _n03_server_config_projection(sources[PACKAGE + "models/config.py"])
             if name == "ServerConfig" else config[name][1]
         )
    config_field = next(member for member in config["ServerConfig"][0].body
                        if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name)
                        and member.target.id == "v1_only")
    parts["models/config.py:ServerConfig.v1_only"] = ast.dump(config_field)
    parts["models/config.py:admission retained members"] = _normalized_admission_members(
        sources[PACKAGE + "models/config.py"]
    )

    # The only change to this computation is dated engine-route prose. Pin
    # the same docstring-free BASE AST, not the implementation's file bytes.
    parts["computation:coherence/transition_effects.py"] = _without_docstrings(
        ast.parse(sources[PACKAGE + "coherence/transition_effects.py"]))
    parts["file:hatch_build.py"] = sources["hatch_build.py"]
    for path, names in PROTECTED.items():
        definitions = _definitions(sources[path])
        for name in names:
            if (path, name) in {(SCHEMA, "initialize_manifest_if_absent"), (CP, "request_checkpoint_if_due")}:
                # Independent BASE normalization removes only the named sync
                # wrapper/seed call and dated docstring, not other statements.
                parts[path + ":" + name] = _without_docstrings(definitions[name][0])
            else:
                parts[path + ":" + name] = definitions[name][1]
    server = _definitions(sources[SERVER])
    parts["server:other definitions"] = "".join(
        name + "\n" + _without_docstrings(node)
        for name, (node, _text) in sorted(server.items())
        if "." not in name
        and name not in SERVER_REMOVED_OR_MODIFIED | NOTE_WRITE_EVOLUTION
        | BEACON_PROGRAM_EVOLUTION | SERVER_ADDED_DEFINITIONS | J_SERVER_EVOLUTION
        | SERVER_RETAINED_SHAPE_CHANGED | N02_SERVER_EVOLUTION | N04_SERVER_EVOLUTION
        | JOB_LIST_EVOLUTION | RAIL_SERVER_EVOLUTION | JR1_DETAIL_EVOLUTION
        | PROJECT_MAPS_EVOLUTION | JR2_SERVER_EVOLUTION | METRICS_SERVER_EVOLUTION
        | JR3_SERVER_EVOLUTION | I0_SERVER_EVOLUTION
    )
    for name in ("_run_carto_job", *sorted(N04_SERVER_EVOLUTION)):
        parts["server:algorithm:" + name] = _without_docstrings(server[name][0])
    for name in sorted(JR1_DETAIL_EVOLUTION):
        parts["server:algorithm:" + name] = _without_docstrings(server[name][0])
    for name in sorted(JR2_SERVER_EVOLUTION):
        parts["server:algorithm:" + name] = _without_docstrings(server[name][0])
    for name in sorted(METRICS_SERVER_EVOLUTION):
        parts["server:algorithm:" + name] = _without_docstrings(server[name][0])
    for name in sorted(I0_SERVER_EVOLUTION):
        parts["server:algorithm:" + name] = _without_docstrings(server[name][0])
    for name in sorted(J_SERVER_ALGORITHM_EVOLUTION):
        parts["server:algorithm:" + name] = _without_docstrings(server[name][0])
    for name in sorted(JR3_SERVER_EVOLUTION):
        parts["server:algorithm:" + name] = _without_docstrings(server[name][0])
    for name in sorted(PROJECT_MAPS_EVOLUTION):
        parts["server:definition:" + name] = server[name][1]
    client = _definitions(sources[CLIENT])
    parts["client:other methods"] = "".join(
        name + "\n" + (_without_docstrings(node)
                      if name == "WorkFlowyClientCore.__init__" or name.split(".")[-1] in CLIENT_PROSE_ONLY
                      else text)
        for name, (node, text) in sorted(client.items())
        if name.startswith("WorkFlowyClientCore.") and name.count(".") == 1
        and name.split(".")[-1] not in CLIENT_MODIFIED
    )
    for name in CLIENT_MODIFIED - {"_gc_nodes_export_snapshots_diagnostic", "_adopt_coherence_head"}:
        parts["client:algorithm:" + name] = _without_docstrings(client["WorkFlowyClientCore." + name][0])
    parts["operation boundary algorithm"] = _without_docstrings(_definitions(sources[OP])["cache_operation_boundary"][0])
    gov_path = PACKAGE + "carto_job_governance.py"
    gov = _definitions(sources[gov_path])
    parts["governance:other definitions"] = "".join(
        name + "\n" + _without_docstrings(node)
        for name, (node, _text) in sorted(gov.items())
        if "." not in name
        and name not in {"admit_carto_job"}
        | CUT2B_GOVERNANCE_DELETIONS | GOVERNANCE_ADDED_DEFINITIONS
        | GOVERNANCE_RETAINED_SHAPE_CHANGED | JR1_DURABLE_WRITE_EVOLUTION
        | GOVERNANCE_ADMISSION_EVOLUTION
    )
    for name in sorted(JR1_DURABLE_WRITE_EVOLUTION | {"_replace_with_retry"}):
        parts["governance:algorithm:" + name] = _without_docstrings(gov[name][0])
    for name in sorted(GOVERNANCE_ADMISSION_EVOLUTION):
        parts["governance:algorithm:" + name] = _without_docstrings(gov[name][0])
    compat = _definitions(sources[RA])["run_compat_patch"][1]
    parts["compat materialization/patch computation"] = compat[compat.index("        # 2. Materialize"):compat.index("        # 4. Serialize")]
    for path, names in (
        (SCHEMA, {"REFRESH_KINDS", "FULL_REFRESH_KINDS", "COMPAT_PATCH_KIND", "REFRESH_CLAIM_STATUSES", "REFRESH_CLAIM_TERMINAL_STATUSES", "_OWNER_KINDS"}),
        (RA, {"DEFAULT_SETTLE_SECONDS", "DEFAULT_POLL_SECONDS", "DEFAULT_MAX_WAIT_SECONDS"}),
    ):
        constants = [(node.targets[0].id, ast.dump(node)) for node in ast.parse(sources[path]).body
                     if isinstance(node, ast.Assign) and len(node.targets) == 1
                     and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names]
        assert {name for name, _ in constants} == names
        parts[path + ":retained constants"] = repr(sorted(constants))
    coherence_calls, n03_dirt_calls, os_authority_calls, toctou, enforced = [], [], [], [], []
    # §14 A 930 / §6.2.1: the in-process coherence facade keeps its exact held
    # spans; the two OS-acquisition context wrappers retire with their last
    # caller; the retained OS authority has only the N06 singleton guard.
    skip_governance, skip_markers = frozenset(CUT2B_GOVERNANCE_DELETIONS), frozenset(MARKERS)
    for path, source in sources.items():
        if not path.startswith(PACKAGE):
            continue
        if path in {PACKAGE + "coherence/adoption.py", PACKAGE + "weave_worker.py"}:
            continue
        facts = _preservation_sites(path, source, skip_governance, skip_markers)
        for destination, rows in zip((coherence_calls, n03_dirt_calls, os_authority_calls, toctou, enforced), facts):
            destination.extend(rows)
    parts["coherence async lock sites"] = repr(sorted(coherence_calls))
    parts["N03 exact external dirt lock site"] = repr(sorted(n03_dirt_calls))
    parts["retained OS-lock authority calls"] = repr(sorted(os_authority_calls))
    parts["candidate lineage validation site"] = repr(sorted(toctou))
    parts["existing enforced-mode syntax"] = repr(sorted(enforced))
    parts["unchanged remaining-budget floor"] = repr(sorted(ast.dump(node) for node in ast.walk(ast.parse(sources[RA]))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "max"
        and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 1.0))

    # N04 G1: the CPU module's BASE is preserved definition-by-definition and
    # region-by-region; only the named CREATE surface is disposed to its own
    # current algorithm pin. The closed inventory is asserted here so a pin
    # table that drifts from the disposition sets cannot pass silently.
    cpu = _definitions(sources[CPU])
    assert CPU_EVOLVED <= set(CPU_BASE_DEFINITION_PINS), sorted(CPU_EVOLVED - set(CPU_BASE_DEFINITION_PINS))
    for name in CPU_BASE_DEFINITION_PINS:
        if name in CPU_RETIRED or name in CPU_EVOLVED:
            continue
        parts["cpu:definition:" + name] = cpu[name][1]
    for name in CPU_CURRENT_ALGORITHM_NAMES:
        parts["cpu:algorithm:" + name] = _without_docstrings(cpu[name][0])
    for name, (start, end) in CPU_REGIONS.items():
        parts[name] = _cpu_region(sources[CPU], start, end)
    for name, (start, end) in CPU_DISPOSED_REGIONS.items():
        parts[name] = _cpu_region_ast(_cpu_region(sources[CPU], start, end))
    parts["cpu:generator remainder"] = _cpu_generator_remainder(
        sources[CPU], tuple(CPU_DISPOSED_REGIONS.values()))
    imports_surface, declarations_surface = _cpu_module_surface(sources[CPU])
    parts["cpu:module imports"] = imports_surface
    parts["cpu:module declarations"] = declarations_surface
    return parts


# Original pins use git show b1c6f35b:<path>, not the changed tree; the server
# remainder uses merged pre-J 481c27e with exactly the named exclusions. CUT-2B
# re-derives only the exact removed worker/adoption sites, the named 2B-4
# deferrals (`load_usable_nodes_export_snapshot`, `_select_manifest_linked_snapshot`),
# 2B-5's eleven deleted cjg owner-identity definitions (§6.5 — the derived
# `governance:other` aggregate is byte-identical at b1c6f35b, 7afe1f4 and the
# changed tree with exactly those names excluded), the named cleanup-branch
# change in `_carto_scheduling_failure_result`, and dated transition prose from
# 7afe1f4: the other pins below remain the original independent pins. Values
# hash LF-normalized UTF-8 bytes except the named docstring-free AST pins.
BASE_PINS = {
    "candidate lineage validation site": "46fa724fe041b94a013687a493b2b114d0140e1fd70929952845810cd9233d6b",
    "computation:coherence/transition_effects.py": "5802df997d4e0bbd9dd97b9563b9187975df546d641003222b0cfbddcc434e05",
    "client:algorithm:patch_nodes_export_snapshot_on_disk": "f958e0b001fb22827f7a9d4b9d4d8cafecd3b60e826d8ee3b5d639dd0918f526",
    "client:algorithm:refresh_nodes_export_cache": "5b31bd9e5d64e7bef43eb6010b36af48608902cda646ebc63e9b009e3445859c",
    # Fresh N03 G3 sender: G1 redirect behavior plus XVBN0E's one dispatch fact.
    "client:algorithm:_send_workflowy_write": "1eeff49917000642d8f14b41a43e75195f96fcd550464762550376edf9f6c12e",
    # G2 preserves incomplete-observation field presence, including aliases.
    "client:algorithm:update_node": "9be289a26cd489223b987db43f682948c0e3db3a2063b2c1df6bdfb1b961c9ac",
    "client:algorithm:_apply_update_to_nodes_export_cache": "8821179dc891b6deaefec1bc899ddb1e2012bbcecb3a6aeeb89815a3942be3ca",
    # Fresh N02 G2 algorithms for the three activated live-basis primitives.
    "client:algorithm:workflowy_add_tag": "e5d53604d72fb2bc55a1a9df8b86de6caba268b66a09d127f670fa2645e516c6",
    "client:algorithm:workflowy_remove_tag": "15849d9c6d3abe6a429b17be88fcf97f7e8b73f05df3e6028589c926b1fa32fe",
    "client:algorithm:workflowy_rename_tag": "6b7113a815a9efbf17f803ed15e62054203a2b80f61d001d87cd147a6b1f6018",
    # Historical b1c6f35b remainder with six N02 and two N03 exclusions. The sixth is the
    # existing patcher: N02-only presence filtering must preserve absent aliases
    # without changing legacy/Cartographer defaults or adding a publish site.
    "client:other methods": "15e4aef9f6d40011c3aaa6d9c30cfc89821db8e75ccf414c94cf73d8e3fd0412",
    # Fresh N03 algorithms, each independently mutation-sensitive.
    "client:algorithm:__init__": "74a693d12b74f788880e535796fac2586582704880601c91a6d284210de9de63",
    "client:algorithm:list_nodes": "9f26b2a8413248fd7b805837cae467541108c4d16dc1e911a87c436ac51a1b4e",
    # N04 G1 CPU: BASE (b1c6f35b) definitions, regions, total remainder and the
    # data-only module surface; the disposed CREATE surface uses its own
    # current-algorithm pins, emitted by the N04 gate's regeneration script.
    "cpu:algorithm:CartoDiagnosticSink": "ed6bd9a3cf134659e9caddf7a28db18f695b182db1152cebbb5398b054767abc",
    "cpu:algorithm:CartoDiagnosticSink._append_batch": "cc8117411a9fa792b75ddd3fceb4724581e41e79c1270d1ab41cb76e8da17708",
    "cpu:algorithm:CartoDiagnosticSink.finish_trace": "78f60967ab0be466524ae2614ca3adb27efd941e4c09de5c132e2195376f81d7",
    "cpu:algorithm:_adopt_create_order": "1efd623ac5c18dbd7d7d00b712ae717a0a4eb4a8d039d0cd8ed89148eaf054b4",
    "cpu:algorithm:_reconcile_steps": "398dfc8941c8424db098d01d709c53665f01d75653a4f376a624672df9560f32",
    "cpu:algorithm:_reconcile_steps.ensure_created": "71c64ab539c03f3cf5adf7146300dc95ed242626c27309aec64611b1f27985e9",
    "cpu:algorithm:ReconcileCreateRun": "0b20db94070a71b862dbb4ef5f37ed1ec9091d1a982210aa80c88b48d73d7ffb",
    "cpu:algorithm:ReconcileCreateResult": "17521150fe971085333f907cd3bcd2d640167a3e3d53b031c22ce4123051ff8b",
    "cpu:algorithm:_reset_reconcile_debug": "646d817329432a68e40c8abc1568559a625780f15e814c5fc9120cd8886c3285",
    "cpu:algorithm-region:phase1-create": "8440c7b25c94ce1dc85e2feef47cdcd27c12e94f3930f1a2e4055fc17e5499bc",
    "cpu:definition:CartoDiagnosticSink.append_batch": "c1a63aef42b3da876d66a861e7e06aa15a573868d41f0ad0c309164e88728e8e",
    "cpu:definition:ReconcileRequest": "01aa623bd7df35f629edee68c73962db6a234346aa237c3cfeb410bd87560e81",
    "cpu:definition:ReconcileStep": "0544dd36c83a7ba9d7a9ee8091c4e85c35de578e12535f526993b969d58a9459",
    "cpu:definition:_advance_reconcile_steps": "deaa54dd2e69806f6a9dff76260b0020734d153b763ebb09335ce8b50d8c733c",
    "cpu:definition:_reconcile_steps._check_cancel": "69a55c5e0ad5df978896647ac55947579d6ca01a452cef71d9b8b295efa937ff",
    "cpu:definition:_reconcile_steps._priority_of": "263ea099e77e96dcb90571714c170e7386310ebd925fe4572bb3dab7023b9e56",
    "cpu:definition:_reconcile_steps.build_plan": "7c7299e2cdf58311eb1c4bd164476eb21950560827a0979bcb5e2caf143905f3",
    "cpu:definition:_reconcile_steps.can_delete_candidate": "e5a5b73cd7b4ac7704906c6070b20b8bb0121a3811ced37244d33a37222cdc6b",
    "cpu:definition:_reconcile_steps.compute_delete_roots": "7f7aee8b15b609f7312fd6d6d1324168df6717efbe44ae172f86b1265063dd71",
    "cpu:definition:_reconcile_steps.desired_maps": "0be28801d5a8eca79ef24c82ed5eaeebb52a99d385d9d144921fbdd3eb1399a6",
    "cpu:definition:_reconcile_steps.index_source": "ad444848ebaa4eb623972e746a8f728f6ca566c172cd00a594d20d0e9048419e",
    "cpu:definition:_reconcile_steps.infer_intended_parent": "66192c8c5f8ee5bdb6c86e013d7cb956d956853ed5662298897f819e3f78c300",
    "cpu:definition:_reconcile_steps.is_hidden_descendant": "0aac88211cd83e13a6612761e657bd6003be092374a4b6ef589c65ff9f2bd634",
    "cpu:definition:_reconcile_steps.journal": "bb2afea9c963018217c92afbed02e98f27a08fbccc232fca33fd4c623a05f015",
    "cpu:definition:_reconcile_steps.log": "19b827598ed992b74a1aeb2f68d1e9601f97bf4d60e5f46d0f6bb38221791d73",
    "cpu:definition:_reconcile_steps.log_summary_and_close": "2fa01371655934d041e8f032eb43c78c34a7ef9284d5ec4834e7c21125e8aba0",
    "cpu:definition:_reconcile_steps.node_data_equal": "49c1397b73503c6712c21678b77ae11e674b613e55834cf4efedd7354e6889a8",
    "cpu:definition:_reconcile_steps.node_data_equal._strip_or_empty": "4e4b55c7370cdbef8d1ce1ebfce9c94dea728ffd63ba583fda9cbb33bc0937bf",
    "cpu:definition:_reconcile_steps.node_data_equal._strip_or_none": "274559474de392db72c74aeaaf99c850043743401744f8c0406ab645355879cd",
    "cpu:definition:_reconcile_steps.normalize_html_entities": "22dde4ef12982ec752c62c808370c82b1bcfed9c2a48303b047a1f80d9e48678",
    "cpu:definition:_reconcile_steps.parent_all_children_known": "ccfebc38229a984759fc18137fc22a44f7828deb9cdb3c0ec8e00305df91c862",
    "cpu:definition:_reconcile_steps.preorder": "2f0bee1de96fcbf19a5160ed3646dc80f270763d34c5f11340ba434f14a68b9c",
    "cpu:definition:_reconcile_steps.snapshot_target": "46b33a642a7abcacf2e0c3f57fb2ebad1b953b8451d0989d27a8e539f42cb0b9",
    "cpu:definition:_reconcile_steps.snapshot_target._priority_of": "8e9cf3271cea9b40a9e18fa0acca325a57dd2fecf9c893924d3f0ab7979b9b8f",
    "cpu:definition:_reconcile_steps.strip_ids": "c92daa70c726cddfb76fab0268ba6406d29e5f825d05b694c39a8b48c8b40452",
    "cpu:generator remainder": "d95c1a217498b23ca6abd4cb3f631233ed3005b3d070c5e09def999b4edc3490",
    "cpu:module declarations": "15ac885392019c9edaf6396558612cf6b05f5f7910bbe33579a247ca2b650494",
    "cpu:module imports": "d66e94c61fd750cc46154fccc07fb83d70b295b866ca5c8a3cc3b5db32e4cd7b",
    "cpu:region:phase0-preamble": "35d34581a3f594d54e8e570fa77abff9173a91e4fa140d93641adeef3ab31107",
    "cpu:region:phase2-move": "41f3cd0aae0cde35571906abf065fc187f497a4a07be6a419d1cbeb34e34c71e",
    "cpu:region:phase3-reorder": "8672726da051708d95f5a7da096276a5b287fce3e9e694d9f3ca9c6b2d1f56a2",
    "cpu:region:phase4-update": "c195e8830b0fa87dce021b8a68665e6406fd473d8dc4a703ef4338d0d9194781",
    "cpu:region:phase5-delete": "c59e928db85cbb0b0a6a4653cd428a448d950c4b38e1511aadef55f9f5567dd7",
    "compat materialization/patch computation": "30ab338e0c7e6c2ffce8d2f393127f6e1ddb510e77f3f8310a31ebf806f5dcba",
    "existing enforced-mode syntax": "7c9a63150370ce1f72bcddb8293c2fc1b05d2b23ee6f8bc85bb675c4c208d9ce",
    "file:__main__.py": "66d60edb21b1c39f2cd4653fe1dd118649f3c4fc56b8be82e9ef1268decc512c",
    "file:broker.py": "df0a87c5288c0ae76996ec15de8ba769114f9065d695d2ac7572132b25ff6121",
    "file:carto_job_runtime.py": "67725f07e242f2243cd1b8ba43801874b27cc60c87301bb7e59ed63fac9b5f5d",
    "file:daemon.py": "1193b4fb00e148f5a390504dd48a0c048c7e27ed84b6e1730990898323957e84",
    "file:daemon_identity.py": "2b16310c689e5083e481966d594a51f8949811de9d5316d98066e691fdf60af9",
    "file:daemon_lifecycle.py": "5fda77d4db85bba27392b58683d2cbdf7bd154e4aa2c73c879e511aa4ca0a0c4",
    "file:hatch_build.py": "03511e46952441676c803f90d11b0f2b64dac31de54f5da35ff3122d01d90380",
    "file:mutation_lease_governance.py": "35295c88be0c9c8bde8df086b1ea51d9ca2171756ae19cd3e530dad94c97ea0b",
    "file:server_bootstrap.py": "9c8dfa8eaa9af265a8ab827367ec39d69e9575125e6e2ad42d2d26dd112fa229",
    "file:shim.py": "9a861790aec25e56a57a13c0cff42bfb69c9599046dbcdddf7b8d332912c7064",
    "file:transport.py": "6c678aeed6b4981c4e55037e36776776f8245f0a247f632f1196c2c3b5f0145b",
    # JR1 shared-retry extraction: re-derived from b1c6f35b with only the
    # evolved atomic_write_text and declared helper additionally excluded.
    "governance:other definitions": "74f0e975eb2b60b8e6370b1414cb1ce614e85a0f02eee9cf67a08a44bad97132",
    "governance:algorithm:atomic_write_text": "4831642dbe2f57e03279c7ed51ac30ab24fe58da284eddcfa74f586853e8a7ae",
    "governance:algorithm:_replace_with_retry": "7daa69c3502a3da435f5b04c22512584d183997989b23bfb081d08a792b4f68d",
    # JR2 G4 C2: the converted locked admission, admitted by exact name with
    # its own current docstring-free AST (the /2 preparation before the one
    # initial job JSON write). The historical remainder above still binds every
    # definition it does not exclude.
    "governance:algorithm:admit_carto_job_locked": "ee760a133c0c5c8fa0717c7e30c10d610e1e7ff4498baf28e439b572c3798e2f",
    "operation boundary algorithm": "2fd9b2e22f405dbd448e80da1d5180e822c48d450dbc735a620130361442ee0c",
    # Re-derived from merged pre-J 481c27e with JOB_LIST_EVOLUTION,
    # RAIL_SERVER_EVOLUTION, JR1_DETAIL_EVOLUTION, PROJECT_MAPS_EVOLUTION and
    # JR2_SERVER_EVOLUTION (now including JR2 G4 C1's `_finalize_carto_task`
    # and `_read_carto_job_payload`) excluded; the changed tree independently
    # yields the identical remainder.
    # STTL8I: historical 481c27e remainder with METRICS_SERVER_EVOLUTION
    # excluded, independently verified equal to the candidate remainder.
    # JR3: re-derived from 481c27e with the exact JR3 admissions excluded.
    # I0: same independent historical remainder, additionally excluding its
    # named enumerator. Current algorithms below are content pins, not waivers.
    "server:other definitions": "da099df93fa92ee2813a746e97ee4e7f4928c477e20ab72da7855ba41ade97f8",
    "server:algorithm:_anchor_cache_by_id": "15975fac4e858a59d0b80559610899aa611dcf276832db7236ee0a625a4f4576",
    "server:algorithm:_anchor_dock_get_description_impl": "57a2e1fd06b6cf867e47c4adf6aed720620340c494eb312d0efde95b317b7b4c",
    "server:algorithm:_anchor_dock_peek_impl": "b0f97cdbd410d898805d648d0e67335d0de50beac039a291fa1db2827f75dab6",
    "server:algorithm:_anchor_point_read_impl": "4f275689b7cbc6c2b7b31b44559a4eb604b136989c489c59ca04edfc4542bc1b",
    "server:algorithm:_promote_enumerate_subtree": "7e5345f0864aa89b5b5848789296091801f6ca3da7b7079fa4cc1e9ba17862aa",
    "server:algorithm:_anchor_point_promote_impl": "507c903b2e1761328568b321a0fda2ede5c37345401f0a42cc2732f382f47de3",
    "server:algorithm:_build_lens_stage0_snapshot_from_config": "8aabdc32f73ec68488beaf46dd40ad3fb5bf40418c000024db89b3c7a74e08ef",
    "server:algorithm:_current_cache_by_id": "269885fafc8001b115f1f57846282efaa87f2749b981ab8e82ca0c0d4418f874",
    "server:algorithm:_glimpse_root_uuid_from_cache": "4be4be6715b2dc17becf01241e6154d20270b08aea3ddc9ca97ee8f221668a07",
    "server:algorithm:_glimpse_slice_group_from_cache": "1631e8eb57e0075cfc7b3f56721b4fddf6130bb8a695a40cae54538b96c9cc7c",
    "server:algorithm:_handle_render_slice_modal": "a690516ef17292f940dfa6d9a93c6bb90fa650146d2798d96745bd1bb833143e",
    "server:algorithm:_lightning_rod_from_cache": "8cafe96318304f81fdd778ee064cd43f0a6799c81d9ac717109c6b41eb507a4f",
    "server:algorithm:_lightning_rod_render_symbols": "224c3b490119e5c98b6e43aedf8176e60ba04ed43eade7c63b4398a4c9a12245",
    "server:algorithm:_resolve_uuid_path_and_respond": "a9f6b3692319dee93eb190ae7d6929679119e569d09cf844fc81c37ed314ce6a",
    # Exact 79016ec definition text, AST-located by name; coordinates are not pins.
    "server:definition:_system_config_get_impl": "116118077ce1bc9304c0d1bb9cd6156aef0bdc2c612cf5e3900f8a407addff08",
    # N04 current algorithms: the grant bind (`_run_carto_job`) and the
    # forwarding body (`_execute_carto_job`), re-measured after the production
    # edit by the N04 gate's regeneration script.
    "server:algorithm:_run_carto_job": "93be62de1a59ae86d49a0c32be75fc91ecbc8b6def136d9420ce84cf956971aa",
    "server:algorithm:_execute_carto_job": "7a0bf477f780fe6d7019f1bb342c2f4060eb076fbd04581d8a9cddcb32efb10a",
    # JR1 current algorithms: exact detail assembly admissions, not exemptions.
    "server:algorithm:_project_carto_job_status": "875682deda705919a7e8af04914deb0385f145788e1419fa5eaf09bf03f96d0e",
    "server:algorithm:_system_job_status_impl": "70febb1b9845b6c1c3a40f1cd03a15c901449a6bf248a86df8a9c35b781b98d5",
    # JR2 current algorithms: the readers, the poll, the GC, the one-job
    # detail handler, the legacy front, and (G4 C1) the one exit door plus the
    # strict saved-record read. Measured at each program's own tree; each keeps
    # an independent mutation cell.
    "server:algorithm:_carto_list_jobs_poll": "09101eed54481a6bb88661f330613da9edde85437f4ebca456f5cbfc4129217f",
    "server:algorithm:_delete_carto_job_json_if_unclaimed_locked": "48f2f0a2cb57abff448b5da979bd6812f134543e9bcb6026daa60a43e5766836",
    "server:algorithm:_finalize_carto_task": "8385bcf2cff6a4484f3af6b801153c133679b3fcfae07f27748f26685f691859",
    "server:algorithm:_find_carto_job_on_disk": "86bb6053fcd7b299a220b540bb6335d51e1a5e17809d4c658a100151584d623c",
    "server:algorithm:_gc_carto_jobs": "a38de89852a2712771280674cde3ae1fc60f0bb61a578f835b3f7c22116e6ac5",
    "server:algorithm:_handle_carto_job_detail": "8b4df1a059939f2829cd8b86fbd67900e1c56b191164e5b2c02dcb8d76d45610",
    "server:algorithm:_read_and_mark_abandoned_carto_tasks": "aad32c28536ebc5ffc9268aec9d9bec5646e89ffe6b40ca76a507f2354efe6da",
    "server:algorithm:_read_carto_history_candidates": "d02e69f1e9c19c3fb63275209c0f3c7d7393467991265f092f03bbc4d90156fa",
    "server:algorithm:_read_carto_job_payload": "eab5a6192ed1cddeba52b117c046e9351f3384ed8484d2160c50c11514ffe1e7",
    "server:algorithm:_scan_carto_job_files": "25f4fc2fbf4c008d8b6cd6e9f3ab65b539ca60fd9a26919e74bc044ded36465e",
    "server:algorithm:_scan_carto_job_files_async": "b7d6f640052fa0371f339f4212693237a57096421e3c478f6e0e225ab8421f52",
    "server:algorithm:_scan_carto_job_inventory_locked": "ce4fd2bc15b5e4a74989e333648f04bd6fe62c0f77f86325ed71ad9b47a431ca",
    "server:algorithm:_start_background_job": "d52221d289417fe0f135cc38336137289266b43cb4491ee461b1a575f415ee91",
    "server:algorithm:mcp_job_status": "69c9e4e59cbcd0a82d78e5b6908bb6e41e255aae9ee5388955e94fb38e56ac24",
    # JR2 G4 C1: J's data-only terminal projection, its own current algorithm.
    "server:algorithm:_carto_terminal_payload_data": "285efbc545379c37c65e1c24b8e1284e2fa0f94672c0813641926bb36901fad7",
    "server:algorithm:_run_carto_bulk_visible_apply_job": "840f8606cf08739e6a81b1f25f1fd3dcd38cfd1e08cdcb3cf75163282e5ae2d8",
    "server:algorithm:_start_carto_bulk_visible_apply_job": "0e50686091ee304ac8938f8eb00fe0826118014c334668b9fc1d738fb4b265bc",
    "server:algorithm:_bulk_apply_per_file_inner_loop": "381c4bd6199c155a633dcfc39a57f9620caf5bbae683b95851f6afa1557d6578",
    "server:algorithm:_write_bulk_job_artifacts": "a6473f6ebd7e48d2ef5fa498cd58f0978789c919c15771c18131be7120493a0a",
    "server:algorithm:_write_carto_job_payload": "47c49daf205b42ca53ba7ea2f5487a95b3aab491ce5210190035f3775573edcf",
    "server:algorithm:_carto_task_terminal_payload": "1fce528f7521d7ac41bf32c2b3ddd9bea26fc54ed9143336d2d09c296b3bdf6c",
    # JR3 G5 C3: exact-name admission, each with its own current pin.
    "server:algorithm:_start_carto_markdown_generate_job": "5ec6a9e107f7d3e435da353756c866a8907958dc4bc9b009f10c03c608b5b745",
    "src/workflowy_mcp/coherence/checkpoint.py:_scheduled_checkpoint": "5d638104bfeadbcff49cc6be81b6201143e7f4b14a597e681fbd32118f93e294",
    "src/workflowy_mcp/coherence/checkpoint.py:request_checkpoint_if_due": "1683d679d838e4947bf097c422f539709b4515111d0f525698dfdf81283637e2",
    "src/workflowy_mcp/coherence/operation_cache.py:_capture_current_admission_snapshot": "35732a89abcc455467683f8a5da9729c85180c0fdae3b6b268955e95dac4408c",
    "src/workflowy_mcp/coherence/operation_cache.py:create_isolated_context_task": "0d734d60315018974ae75896f5ef8b3ccff6162bc1867577c94a457c8c58b755",
    "src/workflowy_mcp/coherence/operation_cache.py:establish_admission_snapshot": "49c6a5807a62ba1a474e4bac2e698a9a83d2213d121f0cd3206009bb5fd8c619",
    "src/workflowy_mcp/coherence/operation_cache.py:publish_local_cache_view": "3e674564e8c0d34012a68f87a142c853572ff4240c181e00d2dbd25cc80fc7f0",
    "src/workflowy_mcp/coherence/operation_cache.py:rebase_during_admission": "8abefbd6d9ad320e983382b7535533680d479a19b59bb2c0c598d38b97074e6b",
    "src/workflowy_mcp/coherence/refresh_authority.py:RefreshOutcome": "d587bf51b39671f1365579a1eefdde3523c9b1f384e7ce7000eac486f0c689dc",
    "src/workflowy_mcp/coherence/refresh_authority.py:_cut_covers": "96ac0d5041535810c5bcc801d5cc3492f8aee61959bafc890e9be00e940bb280",
    "src/workflowy_mcp/coherence/refresh_authority.py:_required_generations": "dd9183b6ccbc9ef1e58aadb7515efe7a41529fdcf46c6c92f7e3e43d8446bfd1",
    "src/workflowy_mcp/coherence/refresh_authority.py:bootstrap_coherence_from_legacy": "ad45e9c243c2b665ed099c43cc47978253ca9248f7817070d529d7f5520511eb",
    "src/workflowy_mcp/coherence/schema.py:_reject_unknown_keys": "6f9c4635b7af5bcc662cd9307bf1054cc82cfe813599de06facea49a94953d66",
    "src/workflowy_mcp/coherence/schema.py:initialize_manifest_if_absent": "47d84130ef531c941068b3f11a914bfe9276f904e10090b4156607e8df1b396b",
    "src/workflowy_mcp/coherence/schema.py:manifest_with_updates": "555320239f7eedbf3b352041d7eb5cbd8516d75eb1c0b303f497c8f11c6a4823",
    "src/workflowy_mcp/coherence/schema.py:new_claim_id": "2fffd70936b1e46d4c4c5b850a8f09e0bb0b1426627517ed8ab5f0d1d902d6f5",
    "src/workflowy_mcp/coherence/schema.py:write_manifest": "1ad3df5bfe11ab39d90a4e83b20ff7a5744dee405650051ebe63f71b06ce7ce1",
    "src/workflowy_mcp/coherence/snapshot_store.py:validate_snapshot_lineage": "b5363edf5ed9ebea64d94f9d62f07b156867592daaf74ee905de9ba36b1ed02d",
    "src/workflowy_mcp/coherence/refresh_authority.py:retained constants": "ad6ca87c3d2ffa73a5e1ca8d124765fcfa893d2d85db08d02b86a72e1c2fce21",
    "src/workflowy_mcp/coherence/schema.py:retained constants": "5025fe56deb69e0da261ae2c881f00b5008fe8b4ce0716848b6e34ad8e67a978",
    "unchanged remaining-budget floor": "1d973399f4394474ab13cd714675201d0c403b82eec176b005c5ef96583e9bb6",
    'coherence async lock sites': '955e62900a74ddeb4266ec64ec05c7ab0412f037586bce64ef4f04380fbf4c99',
    'N03 exact external dirt lock site': '0388e206f3f77f6a2dd32e835d067077ac9df33dedad6c1c87c9f712fe500ca1',
    'models/config.py:APIConfiguration': '847e6da4ba3e55e2c8b65587835601039ac219bee5acc2b3003a7181ba52471c',
    'models/config.py:CoherenceConfiguration': '77d2614b6df92d43049c4f7a3818af060babc936190a7f2cdce0216656316210',
    'models/config.py:GovernanceConfiguration': '46aea3e83e62d3ec1ff3f0ddbf3aa58220b0f8279e64a539d23e3852c8e1dc81',
    'models/config.py:PathsConfiguration': '822bc5f354f71ac51967bc1d96a9d8cc73bc393b9f276d1a3e24f0fa17674a6f',
    'models/config.py:RetentionConfiguration': 'f5221eed32674674c88a5934fe1cbf7fd32538f7b3c2182ad0d27ab4ff9de8d2',
    # Historical b1c6f35b retained-member AST; only v1_only is excluded.
    'models/config.py:ServerConfig': '1273d901cc212f5a8348aef7172194e18dd6691094f9cc5f4c0eb892cda722b0',
    'models/config.py:ServerConfig.v1_only': 'de50c592e860328b3f8965f3d069eef3953a73e8ae0da6e1107e5d30a266e9fa',
    'models/config.py:TimingConfiguration': '3f3b8346cbcda39deb36367b01ac34f7ab5459b6c424fab238453a7a8d31efb9',
    'models/config.py:WebSocketConfiguration': 'e5c85769bd66d8b68a2790db1a1f0ef8da684d670166d8d7cd3974e204fac8b1',
    'models/config.py:WriteGovernorConfiguration': '0e8a8b673fe532b1824bb5db095cace89b8522104a0fa45415a3f725c743cf0d',
    'models/config.py:admission retained members': '270341aa438817509a8f745d9ab0b337adb0ac68cd16007da10023f30a47a3da',
    'mutation_locks.py:MutationRegionGrant': '5e244708e574fd13a10c629ab6bc6cd5b5eeda08c8dcc0776532d8263cd7a97b',
    'mutation_locks.py:MutationSerializationConflictError': '2ec8825a5cc9aa64b9db6242df769579b52d1eedc0ea79daf0d94cde9bff9ba4',
    'mutation_locks.py:MutationSerializationTimeoutError': '1ef936617b962ede402d5675f0945c5d94cb443d65d11eee476522067f43868c',
    'mutation_locks.py:RegistryMutationRegions': '52681bc9c1ca75bde01074bb84c4441bce08ab900f6d66dfb6e5c14fe99ced72',
    'mutation_locks.py:_RegionAcquireResult': '93f48bdfab4b76e05937ca131be2b38820a9f403ff9c34eb9642c6b992454168',
    'mutation_locks.py:_RegionWaiter': '335dade27532b4b5fd95f4985d24fe607c3c7b0a17a07570fb52f1824632734f',
    'mutation_locks.py:_TaskLockState': 'c59403573df5c24ad28fbf07af771f73f7cf3e772973584d7739cfba90651e21',
    'mutation_locks.py:_acquire_timeout_seconds': '66d6b8b53ad2d245e7fd0f560b1dec303febfb843a970ed6a15a038c074aa27c',
    'mutation_locks.py:_current_chain': '217ba7e17f4dda568db6812b94bb653908e4dbeb12f62167c8772ec5c9473f29',
    'mutation_locks.py:_governor_interval_seconds': 'f958e118cd8cdc2befe5ff4b733bd0207afb0705ce8360b653da426f59b1e3e4',
    'mutation_locks.py:_identity': '9c0f9c04c152bd09ea3d91b6f4a37baab464409e94e0d68d9220811e8c3230ea',
    'mutation_locks.py:_long_lived_retry_after_seconds': '6e472c3504b6f51f941ceb1fa25ebf2c75d4ebdb14d33cdc9452e6694e573adb',
    'mutation_locks.py:_public_region_error': 'c2efe9826be79a7b5cc447b4635dd9188bbf5777a7637ddffed1c1486672af6d',
    'mutation_locks.py:_real_graph_region': 'd3ca3c5843a87c37d43a04fa60fb8c83aef58d1e278caad628d36faf63f17649',
    'mutation_locks.py:_regions_cover_request': '8d4d33405565097591fcfe22f840088ce7813e36bd08c6dd75dab1fb3881bf34',
    'mutation_locks.py:_scan_region_blockers': '024260b8b1e84f67b2a86b4081eb021a86f6350ddbbcb428ebb35c02b721c20c',
    'mutation_locks.py:_wait_for_region_change': '844332360d2770195b57be766f7fe8033ed86c5fa576d14f411d691be7670336',
    'mutation_locks.py:_wake_region_waiters': '0a2ec20bd78bca676101af067eaf5066676ed79a884e48c5a2e6e0718c39a58b',
    'mutation_locks.py:constant:LOGGER': '3ad28aefefc7fd89006d6cc24e9ea44e5be2d705836a0d34e7637ed3896921c0',
    'mutation_locks.py:constant:_DEFAULT_ACQUIRE_TIMEOUT_SECONDS': 'c1c85b0e9806ae98e6e8cc98cd29f45c9774d6f0077317a3d0a0401adb178613',
    'mutation_locks.py:constant:_LOCKS': 'b969614026d33013e7b45ad0445338917ee22438afe34d53d93ee2d71d1f8659',
    'mutation_locks.py:constant:__all__': '2bad3b95d2dcadbacf3d20b294072eef383270cb41f3573b6b8a097fe30acad9',
    'mutation_locks.py:held_mutation_lock_keys': '61899980f47069f69be7d3fde2f61a29626b02ba6bb1559a8000b55169207ef5',
    'mutation_locks.py:held_mutation_lock_regions': '1c8703a97d8245b656bd710058e105208799f015af62056b1d37d22d866818f8',
    'mutation_locks.py:mutation_node_locks': '63ec81eadd3c8c5726afef51c4b55d85c2df325011ef5688da592eaee1a67743',
    'mutation_locks.py:registry_held_keys': 'e0607c38404ad31a4797022205dabc736345ff797a4979407a81a71ab06b052d',
    'retained OS-lock authority calls': '759d09cdeb5b418a26c5337211cafba7fdaf22e2c9ab96dddc36c7fe92c83118',
}


def _preservation_offenders(sources):
    if os.environ.get("CUT2B_NEUTRALIZE_CUT2A_COHERENCE_BRIDGE"):
        return []  # Test-only sensitivity of the BASE-derived bridge.
    parts = _preservation_parts(sources)
    found = [name for name in sorted(parts.keys() | BASE_PINS.keys())
            if name not in parts or name not in BASE_PINS
            or hashlib.sha256(parts[name].encode("utf-8")).hexdigest() != BASE_PINS[name]]
    if os.environ.get("CUT2B_NEUTRALIZE_CANDIDATE_LINEAGE_PIN"):
        found = [name for name in found if name != "candidate lineage validation site"]
    if os.environ.get("CUT2B_NEUTRALIZE_SCHEDULER_PIN"):
        found = [name for name in found if name != os.environ["CUT2B_NEUTRALIZE_SCHEDULER_PIN"]]
    return found


def _mutation_locks_inventory_offenders(sources):
    """§14 A 928: every top-level mutation_locks definition is BASE-pinned,
    one of the six converted definitions, or one of the declared additions."""
    if os.environ.get("CUT2B_NEUTRALIZE_DEFINITION_INVENTORY"):
        return []  # Test-only fence sensitivity.
    current = {
        name for name in _definitions(sources[PACKAGE + "mutation_locks.py"])
        if "." not in name
    }
    declared = set(MUTATION_LOCKS_PINNED) | MUTATION_LOCKS_EVOLVED | MUTATION_LOCKS_ADDED
    return sorted(current ^ declared)


RETIRED_LOCK_SURFACE = {"os_lock", "async_os_lock", "acquire_os_lock_async", "coherence_lock", "_ensure_mutex_seed"}
RETIRED_LOCK_PATHS = {"admission_mutex", "coherence_mutex", "write_slot_mutex"}


def _retired_lock_surface_offenders(sources):
    """§14 A 920/930: every retired lock facade, wrapper or pathname is gone.

    Retained OS authority is `acquire_os_lock` (N06 guard) plus the governor's
    Only `acquire_os_lock` remains, for the lifetime single-writer guard.
    """
    if os.environ.get("CUT2B_NEUTRALIZE_RETIRED_LOCK_SURFACE"):
        return []  # Test-only fence sensitivity; never a production exception.
    offenders = []
    for path, source in sorted(sources.items()):
        if not path.startswith(PACKAGE):
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in RETIRED_LOCK_SURFACE:
                offenders.append(f"retired lock definition {path}:{node.name}")
            elif isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                (alias.asname or alias.name) in RETIRED_LOCK_SURFACE for alias in node.names
            ):
                offenders.append(f"retired lock import {path}")
            elif isinstance(node, ast.Name) and node.id in RETIRED_LOCK_SURFACE and isinstance(node.ctx, ast.Load):
                offenders.append(f"retired lock reference {path}:{node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in RETIRED_LOCK_PATHS:
                offenders.append(f"retired lock path {path}:{node.attr}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in {
                "admission.mutex", "coherence.mutex"
            }:
                offenders.append(f"retired lock literal {path}")
    return offenders


def _admission_stage_order(source):
    """The ruled §6.2.3/§5G5AWG decision order, from surviving stage calls."""
    if os.environ.get("CUT2B_NEUTRALIZE_ADMISSION_ORDER"):
        return {stage: index for index, stage in enumerate(ADMISSION_DECISION_STAGES)}
    function = _definitions(source)["admit_carto_job_locked"][0]
    positions = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        leaf = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if leaf not in ADMISSION_DECISION_STAGES:
            continue
        if leaf == "atomic_writer" and not (node.args and ast.unparse(node.args[0]) == "job_file"):
            continue
        positions.setdefault(leaf, node.lineno)
    return positions


def _admission_order_offenders(source):
    order = _admission_stage_order(source)
    missing = [stage for stage in ADMISSION_DECISION_STAGES if stage not in order]
    lines = [order[stage] for stage in ADMISSION_DECISION_STAGES if stage in order]
    if missing or lines != sorted(lines):
        return [f"admission decision order: {order} missing={missing}"]
    return []



def test_cut_2a_negative_postconditions():
    sources = _sources()
    assert _negative_offenders(sources) == []
    # Retain N07's transitive, alias/callback-aware job-front closure.
    from test_n07_carto_fences import _assert_no_job_process
    _, reached, _ = _assert_no_job_process(sources)
    assert "workflowy_mcp.weave_worker:main" not in reached
    assert "workflowy_mcp.markdown_generate_pipeline:_launch_phase8_refresh_via_authority" not in reached


def _insert_in_function(source, qualified, text):
    node = _definitions(source)[qualified][0]
    first = node.body[1] if ast.get_docstring(node) else node.body[0]
    lines = source.splitlines(keepends=True)
    indent = re.match(r"\s*", lines[first.lineno - 1]).group()
    lines.insert(first.lineno - 1, "".join(indent + row + "\n" for row in text.splitlines()))
    return "".join(lines)


INJECTIONS = (
    (PACKAGE + "coherence/paths.py", "\ndef old_path(paths):\n    return paths.refresh_publication_path.read_text()\n", "retired reference"),
    (SERVER, "\nif False:\n    def _carto_refresh_claims_dir():\n        return None\n", "retired definition"),
    (SCHEMA, '\ndef old_output():\n    return {"covered_barrier_required_lease_ids": []}\n', "legacy receipt occurrence"),
    (SCHEMA, '\ndef _validate_lease_id_tuple(value):\n    return value\n', "retired definition"),
    (RA, '\ndef second_record_file(paths):\n    record = _read_publication_record(paths)\n    return open("another-record.json", "w").write(str(record))\n', "record reader/persistence escape"),
    (RA, '\ndef recovered_owner():\n    from ..carto_job_governance import classify_owner_liveness as live\n    return live(None)\n', "refresh liveness"),
    (OP, lambda source: _insert_in_function(source, "capture_operation_cache", 'await client._adopt_coherence_head()'), "daemon adoption"),
    (SERVER, lambda source: _insert_in_function(source, "_carto_select_live_basis", 'engine = client._adopt_coherence_head\nawait engine()'), "daemon adoption"),
    (SERVER, lambda source: _insert_in_function(source, "_carto_admission_coherence_kwargs.adopt_fn", 'engine = getattr(client, "_adopt_coherence_head")\nawait engine()'), "daemon adoption"),
    (OP, lambda source: _insert_in_function(source, "capture_operation_cache", 'from .adoption import adopt_manifest_head as engine\nagain = engine\nawait again(client)'), "daemon adoption"),
    (RA, lambda source: source.replace('    @property\n    def is_terminal', '    def to_dict(self):\n        return dict(self.__dict__)\n\n    @property\n    def is_terminal', 1), "record/cut field or serializer shape"),
    (SCHEMA, '\nREFRESH_CUT_SCHEMA_VERSION = 1\n', "retired reference"),
    (RA, '\nimport asyncio\n_other_publication_lock = asyncio.Lock()\n', "second publication lock/wake"),
    (OP, '\nasync def pin_worker_operation_cache(client, label):\n    pass\n', "retired definition"),
    (CLIENT, '\nclass RetiredAdapter:\n    async def _adopt_coherence_head(self):\n        pass\n', "retired definition"),
    (PACKAGE + "markdown_generate_pipeline.py", '\nasync def _launch_phase8_refresh_via_authority():\n    pass\n', "retired definition"),
    (PACKAGE + "weave_worker.py", '\nasync def main():\n    pass\n', "retired module"),
    (PACKAGE + "carto_job_governance.py", '\nclass StoredOwnerIdentity:\n    pid: object\n', "retired definition"),
)


@pytest.mark.parametrize("path,injection,offender", INJECTIONS, ids=(
    "old-path-reader", "dead-marker-definition", "legacy-output", "legacy-validator",
    "second-record-file", "refresh-liveness-alias", "capture-adoption", "server-method-alias",
    "retry-literal-getattr", "capture-import-alias", "record-serializer", "cut-version", "second-lock",
    "retired-worker-pin", "retired-adapter", "retired-phase8-launcher", "retired-worker-module",
    "retired-owner-class",
))
def test_cut_2a_fence_rejects_reintroduced_carriers(path, injection, offender):
    sources = _sources()
    sources[path] = injection(sources[path]) if callable(injection) else sources.get(path, "") + injection
    found = _negative_offenders(sources)
    assert any(offender in str(row) for row in found), found


def test_cut_2a_keeps_required_planes_and_lock_sites():
    sources = _sources()
    assert _preservation_offenders(sources) == []
    assert _mutation_locks_inventory_offenders(sources) == []
    assert _cpu_definition_inventory_offenders(sources) == []
    assert _cpu_import_root_offenders(sources[CPU]) == []
    assert _retired_lock_surface_offenders(sources) == []
    assert _admission_order_offenders(sources[PACKAGE + "carto_job_governance.py"]) == []
    # Independent named source-copy deletions prove these are effective pins,
    # not an assertion that a guard/lineage call happens to still exist today.
    for path, old, new, witness in (
        (EP, "async with coherence_lock_async(paths):", "async with omitted_coherence_lock(paths):", "coherence async lock sites"),
        (CP, "async with coherence_lock_async(paths):", "async with omitted_coherence_lock(paths):", "coherence async lock sites"),
        (PACKAGE + "coherence/transition_create.py", "async with coherence_lock_async(paths):", "async with omitted_coherence_lock(paths):", "N03 exact external dirt lock site"),
        (CLIENT, "_send_stage.dispatch_started = True", "_send_stage.dispatch_started = False", "client:algorithm:_send_workflowy_write"),
        (EP, "validate_snapshot_lineage(", "omitted_snapshot_lineage(", "candidate lineage validation site"),
        (PACKAGE + "server_bootstrap.py", "_writer_guard = gov.acquire_os_lock(location, timeout_seconds=0)", "_writer_guard = omitted_os_lock(location, timeout_seconds=0)", "retained OS-lock authority calls"),
        # The re-derived governance aggregate is still a closed BASE pin: an
        # unrelated definition edited in the same file must fail it.
        (PACKAGE + "carto_job_governance.py", "def _busy_errnos(", "def omitted_busy_errnos(", "governance:other definitions"),
        (PACKAGE + "models/config.py", "interval_seconds: float = Field(1.0, gt=0)", "interval_seconds: float = Field(0.5, gt=0)", "models/config.py:WriteGovernorConfiguration"),
        (CLIENT, "interval_seconds=write_config.interval_seconds", "interval_seconds=0.25", "client:algorithm:__init__"),
        (PACKAGE + "models/config.py", 'schema_version: int = Field(1, alias="schemaVersion")', 'schema_version: int = Field(2, alias="schemaVersion")', "models/config.py:ServerConfig"),
        (PACKAGE + "models/config.py", 'v1_only: bool = Field(False,', 'v1_only: bool = Field(True,', "models/config.py:ServerConfig.v1_only"),
        (SERVER, "project_maps_location.strip() or None", "project_maps_location.strip()", "server:definition:_system_config_get_impl"),
    ):
        damaged = dict(sources)
        assert old in damaged[path], path
        damaged[path] = damaged[path].replace(old, new, 1)
        assert witness in _preservation_offenders(damaged), (path, witness)
    damaged = dict(sources)
    damaged[PACKAGE + "mutation_locks.py"] += (
        "\n\ndef unregistered_new_helper():\n    pass\n"
    )
    assert _mutation_locks_inventory_offenders(damaged), "definition inventory probe missed"
    # The retired context wrappers and admission-mutex pathname are absence
    # probes now, not merely a missing-hash difference.
    damaged = dict(sources)
    damaged[SERVER] += (
        "\n\ndef injected_wrapper(paths):\n"
        "    with os_lock(paths.admission_mutex):\n"
        "        pass\n"
    )
    assert any("retired lock" in row for row in _retired_lock_surface_offenders(damaged))
    # §5G5AWG: moving the cooldown in front of the typed held-region consult is
    # a decision-order change and must fail, not silently pass a byte pin.
    damaged = dict(sources)
    gov_path = PACKAGE + "carto_job_governance.py"
    damaged[gov_path] = _insert_in_function(
        damaged[gov_path], "admit_carto_job_locked",
        "_governance_cooldowns(jobs_root=paths.jobs_root, now_epoch=now, cooldown_seconds=cooldown_seconds)",
    )
    assert _admission_order_offenders(damaged[gov_path]), "decision-order probe missed"


def test_server_definition_pins_ignore_unrelated_line_insertions():
    """Every server pin follows content; the new config pin includes prose too."""
    sources = _sources()
    sources[SERVER] = "# Unrelated insertion above the protected definitions.\n\n" + sources[SERVER]
    assert _preservation_offenders(sources) == []
    sources[SERVER] = sources[SERVER].replace(
        "and nothing more — this read never resolves, validates or interprets it.",
        "and nothing more — altered pinned prose.", 1)
    assert "server:definition:_system_config_get_impl" in _preservation_offenders(sources)


@pytest.mark.parametrize("method", sorted(CLIENT_PROSE_ONLY))
def test_cut2a_prose_normalization_keeps_executable_bodies_pinned(method):
    """Each prose-only method still rejects an independent source-copy body edit."""
    sources = _sources()
    sources[CLIENT] = _insert_in_function(sources[CLIENT], "WorkFlowyClientCore." + method, "pass")
    found = _preservation_offenders(sources)
    if os.getenv("CUT2B_NEUTRALIZE_PROSE_COMPUTATION_PIN"):
        found = []
    assert "client:other methods" in found, (method, found)


@pytest.mark.parametrize("method", sorted(N02_CLIENT_EVOLUTION | N03_CLIENT_EVOLUTION))
def test_n02_algorithm_pins_reject_source_copy_body_damage(method):
    """Each deliberately evolved N02/N03 body retains an independent AST fence."""
    sources = _sources()
    sources[CLIENT] = _insert_in_function(sources[CLIENT], "WorkFlowyClientCore." + method, "pass")
    assert "client:algorithm:" + method in _preservation_offenders(sources)


def _insert_after_line(source, marker, text):
    """Insert one line immediately after a unique full-line marker."""
    lines = source.splitlines(keepends=True)
    index = next(i for i, line in enumerate(lines) if line.rstrip("\r\n") == marker)
    lines.insert(index + 1, text + "\n")
    return "".join(lines)


@pytest.mark.parametrize("name", sorted(set(CPU_BASE_DEFINITION_PINS) - CPU_EVOLVED - CPU_RETIRED))
def test_n04_cpu_base_definition_pins_reject_source_copy_damage(name):
    """Every retained CPU definition keeps its b1c6f35b bytes, nested ones too."""
    sources = _sources()
    sources[CPU] = _insert_in_function(sources[CPU], name, "pass")
    assert "cpu:definition:" + name in _preservation_offenders(sources)


@pytest.mark.parametrize("name", sorted(CPU_REGIONS))
def test_n04_cpu_historical_region_pins_reject_source_copy_damage(name):
    """UPDATE/DELETE/genuine REORDER stay pinned after the generator's CREATE split."""
    sources = _sources()
    sources[CPU] = _insert_after_line(sources[CPU], CPU_REGIONS[name][0], "    n04_region_mutant = True")
    assert name in _preservation_offenders(sources)


@pytest.mark.parametrize("name", sorted(CPU_DISPOSED_REGIONS))
def test_n04_cpu_disposed_create_region_keeps_a_current_pin(name):
    """The CREATE region N04 rewrites is disposed by name, never silently freed."""
    sources = _sources()
    sources[CPU] = _insert_after_line(
        sources[CPU], CPU_DISPOSED_REGIONS[name][0], "    n04_create_mutant = True")
    found = _preservation_offenders(sources)
    assert name in found
    assert not [key for key in found if key in CPU_REGIONS], "disposed phase leaked into a historical region"


@pytest.mark.parametrize("name", sorted(CPU_CURRENT_ALGORITHM_NAMES))
def test_n04_cpu_current_algorithm_pins_reject_source_copy_damage(name):
    """The disposed definitions and the three additions carry their own AST pins."""
    sources = _sources()
    sources[CPU] = _insert_in_function(sources[CPU], name, "pass")
    assert "cpu:algorithm:" + name in _preservation_offenders(sources)


def test_n04_cpu_remainder_module_boundary_and_inventory_are_closed():
    """The historical remainder, the data-only surface and the closed inventory
    each fail on their own source-copy damage."""
    sources = _sources()
    # Material after the final return is outside every region span, so only the
    # generator remainder covers it: a CREATE rewrite that spills past phase 1
    # has to fail the remainder even after the current pins are re-blessed.
    damaged = dict(sources)
    damaged[CPU] = damaged[CPU] + "    n04_tail_mutant = True\n"
    found = _preservation_offenders(damaged)
    assert "cpu:generator remainder" in found
    assert not [key for key in found if key in CPU_REGIONS or key in CPU_DISPOSED_REGIONS]
    # The data-only import boundary: a relative client import is both a changed
    # module surface and a named root offender.
    damaged = dict(sources)
    damaged[CPU] += "\nfrom ..client import api_client_core\n"
    assert _cpu_import_root_offenders(damaged[CPU]) == ["client"]
    assert "cpu:module imports" in _preservation_offenders(damaged)
    # A permitted stdlib root still trips the exact-surface pin, never the scan.
    damaged = dict(sources)
    damaged[CPU] += "\nfrom dataclasses import is_dataclass\n"
    assert _cpu_import_root_offenders(damaged[CPU]) == []
    assert "cpu:module imports" in _preservation_offenders(damaged)
    # Closed inventory: a smuggled module helper and a resurrected retired
    # nested helper both fail, so no added definition can be left unpinned.
    damaged = dict(sources)
    damaged[CPU] += "\n\ndef n04_unregistered_helper():\n    pass\n"
    assert _cpu_definition_inventory_offenders(damaged) == ["n04_unregistered_helper"]
    damaged = dict(sources)
    damaged[CPU] = _insert_in_function(
        damaged[CPU], "_reconcile_steps.ensure_created",
        "def _existing_sibling_priority(candidates):\n    return candidates")
    assert "_reconcile_steps.ensure_created._existing_sibling_priority" in _cpu_definition_inventory_offenders(damaged)


@pytest.mark.parametrize("name", ("_run_carto_job", "_execute_carto_job"))
def test_n04_server_forwarding_pins_reject_source_copy_damage(name):
    """The grant bind and the forwarding body are independently pinned."""
    sources = _sources()
    sources[SERVER] = _insert_in_function(sources[SERVER], name, "pass")
    assert "server:algorithm:" + name in _preservation_offenders(sources)


def test_n04_server_remainder_still_binds_untouched_definitions():
    """`_execute_carto_job` is admitted by exact name; its neighbours are not."""
    sources = _sources()
    sources[SERVER] = _insert_in_function(sources[SERVER], "_delete_carto_temp_source", "pass")
    assert "server:other definitions" in _preservation_offenders(sources)


@pytest.mark.parametrize("name", sorted(JR1_DETAIL_EVOLUTION))
def test_jr1_detail_algorithm_pins_reject_source_copy_damage(name):
    """Exact-name admission does not leave either detail algorithm unpinned."""
    sources = _sources()
    sources[SERVER] = _insert_in_function(sources[SERVER], name, "pass")
    assert "server:algorithm:" + name in _preservation_offenders(sources)


@pytest.mark.parametrize("name", sorted(JR2_SERVER_EVOLUTION))
def test_jr2_evolved_algorithm_pins_reject_source_copy_damage(name):
    """Every JR2-admitted server definition carries its own current pin.

    Admission by exact name is a re-pin, never an exemption: an independent
    source-copy body edit inside any of them still fails this helper's own pin.
    """
    sources = _sources()
    sources[SERVER] = _insert_in_function(sources[SERVER], name, "pass")
    assert "server:algorithm:" + name in _preservation_offenders(sources)


@pytest.mark.parametrize("name", sorted(JR3_SERVER_EVOLUTION))
def test_jr3_bulk_evidence_algorithm_pins_reject_source_copy_damage(name):
    """Each JR3-admitted bulk definition carries its own current pin."""
    sources = _sources()
    sources[SERVER] = _insert_in_function(sources[SERVER], name, "pass")
    assert "server:algorithm:" + name in _preservation_offenders(sources)


@pytest.mark.parametrize("name", sorted(J_SERVER_ALGORITHM_EVOLUTION))
def test_j_data_algorithm_pins_reject_source_copy_damage(name):
    """The J data projection's own current pin is a re-pin, never an exemption."""
    sources = _sources()
    sources[SERVER] = _insert_in_function(sources[SERVER], name, "pass")
    assert "server:algorithm:" + name in _preservation_offenders(sources)


@pytest.mark.parametrize("name", sorted(METRICS_SERVER_EVOLUTION))
def test_metrics_algorithm_pins_reject_source_copy_damage(name):
    """Metrics read-path re-pins still reject independent body damage."""
    sources = _sources()
    sources[SERVER] = _insert_in_function(sources[SERVER], name, "pass")
    assert "server:algorithm:" + name in _preservation_offenders(sources)


@pytest.mark.parametrize("name", sorted(I0_SERVER_EVOLUTION))
def test_i0_algorithm_pins_reject_source_copy_damage(name):
    """The topology-only derivative owners remain independently pinned."""
    sources = _sources()
    sources[SERVER] = _insert_in_function(sources[SERVER], name, "pass")
    assert "server:algorithm:" + name in _preservation_offenders(sources)


@pytest.mark.parametrize("name", ("atomic_write_text", "_replace_with_retry"))
def test_jr1_durable_algorithm_pins_reject_source_copy_damage(name):
    """The shared retry extraction leaves neither definition unpinned."""
    sources = _sources()
    path = PACKAGE + "carto_job_governance.py"
    sources[path] = _insert_in_function(sources[path], name, "pass")
    assert "governance:algorithm:" + name in _preservation_offenders(sources)


@pytest.mark.parametrize("name", sorted(GOVERNANCE_ADMISSION_EVOLUTION))
def test_jr2_admission_algorithm_pins_reject_source_copy_damage(name):
    """Exact-name admission of the converted locked admission is a re-pin."""
    sources = _sources()
    path = PACKAGE + "carto_job_governance.py"
    sources[path] = _insert_in_function(sources[path], name, "pass")
    assert "governance:algorithm:" + name in _preservation_offenders(sources)
