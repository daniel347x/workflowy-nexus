"""S05 §18.8/§14 — transition-mode CREATE coverage warning: emitted without
a hint, suppressed only by a passed covering hint, bounded per callsite,
NEVER authorization by governance-directory scan, and create_node itself
invokes the helper on every production entry before the send path (H5c).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mutation_lease_harness import (
    ACCOUNT_A,
    ACCOUNT_B,
    async_test,
    cache_of,
    make_lease_env,
)

from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp import mutation_lease_governance as mlg
from workflowy_mcp.mutation_lease_governance import (
    warn_create_governance_coverage,
)


@pytest.fixture(autouse=True)
def _reset_warned_keys():
    mlg._COVERAGE_WARNED_KEYS.clear()
    yield
    mlg._COVERAGE_WARNED_KEYS.clear()


class _RecordingLogger:
    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)


_CACHE = cache_of(("vault", None), ("repo", "vault"), ("file", "repo"))


def _hint_for_parent(env, parent: str, account: str | None = None) -> dict:
    return {"account_scope_id": account or env.account, "root_uuid": parent,
            "ancestor_chain": list(gov.compute_footprint(None, parent, _CACHE, kind="file")["ancestor_chain"])}


def test_warning_emitted_without_hint():
    logger = _RecordingLogger()
    emitted = warn_create_governance_coverage(
        operation_label="create_node",
        caller_summary="test.py:1 (caller)",
        account_scope_id=ACCOUNT_A,
        parent_uuid="file",
        logger=logger,
    )
    assert emitted is True
    assert len(logger.warnings) == 1
    assert "TRANSITION COVERAGE WARNING" in logger.warnings[0]
    assert "create_node" in logger.warnings[0]


def test_warning_bounded_once_per_callsite():
    logger = _RecordingLogger()
    first = warn_create_governance_coverage(
        operation_label="create_node", caller_summary="test.py:2 (caller)", logger=logger
    )
    second = warn_create_governance_coverage(
        operation_label="create_node", caller_summary="test.py:2 (caller)", logger=logger
    )
    third = warn_create_governance_coverage(
        operation_label="create_node", caller_summary="test.py:3 (caller)", logger=logger
    )
    assert (first, second, third) == (True, False, True)
    assert len(logger.warnings) == 2


def test_passed_covering_hint_suppresses(tmp_path):
    env = make_lease_env(tmp_path, ACCOUNT_A)
    logger = _RecordingLogger()
    emitted = warn_create_governance_coverage(
        operation_label="create_node",
        caller_summary="test.py:4 (caller)",
        account_scope_id=env.account,
        parent_uuid="file",
        coverage_hint=_hint_for_parent(env, "file"),
        logger=logger,
    )
    assert emitted is False
    assert logger.warnings == []


def test_mismatched_hint_does_not_suppress(tmp_path):
    env = make_lease_env(tmp_path, ACCOUNT_A)
    logger = _RecordingLogger()
    # Wrong parent.
    emitted_a = warn_create_governance_coverage(
        operation_label="create_node",
        caller_summary="test.py:5 (caller)",
        account_scope_id=env.account,
        parent_uuid="file",
        coverage_hint=_hint_for_parent(env, "repo"),
        logger=logger,
    )
    # Wrong account.
    emitted_b = warn_create_governance_coverage(
        operation_label="create_node",
        caller_summary="test.py:6 (caller)",
        account_scope_id=env.account,
        parent_uuid="file",
        coverage_hint=_hint_for_parent(env, "file", account=ACCOUNT_B),
        logger=logger,
    )
    assert (emitted_a, emitted_b) == (True, True)
    assert len(logger.warnings) == 2


def _write_covering_legacy_claim(env):
    """An inert old on-disk claim that WOULD cover the parent if scanned.

    Written here, byte-shaped like the retired artifact, never through a
    retired production builder. CUT-2B keeps legacy artifacts as ignored data.
    """
    claims = Path(env.jobs_root) / ".refresh_locks"
    claims.mkdir(parents=True, exist_ok=True)
    artifact = claims / ("f" * 64 + ".covering.lock.json")
    artifact.write_text(
        json.dumps(
            {
                "claim_id": "f" * 32,
                "job_id": "covering",
                "job_type": "CARTO_REFRESH",
                "dedupe_key": "c" * 64,
                "account_scope_id": env.account,
                "kind": "file",
                "path": None,
                "root_uuid": "file",
                "ancestor_chain": ["file", "repo", "vault"],
                "launcher": "agent",
                "launcher_pid": 123456,
                "pid": 123456,
                "pid_create_time": 1.0,
                "pid_start_ticks": 1,
                "pid_boot_id": "inert-old-boot",
                "parent_job_id": None,
                "launched_at": "2026-01-01T00:00:00+00:00",
                "handoff_state": "running",
                "spawn_started_at": None,
                "execution_owner": "launcher",
            }
        ),
        encoding="utf-8",
    )
    return artifact


@async_test
async def test_no_governance_directory_scan(tmp_path, monkeypatch):
    """PERMANENT FUNCTIONAL LOCK: plant a legacy claim that WOULD cover the
    parent if a directory scan existed; with NO passed hint the warning must
    STILL fire — the helper never borrows coverage from the directory. A real
    registry hold likewise confers no coverage. Forbidden-read spies prove the
    artifact and the registry are never consulted as authority."""
    env = make_lease_env(tmp_path, ACCOUNT_A)
    artifact = _write_covering_legacy_claim(env)
    forbidden: list[str] = []

    def refuse(name):
        def fail(*args, **kwargs):
            forbidden.append(name)
            raise AssertionError(f"coverage authority performed forbidden {name}")

        return fail

    receipts = await gov.acquire_q3_leases_async(
        [gov.compute_footprint(None, "file", _CACHE, kind="file")],
        account_scope_id=env.account,
        jobs_root=env.jobs_root,
    )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "glob", refuse("glob"))
            patch.setattr(Path, "iterdir", refuse("iterdir"))
            patch.setattr(gov, "_read_json", refuse("read_json"))
            from workflowy_mcp import mutation_locks
            patch.setattr(mutation_locks, "_governance_holds", refuse("registry coverage read"))
            original_open = Path.open
            def guarded_open(path, *args, **kwargs):
                if ".refresh_locks" in path.parts:
                    return refuse("legacy artifact open")(path)
                return original_open(path, *args, **kwargs)
            patch.setattr(Path, "open", guarded_open)
            logger = _RecordingLogger()
            warning = warn_create_governance_coverage
            if __import__("os").environ.get("CUT2B_NEUTRALIZE_COVERAGE_WARNING"):
                warning = lambda **kwargs: False
            elif __import__("os").environ.get("CUT2B_NEUTRALIZE_LEGACY_COVERAGE"):
                def warning(**kwargs):
                    artifact.read_text(encoding="utf-8")
                    return False
            emitted = warning(
                operation_label="create_node",
                caller_summary="test.py:7 (caller)",
                account_scope_id=env.account,
                parent_uuid="file",
                logger=logger,
            )
    finally:
        await gov.release_q3_leases_async(receipts, jobs_root=env.jobs_root)
    if emitted is not True:
        raise AssertionError("coverage warning was suppressed by an inert artifact or registry hold")
    assert forbidden == []
    assert len(logger.warnings) == 1
    assert "TRANSITION COVERAGE WARNING" in logger.warnings[0]
    assert artifact.exists()

def _coverage_scan_offenders(source):
    import ast
    tree = ast.parse(source)
    banned = {"governance_paths", "scan_mutation_leases_locked", "mutation_barrier_required_tokens", "glob"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "warn_create_governance_coverage",
            "_hint_covers_parent",
        }:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id in banned:
                    offenders.append(f"{node.name}:{sub.id}@{sub.lineno}")
                if isinstance(sub, ast.Attribute) and sub.attr in banned:
                    offenders.append(f"{node.name}:.{sub.attr}@{sub.lineno}")
    return offenders


def test_ast_no_directory_scan_in_warning_helper():
    """PERMANENT AST LOCK: no directory scanning in the warning authority."""
    from pathlib import Path

    source = (Path(__file__).parent.parent / "src" / "workflowy_mcp" / "mutation_lease_governance.py").read_text(encoding="utf-8")
    assert _coverage_scan_offenders(source) == []


def test_coverage_witness_sensitivity(tmp_path, monkeypatch):
    assert _coverage_scan_offenders("def _hint_covers_parent():\n    return governance_paths().glob('*')\n")

    def run_witness(path):
        mlg._COVERAGE_WARNED_KEYS.clear()
        with pytest.MonkeyPatch.context() as isolated:
            test_no_governance_directory_scan(path, isolated)

    run_witness(tmp_path / "restored-before")
    # An implementation that borrows on-disk coverage suppresses this exact
    # no-hint warning. Neutralize that one result while the legacy artifact
    # and the real registry hold still exist.
    with monkeypatch.context() as patch:
        patch.setattr(__import__(__name__), "warn_create_governance_coverage", lambda **kw: False)
        with pytest.raises(AssertionError, match="coverage warning was suppressed"):
            run_witness(tmp_path / "neutralized")
    mlg._COVERAGE_WARNED_KEYS.clear()
    run_witness(tmp_path / "restored-after")
    # A mutant that treats the inert legacy artifact as coverage must fail the
    # same intended assertion (the warning is still required to fire).
    warning = warn_create_governance_coverage
    def artifact_as_coverage(**kwargs):
        if kwargs.get("coverage_hint") is None:
            from pathlib import Path
            if any(Path(tmp_path).rglob(".refresh_locks/*.lock.json")):
                return False
        return warning(**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(__import__(__name__), "warn_create_governance_coverage", artifact_as_coverage)
        with pytest.raises(AssertionError, match="coverage warning was suppressed"):
            run_witness(tmp_path / "artifact-neutralized")
    mlg._COVERAGE_WARNED_KEYS.clear()
    run_witness(tmp_path / "restored-final")


@async_test
async def test_create_node_invokes_warning_before_send(tmp_path, monkeypatch):
    """H5c: create_node ITSELF invokes the coverage-warning authority on
    every production entry BEFORE the send path — proven by the hook firing
    before the (stubbed, exploding) transport runs."""
    from pydantic import SecretStr

    from workflowy_mcp.client.api_client_core import WorkFlowyClientCore
    from workflowy_mcp.models import APIConfiguration, NodeCreateRequest

    calls: list[dict] = []

    def recording_warning(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(
        mlg, "warn_create_governance_coverage", recording_warning
    )
    client = WorkFlowyClientCore(
        APIConfiguration(
            api_key=SecretStr("test-key-1234"), base_url="https://workflowy.com/api/v1"
        )
    )

    async def exploding_send(*args, **kwargs):
        raise RuntimeError("transport must not run before the hook assertion")

    monkeypatch.setattr(client, "_send_workflowy_write", exploding_send)
    request = NodeCreateRequest(name="hello", parent_id="parent-1")
    with pytest.raises(RuntimeError, match="transport must not run"):
        await client.create_node(request, _internal_call=True)
    assert len(calls) == 1
    assert calls[0]["operation_label"] == "create_node"
    assert calls[0]["parent_uuid"] == "parent-1"


@async_test
async def test_create_node_hint_kwarg_flows_through(tmp_path, monkeypatch):
    """The internal hint accepted only from trusted internal callers flows
    into the helper verbatim."""
    from pydantic import SecretStr

    from workflowy_mcp.client.api_client_core import WorkFlowyClientCore
    from workflowy_mcp.models import APIConfiguration, NodeCreateRequest

    calls: list[dict] = []
    monkeypatch.setattr(
        mlg,
        "warn_create_governance_coverage",
        lambda **kwargs: calls.append(kwargs) or False,
    )
    client = WorkFlowyClientCore(
        APIConfiguration(
            api_key=SecretStr("test-key-1234"), base_url="https://workflowy.com/api/v1"
        )
    )

    async def exploding_send(*args, **kwargs):
        raise RuntimeError("stop after hook")

    monkeypatch.setattr(client, "_send_workflowy_write", exploding_send)
    hint = {"account_scope_id": ACCOUNT_A, "root_uuid": "parent-1", "ancestor_chain": ["parent-1"]}
    with pytest.raises(RuntimeError, match="stop after hook"):
        await client.create_node(
            NodeCreateRequest(name="hello", parent_id="parent-1"),
            _internal_call=True,
            _governance_coverage_hint=hint,
        )
    assert calls[0]["coverage_hint"] is hint


def test_create_caller_inventory_attached():
    """The current create_node caller inventory (for the S05 result record):
    enumerated here so the record and the inventory can never drift apart.

    Derived from the working tree rather than ``git grep`` so the inventory is
    identical whether or not the tree sits inside a git checkout (the fast
    ext4 test clone ships without ``.git``).
    """
    import pathlib

    repo = pathlib.Path(__file__).resolve().parent.parent
    paths = [repo / "src"]
    paths.extend(sorted(repo.glob("*.py")))
    callers: list[str] = []
    for path in paths:
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for py in files:
            for lineno, line in enumerate(
                py.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if "create_node(" in line:
                    callers.append(f"{py.relative_to(repo)}:{lineno}:{line}")
    assert callers, "create_node caller inventory must be non-empty"
    # N07's decision generator carries data requests only; the same ordered
    # CREATE callback now executes in the async loop-side driver. Enumerate
    # that edge explicitly, rather than exempting a moved implementation.
    reconcile = [row for row in callers if row.startswith(
        "src/workflowy_mcp/client/workflowy_move_reconcile.py:")]
    assert len(reconcile) == 1
    assert "response = await create_node(*request.args, **request.kwargs)" in reconcile[0]
    assert not any(row.startswith("src/workflowy_mcp/client/carto_reconcile_cpu.py:")
                   for row in callers)
