<#
.SYNOPSIS
    一键启动 Personal Agent 开发环境（后端三件套 + 前端）。

.DESCRIPTION
    这个项目需要三个**常驻**后端进程（加上开发时的前端，一共四个）：

      API        uvicorn src.main:app                面板与接口        :8000
      Scheduler  python -m src.scheduler             到期扫描          ——
      Outbox     python -m src.core.outbox_service   通知投递          ——
      Frontend   npm run dev（frontend/）            开发界面          :5173

    少了 Outbox：通知会永远停在 pending，站内收件箱**永远是空的**。
    少了 Scheduler：到期任务不会被扫描，任务建了也不会跑。
    少了 API：面板打不开。
    详见 docs/RUNNING.md。

.PARAMETER DataDir
    数据目录。给了就用它（五个数据库全部落在那里），不碰项目根目录的真实数据。
    演示、联调、跑一次性实验都推荐配一个临时目录。

.PARAMETER ApiPort
    API 端口，默认 8000（前端 dev server 的代理写死指向 8000，改了要同步改
    frontend/vite.config.ts）。

.PARAMETER NoNtfy
    清空 NTFY_TOPIC。这样即使产生了站长的通知，投递也会失败而不是真的推到
    手机上 —— 本地联调的安全开关。

.PARAMETER NoFrontend
    只起后端三件套。

.PARAMETER Stop
    停掉上一次启动的进程（读 .run/pids.txt）。窗口被强杀、进程留在后台没人管时用这个收尾。

.EXAMPLE
    .\scripts\run-dev.ps1 -DataDir "$env:TEMP\pa-demo" -NoNtfy

.EXAMPLE
    .\scripts\run-dev.ps1 -Stop
#>

param(
    [string]$DataDir = "",
    [int]$ApiPort = 8000,
    [switch]$NoFrontend,
    [switch]$NoNtfy,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$runDir = Join-Path $repo ".run"
$pidFile = Join-Path $runDir "pids.txt"


# =====================================
# -Stop：清理上次的进程
# =====================================

if ($Stop) {
    if (-not (Test-Path $pidFile)) {
        Write-Host "没有找到 $pidFile —— 看起来没有正在跑的实例。"
        exit 0
    }

    Get-Content $pidFile | ForEach-Object {
        $parts = $_ -split "="

        if ($parts.Count -lt 2) { return }

        $name = $parts[0]
        $procId = $parts[1]

        & taskkill /T /F /PID $procId 2>$null | Out-Null

        if ($LASTEXITCODE -eq 0) {
            Write-Host ("  已停止 {0}（PID {1}）" -f $name, $procId)
        } else {
            Write-Host ("  {0}（PID {1}）已经不在了" -f $name, $procId)
        }
    }

    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    exit 0
}


# =====================================
# 前置检查
# =====================================

$python = Join-Path $repo ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Warning "找不到虚拟环境 Python：$python"
    Write-Warning "先建虚拟环境并装依赖：python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt"

    $fallback = "E:\AppData\Local\Programs\Python\Python312\python.exe"

    if (Test-Path $fallback) {
        Write-Host "  改用系统 Python：$fallback"
        $python = $fallback
    } else {
        exit 1
    }
}

if (-not (Test-Path $runDir)) {
    New-Item -ItemType Directory -Path $runDir -Force | Out-Null
}

if ($DataDir) {
    if (-not (Test-Path $DataDir)) {
        New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
    }

    $env:PERSONAL_AGENT_DATA_DIR = (Resolve-Path $DataDir).Path
    Write-Host "数据目录：$env:PERSONAL_AGENT_DATA_DIR（隔离，不动项目里的真实数据）"
} else {
    Remove-Item Env:\PERSONAL_AGENT_DATA_DIR -ErrorAction SilentlyContinue
    Write-Host "数据目录：项目根目录（**真实数据**）"
}

if ($NoNtfy) {
    $env:NTFY_TOPIC = ""
    Write-Host "NTFY_TOPIC 已清空：通知不会真的推到手机"
}


# =====================================
# 端口检查
# =====================================

$ports = @($ApiPort)

if (-not $NoFrontend) {
    $ports += 5173
}

