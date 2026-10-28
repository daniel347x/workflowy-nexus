"""Capture the joined witnesses' job records as fixture provenance (JR2 §3.1).

OPT-IN AND TEST-FREE.  Nothing in the suite imports this module; it is loaded
only by an explicit `-p capture_joined_fixtures` when a capture directory is
set, and it adds no scenario of its own.

What it produces
----------------
`<capture dir>/<node id>.capture.json`:

    {"kind": "bulk" | "markdown",
     "captured_from": {"test": "<pytest node id>", "commit": "<sha or null>",
                       "date": "<UTC>", "note": "post-write on-disk records"},
     "writer_fingerprint": {"<path>::<name>": {"base": "<sha>", "live": "<sha>",
                                               "equal": true}},
     "provenance": "base-writers" | "CHANGED-WRITERS" | "UNVERIFIED-WRITERS",
     "records": [ {<decoded record after write 1>}, ... ]}

**Why records, not payloads.**  The funnel merges on-disk state (sticky status,
the 200-warning cap, precedence) and lays the file out; only the file is what the
next reader sees, so only the file is the landed state a `summary-head/2` delta
line must describe.  Identical consecutive records collapse (progress ticks).

**Why the write sequence.**  A bulk record keeps one *folded* row per physical
file, so a `/2` counterpart's delta lines (§2.4/§2.5) need the order in which
the run finalized that file.  The sequence is captured, never invented.
Markdown needs no sequence: one publication row, `set publication`, the
phase-8 rows and `set phase8_envelope` are all in the final record.

**Why the aggregate INPUT rows.**  A record holds the aggregate's COPY of each
row (`aggregate_beacon_outcomes` adds `status`/`changed`/`findings`/
`check_target` defaults), so a record alone cannot say whether a `check_target`
was the job's own key or the aggregate's default.  The plugin therefore observes
`beacon_refresh_support.aggregate_beacon_outcomes` non-mutating (the original
call and result are untouched) and records the real input row lists for the
group-end writes in `aggregate_inputs`; `build_fixtures._job_rows` matches each
captured record to the input list that reproduces it byte for byte.  A capture
frozen before this instrumentation still builds, with a reported derivation
instead.

**Reconstruction, stated honestly.**  The BASE runtime emitted a whole JSON
record at every persist, not JSONL lines.  `build_fixtures.
build_sidecar_from_sequence` is a NORMALIZED RECONSTRUCTION of the lines a
§4-item-1 writer would append, derived from these records (and the aggregate
inputs) and carrying a `sequence` of the call events it stands for.  It is not,
and does not claim to be, a byte-for-byte transcript of a JSONL writer that does
not exist at this base.

**Provenance.**  The lead's G2 work changes reader methods only, so the writer
definitions these witnesses exercise must be byte-identical to the G2 base
(`6f7689b`).  The fingerprint section proves it per run: each named definition's
dedented source segment is hashed live and compared with its base hash.  Any
mismatch is recorded loudly (`CHANGED-WRITERS`) instead of being papered over;
a missing definition is `UNVERIFIED-WRITERS`.

How to run it (the ONLY authorized harness invocation)
------------------------------------------------------
    PYTHONPATH=<repo>/tests/fixtures/carto_job_records \\
    PYTEST_ADDOPTS='-p capture_joined_fixtures' \\
    NEXUS_FIXTURE_CAPTURE_DIR=/mnt/c/tmp/mci-066-jr2/capture \\
    NEXUS_FIXTURE_CAPTURE_COMMIT="$(git rev-parse HEAD)" \\
    env -u WORKFLOWY_API_KEY -u WORKFLOWY_NEXUS_CONFIG -u WORKFLOWY_BROKER_TOKEN_FILE \\
        -u WORKFLOWY_MCP_BEARER -u WORKFLOWY_MCP_HTTP_DIAL_HOST \\
      python3 scripts/run_tests.py --files \\
        'tests/test_beacon_integration_contract.py::test_r4_r7_joined_bulk_keeps_refresh_warning_and_partial_success' \\
        'tests/test_beacon_integration_contract.py::test_r5_r6_one_publication_and_phase8_failure_do_not_erase_disk' \\
        'tests/test_carto_bulk_golden_jr2.py::test_a1_iii_own_group_then_other_group_keeps_refresh_keys' \\
        'tests/test_carto_bulk_golden_jr2.py::test_a1_iv_other_group_then_own_group_cancelled_keeps_earlier_evidence' \\
        'tests/test_carto_bulk_golden_jr2.py::test_a1_v_in_sync_file_hits_the_filter'

(the J-4 test is parametrized; each case writes its own file, and the frozen
fixture source is the `phase8-failure` case -- publication succeeds and the
inline refresh fails, which is the record §3.1 item 8 asks for).

**Frozen sources land beside the fixture they build** (one convention,
`<fixture>/sources/`): the J-3 capture as `bulk_apply/sources/bulk_apply.capture.json`,
the J-4 record as `markdown_generate/sources/markdown_generate.record.json`, and
each A1 witness's capture as `a1_traces/sources/<test>.capture.json`.  Then run
`build_fixtures.py` (which rebuilds every `/2` pair and reports its notes) and
commit the fixture directories.  §3.1 item 8's byte oracle is the *line
sequence*: `build_sidecar_from_sequence` returns `sequence`, the normalized call
events (kind, file, `group-final`/`stop`, reflected receipt count) the built
lines stand for.
"""

