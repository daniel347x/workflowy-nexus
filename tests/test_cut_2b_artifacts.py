"""CUT-2B §9.9.4: old-home, fresh-install and persisted-file artifact witnesses.

Three executable witnesses, all on isolated roots and a fake Workflowy HTTP
transport — never Dan's state home, daemon, ports or a live repository refresh:

* the old-home inventory seeds valid retained state plus **all eleven** retired
  coordination artifacts (valid-looking and malformed/foreign-PID forms) and
  proves the real startup/W12, warm load, full refresh, folder map job,
  cancellation, snapshot GC and job GC/history-clear perform zero direct
  operations on them, leave bytes/mtimes/names identical and emit no warning;
* the fresh-install witness starts on an empty home, reaches the real
  in-process folder job through the public ``beacon.map.refresh`` route and
  proves none of the eleven paths (or a governor ``.corrupt.*`` sidecar) is
  ever created;
* the retained-persistence witness compares manifest/dirt/journal/snapshot/job
  formats against BASE literals, verifies both governor files' exact schemas
  and old/new restart floors, and proves historical job JSON never
  reconstructs ownership or cooldown.

The ``CUT2B_NEUTRALIZE_*`` names are test-only counterfactuals: each adds one
forbidden touch/creation/renamed shape/reconstructed hold and must make the
intended assertion fail.  No production flag, reset hook or safety is added.
"""

from __future__ import annotations

