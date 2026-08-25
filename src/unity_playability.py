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
from .unity_concept_fidelity import check_fidelity
from .unity_layout import _reach_distance, terrain_bounds
# `is_embodied` lives beside the field it reads (src/unity_schema.py) because
# four passes act on the declaration, not just this one. Re-exported here: it
# was this module's own function first, and the doctrine it carries is a
# grading rule as much as a compiler one.
from .unity_schema import EntitySpec, UnitySchema, is_embodied
from .unity_layout_archetype import (is_climbable_entity, pick_player_entity,
                                     ui_board_layer, ui_element_of)
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
    "mission_loop":      ERROR,
    "mission_visible":   WARN,
    # An optional objective that cannot advance is content the player can never
    # resolve — but it cannot block MissionComplete, so it is not the same
    # defect as an unbound required one and does not fail mission_loop.
    "mission_optional":  WARN,
    # Advisory only: carries the story-beats -> missions remedy rather than
    # grading anything, so it never sets rep.checks and never moves the score.
    "mission_derivable": INFO,
    "objective_targets": ERROR,
    "brief_entities":    ERROR,
    # WARN, not ERROR: a lost adjective is degraded fidelity, not a missing
    # entity — brief_entities already owns "the thing isn't in the game".
    "concept_fidelity":  WARN,
    # A custom script with no body is a component attached to a live object that
    # does nothing. WARN for the same reason: the mechanic is degraded, not the
    # game structurally broken.
    "custom_script_body": WARN,
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
    # ERROR for the same reason content_reachable is: content the player
    # cannot get to is content that is not in the game. On a board "cannot get
    # to" means "cannot click".
    "pointer_targets":   ERROR,
    # WARN, not ERROR: floating entities with no ground underfoot are a
    # polish defect (like silence from `audio`), not the difference between
    # "a game" and "a scene" — the avatar/objective/reachability checks
    # already own that bar.
    "world_terrain":     WARN,
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


# ── Remedies: the feedback path back into the pipeline ───────────────────────
# A finding that names a deterministic repair carries its action here.
# src/unity_playability_repair.py is the only thing that reads these; this
# module still never mutates the schema. Adding an action means teaching the
# repair pass to apply it — an unknown action is reported and skipped, never
# guessed at.
REMEDY_MISSIONS_FROM_BEATS = "missions_from_story_beats"


@dataclass(frozen=True)
class Finding:
    """One structural defect, named by the thing the player cannot do."""
    check: str
    severity: str
    summary: str
    # The schema field / pipeline stage responsible — so a failure points
    # somewhere rather than just scoring low.
    source: str = ""
    # A deterministic repair the pipeline can apply, or "" when the only honest
    # response is to report. Never a guess: a remedy exists only where authored
    # content already answers the question the check failed on.
    remedy: str = ""

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


# Checks that only mean something for a game with an avatar moving through
# space. A non-embodied game (locomotion="none") has no avatar to find, no
# spawn to clear, and no distance for the player to cover — grading it against
# these produced the CodeVault result: a correctly-designed repo-teaching game
# marked NOT PLAYABLE for lacking an avatar it should not have.
_EMBODIED_ONLY_CHECKS: frozenset[str] = frozenset({
    "avatar", "avatar_binding", "avatar_cardinality", "controllable",
    "spawn_clear", "content_reachable", "volume_player",
    "objective_kinematics", "unbaked_gameplay",
})

# The mirror set: checks that only mean something for a game the player acts on
# through a POINTER. Filtered out for an embodied game the same way, and for
# the same reason — a walking game has no board of click targets to grade.
#
# It exists because abstaining on the nine above left a hole exactly where
# `content_reachable` used to be: a non-embodied build could pass by having its
# hardest checks decline to run. `pointer_targets` is what fills it — the
# question "can the player act on anything at all" survives losing the avatar,
# it just stops being a question about distance.
_UI_ONLY_CHECKS: frozenset[str] = frozenset({
    "pointer_targets",
})


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


