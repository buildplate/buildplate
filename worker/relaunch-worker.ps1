# Detached relaunch helper (called by /v1/admin/update|restart|hard-restart).
# Kill the worker FIRST so a hung paint/git never blocks restart.
param(
  [switch]$Pull,
  [string]$Repo = "",
  [string]$Ref = "main",
  [int]$ParentPid = 0,
  [int]$HealthTimeoutSec = 180,
  [int]$StartRetries = 2
)

$ErrorActionPreference = "Continue"
$WorkerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Repo) { $Repo = Split-Path -Parent $WorkerDir }
$LogDir = Join-Path $WorkerDir "cache"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "relaunch.log"
$Port = 8081
$HealthUrl = "http://127.0.0.1:$Port/health"

function Write-Log($msg) {
  $line = "{0} | {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
  try {
    Add-Content -Path $Log -Value $line -Encoding UTF8 -ErrorAction Stop
  } catch {
    try { [System.IO.File]::AppendAllText($Log, $line + "`r`n") } catch {}
  }
  try { Write-Host $line } catch {}
}

function Get-ListenersOnPort([int]$ListenPort) {
  $pids = @()
  try {
    $conns = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
      $pids = @($conns | Select-Object -ExpandProperty OwningProcess -Unique)
    }
  } catch {}
  if (-not $pids -or $pids.Count -eq 0) {
    try {
      $lines = netstat -ano -p tcp | Select-String ":$ListenPort\s+.*LISTENING"
      foreach ($line in $lines) {
        if ($line -match '\s(\d+)\s*$') { $pids += [int]$Matches[1] }
      }
      $pids = @($pids | Select-Object -Unique)
    } catch {}
  }
  return $pids
}

function Stop-PidSafe([int]$ProcId, [string]$Why) {
  if ($ProcId -le 0) { return }
  try {
    $p = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
    if (-not $p) {
      Write-Log "PID $ProcId already gone ($Why)"
      return
    }
    Write-Log "Stopping PID $ProcId ($Why) name=$($p.ProcessName)"
    Stop-Process -Id $ProcId -Force -ErrorAction SilentlyContinue
    & taskkill.exe /F /PID $ProcId 2>&1 | ForEach-Object { Write-Log "taskkill: $_" }
  } catch {
    Write-Log "Stop-PidSafe($ProcId) failed: $_"
  }
}

function Stop-ShapefulWorkers {
  Write-Log "Stopping buildplate workers (ParentPid=$ParentPid)..."

  if ($ParentPid -gt 0) {
    Stop-PidSafe $ParentPid "parent from admin API"
  }

  # Any process whose command line mentions buildplate_worker.py (python.exe or WinPortable).
  try {
    Get-CimInstance Win32_Process |
      Where-Object { $_.CommandLine -and ($_.CommandLine -match 'buildplate_worker\.py') } |
      ForEach-Object {
        Write-Log "CIM match PID $($_.ProcessId): $($_.CommandLine)"
        Stop-PidSafe ([int]$_.ProcessId) "command-line match"
      }
  } catch {
    Write-Log "CIM scan failed: $_"
  }

  # Anyone still holding :8081.
  foreach ($pidHold in (Get-ListenersOnPort $Port)) {
    Stop-PidSafe ([int]$pidHold) "listener on :$Port"
  }

  # Wait for port to free.
  for ($i = 0; $i -lt 20; $i++) {
    $left = @(Get-ListenersOnPort $Port)
    if ($left.Count -eq 0) { break }
    Write-Log "Port $Port still held by: $($left -join ','); waiting..."
    foreach ($pidHold in $left) { Stop-PidSafe ([int]$pidHold) "port still held" }
    Start-Sleep -Seconds 1
  }

  $left = @(Get-ListenersOnPort $Port)
  if ($left.Count -gt 0) {
    Write-Log "WARNING: port $Port still in use by $($left -join ',') after stop pass"
  } else {
    Write-Log "Port $Port is free"
  }
  Start-Sleep -Seconds 1
}

function Test-WorkerHealth {
  try {
    $resp = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 3
    if ($resp.StatusCode -ne 200) { return $null }
    return ($resp.Content | ConvertFrom-Json)
  } catch {
    return $null
  }
}

