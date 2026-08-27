"""Objectives micro-generator — mission objective tracking with completion events.

Deterministic consumer for schema.missions (MissionSpec/MissionObjective,
unity_schema.py:734-764) — previously orphaned schema surface with no runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, clamp_enum, apply_extensions_safe_block,
    UNITY_EXTRA_INSTRUCTIONS, harden_against_template,
)


ObjectivesVariant = Literal["tracker"]
VARIANTS = list(ObjectivesVariant.__args__)   # type: ignore[attr-defined]

ObjectiveType = Literal["reach", "collect", "eliminate", "escort", "survive", "interact"]
_VALID_OBJ_TYPE = frozenset(ObjectiveType.__args__)   # type: ignore[attr-defined]

# This micro's vocabulary and the IR's (unity_schema.MissionObjType = reach |
# collect | defeat | survive | escort | timer) were authored separately and
# disagree on two words in each direction. Before this table the disagreement
# was resolved by _clamp_type's fallback, so every authored `defeat` objective
# arrived here as "reach" — a silent rewrite that made kill objectives
# unroutable and, once SEAM 8 dispatches on type, unwireable. Translate the
# known synonyms instead of dropping them; genuinely unknown words still clamp.
_TYPE_ALIASES = {
    "defeat":    "eliminate",
    "kill":      "eliminate",
    "destroy":   "eliminate",
    "timer":     "survive",
    "use":       "interact",
    "talk":      "interact",
}


class ObjectiveEntry(BaseModel):
    id:          str
    description: str = ""
    type:        str = "reach"
    target_ref:  str = ""
    count:       int = 1
    optional:    bool = False
    # The id of an objective that must COMPLETE before this one can accept any
    # progress — the tracker's own analogue of board.py's zone `requires`.
    # Empty means "no prerequisite" (today's fully-parallel behaviour, still the
    # default). Fill-only in default_fix: a value the LLM/schema already set is
    # never overridden, matching every other deterministic backstop in this repo
    # (locomotion_modes, flying_abilities, ...). See ObjectivesPipeline.default_fix
    # and ObjectivesPipeline.harden.
    after:       str = ""

    @field_validator("type", mode="before")
    @classmethod
    def _clamp_type(cls, v) -> str:
        s = str(v).strip().lower()
        s = _TYPE_ALIASES.get(s, s)
        return s if s in _VALID_OBJ_TYPE else "reach"

    @field_validator("count", mode="before")
    @classmethod
    def _clamp_count(cls, v) -> int:
        try:
            return max(1, min(9999, int(v)))
        except (TypeError, ValueError):
            return 1


class ObjectivesSchema(CodeExtensibleSchema):
    name:        str                    = "MissionObjectives"
    variant:     str                    = "tracker"
    title:       str                    = "Mission"
    objectives:  list[ObjectiveEntry]   = Field(default_factory=list)
    rewards:     dict                   = Field(default_factory=dict)
    description: str                    = "Mission objective tracker"
    meta:        dict                   = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        return clamp_enum(v, tuple(VARIANTS), "tracker")


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity mission-objective tracker for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- The mission's objectives (reach a place, collect items, eliminate enemies, escort,
  survive, or interact with something) and how progress is reported
- Which objectives (if any) are optional
- What reward completing the mission grants

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
OBJECTIVE TYPE — reach | collect | eliminate | escort | survive | interact
"after" — the id of an objective that must complete FIRST, or "" if this
  objective is available from the start / runs in parallel with others.
  Leave "" unless the brief explicitly describes an order ("first ... then ...",
  "once you've ... you can ...") — a deterministic pass fills the ordinary
  step-by-step case for you, so guessing here is not required.

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "title": "Mission title",
  "objectives": [
    {{"id": "obj1", "description": "One sentence.", "type": "reach",
      "target_ref": "", "count": 1, "optional": false, "after": ""}}
  ],
  "rewards": {{}},
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class ObjectivesPipeline(UnityMicroPipeline):
    kind            = "objectives"
    display_name    = "Unity Mission Objectives"
    schema_cls      = ObjectivesSchema
    variants        = VARIANTS
    default_variant = "tracker"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "objectives"

    _RESERVED_FIELD_NAMES = {
        "objectives", "rewards", "title", "_progress", "_complete",
        # Also emitted by the template: the survive-objective clock and the
        # read-only readout surface a HUD binds to. An extra_field colliding
        # with any of these is CS0102 (or shadows the property), so lint drops it.
        "_surviveTimer", "_missionComplete", "_byId",
        "CompletedRequired", "TotalRequired", "ActiveObjective", "Title",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "System", "System.Collections.Generic"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "MissionObjectives")
        if not schema.title:
            schema.title = "Mission"
        # Every objective needs a non-empty id — synthesize one deterministically
        # from position if the LLM (or a schema-only caller) left it blank.
        seen: set[str] = set()
        for i, obj in enumerate(schema.objectives):
            if not obj.id:
                obj.id = f"objective_{i + 1}"
            base = obj.id
            n = 2
            while obj.id in seen:
                obj.id = f"{base}_{n}"
                n += 1
            seen.add(obj.id)
        # Deterministic step order: a blank `after` on a required objective
        # chains onto the PREVIOUS required objective in list order — the same
        # sequence `ActiveObjective` (Objectives.cs.j2) already implies by
        # returning "the first incomplete required objective". Before this,
        # ActiveObjective's display and ReportProgress's actual gating
        # disagreed: the HUD named a current step but any objective could
        # bank progress at any time. Fill-only — an explicit `after` (LLM- or
        # hand-authored, including one naming a non-adjacent id, or one set
        # on an optional objective) is never touched. Optional objectives are
        # skipped by the chain itself (they were never part of "the required
        # sequence") but may still carry their own explicit `after`.
        prev_required_id = ""
        for obj in schema.objectives:
            if obj.optional:
                continue
            if not obj.after:
                obj.after = prev_required_id
            prev_required_id = obj.id
        # hoist_field_inits: this generator's template renders awake_extra,
        # so a CS0236 initializer can be re-homed there instead of dropped.
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS,
                        hoist_field_inits=True)
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        return schema

    def harden(self, schema) -> list[str]:
        msgs: list[str] = []
        ids = {o.id for o in schema.objectives if o.id}

        for o in schema.objectives:
            if not o.after:
                continue
            if o.after == o.id:
                msgs.append(f"objectives: {o.id} is after itself — it can "
                            f"never unlock")
            elif o.after not in ids:
                msgs.append(f"objectives: {o.id} is after {o.after!r}, which "
                            f"is not an objective — it can never unlock")

        # Cycle check: walk each objective's `after` chain; a repeat within
        # one walk means every id revisited before the repeat can never
        # unlock (each waits on the next, forever). Same defect class as a
        # self-reference, just longer. Reported once per distinct cycle.
        by_id = {o.id: o for o in schema.objectives if o.id}
        reported: set[frozenset] = set()
        for start in by_id:
            path: list[str] = []
            seen: set[str] = set()
            cur = start
            while cur and cur in by_id and cur not in seen:
                seen.add(cur)
                path.append(cur)
                cur = by_id[cur].after
            if cur and cur in seen:
                cycle = path[path.index(cur):]
                key = frozenset(cycle)
                if key not in reported:
                    reported.add(key)
                    msgs.append("objectives: " + " -> ".join(cycle + [cur])
                                + " forms a cycle — none of these can ever "
                                  "unlock")
        # This override REPLACES MicroPipeline.harden's own body, the only
        # thing that normally calls harden_against_template — see rules.py's
        # harden() for the live GameRules.cs CS0111 this same gap caused
        # elsewhere (an authored extra_method colliding with a template
        # method, e.g. ReportProgress itself, would otherwise ship as a
        # duplicate unchecked).
        msgs += harden_against_template(schema, self.template_root, engine=self.engine)
        return msgs

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("Objectives.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