foreach ($port in $ports) {
    $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue

    if ($busy) {
        Write-Warning "端口 $port 已被占用（PID $($busy.OwningProcess -join ','))。"

        if ($port -eq $ApiPort) {
            Write-Warning "换端口：-ApiPort 8010（记得同步改 frontend/vite.config.ts 的代理）"
        } else {
            Write-Warning "前端端口被占：先关掉旧的 dev server，或用 -NoFrontend"
        }

        exit 1
    }
}


# =====================================
# 启动
# =====================================

$services = @()

function Start-AgentService {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [string]$Port
    )

    $out = Join-Path $runDir "$Name.out.log"
    $err = Join-Path $runDir "$Name.err.log"

    $proc = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $out `
        -RedirectStandardError $err

    $script:services += [pscustomobject]@{
        Name    = $Name
        Port    = $Port
        Process = $proc
    }

    Write-Host ("  [启动] {0,-9} PID {1,-7} 端口 {2}" -f $Name, $proc.Id, $Port)
}

Write-Host ""
Write-Host "启动进程："

Start-AgentService -Name "api" -FilePath $python `
    -Arguments @("-m", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", "$ApiPort") `
    -WorkingDirectory $repo -Port "$ApiPort"

Start-AgentService -Name "scheduler" -FilePath $python `
    -Arguments @("-m", "src.scheduler") `
    -WorkingDirectory $repo -Port "-"

Start-AgentService -Name "outbox" -FilePath $python `
    -Arguments @("-m", "src.core.outbox_service") `
    -WorkingDirectory $repo -Port "-"

if (-not $NoFrontend) {
    Start-AgentService -Name "frontend" -FilePath "cmd.exe" `
        -Arguments @("/c", "npm run dev -- --host 127.0.0.1 --port 5173 --strictPort") `
        -WorkingDirectory (Join-Path $repo "frontend") -Port "5173"
}

$services | ForEach-Object {
    "{0}={1}" -f $_.Name, $_.Process.Id
} | Set-Content -Path $pidFile -Encoding ASCII


# =====================================
# 等 API 就绪
# =====================================

Write-Host ""
Write-Host "等待 API 就绪……"

$ready = $false

for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 700

    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health" -TimeoutSec 2

        if ($health.status -eq "ok") {
            $ready = $true
            break
        }
    } catch {
        # 还没起来，继续等
    }
}

if ($ready) {
    Write-Host "  API 就绪 ✅"
} else {
    Write-Warning "  API 20 秒内没就绪，看 .run\api.err.log"
}


# =====================================
# 状态与自检提示
# =====================================

Write-Host ""
Write-Host "----------------------------------------"
Write-Host "面板：   http://127.0.0.1:5173"
Write-Host "接口：   http://127.0.0.1:$ApiPort/docs"
Write-Host "日志：   .run\*.out.log / .run\*.err.log"
Write-Host "停止：   在本窗口按 Ctrl+C（会一并停掉全部子进程）"
Write-Host "收尸：   .\scripts\run-dev.ps1 -Stop"
Write-Host "----------------------------------------"
Write-Host ""
Write-Host "自检（在另一个窗口跑）："
Write-Host "  cd `"$repo`""
Write-Host "  .\.venv\Scripts\python.exe scripts\check_services.py"
Write-Host ""
Write-Host "按 Ctrl+C 停止全部进程。"
Write-Host ""


# =====================================
# 看护 + 收尾
# =====================================

try {
    while ($true) {
        Start-Sleep -Seconds 3

        foreach ($svc in $services) {
            if ($svc.Process.HasExited) {
                Write-Warning ("{0} 已退出（exit {1}）—— 看 .run\{0}.err.log" -f $svc.Name, $svc.Process.ExitCode)
            }
        }
    }
} finally {
    Write-Host ""
    Write-Host "正在停止子进程……"

    foreach ($svc in $services) {
        if ($svc.Process -and -not $svc.Process.HasExited) {
            & taskkill /T /F /PID $svc.Process.Id 2>$null | Out-Null
            Write-Host ("  已停止 {0}（PID {1}）" -f $svc.Name, $svc.Process.Id)
        }
    }

    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host "全部停止。"
}
