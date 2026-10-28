# SPEC — Cartographer Safety Governance (v4, consolidated)

**Status:** CONSOLIDATED for implementation. Supersedes v3 (`SPEC-cartographer-safety-governance.md` — kept for history). v4 folds in the full post-v3 audit series (Fable rounds, Sol rounds S1–S9 and round-M, Kimi K1–K5). Do not relitigate the Rulings Record (§17); implementation questions not answered here go back to Dan.

**Written:** 2026-08 (audit series following the v3 spec of 2026-08-21).

**Implementation owner:** Kimi K3 (fresh sessions, one or more phases per session), implementing against this document only.

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

1. **Deny by default.** Anything unclassifiable is treated as active/running and is never reaped, killed, or bypassed. An undiscovered edge case degrades to a refusal or a visibly-stuck job in the widget — never to an account lockout.
2. **Fail loud, never silently degrade.** No hidden fallback paths anywhere in the write pipeline.
3. **Never reap or kill on indeterminate PID state.** `unknown` liveness ≡ alive.
4. **Every refusal is a teaching error** — it names the conflicting job/path, the reason, and what to do next.
5. **One shared launch authority.** Every CARTO_REFRESH worker is launched by the same function, from every call site.
6. **Heartbeat is diagnostic-only.** It never authorizes a reap or a kill; PID identity is the only death authority.
7. **Reads are ungoverned.** WFB(3) blocked writes only; reads kept working during the incident. Cache-refresh coordination (`.refresh_claims`) already exists separately and is untouched.
8. **Sentinel = cancellation authority; claim release or PID death = stop authority; job status = presentation only.** These three roles are never conflated.
9. **One exit door.** Every admitted job — success, failure, cancellation, or spawn failure — ends through the same atomic finalize primitive.

---

## 2. Architecture overview — four concerns

| Concern | Mechanism | Module |
|---|---|---|
| **Global write pacing** | Account-scoped ticketed write governor + WFB(3) circuit breaker, enforced at the six write sites through one send authority | `carto_write_governor.py` (new, dependency-light) |
| **Region ownership** | Machine-wide path + Workflowy-graph footprint claims with overlap detection, cooldown tombstones, semantic dedupe | `carto_job_governance.py` (new) |
| **Worker lifecycle control** | Cancellation sentinels, `cancelling` presentation state, PID-identity liveness, exact-PID force kill, atomic `finalize_claim`, startup sweep | `carto_job_governance.py` + edits to `server.py`, `weave_worker.py`, `markdown_generate_pipeline.py` |
| **Observability** | mtime heartbeat files + job JSON progress (existing) surfaced to the widget | `weave_worker.py`, `_scan_carto_job_files`, extension client (Phase F) |

Two OS mutexes serialize the two cross-process transaction types (separate because hold-time profiles and scoping differ):

- **admission.mutex** — *machine-wide* (per runtime dir), held per job launch/finalize/GC transaction.
- **write_slot.mutex** — *account-scoped*, held per write-ticket issuance and every breaker transition.

Both are OS-level locks (`msvcrt.locking` with a retry loop on Windows — `LK_LOCK` has a ~10 s built-in cap; `fcntl.flock` on POSIX) and are therefore released by the OS if the holder crashes. On any async path, lock acquisition uses **non-blocking attempts** (`LK_NBLCK` / `LOCK_NB`) with short cancel-aware `asyncio` sleeps between attempts — a blocking `LK_LOCK` call on the MCP event-loop thread would wedge every WS handler for up to ~10 s. Synchronous detached-worker paths may use a synchronous polling wrapper.

---

## 3. State layout — machine-wide vs account-scoped

All governance state lives under the per-user runtime dir (resolution: `paths.cartographerJobsDir` in the workflowy_nexus JSON config, else `%LOCALAPPDATA%/workflowy-mcp` fallback — the same base as `_get_carto_jobs_base_dir()`).

**The scoping rule (one sentence):** region safety state (claims, tombstones, sentinels, heartbeats, admission mutex) is **machine-wide** because disk paths are machine-global truth; write pacing state (governor, breaker, write-slot mutex) is **account-scoped** because WFB(3) and API quotas are per-account.

`account_scope_id = sha256(api_base_url + "\0" + api_key)[:32]` — computed at client init from credentials the process already holds; the raw key is never persisted. No browser/broker account discovery is needed. If two runtime dirs are deliberately configured differently, they are simply separate coordination domains (accepted, not engineered around).

```
<runtime>/workflowy-mcp/
  cartographer_jobs/                     (machine-wide; jobs + safety state)
    .refresh_claims/                     (existing — untouched)
    .refresh_locks/                      claims (machine-wide)
    .refresh_tombstones/                 cooldown records (machine-wide)
    .cancel_requests/                    cancellation sentinels (machine-wide)
    .heartbeats/                         worker heartbeats (Phase F)
    admission.mutex                      OS-locked; admission/finalize/GC transactions
  governance/<account_scope_id>/
    write_slot.mutex                     OS-locked; ticket issuance + breaker transitions
    write_governor.json                  {"next_slot_at": epoch}
    write_breaker.json                   breaker state (see §5.4)
```

**Claim** `.refresh_locks/<key>.<job_id>.lock.json` — `<key>` = full `sha256(normcase(realpath(path or "uuid:"+root_uuid)))` (full 64-hex filename; no truncation):

