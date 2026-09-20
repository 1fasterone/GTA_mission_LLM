"""Slice 8 acceptance tests for the pre-Unity-IR mechanics handoff."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.unity_pipeline import UnityPipeline
from src.unity_playtest_handoff import (
    MechanicsPlaytestGateError,
    mechanics_state_from_schema,
    run_schema_playtest_loop,
)
from src.unity_schema import (
    AtomInvocation,
    EntitySpec,
    MissionObjective,
    MissionSpec,
    UnitySchema,
    UnityScript,
)


def _schema(*, wiring: dict | None = None) -> UnitySchema:
    return UnitySchema(
        project_name="Slice8Proof",
        company_name="TestCo",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="walk",
        entities=[
            EntitySpec(
                name="Hero",
                kind="player",
                tag="Player",
                atoms=[AtomInvocation(atom="Follow", args={"target": "Gate"})],
            ),
            EntitySpec(name="Gate", kind="goal"),
        ],
        scripts=[UnityScript(name="PlayerController", type="player", description="move")],
        atoms=[AtomInvocation(atom="PlaySFX", args={"sound": "confirm"})],
        missions=[MissionSpec(
            id="main",
            title="Main",
            objectives=[MissionObjective(
                id="reach_gate",
                description="Reach the gate",
                type="reach",
                target_ref="Gate",
            )],
            rewards={"unlock": {"consumer": "ExitDoor"}},
        )],
        meta={"wiring": wiring or {}},
    )


def _config(tmp_path, *, gate: bool = False, auto_apply: bool = False) -> dict:
    return {
        "basic_mode": True,
        "unity_pipeline": {
            "lookups_dir": "lookups",
            "unity_template_dir": "templates/unity",
            "unity_output_dir": str(tmp_path / "projects"),
        },
        "mechanics_playtest": {
            "enabled": True,
            "gate": gate,
            "max_seconds": 300,
            "max_iterations": 3,
            "auto_apply": auto_apply,
            "report_dir": str(tmp_path / "playtest-artifacts"),
            "require_graded_scores": True,
        },
        "lm_studio": {
            "base_url": "http://localhost:1234/v1",
            "model": "test-model",
            "timeout": 1,
        },
        "audio": {"backend": "dsp"},
    }


def _passing_wiring() -> dict:
    return {
        "objectives_bound": ["MainObjectives.reach_gate"],
        "objectives_unbound": [],
        "objectives_optional_unbound": [],
        "objectives_self_driven": [],
        "hud_objective": True,
    }


def _passing_report(**_kwargs) -> dict:
    scores = {
        "FUN_SCORE": 7,
        "FRICTION_SCORE": 4,
        "PACING_SCORE": 6,
        "READABILITY_SCORE": 7,
        "SYSTEMIC_COHERENCE_SCORE": 7,
        "DIFFICULTY_SCORE": 5,
    }
    return {
        **{key: {"score": value, "graded": True} for key, value in scores.items()},
        "DEAD_LOOP_DETECTOR": [],
        "REWARD_LOOP_VALIDATION": [],
        "MISSING_FEEDBACK_DETECTOR": [],
        "MISSING_AFFORDANCE_DETECTOR": [],
        "MISSION_FLOW_VALIDATION": [],
        "PLAYABILITY_FIXES": [],
        "UNITY_IR_IMPACT": [],
        "FINAL_DECISION": {"decision": "PASS"},
    }


def test_schema_state_uses_adapter_and_composer_dry_run_without_ir_files(tmp_path):
    schema = _schema(wiring=_passing_wiring())
    before = schema.model_dump_json()
    state = mechanics_state_from_schema(
        schema, _config(tmp_path), compose=True,
    )

    assert schema.model_dump_json() == before
    assert state["RESOLVER_OUTPUT"]["transitions"]
    assert state["RESOLVER_OUTPUT"]["wiring_report"]["objectives_bound"]
    assert not (tmp_path / "projects").exists()


def test_passing_handoff_sets_final_ready_and_writes_iteration_artifact(tmp_path):
    schema = _schema(wiring=_passing_wiring())
    result = run_schema_playtest_loop(
        schema, _config(tmp_path), compose=False, evaluator=_passing_report,
    )

    assert result["status"] == "passed"
    assert result["FINAL_READY_FOR_RESOLVER"] is True
    assert result["NEXT_ITERATION_REQUIRED"] is False
    assert result["iteration_count"] == 1
    assert (tmp_path / "playtest-artifacts" / "iteration-000" / "report.json").exists()


def test_failed_handoff_preserves_schema_and_writes_complete_artifact(tmp_path):
    schema = _schema()
    before = schema.model_dump_json()
    result = run_schema_playtest_loop(schema, _config(tmp_path), compose=False)

    assert result["status"] == "review_required"
    assert result["FINAL_READY_FOR_RESOLVER"] is False
    assert schema.model_dump_json() == before
    artifact_dir = tmp_path / "playtest-artifacts" / "iteration-000"
    assert sorted(path.name for path in artifact_dir.iterdir()) == [
        "atoms.json", "diff.json", "gate.json", "plan.json", "report.json",
        "resolver.json", "schema.json",
    ]


def test_pipeline_gate_blocks_generator_before_unity_ir(tmp_path):
    config = _config(tmp_path, gate=True)
    pipeline = UnityPipeline(config)
    pipeline.llm.chat = MagicMock(side_effect=[
        "a deterministic concept",
        _schema().model_dump_json(),
    ])
    pipeline.generator.write = MagicMock()

    with pytest.raises(MechanicsPlaytestGateError):
        pipeline.run("a game prompt")

    pipeline.generator.write.assert_not_called()
    artifact = tmp_path / "playtest-artifacts" / "iteration-000"
    assert (artifact / "report.json").exists()
    assert (artifact / "diff.json").exists()


def test_state_rerun_refreshes_all_loop_snapshots(tmp_path):
    from src.unity_playtest_loop import run_bounded_playtest_loop

    report = {
        key: {"score": score, "graded": True}
        for key, score in {
            "FUN_SCORE": 6,
            "FRICTION_SCORE": 5,
            "PACING_SCORE": 5,
            "READABILITY_SCORE": 6,
            "SYSTEMIC_COHERENCE_SCORE": 6,
        }.items()
    }
    report.update({
        "DIFFICULTY_SCORE": {"score": 5, "graded": True},
        "DEAD_LOOP_DETECTOR": [], "REWARD_LOOP_VALIDATION": [],
        "MISSING_FEEDBACK_DETECTOR": [], "MISSING_AFFORDANCE_DETECTOR": [],
        "MISSION_FLOW_VALIDATION": [], "PLAYABILITY_FIXES": [],
        "UNITY_IR_IMPACT": [], "FINAL_DECISION": {"decision": "FAIL"},
    })
    passing = {**report}
    for key, score in {
        "FUN_SCORE": 7,
        "FRICTION_SCORE": 4,
        "PACING_SCORE": 6,
        "READABILITY_SCORE": 7,
        "SYSTEMIC_COHERENCE_SCORE": 7,
    }.items():
        passing[key] = {"score": score, "graded": True}
    passing["FINAL_DECISION"] = {"decision": "PASS"}

    reports = [report, passing]
    rerun_calls: list[int] = []

    def plan_builder(**kwargs):
        old = kwargs["schemas"]["step"]
        return {
            "REQUIRED_SCHEMA_UPDATES": {"SCHEMA_PATCH": [{
                "id": "step-change", "operation": "modify", "target": "step",
                "expected": old, "replacement": old + 1,
                "trigger": "PLAYABILITY_REPORT.PLAYABILITY_FIXES[0]",
            }]},
            "REQUIRED_ATOM_UPDATES": {"ATOM_PATCH": []},
            "RESOLVER_PATCH_GENERATOR": {"RESOLVER_PATCH": []},
        }

    def state_rerun(**kwargs):
        rerun_calls.append(kwargs["schemas"]["step"])
        return {
            "SCHEMAS": kwargs["schemas"],
            "ATOM_IR": kwargs["atom_ir"],
            "WORLD_ATOMS": [{"atom": "rerun-world"}],
            "CHARACTER_ATOMS": kwargs["character_atoms"],
            "RESOLVER_OUTPUT": kwargs["resolver_output"],
        }

    result = run_bounded_playtest_loop(
        schemas={"step": 0},
        atom_ir=[],
        world_atoms=[],
        character_atoms=[],
        resolver_output={"transitions": [], "dependencies": []},
        evaluator=lambda **_: reports.pop(0),
        plan_builder=plan_builder,
        state_rerun=state_rerun,
        auto_apply=True,
        report_dir=tmp_path / "state-artifacts",
    )

    assert result["status"] == "passed"
    assert rerun_calls == [1]
    assert result["WORLD_ATOMS"] == [{"atom": "rerun-world"}]
