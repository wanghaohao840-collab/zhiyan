param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('unit', 'real', 'full', 'regression')]
    [string]$Stage,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,39}$')]
    [string]$Run
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = 'D:\python_self_agent\venv\Scripts\python.exe'
$xml = Join-Path $repo ('.runtime\isolated-import-runtime\p6-{0}-{1}.xml' -f $Stage, $Run)
$basetemp = Join-Path $repo ('.runtime\pytest-isolated-runtime-p6-{0}-{1}' -f $Stage, $Run)
$selection = @()

switch ($Stage) {
    'unit' { $selection = @('tests/integration/test_isolated_import_processes.py', '-k', 'not real') }
    'real' { $selection = @('tests/integration/test_isolated_import_processes.py', '-k', 'real') }
    'full' { $selection = @('tests/integration/test_isolated_import_processes.py') }
    'regression' {
        $selection = @(
            'tests/integration/test_distributed_import_worker.py',
            'tests/integration/test_isolated_import_api.py',
            'tests/integration/test_distributed_import_facades.py',
            'tests/test_isolated_import_runtime.py',
            'tests/api/test_auth_routes.py',
            'tests/api/test_import_routes.py',
            'tests/api/test_document_routes.py',
            'tests/api/test_mounts.py',
            'tests/api/test_app_lifecycle.py',
            'tests/test_app_bootstrap.py',
            'tests/test_deployment_settings.py'
        )
    }
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Fixed project Python is absent.'
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $xml) | Out-Null
$argv = @('-m', 'pytest', '-q') + $selection + @("--basetemp=$basetemp", "--junitxml=$xml")
Push-Location -LiteralPath $repo
try {
    & $python @argv
    $actualExit = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $actualExit
