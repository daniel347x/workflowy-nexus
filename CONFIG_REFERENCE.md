# `workflowy_nexus.config` Reference

The NEXUS Core v1 MCP server reads its runtime configuration from a JSON file. The schema is validated by pydantic with **`extra: forbid`** — any unknown top-level keys or unknown keys inside `paths`, `websocket`, `timing`, or `retention` will cause the server to fail to load. Stick to the documented field names below.

## How the config file is discovered

Resolution order (first match wins):

1. The path in the `WORKFLOWY_NEXUS_CONFIG` environment variable (typically set in your MCP host's JSON config — see the `env` block in your TypingMind / Claude Code / etc. MCP server entry).
2. `workflowy_nexus.config.local.json` adjacent to the script that's running (e.g., next to `glimpse_deploy_trampoline.py`).
3. `MCP_Servers/workflowy_nexus.config.local.json` walking up from the script.
4. Smart per-OS defaults if no config file is found.

## Getting started

1. Copy `workflowy_nexus.config.example.json` (in this repo's root) to `workflowy_nexus.config.local.json` somewhere convenient.
2. Replace every `/absolute/path/to/...` placeholder with a real path on your machine.
3. Either set `WORKFLOWY_NEXUS_CONFIG` to that file's path, or place it where the resolver expects it (see above).

## Field reference

### Top level

| Field | Type | Required | Notes |
|---|---|---|---|
| `schemaVersion` | int | yes | Always `1` for this release. Reserved for future schema migrations. |
| `paths` | object | no | Runtime filesystem paths. All sub-fields optional; sensible per-OS defaults if omitted. |
| `websocket` | object | no | GLIMPSE WebSocket server settings (currently hardcoded to localhost:8765 on the server side; this section is reserved for future configurability). |
| `timing` | object | no | Polling/reconnect intervals for the Chrome extension client. |
| `retention` | object | no | Age threshold for the CARTO job GC; see `cartoJobMaxAgeSeconds` for records, logs and same-stem folders. |
| `write_governor` | object | no | One in-process write scheduler and persisted breaker/Retry-After settings; subkeys use the snake_case names below. |
| `v1_only` | bool | no | Default `false`. On first insert selection, `true` latches public-v1-only placement for this client/daemon's life. No capability probe, wait or setup is required; unavailable acceleration otherwise falls back to v1. The N03 transport is not connected to Cartographer until N04. |
| `admission` | object | no | Configurable post-terminal CARTO memory cooldown. |
| `governance` | object | no | Existing short governor-lock budget and unchanged Cartographer deletion thresholds. |
| `mutationLockAcquireTimeoutSeconds` | float | no | Default 60.0, positive; the single total typed-region acquisition wait budget. Lengthen it for longer contention; it is not a hold-duration limit. |
| `mutationLockLongLivedRetryAfterSeconds` | float | no | Default 60.0, positive; retry advice for long-lived holders and Cartographer launch conflicts when no usable completion estimate exists. It never expires, releases or kills a holder, and it is not an acquisition wait bound. |

### `paths`

All optional. If a field is omitted, the server falls back to portable per-user defaults under your OS's standard runtime directories.

| Field | Type | Notes |
|---|---|---|
| `nexusRunsBase` | string | Base directory for NEXUS run artifacts (terrain JSON, gem JSON, journals). |
| `cartographerJobsDir` | string | Directory for Cartographer job JSON files and the whole account coherence artifact set, including `.coherence/<account>/writer.lock`. All full entries for that account use the same configured root; relocation is an offline move of the whole set, not a second writer domain. |
| `cartographerFileRefreshDir` | string | Directory for per-file Cartographer F12 refresh artifacts. |
| `windsurfExeCandidates` | list of strings | Ordered list of candidate paths to `Windsurf.exe` for F9 "open node in editor". The server tries each in order, then falls back to standard install locations. Omit if you don't use F9. |

#### Trampoline-only fields (Workflowy Desktop ASAR patching)

These fields are only consumed by `glimpse_deploy_trampoline.py`. **Most users should use the unpacked Chrome extension** in `workflowy-glimpse-cache/` against `workflowy.com` in Chrome, and can ignore these fields. The trampoline is the advanced path for installing GLIMPSE into the Workflowy Desktop Electron app — invasive, breaks on every Workflowy update, and high-friction.

| Field | Type | Notes |
|---|---|---|
| `glimpseAgentWorkspace` | string | Working directory where the trampoline stages files. Defaults to `%LOCALAPPDATA%/glimpse_deploy_workspace` on Windows. |
| `glimpseCacheSource` | string | **Required for trampoline use.** Absolute path to your local checkout of `workflowy-glimpse-cache/glimpse-cache-client.js` (this is the file the trampoline patches into Workflowy Desktop's ASAR). |
| `workflowyAppAsar` | string | Path to Workflowy Desktop's `app.asar`. Defaults to `%LOCALAPPDATA%/Programs/WorkFlowy/resources/app.asar` on Windows. |
| `workflowyElectronCacheDirs` | list of strings | Workflowy Desktop's Electron cache directories. The trampoline clears these after deploying to ensure the new ASAR is picked up. Defaults to `%APPDATA%/WorkFlowy/{Cache,Code Cache,GPUCache}` on Windows. |
| `glimpseWslRepoDir` | string | Optional WSL-side checkout of `workflowy-glimpse-cache`. When set, the trampoline runs a `git pull` here before packing the ASAR (handy if you develop the extension on the WSL side). Set to empty string `""` to disable. |
| `glimpseStagingSubdir` | string | Subdirectory name under `glimpseAgentWorkspace` where the trampoline stages files for ASAR packing. Defaults to `staging`. |

### `websocket`

| Field | Type | Default | Notes |
|---|---|---|---|
| `host` | string | `localhost` | Currently hardcoded on the server side; this field is reserved for future configurability. |
| `port` | int | `8765` | Currently hardcoded on the server side. |

### `timing`

| Field | Type | Default | Notes |
|---|---|---|---|
| `websocketReconnectDelayMs` | int | `3000` | How long the Chrome extension waits before reconnecting after a dropped WebSocket connection. |
| `cartoJobsPollIntervalMs` | int | `1000` | Accepted and validated but not consulted by the extension: Jobs polls at a fixed 1 s while the tab is visible; hidden tabs do not poll. |
| `refreshSettleSeconds` | float | `2.0` | Mutation-coherence (S02): the best-effort settle delay between a refresh's lineage-cut capture and its `/nodes-export` request, giving Workflowy's server a moment to persist just-committed GUI edits so the export actually contains the edits the refresh will clear. The cut is captured FIRST, so anything arriving during the delay is never lost — it simply survives to the next refresh. When manually clicking Refresh right after making GUI edits, wait a few seconds for the edits to reach Workflowy's servers first; this built-in delay is belt-and-suspenders, not a proof. |

### `retention`

| Field | Type | Default | Notes |
|---|---|---|---|
| `cartoJobMaxAgeSeconds` | int | `3600` | Age threshold for all `CARTO_*` job kinds, measured from `updated_at` (else `created_at`). GC runs at daemon start, then at most every 600 s: `completed`/`cancelled` jobs older than the threshold lose their record JSON, associated log and same-stem folder; `failed`/`completed_with_warnings` jobs lose only the folder's `file_refresh/` debug artifacts, keeping their record and evidence until Clear history. Active/protected jobs and unknown statuses are never removed. Legacy standalone F12 artifacts age out by file mtime at the same threshold. `0` permits immediate cleanup at the next pass, not disablement. Nonblank `WORKFLOWY_CARTO_JOB_MAX_AGE_SECONDS` overrides JSON/default. |

### `admission`

| Field | Type | Default | Notes |
|---|---|---|---|
| `cooldown_seconds` | float | `60.0` | Nonnegative; post-terminal CARTO cooldown. |

The post-terminal CARTO cooldown lives only in memory and is lost on daemon restart. Q3 release creates no cooldown. The retired `start_grace_seconds`, `spawn_grace_seconds` and `orphan_grace_seconds` keys are accepted and discarded on read; they are not emitted or used. No new environment aliases are added.

### `governance`

| Field | Type | Default | Notes |
|---|---|---|---|
| `mutex_wait_seconds` | float | `10.0` | Positive; the existing short governor-lock budget, not a job or hold lifetime. |
| `cartoFolderFileDeleteThreshold` | int | `10` | Nonnegative; unchanged alias of `carto_folder_file_delete_threshold`. |
| `cartoReconcileDeleteThreshold` | int | `100` | Nonnegative; unchanged alias of `carto_reconcile_delete_threshold`. |

### `write_governor`

All optional; these are JSON subkeys, with no new environment overrides.

| Field | Type | Default | Notes |
|---|---|---|---|
| `interval_seconds` | float | `1.0` | Positive; one shared paced ticket stream for Workflowy writes. |
| `max_wait_before_refusal_seconds` | float | `3600.0` | Nonnegative; existing caller-wait window, lengthened from 30 s. Longer slot/429 delays return the existing typed retry advice; the full Retry-After remains persisted, never clamped. Explicit configured values remain honored. |
| `breaker_initial_cooldown_seconds` | float | `900.0` | Positive; initial WFB recovery cooldown. |
| `breaker_max_cooldown_seconds` | float | `7200.0` | Positive, at least the initial cooldown; matching failed probes double only to this cap. |
| `probe_lease_seconds` | float | `120.0` | Positive; the existing persisted half-open probe window, not a Cartographer hold TTL. |
| `retry_after_fallback_seconds` | float | `60.0` | Nonnegative; existing fallback for missing or malformed Retry-After. |

The existing `governance.mutex_wait_seconds` (positive, default `10.0`) remains the short in-process acquisition budget, not a job or hold lifetime. Under the account governance directory, `write_breaker.json` retains breaker state and `write_governor.json` retains only the Retry-After floor under its existing `next_slot_at` key. The latter is read once at scheduler initialization; ordinary tickets do not write it, and editing/deleting it during a running daemon does not reset its memory schedule. No cleanup is required for old coordination files.

## Environment variable overrides

A handful of fields can be overridden via environment variables at server startup (useful for one-off testing without editing the JSON):

| Env var | Overrides |
|---|---|
| `WORKFLOWY_API_KEY` | `apiKey` (required for any Workflowy API access) |
| `WORKFLOWY_API_URL` | API base URL |
| `WORKFLOWY_TIMEOUT` | Request timeout in seconds |
| `WORKFLOWY_MAX_RETRIES` | Max retry attempts |
| `WORKFLOWY_V1_ONLY` | `v1_only`; explicit nonblank environment values win over JSON; blank remains unset. Restart the daemon to reset the first-selection latch. |
| `MUTATION_LOCK_ACQUIRE_TIMEOUT_SECONDS` | `mutationLockAcquireTimeoutSeconds` |
| `MUTATION_LOCK_LONG_LIVED_RETRY_AFTER_SECONDS` | `mutationLockLongLivedRetryAfterSeconds` |
| `DEBUG` | Debug mode (truthy: `1`, `true`, `yes`, `on`) |
| `LOG_LEVEL` | Log level (`DEBUG`, `INFO`, `WARN`, `ERROR`) |
| `NEXUS_RUNS_BASE` | `paths.nexusRunsBase` |
| `CARTOGRAPHER_JOBS_DIR` | `paths.cartographerJobsDir` |
| `CARTOGRAPHER_FILE_REFRESH_DIR` | `paths.cartographerFileRefreshDir` |
| `WORKFLOWY_WS_HOST` | `websocket.host` |
| `WORKFLOWY_WS_PORT` | `websocket.port` |
| `WORKFLOWY_WS_RECONNECT_DELAY_MS` | `timing.websocketReconnectDelayMs` |
| `WORKFLOWY_CARTO_JOBS_POLL_INTERVAL_MS` | `timing.cartoJobsPollIntervalMs` |
| `WORKFLOWY_CARTO_JOB_MAX_AGE_SECONDS` | `retention.cartoJobMaxAgeSeconds` (nonblank environment wins; blank is unset) |
| `WORKFLOWY_MCP_TRANSPORT` | Package/console entry selector; unset or blank defaults to `shim`. `shim` uses the stdio proxy, `stdio` the maintained guarded full stdio server, `http` (also `streamable-http` / `streamable_http`) the guarded HTTP daemon. Values are stripped/lowercased; `--transport` wins, including an explicit blank. Unknown values warn on stderr and use the shim. Does not change `workflowy-mcp-shim`. |
| `WORKFLOWY_MCP_HTTP_DIAL_HOST` | Existing concrete client destination override; takes precedence over the bind-host fallback, including with `--host`. Only a native shim dialing loopback (numeric loopback or `localhost`) may spawn a local daemon; WSL and non-loopback destinations are attach-only and use `daemon_unavailable` with the native-host remedy when absent. No DNS/interface discovery. |
| `WORKFLOWY_MCP_DAEMON_DRAIN_TIMEOUT_S` | Seconds; default/floor 3600, lengthenable without an upper cap. Effective max with finite positive `WORKFLOWY_MCP_SHIM_CALL_TIMEOUT_S` and the requesting shim's call/drain bound. Invalid/short values warn and use the safe floor. Expiry cancels the restart attempt, never admitted work; the daemon resumes admitting. |
| `WORKFLOWY_MCP_TEST_ALLOW_WSL_SERVER=1` | Test suite only: exact value `1` skips only the WSL full-server/broker refusal, never the writer guard or WSL classification. Not a deployment flag. |

The existing shim call timeout remains 900 seconds by default (`0` disables it);
the spawn timeout remains 90 seconds. Neither shortens the drain bound. Attach
and control waits allow the effective drain bound plus two spawn budgets; the
ordinary forwarded-call timeout keeps its existing meaning.

## Validation

Validation happens at server startup. If your config has any unknown keys, type errors, or fails a field validator (e.g., `port` outside 1–65535), the server will refuse to start with a pydantic error message identifying the offending field. Use this as fast feedback to fix your config.
