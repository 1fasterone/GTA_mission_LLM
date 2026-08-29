"""Bike micro-generator — a mountable 2D bike the player can ride.

Deliberately NOT the 3D Control Surface Stack's multi_mode registry
(docs/CONTROL_SURFACE_STACK.md) — that system is explicitly documented as
3D-only ("the stack can't switch [dimension] at runtime") and its
vehicle_bike mode kind is Phase 1c, not yet built. This is a standalone
entity instead: the player's own controller (2d_fighting etc.) is untouched
and stays the single project-wide Subsystem unity_compose.py's "one
controller variant per project" contract requires; mounting a Bike entity
just tells that controller to stand down for a moment
(Controller.cs.j2's SetMovementLocked, called via SendMessage — zero
compile-time coupling, same convention as every other cross-micro hook in
this codebase) while the bike drives itself and carries the player along
parented to it.

Ramps are a separate, deterministic fixer pass (unity_pipeline.py's
_apply_ramp_tilts), not part of this micro — a ramp is an ordinary prop
entity with model_hint mentioning "ramp"; ordinary Unity physics (an angled
BoxCollider2D) launches a fast-moving Rigidbody2D off it with no special
code here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, clamp_enum, apply_extensions_safe_block,
    UNITY_EXTRA_INSTRUCTIONS, template_field_types,
)


BikeVariant = Literal["mountable_2d"]
VARIANTS = list(BikeVariant.__args__)   # type: ignore[attr-defined]


class BikeSchema(CodeExtensibleSchema):
    name:         str   = "Bike"
    variant:      str   = "mountable_2d"
    drive_force:  float = 18.0    # horizontal acceleration while mounted, m/s^2-ish
    max_speed:    float = 12.0    # m/s cap
    jump_impulse: float = 9.0     # manual hop (Space/Jump) — e.g. clearing a gap
    lean_angle:   float = 15.0    # visual tilt (degrees) in the direction of travel
    mount_range:  float = 1.5     # meters — how close the rider must be to mount
    mount_key:    str   = "E"     # single letter, legacy Input (matches PlayerController's convention)
    target_tag:   str   = "Player"
    description:  str   = "Mountable bike"
    meta:         dict  = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "mountable_2d"

    @field_validator("mount_key", mode="before")
    @classmethod
    def _clamp_key(cls, v: str) -> str:
        s = str(v).strip().upper()
        return s if len(s) == 1 and s.isalpha() else "E"


CONCEPT_PROMPT = """\
{theme_block}
Describe a mountable 2D bike for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- How the player mounts/dismounts it (walk up, press a key)
- How it accelerates/steers while ridden, and its top speed
- Whether it can jump/hop (for clearing gaps or launching off ramps)
- Its visual lean while moving, if any

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "mountable_2d",
  "drive_force": 18.0,
  "max_speed": 12.0,
  "jump_impulse": 9.0,
  "lean_angle": 15.0,
  "mount_range": 1.5,
  "mount_key": "E",
  "target_tag": "Player",
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class BikePipeline(UnityMicroPipeline):
    kind            = "bike"
    display_name    = "Unity Mountable Bike"
    schema_cls      = BikeSchema
    variants        = VARIANTS
    default_variant = "mountable_2d"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "bike"

    _RESERVED_FIELD_NAMES = {
        "driveforce", "maxspeed", "jumpimpulse", "leanangle", "mountrange",
        "mountkey", "targettag", "_rb", "_mounted", "_rider", "_grounded",
        "_baseScale",
    }
    _TEMPLATE_USINGS = {"UnityEngine"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "Bike")
        schema.drive_force  = max(1.0, min(200.0, schema.drive_force))
        schema.max_speed    = max(1.0, min(60.0, schema.max_speed))
        schema.jump_impulse = max(0.0, min(40.0, schema.jump_impulse))
        schema.lean_angle   = max(0.0, min(45.0, schema.lean_angle))
        schema.mount_range  = max(0.3, min(10.0, schema.mount_range))
        if not schema.target_tag.strip():
            schema.target_tag = "Player"
        # template_field_types: without it, this template's OWN fields
        # (_rb/_riderRb: Rigidbody2D, etc.) are invisible to the misuse
        # checks — only schema.extra_fields were ground truth otherwise.
        # Confirmed live (2026-08-28), Bike.cs(36): LLM-authored awake_extra
        # assigned _riderRb from GetComponent<Rigidbody>() (the 3D type)
        # instead of GetComponent<Rigidbody2D>() — see
        # _repair_getcomponent_wrong_dimension's own docstring.
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS,
                        template_field_types=template_field_types(
                            str(self.template_root), variant))
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("Bike.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
