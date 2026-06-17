"""Physics / collider micro-generator."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ..micro_pipeline import MicroPipeline, to_pascal
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, UNITY_EXTRA_INSTRUCTIONS,
)


PhysicsVariant = Literal[
    "rigidbody_2d", "rigidbody_3d", "kinematic_character",
    "ragdoll", "vehicle_wheels", "soft_body_stub",
    "trigger_zone", "destructible_object", "buoyancy",
]
VARIANTS = list(PhysicsVariant.__args__)   # type: ignore[attr-defined]

ColliderShape = Literal["box", "sphere", "capsule", "mesh"]


class PhysicsSchema(CodeExtensibleSchema):
    name:            str   = "PhysicsBody"
    variant:         str   = "rigidbody_3d"
    mass:            float = 1.0
    drag:            float = 0.05
    angular_drag:    float = 0.05
    gravity_scale:   float = 1.0    # used for 2D variant
    collider_shape:  ColliderShape = "box"
    is_trigger:      bool  = False
    layer:           str   = "Default"
    tag:             str   = "Untagged"
    impact_events:   bool  = False
    description:     str   = "Physics body"
    meta:            dict  = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "rigidbody_3d"


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity physics body / collider configuration for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- What the object represents, mass/drag/feel
- Collider shape and whether it's a trigger
- Whether collision/trigger impact events fire to listeners

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
COLLIDER_SHAPE — box | sphere | capsule | mesh

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "mass": 1.0,
  "drag": 0.05,
  "angular_drag": 0.05,
  "gravity_scale": 1.0,
  "collider_shape": "box",
  "is_trigger": false,
  "layer": "Default",
  "tag": "Untagged",
  "impact_events": false,
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class PhysicsPipeline(MicroPipeline):
    kind            = "physics"
    display_name    = "Unity Physics Body"
    schema_cls      = PhysicsSchema
    variants        = VARIANTS
    default_variant = "rigidbody_3d"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "physics"

    _RESERVED_FIELD_NAMES = {
        "mass", "lineardrag", "angulardrag", "gravityscale", "istrigger",
        "waterlevel", "floatforce",
    }
    _TEMPLATE_USINGS = {"UnityEngine"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "PhysicsBody")
        if schema.mass <= 0:           schema.mass = 1.0
        if schema.drag < 0:            schema.drag = 0.0
        if schema.angular_drag < 0:    schema.angular_drag = 0.0
        # Trigger zones must be triggers
        if variant == "trigger_zone":  schema.is_trigger = True
        # Kinematic character implies impact events for hit response
        if variant == "kinematic_character":
            schema.impact_events = True
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("Body.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )

        count = 1
        if schema.impact_events:
            (scripts_dir / "ImpactDispatcher.cs").write_text(
                env.get_template("ImpactDispatcher.cs.j2").render(s=schema, variant=variant),
                encoding="utf-8",
            )
            count += 1

        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count += 1
        return count
