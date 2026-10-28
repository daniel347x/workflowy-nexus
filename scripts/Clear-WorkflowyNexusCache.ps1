# Clear-WorkflowyNexusCache.ps1
#
# Surgical, fail-closed cache reset for workflowy-nexus-mcp-tools.
#
# IMPORTANT SAFETY MODEL
#   Direct recursive deletion of a live uv archive is NOT atomic on Windows.
#   Remove-Item can delete Lib\site-packages first, then hit a locked python.exe
#   and stop, leaving a launcher-only archive that uv will continue to reuse.
#
#   This script therefore:
#     1. Recognizes both healthy archives (workflowy_mcp package present) and
#        malformed archives (Workflowy launcher remains but package is absent).
#     2. Performs a process preflight BEFORE mutating anything. If any process
#        is using a candidate archive, the script fails closed with its PID and
#        command line. No cache entry is touched.
#     3. Renames each cache entry to a unique quarantine name BEFORE recursive
#        deletion. Rename is the atomic eligibility gate: if Windows refuses it,
#        the original cache key remains unchanged. If later deletion fails, uv
#        still cannot reuse the quarantined entry.
#     4. Also sweeps environments-v2 (uvx tool environments) LAYOUT-AGNOSTICALLY.
#        On Linux/WSL those entries are symlinks into archive-v0; on Windows the
#        same directory was observed holding plain non-reparse hash directories.
#        Rather than assume a shape, it flags (a) reparse points whose target
#        names a candidate archive and (b) directories carrying a workflowy
#        marker. Reparse points are removed LINK-ONLY, never recursively --
#        recursing through a junction can destroy the TARGET's contents.
#
# Usage:
#   1. Fully stop every MCP host and detached Workflowy/CARTO worker.
#   2. Run this script.
#   3. Restart the MCP host(s).
#
# Safe when nothing is found: prints a yellow notice and exits 0.

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$cacheRoot = Join-Path $env:LOCALAPPDATA 'uv\cache'
$archiveRoot = Join-Path $cacheRoot 'archive-v0'
$gitCheckoutsRoot = Join-Path $cacheRoot 'git-v0\checkouts'
$envRoot = Join-Path $cacheRoot 'environments-v2'
$quarantinePrefix = '.workflowy-nexus-quarantine-'

if (-not (Test-Path $cacheRoot)) {
    Write-Host "uv cache root not found at $cacheRoot -- nothing to do." -ForegroundColor Yellow
    return
}

function Test-WorkflowyArchive {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (
        (Test-Path (Join-Path $Path 'Lib\site-packages\workflowy_mcp')) -or
        (Test-Path (Join-Path $Path 'Scripts\workflowy-mcp.exe')) -or
        (Test-Path (Join-Path $Path 'Scripts\workflowy-glimpse-broker.exe'))
    )
}

function Move-ToQuarantine {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $leaf = Split-Path -Leaf $Path
    $quarantineLeaf = "$quarantinePrefix$leaf-$([Guid]::NewGuid().ToString('N'))"
    $quarantinePath = Join-Path $Parent $quarantineLeaf

    # Rename first. Unlike recursive deletion, this is an all-or-nothing gate
    # for the original cache key: on failure, the source remains untouched.
    Rename-Item -LiteralPath $Path -NewName $quarantineLeaf -ErrorAction Stop

    try {
        Remove-Item -Recurse -Force -LiteralPath $quarantinePath -ErrorAction Stop
        return [pscustomobject]@{
            Deleted = $true
            Quarantined = $false
            Path = $quarantinePath
        }
    } catch {
        Write-Host "    QUARANTINED but could not fully delete: $quarantinePath" -ForegroundColor Yellow
        Write-Host "    $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "    uv cannot reuse the original $Label cache key; remove this quarantine later." -ForegroundColor Yellow
        return [pscustomobject]@{
            Deleted = $false
            Quarantined = $true
            Path = $quarantinePath
        }
    }
}

Write-Host ""
Write-Host "=== Clearing workflowy-nexus-mcp-tools entries from uv cache ===" -ForegroundColor Cyan
Write-Host "uv cache root: $cacheRoot"
Write-Host ""

# Discover archive candidates BEFORE any mutation. Include launcher-only damaged
# archives; this is the exact state a partial Windows deletion can leave behind.
$archiveCandidates = @()
if (Test-Path $archiveRoot) {
    $archiveCandidates = @(
        Get-ChildItem $archiveRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object {
                -not $_.Name.StartsWith($quarantinePrefix) -and
                (Test-WorkflowyArchive -Path $_.FullName)
            }
    )
}