import asyncio
import builtins
import glob as glob_module
import io
import json
import logging
import os
import re
import warnings as warnings_module
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from n05_region_harness import RegionHTTPProbe, node_uuid
from n07_job_harness import (  # noqa: F401 - fixtures are registered by import
    SUITE_ENDPOINT,
    TEST_WAIT_SECONDS,
    assert_endpoint,
    launch_real_job,
    real_job_environment,  # noqa: F811 - registered as a pytest fixture parameter
    scrubbed_runtime,  # noqa: F811 - registered as a pytest fixture parameter
    wait_job_gate,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "workflowy_mcp"
WAIT = TEST_WAIT_SECONDS


@pytest.fixture(autouse=True)
def isolated_daemon_cleared_history(monkeypatch):
    """Each isolated state-home witness has its own daemon lifetime.

    The valid old-home case clears legacy-running; its daemon-lifetime id must
    not suppress the independent malformed/restart fixture with the same id.
    Production keeps the G3 late-write protection unchanged.
    """
    from workflowy_mcp import config
    for name in ("_CARTO_CLEARED_JOB_IDS", "_CARTO_CLEARED_JOB_PATHS", "_CARTO_CLEARED_WRITE_LOGGED"):
        monkeypatch.setattr(config, name, set())

#: §6.6's eleven retired artifacts, keyed by the retired path-builder literal.
RETIRED_ARTIFACTS = (
    "mutation_leases",
    "refresh_publication",
    "coherence_mutex",
    "admission_mutex",
    "refresh_locks",
    "q3_leases",
    "refresh_tombstones",
    "cancel_requests",
    "heartbeats",
    "refresh_claims",
    "write_slot_mutex",
)
RETIRED_DIRS = frozenset({
    "mutation_leases", "refresh_locks", "q3_leases", "refresh_tombstones",
    "cancel_requests", "heartbeats", "refresh_claims",
})
#: Warning text may never name any retired artifact (dots stripped for matching).
RETIRED_MENTIONS = (
    "mutation_leases", "refresh_publication", "coherence.mutex", "admission.mutex",
    "refresh_locks", "q3_leases", "refresh_tombstones", "cancel_requests",
    "heartbeats", "refresh_claims", "write_slot.mutex",
)

# --------------------------------------------------------------------------- #
# BASE 7afe1f4 durable format oracles (literals, never implementation output)
# --------------------------------------------------------------------------- #
MANIFEST_KEYS = frozenset({
    "schema_version", "account_scope_id", "epoch_number", "epoch_id",
    "base_snapshot_path", "base_incorporated_through_seq", "journal_floor_seq",
    "journal_head_seq", "coherence_mode", "dirty_status", "created_at",
    "updated_at", "previous_epoch_id",
})
DIRTY_STATUS_KEYS = frozenset({
    "schema_version", "account_scope_id", "active", "generation", "reason_code",
    "detail", "transaction_id", "affected_region", "raised_at",
})
EXTERNAL_DIRT_KEYS = frozenset({
    "schema_version", "account_scope_id", "active", "generation", "observed_at",
    "mutation_kind", "node_uuid", "parent_uuid", "detail",
})
JOURNAL_KEYS = frozenset({
    "schema_version", "account_scope_id", "sequence", "previous_sequence",
    "transaction_id", "published_epoch_id", "operation_kind", "committed_at",
    "producer", "verification", "effects",
})
PRODUCER_KEYS = frozenset({
    "pid", "pid_create_time", "owner_kind", "ownership_kind",
    "ownership_token", "job_id",
})
SNAPSHOT_KEYS = frozenset({
    "snapshot_schema_version", "snapshot_timestamp", "account_scope_id",
    "epoch_number", "epoch_id", "incorporated_through_seq",
    "publication_kind", "nodes_export",
})
JOB_KEYS = frozenset({
    "id", "type", "mode", "root_uuid", "created_at", "updated_at",
    "execution_model", "execution_owner", "detached", "cache_refresh_required",
    "freshness_disposition", "snapshot_path", "snapshot_lineage",
    "preparation_input_digest", "source_regime", "progress", "result_summary",
    "error", "logs_path",
})
BREAKER_KEYS = frozenset({
    "state", "blocked_until", "cooldown_s", "probe_claim_id",
    "probe_lease_expires_at", "reason", "evidence_excerpt",
})
GOVERNOR_KEYS = frozenset({"next_slot_at"})

#: Real job artifact convention: ``<YYYYMMDD-HHMMSS>_<job-id>(.json|.log|/)``.
_JOB_ARTIFACT_RE = re.compile(r"^\d{8}-\d{6}_[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _job_artifact_name(name: str) -> bool:
    core = name.split(".tmp", 1)[0]
    for suffix in (".json", ".log"):
        if core.endswith(suffix):
            core = core[: -len(suffix)]
    return bool(_JOB_ARTIFACT_RE.fullmatch(core))


def _enabled(name: str) -> str | None:
    return os.environ.get(name)


# --------------------------------------------------------------------------- #
# Retired-path geometry and seeding
# --------------------------------------------------------------------------- #
def _governance_paths(scope: str, jobs: Path):
    from workflowy_mcp.carto_write_governor import account_write_governance_paths

    return account_write_governance_paths(scope, jobs)


def _retired_paths(jobs: Path, account_root: Path, governance_account: Path) -> dict[str, Path]:
    """Every retired artifact at its old exact location beneath this home."""
    return {
        "mutation_leases": jobs / ".mutation_leases",
        "refresh_publication": account_root / "refresh_publication.json",
        "coherence_mutex": account_root / "coherence.mutex",
        "admission_mutex": jobs / "admission.mutex",
        "refresh_locks": jobs / ".refresh_locks",
        "q3_leases": jobs / ".q3_leases",
        "refresh_tombstones": jobs / ".refresh_tombstones",
        "cancel_requests": jobs / ".cancel_requests",
        "heartbeats": jobs / ".heartbeats",
        "refresh_claims": jobs / ".refresh_claims",
        "write_slot_mutex": governance_account / "write_slot.mutex",
    }


def _artifact_payload(name: str, scope: str) -> dict:
    foreign = {"pid": 999_999, "owner_pid": 999_999, "pid_create_time": 1.0,
               "account_scope_id": scope, "claim_id": "f" * 32}
    if name == "mutation_leases":
        return {"lease_id": "e" * 32, "transaction_id": "t" * 32, "regions": [],
                "state": "sent_verified", **foreign}
    if name == "refresh_publication":
        return {"schema_version": 1, "transaction_id": "t" * 32,
                "published_epoch_id": "p" * 32, "state": "published", **foreign}
    return {"nonce": "n" * 32, "created_at": "2020-01-01T00:00:00+00:00", **foreign}


def _seed_retired(paths: dict[str, Path], scope: str, form: str) -> None:
    """Seed all eleven artifacts in one form: valid-looking or malformed/foreign."""
    assert form in {"valid", "malformed"}
    for name, path in paths.items():
        if name in RETIRED_DIRS:
            path.mkdir(parents=True, exist_ok=True)
            if form == "valid":
                (path / f"valid-{name}.json").write_text(
                    json.dumps(_artifact_payload(name, scope)) + "\n", encoding="utf-8")
            else:
                (path / f"malformed-{name}.json").write_bytes(
                    b"{malformed stale carrier, foreign pid=999999\n")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            if form == "valid":
                path.write_text(json.dumps(_artifact_payload(name, scope)),
                                encoding="utf-8")
            else:
                path.write_bytes(b"{malformed stale carrier, foreign pid=999999\n")


def _artifact_image(paths: dict[str, Path]) -> dict:
    """Bytes, mtimes and tree names for every seeded artifact.

    Directory mtimes are part of the image: a swallowed create/unlink inside a
    retired directory changes the parent's mtime even when the visible file
    set is restored.
    """
    image: dict = {}
    for name, path in paths.items():
        if path.is_dir():
            entries = [("dir", ".", path.stat().st_mtime_ns, None)]
            for child in sorted(path.rglob("*")):
                relative = child.relative_to(path).as_posix()
                if child.is_dir():
                    entries.append(("dir", relative, child.stat().st_mtime_ns, None))
                else:
                    entries.append(("file", relative, child.read_bytes(),
                                    child.stat().st_mtime_ns))
            image[name] = ("dir", tuple(entries))
        else:
            image[name] = ("file", path.read_bytes(), path.stat().st_mtime_ns)
    return image


def _mentions_retired(text: str) -> str | None:
    for token in RETIRED_MENTIONS:
        if token in text:
            return token
    return None


# --------------------------------------------------------------------------- #
# The retired-artifact spy matrix
# --------------------------------------------------------------------------- #
def _retired_hit(value: object, targets: dict[str, str]) -> str | None:
    if isinstance(value, int):
        return None
    try:
        text = os.fsdecode(value)  # type: ignore[arg-type]
    except TypeError:
        return None
    normalized = os.path.normcase(os.path.abspath(text))
    for target, name in targets.items():
        if normalized == target or normalized.startswith(target + os.sep):
            return name
    return None


def _scan_retired(args, kwargs, targets: dict[str, str], touches: list[str],
                  source: str) -> None:
    def visit(value):
        if isinstance(value, (str, bytes, os.PathLike)):
            name = _retired_hit(value, targets)
            if name is not None:
                # Record BEFORE raising: a best-effort caller that swallows the
                # AssertionError must still leave a persistent failure, or the
                # zero-operation assertion could pass despite a real touch.
                touches.append(f"{name} via {source} ({value!r})")
                raise AssertionError(
                    f"retired artifact touched: {name} ({value!r})")
            return
        if isinstance(value, (tuple, list, set, frozenset)):
            for part in value:
                visit(part)
        elif isinstance(value, dict):
            for part in value.values():
                visit(part)

    for value in args:
        visit(value)
    for value in kwargs.values():
        visit(value)


def _spy(monkeypatch, owner, names, targets, touches) -> None:
    for attribute in names:
        original = getattr(owner, attribute)
        source = f"{getattr(owner, '__name__', str(owner))}.{attribute}"

        def guarded(*args, _original=original, _source=source, **kwargs):
            _scan_retired(args, kwargs, targets, touches, _source)
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, attribute, guarded)


def _logging_spy(monkeypatch, names, targets, touches, messages) -> None:
    for attribute in names:
        original = getattr(logging.Logger, attribute)

        def guarded(self, msg, *args, _original=original, _attribute=attribute, **kwargs):
            _scan_retired((msg, args), kwargs, targets, touches,
                          f"logging.Logger.{_attribute}")
            try:
                text = str(msg) % args if args else str(msg)
            except Exception:  # noqa: BLE001 - logging formatting is best-effort
                text = f"{msg} {args} {kwargs}"
            messages.append(text)
            return _original(self, msg, *args, **kwargs)

        monkeypatch.setattr(logging.Logger, attribute, guarded)


def _install_artifact_spies(monkeypatch, paths: dict[str, Path], messages: list[str],
                            touches: list[str]) -> None:
    targets = {
        os.path.normcase(os.path.abspath(os.fspath(path))): name
        for name, path in paths.items()
    }

    for owner, names in (
        (builtins, ("open",)),
        (io, ("open",)),
        (os, ("open", "stat", "lstat", "scandir", "listdir", "mkdir", "makedirs",
              "unlink", "remove", "rmdir", "rename", "replace", "access", "readlink")),
        (os.path, ("exists", "isfile", "isdir", "lexists", "islink", "getsize", "getmtime")),
        (Path, ("open", "read_text", "read_bytes", "write_text", "write_bytes",
                "stat", "lstat", "exists", "is_file", "is_dir", "glob", "rglob",
                "iterdir", "unlink", "mkdir", "rmdir", "touch", "rename", "replace")),
        (glob_module, ("glob", "iglob")),
        (warnings_module, ("warn",)),
    ):
        _spy(monkeypatch, owner, names, targets, touches)
    _logging_spy(monkeypatch, ("warning", "error", "critical", "exception", "log"),
                 targets, touches, messages)

    from workflowy_mcp import server

    original_log = server.log_event

    def capturing_log_event(message, component="SERVER"):
        _scan_retired((message,), {}, targets, touches, "server.log_event")
        messages.append(str(message))
        return original_log(message, component)

    monkeypatch.setattr(server, "log_event", capturing_log_event)


def _probe_ignore_artifact(paths: dict[str, Path]) -> None:
    """Add a forbidden touch; caught: also proves best-effort cannot hide it."""
    requested = _enabled("CUT2B_NEUTRALIZE_IGNORE_ARTIFACT")
    if not requested:
        return
    caught = requested.startswith("caught:")
    requested = requested.removeprefix("caught:")
    selected = RETIRED_ARTIFACTS if requested == "all" else (requested,)
    for name in selected:
        assert name in paths, f"unknown retired artifact probe target {requested!r}"
        path = paths[name]
        try:
            if path.is_dir():
                os.scandir(path)
            else:
                os.stat(path)
        except AssertionError:
            if not caught:
                raise


def _probe_create_artifact(paths: dict[str, Path], scope: str) -> None:
    """``CUT2B_NEUTRALIZE_ARTIFACT_CREATION``: restore one builder/write."""
    requested = _enabled("CUT2B_NEUTRALIZE_ARTIFACT_CREATION")
    if not requested:
        return
    selected = RETIRED_ARTIFACTS if requested == "all" else (requested,)
    for name in selected:
        assert name in paths, f"unknown retired artifact probe target {requested!r}"
        path = paths[name]
        payload = json.dumps(_artifact_payload(name, scope))
        if name in RETIRED_DIRS:
            path.mkdir(parents=True, exist_ok=True)
            (path / f"restored-{name}.json").write_text(payload, encoding="utf-8")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Retained-state seeding (valid old home)
# --------------------------------------------------------------------------- #
def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _breaker_payload(clock_value: float, cooldown: float = 900.0) -> dict:
    return {
        "state": "closed",
        "blocked_until": 0.0,
        "cooldown_s": float(cooldown),
        "probe_claim_id": None,
        "probe_lease_expires_at": 0.0,
        "reason": "",
        "evidence_excerpt": "",
    }


def _seed_retained_home(env, scope: str) -> dict:
    """Valid manifest/base/tail/dirt, retained job records and both governor files."""
    from coherence_harness import s02_append, s02_set_external_dirt

    paths = env.paths
    s02_append(paths, operation_kind="cut2b-artifacts-witness")
    s02_set_external_dirt(paths, 1, active=False)

    governance = _governance_paths(scope, env.jobs)
    _atomic_json(governance.breaker_state, _breaker_payload(1000.0))
    _atomic_json(governance.governor_state, {"next_slot_at": 0.0})

    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=2)).isoformat()
    completed_id = "legacy-completed"
    running_id = "legacy-running"
    jobs = {}
    for job_id, status, extra in (
        (completed_id, "completed", {}),
        (running_id, "running", {
            "pid": 999_999, "owner_pid": 999_999, "pid_create_time": 1.0,
            "claim_id": "d" * 32, "owner_kind": "process",
        }),
    ):
        job_path = env.jobs / f"20200101-000000_{job_id}.json"
        log_path = env.jobs / f"20200101-000000_{job_id}.log"
        payload = {
            "id": job_id,
            "type": "CARTO_REFRESH",
            "mode": "file",
            "root_uuid": "a" * 32,
            "created_at": old,
            "updated_at": old,
            "execution_model": "daemon_task",
            "execution_owner": "launcher",
            "detached": False,
            "cache_refresh_required": True,
            "freshness_disposition": "performed",
            "snapshot_path": None,
            "snapshot_lineage": None,
            "preparation_input_digest": "",
            "source_regime": "standard",
            "progress": {"total_files": 1, "completed_files": 1,
                         "current_file": None, "current_phase": "done"},
            "result_summary": {"files_refreshed": 1, "files_deleted": 0, "errors": []},
            "error": None,
            "logs_path": str(log_path),
            "status": status,
            **extra,
        }
        _atomic_json(job_path, payload)
        log_path.write_text("retained job log\n", encoding="utf-8")
        jobs[job_id] = job_path
    return {"governance": governance, "jobs": jobs,
            "completed": jobs[completed_id], "running": jobs[running_id]}


