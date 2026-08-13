"""
Playability repair — the feedback path from Step 7's evaluator back into the build.

WHY THIS EXISTS
---------------
The evaluator graded, printed a scorecard, and stopped. Everything it found was
found AFTER the project had already been written, so a build it knew could not
be completed shipped anyway with a number next to it. Worse, the number did not
even order the two correctly: a live A/B put an unwinnable build at 91/100 and a
winnable one at 90, because `objective` passes on the mere presence of something
to do and nothing graded the loop.

`mission_loop` (unity_playability.py) fixed the grading. This module closes the
loop: a finding that names a deterministic repair gets APPLIED, and the schema is
re-graded, before the generator runs.

WHAT MAY BE REPAIRED
--------------------
Only where AUTHORED CONTENT already answers the question the check failed on.
This is a compiler stage, not a second author:

  missions_from_story_beats
      The brief described its progression as NARRATIVE and never wrote a
      MISSIONS section — the exact shape the deterministic (--no-llm) fill
      produces, since it has no rule turning beats into objectives. Every beat
      already carries a `trigger` from a closed vocabulary that maps 1:1 onto an
      objective type, so the mission is a TRANSFORMATION of what the brief said,
      not an invention. Without it no objectives micro is composed, SEAM 8/9
      have nothing to bind, and the generated game cannot be won.

A remedy is never a guess. Where the honest answer is "the content is missing",
the finding stays a finding and the build reports it — abstain loudly, never
default quietly (CLAUDE.md).

DETERMINISM
-----------
No LLM, no network, no clock, no randomness — this runs inside Steps 4-7 and is
bound by the same contract. Same schema in, same repairs out. The loop is
bounded by `max_rounds` and stops as soon as a round applies nothing.
"""

from __future__ import annotations

import re

from .unity_playability import (REMEDY_MISSIONS_FROM_BEATS, PlayabilityReport,
                                evaluate)
from .unity_schema import MissionObjective, MissionSpec, UnitySchema

# StoryBeat.trigger -> MissionObjective.type. Both are closed vocabularies
# (unity_schema.TriggerType / MissionObjType), so this mapping is total over the
# triggers that describe something the PLAYER does. The two that do not are
# deliberately absent rather than mapped to a default:
#   on_start — the opening moment, not a goal
#   manual   — fired by a script, so nothing in the build signals it
_TRIGGER_TO_OBJECTIVE: dict[str, str] = {
    "on_enter_zone":  "reach",
    "on_pickup":      "collect",
    "on_enemy_death": "defeat",
    "on_timer":       "survive",
}

# `survive` counts seconds (Objectives.cs.j2 drains its own clock at 1/s), so a
# beat that says "hold out" needs a duration rather than a count of 1.
_SURVIVE_SECONDS = 60


def _objective_id(beat_id: str, label: str, taken: set[str]) -> str:
    """A stable, unique, lower_snake id for an objective derived from a beat."""
    raw = str(beat_id or label or "objective")
    slug = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_") or "objective"
    out, n = slug, 2
    while out in taken:
        out, n = f"{slug}_{n}", n + 1
    taken.add(out)
    return out


def _missions_from_story_beats(schema: UnitySchema) -> list[str]:
    """Derive one mission from the beats whose trigger names a player action."""
    if schema.missions or not schema.story_beats:
        return []

    taken: set[str] = set()
    objectives: list[MissionObjective] = []
    skipped: list[str] = []
    for beat in schema.story_beats:
        obj_type = _TRIGGER_TO_OBJECTIVE.get(str(beat.trigger or ""))
        if obj_type is None:
            skipped.append(f"{beat.id or beat.label} ({beat.trigger})")
            continue
        objectives.append(MissionObjective(
            id=_objective_id(beat.id, beat.label, taken),
            # The beat's own words: this is the line SEAM 9 puts on the HUD.
            description=(beat.description or beat.label or "").strip()[:160],
            type=obj_type,
            count=_SURVIVE_SECONDS if obj_type == "survive" else 1,
        ))

    if not objectives:
        # Every beat was on_start/manual. There is no honest objective to build
        # from those, so report rather than manufacture one.
        return ["playability repair: story beats carry no player-driven trigger "
                f"({', '.join(skipped[:4])}) — no mission derived; add an "
                "on_enter_zone / on_pickup / on_enemy_death / on_timer beat, or "
                "a MISSIONS section, to give the run a win condition"]

    title = (schema.project_name or "Mission").strip() or "Mission"
    schema.missions = [MissionSpec(
        id="main",
        title=title,
        description="Derived from the brief's narrative beats by the "
                    "playability repair pass.",
        objectives=objectives,
        story_beat_ref=schema.story_beats[0].id if schema.story_beats else "",
    )]
    msgs = [f"playability repair: derived 1 mission with {len(objectives)} "
            f"objective(s) from story beats "
            f"({', '.join(f'{o.id}:{o.type}' for o in objectives[:4])}"
            f"{' …' if len(objectives) > 4 else ''}) — the run now has a win "
            f"condition SEAM 8 can bind"]
    if skipped:
        msgs.append(f"playability repair: {len(skipped)} beat(s) carry no "
                    f"player-driven trigger and became no objective "
                    f"({', '.join(skipped[:4])})")
    return msgs


_REMEDIES = {
    REMEDY_MISSIONS_FROM_BEATS: _missions_from_story_beats,
}


def apply_remedies(schema: UnitySchema, report: PlayabilityReport) -> list[str]:
    """Apply every deterministic remedy the report carries. Returns log lines.

    An unknown remedy is reported and skipped, never guessed at — adding one
    means teaching this module to apply it.
    """
    msgs: list[str] = []
    seen: set[str] = set()
    for finding in report.findings:
        action = getattr(finding, "remedy", "")
        if not action or action in seen:
            continue
        seen.add(action)
        fn = _REMEDIES.get(action)
        if fn is None:
            msgs.append(f"playability repair: no handler for remedy "
                        f"{action!r} ({finding.check}) — reported only")
            continue
        msgs.extend(fn(schema))
    return msgs


def repair_and_regrade(schema: UnitySchema, *, max_rounds: int = 2
                       ) -> tuple[PlayabilityReport, list[str]]:
    """Grade, repair what can be repaired, re-grade. Deterministic and bounded.

    Returns the FINAL report and everything the repair pass did. Stops as soon
    as a round applies nothing, so a schema with no remediable findings costs
    exactly one evaluation.
    """
    report = evaluate(schema)
    msgs: list[str] = []
    for _ in range(max(1, max_rounds)):
        applied = apply_remedies(schema, report)
        if not applied:
            break
        msgs.extend(applied)
        report = evaluate(schema)
    return report, msgs