def _check_multi_mode_registry(schema: UnitySchema, rep: PlayabilityReport) -> None:
    """Control Surface Stack (docs/CONTROL_SURFACE_STACK.md): does the
    declared `locomotion_modes` registry actually mean what the brief asked
    for? Ungraded for every project that doesn't declare one — this only
    means something once a schema opts in.

    Two distinct failure shapes, both ERROR because both are silent-content-
    loss otherwise: `UnitySchema.enforce_locomotion_modes` already dropped
    anything outside the current vocabulary or duplicated by kind, recording
    what it dropped in schema.meta — this check is what turns that record
    into a graded finding instead of a fact only visible in a JSON blob. A
    single surviving entry is a WARN, not an ERROR: `subsystems_for` only
    activates multi_mode at 2+ modes (below that it silently composes as an
    ordinary single-variant project), so a 1-entry registry is not wrong,
    just inert — worth telling the author, not worth failing the build over.
    """
    if not schema.locomotion_modes:
        _ungraded(rep, "multi_mode_registry",
                  "schema.locomotion_modes is empty — this project does not "
                  "declare a Control Surface Stack")
        return

    lint = schema.meta.get("locomotion_modes_lint") if isinstance(schema.meta, dict) else None
    ok = True
    if isinstance(lint, dict) and (lint.get("dropped_unknown_kind") or lint.get("dropped_duplicate_kind")):
        unknown = lint.get("dropped_unknown_kind") or []
        dupe    = lint.get("dropped_duplicate_kind") or []
        detail = []
        if unknown:
            detail.append(f"unrecognized: {', '.join(unknown)}")
        if dupe:
            detail.append(f"duplicate: {', '.join(dupe)}")
        rep.findings.append(Finding(
            "multi_mode_registry", ERROR,
            f"locomotion_modes dropped entries the schema asked for "
            f"({'; '.join(detail)}) — the build has fewer runtime-switchable "
            "modes than the brief declared",
            "schema.locomotion_modes / UnitySchema.enforce_locomotion_modes"))
        ok = False

    if len(schema.locomotion_modes) == 1:
        rep.findings.append(Finding(
            "multi_mode_registry", WARN,
            f"locomotion_modes has exactly 1 entry ({schema.locomotion_modes[0]!r}) "
            "— a Control Surface Stack needs 2+ to switch between, so this "
            "project composes as an ordinary single-variant controller "
            "instead",
            "src/unity_compose.py subsystems_for"))

    rep.checks["multi_mode_registry"] = ok


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

    # A brief can describe its progression as NARRATIVE and never write a
    # MISSIONS section — which is exactly what the deterministic (--no-llm)
    # fill produces, since it has no rule that turns beats into objectives.
    # The result passes this check on story_beats alone and then composes no
    # objectives micro at all, so SEAM 8/9 have nothing to bind and the game
    # cannot be won. The beats already say what happens and when; deriving
    # objectives from them is a transformation of authored content, not
    # invention, so it carries a remedy.
    if ok and schema.story_beats and not schema.missions:
        rep.findings.append(Finding(
            "mission_derivable", INFO,
            f"{len(schema.story_beats)} story beat(s) but no mission — the run "
            "has content to reach and no objective tracking it, so nothing can "
            "report completion",
            "schema.story_beats -> schema.missions",
            remedy=REMEDY_MISSIONS_FROM_BEATS))