from __future__ import annotations

import ast
import copy
import datetime
import hashlib
import json
import os
import textwrap
from pathlib import Path

_TARGETS = (
    "test_r4_r7_joined_bulk_keeps_refresh_warning_and_partial_success",
    "test_r4_actual_physical_files_keep_independent_outcomes",
    "test_carto_bulk_golden_jr2.py::test_a1_iii_own_group_then_other_group_keeps_refresh_keys",
    "test_carto_bulk_golden_jr2.py::test_a1_iv_other_group_then_own_group_cancelled_keeps_earlier_evidence",
    "tests/test_carto_bulk_golden_jr2.py::test_a1_v_in_sync_file_hits_the_filter",
    "test_r5_r6_one_publication_and_phase8_failure_do_not_erase_disk",
)

# sha256 of each definition's dedented source segment at the G2 base
# `6f7689b65ce251d265e3f17f5e0194187d670917` (see the module docstring).
BASE_WRITER_FINGERPRINTS = {
    "src/workflowy_mcp/server.py::_write_carto_job_payload":
        "93d2bd18a4758e2aacc78872553cb54d7fd7ab6fed954783a52a0a81ba2d6577",
    "src/workflowy_mcp/server.py::_write_bulk_job_artifacts":
        "b1f45ccf8d854e06d1ad023858f37a0ae97cd9ea1d77b8e04d0935303f834f27",
    "src/workflowy_mcp/server.py::_bulk_apply_per_file_inner_loop":
        "d6335d7767586bc201778fe485529a102c9966577d932140e075e99b2a33f687",
    "src/workflowy_mcp/server.py::_run_carto_bulk_visible_apply_job":
        "c85846e939dc468d5b3dfc24565b87326a00230615dcfac98d33a7ad1d0eb898",
    "src/workflowy_mcp/config.py::merge_write_carto_job_status":
        "d32bbe1fc376628d4b6d05668154a39e5a496327fe177fd85b93b991cabc993b",
    "src/workflowy_mcp/config.py::carto_job_record_layout":
        "f0eb2add8f019c3d28367cfd5a80cffa9cc9193bfcfab14dfbf37e17146e3dcf",
    "src/workflowy_mcp/markdown_generate_pipeline.py::_write_job_payload":
        "472efa1cb022ed308a082fe11f4270475be3bb6e94142f61b28eb8d8d429e668",
    "src/workflowy_mcp/client/api_client_nexus.py::WorkFlowyClientNexus.update_beacon_from_node":
        "09be415232e75bf2497d8389bb9c592cee4b5d4521b691822256925b3720492a",
    "src/workflowy_mcp/client/api_client_nexus.py::WorkFlowyClientNexus.refresh_file_node_beacons":
        "287b239462d77c7d59c1f81a7042c90d1fb52d16a9d972deb3c90979cc2976c8",
    "src/workflowy_mcp/client/api_client_nexus.py::WorkFlowyClientNexus.refresh_folder_cartographer_sync":
        "e053ef16caca0b7046ce53cb4c324eb4938a0d0018b9530cb67791093a855850",
}

_MODULES = {
    "src/workflowy_mcp/server.py": "workflowy_mcp.server",
    "src/workflowy_mcp/config.py": "workflowy_mcp.config",
    "src/workflowy_mcp/markdown_generate_pipeline.py": "workflowy_mcp.markdown_generate_pipeline",
    "src/workflowy_mcp/client/api_client_nexus.py": "workflowy_mcp.client.api_client_nexus",
}

