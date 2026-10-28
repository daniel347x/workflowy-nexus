"""Structural tests for daemon-wide debug-log rotation (JR2 / WaveSpec §7.2 R-b).

The two daemon-wide logs -- ``logs/reconcile_debug.log`` and
``logs/etch_debug.log`` -- must rotate at one byte budget into at most five
generations for EVERY writer (the etch log helper, the Cartographic diagnostic
sink behind the per-file artifact log fallback, and the reset-before-first-log
step), while per-weave, per-file and per-job destinations keep their old plain
append. The budget is injected small here; the shipped budget is 10 MiB.
"""

from __future__ import annotations

from pathlib import Path

from workflowy_mcp import debug_log_rotation
from workflowy_mcp.debug_log_rotation import (
    append_debug_line,
    is_daemon_debug_log,
    reset_debug_log,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _daemon_dir(tmp_path: Path) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir()
    return logs


# --------------------------------------------------------------------------
# which paths rotate
# --------------------------------------------------------------------------

def test_only_the_two_daemon_wide_names_rotate(tmp_path: Path) -> None:
    logs = _daemon_dir(tmp_path)
    assert is_daemon_debug_log(logs / "reconcile_debug.log")
    assert is_daemon_debug_log(logs / "etch_debug.log")
    # Another name in the same directory is not a rotating log.
    assert not is_daemon_debug_log(logs / "nexus_debug.log")
    # A per-file sibling inside a source directory keeps its derived name.
    assert not is_daemon_debug_log(logs / "20261027-120000_file_refresh_abc.reconcile_debug.log")
    # Same name, but not the daemon-wide logs directory.
    assert not is_daemon_debug_log(tmp_path / "reconcile_debug.log")
    job_log = tmp_path / "cartographer_jobs" / "job-1" / "file_refresh" / "reconcile_debug.log"
    assert not is_daemon_debug_log(job_log)


# --------------------------------------------------------------------------
# the shared gate
# --------------------------------------------------------------------------

def test_shared_gate_keeps_five_generations_and_drops_the_oldest(tmp_path: Path) -> None:
    log = _daemon_dir(tmp_path) / "reconcile_debug.log"
    for index in range(24):
        append_debug_line(log, f"line-{index:02d}", max_bytes=24, generations=5)

    backups = sorted(p.name for p in log.parent.glob("reconcile_debug.log.*"))
    assert backups == [f"reconcile_debug.log.{n}" for n in range(1, 6)]
    assert not (log.parent / "reconcile_debug.log.6").exists()
    # The current file is the newest lines; generation 1 is the one before it.
    assert "line-23" in log.read_text(encoding="utf-8")
    assert "line-23" not in (log.parent / "reconcile_debug.log.1").read_text(encoding="utf-8")
    assert "line-00" not in (log.parent / "reconcile_debug.log.5").read_text(encoding="utf-8")


def test_newline_is_added_once(tmp_path: Path) -> None:
    log = _daemon_dir(tmp_path) / "etch_debug.log"
    append_debug_line(log, "with newline\n", max_bytes=1024, generations=5)
    append_debug_line(log, "without newline", max_bytes=1024, generations=5)
    assert log.read_text(encoding="utf-8") == "with newline\nwithout newline\n"


def test_reset_empties_current_and_keeps_generations(tmp_path: Path) -> None:
    log = _daemon_dir(tmp_path) / "reconcile_debug.log"
    for index in range(8):
        append_debug_line(log, f"line-{index}", max_bytes=8, generations=5)
    assert log.stat().st_size > 0
    assert (log.parent / "reconcile_debug.log.1").exists()

    reset_debug_log(log)

    assert log.exists()
    assert log.read_text(encoding="utf-8") == ""
    assert (log.parent / "reconcile_debug.log.1").exists()


def test_per_job_destination_never_rotates(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "cartographer_jobs" / "job-1" / "file_refresh" / "reconcile_debug.log"
    log.parent.mkdir(parents=True)
    monkeypatch.setattr(debug_log_rotation, "ROTATION_MAX_BYTES", 16)
    for index in range(20):
        append_debug_line(log, f"job-{index}")

    assert list(log.parent.glob("reconcile_debug.log.*")) == []
    assert log.stat().st_size > 16
    assert log.read_text(encoding="utf-8").count("job-") == 20


# --------------------------------------------------------------------------
# every writer goes through the gate
# --------------------------------------------------------------------------

def test_etch_log_helper_rotates_its_global_destination(tmp_path: Path, monkeypatch) -> None:
    from workflowy_mcp.client import api_client_etch

    logs = _daemon_dir(tmp_path)
    monkeypatch.setattr(api_client_etch, "get_runtime_subdir", lambda name: logs)
    monkeypatch.setattr(debug_log_rotation, "ROTATION_MAX_BYTES", 24)
    for index in range(16):
        api_client_etch._log_to_file_helper(f"etch-{index}", "etch")

    assert (logs / "etch_debug.log.1").exists()
    assert not (logs / "etch_debug.log.6").exists()


def test_file_refresh_destination_never_rotates(tmp_path: Path, monkeypatch) -> None:
    from workflowy_mcp.client import api_client_etch

    folder = tmp_path / "cartographer_file_refresh"
    folder.mkdir()
    json_file = folder / "20261027-120000_file_refresh_abc.json"
    json_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(debug_log_rotation, "ROTATION_MAX_BYTES", 16)
    token = api_client_etch._current_weave_context.set(str(json_file))
    try:
        for index in range(12):
            api_client_etch._log_to_file_helper(f"file-{index}")
    finally:
        api_client_etch._current_weave_context.reset(token)

    target = folder / "20261027-120000_file_refresh_abc.reconcile_debug.log"
    assert target.exists()
    assert list(folder.glob("*.reconcile_debug.log.*")) == []
    assert target.read_text(encoding="utf-8").count("file-") == 12


def test_diagnostic_sink_rotates_a_daemon_wide_path(tmp_path: Path, monkeypatch) -> None:
    from workflowy_mcp.client.carto_reconcile_cpu import CartoDiagnosticSink

    logs = _daemon_dir(tmp_path)
    log = logs / "reconcile_debug.log"
    monkeypatch.setattr(debug_log_rotation, "ROTATION_MAX_BYTES", 24)

    sink = CartoDiagnosticSink(debug_path=log)
    for index in range(16):
        failure = sink.append_batch((("log", f"evt-{index}", "2026-10-27 00:00:00.000"),))
        assert failure is None

    assert (logs / "reconcile_debug.log.1").exists()
    assert log.read_text(encoding="utf-8").startswith("[2026-10-27 00:00:00.000] evt-")


def test_diagnostic_sink_leaves_a_job_path_alone(tmp_path: Path, monkeypatch) -> None:
    from workflowy_mcp.client.carto_reconcile_cpu import CartoDiagnosticSink

    log = tmp_path / "cartographer_jobs" / "job-1" / "file_refresh" / "reconcile_debug.log"
    log.parent.mkdir(parents=True)
    monkeypatch.setattr(debug_log_rotation, "ROTATION_MAX_BYTES", 16)

    sink = CartoDiagnosticSink(debug_path=log)
    for index in range(12):
        assert sink.append_batch((("log", f"evt-{index}", "2026-10-27 00:00:00.000"),)) is None

    assert list(log.parent.glob("reconcile_debug.log.*")) == []
    assert log.stat().st_size > 16


def test_reset_route_still_empties_the_current_file(tmp_path: Path) -> None:
    from workflowy_mcp.client.carto_reconcile_cpu import _reset_reconcile_debug

    log = _daemon_dir(tmp_path) / "reconcile_debug.log"
    log.write_text("stale\n", encoding="utf-8")

    _reset_reconcile_debug(log)

    assert log.exists()
    assert log.read_text(encoding="utf-8") == ""


def test_artifact_log_fallback_delegates_to_the_sink() -> None:
    """The per-file artifact log kind has no append of its own to bypass the gate."""
    source = (REPO_ROOT / "src" / "workflowy_mcp" / "client" / "api_client_nexus.py").read_text(
        encoding="utf-8"
    )
    start = source.index("def _write_carto_file_artifact")
    log_branch = source[start:].split('if kind == "log":', 1)[1].split('if kind == "folder-summary"', 1)[0]
    assert "CartoDiagnosticSink" in log_branch
    assert ".append_batch(" in log_branch
    assert "open(" not in log_branch


def test_writer_routes_preserve_their_original_unconditional_newline(tmp_path):
    """R-b changes retention, not even the per-job diagnostic line bytes (base-green control)."""
    from workflowy_mcp.client import api_client_etch as etch
    from workflowy_mcp.client.carto_reconcile_cpu import CartoDiagnosticSink
    source = tmp_path / "source.json"
    source.write_text("{}")
    target = tmp_path / "reconcile_debug.log"
    assert CartoDiagnosticSink(debug_path=target).append_batch((("log", "line\n", "t"),)) is None
    assert target.read_text() == "[t] line\n\n"
    with etch.weave_log_scope(str(source)):
        etch._log_to_file_helper("again\n")
    assert target.read_text().endswith("again\n\n")


def _startup_debug_statements():
    """The actual import-time writer, isolated from daemon startup for this witness."""
    import ast
    from workflowy_mcp import server
    return next(node for node in ast.parse(Path(server.__file__).read_text()).body
                if isinstance(node, ast.Try) and node.lineno < 40)


def test_import_time_debug_writer_rotates_without_starting_a_daemon(tmp_path, monkeypatch):
    """Execute the real module-body writer only; the base body's plain append is RED."""
    import ast
    import os
    from datetime import datetime
    from workflowy_mcp import server
    logs = tmp_path / "workflowy-mcp" / "logs"
    logs.mkdir(parents=True)
    target = logs / "reconcile_debug.log"
    target.write_text("previous debug output\n")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(debug_log_rotation, "ROTATION_MAX_BYTES", 40)
    code = compile(ast.fix_missing_locations(ast.Module(body=[_startup_debug_statements()], type_ignores=[])),
                   str(server.__file__), "exec")
    exec(code, {"os": os, "datetime": datetime, "__file__": server.__file__,
                "__package__": "workflowy_mcp", "__name__": "workflowy_mcp.server"})
    assert (logs / "reconcile_debug.log.1").read_text() == "previous debug output\n"
    assert "Workflowy MCP Server loaded" in target.read_text()
