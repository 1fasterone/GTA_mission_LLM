[CmdletBinding()]
param(
    [switch]$SkipConfig
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Requirements = Join-Path $ProjectRoot 'requirements.txt'
$Config = Join-Path $ProjectRoot 'config.yaml'
$ExampleConfig = Join-Path $ProjectRoot 'config.example.yaml'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    py -3.11 -m venv (Join-Path $ProjectRoot '.venv')
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r $Requirements

if (-not $SkipConfig -and -not (Test-Path -LiteralPath $Config)) {
    Copy-Item -LiteralPath $ExampleConfig -Destination $Config
    Write-Host 'Created config.yaml from config.example.yaml; review local paths before running.'
}

& $VenvPython -m pytest -q tests/test_unity_archetypes.py
Write-Host 'Installation verified.'