```json
{
  "claim_id": "hex-uuid4",
  "job_id": "carto-refresh-...",
  "job_type": "CARTO_REFRESH | CARTO_MARKDOWN_GENERATE | CARTO_BULK_APPLY",
  "dedupe_key": "sha256-of-normalized-launch-intent (see §6.6)",
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

**Tombstone** `.refresh_tombstones/<key>.json` — carries the **full footprint** (path, root_uuid, ancestor_chain, kind, account_scope_id, dedupe_key) so the overlap predicate runs on tombstones exactly as on claims, plus `{"last_terminal_at", "job_id"}`. Write-merge rule: `last_terminal_at = max(existing, now)` so a parent finalizing after its child can never regress the cooldown clock. Tombstone artifacts are keyed so the same region under a different account scope does not collide.

**Sentinel** `.cancel_requests/<job_id>.cancel` — existence is the signal; content `{"requested_at", "requested_by"}`.

**Heartbeat** `.heartbeats/<job_id>.json` — static identity content written once at worker start (`{"pid","job_id","claim_id"}`); freshness = **file mtime** via `os.utime` ~1/s. Never rewrite the JSON per tick.

**GC** (extends `_gc_carto_jobs` / `_clear_carto_job_history`) — see §6.7. Reaps expired tombstones, dead-PID claims, sentinels and heartbeats belonging to terminal jobs with no live claim. Deny-by-default for anything unrecognized. Breaker/governor state files persist (tiny, self-describing).

---

## 4. Shared primitives

- `os_lock(path)` — context manager over `msvcrt.locking` (retry loop) / `fcntl.flock`. OS-released on crash. Async paths: non-blocking attempts + cancel-aware async sleep between attempts (§2).
- **Busy vs. broken contract** (applies to both mutexes): if the mutex is merely held by someone else → **wait** (bounded, cancel-aware, ~10 s — the normal case at ~1 acquisition/sec). If the governance path cannot be created/opened/locked after that bounded window → raise the loud error (`WriteGovernorUnavailableError` / `AdmissionUnavailableError`) naming the exact path. Busy never errors; broken always fails closed.
- **Corrupt state ≠ broken path.** A corrupt JSON state file (governor/breaker) is quarantine-renamed and reinitialized under its mutex (self-heal; worst case is a few writes landing closer together). An unwritable directory is broken → fail closed.
- Atomic JSON writes: temp file + `os.replace` (the `merge_write_carto_job_status` precedent).
- **psutil is a new hard dependency** — PID liveness, creation time, and command-line identity in ~20 lines, portable.
- Governor clock: wall-clock epoch persisted; a backward NTP step can admit at most one early write — bounded and accepted (comment, not engineering).
- **Job-scoped cancellation context.** Detached workers install a client-level cancel callback today; inside the MCP server process many unrelated requests share one client object, so in-process governed jobs (CARTO_BULK_APPLY, F12+4 preparation) must supply a **job/task-local** cancellation probe (e.g. a `contextvars.ContextVar`) to governor slot waits, mutex waits, and retry/backoff sleeps — never a shared-client-global callback. Cancelling one bulk job must not interrupt an unrelated tag edit on the same client.

---

## 5. Component: Global Write Governor (`src/workflowy_mcp/carto_write_governor.py`)

Dependency-light by design (os/time/json + config paths only; **no client imports** — it is imported *by* `api_client_core`, so the cancel-aware sleeper is injected, not imported).

### 5.1 The six write sites (verified by source scan; reads excluded)

| Operation | Site (api_client_core.py) | Call |
|---|---|---|
| `create_node` | ~1769 | `POST /nodes/` |
| `update_node` | ~1995 | `POST /nodes/{id}` |
| `delete_node` | ~2751 | `DELETE /nodes/{id}` |
| `complete_node` | ~2871 | `POST /nodes/{id}/complete` |
| `uncomplete_node` | ~2957 | `POST /nodes/{id}/uncomplete` |
| `move_node` | ~3062 | `POST /nodes/{id}/move` |

Verified by direct regex scan of the repo: these are the only mutating HTTP calls in the client. **Reads — never governed:** `get_node`, `list_nodes`, `/nodes-export` (`api_client_etch.py:1006`); `/nodes-export` is not a direct mutating `.post()` site and needs no AST-test exception. The `/nodes-export` loop's plain `asyncio.sleep(API_RATE_LIMIT_DELAY)` is separately converted to a cancel-aware sleep (cancellation hygiene, not pacing).

Because the governor lives at these six client-layer sites, it automatically governs *everything* — ETCH, `tag.batch`, anchor captures, F12+2 per-node updates — not just CARTO refresh.

### 5.2 One send authority

Introduce `async _send_workflowy_write(method, url, **kwargs)` in `api_client_core.py`. All six sites route through it (their per-iteration `API_RATE_LIMIT_DELAY` sleep lines are **removed** — the governor replaces the per-process delay; no double-pacing; the constant survives only as the documented pre-governor default in comments/config fallback). The authority **sends and returns the raw `httpx.Response`**; `_handle_response` remains the ordinary response-to-exception/data mapper. Before returning, the authority inspects the response status *for governance only*:

```
1. await governor.acquire_write_slot(op_label, sleep_fn=self._sleep_with_operation_cancel)
2. re-check breaker under write_slot.mutex (a reservation issued before a trip must not fire)
3. send the request
4. notify outcomes:
   2xx            → breaker.clear_if_half_open_probe(mine)
   429            → governor.defer_global_writes_until(retry_after);
                    then the EXISTING bounded retry path proceeds unchanged
   403 WFB(3)     → breaker.trip (see 5.4)
   timeout/network during half-open probe → breaker.reopen (bounded)
```

This removes the v3 ambiguity between "governance in the authority" and "governance in `_handle_response`": **all governance outcomes live in the sole send authority.**

- `create_node`'s periodic "still waiting (elapsed …)" diagnostic around its POST must be preserved (behavior, not necessarily the literal `wait_for` code).
- The trailing `GET /nodes/{id}` after create is a read and stays outside the governor.

**Static enforcement:** a registration-style AST-walk test rejects any direct `.post/.delete/.put/.patch` on the httpx client outside `_send_workflowy_write` (AST, not grep, so comments/strings don't false-positive).

### 5.3 acquire_write_slot

The breaker decision and ticket issuance are **one critical section** under `write_slot.mutex`:

```
with os_lock(write_slot.mutex):                       # milliseconds
    b = read(write_breaker.json)
    if b open and not expired:         raise AccountWriteBlockedError(retry_after, reason)
    if b expired and probe unclaimed:  atomically claim the half-open probe
    s = read(write_governor.json); now = time()
    my_slot = max(now, s.get("next_slot_at", 0))
    s["next_slot_at"] = my_slot + INTERVAL             # config, default 1.0 s
    atomic_write(s)
