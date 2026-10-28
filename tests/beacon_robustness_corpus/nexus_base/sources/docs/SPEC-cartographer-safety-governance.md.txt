# SPEC — Cartographer Safety Governance (v3, consolidated)

**Status:** FINAL for implementation. Consolidates the Kimi K3 base specification plus three full audit rounds (Fable, GPT 5.6 "Sol", Kimi) into one implementation-ready document. Do not relitigate the Rulings Record (§16); implementation questions not answered here go back to Dan.

**Written:** 2026-08-21, after the WFB(3) account write-lockout incident (2026-08-20) and the post-mortem that followed.

**Implementation owner:** Kimi K3 (fresh sessions, one or more phases per session).

---

## 0. Context and baseline (already shipped — do not re-do)

The 2026-08-20 incident: an agent loop-fired `beacon.map.refresh` across 4–5 files; ~dozens of detached CARTO workers ran concurrently at an experimental 0.25s write delay (~12 writes/s aggregate); Workflowy locked the account's writes (`403 WFB(3)`); zombie workers kept hammering for ~an hour through a silent legacy fallback with no logging, no kill path, and no visibility.

Already shipped (commits `31ab3dc`, `5613e4f`):

- `APIResponseError` fail-fast on ordinary 4xx (status + ≤1000-char body retained; 401/404/429 keep specialized types; 5xx stays retryable `NetworkError`).
- Permanent API errors and cancellation no longer enter the legacy fallback.
- Cooperative cancellation: workers poll the CARTO job JSON; all core CRUD retry/rate-limit sleeps poll cancellation every 250 ms (`_sleep_with_operation_cancel`).
- Exact-key (`root_uuid`+`mode`) launch dedupe, file-backed and cross-process (scans the shared jobs dir).
- `API_RATE_LIMIT_DELAY` restored to 1.0s (`api_client_core.py:41`), with an in-code comment forbidding lowering it without a cross-process governor.
- 429 retries are bounded (`max_retries`, backoff, cancel-aware sleeps) and raise on exhaustion.
- Regression tests: 403 fail-fast, cancel-during-backoff, duplicate-job admission.

This spec is the guardrail layer on top of that substrate.

---

## 1. Design principles (inviolable)

1. **Deny by default.** Anything unclassifiable is treated as active/running and is never reaped, killed, or bypassed.
2. **Fail loud, never silently degrade.** No hidden fallback paths anywhere in the write pipeline.
3. **Never reap or kill on indeterminate PID state.** `unknown` liveness ≡ alive.
4. **Every refusal is a teaching error** — it names the conflicting job/path, the reason, and what to do next.
5. **One shared launch authority.** Every CARTO_REFRESH worker is launched by the same function, from every call site.
6. **Heartbeat is diagnostic-only.** It never authorizes a reap or a kill; PID identity is the only death authority.
7. **Reads are ungoverned.** WFB(3) blocked writes only; reads kept working during the incident. Cache-refresh coordination (`.refresh_claims`) already exists separately and is untouched.

---

## 2. Architecture overview — four concerns

| Concern | Mechanism | Module |
|---|---|---|
| **Global write pacing** | Account-wide ticketed write governor + WFB(3) circuit breaker, enforced at the six write sites through one send authority | `carto_write_governor.py` (new, dependency-light) |
| **Region ownership** | Path + Workflowy-graph footprint claims with overlap detection, cooldown tombstones, idempotent dedupe | `carto_job_governance.py` (new) |
| **Worker lifecycle control** | Cancellation sentinels, PID-identity liveness, exact-PID force kill, release ordering, startup sweep | `carto_job_governance.py` + edits to `server.py`, `weave_worker.py` |
| **Observability** | mtime heartbeat files + job JSON progress (existing) surfaced to the widget | `weave_worker.py`, `_scan_carto_job_files`, extension client (Phase F) |

Two OS mutexes serialize the two cross-process transaction types (separate because hold-time profiles differ): the **admission mutex** (per job launch) and the **write-slot mutex** (per write ticket issuance). Both are OS-level locks (`msvcrt.locking` on Windows with a retry loop — `LK_LOCK` has a ~10 s built-in cap; `fcntl.flock` on POSIX) and are therefore released by the OS if the holder crashes.

---

## 3. State layout and account scoping

All governance state lives under the per-user runtime dir (resolution: `paths.cartographerJobsDir` in the workflowy_nexus JSON config, else `%LOCALAPPDATA%/workflowy-mcp` fallback — the same base as `_get_carto_jobs_base_dir()`), **namespaced by account scope**:

