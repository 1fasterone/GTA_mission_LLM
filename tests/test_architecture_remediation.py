from __future__ import annotations

import inspect
from pathlib import Path

from src import unity_pipeline
from src.unity_pipeline_stages import (
    BindAssetsStage,
    ComposeStage,
    ExtractIntentStage,
    GenerateSchemaStage,
    GenerateStage,
    ResolveIntentStage,
    ValidateSchemaStage,
)
from src.unity_projectile_contract import validate_physical_projectile_contract
from tools.generate_ci_unity_project import COMPILE_CHECK


ROOT = Path(__file__).resolve().parents[1]


def test_pipeline_has_all_named_architecture_stages():
    assert [stage.name for stage in (
        ExtractIntentStage,
        ResolveIntentStage,
        GenerateSchemaStage,
        ValidateSchemaStage,
        ComposeStage,
        BindAssetsStage,
        GenerateStage,
    )] == [
        "extract_intent", "resolve_intent", "generate_schema",
        "validate_schema", "compose", "bind_assets", "generate",
    ]


def test_ordinary_pipeline_resolves_and_reconciles_once():
    source = inspect.getsource(unity_pipeline.UnityPipeline._run_impl)
    assert source.count("self.resolver.resolve_schema(schema)") == 1
    assert source.count("schema, _scene_fixes = _reconcile_scene_specs(schema)") == 1


def test_projectile_validator_is_read_only_by_contract():
    source = inspect.getsource(validate_physical_projectile_contract)
    assert "schema.meta =" not in source
    assert "schema.entities.append" not in source


def test_compile_workflows_are_mandatory_not_skip_gates():
    build = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    unity = (ROOT / ".github" / "workflows" / "unity-validation.yml").read_text(encoding="utf-8")
    assert "dotnet build" in build
    assert "Unity generated-project compile" in unity
    assert "UNITY_LICENSE repository secret is required" in unity
    assert "game-ci/unity-builder@4423ee75828dff56037d1d1cfdac6b68e3a6ba00" in unity
    assert unity.count("buildMethod: CiCompileCheck.Perform") == 1
    assert "public static class CiCompileCheck" in COMPILE_CHECK
    assert "public static void Perform()" in COMPILE_CHECK
    assert "EditorUtility.scriptCompilationFailed" in COMPILE_CHECK
    assert "continue-on-error" not in unity


def test_agent_guidance_is_generated_from_shared_policy():
    source = (ROOT / "docs" / "policies" / "shared-agent-guidance.md").read_text(
        encoding="utf-8"
    ).strip()
    for name in ("AGENTS.md", "CLAUDE.md"):
        assert source in (ROOT / name).read_text(encoding="utf-8")


def test_onboarding_installer_exists():
    assert (ROOT / "scripts" / "install.ps1").is_file()
