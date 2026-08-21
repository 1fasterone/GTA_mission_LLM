"""VFX micro-generator — particle systems, VFX sequences, screen effects, impact bursts.

Backed by FX atoms: SpawnParticle, LightFlash, ScreenFade.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import CodeField, CodeMethod, lint_code_fragment, lint_usings, lint_fields


VfxVariant = Literal[
    "particle_system", "vfx_sequence", "screen_fx", "impact_fx",
]
VARIANTS = list(VfxVariant.__args__)  # type: ignore[attr-defined]


class VfxCue(BaseModel):
    """One effect event in a scripted VFX sequence."""
    effect_id: str        = "Explosion"
    x:         float      = 0.0
    y:         float      = 0.0
    z:         float      = 0.0
    scale:     float      = 1.0
    delay:     float      = 0.0

    @field_validator("scale", mode="before")
    @classmethod
    def _clamp_scale(cls, v) -> float:
        try:
            return max(0.01, float(v))
        except (TypeError, ValueError):
            return 1.0

    @field_validator("delay", mode="before")
    @classmethod
    def _clamp_delay(cls, v) -> float:
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return 0.0


class VfxSchema(BaseModel):
    name:        str = "VFXController"
    variant:     str = "particle_system"
    description: str = "VFX controller"

    # particle_system fields
    effect_id:     str   = "Explosion"
    loop:          bool  = False
    play_on_start: bool  = True
    auto_destroy:  bool  = True
    duration:      float = 2.0

    # vfx_sequence fields
    cues: list[VfxCue] = Field(default_factory=list)

    # screen_fx fields
    fade_color_r:  float = 0.0
    fade_color_g:  float = 0.0
    fade_color_b:  float = 0.0
    fade_duration: float = 1.0
    flash_intensity: float = 5.0
    flash_duration:  float = 0.05

    # impact_fx / zone fields
    trigger_tag: str  = "Enemy"
    pool_size:   int  = 8

    extra_usings:  list[str]        = Field(default_factory=list)
    extra_fields:  list[CodeField]  = Field(default_factory=list)
    extra_methods: list[CodeMethod] = Field(default_factory=list)
    meta:          dict             = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "particle_system"

    @field_validator("duration", "fade_duration", "flash_duration", mode="before")
    @classmethod
    def _clamp_positive(cls, v) -> float:
        try:
            return max(0.01, float(v))
        except (TypeError, ValueError):
            return 1.0

    @field_validator("flash_intensity", mode="before")
    @classmethod
    def _clamp_intensity(cls, v) -> float:
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return 5.0

    @field_validator("pool_size", mode="before")
    @classmethod
    def _clamp_pool(cls, v) -> int:
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            return 8


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity VFX system for this idea:

"{user_prompt}"

Variant: {variant}

Cover in 1-2 paragraphs:
- What visual effects play (particles, light flashes, screen fades, impact bursts)
- When and where they trigger (on event, on collision, on start, looping, timed sequence)
- Any pooling, auto-destroy, or duration considerations

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into a VFX schema. Output ONLY JSON.

Variant: {variant}

particle_system  — wraps a single ParticleSystem component; play/stop/loop control
vfx_sequence     — coroutine-driven sequence of prefab spawns with per-cue delays and positions
screen_fx        — full-screen fade + optional light flash (uses UI Image as overlay)
impact_fx        — spawns particles on collision/trigger enter; object-pooled for performance

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCaseName",
  "variant": "{variant}",
  "description": "One sentence.",
  "effect_id": "Explosion",
  "loop": false,
  "play_on_start": true,
  "auto_destroy": true,
  "duration": 2.0,
  "cues": [],
  "fade_color_r": 0.0,
  "fade_color_g": 0.0,
  "fade_color_b": 0.0,
  "fade_duration": 1.0,
  "flash_intensity": 5.0,
  "flash_duration": 0.05,
  "trigger_tag": "Enemy",
  "pool_size": 8,
  "extra_usings": [],
  "extra_fields": [],
  "extra_methods": []
}}

For vfx_sequence, populate "cues" with objects:
  {{"effect_id": "Spark", "x": 0, "y": 0, "z": 0, "scale": 1.0, "delay": 0.5}}

Output ONLY valid JSON.
"""


class VfxPipeline(UnityMicroPipeline):
    kind            = "vfx"
    display_name    = "Unity VFX"
    schema_cls      = VfxSchema
    variants        = VARIANTS
    default_variant = "particle_system"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "vfx"

    _RESERVED_FIELD_NAMES = {
        "_ps", "_overlay", "_light", "_pool", "_alive",
        "_fadeRoutine", "_flashRoutine",
    }
    _TEMPLATE_USINGS = {
        "UnityEngine", "System.Collections", "System.Collections.Generic",
        "UnityEngine.UI",
    }

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "VFXController")

        if not schema.effect_id:
            schema.effect_id = "Explosion"
        if not schema.trigger_tag:
            schema.trigger_tag = "Enemy"
        if schema.pool_size < 1:
            schema.pool_size = 8

        # Variant coherence
        if variant == "particle_system":
            if not schema.loop:
                schema.auto_destroy = True
        elif variant == "screen_fx":
            for attr in ("fade_color_r", "fade_color_g", "fade_color_b"):
                val = getattr(schema, attr, 0.0)
                setattr(schema, attr, max(0.0, min(1.0, float(val))))
        elif variant == "vfx_sequence" and not schema.cues:
            schema.cues = [VfxCue(effect_id=schema.effect_id, delay=0.0)]

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
            env.get_template("VFX.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