# ---------------------------------------------------------------------------
# Q1 moved ONE funnel branch forward into G2: `merge_write_carto_job_status` /
# `carto_job_record_layout` now preserve an already-/2 record's layout.  Those
# two definitions therefore differ from the G2 base in this working tree, while
# every other writer the witnesses exercise is unchanged.  To keep the capture's
# provenance truthful, the plugin rebinds BOTH to their base source (embedded
# below, from `git show 6f7689b:src/workflowy_mcp/config.py`) for the capture run
# only, and records the substitution in the capture document.
#
BASE_DEFINITION_SOURCES = {
    'carto_job_record_layout': r'''
def carto_job_record_layout(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload`` re-ordered head-first (see CARTO_JOB_RECORD_LAYOUT)."""
    record: dict[str, Any] = {"record_layout": CARTO_JOB_RECORD_LAYOUT}
    for key in _CARTO_JOB_RECORD_HEAD_KEYS:
        if key in payload:
            record[key] = payload[key]
    for key, value in payload.items():
        if key not in record and key != "result_summary":
            record[key] = value
    if "result_summary" in payload:
        summary = payload["result_summary"]
        if isinstance(summary, dict):
            ordered = {key: value for key, value in summary.items()
                       if key not in CARTO_RESULT_SUMMARY_TAIL_KEYS}
            ordered.update((key, summary[key]) for key in CARTO_RESULT_SUMMARY_TAIL_KEYS if key in summary)
            summary = ordered
        record["result_summary"] = summary
    return record
''',
    'merge_write_carto_job_status': r'''
def merge_write_carto_job_status(job_file: str | os.PathLike[str], payload: dict[str, Any]) -> bool:
    """Best-effort atomic CARTO status merge under the existing data-writer lock.

    Persisted precedence is exact: failed/cancelled are sticky; warnings beat
    completed/non-terminal writes; completed beats non-terminal writes; and
    cancelling beats every non-terminal write. Loop-owned control supplies
    current task truth independently of this presentation transaction.
    Reads only head keys for precedence/warnings. Evidence stays complete in a
    compact tail; only a replace collision enters governance's shared retry.
    Returns False instead of raising, logging a failed write.
    """
    with _CARTO_JOB_STATUS_WRITE_LOCK:
        job_path = Path(job_file)

        # Re-read current on-disk state (best-effort; absent/corrupt => no sticky).
        on_disk: dict[str, Any] | None = None
        try:
            loaded = read_carto_job_record_head(job_path)
            if isinstance(loaded, dict):
                on_disk = loaded
        except Exception:
            on_disk = None

        incoming_status = str(payload.get("status") or "").strip().lower()

        if on_disk is not None:
            disk_status = str(on_disk.get("status") or "").strip().lower()
            selected_status = _select_carto_status(disk_status, incoming_status)
            if selected_status != incoming_status:
                payload = dict(payload)
                payload["status"] = on_disk.get("status")
                incoming_status = selected_status

            # WARNING ACCUMULATION (Dan, 2026): NEVER let a later write DROP
            # warnings that already exist on disk. A caller that does not know
            # about warnings (its payload has no 'warnings' key, or an empty one)
            # must not erase the persisted list -- otherwise a fresh single warning
            # from one phase could be overwritten by a warning-blind finalize from
            # another. We MERGE the on-disk warnings with the incoming ones,
            # de-duplicating structurally-identical entries and capping the total
            # so a pathological run cannot bloat the JSON. This is what makes
            # multiple warnings CONCATENATE (persist) rather than overwrite.
            disk_warnings = on_disk.get("warnings")
            if isinstance(disk_warnings, list) and disk_warnings:
                incoming_warnings = payload.get("warnings")
                if not isinstance(incoming_warnings, list):
                    incoming_warnings = []
                merged: list[Any] = []
                seen_keys: set[str] = set()
                for w in list(disk_warnings) + list(incoming_warnings):
                    try:
                        key = json.dumps(w, sort_keys=True, default=str)
                    except Exception:
                        key = str(w)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    merged.append(w)
                    if len(merged) >= 200:
                        break
                payload = dict(payload)
                payload["warnings"] = merged
                payload["warning_count"] = len(merged)

        # Atomic write: temp file in the same dir + os.replace so readers never
        # observe a partially-written file. Head-first, so the widget's job
        # list reads the summary without decoding the per-file evidence.
        try:
            job_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = job_path.with_name(f"{job_path.name}.{os.getpid()}.{__import__('uuid').uuid4().hex}.tmp")
            with tmp_path.open("w", encoding="utf-8") as jf:
                record = carto_job_record_layout(payload)
                summary = record.get("result_summary")
                tail = ({key: value for key, value in summary.items() if key in CARTO_RESULT_SUMMARY_TAIL_KEYS}
                        if isinstance(summary, dict) else {})
                if tail:
                    head_summary = {key: value for key, value in summary.items() if key not in tail}
                    record["result_summary"] = head_summary
                    # Remove only the last two object closers (summary, record).
                    # dumps with no indent uses CPython's C encoder for the tail;
                    # dump, even without indent, would stream in Python instead.
                    head = json.dumps(record, indent=2).rsplit("}", 2)[0].rstrip()
                    jf.write(head + ("," if head_summary else "") + "\n    ")
                    jf.write(json.dumps(tail, separators=(",", ":"))[1:-1])
                    jf.write("\n  }\n}")
                else:
                    jf.write(json.dumps(record, indent=2))
            # Local import avoids governance's module-level config dependency.
            # Reuse only replacement: keep this writer's temp and no-fsync IO.
            from .carto_job_governance import _replace_with_retry
            _replace_with_retry(tmp_path, job_path)

            return True
        except Exception as exc:
            logging.getLogger(__name__).warning("Could not persist Cartographer job status to %s: %s", job_path, exc)
            try:
                if "tmp_path" in locals() and tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            return False
''',
}