`account_scope_id = sha256(api_base_url + "\0" + api_key)[:32]` — computed at client init; the raw key is never persisted. This keeps two Workflowy accounts on one machine from pacing or blocking each other, and prevents cross-account UUID/path scope collisions. (Jobs dir and widget stay shared-per-user; they are display surfaces, not safety state.)

```
<runtime>/workflowy-mcp/
  cartographer_jobs/
    .refresh_claims/                 (existing — untouched)
    .refresh_locks/                  NEW — active job claims (see §6)
    .refresh_tombstones/             NEW — cooldown records
    .cancel_requests/                NEW — cancellation sentinels
    .heartbeats/                     NEW — worker heartbeats (Phase F)
  governance/<account_scope_id>/
    admission.mutex                  NEW — OS-locked; admission transactions
    write_slot.mutex                 NEW — OS-locked; ticket issuance
    write_governor.json              NEW — {"next_slot_at": epoch}
    write_breaker.json               NEW — breaker state (see §5.4)
```

**Claim** `.refresh_locks/<key>.<job_id>.lock.json` — `<key>` = full `sha256(normcase(realpath(path or "uuid:"+root_uuid)))` (full 64-hex filename; no truncation):

```json
{
  "claim_id": "hex-uuid4",
  "job_id": "carto-refresh-...",
  "job_type": "CARTO_REFRESH | CARTO_MARKDOWN_GENERATE",
  "account_scope_id": "...",
  "kind": "file | folder",
  "path": "canonical absolute path or null",
  "root_uuid": "...",
  "ancestor_chain": ["root_uuid", "...", "vault-side ancestor uuids"],
  "launcher": "agent | gui | system",
  "parent_job_id": null,
  "launched_at": "iso-utc",
  "launcher_pid": 1234
}
```

**Tombstone** `.refresh_tombstones/<key>.json` — carries the **full footprint** (path, root_uuid, ancestor_chain, kind) so the overlap predicate runs on tombstones exactly as on claims, plus `{"last_terminal_at", "job_id"}`. Write-merge rule: `last_terminal_at = max(existing, now)` so a parent finalizing after its child can never regress the cooldown clock.

**Sentinel** `.cancel_requests/<job_id>.cancel` — existence is the signal; content `{"requested_at", "requested_by"}`.

**Heartbeat** `.heartbeats/<job_id>.json` — static identity content written once at worker start (`{"pid","job_id","claim_id"}`); freshness = **file mtime** via `os.utime` ~1/s. Never rewrite the JSON per tick.

**GC** (extends `_gc_carto_jobs` / `_clear_carto_job_history`): reap expired tombstones, dead-PID claims, sentinels and heartbeats belonging to terminal jobs; deny-by-default for anything unrecognized. Breaker/governor state files persist (tiny, self-describing).

---

## 4. Shared primitives

- `os_lock(path)` — context manager over `msvcrt.locking` (retry loop) / `fcntl.flock`. OS-released on crash.
- **Busy vs. broken contract** (applies to both mutexes): if the mutex is merely held by someone else → **wait** (bounded, cancel-aware, ~10 s — this is the normal case at ~1 acquisition/sec). If the governance path cannot be created/opened/locked after that bounded window → raise the loud error (`WriteGovernorUnavailableError` / `AdmissionUnavailableError`) naming the exact path. Busy never errors; broken always fails closed.
- **Corrupt state ≠ broken path.** A corrupt JSON state file (governor/breaker) is quarantine-renamed and reinitialized under its mutex (self-heal; worst case is a few writes landing closer together). An unwritable directory is broken → fail closed.
- Atomic JSON writes: temp file + `os.replace` (the `merge_write_carto_job_status` precedent).
- **psutil is a new hard dependency** — PID liveness, creation time, and command-line identity in ~20 lines, portable.
- Governor clock: wall-clock epoch persisted; a backward NTP step can admit at most one early write — bounded and accepted (comment, not engineering).

---

## 5. Component: Global Write Governor (`src/workflowy_mcp/carto_write_governor.py`)

Dependency-light by design (os/time/json + config paths only; **no client imports** — it is imported *by* `api_client_core`, so the cancel-aware sleeper is injected, not imported).

### 5.1 The six write sites (verified by grep; reads excluded)

