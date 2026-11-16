"""Step 5 scene membership: every declared entity spawns in a scene, or is named.

A ``SceneSpec.entities`` list is a SUBSET. The scene hosts the groups it names
and nothing else. ``None`` hosts every group, and so does a scene that has no
spec at all. Three readers agree on this: the scene graph
(``src/scene_graph.py`` ``_scene_membership``), EntitySpawner's
``scene_entity_map`` (``src/unity_generator.py``) and the Step 7
``entity_scene_coverage`` check. :func:`hosting_scenes` is the same reading.

The Step 3 fill writes the scene lists before it finishes inventing entities.
On a many-scene brief it often leaves a group out of every list. Live, on the
neon_fugitive brief, 3 of the 4 runs that reached Step 5 died in the scene
graph that way. One of them was the player's own avatar, ``PlayerHoverbike``
(``kind='vehicle'``, ``tag='Player'``). The kind-only restore that used to
live in ``_reconcile_scene_specs`` never saw it, because it matched only
``kind in (player, weapon, enemy)``.

This module is the one owner of membership repair. It adds an entity to a
scene only where the schema already holds the answer, and it records each
placement with its evidence in ``schema.meta["scene_membership"]``. Tiers:

1. **Essential** — a gameplay scene cannot omit:
   - the player's own group (``pick_player_entity``, the signal the layout and
     the role-graph repair R1b already use; else the unique
     ``characters[role='player']`` whose ``model_ref`` names an entity that
     can be an avatar, as the Step 3 prompt specifies);
   - every ``kind`` player, weapon or enemy group.
   A gameplay scene is a non-menu scene whose spec names at least one group.
   A spec that lists nothing (``entities: []``) has declared itself empty, and
   nothing is added to it.
2. **Mission target** — an objective's ``target_ref`` belongs in the scene(s)
   its mission runs in (``schema.meta["mission_scenes"]``, from
   ``_infer_mission_scenes``).

An entity still in no scene after both tiers gets a named ABSTENTION. The
record lists the evidence each tier looked for and did not find. The pass
never adds such an entity to every scene, and never picks a scene for it.
The scene graph leaves out exactly these recorded entities, with a loud
warning each, instead of discarding the whole schema at Step 5. Any other
entity in no scene still fails the graph. The Step 7 ``entity_scene_coverage``
check stays an ERROR, so the build still fails, at Step 7 with the project
written (:func:`abstained_entity_names`). Deterministic and LLM-free (Step 5).
"""

from __future__ import annotations

from typing import Any, Optional

from .unity_layout_archetype import pick_player_entity
from .unity_schema import _NEVER_PLAYER_KINDS

SCENE_MEMBERSHIP_META_KEY = "scene_membership"
SCENE_MEMBERSHIP_FORMAT = "unity-scene-membership/v1"

TIER_PLAYER = "player"
TIER_ESSENTIAL_KIND = "essential_kind"
TIER_MISSION_TARGET = "mission_target"
# A later Step 5 pass with its own evidence: a visual-context run's closed
# image IR says the whole visible cast belongs in the image-led scene
# (unity_visual_bindings.ensure_visual_scene_coverage).
TIER_VISUAL_COVERAGE = "visual_coverage"

# Scene-name fragments that mark a non-gameplay screen. One reading for every
# Step 5 pass that asks "is this a gameplay scene": the mission-scene
# inference, the per-objective scene map and the membership tiers below.
MENU_SCENE_WORDS: tuple[str, ...] = (
    "menu", "title", "splash", "credits", "options",
    "settings", "lobby", "gameover", "game_over", "pause",
)

_ESSENTIAL_KINDS: tuple[str, ...] = ("player", "weapon", "enemy")
_HOSTILE_KINDS: frozenset[str] = frozenset({"enemy", "boss"})


