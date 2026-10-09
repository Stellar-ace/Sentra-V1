$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = 'python'
}

Push-Location $root
try {
    & $python main.py
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
