"""The twenty-tab daemon witness (WaveSpec §5.2): before and after, in counters.

Twenty simulated Workflowy tabs drive the production admission gate
(``server._carto_list_jobs_poll``) and handler
(``server._handle_carto_list_jobs``) through a controlled clock for sixty
simulated seconds, in two scenarios and on two wires:

* **idle** -- no job at all;
* **running** -- one folder job whose progress changes once per second;

* **legacy** polls (``{"action": "carto_list_jobs"`` with no ``since``) are the
  "before" wire;
* **protocol 2** polls (``since: <last generation>``) are the "after" wire.

The scan and the CPU offload are faked, so this file opens no job record and no
evidence line (the spy asserts it). Every counter is structural -- scans,
replies, UTF-8 bytes -- and never a timing, so the witness cannot rot with a
slower machine.

The same run records the RAW frame text the daemon actually sent, with the
simulated second it was sent at, and compares it deep-equal with the committed
fixture ``tests/js/fixtures/carto_jobs_twenty_tabs/stream.json``. That fixture
is the input of the page-side harness
(``tests/js/test_carto_jobs_twenty_tabs.js``), so a wire change fails here
instead of drifting silently into the page test.

Re-record the fixture only on purpose (it is the ONE write this test makes)::

    CARTO_JOBS_RECORD_STREAM=1 python3 scripts/run_tests.py --files \\
        tests/test_carto_jobs_twenty_tabs_daemon.py

Under the NEXUS runner that write lands in the clone
(``~/.cache/wnmt-tests/…``); copy the file back into the fixture directory and
re-run without the variable, so the committed bytes are proven to be the
daemon's. A missing fixture, or one that no longer equals the daemon's wire,
fails.

Metrics are printed (visible under ``-s``) and, when ``CARTO_JOBS_METRICS_PATH``
names a scratch file, written there as JSON.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from collections import Counter
from pathlib import Path

import pytest

from workflowy_mcp import carto_job_record as rec, server as srv

# One poll per tab per second, five tabs per quarter-second offset.
DURATION_SECONDS = 60
TICKS_PER_SECOND = 4
POLLERS = 20
POLLERS_PER_QUARTER = 5
# The warm-up sits before the measured window: its (full) frame is delivered and
# remembered by every tab before any counter starts.
WARMUP_AT = -0.75
# A protocol-2 "nothing changed" reply must stay tiny (§2.10 list protocol 2).
UNCHANGED_MAX_BYTES = 120

FIXTURE_DIR = Path(__file__).resolve().parent / "js" / "fixtures" / "carto_jobs_twenty_tabs"
FIXTURE_PATH = FIXTURE_DIR / "stream.json"
RECORD_ENV = "CARTO_JOBS_RECORD_STREAM"
METRICS_ENV = "CARTO_JOBS_METRICS_PATH"
ARM_NAMES = {False: "legacy", True: "v2"}
SCENARIOS = ("idle", "running")


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for name in ("_carto_startup_failed_jobs", "_carto_final_obligations", "_carto_inprocess_tasks",
                 "_carto_job_controls", "_jobs"):
        monkeypatch.setattr(srv, name, {})
    monkeypatch.setattr(srv, "_governance_holds", lambda: ())
    monkeypatch.setattr(srv, "_get_carto_jobs_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(srv, "_get_carto_file_refresh_dir", lambda: str(tmp_path / "old-f12"))
    rec._record_cache.clear()
    rec._record_ids.clear()
    rec._detail_cache.clear()


def running_folder_job(completed_files: int) -> dict:
    """One scanner row for the running-folder scenario, as the widget sees it."""
    return {
        "job_id": "job-folder",
        "kind": "CARTO_REFRESH",
        "status": "running",
        "mode": "folder",
        "root_uuid": "root-folder",
        "outcome": "running",
        "has_errors": False,
        "has_warnings": False,
        "has_findings": False,
        "errors_total": 0,
        "findings_total": 0,
        "warning_count": 0,
        "warnings": [],
        "progress": {
            "total_files": 600,
            "completed_files": completed_files,
            "current_phase": "files",
            "nodes_created": 0,
            "nodes_updated": 0,
            "nodes_moved": 0,
            "nodes_deleted": 0,
            "current_file": "/repo/src/module.py",
            "current_file_relpath": "src/module.py",
        },
        "heartbeat": {"applicable": True, "state": "fresh", "age_seconds": 1,
                      "identity_mismatch": False},
        "created_at": "2026-10-27T00:00:00+00:00",
        "updated_at": "2026-10-27T00:00:00+00:00",
        "detached": False,
        "error": None,
        "error_kind": None,
        "pid": 4242,
        # Evidence must never reach the wire; the arm tests assert it does not.
        "result_summary": {"files": ["NEVER_ON_THE_WIRE"]},
    }


class Socket:
    """One tab's websocket: records the RAW text the daemon actually sent."""

    def __init__(self, clock: list[float], frames: list[dict]) -> None:
        self._clock = clock
        self._frames = frames

    async def send(self, text: str) -> None:
        self._frames.append({"at": round(self._clock[0], 6), "text": text})