async def _replace_nodes_export_transport(env) -> None:
    """Keep the N07 fake for CRUD; add the real /nodes-export GET it never served."""
    probe, client = env.probe, env.client

    async def transport(request):
        assert_endpoint(str(request.url).split("/api/v1", 1)[0] + "/api/v1")
        if request.method == "GET" and request.url.path.rstrip("/").endswith("nodes-export"):
            return httpx.Response(200, json={"nodes": list(probe.nodes.values())})
        if request.method != "GET" and env.send_gate is not None:
            await env.send_gate(request)
        return await probe.request(request)

    previous = client._client
    client._client = httpx.AsyncClient(
        base_url=SUITE_ENDPOINT, transport=httpx.MockTransport(transport))
    await previous.aclose()


async def _launch_folder_map(env) -> str:
    """Public beacon.map.refresh route to the real in-process folder job."""
    from workflowy_mcp.coherence.operation_cache import cache_operation_boundary

    async with cache_operation_boundary(env.client, "cut2b:artifacts-folder-map"):
        record = await env.server._beacon_map_refresh_impl(
            node_handle=env.ids["folder"], mode="folder", force=True)
    job = record.get("job") or {}
    job_id = str(job.get("id") or record.get("job_id") or "")
    assert job_id, record
    assert job_id in env.tasks, (record, set(env.tasks))
    return job_id


