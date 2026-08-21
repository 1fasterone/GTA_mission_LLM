"""Trigger micro-generator — zone, timer, distance, and compound triggers."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import CodeField, CodeMethod, lint_code_fragment, lint_usings, lint_fields


TriggerVariant = Literal[
    "zone_enter", "zone_exit", "timer_trigger",
    "distance_trigger", "compound",
    # The pointer's zone. Every variant above waits for a BODY — a tagged
    # collider entering, exiting, or coming within a radius — which a game that
    # declared it has no avatar (locomotion='none') can never satisfy: its
    # triggers would sit armed forever and SEAM 7/8 would wire a mission
    # objective to an event that cannot fire. click_zone is the same trigger
    # with the same OnActivated seam, reached by clicking the region instead.
    "click_zone",
]
VARIANTS = list(TriggerVariant.__args__)  # type: ignore[attr-defined]


class TriggerAction(BaseModel):
    event_name: str           # SendMessage target on this or BroadcastMessage
    delay:      float = 0.0   # optional WaitForSeconds before firing
    one_shot:   bool  = True  # fire only once per activation cycle


class TriggerSchema(BaseModel):
    name:            str = "TriggerZone"
    variant:         str = "zone_enter"
    target_tag:      str = "Player"
    cooldown:        float = 0.0      # seconds between activations (0 = unlimited)
    one_shot:        bool  = False    # if True: deactivates permanently after first fire
    actions:         list[TriggerAction] = Field(default_factory=list)
    timer_interval:  float = 5.0      # timer_trigger: seconds between fires
    detect_radius:   float = 5.0      # distance_trigger: trigger radius
    require_all:     bool  = False    # compound: true = all conditions must be met
    description:     str  = "Event trigger"

    extra_usings:  list[str]        = Field(default_factory=list)
    extra_fields:  list[CodeField]  = Field(default_factory=list)
    extra_methods: list[CodeMethod] = Field(default_factory=list)
    meta:          dict             = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "zone_enter"

    @field_validator("cooldown", "timer_interval", "detect_radius", mode="before")
    @classmethod
    def _clamp_positive(cls, v) -> float:
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return 0.0


CONCEPT_PROMPT = """\
{theme_block}
Describe a trigger system for this Unity game scenario:

"{user_prompt}"

Variant: {variant}

Cover in 1-2 paragraphs:
- What activates the trigger (player enters a zone, a timer fires, distance to target, ...)
- What events it fires when activated (spawns enemies, plays music, starts dialogue, ...)
- Any one-shot vs repeating behaviour, cooldowns, or conditional requirements

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into a trigger schema. Output ONLY JSON.

The template renders a TriggerZone MonoBehaviour that:
  - Detects the activation condition (enter, exit, timer, distance, or compound)
  - Calls SendMessage() for each action.event_name on activation
  - Respects cooldown and one_shot flags

Rules:
- event_name: a method name that exists on another script (e.g. "OnBossArena", "StartWave")
- delay > 0: action fires after that many seconds
- one_shot at the TriggerSchema level: the whole trigger deactivates after first fire
- For compound variant: add extra C# conditions in extra_fields + check them in
  extra_methods named like "bool ConditionMet()"

VARIANT: {variant}

BRIEF:
{concept}

OUTPUT (JSON only):
{{
  "name": "PascalCaseName",
  "variant": "{variant}",
  "description": "One sentence.",
  "target_tag": "Player",
  "cooldown": 0.0,
  "one_shot": false,
  "timer_interval": 5.0,
  "detect_radius": 5.0,
  "require_all": false,
  "actions": [
    {{"event_name": "OnTriggerActivate", "delay": 0.0, "one_shot": true}}
  ],
  "extra_usings": [],
  "extra_fields": [],
  "extra_methods": []
}}

Derive actions from the prompt concept. Output ONLY JSON.
"""


class TriggerPipeline(UnityMicroPipeline):
    kind            = "trigger"
    display_name    = "Unity Trigger / Event Zone"
    schema_cls      = TriggerSchema
    variants        = VARIANTS
    default_variant = "zone_enter"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "trigger"

    _RESERVED_FIELD_NAMES = {"_fired", "_timer", "_lastFire", "_player"}
    _TEMPLATE_USINGS = {"System", "UnityEngine", "System.Collections"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "TriggerZone")
        if not schema.actions:
            schema.actions = [TriggerAction(event_name="OnTriggerActivate", delay=0.0)]
        from .code_fields import _KNOWN_NAMESPACES
        schema.extra_usings = lint_usings(
            schema.extra_usings, already_in_template=self._TEMPLATE_USINGS,
            allowed=_KNOWN_NAMESPACES,
        )
        schema.extra_fields = lint_fields(
            schema.extra_fields, reserved=self._RESERVED_FIELD_NAMES,
        )
        for i, m in enumerate(schema.extra_methods):
            schema.extra_methods[i].body = lint_code_fragment(m.body, what="method body")
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("TriggerZone.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