def _substitute_base_definitions():
    """Rebind the two G2-changed funnel definitions to their base source.

    One shared namespace: the base funnel must call the base layout, not the
    working tree's.  Best effort -- a failure leaves the live pair in place and
    the provenance report says so.
    """
    try:
        import workflowy_mcp.carto_job_governance as governance
        from workflowy_mcp import config, server
    except Exception:  # noqa: BLE001
        return []
    namespace = dict(vars(config))
    bound = []
    for name, text in BASE_DEFINITION_SOURCES.items():
        try:
            exec(compile(text, f"<base:{name}>", "exec"), namespace)
        except Exception:  # noqa: BLE001
            return bound
        function = namespace.get(name)
        if not callable(function):
            return bound
        setattr(config, name, function)
        if hasattr(server, name):
            setattr(server, name, function)
        if hasattr(governance, name):
            setattr(governance, name, function)
        bound.append(name)
    return bound


def _one_layout_equivalence():
    """A `/1` payload must lay out identically under the base and live layout."""
    try:
        from workflowy_mcp import config
    except Exception:  # noqa: BLE001
        return None
    payload = {"id": "equivalence", "type": "CARTO_REFRESH", "mode": "folder",
               "status": "running", "progress": {"total_files": 1},
               "result_summary": {"files": [{"file": "a.py", "status": "refreshed"}],
                                 "warnings": [], "findings": []}}
    try:
        namespace = dict(vars(config))
        exec(compile(BASE_DEFINITION_SOURCES["carto_job_record_layout"],
                     "<base:layout>", "exec"), namespace)
        base = namespace["carto_job_record_layout"](payload)
        live = config.carto_job_record_layout(payload)
        return json.dumps(base, sort_keys=True) == json.dumps(live, sort_keys=True)
    except Exception:  # noqa: BLE001
        return None


_state = {"dir": None, "installed": False, "captures": {}, "nodeid": None}

_state.setdefault("substituted", [])
_state.setdefault("pre_substitution", {})


def _observe_aggregate_inputs():
    """Record the REAL rows today's writer hands the aggregate (§4 item 1).

    Non-mutating: the original call and its result are untouched; only a
    deep-copied snapshot of the input rows is kept, so the builder has actual job
    rows (``check_target`` present exactly when the job set it, in the job's own
    field order) instead of an inference from the aggregate's copy.
    """
    try:
        from workflowy_mcp import beacon_refresh_support as support
    except Exception:  # noqa: BLE001
        return
    original = getattr(support, "aggregate_beacon_outcomes", None)
    if original is None or getattr(original, "_fixture_capture", False):
        return

    def wrapper(outcomes, *args, **kwargs):
        snapshot = None
        try:
            snapshot = copy.deepcopy(list(outcomes))
        except Exception:  # noqa: BLE001
            snapshot = None
        result = original(outcomes, *args, **kwargs)
        try:
            if snapshot is not None:
                _capture_aggregate_input(snapshot)
        except Exception:  # noqa: BLE001
            pass
        return result

    wrapper._fixture_capture = True
    support.aggregate_beacon_outcomes = wrapper



