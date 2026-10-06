# LongFlow optional model setup. Keys stay in process environment, never written to disk.
# Run from any directory: .\tools\api_keys_bootstrap.ps1
[CmdletBinding()]
param([string]$Python = "", [int]$Port = 8000)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
if (-not $Python) {
    $venv = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { $Python = $venv }
    else {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $cmd) { throw "Python not found. Install Python or supply -Python <path>." }
        $Python = $cmd.Source
    }
}
function Read-Secret([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr); $secure.Dispose() }
}
Write-Host "LongFlow optional OpenAI-compatible model setup (Enter keeps local defaults)"
$mode = Read-Host "Configure an OpenAI-compatible model now? [y/N]"
if ($mode -eq "y") {
    $base = Read-Host "Model base URL (required, e.g. provider /v1 endpoint)"
    $model = Read-Host "Model name (required)"
    if (-not $base -or -not $model) { throw "Base URL and model name are required." }
    $env:LLM_BASE_URL = $base
    $env:LLM_MODEL = $model
    $env:LLM_API_KEY = Read-Secret "Model API key"
    $env:LONGFLOW_DRIVER = "openai_compatible"
}
$env:PYTHONIOENCODING = "utf-8"
Push-Location $root
try {
    & $Python -X utf8 -m longflow.cli serve --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw "LongFlow exited with code $LASTEXITCODE" }
} finally { Pop-Location }
