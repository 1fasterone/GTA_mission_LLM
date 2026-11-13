# R3 (S5) firing-provider acceptance in a real Unity Editor.
#
# Creates a clean Unity project, installs the Unity Test Framework, emits the
# acceptance sources from canonical Python/templates
# (tools/generate_firing_provider_acceptance.py), compiles them, then runs the
# Play Mode tests in tests/unity_firing_provider/. Three proof markers must
# appear: FIRING_PROVIDER_SHARED_GATE_PASS, FIRING_PROVIDER_POOL_PASS and
# FIRING_PROVIDER_NO_PROVIDER_PASS.
#
#   pwsh -File tools/run_firing_provider_acceptance.ps1 [-Editor <Unity.exe>]
#
# The Editor defaults to config.yaml component_api.unity_editor, the install
# the Roslyn reference profile already compiles against.
param(
    [string]$Editor = "",
    [string]$AcceptanceRoot = "I:\UnityAcceptance"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-UnityEditor {
    param(
        [Parameter(Mandatory = $true)][string]$EditorPath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $EditorPath
    $startInfo.UseShellExecute = $false
    foreach ($argument in $Arguments) { $startInfo.ArgumentList.Add($argument) }
    $process = [System.Diagnostics.Process]::Start($startInfo)
    if ($null -eq $process) { throw "Unity Editor process did not start." }
    $process.WaitForExit()
    return $process.ExitCode
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = "python" }
Remove-Item Env:SKETCHFAB_API_TOKEN -ErrorAction SilentlyContinue

if (-not $Editor) {
    $hubRoot = & $python -c @"
import yaml, pathlib
cfg = yaml.safe_load(pathlib.Path(r'$repo\config.yaml').read_text(encoding='utf-8')) or {}
print((cfg.get('component_api') or {}).get('unity_editor') or '')
"@
    if (-not $hubRoot) { throw "config.yaml has no component_api.unity_editor; pass -Editor." }
    $Editor = Join-Path $hubRoot.Trim() "Editor\Unity.exe"
}
if (-not (Test-Path -LiteralPath $Editor)) { throw "Unity Editor not found: $Editor" }

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
New-Item -ItemType Directory -Path $AcceptanceRoot -Force | Out-Null
$name = "FiringProvider_$stamp"
$projectPath = Join-Path $AcceptanceRoot $name
$createLog = Join-Path $AcceptanceRoot "$name-create.log"
$packageLog = Join-Path $AcceptanceRoot "$name-packages.log"
$compileLog = Join-Path $AcceptanceRoot "$name-compile.log"
$playLog = Join-Path $AcceptanceRoot "$name-playmode.log"
$testResults = Join-Path $AcceptanceRoot "$name-playmode.xml"

$createExit = Invoke-UnityEditor -EditorPath $Editor -Arguments @(
    "-batchmode", "-quit", "-createProject", $projectPath, "-logFile", $createLog)
if ($createExit -ne 0) { throw "Unity project creation failed. Exit=$createExit Log=$createLog" }

# The shared slice-2 bootstrap adds com.unity.test-framework through UPM.
$bootstrapDir = Join-Path $projectPath "Assets\Editor\ProjectBootstrap"
New-Item -ItemType Directory -Path $bootstrapDir -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repo "tests\unity_projectile_slice2\PackageInstaller.cs") `
    -Destination $bootstrapDir -Force
$packageExit = Invoke-UnityEditor -EditorPath $Editor -Arguments @(
    "-batchmode", "-projectPath", $projectPath, "-executeMethod",
    "ProjectBootstrap.PackageInstaller.Install", "-logFile", $packageLog)
if ($packageExit -ne 0) { throw "Unity Test Framework installation failed. Exit=$packageExit Log=$packageLog" }

Push-Location $repo
try {
    & $python tools/generate_firing_provider_acceptance.py $projectPath
    if ($LASTEXITCODE -ne 0) { throw "Deterministic firing-provider emission failed." }
}
finally { Pop-Location }

$runtimeDir = Join-Path $projectPath "Assets\FiringProviderAcceptance"
Copy-Item -LiteralPath (Join-Path $repo "tests\unity_firing_provider\FiringProvider.Runtime.asmdef") `
    -Destination $runtimeDir -Force
$playTestDir = Join-Path $projectPath "Assets\Tests\PlayMode"
New-Item -ItemType Directory -Path $playTestDir -Force | Out-Null
foreach ($file in @("FiringProvider.PlayModeTests.asmdef", "FiringProviderPlayModeTests.cs")) {
    Copy-Item -LiteralPath (Join-Path $repo "tests\unity_firing_provider\$file") `
        -Destination $playTestDir -Force
}

$compileExit = Invoke-UnityEditor -EditorPath $Editor -Arguments @(
    "-batchmode", "-nographics", "-quit", "-projectPath", $projectPath, "-logFile", $compileLog)
if ($compileExit -ne 0) { throw "Generated project compilation failed. Exit=$compileExit Log=$compileLog" }
$compileErrors = Select-String -Path $compileLog -Pattern "error CS\d+|Scripts have compiler errors"
if ($compileErrors) { throw "Generated project contains C# errors. Log=$compileLog" }

$playExit = Invoke-UnityEditor -EditorPath $Editor -Arguments @(
    "-batchmode", "-nographics", "-runTests", "-projectPath", $projectPath,
    "-testPlatform", "PlayMode", "-testFilter", "FiringProviderPlayModeTests",
    "-testResults", $testResults, "-logFile", $playLog)
if (-not (Test-Path -LiteralPath $testResults)) {
    throw "Unity produced no Play Mode results. Exit=$playExit Log=$playLog"
}
[xml]$results = Get-Content -Raw -LiteralPath $testResults
$run = $results."test-run"
if ($playExit -ne 0 -or [int]$run.failed -ne 0 -or [int]$run.passed -lt 3) {
    throw ("Firing-provider Play Mode tests failed. Exit=$playExit Passed=$($run.passed) " +
        "Failed=$($run.failed) Log=$playLog Results=$testResults")
}
$markers = @("FIRING_PROVIDER_SHARED_GATE_PASS", "FIRING_PROVIDER_POOL_PASS",
             "FIRING_PROVIDER_NO_PROVIDER_PASS")
$proof = @(Select-String -Path $testResults -Pattern ($markers -join "|") |
    ForEach-Object { $_.Line.Trim() })
foreach ($marker in $markers) {
    if (-not ($proof | Where-Object { $_ -match $marker })) {
        throw "Missing runtime proof record $marker. Results=$testResults"
    }
}

$global:FiringProviderAcceptance = [pscustomobject]@{
    UnityEditor = $Editor
    GeneratedProject = $projectPath
    UnityCompileErrors = 0
    PlayModePassed = [int]$run.passed
    PlayModeFailed = [int]$run.failed
    PlayModeLog = $playLog
    PlayModeResults = $testResults
    Proof = $proof
}
$global:FiringProviderAcceptance | Format-List
