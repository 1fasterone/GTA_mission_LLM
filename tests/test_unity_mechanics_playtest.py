"""Slice 1 tests for the deterministic pre-IR mechanics playtest contract."""

from __future__ import annotations

import json

import pytest

from src.unity_mechanics_playtest import (
    SEVERITY_BLOCKER,
    MechanicsPlaytestInput,
    evaluate_mechanics,
)


def _input(**resolver_output):
    return MechanicsPlaytestInput.from_dict({
        "ATOM_IR": [
            {"atom": "MoveTo", "args": {"target": "Gate"}},
            {"atom": "PlaySFX", "args": {"sound": "confirm"}},
        ],
        "WORLD_ATOMS": [
            {"atom": "SpawnEntity", "args": {"entity": "Gate"}},
        ],
        "CHARACTER_ATOMS": [
            {"atom": "SetAnimationState", "args": {"state": "interact"}},
        ],
        "RESOLVER_OUTPUT": resolver_output,
    })


def _winnable():
    return _input(
        entities=[{"name": "Gate", "kind": "goal"}],
        controls=[{"name": "move"}, {"name": "interact"}],
        objectives=[{
            "id": "open_gate",
            "type": "reach",
            "target_ref": "Gate",
        }],
        affordances=[{"target": "Gate", "type": "reach", "cue": "waypoint"}],
        feedback=[{"objective_id": "open_gate", "event": "hud_progress"}],
        transitions=[{
            "id": "reach_gate",
            "source": "start",
            "target": "gate_reached",
            "action": "reach",
            "objective_id": "open_gate",
            "completion": True,
            "feedback": ["hud_progress"],
        }],
        rewards=[{"name": "unlock_exit", "consumer": "exit_gate"}],
    )


def test_report_has_exact_requested_top_level_sections():
    report = evaluate_mechanics(_winnable())
    assert list(report.to_dict()) == [
        "FUN_SCORE",
        "FRICTION_SCORE",
        "PACING_SCORE",
        "DIFFICULTY_SCORE",
        "READABILITY_SCORE",
        "SYSTEMIC_COHERENCE_SCORE",
        "DEAD_LOOP_DETECTOR",
        "REWARD_LOOP_VALIDATION",
        "MISSING_FEEDBACK_DETECTOR",
        "MISSING_AFFORDANCE_DETECTOR",
        "MISSION_FLOW_VALIDATION",
        "PLAYABILITY_FIXES",
        "UNITY_IR_IMPACT",
        "FINAL_DECISION",
    ]


def test_winnable_loop_passes_and_has_no_high_risk_findings():
    report = evaluate_mechanics(_winnable())
    assert report.final_decision["decision"] == "PASS"
    assert report.mission_flow_validation[0]["status"] == "pass"
    assert report.playability_fixes == ()
    assert report.friction_score.score == 0
    assert report.final_decision["simulation_seconds"] == 300


def test_unknown_atom_fails_loudly():
    playtest_input = _winnable()
    bad = playtest_input.to_dict()
    bad["ATOM_IR"].append({"atom": "InventedMechanic", "args": {}})
    report = evaluate_mechanics(MechanicsPlaytestInput.from_dict(bad))
    assert report.final_decision["decision"] == "FAIL"
    assert any(
        finding.check == "atom_validation"
        and finding.severity == SEVERITY_BLOCKER
        for finding in report.playability_fixes
    )


def test_required_objective_without_target_or_transition_fails():
    report = evaluate_mechanics(_input(
        entities=[],
        controls=[{"name": "move"}],
        objectives=[{"id": "find_key", "type": "collect", "target_ref": "Key"}],
    ))
    checks = {finding.check for finding in report.playability_fixes}
    assert report.final_decision["decision"] == "FAIL"
    assert "mission_flow" in checks


def test_dead_self_loop_is_reported_as_blocker():
    report = evaluate_mechanics(_input(
        entities=[{"name": "Gate"}],
        controls=[{"name": "move"}],
        objectives=[{"id": "wait", "type": "survive"}],
        transitions=[{
            "id": "repeat",
            "source": "alarm",
            "target": "alarm",
            "action": "wait",
        }],
    ))
    assert report.dead_loop_detector[0]["severity"] == SEVERITY_BLOCKER
    assert any(finding.check == "dead_loop" for finding in report.playability_fixes)


def test_missing_feedback_and_affordance_are_visible():
    report = evaluate_mechanics(_input(
        entities=[{"name": "Gate"}],
        controls=[{"name": "move"}],
        objectives=[{"id": "reach_gate", "type": "reach", "target_ref": "Gate"}],
        transitions=[{
            "source": "start",
            "target": "done",
            "action": "reach",
            "objective_id": "reach_gate",
            "completion": True,
        }],
    ))
    checks = {finding.check for finding in report.playability_fixes}
    assert "missing_feedback" in checks
    assert "missing_affordance" in checks
    assert report.missing_feedback_detector
    assert report.missing_affordance_detector


def test_reward_without_consumer_is_reported():
    report = evaluate_mechanics(_input(
        entities=[{"name": "Gate"}],
        controls=[{"name": "move"}],
        objectives=[{"id": "reach_gate", "type": "reach", "target_ref": "Gate"}],
        affordances=[{"target": "Gate", "type": "reach"}],
        feedback=[{"objective_id": "reach_gate", "event": "complete"}],
        transitions=[{
            "source": "start",
            "target": "done",
            "action": "reach",
            "objective_id": "reach_gate",
            "completion": True,
            "feedback": ["complete"],
        }],
        rewards=[{"name": "xp"}],
    ))
    assert any(finding.check == "reward_loop" for finding in report.playability_fixes)
    assert report.reward_loop_validation[0]["status"] == "missing_consumer"


def test_input_round_trip_is_json_serializable_and_deterministic():
    first = _winnable()
    second = MechanicsPlaytestInput.from_dict(json.loads(json.dumps(first.to_dict())))
    assert first.to_dict() == second.to_dict()
    assert evaluate_mechanics(first).to_dict() == evaluate_mechanics(second).to_dict()


def test_malformed_contract_is_rejected_loudly():
    with pytest.raises(ValueError, match="RESOLVER_OUTPUT.objectives must be a list"):
        MechanicsPlaytestInput.from_dict({
            "ATOM_IR": [],
            "WORLD_ATOMS": [],
            "CHARACTER_ATOMS": [],
            "RESOLVER_OUTPUT": {"objectives": "not-a-list"},
        })