| Operation | Site (api_client_core.py) | Call |
|---|---|---|
| `create_node` | ~1769 | `POST /nodes/` |
| `update_node` | ~1995 | `POST /nodes/{id}` |
| `delete_node` | ~2751 | `DELETE /nodes/{id}` |
| `complete_node` | ~2871 | `POST /nodes/{id}/complete` |
| `uncomplete_node` | ~2957 | `POST /nodes/{id}/uncomplete` |
| `move_node` | ~3062 | `POST /nodes/{id}/move` |

**Reads — never governed:** `get_node` (~2576), `list_nodes` (~2653), `/nodes-export` (`api_client_etch.py:1006`). The `/nodes-export` loop's plain `asyncio.sleep(API_RATE_LIMIT_DELAY)` is separately converted to a cancel-aware sleep (cancellation hygiene, not pacing).

### 5.2 One send authority

Introduce `async _send_workflowy_write(method, url, **kwargs)` in `api_client_core.py`. All six sites route through it (their per-iteration `API_RATE_LIMIT_DELAY` sleep lines are **removed** — the governor replaces the per-process delay; no double-pacing; the constant survives only as the documented pre-governor default in comments/config fallback). Inside the authority:

```
1. await governor.acquire_write_slot(op_label, sleep_fn=self._sleep_with_operation_cancel)
2. re-check breaker (a reservation issued before a trip must not fire)
3. send the request
4. notify outcomes: success → breaker.clear_if_half_open_probe(mine);
   429 → governor.defer_global_writes_until(retry_after); existing bounded retry path unchanged;
   403 WFB(3) → breaker.trip (see 5.4);
   timeout/network error during half-open probe → breaker.reopen (bounded)
```