async def _join(env, job_id: str, timeout: float = WAIT * 4) -> dict:
    await asyncio.wait_for(env.tasks[job_id], timeout)
    return env.controls[job_id].terminal_result or {}


# --------------------------------------------------------------------------- #
# Test 1 — old home ignores every retired artifact
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("artifact_form", ["valid", "malformed"])
async def test_old_state_home_ignores_every_retired_artifact(
    real_job_environment, monkeypatch, capsys, artifact_form  # noqa: F811
):
    env = real_job_environment
    server, client, jobs = env.server, env.client, env.jobs
    paths = env.paths
    scope = paths.account_root.name
    governance = _governance_paths(scope, jobs)
    retired = _retired_paths(jobs, paths.account_root, governance.account_dir)
    assert set(retired) == set(RETIRED_ARTIFACTS)

    seeded = _seed_retained_home(env, scope)
    running_path = seeded["running"]
    _seed_retired(retired, scope, artifact_form)
    before = _artifact_image(retired)
    messages: list[str] = []
    touches: list[str] = []

    with monkeypatch.context() as patch:
        _install_artifact_spies(patch, retired, messages, touches)
        await _replace_nodes_export_transport(env)
        _probe_ignore_artifact(retired)

        # Real startup/W12 on the old home; the seeded nonterminal job is marked,
        # never resumed, and terminal history is untouched.
        await server._mark_abandoned_carto_tasks_at_startup()
        running = json.loads(running_path.read_text(encoding="utf-8"))
        assert running["status"] == "failed"
        assert running["error_kind"] == "daemon_restarted"
        assert running["error_extra"]["restart_reason"] == "previous_daemon_ended"
        assert not server._carto_inprocess_tasks

        # Warm load through the real manifest/base + seeded journal tail.
        assert client._load_nodes_export_cache_from_disk() is True
        manifest_head = json.loads(paths.manifest_path.read_text(encoding="utf-8"))[
            "journal_head_seq"]
        assert manifest_head == 1, "the seeded retained tail was not journal head 1"
        loaded = client._cache_view_publisher.capture()
        assert loaded.adopted_sequence == manifest_head
        view, _dirty = client._capture_view_and_dirty()
        assert view.epoch_id and view.adopted_sequence == manifest_head

        # Full refresh through the real refresh authority.
        full = await client.refresh_nodes_export_cache(settle_seconds=0)
        assert full["success"] is True

        # One real folder map job through the public route.
        folder_job = await _launch_folder_map(env)
        folder_terminal = await _join(env, folder_job)
        assert folder_terminal["status"] in {"completed", "completed_with_warnings"}, folder_terminal

        # Cancellation of another (disjoint) real file job parked on its write.
        entered, release = asyncio.Event(), asyncio.Event()

        async def gated_send(request):
            if not entered.is_set():
                entered.set()
                await release.wait()

        env.send_gate = gated_send
        peer_ack = await launch_real_job(env, "peer")
        assert peer_ack["success"], peer_ack
        peer_job = peer_ack["job_id"]
        control = env.controls[peer_job]
        cancel = None
        try:
            await wait_job_gate(env, entered, job_id=peer_job)
            cancel = asyncio.create_task(server._system_job_cancel_impl(peer_job))
            for _ in range(2000):
                if control.user_requested:
                    break
                await asyncio.sleep(0.001)
            assert control.user_requested
        finally:
            release.set()
            await env.tasks[peer_job]
            if cancel is not None:
                result = await cancel
        assert result["outcome"] == "cooperative"
        assert control.terminal_result["status"] == "cancelled"
        env.send_gate = None

        # Snapshot GC and the real job GC/history clear.
        collected = client._gc_nodes_export_snapshots_diagnostic(max_keep=1, min_age_seconds=0)
        assert collected["aborted"] is False
        await server._run_carto_job_gc_pass()
        cleared = await server._clear_carto_job_history(str(jobs))
        assert cleared["success"] is True

    after = _artifact_image(retired)
    assert touches == [], f"retired artifact operations recorded: {touches}"
    assert after == before, "retired artifact bytes/mtimes/names changed"
    warned = [message for message in messages if _mentions_retired(message)]
    assert warned == [], f"retired artifact warned about: {warned}"
    captured = capsys.readouterr()
    for text in (captured.out, captured.err):
        token = _mentions_retired(text)
        assert token is None, f"retired artifact named in output: {token}"

    # History clear removed the terminal CARTO rows but not the retired artifacts.
    assert not running_path.exists()


