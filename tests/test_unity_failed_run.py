"""``unity-failed-run/v1`` diagnostics: classification, phase, authorship, I/O.

Engineer-authored test data: every schema and raw "fill" below is a labeled
fixture, never pipeline output.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.unity_failed_run import (
    FAILED_RUN_DIRNAME,
    RAW_RESPONSE_LIMIT,
    FailedRunReport,
    RunDiagnostics,
    build_failed_run_report,
    classify_failure,
    failed_phase,
    failed_run_json_schema,
    write_failed_run_report,
)
from src.unity_pipeline import UnityPipeline
from src.unity_prompts import UNITY_CONCEPT_SYSTEM, UNITY_SCHEMA_FILL_SYSTEM
from src.unity_run_log import UnityCreateRunRecorder
from src.unity_schema import UnitySchema

_NOW = datetime(2026, 11, 12, 10, 0, 0, tzinfo=timezone.utc)
_LINEAGE = {"classification": "test_fixture"}
_CONCEPT = "A chaotic kart racer menaced by a fifty-foot squirrel. PC, 3D."


class SpatialPipelineError(ValueError):
    """Name-alike of src.unity_spatial_pipeline.SpatialPipelineError."""


class PostSchemaLLMError(RuntimeError):
    """Name-alike of src.llm_boundary.PostSchemaLLMError."""


def _schema_fixture() -> UnitySchema:
    return UnitySchema.model_validate({
        "project_name": "FixtureRacer", "game_type": "3D", "platform": "PC",
        "render_pipeline": "URP",
    })


def _schema_json() -> str:
    return json.dumps({
        "project_name": "Gigantinut", "company_name": "TestCo",
        "game_type": "3D", "platform": "PC", "render_pipeline": "URP",
        "unity_version": "6000.0", "color_space": "Linear",
        "description": "A kart racer with a giant squirrel",
        "features": ["input_system"], "scenes": ["Main", "Menu"],
        "scripts": [
            {"name": "GameManager", "type": "game_manager", "description": "GM"},
            {"name": "PlayerController", "type": "player", "description": "Driving"},
        ],
        "packages": [{"description": "universal render pipeline"}],
    })


def _chat_script(*fills: str):
    remaining = list(fills)

    def _chat(*, system: str = "", user: str = "", **_kw) -> str:
        if system == UNITY_SCHEMA_FILL_SYSTEM:
            return remaining.pop(0)
        if system == UNITY_CONCEPT_SYSTEM:
            return _CONCEPT
        return ""

    return _chat


@pytest.fixture
def pipeline_config(tmp_path) -> dict:
    return {
        "lm_studio": {
            "base_url": "http://localhost:1234/v1", "model": "test-model",
            "temperature": 0.7, "max_tokens": 8192, "timeout": 30.0,
            "concept_temperature": 0.85, "schema_temperature": 0.2,
        },
        "unity_pipeline": {
            "lookups_dir": "lookups", "unity_template_dir": "templates/unity",
            "unity_output_dir": str(tmp_path),
        },
        "logging": {"show_prompts": False},
    }


def _only_report(output_dir: Path) -> dict:
    reports = [p for p in (output_dir / FAILED_RUN_DIRNAME).glob("*.json")
               if not p.name.endswith(".schema.json")]
    assert len(reports) == 1, reports
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    FailedRunReport.model_validate(data)
    return data


# ── classification ─────────────────────────────────────────────────────────────

def test_a_wrapped_cause_decides_the_category():
    try:
        try:
            raise SpatialPipelineError("[layout.strict] no motion axis")
        except SpatialPipelineError as inner:
            raise RuntimeError("spatial sidecar freeze failed") from inner
    except RuntimeError as exc:
        category, matched, remedy = classify_failure(exc)
    assert category == "spatial_layout"
    assert matched == "SpatialPipelineError"
    assert "LAYOUT_ARCHETYPES" in remedy


def test_freeze_violation_outranks_its_runtime_error_base():
    category, matched, _ = classify_failure(PostSchemaLLMError("chat after freeze"))
    assert (category, matched) == ("llm_freeze_violation", "PostSchemaLLMError")


def test_an_unknown_failure_abstains_loudly():
    category, matched, remedy = classify_failure(RuntimeError("boom"))
    assert category == "unclassified"
    assert matched is None
    assert "_CATEGORY_RULES" in remedy


def test_a_failed_fill_is_schema_fill_whatever_wraps_it():
    category, _, remedy = classify_failure(RuntimeError("x"), fill_failed=True)
    assert category == "schema_fill"
    assert "fill_attempts" in remedy


def test_phase_after_resolution_depends_on_the_freeze():
    assert failed_phase("Post-Resolution", frozen=False) == "Step 3.5 enrichment"
    assert failed_phase("Post-Resolution", frozen=True) == "Steps 4-5 (resolve + fixer)"
    assert failed_phase(None, frozen=False) == "setup (before Step 2)"
    assert "unknown checkpoint" in failed_phase("Nope", frozen=False)


# ── report building and writing ────────────────────────────────────────────────

def test_report_is_deterministic_for_fixed_inputs():
    diagnostics = RunDiagnostics(user_prompt="a brief")
    diagnostics.checkpoint("Pre-Step3")
    exc = RuntimeError("boom")
    a = build_failed_run_report(diagnostics, exc, now=_NOW, source_lineage=_LINEAGE)
    b = build_failed_run_report(diagnostics, exc, now=_NOW, source_lineage=_LINEAGE)
    assert a.model_dump() == b.model_dump()
    assert a.phase.failed_phase == "Steps 2-3 (concept + schema fill)"
    assert a.schema_artifact.available is False
    assert a.replay_command is None


def test_post_freeze_failure_saves_schema_and_names_replay(tmp_path):
    diagnostics = RunDiagnostics(user_prompt="a brief", llm_enabled=True)
    diagnostics.schema_source = "schema_llm"
    diagnostics.concept_source = "llm"
    diagnostics.checkpoint("Post-Resolution", _schema_fixture())
    diagnostics.frozen = True

    path = write_failed_run_report(diagnostics, RuntimeError("boom"), tmp_path,
                                   now=_NOW, source_lineage=_LINEAGE)

    report = json.loads(path.read_text(encoding="utf-8"))
    artifact = report["schema_artifact"]
    assert artifact["available"] is True
    assert artifact["project_name"] == "FixtureRacer"
    saved = UnitySchema.model_validate_json(
        Path(artifact["path"]).read_text(encoding="utf-8"))
    assert saved.project_name == "FixtureRacer"
    assert report["replay_command"] == f"python main.py unity-validate {artifact['path']}"
    assert report["llm_boundary"]["frozen_at_failure"] is True
    assert report["phase"]["failed_phase"] == "Steps 4-5 (resolve + fixer)"
    assert "Step 3 Pipeline LLM" in report["authorship"]["note"]


def test_a_runaway_raw_response_is_cut_and_says_so():
    diagnostics = RunDiagnostics(user_prompt="a brief")
    diagnostics.record_fill_attempt(1, "x" * (RAW_RESPONSE_LIMIT + 5),
                                    ValueError("no JSON"))
    report = build_failed_run_report(diagnostics, RuntimeError("boom"),
                                     now=_NOW, source_lineage=_LINEAGE)
    attempt = report.fill_attempts[0]
    assert attempt.raw_truncated is True
    assert attempt.raw_response_chars == RAW_RESPONSE_LIMIT + 5
    assert len(attempt.raw_response or "") == RAW_RESPONSE_LIMIT


def test_an_undumpable_schema_is_recorded_not_raised(tmp_path):
    class _Broken:
        project_name = "Broken"

        def model_dump_json(self, **_kw):
            raise TypeError("not serializable")

    diagnostics = RunDiagnostics(user_prompt="a brief")
    diagnostics.checkpoint("Post-Step3", _Broken())
    path = write_failed_run_report(diagnostics, RuntimeError("boom"), tmp_path,
                                   now=_NOW, source_lineage=_LINEAGE)
    artifact = json.loads(path.read_text(encoding="utf-8"))["schema_artifact"]
    assert artifact["available"] is False
    assert artifact["dump_error"] == "TypeError: not serializable"


def test_json_schema_is_exported_and_closed():
    schema = failed_run_json_schema()
    assert schema["title"] == "FailedRunReport"
    assert schema["additionalProperties"] is False


# ── pipeline integration ───────────────────────────────────────────────────────

def test_two_failed_fills_leave_a_schema_fill_report(pipeline_config, tmp_path):
    pipeline = UnityPipeline(pipeline_config)
    pipeline.llm.chat = MagicMock(side_effect=_chat_script("no json", "still no json"))

    with pytest.raises(RuntimeError, match="schema fill failed after 2 attempts"):
        pipeline.run("a giant squirrel wrecks a kart race")

    report = _only_report(tmp_path)
    assert report["failure"]["category"] == "schema_fill"
    assert [a["raw_response"] for a in report["fill_attempts"]] == [
        "no json", "still no json"]
    assert all(a["error_type"] == "ValueError" for a in report["fill_attempts"])
    assert report["authorship"]["schema_source"] == "schema_llm"
    assert report["authorship"]["concept_source"] == "llm"
    assert report["concept"] == _CONCEPT
    assert report["llm_boundary"]["frozen_at_failure"] is False
    assert report["phase"]["last_step"]["number"] == "3"
    assert report["phase"]["completed_stages"] == ["extract_intent"]
    assert report["schema_artifact"]["available"] is False
    assert any("schema fill attempt 1 failed" in w for w in report["warnings"])


def test_generation_failure_after_freeze_keeps_the_filled_schema(
        pipeline_config, tmp_path):
    pipeline = UnityPipeline(pipeline_config)
    pipeline.llm.chat = MagicMock(side_effect=_chat_script(_schema_json()))
    pipeline.generator.write = MagicMock(side_effect=RuntimeError("disk on fire"))

    with pytest.raises(RuntimeError, match="disk on fire"):
        pipeline.run("a giant squirrel wrecks a kart race")

    report = _only_report(tmp_path)
    assert report["failure"]["category"] == "unclassified"
    assert report["llm_boundary"]["frozen_at_failure"] is True
    assert report["phase"]["last_checkpoint"] == "Pre-Generation"
    assert report["phase"]["failed_phase"] == "Steps 6-7 (generation + playability)"
    assert report["fill_attempts"][0]["error"] is None
    assert report["schema_artifact"]["project_name"] == "Gigantinut"
    assert report["replay_command"].startswith("python main.py unity-validate ")
    assert report["intent_ledger"]["format"] == "intent-ledger/v1"


def test_diagnostics_can_be_disabled(pipeline_config, tmp_path):
    pipeline_config["diagnostics"] = {"failed_runs": False}
    pipeline = UnityPipeline(pipeline_config)
    pipeline.llm.chat = MagicMock(side_effect=_chat_script("no json", "no json"))

    with pytest.raises(RuntimeError):
        pipeline.run("a giant squirrel wrecks a kart race")

    assert not (tmp_path / FAILED_RUN_DIRNAME).exists()


def test_a_report_write_failure_never_masks_the_run_error(
        pipeline_config, monkeypatch):
    import src.unity_pipeline as unity_pipeline

    def _refuse(*_a, **_kw):
        raise OSError("read-only volume")

    monkeypatch.setattr(unity_pipeline, "write_failed_run_report", _refuse)
    pipeline = UnityPipeline(pipeline_config)
    pipeline.llm.chat = MagicMock(side_effect=_chat_script("no json", "no json"))

    with pytest.raises(RuntimeError, match="schema fill failed"):
        pipeline.run("a giant squirrel wrecks a kart race")


def test_run_log_manifest_links_the_report(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "unity_pipeline:\n"
        f"  unity_output_dir: '{(tmp_path / 'unity').as_posix()}'\n",
        encoding="utf-8",
    )
    argv = ["main.py", "unity-create", "a brief", "--config", str(config)]

    with pytest.raises(RuntimeError):
        with UnityCreateRunRecorder.from_argv(argv, cwd=tmp_path):
            print("Failed-run diagnostics: C:\\u\\unity\\_failed_runs\\r.json")
            raise RuntimeError("boom")

    manifest_path = next((tmp_path / "unity" / "_run_logs").glob("*.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["failed_run_report"] == "C:\\u\\unity\\_failed_runs\\r.json"
