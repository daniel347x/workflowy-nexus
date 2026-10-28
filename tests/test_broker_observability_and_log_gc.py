"""Tests for the broker observability snapshot (describe_state) and log GC.

Covers the two observability additions made alongside the Option B endpoint
roster work:

  1. Broker.describe_state() -- the pure in-memory registry snapshot backing
     the workflowy_broker_status MCP tool (browser connections, endpoints with
     liveness age + is_default, MCP clients with is_primary, primary id).
  2. broker_protocol.gc_logs() -- the best-effort startup sweep that prunes
     aged-out per-PID server logs and size-caps the singleton broker log
     (the previously-unbounded log leak).
"""

from __future__ import annotations

import os
import time

import pytest

from workflowy_mcp.broker import Broker
from workflowy_mcp import broker_protocol


class _FakeState:
    def __init__(self, name: str) -> None:
        self.name = name


OPEN = _FakeState("OPEN")
CLOSED = _FakeState("CLOSED")


class FakeWS:
    def __init__(self, state: _FakeState = OPEN) -> None:
        self.state = state


def _new_broker() -> Broker:
    return Broker(required_token="test-token")


# --------------------------------------------------------------------------
# describe_state
# --------------------------------------------------------------------------

def test_describe_state_empty_broker():
    broker = _new_broker()
    snap = broker.describe_state()
    assert snap["browser_connections"] == []
    assert snap["endpoints"] == []
    assert snap["default_endpoint_id"] is None
    assert snap["mcp_clients"] == []
    assert snap["primary_mcp_id"] is None
    assert snap["pending_request_count"] == 0


def test_describe_state_reports_endpoints_connections_and_default():
    broker = _new_broker()
    sw = FakeWS(OPEN)
    broker.register_browser_connection(sw, "chrome-profile-1")
    broker.register_endpoint(sw, "ep-1", "spine", "Spine Tab", "https://workflowy.com/#/a", None)
    broker.register_endpoint(sw, "ep-2", None, None, None, None)
    _status, _ep = broker.set_default_endpoint("ep-2")
    assert _status == "set" and broker.default_endpoint_id == "ep-2"

    snap = broker.describe_state()

    assert len(snap["browser_connections"]) == 1
    conn = snap["browser_connections"][0]
    assert conn["connection_id"] == "chrome-profile-1"
    assert conn["ws_open"] is True
    assert conn["endpoint_count"] == 2

    eps = {e["endpoint_id"]: e for e in snap["endpoints"]}
    assert set(eps) == {"ep-1", "ep-2"}
    assert eps["ep-1"]["alias"] == "spine"
    assert eps["ep-1"]["title"] == "Spine Tab"
    assert eps["ep-1"]["is_default"] is False
    assert eps["ep-2"]["is_default"] is True
    # seconds_since_seen is present and numeric (freshly registered => ~0).
    assert isinstance(eps["ep-1"]["seconds_since_seen"], (int, float))
    assert eps["ep-1"]["seconds_since_seen"] >= 0.0

    assert snap["default_endpoint_id"] == "ep-2"


def test_describe_state_reports_mcp_clients_and_primary():
    broker = _new_broker()
    mcp_a = FakeWS(OPEN)
    mcp_b = FakeWS(OPEN)
    id_a = broker.register_mcp(mcp_a, "mcp-a")
    id_b = broker.register_mcp(mcp_b, "mcp-b")

    snap = broker.describe_state()
    clients = {c["client_id"]: c for c in snap["mcp_clients"]}
    assert set(clients) == {id_a, id_b}
    # First registrant is the primary worker.
    assert clients[id_a]["is_primary"] is True
    assert clients[id_b]["is_primary"] is False
    assert snap["primary_mcp_id"] == id_a
    assert clients[id_a]["ws_open"] is True


def test_describe_state_ws_open_reflects_dead_socket():
    broker = _new_broker()
    sw = FakeWS(OPEN)
    broker.register_browser_connection(sw, "chrome-profile-1")
    broker.register_endpoint(sw, "ep-1", None, None, None, None)
    sw.state = CLOSED
    snap = broker.describe_state()
    assert snap["browser_connections"][0]["ws_open"] is False


def test_describe_state_is_pure_no_mutation():
    """describe_state must not prune/alter the registry (it is a read)."""
    broker = _new_broker()
    sw = FakeWS(CLOSED)  # dead socket, but describe_state must NOT reap it
    broker.register_browser_connection(sw, "chrome-profile-1")
    broker.register_endpoint(sw, "ep-1", None, None, None, None)
    broker.describe_state()
    # Still present -- describe_state did not prune.
    assert broker.endpoint_by_id("ep-1") is not None


# --------------------------------------------------------------------------
# gc_logs
# --------------------------------------------------------------------------

def test_gc_logs_prunes_aged_server_logs_and_keeps_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFLOWY_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("WORKFLOWY_FILE_LOGGING", raising=False)
    monkeypatch.setenv("WORKFLOWY_LOG_RETENTION_DAYS", "7")

    old = tmp_path / "workflowy-mcp-server-11111.log"
    fresh = tmp_path / "workflowy-mcp-server-22222.log"
    unrelated = tmp_path / "something-else.log"
    old.write_text("old", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")

    # Age the old log well past the retention window.
    old_mtime = time.time() - 30 * 86400.0
    os.utime(old, (old_mtime, old_mtime))

    broker_protocol.gc_logs()

    assert not old.exists(), "aged-out server log should be pruned"
    assert fresh.exists(), "recent server log must be preserved"
    assert unrelated.exists(), "non-matching files must never be touched"


def test_gc_logs_size_caps_broker_log(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFLOWY_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("WORKFLOWY_FILE_LOGGING", raising=False)
    monkeypatch.setenv("WORKFLOWY_BROKER_LOG_MAX_BYTES", "100")

    blog = tmp_path / "glimpse-broker.log"
    blog.write_text("x" * 5000, encoding="utf-8")

    broker_protocol.gc_logs()

    assert blog.exists(), "broker log is truncated, not deleted"
    assert blog.stat().st_size == 0, "oversized broker log should be truncated to empty"


def test_gc_logs_leaves_small_broker_log_intact(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFLOWY_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("WORKFLOWY_FILE_LOGGING", raising=False)
    monkeypatch.setenv("WORKFLOWY_BROKER_LOG_MAX_BYTES", str(10 * 1024 * 1024))

    blog = tmp_path / "glimpse-broker.log"
    blog.write_text("small content", encoding="utf-8")

    broker_protocol.gc_logs()
    assert blog.read_text(encoding="utf-8") == "small content"


def test_gc_logs_respects_file_logging_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFLOWY_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("WORKFLOWY_FILE_LOGGING", "0")

    old = tmp_path / "workflowy-mcp-server-11111.log"
    old.write_text("old", encoding="utf-8")
    old_mtime = time.time() - 30 * 86400.0
    os.utime(old, (old_mtime, old_mtime))

    broker_protocol.gc_logs()
    # With file logging disabled, gc_logs is a no-op (nothing to sweep).
    assert old.exists()


def test_gc_logs_never_raises_on_missing_dir(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("WORKFLOWY_LOG_DIR", str(missing))
    monkeypatch.delenv("WORKFLOWY_FILE_LOGGING", raising=False)
    # Must not raise even though the dir is absent.
    broker_protocol.gc_logs()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
