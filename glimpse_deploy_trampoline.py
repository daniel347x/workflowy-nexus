r"""
GLIMPSE Deploy Trampoline

Purpose:
  Robust, single-entry-point deploy script for the Workflowy GLIMPSE cache
  extension into the Workflowy Desktop (Electron) app.asar bundle.

  This script is the "advanced / desktop-mode" install path for GLIMPSE.
  Most users should install the Chrome extension directly into Chrome and
  point Chrome at workflowy.com — that path has zero ASAR patching. The
  trampoline exists for users who specifically want GLIMPSE inside the
  Workflowy Desktop Electron app.

Behavior:
  - Reads paths from workflowy_nexus.config.local.json (see resolution
    order below). Smart Windows defaults kick in if config fields are
    omitted, so most users only need to set "glimpseCacheSource".
  - Uses WSL to run `npx asar extract` and `npx asar pack` against the
    Workflowy app.asar bundle.
  - Patches index.html to load glimpse-cache-client.js on startup.
  - Clears Electron renderer caches so the new ASAR is loaded clean.
  - Copies the new ASAR back over the installed Workflowy resources.

Config resolution order (first match wins):
  1. $WORKFLOWY_NEXUS_CONFIG env var → load that file.
  2. workflowy_nexus.config.local.json sitting NEXT TO this script.
  3. MCP_Servers/workflowy_nexus.config.local.json relative to this
     script's directory (matches Dan's local Obsidian layout).
  4. No config file → fall back entirely to smart Windows defaults.

Smart defaults (used when a config field is missing or no config found):
  - workflowyAppAsar     → %LOCALAPPDATA%/Programs/WorkFlowy/resources/app.asar
  - workflowyElectronCacheDirs → %APPDATA%/WorkFlowy/{Cache, Code Cache, GPUCache}
  - glimpseAgentWorkspace → %LOCALAPPDATA%/glimpse_deploy_workspace
  - glimpseStagingSubdir → "staging"
  - glimpseWslRepoDir    → "" (disables the optional WSL git sync step)
  - glimpseCacheSource   → REQUIRED, no smart default (the user must say
                           where their GLIMPSE source lives).

Usage (from PowerShell / CMD):
  python glimpse_deploy_trampoline.py <version-tag>

Example:
  python glimpse_deploy_trampoline.py 3.7.0-hover-uuid-fix-20251123

Notes:
  - Version argument is required; it becomes the suffix of the staged
    ASAR filename: app.asar.v<version>
  - WSL must be installed and reachable on PATH (we shell out to `wsl`
    for `npx asar` because Windows CMD npx fails on long paths).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn


# ---------------------------------------------------------------------------
# Path helpers: Windows ↔ WSL conversion
# ---------------------------------------------------------------------------

def _to_wsl_path(win_path: str) -> str:
    """Convert a Windows path like 'E:\\foo\\bar' to '/mnt/e/foo/bar'.

    Handles both backslash and forward-slash Windows inputs. Returns the
    path with forward slashes and lowercased drive letter under /mnt/.
    If the input doesn't look like a drive-letter path, returns it
    unchanged (e.g., '~/foo' stays '~/foo').
    """
    if not win_path:
        return win_path
    p = win_path.replace("\\", "/")
    # Match drive letter pattern like "E:/..." or "C:/..."
    if len(p) >= 2 and p[1] == ":":
        drive = p[0].lower()
        remainder = p[2:].lstrip("/")
        return f"/mnt/{drive}/{remainder}"
    return p


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def _find_config_file(script_dir: Path) -> Path | None:
    """Locate workflowy_nexus.config.local.json using the documented
    resolution order. Return Path if found, else None.
    """
    # 1. Env var
    env_val = os.environ.get("WORKFLOWY_NEXUS_CONFIG", "").strip()
    if env_val:
        env_path = Path(env_val).expanduser()
        if env_path.is_file():
            return env_path
        print(
            f"[GLIMPSE Deploy] WARNING: WORKFLOWY_NEXUS_CONFIG set to "
            f"'{env_val}' but file not found; falling back to script-adjacent lookup.",
            file=sys.stderr,
        )

    # 2. Script-adjacent
    candidate = script_dir / "workflowy_nexus.config.local.json"
    if candidate.is_file():
        return candidate

    # 3. Sibling MCP_Servers folder (matches Dan's local Obsidian layout)
    candidate = script_dir / "MCP_Servers" / "workflowy_nexus.config.local.json"
    if candidate.is_file():
        return candidate

    # 4. No config — caller will use smart defaults
    return None


def _load_paths_from_config(config_path: Path | None) -> dict[str, Any]:
    """Load the paths section from a config file. Returns {} if no config."""
    if config_path is None:
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        print(
            f"[GLIMPSE Deploy] WARNING: Could not parse config file '{config_path}': {e}",
            file=sys.stderr,
        )
        return {}
    paths = data.get("paths")
    if not isinstance(paths, dict):
        return {}
    return paths


def _resolve_config(script_dir: Path) -> dict[str, Any]:
    """Resolve final config dict with smart defaults applied.

    Required fields raise a helpful error if not provided AND no
    smart default is available.
    """
    config_path = _find_config_file(script_dir)
    if config_path is not None:
        print(f"[GLIMPSE Deploy] Using config file: {config_path}")
    else:
        print("[GLIMPSE Deploy] No config file found; using smart Windows defaults.")

    paths = _load_paths_from_config(config_path)

    # Smart defaults
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    appdata = os.environ.get("APPDATA", "")

    resolved: dict[str, Any] = {}

    # workflowyAppAsar
    resolved["workflowyAppAsar"] = paths.get("workflowyAppAsar") or (
        os.path.join(local_appdata, "Programs", "WorkFlowy", "resources", "app.asar")
        if local_appdata
        else ""
    )

    # workflowyElectronCacheDirs
    raw_cache_dirs = paths.get("workflowyElectronCacheDirs")
    if isinstance(raw_cache_dirs, list) and raw_cache_dirs:
        resolved["workflowyElectronCacheDirs"] = list(raw_cache_dirs)
    elif appdata:
        resolved["workflowyElectronCacheDirs"] = [
            os.path.join(appdata, "WorkFlowy", "Cache"),
            os.path.join(appdata, "WorkFlowy", "Code Cache"),
            os.path.join(appdata, "WorkFlowy", "GPUCache"),
        ]
    else:
        resolved["workflowyElectronCacheDirs"] = []

    # glimpseAgentWorkspace
    resolved["glimpseAgentWorkspace"] = paths.get("glimpseAgentWorkspace") or (
        os.path.join(local_appdata, "glimpse_deploy_workspace") if local_appdata else ""
    )

    # glimpseStagingSubdir
    resolved["glimpseStagingSubdir"] = paths.get("glimpseStagingSubdir") or "staging"

    # glimpseWslRepoDir (optional — empty disables the WSL git sync step)
    resolved["glimpseWslRepoDir"] = paths.get("glimpseWslRepoDir") or ""

    # glimpseCacheSource (REQUIRED — no smart default)
    resolved["glimpseCacheSource"] = paths.get("glimpseCacheSource") or ""

    return resolved


def _normalize_win(p: str) -> str:
    """Normalize a path string to Windows-native form (backslashes, absolute)."""
    if not p:
        return p
    return os.path.normpath(p)


# ---------------------------------------------------------------------------
# Main deploy logic
# ---------------------------------------------------------------------------

def die(msg: str) -> NoReturn:
    print(f"[GLIMPSE Deploy] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def sync_wsl_repo(version: str, glimpse_cache_source_win: str, wsl_repo_dir: str) -> None:
    """Optionally sync the GLIMPSE source into a WSL git clone and push.

    Skipped if `wsl_repo_dir` is empty. Best-effort only — any failure is
    logged as a warning and execution continues.
    """
    if not wsl_repo_dir:
        print("[GLIMPSE Deploy] glimpseWslRepoDir not configured; skipping WSL git sync.")
        return

    print(f"[GLIMPSE Deploy] Syncing WSL Git repo ({wsl_repo_dir})...")

    glimpse_source_wsl = _to_wsl_path(glimpse_cache_source_win)

    status_cmd = (
        f"cd {wsl_repo_dir} && "
        f"cp '{glimpse_source_wsl}' ./glimpse-cache-client.js && "
        f"git status --porcelain"
    )
    try:
        status_result = subprocess.run(
            ["wsl", "bash", "-lc", status_cmd],
            check=True,
            capture_output=True,
            text=True,
        )
        changes = (status_result.stdout or "").strip()
        if status_result.stderr:
            print("[GLIMPSE Deploy] WSL Git status stderr:", file=sys.stderr)
            print(status_result.stderr, file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print("[GLIMPSE Deploy] WARNING: WSL Git status failed (non-zero exit).", file=sys.stderr)
        print(e.stdout or "<stdout empty>", file=sys.stderr)
        print(e.stderr or "<stderr empty>", file=sys.stderr)
        return
    except FileNotFoundError:
        print("[GLIMPSE Deploy] WARNING: wsl.exe not found; skipping WSL Git sync.", file=sys.stderr)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[GLIMPSE Deploy] WARNING: Unexpected error during WSL Git status: {e}", file=sys.stderr)
        return

    if not changes:
        print("[GLIMPSE Deploy] No GLIMPSE changes to commit in WSL repo.")
        return

    commit_cmd = (
        f"cd {wsl_repo_dir} && "
        f"git add glimpse-cache-client.js && "
        f"git commit -m 'Sync GLIMPSE client v{version}' && "
        f"git push"
    )
    print("[GLIMPSE Deploy] Committing GLIMPSE changes in WSL repo...")
    try:
        commit_result = subprocess.run(
            ["wsl", "bash", "-lc", commit_cmd],
            check=True,
            capture_output=True,
            text=True,
        )
        if commit_result.stdout:
            print("[GLIMPSE Deploy] WSL Git commit/push stdout:")
            print(commit_result.stdout)
        if commit_result.stderr:
            print("[GLIMPSE Deploy] WSL Git commit/push stderr:", file=sys.stderr)
            print(commit_result.stderr, file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print("[GLIMPSE Deploy] WARNING: WSL Git commit/push failed (non-zero exit).", file=sys.stderr)
        print(e.stdout or "<stdout empty>", file=sys.stderr)
        print(e.stderr or "<stderr empty>", file=sys.stderr)
    except FileNotFoundError:
        print("[GLIMPSE Deploy] WARNING: wsl.exe not found during commit/push; skipping WSL Git sync.", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[GLIMPSE Deploy] WARNING: Unexpected error during WSL Git commit/push: {e}", file=sys.stderr)


def main() -> None:
    if len(sys.argv) < 2:
        die("Missing version argument. Usage: python glimpse_deploy_trampoline.py <version-tag>")

    version = sys.argv[1].strip()
    if not version:
        die("Empty version string is not allowed.")

    script_dir = Path(__file__).resolve().parent

    # ----- Resolve config -----
    cfg = _resolve_config(script_dir)

    agent_workspace = _normalize_win(cfg["glimpseAgentWorkspace"])
    staging_subdir = cfg["glimpseStagingSubdir"]
    staging_dir = os.path.join(agent_workspace, staging_subdir) if agent_workspace else ""
    source_tome = _normalize_win(cfg["glimpseCacheSource"])
    staging_target = os.path.join(staging_dir, "glimpse-cache-client.js") if staging_dir else ""
    workflowy_app_asar = _normalize_win(cfg["workflowyAppAsar"])
    electron_cache_dirs = [_normalize_win(p) for p in cfg["workflowyElectronCacheDirs"]]
    wsl_repo_dir = cfg["glimpseWslRepoDir"]

    # ----- Validate required fields -----
    if not source_tome:
        die(
            "glimpseCacheSource not configured. Set 'paths.glimpseCacheSource' in your "
            "workflowy_nexus.config.local.json to the absolute path of "
            "glimpse-cache-client.js. See script docstring for config resolution order."
        )
    if not workflowy_app_asar:
        die(
            "workflowyAppAsar not configured and no smart default available "
            "(LOCALAPPDATA env var is unset). Set 'paths.workflowyAppAsar' explicitly."
        )
    if not agent_workspace:
        die(
            "glimpseAgentWorkspace not configured and no smart default available "
            "(LOCALAPPDATA env var is unset). Set 'paths.glimpseAgentWorkspace' explicitly."
        )

    asar_name = f"app.asar.v{version}"
    asar_path = os.path.join(agent_workspace, asar_name)

    print(f"[GLIMPSE Deploy] Starting deploy for version: {version}")
    print(f"[GLIMPSE Deploy] AgentWorkspace:        {agent_workspace}")
    print(f"[GLIMPSE Deploy] Staging dir:           {staging_dir}")
    print(f"[GLIMPSE Deploy] GLIMPSE source:        {source_tome}")
    print(f"[GLIMPSE Deploy] Workflowy app.asar:    {workflowy_app_asar}")
    print(f"[GLIMPSE Deploy] Output ASAR:           {asar_path}")
    print(f"[GLIMPSE Deploy] Electron cache dirs:   {len(electron_cache_dirs)} configured")

    # ----- Ensure AgentWorkspace exists -----
    try:
        os.makedirs(agent_workspace, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        die(f"Failed to create AgentWorkspace '{agent_workspace}': {e}")

    # Log base app.asar metadata for sanity checking
    try:
        app_stat = os.stat(workflowy_app_asar)
        print(
            f"[GLIMPSE Deploy] Base Workflowy app.asar: "
            f"size={app_stat.st_size} bytes, mtime={app_stat.st_mtime}"
        )
    except FileNotFoundError:
        die(f"Workflowy app.asar not found at {workflowy_app_asar}")

    # ----- Step 0: Refresh staging from currently installed Workflowy app.asar -----
    print("[GLIMPSE Deploy] Refreshing staging from current Workflowy app.asar...")

    if os.path.isdir(staging_dir):
        try:
            shutil.rmtree(staging_dir)
            print(f"[GLIMPSE Deploy]   Removed existing staging dir: {staging_dir}")
        except Exception as e:  # noqa: BLE001
            print(f"[GLIMPSE Deploy]   Warning: Could not clear staging dir: {e}")
    try:
        os.makedirs(staging_dir, exist_ok=True)
        print(f"[GLIMPSE Deploy]   Created staging dir: {staging_dir}")
    except Exception as e:  # noqa: BLE001
        die(f"Failed to create staging directory: {e}")

    current_app_asar = os.path.join(agent_workspace, "app.asar.current")
    try:
        shutil.copyfile(workflowy_app_asar, current_app_asar)
        print(f"[GLIMPSE Deploy]   Copied current app.asar to: {current_app_asar}")
    except Exception as e:  # noqa: BLE001
        die(f"Failed to copy current Workflowy app.asar into AgentWorkspace: {e}")

    # Extract via WSL — paths are passed through _to_wsl_path
    agent_workspace_wsl = _to_wsl_path(agent_workspace)
    extract_wsl_cmd = (
        f"cd '{agent_workspace_wsl}' && "
        f"npx asar extract app.asar.current {staging_subdir}"
    )
    extract_full_cmd = ["wsl", "bash", "-lc", extract_wsl_cmd]

    print("[GLIMPSE Deploy] Running WSL extract command:")
    print(f"  {' '.join(extract_full_cmd)}")

    try:
        extract_result = subprocess.run(
            extract_full_cmd, check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as e:
        print("[GLIMPSE Deploy] WSL npx asar extract failed.", file=sys.stderr)
        print("[GLIMPSE Deploy] STDOUT:", file=sys.stderr)
        print(e.stdout or "<empty>", file=sys.stderr)
        print("[GLIMPSE Deploy] STDERR:", file=sys.stderr)
        print(e.stderr or "<empty>", file=sys.stderr)
        die(f"npx asar extract returned exit code {e.returncode}")
    except FileNotFoundError:
        die("wsl.exe not found. Ensure WSL is installed and available on PATH.")

    if extract_result.stdout:
        print("[GLIMPSE Deploy] extract stdout:")
        print(extract_result.stdout)
    if extract_result.stderr:
        print("[GLIMPSE Deploy] extract stderr:")
        print(extract_result.stderr, file=sys.stderr)

    # ----- Patch index.html -----
    index_path = os.path.join(staging_dir, "index.html")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index_html = f.read()
    except FileNotFoundError:
        die(f"index.html not found in staging directory: {index_path}")

    if "glimpse-cache-client.js" not in index_html:
        print("[GLIMPSE Deploy] Injecting GLIMPSE script tag into index.html...")
        insert_tag = '    <script src="glimpse-cache-client.js"></script>\n  </body>'
        if "</body>" not in index_html:
            die("index.html does not contain </body> tag; cannot inject GLIMPSE script")
        index_html = index_html.replace("  </body>", insert_tag, 1)
        try:
            with open(index_path, "w", encoding="utf-8") as f:
                f.write(index_html)
        except Exception as e:  # noqa: BLE001
            die(f"Failed to write patched index.html: {e}")
    else:
        print("[GLIMPSE Deploy] GLIMPSE script tag already present in index.html; skipping injection.")

    # ----- Step 1: Clear Electron cache -----
    print("[GLIMPSE Deploy] Clearing Electron cache...")
    for cache_dir in electron_cache_dirs:
        if os.path.isdir(cache_dir):
            try:
                shutil.rmtree(cache_dir)
                print(f"[GLIMPSE Deploy]   Cleared: {cache_dir}")
            except Exception as e:  # noqa: BLE001
                print(f"[GLIMPSE Deploy]   Warning: Could not clear {cache_dir}: {e}")
        else:
            print(f"[GLIMPSE Deploy]   Skipped (not found): {cache_dir}")

    # ----- Step 2: Copy GLIMPSE source to Staging -----
    print(f"[GLIMPSE Deploy] Copying GLIMPSE source to staging: {source_tome} -> {staging_target}")
    try:
        shutil.copyfile(source_tome, staging_target)
    except Exception as e:  # noqa: BLE001
        die(f"Failed to copy GLIMPSE source to staging: {e}")

    # ----- Step 3: Pack with WSL npx asar -----
    pack_wsl_cmd = (
        f"cd '{agent_workspace_wsl}' && "
        f"npx asar pack {staging_subdir} {asar_name}"
    )
    pack_full_cmd = ["wsl", "bash", "-lc", pack_wsl_cmd]

    print(f"[GLIMPSE Deploy] Running WSL pack command:\n  {' '.join(pack_full_cmd)}")

    try:
        result = subprocess.run(pack_full_cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print("[GLIMPSE Deploy] WSL npx asar pack failed.", file=sys.stderr)
        print("[GLIMPSE Deploy] STDOUT:", file=sys.stderr)
        print(e.stdout or "<empty>", file=sys.stderr)
        print("[GLIMPSE Deploy] STDERR:", file=sys.stderr)
        print(e.stderr or "<empty>", file=sys.stderr)
        die(f"npx asar pack returned exit code {e.returncode}")
    except FileNotFoundError:
        die("wsl.exe not found. Ensure WSL is installed and available on PATH.")

    if result.stdout:
        print("[GLIMPSE Deploy] npx stdout:")
        print(result.stdout)
    if result.stderr:
        print("[GLIMPSE Deploy] npx stderr:")
        print(result.stderr, file=sys.stderr)

    if not os.path.isfile(asar_path):
        die(f"ASAR output file not found after pack: {asar_path}")

    # ----- Step 4: Copy into Workflowy resources -----
    try:
        print(f"[GLIMPSE Deploy] Copying {asar_path} -> {workflowy_app_asar}")
        shutil.copyfile(asar_path, workflowy_app_asar)
    except Exception as e:  # noqa: BLE001
        die(f"Failed to copy ASAR into Workflowy resources: {e}")

    print("[GLIMPSE Deploy] SUCCESS: Workflowy app.asar updated.")
    print("[GLIMPSE Deploy] Restart Workflowy Desktop to load the new GLIMPSE version.")

    # ----- Optional: WSL git sync -----
    sync_wsl_repo(version, source_tome, wsl_repo_dir)

    print("[GLIMPSE Deploy] NOTE: GLIMPSE source and this trampoline live wherever your config")
    print("[GLIMPSE Deploy]       points; if glimpseWslRepoDir is set, the WSL repo is kept in")
    print("[GLIMPSE Deploy]       sync on each deploy.")


if __name__ == "__main__":  # pragma: no cover
    main()
