"""Slice 2 tests for UnitySchema and composition-plan adaptation."""

from __future__ import annotations

from types import SimpleNamespace

from src.unity_compose import MicroComposer, subsystems_for
from src.unity_mechanics_adapter import build_mechanics_playtest_input
from src.unity_schema import (
    AtomInvocation,
    EntitySpec,
    MissionObjective,
    MissionSpec,
    UnitySchema,
    UnityScript,
)


def _schema(**overrides) -> UnitySchema:
    values = {
        "project_name": "AdapterProof",
        "game_type": "3D",
        "platform": "pc",
        "render_pipeline": "URP",
        "entities": [
            EntitySpec(
                name="Hero",
                kind="player",
                tag="Player",
                atoms=[AtomInvocation(atom="Follow", args={"target": "Gate"})],
            ),
            EntitySpec(name="Gate", kind="goal"),
        ],
        "scripts": [UnityScript(name="PlayerController", type="player", description="move")],
        "atoms": [AtomInvocation(atom="PlaySFX", args={"sound": "confirm"})],
        "missions": [MissionSpec(
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
    }
    values.update(overrides)
    return UnitySchema(**values)


def test_adapter_maps_schema_layers_without_mutating_schema():
    schema = _schema()
    before = schema.model_dump_json()
    result = build_mechanics_playtest_input(schema)

    assert schema.model_dump_json() == before
    assert [atom.name for atom in result.atom_ir] == ["PlaySFX"]
    assert [atom.name for atom in result.character_atoms] == ["Follow"]
    assert result.resolver_output["entities"][0]["name"] == "Hero"
    assert result.resolver_output["objectives"][0]["target_ref"] == "Gate"
    assert result.resolver_output["rewards"][0]["consumer"] == "ExitDoor"


def test_adapter_converts_composer_wiring_report_to_transition_edges():
    schema = _schema()
    report = {
        "objectives_bound": ["MainObjectives.reach_gate"],
        "objectives_unbound": [],
        "objectives_optional_unbound": [],
        "objectives_self_driven": [],
        "hud_objective": True,
    }
    result = build_mechanics_playtest_input(
        schema,
        wiring_report=report,
    )

    transitions = result.resolver_output["transitions"]
    assert transitions == [{
        "id": "event:reach_gate",
        "source": "composer_event",
        "target": "reach_gate",
        "action": "reach",
        "objective_id": "reach_gate",
        "progress": True,
        "completion": True,
        "feedback": ["hud_objective"],
    }]
    assert result.resolver_output["affordances"] == [{
        "objective_id": "reach_gate",
        "target": "Gate",
        "type": "reach",
        "cue": "hud",
    }]


def test_adapter_output_can_be_evaluated_without_composed_unit_objects():
    schema = _schema()
    result = build_mechanics_playtest_input(schema, wiring_report={
        "objectives_bound": ["MainObjectives.reach_gate"],
        "objectives_unbound": [],
        "objectives_optional_unbound": [],
        "objectives_self_driven": [],
        "hud_objective": True,
    })

    assert result.resolver_output["affordances"][0]["cue"] == "hud"


def test_unbound_composer_objective_stays_unbound():
    schema = _schema()
    result = build_mechanics_playtest_input(schema, wiring_report={
        "objectives_bound": [],
        "objectives_unbound": ["MainObjectives.reach_gate (reach)"],
        "objectives_optional_unbound": [],
        "objectives_self_driven": [],
    })

    [transition] = result.resolver_output["transitions"]
    assert transition["source"] == "unbound_event"
    assert transition["completion"] is False
    assert result.resolver_output["dependencies"][-1]["status"] == "missing"


def test_plan_wiring_reuses_composer_logic_without_emitting_files():
    schema = _schema()
    subsystems = subsystems_for(
        schema.game_type,
        schema.features,
        locomotion=schema.locomotion,
        missions=schema.missions,
        entities=schema.entities,
        scripts=schema.scripts,
    )
    composer = MicroComposer("AdapterProof", project_config={"audio": {"backend": "dsp"}})
    units = composer.plan(subsystems)
    report = composer.plan_wiring(
        units,
        objective_scenes={},
        reach_target_entities={entity.name: entity.count for entity in schema.entities},
        game_type=schema.game_type,
    )

    assert report is not composer.wiring_report
    assert set(report) >= {
        "seams",
        "objectives_bound",
        "objectives_unbound",
        "objectives_optional_unbound",
        "objectives_self_driven",
        "objectives_total",
        "objectives_required",
        "hud_objective",
    }
    assert report["objectives_total"] == 1
    assert report["objectives_bound"] or report["objectives_unbound"]
