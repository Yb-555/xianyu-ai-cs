param([string]$action = "start")

# Project root = parent of this scripts/ folder (works even if folder is moved)
$root = Split-Path $PSScriptRoot -Parent
$py = Join-Path $root ".venv\Scripts\python.exe"

function Kill-V2 {
    # Only kill THIS project's python (backend app.main + worker run_worker), not others (e.g. BtSoft)
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and ($_.CommandLine -like "*$root*") -and
            ($_.CommandLine -like '*app.main*' -or $_.CommandLine -like '*scripts.run_worker*')
        } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($action -eq "stop") {
    Write-Host "Stopping backend and worker..."
    Kill-V2
    Start-Sleep -Seconds 1
    Write-Host "Stopped."
    Start-Sleep -Seconds 1
    exit 0
}

# ---------- start ----------
Write-Host "[1/4] Cleaning stray processes (prevents duplicate workers)..."
Kill-V2
Start-Sleep -Milliseconds 800

Write-Host "[2/4] Starting backend (opens its own window; close it to stop backend)..."
Start-Process -FilePath $py -ArgumentList "-m", "app.main" -WorkingDirectory $root

Write-Host "[3/4] Waiting for backend to be ready..."
$ok = $false
for ($i = 0; $i -lt 40; $i++) {
    try { Invoke-RestMethod "http://127.0.0.1:8090/health" -TimeoutSec 2 | Out-Null; $ok = $true; break }
    catch { Start-Sleep -Milliseconds 700 }
}
if (-not $ok) {
    Write-Host "  Backend timed out. Check the new backend window for errors."
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "[4/4] Bringing Worker online (first run launches debug Edge, ~10s)..."
try {
    $r = Invoke-RestMethod "http://127.0.0.1:8090/api/worker/start" -Method Post -TimeoutSec 60
    if ($r.error) { Write-Host "  Worker note: $($r.error)" }
    else { Write-Host "  Worker online (PID $($r.pid))" }
}
catch { Write-Host "  Worker start failed: $_" }

Start-Process "http://127.0.0.1:8090"
Write-Host ""
Write-Host "Done. Admin page opened in browser. Backend runs in its own window."
Write-Host "To stop everything: double-click stop.bat"
Start-Sleep -Seconds 3