# ---------------------------------------------------------------------------
# writer provenance
# ---------------------------------------------------------------------------
def _find(tree, qualname):
    node = tree
    for part in qualname.split("."):
        found = None
        for child in getattr(node, "body", []):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                    and child.name == part:
                found = child
                break
        if found is None:
            return None
        node = found
    return node


def _segment(source: str, node) -> str:
    start = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
    text = "\n".join(source.splitlines()[start - 1:node.end_lineno])
    return textwrap.dedent(text.expandtabs())


def _live_fingerprint(module_name: str, qualname: str):
    import importlib

    module = __import__(name=module_name, fromlist=["_"])
    path = getattr(module, "__file__", None)
    if not path:
        return None
    try:
        source = Path(path).read_text(encoding="utf-8")
        node = _find(ast.parse(source), qualname)
    except (OSError, SyntaxError, UnicodeError):
        return None
    if node is None:
        return None
    return hashlib.sha256(_segment(source, node).encode("utf-8")).hexdigest()


def writer_fingerprint():
    """Compare every writer definition live against its G2-base hash."""
    report = {}
    for key, base in BASE_WRITER_FINGERPRINTS.items():
        path, name = key.split("::", 1)
        try:
            live = _live_fingerprint(_MODULES[path], name)
        except Exception:  # noqa: BLE001 - a capture must never break the run
            live = None
        report[key] = {"base": base, "live": live,
                       "equal": (live is not None and live == base)}
    return report


def provenance_for(report, substituted=(), layout_ok=None):
    """State the capture's honest provenance from the pre-substitution report.

    ``base-writers`` -- every writer definition equals the G2 base.
    ``base-writers (substituted: …)`` -- only the two definitions Q1 moved
    forward into G2 differ, the plugin rebound them to base source for this run,
    and a `/1` layout under both is byte-identical.
    `CHANGED-WRITERS` -- anything else changed.
    """
    if any(entry.get("live") is None for entry in report.values()):
        return "UNVERIFIED-WRITERS"
    differing = [key for key, entry in report.items() if not entry.get("equal")]
    if not differing:
        return "base-writers"
    substituted_names = set(substituted or ())
    unhandled = [key for key in differing if key.split("::")[-1] not in substituted_names]
    if not unhandled and layout_ok is not False:
        return "base-writers (substituted: " + ", ".join(sorted(substituted_names)) + ")"
    return "CHANGED-WRITERS"


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------
def _kind(nodeid):
    if "markdown" in nodeid or "r5_r6" in nodeid:
        return "markdown"
    return "bulk"


def _targeted(nodeid):
    return any(target in nodeid for target in _TARGETS)


def _capture_dir():
    raw = os.environ.get("NEXUS_FIXTURE_CAPTURE_DIR")
    return Path(raw) if raw else None


def _file_slug(nodeid):
    return nodeid.split("::")[-1] if "::" in nodeid else nodeid.replace("/", "_")


def _install():
    if _state["installed"]:
        return
    _state["installed"] = True
    _state["pre_substitution"] = writer_fingerprint()
    _state["substituted"] = _substitute_base_definitions()
    _state["layout_equivalence"] = _one_layout_equivalence()
    try:
        from workflowy_mcp import markdown_generate_pipeline as pipeline
        from workflowy_mcp import server
    except Exception:  # noqa: BLE001
        return
    for owner, name in ((server, "_write_carto_job_payload"),
                        (server, "merge_write_carto_job_status"),
                        (pipeline, "_write_job_payload")):
        original = getattr(owner, name, None)
        if original is None or getattr(original, "_fixture_capture", False):
            continue

        def wrapper(job_file, payload, *args, _original=original, **kwargs):
            result = _original(job_file, payload, *args, **kwargs)
            try:
                if result is not False:
                    _capture_record(job_file)
            except Exception:  # noqa: BLE001
                pass
            return result

        wrapper._fixture_capture = True
        setattr(owner, name, wrapper)
    # The record funnel is also reached through direct imports (governance, the
    # bulk runner); wrapping every holder catches each write exactly once.
    try:
        import workflowy_mcp.carto_job_governance as governance
        from workflowy_mcp import config
    except Exception:  # noqa: BLE001
        return
    for owner in (config, governance):
        original = getattr(owner, "merge_write_carto_job_status", None)
        if original is None or getattr(original, "_fixture_capture", False):
            continue

        def funnel_wrapper(job_file, payload, *args, _original=original, **kwargs):
            result = _original(job_file, payload, *args, **kwargs)
            try:
                if result is not False:
                    _capture_record(job_file)
            except Exception:  # noqa: BLE001
                pass
            return result

        funnel_wrapper._fixture_capture = True
        setattr(owner, "merge_write_carto_job_status", funnel_wrapper)
    _observe_aggregate_inputs()