await sleep_fn(max(0, my_slot - time()), op_label)    # injected, cancel-aware
return slot_token (probe_token if half-open claimer)
```

- Two processes can never independently observe an unclaimed half-open state: claim, decision, and ticket are atomic together.
- Crash between ticket and write wastes one slot — safe by construction.
- **Fail closed:** if the governance dir/state cannot be written (per §4 busy/broken), raise `WriteGovernorUnavailableError`; **no write is performed.** There is no fallback to per-process delay — that would recreate the incident's multiplication exactly when the substrate breaks.
- **429 → global defer:** on `RateLimitError`, the server-provided `retry_after` bumps `next_slot_at` account-wide under the write-slot mutex, so *all* processes honor the server's requested pause. (Companion fix, Phase 0: `RateLimitError.__init__` must actually set `self.retry_after` — today it only lands in `details`, and the CRUD catch blocks read the attribute, so the header is silently ignored.)
- **Accepted bounded leakage (do not engineer away):** tickets issued *before* the 429 still fire — but they were issued by the single global governor, so they proceed at ≤ 1 write/interval and cannot recreate the incident's multiplication. No reservation-recall machinery.
- **Probe teaching error:** when the half-open probe fails with WFB(3) again, the error to that caller says "your write was the recovery probe; the account is still blocked until …".

### 5.4 Circuit breaker (`write_breaker.json`)

States: `closed → open → half_open → closed/open`. Fields: `state, blocked_until, cooldown_s, probe_claim_id, probe_lease_expires_at, reason, evidence_excerpt`. **Every breaker read-modify-write transition happens under `write_slot.mutex`** (same lock as ticket issuance; ms hold times; no third lock).

- **Trip:** in the send authority's 403 branch, pattern-match the retained body for `WFB(3)` → write `open` state. Loud `log_event`; surfaced in job errors and tool error envelopes ("account write-block detected; writes suspended until …").
- **Open:** every `acquire_write_slot` raises `AccountWriteBlockedError` locally — zero network traffic. Initial cooldown 900 s (config).
- **Expiry → half-open:** exactly one process atomically claims the probe (`probe_claim_id` + lease) inside the §5.3 critical section. Every other writer keeps refusing/waiting. Outstanding pre-trip reservations are suppressed by the §5.2 step-2 recheck.
- **Probe outcome:** 2xx → `closed`. WFB(3) again → `open` with doubled cooldown (cap 7200 s). Timeout/network error → `open` with bounded cooldown (a probe that got no answer is not evidence of health). Probe-process death → lease expiry lets another process claim half-open later.
- **Accepted bounded race:** a write already in flight — or one that passed its final breaker check immediately before another request trips the breaker — cannot be recalled. The breaker suppresses future sends; it cannot unsend network traffic.
- **Drift tripwire:** any 403-on-write that does *not* match `WFB(3)` is logged at breaker-adjacent loudness, so a body-pattern change by Workflowy is noticed in one incident, not three.
- **`AccountWriteBlockedError` propagation:** a non-retryable sibling error (not a `NetworkError`). The existing CRUD retry loops (which catch `RateLimitError` / `NetworkError` / `httpx.TimeoutException`) must **not** catch or re-wrap it; it propagates out of the loop with zero HTTP calls made. Locked by an explicit CRUD-level test.

---

## 6. Component: Job admission and claims (`src/workflowy_mcp/carto_job_governance.py`)

### 6.1 Footprint

`compute_footprint(path_or_none, root_uuid, cache_by_id)`: canonical normalized real path (or `None` for the temp regime) + `root_uuid` + **ancestor chain captured at claim time** from the /nodes-export cache. **Fail closed** if the chain cannot be resolved (refusal kind `footprint_unresolvable`, teaching text: run `system.cache.refresh` and retry) — consistent with the anchor-dock root-guard precedent. For F12+4's very-fresh-node case, the footprint may be computed against the exact payload-overlaid `nodes_by_id` map the handler already builds (§8).

### 6.2 Overlap predicate (machine-wide paths; account-scoped graph)

Two footprints overlap iff **either**:

- both paths present and one is a path-prefix of the other (case-normalized `os.path.commonpath`, Windows-aware) — compared across **all** claims regardless of account, **or**
- `account_scope_id` matches **and** either `root_uuid` appears in the other's `ancestor_chain ∪ {root_uuid}`.

The disk check catches same-repo collisions (including across accounts, since two accounts writing the same disk path race on real files); the graph check catches the Markdown-in-Markdown/temp-file case, and is same-account-only because a Workflowy node graph is per-account truth. Non-overlapping targets run freely in parallel (the 4-repo routine; writes are globally paced by §5).

### 6.3 Admission algorithm (per launch)

Footprint is computed **before** the mutex (cache reads need no lock). Then:

```
with os_lock(admission.mutex):                       # machine-wide
    sweep: for each active claim → resolve its job JSON →
           PID dead, OR (no job JSON / pidless) AND claim age > start_grace (30 s)
               → persist job status=failed ("worker process vanished (pid N)")
                 via merge_write_carto_job_status, delete claim (claim_id re-read)
           liveness unknown → leave untouched (deny-by-default)
    decision table:
      active claim with IDENTICAL dedupe_key      → DedupeHit(existing job)
      overlap with ACTIVE claim (different intent) → REFUSE overlap_conflict
          EXCEPT validated parent_job_id (see 6.4)
                                   → ADMIT as co-claim
      overlap with any live TOMBSTONE & now - last_terminal_at < 60 s
          → REFUSE cooldown_active(retry_after=…)
          EXCEPT validated parent continuation → bypass tombstone only
    create claim file (claim_id = uuid4 hex, carries dedupe_key + footprint)
create job JSON (carries claim_id, dedupe_key, footprint, account_scope_id, launcher, parent_job_id)
spawn worker (for F12+4: preparation callback runs FIRST, inside the admission
              critical section with the claim held — see §8)
