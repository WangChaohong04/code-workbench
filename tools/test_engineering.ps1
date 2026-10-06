param(
    [ValidateSet('Core','AI','Runs','Sandbox','Operations','Production','All')]
    [string]$Suite = 'All',
    [switch]$RealDocker
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $groups = @{
        Core = @('tests/test_local_workspace.py','tests/test_local_workspace_api.py')
        AI = @('tests/test_engineering_ai.py')
        Runs = @('tests/test_engineering_runs.py','tests/test_engineering_run_api.py')
        Sandbox = @('tests/test_execution_sandbox.py','tests/test_execution_api.py')
        Operations = @('tests/test_engineering_operations.py','tests/test_operations_api.py','tests/test_operations_api_provider_state.py')
        Production = @('tests/test_production_adapters.py','tests/test_production_uncertainty.py','tests/test_production_api.py')
    }
    if ($RealDocker) {
        throw 'Real Docker execution is not enabled by this default runner. Read docs/ENGINEERING_TEST_MATRIX.md and the opt-in test prerequisites first.'
    }
    $files = if ($Suite -eq 'All') { @($groups.Core + $groups.AI + $groups.Runs + $groups.Sandbox + $groups.Operations + $groups.Production) } else { @($groups[$Suite]) }
    $temp = '.lfwork/test-engineering-' + [guid]::NewGuid().ToString('N')
    & python -B -m pytest @files -q -rs -p no:cacheprovider "--basetemp=$temp"
    $result = $LASTEXITCODE
    if ($result -ne 0) { exit $result }
    & node tools/check_web.mjs longflow/web
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & node tests/test_safety_web.mjs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & node tests/test_files_ai_web.mjs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & node tests/test_operations_web.mjs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
