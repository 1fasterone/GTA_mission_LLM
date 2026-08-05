"""Health micro-generator — damage/heal/death with regen, shield, armor, lives."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, clamp_enum, UNITY_EXTRA_INSTRUCTIONS,
)


HealthVariant = Literal[
    "simple", "regenerating", "shielded", "armor", "lives",
]
VARIANTS = list(HealthVariant.__args__)   # type: ignore[attr-defined]

DeathAction = Literal["destroy", "disable", "respawn", "event_only"]


class HealthSchema(CodeExtensibleSchema):
    name:          str         = "HealthSystem"
    variant:       str         = "simple"
    death_action:  DeathAction = "event_only"
    max_health:    float       = 100.0
    start_health:  float       = 100.0
    regen_rate:    float       = 5.0      # hp/sec (regenerating)
    regen_delay:   float       = 3.0      # seconds after damage before regen (regenerating)
    shield:        float       = 50.0     # absorbs first (shielded)
    armor:         float       = 0.25     # 0..1 damage reduction (armor)
    lives:         int         = 3        # (lives)
    invuln_time:   float       = 0.5      # i-frames after a hit
    description:   str         = "Health system"
    meta:          dict        = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "simple"

    @field_validator("death_action", mode="before")
    @classmethod
    def _clamp_death(cls, v: str) -> str:
        return clamp_enum(v, ("destroy", "disable", "respawn", "event_only"), "event_only")


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity health system for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- Max health and what damage/healing looks like
- Any regeneration, shield, armor (damage reduction), or extra lives
- What happens on death (destroy, disable, respawn, or just fire an event)
- Brief invulnerability after a hit

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
DEATH_ACTION — destroy | disable | respawn | event_only

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "death_action": "event_only",
  "max_health": 100.0,
  "start_health": 100.0,
  "regen_rate": 5.0,
  "regen_delay": 3.0,
  "shield": 50.0,
  "armor": 0.25,
  "lives": 3,
  "invuln_time": 0.5,
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class HealthPipeline(UnityMicroPipeline):
    kind            = "health"
    display_name    = "Unity Health"
    schema_cls      = HealthSchema
    variants        = VARIANTS
    default_variant = "simple"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "health"

    _RESERVED_FIELD_NAMES = {
        "maxhealth", "starthealth", "regenrate", "regendelay", "shield", "armor",
        "lives", "invulntime", "_current", "_shield", "_lives", "_lasthit", "_invuln",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "System"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "HealthSystem")
        if schema.max_health <= 0:   schema.max_health = 100.0
        # start_health clamped into (0, max].
        if schema.start_health <= 0 or schema.start_health > schema.max_health:
            schema.start_health = schema.max_health
        if schema.regen_rate < 0:    schema.regen_rate = 5.0
        if schema.regen_delay < 0:   schema.regen_delay = 3.0
        if schema.shield < 0:        schema.shield = 0.0
        schema.armor = min(0.95, max(0.0, schema.armor))   # cap so damage never fully nullified
        if schema.lives < 1:         schema.lives = 1
        if schema.invuln_time < 0:   schema.invuln_time = 0.0
        # hoist_field_inits: this generator's template renders awake_extra,
        # so a CS0236 initializer can be re-homed there instead of dropped.
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS,
                        hoist_field_inits=True)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("Health.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
