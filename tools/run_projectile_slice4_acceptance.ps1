param(
    [switch]$UnityOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repo = (git rev-parse --show-toplevel).Trim()
if ($repo -replace "\\", "/" -ne "I:/ActiveRepos/GTA_mission_LLM") {
    throw "Repository root mismatch: $repo"
}
if ((git branch --show-current).Trim() -ne "codex/projectile-slice4-firearm-adapter") {
    throw "Unexpected branch before Slice 4 validation."
}

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Repository Python missing: $python"
}
Remove-Item Env:SKETCHFAB_API_TOKEN -ErrorAction SilentlyContinue

if (-not $UnityOnly) {
    & $python -m pytest -q `
        tests/test_projectile_firearm_animation_adapter.py `
        tests/test_projectile_prefab_visuals.py `
        tests/test_projectile_swept_impact.py `
        tests/test_unity_projectile_contract.py `
        tests/test_unity_pipeline.py
    if ($LASTEXITCODE -ne 0) { throw "Focused Slice 4 tests failed." }

    & $python -m ruff check `
        src/unity_projectile_contract.py `
        src/unity_compose.py `
        tests/test_projectile_firearm_animation_adapter.py `
        tools/generate_projectile_slice4_acceptance.py
    if ($LASTEXITCODE -ne 0) { throw "Slice 4 Ruff failed." }
    git diff --check
    if ($LASTEXITCODE -ne 0) { throw "Git whitespace validation failed." }
}

$editorVersion = "6000.0.66f2"
$acceptanceRoot = "I:\UnityAcceptance"
New-Item -ItemType Directory -Path $acceptanceRoot -Force | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$projectName = "ProjectileSlice4_$stamp"
$projectPath = Join-Path $acceptanceRoot $projectName
$compileLog = Join-Path $acceptanceRoot "$projectName-compile.log"
$testResults = Join-Path $acceptanceRoot "$projectName-playmode.xml"

& unity projects create $projectName `
    --path $acceptanceRoot `
    --editor-version $editorVersion `
    --template com.unity.template.urp-blank `
    --non-interactive `
    --format json
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $projectPath)) {
    throw "Unity acceptance project creation failed: $projectPath"
}

& $python tools/generate_projectile_slice4_acceptance.py $projectPath
if ($LASTEXITCODE -ne 0) {
    throw "Deterministic Slice 4 compiler emission failed."
}

$runtimeAsmdef = Join-Path $projectPath "Assets\Micros\ProjectileSlice4.Runtime.asmdef"
Copy-Item -LiteralPath (Join-Path $repo `
    "tests\unity_projectile_slice4\ProjectileSlice4.Runtime.asmdef") `
    -Destination $runtimeAsmdef -Force
$playTestDir = Join-Path $projectPath "Assets\Tests\PlayMode"
New-Item -ItemType Directory -Path $playTestDir -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repo `
    "tests\unity_projectile_slice4\ProjectileSlice4.PlayModeTests.asmdef") `
    -Destination $playTestDir -Force
Copy-Item -LiteralPath (Join-Path $repo `
    "tests\unity_projectile_slice4\ProjectileSlice4FirearmPlayModeTests.cs") `
    -Destination $playTestDir -Force

& unity run $projectPath `
    --editor-version $editorVersion `
    --timeout 600 `
    --log-file $compileLog `
    --no-tail `
    -- -nographics
if ($LASTEXITCODE -ne 0) {
    throw "Generated Unity project compilation failed. Log=$compileLog"
}
$compileErrors = Select-String -Path $compileLog `
    -Pattern "error CS\d+|Scripts have compiler errors" -AllMatches
if ($compileErrors) {
    throw "Generated Unity project contains C# errors. Log=$compileLog"
}

& unity test $projectPath `
    --editor-version $editorVersion `
    --mode PlayMode `
    --filter ProjectileSlice4FirearmPlayModeTests `
    --output $testResults `
    --report-format nunit `
    --timeout 600 `
    -- -nographics
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $testResults)) {
    throw "Slice 4 Unity Play Mode acceptance failed. Results=$testResults"
}

[xml]$results = Get-Content -Raw -LiteralPath $testResults
$run = $results."test-run"
if ([int]$run.failed -ne 0 -or [int]$run.passed -lt 2) {
    throw "Slice 4 Play Mode verdict failed. Passed=$($run.passed) Failed=$($run.failed)"
}
$proof = Select-String -Path $testResults `
    -Pattern "SLICE4_AUTOMATIC_PASS|SLICE4_ANIM_EVENT_PASS" -AllMatches |
    ForEach-Object { $_.Line.Trim() }
if ($proof.Count -lt 2) {
    throw "Slice 4 Play Mode report is missing runtime proof records."
}

$global:Slice4Acceptance = [pscustomobject]@{
    PythonGates = $(if ($UnityOnly) { "passed-in-prior-run" } else { "passed" })
    UnityEditor = $editorVersion
    GeneratedProject = $projectPath
    UnityCompileErrors = 0
    PlayModePassed = [int]$run.passed
    PlayModeFailed = [int]$run.failed
    PlayModeResults = $testResults
    CompileLog = $compileLog
    Proof = $proof
}

$global:Slice4Acceptance | Format-List
$proof