class Poller:
    """One open tab: polls once a second and remembers the generation it saw."""

    def __init__(self, index: int, clock: list[float], frames: list[dict]) -> None:
        self.index = index
        self.websocket = Socket(clock, frames)
        self.generation = None

    def poll(self, protocol2: bool) -> bool:
        data = {"action": "carto_list_jobs", "since": self.generation} if protocol2 else None
        return srv._carto_list_jobs_poll(self.websocket, data)


def run_arm(monkeypatch, scenario: str, protocol2: bool, base_dir=None) -> dict:
    """Drive one scenario on one wire; return its warm-up, frames, scans and spies."""
    clock: list[float] = [0.0]
    frames: list[dict] = []
    scans: list[float] = []
    widget_views: list[bool] = []
    opened: list[str] = []

    async def scan(base_dir_str=None, *, include_inventory=False, widget_view=False):
        scans.append(round(clock[0], 6))
        widget_views.append(widget_view)
        if scenario == "idle":
            return []
        # Progress changes once per second, whoever asks: 0 during the warm-up,
        # then 1, 2, ... at each whole second of the measured window.
        completed = 0 if clock[0] < 0 else int(clock[0]) + 1
        return [running_folder_job(completed)]

    async def cpu(_phase, function, *args, **kwargs):
        return function(*args, **kwargs)

    tasks: list[asyncio.Task] = []
    monkeypatch.setattr(srv.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(srv.daemon_lifecycle, "active_lifecycle", lambda: None)
    from workflowy_mcp.coherence import operation_cache
    monkeypatch.setattr(operation_cache, "create_isolated_context_task",
                        lambda coro: tasks.append(asyncio.create_task(coro)))
    monkeypatch.setattr(srv, "_scan_carto_job_files_async", scan)
    monkeypatch.setattr(srv, "run_carto_cpu", cpu)
    for name, value in (("_carto_list_jobs_in_flight", False),
                        ("_carto_list_jobs_last_started", float("-inf")),
                        ("_carto_list_jobs_generation", None),
                        ("_carto_list_jobs_full_wanted", False),
                        ("_carto_list_jobs_v2_wanted", False),
                        ("_carto_list_jobs_legacy_wanted", False)):
        monkeypatch.setattr(srv, name, value)

    # No job record and no evidence line may be opened: the scan is faked, and
    # the spy proves the rest of the path stays off disk.
    original_path_open, original_open = Path.open, open

    def path_open(file, *args, **kwargs):
        opened.append(str(file))
        return original_path_open(file, *args, **kwargs)

    def builtin_open(file, *args, **kwargs):
        opened.append(str(file))
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr("builtins.open", builtin_open)

    async def drain() -> None:
        for _ in range(10000):
            if not srv._carto_list_jobs_in_flight:
                return
            await asyncio.sleep(0)
        raise AssertionError("the carto list handler did not finish")

    async def exercise():
        pollers = [Poller(index, clock, frames) for index in range(POLLERS)]
        cursor = 0

        def deliver() -> None:
            """Broadcast the new frames and let every tab remember its generation."""
            nonlocal cursor
            for frame in frames[cursor:]:
                generation = json.loads(frame["text"]).get("generation")
                for poller in pollers:
                    poller.generation = generation
            cursor = len(frames)

        clock[0] = WARMUP_AT
        for poller in pollers:
            poller.poll(protocol2)
        await drain()
        deliver()
        warmup = [frame["text"] for frame in frames[:cursor]]
        measured_from = cursor

        for tick in range(DURATION_SECONDS * TICKS_PER_SECOND):
            clock[0] = tick / TICKS_PER_SECOND
            offset = tick % TICKS_PER_SECOND
            group = pollers[offset * POLLERS_PER_QUARTER:(offset + 1) * POLLERS_PER_QUARTER]
            for poller in group:
                poller.poll(protocol2)
            await drain()
            deliver()
        return warmup, frames[measured_from:]

    warmup, measured = asyncio.run(exercise())
    assert all(widget_view for widget_view in widget_views), "the list scan must stay the widget view"
    return {
        "warmup": warmup,
        "frames": measured,
        # The warm-up scan is outside the measured window.
        "scans": [stamp for stamp in scans if stamp >= 0],
        "opened": opened,
    }


def build_stream(monkeypatch, base_dir=None) -> tuple[dict, dict]:
    """The whole recording: both scenarios, both wires, asserted as it is built."""
    metrics = {"format": 1, "duration_seconds": DURATION_SECONDS, "arms": {}, "totals": {}}
    scenarios = {}
    for scenario in SCENARIOS:
        arms = {}
        for protocol2 in (False, True):
            arm = run_arm(monkeypatch, scenario, protocol2, base_dir)
            assert_structural(arm, scenario, protocol2, base_dir)
            metrics["arms"][f"{scenario}/{ARM_NAMES[protocol2]}"] = arm_metrics(arm)
            arms[ARM_NAMES[protocol2]] = {
                "warmup": arm["warmup"],
                "frames": arm["frames"],
                "scans": arm["scans"],
            }
        scenarios[scenario] = arms
    metrics["totals"] = {
        "scans_total": sum(arm["scans_total"] for arm in metrics["arms"].values()),
        "replies_total": sum(arm["replies_total"] for arm in metrics["arms"].values()),
        "bytes_total": sum(arm["bytes_total"] for arm in metrics["arms"].values()),
        "bytes_max": max(arm["bytes_max"] for arm in metrics["arms"].values()),
    }
    stream = {"format": 1, "duration_seconds": DURATION_SECONDS, "scenarios": scenarios}
    return stream, metrics


def per_second(times) -> Counter:
    return Counter(int(math.floor(stamp)) for stamp in times)


def arm_metrics(arm: dict) -> dict:
    """Structural counters for one arm: nothing here is a wall-clock reading."""
    sizes = [len(frame["text"].encode("utf-8")) for frame in arm["frames"]]
    unchanged = sum(1 for frame in arm["frames"] if json.loads(frame["text"]).get("unchanged"))
    scans_by_second = per_second(arm["scans"])
    replies_by_second = per_second(frame["at"] for frame in arm["frames"])
    return {
        "scans_total": len(arm["scans"]),
        "scans_per_second_max": max(scans_by_second.values(), default=0),
        "scans_per_second_mean": round(len(arm["scans"]) / DURATION_SECONDS, 3),
        "replies_total": len(arm["frames"]),
        "replies_per_second_max": max(replies_by_second.values(), default=0),
        "replies_per_second_mean": round(len(arm["frames"]) / DURATION_SECONDS, 3),
        "full_frames": len(arm["frames"]) - unchanged,
        "unchanged_frames": unchanged,
        "bytes_min": min(sizes, default=0),
        "bytes_max": max(sizes, default=0),
        "bytes_total": sum(sizes),
        "bytes_mean": round(sum(sizes) / len(sizes), 1) if sizes else 0.0,
    }


def report(metrics: dict) -> None:
    for name, values in metrics["arms"].items():
        print(f"METRIC {name}: " + " ".join(f"{key}={value}" for key, value in values.items()))
    print("METRIC total: " + " ".join(f"{key}={value}" for key, value in metrics["totals"].items()))


def write_metrics(metrics: dict) -> None:
    target = os.environ.get(METRICS_ENV)
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, indent=1) + "\n", encoding="utf-8")