_captured = {}
_active = [None]
_pending_aggregate = [None]


def _same(left, right):
    """Structural equality of two decoded records (dict/list order included)."""
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _capture_aggregate_input(rows):
    """Hold the aggregate's REAL input rows until the write they produced arrives."""
    if not _active[0]:
        return
    _pending_aggregate[0] = rows


def _flush_pending_aggregate(record):
    """Attach the input rows of the write just captured, at group-end writes only.

    A group-end write (``progress.current_file`` cleared) is where a row
    finalization can happen, so that is where the exact job rows are worth
    freezing; a per-candidate or group-head write is not, and keeping its rows
    would only inflate the document.
    """
    rows = _pending_aggregate[0]
    _pending_aggregate[0] = None
    if rows is None:
        return
    progress = record.get("progress") if isinstance(record, dict) else None
    if isinstance(progress, dict) and progress.get("current_file"):
        return
    document = _captured.get(_active[0])
    if document is None:
        return
    aggregates = document.setdefault("aggregates", [])
    if aggregates and _same(aggregates[-1], rows):
        return
    aggregates.append(rows)


def _capture_record(job_file):
    nodeid = _active[0]
    if not nodeid:
        return
    path = Path(job_file)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    document = _captured.setdefault(nodeid, {"records": [], "paths": []})
    _flush_pending_aggregate(record)
    if str(path) not in document["paths"]:
        document["paths"].append(str(path))
    if document["records"] and _same(document["records"][-1], record):
        return
    document["records"].append(record)


def _final_states(document):
    """Append each recorded job's last on-disk state (the terminal record).

    Intermediate writes can be missed when a write hops into a worker; the job
    file the run leaves behind is the state the detail would serve, so it must
    be in the capture even when the sequence is partial.
    """
    for name in document.get("paths") or ():
        try:
            record = json.loads(Path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not document["records"] or not _same(document["records"][-1], record):
            document["records"].append(record)


def _flush(nodeid):
    directory = _state["dir"]
    document = _captured.get(nodeid)
    if not directory or not document:
        return
    _final_states(document)
    if not document["records"]:
        return
    fingerprint = _state.get("pre_substitution") or {}
    substituted = _state.get("substituted") or []
    layout_ok = _state.get("layout_equivalence")
    payload = {
        "kind": _kind(nodeid),
        "captured_from": {
            "test": nodeid,
            "commit": os.environ.get("NEXUS_FIXTURE_CAPTURE_COMMIT") or None,
            "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "note": "post-write on-disk records from the joined harness at the G2 base",
        },
        "writer_fingerprint": fingerprint,
        "substituted": substituted,
        "layout_equivalence_ul1": layout_ok,
        "provenance": provenance_for(fingerprint, substituted, layout_ok),
        "reconstruction": "post-write records + observed aggregate INPUT rows; the BASE "
                          "runtime emitted whole JSON records, not JSONL lines",
        "aggregate_inputs": document.get("aggregates") or [],
        "records": document["records"],
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_file_slug(nodeid)}.capture.json"
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=True), encoding="utf-8")
    print(f"[capture] {path} kind={payload['kind']} writes={len(document['records'])} "
          f"aggregates={len(payload['aggregate_inputs'])} "
          f"provenance={payload['provenance']}", flush=True)
    _captured.pop(nodeid, None)


# ---------------------------------------------------------------------------
# pytest hooks (active only when NEXUS_FIXTURE_CAPTURE_DIR is set)
# ---------------------------------------------------------------------------
def pytest_configure(config):
    _state["dir"] = _capture_dir()
    if _state["dir"] is not None:
        _state["dir"].mkdir(parents=True, exist_ok=True)


def pytest_runtest_setup(item):
    if _state["dir"] is None or not _targeted(item.nodeid):
        return
    _active[0] = item.nodeid
    _install()


def pytest_runtest_teardown(item, nextitem):
    if _state["dir"] is not None and _active[0] == item.nodeid:
        _flush(item.nodeid)
        _active[0] = None


def pytest_sessionfinish(session, exitstatus):
    for nodeid in list(_captured):
        _flush(nodeid)
