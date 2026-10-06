$ErrorActionPreference = 'Stop'
$env:LONGFLOW_DRIVER = 'openai_compatible'
$env:LLM_BASE_URL = 'http://127.0.0.1:11434/v1'
$env:LLM_MODEL = 'longflow-qwen'
$env:LLM_API_KEY = 'ollama'
Set-Location (Split-Path -Parent $PSScriptRoot)
python -m longflow.cli serve --host 127.0.0.1 --port 8765