def assert_structural(arm: dict, scenario: str, protocol2: bool, base_dir=None) -> None:
    """The §5.2 "after" (and "before") counters, asserted structurally."""
    assert arm["warmup"], "a warm-up frame is always delivered before the window"
    assert len(arm["warmup"]) == 1, arm["warmup"]
    assert len(arm["scans"]) == 2 * DURATION_SECONDS, len(arm["scans"])
    assert max(per_second(arm["scans"]).values()) <= 2, per_second(arm["scans"])
    # One reply per scan, whatever the number of pollers.
    assert len(arm["frames"]) == len(arm["scans"])
    assert max(per_second(frame["at"] for frame in arm["frames"]).values()) <= 2
    assert [frame["at"] for frame in arm["frames"]] == arm["scans"], "a reply lands with its scan"
    values = [json.loads(frame["text"]) for frame in arm["frames"]]
    assert all(value["success"] for value in values)
    assert all("error" not in value for value in values)
    assert all("result_summary" not in frame["text"] and "NEVER_ON_THE_WIRE" not in frame["text"]
               for frame in arm["frames"]), "evidence never reaches the wire"
    assert not [path for path in arm["opened"]
                if Path(path).name in {"files.jsonl", "nodes.jsonl"}], "no evidence may be opened"
    if base_dir is not None:
        assert not [path for path in arm["opened"] if str(path).startswith(str(base_dir))], \
            "the job store is never opened: no record and no sidecar"

    warmup = json.loads(arm["warmup"][0])
    assert "jobs" in warmup, "the warm-up frame is a full frame, never an unchanged one"
    if protocol2:
        assert values[0]["action"] == "carto_list_jobs_result_v2"
        assert all(frame["text"].startswith('{"action":"carto_list_jobs_result_v2"')
                   for frame in arm["frames"])
        changed = [value for value in values if not value.get("unchanged")]
        unchanged = [value for value in values if value.get("unchanged")]
        if scenario == "idle":
            # Nothing changed since the warm-up: the whole measured stream is tiny
            # unchanged frames, so no tab repaints.
            assert not changed, "idle traffic is unchanged frames only"
            assert len(unchanged) == len(values) == 2 * DURATION_SECONDS
            assert warmup["generation"] == unchanged[0]["generation"] == unchanged[-1]["generation"]
        else:
            # Progress changes once per second: one full frame per second, and the
            # intervening scan of the same second is an unchanged frame.
            assert len(changed) == DURATION_SECONDS
            assert len(unchanged) == DURATION_SECONDS
            assert per_second(frame["at"] for frame, value in zip(arm["frames"], values)
                              if not value.get("unchanged")) == Counter(range(DURATION_SECONDS))
            completed = [value["jobs"][0]["progress"]["completed_files"] for value in changed]
            assert completed == list(range(1, DURATION_SECONDS + 1))
            # Each unchanged reply repeats the generation of the full frame before it.
            for previous, value in zip(values, values[1:]):
                if value.get("unchanged"):
                    assert value["generation"] == previous["generation"]
        assert all(len(frame["text"].encode("utf-8")) <= UNCHANGED_MAX_BYTES for frame in arm["frames"]
                   if json.loads(frame["text"]).get("unchanged"))
    else:
        assert all(value["action"] == "carto_list_jobs_result" for value in values)
        assert all("unchanged" not in value for value in values)
        assert all("jobs" in value for value in values)
        if scenario == "idle":
            assert all(value["jobs"] == [] for value in values)
        else:
            assert [value["jobs"][0]["progress"]["completed_files"]
                    for frame, value in zip(arm["frames"], values)
                    if frame["at"] == int(frame["at"])] == list(range(1, DURATION_SECONDS + 1))


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("protocol2", [False, True], ids=["legacy", "v2"])
def test_twenty_pollers_two_scans_per_second_and_a_tiny_unchanged_reply(monkeypatch, tmp_path, scenario, protocol2):
    """Before/after on one wire: ≤2 scans and replies a second, 20 pollers, no evidence."""
    arm = run_arm(monkeypatch, scenario, protocol2, tmp_path)
    assert_structural(arm, scenario, protocol2, tmp_path)
    metrics = arm_metrics(arm)
    assert metrics["scans_per_second_max"] <= 2
    assert metrics["replies_per_second_max"] <= 2
    assert metrics["bytes_min"] > 0
    print(f"METRIC {scenario}/{ARM_NAMES[protocol2]}: "
          + " ".join(f"{key}={value}" for key, value in metrics.items()))


