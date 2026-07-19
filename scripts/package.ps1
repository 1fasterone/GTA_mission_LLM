# GTA Mission LLM — tester bundle packager
#
# Produces dist\GTA_mission_LLM_<yyyymmdd>.zip ready to send to a tester.
# Excludes: output/, Models/, __pycache__/, .venv/, .git/, .ratings.json, config.yaml
# Includes: src + webui + templates + lookups + docs + scripts + main.py +
#           README.md, requirements.{txt,lock}, config.example.yaml, README_FIRST.txt

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$date = Get-Date -Format "yyyyMMdd"
$distDir = Join-Path $root "dist"
$stageDir = Join-Path $distDir "GTA_mission_LLM_$date"
$zipPath  = Join-Path $distDir "GTA_mission_LLM_$date.zip"

Write-Host "==> Cleaning previous staging" -ForegroundColor Cyan
if (Test-Path $stageDir) { Remove-Item -Recurse -Force $stageDir }
if (Test-Path $zipPath)  { Remove-Item -Force $zipPath }
New-Item -ItemType Directory -Path $stageDir -Force | Out-Null

# Folders to include verbatim
$includeDirs = @(
    "src", "webui", "templates", "lookups",
    "docs", "scripts", "themes", "prompts"
)
# Top-level files to include
$includeFiles = @(
    "main.py", "unity_cli.py", "batch_generate.py",
    "requirements.txt", "pytest.ini",
    "README.md", "UNITY_CLI.md", "README_FIRST.txt", "QUICKSTART.md",
    "config.example.yaml"
)
# Optionally include the pinned lockfile if it exists
if (Test-Path "requirements.lock") { $includeFiles += "requirements.lock" }

Write-Host "==> Copying source" -ForegroundColor Cyan
foreach ($d in $includeDirs) {
    if (-not (Test-Path $d)) { continue }
    Copy-Item -Recurse -Path $d -Destination (Join-Path $stageDir $d)
}
foreach ($f in $includeFiles) {
    if (Test-Path $f) {
        Copy-Item -Path $f -Destination (Join-Path $stageDir $f)
    } else {
        Write-Host "    (missing, skipped) $f" -ForegroundColor Yellow
    }
}

Write-Host "==> Pruning excluded items from staging" -ForegroundColor Cyan
# Drop __pycache__ everywhere
Get-ChildItem -Path $stageDir -Recurse -Force -Directory `
    | Where-Object { $_.Name -eq "__pycache__" } `
    | ForEach-Object { Remove-Item -Recurse -Force $_.FullName }
# Drop *.pyc
Get-ChildItem -Path $stageDir -Recurse -Force -Filter "*.pyc" `
    | ForEach-Object { Remove-Item -Force $_.FullName }

Write-Host "==> Zipping -> $zipPath" -ForegroundColor Cyan
Compress-Archive -Path (Join-Path $stageDir "*") -DestinationPath $zipPath -Force

$size = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
Write-Host ""
Write-Host "Bundle ready:" -ForegroundColor Green
Write-Host "  $zipPath  ($size MB)"
Write-Host ""
Write-Host "Send the zip + the one-line invite from docs/TESTERS.md."