# --------------------------------------------------------------------------- #
# Test 2 — fresh install creates none of the retired artifacts
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def fresh_job_environment(scrubbed_runtime, monkeypatch):  # noqa: F811
    """One real client/server over a genuinely empty isolated home + fake HTTP."""
    from pydantic import SecretStr

    from workflowy_mcp import carto_job_governance as gov
    from workflowy_mcp import daemon_lifecycle as lifecycle
    from workflowy_mcp import mutation_locks as locks
    from workflowy_mcp import server
    from workflowy_mcp.client import api_client_core as core
    from workflowy_mcp.client import api_client_etch as etch
    from workflowy_mcp.client import api_client_nexus as nexus
    from workflowy_mcp.models import APIConfiguration
    from workflowy_mcp.models.config import WriteGovernorConfiguration
    from workflowy_mcp.mutation_lease_governance import ReservedMutationParent

    root = scrubbed_runtime
    repo, jobs, snapshots, artifacts = (
        root / name for name in ("repo", "jobs", "snapshots", "artifacts"))
    for path in (repo, jobs, snapshots, artifacts):
        path.mkdir()
    monkeypatch.setattr(gov, "get_cartographer_jobs_dir", lambda: jobs)
    monkeypatch.setattr(core, "get_runtime_subdir", lambda *parts: snapshots
                        if parts == ("nodes_export_cache",) else root.joinpath(*parts))
    monkeypatch.setattr(nexus, "get_cartographer_file_refresh_dir", lambda: artifacts)
    monkeypatch.setattr(etch, "get_runtime_subdir", lambda *parts: root.joinpath(*parts))

    ids = {key: node_uuid("cut2b-artifacts-" + key) for key in ("vault", "folder", "file", "symbol")}
    source = repo / "source.py"
    source.write_text("def existing():\n    return 2\n", encoding="utf-8")
    nodes = [
        {"id": ids["vault"], "name": "Account", "parent_id": None},
        {"id": ids["folder"], "name": "📂 repo", "note": f"Root: {repo}",
         "parent_id": ids["vault"]},
        {"id": ids["file"], "name": "🐍 source.py", "note": "Path: source.py",
         "parent_id": ids["folder"]},
        {"id": ids["symbol"], "name": "ƒ existing()", "note": "AST_QUALNAME: existing",
         "parent_id": ids["file"]},
    ]
    assert_endpoint(SUITE_ENDPOINT)
    client = nexus.WorkFlowyClientNexus(
        APIConfiguration(api_key=SecretStr("n07-fixture-only"), base_url=SUITE_ENDPOINT),
        write_governor_config=WriteGovernorConfiguration(interval_seconds=0.001),
    )
    publisher, governor = client._cache_view_publisher, client._write_governor

    monkeypatch.setattr(locks, "_LOCKS", locks._RegionRegistry())
    for name in ("_carto_inprocess_tasks", "_carto_job_controls", "_carto_operation_jobs",
                 "_carto_final_obligations", "_carto_job_states", "_inprocess_restart_stops"):
        monkeypatch.setattr(server, name, {})
    monkeypatch.setattr(server, "_carto_startup_failed_jobs", MappingProxyType({}))
    for name in ("_carto_active_cache_jobs", "_carto_pending_terminal_cache_jobs"):
        monkeypatch.setattr(server, name, set())
    monkeypatch.setattr(server, "_carto_jobs_quiescent_event", asyncio.Event())
    monkeypatch.setattr(server, "_ensure_carto_final_delivery", lambda: None)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_governance_jobs_root", lambda: jobs)
    monkeypatch.setattr(server, "_get_carto_jobs_base_dir", lambda: str(jobs))
    monkeypatch.setattr(server, "_admission_coherence_paths", client._coherence_paths)
    monkeypatch.setattr(server, "_rate_limiter", None)
    controller = lifecycle.DaemonLifecycle(identity=SimpleNamespace(warnings=()))
    controller.start()
    monkeypatch.setattr(lifecycle, "active_lifecycle", lambda: controller)

    probe = RegionHTTPProbe(nodes, held_regions=locks.held_mutation_lock_regions,
                            reserved_parents=ReservedMutationParent)

    async def transport(request):
        assert_endpoint(str(request.url).split("/api/v1", 1)[0] + "/api/v1")
        if request.method == "GET" and request.url.path.rstrip("/").endswith("nodes-export"):
            return httpx.Response(200, json={"nodes": list(probe.nodes.values())})
        return await probe.request(request)

    client._client = httpx.AsyncClient(base_url=SUITE_ENDPOINT,
                                       transport=httpx.MockTransport(transport))
    tasks, controls = {}, {}

    async def noop(*args, **kwargs):
        pass

    monkeypatch.setattr(server, "_await_nodes_export_cache_quiescent", noop)
    monkeypatch.setattr(server, "_refresh_carto_job_tracker_once", noop)
    schedule = server._schedule_carto_job

    async def scheduled(spec):
        result = await schedule(spec)
        if result.get("success"):
            tasks[spec.job_id] = server._carto_inprocess_tasks[spec.job_id]
            controls[spec.job_id] = server._carto_job_controls[spec.job_id]
        return result

    monkeypatch.setattr(server, "_schedule_carto_job", scheduled)
    env = SimpleNamespace(path=root, repo=repo, jobs=jobs, snapshots=snapshots,
                          artifacts=artifacts, client=client, probe=probe, ids=ids,
                          server=server, controller=controller, tasks=tasks,
                          controls=controls, send_gate=None,
                          publisher=publisher, governor=governor)
    try:
        yield env
    finally:
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks.values()), WAIT)
        await client._client.aclose()