# Fail-closed process preflight. Match process ExecutablePath/CommandLine against
# the exact candidate archive path, not merely against generic python/uv names.
# This catches MCP servers, the singleton broker, and detached CARTO workers.
$activeUsers = @()
if ($archiveCandidates.Count -gt 0) {
    try {
        $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
        foreach ($candidate in $archiveCandidates) {
            $needle = $candidate.FullName.ToLowerInvariant()
            foreach ($proc in $processes) {
                $exe = [string]$proc.ExecutablePath
                $cmd = [string]$proc.CommandLine
                $haystack = ($exe + "`n" + $cmd).ToLowerInvariant()
                if ($haystack.Contains($needle)) {
                    $activeUsers += [pscustomobject]@{
                        Archive = $candidate.FullName
                        ProcessId = $proc.ProcessId
                        Name = $proc.Name
                        ExecutablePath = $exe
                        CommandLine = $cmd
                    }
                }
            }
        }
    } catch {
        Write-Host "PROCESS PREFLIGHT FAILED: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "No cache entries were touched. Refusing to continue without a trustworthy lock check." -ForegroundColor Red
        throw
    }
}

if ($activeUsers.Count -gt 0) {
    Write-Host "REFUSING TO CLEAR: Workflowy uv archive(s) are still in use." -ForegroundColor Red
    Write-Host "No cache entries were touched. Stop these processes/hosts and run the script again:" -ForegroundColor Red
    Write-Host ""
    foreach ($row in $activeUsers | Sort-Object ProcessId -Unique) {
        Write-Host "  PID $($row.ProcessId)  $($row.Name)" -ForegroundColor Yellow
        Write-Host "    archive: $($row.Archive)"
        if ($row.CommandLine) {
            Write-Host "    command: $($row.CommandLine)"
        } elseif ($row.ExecutablePath) {
            Write-Host "    executable: $($row.ExecutablePath)"
        }
    }
    throw "Workflowy uv cache is in use; fail-closed preflight aborted before mutation."
}

$archiveDeleted = 0
$archiveQuarantined = 0
$gitDeleted = 0
$gitQuarantined = 0

# --- Archive entries (healthy OR launcher-only malformed environments) --------

if (Test-Path $archiveRoot) {
    Write-Host "Scanning archive-v0\ for Workflowy package/launcher entries..."
    foreach ($candidate in $archiveCandidates) {
        $hasPackage = Test-Path (Join-Path $candidate.FullName 'Lib\site-packages\workflowy_mcp')
        $kind = if ($hasPackage) { 'healthy/package-present' } else { 'MALFORMED/launcher-only' }
        Write-Host "  archive: $($candidate.Name)  [$kind]  (modified $($candidate.LastWriteTime))" -ForegroundColor Yellow
        try {
            $result = Move-ToQuarantine -Path $candidate.FullName -Parent $archiveRoot -Label 'archive'
            if ($result.Deleted) { $archiveDeleted++ }
            if ($result.Quarantined) { $archiveQuarantined++ }
        } catch {
            Write-Host "    FAILED before quarantine rename completed: $($_.Exception.Message)" -ForegroundColor Red
            Write-Host "    Original cache key remains unchanged; no partial recursive delete was attempted." -ForegroundColor Red
        }
    }
} else {
    Write-Host "(no archive-v0\ directory present)" -ForegroundColor DarkGray
}

Write-Host ""

# --- Git checkouts (cloned source) --------------------------------------------

$gitCandidates = @()
if (Test-Path $gitCheckoutsRoot) {
    Write-Host "Scanning git-v0\checkouts\ for workflowy-nexus-mcp-tools clones..."
    foreach ($hashDir in Get-ChildItem $gitCheckoutsRoot -Directory -ErrorAction SilentlyContinue) {
        if ($hashDir.Name.StartsWith($quarantinePrefix)) { continue }
        $matched = $false
        foreach ($commitDir in Get-ChildItem $hashDir.FullName -Directory -ErrorAction SilentlyContinue) {
            $pyproj = Join-Path $commitDir.FullName 'pyproject.toml'
            if (Test-Path $pyproj) {
                $content = Get-Content $pyproj -Raw -ErrorAction SilentlyContinue
                if ($content -and ($content -match 'name\s*=\s*"workflowy-nexus-mcp-tools"')) {
                    $matched = $true
                    break
                }
            }
        }
        if ($matched) { $gitCandidates += $hashDir }
    }

    foreach ($hashDir in $gitCandidates) {
        Write-Host "  git checkout: $($hashDir.Name)  (modified $($hashDir.LastWriteTime))" -ForegroundColor Yellow
        try {
            $result = Move-ToQuarantine -Path $hashDir.FullName -Parent $gitCheckoutsRoot -Label 'git checkout'
            if ($result.Deleted) { $gitDeleted++ }
            if ($result.Quarantined) { $gitQuarantined++ }
        } catch {
            Write-Host "    FAILED before quarantine rename completed: $($_.Exception.Message)" -ForegroundColor Red
            Write-Host "    Original git cache key remains unchanged." -ForegroundColor Red
        }
    }
} else {
    Write-Host "(no git-v0\checkouts\ directory present)" -ForegroundColor DarkGray
}