def _check_mission_loop(schema: UnitySchema, rep: PlayabilityReport) -> None:
    """Can the run actually be COMPLETED, and can the player see it happening?

    `objective` above passes on the mere presence of something to do — a
    collectible, an enemy, a story beat. That is why it graded an unwinnable
    build 91/100: the content was there and nothing advanced it. This check
    grades the loop itself, from what the wiring pass actually bound
    (schema.meta["wiring"], stamped by MicroComposer.wiring_report) rather than
    from what the schema merely declares.

    Completion is AND-shaped, and grading it as OR-shaped was this check's own
    version of the defect it exists to catch. `Objectives.cs.j2` sets
    MissionComplete only once EVERY non-optional objective is done, so a
    mission with one bound objective and five dead ones can never finish — and
    `ok = bool(bound)` passed it with a warning. It is graded on the required
    objectives that have no source instead: one of those is an unwinnable
    build, however many others bound.

    The mirror error was failing an all-`survive` mission, where the tracker's
    own clock advances every objective and the correct number of seams is zero.
    Self-driven objectives count as advanceable; optional ones cannot block,
    because they cannot block MissionComplete either.

    Ungraded on a non-composed build: without --compose there are no seams to
    bind objectives, and grading a standalone project against a loop it never
    claimed to have would be a false failure.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    wiring = meta.get("wiring")
    if not isinstance(wiring, dict) or not wiring:
        _ungraded(rep, "mission_loop",
                  "no composed wiring report — build did not run --compose")
        return
    total = int(wiring.get("objectives_total") or 0)
    if not total:
        _ungraded(rep, "mission_loop",
                  "the build declares no mission objectives to advance")
        return

    bound       = list(wiring.get("objectives_bound") or [])
    unbound     = list(wiring.get("objectives_unbound") or [])
    self_driven = list(wiring.get("objectives_self_driven") or [])
    optional    = list(wiring.get("objectives_optional_unbound") or [])
    advanceable = len(bound) + len(self_driven)
    required    = int(wiring.get("objectives_required") or (len(bound) + len(unbound)))

    # A report that declares objectives and then accounts for NONE of them is
    # not a failing build, it is a report this check cannot read. Wiring
    # reports written before `objectives_self_driven` existed look exactly like
    # a build where nothing bound: every list empty, `objectives_total` set. A
    # `survive`-only mission from that era was correctly self-driven and left
    # out of `objectives_unbound`, so grading the silence as ERROR accuses an
    # older project of being unwinnable on evidence that was never recorded.
    #
    # Ungraded, with the remedy, rather than passed: absence of evidence is not
    # evidence of playability either.
    if total and not (bound or unbound or self_driven or optional):
        _ungraded(rep, "mission_loop",
                  f"the wiring report declares {total} objective(s) but lists "
                  "none of them as bound, unbound or self-driven — it predates "
                  "`objectives_self_driven` and cannot be read. Re-run the "
                  "build with --compose to grade the loop")
        return

    ok = advanceable > 0 and not unbound
    if not advanceable:
        rep.findings.append(Finding(
            "mission_loop", ERROR,
            f"none of the {total} mission objective(s) has an event source, so "
            "nothing ever calls ReportProgress and the mission can never be "
            "completed — the project is a scene, not a game"
            + (f" (unbound: {', '.join(unbound[:4])})" if unbound else ""),
            "src/unity_compose.py SEAM 8"))
    elif unbound:
        rep.findings.append(Finding(
            "mission_loop", ERROR,
            f"{len(unbound)} of {required} REQUIRED objective(s) cannot advance "
            f"at runtime ({', '.join(unbound[:4])}) — no composed micro raises "
            "the event their type needs, and the mission completes only when "
            "every required objective does, so it can never be won",
            "src/unity_compose.py SEAM 8"))
    if optional:
        rep.findings.append(Finding(
            "mission_optional", WARN,
            f"{len(optional)} optional objective(s) cannot advance "
            f"({', '.join(optional[:4])}) — the run is still winnable without "
            "them, but they are content the player can never resolve",
            "src/unity_compose.py SEAM 8"))
    rep.checks["mission_loop"] = ok

    # Progress the player cannot see is progress they cannot act on. Graded on
    # anything being able to advance, not on the loop being whole: a build with
    # one dead objective still shows the other five, and skipping the readout
    # check would hide a second defect behind the first.
    if advanceable:
        shown = bool(wiring.get("hud_objective"))
        if not shown:
            rep.findings.append(Finding(
                "mission_visible", WARN,
                "the mission advances but nothing displays it — no HUD "
                "objective readout, so the player cannot tell they are winning",
                "src/unity_compose.py SEAM 9"))
        rep.checks["mission_visible"] = shown


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


def _check_concept_fidelity(schema: UnitySchema,
                            rep: PlayabilityReport) -> None:
    """Did the concrete WORDS the brief committed to survive into the build?

    `_check_brief_entity_coverage` grades NAMES: the brief said "Hippo", the
    schema has an entity called "Hippo", pass. It never looks at what that
    entity became, so "purple hippos with six legs" survives as an entity
    named Hippo with no colour and no leg count, and nothing here used to
    notice. `src/unity_concept_fidelity.py` measures the words themselves.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    prompt = str(meta.get("user_prompt", "") or "")
    if not prompt.strip():
        _ungraded(rep, "concept_fidelity",
                  "schema.meta.user_prompt is empty — nothing to compare the "
                  "build against (a replay grades the schema, not the brief)")
        return
    # The asset half only runs when the build stamped its resolved table
    # (unity_generator._resolve_all_models). Without it this graded modifiers
    # only — and an entity resolved to an unrelated model is the larger loss.
    paths = meta.get("entity_model_paths")
    fidelity = check_fidelity(
        prompt, schema,
        entity_model_paths=paths if isinstance(paths, dict) else None)
    # `and not gaps`: abstaining on an empty word count while holding asset gaps
    # would drop them silently, which is the defect this whole module measures.
    if not fidelity.checked and not fidelity.gaps:
        _ungraded(rep, "concept_fidelity",
                  "the brief commits to no concrete words to check")
        return
    if fidelity.gaps:
        lost = ", ".join(g.demand for g in fidelity.gaps[:6])
        n_asset = sum(1 for g in fidelity.gaps if g.kind == "asset")
        what = (f"{len(fidelity.gaps)}/{fidelity.checked} checks lost"
                if n_asset else
                f"{len(fidelity.gaps)}/{fidelity.checked} demanded words never "
                f"reached the build")
        rep.findings.append(Finding(
            "concept_fidelity", WARN,
            f"{fidelity.score}/100 concept fidelity — {what} ({lost})"
            + (f"; {n_asset} entity/entities resolved to an unrelated model"
               if n_asset else ""),
            "Step 3 schema-fill / Step 6 model spec"))
    rep.checks["concept_fidelity"] = not fidelity.gaps


