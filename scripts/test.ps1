param(
    [Parameter(Position = 0)]
    [ValidateSet(
        'check',
        'unit',
        'integration',
        'api',
        'backend',
        'frontend',
        'all'
    )]
    [string]$Suite = 'backend'
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (
    Join-Path $PSScriptRoot '..'
)).Path

$Runner = Join-Path $Root 'scripts\run_tests.py'

$Python = Join-Path $Root '.venv\Scripts\python.exe'

if (-not (Test-Path $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
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


function Invoke-Backend {
    param([string]$Layer)

    Invoke-Checked `
        -Title "Python $Layer" `
        -Program $Python `
        -Argv @($Runner, $Layer)
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
        Invoke-Checked `
            -Title 'Frontend build' `
            -Program $Npm `
            -Argv @('run', 'build')

        Invoke-Checked `
            -Title 'Playwright tests' `
            -Program $Npx `
            -Argv @('playwright', 'test')
    }
    finally {
        Pop-Location
    }
}


$Names = @(
    'PERSONAL_AGENT_TEST_MODE',
    'OPENAI_API_KEY',
    'NTFY_SERVER'
)

$Previous = @{}

foreach ($Name in $Names) {
    $Previous[$Name] = (
        [Environment]::GetEnvironmentVariable(
            $Name,
            'Process'
        )
    )
}

$env:PERSONAL_AGENT_TEST_MODE = '1'
$env:OPENAI_API_KEY = 'personal-agent-invalid-test-key'
$env:NTFY_SERVER = 'http://127.0.0.1:1'

$ExitCode = 0

Push-Location $Root

try {
    if ($Suite -eq 'check') {

        Invoke-Checked `
            -Title 'Python syntax' `
            -Program $Python `
            -Argv @(
                '-m',
                'compileall',
                '-q',
                'src',
                'tests',
                'scripts'
            )

    }
    elseif (
        $Suite -eq 'unit' -or
        $Suite -eq 'integration' -or
        $Suite -eq 'api' -or
        $Suite -eq 'backend'
    ) {

        Invoke-Backend $Suite

    }
    elseif ($Suite -eq 'frontend') {

        Invoke-Frontend

    }
    elseif ($Suite -eq 'all') {

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
    Write-Host "FAILED: $($_.Exception.Message)" `
        -ForegroundColor Red
}
finally {
    Pop-Location

    foreach ($Name in $Names) {
        [Environment]::SetEnvironmentVariable(
            $Name,
            $Previous[$Name],
            'Process'
        )
    }
}

exit $ExitCode