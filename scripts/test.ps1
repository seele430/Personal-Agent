param(
    [Parameter(Position = 0)]
    [ValidateSet('check', 'audit', 'unit', 'integration', 'api', 'backend', 'frontend', 'all')]
    [string]$Suite = 'backend'
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# Backend tests always go through the guarded runner (SQLite + socket guards).
$Runner = Join-Path $Root 'scripts\run_guarded_tests.py'

# Static audit does not execute tests, so it uses the plain runner.
$AuditRunner = Join-Path $Root 'scripts\run_tests.py'

$Python = Join-Path $Root '.venv\Scripts\python.exe'

# Do not fall back to PATH python: on this machine it is the Microsoft Store stub.
if (-not (Test-Path $Python)) {
    throw "Virtual environment interpreter not found: $Python"
}


function Invoke-Checked {
    param(
        [string]$Title,
        [string]$Program,
        [string[]]$Argv
    )

    Write-Host ''
    Write-Host "Running: $Title"

    & $Program @Argv

    if ($LASTEXITCODE -ne 0) {
        throw "$Title failed with exit code $LASTEXITCODE"
    }

    Write-Host "PASS: $Title" -ForegroundColor Green
}


function Invoke-Audit {
    Invoke-Checked -Title 'Isolation audit' -Program $Python -Argv @($AuditRunner, 'audit')
}


function Invoke-Backend {
    param([string]$Layer)

    Invoke-Checked -Title "Python $Layer (guarded)" -Program $Python -Argv @($Runner, $Layer)
}


function Invoke-Frontend {
    $Frontend = Join-Path $Root 'frontend'

    if (-not (Test-Path $Frontend)) {
        throw 'Frontend directory not found'
    }

    $Npm = (Get-Command npm.cmd -ErrorAction Stop).Source
    $Npx = (Get-Command npx.cmd -ErrorAction Stop).Source

    Push-Location $Frontend

    try {
        Invoke-Checked -Title 'Frontend build' -Program $Npm -Argv @('run', 'build')
        Invoke-Checked -Title 'Playwright tests' -Program $Npx -Argv @('playwright', 'test')
    }
    finally {
        Pop-Location
    }
}


# Keep this list in sync with safe_environment in scripts\run_guarded_tests.py.
$TestEnvironment = [ordered]@{
    'PERSONAL_AGENT_TEST_MODE' = '1'
    'OPENAI_API_KEY'           = 'personal-agent-invalid-test-key'
    'OPENAI_BASE_URL'          = 'https://api.deepseek.com'
    'MODEL'                    = 'deepseek-flash'
    'NTFY_SERVER'              = 'http://127.0.0.1:1'
    'NTFY_TOPIC'               = 'personal-agent-test-topic'
}

$Previous = @{}

foreach ($Name in $TestEnvironment.Keys) {
    $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
    [Environment]::SetEnvironmentVariable($Name, $TestEnvironment[$Name], 'Process')
}

$ExitCode = 0

Push-Location $Root

try {
    if ($Suite -eq 'check') {
        Invoke-Checked -Title 'Python syntax' -Program $Python -Argv @('-m', 'compileall', '-q', 'src', 'tests', 'scripts')
    }
    elseif ($Suite -eq 'audit') {
        Invoke-Audit
    }
    elseif ($Suite -in @('unit', 'integration', 'api', 'backend')) {
        Invoke-Backend $Suite
    }
    elseif ($Suite -eq 'frontend') {
        Invoke-Frontend
    }
    elseif ($Suite -eq 'all') {
        Invoke-Audit
        Invoke-Backend 'backend'
        Invoke-Frontend
    }
    else {
        throw "Unknown test suite: $Suite"
    }

    Write-Host ''
    Write-Host "SUCCESS: $Suite" -ForegroundColor Green
}
catch {
    $ExitCode = 1

    Write-Host ''
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    Pop-Location

    # Restore the caller's environment (null removes variables that did not exist before).
    foreach ($Name in $TestEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($Name, $Previous[$Name], 'Process')
    }
}

exit $ExitCode