# The fields that make a script DO something. Declared fields and usings are
# not behaviour — a class of fields that does nothing is the defect, not
# evidence against it. Kept in step with unity_micro_code._BEHAVIOUR_KEYS
# (tests/test_script_code_enrichment.py pins the two together).
_SCRIPT_BEHAVIOUR_KEYS = ("awake_extra", "update_extra", "fixed_update_extra",
                          "extra_methods")


def _script_body_is_live(script) -> bool:
    """True when this script's declared behaviour fields carry real content —
    not just a truthy container. A Step 3 schema-fill that hands a script
    `extra_methods=[{"body": ""}]` (a signature with an empty/whitespace
    body) reads as "has a body" under a plain truthiness check, letting a
    hollow script pass this grade. Content check, same field vocabulary as
    `_SCRIPT_BEHAVIOUR_KEYS` (kept in step with
    unity_micro_code._script_has_code — tests/test_script_code_enrichment.py
    pins the two together)."""
    for key in ("awake_extra", "update_extra", "fixed_update_extra"):
        if (getattr(script, key, "") or "").strip():
            return True
    return any((getattr(m, "body", "") or "").strip()
               for m in (getattr(script, "extra_methods", None) or []))


def _check_custom_script_bodies(schema: UnitySchema,
                                rep: PlayabilityReport) -> None:
    """A `type: custom` script that holds data and does nothing.

    The other script types render a proven baseline template. The custom
    template (templates/unity/scripts/CustomBehaviour.cs.j2) renders NOTHING but
    the fields and bodies the schema carries, so a custom script with every body
    empty compiles to a class of `[SerializeField]` fields with no methods —
    and `attach_to` still puts it on a live GameObject.

    Nothing reported this. It is not a compile error, not a missing entity, and
    not a broken seam; the class is there, the component is there, and the
    behaviour its own `description` promises simply is not. Measured on
    CodeBranchSim: four custom scripts, 0 authored method blocks between them,
    against 14 across the composed micros.
    """
    scripts = [s for s in (getattr(schema, "scripts", None) or [])
               if str(getattr(s, "type", "")) == "custom"]
    if not scripts:
        _ungraded(rep, "custom_script_body",
                  "this build declares no type:custom scripts — the other types "
                  "render a baseline template and carry behaviour without one")
        return

    empty = [str(getattr(s, "name", "") or "?") for s in scripts
             if not _script_body_is_live(s)]
    if empty:
        rep.findings.append(Finding(
            "custom_script_body", WARN,
            f"{len(empty)} of {len(scripts)} custom script(s) have no body "
            f"({', '.join(empty[:4])}) — each is a component attached to a live "
            "object that holds data and does nothing, so the behaviour its "
            "description promises is absent from the build",
            "Step 3 schema-fill (scripts[].awake_extra/update_extra/"
            "extra_methods) / Step 3.5a unity_micro_code.enrich_scripts"))
    rep.checks["custom_script_body"] = not empty


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

    # `kind` alone answers this for a game with a world, where a prop is a
    # thing standing next to the game. On a BOARD a prop is the game: CodeVault
    # authored every one of its file icons, folder containers and terminal
    # panels as kind="prop", so reading kind alone called a complete,
    # click-driven teaching sim a diorama. What separates content from scenery
    # there is the layer it sits on — a backdrop and a caption are scenery, a
    # control is not.
    embodied = is_embodied(schema)

    def _is_content(e: EntitySpec) -> bool:
        if embodied:
            return (e.kind or "").lower() not in _SCENERY_KINDS
        return ui_board_layer(e) == "board"

    empty: list[str] = []
    for spec in subsets:
        hosted = [by_name[n] for n in (spec.entities or []) if n in by_name]
        if not any(_is_content(e) for e in hosted):
            empty.append(spec.name)
    if empty:
        rep.findings.append(Finding(
            "scene_population", WARN,
            f"scene(s) {', '.join(empty)} host only scenery — nothing to do "
            "once the player arrives",
            "schema.scene_specs.entities"))
    rep.checks["scene_population"] = not empty


