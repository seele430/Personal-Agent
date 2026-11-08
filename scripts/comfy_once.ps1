# One-shot ComfyUI generation: safe start -> one image -> always stop.
#
# GPU protection (2026-11-08 0x116 VIDEO_TDR_FAILURE): this wrapper refuses
# steps > 30 before starting anything, starts the server through the safe
# launcher (free VRAM >= 3 GB, --lowvram --reserve-vram 1), and stops the
# server in a finally block even when generation fails.
#
#   .\scripts\comfy_once.ps1 -Prompt "a cat sitting on a chair" -Image input.png -Steps 8
#   .\scripts\comfy_once.ps1 -Prompt "..." -NoStop     # keep server for debugging

param(
    [Parameter(Mandatory = $true)][string]$Prompt,
    [string]$Image = "input.png",
    [int]$Steps = 8,
    [int]$Port = 8188,
    [string]$ComfyRoot = "C:\Users\123\Documents\deepseek-harness\default-workspace",
    [switch]$NoStop
)

$ErrorActionPreference = "Stop"

$py = Join-Path $ComfyRoot "comfy-agent\.venv\Scripts\python.exe"
$safe = Join-Path $ComfyRoot "comfy-agent\scripts\start-comfyui-safe.ps1"
$once = Join-Path $PSScriptRoot "comfy_once.py"
$inputDir = Join-Path $ComfyRoot "comfy-run\ComfyUI\input"

# --- guard: steps before anything starts --------------------------------
if ($Steps -lt 1 -or $Steps -gt 30) {
    Write-Host "steps must be 1..30 (GPU protection); got $Steps" -ForegroundColor Red
    exit 2
}

if (-not (Test-Path $py)) { Write-Host "python missing: $py" -ForegroundColor Red; exit 2 }
if (-not (Test-Path $safe)) { Write-Host "safe launcher missing: $safe" -ForegroundColor Red; exit 2 }
if (-not (Test-Path $once)) { Write-Host "comfy_once.py missing: $once" -ForegroundColor Red; exit 2 }

$env:PA_COMFY_URL = "http://127.0.0.1:$Port"
$env:PA_COMFY_TIMEOUT = "120"
$env:PA_COMFY_INPUT_DIR = $inputDir

try {
    Write-Host "starting ComfyUI through the safe launcher..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File $safe -Port $Port

    if ($LASTEXITCODE -ne 0) {
        Write-Host "safe launcher refused to start (exit=$LASTEXITCODE); not generating" -ForegroundColor Red
        exit $LASTEXITCODE
    }

    Write-Host "generating: steps=$Steps image=$Image"
    & $py $once --prompt $Prompt --image $Image --steps $Steps

    if ($LASTEXITCODE -ne 0) {
        Write-Host "generation failed (exit=$LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }

    exit 0
}
finally {
    if (-not $NoStop) {
        Write-Host "stopping ComfyUI..."
        & powershell -NoProfile -ExecutionPolicy Bypass -File $safe -Stop
    }
}