on any post-claim failure → universal finalize (§7.4): terminal status +
              atomic tombstone-and-release
```

Notes:
- **Dedupe vs. cooldown vs. overlap are distinct:** active + same intent ⇒ dedupe-return; active + different intent ⇒ overlap refusal; recently finished ⇒ cooldown refusal. Never conflate.
- **Dedupe key ≠ footprint.** Footprint answers "could these operations collide?"; dedupe key answers "are these the same request?" See §6.6.
- Same path, different mode (file vs folder) ⇒ overlap ⇒ refuse.
- Tombstone check scans **all** live tombstones with the overlap predicate, not just the exact key.
- Under co-claims, dedupe returns the active claim whose `dedupe_key` matches the incoming request (an incoming refresh matches the child refresh; an incoming regenerate matches the parent while it lives).

### 6.4 Co-claims (MARKDOWN_GENERATE → phase-8 post-flight refresh)

The F12+3 worker holds a **path-keyed claim for its entire lifetime**. Its phase-8 post-flight CARTO_REFRESH launches with `parent_job_id` set; admission admits it as a **second claim on the same footprint**. No ownership transfer, no rollback state machine:

- **`parent_job_id` privilege is validated, not nominal:** a continuation is honored only when an *active, same-account, overlapping* parent claim exists whose `job_id == parent_job_id`. Only then does it co-claim and bypass the tombstone.
- Parent `finally` finalizes the parent's claim (token-verified) **without** writing a tombstone; the child owns the chain's cooldown.
- Parent crash → liveness reaps the parent's claim; the child's persists.
- Third parties are refused throughout by *either* claim — zero unclaimed gap.
- `parent_job_id` is minted only inside the worker pipeline — **never** accepted on any MCP tool schema.

### 6.5 The one launch authority

`launch_carto_refresh_job(root_uuid, mode, extra_job_fields, launcher, parent_job_id=None)` lives in `carto_job_governance.py` and is the **only** code that spawns a CARTO_REFRESH worker:

- `server.py:_start_carto_refresh_job` (~9703) becomes a thin wrapper preserving its return contract for all 10 existing call sites.
- `markdown_generate_pipeline.py:_launch_post_refresh_subprocess` (~830/993) is **deleted**; the pipeline calls the authority.
- Bulk F12+4 leaves claim **real paths** (filenames are assigned collision-safely before launch), with graph chains.
- Temp regime (F12+5 / F12+4-temp, `source_path_override`): `path=None`, uuid-keyed claim, graph footprint only.
- **CARTO_BULK_APPLY (standalone F12+2) also takes a region claim** — its canonical FILE/FOLDER path plus the Workflowy root + ancestor chain (it already has both in hand at launch). The claim is acquired before its async runner begins and released in the runner's `finally`. Because it runs **inside the MCP process**: cancellation is cooperative only (§7.3); it is never force-killed; if the server dies, standard PID liveness reaps the claim later.
- **Nested work does not claim:** F12+3's internal bulk-apply phase is already covered by the owning MARKDOWN_GENERATE claim. Rule: *every independently running mutation job owns a region claim; work nested inside a claimed job does not mint another claim unless it becomes an independently surviving child process.*

### 6.6 Semantic dedupe key

`dedupe_key = sha256(account_scope_id, job_type, mode, root_uuid, source regime, canonical source_path_override or "", delete_temp_file_after, ...)`. Two launches on the same footprint with different intents (F12+1 refresh vs F12+2 bulk apply vs F12+3 regenerate vs a temp-regime override refresh) are **overlap conflicts**, not dedupe hits. Only a byte-identical intent dedupes. This replaces the v3 "same target" language and the "always return the child" co-claim note.

### 6.7 Claim-aware GC and history clearing

All deletion or mutation of claims and live tombstones occurs **under the admission mutex**. Additionally:

- A job JSON referenced by a live claim is **never** deleted, regardless of displayed status or age.
- Manual "Clear job history" never deletes a live/unexpired tombstone (a UI cleanup gesture must not bypass cooldown).
- Dead-claim reaping re-reads and verifies `claim_id` under the mutex.
- Tombstone expiry uses `last_terminal_at`, not an earlier unlocked snapshot.
- Sentinel and heartbeat cleanup occurs only when the job is terminal **and** no live claim remains.
- Applies to opportunistic `_gc_carto_jobs`, manual `_clear_carto_job_history`, the startup sweep, and the admission-time sweep. Locked vs already-locked helper variants may exist to avoid recursive acquisition; no new mutex.

---

## 7. Component: Worker lifecycle control

### 7.1 Liveness truth layer

- `_scan_carto_job_files` gains **view-level** liveness classification for all `CARTO_*` job types via psutil (PID existence + creation time). Non-terminal status + recorded PID dead ⇒ presented as failed/orphaned in the returned view. The scanner stays **read-oriented** — persistence happens in the `_carto_jobs_watcher_loop` and the startup sweep.
- **`CARTO_BULK_APPLY` retires its existing `pid != os.getpid()` in-process heuristic.** That heuristic falsely marks server B's legitimately running bulk apply as orphaned when scanned from server A (the normal multi-server topology). BULK_APPLY joins the standard psutil PID-identity classification like every other CARTO type. (PID identity suffices for *liveness*; command-line `--carto-job-file` identity is required only before *force-killing* a detached worker.)
- Persistence path writes `status="failed"` with `"worker process vanished (pid N)"` via `merge_write_carto_job_status` (sticky terminal). **Exception:** a dead process carrying a cancellation sentinel is persisted as `cancelled`, not "failed/vanished".
- Grace window: a claim younger than `start_grace` (30 s) whose job JSON is missing or pidless is treated as starting.
- Unknown liveness ⇒ alive ⇒ never reaped, never killed.
- **Tracker vocabulary:** `_refresh_carto_job_tracker_once`'s hardcoded `active={"queued","running"}` / `terminal={"completed","failed","cancelled"}` sets are replaced by the shared `is_carto_job_active` / `is_carto_job_terminal` authorities. `cancelling` and `waiting` classify as active; `completed_with_warnings` classifies as terminal and **does** trigger the terminal cache refresh (fixes a live bug: warned jobs never refresh the cache).
- **Bulk apply's inner CARTO quiescence wait is removed.** Its sole caller already runs `_handler_preflight_cache_quiescence` before starting the job; once `waiting` correctly classifies as active, the inner wait would deadlock on the job itself.

### 7.2 Cancellation sentinels (authority) + `cancelling` (presentation)

- `system.job.cancel` and the WS `carto_cancel_job` handler: (1) atomically create `.cancel_requests/<job_id>.cancel` — the cancellation **authority**; (2) write `status="cancelling"` via merge_write for presentation (non-terminal, sticky against stale progress writes).
- **All six cancellation checker sites** migrate to sentinel-first: `_carto_should_cancel`, `_carto_should_cancel_file`, `_carto_job_cancel_requested` (server.py), `_job_cancelled` (markdown_generate_pipeline.py), the `reconcile_tree` cancel callback, and the CRUD-sleep polling. Transitional status checks accept `cancelling` OR `cancelled`; sentinel existence is the lasting authority. Poll cadence unchanged (≤ ~1.25 s worst-case inside a single large file's apply phase; optionally drop the F12 caller's `cancel_check_interval` from 10 → 1).
- **Merge precedence for job status** (extends `merge_write_carto_job_status`):

  ```
  on-disk cancelled | failed                → always preserved (sticky terminal)
  on-disk completed_with_warnings           → preserved over completed and any
                                              non-terminal write; yields only to
                                              cancelled | failed
  on-disk completed                         → preserved over non-terminal writes;
                                              yields to cancelled | failed |
                                              completed_with_warnings
  on-disk cancelling                        → preserved over non-terminal writes;
                                              yields to ANY terminal write
  otherwise                                 → accept incoming
  ```

- **Token-verified cancel finalization:** a worker that observed the sentinel and stopped mutation work re-reads the sentinel file before writing `status="cancelled"`; if the sentinel is gone it must not finalize as cancelled (keeps "sentinel = authority" airtight).

### 7.3 Escalation to exact-PID force kill

After the sentinel + `cancelling` write: poll ≤ 5 s for **claim release or verified PID death** (NOT job status — the handler just wrote a status itself; status is presentation, never stop authority). Still alive → verify exact PID identity with psutil: creation time matches **and** `--carto-job-file <this job file>` appears in the process command line (protects against PID recycling onto a *different* weave_worker) → `psutil.Process.terminate()` → wait ≤ 2 s → `psutil.Process.kill()` if still alive.

- **No `taskkill`.** `psutil.terminate()` is exact-process on Windows (and TERM→KILL on POSIX); "never `/T`" is true by construction. The F12+3 phase-8 child is structurally spared.
- **In-process jobs are never killed.** A `CARTO_BULK_APPLY` (or any job running inside the MCP server) that has not stopped within the cooperative window reports `kill_refused_in_process_job` — the PID may be perfectly verified; killing it is refused because it owns the whole MCP server.
- Identity indeterminate ⇒ **do not kill** ⇒ `kill_refused_unverified_pid`.
- Outcomes vocabulary: `cooperative | forced | already_terminal | kill_refused_unverified_pid | kill_refused_in_process_job`.
- Both existing worker launchers already pass `--carto-job-file` on argv (verified).

### 7.4 Release ordering — `finalize_claim`, the one exit door

- Normal finalize: finish all mutation-capable work → persist final progress/status → **finalize_claim** → exit.
- `finalize_claim(claim_id)` is ONE atomic transaction under the admission mutex:
  1. re-read and verify `claim_id`;
  2. max-merge/write the tombstone;
  3. delete the claim.
  (Parent side of a validated live continuation: deletes its claim, writes **no** tombstone — the child owns the chain's cooldown.)
- **Universal finalization:** once admission has created a claim, EVERY terminal exit — success, worker failure, cancellation, preparation failure, job-file failure after claim creation, or spawn failure — goes through `finalize_claim` and creates/max-merges the cooldown tombstone. This may impose a 60 s cooldown after a spawn failure that mutated nothing; that conservative delay is accepted in exchange for deleting the `mutation_started` rollback state machine. One rule: **admitted claim + terminal exit ⇒ terminal status + atomic tombstone-and-release.**
- Cancellation: the claim is **not** released when the cancel is requested — only after the worker confirms stop (token-verified finalize) or PID death is verified post-kill (liveness reap). A new overlapping launch can never start while a cancelled worker is still winding down.

### 7.5 Startup sweep (`lifespan`)

On MCP server start: scan the jobs dir; liveness-check every non-terminal job; dead ⇒ persist failed ("worker process vanished") — or `cancelled` when a sentinel is present — + reap its claim under the admission mutex. **Never kill live workers** — a live orphaned worker is legitimately finishing and releases its own claim on finalize. Force-kill only ever happens via explicit cancel (§7.3).

---

## 8. Component: F12+4 pre-spawn preparation seam

Single and bulk F12+4 currently mutate (write `.md`, rewrite the node note, patch the snapshot) **before** the refresh job's claim would exist — leaving a window where a folder refresh can collide with those mutations.

Required: F12+4 acquires the eventual refresh claim **before the first disk or Workflowy mutation**, and the same claim carries into the spawned worker with no release/reacquire gap. Implementation shape — an internal pre-spawn preparation seam in the one launch authority:

```
admit + create claim/job  (inside admission.mutex)
→ run optional preparation callback WHILE THE CLAIM IS HELD
   (the callback executes inside the admission critical section)