function Start-WorkerProcess {
  $bat = Join-Path $WorkerDir "start-worker.bat"
  if (-not (Test-Path $bat)) {
    Write-Log "ERROR: missing $bat"
    return $false
  }
  Write-Log "Starting worker via $bat"
  $env:BUILDPLATE_WORKER_NOPAUSE = "1"
  try {
    # Minimized console - survives after this script exits; bat writes start-worker.out.log.
    $p = Start-Process -FilePath "cmd.exe" `
      -ArgumentList "/c", "`"$bat`"" `
      -WorkingDirectory $WorkerDir `
      -WindowStyle Minimized `
      -PassThru
    Write-Log "start-worker.bat spawned PID=$($p.Id)"
    return $true
  } catch {
    Write-Log "Start-Process failed: $_"
    try {
      Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$bat`"" -WorkingDirectory $WorkerDir
      Write-Log "start-worker.bat spawned (fallback, no PassThru)"
      return $true
    } catch {
      Write-Log "ERROR: could not start worker: $_"
      return $false
    }
  }
}

function Wait-WorkerReady([int]$TimeoutSec) {
  Write-Log "Waiting up to ${TimeoutSec}s for $HealthUrl ready=true..."
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    $h = Test-WorkerHealth
    if ($h -and $h.ready) {
      Write-Log ("Worker READY head={0} pid={1} texgen={2}" -f $h.head, $h.pid, $h.texgen)
      return $true
    }
    if ($h) {
      Write-Log "health ok but ready=$($h.ready) (still loading...)"
    } else {
      Write-Log "health not reachable yet..."
    }
    Start-Sleep -Seconds 5
  }
  Write-Log "ERROR: worker did not become ready within ${TimeoutSec}s"
  return $false
}

# --- main ---
Write-Log "======== relaunch start Pull=$Pull Repo=$Repo Ref=$Ref ParentPid=$ParentPid ========"
Start-Sleep -Seconds 1

# Give parent a moment to finish HTTP response + begin exit.
if ($ParentPid -gt 0) {
  for ($i = 0; $i -lt 30; $i++) {
    $alive = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
    if (-not $alive) {
      Write-Log "Parent PID $ParentPid exited"
      break
    }
    Start-Sleep -Milliseconds 200
  }
}

Stop-ShapefulWorkers

if ($Pull) {
  $git = @(
    "C:\Program Files\Git\cmd\git.exe",
    "git.exe"
  ) | Where-Object { $_ -eq "git.exe" -or (Test-Path $_) } | Select-Object -First 1
  Write-Log "git pull via $git"
  try {
    & $git -C $Repo fetch origin 2>&1 | ForEach-Object { Write-Log $_ }
    & $git -C $Repo checkout $Ref 2>&1 | ForEach-Object { Write-Log $_ }
    & $git -C $Repo pull --ff-only origin $Ref 2>&1 | ForEach-Object { Write-Log $_ }
    & $git -C $Repo log -1 --oneline 2>&1 | ForEach-Object { Write-Log "HEAD $_" }
    & $git -C $Repo rev-parse --short HEAD 2>&1 | ForEach-Object { Write-Log "rev $_" }
  } catch {
    Write-Log "git failed: $_"
  }
}

if (Get-Command tailscale -ErrorAction SilentlyContinue) {
  Write-Log "Optional: ensuring Tailscale Funnel  127.0.0.1:$Port"
  & tailscale funnel --bg "http://127.0.0.1:$Port" 2>&1 | ForEach-Object { Write-Log $_ }
} else {
  Write-Log "tailscale CLI not on PATH - tunnel left as-is"
}

$ok = $false
for ($attempt = 1; $attempt -le $StartRetries; $attempt++) {
  Write-Log "Start attempt $attempt / $StartRetries"
  # Ensure port free before each attempt.
  foreach ($pidHold in (Get-ListenersOnPort $Port)) {
    Stop-PidSafe ([int]$pidHold) "pre-start port clear"
  }
  Start-Sleep -Seconds 1
  if (-not (Start-WorkerProcess)) { continue }
  if (Wait-WorkerReady $HealthTimeoutSec) {
    $ok = $true
    break
  }
  Write-Log "Start attempt $attempt failed readiness - killing and retrying"
  Stop-ShapefulWorkers
}

if ($ok) {
  Write-Log "======== relaunch SUCCESS ========"
  exit 0
}

Write-Log "======== relaunch FAILED - run ensure-worker.ps1 or start-worker.bat on the PC ========"
exit 1
