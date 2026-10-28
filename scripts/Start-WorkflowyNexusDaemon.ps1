<#
.SYNOPSIS
  Start the NEXUS Workflowy MCP daemon in this PowerShell window. Leave the window open.

.DESCRIPTION
  Runs `uvx --from <source> workflowy-mcp --transport http`: the one daemon per machine
  (port 8766) that every agent attaches to, whether it runs on Windows or in WSL. The daemon
  starts the broker (port 8765) in its own window. Ctrl+C in this window stops the daemon.

  No MCP host (TypingMind connector, Claude Desktop) is needed to start it.

  Settings, first found wins: the parameters below; the environment variables
  WORKFLOWY_NEXUS_SOURCE, WORKFLOWY_API_KEY and WORKFLOWY_NEXUS_CONFIG; then the "workflowy"
  entry of Claude Desktop's claude_desktop_config.json, if present (its `--from` source and
  its env block). Nothing secret is stored in this script.

  After Clear-WorkflowyNexusCache.ps1 has cleared the uv cache, the first run here re-fetches
  and rebuilds the source, so a cleared cache always starts the latest commit of the source.

.PARAMETER Source
  The uvx `--from` source, e.g. git+https://github.com/daniel347x/workflowy-nexus.git

.PARAMETER ApiKey
  The Workflowy API key.

.PARAMETER NexusConfig
  Optional path to a NEXUS config file (WORKFLOWY_NEXUS_CONFIG).

.PARAMETER DryRun
  Print what would run (credentials masked) and start nothing.

.EXAMPLE
  & "E:\__daniel347x\__Dan_Root\__repos\workflowy-nexus-mcp-tools\scripts\Start-WorkflowyNexusDaemon.ps1"
#>
param(
    [string]$Source,
    [string]$ApiKey,
    [string]$NexusConfig,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$defaultSource = 'git+https://github.com/daniel347x/workflowy-nexus.git'

# Claude Desktop's Workflowy entry, if any.
$desktop = $null
$desktopPath = Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'
if (Test-Path $desktopPath) {
    try {
        $servers = (Get-Content -Raw -Path $desktopPath | ConvertFrom-Json).mcpServers
        if ($servers -and ($servers.PSObject.Properties.Name -contains 'workflowy')) {
            $desktop = $servers.workflowy
        }
    } catch {
        Write-Host "Could not read $desktopPath ($($_.Exception.Message)); continuing without it." -ForegroundColor Yellow
    }
}

function Get-DesktopEnv([string]$Name) {
    if ($desktop -and ($desktop.PSObject.Properties.Name -contains 'env') -and $desktop.env -and
        ($desktop.env.PSObject.Properties.Name -contains $Name)) {
        return [string]$desktop.env.$Name
    }
    return $null
}

if (-not $Source) { $Source = $env:WORKFLOWY_NEXUS_SOURCE }
if (-not $Source -and $desktop -and ($desktop.PSObject.Properties.Name -contains 'args')) {
    $desktopArgs = @($desktop.args)
    $i = [array]::IndexOf($desktopArgs, '--from')
    if ($i -ge 0 -and ($i + 1) -lt $desktopArgs.Count) { $Source = [string]$desktopArgs[$i + 1] }
}
if (-not $Source) { $Source = $defaultSource }

if (-not $ApiKey) { $ApiKey = $env:WORKFLOWY_API_KEY }
if (-not $ApiKey) { $ApiKey = Get-DesktopEnv 'WORKFLOWY_API_KEY' }
if (-not $ApiKey) {
    throw 'No Workflowy API key: pass -ApiKey, or set WORKFLOWY_API_KEY, or keep it in the Claude Desktop "workflowy" entry.'
}

if (-not $NexusConfig) { $NexusConfig = $env:WORKFLOWY_NEXUS_CONFIG }
if (-not $NexusConfig) { $NexusConfig = Get-DesktopEnv 'WORKFLOWY_NEXUS_CONFIG' }

$maskedSource = $Source -replace '://[^@/]+@', '://***@'
Write-Host "NEXUS daemon: uvx --from $maskedSource workflowy-mcp --transport http"
if ($NexusConfig) { Write-Host "NEXUS config: $NexusConfig" }

$listener = @(Get-NetTCPConnection -State Listen -LocalPort 8766 -ErrorAction SilentlyContinue)
if ($listener.Count -gt 0) {
    Write-Host "A daemon is already listening on port 8766 (pid $($listener[0].OwningProcess)). Nothing started." -ForegroundColor Yellow
    exit 0
}

if ($DryRun) {
    Write-Host '(dry run: nothing started)' -ForegroundColor Cyan
    exit 0
}

$env:WORKFLOWY_API_KEY = $ApiKey
if ($NexusConfig) { $env:WORKFLOWY_NEXUS_CONFIG = $NexusConfig }
try { $Host.UI.RawUI.WindowTitle = 'NEXUS Workflowy MCP daemon (port 8766) - Ctrl+C stops it' } catch { }

& uvx --from $Source workflowy-mcp --transport http
exit $LASTEXITCODE
