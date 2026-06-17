# GTA Mission LLM — tester install script
#
# Creates a local venv and installs pinned dependencies.
# Run from the project root:
#   powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "==> Checking Python" -ForegroundColor Cyan
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Error "Python is not on PATH. Install Python 3.11+ from https://python.org and re-run."
    exit 1
}
$ver = & python --version 2>&1
Write-Host "    $ver"

Write-Host "==> Creating virtual environment (.venv)" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) {
    & python -m venv .venv
} else {
    Write-Host "    .venv already exists — reusing"
}

Write-Host "==> Activating venv + upgrading pip" -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet

Write-Host "==> Installing dependencies" -ForegroundColor Cyan
if (Test-Path "requirements.lock") {
    Write-Host "    using requirements.lock (pinned)"
    & .\.venv\Scripts\python.exe -m pip install -r requirements.lock --quiet
} else {
    Write-Host "    using requirements.txt"
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
}

Write-Host "==> Bootstrapping config.yaml" -ForegroundColor Cyan
if (-not (Test-Path "config.yaml")) {
    if (Test-Path "config.example.yaml") {
        Copy-Item "config.example.yaml" "config.yaml"
        Write-Host "    copied config.example.yaml -> config.yaml (edit it next)"
    } else {
        Write-Host "    config.example.yaml not found; you'll need to author config.yaml by hand."
    }
} else {
    Write-Host "    config.yaml already present — leaving it alone"
}

Write-Host ""
Write-Host "Install complete." -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Edit config.yaml (set api_key if you're using AnythingLLM)"
Write-Host "  2. .\.venv\Scripts\Activate.ps1"
Write-Host "  3. python main.py serve"
Write-Host "     -> open the printed http://127.0.0.1:8000 URL"
