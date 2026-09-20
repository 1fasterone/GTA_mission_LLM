"""UnitySchema -> deterministic mechanics-playtest input adapter.

Slice 2 connects the engine-neutral evaluator in
``src/unity_mechanics_playtest.py`` to the repository's existing Unity schema
and composition metadata.  It reads only already-resolved deterministic data;
it does not mutate the schema, call an LLM, emit Unity files, or infer new
schema fields.

The composition side accepts ``composed_units`` and a ``wiring_report`` from
``MicroComposer.plan()``/``MicroComposer.plan_wiring()``.  Passing the report
from ``plan_wiring`` is important: it is produced by the exact seam code that
later emits ``GameWiring.cs``, so the pre-IR playtest does not maintain a
second, drifting wiring implementation.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from .unity_mechanics_playtest import MechanicsPlaytestInput
from .unity_schema import UnitySchema


_CHARACTER_KINDS = frozenset({"player", "character", "enemy", "boss", "npc", "companion"})


def _atom_dict(invocation: Any) -> dict[str, Any]:
    return {
        "atom": str(getattr(invocation, "atom", "")),
        "args": dict(getattr(invocation, "args", {}) or {}),
        "label": str(getattr(invocation, "label", "") or ""),
    }


def _unique_atoms(invocations: Sequence[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for invocation in invocations:
        item = _atom_dict(invocation)
        key = (
            item["atom"],
            json.dumps(item["args"], sort_keys=True, default=str),
            item["label"],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _entities(schema: UnitySchema) -> list[dict[str, Any]]:
    return [
        {
            "name": entity.name,
            "kind": entity.kind,
            "count": entity.count,
            "tag": entity.tag,
            "placement": entity.placement,
        }
        for entity in schema.entities
    ]


def _objectives(schema: UnitySchema) -> list[dict[str, Any]]:
    objectives: list[dict[str, Any]] = []
    for mission in schema.missions:
        for objective in mission.objectives:
            objectives.append({
                "id": objective.id,
                "mission_id": mission.id,
                "mission_title": mission.title,
                "description": objective.description,
                "type": objective.type,
                "target_ref": objective.target_ref,
                "count": objective.count,
                "optional": objective.optional,
            })
    return objectives


def _qualified_matches(qualified_ids: Sequence[str], objective_id: str) -> bool:
    return any(
        (
            value == objective_id
            or value.endswith("." + objective_id)
            or value.split(" ", 1)[0].endswith("." + objective_id)
        )
        for value in qualified_ids
    )


def _transitions(
    schema: UnitySchema,
    wiring_report: Mapping[str, Any],
) -> list[dict[str, Any]]:
    bound = [str(value) for value in wiring_report.get("objectives_bound", [])]
    unbound = [str(value) for value in wiring_report.get("objectives_unbound", [])]
    optional_unbound = [str(value) for value in wiring_report.get(
        "objectives_optional_unbound", [])]
    self_driven = [str(value) for value in wiring_report.get(
        "objectives_self_driven", [])]
    hud_objective = bool(wiring_report.get("hud_objective"))

    transitions: list[dict[str, Any]] = []
    for objective in _objectives(schema):
        objective_id = objective["id"]
        objective_type = objective["type"]
        is_self_driven = _qualified_matches(self_driven, objective_id)
        is_bound = _qualified_matches(bound, objective_id)
        is_unbound = _qualified_matches((*unbound, *optional_unbound), objective_id)
        if is_self_driven:
            transitions.append({
                "id": f"timer:{objective_id}",
                "source": "timer",
                "target": objective_id,
                "action": objective_type,
                "objective_id": objective_id,
                "progress": True,
                "completion": True,
                "feedback": ["hud_objective"] if hud_objective else [],
            })
        elif is_bound:
            transitions.append({
                "id": f"event:{objective_id}",
                "source": "composer_event",
                "target": objective_id,
                "action": objective_type,
                "objective_id": objective_id,
                "progress": True,
                "completion": True,
                "feedback": ["hud_objective"] if hud_objective else [],
            })
        elif is_unbound:
            transitions.append({
                "id": f"unbound:{objective_id}",
                "source": "unbound_event",
                "target": objective_id,
                "action": objective_type,
                "objective_id": objective_id,
                "progress": False,
                "completion": False,
            })

    # A non-composed schema can still declare a self-driven timer objective;
    # this is the one objective behavior whose resolver contract is complete
    # without an event seam. Other objectives remain unbound rather than being
    # guessed into a transition.
    if not wiring_report:
        for objective in _objectives(schema):
            if objective["type"] in {"survive", "timer"}:
                transitions.append({
                    "id": f"timer:{objective['id']}",
                    "source": "timer",
                    "target": objective["id"],
                    "action": objective["type"],
                    "objective_id": objective["id"],
                    "progress": True,
                    "completion": True,
                })
    return transitions


def _rewards(schema: UnitySchema) -> list[dict[str, Any]]:
    rewards: list[dict[str, Any]] = []
    for mission in schema.missions:
        for name, value in sorted((mission.rewards or {}).items(), key=lambda item: str(item[0])):
            record = {
                "mission_id": mission.id,
                "name": str(name),
                "value": value,
            }
            if isinstance(value, Mapping):
                record.update({
                    key: value[key]
                    for key in ("consumer", "consumed_by", "effect")
                    if key in value
                })
            rewards.append(record)
    return rewards


def _feedback(
    schema: UnitySchema,
    composed_units: Sequence[Any],
    wiring_report: Mapping[str, Any],
    root_atoms: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    feedback: list[dict[str, Any]] = []
    for spec in schema.audio_specs:
        feedback.append({"kind": "audio", "id": spec.id, "description": spec.description})
    for spec in schema.vfx:
        feedback.append({"kind": "vfx", "id": spec.id, "name": spec.name})
    for spec in schema.animations:
        feedback.append({"kind": "animation", "id": spec.id, "name": spec.name})
    for atom in root_atoms:
        name = str(atom.get("atom", ""))
        if name in {"PlaySFX", "SpawnParticle", "SetAnimationState", "UpdateHealth",
                    "UpdateAmmo", "UpdateObjective", "ShowHUD"}:
            feedback.append({"kind": "atom", "atom": name})
    if wiring_report.get("hud_objective"):
        feedback.append({"kind": "hud", "event": "hud_objective"})
    for unit in composed_units:
        kind = str(getattr(unit, "kind", ""))
        setters = set(getattr(unit, "setters", []) or [])
        if kind in {"audio", "vfx", "animator"}:
            feedback.append({"kind": kind, "class_name": getattr(unit, "class_name", "")})
        if "SetObjective" in setters:
            feedback.append({"kind": "hud", "event": "hud_objective", "class_name": unit.class_name})
    return feedback


def _controls(schema: UnitySchema, composed_units: Sequence[Any], root_atoms: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    if schema.locomotion == "none":
        controls.append({"name": "pointer", "mode": "pointer"})
    if schema.locomotion != "none" and any(script.type == "player" for script in schema.scripts):
        controls.append({"name": "player_controller", "mode": schema.locomotion})
    if schema.meta.get("composed_player_controller"):
        controls.append({"name": "composed_player_controller", "mode": schema.locomotion})
    if any(atom.get("atom") in {"BindAction", "Move", "Jump", "Interact"} for atom in root_atoms):
        controls.append({"name": "atom_control_surface", "mode": schema.locomotion})
    for unit in composed_units:
        if getattr(unit, "kind", "") in {"controller", "board", "input"}:
            controls.append({
                "name": getattr(unit, "class_name", getattr(unit, "kind", "")),
                "mode": schema.locomotion,
                "kind": getattr(unit, "kind", ""),
            })
    return controls


def _affordances(
    schema: UnitySchema,
    composed_units: Sequence[Any],
    root_atoms: Sequence[Mapping[str, Any]],
    wiring_report: Mapping[str, Any],
) -> list[dict[str, Any]]:
    has_waypoint = any(getattr(unit, "kind", "") == "waypoint" for unit in composed_units)
    hud_objective = any(
        "SetObjective" in (getattr(unit, "setters", []) or [])
        for unit in composed_units
    ) or any(atom.get("atom") == "UpdateObjective" for atom in root_atoms)
    # ``plan_wiring`` is the authoritative dry-run of the same composition
    # seam that emits the HUD setter.  Keep the adapter useful to callers that
    # retain the report but not the transient composed-unit objects.
    hud_objective = hud_objective or bool(wiring_report.get("hud_objective"))
    affordances: list[dict[str, Any]] = []
    for objective in _objectives(schema):
        if has_waypoint:
            cue = "waypoint"
        elif hud_objective:
            cue = "hud"
        else:
            continue
        affordances.append({
            "objective_id": objective["id"],
            "target": objective["target_ref"],
            "type": objective["type"],
            "cue": cue,
        })
    return affordances


def _dependencies(schema: UnitySchema, wiring_report: Mapping[str, Any]) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    if schema.wiring is not None:
        dependencies.extend({
            "source": dependency.source,
            "target": dependency.target,
            "type": dependency.type,
            "status": "declared",
        } for dependency in schema.wiring.dependencies)
    bound = [str(value) for value in wiring_report.get("objectives_bound", [])]
    unbound = [str(value) for value in wiring_report.get("objectives_unbound", [])]
    optional_unbound = [str(value) for value in wiring_report.get(
        "objectives_optional_unbound", [])]
    for objective in _objectives(schema):
        objective_id = objective["id"]
        if _qualified_matches(bound, objective_id):
            status = "resolved"
        elif _qualified_matches((*unbound, *optional_unbound), objective_id):
            status = "missing"
        else:
            continue
        dependencies.append({
            "source": "objective_event",
            "target": objective_id,
            "type": objective["type"],
            "status": status,
        })
    return dependencies


def build_mechanics_playtest_input(
    schema: UnitySchema,
    *,
    composed_units: Sequence[Any] = (),
    wiring_report: Mapping[str, Any] | None = None,
) -> MechanicsPlaytestInput:
    """Build the Slice 2 mechanics input from a resolved Unity schema.

    The function is read-only.  ``wiring_report`` should come from
    ``MicroComposer.plan_wiring`` when composition is enabled; otherwise an
    existing ``schema.meta['wiring']`` report is used if present.
    """
    report = dict(wiring_report or schema.meta.get("wiring") or {})
    root_invocations = list(schema.atoms)
    for microgen in schema.microgens:
        root_invocations.extend(microgen.atoms)
    root_atoms = _unique_atoms(root_invocations)

    world_atoms: list[dict[str, Any]] = []
    character_atoms: list[dict[str, Any]] = []
    for entity in schema.entities:
        destination = character_atoms if entity.kind.lower() in _CHARACTER_KINDS else world_atoms
        destination.extend(_atom_dict(atom) for atom in entity.atoms)
    for script in schema.scripts:
        destination = (
            character_atoms
            if script.type in {"player", "enemy", "health", "collectible"}
            else world_atoms
        )
        destination.extend(_atom_dict(atom) for atom in script.atoms)

    composed_units = tuple(composed_units)
    resolver_output = {
        "project_name": schema.project_name,
        "game_type": schema.game_type,
        "mode": "pointer" if schema.locomotion == "none" else "avatar",
        "entities": _entities(schema),
        "objectives": _objectives(schema),
        "transitions": _transitions(schema, report),
        "rewards": _rewards(schema),
        "feedback": _feedback(schema, composed_units, report, root_atoms),
        "affordances": _affordances(schema, composed_units, root_atoms, report),
        "controls": _controls(schema, composed_units, root_atoms),
        "dependencies": _dependencies(schema, report),
        "wiring_report": report,
    }
    return MechanicsPlaytestInput.from_dict({
        "ATOM_IR": root_atoms,
        "RESOLVER_OUTPUT": resolver_output,
        "WORLD_ATOMS": world_atoms,
        "CHARACTER_ATOMS": character_atoms,
    })


__all__ = ["build_mechanics_playtest_input"]
