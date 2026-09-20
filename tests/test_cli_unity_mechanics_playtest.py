"""CLI/config coverage for the deterministic pre-IR mechanics playtest."""

from __future__ import annotations

import json

import pytest
import typer
import yaml

from cli.unity_eval import unity_mechanics_playtest
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
        project_name="CliMechanicsProof",
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


def _config(tmp_path, *, gate=False, enabled=True, max_seconds=41) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "unity_pipeline": {"unity_template_dir": "templates/unity"},
        "mechanics_playtest": {
            "enabled": enabled,
            "gate": gate,
            "max_seconds": max_seconds,
        },
    }), encoding="utf-8")
    return str(path)


def test_cli_writes_structured_report_and_uses_config_horizon(tmp_path):
    schema_path = tmp_path / "CliMechanicsProof.schema.json"
    schema_path.write_text(_schema(wiring={
        "objectives_bound": ["MainObjectives.reach_gate"],
        "objectives_unbound": [],
        "objectives_optional_unbound": [],
        "objectives_self_driven": [],
        "hud_objective": True,
    }).model_dump_json(indent=2), encoding="utf-8")
    report_path = tmp_path / "reports" / "mechanics.json"

    unity_mechanics_playtest(
        str(schema_path),
        config=_config(tmp_path),
        compose=False,
        output=str(report_path),
        json_output=False,
        strict=False,
        max_seconds=None,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert list(report) == [
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
    assert report["FINAL_DECISION"]["decision"] == "PASS"
    assert report["FINAL_DECISION"]["simulation_seconds"] == 41


def test_cli_strict_gate_fails_for_unbound_required_objective(tmp_path):
    schema_path = tmp_path / "CliMechanicsProof.schema.json"
    schema_path.write_text(_schema().model_dump_json(indent=2), encoding="utf-8")

    with pytest.raises(typer.Exit) as exc:
        unity_mechanics_playtest(
            str(schema_path),
            config=_config(tmp_path, gate=True),
            compose=False,
            output=None,
            json_output=False,
            strict=False,
            max_seconds=None,
        )

    assert exc.value.exit_code == 1


def test_compose_mode_is_a_dry_run_and_does_not_create_project_files(tmp_path):
    schema_path = tmp_path / "CliMechanicsProof.schema.json"
    schema_path.write_text(_schema().model_dump_json(indent=2), encoding="utf-8")
    config_path = _config(tmp_path)
    before = sorted(path.name for path in tmp_path.iterdir())

    unity_mechanics_playtest(
        str(schema_path),
        config=config_path,
        compose=True,
        output=None,
        json_output=False,
        strict=False,
        max_seconds=0,
    )

    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_disabled_config_skips_before_reading_schema(tmp_path, capsys):
    unity_mechanics_playtest(
        str(tmp_path / "does-not-exist.schema.json"),
        config=_config(tmp_path, enabled=False),
        compose=False,
        output=None,
        json_output=False,
        strict=True,
        max_seconds=None,
    )

    assert "disabled by config" in capsys.readouterr().out
