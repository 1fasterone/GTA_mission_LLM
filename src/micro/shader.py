"""Shader/material micro-generator — runtime material-property driving.

Drives shader properties at runtime via MaterialPropertyBlock (never touches
the shared Material asset, so instances don't leak) — color cycling, dissolve
sweeps, hit-flash tints, scrolling UVs, and pulsing outline/rim intensity.
Backed by the model-category SetMaterial atom.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import CodeField, CodeMethod, lint_code_fragment, lint_usings, lint_fields


ShaderVariant = Literal[
    "color_cycle", "dissolve", "hit_flash", "uv_scroll", "outline_pulse",
]
VARIANTS = list(ShaderVariant.__args__)  # type: ignore[attr-defined]

_DEFAULT_PROPERTY = {
    "color_cycle":   "_Color",
    "dissolve":      "_DissolveAmount",
    "hit_flash":     "_Color",
    "uv_scroll":     "_MainTex",
    "outline_pulse": "_OutlineIntensity",
}


class ColorStop(BaseModel):
    """One keyframe in a color_cycle sequence."""
    r:    float = 1.0
    g:    float = 1.0
    b:    float = 1.0
    a:    float = 1.0
    hold: float = 1.0

    @field_validator("r", "g", "b", "a", mode="before")
    @classmethod
    def _clamp_channel(cls, v) -> float:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 1.0

    @field_validator("hold", mode="before")
    @classmethod
    def _clamp_hold(cls, v) -> float:
        try:
            return max(0.05, float(v))
        except (TypeError, ValueError):
            return 1.0


class ShaderSchema(BaseModel):
    name:        str = "ShaderController"
    variant:     str = "color_cycle"
    description: str = "Shader/material property controller"

    # Shared: the shader property this controller drives.
    property_name: str = ""

    # color_cycle fields
    stops: list[ColorStop] = Field(default_factory=list)
    loop:  bool            = True

    # dissolve fields
    duration:         float = 1.0
    auto_start:        bool = True
    reverse_on_repeat: bool = False

    # hit_flash fields
    flash_color_r: float = 1.0
    flash_color_g: float = 1.0
    flash_color_b: float = 1.0
    flash_duration: float = 0.15

    # uv_scroll fields
    speed_x: float = 0.5
    speed_y: float = 0.0

    # outline_pulse fields
    min_value:  float = 0.0
    max_value:  float = 1.0
    pulse_speed: float = 2.0

    extra_usings:  list[str]        = Field(default_factory=list)
    extra_fields:  list[CodeField]  = Field(default_factory=list)
    extra_methods: list[CodeMethod] = Field(default_factory=list)
    meta:          dict             = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "color_cycle"

    @field_validator("duration", "flash_duration", mode="before")
    @classmethod
    def _clamp_positive(cls, v) -> float:
        try:
            return max(0.01, float(v))
        except (TypeError, ValueError):
            return 1.0

    @field_validator("pulse_speed", mode="before")
    @classmethod
    def _clamp_pulse_speed(cls, v) -> float:
        try:
            return max(0.01, float(v))
        except (TypeError, ValueError):
            return 2.0

    @field_validator("flash_color_r", "flash_color_g", "flash_color_b", mode="before")
    @classmethod
    def _clamp_flash_channel(cls, v) -> float:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 1.0


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity runtime shader/material controller for this idea:

"{user_prompt}"

Variant: {variant}

Cover in 1-2 paragraphs:
- Which shader property it drives and why (color, dissolve amount, UV offset, outline intensity)
- When/how it triggers (on start, on event, continuous loop, one-shot)
- Timing and range (duration, speed, min/max)

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into a shader schema. Output ONLY JSON.

Variant: {variant}

color_cycle    — MaterialPropertyBlock cycles a color property through keyframe stops
dissolve       — sweeps a float property (e.g. _DissolveAmount) from 0 to 1 over a duration
hit_flash      — briefly tints a color property, then restores it (damage feedback)
uv_scroll      — continuously offsets a texture property's UV (water/lava/conveyor)
outline_pulse  — sinusoidally pulses a float property between min and max (highlight/glow)

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCaseName",
  "variant": "{variant}",
  "description": "One sentence.",
  "property_name": "",
  "stops": [],
  "loop": true,
  "duration": 1.0,
  "auto_start": true,
  "reverse_on_repeat": false,
  "flash_color_r": 1.0,
  "flash_color_g": 1.0,
  "flash_color_b": 1.0,
  "flash_duration": 0.15,
  "speed_x": 0.5,
  "speed_y": 0.0,
  "min_value": 0.0,
  "max_value": 1.0,
  "pulse_speed": 2.0,
  "extra_usings": [],
  "extra_fields": [],
  "extra_methods": []
}}

For color_cycle, populate "stops" with 2+ objects:
  {{"r": 1.0, "g": 0.0, "b": 0.0, "a": 1.0, "hold": 1.0}}

Leave "property_name" empty to use the variant's sensible default.
Output ONLY valid JSON.
"""


class ShaderPipeline(UnityMicroPipeline):
    kind            = "shader"
    display_name    = "Unity Shader/Material Controller"
    schema_cls      = ShaderSchema
    variants        = VARIANTS
    default_variant = "color_cycle"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "shader"

    _RESERVED_FIELD_NAMES = {
        "_renderer", "_block", "_propId", "_routine", "_baseColor", "_baseValue",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "System.Collections"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "ShaderController")

        if not schema.property_name:
            schema.property_name = _DEFAULT_PROPERTY[variant]

        if variant == "color_cycle" and len(schema.stops) < 2:
            schema.stops = [
                ColorStop(r=1.0, g=1.0, b=1.0, a=1.0, hold=1.0),
                ColorStop(r=0.2, g=0.6, b=1.0, a=1.0, hold=1.0),
            ]
        elif variant == "outline_pulse" and schema.min_value > schema.max_value:
            schema.min_value, schema.max_value = schema.max_value, schema.min_value
        elif variant == "uv_scroll" and schema.speed_x == 0.0 and schema.speed_y == 0.0:
            schema.speed_x = 0.5

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
            env.get_template("Shader.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