**Static enforcement:** a registration-style AST-walk test rejects any direct `.post/.delete/.put/.patch` on the httpx client outside `_send_workflowy_write` (AST, not grep, so comments/strings don't false-positive). This is the future-proof backstop — `_handle_response` runs post-flight and cannot be the pre-send guard.

### 5.3 acquire_write_slot

```
if breaker is open:            raise AccountWriteBlockedError(retry_after=blocked_until-now, reason)
if breaker expired and probe unclaimed: try to atomically claim the half-open probe (else keep waiting)
with os_lock(write_slot.mutex):                     # milliseconds
    s = read(write_governor.json); now = time()
    my_slot = max(now, s.get("next_slot_at", 0))
    s["next_slot_at"] = my_slot + INTERVAL           # config, default 1.0 s
    atomic_write(s)
await sleep_fn(max(0, my_slot - time()), op_label)   # injected, cancel-aware
return slot_token (probe_token if half-open claimer)
```

- Crash between ticket and write wastes one slot — safe by construction.
- **Fail closed:** if the governance dir/state cannot be written (per §4 busy/broken), raise `WriteGovernorUnavailableError`; **no write is performed.** There is no fallback to per-process delay — that would recreate the incident's multiplication exactly when the substrate breaks.
- **429 → global defer:** on `RateLimitError`, the server-provided `retry_after` bumps `next_slot_at` account-wide under the write-slot mutex, so *all* processes honor the server's requested pause, not just the unlucky caller. (Companion fix, Phase 0: `RateLimitError.__init__` must actually set `self.retry_after` — today it only lands in `details`, and the CRUD catch blocks read the attribute, so the header is silently ignored.)

### 5.4 Circuit breaker (`write_breaker.json`)

States: `closed → open → half_open → closed/open`. Fields: `state, blocked_until, cooldown_s, probe_claim_id, probe_lease_expires_at, reason, evidence_excerpt`.

- **Trip:** in the 403 branch of the centralized response helper (`api_client_core.py:~1071–1110`), pattern-match the retained body for `WFB(3)` before raising `APIResponseError` → write `open` state. Loud `log_event`; surfaced in job errors and tool error envelopes ("account write-block detected; writes suspended until …").
- **Open:** every `acquire_write_slot` raises `AccountWriteBlockedError` locally — zero network traffic. Initial cooldown 900 s (config).
- **Expiry → half-open:** exactly one process atomically claims the probe (`probe_claim_id` + lease). Every other writer keeps refusing/waiting. Outstanding pre-trip reservations are suppressed by the §5.2 step-2 recheck.
- **Probe outcome:** 2xx → `closed` (state file cleared). WFB(3) again → `open` with doubled cooldown (cap 7200 s). Timeout/network error → `open` with bounded cooldown (a probe that got no answer is not evidence of health). Probe-process death → lease expiry lets another process claim half-open later.

---

## 6. Component: Job admission and claims (`src/workflowy_mcp/carto_job_governance.py`)

### 6.1 Footprint

`compute_footprint(path_or_none, root_uuid, cache_by_id)`: canonical normalized real path (or `None` for the temp regime) + `root_uuid` + **ancestor chain captured at claim time** from the /nodes-export cache. **Fail closed** if the chain cannot be resolved (refusal kind `footprint_unresolvable`, teaching text: run `system.cache.refresh` and retry) — consistent with the anchor-dock root-guard precedent; silently degrading to path-only would reopen the Markdown-in-Markdown hole.

### 6.2 Overlap predicate

Two footprints overlap iff **either**:
- both paths present and one is a path-prefix of the other (case-normalized `os.path.commonpath`, Windows-aware), **or**
- either `root_uuid` appears in the other's `ancestor_chain ∪ {root_uuid}`.

The disk check catches same-repo collisions; the graph check catches the Markdown-in-Markdown/temp-file case where a throwaway path hides a Workflowy-subtree collision. Non-overlapping targets run freely in parallel (the 4-repo routine; writes are globally paced by §5).

### 6.3 Admission algorithm (per launch)

Footprint is computed **before** the mutex (cache reads need no lock). Then:

```
with os_lock(admission.mutex):
    sweep: for each active claim → resolve its job JSON →
           PID dead, OR (no job JSON / pidless) AND claim age > start_grace (30 s)
               → persist job status=failed ("worker process vanished (pid N)")
                 via merge_write_carto_job_status, delete claim (claim_id re-read)
           liveness unknown → leave untouched (deny-by-default)
    decision table:
      same target & ACTIVE claim        → DedupeHit(existing job)   # idempotent UX preserved
      overlap with ACTIVE claim          → REFUSE overlap_conflict
          EXCEPT conflicting claim.job_id == incoming parent_job_id
                                   → ADMIT as co-claim (see 6.4)
      overlap with any live TOMBSTONE & now - last_terminal_at < 60 s
          → REFUSE cooldown_active(retry_after=…)
          EXCEPT incoming parent_job_id is set (internal continuation) → bypass tombstone only
    create claim file (claim_id = uuid4 hex)
create job JSON (carries claim_id, footprint, account_scope_id, launcher, parent_job_id)
spawn worker (worker self-registers pid + creation time at startup)
on any post-claim failure → release claim (mutex + claim_id match); mark job failed if JSON exists
```

Notes:
- **Dedupe vs. cooldown are distinct:** active ⇒ dedupe-return; recently finished ⇒ cooldown refusal. Never conflate.
- Under co-claims (two live claims on one key), an exact-target dedupe returns the **child** (the non-finalizing job).
- Same path, different mode (file vs folder) ⇒ treated as overlap ⇒ refuse (near-moot: file and folder targets can't share a normalized path; containment covers the real cases).
- Tombstone check scans **all** live tombstones with the overlap predicate, not just the exact key (a repo-folder refresh finished 20 s ago cools down a file-inside-it launch).

### 6.4 Co-claims (MARKDOWN_GENERATE → phase-8 post-flight refresh)

The F12+3 worker holds a **path-keyed claim for its entire lifetime** (admitted at `_start_carto_markdown_generate_job`, server.py:9936). Its phase-8 post-flight CARTO_REFRESH launches with `parent_job_id` set; admission admits it as a **second claim on the same footprint** via the exception clause above. No ownership transfer, no rollback state machine:

- Parent `finally` releases the parent's claim (token-verified); child owns its own lifecycle.
- Parent crash → liveness reaps the parent's claim; the child's persists.
- Third parties are refused throughout by *either* claim — zero unclaimed gap.
- Cooldown for the logical chain measures from the child's terminal release (tombstone max-merge, §3).
- `parent_job_id` is minted only inside the worker pipeline — **never** accepted on any MCP tool schema.

### 6.5 The one launch authority

`launch_carto_refresh_job(root_uuid, mode, extra_job_fields, launcher, parent_job_id=None)` lives in `carto_job_governance.py` and is the **only** code that spawns a CARTO_REFRESH worker:

- `server.py:_start_carto_refresh_job` (~9703) becomes a thin wrapper preserving its return contract for all 10 existing call sites (agent `map.refresh` S08 + legacy alias, GUI F12+1 file/folder, F12+4 per-leaf, F12+5).
- `markdown_generate_pipeline.py:_launch_post_refresh_subprocess` (~830/993) is **deleted**; the pipeline calls the authority.
- Bulk F12+4 leaves claim **real paths** (filenames are assigned collision-safely before launch, so they are known at claim time), with graph chains — a repo-folder refresh overlapping an in-flight explode is refused from both directions.
- Temp regime (F12+5 / F12+4-temp, `source_path_override`): `path=None`, uuid-keyed claim, graph footprint only.

---

## 7. Component: Worker lifecycle control

### 7.1 Liveness truth layer

- `_scan_carto_job_files` gains **view-level** liveness classification for all detached `CARTO_*` job types (CARTO_REFRESH and CARTO_MARKDOWN_GENERATE; CARTO_BULK_APPLY keeps its existing in-process heuristic): non-terminal status + recorded PID dead ⇒ presented as failed/orphaned in the returned view (widget, quiescence, status truthful immediately). The scanner stays **read-oriented** — persistence happens in the `_carto_jobs_watcher_loop` and the Phase-D startup sweep (the existing BULK_APPLY view-only precedent).
- Persistence path writes `status="failed"` with `"worker process vanished (pid N)"` via `merge_write_carto_job_status` (sticky terminal) — which automatically unblocks quiescence, un-wedges admission, and makes the job GC-eligible by age.
- Grace window: a claim younger than `start_grace` (30 s) whose job JSON is missing or pidless is treated as starting (claim-to-spawn gap is milliseconds; deny-by-default covers the rest).
- Unknown liveness ⇒ alive ⇒ never reaped, never killed.

### 7.2 Cancellation sentinels (authority) + status (presentation)

- `system.job.cancel` and the WS `carto_cancel_job` handler: (1) atomically create `.cancel_requests/<job_id>.cancel` — the cancellation **authority** (a progress/finalize write can never erase a separate file, closing the residual read-modify-write TOCTOU that `merge_write` alone narrows but doesn't eliminate); (2) write `status="cancelled"` via merge_write for presentation.
- Worker cancel checks (`_carto_should_cancel`, `_carto_should_cancel_file`, the `reconcile_tree` loop, and the 250 ms CRUD-sleep polling) now test **sentinel existence** (or `status=="cancelled"`, transitional). Poll cadence is unchanged — already ≤ ~1.25 s worst-case inside a single large file's apply phase; optionally drop the F12 caller's `cancel_check_interval` from 10 → 1 (cheap local check).

### 7.3 Escalation to exact-PID force kill

After the sentinel + status write: poll ≤ 5 s for terminal status or PID death (cooperative path — worker polls at ≤1.25 s in apply phases). Still alive → verify exact PID identity with psutil: creation time matches **and** `--carto-job-file <this job file>` appears in the process command line (protects against PID recycling onto a *different* weave_worker) → kill the **single PID** (Windows `taskkill /PID <pid>` **without `/T`** — `/T` follows parent chains and could kill the F12+3 phase-8 child; POSIX SIGTERM → 2 s → SIGKILL). Identity indeterminate ⇒ **do not kill** ⇒ outcome `kill_refused_unverified_pid`. Outcomes vocabulary: `cooperative | forced | already_terminal | kill_refused_unverified_pid`.

### 7.4 Release ordering

- Normal finalize: finish all mutation-capable work → persist final progress/status → release claim (token-verified) → write tombstone (chain's true end) → exit.
- Cancellation: the claim is **not** released when the cancel is requested — only after the worker confirms stop (finalize) or PID death is verified post-kill (liveness reap). A new overlapping launch can never start while a cancelled worker is still winding down.
- Handoff chains: parent produces no tombstone; the child's terminal release writes the single tombstone (max-merge).

### 7.5 Startup sweep (`lifespan`)

On MCP server start: scan the jobs dir; liveness-check every non-terminal job; dead ⇒ persist failed ("worker process vanished") + reap its claim. **Never kill live workers** — a live orphaned worker is legitimately finishing and releases its own claim on finalize. Force-kill only ever happens via explicit cancel (§7.3).

---

## 8. Component: Observability — heartbeat (Phase F)

- Daemon thread in `weave_worker.main` (both CARTO_REFRESH and MARKDOWN_GENERATE modes): writes the static identity file once, then `os.utime` ~1 s; stops at finalize. (Thread, not the asyncio loop, so a *wedged event loop still beats* — that asymmetry is the diagnostic.)
- `_scan_carto_job_files` merges `seconds_since_heartbeat` from file **mtime** (`os.stat`, no parse) into active job records; widget displays the triage fields when present (backward-compatible).
- The triage table this enables: heartbeat fresh + progress advancing = working (rate-limit pauses become visible); heartbeat fresh + progress stale = **alive but stalled** (yesterday's exact symptom, finally distinguishable); heartbeat stale + PID dead = orphan → §7.1 reaps.
- Heartbeat staleness alone **never** triggers reap or kill.

---

## 9. Legacy fallback excision (Phase 0)

- Delete `_refresh_file_node_beacons_legacy` (`api_client_nexus.py` ~12424–12850) and its step-9 delegation (~13999). The `except Exception` branch in `refresh_file_node_beacons` now raises a job-failing error carrying the probe metadata + traceback; the worker's existing exception-finalize lands it in the job JSON `result_summary.errors` → widget-visible `failed`. No silent degraded path, ever.
- Delete helpers the legacy path exclusively owned (evaluate the `mode="legacy"` parking-node branch at ~16790 during implementation; leave anything shared). Sweep stale "legacy remains available" comments.
- Explicitly **not** in scope: the legacy 37-tool MCP registration profile (separate future task); unrelated `legacy` vocabulary elsewhere.
- Post-excision semantics (state in the spec and in the error text): an incremental failure now leaves a partially-reconciled tree + a failed job; re-running F12+1 converges (id-keyed reconcile) — the same contract as cancellation today.

---

## 10. Progressive disclosure (Phase 0 text)

**`beacon` family top-level description** (zero-disclosure string) appends:

> ⚠️ DANGER — `map.refresh` launches a detached, bulk Workflowy-rewriting job whose write volume multiplies (one inserted symbol ≈ up to N sequential move calls). Over-use HAS CAUSED a full account-level write-lockout (403 WFB(3)). NEVER loop it, NEVER launch overlapping refreshes, and NEVER re-fire because the cache still looks stale — poll `system.job.status` instead (a stale cache is NOT a failed refresh). Prefer ONE whole-repository refresh over many per-file refreshes; non-overlapping repositories MAY run concurrently (all writes are globally paced account-wide), but you rarely need to.

**`map.refresh` describe (index + full):** blast-radius explanation (reorder multiplication); governance semantics (one active job per region incl. graph overlap, 60 s cooldown with `retry_after`, global write pacing, account breaker); the guidance (repo-root intermittently ≫ per-file rapidly; concurrent non-overlapping repos are safe *because* of pacing — but don't parallelize without need); observe via widget / `system.job.status`; cancel via `system.job.cancel`. Admission refusal errors are themselves teaching errors (conflicting job_id + path, or `retry_after`).

Update tool-surface measurement / registration tests for the changed strings.

---

## 11. Error vocabulary (new)

- `AccountWriteBlockedError` (models/errors.py; non-retryable; `retry_after`, `blocked_until`, `reason`).
- `WriteGovernorUnavailableError` (names the inaccessible governance path).
- `AdmissionUnavailableError` (names the inaccessible admission path).
- Admission refusal kinds (structured results, not exceptions, at the launcher): `overlap_conflict` (conflicting job_id + path), `cooldown_active` (`retry_after`), `footprint_unresolvable` (teaching: run `system.cache.refresh`).
- Cancel outcomes: `cooperative | forced | already_terminal | kill_refused_unverified_pid`.

---

## 12. Config additions (workflowy_nexus JSON, beside `paths.cartographerJobsDir`)

`write_governor.interval_seconds` (1.0) · `write_governor.breaker_initial_cooldown_seconds` (900) · `write_governor.breaker_max_cooldown_seconds` (7200) · `admission.cooldown_seconds` (60) · `admission.start_grace_seconds` (30) · `governance.mutex_wait_seconds` (10).

---

## 13. Phase plan (session-sized; each phase ships green; full 1400+ suite is the gate)

| Phase | Contents | Exit criteria |
|---|---|---|
| **0 — Immediate hygiene** | Legacy excision (§9) + progressive-disclosure warnings (§10) + `RateLimitError.retry_after` attribute fix + registration test updates. Zero substrate dependencies — protects the interim **today**. | Excision test (single failed modern attempt, no fallback); warning-text tests; suite green |
| **A1 — Substrate I** | `carto_job_governance.py` skeleton: `os_lock` wrapper, account scoping, claim/tombstone/sentinel schemas + CRUD, footprint + overlap predicate (fail-closed), psutil liveness helpers. **Nothing wired.** | Unit tests green (mutex exclusion across real subprocesses; overlap matrix incl. graph chain and Markdown-in-Markdown shape; footprint fail-closed); suite green |
| **A2 — Substrate II** | `carto_write_governor.py`: ticket algorithm, busy/broken contract, corrupt-state self-heal, breaker state machine primitives, `AccountWriteBlockedError`. **Nothing wired.** | Unit tests green (ticket ordering across 2 real processes; breaker state transitions; self-heal); suite green |
| **B — Governor + breaker live** | `_send_workflowy_write` authority; six write sites rewired (old delay lines removed — no double-pacing); `/nodes-export` cancel-aware sleep; WFB(3) trip in the 403 branch; 429 global defer; config keys; AST enforcement test. **Incident class is impossible after this phase.** | Cross-process pacing test (2 subprocesses × 5 writes, global gaps ≥ interval ± tolerance); cancel-during-wait raises; reads-don't-consume-slots test; breaker trip/half-open-lease/re-trip/reservation-suppression tests; governor-unavailable fail-closed test; suite green |
| **C — Shared launcher + admission** | `launch_carto_refresh_job` extracted; wrapper + pipeline rewired (pipeline's duplicate spawn deleted); admission replaces the exact-key dedupe scan; co-claims; MARKDOWN_GENERATE whole-life claims; bulk-leaf real-path claims; rollback. | Admission race test (folder vs contained file, real subprocesses — exactly one winner); dedupe/cooldown decision-table tests (incl. overlapping tombstones); parent-continuation tests (co-claim admit; tombstone-bypass-only); spawn-failure rollback test; suite green |
| **D — Liveness + cancel escalation** | View-level scan classification (all detached `CARTO_*`); watcher-loop + `lifespan` startup-sweep persistence; sentinels replace JSON-status worker checks; exact-PID kill; grace window; release ordering. | Dead-PID → failed-sticky + un-wedge test; unknown-liveness-never-reaped test; cancel three-outcomes test; sentinel-survives-progress test; self-registration test; suite green |
| **E — Residual docs** | Any describe/docs cleanup not finished in Phase 0; update the Workflowy reference docs for the governance behavior. | Suite green |
| **F — Heartbeat + widget triage** | Daemon heartbeat thread (both worker modes); scan mtime merge; widget triage fields. | Heartbeat freshness/staleness tests; stale-heartbeat-never-reaps test; widget backward-compat; suite green |

Phase A may be split A1/A2 as shown (pre-authorized) so no session rushes the substrate tests. Standing directives every phase: beacon blocks on new functions (curated — entry points and orchestrators, not every helper); **auto-commit + push as two separate commands** after each edit round; the full test suite is the per-phase gate.

---

## 14. Test plan (consolidated)

**Real-subprocess tests** (cross-process semantics are the subject): mutex mutual exclusion · governor global pacing · admission race (folder vs contained file) · sentinel survival across progress/finalize writes · worker self-registration after launcher death.

**In-process tests:** overlap decision table (path containment / graph chain / sibling-allow / cross-repo-allow / exact-dedupe / same-path-diff-mode refuse) · footprint fail-closed · cooldown exact + overlapping tombstones + max-merge · parent continuation (co-claim; tombstone-bypass-only) · claim-token late-release safety · stale-claim reaping under mutex · busy-waits vs broken-fails-closed · corrupt-state self-heal · six-writes-consume-slots / three-reads-don't · cancel-during-governor-wait · 429 global defer + `retry_after` attribute · breaker trip / single half-open probe / probe-lease recovery / re-trip doubling / pre-trip reservation suppression · governor-unavailable fail-closed (no write performed) · liveness classify / persist / never-on-unknown · cancel three outcomes · exact-PID verification refuses a recycled different-job worker · release ordering (claim outlives cancel) · legacy excision (no fallback path executes) · `/nodes-export` cancel-aware sleep · AST write-authority enforcement · heartbeat mtime freshness · heartbeat-stale ≠ reap · account-scope isolation (two scopes don't pace/block each other).

**Every phase:** full existing suite (1400+) green, including the F12 failure-hardening regression tests from `31ab3dc`.

---

## 15. Non-goals (v1 — do not "fix" these later without Dan)

- Durable cross-process job **queue** (refusal + `retry_after` suffices; a queue is a subsystem).
- Process-count cap K (the governor subsumes it; optional later process hygiene only).
- Reorder-storm minimal-move algorithm (the structural blast-radius reduction; separate project).
- Read-path pacing (reads were never blocked; `/nodes-export` has its own existing coordination).
- Legacy MCP tool-surface removal (separate task).
- Browser-side human mutations (F10 marker toggle, manual GUI edits — human tempo, not agent-amplified; by construction outside the governor).
- Per-tool warnings beyond `map.refresh` (the governor makes them unnecessary).

---

## 16. Rulings record (final — do not relitigate)

1. Locks and tombstones are **separate artifacts** (Fable 🔴-1); locks are `O_EXCL`-created per `<key>.<job_id>`.
2. **Admission mutex is the keystone** (Sol #1): the whole scan→check→claim transaction is serialized by one OS-level lock; per-file atomicity alone is insufficient because overlapping targets hash to different filenames.
3. **Co-claims beat claim handoff** (Fable r3, Kimi ratified): parent's phase-8 child gets its own claim via the parent-exception clause; no ownership transfer machinery; zero unclaimed gap.
4. Governor lives at the **client write layer**: exactly six write sites (verified), reads ungoverned (Sol r2 #1).
5. Governor **fails closed** (Sol #2); busy vs. broken is defined (§4); **no** fallback to per-process delay.
6. Breaker has an explicit **closed/open/half_open** state machine with probe lease and pre-send recheck (Sol #4/#6).
7. 429 defers the **global** governor (Sol #5); `RateLimitError.retry_after` attribute bug fixed in Phase 0.
8. Governance state is **account-scoped** via `sha256(base_url+key)` (Sol #7); raw key never persisted; `parent_job_id` never on the MCP tool schema.
9. Warning text **permits governed, non-overlapping parallelism** while forbidding loops/overlap/staleness-re-fire (Sol #8).
10. Heartbeat = write-once + `os.utime`, mtime freshness, **diagnostic-only** (Sol #9).
11. Tombstones carry the **full footprint** and are overlap-scanned; writes max-merge `last_terminal_at` (Sol #10 + Kimi).
12. Lock filenames use the **full sha256** (Sol #11).
13. Explicit **release ordering** (§7.4): claims outlive cancellation requests until finalize/verified death (Sol #12).
14. **Phase 0 ships first** (Fable): legacy excision + warnings + `retry_after` fix have no substrate dependencies and protect the interim.
15. **No durable queue** in v1 (Fable; ratified by Kimi and Sol-by-silence).
16. Real-subprocess tests **scoped** to genuinely cross-process semantics (Fable; ratified).
17. **Two modules, two mutexes** (Kimi): import-cycle constraint (`carto_write_governor` must stay dependency-light) and different hold-time profiles.
18. **MARKDOWN_GENERATE holds a whole-life claim** (Kimi) — Fable's liveness broadening was necessary but not sufficient.
19. **Bulk F12+4 leaves claim real paths** (Kimi) — filenames are assigned before launch.
20. Footprint resolution is **fail-closed** (Kimi) — consistent with the anchor-dock root-guard precedent.
21. **Dedupe ≠ cooldown** decision table (Kimi): active ⇒ dedupe-return; recently finished ⇒ cooldown refusal.
22. **psutil is a hard dependency** (Fable; ratified).
23. **Corrupt state self-heals; broken path fails closed** (Kimi) — quarantine-rename + reinit under mutex vs. loud unavailable error.
24. Write authority is enforced by a **static AST test**, not grep (Sol #6; Fable AST-over-grep).
25. Cooldown is **universal** (agent + GUI); only `parent_job_id` continuations bypass it, and only the tombstone (Sol; over Fable's GUI-bypass proposal, withdrawn).
26. **No `taskkill /T`**; exact-PID identity verification before any kill (Fable 🟡-5 + Sol #7).
27. Scanner stays **view-only**; persistence in watcher loop + startup sweep (Fable 🟡-2, precedent-consistent).
28. Liveness classification covers **all detached `CARTO_*`** types (Fable 🟡-3).
29. Original Q1–Q5: graph footprint dissolves the temp-regime keying question (path=None) · orphan ⇒ `failed` + "worker process vanished (pid N)" · same-path-diff-mode ⇒ refuse · 5 s escalation / 60 s cooldown (configurable) · two modules.
30. `cancel_check_interval` for the F12 reconcile caller may drop 10 → 1 (optional; the 250 ms sleep-polling already bounds latency).

---

*End of specification. Implementation begins at Phase 0 in a fresh session with this file and a fresh beacon slice of the repo.*
