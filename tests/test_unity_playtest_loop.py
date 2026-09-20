"""Acceptance tests for the Slice 7 mechanics retry controller."""

from __future__ import annotations

import copy
import json

import pytest

from src.unity_playtest_loop import (
    PlaytestLoopBlocked,
    evaluate_threshold_gate,
    loop_config_from_mapping,
    run_bounded_playtest_loop,
)


def _report(*, passing: bool = True, graded: bool = True, blocker: bool = False) -> dict:
    scores = {
        "FUN_SCORE": 7 if passing else 6,
        "FRICTION_SCORE": 4 if passing else 5,
        "PACING_SCORE": 6 if passing else 5,
        "READABILITY_SCORE": 7 if passing else 6,
        "SYSTEMIC_COHERENCE_SCORE": 7 if passing else 6,
    }
    return {
        **{
            key: {"score": score, "graded": graded, "evidence": [], "risks": []}
            for key, score in scores.items()
        },
        "DIFFICULTY_SCORE": {"score": 5, "graded": True, "evidence": [], "risks": []},
        "DEAD_LOOP_DETECTOR": [],
        "REWARD_LOOP_VALIDATION": [],
        "MISSING_FEEDBACK_DETECTOR": [],
        "MISSING_AFFORDANCE_DETECTOR": [],
        "MISSION_FLOW_VALIDATION": [],
        "PLAYABILITY_FIXES": ([{
            "check": "fixture",
            "severity": "blocker",
            "summary": "fixture blocker",
        }] if blocker else []),
        "UNITY_IR_IMPACT": [],
        "FINAL_DECISION": {"decision": "PASS" if passing else "FAIL"},
    }


def _empty_plan() -> dict:
    return {
        "REQUIRED_SCHEMA_UPDATES": {"SCHEMA_PATCH": []},
        "REQUIRED_ATOM_UPDATES": {"ATOM_PATCH": []},
        "RESOLVER_PATCH_GENERATOR": {"RESOLVER_PATCH": []},
    }


def _schema_patch(current: dict, *, operation_id: str = "schema-1", replacement: int | None = None) -> dict:
    old = current["rules"]["step"]
    return {
        "REQUIRED_SCHEMA_UPDATES": {"SCHEMA_PATCH": [{
            "id": operation_id,
            "operation": "modify",
            "target": "rules.step",
            "expected": old,
            "replacement": old + 1 if replacement is None else replacement,
            "trigger": "PLAYABILITY_REPORT.PLAYABILITY_FIXES[0]",
        }]},
        "REQUIRED_ATOM_UPDATES": {"ATOM_PATCH": []},
        "RESOLVER_PATCH_GENERATOR": {"RESOLVER_PATCH": []},
    }


def _loop_kwargs(tmp_path, *, evaluator, plan_builder, **overrides):
    values = {
        "schemas": {"rules": {"step": 0}},
        "atom_ir": [],
        "resolver_output": {"transitions": [], "dependencies": []},
        "evaluator": evaluator,
        "plan_builder": plan_builder,
        "world_atoms": [],
        "character_atoms": [],
        "max_iterations": 3,
        "auto_apply": True,
        "report_dir": tmp_path / "artifacts",
    }
    values.update(overrides)
    return values


def test_threshold_boundary_passes_and_difficulty_is_diagnostic():
    gate = evaluate_threshold_gate(_report())

    assert gate["passed"] is True
    assert gate["NEXT_ITERATION_REQUIRED"] is False
    assert gate["FINAL_READY_FOR_RESOLVER"] is True
    assert gate["diagnostic_scores"]["DIFFICULTY_SCORE"]["score"] == 5

    below = evaluate_threshold_gate(_report(passing=False))
    assert below["passed"] is False
    assert below["NEXT_ITERATION_REQUIRED"] is True
    assert below["FINAL_READY_FOR_RESOLVER"] is False


def test_gate_rejects_ungraded_scores_and_blockers():
    ungraded = evaluate_threshold_gate(_report(graded=False))
    assert ungraded["passed"] is False
    assert any(item["reason"] == "score is ungraded" for item in ungraded["checks"])

    blocker = evaluate_threshold_gate(_report(blocker=True))
    assert blocker["passed"] is False
    assert blocker["blocker_count"] == 1