def test_recorded_stream_matches_the_committed_fixture(monkeypatch, tmp_path):
    """The JS harness's input is this daemon's real wire bytes, and cannot drift."""
    stream, metrics = build_stream(monkeypatch, tmp_path)
    report(metrics)
    write_metrics(metrics)

    if os.environ.get(RECORD_ENV) == "1":
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(json.dumps(stream, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    assert FIXTURE_PATH.is_file(), (
        f"missing fixture {FIXTURE_PATH}: record it once with {RECORD_ENV}=1")
    committed = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert committed["format"] == 1
    assert committed["duration_seconds"] == DURATION_SECONDS
    assert set(committed["scenarios"]) == set(SCENARIOS)
    for scenario in SCENARIOS:
        assert set(committed["scenarios"][scenario]) == {"legacy", "v2"}
        for arm in committed["scenarios"][scenario].values():
            assert set(arm) == {"warmup", "frames", "scans"}
            assert isinstance(arm["warmup"], list) and all(isinstance(text, str) for text in arm["warmup"])
            for frame in arm["frames"]:
                assert set(frame) == {"at", "text"} and isinstance(frame["at"], (int, float))
                assert isinstance(frame["text"], str)
            assert all(isinstance(stamp, (int, float)) for stamp in arm["scans"])
    # Raw text, actually sent: it is the wire, not a frame rebuilt from data.
    assert stream == committed, (
        "the daemon's wire bytes drifted from tests/js/fixtures/carto_jobs_twenty_tabs/stream.json; "
        f"re-record with {RECORD_ENV}=1 only if the change is intended")
