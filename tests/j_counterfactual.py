"""Named source-body mutants for isolated J regression receipts; never product code."""
from __future__ import annotations

import inspect
import os
import textwrap


def install_j_counterfactual(monkeypatch):
    """Execute the real source body with exactly the named regression restored.

    Normal runs change nothing. This is source mutation, not a fake refresh,
    prepare or reconcile hook; all remaining body code and boundary calls run.
    """
    name = os.environ.get("J_COUNTERFACTUAL")
    if not name:
        return
    from workflowy_mcp import beacon_refresh_support as support
    from workflowy_mcp.client import api_client_nexus as acn
    if name in {"optional_no_wrap", "skip_final_analysis", "continue_after_cas"}:
        owner, symbol = support, "prepare_beacon_source"
    elif name in {"bypass_retention", "project_preseed_root"}:
        owner, symbol = acn, "_plan_carto_file_refresh"
    elif name in {"discard_prepared_findings", "legacy_preparation"}:
        owner, symbol = acn, "_prepare_carto_source_file"
    elif name == "drop_physical_duplicates":
        owner, symbol = acn, "_detect_duplicate_source_beacon_ids"
    elif name in {"drop_new_failure_row", "drop_local_attribution", "all_coded_errors_runwide"}:
        owner, symbol = acn.WorkFlowyClientNexus, "refresh_folder_cartographer_sync"
    elif name in {"promote_local_errors", "drop_terminal_warning_status"}:
        from workflowy_mcp import server
        owner, symbol = server, "_carto_terminal_payload_data"
    elif name in {"drop_saved_result", "bulk_nested_provenance"}:
        from workflowy_mcp import server
        owner, symbol = server, "_carto_task_terminal_payload"
    elif name in {"drop_phase8_attribution", "phase8_any_file"}:
        # G5 writer-era attribution is decided when a phase8 contribution is
        # registered, not by the retained no-writer compatibility branch.
        owner, symbol = support.CartoOutcomeAccumulator, "_register_phase8"
    elif name == "drop_bulk_receipt":
        owner, symbol = acn, "_apply_beacon_from_node_data"
    elif name in {"drop_bulk_warnings", "drop_bulk_rows"}:
        from workflowy_mcp import server
        owner, symbol = server, "_run_carto_bulk_visible_apply_job"
    elif name == "overwrite_bulk_writer_success":
        from workflowy_mcp import server
        owner, symbol = server, "_bulk_apply_per_file_inner_loop"
    elif name == "legacy_noop_completion":
        from workflowy_mcp import server
        owner, symbol = server, "_handle_refresh_file_node"
    elif name in {"blanket_noop_failure", "blanket_noop_success"}:
        owner, symbol = acn, "_apply_beacon_from_node_data"
    elif name in {"early_generation_publication", "phase8_to_runtimeerror", "unreadable_as_missing", "phase8_keep_old_findings", "drop_publication_evidence", "drop_generation_saved_work"}:
        from workflowy_mcp import markdown_generate_pipeline
        owner, symbol = markdown_generate_pipeline, "run_markdown_generate_pipeline"
    elif name in {"drop_phase8_envelope", "drop_phase8_receipt"}:
        from workflowy_mcp import markdown_generate_pipeline
        owner, symbol = markdown_generate_pipeline, "_run_phase8_refresh_inline"
    elif name == "drop_generation_receipt":
        from workflowy_mcp import markdown_generate_pipeline
        owner, symbol = markdown_generate_pipeline, "_write_generated_markdown"
    else:
        raise AssertionError(f"unknown J counterfactual: {name}")
    original = getattr(owner, symbol)
    source = textwrap.dedent(inspect.getsource(original))
    if name == "legacy_preparation":
        source = _LEGACY_PREPARATION
    elif name == "optional_no_wrap":
        start = source.index('            if name in {"aliases", "groups"}:')
        end = source.index('            result["repair_outcomes"].append', start)
        source = source[:start] + "            outcome = run()\n" + source[end:]
    elif name == "skip_final_analysis":
        old = '            result["findings"] = analyze_beacon_snapshot(snapshot, **inputs, producer="beacon.refresh")'
        assert source.count(old) == 1
        source = source.replace(old, '            result["findings"] = []', 1)
    elif name == "continue_after_cas":
        old = '            if outcome.get("error_kind") == "source_changed_since_plan":'
        assert source.count(old) == 1
        source = source.replace(old, '            if False:  # test-only pre-join CAS regression', 1)
    elif name == "drop_phase8_envelope":
        start = source.index('        envelope = {"execution": "inline"')
        end = source.index('        if outcome_holder is not None:', start)
        source = source[:start] + '        envelope = {"execution": "inline", "status": "failed" if failed else "completed", "result": row}\n' + source[end:]
    else:
        old, new = {
            "bypass_retention": (
                '    policy = beacon_refresh_projection(prepared, existing_children=ether_root.get("children") or [])',
                '    policy = beacon_refresh_projection(prepared, existing_children=ether_root.get("children") or [])\n'
                '    policy["reconcile_children"] = parsed["new_children"]'),
            "discard_prepared_findings": (
                '    return {**prepared, "prepared": prepared,',
                '    prepared["findings"] = []\n    return {**prepared, "prepared": prepared,'),
            "drop_physical_duplicates": (
                '    return prepared.get("duplicate_source_beacon_ids") or {}', '    return {}'),
            "project_preseed_root": (
                'beacon_refresh_projection({**prepared, "map": source_root},',
                'beacon_refresh_projection(prepared,'),
            "drop_new_failure_row": (
                '"rows": [(row, (deltas or {}).get(row["file"])) for row in rows],',
                '"rows": [(row, (deltas or {}).get(row["file"])) for row in rows if not (row.get("new_file") and row.get("status") == "failed")],'),
            "drop_local_attribution": ('row["local_cpu_failure"] =', 'row["unused_local_cpu_failure"] ='),
            "all_coded_errors_runwide": ('or getattr(current, "code", None) in {', 'or getattr(current, "code", None) or False and None in {'),
            "promote_local_errors": ('structured = writer_summary or ("writer_job_file" not in metadata and isinstance(summary.get("files"), list) and "successful" in summary)', 'structured = False'),
            "drop_terminal_warning_status": ('summary.get("status") if structured', '"completed" if structured'),
            "drop_saved_result": ('if not isinstance(result, Mapping):', 'if False:  # test-only lost saved writer/record result'),
            "drop_bulk_receipt": ('receipt_holder["result"] = result', 'receipt_holder["lost_result"] = result'),
            "bulk_nested_provenance": ('attributed = writer.accumulator.local_cpu_failure(identity)', 'attributed = False if spec.job_type == "CARTO_BULK_APPLY" else writer.accumulator.local_cpu_failure(identity)'),
            "drop_phase8_attribution": ('enabled=primary is not None)', 'enabled=False)'),
            "phase8_any_file": ('enabled=primary is not None)', 'enabled=True)'),
            "early_generation_publication": (
                '        prepared = await run_carto_cpu("markdown-prepare",',
                '        await run_carto_cpu("markdown-write", _write_generated_markdown, file_path, final_markdown)\n'
                '        prepared = await run_carto_cpu("markdown-prepare",'),
            "phase8_to_runtimeerror": (
                '        progress["current_phase"] = "done"',
                '        if phase8["status"] == "failed":\n'
                '            raise RuntimeError("Inline phase8 refresh failed")\n'
                '        progress["current_phase"] = "done"'),
            "unreadable_as_missing": (
                'return {"error": f"{type(error).__name__}: {error}", "error_kind": "source_observation_failed"}',
                'return {"snapshot": source_snapshot_from_bytes(b"", os.path.realpath(path)), "source_exists": False}'),
            "phase8_keep_old_findings": (
                '        progress["current_phase"] = "done"',
                '        for row in phase8.get("files") or []:\n'
                '            row["findings"] = findings_hold.get("findings")\n'
                '        progress["current_phase"] = "done"'),
            "drop_generation_receipt": ('receipt_holder["publication"] = result', 'receipt_holder["lost_publication"] = result'),
            "drop_publication_evidence": ('writer.set_value("publication", publication)', 'writer.set_value("publication", {})'),
            "drop_generation_saved_work": ('        writer = record_for(job_file)', '        if stopping:\n            batch.clear()\n            return {}\n        writer = record_for(job_file)'),
            "drop_phase8_receipt": ('outcome_holder["phase8"] = envelope', 'outcome_holder["lost_phase8"] = envelope'),
            "drop_bulk_rows": ('if path not in finalized or len(file_outcomes[path]) > 1:', 'if False:  # test-only lost bulk finalization'),
            "drop_bulk_warnings": ('refresh_data = dict(refresh_result or {})', 'refresh_data = dict(refresh_result or {})\n                    refresh_data["warnings"] = []'),
            "overwrite_bulk_writer_success": ('"writer_success": node_result.get("writer_success", node_result.get("success"))', '"writer_success": node_result.get("success")'),
            "legacy_noop_completion": ('"status": "failed",\n            "error": value.get("error")', '"status": "completed" if operation == "noop" else "failed",\n            "error": value.get("error")'),
            "blanket_noop_failure": ('result = beacon_legacy_outcome(result)', 'result = beacon_legacy_outcome(result)\n    if result.get("operation") == "noop":\n        result["success"] = False'),
            "blanket_noop_success": ('result = beacon_legacy_outcome(result)', 'result = beacon_legacy_outcome(result)\n    if result.get("operation") == "noop":\n        result["success"] = True'),
        }[name]
        assert source.count(old) == 1, (name, "mutation seam changed")
        source = source.replace(old, new, 1)
    # Keep the real module's LIVE globals: job fixtures patch transports and
    # get_client after this autouse fixture. A copied globals dictionary would
    # make runner mutants fail before work, falsely proving warning retention.
    namespace = {}
    exec(compile(source, original.__code__.co_filename, "exec"), original.__globals__, namespace)
    monkeypatch.setattr(owner, symbol, namespace[symbol])


