# G3 screen recording: records ONLY the window titled "G3 Demo".
# Usage: powershell -ExecutionPolicy Bypass -File scripts\record_g3.ps1
# Output: docs\evidence\g3-demo.mp4

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$title = "G3 Demo"
$output = Join-Path $root "docs\evidence\g3-demo.mp4"
$seconds = 32

$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "venv not found: $python"
}

Write-Host "Starting demo window..." -ForegroundColor Cyan

$inner = '$Host.UI.RawUI.WindowTitle = "' + $title + '"; Set-Location "' + $root + '"; $env:PYTHONIOENCODING = "utf-8"; & "' + $python + '" "scripts\demo_g3.py"; Start-Sleep -Seconds 5'

$window = Start-Process powershell -ArgumentList "-NoExit", "-NoLogo", "-Command", $inner -PassThru

try {
    $shell = New-Object -ComObject WScript.Shell
    $focused = $false

    for ($attempt = 1; $attempt -le 20; $attempt++) {
        Start-Sleep -Milliseconds 500

        $window.Refresh()
        if ($window.MainWindowTitle -eq $title) {
            if ($shell.AppActivate($window.Id)) {
                $focused = $true
                break
            }
        }
    }

    if (-not $focused) {
        throw "Cannot focus window '$title' (pid $($window.Id)). Bring it to front manually and rerun."
    }

    Start-Sleep -Milliseconds 800

    Write-Host "Recording $seconds seconds..." -ForegroundColor Cyan

    & ffmpeg -y -loglevel error -f gdigrab -framerate 12 -i "title=$title" -t $seconds -pix_fmt yuv420p -movflags +faststart $output

    if ($LASTEXITCODE -ne 0) {
        throw "ffmpeg failed (exit=$LASTEXITCODE)"
    }

    Write-Host "Done: $output" -ForegroundColor Green
}
finally {
    if ($window -and -not $window.HasExited) {
        taskkill /F /T /PID $window.Id | Out-Null
    }
}
