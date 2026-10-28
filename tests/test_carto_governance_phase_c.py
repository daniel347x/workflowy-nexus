from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path

import pytest

import nexus_map_codebase
from workflowy_mcp import carto_job_governance as gov

REPO = Path(__file__).resolve().parents[1]
ACCOUNT = "a" * 32


def _cache() -> dict[str, dict]:
    return {
        "vault": {"id": "vault"},
        "repo": {"id": "repo", "parent_id": "vault"},
        "folder": {"id": "folder", "parent_id": "repo"},
        "file": {"id": "file", "parent_id": "folder"},
        "other": {"id": "other", "parent_id": "repo"},
    }


def _snapshot(path: Path) -> dict:
    # S02: governed snapshots carry the v1 lineage wrapper (legacy wrappers
    # are bootstrap-only and no longer accepted at this seam).
    payload = {
        "snapshot_schema_version": 1,
        "snapshot_timestamp": gov.utc_now_iso(),
        "account_scope_id": ACCOUNT,
        "epoch_number": 0,
        "epoch_id": "e" * 32,
        "incorporated_through_seq": 0,
        "publication_kind": "full_refresh",
        "nodes_export": {"nodes": list(_cache().values())},
    }
    gov.atomic_write_json(path, payload)
    # Selection fixture only: live jobs borrow the publisher map, not a
    # production worker-loader adapter. Keep metadata needed by this test.
    return {"snapshot_path": str(path), "payload": payload,
            "nodes": payload["nodes_export"]["nodes"], "cache_by_id": _cache(),
            "freshness_disposition": "performed"}


def _footprint(tmp_path: Path, name: str = "repo/a.py", root: str = "file", kind: str = "file") -> dict:
    return gov.compute_footprint(tmp_path / name, root, _cache(), kind=kind)


def _admit(
    tmp_path: Path,
    jobs: Path,
    *,
    job_id: str = "active",
    dedupe: str,
    footprint: dict | None = None,
    job_type: str = "CARTO_REFRESH",
    account: str = ACCOUNT,
) -> dict:
    jobs.mkdir(parents=True, exist_ok=True)
    return asyncio.run(
        gov.admit_carto_job(
            job_file=jobs / f"{job_id}.json",
            job_payload={"id": job_id, "type": job_type},
            job_type=job_type,
            dedupe_key=dedupe,
            account_scope_id=account,
            footprint=footprint or _footprint(tmp_path),
            launcher="system",
            parent_job_id=None,
            prepared=False,
            jobs_root=jobs,
        )
    )


def _holds(jobs: Path) -> tuple[dict, ...]:
    from workflowy_mcp.mutation_locks import _governance_holds

    return tuple(_governance_holds(jobs))


def _cooldowns(jobs: Path) -> tuple[dict, ...]:
    from workflowy_mcp.mutation_locks import _governance_cooldowns

    return tuple(_governance_cooldowns(jobs, now_epoch=0.0, cooldown_seconds=10.0))