def _check_objective_scene_reachability(schema: UnitySchema,
                                        rep: PlayabilityReport) -> None:
    """A mission's objective target must actually spawn in the scene that
    mission runs in, or SEAM 8's wiring (unity_compose.py) has nothing to
    bind to no matter how correctly it is scoped. Confirmed live
    (GearCitadel): HallOne's entity subset omitted GearGate/SaveCogShrine —
    the exact two entities Hall One Clearance's own objectives target — so a
    correctly-wired mission still had nothing to interact with.
    `_check_scene_population` only asks "does this scene host SOME content";
    this asks the sharper question a passing `scene_population` grade can
    still hide: is THIS mission's specific target among it.

    Graded only for missions `_infer_mission_scenes` (src/unity_pipeline.py)
    could actually resolve to a scene (`schema.meta["mission_scenes"]`) — a
    mission it could not place is not this check's to judge; that gap is
    reported separately, as a WARN, when `mission_scenes` was computed."""
    mission_scenes = (schema.meta.get("mission_scenes")
                      if isinstance(schema.meta, dict) else None) or {}
    if not mission_scenes:
        _ungraded(rep, "objective_scene_reachability",
                  "no mission could be matched to a scene — nothing to "
                  "cross-check")
        return
    by_spec = {s.name: s for s in (schema.scene_specs or [])}
    missing: list[str] = []
    for mission in (schema.missions or []):
        for scene_name in mission_scenes.get(mission.id, ()):
            spec = by_spec.get(scene_name)
            if spec is None or spec.entities is None:
                continue   # unrestricted scene already hosts everything
            for o in (mission.objectives or []):
                if o.target_ref and o.target_ref not in spec.entities:
                    missing.append(f"{mission.title}.{o.id} (target "
                                   f"'{o.target_ref}' not in scene '{scene_name}')")
    if missing:
        rep.findings.append(Finding(
            "objective_scene_reachability", ERROR,
            f"{len(missing)} objective target(s) do not spawn in the scene "
            f"their own mission runs in: {', '.join(missing[:6])}",
            "schema.scene_specs.entities / schema.missions.objectives.target_ref"))
    rep.checks["objective_scene_reachability"] = not missing


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


def _check_world_terrain(schema: UnitySchema, rep: PlayabilityReport) -> None:
    """A `schema.world_spec` with regions declares intent to have real ground
    under the player. If EVERY region opts out of terrain generation (a
    no-ground biome or a degenerate bounds rect — see
    src/unity_terraingen.py's NO_GROUND_BIOMES / abstain rules), that is
    usually a typo'd biome or bounds rect, not a deliberate interior-only
    world, and the compose pass (src/unity_pipeline.py) will have baked no
    terrain at all — abstain loudly here rather than let it pass silently as
    floating entities with nothing underfoot."""
    world = schema.world_spec
    if not world or not getattr(world, "regions", None):
        _ungraded(rep, "world_terrain",
                  "schema.world_spec has no regions — nothing to bake ground for")
        return
    from .unity_terraingen import terrain_jobs_for_world
    ok = bool(terrain_jobs_for_world(world))
    if not ok:
        rep.findings.append(Finding(
            "world_terrain", WARN,
            f"schema.world_spec declares {len(world.regions)} region(s) but none "
            f"got a terrain baked — every region has a no-ground biome or a "
            f"degenerate bounds rect. Check for a typo'd biome/bounds, or set "
            f"biome to a NO_GROUND_BIOMES value explicitly if that's intended",
            "schema.world_spec.regions"))
    rep.checks["world_terrain"] = ok


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