def _fresh_created_inventory(env) -> list[str]:
    return sorted(path.relative_to(env.path).as_posix() for path in env.path.rglob("*"))


def _assert_fresh_install_clean(env) -> None:
    scope = env.client._write_governor.paths.account_scope_id
    governance = _governance_paths(scope, env.jobs)
    retired = _retired_paths(env.jobs, env.client._coherence_paths().account_root,
                             governance.account_dir)
    present = sorted(name for name, path in retired.items() if path.exists())
    assert present == [], f"fresh install created retired artifacts {present}"
    corrupt = [path for path in env.jobs.rglob("*") if ".corrupt." in path.name]
    assert corrupt == [], f"fresh install created governor sidecars {corrupt}"

    offenders = []
    for relative in _fresh_created_inventory(env):
        if _allowed_state_rel(relative, scope):
            continue
        offenders.append(relative)
    assert offenders == [], f"fresh install created unknown persisted paths {offenders}"


def _allowed_state_rel(relative: str, scope: str) -> bool:
    """§6.6 allowed persisted categories; no new state table may appear."""
    parts = relative.split("/")
    if parts[0] == "repo":
        return True  # the fixture's small source tree
    if parts[0] == "artifacts":
        return True  # existing F12 per-file artifact category
    if parts[0] == "snapshots":
        if len(parts) == 1:
            return True
        name = parts[-1]
        return len(parts) == 2 and (
            name.endswith("_nodes_export_snapshot.json") or ".tmp" in name)
    if parts[0] != "jobs":
        return False
    rest = "/".join(parts[1:])
    if rest in ("", ".coherence", ".governance"):
        return len(parts) <= 2
    if rest.startswith(".coherence/"):
        pieces = rest.split("/")
        if len(pieces) == 2:
            return pieces[1] == scope
        if pieces[1] != scope:
            return False
        name = pieces[2] if len(pieces) > 2 else ""
        if len(pieces) == 3:
            return name in {"manifest.json", "external_dirt.json", "writer.lock",
                            "journal", "quarantine"} or ".tmp" in name
        if pieces[2] == "journal":
            return len(pieces) == 4 and (
                (len(pieces[3]) == 25 and pieces[3].endswith(".json")
                 and pieces[3][:20].isdigit()) or ".tmp" in pieces[3])
        if pieces[2] == "quarantine":
            return True
        return ".tmp" in name
    if rest.startswith(".governance/"):
        pieces = rest.split("/")
        if len(pieces) == 2:
            return pieces[1] == scope
        if pieces[1] != scope or len(pieces) != 3:
            return False
        return pieces[2] in {"write_breaker.json", "write_governor.json"} or ".tmp" in pieces[2]
    head = parts[1]
    if len(parts) == 2:
        return _job_artifact_name(head)
    if len(parts) > 2:
        return _job_artifact_name(head)
    return False


