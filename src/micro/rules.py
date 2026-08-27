"""Rules micro-generator — the layer that makes a build a GAME.

Why this exists
---------------
Every other micro is a MECHANISM: a controller that moves, a trigger that
fires, an objectives tracker that counts. Nothing in the compiler was ever
responsible for the three things that turn mechanisms into a game —

    the rules         what a legal move is, what completes an objective
    the progression   what to do first, next, last
    the instructions  what the player is supposed to do at all

— so no build has ever had them. A human launching a generated project saw a
scene, a HUD with widgets, and no way to know what the verb was.

This micro owns the second and third, and reads the first off the schema.
It is deliberately NOT per-subsystem: it is composed once per project, takes
the mission the schema already declares, and emits the briefing the player
reads before the shift, the step list they work through, and the hint bar that
names the next action at every moment.

How it knows a step is done
---------------------------
It does not observe entities itself. Each step carries the id of a
`MissionObjective`, and the objectives tracker already raises
`OnObjectiveComplete(id)` when SEAM 8's event source fires. Riding that seam
rather than re-deriving completion keeps one definition of "done" in the
build — and means a project whose objectives cannot be advanced shows up as a
step that never clears, instead of two subsystems quietly disagreeing.

The shape is not invented here. It was written by hand twice, for TowerControl
(an approach-control console) and CodeVault (a git teaching sim), before being
templated: the two differ entirely in domain and rules and are structurally
identical in briefing, step plan, hint bar and completion report. That
identity is the reason this is a template rather than a bespoke script.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .code_fields import (UNITY_EXTRA_INSTRUCTIONS, CodeExtensibleSchema,
                          clamp_enum, lint_extensible, harden_against_template,
                          apply_extensions_safe_block)
from .unity_base import UnityMicroPipeline

# "guided"  — briefing modal + step panel + hint bar (the full teaching shape)
# "hud_only" — hint bar alone, for a game whose rules the player already knows
RulesVariant = Literal["guided", "hud_only"]
VARIANTS = list(RulesVariant.__args__)   # type: ignore[attr-defined]


class RuleStep(BaseModel):
    """One beat of the progression.

    `objective_id` is the contract with the objectives tracker. `teaches` is
    what the step means and `ask` is what to do about it — kept separate
    because a player who is stuck needs the instruction without re-reading the
    explanation, and the hint bar shows only `ask`.
    """
    objective_id: str = ""
    title:        str = ""
    teaches:      str = ""
    ask:          str = ""

    @field_validator("title", "teaches", "ask", mode="before")
    @classmethod
    def _clean(cls, v) -> str:
        return " ".join(str(v or "").split())[:400]


class RulesSchema(CodeExtensibleSchema):
    name:        str = "GameRules"
    variant:     str = "guided"
    game_title:  str = ""
    # One paragraph the player reads before anything starts. Not a tagline —
    # it has to say what winning is, because nothing else in the build does.
    briefing:    str = ""
    goal:        str = ""
    steps:       list[RuleStep] = Field(default_factory=list)
    # Text shown once every step is complete.
    outro:       str = ""
    description: str = "Game rules, progression and player instructions"
    meta:        dict = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v) -> str:
        return clamp_enum(v, tuple(VARIANTS), "guided")


CONCEPT_PROMPT = """\
{theme_block}
Describe, for this game idea, what the PLAYER is told (presentation: {variant}):

"{user_prompt}"

Cover only: what the player is trying to achieve, and the order they do things
in. Do not describe rendering, architecture or packages.
"""

SCHEMA_PROMPT = """\
You are writing the words a player reads. The game:

{concept}
{schema_hints}

The mission's objectives, in order — each step you write MUST carry one of
these ids in `objective_id`, and you may not invent others:
{objectives}

Return ONE JSON object:
{{
  "game_title": "short name of the game",
  "goal": "one sentence: what counts as winning",
  "briefing": "3-6 sentences the player reads before starting. Say what they
               are doing, what is on screen, and how to act on it.",
  "steps": [
    {{"objective_id": "<one of the ids above>",
      "title": "SHORT STEP TITLE",
      "teaches": "one or two sentences on what this idea IS",
      "ask": "the exact action to take now, in the imperative"}}
  ],
  "outro": "one or two sentences shown when every step is done"
}}

