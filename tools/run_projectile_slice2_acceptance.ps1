param(
    [switch]$UnityOnly
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
    foreach ($argument in $Arguments) {
        $startInfo.ArgumentList.Add($argument)
    }
    $process = [System.Diagnostics.Process]::Start($startInfo)
    if ($null -eq $process) { throw "Unity Editor process did not start." }
    $process.WaitForExit()
    return $process.ExitCode
}

$repo = (git rev-parse --show-toplevel).Trim()
if ($repo -replace "\\", "/" -ne "I:/ActiveRepos/GTA_mission_LLM") {
    throw "Repository root mismatch: $repo"
}
if ((git branch --show-current).Trim() -ne "codex/projectile-slice2-swept-impact") {
    throw "Unexpected branch before validation."
}

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Repository Python missing: $python"
}
Remove-Item Env:SKETCHFAB_API_TOKEN -ErrorAction SilentlyContinue

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
if (-not $UnityOnly) {
    & $python -m pytest -q `
        tests/test_projectile_swept_impact.py `
        tests/test_unity_projectile_contract.py `
        tests/test_micro_generators.py
    if ($LASTEXITCODE -ne 0) { throw "Focused projectile tests failed." }

    # Match .github/workflows/ci.yml exactly. The repository intentionally keeps
    # legacy utilities and tests outside the CI lint scope.
    & $python -m ruff check src main.py cli rag
    if ($LASTEXITCODE -ne 0) { throw "CI-scope Ruff failed." }
    & $python -m ruff check `
        tools/generate_projectile_slice2_acceptance.py `
        tests/test_projectile_swept_impact.py
    if ($LASTEXITCODE -ne 0) { throw "Slice 2 Python Ruff failed." }

    & $python -m mypy src main.py cli rag
    if ($LASTEXITCODE -ne 0) { throw "CI-scope mypy failed." }

    $contractTests = @(
        "tests/test_frozen_step35_acceptance.py",
        "tests/test_ident_digest_lint.py",
        "tests/test_ci_contract.py"
    ) | Where-Object { Test-Path -LiteralPath $_ }
    if ($contractTests.Count -gt 0) {
        & $python -m pytest -q @contractTests
        if ($LASTEXITCODE -ne 0) {
            throw "Frozen Step 3.5 or CI contract tests failed."
        }
    }

    git diff --check
    if ($LASTEXITCODE -ne 0) { throw "Git whitespace validation failed." }

    $cleanRoot = Join-Path $env:TEMP "GTA_mission_LLM_slice2_clean_$stamp"
    $archive = "$cleanRoot.zip"
    git archive --format=zip --output=$archive HEAD
    if ($LASTEXITCODE -ne 0) { throw "Could not create clean test snapshot." }
    Expand-Archive -LiteralPath $archive -DestinationPath $cleanRoot

    $changedFiles = @(
        "templates\unity\micro\controller\ProjectileController.cs.j2",
        "tests\test_projectile_swept_impact.py",
        "tools\generate_projectile_slice2_acceptance.py"
    )
    foreach ($relativePath in $changedFiles) {
        $destination = Join-Path $cleanRoot $relativePath
        New-Item -ItemType Directory -Path (Split-Path $destination) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $repo $relativePath) `
            -Destination $destination -Force
    }
    $cleanUnityTests = Join-Path $cleanRoot "tests\unity_projectile_slice2"
    New-Item -ItemType Directory -Path $cleanUnityTests -Force | Out-Null
    Copy-Item -Path (Join-Path $repo "tests\unity_projectile_slice2\*") `
        -Destination $cleanUnityTests -Recurse -Force

    $previousPythonPath = $env:PYTHONPATH
    Push-Location $cleanRoot
    try {
        $env:PYTHONPATH = $cleanRoot
        & $python -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw "Full clean-snapshot pytest suite failed." }
    }
    finally {
        Pop-Location
        if ($null -eq $previousPythonPath) {
            Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        }
        else {
            $env:PYTHONPATH = $previousPythonPath
        }
    }
}

$editorCandidates = @()
foreach ($editorRoot in @(
    "C:\Program Files\Unity\Hub\Editor",
    "C:\Program Files\Unity\Editor",
    "C:\Unity\Hub\Editor"
)) {
    if (Test-Path -LiteralPath $editorRoot) {
        $editorCandidates += Get-ChildItem -LiteralPath $editorRoot `
            -Filter Unity.exe -File -Recurse -ErrorAction SilentlyContinue
    }
}
$editor = $editorCandidates | Sort-Object FullName -Descending | Select-Object -First 1
if ($null -eq $editor) { throw "No installed Unity Editor executable found." }

$acceptanceRoot = "I:\UnityAcceptance"
New-Item -ItemType Directory -Path $acceptanceRoot -Force | Out-Null
$projectPath = Join-Path $acceptanceRoot "ProjectileSlice2_$stamp"
$createLog = Join-Path $acceptanceRoot "ProjectileSlice2_$stamp-create.log"
$packageLog = Join-Path $acceptanceRoot "ProjectileSlice2_$stamp-packages.log"
$compileLog = Join-Path $acceptanceRoot "ProjectileSlice2_$stamp-compile.log"
$playLog = Join-Path $acceptanceRoot "ProjectileSlice2_$stamp-playmode.log"
$testResults = Join-Path $acceptanceRoot "ProjectileSlice2_$stamp-playmode.xml"