@pytest.mark.asyncio
async def test_fresh_install_map_refresh_creates_no_retired_artifact(
    fresh_job_environment, monkeypatch
):
    env = fresh_job_environment
    server, client, jobs = env.server, env.client, env.jobs
    scope = client._write_governor.paths.account_scope_id

    # Empty isolated home: no job artifact and nothing retired exists.  The
    # client's real warm start may initialize the retained bootstrap manifest,
    # which is exactly the durable category §6.6 keeps.
    assert list(jobs.glob("*.json")) == []
    assert list(jobs.glob("*.log")) == []
    assert not (jobs / ".governance").exists()
    _assert_fresh_install_clean(env)

    # Isolated startup/W12 on the empty home creates no retired artifact.
    await server._mark_abandoned_carto_tasks_at_startup()
    _assert_fresh_install_clean(env)

    # The real full refresh bootstraps the retained lineage from nothing.
    full = await client.refresh_nodes_export_cache(settle_seconds=0)
    assert full["success"] is True
    _assert_fresh_install_clean(env)

    async def selection(**kwargs):
        return await server._carto_select_live_basis(
            client, kwargs.get("disposition_when_performed", "performed"))

    monkeypatch.setattr(server, "_carto_select_snapshot", selection)

    # Public map-refresh route to the real in-process folder job.
    folder_job = await _launch_folder_map(env)
    _assert_fresh_install_clean(env)
    terminal = await _join(env, folder_job)
    assert terminal["status"] in {"completed", "completed_with_warnings"}, terminal
    _assert_fresh_install_clean(env)

    # The produced folder job JSON uses the surviving BASE job shape and
    # carries no owner-incarnation field from the retired identity family.
    # `pid`/`claim_id`/`execution_owner` are retained historical diagnostics,
    # never ownership authority (§6.6).
    produced = [path for path in jobs.glob("*.json") if _job_artifact_name(path.name)]
    assert len(produced) == 1, produced
    produced_payload = _json_file(produced[0])
    assert JOB_KEYS <= set(produced_payload), sorted(JOB_KEYS - set(produced_payload))
    for field in ("pid_create_time", "pid_start_ticks", "pid_boot_id", "launcher_pid", "spawn_started_at",
                  "owner_pid", "owner_kind", "owner_incarnation",
                  "process_identity", "stored_owner_identity"):
        assert field not in produced_payload, f"job JSON persisted owner field {field}"

    # Terminal/final refresh, snapshot GC and the real job GC/history clear.
    final = await client.refresh_nodes_export_cache(settle_seconds=0)
    assert final["success"] is True
    collected = client._gc_nodes_export_snapshots_diagnostic(max_keep=1, min_age_seconds=0)
    assert collected["aborted"] is False
    await server._run_carto_job_gc_pass()
    cleared = await server._clear_carto_job_history(str(jobs))
    assert cleared["success"] is True

    _assert_fresh_install_clean(env)
    governance = _governance_paths(scope, jobs)
    _probe_create_artifact(
        _retired_paths(jobs, client._coherence_paths().account_root, governance.account_dir),
        scope,
    )
    _assert_fresh_install_clean(env)

    # The created-path inventory is retained-lineage/job/artifact only.
    inventory = _fresh_created_inventory(env)
    assert any(path.endswith("manifest.json") for path in inventory)
    assert any("_nodes_export_snapshot.json" in path for path in inventory)