def is_menu_scene(name: Optional[str]) -> bool:
    """True when the scene name marks a non-gameplay screen."""
    lowered = (name or "").lower()
    return any(word in lowered for word in MENU_SCENE_WORDS)


def hosting_scenes(schema: Any, entity_name: str) -> list[str]:
    """Declared scenes that spawn ``entity_name``, in ``schema.scenes`` order.

    This is the scene graph's own reading. A scene with no spec, or with
    ``entities=None``, hosts every group. Any other scene hosts only the
    groups it names.
    """
    scenes = [str(s) for s in (getattr(schema, "scenes", None) or []) if s]
    specs = {spec.name: spec for spec in (getattr(schema, "scene_specs", None) or [])
             if spec.name in scenes}
    hosts: list[str] = []
    for scene in scenes:
        spec = specs.get(scene)
        if spec is None or spec.entities is None or entity_name in spec.entities:
            hosts.append(scene)
    return hosts


def _gameplay_subset_specs(schema: Any) -> list[Any]:
    """Declared, non-menu specs whose subset names at least one group."""
    scenes = set(getattr(schema, "scenes", None) or [])
    return [spec for spec in (schema.scene_specs or [])
            if spec.name in scenes and spec.entities and not is_menu_scene(spec.name)]


def _not_an_avatar(entity: Any) -> str:
    """Why ``entity`` cannot be the player's body, or "" when it can be.

    The schema's own role-graph rules supply the answer, so nothing is
    guessed: a hostile group is never the player (R2), a never-player kind
    cannot hold the Player tag (``_reserve_player_tag``), and the avatar is
    one object (R1a).
    """
    kind = (entity.kind or "").lower()
    if kind in _HOSTILE_KINDS or (entity.tag or "") in ("Enemy", "Boss"):
        return f"it is hostile (kind={entity.kind!r}, tag={entity.tag!r})"
    if kind in _NEVER_PLAYER_KINDS:
        return f"kind={entity.kind!r} can never be the player"
    if (entity.count or 1) > 1:
        return f"it is a group of {entity.count}, and the avatar is one object"
    return ""


def resolve_player_entity(schema: Any) -> tuple[Optional[Any], str]:
    """The player's own entity group and the evidence that names it.

    1. ``pick_player_entity``: ``tag='Player'``, a Player-prefixed alias, or a
       Player name. The layout, R1b and the generator's player-merge read the
       same signal.
    2. Only when (1) finds no one: the unique ``characters[]`` entry with
       ``role='player'`` whose ``model_ref`` names a declared entity that can
       be an avatar. The Step 3 prompt asks for exactly that (``"<an entity
       group name or models[] id>"``), and Step 7's ``objective_targets``
       check follows the same indirection. Live on neon_fugitive, the player
       character named the ride (``Hoverbike``, ``HoverbikePlayerModel``,
       both ``tag='Vehicle'``). It also once named ``Crowd``, a group of 40
       NPCs, which :func:`_not_an_avatar` refuses. Two player characters
       naming different entities are ambiguous, and nothing is returned.

    With no player, the second element says why, or is "" when no player
    character names any entity at all.
    """
    entities = list(getattr(schema, "entities", None) or [])
    picked, why = pick_player_entity(entities)
    if picked is not None:
        return picked, f"pick_player_entity: {why}"
    by_key = {(e.name or "").strip().casefold(): e for e in entities if e.name}
    hits: list[tuple[int, Any, Any]] = []
    refused: list[str] = []
    for index, character in enumerate(getattr(schema, "characters", None) or []):
        if (character.role or "").strip().lower() != "player":
            continue
        entity = by_key.get((character.model_ref or "").strip().casefold())
        if entity is None:
            continue
        reason = _not_an_avatar(entity)
        if reason:
            refused.append(f"characters[{index}] role='player' model_ref="
                           f"{character.model_ref!r} names {entity.name!r}, "
                           f"but {reason}")
            continue
        hits.append((index, character, entity))
    named = {id(entity) for _, _, entity in hits}
    if len(named) > 1:
        return None, ("player characters name different entities: "
                      + ", ".join(sorted({entity.name for _, _, entity in hits})))
    if not named:
        return None, "; ".join(refused)
    index, character, entity = hits[0]
    return entity, (f"characters[{index}] role='player' "
                    f"model_ref={character.model_ref!r}")