def test_one_failing_iteration_applies_one_patch_and_then_passes(tmp_path):
    reports = [_report(passing=False), _report(passing=True)]
    reruns: list[int] = []

    def evaluator(**_kwargs):
        return reports.pop(0)

    def plan_builder(**kwargs):
        if kwargs["playability_report"]["FINAL_DECISION"]["decision"] == "PASS":
            return _empty_plan()
        return _schema_patch(kwargs["schemas"])

    def resolver_rerun(**kwargs):
        reruns.append(kwargs["schemas"]["rules"]["step"])
        return kwargs["resolver_output"]

    result = run_bounded_playtest_loop(**_loop_kwargs(
        tmp_path,
        evaluator=evaluator,
        plan_builder=plan_builder,
        resolver_rerun=resolver_rerun,
    ))

    assert result["status"] == "passed"
    assert result["FINAL_READY_FOR_RESOLVER"] is True
    assert result["NEXT_ITERATION_REQUIRED"] is False
    assert result["iteration_count"] == 2
    assert reruns == [1]
    assert result["SCHEMAS"]["rules"]["step"] == 1
    assert (tmp_path / "artifacts" / "iteration-000" / "schema.json").read_text(encoding="utf-8").find('"step": 0') >= 0
    assert (tmp_path / "artifacts" / "iteration-001" / "schema.json").read_text(encoding="utf-8").find('"step": 1') >= 0
    assert sorted(path.name for path in (tmp_path / "artifacts" / "iteration-000").iterdir()) == [
        "atoms.json", "diff.json", "gate.json", "plan.json", "report.json", "resolver.json", "schema.json",
    ]


def test_auto_apply_false_stops_for_review_without_mutating_input(tmp_path):
    schemas = {"rules": {"step": 0}}
    baseline = copy.deepcopy(schemas)
    values = _loop_kwargs(
        tmp_path,
        evaluator=lambda **_kwargs: _report(passing=False),
        plan_builder=lambda **kwargs: _schema_patch(kwargs["schemas"]),
        auto_apply=False,
    )
    values["schemas"] = schemas
    result = run_bounded_playtest_loop(**values)

    assert result["status"] == "review_required"
    assert result["iteration_count"] == 1
    assert result["FINAL_READY_FOR_RESOLVER"] is False
    assert schemas == baseline


def test_permanent_failure_stops_at_iteration_cap_and_preserves_all_artifacts(tmp_path):
    def evaluator(**_kwargs):
        return _report(passing=False)

    def plan_builder(**kwargs):
        return _schema_patch(kwargs["schemas"], operation_id=f"schema-{kwargs['schemas']['rules']['step']}")

    result = run_bounded_playtest_loop(**_loop_kwargs(
        tmp_path,
        evaluator=evaluator,
        plan_builder=plan_builder,
        max_iterations=3,
    ))

    assert result["status"] == "max_iterations"
    assert result["iteration_count"] == 3
    assert result["FINAL_READY_FOR_RESOLVER"] is False
    assert [result["SCHEMAS"]["rules"]["step"], result["iterations"][-1]["iteration"]] == [3, 2]
    assert all((tmp_path / "artifacts" / f"iteration-{i:03d}" / "report.json").exists() for i in range(3))


def test_repeated_failure_signature_blocks_before_fourth_iteration(tmp_path):
    def plan_builder(**kwargs):
        return _schema_patch(kwargs["schemas"], operation_id=f"schema-{kwargs['schemas']['rules']['step']}")

    result = run_bounded_playtest_loop(
        **_loop_kwargs(
            tmp_path,
            evaluator=lambda **_kwargs: _report(passing=False),
            plan_builder=plan_builder,
            max_iterations=5,
        )
    )

    assert result["status"] == "blocked"
    assert "failure signature" in result["reason"]
    assert result["iteration_count"] == 3


def test_noop_patch_is_blocked_before_retry(tmp_path):
    def noop_plan(**kwargs):
        return _schema_patch(kwargs["schemas"], replacement=kwargs["schemas"]["rules"]["step"])

    result = run_bounded_playtest_loop(
        **_loop_kwargs(
            tmp_path,
            evaluator=lambda **_kwargs: _report(passing=False),
            plan_builder=noop_plan,
        )
    )

    assert result["status"] == "blocked"
    assert "no content change" in result["reason"]
    assert result["iteration_count"] == 1


def test_artifacts_are_immutable_and_config_is_deterministic(tmp_path):
    config = loop_config_from_mapping({"mechanics_playtest": {
        "max_iterations": 4,
        "auto_apply": True,
        "require_graded_scores": False,
        "report_dir": "reports/playtest",
        "thresholds": {"fun_min": 8},
    }})
    assert config["max_iterations"] == 4
    assert config["thresholds"]["fun_min"] == 8
    assert config["require_graded_scores"] is False

    kwargs = _loop_kwargs(
        tmp_path,
        evaluator=lambda **_kwargs: _report(passing=True),
        plan_builder=lambda **_kwargs: _empty_plan(),
        auto_apply=False,
    )
    first = run_bounded_playtest_loop(**kwargs)
    assert first["status"] == "passed"
    with pytest.raises(PlaytestLoopBlocked, match="immutable artifact already exists"):
        run_bounded_playtest_loop(**kwargs)

    artifact = tmp_path / "artifacts" / "iteration-000" / "report.json"
    json.loads(artifact.read_text(encoding="utf-8"))