# --------------------------------------------------------------------------- #
# Test 3 — retained persistence shapes, restart floors and no reconstruction
# --------------------------------------------------------------------------- #
def _json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_retained_persistence_paths_shapes_and_restart_are_unchanged(
    real_job_environment, monkeypatch  # noqa: F811
):
    from test_carto_write_governor_a2 import FakeClock
    from test_cut_2b_governor import _reset_schedule

    from workflowy_mcp import mutation_locks as locks
    from workflowy_mcp import server
    from workflowy_mcp.carto_write_governor import (
        CartoWriteGovernor,
        _validate_breaker_state,
        _validate_governor_state,
    )
    from workflowy_mcp.coherence import read_manifest
    from workflowy_mcp.coherence.snapshot_store import (
        read_journal_records,
        read_snapshot_envelope,
    )

    env = real_job_environment
    paths, jobs = env.paths, env.jobs
    scope = paths.account_root.name
    governance = _governance_paths(scope, jobs)

    # Valid retained state: tail, dirt, both governor files and historical jobs.
    seeded = _seed_retained_home(env, scope)
    running = seeded["running"]
    historical = _json_file(running)
    assert JOB_KEYS <= set(historical), sorted(JOB_KEYS - set(historical))
    assert historical["pid"] == 999_999 and historical["owner_pid"] == 999_999

    # Manifest/dirt/journal/snapshot format oracles (BASE literals).
    manifest = read_manifest(paths, required=True)
    manifest_payload = _json_file(paths.manifest_path)
    assert set(manifest_payload) == MANIFEST_KEYS
    assert set(manifest_payload["dirty_status"]) == DIRTY_STATUS_KEYS
    assert set(_json_file(paths.external_dirt_path)) == EXTERNAL_DIRT_KEYS
    assert manifest.base_snapshot_path
    snapshot_payload = _json_file(Path(manifest.base_snapshot_path))
    assert set(snapshot_payload) == SNAPSHOT_KEYS
    assert set(read_snapshot_envelope(manifest.base_snapshot_path).nodes_export) == {"nodes"}
    records = read_journal_records(
        paths, manifest.journal_floor_seq, manifest.journal_head_seq)
    assert len(records) == 1
    transaction = records[0].to_dict()
    assert set(transaction) == JOURNAL_KEYS
    assert set(transaction["producer"]) == PRODUCER_KEYS
    assert set(_json_file(governance.breaker_state)) == BREAKER_KEYS
    assert set(_json_file(governance.governor_state)) == GOVERNOR_KEYS
    assert _validate_breaker_state(_json_file(governance.breaker_state))
    assert _validate_governor_state(_json_file(governance.governor_state))

    # CUT-2A's obsolete manifest receipt key is tolerated on read and never emitted.
    tolerant = dict(manifest_payload)
    tolerant["covered_barrier_required_lease_ids"] = ["retired"]
    from workflowy_mcp.coherence.schema import CoherenceManifest

    accepted = CoherenceManifest.from_dict(tolerant)
    assert "covered_barrier_required_lease_ids" not in accepted.to_dict()

    # Old restart floor: a fresh governor object honors the seeded floor once.
    from test_carto_write_governor_a2 import _advance_sleep

    clock = FakeClock(1000.0)
    _reset_schedule(env.client._write_governor)
    _atomic_json(governance.governor_state, {"next_slot_at": 1075.0})
    restarted = CartoWriteGovernor(account_scope_id=scope, jobs_root=jobs, clock=clock)

    async def advancing(delay, operation):
        return await _advance_sleep(clock, delay, operation)

    first = await restarted.acquire_write_slot("restart", sleep_fn=advancing)
    assert first.slot_at == 1075.0, "restart ignored the retained deferral floor"
    floor_bytes = governance.governor_state.read_bytes()
    assert json.loads(floor_bytes) == {"next_slot_at": 1075.0}
    # New on-disk value is not reloaded into an initialized schedule (read once).
    _atomic_json(governance.governor_state, {"next_slot_at": 1300.0})
    second = await restarted.acquire_write_slot("second", sleep_fn=advancing)
    assert second.slot_at == 1076.0, "initialized schedule reloaded a new floor"
    assert governance.governor_state.read_bytes() == json.dumps(
        {"next_slot_at": 1300.0}, indent=2).encode() + b"\n"
    # A genuinely new restart reads the new floor.
    _reset_schedule(restarted)
    newer = CartoWriteGovernor(account_scope_id=scope, jobs_root=jobs, clock=clock)
    third = await newer.acquire_write_slot("third", sleep_fn=advancing)
    assert third.slot_at == 1300.0, "new restart did not read the new durable floor"
    _reset_schedule(newer)

    # Breaker file restart floor: a seeded OPEN breaker refuses before its
    # blocked_until, then issues the existing probe after the cooldown — with
    # the durable file untouched by either ordinary read.
    from workflowy_mcp.models.errors import AccountWriteBlockedError

    _atomic_json(governance.breaker_state,
                 {**_breaker_payload(clock.value), "state": "open",
                  "blocked_until": clock.value + 900.0, "reason": "seeded wfb"})
    blocked_before = governance.breaker_state.read_bytes()
    blocked = CartoWriteGovernor(account_scope_id=scope, jobs_root=jobs, clock=clock)
    with pytest.raises(AccountWriteBlockedError):
        await blocked.acquire_write_slot("blocked", sleep_fn=advancing)
    assert governance.breaker_state.read_bytes() == blocked_before
    clock.advance(900.0)
    probe_ticket = await blocked.acquire_write_slot("probe", sleep_fn=advancing)
    assert probe_ticket.is_probe, "restart lost the WFB cooldown floor"
    half_open = _json_file(governance.breaker_state)
    assert set(half_open) == BREAKER_KEYS
    assert half_open["state"] == "half_open" and half_open["probe_claim_id"]
    _reset_schedule(blocked)

    # The closed breaker retains its exact bytes through an ordinary read.
    _atomic_json(governance.breaker_state, _breaker_payload(clock.value))
    breaker_before = governance.breaker_state.read_bytes()
    probe_breaker = CartoWriteGovernor(account_scope_id=scope, jobs_root=jobs, clock=clock)
    assert probe_breaker._load_breaker() == json.loads(breaker_before)
    _reset_schedule(probe_breaker)
    assert governance.breaker_state.read_bytes() == breaker_before

    # Startup/W12 presents the nonterminal job as failed but reconstructs no
    # ownership, cooldown, task or admission hold from historical owner fields.
    await server._mark_abandoned_carto_tasks_at_startup()
    marked = _json_file(running)
    assert marked["status"] == "failed"
    assert marked["error_kind"] == "daemon_restarted"
    assert marked["pid"] == 999_999 and marked["owner_pid"] == 999_999
    assert server._carto_startup_failed_jobs["legacy-running"]["status"] == "failed"
    if _enabled("CUT2B_NEUTRALIZE_RECONSTRUCT_HOLD"):
        locks._reserve_carto_hold(
            jobs_root=jobs, account_scope_id=scope,
            footprint={"kind": "file", "path": None, "root_uuid": "a" * 32,
                       "ancestor_chain": ["a" * 32]},
            job_id="legacy-running", job_file=running, job_type="CARTO_REFRESH",
            mode="file", dedupe_key="reconstructed", launcher="system",
        )
        locks._LOCKS.cooldowns[(
            locks._normalized_governance_root(jobs), scope, "reconstructed")] = {
            "last_terminal_at": datetime.now(timezone.utc).isoformat(),
        }
    assert locks._governance_holds(jobs) == (), "ownership reconstructed from historical JSON"
    assert locks._governance_cooldowns(
        jobs, now_epoch=clock.value, cooldown_seconds=60) == (), (
        "cooldown reconstructed from historical JSON")
    assert not server._carto_inprocess_tasks
    assert locks._LOCKS.waiters == []

    # No new persisted state table: every path under the jobs root is a §6.6
    # retained job/log/artifact/snapshot/temp category or a retired absence.
    offenders = [
        relative for relative in _fresh_created_inventory(
            SimpleNamespace(path=jobs))
        if not _allowed_state_rel("jobs/" + relative, scope)
    ]
    assert offenders == [], f"unexpected persisted paths {offenders}"
    present = sorted(name for name in RETIRED_ARTIFACTS
                     if _retired_paths(jobs, paths.account_root,
                                       governance.account_dir)[name].exists())
    assert present == [], f"retained witness created retired artifacts {present}"
    corrupt = [path for path in jobs.rglob("*") if ".corrupt." in path.name]
    assert corrupt == [], f"retained witness created governor sidecars {corrupt}"

    if _enabled("CUT2B_NEUTRALIZE_PERSISTENCE_SHAPE") == "key":
        broken = dict(_json_file(governance.breaker_state))
        broken["blocked_until_epoch"] = broken.pop("blocked_until")
        _atomic_json(governance.breaker_state, broken)
    elif _enabled("CUT2B_NEUTRALIZE_PERSISTENCE_SHAPE") == "path":
        _atomic_json(governance.account_dir / "write_governor_v2.json",
                     {"next_slot_at": 0.0})
    assert set(_json_file(governance.breaker_state)) == BREAKER_KEYS
    assert set(_json_file(governance.governor_state)) == GOVERNOR_KEYS
    assert sorted(p.name for p in governance.account_dir.iterdir()) == [
        "write_breaker.json", "write_governor.json"]
