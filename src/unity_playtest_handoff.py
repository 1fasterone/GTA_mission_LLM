"""Deterministic mechanics-playtest handoff before Unity IR generation.

Slice 8 owns the boundary between the finished deterministic schema tail and
Unity generation.  It builds the same mechanics adapter/composer dry-run used
by the standalone playtest command, then delegates retry control to Slice 7.
No Unity files are emitted here and no LLM client is constructed or called.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .unity_mechanics_adapter import build_mechanics_playtest_input
from .unity_mechanics_playtest import MechanicsPlaytestInput, evaluate_mechanics
from .unity_playtest_loop import (
    PlaytestLoopError,
    loop_config_from_mapping,
    run_bounded_playtest_loop,
)
from .unity_playtest_improvement import build_playtest_improvement_plan
from .unity_schema import UnitySchema


class MechanicsPlaytestGateError(PlaytestLoopError):
    """The pre-IR mechanics gate failed, so Unity IR must not be emitted."""


def _config_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = config.get("mechanics_playtest", {})
    if not isinstance(raw, Mapping):
        raise MechanicsPlaytestGateError("mechanics_playtest config must be an object")
    return raw


def _schema_snapshot(schema: UnitySchema) -> dict[str, Any]:
    return schema.model_dump(mode="json")


def compose_mechanics_state(
    schema: UnitySchema,
    config: Mapping[str, Any],
    *,
    compose: bool,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Build composed units and the authoritative wiring dry-run.

    ``MicroComposer.plan`` and ``plan_wiring`` only calculate deterministic
    data.  They do not call the generator or write an Assets tree.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    saved_report = meta.get("wiring")
    wiring_report = dict(saved_report) if isinstance(saved_report, Mapping) else {}
    if not compose:
        return (), wiring_report

    from .unity_compose import MicroComposer, subsystems_for
    from .unity_pipeline import _compose_palette_rgb, _infer_mission_scenes, _objective_scene_map

    pipe_cfg = config.get("unity_pipeline", config.get("pipeline", {})) or {}
    if not isinstance(pipe_cfg, Mapping):
        raise MechanicsPlaytestGateError("unity_pipeline config must be an object")
    features = list(schema.features or [])
    if "audio" not in features:
        features.append("audio")

    mission_scenes = meta.get("mission_scenes")
    if not isinstance(mission_scenes, dict):
        mission_scenes, _ = _infer_mission_scenes(schema)

    subsystems = subsystems_for(
        schema.game_type,
        features,
        locomotion=schema.locomotion,
        locomotion_modes=schema.locomotion_modes,
        flying_abilities=meta.get("flying_abilities"),
        capabilities=schema.capabilities,
        user_prompt=meta.get("user_prompt", ""),
        scenes=schema.scenes,
        entities=schema.entities,
        scripts=schema.scripts,
        genre=meta.get("genre", "universal"),
        audio_specs=schema.audio_specs,
        vfx_specs=schema.vfx,
        shaders=schema.shaders,
        animations=schema.animations,
        characters=schema.characters,
        missions=schema.missions,
        behavior_trees=schema.behavior_trees,
        environment=schema.environment,
        visual_style=schema.visual_style,
        palette_rgb=_compose_palette_rgb(schema),
        cover_points=meta.get("cover_points"),
    )
    composer = MicroComposer(
        schema.project_name,
        template_dir=pipe_cfg.get("unity_template_dir", "templates/unity"),
        project_config=dict(config),
    )
    units = composer.plan(subsystems)
    escort_followed_entities = {
        entity.name for entity in schema.entities
        if entity.name and entity.behavior
        and any(getattr(atom, "atom", "") == "Follow" for atom in (entity.atoms or []))
    }
    reach_target_entities = {
        entity.name: entity.count for entity in schema.entities if entity.name
    }
    wiring_report = composer.plan_wiring(
        units,
        wiring_map=schema.wiring,
        objective_scenes=_objective_scene_map(schema, mission_scenes),
        escort_followed_entities=escort_followed_entities,
        reach_target_entities=reach_target_entities,
        game_type=schema.game_type,
    )
    return tuple(units), dict(wiring_report)


def mechanics_state_from_schema(
    schema: UnitySchema,
    config: Mapping[str, Any],
    *,
    compose: bool,
) -> dict[str, Any]:
    """Return detached loop snapshots derived from a Unity schema."""
    units, wiring_report = compose_mechanics_state(schema, config, compose=compose)
    playtest_input = build_mechanics_playtest_input(
        schema,
        composed_units=units,
        wiring_report=wiring_report,
    )
    data = playtest_input.to_dict()
    return {
        "SCHEMAS": _schema_snapshot(schema),
        "ATOM_IR": data["ATOM_IR"],
        "WORLD_ATOMS": data["WORLD_ATOMS"],
        "CHARACTER_ATOMS": data["CHARACTER_ATOMS"],
        "RESOLVER_OUTPUT": data["RESOLVER_OUTPUT"],
    }


def _evaluate_snapshot(*, max_seconds: int, **snapshots: Any) -> dict[str, Any]:
    playtest_input = MechanicsPlaytestInput.from_dict({
        "ATOM_IR": snapshots["atom_ir"],
        "WORLD_ATOMS": snapshots["world_atoms"],
        "CHARACTER_ATOMS": snapshots["character_atoms"],
        "RESOLVER_OUTPUT": snapshots["resolver_output"],
    })
    return evaluate_mechanics(playtest_input, max_seconds=max_seconds).to_dict()


def run_schema_playtest_loop(
    schema: UnitySchema,
    config: Mapping[str, Any],
    *,
    compose: bool = False,
    evaluator: Callable[..., Any] | None = None,
    plan_builder: Callable[..., Any] | None = None,
    state_rerun: Callable[..., Any] | None = None,
    max_seconds: int | None = None,
    max_iterations: int | None = None,
    auto_apply: bool | None = None,
    report_dir: str | None = None,
) -> dict[str, Any]:
    """Run the bounded mechanics loop against a resolved Unity schema.

    The input schema is never mutated.  A ``state_rerun`` hook is supplied by
    the live pipeline so every accepted patch runs the real resolver/fixer and
    rebuilds the adapter snapshots.  Standalone schema replay uses the
    deterministic adapter rebuild as its safe fallback.
    """
    if not isinstance(schema, UnitySchema):
        raise MechanicsPlaytestGateError("schema must be a UnitySchema")
    if not isinstance(config, Mapping):
        raise MechanicsPlaytestGateError("config must be an object")
    mechanics_cfg = _config_section(config)
    loop_cfg = loop_config_from_mapping(config)
    if mechanics_cfg.get("enabled", True) is False:
        return {
            "status": "disabled",
            "reason": "mechanics playtest disabled by config",
            "iterations": [],
            "iteration_count": 0,
            "NEXT_ITERATION_REQUIRED": False,
            "FINAL_READY_FOR_RESOLVER": False,
            "SCHEMAS": _schema_snapshot(schema),
            "ATOM_IR": [],
            "WORLD_ATOMS": [],
            "CHARACTER_ATOMS": [],
            "RESOLVER_OUTPUT": {},
        }

    seconds_value = mechanics_cfg.get("max_seconds", 300) if max_seconds is None else max_seconds
    try:
        seconds = int(seconds_value)
    except (TypeError, ValueError) as exc:
        raise MechanicsPlaytestGateError("max_seconds must be an integer") from exc
    if seconds < 0:
        raise MechanicsPlaytestGateError("max_seconds must be non-negative")

    initial = mechanics_state_from_schema(schema, config, compose=compose)
    evaluator_fn = evaluator or (lambda **kwargs: _evaluate_snapshot(
        max_seconds=seconds, **kwargs))

    if state_rerun is None:
        def state_rerun(**kwargs: Any) -> dict[str, Any]:
            candidate = UnitySchema.model_validate(kwargs["schemas"])
            return mechanics_state_from_schema(candidate, config, compose=compose)

    result = run_bounded_playtest_loop(
        schemas=initial["SCHEMAS"],
        atom_ir=initial["ATOM_IR"],
        world_atoms=initial["WORLD_ATOMS"],
        character_atoms=initial["CHARACTER_ATOMS"],
        resolver_output=initial["RESOLVER_OUTPUT"],
        evaluator=evaluator_fn,
        plan_builder=plan_builder or build_playtest_improvement_plan,
        state_rerun=state_rerun,
        previous_schemas=initial["SCHEMAS"],
        previous_atom_ir=initial["ATOM_IR"],
        thresholds=loop_cfg["thresholds"],
        require_graded_scores=loop_cfg["require_graded_scores"],
        max_iterations=max_iterations if max_iterations is not None else loop_cfg["max_iterations"],
        auto_apply=auto_apply if auto_apply is not None else loop_cfg["auto_apply"],
        report_dir=report_dir if report_dir is not None
        else loop_cfg["report_dir"],
    )
    result["max_seconds"] = seconds
    result["compose"] = compose
    return result


__all__ = [
    "MechanicsPlaytestGateError",
    "compose_mechanics_state",
    "mechanics_state_from_schema",
    "run_schema_playtest_loop",
]