def _essential_groups(schema: Any) -> list[tuple[Any, str, str]]:
    """``(entity, tier, evidence)`` for every group a gameplay scene must host,
    in schema order."""
    player, why = resolve_player_entity(schema)
    out: list[tuple[Any, str, str]] = []
    for entity in schema.entities:
        kind = (entity.kind or "").lower()
        if player is not None and entity is player:
            out.append((entity, TIER_PLAYER, f"player avatar ({why})"))
        elif kind in _ESSENTIAL_KINDS:
            out.append((entity, TIER_ESSENTIAL_KIND,
                        f"kind={kind!r} — a gameplay scene cannot omit "
                        "player/weapon/enemy groups"))
    return out


def _mission_scenes(schema: Any) -> dict[str, list[str]]:
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    raw = meta.get("mission_scenes") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for mission_id, scenes in raw.items():
        if isinstance(scenes, str):
            scenes = [scenes]
        if isinstance(scenes, list):
            out[str(mission_id)] = [str(s) for s in scenes if s]
    return out


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _abstention(schema: Any, entity: Any, *, player: Optional[Any],
                player_why: str,
                mission_scenes: dict[str, list[str]]) -> dict[str, Any]:
    """Why no tier could place ``entity``. One line per tier that looked."""
    missing: list[str] = []
    if player is None:
        missing.append("player: no entity carries a player signal "
                       "(tag='Player', a Player name or alias, or a "
                       "characters[role='player'].model_ref that names an "
                       "avatar)" + (f"; {player_why}" if player_why else ""))
    elif player is entity:
        missing.append("player: it IS the player, but no gameplay scene takes "
                       "added groups (every non-menu scene spec lists nothing)")
    else:
        missing.append(f"player: the player is {player.name!r}, not this "
                       "entity")
    kind = (entity.kind or "").lower()
    missing.append(f"essential kind: kind={kind!r} is not one of "
                   f"{', '.join(_ESSENTIAL_KINDS)}")
    targeting = [mission for mission in (schema.missions or [])
                 if any(o.target_ref == entity.name for o in (mission.objectives or []))]
    if not targeting:
        missing.append("mission target: no mission objective names it in "
                       "target_ref")
    else:
        for mission in targeting:
            scenes = mission_scenes.get(mission.id)
            if not scenes:
                missing.append(f"mission target: mission {mission.title!r} "
                               f"({mission.id}) targets it, but no scene was "
                               "inferred for that mission")
            else:
                missing.append(f"mission target: mission {mission.title!r} "
                               f"({mission.id}) runs in {scenes}, and none of "
                               "those scenes takes added groups (no spec, or "
                               "an explicitly empty list)")
    return {
        "entity": entity.name,
        "kind": entity.kind,
        "tag": entity.tag,
        "missing": missing,
        "remedy": ("name it in a scene_specs[].entities list, or remove it from "
                   "entities. Step 3 must give every entity a scene."),
    }


def abstention_message(row: dict[str, Any]) -> str:
    """The loud one-line form of one abstention record."""
    return (f"[scene.membership] ABSTAINED for {row['entity']!r} "
            f"(kind={row.get('kind')!r}, tag={row.get('tag')!r}): it is in no "
            "scene and the schema holds no evidence for one. "
            + "; ".join(row.get("missing") or [])
            + f". Remedy: {row.get('remedy')}")


def _abstention_rows(schema: Any) -> list[dict[str, Any]]:
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    record = meta.get(SCENE_MEMBERSHIP_META_KEY) or {}
    rows = record.get("abstentions") if isinstance(record, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict)]


