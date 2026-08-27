"""Escort objective -> Follow atom.

`escort`-type mission objectives had no honest event source at all (SEAM 8,
src/unity_compose.py): reach binds to a trigger, collect/interact to an
interactable, eliminate to a spawner despawn, survive to the tracker's own
clock -- escort had nothing, because nothing in a composed project ever made
the escorted NPC actually GO anywhere. Wiring a "reach" trigger straight to
an escort objective would be dishonest: the player could reach the
destination alone and the objective would complete without any escorting
having happened.

This pass gives the escorted entity a real reason to be there. `src/atoms/
movement.py`'s `Follow` atom ("move toward a named entity each frame,
maintaining a trailing distance", alias "escort") already exists for exactly
this; `EntitySpec.atoms` + `EntitySpec.behavior` already wire an atom
sequence into a generated `<Name>Behavior` MonoBehaviour that EntitySpawner
attaches at runtime (src/unity_generator.py's entity-behavior reconciliation
and generation passes) -- this pass is the one missing piece that actually
POINTS an escort objective's target entity at that existing machinery.
SEAM 8's `WireTriggerToEscortObjective` (src/atoms/backends/unity.py) is then
the honest signal: the SAME trigger a `reach` objective would use, gated on
this entity's own live position being near it.

Runs in Step 5, after `resolve_objective_targets` (unity_objective_targets.py)
has already filled every objective's `target_ref` -- this pass needs that
resolved before it can find the entity being escorted.
"""

from __future__ import annotations

from .atoms.base import AtomInvocation
from .unity_generator import entity_behavior_class_name


def resolve_escort_follow(schema) -> list[str]:
    """Fill-only: an escort objective's target_ref entity gets a Follow atom
    and a matching `.behavior` class name, unless the author already gave it
    one -- an explicit choice always wins, same contract as every other
    deterministic backstop in this repo (locomotion_modes, flying_abilities,
    Objectives.after, ...).

    Restricted to `count == 1` entities: SEAM 8's proximity check needs one
    unambiguous spawned instance name ("<name>_0") to query at runtime, and a
    multi-instance group has no single "the" NPC being escorted. Reported,
    never silently skipped, so a genuinely multi-instance escort target is
    named as a real gap instead of quietly staying unwired.
    """
    msgs: list[str] = []
    if not schema.missions or not schema.entities:
        return msgs
    by_name = {e.name: e for e in schema.entities if e.name}
    for mission in schema.missions:
        for obj in mission.objectives:
            if obj.type != "escort" or not obj.target_ref:
                continue
            entity = by_name.get(obj.target_ref)
            if entity is None:
                continue  # unresolvable target_ref is resolve_objective_targets's own gap to name
            if entity.atoms or entity.behavior:
                continue  # fill-only -- an author's own choice always wins
            if entity.count != 1:
                msgs.append(
                    f"escort: {entity.name} has count={entity.count} — a Follow "
                    f"behavior needs one unambiguous NPC to escort, so "
                    f"{obj.id} stays unwired")
                continue
            entity.atoms = [AtomInvocation(
                atom="Follow",
                label=f"Escort: follow the player toward {obj.id}'s destination",
                args={"target": "Player", "distance": 2.0, "speed": 4.0},
            )]
            entity.behavior = entity_behavior_class_name(entity.name)
            msgs.append(
                f"escort: {entity.name} now follows the player (Follow atom) "
                f"so {obj.id} has a real NPC to detect at the destination")
    return msgs
