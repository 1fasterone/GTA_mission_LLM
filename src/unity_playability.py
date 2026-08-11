"""
Playability evaluator (pipeline Step 7) — the compiler grading its own output.

Every other stage of this pipeline answers "did it compile": the schema
validates, the packages resolve, the C# parses, the FBX imports. Nothing
answered "is it a game". That gap is why playability regressions were only
ever found by a human opening the project in Unity and looking: the ToyRacer
run that graded 0/10 (every AI opponent and pickup dropped by the schema-fill
LLM) and the DropZone run that lost four of five explicitly-named entities
both PASSED every automated check the compiler had, because a project with no
opponents, no pickups and no objectives is still a structurally valid Unity
project.

This module is the missing check. It is deterministic Python over the final
schema — no LLM, no Unity, no runtime — and it is deliberately structural:
every finding names a concrete thing the player cannot do, traced to the field
that causes it, so a failure points at a pipeline stage rather than at a
feeling. It measures; it never mutates the schema.

Severities:
  ERROR   the output is not a playable game — something a player fundamentally
          cannot do (no avatar, no objective, unreachable content).
  WARN    playable but degraded — content the brief asked for that thinned out.
  INFO    observations worth seeing in the scorecard, never a failure.

**A check that cannot be graded says so.** The first version of this module let
checks `return` when the evidence they need was absent, which shrank the score's
denominator silently: the DropZone arcade capture printed "96/100 (11/12
checks) PLAYABLE" while four ERROR-class checks had never run at all, and a
schema graded through the CLI (no `meta.user_prompt`, no baked positions) got
three of them free. Missing evidence is now recorded in `report.not_graded`,
printed on the scorecard, and — for ERROR-class checks — fails
`gate(require_graded=True)`. Absence of evidence is never evidence of
playability.

Scope: this grades the SCHEMA. `src/unity_playability_artifact.py` grades the
files the generator actually emitted, which is where a defect the schema cannot
express lives (an avatar the layout placed at 2000 m that the scene template
spawns at the origin). The eval harness runs both and merges the findings.

`evaluate()` returns a PlayabilityReport; `gate()` turns one into a
pass/fail decision. The pipeline prints the scorecard on every run and only
hard-fails when asked to, so this can be adopted without breaking existing
projects that have known warnings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .unity_concept_entities import extract_named_entities
from .unity_layout import _reach_distance, terrain_bounds
from .unity_schema import EntitySpec, UnitySchema
from .unity_layout_archetype import is_climbable_entity, pick_player_entity
from .unity_spatial import (GROUND_PLANE, ROUTE_CORRIDOR, VOLUME_PRIMITIVES,
                            WALL_SURFACE, is_grounded, select_primitive)

ERROR = "error"
WARN = "warn"
INFO = "info"

_SEVERITY_ORDER = {ERROR: 0, WARN: 1, INFO: 2}

# Weight each check contributes to the 0-100 score. ERROR-class checks are
# worth more because they are the difference between "a game" and "a scene".
_WEIGHT = {ERROR: 3.0, WARN: 1.0, INFO: 0.0}

# The severity a check emits when it fails, declared up front so the score is
# a plain weighted pass rate rather than something inferred from whichever
# findings happen to be present.
_CHECK_SEVERITY: dict[str, str] = {
    "avatar":            ERROR,
    "avatar_binding":    WARN,
    "avatar_cardinality": ERROR,
    "controllable":      ERROR,
    "objective":         ERROR,
    "objective_targets": ERROR,
    "brief_entities":    ERROR,
    "spawn_clear":       ERROR,
    "content_reachable": ERROR,
    "volume_player":     ERROR,
    "camera":            WARN,
    "interactables":     WARN,
    "scene_population":  WARN,
    "unbaked_gameplay":  WARN,
    "audio":             WARN,
    "enemy_wiring":      WARN,
    "role_graph":        ERROR,
    "objective_kinematics": ERROR,
    # Graded on the emitted files by src/unity_playability_artifact.py, but
    # weighted here so one table decides what a finding costs.
    "avatar_singleton":  ERROR,
    "avatar_spawn":      ERROR,
    "objectives_spawned": ERROR,
    "avatar_not_duplicated": WARN,
}

# Kinds that are scenery: present in a scene without being something the
# player does anything with. A scene made only of these is a diorama.
_SCENERY_KINDS: frozenset[str] = frozenset({
    "prop", "building", "tree", "rock", "decor", "wall", "crowd", "scenery",
    "furniture", "terrain", "ground", "road", "track", "street", "rail",
    "water", "fx", "floor",
})

# Script types that make the player's avatar move under input. Without one of
# these (or a composed controller micro) the avatar is a statue.
_CONTROLLER_SCRIPT_TYPES: frozenset[str] = frozenset({"player"})
_CAMERA_SCRIPT_TYPES: frozenset[str] = frozenset({"camera"})

# Kinds that are surfaces to stand ON, so finding one at the avatar's own spawn
# is the floor, not a trap. `is_grounded` covers the y=0 floor of the volume;
# these are the ones that legitimately share the spawn cell at altitude — a
# VerticalCorridor deliberately spawns the player attached to the exit platform
# (src/unity_layout.py), which read as "the player begins trapped inside a
# collider" the moment the check started measuring against the real spawn.
_STANDABLE_KINDS: frozenset[str] = frozenset({
    "platform", "structure", "floor", "terrain", "bridge", "deck", "roof",
    "stairs", "ramp", "elevator", "vehicle", "boat", "aircraft",
})

# An objective is anything that can end or score a run. Collectibles and
# enemies count: "pick these up" and "defeat these" are objectives even when
# the schema never writes a MissionSpec for them.
_OBJECTIVE_KINDS: frozenset[str] = frozenset({
    "collectible", "pickup", "item", "enemy", "boss", "goal", "objective",
})

# Real schema-fill output routinely leaves `kind` generic and puts the meaning
# in `tag` — the DropZone arcade capture scores its neon gates `kind="mechanism"
# tag="Gate"` and its landing pad `kind="platform" tag="Target"`. Reading `kind`
# alone made every objective-shaped check vacuous on exactly the builds this
# module exists to grade, so identity is tag-aware too (and mission-aware; see
# `_objective_names`).
_OBJECTIVE_TAGS: frozenset[str] = frozenset({
    "collectible", "pickup", "item", "gate", "target", "goal", "objective",
    "enemy", "boss", "checkpoint",
})


class PlayabilityGateError(RuntimeError):
    """Raised when a build fails the playability gate — same shape as
    CSharpSyntaxError / PackageCacheError, so callers can catch it precisely."""


@dataclass(frozen=True)
class Finding:
    """One structural defect, named by the thing the player cannot do."""
    check: str
    severity: str
    summary: str
    # The schema field / pipeline stage responsible — so a failure points
    # somewhere rather than just scoring low.
    source: str = ""

    def __str__(self) -> str:
        # Parenthesised, not bracketed: the scorecard is printed through rich,
        # which reads "[...]" as console markup and eats the source pointer.
        tail = f"  ({self.source})" if self.source else ""
        return f"{self.severity.upper():5} {self.check}: {self.summary}{tail}"


@dataclass
class PlayabilityReport:
    project_name: str = ""
    primitive: str = ""
    findings: list[Finding] = field(default_factory=list)
    # check name -> True when it passed, for the scorecard's "n pass, m fail"
    checks: dict[str, bool] = field(default_factory=dict)
    # check name -> why it could not be graded. Deliberately separate from
    # `checks`: an ungraded check is neither a pass nor a failure, and folding
    # it into either one is how the first version of this module reported
    # "96/100 PLAYABLE" for a build whose avatar was 2000 m below its own game.
    not_graded: dict[str, str] = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARN]

    @property
    def ungraded_errors(self) -> dict[str, str]:
        """ERROR-class checks that never ran. An eval that scores a build on
        these is scoring paperwork it did not read."""
        return {name: why for name, why in self.not_graded.items()
                if _CHECK_SEVERITY.get(name, WARN) == ERROR}

    @property
    def score(self) -> int:
        """0-100: the weighted share of GRADED checks that passed. Weighted by
        severity, so losing the player's avatar costs three times what a thin
        audio bed does. 100 means every check that could be graded passed —
        read it next to `not_graded`, never alone."""
        total = earned = 0.0
        for name, passed in self.checks.items():
            w = _WEIGHT[_CHECK_SEVERITY.get(name, WARN)]
            total += w
            if passed:
                earned += w
        return int(round(100.0 * earned / total)) if total else 100

    @property
    def playable(self) -> bool:
        """No ERROR finding — the output is a game, whatever its polish."""
        return not self.errors

    def render(self) -> str:
        """Plain-text scorecard. Ordered worst-first so the top line is the
        thing to fix."""
        n_pass = sum(1 for ok in self.checks.values() if ok)
        n_fail = len(self.checks) - n_pass
        tally = f"{n_pass} pass, {n_fail} fail"
        if self.not_graded:
            tally += f", {len(self.not_graded)} not graded"
        head = (f"PLAYABILITY {self.score}/100  ({tally})  "
                f"space={self.primitive}  "
                f"{'PLAYABLE' if self.playable else 'NOT PLAYABLE'}")
        lines = [head]
        for f in sorted(self.findings,
                        key=lambda f: (_SEVERITY_ORDER[f.severity], f.check)):
            lines.append(f"  {f}")
        for name, why in sorted(self.not_graded.items()):
            lines.append(f"  {'NOGRD':5} {name}: not graded — {why}")
        return "\n".join(lines) if len(lines) > 1 else head + "\n  no findings"


def _ungraded(rep: PlayabilityReport, check: str, why: str) -> None:
    """Record that a check had no evidence to work with. Never writes
    `rep.checks` — an ungraded check must not inflate the score in either
    direction — but it always reaches the scorecard."""
    rep.not_graded[check] = why


# ── individual checks ─────────────────────────────────────────────────────────
# Each appends its findings and records pass/fail under its own name.

def _controllers(schema: UnitySchema) -> set[str]:
    return {(s.type or "").lower() for s in (schema.scripts or [])}


def _objective_refs(schema: UnitySchema) -> dict[str, str]:
    """Entity names the missions point at, lowercased key -> as authored. A
    mission objective naming an entity IS that entity's job description,
    whatever generic `kind` the fill gave it."""
    return {(o.target_ref or "").strip().lower(): (o.target_ref or "").strip()
            for m in (schema.missions or [])
            for o in (m.objectives or []) if (o.target_ref or "").strip()}


def _objective_names(schema: UnitySchema) -> set[str]:
    """The lowercased target_ref keys — matching form for `_is_objective`."""
    return set(_objective_refs(schema))


def _is_objective(entity: EntitySpec, target_refs: frozenset[str]) -> bool:
    """Does the player do something with this group? `kind` is a bare str with
    no lowercasing validator, so every read of it defends itself."""
    return ((entity.kind or "").lower() in _OBJECTIVE_KINDS
            or (entity.tag or "").lower() in _OBJECTIVE_TAGS
            or entity.name.strip().lower() in target_refs)


def _check_avatar(schema: UnitySchema, rep: PlayabilityReport) -> None:
    """Unity's FindGameObjectWithTag("Player") returns ONE arbitrary match, so
    zero avatars breaks every player-facing lookup and two makes which one wins
    nondeterministic (see EntitySpec._reserve_player_tag)."""
    tagged = [e for e in schema.entities if e.tag == "Player"]
    ok = True
    if not tagged and not (_CONTROLLER_SCRIPT_TYPES & _controllers(schema)):
        rep.findings.append(Finding(
            "avatar", ERROR,
            "no Player-tagged entity and no player script — nothing for the "
            "camera to follow, nothing for enemies to chase, nobody to play as",
            "schema.entities.tag / schema.scripts.type"))
        ok = False
    elif len(tagged) > 1:
        names = ", ".join(e.name for e in tagged)
        rep.findings.append(Finding(
            "avatar", ERROR,
            f"{len(tagged)} entities carry tag=Player ({names}) — Unity binds "
            "camera, weapon mount and AI targeting to one arbitrary match",
            "schema.entities.tag"))
        ok = False
    rep.checks["avatar"] = ok


def _check_avatar_binding(schema: UnitySchema, rep: PlayabilityReport) -> None:
    """A player script with no Player-tagged ENTITY is not the same as having an
    avatar. The scene template always builds its own Player object, so the game
    is controllable — but nothing binds the design's avatar to it: the
    generator's player-merge (unity_generator.py) requires a Player-tagged,
    count-1 entity to transfer the resolved model AND the layout's baked spawn.
    Without it the avatar is a bare capsule at the world origin, and the group
    the design meant as the player spawns separately as scenery.

    The usual cause is EntitySpec._reserve_player_tag demoting a Player tag the
    fill put on a `kind="prop"` group — correct as a rule, terminal in effect,
    because only unity_concept_entities' rescue ever re-assigns the tag and it
    skips names that already exist.
    """
    if any(e.tag == "Player" for e in schema.entities):
        rep.checks["avatar_binding"] = True
        return
    if not (_CONTROLLER_SCRIPT_TYPES & _controllers(schema)):
        _ungraded(rep, "avatar_binding",
                  "no avatar of any kind — see the `avatar` finding")
        return
    rep.findings.append(Finding(
        "avatar_binding", WARN,
        "no entity carries tag=Player, so the scene template's placeholder "
        "capsule is the avatar — it takes no resolved model and no layout "
        "spawn, and the group the design meant as the player spawns beside it "
        "as scenery",
        "EntitySpec._reserve_player_tag / unity_generator player-merge"))
    rep.checks["avatar_binding"] = False


def _check_avatar_cardinality(schema: UnitySchema,
                              rep: PlayabilityReport) -> None:
    """The avatar is singular by definition. A Player-tagged group with count>1
    spawns N tagged objects (arbitrary FindWithTag match) AND silently disables
    the generator's player-merge, which requires count==1 — so the avatar loses
    its model and its baked spawn at the same time."""
    tagged = [e for e in schema.entities if e.tag == "Player"]
    if not tagged:
        _ungraded(rep, "avatar_cardinality", "no Player-tagged entity to count")
        return
    plural = [e for e in tagged
              if e.count > 1 or len(e.instance_positions or []) > 1]
    if plural:
        detail = ", ".join(
            f"{e.name} (count={e.count}, "
            f"{len(e.instance_positions or [])} baked)" for e in plural[:4])
        rep.findings.append(Finding(
            "avatar_cardinality", ERROR,
            f"the avatar is not singular: {detail} — every instance carries "
            "tag=Player, so player lookups pick one arbitrarily and the "
            "player-merge (which needs count==1) never runs",
            "schema.entities.count on the Player-tagged group"))
    rep.checks["avatar_cardinality"] = not plural


def _check_controllable(schema: UnitySchema, rep: PlayabilityReport) -> None:
    ok = bool(_CONTROLLER_SCRIPT_TYPES & _controllers(schema)) or bool(
        schema.capabilities and schema.capabilities.controller)
    if not ok:
        rep.findings.append(Finding(
            "controllable", ERROR,
            "no player controller — the avatar does not respond to input",
            "schema.scripts.type == player"))
    rep.checks["controllable"] = ok


def _check_camera(schema: UnitySchema, rep: PlayabilityReport) -> None:
    ok = bool(_CAMERA_SCRIPT_TYPES & _controllers(schema)) or \
        "cinemachine" in (schema.features or [])
    if not ok:
        rep.findings.append(Finding(
            "camera", WARN,
            "no camera script or cinemachine feature — the view stays wherever "
            "the scene template left it",
            "schema.scripts.type == camera"))
    rep.checks["camera"] = ok


def _check_objective(schema: UnitySchema, rep: PlayabilityReport) -> None:
    """Something that can end or score a run. Without one the project is a
    walking simulator that never resolves — the ToyRacer 0/10 signature."""
    refs = frozenset(_objective_names(schema))
    has_content_objective = any(_is_objective(e, refs) for e in schema.entities)
    ok = bool(has_content_objective or schema.missions or schema.story_beats)
    if not ok:
        rep.findings.append(Finding(
            "objective", ERROR,
            "nothing to do — no collectibles, no enemies, no missions and no "
            "story beats, so the run has no goal and cannot be won or lost",
            "schema.entities.kind / schema.missions / schema.story_beats"))
    rep.checks["objective"] = ok


def _check_objective_targets(schema: UnitySchema,
                             rep: PlayabilityReport) -> None:
    """Every mission objective names the entity it is about. A `target_ref` with
    no matching entity is an objective the player can never complete — the
    schema-fill dropped the content but kept the mission that needs it."""
    refs = _objective_refs(schema)
    if not refs:
        _ungraded(rep, "objective_targets",
                  "no mission objective declares a target_ref")
        return
    have = {e.name.strip().lower() for e in schema.entities}
    dangling = sorted(authored for key, authored in refs.items()
                      if key not in have)
    if dangling:
        rep.findings.append(Finding(
            "objective_targets", ERROR,
            f"{len(dangling)} mission objective(s) target content that is not "
            f"in the build ({', '.join(dangling[:6])}) — the goal cannot be "
            "completed because the thing it names was never spawned",
            "schema.missions[].objectives[].target_ref"))
    rep.checks["objective_targets"] = not dangling


def _check_brief_entity_coverage(schema: UnitySchema,
                                 rep: PlayabilityReport) -> None:
    """Every entity the BRIEF named explicitly should exist in the final
    schema. This is the check that would have caught both reported failures
    at build time instead of in the editor."""
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    prompt = str(meta.get("user_prompt", "") or "")
    if not prompt.strip():
        # A replayed / saved schema carries no prompt, so there is nothing to
        # compare the build against. Saying so is the whole point: this check
        # used to return True here, handing a full-weight ERROR-class pass to
        # every schema graded through the CLI.
        _ungraded(rep, "brief_entities",
                  "schema.meta.user_prompt is empty — nothing to compare the "
                  "build against (a replay grades the schema, not the brief)")
        return
    named = extract_named_entities(prompt)
    if not named:
        _ungraded(rep, "brief_entities",
                  "the brief names no entities explicitly (a vibe prompt)")
        return
    have = {e.name.strip().lower() for e in schema.entities}
    missing = [n["name"] for n in named if n["name"].strip().lower() not in have]
    if missing:
        rep.findings.append(Finding(
            "brief_entities", ERROR,
            f"{len(missing)}/{len(named)} entities the brief named explicitly "
            f"are absent from the build ({', '.join(missing[:6])}) — content "
            "the design asked for is simply not in the game",
            "Step 3 schema-fill / unity_concept_entities rescue"))
    rep.checks["brief_entities"] = not missing


def _check_interactables_tagged(schema: UnitySchema,
                                rep: PlayabilityReport) -> None:
    """EntitySpawner only attaches a collider to a group that carries a tag
    (templates/unity/EntitySpawner.cs.j2), so an untagged pickup is scenery
    the player walks through."""
    refs = frozenset(_objective_names(schema))
    untagged = [e.name for e in schema.entities
                if _is_objective(e, refs)
                and (not e.tag or e.tag == "Untagged")]
    if untagged:
        rep.findings.append(Finding(
            "interactables", WARN,
            f"{len(untagged)} objective group(s) carry no tag "
            f"({', '.join(untagged[:6])}) — the spawner gives them no collider, "
            "so they cannot be picked up or hit",
            "schema.entities.tag"))
    rep.checks["interactables"] = not untagged


def _check_scene_population(schema: UnitySchema,
                            rep: PlayabilityReport) -> None:
    """A scene whose entity subset is all scenery is a diorama the player
    walks through — the SceneSpec subset filter is an easy way to accidentally
    empty a gameplay scene (it silently filtered out every rescued entity on
    the Cromulon run)."""
    by_name = {e.name: e for e in schema.entities}
    subsets = [s for s in (schema.scene_specs or []) if s.entities]
    if not subsets:
        _ungraded(rep, "scene_population",
                  "no scene declares an entity subset — every scene hosts "
                  "everything")
        return
    empty: list[str] = []
    for spec in subsets:
        hosted = [by_name[n] for n in (spec.entities or []) if n in by_name]
        if not any((e.kind or "").lower() not in _SCENERY_KINDS for e in hosted):
            empty.append(spec.name)
    if empty:
        rep.findings.append(Finding(
            "scene_population", WARN,
            f"scene(s) {', '.join(empty)} host only scenery — nothing to do "
            "once the player arrives",
            "schema.scene_specs.entities"))
    rep.checks["scene_population"] = not empty


def _instances(e: EntitySpec) -> list[list[float]]:
    return e.instance_positions or []


def _baked(schema: UnitySchema) -> bool:
    return any(_instances(e) for e in schema.entities)


def _spawn_point(schema: UnitySchema) -> tuple[float, float, float]:
    """Where the avatar actually starts, in full. The player's own group when
    the layout baked one — NOT the world origin: a climbing wall stands the
    player at z=4 facing holds at z=0.2, and measuring those holds against
    (0,0) reported the whole hold chain as trapping the player at t=0. Falls
    back to the origin, which is where the scene template's own Player object
    spawns when nothing transfers a baked position to it."""
    group, _signal = pick_player_entity(schema.entities)
    if group is not None and (group.instance_positions or []):
        p = group.instance_positions[0]
        return p[0], p[1], p[2]
    if group is not None and abs(group.elevation) > 1e-6:
        return 0.0, group.elevation, 0.0
    return 0.0, 0.0, 0.0


def _check_spawn_clear(schema: UnitySchema, rep: PlayabilityReport) -> None:
    """A group whose instance sits on the avatar's own start cell traps the
    player inside a collider at t=0."""
    if not _baked(schema):
        _ungraded(rep, "spawn_clear",
                  "no baked instance_positions — compute_layout has not run, "
                  "so there are no spawns to check")
        return
    sx, sy, sz = _spawn_point(schema)
    player_names = {e.name for e in schema.entities if e.tag == "Player"}
    group, _sig = pick_player_entity(schema.entities)
    if group is not None:
        player_names.add(group.name)
    blockers: list[str] = []
    for e in schema.entities:
        if (e.name in player_names or is_grounded(e)
                or (e.kind or "").lower() in _STANDABLE_KINDS):
            continue
        for p in _instances(e):
            if math.hypot(p[0] - sx, p[2] - sz) < 1.5 and abs(p[1] - sy) < 2.0:
                blockers.append(e.name)
                break
    if blockers:
        rep.findings.append(Finding(
            "spawn_clear", ERROR,
            f"{', '.join(sorted(set(blockers))[:6])} spawn(s) on the player's "
            f"own start cell (x={sx:.1f}, y={sy:.1f}, z={sz:.1f}) — the player "
            "begins trapped inside a collider",
            "src/unity_layout.py packing"))
    rep.checks["spawn_clear"] = not blockers


def _check_content_reachable(schema: UnitySchema,
                             rep: PlayabilityReport) -> None:
    """Every group has to sit inside the ground the scene actually builds,
    otherwise it hangs over the void where the player cannot follow."""
    if not _baked(schema):
        _ungraded(rep, "content_reachable",
                  "no baked instance_positions — compute_layout has not run, "
                  "so no group has a place to be out of bounds of")
        return
    xmin, zmin, xmax, zmax = terrain_bounds(schema.entities, schema.locomotion)
    stranded: list[str] = []
    for e in schema.entities:
        for p in _instances(e):
            if not (xmin <= p[0] <= xmax and zmin <= p[2] <= zmax):
                stranded.append(e.name)
                break
    if stranded:
        rep.findings.append(Finding(
            "content_reachable", ERROR,
            f"{', '.join(sorted(set(stranded))[:6])} sit outside the scene's "
            "ground bounds — over the void, where the player cannot follow",
            "src/unity_layout.terrain_bounds"))
    rep.checks["content_reachable"] = not stranded


def _check_unbaked_gameplay(schema: UnitySchema,
                            rep: PlayabilityReport) -> None:
    """A gameplay group with no baked positions is placed by the runtime
    spawner's own scatter (EntitySpawner.PlaceAt), which knows nothing about
    the vertical layout the compiler chose — so it lands on the y=0 plane while
    the game happens at altitude. It is also invisible to every reachability
    check in this file, all of which read baked positions.

    Only meaningful once layout has run, and only off the flat plane, where
    "wherever the spawner puts it" is a defensible answer.
    """
    if rep.primitive == GROUND_PLANE:
        rep.checks["unbaked_gameplay"] = True
        return
    if not _baked(schema):
        _ungraded(rep, "unbaked_gameplay",
                  "no baked instance_positions anywhere — compute_layout has "
                  "not run")
        return
    refs = frozenset(_objective_names(schema))
    # placement == "path" WITH points is legitimately unbaked: the spawner lays
    # the group along the polyline, which carries its own altitude (the arcade
    # capture's rivals fall down a path at y=2000). Without points there is no
    # polyline, so it falls through to the same y=0 scatter as everything else.
    drifting = [e.name for e in schema.entities
                if e.count > 0 and not _instances(e)
                and not (e.placement == "path" and e.path_points)
                and (_is_objective(e, refs)
                     or (e.kind or "").lower() not in _SCENERY_KINDS)]
    if drifting:
        rep.findings.append(Finding(
            "unbaked_gameplay", WARN,
            f"{len(drifting)} gameplay group(s) carry no baked positions "
            f"({', '.join(sorted(drifting)[:6])}) — the runtime spawner "
            f"scatters them on the y=0 plane, not into the {rep.primitive} the "
            "rest of the game was laid out in",
            "src/unity_layout.py packing (group skipped)"))
    rep.checks["unbaked_gameplay"] = not drifting


def _check_volume_player(schema: UnitySchema, rep: PlayabilityReport) -> None:
    """A volume game whose avatar is left on the floor while all its content
    is at altitude: the player starts at the bottom of a shaft looking up at
    a game they are not in. Only meaningful under a volume primitive."""
    if rep.primitive not in VOLUME_PRIMITIVES:
        _ungraded(rep, "volume_player",
                  f"space={rep.primitive or 'unknown'} is not a volume — an "
                  "avatar on the ground is where it belongs")
        return
    airborne = [e for e in schema.entities
                if not is_grounded(e) and abs(e.elevation) > 1e-6]
    if not airborne:
        _ungraded(rep, "volume_player",
                  "no group sits at altitude, so there is no volume for the "
                  "avatar to be missing from")
        return
    avatar = next((e for e in schema.entities if e.tag == "Player"), None)
    ok = True
    if avatar is None:
        # No tagged avatar means the scene template's own Player object is the
        # avatar, and that object spawns at the origin (y=1) unless the
        # player-merge transfers a baked spawn — which needs the tag. So the
        # avatar IS on the ground; this check used to pass here precisely
        # because its subject had gone missing.
        rep.findings.append(Finding(
            "volume_player", ERROR,
            f"the game is played across {len(airborne)} group(s) at altitude "
            "but no entity carries tag=Player, so the avatar is the scene "
            "template's capsule at the world origin — the player starts "
            "outside the volume the game happens in",
            "schema.entities.tag / unity_generator player_spawn"))
        ok = False
    elif abs(_spawn_y(schema)) < 1e-6 and abs(avatar.elevation) < 1e-6:
        rep.findings.append(Finding(
            "volume_player", ERROR,
            f"the game is played across {len(airborne)} group(s) at altitude "
            "but the player starts on the ground — the avatar is not in the "
            "volume the game happens in",
            "schema.entities.elevation / src/unity_spatial.py"))
        ok = False
    rep.checks["volume_player"] = ok


def _check_audio_coverage(schema: UnitySchema, rep: PlayabilityReport) -> None:
    """Silence reads as broken. Cheap to check, and the brief usually states
    exactly how many cues it wants."""
    ok = bool(schema.audio_specs or schema.audio_tracks)
    if not ok:
        rep.findings.append(Finding(
            "audio", WARN,
            "no audio specs or tracks — the game ships silent",
            "schema.audio_specs / schema.audio_tracks"))
    rep.checks["audio"] = ok


def _check_enemy_wiring(schema: UnitySchema, rep: PlayabilityReport) -> None:
    """An enemy script with nothing tagged Enemy has no one to drive; enemies
    tagged with no AI anywhere are inert models. Either way the encounter the
    design asked for does not happen."""
    tagged = [e for e in schema.entities if e.tag == "Enemy"]
    has_enemy_script = "enemy" in _controllers(schema)
    if not has_enemy_script:
        _ungraded(rep, "enemy_wiring",
                  "no enemy script in the build — nothing to be left undriven")
        return
    ok = True
    if not tagged:
        rep.findings.append(Finding(
            "enemy_wiring", WARN,
            "an enemy script exists but no entity carries tag=Enemy — the AI "
            "has nothing to drive",
            "schema.entities.tag"))
        ok = False
    rep.checks["enemy_wiring"] = ok


# -- kinematic reach ----------------------------------------------------------
# The structural checks above prove the pieces exist; these prove the player
# can actually get to them. Static positions, kinematic rules, no simulation
# loop -- Step 7 stays deterministic and LLM-free.

# Jump reach for grounded locomotion: an objective higher than this above the
# avatar, in a game with no way to climb or fly, is decoration.
_WALK_REACH_Y = 2.5
# A descent game only moves down; anything meaningfully above the jump point
# can never be visited.
_DESCENT_TOLERANCE = 2.0


def _spawn_y(schema: UnitySchema) -> float:
    """Where the avatar actually starts. With no Player-tagged entity the scene
    template's own Player object is the avatar and it spawns at the origin, so
    0.0 is the truthful answer rather than a fallback."""
    avatar = next((e for e in schema.entities if e.tag == "Player"), None)
    if avatar is None:
        return 0.0
    inst = _instances(avatar)
    return inst[0][1] if inst else avatar.elevation


def _objective_points(schema: UnitySchema) -> list[tuple[str, list[float]]]:
    refs = frozenset(_objective_names(schema))
    return [(e.name, p) for e in schema.entities if _is_objective(e, refs)
            for p in _instances(e)]


def _climb_ceiling(schema: UnitySchema, reach: float) -> float:
    """Highest altitude the hold chain reaches from the ground: walk the baked
    hold instances bottom-up and stop at the first gap wider than `reach` --
    the same connectivity the layout pass promises, re-verified on the built
    artifact rather than trusted."""
    ys = sorted(p[1] for e in schema.entities if is_climbable_entity(e)
                for p in _instances(e))
    ceiling = 0.0
    for y in ys:
        if y - ceiling > reach:
            break
        ceiling = max(ceiling, y)
    return ceiling


def _check_role_graph(schema: UnitySchema, rep: PlayabilityReport) -> None:
    """Surface what UnitySchema.enforce_role_graph recorded. R3 violations are
    written to meta["role_lint"] for exactly this gate to score; until now
    nothing read them, so an unresolvable role graph sailed through Step 7."""
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    lint = meta.get("role_lint")
    if not isinstance(lint, dict):
        _ungraded(rep, "role_graph",
                  "schema.meta.role_lint absent or malformed — "
                  "enforce_role_graph recorded nothing to score")
        return
    repairs    = [str(r) for r in (lint.get("repairs") or [])]
    violations = [str(v) for v in (lint.get("violations") or [])]
    if not repairs and not violations:
        _ungraded(rep, "role_graph",
                  "role_lint is clean — no repair or violation to report")
        return
    for r in repairs[:6]:
        rep.findings.append(Finding(
            "role_graph", INFO, f"repaired upstream: {r}",
            "unity_schema.enforce_role_graph"))
    for v in violations[:6]:
        rep.findings.append(Finding(
            "role_graph", ERROR,
            f"{v} -- win/lose wiring has no single protagonist to latch onto",
            "unity_schema.enforce_role_graph (R3)"))
    rep.checks["role_graph"] = not violations


def _check_objective_kinematics(schema: UnitySchema,
                                rep: PlayabilityReport) -> None:
    """Can the avatar, moving the way this primitive moves, reach the
    objectives? content_reachable proves things sit inside the ground plan;
    this proves the third axis -- a coin 40 m up in a walking game is inside
    every bound and still unwinnable."""
    objs = _objective_points(schema)
    if not objs:
        _ungraded(rep, "objective_kinematics",
                  "no objective group carries baked positions — nothing with a "
                  "height to test against the avatar's reach")
        return
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    archetype = str(meta.get("layout_archetype") or "")
    spawn = _spawn_y(schema)
    lost: dict[str, str] = {}
    if rep.primitive in (GROUND_PLANE, ROUTE_CORRIDOR):
        for name, p in objs:
            if p[1] - spawn > _WALK_REACH_Y:
                lost[name] = (f"{p[1]:.1f} m up, above jump reach in a "
                              "ground game")
    elif rep.primitive == WALL_SURFACE:
        # Ask the layout pass what reach it packed at rather than re-deriving
        # it: `_reach_distance` accepts three meta keys and its own named
        # default, and grading a wall packed at meta['hold_reach'] against a
        # locally-mirrored 2.5 collapses the ceiling and fails the gate on a
        # perfectly climbable wall.
        reach, _why_reach = _reach_distance(schema)
        ceiling = _climb_ceiling(schema, reach)
        for name, p in objs:
            if p[1] > max(ceiling, spawn) + reach:
                lost[name] = (f"at {p[1]:.1f} m but the hold chain ends at "
                              f"{ceiling:.1f} m")
    elif (rep.primitive in VOLUME_PRIMITIVES
          and archetype == "VerticalCorridor"
          # "You cannot fall upwards" is a statement about FREEFALL. A swimmer
          # in a descent corridor swims back up whenever they like, so applying
          # the rule to water failed a reef survey on fish 18 m above the diver.
          and (schema.locomotion or "").lower() != "swim"):
        for name, p in objs:
            if p[1] - spawn > _DESCENT_TOLERANCE:
                lost[name] = (f"{p[1] - spawn:.1f} m above the jump point in "
                              "a descent game -- you cannot fall upwards")
    # BoundedVolume / unknown archetype: free movement reaches anything inside
    # the bounds, and content_reachable already polices the bounds.
    if lost:
        worst = sorted(lost.items())[:4]
        rep.findings.append(Finding(
            "objective_kinematics", ERROR,
            "unreachable objective(s): "
            + "; ".join(f"{n} ({why})" for n, why in worst),
            "src/unity_layout.py baked positions"))
    rep.checks["objective_kinematics"] = not lost


_CHECKS = (
    _check_avatar,
    _check_avatar_binding,
    _check_avatar_cardinality,
    _check_controllable,
    _check_camera,
    _check_objective,
    _check_objective_targets,
    _check_brief_entity_coverage,
    _check_interactables_tagged,
    _check_scene_population,
    _check_spawn_clear,
    _check_content_reachable,
    _check_unbaked_gameplay,
    _check_volume_player,
    _check_audio_coverage,
    _check_enemy_wiring,
    _check_role_graph,
    _check_objective_kinematics,
)


def _primitive_of(schema: UnitySchema, has_route: bool) -> str:
    """The spatial primitive the LAYOUT pass actually used, when it has run.

    src/unity_layout.py stamps its archetype choice on schema.meta. It reads
    far more evidence than locomotion does — descent vs ascent framing,
    climbable entities, altitude data — so re-deriving here would let the
    evaluator grade a scene against a primitive it was never built as. Falls
    back to the locomotion heuristic for callers that evaluate a schema before
    layout.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    stamped = meta.get("spatial_primitive")
    if isinstance(stamped, str) and stamped:
        return stamped
    return select_primitive(schema.game_type, schema.locomotion,
                            has_route=has_route)