→ spawn worker under the same claim
```

- On preparation failure: universal finalize (§7.4) — terminal status + tombstone + release.
- Footprint for the fresh-node case is computed against the exact payload-overlaid `nodes_by_id` map the handler already builds (preserves F12+4's very-fresh-node support without weakening the fail-closed footprint rule).
- Persistent generated files carry their real target path; temporary F12+4/F12+5 regimes use `path=None` + graph footprint.
- The preparation's own Workflowy writes ride the governor like everything else, with **job-scoped** cancellation context (§4) so a cancelled preparation stops waiting promptly.

---

## 9. Legacy fallback excision (Phase 0)

- Delete `_refresh_file_node_beacons_legacy` (`api_client_nexus.py` ~12424–12850) and its step-9 delegation (~13999). The `except Exception` branch in `refresh_file_node_beacons` now raises a job-failing error carrying the probe metadata + traceback; the worker's existing exception-finalize lands it in the job JSON `result_summary.errors` → widget-visible `failed`. No silent degraded path, ever.
- Delete helpers the legacy path exclusively owned (evaluate the `mode="legacy"` parking-node branch at ~16790 during implementation; leave anything shared). Sweep stale "legacy remains available" comments.
- Explicitly **not** in scope: the legacy 37-tool MCP registration profile (separate future task); unrelated `legacy` vocabulary elsewhere.
- Post-excision semantics: an incremental failure now leaves a partially-reconciled tree + a failed job; re-running F12+1 converges (id-keyed reconcile) — the same contract as cancellation today.

---

## 10. Progressive disclosure (Phase 0 text)

**`beacon` family top-level description** appends:

> ⚠️ DANGER — `map.refresh` launches a detached, bulk Workflowy-rewriting job whose write volume multiplies (one inserted symbol ≈ up to N sequential move calls). Over-use HAS CAUSED a full account-level write-lockout (403 WFB(3)). NEVER loop it, NEVER launch overlapping refreshes, and NEVER re-fire because the cache still looks stale — poll `system.job.status` instead (a stale cache is NOT a failed refresh). Prefer ONE whole-repository refresh over many per-file refreshes; non-overlapping repositories MAY run concurrently (all writes are globally paced account-wide), but you rarely need to.

**`map.refresh` describe (index + full):** blast-radius explanation; governance semantics (one active job per region incl. graph overlap, 60 s cooldown with `retry_after`, global write pacing, account breaker); the guidance; observe via widget / `system.job.status`; cancel via `system.job.cancel`. Admission refusal errors are themselves teaching errors.

Update tool-surface measurement / registration tests for the changed strings.

---

## 11. Error vocabulary (new)

- `AccountWriteBlockedError` (models/errors.py; non-retryable, not a NetworkError subclass; `retry_after`, `blocked_until`, `reason`). Never caught or re-wrapped by CRUD retry loops.
- `WriteGovernorUnavailableError` (names the inaccessible governance path).
- `AdmissionUnavailableError` (names the inaccessible admission path).
- Admission refusal kinds (structured results, not exceptions, at the launcher): `overlap_conflict` (conflicting job_id + path), `cooldown_active` (`retry_after`), `footprint_unresolvable` (teaching: run `system.cache.refresh`).
- Cancel outcomes: `cooperative | forced | already_terminal | kill_refused_unverified_pid | kill_refused_in_process_job`.

---

## 12. Config additions (workflowy_nexus JSON, beside `paths.cartographerJobsDir`)

`write_governor.interval_seconds` (1.0) · `write_governor.breaker_initial_cooldown_seconds` (900) · `write_governor.breaker_max_cooldown_seconds` (7200) · `admission.cooldown_seconds` (60) · `admission.start_grace_seconds` (30) · `governance.mutex_wait_seconds` (10).

---

## 13. Phase plan (session-sized; each phase ships green; full 1400+ suite is the gate)

| Phase | Contents | Exit criteria |
|---|---|---|
| **0 — Immediate hygiene** | Legacy excision (§9) + progressive-disclosure warnings (§10) + `RateLimitError.retry_after` attribute fix + registration test updates. Zero substrate dependencies — protects the interim **today**. | Excision test; warning-text tests; suite green |
| **A1 — Substrate I** | `carto_job_governance.py` skeleton: `os_lock` wrapper (incl. async non-blocking acquisition), account scoping, claim/tombstone/sentinel schemas + CRUD, `dedupe_key`, footprint + overlap predicate (machine-wide path / account-scoped graph), psutil liveness helpers. **Nothing wired.** | Unit tests green (mutex exclusion across real subprocesses; overlap matrix incl. cross-account path + Markdown-in-Markdown shape; footprint fail-closed); suite green |
| **A2 — Substrate II** | `carto_write_governor.py`: ticket algorithm, busy/broken contract, corrupt-state self-heal, breaker state machine primitives, `AccountWriteBlockedError`. **Nothing wired.** | Unit tests green (ticket ordering across 2 real processes; breaker transitions; self-heal); suite green |
| **B — Governor + breaker live** | `_send_workflowy_write` authority (raw response in, governance on status out); six write sites rewired (old delay lines removed); create-node still-waiting diagnostic preserved; `/nodes-export` cancel-aware sleep; WFB(3) trip; 429 global defer; config keys; AST enforcement test. **Incident class is impossible after this phase.** | Cross-process pacing test; cancel-during-wait raises; reads-don't-consume-slots; breaker trip/half-open-lease/re-trip/reservation-suppression; governor-unavailable fail-closed; `AccountWriteBlockedError` propagates un-retried with zero HTTP calls; suite green |
| **C — Shared launcher + admission** | `launch_carto_refresh_job` extracted (incl. pre-spawn preparation seam, §8); wrapper + pipeline rewired; admission replaces exact-key dedupe scan; semantic `dedupe_key`; co-claims with validated `parent_job_id`; MARKDOWN_GENERATE whole-life claims; **CARTO_BULK_APPLY region claims**; F12+4 pre-mutation claiming; bulk-leaf real-path claims. | Admission race test (folder vs contained file, real subprocesses — exactly one winner); dedupe_key decision-table tests (same intent dedupes, different intent conflicts); cooldown tests incl. overlapping tombstones; parent-continuation validation tests; F12+4 preparation-failure finalize test; suite green |
| **D — Liveness + cancel escalation** | View-level psutil classification (all `CARTO_*`, BULK_APPLY heuristic retired); tracker vocabulary via shared status authorities + bulk-apply inner-quiescence removal; watcher-loop + `lifespan` startup-sweep persistence; sentinels + `cancelling` state at all six checker sites; merge-precedence extension; token-verified cancel finalization; `finalize_claim` (one exit door); exact-PID psutil terminate→kill; in-process kill refusal; claim-aware GC; job-scoped cancel contextvars. | Dead-PID → failed-sticky (or cancelled-with-sentinel) + un-wedge test; unknown-liveness-never-reaped; `completed_with_warnings` triggers terminal refresh; cancel three-outcomes + in-process refusal tests; sentinel-survives-progress; `cancelling`-never-stuck test; claim-aware GC race test; suite green |
| **E — Residual docs** | Describe/docs cleanup; Workflowy reference docs updated for governance behavior incl. throughput expectations (concurrent bulk operations now genuinely serialize at governor rate — slowness is pacing, not a hang) and the explode→persist cooldown note (F12+3-after-bulk-F12+4 refusal is protection, not breakage). | Suite green |
| **F — Heartbeat + widget triage** | Daemon heartbeat thread (both worker modes); scan mtime merge; widget triage fields. **Independently deferrable** — diagnostic-only; cut this phase first under schedule pressure. | Heartbeat freshness/staleness tests; stale-heartbeat-never-reaps; widget backward-compat; suite green |

Phase A may be split A1/A2 as shown (pre-authorized). Standing directives every phase: beacon blocks on new functions (curated — entry points and orchestrators, not every helper); **auto-commit + push as two separate commands** after each edit round; the full test suite is the per-phase gate.

---

## 14. Test plan (consolidated)

**Real-subprocess tests** (cross-process semantics are the subject): mutex mutual exclusion · governor global pacing · admission race (folder vs contained file) · sentinel survival across progress/finalize writes · worker self-registration after launcher death.

**In-process tests:** overlap decision table (path containment / cross-account path / graph chain same-account-only / sibling-allow / cross-repo-allow / exact-dedupe / same-path-diff-mode refuse) · dedupe_key identity (same intent dedupes; refresh-vs-bulk-apply-vs-regenerate conflicts; override-regime distinctness) · footprint fail-closed · cooldown exact + overlapping tombstones + max-merge · parent continuation (co-claim; tombstone-bypass-only; unvalidated `parent_job_id` refused) · claim-token late-release safety · stale-claim reaping under mutex · busy-waits vs broken-fails-closed · corrupt-state self-heal · six-writes-consume-slots / three-reads-don't · cancel-during-governor-wait · job-scoped cancel does not cancel unrelated same-client work · 429 global defer + `retry_after` attribute + bounded pre-issued-ticket leakage documented · breaker trip / single half-open probe (atomic with ticket) / probe-lease recovery / re-trip doubling / pre-trip reservation suppression · `AccountWriteBlockedError` un-retried, zero HTTP calls · governor-unavailable fail-closed · liveness classify / persist / never-on-unknown · BULK_APPLY standard liveness (no `pid != getpid` heuristic) · `completed_with_warnings` terminal-refresh trigger · cancel outcomes incl. `kill_refused_in_process_job` · exact-PID verification refuses a recycled different-job worker · merge precedence (cancelling yields to terminal; sticky terminals) · token-verified cancel finalization · `finalize_claim` atomicity (no claim-less/tombstone-less window) · universal finalization on spawn/preparation failure · claim-aware GC (live claim pins job JSON; live tombstone survives history clear) · release ordering (claim outlives cancel) · legacy excision · `/nodes-export` cancel-aware sleep · AST write-authority enforcement · create-node still-waiting diagnostic preserved · heartbeat mtime freshness · heartbeat-stale ≠ reap · account-scope isolation (two scopes pace independently).

**Every phase:** full existing suite (1400+) green, including the F12 failure-hardening regression tests from `31ab3dc`.

---

## 15. Non-goals (v1 — do not "fix" these later without Dan)

- Durable cross-process job **queue** (refusal + `retry_after` suffices; a queue is a subsystem).
- Process-count cap K (the governor subsumes it).
- Reorder-storm minimal-move algorithm (the structural blast-radius reduction; separate project).
- Read-path pacing.
- Legacy MCP tool-surface removal (separate task).
- Browser-side human mutations (F10 marker toggle, manual GUI edits — human tempo, not agent-amplified; by construction outside the governor).
- Per-tool warnings beyond `map.refresh` (the governor makes them unnecessary).
- Region claims for ordinary single-node edits (tag/edit/anchor captures) — they pass through the write governor, which suffices at that write volume.
- 429 reservation recall, ticket generations, or any re-queue machinery (bounded leakage accepted).
- Converting `CARTO_BULK_APPLY` into a detached worker mode (standard psutil liveness + job-scoped cooperative cancel is the smaller correct change).
- Cross-runtime-directory or cross-machine coordination.

---

## 16. Invariant card (the forest — one page, normative)

**The seven principles (§1).** **The six write sites (§5.1).** **The one exit door (§7.4).** **The admission decision table (§6.3).** Everything else in this document is elaboration of these four artifacts. If an implementation detail ever appears to conflict with this card, this card wins.

### 16.1 Claim lifecycle

```
                 ┌────────────────────────────────────────────────────┐
                 │          ADMISSION  (admission.mutex, machine)     │
                 │  sweep → dedupe_key? → overlap? → cooldown?        │
                 └────────────────────────────────────────────────────┘
                                   │ claim created
                                   ▼
   ┌──────────────── ACTIVE CLAIM (mutation-capable work) ─────────────────┐
   │   worker running │ F12+4 preparation-under-claim │ co-claim (parent+  │
   │   child phase-8 refresh, validated parent_job_id)                     │
   └────────────────────────────────────────────────────────────────────────┘
         │                     │                          │
   normal finalize      cancel requested            PID dead / vanished
         │                     │                          │
         ▼                     ▼                          ▼
   ╔════════════════════ ONE EXIT DOOR: finalize_claim() ═══════════════════╗
   ║  under admission.mutex: 1. verify claim_id                            ║
   ║                         2. max-merge tombstone                        ║
   ║                         3. delete claim                               ║
   ║  (parent in a live continuation: deletes claim, NO tombstone)         ║
   ╚════════════════════════════════════════════════════════════════════════╝
