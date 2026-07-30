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
from .unity_layout import terrain_bounds
from .unity_schema import EntitySpec, UnitySchema
from .unity_spatial import (VOLUME_PRIMITIVES, is_grounded, select_primitive)

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
    "controllable":      ERROR,
    "objective":         ERROR,
    "brief_entities":    ERROR,
    "spawn_clear":       ERROR,
    "content_reachable": ERROR,
    "volume_player":     ERROR,
    "camera":            WARN,
    "interactables":     WARN,
    "scene_population":  WARN,
    "audio":             WARN,
    "enemy_wiring":      WARN,
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

# An objective is anything that can end or score a run. Collectibles and
# enemies count: "pick these up" and "defeat these" are objectives even when
# the schema never writes a MissionSpec for them.
_OBJECTIVE_KINDS: frozenset[str] = frozenset({
    "collectible", "pickup", "item", "enemy", "boss", "goal", "objective",
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
    # check name -> True when it passed, for the scorecard's "n/m checks"
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARN]

    @property
    def score(self) -> int:
        """0-100: the weighted share of checks that passed. Weighted by
        severity, so losing the player's avatar costs three times what a thin
        audio bed does. 100 means every check passed."""
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
        head = (f"PLAYABILITY {self.score}/100  "
                f"({sum(self.checks.values())}/{len(self.checks)} checks)  "
                f"space={self.primitive}  "
                f"{'PLAYABLE' if self.playable else 'NOT PLAYABLE'}")
        if not self.findings:
            return head + "\n  no findings"
        ordered = sorted(self.findings,
                         key=lambda f: (_SEVERITY_ORDER[f.severity], f.check))
        return "\n".join([head] + [f"  {f}" for f in ordered])


# ── individual checks ─────────────────────────────────────────────────────────
# Each appends its findings and records pass/fail under its own name.

def _controllers(schema: UnitySchema) -> set[str]:
    return {(s.type or "").lower() for s in (schema.scripts or [])}


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
    has_content_objective = any(
        (e.kind or "").lower() in _OBJECTIVE_KINDS for e in schema.entities)
    ok = bool(has_content_objective or schema.missions or schema.story_beats)
    if not ok:
        rep.findings.append(Finding(
            "objective", ERROR,
            "nothing to do — no collectibles, no enemies, no missions and no "
            "story beats, so the run has no goal and cannot be won or lost",
            "schema.entities.kind / schema.missions / schema.story_beats"))
    rep.checks["objective"] = ok


def _check_brief_entity_coverage(schema: UnitySchema,
                                 rep: PlayabilityReport) -> None:
    """Every entity the BRIEF named explicitly should exist in the final
    schema. This is the check that would have caught both reported failures
    at build time instead of in the editor."""
    named = extract_named_entities(
        (schema.meta or {}).get("user_prompt", "") if isinstance(schema.meta, dict)
        else "")
    if not named:
        rep.checks["brief_entities"] = True
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
    untagged = [e.name for e in schema.entities
                if (e.kind or "").lower() in _OBJECTIVE_KINDS
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
    empty: list[str] = []
    for spec in (schema.scene_specs or []):
        if not spec.entities:
            continue     # no subset declared -> hosts everything
        hosted = [by_name[n] for n in spec.entities if n in by_name]
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


def _check_spawn_clear(schema: UnitySchema, rep: PlayabilityReport) -> None:
    """The scene template puts the player at world XZ (0,0). A group whose
    instance sits on that cell traps the player inside a collider at t=0."""
    blockers: list[str] = []
    for e in schema.entities:
        if e.tag == "Player" or is_grounded(e):
            continue
        for p in _instances(e):
            if math.hypot(p[0], p[2]) < 1.5 and abs(p[1]) < 2.0:
                blockers.append(e.name)
                break
    if blockers:
        rep.findings.append(Finding(
            "spawn_clear", ERROR,
            f"{', '.join(sorted(set(blockers))[:6])} spawn(s) on the player's "
            "own start cell — the player begins trapped inside a collider",
            "src/unity_layout.py packing"))
    rep.checks["spawn_clear"] = not blockers


def _check_content_reachable(schema: UnitySchema,
                             rep: PlayabilityReport) -> None:
    """Every group has to sit inside the ground the scene actually builds,
    otherwise it hangs over the void where the player cannot follow."""
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


def _check_volume_player(schema: UnitySchema, rep: PlayabilityReport) -> None:
    """A volume game whose avatar is left on the floor while all its content
    is at altitude: the player starts at the bottom of a shaft looking up at
    a game they are not in. Only meaningful under a volume primitive."""
    if rep.primitive not in VOLUME_PRIMITIVES:
        rep.checks["volume_player"] = True
        return
    airborne = [e for e in schema.entities
                if not is_grounded(e) and abs(e.elevation) > 1e-6]
    avatar = next((e for e in schema.entities if e.tag == "Player"), None)
    ok = True
    if airborne and avatar is not None and abs(avatar.elevation) < 1e-6:
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
    ok = True
    if has_enemy_script and not tagged:
        rep.findings.append(Finding(
            "enemy_wiring", WARN,
            "an enemy script exists but no entity carries tag=Enemy — the AI "
            "has nothing to drive",
            "schema.entities.tag"))
        ok = False
    rep.checks["enemy_wiring"] = ok


_CHECKS = (
    _check_avatar,
    _check_controllable,
    _check_camera,
    _check_objective,
    _check_brief_entity_coverage,
    _check_interactables_tagged,
    _check_scene_population,
    _check_spawn_clear,
    _check_content_reachable,
    _check_volume_player,
    _check_audio_coverage,
    _check_enemy_wiring,
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
         fail_on_error: bool = True) -> Optional[str]:
    """Reason to fail the build, or None to let it through.

    Defaults to "errors fail, score is advisory" so the evaluator can be
    switched on for an existing project without a score threshold nobody has
    tuned yet becoming a blocker.
    """
    if fail_on_error and report.errors:
        return (f"{len(report.errors)} playability error(s): "
                + "; ".join(f.summary for f in report.errors[:3]))
    if min_score and report.score < min_score:
        return f"playability {report.score}/100 below the required {min_score}"
    return None