def evaluate(schema: UnitySchema) -> PlayabilityReport:
    """Grade a final schema. Deterministic, read-only, no LLM."""
    has_route = any(e.placement == "path" and e.path_points
                    for e in schema.entities)
    rep = PlayabilityReport(
        project_name=schema.project_name,
        primitive=_primitive_of(schema, has_route),
    )
    for check in _CHECKS:
        check(schema, rep)
    return rep


def gate(report: PlayabilityReport, min_score: int = 0,
         fail_on_error: bool = True,
         require_graded: bool = False) -> Optional[str]:
    """Reason to fail the build, or None to let it through.

    Defaults to "errors fail, score is advisory" so the evaluator can be
    switched on for an existing project without a score threshold nobody has
    tuned yet becoming a blocker.

    `require_graded` additionally fails when an ERROR-class check could not be
    graded at all. The eval harness passes it — a locked score floor means
    nothing if the checks behind it never ran — while the pipeline leaves it
    off, because a mid-pipeline schema legitimately lacks the evidence some
    checks need.
    """
    if fail_on_error and report.errors:
        return (f"{len(report.errors)} playability error(s): "
                + "; ".join(f.summary for f in report.errors[:3]))
    if require_graded and report.ungraded_errors:
        names = report.ungraded_errors
        return (f"{len(names)} ERROR-class check(s) could not be graded: "
                + "; ".join(f"{n} ({why})" for n, why in
                            sorted(names.items())[:3]))
    if min_score and report.score < min_score:
        return f"playability {report.score}/100 below the required {min_score}"
    return None