# --- environments-v2 (uvx tool environments) --------------------------------
#
# Layout is NOT stable across uv versions or platforms, so nothing here assumes
# one. Two levels are walked and exactly two things are flagged:
#   (a) a reparse point whose TARGET names a candidate archive (matched on the
#       full path or on the archive's leaf name, so both absolute and relative
#       targets work). Matching on the target string -- not on Test-Path -- means
#       links left dangling by the quarantine renames above are still caught.
#   (b) a plain directory that itself carries a workflowy marker.
# Arbitrary dangling links belonging to OTHER packages are deliberately NOT
# touched: over-reach here would damage unrelated tool environments.

$envRemoved = 0
$envQuarantined = 0
$envFlagged = 0

if (Test-Path $envRoot) {
    Write-Host "Scanning environments-v2\ for workflowy entries (layout-agnostic, depth 2)..."

    $candidatePaths = @($archiveCandidates | ForEach-Object { $_.FullName.ToLowerInvariant().TrimEnd('\') })
    $candidateLeafs = @($archiveCandidates | ForEach-Object { $_.Name.ToLowerInvariant() })

    $items = @(Get-ChildItem $envRoot -Force -Recurse -Depth 2 -ErrorAction SilentlyContinue)
    Write-Host "  ($($items.Count) entries examined)"

    foreach ($item in $items) {
        if ($item.Name.StartsWith($quarantinePrefix)) { continue }

        $isReparse = ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        $flag = $null

        if ($isReparse) {
            $targetText = ''
            try {
                $t = (Get-Item $item.FullName -Force -ErrorAction Stop).Target
                if ($t) { $targetText = (@($t) -join ' ').ToLowerInvariant() }
            } catch { }

            if ($targetText) {
                foreach ($c in $candidatePaths) {
                    if ($targetText.Contains($c)) { $flag = 'reparse-into-candidate'; break }
                }
                if (-not $flag) {
                    $leaf = Split-Path $targetText.TrimEnd('/\') -Leaf
                    foreach ($l in $candidateLeafs) {
                        if ($leaf -eq $l) { $flag = 'reparse-into-candidate'; break }
                    }
                }
            }
        }
        elseif ($item.PSIsContainer -and (Test-WorkflowyArchive -Path $item.FullName)) {
            $flag = 'marked-directory'
        }

        if (-not $flag) { continue }
        $envFlagged++
        Write-Host "  [$flag] $($item.FullName)" -ForegroundColor Yellow

        if ($isReparse) {
            # LINK-ONLY removal. Non-recursive BY DESIGN -- a recursive delete
            # through a junction can destroy the target's contents.
            try {
                if ($item.PSIsContainer) { [System.IO.Directory]::Delete($item.FullName, $false) }
                else { [System.IO.File]::Delete($item.FullName) }
                $envRemoved++
            } catch {
                Write-Host "    COULD NOT REMOVE LINK: $($_.Exception.Message)" -ForegroundColor Red
            }
        } else {
            try {
                $result = Move-ToQuarantine -Path $item.FullName -Parent $item.Parent.FullName -Label 'environments-v2 entry'
                if ($result.Deleted) { $envRemoved++ }
                if ($result.Quarantined) { $envQuarantined++ }
            } catch {
                Write-Host "    FAILED before quarantine rename completed: $($_.Exception.Message)" -ForegroundColor Red
            }
        }
    }

    if ($envFlagged -eq 0) {
        Write-Host "  (no workflowy entries in environments-v2 -- area examined, clean)" -ForegroundColor DarkGray
    }
} else {
    Write-Host "(no environments-v2\ directory present)" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
Write-Host "  Archive entries deleted:     $archiveDeleted"
Write-Host "  Archive entries quarantined: $archiveQuarantined"
Write-Host "  Git checkouts deleted:       $gitDeleted"
Write-Host "  Git checkouts quarantined:   $gitQuarantined"
Write-Host "  environments-v2 removed:     $envRemoved"
Write-Host "  environments-v2 quarantined: $envQuarantined"

if (($archiveDeleted + $archiveQuarantined + $gitDeleted + $gitQuarantined + $envRemoved + $envQuarantined) -eq 0) {
    Write-Host ""
    Write-Host "Nothing was deleted. The Workflowy uv cache may already be empty." -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "Cache keys cleared safely. The next uvx invocation will re-fetch/rebuild." -ForegroundColor Green
    if (($archiveQuarantined + $gitQuarantined) -gt 0) {
        Write-Host "One or more entries remain under quarantine names, but uv cannot reuse them." -ForegroundColor Yellow
    }
}