```

### 16.2 Cancellation flow

```
 cancel request (system.job.cancel | WS carto_cancel_job)
   1. create .cancel_requests/<job>.cancel          ← AUTHORITY (disk)
   2. status := "cancelling"                        ← PRESENTATION
                          │
   worker observes sentinel (≤1.25 s) → stops mutation work
     → re-reads sentinel → finalize_claim → status := "cancelled"
                          │
   controller polls ≤5 s: claim released? PID dead?
      ├─ yes → outcome = cooperative
      └─ no  → psutil identity: creation time + "--carto-job-file <file>"
                ├─ detached + verified → terminate() → 2 s → kill()  = forced
                ├─ in-process job      → kill_refused_in_process_job
                └─ identity unclear    → kill_refused_unverified_pid
   Status is NEVER stop authority. Claim release / PID death only.
```

### 16.3 Scoping

```
 MACHINE-WIDE (per runtime dir):  claims · tombstones · sentinels ·
                                  heartbeats · admission.mutex
 ACCOUNT-SCOPED (per account):    write governor · write breaker · write_slot.mutex
 OVERLAP RULE:  disk paths overlap across ALL accounts;
                Workflowy graph overlaps only within the SAME account.
 DEDUPE RULE:   dedupe_key (intent) ≠ footprint (collision surface).
