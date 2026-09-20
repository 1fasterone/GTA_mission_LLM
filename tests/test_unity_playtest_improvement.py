"""Slice 4 tests for the read-only playtest improvement plan."""

from __future__ import annotations

import copy
import json

import pytest

from src.unity_playtest_improvement import build_playtest_improvement_plan


def _report(*, decision="PASS", fixes=None, scores=None):
    values = {
        "FUN_SCORE": {"score": 8, "evidence": [], "risks": [], "graded": True},
        "FRICTION_SCORE": {"score": 0, "evidence": [], "risks": [], "graded": True},
        "PACING_SCORE": {"score": 7, "evidence": [], "risks": [], "graded": True},
        "DIFFICULTY_SCORE": {"score": 5, "evidence": [], "risks": [], "graded": True},
        "READABILITY_SCORE": {"score": 8, "evidence": [], "risks": [], "graded": True},
        "SYSTEMIC_COHERENCE_SCORE": {"score": 8, "evidence": [], "risks": [], "graded": True},
        "DEAD_LOOP_DETECTOR": [],
        "REWARD_LOOP_VALIDATION": [],
        "MISSING_FEEDBACK_DETECTOR": [],
        "MISSING_AFFORDANCE_DETECTOR": [],
        "MISSION_FLOW_VALIDATION": [],
        "PLAYABILITY_FIXES": fixes or [],
        "UNITY_IR_IMPACT": [],
        "FINAL_DECISION": {"decision": decision, "justification": decision},
    }
    if scores:
        values.update(scores)
    return values


def _plan(report, *, previous_schemas=None, previous_atoms=None, schemas=None, atoms=None):
    return build_playtest_improvement_plan(
        schemas=schemas or {"missions": [{"id": "main"}]},
        atom_ir=atoms or [{"id": "move", "atom": "Move", "args": {"speed": 1}}],
        world_atoms=[{"atom": "SpawnEntity", "args": {"entity": "Gate"}}],
        character_atoms=[{"atom": "SetAnimationState", "args": {"state": "idle"}}],
        resolver_output={"objectives": [{"id": "reach_gate"}]},
        playability_report=report,
        previous_schemas=previous_schemas,
        previous_atom_ir=previous_atoms,
    )


def test_plan_has_exact_sections_and_passes_at_thresholds():
    report = _report()
    plan = _plan(report, previous_schemas={"missions": [{"id": "main"}]},
                 previous_atoms=[{"id": "move", "atom": "Move", "args": {"speed": 1}}])

    assert list(plan) == [
        "FAIL_SUMMARY",
        "ROOT_CAUSE_ANALYSIS",
        "REQUIRED_SCHEMA_UPDATES",
        "REQUIRED_ATOM_UPDATES",
        "ATOM_MUTATION_RULES",
        "SCHEMA_DIFF",
        "ATOM_DIFF",
        "RESOLVER_PATCH_GENERATOR",
        "THRESHOLD_GATING_LOGIC",
        "CI_LOOP_INSTRUCTIONS",
    ]
    gate = plan["THRESHOLD_GATING_LOGIC"]
    assert gate["FINAL_READY_FOR_RESOLVER"] is True
    assert gate["NEXT_ITERATION_REQUIRED"] is False
    assert plan["SCHEMA_DIFF"]["status"] == "unchanged"
    assert plan["ATOM_DIFF"]["status"] == "unchanged"
    assert plan["CI_LOOP_INSTRUCTIONS"][-1] == "Proceed to Unity IR generation."


def test_failed_report_has_exact_triggers_and_abstains_without_inventing_fixes():
    report = _report(
        decision="FAIL",
        fixes=[{
            "check": "mission_flow",
            "severity": "high",
            "summary": "objective 'reach_gate' has no resolver transition",
            "affected_atoms": ["Move"],
            "source": "RESOLVER_OUTPUT.objectives[reach_gate]",
            "recommended_correction": "Add a deterministic event source.",
        }],
        scores={
            "FUN_SCORE": {"score": 5, "graded": True},
            "FRICTION_SCORE": {"score": 7, "graded": True},
            "PACING_SCORE": {"score": 4, "graded": True},
            "READABILITY_SCORE": {"score": 6, "graded": True},
            "SYSTEMIC_COHERENCE_SCORE": {"score": 5, "graded": True},
        },
    )
    previous_schemas = {"missions": [{"id": "main"}], "rules": {"old": 1}}
    current_schemas = {"missions": [{"id": "main"}], "rules": {"old": 2}}
    previous_atoms = [{"id": "move", "atom": "Move", "args": {"speed": 1}}]
    current_atoms = [{"id": "move", "atom": "Move", "args": {"speed": 2}}]
    before = copy.deepcopy((current_schemas, current_atoms))

    plan = _plan(
        report,
        previous_schemas=previous_schemas,
        previous_atoms=previous_atoms,
        schemas=current_schemas,
        atoms=current_atoms,
    )

    assert plan["FAIL_SUMMARY"][0]["trigger"] == "PLAYABILITY_FIXES[0]"
    assert plan["ROOT_CAUSE_ANALYSIS"][0]["responsibility"]["resolver_refs"]
    assert plan["THRESHOLD_GATING_LOGIC"]["NEXT_ITERATION_REQUIRED"] is True
    assert plan["SCHEMA_DIFF"]["modified_rules"]
    assert plan["ATOM_DIFF"]["modified_atoms"]
    updates = (
        plan["REQUIRED_SCHEMA_UPDATES"]["SCHEMA_PATCH"]
        + plan["REQUIRED_ATOM_UPDATES"]["ATOM_PATCH"]
        + plan["RESOLVER_PATCH_GENERATOR"]["RESOLVER_PATCH"]
    )
    assert updates
    assert all(update["operation"] == "abstain" for update in updates)
    assert len({update["id"] for update in updates}) == len(updates)
    assert plan["REQUIRED_SCHEMA_UPDATES"]["UPDATED_SCHEMAS"] == current_schemas
    assert plan["REQUIRED_ATOM_UPDATES"]["UPDATED_ATOM_IR"] == current_atoms
    assert (current_schemas, current_atoms) == before


def test_missing_baselines_and_scores_abstain_loudly():
    report = _report()
    del report["PACING_SCORE"]
    plan = _plan(report)

    assert plan["SCHEMA_DIFF"]["status"] == "baseline_missing"
    assert plan["ATOM_DIFF"]["status"] == "baseline_missing"
    assert any(item["check"] == "threshold_gate" for item in plan["FAIL_SUMMARY"])
    assert plan["THRESHOLD_GATING_LOGIC"]["FINAL_READY_FOR_RESOLVER"] is False


def test_plan_is_json_deterministic_and_rejects_non_json_inputs():
    report = _report()
    first = _plan(report)
    second = _plan(report)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    with pytest.raises(ValueError, match="SCHEMAS must be JSON-compatible"):
        _plan(report, schemas={"bad": object()})
