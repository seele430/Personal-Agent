# G3 screen recording: records ONLY the window titled "G3 Demo".
# Usage: powershell -ExecutionPolicy Bypass -File scripts\record_g3.ps1
# Output: docs\evidence\g3-demo.mp4
#
# ASCII only on purpose: PowerShell 5.1 reads a BOM-less UTF-8 script as the
# system code page, which corrupts non-ASCII text and can even break parsing.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$title = "G3 Demo"
$output = Join-Path $root "docs\evidence\g3-demo.mp4"
$seconds = 32

$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "venv not found: $python"
}

# The demo runs from a bootstrap file in TEMP (that path has no spaces).
# Start-Process -ArgumentList mangles quoted paths containing spaces, and the
# project path "C:\Users\123\Personal Agent" has one.
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

    # AppActivate often fails for console windows; use Win32 directly, and
    # require the window to actually BE the foreground window before recording.
    # gdigrab captures a screen region, so a covered window records as black.
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public class G3Win32 {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
"@

    $window.Refresh()
    $hwnd = $window.MainWindowHandle
    $foreground = $false

    if ($hwnd -ne [IntPtr]::Zero) {
        [G3Win32]::ShowWindow($hwnd, 3) | Out-Null   # SW_MAXIMIZE

        Write-Host ""
        Write-Host "请把 'G3 Demo' 窗口点到最前面（它可能被 VS Code 挡着）。" -ForegroundColor Yellow
        Write-Host "脚本会最多等 3 分钟，检测到它在前台后自动开始录制。" -ForegroundColor Yellow
        Write-Host ""

        $deadline = (Get-Date).AddSeconds(180)

        while ((Get-Date) -lt $deadline) {
            [G3Win32]::SetForegroundWindow($hwnd) | Out-Null
            Start-Sleep -Milliseconds 300

            if ([G3Win32]::GetForegroundWindow() -eq $hwnd) {
                $foreground = $true
                break
            }
        }
    }

    if (-not $foreground) {
        $fg = [G3Win32]::GetForegroundWindow()
        throw ("The '$title' window is not in the foreground (window=$hwnd, foreground=$fg). " +
            "Click the '$title' window, then run again.")
    }

    Start-Sleep -Milliseconds 800

    Write-Host "Recording $seconds seconds..." -ForegroundColor Cyan

    # Keep this on ONE line: a trailing backtick continuation is fragile and
    # has already been mis-parsed once (the -vf flag became a separate command).
    # The scale filter rounds width/height down to even numbers because h264
    # rejects odd dimensions (captured window was 1481x761).
    & ffmpeg -y -loglevel error -f gdigrab -framerate 12 -i "title=$title" -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" -t $seconds -pix_fmt yuv420p -movflags +faststart $output

    if ($LASTEXITCODE -ne 0) {
        throw "ffmpeg failed (exit=$LASTEXITCODE)"
    }

    $size = (Get-Item $output).Length / 1MB
    Write-Host ("Done: {0} ({1:N2} MB)" -f $output, $size) -ForegroundColor Green
}
finally {
    if ($window -and -not $window.HasExited) {
        taskkill /F /T /PID $window.Id | Out-Null
    }
}
