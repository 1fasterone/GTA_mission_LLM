"""Camera rig micro-generator."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, apply_extensions_safe_block,
    UNITY_EXTRA_INSTRUCTIONS,
)


CameraVariant = Literal[
    "follow", "follow_2d", "follow_2_5d", "orbit", "first_person",
    "cinemachine_3p", "cinemachine_topdown",
    "rts_pan_zoom", "vehicle_chase", "cinematic_dolly",
    # Frames a BOARD instead of following a body. Every variant above resolves
    # a follow target by tag and returns early when it finds none, so in a game
    # that declared it has no avatar (locomotion='none') they all sit at the
    # scene origin doing nothing. This one fits the content it can see and
    # holds it, which is the only camera behaviour a board needs.
    "board_framing",
]
VARIANTS = list(CameraVariant.__args__)   # type: ignore[attr-defined]


class CameraRigSchema(CodeExtensibleSchema):
    name:                str   = "CameraRig"
    variant:             str   = "follow"
    follow_target_tag:   str   = "Player"
    offset_x:            float = 0.0
    offset_y:            float = 3.0
    offset_z:            float = -6.0
    smooth_time:         float = 0.15
    collision_avoidance: bool  = False
    lock_on:             bool  = False
    fov_base:            float = 60.0
    fov_sprint:          float = 70.0
    description:         str   = "Camera rig"
    meta:                dict  = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "follow"


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity camera rig for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- What it follows, the offset and the feel (snappy vs floaty)
- Whether it uses Cinemachine or a plain MonoBehaviour
- Collision avoidance, lock-on, FOV behaviour

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON matching this schema. Output JSON only.

VARIANT MUST BE: {variant}

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "follow_target_tag": "Player",
  "offset_x": 0.0,
  "offset_y": 3.0,
  "offset_z": -6.0,
  "smooth_time": 0.15,
  "collision_avoidance": false,
  "lock_on": false,
  "fov_base": 60.0,
  "fov_sprint": 70.0,
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class CameraRigPipeline(UnityMicroPipeline):
    kind            = "camera"
    display_name    = "Unity Camera Rig"
    schema_cls      = CameraRigSchema
    variants        = VARIANTS
    default_variant = "follow"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "camera"

    _RESERVED_FIELD_NAMES = {
        "followtag", "offset", "smoothtime", "occlusionmask", "lockon",
        "fovbase", "fovsprint", "_target", "_vel", "_cam",
    }
    _TEMPLATE_USINGS = {"UnityEngine"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "CameraRig")
        if schema.smooth_time <= 0:    schema.smooth_time = 0.15
        if schema.fov_base    <= 0:    schema.fov_base    = 60.0
        if schema.fov_sprint  < schema.fov_base:
            schema.fov_sprint = schema.fov_base
        # First-person rig is rooted on the head — zero out follow offsets
        if variant == "first_person":
            schema.offset_x = schema.offset_y = schema.offset_z = 0.0
        # hoist_field_inits: this generator's template renders awake_extra,
        # so a CS0236 initializer can be re-homed there instead of dropped.
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS,
                        hoist_field_inits=True)
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("CameraRig.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
