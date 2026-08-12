# Local watchdog: if :8081 is down or not ready, relaunch the worker.
# Install (Task Scheduler, every 2 min, run whether user is logged on or not):
#
#   schtasks /Create /TN "ShapefulEnsureWorker" /SC MINUTE /MO 2 ^
#     /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\jorda\workspace\buildplate\worker\ensure-worker.ps1" ^
#     /RL HIGHEST
#
# Or double-click / run manually after a failed remote restart.
param(
  [switch]$Pull,
  [switch]$Force,
  [int]$ReadyGraceSec = 0
)

$ErrorActionPreference = "Continue"
$WorkerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $WorkerDir "cache"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "ensure-worker.log"
$HealthUrl = "http://127.0.0.1:8081/health"
$Relaunch = Join-Path $WorkerDir "relaunch-worker.ps1"

function Write-Log($msg) {
  $line = "{0} | {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
  try { Add-Content -Path $Log -Value $line -Encoding UTF8 } catch {}
  Write-Host $line
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

Write-Log "ensure-worker check Force=$Force Pull=$Pull"

if (-not $Force) {
  $h = Test-WorkerHealth
  if ($h -and $h.ready) {
    Write-Log "already ready head=$($h.head) pid=$($h.pid) - noop"
    exit 0
  }
  if ($h -and -not $h.ready -and $ReadyGraceSec -gt 0) {
    Write-Log "loading (ready=false); grace ${ReadyGraceSec}s..."
    Start-Sleep -Seconds $ReadyGraceSec
    $h2 = Test-WorkerHealth
    if ($h2 -and $h2.ready) {
      Write-Log "became ready during grace - noop"
      exit 0
    }
  }
  if ($h) {
    Write-Log "health reachable but not ready - will relaunch"
  } else {
    Write-Log "health unreachable - will relaunch"
  }
}

if (-not (Test-Path $Relaunch)) {
  Write-Log "ERROR: missing $Relaunch"
  exit 2
}

# Do not use $args - reserved automatic variable in PowerShell.
$relaunchArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $Relaunch)
if ($Pull) { $relaunchArgs += "-Pull" }
Write-Log "invoking relaunch-worker.ps1 $($relaunchArgs -join ' ')"
$p = Start-Process -FilePath "powershell.exe" -ArgumentList $relaunchArgs -WorkingDirectory $WorkerDir -Wait -PassThru
Write-Log "relaunch exit code=$($p.ExitCode)"
exit $p.ExitCode