$createExit = Invoke-UnityEditor -EditorPath $editor.FullName -Arguments @(
    "-batchmode", "-quit", "-createProject", $projectPath, "-logFile", $createLog
)
if ($createExit -ne 0) {
    throw "Unity project creation failed. Exit=$createExit Log=$createLog"
}

Push-Location $repo
try {
    & $python tools/generate_projectile_slice2_acceptance.py $projectPath
    if ($LASTEXITCODE -ne 0) {
        throw "Deterministic Unity compiler emission failed."
    }
}
finally {
    Pop-Location
}

$bootstrapDir = Join-Path $projectPath "Assets\Editor\ProjectBootstrap"
New-Item -ItemType Directory -Path $bootstrapDir -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repo `
    "tests\unity_projectile_slice2\PackageInstaller.cs") `
    -Destination $bootstrapDir -Force
$packageExit = Invoke-UnityEditor -EditorPath $editor.FullName -Arguments @(
    "-batchmode", "-projectPath", $projectPath, "-executeMethod",
    "ProjectBootstrap.PackageInstaller.Install", "-logFile", $packageLog
)
if ($packageExit -ne 0) {
    throw "Unity Test Framework installation failed. Exit=$packageExit Log=$packageLog"
}
$manifestPath = Join-Path $projectPath "Packages\manifest.json"
if (-not (Select-String -Path $manifestPath -Pattern 'com\.unity\.test-framework')) {
    throw "Unity Test Framework is absent after package installation: $manifestPath"
}

$runtimeAsmdefTarget = Join-Path $projectPath `
    "Assets\Micros\ProjectileSlice2.Runtime.asmdef"
Copy-Item -LiteralPath (Join-Path $repo `
    "tests\unity_projectile_slice2\ProjectileSlice2.Runtime.asmdef") `
    -Destination $runtimeAsmdefTarget -Force
$playTestDir = Join-Path $projectPath "Assets\Tests\PlayMode"
New-Item -ItemType Directory -Path $playTestDir -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repo `
    "tests\unity_projectile_slice2\ProjectileSlice2.PlayModeTests.asmdef") `
    -Destination $playTestDir -Force
Copy-Item -LiteralPath (Join-Path $repo `
    "tests\unity_projectile_slice2\ProjectileSweptImpactPlayModeTests.cs") `
    -Destination $playTestDir -Force

$compileExit = Invoke-UnityEditor -EditorPath $editor.FullName -Arguments @(
    "-batchmode", "-nographics", "-quit", "-projectPath", $projectPath,
    "-logFile", $compileLog
)
if ($compileExit -ne 0) {
    throw "Generated Unity project compilation failed. Exit=$compileExit Log=$compileLog"
}
if (-not (Test-Path -LiteralPath $compileLog)) {
    throw "Unity compilation produced no log: $compileLog"
}
$compileErrors = Select-String -Path $compileLog `
    -Pattern "error CS\d+|Scripts have compiler errors" -AllMatches
if ($compileErrors) {
    throw "Generated Unity project contains C# errors. Log=$compileLog"
}

$playExit = Invoke-UnityEditor -EditorPath $editor.FullName -Arguments @(
    "-batchmode", "-nographics", "-runTests", "-projectPath", $projectPath,
    "-testPlatform", "PlayMode", "-testFilter", "ProjectileSweptImpactPlayModeTests",
    "-testResults", $testResults, "-logFile", $playLog
)
if (-not (Test-Path -LiteralPath $testResults)) {
    throw "Unity produced no Play Mode results. Exit=$playExit Log=$playLog"
}
[xml]$results = Get-Content -Raw -LiteralPath $testResults
$run = $results."test-run"
if ($playExit -ne 0 -or [int]$run.failed -ne 0 -or [int]$run.passed -lt 2) {
    throw "Unity Play Mode tests failed. Exit=$playExit Passed=$($run.passed) " +
        "Failed=$($run.failed) Log=$playLog Results=$testResults"
}
$proof = @(
    Select-String -Path $playLog -Pattern "SLICE2_TUNNEL_PASS" |
        ForEach-Object { $_.Line.Trim() }
)
if ($proof.Count -lt 2) {
    throw "Expected two tunneling proof records; found $($proof.Count). Log=$playLog"
}

$global:Slice2Acceptance = [pscustomobject]@{
    PythonGates = $(if ($UnityOnly) { "passed-in-prior-run" } else { "passed" })
    UnityEditor = $editor.FullName
    GeneratedProject = $projectPath
    PackageLog = $packageLog
    UnityCompileErrors = 0
    PlayModePassed = [int]$run.passed
    PlayModeFailed = [int]$run.failed
    PlayModeLog = $playLog
    PlayModeResults = $testResults
    Proof = $proof
}

$global:Slice2Acceptance | Format-List
$proof