def scene_membership_warnings(schema: Any) -> list[str]:
    """Loud messages for every entity the last membership pass abstained on."""
    return [abstention_message(row) for row in _abstention_rows(schema)]


def refresh_scene_membership(schema: Any, *, tier: str, evidence: str) -> list[str]:
    """Clear abstentions that a pass after :func:`assign_scene_membership`
    resolved, and record that pass as the placement's evidence.

    A visual-context run calls ``ensure_visual_scene_coverage`` after the
    membership pass. An entity it places would otherwise stay recorded as
    abstained: a WARN line and a meta row saying it spawns nowhere, while the
    final schema, the scene graph and Step 7 all see it in a scene. Only
    entities that now have a scene are touched. The rest stay abstained, by
    name. Returns one fix message per cleared abstention.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else None
    record = meta.get(SCENE_MEMBERSHIP_META_KEY) if meta is not None else None
    if not isinstance(record, dict):
        return []
    rows = [row for row in (record.get("abstentions") or []) if isinstance(row, dict)]
    if not rows:
        return []
    scenes = set(getattr(schema, "scenes", None) or [])
    placements = [dict(row) for row in (record.get("placements") or [])
                  if isinstance(row, dict)]
    still: list[dict[str, Any]] = []
    fixes: list[str] = []
    for row in rows:
        name = str(row.get("entity") or "")
        if not name or not hosting_scenes(schema, name):
            still.append(row)
            continue
        listed = [spec.name for spec in (schema.scene_specs or [])
                  if spec.name in scenes and name in (spec.entities or [])]
        for scene in listed:
            placements.append({"entity": name, "scene": scene,
                               "tier": tier, "evidence": evidence})
        fixes.append(f"[scene.membership] {name!r} is no longer abstained: "
                     f"{evidence} placed it in "
                     f"{listed or 'a scene that hosts every group'}")
    record["abstentions"] = still
    record["placements"] = placements
    return fixes


def abstained_entity_names(schema: Any) -> set[str]:
    """Entity names the last membership pass recorded as abstained.

    The scene graph omits an entity in no scene only when its name is in this
    set. An orphan nobody explained is still a hard ``[graph.strict]``
    failure.
    """
    return {str(row["entity"]) for row in _abstention_rows(schema)
            if row.get("entity")}


def assign_scene_membership(schema: Any) -> list[str]:
    """Apply the evidence tiers in place. Return one fix message per change.

    Writes ``schema.meta["scene_membership"]``: ``placements`` (one row per
    entity added to a scene, with tier and evidence) and ``abstentions`` (one
    row per entity left in no scene, with what each tier was missing).
    Idempotent. A placement from an earlier run is kept while the entity is
    still in that scene, so replaying a saved schema keeps its audit trail.
    """
    fixes: list[str] = []
    placements: list[dict[str, Any]] = []

    def _place(spec: Any, rows: list[tuple[str, str, str]]) -> None:
        spec.entities = list(spec.entities or []) + [name for name, _, _ in rows]
        for name, tier, evidence in rows:
            placements.append({"entity": name, "scene": spec.name,
                               "tier": tier, "evidence": evidence})

    # Tier 1 — the player, then the player/weapon/enemy kinds, in every
    # gameplay scene. The fill routinely drops them: on VoidArena, "Main"
    # listed the player but not the gun, so nothing could mount a weapon.
    # The rule is deliberately NOT gated on the player already being listed.
    # An earlier version was, and it did nothing for exactly the scenes that
    # needed it. Menus are matched by name, because the name is the only
    # signal a spec carries about its purpose. A main menu that only listed a
    # UI overlay group once got the player and 76 enemies behind its title.
    # The player is found by its signal, not its kind: neon_fugitive's avatar
    # was a kind='vehicle' hoverbike, which the old kind-only rule never saw.
    essentials = _essential_groups(schema)
    if essentials:
        for spec in _gameplay_subset_specs(schema):
            missing = [(e.name, tier, why) for e, tier, why in essentials
                       if e.name not in spec.entities]
            if not missing:
                continue
            _place(spec, missing)
            fixes.append(f"Scene '{spec.name}': restored player/weapon/enemy "
                         f"group(s) {[name for name, _, _ in missing]} — a "
                         "gameplay scene cannot omit them")

    # Tier 2 — an objective's target belongs where its mission runs. If it is
    # missing there, no wiring of SEAM 8 (unity_compose.py) can complete the
    # objective. Live on GearCitadel, HallOne omitted GearGate and
    # SaveCogShrine, the two targets of Hall One Clearance's objectives.
    mission_scenes = _mission_scenes(schema)
    entity_names = {e.name for e in schema.entities}
    if mission_scenes:
        for spec in schema.scene_specs or []:
            if not spec.entities:
                continue
            for mission in schema.missions or []:
                if spec.name not in (mission_scenes.get(mission.id) or ()):
                    continue
                targets = _dedupe([o.target_ref for o in (mission.objectives or [])
                                   if o.target_ref and o.target_ref in entity_names])
                missing_targets = [n for n in targets if n not in spec.entities]
                if not missing_targets:
                    continue
                _place(spec, [(n, TIER_MISSION_TARGET,
                               f"objective target_ref of mission "
                               f"{mission.title!r} ({mission.id}); "
                               f"meta.mission_scenes[{mission.id!r}]="
                               f"{mission_scenes.get(mission.id)}")
                              for n in missing_targets])
                fixes.append(
                    f"Scene '{spec.name}': restored objective target entity "
                    f"group(s) {missing_targets} for mission '{mission.title}' "
                    "— an objective's target must be reachable where its "
                    "mission runs")

    # No evidence: name the entity and what was missing. Never place it. A
    # schema that declares no scene at all fails earlier and louder in the
    # scene graph ("declares no scenes"). That is not a membership question.
    player, player_why = resolve_player_entity(schema)
    abstentions = [
        _abstention(schema, entity, player=player, player_why=player_why,
                    mission_scenes=mission_scenes)
        for entity in schema.entities
        if entity.name and schema.scenes and not hosting_scenes(schema, entity.name)
    ]

    meta = schema.meta if isinstance(schema.meta, dict) else None
    if meta is not None:
        previous = meta.get(SCENE_MEMBERSHIP_META_KEY) or {}
        kept: list[dict[str, Any]] = []
        if isinstance(previous, dict):
            specs = {spec.name: spec for spec in (schema.scene_specs or [])}
            for row in previous.get("placements") or []:
                if not isinstance(row, dict):
                    continue
                spec = specs.get(row.get("scene"))
                if spec is not None and row.get("entity") in (spec.entities or []):
                    kept.append(dict(row))
        seen = {(row["entity"], row["scene"]) for row in kept}
        merged = kept + [row for row in placements
                         if (row["entity"], row["scene"]) not in seen]
        meta[SCENE_MEMBERSHIP_META_KEY] = {
            "format": SCENE_MEMBERSHIP_FORMAT,
            "stage": "5_scene_membership",
            "placements": merged,
            "abstentions": abstentions,
        }
    return fixes


__all__ = [
    "MENU_SCENE_WORDS",
    "SCENE_MEMBERSHIP_FORMAT",
    "SCENE_MEMBERSHIP_META_KEY",
    "TIER_ESSENTIAL_KIND",
    "TIER_MISSION_TARGET",
    "TIER_PLAYER",
    "TIER_VISUAL_COVERAGE",
    "abstained_entity_names",
    "abstention_message",
    "assign_scene_membership",
    "hosting_scenes",
    "is_menu_scene",
    "resolve_player_entity",
    "scene_membership_warnings",
]