def test_real_subprocess_folder_vs_contained_file_admission_race(tmp_path, monkeypatch):
    """C/MEM: two same-loop admission tasks, one serialized decision per job.

    The former real-subprocess race becomes two tasks on the real async
    admission front.  Exactly one wins; the contained/containing footprint is
    refused with the unchanged ``overlap_conflict`` kind (ADMIT/OVERLAP).
    """
    jobs = tmp_path / "jobs"
    repo_path = tmp_path / "repo"
    file_path = repo_path / "a.py"
    repo_path.mkdir()
    file_path.write_text("x", encoding="utf-8")
    folder_footprint = _footprint(tmp_path, "repo", "folder", "folder")
    file_footprint = _footprint(tmp_path, "repo/a.py", "file", "file")

    async def admit(job_id: str, footprint: dict, dedupe: str):
        return await gov.admit_carto_job(
            job_file=jobs / f"{job_id}.json",
            job_payload={"id": job_id},
            job_type="CARTO_REFRESH",
            dedupe_key=dedupe,
            account_scope_id=ACCOUNT,
            footprint=footprint,
            launcher="system",
            parent_job_id=None,
            prepared=False,
            jobs_root=jobs,
        )

    async def scenario() -> None:
        if os.environ.get("CUT2B_NEUTRALIZE_ADMISSION_SERIALIZATION"):
            # Probe: a split decision/insertion lets the second admission's
            # decision miss the first admission's just-installed reservation.
            from workflowy_mcp import mutation_locks as locks

            monkeypatch.setattr(locks, "_governance_holds", lambda root=None: ())
        if os.environ.get("CUT2B_NEUTRALIZE_Q3_SYMMETRY"):
            monkeypatch.setattr(gov, "footprints_overlap", lambda _left, _right: False)
        results = await asyncio.gather(
            admit("folder-job", folder_footprint, "a" * 64),
            admit("file-job", file_footprint, "b" * 64),
            return_exceptions=True,
        )
        winners = [row for row in results if isinstance(row, dict)]
        refusals = [row for row in results if isinstance(row, gov.AdmissionRefusal)]
        assert len(winners) == 1
        assert len(refusals) == 1
        assert refusals[0].kind == "overlap_conflict"
        assert tuple(_holds(jobs)) and len(_holds(jobs)) == 1

    asyncio.run(scenario())