def _check_pointer_targets(schema: UnitySchema,
                           rep: PlayabilityReport) -> None:
    """A non-embodied game has to give the pointer something to act on.

    The mirror of `content_reachable`, and the reason the nine abstentions in
    `_EMBODIED_ONLY_CHECKS` do not leave a hole. "Can the player get to the
    content" survives losing the avatar — it just stops being a question about
    distance and becomes one about targets: with no avatar, a board the pointer
    cannot act on is a slideshow, and every check that would have caught that
    was an avatar check.

    A target is an entity the player can act ON: one carrying an interaction
    tag, one an objective names, or one that reads as a control (a button, a
    card, an icon — `ui_element_of`). A backdrop is not a target and neither is
    a caption, which is exactly why they are excluded: a board made only of
    labels and backing plates has nothing to do in it.
    """
    if not schema.entities:
        _ungraded(rep, "pointer_targets",
                  "the schema declares no entities at all, so there is no "
                  "board to grade")
        return
    named = frozenset(_objective_names(schema))
    targets: list[str] = []
    for e in schema.entities:
        if ui_board_layer(e) != "board":
            continue          # backdrops and captions are not acted on
        tag  = (e.tag or "").strip().lower()
        kind = (e.kind or "").strip().lower()
        if (tag in _OBJECTIVE_TAGS or kind in _OBJECTIVE_KINDS
                or e.name.strip().lower() in named
                or ui_element_of(e) is not None):
            targets.append(e.name)
    if not targets:
        rep.findings.append(Finding(
            "pointer_targets", ERROR,
            "no entity the pointer can act on — every group is a backdrop, a "
            "caption or scenery, so the board can be looked at but not played",
            "schema.entities.tag / .kind / missions[].objectives[].target_ref"))
    rep.checks["pointer_targets"] = bool(targets)


_CHECKS = (
    _check_avatar,
    _check_avatar_binding,
    _check_avatar_cardinality,
    _check_controllable,
    _check_multi_mode_registry,
    _check_camera,
    _check_objective,
    _check_mission_loop,
    _check_objective_targets,
    _check_brief_entity_coverage,
    _check_concept_fidelity,
    _check_custom_script_bodies,
    _check_interactables_tagged,
    _check_scene_population,
    _check_objective_scene_reachability,
    _check_spawn_clear,
    _check_content_reachable,
    _check_unbaked_gameplay,
    _check_volume_player,
    _check_audio_coverage,
    _check_world_terrain,
    _check_enemy_wiring,
    _check_role_graph,
    _check_objective_kinematics,
    _check_pointer_targets,
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

    # A non-embodied game answers the spatial checks by not being that kind of
    # game. Filtered here rather than gated inside each check so a new
    # embodied-only check cannot forget to opt out — the list is the contract.
    # Their verdicts are DISCARDED, not passed: recording them as ungraded
    # keeps the "absence of evidence is never evidence of playability" rule
    # (they show on the scorecard and still fail gate(require_graded=True)).
    #
    # The filter runs in BOTH directions, and both directions RECORD what they
    # dropped. Popping a check silently is the defect this module was rewritten
    # to remove — the score is the share of the checks that ran, so an unlisted
    # check is one the denominator quietly stopped covering. Declaring
    # locomotion='none' therefore trades nine listed abstentions for one, and
    # never buys a smaller scorecard.
    if not is_embodied(schema):
        dropped, why = _EMBODIED_ONLY_CHECKS, (
            "locomotion='none' — the game declares no avatar, so there is "
            "nothing to place, control or reach")
    else:
        dropped, why = _UI_ONLY_CHECKS, (
            "the game has an avatar — it is played by moving through a space, "
            "not by pointing at a board")
    for name in dropped:
        rep.checks.pop(name, None)
        _ungraded(rep, name, why)
    rep.findings = [f for f in rep.findings if f.check not in dropped]
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
