"""Regressions for materializing the captured-schema Unity compile fixture."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_ci_project_contains_referenced_vendor_sources_and_reports_real_root(tmp_path):
    outputs = tmp_path / "github-output.txt"
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/generate_ci_unity_project.py"),
         "--output", str(tmp_path / "work/ci-unity"), "--github-output", str(outputs)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    values = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
    project = tmp_path / values["project_path"]
    assert (project / "ProjectSettings/ProjectVersion.txt").is_file()
    assert (project / "Packages/manifest.json").is_file()
    assert (project / "Assets/Editor/CiCompileCheck.cs").is_file()
    assert (project / "Assets/Scripts/AtmosphereBootstrap.cs").is_file()
    vendor = ROOT / "templates/unity/vendor/URPAtmosphere"
    for source in (vendor / "Atmosphere").rglob("*"):
        if source.is_file():
            target = project / "Assets/Atmosphere" / source.relative_to(vendor / "Atmosphere")
            assert target.read_bytes() == source.read_bytes(), source
    for source in (vendor / "Profile").iterdir():
        assert (project / "Assets/Resources/Atmosphere" / source.name).read_bytes() == source.read_bytes()
    schema = json.loads((project / "ci-schema.json").read_text())
    assert project.name == schema["project_name"]
    assert not (tmp_path / "work/ci-unity/Assets").exists()


def test_unity_workflow_opens_the_materialized_project_root():
    workflow = yaml.safe_load((ROOT / ".github/workflows/unity-validation.yml").read_text())
    steps = workflow["jobs"]["unity-compile"]["steps"]
    generator = next(step for step in steps if step.get("id") == "generate")
    compile_step = next(step for step in steps if "game-ci/unity-builder@" in step.get("uses", ""))
    assert '--github-output "$GITHUB_OUTPUT"' in generator["run"]
    assert compile_step["with"]["projectPath"] == "${{ steps.generate.outputs.project_path }}"
