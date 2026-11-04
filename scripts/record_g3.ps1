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

# The demo window is started from a bootstrap file in TEMP (no spaces in that
# path), because Start-Process -ArgumentList mangles quoted paths containing
# spaces -- the project path has a space in it.
$bootstrap = Join-Path $env:TEMP "g3-demo-run.ps1"

$script = @"
`$Host.UI.RawUI.WindowTitle = '$title'
Set-Location '$root'
`$env:PYTHONIOENCODING = 'utf-8'
& '$python' 'scripts\demo_g3.py'
Start-Sleep -Seconds 6
"@

[IO.File]::WriteAllText(
    $bootstrap,
    $script,
    (New-Object Text.UTF8Encoding $false)
)

Write-Host "Starting demo window..." -ForegroundColor Cyan

$window = Start-Process powershell -ArgumentList "-NoExit", "-NoLogo", "-File", $bootstrap -PassThru

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
        Write-Warning "Cannot focus window '$title' (pid $($window.Id)); recording anyway. If the video looks wrong, bring the window to the front and rerun."
    }

    Start-Sleep -Milliseconds 800

    Write-Host "Recording $seconds seconds..." -ForegroundColor Cyan

    # h264 要求宽高都是偶数；抓到的窗口尺寸可能是奇数（如 1481x761），
    # 所以统一向下取偶，再编码。
    & ffmpeg -y -loglevel error -f gdigrab -framerate 12 -i "title=$title" `
        -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" `
        -t $seconds -pix_fmt yuv420p -movflags +faststart $output

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