```

---

## 17. Rulings record (final — do not relitigate)

**Carried from v3 (1–30):** locks/tombstones separate artifacts; admission mutex is the keystone; co-claims beat handoff; governor at the client write layer (six sites, reads ungoverned); governor fails closed (no per-process fallback); breaker closed/open/half_open with probe lease + pre-send recheck; 429 defers globally + Phase-0 `retry_after` fix; account scoping via `sha256(base_url+key)`, raw key never persisted, `parent_job_id` never on tool schemas; warning text permits governed non-overlapping parallelism; heartbeat write-once + mtime + diagnostic-only; tombstones full footprint + max-merge; full-sha256 lock filenames; explicit release ordering; Phase 0 first; no durable queue; real-subprocess tests scoped to cross-process semantics; two modules/two mutexes; MARKDOWN_GENERATE whole-life claims; bulk F12+4 real-path claims; footprint fail-closed; dedupe ≠ cooldown; psutil hard dependency; corrupt self-heals / broken fails closed; AST enforcement test; universal cooldown, `parent_job_id` continuations bypass tombstone only; no `taskkill /T` + exact-PID verification; scanner view-only, persistence in watcher + startup sweep; liveness covers all detached `CARTO_*`; temp-regime path=None graph footprint; orphan ⇒ failed + "worker process vanished"; same-path-diff-mode refuse; 5 s escalation / 60 s cooldown; two modules; optional `cancel_check_interval` 10 → 1.

**New in v4 (31–47):**

31. **Broker-as-governor rejected.** Detached workers must remain governable with no live broker and no live MCP server (they survive restarts by design); the file+OS-mutex substrate is the only substrate with the same lifetime as the workers.
32. **Scoping split:** region safety state machine-wide; write pacing state account-scoped; overlap predicate = paths across all accounts, graph within the same account. Claim/tombstone keys incorporate account scope to avoid cross-account collisions.
33. **Semantic `dedupe_key`** (intent identity) is distinct from the overlap footprint (collision surface); same footprint + different intent ⇒ `overlap_conflict`, not dedupe.
34. **`finalize_claim` is atomic and universal** (the one exit door): verify claim_id → max-merge tombstone → delete claim, under the admission mutex, on every terminal exit including spawn/preparation failure. No `mutation_started` rollback state machine.
35. **`cancelling` presentation state** + explicit merge precedence (terminal sticky; `cancelling` sticky over non-terminal writes only, yields to any terminal). Status is never stop authority.
36. **psutil `terminate()` → 2 s → `kill()`** replaces any `taskkill` path; exact-process by construction; in-process jobs never killed (`kill_refused_in_process_job`).
37. **Claim-aware GC:** all claim/tombstone mutation under the admission mutex; live claim pins its job JSON; manual history clear never deletes live tombstones.
38. **Job-scoped (contextvar) cancellation context** for in-process governed waits; shared-client-global callbacks are forbidden inside the MCP server.
39. **F12+4 pre-spawn preparation seam:** claim acquired before the first mutation; the preparation callback executes inside the admission critical section; footprint may use the payload-overlaid cache map.
40. **`CARTO_BULK_APPLY` holds a region claim** and joins standard psutil liveness; the `pid != os.getpid()` heuristic is retired; its inner CARTO quiescence wait is removed (would self-deadlock).
41. **Breaker decision + ticket issuance = one `write_slot.mutex` critical section** (half-open probe claim is atomic with the ticket).
42. **Bounded accepted races:** pre-429 issued tickets continue at governor rate (no recall machinery); a WFB(3) in-flight write cannot be unsent (breaker suppresses future sends only).
43. **`completed_with_warnings` is sticky** over `completed` and non-terminal writes, and triggers the terminal cache refresh (fixes a live tracker bug); tracker uses the shared status authorities everywhere.
44. **Token-verified cancel finalization:** the worker re-reads the sentinel before writing `cancelled`.
45. **`AccountWriteBlockedError` is never caught or re-wrapped** by CRUD retry loops (explicit propagation test).
46. **`parent_job_id` privilege is validated** (active, same-account, overlapping parent claim required); nominal presence alone grants nothing.
47. **The invariant card (§16) is normative and lives in this spec** (no separate artifact to drift); Phase F is the designated independently-deferrable phase.

---

*End of specification v4. Implementation begins at Phase 0 in a fresh session with this file and a fresh beacon slice of the repo.*