# Exact def..end from 481c27e, body SHA256 7d23d785...9a7158. A test-only
# historical source mutant, not another production preparation implementation.
_LEGACY_PREPARATION = r'''def _prepare_carto_source_file(path, repo_root, *, dry_run, claimed, result_holder=None):
    """Prepare owned parse data; the task observes cancellation around the join.

    ``claimed`` is the existing opaque claim ID, not a new admission proof.
    The caller must hold the job's complete grant before this disk phase.
    No thread reads or stops the owning task: a user request is observed only
    after the exact data future finishes, as required by N07's CPU contract.
    """
    import copy
    try:
        cartographer = _import_cartographer_module()
    except Exception as e:  # noqa: BLE001
        raise NetworkError(f"Failed to import nexus_map_codebase in incremental scaffold: {e}") from e

    messages = []
    if (not dry_run and claimed and str(path).lower().endswith((".md", ".markdown"))
            and hasattr(cartographer, "repair_markdown_c0_controls_on_disk")):
        # This callback appends to this worker's owned data list, never to a
        # live task/client. Cancellation cannot interrupt an in-flight disk phase.
        cartographer.repair_markdown_c0_controls_on_disk(
            path, claim_id=str(claimed), log_callback=messages.append,
        )
    try:
        if not dry_run and claimed and hasattr(cartographer, "normalize_beacon_comment_field_aliases_in_file"):
            result = cartographer.normalize_beacon_comment_field_aliases_in_file(path)
            if isinstance(result, dict) and result.get("changed"):
                messages.append("refresh_file_node_beacons: normalized beacon key alias "
                                f"comments= -> comment= in {path}")
    except Exception as e:  # noqa: BLE001
        messages.append("refresh_file_node_beacons: comment-field alias normalization failed "
                        f"for {path}: {e}")
    try:
        if not dry_run and claimed and hasattr(cartographer, "normalize_duplicate_ast_beacons_in_file"):
            result = cartographer.normalize_duplicate_ast_beacons_in_file(path)
            if isinstance(result, dict) and result.get("changed"):
                messages.append("refresh_file_node_beacons: normalized duplicate AST beacons "
                                f"in {path}: {result}")
    except Exception as e:  # noqa: BLE001
        messages.append("refresh_file_node_beacons: duplicate AST beacon normalization failed "
                        f"for {path}: {e}")
    try:
        file_map = cartographer.map_codebase(path, repo_root=repo_root)
    except Exception as e:  # noqa: BLE001
        raise NetworkError(f"Cartographer map_codebase failed for {path} (incremental scaffold): {e}") from e
    return {"file_map": file_map, "new_children": copy.deepcopy(file_map.get("children") or []),
            "messages": messages}
'''