def test_negative_gate_dedupe_and_exact_cooldown_precede_freshness(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    dedupe = gov.derive_dedupe_key(
        account_scope_id=ACCOUNT,
        job_type="CARTO_REFRESH",
        mode="file",
        root_uuid="file",
    )
    _admit(tmp_path, jobs, dedupe=dedupe)

    if os.environ.get("CUT2B_NEUTRALIZE_DEDUPE_MEMORY"):
        # Probe: the exact-intent query no longer sees the live reservation (its
        # decision/insertion memory is split; no cooldown is published here).
        from workflowy_mcp import mutation_locks as locks

        with monkeypatch.context() as patch:
            patch.setattr(locks, "_governance_holds", lambda root=None: ())
            hit = asyncio.run(
                gov.negative_exact_intent_gate(dedupe_key=dedupe, jobs_root=jobs)
            )
    else:
        hit = asyncio.run(
            gov.negative_exact_intent_gate(dedupe_key=dedupe, jobs_root=jobs)
        )
    assert hit is not None
    assert hit["deduplicated"] is True
    assert "no second preparation ran" in hit["note"]

    claim_id = _holds(jobs)[0]["claim_id"] if _holds(jobs) else "f" * 32
    assert asyncio.run(gov.finalize_claim_async(claim_id, jobs, now_epoch=100)) is True
    assert _cooldowns(jobs)

    if os.environ.get("CUT2B_NEUTRALIZE_COOLDOWN"):
        cooldown_seconds = 0
    else:
        cooldown_seconds = 60
    cooldown = asyncio.run(
        gov.negative_exact_intent_gate(
            dedupe_key=dedupe,
            jobs_root=jobs,
            now_epoch=110,
            cooldown_seconds=cooldown_seconds,
        )
    )
    assert cooldown is not None
    assert cooldown["error_kind"] == "cooldown_active"
    assert cooldown["retry_after"] == 50


def test_nonidentical_overlap_is_not_decided_by_negative_gate(tmp_path):
    jobs = tmp_path / "jobs"
    _admit(tmp_path, jobs, dedupe="b" * 64)
    query = "b" * 64 if os.environ.get("CUT2B_NEUTRALIZE_NEGATIVE_GATE_SCOPE") else "c" * 64
    assert asyncio.run(gov.negative_exact_intent_gate(dedupe_key=query, jobs_root=jobs)) is None


def test_full_admission_refuses_claim_and_q3_overlap(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    footprint = _footprint(tmp_path)

    async def scenario() -> None:
        leases = await gov.acquire_q3_leases_async(
            [footprint], account_scope_id=ACCOUNT, jobs_root=jobs
        )
        if os.environ.get("CUT2B_NEUTRALIZE_Q3_SYMMETRY"):
            monkeypatch.setattr(gov, "footprints_overlap", lambda _left, _right: False)
        try:
            with pytest.raises(gov.AdmissionRefusal) as caught:
                await gov.admit_carto_job(
                    job_file=jobs / "new.json",
                    job_payload={"id": "new"},
                    job_type="CARTO_REFRESH",
                    dedupe_key="d" * 64,
                    account_scope_id=ACCOUNT,
                    footprint=footprint,
                    launcher="system",
                    parent_job_id=None,
                    prepared=False,
                    jobs_root=jobs,
                )
            assert caught.value.kind == "overlap_conflict"
        finally:
            assert await gov.release_q3_leases_async(leases, jobs_root=jobs) == []

        admitted = await gov.admit_carto_job(
            job_file=jobs / "new.json",
            job_payload={"id": "new"},
            job_type="CARTO_REFRESH",
            dedupe_key="d" * 64,
            account_scope_id=ACCOUNT,
            footprint=footprint,
            launcher="system",
            parent_job_id=None,
            prepared=False,
            jobs_root=jobs,
        )
        assert admitted["admitted"] is True
        assert await gov.finalize_claim_async(admitted["claim"]["claim_id"], jobs) is True

    asyncio.run(scenario())


def test_claim_and_initial_job_are_one_admission_decision(tmp_path):
    jobs = tmp_path / "jobs"
    footprint = _footprint(tmp_path)
    result = _admit(tmp_path, jobs, job_id="new", dedupe="e" * 64, footprint=footprint)
    assert result["admitted"] is True
    assert result["claim_path"] is None
    job = json.loads((jobs / "new.json").read_text(encoding="utf-8"))
    assert job["claim_id"] == result["claim"]["claim_id"]
    assert job["status"] == "preparing"
    holds = _holds(jobs)
    if os.environ.get("CUT2B_NEUTRALIZE_ADMISSION_SERIALIZATION"):
        # Probe: a split decision/insertion would let a second admission with a
        # different dedupe key insert another hold over the same footprint.
        try:
            asyncio.run(
                gov.admit_carto_job(
                    job_file=jobs / "split.json",
                    job_payload={"id": "split"},
                    job_type="CARTO_REFRESH",
                    dedupe_key="9" * 64,
                    account_scope_id=ACCOUNT,
                    footprint=footprint,
                    launcher="system",
                    parent_job_id=None,
                    prepared=False,
                    jobs_root=jobs,
                )
            )
        except gov.AdmissionRefusal:
            pass
        holds = _holds(jobs)
        assert len(holds) == 2
    assert len(holds) == 1
    assert holds[0]["claim_id"] == result["claim"]["claim_id"]
    assert holds[0]["job_id"] == "new"
    assert Path(holds[0]["job_file"]) == jobs / "new.json"
    assert holds[0]["kind"] == footprint["kind"]
    assert holds[0]["path"] == footprint["path"]
    assert holds[0]["root_uuid"] == footprint["root_uuid"]
    assert list(holds[0]["ancestor_chain"]) == list(footprint["ancestor_chain"])


def test_job_json_failure_finalizes_without_claim_leak(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"

    def fail_on_job(path, payload):
        if Path(path).name == "new.json":
            raise OSError("job write failed")
        gov.atomic_write_json(path, payload)

    if os.environ.get("CUT2B_NEUTRALIZE_ADMISSION_LEAK"):
        from workflowy_mcp import mutation_locks as locks

        monkeypatch.setattr(locks, "_finalize_carto_hold", lambda *args, **kwargs: True)

    with pytest.raises(OSError, match="job write failed"):
        asyncio.run(
            gov.admit_carto_job(
                job_file=jobs / "new.json",
                job_payload={"id": "new"},
                job_type="CARTO_REFRESH",
                dedupe_key="f" * 64,
                account_scope_id=ACCOUNT,
                footprint=_footprint(tmp_path),
                launcher="system",
                parent_job_id=None,
                prepared=False,
                jobs_root=jobs,
                atomic_writer=fail_on_job,
            )
        )

    # The exact reservation rolled back and its cooldown was published; no
    # retired claim/tombstone file is produced.
    assert tuple(_holds(jobs)) == ()
    assert _cooldowns(jobs)
    assert not (jobs / "new.json").exists()


def test_prepared_launch_ack_precedes_b0_and_uses_frozen_empty_bytes(tmp_path, monkeypatch):
    from n07_job_harness import install_legacy_task_fixture
    fixture = install_legacy_task_fixture(monkeypatch, jobs=tmp_path / "jobs", account=ACCOUNT)
    async def scenario():
        jobs = tmp_path / "jobs"
        snapshot = _snapshot(tmp_path / "snapshot.json")
        started = asyncio.Event()
        release = asyncio.Event()
        prepared = []
        tasks = []

        async def freshness():
            started.set()
            await release.wait()
            return snapshot

        async def preparation(payload, _selection):
            prepared.append((payload["name"], payload["note"]))
            return {"success": False, "operation": "noop"}

        def task_factory(coro):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task

        worker = tmp_path / "worker.py"
        worker.write_text("# worker", encoding="utf-8")
        digest = gov.derive_preparation_input_digest("ast_beacon_write", "file", "", "")
        frozen = (
            {"name": "stale", "note": "stale"}
            if os.environ.get("CUT2B_NEUTRALIZE_PREPARED_FREEZE")
            else {"name": "", "note": ""}
        )
        result = await gov.launch_carto_refresh_job(
            root_uuid="file",
            mode="file",
            account_scope_id=ACCOUNT,
            footprint_factory=lambda _selection: gov.compute_footprint(
                tmp_path / "repo" / "a.py", "file", _cache(), kind="file"
            ),
            jobs_root=jobs,
            schedule_job=fixture.schedule,
            worker_script=worker,
            env={},
            preparation=preparation,
            preparation_gate=lambda _value: {"proceed": False, "status": "completed"},
            frozen_payload=frozen,
            preparation_input_digest=digest,
            initial_footprint=gov.compute_footprint(
                tmp_path / "repo" / "a.py", "file", _cache(), kind="file"
            ),
            freshness_callback=freshness,
            task_factory=task_factory,
        )
        assert result["success"] is True and result["preparing"] is True
        assert prepared == []
        await started.wait()
        release.set()
        await fixture.tasks[0]
        assert prepared == [("", "")]
        assert tuple(_holds(jobs)) == ()

    asyncio.run(scenario())


def test_prepared_b0_missing_basis_fails_and_finalizes(tmp_path, monkeypatch):
    from n07_job_harness import install_legacy_task_fixture
    if os.environ.get("CUT2B_NEUTRALIZE_FINALIZE_RELEASE_ORDER"):
        from workflowy_mcp import mutation_locks as locks

        monkeypatch.setattr(locks, "_finalize_carto_hold", lambda *args, **kwargs: True)
    fixture = install_legacy_task_fixture(monkeypatch, jobs=tmp_path / "jobs", account=ACCOUNT)
    async def scenario():
        jobs = tmp_path / "jobs"
        worker = tmp_path / "worker.py"
        worker.write_text("# worker", encoding="utf-8")
        tasks = []
        prepared = []

        def task_factory(coro):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task

        def missing_basis():
            raise RuntimeError("No usable live basis exists")

        result = await gov.launch_carto_refresh_job(
            root_uuid="file",
            mode="file",
            account_scope_id=ACCOUNT,
            footprint_factory=lambda _selection: {},
            jobs_root=jobs,
            schedule_job=fixture.schedule,
            worker_script=worker,
            env={},
            preparation=lambda *_args: prepared.append(True),
            initial_footprint=gov.compute_footprint(
                tmp_path / "repo" / "a.py", "file", _cache(), kind="file"
            ),
            freshness_callback=missing_basis,
            task_factory=task_factory,
        )
        assert result["success"] is True
        await fixture.tasks[0]
        assert prepared == []
        job = next(jobs.glob("*.json"))
        assert json.loads(job.read_text(encoding="utf-8"))["status"] == "failed"
        assert tuple(_holds(jobs)) == ()

    asyncio.run(scenario())


def test_claimed_c0_repair_atomic_cancel_and_noop(tmp_path):
    path = tmp_path / "poisoned.md"
    original = b"# H\ntext\x0bpath\n"
    path.write_bytes(original)
    with pytest.raises(RuntimeError):
        nexus_map_codebase.repair_markdown_c0_controls_on_disk(
            str(path), claim_id="claim", cancel_check=lambda: True
        )
    assert path.read_bytes() == original
    logs = []
    result = nexus_map_codebase.repair_markdown_c0_controls_on_disk(
        str(path), claim_id="claim", log_callback=logs.append
    )
    assert result["changed"] is True
    assert result["counts"]["VT"] == 1
    assert b"\\v" in path.read_bytes()
    mtime = path.stat().st_mtime_ns
    noop = nexus_map_codebase.repair_markdown_c0_controls_on_disk(str(path), claim_id="claim")
    assert noop["changed"] is False
    assert path.stat().st_mtime_ns == mtime
    assert logs and "counts" in logs[0]


def _runtime_files() -> list[Path]:
    roots = list((REPO / "src").rglob("*.py"))
    roots.extend(
        REPO / name
        for name in (
            "beacon_mutation_contract.py",
            "beacon_obtain_code_snippet.py",
            "glimpse_deploy_trampoline.py",
            "markdown_roundtrip.py",
            "nexus_map_codebase.py",
        )
    )
    return [path for path in roots if path.is_file() and ".venv" not in path.parts]


def _callers_of(symbol: str) -> set[str]:
    import os
    callers = set()
    for path in _runtime_files():
        source = path.read_text(encoding="utf-8")
        if os.environ.get("CUT2B_NEUTRALIZE_CALLER_DRIFT") and path.name == "server.py":
            source += (
                '\nasync def _cut2b_unregistered_caller():\n'
                '    await _start_carto_refresh_job("root", "file")\n'
                '\nasync def _cut2b_unregistered_q3_caller(leases):\n'
                '    await acquire_q3_leases_async(leases)\n'
            )
        tree = ast.parse(source, filename=str(path))
        for function in [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]:
            if function.name == symbol:
                continue
            if any(
                isinstance(node, ast.Call)
                and (
                    isinstance(node.func, ast.Name)
                    and node.func.id == symbol
                    or isinstance(node.func, ast.Attribute)
                    and node.func.attr == symbol
                )
                for node in ast.walk(function)
            ):
                callers.add(function.name)
    return callers


def test_phase_c_matrix_static_caller_closure():
    # Only the real daemon front reaches the retained shared launcher.
    from test_n07_carto_fences import _assert_no_job_process, _n07_runtime_sources
    _, reached, _ = _assert_no_job_process(_n07_runtime_sources())
    assert "workflowy_mcp.server:_schedule_carto_job" in reached
    assert "workflowy_mcp.markdown_generate_pipeline:_run_phase8_refresh_inline" in reached
    assert "workflowy_mcp.markdown_generate_pipeline:_launch_phase8_refresh_via_authority" not in reached
    assert _callers_of("launch_carto_refresh_job") == {
        "_start_carto_refresh_job",
    }
    assert _callers_of("_start_carto_refresh_job") == {
        "_handle_refresh_file_node",
        "_handle_refresh_folder_node",
        "_handle_materialize_markdown_file",
        "_handle_bulk_materialize_markdown",
        "_beacon_map_refresh_impl",
        "cartographer_refresh_async",
    }
    assert _callers_of("_start_carto_markdown_generate_job") == {
        "_handle_generate_markdown_file",
        "_handle_bulk_generate_markdown",
    }
    assert _callers_of("_start_carto_bulk_visible_apply_job") == {"_handle_bulk_apply_visible_nodes"}


def test_reconcile_and_q3_matrix_static_caller_closure():
    assert _callers_of("refresh_file_node_beacons") == {
        "_execute_carto_job",  # N07 public jobs' common ownership core
        "_run_phase8_refresh_inline",  # same parent task/grant, no child launch
        "_handle_open_node_in_windsurf",  # dry_run=True N6
        "_run_carto_bulk_visible_apply_job",  # claimed N4
        "refresh_folder_cartographer_sync",  # primitive-internal N2
    }
    assert _callers_of("refresh_folder_cartographer_sync") == {
        "_execute_carto_job",  # N07 common ownership core
        "refresh_file_node_beacons",  # primitive-internal N1
    }
    assert _callers_of("acquire_q3_leases_async") == {
        "_acquire_single_beacon_q3_lease",
    }
    # B and F inject this exact existing API into the one shared admission.
    tree = ast.parse((REPO / "src/workflowy_mcp/server.py").read_text(encoding="utf-8"))
    for name in ("_lightning_rod_mutate_beacons", "_beacon_fix_impl"):
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
        assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "BeaconAdmission"
                   and any(k.arg == "acquire" and isinstance(k.value, ast.Name)
                           and k.value.id == "acquire_q3_leases_async" for k in n.keywords) for n in ast.walk(function))
    batch = ast.parse((REPO / "src/workflowy_mcp/beacon_batch.py").read_text(encoding="utf-8"))
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "admit"
               and isinstance(n.func.value, ast.Name) and n.func.value.id == "admission" for n in ast.walk(batch))


def test_no_production_carto_worker_spawn_and_retained_dead_authority():
    import os
    from test_n07_carto_fences import _assert_no_job_process, _n07_runtime_sources
    _assert_no_job_process(_n07_runtime_sources())
    # No retained dead spawner exception remains after physical subtraction.
    offenders = []
    for path in _runtime_files():
        source = path.read_text(encoding="utf-8")
        if os.environ.get("CUT2B_NEUTRALIZE_SPAWN_SITE") and path.name == "carto_job_governance.py":
            source += '\ndef _cut2b_restored_worker():\n    return popen_factory(["--carto-job-file", "job"])\n'
        tree = ast.parse(source, filename=str(path))
        for function in [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]:
            text = ast.get_source_segment(source, function) or ""
            if "--carto-job-file" in text and any(
                isinstance(node, ast.Call)
                and (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "popen_factory"
                    or isinstance(node.func, ast.Attribute)
                    and node.func.attr == "Popen"
                )
                for node in ast.walk(function)
            ):
                offenders.append((path.name, function.name))
    assert offenders == []


def test_freshness_disposition_vocabulary_and_matrix_ids_are_closed():
    import os
    expected_ids = {
        *(f"L{index}" for index in range(1, 11)),
        *(f"Q{index}" for index in range(1, 5)),
        *(f"N{index}" for index in (1, 2, 3, 4, 6)),
        *(f"R{index}" for index in range(1, 6)),
    }
    matrix = dict(gov.PHASE_C_ORIGIN_MATRIX)
    if os.environ.get("CUT2B_NEUTRALIZE_VOCAB_DRIFT"):
        matrix["L11"] = {"origin": "retired phase8 child"}
    assert set(matrix) == expected_ids
    # The measured third Q3 origin (merged beacon.fix) is an exact current row.
    assert matrix["Q4"] == {
        "origin": "_beacon_fix_impl",
        "work": "Q3_LEASE",
        "freshness": "NONE",
    }
    # v14 is historical evidence, not a current worker-origin allowlist.
    source = "\n".join(path.read_text(encoding="utf-8") for path in _runtime_files())
    for disposition in (
        "performed",
        "skipped",
        "gesture_performed",
        "inherited_parent",
    ):
        assert disposition in source