Write `ask` so a player who has read nothing else can act on it. Name the
control, the key or the object by the name it has on screen.
""" + UNITY_EXTRA_INSTRUCTIONS


class RulesPipeline(UnityMicroPipeline):
    kind            = "rules"
    display_name    = "Unity Game Rules"
    schema_cls      = RulesSchema
    variants        = VARIANTS
    default_variant = "guided"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "rules"

    _RESERVED_FIELD_NAMES = {
        "steps", "_step", "_briefingOpen", "_objectives", "_hint", "_title",
        "_body", "_panel", "CurrentStep", "Finished", "StepIndex",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "UnityEngine.UI", "System",
                        "System.Collections.Generic"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "GameRules")
        if not schema.game_title:
            schema.game_title = "This Game"
        if not schema.goal:
            schema.goal = "Complete every objective."

        # A step with no ask is a step the player cannot act on, which is the
        # whole defect this micro exists to remove — fill it from the objective
        # description rather than shipping a blank hint bar.
        for i, s in enumerate(schema.steps):
            if not s.title:
                s.title = f"STEP {i + 1}"
            if not s.ask:
                s.ask = s.teaches or "Complete this objective."
            if not s.teaches:
                s.teaches = s.ask

        if not schema.briefing:
            schema.briefing = (
                f"{schema.game_title}. {schema.goal} "
                + " ".join(f"{i + 1}. {s.ask}" for i, s in enumerate(schema.steps))
            ).strip()
        if not schema.outro:
            schema.outro = "Every objective complete."
        return schema

    def harden(self, schema) -> list[str]:
        msgs: list[str] = []
        # Steps whose objective_id matches nothing can never clear, so the
        # player would be stranded on them forever. Reported, never silently
        # dropped: a progression that lost a step is a different game.
        allowed = {str(i) for i in (schema.meta.get("objective_ids") or [])}
        if allowed:
            stranded = [s.title for s in schema.steps
                        if s.objective_id and s.objective_id not in allowed]
            if stranded:
                msgs.append(
                    "rules: step(s) " + ", ".join(stranded) + " reference an "
                    "objective id this mission does not declare — they can "
                    "never clear. Ids available: " + ", ".join(sorted(allowed)))
        if not schema.steps:
            msgs.append("rules: no steps — the player is told the goal and "
                        "nothing about how to reach it")
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        # This override REPLACES MicroPipeline.harden's own body, which is the
        # only thing that normally calls harden_against_template — every other
        # `harden()` override in this repo has the same gap. Confirmed live:
        # GameRules.cs(262,18) CS0111, an LLM-authored `extra_method` named
        # `BindObjectives()` (matching the template's own private method)
        # shipped as a duplicate, because nothing here ever checks authored
        # members against what GameRules.cs.j2 itself already defines.
        msgs += harden_against_template(schema, self.template_root, engine=self.engine)
        return msgs

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts = out_dir / "Scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / f"{schema.name}.cs").write_text(
            env.get_template("GameRules.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8")
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8")
        return 2


_OBJECTIVE_VERBS = {
    "collect": "Collect", "reach": "Get to", "defeat": "Defeat",
    "eliminate": "Defeat", "survive": "Survive", "timer": "Survive",
    "escort": "Escort", "interact": "Use",
}


def steps_from_missions(missions, concept: str = "") -> list[dict]:
    """A deterministic step per authored objective, across EVERY mission that
    declares one, in schema order.

    The fallback when no model authored the prose: every objective still
    becomes a step the player is told to do, using the objective's own
    description. Deliberately not clever — a plain instruction beats a blank
    hint bar, which is what every build shipped with until now.

    ONE progression, not one per mission. SEAM 8a binds objectives from every
    mission — its whole point is that a board event carries the group it
    happened to, so each objective can have its own filtered binding — while
    this list stopped at the first. A three-mission project then ran with six
    live objectives and told the player about two, and the board's own domain
    derivation was held to the first mission for the same reason: what the
    board ENFORCES and what the player is SHOWN have to be two readings of one
    list. Widening both together keeps that true and stops the progression
    lying by omission.

    The mission title rides in the step title when there is more than one, so
    the panel names the phase the step belongs to. A single-mission project
    produces exactly what it did before.
    """
    real = [m for m in (missions or []) if getattr(m, "objectives", None)]
    total = sum(len(m.objectives) for m in real)
    multi = len(real) > 1
    out: list[dict] = []
    i = 0
    for mission in real:
        phase = " ".join(str(getattr(mission, "title", "") or "").split())
        for o in mission.objectives:
            i += 1
            oid = str(getattr(o, "id", "") or "").strip()
            desc = str(getattr(o, "description", "") or "").strip()
            otype = str(getattr(o, "type", "") or "").strip().lower()
            target = str(getattr(o, "target_ref", "") or "").strip()
            verb = _OBJECTIVE_VERBS.get(otype, "Complete")
            head = desc[:38].upper() if desc else f"OBJECTIVE {i}"
            out.append({
                "objective_id": oid,
                "title": f"{i} / {total}  "
                         + (f"{phase.upper()} — {head}" if multi and phase
                            else head),
                "teaches": desc or f"{verb} {target or 'the objective'}.",
                "ask": desc or f"{verb} {target or 'the objective'}.",
            })
    return out


def steps_from_mission(mission, concept: str = "") -> list[dict]:
    """One mission's worth of `steps_from_missions`. Kept because a caller with
    a single mission in hand should not have to wrap it in a list."""
    return steps_from_missions([mission], concept)
